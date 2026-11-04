"""Partner keys: chosen maps and features, automatic price, enforcement in the API, webhook by map."""
from decimal import Decimal

from app.extensions import db
from app.models import ApiKey, Lot, Role, Space, Tenant, User, WebhookDelivery, WebhookEndpoint
from app.services.api_platform import (
    calculate_price, enqueue_webhook_event, expand_features, issue_partner_access,
)
from app.tenancy import use_tenant
from tests.test_phase8 import _make_booking


def _platform_login(app, client):
    with app.app_context():
        user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
        user.set_password("platform-password")
        db.session.add(user)
        db.session.commit()
    client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})


def _two_lots(app):
    """Returns (tenant_id, lot1_id, lot2_id); lot2 gets one space."""
    tenant = Tenant.query.first()
    with use_tenant(tenant.id):
        lot1 = Lot.query.filter_by(tenant_id=tenant.id).first()
        lot2 = Lot(tenant_id=tenant.id, name="Second map", slug="second-map")
        db.session.add(lot2)
        db.session.flush()
        db.session.add(Space(tenant_id=tenant.id, lot_id=lot2.id, name="Z1"))
        db.session.commit()
        return tenant.id, lot1.id, lot2.id


def test_price_is_base_plus_features_plus_maps(app):
    codes, _ = expand_features(["map"])
    assert calculate_price(codes, 2)["total"] == Decimal("79.00")  # 19 + 30 + 2 x 15
    codes, added = expand_features(["bookings_write"])
    assert set(codes) == {"map", "quotes", "holds", "bookings_write"} and set(added) == {"map", "quotes", "holds"}
    assert calculate_price(codes, 1)["total"] == Decimal("149.00")  # 19 + 30 + 10 + 15 + 60 + 15


def test_price_changes_with_env(app, monkeypatch):
    monkeypatch.setenv("API_PRICE_LOT", "20")
    codes, _ = expand_features(["map"])
    assert calculate_price(codes, 2)["total"] == Decimal("89.00")


def test_form_creates_key_with_price_messages_and_webhook(client, app, monkeypatch):
    from app.services import api_platform
    monkeypatch.setattr(api_platform, "validate_public_webhook_host", lambda _h: None)
    _platform_login(app, client)
    with app.app_context():
        tid, lot1, lot2 = _two_lots(app)
    page = client.post("/admin/platform/api/keys", data={
        "tenant_id": tid, "name": "Partner X", "features": ["bookings_read", "inspections_read"],
        "lot_ids": [lot1], "webhook_url": "https://partner.example/events"}).data.decode()
    assert "Message for the partner (English)" in page and "Nachricht an den Partner (Deutsch)" in page
    assert "Webhook signing secret" in page and "booking.created" in page
    with app.app_context():
        key = ApiKey.query.one()
        assert key.key_type == "sk" and key.allowed_lot_ids == [lot1]
        assert key.monthly_price == Decimal("79.00")  # 19 + 25 + 20 + 15
        assert WebhookEndpoint.query.one().lot_ids == [lot1]


def test_website_key_cannot_have_private_features(client, app):
    _platform_login(app, client)
    with app.app_context():
        tid, lot1, _ = _two_lots(app)
    client.post("/admin/platform/api/keys", data={
        "tenant_id": tid, "features": ["bookings_read"], "lot_ids": [lot1], "allowed_origins": "https://p.com"})
    with app.app_context():
        assert ApiKey.query.count() == 0


def test_key_only_sees_its_maps(client, app):
    with app.app_context():
        tid, lot1, lot2 = _two_lots(app)
        _, token, _ = issue_partner_access(tenant_id=tid, name="P", features=["map"], lot_ids=[lot1], allowed_origins=[])
    headers = {"Authorization": f"Bearer {token}"}
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc) + timedelta(days=5)
    window = {"from": start.isoformat(), "to": (start + timedelta(days=2)).isoformat()}
    assert client.get(f"/v1/lots/{lot2}/map", query_string=window, headers=headers).status_code == 404
    assert client.get(f"/v1/lots/{lot1}/map", query_string=window, headers=headers).status_code == 200
    products = client.get("/v1/products", headers=headers).get_json()["products"]
    assert products and {p["lot_id"] for p in products} == {lot1}


def test_key_only_sees_bookings_of_its_maps(client, app):
    with app.app_context():
        tid, lot1, lot2 = _two_lots(app)
        space1 = Space.query.filter_by(lot_id=lot1).first().id
        space2 = Space.query.filter_by(lot_id=lot2).first().id
        with use_tenant(tid):
            b1 = _make_booking(tid, lot1, space1, "a@example.com", "PP-A").id
            b2 = _make_booking(tid, lot2, space2, "b@example.com", "PP-B").id
        _, token, _ = issue_partner_access(tenant_id=tid, name="P", features=["bookings_read"], lot_ids=[lot1], allowed_origins=[])
    headers = {"Authorization": f"Bearer {token}"}
    listed = client.get("/v1/bookings", headers=headers).get_json()["bookings"]
    assert [b["id"] for b in listed] == [b1]
    assert client.get(f"/v1/bookings/{b1}", headers=headers).status_code == 200
    assert client.get(f"/v1/bookings/{b2}", headers=headers).status_code == 404


def test_webhook_only_for_chosen_maps(app, monkeypatch):
    tid, lot1, lot2 = _two_lots(app)
    space1 = Space.query.filter_by(lot_id=lot1).first().id
    space2 = Space.query.filter_by(lot_id=lot2).first().id
    from app.services.api_platform import create_webhook_endpoint
    from app.services import api_platform
    monkeypatch.setattr(api_platform, "validate_public_webhook_host", lambda _h: None)
    with use_tenant(tid):
        create_webhook_endpoint(tenant_id=tid, name="w1", url="https://one.example/h", events=["booking.created"], lot_ids=[lot1])
        _make_booking(tid, lot1, space1, "a@example.com", "PP-1")
        _make_booking(tid, lot2, space2, "b@example.com", "PP-2")
        assert WebhookDelivery.query.count() == 1
