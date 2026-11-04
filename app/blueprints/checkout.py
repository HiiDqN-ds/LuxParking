"""Public checkout: hold -> booking (card or pay at arrival), customer booking page, cancellation, Stripe webhook."""
import math
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import session, Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from ..ui_i18n import ui
from ..extensions import csrf, db
from ..i18n import translate
from flask_login import current_user

from ..models import Booking, Extra, Hold, Payment, Role, Space, Tenant
from ..services.availability import AvailabilityError, InvalidInterval, quote_space
from ..services.booking_access import create_customer_booking_token, read_customer_booking_token
from ..services.booking_admin import add_extra, cancel_with_refund, validate_persons
from ..services.booking_documents import booking_pdf, booking_qr_value, build_pdf_for_booking, qr_png, qr_svg
from ..services.booking_events import notify_booking_event
from ..services.booking_notifications import BookingEmailError, EmailNotConfigured, send_booking_confirmation
from ..services.invoices import invoice_filename, invoice_pdf, latest_invoice, sync_invoices
from ..services.bookings import (
    BookingServiceError,
    cancel_booking,
    create_booking_from_hold,
    create_payment_record,
)
from ..services.stripe_payments import (
    StripeBookingError,
    construct_stripe_event,
    create_checkout_session,
    expire_checkout_payment,
    process_stripe_event,
    refund_card_payment,
    stripe_booking_payments_enabled,
)
from .public import _language_links, _locale, _public_lot, _seo_context

from ..security import limiter  # noqa: E402

bp = Blueprint("checkout", __name__)
FREE_CANCEL_HOURS = 48


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _zone(tenant: Tenant):
    try:
        return ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _booking_url(locale, token, **values):
    return url_for("checkout.localized_booking_page", locale=locale, token=token, _external=True, **values)


DRAFT_FIELDS = (
    "first_name", "last_name", "email", "phone", "vehicle_plate", "vehicle_make", "vehicle_model", "company", "billing_email",
    "notes", "extra_shuttle", "extra_cleaning", "passengers", "extra_details_shuttle", "extra_details_cleaning", "payment_method",
)


DRAFT_FIELD_LIMITS = {"notes": 300, "extra_details_shuttle": 120, "extra_details_cleaning": 120}
DRAFT_BUDGET = 1400  # bytes of JSON: sessions are 4 KB cookies, an oversized one would be dropped by the browser


def _back_to_hold(hold_url: str, hold_id: int):
    """A refused booking returns to the hold page with what the customer typed (no retyping), within a hard size budget."""
    fields = {name: request.form.get(name, "")[: DRAFT_FIELD_LIMITS.get(name, 80)] for name in DRAFT_FIELDS if name in request.form}
    for droppable in ("notes", "extra_details_shuttle", "extra_details_cleaning", "billing_email", "company", "vehicle_model", "vehicle_make"):
        if len(json.dumps(fields)) <= DRAFT_BUDGET:
            break
        fields.pop(droppable, None)  # non-ASCII text can take six bytes per character in the cookie
    session["checkout_draft"] = {"hold_id": hold_id, "fields": fields}
    return redirect(hold_url)


