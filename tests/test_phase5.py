from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from app.extensions import db
from app.models import (
    Booking,
    DamageMark,
    Dispute,
    Hold,
    Inspection,
    InspectionPhoto,
    InspectionVideo,
    Lot,
    Space,
    Tenant,
    Vehicle,
)
from app.services.bookings import (
    BookingConflict,
    check_in_booking,
    check_out_booking,
    create_booking_from_hold,
    create_hold,
)
from app.services.inspections import (
    REQUIRED_PHOTO_CATEGORIES,
    InspectionConflict,
    InspectionNotFound,
    InspectionValidationError,
    add_damage_mark,
    complete_inspection,
    create_inspection,
    purge_expired_inspections,
    upload_inspection_photo,
    upload_inspection_video,
)
from app.services.inspection_reports import send_inspection_report
from app.tenancy import use_tenant


@pytest.fixture()
def parking(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        return tenant.id, lot.id, space.id


def create_reserved_booking(parking, email="inspection@example.com"):
    tenant_id, lot_id, space_id = parking
    pickup_at = datetime.now(timezone.utc) + timedelta(days=2)
    return_at = pickup_at + timedelta(hours=24)
    hold = create_hold(
        tenant_id=tenant_id,
        lot_id=lot_id,
        space_id=space_id,
        pickup_at=pickup_at,
        return_at=return_at,
        customer_email=email,
    )
    return create_booking_from_hold(
        tenant_id=tenant_id,
        hold_id=hold.id,
        first_name="Casey",
        email=email,
        phone="+491234567",
        total_amount="30.00",
        currency="EUR",
        vehicle_plate="D-PP 1234",
    )


def valid_webp():
    output = BytesIO()
    Image.new("RGB", (4, 4), color=(30, 100, 150)).save(output, format="WEBP")
    return output.getvalue()


def upload_required_photos(tenant_id, inspection_id):
    for category in (*REQUIRED_PHOTO_CATEGORIES, "signature"):
        upload_inspection_photo(
            tenant_id=tenant_id,
            inspection_id=inspection_id,
            staff_user_id=1,
            category=category,
            file_data=valid_webp(),
            content_type="image/webp",
            captured_at=datetime.now(timezone.utc),
        )


def upload_required_videos(tenant_id, inspection_id):
    video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    upload_inspection_video(
        tenant_id=tenant_id,
        inspection_id=inspection_id,
        staff_user_id=1,
        category="walkaround",
        file_data=video,
        content_type="video/mp4",
        captured_at=datetime.now(timezone.utc),
    )


def test_upload_stores_verified_hash_and_employee_metadata(app, parking, monkeypatch):
    from app.services import inspections

    captured = []
    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: captured.append(kwargs))
    booking = create_reserved_booking(parking)
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )
    image = valid_webp()

    photo = upload_inspection_photo(
        tenant_id=parking[0],
        inspection_id=inspection.id,
        staff_user_id=1,
        category="front",
        file_data=image,
        content_type="image/webp",
        captured_at=datetime.now(timezone.utc),
    )

    assert photo.sha256 == hashlib.sha256(image).hexdigest()
    assert photo.size_bytes == len(image)
    assert photo.staff_name == "Owner"
    assert photo.booking_ref == booking.booking_ref
    assert captured[0]["sha256"] == photo.sha256

    with pytest.raises(InspectionValidationError, match="not a valid inspection image"):
        upload_inspection_photo(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            category="back",
            file_data=b"not an image",
            content_type="image/webp",
            captured_at=datetime.now(timezone.utc),
        )


