"""Data model for tenants, parking, pricing, bookings, payments, and audit events.

TENANT ISOLATION
Every tenant-owned table has a tenant_id. Isolation is enforced in two places:
  1. Reads: a SQLAlchemy `do_orm_execute` hook adds `WHERE tenant_id = <current>`
     to every ORM SELECT on a scoped model, so a forgotten filter cannot leak data.
  2. Writes: `tenant_id` defaults to the current tenant and refuses to default to
     nothing, so rows can never be created without an owner.
The current tenant lives in `flask.g.tenant_id` (set per request after login,
or via `tenancy.use_tenant()` in CLI/tests). `None` means "unscoped" and is only
legitimate for the platform owner and for system tasks.

Bulk UPDATE/DELETE statements are NOT auto-scoped; always filter them by tenant_id.
Every tenant-owned model must be added to SCOPED.
"""
import math
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app, g, has_app_context
from flask_login import UserMixin
from itsdangerous import URLSafeSerializer
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, event, orm
from sqlalchemy.orm import declared_attr, deferred, with_loader_criteria
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- tenant context ---------------------------------------------------------

def current_tenant_id():
    return g.get("tenant_id") if has_app_context() else None


def _require_tenant():
    tid = current_tenant_id()
    if tid is None:
        raise RuntimeError(
            "No tenant context: set g.tenant_id (tenancy.use_tenant) or pass tenant_id explicitly."
        )
    return tid


class TenantMixin:
    """Adds a mandatory, indexed tenant_id that defaults to the current tenant."""

    @declared_attr
    def tenant_id(cls):
        return db.Column(
            db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False, index=True, default=_require_tenant,
        )


# --- constants --------------------------------------------------------------

class Role:
    PLATFORM_OWNER = "platform_owner"  # you: no tenant, sees everything (phase 8)
    OWNER = "owner"                    # lot operator: full control of own tenant
    STAFF = "staff"                    # read-only for now; check-in/out in phase 5
    ALL = (PLATFORM_OWNER, OWNER, STAFF)


SPACE_KINDS = ("small", "suv", "van")
# active = bookable (green), optional = bookable extra (blue), blocked = never bookable (grey)
SPACE_STATUSES = ("active", "optional", "blocked")
PRICE_MODES = ("flat", "per_day", "per_hour")
RULE_PERIODS = ("day", "weekend", "season", "holiday")


