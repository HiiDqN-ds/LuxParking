"""API credential issuance, webhook signing, and durable webhook delivery."""
import hashlib
import http.client
import os
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import ApiKey, Booking, Lot, Tenant, WebhookDelivery, WebhookEndpoint, utcnow

WEBHOOK_EVENTS = frozenset(
    {
        "booking.created",
        "booking.paid",
        "booking.cancelled",
        "booking.checked_in",
        "booking.checked_out",
        "inspection.completed",
        "dispute.opened",
    }
)
PUBLIC_SCOPES = frozenset(
    {
        "map:read",
        "availability:read",
        "products:read",
        "quotes:write",
        "holds:write",
        "bookings:write",
    }
)
SECRET_SCOPES = PUBLIC_SCOPES | frozenset(
    {
        "bookings:read",
        "inspections:read",
        "inspections:write",
        "keys:manage",
        "webhooks:manage",
        "usage:read",
    }
)


# Bundles are the only way keys are issued, and only the platform admin can issue them.
# They never include keys:manage or webhooks:manage, so a key cannot create more keys.
API_BUNDLES = {
    "widget": {"title": "Booking Widget", "label": "Booking widget (public key)", "key_type": "pk", "requests_per_minute": 120,
               "scopes": sorted(PUBLIC_SCOPES),
               "text": "Show the live map and take bookings on your own website."},
    "reports": {"title": "Reports", "label": "Read-only reports (secret key)", "key_type": "sk", "requests_per_minute": 60,
                "scopes": ["bookings:read", "inspections:read", "usage:read"],
                "text": "Read bookings and inspection results for accounting or dashboards."},
    "integration": {"title": "Full Integration", "label": "Full integration (secret key)", "key_type": "sk", "requests_per_minute": 120,
                    "scopes": sorted(SECRET_SCOPES - {"keys:manage", "webhooks:manage"}),
                    "text": "Create and read bookings, holds and inspections from your own system."},
}