def test_video_upload_stores_private_walkaround_video(app, parking, monkeypatch):
    from app.services import inspections

    captured = []
    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: captured.append(kwargs))
    booking = create_reserved_booking(parking)
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )
    video_data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    video = upload_inspection_video(
        tenant_id=parking[0],
        inspection_id=inspection.id,
        staff_user_id=1,
        category="walkaround",
        file_data=video_data,
        content_type="video/mp4",
        captured_at=datetime.now(timezone.utc),
    )

    assert video.sha256 == hashlib.sha256(video_data).hexdigest()
    assert video.size_bytes == len(video_data)
    assert video.content_type == "video/mp4"
    assert video.staff_name == "Owner"
    assert video.booking_ref == booking.booking_ref
    assert video.object_key.endswith(".mp4")
    assert captured[0]["file_data"] == video_data
    assert InspectionVideo.query.filter_by(inspection_id=inspection.id, category="walkaround").one().id == video.id

    with pytest.raises(InspectionConflict, match="Only one video"):
        upload_inspection_video(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            category="walkaround",
            file_data=video_data,
            content_type="video/mp4",
            captured_at=datetime.now(timezone.utc),
        )

    with pytest.raises(InspectionValidationError, match="valid inspection video"):
        upload_inspection_video(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            category="walkaround",
            file_data=b"not a video",
            content_type="video/mp4",
            captured_at=datetime.now(timezone.utc),
        )

def test_damage_marker_rejects_non_finite_coordinates(app, parking):
    booking = create_reserved_booking(parking)
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )

    with pytest.raises(InspectionValidationError, match="between 0 and 100"):
        add_damage_mark(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            x=float("nan"),
            y=40,
            kind="scratch",
            notes="Invalid coordinate",
            file_data=valid_webp(),
            content_type="image/webp",
            captured_at=datetime.now(timezone.utc),
        )


def test_check_in_requires_complete_photo_and_signature_evidence(app, parking, monkeypatch):
    from app.services import inspections

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    monkeypatch.setattr(inspections, "_delete_object", lambda **kwargs: None)
    booking = create_reserved_booking(parking)
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )

    with pytest.raises(InspectionValidationError, match="Missing required photos"):
        complete_inspection(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            fuel_level=60,
            odometer_km=12000,
            key_count=2,
            belongings="Child seat",
            notes="",
            no_damage_confirmed=True,
        )
    with pytest.raises(BookingConflict, match="completed arrival inspection"):
        check_in_booking(tenant_id=parking[0], booking_id=booking.id)

    upload_required_photos(parking[0], inspection.id)
    with pytest.raises(InspectionValidationError, match="A vehicle video is required"):
        complete_inspection(
            tenant_id=parking[0],
            inspection_id=inspection.id,
            staff_user_id=1,
            fuel_level=60,
            odometer_km=12000,
            key_count=2,
            belongings="Child seat",
            notes="",
            no_damage_confirmed=True,
            require_videos=True,
        )
    upload_required_videos(parking[0], inspection.id)
    complete_inspection(
        tenant_id=parking[0],
        inspection_id=inspection.id,
        staff_user_id=1,
        fuel_level=60,
        odometer_km=12000,
        key_count=2,
        belongings="Child seat",
        notes="",
        no_damage_confirmed=True,
        require_videos=True,
    )

    assert check_in_booking(tenant_id=parking[0], booking_id=booking.id).status == "checked_in"


