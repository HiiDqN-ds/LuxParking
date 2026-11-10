"""Public tenant-scoped parking search, map, and temporary reservations."""
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import session, Blueprint, Response, abort, current_app, flash, g, jsonify, make_response, redirect, render_template, request, url_for
from flask_socketio import join_room, leave_room
from sqlalchemy import select

from ..ui_i18n import ui
from ..extensions import db
from ..models import Extra, Hold, Lot, Space, Tenant, TenantPage
from ..i18n import (
    language_name,
    normalize_locale,
    supported_locales,
    translate,
    translation_keys,
)
from ..services.availability import (
    AvailabilityError,
    InvalidInterval,
    PricingUnavailable,
    lot_availability,
    parse_aware_datetime,
    quote_space,
    validate_interval,
)
from ..services.bookings import (
    BookingConflict,
    BookingNotFound,
    BookingValidationError,
    create_hold,
    release_hold,
)
from ..services import ai_chat
from ..services.stripe_payments import stripe_booking_payments_enabled
from ..services.live_updates import BOOKING_NAMESPACE, lot_room_name

from ..security import limiter  # noqa: E402

bp = Blueprint("public", __name__)
RTL_LOCALES = frozenset({"ar", "he", "fa", "ur"})
XML_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"


def _public_lot(tenant_slug: str, lot_slug: str) -> tuple[Tenant, Lot]:
    result = db.session.execute(
        select(Tenant, Lot)
        .join(Lot, Lot.tenant_id == Tenant.id)
        .where(
            Tenant.slug == tenant_slug,
            Tenant.status.in_(("active", "trial")),
            Lot.slug == lot_slug,
            Lot.active.is_(True),
        )
    ).one_or_none()
    if result is None:
        abort(404)
    g.public_tenant_id = result.Tenant.id
    g.tenant_id = result.Tenant.id
    return result


def _default_public_lot() -> tuple[Tenant, Lot] | None:
    base = (
        select(Tenant, Lot)
        .join(Lot, Lot.tenant_id == Tenant.id)
        .where(Tenant.status.in_(("active", "trial")), Lot.active.is_(True))
    )
    result = db.session.execute(
        base.where(Tenant.slug == current_app.config["PUBLIC_TENANT_SLUG"]).order_by(Lot.id)
    ).first()
    if result is None:
        # PUBLIC_TENANT_SLUG does not match any tenant (typo, renamed tenant, missing .env).
        # Fall back to the first bookable lot so the homepage always keeps its reserve button.
        result = db.session.execute(base.order_by(Tenant.id, Lot.id)).first()
    if result is not None:
        g.public_tenant_id = result.Tenant.id
        g.tenant_id = result.Tenant.id
    return result


def _locale(locale: str | None, tenant: Tenant | None = None) -> str:
    available = supported_locales()
    if locale is not None:
        candidate = locale.replace("_", "-").lower()
        if candidate not in available:
            abort(404)
        return candidate
    if tenant is not None and tenant.default_locale in available:
        return tenant.default_locale
    best = request.accept_languages.best_match(available)
    return normalize_locale(best or current_app.config["DEFAULT_LOCALE"])


def _absolute_url(path: str) -> str:
    site_url = current_app.config.get("SITE_URL")
    if site_url:
        return urljoin(site_url.rstrip("/") + "/", path.lstrip("/"))
    return urljoin(request.url_root, path.lstrip("/"))


def _localized_path(endpoint: str, locale: str, **values: object) -> str:
    return url_for(endpoint, locale=locale, **values)


def _seo_context(
    *,
    locale: str,
    endpoint: str,
    title: str,
    description: str,
    endpoint_values: dict | None = None,
    structured_data: dict | list | None = None,
    x_default_endpoint: str | None = None,
    x_default_values: dict | None = None,
    alternate_locales: tuple[str, ...] | None = None,
) -> dict:
    values = endpoint_values or {}
    canonical_path = _localized_path(endpoint, locale, **values)
    alternates = [
        {
            "language": alternate,
            "url": _absolute_url(_localized_path(endpoint, alternate, **values)),
        }
        for alternate in (
            supported_locales() if alternate_locales is None else alternate_locales
        )
    ]
    default_endpoint = x_default_endpoint or endpoint
    default_values = x_default_values if x_default_values is not None else values
    alternates.append(
        {
            "language": "x-default",
            "url": _absolute_url(url_for(default_endpoint, **default_values)),
        }
    )
    return {
        "canonical": _absolute_url(canonical_path),
        "alternates": alternates,
        "title": title,
        "description": description,
        "locale": locale,
        "image": _absolute_url(
            url_for("static", filename="img/luxury-parking-stacked-dark.png")
        ),
        "og_locale": {
            "de": "de_DE",
            "en": "en_US",
            "nl": "nl_NL",
            "fr": "fr_FR",
            "es": "es_ES",
            "ar": "ar",
            "tr": "tr_TR",
        }.get(locale, locale.replace("-", "_")),
        "structured_data": structured_data,
        "robots": None,
    }


