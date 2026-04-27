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
REM   - installed:    %LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe
REM
REM Status: STUB — works against the current dev-build .exe path.

setlocal
cd /d "%~dp0"

set "FOUND="

REM First-match-wins search through known binary locations. We can't
REM use a for /F loop with `if exist` cleanly across all Windows shells
REM — repetition is uglier but predictable.
if exist "%~dp0launcher\src-tauri\target\release\vct-launcher.exe" (
    set "FOUND=%~dp0launcher\src-tauri\target\release\vct-launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe" (
    set "FOUND=%~dp0launcher\src-tauri\target\release\vct-launcher-temp.exe"
) else if exist "%~dp0launcher\src-tauri\target\release\launcher.exe" (
    set "FOUND=%~dp0launcher\src-tauri\target\release\launcher.exe"
) else if exist "%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe" (
    set "FOUND=%~dp0launcher\src-tauri\target\debug\vct-launcher-temp.exe"
) else if exist "%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe" (
    set "FOUND=%LOCALAPPDATA%\Programs\VCT Launcher\vct-launcher.exe"
)

if "%FOUND%"=="" (
    echo ERROR: launcher binary not found.
    echo.
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
