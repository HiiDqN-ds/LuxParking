"""Invoices (Rechnung) and credit notes (Gutschrift) for bookings: numbering, VAT, snapshots and PDF.

How it works
- An invoice is issued the moment a booking is confirmed. While it is still *open* (not paid) it follows the booking:
  extras added, a move or a re-price update the same document, so the customer never gets a stale amount.
- Once the payment arrives the invoice becomes *paid* and is frozen.
- Cancelling an unpaid booking marks its invoice *cancelled*. A refund of a paid booking issues a credit note
  (negative amounts, own number series) and marks the invoice *credited*.
- Everything printed is copied into the invoice row, so editing or deleting the booking never rewrites a document.
- Numbers are gapless per tenant and kind (RE-000001 / GS-000001) and handed out under a row lock on the tenant.

All amounts are the gross prices the customer pays; VAT is split out of them with the tenant's rate.
Mixed VAT rates on one invoice are not supported (one rate per tenant).
"""
import math
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas
from sqlalchemy import select

from ..extensions import db
from ..i18n import translate
from ..models import Invoice, Payment, Tenant, money

NAVY, GOLD, INK, MUTED, RULE = HexColor("#14385C"), HexColor("#C9A24B"), HexColor("#14212B"), HexColor("#5B6B78"), HexColor("#D9E0E6")
GREEN, RED = HexColor("#1B7F4B"), HexColor("#B3261E")
SYMBOLS = {"EUR": "\u20ac", "USD": "$", "GBP": "\u00a3", "CHF": "CHF"}
PREFIX = {"invoice": "RE", "credit_note": "GS"}


# ---- money and VAT -------------------------------------------------------------------------------

def vat_rate_for(tenant: Tenant) -> Decimal:
    """The VAT percentage included in the tenant's gross prices (tenant setting, else DEFAULT_VAT_RATE)."""
    if tenant.vat_rate is not None:
        return Decimal(tenant.vat_rate)
    return Decimal(str(current_app.config.get("DEFAULT_VAT_RATE", "19")))


