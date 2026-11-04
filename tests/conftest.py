import os

import pytest
from sqlalchemy import text

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Role, Tenant, User
from app.seed import seed_demo


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    use_migrations = bool(os.environ.get("TEST_USE_MIGRATIONS"))  # build the schema exactly as production does
    with app.app_context():
        if use_migrations:
            from flask_migrate import upgrade
            upgrade()
        else:
            db.create_all()
        seed_demo(owner_email="owner@test.dev", owner_password="correct-horse")
        yield app
        db.session.remove()
        db.drop_all()
        if use_migrations:
            db.session.execute(text("DROP TABLE IF EXISTS alembic_version"))
            db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email="owner@test.dev", password="correct-horse"):
    return client.post("/admin/login", data={"email": email, "password": password})


@pytest.fixture()
def staff(app):
    tenant = Tenant.query.first()
    user = User(tenant_id=tenant.id, email="staff@test.dev", role=Role.STAFF)
    user.set_password("staff-pass")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def other_tenant(app):
    """A second tenant with one lot and one space, to prove isolation."""
    from app.models import Lot, Space
    t = Tenant(name="Other", slug="other")
    db.session.add(t)
    db.session.flush()
    lot = Lot(tenant_id=t.id, name="Other lot", slug="other-lot")
    db.session.add(lot)
    db.session.flush()
    space = Space(tenant_id=t.id, lot_id=lot.id, name="Z1")
    db.session.add(space)
    db.session.commit()
    from types import SimpleNamespace as NS
    out = NS(id=t.id), NS(id=lot.id), NS(id=space.id)
    # Tests share one session with the test client; clear the identity map so
    # lookups really hit the database (in production each request has its own session).
    db.session.expunge_all()
    return out
