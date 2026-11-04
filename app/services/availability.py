"""Tenant-safe public availability and pricing quotes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from ..extensions import db
from ..models import Booking, Hold, Lot, PriceRule, Space, Tenant, money


class AvailabilityError(Exception):
    """Base error for availability requests."""


class InvalidInterval(AvailabilityError):
    """Raised when the requested parking interval is invalid."""


class PricingUnavailable(AvailabilityError):
    """Raised when neither the space nor its lot has a configured rate."""


def parse_aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise InvalidInterval(f"{field} must be an ISO-8601 datetime.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidInterval(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def validate_interval(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
        raise InvalidInterval("Both dates must include a timezone.")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if start <= now:
        raise InvalidInterval("Pick-up must be in the future.")
    if start >= end:
        raise InvalidInterval("Return must be later than pick-up.")
    return start, end


def _rule_matches(rule: PriceRule, start: datetime, end: datetime, tenant_timezone: str) -> bool:
    try:
        zone = ZoneInfo(tenant_timezone)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    local_start = start.astimezone(zone)
    local_end = (end - timedelta(microseconds=1)).astimezone(zone)
    first_day, last_day = local_start.date(), local_end.date()

    if rule.start_day is not None and last_day < rule.start_day:
        return False
    if rule.end_day is not None and first_day > rule.end_day:
        return False
    if rule.period == "day":
        return True
    if rule.period == "weekend":
        day = first_day
        while day <= last_day:
            if day.weekday() >= 5 and (rule.start_day is None or day >= rule.start_day) and (
                rule.end_day is None or day <= rule.end_day
            ):
                return True
            day += timedelta(days=1)
        return False
    if rule.start_day is None or rule.end_day is None:
        return False
    return first_day <= rule.end_day and last_day >= rule.start_day


def quote_space(
    space: Space,
    lot: Lot,
    start: datetime,
    end: datetime,
    *,
    rules: list[PriceRule] | None = None,
    tenant_timezone: str | None = None,
) -> tuple[Decimal, str]:
    """Apply the highest-priority matching rule, otherwise the configured base daily rate."""
    start, end = validate_interval(start, end)
    duration = end - start
    duration_hours = duration.total_seconds() / 3600
    days = int((Decimal(str(duration.total_seconds())) / Decimal(86400)).to_integral_value(rounding=ROUND_CEILING))
    if duration_hours < space.min_hours:
        raise InvalidInterval(f"This space requires a minimum stay of {space.min_hours} hours.")
    if days > space.max_days:
        raise InvalidInterval(f"This space allows stays of up to {space.max_days} days.")

    if rules is None:
        rules = db.session.execute(
            select(PriceRule).where(
                PriceRule.tenant_id == lot.tenant_id,
                PriceRule.lot_id == lot.id,
                PriceRule.active.is_(True),
                (PriceRule.space_id.is_(None) | (PriceRule.space_id == space.id)),
            )
        ).scalars().all()
    if rules and tenant_timezone is None:
        tenant_timezone = db.session.execute(
            select(Tenant.timezone).where(Tenant.id == lot.tenant_id)
        ).scalar_one_or_none() or "UTC"
    tenant_timezone = tenant_timezone or "UTC"
    matching_rules = [
        rule for rule in rules
        if rule.active and _rule_matches(rule, start, end, tenant_timezone)
    ]
    matching_rules.sort(
        key=lambda rule: (
            -rule.priority,
            0 if rule.space_id == space.id else 1,
            rule.id,
        )
    )
    if matching_rules:
        return matching_rules[0].calculate_for(start, end), lot.currency

    space_rate = money(space.base_price or 0)
    rate = space_rate if space_rate > 0 else money(lot.base_daily_rate or 0)
    if rate <= 0:
        raise PricingUnavailable("Pricing has not been configured for this space or its parking lot.")
    return money(rate * days), lot.currency


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lot_availability(
    *,
    tenant_id: int,
    lot_id: int,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Return public, tenant-filtered space states for a half-open booking interval."""
    start, end = validate_interval(start, end)
    lot = db.session.execute(
        select(Lot).where(Lot.id == lot_id, Lot.tenant_id == tenant_id, Lot.active.is_(True))
    ).scalar_one_or_none()
    if lot is None:
        raise AvailabilityError("Parking lot not found.")

    spaces = db.session.execute(
        select(Space)
        .where(Space.tenant_id == tenant_id, Space.lot_id == lot_id)
        .order_by(Space.id)
    ).scalars().all()
    if not spaces:
        return []

    pricing_rules = db.session.execute(
        select(PriceRule).where(
            PriceRule.tenant_id == tenant_id,
            PriceRule.lot_id == lot_id,
            PriceRule.active.is_(True),
        )
    ).scalars().all()
    tenant_timezone = db.session.execute(
        select(Tenant.timezone).where(Tenant.id == tenant_id)
    ).scalar_one_or_none() or "UTC"
    space_ids = [space.id for space in spaces]
    now = datetime.now(timezone.utc)
    bookings = db.session.execute(
        select(Booking).where(
            Booking.tenant_id == tenant_id,
            Booking.lot_id == lot_id,
            Booking.space_id.in_(space_ids),
            Booking.status.in_(("reserved", "checked_in")),
            Booking.pickup_at < end,
            Booking.return_at > start,
        )
    ).scalars().all()
    holds = db.session.execute(
        select(Hold).where(
            Hold.tenant_id == tenant_id,
            Hold.lot_id == lot_id,
            Hold.space_id.in_(space_ids),
            Hold.status == "active",
            Hold.expires_at > now,
            Hold.pickup_at < end,
            Hold.return_at > start,
        )
    ).scalars().all()

    occupied_until: dict[int, list[datetime]] = {}
    for reservation in [*bookings, *holds]:
        occupied_until.setdefault(reservation.space_id, []).append(_as_utc(reservation.return_at))

    states = []
    for space in spaces:
        releases = occupied_until.get(space.id, [])
        latest_release = max(releases) if releases else None
        if space.status == "blocked" or not space.default_available:
            state = "blocked"
        elif latest_release is not None:
            state = "soon" if latest_release < end else "reserved"
        else:
            state = "optional" if space.status == "optional" else "available"

        try:
            space_rules = [
                rule for rule in pricing_rules
                if rule.space_id is None or rule.space_id == space.id
            ]
            price, currency = quote_space(
                space,
                lot,
                start,
                end,
                rules=space_rules,
                tenant_timezone=tenant_timezone,
            )
            price_value = f"{price:.2f}"
            price_error = None
        except (PricingUnavailable, InvalidInterval) as exc:
            price_value = None
            currency = lot.currency
            price_error = str(exc)

        states.append({
            "id": space.id,
            "name": space.name,
            "x": space.x,
            "y": space.y,
            "width": space.w,
            "height": space.h,
            "kind": space.kind,
            "covered": space.covered,
            "ev_charging": space.ev_charging,
            "bus_distance_m": space.bus_distance_m,
            "photo_url": space.photo_url,
            "status": state,
            "available_from": latest_release.isoformat() if state == "soon" else None,
            "price": price_value,
            "price_error": price_error,
            "currency": currency,
            "selectable": state in {"available", "optional"} and price_value is not None,
        })
    return states