def _language_links(
    endpoint: str,
    *,
    locale: str,
    endpoint_values: dict | None = None,
    locales: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    values = endpoint_values or {}
    return [
        {
            "code": language,
            "label": language_name(language),
            "url": _localized_path(endpoint, language, **values),
        }
        for language in (supported_locales() if locales is None else locales)
    ]


def _faq_schema(locale: str) -> dict:
    tenant_id = g.get("public_tenant_id")
    if tenant_id is not None:
        entries = db.session.execute(
            select(TenantPage.title, TenantPage.body)
            .where(
                TenantPage.tenant_id == tenant_id,
                TenantPage.kind == "faq",
                TenantPage.locale == locale,
                TenantPage.published.is_(True),
            )
            .order_by(TenantPage.published_at.desc(), TenantPage.id.desc())
            .limit(20)
        ).all()
        if entries:
            return {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": title,
                        "acceptedAnswer": {"@type": "Answer", "text": body},
                    }
                    for title, body in entries
                ],
            }
    pairs = [(translate(locale, f"lp.faq.q{n}"), translate(locale, f"lp.faq.a{n}")) for n in range(1, 7)]
    pairs.append((translate(locale, "jsonld.faq.question"), translate(locale, "jsonld.faq.answer")))
    return {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}
            for question, answer in pairs
        ],
    }


def _public_error_key(error: Exception) -> str:
    if isinstance(error, PricingUnavailable):
        return "error.pricing"
    if isinstance(error, InvalidInterval):
        return "error.dates"
    if isinstance(error, BookingConflict):
        return "error.conflict"
    if isinstance(error, BookingNotFound):
        return "error.space"
    return "error.input"


def _lot_schema(tenant: Tenant, lot: Lot, locale: str, spaces: list[dict] | None = None) -> dict:
    business: dict = {
        "@type": ["LocalBusiness", "ParkingFacility"],
        "@id": _absolute_url(
            _localized_path(
                "public.localized_reserve",
                locale,
                tenant_slug=tenant.slug,
                lot_slug=lot.slug,
            )
        )
        + "#parking-facility",
        "name": lot.name,
        "url": _absolute_url(
            _localized_path(
                "public.localized_reserve",
                locale,
                tenant_slug=tenant.slug,
                lot_slug=lot.slug,
            )
        ),
        "description": translate(locale, "booking.description", lot=lot.name),
        "priceRange": f"{lot.base_daily_rate} {lot.currency}" if lot.base_daily_rate else "",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": lot.address,
            "addressLocality": lot.city,
        },
    }
    if lot.lat is not None and lot.lng is not None:
        business["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": lot.lat,
            "longitude": lot.lng,
        }
    offers = []
    for space in spaces or []:
        price = space.get("price")
        if price is not None:
            offers.append(
                {
                    "@type": "Offer",
                    "name": translate(locale, "booking.space", name=space["name"]),
                    "price": price,
                    "priceCurrency": space["currency"],
                    "availability": "https://schema.org/InStock"
                    if space["selectable"]
                    else "https://schema.org/SoldOut",
                    "url": business["url"],
                }
            )
    if offers:
        business["offers"] = offers
    breadcrumbs = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "ParkPilot",
                "item": _absolute_url(url_for("public.localized_home", locale=locale)),
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": lot.name,
                "item": business["url"],
            },
        ],
    }
    return {"@context": "https://schema.org", "@graph": [business, breadcrumbs, _faq_schema(locale)]}


def _organization_schema(locale: str, tenant: Tenant | None) -> dict:
    """Organization entry for the homepage; uses the public business profile when the bundled brand is active."""
    from ..business import business_profile

    biz = business_profile()
    url = _absolute_url(url_for("public.localized_home", locale=locale))
    if not biz["logo_enabled"]:
        return {"@type": "Organization", "name": tenant.name if tenant else "ParkPilot", "url": url}
    return {
        "@type": "Organization",
        "name": biz["name"],
        "legalName": biz["legal_name"],
        "url": url,
        "logo": _absolute_url(url_for("static", filename="img/luxury-parking-mark.png")),
        "email": biz["email"],
        "telephone": biz["phone"],
        "address": {
            "@type": "PostalAddress",
            "streetAddress": biz["street"],
            "postalCode": biz["postal_code"],
            "addressLocality": biz["city"],
            "addressCountry": biz["country"],
        },
        "sameAs": biz["same_as"],
    }


def _lot_gallery_schema(locale: str) -> dict:
    """ImageGallery of the parking site photos (alt text and captions come from the translations)."""
    images = []
    for index, key in enumerate(("courtyard", "main-lot", "walled-bays", "arrival-bays"), start=1):
        images.append(
            {
                "@type": "ImageObject",
                "contentUrl": _absolute_url(url_for("static", filename=f"img/lot/{key}-1200.webp")),
                "name": translate(locale, f"tour.s{index}.title"),
                "caption": translate(locale, f"tour.s{index}.cap"),
                "description": translate(locale, f"tour.s{index}.alt"),
            }
        )
    return {"@type": "ImageGallery", "name": translate(locale, "tour.title"), "image": images}

