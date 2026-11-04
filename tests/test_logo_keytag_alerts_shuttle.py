"""Logo on invoices, printable key tag, shuttle bundle pricing (2 persons included) and the notification centre."""
import io
import json
from decimal import Decimal

import pytest
from PIL import Image

from app.extensions import db
from app.models import Alert, Booking, Extra, Role, Tenant, User
from app.services.alerts import alert_text, stats
from app.services.booking_admin import add_extra, extra_cost, validate_persons
from app.services.bookings import BookingServiceError, cancel_booking, create_payment_record, update_payment_status
from app.services.invoices import booking_lines, invoice_pdf, sync_invoices
from app.services.key_tags import build_key_tag_pdf, tag_facts
from app.services.logo import process_logo
from tests.booking_helpers import make_booking, seed_tenant
from tests.conftest import login


def _png(size=(200, 120), colour=(20, 56, 92, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, colour).save(out, "PNG")
    return out.getvalue()


class _Upload:
    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)

    def read(self, size=-1):
        return self._buffer.read(size)


def _shuttle(booking, price="20.00", included=2, extra_price="5.00"):
    row = Extra(tenant_id=booking.tenant_id, lot_id=booking.lot_id, kind="shuttle", name="Airport shuttle", price=Decimal(price),
                per_person=True, included_persons=included, extra_person_price=None if extra_price is None else Decimal(extra_price))
    db.session.add(row)
    db.session.flush()
    return row


# ---- shuttle: 2 persons included ---------------------------------------------------------------

def test_shuttle_price_includes_two_persons_and_charges_each_additional_one(app):
    booking = make_booking(total="100.00")
    shuttle = _shuttle(booking)
    assert [extra_cost(shuttle, n) for n in (1, 2, 3, 4)] == [Decimal("20.00"), Decimal("20.00"), Decimal("25.00"), Decimal("30.00")]
    row = add_extra(booking, shuttle, "LH123", 4)
    assert row.included_persons == 2 and row.additional_persons == 2 and row.total() == Decimal("30.00")
    assert booking.total_amount == Decimal("130.00")
    shuttle.price = Decimal("99.00")  # later price edits never change a booking
    assert row.total() == Decimal("30.00")


def test_more_than_the_included_persons_needs_a_price(app):
    booking = make_booking()
    shuttle = _shuttle(booking, extra_price=None)
    validate_persons(shuttle, 2)  # fine
    with pytest.raises(BookingServiceError, match="Only 2 persons are included"):
        validate_persons(shuttle, 3)
    with pytest.raises(BookingServiceError):
        add_extra(booking, shuttle, "", 3)


def test_invoice_shows_the_bundle_and_the_additional_persons_as_lines(app):
    booking = make_booking(total="100.00")
    add_extra(booking, _shuttle(booking), "", 4)
    db.session.commit()
    lines = booking_lines(booking)
    kinds = [line["kind"] for line in lines]
    assert kinds == ["parking", "extra_bundle", "extra_more"]
    more = lines[2]
    assert more["quantity"] == 2 and Decimal(more["unit"]) == Decimal("5.00") and Decimal(more["total"]) == Decimal("10.00")
    assert sum(Decimal(line["total"]) for line in lines) == Decimal(booking.total_amount)
    assert lines[0]["days"] >= 1


def test_owner_form_saves_bundle_settings(client, app):
    login(client)
    from app.models import Lot
    lot_id = Lot.query.filter_by(tenant_id=seed_tenant().id).first().id
    response = client.post("/admin/extras", data={"lot_id": lot_id, "kind": "shuttle", "name": "Shuttle X", "price": "18", "per_person": "1",
                                                   "included_persons": "2", "extra_person_price": "6.50", "active": "1"})
    assert response.status_code == 302
    saved = Extra.query.filter_by(name="Shuttle X").one()
    assert saved.included_persons == 2 and saved.extra_person_price == Decimal("6.50")
    bad = client.post("/admin/extras", data={"lot_id": lot_id, "kind": "shuttle", "name": "Shuttle Y", "price": "18", "per_person": "1", "included_persons": "abc"})
    assert Extra.query.filter_by(name="Shuttle Y").count() == 0 and bad.status_code in (200, 302)


