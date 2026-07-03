# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 HK-1 / D-8 / D-9 / D-16 — post-file-edit.sh hardening.

- HK-1: the stdin payload is parsed by exactly ONE Python interpreter
  (previously four separate `$PY -c` spawns), yet still yields the same
  four fields (file_path, agent_id, agent_type, session_id).
- D-8: the every-10-edits duplicate-scan report is persisted + surfaced
  (not greped to dropped stdout); a corrupted counter no longer aborts
  the hook under `set -e`.
- D-9: knowledge/docs prefix matching uses a trailing slash so sibling
  directories (knowledge_base/, docs-archive/) don't sync.
- D-16: PROJECT_ROOT honors CLAUDE_PROJECT_DIR.

POSIX-only. Hook-spawning tests MUST clear ambient VCT_VENV (wave-1
diagram-test lesson).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POST_FILE_EDIT = REPO_ROOT / "templates" / "hooks" / "post-file-edit.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hooks are POSIX-only.",
)


def _base_env(project_root: Path) -> dict:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Hard rule (wave-1): never inherit an ambient venv pin.
    env.pop("VCT_VENV", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    return env


def _run(project_root: Path, payload: dict, extra_env: dict | None = None):
    env = _base_env(project_root)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(POST_FILE_EDIT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def _skeleton(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --------------------------------------------------------------------------- #
# HK-1: single-parse — count Python interpreter spawns for the parse block.
# --------------------------------------------------------------------------- #


def test_stdin_parsed_by_single_interpreter(tmp_path):
    """Shim $PY to a counting wrapper; the parse block must invoke it once.

    We point find-python.sh's resolution at a wrapper that logs each
    invocation. A non-code, non-knowledge file short-circuits the hook
    right after the parse block, so the ONLY $PY calls are the parse
    (1 under HK-1; was 4 pre-HK-1) — the diagram md5 path is not reached.
    """
    _skeleton(tmp_path)
    target = tmp_path / "scratch.notrecognized"
    target.write_text("# noop\n")

    counter = tmp_path / "py_calls.log"
    bindir = tmp_path / "shimbin"
    bindir.mkdir()
    # find-python.sh resolves `python3` first — shadow it on PATH with a
    # counting wrapper that delegates to the real interpreter.
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "call" >> "{counter}"\n'
        f'exec {sys.executable} "$@"\n'
    )
    os.chmod(shim, 0o755)

    payload = {
        "tool_input": {"file_path": str(target)},
        "session_id": "sess-1",
        "agent_id": "agent-1",
        "agent_type": "@agent-x",
    }
    env_path = f"{bindir}:{os.environ.get('PATH', '')}"
    result = _run(tmp_path, payload, {"PATH": env_path})
    assert result.returncode == 0, result.stderr

    n_calls = 0
    if counter.exists():
        n_calls = len([ln for ln in counter.read_text().splitlines() if ln.strip()])
    assert n_calls == 1, (
        f"HK-1: expected exactly ONE interpreter spawn for the parse block, "
        f"got {n_calls}. stderr={result.stderr!r}"
    )


def test_parse_extracts_all_fields_and_exports(tmp_path):
    """The single parse still yields file_path + agent identity + session,
    observable via the exports the hook makes for its children."""
    _skeleton(tmp_path)
    target = tmp_path / "scratch.notrecognized"
    target.write_text("# noop\n")
    payload = {
        "tool_input": {"file_path": str(target)},
        "session_id": "S-42",
        "agent_id": "A-42",
        "agent_type": "@agent-probe",
    }
    env = _base_env(tmp_path)
    for k in ("VCT_AGENT_ID", "VCT_AGENT_TYPE", "VCT_SESSION_ID"):
        env.pop(k, None)
    probe = subprocess.run(
        [
            "bash", "-c",
            f'(. "{POST_FILE_EDIT}" <<< {json.dumps(json.dumps(payload))}; '
            'echo "ID=${VCT_AGENT_ID:-_}|TY=${VCT_AGENT_TYPE:-_}|SE=${VCT_SESSION_ID:-_}")',
        ],
        capture_output=True, text=True, env=env, timeout=20,
    )
    line = next(
        (ln for ln in (probe.stdout + probe.stderr).splitlines() if ln.startswith("ID=")),
        "",
    )
    assert "ID=A-42" in line, line
    assert "TY=@agent-probe" in line, line
    assert "SE=S-42" in line, line


def test_malformed_stdin_soft_fails(tmp_path):
    """Non-JSON stdin → all fields empty → EDITED_FILE empty → exit 0."""
    _skeleton(tmp_path)
    env = _base_env(tmp_path)
    result = subprocess.run(
        ["bash", str(POST_FILE_EDIT)],
        input="this is not json",
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# D-9: sibling-directory prefix guard.
# --------------------------------------------------------------------------- #


def test_knowledge_sibling_dir_not_synced(tmp_path):
    """A file under knowledge_base/ (sibling of knowledge/) must NOT be
    treated as a KG file → no .kg_edit_count increment."""
    _skeleton(tmp_path)
    sibling = tmp_path / "knowledge_base"
    sibling.mkdir()
    f = sibling / "x.md"
    f.write_text("# not a KG node\n")
    payload = {"tool_input": {"file_path": str(f)}, "session_id": "s"}
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    # The KG branch (which writes .kg_edit_count) must not have fired.
    assert not (tmp_path / ".claude/logs/.kg_edit_count").exists()


def test_real_knowledge_dir_recognized(tmp_path):
    """Control: a file under knowledge/ DOES enter the KG branch
    (observable via .kg_edit_count being created)."""
    _skeleton(tmp_path)
    (tmp_path / "knowledge").mkdir()
    f = tmp_path / "knowledge" / "node.md"
    f.write_text("# a node\n")
    payload = {"tool_input": {"file_path": str(f)}, "session_id": "s"}
    # kg-sync script absent under tmp — the branch still runs its counter
    # logic before the (backgrounded) sync command. Provide a stub scripts
    # dir so `.claude/scripts/kg-sync` path resolution doesn't matter (the
    # debounce schedule backgrounds it; the counter write is synchronous).
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".claude/logs/.kg_edit_count").exists()


# --------------------------------------------------------------------------- #
# D-8: corrupted counter must not abort the hook under set -e.
# --------------------------------------------------------------------------- #


def test_corrupted_edit_counter_does_not_abort(tmp_path):
    """A non-numeric .kg_edit_count is coerced to 0, not fatal."""
    _skeleton(tmp_path)
    (tmp_path / "knowledge").mkdir()
    counter = tmp_path / ".claude/logs/.kg_edit_count"
    counter.write_text("GARBAGE\n")
    f = tmp_path / "knowledge" / "node.md"
    f.write_text("# a node\n")
    payload = {"tool_input": {"file_path": str(f)}, "session_id": "s"}
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    # Counter reset to a numeric value.
    val = counter.read_text().strip()
    assert val.isdigit(), f"counter should be numeric, got {val!r}"


def test_pending_duplicate_report_is_surfaced(tmp_path):
    """A pre-staged duplicate report is emitted through additionalContext
    on the next KG-file edit, then consumed."""
    _skeleton(tmp_path)
    (tmp_path / "knowledge").mkdir()
    report = tmp_path / ".claude/state/kg_duplicates_report.txt"
    report.write_text("# KG duplicate scan\n⚠️ possible dup: A ~ B (0.97)\n")
    f = tmp_path / "knowledge" / "node.md"
    f.write_text("# a node\n")
    payload = {"tool_input": {"file_path": str(f)}, "session_id": "s"}
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    # Surfaced via the PostToolUse additionalContext envelope on stdout.
    assert "KG duplicate scan" in result.stdout, result.stdout
    # Consumed (removed) so it shows once.
    assert not report.exists()


# --------------------------------------------------------------------------- #
# D-16: CLAUDE_PROJECT_DIR is honored for PROJECT_ROOT.
# --------------------------------------------------------------------------- #


def test_claude_project_dir_drives_project_root(tmp_path):
    """State/log paths resolve under CLAUDE_PROJECT_DIR, not the
    script-relative root."""
    _skeleton(tmp_path)
    (tmp_path / "knowledge").mkdir()
    f = tmp_path / "knowledge" / "node.md"
    f.write_text("# a node\n")
    payload = {"tool_input": {"file_path": str(f)}, "session_id": "s"}
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    # The counter lands under the tmp CLAUDE_PROJECT_DIR, proving the root.
    assert (tmp_path / ".claude/logs/.kg_edit_count").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
