# Architecture & Cross-cutting Concerns

Properties of `vibecoded-orchestrator` that come from how the three subsystems (VCT Launcher, Claude Orchestrator, `vct-secrets`) compose, rather than from any one of them. Also: repo hygiene, CI, security posture, release process.

---

## Three-Component Architecture

### Launcher + Orchestrator + Secrets primitive — composed system
Three subsystems, independent at the file level, designed to compose: the Launcher (Tauri 2 + SvelteKit) writes per-project env files and stores secrets in the OS keychain; the Orchestrator (Python MCP servers + hooks + KG/code graph) reads those env files at startup; the `vct-secrets` CLI wraps child processes with resolved secrets at exec time. Together they cover GUI onboarding through to CI headless use.

<details>
<summary>Details</summary>

The composition contract is one-directional: Launcher writes, Orchestrator reads. The Orchestrator never calls back into the Launcher binary. `vct-secrets` is orthogonal to both and works without the Launcher installed. The three components can therefore be upgraded or swapped independently; the contract surface is the three env files (`.claude/settings.json`, `.vscode/settings.json`, `.claude/env`) plus the `~/.vct-secrets/` directory layout.

</details>

### Hub API contract (`~/.vct/hub.port`)
The Launcher writes its IPC port to `~/.vct/hub.port`; the Orchestrator and CLI tools query `http://localhost:<port>/` for per-project module enablement status and to resolve per-project secrets without touching the keychain directly. See `docs/LAUNCHER_SUBTREE.md`.

### Launcher as git subtree (`launcher/`)
The `launcher/` directory is a `git subtree` of `pb992/VCT-Launcher`, branch `feature/orchestrator-hub`. A single `git clone` gives contributors the full Tauri source without `--recursive`. Direct edits to `launcher/` should originate in the VCT-Launcher repo and flow in via `git subtree pull --prefix=launcher vct-launcher feature/orchestrator-hub --squash`.

### Three-way per-project env write
When the Launcher creates or reconfigures a project, it writes the same env values to three files simultaneously: `.claude/settings.json` `env` block (canonical, all surfaces), `.vscode/settings.json` `claude-code.env` (VS Code extension), and `.claude/env` (POSIX shell-sourceable). All writes are read-merge-write to avoid clobbering existing content. See `docs/CLAUDE_CODE_COMPATIBILITY.md`.

### Atomic write pattern for settings files
All writes to `~/.claude.json` and `.vscode/settings.json` use atomic write semantics (write to a temp file, rename into place). Prevents partial-write corruption when multiple sessions run concurrently.

---

## Surface Compatibility

### Claude Code CLI surface
`tools/claude` is a drop-in wrapper script that auto-sources `$PWD/.claude/env` before exec'ing the real `claude` binary. Enable via symlink (`~/.local/bin/claude`) or shell alias. See `docs/CLAUDE_CODE_COMPATIBILITY.md` §Option A.

### VS Code extension surface
`.vscode/settings.json` `claude-code.env` block provides per-project env to the extension. Written in lockstep with `.claude/settings.json` so values are always identical.

