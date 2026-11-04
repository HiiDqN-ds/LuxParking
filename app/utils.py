"""Small helpers: slugs, grid layout, audit logging, safe redirects."""
import re
import unicodedata
from urllib.parse import urlparse

from flask import request
from flask_login import current_user

from .extensions import db
from .models import AuditLog

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def slugify(text: str) -> str:
    text = text.translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


# Default parking grid: 10 columns, rows lettered A, B, C ... on a 1000x700 canvas.
GRID_COLS, CELL_W, CELL_H, PITCH_X, PITCH_Y, MARGIN_X, MARGIN_Y = 10, 80, 110, 92, 125, 40, 40


def grid_position(index: int) -> dict:
    row, col = divmod(index, GRID_COLS)
    return {"x": MARGIN_X + col * PITCH_X, "y": MARGIN_Y + row * PITCH_Y, "w": CELL_W, "h": CELL_H}


def grid_name(index: int) -> str:
    row, col = divmod(index, GRID_COLS)
    return f"{chr(ord('A') + row % 26)}{col + 1}"


def audit(action: str, entity: str = "", entity_id=None, **data) -> None:
    """Queue an audit entry in the current session (committed with the caller's transaction)."""
    db.session.add(AuditLog(
        tenant_id=getattr(current_user, "tenant_id", None),
        user_id=getattr(current_user, "id", None),
        action=action, entity=entity, entity_id=entity_id, data=data or None,
        ip=request.remote_addr if request else None,
    ))


def is_safe_next(target: str | None) -> bool:
    """Only allow same-site relative redirects after login (blocks open-redirect)."""
    if not target:
        return False
    parts = urlparse(target)
    return not parts.scheme and not parts.netloc and target.startswith("/") and not target.startswith("//")