@bp.post("/reserve/<tenant_slug>/<lot_slug>/holds/<int:hold_id>/book", defaults={"locale": None}, endpoint="book")
@bp.post("/<locale>/reserve/<tenant_slug>/<lot_slug>/holds/<int:hold_id>/book", endpoint="localized_book")
@limiter.limit("10 per minute; 60 per hour")
def book(tenant_slug: str, lot_slug: str, hold_id: int, locale: str | None):
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    locale = _locale(locale, tenant)
    hold_url = url_for("public.localized_hold_confirmation", locale=locale, tenant_slug=tenant.slug, lot_slug=lot.slug, hold_id=hold_id)
    hold = db.session.execute(
        select(Hold).where(Hold.id == hold_id, Hold.tenant_id == tenant.id, Hold.lot_id == lot.id)
    ).scalar_one_or_none()
    if hold is None:
        abort(404)
    if hold.status != "active" or _aware(hold.expires_at) <= datetime.now(timezone.utc):
        return redirect(hold_url)
    space = db.session.get(Space, hold.space_id)
    try:
        price, currency = quote_space(space, lot, _aware(hold.pickup_at), _aware(hold.return_at))
    except (InvalidInterval, AvailabilityError):
        flash(translate(locale, "booking.price_missing"), "error")
        return _back_to_hold(hold_url, hold_id)
    form = request.form
    if not form.get("accept_terms"):
        flash(translate(locale, "legal.accept"), "error")
        return _back_to_hold(hold_url, hold_id)
    method = form.get("payment_method", "arrival")
    if method == "card" and not stripe_booking_payments_enabled():
        method = "arrival"
    legacy = {int(x) for x in form.getlist("extra") if x.isdigit()}
    picks = {kind: form.get(f"extra_{kind}", "") for kind in ("shuttle", "cleaning") if f"extra_{kind}" in form}
    wanted = legacy | {int(v) for v in picks.values() if v.isdigit()}
    found = db.session.execute(
        select(Extra).where(Extra.tenant_id == tenant.id, Extra.lot_id == lot.id, Extra.active.is_(True), Extra.id.in_(wanted))
    ).scalars().all() if wanted else []
    chosen = []
    for kind in ("shuttle", "cleaning"):  # at most one shuttle and one cleaning option; the visible choice wins
        if kind in picks and not picks[kind].isdigit():
            continue  # "No thanks"
        options = [e for e in found if e.kind == kind and (kind not in picks or str(e.id) == picks[kind])]
        if options:
            chosen.append(min(options, key=lambda e: e.id))
    try:
        for extra in chosen:
            validate_persons(extra, form.get("passengers"))  # shuttle: persons beyond the included ones need a price
        booking = create_booking_from_hold(
            tenant_id=tenant.id, hold_id=hold.id,
            first_name=form.get("first_name", ""), last_name=form.get("last_name", ""),
            email=form.get("email", hold.customer_email), phone=form.get("phone", ""),
            vehicle_plate=form.get("vehicle_plate", ""), vehicle_make=form.get("vehicle_make", ""),
            vehicle_model=form.get("vehicle_model", ""), company=form.get("company", ""),
            billing_email=form.get("billing_email", ""), notes=form.get("notes", ""),
            total_amount=price, currency=currency, locale=locale,
        )
    except BookingServiceError as exc:
        flash(ui(str(exc)), "error")
        return _back_to_hold(hold_url, hold_id)
    for extra in chosen:
        add_extra(
            booking, extra, form.get(f"extra_details_{extra.kind}") or form.get(f"extra_details_{extra.id}", ""),
            form.get("passengers"),
        )
    db.session.commit()
    payment = create_payment_record(
        tenant_id=tenant.id, booking_id=booking.id,
        provider="stripe" if method == "card" else "manual", amount=booking.total_amount, currency=booking.currency,
    )
    token = create_customer_booking_token(tenant_id=tenant.id, booking_ref=booking.booking_ref)
    page = _booking_url(locale, token)
    try:
        send_booking_confirmation(booking.id, tenant.id)
    except EmailNotConfigured:
        current_app.logger.warning("Booking %s saved; no email server configured (SMTP_HOST/SMTP_FROM).", booking.id)
    except BookingEmailError:
        flash(translate(locale, "checkout.email_warning"), "error")
    except Exception:  # never lose a saved booking because SMTP failed
        current_app.logger.exception("Confirmation email failed for booking %s", booking.id)
        flash(translate(locale, "checkout.email_warning"), "error")
    # Issues the invoice and sends the SMS / WhatsApp confirmation when those channels are configured.
    notify_booking_event(booking.id, tenant.id, "reserved", channels=("sms", "whatsapp"))
    if method == "card":
        try:
            session = create_checkout_session(
                booking=booking, payment=payment,
                success_url=_booking_url(locale, token, paid=1), cancel_url=page,
            )
            return redirect(session.url)
        except StripeBookingError as exc:
            flash(ui(str(exc)), "error")
    return redirect(page)


