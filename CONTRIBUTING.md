# Contributing to Harbor

Thanks for your interest in contributing to Harbor! Every contribution — big or small — is welcome. This document explains how to set up your development environment, submit changes, and participate in the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [What Can I Contribute?](#what-can-i-contribute)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Fork & Clone](#fork--clone)
  - [Development Setup](#development-setup)
  - [Running Tests](#running-tests)
  - [Running Harbor Locally](#running-harbor-locally)
- [Project Structure](#project-structure)
- [Style Guide](#style-guide)
- [Branch Naming](#branch-naming)
- [Commit Messages](#commit-messages)
- [Submitting Changes](#submitting-changes)
- [Pull Request Checklist](#pull-request-checklist)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)
- [Release Process](#release-process)

## Code of Conduct

By participating in this project, you agree to abide by the project's standards of respectful and inclusive behavior. Be kind. Be constructive. Assume good faith.

## What Can I Contribute?

Harbor is still in early days, so there are many ways to help:

- **Bug reports** — found something broken? [Open an issue](#reporting-bugs).
- **Feature requests** — have an idea? [Start a discussion](#suggesting-features).
- **Documentation** — typos, better examples, new translations (i18n).
- **Code** — bug fixes, new features, tests, refactoring.
- **Frontend** — the UI is a single HTML file; improvements to UX, accessibility, and styling are very welcome.

Look for issues tagged **good first issue** if you're new to the project.

> **Just looking to try Harbor?** See the [Quick Start](README.md#quick-start) in the README — you'll be up and running in 30 seconds.
>
> This guide is for people who want to **contribute code, tests, or documentation**.

## Getting Started

### Prerequisites

- **Python 3.10 or newer** — Harbor uses only the Python standard library at runtime (zero pip dependencies). The only exception is `tomli` on Python < 3.11, which backports `tomllib`.
- **Git** (obviously — it's a Git tool)
- **pytest** and **pytest-timeout** — for running tests; installed via the `[dev]` extras (see below).

No Node.js, no build tools, no database. That's the whole point.

### Fork & Clone

1. Fork the repository on GitHub
2. Clone your fork locally:

```bash
git clone https://github.com/your-username/harbor.git
cd harbor
```

3. Add the upstream repository to keep your fork up to date:

```bash
git remote add upstream https://github.com/ginkgohat/harbor.git
```

### Development Setup

Install Harbor in editable mode so your changes take effect immediately:

```bash
pip install -e ".[dev]"
```

Or with the Makefile shortcut:

```bash
make dev
```

If you prefer not to install globally, you can use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Running Tests

```bash
# Using the Makefile
make test

# Or directly with pytest
PYTHONPATH=src python3 -m pytest tests/ -v

# Run a specific test file
PYTHONPATH=src python3 -m pytest tests/test_harbor.py -v

# Run a specific test
PYTHONPATH=src python3 -m pytest tests/test_harbor.py::test_find_repos_empty -v
```

Tests create temporary git repositories in your OS temp directory. Everything cleans up automatically.

The browser tests are separate: they are marked `e2e`, excluded from every other
target, and **not run by CI** — so run them yourself when touching
`src/harbor/static/` or the server routes.

```bash
make test-e2e    # installs the Chromium build first, then runs tests/e2e/
```

### Running Harbor Locally

Run `make run` (scans the current directory) or see [README](README.md#from-source) for other options.

**Developer tip**: The frontend is a single static HTML file at `src/harbor/static/index.html`. Edit and refresh — no build step needed.

## Project Structure

```
harbor/
├── src/harbor/
│   ├── __init__.py      # version, package metadata
│   ├── __main__.py      # CLI entry point (argparse, server start)
│   ├── config.py        # config file loading/saving (TOML)
│   ├── scanner.py       # directory scanning, repo discovery
│   ├── git.py           # git command wrappers, repo operations
│   ├── server.py        # HTTP server (stdlib http.server), API endpoints, SSE
│   └── static/
│       └── index.html   # entire frontend (HTML + CSS + JS in one file)
├── tests/
│   └── test_harbor.py   # all unit tests
├── pyproject.toml       # package metadata, build config
├── Makefile             # common dev commands
├── README.md
└── CONTRIBUTING.md      # ← you are here
```

### Architecture Notes

- **Zero runtime dependencies**: Harbor uses only the Python standard library. The `dev` optional dependency group (pytest) is the only exception, and it's for development only.
- **Frontend is a single file**: The UI lives in one HTML file with vanilla JS. No frameworks, no bundlers. This keeps the project simple and deployable as a single pip package.
- **Concurrency**: Git operations and pull batch runs use `ThreadingHTTPServer` + threads. Python's GIL is fine here because the work is I/O-bound (waiting on git subprocesses).
- **SSE for progress**: Batch pull uses Server-Sent Events for real-time progress updates.

## Style Guide

### Python

- Follow [PEP 8](https://peps.python.org/pep-0008/) conventions
- Run `make format` before committing — it runs the same two ruff hooks as
  pre-commit (`ruff check --fix`, then `ruff format`). The formatter is
  authoritative on layout: indentation, line breaks, and quote style (it
  normalizes to double quotes everywhere). Don't hand-format against it.
- Keep lines under 88 characters where reasonable
- Type hints are encouraged but not required for simple functions
- All public functions and modules should have docstrings

### Frontend (HTML/CSS/JS)

- Keep it simple — no build tools
- Use `const`/'let' (not `var`)
- Follow the existing dark mode pattern with CSS `prefers-color-scheme`
- Accessibility matters: use semantic HTML, proper ARIA labels, and keyboard navigation

## Branch Naming

Branch names follow the same type system as [Conventional Commits](#commit-messages), with a short kebab-case description.

```
<type>/<short-kebab-description>
```

Optionally include an issue number for traceability:

```
<type>/<issue-number>-<short-kebab-description>
```

### Types

| Type | Purpose |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `docs` | Documentation only changes |
| `style` | Formatting, white-space, etc. (no code logic change) |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Code change that improves performance |
| `test` | Adding or correcting tests |
| `chore` | Maintenance tasks, tooling, build config |
| `ci` | CI configuration changes |
| `build` | Build system or external dependencies |
| `revert` | Reverts a previous change |

### Examples

```
feat/add-repo-sorting
fix/empty-repo-crash
docs/update-contributing-guide
chore/bump-pytest-version
feat/123-batch-stash-all
```

## Commit Messages

This project follows [Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/). Every commit message should use this format:

```
<type>[optional scope]: <short summary>

[optional body — what changed and why]

[optional footer(s)]
```

### Rules

- **Type** is required (see the table in [Branch Naming](#branch-naming)).
- **Summary** is required, in the imperative mood (e.g. "add", not "adds" or "added"), lowercase first letter, no period at the end.
- **Scope** is optional — a noun describing the area of the codebase (e.g. `scanner`, `server`, `frontend`).
- **Body** is optional but recommended for non-trivial changes. Explain *what* and *why*, not *how*.
- **Keep the body short.** Prefer a handful of bullets wrapped at ~72
  characters over paragraphs. The diff already shows what changed — the body is
  for the reasoning a reader cannot reconstruct from it. If a bullet only
  restates a line of the diff, drop it.
- **One concern per commit.** In particular, keep mechanical changes
  (reformatting, renames) out of commits that change behaviour. When a change
  needs both, commit the mechanical part *first* — the behavioural diff on top
  is then free of formatting noise and reviewable on its own.
- **Never commit generated files.** `src/harbor/_version.py` is written by
  setuptools-scm at build time and is gitignored; don't stage it with a glob.
- **Breaking changes** are indicated by a `!` after the type/scope *and* a `BREAKING CHANGE:` footer.

### Common types

| Type | Purpose |
|---|---|
| `feat` | A new feature (corresponds to MINOR in SemVer) |
| `fix` | A bug fix (corresponds to PATCH in SemVer) |
| `docs` | Documentation only changes |
| `style` | Changes that do not affect the meaning of the code (white-space, formatting, etc.) |
| `refactor` | A code change that neither fixes a bug nor adds a feature |
| `perf` | A code change that improves performance |
| `test` | Adding missing tests or correcting existing tests |
| `build` | Changes that affect the build system or external dependencies |
| `ci` | Changes to CI configuration files and scripts |
| `chore` | Other changes that don't modify src or test files |
| `revert` | Reverts a previous commit |

### Examples

```
fix: handle empty git repos without crashing scanner
```

```
feat(server): add repo sorting by last modified time
```

```
feat!: drop Python 3.8 support

BREAKING CHANGE: Python 3.9 is now the minimum supported version.
```

```
docs: update contributing guide with setup steps
```

> **Tip:** PR titles should also follow the Conventional Commits format, matching the type of the PR.

## Submitting Changes

1. **Create a branch** from `main` (see [Branch Naming](#branch-naming) for conventions):

   ```bash
   git checkout main
   git pull upstream main
   git checkout -b feat/my-new-feature
   ```

2. **Make your changes** and commit them.

3. **Run the tests** to make sure nothing is broken:

   ```bash
   make test
   ```

4. **Check coverage** — new code should maintain or improve test coverage:

   ```bash
   make coverage
   ```

   The project enforces a minimum coverage threshold (see `fail_under` in
   `pyproject.toml`). CI will fail if total coverage drops below it.

5. **Push to your fork**:

   ```bash
   git push origin my-feature-branch
   ```

6. **Open a Pull Request** on GitHub. Fill out the PR template (if one exists) and describe what you changed and why.

### Filling in the PR template

`.github/pull_request_template.md` contains both content to fill in and
instructions to you. The instructions are not part of the description:

- The **Type** section is a chooser. Replace the whole list with the one type you
  picked (`fix — bug fix`) — don't paste eleven options with one box ticked. The
  type belongs in the PR title too.
- `<!-- HTML comments -->` are prompts. Replace them with your answer, or write
  `None.` — a comment left in place renders as nothing, so the section reads as
  unanswered.
- Leave the **checkboxes** in the testing, checklist, and breaking-change
  sections: those are claims a reviewer scans, not instructions.
- Same brevity rule as commit bodies: explain the reasoning a reader can't get
  from the diff, and stop there.

### What happens next?

- A maintainer will review your PR, usually within a few days.
- You may get feedback or requests for changes — that's normal! Just push more commits to your branch.
- Once approved, your PR will be merged into `main`.

## Pull Request Checklist

Before submitting your PR, make sure:

- [ ] All existing tests pass (`make test`)
- [ ] New features include tests where appropriate
- [ ] Test coverage does not drop below the threshold (`make coverage`)
- [ ] Documentation is updated (README, docstrings, etc.)
- [ ] The code follows the project's style conventions
- [ ] Your branch is up to date with `main`
- [ ] The PR description explains **what** changed and **why**

## Reporting Bugs

When filing a bug report, please include:

1. **Harbor version** (`harbor --version`)
2. **Python version** (`python --version`)
3. **Operating system** (Windows/macOS/Linux, which version)
4. **Steps to reproduce** — what did you do?
5. **Expected behavior** — what should have happened?
6. **Actual behavior** — what actually happened? (error messages, screenshots)

The more detail you provide, the faster we can fix it.

## Suggesting Features

Feature requests are welcome! Before opening one:

- Check if there's already an issue or PR for it
- Consider whether it fits Harbor's scope: **local-first, zero-dependency, multi-repo web dashboard**
- Explain the use case — what problem does it solve?

## Release Process

(For maintainers)

1. Bump the version in `src/harbor/__init__.py`
2. Update the changelog if applicable
3. Commit and tag: `git tag v0.x.x`
4. Push: `git push --tags`
5. Build: `python -m build`
6. Upload to PyPI: `twine upload dist/*`

---

Thanks for contributing! 🚢