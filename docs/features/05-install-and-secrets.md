# Install & Secrets

From first clone to a running orchestrator: first-install shims (Linux / macOS / Windows), the Python installer with its new `--bootstrap` probe mode, container lifecycle, sanity checks, uninstall, and the `vct-secrets` primitive that keeps API keys out of `.env`. Code lives in `first-install.{sh,command,bat}`, `install.py`, `install.sh`, `install.ps1`, `uninstall.sh`, `scripts/post-install-launcher.{sh,ps1}`, `infrastructure/`, `scripts/`, `vco_lib/`, and `tools/vct-secrets/`.

For the install architecture design (why `--bootstrap` is additive and read-only, why the shims stay multi-language) see [INSTALL_ARCHITECTURE_v2.md](../INSTALL_ARCHITECTURE_v2.md).

---

## First-Install Shims (3-step sequence)

`first-install.{sh,command,bat}` are the user-facing entry points. Each is a thin Python-detect + dispatch wrapper that runs the same three-step sequence (`first-install.sh:1-106`, `first-install.command:1-131`, `first-install.bat:1-712`):

1. **Python detect** — OS-aware candidate cascade. macOS adds `/opt/homebrew/opt/python@3.13/bin/python3.13` + `/opt/homebrew/bin/python3.{13,12,11}` to PATH probes. Linux adds Linuxbrew (`/home/linuxbrew/.linuxbrew/bin/python3.{13,12,11}`). Windows uses the Python Launcher (`py -3.13` → `py -3.12` → `py -3.11` → `python.exe` → `python3.exe`). All shims verify `>= 3.11` via `python -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'`.
2. **Bootstrap prepass** — `"$PYTHON" install.py --bootstrap --json > state/logs/bootstrap-prepass.json 2>/dev/null || true`. Read-only system-detection probe. Best-effort: failure does NOT block the full install (`|| true` on POSIX; `goto :continue` on Windows). The envelope's schema is at `docs/schemas/install-bootstrap-envelope-v1.json` (schema_version=1, see [INSTALL_ARCHITECTURE_v2.md §3](../INSTALL_ARCHITECTURE_v2.md#3-installpy---bootstrap-mode)).
3. **Full install** — `"$PYTHON" install.py <forwarded args>`. The canonical 10-step flow. ARGS array preserves quoting; `--non-interactive` is translated to `--yes` (legacy alias). `--no-auto-launch` is intercepted by the shim and used to gate step 4.
4. **Launcher post-install (auto-spawn)** — `scripts/post-install-launcher.{sh,ps1}` is invoked when install succeeded AND `--no-auto-launch` was NOT passed AND the post-install script is executable. Soft-fail: a broken launcher spawn does not mask a successful install.

**Shim sizes**: `first-install.sh` 106 LoC, `first-install.command` 131 LoC, `first-install.bat` 712 LoC (the `.bat` inline-implements the `post-install-launcher.sh` logic — CMD batch cannot call the POSIX script). The shims are larger than a bare forwarder because they carry the 3-step sequence (prepass + install + launcher post-install) and the per-OS install-hint blocks. Argv passthrough is verbatim via `"$@"` (POSIX) / `%*` (cmd.exe).

### Bootstrap envelope at `state/logs/bootstrap-prepass.json`

The envelope is a system-detection JSON document conforming to `docs/schemas/install-bootstrap-envelope-v1.json` (schema_version=1). Fields cover OS / arch / RAM, Python / Node / npm / pnpm / Podman / Docker / Git / Brew / lean-ctx / claude CLI presence + versions, GPU detection (vendor / VRAM), Linux distro + pkg_mgr / Windows features / macOS features, resolved paths (install_root, venv_python, launcher_binary, vct_root_dir), per-OS package-manager advice, Weaviate / Ollama / code-embed / vct-hub endpoint URLs, missing_prereqs list, `ready_to_install` flag with `blocker_messages`.

Side-effect policy (`install.py:1523-1582`, `_run_bootstrap`):
- READ-ONLY by default. Spawns short read-only probes (`python3 --version`, `podman --version`, `nvidia-smi`, `getenforce`, `brew --prefix`, `podman machine list`). All probes timeout-bounded.
- Writes ONLY `state/logs/bootstrap-prepass.json` if `state/logs/` already exists. Does NOT create `state/` if absent — safe to call before install has ever run.
- No network calls; local TCP probes are best-effort and report failures in the envelope rather than blocking.
- `--bootstrap` is mutually exclusive with `--update`, `--lightweight`, `--uninstall`. Combining them returns exit 2 with a clear error. Unknown args are rejected with exit 2 (`install.py:1547-1552`).
- `--bootstrap --install-missing`: side-effectful. Runs `apt-get install -y <pkg>` / `brew install <pkg>` / `winget install --id <id>` flows for missing prereqs, plus `podman machine init` on macOS/Windows if Podman is installed but the daemon is unreachable. Re-detects after install. Returns exit 3 if any install command failed.

