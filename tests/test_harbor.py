"""Basic tests for Harbor."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from harbor.__main__ import _create_server
from harbor.config import _toml_str, load_config, resolve_roots, save_config
from harbor.git import (
    ActionOutcome,
    do_action,
    parse_porcelain_v2,
    repo_status,
    run_git,
)
from harbor.scanner import find_repos, scan_roots
from harbor.server import Handler

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


# ---------------------------------------------------------------------------
# T-001 / T-080 — worktree .git file detection + broken pointer skipping
# ---------------------------------------------------------------------------

def test_find_repos_worktree(tmp_path):
    """A repo where .git is a file (worktree / submodule) is discovered."""
    main = tmp_path / "main"
    init_repo(main)
    # Simulate a worktree: .git is a file pointing to a real gitdir
    wt = tmp_path / "worktree-repo"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git'}\n")
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "worktree-repo" in names


def test_find_repos_git_file_absolute_target(tmp_path):
    """A .git file with an absolute gitdir: path resolves correctly."""
    main = tmp_path / "main"
    init_repo(main)
    # Absolute target
    wt = tmp_path / "abs-wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main.resolve() / '.git'}\n")
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "abs-wt" in names


def test_find_repos_git_file_relative_target(tmp_path):
    """A .git file with a relative gitdir: path resolves relative to the file."""
    main = tmp_path / "main"
    init_repo(main)
    wt = tmp_path / "rel-wt"
    wt.mkdir()
    # Relative target from the .git file's directory
    (wt / ".git").write_text("gitdir: ../main/.git\n")
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "rel-wt" in names


def test_find_repos_broken_git_file_skipped(tmp_path):
    """A .git file whose gitdir target does not exist is NOT collected (T-080)."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / ".git").write_text("gitdir: /nonexistent/path/.git\n")
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "broken" not in names


def test_find_repos_malformed_git_file_skipped(tmp_path):
    """A .git file with no valid gitdir: line is skipped gracefully."""
    d = tmp_path / "malformed"
    d.mkdir()
    (d / ".git").write_text("not a gitdir pointer at all\n")
    repos = find_repos(str(tmp_path), min_depth=1, max_depth=2)
    names = [r["name"] for r in repos]
    assert "malformed" not in names



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
# T-021 — scan_roots caches default_branch on each repo record
# ---------------------------------------------------------------------------

def test_scan_roots_caches_default_branch(tmp_path):
    d = tmp_path / "work"
    init_repo(d / "project-a")
    init_repo(d / "project-b")
    repos = scan_roots([(str(d), "Work")], min_depth=1, max_depth=2)
    for repo in repos.values():
        assert "default_branch" in repo
        assert repo["default_branch"] == "main"


def test_repo_status_uses_cached_default_branch(tmp_path):
    init_repo(tmp_path / "r")
    repo = {
        "name": "r",
        "path": str(tmp_path / "r"),
        "default_branch": "main",
    }
    status = repo_status(repo)
    assert status["is_main"] is True
    # Cached value is used even if it's non-standard
    repo["default_branch"] = "trunk"
    status2 = repo_status(repo)
    # Branch is actually "main", cached default is "trunk" → is_main False
    assert status2["is_main"] is False


def test_repo_status_cached_default_branch_miss(tmp_path):
    """If default_branch is not in the record, repo_status probes live."""
    init_repo(tmp_path / "r")
    repo = {"name": "r", "path": str(tmp_path / "r")}
    status = repo_status(repo)
    assert status["is_main"] is True


# ---------------------------------------------------------------------------
# T-020 — parse_porcelain_v2 parser unit tests
# ---------------------------------------------------------------------------

def test_parse_porcelain_v2_normal_branch_with_upstream():
    text = (
        "# branch.oid abc123\n"
        "# branch.head main\n"
        "# branch.upstream origin/main\n"
        "# branch.ab +2 -3\n"
        "1 .M N... 100644 100644 100644 abc def file.py\n"
    )
    r = parse_porcelain_v2(text)
    assert r["branch"] == "main"
    assert r["detached"] is False
    assert r["ahead"] == 2
    assert r["behind"] == 3
    assert r["dirty"] is True


def test_parse_porcelain_v2_unborn_branch():
    """Empty repo: oid is (initial), no branch.ab line."""
    text = "# branch.oid (initial)\n# branch.head main\n"
    r = parse_porcelain_v2(text)
    assert r["branch"] == "main"
    assert r["detached"] is False
    assert r["ahead"] is None
    assert r["behind"] is None
    assert r["dirty"] is False


