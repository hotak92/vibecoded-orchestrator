"""v0.2.105: no injection hook can hang past its harness timeout.

Field measurement (Claude Code transcripts, 2026-10-23..29): the injection
hooks hit their 10 s registration on 65-92 % of runs on a busy project, with a
BIMODAL latency (a run answered in ~65 ms - 4 s, or never). Root cause, measured
on that project: after the producer legs, the router's citation amendment
(W3R-10) listed and JSON-parsed EVERY pending-citation file of the project
(``.claude/state/rl_pending``: 4 800 files / 7 GB, one 1-8 MB ctx per
retrieval of every chat, waiting for the Stop drain) in the hook's main
thread — ~90 s, outside any deadline. Fixes pinned here:

* the amendment touches only the files THIS run staged (no directory scan);
* a HARD end-to-end deadline (``VCO_INJECT_HARD_DEADLINE_S``, default 7 s
  from the wrapper's start) ends any run that is still going — exit 0, no
  injection — whatever the main thread is blocked on;
* every router wrapper stamps its start (``_lib/hook-start``) so both
  deadlines count from the moment the harness's timeout starts.

A FIFO in ``rl_pending`` stands in for the 7 GB backlog: opening it for read
blocks forever, so any code path that still scans the directory HANGS
deterministically (the red-proof), where the field version took 90 s.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
HOOKS = REPO_ROOT / "templates" / "hooks"

_POSIX_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")

# A KG producer that stages its pending citation through the REAL
# citation_pending.stage_pending (as rl_kg_search does inside the leg) and
# prints one block. The detached cache warmer re-runs producers with
# --warm-legs in argv; the stub does nothing there.
STAGING_KG_STUB = """\
import os, sys, time
def main():
    if "--warm-legs" in sys.argv:
        return
    time.sleep(float(os.environ.get("VCO_TEST_STUB_DELAY", "0")))
    sys.path.insert(0, os.environ["VCO_TEST_REPO"])
    from claude_mcp_servers.rl_client.citation_pending import stage_pending
    stage_pending(session_id=os.environ["VCO_TEST_SID"],
                  task_id="prompt_" + os.environ.get("VCO_TEST_TASK", "t"),
                  seq=None, query="q",
                  ctx={"nodes": [{"title": "Stub Node", "n_emb": [0.1]}],
                       "active_model": "m", "task_type": "prompt_kg_search"},
                  source="hook", project_root=os.environ["CLAUDE_PROJECT_DIR"])
    print("KG: Stub Node | concept | score=0.90 | TITLES | src=knowledge/stub.md")
"""

# A producer that never returns in time (a stuck connect with no client
# deadline). Marks that it started so a test can tell "bounded" from "never ran".
STUCK_STUB = """\
import os, sys, time
if "--warm-legs" not in sys.argv:
    open(os.environ["VCO_TEST_MARKER"], "a").write(os.environ.get("VCO_TEST_SURFACE", "?") + "\\n")
def main():
    if "--warm-legs" in sys.argv:
        return
    time.sleep(120)
    print("KG: never | concept | score=0.90 | TITLES | src=x.md")
