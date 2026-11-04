from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func

from ..ui_i18n import ui
from ..extensions import db
from ..forms import ChangePasswordForm, EmailChangeForm, ForgotPasswordForm, LoginForm, ProfileForm, ResetCodeForm, ResetPasswordForm
from ..models import PasswordReset, Role, User, utcnow
from ..security import limiter
from ..tenancy import use_tenant
from ..password_policy import validate_password
from ..services import account as account_service, avatar as avatar_service, login_guard, password_reset
from ..services.messaging import normalize_phone
from ..utils import audit, is_safe_next

bp = Blueprint("auth", __name__, url_prefix="/admin")

# One message for "wrong password", "unknown email" and "account locked": the response must not reveal which
# of the three it was, otherwise the login form can be used to find out which email addresses have accounts.
_LOGIN_FAILED = "Email or password is incorrect, or the account is temporarily locked."

# Verified against when the email is unknown, so unknown and known accounts take the same time to answer.
_DUMMY_HASH = None


def _dummy_check(raw: str) -> None:
    global _DUMMY_HASH
    from werkzeug.security import check_password_hash, generate_password_hash

    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("not-a-real-password")
    check_password_hash(_DUMMY_HASH, raw)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute; 100 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        with use_tenant(None):  # email is globally unique; tenant is derived from the user
            user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if login_guard.ip_blocked(request.remote_addr):
            flash(ui("Too many failed attempts. Please wait a few minutes and try again."), "error")
            return render_template("admin/login.html", form=form), 429
        if user is None:
            _dummy_check(form.password.data)
        locked = login_guard.is_locked(user)
        password_ok = bool(user and user.check_password(form.password.data))
        if user and user.active and password_ok and not locked:
            if user.tenant_id and user.tenant.status == "suspended":
                flash(ui("This tenant account is suspended. Contact platform support."), "error")
            else:
                session.clear()  # new session id on privilege change: no session fixation
                login_user(user)
                session.permanent = True
                login_guard.record_success(user)
                user.last_login_at = utcnow()
                audit("login", "user", user.id)
                db.session.commit()
                nxt = request.args.get("next")
                return redirect(nxt if is_safe_next(nxt) else url_for("admin.dashboard"))
        else:
            if not locked:  # a locked account is not extended by further attempts
                login_guard.record_failure(user, request.remote_addr)
                db.session.commit()
            flash(ui(_LOGIN_FAILED), "error")
    return render_template("admin/login.html", form=form)


@bp.post("/logout")
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


# --- my profile: photo, name, phone, email, password ----------------------------------------------------

def _account_page(section: str = "", **forms):
    """The profile page. A form that failed validation is passed back in `forms` so its errors are shown in place."""
    layout = "staff/base.html" if current_user.role == Role.STAFF else "admin/base.html"
    return render_template(
        "admin/account.html", layout=layout, section=section,
        profile_form=forms.get("profile") or ProfileForm(obj=current_user),
        email_form=forms.get("email") or EmailChangeForm(),
        password_form=forms.get("password") or ChangePasswordForm(),
    )


def _saved(message: str, anchor: str):
    flash(message, "ok")
    return redirect(url_for("auth.account") + "#" + anchor)


@bp.get("/account")
@login_required
def account():
    return _account_page()


@bp.post("/account/profile")
@login_required
@limiter.limit("20 per minute")
def account_profile():
    form = ProfileForm()
    if not form.validate_on_submit():
        return _account_page("profile", profile=form)
    user = db.session.get(User, current_user.id)
    user.name = (form.name.data or "").strip()
    user.phone = normalize_phone(form.phone.data) if (form.phone.data or "").strip() else None
    audit("account.profile", "user", user.id)
    db.session.commit()
    return _saved(ui("Your profile was saved."), "profile")


@bp.post("/account/photo")
@login_required
@limiter.limit("10 per minute")
def account_photo():
    user = db.session.get(User, current_user.id)
    if request.form.get("remove"):
        user.avatar_data = user.avatar_mime = user.avatar_updated_at = None
        audit("account.photo.remove", "user", user.id)
        db.session.commit()
        return _saved(ui("Your photo was removed."), "photo")
    try:
        data, mime = avatar_service.process_avatar(request.files.get("photo"))
    except avatar_service.AvatarError as exc:
        messages = {
            "empty": ui("Choose a photo to upload."),
            "too_large": ui("The photo is larger than 3 MB. Choose a smaller one."),
            "format": ui("Only PNG, JPEG and WebP photos are accepted."),
            "pixels": ui("The photo has too many pixels. Choose a smaller one."),
        }
        flash(messages.get(exc.code, ui("The uploaded file is not a valid image.")), "error")
        return redirect(url_for("auth.account") + "#photo")
    user.avatar_data, user.avatar_mime, user.avatar_updated_at = data, mime, utcnow()
    audit("account.photo", "user", user.id)
    db.session.commit()
    return _saved(ui("Your photo was updated."), "photo")