class ApiPlatformError(Exception):
    """Expected API platform operation failure."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_api_key(
    *,
    tenant_id: int,
    name: str,
    key_type: str,
    scopes: list[str],
    allowed_origins: list[str],
    requests_per_minute: int,
    allowed_lot_ids: list[int] | None = None,
    monthly_price=0,
    price_breakdown: list | None = None,
) -> tuple[ApiKey, str]:
    normalized_type = key_type.strip().lower()
    allowed_scopes = PUBLIC_SCOPES if normalized_type == "pk" else SECRET_SCOPES
    normalized_scopes = sorted(set(scopes))
    if normalized_type not in {"pk", "sk"}:
        raise ApiPlatformError("key_type must be pk or sk.")
    if not name.strip() or len(name.strip()) > 100:
        raise ApiPlatformError("A key name of 1 to 100 characters is required.")
    if not normalized_scopes or any(scope not in allowed_scopes for scope in normalized_scopes):
        raise ApiPlatformError("One or more scopes are not allowed for this key type.")
    if isinstance(requests_per_minute, bool) or not 1 <= requests_per_minute <= 10000:
        raise ApiPlatformError("requests_per_minute must be between 1 and 10000.")
    origins = sorted(set(origin.rstrip("/") for origin in allowed_origins))
    if any(
        not origin.startswith("https://")
        or len(origin) > 255
        or urlsplit(origin).netloc == ""
        or urlsplit(origin).path not in ("", "/")
        for origin in origins
    ):
        raise ApiPlatformError("Allowed origins must be HTTPS origins without a path.")

    for _ in range(3):
        prefix = f"{normalized_type}_{secrets.token_hex(6)}"
        secret = secrets.token_urlsafe(32)
        token = f"{prefix}.{secret}"
        key = ApiKey(
            tenant_id=tenant_id,
            name=name.strip(),
            prefix=prefix,
            token_hash=_hash(token),
            key_type=normalized_type,
            scopes=normalized_scopes,
            allowed_origins=origins,
            requests_per_minute=requests_per_minute,
            allowed_lot_ids=sorted(set(allowed_lot_ids)) if allowed_lot_ids else None,
            monthly_price=monthly_price,
            price_breakdown=price_breakdown,
        )
        db.session.add(key)
        try:
            db.session.commit()
            return key, token
        except IntegrityError:
            db.session.rollback()
    raise ApiPlatformError("Could not generate a unique API key. Please try again.")




# --- partner access: features, maps (lots) and automatic monthly price -------------------------

FEATURES = {
    "map": {"label": "Live map and availability", "scopes": ["map:read", "availability:read", "products:read"], "price": 30, "requires": []},
    "quotes": {"label": "Price quotes", "scopes": ["quotes:write"], "price": 10, "requires": ["map"]},
    "holds": {"label": "10-minute space holds", "scopes": ["holds:write"], "price": 15, "requires": ["map"]},
    "bookings_write": {"label": "Create and manage bookings", "scopes": ["bookings:write"], "price": 60, "requires": ["holds", "quotes"]},
    "bookings_read": {"label": "Read bookings", "scopes": ["bookings:read"], "price": 25, "requires": []},
    "inspections_read": {"label": "Read vehicle inspections", "scopes": ["inspections:read"], "price": 20, "requires": ["bookings_read"]},
    "inspections_write": {"label": "Run vehicle inspections", "scopes": ["inspections:write"], "price": 40, "requires": ["bookings_read"]},
    "usage": {"label": "Usage statistics", "scopes": ["usage:read"], "price": 5, "requires": []},
}
BASE_PRICE = 19   # EUR per month for every key
LOT_PRICE = 15    # EUR per month for every map (lot) the key may use
WRITE_FEATURES = {"holds", "bookings_write", "inspections_write"}
PRESETS = {
    "reports": ["bookings_read", "inspections_read", "usage"],
    "widget": ["map", "quotes", "holds", "bookings_write"],
    "integration": list(FEATURES),
}
BOOKING_EVENTS = ["booking.created", "booking.paid", "booking.cancelled", "booking.checked_in", "booking.checked_out"]
INSPECTION_EVENTS = ["inspection.completed", "dispute.opened"]


def _setting(name: str, default) -> Decimal:
    """Prices can be changed with API_PRICE_BASE, API_PRICE_LOT and API_PRICE_FEATURE_<CODE> (EUR/month)."""
    raw = os.environ.get(f"API_PRICE_{name}")
    try:
        return Decimal(str(raw)) if raw not in (None, "") else Decimal(str(default))
    except InvalidOperation:
        return Decimal(str(default))


def feature_price(code: str) -> Decimal:
    return _setting(f"FEATURE_{code.upper()}", FEATURES[code]["price"])


def expand_features(codes) -> tuple[list[str], list[str]]:
    """Add the features a chosen feature depends on. Returns (ordered codes, automatically added codes)."""
    chosen = set(codes or ())
    unknown = chosen - set(FEATURES)
    if unknown:
        raise ApiPlatformError("Unknown feature: " + ", ".join(sorted(unknown)) + ".")
    if not chosen:
        raise ApiPlatformError("Choose at least one feature.")
    result = set(chosen)
    queue = list(chosen)
    while queue:
        for needed in FEATURES[queue.pop()]["requires"]:
            if needed not in result:
                result.add(needed)
                queue.append(needed)
    ordered = [code for code in FEATURES if code in result]
    return ordered, [code for code in ordered if code not in chosen]


def feature_scopes(codes) -> list[str]:
    return sorted({scope for code in codes for scope in FEATURES[code]["scopes"]})


def features_from_scopes(scopes) -> list[str]:
    have = set(scopes or ())
    if "*" in have:
        return list(FEATURES)
    return [code for code, spec in FEATURES.items() if set(spec["scopes"]) <= have]


def requests_per_minute_for(codes) -> int:
    return 120 if WRITE_FEATURES & set(codes) else 60


def webhook_events_for(codes) -> list[str]:
    codes = set(codes)
    events = []
    if codes & {"bookings_read", "bookings_write"}:
        events += BOOKING_EVENTS
    if codes & {"inspections_read", "inspections_write"}:
        events += INSPECTION_EVENTS
    return events


def calculate_price(codes, lot_count: int) -> dict:
    """Monthly price in EUR: base + every feature + every map. Returns the total and the line items."""
    cent = Decimal("0.01")
    lines = [{"label": "Base", "amount": f"{_setting('BASE', BASE_PRICE).quantize(cent)}"}]
    total = _setting("BASE", BASE_PRICE)
    for code in codes:
        amount = feature_price(code)
        lines.append({"label": FEATURES[code]["label"], "amount": f"{amount.quantize(cent)}"})
        total += amount
    maps_price = _setting("LOT", LOT_PRICE) * max(int(lot_count), 0)
    lines.append({"label": f"Maps x {max(int(lot_count), 0)}", "amount": f"{maps_price.quantize(cent)}"})
    total += maps_price
    return {"total": total.quantize(cent), "lines": lines}


def pricing_config() -> dict:
    return {
        "base": str(_setting("BASE", BASE_PRICE)),
        "lot": str(_setting("LOT", LOT_PRICE)),
        "features": {code: {"label": spec["label"], "price": str(feature_price(code)), "requires": spec["requires"]}
                     for code, spec in FEATURES.items()},
        "presets": PRESETS,
    }


def describe_key(key: ApiKey) -> dict:
    query = select(Lot).where(Lot.tenant_id == key.tenant_id, Lot.active.is_(True))
    if key.allowed_lot_ids:
        query = query.where(Lot.id.in_(key.allowed_lot_ids))
    lots = db.session.execute(query.order_by(Lot.id)).scalars().all()
    codes = features_from_scopes(key.scopes)
    return {
        "features": [FEATURES[code]["label"] for code in codes],
        "lots": [lot.name for lot in lots],
        "price": key.monthly_price or Decimal("0.00"),
        "lines": key.price_breakdown or [],
        "rpm": key.requests_per_minute,
        "origins": list(key.allowed_origins or []),
        "key_type": key.key_type,
        "webhook": None,
    }


def issue_partner_access(
    *, tenant_id: int, name: str, features, lot_ids, allowed_origins, webhook_url: str = "",
) -> tuple[ApiKey, str, dict]:
    """One step: a key limited to chosen maps and features, a calculated price and an optional webhook."""
    codes, added = expand_features(features)
    wanted = {int(lot_id) for lot_id in (lot_ids or [])}
    lots = db.session.execute(
        select(Lot).where(Lot.tenant_id == tenant_id, Lot.active.is_(True), Lot.id.in_(wanted or {0})).order_by(Lot.id)
    ).scalars().all()
    if not lots or len(lots) != len(wanted):
        raise ApiPlatformError("Choose at least one map (lot) that belongs to this partner.")
    scopes = feature_scopes(codes)
    origins = [origin.strip() for origin in (allowed_origins or []) if origin.strip()]
    key_type = "pk" if origins else "sk"
    if key_type == "pk" and any(scope not in PUBLIC_SCOPES for scope in scopes):
        raise ApiPlatformError(
            "A website-restricted key can only use map, quotes, holds and booking creation. "
            "Remove the website to create a server key, or untick the private features."
        )
    webhook_url = (webhook_url or "").strip()
    events = []
    if webhook_url:
        events = webhook_events_for(codes)
        if not events:
            raise ApiPlatformError("A webhook needs the booking or inspection features.")
        _validate_webhook_fields(name=f"{name or 'Partner'} webhook"[:100], url=webhook_url, events=events)
    price = calculate_price(codes, len(lots))
    key, token = issue_api_key(
        tenant_id=tenant_id, name=(name or "Partner key")[:100], key_type=key_type, scopes=scopes,
        allowed_origins=origins, requests_per_minute=requests_per_minute_for(codes),
        allowed_lot_ids=[lot.id for lot in lots], monthly_price=price["total"], price_breakdown=price["lines"],
    )
    info = describe_key(key)
    info["added"] = [FEATURES[code]["label"] for code in added]
    if webhook_url:
        endpoint, secret = create_webhook_endpoint(
            tenant_id=tenant_id, name=f"{key.name} webhook"[:100], url=webhook_url, events=events,
            lot_ids=[lot.id for lot in lots],
        )
        info["webhook"] = {"url": endpoint.url, "events": events, "secret": secret}
    return key, token, info


def _validate_webhook_fields(*, name: str, url: str, events: list[str]) -> tuple[str, str, list[str]]:
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(url) > 2000
    ):
        raise ApiPlatformError("Webhook URL must be an HTTPS URL without credentials or a fragment.")
    validate_public_webhook_host(parsed.hostname)
    if parsed.port not in (None, 443):
        raise ApiPlatformError("Webhook URL must use the standard HTTPS port.")
    normalized_events = sorted(set(events))
    if not normalized_events or any(event not in WEBHOOK_EVENTS for event in normalized_events):
        raise ApiPlatformError("One or more webhook event names are not supported.")
    if not name.strip() or len(name.strip()) > 100:
        raise ApiPlatformError("A webhook name of 1 to 100 characters is required.")
    return name.strip(), url.strip(), normalized_events


def create_webhook_endpoint(
    *,
    tenant_id: int,
    name: str,
    url: str,
    events: list[str],
    lot_ids: list[int] | None = None,
) -> tuple[WebhookEndpoint, str]:
    name, url, normalized_events = _validate_webhook_fields(name=name, url=url, events=events)
    endpoint = WebhookEndpoint(
        tenant_id=tenant_id,
        name=name,
        url=url,
        events=normalized_events,
        signing_salt=secrets.token_hex(32),
        lot_ids=sorted(set(lot_ids)) if lot_ids else None,
    )
    db.session.add(endpoint)
    db.session.commit()
    return endpoint, webhook_signing_secret(endpoint)


def resolve_public_webhook_host(hostname: str) -> list[str]:
    """Resolve `hostname` and return its addresses, refusing the whole host if ANY address is not public."""
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ApiPlatformError("Webhook host could not be resolved.") from exc
    try:
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ApiPlatformError("Webhook host must resolve only to public IP addresses.")
    except ValueError as exc:  # e.g. an IPv6 scoped address: never public
        raise ApiPlatformError("Webhook host must resolve only to public IP addresses.") from exc
    return sorted(addresses)


def validate_public_webhook_host(hostname: str) -> None:
    """Reject local and private webhook destinations before any server-side request."""
    resolve_public_webhook_host(hostname)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that resolves the host ONCE, checks the addresses and connects to that exact address.

    Validating a host name and then letting the HTTP client resolve it again leaves a window for DNS rebinding
    (the second lookup returns 127.0.0.1 or a cloud metadata address). Connecting to the validated IP closes it;
    the TLS handshake still uses the original host name for SNI and certificate verification."""

    def connect(self):
        last_error: Exception | None = None
        for address in resolve_public_webhook_host(self.host):
            try:
                sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
            except OSError as exc:
                last_error = exc
                continue
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise ApiPlatformError("Webhook host could not be reached.") from last_error


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def webhook_signing_secret(endpoint: WebhookEndpoint) -> str:
    app_secret = current_app.config["SECRET_KEY"].encode("utf-8")
    salt = ("parkpilot-webhook:" + endpoint.signing_salt).encode("utf-8")
    return hmac.new(app_secret, salt, hashlib.sha256).hexdigest()


