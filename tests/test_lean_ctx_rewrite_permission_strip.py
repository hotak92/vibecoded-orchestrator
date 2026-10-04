# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-3 invariant (v0.2.73 → structural since v0.2.101): no auto-approval.

lean-ctx 3.x's own rewrite handler emits ``"permissionDecision": "allow"``
alongside ``updatedInput`` — on Claude Code >= 2.1.x that AUTO-APPROVES
every wrapped Bash command, bypassing the user's permission settings
(verified empirically on CC 2.1.172, 2026-07-03). From v0.2.73 the hook
filtered the field out of the delegated response; from v0.2.101
(allow-list inversion) the hook CONSTRUCTS the response itself and never
invokes the binary at rewrite time, so the field can never appear — the
D-3 invariant is structural, not a filter.

These tests drive ``templates/hooks/lean-ctx-rewrite.sh`` with a FAKE
``lean-ctx`` binary whose stdout is a permissionDecision-bearing response
(the exact shape lean-ctx 3.4.5 would emit). The pins:

* allow-listed command       -> hook-built rewrite, NO permissionDecision,
                                and the binary's canned stdout is provably
                                ignored (the wrapped command is the tee
                                wrapper, never the binary's suggestion)
* non-allow-listed command   -> empty stdout (raw, normal permission flow)
* VCT_DISABLE_HOOKS=1        -> empty stdout (global kill-switch first)
* the hook never execs lean-ctx (pre-D-3 regression pin)

The .ps1 sibling implements the same construction natively; its
behavioural parity case runs here when pwsh is installed, and structural
parity is covered by the hook-OS-parity gate. The full wrap/raw decision
table lives in tests/test_v02101_lean_ctx_allowlist_tee.py.
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

#: An allow-listed command (templates/hooks/_lib/lean-ctx-allowlist.txt).
PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm install"},
    }
)

#: Canned response matching lean-ctx 3.4.5's real serialization — carrying
#: the auto-approval field. If the hook ever went back to relaying the
#: binary's answer, this exact JSON (field included) would come through.
ALLOW_RESPONSE = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    '"permissionDecision":"allow",'
    '"updatedInput":{"command":"lean-ctx -c \'npm install\'"}}}'
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


def _run_sh_hook(tmp_path: Path, fake_stdout: str, extra_env: dict | None = None,
                 payload: str = PAYLOAD):
    """Run the .sh hook from a clean cwd with a fake lean-ctx on PATH."""
    bin_dir = tmp_path / "fakebin"
    _make_fake_lean_ctx(bin_dir, fake_stdout)
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    # tests/conftest.py pins CLAUDE_PROJECT_DIR to a suite-wide scratch
    # project; this test stages its own project cwd.
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SH_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )


@pytest.mark.skipif(_IS_WINDOWS, reason="bash hook; .ps1 sibling covered below")
class TestShResponseIsHookBuilt:
    def test_no_permission_decision_and_binary_stdout_ignored(self, tmp_path):
        """The binary offers an auto-approving rewrite; the hook must emit
        its OWN response (tee-wrapper form) with no permissionDecision."""
        res = _run_sh_hook(tmp_path, ALLOW_RESPONSE)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, "an allow-listed command must produce a rewrite"
        assert "permissionDecision" not in out, (
            "SECURITY: no auto-approval field may ever reach Claude Code — "
            f"it would bypass the permission flow: {out}"
        )
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        wrapped = hso["updatedInput"]["command"]
        assert "lean-ctx-tee.sh" in wrapped, (
            "the rewrite must be the hook-built tee-wrapper command, not "
            f"the binary's canned suggestion: {wrapped}"
        )
        assert "lean-ctx -c" not in wrapped, (
            f"the binary's canned wrap form leaked into the rewrite: {wrapped}"
        )

    def test_unparseable_binary_stdout_is_irrelevant(self, tmp_path):
        """The pre-v0.2.101 hook parsed the binary's stdout; garbage meant
        no rewrite. Now the binary's stdout is never read at rewrite time —
        garbage changes nothing (this arm pins the retirement)."""
        res = _run_sh_hook(tmp_path, "this is not json {{{")
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, "allow-listed command must still rewrite"
        assert "permissionDecision" not in out
        assert "lean-ctx-tee.sh" in json.loads(
            out)["hookSpecificOutput"]["updatedInput"]["command"]

    def test_non_allow_listed_command_emits_nothing(self, tmp_path):
        payload = json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "git status"}}
        )
        res = _run_sh_hook(tmp_path, ALLOW_RESPONSE, payload=payload)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            "non-allow-listed command must run raw under the normal "
            "permission flow"
        )

    def test_vct_disable_hooks_short_circuits(self, tmp_path):
        res = _run_sh_hook(tmp_path, ALLOW_RESPONSE, {"VCT_DISABLE_HOOKS": "1"})
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_source_never_execs_lean_ctx_directly(self):
        """Regression pin: the pre-D-3 hook `exec`ed lean-ctx, forwarding
        its permissionDecision verbatim. The exec must not come back — and
        since v0.2.101 the delegation that required the strip filter is
        retired too."""
        src = SH_HOOK.read_text(encoding="utf-8")
        assert "exec lean-ctx" not in src, (
            "lean-ctx-rewrite.sh must never exec lean-ctx (exec forwards "
            "permissionDecision:'allow' verbatim — D-3)"
        )
        assert "hook rewrite" not in src, (
            "the delegation to lean-ctx's own rewrite handler is retired; "
            "the hook constructs updatedInput itself"
        )


@pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed on this host"
)
def test_ps1_response_is_hook_built(tmp_path):
    """Behavioural parity for the Windows sibling (pwsh-gated)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    # pwsh resolves `lean-ctx` via Get-Command / PATH; a .ps1 shim works
    # cross-OS under pwsh. The canned response is written to a SIDE FILE
    # the shim reads with `Get-Content -Raw` — NOT interpolated into an
    # inline single-quoted `Write-Output '...'` (ALLOW_RESPONSE contains
    # single quotes; inlining would mangle the JSON).
    response_file = bin_dir / "response.json"
    response_file.write_text(ALLOW_RESPONSE, encoding="utf-8")
    (bin_dir / "lean-ctx.ps1").write_text(
        "$null = $input\n"
        f"Write-Output (Get-Content -Raw -LiteralPath '{response_file}')\n",
        encoding="utf-8",
    )
    cwd = tmp_path / "proj"
    cwd.mkdir()
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
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
    assert "permissionDecision" not in out, (
        f"ps1 must never emit the auto-approval field: {out}"
    )
    data = json.loads(out)
    wrapped = data["hookSpecificOutput"]["updatedInput"]["command"]
    assert "lean-ctx-tee.ps1" in wrapped, wrapped


def test_ps1_source_never_delegates():
    """Static parity pin (runs everywhere, no pwsh needed)."""
    src = PS1_HOOK.read_text(encoding="utf-8-sig")
    assert "hook rewrite" not in src, (
        "the delegation to lean-ctx's own rewrite handler is retired"
    )
    assert "updatedInput" in src
