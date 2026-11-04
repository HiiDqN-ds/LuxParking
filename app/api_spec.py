"""OpenAPI 3.1 description of the ParkPilot partner API.

Served as JSON at /v1/openapi.json and rendered (without any third-party script) at /developers/reference.
tests/test_api_contract.py calls the real endpoints and validates every response against these schemas, so this
description cannot drift away from the behaviour.
"""
from __future__ import annotations

from .services.api_platform import WEBHOOK_EVENTS

API_VERSION = "1.0.0"


def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def obj(props: dict, required: list[str] | None = None, **extra) -> dict:
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    schema.update(extra)
    return schema


def arr(items: dict, **extra) -> dict:
    return {"type": "array", "items": items, **extra}


def nullable(schema: dict) -> dict:
    return {"oneOf": [schema, {"type": "null"}]}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
NUM = {"type": "number"}
DT = {"type": "string", "format": "date-time", "examples": ["2026-12-20T08:00:00+01:00"]}
MONEY = {"type": "string", "pattern": r"^-?\d+\.\d{2}$", "examples": ["19.80"], "description": "Decimal amount as a string with two decimals. Never a float."}

SCHEMAS: dict[str, dict] = {
    "Error": obj(
        {"error": obj({"code": STR, "message": STR, "request_id": STR}, ["code", "message"])}, ["error"],
        description="Every failure uses this shape. `code` is stable and machine readable; `message` is for humans; "
                    "`request_id` identifies the request when you contact support.",
    ),
    "Money": MONEY,
    "SpaceState": obj({
        "id": INT, "name": STR, "x": NUM, "y": NUM, "width": NUM, "height": NUM, "kind": STR,
        "covered": BOOL, "ev_charging": BOOL, "bus_distance_m": nullable(INT), "photo_url": nullable(STR),
        "status": {"type": "string", "enum": ["available", "optional", "soon", "reserved", "blocked"]},
        "available_from": nullable(DT), "price": nullable(ref("Money")), "price_error": nullable(STR),
        "currency": STR, "selectable": BOOL,
    }, ["id", "name", "status", "currency", "selectable"]),
    "LotMap": obj({"lot": obj({"id": INT, "name": STR, "currency": STR}, ["id", "name", "currency"]),
                   "spaces": arr(ref("SpaceState"))}, ["lot", "spaces"]),
    "Availability": obj({"spaces": arr(ref("SpaceState"))}, ["spaces"]),
    "Product": obj({"id": INT, "lot_id": INT, "name": STR, "kind": STR, "covered": BOOL, "ev_charging": BOOL,
                    "daily_rate": nullable(ref("Money")), "currency": STR}, ["id", "lot_id", "name", "currency"]),
    "Extra": obj({
        "id": INT, "kind": STR, "name": STR, "description": STR, "price": ref("Money"), "currency": STR,
        "per_person": BOOL, "included_persons": INT, "extra_person_price": nullable(ref("Money")),
        "max_persons": nullable(INT),
    }, ["id", "kind", "name", "price", "currency"]),
    "ExtraSelection": obj({"id": {**INT, "description": "Extra id from GET /extras."},
                           "quantity": {"type": "integer", "minimum": 1, "default": 1},
                           "details": {"type": "string", "maxLength": 255, "description": "e.g. a flight number."}}, ["id"]),
    "QuoteRequest": obj({
        "lot_id": INT, "space_id": INT, "from": DT, "to": DT,
        "extras": arr(ref("ExtraSelection")), "discount_code": STR,
    }, ["lot_id", "space_id", "from", "to"]),
    "Quote": obj({
        "lot_id": INT, "space_id": INT, "from": DT, "to": DT, "currency": STR,
        "subtotal": ref("Money"), "discount": ref("Money"), "extras_total": ref("Money"), "total": ref("Money"),
        "code": {**STR, "description": "Present only when a discount code was applied."},
    }, ["lot_id", "space_id", "from", "to", "currency", "subtotal", "discount", "extras_total", "total"]),
    "HoldRequest": obj({"lot_id": INT, "space_id": INT, "from": DT, "to": DT,
                        "email": {"type": "string", "format": "email"}}, ["lot_id", "space_id", "from", "to", "email"]),
    "Hold": obj({"id": INT, "space_id": INT, "expires_at": DT, "status": {"type": "string", "enum": ["active", "converted", "expired", "released"]}},
                ["id", "space_id", "expires_at", "status"],
                description="A temporary reservation of one space. It expires, so create the booking promptly."),
    "BookingRequest": obj({
        "hold_id": INT, "first_name": STR, "last_name": STR, "email": {"type": "string", "format": "email"}, "phone": STR,
        "vehicle_plate": STR, "vehicle_make": STR, "vehicle_model": STR, "notes": {"type": "string", "maxLength": 10000},
        "extras": arr(ref("ExtraSelection")), "discount_code": STR,
        "send_notifications": {**BOOL, "default": True, "description": "Set false to send your own customer emails."},
        "test_payment": {**BOOL, "description": "Marks the booking paid without a real payment. Refused in production."},
    }, ["hold_id", "first_name", "email", "phone"]),
    "BookingExtra": obj({"name": STR, "kind": STR, "quantity": INT, "total": ref("Money")}, ["name", "kind", "quantity", "total"]),
    "Booking": obj({
        "id": INT, "booking_ref": STR, "lot_id": INT, "space_id": INT, "vehicle_id": nullable(INT),
        "status": {"type": "string", "enum": ["reserved", "checked_in", "checked_out", "cancelled"]},
        "payment_status": STR, "pickup_at": DT, "return_at": DT, "total_amount": ref("Money"), "currency": STR,
        "created_at": DT, "extras": arr(ref("BookingExtra")),
    }, ["id", "booking_ref", "lot_id", "space_id", "status", "payment_status", "pickup_at", "return_at", "total_amount", "currency", "created_at", "extras"]),
    "BookingResponse": obj({"booking": ref("Booking")}, ["booking"]),
    "BookingList": obj({"bookings": arr(ref("Booking"))}, ["bookings"]),
    "NotesPatch": obj({"notes": {"type": "string", "maxLength": 10000}}, ["notes"], additionalProperties=False),
    "Inspection": obj({"id": INT, "booking_id": INT, "phase": {"type": "string", "enum": ["arrival", "departure"]}, "status": STR,
                       "staff_user_id": INT, "photo_categories": arr(STR), "completed_at": nullable(DT)},
                      ["id", "booking_id", "phase", "status"]),
    "InspectionCreate": obj({"phase": {"type": "string", "enum": ["arrival", "departure"]}, "staff_user_id": INT}, ["phase", "staff_user_id"]),
    "InspectionComplete": obj({
        "operation": {"const": "complete"}, "inspection_id": INT, "staff_user_id": INT, "fuel_level": STR, "odometer_km": INT,
        "key_count": INT, "belongings": STR, "notes": STR, "no_damage_confirmed": BOOL,
    }, ["operation", "inspection_id", "staff_user_id"]),
    "ApiKey": obj({
        "id": INT, "name": STR, "prefix": STR, "type": {"type": "string", "enum": ["pk", "sk"]}, "scopes": arr(STR),
        "allowed_origins": arr(STR), "requests_per_minute": INT, "last_used_at": nullable(DT), "active": BOOL,
    }, ["id", "name", "prefix", "type", "scopes"]),
    "ApiKeyCreated": obj({
        "id": INT, "name": STR, "prefix": STR, "type": STR, "scopes": arr(STR), "allowed_origins": arr(STR),
        "requests_per_minute": INT, "token": {**STR, "description": "Shown once. Store it in a secret manager."}, "warning": STR,
    }, ["id", "name", "prefix", "token"]),
    "Webhook": obj({"id": INT, "name": STR, "url": {"type": "string", "format": "uri"}, "events": arr({"type": "string", "enum": sorted(WEBHOOK_EVENTS)}), "active": BOOL},
                   ["id", "name", "url", "events", "active"]),
    "WebhookCreate": obj({"name": STR, "url": {"type": "string", "format": "uri", "description": "Public HTTPS URL."},
                          "events": arr({"type": "string", "enum": sorted(WEBHOOK_EVENTS)})}, ["name", "url", "events"]),
    "WebhookCreated": obj({"id": INT, "name": STR, "url": STR, "events": arr(STR),
                           "signing_secret": {**STR, "description": "Shown once. Used to verify X-ParkPilot-Signature."}, "warning": STR},
                          ["id", "name", "url", "events", "signing_secret"]),
    "WebhookDelivery": obj({
        "id": {**STR, "description": "Event id (same as the `id` in the delivered payload and X-ParkPilot-Delivery)."},
        "endpoint_id": INT, "event": STR, "status": {"type": "string", "enum": ["pending", "delivered", "failed"]},
        "attempts": INT, "last_status_code": nullable(INT), "last_error": nullable(STR), "next_attempt_at": DT, "delivered_at": nullable(DT),
    }, ["id", "endpoint_id", "event", "status", "attempts"]),
    "UsageRow": obj({"method": STR, "path": STR, "status": INT, "duration_ms": INT, "created_at": DT}, ["method", "path", "status"]),
    "Health": obj({"status": {"const": "ok"}, "api_version": STR, "time": DT}, ["status", "api_version", "time"]),
    "WebhookEvent": obj({
        "id": {**STR, "description": "Unique event id. Use it to ignore duplicates: delivery is at-least-once."},
        "type": {"type": "string", "enum": sorted(WEBHOOK_EVENTS)}, "created_at": DT,
        "data": obj({"booking_id": INT, "booking_ref": STR, "status": STR}, description="Event specific fields. Fetch the booking for the full state."),
    }, ["id", "type", "created_at", "data"]),
}