def split_vat(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Return (net, vat) for a gross amount that already includes `rate` percent VAT."""
    gross = money(gross)
    if rate <= 0:
        return gross, Decimal("0.00")
    net = (gross / (Decimal(1) + rate / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return net, gross - net


def format_money(amount, currency: str, locale: str = "de") -> str:
    value = money(amount)
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.2f}"  # 1,234.56
    if locale in ("de", "nl", "es"):
        text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    elif locale == "fr":
        text = text.replace(",", "\u202f").replace(".", ",")
    symbol = SYMBOLS.get(currency, currency)
    return f"{sign}{symbol} {text}" if locale in ("en", "ar") and symbol != currency else f"{sign}{text} {symbol}"


# ---- seller, customer and lines ------------------------------------------------------------------

def seller_block(tenant: Tenant) -> dict:
    """Who issues the invoice. The operator's own parking falls back to the LEGAL_* settings used on the Impressum."""
    lines = [line.strip() for line in (tenant.invoice_seller or "").splitlines() if line.strip()]
    block = {"name": tenant.name, "lines": lines, "vat_id": (tenant.vat_id or "").strip(), "register": "", "email": "", "phone": ""}
    if not lines and tenant.slug == current_app.config.get("PUBLIC_TENANT_SLUG"):
        env = os.environ.get
        block["name"] = env("LEGAL_COMPANY") or tenant.name
        block["lines"] = [part for part in (env("LEGAL_STREET"), env("LEGAL_CITY")) if part]
        block["vat_id"] = block["vat_id"] or env("LEGAL_VAT_ID", "")
        block["register"] = env("LEGAL_REGISTER", "")
        block["email"] = env("LEGAL_EMAIL", "")
        block["phone"] = env("LEGAL_PHONE", "")
    return block


def _zone(tenant: Tenant):
    try:
        return ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def booking_lines(booking) -> list[dict]:
    """Invoice lines (gross) that always add up to booking.total_amount: parking, extras and an optional discount."""
    extras = [e for e in booking.extras if e.status != "cancelled"]
    extras_sum = sum((e.total() for e in extras), Decimal("0.00"))
    parking = money(Decimal(booking.total_amount) - extras_sum)
    discount = Decimal("0.00")
    if booking.discount_code and money(booking.subtotal_amount) > parking:
        discount = parking - money(booking.subtotal_amount)  # negative
        parking = money(booking.subtotal_amount)
    seconds = (_aware(booking.return_at) - _aware(booking.pickup_at)).total_seconds()
    lines = [{"kind": "parking", "lot": booking.lot.name, "space": booking.space.name, "quantity": 1,
              "days": max(1, math.ceil(seconds / 86400)), "unit": str(parking), "total": str(parking)}]
    for extra in extras:
        if extra.included_persons:  # bundle: base price covers the included persons, additional persons are their own line
            lines.append({"kind": "extra_bundle", "name": extra.name, "included": extra.included_persons, "detail": extra.details or "",
                          "quantity": 1, "unit": str(money(extra.unit_price)), "total": str(money(extra.unit_price))})
            if extra.additional_persons:
                more = extra.additional_persons
                lines.append({"kind": "extra_more", "name": extra.name, "quantity": more, "unit": str(money(extra.extra_person_price)),
                              "total": str(money(extra.extra_person_price) * more)})
            continue
        qty = extra.quantity or 1
        lines.append({"kind": "extra", "name": extra.name, "detail": extra.details or "", "quantity": qty,
                      "unit": str(money(extra.unit_price)), "total": str(money(extra.unit_price) * qty)})
    if discount < 0:
        lines.append({"kind": "discount", "code": booking.discount_code, "quantity": 1,
                      "unit": str(discount), "total": str(discount)})
    return lines


def _snapshot(booking, tenant: Tenant) -> dict:
    gross = money(booking.total_amount)
    rate = vat_rate_for(tenant)
    net, vat = split_vat(gross, rate)
    customer = booking.customer
    return {
        "currency": booking.currency, "vat_rate": rate, "net": net, "vat": vat, "gross": gross,
        "lines": booking_lines(booking), "seller": seller_block(tenant),
        "customer_name": f"{customer.first_name} {customer.last_name}".strip(),
        "customer_company": customer.company or "", "customer_email": customer.billing_email or customer.email,
        "locale": customer.locale or tenant.default_locale, "service_from": booking.pickup_at, "service_to": booking.return_at,
    }


# ---- issuing -------------------------------------------------------------------------------------

def _next_number(tenant_id: int, kind: str) -> str:
    """Gapless number per tenant and kind. The tenant row is locked so two requests can never share a number."""
    tenant = db.session.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update()).scalar_one()
    if kind == "credit_note":
        tenant.credit_seq = (tenant.credit_seq or 0) + 1
        seq = tenant.credit_seq
    else:
        tenant.invoice_seq = (tenant.invoice_seq or 0) + 1
        seq = tenant.invoice_seq
    return f"{PREFIX[kind]}-{seq:06d}"


def _apply(invoice: Invoice, snap: dict, *, sign: int = 1) -> None:
    factor = Decimal(sign)
    invoice.currency = snap["currency"]
    invoice.vat_rate = snap["vat_rate"]
    invoice.net_amount, invoice.vat_amount, invoice.gross_amount = snap["net"] * factor, snap["vat"] * factor, snap["gross"] * factor
    invoice.lines = [{**line, "unit": str(Decimal(line["unit"]) * factor), "total": str(Decimal(line["total"]) * factor)}
                     for line in snap["lines"]]
    invoice.seller = snap["seller"]
    invoice.customer_name, invoice.customer_company = snap["customer_name"], snap["customer_company"]
    invoice.customer_email, invoice.locale = snap["customer_email"], snap["locale"]
    invoice.service_from, invoice.service_to = snap["service_from"], snap["service_to"]


