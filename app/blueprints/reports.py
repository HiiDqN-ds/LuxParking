"""Owner reports: revenue, occupancy, extras, discounts and VAT, with CSV export."""
import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, render_template, request
from flask_login import current_user
from sqlalchemy import select

from ..extensions import db, login_manager
from ..models import Booking, BookingExtra, Invoice, Lot, Space

bp = Blueprint("reports", __name__, url_prefix="/admin")
MAX_DAYS = 366


@bp.before_request
def owner_only():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if not current_user.is_owner:
        abort(403)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _range():
    today = datetime.now(timezone.utc).date()
    def parse(name, default):
        try:
            return date.fromisoformat(request.args.get(name, ""))
        except ValueError:
            return default
    end = parse("to", today)
    start = parse("from", end - timedelta(days=29))
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
    return start, end


def build_report(tenant, start: date, end: date, lot_id: int | None = None) -> dict:
    zone = ZoneInfo(tenant.timezone)
    lo = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    query = select(Booking).where(Booking.tenant_id == tenant.id, Booking.created_at >= lo, Booking.created_at < hi)
    if lot_id:
        query = query.where(Booking.lot_id == lot_id)
    bookings = db.session.execute(query.limit(20000)).scalars().all()
    days = {start + timedelta(days=i): {"bookings": 0, "booked": Decimal(0), "paid": Decimal(0)} for i in range((end - start).days + 1)}
    status_counts: dict[str, int] = defaultdict(int)
    discounts: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    for b in bookings:
        status_counts[b.status] += 1
        if b.status == "cancelled":
            continue
        row = days.get(_aware(b.created_at).astimezone(zone).date())
        if row is None:
            continue
        row["bookings"] += 1
        row["booked"] += Decimal(b.total_amount)
        if b.payment_status == "paid":
            row["paid"] += Decimal(b.total_amount)
        if b.discount_code:
            discounts[b.discount_code][0] += 1
            discounts[b.discount_code][1] += max(Decimal(0), Decimal(b.subtotal_amount) - Decimal(b.total_amount))
    totals = {"bookings": sum(d["bookings"] for d in days.values()), "booked": sum((d["booked"] for d in days.values()), Decimal(0)),
              "paid": sum((d["paid"] for d in days.values()), Decimal(0))}
    totals["average"] = (totals["booked"] / totals["bookings"]).quantize(Decimal("0.01")) if totals["bookings"] else Decimal(0)

    # occupancy: spaces in use per day / bookable spaces (bookings overlapping that local day)
    space_query = select(Space.id).where(Space.tenant_id == tenant.id, Space.status != "blocked")
    if lot_id:
        space_query = space_query.where(Space.lot_id == lot_id)
    space_count = len(db.session.execute(space_query).all())
    occ_query = select(Booking).where(Booking.tenant_id == tenant.id, Booking.status != "cancelled", Booking.pickup_at < hi, Booking.return_at > lo)
    if lot_id:
        occ_query = occ_query.where(Booking.lot_id == lot_id)
    occupied = {d: set() for d in days}
    for b in db.session.execute(occ_query.limit(20000)).scalars():
        first = max(start, _aware(b.pickup_at).astimezone(zone).date())
        last = min(end, (_aware(b.return_at).astimezone(zone) - timedelta(seconds=1)).date())
        d = first
        while d <= last:
            occupied[d].add(b.space_id)
            d += timedelta(days=1)
    occupancy = [{"day": d, "spaces": len(ids), "percent": round(100 * len(ids) / space_count) if space_count else 0} for d, ids in sorted(occupied.items())]

    extras_rows: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    extra_query = select(BookingExtra).join(Booking, Booking.id == BookingExtra.booking_id).where(
        BookingExtra.tenant_id == tenant.id, Booking.created_at >= lo, Booking.created_at < hi, Booking.status != "cancelled", BookingExtra.status != "cancelled")
    for e in db.session.execute(extra_query.limit(20000)).scalars():
        extras_rows[e.name][0] += e.quantity or 1
        extras_rows[e.name][1] += e.total()

    vat = {"net": Decimal(0), "vat": Decimal(0), "gross": Decimal(0), "credit": Decimal(0)}
    for inv in db.session.execute(select(Invoice).where(Invoice.tenant_id == tenant.id, Invoice.issued_at >= lo, Invoice.issued_at < hi,
                                                         Invoice.status != "cancelled").limit(20000)).scalars():
        if inv.kind == "credit_note":
            vat["credit"] += Decimal(inv.gross_amount)
        if inv.kind == "invoice" and inv.status in ("paid", "credited", "open"):
            vat["net"] += Decimal(inv.net_amount); vat["vat"] += Decimal(inv.vat_amount); vat["gross"] += Decimal(inv.gross_amount)
    return {"days": days, "totals": totals, "status": dict(status_counts), "discounts": dict(discounts), "occupancy": occupancy,
            "space_count": space_count, "extras": dict(extras_rows), "vat": vat,
            "avg_occupancy": round(sum(o["percent"] for o in occupancy) / len(occupancy)) if occupancy else 0}


@bp.get("/reports")
def overview():
    start, end = _range()
    lot_id = request.args.get("lot", type=int)
    tenant = current_user.tenant
    report = build_report(tenant, start, end, lot_id)
    peak = max([d["booked"] for d in report["days"].values()] + [Decimal(1)])
    return render_template("admin/reports.html", r=report, start=start, end=end, lot_id=lot_id, peak=peak,
                           lots=db.session.execute(select(Lot).order_by(Lot.name)).scalars().all(), currency=tenant.currency)


@bp.get("/reports.csv")
def export_csv():
    start, end = _range()
    kind = request.args.get("kind", "revenue")
    report = build_report(current_user.tenant, start, end, request.args.get("lot", type=int))
    out = io.StringIO()
    writer = csv.writer(out)
    if kind == "occupancy":
        writer.writerow(["date", "spaces_in_use", "percent"])
        for o in report["occupancy"]:
            writer.writerow([o["day"].isoformat(), o["spaces"], o["percent"]])
    else:
        writer.writerow(["date", "bookings", "booked_total", "paid_total"])
        for d, row in report["days"].items():
            writer.writerow([d.isoformat(), row["bookings"], row["booked"], row["paid"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="report-{kind}-{start}-{end}.csv"'})
