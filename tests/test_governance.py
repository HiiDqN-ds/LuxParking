"""Roles: platform admin issues API keys/webhooks; owners only request and view; staff are workers."""
from app.extensions import db
from app.models import ApiAccessRequest, ApiKey, Role, Tenant, User, WebhookEndpoint
from tests.conftest import login


def _admin(app, client):
    with app.app_context():
        user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
        user.set_password("platform-password")
        db.session.add(user)
        db.session.commit()
    return client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})


def test_owner_has_no_api_pages_or_nav_links(client, app):
    login(client)
    assert client.get("/admin/platform/api").status_code == 403
    assert client.get("/admin/platform/usage").status_code == 403
    assert client.get("/admin/api-access").status_code == 404
    assert client.get("/admin/usage").status_code == 404
    page = client.get("/admin/").data
    assert b"API access" not in page and b"API usage" not in page
    with app.app_context():
        assert ApiKey.query.count() == 0


def test_admin_approves_request_and_issues_key_once(client, app, monkeypatch):
    from app.services import api_platform
    monkeypatch.setattr(api_platform, "validate_public_webhook_host", lambda _h: None)
    with app.app_context():  # the owner's request, created directly (tests share one app context per test)
        owner = User.query.filter_by(email="owner@test.dev").one()
        db.session.add(ApiAccessRequest(tenant_id=owner.tenant_id, bundle="integration", requested_by_id=owner.id))
        db.session.commit()
    other = client
    _admin(app, other)
    page = other.get("/admin/platform/api")
    assert page.status_code == 200 and b"integration" in page.data.lower()
    with app.app_context():
        req_id = ApiAccessRequest.query.one().id
    done = other.post(f"/admin/platform/api/requests/{req_id}/approve")
    assert done.status_code == 200 and b"Copy this for" in done.data
    with app.app_context():
        key = ApiKey.query.one()
        assert key.active and "keys:manage" not in key.scopes and key.key_type == "sk"
        key_id = key.id
    assert b"Copy this for" not in other.get("/admin/platform/api").data
    tenant_id = None
    with app.app_context():
        tenant_id = Tenant.query.first().id
    hook = other.post("/admin/platform/api/webhooks", data={"tenant_id": tenant_id, "name": "Receiver",
                      "url": "https://hooks.example.test/p", "events": ["booking.created"]})
    assert hook.status_code == 200 and b"Copy this for" in hook.data
    with app.app_context():
        endpoint_id = WebhookEndpoint.query.one().id
    assert other.post(f"/admin/platform/api/webhooks/{endpoint_id}/rotate").status_code == 200
    other.post(f"/admin/platform/api/webhooks/{endpoint_id}/revoke")
    other.post(f"/admin/platform/api/keys/{key_id}/revoke")
    with app.app_context():
        assert db.session.get(ApiKey, key_id).active is False and db.session.get(WebhookEndpoint, endpoint_id).active is False


def test_platform_admin_can_edit_and_delete_keys_and_webhooks(client, app, monkeypatch):
    from app.models import UsageLog
    from app.services import api_platform
    monkeypatch.setattr(api_platform, "validate_public_webhook_host", lambda _h: None)
    _admin(app, client)
    with app.app_context():
        tenant_id = Tenant.query.first().id
    assert b"API usage" in client.get("/admin/").data
    assert client.get("/admin/platform/usage").status_code == 200
    client.post("/admin/platform/api/keys", data={"tenant_id": tenant_id, "bundle": "reports", "name": "Acct"})
    with app.app_context():
        key = ApiKey.query.one()
        key_id = key.id
        db.session.add(UsageLog(tenant_id=tenant_id, api_key_id=key_id, method="GET", path="/v1/x", status_code=200, duration_ms=3))
        db.session.commit()
    assert client.get(f"/admin/platform/api/keys/{key_id}/edit").status_code == 200
    r = client.post(f"/admin/platform/api/keys/{key_id}/edit", data={
        "name": "Renamed", "requests_per_minute": "75", "allowed_origins": "", "scopes": ["bookings:read"], "active": "1"})
    assert r.status_code == 302
    with app.app_context():
        key = db.session.get(ApiKey, key_id)
        assert key.name == "Renamed" and key.requests_per_minute == 75 and key.scopes == ["bookings:read"]
    bad = client.post(f"/admin/platform/api/keys/{key_id}/edit", data={
        "name": "X", "requests_per_minute": "75", "scopes": ["bogus:scope"], "active": "1"})
    assert bad.status_code == 200
    hook = client.post("/admin/platform/api/webhooks", data={"tenant_id": tenant_id, "name": "Hook",
                       "url": "https://hooks.example.test/a", "events": ["booking.created"]})
    assert hook.status_code == 200
    with app.app_context():
        endpoint_id = WebhookEndpoint.query.one().id
    r = client.post(f"/admin/platform/api/webhooks/{endpoint_id}/edit", data={
        "name": "Hook2", "url": "https://hooks.example.test/b", "events": ["booking.created"], "active": "1"})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(WebhookEndpoint, endpoint_id).url.endswith("/b")
    assert client.post(f"/admin/platform/api/webhooks/{endpoint_id}/delete").status_code == 302
    assert client.post(f"/admin/platform/api/keys/{key_id}/delete").status_code == 302
    with app.app_context():
        assert ApiKey.query.count() == 0 and WebhookEndpoint.query.count() == 0 and UsageLog.query.count() == 0


def test_owner_can_edit_and_delete_staff_and_lots(client, app):
    from app.models import Booking, Lot
    login(client)
    r = client.post("/admin/team", data={"name": "Sam", "email": "sam@test.dev", "password": "long-enough-pass1", "role": "staff"})
    assert r.status_code == 302
    with app.app_context():
        sam = User.query.filter_by(email="sam@test.dev").one().id
    assert client.get(f"/admin/team/{sam}/edit").status_code == 200
    r = client.post(f"/admin/team/{sam}/edit", data={"name": "Samuel", "email": "samuel@test.dev", "password": ""})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(User, sam).email == "samuel@test.dev"
    assert client.post(f"/admin/team/{sam}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(User, sam) is None
        owner = User.query.filter_by(email="owner@test.dev").one()
        assert client.post(f"/admin/team/{owner.id}/delete").status_code == 403
        lot_with_bookings = {b.lot_id for b in Booking.query.all()}
        empty = Lot(tenant_id=owner.tenant_id, name="Scratch", slug="scratch")
        db.session.add(empty)
        db.session.commit()
        empty_id = empty.id
    assert client.post(f"/admin/lots/{empty_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(Lot, empty_id) is None


def test_staff_app_links_owner_back_to_admin_dashboard(client):
    login(client)
    page = client.get("/staff/")
    assert page.status_code == 200 and b"Admin dashboard" in page.data
