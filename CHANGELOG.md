# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.55] - 2026-06-14

v0.2.55 is a focused fix for the launcher GUI self-update path. A
"Fetch + Install" update could leave the launcher running the OLD
binary while still offering the update — an infinite "update available"
loop with no error the user could act on.

### Fixed

- **Launcher restarts into an available newer binary "in any case"**
  (the primary bug). `finalize_update_and_restart` previously returned
  an error and never restarted when `WaitForBinaryRefresh` timed out —
  even when a newer dist binary than the running launcher was already on
  disk. It now restarts into that newer binary (strictly better than
  staying on the old one) and records the partial-refresh state durably;
  it only hard-fails when there is genuinely no newer binary to run.
- **Durable, Claude-readable update-failure logging.** Launcher-side
  update failures that previously surfaced only as a transient GUI modal
  now also write a `launcher_update_diverged` entry to
  `.claude/context/UPDATE_DEFERRED.md` (four kinds: non-fast-forward
  divergence, partial binary refresh, binary-refresh timeout, and a
  generic git-pull failure). The project `CLAUDE.md` SESSION START rule
  now names this condition so a session reads it at start. The entry
  self-clears on the next successful update (re-probed against the
  on-disk launcher + hub versions).
- **Local fallback builds are staged into `launcher/dist/`.** The
  `post-install-launcher.sh` build/download fallbacks now copy the
  acquired binary into the canonical `launcher/dist/<arch>/` slot the
  restart path re-execs (atomic temp+mv; skips macOS `.app` bundles).
  Inside a launcher-managed update the shell helper now defers binary
  acquisition to the Rust path entirely (no wasteful local build).
- **Hub binary freshness gating.** `WaitForBinaryRefresh` now also gates
  the post-update restart on the on-disk `vct-hub` metadata version, so
  an update cannot restart into a launcher that would start a stale hub.
  Absent hub metadata degrades gracefully (no deadlock).
- **Stale `schema_migration_required` deferrals self-clear.** A clean
  pre-update schema dry-run (no copy/rebuild needed) now clears a stale
  migration deferral left by an earlier update instead of carrying it
  forward forever; unrelated deferral entries are preserved.

### Added

- `tests/test_launcher_dist_subdir_parity.py` — cross-language parity
  test pinning the OS→dist-subdir mapping across the bash, Python, and
  Rust resolvers (specified in `docs/INSTALL_ARCHITECTURE_v2.md` §10.1,
  Track A; first implemented here).

## [0.2.54] - 2026-06-12

v0.2.54 is a multi-wave hardening release. Wave 0 made the Windows
install-smoke CI gates real; Wave 1 landed secrets infrastructure (A1),
container/GPU-passthrough truth (B), self-update hardening (C) and
data-safety guarantees (D); Wave 2 fixed the Windows hub-start hang (W),
shipped post-install health-audit docs (F), swept template/docs truth
(A2), made the CI/test gates honest (E) and revived the Excalidraw MCP
(I); Wave 3 closed cross-OS parity gaps (G) and licensing-tier truth
(H); Wave 4 (J) deduplicated cross-cutting helpers and reconciled this
changelog. Every track passed an adversarial verify-and-amend pass
before merge.

### Added

- **v0.2.54 Track A1 (S-1..S-8): secrets discoverability for 3rd-party Claude agents** — the secrets primitive is now self-describing instead of tribal knowledge:
  - **S-1** — `templates/ORCHESTRATOR-CLAUDE.md.template` ships a dedicated `## Secrets` section (two-store mental model: launcher keychain via vct-hub vs `~/.vct-secrets` file store; discover/probe/inject recipes; GitHub token-shape table; access-gating semantics; bootstrap-envelope pointer). The retired chmod-600 `~/.your-secrets` paragraph (stale since the v0.1.7 wrapper sweep) is replaced with a pointer to the launcher GUI flow (OnboardingWizard step 4 / Preferences → Special Secrets).
  - **S-2** — `vct get` / `vct exec` (and the new `can-read` / `resolve`) default to the **shared scope** when `--project` is omitted, symmetric with `vct list`. `vct set`/`revoke` stay explicit (writes are deliberate).
  - **S-3** — `install.py` materializes an agent-facing key-schema doc at `~/.vct-secrets/shared/_README.md` (new template `templates/vct-secrets-shared-readme.template`; only-if-missing so user edits survive `--update`). Documents which GitHub token shape serves which consumer (classic `ghp_*` 40 chars vs fine-grained `github_pat_*` ~93 chars vs App `ghs_*` — the "401 Bad credentials with a 335-char blob" class) + the trailing-newline gotcha. `vct list` skips `_README.md`.
  - **S-4** — `bash_security.py` credential-access blocks (`env | grep TOKEN`, `.env` reads, `/proc/*/environ`, ssh-key/history reads) now append a REMEDIATION signpost naming `vct list` / `vct exec` / the hub resolver clients / `docs/VCT_SECRETS_PRIMITIVE.md`, so a blocked agent learns the sanctioned path at the moment of failure.
  - **S-5** — new `vct can-read --key K [--project P]` (exit 0/1 probe, prints nothing) and `vct resolve --key K [--project P]` (prints the winning source path, never the value). `vct doctor` additionally shape-checks every `github_pat`/`github_pat.*` key (pattern + length + embedded-whitespace, warns without printing values) and its issue counter now actually increments.
  - **S-7** — new `vco_lib/agent_secrets.py`: `get(key, project=None)` + `exec_with_secrets(cmd, secrets={...})` for Python tooling. vct-hub first (same port/token discovery + 401-retry as `vco_lib.project_config`), file-store fallback when the hub is unreachable / the project is unregistered / the hub has no active binding (`key_not_active` conflates "paused" and "never declared" — live-verified — so the independent file store still answers). Secrets never touch argv or exception messages; `shell=True` refused.
  - **S-8** — `install.py --bootstrap` envelope gains a `secrets` block (CLI path, store dir, shared key NAMES only, `git-credential-vct` registration probe, hub `/env` endpoint + resolver clients) — schema updated in `docs/schemas/install-bootstrap-envelope-v1.json`.
  - New tests: `tests/test_agent_secrets.py` (offline file-store tier + live-hub tier that auto-skips when no hub runs), `tests/test_v0254_secrets_surface.py` (template/README truth guards, live vct CLI contract runs, envelope names-only guard), 4 new cases in `tools/vct-secrets/tests/test_vct.sh` (22 total).
- **v0.2.54 Track F: post-install health-audit + recovery docs** — new `docs/post-install/` set: `POST-INSTALL-HEALTH-AUDIT.md` (6-item GUI-driven checklist with CLI fallbacks and a fillable report template), `CLAUDE-DIAGNOSTIC-PROMPT.md` (paste-into-Claude interpreter prompt with safety rails: no volume `rm -rf`, no `~/.claude.json` hand-edits, ask before destructive ops), `CONTAINER-RECOVERY.md` (Podman/Docker daemon-vs-binary, podman machine, port-conflict diagnosis, ghcr auth, volume sanity before re-embedding) and `WINDOWS-FIRST-RUN-CHECK.md` (PS 5.1 BOM expectations, execution policy, hook parity, Scheduled Task inspection). Track C's `UPDATE-RECOVERY.md` cross-links the set; `templates/CLAUDE.md.template`'s SESSION START block gains the `UPDATE_DEFERRED.md` check + fresh-install audit nudge. The amendment pass fixed two critical claims before merge: the uninstall section now states that volume removal is the DEFAULT (`--keep-data` required to preserve; `--yes` auto-confirms), and the false "removes the Scheduled Task" claim is replaced by a known-gap callout (closed for real by Track G below).
- **v0.2.54 S-1: workflow tooling templates** — `templates/scripts/detect-workflow-needs` + `generate-workflow` (with `.ps1` siblings): detect recurring multi-step patterns in a project's history and scaffold `.claude/workflows/*.mjs` definitions. (The companion background-agent spawner was dropped by user verdict — Claude Code covers all three use cases natively; see the native-capability-check discipline.)
- **v0.2.54 Track G (G-2): `uninstall.bat`** — Windows users had no discoverable uninstall entry point (`uninstall.sh` is POSIX-only; `python install.py --uninstall` was undocumented on the Windows path). Thin wrapper with a zero-side-effect help handler, `--yes` sniffed before any pause-able path (Track W discipline), py-launcher-then-python detection; the unguarded-pause and cmd-parse static lints now cover BOTH `.bat` entry points.
- **v0.2.54 Track G (G-3): portable `cost-summary.py`** — cost-summary was bash-only (native Windows users could not read `~/.claude/metrics/costs.jsonl` even though `cost-tracker.ps1` collects there) and its `--days`/`--session` args were silently ignored (the heredoc read a `DAYS` env var the wrapper never exported). Logic now lives in a stdlib-only Python CLI with a real argparse contract; the bash wrapper survives as a thin shim. The broken `kg-infer` wrapper (exec'd a script that no longer ships) is retired in the same pass.

### Fixed

- **v0.2.54 Track E amendment (adversarial-verifier findings): pyright CI gate made real instead of red** — Track E removed the `|| true` swallow from the `python-typecheck` job, but repo-wide pyright has 813 pre-existing errors across 429 files, so the first merge would have gone red. New repo-root `pyrightconfig.json` scopes the gate to `vco_lib/` + `claude_mcp_servers/weaviate_mcp/` + `claude_mcp_servers/search_mcp/`, and all 14 in-scope errors are FIXED — the gate now enforces ZERO errors on that surface. Two of the 14 were latent runtime bugs: (1) `vco_lib/cli/verify.py`'s env-projection wrappers were written against the plan-era Phase 0.B API — the landed `apply_project_env()` has no `project_folder` kwarg and `project_env_from_db()` returns a `ProjectEnvBundle` TypedDict, not a flat map, so `vct verify-env-projection` would have diffed the bundle's 3 field names instead of canonical env keys and TypeError'd on `--fix`; the wrappers now adapt the landed API (unwrap `canonical_env`, rebuild the bundle, opt into all three surfaces so the round-trip idempotency check holds). (2) `vco_lib/project_init.py`'s `derived_project_name` was assigned inside a try whose except swallows — a templates-install failure would have NameError'd the later legacy-collection detection; also, the lazy `config_projection` import now lives in its own try so an ImportError can't NameError the `except DbUnreachable` clause that referenced the then-unbound class. **Backlog (v0.2.55): clear the two out-of-scope files and gate them too — `claude_mcp_servers/weaviate_mcp/server.py` (93 errors, mostly weaviate-client v4 stub friction) and `install.py` (34 errors).**
- **v0.2.54 Track E amendment: Gate-21 known-names regex missed every mid-line path leak** — the blocklist pattern was `^/(home|Users)/<name>(/|$)`, but `git grep`'s `^` anchors to LINE start, not path start, so real leak shapes like `KG_BASE_DIR=/home/<name>/...` or `path = "/Users/<name>/..."` passed the gate. Anchor dropped; the `/(home|Users)/` prefix + `(/|$)` boundary keep matching exact-name-only (`/home/<name>s/` and name-prefix collisions do not match). Also removed the dead `runner_allowlist_re` variable and its misleading "used below" comment (the generic pass that consumed it was removed earlier in Track E).
- **v0.2.54 Track A1: two divergent secret-shape classifiers in the launcher crate** (`launcher/src-tauri/src/commands/env_cmd.rs`): the webview-facing `read_env_var` blocklist used suffix-only matching and missed `GH_PAT`, `DB_PASS`, and `AUTH_HEADER` (ends `_HEADER`, not `_AUTH`) — those returned **cleartext to the webview** while `mcp_registration.rs`'s segment-based classifier flagged them as secrets. `is_secret_shaped_name` now delegates to the canonical `mcp_registration::is_secret_shaped_env_key` (segments TOKEN/SECRET/PAT/PASSWORD/PASS/AUTH + `KEY`/`_KEY`) plus webview-only extra segments (PASSWD/CREDENTIAL/CREDENTIALS). Regression tests pin the previously-leaking names and the classifier-agreement contract; `KEY_FILE_PATH`-style names still pass through.
- **v0.2.54 Track A1 (S-6): `tools/vct-secrets/` docs rewritten for truth** — README + MIGRATION claimed "the VCT Launcher will do all of the above" (install the file store, migrate secrets, register the git credential helper); the launcher has never done any of that (its canonical store is the OS keychain + vct-hub `/env` resolver). Both docs now lead with the two-store model and mark every file-store step as manual; a "For Claude Code agents" orientation section heads the README. `git-credential-vct`'s project-root fallback default is unified on `$HOME/dev` (the script shipped a machine-specific path while docs said `$HOME/dev`) and its broken regex fallback (unused escape variable) is replaced with a literal prefix match. The stale `search-mcp-wrapper.sh` skip-arm was removed from `vct doctor`.
- **v0.2.54 Track A1 (S-8 extension, Phase C gpu-runtime finding): bootstrap envelope AMD arm hardcoded `vram_gb: null`** — `_bootstrap_detect_gpu` now reuses `_probe_amd_rocm_vram_gb()` (CSV + text rocm-smi parse already shipped for the main install flow), mapping a failed probe back to `null`.
- **v0.2.54 Track A1 (install hygiene): `query_logger.py` wrote JSONL logs into the installed package directory** (`claude_mcp_servers/weaviate_mcp/`) — logs now land under `$VCT_STATE_DIR/logs/weaviate_mcp/` (override: `$VCT_QUERY_LOG_DIR`), with parent-dir creation on first write.
- **v0.2.54 Track A1 (doc truth, live-verified): vct-hub liveness route is `GET /api/v1/health`** — `docs/CONFIGURATION.md` documented a bare `GET /health` "no auth" route; live probe shows `/health` hits the auth layer (401) and only `/api/v1/health` answers without a token.
- **v0.2.54 Track A1 (drive-by, Wave-0 G-1.5 follow-up): `scripts/lib/download-launcher-asset.ps1` shipped without a UTF-8 BOM** while containing non-ASCII bytes — `tests/test_ps1_utf8_bom.py` was red on every full-suite run since G-1.5 landed (PS 5.1 would mis-decode the file as Windows-1252). BOM prepended.

- **v0.2.54 Track B: paid-module GPU pipeline delivered acceleration in name only — variant images ran with ZERO GPU passthrough (P0-4)** (`vct-launcher-core/src/services/container_runtime.rs`, `launcher/src-tauri/src/commands/module_service.rs`, `vct-hub/src/module_supervisor.rs`):
  - `resolve_variant_tag` carefully picked `-cuda`/`-rocm` image tags, but `build_podman_run_args` / `build_podman_run_args_global` — the ONLY arg builders on the module-start path — emitted no `--device` / `--gpus` / `--group-add` flag of any kind, so torch saw no GPU inside the GPU image: users paid the multi-GB CUDA/ROCm layer pull for CPU inference, and the CHANGELOG v0.2.20 claim "AMD users get ROCm-accelerated containers" was false for modules. The builders now take the detected engine + GpuMode and append runtime-appropriate flags via the new `gpu_passthrough_args` helper: podman+CUDA → `--device nvidia.com/gpu=all` (CDI), docker+CUDA → `--gpus all`, ROCm → `--device /dev/kfd --device /dev/dri` plus `--group-add keep-groups` (podman) / `--group-add video --group-add render` (docker). Gated on the manifest declaring `gpu_image_variants` so legacy single-tag modules keep their exact previous argv.
- **v0.2.54 Track B: module path ignored the user's container-runtime choice AND probed client-only (C-RT-1/C-RT-2)**: three near-identical `detect_container_runtime` copies (`module_service.rs`, `installer_engine.rs`, `module_supervisor.rs`) hardcoded podman-first `--version` probing — never honoring `VCT_CONTAINER_RUNTIME` / `state/install/runtime.txt` (despite install.py's "consolidated" v0.2.14 contract) and never contacting the daemon, so a dual-runtime host got infra under docker with modules under podman, and a stopped podman machine beat a live Docker Desktop. Replaced by ONE promoted daemon-aware detector in `vct-launcher-core::services::container_runtime` (env → runtime.txt → podman-first `<cmd> info` probe, 10s timeout; error names binary-present-but-daemon-dead candidates).
- **v0.2.54 Track B: authed pre-pull skipped whenever the hardware snapshot was missing (C-RT-6)**: all four module-start paths (launcher + hub, per-project + global) gated `pre_pull_with_auth_for_start` on `gpu_mode.is_some() && method == ContainerPull`; `gpu_mode` soft-fails to `None` on hub-first boots / DB resets / legacy single-tag modules, so cache-evicted private images re-pulled anonymously and 401'd — the v0.2.46 anonymous-pull-401 bug shape that v0.2.47 fixed for the variant path only. Now gated on `method == ContainerPull` alone (the helper already no-ops when no token resolves).
- **v0.2.54 Track B: podman+AMD silently got the stale legacy ROCm overlay (C-RT-4/C-6)**: install.py has preferred `infrastructure/podman-compose.rocm.yml` since v0.2.20 but the file never shipped, so the project's RECOMMENDED engine fell through to `podman-compose.amd-rocm.yml` — which lacks `cap_add: [SYS_PTRACE]` and `HSA_OVERRIDE_GFX_VERSION` (the workarounds the canonical docker overlay calls necessary; symptom: rocBLAS fails device detection, silent CPU inference). The file now ships with both fixes + the podman `keep-groups` handling.
- **v0.2.54 Track B: two override-file generators diverged per compose engine (C-RT-5)**: `volumes.rs` (Bug-31 path) wrote only `docker-compose.override.yml` while `storage_ux.rs` wrote only `compose.override.yaml` — each engine auto-loads a different name, so which volume aliases applied depended on which compose binary ran, and a runtime switch could re-point Weaviate/Ollama at fresh empty volumes (data "loss" by alias miss). Both writers now mirror the identical body to BOTH names atomically; `volumes.rs`'s remover removes both. Migration note for existing dual-file hosts: the next launcher-driven storage/volume change rewrites both names identically; until then any historical divergence persists on disk (deliberately NOT auto-reconciled at boot — the two bodies encode different intents and picking the wrong one silently re-points data volumes).
- **v0.2.54 Track B: `VCT_GPU_VENDOR` was advertised by two remediation messages but read by nothing (gpu C-3)**: the `compose_overlay_ambiguous` deferral and the `--apply-deferred` skip handler both instructed "set VCT_GPU_VENDOR" — repo-wide, no `os.environ.get` existed, so the documented dual-GPU fix was a no-op and AMD-preferring dual-vendor hosts could never select the ROCm overlay (nvidia-smi probes first). install.py now honors `VCT_GPU_VENDOR=nvidia|amd|metal|auto` ahead of the layered probe (lenient: failed preferred-vendor probe falls through to auto-detect with a stderr note).
- **v0.2.54 Track B: tier-override wrote 768-dim config for a 1024-dim model (gpu C-5)**: `_choose_embedding_config`'s override block swapped `code_model`/`text_model` but left `code_dims`/`text_dims`/`active_embedding`/the Ollama pull list at the profile's stock values — 6-12 GB GPU hosts got `CODE_EMBED_DIMS=768` for the 1024-dim qwen3 pick, and a text-model override would have pointed the named-vector slot at the wrong model. Extracted to `_apply_tier_overrides` which keeps every dependent field in lockstep (copy-on-write on the shared profile lists).
- **v0.2.54 Track B: AMD ≥12 GB hosts got CodeSage-on-CPU labeled "GPU-accelerated" (gpu C-2)**: the gpu profile gate was vendor-blind while `CODE_EMBED_DOCKERFILE=Dockerfile.cuda` is NVIDIA-only and no ROCm code-embed image exists. AMD hosts are now routed to the Ollama-served qwen3 code model — Ollama itself is ROCm-accelerated via the rocm overlay, so code embeddings still run on the GPU, through the backend that actually has a ROCm path (also makes the shipped CHANGELOG-F7 / amd-rocm-overlay claims true).
- **v0.2.54 Track B: `CODE_EMBED_DOCKERFILE`/`CODE_EMBED_BACKEND` never reached compose substitution (gpu C-4)**: install.py wrote them to `PROJECT_ROOT/.env` — one level above the compose project dir, which compose never reads — at step 9/10 (after the step-5 compose-up), and never exported them, so even NVIDIA hosts built the CPU `pytorch` code-embed image (the v0.2.50 audit-F1 fix never connected). install.py now writes a managed block in `infrastructure/.env` (covers boot wrapper / hooks / manual compose runs) AND merges the keys into the compose-up subprocess env. *Needs human verification on a CUDA host*: `podman exec vco_code_embed python -c "import torch; print(torch.cuda.is_available())"` after a fresh install should print `True`.
  - Smaller Track B fixes in the same pass: code-embed `auto` device probe now falls back to Apple `mps` before `cpu` (gpu C-7; docs corrected — the blocker was never upstream Metal wheels); `docs/CONFIGURATION.md`/`GETTING_STARTED.md` no longer claim the Ollama code-embed fallback is `qwen3-embedding:0.6b` (the service default is `jina-embeddings-v2-base-code`, 768-dim; qwen3 only on 6-12 GB GPU tiers); `claude_mcp_servers/compose.yaml` GPU uncomment-blocks now show BOTH the podman CDI form and the docker `deploy.resources` form (C-RT-8); Docker daemon auto-heal sibling of the podman auto-start (`open -a Docker` / Docker Desktop launch + 60s `docker info` poll, C-RT-7); `picker.rs` canonical-collection list fixed from lowercase `_development` (which never matched real `<Project>_Development` classes) to case-insensitive suffix matching + the missing `_Diagrams` entry; `VCT_LAUNCHER_DB_PATH` is now honored by ALL Python-side launcher.db resolvers (`vco_lib/paths.py` + `config_projection.py`, previously only `launcher_db_reader` — split-brain inside `_rebind_orchestrator_root_to_canonical_locked`).
- **v0.2.54 Track D (P0-2): data-loss trio — documented recovery commands no longer drop collections without recreate/re-ingest**:
  1. `vco_lib/project_init.py` — the `rebuild` action now recreates the collection with the target schema immediately after the drop, and the `migrate-collections` CLI (the exact command the `schema_migration_required` deferral tells users to run) re-ingests via the project's `sync_knowledge_graph.py --all` when `--project-folder` is given; a missing re-ingest path → `errors[]` + exit 1 + `reingest_required: true` instead of a silent empty collection. The deferral's `command_to_apply` now passes `--project-folder` and drops the false "falls back to drop+re-embed" promise.
  2. `scripts/migrate-shared-kg-schema.{sh,ps1}` — two pre-drop guards: kg-sync missing → abort (exit 4) BEFORE the drop (previously: drop, exit 0, collection left empty); cross-project shared-write probe → nodes written by other projects (unrestorable from this clone's `kg-sync --all`) refuse the drop (exit 3) unless `VCO_SHARED_KG_MIGRATE_CONSENT=1`. Conservative: probe failures and >10k-object collections also refuse.
  3. `templates/scripts/maintain_knowledge_graph.py` — `--fix`/`--rebuild` refuse (exit 2) on the SHARED KG collection (cross-project nodes all look orphaned from any single project root; `VCO_MAINTAIN_SHARED_KG_CONSENT=1` overrides); orphan deletion is now UUID-keyed (the title-equality re-query deleted duplicate-titled LIVE nodes as collateral); destructive steps prompt for confirmation (`--yes` for non-interactive runs).
- **v0.2.54 Track D (Theme 5): deferral-protocol gaps** — `install.py::_apply_deferred_entries` gains the three missing handlers, each with v0.2.46 re-probe discipline: `schema_drift_rebuild_required` re-probes live drift (no longer resurrects the entry right after a successful `--rebuild-collections` in the same run); `update_resume_required` re-probes the launcher's resume sentinel (the entry was previously immortal — installer.rs deferred to nonexistent `mark_resolved` calls and install.py's docstring claimed a handler that didn't exist); `launcher_restart_required` consumes the documented `launcher-restart-marker` or verifies the recorded old launcher PID has exited (Windows probe via `OpenProcess`, never `os.kill(pid, 0)` which kills there). The launcher additionally self-clears a stale restart entry at boot (entry mtime predates `LAUNCHER_BOOT_TIME` → the running binary is already post-swap) instead of re-rendering the restart banner forever after a manual quit+relaunch.
- **v0.2.54 Track D (Theme 5): orchestrator-self materialize no longer destroys runtime-copy edits** — `_materialize_orchestrator_self_claude_dir` backports the per-project bundle hash-compare: user-modified `.claude/{hooks,scripts}` files are preserved + listed in an `orchestrator_self_user_modified_preserved` deferral (new `--force-materialize-claude-dir` flag accepts the template versions); untouched-but-stale copies still update via the manifest hash or the v0.2.31 git-history heal. Companion fix: `_refresh_orchestrator_self_vco_manifest` now records TEMPLATE hashes — recording destination hashes would have made the next update see installed==prior-shipped and destroy the very file the previous run preserved.
- **v0.2.54 Track D: launcher.db migrations are now transactional** (`vct-launcher-core/src/db/migrations.rs`) — each migration's SQL batch + `_schema_migrations` record commit atomically (BEGIN IMMEDIATE / COMMIT, ROLLBACK on error). Pre-fix, a crash mid-migration (e.g. between migration 029's three `ALTER TABLE ADD COLUMN`s) left it half-applied AND unrecorded → every subsequent open retried the batch → `duplicate column name` → the launcher could never boot, with no rollback and no repair path. The three `PRAGMA foreign_keys`-dependent rebuild migrations (013/027/034) keep their own transaction boundary via an allowlist pinned by test.
- **v0.2.54 Track W: Windows hub-start hang — `install.py` froze forever at the vct-hub deploy step** — `subprocess.run(capture_output=True, timeout=15)` never returns on Windows: vct-hub's detached daemon grandchild inherits the write-ends of CPython's capture pipes (CreateProcessW with `bInheritHandles=TRUE` and no handle allowlist) and holds them for its lifetime, so the pipes never EOF and after `TimeoutExpired` CPython re-calls `communicate()` with NO timeout, blocking indefinitely. On `os.name == "nt"` all three stdio handles are now DEVNULL'd (the `/health` poll is the real success signal); the POSIX branch is byte-identical to prior behaviour. Companion: `first-install.bat`'s three unconditional `pause` statements are gated on the interactive flag, so `--yes` / CI runs exit instead of waiting on a keypress forever (static lint `test_first_install_bat_no_unguarded_pause.py` rejects regressions).
- **v0.2.54 Track C (P0-7 + C-1..C-7): self-update finalize tail unified across all four entry paths** — the V52-AI MCP-exit gate disarm + kill-sweep + V52-AH staging/handoff logic existed only on the plain `--update` path; the resume / merge / rebase paths (the divergence-modal buttons) had NEITHER protection, so both v0.2.52-class bugs were reachable verbatim through them. All four paths now share one `finalize_update_and_restart` tail. In the same pass: hub restart ordered AFTER the staging/handoff decision so vct-updater can swap `vct-hub.exe` (C-1); no more leaked `.update-in-progress.json` with a fresh 15-min deadline after resume/merge/rebase (C-2); vct-updater writes an authoritative `update.result.json` before relaunch and the boot probe reads OUTCOMES instead of inferring from lock timestamps — no more false-positive "recovered" toasts (C-4); the double-updater window under `VCT_AUTO_RESTART_LAUNCHER=1` is eliminated (C-5); a successful terminal Option-B run clears the resume sentinel so the purple Continue-Update badge stops persisting (C-6); hub respawn is gated on the update lockfile in both session-start hooks AND `hub_launcher` (C-7); `detect_arch` no longer emits `linux-x86_64`/`macos-x86_64` slot names that matched no dist dir, and the macOS dist slot is arch-aware (`macos-x64` vs `macos-arm64`) across install.py, restart.rs, installer.rs and the launch scripts — Intel Macs previously got the wrong binary slot. Boot recovery reports are cached in app state (`UpdateRecoveryCache`) and served one-shot (the previous implementation destructively re-read an already-deleted file and had zero frontend callers).
- **v0.2.54 Track I (P0-3): the vendored Excalidraw MCP was dead on arrival** — the fork shipped without `node_modules`, so `node dist/mcp/index.js` died on its first `import`. It now ships a self-contained esbuild bundle (`dist/mcp/index.bundled.js`, esbuild pinned exact, `NODE_ENV=production` baked in); `excalidraw_proxy.py` prefers the bundle with unbundled fallback, and a live smoke test does a real subprocess JSON-RPC initialize round-trip (not argv-shape). `npm-shrinkwrap.json` (865 pinned resolutions) makes the legacy `npm install -g` path reproducible too (C-EX-2). The diagrams editor's `POST /save` route — previously unauthenticated on a localhost port — now requires a per-boot bearer token (`<vct_root_dir>/diagrams.token`, 0o600, rides the editor URL fragment so it never appears in requests) plus an Origin allowlist (C-EX-3). Modularity: the token primitives (generate / 0o600 persist / constant-time eq / Bearer parse) are extracted to `vct-launcher-core::services::boot_token`; vct-hub's `auth.rs` and `module_db_api`'s token generator now delegate — `hub.token` and `diagrams.token` share one implementation.
- **v0.2.54 Track E (P0-1): the Rust CI gate only tested 1 of 4 workspace members** — `scripts/test-keychain-safe.sh` ran `cargo test --lib` against the launcher manifest, which builds only `vct-launcher-temp`; vct-launcher-core, vct-hub and vct-updater tests never ran in CI, so a regression there landed green. Switched to `cargo test --workspace --tests` + a separate vct-cli sub-workspace step. Theme 6 in the same track: release.yml's pre-release-gate no longer wraps install.py/install-bundle in `|| warning` swallows (failures are hard again), installer-smoke triggers broadened to `vco_lib/**`, and the tri-OS matrix re-shaped so fedora-40 actually gets its own job. P0-8: the leak-detector script no longer embeds the literal secrets it scans for (prefix patterns instead), and personal/author paths are scrubbed from tracked sources.
- **v0.2.54 Track G (G-1): `--uninstall` now unregisters boot autostart on all 3 OSes** — install.py registers the container-stack boot service unconditionally (systemd user unit / LaunchAgent / `ClaudeMcpContainers` Scheduled Task) and the GUI can opt into vct-hub boot autostart, but `_run_uninstall` removed neither: after clone deletion every boot retried `scripts/launch-claude-mcp-stack.*` from a deleted path forever. New `vco_lib/boot_service_cleanup.py` (soft-fail, audit-line contract, never raises) + uninstall step 5 invoking it and `vct-hub --unregister-boot`. Closes the known-gap Track F documented.
- **v0.2.54 Track G (G-6): five hooks spawned helpers with hardcoded `pwsh`** — hooks launch under `powershell` (5.1, every Win10+) but spawned children via `pwsh` (PowerShell 7+ only), so on PS 5.1-only machines KG sync, the write-gate, dup-detection, RL launcher and code-graph updates all failed silently. New shared `templates/hooks/_lib/resolve-powershell.ps1` (pwsh → powershell fallback) used by all five spawn sites; the dead `kg-duplicates.ps1` probe is dropped.
- **v0.2.54 Track G (G-8): Windows boot `status()` reported Disabled tasks as Enabled on non-English systems** — `schtasks /FO LIST` localises VALUES (German: `Aktiviert`/`Deaktiviert`); the parser defaulted to Enabled when neither English marker appeared and `status()` early-returned on Enabled, making the locale-invariant PowerShell fallback unreachable — on exactly the systems it was written for. The parser now returns `Option<BootStatus>` (None = unparseable → PowerShell fallback), pinned by a German-locale test.
- **v0.2.54 Track G (G-9): gui-tester agent + gui-test skill hardcoded `/tmp/gui-report/`** — nonexistent on native Windows. Both now use the system temp dir with a shared `vct-gui-report` folder name (`tempfile.gettempdir()` / `$env:TEMP`), with POSIX + PowerShell runner variants in the skill.
- **v0.2.54 Track G (G-7): Windows GUI auto-spawn now actually exists — and a v0.2.53 CHANGELOG correction**. The v0.2.53 entry "Thin OS shims with 3-step orchestration" claimed the shims "(4) auto-spawn `scripts/post-install-launcher.{sh,ps1}` unless `--no-auto-launch` / `-NoAutoLaunch`" and that "Linux/macOS shims pass `--no-auto-launch` through to install.py". Two corrections against the shipped code: (1) `post-install-launcher.ps1` never spawned the GUI — it only wrote Desktop/Start Menu shortcuts and explicitly listed auto-launch as out of scope, so the `install.ps1` path ended without the launcher opening (POSIX users got the spawn via the bash helper's Step 5; Windows users didn't). The .ps1 now performs the detached `Start-Process` spawn when `-NoAutoLaunch` is absent, same soft-fail contract as the bash sibling. (2) The POSIX shims deliberately CONSUME `--no-auto-launch` (install.py has no such argparse flag — passing it through would error); the flag controls the shim's own Step-3 spawn decision.
- **Track H (P0-5): Pro customers saw Free gates across the launcher dashboard** — `get_feature_flags` / `update_orchestrator_setting` / `toggle_mcp_server` derived the tier from a frontend-supplied Supabase `profiles.apps` list that license-key activation never populates (the ActivationModal flow writes the keychain + `tier_cache`, not `profiles.apps`), so every license-key-activated Pro/MAO user was gated as Free in the Orchestrator Dashboard. Tier now resolves server-side from the cached `tier_cache` row (the same source `license_get_tier` serves), `OrchestratorTier::from_apps` is deleted, and the tier ladder is unified on a single `vct_launcher_core::licensing::tier_rank` (extracted from `commands/modules.rs`; mirrors `validator.py::TIER_ORDER`, so `enterprise`/`admin` rank above `mao` instead of falling back to free).

- **v0.2.54 G-1 (Wave 0): Windows install-smoke CI gates were red from the day they landed — 6 distinct defects fixed** (`first-install.bat`, `install.ps1`, `.github/workflows/installer-smoke.yml`, `.github/workflows/install-smoke-tri-os.yml`):
  1. `first-install.bat` had **no help handler**: CI's `first-install.bat /help` parser smoke fell through `%*` into `install.ps1` (also helpless) and ran a REAL ~3-minute install on the runner, then exited non-zero (run 27316431468). The .bat now answers `/help`, `/h`, `/?`, `--help`, `-h`, `-?` with usage + exit 0 before any side effect; `install.ps1` gains a matching `-Help` / `--help` gate ahead of the WSL guard.
  2. The `windows-entry-point-smoke` step invoked the .bat **without `call`** — cmd.exe chained to it and never returned, so the step's findstr guard + exit logic were dead code. Step rewritten with `call` + assertions (exit 0, usage text, no parser-error strings), and the entry-point files (`first-install.bat`, `install.ps1`, `first-install.sh`, `first-install.command`) were added to the workflow's trigger paths (previously a .bat change could not re-run its own gate).
  3. `install-smoke-tri-os.yml` carried **three cmd.exe parse bombs** — `(Track B pending)`, `(Track C pending)` and `artifact(s)` in echo text inside `( ... )` blocks. cmd.exe parses the whole compound before executing; the unescaped `)` aborted each step at parse time with `"; was unexpected at this time."` exit 255 (run 27340118156). All reworded paren-free; directory checks also moved off the legacy `dir\NUL` trick (unreliable on NTFS) to the trailing-backslash idiom.
  4. tri-os ran full installs **without `--no-containers --skip-models`**, guaranteed failures on GitHub runners (ubuntu: rootless podman.socket inactive → compose up dead; macos-14 + the fedora container: no runtime at all). All three first-install steps now pass the same flags the green installer-smoke jobs use; the bootstrap-post assertion tolerates container-runtime blockers (expected under `--no-containers`) while still failing on any other blocker.
  5. tri-os step 7 (`launcher --check-only`) invoked a flag that **does not exist** in v0.2.53 binaries — `handle_cli_args()` recognises only `--set-storage-config` / `--register-default-mcps`, so the binary fell through to GUI startup: a GTK/display hard-fail on headless Linux (not matching the "unrecognized flag" skip filter) or a hung window on Windows until the 25-min job timeout. Replaced with an explicit documented no-op until Track C lands the flag (re-enable contract in the workflow comment).
  6. The tri-os matrix `include` (`os: ubuntu-22.04` + `container: fedora:40`) **merged into** the existing ubuntu-22.04 combination instead of adding a fifth job — the bare ubuntu-22.04 coverage silently ran inside the fedora container under a misleading job name, and the intended `fedora-40` job never existed. Include removed (bare ubuntu-22.04 restored); proper Fedora coverage via a synthetic matrix key is Track G scope.
  - `first-install.bat` `--no-auto-launch` now matches the `first-install.sh` / `.command` contract: it skips the **entire** post-install launcher phase (binary probe + download + source build + spawn), not just the final GUI spawn. Previously a stale bundled binary sent `--yes` runs into a multi-minute `pnpm tauri build` fallback — in CI, a 25-minute-timeout kill.
  - New regression tests: `tests/test_workflow_cmd_paren_safety.py` (cross-platform static lint over every `shell: cmd` step — rejects paren-in-echo-inside-block and missing-`call` patterns; detector self-tested against all four shipped bugs) and `tests/test_first_install_bat_cmd_parse.py` (live cmd.exe/powershell.exe help-contract tests on Windows + cross-platform ordering guards that the help gates precede side effects).
  - Doc truth fixes in the same files: `first-install.bat` header no longer claims "Status: STUB" (the file is a ~680-line full entry point), and `launcher/dist/windows-x64/README.md` no longer claims the directory is "intentionally empty" (it ships vct-launcher.exe + vct-hub.exe + vct-updater.exe with metadata sidecars).
- **v0.2.54 G-1.5 (Wave 0 follow-up): default first-run install fell into the 15-30 min source-build worst case on every OS — dist staleness + Windows zip-vs-exe download filter** (`first-install.bat`, `scripts/lib/download-launcher-asset.ps1` (NEW), `scripts/check-dist-freshness.py` (NEW), `.github/workflows/release.yml`, `launcher/dist/linux-x64/*`):
  1. **Windows download filter never matched any real asset**: `first-install.bat`'s inline downloader filtered release assets with `$_.name.EndsWith('.exe')`, but release.yml has shipped ONLY `vibecoded-orchestrator-<version>-<os>.zip` since the 2026-05-10 uniform-zip packaging change (verified against the live v0.2.53 release: 3× `.zip` + 3× `.zip.sha256`, zero `.exe`). Every Windows first-run hit `NO_ASSET` and silently fell through to the `pnpm tauri build` path — 15-30 min with a Rust+Node toolchain, a loud failure without one. The downloader now lives in `scripts/lib/download-launcher-asset.ps1`: prefers the windows `.zip` (excluding `.sha256` sidecars), downloads to `%TEMP%`, `Expand-Archive`s, lands `vct-launcher.exe` + `vct-hub.exe` + `vct-updater.exe` in `%LOCALAPPDATA%\vct-launcher\`, and keeps the bare-`.exe` asset as a legacy fallback. Windows sibling of the POSIX M-P0-3 fix that shipped in v0.2.53 for `scripts/post-install-launcher.sh`.
  2. **All three OS dist sidecars were stale at the v0.2.53 tag** (`source_hash fd215c7a`, built at v0.2.52, vs live `449e6cc6`), so both install-time freshness readers rejected the bundled binary on every fresh clone and forced the (Windows-broken) download fallback. The linux-x64 binaries (`vct-launcher` + `vct-hub` + `vct-updater` + sidecars) are rebuilt from the current tree (`source_hash 449e6cc6`, 253 embedded frontend-asset refs, post-build smoke-run verified); macos-arm64 + windows-x64 binaries cannot be cross-compiled from a Linux maintainer box and are refreshed by the release build matrix at the v0.2.54 tag (their staleness now degrades gracefully into the *working* zip download on all three OSes instead of a source build).
  3. **New regression gate `scripts/check-dist-freshness.py`**: compares every `launcher/dist/<os>/<binary>.metadata.json` `source_hash` against the live launcher-subtree hash (same `git ls-tree` recipe as the writer + both shell readers; parity pinned by test) plus a cross-OS consistency check (three builds must agree — the v0.2.49 mislabeled-binaries class). Wired into release.yml twice: **Gate 3b** in `pre-release-gate` (`--mode warn`, advisory under the current refresh-after-release flow) and a **strict** post-staging check in `commit-dist-binaries` (fails before push if the freshly built artifacts don't match main HEAD's launcher source). Gate 3 alone could not catch v0.2.53: it only proves `launcher/dist/` *changed* since the previous tag, not that it matches *this* tag's source. Track E: flip Gate 3b to `--mode strict` when the binary refresh moves pre-tag.
  - New regression tests: `tests/test_download_launcher_asset_ps1.py` (live end-to-end pwsh runs of the real helper against a localhost fixture server whose asset list mirrors the actual v0.2.53 release — the pre-fix filter exits `NO_ASSET`/2 on that exact payload; plus NO_ASSET / legacy-exe / no-binary-in-zip exit-code contracts and static wiring guards) and `tests/test_check_dist_freshness.py` (live subprocess runs against synthetic git repos reproducing the exact v0.2.53 stale state + the v0.2.49 cross-OS inconsistency class + writer/gate hash-input parity).

### Fixed (anti-divergence)

- **v0.2.54 Track D (live-test finding): `migrate-collections --name X` leaked the ambient `DIAGRAMS_COLLECTION`** — only KG + Dev env keys were re-scoped from `--name`, so running the CLI from a shell carrying another project's `.claude/settings.json` env put THAT project's live Diagrams collection into the migration plan (and `--force-rebuild` rebuilt it). All three collection env keys are now derived from `--name`; regression test pins it.
- **v0.2.54 Track D Step 0: backported the dogfooded workflow-detection support** to `templates/scripts/agent-skill-keyword-match.py` (+118 lines: `.claude/workflows/*.{mjs,js}` keyword matching via `meta.keywords` or a `// keywords:` comment, main-loop-only gating, `w:`-prefixed dedup keys) plus its 11 tests — the runtime copy had diverged from the template and the next `install.py --update` would have silently reverted it. The new shared-KG guard literals in `maintain_knowledge_graph.py` are pinned to the canonical constants by `tests/test_shared_kg_constant_consistency.py`.

### Removed

- **Track H (P0-5 watermark decision): the never-shipped watermark gate is gone** — `TIER_FEATURES["watermark_disabled"]` (validator.py), the `watermark_enabled` config field + dashboard toggle + preferences row, and the `VCT_WATERMARK` env emission in `apply_mcp_to_claude_settings` are all removed. The watermark concept never had a consumer: nothing read `VCT_WATERMARK`, and no code path called `feature_enabled("watermark_disabled")`. Existing `orchestrator.json` / `.claude/settings.json` files that still carry the old key/env entry parse fine (unknown fields ignored; stale env entries are inert).
- **Track H (P0-6): LemonSqueezy API key removed from the client bundle surface** — `VITE_LEMONSQUEEZY_API_KEY` is gone from `launcher/.env.example` and `launcher/docs/SETUP.md`, and the legacy zero-importer `launcher/src/lib/stores/licenses.ts` store (which embedded the key into the webview bundle via `import.meta.env` and called the LemonSqueezy API directly from the client) is deleted along with its only callee `auth.markAppActiveLocal`. License validation goes exclusively through the Rust backend → Supabase `validate-tier` edge function; the key never belonged in a Vite-bundled client.
- **Track H: placeholder admin "License issuance test" page removed** — `/admin/license-issuance-test` shipped as a "Backend hookup pending" stub yet was wired live in the admin sidebar. The sidebar link and the route are removed; license verification lives in the ActivationModal (`license_activate`/`license_refresh`) and the admin Diagnostics page.

### Changed

- **Track H (H-7): `revalidateTierViaSupabase` deduplicated across edge functions** — the identical 3× copies in `rl-artifact-url` / `rl-latest-version` / `rl-latest-weights` are extracted to `launcher/supabase/functions/_shared/tier_revalidation.ts`, and the 7× copied `CORS_HEADERS` + `jsonResponse` boilerplate is consolidated behind `_shared/http.ts` (parametric, preserving each function's exact wire headers). A future security fix to the tier-revalidation path now lands in one file instead of three.
- **Track H (H-5/H-6): licensing docs brought up to truth** — `docs/license/README.md` now documents the full `free < pro < mao < enterprise < admin` tier ladder (was: pro/enterprise only) and no longer lists a "coordination layer" feature that has no `TIER_FEATURES` entry; `installer_engine.rs`'s `container_pull` doc header no longer claims the pull-token gateway is "not deployed yet" (contradicting the v0.2.35 canonical-path comment 80 lines below).
- **v0.2.54 Track A2: template/docs truth sweep + 3,566 lines of legacy semi-manual install scaffolding removed** — 13 of 14 audited items closed across `templates/` + `docs/`: phantom references deleted (the legacy `coordination` MCP, nonexistent design skills, the never-shipped `session-end.sh` hook, dead `vct-ollama` manifests, the phantom `search_knowledge_graph` MCP tool), the `SHARED_KG_READ_DISABLED` gate documented (implemented since v0.2.46; the template claimed "no read opt-out"), hook-count drift corrected (31 → 36, canonical from vct-module.json), and the MCP list normalised to one canonical phrasing. The big cut: `project-migrator.md` (815 lines) is archived and `project-bootstrapper.md` (1271 → 104) / `orchestrator-installer.md` (1443 → 146) / `helper-scripter.md` (901 → 793) are re-scoped — their phase-by-phase manual install walkthroughs duplicated (and contradicted) the canonical `install.py --add-project` / launcher-GUI path that replaced them in v0.2.4x. The amendment pass closed 6 phantom-ref gaps the sweep itself introduced or missed.
- **v0.2.54 S-2: agent/skill keyword-frontmatter quality sweep** — all 45 agent + 53 skill keyword lines re-derived as multi-word intent-anchored phrases; bare generic tokens (`prompt`, `workflow`, `LLM`, `RAG`, `Weaviate`, ...) that fired on incidental mentions are gone. Single words survive only for unambiguous proper nouns.
- **v0.2.54 Track G (G-4): hook-flavour + script-pattern policy unified in `vco_lib.bundle_globs`** — three divergent answers to "which hook flavours ship" coexisted (install.py: native-only; project_init: both flavours — the correct v0.2.14 policy; orchestrator-self materialize: everything by accident), and the script-copy pattern list was inlined in two places that had ALREADY drifted (per-project bundles silently missed the v0.2.54 extension-less workflow wrappers). Both policies now live in one module; both flavours ship everywhere (dual-boot / WSL-crossover / network-mount projects invoke hooks from both shells).
- **v0.2.54 Track J: cross-cutting helper dedup** — the four `_atomic_write_text` copies in `vco_lib/{env_template,config_projection,deferral_report,cli/codegraph_diagram}.py` (plus the inline block in `DeferralReport.write`) are thin delegates to `vco_lib.atomic.atomic_write_text` (the consolidation `vco_lib/atomic.py`'s own v0.2.53 docstring queued for v0.2.54); the SIX copies of the log-tail truncation recipe (three `LOG_TAIL_MAX_BYTES` consts + `floor_char_boundary` fns + capping closures in the db writers, three `tail_log` siblings in the launcher command layer) are consolidated into `vct-launcher-core::db::log_tail`; the FOUR byte-identical `error_response` envelope builders in vct-hub's `modules_api`/`lifecycle_api`/`secrets_api`/`config_api` are consolidated into `vct-hub::http_error` (wire shape unchanged, now pinned by test).

## [0.2.53] - 2026-06-11

A **cross-OS install hardening + architecture-v2 foundation** release.
Triggered by a real macOS first-install attempt that surfaced 6+ silent
bugs in 30 minutes; expanded by 13 parallel Opus audits into a
~100-issue master plan, then dispatched via 1 design doc track (G) + 9
implementation tracks (A/B/C/D/E/F/G2/G3/H) in worktree isolation. The
v0.2.53 cycle also caught and fixed two P1 production bugs that
silently shipped in v0.2.52: V52-M hooks were dead on POSIX (missing
exec bit) and on Windows PowerShell 5.1 (missing UTF-8 BOM).

Master plan: `.claude/context/plans/v0.2.53-master-plan-FINAL-2026-06-10.md`.
Install architecture v2 design doc: `docs/INSTALL_ARCHITECTURE_v2.md`.

### Added

- **Install architecture v2 + `install.py --bootstrap` mode** (`install.py:618-1700`, `install.py:1524` (`_bootstrap_dispatch`), `docs/schemas/install-bootstrap-envelope-v1.json`, `docs/INSTALL_ARCHITECTURE_v2.md`): Track G's design + Track B's implementation land the **bootstrap prepass** mode. `python install.py --bootstrap --json` emits a read-only system-detection envelope (Python/Node/Podman/Docker versions, GPU + RAM + OS, package_manager_advice, weaviate_endpoints, `ready_to_install` flag, `missing_prereqs[]`). The envelope is the diagnostic surface for failed installs + the v0.2.54 building-block for prepass-based blocker detection. `--bootstrap` is **exclusive** with `--update`/`--lightweight`/`--uninstall` (it's the probe-only mode, NOT the install entry point). Schema is at `docs/schemas/install-bootstrap-envelope-v1.json` with 100+ fields. `install.py:1534` enforces the exclusivity.
- **Thin OS shims with 3-step orchestration** (`first-install.sh`, `first-install.command`, `install.ps1`): the shims now run a deterministic sequence — (1) Python detect via OS-appropriate cascade; (2) bootstrap prepass → `state/logs/bootstrap-prepass.json`; (3) full `install.py <args>`; (4) auto-spawn `scripts/post-install-launcher.{sh,ps1}` unless `--no-auto-launch` / `-NoAutoLaunch`. Linux/macOS shims pass `--no-auto-launch` through to install.py. Windows `install.ps1` honors `-NoAutoLaunch` declared in its existing param block. Apple Silicon shim cascades through `/opt/homebrew/opt/python@3.13/bin/python3.13` → `/opt/homebrew/bin/python3.13` → PATH; Linux cascades through distro python3.13 → linuxbrew; Windows via `py` / `python` / winget.
- **Six new `vco_lib/` modules per Section 7 of design doc** (`vco_lib/atomic.py`, `vco_lib/hashing.py`, `vco_lib/timeutil.py`, `vco_lib/git_meta.py` — implemented; `vco_lib/settings_merge.py`, `vco_lib/manifest.py` — skeletons for v0.2.54): the four implemented modules are migration targets for in-flight install.py refactors. The two skeletons land now so other tracks can target the canonical import paths without rename churn.
- **Tri-OS install smoke CI gate** (`.github/workflows/install-smoke-tri-os.yml`): runs `first-install.{sh,command,bat}` end-to-end on `ubuntu-22.04` + `ubuntu-24.04` + `macos-14` + `windows-latest` + `fedora-40-container` on every PR + push + daily 06:00 UTC. Fresh `git clone` (not `actions/checkout`) so the test exercises the third-party-user experience. Promoted to release-gating via Gate 22 in `scripts/pre-ship-check.sh`. CI catches the bug class that produced Luciano's macOS first-install failure (no end-to-end install test ran on macOS pre-v0.2.53).
- **vct-hub schema-aware Weaviate class resolver** (`launcher/src-tauri/vct-hub/src/weaviate_schema_probe.rs` (NEW) + `config_api.rs:751-761`): closes Fabio Symptom B end-to-end. The hub now probes `/v1/schema` for actual on-disk class casing (case-insensitive match) before deriving `development_collection` via the suffix-swap rule. install.py's case-rebind at `install.py::_resolve_existing_casing` (line 13673) is the second layer; either side independently adopts the on-disk casing if its env is unset. Pre-fix: hub's pure suffix-swap returned `VibeCodedOrchestrator_Development` (camelCase) regardless of on-disk state, so WRITES against a lowercase `Vibecodedorchestrator_Development` collection failed with 500 "class already exists, similar class". 13 Rust tests.
- **Project bundle install/update sentinel + symlink-blocking + FS-disable contract** (`vco_lib/project_init.py::install_project_bundle` + `launcher/src-tauri/src/commands/projects_v2.rs::run_install_bundle_update_with_root` + `launcher/src-tauri/src/commands/project_state_cmd.rs:372-401,455-469`): three project-bundle bugs B1/B2/B3 closed. **B1** adds a Bug-A-equivalent resume sentinel for project bundle updates (mid-update Cmd-C no longer corrupts the per-project manifest). **B2** implements the previously-missing FS-disable contract: `set_project_{agent,skill}_enabled` Tauri commands now move the file between `.claude/{agents,skills}/` and `.claude/{agents,skills}.disabled/` BEFORE flipping the DB row. If the FS move refuses to clobber (corrupt both-locations state), the DB flip is skipped — DB and disk never drift out-of-sync. **B3** adds symlink-blocking to the per-project `_write_file_atomic` (the orchestrator-self variant already had V47-B defense). 4 Rust integration tests pin the FS+DB chain composition for B2.
- **`canonical_class_prefix` SSOT consolidation (NEW-10 / DEDUP-6)** (`vco_lib/project_naming.py::canonical_class_prefix` + `vco_lib/project_init.py::sanitize_for_weaviate_class`): the four colliding sanitisers (`canonical_class_prefix`, `sanitize_for_weaviate_class`, `_sanitize_collection_prefix`, `_sanitize_kg_collection`) consolidate to **2 SSOTs** (underscore-DROPPING vs underscore-PRESERVING — production Weaviate has classes named by BOTH rules; strict deletion would orphan existing collections). The other two sanitisers become thin wrappers that delegate to their SSOT. New AST guard at `tests/test_canonical_class_prefix_parity.py::test_no_fifth_sanitizer_in_vco_lib` prevents future drift. Reframes Track G's design-doc "1 SSOT" plan to match the audit warning at `vco-lib-python-dedup-2026-06-10.md` Finding 1 (the v0.2.15 wedge).
- **DEDUP-1 `_run_logged_subprocess` helper** (`install.py::_run_logged_subprocess` at `install.py:10844`): replaces 7 silent-hang `subprocess.run(capture_output=True, text=True)` callsites (pip-upgrade, pip-install, editable installs, npm, playwright, compileall) with a consolidated helper that streams output via line-buffered tail-on-failure, surfaces a dot-cycle animation after 3s for long-running steps, enforces finite timeouts + retries + `--prefer-binary` for pip, logs success/failure via `_log_install_event`. The dot-cycle is gated by `sys.stdout.isatty()` (CI runs stay quiet). 20 tests including 4 new tests exercising the animation thread.
- **DEDUP-2 `_download_release_binary` helper** (`install.py`): consolidates the 95%-identical `_try_download_launcher_binary` + `_try_download_vct_hub_binary` (both were ~90 LoC of near-duplicated gh-then-curl fallback + ZIP extract + chmod).
- **DEDUP-3 `_make_deferral` helper** (`install.py`): 1 of 51 `DeferralEntry(...)` constructors migrated (mechanical sweep of the remaining 50 staged for v0.2.54).
- **DEDUP-4 launcher.db readonly URI migration** (`install.py`): inline `sqlite3.connect(timeout=5.0)` calls migrate to a URI form with `mode=ro&immutable=1` so install-time launcher.db reads no longer block on a concurrent writer's lock. `install.py`'s URI is stricter than the `vco_lib.launcher_db_reader._open_db_readonly` helper (which uses `mode=ro` only); the staged unification with vco_lib is queued for v0.2.54.
- **DEDUP-5 `.claude.json` atomic-write with `.tmp` cleanup** (`install.py`): inline atomic-write migrates to `vco_lib._atomic_write_text` so the `.tmp` file is deleted on every exception path (was: orphan-accumulation bug on failed install runs).
- **DEDUP-14 paired sentinel+deferral writer** (`launcher/src-tauri/src/commands/installer.rs::write_resume_sentinel_and_deferral`): 3 callsites at `installer.rs:4325, :5779, :5946` now go through a single helper that writes both the sentinel + the deferral atomically. Forgetting to write one was the v0.2.51 Bug A pattern; the helper eliminates the bug class.
- **DEDUP-15 shared frontend-asset substring helper** (`scripts/lib/asset-ref-count.sh` + `.ps1`): 7 callsites that historically used the narrow `_app/immutable/assets` substring (which false-rejects healthy Svelte 5 builds emitting `_app/immutable/{chunks,entry,nodes}/`) now source the helper and use the broad `_app/immutable/` marker. Includes a 3-backend cascade (POSIX `strings | grep`, Git Bash PowerShell byte-scan, last-resort `grep -aoc`) absorbed from `scripts/build-bundled-launcher.sh` + `scripts/check-bundled-binaries.sh` so both CI scripts also source the helper (lost ~24 LoC each).
- **PATH augmentation for graphical-launch contexts** (`launcher/src-tauri/vct-launcher-core/src/services/runtime.rs::augment_path_for_graphical_launch` + `launcher/src-tauri/src/lib.rs::setup`): closes M-P0-7 (the launcher inherited Finder's minimal PATH on macOS / the systemd-user PATH on Linux `.desktop` launches). The augment prepends OS-canonical user-install paths (Apple Silicon: `/opt/homebrew/bin`, `/opt/homebrew/sbin`, `~/.cargo/bin`, `~/.local/bin`; Linux: `~/.local/bin`, `~/.cargo/bin`, `/home/linuxbrew/.linuxbrew/bin`, `/snap/bin`, `/var/lib/flatpak/exports/bin`; Windows: no-op) only when the entry is missing from PATH. User-set PATH order is preserved. Track G3's L-P0-4 test verifies node/npm/cargo/joern/lean-ctx are findable from a `.desktop`-launched context. 3 Rust unit tests + 4 Rust integration tests.
- **Install gate refresh-on-focus + "Re-check" + "Run installer now" buttons** (`launcher/src/lib/components/InstallHealthGate.svelte` + `launcher/src/lib/stores/install-state-store.ts` + `launcher/src-tauri/src/commands/installer.rs::launch_installer_terminal`): closes M-P0-8 + M-P1-6. The gate re-runs `check_install_health` on every `window.focus`. New "Re-check" button forces an immediate re-probe. New "Run installer now" button spawns an OS-appropriate terminal at the install root: `osascript -e 'tell application "Terminal" to do script ...'` on macOS, fall-through chain `gnome-terminal` → `konsole` → `xfce4-terminal` → `xterm` on Linux, `cmd.exe /K` on Windows.
- **Cross-clone localStorage flag scoping** (`launcher/src/lib/stores/install-state-store.ts` (NEW) + `launcher/src/lib/components/InstallHealthGate.svelte` + `launcher/src/lib/stores/updater.ts` + `launcher/src/routes/+layout.svelte`): closes M-P1-5. Four launcher localStorage flags (`install_check_dismissed`, `onboarding_force`, `show_changelog_after_update`, `vct.update.seen_version`) now scope per-install-root path. Pre-fix: WebKit origin is `tauri://localhost` for every Tauri app, so two orchestrator clones on the same machine shared the same dismissal flags (clone-A's "let me through" silently hid the gate on clone-B's broken install). One-shot legacy-cleanup helper removes unscoped keys on first boot under the new logic. 14 Svelte component tests.
- **Update-event toast handler (V52-AH-FE)** (`launcher/src/lib/components/UpdateToast.svelte` (NEW) + `launcher/src/lib/update-toast-handlers.ts` (NEW) + `launcher/src/routes/+layout.svelte`): V52-AH (v0.2.52) shipped the backend that emits `vct-update-recovered` / `vct-update-failed` Tauri events at launcher boot post-Velopack-handoff. v0.2.53 wires the FE handler that subscribes to both events + renders a one-shot toast. Silent on POSIX by design (V52-AH backend is Windows-only meaningful). 12 unit tests.
- **Svelte parser + PowerShell parser in code-graph indexer** (`templates/scripts/analyze_code_graph.py`): V52-O.11.B adds a Svelte parser for `<script>` and `<script lang="ts">` blocks + top-level functions + exports + reactive declarations (`$:`) as pseudo-functions; V52-O.11.N adds a PowerShell parser for `function Name { ... }` + `function Name() { ... }` shapes including balanced-paren param-block detection. Pre-fix: 244 `.svelte` files and 168 `.ps1` files indexed as zero functions; post-fix each file's top-level functions index correctly. 10 + 10 tests.
- **Linux distro broadening** (`install.py:7338-7378` + `install.sh:107-138/263-291/378-403` + `scripts/post-install-launcher.sh::case`): closes L-P0-1. zypper + apk arms added to the Python / Node / Podman install flows. Pre-fix: install.py's pkgmgr ladder advertised zypper + apk via `_detect_pkgmgr` but the install flows had no branch for them. New `tests/test_distro_pkg_mgr_parity.py` asserts install.py + install.sh + post-install-launcher.sh advertise the same pkgmgr set.
- **`libwebkit2gtk-4.1-dev` / `-4.0-dev` fallback** (`scripts/post-install-launcher.sh`): closes L-P0-2. Ubuntu LTS < 24.04 + Debian 12 stable only have `-4.0-dev`; the v0.2.52 hard-pin on `-4.1-dev` broke every install on those distros. Both versions probed; whichever is available is installed.
- **NVIDIA Container Toolkit install hint** (`install.py::_nvidia_container_toolkit_install_hint`): closes L-P0-7. install.py now prints the pkgmgr-aware install command when NVIDIA is detected but the container toolkit is absent (`curl https://nvidia.github.io/libnvidia-container/gpgkey | ...` for apt, `dnf install nvidia-container-toolkit` for dnf, etc.). Pre-fix: containers silently couldn't access the GPU; users blamed the GPU detection.
- **SELinux `:Z` bind-mount hint + render/video group hint** (`install.py:7184-7305`): closes L-P0-3 + L-P0-8. `_detect_selinux_enforcing` probes `getenforce` + sysfs fallback. When SELinux is enforcing, install.py emits an informational hint that bind-mount volumes need a `:Z` suffix (named volumes are unaffected; the default install layout uses named volumes so this is hint-only for now). When NVIDIA + EACCES on `/dev/dri/renderD128` is detected, install.py prints `sudo usermod -aG render,video $USER && newgrp render`.
- **KDE Plasma 6 `first-install.desktop` quoting fix** (`first-install.desktop`): closes L-P0-5. The `%k` field code doesn't survive `cp` to `~/Desktop` (it resolves at the desktop file's ORIGINAL location); the new `Exec=` uses an HOME-rooted explicit path. KDE Plasma 6's stricter Desktop Entry parser also rejected the v0.2.52 `Exec=` quoting; the new form is GNOME + KDE + XFCE compatible.
- **Python wheel-coverage detection + clear refuse for 3.14+** (`install.py:6979`): closes M-P1-1. `pip install --dry-run --only-binary=:all:` probes whether all requirements have prebuilt wheels for the current Python version. If not, install.py refuses with a clear "Python 3.X is too new for the current dependency set; install Python 3.13 via `brew install python@3.13` / `apt install python3.13` / `winget install Python.Python.3.13`" message. Pre-fix: Python 3.14 users would pass the `>= 3.11` check and then silently hang for 30-60 min in Step 4 building scientific deps from source.
- **Podman machine auto-init on macOS + Windows** (`install.py::_podman_machine_auto_init_and_start` at `install.py:8149`): closes M-P1-2. install.py now detects the "Podman binary installed but machine never initialized" state and offers to run `podman machine init --memory 4096 --cpus 2 --disk-size 30 && podman machine start` interactively (or runs it under `--yes`). Pre-fix: install.py's container probe required `podman info` to succeed, which fails when the machine isn't running, so the install would report "no container runtime" despite Podman being installed.
- **Pip flags hardening** (`install.py::_pip_subprocess_env` + `_pip_install_flags`): closes M-P1-3. Adds `--timeout 60`, `--retries 5`, `--prefer-binary`, `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PIP_NO_INPUT=1`. Pre-fix: pip could silently hang indefinitely on slow networks or interactive prompts.
- **Windows hardening (W-P1-1/2/3/4/5)** (`first-install.bat:127-137,315` + `install.ps1::install_args` + `templates/scripts/vct_project_config.ps1` + `templates/scripts/vct_access_check.ps1` + `install.py:18378-18379`):
  - W-P1-1 — `first-install.bat:_log_event` JSONL writer is now apostrophe-safe (passes step/phase/detail via env vars read by `[Environment]::GetEnvironmentVariable`; row built via `ConvertTo-Json` on a PSCustomObject).
  - W-P1-2 — `first-install.bat:after_winget_install` no longer relies on Chocolatey's `refreshenv` shim (which silently no-ops on stock Windows). Reads `[Environment]::GetEnvironmentVariable('Path', 'Machine' + 'User')` from the registry and re-splices into `%PATH%`.
  - W-P1-3 — `installer-smoke.yml` Windows job switches `shell: bash` → `shell: cmd` for `first-install.bat` so the .bat is actually exercised in CI. New job runs `install.ps1` under both `pwsh` (PS 7) and `powershell` (PS 5.1).
  - W-P1-4 — `vct_project_config.ps1` + `vct_access_check.ps1` removed `-SkipHttpErrorCheck` (PS 7+ only). Added PS 5.1-compatible 3-arm try/catch for `System.Net.WebException` + `Microsoft.PowerShell.Commands.HttpResponseException` with StreamReader / `ErrorDetails.Message` body extraction.
  - W-P1-5 — `install.py::_materialize_boot_service_windows` (around line 18378) now `html.escape(quote=False)` USERDOMAIN + USERNAME before substituting into Scheduled Task XML. Pre-fix: `&` in WORKGROUP names (e.g. `ACME&CO`) produced malformed XML; `schtasks /Create` failed silently. 4 Python tests cover the exact `ACME&CO` case + USERNAME `<test>` case.

### Changed

- **DEDUP-1 finite pip timeout** (`install.py:10844`): the two pip-install sites now have a 60-second per-HTTP-request timeout + 5 retries (was: infinite timeout). On slow networks where pip is genuinely making progress slowly, users may now hit the timeout where they didn't before; the dot-cycle animation shows elapsed seconds so users can see if pip is still alive. Behaviour-changing — documented in `docs/TROUBLESHOOTING.md`.
- **DEDUP-4 readonly URI form** (`install.py`): the install-time launcher.db reads use `mode=ro&immutable=1` (stronger isolation than `vco_lib.launcher_db_reader._open_db_readonly`'s `mode=ro`). Audit E flagged this as SUBTLE-DIFF; the stronger form is intentional (the install-time reader cannot afford to compete with the launcher for the writer lock). Cross-helper unification queued for v0.2.54.
- **L-P0-3 SELinux `:Z` is hint-only, not auto-applied** (`install.py:7192`): the audit recommended hint-only because the default install layout uses named volumes (which are unaffected by SELinux). Users on bind-mount layouts apply the `:Z` themselves per the printed hint. Documented in `docs/TROUBLESHOOTING.md`.
- **localStorage flag rename** (`launcher/src/lib/stores/install-state-store.ts`): the master plan named the per-version dismissal flag `dismissed_update_version`; the actual code at `stores/updater.ts:21` uses `vct.update.seen_version`. The migration scoping covers the actual key.
- **DEDUP-15 partial coverage** (`scripts/build-bundled-launcher.sh` + `scripts/check-bundled-binaries.sh`): both CI scripts now source `scripts/lib/asset-ref-count.sh` instead of inline-duplicating ~24 LoC of backend-cascade. Pre-fix: the inline copies used the correct broad marker (matching NEW-1's intent), so there was no drift hazard; the duplication itself was tagged for v0.2.54 backlog and is now closed in-tag.

### Fixed

- **V52-M bundled hooks shipped without exec bit on POSIX (P1, was silently dead since v0.2.52)** (`templates/hooks/pre-bash-context-inject.sh` + `templates/hooks/post-bash-context-record.sh` + `templates/hooks/post-edit-outcome.sh`): `chmod +x` on all 3 files + defensive `os.chmod(target, 0o755)` in `install.py:11299-11305` after `shutil.copy2` for every `.sh` hook target. The defensive chmod prevents the bug class — future contributors who commit a hook with mode 664 will still have a working install. Pre-fix: V52-M's three hooks (PreToolUse Bash KG injection, PostToolUse Bash outcome recording, PostToolUse Edit outcome recording) were dead-on-arrival on every Linux/macOS install since v0.2.52 shipped. Caught by pre-existing `tests/test_install_hooks.py::test_install_into_fresh_target_linux`.
- **V52-M bundled hook .ps1 siblings shipped without UTF-8 BOM (P1, was silently dead since v0.2.52 on Windows PS 5.1)** (8 .ps1 files: `scripts/lib/asset-ref-count.ps1`, `templates/hooks/_lib/credscan.ps1`, `templates/hooks/_lib/snapshot.ps1`, `templates/hooks/post-bash-context-record.ps1`, `templates/hooks/post-edit-outcome.ps1`, `templates/hooks/pre-bash-context-inject.ps1`, `templates/hooks/subagent-stop-reconcile.ps1`, `tests/test_powershell_compat.ps1`): prepended `EF BB BF` to all 8 files. Pre-fix: Windows PowerShell 5.1 (the default on Win10/11) mis-decodes non-ASCII bytes as Windows-1252 unless the file starts with a UTF-8 BOM; the V52-M hooks' .ps1 siblings failed at parse + silently never fired on native Windows. Caught by pre-existing `tests/test_ps1_utf8_bom.py`. Repo's existing `scripts/fix-ps1-bom.ps1` documents the recurring fix.
- **NEW-3 Rust `python3.13` candidate missing** (`launcher/src-tauri/src/commands/installer.rs` Python candidate list at line 9687): adds `python3.13` to the launcher's Python detection list. Pre-fix: `install.sh:48` and `install.ps1:233` had it; launcher's Rust did not. A Linux user with only `python3.13` on PATH (no `python3` alias) would have install.sh succeed but the launcher's `detect_system()` would falsely report "Python not found" and fire the missing-Python modal.
- **NEW-4 Weaviate health endpoint SSOT** (`install.py:9975` + `launcher/src-tauri/src/commands/installer.rs:627`): comment at `installer.rs:627` previously claimed `/v1/meta` was "the right liveness probe" — incorrect; install.py polls `/v1/.well-known/ready` (Weaviate's documented readiness endpoint). The Rust comment is corrected and the bootstrap envelope publishes `/v1/.well-known/ready` as the canonical health endpoint at `weaviate_endpoints.health`. Locked in by `tests/test_weaviate_health_endpoint_parity.py`. Rust-side `detect_existing_services` probe still uses `/v1/meta` because that probe is a "is Weaviate reachable for any signal" check, not a strict readiness check — both endpoints work in practice; the SSOT is for canonical liveness probes elsewhere in the codebase.
- **NEW-11 resume sentinel empty-sha refusal** (`launcher/src-tauri/src/commands/installer.rs::resume_orchestrator_update` at line 6521; the empty-sha guard is inside the body): refuses to resume when the sentinel's `sha_at_conflict` is empty. Pre-fix: an empty-string fallback could let resume proceed despite HEAD not having advanced past the conflict SHA — defensive hardening for Fabio Symptom A even though Symptom A was determined to be most likely user-error rather than this code path.
- **`vct.update.seen_version` cross-clone leak migration** (`launcher/src/lib/stores/install-state-store.ts` one-shot legacy-cleanup): the per-version "dismissed update" flag is migrated to per-install-root scoping. On first boot under the v0.2.53 launcher, the unscoped legacy key is removed.
- **CVE-1 — vitest devDep in `vco_lib/excalidraw_mcp_fork/`** (`install.py::_install_pinned_npm` file-pin branch at `install.py:24365`): file-pinned MCP installs now use `npm install -g --omit=dev <local_dir>`. Pre-fix: `npm install -g <dir>` walked the full devDependencies tree, shipping ~50 MB of test/build tooling to every user install + generating Dependabot CRITICAL noise on every release page. GHSA-5xrq-8626-4rwp is non-exploitable for us (vitest --ui server never invoked) but the dead weight is removed.
- **CVE-2 — lodash-es in `launcher/package-lock.json`** (`launcher/package.json` + `launcher/package-lock.json`): synced lockfile to lodash-es 4.18.1 via the `overrides` block. lodash-es is a transitive dep through `chevrotain` (3 nested copies); a simple `npm update lodash-es` didn't sync them all, so the override is the canonical fix. GHSA-r5fr-rjxr-66jc is non-exploitable for us (the vulnerable `_.template` is dead code in our launcher; only `defaults` + `pick` are imported by `dagre-d3-es`) but the override clears the Dependabot HIGH noise.
- **PRE-1 secrets.rs doc-test failures** (`launcher/src-tauri/vct-launcher-core/src/secrets.rs:789,817`): 2-line `use` statement addition fixes pre-existing doc-test failures that reproduced against base v0.2.52 (independent of v0.2.53 work; caught during pre-existing failure investigation).
- **Test-outdated fixture sync (V52-I Fix B schema migration carry-over)** (`tests/test_vco_lib_migrate.py::_FULL_PROPS` + `_at_target_diagrams`): adds the V52-I Fix B (2026-06-09 commit `600a5c6c`) canonical temporal quartet (`created_at`, `updated_at`, `valid_from`, `valid_until`) to the KG fixture + the validity pair to the Diagrams fixture. Single fixture update fixes 4 cascading test failures in `tests/test_vco_lib_migrate.py`. Production schema was already correct; fixture was stale.
- **`pre-bash-context-inject` registered in PRETOOLUSE_LLM_BOUND_HOOKS** (`tests/test_hooks_pretooluse_json_envelope.py`): static guard registry updated for the V52-M hook (was already correct in production; the test guard was stale).
- **`migrate-development-temporal-props` broader regex assertion** (`tests/test_migrate_development_temporal_props.py`): V52-I Fix B broadened the script to target `_KnowledgeGraph` + `_Development` + `_Diagrams` (was: `_Development` only); test now asserts the broader contract.
- **`trainability_check.py` consolidated to `vco_lib.paths.launcher_db_path()`** (`scripts/trainability_check.py:55-71`): hand-rolled `Path.home() / ".vct" / "launcher.db"` reconstruction migrated to the canonical resolver. Defensive `Path.home()` fallback inside `except ImportError` for the partial-install case. Caught by `tests/test_vct_root_dir_consolidation.py` — a real code-quality drift, not just a test issue.
- **Hub `find_hub_binary` test isolation** (`launcher/src-tauri/src/hub_launcher.rs`): env-var gate `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1` skips steps 4+5 (current_exe sibling + grandparent walk) when set. Production never sets the var; the 3 hub_launcher tests set it in their `with_env` block so `target/debug/vct-hub` left behind by sibling cargo invocations doesn't pollute the "no hub anywhere" probe. Pre-fix: deterministic test failures when run inside `cargo test --workspace`.

### Retrospective notes (no behaviour change)

- **macOS install architecture v2 design + per-track ownership table** (`docs/INSTALL_ARCHITECTURE_v2.md`): the design doc Track G shipped (1655 lines) + the §6 per-track ownership table that drove the 9 implementation tracks without file-scope collisions. Phase 2 integration shipped 11 merges + 7 splices with all conflicts resolved cleanly modulo one duplicate-symbol fix in `runtime.rs` (Track G3's verbatim copy of Track C's `augment_path_for_graphical_launch`, resolved by deletion).
- **`feedback_cargo_build_after_rust_merge.md` memory rule** added: after any merge that touches multiple Rust files, run `cargo build --workspace` before assuming the tree is clean. Git's `ort` merge strategy doesn't detect semantic duplicate-symbol collisions on near-identical (whitespace-only-diff) function bodies.
- **Phase 3 audit dispatch playbook (5 parallel READ-ONLY Opus agents)** standardised as the pre-tag gate: A gap-hunt, B regression-hunt, C cross-cutting wiring, D user-journey end-to-end, E dedup correctness. All 5 returned SHIP-READY for v0.2.53.
- **Pre-existing failure investigator caught 2 P1 production bugs that v0.2.52 silently shipped** — the V52-M hook exec-bit (POSIX) + UTF-8 BOM (Windows). Reinforces the "don't skip tests to cover real bugs" discipline rule.

## [0.2.52] - 2026-06-09

A **comprehensive cycle** following v0.2.51's public-repo cleanup. Major themes:

1. **Codegraph indexer recovery** (V52-O family): full parser-quality sweep across Python/Rust/JS/Java/C#/Go + ignore-set fixes + UUID5 cross-root dedup + file_path attachment + per-language method scoping. Empirically validated: re-walk shows 19,553 functions indexed (was 5,365 with 0 Python; now 8,174 Python + 4,687 Rust).
2. **RL telemetry chain closure** (V52-J/N/AA/AB/AC/AJ): canonical emit pipeline with project_id + session_id propagation + per-project RL container port resolution + embedding-resolution chain (env → launcher.db → default).
3. **Container lifecycle defense** (V52-D family): defensive runtime sanitizer + container_name drift fix + legacy reaper + 3rd-party manifest validation.
4. **Per-project install/update parity** (V52-AF): apply_post_bundle_steps refactor — create + update now run the same post-bundle phases.
5. **User-state architecture** (V52-C): KG nodes moved to templates/knowledge/ + materialization via bundle path; user customizations survive bundle updates.
6. **Conflict-resume UX** (V52-A/B): Keep local + Accept upstream one-click buttons replacing the fragile two-step resolve-manually flow.
7. **Global RL toggle** (V52-AD): cross-project toggle with project_id IS NULL sentinel + GUI surface + install.py seeds default-OFF + auto-enable boot probe.
8. **Schema version tracking** (V52-AG): launcher.db artifact_schema_versions registry + Python/Rust parity payload.
9. **Subagent-aware hooks** (V52-L.2): PreToolUse/PostToolUse hooks parse agent_id/agent_type/session_id + new SubagentStart KG-inject + SubagentStop reconciler.
10. **Pre/post bash + edit outcome hooks** (V52-M): richer RL training pair signals.

### Added

- **V52-A/B — Conflict-resume modal one-click buttons** (`launcher/src/lib/components/OrchestratorUpdateConflictModal.svelte` + `launcher/src-tauri/src/commands/installer.rs`): "Keep local" + "Accept upstream" Tauri commands (`keep_local_and_continue_update`, `accept_upstream_and_continue_update`) replace the fragile two-step Resolve-Manually + Continue-Update flow from v0.2.51. Both buttons run `git checkout --ours/--theirs` + commit + delegate to the v0.2.51 `resume_orchestrator_update` (install.py + binary refresh + auto-restart). Smart default: when ALL conflicted files are under `knowledge/`, the modal highlights "Keep local" with a recommended-badge visual cue. The legacy "Resolve manually (close this dialog)" button is removed; Escape/X/backdrop click now route through a two-step confirmation that maps to Abort per V52-A. 14 regression tests pin the contract.
- **V52-AD — Global RL reranker toggle** (`launcher/src-tauri/vct-launcher-core/src/db/migrations/034_module_settings_nullable_project.sql` + `settings.rs` + `vct-hub/src/config_api.rs` + `GlobalModuleTogglesPanel.svelte` + `routes/preferences/modules/+page.svelte`): cross-project toggle using `project_id IS NULL` sentinel row in `module_settings` (mirrors v0.2.27 module_installs pattern). New hub resolver `module_effective_enabled` cascades per-project setting → global default → enabled-by-default. 4 new Tauri commands: `module_global_enabled`, `set_module_global_enabled`, `module_effective_enabled`, `rl_auto_enable_probe`. GUI surface in Preferences → Modules (system-wide tab). install.py seeds the RL reranker as default-OFF on fresh installs via `_seed_rl_reranker_default_disabled` until 500+ retrieval events accumulate; a background launcher probe auto-enables once the threshold is met.
- **V52-AE — Defensive parallel-agent worktree-isolation helper** (`vco_lib/agent_dispatch_helper.py`): produces prompt boilerplate that every parallel subagent dispatch should prepend to the agent's task prompt. The boilerplate makes the agent (1) probe its own CWD + `git worktree list`, (2) assert it's NOT in the parent's primary checkout, (3) verify the active branch / base SHA / worktree-membership, and (4) refuse to commit + create its own fallback worktree if any assertion fails. Companion `render_pre_dispatch_assertion` / `render_post_dispatch_audit` helpers print sanity blocks for the dispatcher itself. Background: the launcher harness's `isolation: worktree` directive is implemented inside the closed-source Claude Code binary; the orchestrator cannot fix it directly. The defensive helper is the upstream-independent path.
- **V52-AF — apply_post_bundle_steps helper** (`launcher/src-tauri/src/commands/projects_v2.rs`): extracted from inline `create_project_v2` lines 507-711 into a private async helper called from both `create_project_v2` and `update_project_v2`. Closes the SHIP-BLOCKER per-project install/update parity gap (audit a2e4f94): per-project updates now run the same post-bundle phases (env refresh, retry reports, deferral emission, prune-stale rows) as per-project creates. `is_initial_create` discriminator: `false` on create (no rows possible), `true` on update (reap deleted-file rows).
- **V52-AG — Schema version tracking FOUNDATION (L1+L2 only)** (`launcher/src-tauri/vct-launcher-core/src/db/migrations/033_artifact_schema_versions.sql` + `vco_lib/schema_versions.py` + `vco_lib/schema_versions.json` + `vco_lib/artifact_version_registry.py`): launcher.db registry tracking schema versions per (project_id, artifact_type, artifact_name) tuple. 17 canonical version constants across 5 layers (KG/Codegraph/Bundle/LauncherDB/Diagrams). Python/Rust parity payload generated by `scripts/regen_schema_versions_json.py --check` (CI gate). DERIVED vs USER_CURATED classification routes drop+recreate (derived) vs upgrade-in-place (user_curated). New `ArtifactVersionStatus` enum with 5 transitions: NEVER_MATERIALIZED / UP_TO_DATE / RECREATE_NEEDED / UPGRADE_IN_PLACE_NEEDED / REFUSE_DOWNGRADE. **L3 consumer wiring is deferred to v0.2.53** by deliberate scope decision — landing the registry as scaffolding without wiring the readers/writers at collection-create / bundle-update sites keeps L1+L2 reviewable in isolation; L3 lands as a self-contained follow-up with its own test surface. See `launcher/src-tauri/src/commands/projects_v2.rs:583` for the V52-AG L3 TODO marker.
- **V52-AJ — Embedding resolution chain** (`vco_lib/launcher_db_reader.py` (NEW) + `vco_lib/embedding_service.py` + `install.py`): closes the disconnect that blocked Windows + CPU-only users (Fabio's msg 267 root cause). 3-layer fix: (1) install.py threads `ACTIVE_EMBEDDING` + `EMBEDDING_MODEL` into the `sync_knowledge_graph.py` subprocess env via `_subprocess_env_with_embedding`; (2) `EmbeddingService.for_project()` reads `launcher.db app_state[embedding.active_profile]` as fallback when env unset (single canonical chain: env → app_state → "qwen3" default); (3) CLAUDE.md template documents that `.claude/env` is for hook subprocesses only, NOT install.py. 18 regression tests cover all paths.
- **V52-C — KG nodes as user-curated state** (114 file moves `knowledge/` → `templates/knowledge/` + `vco_lib/project_init.py::_enumerate_bundle_files` + `orchestrator-managed-paths.txt` + `launcher/src-tauri/src/commands/installer.rs`): shipped KG nodes now live in `templates/knowledge/` and materialize per-project via the bundle path. User customizations are preserved across orchestrator updates via the V47-A manifest-hash-compare pattern — same shape as the v0.2.12 CLAUDE.md removal. The v0.2.51 modify-vs-delete merge conflict at `apply_conflict_strategy` cannot recur: that function never touches `<project>/knowledge/` anymore. 15 new tests + 191 Rust installer tests pin the contract.
- **V52-D.3 — Manifest sanitizer** (`vco_lib/manifest_validation.py` (NEW) + `launcher/src-tauri/src/commands/module_manifest_extract.rs`): defense against malformed 3rd-party module manifests. Python sanitizer with 3 validation layers (JSON schema, runtime semantics parity with V52-D.1, install.scope coherence) + CLI subcommand `python -m vco_lib.manifest_validation <path>`. Rust install-time hook calls the sanitizer between manifest-parse and atomic-commit (step 5.5); rejection preserves the .tmp file for forensics with `manifest_validation_failed:` prefix. Test bypass: `VCT_MANIFEST_SANITIZER_BYPASS=1`. 42 Python tests + 2 Rust integration tests.
- **V52-F — Per-module update GUI** (`launcher/src-tauri/src/commands/module_updates.rs` + `launcher/src/lib/api/module_updates.ts` + `launcher/src/lib/components/ModuleCatalog.svelte`): 4 new Tauri commands (`check_module_updates_available`, `update_module_to_latest`, `get/set_module_update_auto_check_enabled`) backed by the cached L0 catalog (15-min TTL via `paid_module_releases`). Background 24h poll emits `vct-module-updates-available` Tauri event. Pink-tinted "Update available: vX.Y.Z" badge on each module card; click triggers the existing atomic-swap update path with UPDATE_DEFERRED emission on partial failure. 22 Rust + 1 TS test cover the contract.
- **V52-L.2 — Subagent-aware hooks** (`templates/hooks/pre-tool-use.{sh,ps1}` + `post-tool-security.{sh,ps1}` + `post-file-edit.{sh,ps1}` + `subagent-start-kg-inject.{sh,ps1}` (NEW) + `subagent-stop-reconcile.{sh,ps1}` (NEW)): PreToolUse + PostToolUse hooks now parse `agent_id` / `agent_type` / `session_id` from the JSON payload and include them in every TOUCAN entry + credential alert + file-edit telemetry row. Subagent activity is now distinguishable from parent activity in retrieval-quality analysis. New SubagentStart hook runs `rl_kg_search.py` against the launch prompt for KG context injection (5s timeout per V52-Y). New SubagentStop hook logs `agent_id` / `transcript_path` / `stop_reason` to `.claude/logs/subagent-reconciliation.jsonl` for forensics. 12 tests + 254 hook-suite regression pass.
- **V52-M — Pre/post bash + edit outcome hooks** (`templates/hooks/pre-bash-context-inject.{sh,ps1}` (NEW) + `post-bash-context-record.{sh,ps1}` (NEW) + `post-edit-outcome.{sh,ps1}` (NEW) + `claude_mcp_servers/rl_client/outcome_emit.py` (NEW) + `launcher/src-tauri/vct-hub/src/rl_events_api.rs`): PreToolUse(Bash) hook fires when command length > 500 chars (env override `VCT_BASH_KG_THRESHOLD_CHARS`), mints `pre_bash_<uuid>` task_id, writes state file, runs `rl_kg_search.py`, injects results as additionalContext. PostToolUse(Bash) re-derives `cmd_hash` from stdin, reads paired state file, emits `bash_outcome` event. PostToolUse(Edit|Write) emits `edit_outcome` event with diff_size + file_existed_before. New `emit_outcome_event()` helper mirrors `RLTelemetryWriter._wrap_for_hub` shape; hub `allowed_event_types` extended to `["retrieval", "citation", "bash_outcome", "edit_outcome", "pre_bash"]`. 25 tests + 12 subtests.
- **V52-S — Trainability threshold constants + check script**: lifts the four corpus-health thresholds (citation pair rate 30%, n_emb presence 95%, query_emb presence 99%, cohort uniformity 95%) from A2's audit prose into `vco_lib/rl_trainability_thresholds.py` as named module-level constants. New `scripts/trainability_check.py` reads launcher.db's `rl_events`, computes the four metrics, and emits a per-metric PASS/FAIL plus an overall TRAINABLE / NOT_TRAINABLE_AS_IS verdict (supports `--json` for machine consumption). 6 tests.
- **V52-T — `tests/test_v52_i_post_deploy_smoke.py`**: post-deploy canary that probes `launcher.db rl_events` for `partial_fan_out_schema_missing` failure-mode events written AFTER the v0.2.52 tag time. Zero rows expected within 48h post-tag. Auto-skips until the operator fills in `V0252_TAG_TIME_MS` per the post-deploy runbook.

### Changed

- **V52-D — Container_name drift fix + last_error self-heal on resume** (`launcher/src-tauri/src/commands/module_service.rs` + `vct-hub/src/module_supervisor.rs`): on `start_container_for_module` success, if the resolved container name differs from launcher.db's stored `container_name`, persist the resolved name. Stops the probe→miss→respawn loop (`vct-rl-reranker-manual` stale DB ≠ `vct-rl-reranker-rs-slug` resolved). Closes V52-X. Also clears `module_installs.last_error` on successful container start — closes V52-W.
- **V52-G — Supabase telemetry_events migration idempotency** (`launcher/supabase/migrations/20260516_telemetry_events.sql`): rewritten as fully-idempotent reconciliation script. All columns guarded by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (notably `server_received_at`); all indexes use `CREATE INDEX IF NOT EXISTS`. Zero destructive statements. Preamble documents V52-G + the drift cause so future maintainers don't strip the redundant-looking ALTER block.
- **V52-J — Canonical RL telemetry emit pipeline** (`claude_mcp_servers/rl_client/telemetry_emit.py` (NEW) + `claude_mcp_servers/rl_client/search_pipeline.py` (NEW) + `claude_mcp_servers/weaviate_mcp/server.py` refactor): every KG-search entry point (MCP hybrid_search, MCP semantic_graph_search, CLI rl_kg_search.py, CLI search_knowledge.py, PreToolUse hooks) now routes through a single canonical `rerank_and_emit()` chokepoint. New `resolve_session_id()` 3-layer chain (caller arg → `VCT_SESSION_ID` env → `CLAUDE_SESSION_ID` env). Closes the 99.6% `session_id=""` + 100% `project_id=NULL` problem from the pre-v0.2.52 corpus (also closes V52-H, which was the same underlying disconnect).
- **V52-L.2 — `subagent-stop-reconcile.sh` is logging-only by default** (deliberate scope choice — V52-L.1 expanded reconciler is a separate landing): the SubagentStop hook writes a JSONL row with `session_id` / `agent_id` / `agent_type` / `transcript_path` / `stop_reason` for forensics. Best-effort + always exits 0.
- **V52-N — Citation monitor refactor**: tool-agnostic + sentinel + token threshold. Replaces pre-v0.2.52 character-counting + Edit/Write-only logic with a token-aware monitor that accumulates ANY tool input + emits citation event on threshold OR compaction OR safety-valve timeout.
- **V52-Y — SubagentStart hook timeout 2s → 5s**: bumps `templates/settings.json.{linux,windows}.template` SubagentStart hook timeout. Cold path (Python venv warmup + Weaviate connect) sometimes exceeded 2s; 5s comfortably covers the observed 1.5-2.5s typical cold latency.

### Fixed

- **V52-AH — Windows binary lock during orchestrator update** (`launcher/src-tauri/vct-updater/` (NEW crate) + `launcher/src-tauri/src/commands/update_handoff.rs` (NEW) + `installer.rs::stage_locked_binaries_for_handoff` + `install.py::_try_invoke_windows_stage1_updater`): Velopack/Squirrel-style stage1 updater pattern fixes Fabio's bug 1 (v0.2.50→v0.2.51 update loop where `dist/windows-x64/vct-launcher.exe` was OS-locked by the running launcher process, so `git pull` silently failed to overwrite the .exe but DID overwrite `.metadata.json` → "newer binary on disk, Restart" banner relaunched the SAME stale binary in a loop). The new `vct-updater.exe` is a tiny statically-linked Rust binary that the launcher spawns DETACHED before exiting; it polls the parent PID via `OpenProcess` + `WaitForSingleObject`, then performs `MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)` for each staged `<target>.new` sibling, and spawns the new launcher. Hand-off contract via `~/.vct/update.lock.json` (parent_pid + swaps + relaunch + ISO 8601 started_at). 4-layer fix: (1) new `vct-updater` Rust crate (Win-only meaningful, POSIX no-op); (2) `prepare_windows_update_handoff` Tauri command writes lock + spawns updater; (3) wired into `update_orchestrator` post-WaitForBinaryRefresh, with graceful fall-through to legacy `restart_launcher` on any failure; (4) boot-time recovery probe (`poll_update_lock_on_boot`) emits `vct-update-recovered` / `vct-update-failed` Tauri events on next launcher start. install.py CLI parity via `_try_invoke_windows_stage1_updater` (called from the existing ERROR_SHARING_VIOLATION recovery branch). Soft-fail throughout: worst case is identical UX to v0.2.51 (the legacy `launcher_binary_swap_failed_locked` deferral). 10 Python tests + 4 Rust unit tests (in vct-updater) + 8 Rust unit tests (in update_handoff). POSIX hosts unaffected (rename pattern handles binary swap there).
- **V52-AI — MCP fork-bomb during update on Windows** (`vco_lib/update_gate.py` (NEW) + `claude_mcp_servers/_lib/update_gate.py` (NEW) + `launcher/src-tauri/src/commands/update_gate.rs` (NEW)): closes Fabio's bug 2 (~97 python + ~77 node processes accumulating during update — MCP servers respawning in a loop against the updating launcher binary). 3-layer protection: (1) pre-update kill sweep terminates running MCP-shaped processes (strict pattern filter on `claude_mcp_servers/`, `@modelcontextprotocol/`, `@upstash/context7` — user's other Python/Node processes survive); (2) lockfile at `<vct_root>/.update-in-progress.json` is read at startup by every shipped MCP server (weaviate_mcp + search_mcp + code_embedding_service) — when active they exit cleanly with exit code 75, so every Claude Code respawn dies immediately and the loop can't form; (3) boot-time stale-cleanup removes lockfiles whose 15-min deadline passed (handles the case where the launcher crashed mid-update). Rust `UpdateInProgressGuard` is RAII with `Drop` impl that deletes the lockfile on every exit path (success, `?`-bail, panic). Explicit `disarm_and_cleanup()` before `restart_launcher` guarantees the lockfile is gone BEFORE the binary swap, so the freshly-booted launcher's MCPs don't see a stale lock. 36 Python tests + 21 Rust tests.
- **Known Issue 6 (cosmetic seed warnings)** — three install-banner noise sources cleaned up so first-install no longer scares users with stack-trace-shaped warnings: (a) targeted `warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)` BEFORE `import weaviate` in `claude_mcp_servers/weaviate_mcp/server.py`, `templates/scripts/sync_knowledge_graph.py`, `templates/scripts/analyze_code_graph.py`, and `templates/scripts/detect_duplicates.py` — silences the `authlib.jose module is deprecated` warning that `weaviate-client`'s transitive `authlib` dep emits at module import; (b) abstraction-level tags added to 31 bundled seed nodes (`#high-level-plan` / `#mid-level-architecture` / `#low-level-implementation`), eliminating the per-node "No abstraction level tag" warning; (c) 10 camelCase tag renames (`LoRA` → `lora`, `QLoRA` → `qlora`, `MoE` → `moe`, `NeSy` → `nesy`, `DeepProbLog` → `deepproblog`, `OAuth` → `oauth`, `RoPE` → `rope`, `IaC` → `iac`, `ComfyUI` → `comfyui`, `OpenAPI` → `openapi`), eliminating the "uses camelCase" warning. Live-seed verification: pre-fix 39 warnings; post-fix 0.
- **V52-L.1 — SubagentStop full reconciler** (`templates/hooks/_lib/snapshot.{sh,ps1}` + `_lib/credscan.{sh,ps1}` (NEW) + `subagent-start-kg-inject.{sh,ps1}` extended + `subagent-stop-reconcile.{sh,ps1}` rewritten from logging-only to full reconciler): SubagentStart takes a filesystem snapshot under `.claude/state/subagent-snapshot-<agent_id>.json`; SubagentStop diffs against the snapshot + runs `kg-sync --incremental` per modified `knowledge/**/*.md` file, queues modified code files to `.claude/state/code-graph-queue.jsonl`, runs `credscan.sh` over the file-modification list, and increments `~/.claude/metrics/kg_update_tokens.jsonl` for the parent session's nudge counter. Soft-fail throughout. 14 Python tests + 3 PowerShell-skipped on non-Windows.
- **V52-AA — RL container port propagation gap** (`launcher/src-tauri/vct-hub/src/config_api.rs` + `vco_lib/project_config.py` + `claude_mcp_servers/weaviate_mcp/server.py`): exposes `rl_server_port: Option<u16>` through the hub-resolved `ProjectConfig` channel. `_get_rl_client` reads it as fallback when `RL_SERVER_URL` / `RL_SERVER_PORT` env unset (env still wins). Pre-fix: env surfaces intentionally excluded `RL_SERVER_PORT` per Claude Code's env-precedence H.1 contract → `_resolve_base_url()` always returned `None` → every `RLClient` lived in disabled mode → zero online RL training, ever. Now: the hub-resolved port reaches MCP subprocesses + RLClient gets a working URL.
- **V52-AB — `rl_update_v3` wiring confirmed** (regression tests added): `_rl_answer_monitor` already calls `client.rl_update_v3()` at `server.py:3993`. Backlog spec was outdated; 4 regression tests + 1 source-grep static guard pin the wiring contract against future refactors.
- **V52-AC — Telemetry writer disabled-mode soft-fail audit**: writer has exactly three skip branches, all intentional + logged (`RL_LOCAL_LOGGING_DISABLED` env, hub-down exception, import-failure). Fully independent of RL container state. 11 regression tests + static `except: pass` forbid-pattern audit.
- **V52-AH — Windows binary lock during orchestrator update** (`launcher/src-tauri/vct-updater/` (NEW crate) + `launcher/src-tauri/src/commands/update_handoff.rs` (NEW) + `installer.rs::stage_locked_binaries_for_handoff` + `install.py::_try_invoke_windows_stage1_updater`): Velopack/Squirrel-style stage1 updater pattern that fixes Fabio's bug 1 (v0.2.50→v0.2.51 update loop where `dist/windows-x64/vct-launcher.exe` was OS-locked by the running launcher process, so `git pull` silently failed to overwrite the .exe but DID overwrite `.metadata.json` → "newer binary on disk, Restart" banner relaunched the SAME stale binary in a loop). The new `vct-updater.exe` is a tiny statically-linked Rust binary that the launcher spawns DETACHED before exiting; it polls the parent PID via `OpenProcess` + `WaitForSingleObject`, then performs `MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)` for each staged `<target>.new` sibling, and spawns the new launcher. Hand-off contract via `~/.vct/update.lock.json` (parent_pid + swaps + relaunch + ISO 8601 started_at). 4-layer fix: (1) new vct-updater Rust crate (Win-only meaningful, POSIX no-op); (2) `prepare_windows_update_handoff` Tauri command writes lock + spawns updater; (3) wired into `update_orchestrator` post-WaitForBinaryRefresh, with graceful fall-through to legacy `restart_launcher` on any failure; (4) boot-time recovery probe (`poll_update_lock_on_boot`) emits `vct-update-recovered` / `vct-update-failed` Tauri events on next launcher start. install.py CLI parity via `_try_invoke_windows_stage1_updater` (called from the existing ERROR_SHARING_VIOLATION recovery branch). Soft-fail throughout: worst case is identical UX to v0.2.51 (the legacy `launcher_binary_swap_failed_locked` deferral). 10 Python tests + 4 Rust unit tests (in vct-updater) + 8 Rust unit tests (in update_handoff). POSIX hosts unaffected (rename pattern handles binary swap there).
- **V52-D.1 — Defensive runtime sanitizer** (`launcher/src-tauri/vct-launcher-core/src/services/container_runtime.rs`): `is_runtime_pathological(command, args, module_id)` detects pre-v0.2.49 Bug E manifests (`runtime.command ∈ {podman, docker}`, shell-without-`-c`, unsubstituted `{module_image}` in args). When detected, `build_podman_run_args` drops the CMD override so the image ENTRYPOINT runs unmolested. 10 unit tests.
- **V52-D.2 — Legacy container reaper** (`launcher/src-tauri/vct-launcher-core/src/services/container_runtime.rs::reap_pathological_containers` + `module_service.rs` + `module_supervisor.rs`): `ReaperVerdict` enum (`BrokenCmd` / `Orphan` / `StaleImage` / `Healthy`) + reaper pre-pass wired into production `resume_containers_on_startup`. `name_filter`: only containers whose names match a known module-id prefix are considered — Weaviate / Ollama / user containers never touched. Subsumes V52-E (bare `vct-rl-reranker` orphan). 12 unit tests including V52-E orphan-classification integration.
- **V52-I — `valid_until` gap permanently closed**: install.py + bundle update flows now always materialize the `valid_until` property in the 3 collections that previously lacked it (`VibeCodedOrchestrator_KnowledgeGraph`, `VibeCodedOrchestrator_Diagrams`, `VCODev_Diagrams`). Pre-fix: MCP's universal `_stale_filter` filtered on `valid_until` → schema-missing → `partial_fan_out_schema_missing` failure-mode emitted on every retrieval. Schema-aware stale filter eliminates the false-positive.
- **V52-O.1 — `.svelte-kit` Rollup ignore-set**: codegraph indexer no longer walks `.svelte-kit/` Rollup chunks. Pre-fix: 389 Rollup-generated noise modules dominated CodeModule. Post-fix: 1,388 real modules (was 394).
- **V52-O.2 — Operator collection reset helper** (`scripts/v0252_codegraph_reset.sh`): drops the 5 polluted `VibeCodedOrchestrator_Code*` collections + the 15 legacy `Vco_v0243_*` snapshot collections + re-walks with V52-O.1/O.3/O.4 fixes applied. Uses `connect_to_custom` with `grpc_port=50052` (NOT the weaviate-python-client default 50051) — see `knowledge/tools/weaviate-grpc-port-50052-gotcha.md`. Operator-only: requires `--yes` or explicit confirmation.
- **V52-O.3 — UUID5 cross-root dedup**: code-graph rows now use `f"{project}::{project_source}::{file_path_rel}::{full_name}"` as the UUID5 input, so dual-walked roots (VCO_dev primary + vibecoded-orchestrator extra-path) produce distinct UUIDs. Empty `project_source` (single-root installs) preserves byte-identical UUIDs with the v0.2.16 target. Zero 422 collisions during the post-fix rewalk (was "many").
- **V52-O.4 — `file_path` property on CodeFunction + CodeClass**: query results now surface file paths alongside symbol names; the `query_code_structure callers` query can tell you which FILE the caller lives in.
- **V52-O.6 — Codegraph CLAUDE.md asymmetry docs**: `templates/ORCHESTRATOR-CLAUDE.md.template` now explicitly documents the KG (shared collection auto-merged on every read) vs codegraph (no shared default; cross-project via explicit `codegraph_access` grants) asymmetry. 8-row comparison table + "Why the asymmetry?" rationale + closing "don't add SHARED_CODE_GRAPH_COLLECTION" warning.
- **V52-O.7 — Drop `--prune-stale --language=$LANG` from incremental hook** (`templates/hooks/code-graph-incremental.{sh,ps1}`): the bash incremental hook called the analyzer with `--prune-stale --language=$LANG`, which destroyed all rows in OTHER languages on every Python-file edit. Pre-fix: 0 Python rows indexed (despite Python being the parser's best). Post-fix: 8,174 Python rows.
- **V52-O.11.E — Brace-balanced body extraction (18 sites)** (`templates/scripts/analyze_code_graph.py::_extract_balanced_block`): replaces fixed `start_line + N` heuristic across 11 function-body + 7 class-body sites. Audit reproduction: `is_blocklisted_agent_file` real end 281, was stored as 315 (34-line bleed into `populate_agents`). 18 regression tests.
- **V52-O.11.F + F.2 — Per-language method scoping** (`_rust_methods_for_struct` + `_go_methods_for_struct` + `_js_methods_for_class` + `_java_methods_for_class` + `_csharp_methods_for_class`): replaces `method_pattern.finditer(content_clean)` over-whole-file in 5 language parsers' per-class loop. Pre-fix: 50-fn file with 3 structs produced 150 incorrect method attributions. Post-fix: each struct/class gets only its actual method declarations. 70 tests across the family.
- **V52-O.11.G — Rust `fn`-prefix regex expansion**: catches `pub(crate)`, `pub(super)`, `pub(in path)`, `unsafe`, `const`, `extern "C"`, `default`. Pre-fix the regex required either no prefix or `pub`, silently dropping modifier-prefixed functions.
- **V52-O.11.H — Lua parser passes `language="lua"` to embedders**: 2 LoC fix — was passing `language="javascript"` for Lua content (wrong embedding-service tokenizer hint).
- **V52-O.11.I — `_strip_string_literals` helper**: NEW module-scope helper strips single/double/backtick string-literal content while preserving line numbers. Protects parsers that scan over `content_clean` from false-positives (`let s = "fn foo()";` no longer produces a phantom function row).
- **V52-O.11.J — Skip Rust `#[cfg(test)]` functions**: `_is_rust_test_fn` helper scans backwards from fn position checking for `#[cfg(test)]` / `#[test]` / `#[cfg(any/all(test, ...))]` attributes. Test functions excluded from production code index.
- **V52-O.11.K + V52-U — `_ensure_project_source_property` migration removed**: dead-code removal (40 LoC + call site). After V52-O.2 rewalk (every row has `project_source` from creation), the migration is moot. Closes V52-U.
- **V52-O.11.L — `scripts/v0252_codegraph_reset.sh` extended**: drops 15 legacy `Vco_v0243_*` snapshot collections alongside the canonical `VibeCodedOrchestrator_Code*` collections. Includes the gRPC port 50052 fix for the weaviate-client `connect_to_custom` call.
- **V52-Z — Extra-path runs upsert `code_graph_builds`**: the launcher's "last successful build" UI reads `code_graph_builds.finished_at`, but the two extra-path code paths (`sync_project_codegraph_extra_path` and `reindex_project_codegraph_after_extras_change`) updated `project_codegraph_extra_paths.last_indexed_at` and skipped `code_graph_builds` entirely. Both completion sites now upsert `code_graph_builds` with the analyzer's reported counters. Best-effort: row-write failures log but don't fail the user-visible sync.
- **V52-AI — MCP fork-bomb during update on Windows** (`launcher/src-tauri/src/commands/update_gate.rs` + `vco_lib/update_gate.py` + `claude_mcp_servers/_lib/update_gate.py` + `install.py` + `templates/hooks/ensure-containers.{sh,ps1}`): lockfile-based pre-update gate at `<vct_root>/.update-in-progress.json` that the launcher writes BEFORE git pull and deletes AFTER install.py + binary refresh succeed. The MCP servers (weaviate-kg, search, code-embedding) check the lockfile at startup and exit cleanly with exit code 75 when active. Pre-fix the user reported ~97 python processes (claude_mcp_servers + vct-coordination self-spawning) + ~77 node processes (`npx @upstash/context7` + `@modelcontextprotocol/*`) consuming 100% CPU for hours during update, requiring manual taskkill — root cause was Windows mandatory file locks failing every MCP-spawn-against-an-updating-binary, Claude Code retried, respawn loop formed. The gate breaks the loop by making every respawn during the update window exit immediately before any side effects accumulate. Includes a pre-update kill sweep (POSIX `sysinfo`/`SIGTERM`; Windows `sysinfo`/`taskkill`) with strict pattern matching (`claude_mcp_servers/` / `@modelcontextprotocol/` / `@upstash/context7`) so the user's unrelated Python/Node processes survive untouched. The launcher's RAII `UpdateInProgressGuard` cleans up on every exit path (success / `?`-bail / panic); a `cleanup_if_stale` boot-time self-heal removes the lockfile if the previous update crashed mid-way and exceeded its 15-min deadline. 21 Rust + 36 Python unit/integration tests cover the lockfile lifecycle, stale-detection, RAII cleanup, kill-sweep pattern matching (positive + adversarial cases protecting user's other processes), MCP-shim parity, and hook integration. Cross-language schema parity test prevents Rust ↔ Python drift.

### Retrospective notes (no behaviour change)

- **V52-P — Cutover gap 2026-06-04 → 2026-06-05**: ~8.5h with zero RL telemetry events landing in either backend during the v0.2.47 launcher.db cutover boundary (JSONL last write 2026-06-04 16:13 UTC; launcher.db first write 2026-06-05 00:51 UTC). One-off transition window; future offline-trainer code should expect a hole in the timeline. No fix; documenting for forensic clarity.
- **V52-Q + V52-R — closed by V52-J Edit 3 (already shipped in commit `ab706bd`)**: the `score_cosine` per-node field (V52-Q) is now attached at the Weaviate fetch sites in both `hybrid_search` and `semantic_graph_search` via `r["score_cosine"] = 1.0 - r["distance"]` — distinct from the fused hybrid `score` which can exceed 1.0. The `emb` per-node field (V52-R) is attached via `_extract_obj_vector(obj, query_target)` at the same fan-out sites. Closed.
- **V52-V — closed by V52-O.4**: the `query_code_structure callers` query now surfaces file paths via the V52-O.4 `file_path` property attachment. Closed.
- **Known Issue 6 (cosmetic seed warnings)** — three install-banner noise sources cleaned up so first-install no longer scares users with stack-trace-shaped warnings: (a) targeted `warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)` BEFORE `import weaviate` in `claude_mcp_servers/weaviate_mcp/server.py`, `templates/scripts/sync_knowledge_graph.py`, `templates/scripts/analyze_code_graph.py`, and `templates/scripts/detect_duplicates.py` — silences the `authlib.jose module is deprecated` warning that `weaviate-client`'s transitive `authlib` dep emits at module import; (b) abstraction-level tags added to 31 bundled seed nodes (`#high-level-plan` / `#mid-level-architecture` / `#low-level-implementation`), eliminating the per-node "No abstraction level tag" warning emitted by `sync_knowledge_graph.py:508`; (c) 10 camelCase tag renames (`LoRA` → `lora`, `QLoRA` → `qlora`, `MoE` → `moe`, `NeSy` → `nesy`, `DeepProbLog` → `deepproblog`, `OAuth` → `oauth`, `RoPE` → `rope`, `IaC` → `iac`, `ComfyUI` → `comfyui`, `OpenAPI` → `openapi`), eliminating the per-tag "uses camelCase" warning at line 491. Node titles (the products themselves) preserved; only YAML frontmatter tags renormalised. Validator-side logic untouched on purpose — user-authored content still gets the same nudge, only the bundled seed is now clean. Regression-pinned by `tests/test_no_authlib_deprecation_warning.py` (7 cases), `tests/test_kg_seed_nodes_have_abstraction_tags.py` (2 cases), and `tests/test_kg_seed_nodes_use_kebab_case_tags.py` (2 cases). Live-seed verification: pre-fix produced 39 warnings across the three KNOWN_ISSUES categories; post-fix produces 0.


## [0.2.51] - 2026-06-09

A **public-repo cleanup release** triggered by a critical UX regression discovered during v0.2.50 dogfooding (merge-conflict update flow had no resume path), plus a comprehensive sweep of leftover operational state and stale documentation. Heavy on subtraction: 99 files changed (+3511 / -7150 = net **-3639 LoC**). Subagent-parallel: 5 worktree-isolated Opus agents (Bug A + W1 KG + W2 docs + W3 UI + W4 installer) merged via 4 zero-conflict merges.

### Added

- **Bug A — `resume_orchestrator_update` Tauri command** + sentinel-driven detection of "merge-resolved-but-install-incomplete" state. When a `update_orchestrator` flow hits a merge conflict and the user resolves it manually, the GUI now persists the in-progress state to `.claude/state/orchestrator-update-resume-needed.json` + writes a `update_resume_required` entry to `UPDATE_DEFERRED.md`. A pulsing purple MenuBar badge ("Continue Update") becomes visible across sessions, and a "Continue Update" button is added to `OrchestratorUpdateConflictModal`. Backed by a Rust single-flight `tokio::Mutex` + 8 new unit tests + `templates/ORCHESTRATOR-CLAUDE.md.template` SESSION-START guidance so Claude surfaces the deferral to the user at session start.
- **Bug G — auto-install + auto-start prereqs** (`install.py`, `install.sh`, `install.ps1`, `first-install.*`): when missing, the first-install flow now offers to install `node`/`npm`/`npx`, `python3.11+`, and `podman` via the platform package manager (`apt`/`dnf`/`pacman`/`brew`/`winget`). Availability check is by binary, not by daemon state — a stopped Podman daemon is auto-started (`systemctl --user start podman.socket` / `podman machine start`) rather than failing the install. GPU drivers stay manual (correctly out of scope).
- **Restored `prompt-engineering-2026.md` as 3 split nodes** (<300 LoC each): `prompt-engineering-fundamentals.md`, `prompt-engineering-multi-agent.md`, `prompt-engineering-mcp-tool-design.md`. Refreshed model-version references to current Claude 4.7/4.6/4.5.
- **`launcher/static/logo-512.png`** — the 512×512 brand logo asset referenced by `RightSidebar.svelte` was never committed in v0.2.43 when the code reference was added. Now present (copied from `launcher/src-tauri/icons/icon.png`); the broken-image placeholder in the lower-right sidebar disappears.
- **KG node** `update-resume-required-bug-a-v0251.md` documenting the design.

### Changed

- **Bug B — modal-anchor consistency across all hand-rolled modals**: `OrchestratorUpdateDivergenceModal`, `OrchestratorUpdateConflictModal`, `OrchestratorUpdateProgressModal` now anchor flush to viewport-top (`align-items: flex-start` + `padding: 16px` + scrollable body) instead of vertically-centered (which clipped above the window-top on short viewports). The other 15 `*Modal.svelte` files delegate to `DialogRoot` + native `<dialog>` and were already correctly positioned.
- **Bug D — count drift**: stale `27 hooks` / `52 skills` / `45 agents` references corrected to verified `31 hooks` / `53 skills` / `45 agents` across `README.md`, `BOOTSTRAP.md`, `docs/features/INDEX.md`, `docs/features/03-agents-skills-hooks.md`, `docs/features/06-license-and-commercial.md`, `docs/GETTING_STARTED.md`, `templates/ORCHESTRATOR-CLAUDE.md.template`, KG `hook-discipline-vct-disable-hooks.md`.
- **Bug E — npx fallback for fnm/nvm setups**: `install.py`'s `shutil.which("npx")` probe now falls through to `dirname(realpath(which("npm")))/npx` when the direct lookup fails. Affects ~5% of users with hand-managed Node installations (fnm/nvm/asdf) where `npm` is symlinked but `npx` isn't. Empirically verified: returns the correct fnm path + `npx --version` succeeds on the reproducer.
- **KG collection name canonicalization**: stale `VibeCodedTools_KnowledgeGraph` → `VibeCodedOrchestrator_KnowledgeGraph` (with lowercase-c "Vibecoded" typo fix) in `docs/CONFIGURATION.md`, `docs/GETTING_STARTED.md`, `docs/TROUBLESHOOTING.md`, `docs/USER_GUIDE_TABS.md`, KG `shared-knowledge-graph.md`.
- **Port `11438` → `11440`** (code-embed service): documented port matches the actual default in 3 KG nodes (`orchestrator-hook-system.md`, `codesage-large-v2.md`) + cross-references.
- **`templates/CLAUDE.md.template` line 18**: the literal label `VCO_dev` (maintainer's private operational checkout) → `VibeCoded Orchestrator`. This template materializes into every user's `.claude/CLAUDE.md` at install, so every user installed since v0.2.43 had `VCO_dev` propagated into their project. Now fixed for new installs; existing installs self-correct on next `install.py --update`.
- **Public-scrub of `kg-summary-three-tier-pipeline.md` + `agentic-llm-workflows.md`**: VCO-specific narrative paragraphs stripped while preserving the generic AI/ML concept content. VCO_dev's private knowledge graph retains the full version.

### Fixed

- **Bug F — `install.py.lock` ResourceWarning**: lockfile handle now wrapped in `contextlib.contextmanager` + `atexit` registration. `python -W error::ResourceWarning install.py --update` succeeds cleanly.
- **`chunking-strategies-llm-rag.md`** — dropped the final "Claude Orchestrator Implementation" section referencing the nonexistent `orchestrator/context/embedding.py` and `EMBED_CHUNK_SIZE` constant.
- **`KNOWN_ISSUES.md`** — refreshed from v0.2.18-era pin to current v0.2.50+ state; resolved issues marked closed; still-relevant issues retained with current verification notes.
- **`docs/features/01-launcher.md`** — updated "Eight migrations as of v0.1.0" → "032 (v0.2.49)"; Machine ID binding description updated to current v0.2.36+ algorithm.
- **`docs/license/USER_FLOW.md`** — algorithm description synced to `docs/license/MACHINE_BINDING.md` (was contradicting it).
- **`docs/macos-install.md`** — wrong-org git URL corrected.

### Removed

#### File deletes (post-install artifact + maintainer-only sweep)
- `supabase/.temp/` (9 files) — Supabase CLI scratch state with project-ref / org_id / pooler URL. Now gitignored.
- `internal/` (6 files) — README, RELEASING, LAUNCHER_SUBTREE, demo_script, license/INTEGRATION, license/VARIANT_IDS. The directory's own README self-flagged it "Not user-facing. Excluded from release archives." — this release enforces that intent.
- `tools/install-vco-dev-pre-push-guard.{sh,ps1}` — VCO_dev private-fork pre-push guards (not user-facing helpers).
- `scripts/{add-silent-to-commands.py, apply-branch-protection-v0.2.42.sh, filter-repo-2026-05-06.sh}` — completed one-off operations.
- `docs/operations/supabase-ci-deploy.md` — maintainer-only deploy guide.
- 7 release-narrative / maintainer-only docs: `docs/codeql-triage-v0.2.42.md`, `docs/EXCALIDRAW_WAYLAND_TEST.md`, `docs/MAINTAINER_GUIDE.md`, `docs/MIGRATION-0.2.0.md`, `docs/v0.2.33-l0-deploy.md`, `.github/workflows/README.md`, `launcher/docs/SECURITY_PORTING_GUIDE.md` (the last one also leaked the dual-repo architecture).

#### KG node deletes (28 nodes off-scope or operational/release-narrative)
- 11 devdoc/release-narrative nodes (`config-projection-contract-2026-05-24`, `global-install-scope-v0249-stream-a`, `orchestrator-root-kg-collection-identity-2026-06-01`, `paid-module-install-update-foundation-2026-06-02`, `parallel-pr-coordination-gotchas-2026-05-10`, `supervisor-image-resolution-variant-gap-2026-06-04`, `install-pipeline-self-healing-v0213`, `mcp-simplification-v0211`, `cross-os-hook-portability`, plus 2 borderline)
- 15 founder/marketing nodes off-scope for an AGPL orchestrator (`build-vs-buy-decision-framework-2026`, `churn-taxonomy-classification`, `gdpr-eu-vat-lite`, `landing-page-conversion-audit`, `merchant-of-record-vs-stripe`, `north-star-metric-selection`, `saas-metrics-math-and-benchmarks`, `saas-trial-conversion-playbook`, `sales-funnel-stage-modeling`, `seo-2026-aio-eeat`, `social-platform-algorithms-2026`, `sow-statement-of-work-anatomy`, `solo-saas-pricing-tiers-2026`, `portfolio-triage-burning-watching`, `sliding-window-cohort-analysis`)
- 3 designer/brand nodes (`ai-image-generation-workflows-2026`, `ai-image-prompt-anatomy`, `color-theory-for-ui`)
- 1 cross-project pollution (`online-speed-envelope-learning` — sim-racing AI mod content unrelated to VCO)
- 1 deprecated upstream (`ollama-mcp-server`)

### Security / Privacy

- Scrubbed `pb992/VCT-Launcher` references (co-maintainer's private fork, not a public repo) from `BOOTSTRAP.md`, `docs/features/07-architecture.md`, `launcher/bundled_manifests/vct-hub-api.json`.
- Scrubbed personal product codenames (maintainer's other personal products) from `launcher/docs/SETUP.md` dev-mode activation examples; replaced with `DemoProduct1/2/3`.
- Scrubbed personal-narrative leakage in `CHANGELOG.md` (`instambul_map`, `SD15` → "an external user project" / "a long-lived legacy project"); preserved technical context.
- Scrubbed chat-role narrative ("RL chat", "main VCO chat") from 6 Rust inline comments + 1 TypeScript comment + 1 GHA workflow + 1 pytest test description; replaced with neutral architectural language ("v0.2.49 access-matrix follow-up", "module-author workflows", etc.).
- Scrubbed dated release-narrative paragraphs from `launcher/supabase/functions/rl-{artifact-url,latest-weights}/README.md` while keeping the architectural Vault-vs-Edge-Function-Secrets explainer.
- Scrubbed `VCO_dev` operational-checkout name from launcher docs + `templates/CLAUDE.md.template` (which propagates into user installs).

**Architectural identifiers KEPT** (verified public per release audit 2026-06-09): `ovpdtijpdchzlxbojhsg.supabase.co` (public licensing/RL/telemetry endpoint, hardcoded in 8+ source files), `ghcr.io/hotak92/vct-rl-reranker` (active GHCR image), `github.com/hotak92/vibecoded-orchestrator` (public repo URL), UUID `9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480` (test fixture license key in 5+ validation tests).

### Build / CI / Gates

- **`.gitignore`** extended with 18 new patterns covering categories above + operational state files (`UPDATE_DEFERRED.md`, `SELF-HANDOFF-*.md`, more `HANDOFF-*` variants, `.claude/context/{audits,plans,pre-compact-snapshot}/`). Prevents regression of any pattern this release deleted.
- All ship gates GREEN: `npm run check` (0 errors), `cargo check` (clean, only pre-existing dead-code warnings), `cargo test --lib` (**1378 passed / 0 failed**, +8 new Bug A tests), `pytest tests/` (**3661 passed / 59 skipped**), `python -m py_compile install.py`, `bash -n install.sh`, Gate 21 privacy check, all PASS.

## [0.2.50] - 2026-06-08

Recovery release after v0.2.49 was tagged from a feature branch instead of
`main`. v0.2.49's tag points at correct source but main was never advanced
to it, so the launcher's "Update orchestrator" flow (which uses `git pull`)
would have pulled v0.2.48 source + mislabeled binaries. v0.2.50 propagates
the full v0.2.49 source to main + fixes the release workflow bug that
caused the mislabel + scrubs private operational artifacts that leaked
between v0.2.46–v0.2.49.

### Recovery + scrub

- **Privacy scrub**: removed maintainer-local operational block from CLAUDE.md,
  deleted tracked handoff doc (tracked since v0.2.46), deleted maintainer-only
  licensing audit doc (force-added in v0.2.49). Mechanical scrub of ~40 files:
  user home paths, private codenames, internal hostnames, and
  `commercial_workflow/` references in comments / tests / KG nodes. No
  credentials were leaked at any point; all scrubbed content is paths +
  developer names + private codenames. **Old tags (v0.2.46–v0.2.49) retain
  the leakage as historical artifacts; we did not rewrite history.**

- **CLAUDE.md → template architecture**: root `CLAUDE.md` is now a thin
  reference. The full content lives in `templates/ORCHESTRATOR-CLAUDE.md.template`
  and is materialized by `install.py` at first-install + each `--update`
  with `{{ORCHESTRATOR_ROOT}}` substitution. Eliminates the source of
  v0.2.49's hardcoded paths.

### Release-workflow fix

- **`.github/workflows/release.yml::commit-dist-binaries` now reads VERSION
  from the tag ref** (`${GITHUB_REF#refs/tags/v}`), not from `main`'s
  `Cargo.toml`. Prevents the v0.2.49 issue where binaries were committed
  to main labeled "v0.2.48" because the release was tagged from a feature
  branch whose Cargo.toml said 0.2.49 but main's still said 0.2.48.

### Hardware portability

- **F1**: code-embed Dockerfile is now multi-arch. `Dockerfile` uses a
  CPU-only PyTorch base; `Dockerfile.cuda` opts in to CUDA. `install.py`
  picks the right one per detected GPU. Apple Silicon users no longer
  hit image-pull failure when enabling the code-embed service.
- **F2/F3**: dropped dead `model_router_claude` rows from
  `templates/hooks/verify-container-ports.{sh,ps1}` (last `_claude`-suffix
  leak from the maintainer-machine cleanup that v0.2.15 started).
- **F4/F5**: dropped `model_router` from systemd + Windows task descriptions.
- **F6**: `recover_zombie` in `ensure-containers.{sh,ps1}` now guards
  against running on non-Podman runtimes (prevents spurious `docker rm -f`
  + recreate on healthy containers).
- **F7**: dropped `code_embed` block from ROCm overlay (was unreachable
  due to install.py routing ROCm users to CPU profile).
- **Launcher embedding-dropdown ↔ MCP slot mapping**: tightened the UI to
  only offer values the MCP server knows about
  (`qwen3 | codesage | arctic | ollama | openai`).
- **`templates/scripts/detect-project.ps1`**: created Windows sibling (UTF-8
  BOM) for the bash `detect-project.sh` — native-Windows users no longer
  silently lose multi-codebase auto-detection.

### Pre-tag privacy gate + gitignore hardening

- New `scripts/check-pre-tag-privacy.sh` (Gate 21 in `pre-ship-check.sh`):
  fails on maintainer-local marker blocks, `commercial_workflow/` references,
  private machine hostnames, internal project paths,
  tracked `HANDOFF-*.md` files at repo root, and user home paths outside
  intentional sentinels.
- `.gitignore` patched with 23 new rules (root-level `HANDOFF-*.md`,
  `MEMORY.md`, `.claude/MEMORY.md`, `.claude/PROJECT_REGISTRY.md`,
  `.claude/env`, `.claude/mockups/`, `.claude/rules/`, `.claude/.vco-manifest.json`,
  `.claude/metrics/`, `.claude/settings.json.bak-*`, `.vct/`,
  `.vct-orchestrator-dev`, `.vscode/tasks.json`, `knowledge/{inbox,daily,_drafts}/`,
  ad-hoc scratch scripts at repo root (`/assemble_pngs.py`, `/decode_canvas_png.py`,
  `/decode_chunks.py`, `/default_b64_chunk*.txt`), and
  `claude_mcp_servers/compose.override.{yaml,yml}` + `*.bak-*` backups).
- `tools/install-vco-dev-pre-push-guard.{sh,ps1}` provides a re-installable
  pre-push hook for private operational VCO checkouts that refuses pushes
  to the public-repo remote.

### Misc

- `vct-module.json` hook count corrected: 27 → 31 (matches actual
  `templates/hooks/*.sh` count).

## [0.2.49] - 2026-06-08

The largest single-tag release to date — a 28-item access-matrix
overhaul (10 phases) that closes the asymmetric semantic where the
`kg_collection_access` matrix was a read-gate only. WRITES previously
flowed through `WEAVIATE_URL` blindly, bypassing the access policy the
user could see in the launcher GUI. v0.2.49 makes it a true two-way
gate: every WRITE-path consumer now consults the hub at write time.

Plus 4 SHIP-BLOCKERs, 8 MUST-FIXes, and 9 SHOULD-FIXes from a 4-lens
adversarial Opus review (Step F) — all closed in-tag per the no-defer
release discipline. Plus a contributor's 10-commit UI polish, the Windows-
parity follow-up that closes the OS-specific WRITE-gate bypass, and
the in-cycle carry-over fixes from Bug K through SB1.

### Phase 1 (Steps A + A.5) — Foundation

- **`app_state.orchestrator_root_kg_collection`** is now the canonical
  name for the shared orchestrator KG (default
  `VibeCodedOrchestrator_KnowledgeGraph`, overridable via
  `VCT_ORCHESTRATOR_ROOT_KG_COLLECTION` env at install.py time).
  Migration 028 adds the persistence; every consumer that asks "is
  this the shared root?" calls
  `db.get_orchestrator_root_kg_collection()` and byte-compares.
- **`kg_collection_access` audit columns** (`created_at` +
  `updated_at INTEGER NOT NULL DEFAULT 0`, migration 029). Legacy rows
  backfill to 0; v0.2.49+ INSERTs bind both to wall-clock millis. The
  `is_user_configured(row) := row.updated_at != row.created_at`
  predicate is now implementable.
- **`Db::kg_seed_access`** — system-driven INSERT OR IGNORE setter
  that preserves user-configured rows on re-runs. Distinct from
  `kg_set_access` (user mutation, bumps `updated_at`). Seed paths use
  `kg_seed_access`; UI mutations use `kg_set_access`.

### Phase 2 (Step B) — Centralized resolver

- **`AccessLevel` enum** (`Read | Write | Denied`) with wire-stable
  `as_str()` returning `"read" | "write" | "none"`. Replaces ad-hoc
  string literals across the launcher.
- **`Db::resolve_default_access_level`** — F-2a default: row with
  role `primary` or `shared` → `Write`; otherwise → `Denied`. Single
  source of truth.
- **V44-C structural-row guard relocated** into `Db::kg_set_access` +
  `Db::is_orchestrator_root_structural_row` helper. The
  orchestrator-root primary row can no longer lose write access via
  any code path.
- **`is_shared` substring heuristic eliminated** — replaced with byte
  equality against `db.get_orchestrator_root_kg_collection()`.

### Phase 3 (Step C) — F-1 SHIP-BLOCKER boot-order fix

- **`tokio::sync::oneshot` channel** between adopt + reconcile in
  `lib.rs::setup`. Adopt sends `()` on completion; reconcile awaits
  before its orphan-prune sweep. Pre-fix, scheduler race could drop
  access rows BEFORE adopt rewrote bindings → silent data loss for
  users with renamed collections.
- **`Db::update_binding_for_adoption`** now also calls
  `Db::kg_rename_access` BEFORE the binding UPDATE.

### Phase 4 (Stream W1 + RL module track #13) — Call-site wiring

- **Hub `set_kg_binding`** routes through `_with_root_sync` (M-7).
- **Hub CLI `vct project create`** calls
  `Db::populate_kg_collection_access_for_project`. Pre-v0.2.49 CLI-
  created projects had an empty access matrix.
- **`rename_project_v2`** propagates rename to `kg_collection_access`
  rows.
- **`ModuleManifest.kg_collections: Option<Vec<String>>`** field —
  global-scope modules' declared collections land access rows for
  every project (M-3).

### Phase 5 (Stream W2) — UI

- **F-2b**: "Remove access" button → `mode='none'`.
- **F-2c**: peer-revoke mutation loop skips rows where
  `is_user_configured()` is TRUE.
- **M-1**: boot reconcile scoped to VCO-managed suffixes only.
- **M-4**: hint text rewrite.

### Phase 6 (Stream W3) — Boot folder sanity

- **`projects.folder_missing_at_last_boot`** column (migration 030).
- **Boot probe** flags projects whose folder went missing.
- **`ProjectCard.svelte`** non-blocking warning banner.

### Phase 7 (Step D) — Force-upgrade migration

- **Migration 031** unconditionally upgrades shared
  `kg_collection_access` rows from `read` to `write` per the user
  directive "force-update everything to new default permissions".
  Per Step F's SF1 + Q4 verdicts, migration 031 also:
  - Drops cross-project peer-grant rows that aren't in the v0.2.49
    default keep-list (own primary + own dev + own shared, with a
    corruption carve-out for projects lacking a `role='primary'`
    binding).
  - Sentinel-stamps surviving rows with `updated_at = created_at + 1`
    so `is_user_configured` reads FALSE post-migration.

### Phase 8 (Stream W4) — WRITE-path gate

- **Hub endpoint** `GET /api/v1/projects/{id}/access/{collection}`
  returns `{"level": ...}`. With Step F's SB3 fix, row-absent
  consults `resolve_default_access_level`; row-present round-trips
  through `AccessLevel::from_str_strict` defensively (SF3).
- **Hub endpoint companion** `GET /api/v1/projects/{id}/access?level=write`
  returns the project's writable collections (Q2 enrichment).
- **`templates/scripts/vct_access_check.{sh,ps1}`** + matching
  Python sibling `vco_lib/access_resolver.py` — resolver clients. Hub
  discovery mirrors `vct_project_config`; 5s HTTP timeout; fail-open
  on every error class. PID-scoped rate-limit on hook callers
  (ephemeral); process-scoped on the long-running MCP (no log spam).
- **Hooks**: `templates/hooks/post-file-edit.{sh,ps1}` gate both
  kg-sync paths.
- **MCP server**: `store_knowledge_node` consults the matrix; deny-
  branch response carries `writable_collections` populated from the
  hub for self-resolving errors.

### Step F — adversarial review closures (post-Phase-8)

A 4-lens read-only Opus adversarial review surfaced 28 findings; all
4 SHIP-BLOCKERs, 8 MUST-FIXes, and 9 SHOULD-FIXes closed in-tag per
the no-defer release discipline.

**SHIP-BLOCKERs**:
- **SB1**: empty `VCT_PROJECT_ID` silently bypassed the WRITE-path
  gate. Fix: install.py + project_init.py unconditionally seed
  `VCT_PROJECT_ID=<uuid>` in `.claude/env`. Gate's empty-PID branch
  emits `UPDATE_DEFERRED.md` entry (per user directive: no stderr
  WARNING; deferral file IS the actionable surface) + a
  `dropped_writes.jsonl` row with `reason=gate_skipped_no_project_id`.
- **SB2**: `populate_kg_collection_access_for_project` lifted into
  `db::access`; hub CLI `rename_project` now calls
  `propagate_kg_access_on_rename` (was Tauri-only); `_Diagrams`
  suffix added to propagate list.
- **SB3**: hub endpoint row-absent consults
  `resolve_default_access_level` (was literal `"none"`). Unifies
  three "absent row" defaults across resolver/hub/MCP surfaces.
- **SB4**: `kg_set_access` no-op UPSERT no longer bumps `updated_at`.
  Conditional CASE clause: only on real level change. Pre-fix
  unconditional bump poisoned `is_user_configured` for F-2c logic.

**MUST-FIXes**: V44-C guard in `kg_seed_access` (MF1); `maintenance.rs`
byte-equality (MF2); per-project populate iterates
`list_global_module_installs` for back-fill (MF3 v2 — DB-storage
refactor); peer-row cleanup on `delete_project_v2` (MF4); narrow
`ImportError`/`Exception` catches + gate-crash metric in MCP server
(MF5/MF6); `store_knowledge_node` docstring + deny-branch enrichment
with `writable_collections` (MF7); bash reason-string split aligned
to py/ps1 (MF8).

**SHOULD-FIXes**: migration 031 sentinel + cross-project peer drop
(SF1 + Q4); symmetric V44-C test (SF2); defensive `AccessLevel`
round-trip in hub endpoint (SF3); `pick_higher_access_level` takes
`AccessLevel` (SF4); rename propagate `_Diagrams` (SF5, inline in
SB2); test fixture DDL aligned to migration 029 (SF6); live-binary
tests for `vct_access_check.{sh,ps1}` (SF7); log rotation on
`dropped_writes.jsonl` (SF8).

### UI polish integration — 10 commits across 6 UI branches

- Boot splash (orbital rings + vector logo).
- Update modal restyle (orbital logo).
- Error-notification bell in MenuBar (fixes v0.2.48's sidebar-
  clipping placement).
- Module action buttons on every surface.
- Module install/update failure detection from reloaded catalog kind.
- 5 UI polish fixes across routes + sidebar HiDPI logo + audit page
  header alignment.
- Brand reference doc (`.claude/references/VCO_BRAND_REFERENCE.md`) +
  CLAUDE.md UI/visual directive.

### Other v0.2.49 carry-over fixes

- **install.py self-heal**: parity-insert matches canonical
  `kg_collection_access` schema (Bug O) + RO-mode probe pattern
  (Bug N).
- **kg-sync**: rejects venvs missing `weaviate_mcp` + cross-OS python
  binary detection (Bug K).
- **Install streaming**: `install.py` progress events stream to
  `OrchestratorUpdateProgressModal`.
- **Embedding-selector tier boundaries**: strict-greater-than on
  RAM/VRAM + physical-core check.
- **3 SHIP-BLOCKERs from adversarial review** closed in commit
  `3a706434`.
- **`PerPullAuth` flag-position bug** trio — uses
  `REGISTRY_AUTH_FILE` env, not `--authfile` flag.
- **Single-instance lock** for `install.py --update`.
- **Orphan `install.py` reaper** on launcher startup.

## [0.2.48] - 2026-06-05

Three-fix release closing the gap that broke v0.2.47's `Update orchestrator`
flow + a UX bug surfaced in the License Manager modal + a structural
fix so the underlying class of bug can't recur silently.

### Update flow unblocked (the v0.2.47 ship + binary-refresh asymmetry)

v0.2.47's "version bump" commit (`50665ca`) bumped pyproject.toml,
Cargo.toml × 3, tauri.conf.json, package.json — but missed
`vct-module.json`. CI auto-committed the v0.2.47 binary refresh on top,
so `vct-launcher.metadata.json::launcher_version` advanced to "0.2.47"
while `vct-module.json::version` stayed at "0.2.46". The launcher's
`Update orchestrator` flow read both files on the next `git pull` and
saw `source=0.2.46`, `on_disk=0.2.47`. The post-pull poll
`WaitForBinaryRefresh` expected `on_disk == source` and could never
match because on-disk was AHEAD of source. Five-minute deadlock,
"Binary refresh did not land within 300 sec" modal, retry failed for
the same reason.

- **`vct-module.json`** (+ all 7 other version pins): bumped 0.2.46/0.2.47
  → 0.2.48 in a single coordinated commit so no pin lags this time.
- **`wait_for_binary_refresh` (`installer.rs:3689`)**: exit condition
  changed from `on_disk == source` to `on_disk >= source` via the
  existing `version_is_outdated()` semver helper. When on-disk is past
  source, the loop now exits OK on the first iteration with an
  `eprintln!` documenting the skew (the user keeps running the newer
  binary; that's the only thing they could sensibly run). Pre-existing
  tests (5 in `test_v0245_wait_*`) untouched + 2 new regression tests
  (`test_v0248_wait_succeeds_when_on_disk_ahead_of_source`,
  `test_v0248_wait_succeeds_when_on_disk_ahead_multi_digit`).

### License Manager modal stranded after Remove

Clicking `Remove` on the Orchestrator-tier card and confirming the
prompt stranded the modal on the "No paid-module license keys yet"
empty-state with no "paste a new key" textbox — even after restart.
Root cause: `list_license_keys` (Rust side) only included the
`__orchestrator__` row when a real SQLite row existed OR a legacy
keychain entry was present for one-shot migration. After clear, neither
held, so the wire payload had 0 rows. The front-end's `visibleKeys`
filter always passes `module_id === '__orchestrator__'` through — but
that's only useful if the row is IN the input array. The Orchestrator
slot is a structural always-shown surface; tying its existence to a
data-row presence check was the bug.

- **`list_license_keys` (`licensing.rs:1483`)**: always emit a row for
  `__orchestrator__`. Real persisted row when present; new
  `synthetic_orchestrator_placeholder` (display name "Orchestrator tier
  (root)", redacted_key="(not stored)", validation fields cleared) when
  not. The GUI now renders the Save & Validate textbox in both cases.
- New test `synthetic_orchestrator_placeholder_shape` pins the wire shape.

### Pre-ship check generalized (the structural fix that would have caught the above)

Per-release versioned gate-runner scripts (`v0242-pre-ship-check.sh`,
`v0244-pre-ship-check.sh`, `v0245-pre-ship-check.sh`, `v0246-pre-ship-
check.sh`, `v0246-part2-pre-ship-check.sh`) were exactly the
antipattern that let v0.2.47 ship with the missed `vct-module.json`
bump — there was no `v0247-pre-ship-check.sh`, so the version-pin gate
that would have flagged it didn't run at all.

- **New canonical `scripts/pre-ship-check.sh`** (replaces all five
  per-release scripts). `EXPECTED_VERSION` resolves at runtime in this
  priority: `$1` (argv) → `$EXPECTED_VERSION` (env) → `pyproject.toml`
  (single source of truth). `VERSION_PIN_FILES` is a declared array
  near the top — adding/removing a pinned file is a one-line change
  with no version-number copies to keep in sync.
- 5 per-release scripts deleted from `scripts/`. CI workflow
  references (none found in `.github/workflows/`) require no follow-up.
- Existing release-cycle gates carried forward intact (CI workflow
  status, manifest validate, CodeQL alerts, CHANGELOG entry, working-
  tree clean, dist-binaries presence, [Unreleased] empty,
  re-embed-regression V46-C live gate when Weaviate is reachable).

### CI re-greened (closed via the pre-ship-check sweep)

- **Vitest CVE GHSA-5xrq-8626-4rwp** (critical): vitest@^3.2.4 →
  ^4.1.8. The UI server arbitrary-file-read-and-execute issue was the
  M1 follow-up queued from v0.2.46's release-readiness adversarial.
  All 5 test files / 82 tests pass on vitest 4 (vanilla
  `describe/it/expect/vi/beforeEach` surface, no breaking API
  contacts).
- **`tests/test_v0247_telemetry_v3.py::TestEmbeddingServiceMemoCache`
  CI failure** (4 tests, since v0.2.47): `EmbeddingService.for_project`
  probes the live text + code backends at construction time and raises
  `NoEmbeddingBackendError` when neither is reachable. Memo-cache tests
  don't actually need a live backend (they mock
  `_embed_text_via_active`) but the constructor-time probe broke CI
  every push since v0.2.47 ship. Fix: mock `text_backend_ready` +
  `code_backend_ready` during the `for_project()` call in
  `_make_service()`. Verified by running the suite with
  `OLLAMA_URL=http://localhost:9999` (unreachable) — all 4 pass.

### Diagnosed (no code change in v0.2.48)

- **RL-module install reports success but no container appears on a user's
  Windows + Docker machine**: matched both legs of v0.2.47's
  `58ad931` supervisor + docker-auth fix (variant suffix stripped from
  `docker run` tag + `docker pull` doesn't honor `--authfile`). The
  v0.2.47 fix is correct; the user's machine just hadn't exercised it yet
  because (a) `state/install-manifest.json` was still at 0.2.30 (`git
  pull` advanced source + binary, but `install.py --update` / "Update
  orchestrator" was skipped); (b) the RL module hadn't been re-installed
  since the launcher binary advanced. The `module_installs` row showed
  the pre-v0.2.47 codepath's silent-success shape (`status='installed'`
  + `last_error != NULL` + `container_name = NULL`) with
  `last_error="docker run failed (exit 125): Unable to find image
  'ghcr.io/hotak92/vct-rl-reranker:0.2.9' locally → error from
  registry: unauthorized"`. The image on disk is `:0.2.9-cpu` (proving
  pull succeeded); `docker run` was invoked with bare `:0.2.9` (no
  `-cpu` variant) → cache miss → re-pull → 401. Resolution: "Update
  orchestrator" → reinstall RL → exercises the v0.2.47 fix.

### Queued for v0.2.49

- **Windows Desktop shortcut on install**: Linux install
  drops a `.desktop` file; Windows currently doesn't. Adding `.lnk`
  creation to install.py touches the install path — bigger surface
  than v0.2.48's scope.

### Branch + worktree cleanup

122 stale branches deleted from `vibecoded-orchestrator/` clone (14
worktree branches from v0.2.42/v0.2.43 dev cycle + 108 audited fully-
merged feat/v0.2.34/v0.2.35/v0.2.36/v0.2.37/v0.2.38/v0.2.39/v0.2.40
agent-fanout branches). 14 worktree folders removed from disk. Each
branch verified via `git cherry origin/main` patch-id equivalence to
confirm 0 lost work before deletion.

## [0.2.47] - 2026-06-05

Two-feature release. Both ship together because the supervisor-fix
unblocks paid-module install on private GHCR images (RL Reranker v0.2.9
hits this immediately) and the codegraph-extra-paths feature has been
asked-for since the v0.2.40 multi-codebase work.

### Supervisor image-resolution + docker-auth fix

Closes the two-bug gap discovered post-v0.2.46 when the user attempted
to install `vct-rl-reranker` v0.2.8 on a CUDA host: container start
asked for `:0.2.8` (no variant) anonymously and 401'd against private
GHCR, despite the install pull-token gateway working correctly.

- **Bug 1 (variant suffix missing in start path)**: the launcher-side
  `start_container_for_module` substituted bare `manifest.version`
  ("0.2.8") into `{install.container.tag}` instead of piping through
  `resolve_variant_tag(manifest, version, gpu_mode)`. Fix: promoted
  `resolve_variant_tag` into `vct-launcher-core::services::container_runtime`
  and threaded `gpu_mode` through `resolve_image_ref`. Both launcher
  and hub-side supervisors read the persisted `GpuMode` from the same
  `launcher.hardware_snapshot` row.
- **Bug 2 (start path runs `podman/docker run` without authfile)**: even
  if the variant is correct, cache eviction would fall through to
  anonymous pull. Also discovered: `docker pull` doesn't support
  `--authfile` at all (only `docker login` reads `~/.docker/config.json`),
  so the existing v0.2.46 install path was silently docker-broken too.
  Fix: per-runtime auth dispatch via `PerPullAuth::apply_to` —
  podman gets `--authfile <file>`, docker gets `DOCKER_CONFIG=<dir>` env.
  Plus a pre-pull from the start path that reuses the install path's
  pull-token gateway so the cache is always warm.
- **De-duplication**: two parallel copies of `resolve_image_ref` +
  `build_podman_run_args` + helpers (one in launcher's `module_service.rs`,
  one in hub's `module_supervisor.rs`) merged into a single
  `vct-launcher-core::services::container_runtime` module. Both callers
  now re-export via `pub use`. `DEDUP_SENTINEL` constant gives a compile-
  time conflict if a future PR shadow-defines either helper.
- See `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`
  for the full two-bug analysis and `knowledge/concepts/paid-module-install-update-foundation-2026-06-02.md`
  addendum for the gap classification.

Test delta: +28 (vct-launcher-core 419→441, vct-hub 202→204, launcher 1297→1301).

### Project-extra codegraph paths

New per-project Identity tab feature: index additional read-only
filesystem paths into a project's codegraph collection without making
those paths launcher projects. Canonical use case: index a sibling
`vibecoded-orchestrator/` clone into the maintainer's codegraph for unified
`search_code_graph` queries without registering the clone as a project.

- **New migration `026_project_codegraph_extra_paths.sql`**: schema
  `(project_id, path, label, added_at, last_indexed_at, last_indexed_commit, enabled)`
  with PK `(project_id, path)` and CASCADE on project delete.
- **6 Tauri commands** in `commands::project_codegraph_extras`:
  `list_*`, `add_*` (returns two-variant `AddExtraPathOutcome::Added` /
  `DisambiguationRequired`), `remove_*`, `set_*_enabled`, `sync_*`,
  `reindex_*_after_extras_change`. Per-project mutex registry
  (`ExtrasLockRegistry`) serialises add → reindex sequences so concurrent
  mutations can't race the snapshot read.
- **Hub resolver field** `code_graph_extra_paths: Vec<{path, enabled, last_indexed_commit}>`
  on `/api/v1/projects/{id}/config`. Additive; older Python/bash/PS1
  clients ignore the unknown field. Soft-fail-to-empty on DB read error.
- **Analyzer CLI extensions** in `analyze_code_graph.py`:
  `--extra-path <DIR>` (repeatable) walks additional source roots in
  the same pass; `--since-commit <SHA>` restricts incremental analysis
  to `<sha>..HEAD` per source root (non-git roots fall back to full
  scan). `project_source` property stamped on every emitted row.
  **Critical invariant**: `visited_uuids` is a single shared set across
  primary + all extras so `--prune-stale` only deletes UUIDs that were
  truly not visited. Multi-pass with `--prune-stale` would cause each
  pass to delete the others' UUIDs — structurally impossible now.
- **Hook update** in `code-graph-incremental.sh` (and `.ps1` sibling):
  new check order — own-repo (fast path, no hub RTT) → extras of
  current project → siblings → no-op. Edits under an extra path
  re-index into the CURRENT project's codegraph prefix.
- **Resolver client field** added to all 3 client libs:
  `templates/scripts/vct_project_config.sh` and `.ps1` support
  `--field code_graph_extra_paths` (newline-delimited enabled paths);
  `vco_lib/project_config.py` adds `ExtraCodegraphPath` dataclass +
  `ProjectConfig.code_graph_extra_paths: tuple[ExtraCodegraphPath, ...]`.
- **GUI**: new `ExtraCodegraphPathsPanel.svelte` on the Identity tab
  with per-row Sync/Disable/Remove. Auto-sync on add (minimisable
  modal); re-sync with `--prune-stale` on remove or disable (so
  orphan entries get pruned). Disambiguation modal when the picked
  path is the root of an existing launcher project: "Add as project
  (grant access matrix)" / "Add as path anyway" / "Cancel".
- See `knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md`
  for the architecture write-up.

Test delta: +101 across all 5 surfaces (Rust core +20, Rust hub +4,
Rust launcher +26, Python +20 covering analyzer + ProjectConfig +
bash resolver + hook integration tests, Svelte vitest +11).

### Two deferrals applied during this cycle (not part of the v0.2.47 feature work)

- Dropped orphan Weaviate collection `VibeCodedOrchestrator_Development`
  (0 rows, no callers — superseded by per-project Development
  collections in v0.2.46).
- Re-synced KG + codegraph with the v0.2.46 chunker preset overhaul
  (heavy I/O ran in background during agent fan-out).

### Totals

Grand-total test delta: **+129** (1946 pre-v0.2.47 → 2075 post-v0.2.47).
All gates green: vct-launcher-core 461, vct-hub 208, launcher 1327,
svelte-check 0 errors, vitest 82, pytest 3579.

## [0.2.46] - 2026-06-04

### RL Telemetry (Unified target / pure-train container / hub-side citations) — landing alongside v0.2.46

- **Vendored-file byte-identity guarantee** for the orchestrator ↔ paid-module file pairs that exist solely because the container image must build standalone. Two pairs are covered: `vco_lib/rl_training_targets.py` ↔ `paid-modules/vct-rl-reranker/_training_targets.py`, and `claude_mcp_servers/rl_client/rl_logger.py` ↔ `paid-modules/vct-rl-reranker/rl_logger.py`. New `tests/test_vendored_file_sync.py` (SHA-256 drift test, CI-gated) and `scripts/sync-vendored-files.sh` (canonical→vendored re-sync, self-anchored, post-copy verified) enforce the guarantee mechanically. Header comments on the canonical files name the vendored siblings + the test path + the sync command, eliminating the "vendored copy drifted silently" failure mode reported during v0.2.47 RL telemetry work.

- `RLDataLogger.log_citations` + `RLTelemetryWriter.log_citations` accept an optional `answer_text: str | None = None` kwarg (omitted from the event when None). Reserves the schema slot for future answer-text logging (offline multi-model training) without a v3→v4 schema bump. v0.2.9 callers leave it None.

(Full v0.2.47 RL chain — 14 commits, RL-1 through RL-8 — landed via merge from `rl/citation-detection-mcp-side`. Pairs with paid-module `vct-rl-reranker` v0.2.9 release. The "v0.2.47" prefix on those commits is historical; all of them ship in v0.2.46.)

Three-part release: Part 1 closes the 5th instance of a recurring KG
re-embed bug + RL Reranker client-side hardening + 10 silent-truncation
footgun cleanups. Part 2 delivers the third-party project adoption mode
(`--adopt-project` family) — gap-plugging across 7 agents (V47-A through
V47-G-final) covering settings.json managed-block merge, symlink
preservation, secrets detection + keychain migration, venv adopt-guard,
foreign-compose scan, PROJECT_NAME precedence resolver, detection
heuristic + interactive prompt + dry-run + GUI wizard + Secrets tab
Migrate button. **Part 3 (KG-AUTO-HEAL A through E + module_supervisor
hermetic fix + housekeeping)** delivers the zero-manual-step KG/Dev/
access auto-heal across launcher boot + write paths + hub resolver so
3rd-party users post-update see correct collection routing without any
GUI clicks, even when upgrading from a prior release where binding rows
drifted (full v0.2.12–v0.2.45 migration coverage).

The Part 1 headline bug: `install.py:5948`
(`_batch_query_weaviate_content_hashes`) and `install.py:6135`
(`_prune_stale_kg_rows`) both shipped a GraphQL filter
`where: {operator: Like, valueText: "%"}` — `%` is SQL wildcard
convention; Weaviate uses `*` and rejects `%` as "only stopwords
provided". The HTTP 200 + errors-array response was silently coalesced
to `{}` via `body.get(..., []) or []`, so the diff gate saw zero stored
hashes on every update → full re-embed of all KG files. 4 parallel
Opus investigators converged HIGH confidence on the root cause; 2
auditors independently verified the proposed fix doesn't lose any
feature. See `knowledge/concepts/silent-zero-fallback-antipattern.md`
instance #3 + `knowledge/concepts/install-py-collection-bootstrap-bugs.md`
v0.2.46 section.

V46-B's live integration test caught a SECOND v0.2.43 bug in the same
prune function: the batch-delete used `valueText: <list>` (singular)
when ContainsAny on a UUID list requires `valueTextArray` (plural).
Fixed in V46-A-followup. Exactly why live integration tests matter.

### Fixed

- **install.py CI-10 / V0243-6 stopword GraphQL filter**: drop the broken
  `where: {Like, "%"}` clause that has been silently re-embedding every
  KG file on every `--update` since v0.2.42 (2026-05-31). Bump
  `limit: 1000` → `limit: 10000` (Weaviate's QUERY_MAXIMUM_RESULTS
  default). Inspect `body.get("errors")` BEFORE consuming data; non-empty
  → WARN log + full-sync fallback (loud, not silent). Plus V46-A-followup:
  fix the batch-delete in `_prune_stale_kg_rows` from `valueText:<list>`
  to `valueTextArray:<list>` (a separate v0.2.43-era bug that V46-B's
  live integration test caught). (V46-A, V46-A-followup)

- **10 silent-truncation footguns** across `templates/scripts/*.py` and
  `claude_mcp_servers/weaviate_mcp/server.py`:
  - Pattern A (cursor pagination for full-enumeration):
    `get_all_weaviate_nodes`, `rebuild_all`, `list_all_nodes` (kg-search
    list), `detect_duplicates`, `process_documents`
  - Pattern B (emit `truncated: true` flag for top-N queries):
    `get_node_info` inbound-link scan, `query_code_graph` relations,
    `query_code_structure` MCP tool callers/imports/interactions/
    composed_by/type_users

  Pre-v0.2.46 these silently degraded behavior on collections >1000
  (Pattern A) or >20-50 (Pattern B). LLM consumers of the MCP tool now
  see a truncation signal. (V46-D)

- **RL Reranker client-side install path**
  (`installer_engine.rs::container_pull`):
  - C1: honor server's `tag` value over client-resolved L0 version
  - C1-followup: preserve client GPU-variant suffix
    (`-cpu`/`-cuda`/`-rocm`/`-metal`) when server returns a bare
    patch-level tag (variant resolution stays client-side per the edge
    function's documented contract)
  - C2: per-pull `--authfile <tmp>` via tempfile RAII; removes the risk
    of `podman logout` invalidating a concurrent paid-module pull
  - C3: audit-log emission — `pull_token_requested` /
    `pull_token_resolved` (with `server_tag` / `client_resolved_tag` /
    `effective_tag_with_variant` / `tag_mismatch`) / `pull_token_failed`
    (with HTTP code + `error_class` classification). License key NEVER
    logged in full — 12-char prefix only.
  - C4: hard-fail on MAJOR/MINOR tag mismatch with publisher-pointing
    error message (variant suffix is NOT a version component — same
    base version, different VARIANTS = no mismatch)
  - C5: documented in code comments

  Surfaced by v0.2.45 post-ship forensic. Fixed in coordination with
  the RL module owner (`vct-rl-reranker` repo) who confirmed end-to-end smoke
  green 2026-06-03. (V46-E)

*Part 3 (KG-AUTO-HEAL):*

- **`module_supervisor::tests::resume_null_container_service_routes_through_start_after_install` made hermetic**: previously environment-dependent — passed when no container runtime was installed, failed when podman or docker was available because the test relied on `start_container_after_install` to FAIL (which it doesn't when the CLI is reachable). Now uses `Fn`-pointer injection (`StartAfterInstall` boxed async closure mirroring the existing `ManifestResolver` pattern); the test injects a stub that always errors, so the NEW-3.C assertion (`last_error.is_some()`) holds independent of host runtime. Cross-runtime verified: `detect_container_runtime` (line 246) iterates `["podman", "docker"]` symmetrically; the test now short-circuits before either path. (KG-AUTO-HEAL release-blocker)

- **Dark-mode CSS bug in `launcher/src/lib/components/StorageSettingsCard.svelte`**: the Storage settings card (volume reuse panel) used undefined CSS variables (`--bg-card`, `--text-muted`, `--bg-button`, `--accent`, `--border-subtle`) with light-mode hex fallbacks (`#fff`, `#1a1a1a`, `#f9f9fb`, `#2563eb`). Every fallback won → unreadable white card on the app's dark theme. Aligned to the canonical `ps-*` recipe used by `IdentityTab` / `SecretsTab` / `SettingsTab` / `KgCodegraphTab` / `SkillsTab` / `HooksTab` (rgba surfaces, `color: inherit`, `#888` muted, `#c4b3ff` section headings, `rgb(0,191,166)` teal primary). Native `<select>` styling NOT overridden — `src/app.css`'s global `color-scheme: dark` covers all dropdowns app-wide (lesson from `0d540b6 fix(secrets): dropdown styling`). No layout / class-name changes.

- **`.claude/CONTEXT_STATE.md` no longer tracked**: file is a per-project working artifact (the maintainer's daily state — current task, recent progress, blockers). Was tracked on local `main` so every public-repo clone inherited the maintainer's working state instead of a blank slate. The file is NOT yet on `origin/main` (verified via `git log origin/main -- .claude/CONTEXT_STATE.md` empty), so removing it from the tracked set before v0.2.46 push keeps the public repo clean. `install.py`'s `DEFAULT_PRESERVE_LIST` already treats it as a per-project artifact (preserves existing installs' files across updates), so behaviour for upgrading users is unchanged.

- **2 pre-existing stale-fixture failures in `manifest::tests::vct_rl_reranker_manifest_*`**: `manifest.version` assertion was hardcoded to `"0.1.1"` (pre-v0.2.22) but the live paid-module manifest is at `0.2.9`. `has_info` assertion required a static `Info` control but the current manifest uses `InfoDynamic` (introduced v0.2.32). Bumped to series-prefix match (any 0.2.x / 0.3.x / 0.4.x) AND extended to accept `InfoDynamic` as also satisfying the "section 1 must include at least one info banner" spirit.

- **`test_get_node_info_access_list.py` + 5 other test files: hub-resolver leak**: tests setting `KG_COLLECTION` / `SHARED_KG_COLLECTION` env vars and asserting against env-fallback paths were silently losing to live hub-resolved values on dev machines running `vct-hub`. 21 pre-existing test failures across `test_get_node_info_access_list.py`, `test_shared_kg.py`, `test_search_knowledge_access_list.py`, `test_v0246_v46d_truncation_fixes.py`, `test_kg_collection_env_backfill.py`. Fixed in two layers: (a) gate inside `vco_lib.project_config.resolve()` itself, (b) `tests/conftest.py` autouse `monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")` for the whole suite. All 6 files green. See KG node `parallel-pr-coordination-gotchas-2026-05-10.md` §14 (reinforced + 2026-06-04 update).

- **`test_ps1_utf8_bom.py` scanned `.claude/worktrees/`**: walked into per-agent worktree directories (which carry pre-BOM-fix `.ps1` snapshots by design) and reported them as offenders. Test now excludes `.claude/worktrees`, `node_modules`, `target`, `.venv`, `vibecoded-orchestrator` — scans only files we actually own.

### Added

*Part 1 (V46):*

- **`vco_lib/weaviate_helpers.py`**: reusable helpers for safe Weaviate
  GraphQL interaction. `check_graphql_errors(body, ctx, on_error)` and
  `post_graphql_safe(url, gql, ctx, on_error)` centralize the
  errors-array inspection pattern that the recurring re-embed bug was
  rooted in. Plus `WeaviateGraphQLError` exception class. Future
  callers should use `post_graphql_safe` to inherit the protection by
  default. (V46-F)

- **Live integration tests against real Weaviate** (no mocks):
  `tests/test_v0246_v46b_live_ci10_diff_gate.py`. Brings up a real
  Weaviate (or skips cleanly when unreachable), seeds 3 known objects
  with known `content_hash` values, calls
  `_batch_query_weaviate_content_hashes` directly (NOT mocked), asserts
  the production function returns the expected data. Includes defensive
  source-inspection regression guards that run unconditionally (no
  Weaviate dep) to prevent reintroduction of the SQL-wildcard pattern.
  This is THE missing test that let the bug ship across v0.2.42–v0.2.45.
  (V46-B)

- **Pre-ship-check Gate 18 + 18b**: `scripts/v0246-pre-ship-check.sh`
  clones the v0245 script + adds Gate 18a (live diff-gate fetches
  correctly) + Gate 18b (live prune deletes orphans correctly). The
  structural fix for the "fresh-clone blind spot" that hid the bug
  across 4 releases. (V46-C)

*Part 2 (V47):*

- **Third-party project adoption mode** (`--adopt-project` /
  `--no-adopt-project` / `--adopt-project-replace-all` /
  `--adopt-project-dry-run`): install.py CLI flags + mutex group +
  `_resolve_adopt_project_mode(args)` dispatcher. Enables safely
  installing VCO into an existing project without overwriting
  non-VCO config. (V47-G-stub + V47-G-final)

- **`--project-name <NAME>` CLI flag**: explicit PROJECT_NAME override
  at install time, highest-precedence layer in
  `_resolve_project_name_for_adopt`. (V47-F)

- **`--rebuild-venv` CLI flag**: forces `_venv_triage` to recreate the
  project venv even when no `vct-module.json` manifest is present.
  (V47-D)

- **`.claude/settings.json` managed-block merge schema** with
  `_vco_managed_keys` sentinel: file-present + sentinel → merge only
  VCO-managed keys; file-present + no sentinel → legacy full overwrite;
  file-absent → write VCO defaults. Prevents clobbering user-added
  env vars on every install/update. (V47-A)

- **`vco_lib/symlink_handler.py`**: `is_symlink_blocking(dest)`,
  `compute_vco_new_path(dest)`, `emit_symlink_deferral(...)` — gate
  logic for bundle-apply when destination path is a symlink. Resolves
  the `os.path.exists` vs `os.path.lexists` divergence at gating sites
  that silently skipped writing to broken-symlink destinations. (V47-B)

- **`vco_lib/secrets_audit.py`** + **`vct-hub` POST
  `/api/v1/secrets/migrate` endpoint**: `audit_env_secrets(path)`,
  `is_secret_shaped_env_key(key)`, `rewrite_env_with_sentinels(...)`,
  `harden_env_perms(path)`. Detects `.env` secret-shaped keys and
  offers OS keychain migration. Full Rust round-trip for the Migrate
  button deferred to v0.2.47; Python audit layer + hub endpoint land
  in this release. (V47-C)

- **Foreign-compose informational scan**: `_scan_foreign_compose_files(
  install_path)` — walks the project tree, identifies
  `docker-compose*.yml` / `compose*.yml` files NOT owned by VCO,
  prints an informational summary. Non-blocking; no files are
  modified. (V47-E)

- **`_detect_third_party_project` heuristic**: examines
  `install_path` for non-VCO markers (existing `requirements.txt`,
  `package.json`, `Cargo.toml`, etc.) and returns a detection dict
  with confidence score. (V47-G-final)

- **Interactive adopt prompt + dry-run action manifest**:
  `_prompt_adopt_decision(detection, args)` — modal-style interactive
  prompt shown when `--adopt-project` is implied by heuristic.
  `_print_adopt_dry_run_manifest(install_path)` — real dry-run that
  lists every file that would be written without writing any. (V47-G-final)

- **Launcher Add-Project wizard adopt modal**: GUI-side adopt wizard
  step shown when the heuristic fires during project registration.
  (V47-G-final)

- **Launcher Secrets tab "Migrate from .env" button**: frontend
  affordance that calls the `vct-hub` `/api/v1/secrets/migrate`
  endpoint. Full Rust keychain-write round-trip deferred to v0.2.47;
  the button + HTTP wiring land in this release. (V47-G-final)

- **`scripts/v0246-part2-pre-ship-check.sh`**: Part 2 gate runner
  (Gates 19–27) covering V47-A through V47-G-final contract surfaces.
  (V47-CHANGELOG)

*Part 3 (KG-AUTO-HEAL A through E):*

- **`Db::set_project_kg_binding_with_root_sync`** + **`Db::sync_shared_to_primary_for_orchestrator_root`** (`vct-launcher-core/src/db/project_state.rs` + `access.rs`): the orchestrator-root project's `project_kg_bindings(role='shared')` row now atomically mirrors the `role='primary')` row at two points — write-time (GUI save) AND boot-time backfill (handles upgrades from any prior release where the write-time auto-sync didn't exist). One-way mirror: only primary→shared triggers sync; writing directly to shared does NOT touch primary. Mirrored rows tagged with `config_json.manual_override = "v0.2.46-sync-shared-to-primary[-boot]"` for downstream audit. Closes the v0.2.40 W40-B research doc §4b drift class. (KG-AUTO-HEAL-A)

- **Hub `development_collection` derives via suffix-swap from primary KG** (`vct-hub/src/config_api.rs`): the pre-v0.2.46 lookup for `role='archive'` always returned an empty string (no installer ever wrote such a row in the current schema). Now mirrors the diagrams derivation immediately below — `_KnowledgeGraph` → `_Development` swap on the resolved primary, slug-sanitized fallback for non-canonical primary names. Unifies hub-served + launcher-populate + Python-derived values which had drifted silently. (KG-AUTO-HEAL-C)

- **`SHARED_KG_READ_DISABLED` per-project read opt-out**: symmetric with the existing `SHARED_KG_WRITE_DISABLED` knob. When set, the MCP excludes `SHARED_KG_COLLECTION` from `collections_to_query` in `hybrid_search` and `semantic_graph_search` fan-out. Plumbed across 13 surfaces: env-resolver + DB-fetch + hub response + Rust env_settings + GUI toggle (per-project Identity tab, default OFF). Asymmetric-by-default preserved: fresh projects read shared KG; users opt OUT explicitly per-project. (KG-AUTO-HEAL-B)

- **`Db::kg_delete_access` + `Db::kg_rename_access` + `Db::reconcile_kg_collection_access` + `reconcile_kg_collection_access_at_boot`** (`vct-launcher-core/src/db/access.rs`): the access-matrix surface for the `kg_collection_access` table. `set_project_kg_binding_with_root_sync` now propagates collection renames into matching access-matrix rows (collision-safe: keeps higher privilege, never lowers). Boot reconcile drops orphan rows whose collection_name doesn't exist in Weaviate AND doesn't match any binding for any project. Keeps rows where either condition holds (peer-access to a peer collection stays live; binding-named collections that don't exist yet stay live). (KG-AUTO-HEAL-D)

- **`vco_lib/kg_sync.py`** (NEW module): extracts the V46-A-hardened content-hash fetch from `install.py::_batch_query_weaviate_content_hashes` into a reusable helper. Single source of truth for both `install.py --update`'s CI-10 diff gate AND the forthcoming KG-rebind re-sync path. Routes via `vco_lib.weaviate_helpers.post_graphql_safe` (V46-F) rather than open-coded `urlopen` — inherits the errors-array gate. `install.py` function is now a thin wrapper preserving the legacy `_log_install_event` observability channel via a structured `on_warn(channel, payload)` callback. V46-B regression guard EXTENDED to inspect the new helper's source (avoids the "wrapper passes vacuously" failure mode). (KG-AUTO-HEAL-E)

- **`VCT_DISABLE_HUB_RESOLVER` env-gate** now lives in `vco_lib.project_config.resolve()` itself (was previously only at `weaviate_mcp/server.py::_try_resolve_project_config`, missing all CLI-side callers). Every consumer of the resolver — MCP, CLI scripts (`get_node_info.py`, `search_knowledge.py`), helpers — inherits the short-circuit. `tests/conftest.py` (NEW) sets `VCT_DISABLE_HUB_RESOLVER=1` via autouse `monkeypatch` for the whole orchestrator suite so tests injecting `KG_COLLECTION` / `SHARED_KG_COLLECTION` env vars on dev-machines-with-hub no longer lose silently to live hub-resolved values. Tests that DO exercise the resolver (`test_project_config.py::_ResolverTestBase`) explicitly opt-out in setUp. Closes the dev-only-test-failure class documented in `knowledge/concepts/parallel-pr-coordination-gotchas-2026-05-10.md` §14. (KG-AUTO-HEAL-E)

### Changed

*Part 1 (V46):*

- **Part 1 re-embed fix** (V46-A/A-followup): GraphQL `where: Like %`
  → drop clause + saturation warning + `valueText` → `valueTextArray`
  for ContainsAny batch-delete. (V46-A, V46-A-followup)

- **Part 1.5 venv-audit hardening**: silent MCP-venv fallback now
  emits a WARNING instead of silent continuation (H1); PYTHONPATH
  scrubbed on pip subprocesses to prevent venv pollution (H2); log
  guard added before os.execv relaunch (H3).

- **`sysinfo` pinned to `"0.38"`** in both
  `launcher/src-tauri/Cargo.toml` and
  `launcher/src-tauri/vct-launcher-core/Cargo.toml`: the 0.39.x line
  requires rustc 1.95+, breaking contributors/CI on 1.94. Forward-
  compatible: relax both pins when the project's rustc floor moves to
  1.95.

- **10 silent-truncation footgun cleanups** (V46-D): cursor pagination
  for full-enumeration queries (Pattern A) and `truncated: true` flag
  emission for top-N queries (Pattern B) across 5 template scripts and
  `weaviate_mcp/server.py`.

- **RL Reranker client-side hardening**: variant suffix preservation +
  per-pull `--authfile` via tempfile RAII + audit-log emission. (V46-E)

- **Version pins bumped 0.2.45 → 0.2.46** across the canonical sites:
  `pyproject.toml`, `vct-module.json`, `launcher/package.json`,
  `launcher/package-lock.json`, `launcher/src-tauri/Cargo.toml`,
  `launcher/src-tauri/tauri.conf.json`,
  `launcher/src-tauri/vct-hub/Cargo.toml`,
  `launcher/src-tauri/vct-launcher-core/Cargo.toml`, and the three
  matching entries in `launcher/src-tauri/Cargo.lock`. Dist-binary
  metadata (`launcher/dist/<arch>/*.metadata.json`) is NOT bumped here
  — it is written by the Release workflow on the binary build.
  `claude_mcp_servers/pyproject.toml` is independently versioned and
  stays at its current version.

*Part 2 (V47):*

- **`_venv_triage`**: refuses destructive recreate when no manifest is
  present (unless `force_rebuild=True` is passed via `--rebuild-venv`),
  preserving third-party project venvs. (V47-D)

- **PROJECT_NAME precedence resolver** (`_resolve_project_name_for_adopt`)
  respects `.vscode/settings.json` / `.claude/env` / `.env` over
  folder-name derivation. Explicit `--project-name` CLI flag wins
  over all. (V47-F)

### Fixed

*Part 1 (V46):*

- **install.py CI-10 / V0243-6 stopword GraphQL filter**: drop the
  broken `where: {Like, "%"}` clause that has been silently re-embedding
  every KG file on every `--update` since v0.2.42 (2026-05-31). Bump
  `limit: 1000` → `limit: 10000` (Weaviate's QUERY_MAXIMUM_RESULTS
  default). Inspect `body.get("errors")` BEFORE consuming data; non-empty
  → WARN log + full-sync fallback (loud, not silent). Plus V46-A-followup:
  fix the batch-delete in `_prune_stale_kg_rows` from `valueText:<list>`
  to `valueTextArray:<list>` (a separate v0.2.43-era bug that V46-B's
  live integration test caught). (V46-A, V46-A-followup)

- **10 silent-truncation footguns** across `templates/scripts/*.py` and
  `claude_mcp_servers/weaviate_mcp/server.py`:
  - Pattern A (cursor pagination for full-enumeration):
    `get_all_weaviate_nodes`, `rebuild_all`, `list_all_nodes` (kg-search
    list), `detect_duplicates`, `process_documents`
  - Pattern B (emit `truncated: true` flag for top-N queries):
    `get_node_info` inbound-link scan, `query_code_graph` relations,
    `query_code_structure` MCP tool callers/imports/interactions/
    composed_by/type_users

  Pre-v0.2.46 these silently degraded behavior on collections >1000
  (Pattern A) or >20-50 (Pattern B). LLM consumers of the MCP tool now
  see a truncation signal. (V46-D)

- **RL Reranker client-side install path**
  (`installer_engine.rs::container_pull`):
  - C1: honor server's `tag` value over client-resolved L0 version
  - C1-followup: preserve client GPU-variant suffix
    (`-cpu`/`-cuda`/`-rocm`/`-metal`) when server returns a bare
    patch-level tag (variant resolution stays client-side per the edge
    function's documented contract)
  - C2: per-pull `--authfile <tmp>` via tempfile RAII; removes the risk
    of `podman logout` invalidating a concurrent paid-module pull
  - C3: audit-log emission — `pull_token_requested` /
    `pull_token_resolved` (with `server_tag` / `client_resolved_tag` /
    `effective_tag_with_variant` / `tag_mismatch`) / `pull_token_failed`
    (with HTTP code + `error_class` classification). License key NEVER
    logged in full — 12-char prefix only.
  - C4: hard-fail on MAJOR/MINOR tag mismatch with publisher-pointing
    error message (variant suffix is NOT a version component — same
    base version, different VARIANTS = no mismatch)
  - C5: documented in code comments

  Surfaced by v0.2.45 post-ship forensic. Fixed in coordination with
  the RL module owner (`vct-rl-reranker` repo) who confirmed end-to-end smoke
  green 2026-06-03. (V46-E)

*Part 2 (V47):*

- **HIGH-severity venv-architecture audit finding** (V47-D):
  `_venv_triage` now refuses to destructively recreate a venv when no
  `vct-module.json` manifest is found in a third-party project. Prevents
  accidental wipeout of existing project virtualenvs.

- **Symlink-following on bundle apply** (V47-B): `os.path.lexists`
  replaces `os.path.exists` at the two gating sites in the bundle-apply
  path, ensuring broken symlinks are detected and deferred rather than
  silently skipped.

### Security

- **`.env` secret-shaped keys** detected by `vco_lib/secrets_audit.py`
  and offered for OS keychain migration via `vct-hub` POST
  `/api/v1/secrets/migrate`. (V47-C)

- **`.env` file permissions hardened** to `0o600` on Unix after audit.
  Windows path is a no-op (ACL-based security model). (V47-C)

- **License key never logged in full**: 12-char prefix only in all
  audit-log emissions; security-pinned by 2 levels of tests. (V46-E,
  security-pinned in Part 1)

### Documentation

- KG nodes extended (no new nodes — extended 3 existing ones per the
  KG hygiene rule "update before create"):
  - `silent-zero-fallback-antipattern.md` — instance #3 (install.py
    GraphQL Like-`%`); now documents the 4-release recurrence + the
    "missing invariant" (real Weaviate test required) + the design
    fix layers.
  - `mcp-loud-fail-error-pattern.md` — new sub-pattern: GraphQL
    `errors[]` array MUST be inspected (third response shape outside
    connection/query-time).
  - `install-py-collection-bootstrap-bugs.md` — v0.2.46 section
    documenting the recurring bug + release-archaeology table showing
    why each prior fix didn't hold.

- Coordination handoff docs for v0.2.46 plan + status response
  (after RL module owner's smoke + Supabase secret bump).

### Verified

- All V46 tests pass on integrated `main` (V46-A 11 + V46-B 7 + V46-D 16
  + V46-E 9 + V46-F 12 = 55 Python + Rust tests across the fanout).
- All V47-A through V47-G-final tests pass (V47-A + V47-B + V47-C +
  V47-D + V47-E + V47-F + V47-G-stub 21 + V47-G-final tests).
- Live two-pass smoke on real Weaviate (Gate 18a + Gate 18b) PASS.
- `cargo check --lib` clean.
- `[Unreleased]` block EMPTY at tag time (no-deferred-fixes rule from
  the post-v0.2.41 retrospective).
- Supabase edge function `rl-artifact-url` deployment verified
  returning `tag: "0.2.8"` after `GHCR_PAID_TAG_DEFAULT=0.2.8` secret
  bump (operational fix shipped alongside this release).

## [0.2.45] - 2026-06-02

Hotfix release closing 3 root causes surfaced on the maintainer's clone during the post-v0.2.44 "Update orchestrator" audit (2026-06-02 ~01:34 UTC). All 6 V45 agents (A-F) merged in-tag; no deferred fixes per the post-v0.2.41 retrospective rule. Foundation work for every future paid module — V45-C through V45-F install the contracts (manifest version-compare at resolve time, placeholder-URL detection family, status state-machine completeness, L0 catalog refresh on lifecycle events) that v0.2.46+ will build on. Per user directive 2026-06-02: "RL module is not released, so can skip any legacy compatibility issue, but we need to make sure that — together with it — we build a strong foundation for every future paid module."

### Fixed

- **install.py self-relaunches under the MCP venv** when `weaviate` is not
  importable. The launcher's `update_orchestrator` Tauri command spawns
  install.py under `detect_python()` → `/usr/bin/python3.12` on Linux, but
  step 7d (`_migrate_kg_named_vector_slots`) does an in-process
  `import weaviate` via `vco_lib.project_init._connect_v4_client`.
  `weaviate-client` lives only in `claude_mcp_servers/.venv`, so the
  import failed → 4 `UPDATE_DEFERRED.md` entries per launcher-driven
  update. New `_ensure_running_under_mcp_venv()` at `main()` entry probes
  `importlib.util.find_spec("weaviate")` and `os.execv`'s into the MCP
  venv when missing. No-op for CLI users (venv already activated) and
  re-entry-guarded via `VCT_INSTALL_RELAUNCHED=1`. Soft-fails when the
  MCP venv interpreter doesn't exist (install proceeds with system Python
  and re-fails at step 7d as before — at least the deferral captures the
  error). Latent since v0.2.43 (`0cb229c`); the maintainer's clone was the first install
  to drive it via the launcher. (V45-A)

- **Launcher polls for the binary-refresh commit before re-execing**.
  Every release ships in two steps: source tag (e.g., `f7682a3` at
  23:54 UTC) and a `chore(binary): refresh ... for v0.2.X` auto-commit
  ~49 min later (`88f9758` at 00:43 UTC for v0.2.44). Inside that window,
  `vco_upstream/main` advertises the new source version in
  `state/install-manifest.json` but still carries the old
  `launcher/dist/<arch>/vct-launcher` binary. `update_orchestrator`
  (installer.rs) and `run_post_pull_install_and_restart` now poll
  (15s interval, 5min timeout) for `read_on_disk_binary_version ==
  read_source_version` before re-execing — closing the window where the
  launcher would silently restart the OLD binary. New
  `WaitForBinaryRefresh` test harness with parameterized
  timeout/interval/git-pull-skip flag for deterministic poll-loop tests.
  Latent since v0.2.16 wired the post-update auto-restart; previous
  releases dodged it via timing luck. Structural fix (tag the
  binary-refresh commit instead of the source commit) deferred to
  v0.2.46-46-1. (V45-B)

- **`resolve_manifest_for_install` compares L0 catalog version vs on-disk**.
  Phase 1 of the resolver used to unconditionally win when the on-disk
  manifest existed (`Installed(path)`), so a paid module's "Retry install"
  after an L0 catalog refresh would still install the stale on-disk
  version. New version-compare logic falls through to `L0Synth` Phase 3
  when the L0 catalog has a strictly newer version than the on-disk
  manifest; on-disk still wins when versions are equal or L0 is older.
  Audit-logged via `module_manifest_resolved` for observability. Fixes
  the user's RL "retry install" picking v0.2.7 when L0 advertised v0.2.8.
  New `parse_semver` helper with canonical-form acceptance + strict
  ordering. (V45-C)

- **Pull-token placeholder family widened** to catch the
  `placeholder.<tld>`, `placeholder`, and `<host>.placeholder` shapes
  (case-insensitive) alongside the existing `example.*` family.
  `is_pull_token_placeholder` previously only matched `example`,
  `example.com|net|org|invalid|test`, so v0.2.7's
  `https://placeholder.supabase.co/functions/v1/rl-pull-token` slipped
  through and the request landed at a nonexistent host, falling through
  to anonymous `podman pull` against private GHCR → 401. v0.2.42 W8's
  "Phase 3A pull-token gateway" only wired the `example.*` family.
  Now also catches HTTP and explicit-port variants. (V45-D)

- **Module status state machine completeness on container-start failure**.
  `start_container_after_install` failures now flip `status='error'`
  alongside the existing `set_module_last_error` call, so the V44-G4
  auto-retry sweep (which predicates on `status IN ('error', 'broken')`)
  can recover rows stuck in `status='installed' + last_error != NULL +
  container_name = NULL`. Pre-v0.2.45 partial-failure rows are backfilled
  once at launcher startup via new one-shot
  `backfill_partial_container_start_failures` Db method (idempotent
  UPDATE on `module_installs`). Closes V44-G4's blind spot. (V45-E)

- **L0 catalog refresh on launcher-version-change + per-project update**.
  `bust_cache_if_launcher_version_changed` (which fires when the
  orchestrator version bumps) now spawns a non-blocking
  `cached_module_catalog` refetch on a background tokio task.
  `update_module_for_project` warms the cache pre-resolve, so a stale
  catalog can never produce a stale `Installed` manifest decision in
  V45-C's resolver. TTL-bounded warm path (no excessive refetches).
  Periodic-timer refresh deferred to v0.2.46-46-4. (V45-F)

### Added

- **`VCT_RL_PULL_TOKEN_ENDPOINT` env var**: runtime override for the RL
  Reranker pull-token endpoint. Short-circuits the L0 catalog / L1
  manifest / hardcoded-default resolution chain inside
  `installer_engine::request_pull_token` and POSTs the license-key
  request to the override URL verbatim. Empty/whitespace-only values are
  ignored. Use when the on-disk endpoint is wrong (manifest still carries
  a `placeholder.<tld>` URL, tenant has migrated, gateway staged behind
  a custom domain). Documented in `docs/CONFIGURATION.md`. Per-module-id
  generalization (`VCT_<MODULE_ID>_PULL_TOKEN_ENDPOINT`) deferred to
  v0.2.46-46-2; the V45-D shape is intentionally module-id-flavoured so
  the upgrade is backwards-compatible. (V45-D)

- **`WaitForBinaryRefresh` test harness**: parameterized timeout, poll
  interval, and git-pull-skip flag for deterministic poll-loop tests.
  5 async/sync tests covering immediate-success, timeout, mid-poll
  appearance, missing-source-version, and the production 5-min default.
  (V45-B)

- **One-shot `backfill_partial_container_start_failures` Db method**:
  idempotent UPDATE on `module_installs` that flips pre-v0.2.45 rows
  matching the partial-failure pattern (`status='installed' +
  last_error LIKE '%container%' + container_name IS NULL`) to
  `status='error'`. Wired into launcher startup. (V45-E)

- **`scripts/v0245-pre-ship-check.sh`**: cloned from v0244 with v0.2.45
  version assertions, V45-specific test gates (`test_v0245_*` Python
  tests + `cargo test --lib test_v0245`), and the `[Unreleased]`-empty
  CHANGELOG assertion. Same 17+ PASS / 0 FAIL contract.

- **launcher (UI branch `feat/launcher-logo-circular-white`)**:
  launcher UI polish + brand identity refresh. Robot logo redesigned as
  robot-in-circle (white circular backdrop so the icon stays legible on
  both dark + light Windows taskbar themes); ROBOT_ZOOM=1.70,
  OFFSET_Y=-0.10 for visual center. Two distinct logo treatments now:
  (a) UI-side `static/logo.png` keeps the **full robot** and is rendered
  at 80px in the new R. sidebar brand footer; (b) OS-level icons
  (`icon.ico`, `icon.icns`, `Square*Logo.png`) use a **head-only** variant
  — for the small `.ico` slots (16/24/32/48) the head is drawn
  pixel-perfect (no anti-aliasing, hand-snapped to the pixel grid for
  taskbar legibility); for ≥64 the head is rendered via Playwright/
  Chromium from a head-only SVG. `icon.ico` is built byte-by-byte with
  per-size PNG payloads to bypass Pillow's `append_images=` ICO bug that
  collapses multi-size into a single entry.

- **launcher (UI polish)**: collapsible sidebar groups. Each group header
  (Workspace / Knowledge / Team / System / Module configuration / Admin)
  is now a button that toggles its items' visibility with an animated
  chevron. Collapsed state persists to `localStorage` under
  `vct.sidebar.collapsedGroups` (per-user UI preference; no
  `launcher.db` schema migration needed). All groups default to
  expanded; the gap between groups tightens when collapsed.

- **launcher (UI polish)**: brand footer in `RightSidebar.svelte`. A
  `margin-top:auto` block at the bottom of the right sidebar with a
  divider line, the 80 px robot logo, "VCT Launcher" label, and the
  `getVersion()`-fetched version string. Replaces (a) the old
  menubar-side text+logo block (which visually duplicated the Windows
  titlebar) and (b) the StatusBar version display (single source of
  truth now). MenuBar logo + text removed; StatusBar shows only
  realtime telemetry ("Connected · N apps activated").

- **launcher (UI polish)**: window state plugin + maximized first launch.
  Added `tauri-plugin-window-state` (Cargo + lib.rs + capabilities
  permission) so the launcher remembers size / position / maximized
  state across launches. Combined with `"maximized": true` in
  `tauri.conf.json windows[0]`, the first launch opens full-screen and
  every subsequent launch reuses whatever shape the user left it in
  (Slack / Discord / VS Code style). Per-user state file under the
  platform-conventional app-config dir.

### Changed

- **Version pins bumped 0.2.44 → 0.2.45** across the canonical sites:
  `pyproject.toml`, `vct-module.json`, `launcher/package.json`,
  `launcher/package-lock.json`, `launcher/src-tauri/Cargo.toml`,
  `launcher/src-tauri/tauri.conf.json`,
  `launcher/src-tauri/vct-hub/Cargo.toml`,
  `launcher/src-tauri/vct-launcher-core/Cargo.toml`, and the three
  matching entries in `launcher/src-tauri/Cargo.lock`. Dist-binary
  metadata (`launcher/dist/<arch>/*.metadata.json`) and
  `state/install-manifest.json` are NOT bumped here — they are written
  by the Release workflow on the binary build and by `install.py` at
  install time, respectively.

### Documentation

- New KG node
  `knowledge/concepts/paid-module-install-update-foundation-2026-06-02.md`
  documenting the 4 paid-module foundation contracts installed by V45-C
  through V45-F, the 3 root causes addressed in v0.2.45, the v0.2.46+
  follow-up backlog, and cross-references to
  `[[v0244-shipped-v0245-hotfix-rationale-2026-06-02]]`. (V45-G)

### Verified

- `[Unreleased]` block is EMPTY at tag time (no-deferred-fixes rule from
  the post-v0.2.41 retrospective).
- `tests/test_v0245_self_relaunch_under_venv.py` (V45-A) passes on
  integrated `main`.
- 26 `cargo test --lib test_v0245*` Rust tests across
  `installer_engine.rs` (V45-D, 11 tests), `installer.rs` (V45-B, 5
  tests), `modules.rs` (V45-C, 7 tests), `module_catalog_client.rs`
  (V45-F, 2 tests) pass on integrated `main`. V45-E ships 4 additional
  `v0245_backfill_*` tests in `vct-launcher-core/src/db/modules.rs`.
- `bash scripts/v0245-pre-ship-check.sh` reports zero FAIL on a clean
  fixture.

## [0.2.44] - 2026-06-01

### Fixed
- **Orchestrator-root shared-KG re-seeding loop ENDED** (4-release recurring
  bug): install.py now detects orchestrator-root as a category (not via
  string-equality of collection names) and rebinds both `project_kg_bindings`
  rows + both env keys to a single canonical collection name on every
  `--update`. SHARED_KG_COLLECTION wins as canonical. No re-embedding, no
  re-syncing, no destructive operations on existing collections. (V44-A)
- **Stale KG row prune no longer gated on full-sync**: `_prune_stale_kg_rows`
  now runs on every `--update` regardless of CI-10 partial-vs-full sync state.
  Multi-strategy path matching handles symlinked install dirs, absolute
  paths, and `.claude/worktrees/agent-*/` prefixes. (V44-A)
- **Structural-row guard**: orchestrator-root's WRITE access to its primary
  collection cannot be revoked via the access-matrix UI (was implicitly safe;
  now defensively asserted with a clear error message). Other access-matrix
  mutations unchanged. (V44-C)
- **Lint-contract conformance**: launcher_db_reader.py delegates path
  resolution to vco_lib.paths.launcher_db_path() (test_vct_root_dir_consolidation);
  _rebind_orchestrator_root_to_canonical routes env-surface writes through
  vco_lib.config_projection.apply_project_env() (test_config_projection_single_writer).
  (V44-E)
- **Hardening from adversarial post-merge review (V44-F)**:
  1. `_path_resolves_on_disk` strategy 1 now wraps `Path.exists()` in
     try/except — prevents prune-loop crash on NUL/invalid-char file_paths.
  2. `_rebind_orchestrator_root_to_canonical` errors propagate into
     `seed_errors` so audit-log "warn" entries fire on rebind failures
     (was silently absorbed into print-only).
  3. New informational notice when an orphan collection retains rows
     after the canonical rebind — tells user where the old data lives
     and how to drop it manually (non-destructive).
  4. Env-vs-DB disagreement now warns loudly during install (rather than
     silently letting env win when set).
- **V44-I closes V44-H's deferred known-issues**:
  - **Parallel `install.py --update` concurrency lock**: cross-OS advisory
    lockfile under `~/.vct/install.py.lock` (fcntl on POSIX, msvcrt.locking
    on Windows) acquired around the install-state mutation block. Prevents
    split-brain `.claude/settings.json` ↔ `.claude/env` writes when two
    install processes race. 60s acquisition timeout with exponential backoff;
    soft-fails to "proceeding without concurrency protection" WARN if the
    lock file can't be created.
  - **Dual-clone uid check**: `_check_dual_clone` now skips silently when
    `~/.vct/launcher.db` is owned by a different uid than the current
    process (POSIX) or write-inaccessible (Windows). Eliminates the
    spurious dual-clone WARN previously observed in sudo / docker exec
    contexts where `HOME` resolves to a foreign user's homedir.
- **MCP shared-write gate semantic fix (V44-F)**: store_knowledge_node's
  SHARED_KG_WRITE_DISABLED gate now fires on `scope == "shared"` rather
  than on `target_collection_name == SHARED_KG_COLLECTION`. Post-v0.2.44
  rebind makes KG == SHARED for orchestrator-root, which would otherwise
  block legitimate scope='project' writes when the gate is on. Found by
  v0.2.44 RL adversarial review.
- **V44-G3 lint-contract robustness against local dev-only artefacts**:
  `tests/test_vct_root_dir_consolidation.py::test_no_inline_reconstructions_outside_paths_module`
  now excludes two path classes that trigger false positives only on
  local developer checkouts (not on CI / clean clones):
  (a) `.claude/worktrees/agent-*/` — transient git-worktree mirrors
  spawned by the multi-agent harness containing full copies of production
  code (incl. `install.py` and `vco_lib/paths.py`) that look like
  violations but are scratch dirs; the canonical files are already covered
  by the main repo_root scan + `allowed` set.
  (b) `.claude/scripts/` — gitignored bundle copies of
  `templates/scripts/` scripts. The canonical template
  `templates/scripts/generate-kg-summary.py` is already allow-listed (with
  documented rationale for its inline construction — it can't import
  `vco_lib` because the template is reused inside per-project installs
  that don't ship `vco_lib` on PYTHONPATH). Flagging the bundle copy is
  double-counting. CI runs were unaffected (clean clones don't carry
  either artefact); local dev runs of the lint test now match CI behaviour.
  (V44-G3)

### Added
- **V44-G4 auto-retry policy for failed paid-module installs**:
  `module_installs` rows in `error` or `broken` status are now auto-retried
  when either (a) the user clicks "Update" on a project, or (b) the user
  clicks "Update orchestrator" (gated by the
  `auto_retry_failed_paid_module_installs_on_orchestrator_update` setting,
  default `true`). Per-row decision tree: self-heals when a healthy
  container already exists; skips unlicensed or version-incompatible modules
  with a clear audit-log entry; re-invokes `install_module_for_project` for
  the rest (Trigger A only — Trigger B is restart-bound and defers actual
  installs to the next launcher boot's resume sweep). Closes the
  v0.2.34→v0.2.42 W8 pull-token gateway recovery gap surfaced by the RL module owner
  2026-06-01. New helper `module_service::retry_failed_module_installs` +
  Tauri commands `get_auto_retry_failed_installs_setting` /
  `set_auto_retry_failed_installs_setting` for the Settings page toggle.
- `vco_lib/launcher_db_reader.py`: tiny read-only helper exposing
  `get_orchestrator_root_project_id()`, `get_kg_binding(role)`, and
  `get_orchestrator_root_bindings()`. Soft-fails on missing/corrupt DB.
  install.py now reads launcher.db bindings (the SoT) instead of env vars
  (a projection) for orchestrator-root canonical-collection resolution. (V44-B)
- `install.py::_rebind_orchestrator_root_to_canonical()`: writes canonical
  collection name into launcher.db `project_kg_bindings` (both roles) +
  `.claude/settings.json` env block + `.claude/env` managed block.
  Soft-fails on any error; collected errors logged but never raise. (V44-A)
- **V44-G2 dual-clone WARNING**: when launcher.db's orchestrator-root
  project_id points at a folder different from PROJECT_ROOT, install.py
  prints a clear WARNING explaining the situation but proceeds. User
  is expected to have one VCO root project.

### Changed
- `claude_mcp_servers/weaviate_mcp/server.py`: docstring example updated
  to reflect post-rebind state where orchestrator-root KG_COLLECTION ==
  SHARED_KG_COLLECTION. (V44-C)
- `claude_mcp_servers/scripts/repair_kg_typed_links.py`: usage-string
  example no longer uses an internal project name. (V44-C)
- **V44-G1 hybrid SoT resolution**: install.py now resolves env-vs-DB
  KG collection disagreement by querying Weaviate first (pick the
  collection that actually exists), falling back to "env wins on
  first install / DB wins on subsequent update" heuristic. Replaces
  V44-F's silent-env-wins behavior.
- **V44-G5 developer-script untracking + lint-test tool_backups exclusion**:
  `scripts/migrate-shared-secrets.py` is now gitignored (was an untracked
  developer-local one-off helper that accidentally got committed via V44-G5's
  initial commit and immediately reverted). The lint test
  `tests/test_vct_root_dir_consolidation.py` now also excludes
  `.claude/state/tool_backups/` (Claude Code's pre-edit snapshots) from the
  inline-pattern scan; pre-edit snapshots by definition contain the file's
  pre-refactor state, which is a false positive.
- **V44-H pre-tag hardening (5+2+1 multi-Opus review)**:
  1. `_resolve_orchestrator_root_canonical`: Weaviate-unreachable for ALL
     candidates now returns a `deferred:` rationale. The caller in
     `_seed_weaviate_shared_kg_only` honors the prefix and skips the rebind
     so a transient Weaviate outage cannot clobber last-known-good bindings.
  2. Orphan-collection notice now compares against the PRE-resolution KG
     name (new `orphan_candidate_kg` kwarg) so the warning still fires
     after V44-G1 reassigns `current_kg_collection`.
  3. `_path_resolves_on_disk` strategies 1, 2, 4 now require the resolved
     path to lie inside `PROJECT_ROOT` — stored values containing `..`
     can no longer keep a corrupted KG row alive forever.
  4. `_resolve_orchestrator_root_canonical` short-circuits when ALL four
     inputs are empty with a bootstrap rationale (no empty-string
     propagation into downstream GraphQL).
  5. Dual-clone detection now also SKIPS `_rebind_orchestrator_root_to_canonical`
     via a module-level flag, with a `(skipped — ...)` error tail that
     the V44-F deferral discriminator recognizes. Prevents the rebind
     UPDATEs from corrupting the OTHER registered clone's bindings.
  6. Dual-clone detection also writes a `warn`-level audit_log entry
     (was print-only).
  7. Removed misleading `"(SHARED_KG_COLLECTION wins)"` tail from the
     `→ canonical = ...` print — V44-G1's rationale line printed just
     above already names the actual decision source.

### Documentation
- New KG node `knowledge/concepts/orchestrator-root-kg-collection-identity-2026-06-01.md`
  documenting the new contract + 4-release history. (V44-D)

### Added

- **launcher (UI branch `feat/orchestrator-update-progress-modal`)**: full-screen blocking progress overlay during orchestrator self-update. Subscribes to `$orchestrator.progress` (already populated by the existing `install_progress` Tauri listener at `stores/orchestrator.ts:108`, no duplicate channel listener). Reuses `cgr-progress-fill` 4 px-track style from `CodeGraphReanalysisModal.svelte` for the bar; adds a robot-logo "fill" animation above the bar (two stacked `<img>` from `/logo.png` with `clip-path: inset(N% 0 0 0)` rising as percentage grows — clip-path-mask fill pattern). Covers all three updater flows: `runUpdate` (remote-ahead), `applyPendingInstall` (install-stale), `runRestart` (binary-stale). Held at 100 % for 1.8 s + 400 ms fade-out on success; sticky on error with a `Dismiss` button that clears `updater.error` and closes the overlay. Modal mounted in `+layout.svelte` (pattern mirrors `InstallWizard` / `LicenseManagerModal`), gated by new flag `showOrchestratorUpdateProgress` in `stores/ui.ts` + `openOrchestratorUpdateProgress()` / `closeOrchestratorUpdateProgress()` actions. Flag name reserved by A3 collision audit (2026-05-30) to avoid clashing with `showLicenseManager`. UpdateBadge.svelte now calls the open action right before invoking any updater action so the overlay covers the full lifecycle even after the badge popover closes. Cross-OS automatic (DOM + CSS in Tauri WebView).

## [0.2.43] — 2026-05-31

20 items across 4 worktree-isolated agent branches (A install.py / B launcher Rust / C Weaviate cleanup / D CI + housekeeping). Headline themes: (a) orchestrator-root install hardening — manifest refresh, portability env keys written pre-launcher-boot, stale KG row pruning, hub probe URL fixed after multi-tag regression; (b) KG schema migration safety — additive named-vector UNION strategy ensures pre-v0.2.18 collections gain all 5 slots without re-embedding, plus lowercase-collection cleanup deferrals; (c) launcher resilience — license_keys legacy-file self-heal, container_pull broken-detection, post-install MCP tool defaults assertion; (d) release pipeline — Gate 3 closes the v0.2.42 retag class where launcher/dist/ could silently regress under a new tag.

### Added

- **V0243-3 (license_keys startup self-heal)**: if `license_keys` count=0 AND `~/.vct/license.key` exists non-empty, launcher startup now backfills the `__orchestrator__` row and writes the key to the OS keychain (`source=legacy_backfill_v0243`). Called from `lib.rs::setup()` after the module reconciler; hermetic 3-test suite covers empty-table insertion, no-op when table already populated, and no-op when file is absent.
- **V0243-5-Rust (KG_BASE_DIR in Rust env writer)**: added `KG_BASE_DIR` to `CANONICAL_INSTALL_ENV_KEYS` and the `write_project_env_files` match arm (value = project folder). Propagates to both `.claude/env` and `.claude/settings.json::env`, mirroring the value `build_kg_sync_env` already passed to kg-sync subprocesses. Updated `install_match_arms_cover_every_canonical_key` test to include the new key.
- **V0243-8 (restart: stub-protect UPDATE_DEFERRED.md)**: `clear_restart_deferral` now preserves files whose YAML frontmatter contains `stub: true` even when no real entries remain after stripping. Writes the stripped content back (so the `launcher_restart_required` section is removed) but does NOT unlink the file. Added `frontmatter_has_stub_flag` helper + 4 unit tests (stub preserved, stub stripped of section, non-stub deleted, frontmatter-false-positive check).
- **V0243-12 (module_reconciler container_pull broken detection)**: `reconcile_installed_modules` now also flips status→`broken` for `container_pull` rows where `container_name IS NULL` AND `last_error` matches pull-failure patterns (`pull fail`, `unauthorized`, `retrieve auth`). Uses a lightweight JSON parse of the on-disk manifest to detect `install.method=container_pull` without deserializing the full manifest. 2 new tests.
- **V0243-15 (update_orchestrator audit_log coverage)**: `update_orchestrator` writes `update_orchestrator_start` (old_version, source_commit, branch, install_path) at entry and `update_orchestrator_complete` (success, duration_ms, new_sha, branch) at the success exit and the "already up to date" exit. Best-effort via `app.try_state::<Db>()`.
- **V0243-17 (module_mcp_tool_defaults post-install assertion)**: after `reconcile_module_tool_allowlist` on both install and update paths, if the manifest declares `mcp_registration.tool_allowlist` (non-empty), assert `>= 1` row exists in `module_mcp_tool_defaults` and forensic-log (+ `audit_log` entry) when zero rows found. Never blocks install; soft-fail throughout.

### Fixed

- **V0243-0 (orchestrator-root shared-KG seed skip)**: `_seed_weaviate_shared_kg_only` no longer runs a redundant double-sync when the per-project KG and shared KG point at the same collection on an orchestrator-root install. New `_is_orchestrator_root_install()` helper detects the orchestrator clone (vct-module.json + install.py + vco_lib/ present). Upserts `app_state.last_installed_shared_kg_collection` on skip. 2 unit tests.
- **V0243-1 (hub health probe URL)**: `_probe_vct_hub_health` was hitting `/health` which 404s on every vct-hub version since v0.2.21. Corrected to `/api/v1/health` (the auth-exempt liveness endpoint confirmed in `auth.rs`). On successful probe, also unlinks any stale `~/.vct/v0.2.21-cutover.flag` left by an interrupted v0.2.21 install. CLAUDE.md doc updated to match.
- **V0243-2 (BLOCKER — kg_named_vector_slots migration)**: `_migrate_kg_named_vector_slots` added to install.py step 7d/10. Ensures `$KG_COLLECTION` and `$DEVELOPMENT_COLLECTION` carry all 5 named-vector slots from the v0.2.18 catalog (qwen3_embed + ollama_embed + openai_embed + arctic2_embed + openai_text_embed). Collections created before v0.2.18 only had 3 slots; the new slots are added additively (UNION strategy — no re-embedding of existing data). Idempotent; soft-fails with deferral entries on per-slot errors. 7 unit + integration tests (`tests/test_v0243_kg_named_vector_migration.py`).
- **V0243-4 (.vco-manifest.json refresh)**: After `_materialize_orchestrator_self_claude_dir` copies templates, `_refresh_orchestrator_self_vco_manifest` rewrites `.claude/.vco-manifest.json` with current SHA256 hashes of all installed hooks/scripts/settings.json, bumps `vco_version` to HEAD SHA, and preserves the existing `preserved_files` section. Atomic tempfile+replace write. Only fires on orchestrator-root installs.
- **V0243-5 (emit orchestrator-root portability env keys)**: `_emit_orchestrator_root_env_keys` writes `VCT_ORCHESTRATOR_ROOT`, `VCT_INFRASTRUCTURE_DIR`, and `KG_BASE_DIR` directly to `.claude/env` managed block on every orchestrator-root install, bypassing the launcher.db requirement. Closes the gap where fresh installs (pre-launcher-boot) had no portability keys until the first GUI launch.
- **V0243-6 (stale KG row pruning)**: `_prune_stale_kg_rows` runs at end of full-sync branch — queries Weaviate for all stored `file_path` values, set-diffs against on-disk `knowledge/**/*.md`, batch-deletes orphans via `DELETE /v1/batch/objects`. Defaults to dry_run=True on non-orchestrator-root installs; dry_run=False on orchestrator-root. First run prints stale count before deleting.
- **V0243-9 (kg_collection_access parity self-heal)**: Step 4 added inside `_self_heal_kg_bindings_on_update`: for every `project_kg_bindings` row, INSERT-OR-IGNORE the canonical access level into `kg_collection_access` (write for primary, read for shared/archive). Also backfills the sibling `_Development` collection row for primary bindings. INSERT-OR-IGNORE never lowers existing privilege.
- **V0243-10 (full-sync nodes_synced stat)**: On the full-sync branch of `_seed_weaviate`, `_nodes_synced` is now set to `len(_compute_on_disk_content_hashes(knowledge_root))` BEFORE subprocess.run so `last_kg_sync_stats` records the correct count instead of 0/0 even on non-zero exit.
- **V0243-11 (npm version check uses local ls not registry)**: `_query_installed_npm_version` replaced `npm view -g <pkg> version` (registry query, returns latest published version) with `npm ls -g --json --depth=0 <pkg>` + `dependencies[pkg].version` (offline, returns actually-installed version). Mirrors the existing `_installed_npm_integrity` pattern.
- **V0243-13 (deferred entries for schema drift)**: On `--update` runs on orchestrator-root installs, `_emit_orchestrator_root_schema_deferrals` emits info-severity deferred entries for: (a) `VibeCodedOrchestrator_Development` with 0 rows (safe to DROP, needs consent); (b) KG collections carrying stale `linksTo` property without `typed_links` (semantic_graph_search wiki-link resolution broken).
- **V0243-14 (P2 — lowercase code-graph cleanup deferrals)**: `_emit_lowercase_codegraph_cleanup_deferrals` added to step 7d/10. On each install/update, detects Weaviate code-graph collections whose name is an all-lowercase form of a canonical-cased sibling (e.g. `myproject_codefunction` alongside `MyProject_CodeFunction`) with < 100 rows (clearly residual). Emits `UPDATE_DEFERRED.md` entries per qualifying collection with an explicit drop command. Never auto-drops; idempotent.
- **V0243-18 (P3 — empty Diagrams collection warning)**: `_warn_if_diagrams_empty` added to `_ensure_collections` (step 7b/10). After the Diagrams collection is bootstrapped, checks for `.mmd` / `.excalidraw` sources under `.claude/diagrams/` and queries the Weaviate object count. When both are zero, prints a clear `WARN` message so users understand why `describe_excalidraw` and diagram-search skills return nothing.
- **CI-11 / V0243-7 (dist-binary freshness gate)**: added Gate 3 to `pre-release-gate` in `release.yml`. On a tag push, asserts that `launcher/dist/` changed since the previous tag. Catches the v0.2.42 regression class where a hotfix retag skipped `commit-dist-binaries` (PAT 403), leaving main with the v0.2.41 binary under a v0.2.42 tag. Gate skips gracefully (notice, not error) when no previous tag exists (first-ever release) or when triggered as `workflow_dispatch`. ~35 LoC added to `release.yml`.

## [0.2.42] — 2026-05-31

47 commits across 11 worktree-isolated agent branches (W1-W8 fanout + D1-D3 deferral closers) plus a cargo-warning cleanup. Headline themes: (a) v0.2.40's silent-contamination guards are now AIRTIGHT (RT-1 closes the RLClient singleton hole F1 left open), (b) the long-broken `Installer Smoke Test` workflow turns green for the first time since v0.2.38 (W1 + W7), (c) launcher-side prep for the next vct-rl-reranker container iteration (L1.M multi-key licensing GUI gating, R3-R5 weights pipeline polish, W8 pull-token gateway end-to-end working), (d) **new discipline**: nothing deferred to v0.2.43. Every audit finding closed in-tag.

### Fixed

- **CI-1 (installer-smoke F1 assertions)** (W1): re-targeted from install-root `.env` to per-project `.claude/env`. `VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR` are deliberately excluded from `.env` per `vco_lib/env_template.py:71-91`; the correct assertion target is `$PROJECT_DIR/.claude/env` written by `--write-env`. Both Job 1 (ubuntu) and Job 4 (windows/macos) updated.
- **CI-2 (pyyaml in installer-smoke)** (W1): replaced `pip install -e ".[dev]"` with `pip install -e ".[mcp]"` at 3 sites; removed `|| true` swallows so packaging regressions surface; added `client.close()` before `sys.exit(1)` in the weaviate-bootstrap step to suppress Con004 ResourceWarning.
- **CI-3 (windows install-smoke timeout + pip cache)** (W1): added `actions/cache@v4` for pip across all three installer-smoke jobs; bumped `install-smoke-no-container` `timeout-minutes` from 20 to 30.
- **CI-3-nohub (install.py --no-hub flag)** (W1): added `--no-hub` flag to `install.py` argparse; when set, Step 8 (vct-hub deployment + start) is skipped with a descriptive note referencing the session-start-ensure-hub auto-start fallback. Passed `--no-hub` in the installer-smoke no-container job (windows-latest + macos-latest).
- **CI-5 (Node 20 deprecation)** (W1): bumped `actions/checkout@v4` → `@v6` and `actions/setup-python@v5` → `@v6` at all 4+3 sites in `installer-smoke.yml`.
- **CI-6 (release pipeline hard-gate)** (W7): added `pre-release-gate` job to `release.yml` that runs `installer-smoke` + `manifest-validate` BEFORE `build`. If either gate is red → `build` never starts → no binaries published. Closes the v0.2.40/v0.2.41 footgun where binaries shipped while smoke was red. ~100 LoC added.
- **CI-7 (release commit triggers duplicate CI)** (W1): added `launcher/dist/**` to `ci.yml` `paths-ignore` (push + pull_request) so the release binary-refresh commit no longer triggers the full CI matrix.
- **CI-8 (step22 ubuntu cargo build failure)** (W7): root cause was `vct-hub`'s `keyring` dep linking against `libdbus-1` via `sync-secret-service` on Linux. Added `apt install libdbus-1-dev pkg-config` step gated on `runner.os == 'Linux'` before `cargo build`. Also added `Report matrix cell outcome` step so multi-cell failures name which OS/runtime cell failed.
- **CI-9 (manifest-validate unbounded build)** (W1): added `timeout-minutes: 12` to `manifest-validate.yml` `validate-manifests` job to prevent runaway Rust compilations from burning unbounded CI minutes.
- **CI-10 (content-hash diff-only sync gate on `install.py --update`)** (W3): `_seed_weaviate` no longer runs `kg-sync --all` unconditionally on `--update`. Reads `last_installed_{active_embedding,kg_collection,shared_kg_collection}` from `launcher.db app_state`. Context change (model swap / collection rename) → full sync. Otherwise: computes per-file `content_hash`, batch-queries Weaviate for stored hashes, computes diff, syncs ONLY changed files. Empty diff → skip entirely. `sync_knowledge_graph.py` extended to accept file-path positional args. "Pay once, never again." 10 new unit tests.
- **RT-1 (RLClient singleton freeze)** (W3): `_get_rl_client()` in `claude_mcp_servers/weaviate_mcp/server.py` was a bare None singleton that froze `ACTIVE_EMBEDDING` at first call. Mid-session `ACTIVE_EMBEDDING` flip would still send the OLD value in `/rl_update` payloads → arctic2-sourced signals could silently train the qwen3-tagged NN. Now keyed by `os.getenv("ACTIVE_EMBEDDING")` so a flip produces a fresh `RLClient` with correct tag. Completes v0.2.40 F1 (which only fixed `client.py`'s constructor, not the orchestrator-side singleton).
- **RT-2 (atomic tier_cache RMW)** (W2): introduced `Db::with_tier_cache_mut<F>` in `vct-launcher-core/src/db/tier.rs` — single-lock helper that reads, runs closure, validates tier, writes back atomically. Replaced 4 torn-write call sites in `licensing.rs`. Prevents torn writes when timer-driven `license_refresh` and user-initiated `validate_module_license` race.
- **RT-3 (`unsupported_embedding_source` error UX + 24h cooldown)** (W3): `fetch_signed_download_url` in `commands/module_default_weights.rs` now parses Supabase 400 `{error:"unsupported_embedding_source", supported_embedding_sources:[...]}` into a typed error. GUI surfaces the supported list. R5 daily poll: 24h cooldown gate on `weights_download_last_failed_at` timestamp prevents spam-retry until a sane backoff has elapsed.
- **RT-4 (Reset to global weights Tauri command + GUI button)** (W3 + D3): registered `module_reset_weights_to_global` in `lib.rs`; derives `embedding_source` + `version` from `module_settings` keys written by each successful download. Svelte button on RL Reranker module config tab (D3 fixed 4 functional defects from W6's initial wiring: wrong command name, missing `module_id` param, no success toast, no visibility gate when `weights_last_version` is absent). Button gated by `moduleIsActive`.
- **RT-5 (`.vct-managed` marker for Windows fs::copy fallback)** (W3): on Windows without Developer Mode, the symlink fallback path writes a sibling `.vct-managed` marker file. Override-protection checks marker before treating a regular `.pt` file as a user override — orchestrator-managed copies are now replaceable on subsequent downloads (was: stuck-on-first-version forever).
- **RT-6 (license re-check inside R5 detached spawn)** (W3): second `is_module_licensed` check inside `tauri::async_runtime::spawn` guards against the race where a user deactivates between install-time license gate and the actual download attempt.
- **RT-7 (`set_module_license_key` SQL-first ordering)** (W2): writes the SQL row with a `(pending)` sentinel prefix BEFORE the OS keychain write. Keychain failure no longer leaves an orphan keychain entry with no traceable SQL row.
- **RT-8 (PRAGMA busy_timeout=5000)** (W4): added to both `Db::open()` (vct-launcher-core) and vct-hub's `open_db()` to match `install.py`'s `sqlite3.connect(..., timeout=5.0)`. Prevents SQLITE_BUSY failures when the launcher and a Python install script contend on the same DB file.
- **RT-9 (`validate_module_license` audit-log ordering)** (W2): `db.audit_log_insert` for the validation attempt is now written BEFORE the `secrets::get` call. Every user click on Re-validate is recorded in the audit trail.
- **RT-10 (orchestrator-slot validate forwards tier_cache.last_error)** (W2): when `module_id == ORCHESTRATOR_MODULE_ID`, the `error` field of `ModuleLicenseValidationResult` now uses `per_row_error.or(tier_cache.last_error)` — admin-mismatch and network errors written by `license_refresh` are forwarded through.
- **RT-11 (`resolve_project_name` hub-failure visibility)** (W4): `vco_lib/paths.py::resolve_project_name` now logs a `WARNING` instead of silently swallowing hub-resolver failures.
- **RT-12 (`clear_module_license_key` SQL-first ordering)** (W2): SQL delete first → keychain delete second. `list_license_keys` hides the entry immediately.
- **RT-13 (W40-adoption smart-path uplift)** (W3): after `_self_heal_kg_bindings_on_update` persists a binding flip, runs `migrate_collections` smart-path against the adopted collection. `noop`/`patch_props`/`copy` applied silently (no re-embed). `rebuild` emits `schema_migration_required` deferral (user opts in via launcher GUI "Migrate" button, NOT auto-applied). Audit-log entry written. 7 new tests.
- **D1 (module deprecation poll)**: wired the long-standing `#[allow(dead_code)]` scaffold `module_update_poll` to a real HTTP poller and 24h cron timer. `spawn_deprecation_poll` runs 30s after launcher boot then every 24h thereafter; it fetches the L0 module catalog (same `module-catalog` Supabase edge function as the Modules page) and for every `status='installed'` (project × module) pair applies the catalog's `deprecated` / `deprecation_message` / `deprecation_eol_date` / `deprecation_migration_url` fields via the existing three-layer soft-fail path. Poll timestamps persisted to `app_state`. Removed `#[allow(dead_code)]`. 3 new unit tests.
- **W8 (pull-token gateway placeholder substitution — closes the GHCR `unauthorized` install error)**: root cause was a cascading placeholder. `request_pull_token` resolved its endpoint as `l0_pull_token_endpoint.unwrap_or(&container.pull_token_endpoint)`; when the L0 catalog cache was stale, the L1 manifest's placeholder `"https://example/pull-token"` was used. DNS lookup of `example` failed, error silently swallowed, fell through to anonymous GHCR pull → ghcr.io 401. Fix: added `RL_ARTIFACT_URL_DEFAULT_ENDPOINT` and `PULL_TOKEN_ENDPOINT_PLACEHOLDER` constants; `resolve_pull_token_endpoint()` substitutes the default when manifest is empty or matches the placeholder. 9 new unit tests. Live `rl-artifact-url` Supabase function + L0 catalog bucket verified healthy — no Supabase redeploy needed. End users no longer need to manually `podman login ghcr.io`.
- **MF-1 (pre-ship-check Gate 16 regex)**: `scripts/v0242-pre-ship-check.sh:245` was checking for `## v0.2.42` heading shape, but CHANGELOG uses Keep-a-Changelog `## [0.2.42] — 2026-05-31`. Widened regex to `^## \[?v?0\.2\.42\]?`.
- **MF-4 (License Manager visibility for broken/error states)**: `launcher/src/lib/module-active-gate.ts::moduleIsInstalled` now returns true for `status='broken'` and `status='error'` in addition to `installed`/`running`/`stopped`. A user with a paid license whose container failed to start still needs access to the License Manager modal to manage / re-validate / remove their key. The license is server-side state; container state is orthogonal.

### Tests

- W5-TEST3: added `cfg(any(test, debug_assertions))` thread-local mock keychain seam in `vct-launcher-core/src/secrets.rs`; un-ignored 2 L1.M keychain tests (`migrate_does_not_error_on_clean_install`, `migrate_full_flow_legacy_to_canonical`) + replaced 1 silent-pass test with 3 hermetic mock-backed siblings (`mock_empty_no_op`, `mock_seeded_migrate_succeeds`, `mock_idempotent_double_call`). All formerly-ignored tests now run on CI without D-Bus.
- **TEST-1**: Fixed 8 `test_rebuild_diagram_index_cli.py` failures on dev boxes with a populated `~/.vct/launcher.db`. Added `autouse` fixture that sets `VCT_STATE_DIR` to a per-test tmp dir and seeds a minimal `projects` + `project_diagrams` schema with a `demo-project` row so the FK constraint in `_upsert_row` is satisfiable without touching the production DB.
- **TEST-2**: Converted 31 silent `eprintln!("skipping...") + return` patterns to `#[ignore = "..."]` in `vct-hub/src/cli_api.rs` (11 Weaviate integration tests), `vct-hub/src/modules_api.rs` (8 keychain tests), `src/commands/secrets_cmd.rs` (8 keychain tests), `src/commands/dashboard.rs` (2 keychain tests). Tests now appear as `ignored` in `cargo test` output instead of silently passing.
- **TEST-4**: `_weaviate_reachable()` in `tests/test_vco_lib_migrate.py` now checks `WEAVIATE_AVAILABLE` (guarded by `try: import weaviate` / `except ImportError`) before probing the network. Prevents `ImportError` from turning the `LiveMigrateIntegrationTest` class into a collection failure on envs without the weaviate client installed.
- **TEST-5**: Deleted dead 129-line `BackfillCodeGraphProjectEnvTests` class (Phase 0.B Part 2, 2026-05-25 — legacy add-only-missing-keys contract replaced by `apply_project_env`). Added 2-test `LegacyBackfillRemovedTest` sentinel asserting the deprecated code path doesn't regress.
- **TEST-6**: Converted 14 `pytest.skip` calls to `pytest.fail` for in-repo shipped assets (hook dirs/files, `_normalize_typed_links`, analyzer module, `weaviate_mcp.server`). Missing shipped files in CI are regressions, not legitimate skips. Net: 9 fewer skips, 0 new failures.
- **TEST-7**: Moved 3 `daemon_usable_probe_*` tests in `vct-launcher-core/src/services/runtime.rs` from `#[ignore]` to `#[serial]` (new `serial_test = "3"` dev-dep). Tests now run serially on every `cargo test` invocation instead of being silently skipped.
- **D2 (RT-7 failure-path test)**: Un-ignored `set_module_license_key_pending_row_left_when_keychain_fails` in `src/commands/licensing.rs`. Extended `secrets::for_tests` with `fail_next_set(key)` — a one-shot mock failure injector that causes the next `secrets::set` for the named key to return `Err`. Mock seam plumbing changed `mock_set` from `bool` to `Option<Result<(), String>>` so failure propagates through `secrets::set`'s early-return path. 2 new unit tests in `vct-launcher-core::secrets::tests` pin the one-shot and key-scoped semantics. ([D2])
- **TEST-8**: Re-enabled `refresh_project_env_with_db_re_runs_env_writer` (`projects_v2.rs`). Added `python_env_available()` + `has_launcher_db()` helpers to `vct-launcher-core/src/test_env.rs`. Test now skips (eprintln+return) only when `python3 -c "import vco_lib"` fails; uses `with_state_dir` for an on-disk DB the Python subprocess can read.
- **CLEAN-1**: Deleted dead `pub const RL_RERANKER_MODULE_ID` (hub-side, `vct-hub/src/module_supervisor.rs`) — zero callers since v0.2.40 NEW-3.E generalised the gate. Removes the `#[allow(dead_code)]` annotation too (~12 LoC).

### Security

- **SEC-1 (CodeQL config)**: added `.github/codeql-config.yml` to suppress CodeQL alerts on vendored third-party dist bundles (Excalidraw canvas, Mermaid) and false-positive `py/clear-text-logging-sensitive-data` alerts in `config_projection.py` (logs key NAMES, not values). Inline `# codeql[...]` comments added at each suppression site. Full triage in `docs/codeql-triage-v0.2.42.md`. ([W6])
- **SEC-1 (stack-trace-exposure real fix)**: `GET /health` in `code_embedding_service/server.py` now logs the full exception internally and returns a generic error message rather than `str(e)`, avoiding internal path/model info leakage through the API. ([W6])
- **SEC-2 (npm audit)**: ran `npm audit fix --force` in `launcher/`; upgraded `@excalidraw/excalidraw` from 0.17.x to 0.17.6 and `ws` from 8.20.0 to 8.21.0. Zero vulnerabilities after fix. Build + tests verified. ([W6])

### UX

- **UX-1 (paid-modules-agnostic gating)** (W6 + MF-4): introduced `moduleIsActive(moduleId, installed)` and `moduleIsInstalled(moduleId, installed)` pure helpers (`launcher/src/lib/module-active-gate.ts`) as the single gate for all paid-module-specific UI. RL Reranker status panel + RT-4 Reset button visible only when the module container is running; License Manager shows rows for installed modules including `broken`/`error` states (MF-4 fix — paying users with broken containers still need key management). 16 unit tests covering all status transitions.

### Internal / Cleanup

- **CLEAN-1**: deleted dead `pub const RL_RERANKER_MODULE_ID` (hub-side, `vct-hub/src/module_supervisor.rs`) — zero callers since v0.2.40 NEW-3.E generalised the gate. Removes the `#[allow(dead_code)]` annotation too (~12 LoC).
- **Cargo warning cleanup**: silenced 4 pre-existing cargo warnings (3× `unused_mut` in `vct-launcher-core/src/{process,services/runtime}.rs` via `cargo fix`; `module_update_poll` `#[allow(dead_code)]` removed since D1 wires it).
- **Discipline rule** (maintainer-side CLAUDE.md): codified "no release with deferred fixes" rule after v0.2.41 retrospective. Triggered by v0.2.41 shipping with `Installer Smoke Test` still red. Rule: every tag must close ALL known red gates / queued-for-next-release items.

### Release / Ops

- **Pre-ship verification script** (W7): new `scripts/v0242-pre-ship-check.sh` (executable). 18 gates across three sections: local build gates (cargo test --lib, pytest, npm check/test/audit, manifest-validate strict, check-no-secrets, dist binaries present, Cargo.toml version), GitHub CI last-run status gates (5 workflows via `gh run list`), and repo-level checks (allow_auto_merge, CodeQL error alerts = 0, CHANGELOG entry, clean working tree, release.yml has pre-release-gate). Exit 0 = all pass; exit 1 = listed failures. **Run this before every tag push.**
- **Branch-protection apply script** (W7): new `scripts/apply-branch-protection-v0.2.42.sh` (executable). One-shot script the repo owner runs ONCE after tagging to (a) flip `allow_auto_merge=true` at repo level (unblocks 26 stalled Dependabot PRs), and (b) add 8 new required status check contexts to ruleset 15644739 (paid-module manifests strict, hook .sh/.ps1 parity, set -e + pipefail, managed-paths cross-language, launcher binary leak-check, macOS smoke, install.py + install-bundle smoke, Weaviate bootstrap smoke). Excludes Windows install.py smoke until that surface stabilises. Run with `DRY_RUN=1` to preview before applying.

### Ops follow-up (owner-action required, post-tag)

These are one-shot owner actions, NOT v0.2.42 code changes. Done ONCE after the tag lands:

1. **Enable repo-level auto-merge** (closes 26 stalled Dependabot PRs):
   ```bash
   gh api -X PATCH /repos/hotak92/vibecoded-orchestrator -f allow_auto_merge=true
   ```
2. **Apply branch-protection additions**:
   ```bash
   bash scripts/apply-branch-protection-v0.2.42.sh   # or DRY_RUN=1 first
   ```
3. **Verify pre-ship gate is green BEFORE the tag push**:
   ```bash
   bash scripts/v0242-pre-ship-check.sh
   ```

## [0.2.41] — 2026-05-31

### Fixed

- **CI gate hotfix: 3 CI-only failures introduced by v0.2.40 that did not block the Release workflow (binaries shipped) but left `main` red.**
  1. **Rust `migrate_does_not_error_on_clean_install`** (L1.M test) panicked on CI Ubuntu runners with `keyring get: Platform secure storage failure: DBus error: The name org.freedesktop.secrets was not provided by any .service files`. The migration helper `ensure_legacy_orchestrator_row_migrated` calls `secrets::get` in the BRANCH 2 / BRANCH 3 paths exercised by this test, and CI has no D-Bus session for `secret-service`. Test gated with `#[ignore = "touches host OS keychain via secrets::get — opt-in via --ignored"]` matching the sibling `migrate_full_flow_legacy_to_canonical` which uses the same gate for the same reason. The hermetic DB-only sibling tests (`migrate_row_rewrite_preserves_key_prefix`, `migrate_no_op_when_row_already_at_canonical_username`) remain unconditional — they short-circuit BEFORE the `secrets::get` call by seeding a row at the canonical username.
  2. **Frontend `svelte-check` LicenseManagerModal**: 6 errors of `Cannot use 'state' as a store. 'state' needs to be an object with a subscribe method on it.` at `let lastResults = $state<...>` and `let pendingKeys = $state<...>`. svelte-check's parser resolved `$state<Generic>(...)` as an auto-subscribe to a store named `state` because of the local `const state = $derived($moduleLicenseKeys)` at line 29. Renamed the derived alias from `state` → `summary` (8 reference sites updated in script + template, none in CSS). No behavior change.
  3. **Python `test_patch_props_calls_post_property_per_missing_prop`** in `tests/test_vco_lib_migrate.py` failed on CI with `URLError: <urlopen error [Errno 111] Connection refused>` because the fetcher mapping omitted `"Foo_Diagrams"`. With v0.2.34's Diagrams collection added to the migration loop, the missing fetcher entry made `_classify_action` route Diagrams to "create" and the test (which only mocks `_post_property` + `_copy_collection_with_vectors`) reached a real `_create_class` urlopen. Added the missing `"Foo_Diagrams": _at_target_diagrams()` entry so Diagrams classifies as noop, matching every other test in the class.

No runtime behaviour changes. v0.2.40 binaries on GitHub remain canonical; v0.2.41 ships fresh binaries with the same code modulo the 3 test/build-gate fixes above.

## [0.2.40] — 2026-05-30

### Added

- **L1: multi-key licensing (per-paid-module keys)** (Agent L1). Replaces the single-license-key model with per-module keys: each paid module — RL Reranker, MAO, future Specialist Agent Packs, etc. — has its own license key (`license_keys` SQLite table keyed by `module_id`), its own tier projection in `tier_cache.module_licenses`, its own validation cycle, its own keychain entry at `(service='vct.global.licensing', username='license_key__<module_id>')`. The reserved `module_id='__orchestrator__'` slot keeps the v0.2.39 single-key root tier reachable; the migration synthesises that row on first launch from the legacy keychain entry (`VIBECODED_LICENSE_KEY` username), so existing Pro/MAO/Enterprise installs upgrade with zero user action. `tier_cache` stays the EFFECTIVE projection (single-row `id=1` invariant preserved); the new tables hold the SOURCE of raw user-provided keys, decoupling the storage layers without touching every tier_cache reader. New Tauri commands: `list_license_keys`, `get_module_license_key_status`, `set_module_license_key`, `clear_module_license_key`, `validate_module_license`, `list_module_license_validations`. The raw key value never crosses the IPC boundary — `LicenseKeySummary` only carries a 12-char redacted prefix for the GUI's "ends in …" display. Python validator gains a `module_id=` kwarg on `feature_enabled` plus a new `is_module_licensed(module_id)` helper that reads the per-module overlay from `~/.vibecoded/license_cache.json`; `weaviate_mcp/server.py`'s `rl_retrieval` gate now passes `module_id="vct-rl-reranker"` so a user on orchestrator tier=free who activated a paid RL key unlocks reranking. New License Manager Svelte modal (`LicenseManagerModal.svelte`) groups keys by module with input + Save&Validate + Re-validate + Remove buttons; opened via the user-menu's "License Keys" item. The new modal flag is `showLicenseManager` in `stores/ui.ts` (NOT `showLicense` / `showModal` / `showKeyManager`) per the v0.2.40 pre-push collision audit A3 to avoid namespace overlap with the parallel orchestrator-update-progress branch. Soft-fail throughout: per-module validation network failures leave the cached tier in place and surface `stale=true` so the GUI renders a yellow warning instead of dropping the user. Migration 024 (`launcher.db`) adds `license_keys` + `license_key_validations` (append-only audit, capped at 50 most-recent per module). 16 new Python tests in `tests/test_license_per_module_keys.py` (T1 module-row independence, T2 free-tier + per-module key unlocks feature, T3 cross-module isolation + legacy single-key compat + 13 defensive parsing cases) + 8 new Rust tests in `vct-launcher-core/src/db/license_keys.rs` + 5 new Rust tests in `commands/licensing.rs` (independence, overlay-write contract, clear-pathway, display-name fallback, redaction contract, idempotent legacy synthesis).
- **X1: macOS best-effort support (Apple Silicon, no Developer ID)** (Agent X1). Ships `aarch64-apple-darwin` builds with ad-hoc codesigning (`codesign --force --deep --sign -`); the `vct-launcher` and `vct-hub` binaries run after the user bypasses Gatekeeper once on first launch (right-click -> Open in Finder, or `xattr -d com.apple.quarantine`). Intel `x86_64-apple-darwin` NOT supported (GitHub-runner constraint per user 2026-05-30: `macos-13` image deprecated 2025-12-04 — already removed from the release matrix in 2026-05-01; X1 also drops the now-dead `macos-x64` arm of the case statement in the stage-experimental step for clarity). New `docs/macos-install.md` (~196 lines) documents the first-launch Gatekeeper bypass UX, three install methods (right-click, `xattr`, System Settings), auto-start setup via `vct-hub --register-boot` (preferred) or manual plist install (fallback), full troubleshooting matrix (Gatekeeper, hub auto-start, container runtime PATH, GPU passthrough, crashloop), and uninstall recipe. New `templates/scripts/launchctl-plist.template` (~106 lines) provides a manual-install macOS LaunchAgent template using the canonical `__VCT_HUB_BIN__` / `__VCT_STATE_DIR__` placeholder convention (matches `launcher/src-tauri/vct-hub/templates/com.vibecodedtools.vct-hub.plist.template`) for users who can't or don't want to run the launcher-driven `--register-boot` path; XML 1.0-safe (no `--` sequences in comments, ASCII-only). Two new CI gates: (1) ad-hoc codesign step added to `.github/workflows/release.yml` immediately after `Build launcher (bundled script)` on `macos-latest` runs only — signs both `vct-launcher` and `vct-hub` then verifies with `codesign --verify --verbose=2`. (2) new `macos-smoke` job in `.github/workflows/installer-smoke.yml` runs on `macos-latest` (~2-3 min) on push/PR touching macOS surfaces — five assertions: M1 codesign+plutil tooling available, M2 manual-fallback plist parses via `plutil -lint` after placeholder substitution, M3 canonical vct-hub plist parses, M4 ad-hoc codesign+verify round-trip works on a tiny test Mach-O, M5 all paths referenced by `docs/macos-install.md` exist on disk. Release workflow comment block updated to reflect the new ad-hoc-signed posture (was "macOS UNSIGNED"). **Full Developer ID + notarization deferred to follow-up patch** when Apple credentials are configured. Per multi-Opus pre-push review item X1 (reframed 2026-05-30 from "skip" to "best-effort"). If `installer-smoke.yml` is also expanded for cross-OS by a parallel X2 batch, the `macos-smoke` job here is the X1-owned scope (macOS-specific assertions only, no duplication with X2's general cross-OS smoke).
>
### Fixed

- **F6 (cross-OS hardening): WebView2 runtime probe in `install.ps1`** (Agent F6). Tauri launcher GUI requires Microsoft Edge WebView2 Runtime on Windows; without it the launcher opens a black/blank window with no error message — confusing for new users. New `Test-WebView2Installed` + `Install-WebView2Runtime` functions check three known registry locations (`HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`, the same native-arch key, and the per-user HKCU key); if absent, attempt `winget install Microsoft.EdgeWebView2Runtime --silent --accept-package-agreements --accept-source-agreements` (silent in CI / `-Quiet` / `-NonInteractive` mode), else print the canonical Microsoft download URL + offer to continue (non-interactive) or halt gracefully (interactive). Gated on Windows only via `$IsWindows -or $env:OS -eq 'Windows_NT'`; no-op on POSIX (where install.sh handles things and there's no WebView2 dependency). Per multi-Opus pre-push review recommendation #6 (contributor coordination message confirmed the UX gap). 6 new static-scan tests in `tests/test_install_ps1_webview2_probe.py` lock the contract (function presence, registry-GUID coverage, silent-winget flags, URL fallback, Windows gating, non-interactive respect).

- **H1: CLI scripts pass `project=` to ToolUsageLogger** (Agent H1). Three sites in `templates/scripts/search_knowledge.py`, `get_node_info.py`, and `sync_knowledge_graph.py` instantiated `ToolUsageLogger.log_kg_search` / `log_kg_info` / `log_kg_sync` without the `project=` kwarg, so resulting tool_usage.jsonl rows lacked project metadata — every row's `project` field fell back to the logger's historic `"claude-orchestrator"` default regardless of which workspace ran the command. Now each call resolves the canonical project name via the new `vco_lib.paths.resolve_project_name()` helper (hub-first via `project_config.resolve()`, then `CODE_GRAPH_PROJECT` / `PROJECT_NAME` env vars, then `None`) and passes it through `project=`. When resolution yields `None`, the logger's pre-fix default still kicks in — purely additive when no project context exists. The helper deduplicates the resolution pattern that already appears inline in `vco_lib/cli/codegraph_diagram.py` and `templates/scripts/query_code_graph.py`. Templates in `templates/scripts/` are the canonical source for the per-project `.claude/scripts/` copy that `install-bundle --update` propagates. Doesn't affect RL training today (tool_usage.jsonl isn't in the offline corpus) but completes the per-event project-stamping audit consistent with F3 (citation events). 10 new unit tests in `tests/test_kg_cli_project_logger.py` cover the resolver's env-fallback contract (T1a–T1e), the JSONL row contents under each `log_kg_*` call (T2a–T2c), the None → historic default fallback (T2d), and a static contract guard that each of the three CLI scripts both imports `resolve_project_name` AND passes `project=_project` at its `ToolUsageLogger.log_kg_*` call (T3). Per multi-Opus pre-push review item 7.

- **R3: unify weights staging dirs so "Download default weights" actually reaches the running RL container** (Agent R3). Until v0.2.40 the global download path staged to `<vct_root>/modules/<module>/weights/<source>-<version>.pt` but the per-project container bind-mount looked at `<vct_root>/data/<module>/<project_slug>/state/rl_model_<source>_<version>.pt` (declared in the RL Reranker's `runtime.volumes`) — the .pt downloaded successfully but the container never saw it. Strategy A: keep the global download path as the source of truth and surface it into the per-project bind mount via a symlink named `rl_model_<source>_<version>.pt`. The container loads weights through its existing path-resolution code without any container-side change. Override discipline: if the per-project slot already contains a regular file (not a symlink), it's treated as a deliberate user override and NOT clobbered by re-download; symlinks get refreshed freely. Reset path (`reset_weights_to_global`) removes any override and re-creates the symlink. Windows fallback: when `std::os::windows::fs::symlink_file` fails (no elevated privileges / Developer Mode off), falls back to `fs::copy` so the container still gets the weights. The "use global / diverge / reset" UX from the project RL settings page now actually has runtime effect. 6 tests cover the strategy. Per multi-Opus review item 4.

- **R5: first-install auto-download of default weights** (Agent R5). After `start_container_after_install` succeeds for the RL Reranker, automatically triggers a one-shot weights download (detached via `tauri::async_runtime::spawn`) so paid-tier projects don't run on baked-in qwen3-only weights for up to 24h until the user manually clicks "Download default weights" or the daily poll fires. Reuses the existing `module_download_default_weights_inner` (introduced v0.2.33 Agent D for the chained_action dispatcher) — no duplication. Free-tier silently skipped (manifest button is already hidden in free per existing pattern). Soft-fails on Supabase function unavailability (e.g. if R4's `rl-latest-weights` is not yet deployed); sets `weights_download_deferred=true` in `module_settings` so GUI tile shows "click Download default weights to refresh". NEVER fails the install on weights-download failure — container is already running; weights can be downloaded later. Tier-gated via `is_module_licensed(&manifest, &db)` (the same function the install gate at `modules.rs:1341` uses — consistent contract). 5 unit tests for the pure-Db helpers + audit-record contract. Per multi-Opus review item 5; depends on R4 (Supabase function deploy gated on user action).

- **W40-A: `install.py --update` cross-prefix self-heal** (Agent W40-A): when a `project_kg_bindings` row's `collection_name` doesn't exist in Weaviate AND has no case-sibling, the v0.2.23 case-insensitive self-heal explicitly left the row alone (preserving the "genuine missing-class" contract). v0.2.40 adds a SECOND pass to `_self_heal_kg_bindings_on_update`: probe Weaviate for `*_KnowledgeGraph` / `*_Development` classes with non-zero row count via GraphQL Aggregate; auto-adopt when exactly one candidate exists (tagged `manual_override=v0.2.40-prefix-adopt` in `config_json` so downstream env-backfill picks it up on the next `populate()`). Closes the v0.2.29-cleanup stale-shared-binding case where a maintainer-side `*_KnowledgeGraph` (populated, 1033 rows) wasn't reached by the case-insensitive sweep targeting `VibeCodedOrchestrator_KnowledgeGraph` (never created). Multi-candidate ambiguity → emits `multi_candidate_prefix_adopt` deferral entry (warning severity) with per-candidate row counts and copy-paste SQL — refuses to guess. Zero candidates → no-op (legitimate missing-class state, preserved). Transient Weaviate Aggregate errors per-candidate are treated as "unknown count" and skipped (never auto-adopt blindly). Pre-existing config_json keys preserved during adoption; only `manual_override` is set/updated. Idempotent: a second run finds the adopted class in the schema and short-circuits. 11 new tests in `tests/test_self_heal_cross_prefix_adopt.py` (T1–T9 + 2 helper-direct tests) + 2 regression guards in `tests/test_install_self_heal_kg_bindings.py` pin the contract that a pure case-rebind does NOT acquire the v0.2.40 sentinel.

- **W40-B: launcher boot adopts populated Weaviate collections to `project_kg_bindings` rows whose `collection_name` doesn't exist (cross-prefix)** (Agent V40-W40-B): mirrors `install.py --update`'s W40-A self-heal at LAUNCHER BOOT so users who never re-run `install.py` still get fixed at next launcher start. New `Db::adopt_populated_collections_at_boot` in `vct-launcher-core/src/db/access.rs` probes Weaviate via `/v1/schema` + GraphQL Aggregate count for `*_KnowledgeGraph` / `*_Development` classes when a binding row's `collection_name` doesn't exist on-disk and has no case-sibling (NEW-12 / case-insensitive heal owns that path). Auto-adopts on an unambiguous single populated candidate; defers (logs a structured warning naming alternatives) on multi-candidate; no-ops on zero. Adopted rows are tagged `manual_override=v0.2.40-prefix-adopt` in `config_json` so the env-backfill path (`_align_env_with_db_bindings`) trusts the new value next time `populate()` runs. Wired into the boot init block in `lib.rs` immediately after `migrate_legacy_shared_kg_collection_names` (NEW-12 from v0.2.38). Plus: new `should_regenerate_env_for_project` helper in `project_env_settings.rs` (compares max `project_kg_bindings.updated_at` vs env file mtime) drives a per-project env regen at boot via `refresh_project_env_with_db`, so post-adoption env values realign with the DB without manual re-trigger. 11 new tests: 7 in `vct-launcher-core` access tests (existing-collection no-op, project-shape single-candidate adoption, multi-candidate defer, zero-candidate no-op, idempotency after adoption, case-sibling preserved for case-insensitive heal, Weaviate-unreachable returns Err) + 4 in `project_env_settings` tests (binding-newer-than-env true, env-newer-than-binding false, env-file-missing false, no-bindings false). Tests use a 40-line synchronous TCP mock in lieu of an external mock crate.

- **F1 (silent-correctness): `/rl_update` payload now includes `active_embedding`** (Agent F1). Mirror of the existing `cache_nodes` field. Closes a silent-contamination path where training signals from a mismatched embedding source could silently train the wrong neural network (e.g., arctic2 signals corrupting a qwen3-trained NN). Per-Opus-review highest-risk silent-correctness gap #1. The client-side `RLClient.rl_update` payload (in `claude_mcp_servers/rl_client/client.py`) now carries both `embedding_source` and `active_embedding` (both sourced from the constructor-set `self.active_embedding` tag — same pattern as `cache_nodes`). The `RLUpdateRequest` schema (`claude_mcp_servers/rl_client/schemas.py`) declares both fields as Optional (backward-compat) so older servers still parse newer clients; newer servers can gate on the field. Pinned by 4 new tests in `tests/test_telemetry_orchestrator_v0231.py` `RLClientRlUpdateActiveEmbeddingTest` (default qwen3 included; explicit override honored; default-when-unset still present; pre-existing payload fields preserved). ~5 LoC client.py change + 4 tests pinning the contract.

- **F3 (silent-correctness): citation events carry the (project, ACTIVE_EMBEDDING, embedding_source_id) triple** (Agent F3). Closes a silent-correctness gap where the offline RL training pipeline pairs retrieval events with citation events via shared embedding-triple keys; if the retrieval event is dropped at training_loader step 4/6 (filter or alias-map miss), the citation orphans silently with no anchor. Stamping the triple directly on the citation event (mirror of retrieval event shape) makes it self-contained — citation now survives an upstream-event drop. Pure additive payload change; offline trainer ignores extra keys gracefully. Concretely: `RLDataLogger.log_citations` (the local JSONL writer at `claude_mcp_servers/rl_client/rl_logger.py`) now writes `embedding_source` + `embedding_dim` + `embedding_model` on every citation event; `RLTelemetryWriter._build_citation_payload` (the upload-queue payload builder at `claude_mcp_servers/rl_client/telemetry_writer.py`) which previously included only `embedding_source` now includes the full triple. Field names match the retrieval-event shape exactly so the offline trainer's pairing logic works without changes. 6 new unit tests in `tests/test_telemetry_citation_embedding_triple.py` (T1 local-JSONL carries triple, T2 writer-upload-payload carries triple, T3 retrieval/citation field-name parity guard, T4 default qwen3 values, plus blank-default and writer-local-JSONL coverage). Per multi-Opus pre-push review highest-risk gap #3.

- **R1: generalize launcher-side `resume_containers_on_startup`** (Agent R1). W40-D (v0.2.40) generalized the HUB-side resume path; R1 mirrors the change on the LAUNCHER side at `launcher/src-tauri/src/commands/module_service.rs::resume_containers_on_startup`. Removes the hardcoded `RL_RERANKER_MODULE_ID` gate; iterates `Db::list_module_installs_needing_start` and dispatches per-row via the same `(install.method=ContainerPull, runtime.type ∈ {container, service})` gate. NULL-container rows route through `start_container_after_install` (NEW-3.B helpers synthesize defaults); non-NULL rows keep existing probe-and-restart logic with soft-fail per row. New `resume_containers_on_startup_with_resolver` test-friendly variant injects a manifest resolver so unit tests don't need on-disk catalog. 4 new unit tests (NULL container reaches start_after_install + writes last_error on failure; non-NULL preserves container_name without last_error; ContainerPull+CLI runtime skipped; GitClone+service skipped). Closes the launcher-side half of "any future container-distributed module resumes on boot, not just RL". Per multi-Opus pre-push review item 2.

- **R2: hub `/api/v1/projects/{id}/config` exposes 3 RL settings flags** (Agent R2). Until v0.2.40 the GUI's three RL checkboxes (`rl_use_global`, `rl_online_training_disabled`, `rl_global_training_source_flag`) wrote to launcher.db (`module_settings` under `module_id = "vct-rl-reranker"`) via the setter commands in `launcher/src-tauri/src/commands/rl_settings.rs`, but had NO readback path from the RL container — the container never saw them, so flipping a checkbox had zero runtime effect. Now: `ProjectConfigResponse` in `launcher/src-tauri/vct-hub/src/config_api.rs` exposes all 3 fields; the resolver reads them with the same `get_setting().unwrap_or(false)` pattern the launcher-side getter (`get_bool_flag`) uses, so the hub-served default matches the launcher-internal default exactly. The Python `ProjectConfig` dataclass in `vco_lib/project_config.py` gains the matching `bool` fields, defaulting to `False` so pre-v0.2.40 hubs paired with v0.2.40+ clients don't crash (additive field — `schema_version` stays at 1). Fields are wired through the existing `?key=` single-field filter via serde reflection (no per-field dispatch). The RL container source lives in the paid-modules tree (not in this clone); the container-side reader that fetches and respects the new fields is tracked by a `TODO(paid-modules, v0.2.40 R2)` marker in `launcher/src-tauri/src/commands/module_service.rs::start_container_after_install` — closing that loop requires a container restart OR a `/reload_settings` endpoint on the RL service (see `discovery-A2-dashboard-widget-archaeology.md` F9 for the verification recipe). 4 new tests: 3 in `vct-hub/src/config_api.rs::tests` (defaults branch with empty `module_settings`; set-then-fetch round-trip pinning each canonical key string; `?key=rl_use_global` single-field filter) + 1 new Python test class `ResolveRlFlagsTest` in `tests/test_project_config.py` (3 cases: hub-emits-explicit, hub-omits-back-fills-False, non-bool-truthy-coercion). Per multi-Opus pre-push review item 3 (`.claude/context/reviews/v0240-pre-push-2026-05-30/00-synthesis.md`).

- **H2: rewire `RlRerankerDashboardWidget` (orphan since v0.2.31)** (Agent H2). Passive read-only widget displays RL module state (weights version + last training timestamp, read live from the hub via `module_db_read_row`, plus the three per-project settings flags `rl_use_global` / `rl_online_training_disabled` / `rl_global_training_source_flag`) but was never mounted after v0.2.31 Agent J created it. Now mounted in the RL Reranker module's config tab (`/modules/vct-rl-reranker/config`) above the generic schema-rendered controls. Three NEW getter Tauri commands added (`get_rl_use_global`, `get_rl_online_training_disabled`, `get_rl_global_training_source_flag`) — counterparts to the existing setters, default `false` for missing rows. New lightweight wrapper component `RlRerankerStatusPanel.svelte` bundles the dashboard widget with a flag-state summary (e.g. "Online training active — events update the local model" vs "Frozen — local model unchanged, events logged only"). Pure helper `summarizeRlFlags()` in `rl-settings-summary.ts` translates the 3 booleans into human-readable copy + stable discriminant keys for CSS hooks; covered by 7 vitest cases over all 8 flag combinations. Rust getter commands covered by `getters_round_trip_with_setters_and_default_to_false` in `commands::rl_settings::tests`. Per A2 archaeology discovery + user directive 2026-05-30 (do NOT delete; rewire and make useful). Per multi-Opus pre-push review item 10.

- **NEW-3.E (W40-D): launcher boot — resume containers for service-type modules with `container_name IS NULL`** (Agent W40-D): the previous logic in `vct-hub::module_supervisor::resume_containers_on_startup` had two bugs that blocked auto-restart of container-distributed modules after launcher reboot. (a) It iterated only `module_installs` rows with non-NULL `container_name` (via `list_module_installs_with_containers`), skipping any module whose install-time auto-start failed before NEW-3.B's defaulting helpers shipped in v0.2.39 — those rows have `container_name=NULL` and never got a second chance. (b) It hard-coded a `module_id == RL_RERANKER_MODULE_ID` gate that blocked generalization to other container-distributed modules. Now: new DB query `list_module_installs_needing_start` returns all `status='installed'` rows (NULL container_name included). The caller-side gate matches the install-time NEW-3 widening (`runtime.type ∈ {container, service}` AND `install.method = container_pull`). NULL-container rows route through `start_container_after_install` (synthesizes defaults via NEW-3.B helpers AND persists the resolved name back to the row); non-NULL rows keep the existing probe-and-restart logic. Soft-fail per-module — one container start failure does not abort the resume loop. NEW-3.C surfacing: NULL-container failures write to `module_installs.last_error` so the GUI tile renders a clear failure state. 4 unit tests in `module_supervisor.rs` cover all four branches (NULL container with service manifest reaches start_after_install; non-NULL named container uses existing path without writing last_error; non-container_pull skipped by gate; cli-runtime skipped by gate) + 1 unit test in `vct-launcher-core/src/db/modules.rs` verifies the new query returns both NULL and SET container_name rows while excluding non-`installed` statuses. The hub-side `RL_RERANKER_MODULE_ID` const is now `#[allow(dead_code)]` — kept for one release cycle to avoid cascading rename cleanup; queued for v0.2.41 cleanup if it stays dead.

- **F2 (silent-correctness): RL telemetry writer re-keyed on `(project, ACTIVE_EMBEDDING)`** (Agent F2). Previously a module-global singleton in `claude_mcp_servers/weaviate_mcp/server.py:3179`, the writer would freeze whatever (project, embedding_source) was current at first instantiation and stamp those values onto every subsequent event for the lifetime of the MCP subprocess. Mid-session env changes (e.g. user switching `ACTIVE_EMBEDDING` from qwen3 to arctic2, or PROJECT_NAME re-resolution from launcher.db adopt) silently contaminated the offline training corpus with stale tags. Now: a dict `_rl_telemetry_writers: dict[tuple[str, str], RLTelemetryWriter]` keyed by `(project, embedding_source)`; the factory re-evaluates the key from fresh env on every call (with the existing `EmbeddingService.for_project` probe still preferred when reachable) so each distinct env tuple gets its own writer with correct tags. RLTelemetryWriter holds no persistent file handles or sockets (RLDataLogger opens+closes the JSONL via context manager on every write), so clearing the dict is sufficient teardown — a new `_reset_rl_telemetry_writers()` helper is the canonical reset path for tests and any future shutdown hook. The legacy `_rl_telemetry_writer_instance = None` global is kept as a tombstone for back-compat with any external test that resets it (the factory no longer reads it). 5 new tests in `tests/test_telemetry_writer_rekey_v0240.py` (T1 first-call construction, T2 idempotent same-env, T3 ACTIVE_EMBEDDING flip yields NEW writer — the silent-correctness gap closed here, T4 distinct tuples cache independently, T5 reset clears all). Per multi-Opus pre-push review highest-risk silent-correctness gap #2.

- **F4 (cleanup): delete hub-side stub resolver spawn (dead code)** (Agent F4). The block at `launcher/src-tauri/vct-hub/src/server.rs:186-198` spawned a "resume containers" path whose resolver was hardcoded as `Box::new(|_id| None)` — every lookup returned None, so the V40-D-generalized resume loop on the hub side skipped every row. The launcher-side path at `module_service.rs:1566` is what actually keeps containers alive. Removing the stub eliminates a misleading code path that masqueraded as live coverage. Zero behavior change. Per multi-Opus review highest-risk gap #4 (confirmed by adversarial cross-check #2 as "duplicate dead code, not race risk").

- **R4: write rl-latest-weights Supabase edge function** (Agent R4). Until v0.2.40 the "Download default weights" GUI button at `launcher/src-tauri/src/commands/module_default_weights.rs:74` POSTed to a Supabase function (`/functions/v1/rl-latest-weights`) that was NEVER deployed — introduced 2026-05-24 in v0.2.32 agent D's manifest-button work; the Rust caller's unit tests used in-process HTTP mocks so the gap wasn't caught locally. v0.2.40's multi-Opus pre-push review flagged this as highest-risk gap #5. Source now lives in `launcher/supabase/functions/rl-latest-weights/` modelled after sibling `rl-latest-version` — same `_shared/validate_tier` server-to-server re-validation, same 15-min signed-URL TTL, same private bucket (`paid-module-weights`), same data-driven embedding-source discovery (no hard-coded enum — a new source goes live the moment a `paid_module_releases` row is inserted). Contract differs from `rl-latest-version` in that this endpoint ALWAYS returns the head (no `current_weights_version` comparison): POST `{license_key, machine_id_hash, embedding_source?, module_id?}` + `Authorization: Bearer <license_key>` → 200 `{download_url, version, sha256, expires_at}` for paid-tier users; 400 `unsupported_embedding_source` with a `supported_embedding_sources: string[]` discovery list when no row matches; 401 `tier_insufficient` / `license_invalid`; 405 method_not_allowed; 500 `service_misconfigured` / `release_lookup_failed` / `signed_url_generation_failed`. 30 unit tests in `validation_test.ts` pin the validator's T1–T5 cases (missing license_key, missing machine_id_hash, present-but-empty embedding_source, arbitrary embedding_source string passes validation by design, valid payload roundtrips) plus tier-ladder, token-preview, and UUID-regex assertions. **Deploy is gated on user action**: run `bash launcher/supabase/functions/rl-latest-weights/deploy.sh` from the repo root (the script `cd`s into `launcher/` first so the Supabase CLI's default `./supabase/functions/<name>/index.ts` lookup resolves — running `supabase functions deploy rl-latest-weights` from the repo root without the `cd` fails with `entrypoint path does not exist`, observed in the 2026-05-30 dogfooding attempt). Per user 2026-05-30 decision (option B over A — cleaner contract, function name matches caller intent).

### Changed

- **L1.M (follow-up to L1): migrate legacy license keychain entry to canonical per-module username** (Agent L1.M). v0.2.40's L1 landed multi-key licensing but kept the orchestrator-tier key at the legacy keychain username `VIBECODED_LICENSE_KEY` to preserve downgrade compatibility. Per user directive 2026-05-30: no downgrade lane is needed. L1.M completes the migration: `keychain_username_for(ORCHESTRATOR_MODULE_ID)` now returns the canonical `license_key____orchestrator__` (same pattern as every other module). `ensure_legacy_orchestrator_row_migrated` at launcher boot READS the value from `VIBECODED_LICENSE_KEY` → WRITES it to `license_key____orchestrator__` → DELETES the legacy entry. Atomic-by-construction: write before delete, no data loss on partial failure. Two branches: (1a) older v0.2.40-L1 build's row pointing at the legacy username → keychain move + row rewrite; (2) pre-v0.2.40 install upgrading → keychain move + row insert at canonical. Idempotent across all branches. All 6 launcher-side call sites updated to use `keychain_username_for(ORCHESTRATOR_MODULE_ID)` instead of the hardcoded legacy constant: the `LICENSE_KEY_NAME` const + 4 call sites in `commands/licensing.rs` (`read_license_key_from_keychain`, `license_refresh`, `license_activate`, `license_deactivate`); `module_default_weights.rs::resolve_license_key`; `lib.rs`'s `spawn_daily_weights_poll` closure. New exported helper `vct_launcher_core::db::license_keys::license_keychain_service()` returns `"vct.global.licensing"` so downstream consumers (orchestrator projects, hooks, MCPs) have a canonical entry point for service+username discovery. `SecretsPanel.svelte` GUI entry updated to the new canonical username. New `docs/license/KEY_DISCOVERY.md` documents the discovery contract for downstream consumers (keychain service name, username pattern, reserved orchestrator slot, OS-native lookup commands, migration semantics, per-module keys table). 4 new tests cover the migration: `migrate_no_op_when_row_already_at_canonical_username` (hermetic BRANCH 1b), `migrate_does_not_error_on_clean_install` (hermetic BRANCH 3), `migrate_row_rewrite_preserves_key_prefix` (hermetic SQL pathway), `migrate_full_flow_legacy_to_canonical` (full keychain-touching end-to-end, `#[ignore]`d by default with prior-state restore). Existing `keychain_username_for_orchestrator_returns_legacy_constant` test renamed to `keychain_username_for_orchestrator_uses_canonical_format` with updated assertions; new `license_keychain_service_returns_canonical_value` test pins the new helper. `LEGACY_KEYCHAIN_USERNAME` const retained ONLY for the migration helper's READ path; production call sites no longer reference it.

- **X2: cross-OS CI matrix for installer-smoke** (Agent X2). `.github/workflows/installer-smoke.yml` (v0.2.38 A5) was ubuntu-only. v0.2.40 adds a third job `install-smoke-no-container` that runs `python install.py --no-containers --skip-models` + `install-bundle` on a matrix of `windows-latest` and `macos-latest` (Apple Silicon), with `fail-fast: false` so a Windows-only bug doesn't kill the macOS run (and vice versa). Existing ubuntu jobs (`install-smoke` — full file/env/wrapper assertions — and `weaviate-smoke` — Weaviate schema bootstrap with a real service container) remain unchanged. The cross-OS job exercises the install steps that don't need a container runtime (Python venv setup, `.env` materialization with `VCT_ORCHESTRATOR_ROOT` + `VCT_INFRASTRUCTURE_DIR` keys, `.claude/` scaffold from `install-bundle`, wrapper-script presence — `.ps1` siblings on Windows, bash wrappers on macOS). GitHub-hosted Windows/macOS runners don't ship Podman/Docker and `services:` blocks don't run on non-Linux runners, so Weaviate/Ollama/container-management coverage stays in the dedicated ubuntu jobs. Cross-OS install bugs (Windows path handling in `vco_lib.config_projection`/`env_template`, macOS resource-fork copy quirks, `.ps1` UTF-8 BOM issues from prior contributor fixes) will now surface in CI on every install-pipeline push rather than waiting for a user report. Per multi-Opus pre-push review item X2.

- **W40-C: hardcoded `DEFAULT_SHARED_KG_COLLECTION` fallbacks now read from launcher.db first** (Agent W40-C). Hardcoded fallback sites for `SHARED_KG_COLLECTION` in `vco_lib/config_projection.py` and `vco_lib/env_template.py` now consult `launcher.db project_kg_bindings(slug='orchestrator-root', role='primary').collection_name` before falling back to the bundled const. The Rust const in `launcher/src-tauri/src/commands/project_env_settings.rs` is renamed from `DEFAULT_SHARED_KG_COLLECTION` to `LAST_RESORT_SHARED_KG_COLLECTION` (value unchanged at `"VibeCodedOrchestrator_KnowledgeGraph"`) — the rename is purely an audit-discipline signal so call sites that bypass the DB-read chain become greppable. Python adds a parallel `_LAST_RESORT_SHARED_KG_NAME` const + a `_resolve_shared_kg_default_from_launcher_db()` helper with soft-fail behaviour (DB missing / unreadable / orchestrator-root row absent / binding empty → falls back to the const). `claude_mcp_servers/weaviate_mcp/server.py` stays env-only for `SHARED_KG_COLLECTION` resolution (the MCP subprocess cannot read launcher.db) but gains a startup probe via the first `get_weaviate_client()` call: when `SHARED_KG_COLLECTION` names a Weaviate class that doesn't exist, the MCP emits a structured `WARNING` log line ("SHARED_KG_COLLECTION='X' but no such class exists in Weaviate; set the env var in `.claude/settings.json env` or re-run launcher boot"). Prevents future canonical-name flips (v0.2.12 PR-26, v0.2.23 B1, future) from silently stranding users behind stale const-derived values when their launcher.db binding has the right answer. Cross-language invariant test `tests/test_shared_kg_constant_consistency.py` updated to track the new Rust const name; new `tests/test_env_template_resolve_shared.py` pins all three branches of the soft-fall-through chain. No behaviour change for fresh installs (the const value is unchanged); existing installs whose launcher.db binding is correct but whose env files are stale will pick up the right name on the next env regen.

- **F5 (cross-OS hardening): consolidate `~/.vct/launcher.db` path drift via `vco_lib.paths.vct_root_dir()` / `vco_lib.paths.launcher_db_path()`** (Agent F5). Ten sites across `install.py` (`_vct_state_dir` helper and `_resolve_project_id_by_folder`), `vco_lib/diagram_indexer.py` (three sites: `delete_diagram_from_indices`, snapshot UPSERT helper, `snapshot_diagram_file`), and `vco_lib/project_init.py` (the `_launcher_db_path` helper plus three `_read_*_from_launcher_db` helpers) reconstructed the launcher.db path inline (`Path.home() / ".vct" / "launcher.db"` or, in `diagram_indexer.py`, a slightly-buggier `os.environ.get("VCT_STATE_DIR") or (Path.home() / ".vct")` form that would coerce an empty-string env var to a relative `Path("") / "launcher.db"`). Refactored to use the canonical `vco_lib.paths.vct_root_dir()` / new `launcher_db_path()` helpers so future cross-OS convention changes (macOS `~/Library/Application Support/vct/`, Windows `%LOCALAPPDATA%\vct\`) need a single fix-point. New `launcher_db_path()` convenience helper is `vct_root_dir() / "launcher.db"` — added because most callers want the DB file directly, not the state-root. The two pre-existing module-local helpers (`install._vct_state_dir` and `project_init._launcher_db_path`) are kept as thin delegating wrappers for back-compat with external imports. `templates/scripts/generate-kg-summary.py` intentionally NOT refactored (already documented: ships into per-project installs without `vco_lib` on PYTHONPATH). Pure mechanical refactor; zero behavior change on Linux. Per multi-Opus pre-push review recommendation #5 (preempts a future scramble when X1 implements the macOS / Windows branches). 10 new tests in `tests/test_vct_root_dir_consolidation.py` cover the canonical resolvers, delegation parity (`install._vct_state_dir == vct_root_dir`, `project_init._launcher_db_path == launcher_db_path`), the empty-env-var robustness fix, and a static anti-regression guard that fails CI if any production `.py` file outside `vco_lib/paths.py` re-introduces an inline `Path.home() / ".vct"` reconstruction.

## [0.2.39] — 2026-05-28

### Added

- **NEW-3.D: install-time manifest contract validator for container-distributed modules** (Agent V39-NEW3.D): `ModuleManifest::validate_for_container_start()` in `vct-launcher-core/src/manifest.rs` checks that `container_pull` + `service`/`container` runtime manifests declare `install.container.image` (Error), `runtime.container_name_template` (Deprecation), and `runtime.image_ref` (Deprecation). `ManifestWarning` + `WarningSeverity` types carry field path + message + severity. Wired into `install_module_for_project` in `modules.rs` BEFORE `set_module_status(Installed)`: Error warnings block with `set_module_status(Error)` + a clear `Err` return; Deprecation warnings log to `eprintln` + `db.audit("module_install_manifest_deprecation")` and continue. CI validator binary (`validate-manifest`) extended to call `validate_for_container_start()` and print `[deprecation]`/`[error]` prefixed lines — exits non-zero only on Error severity (RL Reranker v0.2.7 manifest emits 3 deprecation warnings + 0 errors, exit 0: backward compatible). 7 integration tests in `vct-launcher-core/tests/manifest_ci_gate.rs` cover: passes-fully-valid, deprecation-for-missing-container_name_template, deprecation-for-missing-image_ref, error-for-missing-image, skips-non-container-pull, skips-cli-runtime, rl-reranker-v027-emits-deprecations-not-errors.

### Fixed

- **NEW-3.B: synthesize `container_name_template` + `image_ref` defaults; apply NEW-3 widening to hub-side supervisor** (Agent V39-NEW3.B) — BLOCKER for all service-type modules and applies to all future container-distributed modules per project contract. `start_container_for_module` in both the launcher (`module_service.rs`) and the hub (`module_supervisor.rs`) previously hard-failed with `.ok_or_else()` when `runtime.container_name_template` or `runtime.image_ref` were absent from the manifest. Modules declaring `runtime.type = "service"` without these optional fields (e.g. RL Reranker v0.2.7) installed successfully but the container never started, leaving the tile in `Installed` state with no visible error. Fix: added `RuntimeBlock::resolve_container_name_template(module_id)` and `RuntimeBlock::resolve_image_ref(container_install, version)` methods to `vct-launcher-core/src/manifest.rs`; both methods return the declared value when present and non-empty, otherwise synthesize sensible defaults (`{module_id_safe}-{project_slug}` for the container name; `{image}:{version}` from `install.container` for the image ref). Both launcher and hub call the shared methods — no synthesis logic duplication. Also applied the NEW-3 gate widening (`"container" | "service"`) to `build_podman_run_args` in `vct-hub/src/module_supervisor.rs` which was previously still gated on `"container"` only (launcher-side widening in v0.2.38 had not been applied to the hub, creating a divergence). 11 new unit tests across `manifest_ci_gate.rs`, `module_service.rs`, and `module_supervisor.rs`.
- **NEW-3.C: surface container-start failure to `module_installs.last_error`** (Agent V39-NEW3.C): when `start_container_after_install` fails post-install, the error was previously swallowed — `eprintln` + an unlistened Tauri event + audit log only. The GUI tile showed `status='installed'` with no error message, making the failure invisible. `Db::set_module_last_error` (new targeted DB helper) now writes the failure reason to `module_installs.last_error` without touching `status` (install succeeded; only the post-install container start failed — user can retry via Restart). Two unit tests added to `vct-launcher-core/src/db/modules.rs`: `db_set_module_last_error_persists_error` and `db_set_module_last_error_to_none_clears_field`. All 1176 lib tests pass.

## [0.2.38] — 2026-05-28

A **comprehensive paid-module + install-pipeline + telemetry + KG-hygiene release** — closes 14 distinct items (8 fixes + 4 features + 2 CI gates) across two parallel agent fanouts, with audit-before-fanout discipline applied. Fixes 4 production regressions from v0.2.36/0.2.37 (RL Reranker install failure, MCP project misidentification, query_emb missing from telemetry, kg-sync VCT_INSTALL_ROOT plumbing) + ships the AGPL-side training corpus loader + adds 2 CI prevention gates. All fixes propagate via existing `install.py --update` + launcher self-update flows; no operator action required.

### Added

- **A1: pip-installable `weaviate_mcp` package; sys.path hacks removed from consumer scripts** (Agent A1): `claude_mcp_servers/` is now a proper Python package (`claude_mcp_servers/pyproject.toml` + `weaviate_mcp/__init__.py`). `install.py` runs `pip install -e claude_mcp_servers/` after the root requirements install so the `weaviate_mcp` namespace is on the venv's site-packages. Consumer scripts updated: `sync_knowledge_graph.py`, `detect_duplicates.py`, `search_knowledge.py`, `analyze_code_graph.py`, and `query_code_graph.py` no longer manipulate `sys.path` to locate the `weaviate_mcp` package — they import it directly. `vco_lib` path entries are retained where needed (vco_lib is not yet a standalone package). Soft-fail: if `claude_mcp_servers/pyproject.toml` is absent (e.g. older clone), install continues and scripts fall back to their existing sys.path resolution. Ships via `install.py` / `install.py --update`.
- **A3: `install.py --update` reconciles missing canonical env keys** (Agent A3): upgrading the orchestrator via `install.py --update` previously left the existing `.env` unchanged, so keys added in newer versions (e.g. `DIAGRAMS_COLLECTION` added between v0.2.10 and v0.2.34) were silently absent — MCP servers fell back to bundled defaults with no warning. `_reconcile_env_keys()` now runs during `--update`: it parses the existing `.env`, compares against `vco_lib.env_template.list_canonical_env_template_keys()`, and appends any missing keys with their defaults plus a `# Added by install.py --update on <date>` comment marker. User-set values are never touched. Prints a summary: "added N new env key(s): …". 6 unit tests in `tests/test_install_py_env_reconcile.py` cover the append, noop, user-value-preserved, non-canonical-key-preserved, skipped-when-missing, and comment-marker cases.
- **TRAIN-LOADER: AGPL-side training corpus loader for offline qwen3 pretraining** (Agent V38-TRAIN-LOADER): new `claude_mcp_servers/rl_client/training_loader.py` (~260 LoC) — streams `rl_events.jsonl` + `rl_events_qwen3.jsonl` through a 10-step filter funnel (schema_version=2, embedding_dim=1024, no failure_mode, qwen3-aligned source, optional synthetic exclusion, cohort alias normalisation, on-the-fly Ollama query-embedding backfill with per-load cache, cross-file dedup preferring qwen3-native rows). Public API: `load_qwen3_training_corpus(...)` generator; default alias map collapses all five legacy maintainer label variants (Claude/VCODev/VibeCoded Orchestrator/VibeCodedOrchestrator/orchestrator-root) to the canonical slug. Addresses NEW-7 (cohort label drift) and NEW-9 (schema_version filter) at training-read time without rewriting the corpus. Ships via `install.py` / `install-bundle --update`. 37 unit tests in `tests/test_training_loader.py` cover each filter step, backfill mocking, alias map, and dedup semantics.
- **A2: `install-bundle --write-env` for standalone (launcher-less) installs** (Agent A2): `python -m vco_lib.project_init install-bundle --folder . --orchestrator-root /path/to/vco` previously left the project without `.claude/env` or `.claude/settings.json env` when the launcher DB was absent, making every bundled wrapper script fail (`VCT_ORCHESTRATOR_ROOT` / `VCT_INSTALL_ROOT` not found). Added `--write-env` flag: when set, `install-bundle` resolves the env bundle from the launcher DB when available; when the DB is absent (or the project is not registered), falls back to a canonical default bundle derived directly from `--orchestrator-root` and the folder basename (or `--project-name` override). The bundle writes `VCT_ORCHESTRATOR_ROOT`, `VCT_INSTALL_ROOT`, `VCT_INFRASTRUCTURE_DIR`, `KG_COLLECTION`, `CODE_GRAPH_PROJECT`, `DEVELOPMENT_COLLECTION`, and all service-URL defaults into `.claude/env` + `.claude/settings.json env`. Also adds `--project-name` arg to override the folder-basename for KG collection naming. Makes `install-bundle` standalone-usable for OSS-developer / fork-integrator workflows without a running launcher. 11 unit tests in `tests/test_install_bundle_standalone.py`. See v0.2.38 backlog item A2.
- **A4: Unified KG schema invariant — `_KG_NODE_SCALAR_PROPERTIES` canonical constant** (Agent V38-CI): `templates/scripts/sync_knowledge_graph.py` previously maintained two independent property dicts for the fresh-create and additive-migrate paths inside `ensure_collection_exists`. V37-C Gap 6d found that chunking props (`chunk_num`/`total_chunks`/`source_node_id`) were present in the fresh-create branch but missing from the migrate branch. v0.2.38 A4 hoists all scalar properties into a module-level constant `_KG_NODE_SCALAR_PROPERTIES` driven by string sentinels; both paths iterate it. Five new unit tests in `tests/test_kg_schema_consistency.py` enforce the invariant so schema drift can never recur silently. See `docs/audits/install-update-audit-2026-05-27.md` Finding F9.
- **A5: End-to-end install smoke workflow** (Agent V38-CI): new `.github/workflows/installer-smoke.yml` fires on push/PR touching `install.py`, `vco_lib/project_init.py`, `vco_lib/config_projection.py`, or `templates/**`. Two jobs: (1) `install-smoke` — runs `install.py --no-containers --skip-models` + `install-bundle` against fresh tmp dirs, then asserts root `.env` contains `VCT_ORCHESTRATOR_ROOT`/`VCT_INFRASTRUCTURE_DIR`/`VCT_INSTALL_ROOT` and that `kg-sync --help` / `code-graph-analyze --help` exit 0. (2) `weaviate-smoke` — boots Weaviate 1.28.4 as a GitHub Actions service, bootstraps a test KG collection via `ensure_collection_exists`, asserts all 5 chunking/temporal props (`chunk_num`, `total_chunks`, `source_node_id`, `status`, `content_hash`) are present on the live schema. Ollama + embeddings deliberately excluded (too heavy for CI). See `docs/audits/install-update-audit-2026-05-27.md` Finding N7.

### Fixed

- **NEW-10: `analyze_code_graph` silently wrote to bare Weaviate class names when `--project` unset** (Agent NEW-10): `_collection_name` previously returned the bare base name (e.g. `CodeFunction`) when `project_name` was empty, allowing multiple unrelated analyses to pile into a single un-prefixed collection. Fix: raise `SystemExit` with a clear message directing the operator to pass `--project` or set `CODE_GRAPH_PROJECT`, preventing multi-project data collision at the call site. 4 unit tests in `tests/test_code_graph_analyzer.py` cover the guard, the error message content, the normal path, and all 5 base collection types. Ships via `install-bundle --update`.
- **NEW-11: typed_links writer guard + legacy-collection repair script** (Agent NEW-11): malformed `typed_links` values (list-of-strings `"rel::target"` form written by pre-canonicalization writers) crashed Weaviate's gRPC iterator with `creating primitive value for typed_links: proto: invalid type: []interface {}`. REST GraphQL worked fine but all gRPC queries against the affected collection would fail. Fix: new `_normalize_typed_links()` helper in `templates/scripts/sync_knowledge_graph.py` normalises the field to the canonical list-of-objects shape (`[{"relation_type": str, "target_title": str}]`) at both insert sites (single-chunk and chunked paths) before every `coll.data.insert()`. List-of-strings are parsed; unknown types are dropped with a warning; empty/None produce `[]`. New one-shot repair tool `claude_mcp_servers/scripts/repair_kg_typed_links.py` walks all `*_KnowledgeGraph` collections via REST GraphQL, identifies malformed rows, converts them in-place via `data.update()`, and prints a summary (rows-checked / rows-fixed / rows-skipped). 16 unit tests in `tests/test_typed_links_shape_guard.py` cover both the writer guard and the repair normaliser. Ships via `install.py --update` / `install-bundle --update`.
- **NEW-3: auto-start gate excluded `runtime.type="service"` modules; service-type paid modules installed but never got a container** (Agent V38-A): `modules.rs` Phase-1E gate (`installed`-status auto-start) matched only `manifest.runtime.r#type == "container"`, silently skipping the `podman run` step for any module declaring `runtime.type = "service"` (e.g. RL Reranker). The manifest schema accepts 5 runtime types (`mcp_stdio | mcp_http | service | cli | container`); only `container` and `service` are long-running daemons that warrant a persistent container — the other three are on-demand invocations. Fix: widened the gate in `modules.rs` and both preconditions in `module_service.rs` (`build_podman_run_args`, `start_container_for_module`) from `== "container"` to `matches!(…, "container" | "service")`. Regression test `build_podman_run_args_accepts_service_runtime_type` mirrors the existing `auto_restart_true` test with `type="service"`. Defence-in-depth: new `start_module_container` Tauri command + "Start" button on installed tiles whose `container_name` is NULL and `runtime_type` is `container` or `service` (guards against future schema additions missing the gate).
- **NEW-6: MCP resolver used `Path(__file__)` instead of `CLAUDE_PROJECT_DIR` — every non-orchestrator-root project's MCP resolved to the wrong project config** (Agent V38-MCP): `_try_resolve_project_config()` in `claude_mcp_servers/weaviate_mcp/server.py` inferred the active project from `Path(__file__).parent.parent.parent` (the server.py installation path). Because the global `~/.claude.json` MCP registration always points server.py at the orchestrator install dir, every workspace opened in Claude Code — regardless of which project it was — resolved to the same project config (the orchestrator's own). Telemetry events were mislabeled, KG queries hit the wrong collection, and retrieval reranking used the wrong cohort. Fix: prefer `os.environ['CLAUDE_PROJECT_DIR']` (the workspace Claude Code is actively serving) over `__file__`; fall back to `__file__` only when the env var is absent or points at a non-existent path. `vco_lib/project_config.py::resolve()` takes `project_root` as a parameter and does no path inference of its own — no change needed there. Ships via `install.py --update` to all existing installs.
- **NEW-8: MCP writer call site never passed `query_emb` to `writer.log_retrieval` — 100% of post-v0.2.20 retrieval events missing query-side embedding** (Agent V38-MCP): `_rl_cache_and_rerank` in `server.py` and `rl_kg_search.py` never forwarded the query embedding to the local JSONL telemetry writer. The query vector was captured upstream (for `cos_qn` cosine enrichment on candidate nodes) but never threaded through to `log_retrieval`. Result: 758 retrieval events from maintainer-side aliases since 2026-05-20 have no `query_emb` field, breaking any qwen3-aligned training pass that needs query vectors. Fix: added `query_emb: list[float] | None = None` kwarg to `_rl_cache_and_rerank`; both `hybrid_search` and `semantic_graph_search` pass `query_emb=query_vector`; `rl_kg_search.py` passes `query_emb=vector`. Ships via `install.py --update`.
- **NEW-1: `container_pull` used L1 manifest's placeholder `pull_token_endpoint` instead of L0 catalog's real URL** (Agent V38-B): `installer_engine::request_pull_token` (and its caller `container_pull`) read `pull_token_endpoint` from the L1 manifest extracted from the image at install time. The RL Reranker image shipped with `placeholder.supabase.co/functions/v1/rl-pull-token` in `/app/vct-module.json`, causing every install attempt on v0.2.37 to fail with `POST https://placeholder.supabase.co/…: error sending request`. The L0 catalog (`module-catalog` edge function) always carries the real Supabase URL server-side. Fix: added `l0_pull_token_endpoint: Option<&str>` parameter to `request_pull_token` / `container_pull` / `run_install` / `run_upgrade` / `refetch_artifact`; when provided, it is preferred over the L1 value (`l0.unwrap_or(l1)` at the call site). `install_module_for_project` and `update_module_for_project` now call `resolve_install_metadata` (catalog-cache read, no network hit) and thread the L0 endpoint through. Soft-fail: if the catalog cache is empty, `None` is passed and L1 is used as before — install fails with a clear placeholder-URL error rather than silently wrong. Regression test `l0_endpoint_overrides_l1_placeholder_in_request_pull_token` pins both branches of the selection logic.
- **NEW-12: launcher init-time migration rewrites legacy shared-KG collection names in `kg_collection_access`** (Agent NEW-12): projects created before v0.2.12 could carry `VibeCodedTools_KnowledgeGraph` rows; projects created between v0.2.12 and v0.2.22 could carry the lowercase-c `VibecodedOrchestrator_KnowledgeGraph`. Both spellings resolve to a non-existent Weaviate collection, silently degrading cross-project knowledge retrieval. Fix: `Db::migrate_legacy_shared_kg_collection_names` in `vct-launcher-core/src/db/access.rs` runs at every launcher boot — dedup-then-rename pattern: if BOTH a legacy row and the canonical row exist for the same `project_id`, the legacy duplicate is deleted; otherwise the legacy row is renamed in place. Called from `lib.rs` setup block with `DEFAULT_SHARED_KG_COLLECTION` as the target. Audit-log entry written when rows are renamed. Idempotent: no-op on boots with no legacy rows. 6 unit tests cover: lone legacy rename, lowercase-c rename, dedup (both rows same project), canonical-only no-op, empty-table no-op, idempotency.

## [0.2.37] — 2026-05-27

A **multi-axis hotfix release** — closes 7 distinct user-visible regressions discovered during v0.2.36 dogfooding (1 fresh-install of an external user project exposed 5 install-bundle gaps; 1 fresh-3rd-party admin install exposed 2 launcher-side state bugs). 7 parallel Opus agents (V37-A/B/C/D/E/F/G) on isolated worktrees + per-branch diff review during integration.

### Fixed — paid-module install pipeline (BLOCKER series)

- **`container_pull` deadlock on Stdio::piped() with large images** (Agent V37-A, Issue 7): the launcher's `podman/docker pull` invocation in `installer_engine.rs::container_pull` used `Stdio::piped()` on stdout + stderr but never drained the pipes. For images >~64KB of stdout (any image with ≥3 layers), the pipe buffer filled mid-pull, child blocked writing, parent's `.await` returned `success() == false` even though the image successfully downloaded. Symptom: install fails with `podman pull failed (exit -1)` but `podman images` shows the image present at full size. Fix: `Stdio::null()` on stdout + stderr (progress goes via Tauri events, not by parsing podman's stdout). Same fix applied to `git clone --depth 1` (source-method install) and `git pull --ff-only` (run_upgrade). Regression test exercises a controlled subprocess emitting >200KB stdout to verify no deadlock.

- **ActivationModal hides Refresh + Rebind buttons when admin's `machine_id_hash` drifted** (Agent V37-B, Issue 1): when `/validate-tier` returns `tier='free' + error='machine_mismatch'` (the v0.2.36 algorithm-migration recovery case), the modal collapsed into its "no license activated" branch and showed ONLY the activation input field. The rebind button (Agent S's v0.2.36 work) and Refresh button were both in the `{:else}` branch and unreachable. Fix: introduce `hasActiveLicense(tier, lastError)` predicate that returns true when the rebind-relevant error state is set, so the action-buttons branch renders. 8 new vitest tests pin the predicate behaviour (`launcher/src/lib/admin-rebind.ts`, `ActivationModal.svelte`).

- **`rl-artifact-url` GHCR token-exchange used hardcoded `vct-paid-module` username instead of env-driven** (Agent V37-B, Issue 4a): backport of the 2026-05-27 in-place Supabase hotfix into the repo. The Basic-auth header to GHCR's `/token` endpoint at line 270 used the legacy `GHCR_BASIC_AUTH_USERNAME` constant ("vct-paid-module"). GHCR validates that the Basic-auth username matches the PAT owner for personal-account packages — synthetic literals get 403 DENIED. Replaced with `resolveGhcrUsername()` (the v0.2.36 env-driven resolver, already used for the response field). The diagnostic `console.error([rl-artifact-url DIAG] ...)` line + `// touched <timestamp>` comment from the 2026-05-27 forced-redeploy cycle were removed. Dead `GHCR_BASIC_AUTH_USERNAME` const deleted. 3 new Deno tests pin the Basic-auth header round-trip.

### Fixed — install-bundle bootstrap gaps (external-project dogfood findings)

- **`.claude/env` missing `VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR` / `VCT_INSTALL_ROOT` exports** (Agent V37-C Gap 6a + Agent V37-E Finding F1 + F6): two complementary fixes for the root cause that broke every freshly-installed project's wrapper scripts. (1) Agent V37-C threaded `orchestrator_root` through `vco_lib.project_init.install_project_bundle` → `_apply_canonical_env_via_config_projection` → `project_env_from_db` on the Python side. (2) Agent V37-E added `--orchestrator-root <path>` to the Rust `apply_project_env_via_python` subprocess call (sourced from `app_state["launcher.install_path"]` → `find_local_repo_root` fallback). (3) Agent V37-E also fixed `update_project_v2` to invoke `apply_project_env_via_python` after `run_install_bundle_update` so existing projects backfill the env keys on every bundle update — closes the staleness gap that left long-lived projects missing keys added by later launcher versions.

- **`kg-sync` / `kg-migrate` / `kg-search` / `kg-info` / `code-graph-query` wrappers had no venv fallback** (Agent V37-C Gap 6b): the 5 sibling wrappers only probed `$PROJECT_ROOT/.venv` / `$PROJECT_ROOT/claude_mcp_servers/.venv` and failed with `weaviate-client not installed` when neither existed. `code-graph-analyze` (the canonical wrapper) already had the correct fallback to `$VCT_INSTALL_ROOT/.venv` with `weaviate-client` import validation. Backported the canonical pattern to all 5 siblings (sh + ps1 — also created new `kg-migrate.ps1` for Windows parity, previously missing).

- **`code-graph-query` wrapper didn't set `PYTHONPATH` to include `claude_mcp_servers/`** (Agent V37-C Gap 6c): `query_code_graph.py` requires `weaviate_mcp` on PYTHONPATH but the wrapper never set it. Mirrored `sync_knowledge_graph.py::_resolve_mcp_servers_dir()` defence-in-depth pattern (env-var-first, in-tree fallback) in both `query_code_graph.py` and the `code-graph-query` wrapper (sh + ps1).

- **`sync_knowledge_graph.py::ensure_collection_exists` additive migration missing chunking props** (Agent V37-C Gap 6d): the fresh-create schema lists `chunk_num` / `total_chunks` / `source_node_id`, but the additive-migrate path for pre-chunking collections didn't add them. Every pre-chunking KG collection failed sync with `no such prop with name 'chunk_num' found in class '...'`. Fix: extend the additive prop loop to include the 3 chunking props. Verified against a real legacy `VCODev_KnowledgeGraph` collection during smoke testing.

- **`analyze_code_graph.py` ignored `$CODE_GRAPH_PROJECT` env var** (Agent V37-C Gap 6e): when neither `--from-resolver` nor `--project` was passed, the analyzer fell back to repo dir name — diverging from the KG collection prefix set via `.claude/env`. Result: code-graph collections named after the directory (e.g. `Instambul_map_*`) instead of the configured project (`Instambul1860_*`). Fix: 3-step fallback order — `--from-resolver` > `--project` > `$CODE_GRAPH_PROJECT` > repo-dir-derived name.

### Added — release-discipline automation

- **CI Supabase deploy automation** (Agent V37-G, Issue 2): new `supabase-deploy` job in `.github/workflows/release.yml` runs after the binary build + dist-commit jobs. On every `v*.*.*` tag push, the job authenticates to Supabase via `SUPABASE_ACCESS_TOKEN` secret + `SUPABASE_PROJECT_REF` variable, deploys every edge function in `launcher/supabase/functions/*/`, and runs `supabase db push` for any net-new migrations. Closes the v0.2.36 release-discipline gap where the `rebind-admin-token` edge function shipped in the launcher binary but was never deployed to Supabase (404 NOT_FOUND for every customer install). Gracefully degrades when the secret is unset (private-fork friendly). Adds `docs/operations/supabase-ci-deploy.md` (213 lines) with full operator setup + manual fallback runbook.

- **Supabase migration drift baseline** (Agent V37-G, Issue 3): new no-op marker migration `20260527000000_baseline_drift_reconciliation.sql` documents 8 legacy migration IDs that exist on the remote DB but not in the repo (a historical artifact of pre-CI manual SQL-editor applies). Includes a one-time reconciliation runbook in the SQL header + the operator doc. Future `supabase db push` calls work cleanly after the one-time `supabase migration repair --status reverted ...` + `supabase db pull` baseline pull.

### Refactored — orchestrator-root resolver consolidation

- **Single canonical `resolve_orchestrator_root(db)` resolver** (Agent V37-E): merged the two parallel resolvers (`find_local_repo_root` walking `vct-module.json`, `resolve_install_root_sync` walking `install.py + CLAUDE.md`) into a single canonical fn accepting BOTH marker patterns. DB cache → walk-up fallback → sticky writeback (best traits of both). Both legacy fns kept as deprecated shims so the ~30 existing call sites compile unchanged. `option_env!("VCT_REPO_ROOT")` removed entirely (privacy: was unreachable on release builds; risk: leaked build-host paths if shipped). `ProjectEnvSettings::populate` switched to the DB-cached resolver — survives binary moves where the previous uncached resolver would fall through to None and silently omit `VCT_ORCHESTRATOR_ROOT` from the env files.

### Added — install-time DB seeding

- **`install.py` seeds `<install>/.vct/install_path_seed.txt`** (Agent V37-E): on first install OR lightweight reinstall, install.py writes the resolved install path to a seed file. Launcher boot consumes via new `consume_install_path_seed_if_present` (in `lib.rs`) which writes the value to `app_state["launcher.install_path"]` and deletes the seed. Closes the chicken-and-egg gap where install.py finished but launcher.db didn't know the install path until the first launcher boot's walk-up resolver happened to succeed.

- **`refresh_all_projects_env_with_db` Tauri command + boot hook** (Agent V37-E, F1 Step 4): after the install-path-seed consumer runs at boot, the launcher re-applies `apply_project_env_via_python` for every project in launcher.db. Legacy long-lived projects with stale `.claude/env` self-heal on the first boot post-upgrade — no manual operator action.

### Documentation

- **Admin license recovery walkthrough** (Agent V37-D): new `docs/guides/admin-license-recovery.md` (193 lines) — end-to-end walkthrough for the 3 scenarios that trigger admin token rebind (OS reinstall / new laptop / pre-v0.2.36 upgrade). Launcher GUI flow with v0.2.37 visible-button fix called out + curl fallback with per-OS sha256 one-liners (Linux/macOS shell, Windows PowerShell).

- **Supabase Vault vs Edge Function Secrets warning** + **Package ACL vs Repo ACL gotcha** (Agent V37-D): two new sections in `launcher/supabase/functions/rl-artifact-url/README.md` documenting the load-bearing gotchas discovered during 2026-05-27 dogfooding (each cost ~hour of debugging on a clean operator). Plus a "Scaling beyond one paid module" callout noting GitHub's 1-machine-account ToS limit (the per-bot-user pattern scales to 1 module; multi-module distribution will need org migration OR Lemon Squeezy file hosting).

- **Install/update flow audit** (Agent V37-F): new `docs/audits/install-update-audit-2026-05-27.md` (340 lines) — comprehensive read-only audit identifying 15 install/update flow gaps across the 5 surfaces (`install.py`, `vco_lib.project_init install-bundle`, launcher self-update, GitHub Release CI, Supabase migrations + edge function deploys). 10 fixes in this release; 5 architectural items deferred to v0.2.38+.

### Internal

- 7 agents on isolated `/tmp/vco-wt-v0237-*/` worktrees + per-branch diff review during integration (caught 1 KG-discipline violation: V37-F's audit was force-added to gitignored `.claude/context/`; moved to `docs/audits/` for posterity. Also caught + redacted 1 absolute path leak in the audit body).
- Gate baseline (delta vs v0.2.36): vct-launcher-temp 1156 passed (+17 new), vct-hub 191 passed + 5 pre-existing env baseline, vct-launcher-core 343 passed, svelte-check 0/0 across 866 files, vitest 48/48 (+8 new), pytest 2784 passed (+16 new) + 12 pre-existing env baseline. Total net delta: +41 new passing tests; zero new regressions.

## [0.2.36] — 2026-05-26

A **post-v0.2.35-dogfooding follow-up release** — closes the launcher-side fragility surfaces uncovered while attempting first end-to-end paid-module installs against the v0.2.35 binary on real customer hardware (a Windows laptop) + a server-side architectural shift to per-module GitHub bot users for paid-module distribution. 7 agents (P/R/S/T/U/W + integration tree work).

### BREAKING — admin-tier only

- **`machine_id_hash` algorithm switched from MAC-based to platform-stable host ID** (Agent T): Windows `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`, macOS `IOPlatformUUID` via `ioreg`, Linux `/etc/machine-id` (fallback `/var/lib/dbus/machine-id`). Old algorithm hashed the first 6 bytes of a non-loopback MAC; on laptops with multiple NICs (Wi-Fi + USB Ethernet) Python and Rust could pick different MACs → different hashes → `machine_mismatch` 401 from `validate-tier`. Platform host IDs survive NIC changes. Mirror implementation on Python side. Sentinel fallback `vct-no-platform-host-id-v0.2.36` if all sources fail (testable via `VCT_MACHINE_ID_OVERRIDE` env var). **Migration**: admin-tier users with an existing `vct_admin_*` Vault token must click "Rebind to this machine" once after updating — the new GUI button (Agent S) calls the new `rebind-admin-token` edge function to update the Vault entry's bound `machine_id_hash` to the v0.2.36 value. Pro/MAO/free tiers unaffected (they don't use TOFU machine binding). (`commands/licensing.rs::machine_id_hash`, `VCThelpers/license/validator.py::_machine_id_hash`)

### Added — paid-module distribution (server-side)

- **Per-module GitHub bot-user architecture** for paid-module distribution. Each paid module gets a dedicated GitHub user account whose only access is collaborator-read on that module's package. Leaked `GHCR_SERVICE_PAT` bounded blast radius = exactly one module, not "every private package the PAT owner can access" (the v0.2.35 structural concern). The Supabase function deploys with `GHCR_PAID_IMAGE_REPO=hotak92/vct-rl-reranker` + `GHCR_USERNAME=vct-bot-rl` — package owner and credential owner are now decoupled identities. Customer-facing flow unchanged.
- **`GHCR_PAID_IMAGE_REPO` + `GHCR_PAID_TAG_DEFAULT` env-driven** (Agent W): `rl-artifact-url` reads paid-image repo address from env rather than hardcoded `hotak92/vct-rl-reranker`. Migration to a different image (org migration, bot-user pattern) is now a one-line `supabase secrets set` rather than code redeploy. Pure validators + runtime resolvers in new `_shared/config.ts` (162 LoC) with 34 Deno tests. Backwards-compat: unset = v0.2.35 default applies silently. (`launcher/supabase/functions/_shared/config.ts`, `rl-artifact-url/index.ts`)
- **`GHCR_USERNAME` override**: the launcher's `podman login -u <user>` username is now configurable separately from the repo owner. Resolves via `GHCR_USERNAME` env var with fallback to the owner-half of `GHCR_PAID_IMAGE_REPO` (Agent W's auto-derivation, preserved for backwards compat). Required for the per-bot-user architecture where credential owner (`vct-bot-rl`) differs from package owner (`hotak92`). 15 new Deno tests cover the resolution order + warning behaviour on malformed values. (`launcher/supabase/functions/_shared/config.ts::resolveGhcrUsername`)

### Added — admin token rebind GUI (Agent S)

- **`AdminRebindButton` in ActivationModal** when an admin license validates against a different machine than the one stored in the Vault. Calls the new `rebind-admin-token` Supabase edge function which updates the Vault entry's `machine_id_hash` via a `rebind_vault_admin_machine` SECURITY DEFINER RPC. License key never crosses the Svelte IPC boundary — Rust reads it from the OS keychain and orchestrates the call. Audit log: `admin_auth_log.outcome` CHECK constraint extended to include `'rebind'`. 16 new vitest unit tests.
- **Predicate `shouldShowRebindButton(tier, lastError?)`** (Agent U follow-up): also returns true when the last `/validate-tier` error contains `"machine"` — covers the v0.2.36 algorithm-migration recovery case where `validate-tier` returns `tier='free'` + a `machine_mismatch` error (the user's old MAC-based hash no longer matches the platform-stable hash). Pre-Agent-U fix the button was hidden in this state → user stuck. 6 vitest tests pin the predicate behaviour.

### Added — Mermaid + Excalidraw vendored visual editors (Agent R)

- **`open_diagrams_editor` Tauri command** launches a local axum HTTP server on port 22000–22020 that serves a vendored Mermaid editor (textarea + live SVG preview, no external deps) at `127.0.0.1:<port>/mermaid`. Excalidraw ships a "bridge page" pointing at excalidraw.com (full SPA deferred to v0.2.37+ once embedded React playback proves out in Tauri WebView). DiagramsTab "Draw (visual)" buttons next to each registered diagram open the relevant editor in the user's default browser. Auto-register-on-first-edit if the file isn't in the registry yet.

### Fixed — Windows + cross-OS hardening (Agent P, cherry-picked from contributor)

- **`check_install_health` venv-migration alignment**: pre-v0.2.36 the launcher's first-run health check failed on a contributor's Windows machine because it expected the old `vibecodedtools/.venv` layout; `install.py` had moved to `.venv/<project>/`. Now reads from the canonical post-migration path.
- **Windows runtime-probe timeouts raised to 15s** (contributor observation): podman/docker first-call probe on Windows can exceed 5s (Docker Desktop VM cold-start). 5s on Linux unchanged; **macOS extended to 15s as well** (Agent U finding: Mac Docker Desktop also runs in VM, same cold-cache profile).
- **DialogRoot fork-bomb on Windows**: removed the WebKitGTK-only third branch + added re-entry guard. The third branch fired on Windows because the WebKitGTK check returned non-false on Windows WebView2; without the guard it recursed itself open.
- **Windows console-flash suppression**: `CommandExt::silent()` trait with `#[cfg(windows)]` `creation_flags(0x0800_0000)` (CREATE_NO_WINDOW). No-op on Linux/macOS. Applied via a repo-wide 218-site script-driven sweep covering every `Command::new(...)` in the launcher + hub. The earlier console-flash WAS the root cause of a contributor's "launcher GUI fork-bomb on Windows" report — each subprocess flashed a console window, and on slow Windows hardware the launcher's repeated probes appeared as a flickering proliferation of console windows.
- KG node `knowledge/concepts/cross-os-hook-portability.md` updated with the `silent()` pattern.

### Internal

- 7 agents (P/R/S/T/U/V/W) ran in parallel worktrees on `/tmp/vco-wt-v0236-*/`. V was an investigation agent (no code changes — report at `.claude/context/plans/ghcr-registry-denied-investigation-2026-05-26.md`); all others landed code. Single trivial Phase-3A vs CREATE_NO_WINDOW sweep cherry-pick conflict at Agent P merge, resolved preserving Phase 3A's `token_username` field.
- **GHCR registry-denied investigation**: package access list was empty due to a GitHub UI quirk (inheritance toggle off briefly mid-session, then re-enabled). Restored via Manage Access on the package settings page.
- Rust workspace: 1690 lib tests pass + 3 pre-existing baseline failures in `hub_launcher::tests::*` (10 ignored). svelte-check 0/0 across 866 files. vitest 40/40. Python 36/36 license validator.

## [0.2.35] — 2026-05-26

A **3rd-party-user-journey-driven release.** v0.2.34 closed the install-pipeline bugs found by dogfooding the RL Reranker v0.2.7 install up to the `podman pull` step. v0.2.35 closes the chain from `podman pull` through to a working installed module for a paid Pro-tier customer accessing the launcher GUI for the first time — no terminal, no SQL editing, no manual `podman login`, no JSON file inspection. 7 agents (Phase-3A + J/K/L/M/N + O a11y).

### Fixed — Phase 3A pull-token gateway (the headline blocker)

- **`installer_engine::request_pull_token` rewrite**: previously a documented stub that POSTed `~/.vibecoded/license_cache.json` body verbatim to the manifest's `pull_token_endpoint`. The actual wire contract (`launcher/supabase/functions/rl-artifact-url`) expects `{license_key, machine_id_hash}` — the cache file has neither field. Every paid-module install fell through to anonymous pull and 401'd on the private GHCR registry. The error message then misleadingly cited "Phase 3A gateway not deployed" when in fact the gateway IS deployed; the client just sent the wrong shape. Now reads license key from the OS keychain (same source `license_refresh` uses), computes the MAC-based machine_id_hash (same algorithm as `validate-tier` activation), and POSTs the correct shape. (`installer_engine.rs::request_pull_token`)
- **User-actionable pull-token errors**: each documented `rl-artifact-url` failure mode now produces a specific message instead of the generic 401 footer. `license_invalid` → "your license key is invalid or has been revoked. Open Settings → License → Refresh"; `license_expired` → "your license has expired. Renew on the dashboard, then refresh"; `tier_insufficient` → names both required and actual tiers + suggests upgrade; 500 → "temporarily unavailable, try again". 8 regression tests pin every (status, error code) pairing so future server-side rename / status-code drift fails loudly here BEFORE shipping. (`installer_engine.rs::format_pull_token_error`)
- **Container-pull diagnostic accuracy**: when the pull-token request fails AND `podman pull` then 401s on anonymous-fallback, the error message now quotes the actual pull-token-request error verbatim (e.g., "license check failed: your license has expired") instead of the pre-v0.2.35 misleading "Phase 3A gateway not deployed" footer that masked every real failure mode.

### Added — install-pipeline UX completeness (Agents J/K/M/N)

- **Retry button on `error`/`broken` module tiles** (Agent J): a failed install previously left a row with `status='error'` and the GUI offered only "Uninstall" — user had to uninstall-then-install in two clicks. Now error/broken rows render both "Retry install" (primary) and "Uninstall" (secondary), plus a labelled `last_error` block with click-to-expand for long stack traces. The Retry button calls the same `install_module_for_project` Tauri command, leveraging v0.2.34's UPSERT contract to overwrite the error row rather than crash. (`ModuleCatalog.svelte`, new `module-status-display.{ts,test.ts}`)
- **Running-version display + binary-lag warning** (Agent K): the Updates Settings panel now shows `Running: v0.2.X | Latest source release: v0.2.Y` alongside the existing "Update available / up to date" banner. After an `Update orchestrator` action completes, a post-update verification compares the new binary's `CARGO_PKG_VERSION` against the latest source release tag (via the existing `vco_upstream` remote + `git describe --tags`). On mismatch (the v0.2.34 dogfooding case where Update ran BEFORE CI's `chore(binary)` commit landed), an amber banner appears: "Update fetched the previous release's binary (CI hadn't published v0.2.Y's binary commit yet). Click 'Update again' in 5-10 minutes". Dismissible per-tag in localStorage. (`self_update.rs`, `preferences/updates/+page.svelte`)
- **Preflight container-runtime check** (Agent M): before invoking `install_module_for_project`, the launcher now calls a new `check_container_runtime_available()` Tauri command and surfaces an actionable modal if neither podman nor docker is detected. Per-OS install URLs: `podman.io/docs/installation` (Linux), `podman-desktop.io/downloads/{macos,windows}`. Modal buttons: "Install Podman" (opens URL in browser), "Detect again" (re-probes; auto-resumes the install on success), "Cancel". Runs on EVERY install click — not gated by a "first-time" flag, so a user who uninstalls podman mid-session sees the error on their next install attempt. (`commands/install_preflight.rs`, `InstallPreflightRuntimeModal.svelte`, `ModuleCatalog.svelte`)
- **Image variant fallback** (Agent N): before `podman pull`, the launcher now probes the chosen variant tag's existence with `<runtime> manifest inspect <image>:<tag>` (authenticated via the same 15-min pull token). On miss, falls back to the `-cpu` variant (always-available baseline per v0.2.33 manifest schema). Emits an `InstallStage::VariantFallback` progress event so the frontend can show "Requested CUDA variant not available; falling back to CPU variant". Hard-fails if both the chosen variant AND cpu fallback are missing. Probe-error degrades to blind pull (legacy behaviour preserved, so a flaky probe never blocks a valid install). (`installer_engine.rs`)

### Added — Mermaid + Excalidraw UX completeness (Agent L)

- **"No tools exposed by this MCP" placeholder hidden** for atomic MCPs in the per-project Permissions tab. `weaviate-kg`, `search`, `playwright` don't ship a `tool_allowlist` (they're enabled atomically), so the empty placeholder card looked like a bug. Now only MCPs with non-empty tool lists render the per-tool `<details>` block. Atomic MCPs show just the on/off toggle.
- **Draw-new editor entry points** in DiagramsTab: "Draw Mermaid" + "Draw Excalidraw" buttons next to "+ Add diagram" open inline editors with empty scenes; first save auto-registers the file via `register_project_diagram` + `write_text_file`. New `MermaidEditor.svelte` (textarea + live preview pane). Excalidraw uses the existing `ExcalidrawEditor.svelte` from v0.2.33.
- **Drag-drop file import** zone in the empty-state of the diagrams registry. Infers type from extension (`.mmd` → mermaid, `.excalidraw` → excalidraw), opens the Save-as-new dialog pre-filled.
- **Dropdown fold-on-select fix** in the Add Diagram modal Type selector. Wrapped `close() + triggerEl.focus()` in `queueMicrotask` so the value-binding reactive cascade settles before the close toggles `open`. (`Dropdown.svelte`)

### Added — frontend test infrastructure (Agent J bonus)

- `vitest` + `@testing-library` set up as devDependencies, `npm test` script, `vitest.config.ts` with `$lib` alias. The launcher previously had no JS-side unit-test runner (only `svelte-check` + manual smoke). 18 unit tests cover `module-status-display.ts`'s per-status display contract; future agents can use the same harness for component-level testing.

### Improved — accessibility (Agent O)

- 16 distinct WCAG 2.1 AA findings fixed across the new Layer-1 markup, including: `InstallPreflightRuntimeModal` focus trap + `role="status"` redetect banner + sr-only external-link announcement, `Dropdown` `role="combobox"` + `aria-activedescendant` + listbox navigation, `MermaidEditor` `role="region"` + `aria-live` preview + `role="alert"` errors, `DiagramsTab` correct `role="group"` (replacing misleading `role="dialog"`) + drop-zone `aria-live` hints, `PermissionsTab` MCP-toggle `aria-label`s, both binary-lag-warning modals gained `aria-labelledby` + Escape + autofocus. Maintains the v0.2.34 0-error / 0-warning svelte-check baseline.

### Internal

- 7 agents (Phase-3A + J + K + L + M + N + O) ran in parallel worktrees. Single trivial test-block concat conflict at integration merge (J's `module-status-display` import + M's `InstallPreflightRuntimeModal` import in `ModuleCatalog.svelte`, both additive). 1137 Rust workspace lib tests pass (+27 from v0.2.34's 1110 baseline). svelte-check 0/0 maintained.

## [0.2.34] — 2026-05-25

A **dogfooding-driven hardening release.** Real install of the RL Reranker v0.2.7 module on a clean launcher uncovered FIVE bugs in the v0.2.33 install pipeline (`~/.vct/modules/` not bootstrapped, `validate_install_dir` brittle, `module_installs` INSERT vs UPSERT, `{version}` template substitution missing, hardware snapshot stale + missing `gpu_mode_decided`). All five fixed plus a sibling bug (`module_db_migrations` UPSERT) surfaced by a follow-up audit, plus the previously-narrowed Phase 4 per-tool allowlist generalised to all module-shipped MCPs, plus the Mermaid + Excalidraw integration completed (UI catalog, gating, four missing Tauri commands), plus L0 cache stale-on-empty fix, plus Phase 0.E user-secret single-writer contract, plus the a11y sweep clearing all 36 svelte-check warnings to zero. 9 agents, one-week fanout.

### Fixed — install pipeline (5 bugs blocking every first-time paid-module install)

- **Bootstrap `<vct_root>/modules/`** before the path-traversal guard runs. Previously the directory was created lazily by the container-pull step, which ran AFTER the guard, so every clean launcher install failed with a spurious `escapes allowed root` error. (`installer_engine.rs::run_install_inner`, `run_upgrade`)
- **Harden `validate_install_dir`** for the "neither candidate nor root exists yet" case. New shared helper `canonicalize_with_walkup` applies symmetric ancestor-canonicalisation on both sides of the `starts_with` comparison, so the guard works regardless of disk state. (`vct-launcher-core::manifest`)
- **UPSERT for `module_installs`** — previously `INSERT … VALUES(…)` crashed with `UNIQUE constraint failed: module_installs.project_id, module_installs.module_id` on retry-after-error, version upgrade, OR reinstall-after-uninstall paths. Now `INSERT … ON CONFLICT … DO UPDATE`. Covers all three retry/update scenarios + adds 5 regression tests. (`vct-launcher-core::db::modules`)
- **`{version}` template substitution in `resolve_variant_tag`** — manifests declare `"gpu_image_variants.cuda": "{version}-cuda"` as a template, but the launcher returned the literal template string un-substituted, so `podman pull` failed with `<image>:{version}-cuda` (registry returns 401 for nonexistent tags on private repos, which the error path misdiagnosed as "Phase 3A pull-token gateway not deployed yet"). Now substitutes correctly for cuda/rocm/cpu/metal. (`installer_engine.rs`)
- **Hardware-snapshot freshness invariant** — v0.2.20 added the `gpu_mode_decided` field but pre-v0.2.20 snapshots never got backfilled, so CUDA machines pulled CPU variants. Three new trigger points make freshness an architectural invariant: (a) background re-detect at launcher-update completion (via `app_state.hardware_redetect_pending` flag consumed at next-boot — survives the self-update restart); (b) blocking install-time guard in `install_module_for_project` (also covers manual binary-swap upgrade); (c) manual "Re-detect hardware" button in Preferences. Soft-fails to last-known snapshot if probe transiently errors. (`installer.rs`, `self_update.rs`, `preferences/+page.svelte`)
- **UPSERT for `module_db_migrations`** (audit follow-up) — same anti-pattern as `module_installs`. A migration applied once would crash on re-application with `UNIQUE constraint failed: module_db_migrations.module_id, module_db_migrations.filename`, blocking every install retry-after-migration-failure flow. Now `ON CONFLICT(module_id, filename) DO UPDATE SET sha256=excluded.sha256, applied_at=excluded.applied_at`. (`vct-launcher-core::db::module_db_migrations`)

### Added — Mermaid + Excalidraw integration completeness

- **UI catalog entries**: `mermaid` + `excalidraw` now appear in the launcher's global MCP Servers tab AND in each project's Permissions tab. (Previously the MCPs were registered in `~/.claude.json` and worked from Claude Code, but the launcher GUI's catalog was missing the rows, so users couldn't toggle or configure them via the UI.)
- **DiagramsTab gating** — `is_project_module_active("diagrams")` now correctly resolves to true for the orchestrator project (auto-seeds `project_modules('diagrams')` with `enabled=true`; explicit user opt-outs preserved). Previously the per-project Diagrams tab was unreachable because the gate returned false.
- **Four previously-missing Tauri commands** that the DiagramsTab + ExcalidrawEditor invoked but were never implemented (defensive try/catch + 5-second polling masked the gap during v0.2.33 svelte-check):
  - `read_project_diagram_source(project_id, rel_path)` — scoped read enforcing `.claude/diagrams/` boundary.
  - `write_text_file(path, contents)` — atomic write via `tempfile::NamedTempFile::persist`, two-layer path validation.
  - `resolve_project_path(project_id, rel_path)` — canonical + traversal-refusing path resolver (new `path_utils` module, reusable).
  - `subscribe_to_diagram_changes(project_id)` + `diagram-changed` Tauri event — `notify-debouncer-mini` file watcher with ~200ms debounce; replaces the prior 5-second polling fallback. (New file: `commands/diagram_watcher.rs`.)

### Added — Phase 4 generalisation

- **Per-tool MCP allowlist for ALL module-shipped MCPs**, not just diagrams. Modules declare `mcp_registration.tool_allowlist: [{tool, default_enabled, description}]` in their `vct-module.json`; the launcher persists them into a new `module_mcp_tool_defaults` table (migration 023, keyed on `(mcp_name, tool_name)`). The vct-hub allowlist route now MERGES module-shipped defaults with per-project overrides instead of returning the hardcoded diagrams-only constants. Module-update path: `reconcile_module_tool_allowlist` runs on install + update, `clear_mcp_tool_defaults_for_module` on uninstall. Forward-compatible: pre-v0.2.34 manifests without `tool_allowlist` continue to work (treated as "no per-tool defaults"). (`vct-launcher-core::manifest`, `vct-launcher-core::db::mcp_tool_defaults`, `vct-hub::mcp_tool_grants_api`)

### Added — L0 catalog cache improvements

- **Short TTL for empty-modules responses** (60s) vs the existing 15-min TTL for populated responses — prevents the "publisher uploads catalog mid-session, user sits looking at empty catalog for 15 min" trap. (`module_catalog_client.rs::ttl_for`)
- **Always-visible ↻ refresh button** on the Modules tab title row, alongside "Fetched 3m ago" relative-time label. Click bypasses TTL via the existing `refresh_module_catalog` Tauri command. The pre-existing `L0StatusIndicator` stale/unavailable banners are preserved as additive surface. (`ModuleCatalog.svelte`, `stores/modules.ts`)
- **Cache-bust on launcher-version change** — `app_state.last_seen_version` compared with `env!("CARGO_PKG_VERSION")` at startup; if different, `module_catalog.cache*` keys are wiped so the next visit fetches fresh against the new launcher's L0 schema knowledge. Same-version restarts are a no-op (cache preserved → first-paint latency unchanged). Soft-fails. (`module_catalog_client.rs`, `vct-launcher-core::db::app_state::app_state_delete_like`)

### Added — Phase 0.E user-secret single-writer contract

- **`vco_lib/config_projection.py::apply_user_secrets`** extends the v0.2.33 Phase 0.B Part 2 contract to cover `user_secret_pairs` writes (previously explicitly out of scope). Three buckets covered: `per_project`, `shared`, `global` — all at `module_id='user'`. New CLI verbs `apply-user-secrets` + `user-secret-known-keys`. The Python side owns the byte LAYOUT (settings.json deep-merge, `.claude/env` BEGIN/END managed block, `.vscode/settings.json` opt-in); Rust still owns the OS keychain VALUES — intentionally asymmetric. Rust callers bridging into the new CLI verbs is deferred to a future Phase 0.E Part 2 parallel to how Phase 0.B Part 2 staged it. (`vco_lib/config_projection.py`, new `tests/test_config_projection_user_secrets.py` — 36 tests.)

### Added — discoverability + tests

- **State-directory readout in Preferences** — read-only "State directory: `<resolved-path>`" with copy-to-clipboard button and a tooltip explaining when to override via the `VCT_STATE_DIR` environment variable (dev launcher isolation, portable USB-stick state, per-test environments). The path is rendered cross-platform: `~/.vct/` on Linux/macOS, `%LOCALAPPDATA%\\.vct` on Windows. New Tauri command `get_resolved_vct_root_dir`. (`preferences/+page.svelte`, `storage_ux.rs`)
- **Rust/Python parity test hardening for `canonical_class_prefix`** — Phase chat's v0.2.33 follow-up identified divergence between Rust and Python `_` handling in KG class-name generation. Investigation confirmed parity was already aligned in v0.2.15 by the shared `tests/fixtures/project_naming.json` parity-pinning. Pivot: added 5 boundary-case unit tests on the Rust side + 5 on the Python side + 7 new shared fixture rows so any future refactor reintroducing the legacy divergence fails loudly. (`project_naming.rs`, `tests/test_project_naming.py`)

### Fixed — a11y sweep (36 warnings → 0)

- All 36 v0.2.33-baseline svelte-check warnings cleared. Categories: `a11y_label_has_associated_control` (10), `a11y_click_events_have_key_events` (8 + 2 paired on `projects/+page.svelte:77`), `css_unused_selector` (12), `state_referenced_locally` (5). Patterns applied: read-only meta-rows in IdentityTab converted from `<label>` to `<span>` (no input being labeled); `<article>` with click handler in `projects/+page.svelte:77` switched to `<div role="button" tabindex="0">` + Enter/Space keydown (interactive content can't go inside `<button>`); modal click-stopPropagation overlays got paired `onkeydown={(e) => e.stopPropagation()}`; state-referenced-locally cases wrapped in `untrack(() => ...)` to declare intentional prop-snapshot init. (`accessibility-checker` skill methodology end-to-end across 20 Svelte files.) `<dialog>` native modal migration NOT done — separate v0.2.34+ refactor, out of scope.

### Notes

- **No legacy paid-module data shapes need supporting.** Paid-module install was never working end-to-end in the wild (no v0.2.7 user has ever successfully completed an install due to the v0.2.33 install-pipeline bugs above), so module-side backward-compatibility for pre-v0.2.7 data shapes is not required. The **launcher** side, however, fully supports update from v0.2.33-and-earlier: re-detect-at-update-time IS the migration path for the hardware-snapshot schema gap, and migration 023 (`module_mcp_tool_defaults`) is purely additive.
- **Module update path (v0.2.7 → v0.2.8 etc.) is fully tested and deployed.** UPSERT contract, manifest re-extract, DB migrations diff, container restart on version change, config tab re-render from disk — all have regression tests + the install-time hardware-redetect guard ensures the right variant is pulled even when a user updates between hardware changes.
- **Phase 0.E Part 2 (Rust callers bridging to the new Python CLI verbs)** is deferred to a future release, mirroring how Phase 0.B Part 2 was staged.

### Added — diagrams integration (Phases 0–3, completed in v0.2.34)

- **Mermaid + Excalidraw as first-class diagram artifacts.** Save `.mmd` / `.excalidraw` files under `.claude/diagrams/<category>/<name>.{mmd,excalidraw}` and they're auto-indexed for retrieval. Wrapper MCPs (`mermaid_proxy`, `excalidraw_proxy`) gate per-tool access via vct-hub allowlist; default-disabled per project, opt-in via the launcher's per-project DiagramsTab.
- **Metadata-by-construction**: scoped paths enforced at the wrapper MCP AND at a PreToolUse hook (defense in depth on both native `Write`/`Edit` and `mcp__mermaid__*` / `mcp__excalidraw__*` matchers). Filesystem layout becomes the primary tag source; derived metadata (Mermaid frontmatter, Excalidraw scene name + text labels) feeds per-project Weaviate `Diagrams_<Project>` collection. `hybrid_search` auto-includes it; results carry `result_kind="diagram"` discriminator.
- **`vco codegraph-diagram <symbol>`** (PRE-ALPHA) auto-generates Mermaid flowcharts from the Weaviate code-graph, writes under `.claude/diagrams/codegraph/`, indexes the result. Five pre-alpha surface warnings (CLI help, stderr banner, `.mmd` header comment, `/codegraph-diagram` slash skill, docs).
- **Snapshots + cascade delete**: every save creates a content-hashed snapshot (60 s throttle, dedup); deletes via `rm`/`unlink`/`mv` are detected by a new `post-file-delete.sh` PostToolUse(Bash) hook that cascades cleanup across SQLite, sidecar, and Weaviate.
- **`vco verify-diagrams <project>`** end-to-end smoke check for the diagrams installation (mirrors `verify-pins` / `verify-env-projection`).
- **Pinning manifest** (`bundled_mcp_versions.toml`) for all external npm packages used by VCO — exact-version installs only, sha-verified, drift-detected on `install.py --update`.
- **DB-as-source-of-truth contract** (`vco_lib/config_projection.py`) — launcher SQLite is the single source for per-project canonical env; `apply_project_env` is the only legal writer to `.claude/settings.json`, `.claude/env`, `.vscode/settings.json`. CI lint enforces single-writer discipline.
- **Conditional `{{#if_module_active}}` template primitive** in `templates/CLAUDE.md.template` — sections rendered only when their module is active per `project_modules` table.

### Diagrams notes

- **Sidecars (`.claude/diagrams/<file>.meta.json`) are tracked by git** by design — they're the portable metadata that travels with diagrams across machines. Users who prefer not to track them can add `.claude/diagrams/**/*.meta.json` to their `.gitignore`; the indexer regenerates them deterministically from SQLite + file content on next save.
- **Pre-alpha**: codegraph→Mermaid output is experimental; verify against source before sharing.
- **Excalidraw vendored fork** (`vco_lib/excalidraw_mcp_fork/`) — `excalidraw-mcp-server@2.0.0` bit-identical from npm; MIT-licensed, AGPL-compatible. (Moved here from `claude_mcp_servers/excalidraw_mcp_fork/` in v0.2.34 so it ships in the Python wheel.)

## [0.2.33] — 2026-05-25

A **pre-install catalog architecture** release. Replaces the v0.2.22–v0.2.32 builtin-placeholder pattern (which silently fell back to stale hardcoded metadata when the paid-module manifest couldn't be parsed) with a public Supabase edge function (`module-catalog`) backed by a private storage bucket. The launcher's GUI source code now carries zero hardcoded paid-module metadata; pre-install catalog tile data comes from the public endpoint, post-install metadata comes from the manifest extracted out of the pulled GHCR image. Plus a forward-compat `Unsupported` ConfigControl variant so a future module declaring a control kind the user's launcher doesn't recognize degrades gracefully instead of failing the whole manifest parse.

Triggered by v0.2.32 dogfooding: the launcher rejected RL v0.2.7's `tauri_command` chained-step manifest, silently fell back to the v0.1.1 builtin placeholder, and the catalog tile + tier resolution served wrong info for the admin-tier user. Two bugs same day — one schema gap (added Agent D), one architectural (added Agents A+B+B2+C+E). Plus a SubagentStart keyword-suggest hook so spawned subagents discover relevant skills/agents without the parent having to enumerate everything.

### Added

- **`module-catalog` Supabase edge function** (anonymous-callable, `verify_jwt = false` pinned in `config.toml`). Returns catalog metadata for any paid module the launcher knows about: id, name, version, tier requirement, hosts, install-time slice (container coords, gpu_image_variants, requirements). Source under `launcher/supabase/functions/module-catalog/`. Backed by a private storage bucket `paid-module-catalog` that module publishers update via their release CI after GHCR push. Anonymous reads are intentional — catalog metadata is marketing-tile-equivalent; the actual security gate is at `/rl-artifact-url` (license-validated GHCR token issuance), not at the catalog-read layer.
- **Launcher L0 client** at `launcher/src-tauri/src/commands/module_catalog_client.rs`. 15-minute in-memory TTL cache (in `app_state`, not persisted across launcher restarts), retry-with-backoff at 1s / 5s / 30s / 120s (mirrors v0.2.32 UB1's pattern), cache-poisoning protection (parse errors never overwrite a known-good cached value), `schema_version` mismatch handling (logs warning + best-effort renders known fields), `refresh_module_catalog` Tauri command bypasses cache.
- **Post-install manifest extraction** via `docker create <image>` + `docker cp <cid>:/app/vct-module.json <dest>` + atomic rename with `.bak` rollback. Hooked into `installer_engine::run_install` between `container_pull` and `apply_module_db_migrations`. New `module_manifest_extract.rs` module. The synth manifest used to drive `container_pull` is REPLACED by the extracted real manifest immediately after pull succeeds. Cross-runtime: works for both podman and docker.
- **Startup reconciler** at `launcher/src-tauri/src/commands/module_reconciler.rs`. Walks `module_installs` rows with `status='installed'` at launcher boot; if the on-disk manifest at `~/.vct/modules/<id>/vct-module.json` is missing (user manually deleted, install was interrupted), marks `status='broken'` so the catalog tile renders kind=`broken` with a "Reinstall needed" CTA. Bounded soft-fail; runs after hub start, before tray + GUI setup.
- **Cold-start install synthesis** at `launcher/src-tauri/src/commands/l0_manifest_synth.rs`. When a clean install has no on-disk manifest AND no dev-passthrough `paid-modules/`, builds a minimal `ModuleManifest` from the L0 install-slice (carries enough fields to drive `container_pull` + DB-row creation; everything else is defaulted). `gui::default()` + `db::default()` empty placeholders are replaced by the post-install extract. Bypasses `ModuleManifest::from_json`'s license-contradiction check (would reject valid Pro-tier modules with empty variant_ids).
- **3-phase manifest resolver** at `resolve_manifest_for_install`. Phase 1: existing on-disk installed manifest (re-install / repair case). Phase 2: dev-passthrough — `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1` env var + co-located `<install_path>/paid-modules/<id>/vct-module.json` (module-author dev flow). Phase 3: L0-synth (clean cold-start, real user). Pinned in tests: even when L0 advertises a newer version than installed, the installed manifest wins on re-install — fix-broken must not silently morph into upgrade.
- **`ActionDescriptor::TauriCommand` variant** in `vct-launcher-core::manifest`. Chained-action steps can now invoke Tauri commands by name (whitelisted: `module_*` prefix auto-accepted; explicit allowlist for others). Closes the v0.2.32 → v0.2.7 manifest-parse gap. `stop_on_failure` accepted as a serde alias for the canonical `rollback_on_step_failure` field name.
- **`ConfigControl::Unsupported` variant** with lenient `Deserialize` impl. When a manifest declares a control kind the launcher version doesn't recognize (e.g. RL v0.3.0 ships `file_drop_zone`, user on launcher v0.2.33), the per-control fallback renders a placeholder with "this control requires a newer launcher" + a per-section roll-up banner listing the count. Other controls in the same section render normally. Strict mode (`VCT_LAUNCHER_STRICT_MANIFEST=1`) restores the v0.2.32 behavior of failing the whole manifest parse on unknown kinds — useful for CI.
- **`is_module_licensed_v2(LicenseGateInput, &Db)`** — lighter struct API so L0-driven callers don't need a full `ModuleManifest`. Legacy `is_module_licensed(&manifest, &db)` kept as a shim. Admin tier auto-unlocks paid modules through this path; cold-start install for an admin-tier user now skips the "Activate license" step entirely.
- **`CatalogResponse` envelope** at the Tauri command boundary: `{ modules, l0_status, parse_errors, dev_affordance_hint }`. Replaces the bare `Vec<ModuleCatalogEntry>` v0.2.32 contract. `l0_status` is one of `Ok | Stale | Unavailable`; `parse_errors` lists per-module manifest-parse failures; `dev_affordance_hint` populates when the dev `paid-modules/` directory exists but the env var is unset.
- **Manifest-parse-error UI**: `ManifestParseErrorBanner.svelte` + `ManifestParseErrorModal.svelte` at the Modules tab header. Aggregated banner ("N module manifests couldn't be parsed") with click-through modal listing module_id + source path + error message. Errors also logged to `state/logs/launcher_errors.jsonl` (one JSON object per line, RFC 3339 timestamp) for postmortem. `$effect` is hash-guarded to prevent double-append on tab remount.
- **L0 status indicator** at `L0StatusIndicator.svelte`. Grey "Catalog cached Nm ago" for `Stale` state; yellow banner with Retry button for `Unavailable`; nothing rendered for `Ok` (default).
- **Dev-affordance toast** at `DevAffordanceToast.svelte`. One-shot toast on Modules tab mount when `paid-modules/` exists without the env var. Dismissal calls `dismiss_dev_affordance_hint` Tauri command + persists in `app_state`.
- **Per-section Unsupported roll-up banner** in `ModuleConfigTab.svelte`. When a section contains 1+ `Unsupported` controls, an amber banner at the section header summarizes the count alongside the per-control placeholders.
- **Manifest validation CI gate** (`.github/workflows/manifest-validate.yml`). Round-trip every paid-module fixture through `ModuleManifest::from_json`; deploys with strict mode (`VCT_LAUNCHER_STRICT_MANIFEST=1`) so the lenient `Unsupported` fallback doesn't mask schema errors during validation. Two new bins in `vct-launcher-core/src/bin/`: `validate-manifest` (round-trip checker) + `export-schema` (JSON Schema generator). `docs/schemas/vct-module.schema.json` committed; CI asserts drift-free via `cargo run --bin export-schema -- --check`.
- **Publisher image-presence script** at `docs/publisher-ci/validate-manifest-in-image.sh`. Paid-module publishers wire this into their release CI to assert the manifest is shipped at `/app/vct-module.json` inside the container image before pushing to GHCR.
- **`SubagentStart` keyword-suggest hook** (`templates/hooks/subagent-start-suggest.sh` + `.ps1`). Mirrors the existing `UserPromptSubmit` keyword-suggest behavior, but fires when a parent Claude spawns a `Task`/`Agent` subagent — injecting matching agent + skill suggestions into the subagent's context. Delegates to the existing `agent-skill-keyword-match.py`; new `--skills-only` flag suppresses agent suggestions when the subagent's tool list lacks `Agent`/`Task` (skill suggestions always emit). 14 new tests.

### Fixed

- **L1 (catalog version shadow) + L2 (admin tier shows Activate License) — final structural fix.** v0.2.32's Agent A surgical fix (on-disk manifest overrides builtin placeholder when found) was correct as defense-in-depth but didn't address the root cause: real users don't HAVE the on-disk manifest pre-install (the manifest ships inside the private GHCR image; the launcher pulls + extracts via the v0.2.33 post-install flow). v0.2.33 removes the hardcoded `vct-rl-reranker` placeholder from `builtin_catalog_entries()` entirely + sources paid-module catalog metadata from the L0 endpoint. The builtin placeholder anti-pattern (see superseded KG node `builtin-catalog-placeholder-shadow-bug`) no longer applies.
- **L11 (Home right-rail "Installed" for never-installed module).** Root cause traced to `RightSidebar.svelte::effectiveStage` (lines 86–95 pre-fix): when an `App` had no `stage` prop AND its id wasn't in a hardcoded set, the derivation fell through to `'shipped'` which the Status row rendered as unconditional "Installed". Any L0-discovered paid module hit this. Fixed by adding `catalogKind` prop threaded from `ModuleCatalogEntry.kind`; both the home tile badge and right-rail status now derive from the same source.
- **v0.2.32 manifest parse rejection** of RL v0.2.7's `chained_action.steps[].kind == "tauri_command"`. Closed by the new `TauriCommand` ActionDescriptor variant + `stop_on_failure` alias. The exact v0.2.7 fixture used to fail under v0.2.32 is now committed at `launcher/src-tauri/vct-launcher-core/tests/fixtures/manifests/vct-rl-reranker.v0.2.7.json` and exercised by the CI gate.
- **`rl-pull-token` planning-doc placeholder → `rl-artifact-url`** (the actually-deployed function name). Caught during pre-push dashboard review; fixed across L0 seed JSON, `config.toml` comment, `module-catalog` source comment, and the cross-team instructions doc. KG had zero stale references. Documented as a generalizable discipline in the new node `planning-doc-names-must-match-deployed-reality`.

### Changed

- **`find_manifest` split into two functions**. Pre-install path (`resolve_install_metadata` / `resolve_manifest_for_install`) reads from the L0 client; post-install path (`find_installed_manifest`) reads from `~/.vct/modules/<id>/vct-module.json`. Single-source-of-truth precedence locked: pre-install catalog → L0; installed-state → `module_installs` table; dispatcher / config-tab / db migrations → on-disk manifest; builtins → CARGO_PKG_VERSION + repo-root manifest.
- **`module_installs.status` CHECK** extended via migration 021 to accept `'broken'` (used by the new startup reconciler).
- **Supabase `config.toml`** declares `[functions.module-catalog] verify_jwt = false` in source; no longer relies on the `--no-verify-jwt` CLI flag at deploy time. Removes the regression vector where a future deploy without the flag would silently re-enable JWT verification.
- **a11y quick wins**: 5 modal patterns gained `tabindex="-1"` + `role="presentation"`/`role="dialog"` attributes (`OrchestratorUpdateConflictModal`, `codegraph/+page`, `preferences/+page` × 2, `preferences/updates/+page` × 2, `services/+page`). Cleared 15 svelte-check warnings. Remaining 36 a11y warnings (mostly `a11y_click_events_have_key_events` + `a11y_label_has_associated_control`) need per-case judgment and are scoped for v0.2.34 a11y sweep — see `.claude/context/plans/v0.2.34-a11y-sweep-2026-05-25.md`.
- **Test counts**: launcher cargo lib **1059 passed** (was 996; +63), vct-launcher-core **317 passed** (was 280; +37), vct-hub **186 passed** (was 178; +8), svelte-check **0 errors / 36 a11y warnings** (51 pre-v0.2.33 → 36 after quick wins; remainder deferred to v0.2.34).

### Notes for module publishers

The architecture shift means existing paid-module-author workflows need three CI changes (one-time):

1. **Dockerfile**: `COPY vct-module.json /app/` in the final stage.
2. **Release CI**: after GHCR push succeeds, PUT the catalog JSON to `paid-module-catalog/<module_id>.json` via Supabase storage REST API authenticated with `SUPABASE_SERVICE_ROLE_KEY`. Critical ordering — never publish to L0 before the image is live.
3. **Manifest schema**: chained-action step commands should use the `module_*` prefix (auto-whitelisted); other names need explicit allowlist entries on the launcher side.

The dev affordance for module-authoring local workflows (co-located `paid-modules/` directory at the orchestrator install root) survives, gated behind `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1` env var. Without the env var, the launcher ignores any co-located dev manifests and uses L0 exclusively. A one-shot toast prompts dev users to set the env var when the directory exists.

Detailed migration instructions for the RL Reranker publisher are at `.claude/context/plans/instructions-to-rl-chat-v0.2.33-2026-05-25.md` (private; not in this public repo).

### Coincident parallel-track deliverables

The v0.2.33 tag also includes a substantial diagrams + packaging + config-projection work-stream shipped in parallel (Phases 0.A through 3, plus follow-up hardening). These commits were authored independently of the catalog-architecture work narrated above but landed in the same release window. Summarised here so the v0.2.33 tag's history is fully accounted for:

#### Diagrams integration (Phase 1 + 1.5 + 2 + 3)
- **DB substrate (Phase 1.1)**: migration `022_diagrams.sql` (6 tables) + 14 Tauri commands + Rust db layer + diagram-snapshot history. Scoped-path validation via `vco_lib/diagram_paths.py` + cascade-delete bash parser at `vco_lib/diagram_delete_parser.py` with chain walk + wrapper peel + security filter.
- **Indexing (Phase 1.5A + 1.5C)**: `vco_lib/diagram_indexer.py` + Weaviate `Diagrams_<Project>` collection + path-validation hook + `vco rebuild-diagram-index` CLI + `hybrid_search` diagrams support.
- **Conditional CLAUDE.md template primitive (Phase 1.5B)**: `{{#if_module_active}}` directive + re-render CLI + per-project active-module gates.
- **GUI (Phase 1.3)**: `DiagramsTab.svelte` + `PermissionsTab` MCP-Tools sub-section.
- **Excalidraw (Phase 2)**: vendored MIT fork at `vco_lib/excalidraw_mcp_fork/` + `claude_mcp_servers/wrappers/excalidraw_proxy.py` + embedded React-in-Svelte `ExcalidrawEditor.svelte`.
- **codegraph→Mermaid (Phase 3, PRE-ALPHA)**: `vco codegraph-diagram` CLI + slash skill + auto-index pipeline. Marked PRE-ALPHA on 5 surfaces (CLI help, stderr banner, `.mmd` header, slash skill, docs) to signal API instability.
- **Wrapper MCP base (Phase 1.2)**: `claude_mcp_servers/wrappers/_base.py` + `mermaid_proxy.py` + vct-hub allowlist route for module-contributed MCPs.

#### Packaging + CLI (Phase 0)
- **`vco` Python CLI via hatchling**: `pyproject.toml` + 5 subcommands (`verify-pins`, `verify-env-projection`, `verify-diagrams`, `rebuild-diagram-index`, `codegraph-diagram`). Drops the legacy `scripts/vco` shim path.
- **`bundled_mcp_versions.toml` pinning manifest** + Rust + Python parsers + `_install_pinned_npm` enforcement.
- **`vco_lib/config_projection.py`**: single-writer contract for per-project canonical env across 3 surfaces (`.claude/settings.json env` / `.claude/env` / launcher `app_state`). CI lint enforces.
- **`vco_lib/env_template.py` (Phase 0.D)**: sibling for the bare `.env` template with bracket-marker semantics.
- **Phase 0.B Part 2**: legacy Rust env writers migrated to Python subprocess; `_LEGACY_PRODUCTION_WRITERS` allowlist is empty for Phase 0.B keys (regression-guarded).

#### Hardening (cr-b1..b5 + cr-r1..r7)
- **Wheel install fix** (cr-b3) + delete-parser hardening (cr-b4): manifest + vendored fork moved under `vco_lib/` for wheel-distribution; bash cascade-delete parser walks chains + peels wrappers + filters dangerous patterns.
- **Diagrams class name sanitizer unified** (cr-b2): shared JSON fixture pinning Python + Rust + MCP all sanitize to the same string. 67 cross-language parity tests.
- **`config_projection` edge fixes** (cr-b1 + cr-b5 + cr-r1..r7): missing helpers, `apply_project_env` signature alignment, tolerate-missing-diagram-access-table guard, and 7 single-thread code-review fixes.

#### Hooks
- **`templates/hooks/pre-diagram-path-validation.{sh,ps1}`**: native Write/Edit + `mcp__mermaid__*` + `mcp__excalidraw__*` matchers — defense-in-depth path validation.
- **`templates/hooks/post-file-edit.{sh,ps1}`** extended: indexer + snapshot serial chain (R2 race fix).
- **`templates/hooks/post-file-delete.{sh,ps1}`** (new): cascade-delete SQLite + sidecar + Weaviate via `vco_lib.diagram_indexer drop`.

#### KG + docs
- New node `knowledge/concepts/config-projection-contract-2026-05-24.md` documenting the single-writer contract + Phase 0.B Part 2 + Phase 0.D sections.
- Extended `parallel-pr-coordination-gotchas-2026-05-10.md` with §6 / §7 (audit-after-merge) / §8 (shared working tree between chats) / §9 (MCP-matcher gaps).
- Extended `claude-code-hook-input-output-contract.md` with consumer-side gotchas §C1 / C2 / C3.

The catalog-architecture work and the diagrams work touched disjoint code paths (the only overlap was append-only registry files like `commands/mod.rs` + `lib.rs` + `db/migrations.rs`, all benign). Both work-streams ship together under v0.2.33.

## [0.2.32] — 2026-05-24

A **manifest-renderer + paid-module-v0.2.7-unblock** release. Closes 7 launcher items (L1–L7) requested by the RL module owner's post-v0.2.6 GUI test, the two real-world update-detection bugs surfaced during v0.2.30→v0.2.31 dogfooding (UB1+UB2), the GUI-tour polish backlog (V1+V2+E1+E2+M1), and the v0.2.31-deferred License-dialog UI half (D1+D2).

Generic primitives — chained_action, info_dynamic, date_picker, multi_select metadata+filter, per-section project picker — are NOT RL-specific. MAO and future paid modules adopt them with zero new launcher code.

### Added

- **`chained_action` ActionDescriptor variant** — generic chained-action primitive. Executes `steps[]` serially; each step's response is threaded into the next step's body via `{{previous_step.<field>}}` / `{{step.N.<field>}}` placeholders. Optional `polling` attaches to the FINAL step. `rollback_on_step_failure` reserved for v0.2.33+ (no effect today). Replaces what would have been per-module specialize Tauri commands — every future paid module gets multi-step actions for free.
- **`info_dynamic` ConfigControl kind** (L4) — read-only display bound to a `module_db` source via Agent J's `module_db_read_row` Tauri command. `{{project_id}}` substitution in `source.key`; `format` template (`"{value}"` by default); `fallback` string when read returns null. Sections containing at least one `info_dynamic` render a `↻` refresh button next to the section title; clicking it re-fetches every dynamic control in the section.
- **`date_picker` ConfigControl kind** (L5) — native HTML `<input type="date">` with optional min/max + keyword defaults (`"today"`, `"30_days_ago"`, `"90_days_ago"`). Persists via `set_module_setting`. Sibling controls can reference the value via `{{control:<id>}}`.
- **`multi_select` per-option metadata + filter predicate** (L6) — `SelectOption` extends from `{value, label}` to `{value, label?, badge?, meta?}` (back-compat: bare-string lists `["a", "b"]` still parse via custom serde deserializer). New optional `filter: {kind: "match", meta_field, equals_runtime}` predicate hides options whose meta doesn't match a runtime-resolved value (e.g. `container.active_embedding`). Runtime resolution via new `module_get_runtime_value` Tauri command.
- **Per-section project picker auto-wrap** (L3) — any `gui.config_tab` section whose controls reference `{{project_id}}` (in any action's path / body / next_action / chained_action.steps) automatically renders a section-local project picker above the controls. Overrides the global `selectedProject` store for dispatches originating from that section. Auto-injection (no manifest opt-in needed) so existing modules get this for free. Picker-driven project changes clear stale per-control state + re-fetch values + options for the newly-picked project.
- **`{{embedding_source_from_project_kg_binding}}` placeholder** (L7) — client-side substitution at dispatch time, resolved via new `get_project_embedding_source(projectId)` Tauri command. Cached per project_id; lazy-loaded on first reference. Lets a manifest button dispatch to the right per-embedding-source endpoint without hardcoding `"qwen3"` / `"arctic"` / `"openai"`.
- **`module_download_default_weights(module_id, project_id, embedding_source)` Tauri command** — the launcher-side half of the C1 download-flow decision (Option B confirmed). Calls Supabase `rl-latest-weights` with the launcher's tier JWT → downloads .pt to `<vct_root>/modules/<module_id>/weights/<embedding_source>-<version>.pt` → returns `{local_path, version}` so a chained_action can thread it into a follow-up `/finetune` POST. Keeps the license JWT out of paid-module containers. New `module_get_runtime_value(projectId, key)` companion for the L6 filter resolver.
- **Per-module licenses section in License dialog** (D1) — surfaces `tier_cache.module_licenses` (data layer landed in v0.2.31 Agent B) as a UI list with per-row Refresh + Deactivate buttons. New Tauri commands `get_module_licenses`, `module_license_refresh`, `module_license_deactivate`. Empty state explains tier→entitlement semantics.

### Fixed

- **L1 — Modules tab showed `v0.1.1` instead of v0.2.6 for RL Reranker** + **L2 — "Activate License" button shown despite admin tier**. Both were the same single bug: `builtin_catalog_entries()` hardcoded a `vct-rl-reranker` placeholder with `version: "0.1.1"` + `is_licensed: false`, and `list_module_catalog`'s scan loop SKIPPED the on-disk manifest at `if out.iter().any(|e| e.id == manifest.id)`. Now the on-disk manifest WINS for catalog-display fields (version, name, description, hosts, license) via override + `is_module_licensed()` re-evaluation, while preserving the builtin's UX-only fields (kind, parent_id, cta_route). Three new regression tests pin the inversion. The v0.2.31 plan item #20 Fix #1 had promised this but Agent I shipped DB-migrations only.
- **UB1 — `check_for_launcher_update` lost the update badge on transient network blips at boot**. `fetch_upstream`'s single-shot `git fetch --quiet vco_upstream` was never retried; a launch-time DNS hiccup or VPN coming-up race set `remote_ahead = false` until launcher restart. Replaced with `fetch_with_retry` backoff at 1s, 5s, 30s, 120s (~156s wall time on full failure). Soft-fail unchanged on final exhaustion. Cfg(test)-overridden delays (ms instead of s) keep tests sub-200ms. 4 new tests.
- **UB2 — update badge never refreshed after initial mount**. Once `+page.svelte:onMount` ran on a fresh launcher, the badge state was frozen until next launcher restart. Moved the initial `orchestrator.checkStatus()` into root layout's `onMount` so it fires regardless of arrival route, + added hourly `setInterval` to re-check while the launcher stays open.
- **V1+V2 (Modules page polish)** — search input alignment + redundant "Create a project first" / "No modules in catalog yet" empty-state collision (now picks the right one based on actual cause).
- **E1 — `CdiDriftModal.svelte` bypassed the `$lib/tauri` wrapper** — imported `invoke` from `@tauri-apps/api/core` directly so browser-mode threw `TypeError: Cannot read properties of undefined`. Fixed to use the wrapper consistently with every other call site.
- **E2 — `preferences/updates`'s `loadCached()` logged Tauri-missing as `error`** instead of `warn`. Now matches the consistency rule from other Tauri-aware surfaces.
- **D2 — Activation modal's input placeholder `XXXX-XXXX-XXXX-XXXX` was misleading** (Lemon Squeezy keys are 32-hex UUID-shape; our own Supabase keys may differ). Replaced with neutral `"License key…"`.

### Changed

- **M1 — Inconsistent `<title>` tags across routes**. Added per-page `<svelte:head><title>…</title></svelte:head>` to 14 routes (audit / codegraph / coordination / glossary / hub / kg / mcp / modules / preferences / preferences/retrieval / projects / services / store / telemetry). Format: `"<Page Name> — VCT Launcher"`. Invisible inside Tauri windows; cleans up browser-mode debugging UX.
- **Test counts**: launcher cargo lib **996 passed** (was 966; +30), vct-launcher-core **280 passed** (was 267; +13), vct-hub **178 passed** (unchanged), svelte-check **0 errors / 51 pre-existing warnings**.

## [0.2.31] — 2026-05-23

A general-purpose **paid-module lifecycle release**: every step from licensed-discovery through install → update → uninstall is now declarative-manifest-driven and works for any module, not just RL Reranker. Plus several silent-bug closures surfaced by the v0.2.27–v0.2.30 sprint (broken `dismiss-deferral` subcommand, license cache never written, Windows PS1 missing, hardcoded "Upgrade to Pro" copy).

### Added

- **`update_module_for_project` Tauri command** — the missing third leg of the install/uninstall/update tripod. Mirrors `install_module_for_project`. Reads the manifest's `UpgradeBlock` (declared in the schema since v0.2.0 but never invoked): runs `pre_upgrade` commands → re-fetches the artifact (git pull / podman pull / Local no-op) → runs `post_upgrade` → optionally runs `migration_script`. Falls back to a warning-emitting uninstall+reinstall when `manifest.upgrade` is absent. Wired into `lib.rs` handler list. Visible in the GUI as **"Update vX → vY"** button on a module card when the installed `module_version` is less than the catalog version.
- **`dismiss-deferral` argparse subcommand** in `vco_lib.project_init`. The 4 deferral-emission paths in `vco_lib/project_init.py` had been printing `python -m vco_lib.project_init dismiss-deferral --folder ... --condition-id ...` for users to resolve preserved-file deferrals, but the subcommand was never registered in argparse (running it returned "invalid choice"). Now registered with `--folder` + `--condition-id` + `--json` flags. Reuses existing `DeferralReport.read()` / `mark_resolved()` / `write()` primitives. Idempotent (silent exit 0 on missing file or no match; exit 1 only on genuine YAML corruption).
- **Module-deprecation warning surface** (3 layers, RL-chat spec). When a module's `runtime.update_endpoint` poller returns `deprecated: true`, the warning surfaces in three places: (1) **Launcher GUI** — amber `Deprecated` badge near the tier chip in the module catalog card, plus a `DeprecationBanner` rendered above the catalog grid for installed deprecated modules, plus one-shot desktop notification on first detection (gated by new `module_deprecation_seen` SQLite table). (2) **Claude-visible via RLClient** — new Tauri command `apply_deprecation_state` writes 4 `VCT_RL_MODULE_DEPRECATED*` env keys into the project's `.claude/settings.json env` block (canonical channel per CLAUDE.md). `RLClient._deprecation_warning()` reads them, and `hybrid_search` prepends a `[DEPRECATION WARNING] ...` line to the response on every turn (Claude sees it in context). (3) **Audit** — new `deprecation_events` SQLite table logs every state transition. Manual poller entry point only; cron wiring deferred to v0.2.32.
- **Per-module entitlements via `tier_cache.module_licenses`** — `license_refresh` now parses `body.module_licenses` from the Supabase `validate-tier` response and persists to the cache row (`is_module_licensed` already reads from this map but it was always empty). Variant-id-based per-module unlocks declared in `manifest.license.variant_ids` are now enforceable when the wire contract is extended. Out-of-the-box behavior unchanged because the edge function doesn't yet emit `module_licenses`; the parse is defensive (missing / wrong-shape → empty map).
- **`payment_alerts` Supabase table** + audit insert in `handlePaymentFailed`. The Lemon Squeezy `payment_failed` webhook was a silent 200 + `console.warn`; now durably records the event in a new RLS-protected table so downstream notifiers (email / Telegram) can poll for unhandled rows. Never throws; webhook still returns 200 with `audit_row_inserted: bool` in body.
- **`scripts/post-install-launcher.ps1`** — Windows parity helper that mirrors the existing `.sh` sibling (refreshes Desktop + Start Menu .lnk shortcuts via `WScript.Shell` COM). Pre-v0.2.31 the Windows direct-install branch at `install.py:8662` printed a `(TODO)` message and skipped the icon refresh. Now invoked correctly.

### Fixed

- **`request_pull_token` reads `~/.vibecoded/license_cache.json` from a file the Rust launcher never wrote.** Cache file is now written by `license_refresh` on every success path + removed on `license_deactivate`. Without this, the moment any paid-module image flips from public to private GHCR, every install would 401 because the token-gateway path always fell through to anonymous pull. File mode 0o600 on Unix (NTFS ACL inheritance on Windows). Soft-fail discipline preserved — write errors are logged via `eprintln!` and never propagated.
- **`uninstall_module_v2` now respects manifest's `UninstallBlock`** (declared in schema since v0.2.0 but never read). Honors `remove_install_dir` / `preserve_paths` / `deregister_mcp` / `clear_secrets` per manifest declaration. Falls back to legacy hardcoded behavior with warning when manifest is missing. `purge_data` parameter remains a separate concept (wipes `<vct_root>/data/<module_id>`; `clear_secrets` wipes keychain).
- **Tier-blind "Upgrade to Pro" copy** in `dashboard.rs` (3 hardcoded sites). New `tier_required_message(min_tier, feature)` helper renders Pro / MAO / Enterprise / Admin labels correctly. Mao-tier users no longer see "Upgrade to Pro" when their existing license already qualifies. ModuleCatalog button copy also fixed: not-installed + tier-required + unlicensed now reads "Activate license" instead of "Upgrade to Pro". Regression test pins the literal phrase against re-introduction.
- **`InstallStage::Failed` now emitted on error paths.** Previously declared `#[allow(dead_code)]` and never sent — UI progress bar saw the channel hang up instead of getting a terminal failure event. New `report_error` helper threads the Failed stage through `installer_engine::run`.
- **RL telemetry: session_id population (was 0.1% → ~100% expected)** + **embedding_source / dim / model on writer construction (was 31% stragglers → ~0%)** + **emb + cos_qn / cos_ql / cos_nl on log_retrieval rows (was 7.4% missing → ~0%)**. Container-side half shipped in vct-rl-reranker v0.2.4 (`session_id` kwarg on `/cache_nodes`); orchestrator-side completes the pair. `RLClient.cache_nodes` gains `session_id` kwarg; `weaviate_mcp/server.py` call sites pass `os.getenv("CLAUDE_SESSION_ID")`. `RLTelemetryWriter` construction reads `ACTIVE_EMBEDDING` + `EMBEDDING_MODEL` env vars with sensible defaults. `near_vector` calls feeding `log_retrieval` now pass `include_vector=True`; cosines computed via new `_cosine` helper (pure-python, no numpy).

### Changed

- ModuleCatalog button-state replaced with a state×license matrix: `Activate license` instead of `Upgrade to Pro`; `Update vX → vY` when manifest version is newer than installed; enabled-toggle + uninstall otherwise. `tierLabel()` helper centralizes Pro / MAO / Enterprise / Admin display.
- v0.2.31 prep commit `chore(v0.2.31-prep)` (830ad77) staged the `_installed_matches_template_history` heuristic + `_read_codegraph_binding_override` helper + codegraph correction logic in `vco_lib/project_init.py` ahead of agent parallelization. Heuristic walks `git log -50` on template path, sha256s historical blob contents, returns True iff any historical version matches the installed hash — heals manifest-untracked-but-VCO-shipped files (97-agent stale-on-disk symptom observed in the maintainer's clone pre-fix).

### Added (continued — post-tag-prep additions)

- **Citation-monitor fix routed through vct-hub** (Agent H). `claude_mcp_servers/weaviate_mcp/server.py:2644` was computing the Claude session-jsonl directory slug as `str(workspace).replace('/', '-')`. Claude Code's actual slug rule ALSO converts `_` → `-` (and `.` → `-`), so any workspace path containing underscores looked at a non-existent directory and the monitor timed out without writing the citation event. 97.7% orphan-citation rate at `~/.claude/retrieval_rl_data/rl_events.jsonl` is fixed. New `claude_session_dir_for(workspace_path)` helper in `vco_lib/project_config.py` + `vct-hub/src/config_api.rs` implementing the FULL slug rule. vct-hub's `/api/v1/projects/{id}/config` response gains a `claude_session_dir` field. MCP server gets `_resolve_claude_session_dir()` helper preferring hub resolution + falling back to local slug (with COMPLETE rule). 7 new regression tests (4 Python slug, 2 Python resolver, 4 MCP-side, 4 Rust). v0.2.28's asyncio strong-ref discipline left untouched.
- **Module-shipped DB migrations capability — Layer 1** (Agent I). Modules can now declare their own SQLite schema in `vct-module.json`:
  ```jsonc
  "db": { "migrations_dir": "db/", "namespace": "rl" }
  ```
  Launcher applies these at install + update via `installer_engine::run_install` + `run_upgrade`, idempotent via sha256-keyed `module_db_migrations` table (new launcher migration 019). Namespace enforcement: regex-based parser refuses `CREATE`/`ALTER`/`INDEX` on tables outside the manifest's namespace; FK references to launcher-owned tables (e.g. `projects.id`) are allowed; `DROP TABLE` refused unconditionally (forward-only migration discipline). Cross-module namespace-collision soft check: at apply time, the launcher queries `module_db_migrations` for any OTHER module claiming the same namespace and refuses with a structured error naming the offender. Modules access their tables via **5 new vct-hub REST endpoints** under `/api/v1/modules/{module_id}/db/projects/{project_id}/rows/...` with per-(module, project) bearer-token auth scoped via the new `module_access_tokens` table. Column projection via `?fields=col1,col2` on GET. Token refresh via `POST /api/v1/modules/{module_id}/token/refresh`. New `apply_module_db_migrations(module_id)` Tauri command for manual repair; new `issue_module_access_token(module_id, project_id)` for container startup. v0.2.31 ships with per-install shared secrets (32 random bytes, 1h TTL); JWT-signed tokens deferred to v0.2.32. 21 module_db lib tests + 27 hub-endpoint tests + namespace-collision regression tests.
- **Dashboard live reads via hub** (Agent J). New `module_db_read_row(module_id, project_id, table, key, fields?)` Tauri command issues/refreshes the per-(module, project) token from `module_access_tokens`, then HTTP-GETs the hub's `/rows/{table}/{key}?fields=...` endpoint. Bounded 5s timeout; 200 → `Some(Value)`, 404 → `None`, other failures → `Err(String)`. New `RlRerankerDashboardWidget.svelte` component reads `weights_version` + `last_training` on mount + on user-clicked `↻` refresh button. Soft-fail with "Container not running" / "—" placeholders when hub unreachable. `global_weights_status` display deferred to v0.2.32 (depends on RL's `0005_*.sql` not in v0.2.6).

### Removed (continued)

- **`module_weights_state` table dropped** (Agent J). The replacement `rl_weights_state` ships in vct-rl-reranker v0.2.6 via its own `db/0002_rl_weights_state.sql` migration applied at module install. Migration 020 drops the legacy table outright — paid-module v0.2.5 had zero production users, so no backfill is needed. The launcher's `signal_finetune` + `apply_weights_update` Tauri commands stripped of dead writes (~30 LOC removed); now pure orchestration (download .pt → call container's `/rotate_weights` → container does the DB write via vct-hub). The whole `module_weights_state.rs` Rust module + `WeightsStateRow` model deleted.

### Fixed (continued)

- **Pre-existing `module_supervisor.rs:612` compile failure** (hotfix `4ba96f2`). A v0.2.27 manifest schema extension added `RuntimeBlock::log_path_template` (Option<String>) but a test fixture in vct-hub wasn't updated. `cargo test --package vct-hub --lib` failed to compile on the v0.2.30 base. Adds `log_path_template: None` to the fixture.
- **Cherry-pick duplicate-field artifact** (hotfix `55d93b2`). Cherry-picking Agent I on top of hotfix `4ba96f2` caused git's auto-merge to accept BOTH additions of `log_path_template: None` instead of detecting the conflict → `E0062` field used more than once. Resolved by removing the duplicate.

### Versions

- Bump 0.2.30 → 0.2.31 in 6 build files (`launcher/src-tauri/Cargo.toml`, `vct-launcher-core/Cargo.toml`, `vct-hub/Cargo.toml`, `tauri.conf.json`, `launcher/package.json`, `vct-module.json`).
- Cargo.lock auto-synced.

### Test results (final, post-J)

- **launcher cargo test --lib: 966 passed** (929 v0.2.30 baseline + 37 across new module surfaces).
- **vct-launcher-core cargo test --lib: 267 passed** (+21 module_db_migrations tests + 3 migration 020 tests).
- **vct-hub cargo test --lib: 178 passed** (151 baseline + 27 module_db_api endpoint tests).
- **Python pytest: 2074 passed** (main suite + 8 rl_client deprecation + slug/resolver/citation tests).
- **svelte-check: 0 errors** (51 pre-existing warnings in unrelated files).

### Multi-repo ship coordination

This release ships in coordination with vct-rl-reranker v0.2.6 (paid module, private repo). The launcher's manifest-DB-migrations capability is dormant until any module ships a `db/` directory. The RL module ships `db/0001_rl_state.sql` + `db/0002_rl_weights_state.sql` in its v0.2.6 image; container's write paths use the new hub endpoints. The 16-point smoke-test acceptance checklist + T+0…T+2h30m ship sequence were coordinated cross-track.

### Known issues deferred to v0.2.32+

- `runtime.update_endpoint` polling task is NOT cron-wired (`module_update_poll()` exists as manual entry point only). Daily/weekly poll cadence + UI refresh of "Update available" badges queued for v0.2.32.
- Per-module license activation UI (the data-layer is now done — `tier_cache.module_licenses` populates from the Supabase response — but the launcher's License dialog doesn't yet show per-module activations or expose a "Activate per-module key" affordance).
- The `payment_failed` Supabase audit table is in place but no downstream notifier (email / Telegram dispatch transport) is wired. v0.2.32 will wire either Resend/Postmark email or a Telegram webhook.
- Deprecation-warning desktop notification path uses `console.warn` fallback when `@tauri-apps/plugin-notification` isn't installed. Adding the plugin + native notifications queued for v0.2.32.
- Divergence-modal "Try Merge again" footgun (queued from v0.2.30 backlog) and modal-positioning crop bug — still open, queued for v0.2.32.
- The full v0.2.32 backlog was tracked in the maintainer's planning notes.

## [0.2.30] — 2026-05-23

Critical fix for a silent KG-search misroute introduced by the install-flow plumbing. Pre-v0.2.30 every `install.py --update` run rewrote the orchestrator-root project's `.claude/settings.json` from the bundled template (which has no `env` block), wiping the user's `KG_COLLECTION` override. The downstream v0.2.29 backfill only added missing keys — never corrected an existing-but-stale value — so a user who had picked `<Project>_KnowledgeGraph` in the launcher Identity tab saw the binding silently revert to the orchestrator-root literal default on every update. `hybrid_search` returned 0 results because the MCP queried the wrong collection. **All users on v0.2.27–v0.2.29 with a customized `KG_COLLECTION` should update.**

### Fixed

- **`fix(install.py)` Settings.json template render now merges additively when settings.json exists**, instead of unconditional overwrite. Pre-v0.2.30 the orchestrator-root materialization step at the bottom of `_install_hooks_and_settings` called `settings_dst.write_text(rendered, encoding="utf-8")` unconditionally — wiping the user's `env` block (which the template doesn't carry) every update. Now: on existing files, the template's top-level keys (`$schema`, `permissions`, `hooks`, `_template_origin`) override the on-disk version, but every other top-level key (including `env`, `_comment`, `_env_comment`, any user-added field) is preserved. Fresh installs still see the template as-is. Pinned by a new regression test (`InstallSettingsTemplateMergePreservesEnvTests`).
- **`fix(vco_lib.project_init)` `_backfill_kg_collection_env_in_project` now CORRECTS existing-but-stale env values when launcher.db has a `manual_override` sentinel.** Pre-v0.2.30 the function only ADDED missing keys, never overwrote an existing-but-wrong value. The Rust seed-guard (v0.2.28) preserves user-customized `project_kg_bindings` rows on launcher boot, but if `install.py --update` had overwritten settings.json env BEFORE the seed-guard fired (which it did, see above), the env stayed wrong. Now the backfill reads each binding's `config_json.manual_override` sentinel and corrects env values that disagree with the DB. User-edited settings.json without DB override is left alone (no `manual_override` = no correction — respects the user's direct edit). 5 new regression tests in `ManualOverrideCorrectsStaleEnvTests`.

### Versions

- Bump 0.2.29 → 0.2.30 in 5 build files + `vct-module.json`.

### Test results

- 2042/2042 pytests pass (17/17 KG-backfill tests, 6 new)
- All 3 CI gates pass (set-discipline, OS-parity, managed-paths)

### Known issues deferred to v0.2.31+

(Surfaced during v0.2.29 dogfooding; see `.github/workflows/README.md` for the running backlog.)

- `merge_orchestrator_with_upstream` Tauri command (the divergence-modal "Merge" path) lacks `--autostash`. v0.2.29's autostash fix only patched `update_orchestrator`.
- Divergence-modal's "Try Merge again" button is a footgun — re-running always re-fails with the same error.
- Divergence modal positioning crops at viewport top (regression that survived v0.2.27's modal rewrite).
- Pre-merge `.from-upstream-<sha>` sidecars accumulate without GC.

## [0.2.29] — 2026-05-23

Same-day follow-up to v0.2.28's hook-parity-gate failure + critical fixes surfaced during maintainer-side dogfooding. Bundles installer autostash, per-session dedup state moved to project-local `.claude/state/`, RL launcher contracts (`min_launcher_version` enforcement + `/state_summary` consumer), keywords rolled out to all 97 agents/skills (PR #259), and CLAUDE.md disambiguation for the `kg-update-nudge` hook silences.

### Fixed

- **`fix(launcher)` `git pull --rebase --autostash` on `update_orchestrator`.** Pre-v0.2.29 the "Update orchestrator" button aborted with "cannot pull with rebase: You have unstaged changes" whenever the user's working tree had non-allowlisted uncommitted changes. Now safely stashes + restores via `--autostash`. Note: `merge_orchestrator_with_upstream` (the divergence-modal merge path) still lacks this — deferred to v0.2.31+.
- **`fix(hooks)` Per-session dedup state moved from `$TMPDIR` to project-local `.claude/state/`.** Pre-v0.2.29 dedup state for keyword-suggest, edit-cache, diff-context-inject, pre-tool-use lived under `$TMPDIR`. The OS may clear `$TMPDIR` on reboot, breaking dedup mid-session — but Claude Code persists `session_id` across restarts (resume feature), so dedup state MUST survive reboots. Project-local `.claude/state/` is gitignored, survives reboots, and is wiped only by the PostCompact hook (matching the canonical "context resets at compaction" semantic). Affected: `pre-tool-use.{sh,ps1}`, `pre-edit-context-inject.{sh,ps1}`, `diff-context-inject.{sh,ps1}`, `pre-compact-save.{sh,ps1}`, `post-compact.{sh,ps1}`, `agent-skill-keyword-match.py`. Added 14-day GC pruning to keep the dir bounded.
- **`fix(hooks)` `PROJECT_ROOT` resolution now prefers canonical `$CLAUDE_PROJECT_DIR`** (the launcher's source of truth for the active workspace) with SCRIPT_DIR-relative fallback for ad-hoc invocations.
- **`fix(hooks)` `agent-skill-keyword-suggest` matcher hardening**: case-INsensitive matching (was case-sensitive, crippled most realistic matches), README.md/README/ subdirs skipped in agent/skill discovery, per-session dedup with PostCompact reset + path-traversal-safe `session_id` validation.
- **`fix(hooks)` `agent-skill-keyword-suggest.sh` set-discipline**: `set -eu` → `set -euo pipefail` (closes the Hook OS-Parity Gate failure that flagged v0.2.28).

### Added

- **`feat(launcher)` Enforce module manifest's `compatibility.min_launcher_version` at install time.** Pre-v0.2.29 this field was deserialized but never read. Now refuses install with a clear error pointing the user at Settings → Updates → Update orchestrator. 5 `version_lt` tests.
- **`feat(launcher)` `GET /state_summary` consumer for vct-rl-reranker v0.2.3+.** New `dynamic_types_count` + `d1_marker_present` optional fields on `RlDashboardState`. Soft-fail to `None` on any probe failure path (timeout, 404, parse error). 3 wire-shape tests.

### Changed

- **`feat(templates)` Keywords frontmatter rolled out to all 97 agents/skills** (was 20 in v0.2.27). 1,107 (keyword, item) pairs, ~11.4 keywords per item, max-collision capped at 4. PR #259. Adds bullet-list output format with `short_desc:` scope hints.
- **`docs(CLAUDE.md)` `kg-update-nudge` silence-mechanism disambiguation.** Now lists all 3 paths: (a) write a real KG node [default], (b) transcript escape marker `[No KG update needed: <reason>]` [user-facing escape], (c) `KG_NUDGE_OFF=1` env [nuclear]. Corrected threshold (150k tokens → 175k work units first / 50k after). Applied in public CLAUDE.md + maintainer-side CLAUDE.md + `templates/CLAUDE.md.template`.

### CI / workflows

- **`chore(workflows)` Skip CI / CodeQL / Hook-Parity on pure-docs changes via `paths-ignore`.** Narrow allowlist of genuinely doc-only paths (`knowledge/**`, `docs/**`, `.claude/context/**`, top-level README/CHANGELOG/LICENSE/CLA/etc., `.github/ISSUE_TEMPLATE`). Critically does NOT include `**.md` or `templates/**` because many `.md` files are functional code (agents/skills frontmatter, CLAUDE.md, internal release notes) — flagged in the new `.github/workflows/README.md` to prevent future broadening.

### Versions

- Bump 0.2.28 → 0.2.29 in 5 build files + `vct-module.json` 0.2.21 → 0.2.29 (stale since v0.2.21 ship — manifest version had been forgotten in every release commit since).

### Test results

- 2036/2036 pytests pass
- 929/929 cargo tests pass

## [0.2.28] — 2026-05-23

Same-day chained release after v0.2.27 (see
[`knowledge/concepts/same-day-chained-release-pattern.md`](knowledge/concepts/same-day-chained-release-pattern.md)
for why), shipping: (a) Wave 2 D — `install-bundle --update` no longer
resurrects user-disabled agents/skills (the install-side companion to
the Wave 1 FS-disable mechanism shipped in v0.2.27); (b) Wave 2 E —
launcher-startup migration that converts any legacy `enabled=0` rows
from older installs into the `.disabled/` filesystem layout + hook
registration in both OS settings templates; (c) **launcher KG-binding
seed-guard** that stops every launcher boot from clobbering a
user-customized `project_kg_bindings(primary)` row with the
orchestrator-root default (live-reproduced bug: two parallel chats
seeing 0 KG-search results because the binding had been silently
rewritten from `VCODev_KnowledgeGraph` to
`VibeCodedOrchestrator_KnowledgeGraph` on every boot); (d) **`.claude/settings.json
env` KG-keys backfill** — `install-bundle --update` and `install.py --update`
now populate `KG_COLLECTION` / `SHARED_KG_COLLECTION` / `DEVELOPMENT_COLLECTION`
into the canonical per-project env channel from launcher.db's
`project_kg_bindings` table (source of truth); (e) two RL telemetry
fixes from the `rl-logging-audit-report-2026-05-23` audit: project-name
canonicalization via slug (was producing 4 distinct cohort labels for
the same project) and asyncio strong-ref for the citation monitor task
(was hitting 97.7% orphan-citation rate from GC dropping the
unreferenced `create_task` handles); (f) per-model chunker preset in
the KG sync script (was hardcoded to qwen3-specific `max_tokens=2500`
regardless of the active embedding model); (g) Windows-side hardening:
UTF-8 BOM added to `agent-skill-keyword-suggest.ps1` so PowerShell 5.1
on Win10/11 can parse the non-ASCII characters in it.

### Added

- **`feat(install)` `install-bundle --update` no longer resurrects disabled agents/skills** (Wave 2 D). Pre-v0.2.28 a bundle update would re-copy any template file missing from `.claude/agents/` or `.claude/skills/`, silently undoing a user's disable choice. Both install paths (Rust launcher populate in `project_state_populate.rs`, Python `install-bundle` in `vco_lib/project_init.py`) now check both the enabled location AND the `.disabled/` sibling before copying. A new `skip-disabled` action bucket in the bundle-op schema explicitly documents the no-op. 20 new tests covering single-file (agent) and whole-directory (skill) cases, POSIX + Windows path separators, and the corrupt "both locations exist" defensive skip.

- **`feat(launcher,install,docs)` Launcher-startup migration + hook registration for the FS-disable mechanism** (Wave 2 E). A one-time idempotent migration runs once per registered project on launcher startup to convert any existing `enabled=0` rows from older installs into the new `.disabled/` layout. The `agent-skill-keyword-suggest` hook is now wired into both `templates/settings.json.{linux,windows}.template` UserPromptSubmit blocks alongside its sibling hooks. Docs in the existing FS-disable section extended with the migration story + the keyword-suggest hook registration.

### Fixed

- **`fix(launcher-core)` KG-binding seed-guard: stop clobbering user-customized `project_kg_bindings(primary)` on every boot.** Pre-v0.2.28 `ensure_orchestrator_root_kg_binding` (in `commands/orchestrator_root.rs`) called `set_project_kg_binding` unconditionally; the underlying SQL is `INSERT ... ON CONFLICT(project_id, role) DO UPDATE SET collection_name = excluded.collection_name`, which clobbered any user-customized binding with the orchestrator-root literal default on every launcher boot. **Symptom**: KG searches silently returning 0 results across the board — the MCP would resolve `KG_COLLECTION` via the hub (or `.claude/settings.json env`), and the launcher kept pointing both at the wrong collection (`VibeCodedOrchestrator_KnowledgeGraph`) while the actual project nodes lived in `VCODev_KnowledgeGraph`. Violated Dev Constraint #8 ("User choices survive all updates"). The guard now reads the existing binding first via `list_project_kg_bindings`; only re-seeds when no binding exists OR the existing one still carries the auto-seed sentinel `auto_seeded_by: "ensure_orchestrator_root_kg_binding"`. Anything else (manual override, prior migration, future GUI picker) wins. 3 new regression tests (`kg_seed_guard_preserves_manual_override`, `kg_seed_guard_writes_when_absent`, `kg_seed_guard_idempotent_on_prior_auto_seed`).

- **`fix(install)` `.claude/settings.json env` KG-keys backfill.** New idempotent helper `_backfill_kg_collection_env_in_project` in `vco_lib/project_init.py` reads the launcher.db's `project_kg_bindings` table (canonical source of truth) and writes any missing `KG_COLLECTION` / `SHARED_KG_COLLECTION` / `DEVELOPMENT_COLLECTION` keys into the per-project `.claude/settings.json env` block. Wired into both install routes (Dev Constraint #5): per-project `install-bundle --update` calls it alongside the v0.2.11 `_backfill_code_graph_project_env_in_project`; the orchestrator-root `install.py --update` path calls it after its own backfill. User-set values are never overwritten — pure additive idempotent fill. Resolution chain when DB unavailable: existing `env.KG_COLLECTION` suffix-swap → explicit `project_name` arg → `env.PROJECT_NAME` → `folder.name` sanitized. `SHARED_KG_COLLECTION=""` (empty-string user-disable) is respected and never auto-filled. 11 new pytests covering missing file, unparseable JSON, all-three-present noop, partial-fill, db-source-of-truth, db-absent fallback, explicit project_name override, empty-string preservation.

- **`fix(weaviate-mcp)` RL telemetry project-name canonicalization via slug (audit finding #3).** Pre-v0.2.28 `_get_rl_telemetry_writer` preferred `project_display_name` from the hub, which produced 4 distinct cohort labels for the same project (`Claude`, `VibeCoded Orchestrator`, `VibeCodedOrchestrator`, `VCODev`) — depending on which workspace was opened when, plus migration history. Now prefers `project_slug` (stable lowercase-hyphen identifier, the same key `module_settings` uses for the global-training-source flag); env-fallback path sanitizes via `sanitize_for_weaviate_class` so multi-workspace setups still produce one cohort. Existing JSONL events with the old project labels need a separate one-shot migration (out of scope; tracked in `rl-logging-audit-report-2026-05-23.md`).

- **`fix(weaviate-mcp)` RL citation monitor asyncio strong-ref (audit finding #1).** Pre-v0.2.28 `_rl_cache_and_rerank` did `asyncio.create_task(_rl_answer_monitor(...))` without keeping a reference. Python's asyncio runtime tracks tasks in a `WeakSet`, so GC could (and did) collect them mid-poll, producing the "Task was destroyed but it is pending!" warning and silently dropping the citation event. Symptom: 97.7% orphan-citation rate (897 / 918 retrievals had no matching citation event in the trailing 50 MB of `rl_events.jsonl`). Fix follows the [standard pattern](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task): hold a module-level `set[asyncio.Task]` and `add_done_callback(set.discard)` so completed tasks are removed cleanly.

- **`fix(scripts)` Per-model chunker preset in the KG sync script.** Pre-v0.2.28 `templates/scripts/sync_knowledge_graph.py` hardcoded `Chunker(min_tokens=1500, max_tokens=MAX_EMBEDDING_TOKENS=2500, target_tokens=2500)` for every node and doc. That was correct for qwen3-embedding:0.6b (8k context, 2500 working limit) but wrong for 512-token models (would over-chunk ~5x) and wasteful for 32k+ models (under-uses capacity). Now uses two new helpers `_chunker_for(server)` and `_max_chunk_tokens_for(server)` that delegate to `Chunker.for_model(server.embedding_service.text_model_id)` and `chunking_preset_for_model(...)` respectively. The "fits in one chunk?" gate and the actual chunk size now come from the SAME preset (cannot drift). Legacy hardcoded value preserved as a fallback when `embedding_service` is None (early-init paths only).

- **`fix(hooks)` UTF-8 BOM added to `agent-skill-keyword-suggest.ps1`.** The file contains non-ASCII bytes; without a BOM, PowerShell 5.1 (the default on Win10/Win11) mis-decodes it as Windows-1252 and fails to parse. Pre-existing test `tests/test_ps1_utf8_bom.py` was already catching this — the file was committed without a BOM in v0.2.27. Re-saved with the BOM; test passes.

## [0.2.27] — 2026-05-22

Follow-up release the same day as v0.2.26, shipping: (a) two
post-tag-discovered Windows install-path bugs from v0.2.25/v0.2.26
(BOM fix + Python cp1252 reconfigure), (b) the
`events_paths_for` template token the RL module's v0.2.1 manifest
requires, (c) a divergence-modal rewrite that correctly handles
local-only files + retry state, (d) a KG env-propagation safety net
in the Weaviate MCP server, (e) Wave 1 of the agent/skill UX work
(FS-disable on toggle + keyword-suggest hook on UserPromptSubmit +
10 seeded agents/skills), and (f) docs updates clarifying the
canonical per-project env channel. (Wave 2 D + E — install-bundle
preservation + launcher startup migration + hook registration —
shipped in [0.2.28] same-day.)

### Added

- **`feat(launcher-core)` New `runtime.log_path_template` manifest field.** Optional. Single-brace closed-set tokens `{project_slug}` / `{project_id}`. Validated at `ModuleManifest::from_json` time via `validate_log_path_template` (rejects empty, double-brace, unknown placeholders, unclosed braces, no-placeholder templates). Render helper `render_log_path_template` does the per-project substitution. 12 new unit tests.

- **`feat(launcher)` New `{{events_paths_for:<control_id>}}` dispatcher template token.** Whole-string-only (embedded form rejected with a clear "must be the WHOLE string value" error because the resolution returns a JSON array). Resolution pipeline: read referenced control's array of UUIDs → walk each UUID through `db.get_project()` to get the slug → apply the module's `runtime.log_path_template` → return `JsonValue::Array`. 6 new dispatcher tests covering happy path + 5 error cases + nested-object recursion + unknown-token-prefix rejection. Implements the spec the RL module owner shared in `rl-events-paths-for-template-spec-2026-05-22.md`; unblocks the 2 stubbed retrain buttons in RL module v0.2.1.

- **`feat(launcher)` WebKit divergence-modal rewrite** (`OrchestratorUpdateDivergenceModal.svelte`, 325 → 788 lines). Sticky footer keeps action buttons visible when file lists expand; retry-aware state machine (after Merge fails once, Rebase becomes the primary suggestion + button labels swap to "Try X again"); separate sections for "Files where both sides have diverging history" vs "Files only on your clone" (the latter is collapsed by default since they can't merge-conflict); git stderr rendered in its own labelled `<details>` block, never concatenated into the file list; new "Open clone folder" link uses `@tauri-apps/plugin-opener::openPath` with clipboard fallback. Resolves 4 a11y warnings (backdrop click/keydown, modal role/aria, focus management). 0 svelte-check errors. (Frontend-specialist Opus agent, 2026-05-22.)

- **`feat(launcher)` Generic per-(project × module) divergence-files split.** The `update_orchestrator` Tauri command's `collect_diverged_files` previously used `git diff HEAD..upstream/branch --name-only`, which lists every file different between the two tips. Forks tracking paths the public repo doesn't (e.g. private fork directories) saw all those paths flagged as "diverged" in the modal — confusing because they can't merge-conflict (no upstream version). Rewrite anchors on the merge-base: returns `(upstream_changed, local_only)` tuple where `upstream_changed` = files upstream touched since fork (real merge candidates) and `local_only` = files locally touched that upstream never had (pure-local content). New `local_only_files` JSON field on the `orchestrator_update_non_ff` payload; the rewritten modal renders the two categories as distinct collapsible sections.

- **`feat(launcher)` Launcher-GUI "disable agent/skill" toggle now physically moves files.** Pre-v0.2.27 the per-project disable toggle only flipped a DB column with no effect on Claude Code itself (which discovers agents/skills by globbing the filesystem; it had no awareness of the launcher DB). Disabled items kept appearing in autocomplete, autonomous invocation, and `/agents` listing. The toggle now physically moves the `.md` file to a sibling `.claude/agents.disabled/<name>.md` (or `.claude/skills.disabled/<name>/`) directory that falls outside Claude's discovery globs. Re-enable reverses the move. ~1k LOC changes in `db/project_state.rs` + 14 new fs_disable_tests + 6 new populate_disabled_tests. (Wave 2 — install-bundle preservation + launcher-startup migration of legacy `enabled=0` rows + hook registration in settings templates — shipped in [0.2.28] same-day.)

- **`feat(hooks)` New `agent-skill-keyword-suggest` UserPromptSubmit hook** surfaces relevant agents/skills based on case-sensitive whole-word matches against their `keywords:` frontmatter. Pure filesystem contract: globs `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` — no DB lookup. Disabled items naturally fall outside the glob (per the FS-disable change above). Paired cross-OS scripts `templates/hooks/agent-skill-keyword-suggest.{sh,ps1}` shell out to `templates/scripts/agent-skill-keyword-match.py` (stdlib-only Python matcher with explicit word-boundary regex). Seeded discriminative keyword lists on 10 representative agents (brand-identity-architect, kg-navigator, landing-page-critic, etc.) and 10 representative skills (accessibility-checker, hpc-submit, k8s-manifest-reviewer, etc.) as proof of concept; rollout to the remaining ~35 agents and ~42 skills is incremental as owners pick keywords. 46 new tests (Python matcher + hook output shape + edge cases). (Settings-template registration in both OS templates shipped in [0.2.28] same-day.)

### Fixed

- **`fix(launcher)` Windows install.py crash at step `[5b/10]` after BOM fix** (commit `a5b2971`). `install.py` contained ~660 non-ASCII characters (arrows, em-dashes, check marks) in user-facing prints. Python on Windows defaults stdout to the locale's legacy ANSI code page (cp1252 on Western locales); printing `→` (U+2192) crashed the codec with `UnicodeEncodeError`. Reconfigures `sys.stdout` + `sys.stderr` to UTF-8 with `errors="replace"` immediately after the Python version sentinel, before any other import or print. POSIX no-op. (Originally a contributor commit `12dd3e3`; cherry-picked as `a5b2971` with author preserved.)

- **`fix(launcher)` Launcher belt-and-braces for the Windows install.py crash.** The launcher's `update_orchestrator` flow spawns Python via `tokio::process::Command`. To protect upgrades from v0.2.25 / v0.2.26 where the on-disk `install.py` pre-dates the in-Python UTF-8 reconfigure block, sets the Python I/O encoding env vars on the child env at all 6 Python spawn sites (`update_at`, `run_install_orchestrator_lightweight`, `run_hardware_reconfig`, `update_orchestrator`, both fallback re-spawns). POSIX no-op.

- **`fix(weaviate-mcp)` Empty-env safety + resolution-source tracking for `KG_COLLECTION`.** When the user's workspace declared `KG_COLLECTION=""` (or whitespace-only), the MCP server's `os.getenv("KG_COLLECTION", default)` returned the literal empty string — NOT the default — and the empty class name propagated into Weaviate, causing schema-fail. `_config_field(empty_means_unset=True)` now coerces empty/whitespace values to the documented default for keys where empty doesn't have semantic meaning (`KG_COLLECTION`, `DEVELOPMENT_COLLECTION`); keeps `SHARED_KG_COLLECTION=""` semantically (means "shared search disabled"). New resolution-source tracking: every collection name now carries an annotation (`src=env` / `src=hub` / `src=default`) that surfaces in (a) a startup log line `weaviate-kg: resolved collections (kg='...' src=..., ...)` so operators know what the subprocess actually picked up, and (b) annotated error messages on schema-fail (`Tried 2 collections: 'X' [self/KG_COLLECTION src=hub], 'Y' [peer/VCT_KG_ACCESS_LIST] — ...`) so the failure points at the right config layer. 13 new unit tests.

- **`fix(hooks)` `agent-skill-keyword-suggest` hardened with `command -v python3` / `Get-Command` fallback** when `_lib/find-python` is missing (partial-install edge case flagged in Wave 1 review). The hook no longer hard-fails on a half-set-up project that lacks the find-python helper.

### Changed

- **`docs(claude-md)` Clarified canonical per-project env channel.** Made it explicit that `.claude/settings.json env` is the canonical channel and that `.vscode/settings.json claude-code.env` does NOT propagate to MCP subprocesses on Linux (sentinel testing 2026-05-16 against Claude Code 2.1.143; pre-existing limitation, now surfaced in CLAUDE.md rather than only in PR-27's commit message). New precedence-order list (5 layers, highest-to-lowest) covering vct-hub → `.claude/settings.json env` → `.claude/env` → `~/.claude.json mcpServers.weaviate-kg.env` → bundled defaults. Also fixed 3 instances of `Vibecoded` → `VibeCoded` typo (drifted since v0.2.23).

- **`docs(troubleshooting)` New Windows recovery section** for v0.2.25 / v0.2.26 users hit by the BOM / cp1252 install crash. Three recovery options: (1) `git pull origin main` + re-run `first-install.bat`; (2) manual env-var bootstrap before `python install.py --update`; (3) download the v0.2.27+ release zip directly. Cross-references the in-launcher belt-and-braces protection added this release.

- **`docs(kg)` `module-contributed-gui-tabs.md` extended** with a v0.2.27 evolution subsection: `log_path_template` field + `events_paths_for` token semantics + closed-set token table (now 5 supported tokens) + RL manifest example showing how `control:src_projects` (raw UUID array) and `events_paths_for:src_projects` (per-project log-path array) compose cleanly + enumeration of the 5 dispatcher error cases.

### Migration notes

- **End users on Windows v0.2.25 / v0.2.26 stuck on `first-install.bat`**: see `docs/TROUBLESHOOTING.md` § "Windows: `first-install.bat` crashes with `UnicodeEncodeError`". Three recovery paths documented; recommended is `git pull origin main` + re-run.

- **RL module authors**: `runtime.log_path_template` is the new place to declare your per-project log-path convention. Closed-set tokens `{project_slug}` / `{project_id}` only. Use `{{events_paths_for:<control_id>}}` (whole-string only) in any descriptor `body` field to inject the resolved array. See `module-contributed-gui-tabs.md` § "v0.2.27 evolution".

- **Operators hitting "every configured collection schema-failed" on `hybrid_search`**: the MCP error message now points at the resolution-source for each tried collection. Look for the `weaviate-kg: resolved collections` log line at MCP startup (Claude Code's MCP log panel, or `~/.claude/logs/`) to see what the subprocess actually picked up + the source layer. Common cause: workspace env is in `.vscode/settings.json claude-code.env` (does not propagate); move to `.claude/settings.json env` via the launcher's Identity tab.

- **End users with previously toggled-off agents/skills**: the FS-disable migration runs automatically once per project on the first v0.2.27 launcher startup. No user action required. Toggled-off items move from being DB-flagged-but-still-discovered to physically out of Claude Code's filesystem globs. Re-enable in the launcher GUI to reverse.

- **No DB schema breakage**. The FS-disable migration is additive: existing toggled-off agents/skills with `enabled=0` are migrated to the `.disabled/` directory at first launch post-update; the DB column stays for back-compat with older launcher binaries.

## [0.2.26] — 2026-05-22

Headline release: a **generic declarative HTTP-action dispatcher** that
lets paid modules add new GUI controls without launcher rebuilds.
Every future paid module (`vct-coordination`, `vct-ecosystem-app-1`,
`mao`, …) now declares its config tab entirely in its
`vct-module.json` manifest; the launcher renders + executes
everything generically. The four reset/retrain Tauri command stubs
that were placeholders for exactly this dispatcher are deleted.

Also: a WebKitGTK + EGL pre-flight probe so a stale post-driver-upgrade
NVIDIA driver state no longer aborts the launcher at startup. The
probe is per-launch — once the user reboots and the driver state
clears, the GPU/DMABUF fast path returns automatically with no
persistent perf cost.

### Added

- **`feat(launcher)` Generic declarative HTTP-action dispatcher** (the v0.2.26 headline). One new Tauri command `module_dispatch_action(moduleId, projectId, action, value, siblingValues?)` executes any `ActionDescriptor::Http { method, path, body, polling?, next_action? }` declared in a paid module's `vct-module.json`. The launcher's renderer (`ModuleConfigTab.svelte`) routes legacy string-form actions to the existing `invoke(action, ...)` path and descriptor-form actions to the new generic command; back-compat is permanent for v0.2.20–v0.2.25 manifests. The descriptor supports `{{...}}` template substitution (closed set: `project_id` / `module_id` / `value` / `control:<id>`), polling with progress + failed Tauri events, and arbitrary-depth chained `next_action` bounded by `MAX_CHAIN_STEPS = 1024`. Implementation lives at `launcher/src-tauri/src/commands/module_dispatch.rs` (~1500 LOC + 31 tests including in-process axum mock servers). See [`knowledge/concepts/module-contributed-gui-tabs.md`](knowledge/concepts/module-contributed-gui-tabs.md) for the full wire shape + `docs/PAID_MODULE_DEV_CHECKLIST.md` for the module-author integration recipe.

- **`feat(launcher-ui)` Five new schema-rendered control kinds**: `text_input` (with optional apply/validate action), `number_input` (min/max/step), `status_display` (polled GET source + `render_template`), `file_picker` (Tauri native dialog, optional extension filter, directory mode), `link` (external via tauri-plugin-opener / internal via SvelteKit goto). New components under `launcher/src/lib/components/module-controls/`. Each is documented in `docs/PAID_MODULE_DEV_CHECKLIST.md`.

- **`feat(launcher-core)` Generic per-(project × module) port table** — migration 017 adds `module_ports(project_id, module_id, port, updated_at)` with `INSERT OR IGNORE` backfill from the existing `projects.rl_port` column. New helpers `db.get_module_port(...)` / `set_module_port(...)` / `ensure_module_port(...)`. The legacy `get_project_rl_port` / `set_project_rl_port` pair becomes thin wrappers — every v0.2.21+ hub call site compiles unchanged. Unblocks coordination + ecosystem-app-1 without per-module schema changes. See [`knowledge/concepts/generic-per-module-db-architecture.md`](knowledge/concepts/generic-per-module-db-architecture.md).

- **`feat(launcher)` WebKitGTK + EGL pre-flight probe** at `launcher/src-tauri/src/webkit_preflight.rs`. Linux-only (`#[cfg(target_os = "linux")]`); macOS/Windows no-op. Called from `main()` BEFORE Tauri init. Walks `/sys/class/drm/*` to find the primary GPU (`boot_vga=1`), maps to its `/dev/dri/renderD*` node, dlopens `libEGL.so.1` + `libgbm.so.1`, and calls `eglInitialize` against the GBM platform display. On the well-known `EGL_NOT_INITIALIZED` (0x3001) failure signature — most commonly: NVIDIA `apt`-upgraded its proprietary userspace without a kernel-module reload — sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` (and `__NV_DISABLE_EXPLICIT_SYNC=1` on Wayland) so WebKit falls back to its legacy renderer instead of aborting the process. Kill-switch: `VCT_WEBKIT_PREFLIGHT_OFF=1`. User-set `WEBKIT_DISABLE_DMABUF_RENDERER` is respected (the probe is a no-op when the user already chose). Live-verified against the broken NVIDIA driver state on the dev box on 2026-05-22. See [`knowledge/concepts/webkit-egl-preflight-probe.md`](knowledge/concepts/webkit-egl-preflight-probe.md).

- **`feat(launcher-core)` Manifest schema additions**: new `ActionRef` enum (untagged: `Legacy(String) | Descriptor(ActionDescriptor)`), new `ActionDescriptor::Http`, new `HttpMethod` / `PollingSpec` types, and the 5 new `ConfigControl` variants. The existing variants' `action` / `on_change` / `options_source` field types change from `String` to `ActionRef` — back-compat preserved by `#[serde(untagged)]`. 30 new unit tests covering each control kind, descriptor JSON deserialization, polling-spec defaults, chained-action depth, and legacy-form back-compat.

### Changed

- **`refactor(launcher)` Renamed `commands::rl_service` → `commands::module_service`** (~10 files, ~40 textual refs updated). The file's internals were already generic — v0.2.21's header noted "Today this module supports exactly one consumer — `vct-rl-reranker` — but the helpers are written against `ModuleManifest` so future container modules drop in without a code change". Now that v0.2.26 has dispatcher + module_ports for coordination + ecosystem-app-1, the file name matches the scope. Pure rename — no behavioural change.

- **`docs(paid-modules)` Extended `docs/PAID_MODULE_DEV_CHECKLIST.md`** with two new sections: "GUI tab integration via the declarative dispatcher (v0.2.26+)" (declarative-vs-legacy decision matrix, minimum example, polling example, port registration contract, template grammar reference, what-still-requires-rebuild list) and updates to the "When you add a new paid module" procedure (now includes explicit steps for declaring the GUI tab in the manifest + wiring port registration via `ensure_module_port`).

### Removed

- **`refactor(launcher)` Removed four RL stub Tauri commands** (`rl_reset_to_global`, `rl_reset_and_specialize`, `retrain_global_online`, `retrain_global_offline`) from `launcher/src-tauri/src/commands/rl_settings.rs` along with their `invoke_handler!` registrations and the now-orphan `RetrainResult` struct. They were placeholders for the now-shipped declarative dispatcher; the RL module migrates `paid-modules/vct-rl-reranker/vct-module.json` to `ActionDescriptor::Http` entries on its side. Preserved: `set_rl_use_global` / `set_rl_online_training_disabled` / `set_rl_global_training_source_flag` / `list_rl_global_training_source_projects` (these read launcher-side DB state the HTTP-only dispatcher can't express; legacy-routed permanently).

### Fixed

- **`fix(launcher)` Three pre-release review findings** (committed post-cross-agent merge as a single follow-up): (a) `substitute_string` previously cast each byte to `char`, mangling multi-byte UTF-8 sequences in embedded templates — switched to `&str` slicing between token boundaries; (b) `{{control:<id>}}` was documented but unreachable end-to-end because `SubstitutionContext::simple()` hard-wired a `|_| None` resolver — plumbed `sibling_values: Option<HashMap<String, Value>>` through the Tauri command + dispatcher and built a real resolver backed by the renderer snapshot with `module_settings` DB fallback; (c) `run_poller` terminated on a single non-2xx tick — added a consecutive-failure budget (`POLL_CONSECUTIVE_FAILURE_LIMIT = 5`) so transient 503s during container restarts no longer kill long-running polls. Five new regression tests covering all three.

### Migration notes

- **Module authors**: see `docs/PAID_MODULE_DEV_CHECKLIST.md`'s new "GUI tab integration via the declarative dispatcher" section + the dispatcher KG node for the wire shape. The default answer for "I want to add a setting to my module" is now **declare it in the manifest, not in launcher Rust**. Pre-existing modules using `ActionRef::Legacy(String)` keep working; migration is opt-in and per-control.

- **End users**: no action required. The migration 017 backfill is idempotent (`INSERT OR IGNORE`) and the legacy `projects.rl_port` column stays in place for back-compat. NVIDIA users who experience a launcher startup abort after an `apt upgrade` of `nvidia-driver-*` (the `EGL_NOT_INITIALIZED` crash) will now see the launcher start automatically — the GPU/DMABUF path returns on the next reboot.

- **No DB schema breakage**. Migration 017 only adds a new table; no existing tables modified.

## [0.2.25] — 2026-05-22

Follow-up release for the v0.2.24 main feature drop. Originally
attempted as `v0.2.24.1` (matching the v0.2.23.1 precedent of a
point-release tag), but the release-CI's tag-vs-Cargo-version match
check rejects 4-segment tags because SemVer (and therefore Cargo)
doesn't allow them. Versions properly bumped to 0.2.25 across the
6 standard pins.

### Fixed

- **`fix(launcher)` embedding-catalog Tauri command `ModuleNotFoundError: vco_lib`** (A0ter). The per-project Settings → KG/Codegraph tab previously showed a permanent warning banner ("discover exit 1: ... No module named 'vco_lib'") and "Loading…" dropdowns stuck indefinitely. Root cause: `commands::embedding_catalog::run_discover` spawned `python -m vco_lib.embedding_service discover` without setting `cwd` to the orchestrator clone root, so Python's implicit-namespace-package resolution couldn't find the in-tree `vco_lib/` directory. Fix: resolve the clone root via the existing DB-cached `installer::resolve_install_root_sync(&db)` helper (the same pattern the v0.2.23.1 manifest scanners use) and pass it as `cwd`. Best-effort: if no clone root is discoverable, falls through with no cwd set — matches the pre-fix failure mode rather than degrading further.

### Changed

- **`feat(launcher)` Orchestrator Core tab → "Clone integrity"** (A0bis). The Orchestrator Core tab (shipped in v0.2.20, fixed in v0.2.23.1) previously hosted per-project actions (Rebuild KG / Check duplicates / Re-analyze code / Prune stale codegraph) that DUPLICATED controls already on the KG/Codegraph tab, plus a Diagnostics section (health-probe + open-logs). Honest scope audit found only 2 features that are genuinely root-clone-only:
  - **Re-detect orchestrator root** — re-runs the launcher's `current_exe()`-walk discovery + refreshes the cached `launcher.install_path` app_state entry. Use when the launcher's cached install path is stale (clone dir renamed, moved, or copied to a different location).
  - **Validate clone manifest** — parses `vct-module.json` at the active clone root and surfaces schema errors. The launcher's catalog renderer silently skips a malformed manifest (module-contributed tabs disappear); this command makes the failure explicit.

  Tab renamed `"Orchestrator core"` → `"Clone integrity"` to reflect the new scope. Per-project actions removed from the manifest (duplicates with KG/Codegraph tab). Diagnostics deferred to a Services-tab follow-up; the underlying `orchestrator_health_check` + `orchestrator_open_logs` Tauri commands stay registered. Manifest test (`orchestrator_core_manifest_with_gui_tab_deserializes`) updated to pin the new 2-section / 2-button shape.

- **`feat(launcher)` B4 modal cosmetic pass** — `OrchestratorUpdateDivergenceModal.svelte` + `OrchestratorUpdateConflictModal.svelte` now use VCT color tokens (`--color-bg2`, `--color-text`, `--color-mid`, `--color-teal`, `--color-pink`, `--color-card`, `--color-border`) instead of hardcoded hex values (`#1a1a24`, `#e8e8ee`, `#ccc`, etc.). Fix for the header-truncation symptom (modal could overflow viewport when the diverged-files `<details>` was expanded): added `max-height: 90vh` + `overflow-y: auto` to the modal containers. Primary action button switched from generic blue to `--color-teal`; pink accent retained for the conflict modal (conflict severity signalling).

### Migration notes

- Existing projects with the "Orchestrator Core" tab visible in per-project Settings will see it renamed to "Clone integrity" on next launcher restart. The tab's contents change from 6 buttons + 3 sections to 2 buttons + 2 sections — the per-project actions removed live in the KG/Codegraph tab (same `Re-build code graph` / `Re-sync KG` / `Re-build KG summaries` controls); the Diagnostics health-probe + log-dir actions are still available via the orchestrator-health-check + orchestrator-open-logs Tauri commands (a Services-tab UI for them is tracked as a follow-up for v0.2.25).

- The embedding-catalog dropdowns ("Embedding model" in both Knowledge Graph + Code Graph sub-sections of the KG/Codegraph tab) will populate correctly on next launcher restart. No user action required — the fix is launcher-side only.

- No DB schema changes. No template propagation needed (per-project install-bundle is unaffected).

## [0.2.24] — 2026-05-22

> CHANGELOG drift note: 0.2.21, 0.2.22, 0.2.23 ship in git history but
> aren't expanded in this file yet. The git tags (`v0.2.21`, `v0.2.22`,
> `v0.2.23`) + GitHub Release notes are the interim source of truth.
> Backfill tracked as a follow-up.

This release lands two architectural fixes plus a handful of
peer-review-deferred cleanups. The headline items: a per-path 3-way
merge for user-editable orchestrator-root files (§A0 — solves the
"git pull would overwrite your CLAUDE.md edits" wall every 3rd-party
user hit) and a per-collection schema-skip + failure-mode telemetry
fix in the Weaviate MCP (RL-defect-2026-05-22 — restores the
`rl_events.jsonl` corpus that the RL module's qwen3 training run
depends on).

### Added

- **`feat(launcher)` per-path 3-way merge during orchestrator-root updates (§A0)** (`0f751da`, `a6144c5`, `af423e8`, `2b3d757`, `97f8aa8`, `89f2410`, `348fef1`, `632c43e`). New `claude_mcp_servers/.../git_user_editable_merge` module + integration into `update_orchestrator` and `merge_orchestrator_with_upstream`. For each file in the diff between `HEAD` and `vco_upstream/<branch>` that matches a hardcoded allowlist of user-editable paths (`CLAUDE.md`, `CLAUDE.local.md`, `knowledge/**/*.md`, `.claude/CONTEXT_STATE.md`, `.claude/MEMORY.md`, `HANDOFF-*.md`), runs `git merge-file --stdout` with BASE/OURS/THEIRS. Clean merges land in the working tree and get staged + committed via a synthetic `vco: pre-merge user-editable files via A0 (<ts>)` commit (`VCO Orchestrator <orchestrator@vibecoded.tools>` author, `--no-verify`). Conflicts write the upstream content to `<path>.from-upstream-<sha>` sidecar files + emit `orchestrator_user_modified_preserved` deferral entries (via the existing `UPDATE_DEFERRED.md` surface) — local content is NEVER auto-overwritten. After pre-merge, `update_orchestrator` smart-routes through `git pull --rebase` (replays the synthetic commit onto upstream tip → linear history) when a synthetic commit exists, else `--ff-only` (preserves the original strict semantics). The B4 divergence modal still fires for genuine divergence beyond the allowlist. 14 Rust tests (10 unit + 4 integration covering FF / merge / sidecar / non-FF-then-merge paths).

- **`feat(install-bundle)` orchestrator-orphan-deletion handling** (`af423e8`, `2b3d757`). Sibling to §A0: when an `install-bundle --update` finds a file in the project's `.vco-manifest.json` that no longer exists in the orchestrator's `templates/` set, the orphan-detection loop checks whether the installed file matches the manifest's prior-shipped hash. Identical → SAFE_DELETE. Divergent → PRESERVE + emit `bundle_user_modified_deletion_preserved` deferral. 7 new tests.

- **`feat(weaviate-mcp)` per-collection schema-skip + failure-mode telemetry (RL-defect-2026-05-22)** (`bbbcab8`). `_hybrid_search_body` and `_semantic_graph_search_body` no longer bubble `WeaviateSchemaError` from a single missing collection; they classify per-target, skip the offending class, continue with the rest of the fan-out, and surface the partial failure via new `failure_mode: str | None` + `failed_collections: list[str] | None` fields. When EVERY collection schema-fails, a degraded-mode telemetry event lands BEFORE the bubble re-raises. `_rl_cache_and_rerank` now always calls `writer.log_retrieval()` — including on the free-tier early-return path and with empty all_nodes. Unblocks the `~/.claude/retrieval_rl_data/rl_events.jsonl` corpus that the RL module's qwen3 training run depends on. 5 new tests pinning the contract (`test_weaviate_mcp_telemetry_on_failure.py`).

- **`feat(install-bundle)` detect + defer legacy `.vscode/settings.json` MCP_* keys (RL-defect Fix 2)** (`37d6c98`). Detection-only with deferral. Pre-v0.2.12 the launcher wrote MCP_WEAVIATE_SERVER / MCP_PYTHON / MCP_OLLAMA_SERVER / MCP_PYTHONPATH absolute paths into `.vscode/settings.json claude-code.env`. PR-27 (v0.2.12) removed that write because the `claude-code.env` channel didn't propagate to MCP subprocesses on Linux Claude Code, but the stale keys remain in pre-v0.2.12 projects, baking the user's on-disk layout into the project tree. `_detect_legacy_vscode_mcp_env_keys` + `_emit_legacy_vscode_mcp_env_deferral` surface the issue via a `legacy_vscode_mcp_env_keys_present` info-severity entry with an operator-driven `jq` cleanup recipe that preserves the rest of the file. Per user policy 2026-05-22: never auto-overwrite user-edited files. 8 new tests.

- **`feat(claude)` ship `autonomous-orchestrator` output-style** (`5160294`). New `.claude/output-styles/autonomous-orchestrator.md` static asset. Activates via `claude --style autonomous-orchestrator` or by copy/customize.

### Changed

- **`feat(resolver)` rate-limit schema-version drift warning across invocations (§A4)** (`3ad8fd9`). Hooks call `vct_project_config.{sh,ps1}` dozens of times per Claude Code session; the previous one-shot guard reset per-invocation, producing a stderr flood when the hub reports a schema_version higher than the client's `RESOLVER_PROTOCOL_VERSION`. Now routed through the existing `_emit_warning` + JSONL-backed suppression infrastructure with a stable cross-PID suppression key (`schema_version_drift_<hub_version>`); 5-min window per key; `VCO_HOOK_DEBUG=1` bypasses. Sibling `.ps1` got the cross-invocation rate-limit (it had no equivalent infra pre-v0.2.24). 7 new tests.

- **`refactor(install)` extract `_rebind_collection_names_to_on_disk_casing` helper (§B3)** (`288e0d9`). Deduplicates the two healing paths in `_self_heal_kg_bindings_on_update` (`project_kg_bindings` + `kg_collection_access`) into a generic `(db, table, project_id_col, collection_name_col, *, conflict_resolver=None)` helper. Privilege-rank collision resolution stays at the call site for `kg_collection_access` (write > read > none). 3 new helper-contract tests; existing 11 stay green.

- **`refactor(launcher)` extract `is_shared_kg_class_name` helper for case-tolerant shared-KG matching (§B4)** (`8635d65`). New helper in `vct-launcher-core::project_env_settings` consolidates the case-insensitive matching of `DEFAULT_SHARED_KG_COLLECTION` + `LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C` + `LEGACY_SHARED_KG_COLLECTION` (pre-v0.2.12 `VibeCodedTools_KnowledgeGraph`). Refactor of `commands/kg.rs::kg_list_collections` + `commands/maintenance.rs::parse_schema_response`. Closes a pre-existing strict-equality bug in `kg.rs` that demoted the shared KG from the "shared first" sort priority on case-different installs. 7 new unit tests.

- **`chore(launcher)` cleanup batch — subscribe-leak fix + dead-shim removal + SPDX sweep (§A2/A3/A5)** (`7a54986`). `/preferences/+page.svelte`'s two popover-era `.subscribe()` calls switched to `$effect` (auto-unsubscribes on component destroy — eliminates the per-mount leak). `ui.ts::closeSettings` removed (no callers anywhere); `SettingsSection` narrowed from 5-value union to just `'secrets'`. SPDX-License-Identifier header added to 5 `templates/scripts/*.py` files.

### Migration notes

- After installing v0.2.24, the FIRST `Settings → Updates → Update orchestrator` click that runs over a working tree with user-modified allowlisted files will produce a synthetic `vco: pre-merge user-editable files via A0` commit on the local clone. Subsequent updates with no allowlisted-file diff produce no synthetic commit. Users running `git log` will see this commit — that's expected and documented in the orchestrator-install-flow KG node.

- Free-tier installs now accumulate `rl_events.jsonl` events on every `hybrid_search` / `semantic_graph_search` call regardless of whether reranking happens (it doesn't on free tier) — the local-JSONL pipeline is gated only by the existing `RL_LOCAL_LOGGING_DISABLED` env-var opt-out (Preferences → "Collect retrieval data locally"). Users who opted out pre-v0.2.24 are unaffected.

- The new `failure_mode` + `failed_collections` fields are additive in the `rl_events.jsonl` schema. Offline trainers should filter events where `failure_mode is not None` out of training-pair construction (no positive/negative target signal) but keep them as a failure-rate / query-distribution metric.

- The legacy `.vscode/settings.json claude-code.env` MCP_* key detection emits an info-severity deferral on every `install-bundle --update` until the user removes the keys (or the whole `claude-code.env` block). The deferral provides an operator-driven `jq` recipe; the launcher does NOT auto-clean.

## [0.2.20] — 2026-05-19

This release lands the client-side support for the first paid module
(vct-rl-reranker) + a module-extensible GUI tab framework + AMD/ROCm
GPU support across the orchestrator's container stack. The paid module
itself ships from `hotak92/vct-rl-reranker` (private); the orchestrator
core (this repo) only knows how to talk to it.

### Added

- **`feat(rl_client)` AGPL-side adapter for the vct-rl-reranker paid module** (`467f681`). New `claude_mcp_servers/rl_client/` package with: `RLClient` async HTTP client (httpx) with disabled-mode + per-call fallback semantics; `schemas.py` Pydantic wire contract pinning `/cache_nodes`, `/rl_update`, `/health` shapes; `RLTelemetryWriter` fanning events to local JSONL AND the existing telemetry queue when upload consent is granted (query text OMITTED from queue payload — replaced by `query_length` summary; node titles + embeddings + scores preserved); `rl_logger.py` slim copy of the canonical AGPL logger. `claude_mcp_servers/weaviate_mcp/server.py::_rl_cache_and_rerank` now routes through `RLClient` instead of inline aiohttp POSTs. Free-tier users get "disabled mode" (returns inputs unchanged); Pro/MAO with container running gets real reranking. Cross-OS templates at `templates/scripts/rl_client_setup.{sh,ps1}` for per-project install.

- **`feat(launcher)` module-contributed GUI tabs framework** (`c17765c`). Modules can now declare a `gui.config_tab` block in their `vct-module.json` that the launcher renders into the sidebar without bundling Svelte. Five widget kinds (checkbox / multi_select / button / select / info), each carrying `tooltip: Option<String>` so authors can always provide mouseover help. Schema lives in `manifest.rs::GuiBlock`; renderer in `ModuleConfigTab.svelte`; merge logic in `Sidebar.svelte`; generic settings persistence via `get_module_setting` / `set_module_setting` Tauri commands. RL Reranker is the first consumer (its manifest ships from the private repo); orchestrator-core demonstrates the framework with its own KG + Code Graph + Diagnostics controls.

- **`feat(v0.2.20)` orchestrator-core gui.config_tab demo** (`4c2df3f`). Proves the framework generalizes beyond paid modules. New `commands/orchestrator_core.rs` exposes 6 Tauri commands: `kg_rebuild_current_project`, `kg_check_duplicates`, `code_graph_reanalyze_current`, `code_graph_prune_stale`, `orchestrator_health_check`, `orchestrator_open_logs`. Thin tokio-subprocess wrappers around `.claude/scripts/{kg-sync,kg-duplicates,code-graph-analyze}` + reqwest probes for Weaviate / Ollama / code-embed health. `templates/scripts/detect_duplicates.py` gained a `--json` flag for machine-readable consumption.

- **`feat(v0.2.20)` AMD/ROCm support + per-module VRAM threshold + ROCm overlay** (`75317eb`). Closes the AMD-vendor gap inherited from v0.2.9's Bug-K work. `gpu_policy::decide_gpu_mode` now takes `has_amd: bool` between `has_nvidia` and `has_apple_silicon`. `GpuMode::Gpu` renamed to `GpuMode::Cuda`; new `GpuMode::Rocm` variant. Precedence: user_override (explicit) → Apple Silicon → NVIDIA+VRAM → AMD+VRAM → CPU. NVIDIA wins when both NVIDIA + AMD present. `gpu.rs::query_amd_gpu_present()` probes `rocminfo` (canonical) + `/sys/class/drm/card*/device/vendor=0x1002` (fallback). `HardwareSnapshot.has_amd_gpu` added. `manifest::RuntimeBlock` extended with `min_gpu_vram_gb: Option<f64>` (per-module override, None → global 8 GB default), `gpu_optional: bool`, `gpu_image_variants: Option<GpuImageVariants>` (cpu / cuda / rocm tags). `install.py::_decide_gpu_mode` mirror updated; overlay picker prefers `docker-compose.rocm.yml` then falls back to legacy `amd-rocm.yml`. New `infrastructure/docker-compose.rocm.yml` overlay for orchestrator-core's `ollama` + `code_embed` services: `/dev/kfd` + `/dev/dri` device passthrough, `group_add: ["video", "render"]`, `cap_add: ["SYS_PTRACE"]`, `HSA_OVERRIDE_GFX_VERSION="10.3.0"` (conservative RX 6000-class default). 86 new/updated tests across `gpu_policy` + `gpu` + `manifest` + pytest.

- **`feat(v0.2.20)` gpu_image_variants → image tag dispatch** (`6d11024`). Final piece of the GPU plumbing. `installer_engine::run_install` now consults the host's `GpuMode` at container_pull time and picks the right variant tag (cpu / cuda / rocm) when the manifest declares `gpu_image_variants`. `install_module_for_project` reads the persisted `HardwareSnapshot` from app_state; falls back to `Cpu` when no snapshot exists. New helper `resolve_variant_tag(manifest, base_tag, gpu_mode)` returns the right suffix. Module-side: paid modules ship 3 OCI image tags per release (`:0.1.0-{cpu,cuda,rocm}`); the launcher picks the correct one. Legacy modules without `gpu_image_variants` continue using the single-tag flow unchanged. 5 new tests covering Cuda / Rocm / Cpu / Metal / no-variants paths.

### Migration notes

- Existing module manifests without `gpu_image_variants` are unaffected — the legacy single-tag image pull path remains the default. Only modules that opt into per-variant builds (today: vct-rl-reranker) trigger the dispatch.
- Modules that need a per-module VRAM threshold can declare `runtime.min_gpu_vram_gb` (e.g. `4.0`). Unset → global 8 GB default (calibrated for the Ollama + CodeSage + qwen3.5:9b stack).
- AMD users with sufficient VRAM who previously got "CPU-only" mode will now get ROCm-accelerated containers on next `redetect_hardware` + reinstall.
- The `RL_LOCAL_LOGGING_DISABLED` env var lets users opt out of local rl_events.jsonl collection via the new Preferences → "Local data collection" section.
- The orchestrator-core's own `gui.config_tab` adds a "Module configuration" sidebar group with the launcher's own surface. The group is hidden when no modules expose tabs (i.e. on a fresh free-tier install before any paid module is registered locally).

## [0.2.19] — 2026-05-19

### Added

- **`feat(templates)` 7 role-specialist agent/skill packs + KG nodes** (`0c1d285`). 83 new files across `templates/agents/free/` + `templates/skills/` covering 7 senior professional roles built via 7 parallel Opus subagent runs: Consulting CTO (portfolio, SOW, due-diligence, employee-impersonator); Senior Designer/UX (brand identity, enterprise UX, design tokens, AI imagery, Photoshop scripting); Vendor/Sales+Marketing (inbox triage, outbound sequences, SEO, content calendar, sales-call prep); Senior Scientist (paper triage, experiment design, stats consultation, equation check, HPC, repro audit); Automation/AI Engineer (workflow design, API scaffolding, idempotency, webhook security, cost estimation); Solo SaaS Founder (pricing strategy, metrics health-check, landing critique, launch orchestration, build-vs-buy); Senior DevOps/SRE (incident response, post-mortem, IaC review, k8s review, SLO design, observability). Each template uses `opus + effort:high` (or `xhigh` for the few deep-reasoning agents: experiment-designer, equation-check, consulting-sow-drafter). Also adds `knowledge/tools/terraform-opentofu.md` as a foundation node for the existing terraform-plan-reviewer skill.

- **`feat(kg)` 42 new KG nodes + 1 expanded merge** (`32df8aa`). Crystallizes the substantive knowledge that previously lived only in the role-pack skill/agent template bodies into KG nodes, so it surfaces via `hybrid_search` regardless of whether the user invokes the specific skill/agent. Built-in agents (general-purpose, coder, Explore) rarely auto-load custom skills, so knowledge stuck in a skill body is invisible by default — KG nodes are not. Built via 7 parallel Opus extractors (one per role), each in its own isolated worktree. Two merges performed to avoid near-duplicates: `equation-verification-methodology` merged INTO existing `dimensional-analysis-as-debugging` (now covers all 4 sanity checks); `role-impersonation-archetypes` + `wear-the-hat-discipline-specialist` merged into single `wear-the-hat-pattern`. Cross-link added between `sre-incident-response-playbook` and `incident-communication-tempo`.

### Fixed

- **`docs(deferral)` schema_migration_required: explicit VCT_ORCHESTRATOR_ROOT cd** (`195fe9a`). The v0.2.18 `update_project_v2` flow emitted a `schema_migration_required` deferral with a "To apply" command that read `python -m vco_lib.project_init migrate-collections --name 'X' ...`. LLM agents in per-project workspaces (and humans copying-pasting) ran this from the project directory + project venv, triggering `ModuleNotFoundError: No module named 'vco_lib'` because `vco_lib` lives in the orchestrator clone's venv only. Fix: corrected deferral text to `cd "$VCT_ORCHESTRATOR_ROOT" && .venv/bin/python -m vco_lib.project_init migrate-collections --name 'X' ...`. The `--name` flag still scopes the migration to the project's collections; running from the orchestrator clone does NOT migrate the orchestrator's own.

### Migration notes

- v0.2.19 is a templates + KG + doc-fix release. No binary code changes (the launcher binary functionally matches v0.2.18). The 3-OS rebuild fires only because the release workflow rebuilds on every tag — the produced binaries are byte-equivalent in behaviour to v0.2.18's. Users who don't need the new templates/agents/skills/KG nodes can stay on v0.2.18 indefinitely.
- The new templates auto-propagate to per-project installs via Update Bundle (per-project Settings → "Update bundle"). The new KG nodes seed at next `install.py --update` step 7c, OR via per-project `kg-sync` for projects that bind the shared KG.
- The deferral-doc fix only affects FUTURE deferrals (existing v0.2.18 deferrals on user machines have the stale text; they can hand-edit or wait for `install.py --update` to re-emit on the next schema-drift detection).

## [0.2.18] — 2026-05-19

### Added

- **`feat(embed)` `vco_lib.embedding_service.EmbeddingService` — central multi-provider embedding dispatcher** (Wave A Commit 2). Replaces ~7 consumer scripts that each read `EMBEDDING_MODEL` / `ACTIVE_EMBEDDING` directly. Per-project instances via `EmbeddingService.for_project(project_root)` resolve text + code backends from env, expose `.text_vector_slot` / `.code_vector_slot` for active-slot dispatch, and provide `embed_text` / `embed_code` (single + batched). Multi-slot writes via `embed_text_all_configured` / `embed_code_all_configured` preserve search continuity through model changes. Backend adapters live under `vco_lib/embedding_providers/{ollama,codeembed,openai}.py`. CLI: `python -m vco_lib.embedding_service discover --json` for the GUI catalog. Failure capture writes 3 surfaces: `~/.claude/metrics/embedding_failures.jsonl` + `<install_root>/.claude/context/EMBEDDING_FAILURES.md` (Claude-readable hint, auto-cleared on success) + `UPDATE_DEFERRED.md` deferral entry for the GUI banner.

- **`feat(secrets)` OpenAI API key as bundled secret** (Wave A Commit 3). New entry in `vct-module.json::bundled_secrets` (key `openai_api_key`, scope `shared`, module_id `user`). Three Tauri commands in `launcher/src-tauri/src/commands/openai_cmd.rs`: `register_openai_api_key(value, set_as_default)`, `validate_openai_api_key(value, model?)`, `recheck_openai_validity()`. Validation uses the FREE `GET https://api.openai.com/v1/models/text-embedding-3-small` endpoint (no token consumption, no billing entry — verified against OpenAI docs). Startup re-check task in `lib.rs::setup()` runs the recovery state machine: previously-valid-now-invalid → fallback to local + stash openai-defaults in `app_state.openai_fallback_pending`; previously-invalid-now-valid → restore from stash + clear. Tauri events `vct-openai-key-invalidated` / `vct-openai-key-restored` drive Preferences toasts. Keychain access via the existing `keyring` crate — cross-OS (macOS Keychain, Windows Credential Manager, Linux Secret Service).

- **`feat(weaviate)` multi-slot named-vector schema** (Wave A Commit 4). KG-shaped collections (per-project KG, shared KG, Development) gain `arctic2_embed` (1024d) + `openai_text_embed` (1536d) slots alongside the existing `qwen3_embed` / `ollama_embed` / `openai_embed`. Code collections (`CodeModule` / `CodeClass` / `CodeFunction` / `CodeAPI` / `CodeInteraction`) gain `qwen3_embed` (CPU fallback) + `jina_embed` (768d) + `openai_code_embed` (1536d) alongside the existing `codesage_embed` + `ollama_code_embed` + `openai_embed`. UNION strategy preserves all legacy slot data through the migration (additive, non-destructive). New `vco_lib/weaviate_schema.py` module exposes the canonical slot list (`KG_NAMED_VECTORS`, `CODE_NAMED_VECTORS`) + extensibility helpers (`add_named_vector_slot`, `migrate_collection_to_target`). `migrate-collections --all-projects` walks every KG-shaped + code-shaped collection on the server.

- **`feat(gui-dropdowns)` KG/Codegraph + Preferences embed-model dropdowns** (Wave B Commit 8). Per-project KG/Codegraph binding fields swap free-text inputs for `<select>` populated from `get_embedding_catalog` Tauri command. Preferences gains "Default text embedding for new projects" + "Default code embedding for new projects" dropdowns. New project page pre-populates from `app_state.default_text_embedding` / `default_code_embedding`. Bare-name legacy code-graph classes (`CodeFunction` etc. without project prefix) hidden from GUI enumeration via `is_codegraph_class` filter — data preserved, just not exposed.

- **`feat(observability)` embedding-failure SessionStart hook** (Wave B Commit 11). `.claude/hooks/embedding-failures-surface.sh` + `.ps1` surface `EMBEDDING_FAILURES.md` content to Claude on next chat start. `vco_lib.embedding_service` extended to write 3 failure surfaces (JSONL log + hint .md + deferral entry); auto-clears on next successful construction. Hook registered in `templates/settings.json.linux.template` + `settings.json.windows.template`.

- **`feat(wizard)` OpenAI key step in OnboardingWizard** (Wave C Commit 6). New wizard step after GitHub PAT: password-masked API key input with Show/Hide toggle, Validate button (free `/v1/models` probe), checkbox "Use OpenAI as the default embedding provider for new projects" (disabled until validated). Skip path always available. Inline feedback in 6 states (idle / validating / valid / valid-rate-limited / invalid / network-error). Full ARIA + keyboard navigation. No auto-switch outside the checkbox — locked design decision.

- **`feat(preferences)` OpenAI key row + Re-check + styled modal + recovery toasts** (Wave C Commit 7). Preferences gains OpenAI section symmetric to GitHub PAT: password-masked input, Apply + Re-check + Clear buttons, 7-state status indicator (idle / unvalidated / working / valid / previously_valid_failing / invalid / network-error). Apply with valid key prompts via styled Svelte modal "Set OpenAI as default for new projects?" — no auto-switch. Recovery toasts wired to Wave A's `openai_key_invalidated` / `openai_key_restored` Tauri events.

- **`feat(install)` install.py writes preset defaults to launcher app_state** (Wave C Commit 10). Detected hardware preset (gpu/cpu/openai/low_resource) → default text + code embedding model IDs → `app_state.default_text_embedding` / `default_code_embedding`. GUI dropdowns on new project pages pre-populate from these. Idempotent: only sets if currently NULL (preserves user manual selections). DB path resolved via existing `vco_lib.paths.vct_root_dir` helper (cross-OS: `~/.vct/launcher.db` on Linux/macOS, `%USERPROFILE%\.vct\launcher.db` on Windows; `VCT_STATE_DIR` env override supported).

- **`feat(enrichment)` idempotent enrichment migration for embed-slot changes** (Wave D Commit 9). New `vco_lib/embedding_enrichment.py::enrich_collection_vectors` walks one Weaviate collection adding new-slot vectors to objects that lack them. Preserves all other slots (never deletes). Pre-checks: collection exists, slot is in the schema (else raises `SlotNotInSchemaError` pointing to `migrate-collections`), backend reachable. Batches in groups of 100. Soft-fail per object — whole run continues. Tauri command `enrich_collection_vectors` + Svelte `EnrichmentProgressModal` mount when user changes a model dropdown + clicks Save. **Multi-class codegraph sweep**: codegraph Save runs enrichment sequentially across ALL 5 sibling classes (`CodeModule` + `CodeClass` + `CodeFunction` + `CodeAPI` + `CodeInteraction`) — closes the correctness gap where search against 4 of 5 returned nothing until manual re-enrich.

- **`feat(dev-collection)` Development collection `content_hash` + `status` parity with KG** (Wave D bonus). Pre-v0.2.18 Dev collections lacked the embed-skip fast-path property — every install re-embedded every `docs/` file. New `content_hash` property mirrors the v0.2.17 KG addition; sync_doc checks hash + active-slot population before re-embedding. New `status` property allows archived/draft docs to be excluded from `hybrid_search` via the existing stale-filter. KG-only graph properties (`tags`, `links`, `typed_links`, `node_type`) explicitly NOT mirrored. Smoke test confirmed 820x speedup on unchanged content (896ms → 1ms second sync).

- **`feat(embed)` code backend fallback chain — codesage → qwen3 → jina** (Wave D tail). When CodeEmbed FastAPI service is down AND `code_model_id="codesage-large-v2"` (the GPU-accelerated default), prior versions failed every embed call (Ollama doesn't have codesage). New chain probes backends at construction time: (1) CodeEmbed service `/health`, (2) Ollama `qwen3-embedding:0.6b`, (3) Ollama `unclemusclez/jina-embeddings-v2-base-code:latest`. First reachable wins. `code_vector_slot` updates to match the resolved backend so search-by-active-slot stays correct.

- **`feat(codegraph)` language-scoped prune + Re-analyze button (Plan C)** (Wave D tail). `language` property added to `CodeClass` / `CodeFunction` / `CodeAPI` / `CodeInteraction` (already on `CodeModule`). Analyzer auto-stamps the canonical language id on every insert via a dispatcher-level `_current_language` field. Post-edit hook (`code-graph-incremental.{sh,ps1}`) now detects edited file's language from extension + invokes `analyze --incremental --language <lang> --prune-stale` — language-scoped prune correctly cleans up deleted-file entries WITHOUT touching other languages' rows. New Tauri command `reanalyze_code_graph` + Svelte `CodeGraphReanalysisModal` for the user-triggered explicit refresh (no `--language`, full multi-language walk + global prune). The pre-Plan-C warning about `--prune-stale + --language` deletion is removed — the combo is now the correct mode.

### Fixed

- **`fix(install)` 0.0 A3 routing — always run git-pull-case deferral helper** (Commit 1, pre-Wave-A landed early). Earlier v0.2.17 `_refresh_dist_binary_after_rebuild` used `if not src.is_file()` as a routing gate ("src exists ⇒ cargo path will emit"). False — cargo path silently bails on Gate 2 when `src_mtime <= dist_mtime` (common when a stale `target/release/vct-launcher-temp` from a prior local build exists), leaving the deferral unemitted on git-pull updates. Fix: run the helper unconditionally; idempotent via existing version-equality check.

- **`refactor(scripts)` migrate embed consumers to EmbeddingService** (Wave B Commit 5). `sync_knowledge_graph.py` deletes the qwen3-only `RuntimeError` assertion (audit finding KG-W1, 2026-04-30). `analyze_code_graph.py`, `maintain_knowledge_graph.py`, `process_documents.py`, `search_knowledge.py`, `query_code_graph.py`, `claude_mcp_servers/weaviate_mcp/server.py`, `claude_mcp_servers/scripts/migrate_to_new_embeddings.py` all migrated to `EmbeddingService.for_project()`. After migration: only `vco_lib/embedding_service.py` reads `EMBEDDING_MODEL` / `ACTIVE_EMBEDDING` directly.

- **`chore(audit)` OpenAI dropdown ID prefix unification** (Wave D tail). Catalog emission boundary translates raw OpenAI model names (`text-embedding-3-small`) to prefixed form (`openai-text-embedding-3-small`) for the GUI; the HTTP-call boundary strips the prefix back to raw for OpenAI's `/v1/embeddings` API. Closes the cross-commit bug where dropdown pre-select compared `app_state` prefixed-form values against catalog raw-form IDs and silently mismatched.

- **`chore(cleanup)` minor polish items bundled** (Wave D tail). `ensure_collection_exists` in `sync_knowledge_graph.py` now reads `KG_NAMED_VECTORS` from `vco_lib.weaviate_schema` (mirrors the Dev variant). `install.py::_write_preset_defaults_to_app_state` drops unused `install_root` parameter (test injection via `VCT_STATE_DIR` env override). `focusTrap` + `focusOnMount` Svelte actions extracted from Preferences page to `$lib/actions/focusManagement.ts` for reuse.

### Migration notes

- **Schema migration via existing `patch_props` flow** (additive, non-destructive). Add Project + Update Bundle routes already trigger `migrate-collections --dry-run` and emit `schema_migration_required` deferral when drift detected. User runs `migrate-collections` (no `--dry-run`) to apply: existing v0.2.17 KG collections gain `arctic2_embed` + `openai_text_embed`; existing code-graph collections gain the new code slots + `language` property; existing Dev collections gain `content_hash` + `status`. All data preserved.
- **OpenAI integration is opt-in.** No-key installs continue to use local models (qwen3 + codesage). Key entered in wizard with checkbox = sets OpenAI as default for new projects. Key entered in Preferences without checkbox = key stored but defaults unchanged (no auto-switch). Validity-recheck cadence: launcher startup + on-demand Preferences button.
- **Failure surfaces.** Embedding backend unreachable now writes 3 surfaces (JSONL log + Claude-readable hint + GUI deferral banner) — auto-cleared on success.

## [0.2.17] — 2026-05-18

### Fixed

- **`fix(release)` commit-dist-binaries race-tolerant push**: the
  job now checks out `main` (not the tag's pre-squash commit) and
  refetches + rebases before pushing. Resolves the non-fast-forward
  push rejection seen on v0.2.16 release run 26045579108, where PR
  #245's squash-merge left the tag's commit on a divergent ancestry
  even though its tree was identical to the merge commit on main.
  Also handles the rare race where main advances during the
  15-20 min build matrix. Anchors expected for v0.2.17 to land
  auto-binary-commit end-to-end without manual fallback.

- **`fix(launcher)` Update Orchestrator end-to-end on Windows + git-pull-case restart detection on all OSes** (v0.2.17 plan 0.0 + 0.0.B). Two coupled bugs:

  **0.0.B (Windows blocker):** `update_orchestrator` runs `git pull --ff-only` from inside the launcher's own working directory. On Windows, the running launcher binary `launcher/dist/windows-x64/vct-launcher.exe` is OS-locked. Git fails atomically with `ERROR_SHARING_VIOLATION`, reverting the entire pull — no source update, no binary update, no deferral entry. Update Orchestrator was therefore totally broken on Windows for any release touching the launcher binary. Fix: before `git pull`, rename the running binary aside as `<binary>.old-<pid>` (Windows allows rename-while-running; only overwrite is forbidden — same trick `_refresh_dist_binary_after_rebuild`'s post-rebuild swap uses at lines 8941-8993). Git can then write the new binary at the canonical path freely. No-op on Linux/macOS (kernels handle running-binary overwrite via inode/vnode ref-counting). On pull failure, the pre-pull rename is reverted (best-effort).

  **0.0 (cross-OS restart detection):** `_refresh_dist_binary_after_rebuild` ONLY emitted the `launcher_restart_required` deferral on the cargo-rebuild path (`launcher/src-tauri/target/release/vct-launcher-temp` → dist swap). The COMMON end-user case — `git pull` lands a pre-built binary directly at `launcher/dist/<arch>/vct-launcher` — never triggered the deferral, so the launcher's banner stayed silent while the on-disk binary diverged from the running PID's binary. Added `_maybe_emit_running_stale_deferral` helper that compares `vct-module.json::version` (source-of-truth post-pull) against `state/install-manifest.json::version` (what install.py wrote last time). On mismatch + `dist_path.is_file()`, emits the deferral. Mutually-exclusive routing with the cargo-rebuild path (Reviewer A finding A3): runs ONLY when `target/release/vct-launcher-temp` is absent, so the two emit sites never double-fire.

  **Auto-restart (replaces banner ceremony):** the launcher is a process manager / dashboard with no in-flight user state — DB writes are transactional, container processes are independent of launcher lifetime, hub HTTP connections reconnect within 5s. `update_orchestrator` now auto-restarts the launcher after install.py exits successfully. Reuses v0.2.15's `spawn_detached_launcher` for cross-OS spawn (`setsid` on POSIX, `DETACHED_PROCESS` on Windows). When `VCT_AUTO_RESTART_LAUNCHER=1` is set (Rust caller), install.py skips emitting the deferral (auto-restart makes it redundant). Manual `python install.py --update` from terminal still emits the deferral so a running launcher's W4 banner picks it up.

  **Auto-restart failure-path fallback** (Reviewer A finding A2): if `restart_launcher` fails after install.py succeeded, `update_orchestrator` re-spawns `install.py --update` with `VCT_AUTO_RESTART_LAUNCHER` unset AND a new `VCT_FORCE_RESTART_DEFERRAL=1` env. The force-emit env tells `_maybe_emit_running_stale_deferral` to land the deferral even when source-vs-manifest comparison would otherwise skip (the first install.py pass already bumped the manifest to match the source). Without this fallback, the user would see "Orchestrator updated successfully!" while running stale in-memory binary with zero signal. On fallback-spawn failure, `update_orchestrator` returns a real `Err` to the GUI with manual-restart guidance.

  **Boot sweep:** `lib.rs::run`'s setup hook now sweeps `launcher/dist/<arch>/*.old-<pid>` and `*.pending-<pid>` siblings, deleting files whose PID is no longer alive (cross-OS PID check: `kill(pid, 0)` on Unix, `OpenProcess` on Windows). Bounded space cost: ≤1 sibling per release in steady state. Replaces the prior fixed-name `.old.exe` sweep at the same call site.

- **`fix(sync)` content-hash skip avoids re-embedding unchanged KG nodes** (v0.2.17 plan 0.2). Every `install.py --update` step 7c (`Seeding Weaviate with bundled knowledge/ + docs/`) hammered Ollama's `/api/embeddings` for ~1500 vectors (per-project KG + shared KG, ~767 each) for several minutes of sustained GPU work even when zero `knowledge/**/*.md` or `docs/**/*.md` content changed between releases. Observed during v0.2.15 → v0.2.16 upgrade from the maintainer's launcher GUI (sustained 5-10 embed calls/sec, manifest finally flipped to 0.2.16 with `uuid_scheme=v2` correctly, but cost ~1500 wasted embeddings). Root cause (`templates/scripts/sync_knowledge_graph.py:1225-1296`): `sync_node()` unconditionally deletes the existing Weaviate object, regenerates the embedding, and re-inserts — there was NO content-hash skip at the embedding level. The earlier `_update_frontmatter_timestamp` skip only prevented the file-system write of the `updated:` timestamp. Fix: add `content_hash` property to the KG schema (in BOTH `templates/scripts/sync_knowledge_graph.py`'s `ensure_collection_exists` schema-create + migration loop AND `vco_lib/project_init.py::kg_class_definition` so fresh per-project KGs ship with it from day 0 instead of acquiring it lazily on first sync); on every insert (single-chunk + multi-chunk paths), populate it with `_content_signature_excluding_updated(content)` (same hash function as the file-write skip); BEFORE the delete-and-embed pipeline, query existing objects with the same `file_path` and skip the entire pipeline when every chunk's `content_hash` matches the current source's hash AND is non-empty AND chunk-count matches `total_chunks` (Reviewer A finding E2: the chunk-count check guards against partial-write states from a prior mid-write crash). Soft-fail throughout — any exception in the skip check falls through to the original re-embed path. Pre-v0.2.17 objects (without `content_hash`) re-embed on next touch and get tagged, so the run AFTER that one then skips.

## [0.2.16] — 2026-05-18

### Fixed

- **`fix(hook)` post-tool-security smoke-test marker rename**: the
  prior smoke-test sentinel in
  `templates/hooks/post-tool-security.{sh,ps1}` was a common-English-
  looking bare-word token that matched a legitimate CHANGELOG
  release-note entry documenting the marker itself, so every CHANGELOG
  edit triggered a false-positive "Possible credential" alert. The
  sentinel is now a VCT-prefixed, `_PROBE`-suffixed identifier with a
  6-char random hex tail — unique enough that it cannot accidentally
  appear in docs or prose. See the hook source for the actual literal
  (intentionally not quoted here to prevent the same release-note
  collision recurring). The simple bare-pattern check is preserved (no
  regex loosening). No external test fixtures referenced the old name.
- **Code-graph analyzer integrity** (W1 / plan 0.1 + 0.2 + 0.7 + 0.8
  + addendum D + 1.4/H). Five silent-correctness bugs in
  `templates/scripts/analyze_code_graph.py` that combined to make
  the analyzer report success while producing incomplete or empty
  per-project code-graph collections. Edit target is the canonical
  `templates/scripts/` (the installer renders into each project's
  `.claude/scripts/` on `install.py --update`).
    - **0.1 `_dedup_insert` is now actually idempotent**: switched the
      internal call from `collection.data.insert(uuid=...)` (POST-only —
      raises 422 on the second run with the same UUID) to
      `collection.data.replace()` (PUT-upsert — idempotent by contract).
      The previous behaviour swallowed every re-run collision in the
      outer per-file try/except, silently flagging files as "skipped"
      and exiting 0; the wizard then rendered green-toast success while
      most objects never landed. All 25 call sites benefit from the
      single internal change.
    - **0.7 UUID identity-key includes file path**: `_deterministic_uuid`
      signature is now `(project, file_path_rel, full_name)`. Two
      genuinely-different files defining the same `module-stem.symbol`
      (e.g. `server.handler` in `claude_mcp_servers/weaviate_mcp/server.py`
      AND `docs/research/probes/server.py`) no longer collide on the same
      UUID. Cross-OS contract: callers pass `Path(...).as_posix()` so
      Windows backslashes don't produce different UUIDs from the same
      file. All 11 per-language `_analyze_*_file` methods normalise
      `relative_path` via `.as_posix()` before threading it through.
    - **0.8 NameError on `class_uuid` in `_extract_class`**: the
      `self._dedup_insert(...)` call's return value was discarded and
      the next line referenced an unbound `class_uuid`. Every Python
      file containing a class hit a `NameError` that the outer
      try/except swallowed into `files_skipped`. Fixed by capturing the
      return: `class_uuid = self._dedup_insert(...)`.
    - **0.2 non-zero exit code on insert failures**: `stats['insert_errors']`
      now tracks per-file write-to-Weaviate failures separately from
      generic parse/IO errors via the new `_DedupInsertError` wrapper.
      `main()` returns exit code `3` (`E_NO_FILES_INDEXED`) when no
      files succeeded, `4` (`E_PARTIAL_INSERT_FAILURES`) when at least
      one insert failed. Launcher's `rebuild_code_graph` IPC can now
      surface warning toasts instead of silent success.
    - **Addendum D worktree-skip + ignore_dirs refactor**: module-level
      `_COMMON_IGNORE_DIRS` frozenset + `_ignore_dirs_for(language)`
      helper replaces 11 near-duplicate inline sets. Adds `"worktrees"`
      — skips `.claude/worktrees/agent-<hex>/` git-worktree clones that
      would otherwise be re-analyzed alongside the main repo.
    - Tests added: `tests/test_analyze_code_graph_v0_2_16.py` (9 tests,
      all pure-Python unit tests against the analyzer module — no
      Weaviate required). Existing `tests/test_analyze_code_graph_retry_cap.py`
      still passes against a live Weaviate.
- **`fix(release)`** (W2 / plan 0.4): `commit-dist-binaries` Stage step
  now reads the flat artifact layout `actions/upload-artifact@v7`
  actually produces (`<artifact_dir>/<binary>`), not the nested
  `<artifact_dir>/launcher/dist/<target>/<binary>` the pre-v0.2.16 code
  assumed. Root cause of the v0.2.15 auto-commit failure
  (`Expected artifact not found: _dist-artifacts/linux-x64/launcher/dist/linux-x64/vct-launcher`)
  — the build matrix succeeded and the Release page got all three OS
  zips; only the auto-binary-commit step failed, which the maintainer
  worked around via the manual binary-commit recipe (commit 0d24812).
  Verified by inspection against `actions/upload-artifact@v7`'s flat-
  file layout for individual-file `path:` entries. Will be exercised
  end-to-end on the v0.2.16 release run.

### Added

- **`feat(install)` uuid_scheme manifest marker** (W2 / addendum F):
  `state/install-manifest.json` now writes `uuid_scheme = "v2"` on
  every install/update/lightweight path. Pairs with W1's analyzer
  changes that key code-graph UUIDs on `(project, file_path, full_name)`
  rather than the pre-v0.2.16 `(project, full_name)` tuple. Pre-v0.2.16
  manifests have no `uuid_scheme` field — readers MUST treat the
  absence as the implicit `"v1"` scheme. Future migration tooling
  consults this marker to decide whether existing code-graph
  collections need a `--force-recreate` rebuild.
- **`feat(install)` migrate-uuid-scheme stub flag** (W2): stub
  `--migrate-uuid-scheme` flag on `install.py` for the upcoming
  code-graph UUID-key migrator. Currently a no-op beyond the manifest
  marker above; real migration tool ships post-v0.2.16.
- **`feat(analyzer)` `--prune-stale` flag** (W1 + W3 wire-up / plan 1.4
  + addendum H): opt-in tracking of every UUID visited during the
  analyze run; at end, every per-project collection's unvisited UUIDs
  are deleted. Closes the "shrunken codebase leaves orphan rows" gap
  that switching to `replace()` upsert semantics would otherwise
  create. Wizard's Re-analyze button passes `--prune-stale` by default
  (checkbox "Clean stale entries during re-analysis"); first-time
  builds (`create_project_v2`) and boot-resume sweeps pass `false` to
  preserve conservative semantics. Skips with a stderr warning when
  combined with `--language` (would falsely delete other-language
  objects).
- **`feat(wizard)` per-project poll status** (W3 / plan 0.3):
  legacy-collections wizard now polls per-project rebuild status
  until terminal. The kickoff counter used to read "Started 3 / 3
  project(s)" forever because the Rust handler returns immediately
  after spawning the analyzer subprocess; we now invoke a new
  `get_code_graph_build_status_for_projects` Tauri command every
  2 seconds and render a per-project row with icon + status label +
  files-analyzed count + any error message. The wizard does NOT
  auto-close on completion — the user can review the final
  per-project status before dismissing.
- **`feat(wizard)` session-scoped dismissal + Re-check button** (W3 /
  plan 0.9): "Dismiss" button renamed to "Dismiss for now" + companion
  auto-reset of `legacy_codegraph_notice_dismissed` on every
  `rebuild_code_graph` invocation + new "Re-check for legacy
  collections" button in Preferences. Together these change the
  dismissal from "permanently suppress until manually flipped" to
  session-scoped with multiple re-arm paths (re-analyze a project, or
  click Preferences → Code-graph collections → Re-check). New Tauri
  command `force_recheck_legacy_codegraph` backs the Preferences
  button.

### Changed

- `commands::codegraph::rebuild_code_graph` now resets
  `app_state[legacy_codegraph_notice_dismissed]` to `false` after
  inserting the pending row (W3 / plan 0.9). Re-analyzing a project
  is the most common cause of new orphan generations appearing, so
  the wizard should re-detect on the next launcher start. Soft-fail
  — a hiccup writing the flag is logged but does not block the
  rebuild.
- `check_for_updates` now returns a full `UpdateStatus` struct (W4 /
  plan 0.5) replacing the legacy `bool` with three independent flags:
  - `remote_ahead` — local git branch behind `origin/main` (resolves
    via `update_orchestrator`: git pull + install.py --update).
  - `install_stale` — `vct-module.json::version` ahead of
    `state/install-manifest.json::version` (resolves via
    `apply_pending_install`).
  - `binary_stale` — running launcher version differs from
    `launcher/dist/<arch>/vct-launcher.metadata.json::launcher_version`
    on disk (resolves via `restart_launcher`).
- `UpdateBadge.svelte` renders one state at a time in priority order
  (W4 / plan 0.5): `binary_stale` > `install_stale` > `remote_ahead`,
  each wired to the correct resolver. v0.2.15 shipped the
  binary-restart half via `LauncherRestartBanner` but the
  install-stale path was missing — visible install-was-out-of-sync
  for 24+ hours after a manual `git pull` with no UI signal.
- `list_legacy_codegraph_collections` and `codegraph_list_projects`
  accept a new `include_untracked_projects: Option<bool>` parameter
  (default `false`) (W4 / plan 0.11). The GUI legacy-collections
  wizard + Code Graph dashboard now filter Weaviate collections by
  currently-tracked projects, hiding dead-project leftovers
  (orphaned per-project collections from removed projects). Data stays in
  Weaviate for potential re-import; the advanced
  `/preferences/weaviate-untracked` route surfaces the full
  inventory.

### Added (continued)

- **`feat(launcher)` apply_pending_install Tauri command** (W4 / plan
  0.5): resolves the "Pulled-but-not-installed" banner state by
  running `install.py --update` against the existing install root
  WITHOUT a preceding `git pull`. Distinct from `update_orchestrator`
  (which does both) so manual `git pull` workflows don't waste ~30s
  pulling an already-current source tree.
- **`feat(launcher)` /preferences/weaviate-untracked advanced route**
  (W4 / plan 0.11): surfaces the full Weaviate code-graph collection
  inventory, including prefixes whose project is no longer registered
  with the launcher. Each row exposes a per-prefix delete affordance.
  Reachable from Preferences → "Show untracked Weaviate collections".

## [0.2.15] — 2026-05-17

Codegraph-wedge release. v0.2.14's testing on the maintainer machine
surfaced a chain of bugs that combined to deadlock the launcher's
"Re-build code graph" path for the orchestrator-root project: two
project-name → class-prefix sanitizers in the codebase produced
different prefixes for the same project, multiple naming-generations
of code-graph classes co-existed in Weaviate from prior VCO releases,
case-insensitive collisions caused Weaviate to reject schema creates,
and the analyze script retried the rejected creates indefinitely.
This release fixes all four links in the chain.

It also ships the user-anticipated launcher-restart UX after Update
Orchestrator swaps the on-disk binary, finishes the
`weaviate_claude` → `vco_weaviate` container-naming cleanup the
maintainer-machine leak had been hiding, and adds Windows path
support to the install-time legacy-volume probes.

### Major themes

- **Codegraph wedge eliminated**: orphan-collection detection in the
  wizard, fail-fast on case-collision in the analyze script,
  single canonical sanitize function shared between Python + Rust.
- **Update-orchestrator UX**: green sticky "Restart now" banner after
  binary swap; Windows ERROR_SHARING_VIOLATION rename-fallback.
- **Container-naming cleanup**: install.py / hooks / MCP / tests stop
  hardcoding `weaviate_claude`. Single registry at
  `vco_lib/containers.py`. `vct_code_embed` → `vco_code_embed` for
  naming consistency.
- **Cross-OS path support**: install.py path probes now generate
  Windows `%USERPROFILE%\...` + `%LOCALAPPDATA%\...` variants
  alongside POSIX `~/...`. `_find_lean_ctx_binary` adds Windows
  candidates (cargo / scoop / chocolatey / Program Files).

### Added

- `vco_lib/containers.py`: central canonical-name + historical-alias
  registry for the three orchestrator containers (Weaviate, Ollama,
  code-embed). New helpers `canonical_name()`, `all_known_names()`,
  `find_existing_container()` (honors `VCT_CONTAINER_RUNTIME`).
- `vco_lib/project_naming.py`: canonical project-name →
  Weaviate-class-prefix sanitizer. Single source of truth shared
  with `launcher/src-tauri/src/project_naming.rs` (Rust port pinned
  by `tests/fixtures/project_naming.json` parity tests in both
  languages).
- Launcher: `restart_launcher` + `get_launcher_restart_status` Tauri
  commands. `LauncherRestartBanner.svelte` polls every 5s + on
  mount; renders green sticky banner with "Restart now" for
  `launcher_restart_required` deferrals OR red banner with recovery
  steps for `launcher_binary_swap_failed_locked` (Windows lock).
- Launcher wizard: orphan code-graph detection (cross-references
  Weaviate prefixes against known projects via case-insensitive
  normalised-name match) + `cleanup_orphan_codegraph_collections`
  Tauri command (per-group consent, no auto-delete).
- Wizard extension UI: per-group checkboxes + dedicated delete
  button for orphan groups; double-confirmation contract preserved.
- `install.py`: emits `launcher_restart_required` deferral after
  binary swap. Windows rename-fallback for ERROR_SHARING_VIOLATION
  produces `vct-launcher.exe.old-<version>`.
- Tests: `test_container_naming.py`, `test_project_naming.py`,
  `test_project_naming_parity.py`,
  `test_analyze_code_graph_retry_cap.py`,
  `test_binary_swap_deferral.py`. +13 cargo tests for orphan
  helpers + cleanup-verification path.
- Early CI visibility for unset `DIST_COMMIT_TOKEN` — the release
  workflow now emits a clear `::warning::` annotation BEFORE the
  build matrix runs (saves ~25 min of wasted CI on a missing-secret
  release).
- CLAUDE.md: parallel-agent worktree-hygiene + spawn-from-right-repo
  guidance (lessons from this release cycle).

### Changed

- `analyze_code_graph.py`: imports `canonical_class_prefix` from
  `vco_lib.project_naming` (with inline fallback for standalone runs).
  Schema-create retry loop now caps at 3 attempts with exponential
  backoff for transient errors; **fails fast IMMEDIATELY on
  case-collision errors** with an actionable message directing the
  user to the launcher's wizard. Exits with code 2 on collision so
  the launcher IPC surfaces a failure toast instead of hanging.
- `install.py::_refresh_dist_binary_after_rebuild` now takes a
  `deferral_report=` parameter and emits the restart-required
  deferral after a successful swap. Windows path detects
  ERROR_SHARING_VIOLATION and attempts rename-fallback.
- `install.py` self-heal for `weaviate_unreachable_at_update`: uses
  `find_existing_container()` to discover the actual Weaviate
  container name on the host, AND `_detect_container_runtime()` to
  use the right runtime (`podman` vs `docker` vs whatever
  `VCT_CONTAINER_RUNTIME` says). User-facing recovery hints quote
  the resolved runtime + name instead of literal
  `podman start weaviate_claude`.
- `install.py::_build_legacy_volume_probes` generates Windows path
  forms in addition to POSIX. New helper `_expand_path_token()`
  expands both `~/...` and `%VAR%\...` heads transparently.
- `install.py::_find_lean_ctx_binary`: Windows branch with cargo /
  scoop / chocolatey / Program Files candidates. `os.access(X_OK)`
  skipped on Windows (no executable bit).
- `infrastructure/docker-compose.yml`: `vct_code_embed` →
  `vco_code_embed` for naming consistency. Legacy name kept in
  `HISTORICAL_ALIASES` so existing installs migrate cleanly.
- Cleanup paths (both legacy + new orphan): post-delete schema
  re-query verifies each "deleted" class is actually gone; survivors
  move from `deleted` to `failed` with cache-lag explanation. Stops
  the wizard from claiming "4 deleted" when only 2 went through.
- Launcher wizard's `current prefix` display now uses
  `canonical_class_prefix` (shared with the Python side) instead of
  the legacy `sanitize_kg_collection` that produced different output
  for the same project name.

### Fixed

- `storage_ux::cli_helper_tests` intra-process race: switched from
  `process::id()` to `uuid::Uuid::new_v4()` for tempdir naming AND
  added a process-wide `Mutex` around `with_state_dir`
  to serialise env-var mutation. (The UUID swap alone wasn't
  enough — the actual race was on the shared `VCT_STATE_DIR` env
  var.) Verified flake-free across 5 consecutive runs.
- 12+ leak sites where `weaviate_claude` was hardcoded as the
  container fallback (install.py self-heal commands, deferral
  messages, the MCP server's user-facing error hint, both bash +
  ps1 verify-container-ports hooks, two test files). Now sourced
  from `vco_lib/containers.py`.
- 4 leak sites discovered during the audit:
  `vco_lib/project_init.py::_DEFAULT_RESTART_CONTAINER` (flipped to
  lazy lookup), `templates/hooks/_lib/container-names.{sh,ps1}` env
  var (renamed to `vco_code_embed`), `templates/hooks/verify-
  container-ports.{sh,ps1}` compose-service derivation (handles
  both `vco_` and `vct_` prefixes), `test_pr2_templates_portability`
  tokenization bug (substring-match `'weaviate' in 'vco_weaviate'`
  was True; now uses shell-delimiter tokenisation).
- `test_install_storage_prompt::test_detect_uses_user_relative_paths_not_hardcoded`
  broadened to accept Windows `%VAR%` heads (was POSIX-only).

### Known issues

- macOS-arm64 binary lookup (v0.2.14 Bug 1 fix) still not exercised
  on a real macOS host — code-path-correct + unit-tested but no end-
  to-end validation. Will be covered by v0.2.16 candidate 1.1.
- Windows PS1 wrapper (v0.2.14 Bug 2 fix, 756 lines from Agent E)
  still not exercised on a real Windows host. v0.2.16 candidate 1.2.
- `DIST_COMMIT_TOKEN` is now visible in workflow logs but still
  requires a manual `gh secret set` to actually fix. The doc lives
  at `docs/MAINTAINER_GUIDE.md` Option B; until set, every release
  tag-push needs the manual ruleset-disable trick.

### Migration

- Maintainers who installed VCO from a fork pre-v0.2.x and still have
  `weaviate_claude` / `ollama_claude` containers on disk: the new
  `find_existing_container()` recognises them as legacy aliases, so
  no manual rename needed. The shipped compose creates `vco_*`
  containers; both can co-exist if you have ancient data to migrate.
- Project authors whose project name is "Foo Bar" (with a space): the
  new canonical sanitizer produces `FooBar` (drops space). If you
  already have `Foo_Bar_*` collections from the old sanitizer, the
  launcher wizard will flag them as orphans and offer cleanup OR
  re-analyze. No data loss without explicit user consent.
- Underscored-CamelCase and digit-suffixed project class names are unchanged.

## [0.2.14] — 2026-05-17

Cross-OS hardening release on top of v0.2.13. Surfaces fixed: a Windows
boot-service regression that's been latent since the v0.2.x launcher
shipped, a macOS bundled-binary dir mismatch (tier-1 always missed),
4 long-standing cargo-test flakes that gave false CI signal under
parallel test load, and a `VCT_CONTAINER_RUNTIME` env-var contract
that was honored in 4 of 8 surfaces (split-brain risk for users with
both runtimes installed).

Also: a generic paid-module installer framework lands publicly. The
launcher can now install Pro-tier modules via signed-URL container
pulls (first concrete consumer: the RL Reranker module, distributed
separately from the AGPL public repo).

**Major theme — Windows actually works**: the launcher's `find_stack_wrapper`
was probing for `scripts/launch-claude-mcp-stack.ps1` on Windows since
v0.2.x, but the file never existed — launcher silently fell back to
direct compose calls, losing CDI-wait, runtime validation, and override-
file logic. Boot Task XML required Git Bash / WSL on PATH because it
invoked the `.sh` via bash. Fix: shipped a 756-line PowerShell sibling
of the bash wrapper (full functional parity) + 19 PS1 tests + updated
Task XML to invoke PowerShell directly. No Git Bash / WSL dependency
on Windows anymore.

**Major theme — env-var contract consolidation**: `VCT_CONTAINER_RUNTIME`
was honored in hooks + launcher's `services::runtime.rs::resolve_runtime`
(PR-43 v0.2.12) but ignored in `install.py::_detect_container_runtime`,
`install.py::_detect_installed_runtime`, the boot wrapper's
`detect_runtime`, and `project_env_settings.rs::detect_runtime_sync`.
On a host with both podman + docker installed, hooks would pick one,
install + boot would pick the other — split-brain. Now consolidated:
all 8 surfaces consult the env var first (accepted values:
`podman|docker|auto`, case-insensitive, trimmed; unknown values log
to stderr + fall through to auto-detect — lenient).

**Major theme — cargo-test flakes fixed at root cause**: the 4 documented
flakes in `commands::kg_sync::tests` + `commands::installer::tests::
github_pat_keychain_tests` shipped through v0.2.12 + v0.2.13 as
"pass on retry, pass with `--test-threads=1`". Real fixes landed (no
`#[ignore]` markers):
- kg_sync: new `spawn_sh_with_retry` helper uses absolute `/bin/sh`
  (skips `execvp`'s `$PATH` traversal — the actual race source) + 3-attempt
  retry-with-backoff on transient `ErrorKind::NotFound`.
- keychain: `keychain_serialize_lock` upgraded to a `KeychainGuard` that
  bundles the in-process mutex with a cross-process `flock(LOCK_EX)` on
  `/tmp/vct-keychain-test.lock` (Unix) / `LockFileEx` (Windows inline FFI).
  Concurrent `cargo test --lib` invocations from different terminals now
  serialise on the OS keychain slot.
  9-batch parallel reproduction (3 batches × 3 concurrent runs each) post-fix:
  zero flakes.

### Added

- **`scripts/launch-claude-mcp-stack.ps1`** (756 lines, audit Bug #2):
  PowerShell 5.1+ port of the bash wrapper. Full functional parity for
  runtime detection, daemon-access validation, GPU/CDI handling
  (Windows: GPU passthrough is WSL2-mediated; no Linux-host CDI probes),
  compose-override resolution, and soft-fail discipline. Honors
  `VCT_CONTAINER_RUNTIME` env var first per the v0.2.14 contract.
- **`tests/test_launch_claude_mcp_stack_ps1.py`** (343 lines, 19 tests):
  mirrors the bash sibling's test matrix; auto-skips when no
  `pwsh` / `powershell.exe` on PATH.
- **`scripts/launch-claude-mcp-stack.sh::detect_runtime`** step 0:
  `VCT_CONTAINER_RUNTIME` consulted BEFORE runtime.txt + auto-detect.
  Unknown values log to stderr and fall through (lenient).
- **`install.py::_runtime_preference_from_env()`** helper: parses
  `VCT_CONTAINER_RUNTIME`; threaded into both
  `_detect_container_runtime` (probes the explicit runtime first;
  falls through to auto if not reachable) and `_detect_installed_runtime`
  (returns the explicit runtime if installed, else auto-detect).
- **`vco_lib/project_init.py::_hook_globs_for_os()`**: returns BOTH
  `*.sh` and `*.ps1` instead of host-OS only. Bundle installer now
  ships both flavours so cross-OS workflows (dual-boot, WSL crossover,
  network-mounted projects) don't leave stale orphan hooks from the
  unexpected flavour. Hooks are text files (~few KB each); shipping
  both is cheap + correct.
- **`docs/MAINTAINER_GUIDE.md`** (new): DIST_COMMIT_TOKEN setup runbook
  (Option A bypass_actors for org repos; Option B PAT secret for
  user repos), ruleset-disable trick for one-off pushes, full
  release-workflow flow + auto-commit failure recovery.

### Changed

- **`install.py::_launcher_binary_relative_path()`** (audit Bug #1):
  Darwin branch returns `("macos-arm64", "vct-launcher")` (was
  `("experimental_macOS", ...)` which pointed at an empty placeholder
  dir). Tier-1 bundled lookup now finds the macOS binary; tier-2
  download lands in the right dir; release-artifact name mapping
  pass-through.
- **`templates/windows/claude-mcp-containers.task.xml.template`**: invokes
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` against the
  new `.ps1` wrapper (no longer requires Git Bash / WSL). Falls back
  to the `.sh` via bash if the `.ps1` isn't shipped (e.g. early v0.2.14
  partial installs).
- **`install.py::_materialize_boot_service_windows`**: prefers `.ps1`
  when shipped; falls back to `.sh` if absent (defense in depth).
- **`launcher/src-tauri/src/commands/lifecycle.rs::run_stack_wrapper`**:
  adds `-NoProfile` to the PowerShell invocation for parity with the
  Task XML.
- **`launcher/src-tauri/src/commands/project_env_settings.rs::detect_runtime_sync`**:
  honors `VCT_CONTAINER_RUNTIME` env var (audit Bug #3).
- **README.md**: "5 MCP servers" → "3 default + 2 opt-in"; directory
  tree drops `ollama_mcp/` (opt-in via launcher Modules → `vct-ollama`);
  narrows search description to "academic-paper search" only. Closes
  the stale v0.2.11-removed surface that was misleading users on the
  public landing page.
- **`templates/agents/free/prompt-engineer.md`** + **`knowledge-curator.md`**:
  stale Ollama MCP refs cleaned. Tool-card sections for `chat` /
  `read_document` now note opt-in-only status via `vct-ollama`; decision
  trees route quick-analysis tasks to Claude's native reasoning rather
  than to a removed MCP tool.
- **`templates/scripts/claude_token_counter.py`** `WEB_TOOL_NAMES`:
  removed names of v0.2.11-dropped Search MCP tools
  (`mcp__search__web_search`, `mcp__search__fetch_page`,
  `mcp__search__search_code`). Kept `mcp__search__search_papers` (the
  only surviving search tool).

### Fixed

- **Windows boot-service silently failed on logon** (audit Bug #2):
  Task XML invoked `bash <wrapper.sh>` which required Git Bash / WSL
  on PATH. Install-time logged a soft warning; the Scheduled Task
  itself surfaced no diagnostic per failed logon. Now uses PowerShell
  natively; bash on Windows is optional.
- **macOS tier-1 launcher-binary lookup always returned None** (audit
  Bug #1): `_launcher_binary_relative_path()` returned
  `("experimental_macOS", ...)` but binaries shipped in
  `launcher/dist/macos-arm64/`. Fresh macOS installs fell through to
  GitHub download (tier 2) which then landed in the wrong dir too,
  leaving the desktop shortcut + the rest of the codebase
  inconsistent across two dist subdirs.
- **`VCT_CONTAINER_RUNTIME` split-brain on hosts with both runtimes
  installed** (audit Bug #3): see Major theme above.
- **Cross-OS workflows lost stale orphan hooks** (audit Concern #2):
  the bundle installer copied only host-OS hook flavours. A user
  with a project folder opened from both POSIX and Windows shells
  got stale orphan `.sh` files on Windows + stale orphan `.ps1`
  files on Linux/macOS that the unexpected shell could still invoke.
  Now both flavours ship always; the runtime picks which to invoke.
- **4 cargo-test flakes from v0.2.12** (`kg_sync::concurrent_drain`,
  `kg_sync::stall_watchdog`, `installer::pat_file_to_keychain_migration`,
  `installer::register_github_pat_preserves_existing_user_file`): see
  Major theme above. Both pairs fixed at root cause; no `#[ignore]`
  markers.

### Security

No new security advisories in v0.2.14. v0.2.13's Svelte XSS bump
(5.55.7 + Kit 2.60.1 + devalue 5.8.1) carries forward; `npm audit`
remains clean.

### Known issues / deferred to v0.2.15

- **`DIST_COMMIT_TOKEN` repo secret not yet set**: until configured per
  `docs/MAINTAINER_GUIDE.md`, the release-workflow `commit-dist-binaries`
  job 403s on push to protected `main`. Workaround: the ruleset-disable
  trick (also documented). Auto-commit code path verified manually on
  the v0.2.13 release tag.
- **`commands::storage_ux::cli_helper_tests::cli_helper_*`** (bonus
  finding from Agent B's flake audit): intra-process race in
  `with_state_dir` (uses `process::id()` as tempdir suffix → multiple
  parallel tests in one binary race on the same path). Reproducible
  via `cargo test --lib commands::storage_ux::cli_helper_tests`. One-
  line fix (`uuid::Uuid::new_v4()` instead of `process::id()`) deferred
  to v0.2.15.

## [0.2.13] — 2026-05-16

Same-day patch release on top of v0.2.12. Discovered during a real
v0.2.10 → v0.2.12 launcher-update test on the maintainer's dev machine:
8 distinct bugs in the update pipeline, all of which would silently
leave end users on a stale binary, with deprecated MCP entries
preserved, and with stale UI labels. v0.2.13 makes the update flow
self-healing.

**Major theme — update flow self-heals**: install.py's 4-tier launcher
binary resolution now post-rebuild-auto-swaps fresh binaries into
`launcher/dist/<os>-<arch>/` (Fix 1), retries the launcher-CLI against
the freshly-built binary when the bundled tier-1 binary times out
(Fix 5), and always touches `UPDATE_DEFERRED.md` so the user sees the
final union of all deferral entries (not just the ones generated
before `_register_mcps` runs, as in v0.2.12) (Fix 6).

**Major theme — deprecated MCP cleanup**: when `install.py` drops an
MCP from the default set (e.g. Ollama MCP in v0.2.11), existing user
installs were left with the stale entry in `~/.claude.json`. New
`--remove-deprecated-mcps` consent-prompted removal path (PR-34)
detects + offers removal. Detection is unconditional on `--update`;
removal requires the explicit flag. User-customised entries at paths
outside the install_root are never touched.

**Major theme — version-string consistency**: `vct-module.json` was
last bumped to `0.2.4`, never tracking subsequent releases. Bumped
to `0.2.13` + added to the canonical-release-bump checklist (Fix 9).
The launcher Store-page no longer hardcodes `version: '0.1.6'` for
the orchestrator + Pro cards — empty string with `{#if version}`
guard so the catalog override is the only version source (Fix 3).

**Major theme — security**: bumped Svelte `5.0.0` → `5.55.7` and
`@sveltejs/kit` `2.59.1` → `2.60.1` + `devalue` `>=5.8.1` override
to close 4 moderate Svelte XSS CVEs (DOM clobbering, SSR spread
attributes, SSR Promise serialization) AND 1 high `devalue` DoS CVE
(Fix 7). `npm audit` is now clean.

**Major theme — release-pipeline hardening**: new
`commit-dist-binaries` job in `.github/workflows/release.yml`
auto-commits freshly-built binaries back to `launcher/dist/` on every
release-tag push — eliminating the "committed binary lags release"
class of bug that surfaced in v0.2.12 (Fix 8). The auto-commit job
requires `bypass_actors` configured on the branch-protection ruleset
(github-actions[bot] integration); on personal-account repos the
auto-commit job will fail until a PAT-backed `DIST_COMMIT_TOKEN`
secret is wired (deferred to v0.2.14 — see Known issues).

### Added

- **Post-cargo-rebuild dist-binary auto-swap** (Fix 1, `install.py`):
  new `_refresh_dist_binary_after_rebuild()` helper, wired into
  `--update` immediately before `_register_mcps`. Conservative
  4-condition gate (src exists, src mtime > dist mtime, src produced
  in this run OR version-stale, `--no-binary-swap` flag not set). The
  version-stale fallback uses `tauri.conf.json` mtime as a cheap
  proxy for "the dist binary is built from a version older than the
  current tauri config". New flag `--no-binary-swap` to opt out.
  Module-level `_INSTALL_START_TS: Optional[float]` set at the top of
  `_run_install` provides the "produced in this run" signal.

- **Tier-3 retry of launcher CLI after timeout/non-zero-exit** (Fix 5,
  `install.py::_register_mcps`): when `Path A` (launcher binary CLI)
  fails transiently (timeout or non-zero exit) against a tier-1
  binary, install.py now invokes `_try_cargo_tauri_build()`
  explicitly to produce a fresh binary, then retries the CLI ONCE
  with the new binary before falling through to the pure-Python
  fallback (Path B). Emits `register_mcps_tier3_retry` events for
  observability. New flags `--prefer-only-bundled` (skip tier-2/3
  resolution AND the retry) and `--no-rebuild-on-stale` (skip
  ONLY the retry; resolution unaffected).

- **`UPDATE_DEFERRED.md` always populated on `--update`** (Fix 6,
  `install.py`): second `_deferral_report.write()` call at the very
  end of `_run_install` captures entries added AFTER the original
  write (which ran before `_register_mcps`, the deprecated-MCP
  detection, `_check_searxng_remnants`, `_check_ollama_mcp_remnants`,
  `_check_search_mcp_env_obsolete`, `_materialize_boot_service`,
  and `_rewrite_stale_mcp_entries`). On `--update` runs with zero
  deferral entries, writes a stub file with `entries: 0` frontmatter
  so the user has a paper trail that the run completed cleanly.

- **Deprecated-MCP detection + consent-prompted removal** (Fix 4 /
  PR-34, `install.py`): new `_DEPRECATED_DEFAULT_MCPS` registry maps
  removed MCP names to their `removed_in` version + reason + opt-in
  manifest path. New helpers `_scan_deprecated_mcp_entries`,
  `_detect_deprecated_mcp_entries`, `_remove_deprecated_mcp_entries`
  follow the same scan/detect/remove pattern as PR-33 stale-rewrite.
  Detection runs unconditionally on `--update` (hooked into
  `_register_mcps` after both Path A and Path B). Only entries
  whose `command` path is INSIDE the current install_root are
  flagged; user-customised entries at unrelated paths are left
  alone. New flag `--remove-deprecated-mcps` (per-entry consent
  prompt) + env override `VCT_REMOVE_DEPRECATED_MCPS=all` for CI.
  Two-level backup before any write, atomic `lock + tmp + os.replace`
  discipline. Composes with PR-33 `--rewrite-stale-mcps`. First
  entry in the registry: `ollama` (removed in v0.2.11).

- **CI auto-commit of dist binaries** (Fix 8,
  `.github/workflows/release.yml`): new `commit-dist-binaries` job
  (`needs: build`) downloads the 3 OS binaries from the matrix
  artifacts, skips byte-identical (`cmp -s`) ones, stages ONLY
  `launcher/dist/*/vct-launcher[.exe]` + `.metadata.json` paths
  (guard errors if any non-`launcher/dist/` file sneaks in), and
  commits `chore(binary): refresh vct-launcher dist binaries for
  v<version>` to main. New `workflow_dispatch` input
  `skip_dist_commit` for dry-runs.

- **2 Rust regression tests for the catalog-version invariants**
  (`launcher/src-tauri/src/commands/modules.rs`):
  `builtin_catalog_orchestrator_entry_has_non_empty_version` and
  `builtin_catalog_launcher_entry_has_non_empty_version` — guard
  against future regressions where `vct-module.json` or
  `CARGO_PKG_VERSION` returns empty.

- **42 new Python regression tests** across
  `tests/test_install_binary_resolution.py` (19),
  `tests/test_install_deprecated_mcps.py` (23). All passing.
  Full pytest suite: 1264 passed, 5 skipped, 50 subtests.

### Changed

- **`vct-module.json` bumped 0.2.4 → 0.2.13** (Fix 9). Description
  fixed: "5 MCP servers (4 containerized + Playwright)" →
  "4 MCP servers (3 containerized + Playwright)" — post-PR-14a
  Ollama MCP removal. `_canonical_counts_2026_05_16` comment
  refreshed to reflect PR-39 v0.2.12 ground truth: hooks live in
  `templates/hooks/*.sh` and are rendered into `.claude/hooks/` at
  install time, not source-of-truth there. The comment now
  explicitly lists `vct-module.json` as a release-bump-required file
  (alongside Cargo.toml + tauri.conf.json + package.json).

- **Svelte `5.0.0` → `5.55.7`** + **`@sveltejs/kit` `2.59.1` →
  `2.60.1`** + **`devalue` `>=5.8.1`** floor via `overrides`
  (`launcher/package.json`). Closes the 4 moderate Svelte XSS CVEs
  (GHSA-pr6f, GHSA-f3cj, GHSA-rcqx, GHSA-9rmh) and the 1 high
  `devalue` DoS CVE (GHSA-77vg). `npm audit` returns 0
  vulnerabilities. svelte-check still passes with 0 errors (the 45
  pre-existing a11y warnings on the dev-only Preferences /
  Projects / Services pages are unaffected).

- **Store-page `version: '0.1.6'` hardcode removed**
  (`launcher/src/routes/store/+page.svelte`): the orchestrator and
  Pro card now default to empty string `version: ''`. The catalog
  override (`list_module_catalog` → `builtin_catalog_entries` →
  `vct-module.json`) supplies the real version for the orchestrator
  card; the orchestrator-pro card has no catalog entry yet and
  shows no version label until one is added. The version rendering
  is now guarded by `{#if (catalogVersionById[app.id] ?? app.version)}`
  so an empty version never renders a bare `v` glyph.

### Fixed

- **Stale `dist/<os>-<arch>/vct-launcher` after `--update`** (Fix 1):
  in v0.2.12 the bundled binary at `launcher/dist/<os>-<arch>/`
  could be older than the orchestrator's source tree (e.g.
  v0.1.6-era), with `install.py --update` doing nothing to refresh
  it. Users who relied on the desktop icon (which targets
  `dist/<os>-<arch>/vct-launcher`) saw the wrong version
  post-update. Now auto-refreshed.

- **Launcher-CLI timeout against stale tier-1 binary fell straight
  to Python fallback** (Fix 5): when the v0.2.10 tier-1 binary did
  not recognize the v0.2.12 `--register-default-mcps` flag, it
  launched the GUI instead, hit the 30s timeout, and install.py
  fell to the Python writer — missing the tier-3 rebuild that
  would have produced a binary that DOES recognize the flag. Now
  retries with the freshly-built binary first.

- **`UPDATE_DEFERRED.md` missing entries from late-stage helpers**
  (Fix 6): in v0.2.12, deferrals generated by `_register_mcps`,
  the deprecated-MCP / SearXNG / Ollama / search-env remnant
  checks, `_materialize_boot_service`, and `_rewrite_stale_mcp_entries`
  were ALL lost because the single `_deferral_report.write()` ran
  before any of them. Today's launcher-update test produced 0
  files instead of the expected ~3-4 entries. Fixed.

- **Deprecated MCPs not auto-removed on `--update`** (Fix 4): in
  v0.2.12, an end user updating from v0.2.10 to v0.2.12 kept their
  `~/.claude.json mcpServers.ollama` entry (Ollama MCP was dropped
  in v0.2.11 PR-14a but install.py didn't detect or offer removal).
  Now flagged via deferral on every `--update`, removable via
  `--remove-deprecated-mcps`.

- **Store-page showed `v0.1.6` for orchestrator + Pro cards** (Fix 3):
  hardcoded default in `allApps[]` was rendered whenever the
  catalog override returned empty (which is always, for `orchestrator-pro`
  — no catalog entry exists). Fixed by setting the hardcoded
  default to empty + guarding the render.

- **`vct-module.json` reported stale version + wrong MCP count**
  (Fix 9): `0.2.4` since v0.2.4-era; `5 MCP servers` since v0.2.10-era
  (pre-PR-14a). Bumped + corrected.

### Security

- **Svelte XSS** (Fix 7, 4 moderate CVEs): bumped to 5.55.7. Patches:
  DOM clobbering of internal framework state (GHSA-pr6f), SSR XSS
  via spread attributes (GHSA-f3cj), SSR XSS via insecure Promise
  serialization in hydratable (GHSA-rcqx), and the related GHSA-9rmh
  family. None of the patches affected behavior we depend on
  (validated by svelte-check pass + visual inspection of the
  Promise-serialization use-sites).

- **`devalue` DoS** (Fix 7, 1 high CVE): pulled in via `@sveltejs/kit`
  bump to 2.60.1 + `overrides.devalue: ">=5.8.1"` floor. Closes
  GHSA-77vg.

### Known issues / deferred to v0.2.14

- **CI auto-commit job needs `bypass_actors`** (Fix 10, partial):
  GitHub's branch-protection ruleset API rejected the
  `bypass_actors` PUT on our personal-account repo with "Actor
  integration must be part of the ruleset source or owner
  organization". The auto-commit job in release.yml will silently
  fail until either (a) the repo is moved to an org where the
  Integration-type bypass actor pattern works, or (b) a
  `DIST_COMMIT_TOKEN` secret is created with a fine-grained PAT
  having `Contents: Write` on this repo, and the auto-commit job
  is reworked to use that secret in place of the default
  `GITHUB_TOKEN`. v0.2.13 release will use the manual
  ruleset-disable trick (same as v0.2.12) for the version-bump push.

- **macOS Intel x64 not built** (carried forward from v0.2.12).
  Tier-3 cargo rebuild fallback works on Intel Macs but is slow.

## [0.2.12] — 2026-05-16

Stability + UX release on top of v0.2.11. Closes 11 of 13 MCP-instability
bugs identified in the post-v0.2.11 audit (1 deferred to v0.2.13+, 1
worked around via allowlist tightening). Public repo hygiene: 97
shipped-duplicate `.claude/{hooks,scripts,settings.json}` files
deleted — install.py now renders them from `templates/` at install
time, so the public repo no longer ships rendered-output files that
would override user customizations on `--update`. Cross-OS scope kept:
every hook change shipped both `.sh` and `.ps1` bodies, with bash 3.2
compatibility documented as policy (macOS Apple Silicon ships bash
3.2.57; users may not have brew bash).

**Major theme — MCP wiring works end-to-end on fresh install**:
v0.2.11 silently shipped without writing any `mcpServers` entries to
`~/.claude.json` (PR-23 fixes this). On `--update` we also detect
stale entries pointing at moved/deleted install paths and offer a
consent-prompted rewrite (PR-33). Live env reload via SIGHUP + a
launcher file watcher on `.claude/settings.json` means changing per-
project env no longer requires restarting Claude Code (PR-42). The
`~/.claude.json` env allowlist is tightened so per-project values in
`.claude/settings.json env` can take effect (PR-43) — the spawn-time
env precedence in Claude Code applies `~/.claude.json mcpServers.*.env`
LAST, so any key present in both wins on the global side; removing
per-project-varying keys from the global allowlist is the only way to
let per-project overrides work today.

**Major theme — launcher GUI maintenance panels**: register-MCPs,
schema-migration, stale-MCP-rewrite, and reload-MCPs all surfaced as
buttons on the MCP page (`McpMaintenanceSection`) and Services page
(`ServicesSchemaSection`) of the launcher. 8 new Tauri commands wire
to the same install-time helpers, with backend-issued consent tokens
for destructive operations (PR-37 + PR-42).

**Major theme — schema correctness for temporal queries**: the
Development collection was missing 4 temporal properties (`created`,
`updated`, `valid_from`, `valid_until`) and both KG + Development
were missing `indexNullState=True`. Every `hybrid_search` that used
the stale-data filter (`valid_until is_none OR > now`) returned 0
results on those collections with a cryptic "build inverted filter
allow list" error. Idempotent migration scripts ship for existing
installs; new installs get the right schema at create time (PR-24).

**Major theme — public-repo hygiene**: 12 internal dev artifacts
removed from the public clone; root CLAUDE.md (orchestrator-self's
own large dev-context instructions) removed from the install whitelist
so user projects don't get it dropped on top of their own CLAUDE.md
(PR-31). 97 `.claude/{hooks,scripts,settings.json}` files that were
silently duplicated between source and shipped location were deleted
from the public repo — install.py renders all of them from `templates/`
at install time, eliminating the template↔runtime drift class of bugs
(PR-39).

### Added

- **Install-time MCP registration** (PR-23, `install.py`
  + `launcher/src-tauri/src/commands/installer.rs`
  + `launcher/src-tauri/src/mcp_registration.rs`): fresh installs
  (and `--update` reinstalls) now write the 4 default `mcpServers`
  entries (`weaviate-kg`, `coordination`, `search`, `vct-coordination`)
  to `~/.claude.json` via a launcher CLI subcommand
  `--register-default-mcps`. 4-tier launcher-binary resolution
  inside `install.py`: shipped bundled binary
  (`launcher/dist/<os>-<arch>/vct-launcher`) → on-demand GitHub
  release download → `cargo tauri build` rebuild (LAST resort,
  15-25 min) → pure-Python fallback (`launcher/src-tauri/src/mcp_registration.rs`
  ported as a Python helper). macOS ships only `macos-arm64`
  (Intel Macs not built per `.github/workflows/release.yml`). On
  upgrade, `--allow-rewrite-mcps` (or the consent-prompted
  `--rewrite-stale-mcps`) repoints existing `~/.claude.json`
  entries from `commands::installer::run_install_orchestrator_lightweight`
  too, so the "lightweight reinstall" path is consistent with the
  full install path.

- **Stale-MCP-entry detection + consent-prompted rewrite** (PR-33,
  `install.py`): on `--update`, scans `~/.claude.json` for
  `mcpServers.*.env` entries whose `command` or `args` point at
  paths outside the current install root. Reports each stale
  entry on stderr with the proposed rewrite, then waits for an
  explicit per-entry yes/no consent prompt (or `--yes` for
  unattended mode). Auto-rewriting without consent risked
  overwriting deliberate user customizations (e.g. a power user
  pointing at a different venv); per-entry granularity preserves
  intent. `--no-prompt` prints the proposed rewrites without
  applying them (CI / dry-run mode).

- **Launcher-centralized stale-MCP UX** (PR-37 + PR-42,
  `launcher/src-tauri/src/commands/maintenance.rs` +
  `launcher/src/lib/components/{McpMaintenanceSection,StaleMcpModal,
  SchemaMigrationModal,ServicesSchemaSection}.svelte`): 8 new
  Tauri commands surfaced on the launcher MCP + Services pages:
    - `mcp_registration_status` — read which MCPs are registered
      in `~/.claude.json` + their current path validity.
    - `rerun_mcp_registration` — invoke `--register-default-mcps`
      on demand from GUI.
    - `schema_migration_status` — read whether KG / Development
      collections need temporal-property or indexNullState
      migration.
    - `issue_schema_migration_consent_token` — backend-issued
      single-use UUID token; FE doesn't generate its own (preserves
      single-source-of-truth for the destructive op).
    - `run_schema_migrations` — apply pending migrations after
      consent.
    - `stale_mcp_entries` — list stale `~/.claude.json` entries
      with their proposed rewrites.
    - `rewrite_stale_mcp_entries` — apply per-entry consent
      `Vec<(name, bool)>` rewrites; preserves audit trail of
      unchecked entries.
    - `reload_mcps_sighup` (PR-42) — send SIGHUP to all running
      MCP server PIDs to force a clean-exit + Claude-Code respawn,
      picking up new `.claude/settings.json env` values without
      restarting Claude Code.

- **SIGHUP-driven MCP env reload + launcher file watcher** (PR-42,
  `claude_mcp_servers/_lib/sighup_handler.py` +
  `launcher/src-tauri/src/services/settings_json_watcher.rs`):
  every Python MCP server registers a SIGHUP handler that calls
  `sys.exit(0)` (clean exit, not raise) — Claude Code's MCP
  process supervisor respawns on clean exit, picking up the new
  env. Launcher's `notify` crate watcher debounces
  `.claude/settings.json` changes (1s) and dispatches the SIGHUP
  batch automatically. Same code path is exposed via the manual
  "Reload MCPs" button in the launcher MCP page for cases where
  the watcher isn't running (e.g. launcher closed).

- **Weaviate-MCP error classifier hardening** (PR-41,
  `claude_mcp_servers/weaviate_mcp/server.py`): new
  `WeaviateSchemaError` class distinguishes schema-not-found /
  schema-mismatch errors from connectivity (`WeaviateUnreachable`)
  and auth (`WeaviateAuthError`). Schema-not-found now invalidates
  the per-collection schema cache (Issue A: schema-cache stale
  after `--migrate-schema` ran in another process). Error
  messages surfaced to Claude include the distinguishing path so
  users (and future-Claude) can act on the right failure mode
  rather than misattributing schema problems to connectivity.

- **Development collection temporal properties + indexNullState**
  (PR-24, `vco_lib/project_init.py`
  + `scripts/migrate-development-temporal-props.{sh,ps1}`
  + `scripts/migrate-shared-kg-schema.{sh,ps1}`): adds 4
  temporal properties (`created`, `updated`, `valid_from`,
  `valid_until`) to `development_class_definition()` so KG-style
  stale-data filters work on Development collections.
  `invertedIndexConfig.indexNullState=True` added to BOTH KG and
  Development at create time. Migration scripts retro-add the 4
  properties to existing Development collections (POST to
  `/schema/<class>/properties` works on existing collections);
  shared-KG-schema migration drops + recreates the shared
  collection if `indexNullState=False` (Weaviate ≤1.30 can't
  retro-add it; shared KG is typically empty in user installs so
  this is non-destructive). `install.py --update` runs both
  migrations idempotently.

- **Hook dual-layout venv resolution** (PR-25,
  `templates/hooks/{code-graph-incremental,kg-summary-generator,
  pre-edit-context-inject}.{sh,ps1}`): the 3 hooks that need
  Python (for KG/codegraph work) now try `$REPO_ROOT/.venv` first,
  fall back to `$REPO_ROOT/claude_mcp_servers/.venv`, with
  `$VCT_VENV` as explicit override. Previously hardcoded
  `claude_mcp_servers/.venv` only — silently fell through to
  system python on installs using the modern top-level `.venv`
  layout. Cross-OS via `.ps1` siblings with the same fallback
  chain.

- **Shared-KG canonical-name alignment + partial-match picker**
  (PR-26 + PR-34, `vco_lib/project_init.py` + 5 launcher Rust /
  Svelte surfaces): canonical name is now
  `VibecodedOrchestrator_KnowledgeGraph` across all surfaces
  (install pipeline, launcher GUI default, MCP env, tests). Old
  alias `VibeCodedTools_KnowledgeGraph` kept for ~3 releases as a
  fallback in the schema-detection helper. New
  `SharedKgPicker.svelte` GUI lets users adopt a same-suffix
  legacy collection without forcing them to rename. The 5
  surfaces consolidated into a single `VIBECODED_TOOLS_KG_COLLECTION`
  constant.

- **Install-time storage-location prompt** (PR-28, `install.py`):
  on installs with legacy bind-mount paths in
  `$VCO_LEGACY_VOLUME_DIR` (or detected via the same logic as
  PR-10A's GUI), the CLI now prompts at install start with three
  options: keep the legacy path, migrate to the runtime-managed
  named volume (recommended), or pick a new bind path. Same
  3-shape `compose.override.yaml` generator as PR-10A's GUI; both
  paths go through the unified launcher-binary resolver added in
  PR-36. `--storage-mode {named,bind,legacy}` flag for unattended
  installs.

- **Cross-OS cache layer for pre-edit hook** (PR-38,
  `templates/hooks/pre-edit-context-inject.sh`): bash port of the
  `.ps1` cache layer for cross-OS perf parity. Cache key is
  `<file_path>:<offset>:<limit>` (block-atomic dedup — KG/CODE
  result blocks are emitted whole or suppressed whole, never
  partial — so a re-read with a different offset gets a fresh
  retrieval rather than a stale subset). **bash 3.2 compatible**:
  uses file-backed seen-set + `grep -Fxq` rather than `declare -A`,
  so the hook runs on macOS Apple Silicon (default `/bin/bash`
  is 3.2.57). Reduces re-read overhead from ~2.7s to ~31ms when
  the cache is warm.

- **VCT_CONTAINER_RUNTIME env honored in launcher GUI** (PR-43,
  `launcher/src-tauri/src/services/runtime.rs`): the
  `resolve_runtime()` function now honors a per-process
  `VCT_CONTAINER_RUNTIME=podman|docker|auto` override before
  falling back to the auto-detection order. Unrecognized values
  log a warning to stderr and fall back to default detection.
  Hooks already honored this env var; the launcher GUI parity is
  the missing piece.

- **KG node `claude-code-mcp-env-reversed-precedence`** documents
  the spawn-time env-application order in Claude Code's MCP
  supervisor for future reference. Anthropic's documented
  "project scope overrides user scope" semantics applies to chat-
  process settings but NOT to `mcpServers.*.env` — those entries
  are applied LAST in the MCP subprocess spawn, so any key
  present in both `~/.claude.json mcpServers.<name>.env` and
  `.claude/settings.json env` wins on the global side. The
  detection heuristic is "would two different projects on the
  same machine ever want different values for this key?" — if
  yes, the key must not appear in `~/.claude.json mcpServers.*.env`.

### Changed

- **Public repo no longer ships `.claude/{hooks,scripts,settings.json}`
  rendered files** (PR-39): 97 duplicated files deleted from the
  public clone. `install.py` renders all of them from
  `templates/` at install time with the appropriate path
  substitutions (`$INSTALL_ROOT`, `$VENV_PYTHON`, etc.). Source
  of truth is the `templates/` directory; the rendered output
  lives in the target install's `.claude/` and is no longer a
  candidate for source-control drift between the template and
  the shipped copy.

- **Root CLAUDE.md no longer in install whitelist** (PR-31): the
  orchestrator-self's own dev-context CLAUDE.md (which serves the
  dev project, not user projects) is no longer copied into user
  projects at install time. User projects get the project-
  template CLAUDE.md from `templates/CLAUDE.md.template` instead.
  Existing user CLAUDE.md is preserved across `--update` (the
  manifest-based drift detector classifies it as user-modified).

- **`docker-compose.override.yml` → `compose.override.yaml`**
  (PR-22, `launcher/src-tauri/src/commands/storage_ux.rs` +
  `scripts/launch-claude-mcp-stack.sh` + Windows task XML
  template): the dotted `.yaml` form is the one podman-compose
  actually loads. The dot-yml form was silently ignored, so
  bind-mount storage UX (PR-10A) shipped non-functional in
  v0.2.11. Boot script now explicitly adds
  `-f compose.override.yaml` when present (env-overridable via
  `VCT_STACK_COMPOSE_OVERRIDE`). Systemd unit + Windows task
  templates carry the env var. `install.py --update` detects the
  legacy `.yml` filename + renames it (idempotent) with a
  `compose_override_renamed` deferral.

- **`.vscode/settings.json claude-code.env` no longer written**
  (PR-27, `vco_lib/project_init.py`): the channel was empirically
  shown not to propagate env into MCP server subprocesses on
  Linux Claude Code 2.1.143. Removed from `write_project_env_files`
  to avoid creating the false impression that VS Code's
  `claude-code.env` block was a viable env-override mechanism.
  The KG node `vscode-claude-code-env-not-propagated-to-mcp-linux.md`
  documents the empirical finding for future reference.

- **`~/.claude.json mcpServers.*.env` allowlist tightened** (PR-43,
  `install.py` + `launcher/src-tauri/src/mcp_registration.rs`):
  removed `EMBEDDING_MODEL` and `RL_SERVER_URL` from
  `_ALLOWED_GLOBAL_ENV_KEYS`. Both keys legitimately vary per-
  project. Keeping them in the global allowlist meant the
  reversed-precedence behavior (`~/.claude.json` wins) shadowed
  any per-project value. Allowlist now contains only keys whose
  values are machine-invariant: `WEAVIATE_URL`, `OLLAMA_URL`,
  `GRPC_PORT`, `PYTHONPATH`, `ACTIVE_EMBEDDING`,
  `CODE_EMBED_SERVICE_URL`. Per-project-varying keys live ONLY
  in `.claude/settings.json env`. The detection heuristic for
  whether a key belongs in the global allowlist is recorded in
  the KG node `claude-code-mcp-env-reversed-precedence.md`.

- **Launcher-binary resolution DRY'd up across install + storage
  prompt** (PR-36): PR-23 (MCP registration) and PR-28 (storage
  prompt) both needed to resolve a launcher binary in the same
  4-tier order. Consolidated into a single helper. Behavior
  unchanged; pure refactor.

- **Pre-edit-context-inject cache-replay dead code removed**
  (PR-35, `templates/hooks/pre-edit-context-inject.sh`): the
  hook had an unreachable `replay_cached_results()` branch left
  over from an earlier design. Removed; PR-38's new cache layer
  is the only cache path.

- **Windows hooks `.ps1` body parity audit** (PR-32, 4 hooks):
  audited every `.ps1` hook for body-logic drift vs its `.sh`
  sibling. The only legitimate backport was `check-no-fork-bomb`
  env-scrub (the `.sh` had it from v0.2.11, the `.ps1` didn't).
  Other 3 were already in parity; documented in
  `windows-templates-parity-audit-2026-05-16.md` context note.
  Windows task XML wrapper now uses `cmd.exe /c "set ..."` for
  env propagation (the previous `bash -c "export ..."` approach
  failed on Windows hosts without WSL2 / Git-Bash on PATH).
  UTF-8 encoding declaration in the task XML (was UTF-16, fixed
  in fixup commit `8d32c16`).

### Fixed

- **Fresh install left Claude Code with zero MCP servers wired**
  (PR-23): v0.2.11 install pipeline populated everything except
  the `~/.claude.json mcpServers` block. Users on a fresh
  v0.2.11 install would see no orchestrator MCPs in Claude Code
  and had to hand-edit `~/.claude.json` — the #1 first-install
  UX failure. Now the install pipeline writes them via the
  launcher CLI subcommand.

- **`hybrid_search` returned 0 results with cryptic error on
  Development collection + shared KG** (PR-24): the MCP's
  stale-data filter (`valid_until is_none OR > now`) hit a
  Weaviate "build inverted filter allow list" error because
  Development collections didn't have the 4 temporal props at
  all, and shared KG didn't have `indexNullState=True` so
  `is_none` queries failed. Migration script fixes existing
  installs; new installs get the right schema at create time.

- **Schema-not-found error from MCP misattributed to "Weaviate
  unreachable"** (PR-41): when a project's KG / Dev collection
  didn't exist in Weaviate (typically because user hadn't run
  `install.py --update` after a schema migration), the MCP
  returned a generic "WeaviateUnreachable" error to Claude,
  which then suggested checking the container — wasting cycles
  before the user realized the right action was to run the
  migration. Now distinguished: `WeaviateSchemaError` with the
  collection name + suggested migration command.

- **MCP env changes required Claude Code restart to take effect**
  (PR-42): editing `.claude/settings.json env` to update a per-
  project KG collection, RL server URL, etc. required quitting
  and reopening Claude Code to pick up the change (because the
  MCP subprocess inherits env at spawn time). Now the launcher
  watches `.claude/settings.json` and SIGHUPs the running MCP
  PIDs (clean exit → Claude Code respawns) so changes apply
  within ~1s. Manual button for users without the launcher open.

- **Per-project MCP env values shadowed by `~/.claude.json`** (PR-43):
  see Changed section. `EMBEDDING_MODEL` and `RL_SERVER_URL` were
  unconditionally written to `~/.claude.json mcpServers.*.env` at
  install time, and the reversed-precedence behavior in Claude
  Code's MCP spawn made those values win against per-project
  overrides. Allowlist now excludes per-project-varying keys.

- **Stale `~/.claude.json mcpServers` entries from prior install
  locations** (PR-33): users who relocated their orchestrator
  clone had `~/.claude.json` entries pointing at the old path.
  Claude Code would silently fail to spawn the MCP. Now
  `--update` detects + offers per-entry consent-prompted
  rewrite.

- **`code-graph-incremental` + `kg-summary-generator` +
  `pre-edit-context-inject` hooks silently fell through to
  system python on modern installs** (PR-25): the hooks hardcoded
  `claude_mcp_servers/.venv` only. Installs using the modern
  top-level `.venv` layout (since the v0.2.4 install overhaul)
  saw the hooks "succeed" with system python that didn't have
  Weaviate / sentence-transformers installed → the hook returned
  empty results silently. Now tries `$REPO_ROOT/.venv` first.

- **Cache-layer non-determinism in `pre-edit-context-inject.sh`**
  (PR-38): the bash port of the `.ps1` cache could under
  concurrent reads emit a partial result block (cache hit on
  chunk N but cache miss on chunk N+1 of the same KG node, where
  the node was emitted in 2 chunks). Now block-atomic: the cache
  key is the file path + offset + limit, and a result block is
  emitted whole or not at all.

- **Cargo.lock missed `notify` crate dep refresh** (commit
  `98df441`): PR-42 added the `notify` crate for the file
  watcher; Cargo.lock wasn't regenerated in the PR. Fixed in a
  trailing chore commit before push.

- **3 installer conflict-strategy tests asserted root CLAUDE.md
  was in the install whitelist** (commit `a5b8432`): PR-31
  removed it from the whitelist but the test fixtures still
  expected it to be overwritten by `install --strategy overwrite`.
  Tests updated to assert CLAUDE.md survives independent of
  strategy (it's now outside the allowlist).

- **4 test fixups for PR-34 (shared-KG canonical name) + PR-42
  (`reload_mcps_sighup` Tauri command) integration** (commit
  `952fa96`).

### Removed

- 97 shipped-duplicate `.claude/{hooks,scripts,settings.json}` files
  from the public repo (PR-39). Source of truth is `templates/`;
  rendering at install time eliminates the source↔shipped drift
  bug class.
- 12 internal dev artifacts from the public repo (PR-31): test
  scaffolding, dev-only context docs, and dev-laced reference
  notes that had crept into the shipped tree.
- Root `CLAUDE.md` from the install whitelist (PR-31). Orchestrator-
  self's own CLAUDE.md is dev-only; user projects get the project-
  template CLAUDE.md.
- `EMBEDDING_MODEL` and `RL_SERVER_URL` from the
  `_ALLOWED_GLOBAL_ENV_KEYS` allowlist (PR-43). These keys vary
  per-project; the reversed-precedence behavior in Claude Code
  meant including them in the global block shadowed per-project
  values.

### Security

- **Env-scrub on `lean-ctx-rewrite.{sh,ps1}` hooks** (post-v0.2.11
  patch commit `cb7ff88`): the hook now ships with full env-scrub
  for parity with every other VCO hook. Defense-in-depth: the
  hook doesn't process secrets directly, but `unset SUPABASE_KEY
  GITHUB_TOKEN ...` removes the exposure surface if a future
  change to the hook were to log the environment.
- **Backend-issued consent tokens for schema-migration
  destructive op** (PR-37 + PR-42): the schema-migration modal
  fetches the consent token from `issue_schema_migration_consent_token`
  rather than generating its own UUID. Single source of truth
  for the destructive-op gate; FE typos can't drift away from
  the contract.

### Known issues / deferred

- **Per-session MCP subprocesses** (Issue G from the v0.2.12 audit):
  Claude Code's MCP supervisor spawns a fresh MCP subprocess per
  chat session. The MCP spec doesn't currently support a daemon
  mode where multiple sessions share one MCP process. Cold-start
  latency (~300ms) repeats per session. 4 alternative
  approaches documented in
  `.claude/context/plans/v0.2.13-candidates-2026-05-16.md`;
  recommendation is to wait for upstream HTTP-MCP spec movement
  rather than build a custom daemon shim.
- **2 cargo test parallelism-sensitive flakes** (pre-existing
  from v0.2.11): `commands::kg_sync::tests::concurrent_drain_does_not_deadlock_on_large_stderr`
  + `stall_watchdog_kills_silent_subprocess` (subprocess
  fork/exec ENOENT under high concurrency). Pass with
  `--test-threads=1` and in isolation. Documented in
  `.claude/context/plans/v0.2.13-candidates-2026-05-16.md`.
- **2 OS-keychain shared-state cargo test flakes**:
  `commands::installer::tests::github_pat_keychain_tests::pat_file_to_keychain_migration_uses_user_slot_after_module_id_flip`
  + `register_github_pat_preserves_existing_user_file`. Both
  pass in isolation; race is on the OS keychain backend's
  shared state under parallel `cargo test`.

## [0.2.11] — 2026-05-16

Stability release. The orchestrator's own infrastructure (compose
files, boot-service auto-wiring, container lifecycle, lean-ctx
integration, project bundle migration, MCP server surface) had a
series of latent bugs that surfaced during the 2026-05-15 / 2026-05-16
debugging session. All fixed with cross-OS coverage on Linux + macOS +
Windows and "no hardcoded user paths" discipline. Goal: a release
that doesn't need to be upgraded for a while.

**Major theme — MCP simplification**: Ollama MCP and 3 of 4 Search
MCP tools were removed as redundant with Claude's native capabilities.
Ollama remains as embedding infrastructure (Weaviate vectorizers); the
MCP wrapper layer is gone. Search MCP narrowed to just `search_papers`
(OpenAlex + arXiv). SearXNG container also dropped (no surviving tool
needed it). Visible deferral entries surface in
`<project>/.claude/context/UPDATE_DEFERRED.md` for upgraders with
existing `~/.claude.json` entries.

**Major theme — container lifecycle hardening**: PR-12 + PR-13 + PR-15
form a defense-in-depth stack around the 2026-05-16 boot-cascade
failure mode (docker-without-daemon-access → CDI race → conmon killed
→ state-DB desync). bash hooks recover at SessionStart, launcher Rust
recovers continuously while open, install.py auto-repairs stale
systemd unit `WorkingDirectory=` paths on `--update`.

**Major theme — pytest test-isolation**: PR-16 fixes a class of bug
where tests called real install code without sandboxing the
`~/.config/systemd/user/` write path. Every developer running
`pytest tests/test_materialize_boot_service.py` had been silently
corrupting their own systemd unit since PR-12 landed. New
`VCT_USER_HOME_OVERRIDE` env var + autouse fixture make this
impossible going forward — prevention, not recovery.

### Added

- **Ensure-containers zombie recovery hook** (PR-13,
  `templates/hooks/ensure-containers.{sh,ps1}`): detects Podman
  state-DB-desync containers (PID dead per `/proc/<pid>` while
  `podman ps` still reports "Up") at Claude Code SessionStart.
  Force-removes the zombie via `runc delete --force` + `podman rm
  --force`, then recreates via the `launch-claude-mcp-stack.sh`
  wrapper if shipped (preserves CDI-wait + daemon-access semantics).
  Audit log to `~/.local/state/vct/container-recovery.jsonl`
  (Linux) / `$LOCALAPPDATA\vct\container-recovery.jsonl` (Windows).

- **Launcher Rust container-lifecycle defense layer** (PR-15,
  `launcher/src-tauri/src/services/runtime.rs` +
  `commands/lifecycle.rs`):
    - G1: `daemon_usable_probe()` validates `docker info` Server:
      section / `podman info` exit 0 before committing to a runtime.
      Prevents the 2026-05-16 cascade-class bug where a user with
      Docker Desktop installed but not in the docker group got
      runtime detection succeeding silently while every subsequent
      compose call failed.
    - G2: zombie-container detection in `services_status()` plus a
      new `recover_zombie(container_name)` Tauri command. Frontend
      can now show a "stuck" indicator with a Recover button.
      `ServiceRuntimeState` gets a new `zombie: bool` field
      (serde-defaulted for backward-compat).
    - G3: launcher's `services_start_all` / `services_restart_all`
      prefer the `launch-claude-mcp-stack.{sh,ps1}` wrapper over
      direct `compose up -d`. Prevents the `vco_code_embed` boot
      race where GPU container compose-up beats the NVIDIA CDI
      refresh, leaving the container stuck in "CREATED" state.

- **Boot-service auto-repair** (PR-12, `install.py`): new
  `_repair_systemd_unit_working_dir()` helper fires on
  `install.py --update`. Detects stale `WorkingDirectory=` values
  pointing at moved / deleted install paths (a common failure mode
  for users who relocated their orchestrator clone) and re-renders
  the unit at the correct path. Emits `boot_service_path_repaired`
  deferral so Claude Code's next-session check surfaces the change
  with the exact `systemctl --user daemon-reload` command to apply
  it.

- **Runtime detection daemon-access check (bash side)** (PR-12,
  `scripts/launch-claude-mcp-stack.sh`): new `_runtime_usable()`
  validates `docker info` / `podman info` actually exit-0 before
  the wrapper commits to that runtime. Pairs with PR-15 G1
  (launcher Rust side).

- **Runtime.txt multi-path search** (PR-12): the wrapper now probes
  `$VCT_STACK_RUNTIME_FILE` → `$VCT_STACK_WORKING_DIR/state/install/`
  → `$VCT_ORCHESTRATOR_ROOT/state/install/` → `<script_dir>/../state/
  install/` for the persisted runtime choice. Makes the wrapper
  resilient to a stale systemd unit `WorkingDirectory=` pointing at
  a deleted path.

- **Global lean-ctx detection** (PR-11, `install.py::_check_global_lean_ctx_hooks()`):
  defensive check at install start for global lean-ctx artifacts
  (`~/.claude/settings.json` `hooks.PreToolUse` entries containing
  "lean-ctx", `~/.claude/hooks/lean-ctx-*` files). When detected,
  emits a LOUD warning to stderr + `global_lean_ctx_hooks_detected`
  deferral entry with the exact removal commands. The 2026-04-30
  and 2026-05-15 fork-bomb incidents both involved global lean-ctx
  hooks; this surface lets future users hit the same trap less
  often. New CLI flag `--suppress-lean-ctx-warning` for users who
  intentionally keep them (e.g. lean-ctx development).

- **Storage UX choice (named vs bind mount)** (PR-10A,
  `launcher/src-tauri/src/commands/storage_ux.rs` + Svelte
  Settings → Storage card): user can pick between runtime-managed
  named volumes (default, recommended) and a user-chosen bind path
  via the Settings GUI, with auto-detection of pre-existing legacy
  volumes from prior installs. STRICT volume allowlist (`vco_*`
  prefix + curated legacy names) prevents the launcher from ever
  offering to alias another project's data into our compose
  mountpoints. Override.yml generator covers 3 shapes: empty
  (named-default), bind-mount per service, external-alias per
  service. 24 Rust unit tests cover the allowlist boundary +
  render idempotency + atomic write.

- **Legacy KG / codegraph collection detection on Add Project**
  (PR-10B, `vco_lib/project_init.py`): when adding a new project,
  the install pipeline now detects pre-existing same-suffix
  legacy KG / codegraph collections (Levenshtein-based prefix
  similarity check + Weaviate REST API call for object counts +
  embedding-dim sniff via schema). Emits per-project deferral
  entries listing the candidates so the user can decide to adopt
  / archive / drop. 35 new tests.

- **GUI lean-ctx toggle in HooksTab** (PR-6,
  `launcher/src/lib/project-state/HooksTab.svelte` +
  `launcher/src-tauri/src/commands/claude_env.rs`): per-project
  3-state toggle (off / on / default) for `VCO_LEAN_CTX_DEFAULT`.
  Writes `.claude/env`. 19 Rust unit tests for the
  `get_claude_env_value` / `set_claude_env_value` Tauri commands.

- **Pytest test-isolation sandbox** (PR-16, `install.py` +
  `tests/test_materialize_boot_service.py`): new
  `_user_home_for_install()` helper consolidates all
  systemd-unit / launchd-plist / repair-function writes through a
  single env-overridable lookup (`VCT_USER_HOME_OVERRIDE`). New
  autouse fixture sandboxes every test in
  `test_materialize_boot_service.py` so a forgotten
  manual-monkeypatch by a future test author can no longer corrupt
  developer machines' real systemd state. Includes a regression
  guard test that proves the real user systemd unit `mtime` is
  unchanged across a full test-suite run.

### Changed

- **MCP server surface simplified** (PR-14a + PR-14b + PR-14c):
    - Ollama MCP server removed entirely from default install
      (`claude_mcp_servers/ollama_mcp/` deleted,
      `mcpServers.ollama` block removed from both Linux + Windows
      settings templates, manifest `vct-ollama.json` deleted).
      Ollama as embedding **infrastructure** (Weaviate qwen3-
      embedding vectorizer + code_embedding_service fallback)
      remains unchanged.
    - Search MCP narrowed to only `search_papers` (OpenAlex +
      arXiv academic paper search). The dropped tools
      (`web_search`, `search_code`, `fetch_page`) are all
      redundant with Claude's native WebSearch / WebFetch + the
      `site:github.com lang:X` qualifier pattern. Manifest
      `vct-search.json` updated: removed `github_pat` secret +
      `SEARXNG_URL` setting; version bumped 0.3.1 → 0.4.0.
    - SearXNG container removed from default compose
      (`searxng_data` volume gone; `templates/searxng/` template
      deleted; `install.py::_materialize_searxng_settings()`
      function removed).
    - CLAUDE.md / README.md / `docs/features/02-mcps-and-agents.md`
      / 18 agent templates / 2 skill templates / 8 KG nodes
      updated to reflect the new shape. New KG node
      `knowledge/concepts/mcp-simplification-v0211.md` documents
      the decision rationale.
    - 3 visible deferral entries for upgraders with existing
      `~/.claude.json` `ollama` blocks or SearXNG remnants:
      `searxng_removed_from_default_install`,
      `ollama_mcp_deprecated`, `search_mcp_simplified`. Nothing
      auto-removed from user state — clear messages with the
      exact cleanup commands.
    - Search MCP wrapper `claude_mcp_servers/search_mcp/server.py`
      net -322 LOC after stripping dropped tools.

- **SSRF whitelist in `pre-tool-use.{sh,ps1}` hooks** (PR-17):
  removed `localhost:8888` (SearXNG, no longer ships) and the
  `mcp__search__fetch_page` tool-name guard (tool removed in
  PR-14a). Added `localhost:11440` (`code_embed`) which was missing.
  Explanatory comment retained pointing maintainers at PR-14a
  for the v0.2.11 deprecation rationale.

- **Effort-level guidance for ad-hoc agent spawning** (PR-14c,
  `CLAUDE.md`): default recommendation flipped from `xhigh` to
  `high` for substantive work. Reserve `xhigh` / `max` only for
  genuinely deep-reasoning tasks where `high` has empirically
  fallen short. `high` is the right default for ad-hoc spawned
  agents; `xhigh` is overkill in most cases and adds latency /
  token cost without proportional quality gain.

- **`vct-module.json` canonical counts updated**: 5 MCP servers →
  4 (3 Python-stack containerized — weaviate_mcp, search_mcp,
  code_embedding_service — plus 1 system-managed Playwright). The
  comment field documents the v0.2.11 simplification.

### Fixed

- **`vco_code_embed` stuck in CREATED state after boot** (PR-15
  G3 + PR-12 wrapper changes): GPU container compose-up was racing
  the NVIDIA CDI refresh (`/var/run/cdi/nvidia.yaml` not yet
  populated). Now the launcher invokes the wrapper script which
  waits up to ~10s for CDI to be ready before bringing GPU
  containers up. Prevention, not recovery.

- **Docker-without-daemon-access cascade** (PR-12 + PR-15 G1):
  users with Docker Desktop installed but not in the `docker`
  group used to get runtime detection succeeding silently while
  every subsequent compose call failed permission-denied. Now
  rejected at detection time, runtime falls through to Podman.

- **Test corruption of user's real systemd unit** (PR-16): see
  Added section. Was silently corrupting developer machines
  since PR-12 landed.

- **Stale `WorkingDirectory=` in systemd unit across install
  moves** (PR-12): when users relocated their orchestrator clone,
  the systemd unit kept pointing at the old path. Now auto-
  repaired on `install.py --update` with a visible deferral.

- **Search MCP `vct-search.json` declared `github_pat` secret it
  no longer needed** (PR-19): cargo test asserted the secret was
  declared; PR-14a + PR-14b removed it (since `search_code` is
  gone). Test inverted to assert the secret is NOT declared
  (regression guard).

- **`check-no-fork-bomb.sh` missing env-scrub line** (PR-19): the
  hook had the `VCT_DISABLE_HOOKS` opt-out but not the standard
  `unset SUPABASE_KEY GITHUB_TOKEN ...` scrub. Added for parity
  with every other VCO hook (defense-in-depth, not active
  vulnerability — the hook doesn't process secrets).

### Removed

- `claude_mcp_servers/ollama_mcp/` (entire directory + Python module +
  requirements.txt). MCP layer dropped; Ollama as infrastructure
  stays.
- `claude_mcp_servers/search_mcp/server.py` tools: `web_search`,
  `search_code`, `fetch_page` (only `search_papers` survives).
- `templates/searxng/` (template directory) + SearXNG service from
  `claude_mcp_servers/compose.yaml` + `install.py::_materialize_searxng_settings()`
  + `templates/settings.json.*.template` `SEARXNG_URL` env vars +
  `launcher/bundled_manifests/vct-ollama.json` manifest.
- `tests/test_ollama_vision_gating.py` (tested the deleted MCP).

### Migration notes (for users on v0.2.5–v0.2.10)

- **CHANGELOG re-anchoring**: v0.2.5 through v0.2.10 shipped without
  per-release CHANGELOG entries during a period of fast iteration
  (see git tags + merged PRs at each version for content). v0.2.11
  re-establishes the Keep-a-Changelog discipline going forward.
- **`~/.claude.json` cleanup**: if your file has an `ollama` block
  under `mcpServers`, you'll see an `ollama_mcp_deprecated` deferral
  notice on next `install.py --update`. Remove the block manually
  for a clean state. Ollama-as-infrastructure (the container) is
  unaffected.
- **SearXNG container**: if you had it running locally, you'll see
  a `searxng_removed_from_default_install` deferral with the
  cleanup commands. No urgency; the container just becomes orphaned
  cruft.
- **Stale systemd unit**: `install.py --update` will detect + repair.
  If you see a `boot_service_path_repaired` deferral, run
  `systemctl --user daemon-reload` to apply.

### Skipped this release (deferred to v0.2.12)

- Launcher-centralized MCP env architecture (one `~/.claude.json`
  MCP entry per server + wrapper.sh trampoline reading per-project
  env from a launcher hub API). 2-3 day Rust + SQL + shell work;
  too big for the stability release.
- Install-time storage prompt in install.py CLI (PR-10A ships the
  launcher GUI surface; CLI prompt requires `install.py` to shell
  out to the launcher binary).
- `lean-ctx-rewrite.{sh,ps1}` envelope-coverage runtime test
  (requires installing lean-ctx in CI; exempted from envelope-
  literal test for now since envelope is produced by upstream
  binary not by the wrapper).

## [0.2.5] — [0.2.10] — internal iterations

Multiple releases between 0.2.4 (2026-05-12) and 0.2.11 (2026-05-16)
shipped without per-release CHANGELOG entries. See `git log
v0.2.4..v0.2.10` for the commit history and the corresponding GitHub
release pages for shipped artifacts. v0.2.11 re-establishes the
Keep-a-Changelog discipline going forward.

## [0.2.4] — 2026-05-12

Same-day follow-up to 0.2.3. Three bugs surfaced while adding a long-lived legacy project (a
project registered by a pre-v0.2.0 orchestrator) via the v0.2.3
launcher.

### Fixed

- **Schema incompatibility on pre-existing Weaviate collections**
  (blocking). Two distinct mismatches stopped add-project for users
  with old orchestrator state:
    1. **Case-only naming conflict** — old lowercase `<Project>_development`
       blocked the new capital `<Project>_Development` (Weaviate
       treats case-only differences as "similar"). POST /v1/schema
       returned 422 "class already exists: found similar class".
    2. **Multi-named-vector mismatch** — pre-2026-04 collections
       carried both `ollama_embed` (legacy snowflake) AND
       `qwen3_embed` named vectors. Current sync sends a single
       vector. Insert returned 422 "configured with multiple named
       vectors, but received a single vector".
  `vco_lib.project_init.bootstrap_collections` now diffs the actual
  schema of any pre-existing target against the current spec.
  Detection is narrow: additive property drift still fixes via the
  in-place `patch_props` path (Weaviate 1.28.4 allows it); regen is
  reserved for what genuinely cannot be patched on a running
  collection (vectorConfig slot changes, indexNullState, legacy
  no-vectorConfig). When regen IS needed, the collection is dropped
  + recreated with current spec; the existing post-bootstrap
  `kg-sync` task re-ingests from `knowledge/**/*.md` (the lossless
  source of truth). Sub-second per collection on a healthy Weaviate.
  Surfaced via the existing `warnings[]` channel of
  `run_bootstrap_collections` — no new banner state (JSON envelope's
  `regenerated[]` array IS forward-compatible with a real banner if
  promoted later).

- **Optimistic counter lie on subprocess crash**. `kg_syncs` rows
  could report `kg_succeeded: N` despite the script crashing on the
  very first insert — every `🔄 Syncing node:` log line incremented
  the optimistic counter; the final `📊 KG: X succeeded, Y failed`
  summary never emitted; the optimistic count persisted as truth.
  `commands::kg_sync.rs::run_sync_task` now reconciles on crash: if
  the subprocess exits non-zero AND the canonical summary line was
  not parsed, the not-yet-summarized stage's counters reset to
  (succeeded=0, failed=total). Stage-aware: KG-completed-then-Docs-
  crashed only resets the Docs counters. Investigation finding:
  `commands::kg_summary.rs` and `commands::codegraph.rs` do NOT have
  this pattern — kg_summary returns a per-invocation `NodeOutcome`
  enum (always confirmed outcomes); codegraph parses
  `files_analyzed` only on `out.status.success()` (failure branch
  explicitly uses 0). Fix scoped to kg_sync only.

- **AddProject dialog "previous orchestrator content" banner
  rendered as 3 broken columns**. The pre-existing-content sentence
  was passed as separate Svelte children into a flex container,
  splitting "X agents, Y skills will be" / "preserved" / "; hooks…"
  into adjacent boxes — "preserved" hidden behind the X close
  button. Fix: `leftoverSummaryText()` TypeScript helper assembles
  the full sentence as a single string; rendered as plain text in a
  single block. The inline `<strong>preserved</strong>` emphasis is
  dropped (reintroducing it would require `@html` plus explicit
  escaping of every dynamic count — risky for decorative emphasis).
  Plain sentence reads cleanly and matches the dialog's flat tone.

### Tests

- **+ 23 new tests** (Rust 579, was 575; Python 837, was 818 net of
  one pre-existing unrelated failure):
  - 19 Python tests in `test_vco_lib_project_init.py` covering
    `_schema_incompatible` decision matrix,
    `_extract_similar_class_name` (both JSON-escaped + unescaped 422
    bodies), `_drop_and_recreate` happy path + Weaviate-down failure
    path, and the end-to-end bootstrap regen flow.
  - 4 Rust tests in `commands::kg_sync` for
    `reconcile_optimistic_counts_on_crash` (KG-only, Docs-only,
    both-stages, summary-parsed-no-reset).

### Notes for upgraders

- **Existing projects with stale schemas** will trigger one regen
  pass on their next bootstrap (via the `Re-sync KG` header button
  or a manual `python -m vco_lib.project_init
  bootstrap-collections`). The regen is lossless — Weaviate is
  re-populated from `knowledge/**/*.md` immediately afterward. No
  data on disk is touched.
- The `regenerated[]` JSON envelope field is new in 0.2.4. External
  tooling parsing the bootstrap-collections JSON output should
  ignore unknown fields (the existing `actions[]` / `errors[]`
  channels are unchanged).

---

## [0.2.3] — 2026-05-12

Same-day follow-up to 0.2.2. Adds the third instance of the
"background task with banner + pill + retry + resume" pattern: auto-
backfill of `<project>/knowledge/.node_formats.json` (the LLM-generated
summary sidecar consumed by `hybrid_search`'s `summary` tier, score
0.42–0.55).

Before 0.2.3, that sidecar was only populated lazily by the
`PostToolUse` hook `kg-summary-generator.{sh,ps1}` when a user edited
each KG node in a Claude session. A project with 50 pre-existing
nodes therefore needed 50 Claude sessions to fully populate. With
0.2.3 the launcher walks `knowledge/**/*.md` once on add-project and
shells out to `templates/scripts/generate-kg-summary.py` for each
file, in the background.

### Added

- **KG-summary auto-backfill on add-project**
  (`commands::kg_summary`, `db::kg_summaries`, migration 012). After
  bundle install, `create_project_v2` queues a `kg_summaries` row and
  spawns the summariser as a background task (in parallel with the
  kg-sync task added in 0.2.2 — not chained; the summariser reads the
  `.md` file body directly and only needs Weaviate for the optional
  per-chunk path on multi-chunk nodes). Progress streams to the GUI
  via the `kg-summary-progress` Tauri event with live `nodes_total`,
  `nodes_succeeded`, `nodes_unchanged`, `nodes_failed`,
  `nodes_skipped`, and `backend` counters. Mirrors
  `commands::kg_sync::spawn_initial_sync` line-by-line — same DB
  shape, same `current_dir(std::env::temp_dir())` discipline, same
  `CREATE_NO_WINDOW` on Windows, same race-checks for
  user-unregister-mid-run.

- **Three-tier backend fallback in `generate-kg-summary.py`**:
  `claude` CLI on PATH → Ollama at `KG_SUMMARY_OLLAMA_URL` (default
  `http://localhost:11435`, model `KG_SUMMARY_OLLAMA_MODEL`,
  default `qwen3.5:9b`) → `ANTHROPIC_API_KEY` direct → silent skip
  (`exit 0` with `KG-summary: no backend available`). Forced backend
  via `KG_SUMMARY_BACKEND=cli|ollama|api|skip`. Per-call timeout via
  `KG_SUMMARY_TIMEOUT` (default 180 s). The launcher detects the
  no-backend marker on the first node, hard-stops the walk, and
  transitions the row to `skipped` with an actionable hint
  (install `claude` CLI / start Ollama / set `ANTHROPIC_API_KEY`) —
  no point invoking the same script for the remaining N nodes.

- **`Re-build KG summaries` header button** on `/project/[id]`,
  third in the row next to `Re-build code graph` (0.2.x) and
  `Re-sync KG` (0.2.2). Mirrors `resyncKg` end-to-end (same
  `.rebuild-btn` style, `rebuildingSummaries` loading-state guard,
  `retry_kg_summary` Tauri command, toast convention). The
  summariser content-hashes each node, so repeated clicks on an
  already-summarised project are a cheap no-op (logged as
  `nodes_unchanged`).

- **Third banner on `/project/[id]`** (`KgSummaryBanner.svelte`),
  mounted on top of the existing pair (`KgSummaryBanner` →
  `KgSyncBanner` → `CodeGraphBuildBanner`, top-down). Newest task
  on top, matching the add-project spawn order (codegraph → kg-sync
  → kg-summary). Self-managed visibility: `pending`/`running`/
  `failed` always visible; `success`/`skipped` auto-hide 30 s after
  `finished_at_iso`. The `skipped` state surfaces the
  `Show details` button so a user without a summariser backend
  sees the install hint inline.

- **Third pill on `/project`** (`KgSummaryPill.svelte`), mounted
  next to `CodeGraphBuildPill` and `KgSyncPill` in each project's
  list row. Passive read-only — the project page is the action
  surface.

- **Resume-after-crash extended to a third task type.** On launcher
  boot (in `lib.rs::setup()`), the existing two-phase sweep
  (introduced 0.2.2) now also runs `kg_summary::resume_pending_summaries`:
  phase 1 marks any `kg_summaries.status='running'` rows as `failed`
  with `"launcher crashed mid-run; click Retry to re-run"`; phase 2
  re-spawns `status='pending'` rows. The boot-log line now reports
  three task types:
  `[vct] resume-sweep: code-graph (...); kg-sync (...); kg-summary (running→failed: N, pending respawned: M)`.

- **Marker-string drift guards.** `commands::kg_summary` ships two
  unit tests (`no_backend_marker_string_matches_script_log_line`
  and `unchanged_marker_string_matches_script_log_line`) that hold
  a literal snippet of the canonical log lines from
  `generate-kg-summary.py` and assert that the `NO_BACKEND_MARKER`
  and `UNCHANGED_MARKER` constants still substring-match. A future
  rename of the script's log message will fail the test loudly
  rather than silently mis-classifying every run as `succeeded`.

### Changed

- **Boot-log resume-sweep line now reports all three task types**
  (code-graph + kg-sync + kg-summary). 0.2.2 reported two.

### Tests

- **+25 new tests** (575 total, was 550 after 0.2.2). New coverage:
  `db::kg_summaries` (9 — row CRUD, state transitions,
  invalid-status, `log_tail` truncation, FK cascade, resume sweep);
  `commands::kg_summary` (16 — enumerate-walk × 4,
  `resolve_summary_script` × 2, `resolve_venv_python` × 3,
  `parse_backend_from_stdout` × 2, `tail_log` × 2, `append_log` × 1,
  marker-string drift × 2). `cargo test --lib`: 575 passed, 0 failed,
  1 ignored. `cargo check`: clean. `svelte-check`: 0 errors.

### Notes for upgraders

- **No migration required for users.** Migration 012 creates
  `kg_summaries` on first boot; pre-0.2.3 projects can opt-in via
  the new `Re-build KG summaries` header button. The summariser
  content-hashes nodes, so existing projects with the sidecar
  partially populated by the on-edit hook will re-run as
  `unchanged` (cheap no-op) for any node whose body hasn't changed.
- **No backend?** Users without `claude` CLI installed and without
  Ollama running on `KG_SUMMARY_OLLAMA_URL` will see the third
  banner go yellow `skipped` after the first node's subprocess
  detects "no backend available". The `Show details` toggle shows
  the install hint. Summaries also still backfill incrementally on
  each subsequent `knowledge/**/*.md` edit via the PostToolUse hook
  `kg-summary-generator.{sh,ps1}` — the 0.2.3 work is purely a
  startup optimisation; it doesn't change the lazy path.
- **First boot after upgrade** may sweep stale `running` rows
  across all three task types from a previous launcher session.

## [0.2.2] — 2026-05-12

Single-commit cycle focused on closing the add-project KG-sync gap and
elevating background-task status from pills to full-width banners.

Originated from a user-project revival (2026-05-12): user added a
project with ~58 pre-existing `knowledge/**/*.md` nodes via the launcher
GUI, but the project's `_KnowledgeGraph` collection stayed empty until a manual
`.claude/scripts/kg-sync --all`. The launcher add-project flow now
handles this automatically — same lifecycle pattern as the existing
code-graph build.

### Added

- **KG auto-sync on add-project** (`commands::kg_sync`,
  `db::kg_syncs`, migration 011). After bundle install,
  `create_project_v2` queues a `kg_syncs` row and spawns
  `.claude/scripts/kg-sync --all` as a background task. Progress
  streams to the GUI via the `kg-sync-progress` Tauri event;
  failure surfaces a Retry button. Mirrors
  `commands::codegraph::spawn_initial_build` line-by-line — same DB
  shape, same cross-platform script resolution
  (`kg-sync.ps1` on Windows, `kg-sync` POSIX wrapper elsewhere),
  same `CREATE_NO_WINDOW` Windows handling, same
  `eq_ignore_ascii_case` for macOS HFS+ folding.

- **`Re-sync KG` header button** on `/project/[id]`, next to
  the existing `Re-build code graph`. Mirrors `rebuildCodeGraph`
  end-to-end (same `.rebuild-btn` style, loading-state guard,
  toast convention).

- **Resume-after-crash** for both background task types. On
  launcher boot (in `lib.rs::setup()`), two-phase sweep:
  Phase 1 marks any `status='running'` rows as `failed` with
  `"launcher crashed mid-run; click Retry to re-run"` — the
  banner renders the failed state so the broken lifecycle stays
  visible (silent re-spawn would mask the crash). Phase 2
  re-spawns `status='pending'` rows via the same mechanism as
  `create_project_v2`. Honors the long-standing
  `list_pending_code_graph_builds` docstring contract that
  'running' rows are NOT auto-resumed.

### Changed

- **Pills → full-width banners** on `/project/[id]`. New
  `KgSyncBanner.svelte` + `CodeGraphBuildBanner.svelte` replace
  the previous pills above the tab-nav, stacked vertically
  (KG-on-top — newer task). Self-managed visibility:
  `pending`/`running`/`failed` always visible; `success`/`skipped`
  auto-hide 30 s after `finished_at_iso`. Inline expand-on-click
  for failure detail + Retry. Modeled after
  `BrowserModeBanner` (row decoration) + `.orch-banner` (action-row
  layout). Slimmed-down passive pills remain on the projects-list
  page (`/project`) where a banner-per-row would break the grid.

- `list_pending_code_graph_builds` is no longer dead code —
  `#[allow(dead_code)]` stripped now that
  `codegraph::resume_pending_builds` wires it from launcher boot.

### Tests

- **+ 7 new tests** (550 total, was 543). New coverage:
  `mark_orphaned_running_*` helpers (insert 'running' row → marked
  failed with crash-recovery message, no new task spawned);
  `list_pending_*` round-trip (insert 'pending' → respawn yields
  fresh task); kg_syncs row CRUD + state transitions +
  `log_tail` truncation + project FK cascade.

### Notes for upgraders

- **No migration required for users**. Existing projects keep
  their `code_graph_builds` rows; new `kg_syncs` table is created
  on first boot via migration 011 (no data backfill — pre-existing
  projects can opt-in via the new `Re-sync KG` header button).
- **First boot after upgrade** may sweep stale `running` rows from
  a previous launcher session (now marked failed with the recovery
  message). The boot log line
  `[vct] resume-sweep: code-graph (running→failed: N, pending respawned: M); kg-sync (...)`
  reports the counts.

---

## [0.2.1] — 2026-05-10

26 commits since 0.2.0 (#172–#197). Themes: per-project secret grants
+ per-requester active gate (the headline 0.2.1 work, #187/#188),
launcher GUI polish from the post-0.2.0 backlog (#196/#197),
hook-contract audit findings (#190 + sweep), release-pipeline
modernisation (uniform `.zip`, externalized local config, archive
contents), and a substantial doc-audit pass (#180–#183).

### Added

- **Per-project secret grants + per-requester active gate** (#187, #188).
  Migration 009 extends `secret_active_state` with a
  `requester_project_id` PK column and adds a new `secret_grants`
  table. The hub `project_env` resolver walks `list_grants_by_grantee`
  for the consuming project and threads the project_id as the
  per-(secret × requester) gate, so a per-project pause on a shared
  secret takes effect even though the keychain row is shared, and a
  per-project grant lets one project see another's secret without
  re-entry. Five new Tauri commands surface the grants/pause UX:
  `grant_secret`, `revoke_secret_grant_cmd`, `list_grants_for_project`,
  `pause_secret_for_project`, `resume_secret_for_project`. Cross-
  launcher schema-compat path probes a sibling DB's
  `secret_active_state` for the `requester_project_id` column via
  `PRAGMA table_info` and falls back to the legacy single-row query
  when the sibling pre-dates migration 009.
- **`vct-config.toml` local-machine config** (#189, #192). New
  `LocalConfig` loaded at launcher startup from next to the binary.
  Resolution order: env-var override → `vct-config.toml` →
  compiled default. First externalized field is `weaviate_url` (the
  user's local KG instance address). Product-fixed URLs (license
  validate, GitHub repo) stay baked in. Release archive ships
  `vct-config.toml` (renamed from `vct-config.example.toml` source)
  alongside the binary.
- **GUI: shared-tab key-collision badge, "Update all projects" button,
  per-project MCP toggle UI** (#197). Three post-0.2.0 backlog items:
  - SecretsPanel rows render a "shadowed" badge when a key is set at
    multiple scopes (`per_project > shared > global` precedence). New
    `list_user_secret_keys_v2` returns `is_shadowed` + `winning_scope`
    per row.
  - New `update_all_projects(opts)` Tauri command iterates registered
    projects sequentially. New three-phase `UpdateAllProjectsModal`
    Svelte component drives the UX (confirm → running → report).
  - PermissionsTab gains an MCP-servers section above the generic
    permissions table. New `list_project_mcp_permissions` +
    `set_project_mcp_permission` commands. Default-enabled semantic:
    enable=DELETE the row, disable=UPSERT explicit
    `config.enabled=false`. Env-writer mirrors disabled state into
    `.claude/settings.json::disabledMcpjsonServers`.
- **Custom MCP tab now populated by initial project registration** (#196).
  Migration 010 adds `project_mcp_servers` table, populated by a new
  `populate_mcp_servers` step in `project_state_populate.rs` that
  mirrors `<folder>/.claude/settings.json::mcpServers` AND
  `<folder>/.mcp.json` into the table. `is_user_added=true` flag
  driven by a bundled-allowlist (`weaviate-kg`, `ollama`, `search`,
  `code-embedding`, `playwright`, `vct-coordination`). Startup
  backfill in `lib.rs::run` re-runs the populate step for projects
  registered before migration 010 (idempotent — preserves user
  toggles).
- **Lightweight Rust wiring for `--lightweight` re-install** (#196).
  `InstallConfig` carries `lightweight: bool` and
  `lightweight_old_path: Option<String>`; `install_orchestrator`
  honours them by skipping the file-copy stage and invoking
  `install.py --lightweight [--lightweight-old-path <path>]`
  directly. The OnboardingWizard's re-install conflict modal exposes
  a "Fast reinstall" checkbox + optional previous-path text input.
- **Code-graph rank-tier helper extraction** (#191). New
  `_format_code_result_by_rank` shared helper in
  `claude_mcp_servers/weaviate_mcp/server.py`; used by both the MCP
  `search_code_graph` path and the `.claude/scripts/query_code_graph.py`
  CLI (which the pre-edit hook invokes). Result content is now
  byte-identical across the two surfaces. Rank-keyed tier policy
  preserved (top-2 = full + siblings; rank 3-4 = truncated 1200 chars;
  rank 5+ = ref-only).
- **Hook-contract audit fixes** (#190). Five-fix wave: (1)
  `config-change-audit` reads stdin JSON instead of empty
  `$CLAUDE_TOOL_NAME`/`$CLAUDE_TOOL_ARGS` env vars (audit log was
  silently empty before); (2) `post-tool-security` routes credential
  alerts through `hookSpecificOutput.additionalContext` so the model
  actually sees them (was plain stdout, dropped by the PostToolUse
  contract); (3) `post-file-edit` rewrite — pure-status banners
  deleted, three real LLM nudges (CONTEXT_STATE expert-skill,
  workflow-system file edits, code-edit reminder) routed through
  `additionalContext`; (4) env-scrub + `VCT_DISABLE_HOOKS` short-
  circuit added to three hooks that lacked them; (5) new
  `LEAK_TEST_KEY` recognizer pattern so smoke tests use a synthetic
  marker instead of real-looking AWS-key fixtures.

### Changed

- **Release archives are uniform `.zip` for all OSes** (#192).
  Linux + macOS switch from `.tar.gz` to `.zip`; `.sha256` sidecar
  per archive. `zip` is preinstalled on `ubuntu-latest` and
  `macos-latest` runners; Windows continues to use `Compress-Archive`.
- **Release pipeline ships only per-OS archives + sha256** (#175).
  Standalone launcher binaries dropped from Release assets — they
  now live only inside the per-OS archive. Workflow artifacts retain
  the standalone binary for QA.
- **Cross-OS staging via `tar+excludes` instead of `rsync`** (#172).
  `rsync` is not preinstalled on the Windows GitHub-hosted runner;
  switching to `tar -cf - --excludes ... | tar -xf - ...` lets the
  same staging logic run on all three runner OSes.
- **Release archives now ship `docs/`, root `*.md`, `CLAUDE.md`,
  `KNOWN_ISSUES.md`, `MIGRATION-<version>.md`** (#185). Users get the
  full doc surface on extract; previous archives shipped only
  `BOOTSTRAP.md` + `LICENSE`. Doc-leak scrubber runs in CI before
  upload.
- **Mass-rename `background: true` → `async: true`** in all settings
  files (#190, 36 occurrences). Per current docs, `async` is the
  canonical hook-handler field; `background` was at best an
  undocumented alias.
- **Stale OS-EXEMPT-PARITY markers cleaned** (#191, 5 hooks). The
  `.sh` siblings of `pre-compact-save`, `code-graph-incremental`,
  `cost-tracker`, `kg-update-nudge`, `kg-summary-generator` had
  parity markers that were superseded by genuine cross-OS parity
  via `find-python.{sh,ps1}` helpers.

### Fixed

- **`register_github_pat` ↔ SecretsPanel `module_id` unification**
  (post-0.2.0 backlog #6, #195). The OnboardingWizard /
  `/preferences` Manage Token flow wrote the PAT at
  `vct._user_shared_.shared.installer/github_pat` while the SecretsPanel
  "Shared (this user)" tab wrote at
  `vct._user_shared_.shared.user/github_pat`. A user who registered via
  the wizard and later edited via the SecretsPanel ended up with two
  divergent keychain rows; reads from either path returned only that
  path's value, so the alternate row sat as a stale shadow. Both
  writers now use `module_id="user"` (the canonical user-bucket
  enforced by `is_user_emit_bucket`) and `vct-module.json::bundled_secrets[0].module_id`
  matches. Existing 0.2.0 installs are migrated on next
  `register_github_pat` call by `migrate_github_pat_installer_to_user_module_id`,
  which copies the value at the old `installer/` slot into the new
  `user/` slot (or, if both slots have values, keeps the user-bucket
  one because the SecretsPanel write happened later in time) and
  deletes the old keychain row plus its active-flag entry. Audited as
  `github_pat_module_id_migration` for traceability.
- **Pre-edit hook cache typo: `$KG_TMP_RAW` / `$CODE_TMP_RAW` →
  `$KG_RAW` / `$CODE_RAW`**. The cache file was always empty
  post-search because the wrong variable names were referenced; every
  subsequent edit of the same file re-ran the live search instead of
  replaying the cached blocks. Now the cache populates as designed
  and TTL replays work.
- **Pre-edit hook KG/codegraph injection dedup correctness +
  session_id-from-stdin sweep** (#176, #186). Producer/consumer header
  drift fixed: `rl_kg_search.py --hook-format` and
  `query_code_graph.py search --hook-format` now emit `KG: <title>`
  and `CODE: <full_name>` headers the pre-edit hook's regex actually
  matches. Whitespace-only filtered output no longer surfaces an
  empty `[Pre-edit context for ...]:` system-reminder. Cache stores
  raw pre-dedup blocks so replays apply the current seen-list.
  Universal stdin-JSON sweep across 13 hooks via
  `_lib/find-python.{sh,ps1}` resolves cross-OS Python invocation
  (`python3` is missing on Windows; `md5sum` and `stat -c %Y` are
  GNU-only).
- **CONTEXT_STATE.md staleness nudge: counter-based + session_id
  keying** (#177). The KG-update-nudge hook now triggers once per
  session_id when the cumulative work-units counter crosses
  thresholds, instead of firing repeatedly on every prompt. Atomic
  state persistence under `~/.claude/projects/<project>/state/`.
- **Column-rename migration recorded for column drift** (#184). A
  prior in-place column rename in `launcher.db` was never captured in
  the migrations registry; this PR adds the retroactive entry so
  fresh installs match upgraded ones byte-for-byte.
- **`post-git-commit-kg-sync` echoed "KG sync agent spawned" to stdout
  under `async: true`** (#193). PostToolUse plain stdout is silently
  dropped per the v2.1.x contract AND the wiring is fire-and-forget,
  so the line was doubly discarded. Replaced with a contract-explainer
  comment so future audits don't flag it.
- **`cookie@0.6.0` CVE-2024-47764 npm audit alert** (#194).
  `launcher/package.json` carries `overrides: { "cookie": "^0.7.0" }`
  pinning the lockfile to `cookie@0.7.2`; `npm audit` from `launcher/`
  reports 0 vulnerabilities.
- **Latent `query_code_graph.py::search_by_concept` bug**: was
  calling `near_vector` without `target_vector` on the dual-vector
  collections (which the new Weaviate client rejects). Fixed alongside
  the helper extraction (#191).

### Docs

- **Doc-audit Groups A–D** (#180, #181, #182, #183). Four-pass sweep
  across user-facing root + core (Group A), license module (Group C),
  features pages (Group B), and maintainer-doc triage (Group D).
  Factual + tone refresh; maintainer-internal references scrubbed
  from public files; `internal/` directory split for repo-only
  content.
- **README rewrite + verified comparison table** (#179). New
  comparison table grounded in feature-by-feature verification rather
  than marketing claims.
- **Post-ship 0.2.0 sweep** (#173). Version-ref drift, roadmap-label
  consistency, hub-auth section refresh.
- **CLAUDE.md guidance**: agents fix outdated knowledge they retrieve
  in the same turn rather than acting on stale info (#174); warning
  about parallel-agent worktree isolation failure modes (#178). The
  worktree-isolation warning was load-bearing for this 0.2.1 cycle —
  see the fresh KG node `parallel-pr-coordination-gotchas-2026-05-10`
  for the failure modes hit during the parallel-PR push.

### Notes for upgraders from 0.2.0

- The `register_github_pat` migration is automatic and runs on first
  invocation of the wizard / Manage Token flow after upgrade; the
  legacy `installer/` keychain slot is read-fallback-then-removed.
  No user action required.
- The migration-010 `project_mcp_servers` backfill runs at startup
  for every registered project that has zero rows (i.e. every
  pre-0.2.1 project). Per-project, idempotent; user toggles are
  preserved across runs.
- `vct-config.toml` is shipped at the archive root; users can edit it
  in place after install. Env-var overrides (`VCT_WEAVIATE_URL`,
  legacy `WEAVIATE_URL`) take precedence.
- Settings files use `async: true` instead of `background: true` —
  no behaviour change today (both currently work) but the doc-aligned
  spelling de-risks future versions.

## [0.2.0] — 2026-05-08

Iteration since v0.1.6. Substantial install-flow architectural
overhaul, secrets-architecture overhaul, freeze-investigation
hardening, drift-guard work, and maintenance. Headline themes:

- **Secrets architecture overhaul** (PR #171, 2026-05-08):
  keychain-canonical for everything written through the launcher
  (Onboarding `register_github_pat` and the new `/preferences`
  Manage Token panel both write to the OS keychain, not to disk).
  Hub `/projects/{id}/env` resolver path is the canonical reader;
  bundled wrappers consume it via the new
  `templates/scripts/vct_secrets_resolve.{sh,ps1}` helper. Items
  H1-H4 close the per-project / cross-launcher / shared-tab gaps;
  the in-tree `claude_mcp_servers/search_mcp/wrapper.sh` reads
  `$GITHUB_TOKEN` env first, hub resolver second. The legacy
  `~/.vct-secrets/git-credential-vct` helper is retired in favour
  of per-project `GITHUB_TOKEN` env propagation. The user-facing
  `vct` CLI under `tools/vct-secrets/` is unchanged. See
  [docs/MIGRATION-0.2.0.md](docs/MIGRATION-0.2.0.md) for the full
  matrix and migration recipe.
- **Hub authentication gate** (item H5, 2026-05-08): the launcher
  hub at `127.0.0.1:7700` now requires
  `Authorization: Bearer <token>` on every `/api/v1/*` route except
  `/api/v1/health`. The token is a fresh 32-byte CSPRNG value
  regenerated per launcher startup, persisted to
  `<vct_root_dir>/hub.token` (mode `0o600` on Unix). Closes the
  "any same-user process can curl localhost and exfiltrate the
  active secrets set" attack class. In-tree clients (resolver
  helpers, `vco` CLI, `hub_proxy`) read the token transparently.
- **Per-OS release archives** (`.github/workflows/release.yml`,
  2026-05-08): tag push (`v*.*.*`) builds and publishes
  `vibecoded-orchestrator-<version>-{linux-x64,macos-arm64,windows-x64}.{tar.gz,zip}`
  to GitHub Releases, with a `.sha256` sidecar each, alongside the
  existing standalone launcher binaries. SLSA build provenance
  attestation (Sigstore-signed) generated for the launcher binary
  inside each archive.
- **Non-destructive contract on user-owned files**: launcher
  unregister and `clear_github_pat` no longer rewrite or delete
  `.claude/env` outside the managed BEGIN/END block; user shell
  exports survive. The launcher never touches user-owned
  `~/.vct-secrets/projects/<project>/<key>` files written by the
  user-facing `vct` CLI.
- **Replace-existing guard on `register_github_pat`**: returns
  `EXISTS_DIFFERENT:<masked-prefix>` instead of silently
  overwriting a different PAT; GUI surfaces a confirm dialog.
  `force=true` proceeds with overwrite.
- **`/preferences` Manage Token UI**: rotate or clear the GitHub
  PAT outside the OnboardingWizard. Routes through the
  replace-existing guard above; "Clear" strips `GITHUB_TOKEN`
  from every registered project's env surface.
- **Install-flow overhaul** (PRs #139–#152, 2026-05-06): the
  user's "ONE VCO clone shared by all projects" model fully
  implemented. `ORCHESTRATOR_MANAGED_PATHS` as single source of
  truth (Rust + Python parse the same file). `ProjectEnvSettings`
  plumbs launcher state into per-project envs. Gitignore-aware
  `copy_recursive_sync`. Inspector + `update_orchestrator_at`
  gated by `validate_source_repo`. Codegraph venv resolution
  prefers `VCT_INSTALL_ROOT`.
- **New-user readiness** (PR #153, 2026-05-06/07): drift gate
  for `.claude/` ↔ `templates/` lockstep; GUI Option γ for non-FF
  auto-resync; `start-launcher.sh` build-hint fix.
- **Security + tightening** (PRs #154–#157, #163, 2026-05-07):
  tauri 2.11.0 → 2.11.1 CVE bump (origin confusion); cookie
  0.7.2 via npm overrides (Dependabot #1); BOM-strip in
  managed-paths parsers; `.vco-manifest.json` purge on unregister;
  codegraph mid-build unregister race silent.
- **Drift-guard fragility** (PRs #158, #162, #165, 2026-05-07):
  canonical env-key NAMES single source of truth (install ↔
  unregister structurally locked); CI cross-language
  managed-paths diff; full hook lockstep + sentinel rewire blocks
  for 7 PR-2-rewired scripts (drift gate's `EXPECTED_ASYMMETRIC`
  now empty).
- **UX polish + cleanliness** (PRs #159–#161, #164, 2026-05-07):
  `{{PROJECT_ROOT}}` placeholder in agent template subs;
  orchestrator-update banner clarification on per-project page;
  previously-registered leftovers banner in Add Project; gitignore
  hardening to prevent accidental RL Pro-tier IP leak.

Filed upstream:
- `anthropics/claude-code#56876` — `/tmp/claude-{uid}/.../tasks/`
  null-padded files (missing `ftruncate`).
- `tauri-apps/tauri#15353` — `generate_context!()` embeds
  `CARGO_MANIFEST_DIR` despite RUSTFLAGS remap.

Net change since v0.1.6: 23+ PRs to main; install flow
architecturally clean; all 4 declared Dependabot alerts triaged
or fixed.

## [0.1.1] – [0.1.5] — internal iterations

These versions existed only as internal pre-release iterations
during the 2026-04 → 2026-05 window. Substantive work happened
across many small commits without numbered releases; the
collected work landed as v0.1.6. Future released versions will
follow Keep a Changelog discipline more strictly per release.

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
- Setup docs use placeholders (`<YOUR_LS_WEBHOOK_SIGNING_SECRET>`,
  `<YOUR_SUPABASE_PROJECT_REF>`) with instructions to generate via
  `openssl rand -hex 32`.
- Added `scripts/check-no-secrets.sh` pre-commit grep guard with a
  blocklist of token patterns to keep them out of commits.

## [0.1.0] — 2026-04-26 — Initial private release

### Added
- VCT Launcher v1.0 + v1.1 (Tauri 2 desktop app): 17 GUI screens including project management, KG / code-graph dashboards, audit log, modules catalog, license activation, settings, onboarding wizard.
- Knowledge Graph with semantic search (Weaviate + qwen3-embedding 1024-dim, optional OpenAI vectors, GraphRAG-style WikiLink traversal).
- Code Graph (Python AST + regex-based analysis across 9 languages, 5 entity types: `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`; optional Joern integration for CFG / PDG).
- 20 automation hooks (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `PreCompact`, `PostCompact`, `StopFailure`, `SessionEnd`, …).
- 4 MCP servers: `weaviate-kg`, `ollama`, `search`, `code-embedding`.
- 19 agents + 28 skills as templates dropped into `.claude/` at install time.
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

[Unreleased]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/hotak92/vibecoded-orchestrator/compare/v0.1.5...v0.1.6
[0.1.0]: https://github.com/hotak92/vibecoded-orchestrator/releases/tag/v0.1.0
