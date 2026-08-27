# Agent Instructions for Harbor

Guidelines for AI coding assistants working on the Harbor codebase.
Follow these to produce changes that match the project's style, structure, and quality bar.

## Project Overview

Harbor is a local web dashboard for managing multiple git repositories at once.

- **Language**: Python (stdlib only at runtime) + vanilla JS/CSS/HTML frontend
- **Package layout**: `src/harbor/` (setuptools `package-dir: src`)
- **Python target**: 3.9+ (runs on 3.9 through 3.14)
- **Build**: setuptools + setuptools-scm (version from git tags)
- **Zero runtime dependencies** — only stdlib plus `tomli` on <3.11 and `tomli-w`/`platformdirs`
- **Frontend**: single HTML file + one utility JS file. No frameworks, no build step.
- **License**: MIT

## Architecture

```
src/harbor/
├── __main__.py      # CLI entrypoint (argparse)
├── config.py        # TOML config file loading/saving
├── scanner.py       # Directory scanning, repo discovery
├── git.py           # Git command wrappers (subprocess)
├── state.py         # AppState + Repo dataclasses
├── server.py        # HTTP server (http.server), API, SSE
└── static/          # Frontend: index.html + harbor-utils.js + assets
```

Key invariants:
- Git operations go through `git.py` wrappers only — never call subprocess directly elsewhere.
- Server is `ThreadingHTTPServer`; state is shared via an `AppState` object protected by a lock.
- All mutating API endpoints are checked for Origin/Referer (`_dispatch` in server.py).
- Batch pull uses Server-Sent Events (SSE) for progress.
- Frontend has no build step. Edit `index.html`, refresh the browser.

## How to Run Things

Always prefer `make` targets; they handle `PYTHONPATH=src` correctly.

```bash
make dev          # Install in dev mode (pytest, ruff, etc.)
make run          # Run Harbor (scans current dir)
make test         # Run Python tests
make test-js      # Run JS tests + syntax check
make coverage     # Run tests with coverage (fails < 70%)
make lint         # ruff check + mypy (mypy is informational only)
make build-check  # Build + twine check
```

Run a single test:

```bash
PYTHONPATH=src python3 -m pytest tests/test_server.py::test_origin_check -v
```

## Coding Conventions

### Python

- **Target 3.9 compatibility** — no `match`/`case`, no `str.removeprefix` (actually fine in 3.9+, but avoid 3.10+ features). Check `pyproject.toml` `requires-python`.
- Line length: 88 chars (soft limit; `ruff` doesn't enforce E501 but stay close).
- Indentation: 4 spaces.
- Quotes: single quotes for strings, double quotes for docstrings.
- Type hints: encouraged but not required. `mypy` is permissive (informational only).
- Docstrings: all public functions/classes should have them. Use Google style or reST — be consistent with the function you're near.
- Use `pathlib.Path` for paths, not `os.path`.
- Subprocess calls: always via `git.py` wrappers. Set `GIT_TERMINAL_PROMPT=0`.
- Error handling: raise specific exceptions; don't swallow with bare `except`.

### Frontend (HTML/CSS/JS)

- **Vanilla JS only** — no frameworks, no npm, no bundlers.
- Use `const` / `let`, never `var`.
- Dark mode: follow the existing `@media (prefers-color-scheme: dark)` pattern.
- i18n: user-visible strings go in the `STR` table at the top of `harbor-utils.js`. No hardcoded English/CJK text in the logic.
- Accessibility: semantic HTML, ARIA labels, keyboard navigation (Tab / Enter / Escape).
- CSP: no inline `onclick` / `onload` etc. — attach listeners in JS. No `eval()`, no inline styles via JS where avoidable.
- The JS test file is `tests/frontend/test-utils.js` — run with `node` directly.

### Testing

- Tests live under `tests/`, mirroring source modules where it makes sense.
- Use `pytest` fixtures for temp git repos (see existing patterns in `test_harbor.py`).
- Test files create real git repos in temp dirs — they clean up automatically.
- Property-based tests (hypothesis) live in `test_scanner_hypothesis.py` — use for parsers and pure functions.
- Coverage threshold: **70%** (from `pyproject.toml` `[tool.coverage.report] fail_under`). Don't let it drop below.
- When adding a feature, add tests. When fixing a bug, add a regression test.

## Git Workflow

- **Branch naming**: `<type>/<short-kebab-description>`, e.g. `feat/token-auth`, `fix/empty-repo-crash`.
- **Commit messages**: Conventional Commits format (`type: description`). See `CONTRIBUTING.md`.
- **PR template**: `.github/pull_request_template.md` — fill in the checklist.
- Squash-merge is fine; keep the squashed commit message Conventional.

## Security Rules

Read these before touching server.py or git.py:

1. **Never run user input as a shell command.** All `subprocess` calls use list form (no `shell=True`).
2. **All mutating API endpoints must pass the Origin/Referer check.** See `_dispatch` in `server.py`.
3. **CSP headers** must be set on every HTML response. Don't add unsafe-inline exceptions.
4. **Path traversal**: repo paths in URLs must be validated against the known repo list. Never `os.path.join(root, user_path)` directly.
5. **Secrets / tokens**: never hardcode. Use `secrets` module for generated tokens.
6. **GitHub Actions**: workflows default to `permissions: contents: read`. Elevate per-job only when needed.
7. **Dependencies**: runtime dependencies are extremely limited (tomli, tomli-w, platformdirs only). Don't add new ones without a very strong reason.

## What to Avoid

- Adding new runtime dependencies. Harbor's "zero deps" (near-zero) identity is a feature.
- Adding a frontend framework or build step. The single-file frontend is intentional.
- Breaking Python 3.9 compatibility.
- Making network calls from Harbor (it's a local tool).
- Adding a database. State lives in memory + the TOML config file.
- Platform-specific code without guards for Linux/macOS/Windows.

## Before Submitting a PR

Run these locally — they also run in CI:

```bash
make lint      # ruff + mypy
make test      # Python tests
make test-js   # JS syntax + tests
make coverage  # must stay >= 70%
make build-check  # package builds cleanly
```

If any fail, fix before pushing. CI runs on Python 3.9 / 3.13 / 3.14 across Linux / macOS / Windows.

## Useful Files to Reference

- `pyproject.toml` — ruff rules, mypy settings, pytest config, coverage threshold
- `Makefile` — all dev commands
- `CONTRIBUTING.md` — full contributing guide
- `SECURITY.md` — security policy and reporting
- `README.md` — user-facing docs
- `src/harbor/static/index.html` — entire frontend
- `src/harbor/server.py` — API + SSE + CSP + Origin check
