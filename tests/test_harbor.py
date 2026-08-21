"""Basic tests for Harbor."""

import os
import subprocess
import tempfile
from pathlib import Path

from harbor.config import _toml_str, load_config, resolve_roots, save_config
from harbor.git import do_action, repo_status, run_git
from harbor.scanner import find_repos, scan_roots

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def init_repo(path, branch="main"):
    """Create a git repo at *path* and return it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-b", branch], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@harbor.local"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Harbor Test"], check=True, capture_output=True)
    # Create an initial commit so we have a real branch
    (path / "README.md").write_text("# test")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def test_find_repos_empty(tmp_path):
    assert find_repos(str(tmp_path), min_depth=0, max_depth=3) == []


def test_find_repos_single(tmp_path):
    init_repo(tmp_path / "foo")
    repos = find_repos(str(tmp_path), min_depth=0, max_depth=3)
    assert len(repos) == 1
    assert repos[0]["name"] == "foo"


def test_find_repos_root_itself_is_repo(tmp_path):
    """If the root directory itself is a git repo, it is found regardless of min_depth."""
    init_repo(tmp_path)  # root itself is a repo (depth 0)
    repos = find_repos(str(tmp_path), min_depth=2, max_depth=3)
    assert len(repos) == 1
    assert repos[0]["name"] == os.path.basename(str(tmp_path))
    assert repos[0]["path"] == str(tmp_path.resolve())


def test_find_repos_respects_depth(tmp_path):
    init_repo(tmp_path / "a" / "b")          # depth 2 — should be found
    init_repo(tmp_path / "x" / "y" / "z")    # depth 3 — should be excluded
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "a/b" in names
    assert "x/y/z" not in names


def test_find_repos_sorted(tmp_path):
    init_repo(tmp_path / "z")
    init_repo(tmp_path / "a")
    init_repo(tmp_path / "m")
    repos = find_repos(str(tmp_path), min_depth=0, max_depth=3)
    names = [r["name"] for r in repos]
    assert names == ["a", "m", "z"]


def test_find_repos_with_label(tmp_path):
    init_repo(tmp_path / "foo")
    repos = find_repos(str(tmp_path), min_depth=0, max_depth=3, label="MyLabel")
    assert repos[0]["root_label"] == "MyLabel"


def test_find_repos_default_label(tmp_path):
    init_repo(tmp_path / "foo")
    repos = find_repos(str(tmp_path), min_depth=0, max_depth=3)
    assert repos[0]["root_label"] == os.path.basename(str(tmp_path))

def test_find_repos_parent_directory(tmp_path):
    """Pointing at a repo's parent directory (depth 1) should find it."""
    init_repo(tmp_path / "myproject")
    repos = find_repos(str(tmp_path))  # uses default min_depth=1
    assert len(repos) == 1
    assert repos[0]["name"] == "myproject"


def test_find_repos_grandparent_directory(tmp_path):
    """Pointing at a repo's grandparent directory (depth 2) should find it."""
    init_repo(tmp_path / "work" / "myproject")
    repos = find_repos(str(tmp_path))  # uses default min_depth=1, max_depth=5
    names = [r["name"] for r in repos]
    assert "work/myproject" in names


def test_find_repos_direct_repo_path(tmp_path):
    """Pointing directly at a repo directory (depth 0) should find it."""
    init_repo(tmp_path / "myproject")
    repos = find_repos(str(tmp_path / "myproject"))
    assert len(repos) == 1
    assert repos[0]["name"] == "myproject"
    assert repos[0]["path"] == str((tmp_path / "myproject").resolve())


def test_find_repos_stops_at_repo(tmp_path):
    """Once a .git dir is found, we should not descend further into that repo."""
    init_repo(tmp_path / "outer")
    # Create a fake repo inside outer (should be ignored because outer is already a repo)
    (tmp_path / "outer" / "subdir" / ".git").mkdir(parents=True)
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=5)
    names = [r["name"] for r in repos]
    assert "outer" in names
    # The nested .git should not be discovered because we stop at outer
    assert "outer/subdir" not in names


def test_find_repos_nested_repos_at_different_levels(tmp_path):
    """Repos at various depths under the root should all be found."""
    init_repo(tmp_path / "a")          # depth 1
    init_repo(tmp_path / "b" / "c")    # depth 2
    init_repo(tmp_path / "d" / "e" / "f")  # depth 3
    repos = find_repos(str(tmp_path))
    names = [r["name"] for r in repos]
    assert "a" in names
    assert "b/c" in names
    assert "d/e/f" in names
    assert len(repos) == 3



def test_scan_roots_multiple(tmp_path):
    d1 = tmp_path / "work"
    d2 = tmp_path / "personal"
    init_repo(d1 / "project-a")
    init_repo(d2 / "project-b")
    repos = scan_roots([(str(d1), "Work"), (str(d2), "Personal")], min_depth=1, max_depth=2)
    assert len(repos) == 2
    # scan_roots now keys by repo path, not name — so two roots with
    # same-named children both appear.
    p1 = str(d1 / "project-a")
    p2 = str(d2 / "project-b")
    assert repos[p1]["root_label"] == "Work"
    assert repos[p2]["root_label"] == "Personal"
    assert repos[p1]["name"] == "project-a"
    assert repos[p2]["name"] == "project-b"