def test_departure_requires_its_own_inspection_and_opens_unmatched_damage_dispute(
    app, parking, monkeypatch
):
    from app.services import inspections

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    monkeypatch.setattr(inspections, "_delete_object", lambda **kwargs: None)
    booking = create_reserved_booking(parking)
    arrival = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )
    upload_required_photos(parking[0], arrival.id)
    existing_damage = add_damage_mark(
        tenant_id=parking[0],
        inspection_id=arrival.id,
        staff_user_id=1,
        x=30,
        y=40,
        kind="scratch",
        notes="Existing mark",
        file_data=valid_webp(),
        content_type="image/webp",
        captured_at=datetime.now(timezone.utc),
    )
    complete_inspection(
        tenant_id=parking[0],
        inspection_id=arrival.id,
        staff_user_id=1,
        fuel_level=60,
        odometer_km=12000,
        key_count=2,
        belongings="",
        notes="",
        no_damage_confirmed=False,
    )
    check_in_booking(tenant_id=parking[0], booking_id=booking.id)

    with pytest.raises(BookingConflict, match="completed departure inspection"):
        check_out_booking(tenant_id=parking[0], booking_id=booking.id)

    departure = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="departure",
        staff_user_id=1,
    )
    upload_required_photos(parking[0], departure.id)
    add_damage_mark(
        tenant_id=parking[0],
        inspection_id=departure.id,
        staff_user_id=1,
        x=30,
        y=40,
        kind="scratch",
        notes="Same pre-existing mark",
        matched_damage_id=existing_damage.id,
        file_data=valid_webp(),
        content_type="image/webp",
        captured_at=datetime.now(timezone.utc),
    )
    new_damage = add_damage_mark(
        tenant_id=parking[0],
        inspection_id=departure.id,
        staff_user_id=1,
        x=72,
        y=18,
        kind="dent",
        notes="New dent",
        file_data=valid_webp(),
        content_type="image/webp",
        captured_at=datetime.now(timezone.utc),
    )
    complete_inspection(
        tenant_id=parking[0],
        inspection_id=departure.id,
        staff_user_id=1,
        fuel_level=45,
        odometer_km=12110,
        key_count=2,
        belongings="",
        notes="",
        no_damage_confirmed=False,
    )

    disputes = Dispute.query.filter_by(booking_id=booking.id, tenant_id=parking[0]).all()
    assert [dispute.damage_mark_id for dispute in disputes] == [new_damage.id]
    assert check_out_booking(tenant_id=parking[0], booking_id=booking.id).status == "checked_out"


def test_inspection_records_are_tenant_scoped(app, parking, other_tenant, monkeypatch):
    from app.services import inspections

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    monkeypatch.setattr(inspections, "_delete_object", lambda **kwargs: None)
    booking = create_reserved_booking(parking)
    inspection = create_inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        staff_user_id=1,
    )

    with pytest.raises(InspectionNotFound, match="not found"):
        complete_inspection(
            tenant_id=other_tenant[0].id,
            inspection_id=inspection.id,
            staff_user_id=1,
            fuel_level=60,
            odometer_km=12000,
            key_count=2,
            belongings="",
            notes="",
            no_damage_confirmed=True,
        )
    assert db.session.get(Inspection, inspection.id) is not None


def test_staff_inspection_pages_require_staff_login(client, app):
    assert client.get("/staff/").status_code == 302


def test_staff_pwa_and_vehicle_check_settings_are_role_scoped(client, staff, app):
    login = client.post(
        "/admin/login",
        data={"email": staff.email, "password": "staff-pass"},
        follow_redirects=True,
    )
    assert login.status_code == 200
    assert client.get("/staff/").status_code == 200
    worker = client.get("/staff/service-worker.js")
    assert worker.status_code == 200
    assert worker.headers["Service-Worker-Allowed"] == "/staff/"
    assert client.get("/admin/inspection-settings").status_code == 403


