"""One-off: route every flash() message through ui() so it is translated. Source-position based, keeps formatting."""
import ast, re, sys
from pathlib import Path

FILES = ["admin", "manage", "staff", "auth", "billing", "checkout", "public"]
IMPORT = "from ..ui_i18n import ui\n"


def pattern(node: ast.JoinedStr, src: str):
    key, kwargs, used = "", [], set()
    for part in node.values:
        if isinstance(part, ast.Constant):
            key += str(part.value).replace("%", "%%")
        else:
            value = part.value
            seg = ast.get_source_segment(src, value)
            name = value.id if isinstance(value, ast.Name) else (value.attr if isinstance(value, ast.Attribute) else f"p{len(kwargs)}")
            while name in used:
                name += "_"
            used.add(name)
            key += f"%({name})s"
            if isinstance(value, ast.IfExp) and all(isinstance(b, ast.Constant) and isinstance(b.value, str) for b in (value.body, value.orelse)):
                seg = f"ui({seg})"
            kwargs.append(f"{name}={seg}")
    return key, kwargs


def convert(path: Path) -> int:
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    src = raw.replace("\r\n", "\n")
    tree = ast.parse(src)
    lines = src.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "flash" and node.args:
            arg = node.args[0]
            start = offsets[arg.lineno - 1] + arg.col_offset
            end = offsets[arg.end_lineno - 1] + arg.end_col_offset
            if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") in ("translate", "ui"):
                continue
            if isinstance(arg, ast.JoinedStr):
                key, kwargs = pattern(arg, src)
                new = "ui(" + repr(key).replace("\\\\", "\\") + (", " + ", ".join(kwargs) if kwargs else "") + ")"
                if "'" in key and '"' not in key:
                    new = "ui(" + '"' + key + '"' + (", " + ", ".join(kwargs) if kwargs else "") + ")"
            else:
                new = "ui(" + src[start:end] + ")"
            edits.append((start, end, new))
    if not edits:
        return 0
    for start, end, new in sorted(edits, reverse=True):
        src = src[:start] + new + src[end:]
    if "from ..ui_i18n import ui" not in src:
        # after the last top-level 'from .. import' line of the header
        m = list(re.finditer(r"^from \.\.[\w.]+ import .*(?:\n(?!from|import|\n).*)*$", src, re.M))
        pos = m[0].start()
        src = src[:pos] + IMPORT + src[pos:]
    path.write_bytes((src.replace("\n", "\r\n") if crlf else src).encode("utf-8"))
    return len(edits)


total = 0
for name in FILES:
    path = Path("app/blueprints") / f"{name}.py"
    n = convert(path)
    total += n
    print(name, n)
print("flash calls wrapped:", total)
