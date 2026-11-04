"""Shuttle: 10 EUR per person, at most 2 persons (incl. luggage); customers only see spaces they can book;
a stale CSRF token on checkout leads back to the hold instead of a bare 400."""
import re
from decimal import Decimal

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Booking, Extra, Lot, Space, Tenant
from app.seed import seed_demo
from app.services.booking_admin import add_extra, extra_cost, validate_persons
from app.services.bookings import BookingServiceError
from tests.booking_helpers import make_booking, seed_tenant, when
from tests.test_checkout_flow import _hold


def _shuttle(lot_id, tenant_id, price="10.00", max_persons=2):
    row = Extra(tenant_id=tenant_id, lot_id=lot_id, kind="shuttle", name="Flughafen-Shuttle", price=Decimal(price),
                per_person=True, included_persons=0, max_persons=max_persons)
    db.session.add(row)
    db.session.commit()
    return row


def test_shuttle_costs_ten_per_person_and_seats_two(app):
    booking = make_booking(total="70.00")
    shuttle = _shuttle(booking.lot_id, booking.tenant_id)
    assert [extra_cost(shuttle, n) for n in (1, 2)] == [Decimal("10.00"), Decimal("20.00")]
    validate_persons(shuttle, 2)
    with pytest.raises(BookingServiceError, match="at most 2 persons"):
        validate_persons(shuttle, 3)
    row = add_extra(booking, shuttle, "LH123", 2)
    assert row.quantity == 2 and row.total() == Decimal("20.00") and booking.total_amount == Decimal("90.00")


def test_checkout_adds_the_shuttle_per_person_and_refuses_a_third_person(client, app):
    tenant, lot, hold = _hold(app)
    shuttle = _shuttle(lot.id, tenant.id)
    base = f"/de/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    page = client.get(base).get_data(as_text=True)
    assert 'data-max="2"' in page and 'id="passengers-total"' in page
    form = {"first_name": "Ana", "email": "a@example.com", "phone": "+49123456", "vehicle_plate": "D-AB 123",
            "payment_method": "arrival", "accept_terms": "1", "extra_shuttle": str(shuttle.id)}
    too_many = client.post(base + "/book", data={**form, "passengers": "3"})
    assert "/booking/" not in too_many.headers["Location"] and Booking.query.count() == 0
    ok = client.post(base + "/book", data={**form, "passengers": "2"})
    assert "/booking/" in ok.headers["Location"]
    booking = Booking.query.one()
    assert booking.total_amount == Decimal("19.80") + Decimal("20.00")  # 2 days x 9.90 + 2 persons x 10.00


def test_customers_do_not_see_reserved_spaces(client, app):
    tenant = seed_tenant()
    lot = Lot.query.filter_by(tenant_id=tenant.id).first()
    from app.models import PriceRule
    db.session.add(PriceRule(tenant_id=tenant.id, lot_id=lot.id, name="Base", period="day", mode="per_day", amount=9.9))
    db.session.commit()
    spaces = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()
    make_booking(tenant, hours=3, days=1, space_index=0)  # inside the default 2h..26h window of the page
    html = client.get(f"/de/reserve/{tenant.slug}/{lot.slug}").get_data(as_text=True)
    assert f'data-space-id="{spaces[0].id}"' not in html
    assert f'data-space-id="{spaces[1].id}"' in html
    assert "s-reserved" not in html and "s-blocked" not in html


def test_stale_csrf_token_on_checkout_returns_to_the_hold(app):
    class Cfg(TestConfig):
        WTF_CSRF_ENABLED = True

    strict = create_app(Cfg)
    with strict.app_context():
        db.create_all()
        seed_demo(owner_email="o@t.dev", owner_password="correct-horse")
        tenant, lot, hold = _hold(strict)
        base = f"/de/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
        resp = strict.test_client().post(base + "/book", data={"first_name": "Ana", "csrf_token": "stale"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/holds/{hold.id}")
        db.session.remove()
        db.drop_all()


def test_refused_booking_keeps_what_the_customer_typed(client, app):
    tenant, lot, hold = _hold(app)
    shuttle = _shuttle(lot.id, tenant.id)
    base = f"/de/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    typed = {"first_name": "Ana", "last_name": "Muster", "email": "a@example.com", "phone": "+49123456", "vehicle_plate": "D-AB 123",
             "vehicle_make": "VW", "vehicle_model": "Golf", "notes": "Gate code please", "payment_method": "arrival",
             "accept_terms": "1", "extra_shuttle": str(shuttle.id), "passengers": "3", "extra_details_shuttle": "LH123"}
    refused = client.post(base + "/book", data=typed)
    assert refused.headers["Location"].endswith(f"/holds/{hold.id}") and Booking.query.count() == 0
    page = client.get(base).get_data(as_text=True)
    for text in ('value="Ana"', 'value="Muster"', 'value="+49123456"', 'value="D-AB 123"', 'value="VW"', 'value="Golf"',
                 'value="LH123"', "Gate code please", 'value="3"'):
        assert text in page, text
    assert f'value="{shuttle.id}"' in page and "checked" in page.split(f'value="{shuttle.id}"')[1][:260]
    assert 'value="Ana"' not in client.get(base).get_data(as_text=True)  # shown once, not stored for good


def test_saved_form_data_never_overflows_the_session_cookie(client, app):
    tenant, lot, hold = _hold(app)
    shuttle = _shuttle(lot.id, tenant.id)
    base = f"/de/reserve/{tenant.slug}/{lot.slug}/holds/{hold.id}"
    huge = {name: "ä" * 2000 for name in ("first_name", "last_name", "phone", "vehicle_plate", "vehicle_make", "vehicle_model", "company",
                                          "billing_email", "notes", "extra_details_shuttle")}
    response = client.post(base + "/book", data={**huge, "email": "a@example.com", "extra_shuttle": str(shuttle.id), "passengers": "3"})
    cookies = [value for key, value in response.headers if key.lower() == "set-cookie"]
    assert cookies and all(len(cookie) < 3800 for cookie in cookies)  # browsers drop cookies above ~4096 bytes
    assert client.get(base).status_code == 200
