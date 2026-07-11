# Install Recovery — for Claude Code, after a failed first-install

## Where the install log lives

If `<repo_root>/state/logs/install.jsonl` exists, read it FIRST. It's an
append-only JSONL written by both `install.py` and `post-install-launcher.sh`,
with each event capturing what step ran, what phase it reached, and any
relevant detail. The launcher also appends events when it spawns at end of
install or when its first-start wizard fires.

## Read the bootstrap envelope too (v0.2.53+)

If `<repo_root>/state/logs/bootstrap-prepass.json` exists, read it BEFORE
diving into `install.jsonl`. The envelope is a versioned, read-only snapshot
of the host's capabilities taken by the `first-install.{sh,command,bat}`
shim (before `install.py`'s 10-step flow runs). It tells you the answer to
"is this host even capable of installing?" without re-probing:

- `system.python` — interpreter version + `wheel_support_ok` flag (False
  means pip would have fallen back to source build; the user needs a
  3.12/3.13 interpreter, not 3.14).
- `system.podman` / `system.docker` / `system.container_runtime_chosen` —
  which runtime install.py would have picked.
- `system.gpu` — vendor (`nvidia` / `amd` / `metal` / `intel` / `none`),
  VRAM, driver, container-toolkit reachability. This is what drove the
  GPU-vs-CPU tier decision.
- `paths.launcher_binary_exists` / `paths.vct_hub_binary_exists` — whether
  the bundled binaries actually landed on disk.
- `missing_prereqs[]` — explicit `{name, human, severity, install_hint}`
  entries with severity `blocking` / `warning` / `optional`.
- `ready_to_install` — boolean. False means at least one `blocking`
  prereq is missing; the install would have failed regardless of the
  10-step retry logic.

Envelope schema: `docs/schemas/install-bootstrap-envelope-v1.json`.
`schema_version: 1` is pinned — refuse versions you don't recognise.

The envelope is **read-only** — generating it has no install side effects.
It is regenerated on every first-install shim invocation, so a stale
envelope from a prior run is unusual but possible (a user who ran
install.py directly without re-running the shim will see the prior
envelope). Check `generated_at` to confirm freshness.

Schema:
```json
{"ts": "2026-04-27T22:00:00Z",
 "actor": "install.py" | "post-install-launcher.sh" | "launcher",
 "step": "1/10" | "build/tauri" | "first-spawn" | "wizard-step-N" | ...,
 "phase": "start" | "ok" | "skip" | "error" | "warn",
 "detail": "human-readable string, never PII",
 "data": { /* optional structured payload, e.g. {"path": "/x", "size_mb": 42} */ }}
```

The last `"phase":"error"` event is usually the cause of the failure. If the
log shows steps 1-7 ok, build start, build error, no later events — the
launcher build phase is where to dig.

## Reading the log

**Each line is a complete JSON object.** Parse line-by-line; tolerate
malformed lines (a writer that crashed mid-write leaves a half-written
JSON record, which you should skip rather than abort on).

**Common phase patterns:**
- `start → ok` — step succeeded
- `start → error` — step failed; this is your failure point
- `start → skip` — step intentionally skipped (usually optional features
  like lean-ctx, or `--skip-seed` / `--skip-collections`)
- `start → warn` — step partially succeeded (e.g. seed step where the KG
  sync ran but docs upload failed). Non-fatal but worth surfacing.
- `start` with no terminal phase — the writer crashed mid-step.
  `read_install_log` reports these in `failed_steps` with detail
  `"interrupted: ..."` so the wizard offers to re-run them.

**Step IDs you'll see:**
- `1/10` … `10/10` — main install.py phases (Python → system → venv →
  deps → services → Ollama → models → collections → seed → state-dir →
  .env → claude-cli)
- `2b/10`, `7b/10`, `7c/10` — sub-steps (optional companions like lean-ctx,
  collection bootstrap, Weaviate seeding)
- `session` — meta-event: install.py session start/ok markers
- `choices` — install-time decisions (optional companions, embedding mode, container
  runtime). `detail` is the choice name, `data.value` is the chosen
  value, `data.reason` is a human-readable rationale. Read by
  `_load_previous_choices` to replay decisions on a re-install
  (24-hour stale-session rule applies — older choices are ignored).
- `state-hashes` — post-install snapshot of MD5 digests for
  `requirements.txt`, `launcher/src-tauri/Cargo.lock`,
  `launcher/package.json`, and the `knowledge/` directory listing.
  Written exactly once per successful install (at the end of `main()`).
  Read by `_compute_drift` to detect what's changed since the last
  successful install — drives the lightweight re-install path's
  upgrade-vs-skip decision. NOT subject to the 24h stale rule:
  a baseline from 3 months ago is still a perfectly valid drift
  reference.
- `lightweight` — emitted only on `install.py --lightweight` runs.
  Walks through path-rewrite + venv-triage + container-ensure (no
  model pulls, no Weaviate seed). See "Lightweight re-install" below.
- `script-start`, `audit`, `binary-probe`, `download`, `apt-deps`,
  `build/deps`, `build/tauri`, `build/locate`, `spawn` — post-install
  launcher phases
- `first-spawn`, `wizard-step-N`, `project-register`, ... — launcher
  runtime events

## Lightweight re-install (`install.py --lightweight`)

When the launcher's conflict modal Strategy 3 (overwrite-preserve)
runs against an already-installed project, the full install.py path
takes 1-2 minutes (re-detects lean-ctx, redetects GPU, re-pulls Ollama
models). Most of that work is unnecessary on a hot system.

**Lightweight mode skips:**
- Model pulls (shared volume, already pulled)
- Weaviate seeding (`sync_knowledge_graph.py` is idempotent)
- lean-ctx detection
- GPU detection / embedding-mode prompt

**Lightweight mode runs:**
1. Path rewrite — when `--lightweight-old-path <PATH>` is passed,
   replaces every absolute occurrence of `<PATH>` with the current
   `PROJECT_ROOT` in `.env` and `.claude/settings.json`. Used when
   the install moved on disk.
2. Venv triage — chooses one of:
   - `create` if `.venv/` is missing,
   - `recreate` if Python version mismatch (drop + recreate),
   - `upgrade` if `requirements_txt_md5` differs from the last
     `state-hashes` snapshot (`pip install -r ... --upgrade`),
   - `skip` if everything matches.
3. Container ensure — assumes the shared containers are already
   running. Lightweight does NOT start them; the launcher is
   responsible for that on its own startup path.
4. Fresh `state-hashes` snapshot — so the next run has a current
   baseline.

**Triggering lightweight:** the launcher detects when an
`apply_conflict_strategy` Strategy-3 run is over a project with a
healthy `.venv/` AND matching state hashes, and passes `--lightweight`
+ `--lightweight-old-path` to install.py. CLI users can also invoke
`python install.py --lightweight --lightweight-old-path /old/path`
directly.

**Tauri command** (used by the launcher's onboarding wizard + a future
Settings → Install Diagnostics panel):

```rust
read_install_log() -> InstallLog {
    events: Vec<InstallEvent>,
    state_summary: InstallState {
        session_started: Option<String>,    // ISO ts of latest session start
        completed_steps: Vec<String>,       // step IDs that reached "ok"
        skipped_steps: Vec<String>,         // step IDs that ended at "skip"
        failed_steps: Vec<(String, String)>, // (step, error_detail)
        last_event_ts: Option<String>,
        looks_complete: bool,               // heuristic — needs verification
    },
    log_path: String,
    exists: bool,
}
```

`looks_complete` is a hint, not a contract — the wizard MUST still verify
side effects (venv exists, .env present, Weaviate has the collection
schema we expect) before declaring a step a no-op. The log is necessary
but not sufficient.

**Resume behaviour in install.py**:
On re-run, install.py reads the log and skips steps whose latest phase
within the most-recent session is `ok` or `skip`. Sessions older than 24
hours are treated as stale and ignored. The user can disable resume
entirely with `--no-resume` (forces every step to re-run regardless of
log state). Even when the log says skip, install.py re-verifies the
actual side effect (venv-python on disk, schema in Weaviate, etc.)
before honouring it.



If a user opened Claude Code in this repo and pasted the install-recovery
prompt, you're reading this because the launcher build did not produce a
binary. The user has already run `bash first-install.sh` and the
orchestrator's services + knowledge seed completed successfully — only
the **launcher GUI build** failed.

## What you must NOT do

- **Do not tell the user to "build it later manually"** — that's the same
  silent-skip anti-pattern the install loud-stop already avoided.
- **Do not skip the build** with a `MODE=skip` or equivalent. The user
  cannot use the orchestrator's GUI features without a launcher binary.
- **Do not work around the build** by suggesting the CLI alone is enough.
  CLI is the "escape hatch," not the primary surface.
- **Do not edit `post-install-launcher.sh`** to silence the error. The
  user wants the build to actually succeed.

## What the launcher provides (so you understand what's at stake)

The launcher is the **default UI** for vibecoded-orchestrator. Without it
the user loses:

- **Project wizard** (creating, registering, removing projects with
  per-project KG/codegraph collections, hooks, agents, MCP wiring)
- **First-start onboarding** (one-shot setup that wires Claude Code to
  the right MCP servers + per-project memory)
- **Project switcher** (menu bar selector with per-project accent colors)
- **Services panel** (Weaviate / Ollama / code embeddings start/stop,
  health, log streaming, port adoption)
- **KG dashboard** (collection-level read/write ACL, Weaviate browser,
  graph visualisation, score-driven retrieval tier inspection)
- **Code graph dashboard** (CodeFunction/Class/Module/API/Interaction
  views, cross-service interaction map, similar-code search)
- **Hook toggles** (per-project enable/disable for the 23 shipped
  hooks: KG-summary, RL-context, security scan, audit, etc.)
- **Audit log viewer** (filterable, with cli/gui/api source attribution)
- **Module install/uninstall flow** (paid module catalog, license
  activation, telemetry consent, change-log polling for multi-tab)
- **License management** (tier display, activation, deactivation)
- **Per-project URL routes** (`/p/<slug>/...` for bookmarkable views)

The headless `vco` CLI exposes a subset of this via the launcher's local
hub HTTP API, but most flows (project registration, hook config, KG
dashboard, code graph viz) are **GUI-only by design** — they require
direct user interaction with multi-step forms, modals, and visual graph
exploration that don't translate to a flat CLI.

## What you must do

### 1. Read the install log

```bash
ls /tmp/vco-install*.log 2>/dev/null
# or check the user's terminal scrollback if they ran first-install.sh
# in a foreground terminal
```

If the log is unavailable, ask the user to re-run `bash first-install.sh`
in a terminal so you can see the full output. Don't guess — the failure
mode is in the log.

### 2. Identify the actual failure

Common patterns and their fixes:

#### a. Detection mismatch (false-positive `pnpm: yes` then `pnpm install: command not found`)

The user has a shell function (lean-ctx, asdf, nvm, fnm, corepack)
wrapping `pnpm`/`npm`/`node` that shadows the binary. `_resolves_to_binary`
detects this case; if you see it still occurring, the underlying binary
genuinely doesn't exist.

```bash
# Check if real binaries exist behind the wrappers:
ls -la "$HOME/.local/bin"/{node,npm,pnpm} \
       "$HOME/.fnm/aliases/default/bin"/{node,npm,pnpm} \
       "$HOME/.nvm/versions/node"/*/bin/{node,npm,pnpm} \
       /usr/local/bin/{node,npm,pnpm} \
       /usr/bin/{node,npm,pnpm} 2>/dev/null
```

Fix:
- If `node` binary exists somewhere → prepend that dir to PATH and rerun
  the build phase.
- If no node binary anywhere → install Node.js via the system package
  manager:
  - apt: `sudo apt update && sudo apt install -y nodejs npm`
  - dnf: `sudo dnf install -y nodejs npm`
  - pacman: `sudo pacman -S nodejs npm`
  - brew: `brew install node`
  - winget: `winget install OpenJS.NodeJS`

After Node is installed, install pnpm: `npm install -g pnpm` (may need
sudo for system-global installs).

#### b. Tauri build deps missing (Linux)

`pnpm install` succeeds but `pnpm tauri build` fails with linker errors
about webkit2gtk, gtk, libsoup, javascriptcoregtk, or appindicator.

Fix on Ubuntu/Debian:
```bash
sudo apt update && sudo apt install -y \
    libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev \
    librsvg2-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev \
    build-essential curl wget file
```

Fedora/RHEL: replace `apt install` with `dnf install` and use the
distro's package names (`webkit2gtk4.1-devel`, `gtk3-devel`, etc.).
Arch: `sudo pacman -S webkit2gtk-4.1 gtk3 libayatana-appindicator
librsvg libsoup3`.

If on a non-apt distro that the install script flagged with "skipping
auto-install of Tauri deps", install them via the distro's pkg manager
manually and rerun.

#### c. Rust toolchain missing

`pnpm tauri build` errors with "cargo: command not found" or rustc
version too old.

Fix: install Rust via rustup:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
```

#### d. Permissions / disk full

Check `df -h` and `ls -la launcher/src-tauri/target/`. If disk is full,
clean cargo caches: `cargo clean --manifest-path launcher/src-tauri/Cargo.toml`.

#### e. Network / proxy

If Cargo or npm registry calls fail, check `~/.npmrc`, `~/.cargo/config.toml`,
proxy env vars (`HTTP_PROXY`, `HTTPS_PROXY`).

#### f. V52-M: bundled hooks dead because they shipped without exec bit (FIX in v0.2.53)

Symptom: `claude mcp list` is healthy, hook registrations in
`.claude/settings.json` look correct, but `bash-context-inject.sh`,
`pre-edit-context-inject.sh` etc. never run — no entries in
`.claude/logs/security_events.jsonl` (the hooks' security-event log) and
no hook stderr under `.claude/logs/`, no observable side effects. (The
dated `*_tool_usage.jsonl` file is written by the weaviate MCP's query
logger, not by the hooks, so its absence is NOT a hook-health signal.)
Cause (pre-v0.2.53): a subset of `templates/hooks/*.sh`
were committed with mode `0644`, `shutil.copy2` preserved that mode
into the project, and POSIX Claude Code refuses to invoke a
non-executable hook script.

Fix path on a fresh install or upgrade:

```bash
cd <repo_root>
python install.py --update   # install.py:11299 force-chmods every copied
                              # *.sh hook target to 0o755 after copy2 —
                              # any silently-dead hook is now activated
```

No data side effects; `--update` is idempotent. If hooks still don't
fire, check `.claude/settings.json` hook registrations were not
inadvertently removed (PreToolUse / PostToolUse matchers absent).
The chmod-0755 pass also covered a v0.2.52 UTF-8 BOM regression on a
couple of `.ps1` siblings; same recovery command.

#### g. Hub binary not discoverable

If `find_hub_binary` returns `None` (visible as `vct-hub: not found`
in `claude mcp list` failure modes), the launcher's discovery walked
all candidate paths and missed. The discoverer
(`launcher/src-tauri/src/hub_launcher.rs`) walks 5 candidates in order:
explicit env override → `~/.vct/bin/` → in-tree dist resolved via
`current_exe()` walking (sibling layout, then 1-up fallback).

**`VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1` is a test-only sentinel** —
never set it as a workaround. Production code never sets it. It
exists exclusively so `cargo test` runs against `target/debug/`
(where stray `vct-hub` binaries from sibling cargo invocations
contaminate the search) can deterministically assert "no hub
anywhere". If you find this env var set in a user's session, find
out who set it and unset it — it is masking a real discovery failure.

The real fix when discovery fails: rebuild the bundle locally
(`bash scripts/build-bundled-launcher.sh` from the repo root), or
re-run `python install.py --update` which re-stamps the launcher's
recorded binary paths to the canonical `launcher/dist/<os>-<arch>/`
location.

### 3. Run the build manually after fixing

```bash
cd <repo_root>/launcher
pnpm install     # or: npm install
pnpm tauri build # or: npx tauri build
```

The build takes 5-15 minutes depending on the machine. Stream output —
don't redirect to /dev/null. If it fails again, capture the full error
and continue diagnosing.

### 4. Verify the binary exists

```bash
ls -la <repo_root>/launcher/src-tauri/target/release/vct-launcher* 2>/dev/null
```

Expected: a `vct-launcher` executable around 30-60 MB. (Older builds
may have produced a `vct-launcher-temp` artifact instead.)

### 5. Start the launcher

```bash
cd <repo_root>
./start-launcher.sh
```

Or on macOS: `./start-launcher.command`. On Windows: `start-launcher.bat`.

Confirm with the user that the launcher window opened. If it didn't,
check stderr for graphics errors (Wayland incompatibility, missing
shared libraries via `ldd <binary>`) and fix those.

### 6. Tell the user the launcher is ready

Show them how to register their first project via the wizard (the
launcher's first-start flow is the canonical UX for that). Don't
recommend the `vco` CLI as a substitute — it's an escape hatch, not
the primary surface.

## Use the orchestrator's own tools

The user just installed vibecoded-orchestrator. That means **you (Claude)
have access to all of it**:

- `hybrid_search` MCP for "tauri build webkit2gtk linux 2026" type
  questions (the bundled KG has hardware/Linux-distro hints)
- your own reasoning for quick error analysis (the Ollama MCP was removed in v0.2.11; Claude's built-in reasoning handles this directly)
- `Read` tool with `offset`/`limit` for parsing the install log if it's large
- `query_code_structure` MCP if you need to understand what file
  references the failing build artifact

Use them. The whole point of vco is that Claude Code becomes more
capable inside a vco-aware repo. This is a perfect test case.

## When to give up

If after 3 build attempts you still can't get a working binary, AND
you've ruled out:
- Missing Node/Rust toolchain
- Missing system packages (webkit2gtk + friends)
- Disk full
- Network/proxy

…ask the user to file an issue at
`https://github.com/hotak92/vibecoded-orchestrator/issues` with the
full install log + their OS / arch / distro / Node version / Rust
version / kernel version. Include the exact build command you ran
and the last 200 lines of build output.

But that should be rare. Most failures are one of the categories
above.

---

## Conflict Resolution (re-installing over existing files)

When the install path already contains orchestrator files (`.claude/`,
`knowledge/`, etc.), the launcher's OnboardingWizard surfaces a 4-option
modal instead of the legacy "call preview_install + confirm_overwrite=true"
error. CLI users who run `python install.py` directly get the same options
via `--conflict-strategy=...`.

### The 4 strategies

1. **Overwrite, preserving project-specific files** *(default — strongly
   recommended)*

   Copy every tracked install file on top of existing ones BUT for a
   small "preserve list" of user-curated files, leave the existing
   file untouched and write the upstream version next to it as
   `<file>.new.<ext>` (e.g. `CLAUDE.new.md` next to `CLAUDE.md`). After
   all files are copied, append a notification block to
   `.claude/CONTEXT_STATE.md` instructing Claude to merge the pairs
   on the next session start.

   Default preserve list (`DEFAULT_PRESERVE_LIST`):
   - `CLAUDE.md`
   - `.claude/CONTEXT_STATE.md`
   - `.claude/PROJECT_REGISTRY.md`
   - `.env`

   `MEMORY.md` is intentionally NOT in the preserve list — it lives at
   `~/.claude/projects/<id>/memory/MEMORY.md`, not in the install dir,
   so v1.0 of the conflict resolver does not write a `.new.md` for it.
   The notification block mentions this so the user can manually merge
   if their MEMORY.md has diverged from the upstream template.

2. **Overwrite all** — copy every tracked install file on top with no
   preservation. Loses user edits to CLAUDE.md, CONTEXT_STATE.md, etc.

3. **Delete and replace `.claude/`** — wipes ONLY the destination's
   `.claude/` directory (NOT the entire install path; the rest may
   contain real user code), then performs a fresh install. Use this
   when `.claude/` is corrupt and you want a clean slate.

4. **Adopt as-is** — equivalent to the legacy `confirm_overwrite=true`
   path: keep existing files exactly as they are, just register the
   project in the launcher.

### CLI usage (advanced)

CLI users running `install.py` directly:

```bash
# Overwrite-preserve with the default preserve list:
python install.py \
    --conflict-strategy=overwrite-preserve \
    --conflict-source-path=/path/to/bundled/orchestrator

# Custom preserve list:
python install.py \
    --conflict-strategy=overwrite-preserve \
    --conflict-source-path=/path/to/bundled/orchestrator \
    --preserve-paths="CLAUDE.md,.claude/CONTEXT_STATE.md"

# Wipe .claude/ and re-install:
python install.py \
    --conflict-strategy=delete-claude \
    --conflict-source-path=/path/to/bundled/orchestrator
```

`--conflict-source-path` MUST point to a directory containing
`vct-module.json` (i.e. a bundled orchestrator repo). The conflict
resolver will refuse to copy from anywhere else.

### The Claude self-merge contract

After Strategy 1 (`overwrite-preserve`) runs, the appended notification
block in `.claude/CONTEXT_STATE.md` looks like this:

```markdown
<!-- vct-merge-pending -->
## Pending merge — read this on session start

The orchestrator was just upgraded. Several user-curated files have an
upstream-new version sitting next to them (`*.new.md` / `*.new.<ext>`).
For each pair:

1. Read both the existing file AND the upstream-new sibling.
2. Reconcile: keep the user's project-specific content, but adopt new
   structure / guidance / sections from the upstream version. Use your
   judgment for ambiguous merges; ask the user if a conflict is
   irreconcilable.
3. After successfully merging a file, **delete its upstream-new
   sibling**.
4. When ALL `.new.*` siblings under the install path are gone, you'll
   know the merge is complete — at that point, **delete this entire
   notification block** (the HTML-comment markers wrapping this section
   plus all text between them) from this CONTEXT_STATE.md. That removes
   the prompt for the next session.

Files awaiting merge:
- `CLAUDE.md` (upstream-new at `CLAUDE.new.md`)
- `.claude/CONTEXT_STATE.md` (upstream-new at `.claude/CONTEXT_STATE.new.md`)
…

Note: `MEMORY.md` lives at `~/.claude/projects/<id>/memory/MEMORY.md`,
not in the install dir, so v1.0 of the conflict resolver does NOT write
an upstream-new sibling for it. If you suspect your MEMORY.md is
divergent from the upstream template, run a manual diff and merge by
hand.

(Do NOT delete user content. Preserve any session-specific state in
CONTEXT_STATE.md, your existing CLAUDE.md customisations, etc. The
upstream version is a reference for new structure, not a wholesale
replacement.)
<!-- /vct-merge-pending -->
```

Claude self-monitors via the contract:

- **When you process the merge**, you delete the `.new.<ext>` sibling.
- **When all `.new.*` siblings are gone**, you delete the notification
  block.

No external trigger needed. The user simply opens Claude Code in the
install root and you read CONTEXT_STATE.md as you always do on session
start.

The notification block is **idempotent** — if the user re-runs the
install with `overwrite-preserve` while a stale block from a previous
run is still in CONTEXT_STATE.md, the new block REPLACES the old one
in-place rather than duplicating. The marker comments
`<!-- vct-merge-pending -->` ... `<!-- /vct-merge-pending -->` are the
boundary; do not paste them into other prose, otherwise the
marker-counting idempotency check breaks.

### Audit trail

Both the launcher (Rust) and `install.py` (Python) emit a
`conflict-resolve` event into `state/logs/install.jsonl`:

```json
{
  "ts": "2026-04-27T12:34:56Z",
  "actor": "launcher",  // or "install.py"
  "step": "conflict-resolve",
  "phase": "ok",
  "detail": "strategy=OverwritePreserve",
  "data": {
    "strategy": "OverwritePreserve",
    "preserved_count": 4,
    "new_md_count": 4,
    "notification_written": true,
    "copied_count": 137
  }
}
```

`read_install_log` (Tauri command) and the Diagnostics panel surface this
event so the user can see which strategy ran and what it touched.

---

## localStorage flag scoping migration (v0.2.53)

In v0.2.52 and earlier, the launcher's "you have an update available"
dismissal flag was stored under the unscoped localStorage key
`dismissed_update_version` (one master plan name in early drafts). In
v0.2.53 the real code uses the install-path-scoped key
`vct.update.seen_version` (see `launcher/src/lib/stores/updater.ts`),
threaded through `getInstallScopedFlag` / `setInstallScopedFlag` in
`launcher/src/lib/stores/install-state-store.ts`. This lets a user
running multiple VCO installs from the same browser session dismiss
the banner per-install rather than globally.

A one-shot legacy-cleanup migration runs at launcher boot: any
unscoped `dismissed_update_version` value is dropped on first start
after upgrade. No user action required. The migration is silent — it
does not surface to the GUI or `install.jsonl`. If a user reports
"the dismissal sticky doesn't persist any more after upgrade", that
is the expected one-time effect of the scoping move; their next
dismissal will persist (under the new scoped key).

This is a one-off behavioural blip, not a recovery scenario. Don't
re-implement the old key as a workaround.
