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
  `python install.py --update` (creates the task idempotently); OR opt
  in via the launcher GUI Preferences → "Auto-start containers on
  login" toggle.
- Task present but `Last Result: 0x80070002` → the script path it
  references is wrong (typically because the install root moved).
  Re-run `python install.py --update` to repair.

---

## File-watcher quirks (`PostToolUse` hooks fire twice on the same edit)

Windows file-system watchers occasionally fire two events for one
write (an `OnCreated` followed by `OnChanged` within milliseconds).
The orchestrator's hooks dedupe via a per-path debounce, but if you
see KG syncs running twice on every Edit, increase the debounce
window:

```
set VCO_HOOK_DEBOUNCE_MS=500    # default is 200
```

Persist via `setx VCO_HOOK_DEBOUNCE_MS 500` (no `=`) or set per-project
via `.claude/env`.

---

## Uninstall path

For now: re-run the install command with the `--uninstall` flag:

```cmd
python install.py --uninstall
```

This will:

- Remove the Scheduled Task `ClaudeMcpContainers`.
- Stop and remove the orchestrator-owned containers.
- Remove MCP registrations from `~/.claude.json` (additive: other MCPs
  preserved).
- Leave the `<install_root>/` directory and Weaviate volumes intact
  (deliberate — destructive deletion of vector data needs explicit
  consent).

To finish the cleanup manually after uninstall:

```cmd
rmdir /s /q "%USERPROFILE%\.vct"
podman volume rm weaviate_data    REM only if you're sure
```

---

## Quick links

- 6-item audit: [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md)
- Container troubleshooting: [`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md)
- Update / lockfile / sentinel issues: [`UPDATE-RECOVERY.md`](./UPDATE-RECOVERY.md)
- Diagnostic flow with Claude: [`CLAUDE-DIAGNOSTIC-PROMPT.md`](./CLAUDE-DIAGNOSTIC-PROMPT.md)
- Configuration env vars: `docs/CONFIGURATION.md`