def _render_home(locale: str):
    public_lot = _default_public_lot()
    tenant, lot = public_lot if public_lot else (None, None)
    title = translate(locale, "home.title.with_lot", lot=lot.name) if lot else translate(
        locale, "home.title.generic"
    )
    description = translate(
        locale,
        "home.description.with_lot" if lot else "home.description.generic",
        **({"lot": lot.name} if lot else {}),
    )
    graph: list[dict] = [
        _organization_schema(locale, tenant),
        _faq_schema(locale),
    ]
    if tenant is not None and lot is not None:
        graph.insert(0, _lot_schema(tenant, lot, locale))
    graph.append(_lot_gallery_schema(locale))
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_home",
        title=title,
        description=description,
        structured_data={"@context": "https://schema.org", "@graph": graph},
        x_default_endpoint="public.home",
    )
    return render_template(
        "home.html",
        locale=locale,
        tenant=tenant,
        lot=lot,
        seo=seo,
        language_links=_language_links("public.localized_home", locale=locale),
    )


@bp.get("/")
def home():
    return _render_home(_locale(None))


@bp.get("/<locale>/")
def localized_home(locale: str):
    return _render_home(_locale(locale))


def _search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _public_search_entries(locale: str, tenant: Tenant | None, lot: Lot | None) -> list[dict[str, str]]:
    home_url = url_for("public.localized_home", locale=locale)
    entries: list[dict[str, str]] = []

    def add(title: str, summary: str, url: str, keywords: str = "") -> None:
        entries.append({
            "title": title,
            "summary": summary,
            "url": url,
            "search_text": _search_text(f"{title} {summary} {keywords}"),
        })

    if lot is not None and tenant is not None:
        add(
            translate(locale, "home.book"),
            translate(locale, "booking.description", lot=lot.name),
            url_for("public.localized_reserve", locale=locale, tenant_slug=tenant.slug, lot_slug=lot.slug),
            "booking reservation parking availability price",
        )

    add(translate(locale, "lp.about.title"), translate(locale, "lp.about.text"), home_url + "#about-title")
    add(
        translate(locale, "lp.features.title"),
        " ".join(
            f"{translate(locale, f'lp.f{index}.t')} {translate(locale, f'lp.f{index}.d')}"
            for index in range(1, 7)
        ),
        home_url + "#vorteile",
    )
    add(
        translate(locale, "lp.process.title"),
        " ".join(
            f"{translate(locale, f'lp.s{index}.t')} {translate(locale, f'lp.s{index}.d')}"
            for index in range(1, 5)
        ),
        home_url + "#ablauf",
    )
    add(
        translate(locale, "lp.loc.title"),
        f"{translate(locale, 'lp.loc.address')} {translate(locale, 'lp.loc.phone')} "
        f"{translate(locale, 'lp.loc.email')} {translate(locale, 'lp.loc.route')} "
        f"{translate(locale, 'lp.loc.from')} {translate(locale, 'lp.loc.to')}",
        home_url + "#anfahrt",
        "contact address directions airport",
    )
    for index in range(1, 7):
        add(
            translate(locale, f"lp.faq.q{index}"),
            translate(locale, f"lp.faq.a{index}"),
            home_url + "#faq",
        )
    add(
        translate(locale, "faq.hold.question"),
        translate(locale, "faq.hold.answer"),
        home_url + "#faq",
    )
    for key, endpoint, keywords in (
        ("legal.terms", "legal.agb", "AGB terms conditions Allgemeine Geschäftsbedingungen"),
        ("legal.privacy", "legal.datenschutz", "Datenschutz privacy data protection"),
        ("legal.imprint", "legal.impressum", "Impressum Anbieter contact"),
    ):
        add(translate(locale, key), translate(locale, "search.legal_summary"), url_for(endpoint), keywords)

    if tenant is not None:
        for kind in ("faq", "blog"):
            add(
                translate(locale, f"content.{kind}"),
                translate(locale, f"content.{kind}.description", tenant=tenant.name),
                url_for("public.localized_content_index", locale=locale, tenant_slug=tenant.slug, kind=kind),
                "questions answers articles news",
            )
        pages = db.session.execute(
            select(TenantPage)
            .where(
                TenantPage.tenant_id == tenant.id,
                TenantPage.locale == locale,
                TenantPage.published.is_(True),
            )
            .order_by(TenantPage.published_at.desc(), TenantPage.id.desc())
            .limit(100)
        ).scalars()
        for page in pages:
            add(
                page.title,
                page.summary or page.body[:320],
                url_for(
                    "public.localized_content_page",
                    locale=locale,
                    tenant_slug=tenant.slug,
                    kind=page.kind,
                    slug=page.slug,
                ),
                page.body,
            )
    return entries


