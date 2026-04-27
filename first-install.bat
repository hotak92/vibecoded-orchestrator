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

REM EnableDelayedExpansion so we can use !VAR! inside if/else blocks
REM (CMD's standard %VAR% expansion happens at parse-time, which breaks
REM when reading user input inside a (...) block).
setlocal enabledelayedexpansion

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
REM Sniff for our own flags. We accept --no-auto-launch (skip GUI auto-spawn
REM at the end) and --yes (silent / non-interactive). Pass-through everything
REM else to install.ps1.
set "NO_AUTO_LAUNCH=0"
set "YES_FLAG=0"
for %%A in (%*) do (
    if /I "%%~A"=="--no-auto-launch" set "NO_AUTO_LAUNCH=1"
    if /I "%%~A"=="--yes"             set "YES_FLAG=1"
    if /I "%%~A"=="--non-interactive" set "YES_FLAG=1"
    if /I "%%~A"=="--quiet"           set "YES_FLAG=1"
)

"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if %INSTALL_EXIT% NEQ 0 (
    echo Install failed ^(exit %INSTALL_EXIT%^). See messages above.
    pause
    exit /b %INSTALL_EXIT%
)

REM ---------------------------------------------------------------------------
REM Post-install: ensure the launcher binary is available, then auto-launch.
REM Equivalent to scripts/post-install-launcher.sh but inline (CMD batch is
REM a different language; sharing the .sh is not viable on Windows).
REM
REM Pre-installed-tool assumptions (USER CONSTRAINT):
REM   - cmd.exe / PowerShell 5.x: ship with Windows since Win7. OK.
REM   - curl: ships as a cmd alias since Win10 1803. We probe; fall back
REM     to PowerShell Invoke-WebRequest if absent.
REM   - python3: NOT pre-installed; install.ps1 already auto-installs it
REM     before we get here (so guaranteed at this point).
REM   - winget: pre-installed Win11 + recent Win10 only. Detect; URL
REM     fallback if absent.
REM ---------------------------------------------------------------------------

set "LAUNCHER_BIN="
REM First-match-wins probe.
if exist "%~dp0launcher\src-tauri\target\release\vct-launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe"
) else if exist "%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe" (
    set "LAUNCHER_BIN=%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe"
) else if exist "%LOCALAPPDATA%\vct-launcher\vct-launcher.exe" (
    set "LAUNCHER_BIN=%LOCALAPPDATA%\vct-launcher\vct-launcher.exe"
)

if defined LAUNCHER_BIN (
    echo [launcher] Found existing binary: %LAUNCHER_BIN%
    goto :auto_launch
)

REM Binary not found — present the same 3-way menu as the bash helper.
echo.
echo ===============================================
echo   Launcher binary not found. Choose how to get it:
echo ===============================================
echo   [1] Download prebuilt ^(recommended^) - fast, ~30 MB
echo        ^(downloads vct-launcher-windows-x64.exe from latest GitHub Release^)
echo   [2] Build from source - slower, requires Node + Tauri toolchain
echo        ^(auto-installs Node via winget if missing, then runs pnpm tauri build^)
echo   [3] Skip ^(build later manually^)
echo.

set "CHOICE=1"
if "%YES_FLAG%"=="1" (
    echo [launcher] --yes / non-interactive: defaulting to [1] download
) else (
    set /p "CHOICE=Your choice [1]: "
    if not defined CHOICE set "CHOICE=1"
)

if "%CHOICE%"=="3" goto :launch_skip
if "%CHOICE%"=="2" goto :launch_build
goto :launch_download

:launch_download
echo.
echo [launcher] Downloading prebuilt launcher...
set "DEST_DIR=%LOCALAPPDATA%\vct-launcher"
if not exist "%DEST_DIR%" mkdir "%DEST_DIR%"
set "DEST_EXE=%DEST_DIR%\vct-launcher.exe"

