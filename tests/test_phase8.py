from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.extensions import db
from app.models import ApiKey, AuditLog, Booking, Lot, Role, Space, Tenant, User, WebhookEndpoint
from app.services.bookings import create_booking_from_hold, create_hold
from app.services.api_platform import issue_api_key
from app.tenancy import use_tenant


def _make_booking(tenant_id, lot_id, space_id, email, booking_ref):
    start = datetime.now(timezone.utc) + timedelta(days=10)
    hold = create_hold(
        tenant_id=tenant_id,
        lot_id=lot_id,
        space_id=space_id,
        pickup_at=start,
        return_at=start + timedelta(days=1),
        customer_email=email,
    )
    booking = create_booking_from_hold(
        tenant_id=tenant_id,
        hold_id=hold.id,
        first_name="Dashboard",
        last_name="Guest",
        email=email,
        phone="+49123456789",
        total_amount="42.00",
        currency="EUR",
    )
    booking.booking_ref = booking_ref
    booking.payment_status = "paid"
    db.session.commit()
    return booking


def _login_platform_owner(app, client):
    with app.app_context():
        user = User(
            tenant_id=None,
            email="platform@test.dev",
            name="Platform Admin",
            role=Role.PLATFORM_OWNER,
        )
        user.set_password("platform-password")
        db.session.add(user)
        db.session.commit()
    return client.post(
        "/admin/login",
        data={"email": "platform@test.dev", "password": "platform-password"},
    )


def test_tenant_bookings_search_csv_and_dashboard_are_tenant_scoped(
    client, app, other_tenant
):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        with use_tenant(tenant.id):
            lot = Lot.query.first()
            space = Space.query.filter_by(lot_id=lot.id).first()
            booking = _make_booking(
                tenant.id, lot.id, space.id, "ada@example.test", "PP-OWN-001"
            )
            booking_id = booking.id
        other_tenant_id, other_lot_id, other_space_id = (
            other_tenant[0].id,
            other_tenant[1].id,
            other_tenant[2].id,
        )
        _make_booking(
            other_tenant_id,
            other_lot_id,
            other_space_id,
            "foreign@example.test",
            "PP-OTHER-001",
        )

    response = client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    assert response.status_code == 302
    dashboard = client.get("/admin/")
    assert dashboard.status_code == 200
    assert b"42.00" in dashboard.data

    results = client.get("/admin/bookings?q=ada%40example.test")
    assert results.status_code == 200
    assert b"PP-OWN-001" in results.data
    assert b"PP-OTHER-001" not in results.data

    exported = client.get("/admin/bookings.csv")
    assert exported.status_code == 200
    assert exported.mimetype == "text/csv"
    assert b"PP-OWN-001" in exported.data
    assert b"foreign@example.test" not in exported.data
    assert client.get("/admin/calendar").status_code == 200
    with app.app_context():
        assert db.session.get(Booking, booking_id) is not None