def enqueue_webhook_event(
    *,
    tenant_id: int,
    event_type: str,
    data: dict,
    commit: bool = True,
) -> int:
    if event_type not in WEBHOOK_EVENTS:
        raise ApiPlatformError("Unsupported webhook event.")
    endpoints = db.session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.active.is_(True),
        )
    ).scalars().all()
    deliveries = []
    event_lot_id = None
    if any(endpoint.lot_ids for endpoint in endpoints) and data.get("booking_id"):
        event_lot_id = db.session.scalar(
            select(Booking.lot_id).where(Booking.id == data["booking_id"], Booking.tenant_id == tenant_id)
        )
    for endpoint in endpoints:
        if event_type not in endpoint.events:
            continue
        if endpoint.lot_ids and event_lot_id not in endpoint.lot_ids:
            continue
        event_id = secrets.token_hex(16)
        payload = {
            "id": event_id,
            "type": event_type,
            "created_at": utcnow().isoformat(),
            "data": data,
        }
        deliveries.append(WebhookDelivery(
            tenant_id=tenant_id,
            endpoint_id=endpoint.id,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            status="queued",
            next_attempt_at=utcnow(),
        ))
    if deliveries:
        db.session.add_all(deliveries)
        if commit:
            db.session.commit()
    return len(deliveries)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver_pending_webhooks(*, limit: int = 100) -> dict[str, int]:
    now = utcnow()
    deliveries = db.session.execute(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == "queued",
            WebhookDelivery.next_attempt_at <= now,
        )
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(limit)
    ).scalars().all()
    outcome = {"delivered": 0, "retrying": 0, "failed": 0}
    for delivery in deliveries:
        endpoint = db.session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == delivery.endpoint_id,
                WebhookEndpoint.tenant_id == delivery.tenant_id,
            )
        ).scalar_one_or_none()
        if endpoint is None or not endpoint.active:
            delivery.status = "failed"
            delivery.last_error = "Webhook endpoint is missing or inactive."
            outcome["failed"] += 1
            db.session.commit()
            continue

        body = json.dumps(delivery.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        signature = hmac.new(
            webhook_signing_secret(endpoint).encode("ascii"),
            body,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            endpoint.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ParkPilot-Webhooks/1.0",
                "X-ParkPilot-Event": delivery.event_type,
                "X-ParkPilot-Delivery": delivery.event_id,
                "X-ParkPilot-Signature": f"sha256={signature}",
            },
            method="POST",
        )
        delivery.attempts += 1
        try:
            validate_public_webhook_host(urlsplit(endpoint.url).hostname or "")
            with urllib.request.build_opener(_NoRedirect(), _PinnedHTTPSHandler()).open(
                request,
                timeout=current_app.config["WEBHOOK_TIMEOUT_SECONDS"],
            ) as response:
                status = response.status
            delivery.last_status_code = status
            delivery.last_error = ""
            if 200 <= status < 300:
                delivery.status = "delivered"
                delivery.delivered_at = utcnow()
                outcome["delivered"] += 1
            else:
                raise urllib.error.HTTPError(endpoint.url, status, "Webhook endpoint returned a non-2xx status", None, None)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ApiPlatformError) as exc:
            error_status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            delivery.last_status_code = error_status
            delivery.last_error = str(exc)[:500]
            max_attempts = current_app.config["WEBHOOK_MAX_ATTEMPTS"]
            if delivery.attempts >= max_attempts:
                delivery.status = "failed"
                outcome["failed"] += 1
            else:
                delay = min(3600, 30 * (2 ** min(delivery.attempts - 1, 7)))
                delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                outcome["retrying"] += 1
        db.session.commit()
    return outcome


