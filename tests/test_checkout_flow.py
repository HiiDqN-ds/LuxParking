"""Hold -> checkout -> booking page -> cancel, through the public routes."""
from datetime import datetime, timedelta, timezone

from app.models import Lot, PriceRule, Space, Tenant
from app.extensions import db


def _hold(app, pickup_in_hours=72):
    from app.services.bookings import create_hold
    tenant = db.session.query(Tenant).first()
    lot = db.session.query(Lot).filter_by(tenant_id=tenant.id).first()
    space = db.session.query(Space).filter_by(lot_id=lot.id, tenant_id=tenant.id).first()
    db.session.add(PriceRule(tenant_id=tenant.id, lot_id=lot.id, name="Base", period="day", mode="per_day", amount=9.9))
    db.session.commit()
    start = (datetime.now(timezone.utc) + timedelta(hours=pickup_in_hours)).replace(second=0, microsecond=0)
    hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=space.id, customer_email="a@example.com",
                       pickup_at=start, return_at=start + timedelta(days=2))
    return tenant, lot, hold


def test_checkout_creates_booking_and_allows_cancel(client, app):
    with app.app_context():
        tenant, lot, hold = _hold(app)
        base = f"/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    page = client.get(base)
    assert page.status_code == 200 and b"checkout-form" in page.data
    resp = client.post(base + "/book", data={"first_name": "Ana", "email": "a@example.com", "phone": "+49123456",
                                              "vehicle_plate": "D-AB 123", "payment_method": "arrival", "accept_terms": "1"})
    assert resp.status_code == 302 and "/booking/" in resp.headers["Location"]
    view = client.get(resp.headers["Location"])
    assert view.status_code == 200 and b"<svg" in view.data
    cancel = client.post(resp.headers["Location"].rstrip("/") + "/cancel")
    assert cancel.status_code == 302


def test_booking_page_rejects_bad_token(client):
    assert client.get("/booking/not-a-token").status_code == 404


def test_legal_pages_and_terms_required(client, app):
    for path, word in (("/impressum", b"Impressum"), ("/datenschutz", b"Datenschutz"), ("/agb", b"AGB")):
        r = client.get(path)
        assert r.status_code == 200 and word in r.data
    with app.app_context():
        tenant, lot, hold = _hold(app)
        base = f"/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    r = client.post(base + "/book", data={"first_name": "Ana", "email": "a@example.com", "phone": "1", "vehicle_plate": "X"})
    assert "/booking/" not in r.headers["Location"]
