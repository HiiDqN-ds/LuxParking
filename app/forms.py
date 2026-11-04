from flask_wtf import FlaskForm as _FlaskForm
from wtforms import BooleanField, DateField, FloatField, IntegerField, PasswordField, RadioField, SelectField, SelectMultipleField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, InputRequired, Length, NumberRange, Optional, URL, ValidationError

from .models import SPACE_KINDS, SPACE_STATUSES
from .password_policy import MAX_LENGTH as PASSWORD_MAX, password_validator
from .ui_i18n import ui
from .services.inspections import REQUIRED_PHOTO_CATEGORIES
from .services.messaging import normalize_phone

class FlaskForm(_FlaskForm):
    """Every form shows its labels, choices and validation errors in the page language."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self:
            if field.label is not None and field.label.text:
                field.label.text = ui(field.label.text)
            choices = getattr(field, "choices", None)
            if choices:
                field.choices = [(value, ui(label) if isinstance(label, str) else label) for value, label in choices]

    def validate(self, *args, **kwargs):
        valid = super().validate(*args, **kwargs)
        for field in self:
            field.errors = [ui(error) for error in field.errors]
        return valid


KIND_LABELS = {"small": "Small car", "suv": "SUV", "van": "Van"}
STATUS_LABELS = {"active": "Available to book", "optional": "Optional extra", "blocked": "Blocked"}


def phone_validator(form, field):
    """Accept an empty value, or a number that can be turned into +<country code><number>."""
    if field.data and field.data.strip() and normalize_phone(field.data) is None:
        raise ValidationError("Enter the phone number with country code, for example +4915112345678.")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=255)])


class ForgotPasswordForm(FlaskForm):
    identifier = StringField("Email or phone number", validators=[DataRequired(), Length(max=255)])
    method = RadioField("How do you want to verify it is you?", choices=[("email", "Send me a link by email"), ("sms", "Send me a code by text message")], default="email")


class ResetPasswordForm(FlaskForm):
    new_password = PasswordField("New password", validators=[DataRequired(), Length(max=PASSWORD_MAX)])
    confirm_password = PasswordField("Confirm new password", validators=[DataRequired(), Length(max=PASSWORD_MAX)])


class ResetCodeForm(ResetPasswordForm):
    identifier = StringField("Email or phone number", validators=[DataRequired(), Length(max=255)])
    code = StringField("6-digit code", validators=[DataRequired(), Length(min=6, max=6)])


class ProfileForm(FlaskForm):
    name = StringField("Full name", validators=[Optional(), Length(max=120)])
    phone = StringField("Mobile phone number", validators=[Optional(), Length(max=32), phone_validator])


class EmailChangeForm(FlaskForm):
    new_email = StringField("New email address", filters=[lambda value: value.strip() if value else value], validators=[DataRequired(), Email(), Length(max=255)])
    current_password = PasswordField("Current password", validators=[DataRequired(), Length(max=PASSWORD_MAX)])


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired(), Length(max=PASSWORD_MAX)])
    new_password = PasswordField("New password", validators=[DataRequired(), password_validator()])
    confirm_password = PasswordField("Confirm new password", validators=[DataRequired(), Length(max=PASSWORD_MAX)])


class LotForm(FlaskForm):
    name = StringField("Lot name", validators=[DataRequired(), Length(max=120)])
    address = StringField("Address", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=80)])
    lat = FloatField("Latitude", validators=[Optional(), NumberRange(-90, 90)])
    lng = FloatField("Longitude", validators=[Optional(), NumberRange(-180, 180)])
    canvas_w = IntegerField("Map width", default=1000, validators=[InputRequired(), NumberRange(200, 4000)])
    canvas_h = IntegerField("Map height", default=700, validators=[InputRequired(), NumberRange(200, 4000)])
    space_count = IntegerField("Number of spaces", default=1, validators=[Optional(), NumberRange(1, 500)])
    covered_count = IntegerField("Covered spaces", default=0, validators=[Optional(), NumberRange(0, 500)])
    ev_count = IntegerField("EV charging spaces", default=0, validators=[Optional(), NumberRange(0, 500)])
    background_url = StringField("Lot image URL", validators=[Optional(), URL(), Length(max=500)])
    base_daily_rate = FloatField("Base daily rate", default=0.0, validators=[Optional(), NumberRange(0, 100000)])
    currency = StringField("Currency", default="EUR", validators=[Optional(), Length(max=3)])
    active = BooleanField("Lot is active", default=True)


class SpaceForm(FlaskForm):
    name = StringField("Space name", validators=[DataRequired(), Length(max=30)])
    kind = SelectField("Vehicle size", choices=[(k, KIND_LABELS[k]) for k in SPACE_KINDS])
    status = SelectField("Status", choices=[(s, STATUS_LABELS[s]) for s in SPACE_STATUSES])
    covered = BooleanField("Covered")
    ev_charging = BooleanField("EV charging")
    bus_distance_m = IntegerField("Distance to shuttle stop (m)", validators=[Optional(), NumberRange(0, 20000)])
    photo_url = StringField("Photo URL", validators=[Optional(), URL(), Length(max=500)])
    base_price = FloatField("Base price", default=0.0, validators=[Optional(), NumberRange(0, 100000)])
    min_hours = IntegerField("Minimum stay (hours)", default=1, validators=[Optional(), NumberRange(1, 168)])
    max_days = IntegerField("Max booking length (days)", default=30, validators=[Optional(), NumberRange(1, 365)])
    default_available = BooleanField("Available by default", default=True)
    x = IntegerField("X", validators=[InputRequired(), NumberRange(0, 4000)])
    y = IntegerField("Y", validators=[InputRequired(), NumberRange(0, 4000)])
    w = IntegerField("Width", validators=[InputRequired(), NumberRange(10, 1000)])
    h = IntegerField("Height", validators=[InputRequired(), NumberRange(10, 1000)])
    notes = TextAreaField("Internal notes", validators=[Optional(), Length(max=2000)])


class InspectionSettingsForm(FlaskForm):
    retention_days = IntegerField(
        "Evidence retention (days)",
        validators=[InputRequired(), NumberRange(min=1, max=3650)],
    )
    required_photos = SelectMultipleField(
        "Required vehicle photo angles",
        choices=[(category, category.replace("_", " ").title()) for category in REQUIRED_PHOTO_CATEGORIES],
        validators=[DataRequired()],
    )


class TeamMemberForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Temporary password", validators=[DataRequired(), password_validator()])
    phone = StringField("Phone number (optional)", validators=[Optional(), Length(max=32), phone_validator])
    role = SelectField("Role", choices=[("staff", "Staff")], validators=[DataRequired()])


class TeamMemberEditForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("New password (leave empty to keep the current one)", validators=[Optional(), password_validator()])
    phone = StringField("Phone number (optional)", validators=[Optional(), Length(max=32), phone_validator])


class ApiKeyForm(FlaskForm):
    name = StringField("Key name", validators=[DataRequired(), Length(max=100)])
    key_type = SelectField("Key type", choices=[("pk", "Public key"), ("sk", "Secret key")])
    scopes = SelectMultipleField(
        "Permissions",
        choices=[
            ("map:read", "Read lot maps"),
            ("availability:read", "Read availability"),
            ("products:read", "Read products"),
            ("quotes:write", "Create quotes"),
            ("holds:write", "Create holds"),
            ("bookings:write", "Create bookings"),
            ("bookings:read", "Read bookings"),
            ("inspections:read", "Read inspections"),
            ("inspections:write", "Write inspections"),
            ("keys:manage", "Manage API keys"),
            ("webhooks:manage", "Manage webhooks"),
            ("usage:read", "Read API usage"),
        ],
        validators=[DataRequired()],
    )
    allowed_origins = StringField("Allowed HTTPS origins (comma-separated)", validators=[Optional(), Length(max=2000)])
    requests_per_minute = IntegerField(
        "Requests per minute",
        default=60,
        validators=[InputRequired(), NumberRange(min=1, max=10000)],
    )


class WebhookForm(FlaskForm):
    name = StringField("Endpoint name", validators=[DataRequired(), Length(max=100)])
    url = StringField("HTTPS endpoint URL", validators=[DataRequired(), URL(), Length(max=2000)])
    events = SelectMultipleField(
        "Events",
        choices=[
            ("booking.created", "Booking created"),
            ("booking.paid", "Booking paid"),
            ("booking.cancelled", "Booking cancelled"),
            ("booking.checked_in", "Booking checked in"),
            ("booking.checked_out", "Booking checked out"),
            ("inspection.completed", "Inspection completed"),
            ("dispute.opened", "Dispute opened"),
        ],
        validators=[DataRequired()],
    )


class PriceRuleForm(FlaskForm):
    name = StringField("Rule name", validators=[DataRequired(), Length(max=80)])
    lot_id = SelectField("Parking lot", coerce=int, validators=[InputRequired()])
    space_id = SelectField("Applies to", coerce=int, validators=[InputRequired()])
    period = SelectField(
        "Date pattern",
        choices=[
            ("day", "Any date"),
            ("weekend", "Overlaps Saturday or Sunday"),
            ("season", "Overlaps configured season dates"),
            ("holiday", "Overlaps configured holiday dates"),
        ],
        validators=[DataRequired()],
    )
    mode = SelectField(
        "Rate calculation",
        choices=[
            ("flat", "One amount for the whole stay"),
            ("per_day", "Amount per started 24-hour period"),
            ("per_hour", "Amount per started hour"),
        ],
        validators=[DataRequired()],
    )
    amount = FloatField("Amount", validators=[InputRequired(), NumberRange(min=0, max=1000000)])
    start_day = DateField("Start date", format="%Y-%m-%d", validators=[Optional()])
    end_day = DateField("End date", format="%Y-%m-%d", validators=[Optional()])
    priority = IntegerField("Priority (higher wins)", default=100, validators=[InputRequired(), NumberRange(-100000, 100000)])
    active = BooleanField("Rule is active", default=True)
    notes = TextAreaField("Internal notes", validators=[Optional(), Length(max=2000)])


class DiscountCodeForm(FlaskForm):
    code = StringField("Code", validators=[DataRequired(), Length(min=1, max=40)])
    discount_type = SelectField(
        "Discount type",
        choices=[("percentage", "Percentage"), ("fixed", "Fixed amount")],
        validators=[DataRequired()],
    )
    value = FloatField("Discount value", validators=[InputRequired(), NumberRange(min=0.01, max=1000000)])
    starts_on = DateField("Valid from", format="%Y-%m-%d", validators=[Optional()])
    ends_on = DateField("Valid through", format="%Y-%m-%d", validators=[Optional()])
    minimum_amount = FloatField(
        "Minimum booking amount",
        default=0,
        validators=[InputRequired(), NumberRange(min=0, max=1000000)],
    )
    max_redemptions = IntegerField(
        "Maximum redemptions (blank for unlimited)",
        validators=[Optional(), NumberRange(min=1, max=100000000)],
    )
    active = BooleanField("Code is active", default=True)


class BrandSettingsForm(FlaskForm):
    logo_url = StringField("Logo HTTPS URL", validators=[Optional(), URL(), Length(max=500)])
    primary_color = StringField("Primary color", validators=[DataRequired(), Length(min=7, max=7)])
    accent_color = StringField("Accent color", validators=[DataRequired(), Length(min=7, max=7)])
    custom_domain = StringField("Custom domain", validators=[Optional(), Length(max=253)])


class TenantTranslationForm(FlaskForm):
    locale = SelectField("Language", validators=[DataRequired()])
    key = SelectField("Catalog string", validators=[DataRequired()])
    value = TextAreaField("Tenant text", validators=[DataRequired(), Length(max=4000)])


class TenantPageForm(FlaskForm):
    kind = SelectField("Content type", choices=[("blog", "Blog article"), ("faq", "FAQ")], validators=[DataRequired()])
    locale = SelectField("Language", validators=[DataRequired()])
    slug = StringField("URL slug", validators=[DataRequired(), Length(min=1, max=160)])
    title = StringField("Title", validators=[DataRequired(), Length(max=180)])
    summary = StringField("Summary", validators=[Optional(), Length(max=320)])
    body = TextAreaField("Content", validators=[DataRequired(), Length(max=30000)])
    seo_title = StringField("SEO title", validators=[Optional(), Length(max=180)])
    seo_description = TextAreaField("SEO description", validators=[Optional(), Length(max=320)])
    published = BooleanField("Published")
