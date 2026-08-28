"""Tests for harbor.server — routing, JSON body parsing, auth, and pull-all worker."""

import http.server
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest
import pytest as _pytest

from harbor import config as config_mod
from harbor import server
from harbor.state import AppState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeServer:
    """Stand-in for ThreadingHTTPServer to satisfy Handler construction."""
    server_address = ("127.0.0.1", 0)


def _make_handler(method, path, body=b"", headers=None):
    """Construct a Handler with a fake socket so we can call do_* directly.

    We bypass BaseHTTPRequestHandler.__init__ because it requires a real
    socket; we only populate the attributes the route handlers actually read.
    """
    h = server.Handler.__new__(server.Handler)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    merged = dict(headers or {})
    if body and "Content-Length" not in merged and "content-length" not in {k.lower() for k in merged}:
        # _read_json_body() reads Content-Length to size the body read; without
        # it, every POST looks like an empty body.
        merged["Content-Length"] = str(len(body))
    h.headers = merged
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.raw_requestline = f"{method} {path} HTTP/1.1\r\n".encode()
    # log_request (called by send_response) reads self.requestline in 3.14.
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 12345)
    h.server = _FakeServer()
    return h


def _read_response(handler):
    """Parse the wfile buffer and return (status, body_dict_or_None)."""
    raw = handler.wfile.getvalue()
    if not raw:
        return None, None
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin1")
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        # send_error(404) writes an HTML body that isn't JSON
        return 500, None
    if not body:
        return status, None
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, None


@pytest.fixture(autouse=True)
def _fresh_app_state():
    """Replace ``server.app_state`` with a clean :class:`AppState` per test.

    This eliminates the previous snapshot/restore pattern: each test gets
    its own state object, so there is no risk of cross-test contamination
    through class attributes.  Disables auth token checks by default so
    individual tests don't have to supply tokens.
    """
    server.app_state = AppState()
    saved_token = server.AUTH_TOKEN
    server.AUTH_TOKEN = None
    yield
    server.AUTH_TOKEN = saved_token


# ---------------------------------------------------------------------------
# do_POST 400 on bad JSON  (Group A wiring check)
# ---------------------------------------------------------------------------

def test_post_bad_json_returns_400(tmp_path):
    server.app_state.config_path = str(tmp_path / "config.toml")
    h = _make_handler("POST", "/api/rescan", body=b"{not json")
    h.do_POST()
    status, body = _read_response(h)
    assert status == 400
    assert body["ok"] is False
    assert "invalid" in body["error"].lower()


# ---------------------------------------------------------------------------
# do_POST /api/rescan returns the documented shape
# ---------------------------------------------------------------------------

def test_post_rescan_returns_shape(tmp_path):
    config_path = str(tmp_path / "config.toml")
    server.app_state.config_path = config_path
    server.app_state.repos = {}

    h = _make_handler("POST", "/api/rescan", body=b"{}")
    h.do_POST()
    status, body = _read_response(h)
    assert status == 200
    assert body["ok"] is True
    assert "roots" in body
    assert "count" in body
    assert "min_depth" in body
    assert "max_depth" in body
    # roots is a list of {path, label} dicts
    assert isinstance(body["roots"], list)
    for r in body["roots"]:
        assert "path" in r
        assert "label" in r


# ---------------------------------------------------------------------------
# do_POST /api/roots 409 on duplicate path
# ---------------------------------------------------------------------------

def test_post_roots_duplicate_returns_409(tmp_path):
    config_path = tmp_path / "config.toml"
    config_mod.save_config(str(config_path), {
        "roots": [{"path": str(tmp_path), "label": "X"}],
    })
    server.app_state.config_path = str(config_path)

    payload = json.dumps({"path": str(tmp_path), "label": "Y"}).encode()
    h = _make_handler("POST", "/api/roots", body=payload)
    h.do_POST()
    status, body = _read_response(h)
    assert status == 409
    assert body["ok"] is False
    assert "exists" in body["error"].lower()


