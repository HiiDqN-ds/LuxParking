"""One-off: wrap English string literals inside Jinja expressions and confirm() dialogs with _()."""
import glob, html, re, sys
sys.path.insert(0, "tools")
from convert_templates import EXPR, quote

LIT_SKIP = {"btn btn-danger", "btn btn-primary"}
lit = re.compile(r"""'((?:[^'\\]|\\.)*)'""")
blk = re.compile(r"\{\{(?!\s*_\().*?\}\}|\{%(?!\s*(?:extends|include|import|from)).*?%\}", re.S)
confirm = re.compile(r"confirm\('((?:[^'\\]|\\.)*)'\)")

def want(t):
    if t in LIT_SKIP or not re.search(r"[A-Za-z]{2,}", t):
        return False
    if re.fullmatch(r"[\w./:#-]+", t):          # identifiers, endpoints, css classes, urls
        return bool(re.fullmatch(r"[A-Z][a-z]+", t))  # ...except single capitalised words such as Yes / Active
    return " " in t or t[:1].isupper() or t.startswith((",", " "))


def fix_block(m):
    b = m.group(0)
    return lit.sub(lambda l: f"_({l.group(0)})" if want(l.group(1)) else l.group(0), b)

def fix_confirm(m):
    inner = m.group(1).replace("\\'", "'")
    pieces = EXPR.split(inner)
    statics, exprs = pieces[0::2], [re.sub(r"\|\s*e\s*$", "", e.strip()) for e in pieces[1::2]]
    key = ""
    kwargs = []
    for i, s in enumerate(statics):
        key += (s.replace("%", "%%") if exprs else s)
        if i < len(exprs):
            key += f"%(p{i})s"; kwargs.append(f"p{i}=({exprs[i]})")
    return "confirm({{ _(" + quote(key) + (", " + ", ".join(kwargs) if kwargs else "") + ")|tojson|forceescape }})"

n = 0
for p in glob.glob("app/templates/**/*.html", recursive=True):
    if "/public/" in p or "/legal/" in p: continue
    raw = open(p, "rb").read().decode("utf-8"); crlf = "\r\n" in raw; s = raw.replace("\r\n", "\n")
    new = confirm.sub(fix_confirm, s)
    new = blk.sub(fix_block, new)
    if new != s:
        n += 1
        open(p, "wb").write((new.replace("\n", "\r\n") if crlf else new).encode("utf-8"))
print("files:", n)
