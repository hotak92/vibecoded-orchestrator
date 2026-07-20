<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->

# Install Architecture

How VibeCoded Orchestrator installs itself and configures projects. This is the
reference for the install *machinery*; the user-facing walkthrough lives in
[`GETTING_STARTED.md`](GETTING_STARTED.md), failure diagnosis in
[`INSTALL_RECOVERY.md`](INSTALL_RECOVERY.md), and the root/project install
parity contract in [`INSTALL_PARITY.md`](INSTALL_PARITY.md).

---

## 1. Overview — three layers

```
┌──────────────────────────────────────────────────────────────────┐
│ first-install.{sh,command,bat}    — thin OS shims                │
│   Python detect → bootstrap prepass → full install → launcher    │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ install.py                        — the install orchestrator     │
│   --bootstrap: read-only system-detection probe (JSON envelope)  │
│   default: the 10-step install (venv, containers, models,        │
│   collections, MCP registration, hub, launcher binaries)         │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ vco_lib.project_init install-bundle — the ONE bundle engine      │
│   installs/updates every `.claude/` bundle: user projects AND    │
│   the orchestrator root itself (via vco_lib/self_install.py)     │
└──────────────────────────────────────────────────────────────────┘
```

Design principles:

- **Python is the single source of truth for OS-aware facts** (paths, binary
  locations, package-manager advice). Shell and Rust consumers read them from
  the versioned bootstrap JSON envelope instead of re-deriving them — same
  fact, one implementation, parity-tested.
- **Shims stay multi-language and thin.** Detecting Python cannot itself be
  done in Python (chicken-and-egg), and `start-launcher.*` must work even when
  `.venv/` is corrupt — so those stay autonomous shell/BAT with parity tests
  guarding against drift.
- **One bundle engine.** Every installer of `.claude/` content — launcher
  add-project, launcher update, and `install.py` installing the orchestrator
  root into itself — is a subprocess client of the same
  `install-bundle` CLI with the same argv shape and the same `--json` stdout
  contract. See §5.

---

## 2. Entry-point shims

`first-install.sh` (Linux), `first-install.command` (macOS), and
`first-install.bat` (Windows) each run the same three-step sequence,
forwarding every user-supplied flag to `install.py` verbatim
(`"$@"` on POSIX, `%*` in cmd.exe):

1. **Python detect** — OS-aware candidate cascade, each candidate
   version-probed (`>= 3.11` required). macOS tries Apple Silicon Homebrew
   paths first, then PATH; Linux tries PATH then Linuxbrew; Windows uses the
   Python Launcher (`py -3.13` …) then PATH. If none qualifies, the shim
   prints the distro-specific install hint and (interactively) offers to
   install via the platform package manager.
2. **Bootstrap prepass** — `install.py --bootstrap --json` writes the
   system-detection envelope to `state/logs/bootstrap-prepass.json` (§3).
   Best-effort: a prepass failure is logged and the full install still runs.
3. **Full install** — `install.py <forwarded args>` runs the 10-step flow
   (§4).

On `install.py` exit 0 (and unless `--no-auto-launch` was passed) the shim
runs `scripts/post-install-launcher.sh` (or the inline equivalent in
`first-install.bat`), which probes for a launcher binary, downloads the
prebuilt one from GitHub Releases if absent or offers to build from source,
strips `com.apple.quarantine` on macOS, and spawns the launcher GUI detached.

Flags the shims themselves consume: `--no-auto-launch` and
`--non-interactive` (translated to `install.py --yes`). Everything else is
forwarded.

---

## 3. `install.py --bootstrap` mode

A read-only system-detection probe, exclusive with
`--update` / `--lightweight` / `--uninstall`.

```
install.py --bootstrap [--json] [--install-missing] [--no-prompt]
```

**Side-effect policy** (without `--install-missing`):

- Reads files and runs short read-only probe subprocesses
  (`python3 --version`, `podman machine list`, `nvidia-smi`, `getenforce`, …),
  every probe timeout-bounded.
