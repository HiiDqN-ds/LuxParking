"""Versioned, tenant-scoped JSON API for customer integrations."""
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, Response, abort, current_app, g, jsonify, make_response, redirect, request
from sqlalchemy import func, select

from ..api_spec import OPENAPI_SPEC
from ..extensions import csrf, db
from ..models import (
    ApiKey,
    Booking,
    Hold,
    IdempotencyRecord,
    Inspection,
    Lot,
    Space,
    Tenant,
    UsageLog,
    WebhookDelivery,
    WebhookEndpoint,
)
from ..services.api_platform import (
    ApiPlatformError,
    WEBHOOK_EVENTS,
    create_webhook_endpoint,
    issue_api_key,
    webhook_signing_secret,
)
from ..services.availability import (
    AvailabilityError,
    PricingUnavailable,
    lot_availability,
    parse_aware_datetime,
    quote_space,
)
from ..services.pricing import DiscountUnavailable, quote_discount
from ..services import billing
from ..models import Extra
from ..services.bookings import BookingServiceError
from ..services.booking_admin import add_extra, extra_cost, validate_persons
from ..services.booking_events import notify_booking_event
from ..services.invoices import invoice_filename, invoice_pdf, latest_invoice, sync_invoices
from ..services.bookings import (
    BookingConflict,
    BookingNotFound,
    BookingValidationError,
    cancel_booking,
    check_in_booking,
    check_out_booking,
    create_hold,
    create_booking_from_hold,
    create_payment_record,
    update_payment_status,
)
from ..services.inspections import (
    InspectionConflict,
    InspectionError,
    InspectionNotFound,
    InspectionValidationError,
    complete_inspection,
    create_inspection,
    upload_inspection_photo,
)

bp = Blueprint("api", __name__, url_prefix="/v1")
csrf.exempt(bp)


def _error(code: str, message: str, status: int):
    error = {"code": code, "message": message}
    request_id = getattr(g, "api_request_id", None)
    if request_id:
        error["request_id"] = request_id
    return jsonify(error=error), status


def _api_key() -> ApiKey:
    key = getattr(g, "api_key", None)
    if key is None:
        abort(401)
    return key


def _require_scope(scope: str) -> ApiKey:
    key = _api_key()
    scopes = set(key.scopes or ())
    if "*" not in scopes and scope not in scopes:
        abort(403, description=f"API key requires the {scope} scope.")
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


def _start_api_request():
    g.api_started_at = time.perf_counter()
    g.api_key = None
    g.api_idempotency_id = None  # never inherit per-request state (matters when a context is reused, e.g. in tests)
    g.api_idempotent_replay = False
    sent = request.headers.get("X-Request-Id", "")
    g.api_request_id = sent if _REQUEST_ID.match(sent) else "req_" + uuid.uuid4().hex[:24]  # a caller's id is kept for tracing
    if request.method == "OPTIONS":
        return None
    if request.endpoint in {"api.openapi", "api.docs", "api.health"}:
        return None

    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or "." not in token:
        return _error("authentication_required", "A valid Bearer API key is required.", 401)
    prefix, secret = token.split(".", 1)
    if not secret or not prefix.startswith(("pk_", "sk_")):
        return _error("invalid_api_key", "The API key is invalid or revoked.", 401)
    key = db.session.execute(
        select(ApiKey)
        .where(ApiKey.prefix == prefix, ApiKey.active.is_(True))
        .with_for_update()
    ).scalar_one_or_none()
    provided_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if (
        key is None
        or key.revoked_at is not None
        or not hmac.compare_digest(key.token_hash, provided_hash)
    ):
        return _error("invalid_api_key", "The API key is invalid or revoked.", 401)
    if prefix[:2] != key.key_type:
        return _error("invalid_api_key", "The API key type is invalid.", 401)
    tenant = db.session.get(Tenant, key.tenant_id)
    if tenant is None or tenant.status == "suspended":
        return _error("tenant_suspended", "This tenant account is not active.", 403)

    origin = request.headers.get("Origin")
    if origin and origin not in (key.allowed_origins or ()):
        return _error("origin_not_allowed", "This request origin is not allowed for the API key.", 403)
    g.api_key = key
    g.tenant_id = key.tenant_id

    period_start = _now() - timedelta(minutes=1)
    request_count = db.session.execute(
        select(func.count(UsageLog.id)).where(
            UsageLog.tenant_id == key.tenant_id,
            UsageLog.api_key_id == key.id,
            UsageLog.created_at >= period_start,
        )
    ).scalar_one()
    if request_count >= key.requests_per_minute:
        response = _error("rate_limit_exceeded", "The API key request limit was exceeded.", 429)
        response[0].headers["Retry-After"] = "60"
        return response
    g.api_request_count = request_count
    blocked = billing.api_blocked_reason(key.tenant_id)
    if blocked:
        return _error("plan_limit_reached", blocked, 402)

    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            if len(idempotency_key) < 8 or len(idempotency_key) > 200:
                return _error("invalid_idempotency_key", "Idempotency-Key must be 8 to 200 characters.", 400)
            now = _now()
            db.session.execute(
                db.delete(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == key.tenant_id,
                    IdempotencyRecord.api_key_id == key.id,
                    IdempotencyRecord.expires_at <= now,
                )
            )
            request_hash = hashlib.sha256(
                request.method.encode()
                + b"\0"
                + request.path.encode()
                + b"\0"
                + request.query_string
                + b"\0"
                + request.get_data(cache=True)
            ).hexdigest()
            key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
            existing = db.session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == key.tenant_id,
                    IdempotencyRecord.api_key_id == key.id,
                    IdempotencyRecord.key_hash == key_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                db.session.commit()
                if not hmac.compare_digest(existing.request_hash, request_hash):
                    return _error(
                        "idempotency_key_reused",
                        "The Idempotency-Key was already used with a different request.",
                        409,
                    )
                if existing.status_code is None or existing.response_body is None:
                    return _error("request_in_progress", "A request with this key is still processing.", 409)
                response = make_response(existing.response_body, existing.status_code)
                response.headers["Content-Type"] = existing.content_type or "application/json"
                response.headers["Idempotent-Replay"] = "true"
                g.api_idempotent_replay = True
                return response
            record = IdempotencyRecord(
                tenant_id=key.tenant_id,
                api_key_id=key.id,
                key_hash=key_hash,
                request_hash=request_hash,
                expires_at=now + timedelta(hours=current_app.config["API_IDEMPOTENCY_HOURS"]),
            )
            db.session.add(record)
            db.session.commit()
            g.api_idempotency_id = record.id
    return None