HEADERS = {
    "RequestId": {"description": "Unique id of this request. Quote it when you contact support.", "schema": STR},
    "RateLimitLimit": {"description": "Requests per minute allowed for this key.", "schema": INT},
    "RateLimitRemaining": {"description": "Requests left in the current minute.", "schema": INT},
}


def _error_response(description: str) -> dict:
    return {"description": description, "content": {"application/json": {"schema": ref("Error")}}}


RESPONSES = {
    "BadRequest": _error_response("`invalid_request`, `invalid_discount`, `invalid_idempotency_key`: the request is malformed or a value is not allowed."),
    "Unauthorized": _error_response("`authentication_required` or `invalid_api_key`: send `Authorization: Bearer <key>`."),
    "PaymentRequired": _error_response("`plan_limit_reached`: the plan's monthly API or booking allowance is used up."),
    "Forbidden": _error_response("`forbidden` (missing scope or origin not allowed) or `tenant_suspended`."),
    "NotFound": _error_response("`not_found`: the object does not exist or this key may not see it."),
    "Conflict": _error_response("`conflict`, `pricing_unavailable` or `invalid_state`: the action is not possible in the current state, e.g. the space was just taken."),
    "RateLimited": {**_error_response("`rate_limit_exceeded`: wait for `Retry-After` seconds."),
                    "headers": {"Retry-After": {"schema": INT, "description": "Seconds to wait."}}},
}

