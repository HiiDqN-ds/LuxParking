from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.extensions import db
from app.models import Lot, PriceRule, Space, Tenant
from app.services.availability import quote_space
from app.tenancy import use_tenant


def test_global_eur_tariff_uses_flat_short_stay_and_started_week_rates(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        space = Space.query.filter_by(lot_id=lot.id, tenant_id=tenant.id).first()
        lot.base_daily_rate = 15
        space.base_price = 27
        db.session.add(
            PriceRule(
                tenant_id=tenant.id,
                lot_id=lot.id,
                name="Higher-priority rule",
                period="day",
                mode="flat",
                amount=1,
                priority=1000,
            )
        )
        db.session.commit()

        start = datetime.now(timezone.utc) + timedelta(days=10)
        cases = (
            (timedelta(days=1), Decimal("50.00")),
            (timedelta(days=3), Decimal("50.00")),
            (timedelta(days=6), Decimal("50.00")),
            (timedelta(days=6, hours=23, minutes=59), Decimal("50.00")),
            (timedelta(days=7), Decimal("70.00")),
            (timedelta(days=7, hours=1), Decimal("140.00")),
            (timedelta(days=14), Decimal("140.00")),
            (timedelta(days=14, seconds=1), Decimal("210.00")),
        )
        for duration, expected in cases:
            assert quote_space(space, lot, start, start + duration) == (expected, "EUR")


def test_non_eur_lots_keep_their_configured_price_rules(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.filter_by(tenant_id=tenant.id).first()
        space = Space.query.filter_by(lot_id=lot.id, tenant_id=tenant.id).first()
        lot.currency = "USD"
        lot.base_daily_rate = 15
        space.base_price = 27
        start = datetime.now(timezone.utc) + timedelta(days=10)
        assert quote_space(space, lot, start, start + timedelta(days=1)) == (Decimal("27.00"), "USD")

        db.session.add(
            PriceRule(
                tenant_id=tenant.id,
                lot_id=lot.id,
                name="Configured daily price",
                period="day",
                mode="per_day",
                amount=12,
                priority=10,
            )
        )
        db.session.commit()
        assert quote_space(space, lot, start, start + timedelta(days=1)) == (Decimal("12.00"), "USD")
