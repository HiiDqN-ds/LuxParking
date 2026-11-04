"""Tenant-scoped booking, hold, and payment lifecycle operations."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ..extensions import db
from ..models import Booking, Customer, Hold, Lot, Payment, Space, Vehicle, money
from .api_platform import enqueue_webhook_event
from .live_updates import notify_lot_availability
from .inspections import has_completed_inspection
from .pricing import DiscountUnavailable, quote_discount, redeem_discount


class BookingServiceError(Exception):
    """Base exception for expected booking-service failures."""


class BookingNotFound(BookingServiceError):
    """Raised when a requested resource is outside the tenant or does not exist."""


class BookingConflict(BookingServiceError):
    """Raised when a resource is already reserved or its state conflicts."""


class BookingValidationError(BookingServiceError):
    """Raised when service input is invalid."""


class InvalidBookingTransition(BookingConflict):
    """Raised when a booking or payment cannot move to the requested state."""


def _tenant_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BookingValidationError("A positive tenant_id is required.")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BookingValidationError(f"{field} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    """SQLite drops timezone metadata; persisted service timestamps are stored as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _interval(pickup_at: datetime, return_at: datetime) -> tuple[datetime, datetime]:
    start = _utc(pickup_at, "pickup_at")
    end = _utc(return_at, "return_at")
    if start <= datetime.now(timezone.utc):
        raise BookingValidationError("Pick-up must be in the future.")
    if start >= end:
        raise BookingValidationError("return_at must be later than pickup_at.")
    return start, end


def _email(value: str) -> str:
    normalized = (value or "").strip().casefold()
    if not normalized or len(normalized) > 255 or normalized.count("@") != 1 or any(c.isspace() for c in normalized):
        raise BookingValidationError("A valid customer email is required.")
    local, domain = normalized.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise BookingValidationError("A valid customer email is required.")
    return normalized


