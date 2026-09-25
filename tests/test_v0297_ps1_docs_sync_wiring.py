# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""post-file-edit.ps1 → kg-sync.ps1 wiring for ``docs/`` edits (v0.2.97 B1).

The Windows docs auto-sync branch in
``templates/hooks/_lib/route-touched-path.ps1`` used to invoke
``.claude/scripts/upload_docs.py`` — a script retired 2026-04-30 that
nothing ships.  The ``Test-Path`` guard meant the branch was a permanent
silent no-op: on Windows, ``docs/*.md`` edits never reached the
development collection while the shipped docs promised the auto-sync.
The bash sibling routes the same edits through ``kg-sync``.

These tests are DRIVEN, not scanned: a source scan asserting the string
``kg-sync.ps1`` appears in the hook is satisfiable by the very comment
that was wrong.  The test here runs the real hook through its production
entry point (JSON on stdin, ``CLAUDE_PROJECT_DIR`` at a staged project),
with ``VCO_KG_SYNC_DEBOUNCE_SECONDS=0`` so the deferred sync fires
immediately, and asserts the OBSERVABLE consequence: the staged
``kg-sync.ps1`` wrapper ran, receiving the edited file's project-relative
path as its argument.

Interpreter-bound: skipped when no PowerShell is on PATH, refusing to
skip under CI (the posture ``tests/test_v0292_post_file_edit_dup_wiring_ps1.py``
established).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_PS1 = REPO / "templates" / "hooks" / "post-file-edit.ps1"

#: Poll budget for the detached flusher to land the marker.
MARKER_TIMEOUT_S = 45.0


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _require_pwsh() -> str:
    exe = _powershell()
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this wiring gate cannot run and "
            "must not be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _stage_project(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal project whose docs/page.md is the edited file.

    Returns (project, marker).  The staged ``kg-sync.ps1`` stand-in records
    its arguments into ``marker`` so only the wrapper the hook actually
    chose can pass the test.
    """
    project = tmp_path / "proj"
    (project / ".claude" / "scripts").mkdir(parents=True)
    (project / ".claude" / "state").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "docs" / "page.md").write_text("# page\n", encoding="utf-8")
    marker = tmp_path / "docs-sync-ran.txt"
    wrapper = project / ".claude" / "scripts" / "kg-sync.ps1"
    wrapper.write_text(
        f"Set-Content -LiteralPath '{marker}' -Value ($args -join ' ') -Encoding utf8\n",
        encoding="utf-8",
    )
    return project, marker


def _run_hook(exe: str, project: Path, edited: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    # Fire the debounced sync immediately — the wiring, not the window, is
    # under test.
    env["VCO_KG_SYNC_DEBOUNCE_SECONDS"] = "0"
    # No project_id / collection context → Build-GatedSyncCommand falls
    # open and the staged wrapper is reached without a resolver.
    env.pop("VCT_PROJECT_ID", None)
    env.pop("DEVELOPMENT_COLLECTION", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_VENV", None)
    payload = {"tool_input": {"file_path": str(edited)}}
    return subprocess.run(
        [exe, "-NoProfile", "-File", str(HOOK_PS1)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _await_marker(marker: Path) -> str | None:
    deadline = time.monotonic() + MARKER_TIMEOUT_S
    while time.monotonic() < deadline:
        if marker.exists():
            return marker.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.25)
    return None


def test_docs_edit_routes_through_kg_sync_ps1(tmp_path):
    """A docs/*.md edit must invoke the project's kg-sync wrapper.

    Pre-fix (upload_docs.py branch) this never fired: the guard tested for
    a script nothing ships, so the marker never appeared.
    """
    exe = _require_pwsh()
    project, marker = _stage_project(tmp_path)
    edited = project / "docs" / "page.md"

    proc = _run_hook(exe, project, edited)

    body = _await_marker(marker)
    assert body is not None, (
        "docs/ edit did not route through .claude/scripts/kg-sync.ps1 within "
        f"{MARKER_TIMEOUT_S:.0f}s. hook stderr: {proc.stderr[-2000:]!r}"
    )
    # The wrapper receives the file's PROJECT-RELATIVE path (same contract
    # as the bash sibling's kg-sync call), not an absolute or absent arg.
    rel = str(Path("docs") / "page.md")
    assert rel in body.replace("\\", "/"), (
        f"kg-sync.ps1 was invoked with {body!r}, expected the relative path {rel!r}"
    )


def test_docs_branch_no_longer_targets_upload_docs_py():
    """Prose hygiene, deliberately narrow: the retired target is gone.

    The wiring guard is the driven test above; this only asserts the
    ABSENCE of the dead reference so the silent-no-op class cannot be
    reintroduced under a different guard.
    """
    body = (REPO / "templates" / "hooks" / "_lib" / "route-touched-path.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert '.claude/scripts/upload_docs.py"' not in body, (
        "route-touched-path.ps1 still targets .claude/scripts/upload_docs.py — "
        "a script nothing ships; the docs branch must route through kg-sync "
        "like the .sh sibling"
    )