@bp.before_request
def authenticate_api_request():
    return _start_api_request()


@bp.after_request
def record_api_usage(response):
    response.headers["X-Request-Id"] = getattr(g, "api_request_id", "") or "req_" + uuid.uuid4().hex[:24]
    response.headers["X-ParkPilot-API-Version"] = "1"
    origin = request.headers.get("Origin")
    key = getattr(g, "api_key", None)
    if request.method == "OPTIONS":
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization,Content-Type,Idempotency-Key"
        response.headers["Access-Control-Max-Age"] = "600"
        return response
    if key is None or getattr(g, "api_idempotent_replay", False):
        if origin and key is not None and origin in (key.allowed_origins or ()):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    key.last_used_at = _now()
    elapsed_ms = max(0, int((time.perf_counter() - g.api_started_at) * 1000))
    db.session.add(
        UsageLog(
            tenant_id=key.tenant_id,
            api_key_id=key.id,
            method=request.method,
            path=request.path[:255],
            status_code=response.status_code,
            duration_ms=elapsed_ms,
        )
    )
    idempotency_id = getattr(g, "api_idempotency_id", None)
    if idempotency_id is not None:
        record = db.session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.id == idempotency_id,
                IdempotencyRecord.tenant_id == key.tenant_id,
                IdempotencyRecord.api_key_id == key.id,
            )
        ).scalar_one_or_none()
        if record is not None:
            record.status_code = response.status_code
            record.content_type = response.content_type
            record.response_body = response.get_data(as_text=True)[:1_000_000]
    db.session.commit()
    response.headers["X-RateLimit-Limit"] = str(key.requests_per_minute)
    response.headers["X-RateLimit-Remaining"] = str(
        max(0, key.requests_per_minute - getattr(g, "api_request_count", 0) - 1)
    )
    if origin and origin in (key.allowed_origins or ()):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


def _json_object() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        abort(400, description="A JSON object request body is required.")
    return value


def _allowed_lot_ids(key) -> set[int] | None:
    """Lots this key may use. None means every lot of the tenant."""
    ids = getattr(key, "allowed_lot_ids", None)
    return {int(i) for i in ids} if ids else None


def _lot_for(tenant_id: int, lot_id: int) -> Lot:
    lot = db.session.execute(
        select(Lot).where(
            Lot.id == lot_id,
            Lot.tenant_id == tenant_id,
            Lot.active.is_(True),
        )
    ).scalar_one_or_none()
    if lot is None:
        abort(404)
    allowed = _allowed_lot_ids(getattr(g, "api_key", None))
    if allowed is not None and lot.id not in allowed:
        abort(404)
    return lot


