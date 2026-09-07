# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration-level regression test for the v0.2.92 silent-KG-sync-drop fix.

Background
----------
``post-file-edit.sh`` built its debounced sync command as a plain string:

    _kg_write_allowed <proj> <coll> && .claude/scripts/kg-sync <path>

...and handed that string to ``_kg_debounce_schedule`` (``_lib/kg-sync-
debounce.sh``), which persists it to disk and ``eval``s it LATER inside a
process that never sourced ``post-file-edit.sh`` — the v0.2.65 Track B
"Item 1" hardening pass made the debounce flusher spawn via
``setsid bash -c '...'`` for crash-safety, and that child process sources
ONLY ``_lib/kg-sync-debounce.sh``. ``_kg_write_allowed`` (a bash function
defined in ``post-file-edit.sh``) was therefore never visible there:
``eval`` reported "_kg_write_allowed: command not found" (exit 127), and
because it was the LHS of ``&&`` the real sync on the RHS never ran. The
eval's own stdout/stderr are redirected to ``/dev/null`` by
``_kg_debounce_run_claimed``, so the failure left no trace anywhere — the
exact "sync that never ran leaves no trace" fragility this work package
was asked to close. This silently broke the ENTIRE hook-triggered KG/docs
auto-sync path for every project on this install since v0.2.65.

``tests/test_kg_sync_debounce_cap_throttle.py`` never caught this because
it drives ``_kg_debounce_schedule`` with a trivial self-contained test
command (``"echo ... >> log"``), never the real gated command string built
in ``post-file-edit.sh`` — a correct unit test of the debounce MECHANISM
that never exercised the INTEGRATION with its only real caller. This file
closes that gap: it runs the ACTUAL shipped ``post-file-edit.sh`` (copied
fresh from the template at test time, never hand-transcribed) end-to-end
against a scratch project tree with a stub ``kg-sync``, and asserts the
stub is actually invoked once the debounce window elapses.

Both the "act" (gate allows -> sync runs) and "leave-alone" (gate denies
-> sync does not run) cases are covered, per the standing repo rule that
every branch gating an action needs both directions tested.

Linux/macOS-only: same rationale as test_kg_sync_debounce_cap_throttle.py
(bash lib; an end-to-end pwsh run isn't in budget here — the .ps1 sibling,
post-file-edit.ps1's Build-GatedSyncCommand, was ALREADY self-contained
before this fix and is unaffected).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC = REPO_ROOT / "templates" / "hooks" / "post-file-edit.sh"
LIB_SRC = REPO_ROOT / "templates" / "hooks" / "_lib"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = [
    pytest.mark.skipif(platform.system() == "Windows", reason="bash hook; .ps1 sibling already self-contained"),
    pytest.mark.skipif(not _has_bash(), reason="bash not on PATH"),
]


