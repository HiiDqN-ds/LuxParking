"""Logo upload, shuttle bundle pricing (2 persons included) and the notification centre

Existing shuttle extras that were priced per person become bundles that include 2 persons (the operator's rule).
Their price is left untouched and the additional-person price stays empty, so no more than the included persons can be
booked until the owner sets a price for additional persons. Past bookings keep the pricing they were made with.

Revision ID: c9a4e6b21f85
Revises: b7e1d3a9c452
"""
import sqlalchemy as sa
from alembic import op

revision = "c9a4e6b21f85"
down_revision = "b7e1d3a9c452"
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
        _add(batch, have, sa.Column("logo_data", sa.LargeBinary(), nullable=True))
        _add(batch, have, sa.Column("logo_mime", sa.String(40), nullable=True))
    have = _columns("users")
    with op.batch_alter_table("users") as batch:
        _add(batch, have, sa.Column("alerts_seen_at", sa.DateTime(timezone=True), nullable=True))
    have = _columns("extras")
    with op.batch_alter_table("extras") as batch:
        _add(batch, have, sa.Column("included_persons", sa.Integer(), nullable=False, server_default="0"))
        _add(batch, have, sa.Column("extra_person_price", sa.Numeric(10, 2), nullable=True))
    have = _columns("booking_extras")
    with op.batch_alter_table("booking_extras") as batch:
        _add(batch, have, sa.Column("included_persons", sa.Integer(), nullable=False, server_default="0"))
        _add(batch, have, sa.Column("extra_person_price", sa.Numeric(10, 2), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE extras SET included_persons = 2 WHERE kind = 'shuttle' AND per_person = :yes").bindparams(yes=True))

    _create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="SET NULL")),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('booking_created', 'booking_paid', 'booking_cancelled', 'booking_refunded', 'checked_in', 'checked_out', 'dispute_opened')",
            name="ck_alert_kind",
        ),
    )
    _create_index("ix_alerts_tenant_id", "alerts", ["tenant_id"])
    _create_index("ix_alerts_booking_id", "alerts", ["booking_id"])
    _create_index("ix_alerts_created_at", "alerts", ["created_at"])


def downgrade():
    op.drop_table("alerts")
    with op.batch_alter_table("booking_extras") as batch:
        batch.drop_column("extra_person_price")
        batch.drop_column("included_persons")
    with op.batch_alter_table("extras") as batch:
        batch.drop_column("extra_person_price")
        batch.drop_column("included_persons")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("alerts_seen_at")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("logo_mime")
        batch.drop_column("logo_data")
