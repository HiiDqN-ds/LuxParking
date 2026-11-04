"""Printable key tag: a small label with the QR code and the important facts about the car, to stick on the key.

The QR code is the same signed ticket link that the staff app already scans (no personal data inside it). The printed
facts are limited to what the team needs at the key cabinet: plate, car, space, driver's name, dates, key count and the
state recorded at arrival. Phone numbers and e-mail addresses are never printed.
"""
from datetime import timezone
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import segno
from reportlab.lib.colors import HexColor, white
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas
from sqlalchemy import select

from ..extensions import db
from ..models import Booking, Inspection, Tenant
from ..ui_i18n import ui
from .booking_documents import booking_qr_value

INK, MUTED, GOLD = HexColor("#14212B"), HexColor("#5B6B78"), HexColor("#C9A24B")
TAG_W, TAG_H = 100 * mm, 70 * mm  # fits label sheets and prints on plain paper to cut out


def _safe(text) -> str:
    return str(text or "").encode("cp1252", "replace").decode("cp1252")


def _zone(tenant: Tenant):
    try:
        return ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def arrival_inspection(booking: Booking) -> Inspection | None:
    return db.session.execute(
        select(Inspection).where(Inspection.tenant_id == booking.tenant_id, Inspection.booking_id == booking.id,
                                 Inspection.phase == "arrival", Inspection.status == "completed")
    ).scalars().first()


def tag_facts(booking: Booking, tenant: Tenant, locale: str) -> dict:
    T = lambda text, **v: ui(text, locale=locale, **v)  # noqa: E731
    zone = _zone(tenant)
    local = lambda dt: (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(zone).strftime("%d.%m.%y %H:%M")  # noqa: E731
    vehicle, customer = booking.vehicle, booking.customer
    inspection = arrival_inspection(booking)
    car = " ".join(part for part in (getattr(vehicle, "make", ""), getattr(vehicle, "model", ""), getattr(vehicle, "color", "")) if part)
    rows = [
        (T("Space"), booking.space.name),
        (T("Customer"), f"{customer.first_name} {customer.last_name}".strip()),
        (T("Arrival"), local(booking.pickup_at)),
        (T("Return"), local(booking.return_at)),
    ]
    if inspection is not None:
        rows.append((T("Keys"), str(inspection.key_count if inspection.key_count is not None else "-")))
        rows.append((T("Fuel"), f"{inspection.fuel_level}%" if inspection.fuel_level is not None else "-"))
        rows.append((T("Odometer"), f"{inspection.odometer_km} km" if inspection.odometer_km is not None else "-"))
        damage = len(inspection.damage_marks)
        rows.append((T("Damage"), T("No damage recorded") if not damage else T("%(count)s damage mark(s)", count=damage)))
        if (inspection.belongings or "").strip():
            rows.append((T("Belongings"), inspection.belongings.strip().replace("\n", ", ")))
    else:
        rows.append((T("Inspection"), T("Not checked in yet")))
    extras = [e.name for e in booking.extras if e.status != "cancelled"]
    if extras:
        rows.append((T("Extras"), ", ".join(extras)))
    return {"plate": (vehicle.plate if vehicle else "") or T("No plate"), "car": car, "ref": booking.booking_ref, "rows": rows}


def build_key_tag_pdf(booking: Booking) -> bytes:
    tenant = db.session.get(Tenant, booking.tenant_id)
    locale = booking.customer.locale or tenant.default_locale
    locale = "en" if locale == "ar" else locale  # the built-in PDF fonts have no Arabic glyphs
    facts = tag_facts(booking, tenant, locale)
    try:
        brand = HexColor(tenant.primary_color or "#14385C")
    except (ValueError, TypeError):
        brand = HexColor("#14385C")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(TAG_W, TAG_H))
    c.setTitle(_safe(f"{ui('Key tag', locale=locale)} {facts['ref']}"))
    # header band with logo and tenant name
    c.setFillColor(brand); c.rect(0, TAG_H - 15 * mm, TAG_W, 15 * mm, fill=1, stroke=0)
    c.setFillColor(GOLD); c.rect(0, TAG_H - 15.8 * mm, TAG_W, 0.8 * mm, fill=1, stroke=0)
    x = 4 * mm
    if tenant.logo_data:
        try:
            logo = ImageReader(BytesIO(tenant.logo_data)); iw, ih = logo.getSize(); s = min(11 * mm / iw, 11 * mm / ih)
            c.setFillColor(white); c.roundRect(x, TAG_H - 13.5 * mm, 12 * mm, 12 * mm, 1.5 * mm, fill=1, stroke=0)
            c.drawImage(logo, x + (12 * mm - iw * s) / 2, TAG_H - 13.5 * mm + (12 * mm - ih * s) / 2, width=iw * s, height=ih * s, mask="auto")
            x += 15 * mm
        except Exception:
            pass
    c.setFillColor(white); c.setFont("Helvetica-Bold", 11); c.drawString(x, TAG_H - 8 * mm, _safe(tenant.name)[:34])
    c.setFont("Helvetica", 7.5); c.drawString(x, TAG_H - 12 * mm, _safe(ui("Key tag", locale=locale)))
    c.setFont("Courier-Bold", 10); c.drawRightString(TAG_W - 4 * mm, TAG_H - 8.5 * mm, _safe(facts["ref"]))

    # plate and car
    y = TAG_H - 23.5 * mm
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 24); c.drawString(4 * mm, y, _safe(facts["plate"])[:14])
    if facts["car"]:
        c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(4 * mm, y - 5 * mm, _safe(facts["car"])[:40])

    # QR on the right
    value = booking_qr_value(booking)
    matrix = segno.make(value, error="m").matrix
    border, box = 2, 27 * mm
    cell = box / (len(matrix) + 2 * border)
    qx, qy = TAG_W - 4 * mm - box, TAG_H - 15.8 * mm - box - 3 * mm
    c.setFillColor(white); c.setStrokeColor(HexColor("#D9E0E6")); c.rect(qx, qy, box, box, fill=1, stroke=1)
    c.setFillColor(INK)
    for r, row in enumerate(matrix):
        for k, on in enumerate(row):
            if on:
                c.rect(qx + (k + border) * cell, qy + box - (r + border + 1) * cell, cell + 0.15, cell + 0.15, fill=1, stroke=0)

    # facts (left column, narrower than the QR column)
    ry = y - 9 * mm
    for label, value_text in facts["rows"]:
        if ry < 3 * mm:
            break
        c.setFillColor(MUTED); c.setFont("Helvetica", 6); c.drawString(4 * mm, ry, _safe(label).upper()[:16])
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 7.5)
        text = simpleSplit(_safe(value_text), "Helvetica-Bold", 7.5, 38 * mm)
        c.drawString(29 * mm, ry, text[0] if text else "")
        ry -= 3.7 * mm
    c.showPage(); c.save()
    return buf.getvalue()
