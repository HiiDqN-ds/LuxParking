"""Copy-paste code for the developer portal. Kept in Python (not in templates) so tests can run it for real."""
from __future__ import annotations


def flow_samples(base_url: str, lang: str) -> dict[str, dict]:
    de = lang == "de"
    c = (lambda d, e: d if de else e)
    curl = f'''# {c("1) Produkte und Preise lesen", "1) Read products and prices")}
curl "{base_url}/v1/products" -H "Authorization: Bearer $PARKPILOT_API_KEY"

# {c("2) Verbindlichen Preis berechnen", "2) Get a binding price")}
curl -X POST "{base_url}/v1/quotes" \\
  -H "Authorization: Bearer $PARKPILOT_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"lot_id": 1, "space_id": 1, "from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00"}}'

# {c("3) Platz reservieren und 4) buchen", "3) Hold the space and 4) book")}
curl -X POST "{base_url}/v1/holds" \\
  -H "Authorization: Bearer $PARKPILOT_API_KEY" -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \\
  -d '{{"lot_id": 1, "space_id": 1, "from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00", "email": "anna@example.de"}}'
curl -X POST "{base_url}/v1/bookings" \\
  -H "Authorization: Bearer $PARKPILOT_API_KEY" -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \\
  -d '{{"hold_id": 1, "first_name": "Anna", "email": "anna@example.de", "phone": "+491511234567"}}\''''
    python = f'''import os
import uuid

import requests

BASE = "{base_url}/v1"
HEADERS = {{"Authorization": f"Bearer {{os.environ['PARKPILOT_API_KEY']}}"}}


def call(method, path, **kwargs):
    headers = dict(HEADERS)
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())  # {c("macht Wiederholungen sicher", "makes retries safe")}
    response = requests.request(method, BASE + path, headers=headers, timeout=15, **kwargs)
    if not response.ok:
        error = response.json()["error"]  # code, message, request_id
        raise RuntimeError(f"{{error['code']}}: {{error['message']}} ({{error['request_id']}})")
    return response.json()


stay = {{"from": "2026-12-20T08:00:00+01:00", "to": "2026-12-27T20:00:00+01:00"}}
product = call("GET", "/products")["products"][0]
spot = {{"lot_id": product["lot_id"], "space_id": product["id"], **stay}}

quote = call("POST", "/quotes", json=spot)["quote"]
print("{c("Gesamtpreis", "Total")}:", quote["total"], quote["currency"])

hold = call("POST", "/holds", json={{**spot, "email": "anna@example.de"}})["hold"]
booking = call("POST", "/bookings", json={{"hold_id": hold["id"], "first_name": "Anna", "email": "anna@example.de", "phone": "+491511234567"}})["booking"]
print(booking["booking_ref"], booking["status"])'''
    js = f'''// Node.js 18+ ({c("als .mjs-Datei", "save as .mjs")})
import {{ randomUUID }} from "node:crypto";

const BASE = "{base_url}/v1";
const KEY = process.env.PARKPILOT_API_KEY;

async function call(method, path, body) {{
  const headers = {{ Authorization: `Bearer ${{KEY}}`, "Content-Type": "application/json" }};
  if (method !== "GET") headers["Idempotency-Key"] = randomUUID(); // {c("macht Wiederholungen sicher", "makes retries safe")}
  const response = await fetch(BASE + path, {{ method, headers, body: body ? JSON.stringify(body) : undefined }});
  const data = await response.json();
  if (!response.ok) throw new Error(`${{data.error.code}}: ${{data.error.message}} (${{data.error.request_id}})`);
  return data;
}}

const stay = {{ from: "2026-12-20T08:00:00+01:00", to: "2026-12-27T20:00:00+01:00" }};
const product = (await call("GET", "/products")).products[0];
const spot = {{ lot_id: product.lot_id, space_id: product.id, ...stay }};

const {{ quote }} = await call("POST", "/quotes", spot);
console.log("{c("Gesamtpreis", "Total")}:", quote.total, quote.currency);

const {{ hold }} = await call("POST", "/holds", {{ ...spot, email: "anna@example.de" }});
const {{ booking }} = await call("POST", "/bookings", {{ hold_id: hold.id, first_name: "Anna", email: "anna@example.de", phone: "+491511234567" }});
console.log(booking.booking_ref, booking.status);'''
    php = f'''<?php
$base = "{base_url}/v1";
$key = getenv("PARKPILOT_API_KEY");

function call(string $method, string $path, ?array $body = null): array {{
    global $base, $key;
    $headers = ["Authorization: Bearer $key", "Content-Type: application/json"];
    if ($method !== "GET") {{
        $headers[] = "Idempotency-Key: " . bin2hex(random_bytes(16)); // {c("macht Wiederholungen sicher", "makes retries safe")}
    }}
    $options = [CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 15];
    if ($body !== null) {{
        $options[CURLOPT_POSTFIELDS] = json_encode($body);
    }}
    $ch = curl_init($base . $path);
    curl_setopt_array($ch, $options);
    $data = json_decode(curl_exec($ch), true);
    $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($status >= 400) {{
        throw new RuntimeException($data["error"]["code"] . ": " . $data["error"]["message"] . " (" . $data["error"]["request_id"] . ")");
    }}
    return $data;
}}

$stay = ["from" => "2026-12-20T08:00:00+01:00", "to" => "2026-12-27T20:00:00+01:00"];
$product = call("GET", "/products")["products"][0];
$spot = ["lot_id" => $product["lot_id"], "space_id" => $product["id"]] + $stay;

$quote = call("POST", "/quotes", $spot)["quote"];
echo "{c("Gesamtpreis", "Total")}: {{$quote['total']}} {{$quote['currency']}}\\n";

$hold = call("POST", "/holds", $spot + ["email" => "anna@example.de"])["hold"];
$booking = call("POST", "/bookings", ["hold_id" => $hold["id"], "first_name" => "Anna", "email" => "anna@example.de", "phone" => "+491511234567"])["booking"];
echo $booking["booking_ref"] . " " . $booking["status"] . "\\n";'''
    return {"curl": {"label": "curl", "code": curl}, "python": {"label": "Python", "code": python},
            "js": {"label": "JavaScript", "code": js}, "php": {"label": "PHP", "code": php}}


