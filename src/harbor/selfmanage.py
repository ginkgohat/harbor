"""Self-update and self-uninstall commands for Harbor.

Harbor is installed from GitHub (the PyPI name ``harbor`` is taken by another
project), so we upgrade / remove the package by invoking pip against the
current Python interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

GITHUB_INSTALL_URL = "git+https://github.com/ginkgohat/harbor.git"
PACKAGE_NAME = "harbor"


def _run_pip(args: Sequence[str]) -> int:
    """Run pip with *args* using the current Python interpreter.

    Returns the process exit code.  Output is inherited so the user sees
    pip's normal progress output.
    """
    cmd = [sys.executable, "-m", "pip", *args]
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print(
            "error: pip is not available for the current Python interpreter.\n"
            f"  tried: {sys.executable} -m pip",
            file=sys.stderr,
        )
        return 1


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
        # Python < 3.8 fallback — unlikely, since harbor requires >=3.9
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


def cmd_self_update() -> int:
    """Upgrade Harbor to the latest version from GitHub.

    Returns an exit code suitable for ``sys.exit()``.
    """
    kind = _detect_install_kind()

    if kind == "editable":
        print(
            "Harbor appears to be installed in editable (dev) mode.\n"
            "Self-update is not available for editable installs.\n"
            "  → Update with:  git -C <harbor-repo> pull",
            file=sys.stderr,
        )
        return 1

    print("Upgrading Harbor from GitHub...")
    print(f"  $ python -m pip install --upgrade {GITHUB_INSTALL_URL}")
    print()

    rc = _run_pip(["install", "--upgrade", GITHUB_INSTALL_URL])

    if rc == 0:
        print()
        print("✓ Harbor has been updated.")
        print("  If Harbor is currently running, restart it for changes to take effect.")
    else:
        print()
        print("✗ Update failed. See pip output above.", file=sys.stderr)

    return rc


def cmd_self_uninstall() -> int:
    """Uninstall Harbor from the current Python environment.

    Returns an exit code suitable for ``sys.exit()``.
    """
    print("This will uninstall Harbor from your system.")
    print(f"  Package: {PACKAGE_NAME}")
    print(f"  Python:  {sys.executable}")
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
    print(f"  $ python -m pip uninstall -y {PACKAGE_NAME}")
    print()

    rc = _run_pip(["uninstall", "-y", PACKAGE_NAME])

    if rc == 0:
        print()
        print("✓ Harbor has been uninstalled.")
    else:
        print()
        print("✗ Uninstall failed. See pip output above.", file=sys.stderr)

    return rc
