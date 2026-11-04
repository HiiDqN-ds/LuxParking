"""Owners can delete unpaid/cancelled bookings together with their inspections, unless a dispute is open."""
from datetime import timedelta

from app.extensions import db
from app.models import Booking, DamageMark, Dispute, Inspection, InspectionPhoto, Space, Tenant, utcnow
from app.tenancy import use_tenant
from tests.conftest import login
from tests.test_phase8 import _make_booking


def _booking_with_evidence(app, ref, dispute_status=None):
    tenant = Tenant.query.first()
    space = Space.query.filter_by(tenant_id=tenant.id).first()
    with use_tenant(tenant.id):
        booking = _make_booking(tenant.id, space.lot_id, space.id, "g@example.com", ref)
        booking.status, booking.payment_status = "cancelled", "pending"
        arrival = Inspection(tenant_id=tenant.id, booking_id=booking.id, phase="arrival", status="completed",
                             retention_until=utcnow() + timedelta(days=90))
        departure = Inspection(tenant_id=tenant.id, booking_id=booking.id, phase="departure", status="completed",
                               retention_until=utcnow() + timedelta(days=90))
        db.session.add_all([arrival, departure])
        db.session.flush()
        db.session.add(InspectionPhoto(
            tenant_id=tenant.id, inspection_id=arrival.id, category="front", object_key=f"k/{ref}", sha256="0" * 64,
            content_type="image/jpeg", size_bytes=10, booking_ref=ref, staff_name="S", captured_at=utcnow()))
        if dispute_status:
            mark = DamageMark(tenant_id=tenant.id, inspection_id=departure.id, x_percent=1, y_percent=1, kind="scratch")
            db.session.add(mark)
            db.session.flush()
            db.session.add(Dispute(tenant_id=tenant.id, booking_id=booking.id, arrival_inspection_id=arrival.id,
                                   departure_inspection_id=departure.id, damage_mark_id=mark.id, status=dispute_status))
        db.session.commit()
        return booking.id