def latest_invoice(booking, kind: str = "invoice") -> Invoice | None:
    return db.session.execute(
        select(Invoice).where(Invoice.tenant_id == booking.tenant_id, Invoice.booking_id == booking.id, Invoice.kind == kind)
        .order_by(Invoice.id.desc())
    ).scalars().first()


def ensure_invoice(booking) -> Invoice | None:
    """Return the booking's invoice, issuing it if needed and refreshing it while it is still open.
    A cancelled booking never gets a new invoice. The caller commits."""
    tenant = db.session.get(Tenant, booking.tenant_id)
    invoice = latest_invoice(booking)
    if invoice is None:
        if booking.status == "cancelled":
            return None
        invoice = Invoice(tenant_id=booking.tenant_id, booking_id=booking.id, booking_ref=booking.booking_ref, kind="invoice",
                          number=_next_number(booking.tenant_id, "invoice"), status="open", issued_at=datetime.now(timezone.utc))
        _apply(invoice, _snapshot(booking, tenant))
        db.session.add(invoice)
        db.session.flush()
        return invoice
    if invoice.status == "open" and booking.status != "cancelled":
        _apply(invoice, _snapshot(booking, tenant))
    return invoice


def _paid_at(booking) -> datetime:
    payments = db.session.execute(
        select(Payment).where(Payment.tenant_id == booking.tenant_id, Payment.booking_id == booking.id, Payment.status == "paid")
    ).scalars().all()
    stamps = [p.paid_at for p in payments if p.paid_at]
    return max(stamps) if stamps else datetime.now(timezone.utc)


def issue_credit_note(booking, invoice: Invoice) -> Invoice:
    """A credit note that mirrors the invoice with negative amounts (idempotent)."""
    existing = db.session.execute(
        select(Invoice).where(Invoice.tenant_id == booking.tenant_id, Invoice.related_invoice_id == invoice.id, Invoice.kind == "credit_note")
    ).scalars().first()
    if existing is not None:
        return existing
    note = Invoice(tenant_id=booking.tenant_id, booking_id=booking.id, related_invoice_id=invoice.id, booking_ref=invoice.booking_ref,
                   kind="credit_note", number=_next_number(booking.tenant_id, "credit_note"), status="paid",
                   issued_at=datetime.now(timezone.utc))
    note.paid_at = note.issued_at
    note.currency, note.vat_rate = invoice.currency, invoice.vat_rate
    note.net_amount, note.vat_amount, note.gross_amount = -invoice.net_amount, -invoice.vat_amount, -invoice.gross_amount
    note.lines = [{**line, "unit": str(-Decimal(line["unit"])), "total": str(-Decimal(line["total"]))} for line in invoice.lines]
    note.seller, note.locale = invoice.seller, invoice.locale
    note.customer_name, note.customer_company, note.customer_email = invoice.customer_name, invoice.customer_company, invoice.customer_email
    note.service_from, note.service_to = invoice.service_from, invoice.service_to
    invoice.status = "credited"
    db.session.add(note)
    db.session.flush()
    return note


def sync_invoices(booking) -> Invoice | None:
    """Bring the booking's invoice documents in line with its current state. Safe to call at any time; caller commits."""
    invoice = ensure_invoice(booking)
    if invoice is None:
        return None
    if booking.payment_status == "refunded" and invoice.status in ("paid", "open"):
        if invoice.status == "open":
            invoice.status, invoice.paid_at = "paid", _paid_at(booking)  # money was received before it was returned
        issue_credit_note(booking, invoice)
    elif booking.payment_status == "paid" and invoice.status == "open" and booking.status != "cancelled":
        invoice.status, invoice.paid_at = "paid", _paid_at(booking)
    elif booking.status == "cancelled" and invoice.status == "open":
        invoice.status = "cancelled"
    return invoice


