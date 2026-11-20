# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 hook-router request bounds and cache regressions."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
sys.path.insert(0, str(ROUTER.parent))

import hook_context_router as router  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402
from weaviate_mcp import kg_http  # noqa: E402
from vco_lib import endpoint_down  # noqa: E402


_KG_PRODUCER = r'''import argparse, os, urllib.request

def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args().rest
    profile = args[args.index("--injection-profile") + 1]
    for _ in range(19 if profile == "prompt" else 12):
        urllib.request.urlopen(os.environ["WEAVIATE_URL"] + "/v1/graphql", timeout=1).read()
    if profile != "prompt":
        print("KG: Measure Node | concept | score=0.90 | TITLES | src=knowledge/measure.md")
'''

_CG_PRODUCER = r'''import argparse, os, urllib.request

def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args().rest
    symbol = args[2]
    for _ in range(9):
        urllib.request.urlopen(os.environ["WEAVIATE_URL"] + "/v1/graphql", timeout=1).read()
    print("CODE: sample.%s | function | def sample.py:1 | callers: (none) | src=sample.py" % symbol)
'''


class _Handler(BaseHTTPRequestHandler):
    """Fake Weaviate: answers ``{}`` and counts every ``/v1/`` request."""

    hits = 0
    lock = threading.Lock()

    def log_message(self, format, *args):  # noqa: A002 — the base class names it so
        pass

    def do_GET(self):
        if self.path.startswith("/v1/"):
            with _Handler.lock:
                _Handler.hits += 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def _request_measurement(tmp_path: Path) -> dict[str, int]:
    """Drive the REAL router; producer doubles reproduce the audited request shapes."""
    project = tmp_path / "project"
    (project / ".claude" / "state").mkdir(parents=True)
    source = project / "sample.py"
    body = "".join(f"def function_{i}():\n    return {i}\n\n" for i in range(5))
    source.write_text(body)
    kg = tmp_path / "kg_producer.py"
    cg = tmp_path / "cg_producer.py"
    kg.write_text(_KG_PRODUCER)
    cg.write_text(_CG_PRODUCER)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{httpd.server_address[1]}"
    env = child_env(**{
        "CLAUDE_PROJECT_DIR": str(project),
        "VCT_STATE_DIR": str(tmp_path / "vct"),
        "HOME": str(tmp_path / "home"),
        "WEAVIATE_URL": endpoint,
        "VCO_ROUTER_KG_SCRIPT": str(kg),
        "VCO_CG_SCRIPT": str(cg),
        "RL_HUB_POST_DISABLED": "1",
        "VCO_INJECT_HARD_DEADLINE_S": "60",
        "VCO_INJECT_LEG_TIMEOUT_S": "4",
    })

    def fire(payload: dict, surface: str, name: str) -> int:
        with _Handler.lock:
            _Handler.hits = 0
        run = subprocess.run(
            [sys.executable, str(ROUTER), surface], input=json.dumps(payload),
            text=True, capture_output=True, cwd=project, env=child_env(env), timeout=30,
        )
        assert run.returncode == 0, run.stderr
        if run.stderr:
            print(f"ROUTER_STDERR {name}: {run.stderr.strip()}")
        time.sleep(3)  # include a detached warmer's requests in the count
        with _Handler.lock:
            return _Handler.hits

    try:
        read = {
            "session_id": "measure-read", "prompt_id": "p1", "cwd": str(project),
            "tool_name": "Read", "tool_input": {"file_path": str(source)},
            "tool_response": {"file": {"filePath": str(source), "content": body}},
        }
        first = fire(read, "read", "read1")
        second = fire(read, "read", "read2")  # same Read, same turn
        read["prompt_id"] = "p2"  # next turn: the KG leg (per-turn key) re-queries
        next_turn = fire(read, "read", "read3")
        prompt = {
            "session_id": "measure-prompt", "prompt_id": "p3", "cwd": str(project),
            "prompt": "Find the project decision governing this otherwise empty retrieval query, with enough detail to pass the prompt-length gate.",
        }
        empty = fire(prompt, "prompt", "prompt")
        return {"one_read": first, "identical_read": second,
                "identical_read_next_turn": next_turn,
                "empty_prompt_including_warmer": empty}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fake_endpoint_request_counts(tmp_path: Path) -> None:
    counts = _request_measurement(tmp_path)
    print("FAKE_ENDPOINT_REQUEST_COUNTS", json.dumps(counts, sort_keys=True))
    assert counts == {
        "one_read": 57,
        "identical_read": 0,
        "identical_read_next_turn": 12,
        "empty_prompt_including_warmer": 19,
    }