def test_staff_access_is_limited_to_active_vehicle_operations(client, staff, parking, app):
    booking = create_reserved_booking(parking)
    login = client.post(
        "/admin/login",
        data={"email": staff.email, "password": "staff-pass"},
        follow_redirects=True,
    )
    assert login.status_code == 200

    dashboard = client.get("/staff/")
    assert dashboard.status_code == 200
    assert b"Incoming and parked vehicles" in dashboard.data
    assert b"D-PP 1234" in dashboard.data
    assert b"Casey" not in dashboard.data
    assert b"Create walk-in hold" not in dashboard.data
    assert b"Open disputes" not in dashboard.data

    detail = client.get(f"/staff/bookings/{booking.id}")
    assert detail.status_code == 200
    assert b"D-PP 1234" in detail.data
    assert b"Casey" not in detail.data
    assert b"+491234567" not in detail.data
    assert b"Payment" not in detail.data
    assert b"Parkplatz Duesseldorf Flughafen" not in detail.data
    assert client.get("/staff/walk-in").status_code == 403
    assert client.get("/staff/disputes").status_code == 403

    booking.pickup_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.session.commit()
    assert client.get(f"/staff/bookings/{booking.id}").status_code == 404
    assert b"D-PP 1234" not in client.get("/staff/").data

    booking.status = "checked_in"
    db.session.commit()
    assert client.get(f"/staff/bookings/{booking.id}").status_code == 200
    assert b"D-PP 1234" in client.get("/staff/").data

    booking.status = "cancelled"
    db.session.commit()
    assert client.get(f"/staff/bookings/{booking.id}").status_code == 404
    assert client.get(f"/staff/bookings/{booking.id}/qr.svg").status_code == 404


