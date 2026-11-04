"""Legal pages (Impressum, Datenschutz, AGB). Business details come from LEGAL_* environment variables."""
import os

from flask import Blueprint, g, render_template

from .public import _default_public_lot, _locale

bp = Blueprint("legal", __name__)
FIELDS = ("COMPANY", "REPRESENTATIVE", "STREET", "CITY", "PHONE", "EMAIL", "VAT_ID", "REGISTER", "HOSTING", "RETENTION")


def _render(template: str):
    found = _default_public_lot()
    tenant = found[0] if found else None
    info = {name.lower(): os.environ.get(f"LEGAL_{name}", "").strip() for name in FIELDS}
    return render_template(f"legal/{template}.html", locale=_locale(None, tenant), tenant=tenant, language_links=[], legal=info)


@bp.get("/impressum")
def impressum():
    return _render("impressum")


@bp.get("/datenschutz")
def datenschutz():
    return _render("datenschutz")


@bp.get("/agb")
def agb():
    return _render("agb")
