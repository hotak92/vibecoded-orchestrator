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
REM Status: full entry point. Delegates the core install to
REM install.ps1 (which runs install.py), then handles the post-install
REM launcher phase inline: bundled-binary probe + staleness check,
REM prebuilt download, source-build fallback, desktop shortcut, and GUI
REM auto-spawn. (An earlier revision of this header said "STUB ~100 LoC"
REM — that was stale; corrected v0.2.54 G-1.)

REM EnableDelayedExpansion so we can use !VAR! inside if/else blocks
REM (CMD's standard %VAR% expansion happens at parse-time, which breaks
REM when reading user input inside a (...) block).
setlocal enabledelayedexpansion

REM Pin to the script directory so relative paths work even if invoked
REM via Explorer's "Run as Administrator" or from another working dir.
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM Help / usage (v0.2.54 G-1 — W-P1-3 regression fix).
REM
REM CI's "Windows entry-point parser smoke" invokes `first-install.bat /help`
REM and expects usage text + exit 0 with ZERO side effects. Before this
REM handler existed, `/help` fell through %* into install.ps1 (which has no
REM help handling either) and started a REAL install — ~3 minutes of side
REM effects on the CI runner, then a non-zero exit. Accept the
REM cmd.exe-idiomatic forms (/help, /h, /?) and the cross-platform forms
REM (--help, -h, -?). Help is a first-argument contract, matching
REM install.py's argparse behaviour.
REM ---------------------------------------------------------------------------
if /I "%~1"=="/help"  goto :show_help
if /I "%~1"=="/h"     goto :show_help
if "%~1"=="/?"        goto :show_help
if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if "%~1"=="-?"        goto :show_help
goto :after_help

:show_help
echo Usage: first-install.bat [options]
echo.
echo First-time installer for VibeCoded Tools on Windows. Runs install.ps1
echo with a process-scoped ExecutionPolicy bypass, then acquires + launches
echo the VCT Launcher GUI.
echo.
echo Options handled by this script:
echo   /help, --help, -h     Show this help and exit.
echo   --yes                 Non-interactive: accept defaults, no prompts.
echo   --non-interactive     Same as --yes.
echo   --no-auto-launch      Skip the post-install launcher phase entirely
echo                         - no binary download/build, no GUI spawn.
echo                         Run start-launcher.bat later instead.
echo   --no-desktop-icon     Skip desktop + Start Menu shortcut creation.
echo.
echo All other options are forwarded to install.ps1 / install.py, e.g.:
echo   --no-containers --skip-models --cpu-only --low-resource --update
echo Run: powershell -File install.ps1 for the full install.py flag list,
echo or see docs\GETTING_STARTED.md.
exit /b 0

:after_help
echo ===============================================
echo   VibeCoded Tools - First-Time Installer (Windows)
echo ===============================================
echo.
echo This will:
echo   - Auto-install Python 3.11+, Node.js 18+, and Podman via winget if missing
echo     (interactive prompt before any winget invocation)
echo   - Auto-start the Podman machine (podman machine start) if installed but stopped
echo     (deferral written to UPDATE_DEFERRED.md if start fails or machine not initialized)
echo   - Detect NVIDIA/CUDA drivers and print install hints (drivers stay manual)
echo   - Set up the orchestrator (~5-10 min)
echo.

REM Sniff for our own flags BEFORE any pause-able path (v0.2.54 Track W:
REM the sniff used to live below the sanity check, so an unattended --yes
REM run against a broken clone would hang forever on that pause). We accept
REM --no-auto-launch (skip GUI auto-spawn at the end) and --yes (silent /
REM non-interactive). Pass-through everything else to install.ps1.
set "NO_AUTO_LAUNCH=0"
set "NO_DESKTOP_ICON=0"
set "YES_FLAG=0"
for %%A in (%*) do (
    if /I "%%~A"=="--no-auto-launch" set "NO_AUTO_LAUNCH=1"
    if /I "%%~A"=="--no-desktop-icon" set "NO_DESKTOP_ICON=1"
    if /I "%%~A"=="--yes"             set "YES_FLAG=1"
    if /I "%%~A"=="--non-interactive" set "YES_FLAG=1"
    if /I "%%~A"=="--quiet"           set "YES_FLAG=1"
)

