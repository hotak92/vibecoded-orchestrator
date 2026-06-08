# macOS install — best-effort (v0.2.40+)

This doc covers the first-launch user experience on macOS plus auto-start setup, troubleshooting, and uninstall. It is the canonical reference linked from the README, release notes, and the `release.yml` workflow header comment.

## TL;DR

- **Apple Silicon (M1/M2/M3/M4) only.** Intel x86_64 builds are NOT shipped — see "Why no Intel build?" below.
- Binaries are **ad-hoc codesigned** (`codesign --force --deep --sign -`) but NOT signed with an Apple Developer ID. Gatekeeper warns on first launch; you bypass it once and the binary runs normally afterwards.
- Full Developer ID + notarization is **deferred** to a follow-up patch once Apple credentials are provisioned. Until then, you'll see the "can't be verified by Apple" dialog the first time.

## Supported targets

| Target | Status | Why |
|---|---|---|
| `aarch64-apple-darwin` (Apple Silicon) | **Shipped** | GitHub-hosted `macos-latest` runner produces this natively. |
| `x86_64-apple-darwin` (Intel) | **Not shipped** | The `macos-13` GitHub-runner image was fully deprecated 2025-12-04; v0.1.5's release matrix entry queued for 23h47m with no runner pickup. Apple stopped selling Intel Macs in late 2023. Revisit if user demand surfaces — cross-compile path is `--target x86_64-apple-darwin` from the arm64 runner. |

## First launch (Gatekeeper bypass)

VCT was not signed with an Apple Developer ID; the build is ad-hoc signed for structural integrity but has no trust anchor in Apple's PKI. On first launch Gatekeeper will show one of two dialogs depending on your macOS version:

- macOS 14 (Sonoma) and earlier: **"vct-launcher cannot be opened because Apple cannot check it for malicious software."**
- macOS 15 (Sequoia) and later: **"vct-launcher Not Opened" / "Apple could not verify ..."**

Both dialogs are routine for ad-hoc-signed binaries. To bypass:

### Method 1 — Right-click → Open (Finder, simplest)

