# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-3 (v0.2.73): lean-ctx-rewrite hook strips ``permissionDecision``.

lean-ctx 3.x's ``hook rewrite`` emits ``"permissionDecision": "allow"``
alongside ``updatedInput`` — on Claude Code >= 2.1.x that AUTO-APPROVES
every wrapped Bash command, bypassing the user's permission settings
(verified empirically on CC 2.1.172, 2026-07-03: an un-allowlisted Bash
call executed in headless mode solely because of the hook's "allow";
with the field stripped, ``updatedInput`` still applied and the normal
permission flow evaluated the rewritten command).

These tests drive ``templates/hooks/lean-ctx-rewrite.sh`` with a FAKE
``lean-ctx`` binary on PATH so the filter's decision arms are pinned
without needing the real upstream tool:

* allow-bearing response  -> field stripped, updatedInput preserved
* empty response (bypass) -> empty stdout (no rewrite)
* unparseable response    -> empty stdout (conservative: raw command
  under the normal permission flow beats emitting an auto-approval)
* VCT_DISABLE_HOOKS=1     -> empty stdout (global kill-switch first)

The .ps1 sibling implements the same strip natively (ConvertFrom-Json);
its structural parity is covered by the hook-parity CI gate, and a
pwsh-gated behavioural case runs here when pwsh is installed.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.sh"
PS1_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.ps1"

PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }
)

#: Canned response matching lean-ctx 3.4.5's real serialization.
ALLOW_RESPONSE = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    '"permissionDecision":"allow",'
    '"updatedInput":{"command":"lean-ctx -c \'git status\'"}}}'
)

_IS_WINDOWS = platform.system().lower().startswith("win")


def _make_fake_lean_ctx(bin_dir: Path, stdout_text: str) -> None:
    """Drop a fake ``lean-ctx`` on ``bin_dir`` that prints ``stdout_text``.

    The canned response is written to a SIDE FILE the shim cats — quoting
    it inline in the shim's source would break on responses containing
    single quotes (lean-ctx's real output wraps the command in them).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    response_file = bin_dir / "response.json"
    response_file.write_text(stdout_text, encoding="utf-8")
    fake = bin_dir / "lean-ctx"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # consume stdin like the real binary would
            cat > /dev/null
            cat '{response_file}'
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_sh_hook(tmp_path: Path, fake_stdout: str, extra_env: dict | None = None):
    """Run the .sh hook from a clean cwd with a fake lean-ctx on PATH."""
    bin_dir = tmp_path / "fakebin"
    _make_fake_lean_ctx(bin_dir, fake_stdout)
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SH_HOOK)],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )


@pytest.mark.skipif(_IS_WINDOWS, reason="bash hook; .ps1 sibling covered below")
class TestShPermissionStrip:
    def test_strips_permission_decision_keeps_updated_input(self, tmp_path):
        res = _run_sh_hook(tmp_path, ALLOW_RESPONSE)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, "a rewrite response must still be emitted"
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert "permissionDecision" not in hso, (
            "SECURITY: permissionDecision must be stripped — 'allow' "
            f"auto-approves every wrapped Bash command: {out}"
        )
        assert (
            hso["updatedInput"]["command"] == "lean-ctx -c 'git status'"
        ), f"updatedInput must survive the strip verbatim: {out}"
        assert hso["hookEventName"] == "PreToolUse"

    def test_empty_lean_ctx_output_stays_empty(self, tmp_path):
        """The per-call bypass path (lean-ctx emits nothing) is preserved."""
        res = _run_sh_hook(tmp_path, "")
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "empty response must pass through as no-op"

    def test_unparseable_lean_ctx_output_emits_nothing(self, tmp_path):
        """Conservative arm: garbage in -> NOTHING out (raw command runs
        under the normal permission flow; never re-emit unvetted JSON)."""
        res = _run_sh_hook(tmp_path, "this is not json {{{")
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_response_without_updated_input_emits_nothing(self, tmp_path):
        """An allow-only response (no rewrite) must be dropped entirely —
        there is nothing legitimate left once the auto-approval is gone."""
        allow_only = (
            '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"permissionDecision":"allow"}}'
        )
        res = _run_sh_hook(tmp_path, allow_only)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_vct_disable_hooks_short_circuits(self, tmp_path):
        res = _run_sh_hook(tmp_path, ALLOW_RESPONSE, {"VCT_DISABLE_HOOKS": "1"})
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_source_never_execs_lean_ctx_directly(self):
        """Regression pin: the pre-D-3 hook `exec`ed lean-ctx, forwarding
        its permissionDecision verbatim. The exec must not come back."""
        src = SH_HOOK.read_text(encoding="utf-8")
        assert "exec lean-ctx" not in src, (
            "lean-ctx-rewrite.sh must FILTER lean-ctx's response, not exec "
            "it (exec forwards permissionDecision:'allow' verbatim — D-3)"
        )
        assert "permissionDecision" in src, "strip filter missing from .sh"


@pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed on this host"
)
def test_ps1_strips_permission_decision(tmp_path):
    """Behavioural parity for the Windows sibling (pwsh-gated)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    # pwsh resolves `lean-ctx` via Get-Command / PATH; a .ps1 shim works
    # cross-OS under pwsh.
    (bin_dir / "lean-ctx.ps1").write_text(
        f"$null = $input\nWrite-Output '{ALLOW_RESPONSE}'\n", encoding="utf-8"
    )
    cwd = tmp_path / "proj"
    cwd.mkdir()
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    res = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PS1_HOOK)],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    assert out, "a rewrite response must still be emitted"
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert "permissionDecision" not in hso, f"ps1 must strip the field: {out}"
    assert hso["updatedInput"]["command"] == "lean-ctx -c 'git status'"


def test_ps1_source_carries_strip_filter():
    """Static parity pin (runs everywhere, no pwsh needed)."""
    src = PS1_HOOK.read_text(encoding="utf-8-sig")
    assert "permissionDecision" in src, "strip filter missing from .ps1"
    assert "updatedInput" in src
