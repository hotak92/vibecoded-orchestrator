# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-ALERTS2 (owner ruling on review R7-2) — tagged undelivered rows.

Duplicate rows in ``.claude/logs/agent-alerts-undelivered.jsonl`` after a
partial failure are acceptable; an UNTAGGED duplicate or a silently dropped
broken row is not. So:

* a row moved on a RETRY after an earlier move attempt (the log was written
  but the claim survived, or the log write failed part-way) carries
  ``possibly_duplicate: true``;
* a line that was unparseable, truncated, not a JSON object or not UTF-8 is
  carried as ``possibly_broken: true`` with its raw text in a JSON string
  (bounded), never dropped;
* a clean first move is NOT tagged (leave-alone).

These drive the real ``sweep_orphan_alerts`` / ``deliver_for_payload`` /
``readopt_stale_deliveries``; failures are injected at the OS boundary
(``Path.unlink`` / ``open`` on the log), not by stubbing the module's logic.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.test_v02102_w6c2_subagent_hook_census import _dead_pid


def _aa():
    from vco_lib import agent_alerts

    return agent_alerts


def _backdate(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _log_rows(root: Path) -> list:
    log = root / ".claude" / "logs" / "agent-alerts-undelivered.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


MAIN_CHAT = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s1",
             "tool_input": {"command": "ls"}}


# ── leave-alone: a clean move is not tagged ────────────────────────────────


def test_clean_orphan_move_is_not_tagged(tmp_path: Path) -> None:
    aa = _aa()
    p = aa.append_alert(str(tmp_path), "sOld", "lane1", "coder", "credential", "clean row")
    assert p
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(str(tmp_path)) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["clean row"]
    assert "possibly_duplicate" not in rows[0] and "possibly_broken" not in rows[0]
    # The internal attempt marker never leaks into the log.
    assert not any(k.startswith("undelivered_") and k != "undelivered_reason" for k in rows[0])
    assert not list((tmp_path / ".claude" / "state").glob("agent_alerts_*"))
    # The journal's temp file never survives a successful move.
    assert not list((tmp_path / ".claude" / "state").glob(".agent_alerts_*"))


def test_clean_claim_move_is_not_tagged(tmp_path: Path) -> None:
    aa = _aa()
    p = aa.append_alert(str(tmp_path), "sOld", "lane1", "coder", "credential", "old claim")
    assert p
    claim = p.with_name(f"{p.name}.delivering.{os.getpid()}")  # a LIVE pid: never re-adopted
    p.replace(claim)
    _backdate(claim, 8 * 86400)
    assert aa.sweep_orphan_alerts(str(tmp_path)) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["old claim"]
    assert rows[0]["undelivered_reason"] == "orphaned_delivery_claim"
    assert "possibly_duplicate" not in rows[0]
    assert not claim.exists()


# ── act: a retry after a partial move is tagged possibly_duplicate ─────────


def test_claim_retry_after_unlink_failure_is_tagged(tmp_path: Path, monkeypatch) -> None:
    """R7-2 exactly: the log is written, the claim's unlink fails, the next
    sweep logs the same rows again — the second copy must say so."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "dup me")
    assert p
    claim = p.with_name(f"{p.name}.delivering.{os.getpid()}")
    p.replace(claim)
    _backdate(claim, 8 * 86400)

    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.name == claim.name:
            raise PermissionError("injected: unlink refused")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert aa.sweep_orphan_alerts(root) == 0  # log written, claim stays
    assert claim.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    _backdate(claim, 8 * 86400)  # the journal rewrite refreshed its mtime
    assert aa.sweep_orphan_alerts(root) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["dup me", "dup me"]
    assert "possibly_duplicate" not in rows[0], "the first copy is a clean move"
    assert rows[1]["possibly_duplicate"] is True, "the retry's copy is tagged (R7-2)"
    assert not claim.exists()


def test_file_retry_after_partial_log_write_is_tagged(tmp_path: Path, monkeypatch) -> None:
    """The log write dies part-way (one row out of two): the rows go back to
    the alert file and the next sweep's copies are tagged."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "first")
    aa.append_alert(root, "sOld", "lane1", "coder", "credential", "second")
    assert p
    _backdate(p, 8 * 86400)
    log = tmp_path / ".claude" / "logs" / "agent-alerts-undelivered.jsonl"

    import builtins

    real_open = builtins.open

    class _DiesAfterOneRow:
        def __init__(self, fh):
            self._fh = fh
            self._n = 0

        def write(self, s):
            if self._n >= 1:
                raise OSError(28, "injected: no space left on device")
            self._n += 1
            return self._fh.write(s)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def flaky_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if str(file) == str(log) and "a" in mode:
            return _DiesAfterOneRow(fh)
        return fh

    monkeypatch.setattr(builtins, "open", flaky_open)
    assert aa.sweep_orphan_alerts(root) == 0
    monkeypatch.setattr(builtins, "open", real_open)
    assert p.exists(), "rows handed back to the alert file (never lost)"
    assert [r["message"] for r in _log_rows(tmp_path)] == ["first"]
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(root) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["first", "first", "second"]
    assert "possibly_duplicate" not in rows[0]
    assert rows[1]["possibly_duplicate"] is True and rows[2]["possibly_duplicate"] is True