@bp.get("/<locale>/search")
def localized_search(locale: str):
    locale = _locale(locale)
    query = request.args.get("q", "").strip()[:100]
    terms = [term for term in re.findall(r"\w+", _search_text(query)) if term]
    result_entries: list[dict[str, str]] = []
    if terms:
        public = _default_public_lot()
        tenant, lot = public if public else (None, None)
        matches = [
            entry for entry in _public_search_entries(locale, tenant, lot)
            if all(term in entry["search_text"] for term in terms)
        ]
        result_entries = matches[:30]

    title = translate(locale, "search.title")
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_search",
        title=title,
        description=translate(locale, "search.description"),
        x_default_endpoint="public.localized_home",
        x_default_values={"locale": locale},
    )
    seo["robots"] = "noindex,follow"
    return render_template(
        "public/search.html",
        locale=locale,
        query=query,
        results=result_entries,
        seo=seo,
        language_links=_language_links("public.localized_home", locale=locale),
    )


@bp.get("/content/<tenant_slug>/<kind>", defaults={"locale": None}, endpoint="content_index")
@bp.get("/<locale>/content/<tenant_slug>/<kind>", endpoint="localized_content_index")
def content_index(locale: str | None, tenant_slug: str, kind: str):
    if kind not in {"blog", "faq"}:
        abort(404)
    tenant = db.session.execute(
        select(Tenant).where(
            Tenant.slug == tenant_slug,
            Tenant.status.in_(("active", "trial")),
        )
    ).scalar_one_or_none()
    if tenant is None:
        abort(404)
    g.public_tenant_id = tenant.id
    g.tenant_id = tenant.id
    requested_locale = locale
    locale = _locale(locale, tenant)
    available_locales = list(
        db.session.execute(
            select(TenantPage.locale)
            .where(
                TenantPage.tenant_id == tenant.id,
                TenantPage.kind == kind,
                TenantPage.published.is_(True),
            )
            .distinct()
            .order_by(TenantPage.locale)
        ).scalars().all()
    )
    entries = db.session.execute(
        select(TenantPage)
        .where(
            TenantPage.tenant_id == tenant.id,
            TenantPage.kind == kind,
            TenantPage.locale == locale,
            TenantPage.published.is_(True),
        )
        .order_by(TenantPage.published_at.desc(), TenantPage.id.desc())
    ).scalars().all()
    if requested_locale is None and not entries:
        fallback_locale = db.session.execute(
            select(TenantPage.locale)
            .where(
                TenantPage.tenant_id == tenant.id,
                TenantPage.kind == kind,
                TenantPage.published.is_(True),
            )
            .order_by(TenantPage.locale)
            .limit(1)
        ).scalar_one_or_none()
        if fallback_locale:
            locale = fallback_locale
            available_locales = list(
                db.session.execute(
                    select(TenantPage.locale)
                    .where(
                        TenantPage.tenant_id == tenant.id,
                        TenantPage.kind == kind,
                        TenantPage.published.is_(True),
                    )
                    .distinct()
                    .order_by(TenantPage.locale)
                ).scalars().all()
            )
            entries = db.session.execute(
                select(TenantPage)
                .where(
                    TenantPage.tenant_id == tenant.id,
                    TenantPage.kind == kind,
                    TenantPage.locale == locale,
                    TenantPage.published.is_(True),
                )
                .order_by(TenantPage.published_at.desc(), TenantPage.id.desc())
            ).scalars().all()
    endpoint_values = {"tenant_slug": tenant.slug, "kind": kind}
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_content_index",
        title=translate(locale, f"content.{kind}.title", tenant=tenant.name),
        description=translate(locale, f"content.{kind}.description", tenant=tenant.name),
        endpoint_values=endpoint_values,
        x_default_endpoint="public.content_index",
        x_default_values=endpoint_values,
        alternate_locales=tuple(available_locales),
        structured_data=(
            {
                "@context": "https://schema.org",
                **_faq_schema(locale),
            }
            if kind == "faq" and entries
            else None
        ),
    )
    if not entries:
        seo["robots"] = "noindex,follow"
    lot = db.session.execute(
        select(Lot)
        .where(Lot.tenant_id == tenant.id, Lot.active.is_(True))
        .order_by(Lot.id)
        .limit(1)
    ).scalar_one_or_none()
    return render_template(
        "public/content_index.html",
        locale=locale,
        tenant=tenant,
        kind=kind,
        entries=entries,
        lot=lot,
        seo=seo,
        language_links=_language_links(
            "public.localized_content_index",
            locale=locale,
            endpoint_values=endpoint_values,
            locales=tuple(available_locales),
        ) if available_locales else [],
    )


