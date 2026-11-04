"""Developer portal: pages, languages, no third-party resources, working code samples."""
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import textwrap

import pytest

from app.api_spec import OPENAPI_SPEC
from app.developer_samples import flow_samples, webhook_samples

PAGES = ("/developers/", "/developers/reference", "/developers/webhooks", "/developers/security", "/developers/changelog")


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("lang", ["de", "en"])
def test_every_page_renders_in_both_languages(client, path, lang):
    response = client.get(path, query_string={"lang": lang})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'<html lang="{lang}"' in html
    assert ("Entwickler" in html) if lang == "de" else ("Developers" in html)


def test_language_follows_the_browser_and_defaults_to_german(client):
    assert 'lang="de"' in client.get("/developers/", headers={"Accept-Language": "fr-FR"}).get_data(as_text=True)
    assert 'lang="en"' in client.get("/developers/", headers={"Accept-Language": "en-GB,en;q=0.9"}).get_data(as_text=True)
    assert 'lang="de"' in client.get("/developers/", headers={"Accept-Language": "de-DE"}).get_data(as_text=True)
    assert 'lang="en"' in client.get("/developers/?lang=en", headers={"Accept-Language": "de"}).get_data(as_text=True)
    assert "Die Parkplatz-API für Deutschland" in client.get("/developers/").get_data(as_text=True)


@pytest.mark.parametrize("path", PAGES)
def test_pages_load_nothing_from_third_parties(client, path):
    html = client.get(path).get_data(as_text=True)
    external = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', html) if "localhost" not in u and "127.0.0.1" not in u]
    assert not external, external
    assert "unpkg" not in html and "cdn." not in html and "googleapis" not in html


def test_reference_documents_every_operation_of_the_spec(client):
    html = client.get("/developers/reference").get_data(as_text=True)
    for path, item in OPENAPI_SPEC["paths"].items():
        for method, op in item.items():
            assert f'id="{op["operationId"]}"' in html, (method, path)
            assert f"/v1{path}" in html
    assert "curl -X POST" in html and "Idempotency-Key" in html and "X-Request-Id" in html


def test_postman_collection_covers_every_operation(client):
    response = client.get("/developers/postman.json")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
    collection = response.get_json()
    assert collection["info"]["schema"].endswith("collection/v2.1.0/collection.json")
    requests = [r for folder in collection["item"] for r in folder["item"]]
    assert len(requests) == sum(len(item) for item in OPENAPI_SPEC["paths"].values())
    assert {v["key"] for v in collection["variable"]} == {"baseUrl", "apiKey"}
    booking = next(r for r in requests if r["name"] == "Create a booking from a hold")
    assert json.loads(booking["request"]["body"]["raw"])["hold_id"] and any(h["key"] == "Idempotency-Key" for h in booking["request"]["header"])


def test_openapi_download_matches_the_api(client):
    assert client.get("/developers/openapi.json").get_json() == OPENAPI_SPEC


@pytest.mark.parametrize("lang", ["de", "en"])
def test_code_samples_are_valid_code(lang):
    samples = flow_samples("https://api.example.de", lang)
    compile(samples["python"]["code"], "flow.py", "exec")
    compile(webhook_samples(lang)["python"]["code"], "hook.py", "exec")
    assert samples["curl"]["code"].count("curl ") == 4
    if shutil.which("node"):
        for name, code in (("flow", samples["js"]["code"]), ("hook", webhook_samples(lang)["js"]["code"])):
            path = f"/tmp/_pp_{name}_{lang}.mjs"
            open(path, "w", encoding="utf-8").write(code)
            result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
    if shutil.which("php"):
        for name, code in (("flow", samples["php"]["code"]), ("hook", webhook_samples(lang)["php"]["code"])):
            path = f"/tmp/_pp_{name}_{lang}.php"
            open(path, "w", encoding="utf-8").write(code)
            assert subprocess.run(["php", "-l", path], capture_output=True).returncode == 0


def test_the_documented_signature_check_accepts_real_signatures_and_rejects_forged_ones():
    scope: dict = {}
    exec(webhook_samples("en")["python"]["code"], scope)  # the snippet exactly as published
    secret, body = "5f3a9c-secret", b'{"id":"abc","type":"booking.created","data":{"booking_id":1}}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()  # how app/services/api_platform.py signs
    assert scope["is_valid"](secret, body, signature) is True
    assert scope["is_valid"](secret, body + b" ", signature) is False
    assert scope["is_valid"]("other-secret", body, signature) is False
    assert scope["is_valid"](secret, body, "") is False


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_javascript_signature_check_matches_the_python_one(tmp_path):
    secret, body = "5f3a9c-secret", '{"id":"abc","type":"booking.created"}'
    signature = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    module = tmp_path / "check.mjs"
    module.write_text(webhook_samples("en")["js"]["code"] + textwrap.dedent(f"""
        console.log(JSON.stringify([isValid({secret!r}, {body!r}, {signature!r}), isValid({secret!r}, {body!r} + " ", {signature!r})]));
    """), encoding="utf-8")
    out = subprocess.run(["node", str(module)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, False]
