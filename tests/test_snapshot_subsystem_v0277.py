# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 9 — subagent-spawn snapshot subsystem hardening.

Drives the real templates/hooks/_lib/snapshot.sh functions through bash:

  9a: orphan GC — take_snapshot sweeps snapshots older than the GC window while
      leaving fresh ones (functionality-preserving cleanup of leaked state).
  9b: mtime+size quick-check — the snapshot stores {h,m,s}; diff_snapshot
      reuses the stored hash for an unchanged file and re-hashes a changed one,
      producing an IDENTICAL diff to the old always-hash behaviour.
  9c: build-dir exclusion — files under target/ node_modules/ etc. are pruned
      from BOTH snapshot and diff so a build artifact edit is never surfaced to
      the reconciler's consumers (KG-sync / code-graph / cred-scan).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "snapshot.sh"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _has_bash(), reason="bash required")


def _call(func: str, agent_id: str, project_root: Path, extra_env=None) -> subprocess.CompletedProcess:
    snap_dir = project_root / ".claude" / "state"
    cmd = f'. "{SNAPSHOT_SH}" && {func} "{agent_id}" "{project_root}" "{snap_dir}"'
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, timeout=30, env=env
    )


def _setup(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "knowledge").mkdir(parents=True)
    (proj / "src").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / "knowledge" / "a.md").write_text("alpha\n", encoding="utf-8")
    (proj / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return proj


def test_snapshot_stores_v2_hms(tmp_path: Path) -> None:
    proj = _setup(tmp_path)
    r = _call("take_snapshot", "ag1", proj)
    assert r.returncode == 0, r.stderr
    snap = proj / ".claude" / "state" / "subagent-snapshot-ag1.json"
    doc = json.loads(snap.read_text("utf-8"))
    assert doc["version"] == 2, doc
    entry = doc["files"]["src/main.py"]
    assert isinstance(entry, dict) and len(entry["h"]) == 64
    assert "m" in entry and "s" in entry


def test_orphan_gc_reaps_old_keeps_fresh(tmp_path: Path) -> None:
    """9a: taking a NEW snapshot sweeps snapshots older than the GC window; a
    just-written (fresh) snapshot is preserved."""
    proj = _setup(tmp_path)
    state = proj / ".claude" / "state"
    # An orphaned snapshot from an agent whose Stop hook never fired.
    orphan = state / "subagent-snapshot-DEAD.json"
    orphan.write_text('{"version":2,"files":{}}', encoding="utf-8")
    # Backdate it well past the default 3-day GC window.
    old = time.time() - 10 * 86400
    os.utime(orphan, (old, old))

    r = _call("take_snapshot", "ag-fresh", proj)
    assert r.returncode == 0, r.stderr

    assert not orphan.exists(), "orphan snapshot older than GC window must be reaped"
    fresh = state / "subagent-snapshot-ag-fresh.json"
    assert fresh.exists(), "the freshly-taken snapshot must be preserved"


def test_orphan_gc_window_is_configurable(tmp_path: Path) -> None:
    proj = _setup(tmp_path)
    state = proj / ".claude" / "state"
    orphan = state / "subagent-snapshot-DEAD.json"
    orphan.write_text('{"version":2,"files":{}}', encoding="utf-8")
    # 2 days old — within the default 3-day window, so a default run KEEPS it,
    # but VCT_SNAPSHOT_GC_DAYS=1 reaps it.
    two_days = time.time() - 2 * 86400
    os.utime(orphan, (two_days, two_days))
    r = _call("take_snapshot", "ag2", proj, extra_env={"VCT_SNAPSHOT_GC_DAYS": "1"})
    assert r.returncode == 0, r.stderr
    assert not orphan.exists(), "GC window override (1 day) must reap the 2-day orphan"


def test_build_dirs_pruned_from_snapshot(tmp_path: Path) -> None:
    """9c: files under a pruned dir (target/, node_modules/) are excluded."""
    proj = _setup(tmp_path)
    # A Rust build artifact under a target/ tree matched by the *.rs glob.
    tgt = proj / "src" / "target" / "debug"
    tgt.mkdir(parents=True)
    (tgt / "generated.rs").write_text("// build output\n", encoding="utf-8")
    nm = proj / "src" / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    r = _call("take_snapshot", "ag3", proj)
    assert r.returncode == 0, r.stderr
    doc = json.loads((proj / ".claude" / "state" / "subagent-snapshot-ag3.json").read_text("utf-8"))
    keys = set(doc["files"].keys())
    assert "src/main.py" in keys, "real source must still be tracked"
    assert not any("target/" in k for k in keys), f"target/ not pruned: {keys}"
    assert not any("node_modules/" in k for k in keys), f"node_modules/ not pruned: {keys}"


def test_diff_unchanged_file_not_flagged(tmp_path: Path) -> None:
    """9b: an unchanged file (mtime+size identical) is served from the stored
    hash and NOT reported as changed."""
    proj = _setup(tmp_path)
    r = _call("take_snapshot", "ag4", proj)
    assert r.returncode == 0, r.stderr
    # No filesystem change → diff must be empty.
    d = _call("diff_snapshot", "ag4", proj)
    assert d.returncode == 0, d.stderr
    assert d.stdout.strip() == "", f"unchanged tree must diff empty; got {d.stdout!r}"


def test_diff_modified_file_flagged(tmp_path: Path) -> None:
    """9b: a modified file (content + mtime change) IS reported — the quick-check
    must not mask a real edit."""
    proj = _setup(tmp_path)
    r = _call("take_snapshot", "ag5", proj)
    assert r.returncode == 0, r.stderr
    # Modify a file — sleep a hair so mtime changes on coarse-resolution FS.
    time.sleep(0.02)
    (proj / "knowledge" / "a.md").write_text("alpha CHANGED and longer\n", encoding="utf-8")
    d = _call("diff_snapshot", "ag5", proj)
    assert d.returncode == 0, d.stderr
    assert "knowledge/a.md" in d.stdout, f"modified file must be flagged; got {d.stdout!r}"


def test_diff_reads_legacy_v1_snapshot(tmp_path: Path) -> None:
    """Back-compat: a diff against a legacy v1 (bare-hash) snapshot still works
    (re-hashes everything since no quick-check fields exist)."""
    import hashlib

    proj = _setup(tmp_path)
    state = proj / ".claude" / "state"
    # Hand-build a v1 snapshot for the two seed files.
    def _h(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    v1 = {
        "version": 1,
        "agent_id": "agV1",
        "project_root": str(proj.resolve()),
        "created_at": "2026-01-01T00:00:00Z",
        "files": {
            "knowledge/a.md": _h(proj / "knowledge" / "a.md"),
            "src/main.py": _h(proj / "src" / "main.py"),
        },
    }
    (state / "subagent-snapshot-agV1.json").write_text(json.dumps(v1), encoding="utf-8")
    # Unchanged tree → empty diff even though the snapshot is v1.
    d = _call("diff_snapshot", "agV1", proj)
    assert d.returncode == 0, d.stderr
    assert d.stdout.strip() == "", f"v1 unchanged diff must be empty; got {d.stdout!r}"
    # Now change a file → flagged.
    time.sleep(0.02)
    (proj / "src" / "main.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    d2 = _call("diff_snapshot", "agV1", proj)
    assert "src/main.py" in d2.stdout, f"v1 change must be flagged; got {d2.stdout!r}"
