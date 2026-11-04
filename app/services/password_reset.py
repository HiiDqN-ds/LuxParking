"""Forgot password, two ways: a one-time link by email, or a 6-digit code by SMS.

Security rules
- The request never reveals whether an account exists: the answer is always the same.
- Only a keyed hash (HMAC with SECRET_KEY) of the link secret / code is stored. Each request is single-use.
- Link: valid LINK_MINUTES. Code: valid CODE_MINUTES and locked after MAX_CODE_ATTEMPTS wrong guesses.
- A new request cancels the older unused one; a new request within COOLDOWN_SECONDS is ignored (no SMS/email flood).
- A successful reset sets the new password, lifts the account lockout, cancels every other open request and signs
  out all existing sessions (the session id is bound to the password hash).
- Inactive users and users of a suspended parking get nothing.
- Without SMTP / Twilio settings nothing can be delivered. In development the link or code is printed to the
  terminal (never in production).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import current_app, request, url_for
from sqlalchemy import func

from ..extensions import db
from ..models import AuditLog, PasswordReset, User, utcnow
from ..tenancy import use_tenant
from ..ui_i18n import ui
from . import messaging

LINK_MINUTES = 30
CODE_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
COOLDOWN_SECONDS = 60


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _digest(user_id: int, secret: str) -> str:
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"{user_id}:{secret}".encode("utf-8"), hashlib.sha256).hexdigest()


def is_dev() -> bool:
    return current_app.config.get("APP_ENV") != "production"


def find_user(identifier: str) -> User | None:
    """The one active user an email address or phone number belongs to (None when unknown, ambiguous or blocked)."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    with use_tenant(None):
        if "@" in ident:
            user = User.query.filter(func.lower(User.email) == ident.lower()).first()
        else:
            phone = messaging.normalize_phone(ident)
            matches = User.query.filter_by(phone=phone).limit(2).all() if phone else []
            user = matches[0] if len(matches) == 1 else None  # a shared number cannot identify one person
        if user is None or not user.active:
            return None
        if user.tenant_id and user.tenant is not None and user.tenant.status == "suspended":
            return None
    return user


