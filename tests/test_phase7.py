import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import (
    ApiKey,
    Booking,
    Hold,
    Lot,
    Space,
    Tenant,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services.api_platform import (
    create_webhook_endpoint,
    deliver_pending_webhooks,
    enqueue_webhook_event,
    issue_api_key,
    webhook_signing_secret,
)
from app.tenancy import use_tenant


def api_tenant():
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot = Lot.query.first()
        lot.base_daily_rate = 18
        spaces = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()
        spaces[0].base_price = 0
        db.session.commit()
        return tenant.id, lot.id, spaces[0].id


def api_credential(tenant_id, scopes, *, key_type="sk", origins=None, limit=60):
    _issued_key, token = issue_api_key(
        tenant_id=tenant_id,
        name="phase 7 test",
        key_type=key_type,
        scopes=scopes,
        allowed_origins=origins or [],
        requests_per_minute=limit,
    )
    return token


def api_headers(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def date_range():
    start = datetime.now(timezone.utc) + timedelta(days=3)
    return start, start + timedelta(hours=24)


def test_api_requires_valid_bearer_and_exposes_openapi(client):
    missing = client.get("/v1/products")
    assert missing.status_code == 401
    assert missing.json["error"]["code"] == "authentication_required"

    spec = client.get("/v1/openapi.json")
    assert spec.status_code == 200
    assert spec.json["openapi"] == "3.1.0"
    assert "/bookings/{booking_id}/check-in" in spec.json["paths"]
    docs = client.get("/v1/docs")  # the reference moved to the self-hosted developer portal (no third-party Swagger UI)
    assert docs.status_code == 301 and docs.headers["Location"].endswith("/developers/reference")
    assert client.get("/developers/reference").status_code == 200


def test_api_key_hash_scope_rate_limit_and_origin_allowlist(client):
    tenant_id, _lot_id, _space_id = api_tenant()
    token = api_credential(
        tenant_id,
        ["products:read"],
        key_type="pk",
        origins=["https://shop.example"],
        limit=2,
    )
    with use_tenant(tenant_id):
        stored = db.session.execute(select(ApiKey)).scalars().one()
        assert token not in stored.token_hash
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()

    denied_origin = client.get(
        "/v1/products",
        headers=api_headers(token, Origin="https://attacker.example"),
    )
    assert denied_origin.status_code == 403

    no_scope = client.post(
        "/v1/quotes",
        json={},
        headers=api_headers(token, Origin="https://shop.example"),
    )
    assert no_scope.status_code == 403
    assert no_scope.json["error"]["code"] == "forbidden"

    allowed = client.get(
        "/v1/products",
        headers=api_headers(token, Origin="https://shop.example"),
    )
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://shop.example"
    rate_limited = client.get(
        "/v1/products",
        headers=api_headers(token, Origin="https://shop.example"),
    )
    assert rate_limited.status_code == 429
    assert rate_limited.headers["Retry-After"] == "60"


def test_public_keys_cannot_be_created_with_booking_read_scope(app):
    from app.services.api_platform import ApiPlatformError

    tenant_id, _lot_id, _space_id = api_tenant()
    with app.app_context():
        with pytest.raises(ApiPlatformError):
            issue_api_key(
                tenant_id=tenant_id,
                name="over-scoped public key",
                key_type="pk",
                scopes=["bookings:read"],
                allowed_origins=[],
                requests_per_minute=60,
            )


def test_public_key_cannot_access_other_tenant_map(client, other_tenant):
    tenant_id, _lot_id, _space_id = api_tenant()
    token = api_credential(tenant_id, ["map:read"], key_type="pk")
    foreign_lot_id = other_tenant[1].id
    start, end = date_range()
    response = client.get(
        f"/v1/lots/{foreign_lot_id}/map",
        query_string={"from": start.isoformat(), "to": end.isoformat()},
        headers=api_headers(token),
    )
    assert response.status_code == 404


def test_quote_and_hold_use_tenant_pricing_and_idempotency(client):
    tenant_id, lot_id, space_id = api_tenant()
    token = api_credential(tenant_id, ["quotes:write", "holds:write"])
    start, end = date_range()
    interval = {"from": start.isoformat(), "to": end.isoformat()}
    quote = client.post(
        "/v1/quotes",
        json={"lot_id": lot_id, "space_id": space_id, **interval},
        headers=api_headers(token),
    )
    assert quote.status_code == 200
    assert quote.json["quote"]["total"] == "18.00"

    headers = api_headers(token, **{"Idempotency-Key": "hold-request-001"})
    request_body = {
        "lot_id": lot_id,
        "space_id": space_id,
        "email": "api@example.com",
        **interval,
    }
    first = client.post("/v1/holds", json=request_body, headers=headers)
    replay = client.post("/v1/holds", json=request_body, headers=headers)
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.data == first.data
    with use_tenant(tenant_id):
        assert Hold.query.filter_by(space_id=space_id).count() == 1

    changed = client.post(
        "/v1/holds",
        json={**request_body, "email": "other@example.com"},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json["error"]["code"] == "idempotency_key_reused"


def test_booking_uses_server_quote_and_fake_payment_is_nonproduction_only(client):
    tenant_id, lot_id, space_id = api_tenant()
    with use_tenant(tenant_id):
        db.session.add(
            WebhookEndpoint(
                tenant_id=tenant_id,
                name="booking events",
                url="https://hooks.example/events",
                events=["booking.created", "booking.paid"],
                signing_salt="a" * 64,
                active=True,
            )
        )
        db.session.commit()
    token = api_credential(
        tenant_id,
        ["holds:write", "bookings:write", "bookings:read"],
    )
    start, end = date_range()
    hold = client.post(
        "/v1/holds",
        json={
            "lot_id": lot_id,
            "space_id": space_id,
            "email": "guest@example.com",
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        headers=api_headers(token),
    )
    assert hold.status_code == 201
    created = client.post(
        "/v1/bookings",
        json={
            "hold_id": hold.json["hold"]["id"],
            "first_name": "Guest",
            "email": "guest@example.com",
            "phone": "+49123456789",
            "total_amount": "0.01",
            "currency": "USD",
            "test_payment": True,
        },
        headers=api_headers(token, **{"Idempotency-Key": "booking-request-01"}),
    )
    assert created.status_code == 201, created.json
    booking_data = created.json["booking"]
    assert booking_data["total_amount"] == "18.00"
    assert booking_data["currency"] == "EUR"
    assert booking_data["payment_status"] == "paid"

    fetched = client.get(
        f"/v1/bookings/{booking_data['id']}",
        headers=api_headers(token),
    )
    assert fetched.status_code == 200
    check_in = client.post(
        f"/v1/bookings/{booking_data['id']}/check-in",
        headers=api_headers(token),
    )
    assert check_in.status_code == 409
    assert check_in.json["error"]["code"] == "conflict"

    with use_tenant(tenant_id):
        booking = db.session.execute(
            select(Booking).where(Booking.id == booking_data["id"])
        ).scalar_one()
        assert booking.total_amount == 18
        assert booking.payment_status == "paid"
        event_types = set(
            db.session.execute(
                select(WebhookDelivery.event_type).where(
                    WebhookDelivery.tenant_id == tenant_id,
                )
            ).scalars()
        )
        assert event_types == {"booking.created", "booking.paid"}


def test_production_rejects_fake_payment_before_converting_hold(app, client):
    tenant_id, lot_id, space_id = api_tenant()
    token = api_credential(tenant_id, ["holds:write", "bookings:write"])
    start, end = date_range()
    hold = client.post(
        "/v1/holds",
        json={
            "lot_id": lot_id,
            "space_id": space_id,
            "email": "production@example.com",
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        headers=api_headers(token),
    )
    assert hold.status_code == 201

    app.config["APP_ENV"] = "production"
    refused = client.post(
        "/v1/bookings",
        json={
            "hold_id": hold.json["hold"]["id"],
            "first_name": "Guest",
            "email": "production@example.com",
            "phone": "+49123456789",
            "test_payment": True,
        },
        headers=api_headers(token),
    )
    assert refused.status_code == 403
    with use_tenant(tenant_id):
        assert Booking.query.count() == 0
        assert Hold.query.filter_by(id=hold.json["hold"]["id"], status="active").count() == 1


def test_secret_key_management_returns_token_once_and_can_revoke(client):
    tenant_id, _lot_id, _space_id = api_tenant()
    admin_token = api_credential(tenant_id, ["keys:manage"])
    response = client.post(
        "/v1/api-keys",
        json={"name": "Widget", "type": "pk", "scopes": ["map:read", "availability:read"]},
        headers=api_headers(admin_token),
    )
    assert response.status_code == 201
    key_data = response.json["api_key"]
    issued_token = key_data["token"]
    assert issued_token.startswith("pk_")
    assert client.get("/v1/api-keys", headers=api_headers(admin_token)).status_code == 200

    revoke = client.delete(
        f"/v1/api-keys/{key_data['id']}",
        headers=api_headers(admin_token),
    )
    assert revoke.status_code == 200
    invalid = client.get("/v1/products", headers=api_headers(issued_token))
    assert invalid.status_code == 401


def test_webhook_signature_and_retry_log(app, monkeypatch):
    tenant_id, _lot_id, _space_id = api_tenant()
    with app.app_context():
        monkeypatch.setattr(
            "app.services.api_platform.validate_public_webhook_host",
            lambda _host: None,
        )
        endpoint, secret = create_webhook_endpoint(
            tenant_id=tenant_id,
            name="test sink",
            url="https://webhook.example/hook",
            events=["booking.created"],
        )
        enqueue_webhook_event(
            tenant_id=tenant_id,
            event_type="booking.created",
            data={"booking_id": 7},
        )

        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeOpener:
            def open(self, request, timeout):
                captured["body"] = request.data
                captured["headers"] = dict(request.header_items())
                captured["timeout"] = timeout
                return FakeResponse()

        monkeypatch.setattr(
            "app.services.api_platform.urllib.request.build_opener",
            lambda *_handlers: FakeOpener(),
        )
        monkeypatch.setattr(
            "app.services.api_platform.validate_public_webhook_host",
            lambda _host: None,
        )
        result = deliver_pending_webhooks()
        assert result == {"delivered": 1, "retrying": 0, "failed": 0}
        expected_signature = hmac.new(
            secret.encode(),
            captured["body"],
            hashlib.sha256,
        ).hexdigest()
        headers = {name.casefold(): value for name, value in captured["headers"].items()}
        assert headers["x-parkpilot-signature"] == f"sha256={expected_signature}"
        assert captured["timeout"] == app.config["WEBHOOK_TIMEOUT_SECONDS"]
        delivery = db.session.execute(
            select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
        ).scalar_one()
        assert delivery.status == "delivered"
        assert delivery.attempts == 1
        assert delivery.payload["id"] == delivery.event_id
        assert webhook_signing_secret(endpoint) == secret


def test_webhook_url_validation_blocks_insecure_and_private_targets(monkeypatch):
    from app.services.api_platform import ApiPlatformError, validate_public_webhook_host

    with pytest.raises(ApiPlatformError):
        create_webhook_endpoint(
            tenant_id=1,
            name="bad",
            url="http://127.0.0.1/hook",
            events=["booking.created"],
        )

    monkeypatch.setattr(
        "app.services.api_platform.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(ApiPlatformError):
        validate_public_webhook_host("internal.example")