def detach_for_deleted_booking(booking) -> None:
    """Issued documents must be kept even when the booking row is deleted (statutory retention): unlink them."""
    for invoice in db.session.execute(select(Invoice).where(Invoice.tenant_id == booking.tenant_id, Invoice.booking_id == booking.id)).scalars():
        if invoice.status == "open":
            invoice.status = "cancelled"
        invoice.booking_id = None
    from ..models import Notification
    for row in db.session.execute(select(Notification).where(Notification.tenant_id == booking.tenant_id, Notification.booking_id == booking.id)).scalars():
        row.booking_id = None


# ---- PDF ----------------------------------------------------------------------------------------

def _safe(text) -> str:
    """Built-in PDF fonts use WinAnsi: keep what they can draw, replace the rest."""
    return str(text or "").encode("cp1252", "replace").decode("cp1252")


def document_data(invoice: Invoice, tenant: Tenant) -> dict:
    zone = _zone(tenant)
    local = lambda dt: _aware(dt).astimezone(zone) if dt else None  # noqa: E731
    related = None
    if invoice.related_invoice_id:
        related_row = db.session.get(Invoice, invoice.related_invoice_id)
        related = related_row.number if related_row else None
    return {
        "kind": invoice.kind, "number": invoice.number, "status": invoice.status, "booking_ref": invoice.booking_ref,
        "currency": invoice.currency, "vat_rate": Decimal(invoice.vat_rate), "net": Decimal(invoice.net_amount),
        "vat": Decimal(invoice.vat_amount), "gross": Decimal(invoice.gross_amount), "lines": invoice.lines or [],
        "seller": invoice.seller or {}, "customer_name": invoice.customer_name, "customer_company": invoice.customer_company,
        "customer_email": invoice.customer_email, "locale": invoice.locale, "issued": local(invoice.issued_at),
        "paid": local(invoice.paid_at), "from": local(invoice.service_from), "to": local(invoice.service_to), "related": related,
        "brand": tenant.primary_color or "#14385C", "logo": tenant.logo_data or None,
    }


