"""Seat limit for per-person extras (airport shuttle: 2 persons including luggage)

Adds extras.max_persons. Existing shuttle extras that are priced per person get a limit of 2. Prices are not touched:
to charge the shuttle per person (price x persons) set "Persons included" to 0 on the extra in the back office.

Revision ID: d1e5a7c3b942
Revises: c9a4e6b21f85
"""
import sqlalchemy as sa
from alembic import op

revision = "d1e5a7c3b942"
down_revision = "c9a4e6b21f85"
branch_labels = None
depends_on = None


def _columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    if "max_persons" not in _columns("extras"):
        with op.batch_alter_table("extras") as batch:
            batch.add_column(sa.Column("max_persons", sa.Integer(), nullable=True))
    op.execute(
        sa.text("UPDATE extras SET max_persons = 2 WHERE kind = 'shuttle' AND per_person = :yes AND max_persons IS NULL")
        .bindparams(yes=True)
    )


def downgrade():
    if "max_persons" in _columns("extras"):
        with op.batch_alter_table("extras") as batch:
            batch.drop_column("max_persons")
