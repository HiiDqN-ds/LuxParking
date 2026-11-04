"""Staff inspection: the handover form autosaves, one tap completes and checks in, nothing needs a save button."""
from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image

from app.extensions import db
from app.models import Booking, Inspection, Lot, Space, Tenant
from app.services import inspections
from app.services.inspections import (
    REQUIRED_PHOTO_CATEGORIES,
    InspectionConflict,
    create_inspection,
    read_inspection_draft,
    save_inspection_draft,
    upload_inspection_photo,
)
from app.tenancy import use_tenant
from tests.conftest import login
from tests.test_phase3 import booking_for, hold_for, interval


@pytest.fixture()
def parking(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        return tenant.id, lot.id, Space.query.filter_by(lot_id=lot.id).first().id


@pytest.fixture()
def open_inspection(app, parking, monkeypatch):
    monkeypatch.setattr(inspections, "_store_object", lambda **kwargs: None)
    start, end = interval()
    booking = booking_for(hold_for(parking, start, end), parking)
    owner = db.session.execute(db.text("select id from users order by id limit 1")).scalar_one()
    inspection = create_inspection(tenant_id=parking[0], booking_id=booking.id, phase="arrival", staff_user_id=owner)
    from types import SimpleNamespace as NS
    return NS(id=booking.id), NS(id=inspection.id), owner  # plain ids: other fixtures may clear the session


def _add_all_photos(parking, inspection, owner):
    image = BytesIO()
    Image.new("RGB", (4, 4), color=(30, 100, 150)).save(image, format="WEBP")
    for category in (*REQUIRED_PHOTO_CATEGORIES, "signature"):
        upload_inspection_photo(tenant_id=parking[0], inspection_id=inspection.id, staff_user_id=owner, category=category,
                                file_data=image.getvalue(), content_type="image/webp", captured_at=datetime.now(timezone.utc))


def test_draft_is_saved_cleaned_and_prefills_the_page(client, parking, open_inspection):
    booking, inspection, owner = open_inspection
    login(client)
    response = client.post(f"/staff/inspections/{inspection.id}/draft", json={
        "odometer_km": "48200abc", "fuel_level": "60", "key_count": "2", "notes": "Gate code 4711", "vehicle_plate": "D-AK 2026",
        "no_damage_confirmed": "yes", "evil_field": "x"})
    assert response.status_code == 200
    draft = read_inspection_draft(db.session.get(Inspection, inspection.id))
    assert draft["odometer_km"] == "" and draft["fuel_level"] == "60" and "evil_field" not in draft  # digits only, unknown keys dropped
    client.post(f"/staff/inspections/{inspection.id}/draft", json={"odometer_km": "48200"})
    page = client.get(f"/staff/bookings/{booking.id}/inspections/arrival").get_data(as_text=True)
    for text in ('value="48200"', 'value="60"', 'value="D-AK 2026"', "Gate code 4711", 'id="complete-checklist"'):
        assert text in page, text
    assert "checked" in page.split('name="no_damage_confirmed"')[1][:80]
    assert 'id="save-signature"' not in page and "data-upload-url" in page  # the signature saves by itself


def test_draft_is_refused_for_a_completed_inspection_and_bad_bodies(client, parking, open_inspection):
    booking, inspection, owner = open_inspection
    login(client)
    assert client.post(f"/staff/inspections/{inspection.id}/draft", json=["not", "an", "object"]).status_code == 400
    _add_all_photos(parking, inspection, owner)
    save_inspection_draft(tenant_id=parking[0], inspection_id=inspection.id, staff_user_id=owner, values={"odometer_km": "5"})
    inspections.complete_inspection(tenant_id=parking[0], inspection_id=inspection.id, staff_user_id=owner, fuel_level=50, odometer_km=5,
                                    key_count=1, belongings="", notes="", no_damage_confirmed=True)
    assert db.session.get(Inspection, inspection.id).draft_details is None  # the final values replace the draft
    assert client.post(f"/staff/inspections/{inspection.id}/draft", json={"notes": "late"}).status_code == 409
    with pytest.raises(InspectionConflict):
        save_inspection_draft(tenant_id=parking[0], inspection_id=inspection.id, staff_user_id=owner, values={"notes": "late"})


def test_draft_needs_a_login_and_stays_inside_its_tenant(app, client, parking, open_inspection, other_tenant):
    from app.models import Role, User
    booking, inspection, owner = open_inspection
    anonymous = client.post(f"/staff/inspections/{inspection.id}/draft", json={"notes": "x"})  # not signed in at all
    assert anonymous.status_code == 302 and "/admin/login" in anonymous.headers["Location"]
    intruder = User(tenant_id=other_tenant[0].id, email="intruder@other.dev", role=Role.OWNER)
    intruder.set_password("another-correct-horse")
    db.session.add(intruder)
    db.session.commit()
    client.post("/admin/login", data={"email": "intruder@other.dev", "password": "another-correct-horse"})
    assert client.post(f"/staff/inspections/{inspection.id}/draft", json={"notes": "x"}).status_code == 404


def test_one_tap_completes_the_inspection_and_checks_the_customer_in(client, parking, open_inspection):
    booking, inspection, owner = open_inspection
    _add_all_photos(parking, inspection, owner)
    login(client)
    response = client.post(f"/staff/inspections/{inspection.id}/complete", data={
        "then": "next", "fuel_level": "60", "odometer_km": "48200", "key_count": "1", "no_damage_confirmed": "yes",
        "vehicle_plate": "D-AK 2026"})
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/staff/bookings/{booking.id}")
    assert db.session.get(Inspection, inspection.id).status == "completed"
    assert db.session.get(Booking, booking.id).status == "checked_in"


def test_without_the_one_tap_flag_the_booking_is_not_moved(client, parking, open_inspection):
    booking, inspection, owner = open_inspection
    _add_all_photos(parking, inspection, owner)
    login(client)
    client.post(f"/staff/inspections/{inspection.id}/complete", data={
        "fuel_level": "60", "odometer_km": "48200", "key_count": "1", "no_damage_confirmed": "yes", "vehicle_plate": "D-AK 2026"})
    assert db.session.get(Inspection, inspection.id).status == "completed"
    assert db.session.get(Booking, booking.id).status == "reserved"


def test_refused_completion_keeps_the_inspection_open_and_the_booking_unchanged(client, parking, open_inspection):
    booking, inspection, owner = open_inspection  # no photos, no signature
    login(client)
    client.post(f"/staff/inspections/{inspection.id}/complete", data={"then": "next", "fuel_level": "60", "odometer_km": "1", "key_count": "1",
                                                                       "no_damage_confirmed": "yes"})
    assert db.session.get(Inspection, inspection.id).status == "in_progress"
    assert db.session.get(Booking, booking.id).status == "reserved"
