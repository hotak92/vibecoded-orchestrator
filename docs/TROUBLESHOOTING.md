# Troubleshooting

Common issues during install and first-run. If none of these help, open an issue on [GitHub](https://github.com/hotak92/vibecoded-orchestrator/issues) with the output of `python install.py --help` and your OS/Python version.

## First-install issues

Issues specific to `first-install.sh` / `first-install.command` / `first-install.bat` / `first-install.desktop`.

### Joern installer hang

Versions before commit `64d5804` could hang indefinitely while downloading the Joern JVM tool. Fixed in `64d5804`. If you are on an older clone:

```bash
git pull
bash first-install.sh
```

To skip Joern entirely on any version:

```bash
bash first-install.sh --yes
# then re-run install.py directly with:
python3 install.py --no-joern
```

### macOS: "VCT Launcher is damaged and can't be opened" (Gatekeeper)

Gatekeeper blocks unsigned binaries downloaded from the internet. `first-install.command` strips the quarantine xattr automatically on binaries it downloads. If the warning appears anyway (e.g. you downloaded the `.dmg` manually):

```bash
xattr -cr /Applications/VCT\ Launcher.app
```

Then open the app from `/Applications`. This is the standard macOS procedure for unsigned third-party software; it does not bypass Gatekeeper, it tells Gatekeeper you trust this specific app.

Code signing and Apple Developer notarization are on the post-v1.0 backlog.

### Windows: "Windows protected your PC" (SmartScreen)

The first run of an unsigned Windows executable shows SmartScreen's blue dialog. This is expected for v0.1.0 — the build is not yet code-signed.

1. Click **More info**.
2. Click **Run anyway**.

SmartScreen remembers the choice per binary hash; subsequent runs on the same machine open without the dialog. Code signing is on the v0.1.1 backlog.

### Linux: `.desktop` file doesn't open on double-click

Some file managers require enabling executable-text activation before `.desktop` files open as programs rather than text.

**GNOME Files (Nautilus)**: Preferences → Behavior → Executable Text Files → "Run executable text files when they are opened".

**Thunar (XFCE)**: Edit → Preferences → Misc → "Run executable scripts by default".

**Dolphin (KDE)**: right-click → Properties → Permissions → Is executable, then double-click again.

Alternatively, run from a terminal:

```bash
bash first-install.sh
```

### Browse button doesn't open a folder picker

Resolved in commit `2c3429d`. Earlier wizard builds dynamically imported `@tauri-apps/plugin-dialog`, which Vite couldn't bundle — clicking Browse silently failed. Static imports were the fix.

If you still see this on a build older than `2c3429d`, pull the latest:

```bash
git pull
bash first-install.sh --lightweight
```

(`--lightweight` skips model pulls and KG seeding; only re-resolves the launcher build.)

### Project tabs (Hooks / MCP / Agents / Skills) appear empty after registration

Resolved in commit `03eb485`. Earlier launcher builds registered projects without populating the per-project state DB; the tabs read from that DB and showed "no entries" until the next launcher session triggered a manual refresh.

Click the **Refresh** button on the affected tab if you're on a build before `03eb485` and don't want to re-clone. New registrations on current builds populate immediately.

(Custom MCP servers added via `.claude/settings.json` outside the launcher's "Add MCP" flow are still skipped by the initial populate — known gap on the v0.1.x backlog. Workaround: re-add via the launcher's "Add MCP" button, or click **Refresh** on the MCP tab.)

### Bundled launcher binary is stale (built from a different launcher source)

`first-install.*` ships a prebuilt Linux x64 launcher binary at `launcher/dist/linux-x64/vct-launcher` (~30 MB). The installer compares its `source_hash` (in `vct-launcher.metadata.json` alongside it) against the live `launcher/` subtree — if they differ, the binary is treated as stale and the installer falls through to the download / build menu.

If you see "Bundled binary is stale, will try download/build" the bundled binary is older than the `launcher/` source you cloned. This is the expected behavior — your cloned `launcher/` is ahead of the bundled snapshot. Either:

- **Build from source** (option 2 in the menu): clean, ~5-15 min, requires Node + Tauri toolchain.
- **Download from Releases** (option 1, default): fast, but won't include uncommitted launcher source changes.
- **Rebuild the bundle locally** (contributors): `bash scripts/build-bundled-launcher.sh` rebuilds the bundled binary + metadata at the current HEAD.

### Shell-function wrappers shadow `node` / `npm` / `pnpm`

Tools like `lean-ctx`, `asdf`, `fnm`, `nvm`, and `corepack` install shell functions that wrap `node` / `npm` / `pnpm`. `command -v node` reports them as present, but `node --version` may fail (the wrapper resolves to a non-binary, or the underlying binary doesn't exist).

`first-install.sh` / `first-install.bat` now detect this — `_resolves_to_binary` rejects function/builtin shadows, `_ensure_path_for_tool` probes known-binary locations (`~/.local/bin`, `~/.fnm/aliases/default/bin`, `~/.nvm/versions/node/*/bin`, `/usr/local/bin`, `/usr/bin`) and prepends them to PATH while unsetting the shell function. If the loud-stop prompt still says a tool is missing after recheck, no real binary exists anywhere — install the tool via your OS package manager:

```bash
# Linux
sudo apt install -y nodejs npm     # Debian/Ubuntu
sudo dnf install -y nodejs npm     # Fedora/RHEL
sudo pacman -S nodejs npm          # Arch

# macOS
brew install node

# Windows
winget install OpenJS.NodeJS
```

Then `npm install -g pnpm` (may require sudo).

If a build still fails because of a wrapper that bypasses our detection, the recovery guide at [docs/INSTALL_RECOVERY.md](INSTALL_RECOVERY.md) walks Claude Code through finishing the install.

### No container runtime found

`first-install.*` prints a URL and exits if it cannot install a container runtime automatically (macOS and Windows require manual install; Linux uses pkexec to attempt it interactively).

- **Linux**: `sudo apt install podman` (Debian/Ubuntu) or `sudo dnf install podman` (Fedora/RHEL) or `sudo pacman -S podman` (Arch), then re-run `bash first-install.sh`.
- **macOS**: install [Podman Desktop](https://podman-desktop.io/) or [Docker Desktop](https://www.docker.com/products/docker-desktop), then re-run `bash first-install.sh`.
- **Windows**: install [Docker Desktop](https://www.docker.com/products/docker-desktop) or [Podman Desktop](https://podman-desktop.io/), then double-click `first-install.bat` again.

### GPU hardware detected but no driver installed

`first-install.*` detects NVIDIA and AMD GPU hardware but does not attempt driver installation — driver installs require a reboot and carry risk. If it prints a GPU URL:

- **NVIDIA**: install the driver from <https://www.nvidia.com/drivers>, then install the NVIDIA Container Toolkit from <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html>. Re-run `bash first-install.sh` after.
- **AMD (ROCm)**: install ROCm from <https://rocm.docs.amd.com/en/latest/deploy/linux/quick_start.html>. Re-run after.

Without a driver, `first-install.*` continues with CPU-only mode automatically.

---

## Bypass permissions mode

By default, Claude Code prompts for approval on every tool call. With 30+ approvals per setup session, this gets painful fast. The orchestrator ships with `"defaultMode": "bypassPermissions"` in `.claude/settings.json`. How you opt in depends on which surface you use:

**Claude Code CLI** (or Claude Desktop app): pass the flag directly, no extra config:

```bash
claude --dangerously-skip-permissions
```

The CLI/Desktop app honour `.claude/settings.json` directly, so this flag is the only thing you usually need.

**VS Code extension**: the extension wraps the CLI and gates bypass mode behind an extra setting:

1. Open VS Code Settings (`Ctrl+,` on Windows/Linux, `Cmd+,` on macOS)
2. Search for **"claude bypass"**
3. Enable **"Claude Code: Allow Bypass Permissions Mode"**
4. Restart the VS Code window

When active, you'll see **"Bypass permissions"** in the Claude Code status bar at the bottom of VS Code. Claude will not prompt for individual tool approvals.

You can disable bypass permissions again later by removing the `"defaultMode"` line from `.claude/settings.json`.

## Weaviate container won't start

**Port 8081 already in use**:

```bash
# Linux / macOS
sudo lsof -i :8081
# Or on Linux
sudo netstat -tulpn | grep 8081

# Windows
netstat -ano | findstr :8081
```

Stop whatever is using the port, or change `WEAVIATE_PORT` in `.env` and re-run `python install.py --update`.

**Container runtime not running**:

```bash
# Linux (Podman)
systemctl --user start podman.socket

# Linux / macOS (Docker)
sudo systemctl start docker              # Linux
open -a Docker                           # macOS (Docker Desktop)

# Windows
# Start Docker Desktop from the Start menu
```

**Insufficient memory**: Weaviate needs ~512 MB RAM minimum, 1-2 GB to run comfortably. On Docker Desktop, raise the memory limit under Settings → Resources.

## Ollama container won't start

**GPU not detected**:

```bash
nvidia-smi                                                # verify NVIDIA driver
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi   # verify Docker GPU access
```

If the second command fails, install the NVIDIA Container Toolkit: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html>

**Port 11435 in use**: same pattern as Weaviate — check with `lsof` / `netstat`, stop the conflicting process, or change `OLLAMA_PORT` in `.env`.

**Model pull fails**:

```bash
df -h                          # check disk space (need ~10-15 GB free)
curl -I https://ollama.com     # check network
```

If stuck, manually pull inside the container:

```bash
podman exec ollama_claude ollama pull qwen3-embedding:0.6b
```

## MCP connection failures in Claude Code

Symptom: Claude says "MCP server weaviate-kg is not connected" or tool calls like `hybrid_search` fail.

**Check MCP status**:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, ollama ✓ Connected
```

**Common causes**:

1. **Editor opened before containers started**: restart your Claude Code session (VS Code window reload, restart the CLI, or reopen Claude Desktop) once `docker ps` / `podman ps` shows Weaviate + Ollama running.
2. **Wrong Python in MCP config**: `MCP_PYTHON` must point at the `install.py`-created venv, not system Python — check `.vscode/settings.json` → `claude-code.env` (VS Code extension) **or** `.claude/settings.json` → `env` (CLI / Desktop app).
3. **Embedding model mismatch**: `ACTIVE_EMBEDDING` must match a model actually loaded by Ollama. Default is `qwen3` with model `qwen3-embedding:0.6b`. Verify with `podman exec ollama_claude ollama list`.

## Scripts in `.claude/scripts/` don't run

**Not executable**:

```bash
chmod +x .claude/scripts/*
```

(On Windows, use the `.ps1` variants — they're shipped alongside shell scripts.)

**Venv not found**: the scripts auto-detect `.venv` at the repo root. If you installed elsewhere, export `VCO_VENV` to point at your venv's root.

## Post-install, Claude doesn't read the knowledge graph

The most common cause: the editor was opened in a different directory than the orchestrator's project root. `KG_BASE_DIR` resolves relative to the working directory of the Claude Code session, so the VS Code workspace root, the CLI's `cwd`, or the folder Claude Desktop has open must match the orchestrator install dir (or the project dir you configured with the orchestrator).

Verify what Claude sees:

```
In Claude Code: run the skill /context
```

It prints the active workspace path, KG collection name, and recent state.

## A hook isn't firing or is misbehaving

`.claude/hooks/*.sh` shell hooks fire on Claude Code lifecycle events (file edits, session start, prompt submit, etc.). If one is hanging, eating tokens, or silently failing:

**Quickest diagnosis — disable everything**:

```bash
VCT_DISABLE_HOOKS=1 claude
```

Every hook respects this and exits 0 cleanly. If the issue goes away, you've narrowed it to a hook. If the issue persists, look elsewhere.

**Per-hook debugging**: the hooks are plain Bash. Run one directly with `bash -x .claude/hooks/<name>.sh` to see line-by-line execution.

**TMPDIR / scratch space failures** (sometimes seen on macOS sandboxed apps): hooks use `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}` for scratch dirs. If your shell or app has a non-writable `TMPDIR`, hooks may fail to write state. Verify with:

```bash
echo "$TMPDIR"
test -w "${TMPDIR:-/tmp}" && echo "writable" || echo "NOT writable"
```

**syntax-check on edit**: if you've edited a hook locally, run `bash -n .claude/hooks/<name>.sh` to syntax-check before saving. CI does this on every PR.

## Getting more help

- GitHub Issues: <https://github.com/hotak92/vibecoded-orchestrator/issues>
- Community channel: (TBD — linked from vibecodedtools.it at launch)
- Commercial support: Pro and MAO tiers include email support.