Exit codes: `0` detection succeeded; `1` exception (traceback on stderr); `2` bad invocation; `3` `--install-missing` failed.

### Doctor phase (`vco doctor`) — v0.2.91

The envelope above is a PRE-install snapshot. Until v0.2.91 nothing consumed it afterwards: `missing_prereqs` was computed, printed, and dropped, so an assumption that was false (or became false) stayed invisible. `vco_lib/doctor.py` closes that loop — ONE probe engine with three invocation points:

| Invocation | Scope | Where |
|---|---|---|
| End of every install/update | `full` | `install.py::_post_install_probe_phase` → `_run_doctor_phase` (runs on fresh installs too; the re-probe pass ahead of it stays `--update`-only) |
| Launcher boot | `boot` (cheap subset — no subprocess, no network) | launcher wiring |
| On demand | `full` | `vco doctor` / `python -m vco_lib.doctor` (`--json`, `--scope`, `--no-emit`, `--no-auto-fix`) |

It is deliberately **not a new detection codebase**: every probe composes a mechanism that already exists — the npx ladder (`vco_lib/npx_resolver.py`), the `--bootstrap` envelope's `missing_prereqs` (injected by install.py rather than re-derived), WP-A's binary-freshness probe via its Python leg, the deferral registry's own clear probes, and `vco verify-pins`' row collector — with ONE deliberate exception, `disk_space` (v0.2.91), which measures a resource nothing in VCO was watching at all. The exception is bounded on purpose: one `shutil.disk_usage` call per distinct filesystem across the install root and the vct state dir (`vco_lib.paths`), no new subsystem.

Each finding is `ok` / `problem` / `unknown`, and `unknown` never counts as `ok` — a probe that could not run must not read as "all good". Exit code is 1 only when a probe reports a real problem.