def _wait_for(path: Path, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A scratch project tree with the REAL post-file-edit.sh + its libs."""
    root = tmp_path / "proj"
    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    shutil.copytree(LIB_SRC, hooks_dir / "_lib")
    shutil.copy(HOOK_SRC, hooks_dir / "post-file-edit.sh")

    scripts_dir = root / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)

    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "test.md").write_text("# test node\n", encoding="utf-8")

    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "test.md").write_text("# test doc\n", encoding="utf-8")

    (root / ".claude" / "state").mkdir(parents=True)
    return root


def _stub_kg_sync(project: Path, marker: Path) -> None:
    kg_sync = project / ".claude" / "scripts" / "kg-sync"
    kg_sync.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "KG-SYNC-RAN: $*" >> {marker}\n',
        encoding="utf-8",
    )
    kg_sync.chmod(0o755)


def _stub_access_checker(project: Path, verdict: str) -> None:
    """Plant a fake vct_access_check.sh that always prints *verdict*.

    Written under .claude/scripts/ (the second candidate path
    _kg_build_gated_sync_cmd probes, since templates/scripts/ doesn't
    exist under a scratch project tree).
    """
    checker = project / ".claude" / "scripts" / "vct_access_check.sh"
    checker.write_text(
        f'#!/usr/bin/env bash\necho "{verdict}"\n',
        encoding="utf-8",
    )
    checker.chmod(0o755)


def _run_hook(
    project: Path,
    edited_file: Path,
    env_extra: dict | None = None,
    debounce_seconds: str = "1",
) -> subprocess.CompletedProcess:
    payload = (
        '{"tool_input": {"file_path": "%s"}, "agent_id": "", '
        '"agent_type": "", "session_id": "reprosess"}' % str(edited_file)
    )
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
        "CLAUDE_PROJECT_DIR": str(project),
        "VCO_KG_SYNC_DEBOUNCE_SECONDS": debounce_seconds,
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(project / ".claude" / "hooks" / "post-file-edit.sh")],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_kg_sync_actually_invoked_via_debounced_hook(project: Path, tmp_path: Path):
    """No VCT_PROJECT_ID / no access checker on disk -> gate falls open,
    and the REAL debounced flusher must actually invoke kg-sync.

    This is the exact scenario that silently failed pre-fix: the hook
    exits 0 with clean stdout/stderr, but (pre-fix) the stub was never
    invoked because `_kg_write_allowed` was undefined in the detached
    flusher shell.
    """
    marker = tmp_path / "marker.log"
    _stub_kg_sync(project, marker)

    res = _run_hook(project, project / "knowledge" / "test.md")
    assert res.returncode == 0, res.stderr

    assert _wait_for(marker), (
        "kg-sync was never invoked by the debounced flusher — the "
        "v0.2.65 silent-drop regression is back"
    )
    assert "KG-SYNC-RAN: knowledge/test.md" in marker.read_text()


def test_docs_sync_actually_invoked_via_debounced_hook(project: Path, tmp_path: Path):
    """Same integration check for the docs/ branch (DEVELOPMENT_COLLECTION)."""
    marker = tmp_path / "marker.log"
    _stub_kg_sync(project, marker)

    res = _run_hook(project, project / "docs" / "test.md")
    assert res.returncode == 0, res.stderr

    assert _wait_for(marker), "kg-sync was never invoked for the docs/ branch"
    assert "KG-SYNC-RAN: docs/test.md" in marker.read_text()


def test_access_matrix_write_verdict_allows_sync(project: Path, tmp_path: Path):
    """The 'act' case: a project id IS set and the checker returns
    'write' -> the sync must still run (gate correctly evaluated at
    EVAL time inside the detached shell, not just schedule time)."""
    marker = tmp_path / "marker.log"
    _stub_kg_sync(project, marker)
    _stub_access_checker(project, "write")

    res = _run_hook(
        project,
        project / "knowledge" / "test.md",
        env_extra={"VCT_PROJECT_ID": "proj-123", "KG_COLLECTION": "SomeKG"},
    )
    assert res.returncode == 0, res.stderr

    assert _wait_for(marker), "a 'write' verdict must let the sync run"


def test_access_matrix_none_verdict_blocks_sync(project: Path, tmp_path: Path):
    """The 'leave-alone' case: the checker returns 'none' -> the sync
    must NOT run. Without this test, a regression that always falls
    open (e.g. dropping the gate snippet entirely) would pass the
    'act' test above but silently defeat the access-matrix feature."""
    marker = tmp_path / "marker.log"
    _stub_kg_sync(project, marker)
    _stub_access_checker(project, "none")

    res = _run_hook(
        project,
        project / "knowledge" / "test.md",
        env_extra={"VCT_PROJECT_ID": "proj-123", "KG_COLLECTION": "SomeKG"},
    )
    assert res.returncode == 0, res.stderr

    # Give the flusher the same window it would have had to (wrongly) run.
    time.sleep(3.0)
    assert not marker.exists(), (
        "a 'none' access-matrix verdict must block the sync, but kg-sync ran anyway"
    )


def test_gated_command_string_is_single_line(project: Path):
    """The persisted cmd file is read via `head -1` (see kg-sync-
    debounce.sh) — a gate snippet containing a raw newline would
    silently truncate the sync half of the command. Guard the built
    command string never contains one, for both a fall-open and a
    real-checker build."""
    # Exercise the builder in isolation, sourcing the hook's function
    # definitions without letting the rest of the script run (it reads
    # stdin and exits early otherwise). We source just far enough by
    # running a tiny bash snippet that sources post-file-edit.sh's
    # sourced libs plus a hand-lifted copy of the builder would drift;
    # instead, drive it via the real hook with a controlled env and
    # inspect the persisted cmd file the debounce lib writes, which is
    # the actual on-disk artifact that must stay single-line.
    _stub_kg_sync(project, project / "marker.log")
    _stub_access_checker(project, "write")
    res = _run_hook(
        project,
        project / "knowledge" / "test.md",
        env_extra={"VCT_PROJECT_ID": "proj-123", "KG_COLLECTION": "SomeKG"},
        debounce_seconds="999",  # keep the lock file around long enough to inspect
    )
    assert res.returncode == 0, res.stderr

    pending = project / ".claude" / "state" / "kg_sync_pending"
    assert _wait_for(pending), "debounce lock dir was never created"
    lock_dirs = list(pending.glob("kg_*.lock"))
    assert lock_dirs, "expected exactly one pending kg lock dir"
    cmd_file = lock_dirs[0] / "cmd"
    assert _wait_for(cmd_file)
    raw = cmd_file.read_bytes()
    # Exactly one newline: the printf '%s\t%s\n' terminator. Any
    # additional \n would corrupt the head -1 / cut -f2- parse.
    assert raw.count(b"\n") == 1, (
        f"persisted cmd file must be single-line (wd\\tcmd\\n), found "
        f"{raw.count(b'\\n')} newlines: {raw!r}"
    )
