from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Alert,
    ExternalBookingMapping,
    ExternalIntegrationConnection,
    Lot,
    Space,
    Tenant,
    Vehicle,
)
from app.services.bookings import create_booking_from_hold, create_hold
from app.services.integrations import IntegrationNotReady, adapter_for


def _create_booking(tenant_id, lot_id, space_id, reference, email, starts_in_hours=6):
    start = datetime.now(timezone.utc) + timedelta(hours=starts_in_hours)
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
        first_name="Ada",
        last_name="Lovelace",
        email=email,
        phone="+49 123 456 789",
        total_amount="42.00",
        currency="EUR",
    )
    booking.booking_ref = reference
    vehicle = Vehicle(
        tenant_id=tenant_id,
        customer_id=booking.customer_id,
        plate="D-AB 1234",
    )
    db.session.add(vehicle)
    db.session.flush()
    booking.vehicle_id = vehicle.id
    db.session.commit()
    return booking


def test_owner_search_matches_booking_customer_phone_email_plate_and_id(client, app, other_tenant):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        booking = _create_booking(
            tenant.id, lot.id, space.id, "PP-SEARCH-001", "ada@example.test"
        )
        booking_id = booking.id
        foreign_tenant, foreign_lot, foreign_space = other_tenant
        _create_booking(
            foreign_tenant.id,
            foreign_lot.id,
            foreign_space.id,
            "PP-FOREIGN-001",
            "ada@example.test",
        )

    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    for query in ("PP-SEARCH", "Ada Lovelace", "+49123456789", "ada@example", "AB 1234", str(booking_id)):
        response = client.get("/admin/search", query_string={"q": query})
        assert response.status_code == 200
        references = [result["reference"] for result in response.json["results"]]
        assert "PP-SEARCH-001" in references, (query, response.json)
        assert "PP-FOREIGN-001" not in references
    assert client.get("/admin/search?q=x").json == {"results": []}


def test_owner_location_switch_is_authorized_and_search_stays_tenant_wide(client, app, other_tenant):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        first_lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        first_space = Space.query.filter_by(lot_id=first_lot.id).first()
        second_lot = Lot(
            tenant_id=tenant.id,
            name="Second authorized location",
            slug="second-authorized-location",
        )
        db.session.add(second_lot)
        db.session.flush()
        second_space = Space(tenant_id=tenant.id, lot_id=second_lot.id, name="S1")
        db.session.add(second_space)
        db.session.flush()
        first_booking = _create_booking(
            tenant.id,
            first_lot.id,
            first_space.id,
            "PP-FIRST-001",
            "first@example.test",
        )
        second_booking = _create_booking(
            tenant.id,
            second_lot.id,
            second_space.id,
            "PP-SECOND-001",
            "second@example.test",
        )
        second_lot_id = second_lot.id
        other_lot_id = other_tenant[1].id
        first_ref = first_booking.booking_ref
        second_ref = second_booking.booking_ref

    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    initial_dashboard = client.get("/admin/").get_data(as_text=True)
    assert 'data-owner-search' in initial_dashboard
    assert 'id="owner-location-select"' in initial_dashboard
    assert 'class="owner-home-link" href="/"' in initial_dashboard
    assert 'name="lot_id" aria-label="Business location" data-autosubmit' in initial_dashboard
    assert 'class="side-group"' in initial_dashboard
    assert 'class="side-group" open' not in initial_dashboard
    assert 'class="admin-mobile-nav admin-mobile-nav-owner"' in initial_dashboard
    assert 'aria-label="Main"' in initial_dashboard
    assert 'css/admin-mobile.css' in initial_dashboard
    assert initial_dashboard.index('class="topbar"') < initial_dashboard.index('class="admin-mobile-nav admin-mobile-nav-owner"')
    for mobile_link in ('Extras</a>', 'Pricing rules</a>', 'Discount codes</a>', 'Invoices</a>', 'Reports</a>', 'Blog &amp; FAQs</a>', 'Team</a>', 'Vehicle checks</a>', 'Disputes</a>', 'Staff app</a>'):
        assert mobile_link in initial_dashboard
    assert 'class="admin-mobile-more"' in initial_dashboard
    assert 'class="mobile-more-menu"' in initial_dashboard
    assert 'class="topbar-main-controls"' in initial_dashboard
    assert 'class="topbar-account-controls"' in initial_dashboard
    for hidden_option in ("Plan &amp; billing", "Brand &amp; domain", "Translations"):
        assert hidden_option not in initial_dashboard
    assert "Integrations</a>" not in initial_dashboard
    switched = client.post("/admin/location", data={"lot_id": second_lot_id})
    assert switched.status_code == 302
    dashboard = client.get("/admin/").get_data(as_text=True)
    assert second_ref in dashboard and first_ref not in dashboard
    assert "Arrivals in the next 24 hours" in dashboard
    assert "Departures in the next 24 hours" in dashboard
    assert client.get("/admin/search", query_string={"q": first_ref}).json["results"]
    assert client.post("/admin/location", data={"lot_id": other_lot_id}).status_code == 404
    assert client.post("/admin/location", data={"lot_id": "not-a-number"}).status_code == 404


def test_integrations_are_owner_only_and_provider_adapters_do_not_guess_api_contracts(client, app):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        db.session.add(
            ExternalIntegrationConnection(
                tenant_id=tenant.id,
                provider="parkos",
                status="pending",
                credential_ref="vault://private/parkos",
            )
        )
        db.session.commit()
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    page = client.get("/admin/integrations")
    assert page.status_code == 200
    assert b"Parkos" in page.data and b"Parkenlist" in page.data
    assert b"Not connected" in page.data
    assert b"vault://private/parkos" not in page.data
    with pytest.raises(IntegrationNotReady, match="official API contract"):
        adapter_for("parkos").import_reservations("placeholder")
    with pytest.raises(IntegrationNotReady, match="official API contract"):
        adapter_for("parkenlist").sync_availability("placeholder", [])


def test_global_search_requires_an_owner(client, staff):
    client.post(
        "/admin/login",
        data={"email": "staff@test.dev", "password": "staff-pass"},
    )
    assert client.get("/admin/search?q=guest").status_code == 403
    assert client.get("/admin/integrations").status_code == 403


def test_alert_feed_includes_exact_timestamp(client, app):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        created_at = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        db.session.add(
            Alert(
                tenant_id=tenant.id,
                kind="booking_created",
                data={"ref": "PP-TIME-001", "customer": "Ada", "total": "42.00 EUR"},
                created_at=created_at,
            )
        )
        db.session.commit()
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
    )
    alert = client.get("/alerts/feed?after=0").json["alerts"][0]
    assert alert["created_at"] == created_at.isoformat()


def test_external_booking_mapping_unique_key_prevents_duplicate_imports(app):
    with app.app_context():
        tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
        connection = ExternalIntegrationConnection(
            tenant_id=tenant.id,
            provider="parkos",
            status="pending",
        )
        db.session.add(connection)
        db.session.flush()
        mapping = ExternalBookingMapping(
            tenant_id=tenant.id,
            connection_id=connection.id,
            external_booking_id="provider-booking-1",
        )
        db.session.add(mapping)
        db.session.commit()

        db.session.add(
            ExternalBookingMapping(
                tenant_id=tenant.id,
                connection_id=connection.id,
                external_booking_id="provider-booking-1",
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
