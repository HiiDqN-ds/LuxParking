"""Mobile employee app for booking lookup, check-in, and vehicle inspections."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import segno
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    Response,
    send_from_directory,
    url_for,
)
from flask_login import current_user
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import or_, select

from ..ui_i18n import ui
from ..extensions import db, login_manager
from ..models import (
    Booking,
    BookingExtra,
    DamageMark,
    Dispute,
    Hold,
    Inspection,
    InspectionPhoto,
    InspectionVideo,
    Lot,
    Role,
    Space,
    Tenant,
)
from ..services.availability import AvailabilityError, quote_space
from ..services.booking_admin import mark_extra
from ..services.bookings import (
    BookingNotFound,
    BookingServiceError,
    check_in_booking,
    check_out_booking,
    create_hold,
    release_hold,
)
from ..services.inspection_reports import InspectionReportError, send_inspection_report
from ..services.key_tags import build_key_tag_pdf
from ..services.inspections import (
    read_inspection_draft,
    save_inspection_draft,
    DAMAGE_KINDS,
    REQUIRED_PHOTO_CATEGORIES,
    InspectionConflict,
    InspectionError,
    add_damage_mark,
    complete_inspection,
    create_inspection,
    presigned_photo_url,
    upload_inspection_photo,
    upload_inspection_video,
)
from ..services.inspections import InspectionStorageError

bp = Blueprint("staff", __name__, url_prefix="/staff")

PHOTO_LABELS = {
    "front": "Front",
    "back": "Back",
    "right": "Right side",
    "left": "Left side",
    "corner_front_left": "Front left corner",
    "corner_front_right": "Front right corner",
    "corner_back_left": "Rear left corner",
    "corner_back_right": "Rear right corner",
    "roof": "Roof",
    "windshield": "Windshield",
    "odometer": "Odometer",
    "fuel_level": "Fuel level",
    "signature": "Customer signature",
}


@bp.before_request
def require_staff():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if current_user.role not in (Role.OWNER, Role.STAFF) or current_user.tenant_id is None:
        abort(403)


def _booking_or_404(booking_id: int) -> Booking:
    statement = select(Booking).where(
        Booking.id == booking_id,
        Booking.tenant_id == current_user.tenant_id,
    )
    if current_user.role == Role.STAFF:
        statement = statement.where(Booking.status.in_(("reserved", "checked_in")))
    booking = db.session.execute(
        statement
    ).scalar_one_or_none()
    if (
        booking is not None
        and current_user.role == Role.STAFF
        and booking.status == "reserved"
        and _stored_utc(booking.pickup_at) < _staff_arrival_window_start()
    ):
        booking = None
    if booking is None:
        abort(404)
    return booking


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _staff_arrival_window_start() -> datetime:
    tenant = current_user.tenant
    local_now = datetime.now(ZoneInfo(tenant.timezone))
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def _require_owner():
    if current_user.role != Role.OWNER:
        abort(403)


def _inspection_or_404(inspection_id: int) -> Inspection:
    inspection = db.session.execute(
        select(Inspection).where(
            Inspection.id == inspection_id,
            Inspection.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if inspection is None:
        abort(404)
    _booking_or_404(inspection.booking_id)
    return inspection


def _capture_time() -> datetime:
    raw = (request.form.get("captured_at") or "").strip()
    if not raw:
        raise ValueError("Photo capture time is required.")
    try:
        captured_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Photo capture time is invalid.") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("Photo capture time must include a timezone.")
    return captured_at


def _tenant_datetime(value: str, timezone_name: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid local date and time.") from exc
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("The tenant's time zone is not configured correctly.") from exc
    local = parsed.replace(tzinfo=zone, fold=0)
    alternate = parsed.replace(tzinfo=zone, fold=1)
    if local.utcoffset() != alternate.utcoffset():
        raise ValueError(f"{label} is ambiguous during a daylight-saving change; choose another time.")
    round_trip = local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != parsed:
        raise ValueError(f"{label} does not exist in the tenant's local time zone.")
    return local.astimezone(timezone.utc)


def _integer_field(name: str, label: str) -> int:
    try:
        return int(request.form.get(name, ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc


def _photo_bytes():
    image = request.files.get("photo")
    if image is None:
        raise ValueError("Choose a photo before uploading.")
    limit = current_app.config.get("INSPECTION_MAX_PHOTO_BYTES", 8 * 1024 * 1024)
    data = image.stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The compressed image exceeds the upload size limit.")
    return data, image.mimetype


def _video_bytes():
    video = request.files.get("video")
    if video is None:
        raise ValueError("Choose a video before uploading.")
    limit = current_app.config.get("INSPECTION_MAX_VIDEO_BYTES", 8 * 1024 * 1024)
    data = video.stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The inspection video exceeds the upload size limit.")
    return data, video.mimetype


def _booking_qr_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="parkpilot-booking-qr")


def _find_booking_code(value: str) -> Booking | None:
    value = (value or "").strip()
    if not value:
        return None
    parsed_url = urlparse(value)
    if parsed_url.scheme and "/t/" in parsed_url.path:  # the public ticket link printed in the QR code
        value = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    elif parsed_url.query:
        value = parse_qs(parsed_url.query).get("token", [value])[0]
    try:
        payload = _booking_qr_serializer().loads(value)
        booking_ref = payload.get("booking_ref") if isinstance(payload, dict) else None
    except BadSignature:
        booking_ref = value.upper()  # typed numbers: case does not matter
    if not isinstance(booking_ref, str) or not booking_ref:
        return None
    return db.session.execute(
        select(Booking).where(
            Booking.tenant_id == current_user.tenant_id,
            Booking.booking_ref == booking_ref,
        )
    ).scalar_one_or_none()


@bp.get("/")
def dashboard():
    statement = select(Booking).where(
        Booking.tenant_id == current_user.tenant_id,
    )
    if current_user.role == Role.STAFF:
        arrival_window_start = _staff_arrival_window_start()
        statement = statement.where(
            or_(
                Booking.status == "checked_in",
                (
                    (Booking.status == "reserved")
                    & (Booking.pickup_at >= arrival_window_start)
                ),
            )
        )
    else:
        statement = statement.where(Booking.status.in_(("reserved", "checked_in")))
    bookings = db.session.execute(
        statement.order_by(Booking.pickup_at, Booking.id).limit(100)
    ).scalars().all()
    tasks = db.session.execute(
        select(BookingExtra).join(Booking, Booking.id == BookingExtra.booking_id).where(
            BookingExtra.tenant_id == current_user.tenant_id, BookingExtra.status == "requested",
            Booking.status.in_(("reserved", "checked_in")),
        ).order_by(Booking.pickup_at).limit(50)
    ).scalars().all()
    return render_template("staff/dashboard.html", bookings=bookings, tasks=tasks)


@bp.route("/walk-in", methods=("GET", "POST"))
def walk_in_hold():
    _require_owner()
    tenant = db.session.get(Tenant, current_user.tenant_id)
    if tenant is None:
        abort(404)
    spaces = db.session.execute(
        select(Space, Lot)
        .join(Lot, Lot.id == Space.lot_id)
        .where(
            Space.tenant_id == tenant.id,
            Lot.tenant_id == tenant.id,
            Lot.active.is_(True),
            Space.status.in_(("active", "optional")),
            Space.default_available.is_(True),
        )
        .order_by(Lot.name, Space.name)
    ).all()
    local_now = datetime.now(ZoneInfo(tenant.timezone))
    default_pickup = (local_now + timedelta(hours=2)).replace(
        second=0,
        microsecond=0,
    )
    default_return = default_pickup + timedelta(hours=24)
    values = {
        "space_id": request.form.get("space_id", ""),
        "pickup_at": request.form.get("pickup_at", default_pickup.strftime("%Y-%m-%dT%H:%M")),
        "return_at": request.form.get("return_at", default_return.strftime("%Y-%m-%dT%H:%M")),
        "email": request.form.get("email", ""),
    }
    if request.method == "POST":
        try:
            space_id = int(values["space_id"])
            selected = db.session.execute(
                select(Space, Lot)
                .join(Lot, Lot.id == Space.lot_id)
                .where(
                    Space.id == space_id,
                    Space.tenant_id == tenant.id,
                    Lot.tenant_id == tenant.id,
                    Lot.active.is_(True),
                    Space.status.in_(("active", "optional")),
                    Space.default_available.is_(True),
                )
            ).one_or_none()
            if selected is None:
                raise BookingNotFound("Choose an active parking space.")
            space, lot = selected
            pickup_at = _tenant_datetime(values["pickup_at"], tenant.timezone, "Pick-up")
            return_at = _tenant_datetime(values["return_at"], tenant.timezone, "Return")
            quote_space(space, lot, pickup_at, return_at)
            hold = create_hold(
                tenant_id=tenant.id,
                lot_id=lot.id,
                space_id=space.id,
                pickup_at=pickup_at,
                return_at=return_at,
                customer_email=values["email"],
            )
            return redirect(url_for("staff.walk_in_hold_detail", hold_id=hold.id))
        except (BookingServiceError, AvailabilityError, ValueError) as exc:
            flash(ui(str(exc)), "error")
    return render_template(
        "staff/walk_in.html",
        spaces=spaces,
        values=values,
        timezone_name=tenant.timezone,
    )


@bp.get("/holds/<int:hold_id>")
def walk_in_hold_detail(hold_id: int):
    _require_owner()
    hold = db.session.execute(
        select(Hold).where(Hold.id == hold_id, Hold.tenant_id == current_user.tenant_id)
    ).scalar_one_or_none()
    if hold is None:
        abort(404)
    expires_at = hold.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if hold.status != "active" or expires_at <= datetime.now(timezone.utc):
        if hold.status == "active":
            release_hold(tenant_id=current_user.tenant_id, hold_id=hold.id)
        flash(ui("This temporary hold has expired or was released. No booking was confirmed."), "error")
        return redirect(url_for("staff.dashboard"))
    space = db.session.execute(
        select(Space).where(
            Space.id == hold.space_id,
            Space.lot_id == hold.lot_id,
            Space.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    lot = db.session.execute(
        select(Lot).where(
            Lot.id == hold.lot_id,
            Lot.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if space is None or lot is None:
        abort(404)
    pickup_at = hold.pickup_at
    return_at = hold.return_at
    if pickup_at.tzinfo is None:
        pickup_at = pickup_at.replace(tzinfo=timezone.utc)
    if return_at.tzinfo is None:
        return_at = return_at.replace(tzinfo=timezone.utc)
    try:
        price, currency = quote_space(space, lot, pickup_at, return_at)
    except AvailabilityError:
        price, currency = None, lot.currency
    tenant = db.session.get(Tenant, current_user.tenant_id)
    if tenant is None:
        abort(404)
    zone = ZoneInfo(tenant.timezone)
    pickup_local = pickup_at.astimezone(zone)
    return_local = return_at.astimezone(zone)
    return render_template(
        "staff/hold.html",
        hold=hold,
        space=space,
        lot=lot,
        price=price,
        currency=currency,
        expires_at=expires_at,
        pickup_local=pickup_local,
        return_local=return_local,
        timezone_name=tenant.timezone,
    )


@bp.post("/holds/<int:hold_id>/release")
def release_walk_in_hold(hold_id: int):
    _require_owner()
    try:
        release_hold(tenant_id=current_user.tenant_id, hold_id=hold_id)
    except BookingServiceError as exc:
        flash(ui(str(exc)), "error")
    else:
        flash(ui("Temporary hold released. No booking was confirmed."), "ok")
    return redirect(url_for("staff.dashboard"))


@bp.post("/lookup")
def lookup():
    booking = _find_booking_code(request.form.get("booking_code", ""))
    if booking is None:
        flash(ui("No booking was found for that booking number or QR code."), "error")
        return redirect(url_for("staff.dashboard"))
    return redirect(url_for("staff.booking_detail", booking_id=booking.id))


@bp.get("/open")
def open_qr_booking():
    booking = _find_booking_code(request.args.get("token", ""))
    if booking is None:
        abort(404)
    return redirect(url_for("staff.booking_detail", booking_id=booking.id))


@bp.get("/bookings/<int:booking_id>/qr.svg")
def booking_qr(booking_id: int):
    booking = _booking_or_404(booking_id)
    signed_url = url_for("staff.open_qr_booking", token=booking.qr_code, _external=True)
    code = segno.make(signed_url, error="m")
    output = BytesIO()
    code.save(output, kind="svg", scale=6, border=2, dark="#14212B", light="#FFFFFF")
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "image/svg+xml; charset=utf-8"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/bookings/<int:booking_id>")
def booking_detail(booking_id: int):
    booking = _booking_or_404(booking_id)
    vehicle_photos = []
    if booking.vehicle_id is not None:
        photo_query = (
            select(InspectionPhoto)
            .join(Inspection, Inspection.id == InspectionPhoto.inspection_id)
            .where(
                InspectionPhoto.tenant_id == current_user.tenant_id,
                InspectionPhoto.vehicle_id == booking.vehicle_id,
                InspectionPhoto.category.in_(REQUIRED_PHOTO_CATEGORIES),
            )
            .order_by(InspectionPhoto.captured_at.desc(), InspectionPhoto.id.desc())
        )
        if current_user.role == Role.STAFF:
            photo_query = photo_query.where(Inspection.booking_id == booking.id)
        vehicle_photos = db.session.execute(photo_query).scalars().all()
    inspections = db.session.execute(
        select(Inspection).where(
            Inspection.tenant_id == current_user.tenant_id,
            Inspection.booking_id == booking.id,
        ).order_by(Inspection.created_at)
    ).scalars().all()
    disputes = []
    if current_user.role == Role.OWNER:
        disputes = db.session.execute(
            select(Dispute).where(
                Dispute.tenant_id == current_user.tenant_id,
                Dispute.booking_id == booking.id,
            ).order_by(Dispute.created_at)
        ).scalars().all()
    return render_template(
        "staff/booking.html",
        booking=booking,
        is_staff=current_user.role == Role.STAFF,
        vehicle_photos=vehicle_photos,
        inspections={inspection.phase: inspection for inspection in inspections},
        disputes=disputes,
    )


@bp.get("/bookings/<int:booking_id>/key-tag.pdf")
def key_tag(booking_id: int):
    """Printable key tag with QR and car facts (opens in the browser's print dialog)."""
    booking = _booking_or_404(booking_id)
    return Response(build_key_tag_pdf(booking), mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="key-tag-{booking.booking_ref}.pdf"', "Cache-Control": "private, no-store"})


@bp.get("/bookings/<int:booking_id>/inspections/<phase>")
def inspection_page(booking_id: int, phase: str):
    if phase not in ("arrival", "departure"):
        abort(404)
    booking = _booking_or_404(booking_id)
    inspection = db.session.execute(
        select(Inspection).where(
            Inspection.tenant_id == current_user.tenant_id,
            Inspection.booking_id == booking.id,
            Inspection.phase == phase,
        )
    ).scalar_one_or_none()
    arrival_marks = []
    arrival_photos = []
    if phase == "departure":
        arrival = db.session.execute(
            select(Inspection).where(
                Inspection.tenant_id == current_user.tenant_id,
                Inspection.booking_id == booking.id,
                Inspection.phase == "arrival",
                Inspection.status == "completed",
            )
        ).scalar_one_or_none()
        if arrival is not None:
            arrival_photos = db.session.execute(
                select(InspectionPhoto).where(
                    InspectionPhoto.tenant_id == current_user.tenant_id,
                    InspectionPhoto.inspection_id == arrival.id,
                )
            ).scalars().all()
            arrival_marks = db.session.execute(
                select(DamageMark).where(
                    DamageMark.tenant_id == current_user.tenant_id,
                    DamageMark.inspection_id == arrival.id,
                )
            ).scalars().all()
    return render_template(
        "staff/inspection.html",
        booking=booking,
        is_staff=current_user.role == Role.STAFF,
        inspection=inspection,
        phase=phase,
        draft=read_inspection_draft(inspection) if inspection else {},
        video_done=len({video.category for video in inspection.videos} & set(REQUIRED_PHOTO_CATEGORIES)) if inspection else 0,
        required_total=len(REQUIRED_PHOTO_CATEGORIES),
        has_signature=bool(inspection and any(photo.category == "signature" for photo in inspection.photos)),
        mark_count=len(inspection.damage_marks) if inspection else 0,
        required_photos=REQUIRED_PHOTO_CATEGORIES,
        photo_labels=PHOTO_LABELS,
        damage_kinds=sorted(DAMAGE_KINDS),
        arrival_marks=arrival_marks,
        arrival_photos=arrival_photos,
    )


@bp.post("/bookings/<int:booking_id>/inspections/<phase>/start")
def start_inspection(booking_id: int, phase: str):
    try:
        _booking_or_404(booking_id)
        inspection = create_inspection(
            tenant_id=current_user.tenant_id,
            booking_id=booking_id,
            phase=phase,
            staff_user_id=current_user.id,
        )
    except InspectionError as exc:
        flash(ui(str(exc)), "error")
        return redirect(url_for("staff.booking_detail", booking_id=booking_id))
    return redirect(url_for("staff.inspection_page", booking_id=booking_id, phase=inspection.phase))


@bp.post("/inspections/<int:inspection_id>/photos")
def upload_photo(inspection_id: int):
    inspection = _inspection_or_404(inspection_id)
    try:
        file_data, content_type = _photo_bytes()
        photo = upload_inspection_photo(
            tenant_id=current_user.tenant_id,
            inspection_id=inspection.id,
            staff_user_id=current_user.id,
            category=request.form.get("category", ""),
            file_data=file_data,
            content_type=content_type,
            captured_at=_capture_time(),
        )
    except (InspectionError, ValueError) as exc:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error=str(exc)), 400
        flash(ui(str(exc)), "error")
        return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))
    if request.accept_mimetypes.best == "application/json":
        return jsonify(photo_id=photo.id, category=photo.category), 201
    flash(ui('%(p0)s saved.', p0=ui(PHOTO_LABELS.get(photo.category, photo.category))), "ok")
    return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))