def test_booking_with_inspections_can_be_deleted(client, app, monkeypatch):
    removed = []
    monkeypatch.setattr("app.blueprints.manage.delete_object", lambda *, object_key: removed.append(object_key))
    booking_id = _booking_with_evidence(app, "DEL-1")
    login(client)
    assert client.post(f"/admin/bookings/{booking_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(Booking, booking_id) is None
        assert Inspection.query.count() == 0 and InspectionPhoto.query.count() == 0
    assert removed == ["k/DEL-1"]


def test_resolved_dispute_does_not_block_deletion(client, app, monkeypatch):
    monkeypatch.setattr("app.blueprints.manage.delete_object", lambda *, object_key: None)
    booking_id = _booking_with_evidence(app, "DEL-2", dispute_status="resolved")
    login(client)
    client.post(f"/admin/bookings/{booking_id}/delete")
    with app.app_context():
        assert db.session.get(Booking, booking_id) is None and Dispute.query.count() == 0


def test_open_dispute_blocks_deletion(client, app, monkeypatch):
    removed = []
    monkeypatch.setattr("app.blueprints.manage.delete_object", lambda *, object_key: removed.append(object_key))
    booking_id = _booking_with_evidence(app, "DEL-3", dispute_status="open")
    login(client)
    client.post(f"/admin/bookings/{booking_id}/delete")
    with app.app_context():
        assert db.session.get(Booking, booking_id) is not None
        assert Inspection.query.count() == 2 and Dispute.query.count() == 1
    assert removed == []


def test_paid_booking_still_cannot_be_deleted(client, app, monkeypatch):
    booking_id = _booking_with_evidence(app, "DEL-4")
    with app.app_context():
        db.session.get(Booking, booking_id).payment_status = "paid"
        db.session.commit()
    login(client)
    client.post(f"/admin/bookings/{booking_id}/delete")
    with app.app_context():
        assert db.session.get(Booking, booking_id) is not None


def test_new_lot_creates_requested_number_of_spaces(client):
    from tests.conftest import login
    from app.models import Lot, Space

    login(client)
    data = {"name": "Big Lot", "canvas_w": 1000, "canvas_h": 700, "space_count": 30, "base_daily_rate": 9, "currency": "EUR", "active": "y"}
    response = client.post("/admin/lots/new", data=data)
    assert response.status_code == 302
    lot = Lot.query.filter_by(name="Big Lot").one()
    names = [s.name for s in lot.spaces]
    assert len(names) == 30 and len(set(names)) == 30 and names[0] == "A1"
    assert all(0 <= s.x and s.x + s.w <= 1000 and s.y + s.h <= 700 for s in lot.spaces)


def test_new_lot_rejects_more_spaces_than_fit(client):
    from tests.conftest import login
    from app.models import Lot

    login(client)
    data = {"name": "Tiny", "canvas_w": 300, "canvas_h": 300, "space_count": 50, "currency": "EUR", "active": "y"}
    response = client.post("/admin/lots/new", data=data)
    assert response.status_code == 200 and b"fit on a" in response.data
    assert Lot.query.filter_by(name="Tiny").count() == 0


def test_new_lot_covered_ev_counts_and_bulk_update(client):
    from tests.conftest import login
    from app.models import Lot

    login(client)
    data = {"name": "Mix", "canvas_w": 1000, "canvas_h": 700, "space_count": 10, "covered_count": 4, "ev_count": 2, "currency": "EUR", "active": "y"}
    assert client.post("/admin/lots/new", data=data).status_code == 302
    lot = Lot.query.filter_by(name="Mix").one()
    assert sum(s.covered for s in lot.spaces) == 4 and sum(s.ev_charging for s in lot.spaces) == 2
    ids = [s.id for s in lot.spaces[:3]]
    r = client.post(f"/admin/lots/{lot.id}/spaces/bulk", data={"space_ids": ids, "covered": "0", "ev_charging": "1"})
    assert r.status_code == 302
    for s in Lot.query.get(lot.id).spaces[:3]:
        assert not s.covered and s.ev_charging
    page = client.get(f"/admin/lots/{lot.id}/spaces").get_data(as_text=True)
    assert "Set for selected spaces" in page
    assert "Spaces</a>" in client.get("/admin/lots").get_data(as_text=True)


def _mk_lot(client, name="Edit Lot", count=5):
    from app.models import Lot
    data = {"name": name, "canvas_w": 1000, "canvas_h": 700, "space_count": count, "currency": "EUR", "active": "y"}
    assert client.post("/admin/lots/new", data=data).status_code == 302
    return Lot.query.filter_by(name=name).one()


def test_edit_lot_can_add_and_remove_spaces(client):
    from tests.conftest import login
    from app.models import Lot, Space

    login(client)
    lot = _mk_lot(client)
    base = {"name": "Edit Lot", "canvas_w": 1000, "canvas_h": 700, "currency": "EUR", "active": "y"}
    assert f'value="5"' in client.get(f"/admin/lots/{lot.id}/edit").get_data(as_text=True)
    assert client.post(f"/admin/lots/{lot.id}/edit", data={**base, "space_count": 12}).status_code == 302
    names = [s.name for s in Space.query.filter_by(lot_id=lot.id)]
    assert len(names) == 12 and len(set(names)) == 12
    assert client.post(f"/admin/lots/{lot.id}/edit", data={**base, "space_count": 3}).status_code == 302
    assert Space.query.filter_by(lot_id=lot.id).count() == 3
    r = client.post(f"/admin/lots/{lot.id}/edit", data={**base, "space_count": 999})
    assert r.status_code == 200 and Space.query.filter_by(lot_id=lot.id).count() == 3


def test_shuttle_is_priced_per_person(client, app):
    from decimal import Decimal
    from app.models import Extra
    from app.services.booking_admin import add_extra, extras_total, remove_extra

    tenant = Tenant.query.first()
    space = Space.query.filter_by(tenant_id=tenant.id).first()
    with use_tenant(tenant.id):
        booking = _make_booking(tenant.id, space.lot_id, space.id, "p@example.com", "PAX-1")
        shuttle = Extra(tenant_id=tenant.id, lot_id=space.lot_id, kind="shuttle", name="Shuttle", price=10, per_person=True)
        cleaning = Extra(tenant_id=tenant.id, lot_id=space.lot_id, kind="cleaning", name="Wash", price=19, per_person=False)
        db.session.add_all([shuttle, cleaning]); db.session.flush()
        before = Decimal(booking.total_amount)
        row = add_extra(booking, shuttle, "", 3)
        assert row.quantity == 3 and Decimal(booking.total_amount) == before + 30
        assert add_extra(booking, cleaning, "", 5).quantity == 1  # flat extras ignore passengers
        assert Decimal(booking.total_amount) == before + 30 + 19
        assert extras_total(booking) == Decimal(49)
        assert add_extra(booking, shuttle, "", 999).quantity == 20  # capped
        remove_extra(booking, row)
        assert Decimal(booking.total_amount) == before + 19 + 200