def _amount(value: Decimal | int | float | str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BookingValidationError("The amount must be a valid non-negative number.") from None
    if not decimal_value.is_finite() or decimal_value < 0:
        raise BookingValidationError("The amount must be a valid non-negative number.")
    return money(decimal_value)


def _load_lot(tenant_id: int, lot_id: int) -> Lot:
    lot = db.session.execute(
        select(Lot).where(Lot.id == lot_id, Lot.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if lot is None:
        raise BookingNotFound("Parking lot not found.")
    if not lot.active:
        raise BookingConflict("The parking lot is not accepting reservations.")
    return lot


def _lock_space(tenant_id: int, lot_id: int, space_id: int) -> Space:
    space = db.session.execute(
        select(Space)
        .where(
            Space.id == space_id,
            Space.lot_id == lot_id,
            Space.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if space is None:
        raise BookingNotFound("Parking space not found.")
    if space.status == "blocked" or not space.default_available:
        raise BookingConflict("The parking space is not available for reservations.")
    return space


def _has_booking_conflict(
    tenant_id: int,
    space_id: int,
    start: datetime,
    end: datetime,
    *,
    exclude_hold_id: int | None = None,
) -> bool:
    booking = db.session.execute(
        select(Booking.id).where(
            Booking.tenant_id == tenant_id,
            Booking.space_id == space_id,
            Booking.status.in_(("reserved", "checked_in")),
            Booking.pickup_at < end,
            Booking.return_at > start,
        ).limit(1)
    ).first()
    if booking is not None:
        return True

    now = datetime.now(timezone.utc)
    hold_statement = select(Hold.id).where(
            Hold.tenant_id == tenant_id,
            Hold.space_id == space_id,
            Hold.status == "active",
            Hold.expires_at > now,
            Hold.pickup_at < end,
            Hold.return_at > start,
        )
    if exclude_hold_id is not None:
        hold_statement = hold_statement.where(Hold.id != exclude_hold_id)
    hold = db.session.execute(hold_statement.limit(1)).first()
    return hold is not None


def _commit_or_rollback():
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def create_hold(
    *,
    tenant_id: int,
    lot_id: int,
    space_id: int,
    pickup_at: datetime,
    return_at: datetime,
    customer_email: str,
) -> Hold:
    """Reserve a space and interval for the requested ten-minute checkout window."""
    tenant_id = _tenant_id(tenant_id)
    start, end = _interval(pickup_at, return_at)
    email = _email(customer_email)
    try:
        _load_lot(tenant_id, lot_id)
        _lock_space(tenant_id, lot_id, space_id)
        if _has_booking_conflict(tenant_id, space_id, start, end):
            raise BookingConflict("The parking space is already reserved for that interval.")

        hold = Hold(
            tenant_id=tenant_id,
            lot_id=lot_id,
            space_id=space_id,
            customer_email=email,
            pickup_at=start,
            return_at=end,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            status="active",
        )
        db.session.add(hold)
        _commit_or_rollback()
        notify_lot_availability(tenant_id, lot_id)
        return hold
    except Exception:
        db.session.rollback()
        raise


def release_hold(*, tenant_id: int, hold_id: int) -> Hold:
    tenant_id = _tenant_id(tenant_id)
    try:
        hold = db.session.execute(
            select(Hold).where(Hold.id == hold_id, Hold.tenant_id == tenant_id).with_for_update()
        ).scalar_one_or_none()
        if hold is None:
            raise BookingNotFound("Reservation hold not found.")
        if hold.status == "active":
            expires_at = hold.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            hold.status = "released" if expires_at > datetime.now(timezone.utc) else "expired"
            _commit_or_rollback()
            notify_lot_availability(tenant_id, hold.lot_id)
        elif hold.status == "converted":
            raise InvalidBookingTransition("A converted hold cannot be released.")
        return hold
    except Exception:
        db.session.rollback()
        raise


def create_booking_from_hold(
    *,
    tenant_id: int,
    hold_id: int,
    first_name: str,
    email: str,
    phone: str,
    total_amount: Decimal | int | float | str,
    currency: str,
    last_name: str = "",
    vehicle_plate: str = "",
    vehicle_make: str = "",
    vehicle_model: str = "",
    company: str = "",
    billing_email: str = "",
    locale: str = "en",
    notes: str = "",
    discount_code: str | None = None,
    discount_base_amount: Decimal | int | float | str | None = None,
    redeem_discount_now: bool = True,
) -> Booking:
    """Convert a live hold into a booking; total_amount must come from trusted server pricing."""
    tenant_id = _tenant_id(tenant_id)
    from .billing import assert_can_book

    assert_can_book(tenant_id)  # trial over / plan quota reached
    customer_email = _email(email)
    amount = _amount(total_amount)
    first_name = (first_name or "").strip()
    phone = (phone or "").strip()
    if not first_name or len(first_name) > 80:
        raise BookingValidationError("A customer first name of at most 80 characters is required.")
    if not phone or len(phone) > 40:
        raise BookingValidationError("A customer phone number of at most 40 characters is required.")
    billing_email = _email(billing_email) if billing_email else ""
    if not isinstance(locale, str) or len(locale) > 10:
        raise BookingValidationError("A valid customer language is required.")
    if (
        len(last_name or "") > 80
        or len(company or "") > 120
        or len(notes or "") > 10000
    ):
        raise BookingValidationError("Customer or booking text exceeds the allowed length.")
    normalized_currency = (currency or "").strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isascii() or not normalized_currency.isalpha():
        raise BookingValidationError("A three-letter currency code is required.")
    normalized_discount_code = (discount_code or "").strip() or None
    if normalized_discount_code is not None and len(normalized_discount_code) > 40:
        raise BookingValidationError("The discount code exceeds 40 characters.")

    try:
        hold = db.session.execute(
            select(Hold).where(Hold.id == hold_id, Hold.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if hold is None:
            raise BookingNotFound("Reservation hold not found.")

        _load_lot(tenant_id, hold.lot_id)
        _lock_space(tenant_id, hold.lot_id, hold.space_id)
        hold = db.session.execute(
            select(Hold)
            .where(Hold.id == hold_id, Hold.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if hold is None:
            raise BookingNotFound("Reservation hold not found.")
        if hold.status != "active":
            raise InvalidBookingTransition("Only an active hold can be converted.")
        expires_at = hold.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            hold.status = "expired"
            _commit_or_rollback()
            raise BookingConflict("The reservation hold has expired.")
        if customer_email != hold.customer_email.casefold():
            raise BookingValidationError("The booking email must match the hold email.")

        pickup_at = _stored_utc(hold.pickup_at)
        return_at = _stored_utc(hold.return_at)
        if pickup_at <= datetime.now(timezone.utc):
            raise BookingValidationError("Pick-up must be in the future.")
        if pickup_at >= return_at:
            raise BookingValidationError("The hold has an invalid date interval.")
        if _has_booking_conflict(
            tenant_id,
            hold.space_id,
            pickup_at,
            return_at,
            exclude_hold_id=hold.id,
        ):
            raise BookingConflict("The parking space is already booked for that interval.")

        if normalized_discount_code is not None:
            if discount_base_amount is None:
                raise BookingValidationError("A server-calculated pre-discount total is required.")
            try:
                if redeem_discount_now:
                    redeem_discount(
                        tenant_id=tenant_id,
                        code=normalized_discount_code,
                        subtotal=money(discount_base_amount),
                        expected_total=amount,
                    )
                else:
                    quote = quote_discount(
                        tenant_id=tenant_id,
                        code=normalized_discount_code,
                        subtotal=money(discount_base_amount),
                        lock=True,
                    )
                    if quote.total != amount:
                        raise DiscountUnavailable("The discount changed; request a fresh quote.")
            except DiscountUnavailable as exc:
                raise BookingValidationError(str(exc)) from exc

        customer = Customer(
            tenant_id=tenant_id,
            first_name=first_name,
            last_name=(last_name or "").strip(),
            email=customer_email,
            phone=phone,
            company=(company or "").strip(),
            billing_email=billing_email,
            locale=locale,
        )
        db.session.add(customer)
        db.session.flush()

        vehicle = None
        plate = (vehicle_plate or "").strip()
        if plate:
            if len(plate) > 30 or len(vehicle_make or "") > 60 or len(vehicle_model or "") > 60:
                raise BookingValidationError("Vehicle details exceed the allowed length.")
            vehicle = Vehicle(
                tenant_id=tenant_id,
                customer_id=customer.id,
                plate=plate,
                make=(vehicle_make or "").strip(),
                model=(vehicle_model or "").strip(),
            )
            db.session.add(vehicle)
            db.session.flush()

        booking = Booking(
            tenant_id=tenant_id,
            lot_id=hold.lot_id,
            space_id=hold.space_id,
            customer_id=customer.id,
            vehicle_id=vehicle.id if vehicle else None,
            pickup_at=pickup_at,
            return_at=return_at,
            subtotal_amount=money(discount_base_amount) if discount_base_amount is not None else amount,
            total_amount=amount,
            currency=normalized_currency,
            discount_code=normalized_discount_code,
            notes=(notes or "").strip(),
            status="reserved",
            payment_status="pending",
        )
        booking.generate_qr()
        db.session.add(booking)
        hold.status = "converted"
        db.session.flush()
        enqueue_webhook_event(
            tenant_id=tenant_id,
            event_type="booking.created",
            data={
                "booking_id": booking.id,
                "booking_ref": booking.booking_ref,
                "status": booking.status,
            },
            commit=False,
        )
        _commit_or_rollback()
        notify_lot_availability(tenant_id, hold.lot_id)
        from .alerts import record_alert

        record_alert(tenant_id, "booking_created", booking.id)  # notification centre + sound for the team
        return booking
    except Exception:
        db.session.rollback()
        raise


def _transition_booking(
    *,
    tenant_id: int,
    booking_id: int,
    expected_status: str,
    target_status: str,
) -> Booking:
    tenant_id = _tenant_id(tenant_id)
    try:
        booking = db.session.execute(
            select(Booking)
            .where(Booking.id == booking_id, Booking.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if booking is None:
            raise BookingNotFound("Booking not found.")
        if booking.status == target_status:
            return booking
        if booking.status != expected_status:
            raise InvalidBookingTransition(
                f"Cannot change booking from {booking.status} to {target_status}."
            )
        required_phase = {
            "checked_in": "arrival",
            "checked_out": "departure",
        }.get(target_status)
        if required_phase and not has_completed_inspection(
            tenant_id=tenant_id,
            booking_id=booking.id,
            phase=required_phase,
        ):
            raise BookingConflict(
                f"A completed {required_phase} inspection is required before {target_status.replace('_', ' ')}."
            )
        booking.status = target_status
        event_type = {
            "cancelled": "booking.cancelled",
            "checked_in": "booking.checked_in",
            "checked_out": "booking.checked_out",
        }[target_status]
        enqueue_webhook_event(
            tenant_id=tenant_id,
            event_type=event_type,
            data={
                "booking_id": booking.id,
                "booking_ref": booking.booking_ref,
                "status": booking.status,
            },
            commit=False,
        )
        _commit_or_rollback()
        notify_lot_availability(tenant_id, booking.lot_id)
        _notify_customer(tenant_id, booking.id, target_status)
        return booking
    except Exception:
        db.session.rollback()
        raise


def _notify_customer(tenant_id: int, booking_id: int, event: str) -> None:
    """Email / SMS / WhatsApp the customer about a committed change. Never raises (see booking_events)."""
    from .alerts import EVENT_TO_KIND, record_alert
    from .booking_events import notify_booking_event

    if event in EVENT_TO_KIND:
        record_alert(tenant_id, EVENT_TO_KIND[event], booking_id)
    notify_booking_event(booking_id, tenant_id, event)


def cancel_booking(*, tenant_id: int, booking_id: int) -> Booking:
    return _transition_booking(
        tenant_id=tenant_id,
        booking_id=booking_id,
        expected_status="reserved",
        target_status="cancelled",
    )


def check_in_booking(*, tenant_id: int, booking_id: int) -> Booking:
    return _transition_booking(
        tenant_id=tenant_id,
        booking_id=booking_id,
        expected_status="reserved",
        target_status="checked_in",
    )


def check_out_booking(*, tenant_id: int, booking_id: int) -> Booking:
    return _transition_booking(
        tenant_id=tenant_id,
        booking_id=booking_id,
        expected_status="checked_in",
        target_status="checked_out",
    )


def create_payment_record(
    *,
    tenant_id: int,
    booking_id: int,
    provider: str,
    amount: Decimal | int | float | str,
    currency: str,
    provider_ref: str = "",
) -> Payment:
    """Record payment state only; this function does not collect money or contact a provider."""
    tenant_id = _tenant_id(tenant_id)
    payment_amount = _amount(amount)
    normalized_provider = (provider or "").strip().lower()
    if normalized_provider not in {"stripe", "manual", "paypal", "apple_pay"}:
        raise BookingValidationError("An explicitly selected supported payment provider is required.")
    normalized_currency = (currency or "").strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isascii() or not normalized_currency.isalpha():
        raise BookingValidationError("A three-letter currency code is required.")
    if len(provider_ref or "") > 120:
        raise BookingValidationError("The provider reference exceeds 120 characters.")

    try:
        booking = db.session.execute(
            select(Booking)
            .where(Booking.id == booking_id, Booking.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if booking is None:
            raise BookingNotFound("Booking not found.")
        if normalized_currency != booking.currency:
            raise BookingValidationError("Payment currency must match the booking currency.")
        payment = Payment(
            tenant_id=tenant_id,
            booking_id=booking.id,
            provider=normalized_provider,
            provider_ref=(provider_ref or "").strip(),
            amount=payment_amount,
            currency=normalized_currency,
            status="pending",
        )
        db.session.add(payment)
        _commit_or_rollback()
        return payment
    except Exception:
        db.session.rollback()
        raise


def _sync_booking_payment_status(booking: Booking) -> None:
    payments = db.session.execute(
        select(Payment).where(
            Payment.tenant_id == booking.tenant_id,
            Payment.booking_id == booking.id,
        )
    ).scalars().all()
    paid_total = sum((payment.amount for payment in payments if payment.status == "paid"), Decimal("0.00"))
    if booking.total_amount > 0 and paid_total >= booking.total_amount:
        booking.payment_status = "paid"
    elif paid_total > 0:
        booking.payment_status = "partial"
    elif any(payment.status == "pending" for payment in payments):
        booking.payment_status = "pending"
    elif any(payment.status == "failed" for payment in payments):
        booking.payment_status = "failed"
    elif payments and all(payment.status == "refunded" for payment in payments):
        booking.payment_status = "refunded"
    else:
        booking.payment_status = "pending"


def update_payment_status(*, tenant_id: int, payment_id: int, status: str) -> Payment:
    """Apply an internal payment-state update after a trusted provider/manual confirmation."""
    tenant_id = _tenant_id(tenant_id)
    target_status = (status or "").strip().lower()
    if target_status not in {"pending", "paid", "failed", "refunded"}:
        raise BookingValidationError("Unsupported payment status.")

    try:
        payment = db.session.execute(
            select(Payment)
            .where(Payment.id == payment_id, Payment.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if payment is None:
            raise BookingNotFound("Payment record not found.")
        booking = db.session.execute(
            select(Booking)
            .where(Booking.id == payment.booking_id, Booking.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if booking is None:
            raise BookingNotFound("Booking not found.")

        allowed = {
            "pending": {"pending", "paid", "failed"},
            "paid": {"paid", "refunded"},
            "failed": {"failed"},
            "refunded": {"refunded"},
        }
        if target_status not in allowed[payment.status]:
            raise InvalidBookingTransition(
                f"Cannot change payment from {payment.status} to {target_status}."
            )
        previous_status = payment.status
        previous_booking_status = booking.payment_status
        notify_event = None
        payment.status = target_status
        if target_status == "paid" and previous_status != "paid":
            payment.paid_at = datetime.now(timezone.utc)
        elif target_status != "paid":
            payment.paid_at = None
        _sync_booking_payment_status(booking)
        if previous_booking_status != "paid" and booking.payment_status == "paid":
            enqueue_webhook_event(
                tenant_id=tenant_id,
                event_type="booking.paid",
                data={
                    "booking_id": booking.id,
                    "booking_ref": booking.booking_ref,
                    "payment_status": booking.payment_status,
                },
                commit=False,
            )
        if previous_booking_status != "paid" and booking.payment_status == "paid" and booking.status != "cancelled":
            notify_event = "paid"
        elif target_status == "refunded" and previous_booking_status != "refunded" and booking.payment_status == "refunded":
            notify_event = "refunded"  # also for cancelled bookings: the credit note follows the refund
        booking_id_for_notice = booking.id
        _commit_or_rollback()
        if notify_event:
            _notify_customer(tenant_id, booking_id_for_notice, notify_event)
        return payment
    except Exception:
        db.session.rollback()
        raise


def reschedule_booking(*, tenant_id: int, booking_id: int, space_id: int, pickup_at: datetime, return_at: datetime) -> Booking:
    """Move an active booking to other dates and/or another space; never creates an overlap."""
    tenant_id = _tenant_id(tenant_id)
    start, end = _utc(pickup_at, "pickup_at"), _utc(return_at, "return_at")
    if start >= end:
        raise BookingValidationError("Return must be later than pick-up.")
    try:
        booking = db.session.execute(
            select(Booking).where(Booking.id == booking_id, Booking.tenant_id == tenant_id).with_for_update()
        ).scalar_one_or_none()
        if booking is None:
            raise BookingNotFound("Booking not found.")
        if booking.status not in ("reserved", "checked_in"):
            raise InvalidBookingTransition("Only active bookings can be changed.")
        _lock_space(tenant_id, booking.lot_id, space_id)
        clash = db.session.execute(
            select(Booking.id).where(
                Booking.tenant_id == tenant_id, Booking.space_id == space_id, Booking.id != booking.id,
                Booking.status.in_(("reserved", "checked_in")), Booking.pickup_at < end, Booking.return_at > start,
            ).limit(1)
        ).first()
        hold = db.session.execute(
            select(Hold.id).where(
                Hold.tenant_id == tenant_id, Hold.space_id == space_id, Hold.status == "active",
                Hold.expires_at > datetime.now(timezone.utc), Hold.pickup_at < end, Hold.return_at > start,
            ).limit(1)
        ).first()
        if clash is not None or hold is not None:
            raise BookingConflict("That space is already booked or held for these dates.")
        booking.space_id, booking.pickup_at, booking.return_at = space_id, start, end
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    notify_lot_availability(tenant_id, booking.lot_id)
    return booking
