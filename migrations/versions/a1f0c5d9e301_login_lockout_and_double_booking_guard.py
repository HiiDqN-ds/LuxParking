"""login lockout columns and database-level double-booking guard

Revision ID: a1f0c5d9e301
Revises: 0c88fdc8a71e
"""
from alembic import op
import sqlalchemy as sa

revision = "a1f0c5d9e301"
down_revision = "0c88fdc8a71e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    # PostgreSQL only: even if application code or a race were wrong, the database refuses two
    # active bookings whose [pickup_at, return_at) overlap on the same space. Half-open ranges mean
    # back-to-back bookings are fine; cancelled and checked-out bookings no longer count.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_space_overlap "
            "EXCLUDE USING gist (space_id WITH =, tstzrange(pickup_at, return_at) WITH &&) "
            "WHERE (status IN ('reserved', 'checked_in'))"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ex_bookings_no_space_overlap")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_logins")