- Writes nothing except an append to `state/logs/install.jsonl` — and only
  if `state/logs/` already exists. Bootstrap never creates `state/`, never
  writes `.env`, never touches `~/.claude.json`, never pulls models.
- No network calls. Local TCP probes (Weaviate readiness etc.) are
  best-effort; failures are reported in the envelope, never blocking.
- Never prompts.

**With `--install-missing`**: runs the package-manager install flows
(apt/dnf/brew/winget) for missing prereqs, plus `podman machine init` +
`podman machine start` on macOS/Windows when the Podman binary is present but
the daemon is not running; then re-detects so the final envelope reflects
post-install state. Prompts once before installing unless `--no-prompt`.

**Exit codes**: `0` detection completed (check `ready_to_install` in the
envelope — exit 0 does NOT mean all prereqs are present); `1` detection
crashed; `2` bad invocation (e.g. combined with `--update`); `3` an
`--install-missing` remediation command failed.

**The envelope** (`schema_version: 1`, schema at
[`docs/schemas/install-bootstrap-envelope-v1.json`](schemas/install-bootstrap-envelope-v1.json)):

- `system.*` — OS/arch/RAM, per-tool `{cmd, version, ok}` blocks for
  Python/Node/npm/pnpm/Podman/Docker/git/brew/lean-ctx/Claude CLI,
  `gpu` (vendor/VRAM/driver/container-toolkit), `linux_distro` /
  `windows_features` / `macos_features`. `system.python.wheel_support_ok`
  reports whether pip can satisfy the dependency set with wheels only
  (a dry-run `pip install --only-binary=:all:` probe).
- `paths.*` — install root + kind (`orchestrator_clone` /
  `completed_install` / `git_repo`), venv pythons, `launcher_dist_subdir`
  (the single source of truth for the per-OS binary directory:
  `macos-arm64` / `linux-x64` / `windows-x64`), binary existence flags,
  state dir, `~/.vct` paths.
- `package_manager_advice.*` — primary package manager, per-tool install
  commands, Linux Tauri build deps, `selinux_volume_flag_needed` (Enforcing
  SELinux → `:Z` bind-mount flag), NVIDIA container-toolkit hint.
- `weaviate_endpoints` / `ollama_endpoints` / `code_embed_endpoints` /
  `vct_hub_endpoints` — canonical service URLs (Weaviate health is
  `/v1/.well-known/ready`).
- `missing_prereqs[]` — `{name, human, severity, install_hint}` with
  severity `blocking` / `warning` / `optional`; `ready_to_install` is true
  iff no blocking entries; `blocker_messages` / `warnings`.

The envelope is additive within a schema version: consumers ignore unknown
keys and must refuse (fall back gracefully) on a `schema_version` they don't
recognise. The envelope contains no secrets by construction, plus a
defense-in-depth scrub pass before emit.

---

## 4. The full install flow

`install.py` (no mode flag) runs 10 steps, each logged to
`state/logs/install.jsonl` with step IDs `1/10` … `10/10` (sub-steps carry a
letter suffix, e.g. `5b/10`):

1. Python version check
2. System detection (hardware, GPU vendor, embedding-backend choice; `2b` —
   optional companions like lean-ctx)
3. Venv creation
4. Dependency install (editable installs of the `vco` CLI + MCP servers)
5. Containers (Weaviate, Ollama, code-embed) — probe/adopt/alt-port conflict
   handling, then compose-up. **Step 5b** installs the orchestrator root's
   own `.claude/` via the ONE bundle engine (§5)
6. Ollama model pulls
7. Weaviate collections (`7b` bootstrap) + KG seeding (`7c`)
8. vct-hub binary deployment + hub start
9. `.env` and config writes
10. Claude CLI check

Alongside the numbered steps, `install.py` registers the bundled MCP servers
into `~/.claude.json` (soft-fail — the install completes even if registration
fails; opt out with `--skip-mcp-registration`) and deploys/refreshes the
launcher binary.