def _booking_for(tenant_id: int, booking_id: int) -> Booking:
    booking = db.session.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if booking is None:
        abort(404)
    allowed = _allowed_lot_ids(getattr(g, "api_key", None))
    if allowed is not None and booking.lot_id not in allowed:
        abort(404)
    return booking


def _guard_inspection(key, inspection_id: int) -> None:
    inspection = db.session.execute(
        select(Inspection).where(Inspection.id == inspection_id, Inspection.tenant_id == key.tenant_id)
    ).scalar_one_or_none()
    if inspection is None:
        abort(404)
    _booking_for(key.tenant_id, inspection.booking_id)


def _serialize_booking(booking: Booking) -> dict:
    return {
        "id": booking.id,
        "booking_ref": booking.booking_ref,
        "lot_id": booking.lot_id,
        "space_id": booking.space_id,
        "vehicle_id": booking.vehicle_id,
        "status": booking.status,
        "payment_status": booking.payment_status,
        "pickup_at": booking.pickup_at.isoformat(),
        "return_at": booking.return_at.isoformat(),
        "total_amount": f"{booking.total_amount:.2f}",
        "currency": booking.currency,
        "created_at": booking.created_at.isoformat(),
        "extras": [
            {"name": row.name, "kind": row.kind, "quantity": row.quantity, "total": f"{row.total():.2f}"}
            for row in booking.extras if row.status != "cancelled"
        ],
    }


def _serialize_inspection(inspection: Inspection) -> dict:
    return {
        "id": inspection.id,
        "booking_id": inspection.booking_id,
        "phase": inspection.phase,
        "status": inspection.status,
        "staff_user_id": inspection.staff_user_id,
        "photo_categories": [photo.category for photo in inspection.photos],
        "completed_at": inspection.completed_at.isoformat() if inspection.completed_at else None,
    }


@bp.get("/openapi.json")
def openapi():
    return jsonify(OPENAPI_SPEC)


@bp.get("/health")
def health():
    """Unauthenticated liveness probe for uptime monitoring."""
    return jsonify(status="ok", api_version="1", time=_now().isoformat())


@bp.get("/docs")
def docs():
    """The interactive reference lives on the developer portal and needs no third-party script."""
    return redirect("/developers/reference", 301)


@bp.get("/lots/<int:lot_id>/map")
def lot_map(lot_id: int):
    key = _require_scope("map:read")
    lot = _lot_for(key.tenant_id, lot_id)
    start = parse_aware_datetime(request.args.get("from", ""), "from")
    end = parse_aware_datetime(request.args.get("to", ""), "to")
    spaces = lot_availability(
        tenant_id=key.tenant_id,
        lot_id=lot.id,
        start=start,
        end=end,
    )
    return jsonify(lot={"id": lot.id, "name": lot.name, "currency": lot.currency}, spaces=spaces)


@bp.get("/availability")
def availability():
    key = _require_scope("availability:read")
    lot_id = request.args.get("lot_id", type=int)
    if lot_id is None:
        abort(400, description="lot_id is required.")
    lot = _lot_for(key.tenant_id, lot_id)
    start = parse_aware_datetime(request.args.get("from", ""), "from")
    end = parse_aware_datetime(request.args.get("to", ""), "to")
    return jsonify(spaces=lot_availability(tenant_id=key.tenant_id, lot_id=lot.id, start=start, end=end))


@bp.get("/products")
def products():
    key = _require_scope("products:read")
    lots = db.session.execute(
        select(Lot).where(Lot.tenant_id == key.tenant_id, Lot.active.is_(True)).order_by(Lot.id)
    ).scalars().all()
    allowed = _allowed_lot_ids(key)
    if allowed is not None:
        lots = [lot for lot in lots if lot.id in allowed]
    products_list = []
    for lot in lots:
        spaces = db.session.execute(
            select(Space).where(
                Space.tenant_id == key.tenant_id,
                Space.lot_id == lot.id,
                Space.status != "blocked",
                Space.default_available.is_(True),
            ).order_by(Space.id)
        ).scalars().all()
        for space in spaces:
            effective_rate = space.base_price if space.base_price and space.base_price > 0 else lot.base_daily_rate
            products_list.append(
                {
                    "id": space.id,
                    "lot_id": lot.id,
                    "name": space.name,
                    "kind": space.kind,
                    "covered": space.covered,
                    "ev_charging": space.ev_charging,
                    "daily_rate": f"{Decimal(effective_rate):.2f}" if effective_rate and effective_rate > 0 else None,
                    "currency": lot.currency,
                }
            )
    return jsonify(products=products_list)


