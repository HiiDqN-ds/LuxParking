"""Helpers for setting the tenant context outside of a logged-in request (CLI, tests)."""
from contextlib import contextmanager

from flask import g


@contextmanager
def use_tenant(tenant_id):
    """Temporarily scope all ORM reads/writes to `tenant_id` (None = unscoped)."""
    previous = g.get("tenant_id")
    g.tenant_id = tenant_id
    try:
        yield
    finally:
        g.tenant_id = previous
