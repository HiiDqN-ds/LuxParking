"""Shared helpers for the invoice / notification / billing tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db
from app.models import Lot, Space, Tenant
from app.services.bookings import create_booking_from_hold, create_hold

SEED_SLUG = "duesseldorf-airport-parking"


def seed_tenant() -> Tenant:
    return Tenant.query.filter_by(slug=SEED_SLUG).one()


def when(hours: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)


def make_booking(tenant=None, *, hours=72, days=1, total="119.00", email="ana@example.com", locale="de",
                 phone="+4915112345678", space_index=0, first_name="Ana"):
    """A reserved booking on one of the seeded spaces. Each call needs its own space_index when dates overlap."""
    tenant = tenant or seed_tenant()
    lot = Lot.query.filter_by(tenant_id=tenant.id).first()
    space = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()[space_index]
    start = when(hours)
    hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=space.id, customer_email=email,
                       pickup_at=start, return_at=start + timedelta(days=days))
    return create_booking_from_hold(tenant_id=tenant.id, hold_id=hold.id, first_name=first_name, last_name="Muster", email=email,
                                    phone=phone, total_amount=Decimal(total), currency="EUR", vehicle_plate="D-AB 1", locale=locale)


def capture_email(monkeypatch, app):
    """Configure SMTP and collect every message instead of sending it."""
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="from@test.dev", SITE_URL="https://park.test")
    sent = []
    monkeypatch.setattr("app.services.booking_events.smtp_send", sent.append)
    monkeypatch.setattr("app.services.booking_notifications.smtp_send", sent.append)
    return sent


def pdf_attachments(message):
    return [p for p in message.iter_attachments() if p.get_content_type() == "application/pdf"]