_COMMON_ERRORS = {"400": "BadRequest", "401": "Unauthorized", "402": "PaymentRequired", "403": "Forbidden", "429": "RateLimited"}


def _json_body(schema_name: str, example: dict | None = None) -> dict:
    media = {"schema": ref(schema_name)}
    if example is not None:
        media["example"] = example
    return {"required": True, "content": {"application/json": media}}


def _ok(schema_name: str | None, description: str = "Success", status: int = 200, example: dict | None = None,
        extra_errors: tuple[str, ...] = ("404",)) -> dict:
    responses = {}
    if schema_name:
        media = {"schema": ref(schema_name)}
        if example is not None:
            media["example"] = example
        responses[str(status)] = {"description": description, "headers": _std_headers(), "content": {"application/json": media}}
    else:
        responses[str(status)] = {"description": description, "headers": _std_headers()}
    codes = dict(_COMMON_ERRORS)
    codes.update({"404": "NotFound", "409": "Conflict"})
    for code in (*_COMMON_ERRORS.keys(), *extra_errors):
        responses[code] = {"$ref": f"#/components/responses/{codes[code]}"}
    return responses


def _std_headers() -> dict:
    return {"X-Request-Id": {"$ref": "#/components/headers/RequestId"},
            "X-RateLimit-Limit": {"$ref": "#/components/headers/RateLimitLimit"},
            "X-RateLimit-Remaining": {"$ref": "#/components/headers/RateLimitRemaining"}}


