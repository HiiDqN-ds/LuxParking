"""Delete users from the ParkPilot database.

Run from the `parkpilot` folder (the one with wsgi.py), with the virtual environment active:
    Windows:    .venv\\Scripts\\activate     then     python remove_users.py
    Mac/Linux:  . .venv/bin/activate        then     python remove_users.py

It lists what it will delete and asks for confirmation. Edit TARGETS below to change who is removed.
A target matches a user whose email equals it, or whose name equals it, or whose email starts with "<target>@".
It never deletes a platform admin or the owner set in .env (SEED_OWNER_EMAIL).
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

TARGETS = ["owner@example.com", "kkk@gmail.com", "kikoulila@gmail.com"]

from sqlalchemy import update  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import ApiAccessRequest, AuditLog, BookingExtra, Inspection, InspectionPhoto, Role, User  # noqa: E402
from app.tenancy import use_tenant  # noqa: E402

app = create_app()
with app.app_context():
    keep_email = os.environ.get("SEED_OWNER_EMAIL", "").strip().lower()
    with use_tenant(None):
        found = []
        for user in User.query.order_by(User.id).all():
            email, name = user.email.lower(), (user.name or "").strip().lower()
            hit = any(email == t.lower() or name == t.lower() or email.startswith(t.lower() + "@") for t in TARGETS)
            if hit and user.role in (Role.OWNER, Role.STAFF) and email != keep_email:
                found.append(user)
        if not found:
            print("No matching users found. Nothing to delete.")
            sys.exit(0)
        print("These users will be deleted:")
        for u in found:
            print(f"  id={u.id}  {u.email}  name={u.name!r}  role={u.role}")
        if input("Type YES to delete them: ").strip() != "YES":
            print("Cancelled.")
            sys.exit(0)
        for u in found:
            try:
                with db.session.begin_nested():
                    for model, column in (
                        (Inspection, "staff_user_id"), (InspectionPhoto, "staff_user_id"), (BookingExtra, "done_by_id"),
                        (AuditLog, "user_id"), (ApiAccessRequest, "requested_by_id"), (ApiAccessRequest, "decided_by_id"),
                    ):
                        db.session.execute(update(model).where(getattr(model, column) == u.id).values({column: None}))
                    db.session.delete(u)
                print(f"Deleted {u.email}")
            except Exception as exc:  # still referenced: switch it off instead
                u.active = False
                print(f"Could not delete {u.email} ({exc.__class__.__name__}); it was deactivated instead.")
        db.session.commit()
        print("Done. Remaining users:", [u.email for u in User.query.order_by(User.id).all()])