# ---- logo on invoices --------------------------------------------------------------------------

def test_logo_is_validated_and_reencoded():
    data, mime = process_logo(_Upload(_png((1200, 800))))
    image = Image.open(io.BytesIO(data))
    assert mime == "image/png" and max(image.size) <= 600
    with pytest.raises(ValueError):
        process_logo(_Upload(b"this is not an image"))
    with pytest.raises(ValueError):
        process_logo(_Upload(b"x" * 1_000_001))
    with pytest.raises(ValueError):
        process_logo(_Upload(b""))


def test_uploaded_logo_appears_on_the_invoice_pdf(client, app):
    login(client)
    booking = make_booking()
    bid = booking.id
    with_logo = client.post("/admin/brand", data={"primary_color": "#14385C", "accent_color": "#C9A24B", "custom_domain": "", "logo_url": "",
                                                   "logo_file": (io.BytesIO(_png()), "logo.png")}, content_type="multipart/form-data")
    assert with_logo.status_code == 302
    db.session.expire_all()
    tenant = seed_tenant()
    assert tenant.logo_data and client.get("/admin/brand/logo.png").mimetype == "image/png"
    pdf = client.get(f"/admin/bookings/{bid}/invoice.pdf").data
    assert b"/Subtype /Image" in pdf
    client.post("/admin/brand", data={"primary_color": "#14385C", "accent_color": "#C9A24B", "custom_domain": "", "logo_url": "", "remove_logo": "1"})
    db.session.expire_all()
    assert seed_tenant().logo_data is None
    assert b"/Subtype /Image" not in client.get(f"/admin/bookings/{bid}/invoice.pdf").data


def test_invalid_logo_upload_is_refused_without_saving(client, app):
    login(client)
    response = client.post("/admin/brand", data={"primary_color": "#14385C", "accent_color": "#C9A24B", "custom_domain": "", "logo_url": "",
                                                  "logo_file": (io.BytesIO(b"nope"), "logo.png")}, content_type="multipart/form-data")
    assert response.status_code == 200 and b"not a valid image" in response.data
    db.session.expire_all()
    assert seed_tenant().logo_data is None


# ---- generating the invoice by hand ------------------------------------------------------------

