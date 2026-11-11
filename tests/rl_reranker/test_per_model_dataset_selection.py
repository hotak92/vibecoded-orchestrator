# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-model dataset selection (2026-07-22) — the loader must assemble a clean
single-embedding-space dataset so two dedicated RL nets (arctic + qwen) can each
train on their own space.

Acceptance criterion (user, 2026-07-22): the logs must enable training TWO
dedicated neural networks, one per embedding model. This requires the loader to
filter retrievals by embedding_source and pair them with the citations that share
their (slot-suffixed) task_id, WITHOUT cross-contaminating the two spaces.

Design under test (verified in source): the dual fan-out writes the OTHER slot's
retrieval + citation on a ``<task_id>:<source>`` suffixed id, while the active
slot keeps the bare task_id. So filter_embedding_source='arctic' + pair_events
yields exactly the arctic (retrieval, citation) pairs and never mixes qwen data.
"""
from __future__ import annotations

from rl_reranker.hub_event_loader import load_events_from_hub
from rl_reranker.offline_trainer import pair_events


class _FakeHubClient:
    """Returns a fixed row list from list_rl_events (async)."""

    def __init__(self, rows):
        self._rows = rows

    async def list_rl_events(self, project_id, *, event_type=None, since_ms=None,
                             until_ms=None, max_events=None):
        return list(self._rows)


def _row(event_type, task_id, source, payload_extra=None):
    import json
    payload = {
        "event": event_type,
        "schema_version": 3,
        "task_id": task_id,
        "task_type": "mcp_interactive",
        "embedding_source": source,
    }
    if payload_extra:
        payload.update(payload_extra)
    return {
        "id": abs(hash((event_type, task_id))) % 100000,
        "event_type": event_type,
        "task_id": task_id,
        "embedding_source": source,
        "schema_version": 3,
        "payload_json": json.dumps(payload),
    }


def _run_in_thread(coro_fn):
    """Drive the sync loader from a worker thread (mirrors the /finetune path)."""
    import threading
    result = {}

    def _worker():
        result["value"] = coro_fn()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return result["value"]


def test_arctic_filter_selects_only_arctic_pairs():
    # qwen active pair on bare task_id; arctic dual pair on :arctic suffix.
    rows = [
        _row("retrieval", "t1", "qwen3", {"query_emb": [0.1, 0.2]}),
        _row("citation", "t1", "qwen3", {"cosine_sims": {"A": 0.8}}),
        _row("retrieval", "t1:arctic", "arctic", {"query_emb": [0.3, 0.4]}),
        _row("citation", "t1:arctic", "arctic", {"cosine_sims": {"A": 0.7}}),
    ]
    hub = _FakeHubClient(rows)

    # Arctic dataset.
    arctic_events = _run_in_thread(
        lambda: load_events_from_hub(hub, "proj", filter_embedding_source="arctic")
    )
    arctic_pairs = pair_events(arctic_events)
    assert len(arctic_pairs) == 1
    ret, cit = arctic_pairs[0]
    assert ret["embedding_source"] == "arctic"
    assert ret["task_id"] == "t1:arctic"
    # The citation paired is the arctic one (same suffixed task_id).
    assert cit["task_id"] == "t1:arctic"

    # Qwen dataset (bare task_id).
    qwen_events = _run_in_thread(
        lambda: load_events_from_hub(hub, "proj", filter_embedding_source="qwen3")
    )
    qwen_pairs = pair_events(qwen_events)
    assert len(qwen_pairs) == 1
    qret, qcit = qwen_pairs[0]
    assert qret["embedding_source"] == "qwen3"
    assert qret["task_id"] == "t1"
    assert qcit["task_id"] == "t1"


def test_no_cross_space_leakage():
    """An arctic retrieval must never pair with a qwen citation (different
    task_id) — the two nets' datasets stay disjoint."""
    rows = [
        _row("retrieval", "t1:arctic", "arctic", {"query_emb": [0.3, 0.4]}),
        _row("citation", "t1", "qwen3", {"cosine_sims": {"A": 0.8}}),  # qwen citation
    ]
    hub = _FakeHubClient(rows)
    arctic_events = _run_in_thread(
        lambda: load_events_from_hub(hub, "proj", filter_embedding_source="arctic")
    )
    # The qwen citation (bare 't1') can't pair with the arctic retrieval
    # ('t1:arctic') — so no pairs form → no cross-space label leakage.
    assert pair_events(arctic_events) == []