def test_scan_roots_dedup(tmp_path):
    """When two roots contain repos with the same name, both appear (keyed by path)."""
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    init_repo(d1 / "shared")
    init_repo(d2 / "shared")
    repos = scan_roots([(str(d1), "A"), (str(d2), "B")], min_depth=1, max_depth=2)
    # Both roots contribute their own /shared dir; keyed by path so neither
    # shadows the other.
    p1 = str(d1 / "shared")
    p2 = str(d2 / "shared")
    assert len(repos) == 2
    assert p1 in repos
    assert p2 in repos
    assert repos[p1]["root_label"] == "A"
    assert repos[p2]["root_label"] == "B"


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def test_repo_status_clean(tmp_path):
    init_repo(tmp_path / "clean")
    repo = {"name": "clean", "path": str(tmp_path / "clean")}
    status = repo_status(repo)
    assert status["dirty"] is False
    assert status["detached"] is False
    assert status["is_main"] is True
    assert status["branch"] == "main"


def test_repo_status_dirty_tracked(tmp_path):
    init_repo(tmp_path / "dirty")
    # Modify a tracked file — this must show as dirty
    (tmp_path / "dirty" / "README.md").write_text("modified")
    repo = {"name": "dirty", "path": str(tmp_path / "dirty")}
    status = repo_status(repo)
    assert status["dirty"] is True


def test_repo_status_clean_untracked(tmp_path):
    """Untracked files alone do NOT make the repo dirty (they show in diff preview)."""
    init_repo(tmp_path / "clean")
    (tmp_path / "clean" / "new.txt").write_text("hello")
    repo = {"name": "clean", "path": str(tmp_path / "clean")}
    status = repo_status(repo)
    assert status["dirty"] is False  # untracked ≠ dirty in git semantics


def test_repo_status_master_branch(tmp_path):
    init_repo(tmp_path / "master-repo", branch="master")
    repo = {"name": "master-repo", "path": str(tmp_path / "master-repo")}
    status = repo_status(repo)
    assert status["is_main"] is True
    assert status["branch"] == "master"


def test_do_action_unknown_repo():
    code, obj = do_action("nonexistent", "pull", {})
    assert code == 404
    assert obj["ok"] is False


def test_do_action_unknown_action(tmp_path):
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    code, obj = do_action(path, "bogus", repos)
    assert code == 400


def test_do_action_pull_no_upstream(tmp_path):
    """Pull without an upstream is not an error — it just reports the failure."""
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    code, obj = do_action(path, "pull", repos)
    assert code == 200
    # No upstream configured → git pull fails, which is expected
    assert "upstream" in obj["output"].lower() or "no remote" in obj["output"].lower() or not obj["ok"]


def test_do_action_stash(tmp_path):
    init_repo(tmp_path / "r")
    (tmp_path / "r" / "dirty.txt").write_text("unstaged")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    code, obj = do_action(path, "stash", repos)
    assert code == 200
    assert obj["ok"] is True
    # After stash the repo should be clean
    status = repo_status(repos[path])
    assert status["dirty"] is False


def test_do_action_discard(tmp_path):
    init_repo(tmp_path / "r")
    (tmp_path / "r" / "junk.txt").write_text("to be discarded")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    code, obj = do_action(path, "discard", repos)
    assert code == 200
    assert obj["ok"] is True
    assert not (tmp_path / "r" / "junk.txt").exists()


def test_do_action_checkout_main(tmp_path):
    init_repo(tmp_path / "r")
    # Create a feature branch
    subprocess.run(["git", "-C", str(tmp_path / "r"), "checkout", "-b", "feature"], check=True, capture_output=True)
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    code, obj = do_action(path, "checkout-main", repos)
    assert code == 200
    assert obj["ok"] is True
    # Verify we're back on main
    status = repo_status(repos[path])
    assert status["branch"] == "main"


def test_run_git():
    with tempfile.TemporaryDirectory() as d:
        init_repo(Path(d))
        rc, out, _ = run_git(d, "rev-parse", "--abbrev-ref", "HEAD")
        assert rc == 0
        assert out.strip() == "main"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_toml_str():
    assert _toml_str("hello") == '"hello"'
    assert _toml_str('say "hi"') == '"say \\"hi\\""'
    assert _toml_str("a\\b") == '"a\\\\b"'


def test_save_and_load_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config = {
        "roots": [
            {"path": "~/work", "label": "Work"},
            {"path": "~/personal"},
        ],
        "port": 9999,
        "min_depth": 1,
    }
    save_config(str(config_path), config)
    loaded = load_config(str(config_path))
    assert loaded["port"] == 9999
    assert loaded["min_depth"] == 1
    assert len(loaded["roots"]) == 2
    assert loaded["roots"][0]["path"] == "~/work"
    assert loaded["roots"][0]["label"] == "Work"
    assert loaded["roots"][1]["path"] == "~/personal"


def test_resolve_roots_from_config():
    config = {
        "roots": [
            {"path": "/a", "label": "A"},
            {"path": "/b", "label": "B"},
        ]
    }
    roots = resolve_roots([], config)
    assert len(roots) == 2
    assert roots[0] == ("/a", "A")
    assert roots[1] == ("/b", "B")


def test_resolve_roots_cli_overrides():
    config = {"roots": [{"path": "/a", "label": "A"}]}
    roots = resolve_roots(["/x", "/y"], config)
    assert len(roots) == 2
    assert roots[0][0] == "/x"
    assert roots[1][0] == "/y"


def test_resolve_roots_fallback():
    roots = resolve_roots([], None)
    assert len(roots) == 1
    assert roots[0][0] == os.getcwd()
