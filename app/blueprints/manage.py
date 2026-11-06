"""Owner back office: create, read, update and delete bookings; manage extras (shuttle, cleaning)."""
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import delete, select

from ..ui_i18n import ui
from ..extensions import db, login_manager
from ..models import EXTRA_KINDS, Booking, BookingExtra, Extra, Inspection, Invoice, Lot, Payment, Space, Vehicle
from ..services.availability import EUR_CURRENCY, AvailabilityError, InvalidInterval, quote_space
from ..services.booking_access import create_customer_booking_token
from ..services.booking_admin import (
    add_extra, cancel_with_refund, extra_cost, mark_extra, parse_local, remove_extra,
)
from ..services.booking_events import notify_booking_event
from ..services.booking_notifications import BookingEmailError, send_booking_confirmation
from ..services.key_tags import build_key_tag_pdf
from ..services.invoices import detach_for_deleted_booking, ensure_invoice, invoice_filename, invoice_pdf, latest_invoice, sync_invoices
from ..services.bookings import (
    BookingServiceError, check_in_booking, check_out_booking, create_booking_from_hold, create_hold,
    create_payment_record, release_hold, reschedule_booking, update_payment_status,
)
from ..services.inspections import InspectionConflict, delete_object, remove_booking_evidence
from ..services.stripe_payments import StripeBookingError
from ..utils import audit

bp = Blueprint("manage", __name__, url_prefix="/admin")
ERRORS = (BookingServiceError, AvailabilityError, InvalidInterval, StripeBookingError, ValueError, InvalidOperation)


@bp.before_request
def owner_only():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if not current_user.is_owner:
        abort(403)


def _booking(booking_id: int) -> Booking:
    return db.get_or_404(Booking, booking_id)


def _money(value: str) -> Decimal:
    amount = Decimal((value or "").replace(",", ".").strip())
    if amount < 0:
        raise ValueError("Amounts cannot be negative.")
    return amount.quantize(Decimal("0.01"))


def _local(dt, tz):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


# ---- bookings -------------------------------------------------------------------------------

@bp.route("/bookings/new", methods=["GET", "POST"])
def booking_new():
    tenant = current_user.tenant
    spaces = Space.query.filter(Space.status != "blocked").order_by(Space.lot_id, Space.name).all()
    extras = Extra.query.filter_by(active=True).order_by(Extra.kind, Extra.name).all()
    if request.method == "POST":
        form, hold = request.form, None
        g.suppress_booking_notifications = True  # the owner decides below whether the customer is told
        try:
            space = db.get_or_404(Space, int(form.get("space_id", "0") or 0))
            lot = db.session.get(Lot, space.lot_id)
            start, end = parse_local(form.get("pickup_at"), tenant.timezone), parse_local(form.get("return_at"), tenant.timezone)
            email = form.get("email", "").strip()
            hold = create_hold(tenant_id=tenant.id, lot_id=lot.id, space_id=space.id, pickup_at=start, return_at=end, customer_email=email)
            if form.get("price", "").strip() and lot.currency.upper() != EUR_CURRENCY:
                price, currency = _money(form["price"]), lot.currency
            else:
                price, currency = quote_space(space, lot, start, end)
            chosen = [e for e in extras if form.get(f"extra_{e.id}")]
            total = Decimal(price) + sum((extra_cost(e, form.get(f"qty_{e.id}")) for e in chosen), Decimal(0))
            booking = create_booking_from_hold(
                tenant_id=tenant.id, hold_id=hold.id, first_name=form.get("first_name", ""), last_name=form.get("last_name", ""),
                email=email, phone=form.get("phone", ""), vehicle_plate=form.get("vehicle_plate", ""),
                vehicle_make=form.get("vehicle_make", ""), vehicle_model=form.get("vehicle_model", ""),
                notes=form.get("notes", ""), total_amount=total, currency=currency, locale=tenant.default_locale,
            )
            hold = None
            for e in chosen:
                add_extra(booking, e, form.get(f"details_{e.id}", ""), form.get(f"qty_{e.id}"))
            booking.total_amount = total
            payment = create_payment_record(tenant_id=tenant.id, booking_id=booking.id, provider="manual", amount=total, currency=currency)
            if form.get("payment_status") == "paid":
                update_payment_status(tenant_id=tenant.id, payment_id=payment.id, status="paid")
            audit("booking.create_manual", "booking", booking.id)
            db.session.commit()
        except ERRORS as exc:
            db.session.rollback()
            if hold is not None:
                try:
                    release_hold(tenant_id=tenant.id, hold_id=hold.id)
                except Exception:
                    pass
            flash(ui(str(exc)), "error")
            return render_template("admin/booking_new.html", spaces=spaces, extras=extras, form=form, lots={l.id: l for l in Lot.query.all()})
        finally:
            g.suppress_booking_notifications = False
        try:  # the invoice exists from the moment the booking does
            sync_invoices(booking)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Could not issue the invoice for booking %s", booking.id)
        if form.get("send_email"):
            _send_email(booking)
            notify_booking_event(booking.id, booking.tenant_id, "reserved", channels=("sms", "whatsapp"))
            if form.get("payment_status") == "paid":
                notify_booking_event(booking.id, booking.tenant_id, "paid", channels=("email", "sms", "whatsapp"))
        flash(ui("Booking created."), "ok")
        return redirect(url_for("manage.booking_detail", booking_id=booking.id))
    return render_template("admin/booking_new.html", spaces=spaces, extras=extras, form={}, lots={l.id: l for l in Lot.query.all()})


