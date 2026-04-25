# Contributing to VibeCoded Tools — Orchestrator

Thanks for considering a contribution. This project is in alpha, small contributions and issue reports are very welcome.

---

## Before You Start

### 1. Read and Accept the CLA

All contributions to this project require acceptance of our [Contributor License Agreement (CLA.md)](CLA.md). This is standard practice for dual-licensed projects (same model as MongoDB, Grafana, Sentry).

**How to accept**: your first commit in a PR must include a `Signed-off-by` trailer. Use `git commit -s` to add this automatically:

```bash
git commit -s -m "Your commit message"
```

By doing so you confirm you have read the CLA and agree to its terms.

### 2. Licensing

This project uses a split licensing model:

| Directory | License | SPDX Identifier |
|---|---|---|
| `claude_mcp_servers/`, `.claude/`, `knowledge/`, `config/`, `docs/` | AGPL-3.0 | `AGPL-3.0-or-later` |
| `VCThelpers/license/`, RL retrieval modules (when published) | FSL-1.1-Apache-2.0 | `FSL-1.1-Apache-2.0` |

New source files must include the appropriate SPDX header at the top:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
```

For shell scripts, use `# SPDX-License-Identifier: ...`. For JS/TS, use `// SPDX-License-Identifier: ...`.

---

## Development Setup

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git
cd vibecoded-orchestrator
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
./install.sh  # or install.ps1 on Windows
```

Run tests:
```bash
pytest
```

Run linting:
```bash
ruff check --fix .
pyright
```

---

## Contribution Flow

1. **Open an issue first** for anything beyond a small fix — so we can agree on scope before you invest time.
2. **Fork the repo** and create a feature branch: `git checkout -b feature/your-feature-name`.
3. **Write tests** for new behavior. Coverage for changed lines is expected.
4. **Commit with sign-off**: `git commit -s -m "concise message"`. Keep commits focused.
5. **Run the test suite locally** before pushing.
6. **Open a PR** against `main`. Fill in the PR template. Link the related issue.
7. **Address review feedback** promptly. We aim to respond within 48 hours.

---

## Coding Standards

- **Python**: 3.11+ (3.12 recommended). Use type hints. Run `ruff check --fix` before committing. Prefer `pathlib` over `os.path`.
- **Shell**: `bash` only (no POSIX `sh` if avoidable). Use `set -euo pipefail` at the top of scripts.
- **Comments**: only explain WHY, not WHAT. Don't reference issue numbers in code comments — those belong in commit messages.
- **Commit messages**: imperative mood ("Add feature", not "Added feature"). First line ≤72 chars. Blank line then body if needed.

---

## What We Actively Want

- **Bug reports** with reproducible steps (OS, Python version, container runtime)
- **Installation friction reports** — especially on Windows or non-Ubuntu Linux
- **Knowledge Graph / Code Graph improvements** — edge cases in AST analyzer, new language support
- **Hook additions** — automated checks that improve signal-to-noise for users
- **Documentation** — examples, tutorials, architecture explanations

## What We're Not Accepting Right Now

- Large architectural rewrites without prior discussion
- Dependencies with licenses incompatible with AGPL-3.0 (GPL-2-only, etc.)
- Features that require cloud services we don't already use
- Removal of telemetry hooks (paid tier business model depends on aggregated metrics)

---

## Code of Conduct

Be respectful, technical, and concise. Criticism of code is welcome; criticism of people is not. Moderators reserve the right to close issues and PRs that don't meet this standard.

---

## Questions

- Technical: open a GitHub Discussion or Issue
- Licensing / CLA: contributions@vibecodedtools.it
- Security issues: security@vibecodedtools.it (please do not disclose publicly before we've had a chance to respond)
