"""Property-based tests for harbor.scanner using hypothesis.

These tests verify core invariants of ``find_repos`` and ``scan_roots``
against randomly-generated directory trees, giving broader coverage than
hand-written cases.

Invariants tested:
  1. Every returned repo path is inside the scanned root (no escape).
  2. Returned paths are unique.
  3. scan_roots dedupes repos with the same realpath across overlapping roots.
  4. Dangling worktree pointers (gitdir: → nonexistent) are NOT detected.
  5. A repo directly at the root (depth 0) is always found.
"""

import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from harbor.scanner import find_repos, scan_roots

# ---------------------------------------------------------------------------
# Strategy: generate lists of relative directory paths
# ---------------------------------------------------------------------------

# Windows reserved device names cannot be used as file/directory names
# (case-insensitive). Exclude them so hypothesis never generates a path
# that os.makedirs rejects on Windows (e.g. "NUL", "CON", "COM1").
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Generate a path component: a short alphanumeric name
_path_component = st.text(
    min_size=1,
    max_size=12,
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
).filter(lambda s: s.upper() not in _WINDOWS_RESERVED)

# Generate a relative path like "a/b/c" (0-4 components deep)
_relative_path = st.lists(_path_component, min_size=0, max_size=4).map(
    lambda parts: "/".join(parts)
)


# ---------------------------------------------------------------------------
# Helper: create a .git directory (real repo)
# ---------------------------------------------------------------------------


def _make_repo(root, rel_path):
    """Create a git repo at *root* / *rel_path* (with a real .git dir)."""
    repo_dir = os.path.join(root, rel_path) if rel_path else root
    os.makedirs(repo_dir, exist_ok=True)
    git_dir = os.path.join(repo_dir, ".git")
    os.makedirs(git_dir, exist_ok=True)
    with open(os.path.join(git_dir, "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")


def _make_broken_worktree(root, rel_path):
    """Create a .git *file* pointing to a nonexistent git dir (dangling)."""
    repo_dir = os.path.join(root, rel_path) if rel_path else root
    os.makedirs(repo_dir, exist_ok=True)
    with open(os.path.join(repo_dir, ".git"), "w") as f:
        f.write("gitdir: /nonexistent/ghost.git\n")


def _make_plain_dir(root, rel_path):
    """Create a plain directory (no .git)."""
    if rel_path:
        full = os.path.join(root, rel_path)
        os.makedirs(full, exist_ok=True)


# ---------------------------------------------------------------------------
# Property: all returned paths are inside the scanned root
# ---------------------------------------------------------------------------


@given(
    repo_paths=st.lists(_relative_path, min_size=0, max_size=20, unique=True),
    plain_paths=st.lists(_relative_path, min_size=0, max_size=10, unique=True),
)
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_repo_paths_are_inside_root(tmp_path, repo_paths, plain_paths):
    """Every repo returned by find_repos has a path under the scanned root."""
    root = str(tmp_path / "scan_root")
    os.makedirs(root, exist_ok=True)

    for p in repo_paths:
        _make_repo(root, p)
    for p in plain_paths:
        _make_plain_dir(root, p)

    repos = find_repos(root, min_depth=0, max_depth=10, label="test")

    root_real = os.path.realpath(root)
    for repo in repos:
        repo_real = os.path.realpath(repo["path"])
        assert repo_real == root_real or repo_real.startswith(root_real + os.sep), (
            f"repo path {repo_real} escaped from root {root_real}"
        )


# ---------------------------------------------------------------------------
# Property: returned repo paths are unique (no duplicates)
# ---------------------------------------------------------------------------


@given(repo_paths=st.lists(_relative_path, min_size=0, max_size=20, unique=True))
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_repo_paths_are_unique(tmp_path, repo_paths):
    """find_repos never returns the same repo twice."""
    root = str(tmp_path / "scan_root")
    os.makedirs(root, exist_ok=True)

    for p in repo_paths:
        _make_repo(root, p)

    repos = find_repos(root, min_depth=0, max_depth=10, label="test")

    paths = [repo["path"] for repo in repos]
    assert len(paths) == len(set(paths)), f"duplicate paths: {paths}"


# ---------------------------------------------------------------------------
# Property: dangling worktree pointers are NOT detected
# ---------------------------------------------------------------------------


@given(worktree_paths=st.lists(_relative_path, min_size=1, max_size=10, unique=True))
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_broken_worktrees_not_detected(tmp_path, worktree_paths):
    """A .git file pointing to a nonexistent git dir is NOT reported as a repo."""
    root = str(tmp_path / "scan_root")
    os.makedirs(root, exist_ok=True)

    for p in worktree_paths:
        _make_broken_worktree(root, p)

    repos = find_repos(root, min_depth=0, max_depth=10, label="test")
    assert len(repos) == 0, f"expected 0 repos for dangling worktrees, got {len(repos)}"


# ---------------------------------------------------------------------------
# Property: scan_roots dedupes repos with the same realpath across roots
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="symlinks not supported on this platform"
)
@given(repo_paths=st.lists(_relative_path, min_size=0, max_size=10, unique=True))
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_scan_roots_dedup_overlapping_roots(tmp_path, repo_paths):
    """When two roots share the same repos (realpath equality), each appears once."""
    root_a = str(tmp_path / "root_a")
    os.makedirs(root_a, exist_ok=True)

    for p in repo_paths:
        _make_repo(root_a, p)

    root_b = str(tmp_path / "root_b")
    # Remove if hypothesis reuses tmp_path and a previous example left root_b
    if os.path.lexists(root_b):
        os.unlink(root_b)
    os.symlink(root_a, root_b)

    repos = scan_roots([(root_a, "A"), (root_b, "B")], min_depth=0, max_depth=10)

    # Each repo should appear exactly once (dedup by realpath)
    realpaths = [os.path.realpath(p) for p in repos]
    assert len(realpaths) == len(set(realpaths)), (
        f"duplicate realpaths in scan_roots result: {len(realpaths)} vs {len(set(realpaths))}"
    )