def _send_email(booking) -> None:
    try:
        send_booking_confirmation(booking.id, booking.tenant_id)
        flash(ui("Confirmation email sent to the customer."), "ok")
    except BookingEmailError as exc:
        flash(ui(str(exc)), "error")
    except Exception:
        current_app.logger.exception("Confirmation email failed for booking %s", booking.id)
        flash(ui("The confirmation email could not be sent."), "error")


@bp.get("/bookings/<int:booking_id>")
def booking_detail(booking_id: int):
    booking = _booking(booking_id)
    tz = current_user.tenant.timezone
    token = create_customer_booking_token(tenant_id=booking.tenant_id, booking_ref=booking.booking_ref)
    payment = db.session.execute(select(Payment).where(Payment.booking_id == booking.id).order_by(Payment.id.desc())).scalars().first()
    from ..models import Notification
    notifications = db.session.execute(
        select(Notification).where(Notification.booking_id == booking.id).order_by(Notification.id.desc()).limit(30)
    ).scalars().all()
    return render_template(
        "admin/booking_detail.html", booking=booking, payment=payment, token=token,
        invoice=latest_invoice(booking), credit_note=latest_invoice(booking, "credit_note"), notifications=notifications,
        spaces=Space.query.filter_by(lot_id=booking.lot_id).order_by(Space.name).all(),
        extras=Extra.query.filter_by(active=True, lot_id=booking.lot_id).order_by(Extra.kind, Extra.name).all(),
        pickup_value=_local(booking.pickup_at, tz), return_value=_local(booking.return_at, tz),
    )


@bp.post("/bookings/<int:booking_id>/details")
def booking_update(booking_id: int):
    booking, form = _booking(booking_id), request.form
    c = booking.customer
    first = form.get("first_name", "").strip()
    if not first:
        flash(ui("The customer needs a first name."), "error")
        return redirect(url_for("manage.booking_detail", booking_id=booking.id))
    c.first_name, c.last_name = first[:80], form.get("last_name", "").strip()[:80]
    c.email, c.phone = form.get("email", c.email).strip()[:255], form.get("phone", "").strip()[:40]
    c.company, c.billing_email = form.get("company", "").strip()[:120], form.get("billing_email", "").strip()[:255]
    booking.notes = form.get("notes", "").strip()[:2000]
    plate = form.get("vehicle_plate", "").strip()[:30]
    if plate:
        if booking.vehicle is None:
            booking.vehicle = Vehicle(tenant_id=booking.tenant_id, customer_id=c.id, plate=plate)
        booking.vehicle.plate, booking.vehicle.make = plate, form.get("vehicle_make", "").strip()[:60]
        booking.vehicle.model, booking.vehicle.color = form.get("vehicle_model", "").strip()[:60], form.get("vehicle_color", "").strip()[:40]
    audit("booking.update", "booking", booking.id)
    db.session.commit()
    flash(ui("Booking details saved."), "ok")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/reschedule")