REM Use PowerShell to query GitHub Releases API and download. We avoid
REM cmd-only tools (curl / Invoke-WebRequest) so this works on Win10+
REM regardless of curl alias presence.
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "try {" ^
    "  $api='https://api.github.com/repos/hotak92/vibecoded-orchestrator/releases/latest';" ^
    "  $r=Invoke-RestMethod -Uri $api -UseBasicParsing;" ^
    "  $a=$r.assets | Where-Object { $_.name -like '*windows*' -and $_.name.EndsWith('.exe') } | Select-Object -First 1;" ^
    "  if (-not $a) { Write-Host 'NO_ASSET'; exit 2 }" ^
    "  Write-Host ('Downloading ' + $a.name);" ^
    "  Invoke-WebRequest -Uri $a.browser_download_url -OutFile '%DEST_EXE%' -UseBasicParsing;" ^
    "  $size=(Get-Item '%DEST_EXE%').Length;" ^
    "  if ($size -lt 10MB) { Write-Host 'TOO_SMALL'; exit 3 }" ^
    "  Write-Host ('Downloaded {0:N1} MB' -f ($size/1MB))" ^
    "} catch { Write-Host ('ERR: ' + $_.Exception.Message); exit 1 }"
set "DL_EXIT=%ERRORLEVEL%"

if "%DL_EXIT%"=="0" (
    set "LAUNCHER_BIN=%DEST_EXE%"
    goto :auto_launch
)
echo [launcher] Download failed ^(exit %DL_EXIT%^). Falling back to build.
goto :launch_build

:launch_build
echo.
echo [launcher] Building from source...

REM Ensure Node.js. T1 silent (only with --yes), T3 prompt, T4 URL.
where /q node
if %ERRORLEVEL% NEQ 0 (
    where /q winget
    if %ERRORLEVEL% EQU 0 (
        if "%YES_FLAG%"=="1" (
            echo [launcher] T1 silent: winget install Node.js
            winget install --silent --accept-package-agreements --accept-source-agreements OpenJS.NodeJS
        ) else (
            set /p "NODE_ANS=Install Node.js via winget? [Y/n] "
            if /I "!NODE_ANS!"=="N" (
                echo [launcher] T4: install Node manually from https://nodejs.org/
                goto :launch_skip
            )
            winget install --accept-package-agreements --accept-source-agreements OpenJS.NodeJS
        )
        REM winget modifies PATH for new shells but not us — refresh.
        call refreshenv >nul 2>&1
    ) else (
        REM No winget — old Win10 build, Server, or removed by IT policy.
        REM Loud-stop: tell user what to install + give a re-check option.
        echo.
        echo ===============================================
        echo   Cannot auto-install Node: winget is not available
        echo ===============================================
        echo.
        echo   Install Node.js manually:
        echo     https://nodejs.org/  ^(LTS .msi, 18+ recommended^)
        echo   Or get winget first:  https://aka.ms/getwinget
        echo.
        echo   IMPORTANT: After installing Node, OPEN A NEW TERMINAL so PATH
        echo   refreshes, then re-run this script. Or choose [r] to retry.
        echo.
        if "%YES_FLAG%"=="1" (
            echo [launcher] Non-interactive run: skipping.
            goto :launch_skip
        )
        :winget_recheck
        set /p "WG_RETRY=[r] Re-check / [s] Skip the build: "
        if /I "!WG_RETRY!"=="r" (
            where /q node
            if %ERRORLEVEL% EQU 0 (
                echo [launcher] Node detected — continuing.
                goto :node_install_done
            ) else (
                echo [launcher] Still no Node on PATH. Try a new terminal.
                goto :winget_recheck
            )
        ) else (
            goto :launch_skip
        )
        :node_install_done
    )
)