def test_journal_failure_logs_nothing_and_tags_nothing(tmp_path: Path, monkeypatch) -> None:
    """Leave-alone on failure: when the attempt cannot even be journalled,
    nothing reaches the log, so the later move is a clean first move."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "untouched")
    assert p
    _backdate(p, 8 * 86400)

    def boom(*a, **k):
        raise OSError("injected: rewrite refused")

    monkeypatch.setattr(aa, "_rewrite_rows", boom)
    assert aa.sweep_orphan_alerts(root) == 0
    assert _log_rows(tmp_path) == []
    monkeypatch.undo()
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(root) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["untouched"]
    assert "possibly_duplicate" not in rows[0]


def test_retry_through_readoption_keeps_the_tag(tmp_path: Path, monkeypatch) -> None:
    """A claim whose unlink failed and whose owner then died is RE-ADOPTED
    into the alert file (not swept as a claim); the mark travels with the
    rows, so the eventual orphan move is still tagged."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "via readopt")
    assert p
    _backdate(p, 8 * 86400)
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if aa.DELIVERING_MARK in self.name:
            raise PermissionError("injected")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert aa.sweep_orphan_alerts(root) == 1  # logged; the claim's unlink was refused
    monkeypatch.setattr(Path, "unlink", real_unlink)
    state = tmp_path / ".claude" / "state"
    (claim,) = list(state.glob("*.delivering.*"))
    dead = claim.with_name(claim.name.rpartition(".")[0] + f".{_dead_pid()}")
    claim.replace(dead)  # its owner died
    assert aa.readopt_stale_deliveries(root) == 1
    assert p.exists()
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(root) == 1
    rows = _log_rows(tmp_path)
    assert [r["message"] for r in rows] == ["via readopt", "via readopt"]
    assert "possibly_duplicate" not in rows[0] and rows[1]["possibly_duplicate"] is True


# ── act: broken lines are carried, tagged possibly_broken ──────────────────


def _write_raw(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(data)


def test_broken_lines_in_an_orphan_are_carried_and_tagged(tmp_path: Path) -> None:
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "good row")
    assert p
    _write_raw(p, b'{"kind": "credential", "message": "half\n')       # unparseable
    _write_raw(p, b'[1, 2, 3]\n')                                       # not an object
    _write_raw(p, b'{"message": "bad \xff\xfe bytes"}\n')               # not UTF-8
    _write_raw(p, b'{"kind": "credential", "message": "cut mid-wr')    # truncated (EOF)
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(root) == 1
    rows = _log_rows(tmp_path)
    good = [r for r in rows if not r.get("possibly_broken")]
    broken = [r for r in rows if r.get("possibly_broken")]
    assert [r["message"] for r in good] == ["good row"]
    assert "possibly_broken" not in good[0], "a clean row is not tagged"
    reasons = sorted(r["broken_reason"] for r in broken)
    assert reasons == ["invalid_utf8", "not_an_object", "truncated", "unparseable"]
    by_reason = {r["broken_reason"]: r for r in broken}
    assert by_reason["truncated"]["raw"] == '{"kind": "credential", "message": "cut mid-wr'
    assert by_reason["unparseable"]["raw"] == '{"kind": "credential", "message": "half'
    assert by_reason["not_an_object"]["raw"] == "[1, 2, 3]"
    assert "�" in by_reason["invalid_utf8"]["raw"]
    assert all(r["undelivered_reason"] == "orphaned_alert_file" for r in broken)
    # Every logged line is ONE valid JSON object (the raw text is escaped).
    log = tmp_path / ".claude" / "logs" / "agent-alerts-undelivered.jsonl"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 5


