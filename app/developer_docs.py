"""Turns the OpenAPI description into documentation (reference page, code examples, Postman collection).

Everything is rendered on our own server: no Swagger UI, no CDN, no third-party script (CSP- and GDPR-friendly)."""
from __future__ import annotations

import json
import re
from copy import deepcopy

METHOD_ORDER = {"get": 0, "post": 1, "patch": 2, "put": 3, "delete": 4}


def deref(spec: dict, node: dict) -> dict:
    while isinstance(node, dict) and "$ref" in node:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def ref_name(node: dict) -> str | None:
    return node["$ref"].rsplit("/", 1)[-1] if isinstance(node, dict) and "$ref" in node else None


def type_label(spec: dict, schema: dict) -> str:
    name = ref_name(schema)
    resolved = deref(spec, schema)
    if "oneOf" in schema or "oneOf" in resolved:
        options = (schema.get("oneOf") or resolved.get("oneOf"))
        parts = [type_label(spec, o) for o in options]
        return " | ".join(parts)
    if name and resolved.get("type") == "object":
        return name
    kind = resolved.get("type")
    if kind == "array":
        return type_label(spec, resolved.get("items", {})) + "[]"
    if "const" in resolved:
        return json.dumps(resolved["const"])
    if "enum" in resolved:
        return "enum: " + ", ".join(str(v) for v in resolved["enum"])
    if kind == "string" and resolved.get("format"):
        return f"string ({resolved['format']})"
    if name == "Money":
        return "string (money)"
    return kind or "object"


def rows(spec: dict, schema: dict, prefix: str = "", depth: int = 0) -> list[dict]:
    """Flatten an object schema into table rows (name, type, required, description). Two levels deep."""
    resolved = deref(spec, schema)
    if resolved.get("type") == "array":
        return rows(spec, resolved.get("items", {}), prefix + "[]", depth)
    if "oneOf" in resolved:
        return [row for option in resolved["oneOf"] for row in rows(spec, option, prefix, depth)]
    result = []
    required = set(resolved.get("required", ()))
    for name, prop in (resolved.get("properties") or {}).items():
        target = deref(spec, prop)
        label = type_label(spec, prop)
        description = (prop.get("description") if isinstance(prop, dict) else None) or target.get("description", "")
        result.append({"name": f"{prefix}{name}", "type": label, "required": name in required, "description": description})
        inner = target if target.get("type") == "object" else (deref(spec, target.get("items", {})) if target.get("type") == "array" else {})
        if depth < 1 and inner.get("type") == "object" and inner.get("properties"):
            result.extend(rows(spec, inner, f"{prefix}{name}{'[]' if target.get('type') == 'array' else ''}.", depth + 1))
    return result


def example(spec: dict, schema: dict, depth: int = 0):
    """A realistic sample value for a schema (explicit examples win)."""
    resolved = deref(spec, schema)
    for holder in (schema, resolved):
        if isinstance(holder, dict) and holder.get("example") is not None:
            return deepcopy(holder["example"])
        if isinstance(holder, dict) and holder.get("examples"):
            return deepcopy(holder["examples"][0])
    if "const" in resolved:
        return resolved["const"]
    if "oneOf" in resolved:
        options = [o for o in resolved["oneOf"] if deref(spec, o).get("type") != "null"]
        return example(spec, options[0], depth)
    if "enum" in resolved:
        return resolved["enum"][0]
    kind = resolved.get("type")
    if kind == "object":
        if depth > 3:
            return {}
        return {name: example(spec, prop, depth + 1) for name, prop in (resolved.get("properties") or {}).items()}
    if kind == "array":
        return [example(spec, resolved.get("items", {}), depth + 1)]
    fmt = resolved.get("format")
    if kind == "string":
        return {"date-time": "2026-12-20T08:00:00+01:00", "email": "kunde@example.de", "uri": "https://example.de/hooks/parkpilot", "binary": "(file)"}.get(fmt, "string")
    return {"integer": 1, "number": 1.5, "boolean": True}.get(kind, None)


def json_pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def operations(spec: dict) -> list[dict]:
    """Every operation, grouped by tag order, with the pieces the reference page needs."""
    tags = [t["name"] for t in spec.get("tags", [])]
    items = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            items.append({"path": path, "method": method, "op": op, "tag": (op.get("tags") or ["Other"])[0],
                          "anchor": op.get("operationId") or f"{method}-{path}"})
    items.sort(key=lambda i: (tags.index(i["tag"]) if i["tag"] in tags else 99, i["path"], METHOD_ORDER.get(i["method"], 9)))
    return items


