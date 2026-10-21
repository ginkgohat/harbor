"""Git operations — thin wrappers around the ``git`` CLI.

This module is **HTTP-agnostic**: it knows nothing about HTTP status codes,
URLs, or request handlers.  All functions return plain Python objects
(dataclasses, dicts, tuples); the server layer is responsible for
translating them into HTTP responses.
"""

import logging
import os
import queue
import re
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any

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
    extras: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
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

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output}


# Default per-command timeout for status/diff/ref queries.  Generous enough
# for large repos, but prevents one hung git process (dead network mount,
# iCloud placeholder file, credential prompt, index.lock contention) from
# blocking an API request forever.
GIT_TIMEOUT = 10
# Network operations (git pull / fetch) may legitimately take longer on slow
# links.  Fetches are bounded tighter than pulls: a rescan fans out over every
# repo, so one hanging remote must not hold the refresh button hostage.
PULL_TIMEOUT = 120
FETCH_TIMEOUT = 20
FETCH_WORKERS = 8
# Overall wall-clock cap for one rescan's fetch phase.  Per-repo timeouts alone
# still allow ceil(N/FETCH_WORKERS) * FETCH_TIMEOUT (e.g. 30 repos on a dead
# network -> 80s), which would hold the refresh button long past the frontend's
# 30s budget.  Repos that miss the budget fall back to local refs.
FETCH_BUDGET = 20.0


def _git_env() -> dict[str, str]:
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


def run_git(
    path: str, *args: str, timeout: float = GIT_TIMEOUT
) -> tuple[int | None, str, str]:
    """Execute a git command in *path* and return (returncode, stdout, stderr).

    Returns ``rc=None`` when the command times out — callers treat any
    non-zero rc as failure, so a timeout degrades to a safe conservative
    result instead of hanging the request.
    """
    cmd = ["git", "-C", path, *args]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=_git_env()
        )
    except subprocess.TimeoutExpired:
        logger.warning("git command timed out after %ss: %s", timeout, " ".join(cmd))
        return None, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def parse_porcelain_v2(text: str) -> dict[str, Any]:
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
            head = line[len("# branch.head ") :]
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

    return {
        "branch": branch,
        "detached": detached,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
    }


def repo_status(repo: Repo | dict[str, Any]) -> dict[str, Any]:
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
    default_branch = (
        repo.default_branch if isinstance(repo, Repo) else repo.get("default_branch")
    )

    rc, out, _ = run_git(path, "status", "--porcelain=v2", "--branch")
    if rc == 0:
        parsed = parse_porcelain_v2(out)
    else:
        parsed = {
            "branch": "",
            "detached": True,
            "ahead": None,
            "behind": None,
            "dirty": True,
        }

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

# A single process-wide executor reused across status refreshes.  Creating an
# executor per request under the 30s polling loop would tear down and rebuild
# 8 worker threads every cycle; sharing one avoids that constant churn.
# ThreadPoolExecutor cleans its threads up via atexit, so this needs no
# explicit shutdown.
_EXECUTOR = ThreadPoolExecutor(max_workers=STATUS_WORKERS)


