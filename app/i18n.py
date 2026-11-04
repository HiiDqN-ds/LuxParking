"""Small file-backed translation catalogs for public pages."""
import json
from functools import lru_cache
from pathlib import Path
from string import Formatter

from flask import current_app, g, has_app_context

_CATALOG_DIR = Path(__file__).with_name("translations")


@lru_cache(maxsize=32)
def _load_catalog(locale: str) -> dict[str, str]:
    path = _CATALOG_DIR / f"{locale}.json"
    try:
        with path.open(encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Translation catalog is missing for locale '{locale}'.") from exc
    if not isinstance(catalog, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in catalog.items()
    ):
        raise RuntimeError(f"Translation catalog '{locale}' must map string keys to string values.")
    return catalog


def supported_locales() -> tuple[str, ...]:
    locales = tuple(current_app.config.get("SUPPORTED_LOCALES", ()))
    if not locales:
        raise RuntimeError("At least one supported locale must be configured.")
    missing = [locale for locale in locales if not (_CATALOG_DIR / f"{locale}.json").is_file()]
    if missing:
        raise RuntimeError("Missing translation catalogs for: " + ", ".join(missing))
    return locales


def normalize_locale(locale: str | None) -> str:
    supported = supported_locales()
    candidate = (locale or "").replace("_", "-").lower()
    if candidate in supported:
        return candidate
    language = candidate.split("-", 1)[0]
    if language in supported:
        return language
    return current_app.config.get("DEFAULT_LOCALE", supported[0])


def translate(locale: str, key: str, **values: object) -> str:
    normalized = normalize_locale(locale)
    tenant_id = g.get("public_tenant_id") if has_app_context() else None
    if tenant_id is not None:
        from sqlalchemy import select

        from .extensions import db
        from .models import TenantTranslation

        override = db.session.execute(
            select(TenantTranslation.value).where(
                TenantTranslation.__table__.c.tenant_id == tenant_id,
                TenantTranslation.__table__.c.locale == normalized,
                TenantTranslation.__table__.c.key == key,
            )
        ).scalar_one_or_none()
        if override:
            return override.format(**values) if values else override
    catalog = _load_catalog(normalized)
    fallback = _load_catalog(current_app.config.get("DEFAULT_LOCALE", "de"))
    text = catalog.get(key, fallback.get(key, key))
    return text.format(**values) if values else text


def valid_translation_override(locale: str, key: str, value: str) -> bool:
    """Require tenant text to keep the catalog string's interpolation fields."""
    normalized = normalize_locale(locale)
    catalog = _load_catalog(normalized)
    fallback = _load_catalog(current_app.config.get("DEFAULT_LOCALE", "de"))
    original = catalog.get(key, fallback.get(key, key))

    def format_fields(text: str) -> list[tuple[str, str, str]]:
        return sorted(
            (field_name or "", format_spec, conversion or "")
            for _, field_name, format_spec, conversion in Formatter().parse(text)
        )

    try:
        return format_fields(value) == format_fields(original)
    except ValueError:
        return False


def translation_keys(locale: str, prefix: str) -> tuple[str, ...]:
    normalized = normalize_locale(locale)
    return tuple(key for key in _load_catalog(normalized) if key.startswith(prefix))


LANGUAGE_NAMES = {
    "de": "Deutsch",
    "en": "English",
    "nl": "Nederlands",
    "fr": "Français",
    "es": "Español",
    "ar": "العربية",
    "he": "עברית",
    "fa": "فارسی",
    "ur": "اردو",
}


def language_name(locale: str) -> str:
    return LANGUAGE_NAMES.get(locale, locale)
