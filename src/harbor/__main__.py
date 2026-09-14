"""Entry point for Harbor — the local web dashboard for multi-repo management.

Usage:
    harbor                         # scan the current directory
    harbor ~/work ~/personal       # scan specific directories
    python -m harbor               # equivalent to `harbor`
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import http.server
import logging
import os
import secrets
import signal
import sys
import threading
import webbrowser
from types import FrameType
from typing import NoReturn

from . import __version__
from . import config as config_mod
from . import daemon as daemon_mod
from . import scanner as scanner_mod
from . import selfmanage as selfmanage_mod
from . import server as server_mod
from .server import Handler
from .state import HarborApp

logger = logging.getLogger(__name__)

# CLI subcommands.  Module constant so the ``_parse_args`` heuristic and
# tests share one source of truth.
SUBCOMMANDS = {"serve", "update", "uninstall", "start", "status", "stop"}
# Flags meaningful only without a subcommand (print top-level help/version).
TOP_LEVEL_FLAGS = {"-h", "--help", "--version"}
# Standard end-of-options marker.  Everything after ``--`` is a positional
# (root path), so a directory named ``status`` or starting with ``-`` works:
# ``harbor -- status``, ``harbor -- -my-dir``.
END_OF_OPTIONS = "--"


def _create_server(port: int) -> http.server.ThreadingHTTPServer:
    """Create and return a ThreadingHTTPServer bound to *port*.

    Raises SystemExit with a friendly message if the port is already in use.
    Other OSErrors are re-raised as-is.
    """
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == 48 or "Address already in use" in str(e):
            logger.error(
                "Port %d is already in use by another process.\n"
                "  → Try a different port with:  harbor --port <number>\n"
                "  → Or find and stop the process using port %d:\n"
                "       lsof -i :%d",
                port,
                port,
                port,
            )
            sys.exit(1)
        raise


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands.

    To preserve backward compatibility, ``harbor [ROOT ...] [flags]`` still
    works and runs the server — the default subcommand is ``serve``.
    """
    parser = argparse.ArgumentParser(
        prog="harbor",
        description="Harbor — local web dashboard for managing multiple git repos.",
    )
    parser.add_argument("--version", action="version", version=f"harbor {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- serve (default) ---------------------------------------------
    serve = subparsers.add_parser(
        "serve",
        help="Start the Harbor web dashboard (default).",
        description="Start the Harbor web dashboard.",
    )
    serve.add_argument(
        "roots",
        nargs="*",
        metavar="ROOT",
        help="One or more directories to scan for git repos. "
        "If omitted, uses roots saved in the config file (e.g. added "
        "via the UI), or falls back to the current directory.",
    )
    serve.add_argument(
        "--config",
        default=os.environ.get("HARBOR_CONFIG", str(config_mod.CONFIG_PATH)),
        help="Path to config file (default: ~/.config/harbor/config.toml).",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: 8765, env: HARBOR_PORT).",
    )
    serve.add_argument(
        "--min-depth",
        type=int,
        default=None,
        help="Minimum directory depth to scan (default: 1).",
    )
    serve.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum directory depth to scan (default: 5).",
    )
    serve.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically.",
    )

    # --- update ------------------------------------------------------
    subparsers.add_parser(
        "update",
        help="Upgrade Harbor to the latest version from GitHub.",
        description="Upgrade Harbor to the latest version from GitHub.",
    )

    # --- uninstall ---------------------------------------------------
    subparsers.add_parser(
        "uninstall",
        help="Uninstall Harbor from the current Python environment.",
        description="Uninstall Harbor from the current Python environment.",
    )

    # --- start -------------------------------------------------------
    start = subparsers.add_parser(
        "start",
        help="Start Harbor in the background (daemon mode).",
        description="Start Harbor in the background (daemon mode).",
    )
    start.add_argument(
        "roots",
        nargs="*",
        metavar="ROOT",
        help="One or more directories to scan for git repos.",
    )
    start.add_argument(
        "--config",
        default=os.environ.get("HARBOR_CONFIG", str(config_mod.CONFIG_PATH)),
        help="Path to config file (default: ~/.config/harbor/config.toml).",
    )
    start.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: 8765, env: HARBOR_PORT).",
    )
    start.add_argument(
        "--min-depth",
        type=int,
        default=None,
        help="Minimum directory depth to scan (default: 1).",
    )
    start.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum directory depth to scan (default: 5).",
    )
    start.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically (implied in daemon mode).",
    )

    # --- status ------------------------------------------------------
    subparsers.add_parser(
        "status",
        help="Check if Harbor is running in the background.",
        description="Check if Harbor is running in the background.",
    )

    # --- stop --------------------------------------------------------
    subparsers.add_parser(
        "stop",
        help="Stop a background Harbor instance.",
        description="Stop a background Harbor instance.",
    )

    return parser


