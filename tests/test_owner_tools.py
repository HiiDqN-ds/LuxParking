"""Owner booking management, extras at checkout, ticket documents, staff tasks."""
import io
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Booking, BookingExtra, Extra, Lot, PriceRule, Role, Space, Tenant, User
from tests.conftest import login


def _setup(app):
    tenant = Tenant.query.first()
    lot = Lot.query.filter_by(tenant_id=tenant.id).first()
    spaces = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).limit(2).all()
    db.session.add(PriceRule(tenant_id=tenant.id, lot_id=lot.id, name="Base", period="day", mode="per_day", amount=10))
    shuttle = Extra(tenant_id=tenant.id, lot_id=lot.id, kind="shuttle", name="Airport shuttle", price=Decimal("12.50"))
    db.session.add(shuttle)
    db.session.commit()
    return tenant, lot, spaces, shuttle


def _when(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)


def test_checkout_with_extras_pdf_qr_and_email_attachment(client, app, monkeypatch):
    from app.services.bookings import create_hold
    with app.app_context():
        tenant, lot, spaces, shuttle = _setup(app)
        start = _when(72)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=spaces[0].id, customer_email="a@example.com",
                           pickup_at=start, return_at=start + timedelta(days=2))
        base = f"/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
        shuttle_id, slug = shuttle.id, tenant.slug
    page = client.get(base)
    assert b"Airport shuttle" in page.data and b"checkout-extras.js" in page.data
    sent = {}
    monkeypatch.setattr("app.blueprints.checkout.send_booking_confirmation", lambda *a, **k: sent.setdefault("called", True))
    resp = client.post(base + "/book", data={
        "first_name": "Ana", "email": "a@example.com", "phone": "+49123", "vehicle_plate": "D-AB 1", "payment_method": "arrival",
        "accept_terms": "1", "extra": [str(shuttle_id)], f"extra_details_{shuttle_id}": "LH123, 2 pax"})
    assert resp.status_code == 302 and sent.get("called")
    page_url = resp.headers["Location"].split("?")[0]
    token = page_url.rsplit("/", 1)[1]
    html = client.get(page_url).data
    assert b"LH123" in html and b"qr-dialog" in html and b"ticket.pdf" in html
    with app.app_context():
        booking = Booking.query.one()
        assert booking.total_amount == Decimal("62.50")  # EUR 50 parking + 12.50 shuttle
    pdf = client.get(f"/booking/{token}/ticket.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF" and "attachment" in pdf.headers["Content-Disposition"]
    png = client.get(f"/booking/{token}/qr.png")
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from PIL import Image
    gray = np.array(Image.open(io.BytesIO(png.data)).convert("L"))
    small = cv2.resize(gray, (240, 240), interpolation=cv2.INTER_AREA)  # simulates a phone camera far away
    ref = re.search(rb'id="booking-ref">([^<]+)<', html).group(1).decode()
    for image in (gray, small):
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
        assert decoded.endswith(f"/t/{slug}/{ref}"), decoded


def test_qr_link_opens_ticket_for_customers_and_booking_for_staff(client, app):
    from app.services.bookings import create_booking_from_hold, create_hold
    with app.app_context():
        tenant, lot, spaces, _ = _setup(app)
        start = _when(72)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=spaces[0].id, customer_email="q@example.com", pickup_at=start, return_at=start + timedelta(days=1))
        booking = create_booking_from_hold(tenant_id=tenant.id, hold_id=hold.id, first_name="Q", email="q@example.com", phone="1", total_amount=10, currency="EUR", vehicle_plate="Q-1")
        link, bid, ref = f"/t/{tenant.slug}/{booking.booking_ref}", booking.id, booking.booking_ref
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", ref)
    assert client.get(f"/t/{tenant.slug}/NOPE-NOPE-NOPE").status_code == 404
    first = client.get(link)
    assert first.status_code == 302 and "/booking/" in first.headers["Location"]
    assert client.get(first.headers["Location"]).status_code == 200
    login(client)
    staff = client.get(link.lower())
    assert staff.status_code == 302 and staff.headers["Location"].endswith(f"/staff/bookings/{bid}")
    for scanned in (f"https://luxuryparking.de{link}", ref, ref.lower()):  # QR scan, typed number, typed in lower case
        found = client.post("/staff/lookup", data={"booking_code": scanned})
        assert found.status_code == 302 and found.headers["Location"].endswith(f"/staff/bookings/{bid}"), scanned