def _in(col: str, values) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def money(value) -> Decimal:
    """Normalise arbitrary numeric inputs to a 2-decimal EUR amount."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- models -----------------------------------------------------------------

class Tenant(db.Model):
    __tablename__ = "tenants"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default="active")  # billing comes in phase 9
    default_locale = db.Column(db.String(10), nullable=False, default="de")
    timezone = db.Column(db.String(64), nullable=False, default="Europe/Berlin")
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    logo_url = db.Column(db.String(500))
    primary_color = db.Column(db.String(7), nullable=False, default="#0F4C5C")
    accent_color = db.Column(db.String(7), nullable=False, default="#E09F1F")
    custom_domain = db.Column(db.String(253))
    inspection_retention_days = db.Column(
        db.Integer,
        nullable=False,
        default=90,
        server_default="90",
    )
    inspection_required_photos = db.Column(
        db.JSON,
        nullable=False,
        default=lambda: [
            "front",
            "back",
            "right",
            "left",
            "corner_front_left",
            "corner_front_right",
            "corner_back_left",
            "corner_back_right",
            "roof",
            "windshield",
            "odometer",
            "fuel_level",
        ],
        server_default='["front","back","right","left","corner_front_left","corner_front_right","corner_back_left","corner_back_right","roof","windshield","odometer","fuel_level"]',
    )
    # Phase 9/invoices. billing_exempt tenants (the operator's own parking) are never limited or charged by the platform.
    billing_exempt = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    vat_rate = db.Column(db.Numeric(5, 2))          # percent included in the gross prices, e.g. 19.00; NULL = use DEFAULT_VAT_RATE
    vat_id = db.Column(db.String(40))
    invoice_seller = db.Column(db.Text)             # multi-line seller block printed on invoices
    invoice_seq = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    credit_seq = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Uploaded logo (re-encoded PNG, small) used on invoices and key tags. logo_url stays the public web logo.
    logo_data = db.Column(db.LargeBinary)
    logo_mime = db.Column(db.String(40))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint(_in("status", ("trial", "active", "suspended")), name="ck_tenant_status"),
        CheckConstraint("inspection_retention_days > 0", name="ck_tenant_inspection_retention_days"),
        CheckConstraint("length(primary_color) = 7", name="ck_tenant_primary_color_length"),
        CheckConstraint("length(accent_color) = 7", name="ck_tenant_accent_color_length"),
        UniqueConstraint("custom_domain", name="uq_tenant_custom_domain"),
    )


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), index=True)  # NULL = platform owner
    email = db.Column(db.String(255), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.STAFF)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime(timezone=True))
    # Brute-force protection: consecutive wrong passwords, and a temporary lock after too many.
    failed_logins = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    locked_until = db.Column(db.DateTime(timezone=True))
    alerts_seen_at = db.Column(db.DateTime(timezone=True))  # notification centre: alerts newer than this are unread
    phone = db.Column(db.String(32))  # E.164 (+4915112345678): receives the password-reset code by SMS
    # Profile photo: re-encoded 256x256 WebP, loaded only when the image itself is requested (deferred).
    avatar_data = deferred(db.Column(db.LargeBinary))
    avatar_mime = db.Column(db.String(40))
    avatar_updated_at = db.Column(db.DateTime(timezone=True))

    tenant = db.relationship("Tenant")

    __table_args__ = (
        CheckConstraint(_in("role", Role.ALL), name="ck_user_role"),
        CheckConstraint("role = 'platform_owner' OR tenant_id IS NOT NULL", name="ck_user_tenant_required"),
    )

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def get_id(self) -> str:
        """Flask-Login session identifier. It embeds a fingerprint of the password hash, so changing or resetting
        a password immediately invalidates every session that was opened with the old one."""
        return f"{self.id}:{self.session_fingerprint()}"

    def session_fingerprint(self) -> str:
        import hashlib
        import hmac

        from flask import current_app

        key = current_app.config["SECRET_KEY"].encode("utf-8")
        return hmac.new(key, (self.password_hash or "").encode("utf-8"), hashlib.sha256).hexdigest()[:20]

    @property
    def is_active(self):  # Flask-Login hook: deactivated users cannot log in
        return self.active

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER

    @property
    def tenant_currency(self):
        tenant = self.tenant
        if tenant is None:
            return "EUR"
        return tenant.currency


class PasswordReset(db.Model):
    """One forgot-password request: an emailed link token or a texted 6-digit code. Only a keyed hash of the secret
    is stored. It expires, is single-use, and a code locks after a few wrong guesses. Not tenant-scoped: the person is
    not signed in yet (and platform admins have no tenant)."""
    __tablename__ = "password_resets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = db.Column(db.String(10), nullable=False)  # "email" (link) or "sms" (code)
    secret_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (CheckConstraint(_in("channel", ("email", "sms")), name="ck_password_reset_channel"),)


class Lot(TenantMixin, db.Model):
    """A physical parking lot. `canvas_*` is the SVG coordinate space the spaces are drawn in."""
    __tablename__ = "lots"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), nullable=False)
    address = db.Column(db.String(255), nullable=False, default="")
    city = db.Column(db.String(80), nullable=False, default="")
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    canvas_w = db.Column(db.Integer, nullable=False, default=1000)
    canvas_h = db.Column(db.Integer, nullable=False, default=700)
    background_url = db.Column(db.String(500))  # aerial photo / lot image (upload comes with S3 storage)
    active = db.Column(db.Boolean, nullable=False, default=True)
    base_daily_rate = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    spaces = db.relationship("Space", back_populates="lot", cascade="all, delete-orphan", order_by="Space.id")
    price_rules = db.relationship("PriceRule", back_populates="lot", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_lot_tenant_slug"),
        CheckConstraint("base_daily_rate >= 0", name="ck_lot_base_daily_rate"),
    )


class Space(TenantMixin, db.Model):
    __tablename__ = "spaces"
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(30), nullable=False)
    # Rectangle on the lot's SVG canvas (polygons can come later without changing the UI contract).
    x = db.Column(db.Integer, nullable=False, default=0)
    y = db.Column(db.Integer, nullable=False, default=0)
    w = db.Column(db.Integer, nullable=False, default=80)
    h = db.Column(db.Integer, nullable=False, default=110)
    kind = db.Column(db.String(10), nullable=False, default="small")
    covered = db.Column(db.Boolean, nullable=False, default=False)
    ev_charging = db.Column(db.Boolean, nullable=False, default=False)
    bus_distance_m = db.Column(db.Integer)
    photo_url = db.Column(db.String(500))
    status = db.Column(db.String(10), nullable=False, default="active")
    notes = db.Column(db.Text, nullable=False, default="")
    base_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    min_hours = db.Column(db.Integer, nullable=False, default=1)
    max_days = db.Column(db.Integer, nullable=False, default=30)
    default_available = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    lot = db.relationship("Lot", back_populates="spaces")
    price_rules = db.relationship("PriceRule", back_populates="space", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("lot_id", "name", name="uq_space_lot_name"),
        CheckConstraint(_in("kind", SPACE_KINDS), name="ck_space_kind"),
        CheckConstraint(_in("status", SPACE_STATUSES), name="ck_space_status"),
        CheckConstraint("w > 0 AND h > 0", name="ck_space_size"),
        CheckConstraint("base_price >= 0", name="ck_space_base_price"),
        CheckConstraint("min_hours > 0", name="ck_space_min_hours"),
        CheckConstraint("max_days > 0", name="ck_space_max_days"),
    )

    @property
    def price_per_day(self) -> Decimal:
        return money(self.base_price or 0)

    def availability_for(self, start, end, *, exclude_booking_ids=None):
        """Return True if the space is free for [start, end). Safe default: spaces are available unless filtered by future booking data."""
        if self.status == "blocked":
            return False
        if self.default_available is False:
            return False
        return True


class PriceRule(TenantMixin, db.Model):
    """Tenant-specific pricing layer. Rules are evaluated in priority order.

    A space can inherit a base price and then have additional seasonal/day-type rules.
    The model is intentionally lightweight: it is enough to create and validate pricing
    values for phase 2 without hard-coding operational assumptions.
    """
    __tablename__ = "price_rules"
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    space_id = db.Column(db.Integer, db.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=True, index=True)
    name = db.Column(db.String(80), nullable=False, default="Base pricing")
    period = db.Column(db.String(20), nullable=False, default="day")
    mode = db.Column(db.String(20), nullable=False, default="flat")
    amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    start_day = db.Column(db.Date)
    end_day = db.Column(db.Date)
    active = db.Column(db.Boolean, nullable=False, default=True)
    priority = db.Column(db.Integer, nullable=False, default=100)
    notes = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    lot = db.relationship("Lot", back_populates="price_rules")
    space = db.relationship("Space", back_populates="price_rules")

    __table_args__ = (
        CheckConstraint(_in("period", RULE_PERIODS), name="ck_price_rule_period"),
        CheckConstraint(_in("mode", PRICE_MODES), name="ck_price_rule_mode"),
        CheckConstraint("amount >= 0", name="ck_price_rule_amount"),
        CheckConstraint("(start_day IS NULL OR end_day IS NULL OR start_day <= end_day)", name="ck_price_rule_date_range"),
    )

    def calculate_for(self, start, end):
        """Return the total price for a booking interval. This is intentionally deterministic."""
        if not self.active or self.amount is None:
            return Decimal("0.00")
        delta = end - start
        total_hours = max(1, math.ceil(delta.total_seconds() / 3600))
        if self.mode == "per_hour":
            return money(self.amount * total_hours)
        if self.mode == "per_day":
            total_days = max(1, math.ceil(delta.total_seconds() / 86400))
            return money(self.amount * total_days)
        return money(self.amount)


class DiscountCode(TenantMixin, db.Model):
    """A tenant-configured, single-use-per-booking discount rule."""
    __tablename__ = "discount_codes"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False)
    discount_type = db.Column(db.String(12), nullable=False)
    value = db.Column(db.Numeric(10, 2), nullable=False)
    starts_on = db.Column(db.Date)
    ends_on = db.Column(db.Date)
    minimum_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    max_redemptions = db.Column(db.Integer)
    redemption_count = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_discount_code_tenant_code"),
        CheckConstraint(_in("discount_type", ("percentage", "fixed")), name="ck_discount_code_type"),
        CheckConstraint("value > 0", name="ck_discount_code_value"),
        CheckConstraint("minimum_amount >= 0", name="ck_discount_code_minimum"),
        CheckConstraint("max_redemptions IS NULL OR max_redemptions > 0", name="ck_discount_code_max_redemptions"),
        CheckConstraint("redemption_count >= 0", name="ck_discount_code_redemption_count"),
        CheckConstraint("(starts_on IS NULL OR ends_on IS NULL OR starts_on <= ends_on)", name="ck_discount_code_dates"),
    )


class TenantTranslation(TenantMixin, db.Model):
    """Tenant-owned overrides for keys in the platform translation catalogs."""
    __tablename__ = "tenant_translations"
    id = db.Column(db.Integer, primary_key=True)
    locale = db.Column(db.String(10), nullable=False)
    key = db.Column(db.String(120), nullable=False)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "locale", "key", name="uq_tenant_translation_locale_key"),
    )


class TenantPage(TenantMixin, db.Model):
    """Tenant blog articles and FAQ entries with localized, indexable URLs."""
    __tablename__ = "tenant_pages"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    locale = db.Column(db.String(10), nullable=False)
    slug = db.Column(db.String(160), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    summary = db.Column(db.String(320), nullable=False, default="")
    body = db.Column(db.Text, nullable=False, default="")
    seo_title = db.Column(db.String(180), nullable=False, default="")
    seo_description = db.Column(db.String(320), nullable=False, default="")
    published = db.Column(db.Boolean, nullable=False, default=False)
    published_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "locale", "slug", name="uq_tenant_page_kind_locale_slug"),
        CheckConstraint(_in("kind", ("blog", "faq")), name="ck_tenant_page_kind"),
    )


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    first_name = db.Column(db.String(80), nullable=False, default="")
    last_name = db.Column(db.String(80), nullable=False, default="")
    email = db.Column(db.String(255), nullable=False, default="")
    billing_email = db.Column(db.String(255), nullable=False, default="")
    locale = db.Column(db.String(10), nullable=False, default="en")
    phone = db.Column(db.String(40), nullable=False, default="")
    company = db.Column(db.String(120), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    vehicles = db.relationship("Vehicle", back_populates="customer", cascade="all, delete-orphan")
    bookings = db.relationship("Booking", back_populates="customer", cascade="all, delete-orphan")


class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    plate = db.Column(db.String(30), nullable=False)
    make = db.Column(db.String(60), nullable=False, default="")
    model = db.Column(db.String(60), nullable=False, default="")
    color = db.Column(db.String(40), nullable=False, default="")
    photo_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    customer = db.relationship("Customer", back_populates="vehicles")
    bookings = db.relationship("Booking", back_populates="vehicle")


class Hold(db.Model):
    __tablename__ = "holds"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    space_id = db.Column(db.Integer, db.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_email = db.Column(db.String(255), nullable=False)
    pickup_at = db.Column(db.DateTime(timezone=True), nullable=False)
    return_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    lot = db.relationship("Lot")
    space = db.relationship("Space")

    __table_args__ = (
        CheckConstraint(_in("status", ("active", "released", "converted", "expired")), name="ck_hold_status"),
        CheckConstraint("pickup_at < return_at", name="ck_hold_time_order"),
        Index("ix_hold_space_interval_status", "tenant_id", "space_id", "pickup_at", "return_at", "status"),
    )

    def is_active(self) -> bool:
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return self.status == "active" and datetime.now(timezone.utc) < expires_at


_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easy to read out and type


def new_booking_ref() -> str:
    """Readable booking number, e.g. K7QM-4TXD-9BWN (60 bits of randomness)."""
    raw = "".join(secrets.choice(_REF_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


class Booking(db.Model):
    __tablename__ = "bookings"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    space_id = db.Column(db.Integer, db.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=True, index=True)
    booking_ref = db.Column(db.String(32), nullable=False, unique=True, default=lambda: new_booking_ref())
    status = db.Column(db.String(20), nullable=False, default="reserved")
    payment_status = db.Column(db.String(20), nullable=False, default="pending")
    pickup_at = db.Column(db.DateTime(timezone=True), nullable=False)
    return_at = db.Column(db.DateTime(timezone=True), nullable=False)
    subtotal_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    discount_code = db.Column(db.String(40), nullable=True)
    notes = db.Column(db.Text, nullable=False, default="")
    qr_code = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    confirmation_sent_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    lot = db.relationship("Lot")
    space = db.relationship("Space")
    customer = db.relationship("Customer", back_populates="bookings")
    vehicle = db.relationship("Vehicle", back_populates="bookings")

    __table_args__ = (
        CheckConstraint(_in("status", ("reserved", "checked_in", "checked_out", "cancelled")), name="ck_booking_status"),
        CheckConstraint(_in("payment_status", ("pending", "paid", "partial", "failed", "refunded")), name="ck_booking_payment_status"),
        CheckConstraint("pickup_at < return_at", name="ck_booking_time_order"),
        CheckConstraint("total_amount >= 0", name="ck_booking_total_amount"),
        CheckConstraint("subtotal_amount >= 0", name="ck_booking_subtotal_amount"),
        Index("ix_booking_space_interval_status", "tenant_id", "space_id", "pickup_at", "return_at", "status"),
    )

    def generate_qr(self) -> str:
        if not self.qr_code:
            if not self.booking_ref:
                self.booking_ref = new_booking_ref()
            serializer = URLSafeSerializer(
                current_app.config["SECRET_KEY"],
                salt="parkpilot-booking-qr",
            )
            self.qr_code = serializer.dumps({"booking_ref": self.booking_ref})
        return self.qr_code


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = db.Column(db.String(30), nullable=False)
    provider_ref = db.Column(db.String(120), nullable=False, default="")
    provider_intent_ref = db.Column(db.String(120), nullable=False, default="")
    checkout_url = db.Column(db.String(500), nullable=False, default="")
    amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    status = db.Column(db.String(20), nullable=False, default="pending")
    paid_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    booking = db.relationship("Booking")

    __table_args__ = (
        CheckConstraint(_in("provider", ("stripe", "manual", "paypal", "apple_pay")), name="ck_payment_provider"),
        CheckConstraint(_in("status", ("pending", "paid", "failed", "refunded")), name="ck_payment_status"),
        CheckConstraint("amount >= 0", name="ck_payment_amount"),
    )


class ApiKey(TenantMixin, db.Model):
    __tablename__ = "api_keys"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    prefix = db.Column(db.String(24), nullable=False, unique=True, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    key_type = db.Column(db.String(2), nullable=False)
    scopes = db.Column(db.JSON, nullable=False, default=list)
    allowed_origins = db.Column(db.JSON, nullable=False, default=list)
    requests_per_minute = db.Column(db.Integer, nullable=False, default=60)
    active = db.Column(db.Boolean, nullable=False, default=True)
    last_used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at = db.Column(db.DateTime(timezone=True))
    allowed_lot_ids = db.Column(db.JSON)  # None = every active lot of the tenant
    monthly_price = db.Column(db.Numeric(10, 2), nullable=False, default=0, server_default="0")
    price_breakdown = db.Column(db.JSON)

    __table_args__ = (
        CheckConstraint(_in("key_type", ("pk", "sk")), name="ck_api_key_type"),
        CheckConstraint("requests_per_minute > 0", name="ck_api_key_rate_limit"),
    )


class UsageLog(TenantMixin, db.Model):
    __tablename__ = "usage_logs"
    id = db.Column(db.Integer, primary_key=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True)
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(255), nullable=False)
    status_code = db.Column(db.Integer, nullable=False)
    duration_ms = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class IdempotencyRecord(TenantMixin, db.Model):
    __tablename__ = "idempotency_records"
    id = db.Column(db.Integer, primary_key=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True)
    key_hash = db.Column(db.String(64), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    status_code = db.Column(db.Integer)
    content_type = db.Column(db.String(120))
    response_body = db.Column(db.Text)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "api_key_id", "key_hash", name="uq_idempotency_tenant_key"),
    )


class WebhookEndpoint(TenantMixin, db.Model):
    __tablename__ = "webhook_endpoints"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(2000), nullable=False)
    events = db.Column(db.JSON, nullable=False, default=list)
    signing_salt = db.Column(db.String(64), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    lot_ids = db.Column(db.JSON)  # None = events of every lot; else only these lots
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at = db.Column(db.DateTime(timezone=True))


class WebhookDelivery(TenantMixin, db.Model):
    __tablename__ = "webhook_deliveries"
    id = db.Column(db.Integer, primary_key=True)
    endpoint_id = db.Column(db.Integer, db.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = db.Column(db.String(36), nullable=False, unique=True)
    event_type = db.Column(db.String(60), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="queued")
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    last_status_code = db.Column(db.Integer)
    last_error = db.Column(db.String(500), nullable=False, default="")
    delivered_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    endpoint = db.relationship("WebhookEndpoint")

    __table_args__ = (
        CheckConstraint(
            _in("status", ("queued", "delivered", "failed")),
            name="ck_webhook_delivery_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_webhook_delivery_attempts"),
        Index("ix_webhook_delivery_due", "tenant_id", "status", "next_attempt_at"),
    )


class Inspection(TenantMixin, db.Model):
    __tablename__ = "inspections"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    phase = db.Column(db.String(12), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="in_progress")
    staff_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    fuel_level = db.Column(db.Integer)
    odometer_km = db.Column(db.Integer)
    key_count = db.Column(db.Integer)
    belongings = db.Column(db.Text, nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    no_damage_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    retention_until = db.Column(db.DateTime(timezone=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = db.Column(db.DateTime(timezone=True))
    report_sent_at = db.Column(db.DateTime(timezone=True))
    # JSON of the handover form while the inspection is open (autosaved, so nothing is lost on reload or a phone swap)
    draft_details = db.Column(db.Text)

    booking = db.relationship("Booking")
    staff_user = db.relationship("User")
    photos = db.relationship("InspectionPhoto", back_populates="inspection", cascade="all, delete-orphan")
    videos = db.relationship("InspectionVideo", back_populates="inspection", cascade="all, delete-orphan")
    damage_marks = db.relationship(
        "DamageMark",
        foreign_keys="DamageMark.inspection_id",
        back_populates="inspection",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "booking_id", "phase", name="uq_inspection_tenant_booking_phase"),
        CheckConstraint(_in("phase", ("arrival", "departure")), name="ck_inspection_phase"),
        CheckConstraint(_in("status", ("in_progress", "completed")), name="ck_inspection_status"),
        CheckConstraint("fuel_level IS NULL OR fuel_level BETWEEN 0 AND 100", name="ck_inspection_fuel_level"),
        CheckConstraint("odometer_km IS NULL OR odometer_km >= 0", name="ck_inspection_odometer"),
        CheckConstraint("key_count IS NULL OR key_count >= 0", name="ck_inspection_key_count"),
        Index("ix_inspection_booking_phase_status", "tenant_id", "booking_id", "phase", "status"),
    )


class InspectionPhoto(TenantMixin, db.Model):
    __tablename__ = "inspection_photos"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True, index=True)
    category = db.Column(db.String(80), nullable=False)
    object_key = db.Column(db.String(500), nullable=False, unique=True)
    sha256 = db.Column(db.String(64), nullable=False)
    content_type = db.Column(db.String(40), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    booking_ref = db.Column(db.String(32), nullable=False)
    staff_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    staff_name = db.Column(db.String(120), nullable=False)
    captured_at = db.Column(db.DateTime(timezone=True), nullable=False)
    uploaded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    inspection = db.relationship("Inspection", back_populates="photos")
    staff_user = db.relationship("User")

    __table_args__ = (
        UniqueConstraint("tenant_id", "inspection_id", "category", name="uq_inspection_photo_tenant_category"),
        CheckConstraint("size_bytes > 0", name="ck_inspection_photo_size"),
        CheckConstraint("length(sha256) = 64", name="ck_inspection_photo_sha256"),
    )


class InspectionVideo(TenantMixin, db.Model):
    __tablename__ = "inspection_videos"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True)
    category = db.Column(db.String(80), nullable=False)
    object_key = db.Column(db.String(500), nullable=False, unique=True)
    sha256 = db.Column(db.String(64), nullable=False)
    content_type = db.Column(db.String(40), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    booking_ref = db.Column(db.String(32), nullable=False)
    staff_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    staff_name = db.Column(db.String(120), nullable=False)
    captured_at = db.Column(db.DateTime(timezone=True), nullable=False)
    uploaded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    inspection = db.relationship("Inspection", back_populates="videos")
    staff_user = db.relationship("User")

    __table_args__ = (
        UniqueConstraint("tenant_id", "inspection_id", "category", name="uq_inspection_video_tenant_category"),
        CheckConstraint("size_bytes > 0", name="ck_inspection_video_size"),
        CheckConstraint("length(sha256) = 64", name="ck_inspection_video_sha256"),
    )


class DamageMark(TenantMixin, db.Model):
    __tablename__ = "damage_marks"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True)
    x_percent = db.Column(db.Float, nullable=False)
    y_percent = db.Column(db.Float, nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    notes = db.Column(db.String(1000), nullable=False, default="")
    photo_id = db.Column(db.Integer, db.ForeignKey("inspection_photos.id", ondelete="SET NULL"))
    matched_damage_id = db.Column(db.Integer, db.ForeignKey("damage_marks.id", ondelete="SET NULL"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    inspection = db.relationship(
        "Inspection",
        foreign_keys=[inspection_id],
        back_populates="damage_marks",
    )
    photo = db.relationship("InspectionPhoto", foreign_keys=[photo_id])
    matched_damage = db.relationship("DamageMark", remote_side=[id], foreign_keys=[matched_damage_id])

    __table_args__ = (
        CheckConstraint("x_percent BETWEEN 0 AND 100", name="ck_damage_mark_x"),
        CheckConstraint("y_percent BETWEEN 0 AND 100", name="ck_damage_mark_y"),
        CheckConstraint(_in("kind", ("scratch", "dent", "crack", "broken_part")), name="ck_damage_mark_kind"),
        UniqueConstraint("tenant_id", "matched_damage_id", name="uq_damage_mark_matched_once"),
    )


class Dispute(TenantMixin, db.Model):
    __tablename__ = "disputes"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    arrival_inspection_id = db.Column(db.Integer, db.ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False)
    departure_inspection_id = db.Column(db.Integer, db.ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False)
    damage_mark_id = db.Column(db.Integer, db.ForeignKey("damage_marks.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = db.Column(db.String(16), nullable=False, default="open")
    resolution = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True))

    booking = db.relationship("Booking")
    arrival_inspection = db.relationship("Inspection", foreign_keys=[arrival_inspection_id])
    departure_inspection = db.relationship("Inspection", foreign_keys=[departure_inspection_id])
    damage_mark = db.relationship("DamageMark")

    __table_args__ = (
        CheckConstraint(_in("status", ("open", "resolved", "dismissed")), name="ck_dispute_status"),
    )


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), index=True)  # NULL = platform-level event
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    action = db.Column(db.String(60), nullable=False)
    entity = db.Column(db.String(40), nullable=False, default="")
    entity_id = db.Column(db.Integer)
    data = db.Column(db.JSON)
    ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


# --- invoices, notifications, subscriptions (phases 9 and 11) --------------------

INVOICE_KINDS = ("invoice", "credit_note")
INVOICE_STATUSES = ("open", "paid", "cancelled", "credited")


class Invoice(TenantMixin, db.Model):
    """A legal invoice (Rechnung) or credit note (Gutschrift) for one booking.

    Everything printed is copied into the row (customer, seller, lines), so later edits or deleting the booking
    never change a document that has been issued. Invoices are kept even when the booking is deleted.
    """
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    related_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="SET NULL"))
    kind = db.Column(db.String(12), nullable=False, default="invoice")
    number = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="open")
    booking_ref = db.Column(db.String(32), nullable=False, default="")
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    net_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    vat_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    gross_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    customer_name = db.Column(db.String(200), nullable=False, default="")
    customer_company = db.Column(db.String(120), nullable=False, default="")
    customer_email = db.Column(db.String(255), nullable=False, default="")
    locale = db.Column(db.String(10), nullable=False, default="de")
    lines = db.Column(db.JSON, nullable=False, default=list)
    seller = db.Column(db.JSON, nullable=False, default=dict)
    service_from = db.Column(db.DateTime(timezone=True))
    service_to = db.Column(db.DateTime(timezone=True))
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    paid_at = db.Column(db.DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("kind", INVOICE_KINDS), name="ck_invoice_kind"),
        CheckConstraint(_in("status", INVOICE_STATUSES), name="ck_invoice_status"),
        UniqueConstraint("tenant_id", "number", name="uq_invoice_tenant_number"),
    )


NOTIFICATION_CHANNELS = ("email", "sms", "whatsapp")
NOTIFICATION_STATUSES = ("sent", "failed", "skipped")


class Notification(TenantMixin, db.Model):
    """One attempt to tell a customer something (email, SMS, WhatsApp). Doubles as the delivery log and the dedupe key."""
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    channel = db.Column(db.String(10), nullable=False)
    event = db.Column(db.String(30), nullable=False)
    recipient = db.Column(db.String(255), nullable=False, default="")
    status = db.Column(db.String(10), nullable=False)
    error = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    __table_args__ = (
        CheckConstraint(_in("channel", NOTIFICATION_CHANNELS), name="ck_notification_channel"),
        CheckConstraint(_in("status", NOTIFICATION_STATUSES), name="ck_notification_status"),
        Index("ix_notification_booking_event", "tenant_id", "booking_id", "event", "channel"),
    )


SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "cancelled", "expired")


class Subscription(TenantMixin, db.Model):
    """The platform subscription of one tenant (Stripe Billing). One row per tenant."""
    __tablename__ = "subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    plan_key = db.Column(db.String(20), nullable=False, default="starter")
    status = db.Column(db.String(12), nullable=False, default="trialing")
    trial_ends_at = db.Column(db.DateTime(timezone=True))
    current_period_end = db.Column(db.DateTime(timezone=True))
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    stripe_customer_id = db.Column(db.String(80), nullable=False, default="")
    stripe_subscription_id = db.Column(db.String(80), nullable=False, default="")
    amount_cents = db.Column(db.Integer, nullable=False, default=0)   # copied from Stripe, never invented
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    last_payment_failed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(_in("status", SUBSCRIPTION_STATUSES), name="ck_subscription_status"),
        UniqueConstraint("tenant_id", name="uq_subscription_tenant"),
    )


# --- notification centre -----------------------------------------------------------

ALERT_KINDS = ("booking_created", "booking_paid", "booking_cancelled", "booking_refunded", "checked_in", "checked_out", "dispute_opened")


class Alert(TenantMixin, db.Model):
    """Something the team should know (new booking, payment, cancellation, ...). Shown in the notification centre.

    Only facts are stored (reference, customer, amount); the sentence is built when it is shown, in the viewer's language.
    """
    __tablename__ = "alerts"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    data = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    __table_args__ = (CheckConstraint(_in("kind", ALERT_KINDS), name="ck_alert_kind"),)


EXTERNAL_INTEGRATION_PROVIDERS = ("parkos", "parkenlist")
INTEGRATION_CONNECTION_STATUSES = ("pending", "active", "syncing", "error", "disabled")
INTEGRATION_SYNC_DIRECTIONS = ("import", "export")
INTEGRATION_SYNC_STATUSES = ("running", "succeeded", "partial", "failed")


class ExternalIntegrationConnection(TenantMixin, db.Model):
    """Provider connection metadata only; credentials are resolved server-side from an opaque reference."""
    __tablename__ = "external_integration_connections"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="pending")
    credential_ref = db.Column(db.String(255))
    last_sync_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_external_integration_tenant_provider"),
        CheckConstraint(_in("provider", EXTERNAL_INTEGRATION_PROVIDERS), name="ck_external_integration_provider"),
        CheckConstraint(_in("status", INTEGRATION_CONNECTION_STATUSES), name="ck_external_integration_status"),
    )


class ExternalIntegrationLocation(TenantMixin, db.Model):
    """Maps one authorized ParkPilot lot to a provider's opaque location identifier."""
    __tablename__ = "external_integration_locations"
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey("external_integration_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    external_location_id = db.Column(db.String(160), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("connection_id", "lot_id", name="uq_external_location_connection_lot"),
        UniqueConstraint("connection_id", "external_location_id", name="uq_external_location_connection_external_id"),
    )


