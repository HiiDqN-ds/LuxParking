"""Generate and email private PDF evidence reports for vehicle inspections."""

import smtplib
import ssl
import unicodedata
from email.message import EmailMessage
from io import BytesIO

from flask import current_app
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from ..extensions import db
from ..ui_i18n import ui
from ..models import Inspection, InspectionPhoto, utcnow
from .inspections import read_object


class InspectionReportError(Exception):
    """Raised when an inspection report cannot be generated or delivered."""


def _format_capture(photo: InspectionPhoto) -> str:
    return photo.captured_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")


CATEGORY_LABELS = {
    "front": "Front", "back": "Back", "right": "Right side", "left": "Left side",
    "corner_front_left": "Front left corner", "corner_front_right": "Front right corner",
    "corner_back_left": "Rear left corner", "corner_back_right": "Rear right corner",
    "roof": "Roof", "windshield": "Windshield", "odometer": "Odometer", "fuel_level": "Fuel level", "signature": "Customer signature",
}


def _pdf_text(value: object) -> str:
    """The built-in PDF fonts draw Western European letters (umlauts, accents) but nothing else."""
    return str(value).encode("cp1252", "replace").decode("cp1252")


def _report_locale(inspection: Inspection) -> str:
    """The customer's language; Arabic falls back to English because the built-in PDF fonts have no Arabic glyphs."""
    locale = inspection.booking.customer.locale or "de"
    return "en" if locale == "ar" else locale