def test_email_carries_pdf_attachment(app, monkeypatch):
    from app.services.bookings import create_booking_from_hold, create_hold
    from app.services import booking_notifications as bn
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="from@test.dev", SITE_URL="https://luxuryparking.de")
    with app.app_context():
        tenant, lot, spaces, shuttle = _setup(app)
        start = _when(72)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=spaces[0].id, customer_email="c@example.com",
                           pickup_at=start, return_at=start + timedelta(days=1))
        booking = create_booking_from_hold(tenant_id=tenant.id, hold_id=hold.id, first_name="C", email="c@example.com",
                                           phone="1", total_amount=10, currency="EUR", vehicle_plate="X-1", locale="de")
        captured = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): pass
        monkeypatch.setattr(bn.smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(bn, "_authenticate_and_send", lambda smtp, message: captured.setdefault("m", message))
        with app.test_request_context("/"):
            bn.send_booking_confirmation(booking.id, tenant.id)
        message = captured["m"]
        pdfs = [p for p in message.iter_attachments() if p.get_content_type() == "application/pdf"]
        # the ticket and the invoice (Rechnung) both travel with the confirmation
        assert len(pdfs) == 2 and all(p.get_content()[:4] == b"%PDF" for p in pdfs)
        assert {p.get_filename().split("-")[0] for p in pdfs} == {"booking", "invoice"}
        assert "/booking/" in message.get_body().get_content()


def test_owner_extras_crud_and_manual_booking_crud(client, app):
    with app.app_context():
        tenant, lot, spaces, shuttle = _setup(app)
        lot_id, space_ids = lot.id, [s.id for s in spaces]
    login(client)
    created = client.post("/admin/extras", data={"lot_id": lot_id, "kind": "cleaning", "name": "Interior cleaning", "price": "25", "active": "1"})
    assert created.status_code == 302
    with app.app_context():
        cleaning = Extra.query.filter_by(name="Interior cleaning").one()
        cid = cleaning.id
    assert client.post(f"/admin/extras/{cid}/edit", data={"kind": "cleaning", "name": "Full cleaning", "price": "30", "active": "1"}).status_code == 302
    assert b"Full cleaning" in client.get("/admin/extras").data
    start, end = _when(100), _when(148)
    form = {"space_id": space_ids[0], "pickup_at": start.strftime("%Y-%m-%dT%H:%M"), "return_at": end.strftime("%Y-%m-%dT%H:%M"),
            "first_name": "Max", "last_name": "Muster", "email": "max@example.com", "phone": "1", "vehicle_plate": "D-MM 1", "price": "1.00",
            f"extra_{cid}": "on", "payment_status": "pending"}
    r = client.post("/admin/bookings/new", data=form)
    assert r.status_code == 302, r.data[:400]
    with app.app_context():
        booking = Booking.query.one()
        bid, total = booking.id, booking.total_amount
        assert total == Decimal("80.00") and len(booking.extras) == 1  # EUR 50 parking + 30 cleaning
    detail = client.get(f"/admin/bookings/{bid}")
    assert detail.status_code == 200 and b"Full cleaning" in detail.data
    client.post(f"/admin/bookings/{bid}/details", data={"first_name": "Maximilian", "last_name": "Muster", "email": "max@example.com", "vehicle_plate": "D-MM 2", "notes": "VIP"})
    # a second booking on the other space cannot be moved onto the first one's dates/space
    form2 = dict(form, space_id=space_ids[1], vehicle_plate="D-XX 9", email="x@example.com")
    form2.pop(f"extra_{cid}")
    client.post("/admin/bookings/new", data=form2)
    with app.app_context():
        second = Booking.query.filter(Booking.id != bid).one().id
    moved = client.post(f"/admin/bookings/{second}/reschedule", data={"space_id": space_ids[0], "pickup_at": form["pickup_at"], "return_at": form["return_at"]}, follow_redirects=True)
    assert b"already booked" in moved.data
    assert client.post(f"/admin/bookings/{bid}/payment", data={"status": "paid"}).status_code == 302
    with app.app_context():
        assert db.session.get(Booking, bid).payment_status == "paid" and db.session.get(Booking, bid).customer.first_name == "Maximilian"
    blocked = client.post(f"/admin/bookings/{bid}/delete", follow_redirects=True)
    assert b"Only unpaid" in blocked.data
    client.post(f"/admin/bookings/{second}/action/cancel")
    gone = client.post(f"/admin/bookings/{second}/delete", follow_redirects=True)
    assert b"deleted" in gone.data
    with app.app_context():
        assert db.session.get(Booking, second) is None
    assert client.post(f"/admin/extras/{cid}/delete").status_code == 302


def test_staff_sees_extra_tasks_and_has_navigation(client, app):
    from app.services.bookings import create_booking_from_hold, create_hold
    from app.services.booking_admin import add_extra
    with app.app_context():
        tenant, lot, spaces, shuttle = _setup(app)
        start = _when(30)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=spaces[0].id, customer_email="s@example.com", pickup_at=start, return_at=start + timedelta(days=1))
        booking = create_booking_from_hold(tenant_id=tenant.id, hold_id=hold.id, first_name="S", email="s@example.com", phone="1", total_amount=10, currency="EUR", vehicle_plate="D-ST 1")
        add_extra(booking, shuttle, "Terminal 1")
        user = User(tenant_id=tenant.id, email="worker@test.dev", name="Worker", role=Role.STAFF)
        user.set_password("worker-password")
        db.session.add(user)
        db.session.commit()
        bid = booking.id
    client.post("/admin/login", data={"email": "worker@test.dev", "password": "worker-password"})
    home = client.get("/staff/")
    assert home.status_code == 200 and b"Open extras" in home.data and b"staff-tabbar" in home.data
    page = client.get(f"/staff/bookings/{bid}")
    assert b"Next step" in page.data and b"staff-back" in page.data and b"Terminal 1" in page.data
    with app.app_context():
        row = BookingExtra.query.one().id
    assert client.post(f"/staff/bookings/{bid}/extras/{row}/done").status_code == 302
    with app.app_context():
        assert BookingExtra.query.one().status == "done"
    assert client.get("/admin/platform/api").status_code == 403 and client.get("/admin/extras").status_code == 403


