# Agent Instructions for Harbor

Guidelines for AI coding assistants working on the Harbor codebase.
Follow these to produce changes that match the project's style, structure, and quality bar.

## Project Overview

Harbor is a local web dashboard for managing multiple git repositories at once.

- **Language**: Python (stdlib only at runtime) + vanilla JS/CSS/HTML frontend
- **Package layout**: `src/harbor/` (setuptools `package-dir: src`)
- **Python target**: 3.10+ (`requires-python = ">=3.10,<3.14"`)
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
make test-e2e     # Run Playwright browser tests (installs Chromium first)
make coverage     # Run tests with coverage (fails < 70%)
make lint         # ruff check + mypy (mypy is informational only)
make format       # ruff check --fix + ruff format (same as the pre-commit hooks)
make build-check  # Build + twine check
```

`make help` lists every target.

Run a single test:

```bash
PYTHONPATH=src python3 -m pytest tests/test_server.py::test_origin_check -v
```

## Coding Conventions

### Python

- **Target 3.10 compatibility** — `requires-python = ">=3.10,<3.14"` and ruff's
  `target-version = "py310"`. 3.10 features (`match`/`case`, `X | None`) are fine
  and already used; 3.11+ ones are not.
- Line length: 88 chars (soft limit; `ruff` doesn't enforce E501 but stay close).
- Indentation: 4 spaces.
- Quotes: whatever `make format` produces — ruff-format normalizes to double quotes.
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
- **Keep bodies short** — a few bullets wrapped at ~72 chars, not paragraphs. The
  diff shows what changed; the body carries the reasoning it can't. Same for PR
  descriptions.
- **One concern per commit.** Reformatting never rides along with a behaviour
  change; commit the mechanical part first so the logic diff is noise-free.
- **PR template**: `.github/pull_request_template.md` — fill in the checklist. Its
  `## Type` list is a chooser and its HTML comments are prompts; replace both with
  your answer rather than pasting them into the description.
- **Stage explicitly**, never `git add -A` or `src/harbor/*.py` — the latter picks
  up the generated, gitignored `src/harbor/_version.py`.
- Run `make format` before committing, so a later run doesn't reformat your work.
- For a formatting-only change, prove it's behaviour-preserving by comparing each
  file's `ast.dump(ast.parse(...))` before and after rather than eyeballing it.
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
- Breaking Python 3.10 compatibility (`requires-python = ">=3.10,<3.14"` in `pyproject.toml`).
- Making network calls from Harbor (it's a local tool).
- Adding a database. State lives in memory + the TOML config file.
- Platform-specific code without guards for Linux/macOS/Windows.

## Before Submitting a PR

Run these locally — they also run in CI:

```bash
make format    # ruff --fix + ruff format (do this first, or CI lint may fail)
make lint      # ruff + mypy
make test      # Python tests
make test-js   # JS syntax + tests
make coverage  # must stay >= 70%
make build-check  # package builds cleanly
```

If any fail, fix before pushing. CI runs Python 3.10 / 3.12 / 3.13 on Linux, plus
3.12 on macOS and Windows (see the matrix in `.github/workflows/ci.yml`).

`make test-e2e` is **not** run by CI, so run it locally when you touch
`src/harbor/static/` or the server routes — nothing else will catch a break.

## Useful Files to Reference

- `pyproject.toml` — ruff rules, mypy settings, pytest config, coverage threshold
- `Makefile` — all dev commands
- `CONTRIBUTING.md` — full contributing guide
- `SECURITY.md` — security policy and reporting
- `README.md` — user-facing docs
- `src/harbor/static/index.html` — entire frontend
- `src/harbor/server.py` — API + SSE + CSP + Origin check
