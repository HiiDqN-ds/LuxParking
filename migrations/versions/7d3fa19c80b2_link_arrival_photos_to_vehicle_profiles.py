"""Link arrival inspection photos to reusable vehicle profiles.

Revision ID: 7d3fa19c80b2
Revises: 0892d3efd4db
"""
from alembic import op
import sqlalchemy as sa


revision = "7d3fa19c80b2"
down_revision = "0892d3efd4db"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("inspection_photos") as batch_op:
        batch_op.add_column(sa.Column("vehicle_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_inspection_photos_vehicle_id", ["vehicle_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_inspection_photos_vehicle_id_vehicles",
            "vehicles",
            ["vehicle_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table("inspection_photos") as batch_op:
        batch_op.drop_constraint("fk_inspection_photos_vehicle_id_vehicles", type_="foreignkey")
        batch_op.drop_index("ix_inspection_photos_vehicle_id")
        batch_op.drop_column("vehicle_id")