@bp.get(
    "/content/<tenant_slug>/<kind>/<slug>",
    defaults={"locale": None},
    endpoint="content_page",
)
@bp.get(
    "/<locale>/content/<tenant_slug>/<kind>/<slug>",
    endpoint="localized_content_page",
)
def content_page(locale: str | None, tenant_slug: str, kind: str, slug: str):
    if kind not in {"blog", "faq"}:
        abort(404)
    tenant = db.session.execute(
        select(Tenant).where(
            Tenant.slug == tenant_slug,
            Tenant.status.in_(("active", "trial")),
        )
    ).scalar_one_or_none()
    if tenant is None:
        abort(404)
    g.public_tenant_id = tenant.id
    g.tenant_id = tenant.id
    requested_locale = locale
    locale = _locale(locale, tenant)
    page = db.session.execute(
        select(TenantPage).where(
            TenantPage.tenant_id == tenant.id,
            TenantPage.kind == kind,
            TenantPage.locale == locale,
            TenantPage.slug == slug,
            TenantPage.published.is_(True),
        )
    ).scalar_one_or_none()
    if page is None and requested_locale is None:
        page = db.session.execute(
            select(TenantPage)
            .where(
                TenantPage.tenant_id == tenant.id,
                TenantPage.kind == kind,
                TenantPage.slug == slug,
                TenantPage.published.is_(True),
            )
            .order_by((TenantPage.locale == tenant.default_locale).desc(), TenantPage.locale)
        ).scalars().first()
        if page is not None:
            locale = page.locale
    if page is None:
        abort(404)
    translated_locales = tuple(
        db.session.execute(
            select(TenantPage.locale).where(
                TenantPage.tenant_id == tenant.id,
                TenantPage.kind == kind,
                TenantPage.slug == slug,
                TenantPage.published.is_(True),
            )
        ).scalars().all()
    )
    endpoint_values = {"tenant_slug": tenant.slug, "kind": kind, "slug": slug}
    translated_locales = tuple(dict.fromkeys(translated_locales))
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_content_page",
        title=page.seo_title or page.title,
        description=page.seo_description or page.summary or page.title,
        endpoint_values=endpoint_values,
        x_default_endpoint="public.content_page",
        x_default_values=endpoint_values,
        alternate_locales=translated_locales or (locale,),
        structured_data=(
            {
                "@context": "https://schema.org",
                "@type": "Question",
                "name": page.title,
                "acceptedAnswer": {"@type": "Answer", "text": page.body},
            }
            if kind == "faq"
            else None
        ),
    )
    lot = db.session.execute(
        select(Lot)
        .where(Lot.tenant_id == tenant.id, Lot.active.is_(True))
        .order_by(Lot.id)
        .limit(1)
    ).scalar_one_or_none()
    return render_template(
        "public/content_page.html",
        locale=locale,
        tenant=tenant,
        page=page,
        lot=lot,
        seo=seo,
        language_links=[
            {
                "code": translated_locale,
                "label": language_name(translated_locale),
                "url": _localized_path(
                    "public.localized_content_page",
                    translated_locale,
                    **endpoint_values,
                ),
            }
            for translated_locale in translated_locales
        ],
    )


@bp.post("/chat/<tenant_slug>/<lot_slug>")
@limiter.limit("8 per minute; 80 per hour")
def chat(tenant_slug: str, lot_slug: str):
    """Chat assistant of the public pages. JSON in: {"messages": [{"role", "content"}], "locale"}; JSON out: {"reply", "mode"}."""
    if not current_app.config.get("AI_CHAT_ENABLED", True):
        abort(404)
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid_request"), 400
    try:
        locale = _locale(str(payload.get("locale") or "")[:10] or None, tenant)
    except Exception:  # unknown language code: fall back to the parking's default
        locale = _locale(None, tenant)
    messages = ai_chat.clean_history(payload.get("messages"))
    if not messages:
        return jsonify(error="empty_message"), 400
    reply = ai_chat.ai_reply(tenant, lot, locale, messages)
    mode = "ai"
    if reply is None:
        reply, mode = ai_chat.basic_reply(tenant, lot, locale), "basic"
    return jsonify(reply=reply, mode=mode)


@bp.get("/robots.txt")
def robots():
    site_root = current_app.config.get("SITE_URL", "").rstrip("/") or request.url_root.rstrip("/")
    response = make_response(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /staff/\n"
        "Disallow: /reserve/*/holds/\n"
        "Disallow: /reserve/*/availability\n"
        "Disallow: /*/reserve/*/holds/\n"
        "Disallow: /*/reserve/*/availability\n"
        f"Sitemap: {site_root}/sitemap.xml\n"
    )
    response.mimetype = "text/plain"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@bp.get("/sitemap.xml")