def test_broken_raw_text_is_bounded(tmp_path: Path) -> None:
    aa = _aa()
    p = aa.alert_file(str(tmp_path), "sOld", "lane1")
    assert p
    _write_raw(p, b"{" + b"x" * (aa.MAX_BROKEN_RAW_CHARS * 3) + b"\n")
    _backdate(p, 8 * 86400)
    assert aa.sweep_orphan_alerts(str(tmp_path)) == 1
    (row,) = _log_rows(tmp_path)
    assert row["possibly_broken"] is True
    assert len(row["raw"]) == aa.MAX_BROKEN_RAW_CHARS
    assert row["raw_chars"] == aa.MAX_BROKEN_RAW_CHARS * 3 + 1 and row["raw_cut"] is True


def test_delivery_moves_a_broken_line_to_the_log_instead_of_dropping_it(tmp_path: Path) -> None:
    """Before this lane a delivery's claim silently DROPPED an unparseable
    line (``_read_rows`` skipped it, the claim was then removed). Now the
    good row is delivered and the broken one lands in the log, tagged."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "s1", "lane1", "coder", "credential", "deliver me")
    assert p
    _write_raw(p, b'{"session_id": "s1", "message": "tru')
    out = aa.deliver_for_payload(root, dict(MAIN_CHAT))
    assert "deliver me" in out
    assert not p.exists() and not list((tmp_path / ".claude" / "state").glob("agent_alerts_*"))
    (row,) = _log_rows(tmp_path)
    assert row["possibly_broken"] is True and row["broken_reason"] == "truncated"
    assert row["undelivered_reason"] == "unreadable_alert_row"
    assert row["raw"] == '{"session_id": "s1", "message": "tru'


def test_delivery_keeps_a_broken_line_when_the_log_cannot_be_written(tmp_path: Path) -> None:
    """Leave-alone on failure: an unwritable log hands the broken line back
    to the alert file (as a tagged row), never drops it."""
    aa = _aa()
    root = str(tmp_path)
    p = aa.append_alert(root, "s1", "lane1", "coder", "credential", "deliver me")
    assert p
    _write_raw(p, b"not json at all\n")
    (tmp_path / ".claude" / "logs" / "agent-alerts-undelivered.jsonl").mkdir(parents=True)
    out = aa.deliver_for_payload(root, dict(MAIN_CHAT))
    assert "deliver me" in out
    kept = aa._read_rows(p)
    assert len(kept) == 1 and kept[0]["possibly_broken"] is True
    assert kept[0]["raw"] == "not json at all"


def test_clean_delivery_writes_no_log(tmp_path: Path) -> None:
    """Leave-alone: a file with only good rows never touches the log."""
    aa = _aa()
    root = str(tmp_path)
    assert aa.append_alert(root, "s1", "lane1", "coder", "credential", "fine")
    assert "fine" in aa.deliver_for_payload(root, dict(MAIN_CHAT))
    assert _log_rows(tmp_path) == []


def test_complete_last_row_without_newline_is_not_broken(tmp_path: Path) -> None:
    aa = _aa()
    p = aa.alert_file(str(tmp_path), "s1", "lane1")
    assert p
    _write_raw(p, b'{"session_id": "s1", "message": "complete", "agent_id": "lane1"}')
    rows = aa._read_rows(p)
    assert rows == [{"session_id": "s1", "message": "complete", "agent_id": "lane1"}]


@pytest.mark.parametrize("line", [b"\n", b"   \n", b"\r\n"])
def test_blank_lines_are_not_rows(tmp_path: Path, line: bytes) -> None:
    aa = _aa()
    p = aa.alert_file(str(tmp_path), "s1", "lane1")
    assert p
    _write_raw(p, line)
    assert aa._read_rows(p) == []
