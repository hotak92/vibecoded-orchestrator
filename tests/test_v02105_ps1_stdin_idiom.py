# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 -- every shipped ``.ps1`` hook reads its stdin payload one way.

The house idiom is ``try { $HookStdin = [Console]::In.ReadToEnd() } catch { }``
(40 hooks). Three hooks stored the payload in ``$Input`` instead. ``$input``
is PowerShell's AUTOMATIC pipeline-input variable (``about_Automatic_Variables``;
PSScriptAnalyzer flags assigning it, ``PSAvoidAssignmentToAutomaticVariable``):
a ``-File`` script that names it has its redirected stdin fed to that
enumerator, so ``[Console]::In.ReadToEnd()`` comes back EMPTY and the hook
acts on no payload. Measured below with the same pwsh the suite drives the
hooks with; ``post-git-commit-kg-sync.ps1`` lost its session id this way
(its end-to-end test lives in ``test_v02105_kg_reviewer_pending.py``).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
_PWSH = shutil.which("pwsh")

# `$Input =` / `$input=` -- an assignment to the automatic variable (any case).
_ASSIGN_INPUT = re.compile(r"\$input\s*=(?!=)", re.IGNORECASE)


def test_no_shipped_ps1_hook_stores_stdin_in_the_automatic_input_variable():
    offenders = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in HOOKS.rglob("*.ps1")
        if _ASSIGN_INPUT.search(p.read_text(encoding="utf-8", errors="replace")))
    assert offenders == [], (
        "assigning $Input loses the hook payload -- use the house idiom "
        f"`$HookStdin = [Console]::In.ReadToEnd()`: {offenders}")


@pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("name, expect_payload", [("HookStdin", True),
                                                  ("Input", False)])
def test_the_variable_name_decides_whether_the_payload_arrives(
        tmp_path, name, expect_payload):
    """The evidence behind the rule, measured (both sides)."""
    script = tmp_path / "probe.ps1"
    script.write_text(
        f'${name} = ""\n'
        f"try {{ ${name} = [Console]::In.ReadToEnd() }} catch {{ }}\n"
        f'"len=" + ([string]${name}).Length\n', encoding="utf-8")
    proc = subprocess.run(
        [str(_PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(script)],
        input='{"session_id":"s1"}', capture_output=True, text=True,
        timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = int(proc.stdout.strip().rsplit("len=", 1)[-1])
    assert (got > 0) is expect_payload, proc.stdout