class ExternalBookingMapping(TenantMixin, db.Model):
    """Idempotency key and linkage for an external reservation mapped into ParkPilot."""
    __tablename__ = "external_booking_mappings"
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey("external_integration_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    external_booking_id = db.Column(db.String(160), nullable=False)
    external_location_id = db.Column(db.String(160), nullable=False, default="")
    last_synced_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("connection_id", "external_booking_id", name="uq_external_booking_connection_external_id"),
        UniqueConstraint("connection_id", "booking_id", name="uq_external_booking_connection_booking"),
    )


class ExternalIntegrationSyncRun(TenantMixin, db.Model):
    """Auditable status for one future provider synchronization attempt."""
    __tablename__ = "external_integration_sync_runs"
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey("external_integration_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=False)
    status = db.Column(db.String(10), nullable=False, default="running")
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = db.Column(db.DateTime(timezone=True))
    records_processed = db.Column(db.Integer, nullable=False, default=0)
    error_summary = db.Column(db.String(500), nullable=False, default="")

    __table_args__ = (
        CheckConstraint(_in("direction", INTEGRATION_SYNC_DIRECTIONS), name="ck_external_sync_direction"),
        CheckConstraint(_in("status", INTEGRATION_SYNC_STATUSES), name="ck_external_sync_status"),
        CheckConstraint("records_processed >= 0", name="ck_external_sync_records_processed"),
    )


# --- automatic read scoping ---------------------------------------------------

SCOPED = (
    User, Lot, Space, PriceRule, DiscountCode, TenantTranslation, TenantPage, Customer, Vehicle, Hold, Booking, Payment,
    ApiKey, UsageLog, IdempotencyRecord, WebhookEndpoint, WebhookDelivery,
    Inspection, InspectionPhoto, InspectionVideo, DamageMark, Dispute, AuditLog, Invoice, Notification, Subscription, Alert,
    ExternalIntegrationConnection, ExternalIntegrationLocation, ExternalBookingMapping, ExternalIntegrationSyncRun,
)


@event.listens_for(orm.Session, "do_orm_execute")
def _scope_to_tenant(state):
    tid = current_tenant_id()
    if tid is None or not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    for model in SCOPED:
        state.statement = state.statement.options(
            with_loader_criteria(model, lambda cls: cls.tenant_id == tid, include_aliases=True)
        )


class ApiAccessRequest(TenantMixin, db.Model):
    """An owner asks the platform admin for an API bundle; only the admin issues keys."""
    __tablename__ = "api_access_requests"
    id = db.Column(db.Integer, primary_key=True)
    bundle = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(500), nullable=False, default="")
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending | approved | rejected
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    decided_at = db.Column(db.DateTime(timezone=True))
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