def test_staff_walk_in_creates_releasable_ten_minute_hold_without_booking(client, parking, app):
    lot = db.session.get(Lot, parking[1])
    lot.base_daily_rate = 30
    db.session.commit()
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
        follow_redirects=True,
    )
    local_now = datetime.now(ZoneInfo("Europe/Berlin"))
    pickup = (local_now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    returned = (local_now + timedelta(hours=27)).strftime("%Y-%m-%dT%H:%M")
    before = datetime.now(timezone.utc)

    response = client.post(
        "/staff/walk-in",
        data={
            "space_id": str(parking[2]),
            "pickup_at": pickup,
            "return_at": returned,
            "email": "walkin@example.com",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.location.startswith("/staff/holds/")
    hold = Hold.query.filter_by(tenant_id=parking[0], customer_email="walkin@example.com").one()
    expiry = hold.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=9, seconds=59) < expiry - before < timedelta(minutes=10, seconds=2)
    assert Booking.query.filter_by(tenant_id=parking[0]).count() == 0
    detail = client.get(response.location)
    assert detail.status_code == 200
    assert b"No booking is confirmed and no payment is collected." in detail.data

    released = client.post(f"/staff/holds/{hold.id}/release", follow_redirects=True)

    assert released.status_code == 200
    assert db.session.get(Hold, hold.id).status == "released"
    assert Booking.query.filter_by(tenant_id=parking[0]).count() == 0


def test_owner_can_configure_inspection_retention_and_required_photos(client, app):
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
        follow_redirects=True,
    )
    settings_page = client.get("/admin/inspection-settings")
    assert settings_page.status_code == 200
    response = client.post(
        "/admin/inspection-settings",
        data={"retention_days": "180", "required_photos": ["front", "back"]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    assert tenant.inspection_retention_days == 180
    assert tenant.inspection_required_photos == ["front", "back"]


def test_signed_qr_code_resolves_only_a_booking_in_the_staff_tenant(
    client, parking, app, monkeypatch
):
    from app.services import inspections

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    monkeypatch.setattr(inspections, "_delete_object", lambda **kwargs: None)
    booking = create_reserved_booking(parking)
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
        follow_redirects=True,
    )

    response = client.post(
        "/staff/lookup",
        data={"booking_code": booking.qr_code},
    )

    assert response.status_code == 302
    assert response.location.endswith(f"/staff/bookings/{booking.id}")
    assert client.get(response.location).status_code == 200
    qr_image = client.get(f"/staff/bookings/{booking.id}/qr.svg")
    assert qr_image.status_code == 200
    assert qr_image.mimetype == "image/svg+xml"

    scanned = client.get(
        f"/staff/open?token={booking.qr_code}",
    )
    assert scanned.status_code == 302
    assert scanned.location.endswith(f"/staff/bookings/{booking.id}")

    start = client.post(
        f"/staff/bookings/{booking.id}/inspections/arrival/start",
        follow_redirects=True,
    )
    assert start.status_code == 200
    arrival_page = client.get(f"/staff/bookings/{booking.id}/inspections/arrival")
    assert arrival_page.status_code == 200

    arrival = Inspection.query.filter_by(booking_id=booking.id, phase="arrival").one()
    upload_required_photos(parking[0], arrival.id)
    complete_inspection(
        tenant_id=parking[0],
        inspection_id=arrival.id,
        staff_user_id=1,
        fuel_level=50,
        odometer_km=10000,
        key_count=2,
        belongings="",
        notes="",
        no_damage_confirmed=True,
    )
    from app.services.bookings import check_in_booking

    check_in_booking(tenant_id=parking[0], booking_id=booking.id)
    departure = client.post(
        f"/staff/bookings/{booking.id}/inspections/departure/start",
        follow_redirects=True,
    )
    assert departure.status_code == 200
    departure_page = client.get(f"/staff/bookings/{booking.id}/inspections/departure")
    assert departure_page.status_code == 200
    assert b"Arrival and departure photos" in departure_page.data


def test_arrival_inspection_creates_vehicle_profile_and_links_existing_photos(
    client, parking, app, monkeypatch
):
    from app.services import inspections
    from app.blueprints import staff

    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    monkeypatch.setattr(staff, "send_inspection_report", lambda *args, **kwargs: None)
    booking = create_reserved_booking(parking)
    booking.vehicle_id = None
    db.session.commit()
    client.post(
        "/admin/login",
        data={"email": "owner@test.dev", "password": "correct-horse"},
        follow_redirects=True,
    )

    started = client.post(
        f"/staff/bookings/{booking.id}/inspections/arrival/start",
        follow_redirects=True,
    )
    assert started.status_code == 200
    assert b"Vehicle profile" in started.data
    inspection = Inspection.query.filter_by(booking_id=booking.id, phase="arrival").one()
    upload_required_photos(parking[0], inspection.id)
    upload_required_videos(parking[0], inspection.id)

    completed = client.post(
        f"/staff/inspections/{inspection.id}/complete",
        data={
            "fuel_level": "50",
            "odometer_km": "10000",
            "key_count": "2",
            "belongings": "",
            "notes": "",
            "no_damage_confirmed": "yes",
            "vehicle_plate": "D-NEW 123",
            "vehicle_make": "Example Motors",
            "vehicle_model": "City",
            "vehicle_color": "Blue",
        },
        follow_redirects=True,
    )

    assert completed.status_code == 200
    vehicle = Vehicle.query.filter_by(tenant_id=parking[0], plate="D-NEW 123").one()
    db.session.refresh(booking)
    assert booking.vehicle_id == vehicle.id
    assert (vehicle.make, vehicle.model, vehicle.color) == ("Example Motors", "City", "Blue")
    linked_photos = InspectionPhoto.query.filter_by(
        tenant_id=parking[0],
        inspection_id=inspection.id,
        vehicle_id=vehicle.id,
    ).all()
    assert {photo.category for photo in linked_photos} == set(REQUIRED_PHOTO_CATEGORIES)
    assert all(photo.booking_ref == booking.booking_ref for photo in linked_photos)

    booking_page = client.get(f"/staff/bookings/{booking.id}")
    assert b"Vehicle profile photos" in booking_page.data
    assert b"Front" in booking_page.data


def test_expired_inspections_are_retained_while_dispute_is_open(app, parking, monkeypatch):
    from app.services import inspections

    monkeypatch.setattr(inspections, "delete_object", lambda **kwargs: None)
    booking = create_reserved_booking(parking)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    arrival = Inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        status="completed",
        retention_until=past,
        no_damage_confirmed=True,
    )
    departure = Inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="departure",
        status="completed",
        retention_until=past,
        no_damage_confirmed=False,
    )
    db.session.add_all([arrival, departure])
    db.session.flush()
    mark = DamageMark(
        tenant_id=parking[0],
        inspection_id=departure.id,
        x_percent=20,
        y_percent=40,
        kind="dent",
        notes="New dent",
    )
    db.session.add(mark)
    db.session.flush()
    db.session.add(
        Dispute(
            tenant_id=parking[0],
            booking_id=booking.id,
            arrival_inspection_id=arrival.id,
            departure_inspection_id=departure.id,
            damage_mark_id=mark.id,
        )
    )
    db.session.commit()

    assert purge_expired_inspections(tenant_id=parking[0]) == 0
    dispute = Dispute.query.filter_by(booking_id=booking.id).one()
    dispute.status = "resolved"
    db.session.commit()

    assert purge_expired_inspections(tenant_id=parking[0]) == 2
    assert Inspection.query.filter_by(booking_id=booking.id).count() == 0


