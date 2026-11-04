"""Owner-side booking operations that combine several services (extras, refunds, local time)."""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..extensions import db
from ..models import BookingExtra, Extra, Payment, money, utcnow
from .bookings import BookingServiceError, cancel_booking
from .stripe_payments import StripeBookingError, expire_checkout_payment, refund_card_payment


def parse_local(value: str, timezone_name: str) -> datetime:
    """A datetime-local form value in the parking's time zone -> aware UTC."""
    try:
        naive = datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise BookingServiceError("Enter a valid date and time.") from exc
    return naive.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


MAX_PASSENGERS = 20


def clean_quantity(value) -> int:
    """Passenger count from a form value: 1..MAX_PASSENGERS, 1 when missing or invalid."""
    try:
        return max(1, min(int(value), MAX_PASSENGERS))
    except (TypeError, ValueError):
        return 1


def person_limit(extra: Extra) -> int:
    """Most persons one booking can add to this extra (the shuttle seats 2 incl. luggage)."""
    return min(extra.max_persons, MAX_PASSENGERS) if extra.per_person and extra.max_persons else MAX_PASSENGERS


def validate_persons(extra: Extra, quantity) -> None:
    """Per-person extras have a seat limit; bundle extras also need a price for persons beyond the included ones."""
    if not extra.per_person:
        return
    persons = clean_quantity(quantity)
    limit, name = person_limit(extra), extra.name
    if persons > limit:
        raise BookingServiceError(f"The {name} can take at most {limit} persons.")
    if extra.included_persons and persons > extra.included_persons and extra.extra_person_price is None:
        raise BookingServiceError(
            f"Only {extra.included_persons} persons are included and no price is set for additional persons."
        )


def extra_cost(extra: Extra, quantity: int = 1) -> Decimal:
    """Price of an extra.

    Bundle (included_persons > 0): `price` covers the included persons, each additional person pays extra_person_price.
    Older per-person rule: price x persons. Everything else: a flat price.
    """
    persons = clean_quantity(quantity) if extra.per_person else 1
    if extra.per_person and extra.included_persons:
        additional = max(0, persons - extra.included_persons)
        return money(Decimal(extra.price) + Decimal(extra.extra_person_price or 0) * additional)
    return money(Decimal(extra.price) * persons)


def add_extra(booking, extra: Extra, details: str = "", quantity: int = 1) -> BookingExtra:
    validate_persons(extra, quantity)
    quantity = clean_quantity(quantity) if extra.per_person else 1
    bundle = bool(extra.per_person and extra.included_persons)
    row = BookingExtra(
        tenant_id=booking.tenant_id, booking_id=booking.id, extra_id=extra.id, kind=extra.kind,
        name=extra.name, unit_price=extra.price, quantity=quantity, details=(details or "").strip()[:255],
        included_persons=extra.included_persons if bundle else 0,
        extra_person_price=(extra.extra_person_price or 0) if bundle else 0,
    )
    db.session.add(row)
    booking.total_amount = Decimal(booking.total_amount) + extra_cost(extra, quantity)
    return row


def remove_extra(booking, row: BookingExtra) -> None:
    booking.total_amount = max(Decimal(0), Decimal(booking.total_amount) - row.total())
    db.session.delete(row)


def extras_total(booking) -> Decimal:
    return sum((e.total() for e in booking.extras if e.status != "cancelled"), Decimal(0))


def mark_extra(row: BookingExtra, *, done: bool, user_id: int | None) -> None:
    row.status = "done" if done else "requested"
    row.done_at = utcnow() if done else None
    row.done_by_id = user_id if done else None


def cancel_with_refund(*, tenant_id: int, booking) -> None:
    """Cancel a booking; refund a paid card payment or expire an unfinished card checkout first."""
    from flask import g

    payments = db.session.execute(select(Payment).where(Payment.booking_id == booking.id)).scalars().all()
    # The customer should hear "cancelled" first and "refunded" (with the credit note) second, so the refund
    # notice is held back while the payment is refunded and sent explicitly after the cancellation.
    previously_suppressed = g.get("suppress_booking_notifications", False)
    g.suppress_booking_notifications = True
    try:
        for payment in payments:
            if payment.provider != "stripe":
                continue
            if payment.status == "paid":
                refund_card_payment(payment)
            elif payment.status == "pending" and payment.provider_ref:
                expire_checkout_payment(payment)
    finally:
        g.suppress_booking_notifications = previously_suppressed
    cancel_booking(tenant_id=tenant_id, booking_id=booking.id)
    if not previously_suppressed:
        db.session.refresh(booking)
        if booking.payment_status == "refunded":
            from .booking_events import notify_booking_event

            notify_booking_event(booking.id, tenant_id, "refunded")


__all__ = ["BookingServiceError", "StripeBookingError", "parse_local", "add_extra", "remove_extra", "extras_total",
           "mark_extra", "cancel_with_refund"]
