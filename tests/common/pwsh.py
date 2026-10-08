"""Shared argv flags for tests that invoke PowerShell (``pwsh``).

WHY THIS EXISTS (v0.2.102, lane W4-F defect B): on Linux, PowerShell 7's
syslog logging provider writes EVERY compiled scriptblock to the system
journal (``SYSLOG_IDENTIFIER=powershell``, message ``Creating Scriptblock
text``), one entry per dot-sourced/run ``.ps1`` file, carrying the file's
WHOLE source. The cross-OS parity tests launch ``pwsh`` on any host that
happens to have it installed, so one day of pytest runs flooded ~30k
journal entries (measured 2026-10-08; ``pwsh`` itself is the writer —
``_TRANSPORT=syslog``, ``SYSLOG_FACILITY=16`` — not stderr capture).

THE MECHANISM: ``pwsh -SettingsFile <path>`` overrides the system-wide
``powershell.config.json`` for that one session. A settings file with
``{"LogLevel": "Error"}`` silences the Operational-channel syslog provider
for that process only. Measured on pwsh 7.4.6 (2026-10-08, this repo):
running one shipped ``.ps1`` file produced 14 journal entries (2 of them
full-source scriptblock dumps); with the flag, 0. Verified by measurement,
not by doc alone.

SCOPE: TEST invocations only. The user's global PowerShell config and
``$PSHOME`` are never touched (the flag overrides them for the session
only), and shipped hook behaviour on Windows is unaffected: on win32 the
flags are empty (Windows has no syslog, and Windows PowerShell 5.1 has no
``-SettingsFile`` switch), so the spread below is a no-op there.

USAGE — every test that spawns pwsh builds its argv through
``PWSH_BASE_FLAGS`` instead of a literal ``"-NoProfile"``::

    from tests.common.pwsh import PWSH_BASE_FLAGS

    subprocess.run([exe, *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(ps1)], ...)

One concern, one home: do not hand-write ``"-SettingsFile"`` at call
sites; this module is the only place that knows the settings-file path
and the win32 escape.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SETTINGS_DIR = Path(tempfile.gettempdir()) / "vco-test-pwsh-settings"
_SETTINGS_FILE = _SETTINGS_DIR / "powershell.config.json"

# The LogLevel that silences the warning/informational Operational-channel
# syslog entries (the scriptblock dumps are logged at Warning). Error and
# above still reach the journal, which is what a test run should keep.
_SETTINGS_BODY = '{ "LogLevel": "Error" }\n'


def _ensure_settings_file() -> str | None:
    """Create the session-local settings file once; ``None`` if impossible.

    Soft-fail on ``OSError``: without the file the journal noise is merely
    the pre-existing behaviour — never worth failing a test over.
    """
    try:
        _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        if not _SETTINGS_FILE.exists():
            _SETTINGS_FILE.write_text(_SETTINGS_BODY, encoding="ascii")
        return str(_SETTINGS_FILE)
    except OSError:
        return None


def quiet_flags() -> list[str]:
    """The journal-silencing argv fragment (``[]`` on Windows)."""
    if sys.platform == "win32":
        return []
    settings = _ensure_settings_file()
    if settings is None:
        return []
    return ["-SettingsFile", settings]


#: The base flags EVERY test-side pwsh invocation must start with (after
#: the executable): journal suppression on POSIX, ``-NoProfile`` always.
PWSH_BASE_FLAGS: list[str] = quiet_flags() + ["-NoProfile"]
