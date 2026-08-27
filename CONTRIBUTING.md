# Contributing to VibeCoded Tools — Orchestrator

Thanks for considering a contribution. The project is in alpha; small fixes and issue reports are especially welcome.

---

## Before You Start

### 1. Read and Accept the CLA

All contributions require accepting our [Contributor License Agreement (CLA.md)](CLA.md). Standard practice for dual-licensed projects — same model MongoDB, Grafana, and Sentry use.

**How to accept**: your first commit in a PR must include a `Signed-off-by` trailer. `git commit -s` adds it automatically:

```bash
git commit -s -m "Your commit message"
```

By doing so you confirm you have read the CLA and agree to its terms.

### 2. Licensing

The entire repository is licensed under **AGPL-3.0-or-later**. New source files must include the SPDX header at the top:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
```

For shell scripts, use `# SPDX-License-Identifier: ...`. For JS/TS, use `// SPDX-License-Identifier: ...`.

Paid modules (Pro, etc.) are distributed as pre-compiled, signed binaries through a signed-URL CDN. They aren't covered by any source license; there is no separate source-license tier in this repo.

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
PYTHONPATH="$PWD" pytest           # Python suite — see the shadow trap below
bash scripts/test-keychain-safe.sh # Rust workspace battery
```

The `PYTHONPATH="$PWD"` prefix is not decoration. `install.py` installs `vco_lib` into the venv with `pip install -e .`, so a clean install imports it straight from the checkout. But a venv carried over from an older layout can hold a **non-editable COPY** of `vco_lib` in `site-packages` (a real directory, no `__editable__*.pth`), and that copy shadows the repo — so `pytest` silently tests yesterday's code and reports failures that vanish for no visible reason. Putting the repo root first on `PYTHONPATH` is what `scripts/pre-ship-check.sh` does for exactly this reason. If you see failures you cannot reproduce by reading the source, check for a shadowing copy first: `python -c "import vco_lib; print(vco_lib.__file__)"` must print a path inside your checkout, not inside `.venv/lib/`.

The Rust battery is **always** `bash scripts/test-keychain-safe.sh` (single-threaded, keychain-safe) — never a bare `cargo test --workspace`. The wrapper is what CI (`.github/workflows/ci.yml`) and the pre-ship Gate 2 (`scripts/pre-ship-check.sh`) run, so it is the only local invocation whose green means the same thing theirs does. It forwards extra arguments to cargo (`bash scripts/test-keychain-safe.sh secrets_cmd::`, `bash scripts/test-keychain-safe.sh --release`).

Run linting:
```bash
ruff check --fix .
pyright
```

---

## Continuous Integration

Pull requests run a deliberately small CI matrix on every push (`.github/workflows/ci.yml`):

- **Rust** — `bash scripts/test-keychain-safe.sh` (`cargo test --workspace --tests`, single-threaded), plus a separate step for the `vct-cli` sub-workspace. Full Tauri bundle builds are gated behind a release workflow because they need a per-OS matrix and a lot of platform deps.
- **Python** — `pytest tests/` on Python 3.12 with `requirements.txt` + `requirements-dev.txt`. Covers the trust-critical helpers: license validator, telemetry PII scrubbing + consent gating, install-flow detection.
- **Frontend** — `npm run check` in `launcher/` (svelte-check + TypeScript). No frontend runtime tests yet — known gap. PRs that add a Playwright smoke test or component tests are welcome.

CI stays small at this stage. When the launcher gets release tags, a separate workflow will add the full per-OS bundle build, signing, and artifact upload.

[`CHANGELOG.md`](CHANGELOG.md) follows the Keep a Changelog format.

---

## Contribution Flow

1. **Open an issue first** for anything beyond a small fix, so we can agree on scope before you invest time.
2. **Fork the repo** and create a feature branch: `git checkout -b feature/your-feature-name`.
3. **Write tests** for new behaviour. Coverage on changed lines is expected.
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

Be respectful, technical, and concise. Criticism of code is welcome; criticism of people is not. Maintainers may close issues and PRs that don't meet this standard.

---

## Questions

- Technical: open a GitHub Discussion or Issue
- Licensing / CLA: team@vibecodedtools.com
- Security issues: security@vibecodedtools.it (please do not disclose publicly before we've had a chance to respond)
