"""Tenant and platform administration views."""
import calendar
import csv
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, abort, current_app, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user
from sqlalchemy import case, delete, desc, func, or_, select, update

from ..ui_i18n import ui
from ..extensions import db, login_manager
from ..forms import (
    BrandSettingsForm,
    DiscountCodeForm,
    InspectionSettingsForm,
    KIND_LABELS,
    PriceRuleForm,
    STATUS_LABELS,
    LotForm,
    SpaceForm,
    TeamMemberEditForm,
    TeamMemberForm,
    TenantPageForm,
    TenantTranslationForm,
)
from ..services import billing
from ..services.messaging import normalize_phone
from ..services.logo import process_logo
from ..i18n import supported_locales, translation_keys, valid_translation_override
from ..models import (
    SPACE_KINDS,
    SPACE_STATUSES,
    ApiAccessRequest,
    ApiKey,
    AuditLog,
    Booking,
    BookingExtra,
    Extra,
    Hold,
    IdempotencyRecord,
    Inspection,
    InspectionPhoto,
    Customer,
    DiscountCode,
    Dispute,
    EXTERNAL_INTEGRATION_PROVIDERS,
    ExternalIntegrationConnection,
    Lot,
    PriceRule,
    Role,
    Space,
    Tenant,
    TenantPage,
    TenantTranslation,
    UsageLog,
    User,
    Vehicle,
    WebhookDelivery,
    WebhookEndpoint,
    utcnow,
)
from ..services.api_platform import (
    API_BUNDLES,
    describe_key,
    issue_partner_access,
    pricing_config,
    WEBHOOK_EVENTS,
    PUBLIC_SCOPES,
    SECRET_SCOPES,
    ApiPlatformError,
    create_webhook_endpoint,
    issue_bundle_key,
    update_api_key,
    update_webhook_endpoint,
    webhook_signing_secret,
)
from ..tenancy import use_tenant
from ..utils import audit, grid_position, slugify

bp = Blueprint("admin", __name__, url_prefix="/admin")
BOOKING_STATUSES = ("reserved", "checked_in", "checked_out", "cancelled")
TENANT_STATUSES = ("trial", "active", "suspended")


@bp.before_request
def require_login():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if current_user.role == Role.PLATFORM_OWNER and request.endpoint not in {
        "admin.dashboard",
        "admin.platform_tenant_status",
    } and not (request.endpoint or "").startswith("admin.platform_"):
        abort(403)


@bp.app_context_processor
def inject_owner_locations():
    if not current_user.is_authenticated or not current_user.is_owner:
        return {}
    locations = Lot.query.order_by(Lot.name).all()
    selected_id = session.get("owner_dashboard_lot_id")
    if selected_id not in {lot.id for lot in locations}:
        selected_id = None
        session.pop("owner_dashboard_lot_id", None)
    return {"owner_locations": locations, "owner_selected_location_id": selected_id}


