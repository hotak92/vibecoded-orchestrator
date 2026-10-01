# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 owner answers (W3-OLLAMA, W3R-05) — Ollama not answering at step 6.

1. restart Ollama's container ONLY when VCO owns it (row ``vco_managed`` AND the
   container's real compose label is VCO's project) — ``restart`` by name;
   an adopted or foreign Ollama is reported, never touched;
2. a bounded re-wait;
3. still down → the update CONTINUES (exit 0), pulls + the KG seed are skipped
   and owed to the auto_retryable ``ollama_not_ready_at_update`` row, whose
   retry (deferral_retry handler ``ollama_models``) completes BOTH once Ollama
   answers — clearing the row only on proven success.

Text generation: ONE model per tier; the summary runtime never pulls — a model
Ollama does not hold degrades to the next backend.

Fakes only: FakeOllama (HTTP), a fake runtime, a fake seed runner.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from tests.test_v02100_ollama_pull import BASE, GEMMA, QWEN, FakeOllama  # noqa: E402
from vco_lib import containers as _c  # noqa: E402
from vco_lib import deferral_retry as dr  # noqa: E402
from vco_lib import install_weaviate as iw  # noqa: E402
from vco_lib import ollama_pull as op  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.embedding_pull_plan import PullPlan  # noqa: E402

OWN = "infrastructure"


class Runtime:
    """A fake container runtime: records every argv, never touches podman."""

    def __init__(self, rc=0):
        self.calls: list = []
        self.rc = rc

    def __call__(self, argv, **_k):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=self.rc, stdout="", stderr="boom" if self.rc else "")


def _row(mode="vco_managed", name="vco_ollama"):
    return SimpleNamespace(mode=mode, container_name=name, url="")


def _restart(tmp_path, row, *, project=OWN, found="vco_ollama", rt=None):
    rt = rt or Runtime()
    (tmp_path / "infrastructure").mkdir(exist_ok=True)
    (tmp_path / "infrastructure" / "docker-compose.yml").write_text("services: {}\n")
    ident = (lambda n, r: None) if project is None else (lambda n, r: _c.ComposeIdentity(project=project))
    out = op.restart_owned_ollama(tmp_path, "podman", row, run=rt,
                                  find=lambda s, runtime: found, identity=ident)
    return out, rt


# ── 1. restart ONLY an owned container ──────────────────────────────────────


def _verbs(rt):
    return [c[1] for c in rt.calls]


def test_owned_container_is_restarted_by_name(tmp_path):
    (outcome, detail), rt = _restart(tmp_path, _row())
    assert outcome == op.RESTARTED and rt.calls[-1] == ["podman", "restart", "vco_ollama"]
    assert set(_verbs(rt)) <= {"inspect", "restart"}, rt.calls  # read, then restart by name


def test_the_rows_container_is_the_target_not_a_canonical_leftover(tmp_path):
    """W4R-11: the row names `my_ollama`; a stale canonical `vco_ollama` beside
    it is not the service and must not be the one restarted."""
    (outcome, _d), rt = _restart(tmp_path, _row(name="my_ollama"), found="vco_ollama")
    assert outcome == op.RESTARTED and rt.calls[-1] == ["podman", "restart", "my_ollama"]
    assert not any("vco_ollama" in c for c in rt.calls), rt.calls


def test_a_row_naming_a_missing_container_restarts_nothing(tmp_path):
    class Missing(Runtime):
        def __call__(self, argv, **_k):
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=125, stdout="",
                                   stderr="Error: no such container my_ollama")

    rt = Missing()
    (outcome, detail), rt = _restart(tmp_path, _row(name="my_ollama"), found="vco_ollama", rt=rt)
    assert outcome == op.NO_CONTAINER and "my_ollama" in detail
    assert "restart" not in _verbs(rt)


@pytest.mark.parametrize("case", ["adopted row", "foreign label", "no compose label"])
def test_adopted_or_foreign_ollama_is_reported_never_touched(tmp_path, case):
    row = _row(mode="adopted_container", name="their_ollama") if case == "adopted row" else _row()
    project = {"foreign label": "someone_else", "no compose label": None}.get(case, OWN)
    (outcome, detail), rt = _restart(tmp_path, row, project=project)
    assert outcome == op.NOT_OWNED and "restart" not in _verbs(rt)
    assert "not VCO's" in detail


