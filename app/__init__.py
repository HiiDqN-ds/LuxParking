"""ParkPilot application factory."""
import logging
import re

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFError
from flask_login import current_user
from sqlalchemy import text
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .cli import register_cli
from .config import DEV_SECRET, Config
from .extensions import csrf, db, login_manager, migrate, socketio
from .ui_i18n import ui
from .i18n import translate
from .models import Role, User
from .security import init_security


def create_app(config=None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config or Config)
    app.jinja_env.globals["t"] = translate
    from . import ui_i18n

    ui_i18n.init_app(app)
    logging.basicConfig(level=app.config.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if app.config["APP_ENV"] != "production":
        # Development: edited templates, CSS and JS show up on the next page load, no restart or hard refresh needed.
        app.config["TEMPLATES_AUTO_RELOAD"] = True
        app.jinja_env.auto_reload = True
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    if app.config["APP_ENV"] == "production":
        if app.config["SECRET_KEY"] == DEV_SECRET:
            raise RuntimeError("SECRET_KEY must be set in production")
        if len(app.config["SECRET_KEY"]) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production")
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render terminates TLS

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    socketio.init_app(
        app,
        async_mode="threading",
        manage_session=False,
        message_queue=app.config.get("SOCKETIO_MESSAGE_QUEUE"),
    )
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.session_protection = "strong"  # drop the session if the client fingerprint changes
    init_security(app)

    @login_manager.user_loader
    def load_user(session_id):
        """Resolve the session to a user. Deactivated users and sessions that predate a password change are refused."""
        import hmac

        try:
            raw_id, _, fingerprint = str(session_id).partition(":")
            user = db.session.get(User, int(raw_id))
        except (TypeError, ValueError):
            return None
        if user is None or not user.active:
            return None
        if not fingerprint or not hmac.compare_digest(fingerprint, user.session_fingerprint()):
            return None
        return user

    from .blueprints import admin, alerts, api, auth, billing, checkout, developers, legal, manage, public, reports, staff, widget
    socketio.on_event("join_lot", public.join_lot, namespace=public.BOOKING_NAMESPACE)
    socketio.on_event("leave_lot", public.leave_lot, namespace=public.BOOKING_NAMESPACE)
    app.register_blueprint(public.bp)
    app.register_blueprint(checkout.bp)
    app.register_blueprint(legal.bp)

    @app.context_processor
    def _inject_year():
        from datetime import datetime
        return {"now_year": datetime.now().year}

    @app.context_processor
    def _inject_business():
        from .business import business_profile
        return {"biz": business_profile()}
    app.register_blueprint(api.bp)
    app.register_blueprint(developers.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(manage.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(alerts.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(widget.bp)
    app.register_blueprint(staff.bp)
    register_cli(app)

    @app.before_request
    def set_tenant_context():
        """Bind every request to the logged-in user's tenant (read-scoping happens in models.py)."""
        g.tenant_id = None
        if current_user.is_authenticated:
            if current_user.role != Role.PLATFORM_OWNER and current_user.tenant_id is None:
                abort(403)
            if (
                current_user.role != Role.PLATFORM_OWNER
                and current_user.tenant is not None
                and current_user.tenant.status == "suspended"
                and request.endpoint != "auth.logout"
            ):
                abort(403)
            g.tenant_id = current_user.tenant_id

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # CSP, Permissions-Policy, HSTS and no-store caching for back-office pages live in app/security.py.
        return resp

    @app.get("/healthz")
    def healthz():
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ok")

    errors = {
        403: ("Not allowed", "Your account doesn't have permission for this action."),
        404: ("Page not found", "This page doesn't exist, or it belongs to another account."),
        405: ("Method not allowed", "This address doesn't accept that kind of request."),
        413: ("File too large", "The upload is larger than the allowed size."),
        429: ("Too many requests", "Please wait a moment and try again."),
        500: ("Something went wrong", "The problem was logged. Please try again in a moment."),
    }
    api_codes = {403: "forbidden", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large",
                 429: "rate_limit_exceeded", 500: "internal_error"}

    def render_error(error):
        code = error.code if isinstance(error, HTTPException) else 500
        if code == 500:
            db.session.rollback()  # never leave a failed transaction open for the next query
            app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        title, message = errors.get(code, ("Error", "The request could not be completed."))
        if request.path == "/v1" or request.path.startswith("/v1/"):
            return jsonify(error={"code": api_codes.get(code, "error"), "message": title}), code
        return render_template("error.html", code=code, title=ui(title), message=ui(message)), code

    for code in errors:
        app.register_error_handler(code, render_error)

    _book_path = re.compile(r"^(?:/(?P<locale>[a-z]{2}))?/reserve/(?P<tenant>[^/]+)/(?P<lot>[^/]+)/holds/(?P<hold>\d+)/book$")

    @app.errorhandler(CSRFError)
    def csrf_failed(error):
        """A stale or missing CSRF token (expired session, cookie overwritten by another local app, form open in two
        tabs). Send a customer straight back to their hold with a fresh form instead of a bare 400 page."""
        match = _book_path.match(request.path)
        if match:
            flash(translate(match.group("locale") or "en", "checkout.session_expired"), "error")
            return redirect(url_for("public.hold_confirmation", tenant_slug=match.group("tenant"),
                                    lot_slug=match.group("lot"), hold_id=int(match.group("hold"))))
        return render_error(error)

    _heal_local_schema(app)
    return app


def _heal_local_schema(app) -> None:
    """Local SQLite databases created before an update can miss newer tables: add only those, never alter anything.
    Fresh databases are left to the migrations; PostgreSQL always uses `flask db upgrade`."""
    if not str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite"):
        return
    try:
        from sqlalchemy import inspect

        from .models import Alert, ApiAccessRequest, BookingExtra, Extra, Invoice, Notification, Subscription

        with app.app_context():
            inspector = inspect(db.engine)
            names = set(inspector.get_table_names())
            if "tenants" not in names:
                return
            for model in (ApiAccessRequest, Extra, BookingExtra, Invoice, Notification, Subscription, Alert):
                model.__table__.create(bind=db.engine, checkfirst=True)
            have = {c["name"] for c in inspector.get_columns("tenants")}
            added = {"billing_exempt": "BOOLEAN NOT NULL DEFAULT 0", "vat_rate": "NUMERIC(5,2)", "vat_id": "VARCHAR(40)",
                     "invoice_seller": "TEXT", "invoice_seq": "INTEGER NOT NULL DEFAULT 0", "credit_seq": "INTEGER NOT NULL DEFAULT 0"}
            with db.engine.begin() as conn:
                for column, ddl in added.items():
                    if column not in have:
                        conn.execute(text(f"ALTER TABLE tenants ADD COLUMN {column} {ddl}"))
                        if column == "billing_exempt":  # databases that existed before billing keep running unlimited
                            conn.execute(text("UPDATE tenants SET billing_exempt = 1"))
                for table, wanted in (
                    ("tenants", {"logo_data": "BLOB", "logo_mime": "VARCHAR(40)"}),
                    ("users", {"alerts_seen_at": "DATETIME"}),
                    ("inspections", {"draft_details": "TEXT"}),
                    ("extras", {"included_persons": "INTEGER NOT NULL DEFAULT 0", "extra_person_price": "NUMERIC(10,2)", "max_persons": "INTEGER"}),
                    ("booking_extras", {"included_persons": "INTEGER NOT NULL DEFAULT 0", "extra_person_price": "NUMERIC(10,2) NOT NULL DEFAULT 0"}),
                ):
                    existing = {c["name"] for c in inspect(db.engine).get_columns(table)} if table in names else set()
                    for column, ddl in wanted.items():
                        if table in names and column not in existing:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                            if table == "extras" and column == "included_persons":
                                conn.execute(text("UPDATE extras SET included_persons = 2 WHERE kind = 'shuttle' AND per_person = 1"))
                            if table == "extras" and column == "max_persons":  # the shuttle seats 2 persons incl. luggage
                                conn.execute(text("UPDATE extras SET max_persons = 2 WHERE kind = 'shuttle' AND per_person = 1"))
    except Exception:  # never block start-up; `flask db upgrade` remains the proper path
        app.logger.exception("Could not add missing local tables")