def _parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """Parse args, falling back to ``serve`` when no subcommand is given.

    Argparse can't express "subcommand XOR default-to-serve" in one tree, so
    we make a single judgment call: a known subcommand wins if it is the first
    non-option token (``harbor status``, ``harbor update``).  Everything else
    — paths, flags, ``--``-escaped paths — is routed through ``serve``.

    Rules:
    - ``harbor status``/``serve``/etc. → parse as that subcommand.
    - A leading ``--`` forces the rest to be ``serve`` paths, so a directory
      named ``status`` or starting with ``-`` still works (``harbor -- status``,
      ``harbor -- -my-dir``).
    - ``harbor -h``/``--version`` with no other args → top-level help/version.
    - Otherwise (``harbor ~/projects``, ``harbor --port 9000``) → ``serve``.
    """
    argv = sys.argv[1:]

    # First non-option token, honouring a ``--`` end-of-options marker.
    seen_sep = False
    first_positional = None
    for arg in argv:
        if arg == END_OF_OPTIONS:
            seen_sep = True
            continue
        if not arg.startswith("-"):
            first_positional = arg
            break

    if (
        not seen_sep
        and first_positional is not None
        and first_positional in SUBCOMMANDS
    ):
        # Explicit subcommand — use the normal subparser tree.
        return parser.parse_args(argv)

    if first_positional is None and any(a in TOP_LEVEL_FLAGS for a in argv):
        # No subcommand and the user asked for top-level help / version.
        return parser.parse_args(argv)

    # Default to "serve".
    return parser.parse_args(["serve", *argv])


def main() -> None:
    parser = _build_parser()
    args = _parse_args(parser)

    # --- Dispatch subcommands ----------------------------------------
    if args.command == "update":
        sys.exit(selfmanage_mod.cmd_self_update())

    if args.command == "uninstall":
        sys.exit(selfmanage_mod.cmd_self_uninstall())

    if args.command == "start":
        sys.exit(daemon_mod.cmd_start(args))

    if args.command == "status":
        sys.exit(daemon_mod.cmd_status())

    if args.command == "stop":
        sys.exit(daemon_mod.cmd_stop())

    # --- serve (default) ---------------------------------------------
    sys.exit(_run_server(args))


