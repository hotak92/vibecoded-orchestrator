# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.106 — RL training-data collection gaps (readiness survey, 2026-11-11).

Owner directive 2026-11-11: "make sure we collect all the data needed for
training." Four collection-side behaviours are pinned here:

* **G1 — dual-log twins carry linked context.** The twin (other-slot)
  retrieval event re-adds ``linked_embs`` / ``linked_type_names`` from the
  enrichment's ``linked_embs_other`` / ``linked_type_names_other`` (packed by
  the ONE packer against the SAME fetched rows, same order and cap), so the
  other slot's net stops training with five zeroed linked slots. The
  ACTIVE-space linked vectors still never leak into a twin.
* **G4 — citation events name their label regime.** Every citation payload
  carries ``label_regime`` (one constant in one home,
  ``rl_logger.CITATION_LABEL_REGIME``); a payload without the field resolves
  to the legacy regime, which is what every pre-v0.2.106 row is.
* **Test isolation (survey §1.6).** No process under a test context may
  reach the real hub — proven against a LIVE local listener, in a child
  process that has NO pytest sentinel in its environment, so only the
  explicit ``RL_HUB_POST_DISABLED`` leg of the WP-R guard can stop the POST.
* **G5 — no pending file without a chat.** A retrieval staged with neither a
  session anchor nor a ``CLAUDE_CODE_SESSION_ID`` around the process (a
  human running ``kg-search`` in a plain shell) can never be attributed to a
  transcript, so nothing is staged for it — the retrieval event still emits.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ACTIVE_SLOT = "qwen3_embed"
OTHER_SLOT = "arctic_embed"


# ── G1: the other slot's linked vectors ───────────────────────────────────


def _obj(props: dict, vector: dict) -> SimpleNamespace:
    """Match the real Weaviate ``obj.properties`` + ``obj.vector`` shape."""
    return SimpleNamespace(properties=props, vector=vector)


def test_enrichment_packs_other_slot_linked_vectors() -> None:
    """``_rl_enrich_nodes_with_linked_embs`` packs ``linked_embs_other`` from
    the SAME fetched rows: same neighbour-first order and cap as the active
    pack, and a linked row without an other-slot vector is skipped — never
    fabricated."""
    pytest.importorskip(
        "weaviate_mcp.server",
        reason="weaviate_mcp.server must be importable for the enrichment test",
    )
    from claude_mcp_servers.weaviate_mcp.server import (
        _rl_enrich_nodes_with_linked_embs,
    )

    sib1 = _obj(  # the matched chunk — vectors in BOTH slots
        {"source_node_id": "a", "title": "A", "chunk_num": 1},
        {ACTIVE_SLOT: [1.0, 0.0], OTHER_SLOT: [0.0, 1.0]},
    )
    sib2 = _obj(  # an extra chunk of the same node — ACTIVE slot only
        {"source_node_id": "a", "title": "A", "chunk_num": 2},
        {ACTIVE_SLOT: [0.8, 0.6]},
    )
    link_b = _obj(  # an actual linked node — vectors in BOTH slots
        {"source_node_id": "b", "title": "B", "chunk_num": 1, "node_type": "tool"},
        {ACTIVE_SLOT: [0.0, 1.0], OTHER_SLOT: [1.0, 1.0]},
    )

    def resolver(name: str):
        class _Q:
            @staticmethod
            def fetch_objects(**kw):
                return SimpleNamespace(objects=[sib1, sib2, link_b])

        return SimpleNamespace(query=_Q())

    nodes = [{
        "title": "A", "collection": "C", "source_id": "a", "chunk_number": 1,
        "links": ["B"], "emb": [1.0, 0.0], "node_type": "concept",
    }]
    _rl_enrich_nodes_with_linked_embs(
        nodes, query_emb=[1.0, 0.0], active_slot=ACTIVE_SLOT,
        other_slot=OTHER_SLOT, other_query_emb=[0.0, 1.0],
        coll_resolver=resolver,
    )
    n = nodes[0]
    # Active pack: extra chunk first, then the linked node (the packer order).
    assert n["linked_embs"] == [[0.8, 0.6], [0.0, 1.0]]
    assert n["linked_type_names"] == ["concept", "tool"]
    # Other-slot pack: sib2 has no other-slot vector (skipped, not fabricated),
    # the linked node does.
    assert n["linked_embs_other"] == [[1.0, 1.0]]
    assert n["linked_type_names_other"] == ["tool"]
    # The other slot's per-node vector still attaches (pre-existing behaviour).
    assert n["emb_other"] == [0.0, 1.0]


