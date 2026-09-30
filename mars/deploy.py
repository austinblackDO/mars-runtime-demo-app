#!/usr/bin/env python3
"""mars/deploy.py — the deterministic half of the MARS demo: deploy this repo to App Platform, bound to a
PRE-CREATED Postgres cluster, and prove the database and Serverless Inference both work via /proof.

Stdlib only (runs in a bare MARS sandbox). The agent runs it and narrates; it never improvises the API calls.

usage: python3 mars/deploy.py <verb>
  preflight   check env, DO token, cluster online, model listed, source sha — changes nothing
  run         preflight + create the app (or update + forced build) + commit check + /proof → VERDICT
  status      print the app's live URL and active deployment (read-only)

Env (secrets are read here and never printed):
  DO_API_TOKEN       DO token: app create/read/update, database read
  MODEL_ACCESS_KEY   Serverless Inference key: checked against /v1/models, then set as the app's SECRET
  DEMO_APP_REPO      owner/name of the public app repo (git source; no GitHub link needed)
  DEMO_APP_MODEL     model id the app answers with; must appear in /v1/models
  DEMO_DB_CLUSTER    name of the pre-created managed PG cluster (⭐ ENG-3 ruling)
  DEMO_APP_NAME      default mars-demo-app · DEMO_REGION default nyc
  DEMO_DEADLINE_S    default 420 (the 8-min stage budget minus margin) · DEMO_POLL_S default 5
  DEMO_DB_WAIT_S     default 60: how long preflight waits for the cluster to read `online`
  DEMO_EDGE_IPS      default 172.66.0.96,162.159.140.98 (see EDGE_IPS)
Test seams (printed on every run when set): DO_API_BASE, INFERENCE_BASE, GITHUB_API_BASE.

Output grammar (one line each, machine-readable; the only prose is the one-line reason):
  PHASE <name> <status> t+<seconds>s [detail]
  VERDICT: PASS|FAIL|DID-NOT-RUN <reason>
  RESULT {"kind": "mars-demo.result", ...}
Exit: 0 PASS · 1 FAIL · 2 DID-NOT-RUN · 64 usage.
"""
import http.client as httpclient  # aliased: this module defines its own http()
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

T0 = time.monotonic()
DO_API = os.environ.get("DO_API_BASE", "https://api.digitalocean.com").rstrip("/")
INFER = os.environ.get("INFERENCE_BASE", "https://inference.do-ai.run").rstrip("/")
GH_API = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
# App Platform's shared Cloudflare edge for *.ondigitalocean.app (every app on the team resolved to these,
# measured 2026-09-29; an unknown host through them answers 530, a routed one 200). Connecting here with the app's
# hostname as SNI never asks DNS about the NEW name, whose NXDOMAIN the zone lets resolvers cache for 1800 s.
EDGE_IPS = [x.strip() for x in os.environ.get("DEMO_EDGE_IPS", "172.66.0.96,162.159.140.98").split(",") if x.strip()]
REQUIRED = ("DO_API_TOKEN", "MODEL_ACCESS_KEY", "DEMO_APP_REPO", "DEMO_APP_MODEL", "DEMO_DB_CLUSTER")
TERMINAL_BAD = {"ERROR", "CANCELED", "SUPERSEDED"}


class Stop(Exception):
    """Ends the run with a verdict. `verdict` is FAIL or DID-NOT-RUN; reason is one line."""

    def __init__(self, verdict, reason, **facts):
        super().__init__(reason)
        self.verdict, self.reason, self.facts = verdict, reason, facts


def t():
    return f"t+{int(time.monotonic() - T0)}s"


def phase(name, status, detail=""):
    print(f"PHASE {name} {status} {t()}{' ' + detail if detail else ''}", flush=True)