def _op(summary: str, tag: str, operation_id: str, scope: str, responses: dict, description: str = "",
        parameters: list | None = None, body: dict | None = None, idempotent: bool = False) -> dict:
    op = {"summary": summary, "tags": [tag], "operationId": operation_id, "x-required-scope": scope, "responses": responses}
    if description:
        op["description"] = description
    params = list(parameters or [])
    if idempotent:
        params.append({"$ref": "#/components/parameters/IdempotencyKey"})
    if params:
        op["parameters"] = params
    if body:
        op["requestBody"] = body
    return op


def _path(name: str, description: str = "") -> dict:
    return {"name": name, "in": "path", "required": True, "schema": INT, "description": description}


def _query(name: str, description: str, schema: dict, required: bool = True) -> dict:
    return {"name": name, "in": "query", "required": required, "schema": schema, "description": description}


FROM_TO = [
    _query("from", "Start of the stay (ISO 8601 with time zone offset).", DT),
    _query("to", "End of the stay (ISO 8601 with time zone offset), after `from`.", DT),
]
LOT_ID = _query("lot_id", "Parking lot id from GET /products.", INT)
BOOKING_ID = _path("booking_id")

_QUOTE_EXAMPLE = {"lot_id": 1, "space_id": 12, "from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00",
                  "extras": [{"id": 3, "quantity": 2}], "discount_code": "WINTER10"}

