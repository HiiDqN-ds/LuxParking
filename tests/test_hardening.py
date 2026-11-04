"""Security and robustness: login lockout, headers, error pages, translation parity, DB double-booking guard."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Formatter

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.config import Config, TestConfig
from app.extensions import db
from app.models import AuditLog, Booking, Lot, Space, Tenant, User, utcnow
from app.tenancy import use_tenant
from conftest import login

BAD = {"email": "owner@test.dev", "password": "wrong-password"}


# --- login brute-force protection ---------------------------------------------------------

def test_account_locks_after_repeated_failures_and_even_the_right_password_is_refused(client, app):
    for _ in range(5):
        assert client.post("/admin/login", data=BAD).status_code == 200
    r = login(client)  # correct password, but the account is locked
    # A locked account answers exactly like a wrong password: the form must not reveal which emails have accounts.
    assert r.status_code == 200 and b"Email or password is incorrect" in r.data
    assert b"Too many failed attempts" not in r.data
    assert client.get("/admin/").status_code in (302, 401)  # still not signed in
    assert b"Dashboard" not in client.get("/admin/").data


def test_lock_expires_and_success_resets_the_counter(client, app):
    for _ in range(5):
        client.post("/admin/login", data=BAD)
    with use_tenant(None):
        user = User.query.filter_by(email="owner@test.dev").one()
        user.locked_until = utcnow() - timedelta(minutes=1)
        db.session.commit()
    assert login(client).status_code == 302
    with use_tenant(None):
        user = User.query.filter_by(email="owner@test.dev").one()
        assert user.failed_logins == 0 and user.locked_until is None


def test_a_few_typos_do_not_lock_the_account(client, app):
    for _ in range(4):
        client.post("/admin/login", data=BAD)
    assert login(client).status_code == 302


def test_unknown_emails_are_throttled_per_ip_without_revealing_anything(app):
    app.config["LOGIN_IP_MAX_FAILURES"] = 3
    client = app.test_client()
    for i in range(3):
        r = client.post("/admin/login", data={"email": f"nobody{i}@test.dev", "password": "x" * 12})
        assert r.status_code == 200 and b"Email or password is incorrect" in r.data
    r = client.post("/admin/login", data={"email": "nobody9@test.dev", "password": "x" * 12})
    assert r.status_code == 429
    with use_tenant(None):
        assert AuditLog.query.filter_by(action="login_failed").count() == 3


def test_wrong_password_and_unknown_email_give_the_same_message(client, app):
    a = client.post("/admin/login", data=BAD).data
    b = client.post("/admin/login", data={"email": "ghost@test.dev", "password": "wrong-password"}).data
    assert b"Email or password is incorrect" in a and b"Email or password is incorrect" in b


# --- headers & errors ----------------------------------------------------------------------

def test_security_headers_and_no_store_for_back_office(client, app):
    r = client.get("/admin/login")
    assert r.headers["X-Frame-Options"] == "DENY" and "camera=(self)" in r.headers["Permissions-Policy"]
    assert r.headers["Cache-Control"] == "no-store"          # /admin/* is never cached
    assert "Cache-Control" not in client.get("/healthz").headers or "no-store" not in client.get("/healthz").headers["Cache-Control"]
    assert "Strict-Transport-Security" not in r.headers      # only in production


def test_hsts_is_sent_in_production(monkeypatch):
    class Prod(TestConfig):
        APP_ENV = "production"
        SECRET_KEY = "not-the-dev-secret-but-long-enough-0123456789"
    app = create_app(Prod)
    with app.app_context():
        db.create_all()
        r = app.test_client().get("/healthz")
        assert "max-age=63072000" in r.headers["Strict-Transport-Security"]
        db.drop_all()


def test_production_refuses_a_short_secret_key():
    class Prod(TestConfig):
        APP_ENV = "production"
        SECRET_KEY = "too-short"
    with pytest.raises(RuntimeError, match="at least 32"):
        create_app(Prod)


def test_error_pages_are_specific_and_api_errors_are_json(client):
    r = client.post("/healthz")
    assert r.status_code == 405 and b"Method not allowed" in r.data
    r = client.get("/v1/this-does-not-exist")
    assert r.status_code == 404 and r.get_json()["error"]["code"] == "not_found"
    r = client.post("/v1/quotes", data="x")  # wrong method/auth must still be JSON, never an HTML page
    assert r.is_json


def test_unhandled_error_returns_a_clean_500_and_rolls_back(app):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.testing = False

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    r = app.test_client().get("/boom")
    assert r.status_code == 500 and b"Something went wrong" in r.data and b"secret internal detail" not in r.data


# --- translations --------------------------------------------------------------------------

def _catalogs():
    folder = Path(__file__).resolve().parents[1] / "app" / "translations"
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))}


def test_every_locale_has_exactly_the_same_keys():
    cats = _catalogs()
    assert set(cats) >= {"de", "en", "nl", "fr", "es", "ar"}
    reference = set(cats["en"])
    for locale, cat in cats.items():
        assert set(cat) == reference, f"{locale}: missing {sorted(reference - set(cat))[:5]} extra {sorted(set(cat) - reference)[:5]}"


def test_placeholders_match_across_locales_and_nothing_is_blank():
    cats = _catalogs()
    fields = lambda s: {f for _, f, _, _ in Formatter().parse(s) if f}
    for key, english in cats["en"].items():
        for locale, cat in cats.items():
            assert cat[key].strip(), f"{locale}:{key} is empty"
            assert fields(cat[key]) == fields(english), f"{locale}:{key} placeholders differ"


# --- PostgreSQL double-booking guard ---------------------------------------------------------

pg_only = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"),
                             reason="needs PostgreSQL (set TEST_DATABASE_URL)")


@pg_only
def test_database_itself_refuses_overlapping_active_bookings(app):
    from test_phase3 import booking_for, hold_for, parking

    # Schemas built by migrations already have the guard; create_all schemas get it here.
    has_guard = db.session.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'ex_bookings_no_space_overlap'")).first()
    if not has_guard:
        db.session.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        db.session.execute(text(
            "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_space_overlap EXCLUDE USING gist "
            "(space_id WITH =, tstzrange(pickup_at, return_at) WITH &&) WHERE (status IN ('reserved', 'checked_in'))"))
        db.session.commit()
    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot, space = Lot.query.first(), Space.query.first()
        ids = (tenant.id, lot.id, space.id)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    hold = hold_for(ids, start, start + timedelta(hours=24))
    first = booking_for(hold, ids)

    def raw(pickup, ret, status="reserved", ref="X"):
        return Booking(tenant_id=ids[0], lot_id=ids[1], space_id=ids[2], customer_id=first.customer_id, booking_ref=ref,
                       status=status, pickup_at=pickup, return_at=ret, subtotal_amount=0, total_amount=0)

    with use_tenant(tenant.id):
        db.session.add(raw(start + timedelta(hours=1), start + timedelta(hours=30), ref="OVERLAP"))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        # back-to-back is fine, and a cancelled booking never blocks the space
        db.session.add(raw(start + timedelta(hours=24), start + timedelta(hours=48), ref="NEXT"))
        db.session.add(raw(start, start + timedelta(hours=24), status="cancelled", ref="CANC"))
        db.session.flush()
        db.session.rollback()


@pg_only
def test_parallel_requests_for_the_same_space_produce_exactly_one_hold(app):
    """Eight customers press 'reserve' on the same space and dates at the same instant."""
    import threading

    from app.models import Hold
    from app.services.bookings import BookingConflict, create_hold

    tenant = Tenant.query.filter_by(slug="duesseldorf-airport-parking").one()
    with use_tenant(tenant.id):
        lot, space = Lot.query.first(), Space.query.first()
        tenant_id, lot_id, space_id = tenant.id, lot.id, space.id
    db.session.commit()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    outcomes, barrier = [], threading.Barrier(8)

    def worker(i):
        with app.app_context():
            try:
                barrier.wait()
                create_hold(tenant_id=tenant_id, lot_id=lot_id, space_id=space_id, pickup_at=start,
                            return_at=start + timedelta(hours=24), customer_email=f"c{i}@example.com")
                outcomes.append("held")
            except BookingConflict:
                outcomes.append("conflict")
            except Exception as exc:  # anything else is a bug the test must surface
                outcomes.append(repr(exc))
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["conflict"] * 7 + ["held"]
    with use_tenant(tenant_id):
        assert Hold.query.filter_by(space_id=space_id).count() == 1
