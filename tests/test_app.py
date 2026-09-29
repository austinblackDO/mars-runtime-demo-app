"""App tests — no live calls. Inference is a local fake server; Postgres legs need TEST_DATABASE_URL.

Every /proof outcome branch has a leg, INCLUDING the healthy one, and "not configured" is proven to render
differently from "failed".
"""
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

PG = os.environ.get("TEST_DATABASE_URL", "").strip()
needs_pg = pytest.mark.skipif(not PG, reason="TEST_DATABASE_URL unset — Postgres legs verified NOTHING")


class Fake(BaseHTTPRequestHandler):
    mode = "ok"
    seen = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"auth": self.headers.get("Authorization"), "body": body})
        if Fake.mode == "401":
            return self._send(401, {"error": "unauthorized"})
        if Fake.mode == "malformed":
            return self._send(200, {"nope": True})
        text = "" if Fake.mode == "empty" else "pong"
        self._send(200, {"model": body["model"], "choices": [{"message": {"content": text}}]})

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def fake_url():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    srv.shutdown()


@pytest.fixture
def client(monkeypatch):
    for k in ("DATABASE_URL", "MODEL_ACCESS_KEY", "INFERENCE_MODEL", "INFERENCE_URL", "APP_COMMIT"):
        monkeypatch.delenv(k, raising=False)
    Fake.mode, Fake.seen = "ok", []
    return main.app.test_client()


@pytest.fixture
def inference_on(monkeypatch, fake_url):
    monkeypatch.setenv("INFERENCE_URL", fake_url)
    monkeypatch.setenv("MODEL_ACCESS_KEY", "test-key-not-real")
    monkeypatch.setenv("INFERENCE_MODEL", "fake-model")


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_healthz_has_no_dependencies(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json == {"kind": "mars-demo.healthz", "status": "ok"}


def test_proof_unconfigured_is_did_not_run_not_fail(client):
    r = client.get("/proof")
    assert r.status_code == 503
    assert r.json["verdict"] == "DID-NOT-RUN"
    assert r.json["db"]["roundtrip"] == "not-configured" and "DATABASE_URL" in r.json["db"]["detail"]
    assert r.json["inference"]["roundtrip"] == "not-configured"


def test_proof_unreachable_db_is_fail_not_did_not_run(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:p@127.0.0.1:{closed_port()}/x")
    r = client.get("/proof")
    assert r.json["db"]["roundtrip"] == "fail"
    assert r.json["verdict"] == "FAIL" and r.status_code == 503


def test_inference_ok_sends_key_and_model(client, inference_on):
    r = client.get("/proof")
    inf = r.json["inference"]
    assert inf["roundtrip"] == "ok" and inf["model"] == "fake-model" and inf["completion"] == "pong"
    assert Fake.seen[-1]["auth"] == "Bearer test-key-not-real"
    assert r.json["verdict"] == "DID-NOT-RUN"  # db still unconfigured: a half-proof is never PASS


def test_inference_401_is_reported_as_rejected_key(client, inference_on):
    Fake.mode = "401"
    inf = client.get("/proof").json["inference"]
    assert inf["roundtrip"] == "fail" and inf["http_status"] == 401 and "MODEL_ACCESS_KEY rejected" in inf["detail"]


@pytest.mark.parametrize("mode,needle", [("empty", "empty completion"), ("malformed", "no choices")])
def test_inference_bad_payloads_fail(client, inference_on, mode, needle):
    Fake.mode = mode
    inf = client.get("/proof").json["inference"]
    assert inf["roundtrip"] == "fail" and needle in inf["detail"]


def test_inference_model_has_no_default(client, inference_on, monkeypatch):
    monkeypatch.delenv("INFERENCE_MODEL")
    inf = client.get("/proof").json["inference"]
    assert inf["roundtrip"] == "not-configured" and "INFERENCE_MODEL" in inf["detail"]
    assert Fake.seen == []  # refused before any call


def test_inference_unreachable_is_fail(client, inference_on, monkeypatch):
    monkeypatch.setenv("INFERENCE_URL", f"http://127.0.0.1:{closed_port()}/v1/chat/completions")
    inf = client.get("/proof").json["inference"]
    assert inf["roundtrip"] == "fail" and "unreachable" in inf["detail"]


def test_verdict_table():
    ok, nc, f = {"roundtrip": "ok"}, {"roundtrip": "not-configured"}, {"roundtrip": "fail"}
    assert main.verdict(ok, ok) == "PASS"
    assert main.verdict(ok, nc) == main.verdict(nc, nc) == "DID-NOT-RUN"
    assert main.verdict(f, nc) == main.verdict(ok, f) == "FAIL"


@pytest.mark.parametrize("path,kwargs,needle", [
    ("/notes", {"data": "x"}, "send JSON"),
    ("/notes", {"json": {"body": "  "}}, "non-empty"),
    ("/notes", {"json": {"body": "x" * 2001}}, "limit is 2000"),
    ("/ask", {"json": {}}, "non-empty"),
])
def test_bad_input_refuses_and_teaches(client, path, kwargs, needle):
    r = client.post(path, **kwargs)
    assert r.status_code == 400 and needle in r.json["error"] and "POST /notes" in r.json["usage"]


LIGHT = re.compile(r"#fff\b|#ffffff\b|(?<![-\w])white(?![-\w])|\brgb\(\s*255\s*,\s*255\s*,\s*255", re.I)


def test_light_detector_discriminates():
    assert LIGHT.search("body{background:white}") and LIGHT.search("color:#FFF;") and LIGHT.search("rgb(255, 255,255)")
    assert not LIGHT.search("white-space: pre-wrap; --whitelist: 1")


def test_page_is_dark_only(client):
    html = client.get("/").get_data(as_text=True)
    assert 'name="color-scheme" content="dark"' in html and "color-scheme: dark" in html
    assert "prefers-color-scheme" not in html  # no light branch to fall into
    assert not LIGHT.search(html), LIGHT.search(html)
    fav = client.get("/favicon.ico")
    assert fav.mimetype == "image/svg+xml" and "#fff" not in fav.get_data(as_text=True).lower()


@needs_pg
def test_pg_notes_roundtrip_and_ask(client, inference_on, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PG)
    marker = os.urandom(4).hex()
    assert client.post("/notes", json={"body": f"the launch code is {marker}"}).status_code == 201
    assert any(marker in n["body"] for n in client.get("/notes").json["notes"])
    r = client.post("/ask", json={"question": "what is the launch code?"})
    assert r.status_code == 200 and r.json["notes_used"] >= 1
    assert marker in Fake.seen[-1]["body"]["messages"][-1]["content"]  # notes really reached the model


@needs_pg
def test_pg_proof_pass(client, inference_on, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PG)
    monkeypatch.setenv("APP_COMMIT", "abc123")
    r = client.get("/proof")
    assert r.status_code == 200 and r.json["verdict"] == "PASS", r.json
    assert r.json["db"]["roundtrip"] == "ok" and r.json["commit"] == "abc123"
