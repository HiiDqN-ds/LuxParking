"""Plan & billing (owner), subscriptions overview (platform owner) and the Stripe Billing webhook."""
from datetime import timedelta
from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from ..ui_i18n import ui
from ..extensions import csrf, db
from ..models import Role, Subscription, Tenant
from ..services import billing
from ..utils import audit

bp = Blueprint("billing", __name__)


def _owner_only(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_owner or current_user.role == Role.PLATFORM_OWNER:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def _platform_only(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role != Role.PLATFORM_OWNER:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@bp.get("/admin/billing")
@_owner_only
def overview():
    tenant = current_user.tenant
    state = billing.billing_state(tenant)
    db.session.commit()  # persists the lazily created trial row
    plans = [{"key": k, "label": billing.PLAN_LABELS[k], "limits": billing.plan_limits(k), "buyable": bool(billing.price_id(k)),
              "current": k == state.plan_key and state.status in ("active", "past_due")} for k in billing.PLAN_KEYS]
    return render_template("admin/billing.html", state=state, usage=billing.usage(tenant.id), plans=plans,
                           stripe_ready=billing.billing_configured(), sub=billing.get_subscription(tenant))


@bp.post("/admin/billing/subscribe/<plan_key>")
@_owner_only
def subscribe(plan_key: str):
    tenant = current_user.tenant
    try:
        url = billing.create_subscription_checkout(
            tenant, plan_key, email=current_user.email,
            success_url=url_for("billing.overview", _external=True, subscribed=1), cancel_url=url_for("billing.overview", _external=True))
    except billing.BillingError as exc:
        flash(ui(str(exc)), "error")
        return redirect(url_for("billing.overview"))
    audit("billing.checkout", "tenant", tenant.id, plan=plan_key)
    db.session.commit()
    return redirect(url, code=303)


@bp.post("/admin/billing/portal")
@_owner_only
def portal():
    try:
        return redirect(billing.create_portal_session(current_user.tenant, return_url=url_for("billing.overview", _external=True)), code=303)
    except billing.BillingError as exc:
        flash(ui(str(exc)), "error")
        return redirect(url_for("billing.overview"))


@bp.post("/webhooks/stripe-billing")
@csrf.exempt
def stripe_billing_webhook():
    try:
        event = billing.construct_billing_event(payload=request.get_data(), signature=request.headers.get("Stripe-Signature", ""))
    except billing.BillingError:
        return {"error": "invalid signature"}, 400
    try:
        result = billing.process_billing_event(event)
    except Exception:  # Stripe retries on 5xx; log so it can be investigated
        db.session.rollback()
        current_app.logger.exception("Billing webhook %s failed", event.get("type"))
        return {"error": "processing failed"}, 500
    return {"received": True, "result": result}


@bp.get("/admin/platform/subscriptions")
@_platform_only
def platform_subscriptions():
    rows = []
    for tenant in db.session.execute(select(Tenant).order_by(Tenant.name)).scalars():
        state = billing.billing_state(tenant)
        rows.append({"tenant": tenant, "state": state, "usage": billing.usage(tenant.id), "sub": billing.get_subscription(tenant)})
    db.session.commit()
    active = [r for r in rows if r["sub"] is not None and r["state"].status in ("active", "past_due") and r["sub"].amount_cents]
    mrr: dict[str, int] = {}
    for r in active:
        mrr[r["sub"].currency] = mrr.get(r["sub"].currency, 0) + r["sub"].amount_cents
    return render_template("admin/platform_subscriptions.html", rows=rows, mrr={c: v / 100 for c, v in mrr.items()},
                           counts={s: sum(1 for r in rows if r["state"].status == s) for s in ("exempt", "trialing", "active", "past_due", "cancelled", "expired")})


@bp.post("/admin/platform/subscriptions/<int:tenant_id>")
@_platform_only
def platform_subscription_update(tenant_id: int):
    tenant = db.get_or_404(Tenant, tenant_id)
    action = request.form.get("action", "")
    if action == "exempt":
        tenant.billing_exempt = True
    elif action == "charge":
        tenant.billing_exempt = False
    elif action == "extend_trial":
        sub = billing.get_subscription(tenant)
        if sub is None:
            flash(ui("This tenant is exempt from billing."), "error")
            return redirect(url_for("billing.platform_subscriptions"))
        try:
            days = max(1, min(90, int(request.form.get("days", "7"))))
        except ValueError:
            days = 7
        base = max(billing._now(), billing._aware(sub.trial_ends_at) or billing._now())
        sub.trial_ends_at = base + timedelta(days=days)
        if sub.status in ("expired",) and not sub.stripe_subscription_id:
            sub.status = "trialing"
    else:
        abort(400)
    audit(f"platform.billing.{action}", "tenant", tenant.id)
    db.session.commit()
    flash(ui("Billing updated."), "ok")
    return redirect(url_for("billing.platform_subscriptions"))