def test_customer_chooses_one_shuttle_and_one_cleaning_option(client, app):
    from app.seed import ensure_default_extras
    from app.services.bookings import create_hold
    with app.app_context():
        tenant = Tenant.query.first()
        lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        db.session.add(PriceRule(tenant_id=tenant.id, lot_id=lot.id, name="Base", period="day", mode="per_day", amount=10))
        db.session.commit()
        assert ensure_default_extras() == 5 and ensure_default_extras() == 0  # starter set, only once
        ids = {e.name: e.id for e in Extra.query.all()}
        priority, standard = ids["Shuttle Priority"], ids["Shuttle Standard"]
        valet = next(i for n, i in ids.items() if n.startswith("Komplettpflege"))
        start = _when(72)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=space.id, customer_email="e@example.com",
                           pickup_at=start, return_at=start + timedelta(days=2))
        base = f"/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    page = client.get(base)
    assert page.data.count(b'name="extra_shuttle"') == 3 and page.data.count(b'name="extra_cleaning"') == 4  # incl. "No thanks"
    resp = client.post(base + "/book", data={
        "first_name": "E", "email": "e@example.com", "phone": "1", "vehicle_plate": "E-1", "payment_method": "arrival", "accept_terms": "1",
        "extra_shuttle": str(priority), "extra": [str(standard)], "extra_cleaning": str(valet), "extra_details_cleaning": "red car"})
    assert resp.status_code == 302
    with app.app_context():
        booking = Booking.query.one()
        names = sorted(e.name for e in booking.extras)
        assert names == ["Komplettpflege · Full valet", "Shuttle Priority"]  # the visible choice wins; one per kind
        assert booking.total_amount == Decimal("50.00") + sum(e.unit_price for e in booking.extras)


def test_missing_email_server_is_calm_for_the_customer(client, app):
    from app.services.bookings import create_hold
    app.config.update(SMTP_HOST=None, SMTP_FROM=None)
    with app.app_context():
        tenant, lot, spaces, _ = _setup(app)
        start = _when(72)
        hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=spaces[0].id, customer_email="n@example.com", pickup_at=start, return_at=start + timedelta(days=1))
        base = f"/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    resp = client.post(base + "/book", data={"first_name": "N", "email": "n@example.com", "phone": "1", "vehicle_plate": "N-1", "payment_method": "arrival", "accept_terms": "1"})
    page = client.get(resp.headers["Location"], follow_redirects=True)
    assert page.status_code == 200 and b"konnte nicht gesendet" not in page.data and b"could not be sent" not in page.data
