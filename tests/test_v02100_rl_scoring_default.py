# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 - RL scoring: the shipped LOCK, the served toggle beneath it, and
event logging independent of both.

OWNER (2026-10-01): "wire it, but for now we are keeping the RL module off
because we still didn't train the neural network, so keep it unactive for
now" - and logs are ALWAYS collected.

* W5R-02: ``vco_lib/rl_scoring_lock.toml`` is the one home of the lock. While
  it is set, ``search_pipeline._resolve_rl_enabled`` is False whatever the
  per-project row, the host-wide row, the licence or the hub say (including
  hub-down, which used to fall open to the licence alone).
* Beneath the lock, the hub-served ``rl_reranker_enabled_for_project`` and the
  licence still decide (pinned with the lock patched off, i.e. the state that
  applies again once a trained model ships).
* W5R-09: logging independence is exercised through the REAL consumer gates
  (``_retrieval_emit_has_consumer`` / ``_should_capture_citations``) with local
  logging on. The only patched boundaries are ``emit_rl_event`` (the writer),
  the citation-cache stage and the rerank RPC. A gate that consulted the lock,
  the toggle or the licence would drop the emit in at least one cell of the
  matrix and turn it red.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client import search_pipeline
from vco_lib import rl_scoring_lock


def _fake_license(feature_on: bool):
    """Install a stub ``VCThelpers.license`` whose gate answers ``feature_on``."""
    lic = types.ModuleType("VCThelpers.license")
    lic.feature_enabled = lambda *a, **k: feature_on  # type: ignore[attr-defined]
    pkg = types.ModuleType("VCThelpers")
    pkg.license = lic  # type: ignore[attr-defined]
    return {"VCThelpers": pkg, "VCThelpers.license": lic}


def _resolve(feature_on: bool, cfg, *, unlocked: bool = False) -> bool:
    import claude_mcp_servers.weaviate_mcp.server as srv

    with patch.dict(sys.modules, _fake_license(feature_on)), patch.object(
        srv, "_try_resolve_project_config", return_value=cfg
    ):
        if unlocked:
            with patch.object(search_pipeline, "_rl_scoring_lock_reason", return_value=None):
                return search_pipeline._resolve_rl_enabled()
        return search_pipeline._resolve_rl_enabled()


ROW_TRUE = SimpleNamespace(rl_reranker_enabled_for_project=True)
ROW_FALSE = SimpleNamespace(rl_reranker_enabled_for_project=False)
HUB_DOWN = None


# ─── the lock table (the one home) ──────────────────────────────────────────


class TestTheShippedLock:
    def test_ships_locked_with_the_owner_reason(self):
        assert rl_scoring_lock.rl_scoring_locked() is True
        reason = rl_scoring_lock.rl_scoring_lock_reason()
        assert reason and "until the model is trained" in reason
        assert "Data collection continues" in reason

    def test_loader_reads_an_unlocked_table(self, tmp_path: Path):
        p = tmp_path / "lock.toml"
        p.write_text('format_version = 1\nlocked = false\nreason = "r"\n', encoding="utf-8")
        assert rl_scoring_lock.load_rl_scoring_lock(p) == {"locked": False, "reason": "r"}

    @pytest.mark.parametrize(
        "body",
        [
            "format_version = 2\nlocked = true\nreason = 'r'\n",
            "format_version = 1\nlocked = 'yes'\nreason = 'r'\n",
            "format_version = 1\nlocked = true\nreason = ''\n",
            "not toml [",
        ],
    )
    def test_loader_is_loud_on_a_broken_table(self, tmp_path: Path, body: str):
        p = tmp_path / "lock.toml"
        p.write_text(body, encoding="utf-8")
        with pytest.raises(RuntimeError):
            rl_scoring_lock.load_rl_scoring_lock(p)

    def test_loader_is_loud_on_a_missing_table(self, tmp_path: Path):
        with pytest.raises(RuntimeError):
            rl_scoring_lock.load_rl_scoring_lock(tmp_path / "absent.toml")

    def test_a_broken_table_locks_scoring_off_in_the_mcp(self):
        """Cannot confirm scoring is allowed -> do not rerank."""
        with patch(
            "vco_lib.rl_scoring_lock.rl_scoring_lock_reason",
            side_effect=RuntimeError("table unreadable"),
        ):
            assert search_pipeline._rl_scoring_lock_reason() is not None
            assert _resolve(True, ROW_TRUE) is False


# ─── W5R-02: the lock wins in every state ───────────────────────────────────


class TestLockForcesScoringOff:
    @pytest.mark.parametrize("cfg", [ROW_TRUE, ROW_FALSE, HUB_DOWN], ids=["row-true", "row-false", "hub-down"])
    @pytest.mark.parametrize("licensed", [True, False], ids=["pro", "free"])
    def test_scoring_is_off_while_locked(self, cfg, licensed):
        assert _resolve(licensed, cfg) is False

    def test_the_lock_is_what_turns_it_off(self):
        """Same inputs, lock lifted -> scoring on. Proves the OFF above comes
        from the lock, not from the row or licence."""
        assert _resolve(True, ROW_TRUE, unlocked=True) is True
        assert _resolve(True, HUB_DOWN, unlocked=True) is True


