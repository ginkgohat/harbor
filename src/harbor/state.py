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
