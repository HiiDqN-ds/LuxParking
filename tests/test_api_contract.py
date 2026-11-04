"""The published OpenAPI description must match what the API really returns (contract tests)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_openapi

from app.api_spec import OPENAPI_SPEC
from app.extensions import db
from app.models import Extra, Lot, Space, Tenant
from app.services.api_platform import SECRET_SCOPES, issue_api_key


def check(name: str, payload) -> None:
    """Validate `payload` against components.schemas.<name> of the published spec."""
    schema = {"$ref": f"#/components/schemas/{name}", "components": OPENAPI_SPEC["components"]}
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    assert not errors, f"{name}: " + "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5])


@pytest.fixture()
def api(app, client, staff):
    tenant = Tenant.query.first()
    lot = Lot.query.filter_by(tenant_id=tenant.id).first()
    lot.base_daily_rate = Decimal("9.90")
    extra = Extra(tenant_id=tenant.id, lot_id=lot.id, kind="shuttle", name="Shuttle", price=Decimal("10"), per_person=True, included_persons=0, max_persons=2)
    db.session.add(extra)
    db.session.commit()
    _, token = issue_api_key(tenant_id=tenant.id, name="contract", key_type="sk", scopes=sorted(SECRET_SCOPES), allowed_origins=[], requests_per_minute=600)
    space = Space.query.filter_by(lot_id=lot.id).first()
    start = datetime.now(timezone.utc) + timedelta(days=5)
    return type("Api", (), dict(
        client=client, H={"Authorization": f"Bearer {token}"}, lot=lot, space=space, extra=extra, staff_id=staff.id,
        window={"from": start.isoformat(), "to": (start + timedelta(days=3)).isoformat()}))


def test_the_description_is_valid_openapi_3_1():
    validate_openapi(OPENAPI_SPEC)
    assert OPENAPI_SPEC["openapi"].startswith("3.1")
    assert OPENAPI_SPEC["webhooks"]["event"]


def test_every_documented_operation_exists_and_every_route_is_documented(app):
    documented = {(path, method.upper()) for path, item in OPENAPI_SPEC["paths"].items() for method in item}
    real = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/v1/") or rule.endpoint in {"api.openapi", "api.docs"}:
            continue
        path = rule.rule[len("/v1"):]
        for part in ("int:", "string:"):
            path = path.replace(f"<{part}", "<")
        import re
        path = re.sub(r"<(\w+)>", r"{\1}", path)
        real |= {(path, m) for m in rule.methods - {"HEAD", "OPTIONS"}}
    assert documented == real, f"undocumented: {sorted(real - documented)}; not implemented: {sorted(documented - real)}"


def test_catalogue_and_availability_match_the_schemas(api):
    c, H = api.client, api.H
    assert c.get("/v1/health").status_code == 200
    check("Health", c.get("/v1/health").get_json())
    check("Products", c.get("/v1/products", headers=H).get_json())
    check("Extras", c.get(f"/v1/extras?lot_id={api.lot.id}", headers=H).get_json())
    check("Availability", c.get("/v1/availability", query_string={"lot_id": api.lot.id, **api.window}, headers=H).get_json())
    check("LotMap", c.get(f"/v1/lots/{api.lot.id}/map", query_string=api.window, headers=H).get_json())


def test_the_whole_booking_flow_matches_the_schemas(api):
    c, H = api.client, api.H
    body = {"lot_id": api.lot.id, "space_id": api.space.id, **api.window}
    quote = c.post("/v1/quotes", json={**body, "extras": [{"id": api.extra.id, "quantity": 2}]}, headers=H)
    assert quote.status_code == 200
    check("QuoteEnvelope", quote.get_json())
    hold = c.post("/v1/holds", json={**body, "email": "a@example.de"}, headers={**H, "Idempotency-Key": "contract-hold-1"})
    assert hold.status_code == 201
    check("HoldEnvelope", hold.get_json())
    booking = c.post("/v1/bookings", json={"hold_id": hold.get_json()["hold"]["id"], "first_name": "Anna", "email": "a@example.de", "phone": "+491511234567",
                                           "extras": [{"id": api.extra.id, "quantity": 2, "details": "LH 1"}], "test_payment": True, "send_notifications": False},
                     headers={**H, "Idempotency-Key": "contract-book-1"})
    assert booking.status_code == 201
    check("BookingResponse", booking.get_json())
    bid = booking.get_json()["booking"]["id"]
    check("BookingList", c.get("/v1/bookings", headers=H).get_json())
    check("BookingResponse", c.get(f"/v1/bookings/{bid}", headers=H).get_json())
    patched = c.patch(f"/v1/bookings/{bid}", json={"notes": "Kindersitz"}, headers={**H, "Idempotency-Key": "contract-patch-1"})
    assert patched.status_code == 200
    check("BookingResponse", patched.get_json())
    invoice = c.get(f"/v1/bookings/{bid}/invoice", headers=H)
    assert invoice.status_code == 200 and invoice.mimetype == "application/pdf" and invoice.data.startswith(b"%PDF")
    started = c.post(f"/v1/bookings/{bid}/inspections", json={"phase": "arrival", "staff_user_id": api.staff_id}, headers={**H, "Idempotency-Key": "contract-insp-1"})
    assert started.status_code == 201
    check("InspectionEnvelope", started.get_json())
    check("InspectionList", c.get(f"/v1/bookings/{bid}/inspections", headers=H).get_json())
    checked_in = c.post(f"/v1/bookings/{bid}/check-in", headers={**H, "Idempotency-Key": "contract-in-1"})
    check("BookingResponse" if checked_in.status_code == 200 else "Error", checked_in.get_json())
    cancelled = c.delete(f"/v1/bookings/{bid}", headers={**H, "Idempotency-Key": "contract-del-1"})
    check("BookingResponse" if cancelled.status_code == 200 else "Error", cancelled.get_json())


def test_webhooks_keys_and_usage_match_the_schemas(api, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])  # a public address, no real DNS
    c, H = api.client, api.H
    created = c.post("/v1/webhooks", json={"name": "CRM", "url": "https://example.com/hook", "events": ["booking.created", "booking.paid"]}, headers=H)
    assert created.status_code == 201, created.get_json()
    check("WebhookCreatedEnvelope", created.get_json())
    wid = created.get_json()["webhook"]["id"]
    check("WebhookList", c.get("/v1/webhooks", headers=H).get_json())
    check("DeliveryList", c.get("/v1/webhook-deliveries", headers=H).get_json())
    check("RotatedSecret", c.post(f"/v1/webhooks/{wid}/rotate-secret", headers=H).get_json())
    check("Revoked", c.delete(f"/v1/webhooks/{wid}", headers=H).get_json())
    check("ApiKeyList", c.get("/v1/api-keys", headers=H).get_json())
    key = c.post("/v1/api-keys", json={"name": "widget", "type": "pk", "scopes": ["map:read", "availability:read"]}, headers=H)
    assert key.status_code == 201
    check("ApiKeyCreatedEnvelope", key.get_json())
    check("UsageList", c.get("/v1/usage", headers=H).get_json())


def test_errors_use_the_documented_shape_and_carry_a_request_id(api):
    c = api.client
    missing = c.get("/v1/products")
    assert missing.status_code == 401
    check("Error", missing.get_json())
    assert missing.get_json()["error"]["request_id"] == missing.headers["X-Request-Id"]
    notfound = c.get("/v1/bookings/999999", headers=api.H)
    assert notfound.status_code == 404
    check("Error", notfound.get_json())
    bad = c.post("/v1/quotes", json={"lot_id": 1}, headers=api.H)
    assert bad.status_code == 400
    check("Error", bad.get_json())


def test_every_response_has_request_id_and_rate_limit_headers(api):
    ok = api.client.get("/v1/products", headers={**api.H, "X-Request-Id": "trace-from-caller-1"})
    assert ok.headers["X-Request-Id"] == "trace-from-caller-1"  # a caller's id is kept for end-to-end tracing
    assert ok.headers["X-RateLimit-Limit"] == "600" and ok.headers["X-ParkPilot-API-Version"] == "1"
    assert int(ok.headers["X-RateLimit-Remaining"]) >= 0
    hostile = api.client.get("/v1/products", headers={**api.H, "X-Request-Id": "bad value! with spaces"})
    assert hostile.headers["X-Request-Id"].startswith("req_")  # an unsafe id is replaced, never echoed


def test_old_docs_url_redirects_to_the_self_hosted_reference(client):
    response = client.get("/v1/docs")
    assert response.status_code == 301 and response.headers["Location"].endswith("/developers/reference")


def test_the_secret_shown_at_creation_is_the_one_that_signs_deliveries(api, monkeypatch):
    """The documented flow: create endpoint -> store the signing_secret -> verify X-ParkPilot-Signature with it."""
    from app.models import WebhookEndpoint
    from app.services.api_platform import webhook_signing_secret
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    created = api.client.post("/v1/webhooks", json={"name": "t", "url": "https://example.com/h", "events": ["booking.created"]}, headers=api.H)
    shown = created.get_json()["webhook"]["signing_secret"]
    endpoint = db.session.get(WebhookEndpoint, created.get_json()["webhook"]["id"])
    assert shown == webhook_signing_secret(endpoint)
