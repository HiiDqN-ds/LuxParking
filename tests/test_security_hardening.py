"""Regression tests for the security hardening pass (CSP, sessions, password policy, rate limits, webhooks)."""
import re
import socket
import time
from pathlib import Path

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Role, User
from app.password_policy import validate_password
from app.seed import seed_demo
from conftest import login


def _new_request():
    """The test fixture keeps one app context open, so Flask-Login's per-request cache would leak between requests."""
    from flask import g
    g.pop("_login_user", None)

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


# --- Content-Security-Policy -------------------------------------------------------------------------

def test_csp_forbids_inline_scripts_and_framing(client):
    r = client.get("/admin/login")
    csp = r.headers["Content-Security-Policy"]
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "'unsafe-inline'" not in script_src and "'nonce-" in script_src
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'self'" in csp


def test_csp_nonce_changes_per_request(client):
    nonces = {re.search(r"nonce-([\w-]+)", client.get("/admin/login").headers["Content-Security-Policy"]).group(1) for _ in range(3)}
    assert len(nonces) == 3


def test_api_responses_get_a_locked_down_csp(client):
    assert client.get("/healthz").headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_no_inline_event_handlers_remain_in_templates():
    offenders = [str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html")
                 if re.search(r"\son(click|submit|change|load|error|input|focus)\s*=", p.read_text(encoding="utf-8"))]
    assert not offenders, offenders


def test_inline_scripts_carry_the_nonce():
    for path in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", path.read_text(encoding="utf-8")):
            assert "nonce=" in tag, f"{path.name}: {tag}"


def test_sensitive_pages_are_not_cacheable(client):
    login(client)
    assert client.get("/admin/").headers["Cache-Control"] == "no-store"


# --- sessions ---------------------------------------------------------------------------------------

def test_deactivated_user_loses_an_existing_session(client, app):
    login(client)
    assert client.get("/admin/").status_code == 200
    User.query.filter_by(email="owner@test.dev").one().active = False
    db.session.commit()
    _new_request()
    assert client.get("/admin/").status_code in (302, 401)


def test_password_change_signs_out_other_sessions(app):
    first, second = app.test_client(), app.test_client()
    login(first)
    _new_request()
    login(second)
    _new_request()
    assert second.get("/admin/").status_code == 200
    _new_request()
    r = first.post("/admin/account/password", data={
        "current_password": "correct-horse", "new_password": "a-much-longer-passphrase-42",
        "confirm_password": "a-much-longer-passphrase-42"}, follow_redirects=True)
    assert r.status_code == 200
    _new_request()
    assert first.get("/admin/").status_code == 200           # the session that changed it stays valid
    _new_request()
    assert second.get("/admin/").status_code in (302, 401)    # every other session is gone
    _new_request()
    assert login(app.test_client(), password="correct-horse").status_code == 200  # old password no longer works
    assert b"Dashboard" not in app.test_client().get("/admin/").data


def test_password_change_requires_the_current_password_and_a_strong_one(client):
    login(client)
    base = {"current_password": "correct-horse", "new_password": "a-much-longer-passphrase-42", "confirm_password": "a-much-longer-passphrase-42"}
    assert b"current password is incorrect" in client.post("/admin/account/password", data={**base, "current_password": "nope"}).data
    assert b"do not match" in client.post("/admin/account/password", data={**base, "confirm_password": "different-passphrase-99"}).data
    assert b"too common" in client.post("/admin/account/password", data={**base, "new_password": "password1234", "confirm_password": "password1234"}).data


def test_idle_session_is_signed_out(client, app):
    login(client)
    with client.session_transaction() as s:
        s["_last_seen"] = int(time.time()) - (app.config["IDLE_TIMEOUT_MINUTES"] + 5) * 60
    assert client.get("/admin/").status_code in (302, 401)


# --- login ------------------------------------------------------------------------------------------