REM Sanity check: install.ps1 must be alongside us.
REM (pause gated on YES_FLAG so unattended runs exit instead of hanging)
if not exist "%~dp0install.ps1" (
    echo ERROR: install.ps1 not found alongside first-install.bat.
    echo        Make sure you ran first-install.bat from the cloned repo root.
    echo        Repo: https://github.com/hotak92/vibecoded-orchestrator
    if "%YES_FLAG%"=="0" pause
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
REM the script. %* forwards all batch args to PowerShell. (Our own flag
REM sniffing happens earlier, before the sanity check.)

"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if %INSTALL_EXIT% NEQ 0 (
    echo Install failed ^(exit %INSTALL_EXIT%^). See messages above.
    if "%YES_FLAG%"=="0" pause
    exit /b %INSTALL_EXIT%
)

REM ---------------------------------------------------------------------------
REM --no-auto-launch parity with first-install.sh / first-install.command
REM (v0.2.54 G-1). On POSIX the flag skips the ENTIRE post-install launcher
REM phase: scripts/post-install-launcher.sh is never invoked, so no binary
REM probe, no download, no source build, no spawn. This .bat previously
REM honored the flag only at the final :auto_launch gate — the binary
REM acquisition still ran, which in CI meant a multi-minute pnpm + tauri
REM source build on a 25-minute job whenever the bundled binary was stale
REM and the release-asset download found no .exe. Match the .sh contract:
REM skip the whole phase.
REM ---------------------------------------------------------------------------
if "%NO_AUTO_LAUNCH%"=="1" (
    echo [launcher] --no-auto-launch set: skipping launcher binary acquisition + GUI spawn.
    echo [launcher] Run start-launcher.bat later to download/build and open the launcher.
    goto :end
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
REM
REM Shell-function shadowing on Windows: cmd.exe doesn't have shell
REM functions the way bash does, so the Linux _resolves_to_binary problem
REM (lean-ctx/asdf/fnm/nvm wrappers around node/npm) doesn't apply here.
REM `where /q` only matches PATH executables — function/alias shadows are
REM impossible. PowerShell users with `function npm { ... }` in their
REM profile would still be hit, but install.ps1 runs with -NoProfile so
REM profile-defined functions are not loaded.
REM ---------------------------------------------------------------------------

REM Install log path — bidirectional with install.py + post-install-launcher.sh.
REM Cmd doesn't have a JSON library so we hand-roll the JSONL writes via
REM PowerShell. Each event is one line; never PII; the launcher and Claude
REM Code both read this on failure (see docs/INSTALL_RECOVERY.md).
set "INSTALL_LOG=%~dp0state\logs\install.jsonl"

REM _log_event <step> <phase> <detail>
REM Idempotent: silently no-ops if the log dir doesn't exist (install.py
REM Step 8 creates it; if we got here without that step we just skip the
REM event — never crash).
REM
REM W-P1-1 (v0.2.53 Track H): values pass through ENV VARS, not via
REM `%~1` substitution into the PowerShell command string. The previous
REM implementation interpolated `'%~1'` literally into PowerShell `'...'`
REM single-quoted string literals — if the value contained an apostrophe
REM (French/Italian path component like `D'Angelo`, or any detail text
REM with `'`), the PS literal terminated early and the rest was parsed
REM as PowerShell code (corrupt JSONL row at best; logic injection at
REM worst). Env-var passthrough sidesteps the cmd → PS quoting boundary
REM entirely: PowerShell reads from $env:VCT_LOG_* via a string accessor
REM that does not re-parse the value.
goto :after_log_helper
:_log_event
if not exist "%~dp0state\logs" goto :_log_event_done
set "VCT_LOG_STEP=%~1"
set "VCT_LOG_PHASE=%~2"
set "VCT_LOG_DETAIL=%~3"
set "VCT_LOG_PATH=%INSTALL_LOG%"
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ');" ^
    "$step  = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_STEP'));" ^
    "$phase = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_PHASE'));" ^
    "$det   = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_DETAIL'));" ^
    "$path  = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_PATH'));" ^
    "$obj = [pscustomobject]@{ ts = $ts; actor = 'first-install.bat'; step = $step; phase = $phase; detail = $det };" ^
    "$line = $obj | ConvertTo-Json -Compress -Depth 3;" ^
    "Add-Content -Path $path -Value $line" 2>nul