@bp.post("/inspections/<int:inspection_id>/videos")
def upload_video(inspection_id: int):
    inspection = _inspection_or_404(inspection_id)
    try:
        file_data, content_type = _video_bytes()
        video = upload_inspection_video(
            tenant_id=current_user.tenant_id,
            inspection_id=inspection.id,
            staff_user_id=current_user.id,
            category=request.form.get("category", ""),
            file_data=file_data,
            content_type=content_type,
            captured_at=_capture_time(),
        )
    except (InspectionError, ValueError) as exc:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error=str(exc)), 400
        flash(ui(str(exc)), "error")
        return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))
    if request.accept_mimetypes.best == "application/json":
        return jsonify(video_id=video.id, category=video.category), 201
    flash(ui('%(p0)s saved.', p0=ui(PHOTO_LABELS.get(video.category, video.category))), "ok")
    return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))


@bp.post("/inspections/<int:inspection_id>/damages")
def create_damage_mark(inspection_id: int):
    inspection = _inspection_or_404(inspection_id)
    try:
        file_data, content_type = _photo_bytes()
        matched_raw = request.form.get("matched_damage_id", "").strip()
        mark = add_damage_mark(
            tenant_id=current_user.tenant_id,
            inspection_id=inspection.id,
            staff_user_id=current_user.id,
            x=float(request.form.get("x", "")),
            y=float(request.form.get("y", "")),
            kind=request.form.get("kind", ""),
            notes=request.form.get("notes", ""),
            matched_damage_id=int(matched_raw) if matched_raw else None,
            file_data=file_data,
            content_type=content_type,
            captured_at=_capture_time(),
        )
    except (InspectionError, ValueError) as exc:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error=str(exc)), 400
        flash(ui(str(exc)), "error")
        return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))
    if request.accept_mimetypes.best == "application/json":
        return jsonify(damage_mark_id=mark.id), 201
    flash(ui("Damage mark saved with its photo."), "ok")
    return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))


