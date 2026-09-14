"""HTTP server — routes, SSE streaming, and static file serving."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.server
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict
from urllib.parse import ParseResult, parse_qs, unquote, urlparse

from . import config as config_mod
from . import git as git_ops
from . import scanner as scanner_mod
from .state import HarborApp

logger = logging.getLogger(__name__)


class _Job(TypedDict):
    """A running pull-all job: its SSE event queue and creation time."""

    queue: queue.Queue[dict[str, Any]]
    created: float


# The single application-state object, populated by __main__ at startup and
# reset per test.  Every mutable bit of state (config, auth, jobs, login
# throttling) lives on this one object — see :class:`HarborApp` in state.py.
app_state: HarborApp = HarborApp()

# Signed cookie that authenticates every fetch/SSE request (browsers send it
# same-origin automatically, so the token no longer needs to appear in URLs).
SESSION_COOKIE = "harbor_session"
SESSION_MAX_AGE_DAYS = 30
_SESSION_SEP = "|"  # separator inside the signed payload; never in host:port

# A job whose SSE consumer never read its "done" event (client disconnect)
# would otherwise sit in app_state.jobs forever.  Sweep entries older than
# this TTL lazily, whenever a new job is started.
JOB_TTL_SECONDS = 3600

MAX_WORKERS = 8

# Upper bound on the JSON body accepted from clients.  A malicious or runaway
# client that sends a huge "Content-Length" (or a streamed body) would otherwise
# be read into memory and could OOM the process.  Legitimate requests (a token
# form field, a roots list, a small action payload) are far below this.
MAX_BODY_BYTES = 1024 * 1024

# /login brute-force throttling.  The launch token is a single random secret,
# but a local process could still probe the login endpoint repeatedly; bound how
# many failed attempts a client may make within a rolling window before the
# endpoint refuses (429) and forces a pause.  A successful login resets it.
# Attempt history lives on app_state.login_failures (guarded by login_lock).
LOGIN_WINDOW_SECONDS = 60.0
LOGIN_MAX_FAILURES = 5

# Upper bound on the in-flight event buffer of a pull-all SSE job.  pull_one
# emits a small fixed number of events per repo, but a huge root tree with no
# SSE consumer reading the queue would otherwise pile events up in memory.  A
# bound adds backpressure.  Pull worker threads are daemonized, so a blocked
# producer cannot prevent process exit (it just no longer grows the buffer).
MAX_SSE_QUEUE_EVENTS = 1024

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
_ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []


def _route(
    method: str, pattern: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a route handler.  Pattern is matched against the URL path."""
    compiled = re.compile(pattern)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _ROUTES.append((method, compiled, fn))
        return fn

    return decorator


@_route("GET", r"^/$")
def _get_index(self: Handler, m: re.Match[str], parsed: ParseResult, body: Any) -> None:
    """Serve the app.  A request to ``/?token=<launch>`` performs the one-time
    launch-token exchange: issue a signed session cookie, then redirect to the
    clean ``/`` so the token leaves the address bar and browser history."""
    if app_state.auth_token is not None:
        qs = parse_qs(parsed.query)
        tokens = qs.get("token")
        launch = tokens[0] if tokens else None
        if launch and app_state.session_secret is not None and launch == app_state.auth_token:
            host = self.headers.get("Host", "")
            self.send_response(302)
            self._set_session_cookie(_normalize_hostport(host))
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
    self._serve_html()