def sitemap():
    root = Element(f"{{{XML_NAMESPACE}}}urlset")
    root.set("xmlns:xhtml", XHTML_NAMESPACE)
    locale_list = supported_locales()
    localized_pages: list[tuple[str, str, dict, datetime | None]] = []
    for locale in locale_list:
        localized_pages.append(
            (
                "public.localized_home",
                _localized_path("public.localized_home", locale),
                {"locale": locale},
                None,
            )
        )
    lots = db.session.execute(
        select(Tenant.slug, Lot.slug)
        .join(Lot, Lot.tenant_id == Tenant.id)
        .where(
            Tenant.status.in_(("active", "trial")),
            Lot.active.is_(True),
        )
        .order_by(Tenant.slug, Lot.slug)
    ).all()
    for tenant_slug, lot_slug in lots:
        for locale in locale_list:
            values = {
                "locale": locale,
                "tenant_slug": tenant_slug,
                "lot_slug": lot_slug,
            }
            localized_pages.append(
                (
                    "public.localized_reserve",
                    _localized_path("public.localized_reserve", **values),
                    values,
                    None,
                )
            )
    content_pages = db.session.execute(
        select(TenantPage, Tenant.slug)
        .join(Tenant, Tenant.id == TenantPage.tenant_id)
        .where(
            Tenant.status.in_(("active", "trial")),
            TenantPage.published.is_(True),
        )
        .order_by(TenantPage.tenant_id, TenantPage.kind, TenantPage.slug, TenantPage.locale)
    ).all()
    content_indexes = {
        (tenant_slug, page.kind, page.locale)
        for page, tenant_slug in content_pages
    }
    content_index_lastmod: dict[tuple[str, str, str], datetime] = {}
    for page, tenant_slug in content_pages:
        key = (tenant_slug, page.kind, page.locale)
        changed_at = page.updated_at or page.published_at
        if changed_at is not None and (
            key not in content_index_lastmod or changed_at > content_index_lastmod[key]
        ):
            content_index_lastmod[key] = changed_at
    for tenant_slug, kind, locale in sorted(content_indexes):
        values = {"locale": locale, "tenant_slug": tenant_slug, "kind": kind}
        localized_pages.append(
            (
                "public.localized_content_index",
                _localized_path("public.localized_content_index", **values),
                values,
                content_index_lastmod.get((tenant_slug, kind, locale)),
            )
        )
    for page, tenant_slug in content_pages:
        values = {
            "locale": page.locale,
            "tenant_slug": tenant_slug,
            "kind": page.kind,
            "slug": page.slug,
        }
        localized_pages.append(
            (
                "public.localized_content_page",
                _localized_path("public.localized_content_page", **values),
                values,
                page.updated_at or page.published_at,
            )
        )
    for endpoint, path, values, lastmod in localized_pages:
        url_element = SubElement(root, f"{{{XML_NAMESPACE}}}url")
        SubElement(url_element, f"{{{XML_NAMESPACE}}}loc").text = _absolute_url(path)
        if lastmod is not None:
            SubElement(url_element, f"{{{XML_NAMESPACE}}}lastmod").text = lastmod.isoformat()
        if endpoint == "public.localized_content_page":
            page_locales = [
                page.locale
                for page, tenant_slug in content_pages
                if tenant_slug == values["tenant_slug"]
                and page.kind == values["kind"]
                and page.slug == values["slug"]
            ]
        else:
            page_locales = list(locale_list)
        for alternate in page_locales:
            alternate_values = dict(values)
            alternate_values.pop("locale", None)
            alternate_path = _localized_path(endpoint, alternate, **alternate_values)
            alternate_element = SubElement(url_element, f"{{{XHTML_NAMESPACE}}}link")
            alternate_element.set("rel", "alternate")
            alternate_element.set("hreflang", alternate)
            alternate_element.set("href", _absolute_url(alternate_path))
        default_endpoint = {
            "public.localized_home": "public.home",
            "public.localized_content_index": "public.content_index",
            "public.localized_content_page": "public.content_page",
        }.get(endpoint, "public.reserve")
        default_values = dict(values)
        default_values.pop("locale", None)
        default_path = url_for(default_endpoint, **default_values)
        alternate_element = SubElement(url_element, f"{{{XHTML_NAMESPACE}}}link")
        alternate_element.set("rel", "alternate")
        alternate_element.set("hreflang", "x-default")
        alternate_element.set("href", _absolute_url(default_path))
    xml = tostring(root, encoding="utf-8", xml_declaration=True)
    response = make_response(xml)
    response.mimetype = "application/xml"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def _take_draft(hold_id: int) -> dict:
    """What the customer typed before a refused booking (stored by checkout._back_to_hold), shown once."""
    stored = session.pop("checkout_draft", None)
    return stored["fields"] if isinstance(stored, dict) and stored.get("hold_id") == hold_id else {}


SHOWN_SPACE_STATES = ("available", "optional")  # what a customer can book; everything else is hidden


@bp.get("/reserve/<tenant_slug>/<lot_slug>", defaults={"locale": None})
def reserve(tenant_slug: str, lot_slug: str, locale: str | None):
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    locale = _locale(locale or "en", tenant)
    pickup = datetime.now(timezone.utc) + timedelta(hours=2)
    pickup = pickup.replace(second=0, microsecond=0)
    returned = pickup + timedelta(hours=24)
    states = lot_availability(
        tenant_id=tenant.id,
        lot_id=lot.id,
        start=pickup,
        end=returned,
    )
    localized_endpoint_values = {"tenant_slug": tenant.slug, "lot_slug": lot.slug}
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_reserve",
        title=translate(locale, "booking.title", lot=lot.name),
        description=translate(locale, "booking.description", lot=lot.name),
        endpoint_values=localized_endpoint_values,
        structured_data=_lot_schema(tenant, lot, locale, states),
        x_default_endpoint="public.reserve",
        x_default_values=localized_endpoint_values,
    )
    return render_template(
        "public/reserve.html",
        locale=locale,
        tenant=tenant,
        lot=lot,
        # Reserved, held-soon and blocked spaces are not shown to customers at all, only the ones they can book.
        spaces=[space for space in states if space["status"] in SHOWN_SPACE_STATES],
        has_spaces=bool(states),
        pickup_utc=pickup.isoformat(),
        return_utc=returned.isoformat(),
        seo=seo,
        language_links=_language_links(
            "public.localized_reserve",
            locale=locale,
            endpoint_values=localized_endpoint_values,
        ),
        booking_messages={
            key.removeprefix("booking."): translate(locale, key)
            for key in translation_keys(locale, "booking.")
        },
    )


