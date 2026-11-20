# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 HR-1 / HR-3: an errored lookup is never cached as an answer.

Owner rule: a lookup that FINISHED with nothing above the score floor may be
cached as empty for 300 s; a timeout or an error is NEVER cached as empty and
may be re-run by the warmer. Partial-failure rule (KG fan-out): when any
collection that exists failed its query, the rows the other collections
returned are shown, but the lookup is an error — nothing is cached and the
warmer re-runs it. A collection Weaviate reports as absent is not a failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
RL_KG_SEARCH = REPO_ROOT / "claude_mcp_servers" / "scripts" / "rl_kg_search.py"
sys.path.insert(0, str(ROUTER.parent))

import hook_context_router as router  # noqa: E402  # pyright: ignore[reportMissingImports]
from tests.common.child_env import child_env  # noqa: E402
from weaviate_mcp import kg_http  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    return root


# --- code-graph leg ----------------------------------------------------------


def _run_cg_leg(tmp_path: Path, monkeypatch, producer_body: str):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    producer = tmp_path / "cg_producer.py"
    producer.write_text(producer_body)
    monkeypatch.setattr(router, "_resolve_cg_script", lambda _root: producer)
    monkeypatch.setattr(router, "_cg_argv",
                        lambda symbol, source, policy, profile: [symbol])
    warmed: list = []
    monkeypatch.setattr(router, "_spawn_leg_warmer",
                        lambda _root, spec: warmed.extend(spec))
    root = _project(tmp_path)
    plan = router._Plan()
    plan.cg_profile = "read_code"
    plan.cg_symbols = [("sym", "a.py")]
    _kg, cg = router._run_legs(plan, "s", "p", str(root), "", time.monotonic() + 5)
    key = router.cache_key("cgi", "read_code", "sym", "a.py")
    return cg, router.cache_get(str(root), key), warmed


def test_code_graph_producer_returning_1_is_an_error_not_a_cached_empty(
        tmp_path: Path, monkeypatch) -> None:
    # query_code_graph.main returns 1 on a connect failure or an exception.
    cg, cached, warmed = _run_cg_leg(tmp_path, monkeypatch,
                                     "def main():\n    return 1\n")
    assert cg == ""
    assert cached is None, "an errored code-graph lookup was cached as 'no callers'"
    assert warmed == ["cg_0"]


def test_code_graph_producer_finished_empty_is_still_cached(
        tmp_path: Path, monkeypatch) -> None:
    cg, cached, warmed = _run_cg_leg(tmp_path, monkeypatch,
                                     "def main():\n    return 0\n")
    assert cg == ""
    assert cached == "", "a finished-empty lookup is an answer (300 s)"
    assert warmed == []


# --- knowledge-graph leg (the REAL rl_kg_search, service-free fakes) ----------


def _run_kg_leg(tmp_path: Path, monkeypatch, behaviour: dict):
    from claude_mcp_servers.rl_client import search_pipeline
    from tests.common.rl_kg_search_harness import install_fakes
    from weaviate_mcp import server

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    root = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("VCO_ROUTER_KG_SCRIPT", str(RL_KG_SEARCH))
    install_fakes(server, search_pipeline, state={}, patch=monkeypatch.setattr,
                  cfg={"collections": list(behaviour),
                       "collection_behaviour": behaviour,
                       "no_pending_staging": True})
    warmed: list = []
    monkeypatch.setattr(router, "_spawn_leg_warmer",
                        lambda _root, spec: warmed.extend(spec))
    plan = router._Plan()
    plan.kg_profile = "prompt"
    plan.kg_query = "how does the error cache rule work"
    plan.kg_limit = 3
    kg, _cg = router._run_legs(plan, "s", "p", str(root), "", time.monotonic() + 30)
    key = router._kg_cache_key(plan, "p")
    return kg, router.cache_get(str(root), key), warmed


def test_kg_search_failing_on_every_collection_is_not_cached(
        tmp_path: Path, monkeypatch) -> None:
    kg, cached, warmed = _run_kg_leg(tmp_path, monkeypatch, {
        "Self_KnowledgeGraph": "fail", "Peer_KnowledgeGraph": "fail"})
    assert kg == ""
    assert cached is None, "a KG search that failed everywhere was cached as empty"
    assert warmed == ["kg"]


def test_kg_search_finished_empty_is_still_cached(tmp_path: Path, monkeypatch) -> None:
    kg, cached, warmed = _run_kg_leg(tmp_path, monkeypatch, {
        "Self_KnowledgeGraph": "empty", "Peer_KnowledgeGraph": "empty"})
    assert kg == ""
    assert cached == ""
    assert warmed == []


def test_a_collection_weaviate_reports_absent_is_not_a_failure(
        tmp_path: Path, monkeypatch) -> None:
    kg, cached, warmed = _run_kg_leg(tmp_path, monkeypatch, {
        "Self_KnowledgeGraph": "empty", "Peer_KnowledgeGraph": "missing"})
    assert kg == ""
    assert cached == "", "a never-indexed peer must not make every lookup uncacheable"
    assert warmed == []


def test_partial_failure_is_shown_but_not_cached_as_the_complete_answer(
        tmp_path: Path, monkeypatch) -> None:
    kg, cached, warmed = _run_kg_leg(tmp_path, monkeypatch, {
        "Self_KnowledgeGraph": "ok", "Peer_KnowledgeGraph": "fail"})
    assert "Self_KnowledgeGraph-node-a" in kg, "the answering collection's rows are shown"
    assert cached is None
    assert warmed == ["kg"]


