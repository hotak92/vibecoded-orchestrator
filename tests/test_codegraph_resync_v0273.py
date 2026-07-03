# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 R-track: resync trigger observability + owed-gating + verification.

R-5 (RT-2): detached children log to <vct_root_dir>/logs/resync-<proj>-<ts>.log
            instead of DEVNULL; the deferral resume command is shlex-quoted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_resync as cr  # noqa: E402


def _stub_analyzer_tree(root: Path) -> None:
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")


class _FakeProc:
    pid = 4321


# ─────────────────────────── R-5: log file ───────────────────────────


def test_children_log_to_vct_root_logs_dir(monkeypatch, tmp_path):
    """stdout/stderr of BOTH detached children go to the per-spawn log file
    (not DEVNULL), created under <vct_root_dir>/logs/."""
    state_dir = tmp_path / "vct-state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    spawned = []

    def _fake_popen(argv, **kwargs):
        spawned.append({"argv": argv, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(cr.subprocess, "Popen", _fake_popen)
    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    logs = list((state_dir / "logs").glob("resync-MyProj-*.log"))
    assert len(logs) == 1, "one per-spawn log file must exist"
    assert b"codegraph resync for MyProj" in logs[0].read_bytes()
    for rec in spawned:
        out = rec["kwargs"]["stdout"]
        assert out is not subprocess.DEVNULL, "children must not be DEVNULL'd"
        assert Path(out.name) == logs[0]
        assert rec["kwargs"]["stderr"] is out


def test_log_prep_failure_degrades_to_devnull(monkeypatch, tmp_path):
    """Log-path failure must never block the spawn — degrade to DEVNULL."""
    monkeypatch.setattr(cr, "_resync_log_path", lambda name: None)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    spawned = []

    def _fake_popen(argv, **kwargs):
        spawned.append(kwargs)
        return _FakeProc()

    monkeypatch.setattr(cr.subprocess, "Popen", _fake_popen)
    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    assert all(k["stdout"] is subprocess.DEVNULL for k in spawned)


def test_log_filename_sanitizes_project_name(monkeypatch, tmp_path):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    p = cr._resync_log_path("weird name/with:chars")
    assert p is not None
    assert p.name.startswith("resync-weird_name_with_chars-")
    assert "/" not in p.name and ":" not in p.name


# ─────────────────── R-5 rider (A-1): quoted resume command ─────────────────


def test_resume_command_is_shlex_quoted(monkeypatch, tmp_path):
    """A repo path containing spaces must survive into the deferral command."""
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: False)
    repo = tmp_path / "my repo with spaces"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "deferred"
    if result.deferral is not None:
        cmd = result.deferral.command_to_apply
        assert "'" in cmd or '"' in cmd, "spaced path must be quoted"
        # The quoted command round-trips through shlex to the same argv.
        import shlex

        parts = shlex.split(cmd)
        assert str(repo) in parts
        assert parts[-2:] == ["--project", "MyProj"]