def _load(token: str, locale: str | None):
    parsed = read_customer_booking_token(token)
    if parsed is None:
        abort(404)
    tenant_id, ref = parsed
    g.tenant_id = g.public_tenant_id = tenant_id
    booking = db.session.execute(
        select(Booking).where(Booking.tenant_id == tenant_id, Booking.booking_ref == ref)
    ).scalar_one_or_none()
    tenant = db.session.get(Tenant, tenant_id)
    if booking is None or tenant is None:
        abort(404)
    return tenant, booking, _locale(locale, tenant)


@bp.get("/booking/<token>", defaults={"locale": None}, endpoint="booking_page")
@bp.get("/<locale>/booking/<token>", endpoint="localized_booking_page")
@limiter.limit("60 per minute")
def booking_page(token: str, locale: str | None):
    tenant, booking, locale = _load(token, locale)
    zone = _zone(tenant)
    payment = db.session.execute(
        select(Payment).where(Payment.booking_id == booking.id).order_by(Payment.id.desc())
    ).scalars().first()
    qr_markup = qr_svg(booking_qr_value(booking))
    pickup = _aware(booking.pickup_at)
    can_cancel = booking.status == "reserved" and pickup - datetime.now(timezone.utc) >= timedelta(hours=FREE_CANCEL_HOURS)
    lot = booking.lot
    if lot.lat is not None and lot.lng is not None:
        directions = f"https://www.google.com/maps/dir/?api=1&destination={lot.lat},{lot.lng}"
    else:
        directions = "https://www.google.com/maps/search/?api=1&query=" + quote(f"{lot.name} {lot.address}".strip())
    days = max(1, math.ceil((_aware(booking.return_at) - pickup).total_seconds() / 86400))
    seo = _seo_context(
        locale=locale, endpoint="checkout.localized_booking_page",
        title=translate(locale, "checkout.complete_title"), description=translate(locale, "checkout.intro"),
        endpoint_values={"token": token}, x_default_endpoint="checkout.booking_page", x_default_values={"token": token},
    )
    seo["robots"] = "noindex,nofollow"
    seo["structured_data"] = None
    has_invoice = booking.status != "cancelled" or latest_invoice(booking) is not None
    has_credit_note = latest_invoice(booking, "credit_note") is not None
    return render_template(
        "public/booking.html", has_invoice=has_invoice, has_credit_note=has_credit_note, locale=locale, tenant=tenant, booking=booking, lot=booking.lot, space=booking.space,
        payment=payment, qr_svg=qr_markup, token=token, can_cancel=can_cancel,
        pickup_local=pickup.astimezone(zone), return_local=_aware(booking.return_at).astimezone(zone),
        just_paid=request.args.get("paid") == "1", seo=seo, directions_url=directions, duration_days=days, dev_hint=(request.host.split(":")[0] in ("127.0.0.1", "localhost")),
        language_links=_language_links("checkout.localized_booking_page", locale=locale, endpoint_values={"token": token}),
    )


@bp.post("/booking/<token>/cancel", defaults={"locale": None}, endpoint="cancel")
@bp.post("/<locale>/booking/<token>/cancel", endpoint="localized_cancel")
@limiter.limit("10 per minute; 60 per hour")
def cancel(token: str, locale: str | None):
    tenant, booking, locale = _load(token, locale)
    back = url_for("checkout.localized_booking_page", locale=locale, token=token)
    if _aware(booking.pickup_at) - datetime.now(timezone.utc) < timedelta(hours=FREE_CANCEL_HOURS):
        flash(translate(locale, "checkout.policy"), "error")
        return redirect(back)
    try:
        cancel_with_refund(tenant_id=tenant.id, booking=booking)
    except (StripeBookingError, BookingServiceError) as exc:
        flash(ui(str(exc)), "error")
        return redirect(back)
    flash(translate(locale, "checkout.cancelled"), "ok")
    return redirect(back)


@bp.post("/webhooks/stripe")
@csrf.exempt
@limiter.exempt
def stripe_webhook():
    try:
        event = construct_stripe_event(payload=request.get_data(), signature=request.headers.get("Stripe-Signature", ""))
    except Exception:
        return {"error": "invalid signature"}, 400
    process_stripe_event(event)
    return {"received": True}


