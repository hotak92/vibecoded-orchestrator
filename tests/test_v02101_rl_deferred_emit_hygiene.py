# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 lane 2B — RL deferred-emit hygiene (NB-04 / NB-05 / NB-06).

Three register rows, one module family:

* NB-04 — the detached child used to unlink its payload BEFORE ``emit_now``
  ran, so a crash in that window dropped the events with no loss-ledger line.
  Now the payload survives until the events are sent, and the events the
  emitter NEVER sent record ONE ``deferred_unsent``/``deferred_emit_not_sent``
  line. Events whose POST was ATTEMPTED and failed are excluded: ``hub_writer``
  already records them per event (``emit_rl_event`` reports True for them), so
  the batch line must not re-count them.
* NB-05 — a child that crashed before/after reading left the 0600 temp file in
  /tmp; the child now removes it in a ``finally`` (and an ``atexit`` backstop).
* NB-06 — a CLI-only user (no hub) got one ``hub_not_running`` ledger line PER
  SEARCH; repeated lines in an episode are now coalesced to one, with the count
  carried as ``suppressed=<n>`` onto the next recorded line.

The child is exercised in-process (``_child_main``); the real detached-child
round trip (payload removed after a successful send) is already covered by
``tests/test_v02100_w5r_rl_telemetry_fixes.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_mcp_servers.rl_client import deferred_emit as de
from claude_mcp_servers.rl_client import hub_writer
from claude_mcp_servers.rl_client import telemetry_emit
from vco_lib import rl_telemetry_loss

EV = {"event_type": "retrieval", "task_id": "T1", "task_type": "pre_edit_kg_search",
      "embedding_source": "arctic", "payload_json": "{}"}


# ---------------------------------------------------------------------------
# Shared ledger / state helpers
# ---------------------------------------------------------------------------


def _loss_lines():
    p = rl_telemetry_loss.loss_log_path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def _clean_state():
    hub_writer._reset_hub_down_bookkeeping_for_test()
    rl_telemetry_loss._reset_warned_for_test()
    p = rl_telemetry_loss.loss_log_path()
    if p.exists():
        p.unlink()
    yield
    if p.exists():
        p.unlink()


@pytest.fixture
def hub(monkeypatch):
    """The real poster, hermetic: an explicit test-post allow (so the WP-R
    guard does not short-circuit the write) but a port nothing listens on and
    the token patched per test, so no real hub is ever contacted."""
    monkeypatch.setenv("VCT_HUB_ALLOW_TEST_POST", "1")
    monkeypatch.setattr(hub_writer, "_read_hub_port", lambda: 9)
    monkeypatch.setattr(hub_writer, "_RETRY_PAUSE_S", 0.0)
    hub_writer._reset_hub_down_bookkeeping_for_test()
    return hub_writer


def _write_payload(path: Path, *task_ids: str) -> None:
    written = task_ids or ("tid-1",)
    items = []
    for tid in written:
        items.append({
            "event": {
                "query": "q", "query_emb": [0.1, 0.2], "embedding_source": "qwen3",
                "embedding_dim": 2, "embedding_model": "m", "nodes": [{"title": "n"}],
                "task_id": tid, "task_type": "pre_edit_kg_search",
            },
            "writer": {"project": "p", "project_id": "pid", "embedding_source": "qwen3",
                       "embedding_dim": 2, "embedding_model": "m"},
        })
    path.write_text(json.dumps({"items": items}), encoding="utf-8")


def _stub_emit(monkeypatch, by_task_id) -> None:
    """Make the emitter report per-event success by ``task_id`` — the seam
    ``emit_each`` uses. A True means 'handled' (POST attempted), a False means
    'never sent'."""
    def _send(ev, *, writer_factory=None):
        return by_task_id[ev.task_id]

    monkeypatch.setattr(telemetry_emit, "emit_rl_event", _send)


# ---------------------------------------------------------------------------
# NB-04 — emit first, unlink after; only never-sent events are recorded
# ---------------------------------------------------------------------------


def test_payload_still_exists_while_events_are_being_sent(tmp_path, monkeypatch):
    """NB-04: the payload must outlive the send — the old child unlinked it
    BEFORE the emit, so the emit could never see it."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-c")
    seen: dict = {}

    def _probe(items, **kw):
        seen["exists_during_emit"] = path.exists()
        return [True] * len(items)

    monkeypatch.setattr(de, "emit_each", _probe)
    assert de._child_main([str(path)]) == 0
    assert seen.get("exists_during_emit") is True, "the payload must survive until the POSTs run"
    assert not path.exists(), "the payload is removed after the send"


def test_child_records_one_loss_line_when_events_not_sent(tmp_path, monkeypatch):
    """NB-04: an event the emitter never sent leaves ONE ledger line (kind
    ``deferred_unsent``, not ``hub_post_failed`` — no POST was attempted)."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-a")
    monkeypatch.setattr(de, "emit_each", lambda items, **kw: [False] * len(items))
    assert de._child_main([str(path)]) == 0
    assert not path.exists()
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [
        ("deferred_unsent", "deferred_emit_not_sent")
    ]
    (line,) = _loss_lines()
    assert line["unsent"] == 1 and line["task_ids"] == "tid-a"


