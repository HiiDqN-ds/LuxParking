from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from app.extensions import db, socketio
from app.models import Hold, Lot, Space, Tenant
from app.services.bookings import create_booking_from_hold, create_hold, release_hold
from app.tenancy import use_tenant


@pytest.fixture()
def public_lot(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        lot.base_daily_rate = 15
        spaces = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()
        spaces[0].base_price = 0
        spaces[1].base_price = 27
        db.session.commit()
        return tenant.slug, lot.slug, tenant.id, lot.id, spaces[0].id, spaces[1].id


def interval():
    start = datetime.now(timezone.utc) + timedelta(days=2)
    return start, start + timedelta(hours=24)


def availability_url(tenant_slug, lot_slug, start, end):
    query = urlencode({"pickup_at": start.isoformat(), "return_at": end.isoformat()})
    return f"/reserve/{tenant_slug}/{lot_slug}/availability?{query}"


def test_home_and_reservation_page_show_configured_lot(client, public_lot):
    tenant_slug, lot_slug, *_ = public_lot
    assert client.get("/").status_code == 200
    response = client.get(f"/reserve/{tenant_slug}/{lot_slug}")
    assert response.status_code == 200
    assert b"Live parking map" in response.data
    assert b"Hold this space for 10 minutes" in response.data


def test_availability_price_uses_space_then_lot_daily_rate(app, client, public_lot):
    tenant_slug, lot_slug, tenant_id, _, fallback_space_id, override_space_id = public_lot
    start, end = interval()
    response = client.get(availability_url(tenant_slug, lot_slug, start, end))

    assert response.status_code == 200
    spaces = {space["id"]: space for space in response.json["spaces"]}
    assert spaces[fallback_space_id]["price"] == "15.00"
    assert spaces[override_space_id]["price"] == "27.00"
    assert spaces[fallback_space_id]["selectable"] is True

    with use_tenant(tenant_id):
        space = db.session.get(Space, override_space_id)
        space.base_price = 27
        db.session.commit()
    longer_end = start + timedelta(hours=25)
    longer = client.get(availability_url(tenant_slug, lot_slug, start, longer_end))
    longer_spaces = {space["id"]: space for space in longer.json["spaces"]}
    assert longer_spaces[override_space_id]["price"] == "54.00"


def test_zero_rate_is_not_presented_as_a_free_reservation(client):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        lot.base_daily_rate = 0
        space = Space.query.filter_by(lot_id=lot.id).first()
        space.base_price = 0
        db.session.commit()
        tenant_slug, lot_slug = tenant.slug, lot.slug

    start, end = interval()
    response = client.get(availability_url(tenant_slug, lot_slug, start, end))
    space_state = next(item for item in response.json["spaces"] if item["id"] == space.id)
    assert space_state["price"] is None
    assert space_state["selectable"] is False
    assert "Pricing has not been configured" in space_state["price_error"]


def test_public_hold_rejects_unconfigured_price(client):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        lot.base_daily_rate = 0
        space.base_price = 0
        db.session.commit()
        tenant_slug, lot_slug, space_id = tenant.slug, lot.slug, space.id

    start, end = interval()
    response = client.post(
        f"/reserve/{tenant_slug}/{lot_slug}/holds",
        data={
            "space_id": str(space_id),
            "pickup_at": start.isoformat(),
            "return_at": end.isoformat(),
            "email": "guest@example.com",
        },
    )
    assert response.status_code == 302
    with use_tenant(tenant.id):
        assert Hold.query.filter_by(lot_id=lot.id).count() == 0
    page = client.get(response.headers["Location"])
    assert b"Pricing has not been configured" in page.data


def test_availability_marks_overlapping_booking_and_later_release(app, client, public_lot):
    tenant_slug, lot_slug, tenant_id, lot_id, space_id, _ = public_lot
    start, end = interval()
    hold = create_hold(
        tenant_id=tenant_id,
        lot_id=lot_id,
        space_id=space_id,
        pickup_at=start,
        return_at=end,
        customer_email="guest@example.com",
    )
    create_booking_from_hold(
        tenant_id=tenant_id,
        hold_id=hold.id,
        first_name="Guest",
        email="guest@example.com",
        phone="+491234567",
        total_amount="15.00",
        currency="EUR",
    )
    response = client.get(availability_url(tenant_slug, lot_slug, start, end))
    blocked = next(item for item in response.json["spaces"] if item["id"] == space_id)
    assert blocked["status"] == "reserved"
    assert blocked["selectable"] is False


def test_public_hold_post_creates_ten_minute_hold_and_confirmation(client, public_lot):
    tenant_slug, lot_slug, _, _, space_id, _ = public_lot
    start, end = interval()
    response = client.post(
        f"/reserve/{tenant_slug}/{lot_slug}/holds",
        data={
            "space_id": str(space_id),
            "pickup_at": start.isoformat(),
            "return_at": end.isoformat(),
            "email": " Guest@Example.com ",
        },
    )
    assert response.status_code == 302
    confirmation = client.get(response.headers["Location"])
    assert confirmation.status_code == 200
    assert b"Your space is held for 10 minutes" in confirmation.data
    assert b"no payment is collected" in confirmation.data.lower()

    refreshed = client.get(availability_url(tenant_slug, lot_slug, start, end))
    held_space = next(item for item in refreshed.json["spaces"] if item["id"] == space_id)
    assert held_space["status"] == "reserved"
    assert held_space["selectable"] is False


def test_public_availability_rejects_naive_or_invalid_intervals(client, public_lot):
    tenant_slug, lot_slug, *_ = public_lot
    response = client.get(
        f"/reserve/{tenant_slug}/{lot_slug}/availability",
        query_string={"pickup_at": "2027-01-01T12:00", "return_at": "2027-01-02T12:00"},
    )
    assert response.status_code == 400
    assert "timezone" in response.json["error"]


def test_public_map_is_tenant_and_lot_scoped(client, public_lot, other_tenant):
    tenant_slug, lot_slug, *_ = public_lot
    start, end = interval()
    wrong_lot = client.get(availability_url(tenant_slug, "other-lot", start, end))
    wrong_tenant_path = client.get(availability_url("other", lot_slug, start, end))
    assert wrong_lot.status_code == 404
    assert wrong_tenant_path.status_code == 404
    assert other_tenant[0].id != public_lot[2]


def test_socket_room_receives_hold_change_without_personal_data(client, app, public_lot):
    tenant_slug, lot_slug, _, _, space_id, _ = public_lot
    socket_client = socketio.test_client(app, namespace="/booking")
    joined = socket_client.emit(
        "join_lot",
        {"tenant_slug": tenant_slug, "lot_slug": lot_slug},
        namespace="/booking",
        callback=True,
    )
    assert joined == {"joined": True}

    start, end = interval()
    response = client.post(
        f"/reserve/{tenant_slug}/{lot_slug}/holds",
        data={
            "space_id": str(space_id),
            "pickup_at": start.isoformat(),
            "return_at": end.isoformat(),
            "email": "private@example.com",
        },
    )
    assert response.status_code == 302
    events = socket_client.get_received("/booking")
    change = next(event for event in events if event["name"] == "availability_changed")
    assert change["args"] == [{"lot_id": public_lot[3]}]
    assert "private@example.com" not in str(change["args"])

    hold_id = int(response.location.rsplit("/", 1)[-1])
    release_hold(tenant_id=public_lot[2], hold_id=hold_id)
    released_change = socket_client.get_received("/booking")
    assert next(
        event for event in released_change if event["name"] == "availability_changed"
    )["args"] == [{"lot_id": public_lot[3]}]

    start, end = interval()
    replacement = create_hold(
        tenant_id=public_lot[2],
        lot_id=public_lot[3],
        space_id=space_id,
        pickup_at=start,
        return_at=end,
        customer_email="private@example.com",
    )
    create_booking_from_hold(
        tenant_id=public_lot[2],
        hold_id=replacement.id,
        first_name="Casey",
        email="private@example.com",
        phone="+491234567",
        total_amount="15.00",
        currency="EUR",
    )
    converted_change = socket_client.get_received("/booking")
    assert next(
        event for event in converted_change if event["name"] == "availability_changed"
    )["args"] == [{"lot_id": public_lot[3]}]
    socket_client.disconnect(namespace="/booking")


def test_parking_map_renders_configured_background_image(app, client, public_lot):
    with use_tenant(public_lot[2]):
        lot = db.session.get(Lot, public_lot[3])
        lot.background_url = "/static/lot-background.webp"
        db.session.commit()

    response = client.get(f"/reserve/{public_lot[0]}/{public_lot[1]}")

    assert response.status_code == 200
    assert b'<image class="lot-background-image" href="/static/lot-background.webp"' in response.data
