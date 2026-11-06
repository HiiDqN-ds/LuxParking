from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    DiscountCode,
    Lot,
    PriceRule,
    Space,
    Tenant,
    TenantPage,
    TenantTranslation,
)
from app.services.api_platform import issue_api_key
from app.services.availability import quote_space
from app.services.bookings import (
    BookingValidationError,
    create_booking_from_hold,
    create_hold,
)
from app.services.pricing import DiscountUnavailable, quote_discount
from app.tenancy import use_tenant
from conftest import login


def _future_start(days=14):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0
    )


def test_highest_priority_price_rule_wins_and_space_scope_breaks_ties(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        lot.currency = "USD"
        space = Space.query.filter_by(lot_id=lot.id).first()
        start = _future_start()
        end = start + timedelta(days=2)
        db.session.add_all(
            [
                PriceRule(
                    tenant_id=tenant.id,
                    lot_id=lot.id,
                    name="Lot rate",
                    period="day",
                    mode="flat",
                    amount=80,
                    priority=20,
                ),
                PriceRule(
                    tenant_id=tenant.id,
                    lot_id=lot.id,
                    space_id=space.id,
                    name="Space rate tie-break",
                    period="day",
                    mode="flat",
                    amount=70,
                    priority=20,
                ),
                PriceRule(
                    tenant_id=tenant.id,
                    lot_id=lot.id,
                    name="Higher priority",
                    period="day",
                    mode="flat",
                    amount=90,
                    priority=21,
                ),
            ]
        )
        db.session.commit()

        amount, currency = quote_space(space, lot, start, end)

        assert amount == Decimal("90.00")
        assert currency == lot.currency


def test_discount_is_tenant_scoped_and_redemption_limit_is_enforced(app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        code = DiscountCode(
            tenant_id=tenant.id,
            code="SAVE10",
            discount_type="percentage",
            value=10,
            minimum_amount=20,
            max_redemptions=1,
        )
        db.session.add(code)
        db.session.commit()

        quote = quote_discount(
            tenant_id=tenant.id,
            code=" save10 ",
            subtotal=Decimal("100.00"),
        )
        assert (quote.code, quote.discount, quote.total) == (
            "SAVE10",
            Decimal("10.00"),
            Decimal("90.00"),
        )
        with pytest.raises(DiscountUnavailable):
            quote_discount(
                tenant_id=tenant.id + 1,
                code="SAVE10",
                subtotal=Decimal("100.00"),
            )

        start = _future_start()
        hold = create_hold(
            tenant_id=tenant.id,
            lot_id=lot.id,
            space_id=space.id,
            pickup_at=start,
            return_at=start + timedelta(days=1),
            customer_email="guest@example.test",
        )
        booking = create_booking_from_hold(
            tenant_id=tenant.id,
            hold_id=hold.id,
            first_name="Guest",
            email="guest@example.test",
            phone="+49123456789",
            total_amount=quote.total,
            discount_base_amount=quote.subtotal,
            currency="EUR",
            discount_code=quote.code,
        )
        assert booking.discount_code == "SAVE10"
        assert booking.total_amount == Decimal("90.00")
        assert db.session.get(DiscountCode, code.id).redemption_count == 1

        another_start = start + timedelta(days=5)
        another_hold = create_hold(
            tenant_id=tenant.id,
            lot_id=lot.id,
            space_id=space.id,
            pickup_at=another_start,
            return_at=another_start + timedelta(days=1),
            customer_email="second@example.test",
        )
        with pytest.raises(BookingValidationError):
            create_booking_from_hold(
                tenant_id=tenant.id,
                hold_id=another_hold.id,
                first_name="Second",
                email="second@example.test",
                phone="+49123456789",
                total_amount=Decimal("90.00"),
                discount_base_amount=Decimal("100.00"),
                currency="EUR",
                discount_code="SAVE10",
            )


def test_owner_can_manage_rules_discounts_brand_translations_and_content(client, app):
    login(client)
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()

    created_rule = client.post(
        "/admin/pricing",
        data={
            "name": "Summer pricing",
            "lot_id": str(lot.id),
            "space_id": "0",
            "period": "season",
            "mode": "per_day",
            "amount": "18.50",
            "start_day": "2027-06-01",
            "end_day": "2027-08-31",
            "priority": "120",
            "active": "y",
        },
    )
    assert created_rule.status_code == 302
    with use_tenant(tenant.id):
        rule = PriceRule.query.filter_by(name="Summer pricing").one()
        assert rule.lot_id == lot.id
        assert rule.space_id is None

    created_discount = client.post(
        "/admin/discounts",
        data={
            "code": "AIRPORT10",
            "discount_type": "percentage",
            "value": "10",
            "minimum_amount": "0",
            "max_redemptions": "",
            "active": "y",
        },
    )
    assert created_discount.status_code == 302
    with use_tenant(tenant.id):
        discount = DiscountCode.query.filter_by(code="AIRPORT10").one()
        assert discount.max_redemptions is None
        discount_id = discount.id

    updated_discount = client.post(
        f"/admin/discounts/{discount_id}/edit",
        data={
            "code": "AIRPORT12",
            "discount_type": "percentage",
            "value": "12",
            "minimum_amount": "10",
            "max_redemptions": "",
            "active": "y",
        },
    )
    assert updated_discount.status_code == 302
    with use_tenant(tenant.id):
        edited_discount = db.session.get(DiscountCode, discount_id)
        assert edited_discount.code == "AIRPORT12"
        assert edited_discount.value == Decimal("12.00")

    brand = client.post(
        "/admin/brand",
        data={
            "logo_url": "",
            "primary_color": "#123ABC",
            "accent_color": "#AB1234",
            "custom_domain": "booking.example.test",
        },
    )
    assert brand.status_code == 302
    assert b"DNS and TLS" in client.get("/admin/brand").data

    translation = client.post(
        "/admin/translations",
        data={
            "locale": "de",
            "key": "home.eyebrow",
            "value": "Individuelle ParkPilot-Überschrift",
        },
    )
    assert translation.status_code == 302
    with use_tenant(tenant.id):
        assert TenantTranslation.query.filter_by(
            locale="de", key="home.eyebrow"
        ).one().value == "Individuelle ParkPilot-Überschrift"
    assert "Individuelle ParkPilot-Überschrift".encode() in client.get("/de/").data

    page = client.post(
        "/admin/content/new",
        data={
            "kind": "blog",
            "locale": "en",
            "slug": "airport-parking-guide",
            "title": "Airport parking guide",
            "summary": "A tenant-written guide.",
            "body": "Compare your options before you travel.",
            "seo_title": "Airport parking guide",
            "seo_description": "A guide to parking near the airport.",
            "published": "y",
        },
    )
    assert page.status_code == 302
    public_page = client.get(
        f"/en/content/{tenant.slug}/blog/airport-parking-guide"
    )
    assert public_page.status_code == 200
    assert b"Compare your options before you travel." in public_page.data
    assert b"<link rel=\"canonical\"" in public_page.data
    assert b"booking.example.test" not in public_page.data
    assert b"airport-parking-guide" in client.get("/sitemap.xml").data
    public_index = client.get(f"/en/content/{tenant.slug}/blog")
    assert public_index.status_code == 200
    assert b"<title>Blog | " + tenant.name.encode() + b"</title>" in public_index.data
    assert b"hreflang=\"en\"" in public_index.data
    assert b"hreflang=\"de\"" not in public_index.data
    assert b"Find a parking space" in public_index.data

    empty_localized_index = client.get(f"/de/content/{tenant.slug}/blog")
    assert empty_localized_index.status_code == 200
    assert b'<meta name="robots" content="noindex,follow">' in empty_localized_index.data
    assert b"hreflang=\"de\"" not in empty_localized_index.data
    assert b"Ratgeber" in empty_localized_index.data


def test_public_faq_index_uses_tenant_questions_in_structured_data(client, app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        db.session.add(
            TenantPage(
                tenant_id=tenant.id,
                kind="faq",
                locale="en",
                slug="free-cancellation",
                title="Can I cancel for free?",
                body="The lot operator sets the cancellation terms.",
                published=True,
                published_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    response = client.get(f"/en/content/{tenant.slug}/faq")

    assert response.status_code == 200
    assert b"Can I cancel for free?" in response.data
    assert b"The lot operator sets the cancellation terms." in response.data
    assert b"FAQPage" in response.data


def test_api_quote_applies_one_server_validated_discount(client, app):
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        space = Space.query.filter_by(lot_id=lot.id).first()
        lot.base_daily_rate = 25
        space.base_price = 0
        code = DiscountCode(
            tenant_id=tenant.id,
            code="ONECODE",
            discount_type="fixed",
            value=5,
            minimum_amount=0,
        )
        db.session.add(code)
        db.session.commit()
        lot_id, space_id = lot.id, space.id
    _, token = issue_api_key(
        tenant_id=tenant.id,
        name="discount quote test",
        key_type="sk",
        scopes=["quotes:write"],
        allowed_origins=[],
        requests_per_minute=60,
    )
    start = _future_start()
    response = client.post(
        "/v1/quotes",
        json={
            "lot_id": lot_id,
            "space_id": space_id,
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
            "discount_code": "onecode",
            "total": "0.01",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json["quote"]["subtotal"] == "50.00"
    assert response.json["quote"]["discount"] == "5.00"
    assert response.json["quote"]["total"] == "45.00"

    invalid = client.post(
        "/v1/quotes",
        json={
            "lot_id": lot_id,
            "space_id": space_id,
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
            "discount_code": "OTHER-TENANT-CODE",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert invalid.status_code == 400
    assert invalid.json["error"]["code"] == "invalid_discount"


def test_admin_management_pages_and_public_content_are_tenant_scoped(
    client, app, other_tenant
):
    login(client)
    other_tenant_id = other_tenant[0].id
    with use_tenant(other_tenant_id):
        foreign_page = TenantPage(
            tenant_id=other_tenant_id,
            kind="blog",
            locale="en",
            slug="private-draft",
            title="Private draft",
            body="Not visible to another tenant.",
            published=False,
        )
        db.session.add(foreign_page)
        db.session.commit()
        foreign_page_id = foreign_page.id

    for path in (
        "/admin/pricing",
        "/admin/discounts",
        "/admin/brand",
        "/admin/translations",
        "/admin/content",
    ):
        assert client.get(path).status_code == 200
    assert client.get(f"/admin/content/{foreign_page_id}/edit").status_code == 404
