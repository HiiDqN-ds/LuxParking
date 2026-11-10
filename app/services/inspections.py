"""Tenant-scoped vehicle inspections and private evidence storage."""

import hashlib
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    Booking,
    DamageMark,
    Dispute,
    Inspection,
    InspectionPhoto,
    InspectionVideo,
    Role,
    Tenant,
    User,
    Vehicle,
    utcnow,
)
from .api_platform import enqueue_webhook_event

REQUIRED_PHOTO_CATEGORIES = (
    "front",
    "back",
    "right",
    "left",
    "corner_front_left",
    "corner_front_right",
    "corner_back_left",
    "corner_back_right",
    "roof",
    "windshield",
    "odometer",
    "fuel_level",
)
PHOTO_CATEGORIES = frozenset(REQUIRED_PHOTO_CATEGORIES)
PHOTO_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
VIDEO_CONTENT_TYPES = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}
INSPECTION_VIDEO_CATEGORY = "walkaround"
DAMAGE_KINDS = frozenset(("scratch", "dent", "crack", "broken_part"))


class InspectionError(Exception):
    """Base error for vehicle inspection operations."""


class InspectionNotFound(InspectionError):
    """Raised when an inspection or related record is not in the tenant."""


class InspectionConflict(InspectionError):
    """Raised when the inspection or booking is in an incompatible state."""


class InspectionValidationError(InspectionError):
    """Raised when inspection data or evidence does not meet requirements."""


class InspectionStorageError(InspectionError):
    """Raised when private object storage is not configured or unavailable."""


def _tenant_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InspectionValidationError("A positive tenant_id is required.")
    return value


def _load_staff(tenant_id: int, staff_user_id: int) -> User:
    staff = db.session.execute(
        select(User).where(
            User.id == staff_user_id,
            User.tenant_id == tenant_id,
            User.active.is_(True),
            User.role.in_((Role.OWNER, Role.STAFF)),
        )
    ).scalar_one_or_none()
    if staff is None:
        raise InspectionNotFound("Staff member not found.")
    return staff


def _load_booking(tenant_id: int, booking_id: int) -> Booking:
    booking = db.session.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if booking is None:
        raise InspectionNotFound("Booking not found.")
    return booking


def _load_inspection(tenant_id: int, inspection_id: int, *, lock: bool = False) -> Inspection:
    statement = select(Inspection).where(
        Inspection.id == inspection_id,
        Inspection.tenant_id == tenant_id,
    )
    if lock:
        statement = statement.with_for_update()
    inspection = db.session.execute(statement).scalar_one_or_none()
    if inspection is None:
        raise InspectionNotFound("Inspection not found.")
    return inspection


def _required_categories(tenant_id: int) -> tuple[str, ...]:
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        raise InspectionNotFound("Tenant not found.")
    configured = tenant.inspection_required_photos
    if not isinstance(configured, list) or any(
        not isinstance(category, str) or category not in PHOTO_CATEGORIES
        for category in configured
    ):
        raise InspectionValidationError("The tenant's required photo configuration is invalid.")
    if not configured or len(configured) != len(set(configured)):
        raise InspectionValidationError("At least one unique required photo category must be configured.")
    return tuple(configured)


