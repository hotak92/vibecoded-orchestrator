# Install Recovery — for Claude Code, after a failed first-install

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
- **Hook toggles** (per-project enable/disable for the 20+ shipped
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
wrapping `pnpm`/`npm`/`node` that shadows the binary. The 2026-04-27
fixes added `_resolves_to_binary` to detect this, but if you see this
pattern still occurring, the underlying binary genuinely doesn't exist.

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

Expected: a `vct-launcher` (or `vct-launcher-temp` during pre-rename
sprint) executable around 30-60 MB.

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
- `chat` MCP (Ollama, free) for quick error analysis without burning
  Claude API tokens
- `read_document` MCP for parsing the install log if it's large
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