@bp.get("/<locale>/reserve/<tenant_slug>/<lot_slug>", endpoint="localized_reserve")
def localized_reserve(locale: str, tenant_slug: str, lot_slug: str):
    return reserve(tenant_slug, lot_slug, locale)


@bp.get("/reserve/<tenant_slug>/<lot_slug>/availability", defaults={"locale": None})
@bp.get(
    "/<locale>/reserve/<tenant_slug>/<lot_slug>/availability",
    endpoint="localized_availability",
)
@limiter.limit("120 per minute")
def availability(tenant_slug: str, lot_slug: str, locale: str | None):
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    requested_locale = locale
    locale = _locale(locale, tenant)
    try:
        start = parse_aware_datetime(request.args.get("pickup_at", ""), "pickup_at")
        end = parse_aware_datetime(request.args.get("return_at", ""), "return_at")
        start, end = validate_interval(start, end)
        spaces = lot_availability(
            tenant_id=tenant.id,
            lot_id=lot.id,
            start=start,
            end=end,
        )
    except InvalidInterval as exc:
        if requested_locale is None:
            return jsonify(error=str(exc)), 400
        return jsonify(error=translate(locale, "error.dates")), 400
    except AvailabilityError:
        abort(404)
    return jsonify(spaces=spaces)


@bp.post("/reserve/<tenant_slug>/<lot_slug>/holds", defaults={"locale": None})
@bp.post(
    "/<locale>/reserve/<tenant_slug>/<lot_slug>/holds",
    endpoint="localized_reserve_space",
)
@limiter.limit("20 per minute; 200 per hour")
def reserve_space(tenant_slug: str, lot_slug: str, locale: str | None):
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    legacy_route = locale is None
    locale = _locale(locale or "en", tenant)
    try:
        space_id = int(request.form.get("space_id", ""))
        start = parse_aware_datetime(request.form.get("pickup_at", ""), "pickup_at")
        end = parse_aware_datetime(request.form.get("return_at", ""), "return_at")
        start, end = validate_interval(start, end)
        space = db.session.execute(
            select(Space).where(
                Space.id == space_id,
                Space.lot_id == lot.id,
                Space.tenant_id == tenant.id,
            )
        ).scalar_one_or_none()
        if space is None:
            raise BookingNotFound("Parking space not found.")
        quote_space(space, lot, start, end)
        hold = create_hold(
            tenant_id=tenant.id,
            lot_id=lot.id,
            space_id=space_id,
            pickup_at=start,
            return_at=end,
            customer_email=request.form.get("email", ""),
        )
    except ValueError:
        flash(
            ui("Invalid parking space."
            if legacy_route
            else translate(locale, "error.space")),
            "error",
        )
        return redirect(
            url_for("public.localized_reserve", locale=locale, tenant_slug=tenant.slug, lot_slug=lot.slug)
        )
    except (BookingConflict, BookingNotFound, BookingValidationError, InvalidInterval, PricingUnavailable) as exc:
        flash(ui(str(exc) if legacy_route else translate(locale, _public_error_key(exc))), "error")
        return redirect(
            url_for("public.localized_reserve", locale=locale, tenant_slug=tenant.slug, lot_slug=lot.slug)
        )

    return redirect(
        url_for(
            "public.localized_hold_confirmation",
            locale=locale,
            tenant_slug=tenant.slug,
            lot_slug=lot.slug,
            hold_id=hold.id,
        )
    )


