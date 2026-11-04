"""Developer portal: guides, interactive-style API reference, webhooks, security. Public, German by default, no third-party scripts."""
import json

from flask import Blueprint, Response, abort, g, jsonify, render_template, request
from markupsafe import Markup

from .. import developer_docs as docs
from ..developer_samples import flow_samples, webhook_samples
from ..api_spec import OPENAPI_SPEC

bp = Blueprint("developers", __name__, url_prefix="/developers")

PAGES = ("index", "reference", "webhooks", "security", "changelog")


@bp.before_request
def _language():
    wanted = request.args.get("lang")
    g.dev_lang = wanted if wanted in ("de", "en") else (request.accept_languages.best_match(["de", "en"]) or "de")


@bp.context_processor
def _helpers():
    lang = g.dev_lang

    def tt(de: str, en: str) -> Markup:
        """German or English text. Only literal template text is passed in (never user data), so it may contain <code>."""
        return Markup(de if lang == "de" else en)

    return {"lang": lang, "locale": lang, "tt": tt, "base_url": request.url_root.rstrip("/"), "spec": OPENAPI_SPEC}


@bp.after_request
def _headers(response):
    response.headers.setdefault("Cache-Control", "public, max-age=300")
    response.headers["Vary"] = "Accept-Language"
    return response


@bp.get("/")
def index():
    return render_template("developers/index.html", page="index", flow=flow_samples(request.url_root.rstrip("/"), g.dev_lang))


@bp.get("/reference")
def reference():
    ops = docs.operations(OPENAPI_SPEC)
    groups: dict[str, list] = {}
    for item in ops:
        item["params"] = docs.parameters(OPENAPI_SPEC, item["op"])
        mime, media = docs.request_schema(OPENAPI_SPEC, item["op"])
        item["body"] = None if not media else {
            "mime": mime, "rows": docs.rows(OPENAPI_SPEC, media["schema"]), "type": docs.type_label(OPENAPI_SPEC, media["schema"]),
            "example": docs.json_pretty(docs.example(OPENAPI_SPEC, media["schema"])) if mime == "application/json" else None}
        item["responses"] = docs.responses(OPENAPI_SPEC, item["op"])
        item["curl"] = docs.curl_command(OPENAPI_SPEC, request.url_root.rstrip("/"), item)
        groups.setdefault(item["tag"], []).append(item)
    return render_template("developers/reference.html", page="reference", groups=groups,
                           schemas=OPENAPI_SPEC["components"]["schemas"], docs=docs, event_rows=docs.rows(OPENAPI_SPEC, {"$ref": "#/components/schemas/WebhookEvent"}))


@bp.get("/webhooks")
def webhooks():
    from ..services.api_platform import WEBHOOK_EVENTS
    delays = [min(3600, 30 * 2 ** min(n - 1, 7)) for n in range(1, 8)]
    return render_template("developers/webhooks.html", page="webhooks", events=sorted(WEBHOOK_EVENTS), delays=delays, hook=webhook_samples(g.dev_lang))


@bp.get("/security")
def security():
    return render_template("developers/security.html", page="security")


@bp.get("/changelog")
def changelog():
    return render_template("developers/changelog.html", page="changelog")


@bp.get("/openapi.json")
def openapi_download():
    return jsonify(OPENAPI_SPEC)


@bp.get("/postman.json")
def postman():
    body = json.dumps(docs.postman_collection(OPENAPI_SPEC), indent=2, ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={"Content-Disposition": 'attachment; filename="parkpilot-api.postman_collection.json"'})