def test_parse_porcelain_v2_detached_head():
    text = "# branch.oid abc123\n# branch.head (detached)\n"
    r = parse_porcelain_v2(text)
    assert r["branch"] == ""
    assert r["detached"] is True
    assert r["ahead"] is None
    assert r["behind"] is None


def test_parse_porcelain_v2_no_upstream():
    """Normal branch but no upstream configured → no branch.ab line."""
    text = "# branch.oid abc123\n# branch.head feature\n"
    r = parse_porcelain_v2(text)
    assert r["branch"] == "feature"
    assert r["detached"] is False
    assert r["ahead"] is None
    assert r["behind"] is None


def test_parse_porcelain_v2_dirty_variants():
    """Dirty tracks 1, 2, and u entries; ? (untracked) does NOT make dirty."""
    text = (
        "1 .M N... 100644 100644 100644 a b tracked.py\n"
        "? untracked.py\n"
        "! ignored.py\n"
    )
    r = parse_porcelain_v2(text)
    assert r["dirty"] is True

    r2 = parse_porcelain_v2("? new.txt\n? another.txt\n")
    assert r2["dirty"] is False


def test_repo_status_parity_with_legacy(tmp_path):
    """New porcelain=v2 implementation should match legacy semantics for
    the key fields (branch, dirty, detached, is_main)."""
    import subprocess
    init_repo(tmp_path / "r")
    repo = {"name": "r", "path": str(tmp_path / "r")}

    # Clean state
    status = repo_status(repo)
    assert status["dirty"] is False
    assert status["branch"] == "main"
    assert status["detached"] is False
    assert status["is_main"] is True

    # Make it dirty
    (tmp_path / "r" / "README.md").write_text("modified")
    status = repo_status(repo)
    assert status["dirty"] is True

    # Detach HEAD
    subprocess.run(
        ["git", "-C", str(tmp_path / "r"), "checkout", "--detach"],
        check=True, capture_output=True,
    )
    status = repo_status(repo)
    assert status["detached"] is True
    assert status["branch"] == ""


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
    outcome = do_action("nonexistent", "pull", {})
    assert isinstance(outcome, ActionOutcome)
    assert outcome.status == "not_found"
    assert outcome.ok is False


def test_do_action_unknown_action(tmp_path):
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "bogus", repos)
    assert outcome.status == "bad_request"


def test_do_action_pull_no_upstream(tmp_path):
    """Pull without an upstream is not an error — it just reports the failure."""
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "pull", repos)
    # Pull skipped or failed — no upstream configured.
    assert outcome.status in ("ok", "skipped")
    assert "upstream" in outcome.output.lower() or "no remote" in outcome.output.lower() or not outcome.ok


def test_do_action_stash(tmp_path):
    init_repo(tmp_path / "r")
    (tmp_path / "r" / "dirty.txt").write_text("unstaged")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "stash", repos)
    assert outcome.ok is True
    # After stash the repo should be clean
    status = repo_status(repos[path])
    assert status["dirty"] is False


def test_do_action_discard(tmp_path):
    init_repo(tmp_path / "r")
    (tmp_path / "r" / "junk.txt").write_text("to be discarded")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "discard", repos)
    assert outcome.ok is True
    assert not (tmp_path / "r" / "junk.txt").exists()


def test_do_action_checkout_main(tmp_path):
    init_repo(tmp_path / "r")
    # Create a feature branch
    subprocess.run(["git", "-C", str(tmp_path / "r"), "checkout", "-b", "feature"], check=True, capture_output=True)
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "checkout-main", repos)
    assert outcome.ok is True
    # Verify we're back on main
    status = repo_status(repos[path])
    assert status["branch"] == "main"


def test_run_git():
    with tempfile.TemporaryDirectory() as d:
        init_repo(Path(d))
        rc, out, _ = run_git(d, "rev-parse", "--abbrev-ref", "HEAD")
        assert rc == 0
        assert out.strip() == "main"


def test_run_git_disables_interactive_prompts():
    """T-013: every git subprocess runs with GIT_TERMINAL_PROMPT=0."""
    import subprocess
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as d:
        init_repo(Path(d))
        with patch("subprocess.run") as mock_run:
            mock_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="main", stderr=""
            )
            mock_run.return_value = mock_result
            run_git(d, "rev-parse", "--abbrev-ref", "HEAD")
            # Verify env was passed with the prompt-disabling vars
            call_kwargs = mock_run.call_args[1]
            env = call_kwargs.get("env", {})
            assert env.get("GIT_TERMINAL_PROMPT") == "0"
            assert env.get("GCM_INTERACTIVE") == "never"


# ---------------------------------------------------------------------------
# T-041 — diff truncation
# ---------------------------------------------------------------------------

