"""phase 2 pricing and phase 3 booking lifecycle

Revision ID: 8a41c0e92d73
Revises: de2681534c9a
Create Date: 2026-10-13

"""
from alembic import op
import sqlalchemy as sa


revision = "8a41c0e92d73"
down_revision = "de2681534c9a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("lots") as batch_op:
        batch_op.add_column(sa.Column("base_daily_rate", sa.Numeric(10, 2), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"))
        batch_op.create_check_constraint("ck_lot_base_daily_rate", "base_daily_rate >= 0")

    with op.batch_alter_table("lots") as batch_op:
        batch_op.alter_column("base_daily_rate", server_default=None)
        batch_op.alter_column("currency", server_default=None)

    with op.batch_alter_table("spaces") as batch_op:
        batch_op.add_column(sa.Column("base_price", sa.Numeric(10, 2), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("min_hours", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("max_days", sa.Integer(), nullable=False, server_default="30"))
        batch_op.add_column(sa.Column("default_available", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.create_check_constraint("ck_space_base_price", "base_price >= 0")
        batch_op.create_check_constraint("ck_space_min_hours", "min_hours > 0")
        batch_op.create_check_constraint("ck_space_max_days", "max_days > 0")

    with op.batch_alter_table("spaces") as batch_op:
        batch_op.alter_column("base_price", server_default=None)
        batch_op.alter_column("min_hours", server_default=None)
        batch_op.alter_column("max_days", server_default=None)
        batch_op.alter_column("default_available", server_default=None)

    op.create_table(
        "price_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("start_day", sa.Date(), nullable=True),
        sa.Column("end_day", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.CheckConstraint("period IN ('day', 'weekend', 'season', 'holiday')", name="ck_price_rule_period"),
        sa.CheckConstraint("mode IN ('flat', 'per_day', 'per_hour')", name="ck_price_rule_mode"),
        sa.CheckConstraint("amount >= 0", name="ck_price_rule_amount"),
        sa.CheckConstraint("(start_day IS NULL OR end_day IS NULL OR start_day <= end_day)", name="ck_price_rule_date_range"),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_rules_lot_id", "price_rules", ["lot_id"])
    op.create_index("ix_price_rules_space_id", "price_rules", ["space_id"])
    op.create_index("ix_price_rules_tenant_id", "price_rules", ["tenant_id"])

    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"])

    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("plate", sa.String(length=30), nullable=False),
        sa.Column("make", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("color", sa.String(length=40), nullable=False),
        sa.Column("photo_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vehicles_customer_id", "vehicles", ["customer_id"])
    op.create_index("ix_vehicles_tenant_id", "vehicles", ["tenant_id"])

    op.create_table(
        "holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("customer_email", sa.String(length=255), nullable=False),
        sa.Column("pickup_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("return_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'released', 'converted', 'expired')", name="ck_hold_status"),
        sa.CheckConstraint("pickup_at < return_at", name="ck_hold_time_order"),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_holds_lot_id", "holds", ["lot_id"])
    op.create_index("ix_holds_space_id", "holds", ["space_id"])
    op.create_index("ix_holds_tenant_id", "holds", ["tenant_id"])
    op.create_index(
        "ix_hold_space_interval_status",
        "holds",
        ["tenant_id", "space_id", "pickup_at", "return_at", "status"],
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("vehicle_id", sa.Integer(), nullable=True),
        sa.Column("booking_ref", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("payment_status", sa.String(length=20), nullable=False),
        sa.Column("pickup_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("return_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("discount_code", sa.String(length=40), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("qr_code", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('reserved', 'checked_in', 'checked_out', 'cancelled')", name="ck_booking_status"),
        sa.CheckConstraint("payment_status IN ('pending', 'paid', 'partial', 'failed', 'refunded')", name="ck_booking_payment_status"),
        sa.CheckConstraint("pickup_at < return_at", name="ck_booking_time_order"),
        sa.CheckConstraint("total_amount >= 0", name="ck_booking_total_amount"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_ref"),
    )
    op.create_index("ix_bookings_customer_id", "bookings", ["customer_id"])
    op.create_index("ix_bookings_lot_id", "bookings", ["lot_id"])
    op.create_index("ix_bookings_space_id", "bookings", ["space_id"])
    op.create_index("ix_bookings_tenant_id", "bookings", ["tenant_id"])
    op.create_index("ix_bookings_vehicle_id", "bookings", ["vehicle_id"])
    op.create_index(
        "ix_booking_space_interval_status",
        "bookings",
        ["tenant_id", "space_id", "pickup_at", "return_at", "status"],
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_ref", sa.String(length=120), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('stripe', 'manual', 'paypal', 'apple_pay')", name="ck_payment_provider"),
        sa.CheckConstraint("status IN ('pending', 'paid', 'failed', 'refunded')", name="ck_payment_status"),
        sa.CheckConstraint("amount >= 0", name="ck_payment_amount"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payments_booking_id", "payments", ["booking_id"])
    op.create_index("ix_payments_tenant_id", "payments", ["tenant_id"])


def downgrade():
    op.drop_index("ix_payments_tenant_id", table_name="payments")
    op.drop_index("ix_payments_booking_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_booking_space_interval_status", table_name="bookings")
    op.drop_index("ix_bookings_vehicle_id", table_name="bookings")
    op.drop_index("ix_bookings_tenant_id", table_name="bookings")
    op.drop_index("ix_bookings_space_id", table_name="bookings")
    op.drop_index("ix_bookings_lot_id", table_name="bookings")
    op.drop_index("ix_bookings_customer_id", table_name="bookings")
    op.drop_table("bookings")

    op.drop_index("ix_hold_space_interval_status", table_name="holds")
    op.drop_index("ix_holds_tenant_id", table_name="holds")
    op.drop_index("ix_holds_space_id", table_name="holds")
    op.drop_index("ix_holds_lot_id", table_name="holds")
    op.drop_table("holds")

    op.drop_index("ix_vehicles_tenant_id", table_name="vehicles")
    op.drop_index("ix_vehicles_customer_id", table_name="vehicles")
    op.drop_table("vehicles")
    op.drop_index("ix_customers_tenant_id", table_name="customers")
    op.drop_table("customers")

    op.drop_index("ix_price_rules_tenant_id", table_name="price_rules")
    op.drop_index("ix_price_rules_space_id", table_name="price_rules")
    op.drop_index("ix_price_rules_lot_id", table_name="price_rules")
    op.drop_table("price_rules")

    with op.batch_alter_table("spaces") as batch_op:
        batch_op.drop_constraint("ck_space_max_days", type_="check")
        batch_op.drop_constraint("ck_space_min_hours", type_="check")
        batch_op.drop_constraint("ck_space_base_price", type_="check")
        batch_op.drop_column("default_available")
        batch_op.drop_column("max_days")
        batch_op.drop_column("min_hours")
        batch_op.drop_column("base_price")

    with op.batch_alter_table("lots") as batch_op:
        batch_op.drop_constraint("ck_lot_base_daily_rate", type_="check")
        batch_op.drop_column("currency")
        batch_op.drop_column("base_daily_rate")