@bp.get("/booking/<token>/ticket.pdf", defaults={"locale": None}, endpoint="booking_pdf")
@limiter.limit("30 per minute")
def ticket_pdf(token: str, locale: str | None):
    _, booking, _ = _load(token, locale)
    return Response(build_pdf_for_booking(booking), mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="booking-{booking.booking_ref}.pdf"', "Cache-Control": "private, no-store"})


def _document_response(token: str, locale: str | None, kind: str):
    _, booking, _ = _load(token, locale)
    if kind == "invoice":
        document = sync_invoices(booking)  # issue if missing, refresh while unpaid
        db.session.commit()
    else:
        document = latest_invoice(booking, "credit_note")
    if document is None:
        abort(404)
    return Response(invoice_pdf(document), mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{invoice_filename(document)}"', "Cache-Control": "private, no-store"})


@bp.get("/booking/<token>/invoice.pdf", defaults={"locale": None}, endpoint="invoice_pdf")
@limiter.limit("30 per minute")
def invoice_download(token: str, locale: str | None):
    return _document_response(token, locale, "invoice")


@bp.get("/booking/<token>/credit-note.pdf", defaults={"locale": None}, endpoint="credit_note_pdf")
@limiter.limit("30 per minute")
def credit_note_download(token: str, locale: str | None):
    return _document_response(token, locale, "credit_note")


@bp.get("/booking/<token>/qr.png", defaults={"locale": None}, endpoint="qr_png")
@limiter.limit("30 per minute")
def qr_png_download(token: str, locale: str | None):
    _, booking, _ = _load(token, locale)
    return Response(qr_png(booking_qr_value(booking)), mimetype="image/png", headers={
        "Content-Disposition": f'attachment; filename="parking-{booking.booking_ref}.png"', "Cache-Control": "private, no-store"})


@bp.get("/booking/<token>/calendar.ics", defaults={"locale": None}, endpoint="calendar_ics")
@limiter.limit("30 per minute")
def calendar_ics(token: str, locale: str | None):
    _, booking, _ = _load(token, locale)
    lot = booking.lot
    fmt = lambda d: _aware(d).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc = lambda t: str(t).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ParkPilot//Booking//EN", "BEGIN:VEVENT",
             f"UID:{booking.booking_ref}@parkpilot", f"DTSTAMP:{fmt(datetime.now(timezone.utc))}",
             f"DTSTART:{fmt(booking.pickup_at)}", f"DTEND:{fmt(booking.return_at)}",
             f"SUMMARY:{esc('Parking: ' + lot.name)}", f"LOCATION:{esc((lot.address or lot.name).strip())}",
             f"DESCRIPTION:{esc('Booking number ' + booking.booking_ref + ', space ' + booking.space.name)}",
             "END:VEVENT", "END:VCALENDAR"]
    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar", headers={
        "Content-Disposition": f'attachment; filename="parking-{booking.booking_ref}.ics"'})


@bp.get("/t/<tenant_slug>/<ref>", endpoint="ticket_link")
@limiter.limit("20 per minute; 200 per hour")
def ticket_link(tenant_slug: str, ref: str):
    """Target of the QR code. Staff go to the booking in the staff app, everyone else to the customer's ticket."""
    staff_user = current_user.is_authenticated and current_user.role in (Role.OWNER, Role.STAFF)
    tenant = db.session.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one_or_none()
    if tenant is None:
        abort(404)
    g.tenant_id = g.public_tenant_id = tenant.id
    booking = db.session.execute(
        select(Booking).where(Booking.tenant_id == tenant.id, Booking.booking_ref == ref.strip().upper())
    ).scalar_one_or_none()
    if booking is None:
        abort(404)
    if staff_user and current_user.tenant_id == tenant.id:
        return redirect(url_for("staff.booking_detail", booking_id=booking.id))
    token = create_customer_booking_token(tenant_id=tenant.id, booking_ref=booking.booking_ref)
    return redirect(url_for("checkout.localized_booking_page", locale=_locale(None, tenant), token=token))
