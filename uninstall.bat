@echo off
REM uninstall.bat — VibeCoded Tools orchestrator uninstaller (Windows)
REM
REM Windows sibling of uninstall.sh (v0.2.54 Track G, G-2). Thin wrapper:
REM delegates to `python install.py --uninstall`, forwarding all args.
REM
REM Flags forwarded to the uninstaller:
REM   --keep-data         keep container volumes
REM   --remove-projects   also remove .claude\ in registered projects
REM   --dry-run           print plan and exit
REM   --yes               non-interactive: accept all confirmations
REM
REM By default the uninstaller is interactive and prompts before each
REM destructive step. It NEVER touches %USERPROFILE%\.vct-secrets and
REM NEVER touches user source code outside orchestrator-managed paths.
setlocal enabledelayedexpansion

REM Pin to the script directory so install.py resolves even when invoked
REM from Explorer or another working dir.
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM Help / usage. Same first-argument contract as first-install.bat: /help
REM must print usage and exit 0 with ZERO side effects — never fall through
REM into the real uninstall path. Covered by
REM tests/test_first_install_bat_cmd_parse.py on windows-latest.
REM ---------------------------------------------------------------------------
if /I "%~1"=="/help"  goto :show_help
if /I "%~1"=="/h"     goto :show_help
if "%~1"=="/?"        goto :show_help
if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
if "%~1"=="-?"        goto :show_help
goto :after_help

:show_help
echo Usage: uninstall.bat [options]
echo.
echo Uninstalls the VibeCoded Tools orchestrator. Removes ONLY
echo orchestrator-managed paths - never your source code or secrets.
echo.
echo Options forwarded to install.py --uninstall:
echo   --dry-run           Print the removal plan and exit. Run this first.
echo   --keep-data         Preserve container volumes - KG vectors, models.
echo   --remove-projects   Also remove .claude\ folders in registered projects.
echo   --yes               Non-interactive: accept every confirmation.
echo.
echo Interactive by default: each destructive step prompts separately.
exit /b 0

:after_help

REM Sniff interactivity flags BEFORE any pause-able path (v0.2.54 Track W
REM discipline: an unattended run must never hang on `pause`).
set "YES_FLAG=0"
for %%A in (%*) do (
    if /I "%%~A"=="--yes"             set "YES_FLAG=1"
    if /I "%%~A"=="--non-interactive" set "YES_FLAG=1"
    if /I "%%~A"=="--quiet"           set "YES_FLAG=1"
    if /I "%%~A"=="--dry-run"         set "YES_FLAG=1"
)

REM Sanity check: install.py must be alongside us.
if not exist "%~dp0install.py" (
    echo ERROR: install.py not found next to uninstall.bat.
    echo Run this script from the orchestrator install root.
    if "%YES_FLAG%"=="0" pause
    exit /b 1
)

REM Python detection: prefer the py launcher with an explicit 3.11+ probe,
REM then bare `python`. Mirrors uninstall.sh's 3.11+ floor.
set "PYCMD="
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if !ERRORLEVEL! EQU 0 set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 set "PYCMD=python"
    )
)
if not defined PYCMD (
    echo ERROR: Python 3.11+ is required to run the uninstaller.
    echo Install it from https://www.python.org/downloads/ or via winget:
    echo   winget install Python.Python.3.12
    if "%YES_FLAG%"=="0" pause
    exit /b 1
)

%PYCMD% install.py --uninstall %*
set "UNINSTALL_RC=%ERRORLEVEL%"

REM Keep the cmd window open when run from Explorer double-click so the
REM user can read the uninstall summary. Gated on YES_FLAG so unattended
REM runs exit cleanly (v0.2.54 Track W discipline).
if "%YES_FLAG%"=="0" pause
exit /b %UNINSTALL_RC%
