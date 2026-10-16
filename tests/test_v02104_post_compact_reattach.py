# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — Build Anchor recognises the files Claude Code re-attached after
a compaction, read EXACTLY from the session transcript.

Right after a compaction Claude Code puts up to five recently read/edited
files back in the context and records each one in the transcript JSONL as a
``{"type":"attachment","attachment":{"type":"file",…}}`` row after the
``compact_boundary`` row. ``post-compact`` still wipes the Build Anchor reads
ledger (as v0.2.103 did); on a Write miss ``pre-tool-use`` asks
``vco_lib/compact_reattach.py`` whether the target was re-attached after the
LATEST boundary, and allows (and records) only on a positive answer — every
doubt blocks. These tests drive the pure function and the real hook scripts
(bash, and pwsh where installed).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tracemalloc
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


# ─── transcript fixtures (shapes observed on real transcripts) ───────────


def _row(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _boundary() -> str:
    return _row({"parentUuid": None, "type": "system",
                 "subtype": "compact_boundary", "content": "Conversation compacted",
                 "compactMetadata": {"trigger": "auto", "preTokens": 1}})


def _attach(kind: str, path: Path, *, start: int = 1,
            num: int = 1, total: int = 1) -> str:
    att: dict = {"type": kind, "filename": str(path), "displayPath": path.name}
    if kind == "file":
        att["content"] = {"type": "text", "file": {
            "filePath": str(path), "content": "x = 1\n",
            "startLine": start, "numLines": num, "totalLines": total}}
    elif kind == "edited_text_file":
        att["snippet"] = "x"
    return _row({"type": "attachment", "attachment": att})


def _user(text: str) -> str:
    return _row({"type": "user", "message": {"role": "user", "content": text}})


def _transcript(tmp_path: Path, *rows: str) -> Path:
    t = tmp_path / "transcript.jsonl"
    t.write_text("".join(rows), encoding="utf-8")
    return t


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "context" / "plans").mkdir(parents=True)
    return proj


def _file(proj: Path, name: str = "src/a.py") -> Path:
    f = proj / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x = 1\n", encoding="utf-8")
    return f


# ─── the pure check ───────────────────────────────────────────────────────


def test_file_reattached_after_latest_boundary_is_yes(tmp_path: Path):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _user("hi"), _boundary(), _user("summary"),
                    _attach("file", f), _user("go on"))
    assert cr.was_reattached(str(t), str(f)) is True


def test_file_reattached_only_before_an_earlier_boundary_is_no(tmp_path: Path):
    f = _file(tmp_path)
    other = _file(tmp_path, "src/b.py")
    t = _transcript(tmp_path, _boundary(), _attach("file", f), _user("work"),
                    _boundary(), _attach("file", other))
    assert cr.was_reattached(str(t), str(f)) is False
    assert cr.was_reattached(str(t), str(other)) is True


@pytest.mark.parametrize("kind", ["compact_file_reference", "edited_text_file"])
def test_non_file_attachment_is_no(tmp_path: Path, kind: str):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach(kind, f))
    assert cr.was_reattached(str(t), str(f)) is False


@pytest.mark.parametrize("start,num,total", [(10, 5, 50), (1, 5, 50)])
def test_partial_file_content_is_no(tmp_path: Path, start, num, total):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(),
                    _attach("file", f, start=start, num=num, total=total))
    assert cr.was_reattached(str(t), str(f)) is False


def test_no_boundary_is_no(tmp_path: Path):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _user("hi"), _attach("file", f))
    assert cr.was_reattached(str(t), str(f)) is False


def test_missing_or_unreadable_transcript_is_no(tmp_path: Path):
    f = _file(tmp_path)
    assert cr.was_reattached(str(tmp_path / "absent.jsonl"), str(f)) is False
    assert cr.was_reattached(str(tmp_path), str(f)) is False  # a directory
    assert cr.was_reattached("", str(f)) is False
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    t.chmod(0)
    try:
        if not os.access(t, os.R_OK):  # root can still read it
            assert cr.was_reattached(str(t), str(f)) is False
    finally:
        t.chmod(0o600)


def test_subagent_call_is_no(tmp_path: Path):
    # The payload's transcript is the PARENT's; its re-attachments are not in
    # the subagent's context.
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    assert cr.was_reattached(str(t), str(f), agent_id="a123") is False


