"""Platform subscriptions (phase 9): 14-day trial, Starter / Pro / Business plans, quotas and Stripe Billing.

Rules
- Tenants flagged `billing_exempt` (your own parking) are never limited or charged.
- Every other tenant gets a trial that starts when the tenant was created (TRIAL_DAYS, default 14). When the trial ends
  without a paid subscription, or a subscription is cancelled/expired, *new* bookings, new spaces and API calls are refused
  with a clear message. Existing data and the owner's back office stay available so the owner can subscribe.
- A failed payment (`past_due`) keeps working while Stripe retries; the owner sees a warning.
- Limits per plan (spaces, bookings per calendar month, API requests per calendar month; 0 = unlimited) come from
  `PLAN_LIMITS_JSON` or the placeholder defaults below. They are NOT prices: amounts live in Stripe (STRIPE_PRICE_* ids),
  and the platform only copies back what Stripe reports.
- Stripe is the source of truth: subscription state is written from verified webhook events only.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import stripe
from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models import Booking, Space, Subscription, Tenant, UsageLog
from .bookings import BookingValidationError

PLAN_KEYS = ("starter", "pro", "business")
PLAN_LABELS = {"starter": "Starter", "pro": "Pro", "business": "Business"}
PLAN_PRICE_CONFIG = {"starter": "STRIPE_PRICE_STARTER", "pro": "STRIPE_PRICE_PRO", "business": "STRIPE_PRICE_BUSINESS"}
# Placeholders: confirm your own limits before selling plans (override with PLAN_LIMITS_JSON).
DEFAULT_LIMITS = {
    "starter": {"spaces": 25, "bookings": 300, "api_requests": 5000},
    "pro": {"spaces": 100, "bookings": 2000, "api_requests": 50000},
    "business": {"spaces": 0, "bookings": 0, "api_requests": 0},
}
STRIPE_STATUS = {
    "trialing": "trialing", "active": "active", "past_due": "past_due", "unpaid": "past_due", "incomplete": "past_due",
    "canceled": "cancelled", "incomplete_expired": "expired", "paused": "expired",
}


class BillingError(RuntimeError):
    """A billing operation could not be completed (message is safe to show to the owner)."""


class PlanLimitError(BookingValidationError):
    """The tenant's plan does not allow this action (quota reached, trial over or subscription ended)."""


UNAVAILABLE_MESSAGE = "Online booking is currently unavailable for this parking. Please contact the parking operator."


# ---- plans ---------------------------------------------------------------------------------------

def plan_limits(plan_key: str) -> dict:
    limits = dict(DEFAULT_LIMITS.get(plan_key, DEFAULT_LIMITS["starter"]))
    raw = current_app.config.get("PLAN_LIMITS_JSON") or ""
    if raw:
        try:
            override = json.loads(raw).get(plan_key, {})
            limits.update({k: int(v) for k, v in override.items() if k in limits})
        except (ValueError, TypeError, AttributeError):
            current_app.logger.warning("PLAN_LIMITS_JSON is not valid JSON of the form {plan: {spaces, bookings, api_requests}}")
    return limits


def price_id(plan_key: str) -> str:
    return (current_app.config.get(PLAN_PRICE_CONFIG.get(plan_key, ""), "") or "").strip()


def plan_for_price(price: str) -> str | None:
    for key in PLAN_KEYS:
        if price and price == price_id(key):
            return key
    return None


def billing_configured() -> bool:
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


# ---- state ---------------------------------------------------------------------------------------

def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_subscription(tenant: Tenant) -> Subscription | None:
    """The tenant's subscription row; a trial row is created on first use (anchored at the tenant's creation date)."""
    if tenant.billing_exempt:
        return None
    sub = db.session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id)).scalar_one_or_none()
    if sub is None:
        started = _aware(tenant.created_at) or _now()
        sub = Subscription(tenant_id=tenant.id, plan_key="starter", status="trialing",
                           trial_ends_at=started + timedelta(days=current_app.config.get("TRIAL_DAYS", 14)))
        db.session.add(sub)
        db.session.flush()
    return sub


@dataclass
class BillingState:
    exempt: bool
    plan_key: str
    status: str                 # exempt | trialing | active | past_due | cancelled | expired
    limits: dict
    blocked: bool
    reason: str
    trial_days_left: int | None
    period_end: datetime | None
    cancel_at_period_end: bool


def billing_state(tenant: Tenant) -> BillingState:
    if tenant.billing_exempt:
        return BillingState(True, "business", "exempt", {"spaces": 0, "bookings": 0, "api_requests": 0}, False, "", None, None, False)
    sub = get_subscription(tenant)
    now = _now()
    status, blocked, reason, days_left = sub.status, False, "", None
    period_end = _aware(sub.current_period_end)
    if status == "trialing":
        trial_end = _aware(sub.trial_ends_at) or now
        if sub.stripe_subscription_id == "" and trial_end <= now:
            status, blocked, reason = "expired", True, "The free trial has ended. Choose a plan to keep taking bookings."
        else:
            days_left = max(0, (trial_end - now).days + (1 if (trial_end - now).seconds else 0))
    elif status in ("cancelled", "expired"):
        if not (period_end and period_end > now):
            blocked, reason = True, "The subscription has ended. Choose a plan to keep taking bookings."
    elif status == "past_due":
        reason = "The last payment failed. Update your payment method; Stripe will retry automatically."
    return BillingState(False, sub.plan_key, status, plan_limits(sub.plan_key), blocked, reason, days_left, period_end,
                        bool(sub.cancel_at_period_end))