def test_full_send_records_no_loss(tmp_path, monkeypatch):
    """A batch that WAS fully handled records nothing (no false loss)."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-ok")
    monkeypatch.setattr(de, "emit_each", lambda items, **kw: [True] * len(items))
    assert de._child_main([str(path)]) == 0
    assert _loss_lines() == []


def test_attempted_but_failed_events_are_not_double_counted(tmp_path, monkeypatch):
    """SF-1: an event whose POST was ATTEMPTED and failed reports True from the
    emitter (hub_writer records it per event elsewhere), so the child must NOT
    add a batch line for it — otherwise the ledger counts it twice."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-5xx")
    _stub_emit(monkeypatch, {"tid-5xx": True})
    assert de._child_main([str(path)]) == 0
    assert _loss_lines() == [], "a handled-but-failed POST is already recorded per event"


def test_batch_line_names_only_the_never_sent_events(tmp_path, monkeypatch):
    """SF-1: in a mixed batch the batch line must name ONLY the never-sent
    events, never the one the emitter handled (that one has its own line)."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-never", "tid-handled")
    _stub_emit(monkeypatch, {"tid-never": False, "tid-handled": True})
    assert de._child_main([str(path)]) == 0
    (line,) = _loss_lines()
    assert line["kind"] == "deferred_unsent"
    assert line["unsent"] == 1
    assert line["task_ids"] == "tid-never", "an attempted event must not be re-counted"


# ---------------------------------------------------------------------------
# NB-05 — the payload never outlives the child
# ---------------------------------------------------------------------------


def test_child_crash_removes_payload_and_records_loss(tmp_path, monkeypatch):
    """NB-05: even when the emit crashes, the finally removes the temp file —
    the old code removed it only on a spawn failure and after the read."""
    path = tmp_path / "payload.json"
    _write_payload(path, "tid-b")

    def _boom(items, **kw):
        raise RuntimeError("child exploded mid-emit")

    monkeypatch.setattr(de, "emit_each", _boom)
    assert de._child_main([str(path)]) == 1
    assert not path.exists(), "the payload must not outlive a crashed child"
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [
        ("deferred_unsent", "deferred_emit_not_sent")
    ]


def test_unreadable_payload_is_removed_and_recorded(tmp_path):
    """A corrupt payload is reported (count unknown) and never left behind."""
    path = tmp_path / "payload.json"
    path.write_text("{ not json", encoding="utf-8")
    assert de._child_main([str(path)]) == 1
    assert not path.exists()
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("deferred_unsent", "deferred_emit_not_sent")
    assert "unsent" not in line, "the count is unknown when the load itself failed"


# ---------------------------------------------------------------------------
# NB-06 — hub_not_running lines are coalesced per episode
# ---------------------------------------------------------------------------


def test_hub_down_lines_are_coalesced_and_count_carried_forward(hub, monkeypatch):
    monkeypatch.setattr(hub, "_read_hub_token", lambda: None)
    for _ in range(5):
        assert hub.post_rl_event(dict(EV)) is False
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [
        ("hub_post_failed", "hub_not_running")
    ], "5 hub-down searches must produce exactly ONE ledger line"
    assert "suppressed" not in lines[0], "the first line of an episode has nothing suppressed yet"

    # A successful hub contact ends the episode.
    monkeypatch.setattr(hub, "_read_hub_token", lambda: "tok")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(hub.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    assert hub.post_rl_event(dict(EV)) is True

    # The next hub-down episode's line carries the 4 that were suppressed.
    monkeypatch.setattr(hub, "_read_hub_token", lambda: None)
    assert hub.post_rl_event(dict(EV)) is False
    lines = _loss_lines()
    assert len(lines) == 2, lines
    assert lines[1]["reason"] == "hub_not_running"
    assert lines[1]["suppressed"] == 4


def test_hub_down_line_keeps_the_event_fields(hub, monkeypatch):
    """Coalescing must not drop the record's own context."""
    monkeypatch.setattr(hub, "_read_hub_token", lambda: None)
    assert hub.post_rl_event(dict(EV)) is False
    (line,) = _loss_lines()
    assert line["task_id"] == "T1" and line["embedding_source"] == "arctic"