**Resume**: on re-run, install.py reads the log and skips steps whose latest
phase in the most-recent session is `ok`/`skip` — after re-verifying the
actual side effect (venv on disk, schema in Weaviate). Sessions older than
24 hours are stale and ignored; `--no-resume` forces every step.
`--lightweight` is the fast path for re-installs on a hot system (path
rewrite + venv triage + container ensure, no model pulls or seeding) — see
[`INSTALL_RECOVERY.md`](INSTALL_RECOVERY.md).

---

## 5. The ONE bundle engine

`python -m vco_lib.project_init install-bundle` is the only code path that
installs or updates a `.claude/` bundle (hooks, scripts, agents, skills,
knowledge, settings). Three clients invoke it as a subprocess with the same
argv shape:

| Client                         | Where                                                   | Mode       | Folder vs root                |
|--------------------------------|---------------------------------------------------------|------------|-------------------------------|
| Launcher add-project (create)  | `projects_v2.rs::run_install_bundle`                    | `--json` (+ optional `--safe-add`) | folder = project, root = orchestrator |
| Launcher update                | `projects_v2.rs::run_install_bundle_update_with_root`   | `--update --json` | folder = project, root = orchestrator |
| `install.py` root self-install | `vco_lib/self_install.py::run_root_bundle_install`      | `--update --json` | folder = root = orchestrator clone |

The root passes `--folder`, `--orchestrator-root`, and `--project-folder`
all pointing at the install root — the orchestrator clone is both the source
of truth and the install target. There is no separate root-install
implementation: enumeration, the file-action classifier, the atomic file
writer, the manifest writer, and the settings merge each have exactly ONE
home in `vco_lib/project_init.py` (the "five ONE-home invariants" — see
[`INSTALL_PARITY.md`](INSTALL_PARITY.md) for the full contract and its
pinning tests).

**`--json` stdout is a machine contract.** Under `--json`, stdout carries
exactly one JSON document; every human-facing line (adoption notices, audit
rows, progress) goes to stderr. The envelope schema is declared once in
`project_init` — `BUNDLE_RESULT_TOP_KEYS` (the always-present floor;
consumers assert superset) and `BUNDLE_ACTION_KEYS` (an ordered tuple, so
`json.dumps` emits deterministic byte order) — and imported by the clients
and the parity tests.

**The classifier** assigns each shipped file an action: `create`,
`overwrite` (user-untouched, refreshed), `always-overwrite`, `noop`,
`preserve` (user-modified, kept), `adopt` (drifted runtime artifact converged
onto the rendered template, with a timestamped backup under
`.claude/backups/bundle-adoptions/<ts>/`), `skip-existing`, `skip-disabled`,
`keep-regenerated`, and the orphan/retirement actions (`orphan-deleted`,
`orphan-preserved`, `orphan-retired`, `knowledge-retired`).

**Root adoption policy**: the root's `.claude/{hooks,scripts}` are rendered
runtime artifacts — the maintainer's supported edit home is `templates/`
(git-tracked, ships to everyone), and no install flow ever writes under
`<root>/templates/`. A drifted runtime copy is adopted (with backup);
`knowledge/**` divergence is always **preserved** — user knowledge is never
adopted or overwritten, even under `--force`; `settings.json` flows through
the merge path, never the classifier; CLAUDE.md / CONTEXT_STATE / MEMORY /
`.env` are outside the bundle's ops set entirely.

**Why a subprocess and not an in-process call**: the subprocess boundary IS
the parity mechanism. All three clients exercise the exact same argv +
stdout contract, so stdout pollution or envelope drift breaks all three
surfaces identically and is caught by one test family
(`tests/test_v0285_install_parity.py`, `tests/test_v0284_json_stdout_contract.py`).

---

## 6. Manifest-driven updates + deferrals

Updates are driven by `<project>/.claude/.vco-manifest.json`, which records
the shipped hash of every bundled file:

- New shipped file → created.
- Installed file matches the prior-shipped hash (user untouched) →
  overwritten with the new shipped version.
- Installed file differs (user-modified) → preserved on disk; a
  `bundle_user_modified_preserved` deferral entry is written to
  `<project>/.claude/context/UPDATE_DEFERRED.md` naming each preserved file
  and the explicit `--force` command to accept the shipped default.