@bp.get("/reserve/<tenant_slug>/<lot_slug>/holds/<int:hold_id>", defaults={"locale": None})
def hold_confirmation(tenant_slug: str, lot_slug: str, hold_id: int, locale: str | None):
    tenant, lot = _public_lot(tenant_slug, lot_slug)
    locale = _locale(locale, tenant)
    hold = db.session.execute(
        select(Hold).where(
            Hold.id == hold_id,
            Hold.tenant_id == tenant.id,
            Hold.lot_id == lot.id,
        )
    ).scalar_one_or_none()
    if hold is None:
        abort(404)
    expires_at = hold.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if hold.status != "active" or expires_at <= datetime.now(timezone.utc):
        if hold.status == "active":
            release_hold(tenant_id=tenant.id, hold_id=hold.id)
        expired_seo = _seo_context(
            locale=locale,
            endpoint="public.localized_reserve",
            title=translate(locale, "hold.expired.title"),
            description=translate(locale, "hold.expired.description"),
            endpoint_values={"tenant_slug": tenant.slug, "lot_slug": lot.slug},
            x_default_endpoint="public.reserve",
            x_default_values={"tenant_slug": tenant.slug, "lot_slug": lot.slug},
        )
        expired_seo["robots"] = "noindex,nofollow"
        expired_seo["structured_data"] = None
        return render_template(
            "public/hold_expired.html",
            locale=locale,
            tenant=tenant,
            lot=lot,
            seo=expired_seo,
            language_links=_language_links(
                "public.localized_reserve",
                locale=locale,
                endpoint_values={"tenant_slug": tenant.slug, "lot_slug": lot.slug},
            ),
        ), 410

    space = db.session.execute(
        select(Space).where(
            Space.id == hold.space_id,
            Space.lot_id == lot.id,
            Space.tenant_id == tenant.id,
        )
    ).scalar_one_or_none()
    if space is None:
        abort(404)
    try:
        pickup_at = hold.pickup_at
        return_at = hold.return_at
        if pickup_at.tzinfo is None:
            pickup_at = pickup_at.replace(tzinfo=timezone.utc)
        if return_at.tzinfo is None:
            return_at = return_at.replace(tzinfo=timezone.utc)
        price, currency = quote_space(space, lot, pickup_at, return_at)
    except (InvalidInterval, AvailabilityError):
        price, currency = None, lot.currency
    localized_endpoint_values = {"tenant_slug": tenant.slug, "lot_slug": lot.slug}
    try:
        tenant_zone = ZoneInfo(tenant.timezone)
    except ZoneInfoNotFoundError:
        tenant_zone = timezone.utc
    pickup_local = (pickup_at.replace(tzinfo=timezone.utc) if pickup_at.tzinfo is None else pickup_at).astimezone(tenant_zone)
    return_local = (return_at.replace(tzinfo=timezone.utc) if return_at.tzinfo is None else return_at).astimezone(tenant_zone)
    seo = _seo_context(
        locale=locale,
        endpoint="public.localized_hold_confirmation",
        title=translate(locale, "hold.title"),
        description=translate(locale, "hold.description", space=space.name, lot=lot.name),
        endpoint_values={
            **localized_endpoint_values,
            "hold_id": hold.id,
        },
        structured_data=_lot_schema(tenant, lot, locale),
        x_default_endpoint="public.reserve",
        x_default_values=localized_endpoint_values,
    )
    seo["robots"] = "noindex,nofollow"
    seo["structured_data"] = None
    return render_template(
        "public/hold_confirmation.html",
        draft=_take_draft(hold.id),
        locale=locale,
        tenant=tenant,
        lot=lot,
        hold=hold,
        space=space,
        price=price,
        currency=currency,
        expires_at=expires_at,
        pickup_local=pickup_local,
        return_local=return_local,
        timezone_name=tenant.timezone,
        stripe_enabled=stripe_booking_payments_enabled(),
        extras=db.session.execute(select(Extra).where(Extra.tenant_id == tenant.id, Extra.lot_id == lot.id, Extra.active.is_(True)).order_by(Extra.kind, Extra.name)).scalars().all(),
        seo=seo,
        language_links=_language_links(
            "public.localized_reserve",
            locale=locale,
            endpoint_values=localized_endpoint_values,
        ),
    )


@bp.get(
    "/<locale>/reserve/<tenant_slug>/<lot_slug>/holds/<int:hold_id>",
    endpoint="localized_hold_confirmation",
)
def localized_hold_confirmation(locale: str, tenant_slug: str, lot_slug: str, hold_id: int):
    return hold_confirmation(tenant_slug, lot_slug, hold_id, locale)


def join_lot(data):
    if not isinstance(data, dict):
        return {"error": "Invalid lot selection."}
    tenant_slug = data.get("tenant_slug")
    lot_slug = data.get("lot_slug")
    if not isinstance(tenant_slug, str) or not isinstance(lot_slug, str):
        return {"error": "Invalid lot selection."}
    result = db.session.execute(
        select(Tenant.id, Lot.id)
        .join(Lot, Lot.tenant_id == Tenant.id)
        .where(
            Tenant.slug == tenant_slug,
            Tenant.status.in_(("active", "trial")),
            Lot.slug == lot_slug,
            Lot.active.is_(True),
        )
    ).one_or_none()
    if result is None:
        return {"error": "Parking lot not found."}
    join_room(lot_room_name(result[0], result[1]))
    return {"joined": True}


def leave_lot(data):
    if not isinstance(data, dict):
        return
    tenant_slug = data.get("tenant_slug")
    lot_slug = data.get("lot_slug")
    if not isinstance(tenant_slug, str) or not isinstance(lot_slug, str):
        return
    result = db.session.execute(
        select(Tenant.id, Lot.id)
        .join(Lot, Lot.tenant_id == Tenant.id)
        .where(Tenant.slug == tenant_slug, Lot.slug == lot_slug)
    ).one_or_none()
    if result is not None:
        leave_room(lot_room_name(result[0], result[1]))
