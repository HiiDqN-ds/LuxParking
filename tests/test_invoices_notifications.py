"""Invoices (Rechnung / Gutschrift), PDF downloads and the booking-event emails, SMS and WhatsApp."""
from datetime import timedelta
from decimal import Decimal

from flask import g

from app.extensions import db
from app.models import Booking, BookingExtra, Extra, Invoice, Notification
from app.services.booking_events import notify_booking_event, send_due_reminders
from app.services.bookings import (
    cancel_booking, create_payment_record, update_payment_status,
)
from app.services.invoices import ensure_invoice, latest_invoice, split_vat, sync_invoices
from app.services.messaging import normalize_phone
from tests.booking_helpers import capture_email, make_booking, pdf_attachments, seed_tenant
from tests.conftest import login


def _pay(booking):
    payment = create_payment_record(tenant_id=booking.tenant_id, booking_id=booking.id, provider="manual",
                                    amount=booking.total_amount, currency=booking.currency)
    update_payment_status(tenant_id=booking.tenant_id, payment_id=payment.id, status="paid")
    return payment


# ---- invoices ------------------------------------------------------------------------------------

def test_vat_is_split_out_of_the_gross_price():
    assert split_vat(Decimal("119.00"), Decimal("19")) == (Decimal("100.00"), Decimal("19.00"))
    assert split_vat(Decimal("10.00"), Decimal("0")) == (Decimal("10.00"), Decimal("0.00"))


def test_invoice_numbers_are_gapless_and_amounts_add_up(app):
    first = make_booking(space_index=0)
    second = make_booking(space_index=1, email="b@example.com", total="238.00")
    a, b = sync_invoices(first), sync_invoices(second)
    db.session.commit()
    assert (a.number, b.number) == ("RE-000001", "RE-000002")
    assert (a.net_amount, a.vat_amount, a.gross_amount) == (Decimal("100.00"), Decimal("19.00"), Decimal("119.00"))
    assert sum(Decimal(line["total"]) for line in a.lines) == a.gross_amount
    assert sync_invoices(first).number == "RE-000001"  # never a second invoice for one booking


def test_open_invoice_follows_the_booking_but_a_paid_one_is_frozen(app):
    booking = make_booking(total="100.00")
    shuttle = Extra(tenant_id=booking.tenant_id, lot_id=booking.lot_id, kind="shuttle", name="Airport shuttle", price=Decimal("20.00"))
    db.session.add(shuttle)
    db.session.flush()
    invoice = ensure_invoice(booking)
    assert invoice.gross_amount == Decimal("100.00")
    db.session.add(BookingExtra(tenant_id=booking.tenant_id, booking_id=booking.id, extra_id=shuttle.id, kind="shuttle",
                                name="Airport shuttle", unit_price=Decimal("20.00"), quantity=2))
    booking.total_amount = Decimal("140.00")
    db.session.commit()
    db.session.refresh(booking)
    invoice = ensure_invoice(booking)
    assert invoice.gross_amount == Decimal("140.00") and any(line["kind"] == "extra" for line in invoice.lines)
    _pay(booking)
    db.session.expire_all()
    paid = latest_invoice(db.session.get(Booking, booking.id))
    assert paid.status == "paid" and paid.paid_at is not None
    booking = db.session.get(Booking, booking.id)
    booking.total_amount = Decimal("999.00")
    assert ensure_invoice(booking).gross_amount == Decimal("140.00")


def test_refund_issues_a_credit_note_once(app):
    booking = make_booking()
    payment = _pay(booking)
    update_payment_status(tenant_id=booking.tenant_id, payment_id=payment.id, status="refunded")
    db.session.expire_all()
    booking = db.session.get(Booking, booking.id)
    note = latest_invoice(booking, "credit_note")
    invoice = latest_invoice(booking)
    assert note is not None and note.number == "GS-000001" and note.gross_amount == -invoice.gross_amount
    assert invoice.status == "credited"
    sync_invoices(booking)
    assert Invoice.query.filter_by(kind="credit_note").count() == 1


def test_cancelling_an_unpaid_booking_cancels_its_invoice(app):
    booking = make_booking()
    sync_invoices(booking)
    db.session.commit()
    cancel_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
    db.session.expire_all()
    assert latest_invoice(db.session.get(Booking, booking.id)).status == "cancelled"