# ---------------------------------------------------------------------------
# Property: repo at depth 0 (root itself) is always found
# ---------------------------------------------------------------------------


@given(extra_repos=st.lists(_relative_path, min_size=0, max_size=5, unique=True))
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_root_level_repo_found(tmp_path, extra_repos):
    """A repo directly at the scanned root (depth 0) is always included."""
    root = str(tmp_path / "scan_root")
    os.makedirs(root, exist_ok=True)

    # Create repo at the root itself
    _make_repo(root, "")
    for p in extra_repos:
        if p:  # skip empty (that's the root itself)
            _make_repo(root, p)

    repos = find_repos(root, min_depth=1, max_depth=10, label="test")
    repo_paths = {os.path.realpath(r["path"]) for r in repos}
    assert os.path.realpath(root) in repo_paths, (
        f"root-level repo not found; got {repo_paths}"
    )


# ---------------------------------------------------------------------------
# Property: number of found repos <= number of created repos
# ---------------------------------------------------------------------------


@given(repo_paths=st.lists(_relative_path, min_size=0, max_size=20, unique=True))
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_found_repos_leq_created(tmp_path, repo_paths):
    """We never find MORE repos than we created."""
    root = str(tmp_path / "scan_root")
    os.makedirs(root, exist_ok=True)

    # Track unique real repo paths (some may collide via realpath)
    unique_repo_paths = set()
    for p in repo_paths:
        _make_repo(root, p)
        full = os.path.join(root, p) if p else root
        unique_repo_paths.add(os.path.realpath(full))

    repos = find_repos(root, min_depth=0, max_depth=10, label="test")
    assert len(repos) <= len(unique_repo_paths), (
        f"found {len(repos)} repos but created only {len(unique_repo_paths)}"
    )
