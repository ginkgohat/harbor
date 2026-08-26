"""Repository scanner — discovers git repos under one or more root directories."""

import logging
import os

from . import git as git_ops

logger = logging.getLogger(__name__)


def _git_file_target_exists(git_file):
    """Validate a ``.git`` *file* (worktree / submodule pointer).

    Returns True only if the file holds a ``gitdir:`` line whose target
    directory still exists.  A dangling pointer means the git dir was
    removed — collecting it would surface a forever-detached ghost card.
    """
    try:
        with open(git_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("gitdir:"):
                    target = line.split(":", 1)[1].strip()
                    if not os.path.isabs(target):
                        target = os.path.join(os.path.dirname(git_file), target)
                    return os.path.isdir(target)
    except OSError:
        pass
    return False


def find_repos(root, min_depth=1, max_depth=5, label=None):
    """Walk *root* and return every directory that contains a .git marker.

    The marker is either a ``.git`` subdirectory (normal repo) or a ``.git``
    file (worktree / submodule ``gitdir:`` pointer, validated to still
    resolve).  Uses `os.walk` for cross-platform compatibility (Linux,
    macOS, Windows).  Directories are pruned once we exceed *max_depth*.

    The root directory itself is always checked (depth 0), so pointing Harbor
    directly at a repo works.  Subdirectories from depth 1 up to *max_depth*
    are also scanned, which means pointing Harbor at a repo's parent directory
    or grandparent directory all work correctly.

    When a .git directory is found, traversal stops descending into that
    directory — repos inside repos (submodules etc.) are not scanned.

    Args:
        root: The directory to scan.
        min_depth: Minimum directory depth to consider (depth 0 is always checked).
        max_depth: Maximum directory depth to scan.
        label: Optional human-readable label for this root (shown in the UI).

    Returns:
        A list of repo dicts, each with ``name``, ``path``, and ``root_label``.
    """
    root = os.path.realpath(os.path.expanduser(root))
    root_label = label or os.path.basename(root)
    repos = []

    for dirpath, dirnames, _filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1

        # depth 0 (the root itself) always gets checked — if the user pointed
        # Harbor at a repo directly, it should show up regardless of min_depth.
        if 0 < depth < min_depth:
            continue
        if depth > max_depth:
            dirnames.clear()
            continue

        if ".git" in dirnames or (
            ".git" in _filenames
            and _git_file_target_exists(os.path.join(dirpath, ".git"))
        ):
            raw_name = rel if rel != "." else os.path.basename(root)
            # Normalize path separators to forward slash for consistent
            # display across platforms (Linux/macOS/Windows).
            name = raw_name.replace(os.sep, "/")
            repos.append({"name": name, "path": dirpath, "root_label": root_label})
            # Don't descend into a repo — there's nothing useful below.
            dirnames.clear()
            continue

    return sorted(repos, key=lambda r: r["name"])


def scan_roots(roots, min_depth=1, max_depth=5):
    """Scan multiple roots and return a merged repo dict.

    Args:
        roots: A list of ``(path, label)`` tuples.
        min_depth: Minimum directory depth.
        max_depth: Maximum directory depth.

    Returns:
        A dict mapping repo ``path`` to repo dict.  Path is filesystem-unique,
        so repos from different roots never collide even when their display
        ``name`` is the same.  Each value has ``name``, ``path``, and
        ``root_label``.
    """
    all_repos = {}
    for path, label in roots:
        for repo in find_repos(path, min_depth=min_depth, max_depth=max_depth, label=label):
            # Key by path so two roots with same-named children don't shadow
            # each other.  When two roots actually contain the same repo
            # (realpath collision), the first root wins — same as before.
            all_repos.setdefault(repo["path"], repo)
    # T-021: the default branch virtually never changes, so probe it once per
    # scan instead of on every status refresh (saves 1–6 subprocesses per repo
    # per refresh).  Computed after dedup so duplicate paths aren't probed twice.
    for repo in all_repos.values():
        repo["default_branch"] = git_ops._default_branch(repo["path"])
    return all_repos