def _extras_from_request(tenant_id: int, lot_id: int, raw) -> list[tuple]:
    """Validate the optional "extras" list of a quote/booking: [{"id": 3, "quantity": 2, "details": "LH123"}]."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > 10 or not all(isinstance(item, dict) for item in raw):
        raise ApiPlatformError("extras must be a list of objects with id and optional quantity and details.")
    chosen, kinds = [], set()
    for item in raw:
        try:
            extra_id, quantity = int(item["id"]), int(item.get("quantity", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiPlatformError("Every extra needs a numeric id and a whole-number quantity.") from exc
        extra = db.session.execute(
            select(Extra).where(Extra.id == extra_id, Extra.tenant_id == tenant_id, Extra.lot_id == lot_id, Extra.active.is_(True))
        ).scalar_one_or_none()
        if extra is None:
            raise ApiPlatformError(f"Extra {extra_id} is not available for this parking lot.")
        if extra.kind in kinds:
            raise ApiPlatformError("Choose only one extra of each kind.")
        kinds.add(extra.kind)
        try:
            validate_persons(extra, quantity)
        except BookingServiceError as exc:
            raise ApiPlatformError(str(exc)) from exc
        chosen.append((extra, quantity, str(item.get("details", ""))[:255]))
    return chosen


@bp.get("/extras")
def extras():
    key = _require_scope("products:read")
    lot_id = request.args.get("lot_id", type=int)
    if lot_id is None:
        abort(400, description="lot_id is required.")
    lot = _lot_for(key.tenant_id, lot_id)
    rows = db.session.execute(
        select(Extra).where(Extra.tenant_id == key.tenant_id, Extra.lot_id == lot.id, Extra.active.is_(True)).order_by(Extra.kind, Extra.name)
    ).scalars().all()
    return jsonify(extras=[
        {
            "id": row.id, "kind": row.kind, "name": row.name, "description": row.description or "",
            "price": f"{row.price:.2f}", "currency": lot.currency, "per_person": bool(row.per_person),
            "included_persons": row.included_persons or 0,
            "extra_person_price": f"{row.extra_person_price:.2f}" if row.extra_person_price is not None else None,
            "max_persons": row.max_persons if row.per_person else None,
        }
        for row in rows
    ])


@bp.post("/quotes")
def quote():
    key = _require_scope("quotes:write")
    data = _json_object()
    try:
        lot_id, space_id = int(data["lot_id"]), int(data["space_id"])
        start = parse_aware_datetime(data["from"], "from")
        end = parse_aware_datetime(data["to"], "to")
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("lot_id, space_id, from, and to are required.") from exc
    lot = _lot_for(key.tenant_id, lot_id)
    space = db.session.execute(
        select(Space).where(
            Space.id == space_id,
            Space.tenant_id == key.tenant_id,
            Space.lot_id == lot.id,
        )
    ).scalar_one_or_none()
    if space is None:
        abort(404)
    subtotal, currency = quote_space(space, lot, start, end)
    chosen_extras = _extras_from_request(key.tenant_id, lot.id, data.get("extras"))
    extras_sum = sum((extra_cost(extra, quantity) for extra, quantity, _ in chosen_extras), Decimal(0))
    quote = {"subtotal": f"{subtotal:.2f}", "discount": "0.00", "total": f"{subtotal:.2f}"}
    discount_code = data.get("discount_code")
    if discount_code is not None and discount_code != "":
        if not isinstance(discount_code, str):
            return _error("invalid_discount", "discount_code must be a string.", 400)
        try:
            discount = quote_discount(
                tenant_id=key.tenant_id,
                code=discount_code,
                subtotal=subtotal,
            )
        except DiscountUnavailable as exc:
            return _error("invalid_discount", str(exc), 400)
        quote.update(
            code=discount.code,
            discount=f"{discount.discount:.2f}",
            total=f"{discount.total:.2f}",
        )
    quote["extras_total"] = f"{extras_sum:.2f}"
    quote["total"] = f"{Decimal(quote['total']) + extras_sum:.2f}"
    return jsonify(
        quote={
            **quote,
            "lot_id": lot.id,
            "space_id": space.id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "currency": currency,
        }
    )


@bp.post("/holds")
def holds():
    key = _require_scope("holds:write")
    data = _json_object()
    try:
        lot_id = int(data["lot_id"])
        space_id = int(data["space_id"])
        start = parse_aware_datetime(data["from"], "from")
        end = parse_aware_datetime(data["to"], "to")
        lot = _lot_for(key.tenant_id, lot_id)
        hold = create_hold(
            tenant_id=key.tenant_id,
            lot_id=lot.id,
            space_id=space_id,
            pickup_at=start,
            return_at=end,
            customer_email=str(data["email"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("lot_id, space_id, from, to, and email are required.") from exc
    return jsonify(hold={"id": hold.id, "space_id": hold.space_id, "expires_at": hold.expires_at.isoformat(), "status": hold.status}), 201


@bp.post("/bookings")
def create_booking():
    key = _require_scope("bookings:write")
    data = _json_object()
    if data.get("test_payment") is True and current_app.config.get("APP_ENV") == "production":
        return _error("test_payment_disabled", "Test payments are disabled in production.", 403)
    try:
        hold_id = int(data["hold_id"])
        hold = db.session.execute(
            select(Hold).where(Hold.id == hold_id, Hold.tenant_id == key.tenant_id)
        ).scalar_one_or_none()
        if hold is None:
            abort(404)
        space = db.session.execute(
            select(Space).where(
                Space.id == hold.space_id,
                Space.lot_id == hold.lot_id,
                Space.tenant_id == key.tenant_id,
            )
        ).scalar_one_or_none()
        lot = _lot_for(key.tenant_id, hold.lot_id)
        if space is None:
            abort(404)
        chosen_extras = _extras_from_request(key.tenant_id, lot.id, data.get("extras"))
        subtotal, currency = quote_space(
            space,
            lot,
            _stored_utc(hold.pickup_at),
            _stored_utc(hold.return_at),
        )
        discount_code = data.get("discount_code")
        if discount_code is not None and discount_code != "":
            if not isinstance(discount_code, str):
                return _error("invalid_discount", "discount_code must be a string.", 400)
            discount = quote_discount(
                tenant_id=key.tenant_id,
                code=discount_code,
                subtotal=subtotal,
            )
            total = discount.total
            normalized_discount_code = discount.code
        else:
            total = subtotal
            normalized_discount_code = None
        booking = create_booking_from_hold(
            tenant_id=key.tenant_id,
            hold_id=hold.id,
            first_name=str(data["first_name"]),
            last_name=str(data.get("last_name", "")),
            email=str(data["email"]),
            phone=str(data["phone"]),
            vehicle_plate=str(data.get("vehicle_plate", "")),
            vehicle_make=str(data.get("vehicle_make", "")),
            vehicle_model=str(data.get("vehicle_model", "")),
            notes=str(data.get("notes", "")),
            discount_code=normalized_discount_code,
            discount_base_amount=subtotal if normalized_discount_code else None,
            total_amount=total,
            currency=currency,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("hold_id, first_name, email, and phone are required.") from exc
    if chosen_extras:
        for extra, quantity, details in chosen_extras:
            add_extra(booking, extra, details, quantity)
        db.session.commit()
    if data.get("send_notifications", True) is not False:  # partners can opt out and message customers themselves
        notify_booking_event(booking.id, key.tenant_id, "reserved")
    if data.get("test_payment") is True:
        payment = create_payment_record(
            tenant_id=key.tenant_id,
            booking_id=booking.id,
            provider="manual",
            amount=booking.total_amount,
            currency=booking.currency,
            provider_ref=f"test_{secrets.token_hex(8)}",
        )
        payment = update_payment_status(tenant_id=key.tenant_id, payment_id=payment.id, status="paid")
    return jsonify(booking=_serialize_booking(booking)), 201


@bp.get("/bookings")
def list_bookings():
    key = _require_scope("bookings:read")
    limit = min(max(request.args.get("limit", default=50, type=int) or 50, 1), 100)
    query = select(Booking).where(Booking.tenant_id == key.tenant_id)
    allowed = _allowed_lot_ids(key)
    if allowed is not None:
        query = query.where(Booking.lot_id.in_(allowed))
    bookings = db.session.execute(
        query.order_by(Booking.created_at.desc(), Booking.id.desc()).limit(limit)
    ).scalars().all()
    return jsonify(bookings=[_serialize_booking(booking) for booking in bookings])


@bp.get("/bookings/<int:booking_id>")
def get_booking(booking_id: int):
    key = _require_scope("bookings:read")
    return jsonify(booking=_serialize_booking(_booking_for(key.tenant_id, booking_id)))


@bp.get("/bookings/<int:booking_id>/invoice")
def booking_invoice(booking_id: int):
    """The booking's invoice as a PDF (issued on first request if it does not exist yet)."""
    key = _require_scope("bookings:read")
    booking = _booking_for(key.tenant_id, booking_id)
    invoice = sync_invoices(booking)
    db.session.commit()
    if invoice is None:
        abort(404)
    return Response(invoice_pdf(invoice), mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{invoice_filename(invoice)}"', "Cache-Control": "private, no-store"})


@bp.get("/bookings/<int:booking_id>/credit-note")
def booking_credit_note(booking_id: int):
    key = _require_scope("bookings:read")
    booking = _booking_for(key.tenant_id, booking_id)
    note = latest_invoice(booking, "credit_note")
    if note is None:
        abort(404)
    return Response(invoice_pdf(note), mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{invoice_filename(note)}"', "Cache-Control": "private, no-store"})


@bp.patch("/bookings/<int:booking_id>")
def patch_booking(booking_id: int):
    key = _require_scope("bookings:write")
    data = _json_object()
    if set(data) - {"notes"} or not isinstance(data.get("notes"), str) or len(data["notes"]) > 10000:
        return _error("invalid_request", "Only a notes string up to 10000 characters can be updated.", 400)
    booking = _booking_for(key.tenant_id, booking_id)
    if booking.status != "reserved":
        return _error("invalid_state", "Only reserved bookings can be updated.", 409)
    booking.notes = data["notes"]
    db.session.commit()
    return jsonify(booking=_serialize_booking(booking))


@bp.delete("/bookings/<int:booking_id>")
def delete_booking(booking_id: int):
    key = _require_scope("bookings:write")
    _booking_for(key.tenant_id, booking_id)
    booking = cancel_booking(tenant_id=key.tenant_id, booking_id=booking_id)
    return jsonify(booking=_serialize_booking(booking))


@bp.post("/bookings/<int:booking_id>/check-in")
def check_in(booking_id: int):
    key = _require_scope("bookings:write")
    _booking_for(key.tenant_id, booking_id)
    booking = check_in_booking(tenant_id=key.tenant_id, booking_id=booking_id)
    return jsonify(booking=_serialize_booking(booking))


@bp.post("/bookings/<int:booking_id>/check-out")
def check_out(booking_id: int):
    key = _require_scope("bookings:write")
    _booking_for(key.tenant_id, booking_id)
    booking = check_out_booking(tenant_id=key.tenant_id, booking_id=booking_id)
    return jsonify(booking=_serialize_booking(booking))


@bp.route("/bookings/<int:booking_id>/inspections", methods=["GET", "POST"])
def booking_inspections(booking_id: int):
    key = _api_key()
    if request.method == "GET":
        _require_scope("inspections:read")
        _booking_for(key.tenant_id, booking_id)
        records = db.session.execute(
            select(Inspection).where(
                Inspection.tenant_id == key.tenant_id,
                Inspection.booking_id == booking_id,
            ).order_by(Inspection.id)
        ).scalars().all()
        return jsonify(inspections=[_serialize_inspection(record) for record in records])

    _require_scope("inspections:write")
    data = _json_object()
    if data.get("operation") == "complete":
        inspection_id = data.get("inspection_id")
        if not isinstance(inspection_id, int):
            return _error("invalid_request", "inspection_id is required.", 400)
        _guard_inspection(key, inspection_id)
        inspection = complete_inspection(
            tenant_id=key.tenant_id,
            inspection_id=inspection_id,
            staff_user_id=int(data.get("staff_user_id", 0)),
            fuel_level=data.get("fuel_level"),
            odometer_km=data.get("odometer_km"),
            key_count=data.get("key_count"),
            belongings=str(data.get("belongings", "")),
            notes=str(data.get("notes", "")),
            no_damage_confirmed=data.get("no_damage_confirmed") is True,
        )
        return jsonify(inspection=_serialize_inspection(inspection))
    _booking_for(key.tenant_id, booking_id)
    try:
        inspection = create_inspection(
            tenant_id=key.tenant_id,
            booking_id=booking_id,
            phase=str(data["phase"]),
            staff_user_id=int(data["staff_user_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("phase and staff_user_id are required.") from exc
    return jsonify(inspection=_serialize_inspection(inspection)), 201


@bp.post("/inspections/<int:inspection_id>/photos")
def inspection_photo(inspection_id: int):
    key = _require_scope("inspections:write")
    _guard_inspection(key, inspection_id)
    try:
        captured_at = parse_aware_datetime(request.form.get("captured_at", ""), "captured_at")
        file = request.files["photo"]
        photo = upload_inspection_photo(
            tenant_id=key.tenant_id,
            inspection_id=inspection_id,
            staff_user_id=int(request.form.get("staff_user_id", "")),
            category=request.form.get("category", ""),
            file_data=file.read(),
            content_type=file.mimetype or "",
            captured_at=captured_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("photo, category, captured_at, and staff_user_id are required.") from exc
    return jsonify(photo={"id": photo.id, "category": photo.category, "sha256": photo.sha256, "size_bytes": photo.size_bytes}), 201


@bp.route("/api-keys", methods=["GET", "POST"])
def api_keys():
    key = _require_scope("keys:manage")
    if request.method == "GET":
        keys = db.session.execute(
            select(ApiKey).where(ApiKey.tenant_id == key.tenant_id).order_by(ApiKey.created_at.desc())
        ).scalars().all()
        return jsonify(
            api_keys=[
                {
                    "id": item.id,
                    "name": item.name,
                    "prefix": item.prefix,
                    "type": item.key_type,
                    "scopes": item.scopes,
                    "allowed_origins": item.allowed_origins,
                    "requests_per_minute": item.requests_per_minute,
                    "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None,
                    "active": item.active,
                }
                for item in keys
            ]
        )
    data = _json_object()
    try:
        issued, token = issue_api_key(
            tenant_id=key.tenant_id,
            name=str(data["name"]),
            key_type=str(data["type"]),
            scopes=data["scopes"],
            allowed_origins=data.get("allowed_origins", []),
            requests_per_minute=int(data.get("requests_per_minute", current_app.config["API_DEFAULT_REQUESTS_PER_MINUTE"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("name, type, and scopes are required.") from exc
    return jsonify(
        api_key={
            "id": issued.id,
            "name": issued.name,
            "prefix": issued.prefix,
            "type": issued.key_type,
            "scopes": issued.scopes,
            "allowed_origins": issued.allowed_origins,
            "requests_per_minute": issued.requests_per_minute,
            "token": token,
            "warning": "The token is returned once. Store it securely; it cannot be retrieved again.",
        }
    ), 201


@bp.delete("/api-keys/<int:key_id>")
def revoke_api_key(key_id: int):
    actor = _require_scope("keys:manage")
    key = db.session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == actor.tenant_id)
    ).scalar_one_or_none()
    if key is None:
        abort(404)
    if key.id == actor.id:
        return _error("invalid_state", "An API key cannot revoke itself.", 409)
    key.active = False
    key.revoked_at = _now()
    db.session.commit()
    return jsonify(revoked=True, prefix=key.prefix)


@bp.post("/api-keys/<int:key_id>/rotate")
def rotate_api_key(key_id: int):
    actor = _require_scope("keys:manage")
    old_key = db.session.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.tenant_id == actor.tenant_id,
            ApiKey.active.is_(True),
        )
    ).scalar_one_or_none()
    if old_key is None:
        abort(404)
    if old_key.id == actor.id:
        return _error(
            "invalid_state",
            "Use a different active key to rotate this key.",
            409,
        )
    replacement, token = issue_api_key(
        tenant_id=actor.tenant_id,
        name=old_key.name,
        key_type=old_key.key_type,
        scopes=list(old_key.scopes or ()),
        allowed_origins=list(old_key.allowed_origins or ()),
        requests_per_minute=old_key.requests_per_minute,
    )
    old_key.active = False
    old_key.revoked_at = _now()
    db.session.commit()
    return jsonify(
        api_key={
            "id": replacement.id,
            "name": replacement.name,
            "prefix": replacement.prefix,
            "type": replacement.key_type,
            "scopes": replacement.scopes,
            "allowed_origins": replacement.allowed_origins,
            "requests_per_minute": replacement.requests_per_minute,
            "token": token,
            "warning": "The token is returned once. Store it securely; it cannot be retrieved again.",
        }
    )


@bp.route("/webhooks", methods=["GET", "POST"])
def webhooks():
    key = _require_scope("webhooks:manage")
    if request.method == "GET":
        endpoints = db.session.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.tenant_id == key.tenant_id)
            .order_by(WebhookEndpoint.created_at.desc())
        ).scalars().all()
        return jsonify(
            webhooks=[
                {"id": endpoint.id, "name": endpoint.name, "url": endpoint.url, "events": endpoint.events, "active": endpoint.active}
                for endpoint in endpoints
            ],
            supported_events=sorted(WEBHOOK_EVENTS),
        )
    data = _json_object()
    try:
        endpoint, signing_secret = create_webhook_endpoint(
            tenant_id=key.tenant_id,
            name=str(data["name"]),
            url=str(data["url"]),
            events=data["events"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiPlatformError("name, url, and events are required.") from exc
    return jsonify(
        webhook={
            "id": endpoint.id,
            "name": endpoint.name,
            "url": endpoint.url,
            "events": endpoint.events,
            "signing_secret": signing_secret,
            "warning": "The signing secret is returned once. Rotate it if lost.",
        }
    ), 201


@bp.post("/webhooks/<int:endpoint_id>/rotate-secret")
def rotate_webhook_secret(endpoint_id: int):
    key = _require_scope("webhooks:manage")
    endpoint = db.session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.tenant_id == key.tenant_id,
            WebhookEndpoint.active.is_(True),
        )
    ).scalar_one_or_none()
    if endpoint is None:
        abort(404)
    endpoint.signing_salt = secrets.token_hex(32)
    db.session.commit()
    return jsonify(
        id=endpoint.id,
        signing_secret=webhook_signing_secret(endpoint),
        warning="The signing secret is returned once. Update the receiver before retrying deliveries.",
    )


@bp.delete("/webhooks/<int:endpoint_id>")
def revoke_webhook(endpoint_id: int):
    key = _require_scope("webhooks:manage")
    endpoint = db.session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.tenant_id == key.tenant_id,
        )
    ).scalar_one_or_none()
    if endpoint is None:
        abort(404)
    endpoint.active = False
    endpoint.revoked_at = _now()
    db.session.commit()
    return jsonify(revoked=True, id=endpoint.id)


@bp.get("/webhook-deliveries")
def webhook_deliveries():
    key = _require_scope("webhooks:manage")
    limit = min(max(request.args.get("limit", default=50, type=int) or 50, 1), 100)
    deliveries = db.session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.tenant_id == key.tenant_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return jsonify(
        deliveries=[
            {
                "id": item.event_id,
                "endpoint_id": item.endpoint_id,
                "event": item.event_type,
                "status": item.status,
                "attempts": item.attempts,
                "last_status_code": item.last_status_code,
                "last_error": item.last_error,
                "next_attempt_at": item.next_attempt_at.isoformat(),
                "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
            }
            for item in deliveries
        ]
    )


@bp.get("/usage")
def usage():
    key = _require_scope("usage:read")
    rows = db.session.execute(
        select(UsageLog)
        .where(UsageLog.tenant_id == key.tenant_id, UsageLog.api_key_id == key.id)
        .order_by(UsageLog.created_at.desc())
        .limit(100)
    ).scalars().all()
    return jsonify(
        usage=[
            {
                "method": row.method,
                "path": row.path,
                "status": row.status_code,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    )


@bp.errorhandler(400)
def bad_request(error):
    return _error("invalid_request", getattr(error, "description", "The request is invalid."), 400)


@bp.errorhandler(401)
def unauthorized(error):
    return _error("authentication_required", getattr(error, "description", "Authentication is required."), 401)


@bp.errorhandler(403)
def forbidden(error):
    return _error("forbidden", getattr(error, "description", "The API key is not allowed to perform this action."), 403)


@bp.errorhandler(404)
def not_found(_):
    return _error("not_found", "The requested resource was not found.", 404)


@bp.errorhandler(ApiPlatformError)
def api_platform_error(error):
    return _error("invalid_request", str(error), 400)


@bp.errorhandler(AvailabilityError)
@bp.errorhandler(BookingValidationError)
@bp.errorhandler(InspectionValidationError)
def validation_error(error):
    return _error("invalid_request", str(error), 400)


@bp.errorhandler(PricingUnavailable)
def pricing_error(error):
    return _error("pricing_unavailable", str(error), 409)


@bp.errorhandler(billing.PlanLimitError)
def plan_limit_error(error):
    return _error("plan_limit_reached", str(error), 402)


@bp.errorhandler(DiscountUnavailable)
def discount_error(error):
    return _error("invalid_discount", str(error), 400)


@bp.errorhandler(BookingConflict)
@bp.errorhandler(InspectionConflict)
def conflict_error(error):
    return _error("conflict", str(error), 409)


@bp.errorhandler(BookingNotFound)
@bp.errorhandler(InspectionNotFound)
def service_not_found(_):
    return _error("not_found", "The requested resource was not found.", 404)


@bp.errorhandler(InspectionError)
def inspection_error(error):
    return _error("inspection_error", str(error), 400)


@bp.errorhandler(Exception)
def unexpected_error(error):
    current_app.logger.exception("Unhandled ParkPilot API error", exc_info=error)
    return _error("internal_error", "An unexpected API error occurred.", 500)