class TestResolverBeneathTheLock:
    """The cascade that applies again once the lock lifts (unchanged)."""

    def test_row_off_resolves_scoring_off_even_with_a_licence(self):
        assert _resolve(True, ROW_FALSE, unlocked=True) is False

    def test_row_on_is_honoured_when_licensed(self):
        assert _resolve(True, ROW_TRUE, unlocked=True) is True

    def test_licence_gate_still_beats_a_true_row(self):
        assert _resolve(False, ROW_TRUE, unlocked=True) is False

    def test_hub_unreachable_falls_open_to_the_licence_decision(self):
        assert _resolve(True, HUB_DOWN, unlocked=True) is True
        assert _resolve(False, HUB_DOWN, unlocked=True) is False


# ─── W5R-09: logging is independent of the lock and every scoring state ─────


def _request() -> search_pipeline.RerankRequest:
    return search_pipeline.RerankRequest(
        query="q",
        candidates=[
            {"title": "A", "score": 0.9, "emb": [0.1] * 4, "n_emb": [0.1] * 4, "emb_other": [0.2] * 4}
        ],
        limit=5,
        query_emb=[0.1] * 4,
        embedding_source="qwen3",
        embedding_dim=4,
        embedding_model="qwen3-embedding:0.6b",
        task_id="T",
        task_type="mcp_interactive",
        session_id="s",
        spawn_answer_monitor=False,
        **search_pipeline.dual_log_request_fields(
            {"other_source": "arctic", "other_dim": 4, "other_model": "m", "other_query_emb": [0.2] * 4}
        ),
    )


def _clear_opt_outs(monkeypatch) -> None:
    for var in (
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ONLINE_TRAINING_DISABLED",
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
    ):
        monkeypatch.delenv(var, raising=False)


def _run_pipeline(monkeypatch, *, licensed: bool, cfg, unlocked: bool):
    """Run ``rerank_and_emit`` through the REAL consumer gates."""
    import claude_mcp_servers.weaviate_mcp.server as srv

    emitted: list = []
    reranks: list = []
    staged: list = []

    async def _fake_rerank(**kw):
        reranks.append(kw)
        return None

    monkeypatch.setattr(
        search_pipeline, "emit_rl_event", lambda ev, writer_factory=None: emitted.append(ev.task_id) or True
    )
    monkeypatch.setattr(search_pipeline, "_do_rerank", _fake_rerank)
    monkeypatch.setattr(search_pipeline, "_populate_citation_cache", lambda **kw: staged.append(kw["task_id"]))
    monkeypatch.setattr(search_pipeline, "_drive_retention_housekeeping", lambda: None)
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda *a, **k: cfg)
    if unlocked:
        monkeypatch.setattr(search_pipeline, "_rl_scoring_lock_reason", lambda: None)
    with patch.dict(sys.modules, _fake_license(licensed)):
        res = asyncio.run(search_pipeline.rerank_and_emit(_request()))
    return res, emitted, reranks, staged


@pytest.mark.parametrize("cfg", [ROW_TRUE, ROW_FALSE, HUB_DOWN], ids=["row-true", "row-false", "hub-down"])
@pytest.mark.parametrize("licensed", [True, False], ids=["pro", "free"])
@pytest.mark.parametrize("unlocked", [False, True], ids=["locked", "unlocked"])
def test_logging_is_identical_in_every_scoring_state(monkeypatch, cfg, licensed, unlocked):
    _clear_opt_outs(monkeypatch)
    res, emitted, reranks, staged = _run_pipeline(monkeypatch, licensed=licensed, cfg=cfg, unlocked=unlocked)

    # Primary + twin retrieval events, and the citation capture, in EVERY cell.
    assert emitted == ["T", "T:arctic"]
    assert staged == ["T"]
    assert res.emit_success is True

    # Scoring follows the lock, then the licence + row.
    scoring_expected = unlocked and licensed and cfg is not ROW_FALSE
    assert bool(reranks) is scoring_expected
    assert res.rl_used is False  # the fake rerank RPC answers "fell back"


def test_logging_stays_off_only_by_its_own_opt_out(monkeypatch):
    """Control cell: the consumer gate is LIVE in this harness — the logging
    opt-out (and nothing in the scoring chain) is what suppresses the emit, so
    the matrix above is not green merely because the gate is bypassed."""
    _clear_opt_outs(monkeypatch)
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "1")
    _res, emitted, _reranks, _staged = _run_pipeline(monkeypatch, licensed=True, cfg=ROW_TRUE, unlocked=True)
    assert emitted == []
