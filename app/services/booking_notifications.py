"""Customer booking confirmation email ("reserved") and the notification delivery log.

Other booking events (paid, cancelled, refunded, checked in/out, reminders) are handled in `booking_events.py`,
which delegates the first confirmation to `send_booking_confirmation` below.
"""

import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

from flask import current_app

from ..extensions import db
from ..i18n import translate
from ..models import Booking, Notification
from .booking_access import create_customer_booking_token


class BookingEmailError(RuntimeError):
    """Raised when booking confirmation email cannot be delivered."""


class EmailNotConfigured(BookingEmailError):
    """No email server is set up (SMTP_HOST / SMTP_FROM). Not an error for the customer: the ticket is on the booking page."""


def log_notification(booking, channel: str, event: str, recipient: str, status: str, error: str = "") -> None:
    """Record one delivery attempt. Logging problems must never break the caller."""
    try:
        db.session.add(Notification(
            tenant_id=booking.tenant_id, booking_id=booking.id, channel=channel, event=event,
            recipient=(recipient or "")[:255], status=status, error=(error or "")[:255],
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record the %s/%s notification for booking %s", channel, event, booking.id)


def send_booking_confirmation(booking_id: int, tenant_id: int) -> None:
    from .booking_events import customer_url, local_text
    from .invoices import format_money, invoice_filename, invoice_pdf, latest_invoice, sync_invoices

    booking = db.session.execute(
        db.select(Booking).where(
            Booking.id == booking_id,
            Booking.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if booking is None:
        raise BookingEmailError("Booking not found for confirmation email.")
    if booking.confirmation_sent_at is not None:
        return
    host = current_app.config.get("SMTP_HOST")
    sender = current_app.config.get("SMTP_FROM")
    customer = booking.customer
    if not host or not sender:
        log_notification(booking, "email", "reserved", customer.email, "skipped", "SMTP is not configured")
        raise EmailNotConfigured("Booking was saved, but SMTP_HOST and SMTP_FROM are not configured.")

    from ..models import Tenant
    tenant = db.session.get(Tenant, tenant_id)
    locale = customer.locale
    try:  # the invoice is issued with the confirmation; a problem here must not stop the email
        sync_invoices(booking)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not issue the invoice for booking %s", booking.id)

    confirmation_url = customer_url(booking, locale)
    message = EmailMessage()
    message["Subject"] = translate(locale, "checkout.email.subject", booking=booking.booking_ref)
    message["From"] = sender
    message["To"] = customer.email
    billing = (customer.billing_email or "").strip()
    if billing and billing.casefold() != customer.email.casefold():
        message["Cc"] = billing
    if current_app.config.get("OWNER_NOTIFY_EMAIL"):
        message["Bcc"] = current_app.config["OWNER_NOTIFY_EMAIL"]
    message.set_content(
        "\n".join(
            (
                translate(locale, "checkout.email.greeting", name=customer.first_name),
                "",
                translate(locale, "checkout.email.reference", booking=booking.booking_ref),
                translate(locale, "checkout.email.dates", pickup=local_text(booking.pickup_at, tenant), returned=local_text(booking.return_at, tenant)),
                translate(locale, "checkout.email.total", total=format_money(booking.total_amount, booking.currency, locale)),
                translate(locale, "checkout.email.link", url=confirmation_url),
                "",
                translate(locale, "checkout.email.policy"),
            )
        )
    )

    try:
        from .booking_documents import build_pdf_for_booking
        message.add_attachment(build_pdf_for_booking(booking, locale), maintype="application", subtype="pdf",
                               filename=f"booking-{booking.booking_ref}.pdf")
    except Exception:  # the email is still useful without the attachment
        current_app.logger.exception("Could not attach the PDF ticket for booking %s", booking.id)
    try:
        invoice = latest_invoice(booking)
        if invoice is not None and invoice.status != "cancelled":
            message.add_attachment(invoice_pdf(invoice), maintype="application", subtype="pdf", filename=invoice_filename(invoice))
    except Exception:
        current_app.logger.exception("Could not attach the invoice for booking %s", booking.id)
    try:
        smtp_send(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        current_app.logger.exception("Could not email booking confirmation for booking %s.", booking.id)
        log_notification(booking, "email", "reserved", customer.email, "failed", str(exc))
        raise BookingEmailError("Booking is saved, but the confirmation email could not be delivered.") from exc

    booking.confirmation_sent_at = datetime.now(timezone.utc)
    db.session.add(Notification(tenant_id=booking.tenant_id, booking_id=booking.id, channel="email", event="reserved",
                                recipient=customer.email[:255], status="sent", error=""))
    db.session.commit()


def _authenticate_and_send(smtp: smtplib.SMTP, message: EmailMessage) -> None:
    username = current_app.config.get("SMTP_USERNAME")
    password = current_app.config.get("SMTP_PASSWORD")
    if username:
        smtp.login(username, password or "")
    smtp.send_message(message)


def smtp_send(message: EmailMessage) -> None:
    """Deliver one message through the configured SMTP server (port 465 = SSL, otherwise STARTTLS)."""
    host, port = current_app.config.get("SMTP_HOST"), current_app.config.get("SMTP_PORT", 587)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
            _authenticate_and_send(smtp, message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            _authenticate_and_send(smtp, message)
