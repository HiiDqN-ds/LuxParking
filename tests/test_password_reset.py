"""Forgot password: emailed link and texted code."""
import re
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import PasswordReset, Role, User, utcnow

NEW_PASSWORD = "Harbor-Lamp-Cedar-58"
OWNER = "owner@test.dev"


@pytest.fixture()
def outbox(app, monkeypatch):
    """Capture every email and SMS the reset feature tries to send."""
    sent = {"email": [], "sms": []}
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="noreply@test.dev")
    monkeypatch.setattr("app.services.booking_notifications.smtp_send", lambda message: sent["email"].append(message))
    monkeypatch.setattr("app.services.messaging.send_message", lambda channel, to, body: sent["sms"].append((to, body)) or "SM1")
    return sent


def _owner() -> User:
    return User.query.filter_by(email=OWNER).one()


def _link_from(message) -> str:
    return re.search(r"https?://\S+/admin/reset/\S+", message.get_content()).group(0)


def _code_from(sms) -> str:
    return re.search(r"\b(\d{6})\b", sms[1]).group(1)


def _request(client, identifier=OWNER, method="email"):
    return client.post("/admin/forgot-password", data={"identifier": identifier, "method": method})


def _pw(password) -> bool:
    """Is `password` the owner's current password? (Checked directly: test requests share one app context, so a
    second HTTP login inside one test would see the first one's user.)"""
    db.session.expire_all()
    return _owner().check_password(password)


def test_pages_render_and_login_links_to_them(client):
    assert "/admin/forgot-password" in client.get("/admin/login").get_data(as_text=True)
    page = client.get("/admin/forgot-password")
    assert page.status_code == 200 and 'name="identifier"' in page.get_data(as_text=True)


def test_email_link_resets_the_password_once(app, client, outbox):
    response = _request(client)
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/login")
    assert len(outbox["email"]) == 1 and outbox["email"][0]["To"] == OWNER
    link = _link_from(outbox["email"][0])
    path = "/admin/reset/" + link.rsplit("/admin/reset/", 1)[1]

    assert client.get(path).status_code == 200
    done = client.post(path, data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert done.status_code == 302 and done.headers["Location"].endswith("/admin/login")
    assert _pw(NEW_PASSWORD)
    assert not _pw("correct-horse")
    assert client.get(path).status_code == 400  # a link works once
    assert client.post(path, data={"new_password": NEW_PASSWORD + "x", "confirm_password": NEW_PASSWORD + "x"}).status_code == 400


def test_link_rejects_tampering_expiry_and_weak_passwords(app, client, outbox):
    _request(client)
    link = _link_from(outbox["email"][0])
    token = link.rsplit("/admin/reset/", 1)[1]
    path = "/admin/reset/" + token
    assert client.get("/admin/reset/" + token[:-3] + "xyz").status_code == 400
    assert client.get("/admin/reset/not-a-token").status_code == 400
    # weak / mismatching passwords keep the link valid
    assert "do not match" in client.post(path, data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD + "1"}).get_data(as_text=True)
    client.post(path, data={"new_password": "short", "confirm_password": "short"})
    assert client.get(path).status_code == 200
    assert not _pw("short")
    # expired
    row = PasswordReset.query.one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert client.get(path).status_code == 400


