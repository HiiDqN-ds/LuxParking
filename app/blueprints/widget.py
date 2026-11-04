"""Serves the embeddable booking widget script at /widget.js (no secrets in it; configuration is on the script tag)."""
from pathlib import Path

from flask import Blueprint, Response

from ..extensions import csrf

bp = Blueprint("widget", __name__)
_SCRIPT = Path(__file__).resolve().parent.parent / "static" / "widget" / "parkpilot-widget.js"


@bp.get("/widget.js")
@csrf.exempt
def widget_js():
    return Response(_SCRIPT.read_text(encoding="utf-8"), mimetype="application/javascript", headers={
        "Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*", "Cross-Origin-Resource-Policy": "cross-origin"})