- Weaviate schema drift → a `schema_migration_required` deferral; the
  destructive migration is never auto-applied (explicit consent via
  `python -m vco_lib.project_init migrate-collections --name <project>`).

`--force` accepts shipped defaults for preserved files but **never**
overwrites user `knowledge/**` (the preserve carve-out is unconditional).
Deferral entries self-clear on the next run once their condition no longer
holds. Soft-fail throughout: subprocess errors surface as warnings, never
abort the install.

A re-run of `install.py` over an installed root is an update by
construction: the presence of `.vco-manifest.json` is the installed marker.

---

## 7. `start-launcher.*` autonomy + dist metadata

`start-launcher.{sh,command,bat}` never route through Python — they must
work when `.venv/` is corrupt or Python is uninstalled. Their view of "where
does the launcher binary live" comes from a CI-emitted fixture:

- The release CI writes `launcher/dist/<os-arch>/metadata.json` next to each
  bundled binary (binary name, version, build time, frontend-asset marker,
  per-OS candidate paths).
- The scripts read it via `scripts/lib/launcher-metadata.sh`, falling back
  to a shared hardcoded candidate list when the metadata is missing (e.g. a
  dev build straight from `cargo tauri build`).
- The frontend-asset sanity check (is this binary a real bundled build?) is
  shared via `scripts/lib/asset-ref-count.{sh,ps1}`.

The canonical macOS dist subdir is `macos-arm64/`;
`launcher/dist/experimental_macOS/` remains in the fallback candidate list
as a legacy location only — releases populate `macos-arm64/` exclusively.

---

## 8. The install log

`state/logs/install.jsonl` is an append-only JSONL log written by four
writers in their own languages — the POSIX shims/scripts (bash),
`first-install.bat` (cmd.exe), `install.py` (Python), and the launcher
(Rust) — all emitting one parity-tested schema:

```json
{"ts": "…", "actor": "install.py", "step": "5b/10",
 "phase": "start|ok|skip|warn|error", "detail": "human string, never PII",
 "data": { }}
```

The writers stay multi-language on purpose: the shim and BAT writers must
work before `install.py` is reachable. What is unified is the schema, locked
by `tests/test_jsonl_log_schema_parity.py`. Reading guidance (phase
patterns, step IDs, the `read_install_log` Tauri command) is in
[`INSTALL_RECOVERY.md`](INSTALL_RECOVERY.md).

---

## 9. Tri-OS CI smoke

`.github/workflows/install-smoke-tri-os.yml` runs the actual entry-point
shims end-to-end on every PR touching install files, every push to `main`,
and daily:

- **Matrix**: `ubuntu-22.04`, `ubuntu-24.04` (libwebkit2gtk 4.0/4.1
  fallback), `macos-14` (Apple Silicon, bash 3.2, Homebrew cascade),
  `windows-latest` (`first-install.bat` under real `cmd.exe`), `fedora-40`
  (SELinux `:Z`, dnf).
- **Fresh `git clone`**, not `actions/checkout` — the smoke exercises the
  exact code path a third-party user hits, including the bootstrap prepass
  before and after install (asserting `ready_to_install` flips to true).
- The pre-ship gate blocks release tags while this workflow is red on
  `main`.

---

## 10. Related references

- [`INSTALL_PARITY.md`](INSTALL_PARITY.md) — the root/project one-engine
  contract, argv pins, and parity-test inventory.
- [`INSTALL_RECOVERY.md`](INSTALL_RECOVERY.md) — reading the install log +
  bootstrap envelope, failure playbooks, conflict-resolution strategies.
- [`GETTING_STARTED.md`](GETTING_STARTED.md) — user-facing install
  walkthrough, flags, service coexistence.
- [`macos-install.md`](macos-install.md) — macOS specifics (Gatekeeper,
  LaunchAgent, Podman machine).
- [`schemas/install-bootstrap-envelope-v1.json`](schemas/install-bootstrap-envelope-v1.json)
  — the bootstrap envelope schema.
- `vco_lib/project_init.py` (`install-bundle` CLI), `vco_lib/self_install.py`
  (root delegation) — the code.
