"""Security hardening: Content-Security-Policy with per-request nonces, extra response headers,
session lifetime / idle timeout, and the shared rate limiter.

Everything here is configuration-driven (see `Config` in config.py) so it can be tuned per deployment.
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlsplit

from flask import abort, current_app, g, redirect, request, session, url_for
from flask_login import current_user, logout_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Rate limiter. Storage comes from RATELIMIT_STORAGE_URI (set it to your Redis URL in production so limits are
# shared between workers); the in-memory default is per process and fine for a single worker or for development.
limiter = Limiter(key_func=get_remote_address, default_limits=[], headers_enabled=True, strategy="fixed-window")

_STATIC_PREFIXES = ("/static/",)
_SENSITIVE_PREFIXES = ("/admin", "/staff", "/booking/", "/t/", "/billing", "/reports")
_SENSITIVE_ENDPOINTS = frozenset(
    {"public.hold_confirmation", "public.localized_hold_confirmation"}
)
_IDLE_KEY = "_last_seen"


def _csp_for_request(nonce: str) -> str:
    app = current_app
    configured_site_url = app.config.get("SITE_URL")
    configured_origin = urlsplit(configured_site_url) if configured_site_url else None
    host = (
        configured_origin.netloc
        if configured_origin
        and configured_origin.scheme in {"http", "https"}
        and configured_origin.hostname
        and not configured_origin.username
        and not configured_origin.password
        else request.host
    )
    if request.path == "/v1/docs":
        # Swagger UI is loaded from a CDN and starts with an inline call. No cookies or secrets live on this page.
        return ("default-src 'none'; script-src 'self' https://unpkg.com 'unsafe-inline'; "
                "style-src 'self' https://unpkg.com 'unsafe-inline'; img-src 'self' data: https:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    if request.path.startswith("/v1/") or request.path == "/healthz":
        return "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    directives = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",              # inline style attributes are used for tenant branding colours
        "img-src 'self' data: https:",                   # tenant logos / lot photos can be any HTTPS URL
        "font-src 'self' data:",
        f"connect-src 'self' wss://{host} ws://{host}",  # Socket.IO live availability
        "worker-src 'self'",                             # staff PWA service worker
        "manifest-src 'self'",
        "media-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        # Stripe Checkout / Billing Portal are reached by redirect after a form post, which form-action also governs.
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com",
        "frame-ancestors 'none'",
    ]
    if app.config["APP_ENV"] == "production":
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def init_security(app) -> None:
    limiter.init_app(app)

    @app.before_request
    def _security_before_request():
        g.csp_nonce = secrets.token_urlsafe(16)
        if request.path.startswith(_STATIC_PREFIXES) or request.path == "/healthz":
            return None
        return _enforce_session_lifetime()

    @app.context_processor
    def _inject_nonce():
        return {"csp_nonce": lambda: g.get("csp_nonce", "")}

    @app.after_request
    def _security_after_request(resp):
        nonce = g.get("csp_nonce")
        if nonce and not request.path.startswith(_STATIC_PREFIXES):
            resp.headers.setdefault("Content-Security-Policy", _csp_for_request(nonce))
        resp.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        resp.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(), geolocation=(), payment=(self), usb=(), serial=(), bluetooth=(), interest-cohort=()")
        if (
            request.path.startswith(_SENSITIVE_PREFIXES)
            or request.endpoint in _SENSITIVE_ENDPOINTS
        ) and not request.path.startswith("/admin/avatar/"):
            resp.headers["Cache-Control"] = "no-store"  # customer data and tokens in URLs: never keep a copy
            resp.headers.setdefault("Pragma", "no-cache")
        if request.endpoint in _SENSITIVE_ENDPOINTS:
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        if app.config["APP_ENV"] == "production":
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return resp


def _enforce_session_lifetime():
    """Sign users out after a period without activity (absolute lifetime is PERMANENT_SESSION_LIFETIME)."""
    if not current_user.is_authenticated:
        return None
    idle = int(current_app.config.get("IDLE_TIMEOUT_MINUTES", 0) or 0)
    now = int(time.time())
    last = session.get(_IDLE_KEY)
    if idle and isinstance(last, int) and now - last > idle * 60:
        logout_user()
        session.clear()
        if request.method == "GET" and not request.path.startswith("/v1/"):
            return _redirect_login()
        abort(401)
    session[_IDLE_KEY] = now
    return None


def _redirect_login():
    from flask import flash
    from .ui_i18n import ui

    flash(ui("You were signed out after a period of inactivity. Please sign in again."), "error")
    return redirect(url_for("auth.login", next=request.path))
