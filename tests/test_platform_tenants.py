"""Platform owner manages tenants: create, read, update, delete."""
from app.extensions import db
from app.models import Lot, Role, Tenant, User
from tests.conftest import login


def _platform_login(app, client):
    with app.app_context():
        user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
        user.set_password("platform-password")
        db.session.add(user)
        db.session.commit()
    client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})


def _create(client, **over):
    data = {"name": "Neo Parking", "slug": "", "status": "trial", "default_locale": "de",
            "timezone": "Europe/Berlin", "currency": "EUR", "custom_domain": "",
            "owner_name": "Neo", "owner_email": "neo@example.com", "owner_password": "long-enough-pass"}
    data.update(over)
    return client.post("/admin/platform/tenants/new", data=data)


def test_tenant_pages_are_platform_owner_only(client, app):
    login(client)
    assert client.get("/admin/platform/tenants/new").status_code == 403
    assert client.get("/admin/platform/tenants/1").status_code == 403


def test_platform_owner_full_tenant_crud(client, app):
    _platform_login(app, client)
    assert client.get("/admin/platform/tenants/new").status_code == 200
    created = _create(client)
    assert created.status_code == 302
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="neo-parking").one()
        tid = tenant.id
        owner = User.query.filter_by(email="neo@example.com").one()
        assert owner.tenant_id == tid and owner.role == Role.OWNER
    assert b"Neo Parking" in client.get(f"/admin/platform/tenants/{tid}").data
    updated = client.post(f"/admin/platform/tenants/{tid}/edit", data={
        "name": "Neo Parking GmbH", "slug": "neo-parking", "status": "active", "default_locale": "en",
        "timezone": "Europe/Berlin", "currency": "EUR", "custom_domain": "book.neo.de"})
    assert updated.status_code == 302
    with app.app_context():
        tenant = db.session.get(Tenant, tid)
        assert (tenant.name, tenant.status, tenant.default_locale) == ("Neo Parking GmbH", "active", "en")
    wrong = client.post(f"/admin/platform/tenants/{tid}/delete", data={"confirm_slug": "nope"})
    assert wrong.status_code == 302
    with app.app_context():
        assert db.session.get(Tenant, tid) is not None
    done = client.post(f"/admin/platform/tenants/{tid}/delete", data={"confirm_slug": "neo-parking"})
    assert done.status_code == 302
    with app.app_context():
        assert db.session.get(Tenant, tid) is None
        assert User.query.filter_by(email="neo@example.com").first() is None


def test_tenant_validation_and_unique_slug(client, app):
    _platform_login(app, client)
    assert _create(client).status_code == 302
    assert _create(client, owner_email="other@example.com").status_code == 400
    assert _create(client, name="Other", owner_email="short@example.com", owner_password="short").status_code == 400
    assert _create(client, name="Bad Zone", timezone="Mars/Base", owner_email="z@example.com").status_code == 400


def test_tenant_with_bookings_cannot_be_deleted(client, app):
    from app.models import Booking, Space
    from app.tenancy import use_tenant
    from tests.test_phase8 import _make_booking
    _platform_login(app, client)
    with app.app_context(), use_tenant(None):
        tenant = Tenant.query.first()
        lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        space = Space.query.filter_by(tenant_id=tenant.id).first()
        tid, slug = tenant.id, tenant.slug
        with use_tenant(tid):
            _make_booking(tid, lot.id, space.id, "guest@example.com", "PP-DEL-1")
    resp = client.post(f"/admin/platform/tenants/{tid}/delete", data={"confirm_slug": slug})
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(Tenant, tid) is not None
        assert db.session.query(Booking).filter_by(tenant_id=tid).count() == 1
