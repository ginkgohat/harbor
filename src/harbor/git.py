"""Git operations — thin wrappers around the ``git`` CLI.

This module is **HTTP-agnostic**: it knows nothing about HTTP status codes,
URLs, or request handlers.  All functions return plain Python objects
(dataclasses, dicts, tuples); the server layer is responsible for
translating them into HTTP responses.
"""

import logging
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo dataclass
# ---------------------------------------------------------------------------

@dataclass
class Repo:
    """A single git repository discovered by the scanner.

    Use :meth:`as_dict` when serializing for the frontend.
    """

    name: str
    path: str
    root_label: str = ""
    default_branch: str | None = None

    # Extra scanner-cached attributes can be added via this catch-all.
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """Return a plain dict representation for the frontend."""
        d = asdict(self)
        # Flatten extras into the top level for backwards compatibility.
        extras = d.pop("extras", {})
        d.update(extras)
        return d


@dataclass
class ActionOutcome:
    """Domain-level result of a git action (HTTP-agnostic).

    The server layer translates :attr:`ok` / :attr:`status` into an HTTP
    status code.
    """

    ok: bool
    output: str = ""
    status: str = "ok"  # "ok" | "not_found" | "bad_request" | "skipped"

    def as_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output}

# Default per-command timeout for status/diff/ref queries.  Generous enough
# for large repos, but prevents one hung git process (dead network mount,
# iCloud placeholder file, credential prompt, index.lock contention) from
# blocking an API request forever.
GIT_TIMEOUT = 10
# Network operations (git pull) may legitimately take longer on slow links.
PULL_TIMEOUT = 120