def test_invoice_pdf_downloads_for_customer_and_owner(client, app):
    from app.services.booking_access import create_customer_booking_token
    booking = make_booking()
    bid, tid, ref = booking.id, booking.tenant_id, booking.booking_ref
    token = create_customer_booking_token(tenant_id=tid, booking_ref=ref)
    page = client.get(f"/de/booking/{token}")
    assert page.status_code == 200 and b"invoice.pdf" in page.data
    pdf = client.get(f"/booking/{token}/invoice.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf" and pdf.data[:4] == b"%PDF"
    assert "invoice-RE-" in pdf.headers["Content-Disposition"]
    assert client.get(f"/admin/bookings/{bid}/invoice.pdf").status_code in (302, 401)  # owners only
    login(client)
    owner_pdf = client.get(f"/admin/bookings/{bid}/invoice.pdf")
    assert owner_pdf.status_code == 200 and owner_pdf.data[:4] == b"%PDF"
    assert b"RE-000001" in client.get("/admin/invoices").data
    assert client.get("/admin/invoices.csv").data.startswith(b"number,")
    assert client.get(f"/booking/{token}/credit-note.pdf").status_code == 404


def test_deleting_a_booking_keeps_its_invoice(client, app):
    booking = make_booking()
    bid = booking.id
    sync_invoices(booking)
    db.session.commit()
    login(client)
    assert client.post(f"/admin/bookings/{bid}/delete").status_code == 302
    db.session.expire_all()
    invoice = Invoice.query.one()
    assert invoice.booking_id is None and invoice.status == "cancelled" and invoice.number == "RE-000001"


def test_api_invoice_endpoint_needs_the_read_scope(client, app):
    from app.services.api_platform import issue_api_key
    booking = make_booking()
    reader = issue_api_key(tenant_id=booking.tenant_id, name="reader", key_type="sk", scopes=["bookings:read"], allowed_origins=[], requests_per_minute=60)[1]
    blind = issue_api_key(tenant_id=booking.tenant_id, name="blind", key_type="sk", scopes=["map:read"], allowed_origins=[], requests_per_minute=60)[1]
    ok = client.get(f"/v1/bookings/{booking.id}/invoice", headers={"Authorization": f"Bearer {reader}"})
    assert ok.status_code == 200 and ok.data[:4] == b"%PDF"
    assert client.get(f"/v1/bookings/{booking.id}/invoice", headers={"Authorization": f"Bearer {blind}"}).status_code == 403
    assert client.get(f"/v1/bookings/{booking.id}/credit-note", headers={"Authorization": f"Bearer {reader}"}).status_code == 404


# ---- emails --------------------------------------------------------------------------------------

def test_every_booking_event_emails_the_customer_with_the_right_pdf(app, monkeypatch):
    sent = capture_email(monkeypatch, app)
    booking = make_booking(locale="de")
    with app.test_request_context("/"):
        assert notify_booking_event(booking.id, booking.tenant_id, "reserved")["email"] == "sent"
        assert len(pdf_attachments(sent[-1])) == 2  # ticket + invoice
        payment = _pay(booking)
        payment_id = payment.id
        assert "Zahlung erhalten" in sent[-1]["Subject"] and len(pdf_attachments(sent[-1])) == 1
        assert pdf_attachments(sent[-1])[0].get_filename().startswith("invoice-RE-")
        update_payment_status(tenant_id=booking.tenant_id, payment_id=payment_id, status="refunded")
        assert "Erstattung" in sent[-1]["Subject"] and pdf_attachments(sent[-1])[0].get_filename().startswith("credit-note-GS-")
        assert notify_booking_event(booking.id, booking.tenant_id, "checked_in")["email"] == "sent"
        assert notify_booking_event(booking.id, booking.tenant_id, "checked_out")["email"] == "sent"
    subjects = [m["Subject"] for m in sent]
    assert any("Ihre Parkplatzbuchung" in s for s in subjects) and any("Danke" in s for s in subjects)
    assert all(m["To"] == "ana@example.com" for m in sent)


def test_cancellation_email_and_no_duplicates(app, monkeypatch):
    sent = capture_email(monkeypatch, app)
    booking = make_booking(locale="en")
    with app.test_request_context("/"):
        cancel_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
        assert [m["Subject"] for m in sent] == [f"Booking {booking.booking_ref} cancelled"]
        assert notify_booking_event(booking.id, booking.tenant_id, "cancelled")["email"] == "duplicate"
        assert notify_booking_event(booking.id, booking.tenant_id, "cancelled", force=True)["email"] == "sent"
    assert len(sent) == 2
    assert Notification.query.filter_by(event="cancelled", status="sent").count() == 2


def test_without_smtp_nothing_breaks_and_it_is_logged(app):
    booking = make_booking()
    cancel_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
    row = Notification.query.filter_by(event="cancelled", channel="email").one()
    assert row.status == "skipped" and "SMTP" in row.error


def test_failing_mail_server_never_breaks_the_booking_change(app, monkeypatch):
    capture_email(monkeypatch, app)
    def boom(message):
        raise OSError("connection refused")
    monkeypatch.setattr("app.services.booking_events.smtp_send", boom)
    booking = make_booking()
    with app.test_request_context("/"):
        cancel_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
    db.session.expire_all()
    assert db.session.get(Booking, booking.id).status == "cancelled"
    assert Notification.query.filter_by(event="cancelled").one().status == "failed"


def test_owner_can_silence_automatic_notices(app, monkeypatch):
    sent = capture_email(monkeypatch, app)
    booking = make_booking()
    with app.test_request_context("/"):
        g.suppress_booking_notifications = True
        assert notify_booking_event(booking.id, booking.tenant_id, "paid") == {}
        g.suppress_booking_notifications = False
    assert sent == []


def test_billing_address_gets_a_copy_of_invoice_mails(app, monkeypatch):
    sent = capture_email(monkeypatch, app)
    app.config.update(OWNER_NOTIFY_EMAIL="owner@test.dev")
    booking = make_booking()
    booking.customer.billing_email = "accounts@acme.example"
    db.session.commit()
    with app.test_request_context("/"):
        notify_booking_event(booking.id, booking.tenant_id, "paid", channels=("email",))
    assert sent[-1]["Cc"] == "accounts@acme.example" and sent[-1]["Bcc"] == "owner@test.dev"


# ---- SMS / WhatsApp ------------------------------------------------------------------------------

def test_phone_numbers_are_normalised_safely(app):
    with app.app_context():
        assert normalize_phone("+49 151 1234 5678") == "+4915112345678"
        assert normalize_phone("0049 151 12345678") == "+4915112345678"
        assert normalize_phone("0151 12345678") is None  # no default country configured: refuse to guess
        app.config["DEFAULT_PHONE_COUNTRY_CODE"] = "+49"
        assert normalize_phone("0151 12345678") == "+4915112345678"
        assert normalize_phone("12345") is None and normalize_phone("") is None


def test_sms_and_whatsapp_are_sent_only_when_configured(app, monkeypatch):
    booking = make_booking(locale="en")
    calls = []
    monkeypatch.setattr("app.services.booking_events.send_message", lambda channel, to, body: calls.append((channel, to, body)) or "SM1")
    with app.test_request_context("/"):
        assert notify_booking_event(booking.id, booking.tenant_id, "reserved", channels=("sms", "whatsapp")) == {"sms": "not_enabled", "whatsapp": "not_enabled"}
        app.config.update(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok", TWILIO_SMS_FROM="+15005550006", TWILIO_WHATSAPP_FROM="+15005550007",
                          SITE_URL="https://park.test")
        result = notify_booking_event(booking.id, booking.tenant_id, "reserved", channels=("sms", "whatsapp"))
    assert result == {"sms": "sent", "whatsapp": "sent"}
    assert [c[0] for c in calls] == ["sms", "whatsapp"] and all(c[1] == "+4915112345678" and booking.booking_ref in c[2] for c in calls)


def test_local_numbers_are_skipped_and_logged(app, monkeypatch):
    app.config.update(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok", TWILIO_SMS_FROM="+15005550006", SITE_URL="https://park.test")
    booking = make_booking(phone="0151 12345678")
    monkeypatch.setattr("app.services.booking_events.send_message", lambda *a: (_ for _ in ()).throw(AssertionError("must not send")))
    with app.test_request_context("/"):
        assert notify_booking_event(booking.id, booking.tenant_id, "reserved", channels=("sms",)) == {"sms": "skipped"}
    assert "international" in Notification.query.filter_by(channel="sms").one().error


# ---- reminders -----------------------------------------------------------------------------------

def test_reminders_go_out_once_and_only_for_soon_bookings(app, monkeypatch):
    sent = capture_email(monkeypatch, app)
    soon = make_booking(hours=5, space_index=0)
    later = make_booking(hours=96, space_index=1, email="later@example.com")
    with app.test_request_context("/"):
        assert send_due_reminders() == 1
        assert send_due_reminders() == 0
    reminder = [m for m in sent if "Erinnerung" in m["Subject"]]
    assert len(reminder) == 1 and soon.booking_ref in reminder[0]["Subject"] and later.booking_ref not in reminder[0]["Subject"]
    assert "maps" in reminder[0].get_body().get_content()
