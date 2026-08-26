"""Git operations — thin wrappers around the ``git`` CLI."""

import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Default per-command timeout for status/diff/ref queries.  Generous enough
# for large repos, but prevents one hung git process (dead network mount,
# iCloud placeholder file, credential prompt, index.lock contention) from
# blocking an API request forever.
GIT_TIMEOUT = 10
# Network operations (git pull) may legitimately take longer on slow links.
PULL_TIMEOUT = 120


def run_git(path, *args, timeout=GIT_TIMEOUT):
    """Execute a git command in *path* and return (returncode, stdout, stderr).

    Returns ``rc=None`` when the command times out — callers treat any
    non-zero rc as failure, so a timeout degrades to a safe conservative
    result instead of hanging the request.
    """
    cmd = ["git", "-C", path, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("git command timed out after %ss: %s", timeout, " ".join(cmd))
        return None, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def repo_status(repo):
    """Return a dict describing the current state of *repo*."""
    path = repo["path"]

    rc_u, _, _ = run_git(path, "diff", "--quiet")
    rc_s, _, _ = run_git(path, "diff", "--cached", "--quiet")
    dirty = rc_u != 0 or rc_s != 0

    _, branch_out, _ = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    branch = branch_out.strip()
    detached = branch == ""
    is_main = branch == _default_branch(path)

    ahead = behind = None
    if not detached:
        rc, out, _ = run_git(
            path, "rev-list", "--left-right", "--count", f"{branch}...{branch}@{{u}}"
        )
        if rc == 0:
            parts = out.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    return {
        "name": repo["name"],
        "path": repo["path"],
        "root_label": repo.get("root_label"),
        "branch": branch,
        "dirty": dirty,
        "detached": detached,
        "is_main": is_main,
        "ahead": ahead,
        "behind": behind,
    }


STATUS_WORKERS = 8


def get_repos_status(repos):
    """Return status for every repo in *repos*, collected concurrently.

    Each repo runs several git subprocesses; running them in a thread pool
    cuts total refresh latency roughly by the worker count and keeps one
    slow repo from extending the tail linearly.
    """
    with ThreadPoolExecutor(max_workers=STATUS_WORKERS) as ex:
        # map() preserves input order, matching the previous serial output.
        return list(ex.map(repo_status, repos.values()))


def pull_one(repo, q):
    """Pull a single repo, pushing progress events to *q*."""
    name = repo["name"]
    path = repo["path"]
    q.put({"repo": name, "status": "running"})

    rc, _, _ = run_git(path, "diff", "--quiet")
    if rc != 0:
        q.put({"repo": name, "status": "skipped", "message": "uncommitted changes (unstaged)"})
        return
    rc, _, _ = run_git(path, "diff", "--cached", "--quiet")
    if rc != 0:
        q.put({"repo": name, "status": "skipped", "message": "uncommitted changes (staged)"})
        return
    _, branch_out, _ = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    if branch_out.strip() == "":
        q.put({"repo": name, "status": "skipped", "message": "detached HEAD"})
        return

    rc, out, err = run_git(path, "pull", "--ff-only", timeout=PULL_TIMEOUT)
    if rc == 0:
        q.put({"repo": name, "status": "success", "message": (out.strip() or "already up to date")})
    else:
        q.put({"repo": name, "status": "failed", "message": (out + err).strip()})


def get_diff(path, repos):
    """Return tracked diff and untracked files for a repo."""
    repo = repos.get(path)
    if not repo:
        return None
    repo_path = repo["path"]
    _, tracked_diff, _ = run_git(repo_path, "diff", "HEAD", "--", ".")
    _, status_out, _ = run_git(repo_path, "status", "--porcelain")
    untracked = [line[3:] for line in status_out.splitlines() if line.startswith("??")]
    return {"diff": tracked_diff, "untracked": untracked}


def do_action(path, action, repos):
    """Execute a named action on a repo. Returns (http_status, result_dict)."""
    repo = repos.get(path)
    if not repo:
        return 404, {"ok": False, "output": "unknown repo"}
    repo_path = repo["path"]

    if action == "pull":
        rc_u, _, _ = run_git(repo_path, "diff", "--quiet")
        rc_s, _, _ = run_git(repo_path, "diff", "--cached", "--quiet")
        if rc_u != 0 or rc_s != 0:
            return 200, {"ok": False, "output": "skipped: uncommitted changes"}
        _, branch_out, _ = run_git(repo_path, "symbolic-ref", "--short", "-q", "HEAD")
        if branch_out.strip() == "":
            return 200, {"ok": False, "output": "skipped: detached HEAD"}
        rc, out, err = run_git(repo_path, "pull", "--ff-only", timeout=PULL_TIMEOUT)
    elif action == "stash":
        rc, out, err = run_git(repo_path, "stash", "push", "-u", "-m", "harbor")
    elif action == "discard":
        rc1, out1, err1 = run_git(repo_path, "checkout", "--", ".")
        rc2, out2, err2 = run_git(repo_path, "clean", "-fd")
        rc, out, err = (rc1 or rc2), out1 + out2, err1 + err2
    elif action == "checkout-main":
        target = _default_branch(repo_path)
        if not target:
            return 200, {"ok": False, "output": "no default branch found"}
        rc, out, err = run_git(repo_path, "checkout", target)
    elif action == "open-vscode":
        code_bin = shutil.which("code")
        if not code_bin:
            return 200, {"ok": False, "output": "VS Code 'code' command not found. Run 'Shell Command: Install code command in PATH' from VS Code first."}
        subprocess.Popen([code_bin, repo_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 200, {"ok": True, "output": f"code {repo_path}"}
    else:
        return 400, {"ok": False, "output": f"unknown action: {action}"}

    return 200, {"ok": rc == 0, "output": (out + err).strip()}


def _default_branch(path):
    """Return the default branch name for a repo, or None.

    Tries ``refs/remotes/origin/HEAD`` first (the canonical answer after a
    ``git clone``); falls back to probing common names.  Returns ``None`` when
    no candidate is found.
    """
    rc, out, _ = run_git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip().split("/", 2)[-1]
    for candidate in ("main", "master", "trunk", "develop", "dev"):
        rc, _, _ = run_git(path, "rev-parse", "--verify", "--quiet", candidate)
        if rc == 0:
            return candidate
    return None