def render_invoice_pdf(data: dict, t) -> bytes:
    """Draw the document. `t(key, **values)` returns translated label text; no Flask objects are needed."""
    locale = data["locale"]
    credit = data["kind"] == "credit_note"
    money_text = lambda value: _safe(format_money(value, data["currency"], locale))  # noqa: E731
    date_text = lambda value: value.strftime("%d.%m.%Y") if value else ""  # noqa: E731
    try:
        brand = HexColor(data.get("brand") or "#14385C")
    except (ValueError, TypeError):
        brand = NAVY

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    left, right = 48, width - 48
    seller = data["seller"]

    def page_header():
        c.setFillColor(brand); c.rect(0, height - 110, width, 110, fill=1, stroke=0)
        c.setFillColor(GOLD); c.rect(0, height - 114, width, 4, fill=1, stroke=0)
        name_x = left
        logo = _logo_reader(data.get("logo"))
        if logo is not None:  # logo on a white tile so any colour of logo stays readable on the brand-coloured header
            c.setFillColor(white); c.roundRect(left, height - 98, 78, 78, 8, fill=1, stroke=0)
            iw, ih = logo.getSize(); scale = min(66 / iw, 66 / ih)
            c.drawImage(logo, left + (78 - iw * scale) / 2, height - 98 + (78 - ih * scale) / 2, width=iw * scale, height=ih * scale, mask="auto")
            name_x = left + 94
        c.setFillColor(white); c.setFont("Helvetica-Bold", 21); c.drawString(name_x, height - 58, _safe(seller.get("name", ""))[:40])
        c.setFont("Helvetica", 13); c.drawString(name_x, height - 82, _safe(t("invoice.title_credit" if credit else "invoice.title_invoice")))
        c.setFont("Courier-Bold", 15); c.drawRightString(right, height - 58, _safe(data["number"]))

    def footer():
        c.setStrokeColor(RULE); c.setLineWidth(0.6); c.line(left, 74, right, 74)
        c.setFillColor(MUTED); c.setFont("Helvetica", 8)
        bits = [seller.get("name", "")] + list(seller.get("lines", []))
        row1 = "  |  ".join(_safe(b) for b in bits if b)
        extra = []
        if seller.get("vat_id"):
            extra.append(f'{t("invoice.vat_id")}: {seller["vat_id"]}')
        if seller.get("register"):
            extra.append(seller["register"])
        if seller.get("email"):
            extra.append(seller["email"])
        if seller.get("phone"):
            extra.append(seller["phone"])
        c.drawString(left, 62, row1[:150])
        c.drawString(left, 51, _safe("  |  ".join(extra))[:150])

    page_header()
    y = height - 150
    # customer (left) and document facts (right)
    c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(left, y, _safe(t("invoice.customer")))
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 12)
    cy = y - 16
    for text in [data["customer_company"], data["customer_name"]]:
        if text:
            c.drawString(left, cy, _safe(text)[:46]); cy -= 15
    c.setFont("Helvetica", 10); c.setFillColor(MUTED)
    if data["customer_email"]:
        c.drawString(left, cy, _safe(data["customer_email"])[:50])
    facts = [(t("invoice.number"), data["number"]), (t("invoice.date"), date_text(data["issued"])), (t("invoice.booking"), data["booking_ref"])]
    if data["from"] and data["to"]:
        facts.append((t("invoice.service_period"), f'{date_text(data["from"])} - {date_text(data["to"])}'))
    if credit and data["related"]:
        facts.append((t("invoice.credit_for"), data["related"]))
    fy = y
    for label, value in facts:
        c.setFillColor(MUTED); c.setFont("Helvetica", 9); c.drawString(width - 270, fy, _safe(label))
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 10); c.drawRightString(right, fy, _safe(value))
        fy -= 16
    y = min(cy, fy) - 34

    def table_head(at):
        c.setFillColor(brand); c.rect(left, at - 6, right - left, 22, fill=1, stroke=0)
        c.setFillColor(white); c.setFont("Helvetica-Bold", 9)
        c.drawString(left + 8, at + 1, _safe(t("invoice.description")))
        c.drawRightString(right - 190, at + 1, _safe(t("invoice.qty")))
        c.drawRightString(right - 100, at + 1, _safe(t("invoice.unit_price")))
        c.drawRightString(right - 8, at + 1, _safe(t("invoice.amount")))
        return at - 24

    y = table_head(y)
    for line in data["lines"]:
        if line.get("kind") == "parking":
            title = t("invoice.parking_line", lot=line.get("lot", ""), space=line.get("space", ""))
            detail = ""
            if data["from"] and data["to"]:
                detail = f'{data["from"].strftime("%d.%m.%Y %H:%M")} - {data["to"].strftime("%d.%m.%Y %H:%M")}'
                if line.get("days"):
                    detail += f' ({t("invoice.stay_days", count=line["days"])})'
        elif line.get("kind") == "extra_bundle":
            title, detail = t("invoice.extra_included", name=line.get("name", ""), included=line.get("included", 0)), line.get("detail", "")
        elif line.get("kind") == "extra_more":
            title, detail = t("invoice.extra_additional", name=line.get("name", "")), ""
        elif line.get("kind") == "discount":
            title, detail = t("invoice.discount_line", code=line.get("code", "")), ""
        else:
            title, detail = line.get("name", ""), line.get("detail", "")
        wrapped = simpleSplit(_safe(title), "Helvetica-Bold", 10, right - left - 250) or [""]
        needed = 16 * len(wrapped) + (12 if detail else 0) + 10
        if y - needed < 130:
            footer(); c.showPage(); page_header(); y = table_head(height - 150)
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 10)
        ty = y
        for part in wrapped:
            c.drawString(left + 8, ty, part); ty -= 14
        if detail:
            c.setFillColor(MUTED); c.setFont("Helvetica", 8.5); c.drawString(left + 8, ty, _safe(detail)[:80]); ty -= 11
        c.setFillColor(INK); c.setFont("Helvetica", 10)
        c.drawRightString(right - 190, y, str(line.get("quantity", 1)))
        c.drawRightString(right - 100, y, money_text(line.get("unit", 0)))
        c.drawRightString(right - 8, y, money_text(line.get("total", 0)))
        y = ty - 6
        c.setStrokeColor(RULE); c.setLineWidth(0.4); c.line(left, y + 2, right, y + 2)
        y -= 8

    if y < 210:
        footer(); c.showPage(); page_header(); y = height - 150
    y -= 6
    rate_text = f'{data["vat_rate"]:.2f}'
    if rate_text.endswith(".00"):
        rate_text = rate_text[:-3]
    for label, value, bold in ((t("invoice.net"), data["net"], False), (t("invoice.vat", rate=rate_text), data["vat"], False)):
        c.setFillColor(MUTED); c.setFont("Helvetica", 10); c.drawRightString(right - 110, y, _safe(label))
        c.setFillColor(INK); c.drawRightString(right - 8, y, money_text(value)); y -= 16
    c.setStrokeColor(GOLD); c.setLineWidth(1.5); c.line(right - 250, y + 8, right, y + 8)
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 13)
    c.drawRightString(right - 110, y - 8, _safe(t("invoice.credit_total" if credit else "invoice.gross")))
    c.setFont("Helvetica-Bold", 15); c.drawRightString(right - 8, y - 8, money_text(data["gross"]))
    y -= 40

    # payment line
    if credit:
        note, colour = t("invoice.credit_refund"), GREEN
    elif data["status"] == "paid":
        note, colour = t("invoice.paid_on", date=date_text(data["paid"])), GREEN
    elif data["status"] in ("cancelled", "credited"):
        note, colour = t("invoice.cancelled"), RED
    else:
        note, colour = t("invoice.due_arrival"), MUTED
    c.setFillColor(colour); c.setFont("Helvetica-Bold", 11)
    for part in simpleSplit(_safe(note), "Helvetica-Bold", 11, right - left):
        c.drawString(left, y, part); y -= 14
    c.setFillColor(MUTED); c.setFont("Helvetica", 9)
    for part in simpleSplit(_safe(t("invoice.thanks")), "Helvetica", 9, right - left):
        c.drawString(left, y - 6, part); y -= 12
    y -= 16
    if y < 110:  # keep the explanation clear of the footer
        footer(); c.showPage(); page_header(); y = height - 150
    c.setFillColor(MUTED); c.setFont("Helvetica-Oblique", 8)
    for part in simpleSplit(_safe(t("invoice.qty_note")), "Helvetica-Oblique", 8, right - left):
        c.drawString(left, y, part); y -= 10
    footer()
    c.showPage(); c.save()
    return buf.getvalue()


def _logo_reader(blob):
    if not blob:
        return None
    try:
        return ImageReader(BytesIO(blob))
    except Exception:  # a broken logo must never stop an invoice
        return None


def invoice_pdf(invoice: Invoice) -> bytes:
    tenant = db.session.get(Tenant, invoice.tenant_id)
    locale = "en" if invoice.locale == "ar" else invoice.locale  # built-in PDF fonts have no Arabic glyphs

    def t(key: str, **values):
        return translate(locale, key, **values)

    return render_invoice_pdf(document_data(invoice, tenant), t)


def invoice_filename(invoice: Invoice) -> str:
    return f"{'credit-note' if invoice.kind == 'credit_note' else 'invoice'}-{invoice.number}.pdf"
