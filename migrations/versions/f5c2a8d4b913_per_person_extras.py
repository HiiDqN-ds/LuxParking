"""Per-person extras (shuttle price per passenger)

Revision ID: f5c2a8d4b913
Revises: e4b9f2a61c07
"""
from alembic import op
import sqlalchemy as sa

revision = "f5c2a8d4b913"
down_revision = "e4b9f2a61c07"
branch_labels = None
depends_on = None


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    if "per_person" not in _columns("extras"):
        op.add_column("extras", sa.Column("per_person", sa.Boolean(), nullable=False, server_default=sa.false()))
        # Airport shuttles are sold per passenger.
        op.execute("UPDATE extras SET per_person = :t WHERE kind = 'shuttle'".replace(":t", "1" if op.get_bind().dialect.name == "sqlite" else "TRUE"))
    if "quantity" not in _columns("booking_extras"):
        op.add_column("booking_extras", sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    op.drop_column("booking_extras", "quantity")
    op.drop_column("extras", "per_person")
