"""Profile photo on users

Revision ID: c9e5a2b7d106
Revises: b8d4f1a9c305
"""
import sqlalchemy as sa
from alembic import op

revision = "c9e5a2b7d106"
down_revision = "b8d4f1a9c305"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "avatar_data" not in existing:
            batch.add_column(sa.Column("avatar_data", sa.LargeBinary(), nullable=True))
        if "avatar_mime" not in existing:
            batch.add_column(sa.Column("avatar_mime", sa.String(length=40), nullable=True))
        if "avatar_updated_at" not in existing:
            batch.add_column(sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("avatar_updated_at")
        batch.drop_column("avatar_mime")
        batch.drop_column("avatar_data")
