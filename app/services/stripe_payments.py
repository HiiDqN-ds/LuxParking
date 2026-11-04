"""Stripe Checkout and webhook processing for customer bookings."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import stripe
from flask import current_app
from sqlalchemy import select

from ..extensions import db
from ..models import Booking, Payment
from .booking_notifications import send_booking_confirmation
from .bookings import (
    BookingServiceError,
    cancel_booking,
    update_payment_status,
)
from .pricing import DiscountUnavailable, redeem_discount


class StripeBookingError(RuntimeError):
    """A Stripe checkout or payment operation failed safely."""


def stripe_booking_payments_enabled() -> bool:
    return bool(
        current_app.config.get("STRIPE_SECRET_KEY")
        and current_app.config.get("STRIPE_WEBHOOK_SECRET")
    )


def create_checkout_session(*, booking: Booking, payment: Payment, success_url: str, cancel_url: str):
    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key or not current_app.config.get("STRIPE_WEBHOOK_SECRET"):
        raise StripeBookingError("Online card payments are unavailable until Stripe keys and webhook signing are configured.")
    amount_cents = int((Decimal(booking.total_amount) * 100).to_integral_exact())
    if amount_cents <= 0:
        raise StripeBookingError("Online card payment requires a positive booking total.")
    # Payment methods: by default Stripe offers the ones switched on in the Stripe Dashboard (cards, SEPA direct debit,
    # PayPal, Klarna, Apple Pay, Google Pay ...), which is what customers in Germany expect. Set
    # STRIPE_PAYMENT_METHOD_TYPES=card (or a comma list) to pin the list. Delayed methods (SEPA) are completed later
    # by checkout.session.async_payment_succeeded / async_payment_failed, which the webhook below handles.
    pinned = [m.strip() for m in (current_app.config.get("STRIPE_PAYMENT_METHOD_TYPES") or "").split(",") if m.strip()]
    method_options = {"payment_method_types": pinned} if pinned else {}
    try:
        session = stripe.checkout.Session.create(
            api_key=secret_key,
            idempotency_key=f"parkpilot-booking-{booking.booking_ref}-{payment.id}",
            mode="payment",
            **method_options,
            line_items=[
                {
                    "price_data": {
                        "currency": booking.currency.lower(),
                        "product_data": {
                            "name": f"Parking at {booking.lot.name}",
                            "description": (
                                f"Space {booking.space.name}; "
                                f"{booking.pickup_at.isoformat()} to {booking.return_at.isoformat()}"
                            ),
                        },
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            customer_email=booking.customer.email,
            client_reference_id=str(booking.id),
            metadata={
                "tenant_id": str(booking.tenant_id),
                "booking_id": str(booking.id),
                "payment_id": str(payment.id),
            },
            success_url=success_url,
            cancel_url=cancel_url,
            expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
        )
    except stripe.StripeError as exc:
        current_app.logger.exception("Stripe Checkout session creation failed for booking %s.", booking.id)
        raise StripeBookingError("Secure card checkout could not be started. Your booking was not confirmed.") from exc
    if not session.id or not session.url:
        raise StripeBookingError("Stripe did not return a usable Checkout session.")
    payment.provider_ref = session.id
    payment.checkout_url = session.url
    db.session.commit()
    return session


def construct_stripe_event(*, payload: bytes, signature: str):
    webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise StripeBookingError("Stripe webhook signing is not configured.")
    try:
        return stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise StripeBookingError("Invalid Stripe webhook signature or payload.") from exc


def expire_checkout_payment(payment: Payment) -> None:
    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key or not payment.provider_ref:
        raise StripeBookingError("The pending card checkout cannot be safely expired.")
    try:
        stripe.checkout.Session.expire(payment.provider_ref, api_key=secret_key)
    except stripe.StripeError as exc:
        current_app.logger.exception("Could not expire Stripe Checkout for payment %s.", payment.id)
        raise StripeBookingError("The pending card checkout could not be cancelled safely.") from exc


def refund_card_payment(payment: Payment) -> str:
    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key or not payment.provider_intent_ref:
        raise StripeBookingError("The card payment has no refundable Stripe payment reference.")
    try:
        refund = stripe.Refund.create(
            api_key=secret_key,
            idempotency_key=f"parkpilot-cancel-{payment.booking_id}-{payment.id}",
            payment_intent=payment.provider_intent_ref,
        )
    except stripe.StripeError as exc:
        current_app.logger.exception("Could not refund Stripe payment %s.", payment.id)
        raise StripeBookingError("The card refund could not be started. The booking was not cancelled.") from exc
    if refund.status not in {"succeeded", "pending", "requires_action"}:
        raise StripeBookingError("Stripe did not accept the requested refund.")
    if refund.status == "succeeded":
        update_payment_status(
            tenant_id=payment.tenant_id,
            payment_id=payment.id,
            status="refunded",
        )
    return refund.status


def process_stripe_event(event) -> None:
    event_type = event.get("type")
    if event_type not in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
        "charge.refunded",
    }:
        return

    if event_type == "charge.refunded":
        payment_intent = event.get("data", {}).get("object", {}).get("payment_intent")
        payment = db.session.execute(
            select(Payment).where(
                Payment.provider == "stripe",
                Payment.provider_intent_ref == str(payment_intent or ""),
            )
        ).scalar_one_or_none()
        if payment is None:
            raise StripeBookingError("No booking payment matches the Stripe refund event.")
        if payment.status == "paid":
            update_payment_status(
                tenant_id=payment.tenant_id,
                payment_id=payment.id,
                status="refunded",
            )
        return

    session = event.get("data", {}).get("object", {})
    metadata = session.get("metadata") or {}
    try:
        payment_id = int(metadata.get("payment_id", ""))
        tenant_id = int(metadata.get("tenant_id", ""))
        booking_id = int(metadata.get("booking_id", ""))
    except (TypeError, ValueError) as exc:
        raise StripeBookingError("Stripe Checkout metadata is missing valid internal identifiers.") from exc

    payment = db.session.execute(
        select(Payment).where(
            Payment.id == payment_id,
            Payment.tenant_id == tenant_id,
            Payment.booking_id == booking_id,
            Payment.provider == "stripe",
            Payment.provider_ref == session.get("id"),
        )
    ).scalar_one_or_none()
    if payment is None:
        raise StripeBookingError("No matching booking payment exists for this Stripe Checkout session.")
    expected_amount = int((Decimal(payment.amount) * 100).to_integral_exact())
    if (
        session.get("currency") != payment.currency.lower()
        or session.get("amount_total") != expected_amount
    ):
        raise StripeBookingError("Stripe Checkout amount or currency does not match the booking.")

    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if session.get("payment_status") != "paid":
            return
        if payment.status == "paid":
            booking = db.session.execute(
                select(Booking).where(
                    Booking.id == booking_id,
                    Booking.tenant_id == tenant_id,
                )
            ).scalar_one()
            if booking.confirmation_sent_at is None:
                send_booking_confirmation(booking.id, tenant_id)
            return
        if payment.status != "pending":
            raise StripeBookingError("The booking payment is no longer pending.")
        booking = db.session.execute(
            select(Booking).where(
                Booking.id == booking_id,
                Booking.tenant_id == tenant_id,
            ).with_for_update()
        ).scalar_one()
        if booking.discount_code:
            try:
                redeem_discount(
                    tenant_id=tenant_id,
                    code=booking.discount_code,
                    subtotal=booking.subtotal_amount,
                    expected_total=booking.total_amount,
                )
            except DiscountUnavailable as exc:
                raise StripeBookingError(
                    "The Stripe payment succeeded but the discount could not be redeemed; manual review is required."
                ) from exc
        payment.provider_intent_ref = str(session.get("payment_intent") or "")
        try:
            update_payment_status(
                tenant_id=tenant_id,
                payment_id=payment.id,
                status="paid",
            )
        except BookingServiceError as exc:
            raise StripeBookingError("The confirmed Stripe payment could not be applied to the booking.") from exc
        booking = db.session.execute(
            select(Booking).where(
                Booking.id == booking_id,
                Booking.tenant_id == tenant_id,
            )
        ).scalar_one()
        send_booking_confirmation(booking.id, tenant_id)
        return

    if payment.status == "paid":
        return
    booking = db.session.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if booking is None:
        raise StripeBookingError("The booking for this Checkout session no longer exists.")
    if payment.status == "pending":
        try:
            update_payment_status(
                tenant_id=tenant_id,
                payment_id=payment.id,
                status="failed",
            )
        except BookingServiceError as exc:
            raise StripeBookingError("The expired Checkout payment could not be updated.") from exc
    if booking.status == "reserved":
        try:
            cancel_booking(tenant_id=tenant_id, booking_id=booking.id)
        except BookingServiceError as exc:
            raise StripeBookingError("The unpaid booking could not be released after Checkout expired.") from exc
