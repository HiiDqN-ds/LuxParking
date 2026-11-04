"""Per-key allowed lots, monthly price and webhook lot filter

Revision ID: e4b9f2a61c07
Revises: c3d8a7e5b210
"""
from alembic import op
import sqlalchemy as sa

revision = "e4b9f2a61c07"
down_revision = "c3d8a7e5b210"
branch_labels = None
depends_on = None


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    keys = _columns("api_keys")
    if "allowed_lot_ids" not in keys:
        op.add_column("api_keys", sa.Column("allowed_lot_ids", sa.JSON()))
    if "monthly_price" not in keys:
        op.add_column("api_keys", sa.Column("monthly_price", sa.Numeric(10, 2), nullable=False, server_default="0"))
    if "price_breakdown" not in keys:
        op.add_column("api_keys", sa.Column("price_breakdown", sa.JSON()))
    if "lot_ids" not in _columns("webhook_endpoints"):
        op.add_column("webhook_endpoints", sa.Column("lot_ids", sa.JSON()))


def downgrade():
    op.drop_column("webhook_endpoints", "lot_ids")
    op.drop_column("api_keys", "price_breakdown")
    op.drop_column("api_keys", "monthly_price")
    op.drop_column("api_keys", "allowed_lot_ids")
