"""Phase 9: trial, plan limits and Stripe Billing state handling."""
import time
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Role, Subscription, Tenant, User, utcnow
from app.services import billing
from app.services.bookings import BookingValidationError
from tests.booking_helpers import make_booking, seed_tenant
from tests.conftest import login


def _unix(delta: timedelta) -> int:
    return int(time.time() + delta.total_seconds())


def _stripe_sub(tenant_id, **over):
    obj = {"id": "sub_1", "customer": "cus_1", "status": "active", "cancel_at_period_end": False,
           "current_period_end": _unix(timedelta(days=30)), "metadata": {"tenant_id": str(tenant_id), "plan_key": "pro"},
           "items": {"data": [{"price": {"id": "price_pro", "unit_amount": 4900, "currency": "eur"}}]}}
    obj.update(over)
    return {"type": "customer.subscription.updated", "data": {"object": obj}}


def test_exempt_tenant_is_never_limited(app):
    tenant = seed_tenant()
    state = billing.billing_state(tenant)
    assert state.exempt and not state.blocked and state.status == "exempt"
    billing.assert_can_book(tenant.id)
    assert billing.space_limit_message(tenant.id, 10_000) is None
    assert billing.api_blocked_reason(tenant.id) is None


def test_new_tenant_gets_a_14_day_trial(app, other_tenant):
    tenant = db.session.get(Tenant, other_tenant[0].id)
    state = billing.billing_state(tenant)
    assert state.status == "trialing" and not state.blocked and 13 <= state.trial_days_left <= 14
    assert billing.get_subscription(tenant).plan_key == "starter"


def test_finished_trial_blocks_new_bookings_spaces_and_api(app, other_tenant):
    tenant = db.session.get(Tenant, other_tenant[0].id)
    tenant.created_at = utcnow() - timedelta(days=20)
    db.session.commit()
    assert billing.billing_state(tenant).blocked
    with pytest.raises(billing.PlanLimitError):
        billing.assert_can_book(tenant.id)
    assert billing.space_limit_message(tenant.id, 1)
    assert billing.api_blocked_reason(tenant.id)
    assert issubclass(billing.PlanLimitError, BookingValidationError)


def test_monthly_booking_quota_is_enforced(app):
    app.config["PLAN_LIMITS_JSON"] = '{"starter": {"bookings": 1, "spaces": 0, "api_requests": 0}}'
    tenant = seed_tenant()
    tenant.billing_exempt = False
    db.session.commit()
    make_booking(tenant, space_index=0)
    with pytest.raises(billing.PlanLimitError):
        make_booking(tenant, space_index=1, email="two@example.com")


def test_space_quota_message(app):
    app.config["PLAN_LIMITS_JSON"] = '{"starter": {"spaces": 50, "bookings": 0, "api_requests": 0}}'
    tenant = seed_tenant()
    tenant.billing_exempt = False
    db.session.commit()
    used = billing.usage(tenant.id)["spaces"]
    assert billing.space_limit_message(tenant.id, 50 - used) is None
    assert "up to 50 spaces" in billing.space_limit_message(tenant.id, 50 - used + 1)