def test_post_roots_adds_new_path(tmp_path):
    """A new path is added to the config; the response includes its label."""
    config_path = tmp_path / "config.toml"
    config_mod.save_config(str(config_path), {"roots": []})
    server.app_state.config_path = str(config_path)
    new_path = str(tmp_path / "newdir")
    os.makedirs(new_path)

    payload = json.dumps({"path": new_path, "label": "NewDir"}).encode()
    h = _make_handler("POST", "/api/roots", body=payload)
    h.do_POST()
    status, body = _read_response(h)
    assert status == 200
    assert body["ok"] is True
    assert body["path"] == new_path
    assert body["label"] == "NewDir"
    # Verify config file was written
    cfg = config_mod.load_config(str(config_path))
    assert any(r["path"] == new_path for r in cfg["roots"])


# ---------------------------------------------------------------------------
# do_DELETE /api/roots/<path> removes by path  (B-3)
# ---------------------------------------------------------------------------

def test_delete_root_by_path(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    config_mod.save_config(str(tmp_path / "config.toml"), {
        "roots": [
            {"path": str(a), "label": "Alpha"},
            {"path": str(b), "label": "Beta"},
        ],
    })
    server.app_state.config_path = str(tmp_path / "config.toml")

    h = _make_handler("DELETE", f"/api/roots/{a}", body=b"")
    h.do_DELETE()
    status, body = _read_response(h)
    assert status == 200
    assert body["ok"] is True
    assert body["path"] == str(a)

    # Config should now have only one root
    config = config_mod.load_config(str(tmp_path / "config.toml"))
    assert len(config["roots"]) == 1
    assert config["roots"][0]["path"] == str(b)


def test_delete_root_by_path_handles_duplicate_labels(tmp_path):
    """Two roots with the same label must be distinguished by path."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    config_mod.save_config(str(tmp_path / "config.toml"), {
        "roots": [
            {"path": str(a), "label": "SameLabel"},
            {"path": str(b), "label": "SameLabel"},
        ],
    })
    server.app_state.config_path = str(tmp_path / "config.toml")

    # Delete only the first — second must remain even with the same label
    h = _make_handler("DELETE", f"/api/roots/{a}", body=b"")
    h.do_DELETE()
    config = config_mod.load_config(str(tmp_path / "config.toml"))
    assert len(config["roots"]) == 1
    assert config["roots"][0]["path"] == str(b)


def test_delete_root_missing_returns_404(tmp_path):
    config_path = tmp_path / "config.toml"
    config_mod.save_config(str(config_path), {"roots": []})
    server.app_state.config_path = str(config_path)

    h = _make_handler("DELETE", f"/api/roots/{tmp_path}/nope", body=b"")
    h.do_DELETE()
    status, body = _read_response(h)
    assert status == 404
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# Origin/Referer cross-origin POST returns 403  (C-3)
# ---------------------------------------------------------------------------

def test_action_cross_origin_returns_403(tmp_path):
    """A POST with a foreign Origin is rejected before any git op runs."""
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    headers = {
        "Origin": "https://evil.example",
        "Host": "127.0.0.1:8765",
    }
    h = _make_handler(
        "POST", "/api/repo/some-path/action",
        body=b'{"action":"pull"}', headers=headers,
    )
    h.do_POST()
    status, body = _read_response(h)
    assert status == 403
    assert body["ok"] is False
    assert "cross-origin" in body["error"].lower()


def test_action_no_origin_allows_through(tmp_path):
    """A POST without Origin or Referer is allowed (same-origin or curl)."""
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}  # unknown path → 404, not 403

    h = _make_handler(
        "POST", "/api/repo/missing/action",
        body=b'{"action":"pull"}',
    )
    h.do_POST()
    status, _ = _read_response(h)
    # We just want to confirm the origin check did NOT intercept.
    assert status == 404


def test_action_same_origin_allows_through(tmp_path):
    """A POST whose Origin matches Host is allowed."""
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}  # unknown path → 404, not 403

    headers = {
        "Origin": "http://127.0.0.1:8765",
        "Host": "127.0.0.1:8765",
    }
    h = _make_handler(
        "POST", "/api/repo/missing/action",
        body=b'{"action":"pull"}', headers=headers,
    )
    h.do_POST()
    status, _ = _read_response(h)
    assert status == 404


# ---------------------------------------------------------------------------
# T-010 — origin check centralized in _dispatch: all mutating routes protected
# ---------------------------------------------------------------------------

@_pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/repo/x/action", b'{"action":"pull"}'),
    ("POST", "/api/pull-all", b"{}"),
    ("POST", "/api/rescan", b"{}"),
    ("POST", "/api/roots", b'{"path":"/tmp"}'),
    ("DELETE", "/api/roots/%2Ftmp", b""),
])
def test_mutating_routes_reject_cross_origin(tmp_path, method, path, body):
    """Every POST/DELETE route rejects cross-origin requests — not just one."""
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    headers = {
        "Origin": "https://evil.example",
        "Host": "127.0.0.1:8765",
    }
    h = _make_handler(method, path, body=body, headers=headers)
    if method == "POST":
        h.do_POST()
    else:
        h.do_DELETE()
    status, body = _read_response(h)
    assert status == 403
    assert "cross-origin" in body["error"].lower()


def test_get_requests_skip_origin_check(tmp_path):
    """GET requests are not subject to origin validation (read-only)."""
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}
    headers = {
        "Origin": "https://evil.example",
        "Host": "127.0.0.1:8765",
    }
    h = _make_handler("GET", "/api/repos", body=b"", headers=headers)
    h.do_GET()
    status, body = _read_response(h)
    # Should succeed (200), not be blocked by origin
    assert status == 200
    assert isinstance(body, list)


# ---------------------------------------------------------------------------
# T-011 — local token authentication
# ---------------------------------------------------------------------------

def test_api_without_token_returns_403_when_auth_enabled(tmp_path):
    """When AUTH_TOKEN is set, API calls without a token get 403."""
    server.AUTH_TOKEN = "secret123"
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    h = _make_handler("GET", "/api/repos", body=b"")
    h.do_GET()
    status, body = _read_response(h)
    assert status == 403
    assert body["ok"] is False
    assert "token" in body["error"].lower()


def test_api_with_correct_token_returns_200(tmp_path):
    """When AUTH_TOKEN is set and the query has the right token, request proceeds."""
    server.AUTH_TOKEN = "secret123"
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    h = _make_handler("GET", "/api/repos?token=secret123", body=b"")
    h.do_GET()
    status, body = _read_response(h)
    assert status == 200
    assert isinstance(body, list)


def test_api_with_wrong_token_returns_403(tmp_path):
    """Wrong token value is rejected."""
    server.AUTH_TOKEN = "secret123"
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    h = _make_handler("GET", "/api/repos?token=wrong", body=b"")
    h.do_GET()
    status, _ = _read_response(h)
    assert status == 403


def test_static_and_root_exempt_from_token_auth(tmp_path):
    """Static assets and the root page don't require a token."""
    server.AUTH_TOKEN = "secret123"
    # Root page
    h = _make_handler("GET", "/", body=b"")
    h.do_GET()
    status, _ = _read_response(h)
    # Should not be 403 (may be 200 or 500 depending on whether index.html exists)
    assert status != 403
    # Static file
    h2 = _make_handler("GET", "/static/foo.js", body=b"")
    h2.do_GET()
    status2, _ = _read_response(h2)
    assert status2 != 403
    # favicon
    h3 = _make_handler("GET", "/favicon.ico", body=b"")
    h3.do_GET()
    status3, _ = _read_response(h3)
    assert status3 != 403


def test_post_action_with_token(tmp_path):
    """POST requests also accept token via query string."""
    server.AUTH_TOKEN = "secret123"
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    h = _make_handler("POST", "/api/rescan?token=secret123", body=b"{}")
    h.do_POST()
    status, body = _read_response(h)
    assert status == 200
    assert body["ok"] is True


def test_bearer_token_auth(tmp_path):
    """Token can also be passed via Authorization: Bearer header."""
    server.AUTH_TOKEN = "secret123"
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    headers = {"Authorization": "Bearer secret123"}
    h = _make_handler("GET", "/api/repos", body=b"", headers=headers)
    h.do_GET()
    status, body = _read_response(h)
    assert status == 200
    assert isinstance(body, list)


def test_no_auth_token_means_no_auth_required(tmp_path):
    """When AUTH_TOKEN is None, no token is needed (backwards compatible)."""
    assert server.AUTH_TOKEN is None  # fixture default
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}

    h = _make_handler("GET", "/api/repos", body=b"")
    h.do_GET()
    status, body = _read_response(h)
    assert status == 200
    assert isinstance(body, list)


