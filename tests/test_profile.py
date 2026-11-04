"""My profile: photo, name, phone, email, password."""
from io import BytesIO

import pytest
from PIL import Image

from app.extensions import db
from app.models import PasswordReset, Role, User, utcnow
from conftest import login

OWNER = "owner@test.dev"


def _image(fmt="PNG", size=(600, 400), color=(200, 30, 30), **save):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, fmt, **save)
    out.seek(0)
    return out


def _upload(client, data, name="me.png", **extra):
    return client.post("/admin/account/photo", data={"photo": (data, name), **extra}, content_type="multipart/form-data")


def _owner():
    db.session.expire_all()
    return User.query.filter_by(email=OWNER).one()


@pytest.fixture()
def owner_client(client):
    login(client)
    return client


def test_profile_page_renders_for_every_role(app, client, staff):
    login(client)
    html = client.get("/admin/account").get_data(as_text=True)
    for marker in ('id="photo"', 'id="profile"', 'id="email"', 'id="password"', "account.js"):
        assert marker in html
    client.post("/admin/logout")


def test_staff_gets_the_page_with_the_staff_layout(app, staff):
    client = app.test_client()
    login(client, "staff@test.dev", "staff-pass")
    page = client.get("/admin/account")
    assert page.status_code == 200 and 'id="photo-form"' in page.get_data(as_text=True)


def test_old_password_url_redirects_to_the_profile_page(owner_client):
    response = owner_client.get("/admin/account/password")
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/account#password")


def test_name_and_phone_are_saved_and_validated(owner_client):
    done = owner_client.post("/admin/account/profile", data={"name": "  Anna Boss ", "phone": "+49 151 2345 6789"})
    assert done.status_code == 302
    assert (_owner().name, _owner().phone) == ("Anna Boss", "+4915123456789")
    bad = owner_client.post("/admin/account/profile", data={"name": "A" * 200, "phone": "12"})
    assert bad.status_code == 200 and "country code" in bad.get_data(as_text=True)
    assert _owner().name == "Anna Boss"


# --- email ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def mails(app, monkeypatch):
    sent = []
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="noreply@test.dev")
    monkeypatch.setattr("app.services.booking_notifications.smtp_send", lambda message: sent.append(message))
    return sent


def test_email_change_needs_the_password_and_notifies_the_old_address(owner_client, mails):
    wrong = owner_client.post("/admin/account/email", data={"new_email": "new@test.dev", "current_password": "nope-nope-nope"})
    assert "current password is incorrect" in wrong.get_data(as_text=True) and _owner().email == OWNER and not mails
    db.session.add(PasswordReset(user_id=_owner().id, channel="email", secret_hash="x" * 64, expires_at=utcnow()))
    db.session.commit()
    ok = owner_client.post("/admin/account/email", data={"new_email": "  New.Owner@Test.dev ", "current_password": "correct-horse"})
    assert ok.status_code == 302
    db.session.expire_all()
    user = User.query.filter_by(email="new.owner@test.dev").one()
    assert user.role == Role.OWNER and user.check_password("correct-horse")
    assert len(mails) == 1 and mails[0]["To"] == OWNER and "new.owner@test.dev" in mails[0].get_content()
    assert PasswordReset.query.filter_by(user_id=user.id, used_at=None).count() == 0


def test_email_change_rejects_taken_same_and_invalid_addresses(owner_client, staff, mails):
    for value, message in (("staff@test.dev", "already used"), (OWNER, "already your email"), ("not-an-email", "")):
        page = owner_client.post("/admin/account/email", data={"new_email": value, "current_password": "correct-horse"})
        assert page.status_code == 200 and (message in page.get_data(as_text=True))
    assert _owner().email == OWNER and not mails


# --- photo ---------------------------------------------------------------------------------------------------

def test_photo_is_cropped_to_a_small_square_webp(owner_client):
    assert _upload(owner_client, _image(size=(900, 300))).status_code == 302
    owner = _owner()
    assert owner.avatar_updated_at is not None and owner.avatar_mime == "image/webp"
    stored = Image.open(BytesIO(owner.avatar_data))
    assert stored.format == "WEBP" and stored.size == (256, 256)


