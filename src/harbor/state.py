"""Application state shared between the HTTP handler and the CLI entrypoint.

Previously all state lived as :class:`~http.server.BaseHTTPRequestHandler`
class attributes, which forced tests to snapshot/restore the class after
every test and made it impossible to run more than one Harbor instance in
the same process.

The :class:`AppState` dataclass owns *all* mutable configuration and repo
state.  A single instance is constructed in ``__main__.py`` and injected
into the handler via a module-level attribute (``server.app_state``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AppState:
    """Mutable application state shared across HTTP handler instances.

    Attributes:
        repos: Mapping of repo path → :class:`Repo` (or dict, during transition).
        roots: List of ``(path, label)`` tuples — the active scan roots.
        html_path: Filesystem path to ``index.html``.
        static_dir: Directory that holds static assets.
        config_path: Path to the TOML config file.
        min_depth: Minimum directory depth for repo scanning.
        max_depth: Maximum directory depth for repo scanning.
        cli_min_depth: When set, overrides ``min_depth`` from config file.
        cli_max_depth: When set, overrides ``max_depth`` from config file.
    """

    repos: dict = field(default_factory=dict)
    roots: list = field(default_factory=list)
    html_path: str = ""
    static_dir: str = ""
    config_path: str = ""
    min_depth: int = 1
    max_depth: int = 5

    # CLI args take priority and aren't hot-reloaded from config file.
    cli_min_depth: int | None = None
    cli_max_depth: int | None = None
