import click
from flask import Flask
from sqlalchemy import select

from .extensions import db
from .models import Role, Tenant, User
from .seed import SPACE_COUNT, ensure_default_extras, ensure_platform_admin, seed_demo, sync_dev_passwords
from .services.inspections import purge_expired_inspections
from .services.api_platform import ApiPlatformError, deliver_pending_webhooks, issue_api_key
from .tenancy import use_tenant


def register_cli(app: Flask) -> None:
    @app.cli.command("create-platform-owner")
    @click.option("--email", required=True, help="Unique login email for the platform owner.")
    @click.password_option("--password", confirmation_prompt=True)
    def create_platform_owner(email, password):
        """Create the first platform-owner account without a tenant."""
        normalized_email = email.strip().lower()
        if len(password) < 12:
            raise click.ClickException("Password must contain at least 12 characters.")
        if User.query.filter_by(email=normalized_email).first():
            raise click.ClickException("A user with that email already exists.")
        user = User(
            tenant_id=None,
            email=normalized_email,
            name="Platform Owner",
            role=Role.PLATFORM_OWNER,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created platform owner account for {normalized_email}.")

    @app.cli.command("reset-user-password")
    @click.option("--email", required=True, help="Email address of the existing account.")
    @click.password_option("--password", confirmation_prompt=True)
    def reset_user_password(email, password):
        """Reset an existing user's password without displaying it."""
        if len(password) < 12:
            raise click.ClickException("Password must contain at least 12 characters.")
        normalized_email = email.strip().lower()
        with use_tenant(None):
            user = db.session.execute(
                select(User).where(User.email == normalized_email)
            ).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"No user was found with email '{normalized_email}'.")
        user.set_password(password)
        db.session.commit()
        click.echo(f"Password reset for {normalized_email}.")

    @app.cli.command("seed")
    @click.option("--spaces", default=SPACE_COUNT, show_default=True)
    def seed(spaces):
        """Create the starter tenant, owner login and parking spaces (safe to run repeatedly)."""
        tenant, password = seed_demo(spaces=spaces)
        admin_email = ensure_platform_admin()
        if admin_email:
            click.echo(f"Platform admin ready: {admin_email}")
        for email in sync_dev_passwords():
            click.echo(f"Login ready (password taken from .env): {email}")
        if ensure_default_extras():
            click.echo("Added starter shuttle and cleaning options (edit prices under Admin > Extras).")
        if password:
            click.echo(f"Created tenant '{tenant.name}'. Generated owner password (change it): {password}")
        else:
            click.echo(f"Tenant '{tenant.name}' ready.")

    @app.cli.command("test-email")
    @click.argument("to")
    def test_email(to):
        """Send a test email and show the real error if it fails (check your SMTP_* settings with this)."""
        from email.message import EmailMessage

        from .services.booking_notifications import smtp_send

        cfg = app.config
        if not cfg.get("SMTP_HOST") or not cfg.get("SMTP_FROM"):
            raise click.ClickException("SMTP_HOST and SMTP_FROM are empty. Fill in the SMTP_ lines in .env first.")
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = "ParkPilot test email", cfg["SMTP_FROM"], to
        message.set_content("Email sending works. Booking confirmations with the PDF ticket will arrive like this.")
        try:
            smtp_send(message)
        except Exception as exc:
            hint = ""
            if "535" in str(exc) or "Authentication" in str(exc):
                hint = "\nGmail: use an App Password (Google Account > Security > 2-Step Verification > App passwords), not your normal password."
            raise click.ClickException(f"Sending failed: {exc!r}{hint}")
        click.echo(f"Test email sent to {to}.")

    @app.cli.command("purge-inspection-evidence")
    def purge_inspection_evidence():
        """Delete expired inspection photos unless the booking has an open dispute."""
        tenant_ids = db.session.execute(select(Tenant.id).order_by(Tenant.id)).scalars().all()
        deleted = sum(purge_expired_inspections(tenant_id=tenant_id) for tenant_id in tenant_ids)
        click.echo(f"Deleted {deleted} expired inspection(s).")

    @app.cli.command("i18n-missing")
    @click.option("--write", is_flag=True, help="Add missing texts (in English) to the catalogs so they can be translated.")
    def i18n_missing(write):
        """List interface texts that have no translation in some language."""
        import json

        from .i18n import supported_locales
        from .ui_extract import UI_DIR, load, missing

        found = missing([l for l in supported_locales() if l != "en"])
        for locale, keys in found.items():
            click.echo(f"{locale}: {len(keys)} missing")
            for key in keys[:20]:
                click.echo(f"   {key}")
            if write and keys:
                data = load(locale)
                data.update({key: key for key in keys})
                UI_DIR.mkdir(parents=True, exist_ok=True)
                (UI_DIR / f"{locale}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @app.cli.command("send-reminders")
    def send_reminders():
        """Email/SMS/WhatsApp customers whose pick-up is within REMINDER_HOURS. Schedule hourly (cron / Render cron job)."""
        from .services.booking_events import send_due_reminders

        click.echo(f"Reminders sent: {send_due_reminders()}")

    @app.cli.command("deliver-webhooks")
    @click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
    def deliver_webhooks(limit):
        """Deliver due webhook events and schedule retries for failures."""
        result = deliver_pending_webhooks(limit=limit)
        click.echo(
            f"Delivered {result['delivered']}; retrying {result['retrying']}; "
            f"failed {result['failed']}."
        )

    @app.cli.command("create-api-key")
    @click.option("--tenant", "tenant_slug", required=True)
    @click.option("--name", "key_name", required=True)
    @click.option("--type", "key_type", type=click.Choice(["pk", "sk"]), required=True)
    @click.option("--scope", "scopes", multiple=True, required=True)
    @click.option("--origin", "origins", multiple=True)
    @click.option("--requests-per-minute", type=click.IntRange(1, 10000), default=60, show_default=True)
    def create_api_key(tenant_slug, key_name, key_type, scopes, origins, requests_per_minute):
        """Create a scoped API key and print its token once for secure storage."""
        tenant = db.session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        ).scalar_one_or_none()
        if tenant is None:
            raise click.ClickException(f"Tenant '{tenant_slug}' was not found.")
        try:
            key, token = issue_api_key(
                tenant_id=tenant.id,
                name=key_name,
                key_type=key_type,
                scopes=list(scopes),
                allowed_origins=list(origins),
                requests_per_minute=requests_per_minute,
            )
        except ApiPlatformError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo(f"API key: {key.prefix}")
        click.echo(f"Token (shown once): {token}")
        click.echo("Store this token securely; only its hash is retained.")
