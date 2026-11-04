"""Autosaved handover form of an open inspection

Revision ID: e2f6b8d4a153
Revises: d1e5a7c3b942
"""
import sqlalchemy as sa
from alembic import op

revision = "e2f6b8d4a153"
down_revision = "d1e5a7c3b942"
branch_labels = None
depends_on = None


def _columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    if "draft_details" not in _columns("inspections"):
        with op.batch_alter_table("inspections") as batch:
            batch.add_column(sa.Column("draft_details", sa.Text(), nullable=True))


def downgrade():
    if "draft_details" in _columns("inspections"):
        with op.batch_alter_table("inspections") as batch:
            batch.drop_column("draft_details")
