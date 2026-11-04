"""Finds every English text of the back office / staff app that needs a translation.

    flask --app wsgi i18n-missing          # which texts are missing per language
    flask --app wsgi i18n-missing --write  # add the missing ones (as English) to translations/ui/<locale>.json for translating

The same function backs tests/test_ui_translations.py, which fails when any language lacks a text.
Sources: _('...') in templates, ui('...') in Python, exception messages, form labels and label dictionaries, standard
validation messages and the status / kind names that templates print from the database.
"""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
UI_DIR = ROOT / "translations" / "ui"
SKIP_TEMPLATE_DIRS = ("public", "legal")  # translated with translations/<locale>.json (public) or legal text (German only)

STANDARD = [
    "This field is required.", "Invalid email address.", "Invalid URL.", "Not a valid choice.", "Not a valid integer value.",
    "Not a valid float value.", "Not a valid decimal value.", "Not a valid date value.", "Not a valid datetime value.",
    "Field must be between %(min)s and %(max)s characters long.", "Field must be at least %(min)s characters long.",
    "Field must be at least %(min)s character long.", "Field cannot be longer than %(max)s characters.",
    "Field cannot be longer than %(max)s character.", "Number must be between %(min)s and %(max)s.",
    "Number must be at least %(min)s.", "Number must be at most %(max)s.", "Field must be equal to %(other_name)s.",
    "The CSRF token is missing.", "The CSRF session token is missing.", "The CSRF tokens do not match.", "The CSRF token has expired.",
    "Please sign in to continue.", "You do not have permission to do that.", "Page not found.",
    # error pages (app/__init__.py)
    "Not allowed", "Your account doesn't have permission for this action.", "Page not found", "This page doesn't exist, or it belongs to another account.",
    "Method not allowed", "This address doesn't accept that kind of request.", "File too large", "The upload is larger than the allowed size.",
    "Too many requests", "Please wait a moment and try again.", "Something went wrong", "The problem was logged. Please try again in a moment.",
    "Error", "The request could not be completed.", "arrival", "departure",
    # notification centre filters (app/services/alerts.py KINDS)
    "New booking", "Payment", "Cancellation", "Refund", "Check-in", "Check-out", "Dispute",
]

# Values the templates print from the database, shown with .replace('_', ' ')|title|tr
ENUM_VALUES = [
    "reserved", "checked_in", "checked_out", "cancelled", "pending", "paid", "failed", "refunded", "open", "completed", "in_progress",
    "owner", "staff", "platform_owner", "trial", "active", "suspended", "inactive", "blocked", "optional", "small", "suv", "van",
    "scratch", "dent", "crack", "broken_part", "none", "shuttle", "cleaning", "blog", "faq", "guide", "draft", "published",
    "invoice", "credit_note", "credited", "sent", "skipped", "email", "sms", "whatsapp", "reminder", "trialing", "past_due",
    "expired", "exempt", "starter", "pro", "business", "arrival", "departure", "day", "weekend", "season", "holiday", "flat",
    "per_day", "per_hour", "pk", "sk", "revoked", "disabled", "resolved", "dismissed", "under_review", "done", "manual", "stripe",
    "cash", "card", "pay_on_arrival", "walk_in", "online", "public", "secret", "percent", "fixed",
]


LOWERCASE = ["active", "inactive", "done", "open"]  # used inside sentences: "Discount code X is active."


def _title(value: str) -> str:
    return value.replace("_", " ").title()


def _unescape(text: str) -> str:
    return text.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _template_keys() -> set[str]:
    keys: set[str] = set()
    call = re.compile(r"""(?<![\w])_\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""")
    hint = re.compile(r"""\bhint=(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""")
    for path in TEMPLATES.rglob("*.html"):
        if path.relative_to(TEMPLATES).parts[0] in SKIP_TEMPLATE_DIRS:
            continue
        source = path.read_text(encoding="utf-8")
        for regex in (call, hint):
            for match in regex.finditer(source):
                keys.add(_unescape(match.group(1) if match.group(1) is not None else match.group(2)))
    return keys


def _pattern(node: ast.JoinedStr) -> str:
    out, used = "", set()
    for part in node.values:
        if isinstance(part, ast.Constant):
            out += str(part.value).replace("%", "%%")
        else:
            value = part.value
            name = value.id if isinstance(value, ast.Name) else (value.attr if isinstance(value, ast.Attribute) else f"p{len(used)}")
            while name in used:
                name += "_"
            used.add(name)
            out += f"%({name})s"
    return out


def _python_keys() -> set[str]:
    keys: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if path.name in ("ui_extract.py", "ui_i18n.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                name = getattr(node.func, "id", None)
                arg = node.args[0]
                if name in ("ui", "T") and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append" and node.args
                    and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "errors"
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)  # form.field.errors.append("...")
            if (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                    and " " in node.value.value and node.value.value[:1].isupper() and node.value.value.endswith(".")):
                keys.add(node.value.value)  # helpers that return an error sentence
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                arg = node.exc.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and " " in arg.value:
                    keys.add(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    keys.add(_pattern(arg))
            # form labels: StringField("Email", ...), SelectField("Status", ...)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "").endswith("Field") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "message" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        keys.add(keyword.value.value)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(t.endswith("LABELS") for t in targets):
                    for value in node.value.values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            keys.add(value.value)
    return keys


def js_keys() -> set[str]:
    """English texts used by the staff scripts through ppT("...")."""
    keys: set[str] = set()
    call = re.compile(r"""ppT\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
    for path in (ROOT / "static" / "js").glob("*.js"):
        for match in call.finditer(path.read_text(encoding="utf-8")):
            keys.add(_unescape(match.group(1) if match.group(1) is not None else match.group(2)))
    return keys


def collect_keys() -> set[str]:
    keys = _template_keys() | _python_keys() | js_keys() | set(STANDARD) | {_title(v) for v in ENUM_VALUES} | set(LOWERCASE)
    return {k for k in keys if re.search(r"[A-Za-z]{2}", k) and not re.fullmatch(r"(%\(\w+\)s|[\s·|/,.:()-])+", k)}


def load(locale: str) -> dict[str, str]:
    path = UI_DIR / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def missing(locales) -> dict[str, list[str]]:
    keys = collect_keys()
    return {locale: sorted(k for k in keys if k not in load(locale)) for locale in locales}


def stale(locale: str) -> list[str]:
    keys = collect_keys()
    return sorted(k for k in load(locale) if k not in keys)
