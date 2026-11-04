"""Checkout offers the payment methods German customers expect (SEPA, PayPal, Klarna, Apple Pay ...)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import stripe_payments


def _booking():
    start = datetime(2026, 12, 20, 8, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=7, tenant_id=1, booking_ref="PP-TEST", total_amount="79.20", currency="EUR", pickup_at=start, return_at=start + timedelta(days=7),
        lot=SimpleNamespace(name="Luxury Parking"), space=SimpleNamespace(name="A1"), customer=SimpleNamespace(email="a@example.de"))


@pytest.fixture()
def stripe_calls(app, monkeypatch):
    app.config.update(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    calls = []
    monkeypatch.setattr(stripe_payments.stripe.checkout.Session, "create", lambda **kw: calls.append(kw) or SimpleNamespace(id="cs_1", url="https://checkout.example/cs_1"))
    return calls


def _create():
    return stripe_payments.create_checkout_session(booking=_booking(), payment=SimpleNamespace(id=3), success_url="https://x/ok", cancel_url="https://x/no")


def test_by_default_stripe_offers_every_method_enabled_in_the_dashboard(app, stripe_calls):
    app.config["STRIPE_PAYMENT_METHOD_TYPES"] = ""
    _create()
    assert "payment_method_types" not in stripe_calls[0]  # SEPA, PayPal, Klarna, wallets ... as configured in Stripe
    assert stripe_calls[0]["line_items"][0]["price_data"]["currency"] == "eur" and stripe_calls[0]["line_items"][0]["price_data"]["unit_amount"] == 7920


def test_the_list_can_be_pinned(app, stripe_calls):
    app.config["STRIPE_PAYMENT_METHOD_TYPES"] = "card, sepa_debit ,paypal"
    _create()
    assert stripe_calls[0]["payment_method_types"] == ["card", "sepa_debit", "paypal"]
