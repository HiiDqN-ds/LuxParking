"""Phase 11: reports, CSV export and reminders command."""
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import Tenant
from app.services.bookings import cancel_booking, create_payment_record, update_payment_status
from app.services.invoices import sync_invoices
from tests.booking_helpers import make_booking, seed_tenant
from tests.conftest import login


def test_reports_total_revenue_and_ignore_cancelled(client, app):
    paid = make_booking(space_index=0, total="119.00")
    payment = create_payment_record(tenant_id=paid.tenant_id, booking_id=paid.id, provider="manual", amount=paid.total_amount, currency="EUR")
    update_payment_status(tenant_id=paid.tenant_id, payment_id=payment.id, status="paid")
    open_booking = make_booking(space_index=1, email="b@example.com", total="50.00")
    gone = make_booking(space_index=2, email="c@example.com", total="999.00")
    cancel_booking(tenant_id=gone.tenant_id, booking_id=gone.id)
    sync_invoices(open_booking)
    db.session.commit()
    from app.blueprints.reports import build_report
    today = date.today()
    report = build_report(seed_tenant(), today - timedelta(days=1), today + timedelta(days=1))
    assert report["totals"]["bookings"] == 2
    assert report["totals"]["booked"] == Decimal("169.00") and report["totals"]["paid"] == Decimal("119.00")
    assert report["status"]["cancelled"] == 1
    assert report["vat"]["gross"] == Decimal("169.00") and report["vat"]["vat"] > 0


def test_occupancy_counts_spaces_on_the_days_of_the_stay(app):
    make_booking(space_index=0, hours=48, days=2)
    from app.blueprints.reports import build_report
    today = date.today()
    report = build_report(seed_tenant(), today, today + timedelta(days=6))
    assert sum(1 for o in report["occupancy"] if o["spaces"] == 1) >= 2
    assert all(0 <= o["percent"] <= 100 for o in report["occupancy"])


def test_reports_page_and_csv_are_owner_only_and_work(client, app):
    assert client.get("/admin/reports").status_code in (302, 401)
    login(client)
    page = client.get("/admin/reports")
    assert page.status_code == 200 and b"Revenue per day" in page.data
    revenue = client.get("/admin/reports.csv?kind=revenue")
    assert revenue.status_code == 200 and revenue.data.startswith(b"date,bookings,booked_total,paid_total")
    assert client.get("/admin/reports.csv?kind=occupancy").data.startswith(b"date,spaces_in_use,percent")
    assert client.get("/admin/reports?from=garbage&to=nonsense").status_code == 200  # bad input falls back to the last 30 days


def test_staff_cannot_open_reports_or_invoices(client, app, staff):
    login(client, "staff@test.dev", "staff-pass")
    for url in ("/admin/reports", "/admin/invoices", "/admin/billing"):
        assert client.get(url).status_code == 403


def test_reminder_cli_command_runs(app):
    result = app.test_cli_runner().invoke(args=["send-reminders"])
    assert result.exit_code == 0 and "Reminders sent: 0" in result.output
