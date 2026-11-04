"""Notification centre: what happened in the parking (new booking, payment, cancellation, check-in/out, dispute).

Alerts store facts only; the sentence is built when it is shown, in the viewer's language. Nothing here can break a
booking: recording an alert never raises.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models import ALERT_KINDS, Alert, Booking, Dispute, Tenant, utcnow
from ..ui_i18n import ui

# kind -> (icon, colour token used by the CSS, short name for filters)
KINDS = {
    "booking_created": ("🆕", "green", "New booking"),
    "booking_paid": ("💶", "blue", "Payment"),
    "booking_cancelled": ("🚫", "red", "Cancellation"),
    "booking_refunded": ("↩️", "orange", "Refund"),
    "checked_in": ("🚗", "teal", "Check-in"),
    "checked_out": ("🏁", "gray", "Check-out"),
    "dispute_opened": ("⚠️", "red", "Dispute"),
}
EVENT_TO_KIND = {"cancelled": "booking_cancelled", "paid": "booking_paid", "refunded": "booking_refunded",
                 "checked_in": "checked_in", "checked_out": "checked_out"}


def _facts(booking: Booking) -> dict:
    customer = booking.customer
    return {
        "ref": booking.booking_ref,
        "customer": f"{customer.first_name} {customer.last_name}".strip(),
        "total": f"{Decimal(booking.total_amount):.2f} {booking.currency}",
        "space": booking.space.name if booking.space is not None else "",
        "plate": booking.vehicle.plate if booking.vehicle is not None else "",
    }


def stage_alert(tenant_id: int, kind: str, booking: Booking) -> None:
    """Add an alert to the current transaction (the caller commits)."""
    if kind in ALERT_KINDS:
        db.session.add(Alert(tenant_id=tenant_id, kind=kind, booking_id=booking.id, data=_facts(booking)))


def record_alert(tenant_id: int, kind: str, booking_id: int) -> None:
    """Store an alert in its own commit. Never raises."""
    try:
        booking = db.session.execute(select(Booking).where(Booking.id == booking_id, Booking.tenant_id == tenant_id)).scalar_one_or_none()
        if booking is not None:
            stage_alert(tenant_id, kind, booking)
            db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record the %s alert for booking %s", kind, booking_id)


# Staff work with vehicles, not with customers or money: they never see names or amounts, and payment events are hidden.
STAFF_HIDDEN_KINDS = frozenset({"booking_paid", "booking_refunded"})


def visible_to(user, alerts):
    if getattr(user, "role", None) == "staff":
        return [a for a in alerts if a.kind not in STAFF_HIDDEN_KINDS]
    return list(alerts)


def staff_alert_text(alert: Alert) -> str:
    ref = (alert.data or {}).get("ref", "")
    if alert.kind == "booking_created":
        return ui("New booking %(ref)s", ref=ref)
    if alert.kind == "booking_cancelled":
        return ui("Booking %(ref)s was cancelled", ref=ref)
    if alert.kind == "checked_in":
        return ui("%(ref)s checked in", ref=ref)
    if alert.kind == "checked_out":
        return ui("%(ref)s checked out", ref=ref)
    return ui("Damage dispute opened for %(ref)s", ref=ref)


def alert_text(alert: Alert) -> str:
    d = alert.data or {}
    ref, customer, total = d.get("ref", ""), d.get("customer", ""), d.get("total", "")
    if alert.kind == "booking_created":
        return ui("New booking %(ref)s: %(customer)s, %(total)s", ref=ref, customer=customer, total=total)
    if alert.kind == "booking_paid":
        return ui("Payment received for %(ref)s (%(total)s)", ref=ref, total=total)
    if alert.kind == "booking_cancelled":
        return ui("Booking %(ref)s was cancelled", ref=ref)
    if alert.kind == "booking_refunded":
        return ui("Refund issued for %(ref)s (%(total)s)", ref=ref, total=total)
    if alert.kind == "checked_in":
        return ui("%(customer)s checked in (%(ref)s)", customer=customer, ref=ref)
    if alert.kind == "checked_out":
        return ui("%(customer)s checked out (%(ref)s)", customer=customer, ref=ref)
    return ui("Damage dispute opened for %(ref)s", ref=ref)


def time_ago(moment: datetime) -> str:
    moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 60:
        return ui("just now")
    if seconds < 3600:
        return ui("%(count)s min ago", count=seconds // 60)
    if seconds < 86400:
        return ui("%(count)s h ago", count=seconds // 3600)
    return ui("%(count)s d ago", count=seconds // 86400)


def unread_count(user) -> int:
    since = user.alerts_seen_at
    query = select(func.count(Alert.id)).where(Alert.tenant_id == user.tenant_id)
    if since is not None:
        query = query.where(Alert.created_at > since)
    return db.session.execute(query).scalar_one()


def latest_id(tenant_id: int) -> int:
    return db.session.execute(select(func.max(Alert.id)).where(Alert.tenant_id == tenant_id)).scalar_one() or 0


def recent(tenant_id: int, *, after: int = 0, kind: str = "", limit: int = 30) -> list[Alert]:
    query = select(Alert).where(Alert.tenant_id == tenant_id, Alert.id > after)
    if kind in ALERT_KINDS:
        query = query.where(Alert.kind == kind)
    return db.session.execute(query.order_by(Alert.id.desc()).limit(limit)).scalars().all()


def mark_all_read(user) -> None:
    user.alerts_seen_at = utcnow()
    db.session.commit()


def serialize(alert: Alert, url: str, user=None) -> dict:
    icon, colour, name = KINDS.get(alert.kind, ("🔔", "gray", ""))
    text = staff_alert_text(alert) if getattr(user, "role", None) == "staff" else alert_text(alert)
    return {"id": alert.id, "kind": alert.kind, "icon": icon, "colour": colour, "text": text, "ago": time_ago(alert.created_at), "url": url}


def stats(tenant: Tenant) -> dict:
    """Numbers and chart data for the notification centre."""
    try:
        zone = ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    now = datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    start_day = today - timedelta(days=13)
    since = datetime.combine(start_day, datetime.min.time(), zone).astimezone(timezone.utc)
    created = db.session.execute(select(Booking.created_at, Booking.status).where(Booking.tenant_id == tenant.id, Booking.created_at >= since)).all()
    per_day = {start_day + timedelta(days=i): 0 for i in range(14)}
    for stamp, status in created:
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        day = stamp.astimezone(zone).date()
        if day in per_day and status != "cancelled":
            per_day[day] += 1
    status_rows = dict(db.session.execute(select(Booking.status, func.count(Booking.id)).where(Booking.tenant_id == tenant.id).group_by(Booking.status)).all())
    arriving = db.session.execute(select(func.count(Booking.id)).where(
        Booking.tenant_id == tenant.id, Booking.status == "reserved", Booking.pickup_at > now, Booking.pickup_at <= now + timedelta(hours=24))).scalar_one()
    open_disputes = db.session.execute(select(func.count(Dispute.id)).where(Dispute.tenant_id == tenant.id, Dispute.status == "open")).scalar_one()
    peak = max(per_day.values()) or 1
    bars = [{"day": day, "count": count, "height": round(100 * count / peak), "today": day == today} for day, count in per_day.items()]
    total = sum(status_rows.values()) or 1
    order = ("reserved", "checked_in", "checked_out", "cancelled")
    colours = {"reserved": "#2F7DD1", "checked_in": "#1B7F4B", "checked_out": "#8794A0", "cancelled": "#B3261E"}
    slices, offset = [], 0.0
    for status in order:
        count = status_rows.get(status, 0)
        share = 100 * count / total
        slices.append({"status": status, "count": count, "share": round(share), "colour": colours[status], "offset": offset, "length": share})
        offset += share
    return {"today": per_day.get(today, 0), "arriving": arriving, "parked": status_rows.get("checked_in", 0),
            "disputes": open_disputes, "bars": bars, "peak": peak if any(per_day.values()) else 0, "slices": slices, "total_bookings": sum(status_rows.values())}