def get_repos_status(repos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return status for every repo in *repos*, collected concurrently.

    Each repo runs several git subprocesses; running them in a thread pool
    cuts total refresh latency roughly by the worker count and keeps one
    slow repo from extending the tail linearly.
    """
    # map() preserves input order, matching the previous serial output.
    return list(_EXECUTOR.map(repo_status, repos.values()))


def _repo_path(repo: Repo | dict[str, Any]) -> str:
    """Extract the filesystem path from a repo (dataclass or dict)."""
    if isinstance(repo, Repo):
        return repo.path
    return str(repo["path"])


def _repo_name(repo: Repo | dict[str, Any]) -> str:
    """Extract the display name from a repo (dataclass or dict)."""
    if isinstance(repo, Repo):
        return repo.name
    return str(repo["name"])


def fetch_remote(path: str, timeout: float = FETCH_TIMEOUT) -> str | None:
    """Run ``git fetch --quiet`` in *path*; return a short error string or None.

    ``git status --porcelain=v2 --branch`` derives ahead/behind from the
    *local* remote-tracking refs, so without a fetch a repo whose upstream has
    new commits still looks "up to date" — the dashboard is blind to anything
    that happened on the remote since the last manual ``git pull``/fetch.
    Rescans call this before sampling status so the page reports what actually
    exists upstream, matching what a terminal ``git pull`` would see.

    Failures (no remote, offline, auth) are returned, not raised: local status
    is still useful and one broken remote must not fail the whole refresh.
    """
    rc, out, err = run_git(path, "fetch", "--quiet", timeout=timeout)
    if rc == 0:
        return None
    detail = (err or out).strip() or "git fetch failed"
    # Collapse multi-line git errors into one line for the compact failure log.
    return " ".join(detail.split())[:300]


def fetch_repos(
    repos: dict[str, Any], timeout: float = FETCH_TIMEOUT, budget: float = FETCH_BUDGET
) -> list[str]:
    """Fetch every repo concurrently, returning ``"<path>: <error>"`` failures.

    Never raises: a malformed record, an offline/remote-less/credential-
    prompting repo, or a fetch that overruns the overall *budget* is reported
    as a failure and the rescan continues with local refs.

    *timeout* bounds a single ``git fetch``; *budget* bounds the whole phase in
    wall-clock seconds so a dead network cannot stall the refresh button (or
    the rescan single-flight) for ``ceil(N/FETCH_WORKERS) * timeout``.
    """
    if not repos:
        return []

    def _one(repo: Repo | dict[str, Any]) -> tuple[str, str | None]:
        try:
            path = _repo_path(repo)
        except Exception:  # malformed record — can't even name the repo
            return repr(repo), "invalid repo record (no path)"
        try:
            return path, fetch_remote(path, timeout=timeout)
        except Exception as exc:  # defensive belt-and-braces around run_git
            return path, f"{type(exc).__name__}: {exc}"

    failures: list[str] = []
    pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
    try:
        submitted: list[tuple[Future[tuple[str, str | None]], str]] = []
        for repo in repos.values():
            try:
                label = _repo_path(repo)
            except Exception:
                label = repr(repo)
            try:
                submitted.append((pool.submit(_one, repo), label))
            except Exception as exc:  # e.g. "can't start new thread"
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
        done, not_done = wait([fut for fut, _ in submitted], timeout=budget)
        labels = {id(fut): label for fut, label in submitted}
        for fut in done:
            try:
                path, error = fut.result()
            except Exception as exc:  # cancelled/broken future
                failures.append(
                    f"{labels.get(id(fut), '<unknown>')}: {type(exc).__name__}: {exc}"
                )
            else:
                if error:
                    failures.append(f"{path}: {error}")
        for fut in not_done:
            fut.cancel()
            failures.append(
                f"{labels.get(id(fut), '<unknown>')}: skipped "
                f"(overall fetch budget of {budget:g}s exceeded)"
            )
    except Exception as exc:  # never let the fetch phase break a refresh
        failures.append(f"<fetch phase>: {type(exc).__name__}: {exc}")
    finally:
        # Do not block the rescan on stragglers: their own subprocess timeout
        # still bounds them and the pool threads unwind on their own.
        pool.shutdown(wait=False, cancel_futures=True)
    return failures


def safe_pull_check(path: str) -> tuple[bool, str]:
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


def pull_one(repo: Repo | dict[str, Any], q: queue.Queue[dict[str, Any]]) -> None:
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
        q.put(
            {
                "repo": name,
                "status": "success",
                "message": (out.strip() or "already up to date"),
            }
        )
    else:
        q.put({"repo": name, "status": "failed", "message": (out + err).strip()})


# Diff payloads go straight into memory and the DOM; cap them so a
# lockfile-scale diff can't freeze the page (T-041).
MAX_DIFF_BYTES = 512 * 1024


def get_diff(path: str, repos: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
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
    # ``-z`` disables core.quotePath's C-style quoting, so filenames come back
    # raw (unquoted) with NUL separators.  Splitting on NUL then strips the
    # ``?? `` prefix intact even for paths with spaces / control characters.
    _, status_out, _ = run_git(repo_path, "status", "--porcelain", "-z")
    untracked = [
        token[3:] for token in status_out.split(chr(0)) if token.startswith("?? ")
    ]
    truncated = False
    encoded = tracked_diff.encode("utf-8", errors="replace")
    if len(encoded) > MAX_DIFF_BYTES:
        # decode with errors="ignore" in case the byte cut split a UTF-8 char
        cut = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        tracked_diff = cut[: cut.rfind("\n") + 1] or cut
        truncated = True
    return {"diff": tracked_diff, "untracked": untracked, "truncated": truncated}


def do_action(
    path: str, action: str, repos: dict[str, dict[str, Any]]
) -> ActionOutcome:
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
            return ActionOutcome(
                ok=False, output=f"skipped: {reason}", status="skipped"
            )
        rc, out, err = run_git(repo_path, "pull", "--ff-only", timeout=PULL_TIMEOUT)
    elif action == "stash":
        rc, out, err = run_git(repo_path, "stash", "push", "-u", "-m", "harbor")
    elif action == "discard":
        # Recoverable discard: stash (including untracked) instead of a hard
        # ``checkout -- . && clean -fd``.  Changes leave the working tree but
        # can be restored later with ``git stash apply``.  The message makes
        # these entries distinguishable from normal stash/pause stashes.
        rc, out, err = run_git(
            repo_path, "stash", "push", "-u", "-m", f"harbor:discard {int(time.time())}"
        )
    elif action == "checkout-main":
        target = _default_branch(repo_path)
        if not target:
            return ActionOutcome(
                ok=False, output="no default branch found", status="skipped"
            )
        rc, out, err = run_git(repo_path, "checkout", target)
    elif action == "open-vscode":
        code_bin = shutil.which("code")
        if not code_bin:
            return ActionOutcome(
                ok=False,
                output="VS Code 'code' command not found. Run 'Shell Command: Install code command in PATH' from VS Code first.",
                status="skipped",
            )
        subprocess.Popen(
            [code_bin, repo_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return ActionOutcome(ok=True, output=f"code {repo_path}", status="ok")
    else:
        return ActionOutcome(
            ok=False, output=f"unknown action: {action}", status="bad_request"
        )

    return ActionOutcome(ok=(rc == 0), output=(out + err).strip(), status="ok")


def _default_branch(path: str) -> str | None:
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