def envint(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        raise Stop("DID-NOT-RUN", f"{name}={v!r} is not an integer") from None


def http(method, url, token=None, body=None, timeout=30):
    """(status, parsed-json-or-text). Network errors return status 0 — never raise into the caller."""
    headers = {"Accept": "application/json", "User-Agent": "mars-demo-deploy/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode(errors="replace")[:300]


def http_via(url, ip, timeout=30):
    """GET url by connecting to `ip` while TLS-verifying and routing on the URL's own hostname (curl --resolve)."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    ctx = ssl.create_default_context()

    class Conn(httpclient.HTTPSConnection if u.scheme == "https" else httpclient.HTTPConnection):
        def connect(self):
            sock = socket.create_connection((ip, port), timeout)
            self.sock = ctx.wrap_socket(sock, server_hostname=host) if u.scheme == "https" else sock

    kw = {"context": ctx} if u.scheme == "https" else {}
    c = Conn(host, port, timeout=timeout, **kw)
    try:
        c.request("GET", path, headers={"Host": u.netloc, "Accept": "application/json", "User-Agent": "mars-demo-deploy/1"})
        r = c.getresponse()
        raw, status = r.read(), r.status
    except (OSError, httpclient.HTTPException) as e:
        return 0, f"{type(e).__name__}: {e}"
    finally:
        c.close()
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode(errors="replace")[:300]


def do(method, path, body=None):
    return http(method, DO_API + path, os.environ["DO_API_TOKEN"], body)


def api_error(what, status, data):
    msg = data.get("message") if isinstance(data, dict) else data
    return f"{what}: HTTP {status} {str(msg)[:200]}"


# ── preflight ─────────────────────────────────────────────────────────────────────────────────────
def preflight():
    phase("preflight", "start")
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        raise Stop("DID-NOT-RUN", f"env not set: {', '.join(missing)}")
    placeholders = [k for k in ("DO_API_TOKEN", "MODEL_ACCESS_KEY") if os.environ[k].strip().startswith("REPLACE-WITH-")]
    if placeholders:
        raise Stop("DID-NOT-RUN", f"still the stub placeholder, not a real value: {', '.join(placeholders)}")
    for seam, val in (("DO_API_BASE", DO_API), ("INFERENCE_BASE", INFER), ("GITHUB_API_BASE", GH_API)):
        if os.environ.get(seam):
            phase("preflight", "note", f"TEST SEAM {seam}={val}")

    s, d = do("GET", "/v2/account")
    if s in (401, 403):
        raise Stop("DID-NOT-RUN", f"DO token rejected (HTTP {s})")
    if s != 200:
        raise Stop("DID-NOT-RUN", api_error("DO API unreachable", s, d))
    phase("preflight", "ok", "do-token")

    cluster = os.environ["DEMO_DB_CLUSTER"]
    wait_until = time.monotonic() + envint("DEMO_DB_WAIT_S", 60)
    while True:
        s, d = do("GET", "/v2/databases")
        if s != 200:
            raise Stop("DID-NOT-RUN", api_error("cannot list databases", s, d))
        match = [x for x in d.get("databases") or [] if x.get("name") == cluster]
        if not match:
            names = sorted(x.get("name", "?") for x in d.get("databases") or [])
            raise Stop("DID-NOT-RUN", f"db did not come up: no cluster named {cluster!r} on this team (have: {names})")
        db = match[0]
        if db.get("status") == "online":
            break
        if time.monotonic() > wait_until:
            raise Stop("DID-NOT-RUN", f"db did not come up: cluster {cluster!r} is {db.get('status')!r}, not online")
        phase("preflight", "wait", f"db {db.get('status')}")
        time.sleep(envint("DEMO_POLL_S", 5))
    if db.get("engine") != "pg":
        raise Stop("DID-NOT-RUN", f"cluster {cluster!r} is engine {db.get('engine')!r}, not pg")
    phase("preflight", "ok", f"db {cluster} online ({db.get('region')})")

    s, d = http("GET", INFER + "/v1/models", os.environ["MODEL_ACCESS_KEY"])
    if s in (401, 403):
        raise Stop("FAIL", f"inference key rejected (HTTP {s}) by {INFER}/v1/models")
    if s != 200:
        raise Stop("DID-NOT-RUN", api_error("inference unreachable", s, d))
    ids = sorted(m.get("id", "") for m in (d.get("data") if isinstance(d, dict) else None) or [])
    model = os.environ["DEMO_APP_MODEL"]
    if model not in ids:
        raise Stop("DID-NOT-RUN", f"model {model!r} not offered by /v1/models (e.g. {ids[:8]})")
    phase("preflight", "ok", f"inference model {model}")

    repo = os.environ["DEMO_APP_REPO"]
    s, d = http("GET", f"{GH_API}/repos/{repo}/commits/main")
    if s != 200 or not isinstance(d, dict) or not d.get("sha"):
        raise Stop("DID-NOT-RUN", api_error(f"cannot read {repo} main", s, d))
    sha = d["sha"]
    phase("preflight", "ok", f"source {repo}@{sha[:12]}")
    return {"cluster": cluster, "model": model, "repo": repo, "sha": sha}


# ── app spec + deploy ─────────────────────────────────────────────────────────────────────────────
def app_spec(pf):
    cluster = pf["cluster"]
    return {
        "name": os.environ.get("DEMO_APP_NAME", "").strip() or "mars-demo-app",
        "region": os.environ.get("DEMO_REGION", "").strip() or "nyc",
        "services": [{
            "name": "web",
            "git": {"repo_clone_url": f"https://github.com/{pf['repo']}.git", "branch": "main"},
            "environment_slug": "python",
            "instance_size_slug": "apps-s-1vcpu-0.5gb",
            "instance_count": 1,
            "http_port": 8080,
            "run_command": "gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 60 main:app",
            "health_check": {"http_path": "/healthz"},
            "envs": [
                {"key": "DATABASE_URL", "scope": "RUN_TIME", "value": "${" + cluster + ".DATABASE_URL}"},
                {"key": "MODEL_ACCESS_KEY", "scope": "RUN_TIME", "type": "SECRET",
                 "value": os.environ["MODEL_ACCESS_KEY"]},
                {"key": "INFERENCE_MODEL", "scope": "RUN_TIME", "value": pf["model"]},
            ],
        }],
        "databases": [{"name": cluster, "engine": "PG", "production": True, "cluster_name": cluster}],
    }


def deploy(pf):
    spec = app_spec(pf)
    phase("deploy", "start", f"app {spec['name']} in {spec['region']}")
    s, d = do("GET", "/v2/apps?per_page=200")
    if s != 200:
        raise Stop("FAIL", api_error("cannot list apps", s, d))
    existing = [a for a in d.get("apps") or [] if a.get("spec", {}).get("name") == spec["name"]]
    if existing:
        app_id = existing[0]["id"]
        s, d = do("PUT", f"/v2/apps/{app_id}", {"spec": spec})
        if s != 200:
            raise Stop("FAIL", api_error("app spec update rejected", s, d))
        phase("deploy", "updated", f"existing app {app_id}")
    else:
        s, d = do("POST", "/v2/apps", {"spec": spec})
        if s not in (200, 201):
            raise Stop("FAIL", api_error("app create rejected", s, d))
        app_id = d["app"]["id"]
        phase("deploy", "created", f"app {app_id}")
        # a fresh app's own first deployment builds main as it is now; forcing a second would supersede it and
        # double the build time
        for _ in range(12):
            s, d = do("GET", f"/v2/apps/{app_id}/deployments")
            deps = sorted((d.get("deployments") or []) if s == 200 else [], key=lambda x: x.get("created_at", ""))
            if deps:
                phase("deploy", "building", f"deployment {deps[-1]['id']}")
                return app_id, deps[-1]["id"]
            time.sleep(envint("DEMO_POLL_S", 5))
        raise Stop("FAIL", "app created but no deployment appeared", app_id=app_id)
    # an EXISTING app: force a build of the sha we checked — a spec update can deploy the PREVIOUS commit
    # (stale-commit bug, mars-hackathon HANDOFF); the source_commit_hash check catches it if it happens anyway
    s, d = do("POST", f"/v2/apps/{app_id}/deployments", {"force_build": True})
    if s not in (200, 201):
        raise Stop("FAIL", api_error("forced build rejected", s, d))
    dep_id = d["deployment"]["id"]
    phase("deploy", "building", f"deployment {dep_id} (forced)")
    return app_id, dep_id


def wait_active(app_id, dep_id, pf, deadline):
    last = None
    while True:
        s, d = do("GET", f"/v2/apps/{app_id}/deployments/{dep_id}")
        if s == 200:
            dep = d["deployment"]
            ph = dep.get("phase")
            prog = dep.get("progress") or {}
            if ph != last:
                phase("deploy", ph or "?", f"steps {prog.get('success_steps', 0)}/{prog.get('total_steps', 0)}")
                last = ph
            if ph == "ACTIVE":
                shas = {c.get("source_commit_hash") for c in dep.get("services") or []}
                if shas != {pf["sha"]}:
                    raise Stop("FAIL", f"stale commit: deployment runs {sorted(x or '?' for x in shas)}, "
                                       f"expected {pf['sha'][:12]}", app_id=app_id, deployment=dep_id)
                return
            if ph in TERMINAL_BAD:
                steps = [x.get("name") for x in prog.get("steps") or [] if x.get("status") == "ERROR"]
                raise Stop("FAIL", f"deployment {ph}{' at ' + ','.join(steps) if steps else ''}",
                           app_id=app_id, deployment=dep_id)
        else:
            phase("deploy", "poll-error", api_error("deployment read", s, d))
        if time.monotonic() > deadline:
            raise Stop("FAIL", f"deploy did not finish in budget (last phase {last})", app_id=app_id, deployment=dep_id)
        time.sleep(envint("DEMO_POLL_S", 5))


def prove(app_id, pf, deadline):
    s, d = do("GET", f"/v2/apps/{app_id}")
    url = (d.get("app") or {}).get("live_url") if s == 200 else None
    if not url:
        raise Stop("FAIL", api_error("app has no live_url after ACTIVE", s, d), app_id=app_id)
    phase("proof", "start", url)
    last, n, edge_unreachable = "no attempt", 0, 0
    # Edge IPs, round-robin, for the whole budget. Two causes, two paths (laptop run 2): HTTP 530 = the edge is up but
    # its route to the new app is not live yet → keep waiting ON THE EDGE (a DNS lookup now could cache NXDOMAIN for
    # 1800 s); a CONNECTION failure = this sandbox cannot reach the edge IPs at all → only then fall back to DNS.
    routes = [("edge", ip) for ip in EDGE_IPS]
    use_dns = not routes
    while True:
        route = ("dns", None) if use_dns else routes[n % len(routes)]
        if route[0] == "edge":
            s, body = http_via(url.rstrip("/") + "/proof", route[1], timeout=45)
            if s == 0:
                edge_unreachable += 1
                if edge_unreachable >= 2 * len(routes):
                    use_dns = True
                    phase("proof", "note", "edge IPs unreachable from here (connection errors) — falling back to DNS")
        else:
            s, body = http("GET", url.rstrip("/") + "/proof", timeout=45)
        n += 1
        if isinstance(body, dict) and body.get("kind") == "mars-demo.proof":
            db, inf = body.get("db") or {}, body.get("inference") or {}
            phase("proof", body.get("verdict", "?"),
                  f"db={db.get('roundtrip')} inference={inf.get('roundtrip')} via {route[0]}{' ' + route[1] if route[1] else ''}")
            if body.get("verdict") == "PASS" and db.get("roundtrip") == "ok" and inf.get("roundtrip") == "ok":
                if body.get("commit") not in (None, pf["sha"]):
                    raise Stop("FAIL", f"/proof reports commit {body['commit']}, expected {pf['sha'][:12]}")
                return url, body
            parts = [f"db {db.get('roundtrip')}: {db.get('detail')}" if db.get("roundtrip") != "ok" else "",
                     f"inference {inf.get('roundtrip')}: {inf.get('detail')}" if inf.get("roundtrip") != "ok" else ""]
            raise Stop("FAIL", "/proof " + "; ".join(p for p in parts if p), url=url, proof=body)
        snippet = "HTML page" if isinstance(body, str) and body.lstrip().startswith("<") else " ".join(str(body).split())[:100]
        last = f"via {route[0]}{' ' + route[1] if route[1] else ''}: HTTP {s} {snippet}"  # one line: the agent narrates these
        if time.monotonic() > deadline:
            raise Stop("FAIL", f"/proof never answered in budget (last: {last})", url=url)
        phase("proof", "retry", last)
        time.sleep(envint("DEMO_POLL_S", 5))


def finish(verdict, reason, **facts):
    print(f"VERDICT: {verdict} {reason}", flush=True)
    print("RESULT " + json.dumps({"kind": "mars-demo.result", "version": 1, "verdict": verdict,
                                  "reason": reason, "elapsed_s": int(time.monotonic() - T0), **facts},
                                 sort_keys=True, default=str), flush=True)
    return {"PASS": 0, "FAIL": 1, "DID-NOT-RUN": 2}[verdict]


def cmd_run():
    deadline = time.monotonic() + envint("DEMO_DEADLINE_S", 420)
    app_id = None
    try:
        pf = preflight()
        app_id, dep_id = deploy(pf)
        wait_active(app_id, dep_id, pf, deadline)
        url, proof = prove(app_id, pf, deadline)
        db, inf = proof["db"], proof["inference"]
        return finish("PASS", f"{url} db row round-trip ok, inference {inf.get('model')} answered",
                      app_id=app_id, url=url, commit=pf["sha"], db_ms=db.get("ms"), inference_ms=inf.get("ms"))
    except Stop as e:
        return finish(e.verdict, e.reason, **{"app_id": app_id, **e.facts})
    except Exception as e:  # noqa: BLE001 — a crash must still end in a VERDICT, never in a bare traceback
        return finish("FAIL", f"deploy.py crashed: {type(e).__name__}: {str(e)[:200]}", app_id=app_id)


def cmd_preflight():
    try:
        pf = preflight()
    except Stop as e:
        return finish(e.verdict, e.reason)
    print("PREFLIGHT OK " + json.dumps({k: v for k, v in pf.items()}, sort_keys=True))
    return 0


def cmd_status():
    try:
        missing = [k for k in ("DO_API_TOKEN",) if not os.environ.get(k)]
        if missing:
            raise Stop("DID-NOT-RUN", "env not set: DO_API_TOKEN")
        name = os.environ.get("DEMO_APP_NAME", "").strip() or "mars-demo-app"
        s, d = do("GET", "/v2/apps?per_page=200")
        if s != 200:
            raise Stop("DID-NOT-RUN", api_error("cannot list apps", s, d))
        apps = [a for a in d.get("apps") or [] if a.get("spec", {}).get("name") == name]
        if not apps:
            print(f"STATUS no app named {name}")
            return 0
        a = apps[0]
        act = a.get("active_deployment") or {}
        print("STATUS " + json.dumps({"app_id": a["id"], "live_url": a.get("live_url"), "active_phase": act.get("phase"),
                                      "commits": [c.get("source_commit_hash") for c in act.get("services") or []]}))
        return 0
    except Stop as e:
        return finish(e.verdict, e.reason)


def main(argv):
    verbs = {"run": cmd_run, "preflight": cmd_preflight, "status": cmd_status}
    if len(argv) != 2 or argv[1] not in verbs:
        sys.stderr.write(f"deploy.py: REFUSED — expected exactly one verb of {sorted(verbs)}, got {argv[1:]}\n\n{__doc__}")
        return 64
    return verbs[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