def _open_requests(user_id: int, channel: str | None = None):
    query = PasswordReset.query.filter(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
    if channel:
        query = query.filter(PasswordReset.channel == channel)
    return query


def request_reset(identifier: str, channel: str) -> None:
    """Create and deliver a reset link (channel "email") or code ("sms"). Silent when nothing can be sent."""
    user = find_user(identifier)
    if user is None or channel not in ("email", "sms"):
        return
    if channel == "sms" and not user.phone:
        return
    now = utcnow()
    with use_tenant(None):
        latest = PasswordReset.query.filter_by(user_id=user.id).order_by(PasswordReset.created_at.desc()).first()
        if latest is not None and _aware(latest.created_at) > now - timedelta(seconds=COOLDOWN_SECONDS):
            return
        for old in _open_requests(user.id, channel).all():
            old.used_at = now
        secret = f"{secrets.randbelow(10 ** 6):06d}" if channel == "sms" else secrets.token_urlsafe(32)
        row = PasswordReset(
            user_id=user.id, channel=channel, secret_hash=_digest(user.id, secret),
            expires_at=now + timedelta(minutes=CODE_MINUTES if channel == "sms" else LINK_MINUTES),
        )
        db.session.add(row)
        db.session.add(AuditLog(tenant_id=user.tenant_id, user_id=user.id, action="password.reset.request", entity="user",
                                entity_id=user.id, data={"channel": channel}, ip=request.remote_addr if request else None))
        db.session.commit()
        token = f"{row.id}.{secret}"
    try:
        if channel == "email":
            _send_email(user, token)
        else:
            _send_sms(user, secret)
    except Exception:  # never tell the visitor: the answer must be identical for every account
        current_app.logger.exception("Could not deliver a password reset %s for user %s.", channel, user.id)


def _base_url() -> str:
    return (current_app.config.get("SITE_URL") or request.url_root).rstrip("/")


def _send_email(user: User, token: str) -> None:
    link = _base_url() + url_for("auth.reset_with_link", token=token)
    cfg = current_app.config
    if not (cfg.get("SMTP_HOST") and cfg.get("SMTP_FROM")):
        _undeliverable("email", user, link)
        return
    from .booking_notifications import smtp_send

    message = EmailMessage()
    message["Subject"] = ui("Reset your ParkPilot password")
    message["From"], message["To"] = cfg["SMTP_FROM"], user.email
    message.set_content("\n".join((
        ui("Someone asked to reset the password of your ParkPilot account."),
        ui("Open this link to choose a new password. It works once and expires in %(minutes)s minutes:", minutes=LINK_MINUTES),
        "", link, "",
        ui("If you did not ask for this, ignore this email: your password stays as it is."),
    )))
    smtp_send(message)


def _send_sms(user: User, code: str) -> None:
    body = ui("ParkPilot code: %(code)s. It expires in %(minutes)s minutes. Never share it.", code=code, minutes=CODE_MINUTES)
    try:
        messaging.send_message("sms", user.phone, body)
    except messaging.MessagingNotConfigured:
        _undeliverable("sms", user, code)


def _undeliverable(channel: str, user: User, secret: str) -> None:
    if is_dev():
        what = "link" if channel == "email" else "code"
        current_app.logger.warning("[DEV ONLY] Password reset %s for %s: %s (%s is not set up, so it was not sent)",
                                   what, user.email, secret, "SMTP" if channel == "email" else "Twilio SMS")
    else:
        current_app.logger.error("Password reset requested but %s delivery is not configured.", channel)


def _finish(user: User, row: PasswordReset, new_password: str) -> None:
    now = utcnow()
    user.set_password(new_password)
    user.failed_logins, user.locked_until = 0, None
    for other in _open_requests(user.id).all():
        other.used_at = now
    row.used_at = now
    db.session.add(AuditLog(tenant_id=user.tenant_id, user_id=user.id, action="password.reset", entity="user",
                            entity_id=user.id, data={"channel": row.channel}, ip=request.remote_addr if request else None))
    db.session.commit()


def valid_link(token: str) -> tuple[PasswordReset, User] | None:
    """The open email-link request for `token` ("<id>.<secret>"), or None."""
    try:
        raw_id, secret = (token or "").split(".", 1)
        request_id = int(raw_id)
    except ValueError:
        return None
    with use_tenant(None):
        row = db.session.get(PasswordReset, request_id)
        if row is None or row.channel != "email" or row.used_at is not None or _aware(row.expires_at) <= utcnow():
            return None
        if not hmac.compare_digest(row.secret_hash, _digest(row.user_id, secret)):
            return None
        user = db.session.get(User, row.user_id)
        if user is None or not user.active:
            return None
        if user.tenant_id and user.tenant is not None and user.tenant.status == "suspended":
            return None
    return row, user


def reset_with_link(token: str, new_password: str) -> bool:
    found = valid_link(token)
    if found is None:
        return False
    row, user = found
    with use_tenant(None):
        _finish(user, row, new_password)
    return True


def reset_with_code(identifier: str, code: str, new_password: str) -> bool:
    """Check the texted code for the account behind `identifier` and set the new password."""
    user = find_user(identifier)
    code = (code or "").strip()
    if user is None:
        hmac.compare_digest(_digest(0, code), _digest(0, "000000"))  # similar work for unknown accounts
        return False
    with use_tenant(None):
        row = _open_requests(user.id, "sms").order_by(PasswordReset.created_at.desc()).first()
        if row is None or _aware(row.expires_at) <= utcnow() or row.attempts >= MAX_CODE_ATTEMPTS:
            return False
        row.attempts += 1
        if not hmac.compare_digest(row.secret_hash, _digest(user.id, code)):
            if row.attempts >= MAX_CODE_ATTEMPTS:
                row.used_at = utcnow()  # too many wrong guesses: this code is dead, ask for a new one
            db.session.commit()
            return False
        _finish(user, row, new_password)
    return True
