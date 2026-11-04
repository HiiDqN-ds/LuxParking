"""Customer notifications for booking events (reserved, paid, cancelled, refunded, checked_in, checked_out, reminder).

One entry point, `notify_booking_event`, is called after a booking changes state. It
  1. brings the invoice documents up to date (issue, mark paid, credit note),
  2. emails the customer in their language with the right PDF attached (ticket, invoice or credit note),
  3. optionally sends an SMS and/or WhatsApp message (Twilio),
  4. records every attempt in the `notifications` table, which also stops the same event being sent twice.

It never raises: a failing mail server must not break a booking, a payment webhook or a check-in.
Set `g.suppress_booking_notifications = True` to silence it (the owner back office does this while creating a booking).
"""
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g, has_request_context, url_for
from sqlalchemy import select

from ..extensions import db
from ..i18n import translate
from ..models import Booking, Notification, Tenant
from .booking_access import create_customer_booking_token
from .booking_notifications import (
    BookingEmailError, EmailNotConfigured, log_notification, send_booking_confirmation, smtp_send,
)
from .invoices import format_money, invoice_filename, invoice_pdf, latest_invoice, sync_invoices
from .messaging import MessagingError, MessagingNotConfigured, channel_configured, normalize_phone, send_message

EVENTS = ("reserved", "paid", "cancelled", "refunded", "checked_in", "checked_out", "reminder")
INVOICE_EVENTS = {"reserved", "paid", "cancelled", "refunded", "checked_out"}
ATTACHMENTS = {
    "reserved": ("invoice", "ticket"), "paid": ("invoice",), "refunded": ("credit",),
    "checked_out": ("invoice",), "reminder": ("ticket",), "cancelled": (), "checked_in": (),
}
OWNER_COPY = {"reserved", "paid", "cancelled", "refunded"}
INVOICE_COPY = {"reserved", "paid", "refunded", "checked_out"}  # also sent to the billing address when it differs


# ---- helpers -------------------------------------------------------------------------------------

def _zone(tenant: Tenant):
    try:
        return ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def local_text(value: datetime, tenant: Tenant) -> str:
    return _aware(value).astimezone(_zone(tenant)).strftime("%d.%m.%Y %H:%M")


def customer_url(booking: Booking, locale: str) -> str:
    """Link to the customer's booking page; also works outside a request (reminders run from a cron job)."""
    token = create_customer_booking_token(tenant_id=booking.tenant_id, booking_ref=booking.booking_ref)

    def build():
        return url_for("checkout.localized_booking_page", locale=locale, token=token, _external=True)

    if has_request_context():
        return build()
    base = current_app.config.get("SITE_URL") or "http://localhost"
    with current_app.test_request_context("/", base_url=base):
        return build()


def directions_url(booking: Booking) -> str:
    lot = booking.lot
    if lot.lat is not None and lot.lng is not None:
        return f"https://www.google.com/maps/dir/?api=1&destination={lot.lat},{lot.lng}"
    return "https://www.google.com/maps/search/?api=1&query=" + quote(f"{lot.name} {lot.address or ''}".strip())


def already_sent(tenant_id: int, booking_id: int, event: str, channel: str) -> bool:
    return db.session.execute(
        select(Notification.id).where(
            Notification.tenant_id == tenant_id, Notification.booking_id == booking_id,
            Notification.event == event, Notification.channel == channel, Notification.status == "sent",
        ).limit(1)
    ).first() is not None


# ---- email ---------------------------------------------------------------------------------------

def build_email(booking: Booking, tenant: Tenant, event: str) -> EmailMessage:
    customer = booking.customer
    locale = customer.locale or tenant.default_locale
    t = lambda key, **values: translate(locale, key, **values)  # noqa: E731
    ref = booking.booking_ref
    url = customer_url(booking, locale)
    total = format_money(booking.total_amount, booking.currency, locale)
    now_local = local_text(datetime.now(timezone.utc), tenant)

    paragraphs: list[str] = []
    if event == "paid":
        paragraphs.append(t("notify.paid.body", total=total))
    elif event == "cancelled":
        paragraphs.append(t("notify.cancelled.body", booking=ref))
        if booking.payment_status in ("paid", "refunded"):
            paragraphs.append(t("notify.cancelled.refund"))
    elif event == "refunded":
        paragraphs.append(t("notify.refunded.body", total=total, booking=ref))
    elif event == "checked_in":
        plate = booking.vehicle.plate if booking.vehicle is not None else ""
        paragraphs.append(t("notify.checked_in.body", plate=plate or "-", time=now_local, returned=local_text(booking.return_at, tenant)))
    elif event == "checked_out":
        paragraphs.append(t("notify.checked_out.body", time=now_local, booking=ref))
        review = current_app.config.get("REVIEW_URL")
        if review:
            paragraphs.append(t("notify.checked_out.review", url=review))
    elif event == "reminder":
        paragraphs.append(t("notify.reminder.body", booking=ref, pickup=local_text(booking.pickup_at, tenant), directions=directions_url(booking)))
    else:
        raise ValueError(f"Unknown booking event: {event}")

    lines = [t("checkout.email.greeting", name=customer.first_name), ""]
    for paragraph in paragraphs:
        lines += [paragraph, ""]
    lines += [
        t("checkout.email.reference", booking=ref),
        t("checkout.email.dates", pickup=local_text(booking.pickup_at, tenant), returned=local_text(booking.return_at, tenant)),
        t("checkout.email.total", total=total),
        t("checkout.email.link", url=url),
        "",
        t("checkout.email.policy"),
    ]
    message = EmailMessage()
    message["Subject"] = t(f"notify.{event}.subject", booking=ref)
    message["From"] = current_app.config["SMTP_FROM"]
    message["To"] = customer.email
    billing = (customer.billing_email or "").strip()
    if event in INVOICE_COPY and billing and billing.casefold() != customer.email.casefold():
        message["Cc"] = billing
    owner_copy = current_app.config.get("OWNER_NOTIFY_EMAIL")
    if event in OWNER_COPY and owner_copy:
        message["Bcc"] = owner_copy
    message.set_content("\n".join(lines))

    for kind in ATTACHMENTS.get(event, ()):
        try:
            if kind == "ticket":
                from .booking_documents import build_pdf_for_booking
                message.add_attachment(build_pdf_for_booking(booking, locale), maintype="application", subtype="pdf",
                                       filename=f"booking-{ref}.pdf")
            else:
                document = latest_invoice(booking, "credit_note" if kind == "credit" else "invoice")
                if document is not None and document.status != "cancelled":
                    message.add_attachment(invoice_pdf(document), maintype="application", subtype="pdf", filename=invoice_filename(document))
        except Exception:  # the email is still useful without the attachment
            current_app.logger.exception("Could not attach %s for booking %s", kind, booking.id)
    return message