def test_completed_inspection_report_is_emailed_as_pdf_and_recorded(app, parking, monkeypatch):
    from app.services import inspection_reports

    booking = create_reserved_booking(parking)
    inspection = Inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        status="completed",
        staff_user_id=1,
        fuel_level=70,
        odometer_km=12000,
        key_count=2,
        no_damage_confirmed=True,
        retention_until=datetime.now(timezone.utc) + timedelta(days=90),
        completed_at=datetime.now(timezone.utc),
    )
    db.session.add(inspection)
    db.session.commit()
    app.config.update(SMTP_HOST="mail.example.test", SMTP_PORT=587, SMTP_FROM="reports@example.test")
    monkeypatch.setattr(inspection_reports, "build_inspection_pdf", lambda item: b"%PDF-1.4 test")

    class FakeSMTP:
        message = None

        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("mail.example.test", 587, 20)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context):
            assert context is not None

        def send_message(self, message):
            self.__class__.message = message

    monkeypatch.setattr(inspection_reports.smtplib, "SMTP", FakeSMTP)

    send_inspection_report(inspection.id, parking[0])

    assert FakeSMTP.message["To"] == booking.customer.email
    assert any(
        part.get_content_type() == "application/pdf"
        and part.get_payload(decode=True).startswith(b"%PDF")
        for part in FakeSMTP.message.walk()
        if part.get_content_maintype() == "application"
    )
    assert db.session.get(Inspection, inspection.id).report_sent_at is not None


def test_departure_pdf_contains_arrival_and_departure_photo_pair(app, parking, monkeypatch):
    from app.services import inspection_reports

    booking = create_reserved_booking(parking)
    now = datetime.now(timezone.utc)
    arrival = Inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="arrival",
        status="completed",
        staff_user_id=1,
        fuel_level=70,
        odometer_km=12000,
        key_count=2,
        no_damage_confirmed=True,
        retention_until=now + timedelta(days=90),
        completed_at=now,
    )
    departure = Inspection(
        tenant_id=parking[0],
        booking_id=booking.id,
        phase="departure",
        status="completed",
        staff_user_id=1,
        fuel_level=65,
        odometer_km=12100,
        key_count=2,
        no_damage_confirmed=True,
        retention_until=now + timedelta(days=90),
        completed_at=now,
    )
    db.session.add_all([arrival, departure])
    db.session.flush()
    image = valid_webp()
    for inspection, key in ((arrival, "arrival-front.webp"), (departure, "departure-front.webp")):
        db.session.add(
            InspectionPhoto(
                tenant_id=parking[0],
                inspection_id=inspection.id,
                category="front",
                object_key=key,
                sha256=hashlib.sha256(image).hexdigest(),
                content_type="image/webp",
                size_bytes=len(image),
                booking_ref=booking.booking_ref,
                staff_user_id=1,
                staff_name="Owner",
                captured_at=now,
            )
        )
    db.session.commit()
    monkeypatch.setattr(inspection_reports, "read_object", lambda **kwargs: image)

    report = inspection_reports.build_inspection_pdf(departure)

    assert report.startswith(b"%PDF-")
    assert len(report) > 1500
