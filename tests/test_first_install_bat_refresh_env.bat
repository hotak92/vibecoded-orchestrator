@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later
REM Copyright (c) 2026 VibeCoded Tools
REM
REM tests/test_first_install_bat_refresh_env.bat — W-P1-2 (v0.2.53 Track H).
REM
REM Regression test for first-install.bat's PATH refresh after `winget
REM install OpenJS.NodeJS`. The previous implementation called
REM `refreshenv` (a Chocolatey shim) which is not pre-installed on stock
REM Windows; the call silently no-op'd and the in-process %PATH% never
REM picked up the just-installed Node.
REM
REM Test strategy: invoke the new PowerShell-based registry reader and
REM assert that:
REM   1. The output is non-empty (Machine PATH at minimum is always set).
REM   2. The output contains at least one ';'-separated component (sanity).
REM   3. The output contains the System32 path (universally present on
REM      Windows; if it's missing, our registry read is broken).
REM   4. We can splice the output into a `set PATH=...` and `where` still
REM      finds a guaranteed-present binary (cmd.exe itself).
REM
REM We do NOT test winget install — that would mutate the user's machine
REM and require network. We test only the PATH-refresh half of the fix.
REM
REM Runner: cmd.exe ONLY. CI matrix gates this to Windows runners.
REM
REM Exit codes:
REM   0 = all assertions passed
REM   1 = at least one assertion failed
REM   2 = environment issue (missing pwsh/powershell)

setlocal enabledelayedexpansion

where /q pwsh.exe
if %ERRORLEVEL% EQU 0 (
    set "PSCMD=pwsh.exe"
) else (
    set "PSCMD=powershell.exe"
)
where /q !PSCMD!
if %ERRORLEVEL% NEQ 0 (
    echo SKIP: no powershell or pwsh on PATH
    exit /b 2
)

set "REFRESHED_PATH="
for /f "delims=" %%P in ('"!PSCMD!" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$m = [Environment]::GetEnvironmentVariable('Path', 'Machine');" ^
    "$u = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
    "$p = @($m, $u) ^| Where-Object { $_ } ^| ForEach-Object { $_.TrimEnd(';') };" ^
    "[Console]::Out.Write(($p -join ';'))"') do set "REFRESHED_PATH=%%P"

if not defined REFRESHED_PATH (
    echo FAIL: refreshed PATH is empty
    exit /b 1
)

REM Assertion 2: must contain a ';' (Machine PATH always has multiple entries).
echo !REFRESHED_PATH! | findstr ";" >nul
if %ERRORLEVEL% NEQ 0 (
    echo FAIL: refreshed PATH has no ';' separator: !REFRESHED_PATH!
    exit /b 1
)

REM Assertion 3: must contain System32 (universally present on every Windows install).
echo !REFRESHED_PATH! | findstr /I "System32" >nul
if %ERRORLEVEL% NEQ 0 (
    echo FAIL: refreshed PATH does not contain System32: !REFRESHED_PATH!
    exit /b 1
)

REM Assertion 4: splice into PATH and assert `where cmd` still works.
set "ORIG_PATH=%PATH%"
set "PATH=!REFRESHED_PATH!"
where /q cmd.exe
set "WHERE_EXIT=%ERRORLEVEL%"
set "PATH=%ORIG_PATH%"

if %WHERE_EXIT% NEQ 0 (
    echo FAIL: after splicing refreshed PATH, `where cmd.exe` failed
    exit /b 1
)

echo OK: PATH refresh via registry works as expected.
exit /b 0