PATHS: dict[str, dict] = {
    "/health": {"get": {**_op("Service health", "Platform", "getHealth", "none (no key needed)", {
        "200": {"description": "The API is up.", "content": {"application/json": {"schema": ref("Health"), "example": {"status": "ok", "api_version": "1", "time": "2026-12-01T10:00:00+00:00"}}}}},
        "Use it for uptime monitoring. It needs no API key and does not count against rate limits."), "security": []}},
    "/products": {"get": _op("List bookable spaces", "Catalogue", "listProducts", "products:read", _ok("Products", extra_errors=()),
                             "All active lots of your account and their bookable spaces with the daily rate. Start here to learn the ids.")},
    "/extras": {"get": _op("List extras of a lot", "Catalogue", "listExtras", "products:read", _ok("Extras"),
                           "Shuttle, cleaning and other add-ons you can send with a quote or booking.", [LOT_ID])},
    "/availability": {"get": _op("Availability and price per space", "Availability", "getAvailability", "availability:read", _ok("Availability"),
                                 "State and price of every space for the stay. `selectable` is true when the space can be booked.", [LOT_ID, *FROM_TO])},
    "/lots/{lot_id}/map": {"get": _op("Lot map with live availability", "Availability", "getLotMap", "map:read", _ok("LotMap"),
                                      "Same as availability plus the lot and the x/y/width/height of each space for drawing a map.", [_path("lot_id"), *FROM_TO])},
    "/quotes": {"post": _op("Price a stay", "Booking flow", "createQuote", "quotes:write", _ok("QuoteEnvelope", extra_errors=("404", "409")),
                            "Calculates subtotal, discount, extras and total. Nothing is reserved. Totals are authoritative: the booking charges the same amount.",
                            body=_json_body("QuoteRequest", _QUOTE_EXAMPLE))},
    "/holds": {"post": _op("Hold a space", "Booking flow", "createHold", "holds:write", _ok("HoldEnvelope", "Hold created", 201, extra_errors=("404", "409")),
                           "Reserves the space for a short time so nobody else books it while the customer enters details.",
                           body=_json_body("HoldRequest", {"lot_id": 1, "space_id": 12, "from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00", "email": "kunde@example.de"}),
                           idempotent=True)},
    "/bookings": {
        "post": _op("Create a booking from a hold", "Booking flow", "createBooking", "bookings:write", _ok("BookingResponse", "Booking created", 201, extra_errors=("404", "409")),
                    "Turns a hold into a booking and sends the confirmation email (unless `send_notifications` is false).",
                    body=_json_body("BookingRequest", {"hold_id": 41, "first_name": "Anna", "last_name": "Schmidt", "email": "anna@example.de", "phone": "+491511234567",
                                                      "vehicle_plate": "D-AB 1234", "extras": [{"id": 3, "quantity": 2, "details": "LH 123"}]}), idempotent=True),
        "get": _op("List bookings", "Bookings", "listBookings", "bookings:read", _ok("BookingList", extra_errors=()),
                   "Newest first.", [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}]),
    },
    "/bookings/{booking_id}": {
        "get": _op("Get a booking", "Bookings", "getBooking", "bookings:read", _ok("BookingResponse"), parameters=[BOOKING_ID]),
        "patch": _op("Update the notes of a booking", "Bookings", "updateBooking", "bookings:write", _ok("BookingResponse", extra_errors=("404", "409")),
                     "Only `notes`, and only while the booking is `reserved`.", [BOOKING_ID], _json_body("NotesPatch", {"notes": "Kindersitz im Kofferraum"}), idempotent=True),
        "delete": _op("Cancel a booking", "Bookings", "cancelBooking", "bookings:write", _ok("BookingResponse", extra_errors=("404", "409")),
                      "Cancels the booking and issues a credit note when it was paid.", [BOOKING_ID], idempotent=True),
    },
    "/bookings/{booking_id}/invoice": {"get": _op("Invoice PDF", "Bookings", "getInvoice", "bookings:read",
                                                  {"200": {"description": "The invoice as PDF (German VAT invoice with gapless number).", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                                                   **{c: {"$ref": f"#/components/responses/{n}"} for c, n in (("401", "Unauthorized"), ("403", "Forbidden"), ("404", "NotFound"), ("429", "RateLimited"))}},
                                                  "Issued on first request when it does not exist yet.", [BOOKING_ID])},
    "/bookings/{booking_id}/credit-note": {"get": _op("Credit note PDF", "Bookings", "getCreditNote", "bookings:read",
                                                      {"200": {"description": "The credit note (Gutschrift) as PDF.", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                                                       **{c: {"$ref": f"#/components/responses/{n}"} for c, n in (("401", "Unauthorized"), ("403", "Forbidden"), ("404", "NotFound"), ("429", "RateLimited"))}},
                                                      "404 until the booking was cancelled after payment.", [BOOKING_ID])},
    "/bookings/{booking_id}/check-in": {"post": _op("Check a vehicle in", "Operations", "checkIn", "bookings:write", _ok("BookingResponse", extra_errors=("404", "409")), parameters=[BOOKING_ID], idempotent=True)},
    "/bookings/{booking_id}/check-out": {"post": _op("Check a vehicle out", "Operations", "checkOut", "bookings:write", _ok("BookingResponse", extra_errors=("404", "409")), parameters=[BOOKING_ID], idempotent=True)},
    "/bookings/{booking_id}/inspections": {
        "get": _op("List inspections of a booking", "Operations", "listInspections", "inspections:read", _ok("InspectionList"), parameters=[BOOKING_ID]),
        "post": _op("Start or complete an inspection", "Operations", "writeInspection", "inspections:write", _ok("InspectionEnvelope", "Inspection created or completed", 201, extra_errors=("404", "409")),
                    "Without `operation` it starts an inspection (`phase`, `staff_user_id`). With `operation: complete` it completes the given inspection (status 200).",
                    [BOOKING_ID], {"required": True, "content": {"application/json": {"schema": {"oneOf": [ref("InspectionCreate"), ref("InspectionComplete")]}}}}, idempotent=True),
    },
    "/inspections/{inspection_id}/photos": {"post": _op("Upload an inspection photo", "Operations", "uploadInspectionPhoto", "inspections:write",
                                                         _ok("PhotoEnvelope", "Photo stored", 201, extra_errors=("404", "409")),
                                                         "Multipart form: `photo` (file), `category`, `captured_at` (ISO 8601), `staff_user_id`.", [_path("inspection_id")],
                                                         {"required": True, "content": {"multipart/form-data": {"schema": obj({"photo": {"type": "string", "format": "binary"}, "category": STR, "captured_at": DT, "staff_user_id": INT}, ["photo", "category", "captured_at", "staff_user_id"])}}})},
    "/webhooks": {
        "get": _op("List webhook endpoints", "Webhooks", "listWebhooks", "webhooks:manage", _ok("WebhookList", extra_errors=())),
        "post": _op("Create a webhook endpoint", "Webhooks", "createWebhook", "webhooks:manage", _ok("WebhookCreatedEnvelope", "Created", 201, extra_errors=("404",)),
                    "The `signing_secret` is returned once.", body=_json_body("WebhookCreate", {"name": "CRM", "url": "https://example.de/hooks/parkpilot", "events": ["booking.created", "booking.paid"]})),
    },
    "/webhooks/{endpoint_id}": {"delete": _op("Delete a webhook endpoint", "Webhooks", "deleteWebhook", "webhooks:manage", _ok("Revoked"), parameters=[_path("endpoint_id")])},
    "/webhooks/{endpoint_id}/rotate-secret": {"post": _op("Rotate the signing secret", "Webhooks", "rotateWebhookSecret", "webhooks:manage", _ok("RotatedSecret"),
                                                          "Returns the new secret once. Update your receiver first, then retry failed deliveries.", [_path("endpoint_id")])},
    "/webhook-deliveries": {"get": _op("Delivery log", "Webhooks", "listWebhookDeliveries", "webhooks:manage", _ok("DeliveryList", extra_errors=()),
                                       "Attempts, status codes and errors of recent deliveries.", [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}])},
    "/api-keys": {
        "get": _op("List API keys", "Keys and usage", "listApiKeys", "keys:manage", _ok("ApiKeyList", extra_errors=())),
        "post": _op("Create an API key", "Keys and usage", "createApiKey", "keys:manage", _ok("ApiKeyCreatedEnvelope", "Created", 201, extra_errors=()),
                    "The token is returned once.", body={"required": True, "content": {"application/json": {"schema": obj({"name": STR, "type": {"enum": ["pk", "sk"]}, "scopes": arr(STR), "allowed_origins": arr(STR), "requests_per_minute": INT}, ["name", "type", "scopes"])}}}),
    },
    "/api-keys/{key_id}": {"delete": _op("Revoke an API key", "Keys and usage", "revokeApiKey", "keys:manage", _ok("Revoked"), parameters=[_path("key_id")])},
    "/api-keys/{key_id}/rotate": {"post": _op("Rotate an API key", "Keys and usage", "rotateApiKey", "keys:manage", _ok("ApiKeyCreatedEnvelope"), "Issues a replacement with the same scopes.", [_path("key_id")])},
    "/usage": {"get": _op("Recent API usage of this key", "Keys and usage", "getUsage", "usage:read", _ok("UsageList", extra_errors=()))},
}