def webhook_samples(lang: str) -> dict[str, dict]:
    de = lang == "de"
    c = (lambda d, e: d if de else e)
    python = f'''import hashlib
import hmac


def is_valid(signing_secret: str, raw_body: bytes, signature_header: str) -> bool:
    """raw_body: {c("die unveränderten Bytes der Anfrage (nicht neu serialisiert!)", "the unchanged request bytes (not re-serialised!)")}"""
    expected = "sha256=" + hmac.new(signing_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


# Flask: is_valid(SECRET, request.get_data(), request.headers.get("X-ParkPilot-Signature"))'''
    js = f'''import {{ createHmac, timingSafeEqual }} from "node:crypto";

// rawBody: {c("Buffer/String der unveränderten Anfrage (z. B. express.raw())", "Buffer/string of the unchanged request (e.g. express.raw())")}
export function isValid(signingSecret, rawBody, signatureHeader) {{
  const expected = Buffer.from("sha256=" + createHmac("sha256", signingSecret).update(rawBody).digest("hex"));
  const received = Buffer.from(signatureHeader || "");
  return received.length === expected.length && timingSafeEqual(received, expected);
}}'''
    php = f'''<?php
// {c("Unveränderte Anfrage lesen", "Read the unchanged request")}: $rawBody = file_get_contents("php://input");
function isValid(string $signingSecret, string $rawBody, string $signatureHeader): bool {{
    $expected = "sha256=" . hash_hmac("sha256", $rawBody, $signingSecret);
    return hash_equals($expected, $signatureHeader);
}}

// if (!isValid($secret, $rawBody, $_SERVER["HTTP_X_PARKPILOT_SIGNATURE"] ?? "")) {{ http_response_code(401); exit; }}'''
    return {"python": {"label": "Python", "code": python}, "js": {"label": "JavaScript", "code": js}, "php": {"label": "PHP", "code": php}}