@_route("POST", r"^/login$")
def _post_login(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    """Token entry form submission.  On success, issue a session cookie and
    redirect to the clean ``/`` (no token in the URL).

    Failed attempts are rate-limited per client (see ``_login_throttled``) so a
    local process can't brute-force the launch token."""
    # Rate-limit bookkeeping happens before parsing the body so a flood of
    # attempts can't even reach the token comparison.
    if app_state.auth_token is not None and _login_throttled(self.client_address[0]):
        self._send_json(
            429, {"ok": False, "error": "too many attempts, try again later"}
        )
        return
    # Accept both form-encoded and JSON bodies.
    if isinstance(body, dict):
        token = (body.get("token") or "").strip()
    else:
        qs = parse_qs(body or "")
        token = (qs.get("token") or [""])[0].strip()

    if app_state.auth_token is None or token == app_state.auth_token:
        _login_success(self.client_address[0])
        self._send_json(
            200,
            {"ok": True, "redirect": "/"},
            set_cookie=(
                _normalize_hostport(self.headers.get("Host", ""))
                if app_state.session_secret is not None
                else None
            ),
        )
        return
    _login_failure(self.client_address[0])
    self._send_json(401, {"ok": False, "error": "invalid token"})


@_route("GET", r"^/static/(?P<name>[^/]+)$")
def _get_static(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    self._serve_static_file(m.group("name"))


@_route("GET", r"^/favicon\.ico$")
def _get_favicon(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    """Serve SVG favicon at the /favicon.ico path for broad browser support."""
    self._serve_static_file("favicon.svg")


@_route("GET", r"^/api/repos$")
def _get_repos(self: Handler, m: re.Match[str], parsed: ParseResult, body: Any) -> None:
    """Serve the cached status snapshot, recomputed in the background.

    If the snapshot is empty but repos exist — first request before the
    refresher's first tick, or a rescan just invalidated it — compute once on
    demand so the UI never shows an empty list.
    """
    snapshot = app_state.repo_status
    if not snapshot and app_state.repos:
        snapshot = {s["path"]: s for s in git_ops.get_repos_status(app_state.repos)}
        app_state.repo_status = snapshot
    self._send_json(200, list(snapshot.values()))


@_route("GET", r"^/api/roots$")
def _get_roots(self: Handler, m: re.Match[str], parsed: ParseResult, body: Any) -> None:
    # Renamed `l` → `label` to clear the E741 lint.
    self._send_json(200, [{"path": p, "label": label} for p, label in app_state.roots])


@_route("GET", r"^/api/browse$")
def _get_browse(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    qs = parse_qs(parsed.query)
    dir_path = os.path.expanduser((qs.get("path") or [os.path.expanduser("~")])[0])
    self._send_json(200, _browse_dir(dir_path))


@_route("GET", r"^/api/stream$")
def _get_stream(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    self._stream(parse_qs(parsed.query))


@_route("GET", r"^/api/repo/(?P<path>.+)/diff$")
def _get_repo_diff(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    path = unquote(m.group("path"))
    diff = git_ops.get_diff(path, app_state.repos)
    if diff is None:
        self.send_error(404)
    else:
        self._send_json(200, diff)


@_route("POST", r"^/api/pull-all$")
def _post_pull_all(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    job_id = start_pull_all_job(app_state.repos)
    self._send_json(200, {"job_id": job_id})


@_route("POST", r"^/api/roots$")
def _post_roots(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    path = (body.get("path") or "").strip()
    label = (body.get("label") or "").strip() or os.path.basename(
        os.path.expanduser(path)
    )
    if not path:
        self._send_json(400, {"ok": False, "error": "path is required"})
        return
    # Add to in-memory roots (source of truth)
    if any(p == path for p, _ in app_state.roots):
        self._send_json(409, {"ok": False, "error": "path already exists"})
        return
    app_state.roots.append((path, label))
    # Also persist to config file so it survives restarts when the user
    # explicitly adds roots through the UI.
    config = config_mod.load_config(app_state.config_path) or {}
    roots_list = config.setdefault("roots", [])
    if not any(r.get("path") == path for r in roots_list):
        roots_list.append({"path": path, "label": label})
        config_mod.save_config(app_state.config_path, config)
    self._rescan()
    self._send_json(200, {"ok": True, "path": path, "label": label})


@_route("POST", r"^/api/rescan$")
def _post_rescan(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    roots, repos = self._rescan()
    self._send_json(
        200,
        {
            "ok": True,
            "roots": [{"path": p, "label": label} for p, label in roots],
            "count": len(repos),
            "min_depth": app_state.min_depth,
            "max_depth": app_state.max_depth,
        },
    )


@_route("POST", r"^/api/repo/(?P<path>.+)/action$")
def _post_repo_action(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    path = unquote(m.group("path"))
    outcome = git_ops.do_action(path, body.get("action"), app_state.repos)
    # T-022: attach the repo's post-action status so the frontend can update
    # just this card instead of re-fetching every repo.  The status must be
    # sampled AFTER the action — discard/stash change state drastically.
    http_code = _outcome_http_code(outcome)
    result = outcome.as_dict()
    if outcome.status == "ok":
        repo = app_state.repos.get(path)
        if repo is not None:
            fresh = git_ops.repo_status(repo)
            result["status"] = fresh
            # Keep the background snapshot consistent so the next poll doesn't
            # revert this card to its pre-action status.
            if path in app_state.repo_status:
                snapshot = dict(app_state.repo_status)
                snapshot[path] = fresh
                app_state.repo_status = snapshot
    self._send_json(http_code, result)


@_route("DELETE", r"^/api/roots/(?P<path>.+)$")
def _delete_root(
    self: Handler, m: re.Match[str], parsed: ParseResult, body: Any
) -> None:
    path = unquote(m.group("path"))
    # Remove from in-memory roots (source of truth)
    before = len(app_state.roots)
    app_state.roots = [(p, label) for p, label in app_state.roots if p != path]
    if len(app_state.roots) == before:
        self._send_json(404, {"ok": False, "error": f"root '{path}' not found"})
        return
    # Also remove from config file for persistence.
    config = config_mod.load_config(app_state.config_path) or {}
    if "roots" in config:
        config["roots"] = [r for r in config["roots"] if r.get("path", "") != path]
        config_mod.save_config(app_state.config_path, config)
    self._rescan()
    self._send_json(200, {"ok": True, "path": path})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    """Return True if the ``Host`` header names a loopback endpoint.

    Handles ``host``, ``host:port``, and bracketed IPv6 like ``[::1]:8765``.
    """
    if host.startswith("["):
        hostname = host.split("]", 1)[0][1:]
    else:
        hostname = host.rsplit(":", 1)[0]
    hostname = hostname.strip().lower()
    return hostname in ("127.0.0.1", "localhost", "::1")


def _normalize_hostport(host: str) -> str:
    """Normalize the ``Host`` header for cookie binding (hostname[:port])."""
    return host.strip().lower()


def _new_session_cookie(hostport: str) -> str:
    """Return a signed ``value`` for a session cookie bound to *hostport*.

    Format ``<hmac_sha256>.<urlsafe_b64(payload)>`` where payload is
    ``<expiry_epoch>|<hostport>``.  The host:port is baked in so a cookie issued
    for one port is invalid on another (mirrors the anti-collision design).
    """
    exp = int(time.time()) + SESSION_MAX_AGE_DAYS * 86400
    payload = f"{exp}{_SESSION_SEP}{hostport}".encode()
    secret = app_state.session_secret
    if secret is None:
        raise RuntimeError("session signing secret not configured")
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    enc = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{sig}.{enc}"


def _valid_session_cookie(value: str, hostport: str) -> bool:
    """Validate a session cookie's signature, expiry, and host:port binding."""
    if not value:
        return False
    if app_state.session_secret is None:
        # Signing disabled — accept anything (tests / no-auth mode).
        return True
    try:
        sig, enc = value.split(".", 1)
        payload = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
        exp_s, _, bound = payload.decode("utf-8").partition(_SESSION_SEP)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return False
    good_sig = hmac.compare_digest(
        sig, hmac.new(app_state.session_secret, payload, hashlib.sha256).hexdigest()
    )
    if not good_sig:
        return False
    if bound != hostport:
        return False
    try:
        if int(exp_s) < time.time():
            return False
    except ValueError:
        return False
    return True


def _extract_cookie(header: str, name: str) -> str | None:
    """Pull the value of the named cookie out of a raw ``Cookie`` header."""
    prefix = name + "="
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def _login_throttled(addr: str) -> bool:
    """Return True if *addr* has exhausted its login attempt budget.

    Keeps ``app_state.login_failures`` trimmed to a rolling window so staleness never
    accumulates unboundedly."""
    with app_state.login_lock:
        now = time.monotonic()
        keep = [
            t for t in app_state.login_failures.get(addr, []) if now - t < LOGIN_WINDOW_SECONDS
        ]
        app_state.login_failures[addr] = keep
        return len(keep) >= LOGIN_MAX_FAILURES


def _login_failure(addr: str) -> None:
    """Record a failed login so the next attempt counts toward throttling."""
    with app_state.login_lock:
        app_state.login_failures.setdefault(addr, []).append(time.monotonic())


def _login_success(addr: str) -> None:
    """Clear attempt history on a successful login."""
    with app_state.login_lock:
        app_state.login_failures.pop(addr, None)


def _outcome_http_code(outcome: git_ops.ActionOutcome) -> int:
    """Translate an :class:`ActionOutcome` status into an HTTP status code."""
    mapping = {"ok": 200, "skipped": 200, "not_found": 404, "bad_request": 400}
    return mapping.get(outcome.status, 200)


def _browse_dir(path: str) -> dict[str, Any]:
    """Return a list of subdirectories for the given path."""
    path = os.path.expanduser(path)
    result: dict[str, Any] = {"path": path, "parent": os.path.dirname(path), "dirs": []}
    try:
        path = os.path.realpath(path)
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                result["dirs"].append({"name": name, "path": full})
    except (OSError, PermissionError):
        pass
    return result


def _sweep_stale_jobs() -> int:
    """Drop jobs older than JOB_TTL_SECONDS.  Returns how many were swept."""
    now = time.monotonic()
    with app_state.jobs_lock:
        stale = [
            jid
            for jid, job in app_state.jobs.items()
            if now - job.get("created", now) > JOB_TTL_SECONDS
        ]
        for jid in stale:
            app_state.jobs.pop(jid, None)
    if stale:
        logger.info("swept %d stale job(s) from the pull-all table", len(stale))
    return len(stale)


def start_pull_all_job(repos: dict[str, dict[str, Any]]) -> str:
    """Start a background pull-all job and return its job_id."""
    _sweep_stale_jobs()
    job_id = uuid.uuid4().hex
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_SSE_QUEUE_EVENTS)
    with app_state.jobs_lock:
        app_state.jobs[job_id] = {"queue": q, "created": time.monotonic()}

    def worker() -> None:
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


# ---------------------------------------------------------------------------
# Background status refresh
# ---------------------------------------------------------------------------

# The frontend polls /api/repos every 30s while visible; the background
# refresher recomputes statuses on the same cadence so every poll is a cheap
# snapshot read instead of spawning one ``git status`` per repo per request.
REFRESH_INTERVAL = 30.0


def _refresh_once(app: HarborApp) -> None:
    """Compute one fresh status snapshot for ``app.repos`` and swap it in.

    The snapshot is swapped in only if the repo set didn't change while we
    were computing (a rescan rebinds ``app.repos``, making a concurrent result
    stale — the snapshot is dropped instead, and the next /api/repos falls
    back to an on-demand compute).
    """
    repos = app.repos
    try:
        snapshot = {s["path"]: s for s in git_ops.get_repos_status(repos)}
    except Exception:
        # One bad iteration (a pathological repo) must not kill the
        # refresher — log and retry next cycle.
        logger.exception("status refresh failed; will retry")
        snapshot = {}
    app.repo_status = snapshot if app.repos is repos else {}


def _refresh_loop(app: HarborApp) -> None:
    """Background loop that keeps ``app.repo_status`` fresh.

    Runs until the process exits (daemon thread).  Statuses are computed once
    per interval for the whole repo set in the shared git executor, so every
    /api/repos poll is a cheap snapshot read.
    """
    while True:
        _refresh_once(app)
        time.sleep(REFRESH_INTERVAL)


def start_status_refresher(app: HarborApp) -> None:
    """Start the background status-refresh daemon thread (idempotent)."""
    if app.refresher is not None and app.refresher.is_alive():
        return
    app.refresher = threading.Thread(
        target=_refresh_loop, args=(app,), daemon=True, name="status-refresher"
    )
    app.refresher.start()


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the Harbor web UI.

    All mutable state lives in the module-level :data:`app_state`
    (:class:`HarborApp`) instead of class attributes, so tests can reset
    state cleanly and multiple instances share the same state naturally.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_json(self, code: int, obj: Any, set_cookie: str | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self._set_session_cookie(set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _set_session_cookie(self, hostport: str) -> None:
        """Set the signed, HttpOnly session cookie for *hostport*."""
        value = _new_session_cookie(hostport)
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_MAX_AGE_DAYS * 86400}",
        )

    def _check_origin(self) -> bool:
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
        if host and not _is_loopback_host(host):
            self._send_json(403, {"ok": False, "error": "non-loopback host"})
            return False
        return True

    def _serve_html(self) -> None:
        try:
            with open(app_state.html_path, "rb") as f:
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

    def _serve_static_file(self, name: str) -> None:
        """Serve a file from the static directory by filename.

        Only simple filenames are allowed (no subdirectories) to prevent
        directory traversal attacks.
        """
        if not name or "/" in name or ".." in name or name.startswith("."):
            self.send_error(400)
            return
        full = os.path.join(app_state.static_dir, name)
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

    def _stream(self, qs: dict[str, list[str]]) -> None:
        job_ids = qs.get("job")
        job_id = job_ids[0] if job_ids else None
        if job_id is None:
            self.send_error(404)
            return
        with app_state.jobs_lock:
            job = app_state.jobs.get(job_id)
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
                with app_state.jobs_lock:
                    app_state.jobs.pop(job_id, None)
                return

    # ------------------------------------------------------------------
    # Re-scan
    # ------------------------------------------------------------------

    def _rescan(self) -> tuple[list[tuple[str, str]], dict[str, dict[str, Any]]]:
        """Re-scan all roots and update app_state.repos.

        Also reloads min_depth / max_depth from the config file (unless
        overridden by CLI arguments at startup).
        """
        config = config_mod.load_config(app_state.config_path) or {}
        # Reload depth settings from config (CLI args remain supreme)
        if app_state.cli_min_depth is None:
            app_state.min_depth = config.get("min_depth", app_state.min_depth)
        if app_state.cli_max_depth is None:
            app_state.max_depth = config.get("max_depth", app_state.max_depth)
        new_repos = scanner_mod.scan_roots(
            app_state.roots,
            min_depth=app_state.min_depth,
            max_depth=app_state.max_depth,
        )
        app_state.repos = new_repos
        # The snapshot describes the old repo set — invalidate it so the next
        # /api/repos recomputes fresh statuses for the new set (the refresher
        # repopulates it on its next tick).
        app_state.repo_status = {}
        return app_state.roots, new_repos

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _is_static_or_root(self, parsed: ParseResult) -> bool:
        """Return True if the request targets the root page or static assets.

        These paths are exempt from token auth so the browser can load the
        page normally (the token is in the URL query string read by JS).

        `/login` is also exempt — otherwise users who arrived without a
        token couldn't even submit the token-entry form.
        """
        path = parsed.path
        return (
            path == "/"
            or path == "/index.html"
            or path == "/login"
            or path.startswith("/static/")
            or path == "/favicon.ico"
        )

    def _check_token(self, parsed: ParseResult) -> bool:
        """Return True if the request carries a valid session cookie.

        Sends a 403 and returns False when auth is required but missing/wrong.
        The launch token is intentionally NOT accepted here — it is a one-time
        entry credential exchanged for the cookie on ``/`` and must never
        authenticate API/SSE routes directly.
        """
        if app_state.auth_token is None and app_state.session_secret is None:
            return True
        if self._is_static_or_root(parsed):
            return True
        host = self.headers.get("Host", "")
        header = self.headers.get("Cookie", "")
        value = _extract_cookie(header, SESSION_COOKIE)
        if value and _valid_session_cookie(value, _normalize_hostport(host)):
            return True
        self._send_json(
            403,
            {
                "ok": False,
                "error": "authentication required (missing or invalid session",
            },
        )
        return False

    def _dispatch(self, method: str, parsed: ParseResult, body: Any = None) -> None:
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

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            return None  # caller turns this into a 413
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return None  # caller turns this into a 400

    def do_GET(self) -> None:
        self._dispatch("GET", urlparse(self.path))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "request body too large"})
            return
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        self._dispatch("POST", urlparse(self.path), body)

    def do_DELETE(self) -> None:
        self._dispatch("DELETE", urlparse(self.path))

    def log_message(self, fmt: str, *args: Any) -> None:
        # T-011: never log the full URL which may contain the auth token.
        # We log only the path portion (without query string).
        msg = fmt % args
        # BaseHTTPRequestHandler formats "GET /path?token=xxx HTTP/1.1" 200 -
        # Strip the query string to avoid leaking tokens to logs.
        import re as _re

        cleaned = _re.sub(r"( \S+?)\?\S+? ", r"\1 ", msg, count=1)
        logger.debug("%s - %s", self.address_string(), cleaned)