EXTRA_KINDS = ("shuttle", "cleaning")


class Extra(TenantMixin, db.Model):
    """Optional paid service sold with a booking (airport shuttle, car cleaning)."""
    __tablename__ = "extras"
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(12), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    per_person = db.Column(db.Boolean, nullable=False, default=False)  # price depends on the number of passengers
    # Bundle pricing (airport shuttle): `price` covers up to `included_persons` people; every additional person pays
    # `extra_person_price`. When that is NULL no additional person is accepted (we never invent a price).
    # included_persons = 0 keeps the older rule: price per passenger.
    included_persons = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    extra_person_price = db.Column(db.Numeric(10, 2))
    # Hard cap on the number of persons for a per-person extra (airport shuttle: 2 persons incl. luggage). NULL = no cap.
    max_persons = db.Column(db.Integer)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint(_in("kind", EXTRA_KINDS), name="ck_extra_kind"),
        CheckConstraint("price >= 0", name="ck_extra_price"),
    )


class BookingExtra(TenantMixin, db.Model):
    """An extra ordered on a booking; the name and price are copied so later edits never change old bookings."""
    __tablename__ = "booking_extras"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    extra_id = db.Column(db.Integer, db.ForeignKey("extras.id", ondelete="SET NULL"))
    kind = db.Column(db.String(12), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)  # price per unit (per person for per-person extras)
    quantity = db.Column(db.Integer, nullable=False, default=1)  # passengers for per-person extras
    included_persons = db.Column(db.Integer, nullable=False, default=0, server_default="0")  # copied from the extra
    extra_person_price = db.Column(db.Numeric(10, 2), nullable=False, default=0, server_default="0")
    details = db.Column(db.String(255), nullable=False, default="")  # e.g. flight number, passengers
    status = db.Column(db.String(10), nullable=False, default="requested")  # requested | done | cancelled
    done_at = db.Column(db.DateTime(timezone=True))
    done_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    booking = db.relationship("Booking", backref=db.backref("extras", cascade="all, delete-orphan", order_by="BookingExtra.id"))

    @property
    def additional_persons(self) -> int:
        """People beyond the included ones (bundle pricing only)."""
        return max(0, (self.quantity or 1) - self.included_persons) if self.included_persons else 0

    def total(self) -> Decimal:
        """What this row costs: bundle = base price + additional persons; otherwise unit price x quantity."""
        unit = Decimal(self.unit_price)
        if self.included_persons:
            return money(unit + Decimal(self.extra_person_price or 0) * self.additional_persons)
        return money(unit * (self.quantity or 1))
