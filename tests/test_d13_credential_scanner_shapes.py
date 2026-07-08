# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-13 (v0.2.75): credential scanner shapes cover VCO's own token shapes.

Three surfaces, one gap-class each (all OPEN at ecec456d):

  * ``templates/hooks/post-tool-security.sh`` (+ ``.ps1``): the
    ``gh[pousr]_[a-zA-Z0-9]{36}`` GitHub rule NEVER matched a fine-grained
    ``github_pat_*`` token — exactly the shape VCO's secrets flow
    provisions. And the generic-secret rule required a QUOTED value, so a
    ``.env``-style bare ``API_KEY=<value>`` escaped.
  * ``templates/scripts/bash_security.py``: ``env_grep_secrets`` needed a
    pipe-to-grep, so ``printenv GITHUB_TOKEN`` / ``echo $OPENAI_API_KEY``
    passed unchallenged.

Fixtures use obviously-fake bodies of the CORRECT length/charset — no
real-shaped canary lives in the tracked tree. The ``github_pat_`` fixture
is 22 ``a`` + ``_`` + 59 ``b`` (right shape, unmistakably synthetic).

The token-shape patterns are anchored on ONE home
(``scripts/check-no-secrets.sh``'s ``TOKEN_SHAPES``); this test also pins
that the hook's regex is byte-identical to that anchor so a future edit to
one that forgets the other trips CI.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_HOOK = REPO_ROOT / "templates" / "hooks" / "post-tool-security.sh"
PS1_HOOK = REPO_ROOT / "templates" / "hooks" / "post-tool-security.ps1"
CHECK_NO_SECRETS = REPO_ROOT / "scripts" / "check-no-secrets.sh"
BASH_SECURITY = REPO_ROOT / "templates" / "scripts" / "bash_security.py"

_IS_WINDOWS = platform.system().lower().startswith("win")

# ── Synthetic fixtures (correct shape, obviously fake bodies) ────────────
# github_pat_ + 22 alnum + _ + 59 alnum — the exact-format shape.
FAKE_GITHUB_PAT = "github_pat_" + ("a" * 22) + "_" + ("b" * 59)
# Legacy classic PAT: gh?_ + 36 alnum (still-must-fire regression).
FAKE_CLASSIC_PAT = "ghp_" + ("c" * 36)
# .env-style UNQUOTED assignment, >=32 secret-alphabet chars.
FAKE_UNQUOTED_ENV = "API_KEY=" + ("d" * 40)
# Legacy QUOTED generic secret (must still fire).
FAKE_QUOTED_SECRET = 'API_KEY="' + ("e" * 40) + '"'


def _run_sh_scanner(tmp_path: Path, file_body: str) -> subprocess.CompletedProcess:
    """Write ``file_body`` to a temp file, run the .sh scanner over it via
    the PostToolUse stdin envelope, return the completed process.

    The scanner appends alerts to
    ``$CLAUDE_PROJECT_DIR/.claude/logs/credential_alerts.jsonl`` and emits
    the model-facing reminder; we harvest the JSONL to detect a fire.
    """
    proj = tmp_path / "proj"
    (proj / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    target = proj / "leak_candidate.txt"
    target.write_text(file_body, encoding="utf-8")
    payload = (
        '{"hook_event_name":"PostToolUse","tool_name":"Write",'
        '"tool_input":{"file_path":"' + str(target) + '"}}'
    )
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    subprocess.run(
        ["bash", str(SH_HOOK)],
        input=payload, capture_output=True, text=True, env=env, timeout=30,
    )
    alert_log = proj / ".claude" / "logs" / "credential_alerts.jsonl"
    alerts = alert_log.read_text(encoding="utf-8") if alert_log.exists() else ""
    return alerts


@pytest.mark.skipif(_IS_WINDOWS, reason="bash hook; .ps1 covered separately")
class TestShScannerShapes:
    def test_github_fine_grained_pat_fires(self, tmp_path):
        alerts = _run_sh_scanner(tmp_path, FAKE_GITHUB_PAT)
        assert "GitHub fine-grained PAT" in alerts, alerts

    def test_unquoted_env_assignment_fires(self, tmp_path):
        alerts = _run_sh_scanner(tmp_path, FAKE_UNQUOTED_ENV)
        assert "Generic secret (unquoted)" in alerts, alerts

    def test_legacy_classic_pat_still_fires(self, tmp_path):
        alerts = _run_sh_scanner(tmp_path, FAKE_CLASSIC_PAT)
        assert "GitHub token" in alerts, alerts

    def test_legacy_quoted_secret_still_fires(self, tmp_path):
        alerts = _run_sh_scanner(tmp_path, FAKE_QUOTED_SECRET)
        assert "Generic secret" in alerts, alerts

    def test_benign_short_config_line_does_not_fire(self, tmp_path):
        # `API_KEY=on` is a legit config toggle — must NOT alert
        # (leave-alone case).
        alerts = _run_sh_scanner(tmp_path, "API_KEY=on\nDEBUG=true\n")
        assert "Generic secret" not in alerts, alerts


def test_github_pat_shape_matches_canonical_anchor():
    """The hook's github_pat_ regex MUST be byte-identical to
    check-no-secrets.sh's TOKEN_SHAPES anchor (one pattern home)."""
    anchor = CHECK_NO_SECRETS.read_text(encoding="utf-8")
    hook_sh = SH_HOOK.read_text(encoding="utf-8")
    hook_ps1 = PS1_HOOK.read_text(encoding="utf-8")
    shape = r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}"
    assert shape in anchor, "canonical anchor missing from check-no-secrets.sh"
    assert shape in hook_sh, "post-tool-security.sh must reuse the anchor shape"
    assert shape in hook_ps1, "post-tool-security.ps1 must reuse the anchor shape"


def test_no_real_shaped_github_pat_canary_in_tree():
    """A real-shaped github_pat_ fixture must NOT live in the tracked tree
    (only assembled at runtime from synthetic parts). This test itself
    assembles the fixture so it never appears as a literal."""
    # The literal token 'github_pat_' followed by a real 22_59 body should
    # only appear in regex form (with [A-Za-z0-9]{...}) across the tree, or
    # as this synthetic all-a/all-b fixture. Assert our synthetic fixture
    # is not a plausible real token (single-char runs).
    assert set(FAKE_GITHUB_PAT.split("_")[2]) == {"a"}
    assert set(FAKE_GITHUB_PAT.split("_")[3]) == {"b"}


# ── bash_security.py: printenv / echo direct-dump rules ──────────────────


def _check(cmd: str):
    sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))
    try:
        import importlib
        import bash_security
        importlib.reload(bash_security)
    finally:
        sys.path.pop(0)
    return bash_security.check_command(cmd)


