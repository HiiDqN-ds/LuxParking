"""Invoices (Rechnung/Gutschrift), notification log, platform subscriptions, tenant invoice/billing settings

Existing tenants are marked billing_exempt so an operator's own parking is never limited by plan quotas.

Revision ID: b7e1d3a9c452
Revises: f5c2a8d4b913
"""
import sqlalchemy as sa
from alembic import op

revision = "b7e1d3a9c452"
down_revision = "f5c2a8d4b913"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table):
    return {c["name"] for c in _inspector().get_columns(table)}


def _add(batch, existing, column):
    """Add a column only if the table doesn't have it yet (databases that were partly created before)."""
    if column.name not in existing:
        batch.add_column(column)


def _create_table(name, *args, **kwargs):
    if name not in _inspector().get_table_names():
        op.create_table(name, *args, **kwargs)


def _create_index(name, table, columns, **kwargs):
    if name not in {i["name"] for i in _inspector().get_indexes(table)}:
        op.create_index(name, table, columns, **kwargs)



def upgrade():
    have = _columns("tenants")
    with op.batch_alter_table("tenants") as batch:
        _add(batch, have, sa.Column("billing_exempt", sa.Boolean(), nullable=False, server_default=sa.false()))
        _add(batch, have, sa.Column("vat_rate", sa.Numeric(5, 2), nullable=True))
        _add(batch, have, sa.Column("vat_id", sa.String(40), nullable=True))
        _add(batch, have, sa.Column("invoice_seller", sa.Text(), nullable=True))
        _add(batch, have, sa.Column("invoice_seq", sa.Integer(), nullable=False, server_default="0"))
        _add(batch, have, sa.Column("credit_seq", sa.Integer(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE tenants SET billing_exempt = :yes").bindparams(yes=True))

    _create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="SET NULL")),
        sa.Column("related_invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(12), nullable=False, server_default="invoice"),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("booking_ref", sa.String(32), nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column("vat_rate", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("net_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("vat_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("gross_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("customer_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("customer_company", sa.String(120), nullable=False, server_default=""),
        sa.Column("customer_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("locale", sa.String(10), nullable=False, server_default="de"),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("seller", sa.JSON(), nullable=False),
        sa.Column("service_from", sa.DateTime(timezone=True)),
        sa.Column("service_to", sa.DateTime(timezone=True)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('invoice', 'credit_note')", name="ck_invoice_kind"),
        sa.CheckConstraint("status IN ('open', 'paid', 'cancelled', 'credited')", name="ck_invoice_status"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_invoice_tenant_number"),
    )
    _create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])
    _create_index("ix_invoices_booking_id", "invoices", ["booking_id"])

    _create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("event", sa.String(30), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'sms', 'whatsapp')", name="ck_notification_channel"),
        sa.CheckConstraint("status IN ('sent', 'failed', 'skipped')", name="ck_notification_status"),
    )
    _create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    _create_index("ix_notifications_booking_id", "notifications", ["booking_id"])
    _create_index("ix_notifications_created_at", "notifications", ["created_at"])
    _create_index("ix_notification_booking_event", "notifications", ["tenant_id", "booking_id", "event", "channel"])

    _create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_key", sa.String(20), nullable=False, server_default="starter"),
        sa.Column("status", sa.String(12), nullable=False, server_default="trialing"),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True)),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stripe_customer_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("stripe_subscription_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column("last_payment_failed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('trialing', 'active', 'past_due', 'cancelled', 'expired')", name="ck_subscription_status"),
        sa.UniqueConstraint("tenant_id", name="uq_subscription_tenant"),
    )
    _create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])


def downgrade():
    op.drop_table("subscriptions")
    op.drop_table("notifications")
    op.drop_table("invoices")
    with op.batch_alter_table("tenants") as batch:
        for column in ("credit_seq", "invoice_seq", "invoice_seller", "vat_id", "vat_rate", "billing_exempt"):
            batch.drop_column(column)