@bp.post("/inspections/<int:inspection_id>/draft")
def save_draft(inspection_id: int):
    """Autosave of the handover form (JSON). Called while typing, so the employee never presses a save button."""
    inspection = _inspection_or_404(inspection_id)
    try:
        save_inspection_draft(
            tenant_id=current_user.tenant_id,
            inspection_id=inspection.id,
            staff_user_id=current_user.id,
            values=request.get_json(silent=True),
        )
    except InspectionConflict as exc:
        return jsonify(error=ui(str(exc))), 409
    except InspectionError as exc:
        return jsonify(error=ui(str(exc))), 400
    return jsonify(saved=True)


@bp.post("/inspections/<int:inspection_id>/complete")
def finish_inspection(inspection_id: int):
    inspection = _inspection_or_404(inspection_id)
    try:
        completed = complete_inspection(
            tenant_id=current_user.tenant_id,
            inspection_id=inspection.id,
            staff_user_id=current_user.id,
            fuel_level=_integer_field("fuel_level", "Fuel level"),
            odometer_km=_integer_field("odometer_km", "Odometer"),
            key_count=_integer_field("key_count", "Key count"),
            belongings=request.form.get("belongings", ""),
            notes=request.form.get("notes", ""),
            no_damage_confirmed=request.form.get("no_damage_confirmed") == "yes",
            require_videos=True,
            vehicle_plate=request.form.get("vehicle_plate", ""),
            vehicle_make=request.form.get("vehicle_make", ""),
            vehicle_model=request.form.get("vehicle_model", ""),
            vehicle_color=request.form.get("vehicle_color", ""),
        )
        moved = False
        if request.form.get("then") == "next":  # one tap: finish the inspection and move the booking on
            try:
                if completed.phase == "arrival":
                    check_in_booking(tenant_id=current_user.tenant_id, booking_id=completed.booking_id)
                    flash(ui("Customer checked in."), "ok")
                else:
                    check_out_booking(tenant_id=current_user.tenant_id, booking_id=completed.booking_id)
                    flash(ui("Customer checked out and the space is available again."), "ok")
                moved = True
            except BookingServiceError as exc:
                flash(ui(str(exc)), "error")
        try:
            send_inspection_report(completed.id, current_user.tenant_id)
            flash(ui("Inspection completed and PDF report emailed to the customer."), "ok")
        except InspectionReportError as exc:
            flash(ui(str(exc)), "error")
        if moved:
            return redirect(url_for("staff.booking_detail", booking_id=completed.booking_id))
    except (InspectionError, ValueError) as exc:
        flash(ui(str(exc)), "error")
    return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))