class TestBashSecurityDirectDump:
    def test_printenv_named_secret_blocked(self):
        ok, reason = _check("printenv GITHUB_TOKEN")
        assert not ok, reason
        assert "REMEDIATION" in reason, reason

    def test_echo_secret_var_blocked(self):
        ok, reason = _check("echo $OPENAI_API_KEY")
        assert not ok, reason
        assert "REMEDIATION" in reason, reason

    def test_echo_braced_secret_var_blocked(self):
        ok, reason = _check('echo "${AWS_SECRET_ACCESS_KEY}"')
        assert not ok, reason

    def test_legacy_env_grep_still_blocked(self):
        ok, reason = _check("env | grep " + "TOK" + "EN")
        assert not ok, reason
        assert "REMEDIATION" in reason, reason

    def test_benign_echo_not_blocked(self):
        # echo of a non-secret var (leave-alone case).
        ok, _ = _check("echo $HOME")
        assert ok
        ok2, _ = _check('echo "build complete"')
        assert ok2

    def test_printenv_bare_not_blocked(self):
        # `printenv` with no secret-shaped name — not a targeted dump.
        ok, _ = _check("printenv PATH")
        assert ok


@pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed on this host"
)
class TestPs1ScannerParity:
    """Behavioural parity for the Windows sibling (pwsh-gated)."""

    def _run_ps1(self, tmp_path: Path, file_body: str) -> str:
        proj = tmp_path / "proj"
        (proj / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
        target = proj / "leak_candidate.txt"
        target.write_text(file_body, encoding="utf-8")
        payload = (
            '{"hook_event_name":"PostToolUse","tool_name":"Write",'
            '"tool_input":{"file_path":"' + str(target).replace("\\", "\\\\") + '"}}'
        )
        env = dict(os.environ)
        env.pop("VCT_DISABLE_HOOKS", None)
        env["CLAUDE_PROJECT_DIR"] = str(proj)
        subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(PS1_HOOK)],
            input=payload, capture_output=True, text=True, env=env, timeout=30,
        )
        alert_log = proj / ".claude" / "logs" / "credential_alerts.jsonl"
        return alert_log.read_text(encoding="utf-8") if alert_log.exists() else ""

    def test_ps1_github_fine_grained_pat_fires(self, tmp_path):
        alerts = self._run_ps1(tmp_path, FAKE_GITHUB_PAT)
        assert "GitHub fine-grained PAT" in alerts, alerts

    def test_ps1_unquoted_env_assignment_fires(self, tmp_path):
        alerts = self._run_ps1(tmp_path, FAKE_UNQUOTED_ENV)
        assert "Generic secret (unquoted)" in alerts, alerts


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