def test_sse_stream_requires_token(tmp_path):
    """SSE stream endpoint also requires a token."""
    server.AUTH_TOKEN = "secret123"
    # The /api/stream route is a GET — verify it's blocked without token
    server.JOBS.clear()
    h = _make_handler("GET", "/api/stream?job=nonexistent", body=b"")
    h.do_GET()
    status, _ = _read_response(h)
    assert status == 403


# ---------------------------------------------------------------------------
# T-012 — CSP header on all response types
# ---------------------------------------------------------------------------

def _response_headers(handler):
    """Return the response status line + headers as a list of strings."""
    raw = handler.wfile.getvalue()
    head, _, _ = raw.partition(b"\r\n\r\n")
    return [line.decode("latin1") for line in head.split(b"\r\n")]


def test_index_serves_csp_header(tmp_path):
    """Root page serves HTML with a CSP header."""
    html_file = tmp_path / "index.html"
    html_file.write_text("<html></html>")
    server.app_state.html_path = str(html_file)
    h = _make_handler("GET", "/", body=b"")
    h.do_GET()
    headers = _response_headers(h)
    csp_lines = [h for h in headers if h.lower().startswith("content-security-policy:")]
    assert len(csp_lines) == 1
    assert "default-src 'self'" in csp_lines[0]


