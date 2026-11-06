"""Configuration. Everything environment-specific comes from env vars (12-factor)."""
import os
from datetime import timedelta


def _normalize(url: str) -> str:
    """Render hands out postgres:// URLs; SQLAlchemy + psycopg3 needs postgresql+psycopg://."""
    for old in ("postgres://", "postgresql://"):
        if url.startswith(old):
            return url.replace(old, "postgresql+psycopg://", 1)
    return url


def _database_url() -> str:
    return _normalize(os.environ.get("DATABASE_URL", "sqlite:///parkpilot.db"))


DEV_SECRET = "dev-only-change-me"


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")  # "production" on Render
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET)
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = APP_ENV == "production"
    # "__Host-" cookies are only accepted by browsers when Secure, Path=/ and without a Domain: they cannot be
    # overwritten from a sibling subdomain. Only used in production because it requires HTTPS.
    # Locally, cookies are shared by every app on 127.0.0.1 whatever the port, so a generic "session" cookie from another
    # local app overwrites ours and checkout then fails with "400 Bad Request" (CSRF session token missing).
    SESSION_COOKIE_NAME = "__Host-session" if APP_ENV == "production" else "parkpilot_session"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = APP_ENV == "production"
    REMEMBER_COOKIE_SAMESITE = "Lax"
    # Sessions end after SESSION_LIFETIME_HOURS in any case and after IDLE_TIMEOUT_MINUTES without activity (0 = off).
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get("SESSION_LIFETIME_HOURS", "12")))
    IDLE_TIMEOUT_MINUTES = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "120"))
    # Rate limits share state through Redis when one is configured (several workers); otherwise per process.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI") or os.environ.get("SOCKETIO_MESSAGE_QUEUE") or "memory://"
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "1") == "1"
    RATELIMIT_HEADERS_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None
    DEFAULT_LOCALE = "de"
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
    LOGIN_IP_MAX_FAILURES = int(os.environ.get("LOGIN_IP_MAX_FAILURES", "20"))
    LOGIN_LOCK_MINUTES = int(os.environ.get("LOGIN_LOCK_MINUTES", "15"))
    SUPPORTED_LOCALES = tuple(dict.fromkeys(
        [
            locale.strip().lower()
            for locale in os.environ.get("SUPPORTED_LOCALES", "de,en,nl,fr,es,ar").split(",")
            if locale.strip()
        ] + ["tr"]
    ))
    RTL_LOCALES = ("ar", "he", "fa", "ur")
    SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
    # Address printed into QR codes. Empty = SITE_URL. Local runs set it to this computer's network address so phones can open it.
    QR_BASE_URL = os.environ.get("QR_BASE_URL", "").rstrip("/")
    API_DEFAULT_REQUESTS_PER_MINUTE = int(os.environ.get("API_DEFAULT_REQUESTS_PER_MINUTE", "60"))
    API_IDEMPOTENCY_HOURS = int(os.environ.get("API_IDEMPOTENCY_HOURS", "24"))
    WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "8"))
    WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "5"))
    PUBLIC_TENANT_SLUG = os.environ.get("PUBLIC_TENANT_SLUG", "luxury-parking")
    SOCKETIO_MESSAGE_QUEUE = os.environ.get("SOCKETIO_MESSAGE_QUEUE")
    INSPECTION_S3_BUCKET = os.environ.get("INSPECTION_S3_BUCKET")
    INSPECTION_S3_ENDPOINT_URL = os.environ.get("INSPECTION_S3_ENDPOINT_URL")
    INSPECTION_S3_REGION = os.environ.get("INSPECTION_S3_REGION", "eu-central-1")
    INSPECTION_S3_ACCESS_KEY_ID = os.environ.get("INSPECTION_S3_ACCESS_KEY_ID")
    INSPECTION_S3_SECRET_ACCESS_KEY = os.environ.get("INSPECTION_S3_SECRET_ACCESS_KEY")
    INSPECTION_MAX_PHOTO_BYTES = int(os.environ.get("INSPECTION_MAX_PHOTO_BYTES", 8 * 1024 * 1024))
    INSPECTION_MAX_VIDEO_BYTES = int(os.environ.get("INSPECTION_MAX_VIDEO_BYTES", 8 * 1024 * 1024))
    INSPECTION_MAX_IMAGE_PIXELS = int(os.environ.get("INSPECTION_MAX_IMAGE_PIXELS", 12_000_000))
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_FROM = os.environ.get("SMTP_FROM")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
    STRIPE_PAYMENT_METHOD_TYPES = os.environ.get("STRIPE_PAYMENT_METHOD_TYPES", "")  # empty = the methods enabled in the Stripe Dashboard

    # --- invoices and customer notifications -------------------------------------------------------
    # VAT percentage included in gross prices when a tenant has not set its own. 19 is the German standard rate:
    # confirm it (and whether it applies to your parking services) with your tax advisor.
    DEFAULT_VAT_RATE = os.environ.get("DEFAULT_VAT_RATE", "19")
    OWNER_NOTIFY_EMAIL = os.environ.get("OWNER_NOTIFY_EMAIL", "")      # gets a blind copy of new / cancelled booking emails
    REVIEW_URL = os.environ.get("REVIEW_URL", "")                      # e.g. your Google review link, sent after check-out
    REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "24"))       # reminder this long before pick-up
    # SMS / WhatsApp through Twilio. Nothing is sent until the credentials and a sender are configured.
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_SMS_FROM = os.environ.get("TWILIO_SMS_FROM", "")            # number or alphanumeric sender id
    TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")
    TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. +4915112345678 (WhatsApp-enabled sender)
    SMS_EVENTS = tuple(e.strip() for e in os.environ.get("SMS_EVENTS", "reserved,cancelled,reminder").split(",") if e.strip())
    WHATSAPP_EVENTS = tuple(e.strip() for e in os.environ.get("WHATSAPP_EVENTS", "reserved,cancelled,reminder").split(",") if e.strip())
    # Local numbers (starting with 0) are only converted when you set a country code such as +49. Otherwise they are skipped.
    DEFAULT_PHONE_COUNTRY_CODE = os.environ.get("DEFAULT_PHONE_COUNTRY_CODE", "")

    # --- AI chat assistant on the public pages (Claude). Without ANTHROPIC_API_KEY the chat still opens and answers with
    # the parking's own facts (prices, extras, contact) instead of free-form AI replies. ---------------------------------
    AI_CHAT_ENABLED = os.environ.get("AI_CHAT_ENABLED", "1").strip().lower() not in {"0", "false", "no", ""}
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
    AI_CHAT_MAX_TOKENS = int(os.environ.get("AI_CHAT_MAX_TOKENS", "500"))
    AI_CHAT_DAILY_LIMIT = int(os.environ.get("AI_CHAT_DAILY_LIMIT", "2000"))  # AI replies per day and worker (cost cap)

    # --- platform subscriptions (Stripe Billing) ---------------------------------------------------
    # Create the three recurring prices in Stripe and paste their ids. No price is invented here: plans without an id
    # cannot be bought. STRIPE_BILLING_WEBHOOK_SECRET belongs to the endpoint /webhooks/stripe-billing.
    TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
    STRIPE_BILLING_WEBHOOK_SECRET = os.environ.get("STRIPE_BILLING_WEBHOOK_SECRET", "")
    STRIPE_PRICE_STARTER = os.environ.get("STRIPE_PRICE_STARTER", "")
    STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")
    STRIPE_PRICE_BUSINESS = os.environ.get("STRIPE_PRICE_BUSINESS", "")
    # Plan limits (spaces / bookings per month / API requests per month; 0 = unlimited). PLACEHOLDERS: set your own
    # numbers with PLAN_LIMITS_JSON, e.g. {"starter":{"spaces":25,"bookings":300,"api_requests":5000}, ...}
    PLAN_LIMITS_JSON = os.environ.get("PLAN_LIMITS_JSON", "")


class TestConfig(Config):
    TESTING = True
    # Set TEST_DATABASE_URL to run the whole suite against PostgreSQL (what production uses).
    SQLALCHEMY_DATABASE_URI = _normalize(os.environ["TEST_DATABASE_URL"]) if os.environ.get("TEST_DATABASE_URL") else "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_NAME = "session"
    RATELIMIT_ENABLED = False  # tests/test_security_hardening.py turns it back on where it is the subject
    RATELIMIT_STORAGE_URI = "memory://"
    SECRET_KEY = "test"
    UI_FORCE_LOCALE = "en"  # older tests assert English text; tests/test_ui_translations.py overrides this