PATHS["/bookings/{booking_id}/inspections"]["post"]["responses"]["200"] = {
    "description": "Inspection completed (when `operation` is `complete`).", "headers": _std_headers(),
    "content": {"application/json": {"schema": ref("InspectionEnvelope")}}}

# response envelopes
SCHEMAS.update({
    "Products": obj({"products": arr(ref("Product"))}, ["products"]),
    "Extras": obj({"extras": arr(ref("Extra"))}, ["extras"]),
    "QuoteEnvelope": obj({"quote": ref("Quote")}, ["quote"]),
    "HoldEnvelope": obj({"hold": ref("Hold")}, ["hold"]),
    "InspectionList": obj({"inspections": arr(ref("Inspection"))}, ["inspections"]),
    "InspectionEnvelope": obj({"inspection": ref("Inspection")}, ["inspection"]),
    "PhotoEnvelope": obj({"photo": obj({"id": INT, "category": STR, "sha256": STR, "size_bytes": INT}, ["id", "category"])}, ["photo"]),
    "WebhookList": obj({"webhooks": arr(ref("Webhook")), "supported_events": arr(STR)}, ["webhooks", "supported_events"]),
    "WebhookCreatedEnvelope": obj({"webhook": ref("WebhookCreated")}, ["webhook"]),
    "DeliveryList": obj({"deliveries": arr(ref("WebhookDelivery"))}, ["deliveries"]),
    "ApiKeyList": obj({"api_keys": arr(ref("ApiKey"))}, ["api_keys"]),
    "ApiKeyCreatedEnvelope": obj({"api_key": ref("ApiKeyCreated")}, ["api_key"]),
    "Revoked": obj({"revoked": BOOL, "id": INT, "prefix": STR}, ["revoked"]),
    "RotatedSecret": obj({"id": INT, "signing_secret": STR, "warning": STR}, ["signing_secret"]),
    "UsageList": obj({"usage": arr(ref("UsageRow"))}, ["usage"]),
})