def issue_bundle_key(*, tenant_id: int, bundle: str, name: str = "", allowed_origins=None):
    if bundle not in API_BUNDLES:
        raise ApiPlatformError("Unknown API bundle.")
    spec = API_BUNDLES[bundle]
    lot_ids = list(db.session.execute(
        select(Lot.id).where(Lot.tenant_id == tenant_id, Lot.active.is_(True)).order_by(Lot.id)
    ).scalars())
    price = calculate_price(features_from_scopes(spec["scopes"]), len(lot_ids))
    return issue_api_key(
        tenant_id=tenant_id, name=(name or spec["label"])[:100], key_type=spec["key_type"],
        scopes=list(spec["scopes"]), allowed_origins=list(allowed_origins or []),
        requests_per_minute=spec["requests_per_minute"],
        allowed_lot_ids=lot_ids or None, monthly_price=price["total"], price_breakdown=price["lines"],
    )


def _normalize_origins(allowed_origins) -> list[str]:
    origins = sorted(set(origin.strip().rstrip("/") for origin in allowed_origins if origin.strip()))
    if any(
        not origin.startswith("https://")
        or len(origin) > 255
        or urlsplit(origin).netloc == ""
        or urlsplit(origin).path not in ("", "/")
        for origin in origins
    ):
        raise ApiPlatformError("Allowed origins must be HTTPS origins without a path.")
    return origins


