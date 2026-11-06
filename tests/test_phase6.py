from datetime import datetime, timedelta, timezone
import html
from xml.etree import ElementTree

from app.extensions import db
from app.i18n import supported_locales, translation_keys
from app.models import Lot, Space, Tenant
from app.tenancy import use_tenant


def public_lot():
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        lot.base_daily_rate = 15
        spaces = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()
        spaces[0].base_price = 0
        db.session.commit()
        return tenant.slug, lot.slug, tenant.id, lot.id, spaces[0].id


def test_home_language_negotiation_metadata_and_rtl(client):
    negotiated = client.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    assert negotiated.status_code == 200
    assert b"lang=\"fr\"" in negotiated.data
    assert b"hreflang=\"x-default\"" in negotiated.data
    assert b"rel=\"canonical\"" in negotiated.data
    assert b"og:title" in negotiated.data
    assert b"application/ld+json" in negotiated.data
    german_home = client.get("/de/")
    assert "Reservieren Sie Ihren Parkplatz bei".encode() in german_home.data
    assert b'href="/tr/"' in german_home.data

    arabic = client.get("/ar/")
    assert arabic.status_code == 200
    assert b"<html lang=\"ar\" dir=\"rtl\"" in arabic.data
    assert "العربية".encode() in arabic.data

    turkish = client.get("/tr/")
    assert turkish.status_code == 200
    assert b"<html lang=\"tr\"" in turkish.data
    assert b'hreflang="tr"' in turkish.data
    assert b"konumunda park yerinizi bulun" in turkish.data
    assert b'og:locale" content="tr_TR"' in turkish.data


def test_translation_catalogs_have_the_same_keys(app):
    with app.app_context():
        expected = set(translation_keys("en", ""))
        for locale in supported_locales():
            assert set(translation_keys(locale, "")) == expected


def test_localized_map_renders_translated_copy_and_seo(client):
    tenant_slug, lot_slug, *_ = public_lot()
    response = client.get(f"/de/reserve/{tenant_slug}/{lot_slug}")

    assert response.status_code == 200
    assert b"Live-Parkplatzkarte" in response.data
    assert b"Choose your space" not in response.data
    assert b'hreflang="de"' in response.data
    assert b'hreflang="ar"' in response.data
    assert b"FAQPage" in response.data
    assert "Ist ein temporärer Platzhalter eine bestätigte Buchung?".encode() in response.data

    turkish = client.get(f"/tr/reserve/{tenant_slug}/{lot_slug}")
    assert turkish.status_code == 200
    assert b'<html lang="tr"' in turkish.data
    assert "Park yerinizi seçin" in html.unescape(turkish.get_data(as_text=True))
    assert b'hreflang="tr"' in turkish.data


def test_localized_availability_error_uses_selected_locale(client):
    tenant_slug, lot_slug, *_ = public_lot()
    response = client.get(
        f"/ar/reserve/{tenant_slug}/{lot_slug}/availability",
        query_string={
            "pickup_at": "2027-01-01T12:00",
            "return_at": "2027-01-02T12:00",
        },
    )
    assert response.status_code == 400
    assert "اختر وقت تسليم".encode() in response.json["error"].encode()


def test_localized_hold_keeps_locale_and_is_not_indexable(client):
    tenant_slug, lot_slug, _, _, space_id = public_lot()
    pickup = datetime.now(timezone.utc) + timedelta(days=2)
    returned = pickup + timedelta(hours=24)
    response = client.post(
        f"/de/reserve/{tenant_slug}/{lot_slug}/holds",
        data={
            "space_id": str(space_id),
            "pickup_at": pickup.isoformat(),
            "return_at": returned.isoformat(),
            "email": "gast@example.com",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith(
        f"/de/reserve/{tenant_slug}/{lot_slug}/holds/"
    )
    confirmation = client.get(response.headers["Location"])
    assert confirmation.status_code == 200
    assert b"noindex,nofollow" in confirmation.data
    assert "Ihr Platz ist zehn Minuten reserviert".encode() in confirmation.data
    assert b"dir=\"ltr\"" in confirmation.data


def test_sitemap_and_robots_cover_public_locales(client):
    sitemap_response = client.get("/sitemap.xml")
    assert sitemap_response.status_code == 200
    root = ElementTree.fromstring(sitemap_response.data)
    locs = [element.text for element in root.iter() if element.tag.endswith("}loc")]
    assert any("/de/" in url for url in locs)
    assert any("/ar/" in url for url in locs)
    alternates = [element for element in root.iter() if element.tag.endswith("}link")]
    assert any(element.attrib.get("hreflang") == "x-default" for element in alternates)

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert b"Sitemap: http://" in robots.data
    assert b"Disallow: /admin/" in robots.data
