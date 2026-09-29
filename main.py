"""MARS demo sample app — notes in Postgres, answers from DO Serverless Inference, and /proof.

Env (all read per request, so a missing one reports instead of crashing the boot):
  DATABASE_URL      Postgres URL (App Platform DB binding: ${<db>.DATABASE_URL})
  MODEL_ACCESS_KEY  DO Serverless Inference model access key (app SECRET)
  INFERENCE_MODEL   model id, e.g. from GET https://inference.do-ai.run/v1/models — no default: a guessed
                    id would make /proof fail for the wrong reason
  INFERENCE_URL     default https://inference.do-ai.run/v1/chat/completions
  APP_COMMIT        optional; echoed by /proof so the caller can compare it to the sha it pushed
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

DEFAULT_INFERENCE_URL = "https://inference.do-ai.run/v1/chat/completions"
NOTE_MAX = 2000
QUESTION_MAX = 1000
ASK_NOTES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id bigserial PRIMARY KEY,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS proof_probe (
  nonce text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""


class NotConfigured(Exception):
    """A required env var is unset — reported as not-configured, never as a failure."""


def _env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise NotConfigured(f"{name} is not set")
    return v


def db_connect():
    import psycopg  # imported here so /healthz and the page work even if the driver is broken

    conn = psycopg.connect(_env("DATABASE_URL"), connect_timeout=5, autocommit=True)
    conn.execute(SCHEMA)
    return conn


def infer(messages, max_tokens=200):
    """(model, text, http_status). Raises NotConfigured, or RuntimeError carrying the HTTP status."""
    key, model = _env("MODEL_ACCESS_KEY"), _env("INFERENCE_MODEL")
    url = os.environ.get("INFERENCE_URL", "").strip() or DEFAULT_INFERENCE_URL
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
            status = r.status
    except urllib.error.HTTPError as e:
        hint = " — MODEL_ACCESS_KEY rejected" if e.code in (401, 403) else ""
        raise RuntimeError(f"inference HTTP {e.code}{hint}", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"inference unreachable: {e}", None) from None
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"inference response has no choices[0].message.content: {str(data)[:200]}", status) from None
    return data.get("model") or model, text, status


def refuse(msg, status=400):
    return jsonify({"kind": "mars-demo.error", "error": msg,
                    "usage": {"GET /notes": "list notes",
                              "POST /notes": '{"body": "<1-2000 chars>"}',
                              "POST /ask": '{"question": "<1-1000 chars>"}',
                              "GET /proof": "db + inference round-trip verdict",
                              "GET /healthz": "liveness only (no dependencies)"}}), status


def _text_field(name, limit):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, refuse(f'send JSON with Content-Type: application/json, e.g. {{"{name}": "..."}}')
    v = payload.get(name)
    if not isinstance(v, str) or not v.strip():
        return None, refuse(f'"{name}" must be a non-empty string')
    if len(v) > limit:
        return None, refuse(f'"{name}" is {len(v)} chars; the limit is {limit}')
    return v.strip(), None


@app.get("/healthz")
def healthz():
    return jsonify({"kind": "mars-demo.healthz", "status": "ok"})


@app.get("/notes")
def notes_list():
    try:
        with db_connect() as c:
            rows = c.execute("SELECT id, body, created_at FROM notes ORDER BY id DESC LIMIT 100").fetchall()
    except NotConfigured as e:
        return refuse(str(e), 503)
    except Exception as e:  # noqa: BLE001 — the caller is told, the process keeps serving
        return refuse(f"database error: {type(e).__name__}: {e}", 503)
    return jsonify({"kind": "mars-demo.notes", "notes": [
        {"id": r[0], "body": r[1], "created_at": r[2].isoformat()} for r in rows]})


@app.post("/notes")
def notes_add():
    body, err = _text_field("body", NOTE_MAX)
    if err:
        return err
    try:
        with db_connect() as c:
            nid = c.execute("INSERT INTO notes (body) VALUES (%s) RETURNING id", (body,)).fetchone()[0]
    except NotConfigured as e:
        return refuse(str(e), 503)
    except Exception as e:  # noqa: BLE001
        return refuse(f"database error: {type(e).__name__}: {e}", 503)
    return jsonify({"kind": "mars-demo.note", "id": nid, "body": body}), 201


@app.post("/ask")
def ask():
    q, err = _text_field("question", QUESTION_MAX)
    if err:
        return err
    try:
        with db_connect() as c:
            notes = [r[0] for r in c.execute("SELECT body FROM notes ORDER BY id DESC LIMIT %s", (ASK_NOTES,))]
    except NotConfigured as e:
        return refuse(str(e), 503)
    except Exception as e:  # noqa: BLE001
        return refuse(f"database error: {type(e).__name__}: {e}", 503)
    context = "\n".join(f"- {n}" for n in notes) or "(no notes yet)"
    try:
        model, text, _ = infer([
            {"role": "system", "content": "Answer using only the user's notes. If they do not cover it, say so."},
            {"role": "user", "content": f"Notes:\n{context}\n\nQuestion: {q}"}])
    except NotConfigured as e:
        return refuse(str(e), 503)
    except RuntimeError as e:
        return refuse(str(e.args[0]), 502)
    return jsonify({"kind": "mars-demo.answer", "answer": text, "model": model, "notes_used": len(notes)})


def _probe_db():
    t = time.monotonic()
    try:
        nonce = uuid.uuid4().hex
        with db_connect() as c:
            c.execute("INSERT INTO proof_probe (nonce) VALUES (%s)", (nonce,))
            back = c.execute("SELECT nonce FROM proof_probe WHERE nonce = %s", (nonce,)).fetchone()
        ok = bool(back) and back[0] == nonce
        return {"roundtrip": "ok" if ok else "fail", "nonce": nonce,
                "detail": "row written and read back" if ok else "row written but not read back",
                "ms": round((time.monotonic() - t) * 1000)}
    except NotConfigured as e:
        return {"roundtrip": "not-configured", "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"roundtrip": "fail", "detail": f"{type(e).__name__}: {e}"[:300],
                "ms": round((time.monotonic() - t) * 1000)}


def _probe_inference():
    t = time.monotonic()
    try:
        model, text, status = infer([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16)
        ok = bool(text.strip())
        return {"roundtrip": "ok" if ok else "fail", "model": model, "http_status": status,
                "completion_chars": len(text), "completion": text.strip()[:80],
                "detail": "completion received" if ok else "empty completion",
                "ms": round((time.monotonic() - t) * 1000)}
    except NotConfigured as e:
        return {"roundtrip": "not-configured", "detail": str(e)}
    except RuntimeError as e:
        return {"roundtrip": "fail", "http_status": e.args[1], "detail": e.args[0][:300],
                "ms": round((time.monotonic() - t) * 1000)}


def verdict(db, inf):
    states = {db["roundtrip"], inf["roundtrip"]}
    if "fail" in states:
        return "FAIL"
    if "not-configured" in states:
        return "DID-NOT-RUN"
    return "PASS"


@app.get("/proof")
def proof():
    db, inf = _probe_db(), _probe_inference()
    v = verdict(db, inf)
    return jsonify({"kind": "mars-demo.proof", "version": 1, "verdict": v, "db": db, "inference": inf,
                    "commit": os.environ.get("APP_COMMIT") or None}), (200 if v == "PASS" else 503)


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
           '<rect width="16" height="16" rx="3" fill="#111418"/><circle cx="8" cy="8" r="4" fill="#c2553a"/></svg>')


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    return Response(FAVICON, mimetype="image/svg+xml")


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


# DARK ONLY (owner accessibility rule): no light theme, no toggle, no prefers-color-scheme branch.
PAGE = """<!doctype html>
<html lang="en" style="background:#0d0f12">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0d0f12">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>MARS demo — notes</title>
<style>
  :root { color-scheme: dark; --bg:#0d0f12; --panel:#161a1f; --line:#262c34; --fg:#d7dce2; --dim:#8b95a1;
          --accent:#e0795c; --ok:#6fcf8f; --bad:#ef6b6b; --warn:#e6c26a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; color: var(--dim); margin: 24px 0 8px; }
  p.sub { color: var(--dim); margin: 0 0 16px; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
  input, textarea, button { font: inherit; color: var(--fg); background: var(--bg); border: 1px solid var(--line);
          border-radius: 6px; padding: 8px 10px; }
  textarea, input { width: 100%; } button { cursor: pointer; margin-top: 8px; }
  button:hover { border-color: var(--accent); }
  ul { list-style: none; padding: 0; margin: 0; } li { padding: 6px 0; border-bottom: 1px solid var(--line); }
  li:last-child { border-bottom: 0; } .dim { color: var(--dim); font-size: 13px; }
  pre { white-space: pre-wrap; margin: 8px 0 0; background: var(--bg); padding: 10px; border-radius: 6px;
        border: 1px solid var(--line); overflow-x: auto; }
  .PASS { color: var(--ok); } .FAIL { color: var(--bad); } .DID-NOT-RUN { color: var(--warn); }
</style>
</head>
<body>
<main>
  <h1>MARS demo — notes</h1>
  <p class="sub">Notes live in Postgres; answers come from DO Serverless Inference.</p>
  <h2>Add a note</h2>
  <section><textarea id="note" rows="2" maxlength="2000" placeholder="Something worth remembering"></textarea>
    <button id="add">Save note</button> <span id="addmsg" class="dim"></span></section>
  <h2>Ask about your notes</h2>
  <section><input id="q" maxlength="1000" placeholder="What did I write about…?">
    <button id="ask">Ask</button><pre id="answer" hidden></pre></section>
  <h2>Proof</h2>
  <section><button id="proof">Run /proof</button> <strong id="verdict"></strong><pre id="proofout" hidden></pre></section>
  <h2>Notes</h2>
  <section><ul id="notes"><li class="dim">loading…</li></ul></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function call(method, path, body) {
  const r = await fetch(path, {method, headers: body ? {'Content-Type': 'application/json'} : {},
                               body: body ? JSON.stringify(body) : undefined});
  let data; try { data = await r.json(); } catch { data = {error: 'non-JSON response, HTTP ' + r.status}; }
  return {ok: r.ok, status: r.status, data};
}
async function loadNotes() {
  const {ok, data} = await call('GET', '/notes');
  $('notes').innerHTML = !ok ? '<li class="FAIL">' + esc(data.error || 'error') + '</li>'
    : data.notes.length ? data.notes.map((n) => '<li>' + esc(n.body) + ' <span class="dim">#' + n.id + '</span></li>').join('')
    : '<li class="dim">no notes yet</li>';
}
$('add').onclick = async () => {
  const {ok, data} = await call('POST', '/notes', {body: $('note').value});
  $('addmsg').textContent = ok ? 'saved #' + data.id : data.error;
  if (ok) { $('note').value = ''; loadNotes(); }
};
$('ask').onclick = async () => {
  $('answer').hidden = false; $('answer').textContent = 'thinking…';
  const {ok, data} = await call('POST', '/ask', {question: $('q').value});
  $('answer').textContent = ok ? data.answer + '\\n\\n— ' + data.model + ', ' + data.notes_used + ' notes' : data.error;
};
$('proof').onclick = async () => {
  $('verdict').textContent = '…'; $('verdict').className = '';
  const {data} = await call('GET', '/proof');
  $('verdict').textContent = data.verdict || 'ERROR'; $('verdict').className = data.verdict || 'FAIL';
  $('proofout').hidden = false; $('proofout').textContent = JSON.stringify(data, null, 2);
};
loadNotes();
</script>
</body>
</html>
"""
