from datetime import datetime

import pytest

from app.extensions import db
from app.models import Lot, PriceRule, Space, Tenant
from app.tenancy import use_tenant
from conftest import login


def test_price_rule_calculation_and_space_availability_defaults(app):
    with use_tenant(Tenant.query.first().id):
        lot = Lot.query.first()
        space = Space.query.filter_by(name="A1").one()
        rule = PriceRule(
            tenant_id=lot.tenant_id,
            lot_id=lot.id,
            space_id=space.id,
            name="Weekend premium",
            period="weekend",
            mode="per_day",
            amount=12.5,
            priority=10,
        )
        db.session.add(rule)
        db.session.commit()

        assert space.default_available is True
        assert space.availability_for(None, None) is True
        assert rule.calculate_for(
            datetime(2025, 1, 4, 9, 0),
            datetime(2025, 1, 4, 15, 0),
        ) == pytest.approx(12.5)


def test_lot_and_space_forms_accept_configurable_pricing_defaults(client, app):
    login(client)
    payload = {
        "name": "Test Lot",
        "address": "Airport Road 1",
        "city": "Dusseldorf",
        "canvas_w": "1000",
        "canvas_h": "700",
        "base_daily_rate": "12.50",
        "currency": "EUR",
        "active": "y",
    }
    response = client.post("/admin/lots/new", data=payload)
    assert response.status_code == 302

    with use_tenant(Tenant.query.first().id):
        lot = Lot.query.filter_by(name="Test Lot").one()
        lot_id = lot.id
        space = Space.query.filter_by(lot_id=lot_id).first()
        assert lot.base_daily_rate == 12.50
        assert space is not None

    space_payload = {
        "name": "X1",
        "kind": "small",
        "status": "active",
        "covered": "y",
        "ev_charging": "y",
        "base_price": "7.25",
        "min_hours": "2",
        "max_days": "10",
        "default_available": "y",
        "x": "40",
        "y": "40",
        "w": "80",
        "h": "110",
        "bus_distance_m": "120",
    }
    response = client.post(f"/admin/lots/{lot_id}/spaces/new", data=space_payload)
    assert response.status_code == 302

    with use_tenant(Tenant.query.first().id):
        space = Space.query.filter_by(lot_id=lot_id, name="X1").one()
        assert space.base_price == 7.25
        assert space.min_hours == 2
        assert space.max_days == 10
        assert space.default_available is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_daily_rate", "-1"),
        ("canvas_w", "199"),
    ],
)
def test_lot_form_rejects_invalid_pricing_and_map_dimensions(client, field, value):
    login(client)
    payload = {
        "name": "Invalid Lot",
        "address": "",
        "city": "",
        "canvas_w": "1000",
        "canvas_h": "700",
        "base_daily_rate": "12.50",
        "currency": "EUR",
        "active": "y",
        field: value,
    }

    response = client.post("/admin/lots/new", data=payload)

    assert response.status_code == 200
    assert Lot.query.filter_by(name="Invalid Lot").first() is None
