"""Forgot password: phone number on users and the password_resets table

Revision ID: b8d4f1a9c305
Revises: e2f6b8d4a153
"""
import sqlalchemy as sa
from alembic import op

revision = "b8d4f1a9c305"
down_revision = "e2f6b8d4a153"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "phone" not in {c["name"] for c in inspector.get_columns("users")}:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("phone", sa.String(length=32), nullable=True))
    if "password_resets" not in inspector.get_table_names():
        op.create_table(
            "password_resets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel", sa.String(length=10), nullable=False),
            sa.Column("secret_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("channel IN ('email', 'sms')", name="ck_password_reset_channel"),
        )
        op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])


def downgrade():
    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_table("password_resets")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("phone")