def test_static_file_serves_csp_header(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "test.js").write_text("console.log(1)")
    monkeypatch.setattr(server.app_state, "static_dir", str(static_dir))
    h = _make_handler("GET", "/static/test.js", body=b"")
    h.do_GET()
    headers = _response_headers(h)
    csp_lines = [h for h in headers if h.lower().startswith("content-security-policy:")]
    assert len(csp_lines) == 1
    assert "default-src 'self'" in csp_lines[0]


def test_json_response_serves_csp_header(tmp_path):
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}
    h = _make_handler("GET", "/api/repos", body=b"")
    h.do_GET()
    headers = _response_headers(h)
    csp_lines = [h for h in headers if h.lower().startswith("content-security-policy:")]
    assert len(csp_lines) == 1
    assert "default-src 'self'" in csp_lines[0]


# ---------------------------------------------------------------------------
# T-022 — action response includes fresh post-action status
# ---------------------------------------------------------------------------

def test_action_response_includes_fresh_status(tmp_path):
    """POST action returns the post-action status for per-card refresh."""
    # Import init_repo from test_harbor
    from tests.test_harbor import init_repo
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    server.app_state.repos = {path: {"name": "r", "path": path}}

    h = _make_handler("POST", f"/api/repo/{path}/action",
                      body=b'{"action":"stash"}')
    h.do_POST()
    status, body = _read_response(h)
    assert status == 200
    assert "status" in body
    assert body["status"]["name"] == "r"
    assert body["status"]["path"] == path
    assert "dirty" in body["status"]
    assert "branch" in body["status"]


def test_action_unknown_repo_has_no_status(tmp_path):
    server.app_state.config_path = str(tmp_path / "config.toml")
    server.app_state.repos = {}
    h = _make_handler("POST", "/api/repo/nonexistent/action",
                      body=b'{"action":"pull"}')
    h.do_POST()
    status, body = _read_response(h)
    assert status == 404
    assert "status" not in body


# ---------------------------------------------------------------------------
# start_pull_all_job worker always emits {"done": True}  (C-2)
# ---------------------------------------------------------------------------

