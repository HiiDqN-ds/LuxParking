"""Tenant-scoped discount validation and redemption."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update

from ..extensions import db
from ..models import DiscountCode, Tenant, money


class DiscountUnavailable(ValueError):
    """Raised when a discount is invalid or no longer redeemable."""


@dataclass(frozen=True)
class DiscountQuote:
    code: str
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    checked_on: date


def _tenant_today(tenant_id: int) -> date:
    timezone_name = db.session.execute(
        select(Tenant.timezone).where(Tenant.id == tenant_id)
    ).scalar_one_or_none() or "UTC"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.now(zone).date()


def quote_discount(
    *,
    tenant_id: int,
    code: str,
    subtotal: Decimal,
    today: date | None = None,
    lock: bool = False,
) -> DiscountQuote:
    """Validate one code and return its rounded, non-stacking discount."""
    normalized_code = (code or "").strip().upper()
    if not normalized_code or len(normalized_code) > 40:
        raise DiscountUnavailable("Enter a valid discount code.")

    statement = select(DiscountCode).where(
            DiscountCode.tenant_id == tenant_id,
            DiscountCode.code == normalized_code,
        )
    if lock:
        statement = statement.with_for_update()
    discount_code = db.session.execute(statement).scalar_one_or_none()
    current_day = today or _tenant_today(tenant_id)
    if (
        discount_code is None
        or not discount_code.active
        or (discount_code.starts_on is not None and current_day < discount_code.starts_on)
        or (discount_code.ends_on is not None and current_day > discount_code.ends_on)
        or (
            discount_code.max_redemptions is not None
            and discount_code.redemption_count >= discount_code.max_redemptions
        )
    ):
        raise DiscountUnavailable("This discount code is not valid.")

    amount = money(subtotal)
    if amount < money(discount_code.minimum_amount):
        raise DiscountUnavailable("The booking does not meet this code's minimum amount.")
    value = Decimal(str(discount_code.value))
    if discount_code.discount_type == "percentage":
        if value > 100:
            raise DiscountUnavailable("This discount code is not configured correctly.")
        discount = money(amount * value / Decimal("100"))
    else:
        discount = money(value)
    discount = min(discount, amount)
    return DiscountQuote(
        code=normalized_code,
        subtotal=amount,
        discount=discount,
        total=money(amount - discount),
        checked_on=current_day,
    )


def redeem_discount(
    *,
    tenant_id: int,
    code: str,
    subtotal: Decimal,
    expected_total: Decimal,
) -> DiscountQuote:
    """Revalidate and atomically consume a code inside the booking transaction."""
    quote = quote_discount(tenant_id=tenant_id, code=code, subtotal=subtotal, lock=True)
    if quote.total != money(expected_total):
        raise DiscountUnavailable("The discount changed; request a fresh quote.")

    today = quote.checked_on
    conditions = [
        DiscountCode.tenant_id == tenant_id,
        DiscountCode.code == quote.code,
        DiscountCode.active.is_(True),
        or_(DiscountCode.starts_on.is_(None), DiscountCode.starts_on <= today),
        or_(DiscountCode.ends_on.is_(None), DiscountCode.ends_on >= today),
    ]
    # The guarded UPDATE makes the usage cap safe when bookings race.
    conditions.append(
        or_(
            DiscountCode.max_redemptions.is_(None),
            DiscountCode.redemption_count < DiscountCode.max_redemptions,
        )
    )
    result = db.session.execute(
        update(DiscountCode)
        .where(*conditions)
        .values(redemption_count=DiscountCode.redemption_count + 1)
    )
    if result.rowcount != 1:
        raise DiscountUnavailable("This discount code has reached its redemption limit.")
    return quote
