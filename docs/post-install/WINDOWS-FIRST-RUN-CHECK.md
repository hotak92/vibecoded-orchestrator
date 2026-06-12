# Windows First-Run Check — native-Windows (no WSL2) specifics

> **Audience**: anyone installing on Windows without WSL2. WSL2 users
> follow the Linux paths in the other recovery docs and most of this
> page doesn't apply to them.

The orchestrator supports native Windows. Some moving parts diverge
from the `*.sh` shipped to Linux/macOS — this page collects the gotchas
that bite first-run users.

---

## PowerShell version expectations

The shipped `.ps1` hooks and scripts target **PowerShell 5.1** (the one
that ships with every Windows install since Win10) AND **PowerShell 7+**.
Two practical implications:

1. **`.ps1` files MUST be saved as UTF-8 with BOM** (or US-ASCII).
   PowerShell 5.1's default encoding is UTF-16-LE; if your editor
   accidentally re-saved a hook in UTF-8 *without* BOM, PS 5.1 will
   misparse non-ASCII characters in the file. Symptom: hooks fire but
   silently corrupt their output.
2. **Execution policy**: `Get-ExecutionPolicy -List` should show at
   least `RemoteSigned` or `Bypass` for `CurrentUser`. Default
   `Restricted` blocks every `.ps1` hook. Fix:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   ```

---

## `.bat` vs `.sh` for first-install

The Windows first-install path is `first-install.bat` (NOT
`first-install.sh`). It internally calls PowerShell for the actual
work — the `.bat` exists because double-clicking it from File Explorer
works in cmd.exe, where `.ps1` would prompt for "what to open with".

Common cmd.exe parse traps the `.bat` works around but you may hit
directly:

- Paths with spaces: always quote (`"C:\Program Files\foo\..."`).
- `%` in literal strings: escape as `%%` inside `.bat`, single `%` in
  PowerShell.
- Long paths (>260 chars): enable `LongPathsEnabled` in registry OR
  install to a short path like `C:\vco\`.

---

## Hook parity: every `.sh` has a `.ps1` sibling

The shipped `.claude/hooks/` directory contains both `.sh` and `.ps1`
versions of every hook. On Windows without WSL2, only the `.ps1`
versions fire. To verify the parity is intact after a bundle update:

```powershell
Get-ChildItem .claude\hooks\*.sh | ForEach-Object {
    $ps = $_.FullName -replace '\.sh$', '.ps1'
    if (-not (Test-Path $ps)) { "MISSING: $ps" }
}
```

If this prints any "MISSING" lines, re-run the bundle update from the
install root:

```cmd
python install.py --update
```

(Or from PowerShell: `python install.py --update`.) The bundle's
manifest-driven install will recreate any missing siblings.

---

## Boot service (Scheduled Task)

On Linux this is a systemd-user unit; on macOS a LaunchAgent; on
Windows a Scheduled Task named **`ClaudeMcpContainers`**.

**Inspect**:

```cmd
schtasks /Query /TN ClaudeMcpContainers /V /FO LIST
```

Look for `Status: Ready` (not `Disabled`) and a `Next Run Time`
populated.

**Common failures**:

- Task absent → install never ran the boot-registration step. Re-run
  `python install.py --update` (creates the task idempotently — skipped
  only when `VCT_DISABLE_BOOT_SERVICE=1` or `--no-containers` is set).
- Task present but `Last Result: 0x80070002` → the script path it
  references is wrong (typically because the install root moved).
  Re-run `python install.py --update` to repair.

---

## Uninstall path

Re-run the install command with the `--uninstall` flag:

```cmd
python install.py --uninstall
```

> ⚠️ **Data-loss warning — read before running.** Container-volume
> removal (your Weaviate KG vectors + Ollama models + code embeddings)
> is the DEFAULT in the uninstall plan. You must pass **`--keep-data`**
> to preserve the volumes. If you want to keep your KG vectors, back up
> the Weaviate volume (`vco_weaviate_data`) BEFORE uninstalling, or run
> with `--keep-data`.

What it does, step by step:

- Stops the orchestrator-owned containers (`compose down` — this step
  alone preserves volumes).
- **Volume removal (default unless `--keep-data`)**: the uninstaller
  prints the exact destructive commands
  (`<runtime> compose down --volumes`, or per-volume `volume rm`) for
  you to run — it deliberately does not invoke `volume rm` itself
  (defense-in-depth audit rule). Treat the printed commands as the
  intended cleanup: running them deletes your KG data.
- Removes the launcher state DB (`~/.vct/launcher.db`).
- Removes orchestrator MCP registrations from `~/.claude.json` (your
  other MCP servers are preserved).
- Never touches `~/.vct-secrets/` or your source code.

Flags that change confirmation behavior — be explicit about these:

- **`--yes`** (or running non-interactively / piped stdin) **skips every
  confirmation prompt** — all steps proceed as `[auto-yes]` with no
  chance to back out. Combine `--yes` WITHOUT `--keep-data` only if you
  have already backed up or deliberately want the data gone.
- **`--dry-run`** prints the full plan and exits without removing
  anything. Run this first if unsure.
- **`--keep-data`** skips the volume-removal step entirely.

**Known gap**: the Scheduled Task registered at install time
(`ClaudeMcpContainers`) is NOT automatically removed by `--uninstall`
in v0.2.54. To remove it manually after uninstall:

```cmd
schtasks /Delete /TN ClaudeMcpContainers /F
```

(Track G in a later release will add automatic boot-service
unregister.)

To finish the cleanup manually after uninstall (only if you're sure —
this deletes your KG vectors):

```cmd
rmdir /s /q "%USERPROFILE%\.vct"
podman volume rm vco_weaviate_data    REM destroys KG vectors — back up first
```

---

## Quick links

- 6-item audit: [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md)
- Container troubleshooting: [`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md)
- Update / lockfile / sentinel issues: [`UPDATE-RECOVERY.md`](./UPDATE-RECOVERY.md)
- Diagnostic flow with Claude: [`CLAUDE-DIAGNOSTIC-PROMPT.md`](./CLAUDE-DIAGNOSTIC-PROMPT.md)
- Configuration env vars: `docs/CONFIGURATION.md`