def booking_reschedule(booking_id: int):
    booking, form, tenant = _booking(booking_id), request.form, current_user.tenant
    try:
        start, end = parse_local(form.get("pickup_at"), tenant.timezone), parse_local(form.get("return_at"), tenant.timezone)
        space_id = int(form.get("space_id", booking.space_id))
        reschedule_booking(tenant_id=tenant.id, booking_id=booking.id, space_id=space_id, pickup_at=start, return_at=end)
        if form.get("reprice"):
            db.session.refresh(booking)
            base, _ = quote_space(db.session.get(Space, space_id), booking.lot, start, end)
            extras = sum((e.total() for e in booking.extras if e.status != "cancelled"), Decimal(0))
            booking.total_amount = Decimal(base) + extras
        audit("booking.reschedule", "booking", booking.id)
        db.session.commit()
        flash(ui("Booking moved."), "ok")
    except ERRORS as exc:
        db.session.rollback()
        flash(ui(str(exc)), "error")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/action/<action>")
def booking_action(booking_id: int, action: str):
    booking = _booking(booking_id)
    try:
        if action == "cancel":
            cancel_with_refund(tenant_id=booking.tenant_id, booking=booking)
        elif action == "check-in":
            check_in_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
        elif action == "check-out":
            check_out_booking(tenant_id=booking.tenant_id, booking_id=booking.id)
        else:
            abort(404)
        audit(f"booking.{action}", "booking", booking.id)
        db.session.commit()
        flash(ui("Booking updated."), "ok")
    except ERRORS as exc:
        db.session.rollback()
        flash(ui(str(exc)), "error")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/payment")
def booking_payment(booking_id: int):
    booking = _booking(booking_id)
    status = request.form.get("status", "")
    try:
        payment = db.session.execute(select(Payment).where(Payment.booking_id == booking.id).order_by(Payment.id.desc())).scalars().first()
        if payment is None:
            payment = create_payment_record(tenant_id=booking.tenant_id, booking_id=booking.id, provider="manual",
                                            amount=booking.total_amount, currency=booking.currency)
        update_payment_status(tenant_id=booking.tenant_id, payment_id=payment.id, status=status)
        audit("booking.payment", "booking", booking.id, status=status)
        db.session.commit()
        flash(ui("Payment status updated."), "ok")
    except ERRORS as exc:
        db.session.rollback()
        flash(ui(str(exc)), "error")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/extras")
def booking_extra_add(booking_id: int):
    booking = _booking(booking_id)
    extra = Extra.query.filter_by(id=int(request.form.get("extra_id", "0") or 0), lot_id=booking.lot_id, active=True).first()
    if extra is None:
        flash(ui("Choose an available extra."), "error")
    else:
        try:
            add_extra(booking, extra, request.form.get("details", ""), request.form.get("quantity"))
        except BookingServiceError as exc:
            db.session.rollback()
            flash(ui(str(exc)), "error")
            return redirect(url_for("manage.booking_detail", booking_id=booking.id))
        audit("booking.extra_add", "booking", booking.id, extra=extra.name)
        db.session.commit()
        flash(ui('%(name)s added.', name=extra.name), "ok")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/extras/<int:row_id>/<op>")
def booking_extra_op(booking_id: int, row_id: int, op: str):
    booking = _booking(booking_id)
    row = db.get_or_404(BookingExtra, row_id)
    if row.booking_id != booking.id or op not in ("remove", "done", "reopen"):
        abort(404)
    if op == "remove":
        remove_extra(booking, row)
    else:
        mark_extra(row, done=op == "done", user_id=current_user.id)
    db.session.commit()
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/resend")
def booking_resend(booking_id: int):
    booking = _booking(booking_id)
    booking.confirmation_sent_at = None
    db.session.commit()
    _send_email(booking)
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


@bp.post("/bookings/<int:booking_id>/delete")
def booking_delete(booking_id: int):
    booking = _booking(booking_id)
    if booking.status not in ("cancelled", "reserved") or booking.payment_status == "paid":
        flash(ui("Only unpaid reserved or cancelled bookings can be deleted. Cancel or refund it first."), "error")
        return redirect(url_for("manage.booking_detail", booking_id=booking.id))
    try:
        object_keys = remove_booking_evidence(tenant_id=booking.tenant_id, booking_id=booking.id)
    except InspectionConflict as exc:
        db.session.rollback()
        flash(ui(str(exc)), "error")
        return redirect(url_for("manage.booking_detail", booking_id=booking.id))
    ref = booking.booking_ref
    audit("booking.delete", "booking", booking.id, ref=ref, inspection_photos_removed=len(object_keys))
    detach_for_deleted_booking(booking)  # invoices must be kept; they only lose the link to the deleted booking
    db.session.flush()
    db.session.execute(delete(Payment).where(Payment.booking_id == booking.id))
    db.session.delete(booking)
    db.session.commit()
    for object_key in object_keys:  # after the commit: a failed commit must not lose the images
        try:
            delete_object(object_key=object_key)
        except Exception:
            current_app.logger.warning("Could not delete stored inspection image %s", object_key, exc_info=True)
    flash(ui('Booking %(ref)s deleted.', ref=ref), "ok")
    return redirect(url_for("admin.bookings"))