def build_inspection_pdf(inspection: Inspection) -> bytes:
    booking = inspection.booking
    loc = _report_locale(inspection)
    T = lambda text, **values: ui(text, locale=loc, **values)  # noqa: E731
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    page_width, page_height = A4
    document.setTitle(T("Vehicle inspection - %(ref)s", ref=booking.booking_ref))
    document.setFont("Helvetica-Bold", 18)
    document.drawString(48, page_height - 56, _pdf_text(T("ParkPilot vehicle inspection")))
    document.setFont("Helvetica", 10)
    summary = (
        T("Booking: %(value)s", value=booking.booking_ref),
        T("Customer: %(value)s", value=f"{booking.customer.first_name} {booking.customer.last_name}".strip()),
        T("Vehicle: %(value)s", value=booking.vehicle.plate if booking.vehicle else T("Not provided")),
        T("Inspection: %(value)s", value=T(inspection.phase.title())),
        T("Staff: %(value)s", value=inspection.staff_user.name if inspection.staff_user else T("Former staff member")),
        T("Fuel: %(value)s%%", value=inspection.fuel_level),
        T("Odometer: %(value)s km", value=inspection.odometer_km),
        T("Keys received: %(value)s", value=inspection.key_count),
        T("Belongings: %(value)s", value=inspection.belongings or T("None recorded")),
        T("Notes: %(value)s", value=inspection.notes or T("None")),
    )
    y = page_height - 86
    for line in summary:
        document.drawString(48, y, _pdf_text(line)[:115])
        y -= 17
    document.drawString(
        48,
        y - 4,
        _pdf_text(T("No damage confirmed.") if inspection.no_damage_confirmed else T("Damage marks are shown in the evidence pages.")),
    )
    for mark in inspection.damage_marks:
        match_status = T("Pre-existing") if mark.matched_damage_id else T("Unmatched departure damage")
        document.drawString(
            48,
            y - 21,
            _pdf_text(
                T("%(kind)s at %(x)s%%, %(y)s%% (%(status)s): %(note)s", kind=T(mark.kind.replace("_", " ").title()),
                  x=f"{mark.x_percent:.0f}", y=f"{mark.y_percent:.0f}", status=match_status, note=(mark.notes or T("No note"))[:70])
            ),
        )
        y -= 17
    document.showPage()

    photos = db.session.execute(
        db.select(InspectionPhoto)
        .where(
            InspectionPhoto.tenant_id == inspection.tenant_id,
            InspectionPhoto.inspection_id == inspection.id,
        )
        .order_by(InspectionPhoto.category)
    ).scalars().all()
    arrival_photos = {}
    if inspection.phase == "departure":
        arrival = db.session.execute(
            db.select(Inspection)
            .where(
                Inspection.tenant_id == inspection.tenant_id,
                Inspection.booking_id == booking.id,
                Inspection.phase == "arrival",
                Inspection.status == "completed",
            )
        ).scalar_one_or_none()
        if arrival is not None:
            arrival_photos = {
                photo.category: photo
                for photo in db.session.execute(
                    db.select(InspectionPhoto).where(
                        InspectionPhoto.tenant_id == inspection.tenant_id,
                        InspectionPhoto.inspection_id == arrival.id,
                    )
                ).scalars().all()
            }
    for photo in photos:
        before = arrival_photos.get(photo.category)
        paired = [(T("Arrival"), before), (T("Departure"), photo)] if before else [(T("Evidence"), photo)]
        paired_width = (page_width - 100) / len(paired)
        for index, (label, evidence) in enumerate(paired):
            x = 48 + index * (paired_width + 4)
            document.setFont("Helvetica-Bold", 14 if not before else 11)
            document.drawString(
                x,
                page_height - 48,
                _pdf_text(T("%(category)s - %(label)s", category=T(CATEGORY_LABELS.get(photo.category, photo.category.replace("_", " ").title())), label=label)),
            )
            document.setFont("Helvetica", 8)
            document.drawString(x, page_height - 66, _pdf_text(T("Captured %(when)s by %(who)s", when=_format_capture(evidence), who=evidence.staff_name)))
            document.drawString(x, page_height - 81, f"SHA-256: {evidence.sha256}")
            image = ImageReader(BytesIO(read_object(object_key=evidence.object_key)))
            image_width, image_height = image.getSize()
            max_width = paired_width - 12
            max_height = page_height - 150
            scale = min(max_width / image_width, max_height / image_height)
            width, height = image_width * scale, image_height * scale
            document.drawImage(
                image,
                x + (paired_width - width) / 2,
                (page_height - 110 - height) / 2,
                width=width,
                height=height,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
        document.showPage()
    document.save()
    return output.getvalue()


def send_inspection_report(inspection_id: int, tenant_id: int) -> None:
    inspection = db.session.execute(
        db.select(Inspection).where(
            Inspection.id == inspection_id,
            Inspection.tenant_id == tenant_id,
            Inspection.status == "completed",
        )
    ).scalar_one_or_none()
    if inspection is None:
        raise InspectionReportError("Completed inspection not found.")
    host = current_app.config.get("SMTP_HOST")
    sender = current_app.config.get("SMTP_FROM")
    if not host or not sender:
        raise InspectionReportError("SMTP_HOST and SMTP_FROM must be configured to email reports.")

    try:
        pdf = build_inspection_pdf(inspection)
        message = EmailMessage()
        loc = _report_locale(inspection)
        message["Subject"] = ui("Vehicle inspection for booking %(ref)s", locale=loc, ref=inspection.booking.booking_ref)
        message["From"] = sender
        message["To"] = inspection.booking.customer.email
        message.set_content(ui("Attached is the %(phase)s vehicle inspection report for booking %(ref)s.", locale=loc,
                               phase=ui(inspection.phase, locale=loc), ref=inspection.booking.booking_ref))
        message.add_attachment(
            pdf,
            maintype="application",
            subtype="pdf",
            filename=f"parkpilot-inspection-{inspection.booking.booking_ref}-{inspection.phase}.pdf",
        )
        port = current_app.config.get("SMTP_PORT", 587)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
                _authenticate_and_send(smtp, message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                _authenticate_and_send(smtp, message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        current_app.logger.exception("Could not email report for inspection %s.", inspection_id)
        raise InspectionReportError("The PDF report could not be delivered.") from exc

    inspection.report_sent_at = utcnow()
    db.session.commit()


def _authenticate_and_send(smtp: smtplib.SMTP, message: EmailMessage) -> None:
    username = current_app.config.get("SMTP_USERNAME")
    password = current_app.config.get("SMTP_PASSWORD")
    if username:
        smtp.login(username, password or "")
    smtp.send_message(message)
