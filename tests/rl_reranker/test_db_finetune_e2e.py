# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""End-to-end DB-only finetune job: _run_finetune_job against a stubbed hub.

This is the integration test the v0.2.11 cutover was missing (audit CONCERN-3):
it drives the FULL job chain — _run_finetune_job → offline_trainer.train →
list_rl_events (stubbed) → RLModel train + save — and pins two behaviours that
unit tests of the pieces could not:

  (c-trained) a non-empty hub corpus → the model checkpoint actually changes on
              disk AND the job reaches state="done" with a "complete" message.
  (c-empty)   an empty/401 hub (list_rl_events soft-fails to []) → the job is
              terminal but reports "No training performed" (NOT a false
              "Fine-tune complete"), and the result points at the UNCHANGED base
              weights — audit CONCERN-1.

These exercise the real RLModel + offline_trainer (no torch mocks), so they also
guard the trained-vs-no-data return contract of offline_trainer.train.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")
pytest.importorskip("torch")


from rl_reranker.rl_server import RLServer, JobState  # noqa: E402
from rl_reranker.rl_model import RLModel  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _make_server(tmp_path, monkeypatch, *, project_id="proj-e2e"):
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "global").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    monkeypatch.setenv("VCT_PROJECT_ID", project_id)
    monkeypatch.setenv("VCT_MODULE_TOKEN", "fake-identity-token")
    return RLServer(
        project_root=project_root,
        ollama_url="http://localhost:11435",
        log_path=str(tmp_path / "logs" / "rl_events.jsonl"),
        reranker_model=None,
        port=0,
        bind_host="127.0.0.1",
    )


def _seed_base_weights(srv) -> Path:
    """Create + save a base checkpoint at the server's state_path."""
    model = RLModel.load_or_create(
        state_path=srv._state_path,
        emb_dim=srv._rl_emb_dim,
        arch_version=srv._rl_arch_version,
        device=srv._rl_device_spec,
        embedding_source=srv._active_emb_source,
    )
    model.save()
    return srv._state_path


def _corpus(emb_dim: int, project_id: str) -> list[dict[str, Any]]:
    """A small trainable retrieval+citation corpus (hub-row shape: the loader
    reshapes payload_json, but list_rl_events is stubbed to return already-
    reshaped event dicts here — see _stub_hub)."""
    events = []
    for i in range(6):
        tid = f"t{i}"
        events.append({
            "event": "retrieval", "schema_version": 3, "task_id": tid,
            "embedding_source": "arctic",
            "query_emb": [0.1 + 0.01 * i] * emb_dim,
            "nodes": [
                {"title": "A", "emb": [0.1 + 0.01 * i] * emb_dim},
                {"title": "B", "emb": [0.9 - 0.01 * i] * emb_dim},
            ],
        })
        events.append({
            "event": "citation", "schema_version": 3, "task_id": tid,
            "cosine_sims": {"A": 0.9, "B": 0.2},
            "literal_cited": {"A": True, "B": False},
        })
    return events


def _stub_hub(srv, events: list[dict]):
    """Stub list_rl_events to return `events` (already in load_events() shape).

    hub_event_loader.load_events_from_hub calls hub_client.list_rl_events(pid,...)
    and expects RAW HUB ROWS (with payload_json). We stub at that boundary by
    returning rows whose payload_json is each event — exactly what the real hub
    sends — so the loader's reshape path is still exercised.
    """
    import json as _json

    async def _fake_list_rl_events(project_id, **kwargs):
        return [
            {
                "id": i + 1, "event_type": ev["event"], "schema_version": 3,
                "ts_ms": 1_700_000_000_000 + i, "project_id": project_id,
                "project_name": project_id, "task_id": ev.get("task_id", ""),
                "task_type": None, "embedding_source": ev.get("embedding_source"),
                "embedding_dim": srv._rl_emb_dim if ev["event"] == "retrieval" else None,
                "embedding_model": None, "payload_json": _json.dumps(ev),
            }
            for i, ev in enumerate(events)
        ]

    srv._hub_client.list_rl_events = _fake_list_rl_events  # type: ignore[assignment]
    # Neutralize the hub-state WRITES (they'd try real HTTP); record the calls
    # so we can assert the finetune still attempts to push status.
    pushed = []

    async def _fake_upsert(project_id, table, key, fields):
        pushed.append({"project_id": project_id, "table": table, "key": key})
        return True

    srv._hub_client.upsert_row = _fake_upsert  # type: ignore[assignment]
    return pushed


def _job(srv, job_id="job-e2e") -> JobState:
    job = JobState(job_id=job_id, kind="finetune", state="running",
                   percent=0, message="", started_at="2026-06-18T00:00:00+00:00")
    srv._jobs[job_id] = job
    return job


class TestFinetuneE2E:
    def test_trained_changes_model_and_reports_done(self, tmp_path, monkeypatch):
        srv = _make_server(tmp_path, monkeypatch)
        base = _seed_base_weights(srv)
        before = base.read_bytes()
        _stub_hub(srv, _corpus(srv._rl_emb_dim, "proj-e2e"))
        job = _job(srv)

        _run(srv._run_finetune_job(
            job_id=job.job_id, project_id="proj-e2e",
            base_weights_path=base, days=30, kind="finetune",
        ))

        assert job.state == "done"
        assert "complete" in job.message.lower()
        # The finetuned artifact is a _finetuned.pt sibling and DIFFERS from base.
        result_path = Path(job.result_weights_path)
        assert result_path.name.endswith("_finetuned.pt")
        assert result_path.exists()
        assert result_path.read_bytes() != before, "training did not change the weights"

    def test_empty_hub_reports_no_events_not_false_done(self, tmp_path, monkeypatch):
        srv = _make_server(tmp_path, monkeypatch)
        base = _seed_base_weights(srv)
        _stub_hub(srv, [])  # empty/401 hub → list_rl_events → []
        job = _job(srv, job_id="job-empty")

        _run(srv._run_finetune_job(
            job_id="job-empty", project_id="proj-e2e",
            base_weights_path=base, days=30, kind="finetune",
        ))

        # Terminal, but HONEST: not a false "complete".
        assert job.state == "done"
        assert "complete" not in job.message.lower()
        assert "no training performed" in job.message.lower()
        # Result points at the UNCHANGED base, not a finetuned artifact.
        assert job.result_weights_path == str(base)

    def test_finetune_requires_hub_client(self, tmp_path, monkeypatch):
        srv = _make_server(tmp_path, monkeypatch)
        base = _seed_base_weights(srv)
        srv._hub_client = None  # simulate a container with no hub client
        job = _job(srv, job_id="job-nohub")

        _run(srv._run_finetune_job(
            job_id="job-nohub", project_id="proj-e2e",
            base_weights_path=base, days=30, kind="finetune",
        ))
        # No _hub_client → job fails loudly (NOT a silent success).
        assert job.state == "failed"


TestDbFinetuneE2E = TestFinetuneE2E