@bp.post("/account/email")
@login_required
@limiter.limit("5 per minute; 20 per hour")
def account_email():
    """Change the sign-in email. Needs the current password; the old address is told about it."""
    form = EmailChangeForm()
    if not form.validate_on_submit():
        return _account_page("email", email=form)
    user = db.session.get(User, current_user.id)
    new_email = form.new_email.data.strip().lower()
    if not user.check_password(form.current_password.data):
        form.current_password.errors.append("The current password is incorrect.")
    elif new_email == user.email.lower():
        form.new_email.errors.append("This is already your email address.")
    elif User.query.filter(func.lower(User.email) == new_email, User.id != user.id).first() is not None:
        form.new_email.errors.append("This email address is already used by another account.")
    else:
        old_email, user.email = user.email, new_email
        for pending in PasswordReset.query.filter_by(user_id=user.id, used_at=None).all():
            pending.used_at = utcnow()  # an open reset link must not outlive the address it was meant for
        audit("account.email", "user", user.id, old=old_email, new=new_email)
        db.session.commit()
        account_service.notify_email_changed(old_email, new_email)
        return _saved(ui("Your email address was changed. Use it the next time you sign in."), "email")
    return _account_page("email", email=form)


@bp.get("/avatar/<int:user_id>")
@login_required
def avatar(user_id):
    """A profile photo: yours, a colleague's in the same parking, or (platform admin) anyone's."""
    user = db.session.get(User, user_id)
    if user is None or user.avatar_updated_at is None or not user.avatar_data:
        abort(404)
    me = current_user
    if not (me.id == user.id or me.role == Role.PLATFORM_OWNER or (me.tenant_id and me.tenant_id == user.tenant_id)):
        abort(404)
    return Response(user.avatar_data, mimetype=user.avatar_mime or "image/webp",
                    headers={"Cache-Control": "private, max-age=86400", "Vary": "Cookie"})


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def change_password():
    """Self-service password change. Every other session of the account is signed out (the session id is bound
    to the password hash), and this one is renewed."""
    if request.method == "GET":
        return redirect(url_for("auth.account") + "#password")
    form = ChangePasswordForm()
    if form.validate_on_submit():
        user = db.session.get(User, current_user.id)
        if not user.check_password(form.current_password.data):
            form.current_password.errors.append("The current password is incorrect.")
        elif form.new_password.data != form.confirm_password.data:
            form.confirm_password.errors.append("The two new passwords do not match.")
        elif form.new_password.data == form.current_password.data:
            form.new_password.errors.append("The new password must be different from the current one.")
        else:
            user.set_password(form.new_password.data)
            audit("password.change", "user", user.id)
            db.session.commit()
            session.clear()
            login_user(user)
            session.permanent = True
            return _saved(ui("Your password was changed. All other sessions were signed out."), "password")
    return _account_page("password", password=form)


# --- forgot password: emailed link or texted code ----------------------------------------------------------

def _password_problems(user, new_password, confirm_password) -> list[str]:
    if new_password != confirm_password:
        return ["The two new passwords do not match."]
    return validate_password(new_password, email=user.email if user else None, name=user.name if user else None)


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("auth.change_password"))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        password_reset.request_reset(form.identifier.data, form.method.data)
        if form.method.data == "sms":
            session["reset_identifier"] = form.identifier.data.strip()[:255]
            flash(ui("If an account with a phone number matches, we sent a 6-digit code by text message."), "ok")
            return redirect(url_for("auth.reset_with_code"))
        flash(ui("If an account matches, we sent a link to reset the password. It is valid for %(minutes)s minutes.", minutes=password_reset.LINK_MINUTES), "ok")
        return redirect(url_for("auth.login"))
    return render_template("admin/forgot_password.html", form=form, dev_mode=password_reset.is_dev())


@bp.route("/reset-code", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def reset_with_code():
    if current_user.is_authenticated:
        return redirect(url_for("auth.change_password"))
    form = ResetCodeForm()
    if request.method == "GET" and session.get("reset_identifier"):
        form.identifier.data = session["reset_identifier"]
    if form.validate_on_submit():
        problems = _password_problems(password_reset.find_user(form.identifier.data), form.new_password.data, form.confirm_password.data)
        if problems:
            form.new_password.errors.extend(ui(problem) for problem in problems)
        elif password_reset.reset_with_code(form.identifier.data, form.code.data, form.new_password.data):
            session.pop("reset_identifier", None)
            flash(ui("Your password was changed. Please sign in with the new password."), "ok")
            return redirect(url_for("auth.login"))
        else:
            flash(ui("The code is wrong or has expired. Request a new one."), "error")
    return render_template("admin/reset_code.html", form=form, dev_mode=password_reset.is_dev())


@bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def reset_with_link(token):
    if current_user.is_authenticated:
        return redirect(url_for("auth.change_password"))
    found = password_reset.valid_link(token)
    if found is None:
        return render_template("admin/reset_password.html", form=None, invalid=True), 400
    form = ResetPasswordForm()
    if form.validate_on_submit():
        problems = _password_problems(found[1], form.new_password.data, form.confirm_password.data)
        if problems:
            form.new_password.errors.extend(ui(problem) for problem in problems)
        elif password_reset.reset_with_link(token, form.new_password.data):
            flash(ui("Your password was changed. Please sign in with the new password."), "ok")
            return redirect(url_for("auth.login"))
        else:
            return render_template("admin/reset_password.html", form=None, invalid=True), 400
    return render_template("admin/reset_password.html", form=form, invalid=False)
