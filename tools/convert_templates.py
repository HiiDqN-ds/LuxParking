"""One-off converter: wraps the English text of back-office templates in {{ _('...') }} so it can be translated.

    python tools/convert_templates.py --dry-run     # report only
    python tools/convert_templates.py               # rewrite files in place

Text with {{ expressions }} becomes a message with named placeholders:  _('Showing %(p0)s of %(p1)s', p0=(a), p1=(b)).
Runs it cannot convert safely (|safe, |tojson, Markup) are listed so they can be fixed by hand.
"""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "app" / "templates"
TARGETS = ["admin", "staff"] + ["error.html", "home.html", "base.html", "_macros.html"]
TAG = r"""<(?:[^>"']|"[^"]*"|'[^']*')*>"""
SPLIT = re.compile(rf"(<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->|\{{%.*?%\}}|\{{#.*?#\}}|{TAG})", re.S)
EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
ATTR = re.compile(r"""(\s(?:placeholder|title|aria-label|alt)=)"([^"{}<>]*[A-Za-z][^"{}<>]*)\"""")
SKIP_TAGS = ("code", "pre", "kbd", "samp", "svg", "textarea")
BYPASS = ("safe", "tojson", "Markup", "csrf_token", "caller")


def quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def convert_run(run: str, report: list, where: str):
    """Return the converted text run or None to keep it."""
    pieces = EXPR.split(run)  # static, expr, static, expr, ...
    statics, exprs = pieces[0::2], [e.strip() for e in pieces[1::2]]
    visible = html.unescape("".join(statics))
    if not re.search(r"[A-Za-z]{2}", visible):
        return None
    if re.fullmatch(r"[A-Z0-9 %./:,\-\u2013\u00b7()\u20ac$+]+", visible.strip()):
        return None  # codes such as EUR, PDF, CSV stay as they are
    if any(b in e for e in exprs for b in BYPASS):
        report.append((where, run.strip()[:90]))
        return None
    lead = re.match(r"\s*", run).group(0)
    trail = re.search(r"\s*$", run).group(0)
    key_parts, kwargs = [], []
    for index, static in enumerate(statics):
        text = html.unescape(re.sub(r"\s+", " ", static))
        if exprs:
            text = text.replace("%", "%%")
        key_parts.append(text)
        if index < len(exprs):
            key_parts.append(f"%(p{index})s")
            kwargs.append(f"p{index}=({exprs[index]})")
    key = "".join(key_parts).strip()
    if not key:
        return None
    # the lead/trail spaces inside the static pieces are re-added outside the call
    call = f"{{{{ _({quote(key)}{', ' + ', '.join(kwargs) if kwargs else ''}) }}}}"
    return f"{lead}{call}{trail}"


def convert(source: str, where: str, report: list) -> str:
    out, stack = [], []
    for token in SPLIT.split(source):
        if not token:
            continue
        if SPLIT.fullmatch(token):
            if token.startswith("<") and not token.startswith(("<!--", "<script", "<style")):
                name = re.match(r"</?\s*([a-zA-Z0-9]+)", token)
                if name:
                    tag = name.group(1).lower()
                    if tag in SKIP_TAGS:
                        if token.startswith("</"):
                            if tag in stack:
                                stack.remove(tag)
                        elif not token.endswith("/>"):
                            stack.append(tag)
                token = ATTR.sub(lambda m: f'{m.group(1)}"{{{{ _({quote(html.unescape(m.group(2)))}) }}}}"', token)
            out.append(token)
            continue
        out.append(token if stack else (convert_run(token, report, where) or token))
    return "".join(out)


def main() -> None:
    dry = "--dry-run" in sys.argv
    report, changed = [], 0
    for target in TARGETS:
        base = ROOT / target
        files = sorted(base.glob("*.html")) if base.is_dir() else [base]
        for path in files:
            raw = path.read_bytes().decode("utf-8")
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n")
            new = convert(text, str(path.relative_to(ROOT)), report)
            if new != text:
                changed += 1
                if not dry:
                    path.write_bytes((new.replace("\n", "\r\n") if crlf else new).encode("utf-8"))
    print(f"files changed: {changed}")
    for where, run in report:
        print("SKIPPED", where, "|", run)


if __name__ == "__main__":
    main()
