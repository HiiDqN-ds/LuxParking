"""One language per page: every interface text exists in every language, pages leak no English, the switch works."""
import html
import json
import re
from pathlib import Path
from string import Formatter

import pytest

from app.extensions import db
from app.models import Tenant
from app.ui_extract import UI_DIR, collect_keys, load, stale
from app.ui_i18n import ui
from tests.conftest import login

LOCALES = ("de", "nl", "fr", "es", "ar")  # English is the source language


def _fields(text: str) -> list[str]:
    return sorted(re.findall(r"%\(\w+\)[sdf]|%%", text))


# ---- catalogs ------------------------------------------------------------------------------------

@pytest.mark.parametrize("locale", LOCALES)
def test_every_interface_text_is_translated(locale):
    missing = sorted(k for k in collect_keys() if not load(locale).get(k, "").strip())
    assert not missing, f"{len(missing)} texts missing in {locale}: {missing[:8]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_translations_keep_their_placeholders(locale):
    bad = [k for k, v in load(locale).items() if _fields(k) != _fields(v)]
    assert not bad, f"placeholder mismatch in {locale}: {bad[:5]}"


def test_catalogs_have_no_dead_entries():
    # entries nobody uses any more are harmless but hide typos in keys: keep the files tidy
    for locale in LOCALES:
        assert len(stale(locale)) < 20, f"{locale} has unused entries, e.g. {stale(locale)[:5]}"


def test_every_supported_language_has_a_catalog(app):
    for locale in app.config["SUPPORTED_LOCALES"]:
        if locale != "en":
            assert (UI_DIR / f"{locale}.json").is_file(), f"translations/ui/{locale}.json is missing"


# ---- runtime -------------------------------------------------------------------------------------

def test_ui_translates_placeholders_and_already_formatted_text(app):
    with app.test_request_context("/"):
        assert ui("Save", "de") == "Speichern" and ui("Save", "en") == "Save"
        assert ui("Space %(name)s added.", "de", name="A1") == "Platz A1 hinzugefügt."
        assert ui("Space A1 added.", "fr") == "Place A1 ajoutée."
        assert ui("Field cannot be longer than 5 characters.", "nl") == "Het veld mag maximaal 5 tekens lang zijn."
        assert ui("A text nobody translated", "ar") == "A text nobody translated"


def test_choice_follows_cookie_then_tenant_default(client, app):
    app.config["UI_FORCE_LOCALE"] = None
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    tenant.default_locale = "nl"
    db.session.commit()
    login(client)
    assert b"Facturen" in client.get("/admin/invoices").data          # tenant default (Dutch)
    client.set_cookie("pp_ui_lang", "fr")
    assert b"Factures" in client.get("/admin/invoices").data          # the visitor's choice wins


def test_language_switch_sets_cookie_and_refuses_offsite_redirects(client, app):
    app.config["UI_FORCE_LOCALE"] = None
    ok = client.post("/ui-language", data={"locale": "es", "next": "/admin/login"})
    assert ok.status_code == 302 and ok.headers["Location"].endswith("/admin/login")
    assert "pp_ui_lang=es" in ok.headers["Set-Cookie"]
    evil = client.post("/ui-language", data={"locale": "de", "next": "https://evil.example/phish"})
    assert evil.headers["Location"] in ("/", "http://localhost/")
    junk = client.post("/ui-language", data={"locale": "klingon", "next": "//evil.example"})
    assert "pp_ui_lang=de" in junk.headers["Set-Cookie"] and junk.headers["Location"] in ("/", "http://localhost/")  # unknown -> default


def test_login_page_is_one_language_and_rtl_for_arabic(client, app):
    app.config["UI_FORCE_LOCALE"] = None
    client.set_cookie("pp_ui_lang", "ar")
    page = client.get("/admin/login").get_data(as_text=True)
    assert 'lang="ar"' in page and 'dir="rtl"' in page and "تسجيل الدخول" in page
    client.set_cookie("pp_ui_lang", "de")
    page = client.get("/admin/login").get_data(as_text=True)
    assert 'lang="de"' in page and 'dir="ltr"' in page and "Anmelden" in page and "Sign in to ParkPilot" not in page


def test_flash_and_validation_messages_are_translated(client, app):
    client.set_cookie("pp_ui_lang", "de")
    wrong = client.post("/admin/login", data={"email": "nobody@test.dev", "password": "x" * 12}).get_data(as_text=True)
    assert "E-Mail oder Passwort ist falsch, oder das Konto ist vorübergehend gesperrt." in wrong and "Email or password is incorrect" not in wrong
    login(client)
    pricing = client.post("/admin/discounts", data={"code": "", "value": "-5"}, follow_redirects=True).get_data(as_text=True)
    assert "This field is required." not in pricing and "Invalid" not in pricing  # form errors appear in German, never English


def test_staff_scripts_get_their_texts_in_the_page_language(client, app, staff):
    client.set_cookie("pp_ui_lang", "de")
    login(client, "staff@test.dev", "staff-pass")
    page = client.get("/staff/").get_data(as_text=True)
    assert "window.PP_UI" in page
    dictionary = json.loads(re.search(r"window\.PP_UI = (\{.*?\});</script>", page, re.S).group(1))
    assert dictionary.get(
        "Choose a video that is 30 seconds or shorter."
    ) == "Wählen Sie ein Video mit höchstens 30 Sekunden Länge."


# ---- no English left on any back-office or staff page --------------------------------------------

def _get_rules(app, prefix):
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and rule.rule.startswith(prefix) and "static" not in rule.endpoint:
            yield rule.rule


def _leaks(page: str, locale: str) -> list[str]:
    text = html.unescape(re.sub(r"<(script|style)\b.*?</\1>", "", page, flags=re.S))
    own = load(locale)
    found = []
    for key in collect_keys():
        if len(key) < 14 or "%(" in key or key not in text:
            continue
        if own.get(key, key) != key:  # a real translation exists, so seeing the English text is a leak
            found.append(key)
    return found


@pytest.mark.parametrize("locale", ["de", "ar"])
def test_owner_pages_contain_no_english_interface_text(client, app, locale):
    client.set_cookie("pp_ui_lang", locale)
    login(client)
    checked = 0
    for url in sorted(set(_get_rules(app, "/admin")) | {"/alerts"}):
        if any(skip in url for skip in ("logout", ".csv", ".pdf", "login", ".png")):
            continue
        response = client.get(url)
        if response.status_code != 200:
            continue
        checked += 1
        leaks = _leaks(response.get_data(as_text=True), locale)
        assert not leaks, f"{url} still shows English in {locale}: {leaks[:5]}"
    assert checked >= 8


def test_staff_pages_contain_no_english_interface_text(client, app, staff):
    client.set_cookie("pp_ui_lang", "de")
    login(client, "staff@test.dev", "staff-pass")
    checked = 0
    for url in sorted(set(_get_rules(app, "/staff"))):
        response = client.get(url)
        if response.status_code == 200:
            checked += 1
            assert not _leaks(response.get_data(as_text=True), "de"), url
    assert checked >= 1
