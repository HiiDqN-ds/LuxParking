"""Idempotent seed: one tenant (your lot at Duesseldorf Airport) with 50 editable spaces."""
import os
import secrets

from .extensions import db
from .models import PriceRule, Lot, Role, Space, Tenant, User
from .tenancy import use_tenant
from .utils import grid_name, grid_position, slugify

SPACE_COUNT = 50


def _seed_phone(var: str):
    """Optional phone number (for the SMS password-reset code) from an environment variable, as +<country><number>."""
    from .services.messaging import normalize_phone

    raw = os.environ.get(var, "").strip()
    return normalize_phone(raw) if raw else None


def seed_demo(tenant_name=None, owner_email=None, owner_password=None, spaces=SPACE_COUNT):
    """Create tenant + owner + lot + `spaces` spaces if the tenant doesn't exist yet.

    Returns (tenant, generated_password_or_None). Names/emails are placeholders: change them
    via env vars (SEED_TENANT_NAME, SEED_OWNER_EMAIL, ADMIN_PASSWORD) or in the admin UI.
    """
    tenant_name = tenant_name or os.environ.get("SEED_TENANT_NAME", "Duesseldorf Airport Parking")
    owner_email = (owner_email or os.environ.get("SEED_OWNER_EMAIL", "owner@example.com")).lower()
    slug = slugify(tenant_name)

    with use_tenant(None):
        existing = Tenant.query.filter_by(slug=slug).first()
    if existing:
        return existing, None

    password = owner_password or os.environ.get("ADMIN_PASSWORD")
    generated = None
    if not password:
        password = generated = secrets.token_urlsafe(18)
    elif os.environ.get("APP_ENV") == "production":
        from .password_policy import validate_password

        problems = validate_password(password, email=owner_email)
        if problems:
            raise SystemExit("ADMIN_PASSWORD is not acceptable for production: " + " ".join(problems))

    tenant = Tenant(
        name=tenant_name, slug=slug, default_locale="de", timezone="Europe/Berlin", currency="EUR",
        primary_color=os.environ.get("SEED_PRIMARY_COLOR", "#14385C"), accent_color=os.environ.get("SEED_ACCENT_COLOR", "#C9A24B"),  # Luxury Parking navy + gold
        billing_exempt=True,  # the operator's own parking is never limited or charged by the platform
    )
    db.session.add(tenant)
    db.session.flush()

    with use_tenant(tenant.id):
        owner = User(tenant_id=tenant.id, email=owner_email, name="Owner", role=Role.OWNER, phone=_seed_phone("SEED_OWNER_PHONE"))
        owner.set_password(password)
        db.session.add(owner)
        lot = Lot(
            tenant_id=tenant.id, name=os.environ.get("SEED_LOT_NAME", "Luxury Parking Ratingen"), slug=os.environ.get("SEED_LOT_SLUG", "ratingen"),
            address=os.environ.get("SEED_LOT_ADDRESS", "Sohlstättenstraße 58, 40880 Ratingen"), city=os.environ.get("SEED_LOT_CITY", "Ratingen"),
            lat=51.2895, lng=6.7668,  # Duesseldorf Airport coordinates as a starting point: edit to your lot in the admin
        )
        db.session.add(lot)
        db.session.flush()
        day_rate = os.environ.get("SEED_DAY_RATE")  # e.g. 9.90: starting daily price so a fresh deployment can sell at once
        if day_rate:
            db.session.add(PriceRule(tenant_id=tenant.id, lot_id=lot.id, name="Base pricing", period="day", mode="per_day", amount=day_rate, priority=100))
        for i in range(spaces):
            db.session.add(Space(tenant_id=tenant.id, lot_id=lot.id, name=grid_name(i), **grid_position(i)))
    db.session.commit()
    return tenant, generated


