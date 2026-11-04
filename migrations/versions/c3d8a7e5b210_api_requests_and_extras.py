"""API access requests, shuttle and cleaning extras

Revision ID: c3d8a7e5b210
Revises: a1f0c5d9e301
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d8a7e5b210"
down_revision = "a1f0c5d9e301"
branch_labels = None
depends_on = None


def _tenant():
    return sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    ts = lambda n: sa.Column(n, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    if "api_access_requests" not in existing:
      op.create_table(
        "api_access_requests", sa.Column("id", sa.Integer(), primary_key=True), _tenant(),
        sa.Column("bundle", sa.String(30), nullable=False), sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("requested_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("api_key_id", sa.Integer(), sa.ForeignKey("api_keys.id", ondelete="SET NULL")), ts("created_at"),
    )
    if "extras" not in existing:
      op.create_table(
        "extras", sa.Column("id", sa.Integer(), primary_key=True), _tenant(),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("lots.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(12), nullable=False), sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()), ts("created_at"),
        sa.CheckConstraint("kind IN ('shuttle', 'cleaning')", name="ck_extra_kind"),
        sa.CheckConstraint("price >= 0", name="ck_extra_price"),
    )
    if "booking_extras" not in existing:
      op.create_table(
        "booking_extras", sa.Column("id", sa.Integer(), primary_key=True), _tenant(),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("extra_id", sa.Integer(), sa.ForeignKey("extras.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(12), nullable=False), sa.Column("name", sa.String(100), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("details", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False, server_default="requested"),
        sa.Column("done_at", sa.DateTime(timezone=True)),
        sa.Column("done_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")), ts("created_at"),
    )


def downgrade():
    op.drop_table("booking_extras")
    op.drop_table("extras")
    op.drop_table("api_access_requests")