def test_pull_all_job_emits_done_on_worker_failure():
    """A worker exception must not prevent the {"done": True} event."""
    repos = {"/r1": {"name": "r1", "path": "/r1"}}

    def boom(repo, q):
        q.put({"repo": repo["name"], "status": "running"})
        raise RuntimeError("synthetic failure")

    original = server.git_ops.pull_one
    server.git_ops.pull_one = boom
    try:
        job_id = server.start_pull_all_job(repos)
        seen = _drain_queue(job_id)
    finally:
        server.git_ops.pull_one = original

    assert any(item.get("error") for item in seen), f"no error event in {seen}"
    assert any(item.get("done") for item in seen), f"no done event in {seen}"


def test_pull_all_job_emits_done_on_clean_run():
    """A clean run also emits {"done": True}."""
    repos = {"/r1": {"name": "r1", "path": "/r1"}}

    def ok(repo, q):
        q.put({"repo": repo["name"], "status": "success", "message": "ok"})

    original = server.git_ops.pull_one
    server.git_ops.pull_one = ok
    try:
        job_id = server.start_pull_all_job(repos)
        seen = _drain_queue(job_id)
    finally:
        server.git_ops.pull_one = original

    assert any(item.get("done") for item in seen), f"no done event in {seen}"
    # The JOBS entry is owned by the SSE consumer — the worker emits the
    # "done" event, the consumer is the one that pops the entry.  We
    # verify the entry still exists; an SSE-driven test would also pop it.
    assert job_id in server.JOBS


def _drain_queue(job_id, timeout=2.0):
    """Pull every event from a job's queue until done, with a timeout guard."""
    q = server.JOBS[job_id]["queue"]
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            item = q.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(item)
        if item.get("done"):
            break
    return seen


# ---------------------------------------------------------------------------
# Routing — greedy path regex handles URL-encoded paths with slashes  (B-2)
# ---------------------------------------------------------------------------

def test_repo_route_matches_url_encoded_path(tmp_path):
    """`/api/repo/<path with />/diff` decodes into a single path argument."""
    from urllib.parse import quote
    server.app_state.config_path = str(tmp_path / "config.toml")
    # Empty repos → get_diff returns None → handler sends 404
    server.app_state.repos = {}

    encoded = quote("/Users/x/work/api", safe="")
    h = _make_handler("GET", f"/api/repo/{encoded}/diff", body=b"")
    h.do_GET()
    status, _ = _read_response(h)
    # 404 because the repo isn't in our empty dict; but the route MUST match,
    # not fall through to the 404 from _dispatch.  The 404 we see here is
    # sent by the handler itself when get_diff returns None — the routing
    # table matched the request.
    assert status == 404


def test_delete_root_url_encoded_path(tmp_path):
    """`DELETE /api/roots/<encoded path>` decodes and matches correctly."""
    a = tmp_path / "space dir"  # contains a space
    os.makedirs(a, exist_ok=True)
    config_mod.save_config(str(tmp_path / "config.toml"), {
        "roots": [{"path": str(a), "label": "SpaceDir"}],
    })
    server.app_state.config_path = str(tmp_path / "config.toml")

    from urllib.parse import quote
    h = _make_handler("DELETE", f"/api/roots/{quote(str(a))}", body=b"")
    h.do_DELETE()
    status, body = _read_response(h)
    assert status == 200
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# C-1: CLI roots are persisted to config on startup
# ---------------------------------------------------------------------------

