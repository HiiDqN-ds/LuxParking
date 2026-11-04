"""Account notifications."""
from __future__ import annotations

from email.message import EmailMessage

from flask import current_app

from ..ui_i18n import ui


def notify_email_changed(old_email: str, new_email: str) -> None:
    """Tell the OLD address that the account email changed (the usual sign of a hijacked account). Best effort."""
    cfg = current_app.config
    if not (cfg.get("SMTP_HOST") and cfg.get("SMTP_FROM")):
        if cfg.get("APP_ENV") != "production":
            current_app.logger.warning("[DEV ONLY] Email change notice for %s -> %s was not sent (SMTP is not set up).", old_email, new_email)
        return
    try:
        from .booking_notifications import smtp_send

        message = EmailMessage()
        message["Subject"] = ui("Your ParkPilot email address was changed")
        message["From"], message["To"] = cfg["SMTP_FROM"], old_email
        message.set_content("\n".join((
            ui("The email address of your ParkPilot account was changed to %(email)s.", email=new_email),
            ui("If you did not do this, change your password now and contact your administrator."),
        )))
        smtp_send(message)
    except Exception:
        current_app.logger.exception("Could not send the email-change notice.")