def test_stripe_events_drive_the_subscription_state(app, other_tenant):
    app.config["STRIPE_PRICE_PRO"] = "price_pro"
    tenant = db.session.get(Tenant, other_tenant[0].id)
    assert billing.process_billing_event(_stripe_sub(tenant.id)) == "updated"
    sub = db.session.execute(db.select(Subscription).where(Subscription.tenant_id == tenant.id)).scalar_one()
    assert (sub.plan_key, sub.status, sub.amount_cents, sub.currency) == ("pro", "active", 4900, "EUR")
    assert not billing.billing_state(tenant).blocked

    billing.process_billing_event({"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    state = billing.billing_state(tenant)
    assert state.status == "past_due" and not state.blocked and state.reason  # grace while Stripe retries

    billing.process_billing_event({"type": "invoice.paid", "data": {"object": {"subscription": "sub_1", "customer": "cus_1"}}})
    assert billing.billing_state(tenant).status == "active"

    ended = _stripe_sub(tenant.id, current_period_end=_unix(-timedelta(days=1)))
    ended["type"] = "customer.subscription.deleted"
    billing.process_billing_event(ended)
    state = billing.billing_state(tenant)
    assert state.status == "cancelled" and state.blocked


def test_cancelled_subscription_keeps_access_until_the_period_ends(app, other_tenant):
    tenant = db.session.get(Tenant, other_tenant[0].id)
    event = _stripe_sub(tenant.id, status="active", cancel_at_period_end=True)
    billing.process_billing_event(event)
    state = billing.billing_state(tenant)
    assert state.cancel_at_period_end and not state.blocked
    event["data"]["object"]["status"] = "canceled"
    billing.process_billing_event(event)
    assert not billing.billing_state(tenant).blocked  # paid period has not ended yet


def test_unknown_stripe_subscription_is_ignored(app):
    assert billing.process_billing_event({"type": "customer.subscription.updated", "data": {"object": {"id": "sub_x", "customer": "cus_x", "metadata": {}}}}) == "ignored"
    assert billing.process_billing_event({"type": "charge.succeeded", "data": {"object": {}}}) == "ignored"


def test_billing_webhook_rejects_unsigned_requests(client, app):
    assert client.post("/webhooks/stripe-billing", data=b"{}", headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    app.config["STRIPE_BILLING_WEBHOOK_SECRET"] = "whsec_test"
    assert client.post("/webhooks/stripe-billing", data=b"{}", headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400


def test_owner_sees_billing_page_and_cannot_buy_unconfigured_plans(client, app):
    login(client)
    page = client.get("/admin/billing")
    assert page.status_code == 200 and b"No subscription needed" in page.data
    assert client.get("/admin/platform/subscriptions").status_code == 403


def test_trial_tenant_billing_page_and_checkout_guard(client, app):
    tenant = seed_tenant()
    tenant.billing_exempt = False
    db.session.commit()
    login(client)
    page = client.get("/admin/billing")
    assert page.status_code == 200 and b"Free trial" in page.data
    flash = client.post("/admin/billing/subscribe/pro", follow_redirects=True)
    assert b"not available" in flash.data or b"Stripe is not configured" in flash.data


def test_platform_owner_manages_subscriptions(client, app, other_tenant):
    user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
    user.set_password("platform-password")
    db.session.add(user)
    db.session.commit()
    client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})
    page = client.get("/admin/platform/subscriptions")
    assert page.status_code == 200 and b"Other" in page.data
    tid = other_tenant[0].id
    before = billing.get_subscription(db.session.get(Tenant, tid)).trial_ends_at
    assert client.post(f"/admin/platform/subscriptions/{tid}", data={"action": "extend_trial", "days": "10"}).status_code == 302
    db.session.expire_all()
    assert billing.get_subscription(db.session.get(Tenant, tid)).trial_ends_at >= before + timedelta(days=9)
    client.post(f"/admin/platform/subscriptions/{tid}", data={"action": "exempt"})
    db.session.expire_all()
    assert db.session.get(Tenant, tid).billing_exempt is True
    assert client.post(f"/admin/platform/subscriptions/{tid}", data={"action": "nonsense"}).status_code == 400


def test_new_platform_tenant_starts_a_trial(client, app):
    user = User(tenant_id=None, email="platform@test.dev", name="Admin", role=Role.PLATFORM_OWNER)
    user.set_password("platform-password")
    db.session.add(user)
    db.session.commit()
    client.post("/admin/login", data={"email": "platform@test.dev", "password": "platform-password"})
    created = client.post("/admin/platform/tenants/new", data={
        "name": "Neo Parking", "slug": "", "status": "trial", "default_locale": "de", "timezone": "Europe/Berlin", "currency": "EUR",
        "custom_domain": "", "owner_name": "Neo", "owner_email": "neo@example.com", "owner_password": "long-enough-pass"})
    assert created.status_code == 302
    tenant = Tenant.query.filter_by(slug="neo-parking").one()
    assert not tenant.billing_exempt
    assert Subscription.query.filter_by(tenant_id=tenant.id, status="trialing").count() == 1