def _s3_client():
    bucket = current_app.config.get("INSPECTION_S3_BUCKET")
    if not bucket:
        raise InspectionStorageError("Private inspection object storage is not configured.")
    try:
        return boto3.client(
            "s3",
            endpoint_url=current_app.config.get("INSPECTION_S3_ENDPOINT_URL") or None,
            region_name=current_app.config.get("INSPECTION_S3_REGION") or None,
            aws_access_key_id=current_app.config.get("INSPECTION_S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=current_app.config.get("INSPECTION_S3_SECRET_ACCESS_KEY") or None,
        )
    except (BotoCoreError, ClientError) as exc:
        raise InspectionStorageError("Could not connect to private inspection storage.") from exc


def _store_object(
    *,
    object_key: str,
    file_data: bytes,
    content_type: str,
    sha256: str,
) -> None:
    try:
        _s3_client().put_object(
            Bucket=current_app.config["INSPECTION_S3_BUCKET"],
            Key=object_key,
            Body=file_data,
            ContentType=content_type,
            Metadata={"sha256": sha256},
        )
    except (BotoCoreError, ClientError) as exc:
        raise InspectionStorageError("Could not upload the inspection evidence.") from exc


def _delete_object(*, object_key: str) -> None:
    try:
        delete_object(object_key=object_key)
    except InspectionStorageError:
        current_app.logger.exception("Could not clean up inspection object %s.", object_key)


def delete_object(*, object_key: str) -> None:
    try:
        _s3_client().delete_object(
            Bucket=current_app.config["INSPECTION_S3_BUCKET"],
            Key=object_key,
        )
    except (BotoCoreError, ClientError) as exc:
        raise InspectionStorageError("Could not delete expired inspection evidence.") from exc


def read_object(*, object_key: str) -> bytes:
    try:
        result = _s3_client().get_object(
            Bucket=current_app.config["INSPECTION_S3_BUCKET"],
            Key=object_key,
        )
        return result["Body"].read()
    except (BotoCoreError, ClientError, KeyError) as exc:
        raise InspectionStorageError("Could not read the inspection evidence.") from exc


def presigned_photo_url(*, object_key: str, expires_in: int = 300) -> str:
    try:
        return _s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": current_app.config["INSPECTION_S3_BUCKET"],
                "Key": object_key,
            },
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as exc:
        raise InspectionStorageError("Could not create a private evidence link.") from exc


