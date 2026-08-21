"""Entry point for Harbor — the local web dashboard for multi-repo management.

Usage:
    harbor                         # scan roots from config file, fallback to cwd
    harbor ~/work ~/personal       # scan specific directories
    python -m harbor               # equivalent to `harbor`
"""

import argparse
import http.server
import logging
import os
import sys
import threading
import webbrowser

from . import __version__
from . import config as config_mod
from . import scanner as scanner_mod
from .server import Handler

logger = logging.getLogger(__name__)


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
                port, port, port,
            )
            sys.exit(1)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Harbor — local web dashboard for managing multiple git repos.",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        metavar="ROOT",
        help="One or more directories to scan for git repos. "
             "If omitted, reads from the config file or falls back to the current directory.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("HARBOR_CONFIG", str(config_mod.CONFIG_PATH)),
        help="Path to config file (default: ~/.config/harbor/config.toml).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: 8765, env: HARBOR_PORT).",
    )
    parser.add_argument(
        "--min-depth",
        type=int,
        default=None,
        help="Minimum directory depth to scan (default: 1).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum directory depth to scan (default: 5).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harbor {__version__}",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Load config --------------------------------------------------
    config = config_mod.load_config(args.config)

    # --- Resolve settings ---------------------------------------------
    port = config_mod.resolve_setting(args.port, "HARBOR_PORT", "port", config, 8765)
    min_depth = config_mod.resolve_setting(args.min_depth, "HARBOR_MIN_DEPTH", "min_depth", config, 1)
    max_depth = config_mod.resolve_setting(args.max_depth, "HARBOR_MAX_DEPTH", "max_depth", config, 5)

    # --- Resolve roots ------------------------------------------------
    roots = config_mod.resolve_roots(args.roots, config)
    repos = scanner_mod.scan_roots(roots, min_depth=min_depth, max_depth=max_depth)

    # C-1: when the user passes roots on the CLI, persist them to the
    # config file so that Rescan / GET /api/roots keep using them after
    # the CLI args are gone.  Dedupe by realpath against existing config
    # roots; don't touch the file when no CLI roots were given.
    if args.roots:
        config = config or {}
        existing_paths = {os.path.realpath(os.path.expanduser(r["path"]))
                          for r in config.get("roots", [])}
        existing_paths = {p for p in existing_paths if p}
        for path, label in roots:
            real = os.path.realpath(os.path.expanduser(path))
            if real and real not in existing_paths:
                config.setdefault("roots", []).append({"path": real, "label": label})
                existing_paths.add(real)
        config_mod.save_config(args.config, config)

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

    # --- Configure handler --------------------------------------------
    Handler.repos = repos
    Handler.html_path = html_path
    Handler.static_dir = static_dir
    Handler.config_path = args.config
    Handler.min_depth = min_depth
    Handler.max_depth = max_depth
    # Remember whether depth was set via CLI so hot-reload doesn't override it.
    Handler.cli_min_depth = args.min_depth
    Handler.cli_max_depth = args.max_depth

    server = _create_server(port)
    url = f"http://127.0.0.1:{port}"
    logger.info("serving at %s", url)

    if not args.no_browser:
        threading.Timer(0.5, lambda: _try_open_browser(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


def _try_open_browser(url):
    """Open the browser, swallowing any error (e.g. headless environments)."""
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    main()