set "VCT_LOG_STEP="
set "VCT_LOG_PHASE="
set "VCT_LOG_DETAIL="
set "VCT_LOG_PATH="
:_log_event_done
goto :eof
:after_log_helper

set "LAUNCHER_BIN="
REM First-match-wins probe. Order:
REM   1. Locally built RELEASE binary (developer running pnpm tauri build)
REM   2. Bundled prebuilt in launcher\dist\windows-x64\ (default for end
REM      users; vct-launcher.exe + vct-hub.exe + vct-updater.exe ship there
REM      with .metadata.json sidecars — see the staleness check below)
REM   3. System install paths (someone installed via winget / msi)
REM   4. Locally built DEBUG binary (last resort only)
REM
REM v0.2.62: the target\debug\vct-launcher-temp.exe candidate is probed LAST,
REM not 4th. A months-old debug-temp leftover on a dirty tree must never
REM outrank the freshly-bundled dist binary (the one the launcher's own
REM restart, restart.rs::resolve_target_binary, relaunches from). Pre-fix it
REM sat ahead of dist, so a stale debug build captured the .lnk and the user
REM opened an OLD binary (old icon) while the app restarted into fresh dist.
REM (target\release\* stays ahead of dist so a contributor's real build still
REM wins — only the debug artifact is demoted.)
if exist "%~dp0launcher\src-tauri\target\release\vct-launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\release\launcher.exe"
) else if exist "%~dp0launcher\dist\windows-x64\vct-launcher.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\dist\windows-x64\vct-launcher.exe"
) else if exist "%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe" (
    set "LAUNCHER_BIN=%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe"
) else if exist "%LOCALAPPDATA%\vct-launcher\vct-launcher.exe" (
    set "LAUNCHER_BIN=%LOCALAPPDATA%\vct-launcher\vct-launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe" (
    set "LAUNCHER_BIN=%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe"
)

REM Staleness check for bundled binaries. Reads <binary>.metadata.json
REM (the manifest scripts/build-bundled-launcher.sh writes alongside the
REM binary) and compares its source_hash against the live launcher
REM subtree's git hash. If they don't match, the bundled binary was built
REM from an older snapshot — fall through to the download/build menu.
REM Mirror of post-install-launcher.sh::_bundled_binary_is_fresh, ported
REM to PowerShell for Windows. Only runs for paths under launcher\dist\.
if defined LAUNCHER_BIN (
    echo !LAUNCHER_BIN! | findstr /I /C:"\launcher\dist\" >nul
    if !ERRORLEVEL! EQU 0 (
        set "META_FILE=!LAUNCHER_BIN!.metadata.json"
        if exist "!META_FILE!" (
            for /f "delims=" %%H in ('"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
                "$j = Get-Content -Raw '!META_FILE!' | ConvertFrom-Json;" ^
                "$mh = $j.source_hash;" ^
                "Push-Location '%~dp0';" ^
                "$lh = (git ls-tree HEAD launcher/src-tauri/src/ launcher/src/ launcher/src-tauri/Cargo.toml launcher/src-tauri/Cargo.lock launcher/package.json 2^>$null ^| git hash-object --stdin 2^>$null);" ^
                "Pop-Location;" ^
                "if (-not $lh -or -not $mh) { 'fresh' } elseif ($mh -eq $lh) { 'fresh' } else { 'stale' }" 2^>nul') do set "FRESHNESS=%%H"
            if /I "!FRESHNESS!"=="stale" (
                echo [launcher] Bundled binary is stale ^(built from a different launcher source^); will try download/build.
                set "LAUNCHER_BIN="
            )
        ) else (
            REM No metadata file alongside the bundled binary — treat as
            REM stale. Mirror of bash behaviour: err toward correctness.
            echo [launcher] Bundled binary has no metadata.json — treating as stale.
            set "LAUNCHER_BIN="
        )
    )
)

REM Frontend-embedded check (added 2026-04-28). A release binary built
REM with an empty launcher\build\ compiles fine but renders "Could not
REM connect to localhost" at startup because no SvelteKit assets were
REM embedded. We require >=5 occurrences of "_app/immutable/" in the
REM binary's bytes; otherwise treat as broken and fall through to
REM download/build. Threshold derived from build artifact analysis at
REM commit d576ad6 (which fixed the original regression in 5abb8cf).
REM
REM DEDUP-15 (v0.2.53 Track H + Track A): the count is delegated to
REM scripts\lib\asset-ref-count.ps1 (Get-AssetRefCount) — the same
REM helper start-launcher.bat uses and the bash sibling
REM scripts/lib/asset-ref-count.sh consumes. Previously this site had
REM its own inline marker substring + threshold ("_app/immutable/assets",
REM the narrow form); the helper uses the canonical broad form
REM "_app/immutable/" which correctly catches Svelte 5 builds where the
REM trailing "assets/" segment is dropped. The 4 prior copies of the
REM marker (this file, start-launcher.bat, .sh sibling, build scripts)
REM are now collapsed to one.
if defined LAUNCHER_BIN (
    for /f "delims=" %%C in ('"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
        ". '%~dp0scripts\lib\asset-ref-count.ps1'; Get-AssetRefCount -Path '!LAUNCHER_BIN!'" 2^>nul') do set "FE_COUNT=%%C"
    if not defined FE_COUNT set "FE_COUNT=0"
    REM CMD batch can't do GEQ on arbitrary strings; coerce + compare numerically.
    set /a "FE_COUNT_INT=!FE_COUNT! + 0" 2>nul
    if !FE_COUNT_INT! LSS 5 (
        echo [launcher] Bundled binary has !FE_COUNT_INT! frontend asset refs ^(expected ^>=5^) — frontend not embedded; treating as broken.
        set "LAUNCHER_BIN="
    )
)

if defined LAUNCHER_BIN (
    echo [launcher] Found existing binary: %LAUNCHER_BIN%
    call :_log_event "binary-probe" "ok" "existing launcher binary found"
    goto :auto_launch
) else (
    call :_log_event "binary-probe" "skip" "no existing launcher binary on disk"
)

REM Binary not found — present the same 3-way menu as the bash helper.
echo.
echo ===============================================
echo   Launcher binary not found. Choose how to get it:
echo ===============================================
echo   [1] Download prebuilt ^(recommended^) - fast, ~22 MB
echo        ^(downloads vibecoded-orchestrator-^<version^>-windows-x64.zip from latest GitHub Release^)
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
REM
REM v0.2.54 G-1.5 (Wave 0 follow-up): logic moved to
REM scripts\lib\download-launcher-asset.ps1. The previous inline command
REM filtered assets with $_.name.EndsWith('.exe'), but release.yml has
REM shipped ONLY vibecoded-orchestrator-<version>-windows-x64.zip since
REM the 2026-05-10 uniform-zip packaging change (verified against the
REM live v0.2.53 release: zero .exe assets). The filter therefore ALWAYS
REM returned NO_ASSET and every Windows first-run silently fell through
REM to the 15-30 min source build. The helper prefers the windows .zip
REM (downloads + Expand-Archive + lands vct-launcher.exe, vct-hub.exe,
REM vct-updater.exe in %DEST_DIR%) and keeps the bare-.exe asset as a
REM legacy fallback. Windows sibling of the POSIX M-P0-3 fix in
REM scripts/post-install-launcher.sh. Exit codes: 0 ok, 2 NO_ASSET,
REM 3 TOO_SMALL, 4 NO_BINARY_IN_ZIP, 1 generic.
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lib\download-launcher-asset.ps1" -DestDir "%DEST_DIR%"
set "DL_EXIT=%ERRORLEVEL%"

if "%DL_EXIT%"=="0" (
    set "LAUNCHER_BIN=%DEST_EXE%"
    call :_log_event "download" "ok" "windows launcher downloaded (zip or legacy exe)"
    goto :auto_launch
)
echo [launcher] Download failed ^(exit %DL_EXIT%^). Falling back to build.
call :_log_event "download" "error" "windows download failed; falling back to build"
goto :launch_build

:launch_build
echo.
echo [launcher] Building from source...

REM Ensure Node.js. T1 silent (only with --yes), T3 prompt, T4 URL.
REM
REM IMPORTANT: cmd.exe does NOT allow labels (`:foo`) inside parenthesised
REM blocks. The previous version had `:winget_recheck`, `:node_install_done`,
REM `:node_recheck` inside `if ( ... )` blocks — that produced the cryptic
REM `") non atteso."` ("`)` not expected") parser error reported 2026-04-28.
REM Refactored to flat goto-driven control flow with labels at top level.
where /q node
if %ERRORLEVEL% EQU 0 goto :node_ready

where /q winget
if %ERRORLEVEL% NEQ 0 goto :no_winget

REM winget available — try silent (T1) or prompt (T3).
if "%YES_FLAG%"=="1" (
    echo [launcher] T1 silent: winget install Node.js
    winget install --silent --accept-package-agreements --accept-source-agreements OpenJS.NodeJS
    goto :after_winget_install
)

set "NODE_ANS="
set /p "NODE_ANS=Install Node.js via winget? [Y/n] "
if /I "%NODE_ANS%"=="N" (
    echo [launcher] T4: install Node manually from https://nodejs.org/
    goto :launch_skip
)
winget install --accept-package-agreements --accept-source-agreements OpenJS.NodeJS

:after_winget_install
REM winget modifies the persistent PATH (HKLM + HKCU registry) for NEW
REM shells but not for us — refresh by reading the registry directly.
REM
REM W-P1-2 (v0.2.53 Track H): the previous `call refreshenv >nul 2>&1`
REM relied on Chocolatey's `refreshenv` shim. That shim ships with
REM `choco install` (and with the cmder/clink helpers) — it is NOT
REM pre-installed on stock Win10/Win11. The redirect to nul swallowed
REM the "is not recognized" error, so the call silently no-op'd, our
REM in-process PATH was never updated, and `:recheck_node`'s `where /q
REM node` couldn't find the just-installed binary. The user got "Still
REM no Node on PATH. Try a new terminal." on the SAME terminal that
REM JUST ran a successful winget install.
REM
REM Fix: read HKLM Path + HKCU Path via PowerShell's
REM [Environment]::GetEnvironmentVariable("Path", "Machine"|"User"),
REM concat (Machine wins ties — same precedence as a fresh shell), and
REM splice into the current %PATH%. We use PowerShell rather than `reg
REM query` because reg-query output requires tokenization that is
REM brittle when the PATH contains spaces (cmd-side parsing trap).
REM PowerShell's API returns the value as a single string already.
REM
REM Idempotent: if PS fails (unlikely — powershell.exe ships with
REM every Win7+ install), we fall through with the unchanged PATH and
REM let `:recheck_node` give the user the manual-retry prompt.
for /f "delims=" %%P in ('"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$m = [Environment]::GetEnvironmentVariable('Path', 'Machine');" ^
    "$u = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
    "$p = @($m, $u) | Where-Object { $_ } | ForEach-Object { $_.TrimEnd(';') };" ^
    "[Console]::Out.Write(($p -join ';'))" 2^>nul') do set "PATH=%%P"
goto :recheck_node

:no_winget
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
set "WG_RETRY="
set /p "WG_RETRY=[r] Re-check / [s] Skip the build: "
if /I "%WG_RETRY%"=="r" goto :recheck_node
goto :launch_skip

:recheck_node
where /q node
if %ERRORLEVEL% EQU 0 (
    echo [launcher] Node detected — continuing.
    goto :node_ready
)
echo [launcher] Still no Node on PATH. Try a new terminal.
if "%YES_FLAG%"=="1" goto :launch_skip
goto :winget_recheck

:node_ready
where /q node
if %ERRORLEVEL% EQU 0 goto :node_install_done

REM Loud-stop pattern (parity with Linux/macOS post-install-launcher.sh).
REM Don't silently skip and report "Installation complete!" while the
REM launcher build was actually skipped — that's the same anti-pattern
REM as the old Joern silent-hang (Joern integration was removed in
REM v0.2.73 CG-3; this comment is a historical analogy only, not a
REM claim that Joern is still installed/probed here). Tell the user
REM exactly what to do.
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
set "NODE_RETRY="
set /p "NODE_RETRY=[r] Re-check / [s] Skip the build: "
if /I not "%NODE_RETRY%"=="r" goto :launch_skip
where /q node
if %ERRORLEVEL% EQU 0 (
    echo [launcher] Node detected — continuing build.
    goto :node_install_done
)
echo [launcher] Still no Node on PATH. Try opening a new terminal.
goto :node_recheck

:node_install_done
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
    call :_log_event "build/deps" "start" "pnpm install"
    call pnpm install
    if !ERRORLEVEL! NEQ 0 (
        call :_log_event "build/deps" "error" "pnpm install failed"
    ) else (
        call :_log_event "build/deps" "ok" "pnpm install completed"
    )
    echo [launcher] [4/4] tauri build ^(this takes 5-15 min^)
    REM --no-bundle: skip MSI packaging. End users only need the .exe;
    REM the bundle step needs WiX which often isn't installed. See
    REM post-install-launcher.sh for the same rationale.
    call :_log_event "build/tauri" "start" "pnpm tauri build --no-bundle"
    call pnpm tauri build --no-bundle
    if !ERRORLEVEL! NEQ 0 (
        call :_log_event "build/tauri" "error" "pnpm tauri build exit non-zero"
    ) else (
        call :_log_event "build/tauri" "ok" "release exe built"
    )
) else (
    echo [launcher] [3/4] npm install
    call :_log_event "build/deps" "start" "npm install"
    call npm install
    if !ERRORLEVEL! NEQ 0 (
        call :_log_event "build/deps" "error" "npm install failed"
    ) else (
        call :_log_event "build/deps" "ok" "npm install completed"
    )
    echo [launcher] [4/4] tauri build ^(this takes 5-15 min^)
    call :_log_event "build/tauri" "start" "npx tauri build --no-bundle"
    call npx tauri build --no-bundle
    if !ERRORLEVEL! NEQ 0 (
        call :_log_event "build/tauri" "error" "npx tauri build exit non-zero"
    ) else (
        call :_log_event "build/tauri" "ok" "release exe built"
    )
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
echo ===============================================
echo   Launcher build did not complete
echo ===============================================
echo.
echo   Manual build (when you've installed the missing prereqs):
echo     cd %~dp0launcher
echo     pnpm install     ^(or npm install^)
echo     pnpm tauri build ^(or npx tauri build^)
echo     cd %~dp0
echo     start-launcher.bat
echo.
echo   Detailed recovery doc (humans + Claude both read this):
echo     %~dp0docs\INSTALL_RECOVERY.md
echo.
echo   Stuck? Let Claude Code diagnose this for you:
echo     cd %~dp0
echo     claude    ^(opens Claude Code in this repo^)
echo.
echo   Then paste this exact prompt to Claude:
echo     -------- COPY BELOW --------
echo     Read docs\INSTALL_RECOVERY.md and follow it. The launcher
echo     build did not complete during first-install. The launcher
echo     is the GUI for managing projects, services, hooks, RL
echo     retrieval, the knowledge graph dashboard, and the audit log
echo     - without it the user loses the project wizard, KG/codegraph
echo     visualizations, hook toggles, and module install flow.
echo     DO NOT just skip the launcher build or tell me to do it
echo     manually. Find why the build failed (missing prereqs, system
echo     deps, perms, Visual Studio Build Tools, WebView2, Rust) and
echo     ACTUALLY install/fix what's needed, then run pnpm tauri build
echo     (or npm + npx tauri) until I have a working binary at
echo     launcher\src-tauri\target\release\. After that, run
echo     start-launcher.bat and confirm the launcher window opens.
echo     -------- COPY ABOVE --------
echo.
echo   The repo's own knowledge graph + hooks give Claude full
echo   context to debug your specific machine. That's why vco exists.
echo.
goto :end

:auto_launch
if "%NO_AUTO_LAUNCH%"=="1" (
    echo.
    echo [launcher] --no-auto-launch set. Run start-launcher.bat to open the GUI.
    goto :end
)

REM ----- Desktop shortcut (opt-out) -----------------------------------------
REM Create a .lnk on the Desktop + Start Menu so the user can launch the GUI
REM with double-click. Skip via VCT_NO_DESKTOP_ICON=1, --no-desktop-icon flag,
REM or by answering N to the prompt.
if "%VCT_NO_DESKTOP_ICON%"=="1" goto :skip_shortcut
if "%NO_DESKTOP_ICON%"=="1" goto :skip_shortcut
if "%YES_FLAG%"=="1" goto :create_shortcut
set "SHORTCUT_ANS=Y"
set /p "SHORTCUT_ANS=Create a desktop shortcut for the launcher? [Y/n] "
if /I "%SHORTCUT_ANS%"=="N" goto :skip_shortcut
:create_shortcut
REM Use PowerShell's WScript.Shell to create the .lnk. The IconLocation
REM points at the launcher's .exe so Windows uses its embedded icon
REM resource (Tauri bakes the launcher icon into the binary at build time).
set "DESKTOP_LNK=%USERPROFILE%\Desktop\VCT Launcher.lnk"
set "STARTMENU_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "STARTMENU_LNK=%STARTMENU_DIR%\VCT Launcher.lnk"
if not exist "%STARTMENU_DIR%" mkdir "%STARTMENU_DIR%"
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell;" ^
    "foreach ($p in @('%DESKTOP_LNK%','%STARTMENU_LNK%')) {" ^
    "  $s = $ws.CreateShortcut($p);" ^
    "  $s.TargetPath = '%LAUNCHER_BIN%';" ^
    "  $s.WorkingDirectory = (Split-Path -Parent '%LAUNCHER_BIN%');" ^
    "  $s.Description = 'VibeCoded Tools Launcher';" ^
    "  $s.IconLocation = '%LAUNCHER_BIN%,0';" ^
    "  $s.Save();" ^
    "  Write-Host ('[launcher] Shortcut: ' + $p)" ^
    "}"
:skip_shortcut

echo.
echo Installation complete. Opening launcher...
call :_log_event "spawn" "ok" "launcher detached"
REM `start "" ...`: empty string is the WINDOW TITLE arg required by `start`
REM when the path is quoted; without it cmd treats the quoted path as the
REM title. Detached spawn — this cmd window can close without killing the GUI.
start "" "%LAUNCHER_BIN%"

:end
REM Keep the cmd window open when run from Explorer double-click;
REM `pause` shows "Press any key to continue..." which is exactly
REM what we want. Gated on YES_FLAG so unattended runs (--yes /
REM --non-interactive, e.g. CI) exit cleanly instead of hanging
REM forever waiting for a keypress (v0.2.54 Track W).
if "%YES_FLAG%"=="0" pause
exit /b 0

REM TODO(post-v1.0):
REM   - Show a Windows Forms progress window during long-running steps
REM     (PowerShell + System.Windows.Forms is enough; no extra deps).
REM   - Sign the launcher .exe (Authenticode) to avoid SmartScreen
REM     warnings for end users.
