"""Store short vehicle-inspection videos alongside comparison frames.

Revision ID: a6d2f9c4e871
Revises: e3c9b2f0417a
"""
import sqlalchemy as sa
from alembic import op


revision = "a6d2f9c4e871"
down_revision = "e3c9b2f0417a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inspection_videos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inspection_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=40), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("booking_ref", sa.String(length=32), nullable=False),
        sa.Column("staff_user_id", sa.Integer(), nullable=True),
        sa.Column("staff_name", sa.String(length=120), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["inspection_id"], ["inspections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["staff_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("tenant_id", "inspection_id", "category", name="uq_inspection_video_tenant_category"),
        sa.CheckConstraint("size_bytes > 0", name="ck_inspection_video_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_inspection_video_sha256"),
    )
    op.create_index(
        op.f("ix_inspection_videos_inspection_id"),
        "inspection_videos",
        ["inspection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inspection_videos_tenant_id"),
        "inspection_videos",
        ["tenant_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_inspection_videos_inspection_id"), table_name="inspection_videos")
    op.drop_index(op.f("ix_inspection_videos_tenant_id"), table_name="inspection_videos")
    op.drop_table("inspection_videos")
