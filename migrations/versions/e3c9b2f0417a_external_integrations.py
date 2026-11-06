"""Add provider-neutral external integration records.

Revision ID: e3c9b2f0417a
Revises: c9e5a2b7d106
"""
import sqlalchemy as sa
from alembic import op


revision = "e3c9b2f0417a"
down_revision = "c9e5a2b7d106"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_integration_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('parkos', 'parkenlist')", name="ck_external_integration_provider"),
        sa.CheckConstraint("status IN ('pending', 'active', 'syncing', 'error', 'disabled')", name="ck_external_integration_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_external_integration_tenant_provider"),
    )
    op.create_index("ix_external_integration_connections_tenant_id", "external_integration_connections", ["tenant_id"])

    op.create_table(
        "external_integration_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("external_location_id", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["external_integration_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "external_location_id", name="uq_external_location_connection_external_id"),
        sa.UniqueConstraint("connection_id", "lot_id", name="uq_external_location_connection_lot"),
    )
    op.create_index("ix_external_integration_locations_tenant_id", "external_integration_locations", ["tenant_id"])
    op.create_index("ix_external_integration_locations_connection_id", "external_integration_locations", ["connection_id"])
    op.create_index("ix_external_integration_locations_lot_id", "external_integration_locations", ["lot_id"])

    op.create_table(
        "external_booking_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("external_booking_id", sa.String(length=160), nullable=False),
        sa.Column("external_location_id", sa.String(length=160), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["connection_id"], ["external_integration_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "booking_id", name="uq_external_booking_connection_booking"),
        sa.UniqueConstraint("connection_id", "external_booking_id", name="uq_external_booking_connection_external_id"),
    )
    op.create_index("ix_external_booking_mappings_tenant_id", "external_booking_mappings", ["tenant_id"])
    op.create_index("ix_external_booking_mappings_connection_id", "external_booking_mappings", ["connection_id"])
    op.create_index("ix_external_booking_mappings_booking_id", "external_booking_mappings", ["booking_id"])

    op.create_table(
        "external_integration_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records_processed", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=False),
        sa.CheckConstraint("direction IN ('import', 'export')", name="ck_external_sync_direction"),
        sa.CheckConstraint("records_processed >= 0", name="ck_external_sync_records_processed"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'partial', 'failed')", name="ck_external_sync_status"),
        sa.ForeignKeyConstraint(["connection_id"], ["external_integration_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_integration_sync_runs_tenant_id", "external_integration_sync_runs", ["tenant_id"])
    op.create_index("ix_external_integration_sync_runs_connection_id", "external_integration_sync_runs", ["connection_id"])


def downgrade():
    op.drop_index("ix_external_integration_sync_runs_connection_id", table_name="external_integration_sync_runs")
    op.drop_index("ix_external_integration_sync_runs_tenant_id", table_name="external_integration_sync_runs")
    op.drop_table("external_integration_sync_runs")
    op.drop_index("ix_external_booking_mappings_booking_id", table_name="external_booking_mappings")
    op.drop_index("ix_external_booking_mappings_connection_id", table_name="external_booking_mappings")
    op.drop_index("ix_external_booking_mappings_tenant_id", table_name="external_booking_mappings")
    op.drop_table("external_booking_mappings")
    op.drop_index("ix_external_integration_locations_lot_id", table_name="external_integration_locations")
    op.drop_index("ix_external_integration_locations_connection_id", table_name="external_integration_locations")
    op.drop_index("ix_external_integration_locations_tenant_id", table_name="external_integration_locations")
    op.drop_table("external_integration_locations")
    op.drop_index("ix_external_integration_connections_tenant_id", table_name="external_integration_connections")
    op.drop_table("external_integration_connections")
