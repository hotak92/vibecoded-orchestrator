# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the .claude/diagrams/** branch added to post-file-edit.sh.

We exercise the branch end-to-end:
  - bash -n on the modified hook (syntax check).
  - dry-run dispatch: hook with a diagrams payload should attempt to
    invoke `vco_lib.diagram_indexer index <path>` (we replace the venv
    Python with a stub that records the call) AND respect the 60s
    throttle on repeat invocations of the same file.
  - non-diagram paths (a .py edit) should NOT touch the diagrams branch.

POSIX-only — Windows runners skip (no bash). The PowerShell sibling has
its own parity-confirmed body; visual parity is enforced by
.github/scripts/check_hook_parity.py and the .ps1 patterns mirror this
test's expectations.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "post-file-edit.sh"


def _have_bash() -> bool:
    return shutil.which("bash") is not None


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_hook_bash_syntax_valid():
    """The diagrams branch must not break bash -n parsing."""
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, "-n", str(HOOK_SH)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, (
        f"syntax error in {HOOK_SH}: {result.stderr}"
    )


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_hook_contains_diagrams_branch():
    """Light static check — the diagrams branch must be present in the
    same file we exercise dynamically below."""
    text = HOOK_SH.read_text()
    assert "DIAGRAMS_DIR=" in text
    assert ".claude/diagrams" in text
    assert "vco_lib.diagram_indexer" in text
    assert "diagram_idx_" in text  # throttle file key prefix


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_non_diagram_edit_does_not_touch_throttle(tmp_path: Path):
    """A non-diagram edit must not create the diagrams-throttle dir/file."""
    bash = shutil.which("bash")
    assert bash is not None

    # Build a fake project root containing the hook.
    project = tmp_path / "fakeproj"
    (project / ".claude" / "hooks" / "_lib").mkdir(parents=True)
    # Copy the hook and its dependencies.
    shutil.copy(HOOK_SH, project / ".claude" / "hooks" / "post-file-edit.sh")
    for lib in (
        "find-python.sh",
        "stderr-cap.sh",
        "emit-context.sh",
        "resolve-vco-venv.sh",  # v0.2.46 post-adversarial F1
        "kg-sync-debounce.sh",  # 2026-06-18 write-amplification debounce
    ):
        src = REPO_ROOT / "templates" / "hooks" / "_lib" / lib
        if src.exists():
            shutil.copy(src, project / ".claude" / "hooks" / "_lib" / lib)

    # A python file under .claude/skills/ — triggers the workflow branch
    # but NOT the diagrams branch.
    skill = project / ".claude" / "skills" / "some-skill.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# skill")

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(skill)},
    }

    env = os.environ.copy()
    env["VCT_DISABLE_HOOKS"] = ""
    env["PATH"] = (
        os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    )
    result = subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "post-file-edit.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,  # contention-tolerant (was 10s; flaked under parallel CI load)
    )
    # Hook should complete fine.
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # No diagrams-throttle artefacts.
    throttle_dir = project / ".claude" / "state"
    if throttle_dir.exists():
        diagram_files = list(throttle_dir.glob("diagram_idx_*.ts"))
        assert diagram_files == [], f"unexpected throttle files: {diagram_files}"


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_diagram_edit_creates_throttle_and_invokes_indexer(tmp_path: Path):
    """A diagram edit triggers the indexer subprocess and writes the
    throttle file. We stub the venv-Python with a script that records
    its argv so we can assert the dispatch happened without needing a
    real Weaviate / DB."""
    bash = shutil.which("bash")
    assert bash is not None

    # Build a project tree with the hook + a stub python "venv".
    project = tmp_path / "fakeproj"
    (project / ".claude" / "hooks" / "_lib").mkdir(parents=True)
    (project / ".claude" / "diagrams" / "gui").mkdir(parents=True)
    (project / ".venv" / "bin").mkdir(parents=True)

    shutil.copy(HOOK_SH, project / ".claude" / "hooks" / "post-file-edit.sh")
    for lib in (
        "find-python.sh",
        "stderr-cap.sh",
        "emit-context.sh",
        "resolve-vco-venv.sh",  # v0.2.46 post-adversarial F1
        "kg-sync-debounce.sh",  # 2026-06-18 write-amplification debounce
    ):
        src = REPO_ROOT / "templates" / "hooks" / "_lib" / lib
        if src.exists():
            shutil.copy(src, project / ".claude" / "hooks" / "_lib" / lib)

    # Stub python script that just records its argv to a file.
    record_file = tmp_path / "indexer_calls.log"
    stub_python = project / ".venv" / "bin" / "python"
    stub_python.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> {record_file}\n'
        "exit 0\n"
    )
    stub_python.chmod(
        stub_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    # Real diagram file.
    diagram = project / ".claude" / "diagrams" / "gui" / "test-flow.mmd"
    diagram.write_text("flowchart TD\n  A --> B")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(diagram)},
    }

    env = os.environ.copy()
    env["VCT_DISABLE_HOOKS"] = ""
    env["VCT_INSTALL_ROOT"] = str(project)
    env["PATH"] = (
        os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    )

    result = subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "post-file-edit.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        # 30s (was 10s): the hook resolves venv-python + spawns subprocesses;
        # under heavy CPU contention (e.g. the full pytest suite running
        # alongside cargo test in pre-ship-check) a 10s budget flakes with
        # TimeoutExpired. 30s is generous headroom and still fails fast on a
        # genuine hang.
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Throttle file was written.
    throttle_dir = project / ".claude" / "state"
    diagram_files = list(throttle_dir.glob("diagram_idx_*.ts"))
    assert len(diagram_files) == 1, (
        f"expected exactly one throttle file, got {diagram_files}"
    )

    # Indexer invocation: background-spawned, so the log may take a moment to
    # flush. Poll for up to 10s (was 1s = range(20)*0.05) — a 1s budget for a
    # BACKGROUND subprocess to be scheduled + flush flakes under CPU contention
    # (the pre-ship failure mode: this loop exhausted and pytest.fail'd while
    # the indexer was simply queued behind other load).
    import time as _t
    for _ in range(200):
        if record_file.exists():
            content = record_file.read_text()
            if "vco_lib.diagram_indexer" in content:
                break
        _t.sleep(0.05)
    else:
        pytest.fail(
            f"indexer never invoked; record_file exists={record_file.exists()}"
        )

    content = record_file.read_text()
    assert "vco_lib.diagram_indexer" in content
    assert "index" in content
    assert str(diagram) in content

    # A6 wire-up: after the indexer call, the hook must also dispatch
    # `snapshot create <file> --quiet`. Both calls share the same
    # `vco_lib.diagram_indexer` module + file path, so we grep for the
    # subcommand keyword `snapshot` to disambiguate from the `index`
    # call we just asserted. Background-spawned: poll up to 10s (same
    # contention-tolerant budget as the index assertion above).
    import time as _t
    for _ in range(200):
        if "snapshot" in record_file.read_text():
            break
        _t.sleep(0.05)
    snap_content = record_file.read_text()
    assert "snapshot" in snap_content, (
        f"snapshot subcommand never dispatched; record_file content:\n"
        f"{snap_content}"
    )
    assert "create" in snap_content
    # --quiet keeps the hook output clean
    assert "--quiet" in snap_content


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_diagram_throttle_60s_blocks_immediate_reindex(tmp_path: Path):
    """Second consecutive edit of the SAME diagram within 60s does NOT
    re-invoke the indexer."""
    bash = shutil.which("bash")
    assert bash is not None

    project = tmp_path / "fakeproj"
    (project / ".claude" / "hooks" / "_lib").mkdir(parents=True)
    (project / ".claude" / "diagrams" / "gui").mkdir(parents=True)
    (project / ".venv" / "bin").mkdir(parents=True)

    shutil.copy(HOOK_SH, project / ".claude" / "hooks" / "post-file-edit.sh")
    for lib in (
        "find-python.sh",
        "stderr-cap.sh",
        "emit-context.sh",
        "resolve-vco-venv.sh",  # v0.2.46 post-adversarial F1
        "kg-sync-debounce.sh",  # 2026-06-18 write-amplification debounce
    ):
        src = REPO_ROOT / "templates" / "hooks" / "_lib" / lib
        if src.exists():
            shutil.copy(src, project / ".claude" / "hooks" / "_lib" / lib)

    record_file = tmp_path / "indexer_calls.log"
    stub_python = project / ".venv" / "bin" / "python"
    stub_python.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> {record_file}\n'
        "exit 0\n"
    )
    stub_python.chmod(stub_python.stat().st_mode | stat.S_IXUSR)

    diagram = project / ".claude" / "diagrams" / "gui" / "throttled.mmd"
    diagram.write_text("flowchart TD\n  A --> B")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(diagram)},
    }

    env = os.environ.copy()
    env["VCT_DISABLE_HOOKS"] = ""
    env["VCT_INSTALL_ROOT"] = str(project)
    env["PATH"] = (
        os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    )

    def run_hook():
        return subprocess.run(
            [bash, str(project / ".claude" / "hooks" / "post-file-edit.sh")],
            input=json.dumps(payload),
            cwd=str(project),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,  # contention-tolerant (was 10s; flaked under parallel CI load)
        )

    # First call → indexer dispatched.
    r1 = run_hook()
    assert r1.returncode == 0

    # Wait for the background invocation to flush. The hook now
    # dispatches TWO calls per uncomma-throttled edit: one `index ...`
    # and one `snapshot create ...` (A6 wire-up 2026-05-25). We wait
    # for BOTH to land before asserting throttle behaviour, otherwise
    # the second-hook-call assertion below races the still-flushing
    # background spawns.
    import time as _t
    for _ in range(400):  # up to 20s (was 1.5s) — TWO background dispatches
        if record_file.exists():
            text = record_file.read_text()
            if text.count("vco_lib.diagram_indexer") >= 2:
                break
        _t.sleep(0.05)
    first_calls = (
        record_file.read_text() if record_file.exists() else ""
    )
    assert first_calls.count("vco_lib.diagram_indexer") == 2, (
        "expected exactly two dispatches per edit (index + snapshot); "
        f"got log:\n{first_calls}"
    )

    # Second call within throttle window → neither indexer nor
    # snapshot invoked again. The whole diagrams-branch is gated on
    # the 60s throttle, so the count stays at 2.
    r2 = run_hook()
    assert r2.returncode == 0

    # Give any potential second invocation time to leak through.
    _t.sleep(0.5)
    second_calls = (
        record_file.read_text() if record_file.exists() else ""
    )
    assert second_calls.count("vco_lib.diagram_indexer") == 2, (
        f"throttle failed; calls log:\n{second_calls}"
    )
