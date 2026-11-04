"""Add customer billing details and Stripe payment references.

Revision ID: 0c88fdc8a71e
Revises: 7d3fa19c80b2
"""
from alembic import op
import sqlalchemy as sa


revision = "0c88fdc8a71e"
down_revision = "7d3fa19c80b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(
            sa.Column("billing_email", sa.String(length=255), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("locale", sa.String(length=10), nullable=False, server_default="en")
        )
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.add_column(sa.Column("confirmation_sent_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("subtotal_amount", sa.Numeric(precision=10, scale=2), nullable=False, server_default="0")
        )
    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(
            sa.Column("provider_intent_ref", sa.String(length=120), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("checkout_url", sa.String(length=500), nullable=False, server_default="")
        )


def downgrade():
    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_column("checkout_url")
        batch_op.drop_column("provider_intent_ref")
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.drop_column("subtotal_amount")
        batch_op.drop_column("confirmation_sent_at")
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("locale")
        batch_op.drop_column("billing_email")