def parameters(spec: dict, op: dict) -> list[dict]:
    return [deref(spec, p) for p in op.get("parameters", [])]


def request_schema(spec: dict, op: dict):
    content = (op.get("requestBody") or {}).get("content") or {}
    if not content:
        return None, None
    mime, media = next(iter(content.items()))
    return mime, media


def responses(spec: dict, op: dict) -> list[dict]:
    out = []
    for status, response in op.get("responses", {}).items():
        resolved = deref(spec, response)
        content = resolved.get("content") or {}
        mime, media = (next(iter(content.items())) if content else (None, None))
        out.append({"status": status, "description": resolved.get("description", ""), "mime": mime,
                    "schema": type_label(spec, media["schema"]) if media and "schema" in media else None,
                    "example": json_pretty(example(spec, media["schema"], 0)) if mime == "application/json" and media and status.startswith("2") else None})
    return sorted(out, key=lambda r: r["status"])


def curl_command(spec: dict, base_url: str, item: dict) -> str:
    path, method, op = item["path"], item["method"], item["op"]
    url_path = re.sub(r"\{(\w+)\}", lambda m: "1", path)
    query = [p for p in parameters(spec, op) if p.get("in") == "query" and p.get("required")]
    if query:
        samples = {"from": "2026-12-20T08:00:00%2B01:00", "to": "2026-12-27T20:00:00%2B01:00", "lot_id": "1"}
        url_path += "?" + "&".join(f"{p['name']}={samples.get(p['name'], '1')}" for p in query)
    secured = op.get("security", spec.get("security")) != []
    parts = [f'curl -X {method.upper()} "{base_url}/v1{url_path}"']
    if secured:
        parts.append('-H "Authorization: Bearer $PARKPILOT_API_KEY"')
    if method in {"post", "patch", "delete"} and any(p.get("name") == "Idempotency-Key" for p in map(lambda p: deref(spec, p), op.get("parameters", []))):
        parts.append('-H "Idempotency-Key: $(uuidgen)"')
    mime, media = request_schema(spec, op)
    if mime == "application/json":
        parts.append('-H "Content-Type: application/json"')
        parts.append("-d '" + json.dumps(example(spec, media["schema"]), ensure_ascii=False) + "'")
    elif mime == "multipart/form-data":
        parts += ['-F "photo=@vorher.jpg"', '-F "category=front"', '-F "captured_at=2026-12-20T08:05:00+01:00"', '-F "staff_user_id=1"']
    return " \\\n  ".join(parts)


def postman_collection(spec: dict) -> dict:
    """Postman Collection v2.1 generated from the spec: import it, set `apiKey` and `baseUrl`, send."""
    folders: dict[str, list] = {}
    for item in operations(spec):
        op = item["op"]
        url_path = re.sub(r"\{(\w+)\}", lambda m: f":{m.group(1)}", item["path"])
        variables = [{"key": name, "value": "1"} for name in re.findall(r"\{(\w+)\}", item["path"])]
        query = [{"key": p["name"], "value": {"from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00"}.get(p["name"], "1")}
                 for p in parameters(spec, op) if p.get("in") == "query" and p.get("required")]
        headers = []
        if any(deref(spec, p).get("name") == "Idempotency-Key" for p in op.get("parameters", [])):
            headers.append({"key": "Idempotency-Key", "value": "{{$guid}}"})
        mime, media = request_schema(spec, op)
        request = {"method": item["method"].upper(), "header": headers,
                   "url": {"raw": "{{baseUrl}}/v1" + url_path, "host": ["{{baseUrl}}"], "path": ["v1", *[seg for seg in url_path.split("/") if seg]],
                           "query": query, "variable": variables},
                   "description": op.get("description", "")}
        if item["path"] == "/health":
            request["auth"] = {"type": "noauth"}
        if mime == "application/json":
            request["header"].append({"key": "Content-Type", "value": "application/json"})
            request["body"] = {"mode": "raw", "raw": json_pretty(example(spec, media["schema"])), "options": {"raw": {"language": "json"}}}
        folders.setdefault(item["tag"], []).append({"name": op.get("summary", item["path"]), "request": request})
    return {
        "info": {"name": "ParkPilot API", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                 "description": "Set the collection variables `baseUrl` (your ParkPilot server) and `apiKey` (an sk_ or pk_ key)."},
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{apiKey}}", "type": "string"}]},
        "variable": [{"key": "baseUrl", "value": "http://127.0.0.1:5000"}, {"key": "apiKey", "value": "sk_xxxxxxxx.yyyyyyyyyyyy"}],
        "item": [{"name": tag, "item": requests} for tag, requests in folders.items()],
    }