def ensure_platform_admin():
    """Create the super-user (platform admin) from SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD if both are set."""
    email, password = os.environ.get("SEED_ADMIN_EMAIL", "").strip().lower(), os.environ.get("SEED_ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    if User.query.filter_by(email=email).first():
        return email
    if os.environ.get("APP_ENV") == "production":
        from .password_policy import validate_password

        problems = validate_password(password, email=email)
        if problems:
            raise SystemExit("SEED_ADMIN_PASSWORD is not acceptable for production: " + " ".join(problems))
    admin = User(tenant_id=None, email=email, name="Platform admin", role=Role.PLATFORM_OWNER, phone=_seed_phone("SEED_ADMIN_PHONE"))
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return email


def sync_dev_passwords():
    """Local development only: make the logins match .env (SEED_SYNC_PASSWORDS=1).

    A parking (tenant) has exactly ONE owner plus that owner's own staff. The normal seed never touches an
    existing database, so after editing .env an old database would keep the old owner email/password and
    even end up with two owners. This (re)applies the owner and platform-admin logins from .env on every
    start, turns a leftover old owner into the .env owner (or removes extra owners) and lifts lockouts.
    Never runs in production.
    """
    if os.environ.get("APP_ENV") == "production" or os.environ.get("SEED_SYNC_PASSWORDS") != "1":
        return []
    import hashlib
    from pathlib import Path

    from flask import current_app
    from sqlalchemy import update

    # Apply the .env logins only when they changed since the last start. Otherwise every restart would undo what people
    # edit themselves under "My profile" (email, password, ...).
    fingerprint = hashlib.sha256("|".join(os.environ.get(key, "") for key in (
        "SEED_TENANT_NAME", "SEED_OWNER_EMAIL", "ADMIN_PASSWORD", "SEED_ADMIN_EMAIL", "SEED_ADMIN_PASSWORD",
        "SEED_OWNER_PHONE", "SEED_ADMIN_PHONE", "SEED_LOT_SLUG")).encode("utf-8")).hexdigest()
    state_file = Path(current_app.instance_path) / "seed_sync"
    try:
        if state_file.read_text(encoding="utf-8").strip() == fingerprint:
            return []
    except OSError:
        pass

    from .models import ApiAccessRequest, AuditLog, BookingExtra, Inspection, InspectionPhoto, Tenant

    done = []
    owner_email = os.environ.get("SEED_OWNER_EMAIL", "").strip().lower()
    owner_pw = os.environ.get("ADMIN_PASSWORD", "")
    with use_tenant(None):
        tenant = Tenant.query.filter_by(slug=slugify(os.environ.get("SEED_TENANT_NAME", "Duesseldorf Airport Parking"))).first()
        if owner_email and owner_pw and tenant is not None:
            owners = User.query.filter_by(tenant_id=tenant.id, role=Role.OWNER).order_by(User.id).all()
            wanted = next((u for u in owners if u.email == owner_email), None)
            if wanted is None and User.query.filter_by(email=owner_email).first() is None:
                if owners:  # old database with another owner email: that same owner now signs in with the .env email
                    wanted = owners[0]
                    wanted.email = owner_email
                else:
                    wanted = User(tenant_id=tenant.id, email=owner_email, name="Owner", role=Role.OWNER)
                    db.session.add(wanted)
            if wanted is not None:
                wanted.set_password(owner_pw)
                if _seed_phone("SEED_OWNER_PHONE"):
                    wanted.phone = _seed_phone("SEED_OWNER_PHONE")
                wanted.active = True
                wanted.failed_logins, wanted.locked_until = 0, None
                done.append(owner_email)
                db.session.flush()
                for extra in [u for u in owners if u.id != wanted.id]:  # one owner per parking
                    try:
                        with db.session.begin_nested():
                            for model, column in (
                                (Inspection, "staff_user_id"), (InspectionPhoto, "staff_user_id"), (BookingExtra, "done_by_id"),
                                (AuditLog, "user_id"), (ApiAccessRequest, "requested_by_id"), (ApiAccessRequest, "decided_by_id"),
                            ):
                                db.session.execute(update(model).where(getattr(model, column) == extra.id).values({column: None}))
                            db.session.delete(extra)
                    except Exception:  # still referenced somewhere: at least switch it off
                        extra.active = False
        admin_email = os.environ.get("SEED_ADMIN_EMAIL", "").strip().lower()
        admin_pw = os.environ.get("SEED_ADMIN_PASSWORD", "")
        if admin_email and admin_pw:
            user = User.query.filter_by(email=admin_email).first()
            if user is not None:
                user.set_password(admin_pw)
                if _seed_phone("SEED_ADMIN_PHONE"):
                    user.phone = _seed_phone("SEED_ADMIN_PHONE")
                user.active = True
                user.failed_logins, user.locked_until = 0, None
                done.append(admin_email)
    lot_slug = os.environ.get("SEED_LOT_SLUG", "").strip()
    if lot_slug and tenant is not None:  # keep the booking link in sync with .env when the tenant has a single lot
        with use_tenant(tenant.id):
            lots = Lot.query.all()
            if len(lots) == 1 and lots[0].slug != lot_slug:
                lots[0].slug = lot_slug
    if done:  # lift any "too many attempts" block from earlier wrong tries
        with use_tenant(None):
            db.session.query(AuditLog).filter(AuditLog.action == "login_failed").delete()
    db.session.commit()
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(fingerprint, encoding="utf-8")
    except OSError:
        pass
    return done


DEFAULT_EXTRAS = (
    ("shuttle", "Shuttle Standard", "Hin- und Rückfahrt zum Terminal, pro Person, max. 2 Personen inkl. Gepäck · Round trip to the terminal, per person, max. 2 persons incl. luggage", "10.00"),
    ("shuttle", "Shuttle Priority", "Direkt, ohne Wartezeit · Direct, no waiting", "24.00"),
    ("cleaning", "Außenwäsche · Exterior wash", "Handwäsche, während Sie reisen · Hand wash while you travel", "19.00"),
    ("cleaning", "Innenreinigung · Interior clean", "Saugen, Armaturen, Scheiben innen · Vacuum, dashboard, inside glass", "29.00"),
    ("cleaning", "Komplettpflege · Full valet", "Innen und außen, bereit bei Ihrer Rückkehr · Inside and out, ready when you return", "45.00"),
)


def ensure_default_extras() -> int:
    """Give every lot without extras a starting set of shuttle and cleaning options (prices are editable in the admin)."""
    from .models import Extra, Lot, Tenant
    from .tenancy import use_tenant

    if os.environ.get("SEED_EXTRAS", "1") != "1":
        return 0
    created = 0
    for tenant in Tenant.query.all():
        with use_tenant(tenant.id):
            for lot in Lot.query.all():
                if Extra.query.filter_by(lot_id=lot.id).count():
                    continue
                for kind, name, description, price in DEFAULT_EXTRAS:
                    db.session.add(Extra(tenant_id=tenant.id, lot_id=lot.id, kind=kind, name=name, description=description, price=price, per_person=(kind == "shuttle"), included_persons=0, max_persons=(2 if kind == "shuttle" else None)))
                    created += 1
    db.session.commit()
    return created