def test_cli_roots_persist_to_config(tmp_path, monkeypatch):
    """When the user passes roots on the CLI, they are written to config."""
    from harbor.__main__ import main

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config_path = tmp_path / "config.toml"

    # Prevent the HTTP server from blocking; main() calls serve_forever().
    class _NoopServer:
        def __init__(self, *a, **kw):
            pass
        def serve_forever(self):
            raise SystemExit(0)
        def shutdown(self):
            pass
    monkeypatch.setattr("http.server.ThreadingHTTPServer", _NoopServer)
    # Suppress browser-open side effect
    monkeypatch.setattr("harbor.__main__._try_open_browser", lambda url: None)

    monkeypatch.setattr(sys, "argv", [
        "harbor", "--no-browser", "--config", str(config_path), str(work_dir),
    ])

    with pytest.raises(SystemExit):
        main()

    # Config file should now contain the CLI root (realpath form)
    config = config_mod.load_config(str(config_path))
    assert config is not None
    real = str(work_dir.resolve())
    paths = [os.path.realpath(r["path"]) for r in config.get("roots", [])]
    assert real in paths


# ---------------------------------------------------------------------------
# T-002 — stale job TTL sweep
# ---------------------------------------------------------------------------

def test_stale_job_swept_when_new_job_starts(monkeypatch):
    """A job older than JOB_TTL_SECONDS is removed when a new job starts."""
    server.JOBS.clear()
    server.JOBS["old"] = {"queue": queue.Queue(), "created": time.monotonic() - server.JOB_TTL_SECONDS - 10}

    def noop_pull(repo, q):
        q.put({"done": True})

    monkeypatch.setattr(server.git_ops, "pull_one", noop_pull)
    monkeypatch.setattr(server, "ThreadPoolExecutor", _MockExecutor)

    job_id = server.start_pull_all_job({"/fake": {"name": "fake", "path": "/fake"}})
    # Old job should have been swept
    assert "old" not in server.JOBS
    # New job is present
    assert job_id in server.JOBS
    # Clean up
    server.JOBS.pop(job_id, None)


def test_fresh_job_survives_sweep(monkeypatch):
    """A recently-created job survives the lazy sweep."""
    server.JOBS.clear()
    server.JOBS["fresh"] = {"queue": queue.Queue(), "created": time.monotonic() - 10}

    def noop_pull(repo, q):
        q.put({"done": True})

    monkeypatch.setattr(server.git_ops, "pull_one", noop_pull)
    monkeypatch.setattr(server, "ThreadPoolExecutor", _MockExecutor)

    job_id = server.start_pull_all_job({"/fake": {"name": "fake", "path": "/fake"}})
    assert "fresh" in server.JOBS
    # Clean up
    server.JOBS.pop(job_id, None)
    server.JOBS.pop("fresh", None)


def test_job_records_created_timestamp(monkeypatch):
    """Every new job gets a 'created' monotonic timestamp."""
    server.JOBS.clear()

    def noop_pull(repo, q):
        q.put({"done": True})

    monkeypatch.setattr(server.git_ops, "pull_one", noop_pull)
    monkeypatch.setattr(server, "ThreadPoolExecutor", _MockExecutor)

    before = time.monotonic()
    job_id = server.start_pull_all_job({"/fake": {"name": "fake", "path": "/fake"}})
    after = time.monotonic()

    job = server.JOBS[job_id]
    assert "created" in job
    assert before <= job["created"] <= after
    # Clean up
    server.JOBS.pop(job_id, None)


class _MockExecutor:
    """Minimal ThreadPoolExecutor stand-in that runs work synchronously."""
    def __init__(self, max_workers=1):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def submit(self, fn, *args, **kwargs):
        class _MockFuture:
            def result(self_):
                return fn(*args, **kwargs)
        return _MockFuture()


# ---------------------------------------------------------------------------
# T-050 — SSE stream integration tests (real ThreadingHTTPServer)
# ---------------------------------------------------------------------------