def _run_server(args: argparse.Namespace) -> int:
    """Actually start the HTTP server.  Shared by foreground and daemon modes.

    Returns an exit code (0 for normal shutdown).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Load config --------------------------------------------------
    config = config_mod.load_config(args.config)

    # --- Resolve settings ---------------------------------------------
    # Values come from CLI / env / config file.  resolve_int_setting converts the
    # env string and validates range, raising a clear ValueError on bad input so
    # we can print a friendly line instead of a raw traceback.
    try:
        port = config_mod.resolve_int_setting(
            args.port, "HARBOR_PORT", "port", config, 8765, 1, 65535, "port"
        )
        min_depth = config_mod.resolve_int_setting(
            args.min_depth,
            "HARBOR_MIN_DEPTH",
            "min_depth",
            config,
            1,
            0,
            99,
            "min_depth",
        )
        max_depth = config_mod.resolve_int_setting(
            args.max_depth,
            "HARBOR_MAX_DEPTH",
            "max_depth",
            config,
            5,
            0,
            99,
            "max_depth",
        )
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)
    if max_depth < min_depth:
        logger.error("max_depth (%d) must be >= min_depth (%d)", max_depth, min_depth)
        sys.exit(1)

    # --- Resolve roots ------------------------------------------------
    roots = config_mod.resolve_roots(args.roots, config)
    repos = scanner_mod.scan_roots(roots, min_depth=min_depth, max_depth=max_depth)

    for path, label in roots:
        count = sum(1 for r in repos.values() if r["root_label"] == label)
        logger.info("scanned %s (%s) — %d repo(s)", path, label, count)
    logger.info("total: %d repo(s) across %d root(s)", len(repos), len(roots))

    # --- Resolve HTML path --------------------------------------------
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    html_path = os.path.join(static_dir, "index.html")
    if not os.path.isfile(html_path):
        logger.error("index.html not found at %s", html_path)
        sys.exit(1)

    # --- Configure handler (HarborApp) ----------------------------------
    state = HarborApp(
        repos=repos,
        roots=roots,
        html_path=html_path,
        static_dir=static_dir,
        config_path=args.config,
        min_depth=min_depth,
        max_depth=max_depth,
        # Remember whether depth was set via CLI so hot-reload doesn't override it.
        cli_min_depth=args.min_depth,
        cli_max_depth=args.max_depth,
        # The startup scan above is the first completed scan; the frontend
        # waits for scan_generation to advance after kicking a background
        # rescan (X-Harbor-Scan-Gen header).
        scan_generation=1,
    )
    # Share state with the server module (read by every Handler instance).
    server_mod.app_state = state
    # Background repo-status refresher: /api/repos serves a cached snapshot
    # instead of blocking on per-repo git subprocesses on every request.
    server_mod.start_status_refresher(state)

    # --- Authentication token + PID file -------------------------------
    # T-011: generate a random token and include it in the URL.
    # Protects against CSRF, DNS rebinding, and local process attacks.
    token = secrets.token_urlsafe(16)
    state.auth_token = token

    # Write PID + token to state files so `harbor status` works for both
    # foreground and daemon mode.  In daemon mode the PID file was already
    # written by daemon.py — overwriting with the same value is fine.
    try:
        from . import daemon as _daemon_mod

        _daemon_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # PID file (0644 — harmless, just a number)
        _daemon_mod.PID_FILE.write_text(str(os.getpid()))
        # Port file so `harbor status` can show the full URL
        _daemon_mod.PORT_FILE.write_text(str(port))
        # Token file (0600 — sensitive)
        fd = os.open(
            str(_daemon_mod.TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        # Session-signing secret (0600).  Loaded-or-created so signed cookies
        # survive restarts; if the file can't be written we still fall back to
        # an in-memory secret so the current run keeps working.
        if _daemon_mod.SESSION_SECRET_FILE.exists():
            state.session_secret = _daemon_mod.SESSION_SECRET_FILE.read_bytes()
        else:
            secret = os.urandom(32)
            import contextlib

            try:
                with contextlib.suppress(FileNotFoundError):
                    _daemon_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
                sfd = os.open(
                    str(_daemon_mod.SESSION_SECRET_FILE),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(sfd, "wb") as sf:
                    sf.write(secret)
            except OSError:
                # Non-fatal — fall back to the in-memory secret.
                pass
            state.session_secret = secret
        # Clean up both on exit (works for foreground mode; daemon mode
        # already has its own atexit handler registered in daemon.py).
        atexit.register(_daemon_mod._remove_pid)
    except OSError:
        # Non-fatal — the token is still visible in the startup log.
        pass

    # Handle SIGTERM gracefully so atexit runs (cleaning up PID/token files)
    # even when killed from outside (e.g. `kill <pid>` or `harbor stop`).
    # On Windows, SIGTERM isn't available — skip silently.
    if hasattr(signal, "SIGTERM"):

        def _handle_sigterm(signum: int, frame: FrameType | None) -> NoReturn:
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handle_sigterm)

    httpd = _create_server(port)
    url = f"http://127.0.0.1:{port}/?token={token}"
    logger.info("serving at http://127.0.0.1:%d/?token=<hidden>", port)
    logger.info(
        "note: the URL contains an auth token — don't share it or paste it "
        "into untrusted pages. It will also appear in your browser history."
    )

    if not args.no_browser:
        threading.Timer(0.5, lambda: _try_open_browser(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        httpd.shutdown()

    return 0


def _try_open_browser(url: str) -> None:
    """Open the browser, swallowing any error (e.g. headless environments)."""
    with contextlib.suppress(Exception):
        webbrowser.open(url)


if __name__ == "__main__":
    main()