def _make_repo_with_big_diff(tmp_path):
    init_repo(tmp_path / "r")
    # Write a big file so the diff exceeds the cap
    big = "x" * 100 + "\n"
    (tmp_path / "r" / "big.txt").write_text(big * 6000)  # ~600KB
    repos = {str(tmp_path / "r"): {"name": "r", "path": str(tmp_path / "r")}}
    return repos


def test_get_diff_small_not_truncated(tmp_path):
    init_repo(tmp_path / "r")
    # Modify a tracked file so `git diff HEAD` produces output
    (tmp_path / "r" / "README.md").write_text("modified content\n")
    repos = {str(tmp_path / "r"): {"name": "r", "path": str(tmp_path / "r")}}
    from harbor.git import get_diff
    result = get_diff(str(tmp_path / "r"), repos)
    assert result is not None
    assert result["truncated"] is False
    assert "README.md" in result["diff"]


def test_get_diff_truncates_large_diff(tmp_path):
    from harbor.git import get_diff
    init_repo(tmp_path / "r")
    # Modify tracked README to be huge (exceeds 512KB)
    big_line = "x" * 100 + "\n"
    (tmp_path / "r" / "README.md").write_text(big_line * 6000)  # ~600KB diff
    repos = {str(tmp_path / "r"): {"name": "r", "path": str(tmp_path / "r")}}
    result = get_diff(str(tmp_path / "r"), repos)
    assert result is not None
    assert result["truncated"] is True
    assert len(result["diff"].encode("utf-8")) <= 512 * 1024


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path):
    """tomli_w produces valid TOML that tomllib can read back unchanged."""
    config_path = tmp_path / "config.toml"
    config = {
        "roots": [
            {"path": "~/work", "label": "Work"},
            {"path": "~/personal"},
            # Edge case: path with backslashes and quotes
            {"path": 'C:\\Users\\test "quotes"', "label": 'say "hi"'},
        ],
        "port": 9999,
        "min_depth": 1,
        "max_depth": 5,
    }
    save_config(str(config_path), config)
    loaded = load_config(str(config_path))
    assert loaded["port"] == 9999
    assert loaded["min_depth"] == 1
    assert loaded["max_depth"] == 5
    assert len(loaded["roots"]) == 3
    assert loaded["roots"][0]["path"] == "~/work"
    assert loaded["roots"][0]["label"] == "Work"
    assert loaded["roots"][2]["path"] == 'C:\\Users\\test "quotes"'
    assert loaded["roots"][2]["label"] == 'say "hi"'


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


def test_migrate_legacy_config(tmp_path, monkeypatch):
    """Legacy config at ~/.config/harbor/config.toml is migrated to the platformdirs path."""
    from harbor import config as config_mod

    # Set up a fake legacy dir and a fake new config path
    legacy_dir = tmp_path / "legacy" / "harbor"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "config.toml"
    legacy_path.write_text('port = 4242\nmin_depth = 2\n')

    new_dir = tmp_path / "new" / "harbor"
    new_path = new_dir / "config.toml"

    monkeypatch.setattr(config_mod, "_LEGACY_CONFIG_PATH", legacy_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", new_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", new_dir)

    # load_config from the new path should auto-migrate
    loaded = load_config(str(new_path))
    assert loaded is not None
    assert loaded["port"] == 4242
    assert loaded["min_depth"] == 2

    # New file exists, legacy file is gone, backup remains
    assert new_path.is_file()
    assert not legacy_path.is_file()
    assert legacy_path.with_suffix(".bak").is_file()


