"""Self-update and self-uninstall commands for Harbor.

Harbor is installed from GitHub (the PyPI name ``harbor`` is taken by another
project), so we upgrade / remove the package by invoking pip against the
current Python interpreter -- unless that interpreter is managed by pipx or by
the OS (PEP 668).  In those environments running pip either fails outright or
would break the installation, so we delegate: a pipx install is upgraded with
``pipx upgrade harbor`` (run for the user where the OS allows it), and PEP 668
installs get the command the user should run instead.

The detection helpers mirror pip's own logic, are stdlib-only and side-effect
free, so they are cheap to call from the CLI and easy to unit test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Literal

GITHUB_INSTALL_URL = "git+https://github.com/ginkgohat/harbor.git"
PACKAGE_NAME = "harbor"

#: User-facing commands printed as guidance.  Kept as module constants so the
#: docs, tests and messages can't drift apart.
PIPX_INSTALL_COMMAND = f'pipx install "{GITHUB_INSTALL_URL}"'
PIPX_UPGRADE_COMMAND = f"pipx upgrade {PACKAGE_NAME}"
PIPX_REINSTALL_COMMAND = f"pipx reinstall {PACKAGE_NAME}"
PIPX_UNINSTALL_COMMAND = f"pipx uninstall {PACKAGE_NAME}"

#: Marker pip itself looks for (PEP 668), relative to the stdlib directory.
PEP668_MARKER = "EXTERNALLY-MANAGED"

_Action = Literal["update", "uninstall"]

#: Fragments of captured pip output used to explain a failure.  Matched
#: case-insensitively against stdout + stderr.
_PEP668_OUTPUT_MARKERS = ("externally-managed-environment", "externally managed")
_INSTALL_FAILURE_MARKERS = (
    "no matching distribution",
    "could not find a version",
    "failed to connect",
    "connection error",
    "timed out",
    "temporary failure in name resolution",
)


def _run_pip(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run pip with *args* using the current Python interpreter.

    pip's output is captured instead of inherited so the caller can print it
    *and* inspect it (e.g. to recognise ``externally-managed-environment``).
    The caller is responsible for showing :attr:`stdout` / :attr:`stderr`.
    """
    cmd = [sys.executable, "-m", "pip", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        print(
            "error: pip is not available for the current Python interpreter.\n"
            f"  tried: {' '.join(cmd)}",
            file=sys.stderr,
        )
        return subprocess.CompletedProcess(cmd, 1, "", "")


def _write(text: str, *, to_stderr: bool = False) -> None:
    """Print *text* verbatim, adding a trailing newline only when missing."""
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    print(text, end="", file=sys.stderr if to_stderr else sys.stdout)


def _print_pip_output(proc: subprocess.CompletedProcess[str]) -> None:
    """Show captured pip output on the stream pip itself would have used."""
    _write(proc.stdout)
    _write(proc.stderr, to_stderr=True)


def _pip_output_text(proc: subprocess.CompletedProcess[str]) -> str:
    """Combine captured stdout/stderr into one string for marker matching."""
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


# ---------------------------------------------------------------------------
# Environment detection (stdlib only, no subprocesses)
# ---------------------------------------------------------------------------


def _running_under_virtualenv() -> bool:
    """True inside any PEP 405 virtualenv (``venv``, ``virtualenv``, pipx).

    Same check pip uses: a virtualenv overrides ``sys.prefix`` but leaves
    ``sys.base_prefix`` pointing at the interpreter it was created from.
    """
    return sys.prefix != sys.base_prefix


def _has_externally_managed_marker(stdlib_dir: str) -> bool:
    """True when *stdlib_dir* contains pip's PEP 668 marker file."""
    return bool(stdlib_dir) and os.path.isfile(os.path.join(stdlib_dir, PEP668_MARKER))


def _is_externally_managed() -> bool:
    """True when pip would refuse to install here because of PEP 668.

    Mirrors ``pip._internal.utils.misc.check_externally_managed``: virtualenvs
    are never externally managed (pip skips the check there), otherwise the
    ``EXTERNALLY-MANAGED`` marker lives in the interpreter's stdlib directory.
    """
    if _running_under_virtualenv():
        return False
    return _has_externally_managed_marker(sysconfig.get_path("stdlib"))


def _looks_like_pipx_venv(prefix: str) -> bool:
    """True when *prefix* is a pipx venv (``.../pipx/venvs/<package>``).

    pipx keeps every app in its own venv under ``<pipx home>/venvs/``, so the
    two adjacent path components are the reliable signal.  Both separators are
    accepted so the check also works on Windows-style paths.
    """
    parts = [part.casefold() for part in prefix.replace("\\", "/").split("/") if part]
    return any(
        first == "pipx" and second == "venvs" for first, second in pairwise(parts)
    )


def _is_pipx_managed() -> bool:
    """True when the currently running Harbor lives in a pipx-managed venv."""
    return _looks_like_pipx_venv(sys.prefix)


def _find_pipx() -> str | None:
    """Return the pipx executable, or ``None`` when it cannot be located.

    ``pipx`` normally lands in ``~/.local/bin``, which launchers, desktop
    shortcuts and some shells leave off ``PATH``; fall back to pipx's default
    install location instead of failing just because ``PATH`` is minimal.
    """
    found = shutil.which("pipx")
    if found:
        return found
    exe = "pipx.exe" if os.name == "nt" else "pipx"
    candidate = Path.home() / ".local" / "bin" / exe
    return str(candidate) if candidate.is_file() else None


def _run_pipx_upgrade() -> int:
    """Upgrade a pipx-managed Harbor by running ``pipx upgrade harbor``.

    pipx upgrades a git install by *replacing* the virtualenv: it renames the
    old venv into the trash and builds a fresh one at the same path.  On POSIX
    that works while Harbor itself is executing from that venv (running
    processes keep their inodes); on Windows a directory containing a running
    executable cannot be renamed, so there the command is handed to the user
    instead of failing half-way.
    """
    pipx = _find_pipx()
    if pipx is None:
        _print_pipx_update_help()
        return 1

    if os.name == "nt":
        print(
            "Harbor is managed by pipx.  pipx replaces Harbor's virtualenv when\n"
            "upgrading a git install, which Windows cannot do while Harbor is\n"
            "running from it.  Run this in a new terminal:\n"
            f"  → {PIPX_UPGRADE_COMMAND}\n"
            f"  → or force a reinstall:  {PIPX_REINSTALL_COMMAND}",
            file=sys.stderr,
        )
        return 1

    print("Upgrading Harbor via pipx...")
    print(f"  $ {pipx} upgrade {PACKAGE_NAME}")
    print()
    try:
        # Inherit stdio: pipx clones and builds for a while, and it may need to
        # answer a git credential prompt, which only works with a real TTY.
        proc = subprocess.run([pipx, "upgrade", PACKAGE_NAME], check=False)
    except FileNotFoundError:
        print("error: pipx is not available on PATH.", file=sys.stderr)
        _print_pipx_update_help()
        return 1
    except OSError as exc:
        print(f"error: could not run pipx: {exc}", file=sys.stderr)
        _print_pipx_update_help()
        return 1

    if proc.returncode == 0:
        print()
        print("✓ Harbor has been updated.")
        print(
            "  If Harbor is currently running, restart it for changes to take effect:\n"
            "       harbor stop && harbor start"
        )
        return 0

    print(file=sys.stderr)
    print(f"✗ pipx upgrade failed (exit {proc.returncode}).", file=sys.stderr)
    print(
        f"  → Try a forced reinstall from GitHub:  {PIPX_REINSTALL_COMMAND}",
        file=sys.stderr,
    )
    return proc.returncode or 1


def _detect_install_kind() -> str:
    """Try to figure out how Harbor is currently installed.

    Returns one of:
    - ``"git+https"`` — installed from a GitHub git URL (the normal case)
    - ``"editable"``  — installed in editable / dev mode (``pip install -e .``)
    - ``"unknown"``   — couldn't determine, or installed some other way
    """
    try:
        import harbor  # noqa: F401
    except ImportError:
        return "unknown"

    harbor_path = os.path.dirname(os.path.abspath(__file__))

    # Editable install: the source tree is a git repo on disk.
    # We detect this by looking for a .git directory near the package.
    # src/harbor/selfmanage.py -> src/harbor -> src -> <project root>
    project_root = os.path.dirname(os.path.dirname(harbor_path))
    if os.path.isdir(os.path.join(project_root, ".git")):
        # But wait — if it's installed via git+https, pip copies the files,
        # it doesn't leave a .git dir.  So a .git dir here strongly suggests
        # an editable install from a local clone.
        return "editable"

    # Check pip's metadata for the direct URL reference.
    # pip stores the original URL in direct_url.json for PEP 610 installs.
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except ImportError:
        # Python < 3.10 fallback — unlikely, since harbor requires >=3.10
        return "unknown"

    try:
        dist = distribution(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"

    direct_url = dist.read_text("direct_url.json")
    if direct_url and "github.com/ginkgohat/harbor" in direct_url:
        return "git+https"

    # Fallback: if it's an egg-link or .dist-info without direct_url,
    # we still try the GitHub URL — it's the only supported source anyway.
    return "unknown"


# ---------------------------------------------------------------------------
# Guidance for environments where pip must not be used
# ---------------------------------------------------------------------------


def _print_editable_update_help() -> None:
    """Explain why an editable checkout can't self-update."""
    print(
        "Harbor appears to be installed in editable (dev) mode.\n"
        "Self-update is not available for editable installs.\n"
        "  → Update with:  git -C <harbor-repo> pull\n"
        "  → Then re-run:  make dev",
        file=sys.stderr,
    )


def _print_pipx_update_help() -> None:
    """Tell the user to upgrade through pipx instead of pip."""
    print(
        "Harbor is managed by pipx, so pip must not modify its environment.\n"
        f"  → Upgrade with:  {PIPX_UPGRADE_COMMAND}\n"
        f"  → Force a reinstall from GitHub:  {PIPX_REINSTALL_COMMAND}",
        file=sys.stderr,
    )


def _print_pipx_uninstall_help() -> None:
    """Tell the user to uninstall through pipx instead of pip."""
    print(
        "Harbor is managed by pipx, so pip must not remove it from this environment.\n"
        f"  → Uninstall with:  {PIPX_UNINSTALL_COMMAND}",
        file=sys.stderr,
    )


def _print_pep668_update_help() -> None:
    """Explain PEP 668 and offer executable ways to install/upgrade."""
    print(
        "This Python is externally managed (PEP 668), so pip cannot install into it.\n"
        "Use one of:\n"
        f"  • pipx (recommended):  {PIPX_INSTALL_COMMAND}\n"
        f"                        {PIPX_UPGRADE_COMMAND}\n"
        "  • a virtualenv:        python3 -m venv ~/.venvs/harbor\n"
        f'                        ~/.venvs/harbor/bin/pip install --upgrade "{GITHUB_INSTALL_URL}"\n'
        "                        (use .venvs/harbor/Scripts/pip on Windows)\n"
        "  • last resort:         python3 -m pip install --upgrade --user "
        f'--break-system-packages "{GITHUB_INSTALL_URL}"\n'
        "    --break-system-packages can break OS/Homebrew-provided packages.",
        file=sys.stderr,
    )


def _print_pep668_uninstall_help() -> None:
    """Explain how to uninstall when system pip is PEP 668-managed."""
    print(
        "This Python is externally managed (PEP 668), so pip cannot remove\n"
        "packages from it.\n"
        "Use one of:\n"
        f"  • if Harbor was installed with pipx:  {PIPX_UNINSTALL_COMMAND}\n"
        "  • otherwise, use the same interpreter that installed it:\n"
        f"      python3 -m pip uninstall --break-system-packages {PACKAGE_NAME}",
        file=sys.stderr,
    )


def _print_pip_failure_help(output: str, action: _Action) -> None:
    """Print actionable guidance after pip itself failed.

    *output* is pip's captured stdout + stderr; *action* is what the user was
    trying to do, so the suggested command matches their intent.
    """
    lowered = output.casefold()
    if any(marker in lowered for marker in _PEP668_OUTPUT_MARKERS):
        # pip refused even though the pre-check didn't catch it (e.g. a
        # virtualenv whose base interpreter is managed, or a pip version with
        # different detection rules) — show the same PEP 668 guidance.
        if action == "update":
            _print_pep668_update_help()
        else:
            _print_pep668_uninstall_help()
        return

    if any(marker in lowered for marker in _INSTALL_FAILURE_MARKERS):
        print(
            "pip could not build/install Harbor from the GitHub URL.\n"
            "  • Check network or proxy access to github.com (git clone must work).\n"
            "  • If Harbor is managed by pipx, use:  " + PIPX_UPGRADE_COMMAND,
            file=sys.stderr,
        )
        return

    print(
        "If Harbor was installed with pipx, manage it there instead:\n"
        f"  {PIPX_UPGRADE_COMMAND if action == 'update' else PIPX_UNINSTALL_COMMAND}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_self_update() -> int:
    """Upgrade Harbor to the latest version from GitHub.

    Returns an exit code suitable for ``sys.exit()``.
    """
    if _detect_install_kind() == "editable":
        _print_editable_update_help()
        return 1

    # pipx: never run pip inside its venv — upgrade the app through pipx.
    if _is_pipx_managed():
        return _run_pipx_upgrade()

    # PEP 668: pip would fail here with "externally-managed-environment".
    if _is_externally_managed():
        _print_pep668_update_help()
        return 1

    print("Upgrading Harbor from GitHub...")
    print(f"  $ {sys.executable} -m pip install --upgrade {GITHUB_INSTALL_URL}")
    print()

    proc = _run_pip(["install", "--upgrade", GITHUB_INSTALL_URL])
    _print_pip_output(proc)

    if proc.returncode == 0:
        print()
        print("✓ Harbor has been updated.")
        print(
            "  If Harbor is currently running, restart it for changes to take effect."
        )
        return 0

    print()
    print("✗ Update failed. See pip output above.", file=sys.stderr)
    _print_pip_failure_help(_pip_output_text(proc), "update")
    return proc.returncode or 1


def cmd_self_uninstall() -> int:
    """Uninstall Harbor from the current Python environment.

    Returns an exit code suitable for ``sys.exit()``.
    """
    print("This will uninstall Harbor from your system.")
    print(f"  Package: {PACKAGE_NAME}")
    print(f"  Python:  {sys.executable}")

    # pipx and PEP 668 environments can't be changed with pip: print the
    # command that actually works (before asking for confirmation, since
    # nothing will be removed here).
    if _is_pipx_managed():
        print("  Manager: pipx")
        print()
        _print_pipx_uninstall_help()
        return 1

    if _is_externally_managed():
        print()
        _print_pep668_uninstall_help()
        return 1

    print()

    # Confirm interactively, unless stdin is not a TTY (e.g. piped).
    if sys.stdin.isatty():
        try:
            answer = input("Are you sure? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("Uninstall cancelled.")
            return 1
        if answer not in ("y", "yes"):
            print("Uninstall cancelled.")
            return 0

    print()
    print(f"  $ {sys.executable} -m pip uninstall -y {PACKAGE_NAME}")
    print()

    proc = _run_pip(["uninstall", "-y", PACKAGE_NAME])
    _print_pip_output(proc)

    if proc.returncode == 0:
        print()
        print("✓ Harbor has been uninstalled.")
        return 0

    print()
    print("✗ Uninstall failed. See pip output above.", file=sys.stderr)
    _print_pip_failure_help(_pip_output_text(proc), "uninstall")
    return proc.returncode or 1