def test_unknown_email_and_wrong_password_are_indistinguishable(client):
    a = client.post("/admin/login", data={"email": "nobody@test.dev", "password": "x" * 14})
    b = client.post("/admin/login", data={"email": "owner@test.dev", "password": "x" * 14})
    assert a.status_code == b.status_code == 200
    strip = lambda r: re.sub(rb'(name="csrf_token"[^>]*>|value="[^"]*")', b"", r.data)
    assert strip(a) == strip(b)


def test_login_is_rate_limited_per_ip():
    class Limited(TestConfig):
        RATELIMIT_ENABLED = True
    from app.security import limiter
    app = create_app(Limited)
    with app.app_context():
        db.create_all()
        limiter.reset()
        client = app.test_client()
        codes = [client.post("/admin/login", data={"email": "x@test.dev", "password": "y" * 14}).status_code for _ in range(25)]
        assert 429 in codes and codes[0] == 200
        limiter.reset()
        db.drop_all()


# --- password policy --------------------------------------------------------------------------------

@pytest.mark.parametrize("password", ["short", "password1234", "aaaaaaaaaaaa", "abcdefghijkl", "parkpilot123"])
def test_weak_passwords_are_rejected(password):
    assert validate_password(password)


def test_password_may_not_contain_the_email_or_name():
    assert validate_password("jane.doe-2024-secret", email="jane.doe@example.com")
    assert validate_password("my-name-is-sultan-123", name="Ghatfan Sultan")


def test_a_long_passphrase_is_accepted():
    assert validate_password("purple-tractor-sings-quietly") == []


def test_team_form_enforces_the_policy(client):
    login(client)
    r = client.post("/admin/team", data={"name": "Sam", "email": "sam@test.dev", "password": "password1234", "role": "staff"})
    assert b"too common" in r.data
    assert User.query.filter_by(email="sam@test.dev").first() is None


# --- webhooks / SSRF --------------------------------------------------------------------------------

def test_webhook_host_resolving_to_a_private_address_is_refused(monkeypatch):
    from app.services import api_platform as ap
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ap.ApiPlatformError):
        ap.resolve_public_webhook_host("rebind.example.com")


def test_webhook_mixed_public_and_private_answers_are_refused(monkeypatch):
    from app.services import api_platform as ap
    answers = [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("169.254.169.254", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(ap.ApiPlatformError):
        ap.resolve_public_webhook_host("mixed.example.com")


def test_pinned_connection_revalidates_at_connect_time(monkeypatch):
    """DNS rebinding: the name looked public when the endpoint was saved but now points at localhost."""
    from app.services import api_platform as ap
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    conn = ap._PinnedHTTPSConnection("rebind.example.com", 443, timeout=1)
    with pytest.raises(ap.ApiPlatformError):
        conn.connect()


# --- staff least privilege --------------------------------------------------------------------------

def test_staff_alerts_hide_customer_names_and_amounts(client, app):
    from app.models import Alert, Tenant
    from app.tenancy import use_tenant
    with app.app_context():
        tenant = Tenant.query.first()
        staff = User(tenant_id=tenant.id, email="staff@test.dev", name="Staff", role=Role.STAFF)
        staff.set_password("staff-passphrase-123")
        db.session.add(staff)
        with use_tenant(tenant.id):
            db.session.add(Alert(tenant_id=tenant.id, kind="booking_created", data={"ref": "AAAA-BBBB-CCCC", "customer": "Casey Jones", "total": "99.00 EUR"}))
            db.session.add(Alert(tenant_id=tenant.id, kind="booking_paid", data={"ref": "AAAA-BBBB-CCCC", "customer": "Casey Jones", "total": "99.00 EUR"}))
        db.session.commit()
    login(client, "staff@test.dev", "staff-passphrase-123")
    body = client.get("/staff/").data
    assert b"AAAA-BBBB-CCCC" in body
    assert b"Casey" not in body and b"99.00" not in body and b"Payment received" not in body