def test_free_query_slot_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    with router._query_slot() as acquired:
        assert acquired is True


def test_no_query_slot_for_five_seconds_skips(tmp_path: Path, monkeypatch) -> None:
    from vco_lib.atomic import exclusive_file_lock

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    slot_dir = tmp_path / "hook_router_slots"
    with contextlib.ExitStack() as held:
        for i in range(2):
            held.enter_context(exclusive_file_lock(slot_dir / f"slot-{i}.lock", timeout_s=0))
        started = time.monotonic()
        with router._query_slot() as acquired:
            assert acquired is False
        elapsed = time.monotonic() - started
    assert 4.5 <= elapsed < 6.5


def test_finished_empty_is_cached_but_timeout_is_not(tmp_path: Path) -> None:
    key = router.cache_key("kgi", "prompt", "empty", "3")
    router.cache_finished_empty(str(tmp_path), key)
    assert router.cache_get(str(tmp_path), key) == ""
    other = router.cache_key("kgi", "prompt", "timeout", "3")
    router.cache_put(str(tmp_path), other, "")
    assert router.cache_get(str(tmp_path), other) is None


def test_leg_completion_controls_empty_cache_and_warmer(tmp_path: Path, monkeypatch) -> None:
    producer = tmp_path / "producer.py"
    producer.write_text("def main():\n    pass\n")
    monkeypatch.setenv("VCO_ROUTER_KG_SCRIPT", str(producer))
    warmed: list[str] = []
    monkeypatch.setattr(router, "_spawn_leg_warmer",
                        lambda _root, spec: warmed.extend(spec))

    for status in ("finished", "timed_out", "error"):
        plan = router._Plan()
        plan.kg_profile = "prompt"
        plan.kg_query = f"query-{status}"
        plan.kg_limit = 3
        monkeypatch.setattr(router._hds, "run_legs", lambda legs, leg_timeout_s=None, s=status: (
            router._hds.LegResults({name: "" for name in legs},
                                   status={name: s for name in legs})))
        router._run_legs(plan, "s", "p", str(tmp_path), "", time.monotonic() + 5)
        key = router.cache_key("kgi", "prompt", plan.kg_query, "3", "p")
        if status == "finished":
            assert router.cache_get(str(tmp_path), key) == ""
            assert warmed == []
        else:
            assert router.cache_get(str(tmp_path), key) is None
            assert warmed == ["kg"]
            warmed.clear()