def owner_required(view):
    """Write access: owners only (staff are read-only until the staff app in phase 5)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_owner:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def platform_owner_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user.role != Role.PLATFORM_OWNER:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def _lot_or_404(lot_id) -> Lot:
    return db.get_or_404(Lot, lot_id)  # tenant-scoped automatically


def _owned_record_or_404(model, record_id):
    record = db.session.execute(
        select(model).where(
            model.id == record_id,
            model.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if record is None:
        abort(404)
    return record


def _booking_query():
    query = Booking.query
    status = request.args.get("status", "").strip()
    if status:
        if status not in BOOKING_STATUSES:
            abort(400)
        query = query.filter(Booking.status == status)
    term = request.args.get("q", "").strip()[:100]
    if term:
        pattern = f"%{term}%"
        query = query.filter(
            or_(
                Booking.booking_ref.ilike(pattern),
                Booking.customer.has(
                    or_(
                        Customer.email.ilike(pattern),
                        Customer.first_name.ilike(pattern),
                        Customer.last_name.ilike(pattern),
                    )
                ),
                Booking.vehicle.has(Vehicle.plate.ilike(pattern)),
            )
        )
    return query.order_by(Booking.pickup_at.desc(), Booking.id.desc())


def _csv_value(value) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unique_slug(base: str) -> str:
    slug, n = base, 2
    while Lot.query.filter_by(slug=slug).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _validate_space(form: SpaceForm, lot: Lot, space_id=None) -> bool:
    ok = form.validate_on_submit()
    if not ok:
        return False
    raw_name = (form.name.data or "").strip()
    if not raw_name:
        form.name.errors.append("This field is required.")
        return False
    clash = Space.query.filter(Space.lot_id == lot.id, Space.name == raw_name, Space.id != (space_id or 0)).first()
    if clash:
        form.name.errors.append("Another space in this lot already has this name.")
        ok = False
    if form.x.data + form.w.data > lot.canvas_w or form.y.data + form.h.data > lot.canvas_h:
        form.x.errors.append(f"Space must fit inside the {lot.canvas_w}x{lot.canvas_h} map.")
        ok = False
    return ok


# --- dashboard ----------------------------------------------------------------

@bp.get("/")
def dashboard():
    if current_user.role == Role.PLATFORM_OWNER:
        return _platform_dashboard()

    lots = Lot.query.order_by(Lot.name).all()
    selected_lot_id = session.get("owner_dashboard_lot_id")
    selected_lot = None
    lot_filter = []
    if selected_lot_id:
        selected_lot = db.session.execute(
            select(Lot).where(
                Lot.id == selected_lot_id,
                Lot.tenant_id == current_user.tenant_id,
            )
        ).scalar_one_or_none()
        if selected_lot is None:
            abort(404)
        lot_filter = [Booking.lot_id == selected_lot.id]

    bookings_total = db.session.scalar(
        select(func.count()).select_from(Booking).where(*lot_filter)
    )
    paid_total = db.session.scalar(
        select(func.coalesce(func.sum(Booking.total_amount), 0)).where(
            Booking.payment_status == "paid",
            *lot_filter,
        )
    )
    now = utcnow()
    next_day = now + timedelta(hours=24)
    upcoming_count = db.session.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.status == "reserved",
            Booking.pickup_at >= now,
            Booking.pickup_at < next_day,
            *lot_filter,
        )
    )
    upcoming_departure_count = db.session.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.status.in_(("reserved", "checked_in")),
            Booking.return_at >= now,
            Booking.return_at < next_day,
            *lot_filter,
        )
    )
    open_disputes = db.session.scalar(
        select(func.count()).select_from(Dispute)
        .join(Booking, Booking.id == Dispute.booking_id)
        .where(Dispute.status == "open", *lot_filter)
    )
    recent_bookings = Booking.query.filter(*lot_filter).order_by(Booking.created_at.desc()).limit(8).all()
    upcoming_arrivals = Booking.query.filter(
        Booking.status == "reserved",
        Booking.pickup_at >= now,
        Booking.pickup_at < next_day,
        *lot_filter,
    ).order_by(Booking.pickup_at, Booking.id).limit(6).all()
    upcoming_departures = Booking.query.filter(
        Booking.status.in_(("reserved", "checked_in")),
        Booking.return_at >= now,
        Booking.return_at < next_day,
        *lot_filter,
    ).order_by(Booking.return_at, Booking.id).limit(6).all()
    return render_template(
        "admin/dashboard.html",
        lots=lots,
        selected_lot=selected_lot,
        bookings_total=bookings_total,
        paid_total=paid_total,
        upcoming_count=upcoming_count,
        upcoming_departure_count=upcoming_departure_count,
        open_disputes=open_disputes,
        recent_bookings=recent_bookings,
        upcoming_arrivals=upcoming_arrivals,
        upcoming_departures=upcoming_departures,
    )


@bp.post("/location")
@owner_required
def set_dashboard_location():
    selected_id = request.form.get("lot_id", "").strip()
    if not selected_id:
        session.pop("owner_dashboard_lot_id", None)
        return redirect(url_for("admin.dashboard"))
    if not selected_id.isdigit():
        abort(404)
    location = db.session.execute(
        select(Lot).where(
            Lot.id == int(selected_id),
            Lot.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if location is None:
        abort(404)
    session["owner_dashboard_lot_id"] = location.id
    return redirect(url_for("admin.dashboard"))


@bp.get("/search")
@owner_required
def owner_search():
    term = request.args.get("q", "").strip()[:100]
    if len(term) < 2 and not term.isdigit():
        return jsonify(results=[])

    escaped_term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_term}%"
    phone_term = re.sub(r"\D", "", term)
    phone_match = None
    if len(phone_term) >= 2:
        normalized_phone = func.replace(
            func.replace(
                func.replace(
                    func.replace(func.replace(Customer.phone, " ", ""), "-", ""),
                    "+",
                    "",
                ),
                "(",
                "",
            ),
            ")",
            "",
        )
        phone_match = normalized_phone.like(f"%{phone_term}%")

    matches = [
        Booking.booking_ref.ilike(pattern, escape="\\"),
        Customer.first_name.ilike(pattern, escape="\\"),
        Customer.last_name.ilike(pattern, escape="\\"),
        (Customer.first_name + " " + Customer.last_name).ilike(pattern, escape="\\"),
        Customer.email.ilike(pattern, escape="\\"),
        Customer.billing_email.ilike(pattern, escape="\\"),
        Vehicle.plate.ilike(pattern, escape="\\"),
    ]
    if phone_match is not None:
        matches.append(phone_match)
    if term.isdigit():
        matches.append(Booking.id == int(term))

    bookings = db.session.execute(
        select(Booking)
        .join(Customer, Customer.id == Booking.customer_id)
        .outerjoin(Vehicle, Vehicle.id == Booking.vehicle_id)
        .where(
            Booking.tenant_id == current_user.tenant_id,
            or_(*matches),
        )
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .limit(8)
    ).scalars().unique().all()
    response = jsonify(
        results=[
            {
                "id": booking.id,
                "reference": booking.booking_ref,
                "customer": f"{booking.customer.first_name} {booking.customer.last_name}".strip(),
                "plate": booking.vehicle.plate if booking.vehicle else "",
                "lot": booking.lot.name if booking.lot else "",
                "pickup": booking.pickup_at.strftime("%Y-%m-%d %H:%M"),
                "status": booking.status.replace("_", " ").title(),
                "url": url_for("manage.booking_detail", booking_id=booking.id),
            }
            for booking in bookings
        ]
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/integrations")
@owner_required
def integrations():
    connections = {
        connection.provider: connection
        for connection in ExternalIntegrationConnection.query.order_by(ExternalIntegrationConnection.provider).all()
    }
    provider_names = {"parkos": "Parkos", "parkenlist": "Parkenlist"}
    return render_template(
        "admin/integrations.html",
        providers=[
            {"key": provider, "name": provider_names[provider], "connection": connections.get(provider)}
            for provider in EXTERNAL_INTEGRATION_PROVIDERS
        ],
    )


def _platform_dashboard():
    booking_stats = (
        select(
            Booking.tenant_id.label("tenant_id"),
            func.count(Booking.id).label("booking_count"),
            func.coalesce(
                func.sum(
                    case(
                        (Booking.payment_status == "paid", Booking.total_amount),
                        else_=0,
                    )
                ),
                0,
            ).label("paid_total"),
        )
        .group_by(Booking.tenant_id)
        .subquery()
    )
    lot_stats = (
        select(Lot.tenant_id.label("tenant_id"), func.count(Lot.id).label("lot_count"))
        .group_by(Lot.tenant_id)
        .subquery()
    )
    usage_stats = (
        select(UsageLog.tenant_id.label("tenant_id"), func.count(UsageLog.id).label("api_calls"))
        .group_by(UsageLog.tenant_id)
        .subquery()
    )
    tenants = db.session.execute(
        select(
            Tenant,
            func.coalesce(booking_stats.c.booking_count, 0),
            func.coalesce(booking_stats.c.paid_total, 0),
            func.coalesce(lot_stats.c.lot_count, 0),
            func.coalesce(usage_stats.c.api_calls, 0),
        )
        .outerjoin(booking_stats, booking_stats.c.tenant_id == Tenant.id)
        .outerjoin(lot_stats, lot_stats.c.tenant_id == Tenant.id)
        .outerjoin(usage_stats, usage_stats.c.tenant_id == Tenant.id)
        .order_by(Tenant.created_at.desc(), Tenant.id.desc())
    ).all()
    totals = {
        "tenants": len(tenants),
        "active": sum(tenant.status == "active" for tenant, *_ in tenants),
        "trial": sum(tenant.status == "trial" for tenant, *_ in tenants),
        "suspended": sum(tenant.status == "suspended" for tenant, *_ in tenants),
        "bookings": sum(row[1] for row in tenants),
        "api_calls": sum(row[4] for row in tenants),
    }
    paid_by_currency = db.session.execute(
        select(
            Booking.currency,
            func.sum(Booking.total_amount),
        )
        .where(Booking.payment_status == "paid")
        .group_by(Booking.currency)
        .order_by(Booking.currency)
    ).all()
    return render_template(
        "admin/platform_dashboard.html",
        tenants=tenants,
        totals=totals,
        paid_by_currency=paid_by_currency,
    )


@bp.post("/platform/tenants/<int:tenant_id>/status")
@platform_owner_required
def platform_tenant_status(tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        abort(404)
    status = request.form.get("status", "").strip()
    if status not in TENANT_STATUSES:
        abort(400)
    previous_status = tenant.status
    tenant.status = status
    audit(
        "platform.tenant.status",
        "tenant",
        tenant.id,
        old_status=previous_status,
        new_status=status,
    )
    db.session.commit()
    flash(ui('%(name)s is now %(status)s.', name=tenant.name, status=status), "ok")
    return redirect(url_for("admin.dashboard"))


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _tenant_or_404(tenant_id) -> Tenant:
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        abort(404)
    return tenant


def _read_tenant_form(tenant=None):
    """Validate the tenant fields shared by create and edit. Returns (values, errors)."""
    from zoneinfo import ZoneInfo

    form = request.form
    name = form.get("name", "").strip()
    slug = slugify(form.get("slug", "").strip() or name)
    values = {
        "name": name,
        "slug": slug,
        "status": form.get("status", "trial").strip(),
        "default_locale": form.get("default_locale", "de").strip(),
        "timezone": form.get("timezone", "Europe/Berlin").strip(),
        "currency": form.get("currency", "EUR").strip().upper(),
        "custom_domain": form.get("custom_domain", "").strip().lower() or None,
    }
    errors = []
    if not 2 <= len(name) <= 120:
        errors.append("Name must be 2 to 120 characters.")
    if not _SLUG_RE.match(slug) or len(slug) > 80:
        errors.append("Slug may only contain lowercase letters, digits and hyphens.")
    if values["status"] not in TENANT_STATUSES:
        errors.append("Unknown status.")
    if values["default_locale"] not in supported_locales():
        errors.append("Unsupported language.")
    try:
        ZoneInfo(values["timezone"])
    except Exception:
        errors.append("Unknown time zone.")
    if not re.fullmatch(r"[A-Z]{3}", values["currency"]):
        errors.append("Currency must be a three-letter code such as EUR.")
    if values["custom_domain"] and not _DOMAIN_RE.match(values["custom_domain"]):
        errors.append("Custom domain is not a valid host name.")
    same_slug = Tenant.query.filter(Tenant.slug == slug)
    if tenant is not None:
        same_slug = same_slug.filter(Tenant.id != tenant.id)
    if not errors and same_slug.first():
        errors.append("That slug is already used by another tenant.")
    if not errors and values["custom_domain"]:
        same_domain = Tenant.query.filter(Tenant.custom_domain == values["custom_domain"])
        if tenant is not None:
            same_domain = same_domain.filter(Tenant.id != tenant.id)
        if same_domain.first():
            errors.append("That custom domain is already used by another tenant.")
    return values, errors


def _render_tenant_form(tenant, values, owner=None, status_code=200):
    stats = None
    users = []
    if tenant is not None:
        stats = {
            "lots": db.session.scalar(select(func.count(Lot.id)).where(Lot.tenant_id == tenant.id)),
            "bookings": db.session.scalar(select(func.count(Booking.id)).where(Booking.tenant_id == tenant.id)),
        }
        users = db.session.execute(
            select(User).where(User.tenant_id == tenant.id).order_by(User.role, User.email)
        ).scalars().all()
    return render_template(
        "admin/tenant_form.html", tenant=tenant, values=values, owner=owner or {},
        statuses=TENANT_STATUSES, locales=supported_locales(), stats=stats, users=users,
    ), status_code


@bp.route("/platform/tenants/new", methods=["GET", "POST"])
@platform_owner_required
def platform_tenant_new():
    if request.method == "GET":
        return _render_tenant_form(None, {"status": "trial", "default_locale": "de",
                                          "timezone": "Europe/Berlin", "currency": "EUR"})
    values, errors = _read_tenant_form()
    owner = {
        "email": request.form.get("owner_email", "").strip().lower(),
        "name": request.form.get("owner_name", "").strip() or "Owner",
    }
    password = request.form.get("owner_password", "")
    if not _EMAIL_RE.match(owner["email"]):
        errors.append("Enter a valid owner email.")
    elif User.query.filter(User.email == owner["email"]).first():
        errors.append("A user with that email already exists.")
    from ..password_policy import validate_password
    errors.extend(validate_password(password, email=owner["email"], name=owner["name"]))
    if errors:
        for message in errors:
            flash(ui(message), "error")
        return _render_tenant_form(None, values, owner, 400)
    tenant = Tenant(**values)
    db.session.add(tenant)
    db.session.flush()
    billing.start_trial(tenant)
    user = User(tenant_id=tenant.id, email=owner["email"], name=owner["name"], role=Role.OWNER)
    user.set_password(password)
    db.session.add(user)
    audit("platform.tenant.create", "tenant", tenant.id, slug=tenant.slug, owner_email=owner["email"])
    db.session.commit()
    flash(ui('%(name)s was created. Share the owner login with them.', name=tenant.name), "ok")
    return redirect(url_for("admin.platform_tenant_detail", tenant_id=tenant.id))


@bp.get("/platform/tenants/<int:tenant_id>")
@platform_owner_required
def platform_tenant_detail(tenant_id):
    tenant = _tenant_or_404(tenant_id)
    values = {c: getattr(tenant, c) for c in (
        "name", "slug", "status", "default_locale", "timezone", "currency", "custom_domain")}
    return _render_tenant_form(tenant, values)


@bp.post("/platform/tenants/<int:tenant_id>/edit")
@platform_owner_required
def platform_tenant_update(tenant_id):
    tenant = _tenant_or_404(tenant_id)
    values, errors = _read_tenant_form(tenant)
    if errors:
        for message in errors:
            flash(ui(message), "error")
        return _render_tenant_form(tenant, values, status_code=400)
    changed = {k: v for k, v in values.items() if getattr(tenant, k) != v}
    for key, value in values.items():
        setattr(tenant, key, value)
    audit("platform.tenant.update", "tenant", tenant.id, changed=sorted(changed))
    db.session.commit()
    flash(ui('%(name)s was updated.', name=tenant.name), "ok")
    return redirect(url_for("admin.platform_tenant_detail", tenant_id=tenant.id))


@bp.post("/platform/tenants/<int:tenant_id>/delete")
@platform_owner_required
def platform_tenant_delete(tenant_id):
    tenant = _tenant_or_404(tenant_id)
    if request.form.get("confirm_slug", "").strip() != tenant.slug:
        flash(ui("Type the tenant slug exactly to confirm the deletion."), "error")
        return redirect(url_for("admin.platform_tenant_detail", tenant_id=tenant.id))
    booking_count = db.session.scalar(select(func.count(Booking.id)).where(Booking.tenant_id == tenant.id))
    if booking_count:
        flash(
            ui('%(name)s has %(booking_count)s booking(s). Bookings are financial records, so the tenant cannot be deleted. Suspend it instead.', name=tenant.name, booking_count=booking_count),
            "error",
        )
        return redirect(url_for("admin.platform_tenant_detail", tenant_id=tenant.id))
    name, slug, tenant_pk = tenant.name, tenant.slug, tenant.id
    try:
        for table in reversed(db.metadata.sorted_tables):
            if "tenant_id" in table.c:
                db.session.execute(table.delete().where(table.c.tenant_id == tenant_pk))
        db.session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_pk))
        audit("platform.tenant.delete", "tenant", tenant_pk, name=name, slug=slug)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Tenant deletion failed")
        flash(ui("The tenant could not be deleted. Suspend it instead."), "error")
        return redirect(url_for("admin.platform_tenant_detail", tenant_id=tenant_pk))
    flash(ui('%(name)s was deleted.', name=name), "ok")
    return redirect(url_for("admin.dashboard"))



@bp.get("/bookings")
def bookings():
    return render_template(
        "admin/bookings.html",
        bookings=_booking_query().limit(100).all(),
        term=request.args.get("q", "").strip()[:100],
        status=request.args.get("status", "").strip(),
        statuses=BOOKING_STATUSES,
    )


@bp.get("/bookings.csv")
def bookings_csv():
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Booking reference",
            "Customer",
            "Email",
            "Space",
            "Pickup",
            "Return",
            "Status",
            "Payment",
            "Total",
            "Currency",
        ]
    )
    for booking in _booking_query().limit(5000).all():
        writer.writerow(
            [
                _csv_value(booking.booking_ref),
                _csv_value(f"{booking.customer.first_name} {booking.customer.last_name}".strip()),
                _csv_value(booking.customer.email),
                _csv_value(booking.space.name),
                _csv_value(booking.pickup_at.isoformat()),
                _csv_value(booking.return_at.isoformat()),
                _csv_value(booking.status),
                _csv_value(booking.payment_status),
                _csv_value(booking.total_amount),
                _csv_value(booking.currency),
            ]
        )
    response = make_response(output.getvalue())
    response.mimetype = "text/csv"
    response.headers["Content-Disposition"] = 'attachment; filename="parkpilot-bookings.csv"'
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/calendar")
def calendar_view():
    now = datetime.now(timezone.utc)
    month_value = request.args.get("month", "")
    try:
        year, month = (
            (int(part) for part in month_value.split("-", 1))
            if month_value
            else (now.year, now.month)
        )
        first_day = datetime(year, month, 1, tzinfo=timezone.utc)
        last_day = calendar.monthrange(year, month)[1]
    except (ValueError, TypeError):
        abort(400)
    if year < 2000 or year > 2100:
        abort(400)
    next_month = datetime(
        year + (month == 12),
        1 if month == 12 else month + 1,
        1,
        tzinfo=timezone.utc,
    )
    bookings_in_month = Booking.query.filter(
        Booking.status != "cancelled",
        Booking.pickup_at < next_month,
        Booking.return_at > first_day,
    ).all()
    space_count = db.session.scalar(
        select(func.count()).select_from(Space).where(
            Space.status != "blocked",
            Space.default_available.is_(True),
        )
    ) or 0
    occupancy = []
    for day in range(1, last_day + 1):
        current_day = datetime(year, month, day, tzinfo=timezone.utc)
        following_day = (
            current_day.replace(day=day + 1) if day < last_day else next_month
        )
        occupied_spaces = {
            booking.space_id
            for booking in bookings_in_month
            if _as_utc(booking.pickup_at) < following_day
            and _as_utc(booking.return_at) > current_day
        }
        occupied = len(occupied_spaces)
        occupancy.append(
            {
                "day": day,
                "occupied": occupied,
                "spaces": space_count,
                "percent": round(occupied * 100 / space_count) if space_count else 0,
            }
        )
    return render_template(
        "admin/calendar.html",
        occupancy=occupancy,
        month=first_day.strftime("%Y-%m"),
        month_label=first_day.strftime("%B %Y"),
        space_count=space_count,
    )


@bp.route("/team", methods=["GET", "POST"])
@owner_required
def team():
    form = TeamMemberForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        with use_tenant(None):
            existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            form.email.errors.append("An account with this email already exists.")
        else:
            member = User(
                tenant_id=current_user.tenant_id,
                name=form.name.data.strip(),
                email=email,
                role=Role.STAFF,
                phone=normalize_phone(form.phone.data) if (form.phone.data or "").strip() else None,
            )
            member.set_password(form.password.data)
            db.session.add(member)
            db.session.flush()
            audit("team.member.create", "user", member.id, role=member.role)
            db.session.commit()
            flash(ui("Staff account created."), "ok")
            return redirect(url_for("admin.team"))
    members = User.query.filter(User.role.in_([Role.OWNER, Role.STAFF])).order_by(User.name, User.email).all()
    return render_template("admin/team.html", form=form, members=members)


@bp.post("/team/<int:user_id>/active")
@owner_required
def team_member_active(user_id):
    member = db.get_or_404(User, user_id)
    if member.role != Role.STAFF or member.id == current_user.id:
        abort(403)
    member.active = not member.active
    audit("team.member.active", "user", member.id, active=member.active)
    db.session.commit()
    flash(ui('%(email)s is %(p1)s.', email=member.email, p1=ui('active' if member.active else 'inactive')), "ok")
    return redirect(url_for("admin.team"))


@bp.route("/team/<int:user_id>/edit", methods=["GET", "POST"])
@owner_required
def team_member_edit(user_id):
    member = db.get_or_404(User, user_id)
    if member.role != Role.STAFF:
        abort(403)
    form = TeamMemberEditForm(obj=member)
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        with use_tenant(None):
            clash = User.query.filter(User.email == email, User.id != member.id).first()
        if clash:
            form.email.errors.append("An account with this email already exists.")
        else:
            member.name = form.name.data.strip()
            member.email = email
            member.phone = normalize_phone(form.phone.data) if (form.phone.data or "").strip() else None
            if form.password.data:
                member.set_password(form.password.data)
                member.failed_logins, member.locked_until = 0, None
            audit("team.member.update", "user", member.id, password_changed=bool(form.password.data))
            db.session.commit()
            flash(ui("Staff account saved."), "ok")
            return redirect(url_for("admin.team"))
    return render_template("admin/team_edit.html", form=form, member=member)


@bp.post("/team/<int:user_id>/delete")
@owner_required
def team_member_delete(user_id):
    member = db.get_or_404(User, user_id)
    if member.role != Role.STAFF or member.id == current_user.id:
        abort(403)
    email = member.email
    audit("team.member.delete", "user", member.id, email=email)
    for model, column in (
        (Inspection, "staff_user_id"), (InspectionPhoto, "staff_user_id"), (BookingExtra, "done_by_id"),
        (AuditLog, "user_id"), (ApiAccessRequest, "requested_by_id"), (ApiAccessRequest, "decided_by_id"),
    ):
        db.session.execute(update(model).where(getattr(model, column) == member.id).values({column: None}))
    db.session.delete(member)
    db.session.commit()
    flash(ui('%(email)s was deleted.', email=email), "ok")
    return redirect(url_for("admin.team"))


def _lots_by_tenant():
    grouped = {}
    for lot in Lot.query.filter(Lot.active.is_(True)).order_by(Lot.name).all():
        grouped.setdefault(lot.tenant_id, []).append(lot)
    return grouped


def _platform_api_page(**extra):
    response = make_response(render_template(
        "admin/platform_api.html", bundles=API_BUNDLES, pricing=pricing_config(), events=sorted(WEBHOOK_EVENTS),
        tenants=Tenant.query.order_by(Tenant.name).all(),
        keys=ApiKey.query.order_by(ApiKey.created_at.desc()).limit(100).all(),
        endpoints=WebhookEndpoint.query.order_by(WebhookEndpoint.created_at.desc()).limit(100).all(),
        requests=ApiAccessRequest.query.order_by(
            case((ApiAccessRequest.status == "pending", 0), else_=1), ApiAccessRequest.created_at.desc()).limit(50).all(),
        tenant_names={t.id: t.name for t in Tenant.query.all()},
        lots_by_tenant=_lots_by_tenant(), lot_names={lot.id: lot.name for lot in Lot.query.all()},
        mrr=sum((k.monthly_price or 0) for k in ApiKey.query.filter(ApiKey.active.is_(True)).all()), **extra))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/platform/api")
@platform_owner_required
def platform_api():
    return _platform_api_page()


@bp.post("/platform/api/keys")
@platform_owner_required
def platform_api_key_issue():
    try:
        tenant = db.get_or_404(Tenant, int(request.form.get("tenant_id", "0") or 0))
        origins = [o.strip() for o in (request.form.get("allowed_origins") or "").split(",") if o.strip()]
        name = (request.form.get("name") or "").strip()
        webhook_secret = None
        if request.form.get("bundle") and not request.form.getlist("features"):
            key, token = issue_bundle_key(tenant_id=tenant.id, bundle=request.form["bundle"], name=name, allowed_origins=origins)
            issued = describe_key(key)
        else:
            key, token, issued = issue_partner_access(
                tenant_id=tenant.id, name=name, features=request.form.getlist("features"),
                lot_ids=[int(v) for v in request.form.getlist("lot_ids") if v.isdigit()],
                allowed_origins=origins, webhook_url=request.form.get("webhook_url", ""),
            )
            webhook_secret = (issued.get("webhook") or {}).get("secret")
    except (ApiPlatformError, ValueError) as exc:
        flash(ui(str(exc)), "error")
        return redirect(url_for("admin.platform_api"))
    audit("api_key.issue", "api_key", key.id, tenant_id=tenant.id, prefix=key.prefix, monthly_price=str(key.monthly_price))
    db.session.commit()
    return _platform_api_page(issued_token=token, issued_for=tenant.name, issued=issued, tenant_for_issue=tenant)


@bp.post("/platform/api/requests/<int:request_id>/<decision>")
@platform_owner_required
def platform_api_request_decide(request_id, decision):
    item = db.get_or_404(ApiAccessRequest, request_id)
    if item.status != "pending" or decision not in ("approve", "reject"):
        abort(409)
    token = None
    if decision == "approve":
        try:
            key, token = issue_bundle_key(tenant_id=item.tenant_id, bundle=item.bundle)
        except ApiPlatformError as exc:
            flash(ui(str(exc)), "error")
            return redirect(url_for("admin.platform_api"))
        item.api_key_id = key.id
        item.status = "approved"
    else:
        item.status = "rejected"
    item.decided_by_id = current_user.id
    item.decided_at = utcnow()
    audit(f"api_access.{decision}", "api_access_request", item.id, bundle=item.bundle)
    db.session.commit()
    if token:
        return _platform_api_page(issued_token=token, issued_for=tenant_names_for(item.tenant_id), issued=describe_key(key))
    flash(ui("Request rejected."), "ok")
    return redirect(url_for("admin.platform_api"))


def tenant_names_for(tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    return tenant.name if tenant else ""


@bp.post("/platform/api/keys/<int:key_id>/revoke")
@platform_owner_required
def platform_api_key_revoke(key_id):
    key = db.get_or_404(ApiKey, key_id)
    if key.active:
        key.active = False
        key.revoked_at = utcnow()
        audit("api_key.revoke", "api_key", key.id, prefix=key.prefix)
        db.session.commit()
    flash(ui('API key %(prefix)s has been revoked.', prefix=key.prefix), "ok")
    return redirect(url_for("admin.platform_api"))


@bp.post("/platform/api/webhooks")
@platform_owner_required
def platform_webhook_create():
    try:
        tenant = db.get_or_404(Tenant, int(request.form.get("tenant_id", "0") or 0))
        endpoint, secret = create_webhook_endpoint(
            tenant_id=tenant.id, name=(request.form.get("name") or "").strip(),
            url=(request.form.get("url") or "").strip(), events=request.form.getlist("events"))
    except (ApiPlatformError, ValueError) as exc:
        flash(ui(str(exc)), "error")
        return redirect(url_for("admin.platform_api"))
    audit("webhook.create", "webhook_endpoint", endpoint.id, tenant_id=tenant.id)
    db.session.commit()
    return _platform_api_page(one_time_secret=secret, issued_for=tenant.name)


@bp.post("/platform/api/webhooks/<int:endpoint_id>/rotate")
@platform_owner_required
def platform_webhook_rotate(endpoint_id):
    endpoint = db.get_or_404(WebhookEndpoint, endpoint_id)
    if not endpoint.active:
        abort(409)
    endpoint.signing_salt = secrets.token_hex(32)
    audit("webhook.secret.rotate", "webhook_endpoint", endpoint.id)
    db.session.commit()
    return _platform_api_page(one_time_secret=webhook_signing_secret(endpoint), issued_for=tenant_names_for(endpoint.tenant_id))


@bp.post("/platform/api/webhooks/<int:endpoint_id>/revoke")
@platform_owner_required
def platform_webhook_revoke(endpoint_id):
    endpoint = db.get_or_404(WebhookEndpoint, endpoint_id)
    if endpoint.active:
        endpoint.active = False
        endpoint.revoked_at = utcnow()
        audit("webhook.revoke", "webhook_endpoint", endpoint.id)
        db.session.commit()
    flash(ui('Webhook endpoint %(name)s has been disabled.', name=endpoint.name), "ok")
    return redirect(url_for("admin.platform_api"))


def _key_form_values(key: ApiKey) -> dict:
    return {
        "name": key.name, "scopes": list(key.scopes or []), "allowed_origins": ", ".join(key.allowed_origins or []),
        "requests_per_minute": key.requests_per_minute, "active": key.active,
    }


@bp.route("/platform/api/keys/<int:key_id>/edit", methods=["GET", "POST"])
@platform_owner_required
def platform_api_key_edit(key_id):
    key = db.get_or_404(ApiKey, key_id)
    tenant_lots = Lot.query.filter(Lot.tenant_id == key.tenant_id, Lot.active.is_(True)).order_by(Lot.name).all()
    scope_choices = sorted(PUBLIC_SCOPES if key.key_type == "pk" else SECRET_SCOPES)
    values = _key_form_values(key)
    if request.method == "POST":
        values = {
            "name": request.form.get("name", ""), "scopes": request.form.getlist("scopes"),
            "allowed_origins": request.form.get("allowed_origins", ""),
            "requests_per_minute": request.form.get("requests_per_minute", ""), "active": bool(request.form.get("active")),
        }
        try:
            lot_ids = [int(v) for v in request.form.getlist("lot_ids") if v.isdigit()]
            change_lots = bool(request.form.get("lots_present"))
            if change_lots and (not lot_ids or not set(lot_ids) <= {lot.id for lot in tenant_lots}):
                raise ApiPlatformError("Choose at least one map that belongs to this partner.")
            update_api_key(
                key, name=values["name"], scopes=values["scopes"],
                allowed_origins=values["allowed_origins"].split(","),
                requests_per_minute=int(values["requests_per_minute"]), active=values["active"],
                allowed_lot_ids=lot_ids, set_lots=change_lots,
            )
        except ValueError:
            db.session.rollback()
            flash(ui("Requests per minute must be a whole number."), "error")
        except ApiPlatformError as exc:
            db.session.rollback()
            flash(ui(str(exc)), "error")
        else:
            audit("api_key.update", "api_key", key.id, prefix=key.prefix)
            db.session.commit()
            flash(ui('API key %(prefix)s saved.', prefix=key.prefix), "ok")
            return redirect(url_for("admin.platform_api"))
    return render_template(
        "admin/platform_key_edit.html", key=key, values=values, scope_choices=scope_choices,
        tenant_lots=tenant_lots, selected_lots=set(key.allowed_lot_ids or [lot.id for lot in tenant_lots]),
        tenant_name=tenant_names_for(key.tenant_id),
    )


@bp.post("/platform/api/keys/<int:key_id>/delete")
@platform_owner_required
def platform_api_key_delete(key_id):
    key = db.get_or_404(ApiKey, key_id)
    prefix = key.prefix
    audit("api_key.delete", "api_key", key.id, prefix=prefix)
    # SQLite does not enforce ON DELETE here, so clear the dependent rows explicitly.
    db.session.execute(delete(UsageLog).where(UsageLog.api_key_id == key.id))
    db.session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.api_key_id == key.id))
    db.session.execute(update(ApiAccessRequest).where(ApiAccessRequest.api_key_id == key.id).values(api_key_id=None))
    db.session.delete(key)
    db.session.commit()
    flash(ui('API key %(prefix)s deleted permanently.', prefix=prefix), "ok")
    return redirect(url_for("admin.platform_api"))


@bp.route("/platform/api/webhooks/<int:endpoint_id>/edit", methods=["GET", "POST"])
@platform_owner_required
def platform_webhook_edit(endpoint_id):
    endpoint = db.get_or_404(WebhookEndpoint, endpoint_id)
    values = {"name": endpoint.name, "url": endpoint.url, "events": list(endpoint.events or []), "active": endpoint.active}
    if request.method == "POST":
        values = {
            "name": request.form.get("name", ""), "url": request.form.get("url", ""),
            "events": request.form.getlist("events"), "active": bool(request.form.get("active")),
        }
        try:
            update_webhook_endpoint(endpoint, **values)
        except ApiPlatformError as exc:
            db.session.rollback()
            flash(ui(str(exc)), "error")
        else:
            audit("webhook.update", "webhook_endpoint", endpoint.id)
            db.session.commit()
            flash(ui('Webhook %(name)s saved.', name=endpoint.name), "ok")
            return redirect(url_for("admin.platform_api"))
    return render_template(
        "admin/platform_webhook_edit.html", endpoint=endpoint, values=values, events=sorted(WEBHOOK_EVENTS),
        tenant_name=tenant_names_for(endpoint.tenant_id),
    )


@bp.post("/platform/api/webhooks/<int:endpoint_id>/delete")
@platform_owner_required
def platform_webhook_delete(endpoint_id):
    endpoint = db.get_or_404(WebhookEndpoint, endpoint_id)
    name = endpoint.name
    audit("webhook.delete", "webhook_endpoint", endpoint.id)
    db.session.execute(delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id))
    db.session.delete(endpoint)
    db.session.commit()
    flash(ui('Webhook %(name)s deleted permanently.', name=name), "ok")
    return redirect(url_for("admin.platform_api"))


@bp.get("/platform/usage")
@platform_owner_required
def platform_usage():
    """API usage across all parkings (super admin only)."""
    tenant_id = request.args.get("tenant_id", type=int)
    filters = [UsageLog.tenant_id == tenant_id] if tenant_id else []
    usage_by_path = db.session.execute(
        select(
            UsageLog.method,
            UsageLog.path,
            func.count(UsageLog.id),
            func.sum(case((UsageLog.status_code >= 400, 1), else_=0)),
            func.max(UsageLog.created_at),
        )
        .where(*filters)
        .group_by(UsageLog.method, UsageLog.path)
        .order_by(desc(func.count(UsageLog.id)), UsageLog.path)
        .limit(100)
    ).all()
    usage_by_tenant = db.session.execute(
        select(
            UsageLog.tenant_id,
            func.count(UsageLog.id),
            func.sum(case((UsageLog.status_code >= 400, 1), else_=0)),
            func.max(UsageLog.created_at),
        )
        .group_by(UsageLog.tenant_id)
        .order_by(desc(func.count(UsageLog.id)))
    ).all()
    total_calls = db.session.scalar(select(func.count()).select_from(UsageLog).where(*filters)) or 0
    failed_calls = db.session.scalar(
        select(func.count()).select_from(UsageLog).where(UsageLog.status_code >= 400, *filters)
    ) or 0
    return render_template(
        "admin/usage.html",
        usage_by_path=usage_by_path,
        usage_by_tenant=usage_by_tenant,
        tenants=Tenant.query.order_by(Tenant.name).all(),
        tenant_names={t.id: t.name for t in Tenant.query.all()},
        selected_tenant=tenant_id,
        total_calls=total_calls,
        failed_calls=failed_calls,
    )


# --- tenant pricing, discounts, branding, and content -------------------------

def _pricing_form_choices(form: PriceRuleForm, selected_lot_id: int | None = None) -> Lot | None:
    lots = Lot.query.order_by(Lot.name).all()
    form.lot_id.choices = [(lot.id, lot.name) for lot in lots]
    form.space_id.choices = [(0, "Entire selected lot")]
    lot = next((row for row in lots if row.id == selected_lot_id), None)
    spaces = db.session.execute(
        select(Space, Lot.name)
        .join(Lot, Lot.id == Space.lot_id)
        .where(Space.tenant_id == current_user.tenant_id)
        .order_by(Lot.name, Space.name)
    ).all()
    form.space_id.choices.extend(
        (space.id, f"{lot_name} — {space.name}") for space, lot_name in spaces
    )
    return lot


@bp.route("/pricing", methods=["GET", "POST"])
@owner_required
def pricing_rules():
    form = PriceRuleForm()
    try:
        selected_lot_id = int(request.form.get("lot_id") or request.args.get("lot_id") or 0)
    except ValueError:
        selected_lot_id = 0
    _pricing_form_choices(form, selected_lot_id or None)
    if form.validate_on_submit():
        lot = Lot.query.filter_by(id=form.lot_id.data).first()
        space_id = form.space_id.data or None
        if lot is None:
            form.lot_id.errors.append("Choose one of your parking lots.")
        elif space_id is not None and Space.query.filter_by(id=space_id, lot_id=lot.id).first() is None:
            form.space_id.errors.append("Choose a space from the selected parking lot.")
        elif (
            form.period.data in {"season", "holiday"}
            and (form.start_day.data is None or form.end_day.data is None)
        ):
            form.start_day.errors.append("Season and holiday rules require both dates.")
        elif (
            form.start_day.data is not None
            and form.end_day.data is not None
            and form.start_day.data > form.end_day.data
        ):
            form.end_day.errors.append("End date must be on or after the start date.")
        else:
            rule = PriceRule(
                tenant_id=current_user.tenant_id,
                lot_id=lot.id,
                space_id=space_id,
                name=form.name.data.strip(),
                period=form.period.data,
                mode=form.mode.data,
                amount=form.amount.data,
                start_day=form.start_day.data,
                end_day=form.end_day.data,
                priority=form.priority.data,
                active=form.active.data,
                notes=(form.notes.data or "").strip(),
            )
            db.session.add(rule)
            db.session.flush()
            audit("pricing_rule.create", "price_rule", rule.id, lot_id=lot.id, priority=rule.priority)
            db.session.commit()
            flash(ui("Pricing rule created."), "ok")
            return redirect(url_for("admin.pricing_rules"))
    return render_template(
        "admin/pricing.html",
        form=form,
        rules=PriceRule.query.order_by(PriceRule.priority.desc(), PriceRule.id).all(),
    )


@bp.route("/pricing/<int:rule_id>/edit", methods=["GET", "POST"])
@owner_required
def pricing_rule_edit(rule_id):
    rule = _owned_record_or_404(PriceRule, rule_id)
    form = PriceRuleForm(obj=rule)
    if request.method == "GET":
        form.space_id.data = rule.space_id or 0
    try:
        selected_lot_id = int(request.form.get("lot_id") or request.args.get("lot_id") or rule.lot_id)
    except ValueError:
        selected_lot_id = rule.lot_id
    _pricing_form_choices(form, selected_lot_id)
    if form.validate_on_submit():
        lot = Lot.query.filter_by(id=form.lot_id.data).first()
        space_id = form.space_id.data or None
        if lot is None:
            form.lot_id.errors.append("Choose one of your parking lots.")
        elif space_id is not None and Space.query.filter_by(id=space_id, lot_id=lot.id).first() is None:
            form.space_id.errors.append("Choose a space from the selected parking lot.")
        elif form.period.data in {"season", "holiday"} and (
            form.start_day.data is None or form.end_day.data is None
        ):
            form.start_day.errors.append("Season and holiday rules require both dates.")
        elif form.start_day.data and form.end_day.data and form.start_day.data > form.end_day.data:
            form.end_day.errors.append("End date must be on or after the start date.")
        else:
            form.populate_obj(rule)
            rule.name = rule.name.strip()
            rule.lot_id = lot.id
            rule.space_id = space_id
            rule.notes = (rule.notes or "").strip()
            audit("pricing_rule.update", "price_rule", rule.id, lot_id=lot.id, priority=rule.priority)
            db.session.commit()
            flash(ui("Pricing rule saved."), "ok")
            return redirect(url_for("admin.pricing_rules"))
    return render_template("admin/pricing_rule_form.html", form=form, rule=rule)


@bp.post("/pricing/<int:rule_id>/delete")
@owner_required
def pricing_rule_delete(rule_id):
    rule = _owned_record_or_404(PriceRule, rule_id)
    audit("pricing_rule.delete", "price_rule", rule.id)
    db.session.delete(rule)
    db.session.commit()
    flash(ui("Pricing rule deleted."), "ok")
    return redirect(url_for("admin.pricing_rules"))


@bp.route("/discounts", methods=["GET", "POST"])
@owner_required
def discounts():
    form = DiscountCodeForm()
    if form.validate_on_submit():
        normalized_code = form.code.data.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,39}", normalized_code):
            form.code.errors.append("Use letters, numbers, hyphens, or underscores.")
        elif form.discount_type.data == "percentage" and form.value.data > 100:
            form.value.errors.append("A percentage discount cannot exceed 100.")
        elif form.starts_on.data and form.ends_on.data and form.starts_on.data > form.ends_on.data:
            form.ends_on.errors.append("Valid-through date must be on or after the start date.")
        elif DiscountCode.query.filter_by(code=normalized_code).first():
            form.code.errors.append("This code already exists for your tenant.")
        else:
            code = DiscountCode(
                tenant_id=current_user.tenant_id,
                code=normalized_code,
                discount_type=form.discount_type.data,
                value=form.value.data,
                starts_on=form.starts_on.data,
                ends_on=form.ends_on.data,
                minimum_amount=form.minimum_amount.data,
                max_redemptions=form.max_redemptions.data,
                active=form.active.data,
            )
            db.session.add(code)
            db.session.flush()
            audit("discount_code.create", "discount_code", code.id, code=code.code)
            db.session.commit()
            flash(ui("Discount code created."), "ok")
            return redirect(url_for("admin.discounts"))
    codes = DiscountCode.query.order_by(DiscountCode.created_at.desc(), DiscountCode.id.desc()).all()
    return render_template("admin/discounts.html", form=form, codes=codes)


@bp.post("/discounts/<int:code_id>/active")
@owner_required
def discount_toggle(code_id):
    code = _owned_record_or_404(DiscountCode, code_id)
    code.active = not code.active
    audit("discount_code.active", "discount_code", code.id, active=code.active)
    db.session.commit()
    flash(ui('Discount code %(code)s is %(p1)s.', code=code.code, p1=ui('active' if code.active else 'inactive')), "ok")
    return redirect(url_for("admin.discounts"))


@bp.route("/discounts/<int:code_id>/edit", methods=["GET", "POST"])
@owner_required
def discount_edit(code_id):
    code = _owned_record_or_404(DiscountCode, code_id)
    form = DiscountCodeForm(obj=code)
    if form.validate_on_submit():
        normalized_code = form.code.data.strip().upper()
        duplicate = DiscountCode.query.filter(
            DiscountCode.code == normalized_code,
            DiscountCode.id != code.id,
        ).first()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,39}", normalized_code):
            form.code.errors.append("Use letters, numbers, hyphens, or underscores.")
        elif duplicate:
            form.code.errors.append("This code already exists for your tenant.")
        elif form.discount_type.data == "percentage" and form.value.data > 100:
            form.value.errors.append("A percentage discount cannot exceed 100.")
        elif form.starts_on.data and form.ends_on.data and form.starts_on.data > form.ends_on.data:
            form.ends_on.errors.append("Valid-through date must be on or after the start date.")
        else:
            code.code = normalized_code
            code.discount_type = form.discount_type.data
            code.value = form.value.data
            code.starts_on = form.starts_on.data
            code.ends_on = form.ends_on.data
            code.minimum_amount = form.minimum_amount.data
            code.max_redemptions = form.max_redemptions.data
            code.active = form.active.data
            audit("discount_code.update", "discount_code", code.id, code=code.code)
            db.session.commit()
            flash(ui("Discount code saved."), "ok")
            return redirect(url_for("admin.discounts"))
    return render_template("admin/discount_form.html", form=form, code=code)


@bp.get("/brand/logo.png")
@owner_required
def brand_logo():
    tenant = db.session.get(Tenant, current_user.tenant_id)
    if tenant is None or not tenant.logo_data:
        abort(404)
    return Response(tenant.logo_data, mimetype=tenant.logo_mime or "image/png", headers={"Cache-Control": "private, max-age=300"})


@bp.route("/brand", methods=["GET", "POST"])
@owner_required
def brand_settings():
    tenant = db.session.get(Tenant, current_user.tenant_id)
    if tenant is None:
        abort(404)
    form = BrandSettingsForm(obj=tenant)
    if form.validate_on_submit():
        primary = (form.primary_color.data or "").upper()
        accent = (form.accent_color.data or "").upper()
        domain = (form.custom_domain.data or "").strip().lower() or None
        if form.logo_url.data and not form.logo_url.data.strip().lower().startswith("https://"):
            form.logo_url.errors.append("Logo URLs must use HTTPS.")
        if not re.fullmatch(r"#[0-9A-F]{6}", primary):
            form.primary_color.errors.append("Use a six-digit hexadecimal color such as #0F4C5C.")
        if not re.fullmatch(r"#[0-9A-F]{6}", accent):
            form.accent_color.errors.append("Use a six-digit hexadecimal color such as #E09F1F.")
        if domain and (
            "://" in domain
            or "/" in domain
            or not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+",
                domain,
            )
        ):
            form.custom_domain.errors.append("Enter a hostname only, for example booking.example.com.")
        domain_owner = db.session.execute(
            select(Tenant.id).where(Tenant.custom_domain == domain, Tenant.id != tenant.id)
        ).scalar_one_or_none() if domain else None
        if domain_owner is not None:
            form.custom_domain.errors.append("That custom domain is already assigned to another tenant.")
        logo_upload = request.files.get("logo_file")
        if logo_upload is not None and logo_upload.filename:
            try:
                tenant.logo_data, tenant.logo_mime = process_logo(logo_upload)
            except ValueError as exc:
                form.logo_url.errors.append(str(exc))
        elif request.form.get("remove_logo"):
            tenant.logo_data = tenant.logo_mime = None
        if not form.primary_color.errors and not form.accent_color.errors and not form.custom_domain.errors and not form.logo_url.errors:
            tenant.logo_url = (form.logo_url.data or "").strip() or None
            tenant.primary_color = primary
            tenant.accent_color = accent
            tenant.custom_domain = domain
            audit("brand.update", "tenant", tenant.id, custom_domain=domain)
            db.session.commit()
            flash(ui("Brand settings saved. DNS and TLS still need to be configured with your provider."), "ok")
            return redirect(url_for("admin.brand_settings"))
    return render_template("admin/brand.html", form=form, tenant=tenant)


def _translation_choices(form: TenantTranslationForm, selected_locale: str | None = None) -> None:
    locales = supported_locales()
    form.locale.choices = [(locale, locale.upper()) for locale in locales]
    locale = selected_locale if selected_locale in locales else (locales[0] if locales else "")
    form.key.choices = [(key, key) for key in translation_keys(locale, "")]


@bp.route("/translations", methods=["GET", "POST"])
@owner_required
def translations():
    form = TenantTranslationForm()
    _translation_choices(form, request.form.get("locale"))
    if form.validate_on_submit():
        if not valid_translation_override(form.locale.data, form.key.data, form.value.data):
            form.value.errors.append("Keep the same named placeholders as the platform translation.")
        else:
            override = TenantTranslation.query.filter_by(locale=form.locale.data, key=form.key.data).first()
            if override is None:
                override = TenantTranslation(
                    tenant_id=current_user.tenant_id,
                    locale=form.locale.data,
                    key=form.key.data,
                    value=form.value.data.strip(),
                )
                db.session.add(override)
                action = "translation.create"
            else:
                override.value = form.value.data.strip()
                action = "translation.update"
            db.session.flush()
            audit(action, "tenant_translation", override.id, locale=override.locale, key=override.key)
            db.session.commit()
            flash(ui("Translation override saved."), "ok")
            return redirect(url_for("admin.translations"))
    overrides = TenantTranslation.query.order_by(TenantTranslation.locale, TenantTranslation.key).all()
    return render_template("admin/translations.html", form=form, overrides=overrides)


@bp.post("/translations/<int:translation_id>/delete")
@owner_required
def translation_delete(translation_id):
    override = _owned_record_or_404(TenantTranslation, translation_id)
    audit("translation.delete", "tenant_translation", override.id, locale=override.locale, key=override.key)
    db.session.delete(override)
    db.session.commit()
    flash(ui("Translation override removed."), "ok")
    return redirect(url_for("admin.translations"))


def _content_form_choices(form: TenantPageForm) -> None:
    form.locale.choices = [(locale, locale.upper()) for locale in supported_locales()]


def _save_page_fields(form: TenantPageForm, page: TenantPage) -> None:
    page.kind = form.kind.data
    page.locale = form.locale.data
    page.slug = slugify(form.slug.data)[:160]
    page.title = form.title.data.strip()
    page.summary = (form.summary.data or "").strip()
    page.body = form.body.data.strip()
    page.seo_title = (form.seo_title.data or "").strip()
    page.seo_description = (form.seo_description.data or "").strip()
    page.published = bool(form.published.data)
    if page.published and page.published_at is None:
        page.published_at = utcnow()
    elif not page.published:
        page.published_at = None


@bp.get("/content")
@owner_required
def content_list():
    kind = request.args.get("kind", "")
    if kind not in {"", "blog", "faq"}:
        abort(400)
    query = TenantPage.query
    if kind:
        query = query.filter_by(kind=kind)
    pages = query.order_by(TenantPage.kind, TenantPage.locale, TenantPage.title).all()
    return render_template("admin/content.html", pages=pages, kind=kind)


@bp.route("/content/new", methods=["GET", "POST"])
@owner_required
def content_new():
    form = TenantPageForm()
    _content_form_choices(form)
    if form.validate_on_submit():
        slug = slugify(form.slug.data)[:160]
        duplicate = TenantPage.query.filter_by(
            kind=form.kind.data,
            locale=form.locale.data,
            slug=slug,
        ).first()
        if not slug:
            form.slug.errors.append("Enter a slug with letters or numbers.")
        elif duplicate:
            form.slug.errors.append("This URL already exists for this content type and language.")
        else:
            page = TenantPage(tenant_id=current_user.tenant_id)
            _save_page_fields(form, page)
            db.session.add(page)
            db.session.flush()
            audit("content.create", "tenant_page", page.id, kind=page.kind, published=page.published)
            db.session.commit()
            flash(ui("Content created."), "ok")
            return redirect(url_for("admin.content_list"))
    return render_template("admin/content_form.html", form=form, page=None)


@bp.route("/content/<int:page_id>/edit", methods=["GET", "POST"])
@owner_required
def content_edit(page_id):
    page = _owned_record_or_404(TenantPage, page_id)
    form = TenantPageForm(obj=page)
    _content_form_choices(form)
    if form.validate_on_submit():
        slug = slugify(form.slug.data)[:160]
        duplicate = TenantPage.query.filter(
            TenantPage.kind == form.kind.data,
            TenantPage.locale == form.locale.data,
            TenantPage.slug == slug,
            TenantPage.id != page.id,
        ).first()
        if not slug:
            form.slug.errors.append("Enter a slug with letters or numbers.")
        elif duplicate:
            form.slug.errors.append("This URL already exists for this content type and language.")
        else:
            _save_page_fields(form, page)
            audit("content.update", "tenant_page", page.id, kind=page.kind, published=page.published)
            db.session.commit()
            flash(ui("Content saved."), "ok")
            return redirect(url_for("admin.content_list"))
    return render_template("admin/content_form.html", form=form, page=page)


@bp.post("/content/<int:page_id>/delete")
@owner_required
def content_delete(page_id):
    page = _owned_record_or_404(TenantPage, page_id)
    audit("content.delete", "tenant_page", page.id, kind=page.kind)
    db.session.delete(page)
    db.session.commit()
    flash(ui("Content deleted."), "ok")
    return redirect(url_for("admin.content_list"))


# --- lots -----------------------------------------------------------------------

@bp.get("/lots")
def lots():
    counts = dict(db.session.execute(select(Space.lot_id, func.count()).group_by(Space.lot_id)).all())
    return render_template("admin/lots.html", lots=Lot.query.order_by(Lot.name).all(), counts=counts)


SPACE_W, SPACE_H, SPACE_GAP, SPACE_MARGIN = 80, 110, 10, 40


def _grid_capacity(canvas_w: int, canvas_h: int) -> tuple[int, int]:
    """Columns and rows of default-size spaces that fit on the lot map."""
    cols = max(0, (canvas_w - 2 * SPACE_MARGIN + SPACE_GAP) // (SPACE_W + SPACE_GAP))
    rows = max(0, (canvas_h - 2 * SPACE_MARGIN + SPACE_GAP) // (SPACE_H + SPACE_GAP))
    return cols, rows


def _row_label(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    label = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        label = chr(65 + rem) + label
    return label


@bp.route("/lots/new", methods=["GET", "POST"])
@owner_required
def lot_new():
    form = LotForm()
    if form.validate_on_submit():
        space_count = form.space_count.data or 1
        cols, rows = _grid_capacity(form.canvas_w.data, form.canvas_h.data)
        if space_count > cols * rows:
            form.space_count.errors.append(
                f"Only {cols * rows} spaces fit on a {form.canvas_w.data}x{form.canvas_h.data} map. "
                "Lower the number or enlarge the map."
            )
            return render_template("admin/lot_form.html", form=form, lot=None)
        covered_count, ev_count = form.covered_count.data or 0, form.ev_count.data or 0
        if covered_count > space_count or ev_count > space_count:
            form.covered_count.errors.append("Covered and EV charging spaces cannot exceed the number of spaces.")
            return render_template("admin/lot_form.html", form=form, lot=None)
        plan_message = billing.space_limit_message(current_user.tenant_id, space_count)
        if plan_message:
            flash(ui(plan_message), "error")
            return render_template("admin/lot_form.html", form=form, lot=None)
        del form.space_count, form.covered_count, form.ev_count  # not Lot columns
        lot = Lot(slug=_unique_slug(slugify(form.name.data)))
        form.populate_obj(lot)
        lot.currency = (lot.currency or "EUR").upper()[:3]
        lot.base_daily_rate = float(lot.base_daily_rate or 0)
        db.session.add(lot)
        db.session.flush()

        for index in range(space_count):
            col, row = index % cols, index // cols
            db.session.add(
                Space(
                    tenant_id=lot.tenant_id,
                    lot_id=lot.id,
                    name=f"{_row_label(row)}{col + 1}",
                    kind="small",
                    status="active",
                    covered=index < covered_count,  # first N spaces; adjust per space later
                    ev_charging=index >= space_count - ev_count,  # last N spaces
                    x=SPACE_MARGIN + col * (SPACE_W + SPACE_GAP),
                    y=SPACE_MARGIN + row * (SPACE_H + SPACE_GAP),
                    w=SPACE_W,
                    h=SPACE_H,
                    base_price=lot.base_daily_rate or 0,
                    min_hours=1,
                    max_days=30,
                    default_available=True,
                )
            )
        audit("lot.create", "lot", lot.id, name=lot.name)
        db.session.commit()
        flash(ui("Lot created."), "ok")
        return redirect(url_for("admin.layout", lot_id=lot.id))
    return render_template("admin/lot_form.html", form=form, lot=None)


def _resize_lot_spaces(lot: Lot, target: int, canvas_w: int, canvas_h: int) -> str | None:
    """Grow or shrink a lot to `target` spaces. Returns an error message, or None when done.

    New spaces fill free grid slots. Only spaces with no bookings or holds are removed (newest first),
    because deleting a space also deletes its bookings."""
    spaces = list(lot.spaces)
    if target == len(spaces):
        return None
    if target > len(spaces):
        cols, rows = _grid_capacity(canvas_w, canvas_h)
        taken_names = {s.name for s in spaces}
        taken_pos = {(s.x, s.y) for s in spaces}
        slots = []
        for index in range(cols * rows):
            col, row = index % cols, index // cols
            name = f"{_row_label(row)}{col + 1}"
            pos = (SPACE_MARGIN + col * (SPACE_W + SPACE_GAP), SPACE_MARGIN + row * (SPACE_H + SPACE_GAP))
            if name not in taken_names and pos not in taken_pos:
                slots.append((name, pos))
        needed = target - len(spaces)
        plan_message = billing.space_limit_message(lot.tenant_id, needed)
        if plan_message:
            return plan_message
        if needed > len(slots):
            return (f"Only {len(spaces) + len(slots)} spaces fit on a {canvas_w}x{canvas_h} map. "
                    "Lower the number or enlarge the map.")
        for name, (x, y) in slots[:needed]:
            db.session.add(Space(
                tenant_id=lot.tenant_id, lot_id=lot.id, name=name, kind="small", status="active",
                x=x, y=y, w=SPACE_W, h=SPACE_H, base_price=lot.base_daily_rate or 0,
                min_hours=1, max_days=30, default_available=True,
            ))
        audit("lot.spaces_added", "lot", lot.id, count=needed)
        return None
    used = set(db.session.scalars(select(Booking.space_id).where(Booking.lot_id == lot.id)))
    used |= set(db.session.scalars(select(Hold.space_id).where(Hold.lot_id == lot.id)))
    removable = [s for s in reversed(spaces) if s.id not in used]
    to_remove = len(spaces) - target
    if to_remove > len(removable):
        return (f"Only {len(removable)} space(s) can be removed. The others have bookings. "
                f"Lowest possible number: {len(spaces) - len(removable)}. Block those spaces instead.")
    for space in removable[:to_remove]:
        db.session.delete(space)
    audit("lot.spaces_removed", "lot", lot.id, count=to_remove)
    return None


@bp.route("/lots/<int:lot_id>/edit", methods=["GET", "POST"])
@owner_required
def lot_edit(lot_id):
    lot = _lot_or_404(lot_id)
    form = LotForm(obj=lot)
    del form.covered_count, form.ev_count
    current_count = len(lot.spaces)
    if request.method == "GET":
        form.space_count.data = current_count
    if form.validate_on_submit():
        target = form.space_count.data or current_count
        error = _resize_lot_spaces(lot, target, form.canvas_w.data, form.canvas_h.data)
        if error:
            form.space_count.errors.append(error)
            return render_template("admin/lot_form.html", form=form, lot=lot)
        del form.space_count
        form.populate_obj(lot)
        lot.currency = (lot.currency or "EUR").upper()[:3]
        lot.base_daily_rate = float(lot.base_daily_rate or 0)
        audit("lot.update", "lot", lot.id)
        db.session.commit()
        flash(ui("Lot saved."), "ok")
        return redirect(url_for("admin.lots"))
    return render_template("admin/lot_form.html", form=form, lot=lot)


@bp.post("/lots/<int:lot_id>/delete")
@owner_required
def lot_delete(lot_id):
    lot = _lot_or_404(lot_id)
    booking_count = db.session.scalar(select(func.count()).select_from(Booking).where(Booking.lot_id == lot.id)) or 0
    if booking_count:
        flash(
            ui('%(name)s has %(booking_count)s booking(s) and cannot be deleted. Set it to inactive instead so its history is kept.', name=lot.name, booking_count=booking_count), "error")
        return redirect(url_for("admin.lots"))
    name = lot.name
    audit("lot.delete", "lot", lot.id, name=name)
    db.session.execute(delete(Hold).where(Hold.lot_id == lot.id))
    db.session.execute(delete(Extra).where(Extra.lot_id == lot.id))
    db.session.delete(lot)
    db.session.commit()
    flash(ui('Lot %(name)s deleted.', name=name), "ok")
    return redirect(url_for("admin.lots"))


# --- spaces ---------------------------------------------------------------------

@bp.get("/lots/<int:lot_id>/spaces")
def spaces(lot_id):
    lot = _lot_or_404(lot_id)
    q = Space.query.filter_by(lot_id=lot.id)
    status = request.args.get("status", "")
    if status in SPACE_STATUSES:
        q = q.filter_by(status=status)
    term = request.args.get("q", "").strip()
    if term:
        q = q.filter(Space.name.ilike(f"%{term}%"))
    rows = q.order_by(Space.id).all()
    return render_template("admin/spaces.html", lot=lot, spaces=rows, status=status, term=term,
                           kind_labels=KIND_LABELS, status_labels=STATUS_LABELS)


@bp.post("/lots/<int:lot_id>/spaces/bulk")
@owner_required
def spaces_bulk(lot_id):
    """Apply covered / EV charging / size / status to several spaces at once."""
    lot = _lot_or_404(lot_id)
    ids = {int(i) for i in request.form.getlist("space_ids") if i.isdigit()}
    rows = Space.query.filter(Space.lot_id == lot.id, Space.id.in_(ids)).all() if ids else []
    if not rows:
        flash(ui("Select at least one space."), "error")
        return redirect(url_for("admin.spaces", lot_id=lot.id))
    for field in ("covered", "ev_charging"):
        value = request.form.get(field, "")
        if value in ("1", "0"):
            for space in rows:
                setattr(space, field, value == "1")
    kind, status = request.form.get("kind", ""), request.form.get("status", "")
    for space in rows:
        if kind in SPACE_KINDS:
            space.kind = kind
        if status in SPACE_STATUSES:
            space.status = status
    audit("space.bulk_update", "lot", lot.id, count=len(rows))
    db.session.commit()
    flash(ui('Updated %(p0)s space(s).', p0=len(rows)), "ok")
    return redirect(url_for("admin.spaces", lot_id=lot.id))


@bp.route("/lots/<int:lot_id>/spaces/new", methods=["GET", "POST"])
@owner_required
def space_new(lot_id):
    lot = _lot_or_404(lot_id)
    form = SpaceForm()
    if request.method == "GET":
        for k, v in grid_position(Space.query.filter_by(lot_id=lot.id).count()).items():
            getattr(form, k).data = v
    plan_message = billing.space_limit_message(lot.tenant_id, 1) if request.method == "POST" else None
    if plan_message:
        flash(ui(plan_message), "error")
    elif _validate_space(form, lot):
        space = Space(lot_id=lot.id)
        form.populate_obj(space)
        space.name = space.name.strip()
        space.notes = space.notes or ""
        space.base_price = float(space.base_price or 0)
        space.min_hours = int(space.min_hours or 1)
        space.max_days = int(space.max_days or 30)
        space.default_available = bool(space.default_available)
        db.session.add(space)
        db.session.flush()
        audit("space.create", "space", space.id, name=space.name)
        db.session.commit()
        flash(ui('Space %(name)s added.', name=space.name), "ok")
        return redirect(url_for("admin.spaces", lot_id=lot.id))
    return render_template("admin/space_form.html", form=form, lot=lot, space=None)


@bp.route("/spaces/<int:space_id>/edit", methods=["GET", "POST"])
@owner_required
def space_edit(space_id):
    space = db.get_or_404(Space, space_id)
    form = SpaceForm(obj=space)
    if request.method == "POST" and _validate_space(form, space.lot, space.id):
        form.populate_obj(space)
        space.name = space.name.strip()
        space.notes = space.notes or ""
        space.base_price = float(space.base_price or 0)
        space.min_hours = int(space.min_hours or 1)
        space.max_days = int(space.max_days or 30)
        space.default_available = bool(space.default_available)
        audit("space.update", "space", space.id, name=space.name)
        db.session.commit()
        flash(ui('Space %(name)s saved.', name=space.name), "ok")
        return redirect(url_for("admin.spaces", lot_id=space.lot_id))
    return render_template("admin/space_form.html", form=form, lot=space.lot, space=space)


@bp.post("/spaces/<int:space_id>/delete")
@owner_required
def space_delete(space_id):
    space = db.get_or_404(Space, space_id)
    lot_id, name = space.lot_id, space.name
    db.session.delete(space)
    audit("space.delete", "space", space_id, name=name)
    db.session.commit()
    flash(ui('Space %(name)s deleted.', name=name), "ok")
    return redirect(url_for("admin.spaces", lot_id=lot_id))


# --- layout ---------------------------------------------------------------------

@bp.get("/lots/<int:lot_id>/layout")
def layout(lot_id):
    lot = _lot_or_404(lot_id)
    return render_template("admin/layout.html", lot=lot, spaces=Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all(),
                           statuses=SPACE_STATUSES, status_labels=STATUS_LABELS, kind_labels=KIND_LABELS)


@bp.post("/lots/<int:lot_id>/autolayout")
@owner_required
def autolayout(lot_id):
    """Re-arrange all spaces of the lot into the default grid (order = creation order)."""
    lot = _lot_or_404(lot_id)
    rows = Space.query.filter_by(lot_id=lot.id).order_by(Space.id).all()
    for i, space in enumerate(rows):
        for k, v in grid_position(i).items():
            setattr(space, k, v)
    audit("lot.autolayout", "lot", lot.id, spaces=len(rows))
    db.session.commit()
    flash(ui("Spaces re-arranged in a grid."), "ok")
    return redirect(url_for("admin.layout", lot_id=lot.id))


@bp.route("/inspection-settings", methods=["GET", "POST"])
@owner_required
def inspection_settings():
    tenant = current_user.tenant
    form = InspectionSettingsForm()
    if request.method == "GET":
        form.retention_days.data = tenant.inspection_retention_days
        form.required_photos.data = tenant.inspection_required_photos
    elif form.validate_on_submit():
        tenant.inspection_retention_days = form.retention_days.data
        tenant.inspection_required_photos = form.required_photos.data
        audit(
            "inspection.settings.update",
            "tenant",
            tenant.id,
            retention_days=tenant.inspection_retention_days,
            required_photos=tenant.inspection_required_photos,
        )
        db.session.commit()
        flash(ui("Vehicle inspection requirements saved."), "ok")
        return redirect(url_for("admin.inspection_settings"))
    return render_template("admin/inspection_settings.html", form=form)