"""

LONG_PROMPT = ("how does the telemetry ingestion pipeline handle lap segmentation and "
               "tyre temperature smoothing during a live session with pit stops")


def _project(tmp_path: Path, *, fifo: bool = False, junk: int = 0) -> Path:
    proj = Path(os.path.realpath(tmp_path)) / "proj"
    pend = proj / ".claude" / "state" / "rl_pending"
    pend.mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}")
    (proj / "notes.md").write_text("# notes\nsome text about telemetry\n")
    (proj / "pkg").mkdir()
    (proj / "pkg" / "telemetry.py").write_text(
        "def smooth_tyre_temperature(samples):\n    return samples\n")
    for i in range(junk):  # another chat's unscored backlog
        (pend / f"other-sess__prompt_{i:04d}.json").write_text(
            json.dumps({"session_id": "other-sess", "ctx": {"nodes": []}}))
    if fifo:
        os.mkfifo(pend / "zz-backlog__prompt_blocker.json")
    return proj


def _env(tmp_path: Path, proj: Path, kg: Path, **extra: str) -> dict:
    env = child_env(
        CLAUDE_PROJECT_DIR=str(proj),
        VCT_STATE_DIR=str(tmp_path / "vct-state"),
        WEAVIATE_URL="http://127.0.0.1:9",
        PYTHONPATH=str(REPO_ROOT),
        VCO_ROUTER_KG_SCRIPT=str(kg),
        VCO_CG_SCRIPT=str(kg),
        VCO_TEST_REPO=str(REPO_ROOT),
        RL_HUB_POST_DISABLED="1",
    )
    for k in ("VCO_INJECT_PROFILE", "VCO_HOOK_STARTED_AT", "VCO_PROMPT_INJECT",
              "VCO_INJECT_HARD_DEADLINE_S", "VCO_INJECT_BUDGET_S",
              "VCO_INJECT_LEG_TIMEOUT_S", "VCT_DISABLE_HOOKS"):
        env.pop(k, None)
    env.update(extra)
    return env


def _prompt_payload(proj: Path, sid: str, prompt_id: str) -> str:
    return json.dumps({"session_id": sid, "prompt_id": prompt_id, "cwd": str(proj),
                       "hook_event_name": "UserPromptSubmit", "prompt": LONG_PROMPT})


def _run(argv: list, payload: str, env: dict, timeout: float = 25.0):
    """(elapsed, CompletedProcess | None) — None = it hung past ``timeout``
    (the child is killed by subprocess.run: it is this test's own child)."""
    start = time.monotonic()
    try:
        r = subprocess.run(argv, input=payload, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return time.monotonic() - start, None
    return time.monotonic() - start, r


# --- 1. the amendment never reads the shared backlog -------------------------


@_POSIX_FIFO
def test_suppressed_repeat_does_not_read_other_chats_pending_files(tmp_path: Path) -> None:
    """RED on the base (v0.2.104): run B's amendment opens the FIFO and hangs.
    The hard deadline is pushed out of the way so ONLY the amendment fix can
    make this pass; the W3R-10 outcome (B's never-shown ctx deleted, A's kept)
    must still hold."""
    proj = _project(tmp_path, fifo=True, junk=50)
    kg = tmp_path / "kg_stub.py"
    kg.write_text(STAGING_KG_STUB)
    sid = "sess-amend"
    pend = proj / ".claude" / "state" / "rl_pending"
    for pid in ("pa", "pb"):
        env = _env(tmp_path, proj, kg, VCO_TEST_SID=sid, VCO_TEST_TASK=pid,
                   VCO_INJECT_HARD_DEADLINE_S="60")
        elapsed, r = _run([sys.executable, str(ROUTER), "prompt"],
                          _prompt_payload(proj, sid, pid), env)
        assert r is not None, f"run {pid} hung reading the shared rl_pending backlog"
        assert r.returncode == 0, r.stderr
        assert elapsed < 6.0, f"run {pid} took {elapsed:.1f} s"
        if pid == "pa":
            assert "KG: Stub Node" in r.stdout
        else:
            assert "KG: Stub Node" not in r.stdout, "run B is a seen-store repeat"
    assert (pend / f"{sid}__prompt_pa.json").is_file(), "A's shown ctx is kept"
    assert not (pend / f"{sid}__prompt_pb.json").exists(), (
        "B's fully-suppressed ctx is still removed (W3R-10)")
    assert len(list(pend.glob("other-sess__*.json"))) == 50, "other chats' files untouched"


def test_amend_touches_only_files_staged_by_this_run(tmp_path: Path, monkeypatch) -> None:
    """In-process: the candidate list is the run's own record, not a glob —
    a stamped, same-session file the run did NOT record is left alone."""
    sys.path.insert(0, str(ROUTER.parent))
    import hook_context_router as hcr
    from claude_mcp_servers.rl_client.citation_pending import stage_pending

    proj = _project(tmp_path)
    ctx = {"nodes": [{"title": "Hidden", "n_emb": [0.1]}], "router_run_id": "router_x"}
    own = stage_pending(session_id="s1", task_id="prompt_own", seq=None, query="q",
                        ctx=ctx, source="hook", project_root=str(proj))
    foreign = stage_pending(session_id="s1", task_id="prompt_foreign", seq=None,
                            query="q", ctx=ctx, source="hook", project_root=str(proj))
    monkeypatch.setitem(hcr._STAGED_BY_RUN, "router_x", [str(own)])
    hcr._amend_pending_citations(str(proj), "s1", ["Hidden"], "router_x")
    assert not own.exists(), "the run's own never-shown ctx is removed"
    assert foreign.is_file(), "a file the run did not stage is never opened"
    # Nothing recorded → nothing touched (no fallback scan).
    monkeypatch.setitem(hcr._STAGED_BY_RUN, "router_x", [])
    hcr._amend_pending_citations(str(proj), "s1", ["Hidden"], "router_x")
    assert foreign.is_file()


def test_the_staging_wrapper_records_the_files_of_the_run_in_flight(
        tmp_path: Path, monkeypatch) -> None:
    sys.path.insert(0, str(ROUTER.parent))
    import hook_context_router as hcr
    import claude_mcp_servers.rl_client.citation_pending as cp

    proj = _project(tmp_path)
    hcr._install_staged_ctx_run_id_wrapper()
    monkeypatch.setattr(hcr, "_CURRENT_RUN_ID", "router_rec")
    path = cp.stage_pending(session_id="s1", task_id="prompt_rec", seq=None, query="q",
                            ctx={"nodes": []}, source="hook", project_root=str(proj))
    try:
        assert hcr._STAGED_BY_RUN.get("router_rec") == [str(path)]
        assert json.loads(Path(path).read_text())["ctx"]["router_run_id"] == "router_rec"
    finally:
        hcr._STAGED_BY_RUN.pop("router_rec", None)
    # Outside a run: passthrough, nothing recorded.
    monkeypatch.setattr(hcr, "_CURRENT_RUN_ID", "")
    cp.stage_pending(session_id="s1", task_id="prompt_out", seq=None, query="q",
                     ctx={"nodes": []}, source="hook", project_root=str(proj))
    assert "" not in hcr._STAGED_BY_RUN


# --- 2. concurrency: N sessions x repeated calls, shared backlog --------------


@_POSIX_FIFO
def test_concurrent_sessions_never_hang(tmp_path: Path) -> None:
    """6 concurrent chats x 3 prompts each against one project whose backlog
    holds the FIFO. RED on the base: every repeat (suppressed) run hangs. After
    the fix every run ends well under the 10 s harness timeout."""
    proj = _project(tmp_path, fifo=True, junk=100)
    kg = tmp_path / "kg_stub.py"
    kg.write_text(STAGING_KG_STUB)
    results: list = []
    lock = threading.Lock()

    def chat(i: int) -> None:
        sid = f"sess-{i}"
        for c in range(3):
            env = _env(tmp_path, proj, kg, VCO_TEST_SID=sid, VCO_TEST_TASK=f"{i}_{c}",
                       VCO_TEST_STUB_DELAY="0.3",
                       VCO_HOOK_STARTED_AT="%.6f" % time.time())
            elapsed, r = _run([sys.executable, str(ROUTER), "prompt"],
                              _prompt_payload(proj, sid, f"p{c}"), env, timeout=12.0)
            with lock:
                results.append((sid, c, elapsed, r is not None and r.returncode == 0))

    threads = [threading.Thread(target=chat, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 18
    hung = [(s, c, round(e, 1)) for s, c, e, ok in results if not ok or e >= 9.0]
    assert not hung, f"runs at/over the harness timeout: {hung}"


# --- 3. the hard deadline, per surface -----------------------------------------


def _surface_payloads(proj: Path) -> dict:
    py = str(proj / "pkg" / "telemetry.py")
    return {
        "prompt": {"prompt": LONG_PROMPT, "prompt_id": "p1",
                   "hook_event_name": "UserPromptSubmit"},
        "edit": {"tool_name": "Edit", "tool_input": {
            "file_path": py, "old_string": "    return samples",
            "new_string": "    return list(samples)"}},
        "write": {"tool_name": "Write", "tool_input": {
            "file_path": str(proj / "pkg" / "fresh_module.py"), "content": "x = 1\n"}},
        "bash": {"tool_name": "Bash", "tool_use_id": "toolu_hd",
                 "tool_input": {"command": f"cat {proj / 'notes.md'}"}},
        "read": {"tool_name": "Read", "tool_input": {"file_path": py},
                 "tool_response": {"file": {"content": (proj / "pkg" / "telemetry.py").read_text()}}},
        "grep": {"tool_name": "Grep", "tool_input": {"pattern": "smooth_tyre_temperature"}},
        "agent": {"tool_name": "Agent", "tool_use_id": "toolu_ag", "tool_input": {
            "description": "telemetry review", "subagent_type": "general-purpose",
            "prompt": "Review the telemetry smoothing module for tyre temperature handling " * 3}},
    }


@pytest.mark.parametrize("surface", ["prompt", "edit", "write", "bash", "read", "grep", "agent"])
def test_hard_deadline_ends_every_surface(tmp_path: Path, surface: str) -> None:
    """The leg budgets are set far beyond the harness timeout (a misconfigured
    or ineffective inner bound) and the producer never returns: the hard
    deadline alone must end the run, exit 0, with nothing injected. RED on
    the base: the run lasts the 30 s leg timeout."""
    proj = _project(tmp_path)
    stub = tmp_path / "stuck.py"
    stub.write_text(STUCK_STUB)
    marker = tmp_path / "started.txt"
    payload = {"session_id": "sess-hd", "cwd": str(proj), **_surface_payloads(proj)[surface]}
    env = _env(tmp_path, proj, stub, VCO_TEST_MARKER=str(marker), VCO_TEST_SURFACE=surface,
               VCO_INJECT_BUDGET_S="30", VCO_INJECT_LEG_TIMEOUT_S="30",
               VCO_INJECT_HARD_DEADLINE_S="2")
    elapsed, r = _run([sys.executable, str(ROUTER), surface], json.dumps(payload), env,
                      timeout=20.0)
    assert marker.is_file() and surface in marker.read_text(), (
        f"{surface}: the stuck producer never started — the payload ran no leg"
        f" ({r.stderr[-600:] if r else 'hung'})")
    assert r is not None, f"{surface}: the run hung past 20 s"
    assert r.returncode == 0, r.stderr
    assert elapsed < 5.0, f"{surface}: {elapsed:.1f} s with a 2 s hard deadline"
    assert "KG: never" not in r.stdout


def test_hard_deadline_counts_from_the_wrapper_start(tmp_path: Path) -> None:
    """A wrapper that already spent 1.5 s before spawning the router leaves the
    router 0.5 s of a 2 s hard deadline."""
    proj = _project(tmp_path)
    stub = tmp_path / "stuck.py"
    stub.write_text(STUCK_STUB)
    env = _env(tmp_path, proj, stub, VCO_TEST_MARKER=str(tmp_path / "m"),
               VCO_INJECT_BUDGET_S="30", VCO_INJECT_LEG_TIMEOUT_S="30",
               VCO_INJECT_HARD_DEADLINE_S="2",
               VCO_HOOK_STARTED_AT="%.6f" % (time.time() - 1.5))
    elapsed, r = _run([sys.executable, str(ROUTER), "prompt"],
                      _prompt_payload(proj, "sess-a", "p1"), env, timeout=20.0)
    assert r is not None and r.returncode == 0
    assert elapsed < 2.0, f"{elapsed:.1f} s: the wrapper's 1.5 s were not counted"


def test_a_fast_run_is_untouched_by_the_watchdog(tmp_path: Path) -> None:
    """Leave-alone side: a run that finishes in time emits its block and
    exits normally, long before the hard deadline."""
    proj = _project(tmp_path)
    kg = tmp_path / "kg_stub.py"
    kg.write_text(STAGING_KG_STUB)
    env = _env(tmp_path, proj, kg, VCO_TEST_SID="sess-f", VCO_INJECT_HARD_DEADLINE_S="5")
    elapsed, r = _run([sys.executable, str(ROUTER), "prompt"],
                      _prompt_payload(proj, "sess-f", "p1"), env)
    assert r is not None and r.returncode == 0, r and r.stderr
    assert "KG: Stub Node" in r.stdout
    assert "hard deadline" not in r.stderr
    assert elapsed < 4.0


def test_the_warmer_child_is_never_armed(tmp_path: Path) -> None:
    """The detached --warm-legs cache warmer has no deadline by design (L4):
    the watchdog must not end it."""
    proj = _project(tmp_path)
    slow = tmp_path / "slow_warm.py"
    out = tmp_path / "warm_done"
    slow.write_text(
        "import os, time\n"
        "def main():\n"
        "    time.sleep(2.5)\n"
        f"    open({str(out)!r}, 'w').write('done')\n"
        "    print('KG: warmed | concept | score=0.9 | TITLES')\n")
    spec = {"project_root": str(proj),
            "legs": {"kg": {"script": str(slow), "argv": [], "cache_key": "k" * 40}}}
    env = _env(tmp_path, proj, slow, VCO_INJECT_HARD_DEADLINE_S="1")
    elapsed, r = _run([sys.executable, str(ROUTER), "--warm-legs"], json.dumps(spec), env)
    assert r is not None and r.returncode == 0
    assert out.is_file(), "the warmer was cut short by the hard deadline"
    assert elapsed >= 2.5


def test_hard_deadline_sits_under_every_injection_registration() -> None:
    """The default hard deadline leaves >= 3 s of the registered timeout of
    every surface the router serves, and is above the leg budget."""
    import re

    src = ROUTER.read_text(encoding="utf-8")
    hard = float(re.search(r"HARD_DEADLINE_DEFAULT_S = ([0-9.]+)", src).group(1))
    budget = float(re.search(r'env_float\("VCO_INJECT_BUDGET_S", ([0-9.]+)', src).group(1))
    assert hard > budget
    wrappers = ("prompt-context-inject", "pre-edit-context-inject", "pre-write-context-inject",
                "pre-bash-context-inject", "read-context-inject", "grep-context-inject",
                "agent-brief-kg-inject")
    for tpl in ("settings.json.linux.template", "settings.json.windows.template"):
        doc = json.loads((REPO_ROOT / "templates" / tpl).read_text(encoding="utf-8"))
        seen = set()
        for groups in doc["hooks"].values():
            for g in groups:
                for h in g.get("hooks", []):
                    for w in wrappers:
                        if w in h.get("command", ""):
                            seen.add(w)
                            assert h.get("timeout", 60) - hard >= 3, (tpl, w, h.get("timeout"))
        assert seen == set(wrappers), (tpl, set(wrappers) - seen)


# --- 4. the wrappers stamp their start, sh + ps1 ------------------------------

_ROUTER_WRAPPERS = ("prompt-context-inject", "pre-edit-context-inject", "pre-write-context-inject",
                    "pre-bash-context-inject", "read-context-inject", "grep-context-inject",
                    "agent-brief-kg-inject")


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_hook_start_lib_sh_exports_a_plausible_stamp() -> None:
    out = subprocess.run(
        ["bash", "-c", f'. "{HOOKS / "_lib" / "hook-start.sh"}"; vco_mark_hook_start; '
                       'bash -c \'printf %s "$VCO_HOOK_STARTED_AT"\''],
        capture_output=True, text=True, timeout=20).stdout
    assert abs(float(out) - time.time()) < 5


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh required")
def test_hook_start_lib_ps1_exports_a_plausible_stamp() -> None:
    from tests.common.pwsh import PWSH_BASE_FLAGS

    cmd = (f". '{HOOKS / '_lib' / 'hook-start.ps1'}'; Set-VcoHookStartedAt; "
           "[Console]::Out.Write($env:VCO_HOOK_STARTED_AT)")
    out = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", cmd],
                         capture_output=True, text=True, timeout=60).stdout
    assert abs(float(out) - time.time()) < 5


@pytest.mark.parametrize("wrapper", _ROUTER_WRAPPERS)
def test_every_router_wrapper_stamps_its_start(tmp_path: Path, wrapper: str) -> None:
    """Behavioural, per wrapper: the router it spawns inherits a fresh
    VCO_HOOK_STARTED_AT (sh; the ps1 sibling where pwsh exists)."""
    if not shutil.which("bash"):
        pytest.skip("bash required")
    proj = _project(tmp_path)
    root = tmp_path / "install"
    scripts = root / "claude_mcp_servers" / "scripts"
    scripts.mkdir(parents=True)
    log = tmp_path / "spawn.log"
    (scripts / "hook_context_router.py").write_text(
        "import os, sys\nsys.stdin.read()\n"
        f"open({str(log)!r}, 'a').write(os.environ.get('VCO_HOOK_STARTED_AT', '') + '\\n')\n")
    env = {**child_env(), "CLAUDE_PROJECT_DIR": str(proj), "VCT_VENV": sys.executable,
           "VCT_INSTALL_ROOT": str(root), "VCT_STATE_DIR": str(tmp_path / "vct-state"),
           "WEAVIATE_URL": "http://127.0.0.1:9"}
    for k in ("VCT_DISABLE_HOOKS", "VCO_INJECT_PROFILE", "VCO_HOOK_STARTED_AT"):
        env.pop(k, None)
    payload = {"session_id": "sess-w", "cwd": str(proj),
               **_surface_payloads(proj)[{"prompt-context-inject": "prompt",
                                          "pre-edit-context-inject": "edit",
                                          "pre-write-context-inject": "write",
                                          "pre-bash-context-inject": "bash",
                                          "read-context-inject": "read",
                                          "grep-context-inject": "grep",
                                          "agent-brief-kg-inject": "agent"}[wrapper]]}
    runs = [["bash", str(HOOKS / f"{wrapper}.sh")]]
    if shutil.which("pwsh"):
        from tests.common.pwsh import PWSH_BASE_FLAGS
        runs.append(["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / f"{wrapper}.ps1")])
    for argv in runs:
        log.unlink(missing_ok=True)
        t0 = time.time()
        subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(proj))
        assert log.is_file(), f"{argv[0]} {wrapper}: the router was never spawned"
        stamp = float(log.read_text().split()[0])
        assert t0 - 1 <= stamp <= time.time(), f"{argv[0]} {wrapper}: stamp {stamp}"
