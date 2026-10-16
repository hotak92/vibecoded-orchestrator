# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — post-compact re-seeds the Build Anchor reads list with the files
Claude Code re-attached, and the compact re-inject stops repeating them.

Documented harness rule (https://code.claude.com/docs/en/context-window,
"What survives compaction"): right after a compaction Claude Code re-reads
up to five of the files read or edited in the session, the most recently
modified first; a file over 5,000 tokens comes back as a path reference only.
Before v0.2.104 ``post-compact`` wiped the reads ledger, so Build Anchor
blocked a Write to a file whose content was back in context.

The estimate lives in ``vco_lib/compact_reattach.py``; these tests drive the
pure function AND the real hook scripts (bash, and pwsh where installed).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import compact_reattach as cr

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
HAVE_PWSH = shutil.which("pwsh") is not None
PWSH = shutil.which("pwsh") or "pwsh"
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    not HAVE_PWSH, reason="pwsh not installed"))]
SID = "sessPC1"


# ─── helpers ──────────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "context" / "plans").mkdir(parents=True)
    return proj


def _touch(path: Path, text: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _ledger(proj: Path, entries: list[str], key: str = SID) -> Path:
    led = proj / ".claude" / "state" / f"reads_{key}.txt"
    led.write_text("".join(f"{e}\n" for e in entries), encoding="utf-8")
    return led


def _env(proj: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for k in ("VCT_DISABLE_HOOKS", "VCO_HOOK_CONTEXT_CAP",
              "VCO_SNAPSHOT_MAX_AGE", "VCT_VENV", "VCT_INSTALL_ROOT"):
        env.pop(k, None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["PYTHONPATH"] = str(REPO)
    env.update(extra)
    return env


def _hook(impl: str, name: str, proj: Path, payload: dict,
          **extra: str) -> subprocess.CompletedProcess:
    argv = (["bash", str(HOOKS / f"{name}.sh")] if impl == "sh" else
            [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
             str(HOOKS / f"{name}.ps1")])
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120, cwd=str(proj),
                          env=_env(proj, **extra))


def _seven_files(proj: Path) -> list[Path]:
    """f0..f6 touched in that order; mtimes put f4,f2,f6,f3,f5 newest."""
    base = time.time() - 3600
    order_by_mtime = {4: 50, 2: 40, 6: 30, 3: 20, 5: 10, 1: 5, 0: 1}
    return [_touch(proj / "src" / f"f{i}.py", f"x = {i}\n",
                   base + order_by_mtime[i]) for i in range(7)]


# ─── the pure estimate ────────────────────────────────────────────────────


def test_picks_five_newest_by_mtime_among_touched_files(tmp_path: Path):
    proj = _project(tmp_path)
    files = _seven_files(proj)
    got = cr.select_reattached([str(f) for f in files], str(proj))
    assert got == [str(files[i]) for i in (4, 2, 6, 3, 5)]


def test_duplicates_collapse_and_relative_entries_resolve(tmp_path: Path):
    proj = _project(tmp_path)
    f = _touch(proj / "a.md", "hi\n", time.time())
    got = cr.select_reattached(["a.md", str(f), "a.md"], str(proj))
    assert got == ["a.md"]


def test_newest_mtime_but_touched_long_ago_is_left_out(tmp_path: Path):
    # Conservative hedge: "modified most recently" may mean the session's own
    # touch order; a file outside the five last-touched entries is left out
    # even with the newest mtime — and its slot is NOT refilled.
    proj = _project(tmp_path)
    files = _seven_files(proj)
    os.utime(files[0], (time.time(), time.time()))
    got = cr.select_reattached([str(f) for f in files], str(proj))
    assert str(files[0]) not in got
    assert got == [str(files[i]) for i in (4, 2, 6, 3)]


def test_over_token_cap_file_is_not_reattached(tmp_path: Path):
    proj = _project(tmp_path)
    now = time.time()
    big = _touch(proj / "big.py", "y = 1\n" * 4000, now)          # 24 000 bytes
    near = _touch(proj / "near.py", "z" * 15600 + "\n", now - 1)   # est. > 5000
    small = _touch(proj / "small.py", "ok\n", now - 2)
    got = cr.select_reattached([str(big), str(near), str(small)], str(proj))
    assert got == [str(small)]


def test_binary_and_non_utf8_count_as_over_cap(tmp_path: Path):
    proj = _project(tmp_path)
    b = proj / "img.bin"
    b.write_bytes(b"\x89PNG\x00\x01")
    l1 = proj / "latin.txt"
    l1.write_bytes(b"caf\xe9\n")
    assert not cr.within_token_cap(str(b))
    assert not cr.within_token_cap(str(l1))


def test_deleted_file_is_not_reattached(tmp_path: Path):
    proj = _project(tmp_path)
    now = time.time()
    keep = _touch(proj / "keep.py", "k\n", now - 5)
    gone = _touch(proj / "gone.py", "g\n", now)
    gone.unlink()
    assert cr.select_reattached([str(keep), str(gone)], str(proj)) == [str(keep)]


def test_reseed_failure_clears_the_ledger(tmp_path: Path, monkeypatch):
    proj = _project(tmp_path)
    led = _ledger(proj, [str(_touch(proj / "a.py", "a\n", time.time()))])

    def boom(*_a, **_k):
        raise OSError("disk says no")

    monkeypatch.setattr(cr, "select_reattached", boom)
    with pytest.raises(OSError):
        cr.reseed(str(proj), SID)
    assert not led.exists()


def test_reattached_ignores_a_stale_marker(tmp_path: Path):
    proj = _project(tmp_path)
    marker = Path(cr.reattached_path(str(proj), SID))
    marker.write_text(".claude/CONTEXT_STATE.md\n", encoding="utf-8")
    assert cr.reattached(str(proj), SID) == [".claude/CONTEXT_STATE.md"]
    old = time.time() - 4000
    os.utime(marker, (old, old))
    assert cr.reattached(str(proj), SID) == []


def test_snapshot_body_drops_the_git_status_block(tmp_path: Path):
    snap = tmp_path / "snap.md"
    snap.write_text("# Pre-Compaction Snapshot\nSaved: now\n\n"
                    "## Modified Files (git status, top 30)\n M secret.py\n\n"
                    "## Recently Modified Files (newest first, since the last "
                    "compaction)\n/p/a.py\n", encoding="utf-8")
    body = "\n".join(cr.snapshot_body(str(snap)))
    assert "secret.py" not in body and "git status" not in body
    assert "/p/a.py" in body and "Saved: now" in body


# ─── the real hooks ───────────────────────────────────────────────────────


@pytest.mark.parametrize("impl", _SHELLS)
def test_post_compact_reseeds_and_build_anchor_follows(impl: str,
                                                       tmp_path: Path):
    proj = _project(tmp_path)
    files = _seven_files(proj)
    led = _ledger(proj, [str(f) for f in files])
    res = _hook(impl, "post-compact", proj,
                {"session_id": SID, "trigger": "auto", "compact_summary": "s"})
    assert res.returncode == 0, res.stderr
    assert led.read_text(encoding="utf-8").splitlines() == [
        str(files[i]) for i in (4, 2, 6, 3, 5)]
    marker = proj / ".claude" / "state" / f"compact_reattached_{SID}.txt"
    assert marker.read_text(encoding="utf-8").splitlines() == [
        f"src/f{i}.py" for i in (4, 2, 6, 3, 5)]

    # Build Anchor: a Write to a re-attached file passes, a Write to a file
    # that was read before the compaction but NOT re-attached is blocked.
    ok = _hook(impl, "pre-tool-use", proj, {
        "session_id": SID, "tool_name": "Write",
        "tool_input": {"file_path": str(files[4]), "content": "x = 9\n"}})
    assert ok.returncode == 0, ok.stderr
    blocked = _hook(impl, "pre-tool-use", proj, {
        "session_id": SID, "tool_name": "Write",
        "tool_input": {"file_path": str(files[0]), "content": "x = 9\n"}})
    assert blocked.returncode == 2
    assert "Build Anchor" in blocked.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_post_compact_falls_back_to_wipe_without_vco_lib(impl: str,
                                                         tmp_path: Path):
    proj = _project(tmp_path)
    led = _ledger(proj, [str(_touch(proj / "a.py", "a\n", time.time()))])
    res = _hook(impl, "post-compact", proj,
                {"session_id": SID, "trigger": "manual"},
                PYTHONPATH=str(tmp_path / "nowhere"),
                PATH="/usr/bin:/bin" + os.pathsep + str(Path(PWSH).parent))
    assert res.returncode == 0, res.stderr
    assert not led.exists()
    assert "compact_reattach" in res.stderr


def _snapshot(proj: Path) -> None:
    (proj / ".claude" / "context" / "pre-compact-snapshot.md").write_text(
        "# Pre-Compaction Snapshot\nSaved: now\n\n"
        "## Modified Files (git status, top 30)\n M only_in_git_status.py\n\n"
        "## Recently Modified Files (newest first, since the last compaction)\n"
        "/p/recent_marker.py\n", encoding="utf-8")


@pytest.mark.parametrize("impl", _SHELLS)
def test_reinject_skips_a_reattached_context_state(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    ctx = _touch(proj / ".claude" / "CONTEXT_STATE.md",
                 "# State\nUNIQUE_CTX_BODY_MARKER\n", time.time())
    _snapshot(proj)
    _ledger(proj, [str(ctx)])
    assert _hook(impl, "post-compact", proj,
                 {"session_id": SID, "trigger": "auto"}).returncode == 0
    res = _hook(impl, "compact-context-reinject", proj,
                {"session_id": SID, "source": "compact"})
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "UNIQUE_CTX_BODY_MARKER" not in out
    assert "Already in context: Claude Code re-attached .claude/CONTEXT_STATE.md" in out
    # Harness-restored material is not repeated after a compaction.
    assert "## Recent Commits" not in out
    assert "only_in_git_status.py" not in out
    assert "/p/recent_marker.py" in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_reinject_keeps_context_state_not_reattached(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    _touch(proj / ".claude" / "CONTEXT_STATE.md",
           "# State\nUNIQUE_CTX_BODY_MARKER\n", time.time())
    other = _touch(proj / "other.py", "o\n", time.time())
    _ledger(proj, [str(other)])
    assert _hook(impl, "post-compact", proj,
                 {"session_id": SID, "trigger": "auto"}).returncode == 0
    res = _hook(impl, "compact-context-reinject", proj,
                {"session_id": SID, "source": "compact"})
    assert res.returncode == 0, res.stderr
    assert "UNIQUE_CTX_BODY_MARKER" in res.stdout
    assert "Already in context" not in res.stdout


@pytest.mark.parametrize("impl", _SHELLS)
def test_startup_reinject_is_unchanged(impl: str, tmp_path: Path):
    # A fresh marker must not leak into a startup injection.
    proj = _project(tmp_path)
    _touch(proj / ".claude" / "CONTEXT_STATE.md",
           "# State\nUNIQUE_CTX_BODY_MARKER\n", time.time())
    Path(cr.reattached_path(str(proj), SID)).write_text(
        ".claude/CONTEXT_STATE.md\n", encoding="utf-8")
    res = _hook(impl, "compact-context-reinject", proj,
                {"session_id": SID, "source": "startup"})
    assert res.returncode == 0, res.stderr
    assert "UNIQUE_CTX_BODY_MARKER" in res.stdout
    assert "## Recent Commits" in res.stdout