def test_owner_can_create_staff_but_cannot_assign_owner_role(client, app):
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    page = client.get("/admin/team")
    assert page.status_code == 200
    created = client.post(
        "/admin/team",
        data={
            "name": "New attendant",
            "email": "new-staff@test.dev",
            "password": "long-enough-staff-password",
            "role": Role.STAFF,
        },
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert b"new-staff@test.dev" in created.data
    with app.app_context():
        staff = User.query.filter_by(email="new-staff@test.dev").one()
        assert staff.role == Role.STAFF
        staff_id = staff.id

    rejected = client.post(
        "/admin/team",
        data={
            "name": "Escalation",
            "email": "escalation@test.dev",
            "password": "long-enough-staff-password",
            "role": Role.OWNER,
        },
    )
    assert rejected.status_code == 200
    with app.app_context():
        assert User.query.filter_by(email="escalation@test.dev").count() == 0
        with use_tenant(Tenant.query.filter_by(slug="duesseldorf-airport-parking").one().id):
            member = db.session.get(User, staff_id)
            member_id = member.id
    disabled = client.post(f"/admin/team/{member_id}/active", follow_redirects=True)
    assert disabled.status_code == 200
    with app.app_context():
        assert db.session.get(User, member_id).active is False


def test_platform_owner_can_manage_tenant_status_without_tenant_data_access(
    app, client, other_tenant
):
    tenant_id = other_tenant[0].id
    with app.app_context():
        tenant_owner = User(
            tenant_id=tenant_id,
            email="other-owner@test.dev",
            name="Other Owner",
            role=Role.OWNER,
        )
        tenant_owner.set_password("other-owner-password")
        db.session.add(tenant_owner)
        db.session.commit()
        api_key, api_token = issue_api_key(
            tenant_id=tenant_id,
            name="suspended tenant test",
            key_type="pk",
            scopes=["products:read"],
            allowed_origins=[],
            requests_per_minute=60,
        )
        api_key_id = api_key.id
    platform_login = _login_platform_owner(app, client)
    assert platform_login.status_code == 302
    with client.session_transaction() as session:
        platform_user_id = int(session["_user_id"].split(":")[0])  # "<id>:<password fingerprint>"
    with app.app_context():
        assert db.session.get(User, platform_user_id).role == Role.PLATFORM_OWNER
    dashboard = client.get("/admin/")
    assert dashboard.status_code == 200
    assert b"Platform overview" in dashboard.data
    assert b"Duesseldorf Airport Parking" in dashboard.data

    suspended = client.post(
        f"/admin/platform/tenants/{tenant_id}/status",
        data={"status": "suspended"},
        follow_redirects=True,
    )
    assert suspended.status_code == 200
    assert b"suspended" in suspended.data
    assert client.get("/admin/lots").status_code == 403
    api_response = client.get(
        "/v1/products",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert api_response.status_code == 403
    assert api_response.json["error"]["code"] == "tenant_suspended"

    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        assert tenant.status == "suspended"
        assert db.session.get(ApiKey, api_key_id).active is True
        assert db.session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id.is_(None),
                AuditLog.action == "platform.tenant.status",
            )
        ).scalars().first()

    invalid = client.post(
        f"/admin/platform/tenants/{tenant_id}/status",
        data={"status": "deleted"},
    )
    assert invalid.status_code == 400
    assert client.post("/admin/logout").status_code == 302
    refused_login = client.post(
        "/admin/login",
        data={"email": "other-owner@test.dev", "password": "other-owner-password"},
    )
    assert refused_login.status_code == 200
    assert b"account is suspended" in refused_login.data


def test_suspended_tenant_existing_sessions_are_blocked(client, app, other_tenant):
    tenant_id = other_tenant[0].id
    with app.app_context():
        tenant_owner = User(
            tenant_id=tenant_id,
            email="suspended-owner@test.dev",
            name="Suspended Owner",
            role=Role.OWNER,
        )
        tenant_owner.set_password("suspended-owner-password")
        db.session.add(tenant_owner)
        db.session.commit()
    assert client.post(
        "/admin/login",
        data={"email": "suspended-owner@test.dev", "password": "suspended-owner-password"},
    ).status_code == 302
    with app.app_context():
        db.session.get(Tenant, tenant_id).status = "suspended"
        db.session.commit()
    assert client.get("/admin/").status_code == 403
    assert client.post("/admin/logout").status_code == 302


def test_booking_csv_escapes_spreadsheet_formulas(client, app):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        with use_tenant(tenant.id):
            lot = Lot.query.first()
            space = Space.query.filter_by(lot_id=lot.id).first()
            _make_booking(
                tenant.id,
                lot.id,
                space.id,
                "=IMPORTDATA(\"https://example.invalid\")@example.test",
                "PP-SAFE-001",
            )
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    export = client.get("/admin/bookings.csv")
    assert b"'=importdata" in export.data, repr(export.data)


def test_cli_can_bootstrap_platform_owner_without_exposing_password(app):
    result = app.test_cli_runner().invoke(
        args=["create-platform-owner", "--email", "Root@Example.test"],
        input="long-enough-platform-password\nlong-enough-platform-password\n",
    )
    assert result.exit_code == 0
    assert "Created platform owner account for root@example.test." in result.output
    assert "long-enough-platform-password" not in result.output
    with app.app_context():
        user = User.query.filter_by(email="root@example.test").one()
        assert user.tenant_id is None
        assert user.role == Role.PLATFORM_OWNER
        assert user.check_password("long-enough-platform-password")

def test_cli_can_reset_existing_user_password_without_exposing_it(app):
    result = app.test_cli_runner().invoke(
        args=["reset-user-password", "--email", "OWNER@Test.dev"],
        input="new-secure-password\nnew-secure-password\n",
    )
    assert result.exit_code == 0
    assert "Password reset for owner@test.dev." in result.output
    assert "new-secure-password" not in result.output
    with app.app_context():
        user = User.query.filter_by(email="owner@test.dev").one()
        assert user.check_password("new-secure-password")


