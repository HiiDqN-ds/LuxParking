import pytest
from flask import g
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Lot, Role, Space, Tenant, User
from app.tenancy import use_tenant
from conftest import login


# --- data / seed ----------------------------------------------------------------

def test_seed_creates_50_unique_spaces(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        names = [s.name for s in Space.query.all()]
    assert len(names) == 50 and len(set(names)) == 50
    assert names[0] == "A1" and names[-1] == "E10"


def test_seed_is_idempotent(app):
    from app.seed import seed_demo
    seed_demo(owner_email="owner@test.dev", owner_password="x")
    assert Tenant.query.count() == 1


def test_non_platform_user_requires_tenant(app):
    db.session.add(User(email="x@y.z", password_hash="h", role=Role.OWNER))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# --- tenant isolation -------------------------------------------------------------

def test_reads_are_scoped_to_current_tenant(app, other_tenant):
    t, lot, space = other_tenant
    first = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(first.id):
        assert db.session.scalar(select(func.count()).select_from(Space)) == 50
        assert db.session.get(Space, space.id) is None  # other tenant's row is invisible
    with use_tenant(t.id):
        assert db.session.scalar(select(func.count()).select_from(Space)) == 1
    with use_tenant(None):
        assert Space.query.count() == 51


def test_write_without_tenant_context_is_refused(app):
    with use_tenant(None):
        db.session.add(Lot(name="orphan", slug="orphan"))
        with pytest.raises(Exception):
            db.session.commit()
    db.session.rollback()


# --- auth & roles -----------------------------------------------------------------

def test_admin_requires_login(client):
    r = client.get("/admin/")
    assert r.status_code == 302 and "/admin/login" in r.headers["Location"]


def test_login_success_and_failure(client):
    assert login(client, password="wrong").status_code == 200
    r = login(client)
    assert r.status_code == 302
    assert b"Dashboard" in client.get("/admin/").data


def test_login_rejects_open_redirect(client):
    r = client.post("/admin/login?next=https://evil.example", data={"email": "owner@test.dev", "password": "correct-horse"})
    assert r.headers["Location"].endswith("/admin/")


def test_owner_can_edit_space(client, app):
    login(client)
    space = Space.query.filter_by(name="A1").one() if False else None
    with use_tenant(Tenant.query.first().id):
        space = Space.query.filter_by(name="A1").one()
        sid = space.id
    data = {"name": "A1", "kind": "suv", "status": "optional", "covered": "y", "x": 40, "y": 40, "w": 80, "h": 110,
            "bus_distance_m": "120", "ev_charging": "y", "photo_url": "", "notes": ""}
    assert client.post(f"/admin/spaces/{sid}/edit", data=data).status_code == 302
    db.session.expire_all()
    with use_tenant(Tenant.query.first().id):
        s = db.session.get(Space, sid)
        assert (s.kind, s.status, s.covered, s.ev_charging, s.bus_distance_m) == ("suv", "optional", True, True, 120)


def test_duplicate_name_and_out_of_bounds_rejected(client, app):
    login(client)
    lot_id = Lot.query.first().id if False else None
    with use_tenant(Tenant.query.first().id):
        lot_id = Lot.query.first().id
    base = {"kind": "small", "status": "active", "x": 40, "y": 40, "w": 80, "h": 110}
    assert b"already has this name" in client.post(f"/admin/lots/{lot_id}/spaces/new", data={**base, "name": "A1"}).data
    assert b"fit inside" in client.post(f"/admin/lots/{lot_id}/spaces/new", data={**base, "name": "N1", "x": 990}).data


def test_owner_can_add_and_delete_space(client, app):
    login(client)
    with use_tenant(Tenant.query.first().id):
        lot_id = Lot.query.first().id
    r = client.post(f"/admin/lots/{lot_id}/spaces/new", data={"name": "F1", "kind": "van", "status": "active", "x": 40, "y": 600, "w": 80, "h": 90})
    assert r.status_code == 302
    with use_tenant(Tenant.query.first().id):
        sid = Space.query.filter_by(name="F1").one().id
    assert client.post(f"/admin/spaces/{sid}/delete").status_code == 302
    with use_tenant(Tenant.query.first().id):
        assert Space.query.count() == 50


def test_staff_is_read_only(client, staff):
    login(client, "staff@test.dev", "staff-pass")
    with use_tenant(staff.tenant_id):
        sid, lot_id = Space.query.first().id, Lot.query.first().id
    assert client.get(f"/admin/lots/{lot_id}/layout").status_code == 200
    assert client.get(f"/admin/spaces/{sid}/edit").status_code == 403
    assert client.post(f"/admin/spaces/{sid}/delete").status_code == 403
    assert client.post(f"/admin/lots/{lot_id}/autolayout").status_code == 403


def test_cross_tenant_urls_return_404(client, other_tenant):
    login(client)
    _, lot, space = other_tenant
    assert client.get(f"/admin/spaces/{space.id}/edit").status_code == 404
    assert client.get(f"/admin/lots/{lot.id}/layout").status_code == 404
    assert client.post(f"/admin/spaces/{space.id}/delete").status_code == 404


def test_layout_renders_all_50_spaces_and_autolayout(client, app):
    login(client)
    with use_tenant(Tenant.query.first().id):
        lot_id = Lot.query.first().id
    html = client.get(f"/admin/lots/{lot_id}/layout").data.decode()
    assert html.count('class="space s-') == 50
    assert client.post(f"/admin/lots/{lot_id}/autolayout").status_code == 302


def test_healthz(client):
    assert client.get("/healthz").json == {"status": "ok"}