def test_partial_success_without_failures_is_cached(tmp_path: Path, monkeypatch) -> None:
    kg, cached, warmed = _run_kg_leg(tmp_path, monkeypatch, {
        "Self_KnowledgeGraph": "ok", "Peer_KnowledgeGraph": "empty"})
    assert "Self_KnowledgeGraph-node-a" in kg
    assert cached == kg
    assert warmed == []


class _SchemaClient:
    def __init__(self, exc: "BaseException | None") -> None:
        self.exc = exc

    def _get_json(self, path: str) -> dict:
        if self.exc is not None:
            raise self.exc
        return {"class": path.rsplit("/", 1)[-1]}


def test_http_client_exists_is_false_only_on_a_404() -> None:
    def exists(exc):
        return kg_http._Collections(_SchemaClient(exc)).exists("Peer")  # pyright: ignore[reportArgumentType]

    assert exists(None) is True
    assert exists(kg_http.WeaviateHttpQueryError("HTTP 404", status=404)) is False
    with pytest.raises(kg_http.WeaviateHttpQueryError):
        exists(kg_http.WeaviateHttpQueryError("HTTP 500", status=500))
    with pytest.raises(urllib.error.URLError):
        exists(urllib.error.URLError("refused"))


# --- the --warm-legs child ---------------------------------------------------


def _warm(tmp_path: Path, producer_body: str, names=("kg",), **extra: str):
    proj = _project(tmp_path)
    producer = tmp_path / "warm_producer.py"
    producer.write_text(producer_body)
    keys = {name: router.cache_key("kgi", "prompt", f"q-{name}", "3") for name in names}
    spec = {"project_root": str(proj), "legs": {
        name: {"script": str(producer), "argv": ["q"], "cache_key": key}
        for name, key in keys.items()}}
    env = child_env(CLAUDE_PROJECT_DIR=str(proj), PYTHONPATH=str(REPO_ROOT),
                    WEAVIATE_URL="http://127.0.0.1:9",
                    VCT_STATE_DIR=str(tmp_path / "vct"), RL_HUB_POST_DISABLED="1")
    env.update(extra)
    started = time.monotonic()
    run = subprocess.run([sys.executable, str(ROUTER), "--warm-legs"],
                         input=json.dumps(spec), capture_output=True, text=True,
                         timeout=120, cwd=str(proj), env=env)
    elapsed = time.monotonic() - started
    cached = {name: router.cache_get(str(proj), key) for name, key in keys.items()}
    return run, cached, elapsed


def test_warmer_does_not_cache_partial_output_of_an_errored_leg(tmp_path: Path) -> None:
    run, cached, _ = _warm(tmp_path, (
        "def main():\n"
        "    print('KG: Partial | concept | score=0.90 | TITLES | src=knowledge/p.md')\n"
        "    return 1\n"))
    assert run.returncode == 0, run.stderr
    assert cached == {"kg": None}


def test_warmer_caches_a_finished_leg(tmp_path: Path) -> None:
    run, cached, _ = _warm(tmp_path, (
        "def main():\n"
        "    print('KG: Whole | concept | score=0.90 | TITLES | src=knowledge/w.md')\n"))
    assert run.returncode == 0, run.stderr
    assert cached["kg"] is not None and "KG: Whole" in cached["kg"]


def test_warmer_holds_a_slot_for_a_bounded_time(tmp_path: Path) -> None:
    run, cached, elapsed = _warm(
        tmp_path, "import time\ndef main():\n    time.sleep(20)\n",
        names=("kg", "cg_0"), VCO_WARM_LEG_TIMEOUT_S="1")
    assert run.returncode == 0, run.stderr
    assert elapsed < 12, f"the warmer held its slot for {elapsed:.1f}s past the bound"
    assert cached == {"kg": None, "cg_0": None}


# --- HR-1 residual: the structure path no longer swallows a failed query ------


def _qcg_main_with_failing_query(monkeypatch, argv):
    """Drive the REAL query_code_graph.main with a Weaviate that is reachable
    but whose collection lookup raises (shard not ready)."""
    scripts = str(REPO_ROOT / "templates" / "scripts")
    for p in (scripts, str(REPO_ROOT / "claude_mcp_servers"), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    qcg = importlib.import_module("query_code_graph")

    class _Colls:
        def get(self, _name):
            raise RuntimeError("UNAVAILABLE: shard not ready")

    class _Client:
        collections = _Colls()

        def close(self):
            pass

    def _connect(self):
        self.client = _Client()
        return True

    monkeypatch.setattr(qcg.CodeGraphQuery, "connect", _connect)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "Fixture")
    return qcg.main(argv)


def test_structure_query_failure_in_hook_format_is_an_error(
        monkeypatch, capsys) -> None:
    rc = _qcg_main_with_failing_query(
        monkeypatch, ["structure", "callers", "X", "--hook-format"])
    assert rc == 1, "a failed hook-format structure query returned 0 (cached as empty)"
    assert "shard not ready" in capsys.readouterr().err


def test_structure_query_failure_in_cli_mode_still_prints_and_returns_0(
        monkeypatch, capsys) -> None:
    rc = _qcg_main_with_failing_query(monkeypatch, ["structure", "callers", "X"])
    assert rc == 0
    assert "Structure query error: UNAVAILABLE" in capsys.readouterr().err
