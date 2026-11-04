"""Brute-force protection for the login form.

Two independent limits, both stored in the database so they hold across several workers:
  * per account: LOGIN_MAX_FAILURES wrong passwords in a row lock that account for LOGIN_LOCK_MINUTES;
  * per IP address: more than LOGIN_IP_MAX_FAILURES failed attempts within the window blocks that IP,
    which also covers guessing of unknown email addresses.
Responses never reveal whether an email exists.
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models import AuditLog, User, utcnow
from ..tenancy import use_tenant

LOGIN_FAILED = "login_failed"


def _aware(dt):
    from datetime import timezone
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cfg(name: str, default: int) -> int:
    return int(current_app.config.get(name, default))


def is_locked(user: User | None) -> bool:
    return bool(user and user.locked_until and _aware(user.locked_until) > utcnow())


def ip_blocked(ip: str | None) -> bool:
    if not ip:
        return False
    window = utcnow() - timedelta(minutes=_cfg("LOGIN_LOCK_MINUTES", 15))
    with use_tenant(None):
        count = db.session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == LOGIN_FAILED, AuditLog.ip == ip, AuditLog.created_at > window)
        )
    return count >= _cfg("LOGIN_IP_MAX_FAILURES", 20)


def record_failure(user: User | None, ip: str | None) -> None:
    """Count a failed attempt (call before committing). Locks the account after repeated failures."""
    db.session.add(AuditLog(tenant_id=user.tenant_id if user else None, user_id=user.id if user else None,
                            action=LOGIN_FAILED, entity="user", entity_id=user.id if user else None, ip=ip))
    if user is not None:
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= _cfg("LOGIN_MAX_FAILURES", 5):
            user.locked_until = utcnow() + timedelta(minutes=_cfg("LOGIN_LOCK_MINUTES", 15))
            user.failed_logins = 0


def record_success(user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
