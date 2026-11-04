"""Short, signed customer links for booking confirmation and cancellation."""

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


CUSTOMER_LINK_MAX_AGE_SECONDS = 60 * 60 * 24 * 365


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="parkpilot-customer-booking",
    )


def create_customer_booking_token(*, tenant_id: int, booking_ref: str) -> str:
    return _serializer().dumps({"tenant_id": tenant_id, "booking_ref": booking_ref})


def read_customer_booking_token(token: str) -> tuple[int, str] | None:
    try:
        payload = _serializer().loads(token, max_age=CUSTOMER_LINK_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("tenant_id"), int)
        or not isinstance(payload.get("booking_ref"), str)
        or not payload["booking_ref"]
    ):
        return None
    return payload["tenant_id"], payload["booking_ref"]
