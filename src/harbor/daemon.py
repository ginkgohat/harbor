"""Background daemon management for Harbor.

Provides ``start`` / ``status`` / ``stop`` commands so Harbor can run in
the background without keeping a terminal open.

PID file and log file live in the platform-specific user state directory
(``~/.local/state/harbor`` on Linux, ``~/Library/Application Support/harbor``
on macOS, ``%LOCALAPPDATA%/harbor`` on Windows).
"""

from __future__ import annotations

import errno
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

from platformdirs import user_state_dir

STATE_DIR = Path(user_state_dir("harbor", appauthor=False))
PID_FILE = STATE_DIR / "harbor.pid"
LOG_FILE = STATE_DIR / "harbor.log"
TOKEN_FILE = STATE_DIR / "harbor.token"
PORT_FILE = STATE_DIR / "harbor.port"


def _read_pid() -> int | None:
    """Read the PID file, returning the PID or ``None`` if it doesn't exist."""
    if not PID_FILE.is_file():
        return None
    try:
        text = PID_FILE.read_text().strip()
        return int(text) if text else None
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    """Write *pid* to the PID file, creating the state dir if needed."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    """Remove the PID / token / port files if they exist."""
    with suppress(FileNotFoundError):
        PID_FILE.unlink()
    with suppress(FileNotFoundError):
        TOKEN_FILE.unlink()
    with suppress(FileNotFoundError):
        PORT_FILE.unlink()


def _is_process_alive(pid: int) -> bool:
    """Return ``True`` if the process with *pid* is currently running."""
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            # No such process.
            return False
        # EPERM = process exists but we can't signal it; any other error
        # we treat as "not alive" but it's ambiguous — return False.
        return e.errno == errno.EPERM
    return True


def _pid_start_time(pid: int) -> float | None:
    """Return the start time (epoch) of *pid*, or ``None`` if unavailable."""
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            import subprocess
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=", "-ww"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse the ps output (standard "ctime" format).
                from datetime import datetime
                return datetime.strptime(
                    result.stdout.strip(), "%a %b %d %H:%M:%S %Y"
                ).timestamp()
    except Exception:
        pass
    return None


def cmd_start(serve_args) -> int:
    """Start Harbor in the background.

    *serve_args* is the argparse namespace for the ``start`` subcommand
    (roots, port, etc.).  Returns an exit code.
    """
    if not hasattr(os, "fork"):
        print(
            "error: harbor start is not supported on this platform (no fork).\n"
            "  On Windows, run Harbor normally in a terminal or use a service manager.",
            file=sys.stderr,
        )
        return 1

    # Check if already running.
    existing_pid = _read_pid()
    if existing_pid and _is_process_alive(existing_pid):
        print(f"Harbor is already running (PID {existing_pid}).")
        print(f"  Log: {LOG_FILE}")
        return 0

    # Stale PID file — clean it up.
    if existing_pid:
        _remove_pid()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # In daemon mode, don't auto-open the browser.
    serve_args.no_browser = True

    # --- Daemonize ----------------------------------------------------
    # First fork.
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly and report status.
        time.sleep(0.3)
        child_pid = _read_pid()
        if child_pid and _is_process_alive(child_pid):
            print(f"Harbor started in background (PID {child_pid}).")
            print(f"  Log: {LOG_FILE}")
            print("  Stop with:  harbor stop")
            print("  Status:     harbor status")
        else:
            print("Harbor failed to start. Check the log for details:")
            print(f"  {LOG_FILE}")
            return 1
        return 0

    # First child — continue daemonization.
    os.setsid()

    # Second fork (so we can't reacquire a controlling terminal).
    pid = os.fork()
    if pid > 0:
        # First child exits; grandchild continues.
        os._exit(0)

    # --- Grandchild (the actual daemon) ------------------------------
    # Write PID file as early as possible.
    _write_pid(os.getpid())

    # Redirect stdin from /dev/null, stdout/stderr to log file.
    log_f = open(LOG_FILE, "a")  # noqa: SIM115 — held for the lifetime of the daemon
    devnull = open(os.devnull)   # noqa: SIM115 — same
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(log_f.fileno(), sys.stdout.fileno())
    os.dup2(log_f.fileno(), sys.stderr.fileno())
    devnull.close()
    # Don't close log_f — we dup2'd it, but keep it open for safety.

    # Set a reasonable umask.
    os.umask(0o022)

    # Change to root so we don't hold any directory open.
    os.chdir("/")

    # Register atexit handler to clean up PID file.
    import atexit
    atexit.register(_remove_pid)

    # Handle SIGTERM gracefully.
    def _handle_sigterm(signum, frame):
        # Raise SystemExit so atexit runs and the server shuts down cleanly.
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Now run the server (imported lazily to avoid circular imports).
    from .__main__ import _run_server
    _run_server(serve_args)

    return 0


def cmd_status() -> int:
    """Print whether Harbor is running in the background.

    Returns 0 if running, 1 if not.
    """
    pid = _read_pid()
    if not pid:
        print("Harbor is not running (no PID file).")
        return 1

    if not _is_process_alive(pid):
        print(f"Harbor is not running (stale PID file: {pid}).")
        _remove_pid()
        return 1

    start_time = _pid_start_time(pid)
    started_str = ""
    if start_time:
        from datetime import datetime
        started_str = f" since {datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')}"

    print(f"Harbor is running (PID {pid}){started_str}.")
    print(f"  Log:    {LOG_FILE}")
    # Show the auth token and URL if state files exist.
    try:
        token = TOKEN_FILE.read_text().strip()
        port = None
        with suppress(OSError, ValueError):
            port = int(PORT_FILE.read_text().strip())
        if token:
            if port:
                print(f"  URL:    http://127.0.0.1:{port}/?token={token}")
            print(f"  Token:  {token}")
            print("  ⚠  Keep this private — it grants full access to Harbor.")
    except OSError:
        pass
    print("  Stop with:  harbor stop")
    return 0


def cmd_stop() -> int:
    """Stop a background Harbor instance.

    Sends SIGTERM, waits up to 10 seconds, then SIGKILL if still alive.
    Returns 0 on success, 1 if Harbor wasn't running.
    """
    pid = _read_pid()
    if not pid:
        print("Harbor is not running (no PID file).")
        return 1

    if not _is_process_alive(pid):
        print(f"Harbor is not running (stale PID file: {pid}).")
        _remove_pid()
        return 1

    print(f"Stopping Harbor (PID {pid})...")

    # Send SIGTERM.
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"Failed to send SIGTERM: {e}")
        return 1

    # Wait for it to exit.
    for _ in range(50):  # 10 seconds total, 0.2s per check
        time.sleep(0.2)
        if not _is_process_alive(pid):
            break
    else:
        # Still alive after 10s — force kill.
        print("Harbor did not exit gracefully, sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:
            print(f"Failed to send SIGKILL: {e}")
            return 1
        time.sleep(0.5)

    _remove_pid()
    print("✓ Harbor stopped.")
    return 0
