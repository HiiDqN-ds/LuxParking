"""Phase 10: embeddable widget, WordPress plugin package and the integration guide."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_widget_script_is_served_publicly_and_cacheable(client):
    response = client.get("/widget.js")
    assert response.status_code == 200 and response.mimetype == "application/javascript"
    assert response.headers["Access-Control-Allow-Origin"] == "*" and "max-age" in response.headers["Cache-Control"]
    body = response.get_data(as_text=True)
    assert "/v1/availability" in body and "pickup_local" in body
    assert "sk_" not in body.replace("pk_", "")  # a browser script must never mention secret keys


def test_widget_covers_all_launch_languages(client):
    body = client.get("/widget.js").get_data(as_text=True)
    for lang in ("de:", "en:", "nl:", "fr:", "es:", "ar:"):
        assert lang in body


def test_reserve_page_accepts_widget_dates(client):
    script = (ROOT / "app" / "static" / "js" / "booking.js").read_text(encoding="utf-8")
    assert "pickup_local" in script and "return_local" in script


def test_wordpress_plugin_package_is_complete_and_public_key_only():
    folder = ROOT / "integrations" / "wordpress" / "parkpilot-booking"
    php = (folder / "parkpilot-booking.php").read_text(encoding="utf-8")
    assert "add_shortcode('parkpilot'" in php and "pk_" in php and "esc_attr" in php
    assert (folder / "readme.txt").is_file()
    with zipfile.ZipFile(ROOT / "integrations" / "wordpress" / "parkpilot-booking.zip") as archive:
        names = archive.namelist()
    assert "parkpilot-booking/parkpilot-booking.php" in names and "parkpilot-booking/readme.txt" in names


def test_integration_guide_has_examples_in_three_languages():
    guide = (ROOT / "docs" / "INTEGRATION.md").read_text(encoding="utf-8")
    for needle in ("```js", "```php", "```python", "widget.js", "Idempotency-Key", "/invoice"):
        assert needle in guide


def test_openapi_documents_the_invoice_endpoints(client, app):
    from app.services.api_platform import issue_api_key
    from tests.booking_helpers import seed_tenant
    token = issue_api_key(tenant_id=seed_tenant().id, name="doc", key_type="sk", scopes=["bookings:read"], allowed_origins=[], requests_per_minute=60)[1]
    spec = client.get("/v1/openapi.json", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert "/bookings/{booking_id}/invoice" in spec["paths"] and "/bookings/{booking_id}/credit-note" in spec["paths"]