def test_no_container_is_reported_not_created(tmp_path):
    (outcome, _d), rt = _restart(tmp_path, _row(name=""), found=None)
    assert outcome == op.NO_CONTAINER and rt.calls == []


# ── 2. restart → bounded re-wait → ready ────────────────────────────────────


def test_restart_then_rewait_recovers_the_step(monkeypatch):
    fake = FakeOllama(ready=False)
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)

    def recover():
        fake.ready = True
        return op.RESTARTED, "restarted VCO's own container vco_ollama"

    buf = io.StringIO()
    with redirect_stdout(buf):
        err = op.wait_ready_step(BASE, timeout_s=0, log_event=lambda *a, **k: None,
                                 recover=recover, rewait_s=0, http=fake)
    assert err is None and "restarted VCO's own container" in buf.getvalue()


# ── 3. still down → continue, pulls + seed owed ─────────────────────────────


@pytest.fixture
def down_env(monkeypatch):
    fake = FakeOllama(ready=False)
    monkeypatch.setattr(op, "UrllibHttp", lambda: fake)
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    monkeypatch.setattr(install, "HEALTH_TIMEOUT", 0)
    monkeypatch.setattr(install, "_service_endpoint_urls",
                        lambda: {"ollama_url": BASE, "code_embed_url": None})
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "rows",
                        {"ollama": _row(mode="adopted_container", name="their_ollama")})
    monkeypatch.setitem(install._SERVICE_ENDPOINTS, "weaviate_pending", False)
    planned = []
    monkeypatch.setattr(install._embedding_pull_plan, "plan_for_install",
                        lambda *a, **k: planned.append(1) or PullPlan(embedding=(QWEN,), inference=()))
    return fake, planned


def test_still_down_continues_with_the_owed_row_and_skips_pulls_and_seed(down_env, capsys):
    fake, planned = down_env
    report = DeferralReport()
    rc = install._ollama_models_step({}, SimpleNamespace(container_cmd="podman"), report)
    assert rc is None                                     # the update goes on, exit 0
    assert report.has_condition(op.NOT_READY_CID)
    assert planned == [] and fake.pulled == []           # no pull attempted
    assert install._SERVICE_ENDPOINTS["ollama_owed"] is True
    assert install._seed_weaviate(argparse.Namespace()) == iw.OWED
    assert iw.collections_and_seed(lambda: None, lambda: iw.OWED, report=report,
                                   rebuild_performed=False, runtime=lambda: "podman") == iw.OWED
    out = capsys.readouterr().out
    assert "not VCO's container" in out and "Continuing the update WITHOUT" in out


# ── the retry completes pulls + seed (drive the retry entry point) ──────────


@pytest.fixture
def retry_world(monkeypatch, tmp_path):
    folder = tmp_path / "root"
    (folder / "templates" / "scripts").mkdir(parents=True)
    (folder / "templates" / "scripts" / "sync_knowledge_graph.py").write_text("# fake\n")
    fake = FakeOllama(ready=True, present=set())
    monkeypatch.setattr(op, "UrllibHttp", lambda: fake)
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    monkeypatch.setattr("vco_lib.service_endpoints.machine_service_urls",
                        lambda _db=None: {"ollama_url": BASE})
    monkeypatch.setattr("vco_lib.embedding_pull_plan.plan_from_machine",
                        lambda root, db=None: PullPlan(embedding=(QWEN,), inference=(GEMMA,)))
    monkeypatch.setattr(dr, "attempts_path", lambda f: tmp_path / "attempts.jsonl")
    monkeypatch.setattr(dr, "_record_resolution", lambda *a, **k: None)
    report = DeferralReport()
    report.add_entry(op.OllamaNotReadyError(BASE, 120).deferral_entry())
    report.write(folder)
    seeds: list = []

    def runner(argv, cwd):
        seeds.append(list(argv))
        return world.seed_rc

    world = SimpleNamespace(folder=folder, fake=fake, seeds=seeds, runner=runner, seed_rc=0)
    return world