1. Open Finder, locate `vct-launcher` (or `vct-launcher.app` if you've wrapped it).
2. **Right-click** (or Control-click) → **Open**.
3. The dialog now offers an explicit **Open** button (vs. just "Cancel"). Click it.
4. macOS records your explicit approval; subsequent launches skip the warning.

Important: double-clicking from Finder does NOT show the "Open anyway" option on Sequoia+ — you MUST use the right-click route to surface it.

### Method 2 — `xattr` (terminal, scriptable)

If you've downloaded the binary outside Finder (curl, wget, scp) it may or may not carry the `com.apple.quarantine` extended attribute. Strip it explicitly:

```bash
xattr -d com.apple.quarantine /path/to/vct-launcher
xattr -d com.apple.quarantine /path/to/vct-hub
```

This is idempotent (an `xattr: ... No such xattr` error means the quarantine flag wasn't set — safe to ignore).

### Method 3 — System Settings → Privacy & Security (macOS 13+)

After macOS blocks the first launch, the warning dialog leaves an entry under **System Settings → Privacy & Security**. Scroll to the bottom, find the "vct-launcher was blocked..." line, click **Open Anyway**. Confirms in a second dialog and runs.

## Auto-start (vct-hub LaunchAgent)

`vct-hub` is the detached background service that resolves per-project configuration for Claude Code hooks and MCP servers. On Linux it runs as a systemd-user unit; on Windows it's a Scheduled Task; on macOS it's a per-user LaunchAgent.

### Preferred install — launcher-driven (recommended)

```bash
vct-hub --register-boot
```

This renders the canonical plist template baked into the binary and writes it to `~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist`, then enables-and-starts the agent via `launchctl bootstrap` + `launchctl kickstart`. Re-running is idempotent.

Verify status:

```bash
vct-hub --boot-status
# Exit 0 = enabled, 1 = disabled, 2 = not installed, 3 = inspection error.

launchctl list | grep vct
# Should show: PID  status  com.vibecodedtools.vct-hub
```

To unregister:

```bash
vct-hub --unregister-boot
```

### Fallback install — manual (no launcher available)

If you can't run `vct-hub --register-boot` (no launcher binary, permissions issue, custom layout, etc.), use the template at `templates/scripts/launchctl-plist.template`. Substitute the two placeholders:

| Placeholder | Replace with |
|---|---|
| `__VCT_HUB_BIN__` | Absolute path to `vct-hub` (e.g. `/usr/local/bin/vct-hub` or `$HOME/.vct/bin/vct-hub`) |
| `__VCT_STATE_DIR__` | Absolute path to your VCT state directory (default `$HOME/.vct`) |

Then:

```bash
# Substitute placeholders (example with sed; adjust paths to your install).
VCT_HUB_BIN="$HOME/.vct/bin/vct-hub"
VCT_STATE_DIR="$HOME/.vct"
sed -e "s|__VCT_HUB_BIN__|$VCT_HUB_BIN|g" \
    -e "s|__VCT_STATE_DIR__|$VCT_STATE_DIR|g" \
    templates/scripts/launchctl-plist.template \
  > ~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist

# Load + start.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist
launchctl kickstart -k gui/$(id -u)/com.vibecodedtools.vct-hub

# Verify.
launchctl print gui/$(id -u)/com.vibecodedtools.vct-hub | head -20
```

The plist's `KeepAlive { SuccessfulExit = false }` means launchd restarts the hub on abnormal exit but NOT on a clean `vct-hub --stop`, so you can shut down the hub without launchd respawning it.

## Troubleshooting

### "vct-launcher won't open" / Gatekeeper dialog keeps appearing

1. Confirm the binary is ad-hoc signed: `codesign --verify --verbose=2 /path/to/vct-launcher`. Exit 0 = signature parses. If this fails, your download is corrupted — re-download from the GitHub Release sidebar.
2. Strip the quarantine attribute explicitly: `xattr -d com.apple.quarantine /path/to/vct-launcher`. The "approve once" UI flow doesn't always persist on older macOS versions; this is the reliable path.
3. If you've already approved it once but it still warns, check **System Settings → Privacy & Security** for a residual block entry and clear it.

### "Hub doesn't auto-start at login"

1. `vct-hub --boot-status` — confirms whether the LaunchAgent is registered. Exit 2 = "not installed" (run `--register-boot`); exit 1 = "disabled" (the plist is there but `launchctl` won't run it; usually means a manual `launchctl bootout` happened); exit 3 = inspection error (check stderr).
2. `launchctl list | grep vct` — confirms whether the agent is loaded into your user's launchd session. Empty result = agent file present but not loaded; re-run `vct-hub --register-boot`.
3. Check the LaunchAgent's stderr log: `tail -50 $HOME/.vct/logs/vct-hub.launchd.err`. The path comes from `__VCT_STATE_DIR__` in the plist — substituted at registration time.
4. Verify the plist's `__VCT_HUB_BIN__` path still resolves. If you moved the binary after registering, the plist's embedded path is stale. Re-register (`--unregister-boot` then `--register-boot`).

### "Container runtime not found"

LaunchAgents do NOT inherit your shell's `PATH`. The plist sets a baseline of `/usr/local/bin:/opt/homebrew/bin:/opt/podman/bin:/usr/bin:/bin:/usr/sbin:/sbin`. If your `podman` or `docker` lives elsewhere (e.g. you installed Podman via a custom prefix), edit the plist's `EnvironmentVariables → PATH` and `launchctl bootout` / `bootstrap` the agent to reload it.

If neither Podman nor Docker Desktop is installed, install one:

- Podman (recommended, open-source): `brew install podman && podman machine init && podman machine start`
- Docker Desktop: download from <https://www.docker.com/products/docker-desktop/>.

### "GPU passthrough doesn't work"

Apple Silicon Macs cannot pass the Metal GPU through to containers. The orchestrator's compose files detect this and degrade to CPU-only — same as the Linux fallback when CDI is absent. Workloads that need GPU (Ollama embeddings, the optional code-embed service) will run on CPU at degraded speed. This is a platform limitation, not a VCT bug.

### "The `vct-hub` LaunchAgent crashes in a loop"

The plist's `ThrottleInterval = 10` cap means launchd won't restart faster than once per 10s. If the agent is crashlooping:

1. `tail -100 $HOME/.vct/logs/vct-hub.launchd.err` — read the crash message.
2. Common causes: port 7700 already in use (another `vct-hub` left running from a previous session), `~/.vct/launcher.db` corrupted (rename it; `vct-hub` will recreate on next start), state-dir permissions wrong.
3. To stop the loop while debugging: `launchctl bootout gui/$(id -u)/com.vibecodedtools.vct-hub`. To resume: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist`.

## Uninstall

```bash
# 1. Unregister the LaunchAgent (preferred).
vct-hub --unregister-boot
# OR manually:
launchctl bootout gui/$(id -u)/com.vibecodedtools.vct-hub
rm ~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist

# 2. Remove the binaries (paths depend on where you put them).
rm -f /usr/local/bin/vct-launcher /usr/local/bin/vct-hub /usr/local/bin/vco
rm -f "$HOME/.vct/bin/vct-launcher" "$HOME/.vct/bin/vct-hub" "$HOME/.vct/bin/vco"

# 3. Remove the state directory (KG/code-graph embeddings, launcher.db, logs).
#    WARNING: this drops all per-project KG state. Skip if you might reinstall.
rm -rf "$HOME/.vct"

# 4. (Optional) Remove the per-user Claude Code config registrations.
#    Use the launcher's "Uninstall" flow before purging this if possible —
#    it knows which MCP entries to remove without breaking other tools.
#    Manual edit if needed: ~/.claude.json (mcpServers section).
```

## Why no Intel build?

The `macos-13` GitHub-runner image was fully deprecated 2025-12-04 (actions/runner-images #13046). The v0.1.5 release run's Intel matrix entry queued for 23h47m with no runner pickup, then aborted. We removed the entry on 2026-05-01.

Cross-compiling from the arm64 runner with `--target x86_64-apple-darwin` is technically possible but:

- Tauri's bundling chain plus the SvelteKit frontend embedding have repeatedly had cross-target paper cuts on this path; locking it in for v0.2.x without a real Intel-Mac smoke test would be irresponsible.
- Apple stopped selling Intel Macs in late 2023. The active cohort is shrinking month-over-month; for a tier-2 alpha, it's not worth blocking releases on.

If you have an Intel Mac and need a build, the build script `scripts/build-bundled-launcher.sh` runs identically on Intel Darwin — you can build locally:

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator
cd vibecoded-orchestrator
# Install Rust toolchain + Node 20+ + pnpm first.
bash scripts/build-bundled-launcher.sh
# Binary lands at launcher/dist/macos-x64/vct-launcher.
codesign --force --deep --sign - launcher/dist/macos-x64/vct-launcher
# Then run via the right-click → Open flow above.
```

## Related references

- Canonical plist template (rendered by `vct-hub --register-boot`): `launcher/src-tauri/vct-hub/templates/com.vibecodedtools.vct-hub.plist.template`
- Linux systemd-user equivalent: `launcher/src-tauri/vct-hub/templates/vct-hub.service.template`
- Windows Scheduled Task equivalent: `launcher/src-tauri/vct-hub/templates/vct-hub-task.xml.template`
- Manual-fallback macOS template (this doc's `templates/scripts/launchctl-plist.template` companion)
- Release workflow header (codesign rationale): `.github/workflows/release.yml` (top comment block, lines ~38-48)
- Boot module source (`vct-hub::boot::macos`): `launcher/src-tauri/vct-hub/src/boot.rs`
