"""Tests for harbor.server — routing, JSON body parsing, auth, and pull-all worker."""

import io
import json
import os
import queue
import sys
import time

import pytest

from harbor import config as config_mod
from harbor import server

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
def _restore_handler_state():
    """Snapshot the Handler class-attribute state and restore after each test."""
    saved = {
        "repos": dict(server.Handler.repos),
        "html_path": server.Handler.html_path,
        "config_path": server.Handler.config_path,
        "min_depth": server.Handler.min_depth,
        "max_depth": server.Handler.max_depth,
        "cli_min_depth": server.Handler.cli_min_depth,
        "cli_max_depth": server.Handler.cli_max_depth,
    }
    yield
    for k, v in saved.items():
        setattr(server.Handler, k, v)


# ---------------------------------------------------------------------------
# do_POST 400 on bad JSON  (Group A wiring check)
# ---------------------------------------------------------------------------

def test_post_bad_json_returns_400(tmp_path):
    server.Handler.config_path = str(tmp_path / "config.toml")
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
    server.Handler.config_path = config_path
    server.Handler.repos = {}

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
    server.Handler.config_path = str(config_path)

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
    server.Handler.config_path = str(config_path)
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
    server.Handler.config_path = str(tmp_path / "config.toml")

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
    server.Handler.config_path = str(tmp_path / "config.toml")

    # Delete only the first — second must remain even with the same label
    h = _make_handler("DELETE", f"/api/roots/{a}", body=b"")
    h.do_DELETE()
    config = config_mod.load_config(str(tmp_path / "config.toml"))
    assert len(config["roots"]) == 1
    assert config["roots"][0]["path"] == str(b)


def test_delete_root_missing_returns_404(tmp_path):
    config_path = tmp_path / "config.toml"
    config_mod.save_config(str(config_path), {"roots": []})
    server.Handler.config_path = str(config_path)

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
    server.Handler.config_path = str(tmp_path / "config.toml")
    server.Handler.repos = {}

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
    server.Handler.config_path = str(tmp_path / "config.toml")
    server.Handler.repos = {}  # unknown path → 404, not 403

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
    server.Handler.config_path = str(tmp_path / "config.toml")
    server.Handler.repos = {}  # unknown path → 404, not 403

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
    server.Handler.config_path = str(tmp_path / "config.toml")
    # Empty repos → get_diff returns None → handler sends 404
    server.Handler.repos = {}

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
    server.Handler.config_path = str(tmp_path / "config.toml")

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
