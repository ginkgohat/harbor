"""Application state shared between the HTTP handler and the CLI entrypoint.

Previously all state lived as :class:`~http.server.BaseHTTPRequestHandler`
class attributes, which forced tests to snapshot/restore the class after
every test and made it impossible to run more than one Harbor instance in
the same process.

The :class:`HarborApp` dataclass owns *all* mutable application state:
configuration, repo records, authentication (launch token + session-signing
secret), pull-all jobs, and login throttling.  A single instance is
constructed in ``__main__.py`` and injected into the handler via a module-level
attribute (``server.app_state``).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HarborApp:
    """Mutable application state shared across HTTP handler instances.

    Attributes:
        repos: Mapping of repo path → repo dict (as produced by the scanner).
        roots: List of ``(path, label)`` tuples — the active scan roots.
        html_path: Filesystem path to ``index.html``.
        static_dir: Directory that holds static assets.
        config_path: Path to the TOML config file.
        min_depth: Minimum directory depth for repo scanning.
        max_depth: Maximum directory depth for repo scanning.
        cli_min_depth: When set, overrides ``min_depth`` from config file.
        cli_max_depth: When set, overrides ``max_depth`` from config file.
        fetch_on_scan: When true, rescans run ``git fetch`` in every repo
            before computing status so ahead/behind reflects upstream; set by
            the config file key ``fetch`` (default true).
        auth_token: The launch token (one-time entry credential).  ``None``
            disables authentication (tests / no-auth mode).
        session_secret: HMAC key that signs web-session cookies.  ``None``
            disables cookie signing (tests / no-auth mode).
        jobs: Pull-all SSE jobs keyed by job id; each value is a ``_Job``
            dict ``{"queue": Queue, "created": float}``.
        jobs_lock: Guards ``jobs``.
        login_failures: Per-client failed-login timestamps (throttling).
        login_lock: Guards ``login_failures``.
        repo_status: Cached repo status snapshot (path → status dict), kept
            fresh in the background so /api/repos never blocks on git.
        refresher: The daemon thread running the refresh loop, if started.
        scan_generation: Count of completed scans (frontend waits on it after
            kicking a background rescan via the X-Harbor-Scan-Gen header).
        rescanning: True while a background rescan worker is running.
        rescan_requested: True when a mutation asked for a rescan while one
            was already running (worker re-runs instead of missing it).
        rescan_lock: Guards ``rescanning``/``rescan_requested``.
        epoch: Random hex id, unique per :class:`HarborApp` instance.  Reported
            via the X-Harbor-Epoch header so a browser tab can detect that the
            server process restarted (and its scan counters reset).
        state_lock: Reentrant lock guarding publication and reads of ``repos``,
            ``repo_status``, ``scan_generation``, ``scan_request_seq``,
            ``scan_completed_seq``, and ``last_scan_error``.  A reader must
            observe a consistent (repo set, snapshot, scan counters) tuple.
        scan_request_seq: Incremented once per rescan request (including the
            ones triggered by adding/removing roots); returned to the frontend
            as ``scan_seq`` so it can wait for exactly that request to finish.
        scan_completed_seq: Set to the ``scan_request_seq`` observed when a
            worker pass started, both on success and on failure; monotonically
            non-decreasing.  The frontend waits for ``completed >= scan_seq``.
        last_scan_error: Error string of the most recent failed rescan pass
            (``"Type: message"``, truncated), cleared by the next success;
            reported via the X-Harbor-Scan-Error header.
    """

    # --- configuration / repo state ---
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    roots: list[tuple[str, str]] = field(default_factory=list)
    html_path: str = ""
    static_dir: str = ""
    config_path: str = ""
    min_depth: int = 1
    max_depth: int = 5

    # CLI args take priority and aren't hot-reloaded from config file.
    cli_min_depth: int | None = None
    cli_max_depth: int | None = None

    # Rescans run `git fetch` before sampling status so ahead/behind reflects
    # upstream instead of stale local remote-tracking refs.  Hot-reloaded from
    # the config file key `fetch` (default on).
    fetch_on_scan: bool = True

    # --- authentication ---
    auth_token: str | None = None
    session_secret: bytes | None = None

    # --- pull-all jobs ---
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs_lock: threading.Lock = field(default_factory=threading.Lock)

    # --- login throttling ---
    login_failures: dict[str, list[float]] = field(default_factory=dict)
    login_lock: threading.Lock = field(default_factory=threading.Lock)

    # --- background status snapshot ---
    # repo path → status dict, recomputed in the background by the status
    # refresher so /api/repos is a cheap cache read instead of spawning one
    # `git status` per repo per request (see server.start_status_refresher).
    repo_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The daemon thread running the refresh loop (None until started).
    refresher: threading.Thread | None = None

    # --- background rescan ---
    # Count of completed scans; /api/repos reports it via the
    # X-Harbor-Scan-Gen header so the frontend can wait for a background
    # rescan to finish.  Incremented by server._rescan_worker.
    scan_generation: int = 0
    # Single-flight guard for background rescans: True while a worker runs;
    # rescan_requested makes the worker loop once more instead of missing a
    # mutation that arrived mid-scan.
    rescanning: bool = False
    rescan_requested: bool = False
    rescan_lock: threading.Lock = field(default_factory=threading.Lock)

    # --- scan completion protocol ---
    # Unique per instance so a restarted server is never confused with the
    # previous process by a still-open browser tab.
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Guards publication/reads of repos, repo_status, and the scan counters so
    # /api/repos can never pair a stale body with a newer generation counter.
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    # Requests a rescan (explicit refresh or a roots mutation); the frontend
    # waits for scan_completed_seq to reach the value returned as scan_seq.
    scan_request_seq: int = 0
    # Request sequence observed by the pass that just finished (success or
    # failure); monotonically non-decreasing.
    scan_completed_seq: int = 0
    # Last rescan failure ("Type: message"), cleared on the next success.
    last_scan_error: str | None = None
