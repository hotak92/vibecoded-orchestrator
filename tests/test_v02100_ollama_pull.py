# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 AD-6 / L1-F14 — verified Ollama pulls and a wait that can fail.

Everything runs against :class:`FakeOllama` (the injectable HTTP layer) — no
test here can reach a real Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import ollama_pull as op  # noqa: E402
from vco_lib.embedding_pull_plan import PullPlan  # noqa: E402

BASE = "http://127.0.0.1:19999"
QWEN = "qwen3-embedding:0.6b"
ARCTIC = "snowflake-arctic-embed2:latest"
GEMMA = "gemma4:e4b"


class FakeOllama:
    """A scripted Ollama: ``present`` models in /api/tags, per-model pull streams."""

    def __init__(self, present=(), streams: Mapping[str, list] | None = None,
                 ready: bool = True, list_after_pull: bool = True) -> None:
        self.present = set(present)
        self.streams = dict(streams or {})
        self.ready = ready
        self.list_after_pull = list_after_pull
        self.pulled: list[str] = []
        self.gets: list[str] = []

    def get_json(self, url: str, timeout: float) -> Any:
        self.gets.append(url)
        if not self.ready:
            raise OSError("connection refused")
        assert url == f"{BASE}/api/tags", url
        return {"models": [{"name": m} for m in sorted(self.present)]}

    def post_lines(self, url: str, payload: Mapping[str, Any], timeout: float):
        assert url == f"{BASE}/api/pull", url
        model = payload["name"]
        self.pulled.append(model)
        lines = self.streams.get(model, [{"status": "pulling manifest"}, {"status": "success"}])
        for obj in lines:
            if isinstance(obj, Exception):
                raise obj
            yield (json.dumps(obj) + "\n").encode()
        if self.list_after_pull and lines and lines[-1] == {"status": "success"}:
            self.present.add(model if ":" in model else f"{model}:latest")


def test_present_models_are_skipped_not_repulled():
    fake = FakeOllama(present={QWEN, GEMMA})
    lines: list[str] = []
    res = op.ensure(BASE, [QWEN, GEMMA], load_bearing=[QWEN], http=fake, out=lines.append)
    assert fake.pulled == []
    assert res.present == [QWEN, GEMMA] and res.pulled == []
    assert f"  {QWEN} ... present" in lines


def test_missing_model_is_pulled_and_verified_after():
    fake = FakeOllama(present={QWEN})
    res = op.ensure(BASE, [QWEN, ARCTIC], load_bearing=[QWEN, ARCTIC], http=fake, out=lambda _s: None)
    assert fake.pulled == [ARCTIC] and res.pulled == [ARCTIC]
    assert fake.gets.count(f"{BASE}/api/tags") == 2  # before AND after


def test_stream_without_success_is_a_failure():
    fake = FakeOllama(streams={ARCTIC: [{"status": "pulling manifest"}, {"status": "downloading"}]})
    with pytest.raises(op.OllamaPullError) as ei:
        op.ensure(BASE, [ARCTIC], load_bearing=[ARCTIC], http=fake, out=lambda _s: None)
    assert "without success" in ei.value.failed[ARCTIC]


def test_in_stream_error_is_a_failure():
    fake = FakeOllama(streams={ARCTIC: [{"status": "pulling manifest"},
                                        {"error": "pull model manifest: file does not exist"},
                                        {"status": "success"}]})
    with pytest.raises(op.OllamaPullError) as ei:
        op.ensure(BASE, [ARCTIC], load_bearing=[ARCTIC], http=fake, out=lambda _s: None)
    assert "file does not exist" in ei.value.failed[ARCTIC]


def test_success_but_not_listed_afterwards_is_a_failure():
    fake = FakeOllama(list_after_pull=False)
    with pytest.raises(op.OllamaPullError) as ei:
        op.ensure(BASE, [QWEN], load_bearing=[QWEN], http=fake, out=lambda _s: None)
    assert "/api/tags" in ei.value.failed[QWEN]


def test_inference_failure_is_returned_not_raised_and_all_models_attempted():
    fake = FakeOllama(streams={GEMMA: [OSError("reset")]})
    res = op.ensure(BASE, [GEMMA, QWEN], load_bearing=[QWEN], http=fake, out=lambda _s: None)
    assert fake.pulled == [GEMMA, QWEN]
    assert set(res.failed) == {GEMMA} and res.pulled == [QWEN]


def test_embedding_failure_raised_after_attempting_the_rest():
    fake = FakeOllama(streams={QWEN: [OSError("reset")]})
    with pytest.raises(op.OllamaPullError) as ei:
        op.ensure(BASE, [QWEN, GEMMA], load_bearing=[QWEN], http=fake, out=lambda _s: None)
    assert fake.pulled == [QWEN, GEMMA]
    assert set(ei.value.failed) == {QWEN}
    assert ei.value.deferral_entry().condition_id == "ollama_model_pull_failed"