def month_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage(tenant_id: int) -> dict:
    since = month_start()
    return {
        "spaces": db.session.execute(select(func.count(Space.id)).where(Space.tenant_id == tenant_id)).scalar_one(),
        "bookings": db.session.execute(select(func.count(Booking.id)).where(Booking.tenant_id == tenant_id, Booking.created_at >= since)).scalar_one(),
        "api_requests": db.session.execute(select(func.count(UsageLog.id)).where(UsageLog.tenant_id == tenant_id, UsageLog.created_at >= since)).scalar_one(),
    }


# ---- enforcement ---------------------------------------------------------------------------------

def assert_can_book(tenant_id: int) -> None:
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None or tenant.billing_exempt:
        return
    state = billing_state(tenant)
    if state.blocked:
        raise PlanLimitError(UNAVAILABLE_MESSAGE)
    limit = state.limits.get("bookings", 0)
    if limit and usage(tenant_id)["bookings"] >= limit:
        raise PlanLimitError(UNAVAILABLE_MESSAGE)


def space_limit_message(tenant_id: int, adding: int) -> str | None:
    """Why `adding` more spaces are not allowed, or None."""
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None or tenant.billing_exempt or adding <= 0:
        return None
    state = billing_state(tenant)
    if state.blocked:
        return state.reason
    limit = state.limits.get("spaces", 0)
    if limit and usage(tenant_id)["spaces"] + adding > limit:
        return f"Your {PLAN_LABELS.get(state.plan_key, state.plan_key)} plan includes up to {limit} spaces. Upgrade the plan to add more."
    return None


def assert_can_add_spaces(tenant_id: int, adding: int) -> None:
    message = space_limit_message(tenant_id, adding)
    if message:
        raise PlanLimitError(message)


def api_blocked_reason(tenant_id: int) -> str | None:
    """Reason the API must refuse this tenant's request, or None."""
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None or tenant.billing_exempt:
        return None
    state = billing_state(tenant)
    if state.blocked:
        return state.reason
    limit = state.limits.get("api_requests", 0)
    if limit and usage(tenant_id)["api_requests"] >= limit:
        return "The monthly API request limit of the plan has been reached."
    return None


def start_trial(tenant: Tenant) -> Subscription | None:
    """Explicitly start the trial for a newly created tenant (also created lazily by get_subscription)."""
    return get_subscription(tenant)


# ---- Stripe Billing ------------------------------------------------------------------------------

def _key() -> str:
    key = current_app.config.get("STRIPE_SECRET_KEY")
    if not key:
        raise BillingError("Stripe is not configured (STRIPE_SECRET_KEY).")
    return key


def _ensure_customer(tenant: Tenant, sub: Subscription, email: str) -> str:
    if sub.stripe_customer_id:
        return sub.stripe_customer_id
    try:
        customer = stripe.Customer.create(api_key=_key(), name=tenant.name, email=email, metadata={"tenant_id": str(tenant.id)},
                                          idempotency_key=f"parkpilot-customer-{tenant.id}")
    except stripe.StripeError as exc:
        current_app.logger.exception("Stripe customer creation failed for tenant %s", tenant.id)
        raise BillingError("Stripe could not create the billing account. Please try again.") from exc
    sub.stripe_customer_id = customer.id
    db.session.commit()
    return customer.id


def create_subscription_checkout(tenant: Tenant, plan_key: str, *, email: str, success_url: str, cancel_url: str) -> str:
    """Stripe Checkout for a plan; the remaining free trial days carry over so nobody loses trial time by subscribing early."""
    if plan_key not in PLAN_KEYS or not price_id(plan_key):
        raise BillingError("This plan is not available for purchase yet.")
    sub = get_subscription(tenant)
    if sub is None:
        raise BillingError("This account does not need a subscription.")
    if sub.status in ("active", "past_due") and sub.stripe_subscription_id:
        raise BillingError("You already have a subscription. Use 'Manage billing' to change or cancel it.")
    customer = _ensure_customer(tenant, sub, email)
    data = {"metadata": {"tenant_id": str(tenant.id), "plan_key": plan_key}}
    trial_end = _aware(sub.trial_ends_at)
    if sub.status == "trialing" and trial_end and trial_end - _now() >= timedelta(hours=48):  # Stripe needs at least 48h
        data["trial_end"] = int(trial_end.timestamp())
    try:
        session = stripe.checkout.Session.create(
            api_key=_key(), mode="subscription", customer=customer, client_reference_id=str(tenant.id),
            line_items=[{"price": price_id(plan_key), "quantity": 1}], subscription_data=data,
            metadata={"tenant_id": str(tenant.id), "plan_key": plan_key}, allow_promotion_codes=True,
            success_url=success_url, cancel_url=cancel_url,
        )
    except stripe.StripeError as exc:
        current_app.logger.exception("Stripe subscription checkout failed for tenant %s", tenant.id)
        raise BillingError("Stripe Checkout could not be started. Please try again.") from exc
    if not session.url:
        raise BillingError("Stripe did not return a checkout address.")
    return session.url