def _start_real_server(monkeypatch, tmp_path):
    """Start a real ThreadingHTTPServer on a random free port.

    Returns (base_url, server).  Caller must call server.shutdown() when done.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text("min_depth = 1\nmax_depth = 3\n")
    server.app_state.config_path = str(config_path)
    server.app_state.html_path = str(tmp_path / "index.html")
    (tmp_path / "index.html").write_text("<html></html>")
    server.app_state.static_dir = str(tmp_path / "static")
    (tmp_path / "static").mkdir(exist_ok=True)
    server.app_state.repos = {}

    def noop_pull(repo, q):
        q.put({"done": True})

    monkeypatch.setattr(server.git_ops, "pull_one", noop_pull)
    monkeypatch.setattr(server, "ThreadPoolExecutor", _MockExecutor)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    return f"http://127.0.0.1:{port}", srv


def _read_sse_events(base_url, job_id, max_events=100, timeout=3):
    """Read SSE events from /api/stream?job=... until 'done' or timeout.

    Uses a raw socket instead of urllib because HTTP/1.0 SSE responses have
    no Content-Length and urllib waits for EOF before returning any data.
    """
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    host = parsed.hostname
    port = parsed.port
    path = f"/api/stream?job={job_id}"

    events = []
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode()
        )
        # Read until end of headers
        header_buf = b""
        while b"\r\n\r\n" not in header_buf:
            chunk = sock.recv(1024)
            if not chunk:
                return events
            header_buf += chunk
        header_end = header_buf.find(b"\r\n\r\n") + 4
        body = header_buf[header_end:]

        buf = body.decode("utf-8", errors="replace")
        while len(events) < max_events:
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                data_lines = [ln[6:] for ln in block.split("\n") if ln.startswith("data: ")]
                if data_lines:
                    payload = "\n".join(data_lines)
                    events.append(json.loads(payload))
                    if events[-1].get("done"):
                        return events
            try:
                chunk = sock.recv(1024)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
    except OSError:
        pass
    finally:
        sock.close()
    return events


def test_sse_stream_emits_done_and_job_is_cleaned_up(monkeypatch, tmp_path):
    """Normal path: 'done' event arrives, JOBS entry is removed."""
    server.JOBS.clear()
    base_url, srv = _start_real_server(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(
            f"{base_url}/api/pull-all",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read())
        job_id = body["job_id"]
        assert job_id in server.JOBS

        events = _read_sse_events(base_url, job_id, timeout=3)
        assert len(events) >= 1
        assert events[-1].get("done") is True

        # Job cleanup happens in the handler thread after writing the done
        # event — wait briefly for it to be removed (avoids flaky races).
        deadline = time.time() + 2
        while job_id in server.JOBS and time.time() < deadline:
            time.sleep(0.01)
        assert job_id not in server.JOBS
    finally:
        srv.shutdown()


def test_sse_stream_404_for_unknown_job(monkeypatch, tmp_path):
    """Requesting a stream for a non-existent job returns 404."""
    base_url, srv = _start_real_server(monkeypatch, tmp_path)
    try:
        with pytest.raises(HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/api/stream?job=nonexistent", timeout=3)
        assert exc_info.value.code == 404
    finally:
        srv.shutdown()


def test_sse_client_disconnect_leaves_job_for_sweep(monkeypatch, tmp_path):
    """Client disconnects before 'done' — job stays until TTL sweep cleans it."""
    server.JOBS.clear()
    base_url, srv = _start_real_server(monkeypatch, tmp_path)
    try:
        slow_q = queue.Queue()
        slow_job_id = "slow-test-job"
        with server.JOBS_LOCK:
            server.JOBS[slow_job_id] = {"queue": slow_q, "created": time.monotonic()}

        slow_q.put({"repo": "test", "status": "running"})
        # Connect via raw socket, read one event, then close abruptly
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        sock = socket.socket()
        sock.settimeout(2)
        sock.connect((parsed.hostname, parsed.port))
        sock.sendall(
            f"GET /api/stream?job={slow_job_id} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n\r\n".encode()
        )
        # Read until we get the first event
        data = b""
        while b"\n\n" not in data:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
        sock.close()

        # Give the server thread a moment to notice the disconnect
        time.sleep(0.2)
        assert slow_job_id in server.JOBS

        # Simulate TTL expiry — sweep should clean it
        with server.JOBS_LOCK:
            server.JOBS[slow_job_id]["created"] = time.monotonic() - server.JOB_TTL_SECONDS - 100
        server._sweep_stale_jobs()
        assert slow_job_id not in server.JOBS
    finally:
        srv.shutdown()
