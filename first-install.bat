@echo off
REM first-install.bat — VibeCoded Tools first-time installer (Windows)
REM
REM Double-clickable from Explorer; also runnable from cmd.exe or
REM PowerShell. Forwards to install.ps1, which already handles:
REM   - Python detection + auto-install via winget (or python.org URL
REM     fallback if winget absent)
REM   - WSL detection (redirects to install.sh inside the distro)
REM   - All install.py flags (passes through cmdline args)
REM
REM We use .bat (not .ps1) as the click target because:
REM   1. .ps1 files DON'T run on double-click by default — Windows
REM      blocks them under the default ExecutionPolicy (Restricted).
REM      A user double-clicking a .ps1 just opens it in Notepad.
REM   2. .bat files DO run on double-click out of the box.
REM   3. From a .bat we can spawn PowerShell with -ExecutionPolicy
REM      Bypass scoped to this one process — the user's machine-wide
REM      policy stays untouched.
REM
REM Status: STUB. Delegates to install.ps1. Tauri launcher build/launch
REM is post-v1.0 (tracked in plans/first-install-entry-points.md).

setlocal

REM Pin to the script directory so relative paths work even if invoked
REM via Explorer's "Run as Administrator" or from another working dir.
cd /d "%~dp0"

echo ===============================================
echo   VibeCoded Tools - First-Time Installer (Windows)
echo ===============================================
echo.
echo This will:
echo   - Install Python 3.11+ via winget if missing
echo   - Detect Podman/Docker; print install URLs if neither is present
echo   - Detect NVIDIA/CUDA drivers; recommend install if missing
echo   - Set up the orchestrator (~5-10 min)
echo.

REM Sanity check: install.ps1 must be alongside us.
if not exist "%~dp0install.ps1" (
    echo ERROR: install.ps1 not found alongside first-install.bat.
    echo        Make sure you ran first-install.bat from the cloned repo root.
    echo        Repo: https://github.com/hotak92/vibecoded-orchestrator
    pause
    exit /b 1
)

REM Prefer pwsh.exe (PowerShell 7+) when available — it's faster and
REM has fewer quirks than the bundled Windows PowerShell 5.1. Fall
REM back to powershell.exe (which ships with every Windows install
REM since Windows 7).
where /q pwsh.exe
if %ERRORLEVEL% EQU 0 (
    set "PSCMD=pwsh.exe"
) else (
    set "PSCMD=powershell.exe"
)

REM -ExecutionPolicy Bypass: process-scoped policy override so install.ps1
REM runs without modifying the machine policy. -NoProfile: skip the user's
REM PowerShell profile (faster startup; deterministic env). -File: run
REM the script. %* forwards all batch args to PowerShell.
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if %INSTALL_EXIT% EQU 0 (
    echo Install complete. To start the launcher: start-launcher.bat
) else (
    echo Install failed ^(exit %INSTALL_EXIT%^). See messages above.
)

REM Keep the cmd window open when run from Explorer double-click;
REM `pause` shows "Press any key to continue..." which is exactly
REM what we want. Harmless if run from terminal.
pause
exit /b %INSTALL_EXIT%

REM TODO(post-v1.0):
REM   - Build/install the Tauri launcher .exe + Start Menu shortcut.
REM   - Show a Windows Forms progress window during long-running steps
REM     (PowerShell + System.Windows.Forms is enough; no extra deps).
REM   - Sign the launcher .exe (Authenticode) to avoid SmartScreen
REM     warnings for end users.