**Fix boundary** (§F decision #4): environment-level owed WORK may be re-attempted automatically (the WP-H retry dispatcher re-runs an owed KG seed or code-graph walk once its backend answers — work the user already asked for by installing, idempotent, precondition-gated, attempt-capped). Everything else is surface-only: findings that touch a RUNNING binary are reported, never repaired, per the standing no-auto-restart ruling. The hub's own boot auto-restart (`running_hub_is_stale`) predates that ruling, is the hub's documented contract, and stays grandfathered — the doctor itself restarts nothing.

Findings it defers become registry-classed deferral conditions with the exact remediation command (`npx_missing_mcp_unspawnable`, `disk_space_low`). When install.py drives the phase it passes its in-flight run report as the sink, so those entries ride the run's single authoritative write instead of a second writer behind `finalize()`'s back.

#### Disk-space probe

The doctor probes free disk space on **two** locations — the install root and the vct state dir (`$VCT_STATE_DIR`, else `~/.vct` via `vco_lib.paths.vct_root_dir`) — because they are frequently on different filesystems and either one filling up breaks a different half of the system: the install root starves clones, venvs and dist binaries; the state dir starves `launcher.db`, the hub's lockfiles, RL archives and logs. The two are deduplicated by `st_dev` (falling back to the resolved path string when `stat` fails), so a single-filesystem install reports one mount rather than the same mount twice.

It runs in **both** scopes — `boot` as well as `full` — so launcher boot, the end of every install/update, and `vco doctor` all check it. The dedupe happens *before* the measurement, not after, so the cost is one `shutil.disk_usage` call per distinct filesystem: two on a split install, one when the clone and the state dir share a filesystem. Deliberately cheap enough for the boot path.

| Free space | Severity |
|---|---|
| ≥ floor | `ok` |
| < floor (default **2 GiB**, override `VCT_DISK_SPACE_MIN_FREE_GB`) | warning |
| < 256 MiB | critical |

The comparison is strict: free space exactly *at* the floor is `ok`. A malformed or non-positive `VCT_DISK_SPACE_MIN_FREE_GB` falls back to the 2 GiB default rather than disabling the check (the same policy as `VCO_CG_INJECT_CAP`); fractional values such as `0.5` are legal.

Below the floor the probe emits the `disk_space_low` deferral condition through the locked emitter (`vco_lib.deferral_emit`, never a raw report write), classed **environmental** — a fact about the machine rather than a task — so it renders in the ledger panel's "Records / by-design" group and does not badge. Its `command_to_apply` names `df -h` on the affected mounts, VCO's own reclaimable space (podman prune, `~/.vct/logs`, unused Ollama models), and the env knob. It reaches the user on both surfaces the deferral system already owns: the session-start deferral surface (so Claude sees it at the top of a session, before starting a walk that will fail on write) and the launcher's deferral-ledger panel.

It **clears itself** on the next VCO run whose space check comes back above the floor — the boot doctor, `vco doctor`, the install/update re-probe pass, or a bundle update. Nothing to dismiss by hand. The doctor-side self-resolve exists because the boot doctor does *not* run the re-probe pass (`run_boot_doctor_and_retries` invokes `--scope boot --json`, which only emits), so a re-probe-only clear would have left the entry standing until the user's next `--update`. The one deliberate exception is install.py's sink path: a resolve landing while `InstallDeferralFlow.finalize()` is pending would be resurrected by that run's own write, so the self-resolve stands down there and the `--update` re-probe pass covers it.

A path the probe cannot **measure** (permission denied, IO error, an unstat-able path) reports `unknown`, never `ok` — the same tri-state rule as every other doctor probe. A not-yet-created state dir is not that case: it is measured on its nearest existing ancestor, whose filesystem is the one that would hold it, so a first run gets a real reading.

---

## Bootstrapper Scripts

Two thin shell wrappers (`install.sh` for POSIX, `install.ps1` for PowerShell) probe for a usable Python interpreter, then exec `python install.py "$@"`. The Python installer is the actual logic; the shells exist only because users run `./install.sh`, not `python -c "import urllib.request..."`.

### `install.sh` — Linux/macOS entry point
Probes `python3.13`, `python3.12`, `python3.11`, `python3`, `python` in order, verifies major≥3 AND minor≥11 using a Python-2-safe version probe (no f-strings to survive stray Python 2 on PATH), then delegates to `python install.py "$@"`. Prints per-distro install hints if no suitable Python found.

### `install.ps1` — Windows PowerShell bootstrapper
PowerShell 5.1+ script with the same interpreter probe (`python3.12`, …, `py -3.13/-3.12/-3.11` via Windows `py` launcher). Maps all `install.py` flags to PowerShell switch/string parameters.

<details>
<summary>Details</summary>

`install.ps1` supports the flags the Python installer accepts: `-NoContainers`, `-Gpu`, `-CpuOnly`, `-LowResource`, `-OpenaiKey`, `-Container`, `-Dev`, `-Update`, `-SkipModels`, `-Quiet`, `-NoAgents`, `-NoSkills`. The Windows `py` launcher with version pinning (`py -3.12`) is tried as a secondary probe to handle Windows Store Python stubs.

> The `-WithMaoAgents` switch is also present in `install.ps1` and forwards `--with-mao-agents` to `install.py`, but `install.py` doesn't define that flag and `templates/agents/mao/` doesn't exist in the OSS bundle — the switch is effectively a no-op until those land. Tracked as a code-doc gap, not a documented flag.
>

</details>

### Python version guard
Both `install.sh` and `install.py` enforce Python ≥ 3.11 (`MIN_PYTHON = (3, 11)` in `_check_python_version()`). On failure it prints OS-specific install hints (apt / dnf / Homebrew / winget).

### `venv` / `ensurepip` soft-prereq check
`_check_prerequisites()` probes `import venv` and `import ensurepip` before creating the venv. On Debian/Ubuntu these are split packages; the function warns and prints the apt fix rather than aborting.

---

## Install Flags

### `--gpu` / `--cpu-only` / `--low-resource`
Three embedding-mode overrides. `--gpu` forces CodeSage-Large-v2 (GPU) for code and qwen3-embedding for text. `--cpu-only` forces both via Ollama. `--low-resource` selects snowflake-arctic-embed2 text + Jina V2 code — both via Ollama. Without any flag: NVIDIA GPU detected → `gpu`; else → `cpu`.

### `--openai-key KEY`
Switches the entire embedding stack to OpenAI `text-embedding-3-small` (1536-dim). Skips GPU detection. Key written into `.env` as `OPENAI_API_KEY`.

### `--no-containers`
Skips all Docker/Podman service setup. Useful for CI or when services are managed externally. `.env` is still written; agents/skills are still installed.

### `--container docker|podman`
Forces a specific container runtime instead of auto-detecting (Linux prefers Podman; macOS/Windows prefers Docker).

### `--skip-models`
Skips pulling Ollama models after containers start. Models can be pulled manually later via `ollama pull`.

### `--dev`
Installs `requirements-dev.txt` in addition to `requirements.txt` inside the venv.

### `--update`
Re-installs deps and restarts services, but skips `.env` and `settings.json` creation (preserves user edits).

### `--with-agents` / `--no-agents`
Default-on. Copies bundled agent templates from `templates/agents/free/` into `~/.claude/agents/`. Already-present files are skipped (idempotent).

### `--with-skills` / `--no-skills`
Default-on. Copies skill directories from `templates/skills/` into `~/.claude/skills/`. `.md` files get placeholder substitution; other files are copied raw.

### `--no-compile`
Skips Step 11b (bytecode pre-compile of orchestrator Python modules). Default is to run `python -m compileall` against `VCThelpers/`, `claude_mcp_servers/`, `tools/`, `vco_lib/`, and `.claude/scripts/` so first-import is ~50-200ms faster per cold module. Best-effort: per-directory failures warn but never abort. Cross-OS via stdlib `compileall`. Skip in dev/CI runs where the speedup doesn't matter.

### `--telemetry on|off`
Explicit telemetry consent for the generated `.env`. Default is prompt-on-TTY; non-interactive defaults to `off`. The generated `.env` always contains an explicit `VIBECODED_TELEMETRY=true|false` line so consent state is auditable.

### `--yes`
Non-interactive mode: accept all defaults (telemetry=off, confirm all uninstall prompts).

### `--quiet`
Minimal output. Also suppresses interactive optional-companion prompts (e.g. lean-ctx detection).

### `--uninstall`
Switches the installer to uninstall mode. Pairs with `--keep-data`, `--remove-projects`, `--dry-run`.

---

## Installation Steps

### Step ordering (1/10 … 10/10)
install.py runs 10 numbered steps: (1) Python version, (2) system detection, (2b) optional companions, (3) venv creation, (4) dependency install (pip + editable `vco` CLI and `weaviate_mcp`), (5) container services, (5b) orchestrator-root `.claude/` install via the ONE bundle engine (`install-bundle`), (6) Ollama wait, (7) model pull, (7b) Weaviate collection bootstrap, (8) `state/` creation + `vct-hub` deploy/start, (9) configuration write, (10) Claude CLI check. Followed by Step 11 (initial code graph analysis) and an optional Step 11b (bytecode pre-compile, opt-out via `--no-compile`).

### Idempotency
Each step checks before acting. Venv creation is skipped if `.venv/bin/python` exists. `.env` write is skipped if `.env` already exists. Agents/skills skip files already present. Weaviate collection creation is skipped per-collection if it already exists (tolerates 422 "already exists" race). Container start probes service ports first and skips services already running.

### Placeholder substitution in agent/skill templates
`_install_agents_and_skills()` replaces `{{ORCHESTRATOR_ROOT}}`, `{{PROJECTS_ROOT}}`, and `{{HOME}}` in all `.md` files before copying. This embeds absolute paths into agent definitions at install time so they work regardless of how the CLI invokes them.

### `.env` generation
`_write_env_config()` writes a fully-populated `.env` including `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `CODE_EMBED_BACKEND`, `CODE_EMBED_DIMS`, `KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `VIBECODED_TELEMETRY`, and (if `--openai-key` was given) `OPENAI_API_KEY`. File is only written if `.env` does not already exist.

### `.claude/settings.json` generation
`_configure_claude_settings()` creates `.claude/settings.json` with base `permissions.allow` rules and an `env` block that injects all service URLs and embedding config into every Claude Code session in this project folder. Skipped if the file already exists.

---

## Container Start

### Shared-service reuse
Before running `compose up -d`, install.py probes `http://localhost:<port>/v1/.well-known/ready` (Weaviate), `/api/tags` (Ollama), and `/health` (code_embed) with a 2s timeout. Services already up are reused; only missing ones are started. Multiple installs on the same machine share one Weaviate / Ollama; isolation is by KG collection namespace, not separate containers.

### `VCT_FORCE_SEPARATE_CONTAINERS=1` escape hatch
Bypasses the service-reuse check and runs `compose up -d` for everything. The caller is responsible for setting `WEAVIATE_PORT` / `OLLAMA_PORT` / `CODE_EMBED_PORT` to avoid bind conflicts.

### compose command resolution
`_get_compose_command()` tries `podman-compose` → `podman compose` → falls back on Podman. For Docker it tries `docker compose` (v2 plugin) → `docker-compose` (standalone).

### GPU overlay
When `--gpu` / NVIDIA GPU detected, `docker-compose.gpu.yml` is passed as a second `-f` overlay and `--profile gpu` is added. Adds NVIDIA device reservations to both `ollama` and `code_embed` services.

### 15-minute container start timeout
`compose up -d` has a 900s subprocess timeout. On failure, stderr is scanned for "cannot connect" / "daemon" (→ systemd/Desktop hint) and "address already in use" / "bind" (→ VCT_FORCE_SEPARATE_CONTAINERS hint).

### Ollama health wait
`_wait_for_ollama()` polls `/api/tags` every 2s for up to `HEALTH_TIMEOUT=120s`. Timeout is non-fatal — installer continues and prints a warning.

### Model pull
`_pull_ollama_models()` POSTs to `/api/pull` for each model in the selected embedding config's `ollama_models` list. Pull failures are WARN-level rather than fatal. 600s per-model timeout.

### Weaviate collection bootstrap
`_ensure_collections()` reads the existing schema, then POSTs only the missing collections (`KG_COLLECTION` and `DEVELOPMENT_COLLECTION`). Handles 422 "already exists" race gracefully. Code-graph collections are excluded — they are shared machine-wide and created lazily by the MCP server on first write.

---

## Existing Volume Detection

### Volume probe on start
`_detect_existing_volume_paths()` calls `<runtime> volume inspect <name>` (read-only) for all canonical and historical volume names. Prints mountpoints and sizes (best-effort `du -sk`). When found, no bind-mount override is generated.

### Destructive-op firewall
`install.py` never shells out to `volume rm` or `compose down --volumes`. The uninstaller prints the equivalent commands as manual steps but does not execute them. The Launcher's `migrate_volumes` function is the only allowed caller of `volume rm`.

---

## Sanity Checks

Three scripts at three different levels of cost. `check-install.sh` runs in seconds with no network or containers (CI default). `test-install.sh` does an end-to-end install in a clean Ubuntu container (~3-5 min, opt-in). `check-no-secrets.sh` is a pre-commit gate against historically-leaked tokens.

### `scripts/check-install.sh` — static CI gate
Runs without network or containers. Checks: `bash -n install.sh` syntax, `shellcheck` (optional), `python3 -m py_compile install.py`, `install.py --help` exit 0, PowerShell parse via `pwsh` (optional), `pip install --dry-run -r requirements.txt` resolver check, scan for hardcoded personal paths, scan for stale private-module refs, and image-tag pinning in compose files (warns on `:latest` but only fails on non-intentional tags).

### `scripts/test-install.sh` — container-based smoke test
Opt-in, requires Docker or Podman, takes ~3-5 minutes. Launches a clean `ubuntu:22.04` container, copies the repo in, runs `install.py --no-containers --skip-models --no-agents --no-skills`, then asserts: `.venv/bin/python` exists, `pip list` works, `.env` was created. Supports `RUNTIME=docker` and `IMAGE=ubuntu:24.04` overrides.

### `scripts/check-no-secrets.sh` — pre-commit token blocklist
Scans the git-tracked tree for historically-leaked secrets. Wire as a pre-commit hook: `ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit`. Supports `--staged` and `--all` modes. Exits non-zero with instructions to replace with placeholders if any match is found.

---

## Uninstall

### `uninstall.sh` — shell wrapper
Same Python interpreter probe as `install.sh`, then `exec python install.py --uninstall "$@"`. Passes through `--keep-data`, `--remove-projects`, `--dry-run`, `--yes`.

### `python install.py --uninstall`
Five categories, each confirmed separately (or auto-yes with `--yes`): (1) stop containers via `compose down`, (2) print volume cleanup commands manually (never executes `volume rm` itself), (3) remove `~/.vct/launcher.db`, (4) scrub orchestrator MCP entries from `~/.claude.json` while preserving user's other MCP servers, (5) per-project `.claude/` removal (opt-in, `--remove-projects`).

### `--dry-run` for uninstall
Prints the full plan and exits without touching anything.

### `--keep-data`
Suppresses the volume cleanup step entirely. KG data and Ollama models are preserved.

### `~/.vct-secrets/` is never touched
Both the installer and uninstaller explicitly state this. User's GitHub PAT, Supabase keys, etc. survive uninstall.

### Selective MCP entry removal
The uninstaller removes exactly: `weaviate-kg`, `ollama`, `search`, `code-embedding`, and VCO-registered Pro-tier MCPs from `~/.claude.json`, leaving any user-added servers intact.

---

## vct-secrets CLI

The point of `vct-secrets` is that secrets stop ending up in `.env` files, in `ps` output, in shell history, and in `~/.claude.json`. They live as 600-mode files under `~/.vct-secrets/`, and they reach the consuming process via `exec env -i KEY=value CMD` rather than via the parent environment.

### Overview
Single Bash script at `tools/vct-secrets/vct` (~600 lines, zero deps beyond bash 4+ and GNU coreutils). Stores secrets in `~/.vct-secrets/` with a two-tier layout: `shared/<key>` for cross-project secrets, `projects/<NAME>/<key>` for project-scoped overrides. Resolution is project-first, then shared.

<details>
<summary>Details</summary>

Phase 1 is Bash for auditability and zero-dependency deployment. Phase 2 will replace the script with a Rust binary distributed by the VCT Launcher. The on-disk layout (`~/.vct-secrets/`) and CLI contract are stable — all consumers (git credential helper, MCP wrappers, hooks) keep working through the transition.

</details>

### `vct list [--project NAME]`
Lists secret key names (never values) for a project or for `shared/` if no `--project` given.

### `vct set --project NAME --key KEY`
Reads the secret value from stdin only — `--value` is explicitly forbidden to prevent shell-history capture. Refuses interactive TTY stdin unless `--confirm-tty` is given. Writes atomically: `mktemp` → `cat > tmp` → `mv tmp target` → `chmod 600`. Appends a `set` entry to `audit.log`.

### `vct get --project NAME --key KEY [--trusted]`
Prints secret value to stdout. Refuses when stdout is a TTY unless `--trusted` is given — a tripwire against accidental terminal scrollback / clipboard capture. Prefer `vct exec` for most use cases.

### `vct exec --project NAME --secret KEY[=VAR_NAME]... -- CMD [ARGS...]`
Resolves secrets, validates all exist before running anything (fail-fast), then `exec env -i <safe-env> KEY=value CMD`. Secrets never appear in `ps` output, shell history, or `set -x` traces.

<details>
<summary>Details</summary>

`KEY=VAR_NAME` syntax lets you rename the secret when injecting: `--secret github_pat=GITHUB_TOKEN` injects the value of `github_pat` as `$GITHUB_TOKEN`. Multiple `--secret` flags are allowed. All secrets are resolved up-front before `exec`; if any are missing the command is not run (exit 2). Without `--preserve-env`, only a minimal safe env (`PATH HOME USER LOGNAME TERM LANG SHELL PWD TMPDIR`) is carried through.

</details>

### `vct revoke --project NAME --key KEY [--yes]`
Deletes the secret file for a project. Prompts for confirmation unless `--yes`. Returns exit 2 if not found.

### `vct copy --from-project SRC --to-project DST --key KEY [--yes]`
Explicit cross-project copy with confirmation prompt. Preserves `chmod 600`. Audits as `copy` op.

### `vct migrate-from-env FILE --project NAME [--dry-run]`
Parses a `.env` file (KEY=VALUE lines, `#` comments, quoted values, optional `export` prefix). Stores each valid key atomically. Dry-run shows what would import. On success, renames the source file to `FILE.migrated` to prevent accidental reuse.

### `vct doctor`
Audits `~/.vct-secrets/` permissions. Fixes any directory with mode ≠ 700 and any secret file with mode ≠ 600. Never reads secret contents.

### `vct detect-project`
Walks up from `$PWD` looking for a `.vct-project` file. Prints its first line (the project name). Used by the git credential helper and other consumers to auto-scope secrets.

### Key / project name validation
All key and project names are validated against `[A-Za-z0-9_.-]`, max 128 chars. Path-traversal patterns (`..`, `/`, leading `-`, control chars) are rejected at the `validate_name()` call site.

### `umask 077`
Set at script top-level. Ensures all files created by `vct` are created with restrictive permissions by default.

### Audit log format
Append-only JSONL at `~/.vct-secrets/audit.log` (mode 600). Each line: `{"ts":"…ISO8601…","op":"set|get|exec|revoke|copy","project":"…","secrets":[…],"caller_pid":12345,"caller_cmd":"…"}`. Secret values are never written; only key names are logged.

---

## Secrets Architecture

### `~/.vct-secrets/` directory layout
```
~/.vct-secrets/          (mode 700)
├── vct                  (mode 755) — CLI
├── git-credential-vct   (mode 755) — git credential helper
├── audit.log            (mode 600) — append-only JSONL
├── shared/              (mode 700)
│   └── github_pat       (mode 600)
└── projects/
    └── <NAME>/          (mode 700)
        └── <key>        (mode 600)
```

### Project-first resolution order
1. `~/.vct-secrets/projects/<NAME>/<key>` (project-scoped)
2. `~/.vct-secrets/shared/<key>` (shared fallback)
3. Exit 2 — no leak to environment

### Secrets never on argv or in `.env` files
The `vct exec` model ensures secrets are injected as env vars at process exec time. The search MCP wrapper and git credential helper read secrets at runtime from the store, not from `~/.claude.json` or any checked-in file.

### `VCT_SECRETS_DIR` override
Set this env var to relocate the secrets root. Default: `~/.vct-secrets`. Useful for testing and multi-user setups.

### `.vct-project` marker file
A file placed at the project root (one line = project name) used by `vct detect-project` and the git credential helper to auto-scope to the correct project's secrets without requiring explicit `--project` flags.

---

## Search MCP Wrapper + Git Credential Helper

### Search MCP wrapper (`claude_mcp_servers/search_mcp/wrapper.sh`)
**Interpreter resolution (post-`0541dcf7`)**: the wrapper probes, in order, `$SEARCH_MCP_PYTHON` → `$REPO_ROOT/claude_mcp_servers/.venv/bin/python` (the legacy pre-unification layout) → `$REPO_ROOT/.venv/bin/python` (canonical since the venv unification), and only then falls back to the legacy path for its error message. Before that fix it hardcoded the legacy path alone, so on a root-venv install the MCP simply never started — `claude mcp list` said "Failed to connect" with no visible cause. The lesson generalises beyond this wrapper: a layout fact copied into a script is an assumption frozen at authoring time, and nothing re-verified it when the layout moved. v0.2.91's doctor phase (`vco doctor`) exists to be the thing that re-verifies such assumptions after install — the same probe engine that catches an unresolvable `npx`, a stale launcher binary, and drifted npm pins.

Two-stage `GITHUB_TOKEN` resolution as of v0.1.7: (1) env-first — `$GITHUB_TOKEN` already exported by the launcher's `write_project_env_files` (which sources from the keychain); (2) resolver helper — `vct_secrets_resolve.sh <project_path> github_pat`, which calls the launcher's hub HTTP API (`GET /api/v1/projects/{id}/env?key=github_pat`). The legacy `~/.vct-secrets/shared/github_pat` file fallback (gated behind `VCT_LEGACY_FILE_FALLBACK=1`) was removed in the 0.1.7 fork-readiness sweep. Either way the token never appears in `~/.claude.json`.

### `git-credential-vct` — GitHub credential helper
At `tools/vct-secrets/git-credential-vct`. Registered via `git config --global credential.https://github.com.helper '!<path>'`. Only responds to the `get` operation (never stores). Resolution order for `github_pat`: (1) walk `$PWD` upward for `.vct-project` → use `projects/<name>/github_pat`; (2) fallback to `VCT_PROJECT_ROOT_PATTERN/<segment>/` heuristic; (3) `shared/github_pat`. Refuses to read files with perms other than 600 or 400.

---

## Migration Helpers

### `migrate-shared.sh` — flat → structured migration
Moves legacy flat `~/.vct-secrets/<key>` files (from pre-v0.1 layout) into `~/.vct-secrets/shared/<key>`. Refuses if destination already exists. Supports `--dry-run`.

### `tools/vct-secrets/MIGRATION.md`
Step-by-step guide for manual migration from flat secrets to the structured layout, plus the upgrade path from `.env` files.

---

## Infrastructure / Compose

### `infrastructure/docker-compose.yml` — base services
Three services: `weaviate` (pinned `cr.weaviate.io/semitechnologies/weaviate:1.28.4`, ports `8081:8080` + `50052:50051`, named volume `weaviate_data`, healthcheck via `wget --spider`), `ollama` (pinned `docker.io/ollama/ollama:0.20.2`, port `11435:11434`, named volume `ollama_data`, env `OLLAMA_ORIGINS=*`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`), and `code_embed` (custom-built from `claude_mcp_servers/code_embedding_service`, port `11440`, volume `code_embed_cache`, profile `gpu` — only started with `--profile gpu`).

<details>
<summary>Details</summary>

All three services use named volumes with default container-engine paths. Port numbers are configurable via env vars (`WEAVIATE_PORT`, `WEAVIATE_GRPC_PORT`, `OLLAMA_PORT`, `CODE_EMBED_PORT`). Image tags: Weaviate and ollama are pinned for supply-chain reproducibility. `check-install.sh` warns on any `:latest` tags it finds in compose files.

</details>

### `infrastructure/docker-compose.gpu.yml` — GPU overlay
Compose override that adds NVIDIA device reservations to both `ollama` and `code_embed` services. Applied with `-f docker-compose.gpu.yml --profile gpu`. Requires NVIDIA Container Toolkit.

### Port assignments (defaults)

| Service | Host port | Container port |
|---|---|---|
| Weaviate HTTP | 8081 | 8080 |
| Weaviate gRPC | 50052 | 50051 |
| Ollama | 11435 | 11434 |
| code_embed | 11440 | 11440 |

---

## Environment Configuration

### `.env` generated by installer
The repo does not ship a top-level `.env.example`. `install.py::_write_env_config` writes a fully-populated `.env` at install time covering: infrastructure URLs (`WEAVIATE_URL`, `OLLAMA_URL`, `GRPC_PORT`, `CODE_EMBED_SERVICE_URL`), KG collection names (`KG_COLLECTION`, `SHARED_KG_COLLECTION`), embedding settings (`ACTIVE_EMBEDDING`, `EMBEDDING_MODEL`, `CODE_EMBED_BACKEND`, `CODE_EMBED_DIMS`), telemetry opt-in (`VIBECODED_TELEMETRY=false`), and OpenAI key (only if `--openai-key`). The launcher subtree ships its own `launcher/.env.example` for the SvelteKit / Supabase auth client.

### `VIBECODED_TELEMETRY` env var
Explicit opt-in flag (default `false`). Written by `install.py` based on the telemetry consent prompt.

### `ACTIVE_EMBEDDING` env var
Controls which named vector is used for KG searches. Recognized values in `claude_mcp_servers/weaviate_mcp/server.py`: `qwen3` (default → `qwen3_embed` slot), `openai` (→ `openai_embed`), and any other value (treated as legacy → `ollama_embed` for KG / `ollama_code_embed` for code). For the `--low-resource` install profile `install.py` writes `ACTIVE_EMBEDDING=arctic`; the MCP's branch-by-negation mapping routes it to the legacy slot. Switch without reindexing as long as the target slot is populated.

---

## Requirements

### `requirements.txt` — production dependencies
Pinned floors: `mcp>=1.0`, `fastmcp>=0.1`, `weaviate-client>=4.9`, `aiohttp>=3.9`, `httpx>=0.27`, `requests>=2.31`, `pydantic>=2.0`, `pyyaml>=6.0`, `watchdog>=4.0`, `asyncpg>=0.29`, `ollama>=0.3`, `sentence-transformers>=3.0`, `transformers>=4.40.0,<5.0.0` (capped for CodeSage Conv1D compatibility), `torch>=2.0`, `fastapi>=0.111`, `uvicorn>=0.30`, `psutil>=5.9`. All licenses MIT, Apache 2.0, or BSD-3-Clause.

---

## BOOTSTRAP.md — Install Playbook

### Path A vs Path B
`BOOTSTRAP.md` is the first file read by AI assistants opening the repo. Cleanly separates "you came from VCT Launcher" (Path A — services already running, nothing to do) from "you cloned from GitHub" (Path B — manual install steps).

### Troubleshooting table
Documents six common failure modes with causes and fixes: `hybrid_search` returns nothing, hooks don't fire (`VCT_DISABLE_HOOKS=1`), search MCP GitHub errors, code-graph returns nothing (run analyze first), slow/missing Ollama models, container runtime not detected.

### vct-secrets manual setup snippet
BOOTSTRAP.md shows the exact commands to set up `~/.vct-secrets/` manually (for Path B users): `mkdir -p`, `chmod 700`, `cp vct`, `chmod 755`, `echo "ghp_…" | vct set --project SHARED --key github_pat`.

---

## vct-secrets Tests

### `tools/vct-secrets/tests/test_vct.sh`
18 tests covering: set/get/exec/revoke/copy round-trips, `migrate-from-env` with quoted values and comments, `doctor` permission fixing, and permission enforcement (refuses TTY get without `--trusted`, refuses set via argv). Run with `bash tools/vct-secrets/tests/test_vct.sh`.
