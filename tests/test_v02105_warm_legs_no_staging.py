"""v0.2.105: the L4 ``--warm-legs`` cache warmer stages NO citation ctx.

The warmer finishes, in the background, a producer leg that missed the
interactive deadline, so the NEXT identical call is a cache hit. The KG
producer stages an RL citation ctx into ``.claude/state/rl_pending`` as a
side effect of every run, and before this fix the warmer's run did too: the
Stop drain then scored context the model had never been shown (junk labels;
17 583 warmer claims on one busy project). The owner rule is that RL training
logs are ALWAYS collected for real retrievals, so the ctx is not dropped: the
warmer HOLDS it beside the cache entry, and the interactive run that later
SHOWS that entry stages it, for its own session, pruned like a fresh
retrieval. Pinned here:

* a warm run leaves ``rl_pending`` untouched and holds the ctx;
* an empty warm result holds nothing;
* the cache hit that shows the entry stages the held ctx (stamped with that
  run's id, its session) and consumes the held file; a hit without a held
  file stages nothing;
* end to end through the real wrappers-free router: miss → warmer → hit;
* an expired held file is garbage-collected, a fresh one kept.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.common.child_env import child_env
from tests.test_v02105_router_hard_deadline import (
    REPO_ROOT,
    ROUTER,
    _env,
    _project,
    _prompt_payload,
    _run,
)

sys.path.insert(0, str(ROUTER.parent))

# A KG producer that stages through the REAL citation_pending.stage_pending
# (as rl_kg_search does inside the leg) and prints one block. Optionally
# blocks until a gate file exists (an event-gated "slow" producer).
STAGING_STUB = """\
import os, sys, time
def main():
    gate = os.environ.get("VCO_TEST_GATE")
    if gate:
        end = time.monotonic() + 300
        while not os.path.exists(gate) and time.monotonic() < end:
            time.sleep(0.02)
    sys.path.insert(0, os.environ["VCO_TEST_REPO"])
    from claude_mcp_servers.rl_client.citation_pending import stage_pending
    stage_pending(session_id=os.environ.get("VCO_TEST_SID", "warm-sess"),
                  task_id="prompt_" + os.environ.get("VCO_TEST_TASK", "t"),
                  seq=None, query="q",
                  ctx={"nodes": [{"title": "Stub Node", "n_emb": [0.1]}],
                       "active_model": "m", "task_type": "prompt_kg_search"},
                  source="hook")
    if os.environ.get("VCO_TEST_EMPTY") != "1":
        print("KG: Stub Node | concept | score=0.90 | TITLES | src=knowledge/stub.md")