@bp.post("/inspections/<int:inspection_id>/report")
def retry_report(inspection_id: int):
    _require_owner()
    inspection = _inspection_or_404(inspection_id)
    try:
        send_inspection_report(inspection.id, current_user.tenant_id)
    except (InspectionReportError, InspectionStorageError) as exc:
        flash(ui(str(exc)), "error")
    else:
        flash(ui("PDF report emailed to the customer."), "ok")
    return redirect(url_for("staff.inspection_page", booking_id=inspection.booking_id, phase=inspection.phase))


@bp.get("/photos/<int:photo_id>")
def photo(photo_id: int):
    stored = db.session.execute(
        select(InspectionPhoto).where(
            InspectionPhoto.id == photo_id,
            InspectionPhoto.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if stored is None:
        abort(404)
    _booking_or_404(stored.inspection.booking_id)
    try:
        target = presigned_photo_url(object_key=stored.object_key)
    except InspectionError as exc:
        abort(503, description=str(exc))
    return redirect(target)


@bp.get("/videos/<int:video_id>")
def video(video_id: int):
    stored = db.session.execute(
        select(InspectionVideo).where(
            InspectionVideo.id == video_id,
            InspectionVideo.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if stored is None:
        abort(404)
    _booking_or_404(stored.inspection.booking_id)
    try:
        target = presigned_photo_url(object_key=stored.object_key)
    except InspectionError as exc:
        abort(503, description=str(exc))
    return redirect(target)


@bp.post("/bookings/<int:booking_id>/check-in")
def check_in(booking_id: int):
    try:
        _booking_or_404(booking_id)
        check_in_booking(tenant_id=current_user.tenant_id, booking_id=booking_id)
    except BookingServiceError as exc:
        flash(ui(str(exc)), "error")
    else:
        flash(ui("Customer checked in."), "ok")
    return redirect(url_for("staff.booking_detail", booking_id=booking_id))


@bp.post("/bookings/<int:booking_id>/check-out")
def check_out(booking_id: int):
    try:
        _booking_or_404(booking_id)
        check_out_booking(tenant_id=current_user.tenant_id, booking_id=booking_id)
    except BookingServiceError as exc:
        flash(ui(str(exc)), "error")
    else:
        flash(ui("Customer checked out and the space is available again."), "ok")
    return redirect(url_for("staff.booking_detail", booking_id=booking_id))


@bp.get("/disputes")
def disputes():
    _require_owner()
    records = db.session.execute(
        select(Dispute)
        .where(
            Dispute.tenant_id == current_user.tenant_id,
            Dispute.status == "open",
        )
        .order_by(Dispute.created_at.desc())
    ).scalars().all()
    return render_template("staff/disputes.html", disputes=records)


@bp.get("/offline")
def offline():
    return send_from_directory(current_app.static_folder, "staff/offline.html")


@bp.get("/service-worker.js")
def service_worker():
    response = make_response(send_from_directory(current_app.static_folder, "js/staff-service-worker.js"))
    response.headers["Service-Worker-Allowed"] = "/staff/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.post("/bookings/<int:booking_id>/extras/<int:row_id>/<op>")
def extra_task(booking_id: int, row_id: int, op: str):
    """Staff mark a shuttle or cleaning job done (or reopen it)."""
    booking = _booking_or_404(booking_id)
    row = db.session.get(BookingExtra, row_id)
    if row is None or row.booking_id != booking.id or op not in ("done", "reopen"):
        abort(404)
    mark_extra(row, done=op == "done", user_id=current_user.id)
    db.session.commit()
    flash(ui('%(name)s marked %(p1)s.', name=row.name, p1=ui('done' if op == 'done' else 'open')), "ok")
    return redirect(url_for("staff.booking_detail", booking_id=booking.id))
