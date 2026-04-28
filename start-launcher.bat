@echo off
REM start-launcher.bat — Run the VibeCoded Tools launcher (Windows)
REM
REM Run after first-install.bat has completed. Locates the built Tauri
REM .exe and starts it. We use `start` (not direct invocation) so the
REM launcher detaches from this cmd window and the window can close.
REM
REM Search paths cover:
REM   - dev-build:    launcher\src-tauri\target\release\vct-launcher-temp.exe
REM   - planned v1.0: launcher\src-tauri\target\release\vct-launcher.exe
REM   - debug build:  launcher\src-tauri\target\debug\vct-launcher-temp.exe
REM   - bundle:       launcher\src-tauri\target\release\bundle\nsis\*.exe
REM   - bundled:      launcher\dist\windows-x64\vct-launcher.exe
REM   - installed:    %LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe
REM
REM Frontend-embedded check (added 2026-04-28 mirror of bash version):
REM A release binary built without launcher\build\ populated will compile
REM fine but render "Could not connect to localhost" at startup because
REM no SvelteKit assets were embedded. We refuse to launch any such
REM binary by counting "_app/immutable/assets" string occurrences via
REM PowerShell's -SimpleMatch -Encoding Byte; threshold is >=5.
REM Background: regression at commit 5abb8cf, fix d576ad6.

setlocal
cd /d "%~dp0"

set "FOUND="
set "SKIPPED="

REM First-match-wins search through known binary locations. Each match
REM goes through _is_valid_binary (checks frontend-embedded asset refs).
REM If a candidate exists but lacks embedded frontend, we record it in
REM SKIPPED and continue probing.
call :probe "%~dp0launcher\src-tauri\target\release\vct-launcher.exe"
if not defined FOUND call :probe "%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe"
if not defined FOUND call :probe "%~dp0launcher\src-tauri\target\release\launcher.exe"
if not defined FOUND call :probe "%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe"
if not defined FOUND call :probe "%~dp0launcher\dist\windows-x64\vct-launcher.exe"
if not defined FOUND call :probe "%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe"

if "%FOUND%"=="" (
    echo ERROR: launcher binary not found.
    echo.
    if defined SKIPPED (
        echo Skipped broken binary/binaries ^(no embedded frontend^):
        echo %SKIPPED%
        echo.
        echo Rebuild with: bash scripts\build-bundled-launcher.sh
        echo.
    )
    echo Run first-install.bat first to set up VibeCoded Tools.
    echo If you already did, the launcher binary may not have been built yet.
    echo Build it manually:
    echo   cd launcher
    echo   pnpm install
    echo   pnpm tauri build
    echo.
    pause
    exit /b 1
)

REM `start "" ...`: the empty string is the WINDOW TITLE arg required
REM by `start` when the path is quoted; without it, cmd interprets the
REM quoted path as the title and doesn't actually launch anything.
REM This is a known Windows cmd gotcha.
start "" "%FOUND%" %*
exit /b 0

REM ─── helpers ────────────────────────────────────────────────────────────
:probe
REM %~1 = candidate exe path
if not exist "%~1" goto :eof
REM Count occurrences of the SvelteKit asset path in the binary. If the
REM count is >=5, treat the binary as healthy. If <5, log to SKIPPED and
REM keep probing. The PowerShell call returns the integer match count;
REM we capture it via for /f.
set "PROBE_PATH=%~1"
set "PROBE_COUNT="
for /f "usebackq tokens=*" %%C in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$bytes=[System.IO.File]::ReadAllBytes('%PROBE_PATH:'=''%'); $s=[System.Text.Encoding]::ASCII.GetString($bytes); ($s.Split([string[]]@('_app/immutable/assets'), [System.StringSplitOptions]::None).Count - 1)"`) do set "PROBE_COUNT=%%C"
if not defined PROBE_COUNT set "PROBE_COUNT=0"
if %PROBE_COUNT% GEQ 5 (
    set "FOUND=%~1"
) else (
    echo WARNING: %~1 has no embedded frontend ^(found %PROBE_COUNT% asset refs, expected ^>=5^) — skipping.
    set "SKIPPED=%SKIPPED% %~1"
)
goto :eof