def test_twin_carries_other_slot_linked_vectors() -> None:
    """``_build_other_slot_log_nodes`` re-adds the linked fields from the
    ``*_other`` keys under the trainer's field names; the ACTIVE-space linked
    vectors never appear in the twin."""
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cand = {
        "title": "n", "score": 0.5, "emb": [0.1] * 4, "n_emb": [0.1] * 4,
        "linked_embs": [[0.1] * 4], "linked_type_names": ["concept"],
        "cos_ql": 0.3, "cos_nl": 0.2,
        "emb_other": [0.2] * 4,
        "linked_embs_other": [[0.9] * 4, [0.8] * 4],
        "linked_type_names_other": ["concept", "tool"],
    }
    (twin,) = sp._build_other_slot_log_nodes([cand], 1)
    assert twin["linked_embs"] == [[0.9] * 4, [0.8] * 4]
    assert twin["linked_type_names"] == ["concept", "tool"]
    assert [0.1] * 4 not in twin["linked_embs"], "active-space vector leaked"
    assert "cos_ql" not in twin and "cos_nl" not in twin


def test_twin_without_other_linked_vectors_has_no_linked_fields() -> None:
    """A node whose linked rows lack other-slot vectors ships the twin with NO
    linked fields (the trainer zero-pads) — the active pack must not leak."""
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cand = {
        "title": "n", "score": 0.5, "emb": [0.1] * 4,
        "linked_embs": [[0.1] * 4], "linked_type_names": ["concept"],
        "emb_other": [0.2] * 4,
    }
    (twin,) = sp._build_other_slot_log_nodes([cand], 1)
    assert "linked_embs" not in twin
    assert "linked_type_names" not in twin


# ── G4: the citation label regime ─────────────────────────────────────────


def test_citation_event_carries_the_label_regime(monkeypatch) -> None:
    from claude_mcp_servers.rl_client import telemetry_writer
    from claude_mcp_servers.rl_client.rl_logger import (
        CITATION_LABEL_REGIME,
        LEGACY_CITATION_LABEL_REGIME,
        resolve_label_regime,
    )

    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: False)
    posted: list[dict] = []
    writer = telemetry_writer.RLTelemetryWriter(
        project="P", embedding_source="qwen3", embedding_dim=4,
        embedding_model="m", hub_post_fn=posted.append,
    )
    writer.log_citations(
        task_id="T", task_type="mcp_interactive", citations={"A": True},
        cosine_sims={"A": 0.9}, session_id="sess",
    )
    assert len(posted) == 1
    payload = json.loads(posted[0]["payload_json"])
    assert payload["label_regime"] == CITATION_LABEL_REGIME
    assert resolve_label_regime(payload) == CITATION_LABEL_REGIME
    # Every pre-v0.2.106 row has no field — that IS the legacy regime.
    assert resolve_label_regime({"citations": {}}) == LEGACY_CITATION_LABEL_REGIME
    assert resolve_label_regime({}) == LEGACY_CITATION_LABEL_REGIME
    assert resolve_label_regime(None) == LEGACY_CITATION_LABEL_REGIME
    assert CITATION_LABEL_REGIME != LEGACY_CITATION_LABEL_REGIME


# ── Test isolation: no test context can reach the hub ─────────────────────


class _RecordingServer:
    """A one-connection-at-a-time HTTP server that records every request."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.requests: list[bytes] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    data = conn.recv(65536)
                    with self._lock:
                        self.requests.append(data)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                except OSError:
                    pass

    def close(self) -> None:
        self.sock.close()


_CHILD_POST = """
import sys
from claude_mcp_servers.rl_client import hub_writer
ok = hub_writer.post_rl_event(
    {"event_type": "retrieval", "task_id": "X", "task_type": "t",
     "payload_json": "{}"})