where /q node
if %ERRORLEVEL% NEQ 0 (
    REM Loud-stop pattern (parity with Linux/macOS post-install-launcher.sh).
    REM Don't silently skip and report "Installation complete!" while the
    REM launcher build was actually skipped — that's the same anti-pattern
    REM as the Joern silent-hang. Tell the user exactly what to do.
    echo.
    echo ===============================================
    echo   Cannot build the launcher: Node.js is missing
    echo ===============================================
    echo.
    echo   Auto-install attempts failed. Install Node.js manually:
    echo     https://nodejs.org/  ^(LTS, 18+ recommended^)
    echo   Or via winget:  winget install OpenJS.NodeJS
    echo.
    echo   IMPORTANT: After installing, OPEN A NEW TERMINAL so PATH refreshes,
    echo   then re-run this script. Or choose [r] to retry from this terminal
    echo   ^(may not see new PATH^).
    echo.
    if "%YES_FLAG%"=="1" (
        echo [launcher] Non-interactive run: skipping. Re-run with Node available.
        goto :launch_skip
    )
    :node_recheck
    set /p "NODE_RETRY=[r] Re-check / [s] Skip the build: "
    if /I "!NODE_RETRY!"=="r" (
        where /q node
        if %ERRORLEVEL% EQU 0 (
            echo [launcher] Node detected — continuing build.
        ) else (
            echo [launcher] Still no Node on PATH. Try opening a new terminal.
            goto :node_recheck
        )
    ) else (
        goto :launch_skip
    )
)

REM pnpm preferred, npm fallback.
where /q pnpm
if %ERRORLEVEL% NEQ 0 (
    echo [launcher] pnpm not found. Installing via npm...
    call npm install -g pnpm
)

cd /d "%~dp0launcher"
where /q pnpm
if %ERRORLEVEL% EQU 0 (
    echo [launcher] [3/4] pnpm install
    call pnpm install
    echo [launcher] [4/4] tauri build ^(this takes 5-15 min^)
    call pnpm tauri build
) else (
    echo [launcher] [3/4] npm install
    call npm install
    echo [launcher] [4/4] tauri build ^(this takes 5-15 min^)
    call npx tauri build
)
cd /d "%~dp0"

REM Re-probe.
if exist "%~dp0launcher\src-tauri\target\release\vct-launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe"
)
if not defined LAUNCHER_BIN (
    REM Loud-stop: build "succeeded" per its exit code but no .exe is present.
    REM This usually means Tauri produced an error we missed. Tell the user
    REM what to look for.
    echo.
    echo ===============================================
    echo   Build reported success but no .exe was found
    echo ===============================================
    echo.
    echo   Expected one of:
    echo     %~dp0launcher\src-tauri\target\release\vct-launcher.exe
    echo     %~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe
    echo.
    echo   This usually means a Tauri build dependency is missing.
    echo   See: https://tauri.app/start/prerequisites/
    echo   ^(Windows: Visual Studio Build Tools + WebView2 + Rust toolchain^)
    echo.
    goto :launch_skip
)
goto :auto_launch

:launch_skip
echo.
echo [launcher] Launcher build was skipped or failed.
echo [launcher] To build later, open a terminal in the repo root and run:
echo            cd launcher
echo            pnpm install ^(or npm install^)
echo            pnpm tauri build
echo            ..\start-launcher.bat
echo.
goto :end

:auto_launch
if "%NO_AUTO_LAUNCH%"=="1" (
    echo.
    echo [launcher] --no-auto-launch set. Run start-launcher.bat to open the GUI.
    goto :end
)
echo.
echo Installation complete. Opening launcher...
REM `start "" ...`: empty string is the WINDOW TITLE arg required by `start`
REM when the path is quoted; without it cmd treats the quoted path as the
REM title. Detached spawn — this cmd window can close without killing the GUI.
start "" "%LAUNCHER_BIN%"

:end
REM Keep the cmd window open when run from Explorer double-click;
REM `pause` shows "Press any key to continue..." which is exactly
REM what we want. Harmless if run from terminal.
pause
exit /b 0

REM TODO(post-v1.0):
REM   - Show a Windows Forms progress window during long-running steps
REM     (PowerShell + System.Windows.Forms is enough; no extra deps).
REM   - Sign the launcher .exe (Authenticode) to avoid SmartScreen
REM     warnings for end users.
