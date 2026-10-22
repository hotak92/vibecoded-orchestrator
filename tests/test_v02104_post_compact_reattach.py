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
doubt blocks. The module is asked only when ``post-compact`` left this
session's ``compacted_<key>.txt`` marker (no marker → blocked without
spawning Python), and it answers "no" past its own wall-clock deadline: the
hook's 3 s registration timeout must never be what ends the scan, because a
hook the harness kills on timeout does not block the Write — so the hook
hands the module only what is LEFT of that budget (``--deadline``), and skips
the spawn and blocks when too little is left. These tests drive the pure function and the real hook scripts
(bash, and pwsh where installed).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
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


def test_scan_past_the_deadline_is_no(tmp_path: Path):
    # A file that WOULD be "yes" is "no" once the budget is spent.
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    assert cr.was_reattached(str(t), str(f)) is True
    assert cr.was_reattached(str(t), str(f), deadline=-1.0) is False


def test_deadline_hit_mid_scan_is_no(tmp_path: Path, monkeypatch):
    # The boundary and the attachment sit at the START, behind ~3 MiB of
    # filler: the backward scan needs several chunks. A clock that jumps past
    # the deadline after the first chunk makes the answer "no".
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f),
                    _user("y" * 1000) * 3000)
    assert cr.was_reattached(str(t), str(f)) is True
    ticks = iter([0.0, 0.0] + [100.0] * 10_000)
    monkeypatch.setattr(cr.time, "monotonic", lambda: next(ticks))
    assert cr.was_reattached(str(t), str(f)) is False


def test_cli_answers_exact_words(tmp_path: Path):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    assert cr._main(["check", f"--transcript={t}", f"--path={f}"]) == 0
    assert cr._main(["check", f"--transcript={t}",
                     f"--path={tmp_path / 'other.py'}"]) == 1


def test_cli_deadline_is_capped_and_a_spent_one_is_no(tmp_path: Path,
                                                      monkeypatch):
    f = _file(tmp_path)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    seen: list[float] = []
    real = cr.was_reattached

    def spy(*a, **k):
        seen.append(k["deadline"])
        return real(*a, **k)

    monkeypatch.setattr(cr, "was_reattached", spy)
    base = ["check", f"--transcript={t}", f"--path={f}"]
    assert cr._main(base + ["--deadline=0.4"]) == 0
    assert cr._main(base + ["--deadline=9"]) == 0
    assert cr._main(base) == 0
    assert seen == [0.4, cr.DEADLINE_SECONDS, cr.DEADLINE_SECONDS]
    # Nothing left: "no" without scanning.
    assert cr._main(base + ["--deadline=0"]) == 1
    assert cr._main(base + ["--deadline=-1"]) == 1
    assert cr._main(base + ["--deadline=nan"]) == 1
    assert len(seen) == 3


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
           env: dict | None = None,
           **payload_extra: str) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "tool_name": "Write",
               "tool_input": {"file_path": str(target), "content": "x = 9\n"}}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    payload.update(payload_extra)
    return _hook(impl, "pre-tool-use", proj, payload, **(env or {}))


def _ledger(proj: Path) -> Path:
    return proj / ".claude" / "state" / f"reads_{SID}.txt"


def _marker(proj: Path) -> Path:
    return proj / ".claude" / "state" / f"compacted_{SID}.txt"


def _compact(impl: str, proj: Path) -> None:
    """Run the real post-compact hook for SID (the real order: compaction
    first, the Write after)."""
    res = _hook(impl, "post-compact", proj,
                {"session_id": SID, "trigger": "auto", "compact_summary": "s"})
    assert res.returncode == 0, res.stderr
    assert _marker(proj).is_file()