def test_owner_can_generate_and_download_the_invoice_from_the_booking(client, app):
    booking = make_booking()
    bid = booking.id
    login(client)
    page = client.get(f"/admin/bookings/{bid}").get_data(as_text=True)
    assert "Generate invoice (PDF)" in page and "Print key tag" in page
    pdf = client.get(f"/admin/bookings/{bid}/invoice.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    assert "Download invoice (PDF)" in client.get(f"/admin/bookings/{bid}").get_data(as_text=True)


# ---- key tag ------------------------------------------------------------------------------------

def test_key_tag_has_the_car_facts_but_no_phone_or_email(app):
    booking = make_booking(phone="+4915112345678", email="ana@example.com")
    tenant = seed_tenant()
    facts = tag_facts(booking, tenant, "en")
    flat = " ".join(f"{label} {value}" for label, value in facts["rows"]) + facts["plate"] + facts["car"]
    assert "D-AB 1" in facts["plate"] and booking.space.name in flat and "Muster" in flat
    assert "+4915112345678" not in flat and "ana@example.com" not in flat
    assert build_key_tag_pdf(booking)[:4] == b"%PDF"


def test_key_tag_downloads_for_owner_and_staff(client, app, staff):
    booking = make_booking()
    bid = booking.id
    login(client)
    owner = client.get(f"/admin/bookings/{bid}/key-tag.pdf")
    assert owner.status_code == 200 and owner.mimetype == "application/pdf" and "inline" in owner.headers["Content-Disposition"]
    client.post("/admin/logout")
    login(client, "staff@test.dev", "staff-pass")
    assert client.get(f"/staff/bookings/{bid}/key-tag.pdf").data[:4] == b"%PDF"
    assert b"key-tag.pdf" in client.get(f"/staff/bookings/{bid}").data


# ---- notification centre -----------------------------------------------------------------------

def _pay(booking):
    payment = create_payment_record(tenant_id=booking.tenant_id, booking_id=booking.id, provider="manual",
                                    amount=booking.total_amount, currency=booking.currency)
    update_payment_status(tenant_id=booking.tenant_id, payment_id=payment.id, status="paid")


def test_booking_events_create_alerts(app):
    booking = make_booking()
    _pay(booking)
    cancel_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
    kinds = [a.kind for a in Alert.query.order_by(Alert.id).all()]
    assert kinds == ["booking_created", "booking_paid", "booking_cancelled"]
    created = Alert.query.filter_by(kind="booking_created").one()
    assert created.data["ref"] == booking.booking_ref and created.data["customer"] == "Ana Muster"


def test_alert_sentences_follow_the_language(client, app):
    make_booking()
    alert = Alert.query.one()
    with app.test_request_context("/"):
        assert alert_text(alert).startswith("New booking")
    client.set_cookie("pp_ui_lang", "de")
    login(client)
    assert "Neue Buchung" in client.get("/alerts").get_data(as_text=True)


def test_feed_unread_and_mark_read(client, app):
    first = make_booking(space_index=0)
    login(client)
    data = client.get("/alerts/feed?after=0").get_json()
    assert len(data["alerts"]) == 1 and data["unread"] == 1 and data["alerts"][0]["kind"] == "booking_created"
    latest = data["latest"]
    assert client.get(f"/alerts/feed?after={latest}").get_json()["alerts"] == []
    assert client.post("/alerts/read").get_json()["unread"] == 0
    make_booking(space_index=1, email="b@example.com")
    data = client.get(f"/alerts/feed?after={latest}").get_json()
    assert [a["kind"] for a in data["alerts"]] == ["booking_created"] and data["unread"] == 1 and data["latest"] > latest
    assert data["alerts"][0]["url"].startswith("/admin/bookings/")
    assert first.booking_ref in client.get("/alerts").get_data(as_text=True)


def test_bell_sound_script_and_centre_graphics_are_on_the_pages(client, app):
    make_booking()
    login(client)
    page = client.get("/admin/invoices").get_data(as_text=True)
    assert "data-alerts" in page and "alerts.js" in page and "Notifications" in page
    centre = client.get("/alerts").get_data(as_text=True)
    assert "<svg" in centre and "bar-chart" in centre and "donut" in centre and "Notification centre" in centre
    script = client.get("/static/js/alerts.js").get_data(as_text=True)
    assert "AudioContext" in script and "booking_created" in script


def test_staff_get_the_bell_and_the_centre_but_platform_owner_does_not(client, app, staff):
    make_booking()
    login(client, "staff@test.dev", "staff-pass")
    assert client.get("/alerts").status_code == 200 and client.get("/alerts/feed").status_code == 200
    assert "data-alerts" in client.get("/staff/").get_data(as_text=True)
    client.post("/admin/logout")
    user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
    user.set_password("platform-password")
    db.session.add(user)
    db.session.commit()
    client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})
    assert client.get("/alerts/feed").status_code == 403


def test_alerts_are_private_to_their_tenant(client, app, other_tenant):
    make_booking()
    other_user = User(tenant_id=other_tenant[0].id, email="o@test.dev", role=Role.OWNER)
    other_user.set_password("other-password-1")
    db.session.add(other_user)
    db.session.commit()
    client.post("/admin/login", data={"email": "o@test.dev", "password": "other-password-1"})
    assert client.get("/alerts/feed?after=0").get_json()["alerts"] == []


def test_centre_numbers(app):
    make_booking(hours=5, space_index=0)
    make_booking(hours=96, space_index=1, email="b@example.com")
    numbers = stats(seed_tenant())
    assert numbers["today"] == 2 and numbers["arriving"] == 1 and numbers["total_bookings"] == 2
    assert len(numbers["bars"]) == 14 and sum(b["count"] for b in numbers["bars"]) == 2
    assert round(sum(s["length"] for s in numbers["slices"])) == 100


def test_unread_resets_per_user(client, app, staff):
    make_booking()
    login(client)
    client.post("/alerts/read")
    client.post("/admin/logout")
    login(client, "staff@test.dev", "staff-pass")
    assert client.get("/alerts/feed").get_json()["unread"] == 1  # the owner's reading does not mark the staff member's
