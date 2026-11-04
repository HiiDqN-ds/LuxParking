"""One language for the whole interface.

Public pages are translated with `translate(locale, key)` (translations/<locale>.json). Everything else (owner back
office, staff app, login, error pages, flash messages, form labels and validation errors) was written in English and is
translated here with the English text itself as the key: translations/ui/<locale>.json maps "English text" -> "text".

    Templates:  {{ _('Save') }}   {{ _('Space %(name)s added.', name=space.name) }}
    Python:     flash(ui('Space %(name)s added.', name=space.name))

Rules that keep a page in a single language
- The interface language is the URL language on public pages, otherwise the visitor's choice (cookie, set with the
  language switch) and otherwise the tenant's default language.
- A missing translation falls back to English, and the test-suite (tests/test_ui_translations.py) fails when any text
  used by a template or by Python code is missing in any language, so mixed pages cannot ship unnoticed.
- Messages that were already formatted (exception texts) are matched against catalog entries that contain %(name)s
  placeholders, so they are translated too.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, has_app_context, has_request_context, make_response, redirect, request
from flask_login import current_user

from .i18n import normalize_locale, supported_locales

_UI_DIR = Path(__file__).with_name("translations") / "ui"
COOKIE = "pp_ui_lang"
RTL_LOCALES = {"ar", "he", "fa", "ur"}


@lru_cache(maxsize=32)
def _catalog(locale: str) -> tuple[dict[str, str], tuple]:
    """(exact entries, [(compiled regex, translated pattern, placeholder names)] for entries with %(name)s fields)."""
    try:
        data = json.loads((_UI_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ()
    patterns = []
    for source, target in data.items():
        names = re.findall(r"%\((\w+)\)[sdf]", source)
        if not names:
            continue
        parts = re.split(r"(%\(\w+\)[sdf]|%%)", source)
        regex = ""
        for part in parts:
            field = re.match(r"%\((\w+)\)", part)
            if field:
                regex += f"(?P<{field.group(1)}>.+?)"
            elif part == "%%":
                regex += "%"
            else:
                regex += re.escape(part)
        try:
            patterns.append((re.compile(f"^{regex}$", re.S), target, names))
        except re.error:  # e.g. the same placeholder twice: exact lookups still work
            continue
    patterns.sort(key=lambda item: -len(item[0].pattern))  # most specific first
    return data, tuple(patterns)


def clear_cache() -> None:
    _catalog.cache_clear()


def ui_locale() -> str:
    """The language the current page must be written in."""
    if not has_app_context():
        return current_app.config.get("DEFAULT_LOCALE", "de")
    cached = g.get("_ui_locale")
    if cached:
        return cached
    chosen = None
    if has_request_context():
        view_locale = (request.view_args or {}).get("locale")
        if view_locale:
            chosen = view_locale
        else:
            chosen = request.cookies.get(COOKIE)
        if not chosen:
            chosen = current_app.config.get("UI_FORCE_LOCALE")  # tests / single-language deployments
        if not chosen and current_user.is_authenticated and getattr(current_user, "tenant", None) is not None:
            chosen = current_user.tenant.default_locale
    locale = normalize_locale(chosen)
    g._ui_locale = locale
    return locale


def is_rtl(locale: str | None = None) -> bool:
    return (locale or ui_locale()) in RTL_LOCALES


def ui(text, locale: str | None = None, **values) -> str:
    """Translate English `text` into the page language; `values` fill %(name)s placeholders."""
    text = str(text)
    locale = locale or ui_locale()
    exact, patterns = _catalog(locale)
    translated = exact.get(text)
    if translated is None and not values:
        for regex, target, _names in patterns:
            match = regex.match(text)
            if match:
                try:
                    return target % match.groupdict()
                except (KeyError, TypeError, ValueError):
                    break
    if translated is None:
        translated = text
    if values:
        try:
            return translated % values
        except (KeyError, TypeError, ValueError):
            return text % values if "%(" in text else text
    return translated


def js_dictionary() -> dict[str, str]:
    """Translations of the texts the staff scripts show (window.PP_UI)."""
    from .ui_extract import js_keys

    locale = ui_locale()
    exact, _patterns = _catalog(locale)
    return {key: exact[key] for key in js_keys() if key in exact} if locale != "en" else {}


def language_options() -> list[tuple[str, str]]:
    from .i18n import language_name

    return [(code, language_name(code)) for code in supported_locales()]


bp = Blueprint("uilang", __name__)


@bp.post("/ui-language")
def set_language():
    """Remember the visitor's interface language (cookie) and go back to the page they came from."""
    locale = normalize_locale(request.form.get("locale"))
    target = request.form.get("next") or request.referrer or "/"
    parts = urlsplit(target)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if parts.netloc and parts.netloc != request.host:  # never redirect off-site
        path = "/"
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    response = make_response(redirect(path))
    response.set_cookie(COOKIE, locale, max_age=60 * 60 * 24 * 365, samesite="Lax", secure=request.is_secure, httponly=True)
    return response


def init_app(app) -> None:
    app.jinja_env.globals.update(_=ui, ui_locale=ui_locale, ui_dir=lambda: "rtl" if is_rtl() else "ltr", language_options=language_options, js_dictionary=js_dictionary)
    app.jinja_env.filters["tr"] = ui  # {{ status|title|tr }}: translate a value that comes from the database or Python
    from .extensions import login_manager

    login_manager.localize_callback = ui  # "Please sign in to continue."
    app.register_blueprint(bp)