def _deliver_email(booking: Booking, tenant: Tenant, event: str) -> str:
    if event == "reserved":  # the original confirmation keeps its own once-only guard and logs itself
        try:
            send_booking_confirmation(booking.id, booking.tenant_id)
            return "sent"
        except EmailNotConfigured:
            return "skipped"
        except BookingEmailError:
            return "failed"
    if not current_app.config.get("SMTP_HOST") or not current_app.config.get("SMTP_FROM"):
        log_notification(booking, "email", event, booking.customer.email, "skipped", "SMTP is not configured")
        return "skipped"
    try:
        message = build_email(booking, tenant, event)
        smtp_send(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        current_app.logger.exception("Could not send the %s email for booking %s", event, booking.id)
        log_notification(booking, "email", event, booking.customer.email, "failed", str(exc))
        return "failed"
    log_notification(booking, "email", event, booking.customer.email, "sent")
    return "sent"


# ---- SMS / WhatsApp ------------------------------------------------------------------------------

def _deliver_message(booking: Booking, tenant: Tenant, event: str, channel: str) -> str:
    allowed = current_app.config.get("SMS_EVENTS" if channel == "sms" else "WHATSAPP_EVENTS", ())
    if event not in allowed or not channel_configured(channel):
        return "not_enabled"
    customer = booking.customer
    locale = customer.locale or tenant.default_locale
    key = f"sms.{event}"
    pickup = local_text(booking.pickup_at, tenant)
    body = translate(locale, key, brand=tenant.name, booking=booking.booking_ref, pickup=pickup, url=customer_url(booking, locale))
    if body == key:  # no text defined for this event
        return "not_enabled"
    number = normalize_phone(customer.phone)
    if number is None:
        log_notification(booking, channel, event, customer.phone or "", "skipped", "phone number is not in international format")
        return "skipped"
    try:
        send_message(channel, number, body)
    except MessagingNotConfigured:
        return "not_enabled"
    except MessagingError as exc:
        current_app.logger.warning("%s for booking %s failed: %s", channel, booking.id, exc)
        log_notification(booking, channel, event, number, "failed", str(exc))
        return "failed"
    log_notification(booking, channel, event, number, "sent")
    return "sent"


# ---- entry points --------------------------------------------------------------------------------

def notify_booking_event(booking_id: int, tenant_id: int, event: str, *, channels=("email", "sms", "whatsapp"), force: bool = False) -> dict:
    """Send the notifications for one booking event. Returns {channel: result}; never raises."""
    results: dict[str, str] = {}
    if event not in EVENTS or g.get("suppress_booking_notifications"):
        return results
    previous_public_tenant = g.get("public_tenant_id")
    g.public_tenant_id = tenant_id  # tenant text overrides apply to the customer's emails too
    try:
        booking = db.session.execute(select(Booking).where(Booking.id == booking_id, Booking.tenant_id == tenant_id)).scalar_one_or_none()
        tenant = db.session.get(Tenant, tenant_id)
        if booking is None or tenant is None:
            return results
        if event in INVOICE_EVENTS:
            try:
                sync_invoices(booking)
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Could not update the invoice of booking %s", booking_id)
        for channel in channels:
            try:
                if not force and already_sent(tenant_id, booking.id, event, channel):
                    results[channel] = "duplicate"
                elif channel == "email":
                    results[channel] = _deliver_email(booking, tenant, event)
                else:
                    results[channel] = _deliver_message(booking, tenant, event, channel)
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Notification %s/%s failed for booking %s", event, channel, booking_id)
                results[channel] = "error"
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not prepare notifications for booking %s", booking_id)
    finally:
        g.public_tenant_id = previous_public_tenant
    return results


def send_due_reminders(now: datetime | None = None, limit: int = 500) -> int:
    """Remind customers whose pick-up is within REMINDER_HOURS. Run from cron; each booking is reminded once."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=current_app.config.get("REMINDER_HOURS", 24))
    rows = db.session.execute(
        select(Booking.id, Booking.tenant_id).where(Booking.status == "reserved", Booking.pickup_at > now, Booking.pickup_at <= horizon)
        .order_by(Booking.pickup_at).limit(limit)
    ).all()
    sent = 0
    for booking_id, tenant_id in rows:
        result = notify_booking_event(booking_id, tenant_id, "reminder")
        sent += 1 if "sent" in result.values() else 0
    return sent
