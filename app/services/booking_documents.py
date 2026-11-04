"""Booking ticket documents: scannable QR (SVG/PNG) and a PDF for download and email."""
import re
from io import BytesIO

import segno
from flask import current_app, has_request_context, request
from PIL import Image, ImageDraw
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from ..i18n import translate

NAVY, GOLD, INK, MUTED = HexColor("#14385C"), HexColor("#C9A24B"), HexColor("#14212B"), HexColor("#5B6B78")


def _lan_ip() -> str | None:
    import socket
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None


def _reachable(base: str) -> str:
    """When testing locally the page is on localhost, which a phone cannot open: use this computer's Wi-Fi address instead."""
    import os
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    if os.environ.get("APP_ENV", "development") != "production" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        ip = _lan_ip()
        if ip:
            return f"{parts.scheme}://{ip}" + (f":{parts.port}" if parts.port else "")
    return base


def booking_qr_value(booking) -> str:
    """Short public link /t/<parking>/<number>. Any phone camera opens the customer's ticket; a logged-in
    staff member who scans it lands on the booking in the staff app."""
    from ..extensions import db
    from ..models import Tenant

    tenant = db.session.get(Tenant, booking.tenant_id)
    base = current_app.config.get("QR_BASE_URL") or current_app.config.get("SITE_URL") or (
        request.host_url.rstrip("/") if has_request_context() else "")
    return f"{_reachable(base)}/t/{tenant.slug}/{booking.booking_ref}"


def qr_svg(value: str) -> str:
    out = BytesIO()
    segno.make(value, error="m").save(out, kind="svg", scale=10, border=4, dark="#14212B", light="#FFFFFF",
                                      xmldecl=False, svgns=True, svgclass=None, lineclass=None)
    svg = out.getvalue().decode()
    # segno emits a fixed width/height without a viewBox, which stops the code scaling inside its tile.
    match = re.search(r'<svg\b[^>]*?\bwidth="(\d+)"[^>]*?\bheight="(\d+)"', svg)
    if match and "viewBox" not in svg[:svg.find(">") + 1]:
        svg = svg.replace("<svg ", f'<svg viewBox="0 0 {match.group(1)} {match.group(2)}" ', 1)
    return svg


def qr_png(value: str, scale: int = 16, border: int = 4) -> bytes:
    matrix = segno.make(value, error="m").matrix
    size = (len(matrix) + 2 * border) * scale
    image = Image.new("L", (size, size), 255)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(matrix):
        for x, cell in enumerate(row):
            if cell:
                px, py = (x + border) * scale, (y + border) * scale
                draw.rectangle([px, py, px + scale - 1, py + scale - 1], fill=20)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _t(locale: str, key: str, **kw) -> str:
    text = translate("en" if locale == "ar" else locale, key, **kw)  # built-in PDF fonts have no Arabic glyphs
    return text.encode("latin-1", "replace").decode("latin-1")


def booking_pdf(booking, *, tenant, locale: str, pickup_local, return_local, qr_value: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    left = 48
    c.setFillColor(NAVY); c.rect(0, height - 120, width, 120, fill=1, stroke=0)
    c.setFillColor(GOLD); c.rect(0, height - 124, width, 4, fill=1, stroke=0)
    c.setFillColor(white); c.setFont("Helvetica-Bold", 22)
    c.drawString(left, height - 62, (tenant.name or "Parking").encode("latin-1", "replace").decode("latin-1"))
    c.setFont("Helvetica", 13); c.drawString(left, height - 88, _t(locale, "checkout.complete_title"))

    y = height - 170
    c.setFillColor(MUTED); c.setFont("Helvetica", 10); c.drawString(left, y, _t(locale, "checkout.reference"))
    c.setFillColor(INK); c.setFont("Courier-Bold", 30); c.drawString(left, y - 32, booking.booking_ref)

    code = segno.make(qr_value, error="m"); matrix = code.matrix
    border, box = 4, 168
    cell = box / (len(matrix) + 2 * border)
    qx, qy = width - left - box, height - 160 - box
    c.setFillColor(white); c.setStrokeColor(HexColor("#D9E0E6")); c.rect(qx - 6, qy - 6, box + 12, box + 12, fill=1, stroke=1)
    c.setFillColor(INK)
    for r, row in enumerate(matrix):
        for k, on in enumerate(row):
            if on:
                c.rect(qx + (k + border) * cell, qy + box - (r + border + 1) * cell, cell + 0.2, cell + 0.2, fill=1, stroke=0)
    c.setFillColor(MUTED); c.setFont("Helvetica", 9)
    for i, line in enumerate(simpleSplit(_t(locale, "ticket.hint"), "Helvetica", 9, box + 12)):
        c.drawString(qx - 6, qy - 22 - i * 11, line)

    rows = [
        (_t(locale, "hold.space"), f"{booking.lot.name} - {booking.space.name}"),
        (_t(locale, "hold.pickup"), pickup_local.strftime("%d.%m.%Y %H:%M")),
        (_t(locale, "hold.return"), return_local.strftime("%d.%m.%Y %H:%M")),
    ]
    if booking.vehicle is not None:
        rows.append((_t(locale, "checkout.plate"), booking.vehicle.plate))
    rows.append((_t(locale, "checkout.heading"), f"{booking.customer.first_name} {booking.customer.last_name}".strip()))
    y = height - 300
    for label, value in rows:
        c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(left, y, label)
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 12); c.drawString(left, y - 15, str(value).encode("latin-1", "replace").decode("latin-1"))
        y -= 42
    extras = [e for e in booking.extras if e.status != "cancelled"]
    if extras:
        c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(left, y, "Extras")
        y -= 15
        c.setFillColor(INK); c.setFont("Helvetica", 11)
        for e in extras:
            text = f"{e.name}" + (f" x {e.quantity}" if (e.quantity or 1) > 1 else "") + (f" ({e.details})" if e.details else "")
            c.drawString(left, y, text.encode("latin-1", "replace").decode("latin-1")[:70])
            c.drawRightString(left + 330, y, f"{e.total():.2f} {booking.currency}")
            y -= 15
        y -= 12
    c.setStrokeColor(GOLD); c.setLineWidth(1.5); c.line(left, y + 6, left + 330, y + 6)
    c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(left, y - 12, _t(locale, "hold.total"))
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 20); c.drawString(left, y - 36, f"{booking.total_amount:.2f} {booking.currency}")
    status = _t(locale, "checkout.status_paid" if booking.payment_status == "paid" else "checkout.status_arrival")
    c.setFont("Helvetica", 10); c.setFillColor(MUTED); c.drawString(left, y - 54, status)

    c.setFillColor(MUTED); c.setFont("Helvetica", 9)
    for i, line in enumerate(simpleSplit(_t(locale, "checkout.policy"), "Helvetica", 9, width - 2 * left)):
        c.drawString(left, 70 - i * 11, line)
    c.showPage(); c.save()
    return buf.getvalue()


def build_pdf_for_booking(booking, locale: str | None = None) -> bytes:
    """Load what the PDF needs (tenant, local times) and render it."""
    from zoneinfo import ZoneInfo

    from ..extensions import db
    from ..models import Tenant

    tenant = db.session.get(Tenant, booking.tenant_id)
    zone = ZoneInfo(tenant.timezone)

    def local(dt):
        from datetime import timezone
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(zone)

    return booking_pdf(booking, tenant=tenant, locale=locale or booking.customer.locale or "en",
                       pickup_local=local(booking.pickup_at), return_local=local(booking.return_at),
                       qr_value=booking_qr_value(booking))
