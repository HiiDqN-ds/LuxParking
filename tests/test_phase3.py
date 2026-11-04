from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from PIL import Image

from app.extensions import db
from app.models import Lot, Payment, Space, Tenant
from app.services.bookings import (
    BookingConflict,
    BookingNotFound,
    BookingValidationError,
    InvalidBookingTransition,
    cancel_booking,
    check_in_booking,
    check_out_booking,
    create_booking_from_hold,
    create_hold,
    create_payment_record,
    release_hold,
    update_payment_status,
)
from app.services.inspections import (
    REQUIRED_PHOTO_CATEGORIES,
    complete_inspection,
    create_inspection,
    upload_inspection_photo,
)
from app.tenancy import use_tenant


@pytest.fixture()
def parking(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        return tenant.id, lot.id, space.id


def interval():
    start = datetime.now(timezone.utc) + timedelta(days=2)
    return start, start + timedelta(hours=24)


def hold_for(parking, start, end, email="customer@example.com"):
    tenant_id, lot_id, space_id = parking
    return create_hold(
        tenant_id=tenant_id,
        lot_id=lot_id,
        space_id=space_id,
        pickup_at=start,
        return_at=end,
        customer_email=email,
    )


def booking_for(hold, parking, *, amount="40.00"):
    return create_booking_from_hold(
        tenant_id=parking[0],
        hold_id=hold.id,
        first_name="Casey",
        last_name="Driver",
        email=hold.customer_email,
        phone="+491234567",
        vehicle_plate="D-PP 1234",
        vehicle_make="Example",
        vehicle_model="Car",
        total_amount=amount,
        currency="EUR",
    )


def complete_evidence(parking, booking, phase, monkeypatch):
    from app.services import inspections

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    image = BytesIO()
    Image.new("RGB", (4, 4), color=(30, 100, 150)).save(image, format="WEBP")
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase=phase,
        staff_user_id=1,
    )
    for category in (*REQUIRED_PHOTO_CATEGORIES, "signature"):
        upload_inspection_photo(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            category=category,
            file_data=image.getvalue(),
            content_type="image/webp",
            captured_at=datetime.now(timezone.utc),
        )
    complete_inspection(
        tenant_id=parking[0],
        inspection_id=inspection.id,
        staff_user_id=1,
        fuel_level=50,
        odometer_km=10000,
        key_count=2,
        belongings="",
        notes="",
        no_damage_confirmed=True,
    )


def test_hold_blocks_overlapping_interval_for_ten_minutes(app, parking):
    start, end = interval()
    hold = hold_for(parking, start, end)

    expires_at = hold.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=9, seconds=59) <= expires_at - datetime.now(timezone.utc) <= timedelta(minutes=10)

    with pytest.raises(BookingConflict):
        hold_for(parking, start + timedelta(hours=1), end + timedelta(hours=1), "another@example.com")

    adjacent = hold_for(parking, end, end + timedelta(hours=4), "another@example.com")
    assert adjacent.status == "active"


def test_expired_hold_does_not_block_a_new_hold(app, parking):
    start, end = interval()
    stale = hold_for(parking, start, end)
    stale.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()

    replacement = hold_for(parking, start, end, "replacement@example.com")
    assert replacement.id != stale.id


def test_hold_conversion_and_booking_state_transitions(app, parking, monkeypatch):
    start, end = interval()
    hold = hold_for(parking, start, end)
    booking = booking_for(hold, parking)
    assert booking.status == "reserved"
    assert booking.payment_status == "pending"
    assert booking.qr_code and booking.booking_ref not in booking.qr_code
    assert hold.status == "converted"

    complete_evidence(parking, booking, "arrival", monkeypatch)
    assert check_in_booking(tenant_id=parking[0], booking_id=booking.id).status == "checked_in"
    complete_evidence(parking, booking, "departure", monkeypatch)
    assert check_out_booking(tenant_id=parking[0], booking_id=booking.id).status == "checked_out"
    with pytest.raises(InvalidBookingTransition):
        check_in_booking(tenant_id=parking[0], booking_id=booking.id)

    replacement = hold_for(parking, start, end, "replacement@example.com")
    assert replacement.status == "active"


def test_booking_requires_live_hold_and_matching_email(app, parking):
    start, end = interval()
    hold = hold_for(parking, start, end)

    with pytest.raises(BookingValidationError):
        create_booking_from_hold(
            tenant_id=parking[0],
            hold_id=hold.id,
            first_name="Casey",
            email="different@example.com",
            phone="+491234567",
            total_amount="40.00",
            currency="EUR",
        )
    assert hold.status == "active"

    converted = booking_for(hold, parking)
    with pytest.raises(InvalidBookingTransition):
        booking_for(hold, parking)
    assert converted.status == "reserved"


def test_booking_and_hold_queries_are_tenant_scoped(app, parking, other_tenant):
    start, end = interval()
    hold = hold_for(parking, start, end)
    booking = booking_for(hold, parking)

    with pytest.raises(BookingNotFound):
        cancel_booking(tenant_id=other_tenant[0].id, booking_id=booking.id)
    with pytest.raises(BookingNotFound):
        release_hold(tenant_id=other_tenant[0].id, hold_id=hold.id)


def test_cancelled_booking_releases_its_interval(app, parking):
    start, end = interval()
    booking = booking_for(hold_for(parking, start, end), parking)
    assert cancel_booking(tenant_id=parking[0], booking_id=booking.id).status == "cancelled"
    assert hold_for(parking, start, end, "next@example.com").status == "active"


def test_payment_records_update_booking_payment_state(app, parking):
    start, end = interval()
    booking = booking_for(hold_for(parking, start, end), parking)
    first = create_payment_record(
        tenant_id=parking[0],
        booking_id=booking.id,
        provider="manual",
        amount=Decimal("20.00"),
        currency="EUR",
    )
    update_payment_status(tenant_id=parking[0], payment_id=first.id, status="paid")
    db.session.refresh(booking)
    assert booking.payment_status == "partial"

    second = create_payment_record(
        tenant_id=parking[0],
        booking_id=booking.id,
        provider="manual",
        amount=Decimal("20.00"),
        currency="EUR",
    )
    update_payment_status(tenant_id=parking[0], payment_id=second.id, status="paid")
    db.session.refresh(booking)
    assert booking.payment_status == "paid"

    update_payment_status(tenant_id=parking[0], payment_id=first.id, status="refunded")
    db.session.refresh(booking)
    assert booking.payment_status == "partial"
    with pytest.raises(InvalidBookingTransition):
        update_payment_status(tenant_id=parking[0], payment_id=first.id, status="paid")


def test_booking_input_rejects_naive_dates_and_unknown_provider(app, parking):
    tenant_id, lot_id, space_id = parking
    start, end = interval()
    with pytest.raises(BookingValidationError):
        create_hold(
            tenant_id=tenant_id,
            lot_id=lot_id,
            space_id=space_id,
            pickup_at=start.replace(tzinfo=None),
            return_at=end,
            customer_email="customer@example.com",
        )

    hold = hold_for(parking, start, end)
    booking = booking_for(hold, parking)
    with pytest.raises(BookingValidationError):
        create_payment_record(
            tenant_id=tenant_id,
            booking_id=booking.id,
            provider="unknown",
            amount="40",
            currency="EUR",
        )
    with pytest.raises(BookingValidationError):
        create_payment_record(
            tenant_id=tenant_id,
            booking_id=booking.id,
            provider="manual",
            amount="40",
            currency="USD",
        )
    assert Payment.query.count() == 0