def test_migrate_legacy_noop_when_no_legacy(tmp_path, monkeypatch):
    """If no legacy config exists, load_config just returns None."""
    from harbor import config as config_mod

    new_dir = tmp_path / "new" / "harbor"
    new_path = new_dir / "config.toml"
    legacy_dir = tmp_path / "nonexistent"
    legacy_path = legacy_dir / "config.toml"

    monkeypatch.setattr(config_mod, "_LEGACY_CONFIG_PATH", legacy_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", new_path)

    loaded = load_config(str(new_path))
    assert loaded is None
    assert not new_path.is_file()


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


# ---------------------------------------------------------------------------
# __main__ — port-in-use error handling
# ---------------------------------------------------------------------------


def test_create_server_port_in_use_exits_with_friendly_message(caplog):
    """When the port is already in use, _create_server exits with code 1
    and logs a helpful error message."""
    # Simulate "Address already in use" OSError (errno 48 on macOS/Linux)
    with patch("harbor.__main__.http.server.ThreadingHTTPServer") as mock_cls:
        mock_cls.side_effect = OSError(48, "Address already in use")
        with pytest.raises(SystemExit) as exc_info:
            _create_server(8765)
        assert exc_info.value.code == 1

    assert "Port 8765 is already in use" in caplog.text
    assert "harbor --port <number>" in caplog.text
    assert "lsof -i :8765" in caplog.text


def test_create_server_port_in_use_by_message_text(caplog):
    """Some platforms may set errno differently but include the message text."""
    with patch("harbor.__main__.http.server.ThreadingHTTPServer") as mock_cls:
        mock_cls.side_effect = OSError(99, "Address already in use")
        with pytest.raises(SystemExit) as exc_info:
            _create_server(8765)
        assert exc_info.value.code == 1

    assert "Port 8765 is already in use" in caplog.text


def test_create_server_other_oserror_is_raised():
    """Unrelated OSErrors should bubble up unchanged."""
    with patch("harbor.__main__.http.server.ThreadingHTTPServer") as mock_cls:
        mock_cls.side_effect = OSError(13, "Permission denied")
        with pytest.raises(OSError) as exc_info:
            _create_server(80)
        assert exc_info.value.errno == 13


def test_create_server_success():
    """Happy path: server is created and returned."""
    with patch("harbor.__main__.http.server.ThreadingHTTPServer") as mock_cls:
        mock_server = mock_cls.return_value
        result = _create_server(8765)
        assert result is mock_server
        mock_cls.assert_called_once_with(("127.0.0.1", 8765), Handler)


# ---------------------------------------------------------------------------
# T-030 — AppState dataclass
# ---------------------------------------------------------------------------

def test_app_state_defaults():
    from harbor.state import AppState
    state = AppState()
    assert state.repos == {}
    assert state.config_path == ""
    assert state.min_depth == 1
    assert state.max_depth == 5
    assert state.cli_min_depth is None
    assert state.cli_max_depth is None


def test_app_state_is_independent():
    """Two AppState instances don't share mutable defaults."""
    from harbor.state import AppState
    a = AppState()
    b = AppState()
    a.repos["/x"] = {"name": "x"}
    assert b.repos == {}


# ---------------------------------------------------------------------------
# T-031 — Repo dataclass + safe_pull_check
# ---------------------------------------------------------------------------

def test_repo_dataclass_as_dict(tmp_path):
    from harbor.git import Repo
    repo = Repo(name="myrepo", path=str(tmp_path), root_label="work")
    d = repo.as_dict()
    assert d["name"] == "myrepo"
    assert d["path"] == str(tmp_path)
    assert d["root_label"] == "work"

def test_repo_status_accepts_dataclass(tmp_path):
    from harbor.git import Repo, repo_status
    init_repo(tmp_path / "r")
    repo = Repo(name="r", path=str(tmp_path / "r"), root_label="test")
    status = repo_status(repo)
    assert status["name"] == "r"
    assert status["root_label"] == "test"
    assert "branch" in status


def test_safe_pull_check_clean_repo(tmp_path):
    from harbor.git import safe_pull_check
    init_repo(tmp_path / "r")
    can, reason = safe_pull_check(str(tmp_path / "r"))
    assert can is True
    assert reason == ""


def test_safe_pull_check_dirty_repo(tmp_path):
    from harbor.git import safe_pull_check
    init_repo(tmp_path / "r")
    # Modify a tracked file (init_repo creates README.md)
    (tmp_path / "r" / "README.md").write_text("modified\n")
    can, reason = safe_pull_check(str(tmp_path / "r"))
    assert can is False
    assert "uncommitted changes" in reason


def test_safe_pull_check_detached(tmp_path):
    from harbor.git import safe_pull_check
    init_repo(tmp_path / "r")
    subprocess.run(
        ["git", "-C", str(tmp_path / "r"), "checkout", "--detach", "HEAD"],
        capture_output=True, check=True,
    )
    can, reason = safe_pull_check(str(tmp_path / "r"))
    assert can is False
    assert "detached" in reason


def test_do_action_returns_action_outcome(tmp_path):
    from harbor.git import ActionOutcome
    outcome = do_action("nonexistent", "pull", {})
    assert isinstance(outcome, ActionOutcome)
    assert outcome.status == "not_found"
    assert outcome.ok is False

def test_do_action_outcome_as_dict(tmp_path):
    init_repo(tmp_path / "r")
    path = str(tmp_path / "r")
    repos = {path: {"name": "r", "path": path}}
    outcome = do_action(path, "stash", repos)
    d = outcome.as_dict()
    assert "ok" in d
    assert "output" in d
    assert isinstance(d["ok"], bool)