def create_portal_session(tenant: Tenant, *, return_url: str) -> str:
    """Stripe's hosted page to change plan, update the card, see invoices or cancel."""
    sub = get_subscription(tenant)
    if sub is None or not sub.stripe_customer_id:
        raise BillingError("There is no billing account yet. Choose a plan first.")
    try:
        session = stripe.billing_portal.Session.create(api_key=_key(), customer=sub.stripe_customer_id, return_url=return_url)
    except stripe.StripeError as exc:
        current_app.logger.exception("Stripe billing portal failed for tenant %s", tenant.id)
        raise BillingError("The billing portal could not be opened. Please try again.") from exc
    return session.url


def construct_billing_event(*, payload: bytes, signature: str):
    secret = current_app.config.get("STRIPE_BILLING_WEBHOOK_SECRET")
    if not secret:
        raise BillingError("The billing webhook secret is not configured (STRIPE_BILLING_WEBHOOK_SECRET).")
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise BillingError("Invalid Stripe webhook signature or payload.") from exc


def _from_unix(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None


def _find_subscription(*, subscription_id: str = "", customer_id: str = "", tenant_id=None) -> Subscription | None:
    # Unscoped on purpose: a webhook is not a tenant request. Matching is by Stripe ids or our own metadata.
    if subscription_id:
        sub = db.session.execute(select(Subscription).where(Subscription.stripe_subscription_id == subscription_id)).scalar_one_or_none()
        if sub:
            return sub
    if customer_id:
        sub = db.session.execute(select(Subscription).where(Subscription.stripe_customer_id == customer_id)).scalar_one_or_none()
        if sub:
            return sub
    try:
        tid = int(tenant_id)
    except (TypeError, ValueError):
        return None
    tenant = db.session.get(Tenant, tid)
    return get_subscription(tenant) if tenant is not None else None


def apply_stripe_subscription(obj: dict) -> Subscription | None:
    """Copy a Stripe subscription object into our row (idempotent: replays and out-of-order events end in the same state)."""
    metadata = obj.get("metadata") or {}
    sub = _find_subscription(subscription_id=obj.get("id", ""), customer_id=obj.get("customer", "") or "", tenant_id=metadata.get("tenant_id"))
    if sub is None:
        current_app.logger.warning("Stripe subscription %s matches no tenant", obj.get("id"))
        return None
    items = ((obj.get("items") or {}).get("data")) or []
    price = (items[0].get("price") or {}) if items else {}
    sub.stripe_subscription_id = obj.get("id", sub.stripe_subscription_id)
    sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
    sub.status = STRIPE_STATUS.get(obj.get("status", ""), sub.status)
    plan = plan_for_price(price.get("id", "")) or metadata.get("plan_key")
    if plan in PLAN_KEYS:
        sub.plan_key = plan
    sub.current_period_end = _from_unix(obj.get("current_period_end") or (items[0].get("current_period_end") if items else None)) or sub.current_period_end
    sub.trial_ends_at = _from_unix(obj.get("trial_end")) or sub.trial_ends_at
    sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    if price.get("unit_amount") is not None:
        sub.amount_cents = int(price["unit_amount"])
    if price.get("currency"):
        sub.currency = price["currency"].upper()[:3]
    if sub.status == "active":
        sub.last_payment_failed_at = None
    db.session.commit()
    return sub


def process_billing_event(event) -> str:
    """Handle one verified Stripe event. Returns what happened (useful in tests and logs)."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        if kind == "customer.subscription.deleted":
            obj = {**obj, "status": "canceled"}
        return "updated" if apply_stripe_subscription(obj) else "ignored"
    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        sub = _find_subscription(subscription_id=obj.get("subscription") or "", customer_id=obj.get("customer") or "",
                                 tenant_id=(obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id"))
        if sub is None:
            return "ignored"
        sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
        sub.stripe_subscription_id = obj.get("subscription") or sub.stripe_subscription_id
        plan = (obj.get("metadata") or {}).get("plan_key")
        if plan in PLAN_KEYS:
            sub.plan_key = plan
        if sub.status in ("trialing", "expired", "cancelled"):
            sub.status = "active" if obj.get("payment_status") == "paid" else sub.status
        db.session.commit()
        return "linked"
    if kind in ("invoice.payment_failed", "invoice.paid", "invoice.payment_succeeded"):
        sub = _find_subscription(subscription_id=obj.get("subscription") or "", customer_id=obj.get("customer") or "")
        if sub is None:
            return "ignored"
        if kind == "invoice.payment_failed":
            sub.status, sub.last_payment_failed_at = "past_due", _now()
        else:
            if sub.status == "past_due":
                sub.status = "active"
            sub.last_payment_failed_at = None
        db.session.commit()
        return "updated"
    return "ignored"
