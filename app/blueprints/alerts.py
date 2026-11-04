"""Notification centre for owners and staff: bell with live updates, sound for new bookings, and an overview page."""
from flask import Blueprint, abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ALERT_KINDS, Role, Tenant
from ..services import alerts as svc

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


def _tenant_user():
    if not current_user.is_authenticated or current_user.tenant_id is None:
        abort(403)  # platform owners have no parking, so no alerts


def _booking_url(alert) -> str:
    if alert.booking_id is None:
        return ""
    endpoint = "staff.booking_detail" if current_user.role == Role.STAFF else "manage.booking_detail"
    return url_for(endpoint, booking_id=alert.booking_id)


@bp.app_context_processor
def inject_alert_widget():
    def alert_widget():
        if not current_user.is_authenticated or current_user.tenant_id is None:
            return None
        items = [svc.serialize(a, _booking_url(a), current_user) for a in svc.visible_to(current_user, svc.recent(current_user.tenant_id, limit=6))]
        return {"unread": svc.unread_count(current_user), "latest": svc.latest_id(current_user.tenant_id), "items": items}

    return {"alert_widget": alert_widget}


@bp.get("/feed")
@login_required
def feed():
    """New alerts after `after` (polled every few seconds by the bell)."""
    _tenant_user()
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after = 0
    fresh = svc.recent(current_user.tenant_id, after=after, limit=20)
    return jsonify({
        "alerts": [svc.serialize(a, _booking_url(a), current_user) for a in svc.visible_to(current_user, reversed(fresh))],
        "unread": svc.unread_count(current_user),
        "latest": max([a.id for a in fresh], default=after),
    })


@bp.post("/read")
@login_required
def read():
    _tenant_user()
    svc.mark_all_read(current_user)
    return jsonify({"unread": 0})


@bp.get("")
@login_required
def center():
    _tenant_user()
    kind = request.args.get("kind", "")
    tenant = db.session.get(Tenant, current_user.tenant_id)
    items = [svc.serialize(a, _booking_url(a), current_user) for a in svc.visible_to(current_user, svc.recent(tenant.id, kind=kind, limit=60))]
    base = "staff/base.html" if current_user.role == Role.STAFF else "admin/base.html"
    return render_template("alerts/center.html", base=base, items=items, stats=svc.stats(tenant), kind=kind,
                           kinds=[(k, svc.KINDS[k][0], svc.KINDS[k][2]) for k in ALERT_KINDS], unread=svc.unread_count(current_user))
