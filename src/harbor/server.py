"""HTTP server — routes, SSE streaming, and static file serving."""

from __future__ import annotations

import http.server
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

from . import config as config_mod
from . import git as git_ops
from . import scanner as scanner_mod

logger = logging.getLogger(__name__)

JOBS_LOCK = threading.Lock()
JOBS = {}

# Authentication token — set at startup by __main__.
# When set (not None), every API and SSE request must include ?token=<value>
# or it gets a 403.  Static assets and the root HTML page are always
# accessible (the HTML page itself carries the token in its URL query).
#
# This protects against CSRF, DNS rebinding, and random local processes
# discovering the port and calling destructive endpoints (discard, etc.).
AUTH_TOKEN: str | None = None

# A job whose SSE consumer never read its "done" event (client disconnect)
# would otherwise sit in JOBS forever.  Sweep entries older than this TTL
# lazily, whenever a new job is started.
JOB_TTL_SECONDS = 3600

MAX_WORKERS = 8

# The UI is a single self-contained page with inline <script>/<style>, so
# 'unsafe-inline' is unavoidable without a build step — but we still forbid
# external resources and cross-origin connections (EventSource/fetch are
# same-origin, which "connect-src 'self'" allows).
CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'"
)

# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------
# Each entry: (HTTP method, compiled regex, handler).
# Handler signature: handler(self, match, parsed_url, body=None)
#   - body is the already-parsed JSON dict for POST; None for GET/DELETE.
# The repo routes use a greedy `(?P<path>.+)` so URL-encoded paths with
# embedded slashes (e.g. /Users/x/work/api) match as a single segment.
# ---------------------------------------------------------------------------
_ROUTES = []


def _route(method, pattern):
    """Register a route handler.  Pattern is matched against the URL path."""
    compiled = re.compile(pattern)

    def decorator(fn):
        _ROUTES.append((method, compiled, fn))
        return fn

    return decorator


@_route("GET", r"^/$")
def _get_index(self, m, parsed, body):
    self._serve_html()


@_route("GET", r"^/static/(?P<name>[^/]+)$")
def _get_static(self, m, parsed, body):
    self._serve_static_file(m.group("name"))


@_route("GET", r"^/favicon\.ico$")
def _get_favicon(self, m, parsed, body):
    """Serve SVG favicon at the /favicon.ico path for broad browser support."""
    self._serve_static_file("favicon.svg")


@_route("GET", r"^/api/repos$")
def _get_repos(self, m, parsed, body):
    self._send_json(200, git_ops.get_repos_status(self.repos))


@_route("GET", r"^/api/roots$")
def _get_roots(self, m, parsed, body):
    config = config_mod.load_config(self.config_path)
    roots = config_mod.resolve_roots([], config)
    # Renamed `l` → `label` to clear the E741 lint.
    self._send_json(200, [{"path": p, "label": label} for p, label in roots])


@_route("GET", r"^/api/browse$")
def _get_browse(self, m, parsed, body):
    qs = parse_qs(parsed.query)
    dir_path = os.path.expanduser((qs.get("path") or [os.path.expanduser("~")])[0])
    self._send_json(200, _browse_dir(dir_path))


@_route("GET", r"^/api/stream$")
def _get_stream(self, m, parsed, body):
    self._stream(parse_qs(parsed.query))


@_route("GET", r"^/api/repo/(?P<path>.+)/diff$")
def _get_repo_diff(self, m, parsed, body):
    path = unquote(m.group("path"))
    diff = git_ops.get_diff(path, self.repos)
    if diff is None:
        self.send_error(404)
    else:
        self._send_json(200, diff)


@_route("POST", r"^/api/pull-all$")
def _post_pull_all(self, m, parsed, body):
    job_id = start_pull_all_job(self.repos)
    self._send_json(200, {"job_id": job_id})


@_route("POST", r"^/api/roots$")
def _post_roots(self, m, parsed, body):
    path = (body.get("path") or "").strip()
    label = (body.get("label") or "").strip() or os.path.basename(os.path.expanduser(path))
    if not path:
        self._send_json(400, {"ok": False, "error": "path is required"})
        return
    config = config_mod.load_config(self.config_path) or {}
    roots = config.setdefault("roots", [])
    # Avoid duplicate paths
    if any(r["path"] == path for r in roots):
        self._send_json(409, {"ok": False, "error": "path already exists"})
        return
    roots.append({"path": path, "label": label})
    config_mod.save_config(self.config_path, config)
    self._rescan()
    self._send_json(200, {"ok": True, "path": path, "label": label})