def test_code_cache_is_per_symbol_and_has_no_prompt_id(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(router, "_resolve_cg_script", lambda _root: ROUTER)
    monkeypatch.setattr(router, "_cg_argv", lambda symbol, source, policy, profile: [symbol])

    def fake_run_legs(legs, leg_timeout_s=None):
        calls.extend(legs)
        return router._hds.LegResults({name: f"CODE: {name}\n" for name in legs},
                                      status={name: "finished" for name in legs})

    monkeypatch.setattr(router._hds, "run_legs", fake_run_legs)
    monkeypatch.setattr(router._hds, "_load_cg_module", lambda _path: object())
    monkeypatch.setattr(router._hds, "_pin_argv", lambda *_args: None)

    def plan():
        value = router._Plan()
        value.cg_profile = "read_code"
        value.cg_symbols = [("one", "a.py"), ("two", "a.py")]
        return value

    router._run_legs(plan(), "s", "prompt-a", str(tmp_path), "", time.monotonic() + 5)
    first_calls = list(calls)
    router._run_legs(plan(), "s", "prompt-b", str(tmp_path), "", time.monotonic() + 5)
    assert len(first_calls) == 2
    assert calls == first_calls, "both symbols must hit their prompt-independent caches"


def _stage_ctx(project: Path, titles: list[str], run_id: str) -> Path:
    from claude_mcp_servers.rl_client.citation_pending import stage_pending

    path = stage_pending(
        session_id="s1", task_id="prompt_shown", seq=None, query="q",
        ctx={"nodes": [{"title": title, "n_emb": [0.1]} for title in titles],
             "router_run_id": run_id},
        source="hook", project_root=str(project),
    )
    assert path is not None
    return path


def test_citation_context_keeps_only_actually_shown_titles(tmp_path: Path) -> None:
    pending = _stage_ctx(tmp_path, ["Shown", "Below floor"], "router-test")
    router._amend_pending_citations(
        str(tmp_path), "s1", [], "router-test", staged=[pending],
        shown_titles=["Shown"],
    )
    payload = json.loads(pending.read_text())
    assert [node["title"] for node in payload["ctx"]["nodes"]] == ["Shown"]


def test_no_shown_titles_deletes_citation_context(tmp_path: Path) -> None:
    pending = _stage_ctx(tmp_path, ["Below floor"], "router-test")
    router._amend_pending_citations(
        str(tmp_path), "s1", [], "router-test", staged=[pending], shown_titles=[],
    )
    assert not pending.exists()


def test_router_finalization_prunes_unshown_staged_candidates(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude" / "state").mkdir(parents=True)
    producer = tmp_path / "staging_producer.py"
    producer.write_text(r'''import os, sys
sys.path.insert(0, os.environ["VCO_TEST_REPO"])
from claude_mcp_servers.rl_client.citation_pending import stage_pending

def main():
    stage_pending(session_id="s1", task_id="prompt_final", seq=None, query="q",
                  ctx={"nodes": [{"title": "Shown", "n_emb": [0.1]},
                                  {"title": "Below floor", "n_emb": [0.2]}]},
                  source="hook", project_root=os.environ["CLAUDE_PROJECT_DIR"])
    print("KG: Shown | concept | score=0.90 | TITLES | src=knowledge/shown.md")
''')
    env = child_env(**{
        "CLAUDE_PROJECT_DIR": str(project), "VCT_STATE_DIR": str(tmp_path / "vct"),
        "VCO_TEST_REPO": str(REPO_ROOT), "VCO_ROUTER_KG_SCRIPT": str(producer),
        "VCO_CG_SCRIPT": str(tmp_path / "absent.py"), "WEAVIATE_URL": "http://127.0.0.1:9",
        "RL_HUB_POST_DISABLED": "1",
    })
    run = subprocess.run([sys.executable, str(ROUTER), "prompt"], input=json.dumps({
        "session_id": "s1", "prompt_id": "p1", "cwd": str(project),
        "prompt": "Retrieve the decision records for this sufficiently detailed prompt so the citation finalization path is exercised.",
    }), text=True, capture_output=True, cwd=project, env=env, timeout=30)
    assert run.returncode == 0, run.stderr
    assert "KG: Shown" in run.stdout
    payload = json.loads((project / ".claude" / "state" / "rl_pending" /
                          "s1__prompt_final.json").read_text())
    assert [node["title"] for node in payload["ctx"]["nodes"]] == ["Shown"]


def test_gc_reaches_past_first_200_and_prunes_query_cache(tmp_path: Path) -> None:
    state = tmp_path / ".claude" / "state"
    query = state / "query_cache"
    query.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    ancient = time.time() - 15 * 24 * 3600
    for i in range(205):
        for path in (state / f"warm_leg_{i:04d}", query / f"key-{i:04d}"):
            path.write_text("stale")
            os.utime(path, (old, old))
        suggest = state / f"keyword_suggest_{i:04d}.txt"
        suggest.write_text("a:old\n")
        os.utime(suggest, (ancient, ancient))
    fresh = state / "warm_leg_fresh"
    fresh.write_text("live")
    fresh_suggest = state / "keyword_suggest_fresh.txt"
    fresh_suggest.write_text("a:new\n")
    router._budget_gc(str(tmp_path))
    assert not list(state.glob("warm_leg_[0-9]*"))
    assert not list(state.glob("keyword_suggest_[0-9]*"))
    assert not list(query.iterdir())
    assert fresh.exists() and fresh_suggest.exists()


def test_gc_runs_at_most_hourly(tmp_path: Path) -> None:
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    router._budget_gc(str(tmp_path))  # writes the stamp
    old = time.time() - 8 * 24 * 3600
    stale = state / "warm_leg_late"
    stale.write_text("stale")
    os.utime(stale, (old, old))
    router._budget_gc(str(tmp_path))
    assert stale.exists(), "a second sweep inside the hour must not rescan"


_LONG_PROMPT = ("A sufficiently long prompt that would ordinarily perform a KG query, "
                "with enough additional words to exceed the one-hundred-character gate.")


def _prompt_fire(router_mod, tmp_path: Path, monkeypatch) -> list:
    """Run ``_main(["prompt"])`` in process; return the ``_run_legs`` calls."""
    calls: list = []
    monkeypatch.setattr(router_mod, "_resolve_project_root", lambda _payload: str(tmp_path))
    monkeypatch.setattr(router_mod, "_run_legs",
                        lambda *args: calls.append(args) or ("", ""))
    monkeypatch.setattr(router_mod.sys, "stdin", io.StringIO(json.dumps({
        "session_id": "s1", "prompt_id": "p1", "cwd": str(tmp_path),
        "prompt": _LONG_PROMPT,
    })))
    assert router_mod._main(["prompt"]) == 0
    return calls


def test_endpoint_down_marker_skips_the_next_router_fire(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    monkeypatch.setenv("WEAVIATE_URL", "http://127.0.0.1:9")
    # Leave-alone: no marker → the legs run.
    assert not endpoint_down.endpoint_is_down()
    assert len(_prompt_fire(router, tmp_path, monkeypatch)) == 1
    # Act: a refused connection marks the endpoint; the next fire runs no leg.
    with pytest.raises(kg_http.WeaviateUnreachable):
        kg_http.HttpWeaviateClient("http://127.0.0.1:9", init_timeout=0.05)
    assert endpoint_down.endpoint_is_down("http://127.0.0.1:9")
    assert _prompt_fire(router, tmp_path, monkeypatch) == []
    # The verdict is short-lived: past its TTL the legs run again.
    marker = endpoint_down.down_marker_path("http://127.0.0.1:9")
    old = time.time() - endpoint_down.ENDPOINT_DOWN_TTL_S - 1
    os.utime(marker, (old, old))
    assert not endpoint_down.endpoint_is_down()
    assert len(_prompt_fire(router, tmp_path, monkeypatch)) == 1


def test_down_marker_names_the_endpoint_the_client_uses(tmp_path: Path, monkeypatch) -> None:
    """WEAVIATE_PORT-only config: the router's check and the client's mark agree."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    monkeypatch.delenv("WEAVIATE_URL", raising=False)
    monkeypatch.setenv("WEAVIATE_PORT", "9")
    endpoint_down.mark_endpoint_down("http://localhost:9")
    assert endpoint_down.endpoint_is_down()
    monkeypatch.setenv("WEAVIATE_PORT", "8081")
    assert not endpoint_down.endpoint_is_down()


def test_query_timeout_does_not_mark_endpoint_down(tmp_path: Path, monkeypatch) -> None:
    import urllib.error

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    client = kg_http.HttpWeaviateClient.__new__(kg_http.HttpWeaviateClient)
    client.base_url = "http://127.0.0.1:9"
    client._query_timeout = 0.1

    def raising(exc):
        def _open(*_a, **_kw):
            raise exc
        return _open

    monkeypatch.setattr(kg_http.urllib.request, "urlopen",
                        raising(urllib.error.URLError(TimeoutError("timed out"))))
    with pytest.raises(urllib.error.URLError):
        client._request("/v1/graphql", {})
    assert not endpoint_down.endpoint_is_down(client.base_url)
    monkeypatch.setattr(kg_http.urllib.request, "urlopen",
                        raising(urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
    with pytest.raises(urllib.error.URLError):
        client._request("/v1/graphql", {})
    assert endpoint_down.endpoint_is_down(client.base_url)


def _read_plan(source: str = "a.py") -> "router._Plan":
    plan = router._Plan()
    plan.kg_profile = "read_code"
    plan.kg_query = "read query"
    plan.kg_limit = 3
    plan.cg_profile = "read_code"
    plan.cg_symbols = [("alpha", source)]
    return plan


def test_read_replay_returns_before_legs_only_when_everything_is_seen(tmp_path: Path) -> None:
    root = str(tmp_path)
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    plan = _read_plan()
    kg_key = router._kg_cache_key(plan, "t1")
    cg_key = router.cache_key("cgi", "read_code", "alpha", "a.py")
    # Leave-alone: a cache miss always runs the producer.
    assert router._read_replay_is_fully_seen(plan, "s1", root, "t1") is False
    kg_block = "KG: Node | concept | score=0.90 | TITLES | src=knowledge/node.md\n"
    cg_block = "CODE: mod.alpha | function | def a.py:1 | callers: (none) | src=a.py\n"
    router.cache_put(root, kg_key, kg_block)
    router.cache_put(root, cg_key, cg_block)
    inject = router.seen_store_path("inject", "s1", root)
    # Leave-alone: cached but never shown → the run proceeds (and the check
    # itself records nothing).
    assert router._read_replay_is_fully_seen(plan, "s1", root, "t1") is False
    assert not Path(inject).exists() or Path(inject).read_text() == ""
    # Act: once both blocks were emitted (recorded), the replay is a no-op
    # in the SAME turn.
    reads = router.seen_store_path("reads", "s1", root)
    router.filter_seen_blocks(kg_block + "\n" + cg_block, inject, reads, root)
    assert router._read_replay_is_fully_seen(plan, "s1", root, "t1") is True
    # R31 (per-turn KG key): a NEW turn misses the KG entry → the KG leg
    # re-runs, even though the code-graph entry is still cached.
    assert router._read_replay_is_fully_seen(plan, "s1", root, "t2") is False
    # Leave-alone: a new symbol in the same file is a miss → proceed.
    plan.cg_symbols.append(("beta", "a.py"))
    assert router._read_replay_is_fully_seen(plan, "s1", root, "t1") is False


def test_same_turn_identical_reread_makes_no_requests_new_turn_requeries_kg(
        tmp_path: Path) -> None:
    """Through ``_run_legs``: a second identical Read in the same turn is
    answered from the cache (the KG producer is not run again); a new turn
    runs the KG leg again."""
    root = str(tmp_path)
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    plan = _read_plan()
    plan.cg_profile = ""
    plan.cg_symbols = []
    router.cache_put(root, router._kg_cache_key(plan, "t1"),
                     "KG: Node | concept | score=0.90 | TITLES | src=k.md\n")
    kg_same, _ = router._run_legs(plan, "s1", "t1", root, "", time.monotonic() + 5)
    assert "KG: Node" in kg_same, "same turn: served from the cache"
    assert router.cache_get(root, router._kg_cache_key(plan, "t2")) is None, (
        "a new turn must miss the KG entry (it re-queries)")


def test_read_surface_passes_the_prompt_id_to_the_replay_check(
        tmp_path: Path, monkeypatch) -> None:
    """The main() call site hands the payload's prompt id to the replay check,
    so a same-turn identical re-Read returns before any leg; with the check
    answering False the legs run (leave-alone)."""
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    monkeypatch.delenv("VCO_INJECT_PROFILE", raising=False)
    monkeypatch.setitem(router._PLAN_BUILDERS, "read",
                        lambda _payload, _cwd: _read_plan())
    got: list = []
    legs: list = []

    def fake_seen(plan, state_sid, project_root, prompt_id=""):
        got.append(prompt_id)
        return answer["value"]

    def fake_legs(*args, **kwargs):
        legs.append(1)
        return "", ""

    answer = {"value": True}
    monkeypatch.setattr(router, "_read_replay_is_fully_seen", fake_seen)
    monkeypatch.setattr(router, "_run_legs", fake_legs)
    for value in (True, False):
        answer["value"] = value
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({
            "session_id": "s", "prompt_id": "turn-7", "cwd": str(tmp_path),
            "tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "a.py")},
            "tool_response": {"file": {"filePath": str(tmp_path / "a.py"),
                                       "content": "def alpha():\n    return 1\n"}},
        })))
        router.main(["read"])
    assert got == ["turn-7", "turn-7"]
    assert len(legs) == 1, "True returns before any leg; False runs them"


def test_no_free_slot_skips_uncached_legs_without_caching_or_warming(
        tmp_path: Path, monkeypatch) -> None:
    from vco_lib.atomic import exclusive_file_lock

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    monkeypatch.setattr(router, "HOOK_QUERY_SLOT_WAIT_S", 0.3)
    producer = tmp_path / "producer.py"
    producer.write_text("def main():\n    print('KG: X | concept | score=0.90 | TITLES | src=x.md')\n")
    monkeypatch.setenv("VCO_ROUTER_KG_SCRIPT", str(producer))
    warmed: list = []
    monkeypatch.setattr(router, "_spawn_leg_warmer", lambda _r, spec: warmed.append(spec))
    ran: list = []
    real_run_legs = router._hds.run_legs

    def spy(legs, leg_timeout_s=None):
        ran.append(sorted(legs))
        return real_run_legs(legs, leg_timeout_s=leg_timeout_s)

    monkeypatch.setattr(router._hds, "run_legs", spy)
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)

    def plan(query: str) -> "router._Plan":
        value = router._Plan()
        value.kg_profile = "prompt"
        value.kg_query = query
        value.kg_limit = 3
        return value

    slot_dir = tmp_path / "vct" / "hook_router_slots"
    with contextlib.ExitStack() as held:
        for i in range(router.HOOK_QUERY_SLOTS):
            held.enter_context(exclusive_file_lock(slot_dir / f"slot-{i}.lock", timeout_s=0))
        # Act: both slots busy → no leg runs, nothing cached, nothing warmed.
        kg, cg = router._run_legs(plan("busy"), "s", "p", str(root), "", time.monotonic() + 5)
        assert (kg, cg) == ("", "")
        assert ran == [] and warmed == []
        assert router.cache_get(str(root), router.cache_key("kgi", "prompt", "busy", "3", "p")) is None
        # Leave-alone: a cache hit needs no slot and is served while both are held.
        router.cache_put(str(root), router.cache_key("kgi", "prompt", "hit", "3", "p"), "KG: Hit\n")
        started = time.monotonic()
        kg, _cg = router._run_legs(plan("hit"), "s", "p", str(root), "", time.monotonic() + 5)
        assert kg == "KG: Hit\n" and time.monotonic() - started < 0.25
    # Leave-alone: a free slot → the leg runs.
    kg, _cg = router._run_legs(plan("busy"), "s", "p", str(root), "", time.monotonic() + 5)
    assert ran == [["kg"]] and "KG: X" in kg


def test_hook_producer_passes_full_render_neighbour_floor_only_for_injection(
        tmp_path: Path, monkeypatch) -> None:
    from claude_mcp_servers.rl_client import search_pipeline
    from tests.common.rl_kg_search_harness import import_rl_kg_search, install_fakes, run_main
    from weaviate_mcp import server

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for profile, expected in (("read_code", 0.85), (None, None)):
        state: dict = {}
        install_fakes(server, search_pipeline, state=state, patch=monkeypatch.setattr,
                      cfg={"collections": ["Acme_KnowledgeGraph"],
                           "no_pending_staging": True})
        run_main(import_rl_kg_search(), injection_profile=profile)
        assert state["neighbour_floors"] == [expected]


def test_neighbour_floor_is_hook_only_and_mcp_default_is_unchanged(monkeypatch) -> None:
    from weaviate_mcp import kg_query_core

    calls: list[float] = []
    monkeypatch.setattr(kg_query_core, "_fetch_adjacent_chunks", lambda *_args, **_kw: [])

    def result(score: float) -> dict:
        return {"title": "Node", "file_path": "knowledge/node.md", "score": score,
                "chunk_number": 2, "total_chunks": 3}

    kg_query_core._enrich_with_adjacent_chunks(object(), [result(0.60)], "KG")
    assert calls == []  # default MCP floor still reaches the fetch below
    monkeypatch.setattr(kg_query_core, "_fetch_adjacent_chunks",
                        lambda *_args, **_kw: calls.append(0.60) or [])
    kg_query_core._enrich_with_adjacent_chunks(object(), [result(0.60)], "KG")
    assert calls == [0.60]
    calls.clear()
    kg_query_core._enrich_with_adjacent_chunks(
        object(), [result(0.84)], "KG", min_score=0.85)
    assert calls == []
    kg_query_core._enrich_with_adjacent_chunks(
        object(), [result(0.85)], "KG", min_score=0.85)
    assert calls == [0.60]  # the fake's marker; the high-score fetch occurred