def test_escaped_boundary_text_inside_a_message_is_not_a_boundary(tmp_path: Path):
    f = _file(tmp_path)
    fake = '{"type":"system","subtype":"compact_boundary"}'
    t = _transcript(tmp_path, _boundary(), _attach("file", f), _user(fake))
    assert cr.was_reattached(str(t), str(f)) is True


def test_unterminated_row_is_ignored(tmp_path: Path):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f).rstrip("\n"))
    assert cr.was_reattached(str(t), str(f)) is False


def test_paths_are_normalised(tmp_path: Path):
    f = _file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(f.parent, target_is_directory=True)
    t = _transcript(tmp_path, _boundary(), _attach("file", link / "a.py"))
    assert cr.was_reattached(str(t), str(f.parent / ".." / "src" / "a.py")) is True


def test_boundary_outside_the_scan_window_is_no(tmp_path: Path):
    f = _file(tmp_path)
    filler = _user("z" * 1000) * 300
    t = _transcript(tmp_path, _boundary(), _attach("file", f), filler)
    assert cr.was_reattached(str(t), str(f)) is True
    assert cr.was_reattached(str(t), str(f), window=100_000) is False


def _big_transcript(tmp_path: Path, f: Path) -> Path:
    """~50 MB of ordinary rows plus one 6 MB single line BEFORE the boundary,
    then the boundary and the re-attached file."""
    t = tmp_path / "big.jsonl"
    row = _user("filler " + "y" * 1000)
    with open(t, "w", encoding="utf-8") as fh:
        fh.write(_boundary())
        fh.write(_attach("file", tmp_path / "old.py"))
        fh.write(row * 25_000)
        fh.write(_user("w" * (6 * 1024 * 1024)))
        fh.write(row * 25_000)
        fh.write(_boundary())
        fh.write(_user("summary"))
        fh.write(_attach("file", f))
        fh.write(row * 50)
    assert t.stat().st_size > 50 * 1024 * 1024
    return t


def test_large_transcript_answers_fast_with_bounded_memory(tmp_path: Path):
    f = _file(tmp_path)
    t = _big_transcript(tmp_path, f)
    tracemalloc.start()
    t0 = time.monotonic()
    yes = cr.was_reattached(str(t), str(f))
    no = cr.was_reattached(str(t), str(tmp_path / "old.py"))
    elapsed = time.monotonic() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert (yes, no) == (True, False)
    assert elapsed < 2.0, elapsed
    assert peak < 16 * 1024 * 1024, peak


def test_large_transcript_without_boundary_is_bounded(tmp_path: Path):
    # Worst case: no compaction at all — the scan stops at the window.
    f = _file(tmp_path)
    t = tmp_path / "nob.jsonl"
    with open(t, "w", encoding="utf-8") as fh:
        fh.write(_user("filler " + "y" * 1000) * 52_000)
    tracemalloc.start()
    t0 = time.monotonic()
    assert cr.was_reattached(str(t), str(f)) is False
    elapsed = time.monotonic() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert elapsed < 2.0, elapsed
    assert peak < 16 * 1024 * 1024, peak


def test_cli_answers_exact_words(tmp_path: Path):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    assert cr._main(["check", f"--transcript={t}", f"--path={f}"]) == 0
    assert cr._main(["check", f"--transcript={t}",
                     f"--path={tmp_path / 'other.py'}"]) == 1


def test_snapshot_body_drops_the_git_status_block(tmp_path: Path):
    snap = tmp_path / "snap.md"
    snap.write_text("# Pre-Compaction Snapshot\nSaved: now\n\n"
                    "## Modified Files (git status, top 30)\n M a.py\n\n"
                    "## Recently Modified Files (newest first)\n/p/b.py\n",
                    encoding="utf-8")
    body = cr.snapshot_body(str(snap))
    assert " M a.py" not in body
    assert "/p/b.py" in body
    assert body[0] == "# Pre-Compaction Snapshot"


# ─── the real hooks ───────────────────────────────────────────────────────


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


def _write(impl: str, proj: Path, target: Path, transcript: Path | None,
           **payload_extra: str) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "tool_name": "Write",
               "tool_input": {"file_path": str(target), "content": "x = 9\n"}}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    payload.update(payload_extra)
    return _hook(impl, "pre-tool-use", proj, payload)


def _ledger(proj: Path) -> Path:
    return proj / ".claude" / "state" / f"reads_{SID}.txt"