def test_untagged_name_matches_latest():
    fake = FakeOllama(present={"nomic-embed-text:latest"})
    assert op.verify_present(BASE, ["nomic-embed-text"], http=fake) == (["nomic-embed-text"], [])


def test_not_ready_is_typed_and_bounded(monkeypatch):
    fake = FakeOllama(ready=False)
    ticks = iter(range(1000))
    monkeypatch.setattr(op.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    with pytest.raises(op.OllamaNotReadyError) as ei:
        op.wait_ready(BASE, timeout_s=5, http=fake)
    assert ei.value.deferral_entry().condition_id == "ollama_not_ready_at_update"
    assert len(fake.gets) <= 7


# ── install.py steps 6/7: typed failure → deferral on disk + exit 1 ────────


def _install_env(monkeypatch, tmp_path, fake: FakeOllama, plan: PullPlan):
    monkeypatch.setattr(op, "UrllibHttp", lambda: fake)
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    monkeypatch.setattr(install, "HEALTH_TIMEOUT", 0)
    monkeypatch.setattr(install, "_service_endpoint_urls",
                        lambda: {"ollama_url": BASE, "code_embed_url": None})
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setattr(install, "_inference_models_for_capability", lambda _s: [])
    monkeypatch.setattr(install._embedding_pull_plan, "plan_for_install", lambda *a, **k: plan)


def _ledger_ids(folder: Path) -> list[str]:
    from vco_lib.deferral_report import DeferralReport

    return [e.condition_id for e in DeferralReport.read(folder).entries]


def test_install_step_not_ready_exits_1_with_deferral(monkeypatch, tmp_path, capsys):
    from vco_lib.deferral_report import DeferralReport

    _install_env(monkeypatch, tmp_path, FakeOllama(ready=False),
                 PullPlan(embedding=(QWEN,), inference=()))
    report = DeferralReport()
    rc = install._ollama_models_step({}, object(), report, tmp_path)
    assert rc == 1
    assert _ledger_ids(tmp_path) == ["ollama_not_ready_at_update"]
    assert report.has_condition("ollama_not_ready_at_update")
    err = capsys.readouterr().err
    assert "Traceback" not in err and "ollama_not_ready_at_update" in err


def test_install_step_embedding_pull_failure_exits_1_with_deferral(monkeypatch, tmp_path):
    from vco_lib.deferral_report import DeferralReport

    _install_env(monkeypatch, tmp_path, FakeOllama(streams={QWEN: [{"error": "no space left"}]}),
                 PullPlan(embedding=(QWEN,), inference=()))
    rc = install._ollama_models_step({}, object(), DeferralReport(), tmp_path)
    assert rc == 1
    assert _ledger_ids(tmp_path) == ["ollama_model_pull_failed"]


def test_install_step_success_and_nonfatal_reports(monkeypatch, tmp_path):
    from vco_lib.deferral_report import DeferralReport

    fake = FakeOllama(present={QWEN}, streams={GEMMA: [{"status": "downloading"}]})
    _install_env(monkeypatch, tmp_path, fake,
                 PullPlan(embedding=(QWEN,), inference=(GEMMA,), code_backend_unavailable=True))
    report = DeferralReport()
    assert install._ollama_models_step({}, object(), report, tmp_path) is None
    assert fake.pulled == [GEMMA]
    assert report.has_condition("ollama_model_pull_failed")
    assert report.has_condition("code_embed_backend_unavailable")
    assert _ledger_ids(tmp_path) == []  # non-fatal: the run's own final write carries them


# ── the launcher toggle's ensure ────────────────────────────────────────────


def test_ensure_for_machine_uses_the_row_url_and_records_failure(monkeypatch, tmp_path):
    from vco_lib import service_endpoints

    monkeypatch.setattr(service_endpoints, "machine_service_urls",
                        lambda _db=None: {"ollama_url": BASE})
    fake = FakeOllama(present={QWEN}, streams={ARCTIC: [{"error": "manifest unknown"}]})
    out = op.ensure_for_machine(PullPlan(embedding=(QWEN, ARCTIC), inference=()), tmp_path,
                                quiet=True, http=fake)
    assert not out["ok"] and out["deferral"] == "ollama_model_pull_failed"
    assert out["ollama_url"] == BASE
    assert _ledger_ids(tmp_path) == ["ollama_model_pull_failed"]

    fake2 = FakeOllama(present={QWEN, ARCTIC})
    out2 = op.ensure_for_machine(PullPlan(embedding=(QWEN, ARCTIC), inference=()), tmp_path,
                                 quiet=True, http=fake2)
    assert out2["ok"] and out2["present"] == [QWEN, ARCTIC] and fake2.pulled == []
    assert _ledger_ids(tmp_path) == []  # resolved on success