### Claude Desktop app surface
`.claude/settings.json` `env` block is the only path that reaches Desktop app users (macOS / Windows). MCP servers still connect via `~/.claude.json`. Linux Desktop app is an upstream gap (Anthropic doesn't ship it yet); Linux users must use CLI.

### direnv integration
`.claude/env` can be sourced from a `.envrc` file via direnv for per-directory automatic env loading in any shell.

### WSL2 requirement on Windows for hooks
Bash hooks (`.claude/hooks/*.sh`) require WSL2 on Windows to fire automatically. Without WSL, MCP servers and CLI tools still work; only the hook automation layer is inoperable. PowerShell wrappers (`.ps1`) cover the main CLI tools.

### Full surface compatibility matrix
`docs/CLAUDE_CODE_COMPATIBILITY.md` contains the authoritative table of which features (hooks, agents, skills, MCP, slash commands, per-project env, Stop-event hooks) work on each surface.

### Stop-event hook gap (VS Code)
`Stop`, `StopFailure`, `SessionEnd` hooks do not fire in the VS Code extension as of v2.1.x. `notify-stop.sh` and `cost-tracker.sh` are affected. Users who need Stop-event automation should use the CLI or Desktop surface.

---

## Configuration Philosophy

### Minimal global, maximum per-project
No MCP server URLs, collection names, or project-specific paths go in the global `~/.claude/settings.json`. The only recommended global env var is `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Everything else is scoped per-project. This prevents cross-project KG contamination. See `docs/CONFIGURATION.md`.

### Per-project KG isolation via env vars
Six env vars control KG routing per project: `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `CONVERSATION_COLLECTION`, `PROJECT_NAME`, `KG_BASE_DIR`. The active VS Code workspace determines which collection is active.

### `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` global flag
The one recommended global env var. When set to `"1"`, Claude Code spawns up to 3 parallel agents for multi-file tasks, giving 3–5x speedup on large operations.

---

## Security Model

The trust boundary lives on the server side, not in the AGPL client. License flags on the launcher and orchestrator are advisory — convenient for UI gating, useless for actually withholding paid bytes. The artifact gateway re-validates a server-issued JWT at download time, so a patched client that flips its tier flag still can't pull paid binaries. Path A and Path B both classify admin tier server-side for the same reason.

### Client-is-advisory, server-is-authoritative trust boundary
License tier flags on the client (Launcher / Orchestrator) are advisory. Paid module artifact downloads are gated by a server-issued JWT validated at download time via the signed-URL CDN gateway. A client-side patch that spoofs the tier flag cannot bypass the gateway because the gateway re-validates the JWT independently. The trust root lives in Supabase + Lemon Squeezy + Ed25519 signatures on artifacts, not in open-source client code.

### Admin tier — two server-authoritative paths, no local bypass
Admin access has two paths, both classified server-side by `validate-tier`. **Path A — Vault-token admin (recommended)**: high-entropy `vct_admin_<URL-safe-base64>` tokens stored in the `vct_admin_tokens` Supabase Vault secret, with TOFU machine binding + optional expiration + an append-only `admin_auth_log` audit table. **Path B — Lemon Squeezy admin variant (legacy/Bug 33)**: real LS license whose `variant_id` lives in the `LS_ADMIN_VARIANT_IDS` Supabase env var (never in public source). Both paths supersede an earlier draft mentioning a local `MAINTAINER_TOKEN` / Ed25519 bypass — that approach was dropped because a one-line client patch defeated it. Both Path A and Path B classify server-side; client patches accomplish nothing. See [06-license-and-commercial.md](06-license-and-commercial.md#admin-license) and `docs/ADMIN_LICENSE.md`.

### `verify_jwt = false` for public auth-by-body edge functions
`launcher/supabase/config.toml` sets `verify_jwt = false` on `validate-tier` (auth via request-body license key / Vault admin token) and `lemon-squeezy-webhook` (auth via HMAC-SHA256 signature header). Deliberate architectural choice — the auth boundary is at the body level, so a JWT requirement on top would only force the launcher to obtain a Supabase anon key with no security gain (and `sb_publishable_*` keys don't satisfy `verify_jwt` anyway). Both endpoints are public by design.

### License validator fail-open on free tier
`VCThelpers/license/validator.py` fail-opens for the free tier: if the network is flaky, the validator does not block startup. Pro/MAO tiers have a 3-day offline grace window. After the grace window expires, the tier degrades gracefully without crashing the orchestrator.

### Credential scrubbing hooks
`PostToolUse` hooks scan every written file for high-entropy strings and known token formats. Env scrubbing hooks sanitise `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, AWS credentials, `TELEGRAM_BOT_TOKEN`, etc. before spawning subprocesses. Listed in `SECURITY.md` §Hardening notes.

### Bash injection guard (PreToolUse)
A `PreToolUse` hook inspects bash commands for shell-injection patterns and blocks execution before any shell command runs.

### Per-project secret isolation (`vct-secrets`)
`tools/vct-secrets/` enforces project-scoped secret resolution: one project cannot read another's secrets unless explicitly placed in the `shared/` namespace. The `vct exec` command injects resolved secrets into the child env via `exec`, never exposing them on the parent shell command line, in history, or in the parent environment.

### Secret rename-on-inject
`vct exec --project myproj --secret github_pat=GH_TOKEN` reads the stored file and exposes it to the child as `$GH_TOKEN`. Different tools that expect different env var names for the same credential are handled without duplicating the stored secret.

### OS keychain-backed secret storage
Secrets entered through the Launcher GUI are stored in the OS keychain, not in plaintext files.

### Pre-commit secret blocklist (`scripts/check-no-secrets.sh`)
Maintained blocklist of every token that has ever leaked from this repo's history. Wire as a git pre-commit hook. Refusals include the exact file and a pointer to the secrets rotation runbook (see maintainer docs).

### Hard path whitelist (install safety)
The installer and Launcher enforce a hard whitelist of orchestrator-managed paths. No write operation touches user code outside those paths. A `preflight_install_safety_check` Tauri command runs before any installation step.

### Read-merge-write for all settings files
All writes to `.claude/settings.json` and `.vscode/settings.json` perform a read-merge-write: only the managed key(s) are overwritten; any other content the user has added is preserved.

### No destructive container ops
The install path contains zero destructive container operations. Audit-tested and confirmed in `CHANGELOG.md [0.1.0] Security`.

### Local-first by default
Weaviate and Ollama run on-device. No data leaves the machine unless the user sets `VIBECODED_TELEMETRY=true` or the license validator reaches `https://api.vibecodedtools.it/validate-tier`.

---

## Compliance Posture

### AGPL-3.0 with no source-level dual license
The entire repository is AGPL-3.0-or-later. There is no FSL tier, no BSL drift, no separate source license for paid features. Paid modules are distributed as pre-compiled Ed25519-signed binaries via signed-URL CDN.

### CLA via `git commit -s` DCO sign-off
Contributors accept the CLA (`CLA.md`) by including a `Signed-off-by` trailer via `git commit -s`. Standard DCO pattern.

### Sub-processor minimization
Only five cloud sub-processors: Supabase, Vercel, IONOS, Lemon Squeezy, Cloudflare (planned). No Mailchimp, Resend, Loops, or other email/analytics processors.

---

## Repository Hygiene

### Dependabot across all four ecosystems
`.github/dependabot.yml` configures weekly Dependabot PRs for: root Python (`pip`), MCP server Python (`claude_mcp_servers/`, `pip`), GitHub Actions, and Launcher npm (`launcher/`). PRs capped at 5 per ecosystem to avoid noise.

### `.gitattributes` — LF enforcement + binary markers
Enforces LF line endings on all text files (critical for `.sh` scripts, which bash cannot parse if CRLF). Shell scripts get `eol=lf`. Binary model files (`*.pt`, `*.onnx`, `*.bin`) are marked `binary`.

### Issue templates (bug + feature)
`bug_report.md` structures reports with environment fields (OS, Python version, Claude Code version, orchestrator commit, install mode). `feature_request.md` provides a feature request template. Blank issues are disabled (`config.yml`).

### PR template with CLA checklist
`.github/PULL_REQUEST_TEMPLATE.md` includes: CLA sign-off (`git commit -s`), no secrets in commit, documentation updated, `pytest tests/` passing, no `LICENSE`/`CLA.md` changes without prior discussion.

### Discussions redirect for questions
`config.yml` disables blank issues and adds a `contact_links` entry redirecting general questions to GitHub Discussions. Security issues link to GitHub Security Advisories.

---

## CI / Quality Gates

Three independent CI jobs (Rust / Python / Frontend), no docs-only skip. The matrix is small but the three together cover the trust-critical surfaces: license validator, telemetry PII scrubbing, and the launcher's TypeScript types. Frontend runtime tests are an acknowledged gap at v0.1.0 — Playwright PRs are explicitly invited.

### Three-job CI matrix (Rust + Python + Frontend)
`.github/workflows/ci.yml` runs three independent jobs on every push to `main` and every PR. No job is skipped for docs-only PRs.

### Rust job: cargo test --lib
Runs `cargo test --lib --manifest-path launcher/src-tauri/Cargo.toml` on `ubuntu-latest`. Installs Tauri 2 Linux build deps. Full Tauri bundle builds are gated behind the release workflow.

### Python job: pytest (73+ trust-critical tests)
Runs `pytest tests/` against Python 3.12. Covers the trust-critical helpers: license validator, telemetry PII scrubbing, consent gating, install-flow detection.

### Frontend job: svelte-check (TypeScript type-check)
Runs `npm run check` in the `launcher/` working directory (Node 20). No frontend runtime unit tests at v0.1.0 — acknowledged gap in `CONTRIBUTING.md`. PRs adding Playwright smoke tests are explicitly invited.

### Rust cache (`Swatinem/rust-cache`)
CI uses `Swatinem/rust-cache@v2` scoped to `launcher/src-tauri` to avoid re-compiling Tauri deps on every run.

---

## Release Model

### Semver with manual tagging
Version numbers live in three places: `launcher/package.json` `version`, `launcher/src-tauri/Cargo.toml` `[package].version`, and `CHANGELOG.md` section headers. Manual tagging is a deliberate policy — automated tag-on-merge would push releases out faster than the maintainers can verify them on a clean machine, and the cost of a bad release is much higher than the cost of remembering to tag. See `docs/RELEASING.md`.

### CHANGELOG follows Keep a Changelog format
Uses the [Keep a Changelog](https://keepachangelog.com/) format with `[Unreleased]` at the top. Release commit moves `[Unreleased]` entries to a `[x.y.z] — YYYY-MM-DD` section.

### Hotfix branch process
Branch from the most recent tag (`git checkout -b hotfix/x.y.z+1 vx.y.z`), apply the fix, bump to `x.y.z+1`, update `CHANGELOG.md`, open a PR against `main`, and tag from the merge commit.

### Pre-release identifiers
`0.2.0-alpha.1`, `0.2.0-beta.1`, `0.2.0-rc.1` pattern. Pre-release GitHub releases are marked as such.

### Release pre-flight checklist (`docs/RELEASING.md`)
Seven items: CI green, local `cargo test --lib`, local `pytest tests/ -q`, local `npm run check`, smoke-test install on clean machine/VM, Ollama image pin is current in both compose files.

---

## Installer / Reproducibility

### Idempotent `install.py` with `--update` flag
`python install.py --update` re-runs on an existing install, preserving `.env` and user settings. Safe to re-run after upstream changes.

### `--quiet` flag for non-interactive / CI installs
`python install.py --quiet --no-joern --no-containers` runs without interactive prompts.

### Hardware-aware install profiles
Four embedding profiles auto-selected by install flags or hardware detection: NVIDIA GPU (CodeSage-Large-v2 GPU + qwen3 via Ollama), CPU-only (qwen3 for both), OpenAI API key (text-embedding-3-small), `--low-resource` (lightest models). Documented in `README.md` §Hardware table.

### Uninstall preserves user code
The uninstall path removes only orchestrator-managed files. User project code is never touched. Hard path whitelist enforces this.

---

## Key Documentation Index

| File | Contents |
|---|---|
| `BOOTSTRAP.md` | First-run install playbook — Path A (Launcher) vs Path B (clone) |
| `CLAUDE.md` | Claude Code project instructions — KG-first policy, hooks reference, workflow discipline |
| `docs/ARCHITECTURE.md` | High-level system architecture |
| `docs/CONFIGURATION.md` | Configuration philosophy, per-project vs global env vars |
| `docs/CLAUDE_CODE_COMPATIBILITY.md` | Surface matrix (hooks/agents/MCP by surface) |
| `docs/RELEASING.md` | Release process, semver, pre-flight checklist |
| `docs/TROUBLESHOOTING.md` | Common failure modes with causes and fixes |
| `docs/DEPENDENCY_LICENSES.md` | Transitive dependency license audit |
| `docs/VCT_SECRETS_PRIMITIVE.md` | vct-secrets design |
| Secrets rotation runbook | Key rotation runbooks (maintainer docs) |
| `docs/TELEMETRY.md` | Opt-in telemetry model |
| `docs/ADMIN_LICENSE.md` | Admin tier architecture — both Path A (Vault-token) and Path B (LS variant) with operational runbooks |
| `docs/LAUNCHER_SUBTREE.md` | Git subtree workflow for launcher/ |
| `SECURITY.md` | Security posture, hardening notes, disclosure process |
| `CLA.md` | Contributor License Agreement |
| `CODE_OF_CONDUCT.md` | Contributor Covenant v2.1 |