@_route("POST", r"^/api/rescan$")
def _post_rescan(self, m, parsed, body):
    roots, repos = self._rescan()
    self._send_json(200, {
        "ok": True,
        "roots": [{"path": p, "label": label} for p, label in roots],
        "count": len(repos),
        "min_depth": self.min_depth,
        "max_depth": self.max_depth,
    })


@_route("POST", r"^/api/repo/(?P<path>.+)/action$")
def _post_repo_action(self, m, parsed, body):
    path = unquote(m.group("path"))
    code, obj = git_ops.do_action(path, body.get("action"), self.repos)
    # T-022: attach the repo's post-action status so the frontend can update
    # just this card instead of re-fetching every repo.  The status must be
    # sampled AFTER the action — discard/stash change state drastically.
    if code == 200:
        repo = self.repos.get(path)
        if repo is not None:
            obj["status"] = git_ops.repo_status(repo)
    self._send_json(code, obj)


@_route("DELETE", r"^/api/roots/(?P<path>.+)$")
def _delete_root(self, m, parsed, body):
    path = unquote(m.group("path"))
    config = config_mod.load_config(self.config_path)
    if not config or "roots" not in config:
        self._send_json(404, {"ok": False, "error": "no roots configured"})
        return
    before = len(config["roots"])
    # Match by path (not label) so the contract is symmetric with POST
    # /api/roots which dedupes by path.
    config["roots"] = [r for r in config["roots"] if r.get("path", "") != path]
    if len(config["roots"]) == before:
        self._send_json(404, {"ok": False, "error": f"root '{path}' not found"})
        return
    config_mod.save_config(self.config_path, config)
    self._rescan()
    self._send_json(200, {"ok": True, "path": path})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _browse_dir(path):
    """Return a list of subdirectories for the given path."""
    path = os.path.expanduser(path)
    result = {"path": path, "parent": os.path.dirname(path), "dirs": []}
    try:
        path = os.path.realpath(path)
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                result["dirs"].append({"name": name, "path": full})
    except (OSError, PermissionError):
        pass
    return result


def _sweep_stale_jobs():
    """Drop jobs older than JOB_TTL_SECONDS.  Returns how many were swept."""
    now = time.monotonic()
    with JOBS_LOCK:
        stale = [jid for jid, job in JOBS.items() if now - job.get("created", now) > JOB_TTL_SECONDS]
        for jid in stale:
            JOBS.pop(jid, None)
    if stale:
        logger.info("swept %d stale job(s) from JOBS", len(stale))
    return len(stale)