DESCRIPTION = """\
REST API for airport parking in Germany: availability, prices, holds, bookings, invoices (German VAT invoice with gapless numbering),
vehicle inspections and webhooks.

**Basics**
- Base path `/v1`. JSON in and out, UTF-8. Money is always a string with two decimals; times are ISO 8601 with offset.
- Authenticate with `Authorization: Bearer <key>`. `pk_` keys are public (booking widgets, limited scopes); `sk_` keys are secret (server only).
- Send `Idempotency-Key` (8 to 200 characters) on every POST, PATCH and DELETE you may retry. The first result is replayed for 24 hours.
- Every response carries `X-Request-Id`, `X-RateLimit-Limit` and `X-RateLimit-Remaining`. Over the limit you get `429` with `Retry-After`.
- Errors always look like `{"error": {"code", "message", "request_id"}}`.
- Webhooks are signed with HMAC-SHA256; see the `webhooks` section and /developers/webhooks.
"""


def build_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "ParkPilot API",
            "version": API_VERSION,
            "summary": "Airport parking API for Germany",
            "description": DESCRIPTION,
        },
        "servers": [{"url": "/v1", "description": "This server"}],
        "security": [{"BearerAuth": []}],
        "tags": [{"name": n} for n in ("Platform", "Catalogue", "Availability", "Booking flow", "Bookings", "Operations", "Webhooks", "Keys and usage")],
        "paths": PATHS,
        "webhooks": {
            "event": {"post": {
                "summary": "Event delivered to your webhook URL",
                "description": "Headers: `X-ParkPilot-Event`, `X-ParkPilot-Delivery` (event id), `X-ParkPilot-Signature: sha256=<hex>` "
                               "(HMAC-SHA256 of the raw body with your signing secret). Answer with any 2xx within 5 seconds, otherwise it is "
                               "retried (after 30 s, then 1, 2, 4, 8, 16 and 32 minutes; 8 attempts in total). Delivery is at-least-once: ignore events with an id you have seen.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": ref("WebhookEvent"), "example": {
                    "id": "9f2c1d7a5b8e4c3fa1d0e6b7c8a9f012", "type": "booking.created", "created_at": "2026-12-01T10:00:00+00:00",
                    "data": {"booking_id": 41, "booking_ref": "PP-7K2M9Q", "status": "reserved"}}}}},
                "responses": {"2XX": {"description": "Received."}},
            }}
        },
        "components": {
            "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer", "description": "`pk_...` public key or `sk_...` secret key, created in the admin under API & webhooks."}},
            "parameters": {"IdempotencyKey": {"name": "Idempotency-Key", "in": "header", "required": False, "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                                              "description": "Makes retries safe: the same key returns the first result instead of acting twice."}},
            "headers": HEADERS,
            "responses": RESPONSES,
            "schemas": SCHEMAS,
        },
    }


OPENAPI_SPEC = build_spec()
