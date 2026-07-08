# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-14 (v0.2.75): TOUCAN log truncates content fields + size-caps/rotates.

``templates/hooks/pre-tool-use.sh`` (and ``.ps1``) log every tool call to
``.claude/logs/toucan_dataset.jsonl``. Pre-D-14 the row embedded the WHOLE
Write ``content`` / WHOLE Bash ``command`` unbounded — any secret that ever
transited a tool call was durably duplicated into a plaintext log that
outlived the scrubbed originals, with no truncation and no size cap.

D-14 mitigations (both mirrored .sh/.ps1):
  1. Truncate the known content-bearing fields (content / new_string /
     old_string / command) to 2000 chars before serializing.
  2. Size-cap + rotate the JSONL at ~5 MB (oldest rows dropped).

These tests drive the real hook (bash), asserting:
  * oversized Write content → the persisted row is truncated (act).
  * short content → untouched (leave-alone).
  * an already-oversized log → rotated to the tail on the next write (act).
  * an under-cap log → left intact (leave-alone).
  * ps1 parity (pwsh-gated).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"
PRE_TOOL_USE_SH = HOOKS_DIR / "pre-tool-use.sh"
PRE_TOOL_USE_PS1 = HOOKS_DIR / "pre-tool-use.ps1"

_FIELD_CAP = 2000

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook; .ps1 sibling covered by the pwsh-gated case below.",
)


def _setup(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _run_sh(payload: dict, project_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        ["bash", str(PRE_TOOL_USE_SH)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=15,
    )


def _last_row(project_root: Path) -> dict | None:
    log = project_root / ".claude" / "logs" / "toucan_dataset.jsonl"
    if not log.exists():
        return None
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def test_oversized_write_content_is_truncated(tmp_path):
    _setup(tmp_path)
    big = "A" * (_FIELD_CAP + 5000)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "f.txt"), "content": big},
        "user_message": "write it",
        "session_id": "s1",
    }
    res = _run_sh(payload, tmp_path)
    assert res.returncode == 0, res.stderr
    row = _last_row(tmp_path)
    assert row is not None
    stored = row["tool_args"]["content"]
    assert len(stored) < len(big), "content must be truncated"
    assert stored.startswith("A" * _FIELD_CAP)
    assert "truncated by D-14" in stored


def test_short_content_is_untouched(tmp_path):
    _setup(tmp_path)
    small = "B" * 100
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "f.txt"), "content": small},
        "user_message": "write it",
        "session_id": "s1",
    }
    res = _run_sh(payload, tmp_path)
    assert res.returncode == 0, res.stderr
    row = _last_row(tmp_path)
    assert row["tool_args"]["content"] == small, "short content must be verbatim"


def test_oversized_bash_command_is_truncated(tmp_path):
    _setup(tmp_path)
    big_cmd = "echo " + ("x" * (_FIELD_CAP + 3000))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": big_cmd},
        "user_message": "run it",
        "session_id": "s1",
    }
    res = _run_sh(payload, tmp_path)
    # Bash branch may exit 2 if a security rule trips, but the TOUCAN row is
    # written BEFORE the security scan. `echo x…` is benign → exit 0.
    row = _last_row(tmp_path)
    assert row is not None
    assert len(row["tool_args"]["command"]) < len(big_cmd)
    assert "truncated by D-14" in row["tool_args"]["command"]


def test_oversized_log_is_rotated_on_next_write(tmp_path):
    """An already-oversized log is rotated to its tail on the next append
    (act). We seed >5 MB of rows, then trigger one more write."""
    _setup(tmp_path)
    log = tmp_path / ".claude" / "logs" / "toucan_dataset.jsonl"
    # Seed ~6 MB: each row ~600 bytes → ~10500 rows.
    row = json.dumps({"timestamp": "t", "query": "q", "chosen_tool": "X",
                      "tool_args": {"pad": "p" * 500}, "session_id": "old",
                      "agent_id": "", "agent_type": ""})
    with log.open("w", encoding="utf-8") as fh:
        n = (6 * 1024 * 1024) // (len(row) + 1) + 100
        for _ in range(n):
            fh.write(row + "\n")
    size_before = log.stat().st_size
    assert size_before > 5 * 1024 * 1024

    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(tmp_path / "z.py")},
        "user_message": "read", "session_id": "s2",
    }
    (tmp_path / "z.py").write_text("# stub\n")
    res = _run_sh(payload, tmp_path)
    assert res.returncode == 0, res.stderr

    size_after = log.stat().st_size
    assert size_after < size_before, "oversized log must be rotated down"
    # The newest row (our fresh Read) must survive the rotation.
    last = _last_row(tmp_path)
    assert last["session_id"] == "s2"
    kept = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(kept) <= 2001, "rotation keeps the tail (~2000 rows)"


def test_undercap_log_not_rotated(tmp_path):
    """A small log is left intact — no rotation (leave-alone)."""
    _setup(tmp_path)
    log = tmp_path / ".claude" / "logs" / "toucan_dataset.jsonl"
    log.write_text('{"seed":1}\n', encoding="utf-8")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(tmp_path / "z.py")},
        "user_message": "read", "session_id": "s3",
    }
    (tmp_path / "z.py").write_text("# stub\n")
    res = _run_sh(payload, tmp_path)
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # seed row + the new row = 2. No rotation dropped the seed.
    assert lines[0] == '{"seed":1}', "under-cap log's oldest row must survive"
    assert len(lines) == 2


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_ps1_truncates_oversized_content(tmp_path):
    _setup(tmp_path)
    big = "A" * (_FIELD_CAP + 5000)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "f.txt"), "content": big},
        "user_message": "write it", "session_id": "s1",
    }
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env.pop("VCT_DISABLE_HOOKS", None)
    subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PRE_TOOL_USE_PS1)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30,
    )
    row = _last_row(tmp_path)
    assert row is not None
    assert len(row["tool_args"]["content"]) < len(big)
    assert "truncated by D-14" in row["tool_args"]["content"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