def start_pull_all_job(repos):
    """Start a background pull-all job and return its job_id."""
    _sweep_stale_jobs()
    job_id = uuid.uuid4().hex
    q = queue.Queue()
    with JOBS_LOCK:
        JOBS[job_id] = {"queue": q, "created": time.monotonic()}

    def worker():
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = [ex.submit(git_ops.pull_one, r, q) for r in repos.values()]
                for f in futures:
                    # Surface worker exceptions as SSE events so the UI can
                    # unblock the progress bar; otherwise a single failure
                    # would skip the "done" event and leak this job's queue.
                    try:
                        f.result()
                    except Exception as exc:
                        q.put({"error": str(exc), "repo": "<unknown>"})
        finally:
            # Always emit "done" so the SSE consumer can clean up the job
            # entry, even when something above raised before the loop
            # finished.
            q.put({"done": True})

    threading.Thread(target=worker, daemon=True).start()
    return job_id


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the Harbor web UI."""

    repos: ClassVar[dict] = {}
    html_path: ClassVar[str] = ""
    static_dir: ClassVar[str] = ""
    config_path: ClassVar[str] = ""
    min_depth: ClassVar[int] = 1
    max_depth: ClassVar[int] = 5

    # CLI args take priority and aren't hot-reloaded from config file.
    cli_min_depth: ClassVar[int | None] = None
    cli_max_depth: ClassVar[int | None] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_origin(self):
        """Return True if the request may proceed; False if 403 was sent.

        Cross-origin mutation requests are rejected.  A missing Origin/Referer
        (e.g. curl from the same host) is allowed — same-origin policy is
        enforced by the browser, not by us.  This is a defense-in-depth check
        applied centrally to every POST/DELETE in ``_dispatch``.
        """
        origin = self.headers.get("Origin") or self.headers.get("Referer", "")
        host = self.headers.get("Host", "")
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https"):
            self._send_json(403, {"ok": False, "error": "bad origin"})
            return False
        if parsed.netloc and host and parsed.netloc != host:
            self._send_json(403, {"ok": False, "error": "cross-origin blocked"})
            return False
        return True

    def _serve_html(self):
        try:
            with open(self.html_path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "index.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static_file(self, name):
        """Serve a file from the static directory by filename.

        Only simple filenames are allowed (no subdirectories) to prevent
        directory traversal attacks.
        """
        if not name or "/" in name or ".." in name or name.startswith("."):
            self.send_error(400)
            return
        full = os.path.join(type(self).static_dir, name)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        ext = os.path.splitext(name)[1].lower()
        ctype = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".css": "text/css",
            ".js": "application/javascript",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, qs):
        job_id = (qs.get("job") or [None])[0]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = job["queue"]
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                try:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                continue
            try:
                self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if item.get("done"):
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
                return

    # ------------------------------------------------------------------
    # Re-scan
    # ------------------------------------------------------------------

    def _rescan(self):
        """Re-scan all roots and update Handler.repos.

        Also reloads min_depth / max_depth from the config file (unless
        overridden by CLI arguments at startup).
        """
        config = config_mod.load_config(self.config_path) or {}
        # Reload depth settings from config (CLI args remain supreme)
        if type(self).cli_min_depth is None:
            type(self).min_depth = config.get("min_depth", type(self).min_depth)
        if type(self).cli_max_depth is None:
            type(self).max_depth = config.get("max_depth", type(self).max_depth)
        roots = config_mod.resolve_roots([], config)
        new_repos = scanner_mod.scan_roots(roots, min_depth=self.min_depth, max_depth=self.max_depth)
        type(self).repos = new_repos
        return roots, new_repos

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _is_static_or_root(self, parsed) -> bool:
        """Return True if the request targets the root page or static assets.

        These paths are exempt from token auth so the browser can load the
        page normally (the token is in the URL query string read by JS).
        """
        path = parsed.path
        return path == "/" or path == "/index.html" or path.startswith("/static/") or path == "/favicon.ico"

    def _check_token(self, parsed) -> bool:
        """Return True if the request has a valid auth token (or none is needed).

        Sends a 403 and returns False when auth is required but missing/wrong.
        """
        if AUTH_TOKEN is None:
            return True
        if self._is_static_or_root(parsed):
            return True
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [None])[0]
        # Also accept token in POST body for routes that read JSON bodies.
        if not token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
        if token != AUTH_TOKEN:
            self._send_json(403, {"ok": False, "error": "invalid or missing token"})
            return False
        return True

    def _dispatch(self, method, parsed, body=None):
        # Token check is centralized here — every non-static route is protected.
        if not self._check_token(parsed):
            return
        # Origin check is centralized here so every mutating route (actions,
        # pull-all, rescan, roots) is protected, not just one of them.
        if method in ("POST", "DELETE") and not self._check_origin():
            return
        for m_method, pattern, handler in _ROUTES:
            if m_method != method:
                continue
            m = pattern.match(parsed.path)
            if m:
                handler(self, m, parsed, body)
                return
        self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return None  # caller turns this into a 400

    def do_GET(self):
        self._dispatch("GET", urlparse(self.path))

    def do_POST(self):
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        self._dispatch("POST", urlparse(self.path), body)

    def do_DELETE(self):
        self._dispatch("DELETE", urlparse(self.path))

    def log_message(self, fmt, *args):
        # T-011: never log the full URL which may contain the auth token.
        # We log only the path portion (without query string).
        msg = fmt % args
        # BaseHTTPRequestHandler formats "GET /path?token=xxx HTTP/1.1" 200 -
        # Strip the query string to avoid leaking tokens to logs.
        import re as _re
        cleaned = _re.sub(r"( \S+?)\?\S+? ", r"\1 ", msg, count=1)
        logger.debug("%s - %s", self.address_string(), cleaned)
