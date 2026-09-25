# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the routing hooks read ``VCT_PROJECT_ID`` from ``.claude/env``
the way the projection WRITES it.

``_lib/route-touched-path.sh`` / ``.ps1`` fall back to ``.claude/env`` when
the hook's environment carries no ``VCT_PROJECT_ID``. Both matched only the
bare ``VCT_PROJECT_ID=`` form, but the projection's managed block writes
``export VCT_PROJECT_ID="…"`` — so the fallback never found it, and the
Phase-8 write gate ran with no project id (silent-allow + a
``gate_skipped_no_project_id`` deferral).

These tests EXECUTE the real scripts — bash and PowerShell — against a real
managed block produced by the projection's own block builder, and pin both
to the Python line rule (:func:`vco_lib.envfile.env_value`) so the three
cannot drift.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.config_projection import _build_managed_block  # noqa: E402
from vco_lib.envfile import env_value  # noqa: E402

HOOKS = REPO_ROOT / "templates" / "hooks"
LIB_SH = HOOKS / "_lib" / "route-touched-path.sh"
LIB_PS1 = HOOKS / "_lib" / "route-touched-path.ps1"
PID = "0f6c1d9e-1234-4abc-9def-00000000beef"


def _env_without_pid() -> dict:
    env = dict(os.environ)
    env.pop("VCT_PROJECT_ID", None)
    return env


def _bash_pid(root: Path) -> str:
    out = subprocess.run(
        ["bash", "-c", '. "$1"; vco_route_init "$2" "$3" python3; printf %s "$VCT_PROJECT_ID"',
         "_", str(LIB_SH), str(HOOKS), str(root)],
        capture_output=True, text=True, env=_env_without_pid(), timeout=60, check=True)
    return out.stdout


def _pwsh_pid(root: Path) -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        pytest.skip("no PowerShell on this machine")
    lib = str(LIB_PS1).replace("'", "''")
    hooks = str(HOOKS).replace("'", "''")
    proj = str(root).replace("'", "''")
    out = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command",
         f". '{lib}'; Initialize-VcoRoute -HooksDir '{hooks}' -ProjectRoot '{proj}'; "
         "[Console]::Out.Write([string]$script:VcoRouteProjectId)"],
        capture_output=True, text=True, env=_env_without_pid(), timeout=120, check=True)
    return out.stdout


CASES = {
    # What the projection writes — the case that never matched before.
    "managed_block": _build_managed_block({"KG_COLLECTION": "P_KnowledgeGraph",
                                           "VCT_PROJECT_ID": PID}),
    "managed_block_crlf": _build_managed_block({"VCT_PROJECT_ID": PID}).replace("\n", "\r\n"),
    "bare": f"VCT_PROJECT_ID={PID}\n",
    "single_quoted": f"export VCT_PROJECT_ID='{PID}'\n",
    "user_line_first_wins": 'export VCT_PROJECT_ID="mine"\n'
                            + _build_managed_block({"VCT_PROJECT_ID": PID}),
    "commented_out": f'# export VCT_PROJECT_ID="{PID}"\n',
    "absent": _build_managed_block({"KG_COLLECTION": "P_KnowledgeGraph"}),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_bash_and_powershell_read_what_the_projection_writes(tmp_path, case):
    text = CASES[case]
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "env").write_bytes(text.encode("utf-8"))
    want = env_value(text, "VCT_PROJECT_ID") or ""
    if case.startswith("managed_block") or case in ("bare", "single_quoted"):
        assert want == PID  # the reference itself reads the value
    assert _bash_pid(tmp_path) == want, "bash"
    assert _pwsh_pid(tmp_path) == want, "PowerShell"


def test_the_hook_environment_still_wins(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "env").write_text(
        _build_managed_block({"VCT_PROJECT_ID": PID}), encoding="utf-8")
    env = _env_without_pid()
    env["VCT_PROJECT_ID"] = "from-env"
    out = subprocess.run(
        ["bash", "-c", '. "$1"; vco_route_init "$2" "$3" python3; printf %s "$VCT_PROJECT_ID"',
         "_", str(LIB_SH), str(HOOKS), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=60, check=True)
    assert out.stdout == "from-env"