def update_api_key(
    key: ApiKey,
    *,
    name: str,
    scopes: list[str],
    allowed_origins: list[str],
    requests_per_minute: int,
    active: bool,
    allowed_lot_ids=None,
    set_lots: bool = False,
) -> ApiKey:
    """Edit a key's settings. The secret itself never changes; issue a new key to rotate it."""
    allowed_scopes = PUBLIC_SCOPES if key.key_type == "pk" else SECRET_SCOPES
    normalized_scopes = sorted(set(scopes))
    if not name.strip() or len(name.strip()) > 100:
        raise ApiPlatformError("A key name of 1 to 100 characters is required.")
    if not normalized_scopes or any(scope not in allowed_scopes for scope in normalized_scopes):
        raise ApiPlatformError("Choose at least one permission that is allowed for this key type.")
    if isinstance(requests_per_minute, bool) or not 1 <= requests_per_minute <= 10000:
        raise ApiPlatformError("Requests per minute must be between 1 and 10000.")
    key.name = name.strip()
    key.scopes = normalized_scopes
    key.allowed_origins = _normalize_origins(allowed_origins)
    key.requests_per_minute = requests_per_minute
    if set_lots:
        key.allowed_lot_ids = sorted({int(i) for i in allowed_lot_ids}) if allowed_lot_ids else None
    lot_total = len(key.allowed_lot_ids) if key.allowed_lot_ids else db.session.scalar(
        select(func.count(Lot.id)).where(Lot.tenant_id == key.tenant_id, Lot.active.is_(True))
    )
    price = calculate_price(features_from_scopes(normalized_scopes), lot_total or 0)
    key.monthly_price, key.price_breakdown = price["total"], price["lines"]
    if active and not key.active:
        key.active, key.revoked_at = True, None
    elif not active and key.active:
        key.active, key.revoked_at = False, utcnow()
    return key


def update_webhook_endpoint(endpoint: WebhookEndpoint, *, name: str, url: str, events: list[str], active: bool) -> WebhookEndpoint:
    name, url, normalized_events = _validate_webhook_fields(name=name, url=url, events=events)
    endpoint.name, endpoint.url, endpoint.events = name, url, normalized_events
    if active and not endpoint.active:
        endpoint.active, endpoint.revoked_at = True, None
    elif not active and endpoint.active:
        endpoint.active, endpoint.revoked_at = False, utcnow()
    return endpoint
