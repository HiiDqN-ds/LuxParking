"""The partner API can list a lot's extras and take them on quotes and bookings (shuttle: 10 EUR per person, max 2)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db
from app.models import Extra, Lot, Space, Tenant
from app.services.api_platform import issue_partner_access


def _setup(app):
    tenant = Tenant.query.first()
    lot = Lot.query.filter_by(tenant_id=tenant.id).first()
    lot.base_daily_rate = Decimal("9.90")
    shuttle = Extra(tenant_id=tenant.id, lot_id=lot.id, kind="shuttle", name="Flughafen-Shuttle", price=Decimal("10"),
                    per_person=True, included_persons=0, max_persons=2)
    db.session.add(shuttle)
    db.session.commit()
    _, token, _ = issue_partner_access(tenant_id=tenant.id, name="site", features=["map", "quotes", "holds", "bookings_write"],
                                       lot_ids=[lot.id], allowed_origins=[])
    return lot, Space.query.filter_by(lot_id=lot.id).first(), shuttle, {"Authorization": f"Bearer {token}"}


def test_extras_on_quote_and_booking(client, app):
    from app.seed import seed_demo
    seed_demo(owner_email="o@t.dev", owner_password="correct-horse")
    lot, space, shuttle, H = _setup(app)
    listed = client.get(f"/v1/extras?lot_id={lot.id}", headers=H).get_json()["extras"]
    assert listed[0]["max_persons"] == 2 and listed[0]["price"] == "10.00" and listed[0]["included_persons"] == 0
    start = datetime.now(timezone.utc) + timedelta(days=3)
    body = {"lot_id": lot.id, "space_id": space.id, "from": start.isoformat(), "to": (start + timedelta(days=2)).isoformat()}
    quote = client.post("/v1/quotes", json={**body, "extras": [{"id": shuttle.id, "quantity": 2}]}, headers=H).get_json()["quote"]
    assert quote["subtotal"] == "50.00" and quote["extras_total"] == "20.00" and quote["total"] == "70.00"
    too_many = client.post("/v1/quotes", json={**body, "extras": [{"id": shuttle.id, "quantity": 3}]}, headers=H)
    assert too_many.status_code == 400 and "at most 2" in too_many.get_json()["error"]["message"]
    hold = client.post("/v1/holds", json={**body, "email": "a@example.com"}, headers={**H, "Idempotency-Key": "hold-key-0001"}).get_json()["hold"]
    booked = client.post("/v1/bookings", json={"hold_id": hold["id"], "first_name": "Ana", "email": "a@example.com", "phone": "+49123456",
                                               "extras": [{"id": shuttle.id, "quantity": 2, "details": "LH123"}]},
                         headers={**H, "Idempotency-Key": "book-key-0001"})
    assert booked.status_code == 201
    booking = booked.get_json()["booking"]
    assert booking["total_amount"] == "70.00" and booking["extras"][0]["quantity"] == 2