def create_inspection(
    *,
    tenant_id: int,
    booking_id: int,
    phase: str,
    staff_user_id: int,
) -> Inspection:
    tenant_id = _tenant_id(tenant_id)
    if phase not in ("arrival", "departure"):
        raise InspectionValidationError("Inspection phase must be arrival or departure.")
    booking = _load_booking(tenant_id, booking_id)
    _load_staff(tenant_id, staff_user_id)
    existing = db.session.execute(
        select(Inspection).where(
            Inspection.tenant_id == tenant_id,
            Inspection.booking_id == booking_id,
            Inspection.phase == phase,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == "completed":
            return existing
        return existing
    expected_status = "reserved" if phase == "arrival" else "checked_in"
    if booking.status != expected_status:
        raise InspectionConflict(f"{phase.title()} inspection requires booking status {expected_status}.")
    if phase == "departure" and not has_completed_inspection(
        tenant_id=tenant_id,
        booking_id=booking_id,
        phase="arrival",
    ):
        raise InspectionConflict("A completed arrival inspection is required first.")

    tenant = db.session.get(Tenant, tenant_id)
    inspection = Inspection(
        tenant_id=tenant_id,
        booking_id=booking_id,
        phase=phase,
        staff_user_id=staff_user_id,
        retention_until=utcnow() + timedelta(days=tenant.inspection_retention_days),
    )
    db.session.add(inspection)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise InspectionConflict("An inspection already exists for this booking phase.") from exc
    return inspection


def _validate_photo(
    *,
    file_data: bytes,
    content_type: str,
    captured_at: datetime,
) -> tuple[bytes, str, datetime]:
    if not isinstance(file_data, bytes) or not file_data:
        raise InspectionValidationError("A non-empty inspection image is required.")
    max_bytes = current_app.config.get("INSPECTION_MAX_PHOTO_BYTES", 8 * 1024 * 1024)
    if len(file_data) > max_bytes:
        raise InspectionValidationError("The compressed image exceeds the upload size limit.")
    if content_type not in PHOTO_CONTENT_TYPES:
        raise InspectionValidationError("Only JPEG, PNG, and WebP inspection images are accepted.")
    try:
        with Image.open(BytesIO(file_data)) as image:
            expected_format = {
                "image/jpeg": "JPEG",
                "image/png": "PNG",
                "image/webp": "WEBP",
            }[content_type]
            if image.format != expected_format:
                raise InspectionValidationError("Image contents do not match the declared file type.")
            max_pixels = current_app.config.get("INSPECTION_MAX_IMAGE_PIXELS", 12_000_000)
            if image.width * image.height > max_pixels:
                raise InspectionValidationError("Image dimensions exceed the inspection limit.")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InspectionValidationError("The uploaded file is not a valid inspection image.") from exc
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None:
        raise InspectionValidationError("The photo capture time must include a timezone.")
    return file_data, content_type, captured_at.astimezone(timezone.utc)


def _save_photo(
    *,
    inspection: Inspection,
    staff: User,
    category: str,
    file_data: bytes,
    content_type: str,
    captured_at: datetime,
    commit: bool = True,
) -> InspectionPhoto:
    data, mime, capture_time = _validate_photo(
        file_data=file_data,
        content_type=content_type,
        captured_at=captured_at,
    )
    extension = PHOTO_CONTENT_TYPES[mime]
    digest = hashlib.sha256(data).hexdigest()
    object_key = (
        f"tenants/{inspection.tenant_id}/bookings/{inspection.booking.booking_ref}/"
        f"inspections/{inspection.id}/{secrets.token_hex(16)}.{extension}"
    )
    _store_object(
        object_key=object_key,
        file_data=data,
        content_type=mime,
        sha256=digest,
    )
    photo = InspectionPhoto(
        tenant_id=inspection.tenant_id,
        inspection_id=inspection.id,
        category=category,
        object_key=object_key,
        sha256=digest,
        content_type=mime,
        size_bytes=len(data),
        booking_ref=inspection.booking.booking_ref,
        staff_user_id=staff.id,
        staff_name=staff.name or staff.email,
        captured_at=capture_time,
    )
    db.session.add(photo)
    try:
        db.session.flush()
        if commit:
            db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        _delete_object(object_key=object_key)
        raise InspectionConflict("A photo for this inspection category already exists.") from exc
    except Exception:
        db.session.rollback()
        _delete_object(object_key=object_key)
        raise
    return photo


def upload_inspection_photo(
    *,
    tenant_id: int,
    inspection_id: int,
    staff_user_id: int,
    category: str,
    file_data: bytes,
    content_type: str,
    captured_at: datetime,
) -> InspectionPhoto:
    tenant_id = _tenant_id(tenant_id)
    inspection = _load_inspection(tenant_id, inspection_id, lock=True)
    if inspection.status != "in_progress":
        raise InspectionConflict("Photos cannot be changed after inspection completion.")
    allowed = set(_required_categories(tenant_id)) | {"signature"}
    if category not in allowed:
        raise InspectionValidationError("This photo category is not configured for this tenant.")
    staff = _load_staff(tenant_id, staff_user_id)
    return _save_photo(
        inspection=inspection,
        staff=staff,
        category=category,
        file_data=file_data,
        content_type=content_type,
        captured_at=captured_at,
    )


def upload_inspection_video(
    *,
    tenant_id: int,
    inspection_id: int,
    staff_user_id: int,
    category: str,
    file_data: bytes,
    content_type: str,
    captured_at: datetime,
) -> InspectionVideo:
    tenant_id = _tenant_id(tenant_id)
    if not isinstance(file_data, bytes) or not file_data:
        raise InspectionValidationError("A non-empty inspection video is required.")
    max_bytes = current_app.config.get("INSPECTION_MAX_VIDEO_BYTES", 8 * 1024 * 1024)
    if len(file_data) > max_bytes:
        raise InspectionValidationError("The inspection video exceeds the upload size limit.")
    if content_type not in VIDEO_CONTENT_TYPES:
        raise InspectionValidationError("Only MP4, WebM, and QuickTime inspection videos are accepted.")
    if content_type == "video/webm":
        valid_signature = file_data.startswith(b"\x1a\x45\xdf\xa3")
    else:
        valid_signature = len(file_data) >= 12 and file_data[4:8] == b"ftyp"
    if not valid_signature:
        raise InspectionValidationError("The uploaded file is not a valid inspection video.")
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise InspectionValidationError("The video capture time must include a timezone.")

    inspection = _load_inspection(tenant_id, inspection_id, lock=True)
    if inspection.status != "in_progress":
        raise InspectionConflict("Videos cannot be changed after inspection completion.")
    if category != INSPECTION_VIDEO_CATEGORY:
        raise InspectionValidationError("The inspection video must be a walkaround video.")
    existing_video = db.session.execute(
        select(InspectionVideo.id)
        .where(
            InspectionVideo.tenant_id == tenant_id,
            InspectionVideo.inspection_id == inspection.id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing_video is not None:
        raise InspectionConflict("Only one video is allowed per inspection.")
    staff = _load_staff(tenant_id, staff_user_id)
    extension = VIDEO_CONTENT_TYPES[content_type]
    digest = hashlib.sha256(file_data).hexdigest()
    object_key = (
        f"tenants/{inspection.tenant_id}/bookings/{inspection.booking.booking_ref}/"
        f"inspections/{inspection.id}/{secrets.token_hex(16)}.{extension}"
    )
    _store_object(
        object_key=object_key,
        file_data=file_data,
        content_type=content_type,
        sha256=digest,
    )
    video = InspectionVideo(
        tenant_id=inspection.tenant_id,
        inspection_id=inspection.id,
        category=category,
        object_key=object_key,
        sha256=digest,
        content_type=content_type,
        size_bytes=len(file_data),
        booking_ref=inspection.booking.booking_ref,
        staff_user_id=staff.id,
        staff_name=staff.name or staff.email,
        captured_at=captured_at.astimezone(timezone.utc),
    )
    db.session.add(video)
    try:
        db.session.flush()
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        _delete_object(object_key=object_key)
        raise InspectionConflict("A video for this inspection category already exists.") from exc
    except Exception:
        db.session.rollback()
        _delete_object(object_key=object_key)
        raise
    return video


def add_damage_mark(
    *,
    tenant_id: int,
    inspection_id: int,
    staff_user_id: int,
    x: float,
    y: float,
    kind: str,
    notes: str,
    file_data: bytes,
    content_type: str,
    captured_at: datetime,
    matched_damage_id: int | None = None,
) -> DamageMark:
    tenant_id = _tenant_id(tenant_id)
    inspection = _load_inspection(tenant_id, inspection_id, lock=True)
    if inspection.status != "in_progress":
        raise InspectionConflict("Damage marks cannot be changed after inspection completion.")
    try:
        x_value, y_value = float(x), float(y)
    except (TypeError, ValueError) as exc:
        raise InspectionValidationError("Damage location must be between 0 and 100 on both axes.") from exc
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not math.isfinite(x_value)
        or not math.isfinite(y_value)
        or not 0 <= x_value <= 100
        or not 0 <= y_value <= 100
    ):
        raise InspectionValidationError("Damage location must be between 0 and 100 on both axes.")
    if kind not in DAMAGE_KINDS:
        raise InspectionValidationError("Unsupported damage type.")
    if len(notes or "") > 1000:
        raise InspectionValidationError("Damage notes exceed the allowed length.")
    staff = _load_staff(tenant_id, staff_user_id)
    matched = None
    if matched_damage_id is not None:
        matched = db.session.execute(
            select(DamageMark)
            .join(Inspection, Inspection.id == DamageMark.inspection_id)
            .where(
                DamageMark.id == matched_damage_id,
                DamageMark.tenant_id == tenant_id,
                Inspection.tenant_id == tenant_id,
                Inspection.booking_id == inspection.booking_id,
                Inspection.phase == "arrival",
                Inspection.status == "completed",
                DamageMark.kind == kind,
            )
        ).scalar_one_or_none()
        if matched is None:
            raise InspectionValidationError("A matched damage must be the same type on this booking's arrival inspection.")
        if db.session.execute(
            select(DamageMark.id).where(
                DamageMark.tenant_id == tenant_id,
                DamageMark.matched_damage_id == matched.id,
            )
        ).first():
            raise InspectionValidationError("An arrival damage can only be matched once.")
    mark = DamageMark(
        tenant_id=tenant_id,
        inspection_id=inspection.id,
        x_percent=x_value,
        y_percent=y_value,
        kind=kind,
        notes=(notes or "").strip(),
        matched_damage_id=matched.id if matched else None,
    )
    db.session.add(mark)
    db.session.flush()
    photo_object_key = None
    try:
        photo = _save_photo(
            inspection=inspection,
            staff=staff,
            category=f"damage-{mark.id}",
            file_data=file_data,
            content_type=content_type,
            captured_at=captured_at,
            commit=False,
        )
        photo_object_key = photo.object_key
        mark.photo_id = photo.id
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        if photo_object_key:
            _delete_object(object_key=photo_object_key)
        error_text = str(exc.orig)
        if (
            "uq_damage_mark_matched_once" in error_text
            or "damage_marks.tenant_id, damage_marks.matched_damage_id" in error_text
        ):
            raise InspectionConflict("This arrival damage has already been matched.") from exc
        raise
    except Exception:
        db.session.rollback()
        if photo_object_key:
            _delete_object(object_key=photo_object_key)
        raise
    return mark


def complete_inspection(
    *,
    tenant_id: int,
    inspection_id: int,
    staff_user_id: int,
    fuel_level: int,
    odometer_km: int,
    key_count: int,
    belongings: str,
    notes: str,
    no_damage_confirmed: bool,
    require_videos: bool = False,
    vehicle_plate: str = "",
    vehicle_make: str = "",
    vehicle_model: str = "",
    vehicle_color: str = "",
) -> Inspection:
    tenant_id = _tenant_id(tenant_id)
    inspection = _load_inspection(tenant_id, inspection_id, lock=True)
    if inspection.status == "completed":
        return inspection
    staff = _load_staff(tenant_id, staff_user_id)
    booking = _load_booking(tenant_id, inspection.booking_id)
    expected_status = "reserved" if inspection.phase == "arrival" else "checked_in"
    if booking.status != expected_status:
        raise InspectionConflict(f"{inspection.phase.title()} inspection requires booking status {expected_status}.")
    if (
        isinstance(fuel_level, bool)
        or not isinstance(fuel_level, int)
        or not 0 <= fuel_level <= 100
    ):
        raise InspectionValidationError("Fuel level must be a percentage from 0 to 100.")
    if isinstance(odometer_km, bool) or not isinstance(odometer_km, int) or odometer_km < 0:
        raise InspectionValidationError("Odometer must be a non-negative whole number.")
    if isinstance(key_count, bool) or not isinstance(key_count, int) or key_count < 0:
        raise InspectionValidationError("Key count must be a non-negative whole number.")
    if len(belongings or "") > 2000 or len(notes or "") > 2000:
        raise InspectionValidationError("Inspection notes exceed the allowed length.")
    vehicle_details = {
        "plate": (vehicle_plate or "").strip(),
        "make": (vehicle_make or "").strip(),
        "model": (vehicle_model or "").strip(),
        "color": (vehicle_color or "").strip(),
    }
    if (
        len(vehicle_details["plate"]) > 30
        or len(vehicle_details["make"]) > 60
        or len(vehicle_details["model"]) > 60
        or len(vehicle_details["color"]) > 40
    ):
        raise InspectionValidationError("Vehicle profile details exceed the allowed length.")
    if not isinstance(no_damage_confirmed, bool):
        raise InspectionValidationError("The damage declaration must be explicitly confirmed.")
    if not isinstance(require_videos, bool):
        raise InspectionValidationError("Video evidence requirement must be explicit.")

    photos = db.session.execute(
        select(InspectionPhoto.category).where(
            InspectionPhoto.tenant_id == tenant_id,
            InspectionPhoto.inspection_id == inspection.id,
        )
    ).scalars().all()
    photo_categories = set(photos)
    missing = set(_required_categories(tenant_id)) - photo_categories
    if missing:
        raise InspectionValidationError(
            "Missing required photos: " + ", ".join(sorted(missing)) + "."
        )
    if require_videos:
        video_exists = db.session.execute(
            select(InspectionVideo.id).where(
                InspectionVideo.tenant_id == tenant_id,
                InspectionVideo.inspection_id == inspection.id,
            ).limit(1)
        ).scalar_one_or_none()
        if video_exists is None:
            raise InspectionValidationError("A vehicle video is required.")
    if "signature" not in photo_categories:
        raise InspectionValidationError("A customer signature is required.")

    marks = db.session.execute(
        select(DamageMark).where(
            DamageMark.tenant_id == tenant_id,
            DamageMark.inspection_id == inspection.id,
        )
    ).scalars().all()
    if any(mark.photo_id is None for mark in marks):
        raise InspectionValidationError("Every damage mark requires a photo.")
    if marks and no_damage_confirmed:
        raise InspectionValidationError("Do not confirm 'no damage' while damage marks exist.")
    if not marks and not no_damage_confirmed:
        raise InspectionValidationError("Explicitly confirm 'no damage' or add a damage mark.")

    if inspection.phase == "arrival":
        vehicle = None
        if booking.vehicle_id is not None:
            vehicle = db.session.execute(
                select(Vehicle).where(
                    Vehicle.id == booking.vehicle_id,
                    Vehicle.tenant_id == tenant_id,
                    Vehicle.customer_id == booking.customer_id,
                )
            ).scalar_one_or_none()
            if vehicle is None:
                raise InspectionConflict("The booking vehicle is not owned by its tenant customer.")
        elif vehicle_details["plate"]:
            vehicle = Vehicle(
                tenant_id=tenant_id,
                customer_id=booking.customer_id,
                plate=vehicle_details["plate"],
                make=vehicle_details["make"],
                model=vehicle_details["model"],
                color=vehicle_details["color"],
            )
            db.session.add(vehicle)
            db.session.flush()
            booking.vehicle_id = vehicle.id

        if vehicle is not None:
            for field, value in vehicle_details.items():
                if value:
                    setattr(vehicle, field, value)
            db.session.execute(
                update(InspectionPhoto)
                .where(
                    InspectionPhoto.tenant_id == tenant_id,
                    InspectionPhoto.inspection_id == inspection.id,
                    InspectionPhoto.category.in_(_required_categories(tenant_id)),
                )
                .values(vehicle_id=vehicle.id)
            )

    inspection.draft_details = None  # the final values below replace the autosaved draft
    inspection.staff_user_id = staff.id
    inspection.fuel_level = fuel_level
    inspection.odometer_km = odometer_km
    inspection.key_count = key_count
    inspection.belongings = (belongings or "").strip()
    inspection.notes = (notes or "").strip()
    inspection.no_damage_confirmed = no_damage_confirmed
    inspection.status = "completed"
    inspection.completed_at = utcnow()
    inspection.retention_until = utcnow() + timedelta(
        days=db.session.get(Tenant, tenant_id).inspection_retention_days
    )

    if inspection.phase == "departure":
        arrival = db.session.execute(
            select(Inspection).where(
                Inspection.tenant_id == tenant_id,
                Inspection.booking_id == booking.id,
                Inspection.phase == "arrival",
                Inspection.status == "completed",
            )
        ).scalar_one_or_none()
        if arrival is None:
            raise InspectionConflict("A completed arrival inspection is required before departure.")
        for mark in marks:
            if mark.matched_damage_id is None:
                db.session.add(
                    Dispute(
                        tenant_id=tenant_id,
                        booking_id=booking.id,
                        arrival_inspection_id=arrival.id,
                        departure_inspection_id=inspection.id,
                        damage_mark_id=mark.id,
                        status="open",
                    )
                )
                from .alerts import stage_alert

                stage_alert(tenant_id, "dispute_opened", booking)

    try:
        db.session.flush()
        enqueue_webhook_event(
            tenant_id=tenant_id,
            event_type="inspection.completed",
            data={
                "inspection_id": inspection.id,
                "booking_id": booking.id,
                "phase": inspection.phase,
            },
            commit=False,
        )
        for mark in marks:
            if inspection.phase == "departure" and mark.matched_damage_id is None:
                enqueue_webhook_event(
                    tenant_id=tenant_id,
                    event_type="dispute.opened",
                    data={
                        "booking_id": booking.id,
                        "inspection_id": inspection.id,
                        "damage_mark_id": mark.id,
                    },
                    commit=False,
                )
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise InspectionConflict("Could not complete the inspection or open its damage disputes.") from exc
    return inspection


DRAFT_LIMITS = {
    "fuel_level": 3, "odometer_km": 9, "key_count": 3, "belongings": 2000, "notes": 2000, "no_damage_confirmed": 3,
    "vehicle_plate": 30, "vehicle_make": 60, "vehicle_model": 60, "vehicle_color": 40,
}
DRAFT_NUMBERS = ("fuel_level", "odometer_km", "key_count")


def read_inspection_draft(inspection: Inspection) -> dict:
    """The autosaved handover form of an open inspection (empty when nothing was typed yet)."""
    try:
        data = json.loads(inspection.draft_details or "{}")
    except ValueError:
        return {}
    return {key: value for key, value in data.items() if key in DRAFT_LIMITS and isinstance(value, str)} if isinstance(data, dict) else {}


def save_inspection_draft(*, tenant_id: int, inspection_id: int, staff_user_id: int, values) -> dict:
    """Store what the employee typed so far. Lenient on purpose: strict checks happen when the inspection is completed."""
    tenant_id = _tenant_id(tenant_id)
    inspection = _load_inspection(tenant_id, inspection_id, lock=True)
    _load_staff(tenant_id, staff_user_id)
    if inspection.status != "in_progress":
        raise InspectionConflict("A completed inspection is locked.")
    if not isinstance(values, dict):
        raise InspectionValidationError("Draft details must be an object.")
    clean: dict[str, str] = {}
    for key, limit in DRAFT_LIMITS.items():
        if key not in values:
            continue
        raw = values[key]
        text = ("yes" if raw else "") if isinstance(raw, bool) else str(raw if raw is not None else "")
        text = text.strip()[:limit] if key not in ("belongings", "notes") else text[:limit]
        if key in DRAFT_NUMBERS and not text.isdigit():
            text = ""
        if key == "no_damage_confirmed":
            text = "yes" if text == "yes" else ""
        clean[key] = text
    draft = read_inspection_draft(inspection)
    draft.update(clean)
    inspection.draft_details = json.dumps(draft, ensure_ascii=False)
    db.session.commit()
    return draft


def has_completed_inspection(*, tenant_id: int, booking_id: int, phase: str) -> bool:
    return db.session.execute(
        select(Inspection.id).where(
            Inspection.tenant_id == tenant_id,
            Inspection.booking_id == booking_id,
            Inspection.phase == phase,
            Inspection.status == "completed",
        ).limit(1)
    ).first() is not None


def purge_expired_inspections(*, tenant_id: int, now: datetime | None = None) -> int:
    """Delete expired evidence, retaining all booking evidence while a dispute is open."""
    tenant_id = _tenant_id(tenant_id)
    current_time = now or utcnow()
    open_dispute = (
        select(Dispute.id)
        .where(
            Dispute.tenant_id == tenant_id,
            Dispute.booking_id == Inspection.booking_id,
            Dispute.status == "open",
        )
        .exists()
    )
    inspections = db.session.execute(
        select(Inspection)
        .where(
            Inspection.tenant_id == tenant_id,
            Inspection.status == "completed",
            Inspection.retention_until <= current_time,
            ~open_dispute,
        )
        .order_by(Inspection.id)
    ).scalars().all()
    purged = 0
    for inspection in inspections:
        photos = db.session.execute(
            select(InspectionPhoto).where(
                InspectionPhoto.tenant_id == inspection.tenant_id,
                InspectionPhoto.inspection_id == inspection.id,
            )
        ).scalars().all()
        for photo in photos:
            delete_object(object_key=photo.object_key)
        videos = db.session.execute(
            select(InspectionVideo).where(
                InspectionVideo.tenant_id == inspection.tenant_id,
                InspectionVideo.inspection_id == inspection.id,
            )
        ).scalars().all()
        for video in videos:
            delete_object(object_key=video.object_key)
        db.session.delete(inspection)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Could not delete expired inspection %s after deleting its stored evidence.",
                inspection.id,
            )
            raise
        purged += 1
    return purged


def remove_booking_evidence(*, tenant_id: int, booking_id: int) -> list[str]:
    """Remove every inspection of a booking (photos, damage marks, resolved disputes) from the database.

    Refuses while a dispute is open. Does not commit: the caller commits, then deletes the returned
    storage keys, so a failed commit never leaves rows pointing at missing images.
    """
    tenant_id = _tenant_id(tenant_id)
    open_dispute = db.session.execute(
        select(Dispute.id).where(
            Dispute.tenant_id == tenant_id, Dispute.booking_id == booking_id, Dispute.status == "open",
        ).limit(1)
    ).first()
    if open_dispute is not None:
        raise InspectionConflict("This booking has an open damage dispute. Resolve it first, then delete the booking.")
    for dispute in db.session.execute(
        select(Dispute).where(Dispute.tenant_id == tenant_id, Dispute.booking_id == booking_id)
    ).scalars().all():
        db.session.delete(dispute)
    db.session.flush()
    keys: list[str] = []
    for inspection in db.session.execute(
        select(Inspection).where(Inspection.tenant_id == tenant_id, Inspection.booking_id == booking_id)
    ).scalars().all():
        for photo in db.session.execute(
            select(InspectionPhoto).where(
                InspectionPhoto.tenant_id == tenant_id, InspectionPhoto.inspection_id == inspection.id,
            )
        ).scalars().all():
            keys.append(photo.object_key)
        for video in db.session.execute(
            select(InspectionVideo).where(
                InspectionVideo.tenant_id == tenant_id, InspectionVideo.inspection_id == inspection.id,
            )
        ).scalars().all():
            keys.append(video.object_key)
        db.session.delete(inspection)
    db.session.flush()
    return keys
