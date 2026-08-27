# Security Policy

## Supported Versions

Harbor is in early development (`0.x`). Security updates are applied to the
latest release on PyPI and the `main` branch. Older versions are not
backported.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest| :x:                |

## Reporting a Vulnerability

**Please do not open public issues for security vulnerabilities.**

To report a security issue, use one of these channels:

1. **GitHub Security Advisories** — preferred. Go to
   [Security → Advisories → New draft security advisory](https://github.com/ginkgohat/harbor/security/advisories/new)
   (recommended for anything that could affect users).

2. **Email** — send details to `ginkgohat [at] users.noreply.github.com`.
   Include as much detail as possible: steps to reproduce, affected versions,
   and any proof-of-concept code.

### What to expect

- **Acknowledgment** within 3 business days
- **Status update** within 7 business days with an initial assessment
- **Fix timeline** depends on severity; critical issues are prioritized
- **Credit** in the advisory if the report is valid (let us know if you'd
  prefer to stay anonymous)

### Scope

This security policy covers the Harbor package itself (`harbor` on PyPI) and
this repository. It does **not** cover:

- Third-party tools Harbor invokes (e.g., `git`, `code`)
- User-owned Git repositories Harbor manages
- Misconfiguration by the user (e.g., port-forwarding Harbor to the internet)

## Security Model

Harbor is designed as a **local-only** tool:

- Binds to `127.0.0.1` by default — not reachable from the network
- No built-in authentication — anyone with local shell access can use it
- Cross-origin POST requests are rejected via Origin/Referer checks
- Destructive operations (discard, checkout, stash drop) require UI
  confirmation

Harbor is **not** intended to be exposed to untrusted networks. If you
reverse-proxy or port-forward it, you assume all risk. See the
[Security model section in the README](https://github.com/ginkgohat/harbor#security-model)
for details.