def _git_env():
    """Environment for git subprocesses with interactive prompts disabled.

    Harbor has no TTY, so a credential prompt (HTTPS askpass, Git Credential
    Manager, ...) can only hang until the command timeout.  Failing fast is
    strictly better.  ``GIT_TERMINAL_PROMPT=0`` is git's official switch;
    ``GCM_INTERACTIVE=never`` covers Git Credential Manager (Windows) on a
    best-effort basis.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def run_git(path, *args, timeout=GIT_TIMEOUT):
    """Execute a git command in *path* and return (returncode, stdout, stderr).

    Returns ``rc=None`` when the command times out — callers treat any
    non-zero rc as failure, so a timeout degrades to a safe conservative
    result instead of hanging the request.
    """
    cmd = ["git", "-C", path, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_git_env())
    except subprocess.TimeoutExpired:
        logger.warning("git command timed out after %ss: %s", timeout, " ".join(cmd))
        return None, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def parse_porcelain_v2(text):
    """Parse ``git status --porcelain=v2 --branch`` output into a status dict.

    Pure function (no I/O) so every branch shape can be unit-tested:
      - normal branch: ``# branch.head <name>``, optionally ``# branch.ab +A -B``
      - unborn branch (empty repo): ``# branch.oid (initial)``, no ``branch.ab``
      - detached HEAD: ``# branch.head (detached)``
      - no upstream: no ``# branch.ab`` line at all

    ``dirty`` counts tracked changes only (ordinary ``1 ``, rename ``2 ``,
    unmerged ``u `` entries).  Untracked (``? ``) and ignored (``! ``)
    entries do NOT make a repo dirty — same semantics as the previous
    ``git diff --quiet`` based implementation.
    """
    branch = ""
    detached = False
    ahead = behind = None
    dirty = False

    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            if head == "(detached)":
                detached = True
            else:
                branch = head
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                ahead, behind = int(m.group(1)), int(m.group(2))
        elif line[:2] in ("1 ", "2 ", "u "):
            dirty = True

    return {"branch": branch, "detached": detached, "ahead": ahead, "behind": behind, "dirty": dirty}


def repo_status(repo):
    """Return a dict describing the current state of *repo*.

    All per-repo facts come from a single ``git status --porcelain=v2
    --branch`` subprocess, replacing the previous 4-6 subprocess calls per
    repo.  A failed command degrades to the same conservative answer as
    before (dirty + detached), so a broken repo still surfaces as needing
    attention instead of looking healthy.

    Accepts both :class:`Repo` instances and plain dicts for backwards
    compatibility during the transition.
    """
    path = _repo_path(repo)
    name = _repo_name(repo)
    root_label = repo.root_label if isinstance(repo, Repo) else repo.get("root_label")
    default_branch = repo.default_branch if isinstance(repo, Repo) else repo.get("default_branch")

    rc, out, _ = run_git(path, "status", "--porcelain=v2", "--branch")
    if rc == 0:
        parsed = parse_porcelain_v2(out)
    else:
        parsed = {"branch": "", "detached": True, "ahead": None, "behind": None, "dirty": True}

    branch = parsed["branch"]
    # T-021: scan_roots caches the probe result on the repo record; fall back
    # to a live probe for records built by other paths (tests, actions).
    default = default_branch if default_branch is not None else _default_branch(path)
    return {
        "name": name,
        "path": path,
        "root_label": root_label,
        "branch": branch,
        "dirty": parsed["dirty"],
        "detached": parsed["detached"],
        "is_main": bool(branch) and branch == default,
        "ahead": parsed["ahead"],
        "behind": parsed["behind"],
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


def _repo_path(repo) -> str:
    """Extract the filesystem path from a repo (dataclass or dict)."""
    if isinstance(repo, Repo):
        return repo.path
    return repo["path"]


def _repo_name(repo) -> str:
    """Extract the display name from a repo (dataclass or dict)."""
    if isinstance(repo, Repo):
        return repo.name
    return repo["name"]


def safe_pull_check(path: str):
    """Check whether it is safe to pull *path*.

    Returns ``(can_pull, reason)`` where *can_pull* is True when the repo
    is on a branch with a clean working tree, and *reason* is a human
    readable explanation when *can_pull* is False.

    Shared between :func:`pull_one` (used by pull-all jobs) and
    :func:`do_action` (used by per-card pull) so the skip logic never
    drifts apart.
    """
    rc, _, _ = run_git(path, "diff", "--quiet")
    if rc != 0:
        return False, "uncommitted changes (unstaged)"
    rc, _, _ = run_git(path, "diff", "--cached", "--quiet")
    if rc != 0:
        return False, "uncommitted changes (staged)"
    _, branch_out, _ = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    if branch_out.strip() == "":
        return False, "detached HEAD"
    return True, ""


def pull_one(repo, q):
    """Pull a single repo, pushing progress events to *q*."""
    name = _repo_name(repo)
    path = _repo_path(repo)
    q.put({"repo": name, "status": "running"})

    can_pull, reason = safe_pull_check(path)
    if not can_pull:
        q.put({"repo": name, "status": "skipped", "message": reason})
        return

    rc, out, err = run_git(path, "pull", "--ff-only", timeout=PULL_TIMEOUT)
    if rc == 0:
        q.put({"repo": name, "status": "success", "message": (out.strip() or "already up to date")})
    else:
        q.put({"repo": name, "status": "failed", "message": (out + err).strip()})


# Diff payloads go straight into memory and the DOM; cap them so a
# lockfile-scale diff can't freeze the page (T-041).
MAX_DIFF_BYTES = 512 * 1024


def get_diff(path, repos):
    """Return tracked diff and untracked files for a repo.

    The tracked diff is truncated at MAX_DIFF_BYTES (cut at a line boundary)
    with ``truncated: True`` so the frontend can show a notice instead of
    choking on the full payload.
    """
    repo = repos.get(path)
    if not repo:
        return None
    repo_path = _repo_path(repo)
    _, tracked_diff, _ = run_git(repo_path, "diff", "HEAD", "--", ".")
    _, status_out, _ = run_git(repo_path, "status", "--porcelain")
    untracked = [line[3:] for line in status_out.splitlines() if line.startswith("??")]
    truncated = False
    encoded = tracked_diff.encode("utf-8", errors="replace")
    if len(encoded) > MAX_DIFF_BYTES:
        # decode with errors="ignore" in case the byte cut split a UTF-8 char
        cut = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        tracked_diff = cut[: cut.rfind("\n") + 1] or cut
        truncated = True
    return {"diff": tracked_diff, "untracked": untracked, "truncated": truncated}


def do_action(path: str, action: str, repos) -> ActionOutcome:
    """Execute a named action on a repo.

    Returns an :class:`ActionOutcome` with ``ok``, ``output``, and
    ``status`` fields.  **HTTP-agnostic** — the server layer decides what
    status code to return based on ``outcome.status``.
    """
    repo = repos.get(path)
    if not repo:
        return ActionOutcome(ok=False, output="unknown repo", status="not_found")
    repo_path = _repo_path(repo)

    if action == "pull":
        can_pull, reason = safe_pull_check(repo_path)
        if not can_pull:
            return ActionOutcome(ok=False, output=f"skipped: {reason}", status="skipped")
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
            return ActionOutcome(ok=False, output="no default branch found", status="skipped")
        rc, out, err = run_git(repo_path, "checkout", target)
    elif action == "open-vscode":
        code_bin = shutil.which("code")
        if not code_bin:
            return ActionOutcome(
                ok=False,
                output="VS Code 'code' command not found. Run 'Shell Command: Install code command in PATH' from VS Code first.",
                status="skipped",
            )
        subprocess.Popen([code_bin, repo_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ActionOutcome(ok=True, output=f"code {repo_path}", status="ok")
    else:
        return ActionOutcome(ok=False, output=f"unknown action: {action}", status="bad_request")

    return ActionOutcome(ok=(rc == 0), output=(out + err).strip(), status="ok")


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
