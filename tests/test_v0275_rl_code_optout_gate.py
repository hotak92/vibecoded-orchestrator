# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 NEW-1 — the CODE telemetry path honours the opt-out COMPUTE gate.

v0.2.73 Concern-A gated the KG path's citation capture + retrieval emit on
"does a consumer exist?" — but the RL-2/RL-2b CODE path (added in the same
release) bypassed both gates: ``_emit_code_retrieval_telemetry`` staged the
citation pending file unconditionally whenever nodes carried ``n_emb`` (so the
turn-end drain answer-embedded in the CODE model space for a result nothing
consumed) and built + posted the retrieval event the writer would drop at its
boundary anyway. ``_emit_code_structure_telemetry`` likewise built + posted
unconditionally.

v0.2.75 reuses the KG path's probes (one concern, one home —
``search_pipeline._should_capture_citations`` / ``_retrieval_emit_has_consumer``):

  * fully opted-out (local logging off, no upload consent) → NO pending file
    staged (⇒ no answer-embed at drain), NO event build/POST.
  * opted-in (default) → behaviour byte-unchanged from v0.2.73.
  * probe error → FALL OPEN: staged + emitted (over-collect, never drop a
    paying user's corpus — house style).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import telemetry_writer  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402

srv = pytest.importorskip(
    "weaviate_mcp.server",
    reason="weaviate_mcp.server must be importable for the code-emit gate tests",
)


def _writer(captured: list) -> RLTelemetryWriter:
    def _post(envelope, timeout: float = 2.0) -> bool:
        captured.append(envelope)
        return True

    return RLTelemetryWriter(
        project="sample-project",
        embedding_source="codesage",
        embedding_dim=16,
        embedding_model="sample-code-model",
        hub_post_fn=_post,
    )


def _code_survivors_with_vectors():
    return [
        {
            "_c": "CodeFunction",
            "_s": 0.61,
            "_p": {
                "full_name": "alpha.processing.process_batch",
                "file_path": "src/alpha/processing.py",
            },
            "_tier": "three_chunks",
            "n_emb": [1.0] + [0.0] * 15,
        },
        {
            "_c": "CodeModule",
            "_s": 0.40,
            "_p": {"path": "src/alpha/util.py"},
            "_tier": "summary",
        },
    ]


@pytest.fixture()
def _env(tmp_path, monkeypatch):
    """Clean two-level opt-out env + isolated project root + no upload consent."""
    for k in (
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ONLINE_TRAINING_DISABLED",
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_SESSION_ID", "sess-code-gate")
    monkeypatch.setenv("CODE_EMBED_MODEL", "sample-code-model")
    # Deterministic upload-consent OFF regardless of this machine's
    # ~/.vibecoded/config.json (the probes read the module attr at call time).
    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: False)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    return tmp_path


def _pending_files(tmp_path) -> list:
    pend_dir = tmp_path / ".claude" / "state" / "rl_pending"
    return list(pend_dir.glob("*.json")) if pend_dir.exists() else []


# ── code-search emit (retrieval event + citation staging) ───────────────


def test_opted_out_stages_nothing_and_skips_emit(_env, monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_retrieval_telemetry(
        query="where are batches validated?",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
        task_id="task-code-optout",
    )
    assert ok is False
    assert not captured, "no retrieval event may be built/posted when opted out"
    assert _pending_files(_env) == [], (
        "no pending file may be staged when opted out — the drain would "
        "answer-embed for a result nothing consumes"
    )


def test_opted_out_global_leg_also_gates(_env, monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED_GLOBAL", "1")
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_retrieval_telemetry(
        query="q",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
    )
    assert ok is False
    assert not captured
    assert _pending_files(_env) == []


def test_opted_in_unchanged_stages_and_emits(_env, monkeypatch):
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_retrieval_telemetry(
        query="where are batches validated?",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
        task_id="task-code-optin",
    )
    assert ok is True
    assert len(captured) == 1
    event = json.loads(captured[0]["payload_json"])
    assert event["task_id"] == "task-code-optin"
    files = _pending_files(_env)
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["ctx"]["retrieval_kind"] == "code"


def test_probe_error_falls_open_and_stages(_env, monkeypatch):
    def _boom():
        raise RuntimeError("gate probe exploded")

    # Both consumer probes read this at call time; raising must FALL OPEN.
    # (The same broken helper also breaks the WRITER's own boundary check
    # downstream — soft-failed there — so the load-bearing fall-open
    # assertion is the citation STAGING: the pending file must exist, i.e.
    # a transient probe failure never silently drops a paying user's corpus.)
    monkeypatch.setattr(telemetry_writer, "_local_logging_disabled", _boom)
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    srv._emit_code_retrieval_telemetry(
        query="q",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
        task_id="task-code-fallopen",
    )
    assert len(_pending_files(_env)) == 1, "probe error must fall open to capture"


def test_upload_consent_alone_keeps_capture_when_local_off(_env, monkeypatch):
    # Local logging off but the user consented to upload → both the citation
    # capture and the retrieval emit still have a consumer.
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: True)
    monkeypatch.setattr(telemetry_writer, "_enqueue", lambda *a, **k: True)
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_retrieval_telemetry(
        query="q",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
        task_id="task-code-upload",
    )
    assert ok is True
    assert len(_pending_files(_env)) == 1


# ── structure emit (no staging — retrieval-consumer gate only) ──────────


def test_structure_emit_skipped_when_opted_out(_env, monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_structure_telemetry(
        query_type="callers",
        target="alpha.processing.process_batch",
        results=[{"full_name": "beta.caller", "file_path": "src/beta.py"}],
    )
    assert ok is False
    assert not captured


def test_structure_emit_unchanged_when_opted_in(_env, monkeypatch):
    captured: list = []
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured))

    ok = srv._emit_code_structure_telemetry(
        query_type="callers",
        target="alpha.processing.process_batch",
        results=[{"full_name": "beta.caller", "file_path": "src/beta.py"}],
    )
    assert ok is True
    assert len(captured) == 1
    event = json.loads(captured[0]["payload_json"])
    assert event["extras"]["retrieval_kind"] == "code_structure"