print("RESULT", ok)
"""


def _child_env_no_sentinels(tmp_path: Path) -> dict:
    from tests.common.child_env import child_env

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "hub.token").write_text("test-token", encoding="utf-8")
    env = child_env()
    env.update(VCT_STATE_DIR=str(state))
    for key in (
        "PYTEST_CURRENT_TEST", "RL_HUB_POST_DISABLED", "VCT_HUB_ALLOW_TEST_POST",
    ):
        env.pop(key, None)
    return env


def test_guard_blocks_the_post_even_without_the_pytest_sentinel(tmp_path) -> None:
    """The WP-R guard, proven against a LIVE listener: a child process with
    NO pytest sentinel in its env (so ``PYTEST_CURRENT_TEST`` cannot be the
    leg that fires) still must not reach the hub when the suite-wide
    ``RL_HUB_POST_DISABLED`` opt-out is set — while the SAME child WITHOUT
    the opt-out does connect, proving the listener and the child both work
    and it is the guard that suppressed the write."""
    server = _RecordingServer()
    try:
        base = _child_env_no_sentinels(tmp_path)

        # Control: no opt-out — the child reaches the listener (the hub route
        # answers 2xx, so post_rl_event returns True). Without this leg, a
        # "no connection" assertion could pass for the wrong reason.
        env_open = dict(base)
        env_open["VCT_HUB_PORT"] = str(server.port)
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_POST], env=env_open,
            capture_output=True, text=True, timeout=60,
        )
        assert "RESULT True" in result.stdout, result.stdout + result.stderr
        assert server.requests, "control run did not reach the listener"

        # The guard: the opt-out alone suppresses the POST (no connection).
        server.requests.clear()
        env_guarded = dict(base)
        env_guarded["VCT_HUB_PORT"] = str(server.port)
        env_guarded["RL_HUB_POST_DISABLED"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_POST], env=env_guarded,
            capture_output=True, text=True, timeout=60,
        )
        assert "RESULT False" in result.stdout, result.stdout + result.stderr
        assert server.requests == [], "a suppressed test context reached the hub"
    finally:
        server.close()


# ── G5: no pending file without a chat to attribute it ────────────────────


def _fake_srv() -> SimpleNamespace:
    return SimpleNamespace(
        _try_resolve_project_config=lambda: None,
        PROJECT_NAME="P",
    )


def _populate(sp, **overrides) -> None:
    kwargs = dict(
        task_id="T",
        candidates=[{"title": "a", "score": 0.5, "emb": [0.1] * 4}],
        limit=5,
        query_emb=[0.1] * 4,
        embedding_source="qwen3",
        embedding_dim=4,
        embedding_model="m",
        task_type="kg_search_cli",
        query="the query",
        stage_pending_file=True,
        cache_in_memory=False,
    )
    kwargs.update(overrides)
    sp._populate_citation_cache(**kwargs)


def test_no_pending_file_without_any_chat_anchor(monkeypatch) -> None:
    """A search with neither a session id (3-layer chain empty) NOR a
    ``CLAUDE_CODE_SESSION_ID`` in the env (a human running ``kg-search`` in a
    plain shell) stages nothing: no transcript can ever be attributed to it,
    so the file could only rot for the abandoned collector."""
    from claude_mcp_servers.rl_client import citation_pending as cp
    from claude_mcp_servers.rl_client import search_pipeline as sp

    staged: list[dict] = []
    monkeypatch.setattr(cp, "stage_pending", lambda **kw: staged.append(kw) or None)
    monkeypatch.setattr(sp, "_kg_server", _fake_srv)
    for key in ("VCT_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)

    _populate(sp)
    assert staged == [], "staged a pending file no drain can ever attribute"

    # A Claude Code session around the process (the Bash-tool CLI run) keeps
    # staging: the drain attributes it by the transcript's kg-search call.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    _populate(sp)
    assert len(staged) == 1
    assert staged[0]["source"] == "hook"
    assert staged[0]["task_id"] == "T"
    assert staged[0]["session_id"] == "", "no explicit session: nosession file"