def _ledger(folder):
    return [e.condition_id for e in DeferralReport.read(folder).entries]


def _dispatch(world, monkeypatch, *, through_registry=False):
    if not through_registry:
        monkeypatch.setattr(dr, "handler_name_for",
                            lambda cid: "ollama_models" if cid.startswith("ollama_") else None)
    return dr.dispatch(world.folder, condition_ids=[op.NOT_READY_CID],
                       backend_probe=lambda f, k: True, runner=world.runner,
                       python=sys.executable, single_instance=False)


def test_retry_completes_both_pulls_and_seed_once_ollama_answers(retry_world, monkeypatch):
    res = _dispatch(retry_world, monkeypatch)
    assert [r.status for r in res] == [dr.RETRIED], res
    assert retry_world.fake.pulled == [QWEN, GEMMA]                    # the pulls
    assert any("sync_knowledge_graph.py" in " ".join(a) for a in retry_world.seeds)  # the seed
    assert _ledger(retry_world.folder) == []                           # cleared on proof


def test_retry_leaves_the_row_while_ollama_is_still_down(retry_world, monkeypatch):
    retry_world.fake.ready = False
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr(op.time, "monotonic", lambda: float(next(ticks)))
    res = _dispatch(retry_world, monkeypatch)
    assert res[0].status == dr.SKIPPED and retry_world.seeds == []
    assert _ledger(retry_world.folder) == [op.NOT_READY_CID]


def test_retry_does_not_clear_on_an_unproven_seed(retry_world, monkeypatch):
    retry_world.seed_rc = 1
    res = _dispatch(retry_world, monkeypatch)
    assert res[0].status == dr.FAILED
    assert _ledger(retry_world.folder) == [op.NOT_READY_CID]


def test_retry_does_not_clear_when_the_seed_ran_without_a_backend(retry_world, monkeypatch):
    """Exit 0 is not proof: a KG sync that re-emitted
    ``kg_sync_no_embedding_backend`` did not seed anything."""
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    def runner(argv, cwd):
        retry_world.seeds.append(list(argv))
        emit(retry_world.folder, DeferralEntry(
            condition_id="kg_sync_no_embedding_backend", title="t", detected="d",
            why_deferred="w", command_to_apply="c"))
        return 0

    retry_world.runner = runner
    res = _dispatch(retry_world, monkeypatch)
    assert res[0].status == dr.FAILED
    assert op.NOT_READY_CID in _ledger(retry_world.folder)


def test_registry_rows_name_the_handler():
    """RED until the orchestrator adds ``retry_action = "retry:py:ollama_models"``
    to both toml rows at merge (WP-06 owns the toml this wave)."""
    assert dr.handler_name_for(op.NOT_READY_CID) == "ollama_models"
    assert dr.handler_name_for(op.PULL_FAILED_CID) == "ollama_models"


def test_retry_end_to_end_through_the_real_registry(retry_world, monkeypatch):
    """RED until the same merge: the dispatcher reaches the handler only
    through the registry's ``retry_action``."""
    res = _dispatch(retry_world, monkeypatch, through_registry=True)
    assert [r.status for r in res] == [dr.RETRIED]
    assert _ledger(retry_world.folder) == []


# ── text generation: one model, never pulled at runtime ─────────────────────


def _summary_backends():
    path = REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
    spec = importlib.util.spec_from_file_location("_wp12_sb", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_wp12_sb"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_summary_ollama_tier_needs_the_model_present_and_never_pulls(monkeypatch, tmp_path):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    sb = _summary_backends()
    urls = []

    def tags(held):
        def urlopen(url, timeout=0):
            urls.append(url)
            return _Resp(json.dumps({"models": [{"name": m} for m in held]}).encode())
        return urlopen

    monkeypatch.setattr(sb.urllib.request, "urlopen", tags(["qwen3.5:0.8b"]))
    assert sb.ollama_available() is False                 # the tier model is absent → degrade
    monkeypatch.setattr(sb.urllib.request, "urlopen", tags([sb.OLLAMA_DEFAULT_MODEL]))
    assert sb.ollama_available() is True
    assert all(u.endswith("/api/tags") for u in urls)     # a probe, never /api/pull