def test_sms_code_flow(app, client, outbox):
    _owner().phone = "+4915112345678"
    db.session.commit()
    response = _request(client, method="sms")
    assert response.headers["Location"].endswith("/admin/reset-code")
    assert outbox["sms"] and outbox["sms"][0][0] == "+4915112345678"
    code = _code_from(outbox["sms"][0])
    wrong = "000000" if code != "000000" else "111111"

    bad = client.post("/admin/reset-code", data={"identifier": OWNER, "code": wrong, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert bad.status_code == 200 and not _pw(NEW_PASSWORD)
    ok = client.post("/admin/reset-code", data={"identifier": OWNER, "code": code, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert ok.status_code == 302 and ok.headers["Location"].endswith("/admin/login")
    assert _pw(NEW_PASSWORD)
    again = client.post("/admin/reset-code", data={"identifier": OWNER, "code": code, "new_password": NEW_PASSWORD + "z", "confirm_password": NEW_PASSWORD + "z"})
    assert again.status_code == 200 and not _pw(NEW_PASSWORD + "z")  # single use


def test_sms_code_by_phone_number_and_lockout_after_wrong_guesses(app, client, outbox):
    _owner().phone = "+4915112345678"
    db.session.commit()
    _request(client, identifier="+49 151 1234 5678", method="sms")
    assert len(outbox["sms"]) == 1
    code = _code_from(outbox["sms"][0])
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post("/admin/reset-code", data={"identifier": "+4915112345678", "code": wrong, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    dead = client.post("/admin/reset-code", data={"identifier": "+4915112345678", "code": code, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert dead.status_code == 200 and not _pw(NEW_PASSWORD)  # even the right code no longer works


def test_policy_failure_does_not_burn_the_code(app, client, outbox):
    _owner().phone = "+4915112345678"
    db.session.commit()
    _request(client, method="sms")
    code = _code_from(outbox["sms"][0])
    weak = "owner@test.dev-owner@test.dev"  # contains the email address
    client.post("/admin/reset-code", data={"identifier": OWNER, "code": code, "new_password": weak, "confirm_password": weak})
    assert PasswordReset.query.one().attempts == 0
    ok = client.post("/admin/reset-code", data={"identifier": OWNER, "code": code, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert ok.status_code == 302


def test_nothing_is_revealed_or_sent_for_unknown_inactive_or_phoneless_accounts(app, client, outbox, staff):
    unknown = _request(client, identifier="nobody@test.dev")
    known = _request(client)
    assert unknown.status_code == known.status_code == 302 and unknown.headers["Location"] == known.headers["Location"]
    assert len(outbox["email"]) == 1  # only the real account
    sms = _request(client, method="sms")  # owner has no phone saved
    assert sms.headers["Location"].endswith("/admin/reset-code") and outbox["sms"] == []
    staff.active = False
    db.session.commit()
    _request(client, identifier="staff@test.dev")
    assert len(outbox["email"]) == 1


def test_cooldown_and_newer_request_replaces_older_one(app, client, outbox):
    _request(client)
    _request(client)  # within the cooldown: ignored
    assert len(outbox["email"]) == 1
    first = PasswordReset.query.one()
    first.created_at = utcnow() - timedelta(minutes=5)
    db.session.commit()
    _request(client)
    assert len(outbox["email"]) == 2
    old_path = "/admin/reset/" + _link_from(outbox["email"][0]).rsplit("/admin/reset/", 1)[1]
    new_path = "/admin/reset/" + _link_from(outbox["email"][1]).rsplit("/admin/reset/", 1)[1]
    assert client.get(old_path).status_code == 400 and client.get(new_path).status_code == 200


def test_reset_signs_out_other_sessions_and_lifts_the_lockout(app, client, outbox):
    owner = _owner()
    session_id_before = owner.get_id()
    owner.failed_logins, owner.locked_until = 9, utcnow() + timedelta(minutes=10)
    db.session.commit()
    _request(client)
    path = "/admin/reset/" + _link_from(outbox["email"][0]).rsplit("/admin/reset/", 1)[1]
    client.post(path, data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    db.session.expire_all()
    owner = _owner()
    assert owner.get_id() != session_id_before  # sessions are bound to the password hash: every old session is dead
    assert owner.failed_logins == 0 and owner.locked_until is None


def test_phone_numbers_can_be_saved_by_owner_and_for_staff(app, client):
    from conftest import login
    login(client)
    assert client.post("/admin/account/profile", data={"name": "Boss", "phone": "+49 151 55886444"}).status_code == 302
    assert _owner().phone == "+4915155886444" and _owner().name == "Boss"
    assert "country code" in client.post("/admin/account/profile", data={"name": "Boss", "phone": "12345"}).get_data(as_text=True)
    client.post("/admin/team", data={"name": "Max", "email": "max@test.dev", "password": NEW_PASSWORD, "role": "staff", "phone": "+31 6 12345678"})
    assert User.query.filter_by(email="max@test.dev").one().phone == "+31612345678"
    assert client.post("/admin/account/profile", data={"name": "Boss", "phone": ""}).status_code == 302 and _owner().phone is None
