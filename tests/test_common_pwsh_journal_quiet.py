# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (W4-F defect B): the shared pwsh journal-silencing flags.

On Linux, PowerShell 7's syslog provider writes every compiled
scriptblock to the system journal — the WHOLE ``.ps1`` source per entry.
The parity tests' pwsh invocations flooded ~30k journal entries in one
day (2026-10-08 measurement, see ``tests/common/pwsh.py``). The fix is
one shared helper: every test-side pwsh argv starts with
``*PWSH_BASE_FLAGS`` (``-SettingsFile <session-local config with
LogLevel=Error> -NoProfile`` on POSIX; ``["-NoProfile"]`` on Windows,
where Windows PowerShell 5.1 has no ``-SettingsFile`` and no syslog).

These tests pin (a) the helper's shape and settings-file content, (b)
that a real pwsh accepts the flags, and (c) a ratchet: NO test file may
hand-write the ``"-NoProfile"`` argv literal anymore — every pwsh
invocation must go through the helper, so a future test cannot silently
reintroduce the journal flood.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS, _SETTINGS_FILE

TESTS_DIR = Path(__file__).resolve().parent


def test_base_flags_end_with_no_profile_and_carry_settings_on_posix():
    assert PWSH_BASE_FLAGS[-1] == "-NoProfile"
    if sys.platform == "win32":
        # Windows PowerShell 5.1 has no -SettingsFile and there is no
        # syslog to silence — the spread must be a no-op there.
        assert PWSH_BASE_FLAGS == ["-NoProfile"]
    else:
        assert PWSH_BASE_FLAGS[:2] == ["-SettingsFile", str(_SETTINGS_FILE)]


def test_settings_file_silences_operational_channel():
    if sys.platform == "win32":
        pytest.skip("no settings file on win32")
    body = json.loads(_SETTINGS_FILE.read_text(encoding="ascii"))
    # The scriptblock dumps are logged at Warning; LogLevel Error is the
    # lowest level that suppresses them while keeping genuine errors.
    assert body == {"LogLevel": "Error"}


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="pwsh/powershell required",
)
def test_pwsh_accepts_the_base_flags():
    exe = shutil.which("pwsh") or shutil.which("powershell")
    assert exe is not None  # narrow for pyright (skipif above guarantees it)
    if Path(exe).name.startswith("powershell") and sys.platform != "win32":
        pytest.skip("POSIX 'powershell' binary predates -SettingsFile")
    proc = subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", "1+1"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2"


def test_no_test_hand_writes_the_noprofile_literal():
    """Ratchet: every test-side pwsh argv must go through PWSH_BASE_FLAGS.

    A bare ``"-NoProfile"`` literal in an argv list is how the journal
    flood came back — the helper is the only home for the suppression
    flags, so the literal must not reappear outside it.
    """
    offenders: list[str] = []
    for py in sorted(TESTS_DIR.rglob("*.py")):
        if py == Path(__file__).resolve():
            continue
        if py.relative_to(TESTS_DIR.parent).as_posix() == "tests/common/pwsh.py":
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if '"-NoProfile"' in line:
                offenders.append(f"{py.relative_to(TESTS_DIR.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "test-side pwsh argv must use *PWSH_BASE_FLAGS (journal-silencing), "
        "not a bare \"-NoProfile\" literal:\n" + "\n".join(offenders)
    )
