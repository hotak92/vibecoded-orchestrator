# Install Architecture v2 — Design Document

**Status**: DESIGN (Track G of v0.2.53)
**Author**: Track G (planning agent, Opus)
**Date**: 2026-06-10
**Targets**: v0.2.53 implementation by Tracks A, B, C, D, E, F, G2, G3, H
**Supersedes**: ad-hoc shell + Python + Rust install code paths that have drifted across v0.2.46→v0.2.52

---

## Table of Contents

1. [Architectural goals + non-goals](#1-architectural-goals--non-goals)
2. [Current state map (audit synthesis)](#2-current-state-map-audit-synthesis)
3. [Target architecture: `install.py --bootstrap` mode](#3-target-architecture-installpy---bootstrap-mode)
4. [Thin OS shims](#4-thin-os-shims)
5. [Migration plan for each existing duplicate](#5-migration-plan-for-each-existing-duplicate-tier-1--tier-2-dedup)
6. [Per-track ownership table](#6-per-track-ownership-table)
7. [New `vco_lib/` modules](#7-new-vco_lib-modules)
8. [Rust shared middle layer](#8-rust-shared-middle-layer)
9. [Tri-OS CI smoke spec](#9-tri-os-ci-smoke-spec)
10. [Parity tests required](#10-parity-tests-required)
11. [Migration risks + rollback plan](#11-migration-risks--rollback-plan)
12. [Implementation sequencing for Phase 1](#12-implementation-sequencing-for-phase-1)
13. [v0.2.54+ deferrals](#13-v0254-deferrals)

---

## 1. Architectural goals + non-goals

### 1.1 Goals (the unification accomplishes)

1. **Eliminate cross-language drift bugs** at their source.
   The v0.2.51→v0.2.52 retrospective named drift between `install.sh`, `install.py`, `installer.rs`, and the `start-launcher.*` family as the dominant source of OS-specific bug reports. The "macOS `experimental_macOS/` vs `macos-arm64/`" drift (M-P0-2) is the canonical example — same fact (where does the binary live?) stated in 3 languages with no shared schema. v2 makes Python the **single source of truth** for OS-aware facts and forces shells/Rust to consume them via a versioned JSON envelope.

2. **Close the silent-hang class** (M-P0-4) by routing every long-running subprocess in install.py through `_run_logged_subprocess`. Single helper = single place to add dot-cycle animation, retry-with-backoff, stderr-tail logging, env-scrub, timeout policy. Today's 8+ silent-hang callsites become 8 calls to one helper.

3. **Close the JSONL drift class** (cross-OS audit Q2 Drift C) by canonising the schema in one shared fixture + parity tests, while keeping the 4 writers (bash, BAT, Python, Rust) in their respective languages where they must remain. Schema unification ≠ implementation unification — both are needed, but for different surfaces.

4. **Make tri-OS CI smoke load-bearing** (M-P0-9). Today the only release gate is Linux-side and non-blocking. v2 ships a tri-OS smoke gate that exercises the actual user entry point (`first-install.{sh,command,bat}`) on Ubuntu 22.04/24.04, macOS 14, Windows latest, Fedora 40. Pre-ship-check gate 22 = "tri-OS smoke green on main."

5. **Land the highest-ROI Tier-1 dedup extracts** in v0.2.53 itself so the v0.2.54 unification can proceed without first chasing accumulated drift.
   Specifically: 6 LOW-risk extracts identified by the audits, totalling ~600 LoC saved across `install.py` + ~50 LoC across Rust. These extracts close bug classes by construction (e.g. `_run_logged_subprocess` closes M-P0-4 + M-P1-7 simultaneously).

6. **Preserve project-bundle install architecture untouched**. The per-project bundle path (`vco_lib/project_init.py::install_project_bundle`) is already single-source and well-architected per audit #12. v2 explicitly **does not** restructure it — it only adds B1 (sentinel for Cmd-C resume), B2 (verify FS-disable contract end-to-end), B3 (symlink-blocking parity with orchestrator-self).

### 1.2 Non-goals (what v2 does NOT change)

1. **Project bundle install flow stays as-is.** Audit #12 verdict: "cleanest part of the install family." v2 does NOT consolidate `_materialize_orchestrator_self_claude_dir` into `install_project_bundle` (deferred to v0.2.54 as separate work). v2 also does NOT touch `_enumerate_bundle_files`, `_file_action`, or the manifest hash-compare rule.

2. **`install.sh` / `install.ps1` Python detection blocks stay autonomous.** Chicken-and-egg: can't shell out to Python to detect Python. These remain in shell, ≤30 LoC each, with parity-test coverage to prevent drift.

3. **`start-launcher.{sh,command,bat}` stays autonomous.** These MUST work when `.venv/` is corrupt or Python is uninstalled (that's their job). v2 unifies their launcher-binary candidate lists via a CI-emitted shared fixture (`launcher/dist/CANDIDATE_PATHS.json`), but they do not route through `install.py`.

4. **JSONL log writers stay multi-language.** The bash hook and `first-install.bat` writer must work BEFORE install.py exists or is reachable. v2 unifies the schema via a shared fixture + parity tests (see §10).

5. **Codesigning, `.app` bundle, `vco://` URL scheme, Intel Mac support, cross-encoder citation detection** — explicitly deferred to v0.2.54+ (see §13).

6. **`installer_engine.rs` / `_install_project_bundle` Python** — confirmed by audits to NOT exist; v2 does not chase these ghosts.

7. **Update flow choreography** (`update_orchestrator` ↔ `merge_orchestrator_with_upstream` ↔ `rebase_orchestrator_onto_upstream` — Rust audit Finding K, 3 update flows sharing 70% of body) — deferred to v0.2.54. Higher risk because it touches the merge/rebase split logic. v2 lands the **paired** sentinel+deferral writer (Finding E), which fixes the most acute bug class.

### 1.3 Success criteria

A v0.2.53 release that ships v2 is successful when:
- The macOS fresh-clone failure scenario (the report that triggered the 13-audit dispatch) can `git clone && ./first-install.command` and reach a working launcher without hand-edits.
- The `VibeCodedOrchestrator_Development` WRITE case-mismatch (FAB-2) no longer fires.
- Tri-OS CI smoke is green on every PR + push to main.
- Every Tier-1 extract has a parity/regression test demonstrating no behavioural change.
- Phase 3 audit AUDIT-5 confirms `_run_logged_subprocess` extracted-vs-inline output is byte-identical for the happy path.

---

## 2. Current state map (audit synthesis)

This section answers: **why do we need v2?** Cross-references throughout point at `.claude/context/audits/<file>-2026-06-10.md` so implementing tracks can pull details.

### 2.1 File inventory + duplication shape

#### 2.1.1 Shell entry points (`first-install.*`, `start-launcher.*`)

Per **`shell-scripts-dedup-2026-06-10.md`** + **`install-family-crossfile-dedup-2026-06-10.md`**:

| File | LoC | Role | Duplicated where |
|---|---|---|---|
| `first-install.sh` | 130 | Linux first-run wrapper | 90% identical to `first-install.command` (Drift G) |
| `first-install.command` | 119 | macOS first-run wrapper | Near-twin of `first-install.sh` |
| `first-install.bat` | **568** | Windows first-run wrapper | **Inline reimplementation of `post-install-launcher.sh` (1295 LoC)** — duplicates launcher binary probe, freshness check, 3-way menu, Node winget ladder, build+spawn (Finding 1, ~500 LoC of cross-language dup) |
| `start-launcher.sh` | ~70 | Linux launcher spawn | Drift A (binary candidates), Drift 4 (substring) |
| `start-launcher.command` | ~85 | macOS launcher spawn | **DRIFT A: `experimental_macOS/` (LIVE BUG)**, Drift 4 (substring) |
| `start-launcher.bat` | 87 | Windows launcher spawn | Drift A, Drift 4 |
| `install.sh` | 575 | Linux/macOS install orchestrator | 3-way duplicate Python/Node/Podman detect+install (~180 LoC) |
| `install.ps1` | ~600 | Windows install orchestrator | Mirrors install.sh; no PowerShell sibling for `post-install-launcher.sh` |
| `post-install-launcher.sh` | **1295** | post-install verification + launcher spawn | Mirror only 30% covered by `post-install-launcher.ps1` (227 LoC) |
| `post-install-launcher.ps1` | 227 | Windows post-install (DESKTOP SHORTCUT ONLY) | Should mirror `.sh` but doesn't |

**Detection duplication** (cross-language):
- "Is Podman installed?" → **5 separate implementations** (Q1 audit). `install.sh:344`, `install.ps1:387`, `install.py:7018`, `post-install-launcher.sh` (transitive), `installer.rs:9425` + `:2126`.
- "Python candidate list" → **3 implementations diverge**:
  - `install.sh:48` lists `python3.13 python3.12 python3.11 python3 python` ✓
  - `install.ps1:233` lists `python3.13 python3.12 python3.11 python3 python` ✓
  - `installer.rs:9596` lists `python3.12 python3.11 python3 python` ✗ (missing `python3.13` — **NEW-3**)
- "Where does the macOS launcher binary live?" → **4 implementations**:
  - `install.py:16956-16974` says `macos-arm64/` (canonical) ✓
  - `installer.rs:1115` says `"macos-arm64"` ✓
  - `post-install-launcher.sh:397-402` says `experimental_macOS/` ✗ **LIVE DRIFT**
  - `start-launcher.command:28` says `experimental_macOS/` ✗ **LIVE DRIFT**

**JSONL writers** (cross-language, schema drift):
- `install.py:458` `_log_install_event`: actor=`install.py`, escapes via `json.dumps(ensure_ascii=True)` (everything escaped).
- `post-install-launcher.sh:76` `_log_event`: actor=`post-install-launcher.sh`, escapes only `\` and `"` (control chars unescaped → invalid JSON for `detail` with tab/newline).
- `first-install.bat:127` `:_log_event`: actor=`first-install.bat`, escapes only `\` and `"`, **NO `data` field at all**.
- `installer.rs:9274` `append_install_log_event`: actor=`launcher`, uses serde (correct full escaping).
- **Net effect**: 4 different escape policies, 4 different actor strings, 1 writer (BAT) silently drops the structured `data` field. Claude Code's `docs/INSTALL_RECOVERY.md` reader gets less context on Windows installs.

#### 2.1.2 install.py (~23,356 LoC)

Per **`install-py-dedup-2026-06-10.md`**:

- **71 `subprocess.run` callsites**, ~40 follow the same `capture_output=True, text=True, timeout=N` + `if returncode != 0: print(FAIL); for line in stderr.splitlines()[-N:]: print(line); sys.exit(1)` shape.
- **328 `_log_install_event` callsites** (helper itself well-factored at line 458).
- **51 `DeferralEntry` constructions** with shared `condition_id` / `detected` / `why_deferred` / `command_to_apply` / `severity` / `kg_node_refs=[]` shape — variant phrasing.
- **9 `try: json.loads(p.read_text("utf-8"))` + `except (OSError, JSONDecodeError): return None|[]|{}` blocks**.
- **6+ atomic-write recipes** (`<x>.tmp + os.replace`) — `vco_lib/env_template.py::_atomic_write_text` already exists; install.py just doesn't import it.
- **2 near-twin GitHub-release downloaders** (launcher + vct-hub) — 95% identical bodies.
- **2 near-twin cargo-build helpers** (launcher Tauri + vct-hub) — 85% identical.

Total ~1,400–1,800 LoC of mechanical dedup available; ~600 LoC from Tier-1 LOW-risk extracts alone.

#### 2.1.3 Rust install layer (~32,856 LoC across `launcher/src-tauri/src/commands/`)

Per **`rust-installer-dedup-2026-06-10.md`**:

- **101 raw `Command::new`** callsites with no shared "spawn + capture + stderr-tail" helper (63 in `installer.rs`, 25 in `projects_v2.rs`, 9 in `git_user_editable_merge.rs`).
- **5 near-identical Python subprocess wrappers in `projects_v2.rs`** (~150 LoC each, 80% scaffolding duplicate): `run_bootstrap_collections`, `run_install_bundle`, `run_install_bundle_update_with_root`, `run_migrate_dry_run`, `drop_owned_collections`.
- **3 separate `update_orchestrator*` flows** sharing 70% of body: `update_orchestrator` (L4017, 800 LoC), `merge_orchestrator_with_upstream` (L5654, 194 LoC), `rebase_orchestrator_onto_upstream` (L5849, 141 LoC). Each writes paired sentinel + deferral on conflict — 3 sites, all 4 lines identical mod op string.
- **3 different `which_on_path` definitions** with disagreeing signatures (`Option<PathBuf>` vs `bool`).
- **5 install-root walkers, 4 predicates with partial disagreement**: `walk_for_orchestrator_root`, `looks_like_orchestrator_root`, `find_local_repo_root`, `is_completed_install_root`, `find_install_root_from_exe`, `walk_up_for_git`, `find_launcher_repo_root`.
- **15 callsites** with the Python-env block (`PYTHONIOENCODING` + `PYTHONUTF8` + `VCT_LAUNCHER_PID` + `creation_flags`). One missing → silent Windows UTF-8 break.
- **2 pre-pull rename helpers** (launcher + hub binaries) — 95% identical Windows bodies.
- **3 `chrono_iso_z*` definitions** + **2 `path_with_new_suffix` helpers** in different files.

Total ~2,200–2,800 LoC of mechanical dedup; the audit recommends ~50 LoC Tier-1 in v0.2.53 (paired sentinel+deferral writer = Finding E) and defers the rest to v0.2.54.

#### 2.1.4 vco_lib (Python)

Per **`vco-lib-python-dedup-2026-06-10.md`**:

- **FOUR Python implementations of `canonical_class_prefix`** (project_naming.py, project_init.py, codegraph_to_mermaid.py, config_projection.py) — three different fallback strings (`"vct"` / `"Vct"` / raises). Already burned the project at v0.2.15 bug 0.7. **NEW-10**.
- **install.py at 5 callsites still uses inline `sqlite3.connect(timeout=5.0)` to launcher.db** despite `vco_lib/launcher_db_reader.py::_open_db_readonly` existing (which uses `mode=ro&immutable=1` for non-blocking access). **CORRECT-2**.
- **install.py inline `.claude.json` atomic-write** (line 18866-18928) does NOT delete `.tmp` on failure; `vco_lib._atomic_write_text` handles this correctly. **CORRECT-1**.
- **Settings.json template merge** duplicated wholesale: `install.py::_merge_settings_template` + `_smart_merge_settings` + `_merge_hooks_block` ≡ `vco_lib/project_init.py::_merge_settings_template_for_bundle` + siblings. Comment acknowledges duplication; symmetric extraction never happened.
- **sha256 helpers** in 4 places (project_init.py × 2, diagram_indexer.py, install.py × 2).

#### 2.1.5 Project bundle install (audit #12 — DELIBERATELY UNTOUCHED in v2)

Per **`project-bundle-install-audit-2026-06-10.md`**:

- `vco_lib/project_init.py::install_project_bundle` is the SINGLE SOURCE OF TRUTH for the per-project flow. Used by both the launcher's `run_install_bundle` and the standalone CLI.
- Rust side (`projects_v2.rs::run_install_bundle*`) is a thin subprocess wrapper. Does NOT duplicate manifest writing, preservation logic, or hash-diff.
- 3 bugs identified for v0.2.53 fix:
  - **B1 (HIGH)**: No Bug-A-equivalent sentinel for project bundle updates → mid-update Cmd-C corrupts manifest (track F, NEW-7).
  - **B2 (MEDIUM)**: FS-disable contract (`.claude/{agents,skills}.disabled/`) requires file move that audit couldn't locate. If GUI doesn't move the file, bundle updates silently re-enable user-disabled agents (track F, NEW-9).
  - **B3 (MEDIUM)**: Per-project `_write_file_atomic` lacks symlink-blocking (orchestrator-self has it via V47-B) — silent data destruction risk (track F, NEW-8).

#### 2.1.6 CI surface

- `installer-smoke.yml` Job 1 (Linux): non-blocking `|| { echo "::warning..."; }`; exercises NONE of `first-install.sh` / `post-install-launcher.sh` / launcher boot. **L-P0-6**.
- `installer-smoke.yml` Job 4 (Windows): uses `shell: bash`, so `first-install.bat` is NEVER exercised in CI. cmd.exe-specific bugs cannot be caught. **W-P1-3**.
- No macOS install smoke at all. **M-P0-9**.

### 2.2 The cross-OS drift triage (which patterns are TRULY cross-OS)

Per **`cross-os-triage-2026-06-10.md`**:

| Pattern | Truly cross-OS? | Why |
|---|---|---|
| `_run_logged_subprocess` silent-hang | YES — ALL-OS | Python stdlib semantic, identical buffering everywhere |
| `.dmg`/`.appimage` filter (releases ship `.zip`) | YES — MACOS-AND-LINUX | Both filters wrong; Windows downloader is BAT, not this Python block |
| `local` outside function | YES — MACOS-AND-LINUX | Reproduced on bash 5.2 Linux with `set -u` |
| Launcher PATH inheritance | YES — MACOS-AND-LINUX | systemd `.desktop` apps inherit minimal PATH on Linux |
| InstallHealthGate refresh | YES — ALL-OS | Pure Svelte, no platform branching |
| Python max-version policy | YES — ALL-OS | Single MAX_PYTHON is right; wheel coverage varies but policy is one |
| Podman machine auto-init | MACOS-AND-WINDOWS | Linux uses systemd; macOS + Windows both need `podman machine init` |
| bash 3.2 empty-array (`install.sh:575`) | MACOS-ONLY-IN-PRACTICE | Linux ships bash 4+/5+; only macOS still has bash 3.2 in `/bin/bash` |
| `experimental_macOS/` path drift | MACOS-ONLY | Linux `linux-x64/` and Windows `windows-x64/` consumers are correctly aligned |
| `/opt/homebrew/bin/` Homebrew path probe | MACOS-ONLY | Linuxbrew at `/home/linuxbrew/.linuxbrew/bin` is a separate concern (L-P0-1) |

**Implication for v2**: target architecture must handle the truly cross-OS bugs uniformly (via `install.py --bootstrap`) AND respect OS-specific shim boundaries (each first-install wrapper stays in its own language for Python-detection chicken-and-egg).

---

## 3. Target architecture: `install.py --bootstrap` mode

### 3.1 High-level shape

```
┌──────────────────────────────────────────────────────────────────┐
│ first-install.{sh,command,bat}   ~30-60 LoC each (Python detect) │
│                                                                  │
│   1. Locate Python (OS-aware candidates)                         │
│   2. If absent: pkgmgr install prompt (only OS-specific bit)     │
│   3. cd "$SCRIPT_DIR"                                            │
│   4. exec python3 install.py --bootstrap "$@"                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ install.py --bootstrap [--json] [--install-missing]              │
│                                                                  │
│   READ-ONLY by default:                                          │
│     - Detect system (OS, arch, RAM, GPU, Python, Node, Podman…)  │
│     - Resolve paths (install root, launcher binary, dist subdir) │
│     - Probe Weaviate endpoints                                   │
│     - Compute package-manager advice                             │
│     - Build missing-prereqs list                                 │
│     - Emit JSON envelope to stdout (--json) OR human-readable    │
│       table (default for first-install fallback)                 │
│                                                                  │
│   With --install-missing:                                        │
│     - Run the package-manager install flows                      │
│       (apt/dnf/brew/winget) for missing prereqs                  │
│     - Re-detect after install                                    │
│                                                                  │
│   With NO flags (legacy mode, install.py default):               │
│     - Existing 10-step install runs as today                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Rust + bash consumers (post-install-launcher.sh, installer.rs)   │
│                                                                  │
│   Subprocess-shell to `install.py --bootstrap --json`            │
│   Parse JSON envelope                                            │
│   Use canonical values                                           │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 Argv contract

```
install.py [--bootstrap] [--json] [--install-missing] [--no-prompt] [--verbose]
```

**Backwards compatibility**:
- `install.py` (no flags) → existing 10-step install runs unchanged (v0.2.52 behavior).
- `install.py --update` → existing update flow runs unchanged.
- `install.py --bootstrap` is **additive**. Default output: human-readable status table. With `--json`: machine-readable envelope on stdout.
- `--bootstrap` is mutually exclusive with `--update` (mutually-exclusive arg group). If both passed, exit 2 with "bootstrap cannot be combined with update".

**Side-effect policy**:
- `install.py --bootstrap` (no `--install-missing`) is **READ-ONLY**. It must NOT write any files except the JSONL log (which itself is append-only and per-design). Specifically: does NOT create `state/`, does NOT write `.env`, does NOT spawn `podman machine init`, does NOT pull Ollama models, does NOT mutate `~/.claude.json`.
- `install.py --bootstrap --install-missing` runs `apt-get install <pkg>` / `brew install <pkg>` / `winget install <pkg>` flows for missing prereqs. ALSO runs `podman machine init` on macOS/Windows if Podman is installed but the daemon is not reachable. Re-runs detection after install.
- `install.py --bootstrap --json --install-missing` chains the two: install missing, then emit the post-install detection envelope.

**Exit codes**:
- `0` — Detection succeeded. `ready_to_install` in the envelope is what callers MUST check; `0` does NOT mean "all prereqs present", only "detection completed without crashing".
- `1` — Detection threw an exception (unexpected failure). stderr contains traceback.
- `2` — Bad invocation (`--bootstrap --update` combined, etc.).
- `3` — `--install-missing` was requested but a prereq install command failed. stderr names the failing prereq.

### 3.3 JSON envelope schema

**Schema version**: `1` (track on the envelope, allow Rust + bash consumers to refuse versions they don't know).

```json
{
  "schema_version": 1,
  "vco_version": "v0.2.53",
  "vco_short_sha": "9396ea96",
  "generated_at": "2026-06-10T12:34:56Z",

  "system": {
    "os": "macos | linux | windows",
    "os_family": "darwin | linux | mingw64 | msvc",
    "kernel_release": "23.4.0",
    "arch": "arm64 | x86_64 | aarch64",
    "ram_gb": 16,
    "cpu_count": 10,

    "python": {
      "cmd": "/opt/homebrew/bin/python3.13",
      "version": "3.13.2",
      "version_tuple": [3, 13, 2],
      "wheel_support_ok": true,
      "ok": true
    },
    "node": {
      "cmd": "/opt/homebrew/bin/node",
      "version": "20.11.0",
      "version_tuple": [20, 11, 0],
      "min_required": [18, 0, 0],
      "ok": true
    },
    "npm": {
      "cmd": "/opt/homebrew/bin/npm",
      "version": "10.2.4",
      "ok": true
    },
    "pnpm": {
      "cmd": null,
      "version": null,
      "ok": false
    },
    "podman": {
      "cmd": "/opt/homebrew/bin/podman",
      "version": "5.1.0",
      "machine_running": true,
      "ok": true
    },
    "docker": {
      "cmd": null,
      "version": null,
      "ok": false
    },
    "container_runtime_chosen": "podman",
    "git": {
      "cmd": "/usr/bin/git",
      "version": "2.45.0",
      "ok": true
    },
    "brew": {
      "cmd": "/opt/homebrew/bin/brew",
      "version": "4.2.18",
      "prefix": "/opt/homebrew",
      "ok": true
    },
    "lean_ctx": {
      "cmd": null,
      "version": null,
      "ok": false
    },
    "claude_cli": {
      "cmd": "/usr/local/bin/claude",
      "version": "2.1.143",
      "ok": true
    },

    "gpu": {
      "vendor": "metal | nvidia | amd | none",
      "model": "Apple M3",
      "vram_gb": 0,
      "driver_version": null,
      "container_toolkit_ok": null
    },

    "linux_distro": {
      "id": "ubuntu",
      "version_id": "24.04",
      "codename": "noble",
      "pkg_mgr": "apt"
    } | null,

    "windows_features": {
      "powershell_version": [7, 4, 1],
      "winget_present": true,
      "chocolatey_present": false,
      "wsl2_present": false
    } | null,

    "macos_features": {
      "homebrew_prefix": "/opt/homebrew",
      "is_apple_silicon": true,
      "rosetta_present": false
    } | null
  },

  "paths": {
    "install_root": "/Users/<user>/vibecoded-orchestrator",
    "install_root_kind": "orchestrator_clone | completed_install | git_repo",
    "venv_python": "/Users/<user>/vibecoded-orchestrator/.venv/bin/python3",
    "mcp_venv_python": "/Users/<user>/vibecoded-orchestrator/claude_mcp_servers/.venv/bin/python3",
    "launcher_dist_subdir": "macos-arm64 | linux-x64 | windows-x64",
    "launcher_binary": "/Users/<user>/vibecoded-orchestrator/launcher/dist/macos-arm64/vct-launcher",
    "launcher_binary_exists": true,
    "vct_hub_binary": "/Users/<user>/vibecoded-orchestrator/launcher/dist/macos-arm64/vct-hub",
    "vct_hub_binary_exists": true,
    "state_dir": "/Users/<user>/vibecoded-orchestrator/state",
    "state_dir_exists": false,
    "claude_dir": "/Users/<user>/vibecoded-orchestrator/.claude",
    "vct_root_dir": "/Users/<user>/.vct",
    "launcher_db": "/Users/<user>/.vct/launcher.db",
    "hub_port_file": "/Users/<user>/.vct/hub.port",
    "hub_token_file": "/Users/<user>/.vct/hub.token"
  },

  "package_manager_advice": {
    "primary": "brew | apt | dnf | pacman | zypper | apk | winget",
    "install_python": ["brew install python@3.13"],
    "install_node": ["brew install node"],
    "install_podman": ["brew install podman", "podman machine init", "podman machine start"],
    "install_lean_ctx": ["brew install lean-ctx"],
    "tauri_deps": [],
    "selinux_volume_flag_needed": false,
    "nvidia_container_toolkit_install": null,
    "render_group_remediation": null
  },

  "weaviate_endpoints": {
    "base": "http://localhost:8081",
    "health": "http://localhost:8081/v1/.well-known/ready",
    "meta": "http://localhost:8081/v1/meta",
    "schema": "http://localhost:8081/v1/schema",
    "graphql": "http://localhost:8081/v1/graphql",
    "grpc_host": "localhost:50052"
  },

  "ollama_endpoints": {
    "base": "http://localhost:11435",
    "tags": "http://localhost:11435/api/tags",
    "pull": "http://localhost:11435/api/pull"
  },

  "code_embed_endpoints": {
    "base": "http://localhost:11440",
    "health": "http://localhost:11440/health"
  },

  "vct_hub_endpoints": {
    "base": "http://127.0.0.1:7700",
    "health": "http://127.0.0.1:7700/api/v1/health"
  },

  "missing_prereqs": [
    {
      "name": "podman",
      "human": "Podman or Docker",
      "severity": "blocking",
      "install_hint": "brew install podman && podman machine init && podman machine start"
    }
  ],

  "ready_to_install": false,
  "blocker_messages": [
    "Podman or Docker is required but neither was found on PATH."
  ],
  "warnings": []
}
```

### 3.4 Field-by-field contracts (key items)

**`system.python.wheel_support_ok`**: implements OS-4 from the master plan. Calls `pip install --dry-run --only-binary=:all: <key-deps>` against the canonical Python install. `true` iff the dry-run succeeds for the test set; `false` if pip would need to build from source for any dep. This replaces a hard `MAX_PYTHON` constant. If `wheel_support_ok=false`, the envelope adds a warning suggesting the user downgrade Python.

**`system.podman.machine_running`**: macOS + Windows only. Probes `podman machine list --format json` and looks for `"Running": true`. On Linux, this field is `null` (irrelevant; uses systemd socket). M-P1-2: when this field is `false`, `package_manager_advice.install_podman` includes `podman machine init` + `podman machine start` (only for macOS + Windows).

**`paths.install_root_kind`**: implements Rust audit Finding C. Three values:
- `orchestrator_clone` — repo has `install.py` + `CLAUDE.md` + `.git/` but no `state/`. This is what a fresh `git clone` looks like.
- `completed_install` — has `install.py` + `CLAUDE.md` AND (`state/` OR `.env` with `KG_COLLECTION`). This is what a successful first-install produces.
- `git_repo` — has `.git/` but no install markers. Not a VCO root.

**`paths.launcher_dist_subdir`**: single source of truth for the per-OS subdir name. **macOS = `macos-arm64`** (NOT `experimental_macOS`). Implements M-P0-2.

**`package_manager_advice.primary`**: implements L-P0-1 (zypper + apk parity). The detection picks the first available among `brew > winget > apt > dnf > pacman > zypper > apk`. If none found, `null` + adds blocker.

**`package_manager_advice.tauri_deps`**: Linux-only. Implements L-P0-2. List of distro-specific tauri build deps. For Ubuntu < 24.04 / Debian 12 stable: includes both `libwebkit2gtk-4.1-dev` AND `libwebkit2gtk-4.0-dev` fallback advice. For Ubuntu 24.04+: only `4.1-dev`. Empty list on non-Linux.

**`package_manager_advice.selinux_volume_flag_needed`**: Linux-only. Implements L-P0-3. `true` when `getenforce` returns `Enforcing` AND distro is Fedora/RHEL/CentOS Stream. When `true`, downstream code must add `:Z` to bind-mount volume args.

**`package_manager_advice.nvidia_container_toolkit_install`**: Linux-only. Implements L-P0-7. `null` if user has no NVIDIA GPU OR toolkit is installed. Otherwise a string with the pkgmgr-aware install command.

**`weaviate_endpoints.health`**: implements NEW-4. **Canonical value: `/v1/.well-known/ready`** (Weaviate's documented readiness probe). The `installer.rs:627` comment claiming `/v1/meta` is "the right liveness probe" is wrong and must be removed.

**`missing_prereqs`**: ordered list. Each entry has `name`, `human`, `severity` (`blocking` | `warning` | `optional`), and `install_hint`. `ready_to_install = (no blocking entries)`.

### 3.5 Side-effect policy details

**`install.py --bootstrap`** (no `--install-missing`):
- Reads files: yes (PATH, env, file probes for binary detection).
- Spawns subprocesses: yes, but only short read-only probes (`python3 --version`, `podman --version`, `nvidia-smi --query-gpu`, `getenforce`, `brew --prefix`, `podman machine list`). Every probe has timeout ≤ 10s.
- Writes files: ONE write only — appends to `state/logs/install.jsonl` IF the `state/logs/` directory already exists. If `state/` doesn't exist, the bootstrap mode does NOT create it. This is the rule: bootstrap must be safe to call BEFORE install has ever run.
- Network: no network calls. Local TCP probes (Weaviate `/v1/.well-known/ready` etc.) are timeout-bounded and best-effort; failures are reported in the envelope, never blocking.
- User prompts: never.

**`install.py --bootstrap --install-missing`**:
- Adds: subprocess invocations of `apt-get install -y <pkg>` / `brew install <pkg>` / `winget install --id <id>` via the package-manager advice list.
- Adds: `podman machine init` + `podman machine start` on macOS/Windows if Podman binary present but daemon not running.
- Adds: pre-install user prompts IF stdin is a TTY AND `--no-prompt` not given (one prompt: "Install missing prereqs [N] ? [Y/n]"). With `--no-prompt`, defaults to YES.
- Adds: re-detection pass after each install, so the final envelope reflects post-install state.

### 3.6 Why this shape

**Why JSON envelope vs human prose**: Rust + bash consumers need machine-readable data. The audit found 4 different escape policies across the 4 current JSONL writers (cross-file dedup §2.2 Drift C); a versioned envelope with a published schema is the only way to make multi-language consumers safe.

**Why `--bootstrap` is additive (not a replacement)**: legacy `install.py` (no flags) is 23K LoC of carefully-tested step-dispatch. Replacing it is a v0.2.55+ project. v0.2.53 needs the bootstrap envelope NOW to fix M-P0-2 (path drift) + NEW-3 (Python candidate drift) + NEW-4 (Weaviate endpoint drift) by giving Rust/bash a canonical place to read from.

**Why `--install-missing` is separate from default `--bootstrap`**: read-only safety. Today's shell scripts call `install.py --bootstrap --json` from inside `first-install.{sh,command,bat}` BEFORE the user has consented to anything. Read-only mode means we can probe everything without mutating the system. Once `ready_to_install: false`, the shim asks the user "install missing? [Y/n]" and re-invokes with `--install-missing`.

---

## 4. Thin OS shims

### 4.1 `first-install.command` (macOS, target ~30 LoC)

**Responsibilities**:
1. Resolve `SCRIPT_DIR` (Finder cwd compensation — M-P1-4).
2. Locate Python via candidate cascade:
   - `/opt/homebrew/opt/python@3.13/bin/python3.13` (Apple Silicon Homebrew, latest)
   - `/opt/homebrew/bin/python3.13`
   - `/opt/homebrew/bin/python3.12`
   - `/opt/homebrew/bin/python3.11`
   - `python3.13` (PATH)
   - `python3.12` (PATH)
   - `python3.11` (PATH)
   - `python3` (PATH)
   - `python` (PATH, last resort)
3. If no Python found:
   - Print "Python 3.11+ required. Install via: `brew install python@3.13`"
   - If user has Homebrew (`/opt/homebrew/bin/brew` exists) AND stdin is a TTY: prompt `[Y/n]` to run `brew install python@3.13`.
   - On consent: run the install, then re-probe.
   - On decline or no Homebrew: exit 1 with manual install hint.
4. `cd "$SCRIPT_DIR"` (mandatory — Finder cwd is `$HOME`).
5. `exec "$PYTHON" install.py --bootstrap "$@"` — Python takes over.

**Constraints**:
- Bash 3.2 compatible (M-P0-1 lesson: no `local` outside function, no `${array[@]:-}` shenanigans, no associative arrays).
- `set -euo pipefail`.
- No `local` outside function (M-P0-5).
- NO probing of `/usr/local/bin/python*` (OS-1: Apple Silicon only).
- Forwards `"$@"` exactly so Python sees `--json`, `--install-missing`, etc.

**What it does NOT do**:
- Does NOT detect Node, Podman, container runtime, GPU. Python does that via `install.py --bootstrap`.
- Does NOT write JSONL logs. Python does that.
- Does NOT mutate `~/.claude.json`, `.env`, `state/`. Python does that.

**Example (illustrative — Track A writes the actual code)**:
```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_CANDIDATES=(
  "/opt/homebrew/opt/python@3.13/bin/python3.13"
  "/opt/homebrew/bin/python3.13"
  "/opt/homebrew/bin/python3.12"
  "/opt/homebrew/bin/python3.11"
  "python3.13" "python3.12" "python3.11" "python3" "python"
)
PYTHON=""
for cand in "${PYTHON_CANDIDATES[@]}"; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then
  echo "Python 3.11+ required."
  # ... brew install prompt block ...
  exit 1
fi
cd "$SCRIPT_DIR"
exec "$PYTHON" install.py --bootstrap "$@"
```

### 4.2 `first-install.sh` (Linux, target ~30 LoC)

Same shape as macOS, but:
- Python candidates: `python3.13 python3.12 python3.11 python3 python` (NO Homebrew paths; Linuxbrew gets `/home/linuxbrew/.linuxbrew/bin/...` as a separate cascade after PATH).
- Install prompt uses the `pkg_install_via_pkgmgr` helper (shell-scripts-dedup Finding 3) — apt / dnf / pacman / zypper / apk.
- Bash 4+ available, but for cross-distro safety stick to bash 3.2 idioms anyway.

### 4.3 `first-install.bat` (Windows, target ~60 LoC)

The biggest reduction in the audit: from 568 LoC to ~60. Today's `first-install.bat` is an inline reimplementation of `post-install-launcher.sh` (shell-scripts-dedup Finding 1). v2's plan: shrink to a thin Python-detect + forward.

**Responsibilities**:
1. Resolve `%~dp0` for SCRIPT_DIR.
2. Locate Python via candidate cascade:
   - `py -3.13` (Python Launcher for Windows)
   - `py -3.12`
   - `py -3.11`
   - `py -3`
   - `python.exe` (PATH)
   - `python3.exe` (PATH)
3. If no Python found:
   - Print install instructions.
   - If `winget` present AND TTY: prompt `[Y/N]` for `winget install --id Python.Python.3.13 --source winget --silent`.
   - On consent: install, then re-probe with `py -3.13`.
   - On decline: exit 1.
4. `cd /d "%~dp0"`.
5. `"%PYTHON%" install.py --bootstrap %*` (cmd.exe argv forwarding via `%*`).

**Constraints**:
- cmd.exe batch syntax (NOT PowerShell). Reason: pwsh.exe is not preinstalled on stock Windows; Powershell 5.1 has parse issues with modern scripts (W-P1-4). The 60 LoC stays in pure cmd.exe.
- JSONL log writer schema (W-P1-1): properly escapes apostrophes via PowerShell `-replace`. Schema MUST include `data` field even if writing `null` — for parity with bash + Python writers.
- `refreshenv` fallback (W-P1-2): NEVER call `refreshenv` directly (Chocolatey shim, not preinstalled). Instead, after `winget install` succeeds, re-read PATH from registry via PowerShell: `[Environment]::GetEnvironmentVariable("PATH", "User")`.
- `start-launcher.bat` (separate file) keeps a similar 60-LoC shape but stays multi-language for the autonomous "Python broken" case.

**What it does NOT do**:
- Does NOT inline-reimplement `post-install-launcher.sh`. That work goes into a separate `post-install-launcher.ps1` rewrite (deferred to v0.2.54 — see §13).
- For v0.2.53, the existing `first-install.bat` keeps SOME of its 568 LoC; the Python-detect block at top (~60 LoC) is what changes. The rest is patched for W-P1-1/W-P1-2/W-P1-5 fixes (Track H) but the bigger restructure is v0.2.54.

### 4.4 `start-launcher.{sh,command,bat}` (KEEP autonomous)

**These do NOT route through `install.py --bootstrap`**. They MUST work when Python is broken or `.venv/` is corrupt — that's their purpose. Today they have:
- Hardcoded launcher binary candidate lists (shell-scripts-dedup Finding 5 — diverged).
- Inline frontend-asset substring check (Finding 4 — diverged narrow vs broad).

**v2 fix**: a CI-emitted shared fixture.

The release CI (`commit-dist-binaries` job) writes `launcher/dist/<os-arch>/metadata.json` next to the binary. The metadata contains:

```json
{
  "schema_version": 1,
  "binary_name": "vct-launcher",
  "vco_version": "v0.2.53",
  "build_time_utc": "2026-06-10T11:22:33Z",
  "frontend_asset_marker": "_app/immutable/",
  "expected_marker_count_min": 5,
  "candidate_paths_per_os": {
    "macos": ["launcher/dist/macos-arm64/vct-launcher", "launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/vct-launcher"],
    "linux": ["launcher/dist/linux-x64/vct-launcher"],
    "windows": ["launcher/dist/windows-x64/vct-launcher.exe"]
  }
}
```

`start-launcher.*` reads its OWN `metadata.json` (`launcher/dist/<os-arch>/metadata.json`) when available, and falls back to a hardcoded list (M-P0-2 fix: `macos-arm64`, not `experimental_macOS`) when the metadata is missing (e.g. dev build from `cargo tauri build` without CI). The hardcoded fallback is shared via `scripts/lib/launcher-candidates.{sh,ps1}` per shell-scripts-dedup Finding 5.

This gives us:
- **Schema parity** at CI time (one writer, one reader contract).
- **Autonomous operation** at runtime (no Python dependency).
- **Single source of truth** for candidate paths + asset marker (closes M-P0-2 + NEW-1).

### 4.5 Forwarding `$@` / `%*` correctly

A common shim bug: failing to forward args (or double-forwarding them). The shims MUST pass ALL command-line args through to Python verbatim:

- macOS / Linux: `exec "$PYTHON" install.py --bootstrap "$@"` (the `"$@"` preserves quoting).
- Windows: `"%PYTHON%" install.py --bootstrap %*` (cmd.exe `%*` preserves the rest of the original command line).

Args the shim itself consumes (none in v2 — keep the shim arg-free) must be parsed BEFORE the forward. v2's shims have NO own flags; everything goes to Python.

---

## 5. Migration plan for each existing duplicate (Tier-1 + Tier-2 dedup)

For each item from the master plan §1C + §1D, this section names the new home, the owner track, the migration risk, and the test that proves no behavior change.

### 5.1 Tier-1 LOW-risk extracts (LAND in v0.2.53)

#### DEDUP-1: `_run_logged_subprocess` helper

**Source**: 8+ silent-hang callsites in `install.py` (lines 9203, 9242, 9280, 9328, 22551, 22981, 23005, 23088) + ~30 other `subprocess.run(capture_output=True)` callsites.
**New home**: `install.py` (top-level helper, near line 1000 next to existing subprocess helpers; intentionally NOT in `vco_lib/` because dot-cycle animation + `_log_install_event` integration are install.py-specific).
**Signature**:
```python
def _run_logged_subprocess(
    cmd: list[str],
    *,
    step: str,
    phase_label: str,
    timeout: int,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stderr_tail_lines: int = 15,
    on_failure: Literal["exit", "return", "deferral"] = "exit",
    show_dots_after_seconds: float | None = 3.0,
    elapsed_counter: bool = True,
) -> subprocess.CompletedProcess[str]: ...
```
**Owner track**: B.
**Risk**: MEDIUM (touches 40+ callsites; risk is subtle behavior change in stderr-tail formatting or timeout policy).
**Test**: `tests/test_run_logged_subprocess.py` — happy-path, timeout, non-zero exit, dot-cycle activation after 3s, env-scrub propagation, on_failure modes.
**Closes**: M-P0-4 (silent-hang at 8+ sites), M-P1-7 (dot-cycle animation), M-P1-3 (pip flags uniformity).

#### DEDUP-2: `_download_release_binary` helper

**Source**: `install.py::_try_download_launcher_binary` (17008) + `_try_download_vct_hub_binary` (18236) — 95% identical bodies.
**New home**: `install.py` (single helper replacing both).
**Signature**:
```python
def _download_release_binary(
    *, install_root: Path,
    binary_basename: str,           # "vct-launcher" | "vct-hub"
    bin_subdir_fname: tuple[str, str],
    tmpdir_prefix: str,
    timeout_s: int = 60,
) -> Optional[Path]: ...
```
**Owner track**: B.
**Risk**: LOW (both helpers go through `_run_logged_subprocess` once DEDUP-1 lands; the unification is mechanical after that).
**Test**: `tests/test_download_release_binary.py` — gh path, curl fallback path, both unavailable, ZIP-not-found-in-archive, chmod 0o755 on POSIX.
**Closes**: drift before a 3rd binary (`vct-updater`?) needs the same recipe.

#### DEDUP-3: `_make_deferral` builder

**Source**: 51 inline `DeferralEntry(...)` constructions in `install.py`.
**New home**: `install.py` (top-level builder).
**Signature**:
```python
def _make_deferral(
    condition_id: str, *,
    title: str,
    detected: str,
    why_deferred: str,
    command_to_apply: str,
    severity: Literal["info", "warning", "error"] = "warning",
    kg_node_refs: list[str] | None = None,
) -> DeferralEntry: ...
```
The helper applies `textwrap.dedent().strip()` to multi-line strings + ensures `kg_node_refs=[]` default.
**Owner track**: B.
**Risk**: LOW (mechanical; type signature catches the missing `kg_node_refs=[]` typo that was an empirical bug source).
**Test**: `tests/test_make_deferral.py` — round-trip through `DeferralReport.write()` + `DeferralReport.read()` for at least 3 sample condition_ids.
**Closes**: missing-kwarg typo class.

#### DEDUP-4: Migrate launcher.db sqlite3 → `vco_lib.launcher_db_reader._open_db_readonly`

**Source**: `install.py:8552-8572`, `:8210-8232`, `:8702-8780`, `:11654-11680` — inline `sqlite3.connect(timeout=5.0)` (blocking) calls to launcher.db.
**New home**: import + use the existing `vco_lib.launcher_db_reader._open_db_readonly` which uses `mode=ro&immutable=1` (non-blocking).
**Owner track**: B.
**Risk**: LOW-MEDIUM (read-only mode is strictly safer than blocking 5s on a writer lock; semantic parity check needed for `None` return).
**Test**: `tests/test_launcher_db_reader_parity.py` — assert vco_lib reader returns identical row for at least 5 known keys vs the install.py inline pattern (run both, compare).
**Closes**: CORRECT-2 (5s blocking hang during install).

#### DEDUP-5: Migrate `.claude.json` atomic-write → `vco_lib._atomic_write_text`

**Source**: `install.py:18866-18928` (`.claude.json` atomic-write that does NOT delete `.tmp` on failure).
**New home**: import + use `vco_lib.env_template._atomic_write_text` (and call the new `vco_lib.atomic._atomic_write_json` if it lands — see §7.1).
**Owner track**: B.
**Risk**: LOW (the vco_lib helper has correct cleanup; the install.py inline does not).
**Test**: `tests/test_atomic_write_cleanup.py` — simulate write failure mid-rename, assert `.tmp` file is deleted.
**Closes**: CORRECT-1 (`.tmp` cleanup on failure).

#### DEDUP-6: Resolve 4-way `canonical_class_prefix` SSOT

**Source**: 4 implementations:
- `vco_lib/project_naming.py:70-189::canonical_class_prefix` (DECLARED SSOT, raises on empty)
- `vco_lib/project_init.py:77-107::sanitize_for_weaviate_class` (DECLARED SSOT, fallback `"vct"` lowercase)
- `vco_lib/codegraph_to_mermaid.py:130-138::_sanitize_collection_prefix` (DIFFERENT rule — underscore-preserving for MCP server contract)
- `vco_lib/config_projection.py:1131-1151::_sanitize_kg_collection` (DECLARED mirror of `project_init.py`, fallback `"Vct"` capitalised)

**Migration**:
- `project_naming.py::canonical_class_prefix` stays the SSOT for the "drop spaces" rule.
- `project_init.py::sanitize_for_weaviate_class` becomes a thin wrapper: `try: return canonical_class_prefix(name); except ValueError: return "Vct"`.
- `config_projection.py::_sanitize_kg_collection` calls the wrapper above.
- `codegraph_to_mermaid.py::_sanitize_collection_prefix` is RENAMED to `_codegraph_mcp_sanitize_prefix` and gets a docstring noting "this is the UNDERSCORE-PRESERVING variant for the MCP server contract; NOT the project_naming canonical".

**Owner track**: F.
**Risk**: MEDIUM (two real Weaviate collection-naming behaviors must both be preserved; the v0.2.15 bug 0.7 is the cautionary tale).
**Test**: `tests/test_canonical_class_prefix_parity.py` — assert all 4 sites converge to the same result for a fixed input set INCLUDING the underscore-preserving variant's rename.
**Closes**: NEW-10.

### 5.2 Tier-2 MEDIUM-risk dedup (deferred to v0.2.54)

These are NOT done in v0.2.53. v2 names them for completeness because Track G's design doc is also the staging plan for v0.2.54.

#### DEDUP-7: `_detect_tool_with_version`

**Source**: install.py `_find_lean_ctx_binary` (7432), `_find_npx` (22812), `_find_npm` (22910), `_check_python_version` (5879), `_check_claude_cli` (22122), `_detect_container_runtime` (6925), `_query_launcher_version` (17207), `_try_cargo_*` (17129, 18356), brew/cargo/yay/paru detection (7588, 7727).
**New home (v0.2.54)**: `vco_lib/detect.py::resolve_binary(name, *, extra_search_dirs=None, version_argv=None, version_parser=None, timeout_s=15) -> tuple[str | None, str | None]`.
**Owner track (v0.2.54)**: TBD.
**Risk**: MEDIUM (10+ callsites with subtle differences — extra search dirs, version flag, parser).
**Test**: `tests/test_resolve_binary.py` for the new helper + per-call regression tests for each migrated callsite.

#### DEDUP-8: `_retry_with_backoff`

**Source**: install.py `_wait_for_ollama` (11107), `_wait_for_vct_hub_health` (18576), `_probe_dual_ollama_instances` (11244), Weaviate-reachable handler (5700).
**New home (v0.2.54)**: `vco_lib/health.py::wait_for_http_ready(url, *, deadline_seconds, poll_interval_seconds=1.0, timeout_per_probe=3.0, is_ready=None) -> bool`.
**Owner track (v0.2.54)**: TBD.

#### DEDUP-9: `_package_manager_install_prompt`

**Source**: install.py `_prompt_install_container_runtime` (7281), `_maybe_install_lean_ctx` (7562). (`_install_joern` was one of the three call-sites cited here at v0.2.54; removed in v0.2.73 CG-3 along with all Joern CFG/PDG code-graph extraction.)
**New home (v0.2.54)**: ??? — audit-marked HIGH risk; v2 recommendation: extract the *shape* (recipe enum + try-each loop) but keep recipes inline per caller.
**Owner track (v0.2.54)**: TBD.

#### DEDUP-10: Rust subprocess wrapper

**Source**: 101 raw `Command::new` callsites across `installer.rs`, `projects_v2.rs`, `git_user_editable_merge.rs`.
**New home (v0.2.54)**: `launcher/src-tauri/src/commands/subprocess.rs::run(SubprocessSpec) -> SubprocessOutput` per Rust audit Finding A.
**Owner track (v0.2.54)**: TBD.
**Risk**: LOW per audit, but large blast radius (101 callsites).

#### DEDUP-11: Rust 5-way `vco_lib.project_init` JSON wrapper

**Source**: `projects_v2.rs::run_bootstrap_collections`, `run_install_bundle`, `run_install_bundle_update_with_root`, `run_migrate_dry_run`, `drop_owned_collections`.
**New home (v0.2.54)**: `projects_v2_subprocess.rs::run_project_init(invocation) -> ProjectInitResult` per Rust audit Finding B.
**Owner track (v0.2.54)**: TBD.

#### DEDUP-12: Rust `which_on_path` SSOT

**Source**: `installer.rs:2180` (`Option<PathBuf>`), `projects_v2.rs:5223` (`bool`).
**New home (v0.2.54)**: `launcher/src-tauri/src/commands/path_util.rs::which_on_path(name) -> Option<PathBuf>`. Replace bool callsites with `.is_some()`.
**Owner track (v0.2.54)**: TBD.

#### DEDUP-13: Rust install-root finder SSOT

**Source**: 5 walkers, 4 predicates with partial disagreement (Rust audit Finding C).
**New home (v0.2.54)**: `launcher/src-tauri/src/commands/install_root.rs` with `RootKind` enum + `walk_up_for(start, kind, max_levels)` + `find_from_exe(kind)`.
**Owner track (v0.2.54)**: TBD.
**Risk**: MEDIUM (every existing caller assumes a specific predicate's semantics).

#### DEDUP-14: Rust paired sentinel+deferral writer

**Source**: 3 sites in `installer.rs` (4325, 5779, 5946) — the v0.2.51 Bug A pattern.
**New home (v0.2.53 — Track C lands this Tier-1 because it closes a class)**: `installer.rs::handle_pull_conflict(install_path, op, pull_branch, combined_output)` per Rust audit Finding E.

**Note**: This is PROMOTED from Tier-2 to Tier-1 because the cost is ~10 LoC and the bug class it closes is exactly the v0.2.51 Bug A that triggered the original sentinel work.

**Owner track**: C (NEW-11 hardening + this paired writer go together).
**Risk**: LOW.
**Test**: `tests/test_paired_sentinel_deferral.rs` — assert both writes happen atomically (or both fail).

#### DEDUP-15: Shell `lib/asset-ref-count`

**Source**: 7 sites, 2 substrings (`_app/immutable/` broad vs `_app/immutable/assets` narrow). The narrow variant false-rejects Svelte 5 builds (NEW-1).
**New home (v0.2.53 — promoted to Tier-1, owned by Track A)**: `scripts/lib/asset-ref-count.sh` + `scripts/lib/asset-ref-count.ps1`. Both use the broad `_app/immutable/` substring. Sourced from all 7 sites.
**Owner track**: A.
**Risk**: MEDIUM (behavior change at runtime; currently-rejected binaries would now launch).
**Test**: `tests/test_asset_ref_count_parity.py` — assert all 7 sites give same count for a known good Svelte 5 binary.
**Closes**: NEW-1.

#### DEDUP-16: Shell `lib/launcher-candidates`

**Source**: 5+ sites (post-install-launcher.sh + post-install-launcher.ps1 + start-launcher.* × 3 + first-install.bat).
**New home (v0.2.53 — partial, Track A lands the macOS path drift fix)**: For v0.2.53, the hardcoded macOS list is FIXED to `macos-arm64/` everywhere (M-P0-2) but the full shared-fixture extraction is deferred to v0.2.54.
**Owner track**: A (v0.2.53 — path drift fix only) + TBD (v0.2.54 — full extract).
**Risk**: MEDIUM.

#### DEDUP-17: `vco_lib/settings_merge.py`

**Source**: `install.py::_merge_settings_template` (22038), `_smart_merge_settings` (22066), `_merge_hooks_block` (22085) vs `vco_lib/project_init.py::_merge_settings_template_for_bundle` (6525) + siblings.
**New home (v0.2.54)**: `vco_lib/settings_merge.py` with `dry_run` kwarg; install.py imports + calls with `dry_run=False`; project_init.py with the user's flag.
**Owner track (v0.2.54)**: TBD.
**Risk**: LOW (logic is well-isolated, mostly pure functions).
**Bug-fix opportunity**: install.py's variant doesn't use `_write_file_atomic` → crash mid-write leaves torn settings.json. Extraction unifies both sides on the safer code path.

---

## 6. Per-track ownership table

This table is the **non-overlap contract**. Other tracks reference their assigned rows and DO NOT touch files outside their column.

| Item | Owner Track | Files modified | Files NEW | Test |
|---|---|---|---|---|
| **M-P0-1** | A | `install.sh:575` | — | `test_install_sh_no_args.sh` (bash 3.2 + `set -u` smoke) |
| **M-P0-2** | A | `start-launcher.command:28`, `post-install-launcher.sh:397-402`, `rebuild-dist-binary.sh:46` | — | `test_macos_arm64_no_experimental_refs.py` |
| **M-P0-3** | A | `post-install-launcher.sh:583-589, :584` | — | `test_macos_linux_zip_download_filter.py` |
| **M-P0-4** | B | install.py 8+ callsites via DEDUP-1 | — | `test_run_logged_subprocess.py` |
| **M-P0-5** | A | `post-install-launcher.sh:851,853,862` | — | `test_post_install_launcher_no_local_outside_function.py` |
| **M-P0-6** | A | `post-install-launcher.sh:263-282` | — | `test_apple_silicon_homebrew_path_probe.sh` |
| **M-P0-7** | C | `launcher/src-tauri/vct-launcher-core/src/services/runtime.rs:181-191`, `self_update.rs:316-325` | — | `test_launcher_path_augmentation.rs` (macOS + Linux branches) |
| **M-P0-8** | C | `launcher/src/lib/InstallHealthGate.svelte:67-82` | — | `test_install_gate_refresh_on_focus.spec.ts` |
| **M-P0-9** | D | `.github/workflows/installer-smoke.yml` | `.github/workflows/install-smoke-tri-os.yml` | (CI is its own test) |
| **NEW-1** | A | All 7 substring sites | `scripts/lib/asset-ref-count.sh`, `scripts/lib/asset-ref-count.ps1` | `test_asset_ref_count_parity.py` |
| **NEW-2** | F | `vct-hub/src/config_api.rs:653` | — | `test_dev_collection_case_rebind.rs` |
| **NEW-3** | C | `launcher/src-tauri/src/commands/installer.rs:9596` | — | `test_python_candidate_parity.py` |
| **NEW-4** | B | install.py `_wait_for_weaviate` (or whatever the divergent site is); installer.rs comment removal | — | `test_weaviate_health_endpoint_parity.py` |
| **NEW-7 (B1)** | F | `vco_lib/project_init.py::install_project_bundle`, `launcher/src-tauri/src/commands/projects_v2.rs::run_install_bundle_update_with_root` | `tests/test_project_bundle_resume_sentinel.py` | as named |
| **NEW-8 (B3)** | F | `vco_lib/project_init.py::_write_file_atomic` | — | `test_project_bundle_symlink_blocking.py` |
| **NEW-9 (B2)** | F | (audit-only first; if broken, `launcher/src-tauri/src/commands/project_state_cmd.rs::set_project_*_enabled`) | — | `test_fs_disable_contract_end_to_end.py` |
| **NEW-10** | F | 4-way `canonical_class_prefix` sites | — | `test_canonical_class_prefix_parity.py` |
| **NEW-11** | C | `launcher/src-tauri/src/commands/installer.rs:6255` (sha_at_conflict empty-string hardening) | — | `test_resume_sentinel_empty_sha_refusal.rs` |
| **DEDUP-1** | B | install.py 40+ callsites | — (helper added inline to install.py) | `test_run_logged_subprocess.py` |
| **DEDUP-2** | B | `_try_download_launcher_binary`, `_try_download_vct_hub_binary` | — | `test_download_release_binary.py` |
| **DEDUP-3** | B | 51 `DeferralEntry(...)` callsites in install.py | — | `test_make_deferral.py` |
| **DEDUP-4** | B | install.py 5 inline sqlite3 sites | — | `test_launcher_db_reader_parity.py` |
| **DEDUP-5** | B | install.py `.claude.json` atomic-write | — | `test_atomic_write_cleanup.py` |
| **DEDUP-6** | F | 4-way sanitiser sites + codegraph_to_mermaid.py rename | — | `test_canonical_class_prefix_parity.py` |
| **DEDUP-14** (PROMOTED) | C | `installer.rs:4325`, `:5779`, `:5946` | — | `test_paired_sentinel_deferral.rs` |
| **DEDUP-15** (PROMOTED) | A | 7 substring sites | `scripts/lib/asset-ref-count.{sh,ps1}` | `test_asset_ref_count_parity.py` |
| **M-P1-1** | B | install.py wheel-detection (`pip install --dry-run --only-binary=:all:`); 3.14+ refusal message | — | `test_python_wheel_support_detection.py` |
| **M-P1-2** | B | install.py `_start_container_daemon` macOS + Windows `podman machine init` | — | `test_podman_machine_init.py` |
| **M-P1-3** | B | `_pip_subprocess_env`: add `--timeout`, `--retries`, `--prefer-binary`, `PIP_DISABLE_PIP_VERSION_CHECK`, `PIP_NO_INPUT` | — | `test_pip_subprocess_env.py` |
| **M-P1-4** | A | All `.command` files | — | `test_command_finder_cwd.py` |
| **M-P1-5** | C | `InstallHealthGate.svelte` + 3 other localStorage flags | — | `test_localstorage_cross_clone_scoping.spec.ts` |
| **M-P1-6** | C | `InstallHealthGate.svelte` (add Run-installer button) | — | `test_install_gate_run_installer_button.spec.ts` |
| **M-P1-7** | B | folded into DEDUP-1 | — | (covered by DEDUP-1 test) |
| **M-P1-8** | D | `pre-ship-check.sh` (add gate 22) | — | (CI gate test) |
| **L-P0-1** | G2 | `install.py:7338-7357`, `install.sh:107-138/263-291/378-403` | — | `test_distro_pkg_mgr_parity.py` (assert install.py + install.sh + post-install-launcher.sh same pkgmgr set) |
| **L-P0-2** | G2 | `install.sh` Tauri deps block; new fallback for `libwebkit2gtk-4.0-dev` | — | `test_libwebkit2gtk_fallback.sh` |
| **L-P0-3** | G3 | install.py container compose | — | `test_selinux_z_flag.py` |
| **L-P0-4** | G3 | `launcher/src-tauri/vct-launcher-core/src/services/runtime.rs:438` PATH augmentation | — | `test_linux_desktop_launch_path_augmentation.rs` |
| **L-P0-5** | G3 | `first-install.desktop` `%k` + Exec quoting | — | `test_kde_plasma6_desktop_launch.sh` |
| **L-P0-6** | D | `installer-smoke.yml` Job 1 promotion | — | (CI) |
| **L-P0-7** | G2 | install.py nvidia-container-toolkit install hint | — | `test_nvidia_container_toolkit_hint.py` |
| **L-P0-8** | G3 | install.py render/video group remediation hint | — | `test_render_group_remediation.py` |
| **W-P1-1** | H | `first-install.bat:127-137` JSONL log writer | — | `test_first_install_bat_jsonl_writer.bat` (exercise apostrophe + backslash + tab in detail) |
| **W-P1-2** | H | `first-install.bat:315` refreshenv fallback | — | `test_first_install_bat_refresh_env.bat` |
| **W-P1-3** | H + D | `installer-smoke.yml` Job 4 → switch to `shell: cmd`; ADD `install.ps1` job under pwsh AND powershell | — | (CI) |
| **W-P1-4** | H | `templates/scripts/vct_project_config.ps1`, `vct_access_check.ps1` PS 5.1 fallback OR require PS 7+ in install.py | — | `test_powershell_compat.ps1` |
| **W-P1-5** | H | `install.py:16540` Scheduled Task XML escaping | — | `test_scheduled_task_xml_escape.py` |
| **CORRECT-1** | B | covered by DEDUP-5 | — | (DEDUP-5 test) |
| **CORRECT-2** | B | covered by DEDUP-4 | — | (DEDUP-4 test) |
| **CVE-1 (NEW-5)** | E | install.py `_install_pinned_npm` is_file_pin branch | — | `test_npm_omit_dev.py` |
| **CVE-2 (NEW-6)** | E | `launcher/package-lock.json` sync | — | (lockfile-sync workflow) |
| **DC-1/2/3** | E | `launcher/src-tauri/src/commands/project_env_settings.rs` dead-code cleanup | — | (cargo warnings = 0) |
| **D1/D2/D3** | E | `docs/CONFIGURATION.md`, `docs/GETTING_STARTED.md`, `docs/TROUBLESHOOTING.md` doc stubs | — | n/a (doc only) |
| **V52-AH-FE** | E | `launcher/src/lib/UpdateToast.svelte` (or wherever) | — | `test_update_event_toast.spec.ts` |
| **V52-O.11.B** | E | code-graph Svelte parser | — | `test_svelte_parser.py` |
| **V52-O.11.N** | E | code-graph PowerShell parser | — | `test_powershell_parser.py` |
| **Bootstrap mode** | B | install.py `--bootstrap` argv + dispatch | (helper functions inline) | `test_install_py_bootstrap_mode.py` |
| **Bootstrap envelope schema** | B | install.py emit envelope; schema lives in `docs/schemas/install-bootstrap-envelope-v1.json` | `docs/schemas/install-bootstrap-envelope-v1.json` | `test_bootstrap_envelope_schema.py` |
| **`metadata.json` writer (CI)** | D | `.github/workflows/release.yml` commit-dist-binaries step | (per-OS `launcher/dist/<os-arch>/metadata.json` emitted at build time) | `test_dist_metadata_schema.py` |
| **`metadata.json` reader (shells)** | A | `start-launcher.{sh,command,bat}` + `post-install-launcher.sh` | — | `test_launcher_metadata_reader.sh` |

**Critical non-overlap rule**: if your row says "files modified: X", you may modify X. You may NOT modify any file in another track's column unless it's in YOUR row too. If integration requires touching shared files (e.g. `install.py`), the touch goes in Track B's worktree only; other tracks coordinate via the Phase 2 integration merge.

---

## 7. New `vco_lib/` modules

Per **`vco-lib-python-dedup-2026-06-10.md`**: 6 new modules consolidate cross-cutting Python concerns. v0.2.53 lands the minimum necessary; the rest land in v0.2.54.

### 7.1 `vco_lib/atomic.py` (v0.2.54, prep in v0.2.53)

**Purpose**: atomic file writes with crash-safety. Centralises the `<x>.tmp + os.replace` pattern used 8+ times in install.py and matches the existing `vco_lib/env_template.py::_atomic_write_text`.

**Exports**:
```python
def atomic_write_text(path: Path, body: str, *, encoding: str = "utf-8", fsync: bool = True) -> None: ...
def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None: ...
def atomic_write_json(path: Path, obj: Any, *, indent: int = 2, fsync: bool = True) -> None: ...
```

Crash-safety: `tmp.flush()` + `os.fsync()` BEFORE `os.replace` (closes the power-loss-leaves-empty-file bug noted in install-py-dedup audit #13).

**v0.2.53 prep**: Track B re-uses existing `vco_lib/env_template.py::_atomic_write_text` via DEDUP-5; the new module is a v0.2.54 organisational refactor that promotes the helper to its own namespace.

**Owner**: Track B (v0.2.53) for DEDUP-5; TBD for v0.2.54 module creation.

### 7.2 `vco_lib/hashing.py` (v0.2.54)

**Purpose**: sha256 + xxhash helpers. Consolidates 4 sites per audit Finding 3.

**Exports**:
```python
def sha256_file(path: Path, chunk_size: int = 65536) -> str: ...
def sha256_bytes(data: bytes) -> str: ...
def sha256_text(text: str) -> str: ...
```

**Owner**: TBD (v0.2.54).

### 7.3 `vco_lib/settings_merge.py` (v0.2.54)

**Purpose**: settings.json template merge — single home for `_merge_settings_template` + `_smart_merge_settings` + `_merge_hooks_block`.

**Exports**:
```python
def merge_settings_template(
    target: Path,
    template_data: dict,
    *,
    dry_run: bool = False,
) -> SettingsMergeResult: ...
```

Used by both `install.py` (orchestrator-self settings.json) and `vco_lib/project_init.py` (per-project settings.json).

**Owner**: TBD (v0.2.54).

### 7.4 `vco_lib/manifest.py` — REMOVED in v0.2.75

**Original purpose**: `.vco-manifest.json` schema + read/write, consolidating the per-project manifest writer (`project_init.py::_write_manifest_atomic`) and the orchestrator-self manifest writer (`install.py::_refresh_orchestrator_self_vco_manifest`).

**Outcome**: the v0.2.53 skeleton's three stubs (`read_manifest` / `write_manifest` / `validate_manifest`) only raised `NotImplementedError` with a "lands in v0.2.54" promise; the migration never happened and the module accumulated zero production callers across ~22 releases. Deleted in v0.2.75 rather than left as a dead promise. The two live writers named above remain the canonical `.vco-manifest.json` code paths; if a consolidation is attempted again, start from those call-sites, not from a pre-seeded skeleton.

### 7.5 `vco_lib/timeutil.py` (v0.2.54)

**Purpose**: UTC ISO-8601 timestamp helper (audit Finding 7 — 4 sites with 2 different formats).

**Exports**:
```python
def utc_iso_now() -> str: ...  # "%Y-%m-%dT%H:%M:%SZ" form
def utc_iso_now_us() -> str: ...  # microsecond + +00:00 form (for embedding_service.py back-compat)
```

**Owner**: TBD (v0.2.54).

### 7.6 `vco_lib/git_meta.py` (v0.2.54)

**Purpose**: git HEAD/rev resolution (install.py:15445 file-read vs project_init.py:2610 subprocess).

**Exports**:
```python
def resolve_vco_version(orchestrator_root: Path) -> str: ...  # "v0.2.53" | "9396ea96" | "unknown"
def git_short_sha(repo: Path) -> str | None: ...
def git_branch(repo: Path) -> str | None: ...
```

**Owner**: TBD (v0.2.54).

---

## 8. Rust shared middle layer

Per **`rust-installer-dedup-2026-06-10.md`**: 8 primitives proposed for `launcher/src-tauri/src/commands/install_helpers.rs` (or split across smaller modules).

### 8.1 `subprocess::run` (Finding A — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/subprocess.rs` (new module).
**Migrates**: 101 raw `Command::new` callsites across `installer.rs`, `projects_v2.rs`, `git_user_editable_merge.rs`.
**Owner (v0.2.54)**: TBD.
**Why deferred**: blast radius (101 callsites). Tier-1 land in v0.2.53 is just Finding E (paired sentinel) which is 3 sites.

### 8.2 `prep_python_subprocess` (Finding N — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/subprocess.rs::prep_python_subprocess(cmd: &mut Command)`.
**Sets**: `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `VCT_LAUNCHER_PID=<pid>`, Windows `creation_flags(0x08000000)`.
**Migrates**: 15 callsites.
**Owner (v0.2.54)**: TBD.

### 8.3 `emit_progress` with `Stage` enum (Finding L — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/progress.rs`.
**Replaces**: 54 free-form `window.emit("install_progress", ...)` callsites. Stage-name typos silently break the frontend.
**Migrates**: 54 callsites.
**Owner (v0.2.54)**: TBD.

### 8.4 Audit-log helper (Finding O — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/audit.rs::audit(operation, project_id, json)`.
**Migrates**: install_audit JSONL writes that currently span installer.rs + projects_v2.rs with inconsistent shapes.
**Owner (v0.2.54)**: TBD.

### 8.5 JSON-envelope parser (Finding B — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/project_init_envelope.rs`.
**Replaces**: 5 near-identical `vco_lib.project_init <subcmd> --json` wrappers in `projects_v2.rs`.
**Owner (v0.2.54)**: TBD.

### 8.6 Paired sentinel+deferral writer (Finding E — LANDS in v0.2.53)

**Location**: `installer.rs::handle_pull_conflict(install_path, op, pull_branch, combined_output) -> String`.
**Migrates**: 3 sites in installer.rs (4325, 5779, 5946).
**Owner**: C (v0.2.53).
**Why Tier-1 in v0.2.53**: ~10 LoC cost; closes the v0.2.51 Bug A class by construction (forgetting one of the paired writes is the actual bug).
**Risk**: LOW.
**Test**: `test_paired_sentinel_deferral.rs` — assert both writes are atomic (sentinel + deferral both written, OR both fail).

### 8.7 Install-root finders SSOT (Finding C — DEFERRED to v0.2.54)

**Location**: `launcher/src-tauri/src/commands/install_root.rs`.
**Replaces**: 5 walkers + 4 predicates with partial disagreement.
**Owner (v0.2.54)**: TBD.

### 8.8 Pre-pull rename (Finding F — DEFERRED to v0.2.54)

**Location**: `installer.rs::pre_pull_rename_binary(target, install_path, label)`.
**Replaces**: 2 near-twin Windows bodies (~50 LoC each).
**Owner (v0.2.54)**: TBD.
**Sets up**: future `vct-updater.exe` binary support.

### 8.9 v0.2.53 Rust touch summary

Only one new shared helper lands in v0.2.53: `handle_pull_conflict`. The rest are documented here as the v0.2.54 plan so implementing tracks know the architectural direction without writing the helpers prematurely.

---

## 9. Tri-OS CI smoke spec

Per **M-P0-9** and **`macos-release-ci-audit-2026-06-10.md`**:

### 9.1 Workflow file

**Name**: `.github/workflows/install-smoke-tri-os.yml`
**Owner**: D
**Triggers**:
```yaml
on:
  pull_request:
    paths:
      - 'install.py'
      - 'install.sh'
      - 'install.ps1'
      - 'first-install.*'
      - 'start-launcher.*'
      - 'scripts/post-install-launcher.*'
      - 'launcher/src-tauri/src/commands/installer.rs'
      - '.github/workflows/install-smoke-tri-os.yml'
  push:
    branches: [main]
  schedule:
    - cron: '0 6 * * *'  # daily 06:00 UTC
```

### 9.2 Matrix

```yaml
jobs:
  install-smoke:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-22.04, ubuntu-24.04, macos-14, windows-latest, fedora-40]
    runs-on: ${{ matrix.os }}
```

**Why these OSes**:
- `ubuntu-22.04` — modal corporate distro (Debian 12 stable analog). Tests L-P0-2 (`libwebkit2gtk-4.0-dev` fallback).
- `ubuntu-24.04` — current Ubuntu LTS. Tests L-P0-2 (`libwebkit2gtk-4.1-dev` path).
- `macos-14` — Apple Silicon (M3) runners. Tests M-P0-* (bash 3.2, `/opt/homebrew/`, `experimental_macOS` drift).
- `windows-latest` — currently `windows-2022`. Tests W-P1-* + `first-install.bat`.
- `fedora-40` — SELinux + dnf + Linuxbrew gap. Tests L-P0-3 (SELinux `:Z`) + L-P0-1 (dnf parity).

**Future addition (v0.2.54)**: openSUSE Leap (zypper) + Alpine (apk). Deferred because GitHub Actions doesn't host these directly; would need self-hosted runners or container-in-VM.

### 9.3 Steps per OS

```yaml
steps:
  # 1. Fresh git clone — NOT actions/checkout
  - name: Clone fresh (third-party-user experience)
    run: |
      git clone https://github.com/${{ github.repository }} vco-fresh-clone
      cd vco-fresh-clone
      git checkout ${{ github.sha }}
    shell: bash

  # 2. Pre-install probe (smoke for install.py --bootstrap)
  - name: Bootstrap probe (read-only)
    working-directory: vco-fresh-clone
    run: |
      python3 install.py --bootstrap --json > bootstrap-pre.json
      cat bootstrap-pre.json | python3 -m json.tool
    shell: bash
    if: matrix.os != 'windows-latest'

  - name: Bootstrap probe (Windows)
    working-directory: vco-fresh-clone
    run: |
      py -3 install.py --bootstrap --json > bootstrap-pre.json
      type bootstrap-pre.json
    shell: cmd
    if: matrix.os == 'windows-latest'

  # 3. Run actual first-install
  - name: first-install.sh (Linux)
    working-directory: vco-fresh-clone
    run: bash first-install.sh --yes --no-auto-launch
    shell: bash
    if: matrix.os == 'ubuntu-22.04' || matrix.os == 'ubuntu-24.04' || matrix.os == 'fedora-40'

  - name: first-install.command (macOS)
    working-directory: vco-fresh-clone
    run: bash first-install.command --yes --no-auto-launch
    shell: bash
    if: matrix.os == 'macos-14'

  - name: first-install.bat (Windows)
    working-directory: vco-fresh-clone
    run: first-install.bat /yes /no-auto-launch
    shell: cmd
    if: matrix.os == 'windows-latest'

  # 4. Assert exit 0 (job step would have failed already if non-zero)

  # 5. Assert expected files exist
  - name: Verify install artifacts
    working-directory: vco-fresh-clone
    run: |
      test -d .venv || (echo "MISSING .venv" && exit 1)
      test -d state || (echo "MISSING state/" && exit 1)
      test -d claude_mcp_servers/.venv || (echo "MISSING claude_mcp_servers/.venv" && exit 1)
      test -f .env || (echo "MISSING .env" && exit 1)
      grep -q "KG_COLLECTION=" .env || (echo ".env missing KG_COLLECTION" && exit 1)
      echo "Install artifacts OK"
    shell: bash
    if: matrix.os != 'windows-latest'

  # (Windows equivalent uses if exist ... else exit 1)

  # 6. Launcher headless boot
  - name: Launcher --check-only
    working-directory: vco-fresh-clone
    run: |
      ./launcher/dist/${{ env.LAUNCHER_DIST_SUBDIR }}/vct-launcher --check-only
    shell: bash
    env:
      LAUNCHER_DIST_SUBDIR: ${{ matrix.os == 'macos-14' && 'macos-arm64' || matrix.os == 'windows-latest' && 'windows-x64' || 'linux-x64' }}
    if: matrix.os != 'windows-latest'

  # (Windows equivalent calls .exe)

  # 7. Post-install bootstrap probe — assert ready_to_install=true now
  - name: Bootstrap probe (post-install)
    working-directory: vco-fresh-clone
    run: |
      python3 install.py --bootstrap --json > bootstrap-post.json
      python3 -c "import json; d=json.load(open('bootstrap-post.json')); assert d['ready_to_install'], d['blocker_messages']"
    shell: bash
    if: matrix.os != 'windows-latest'

  # 8. Assert install gate would NOT fire
  - name: Verify InstallHealthGate would pass
    working-directory: vco-fresh-clone
    run: |
      python3 -c "
      import json
      d = json.load(open('bootstrap-post.json'))
      assert d['paths']['install_root_kind'] == 'completed_install', d['paths']
      assert d['paths']['state_dir_exists']
      assert d['paths']['launcher_binary_exists']
      print('Install gate would NOT fire — OK')
      "
    shell: bash
    if: matrix.os != 'windows-latest'

  # 9. Upload artifacts on failure for debugging
  - name: Upload bootstrap envelopes on failure
    if: failure()
    uses: actions/upload-artifact@v4
    with:
      name: bootstrap-envelopes-${{ matrix.os }}
      path: |
        vco-fresh-clone/bootstrap-pre.json
        vco-fresh-clone/bootstrap-post.json
        vco-fresh-clone/state/logs/install.jsonl
```

### 9.4 The `--check-only` launcher flag (NEW)

Track C must add `--check-only` to the launcher binary. When invoked with this flag, the launcher:
1. Performs the install-health check.
2. Prints the result to stdout as JSON.
3. Exits 0 if healthy, 1 if not healthy, 2 if check itself failed.
4. Does NOT open the GUI window.

This is what CI uses to verify "launcher boots in headless mode."

### 9.5 Pre-ship-check gate 22

**File**: `scripts/pre-ship-check.sh`
**Owner**: D
**Gate 22**: "tri-OS smoke green on main"

Implementation: `gh run list --workflow=install-smoke-tri-os.yml --branch=main --limit=1 --json conclusion --jq '.[0].conclusion'` MUST be `success`.

### 9.6 Why fresh `git clone` (not `actions/checkout`)

`actions/checkout` does shallow clone, sets up auth, and does NOT exercise the experience a third-party user gets when they `git clone` from the public repo. The fresh-clone step ensures the CI exercises the same code path the macOS fresh-clone report hit (and failed at).

---

## 10. Parity tests required

Each test gets: filename, what it asserts, owner track. Implemented across `tests/` (Python) and `launcher/src-tauri/tests/` (Rust) and `launcher/tests/` (Svelte).

### 10.1 Cross-language schema parity (from cross-file dedup audit §Q4)

| Test | Asserts | Owner |
|---|---|---|
| `tests/test_launcher_dist_subdir_parity.py` | `post-install-launcher.sh`, `start-launcher.{sh,command,bat}`, `installer.rs::launcher_dist_subdir()`, `install.py::_launcher_binary_relative_path()` all return same per-OS subdir | A |
| `tests/test_python_candidate_parity.py` | Candidate-list literals from `install.sh:48`, `install.ps1:233`, `installer.rs:9596`, `first-install.{command,sh,bat}` are equal sets (ordering allowed to differ if the priority semantic is documented as TODO) | C |
| `tests/test_jsonl_log_schema_parity.py` | Invokes the bash, BAT, Python, and Rust log writers with same `(step, phase, detail, data)` payload; asserts the resulting JSON line parses to the same dict (modulo `actor` and `ts`) | A + H + B |
| `tests/test_container_runtime_detection_parity.py` | Monkeypatches PATH with `podman` and `docker` shims; runs each detection impl; asserts they all return `("podman", ...)` | C + G2 |
| `tests/test_update_deferred_roundtrip.rs` | Invoke `write_update_resume_deferral` (Rust); read with `DeferralReport.read()` (Python); assert round-trip | C |
| `tests/test_macos_arm64_no_experimental_refs.py` | Grep entire repo for `experimental_macOS`; assert zero matches outside install.py's legacy-alias comment | A |
| `tests/test_weaviate_health_endpoint_parity.py` | Grep `install.py` and `installer.rs` for `/v1/.well-known/ready` and `/v1/meta`; assert one canonical endpoint everywhere (after NEW-4 fix) | B |

### 10.2 New parity tests (Linux/Windows audit additions)

| Test | Asserts | Owner |
|---|---|---|
| `tests/test_distro_pkg_mgr_parity.py` | `install.py:7338-7357`, `install.sh:107-138/263-291/378-403`, `post-install-launcher.sh` advertise the SAME pkgmgr set (apt/dnf/pacman/zypper/apk) | G2 |
| `tests/test_libwebkit2gtk_fallback.sh` | On Ubuntu 22.04: install.sh advertises both `libwebkit2gtk-4.1-dev` AND `libwebkit2gtk-4.0-dev` fallback | G2 |
| `tests/test_selinux_z_flag.py` | When `getenforce` returns `Enforcing`, bind-mount volume args in install.py compose include `:Z` | G3 |
| `tests/test_linux_desktop_launch_path_augmentation.rs` | `runtime.rs::augment_path()` adds `~/.cargo/bin`, `~/.local/bin`, `/home/linuxbrew/.linuxbrew/bin`, `/snap/bin` on Linux | G3 |
| `tests/test_kde_plasma6_desktop_launch.sh` | `first-install.desktop` Exec quoting parses correctly under KDE Plasma 6's parser | G3 |
| `tests/test_first_install_bat_jsonl_writer.bat` | Exercise apostrophe + backslash + tab in `detail`; assert JSON line parses cleanly | H |
| `tests/test_first_install_bat_refresh_env.bat` | After winget install, PATH re-read from registry works (without `refreshenv`) | H |
| `tests/test_powershell_compat.ps1` | `vct_project_config.ps1` parses + runs cleanly under stock PS 5.1 (OR install.py prereq check refuses install if PS < 7) | H |
| `tests/test_scheduled_task_xml_escape.py` | XML-escape `&` + `<` + `>` + `"` + `'` in USER_ID before schtasks /Create | H |

### 10.3 Tier-1 dedup regression tests

| Test | Asserts | Owner |
|---|---|---|
| `tests/test_run_logged_subprocess.py` | Happy-path, timeout, non-zero exit, dot-cycle after 3s, env-scrub, on_failure modes | B |
| `tests/test_download_release_binary.py` | gh path, curl fallback, both unavailable, ZIP-not-found, chmod 0o755 on POSIX | B |
| `tests/test_make_deferral.py` | Round-trip through DeferralReport for 3+ sample condition_ids; missing `kg_node_refs` defaults to `[]` | B |
| `tests/test_launcher_db_reader_parity.py` | Inline vs vco_lib reader return same row for 5+ known keys | B |
| `tests/test_atomic_write_cleanup.py` | Simulate write failure mid-rename; assert `.tmp` deleted | B |
| `tests/test_canonical_class_prefix_parity.py` | 4 sanitiser sites converge for fixed input set; underscore-preserving variant correctly renamed | F |
| `tests/test_paired_sentinel_deferral.rs` | Both writes atomic | C |
| `tests/test_asset_ref_count_parity.py` | All 7 substring sites give same count for known good Svelte 5 binary | A |
| `tests/test_project_bundle_resume_sentinel.py` | Mid-update Cmd-C → sentinel written; resume command succeeds | F |
| `tests/test_project_bundle_symlink_blocking.py` | Symlinked .claude/agents/coder.md → preserved (not overwritten) | F |
| `tests/test_fs_disable_contract_end_to_end.py` | GUI disable toggle → file moves to .disabled/; bundle update respects it | F |

### 10.4 Tri-OS smoke (CI-driven)

See §9. Each OS run is itself a parity test asserting the install completes end-to-end under that OS's idioms.

---

## 11. Migration risks + rollback plan

### 11.1 Risk: `install.py --bootstrap --json` malformed under edge cases

**Scenario**: bootstrap envelope contains invalid JSON because some probe returned bytes the JSON encoder can't handle (e.g. nvidia-smi output with mixed encodings on Windows).

**Detection**:
- Track B writes `tests/test_bootstrap_envelope_schema.py` validating against `docs/schemas/install-bootstrap-envelope-v1.json`.
- The schema test runs on every `pytest`.
- The CI tri-OS smoke implicitly tests it by `python -m json.tool` on the output.

**Rollback**:
- Bootstrap mode is **additive**. If the envelope is broken on some OS, the shim falls back to the legacy code path (today's behavior).
- Specifically: `first-install.command` catches the bootstrap exit code; if non-zero, prints stderr and falls back to invoking `install.py` (no flags) for the legacy install flow.
- This means: even if `--bootstrap` is broken on a particular OS, the user can still install. We trade off cleaner UX for safety.

### 11.2 Risk: shim forwards `$@` / `%*` incorrectly

**Scenario**: macOS shim drops a quoted argument with spaces (e.g. `--install-root "/Users/<user>/My Projects/vco"`).

**Detection**:
- Track A writes `tests/test_shim_argv_forwarding.sh` (and `.bat` equivalent) exercising args with spaces, special chars, quotes.
- CI tri-OS smoke passes a known set of args through; envelope output is grep'd for them.

**Rollback**:
- Shim files are tiny (~30-60 LoC). Revert is `git revert <shim-commit>`.

### 11.3 Risk: new Tier-1 dedup helper subtly alters behavior

**Scenario**: `_run_logged_subprocess` changes stderr-tail format such that downstream parsers (e.g. the launcher's `state/logs/install.jsonl` reader) break.

**Detection**:
- Phase 3 AUDIT-5 (Dedup correctness) is the explicit gate. AUDIT-5 must spot-check the Tier-1 extracts byte-by-byte for at least the 8 silent-hang callsites.
- Regression test `tests/test_run_logged_subprocess.py` covers the happy path.
- Real-install rehearsal on macOS + Linux + Windows (Phase 3) checks for surprise output drift.

**Rollback**:
- Each Tier-1 extract is a separate commit. `git revert` removes just that one.
- Master plan §2 Phase 1 explicitly says each step is a separate commit so any regression can be bisected.

### 11.4 Risk: tri-OS CI smoke is flaky

**Scenario**: macOS-14 runner takes 25 minutes to install Podman + Node; Fedora-40 runner has transient SELinux denials.

**Detection**:
- Run the workflow on a non-gating cron schedule for 2 weeks BEFORE making it a release gate.
- Track flakiness via `gh run list --workflow=install-smoke-tri-os.yml --json conclusion --jq '.[] | .conclusion' | sort | uniq -c`.
- If flakiness > 5% in 2 weeks, DO NOT promote to gating.

**Rollback**:
- Workflow can be gating OR non-gating via the `pre-ship-check.sh` gate 22 toggle. Master plan §2 Phase 4 mentions: don't tag v0.2.53 if gate 22 is red. If we discover flakiness POST-tag, demote gate 22 to warning-only for v0.2.54+ work and fix the flakes.

### 11.5 Risk: schema_version=1 envelope locks us into bad design

**Scenario**: we discover v0.2.54 needs a key we didn't include in schema_version=1.

**Mitigation**:
- Envelope MUST be additive — consumers ignore unknown keys. The schema declares this contract explicitly.
- Adding a new key in v0.2.54 stays at `schema_version=1`. Bumping to schema_version=2 is reserved for breaking changes (renamed or removed keys).
- Consumers MUST check `schema_version` at the top. If they see a version they don't support, they MUST refuse gracefully (fall back to legacy code path) rather than crash.

### 11.6 Risk: `--check-only` launcher flag breaks GUI launches

**Scenario**: Track C adds `--check-only` but the parsing logic accidentally fires on the GUI launch path too.

**Detection**:
- Track C writes a Rust unit test asserting `--check-only` is detected ONLY when explicitly passed, and the GUI launch path (no args) still opens the window.
- CI smoke step 6 (launcher headless boot) tests `--check-only`; the existing launcher-smoke tests should cover the no-args case.

**Rollback**:
- Revert just the `--check-only` flag patch. The rest of the launcher remains unchanged.

### 11.7 Risk: bootstrap envelope leaks secrets

**Scenario**: the envelope inadvertently includes the `GITHUB_TOKEN`, the `hub.token`, or some pip auth header.

**Mitigation**:
- The envelope spec (§3.3) does NOT include any field that resolves to a secret.
- Track B implements a `_scrub_envelope_for_secrets()` defense-in-depth pass before emit. It runs the envelope through the same `_VCO_MANAGED_KEYS_SENTINEL` filter that install.py already uses for `.claude.json` writes.
- Phase 3 AUDIT-3 (Cross-cutting wiring) explicitly checks: "does the envelope contain any string matching credential patterns (`/[A-Za-z0-9]{40,}/`, `ghp_*`, `Bearer *`, etc.)?"

### 11.8 Risk: shim-falls-back-on-bootstrap-failure causes silent regression to legacy path

**Scenario**: User on a broken system gets the legacy code path silently because bootstrap envelope was malformed. They report bug "v0.2.53 looks just like v0.2.52" — and we don't know which path they hit.

**Mitigation**:
- The shim PRINTS a clear message on fallback: "WARNING: install.py --bootstrap failed; falling back to legacy install flow. Please report this output."
- The JSONL log writer (bash + BAT) writes a `bootstrap_failed` event when this happens, so we can correlate.
- Phase 3 AUDIT-1 (Gap hunt) verifies the fallback message is shown when bootstrap is forced to fail.

---

## 12. Implementation sequencing for Phase 1

Per master plan §2 Phase 1: 9 tracks dispatch in parallel after Track G's design doc lands.

### 12.1 Critical-path dependencies

```
Track G (design doc — this doc)
    │
    ▼
┌───────────────────────────────────────────────────────┐
│                    PARALLEL DISPATCH                  │
├───────────┬──────────┬─────────┬─────────┬───────────┤
│ Track A   │ Track B  │ Track C │ Track D │ Track E   │
│ (shells)  │ (.py)    │ (Rust+ │ (CI)    │ (carry+   │
│           │          │  Svelte)│         │  CVEs)    │
├───────────┴──────────┴─────────┴─────────┴───────────┤
│ Track F   │ Track G2 │ Track G3│ Track H │           │
│ (case+    │ (Linux   │ (Linux  │ (Win)   │           │
│  bundle)  │  distro) │  GPU)   │         │           │
└───────────────────────────────────────────────────────┘
```

**Hard dependencies (B blocks others)**:
- Track B's `_run_logged_subprocess` (DEDUP-1) is needed by Track C if Track C wants to use it for any new subprocess call in Rust→Python invocations. **Resolution**: Track C does NOT use `_run_logged_subprocess` in this cycle; it stays Rust-side. So no Track B → Track C dependency in v0.2.53.
- Track B's `--bootstrap` mode is needed by Track A's shims. **Resolution**: Track A's shims are written assuming the envelope spec from §3.3; they exit gracefully if the envelope is missing keys. Track A can develop in parallel; integration test in Phase 2.

**Soft dependencies (informational, not blocking)**:
- Track A needs to know the canonical `macos-arm64` string (M-P0-2). It's stated in this doc + master plan; Track A doesn't need to wait for Track B to write it in install.py.
- Track D needs Track C's `--check-only` flag for the launcher-headless-boot step. **Resolution**: Track D develops the workflow with the launcher boot step initially STUBBED (`echo "TODO: --check-only"`); the workflow lands the actual check after Track C lands the flag (sequenced in Phase 2 integration).

### 12.2 Integration choreography (Phase 2)

After all 9 tracks complete in their worktrees:

1. **Merge order**:
   - Track G (design doc, this file) — merged first.
   - Track B (install.py — biggest blast radius) — merged second.
   - Track A (shells) — merged third.
   - Track C (Rust + Svelte) — merged fourth.
   - Tracks D, E, F, G2, G3, H — merged in parallel after the four above land.

2. **Conflict resolution rule**: any conflict between tracks goes to the parent agent (orchestrator) for surgical splice. No track may force-merge over another track's changes.

3. **Per-merge gates**:
   - `cargo test --workspace` (Rust)
   - `pytest tests/` (Python)
   - `cd launcher && pnpm test` (Svelte)
   - `bash scripts/test-install.sh` if the shells changed
   - `gh run watch` for tri-OS smoke before merging Track D

4. **Phase 3 audit dispatch happens AFTER all merges land**, not concurrently.

### 12.3 What can run in TRUE parallel

Per the audit findings and the audit table in §6:

- Tracks A + B + C + D + E + F + G2 + G3 + H can ALL run in their own git worktrees against the same base commit (`9396ea96`).
- Cap at 3 simultaneously active Claude Code subagents per CLAUDE.md guidance.
- Worktree paths follow the pattern `/tmp/vco-track-<X>-worktree`.

### 12.4 What MUST be sequential

- Phase 1 → Phase 2 → Phase 3 → Phase 4 are strictly sequential.
- Within Phase 1, Track G (this doc) → all other 9 tracks. Once this doc lands, the others fan out.
- Phase 3 audits run in parallel BUT must complete before Phase 4 tag.

---

## 13. v0.2.54+ deferrals

What we explicitly DO NOT do in v0.2.53. The user said "no rush" — these are deferrals for principled reasons, not because we ran out of time.

### 13.1 Deferred to v0.2.54

| Item | Why deferred |
|---|---|
| **Full `_run_logged_subprocess` extension to all 40+ callsites** | v0.2.53 lands the helper + migrates the 8 silent-hang callsites. The remaining ~30 callsites migrate in v0.2.54 once the helper is proven in production. |
| **All Rust Tier-2 dedup (DEDUP-10/11/12/13)** | 101-callsite blast radius; risk-managed by deferring one cycle. |
| **All `vco_lib/` Tier-2 modules (atomic, hashing, settings_merge, manifest, timeutil, git_meta)** | Module reorg work; v0.2.53 keeps using `vco_lib/env_template.py::_atomic_write_text` as the existing single source. |
| **Project bundle install consolidation into `install_project_bundle`** | Audit #12 names this as the v0.2.54 unification target. v0.2.53 just adds the 3 bug fixes (B1, B2, B3). |
| **Update-flow choreography unification (3 update flows → shared backbone)** | Rust audit Finding K; highest risk because it touches merge/rebase split logic. v0.2.54. |
| **Full PowerShell port of `post-install-launcher.sh`** | Shell-scripts-dedup Finding 1; ~500 LoC eliminated by collapsing `first-install.bat` into a shim. v0.2.54. |
| **`install.sh` Python/Node/Podman triple-duplicate refactor (DEDUP-7-equivalent for shells)** | Shell-scripts-dedup Finding 2; v0.2.54. |
| **Extend `hook-os-parity` CI gate to cover `templates/scripts/` and `scripts/`** | Memory rule from v0.2.49; v0.2.54 housekeeping. |
| **`scripts/lib/launcher-candidates.{sh,ps1}` full shared fixture** | v0.2.53 lands the macOS path-drift fix (M-P0-2) only. The full extraction is v0.2.54. |
| **`launcher/dist/<os-arch>/metadata.json` CI emit + reader** | Architecturally specified in §4.4; Track D lands the CI write; Track A lands the read with hardcoded fallback. Full integration (start-launcher.* reads metadata.json before falling back to hardcoded list) finishes in v0.2.54 once the CI has been emitting metadata.json for a release cycle. |

### 13.2 Deferred to v0.2.55+

| Item | Why deferred |
|---|---|
| **Codesigning ($99/yr Apple Dev + ~1 week integration)** | OS-3 from master plan. Quarantine workaround stays. |
| **`.app` bundle for macOS** | Deferred with codesigning; requires the same Apple Dev account. |
| **`vco://` URL scheme** | Cross-OS URL handler registration is its own architecture project. Linux `.desktop` MIME + Windows registry + macOS `LSHandlers` plist. Out of scope for install. |
| **Intel Mac support** | OS-1 decision: Apple Silicon only. Won't reverse. |
| **macOS sleep/wake handlers** | Out of scope — orchestrator daemons handle their own reconnection. |
| **Cross-encoder citation detection (XENC)** | Master plan §1E: deferred to v0.2.54. |
| **Linux openSUSE + Alpine CI runners** | GitHub Actions doesn't host these; needs self-hosted runners or container-in-VM. v0.2.54 if user demand surfaces. |
| **Intel Mac Rosetta detection beyond the envelope field** | The envelope has `macos_features.rosetta_present` but the install flow doesn't branch on it. v0.2.55 if requested. |
| **Tray left-click activates app (macOS convention)** | UX work, separate from install. |
| **Single-instance plugin per-user (not per-clone)** | Cross-clone Tauri single-instance is a separate architecture project. |
| **iCloud / OneDrive Desktop sync detection** | Out of scope — user opt-in concern. |
| **Replace `install.py` step-dispatch with subcommand-style argv (`install.py install`, `install.py update`, `install.py bootstrap`)** | Larger CLI redesign. v0.2.55+ if `install.py` grows past 30K LoC. |
| **`Stage` enum for Rust emit_progress (Finding L)** | Soft drift — fix when v0.2.54 ships the shared middle layer. |

### 13.3 Deferred indefinitely

| Item | Why |
|---|---|
| **Replacing install.py entirely with a Rust binary** | install.py is 23K LoC of carefully-tested step-dispatch; replacing is a multi-quarter project with massive regression risk. The bootstrap mode + subprocess pattern is the strategically right middle ground. |
| **Unifying JSONL log writers across 4 languages into one writer** | Cross-file dedup §Q3: JSONL emission MUST stay in 3 languages (bash hook emits BEFORE install.py exists; same for BAT). Schema parity is what we unify, not implementation. |
| **Replacing the project bundle install Python subprocess with native Rust** | Audit #12 explicitly recommends NOT doing this. The Rust-thin / Python-thick split is the right architecture. |

---

## 14. Appendix — file paths cross-reference

### 14.1 Audit files (all at `.claude/context/audits/<file>-2026-06-10.md`)

| Audit | Lines | Used by section |
|---|---|---|
| `macos-install-shell-audit` | 306 | §2.1.1, §2.2 |
| `macos-install-py-audit` | 320 | §2.1.2 |
| `macos-launcher-ux-audit` | (varies) | §2.1.3 |
| `macos-release-ci-audit` | 336 | §9 |
| `cross-os-triage` | 454 | §2.2, §6 |
| `v0252-case-mismatch-rootcause` | 707 | §6 (NEW-2) |
| `install-py-dedup` | 620 | §5.1 (DEDUP-1 through DEDUP-5) |
| `install-family-crossfile-dedup` | 203 | §2.1, §10.1 |
| `shell-scripts-dedup` | 493 | §2.1.1, §4, §5.2 (DEDUP-15) |
| `rust-installer-dedup` | 579 | §5.2, §8 |
| `vco-lib-python-dedup` | 413 | §7, §5.1 (DEDUP-6) |
| `project-bundle-install-audit` | 447 | §1.2, §2.1.5, §5.2, §6 |
| `linux-comprehensive-audit` | (varies) | §6 (L-P0-*) |
| `windows-comprehensive-audit` | (varies) | §6 (W-P1-*) |

### 14.2 Master plan

`.claude/context/plans/v0.2.53-master-plan-FINAL-2026-06-10.md` — section reference table:

| Master plan section | v2 doc section |
|---|---|
| §1A (P0 user-blocking bugs) | §6 ownership table |
| §1A-extension (Linux + Windows P0/P1) | §6 ownership table (L-* and W-* rows) |
| §1B (P1 strongly preferred) | §6 ownership table |
| §1C (Tier-1 LOW-risk dedup) | §5.1 |
| §1D (Tier-2 MEDIUM-risk dedup) | §5.2 |
| §1E (carry-overs) | §6 ownership table (V52-* and DC-* and D* rows) |
| §1F (case-mismatch + binary-lock bugs) | §6 ownership table (Track F rows) |
| §2 (Phase scoping) | §12 |
| §4 (v0.2.54 preview) | §3, §7, §8, §13 |
| §5 (Process improvements) | §9, §10 |

### 14.3 Schema files

Track B must create:
- `docs/schemas/install-bootstrap-envelope-v1.json` — JSON Schema for the envelope defined in §3.3.

Track D must create:
- `docs/schemas/dist-metadata-v1.json` — JSON Schema for the per-binary metadata.json defined in §4.4.

Both schemas use draft-2020-12. They are referenced by the parity tests in §10.

### 14.4 New script library files

Track A creates:
- `scripts/lib/asset-ref-count.sh`
- `scripts/lib/asset-ref-count.ps1`

These are the only NEW shared shell libraries in v0.2.53. The `launcher-candidates.{sh,ps1}` library (shell-scripts-dedup Finding 5) is deferred to v0.2.54.

### 14.5 New tests directory layout

```
tests/
├── test_install_sh_no_args.sh                          # Track A
├── test_macos_arm64_no_experimental_refs.py            # Track A
├── test_macos_linux_zip_download_filter.py             # Track A
├── test_post_install_launcher_no_local_outside_function.py  # Track A
├── test_apple_silicon_homebrew_path_probe.sh           # Track A
├── test_asset_ref_count_parity.py                      # Track A
├── test_launcher_dist_subdir_parity.py                 # Track A
├── test_command_finder_cwd.py                          # Track A
├── test_launcher_metadata_reader.sh                    # Track A
│
├── test_run_logged_subprocess.py                       # Track B
├── test_download_release_binary.py                     # Track B
├── test_make_deferral.py                               # Track B
├── test_launcher_db_reader_parity.py                   # Track B
├── test_atomic_write_cleanup.py                        # Track B
├── test_install_py_bootstrap_mode.py                   # Track B
├── test_bootstrap_envelope_schema.py                   # Track B
├── test_python_wheel_support_detection.py              # Track B
├── test_podman_machine_init.py                         # Track B
├── test_pip_subprocess_env.py                          # Track B
├── test_weaviate_health_endpoint_parity.py             # Track B
│
├── test_launcher_path_augmentation.rs                  # Track C (Rust)
├── test_install_gate_refresh_on_focus.spec.ts          # Track C (Svelte)
├── test_localstorage_cross_clone_scoping.spec.ts       # Track C
├── test_install_gate_run_installer_button.spec.ts      # Track C
├── test_python_candidate_parity.py                     # Track C
├── test_resume_sentinel_empty_sha_refusal.rs           # Track C
├── test_paired_sentinel_deferral.rs                    # Track C
├── test_update_deferred_roundtrip.rs                   # Track C
│
├── test_dist_metadata_schema.py                        # Track D
├── (CI workflows live under .github/workflows/)        # Track D
│
├── test_npm_omit_dev.py                                # Track E
├── test_svelte_parser.py                               # Track E
├── test_powershell_parser.py                           # Track E
├── test_update_event_toast.spec.ts                     # Track E
│
├── test_dev_collection_case_rebind.rs                  # Track F
├── test_project_bundle_resume_sentinel.py              # Track F
├── test_project_bundle_symlink_blocking.py             # Track F
├── test_fs_disable_contract_end_to_end.py              # Track F
├── test_canonical_class_prefix_parity.py               # Track F
│
├── test_distro_pkg_mgr_parity.py                       # Track G2
├── test_libwebkit2gtk_fallback.sh                      # Track G2
├── test_nvidia_container_toolkit_hint.py               # Track G2
│
├── test_selinux_z_flag.py                              # Track G3
├── test_linux_desktop_launch_path_augmentation.rs      # Track G3
├── test_kde_plasma6_desktop_launch.sh                  # Track G3
├── test_render_group_remediation.py                    # Track G3
│
├── test_first_install_bat_jsonl_writer.bat             # Track H
├── test_first_install_bat_refresh_env.bat              # Track H
├── test_powershell_compat.ps1                          # Track H
├── test_scheduled_task_xml_escape.py                   # Track H
│
└── test_jsonl_log_schema_parity.py                     # Tracks A + H + B (joint)
└── test_container_runtime_detection_parity.py          # Tracks C + G2 (joint)
```

---

## 15. Glossary

- **Shim**: a thin OS-language wrapper (bash, cmd.exe) that does just enough to invoke Python, then exits.
- **Envelope**: the JSON object returned by `install.py --bootstrap --json`.
- **Bootstrap**: the new read-only detection mode added to install.py.
- **Tier-1 dedup**: LOW-risk, mechanical extracts that land in v0.2.53.
- **Tier-2 dedup**: MEDIUM-risk extracts that land in v0.2.54.
- **Parity test**: a test that asserts two or more sites agree on a value/behavior.
- **Drift**: when two implementations of the same fact disagree.
- **Single source of truth (SSOT)**: the one canonical location for a piece of information; all consumers read from it.
- **Track**: a worktree-isolated implementation stream that runs in parallel with other tracks.
- **VCO**: VibeCoded Orchestrator (this project).
- **First-install shim**: `first-install.{sh,command,bat}` — the user-facing entry point on each OS.
- **Bug A**: the v0.2.51 sentinel pattern — paired sentinel + deferral writes that must be atomic.

---

*End of Track G design doc. Other tracks: read §6 for your ownership, §10 for your tests, §11 for risks affecting your area. Questions to the orchestrator/parent agent.*