def test_photo_metadata_is_stripped(owner_client):
    source = BytesIO()
    image = Image.new("RGB", (400, 400), (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    image.save(source, "JPEG", exif=exif)
    source.seek(0)
    _upload(owner_client, source, "cam.jpg")
    data = _owner().avatar_data
    assert b"SecretCameraMaker" not in data and not dict(Image.open(BytesIO(data)).getexif())


def test_transparent_png_gets_a_white_background(owner_client):
    out = BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(out, "PNG")
    out.seek(0)
    _upload(owner_client, out)
    assert Image.open(BytesIO(_owner().avatar_data)).convert("RGB").getpixel((128, 128)) == (255, 255, 255)


@pytest.mark.parametrize("payload,name,expected", [
    (b"", "empty.png", "Choose a photo"),
    (b"this is not an image at all", "fake.png", "not a valid image"),
    (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;", "pic.gif", "Only PNG, JPEG and WebP"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "x.svg", "not a valid image"),
    (b"\x89PNG\r\n\x1a\n" + b"0" * (3_100_000), "huge.png", "larger than 3 MB"),
])
def test_bad_uploads_are_refused_and_change_nothing(owner_client, payload, name, expected):
    response = _upload(owner_client, BytesIO(payload), name)
    assert response.status_code == 302
    page = owner_client.get("/admin/account").get_data(as_text=True)
    assert expected in page and _owner().avatar_updated_at is None


def test_pixel_bomb_is_refused(owner_client):
    out = BytesIO()
    Image.new("L", (6000, 5000), 0).save(out, "PNG", optimize=True)
    assert out.tell() < 3_000_000
    out.seek(0)
    _upload(owner_client, out)
    assert "too many pixels" in owner_client.get("/admin/account").get_data(as_text=True) and _owner().avatar_updated_at is None


def test_photo_can_be_removed(owner_client):
    _upload(owner_client, _image())
    assert owner_client.post("/admin/account/photo", data={"remove": "1"}).status_code == 302
    owner = _owner()
    assert owner.avatar_data is None and owner.avatar_updated_at is None


def test_header_and_team_list_show_the_photo(owner_client, staff):
    _upload(owner_client, _image())
    owner = _owner()
    assert f"/admin/avatar/{owner.id}" in owner_client.get("/admin/").get_data(as_text=True)
    assert f"/admin/avatar/{owner.id}" in owner_client.get("/admin/team").get_data(as_text=True)


# --- who may fetch a photo -----------------------------------------------------------------------------------

def _give_owner_a_photo():
    from app.services.avatar import process_avatar
    owner = _owner()
    owner.avatar_data, owner.avatar_mime = process_avatar(_image())
    owner.avatar_updated_at = utcnow()
    db.session.commit()
    return owner.id


# (one login per test: test requests share one app context, so a second login would still see the first user)

def test_photo_owner_can_fetch_it_and_it_is_cached_privately(app, client):
    owner_id = _give_owner_a_photo()
    login(client)
    own = client.get(f"/admin/avatar/{owner_id}")
    assert own.status_code == 200 and own.mimetype == "image/webp"
    assert own.headers["Cache-Control"].startswith("private") and "no-store" not in own.headers["Cache-Control"]
    assert client.get("/admin/avatar/999999").status_code == 404


def test_photo_needs_a_login(app, client):
    owner_id = _give_owner_a_photo()
    anonymous = client.get(f"/admin/avatar/{owner_id}")
    assert anonymous.status_code == 302 and "/admin/login" in anonymous.headers["Location"]


def test_colleague_in_the_same_parking_can_see_the_photo(app, client, staff):
    owner_id = _give_owner_a_photo()
    login(client, "staff@test.dev", "staff-pass")
    assert client.get(f"/admin/avatar/{owner_id}").status_code == 200


def test_another_parking_cannot_see_the_photo(app, client, other_tenant):
    owner_id = _give_owner_a_photo()
    stranger = User(tenant_id=other_tenant[0].id, email="x@other.dev", role=Role.OWNER)
    stranger.set_password("stranger-pass-123")
    db.session.add(stranger)
    db.session.commit()
    login(client, "x@other.dev", "stranger-pass-123")
    assert client.get(f"/admin/avatar/{owner_id}").status_code == 404


def test_platform_admin_can_see_any_photo(app, client):
    owner_id = _give_owner_a_photo()
    admin = User(tenant_id=None, email="root@test.dev", role=Role.PLATFORM_OWNER)
    admin.set_password("platform-pass-123")
    db.session.add(admin)
    db.session.commit()
    login(client, "root@test.dev", "platform-pass-123")
    assert client.get(f"/admin/avatar/{owner_id}").status_code == 200


def test_password_change_still_works_from_the_profile_page(owner_client):
    done = owner_client.post("/admin/account/password", data={
        "current_password": "correct-horse", "new_password": "Harbor-Lamp-Cedar-58", "confirm_password": "Harbor-Lamp-Cedar-58"})
    assert done.status_code == 302 and done.headers["Location"].endswith("/admin/account#password")
    assert _owner().check_password("Harbor-Lamp-Cedar-58")


def test_platform_admin_gets_the_page(app, client):
    admin = User(tenant_id=None, email="root@test.dev", name="Root", role=Role.PLATFORM_OWNER)
    admin.set_password("platform-pass-123")
    db.session.add(admin)
    db.session.commit()
    login(client, "root@test.dev", "platform-pass-123")
    page = client.get("/admin/account")
    assert page.status_code == 200 and 'id="photo-form"' in page.get_data(as_text=True)
