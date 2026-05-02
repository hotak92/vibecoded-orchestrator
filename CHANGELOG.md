# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.6] — 2026-05-02

### Changed
- **Project install/update overhaul.** `create_project_v2` now bundles all
  orchestrator infrastructure on first install (hooks, scripts, agents,
  skills, MCP servers, per-project Weaviate collection bootstrap). New
  `update_project_v2` keeps installed projects in sync non-destructively
  via a manifest-based drift detector — user-modified files are preserved,
  conflicts produce a `.md` reference note for Claude Code to resolve.
  Schema migrations port existing collection data instead of recomputing
  embeddings. Install/init logic extracted into `vco_lib/project_init.py`
  as a single source of truth shared by launcher and CLI.
- **Asymmetric SHARED_KG semantics.** All projects can READ the shared
  knowledge graph; per-project toggle now gates only WRITES. Lets a
  project consume cross-project patterns without polluting the shared
  collection.
- **KG-update-nudge counter (v10.1).** Replaced the v9 `cache_creation`
  proxy (cost-accurate but work-blind) with a `work_units_total` formula
  that sums output tokens + bounded intake from Read/Write/Edit/Web/
  Agent/Bash. Thresholds: 175k first nudge, 50k subsequent. Per-tool
  caps prevent any single call from triggering on its own. Two
  adversarial Opus reviews validated the design.

### Added
- **Full cross-OS hook parity.** Every `.sh` hook now ships with a
  matching `.ps1` sibling. macOS bash 3.2 quirks fixed (no `[[ ]]`
  regex, no `${var,,}`, no associative arrays). Zero `OS-EXEMPT-PARITY`
  shortcuts.
- **Bytecode pre-compile (Step 11b).** New default-on install step runs
  `python -m compileall` on orchestrator Python directories so first
  import is ~50-200ms faster per cold module. Opt-out via `--no-compile`
  (Linux/macOS) or `-NoCompile` (Windows). Best-effort: per-directory
  failures warn but never abort. Cross-OS via stdlib `compileall`.
- Vercel token-leak guard hook bundled into installed projects.
- AMD ROCm overlay + VRAM-aware Ollama model selection.
- Phase 1 `~/.vct-secrets/` shared/per-project secret layout with
  legacy-path fallback.

### Fixed
- Bare `KG_COLLECTION` bug across all 4 env surfaces; canonical
  per-project collection naming end-to-end.
- KG-nudge baseline reset on `Edit`/`Write` to any `**/knowledge/**.md`
  (not only project-local).
- Agent effort frontmatter recalibrated per-model (Opus 4.7 caps
  `xhigh`, Haiku → `high`).

### Security
- Sanitized leaked Lemon Squeezy webhook signing secret from
  `launcher/docs/SETUP.md` and `launcher/docs/HANDOFF-MARTINO.md`. The
  example value (`wh_vct_ls_2026_s3cur3k3y`) had been present since the
  launcher subtree was imported on 2026-03-07 and must be considered
  compromised. Replaced with `<YOUR_LS_WEBHOOK_SIGNING_SECRET>` plus
  `openssl rand -hex 32` instructions. **The webhook secret must be
  rotated in the LS dashboard.**
- Sanitized internal Supabase project ref `ltnlwhaxnpbiifordlbk` from
  `launcher/docs/SETUP.md`. Replaced with `<YOUR_SUPABASE_PROJECT_REF>`
  placeholder. The 0.1.0 changelog claim that "internal Supabase URLs
  are not committed to public source" is now actually true.
- Added `scripts/check-no-secrets.sh` pre-commit grep guard listing
  known-leaked tokens so they cannot sneak back in.

## [0.1.0] — 2026-04-26 — Initial private release

### Added
- VCT Launcher v1.0 + v1.1 (Tauri 2 desktop app): 17 GUI screens including project management, KG / code-graph dashboards, audit log, modules catalog, license activation, settings, onboarding wizard.
- Knowledge Graph with semantic search (Weaviate + qwen3-embedding 1024-dim, optional OpenAI vectors, GraphRAG-style WikiLink traversal).
- Code Graph (Tree-sitter AST analysis, 5 entity types: `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`; optional Joern integration for CFG / PDG).
- 20 automation hooks (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `PreCompact`, `PostCompact`, `StopFailure`, `SessionEnd`, …).
- 4 MCP servers: `weaviate-kg`, `ollama`, `search`, `code-embedding`.
- 19 specialist agents + 28 skills as templates dropped into `.claude/` at install time. Optional MAO add-on adds 10 more agents.
- Multi-surface compatibility: Claude Code CLI + VS Code extension + Claude Code Desktop app.
- Per-project env injection via `.claude/settings.json` (Anthropic-canonical) + `.vscode/settings.json` (VS Code) + `.claude/env` (CLI shell). User code is never touched.
- Container runtime support for podman and docker; shared services across projects on a single machine.
- License validator: free tier fail-open (so a flaky network never breaks startup), optional Pro / Admin tiers via Lemon Squeezy with a 3-day offline grace window.
- Local-only architecture: Weaviate + Ollama on-device; no data leaves the machine without explicit user consent.

### Security
- Hard whitelist for orchestrator-managed paths (zero-touch user code).
- Pre-flight install-safety check (`preflight_install_safety_check` Tauri command).
- Read-merge-write for all settings files (preserves user content).
- Atomic write patterns for `~/.claude.json` and `.vscode/settings.json`.
- No destructive container ops anywhere in install path (audit-tested).
- OS-keychain-backed secret storage (no plaintext on disk).
- Default-OFF telemetry (explicit opt-in required; default `.env` writes `VIBECODED_TELEMETRY=false`).
- Public alias for license validation (`https://api.vibecodedtools.it/validate-tier`); internal Supabase URLs are not committed to public source.

[Unreleased]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.5...v0.1.6
[0.1.0]: https://github.com/hotak92/vibecoded-orchestrator/releases/tag/v0.1.0
