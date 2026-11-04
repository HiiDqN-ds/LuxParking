"""Best-effort, privacy-safe Socket.IO notifications for parking availability."""

from flask import current_app

from ..extensions import socketio

BOOKING_NAMESPACE = "/booking"


def lot_room_name(tenant_id: int, lot_id: int) -> str:
    return f"lot:{tenant_id}:{lot_id}"


def notify_lot_availability(tenant_id: int, lot_id: int) -> None:
    try:
        socketio.emit(
            "availability_changed",
            {"lot_id": lot_id},
            namespace=BOOKING_NAMESPACE,
            to=lot_room_name(tenant_id, lot_id),
        )
    except Exception:
        current_app.logger.exception(
            "Could not broadcast availability change for lot %s.", lot_id
        )