@bp.post("/bookings/<int:booking_id>/resend/<event>")
def booking_resend_event(booking_id: int, event: str):
    """Send one customer notice again (payment receipt, cancellation, refund/credit note, check-out invoice)."""
    booking = _booking(booking_id)
    if event not in ("paid", "cancelled", "refunded", "checked_out", "reminder"):
        abort(404)
    result = notify_booking_event(booking.id, booking.tenant_id, event, channels=("email",), force=True)
    if result.get("email") == "sent":
        flash(ui("Email sent to the customer."), "ok")
    elif result.get("email") == "skipped":
        flash(ui("No email server is configured (SMTP_HOST / SMTP_FROM), so nothing was sent."), "error")
    else:
        flash(ui("The email could not be sent. See the notification log on this page."), "error")
    return redirect(url_for("manage.booking_detail", booking_id=booking.id))


# ---- invoices (Rechnung) and credit notes -----------------------------------------------------

def _pdf_response(invoice: Invoice) -> Response:
    return Response(invoice_pdf(invoice), mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{invoice_filename(invoice)}"', "Cache-Control": "private, no-store"})


@bp.get("/bookings/<int:booking_id>/invoice.pdf")
def booking_invoice_pdf(booking_id: int):
    booking = _booking(booking_id)
    invoice = sync_invoices(booking)  # issues it if missing and refreshes it while it is still open
    db.session.commit()
    if invoice is None:
        abort(404)
    return _pdf_response(invoice)


@bp.get("/bookings/<int:booking_id>/key-tag.pdf")
def booking_key_tag(booking_id: int):
    """Printable key tag (QR + car facts) to stick on the key."""
    booking = _booking(booking_id)
    return Response(build_key_tag_pdf(booking), mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="key-tag-{booking.booking_ref}.pdf"', "Cache-Control": "private, no-store"})


@bp.get("/bookings/<int:booking_id>/credit-note.pdf")
def booking_credit_note_pdf(booking_id: int):
    booking = _booking(booking_id)
    note = latest_invoice(booking, "credit_note")
    if note is None:
        abort(404)
    return _pdf_response(note)


@bp.get("/invoices")
def invoices():
    """All invoices and credit notes of this tenant, newest first, with filters."""
    status, kind, q = request.args.get("status", ""), request.args.get("kind", ""), request.args.get("q", "").strip()
    query = select(Invoice).order_by(Invoice.id.desc())
    if status in ("open", "paid", "cancelled", "credited"):
        query = query.where(Invoice.status == status)
    if kind in ("invoice", "credit_note"):
        query = query.where(Invoice.kind == kind)
    if q:
        like = f"%{q}%"
        query = query.where(Invoice.number.ilike(like) | Invoice.booking_ref.ilike(like) | Invoice.customer_name.ilike(like) | Invoice.customer_email.ilike(like))
    rows = db.session.execute(query.limit(500)).scalars().all()
    return render_template("admin/invoices.html", rows=rows, status=status, kind=kind, q=q)


@bp.get("/invoices.csv")
def invoices_csv():
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["number", "type", "status", "issued", "booking", "customer", "email", "currency", "net", "vat_rate", "vat", "gross"])

    def safe(value):  # spreadsheet formula injection guard, like the bookings export
        text = str(value if value is not None else "")
        return "'" + text if text[:1] in ("=", "+", "-", "@") and not text.lstrip("-").replace(".", "", 1).isdigit() else text

    for inv in db.session.execute(select(Invoice).order_by(Invoice.id).limit(5000)).scalars():
        writer.writerow([safe(x) for x in (inv.number, inv.kind, inv.status, inv.issued_at.strftime("%Y-%m-%d"), inv.booking_ref, inv.customer_name,
                                           inv.customer_email, inv.currency, inv.net_amount, inv.vat_rate, inv.vat_amount, inv.gross_amount)])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="invoices.csv"'})


@bp.get("/invoices/<int:invoice_id>.pdf")
def invoice_download(invoice_id: int):
    return _pdf_response(db.get_or_404(Invoice, invoice_id))


@bp.route("/invoice-settings", methods=["GET", "POST"])
def invoice_settings():
    """Seller details, VAT rate and VAT ID printed on this tenant's invoices."""
    tenant = current_user.tenant
    if request.method == "POST":
        raw_rate = request.form.get("vat_rate", "").strip().replace(",", ".")
        try:
            rate = Decimal(raw_rate) if raw_rate else None
            if rate is not None and not (Decimal(0) <= rate <= Decimal(100)):
                raise ValueError
        except (InvalidOperation, ValueError):
            flash(ui("Enter the VAT percentage as a number between 0 and 100, or leave it empty to use the default."), "error")
            return render_template("admin/invoice_settings.html", tenant=tenant)
        tenant.vat_rate = rate
        tenant.vat_id = request.form.get("vat_id", "").strip()[:40] or None
        tenant.invoice_seller = request.form.get("invoice_seller", "").strip()[:600] or None
        audit("invoice.settings", "tenant", tenant.id)
        db.session.commit()
        flash(ui("Invoice settings saved. Open invoices pick them up the next time they are opened or sent; paid invoices never change."), "ok")
        return redirect(url_for("manage.invoice_settings"))
    return render_template("admin/invoice_settings.html", tenant=tenant)


# ---- extras catalogue -----------------------------------------------------------------------

def _save_extra(extra: Extra, form) -> None:
    kind, name = form.get("kind", ""), form.get("name", "").strip()
    if kind not in EXTRA_KINDS or not name:
        raise ValueError("Choose a type and enter a name.")
    extra.kind, extra.name, extra.description = kind, name[:100], form.get("description", "").strip()[:255]
    extra.price, extra.active = _money(form.get("price", "0")), bool(form.get("active"))
    extra.per_person = bool(form.get("per_person"))
    included = (form.get("included_persons") or "0").strip() or "0"
    if not included.isdigit() or int(included) > 20:
        raise ValueError("Persons included must be a whole number from 0 to 20.")
    extra.included_persons = int(included) if extra.per_person else 0
    extra_price = (form.get("extra_person_price") or "").strip()
    extra.extra_person_price = _money(extra_price) if extra_price and extra.included_persons else None
    limit = (form.get("max_persons") or "").strip()
    if limit and (not limit.isdigit() or not 1 <= int(limit) <= 20):
        raise ValueError("Maximum persons must be a whole number from 1 to 20.")
    extra.max_persons = int(limit) if limit and extra.per_person else None


@bp.route("/extras", methods=["GET", "POST"])
def extras():
    lots = Lot.query.order_by(Lot.name).all()
    if request.method == "POST":
        try:
            lot = db.get_or_404(Lot, int(request.form.get("lot_id", "0") or 0))
            extra = Extra(lot_id=lot.id, tenant_id=current_user.tenant_id)
            _save_extra(extra, request.form)
            db.session.add(extra)
            audit("extra.create", "extra", None, name=extra.name)
            db.session.commit()
            flash(ui("Extra created."), "ok")
            return redirect(url_for("manage.extras"))
        except ERRORS as exc:
            db.session.rollback()
            flash(ui(str(exc)), "error")
    return render_template("admin/extras.html", lots=lots, items=Extra.query.order_by(Extra.lot_id, Extra.kind, Extra.name).all(), kinds=EXTRA_KINDS)


@bp.route("/extras/<int:extra_id>/edit", methods=["GET", "POST"])
def extra_edit(extra_id: int):
    extra = db.get_or_404(Extra, extra_id)
    if request.method == "POST":
        try:
            _save_extra(extra, request.form)
            db.session.commit()
            flash(ui("Extra saved."), "ok")
            return redirect(url_for("manage.extras"))
        except ERRORS as exc:
            db.session.rollback()
            flash(ui(str(exc)), "error")
    return render_template("admin/extra_form.html", extra=extra, kinds=EXTRA_KINDS)


@bp.post("/extras/<int:extra_id>/delete")
def extra_delete(extra_id: int):
    extra = db.get_or_404(Extra, extra_id)
    db.session.delete(extra)  # past bookings keep their copied name and price
    db.session.commit()
    flash(ui("Extra deleted."), "ok")
    return redirect(url_for("manage.extras"))