@pytest.mark.parametrize("impl", _SHELLS)
def test_post_compact_wipes_the_ledger(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    led = _ledger(proj)
    led.write_text(f"{_file(proj)}\n", encoding="utf-8")
    res = _hook(impl, "post-compact", proj,
                {"session_id": SID, "trigger": "auto", "compact_summary": "s"})
    assert res.returncode == 0, res.stderr
    assert not led.exists()


@pytest.mark.parametrize("impl", _SHELLS)
def test_write_to_a_reattached_file_is_allowed_and_recorded(impl: str,
                                                            tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _user("read it"), _boundary(), _attach("file", f))
    ok = _write(impl, proj, f, t)
    assert ok.returncode == 0, ok.stderr
    assert str(f) in _ledger(proj).read_text(encoding="utf-8").splitlines()
    # Recorded: a later Write passes without the transcript.
    again = _write(impl, proj, f, None)
    assert again.returncode == 0, again.stderr


def _scenario(name: str, tmp_path: Path, f: Path) -> Path | None:
    if name == "earlier_boundary_only":
        return _transcript(tmp_path, _boundary(), _attach("file", f),
                           _boundary(), _user("nothing re-attached"))
    if name == "non_file_attachment":
        return _transcript(tmp_path, _boundary(),
                           _attach("compact_file_reference", f))
    if name == "no_boundary":
        return _transcript(tmp_path, _attach("file", f))
    if name == "missing_transcript":
        return tmp_path / "absent.jsonl"
    return None  # no transcript_path in the payload


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("scenario", ["earlier_boundary_only",
                                      "non_file_attachment", "no_boundary",
                                      "missing_transcript", "no_transcript"])
def test_write_is_blocked_unless_reattached(impl: str, scenario: str,
                                            tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    res = _write(impl, proj, f, _scenario(scenario, tmp_path, f))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_subagent_write_is_blocked_even_when_the_parent_had_it(impl: str,
                                                               tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    res = _write(impl, proj, f, t, agent_id="a123", agent_type="general")
    assert res.returncode == 2, (res.stdout, res.stderr)


@pytest.mark.parametrize("impl", _SHELLS)
def test_write_is_blocked_when_vco_lib_cannot_be_imported(impl: str,
                                                          tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    payload = {"session_id": SID, "tool_name": "Write", "transcript_path": str(t),
               "tool_input": {"file_path": str(f), "content": "x = 9\n"}}
    res = _hook(impl, "pre-tool-use", proj, payload,
                PYTHONPATH=str(tmp_path / "nowhere"))
    assert res.returncode == 2, (res.stdout, res.stderr)


def test_hook_on_a_large_transcript_is_fast(tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _big_transcript(tmp_path, f)
    t0 = time.monotonic()
    res = _write("sh", proj, f, t)
    assert res.returncode == 0, res.stderr
    assert time.monotonic() - t0 < 20


# ─── the compact re-inject ───────────────────────────────────────────────


def _snapshot(proj: Path) -> None:
    (proj / ".claude" / "context" / "pre-compact-snapshot.md").write_text(
        "# Pre-Compaction Snapshot\nSaved: now\n\n"
        "## Modified Files (git status, top 30)\n M only_in_git_status.py\n\n"
        "## Recently Modified Files (newest first, since the last compaction)\n"
        "/p/recent_marker.py\n", encoding="utf-8")


@pytest.mark.parametrize("impl", _SHELLS)
def test_compact_reinject_keeps_state_drops_harness_restored_git(impl: str,
                                                                 tmp_path: Path):
    proj = _project(tmp_path)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(
        "# State\nUNIQUE_CTX_BODY_MARKER\n", encoding="utf-8")
    _snapshot(proj)
    res = _hook(impl, "compact-context-reinject", proj,
                {"session_id": SID, "source": "compact"})
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "UNIQUE_CTX_BODY_MARKER" in out
    assert "## Recent Commits" not in out
    assert "only_in_git_status.py" not in out
    assert "/p/recent_marker.py" in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_startup_reinject_is_unchanged(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(
        "# State\nUNIQUE_CTX_BODY_MARKER\n", encoding="utf-8")
    res = _hook(impl, "compact-context-reinject", proj,
                {"session_id": SID, "source": "startup"})
    assert res.returncode == 0, res.stderr
    assert "UNIQUE_CTX_BODY_MARKER" in res.stdout
    assert "## Recent Commits" in res.stdout