"""


def _warm(tmp_path: Path, proj: Path, key: str, **extra: str):
    stub = tmp_path / "kg_stub.py"
    stub.write_text(STAGING_STUB)
    spec = {"project_root": str(proj), "legs": {
        "kg": {"script": str(stub), "argv": ["q", "--limit", "3"], "cache_key": key}}}
    env = child_env(CLAUDE_PROJECT_DIR=str(proj), PYTHONPATH=str(REPO_ROOT),
                    VCO_TEST_REPO=str(REPO_ROOT), WEAVIATE_URL="http://127.0.0.1:9",
                    VCT_STATE_DIR=str(tmp_path / "vct-state"), RL_HUB_POST_DISABLED="1")
    for k in ("VCT_SESSION_ID", "VCO_RL_NO_TRANSCRIPT"):
        env.pop(k, None)
    env.update(extra)
    return subprocess.run([sys.executable, str(ROUTER), "--warm-legs"],
                          input=json.dumps(spec), capture_output=True, text=True,
                          timeout=120, cwd=str(proj), env=env)


def _pending(proj: Path) -> list:
    d = proj / ".claude" / "state" / "rl_pending"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _held(proj: Path, key: str) -> Path:
    return proj / ".claude" / "state" / "rl_warm_held" / f"{key}.json"


# --- 1. the warmer stages nothing -------------------------------------------


def test_a_warm_run_leaves_rl_pending_untouched_and_holds_the_ctx(tmp_path: Path) -> None:
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path, junk=3)
    before = {p.name: p.stat().st_mtime_ns for p in _pending(proj)}
    key = hcr.cache_key("kgi", "prompt", "q", "3", "p1")
    r = _warm(tmp_path, proj, key)
    assert r.returncode == 0, r.stderr
    assert (proj / ".claude" / "state" / "query_cache" / key).is_file(), "cache warmed"
    after = {p.name: p.stat().st_mtime_ns for p in _pending(proj)}
    assert after == before, "the warmer wrote to rl_pending"
    held = json.loads(_held(proj, key).read_text())["held"]
    assert len(held) == 1 and held[0]["ctx"]["nodes"][0]["title"] == "Stub Node"


def test_an_empty_warm_result_holds_nothing(tmp_path: Path) -> None:
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path)
    key = hcr.cache_key("kgi", "prompt", "q-empty", "3", "p1")
    r = _warm(tmp_path, proj, key, VCO_TEST_EMPTY="1")
    assert r.returncode == 0, r.stderr
    assert _pending(proj) == []
    assert not _held(proj, key).exists(), "an uncached result can never be shown"


# --- 2. the run that SHOWS the entry stages it -------------------------------


def _hit(hcr, proj: Path, monkeypatch, run_id: str, prompt_id: str = "p1"):
    plan = hcr._Plan()
    plan.kg_query = "q"
    plan.kg_profile = "prompt"
    plan.kg_limit = 3
    hcr._install_staged_ctx_run_id_wrapper()
    monkeypatch.setattr(hcr, "_CURRENT_RUN_ID", run_id)
    monkeypatch.setenv("VCT_SESSION_ID", "viewer-sess")
    monkeypatch.delenv("VCT_AGENT_ID", raising=False)
    return hcr._run_legs(plan, "viewer-sess", prompt_id, str(proj), "",
                         time.monotonic() + 6.0)


def test_the_cache_hit_that_shows_the_entry_stages_the_held_ctx(
        tmp_path: Path, monkeypatch) -> None:
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path)
    key = hcr.cache_key("kgi", "prompt", "q", "3", "p1")
    assert _warm(tmp_path, proj, key).returncode == 0
    try:
        kg_text, _cg = _hit(hcr, proj, monkeypatch, "router_hit")
        assert "KG: Stub Node" in kg_text
        files = _pending(proj)
        assert len(files) == 1, files
        payload = json.loads(files[0].read_text())
        assert payload["session_id"] == "viewer-sess", "staged for the viewing chat"
        assert payload["ctx"]["router_run_id"] == "router_hit", "amendable by this run"
        assert hcr._STAGED_BY_RUN.get("router_hit") == [str(files[0])]
        assert not _held(proj, key).exists(), "the held ctx is consumed once"
        assert list(_held(proj, key).parent.glob("*")) == []
    finally:
        hcr._STAGED_BY_RUN.pop("router_hit", None)
    # A second hit of the same entry stages nothing more.
    try:
        _hit(hcr, proj, monkeypatch, "router_hit2")
        assert len(_pending(proj)) == 1
        assert "router_hit2" not in hcr._STAGED_BY_RUN
    finally:
        hcr._STAGED_BY_RUN.pop("router_hit2", None)


def test_a_cache_hit_with_nothing_held_stages_nothing(tmp_path: Path, monkeypatch) -> None:
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path)
    key = hcr.cache_key("kgi", "prompt", "q", "3", "p1")
    hcr.cache_put(str(proj), key, "KG: Cached | concept | score=0.90 | TITLES | src=x.md\n")
    try:
        kg_text, _cg = _hit(hcr, proj, monkeypatch, "router_plain")
        assert "KG: Cached" in kg_text
        assert _pending(proj) == []
    finally:
        hcr._STAGED_BY_RUN.pop("router_plain", None)


# --- 3. end to end: miss -> detached warmer -> hit ---------------------------


def test_end_to_end_miss_warm_then_shown(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    kg = tmp_path / "kg_stub.py"
    kg.write_text(STAGING_STUB)
    gate = tmp_path / "gate"
    sid = "sess-e2e"
    env = _env(tmp_path, proj, kg, VCO_TEST_SID="producer-sess", VCO_TEST_TASK="e2e",
               VCO_INJECT_LEG_TIMEOUT_S="0.3", VCO_TEST_GATE=str(gate))
    # The session must come from the payload, as in a real hook process (an
    # in-process router test earlier in the run can leave one in os.environ).
    for k in ("VCT_SESSION_ID", "CLAUDE_SESSION_ID", "VCT_AGENT_ID",
              "VCO_RL_NO_TRANSCRIPT"):
        env.pop(k, None)
    _, r1 = _run([sys.executable, str(ROUTER), "prompt"],
                 _prompt_payload(proj, sid, "pe"), env)
    assert r1 is not None and r1.returncode == 0, r1 and r1.stderr
    assert "KG: Stub Node" not in r1.stdout, "the interactive leg missed its deadline"
    gate.write_text("go\n")
    held_dir = proj / ".claude" / "state" / "rl_warm_held"
    end = time.monotonic() + 120.0
    while time.monotonic() < end and not list(held_dir.glob("*.json")):
        time.sleep(0.2)
    assert list(held_dir.glob("*.json")), "the warmer never held its ctx"
    assert _pending(proj) == [], "the warmer staged a ctx nobody saw"

    _, r2 = _run([sys.executable, str(ROUTER), "prompt"],
                 _prompt_payload(proj, sid, "pe"), env)
    assert r2 is not None and r2.returncode == 0, r2 and r2.stderr
    assert "KG: Stub Node" in r2.stdout, "the next identical call is a cache hit"
    files = _pending(proj)
    assert len(files) == 1, files
    payload = json.loads(files[0].read_text())
    assert payload["session_id"] == sid
    assert payload["ctx"]["router_run_id"].startswith("router_")
    assert payload["ctx"]["nodes"][0]["title"] == "Stub Node"
    assert list(held_dir.glob("*")) == []


# --- 4. garbage collection ---------------------------------------------------


def test_expired_held_ctxs_are_collected(tmp_path: Path, monkeypatch) -> None:
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path)
    held_dir = proj / ".claude" / "state" / "rl_warm_held"
    held_dir.mkdir(parents=True)
    old = held_dir / ("a" * 40 + ".json")
    fresh = held_dir / ("b" * 40 + ".json")
    for f in (old, fresh):
        f.write_text('{"held": []}')
    monkeypatch.setenv("VCO_QUERY_CACHE_TTL", "900")
    past = time.time() - 901
    os.utime(old, (past, past))
    hcr._budget_gc(str(proj))
    assert not old.exists(), "a held ctx outliving its cache entry is removed"
    assert fresh.is_file(), "a held ctx whose cache entry is live is kept"


# --- 5. a session that keeps no transcript stages nothing --------------------


def test_stage_pending_skips_a_session_marked_as_keeping_no_transcript(
        tmp_path: Path, monkeypatch) -> None:
    """The post-commit KG reviewer (``claude -p --no-session-persistence``)
    writes no transcript: its citations could never be scored and were most
    of one busy project's 6 GB backlog. Marked → nothing staged; unmarked →
    staged as before."""
    from claude_mcp_servers.rl_client import citation_pending as cp
    from vco_lib.rl_citation_env import NO_TRANSCRIPT_ENV

    proj = _project(tmp_path)
    def _stage(task_id: str):
        return cp.stage_pending(session_id="s1", task_id=task_id, seq=None, query="q",
                                ctx={"nodes": []}, source="hook",
                                project_root=str(proj))

    monkeypatch.setenv(NO_TRANSCRIPT_ENV, "1")
    assert _stage("prompt_marked") is None
    assert _pending(proj) == []
    monkeypatch.setenv(NO_TRANSCRIPT_ENV, "0")
    assert _stage("prompt_zero") is not None
    monkeypatch.delenv(NO_TRANSCRIPT_ENV)
    assert _stage("prompt_plain") is not None
    assert [p.name for p in _pending(proj)] == ["s1__prompt_plain.json",
                                                "s1__prompt_zero.json"]