def _spy_venv(tmp_path: Path, runner: Path | None = None,
              prelude_sleep: float = 0.0) -> tuple[Path, Path]:
    """A fake venv whose python records every call, then runs the real
    interpreter — or, for the compact_reattach call only, *runner* with the
    same argv (the hook uses this python for its other helpers too). With
    *prelude_sleep*, the FIRST ``-c`` call (the bash hook's payload decode)
    sleeps that long first: a hook whose prelude ate its budget.
    Returns (venv, log)."""
    venv = tmp_path / "spyvenv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "python_calls.log"
    route = (f'[ "$1 $2" = "-m vco_lib.compact_reattach" ] && '
             f'exec "{sys.executable}" "{runner}" "$@"\n') if runner else ""
    flag = tmp_path / "prelude_slept"
    slow = (f'if [ "$1" = "-c" ] && [ ! -e "{flag}" ]; then : > "{flag}"; '
            f'sleep {prelude_sleep}; fi\n') if prelude_sleep else ""
    shim = venv / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\n'
                    f'{slow}{route}exec "{sys.executable}" "$@"\n',
                    encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return venv, log


def _reattach_calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines()
            if "compact_reattach" in ln]


@pytest.mark.parametrize("impl", _SHELLS)
def test_post_compact_wipes_the_ledger(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    led = _ledger(proj)
    led.write_text(f"{_file(proj)}\n", encoding="utf-8")
    res = _hook(impl, "post-compact", proj,
                {"session_id": SID, "trigger": "auto", "compact_summary": "s"})
    assert res.returncode == 0, res.stderr
    assert not led.exists()
    assert _marker(proj).is_file()


@pytest.mark.parametrize("impl", _SHELLS)
def test_write_to_a_reattached_file_is_allowed_and_recorded(impl: str,
                                                            tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _user("read it"), _boundary(), _attach("file", f))
    _compact(impl, proj)
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
    _compact(impl, proj)
    res = _write(impl, proj, f, _scenario(scenario, tmp_path, f))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_subagent_write_is_blocked_even_when_the_parent_had_it(impl: str,
                                                               tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    _compact(impl, proj)
    res = _write(impl, proj, f, t, agent_id="a123", agent_type="general")
    assert res.returncode == 2, (res.stdout, res.stderr)


@pytest.mark.parametrize("impl", _SHELLS)
def test_write_is_blocked_when_vco_lib_cannot_be_imported(impl: str,
                                                          tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    _compact(impl, proj)
    # The premise is pinned on the interpreter the hook hands the check to
    # (VCT_VENV is the first tier of both siblings' resolver): its
    # compact_reattach call runs under `python -S` with PYTHONPATH pointing
    # nowhere, so neither PYTHONPATH nor site-packages can supply vco_lib.
    # PYTHONPATH alone did not pin it: with vco_lib pip-installed (`pip
    # install -e .` — the CI job, any healthy install) the module imported
    # anyway, answered `reattached` for this transcript and the hook rightly
    # allowed. The other helpers keep the real interpreter.
    venv, log = _unimportable_vco_lib_venv(tmp_path)
    payload = {"session_id": SID, "tool_name": "Write", "transcript_path": str(t),
               "tool_input": {"file_path": str(f), "content": "x = 9\n"}}
    res = _hook(impl, "pre-tool-use", proj, payload,
                PYTHONPATH=str(tmp_path / "nowhere"), VCT_VENV=str(venv))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr
    # the check really ran (and failed to import) — not a skipped spawn
    assert len(_reattach_calls(log)) == 1, _reattach_calls(log)
    assert str(f) not in (_ledger(proj).read_text(encoding="utf-8").splitlines()
                          if _ledger(proj).exists() else [])


def _unimportable_vco_lib_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A fake venv whose python runs the compact_reattach call with site
    disabled (`-S`), so `import vco_lib` fails whatever is installed, and
    every other call with the real interpreter. Returns (venv, log)."""
    venv = tmp_path / "novcolib-venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "python_calls.log"
    shim = venv / "bin" / "python"
    shim.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\n'
        f'[ "$1 $2" = "-m vco_lib.compact_reattach" ] && '
        f'exec "{sys.executable}" -S "$@"\n'
        f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return venv, log


def test_hook_on_a_large_transcript_is_fast(tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _big_transcript(tmp_path, f)
    _compact("sh", proj)
    t0 = time.monotonic()
    res = _write("sh", proj, f, t)
    assert res.returncode == 0, res.stderr
    assert time.monotonic() - t0 < 20


# ─── M1: never outlive the hook's 3 s registration timeout ─────────────────


@pytest.mark.parametrize("impl", _SHELLS)
def test_no_compaction_marker_blocks_without_spawning_python(impl: str,
                                                            tmp_path: Path):
    # The transcript WOULD answer "reattached" — but this session never ran
    # post-compact, so the hook must block without asking the module at all.
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path)
    res = _write(impl, proj, f, t, env={"VCT_VENV": str(venv)})
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr
    assert not _marker(proj).exists()
    assert _reattach_calls(log) == []


@pytest.mark.parametrize("impl", _SHELLS)
def test_marker_and_reattached_spawns_the_check_and_allows(impl: str,
                                                          tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path)
    _compact(impl, proj)
    res = _write(impl, proj, f, t, env={"VCT_VENV": str(venv)})
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(_reattach_calls(log)) == 1


_SLOW_RUNNER = """\
import builtins, sys, time
from vco_lib import compact_reattach as cr

cr.CHUNK_BYTES = 4096


class _Slow:
    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def read(self, n=-1):
        time.sleep(0.05)
        return self._fh.read(n)

    def __getattr__(self, name):
        return getattr(self._fh, name)


cr.open = lambda *a, **k: _Slow(builtins.open(*a, **k))
args = sys.argv[1:]
assert args[:2] == ["-m", "vco_lib.compact_reattach"], args
t0 = time.monotonic()
rc = cr._main(args[2:])
with builtins.open(sys.argv[0] + ".elapsed", "w") as out:
    out.write(str(time.monotonic() - t0))
sys.exit(rc)
"""


@pytest.mark.parametrize("impl", _SHELLS)
def test_slow_scan_past_the_deadline_blocks(impl: str, tmp_path: Path):
    # The file IS re-attached (boundary + row at the start), but every read
    # is slow and the backward scan needs ~100 chunks (~5 s without a
    # deadline — past the hook's 3 s timeout). The module must give up at
    # its own deadline and answer "no", so the hook blocks.
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f),
                    _user("y" * 1000) * 400)
    runner = tmp_path / "slow_runner.py"
    runner.write_text(_SLOW_RUNNER, encoding="utf-8")
    venv, log = _spy_venv(tmp_path, runner)
    _compact(impl, proj)
    res = _write(impl, proj, f, t, env={"VCT_VENV": str(venv)})
    assert len(_reattach_calls(log)) == 1
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr
    elapsed = float((tmp_path / "slow_runner.py.elapsed").read_text())
    assert elapsed < cr.DEADLINE_SECONDS + 0.5, elapsed


# ─── L1 (R3): the deadline is what is LEFT of the hook's budget ───────────
#
# The hook measures its own elapsed time and hands the module
# budget − elapsed − 0.7 s (interpreter start + exit); under 0.2 s it skips
# the spawn and blocks. Never an allow on exhaustion.

_BUDGET_S = 3
_MARGIN_S = 0.7


def _deadline_arg(log: Path) -> float:
    calls = _reattach_calls(log)
    assert len(calls) == 1, calls
    vals = [tok.split("=", 1)[1] for tok in calls[0].split()
            if tok.startswith("--deadline=")]
    assert len(vals) == 1, calls[0]
    return float(vals[0])


def test_hook_budget_matches_the_registered_timeout():
    # The hooks' default budget is the `timeout` both settings templates
    # register for pre-tool-use.
    for os_name, hook in (("linux", "pre-tool-use.sh"),
                          ("windows", "pre-tool-use.ps1")):
        data = json.loads((REPO / "templates" /
                           f"settings.json.{os_name}.template").read_text(
                               encoding="utf-8"))
        timeouts = [h.get("timeout")
                    for group in data["hooks"]["PreToolUse"]
                    for h in group["hooks"] if hook in h["command"]]
        assert timeouts == [_BUDGET_S], (os_name, timeouts)


@pytest.mark.parametrize("impl", _SHELLS)
def test_hook_passes_the_budget_it_has_left(impl: str, tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path)
    _compact(impl, proj)
    res = _write(impl, proj, f, t, env={"VCT_VENV": str(venv)})
    assert res.returncode == 0, (res.stdout, res.stderr)
    d = _deadline_arg(log)
    assert 0.2 <= d <= _BUDGET_S - _MARGIN_S, d


def test_registered_timeout_override_shrinks_the_deadline(tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path)
    _compact("sh", proj)
    res = _write("sh", proj, f, t, env={"VCT_VENV": str(venv),
                                        "VCO_PRE_TOOL_USE_TIMEOUT_S": "2"})
    assert res.returncode == 0, (res.stdout, res.stderr)
    d = _deadline_arg(log)
    assert 0.2 <= d <= 2 - _MARGIN_S, d


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("value,low,high", [
    # Over-range: no error line on stderr, the 3 s default applies.
    ("99999999999999999999", 0.2, _BUDGET_S - _MARGIN_S),
    ("1000000", 0.2, _BUDGET_S - _MARGIN_S),
    # Leading zero: read as DECIMAL 8 (bash would read `08` as a bad octal).
    ("08", _BUDGET_S, 8 - _MARGIN_S),
])
def test_timeout_override_is_parsed_safely(impl: str, value: str, low: float,
                                           high: float, tmp_path: Path):
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path)
    _compact(impl, proj)
    res = _write(impl, proj, f, t, env={"VCT_VENV": str(venv),
                                        "VCO_PRE_TOOL_USE_TIMEOUT_S": value})
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stderr.strip() == "", res.stderr
    d = _deadline_arg(log)
    assert low <= d <= high, d


def test_slow_prelude_hands_a_shorter_deadline(tmp_path: Path):
    # The payload decode (the hook's first python call) takes 1.2 s: the
    # module may only get 3 − 1.2 − 0.7 = 1.1 s or less.
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    venv, log = _spy_venv(tmp_path, prelude_sleep=1.2)
    _compact("sh", proj)
    res = _write("sh", proj, f, t, env={"VCT_VENV": str(venv)})
    assert (tmp_path / "prelude_slept").exists()
    d = _deadline_arg(log)
    assert 0.2 <= d <= _BUDGET_S - 1.2 - _MARGIN_S, d
    assert res.returncode == 0, (res.stdout, res.stderr)


@pytest.mark.parametrize("impl", _SHELLS)
def test_exhausted_budget_skips_the_check_and_blocks(impl: str,
                                                     tmp_path: Path):
    # The transcript WOULD answer "reattached", but nothing is left of the
    # budget: bash — the payload decode took 2.2 s; pwsh (no python in its
    # prelude) — the registered timeout is 1 s and the powershell start
    # alone is over 0.1 s. No spawn, and the Write is blocked.
    proj = _project(tmp_path)
    f = _file(proj)
    t = _transcript(tmp_path, _boundary(), _attach("file", f))
    if impl == "sh":
        venv, log = _spy_venv(tmp_path, prelude_sleep=2.2)
        env = {"VCT_VENV": str(venv)}
    else:
        venv, log = _spy_venv(tmp_path)
        env = {"VCT_VENV": str(venv), "VCO_PRE_TOOL_USE_TIMEOUT_S": "1"}
    _compact(impl, proj)
    res = _write(impl, proj, f, t, env=env)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr
    assert _reattach_calls(log) == []


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
