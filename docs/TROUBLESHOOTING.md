# Troubleshooting

Common issues during install and first-run. If none of these help, open an issue on [GitHub](https://github.com/hotak92/vibecoded-orchestrator/issues) with the output of `python install.py --help` and your OS/Python version.

## First-install issues

Issues specific to `first-install.sh` / `first-install.command` / `first-install.bat` / `first-install.desktop`.

### macOS: "VCT Launcher is damaged and can't be opened" (Gatekeeper)

Gatekeeper blocks unsigned binaries downloaded from the internet. `first-install.command` strips the quarantine xattr automatically on binaries it downloads. If the warning appears anyway (e.g. you downloaded the `.dmg` manually):

```bash
xattr -cr /Applications/VCT\ Launcher.app
```

Then open the app from `/Applications`. This is the standard macOS procedure for unsigned third-party software; it does not bypass Gatekeeper, it tells Gatekeeper you trust this specific app.

Code signing and Apple Developer notarization are on the post-0.2.0 backlog.

### Windows: "Windows protected your PC" (SmartScreen)

The first run of an unsigned Windows executable shows SmartScreen's blue dialog. This is expected for v0.2.x — the build is not yet code-signed.

1. Click **More info**.
2. Click **Run anyway**.

SmartScreen remembers the choice per binary hash; subsequent runs on the same machine open without the dialog. Code signing is on the post-0.2.0 backlog.

### Windows: Update orchestrator binary handoff (V52-AH — FIXED in v0.2.52)

**Symptom (pre-v0.2.52 only)**: on Windows, after clicking "Update orchestrator" from the launcher GUI, the banner showed "newer binary on disk v0.2.5X, running v0.2.5X-1, Restart" and the restart relaunched the SAME old version, looping forever. On-disk `dist/windows-x64/vct-launcher.exe` was the OLD version even though `git pull` succeeded.

**Root cause**: Windows mandatory file locks prevented overwriting the running `.exe`. Linux/macOS advisory locks allowed the pre-pull rename pattern to work; Windows did not.

**Fix (v0.2.52)**: Velopack/Squirrel-style stage1 updater pattern. The launcher spawns `vct-updater.exe` (small statically-linked Rust binary) DETACHED before exiting; the updater polls the parent PID, waits for the launcher to exit (releasing file handles), performs `MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)` for each staged `<target>.new` sibling, then spawns the new launcher. Hand-off contract via `~/.vct/update.lock.json`.

**If you hit this on v0.2.52+**: the stage1 updater should handle it automatically. If it fails for any reason, the legacy `restart_launcher` path still runs as a fallback (= same UX as v0.2.51, which is the existing `launcher_binary_swap_failed_locked` deferral with manual `git checkout` recovery instructions). Either way, the update completes.

**Emergency manual workaround** (only if vct-updater.exe is missing or fails):

```powershell
taskkill /F /IM vct-launcher.exe
taskkill /F /IM vct-hub.exe
cd C:\path\to\vibecoded-orchestrator
git checkout HEAD -- dist/windows-x64/vct-launcher.exe dist/windows-x64/vct-hub.exe
.\dist\windows-x64\vct-launcher.exe
```

### Windows: MCP fork-bomb during update (V52-AI — FIXED in v0.2.52)

**Symptom (pre-v0.2.52 only)**: during or after `Update orchestrator`, the system slowed to a crawl. Task Manager showed dozens of `python.exe` processes (typically 50–100) OR `node.exe` processes (typically `npx @upstash/context7` / `@modelcontextprotocol/*`). CPU pinned at 100% for hours.

**Root cause**: during the update window, the launcher restart + MCP supervisor restart + Claude Code's reconnection attempts overlapped. On Windows mandatory locks, every MCP-spawn-against-an-updating-binary failed → supervisor retried → respawn loop.

**Fix (v0.2.52)**: 3-layer lockfile gate at `<vct_root>/.update-in-progress.json`. (1) Pre-update kill sweep terminates currently-running MCP-shaped processes; (2) every shipped MCP server reads the lockfile at startup and exits cleanly with exit code 75 when active — so every Claude Code respawn dies immediately, breaking the loop; (3) Rust RAII `Drop` impl deletes the lockfile on every exit path. Boot-time stale-cleanup removes lockfiles whose 15-min deadline passed.

**On v0.2.52+**: no action needed; the gate handles it. The kill sweep is strictly scoped to MCP-shaped processes (pattern: `claude_mcp_servers/` / `@modelcontextprotocol/` / `@upstash/context7`) — other Python/Node processes are unaffected.

**Emergency manual workaround** (only if the gate fails):

```powershell
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq "python.exe" -or $_.Name -eq "node.exe") -and
    ($_.CommandLine -match "mcp|claude_mcp_servers|@modelcontextprotocol|@upstash")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### Windows: `install.py --update` step 7c "seeding Weaviate" hangs at 40-50% (V52-AJ — FIXED in v0.2.52)

**Symptom** (Windows + CPU-only, reported in v0.2.51 by users who configured `arctic` as their embedding via the launcher's Identity tab): `install.py --update` reaches step `7c/10 seeding Weaviate` and hangs for hours at 40-50% completion. The log shows `CI-10: full sync (context change: embedding=None→'qwen3', ...)` — using qwen3 (slow on CPU: ~30s/chunk) instead of the user-selected arctic.

**Why it happened** (v0.2.51 root cause): `install.py` spawned `python sync_knowledge_graph.py` as a subprocess. The subprocess called `EmbeddingService.for_project()`, which read ONLY from `os.environ` (`ACTIVE_EMBEDDING` env var). It did NOT read `app_state.embedding.active_profile` from `launcher.db`, nor `.claude/settings.json`, nor `.claude/env`. Setting `ACTIVE_EMBEDDING=arctic` in `.claude/settings.json` had no effect — that file is read by Claude Code's MCP subprocesses, not by `install.py`'s sync subprocess.

**v0.2.52 fix (V52-AJ)**: 3-layer resolution chain now closes the disconnect:

1. `install.py` reads `launcher.db app_state[embedding.active_profile]` and threads `ACTIVE_EMBEDDING` + `EMBEDDING_MODEL` into the subprocess env before spawning `sync_knowledge_graph.py`.
2. `EmbeddingService.for_project()` reads `launcher.db` as fallback when env unset (single canonical chain: env → launcher.db → `"qwen3"` default).
3. This `docs/TROUBLESHOOTING.md` entry + `templates/ORCHESTRATOR-CLAUDE.md.template` documents the resolution chain so users + future Claude know which channel to use.

**On v0.2.52+**: no action needed. Whatever you selected in the launcher's Identity tab → Embedding selector is what `install.py --update` will use. Setting `$env:ACTIVE_EMBEDDING` in PowerShell before running install.py still works as an override.

**Pre-v0.2.52 workaround** (if you're still on v0.2.51 and stuck): set the env var in your shell BEFORE running install.py:

```powershell
$env:ACTIVE_EMBEDDING = "arctic"
$env:EMBEDDING_MODEL = "snowflake-arctic-embed2:latest"
# Verify arctic2 is on Ollama:
ollama list | Select-String arctic
ollama pull snowflake-arctic-embed2:latest   # if missing

cd C:\path\to\vibecoded-orchestrator
python install.py --update
```

Speed expectation on CPU + 24GB RAM: ~1-2 s/chunk (arctic2) vs ~30 s/chunk (qwen3). Full sync of ~200 nodes completes in 5-10 minutes instead of "hours".

### Windows: `first-install.bat` crashes with `UnicodeEncodeError` (v0.2.25 / v0.2.26)

If you downloaded a v0.2.25 or v0.2.26 release zip and `first-install.bat` crashes at step `[5b/10]` with a message like:

```
UnicodeEncodeError: 'charmap' codec can't encode character '→' (U+2192)
  File "install.py", line 6205, in _resolve_service_safety
    print(f"  [{name}] not running ...")
```

…you've hit a Windows-only encoding bug in those releases. **Two fixes shipped in v0.2.27 (commits `97eceaf` + `a5b2971`)**: install.py now reconfigures its stdout/stderr to UTF-8 on Windows, and every `.ps1` script in the repo carries a UTF-8 BOM so PowerShell 5.1 (the OS default on Windows 10/11) can parse them. The release artifacts for v0.2.25 / v0.2.26 don't contain these fixes because they shipped post-tag.

**Recovery on stock Windows 10/11** — pick one:

1. **Recommended — upgrade to v0.2.27+ via git**. Open a terminal (any of PowerShell, cmd, Git Bash) in the directory where you extracted the zip:

   ```cmd
   git pull origin main
   first-install.bat
   ```

   The `git pull` brings in the two fixes; the second `first-install.bat` run completes normally. This works even though the **on-disk** install.py from the zip is still the broken one — the `git pull` overwrites it with the fixed version before any installation step runs.

2. **Run install.py with a forced UTF-8 environment** (no git needed). From cmd:

   ```cmd
   set PYTHONIOENCODING=utf-8
   set PYTHONUTF8=1
   python install.py --update
   ```

   Or from PowerShell:

   ```powershell
   $env:PYTHONIOENCODING = "utf-8"
   $env:PYTHONUTF8 = "1"
   python install.py --update
   ```

   This forces the legacy v0.2.25 / v0.2.26 install.py to use UTF-8 for stdout, sidestepping the `cp1252` codec entirely. Equivalent to the in-Python fix that landed in v0.2.27.

3. **Download the v0.2.27+ release zip directly** (the simplest if you don't have git installed): delete your old install and re-extract the new zip. Both fixes are baked into v0.2.27's `install.py` + `.ps1` files.

The fix is also belt-and-braces inside the launcher: when you click **Update orchestrator** from the launcher GUI (instead of running `install.py` from a terminal), the launcher binary itself sets `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` on the Python subprocess — so the in-GUI update path works even on a v0.2.25 install where the on-disk install.py is still the broken version. This protection landed in launcher v0.2.27 commit `046e1dc`.

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

(Custom MCP servers added via `.claude/settings.json` outside the launcher's "Add MCP" flow are still skipped by the initial populate — known gap on the v0.2.x backlog. Workaround: re-add via the launcher's "Add MCP" button, or click **Refresh** on the MCP tab.)

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

### GPU mode flaps between CUDA and CPU across reboots (exactly-8GB-VRAM hosts)

If you have exactly 8 GB of VRAM on the NVIDIA card and `install.py --update` decides "CPU mode" some boots and "CUDA mode" other boots, the auto-tier-up to `qwen3-embedding` is hitting the boundary case. The threshold check uses `>= 8GB` (inclusive) which is correct in theory, but **NVIDIA drivers reserve some VRAM at boot for the framebuffer + compositor**, so `nvidia-smi` reports anywhere from 7.6 GB to 8.0 GB depending on whether a display is connected, whether the compositor is running, and whether you booted with a different kernel module.

Symptoms: services come up in CUDA mode after one reboot but CPU mode after the next; you see `code-embed` switching between `:11440-codesage` and `:11440-ollama` backends in logs. To pin to CPU and stop the flap:

```bash
python install.py --update --gpu-vram-threshold-gb 9
```

This raises the threshold above 8.0 GB so the host falls into the CPU tier deterministically. Alternative: `--cpu-only` skips the GPU probe entirely.

### Python 3.14 detected at install step 1/10 with a wheel-coverage error

On v0.2.53+, `_check_python_version` (install.py:6979) detects Python ≥ 3.14 and probes wheel coverage via `pip install --dry-run --only-binary=:all:` against a representative set of binary-heavy deps (`weaviate-client`, `pydantic`, `httpx`). When the probe reports the install would fall back to source build, step 1/10 fails fast with:

```
FAIL
  Python 3.14.X is too new — wheels are not yet published for VCO's binary deps.
  pip would try to build from source, which typically fails without a C/C++ toolchain installed.

  Workaround: install Python 3.12 or 3.13 and re-run first-install with that interpreter:
    # macOS / Homebrew:
    brew install python@3.13
    /opt/homebrew/bin/python3.13 install.py
    # Linux / apt:
    sudo apt install python3.13
    python3.13 install.py
    # Windows / py launcher:
    py -3.13 install.py
```

This is a fail-fast rather than a wait-for-pip-to-fail-15-minutes-deep. The same probe runs in the bootstrap envelope (`_bootstrap_python_wheel_support`, install.py:716) so the `system.python.wheel_support_ok` field in `state/logs/bootstrap-prepass.json` indicates the same condition without running the full install. The check is gated on Python ≥ 3.14 because wheel coverage for 3.12 / 3.13 is universal in practice.

If you already created a venv on 3.14, delete `.venv/` and re-run install with a 3.13 / 3.12 interpreter — install.py won't rebuild the venv just to downgrade. Verify the venv came up on the right interpreter: `cat .venv/pyvenv.cfg | grep version` should show 3.12.x or 3.13.x.

### `install.py` looks hung mid-step (pip / npm / playwright)

Through v0.2.52, several install steps (`pip install -r`, `pip install -e`, `npm install -g`, `npx playwright install`) blocked on long-running subprocesses with no output, making the install indistinguishable from a hang. v0.2.53 routes these through `_run_logged_subprocess` (install.py:10844) with two changes:

1. **Dot-cycle animation** after 3 s of subprocess silence — you see `[12s] pip-install ...` with cycling dots, refreshed once per second. The leading second-count is monotonic from subprocess start. As long as the seconds are advancing, the subprocess is alive.
2. **Finite per-request pip timeout**: pip is now invoked with `--timeout 60 --retries 5 --prefer-binary` (`_pip_install_flags`, install.py:10777). Previously pip used its default 15-second timeout but install.py wrapped pip in an unbounded outer subprocess — so a stuck pip call could block install.py indefinitely. The new shape gives pip 60 s per HTTP request and surfaces a clean failure (with stderr tail) if the subprocess's outer timeout fires.

What changed for you: if your network is so slow pip is making real progress but slowly, you may now hit the outer subprocess timeout where you previously did not. Check the elapsed-seconds counter — if it stops advancing, the subprocess genuinely hung. If it keeps advancing past the timeout, file an issue with the bootstrap envelope + step log.

### Podman: "machine not initialized" on macOS / Windows

The first time you run install on macOS or Windows with Podman selected, the Podman daemon needs a one-time `machine init`. v0.2.51+ install.py auto-runs `podman machine start` if the machine exists but is stopped — but it deliberately does NOT auto-run `podman machine init` (which writes a several-GB VM image to your home directory; should be a user-explicit step). Symptom: `install.py` exits with a `UPDATE_DEFERRED.md` entry naming the `podman machine init` command.

Recovery (one-time setup):

```bash
podman machine init --memory 4096 --cpus 2 --disk-size 30
podman machine start
podman info                          # sanity-check; should print the connection URI
python install.py --update           # re-run install — the deferral self-clears once Podman responds
```

The `--memory 4096 --cpus 2 --disk-size 30` flags are a sensible default for VCO (Weaviate plus Ollama + code-embed comfortably fit). Without them, Podman's machine defaults are smaller and the Weaviate container can run out of memory under embedding load.

If `podman machine init` fails with "out of disk", clear `~/.local/share/containers/podman/machine/` and retry — the partial download blocks the next init attempt.

### V52-M: bundled hooks shipped without exec bit (FIXED in v0.2.53)

**Symptom (pre-v0.2.53 only)**: hooks dropped into a fresh project by `install.py` (e.g. `bash-context-inject.sh`, `pre-edit-context-inject.sh`) never fired. `claude mcp list` was healthy, the hook registrations in `.claude/settings.json` looked correct, but the hook script behaviour was silently absent. Cause: a subset of `templates/hooks/*.sh` shipped with mode `0644` (no exec bit), and on POSIX Claude Code refuses to invoke a non-executable hook.

**Root cause**: `shutil.copy2` (used by install.py's hook copier) preserves the source file's mode. Any contributor who committed a 664-mode hook silently disabled it for every fresh install. The pre-existing test `test_install_into_fresh_target_linux` caught three such hooks in v0.2.52.

**Fix (v0.2.53)**: belt-and-braces — `install.py:11299` now force-chmods every copied `*.sh` hook target to `0o755` after `copy2`. Source mode is no longer trusted. Same pass also fixed a UTF-8 BOM regression on a couple of `.ps1` siblings that made PowerShell 5.1 refuse to parse them.

**Recovery on v0.2.52 → v0.2.53**: re-run `python install.py --update` from the install root. The chmod-0755 defence runs against every copied hook target, activating any hook that was silently dead before. No data-side work required.

### Bundled binary path drift on in-place upgrade

If you upgrade VCO in place (e.g. `python install.py --update` on top of an existing install where the launcher binary was previously copied to a non-default location), the launcher may keep launching from the OLD path while the new bundled binary lives at the new path. Symptom: launcher UI reports the OLD version after an update; the new `vct-launcher` and `vct-hub` binaries are present in the install root but nothing picks them up.

Workaround:

1. From the install root, locate both binaries: `find . -name 'vct-launcher*' -o -name 'vct-hub*'` (expect 2-4 hits).
2. The canonical paths are `launcher/dist/<os>-<arch>/vct-launcher{,.exe}` and `vct-hub/dist/<os>-<arch>/vct-hub{,.exe}`. Any binaries at non-canonical paths are stale.
3. Delete the stale copies (NOT the canonical ones).
4. Quit + relaunch the launcher. The next boot picks up the canonical binary; if it doesn't, run `python install.py --update --force` to re-stamp the launcher's recorded binary path back to the canonical one.

This is rare — most installs stay on the canonical paths. It happens when an earlier version's installer placed binaries in `~/bin/`, `~/.local/bin/`, or `/usr/local/bin/` and a manual upgrade left the symlinks behind.

---

## Bypass permissions mode

By default, Claude Code prompts for approval on every tool call. With 30+ approvals per setup session, this gets painful fast. The orchestrator ships with `"defaultMode": "bypassPermissions"` in `.claude/settings.json`. How you opt in depends on which Claude Code client you use (the primary target is **VS Code with the [Claude Code extension](https://docs.anthropic.com/en/docs/claude-code/ide-integrations)**; any client that reads `.claude/` works):

**VS Code extension** (primary target): the extension wraps the underlying Claude Code engine and gates bypass mode behind an extra setting:

1. Open VS Code Settings (`Ctrl+,` on Windows/Linux, `Cmd+,` on macOS)
2. Search for **"claude bypass"**
3. Enable **"Claude Code: Allow Bypass Permissions Mode"**
4. Restart the VS Code window

When active, you'll see **"Bypass permissions"** in the Claude Code status bar at the bottom of VS Code. Claude will not prompt for individual tool approvals.

**Claude Code CLI** (optional, or Claude Desktop app): pass the flag directly, no extra config:

```bash
claude --dangerously-skip-permissions
```

The CLI/Desktop app honour `.claude/settings.json` directly, so this flag is the only thing you usually need.

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
podman exec vco_ollama ollama pull qwen3-embedding:0.6b
```

> The canonical container name is `vco_ollama` (per `vco_lib/containers.py::CANONICAL_CONTAINERS`, since v0.2.15). The legacy name `ollama_claude` is preserved as an alias in `HISTORICAL_ALIASES` for backward compatibility — both work, but new docs and tooling use the canonical form.

## MCP connection failures in Claude Code

Symptom: Claude says "MCP server weaviate-kg is not connected" or tool calls like `hybrid_search` fail.

**Check MCP status**:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, search ✓ Connected
# (coordination is optional; lean-ctx / vct-coordination may also appear if installed.)
```

> **"Where is the `ollama` MCP / `chat` / `read_image` tool?"** Removed in v0.2.11. Claude's native reasoning and the built-in `Read` tool (which handles images via vision) cover those use cases at higher quality. Ollama still runs as infrastructure on `:11435` — it powers Weaviate's text embeddings and the code-embedding service CPU fallback — but it is no longer exposed as an MCP tool. See `knowledge/concepts/mcp-simplification-v0211.md` if you need the full rationale.

**Common causes**:

1. **Editor opened before containers started**: restart your Claude Code session (VS Code window reload, restart the CLI, or reopen Claude Desktop) once `docker ps` / `podman ps` shows Weaviate + Ollama running.
2. **Wrong Python in MCP config**: `MCP_PYTHON` must point at the `install.py`-created venv, not system Python — check `.claude/settings.json` → `env` (the canonical channel since v0.2.12; CLI / Desktop app / VS Code extension all read it, and MCP subprocesses inherit from it). The historical `.vscode/settings.json` `claude-code.env` surface was removed in v0.2.12 (PR-27) because that block didn't propagate to MCP subprocesses on Linux Claude Code 2.1.143 (empirical sentinel testing confirmed; that's why the surface was dropped). If you're migrating from a pre-v0.2.12 install and your project relied on `claude-code.env`, copy the keys into `.claude/settings.json` `env` — same shape, different file.
3. **Embedding model mismatch**: `ACTIVE_EMBEDDING` must match a model actually loaded by Ollama. Default is `qwen3` with model `qwen3-embedding:0.6b`. Verify with `podman exec vco_ollama ollama list` (the canonical container name since v0.2.15; legacy `ollama_claude` still works as an alias).

## vct-hub troubleshooting (v0.2.21+)

Since v0.2.21 the launcher's HTTP hub is a detached binary (`vct-hub`) shipped alongside `vct-launcher` in `launcher/dist/<arch>/`. It outlives the launcher GUI: close the GUI and hooks, MCP servers, and resolver clients can still reach the hub on `http://127.0.0.1:7700`. It is started by `install.py`'s post-install step, by the `session-start-ensure-hub.sh` Claude Code hook, by `.vscode/tasks.json` on `folderOpen`, and by the launcher itself on GUI start. See `launcher/src-tauri/vct-hub/src/{lifecycle,lockfile,auth,boot}.rs` for the implementation.

### Quick health check

```bash
vct-hub --status
# Possible outputs (exit codes in parentheses):
#   running pid=12345   (exit 0) — hub is up
#   not-running         (exit 1) — no hub started
#   stale pid=12345     (exit 2) — pid file present but owner is dead

curl -s http://127.0.0.1:7700/health
# Expected: HTTP 200 with a JSON body. /health is the ONLY route that
# doesn't require the bearer token.
```

If `--status` says `stale`, run `vct-hub --start-if-not-running` — it cleans up the dead lockfile and spawns a fresh detached instance (idempotent; safe to run any time).

### `vct-hub` won't start

State files live under `<vct_root_dir>` (defaults to `~/.vct/`; override with `VCT_STATE_DIR`):

| File | Purpose | Expected mode (Unix) |
|---|---|---|
| `hub.pid` | Lockfile; first line is the running PID | `0o600` |
| `hub.port` | Bound TCP port (default 7700) | `0o644` |
| `hub.token` | Bearer token for `/api/v1/*` routes; regenerated on every startup | `0o600` |

**Lockfile won't release after a hard kill**:

```bash
cat ~/.vct/hub.pid          # see which PID it thinks owns the lock
ps -p $(cat ~/.vct/hub.pid) # verify the owner is actually alive
rm ~/.vct/hub.pid           # only if the PID is dead — vct-hub does this for you on next --start-if-not-running
vct-hub --start-if-not-running
```

The implementation (see `lockfile.rs`) reclaims a stale lockfile automatically when the recorded PID is no longer alive — manual `rm` is the workaround only if the file is corrupt (zero-byte, unparseable content).

**Port 7700 already in use**: the lockfile lookup succeeded but `TcpListener::bind` fails. Find the conflict and either stop it or move the hub:

```bash
# Linux / macOS
sudo lsof -i :7700
# Windows
netstat -ano | findstr :7700

# Move the hub to a different port (writes <vct_root_dir>/hub.port):
VCT_HUB_PORT=7701 vct-hub --start-if-not-running
```

Resolver clients (`vco_lib/project_config.py`, the bash/PowerShell helpers under `templates/scripts/vct_project_config.*`) auto-discover via `$VCT_HUB_PORT` → `<vct_root_dir>/hub.port` → `7700` default, so a moved port is picked up without further config.

**Token file permissions wrong**: vct-hub creates `hub.token` with mode `0o600` on Unix in a single open call (no chmod-after-create TOCTOU). If you see `Permission denied` reading it from a script, you likely launched the hub as a different user (e.g. root via sudo) than the script's caller. Fix: stop the hub, delete the file, restart as the right user — `vct-hub --stop && rm ~/.vct/hub.token && vct-hub --start-if-not-running`.

### Resolver returns 401 Unauthorized

Symptom: a script or wrapper calling `http://127.0.0.1:7700/api/v1/...` gets `401 Unauthorized` with `{"error":{"code":"unauthorized",...}}`.

Every `/api/v1/*` route (except `/health`) requires `Authorization: Bearer <token>` where the token lives in `<vct_root_dir>/hub.token`. The token is **regenerated on every hub startup** — so any client that cached an old token will 401 after a `vct-hub --stop` + restart.

- **Python clients** (`vco_lib.project_config`): auto-recover. The internal `_get_with_401_retry` wrapper catches a single 401, invalidates the 5-second discovery cache, re-reads `hub.token` + `hub.port` from disk, and re-issues the request. Subsequent 401s after the retry are surfaced as `HubUnreachable`. You don't need to do anything in calling code.
- **In-tree wrappers** (`claude_mcp_servers/search_mcp/wrapper.sh`, `vco` CLI, the `vct_secrets_resolve.{sh,ps1}` helpers): read the token per-call automatically — no extra config — but they don't auto-retry. If they 401, re-source / re-invoke them.
- **Custom bash / PowerShell scripts**: re-read `<vct_root_dir>/hub.token` per call (or per failure-and-retry). Don't cache the token across hub restarts. See `templates/scripts/vct_project_config.sh` and `templates/scripts/vct_project_config.ps1` for reference implementations of the discover-and-call pattern.
- **`hub.token` missing**: the hub hasn't started, or `VCT_STATE_DIR` differs between the hub and your client. `vct-hub --status` first; if `not-running`, start it with `vct-hub --start-if-not-running`.

### Boot autostart not firing

`vct-hub --register-boot` installs a per-user autostart unit. Default-OFF in v0.2.21 — users opt in via the launcher Preferences tab (or by running the CLI directly).

| OS | Mechanism | Location | Status check |
|---|---|---|---|
| Linux | systemd user unit | `$XDG_CONFIG_HOME/systemd/user/vct-hub.service` | `systemctl --user status vct-hub.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist` | `launchctl list \| grep vct-hub` |
| Windows | Scheduled Task `VCT-Hub` | (Task Scheduler library) | `Get-ScheduledTask -TaskName "VCT-Hub"` (PowerShell) or `schtasks /Query /TN "VCT-Hub" /V /FO LIST` |

```bash
vct-hub --register-boot     # idempotent; safe to re-run
vct-hub --boot-status       # 0=enabled, 1=disabled, 2=not-installed, 3=inspection error
vct-hub --unregister-boot   # removes the unit; idempotent
```

**Linux**: requires `systemctl` on PATH. On non-systemd distros (some musl-based, some container images) registration fails with `ToolNotFound`. Workaround: schedule via your init system manually with the binary path printed by `which vct-hub`.

**macOS — Gatekeeper rejects the boot agent**: `vct-hub` ships **UNSIGNED in v0.2.21** (code-signing + notarization is on the post-0.2.x backlog). launchd will load the plist, but Gatekeeper rejects the binary, leaving the agent in a permanent restart loop. The fix is the same as for any unsigned binary downloaded from the internet:

```bash
xattr -d com.apple.quarantine "$(which vct-hub)"
# or
xattr -dr com.apple.quarantine launcher/dist/macos-arm64/vct-hub
```

Then re-run `vct-hub --register-boot`. The `--register-boot` path emits a stderr warning when it detects an unsigned binary (`warn_if_unsigned`) — it does not refuse to register, because you may have signed it locally.

**Windows — Scheduled Task created but never fires**: check the task's last-run state:

```powershell
Get-ScheduledTask -TaskName "VCT-Hub" | Get-ScheduledTaskInfo
```

If `LastTaskResult` is non-zero, the boot shim (`vct-hub-boot.cmd` next to the binary) couldn't locate `vct-hub.exe`. Common cause: you moved the launcher install tree after registration. Re-register from the new location: `vct-hub --unregister-boot && vct-hub --register-boot`.

### Cutover sentinel stuck after a failed install

When `install.py` upgrades a v0.2.20 install to v0.2.21, it writes a sentinel at `<vct_root_dir>/v0.2.21-cutover.flag` **before** starting `vct-hub`. The v0.2.21 launcher reads this sentinel on startup and **skips** spawning its embedded `services::watcher` — the assumption is that `vct-hub` will take over supervision. `install.py` deletes the sentinel after `vct-hub` responds to `/health`.

If `install.py` is killed mid-cutover (Ctrl-C, OOM, power loss), the sentinel persists and the v0.2.21 launcher refuses to start its services watcher indefinitely — symptom: Weaviate/Ollama not auto-restarting when they crash.

**Auto-recovery (since v0.2.21 launcher startup)**: when the sentinel is older than **60 seconds AND** `vct-hub` is already reachable, the launcher deletes the sentinel itself and starts the embedded watcher (see `launcher/src-tauri/src/lib.rs` around the `cutover_sentinel_present` check). So in most cases, restarting the launcher fixes it.

**Manual recovery** (if auto-delete doesn't kick in — sentinel is fresh, or `vct-hub` is also down):

```bash
ls -la ~/.vct/v0.2.21-cutover.flag       # confirm presence
rm ~/.vct/v0.2.21-cutover.flag
vct-hub --start-if-not-running           # bring the hub back up
# Now restart the launcher GUI — the embedded watcher will spawn.
```

### Contributor: `cargo build --release --bin vct-hub` fails

Symptom: `error: no bin target named vct-hub in default-run packages` when building from the launcher workspace root.

The workspace root (`launcher/src-tauri/`) defines the `vct-launcher` binary; `vct-hub` is a workspace **member** at `launcher/src-tauri/vct-hub/`. Add `-p vct-hub` to select the right package:

```bash
cd launcher/src-tauri
cargo build --release -p vct-hub --bin vct-hub
```

Fixed in `scripts/build-bundled-launcher.sh` at commit `fe345a0` — pull/rebase if you're on an older clone.

## Embedding service / OpenAI key validation failures

Since v0.2.18 (`refactor(EmbeddingService) + OpenAI integration`, see `CHANGELOG.md`), the orchestrator can route text embeddings through either Ollama (local, default) or OpenAI (paid). The provider abstraction lives under `vco_lib/embedding_providers/`.

**"OpenAI key validation failed"** in the launcher Settings tab: validation hits `GET https://api.openai.com/v1/models/text-embedding-3-small` — a **free** endpoint per OpenAI's docs (no token billing for `GET /v1/models/<model>`). The probe accepts HTTP 200 (key works) and HTTP 429 (rate-limited but key is valid). Anything else — 401, 403, 404, network error — means the key really is bad.

- **401 / 403**: check the key prefix (`sk-…`) and project scope. Project-scoped keys (`sk-proj-…`) need the `text-embedding-3-small` model enabled in the project's model permissions.
- **404 on the model**: your account hasn't been granted access to `text-embedding-3-small`. Pick a different model in the launcher's Embeddings panel (e.g. `text-embedding-3-large`).
- **No billing involved in validation**: the call is `GET /v1/models/<model>`, not an embedding call. If your bank flags activity at activation time it's the launcher's separate seed embed-call against your first project — that one is billable (~1 cent).

The free Ollama path (`qwen3-embedding:0.6b`) is unaffected; switch back in the Embeddings panel if you want to disable the OpenAI route entirely.

## Shared KG looks empty after upgrading from <v0.2.12

In v0.2.12 the bundled shared collection was renamed from `VibeCodedTools_KnowledgeGraph` to `VibeCodedOrchestrator_KnowledgeGraph` (PR-26 / Group E). Fresh installs land on the new name; pre-v0.2.12 installs already have data under the old name and `hybrid_search` queries the new (empty) one.

Two paths:

1. **Designate the legacy class as canonical (recommended)**: launcher → Identity tab → "Manage shared KG collection" picker. It scans Weaviate for orchestrator-shaped classes and lets you point `SHARED_KG_COLLECTION` at the old one without renaming the underlying Weaviate class.
2. **Override the env directly**: edit `.claude/settings.json` `env` and set `SHARED_KG_COLLECTION=VibeCodedTools_KnowledgeGraph`. Restart Claude Code so MCP subprocesses see the new value.

See `docs/CONFIGURATION.md` → "Shared KG collection" for the full migration matrix.

## Update Bundle deferrals: `UPDATE_DEFERRED.md`

When you click "Update bundle" in the per-project Settings page (or run `python -m vco_lib.project_init install-bundle --update`), the orchestrator writes `<project>/.claude/context/UPDATE_DEFERRED.md` whenever an update step needs explicit user consent before continuing. The launcher toast surfaces the count ("5 files updated, 2 deferrals"); the file lists each deferral entry with the exact command to clear it.

The two deferral types you'll encounter most often:

**`bundle_user_modified_preserved`** — Update Bundle detected that one of your project files differs from the orchestrator's prior-shipped hash recorded in `.claude/.vco-manifest.json`. The orchestrator interpreted this as "user edits present" and preserved your version on disk rather than overwriting. Each preserved file appears in the deferral with the explicit force command (typically `python -m vco_lib.project_init install-bundle --update --force --file <path>`) that accepts the orchestrator's default for that file. Inspect your edits first; run the force command only if you want to discard them.

**`schema_migration_required`** — Update Bundle detected drift between the Weaviate target schema and the schema currently on disk for one of your project's collections. Because schema migration is destructive (it can re-embed or recreate collection objects), the bundle path never auto-applies it. The deferral entry shows the explicit consent command: `cd "$VCT_ORCHESTRATOR_ROOT" && .venv/bin/python -m vco_lib.project_init migrate-collections --name '<project>'` (the v0.2.19 fix made this work correctly from project venvs — previously it produced `ModuleNotFoundError`). Run it once you have a Weaviate backup or are comfortable with the migration's destructive scope.

The file lives at `<project>/.claude/context/UPDATE_DEFERRED.md` and is regenerated on each Update Bundle / `install.py --update` run. Cleared entries disappear automatically when the underlying condition resolves on the next run; you don't need to delete the file manually.

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

## Contributor: `cargo test` flakes on shared-state tests

Since v0.2.21 (Step 23), `launcher/src-tauri/.cargo/config.toml` pins `RUST_TEST_THREADS = "1"` workspace-wide. Background: a subset of tests across `auth`, `lockfile`, `boot`, `hub_status`, `hub_launcher`, `commands::installer::hub_stop_tests`, and `commands::self_update::state_roundtrip` mutate process-wide env vars (`VCT_STATE_DIR`, `HOME`, `VCT_HUB_PORT`). Even with the workspace-wide serialisation `Mutex` in `vct_launcher_core::test_env`, parallel runs can recycle PIDs under load and break "definitely-dead PID" assumptions.

**Cost**: ~2× wall time on `cargo test` (~80 s single-threaded vs ~40 s multi-threaded on a mid-tier laptop).

**Override for local parallel runs** (accepting the flake risk):

```bash
cd launcher/src-tauri
cargo test -- --test-threads=4
```

CI overrides explicitly via the `--test-threads` flag — see `.github/workflows/`. If you see a fresh flake on a test that doesn't touch global state, it's likely a real bug; file an issue.

### Contributor: `db::access::adopt_populated_tests` flaky on parallel runs (known issue PRE-2)

Three tests in `launcher/src-tauri/vct-launcher-core/src/db/access.rs` flake with ~20-30% probability regardless of `--test-threads`:

- `t2_vco_dev_shape_single_populated_candidate_adopted`
- `t3_multiple_populated_candidates_defer`
- `t5_idempotent_after_adoption`

Failure mode: the assertion compares `AdoptionReport { adopted, deferred, no_change }` and the observed report has counts off by one — typically `adopted: 0, no_change: 1` instead of `adopted: 1, no_change: 0`, or `adopted: 1` instead of `deferred: 1`.

**Root cause** (Track E investigation, 2026-06-10): the test fixture's hand-rolled `MockWeaviate` HTTP server does a single non-async `read()` on each accepted TCP stream. On heavily loaded systems the kernel can return BEFORE the full HTTP request body has arrived, the GraphQL class-name extraction returns empty, the count probe returns 0, and the production code takes a different branch than the test asserts. Full notes in `tests/KNOWN_FAILURES.md` § PRE-2.

**Recommended retry policy**: re-run the failing test up to 3× before treating it as a real regression. The tests pass reliably in isolation (`cargo test -p vct-launcher-core --lib t5_idempotent_after_adoption`) — the race only manifests under suite-level pressure.

**Real fix** is queued for v0.2.54: replace the hand-rolled mock with `httpmock` or wiremock-rs, or migrate the mock listener to async / tokio-aware TcpListener so it co-schedules cooperatively with the test's tokio runtime.

## Code embedding service on Apple Silicon / Intel macOS

The CodeSage-Large-v2 GPU build is shipped through `infrastructure/Dockerfile.cuda` (referenced from `install.py:22813`) and is selected automatically on hosts where the bootstrap envelope reports `gpu.vendor == "nvidia"` AND the NVIDIA Container Toolkit is reachable. The plain `Dockerfile` is the CPU-only image (sentence-transformers on CPU, slow).

Apple Silicon (`gpu.vendor == "metal"`) and Intel macOS do NOT have a Metal-accelerated build of the code-embedding service in v0.2.53. The fallback path is Ollama-served `qwen3-embedding:0.6b` for code embeddings (CPU). Configure by setting `CODE_EMBED_BACKEND=ollama` in `.claude/settings.json env` (see `docs/CONFIGURATION.md` → "Embedding configuration"). Code-graph search quality is lower than CodeSage on a CUDA host, but the system stays functional. Metal-backed code embeddings are on the v0.2.5x backlog; track via the `code-embed-metal` label on GitHub.

## SELinux: bind-mount layouts need a `:Z` flag

The bootstrap envelope's `package_manager_advice.selinux_volume_flag_needed` is true when the host is Fedora/RHEL/CentOS Stream with SELinux in enforcing mode (probed via `getenforce` then `/sys/fs/selinux/enforce`). On installs that use **named volumes** (the orchestrator's default), this is informational only — Podman / Docker manage the SELinux relabeling automatically. On installs that **swap in bind mounts** for any container (custom infrastructure layout, dev work, mounting a host directory into the Weaviate container), the bind-mount source needs the `:Z` flag added to the volume argument, or SELinux will deny the container's read/write attempts and the container will fail to start.

The install prints the hint when this condition is detected (see `install.py:7192` → `_print_selinux_bind_mount_hint`). The hint is NOT auto-applied — it is the user's call which mount to relabel and which to leave alone. Typical fix for a custom bind mount in `compose.yaml`:

```yaml
volumes:
  - ./mydir:/data:Z       # `:Z` tells Podman/Docker to relabel for private container access
```

Or, for shared access across multiple containers: `:z` (lowercase, shared label).

## RL retrieval reranking looks off (Pro/MAO) — run rl-doctor

If retrieval reranking (the paid RL module) seems to be underperforming, not
learning, or silently falling back to plain cosine order, run the built-in
diagnostic:

```bash
python claude_mcp_servers/scripts/rl_doctor.py     # from the orchestrator root
```

`rl-doctor` (v0.2.73, RL-12) reports RL retrieval health: whether the feature
gate is on for the project, whether telemetry (`rl_events`) is being written,
recent retrieval/citation rates, and the fallback counter (how often a rerank RPC
fell back to cosine). It reads the same state the pipeline writes, so a healthy
report means the loop is wired end-to-end; a fallback-heavy report points at the
reranker container or the license gate. Free-tier installs (no RL license) will
see the gate reported closed — that is expected, retrieval uses plain cosine.

## When asking for help

Attach `state/logs/bootstrap-prepass.json` from your install root to any issue or support request. The envelope captures OS / arch / GPU / RAM / tool versions / package-manager advice in a single file (schema at `docs/schemas/install-bootstrap-envelope-v1.json`); maintainers can read the host shape without an unstructured back-and-forth. The file contains no PII — every probe writes only the public-facing data (`pip --version`, `nvidia-smi -L`, etc.). It is regenerated on every `first-install.{sh,command,bat}` invocation.

For a deeper failure, also attach `state/logs/install.jsonl` (the structured per-step install log). `INSTALL_RECOVERY.md` documents its schema for AI-assisted recovery.

## Getting more help

- GitHub Issues: <https://github.com/hotak92/vibecoded-orchestrator/issues>
- Community channel: (TBD — linked from vibecodedtools.it at launch)
- Commercial support: Pro tier includes email support.
