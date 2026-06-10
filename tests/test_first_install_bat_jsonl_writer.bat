@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later
REM Copyright (c) 2026 VibeCoded Tools
REM
REM tests/test_first_install_bat_jsonl_writer.bat — W-P1-1 (v0.2.53 Track H).
REM
REM Regression test for the first-install.bat :_log_event JSONL writer.
REM Pre-fix, the writer interpolated `'%~1'` into PowerShell single-quoted
REM literals; any value containing an apostrophe, backslash, or tab would
REM either truncate the literal (apostrophe) or arrive as garbled text
REM (backslash + tab survive cmd substitution differently than escape
REM sequences inside PS literals).
REM
REM Test strategy: simulate the :_log_event subroutine inline (we can't
REM directly `call :_log_event` from this test because first-install.bat
REM is a full installer script — we mimic only the helper). The test
REM writes 3 events with hostile inputs and parses the resulting JSONL
REM with PowerShell's ConvertFrom-Json, asserting each row round-trips
REM cleanly.
REM
REM Runner: cmd.exe ONLY. This file runs as a Windows batch script on
REM Windows runners (installer-smoke.yml, Track D wires it up). On Linux
REM dev machines it can be linted manually with `cmd /c <file>` only when
REM Wine + cmd.exe shim is available; otherwise it is exec-skipped by CI
REM matrix gating.
REM
REM Exit codes:
REM   0 = all assertions passed
REM   1 = at least one assertion failed
REM   2 = environment issue (missing pwsh/powershell)

setlocal enabledelayedexpansion

set "TEST_LOG=%TEMP%\vct_test_jsonl_writer_%RANDOM%.jsonl"
del /q "%TEST_LOG%" 2>nul

REM Detect PowerShell (mirrors first-install.bat's PSCMD selection).
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

REM Mirror of the post-fix :_log_event body (W-P1-1).
goto :run_tests
:_log_event_helper
set "VCT_LOG_STEP=%~1"
set "VCT_LOG_PHASE=%~2"
set "VCT_LOG_DETAIL=%~3"
set "VCT_LOG_PATH=%TEST_LOG%"
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ');" ^
    "$step  = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_STEP'));" ^
    "$phase = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_PHASE'));" ^
    "$det   = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_DETAIL'));" ^
    "$path  = [string]([Environment]::GetEnvironmentVariable('VCT_LOG_PATH'));" ^
    "$obj = [pscustomobject]@{ ts = $ts; actor = 'first-install.bat'; step = $step; phase = $phase; detail = $det };" ^
    "$line = $obj | ConvertTo-Json -Compress -Depth 3;" ^
    "Add-Content -Path $path -Value $line"
set "VCT_LOG_STEP="
set "VCT_LOG_PHASE="
set "VCT_LOG_DETAIL="
set "VCT_LOG_PATH="
goto :eof

:run_tests
REM Hostile inputs: apostrophe + backslash + literal "quote".
call :_log_event_helper "probe" "ok" "user=D'Angelo path=C:\Users\test"
call :_log_event_helper "step\with\backslash" "phase" "value with 'apostrophes' inside"
call :_log_event_helper "tab" "phase" "before	after"

REM Parse + assert via PowerShell. Each row must round-trip via
REM ConvertFrom-Json; the detail field must equal the input verbatim.
"%PSCMD%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ok = $true;" ^
    "$lines = Get-Content -LiteralPath '%TEST_LOG%';" ^
    "if ($lines.Count -ne 3) { Write-Host 'FAIL: expected 3 rows, got' $lines.Count; exit 1 }" ^
    "$expected = @(" ^
    "  @{ step='probe'; phase='ok'; detail=\"user=D'Angelo path=C:\Users\test\" }," ^
    "  @{ step='step\with\backslash'; phase='phase'; detail=\"value with 'apostrophes' inside\" }," ^
    "  @{ step='tab'; phase='phase'; detail=\"before`tafter\" }" ^
    ");" ^
    "for ($i=0; $i -lt 3; $i++) {" ^
    "  try { $obj = $lines[$i] | ConvertFrom-Json -ErrorAction Stop } catch { Write-Host ('FAIL row ' + $i + ' is not valid JSON: ' + $lines[$i]); $ok = $false; continue }" ^
    "  $e = $expected[$i];" ^
    "  if ($obj.step -ne $e.step) { Write-Host ('FAIL row ' + $i + ' step mismatch: got=' + $obj.step + ' expected=' + $e.step); $ok = $false }" ^
    "  if ($obj.phase -ne $e.phase) { Write-Host ('FAIL row ' + $i + ' phase mismatch: got=' + $obj.phase + ' expected=' + $e.phase); $ok = $false }" ^
    "  if ($obj.detail -ne $e.detail) { Write-Host ('FAIL row ' + $i + ' detail mismatch: got=' + $obj.detail + ' expected=' + $e.detail); $ok = $false }" ^
    "  if ($obj.actor -ne 'first-install.bat') { Write-Host ('FAIL row ' + $i + ' actor mismatch: ' + $obj.actor); $ok = $false }" ^
    "}" ^
    "if ($ok) { Write-Host 'OK: all 3 rows parsed + matched'; exit 0 } else { exit 1 }"

set "PS_EXIT=%ERRORLEVEL%"
del /q "%TEST_LOG%" 2>nul
exit /b %PS_EXIT%
