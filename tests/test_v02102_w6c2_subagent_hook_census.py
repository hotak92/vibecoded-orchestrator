# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-C2 — the rest of the subagent hook census (F5-F9, F12, F13,
F15, L1-L3, L5), the W6-C1 residuals and the R1 review LOWs C1/C2.

Every behaviour is exercised through the shipped artefact (hook script,
router, drain, module) on BOTH OS flavours where the change has a .ps1
sibling. Every branch that drops / evicts / deletes something is tested for
both the act and the leave-alone case.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
HOOKS_LIB = HOOKS / "_lib"
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

from vco_lib.hook_agent_key import agent_state_key, sanitize_agent_id  # noqa: E402

# Imported lazily inside the tests that need them, so that against the
# pre-fix tree each test fails on its OWN assertion (red-proof), not on a
# module-level import error.


def _agent_alerts():
    from vco_lib import agent_alerts

    return agent_alerts


def _split(key: str):
    from vco_lib.hook_agent_key import split_agent_state_key

    return split_agent_state_key(key)

BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")
FLAVOURS = ["sh", "ps1"]


def _need(flavour: str) -> None:
    if flavour == "sh" and not BASH:
        pytest.skip("bash required")
    if flavour == "ps1" and not PWSH:
        pytest.skip("pwsh required")


def _env(project: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "VCT_DISABLE_HOOKS"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.update(extra)
    return env


def _run_hook(name: str, flavour: str, payload, project: Path, *,
              env: dict | None = None, hooks_dir: Path = HOOKS,
              timeout: int = 120) -> subprocess.CompletedProcess:
    _need(flavour)
    script = hooks_dir / f"{name}.{flavour}"
    argv = (["bash", str(script)] if flavour == "sh"
            else ["pwsh", *PWSH_BASE_FLAGS, "-File", str(script)])
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(argv, input=data, capture_output=True, text=True,
                          env=env if env is not None else _env(project),
                          cwd=str(project), timeout=timeout)


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    for d in (".claude/logs", ".claude/state", "knowledge", "docs", "src"):
        (p / d).mkdir(parents=True, exist_ok=True)
    (p / "knowledge" / "intro.md").write_text("# intro\n", encoding="utf-8")
    (p / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    return p


def _wait_for(pred, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


# Fake AWS key assembled at runtime (never a literal in the repo).
AWS_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"


# ── R1 C1: the per-agent key is unambiguous ────────────────────────────────

KEY_FIXTURES = [
    ("sess-1", "acb0a3ac69f7894db", "sess-1__acb0a3ac69f7894db"),  # real shape
    ("sess-1", "ag-2", "sess-1__ag-2"),
    ("sess-1", "", "sess-1"),
    # "_" is no longer allowed in the agent component -> its own bucket.
    ("sess-1", "ag_2", "sess-1__default"),
    # The review's collision pair now composes DIFFERENT keys.
    ("a__b", "c", "a__b__c"),
    ("a", "b__c", "a__default"),
    ("default", "ag", "default"),
    ("", "ag", ""),
]


class TestUnambiguousAgentKey:
    def test_python(self) -> None:
        for sid, ag, want in KEY_FIXTURES:
            assert agent_state_key(sid, ag) == want, (sid, ag)

    def test_collision_pair_is_distinct_and_decodes(self) -> None:
        k1 = agent_state_key("a__b", "c")
        k2 = agent_state_key("a", sanitize_agent_id("b__c"))
        assert k1 != k2
        assert _split(k1) == ("a__b", "c")
        assert _split("s_1___x9") == ("s_1_", "x9")
        assert _split("sess-1") == ("sess-1", "")

    def test_roundtrip_over_real_shapes(self) -> None:
        sid = "bf3ee8c2-5d1e-4c55-9a3b-0c2f6f0f9a11"  # UUID: no separator
        assert "__" not in sid
        for agent in ("acb0a3ac69f7894db", "a0a740982c6ade4b8", "x-1"):
            assert _split(agent_state_key(sid, agent)) == (sid, agent)

    @pytest.mark.skipif(not BASH, reason="bash required")
    def test_bash_leg(self, tmp_path: Path) -> None:
        lines = [f'. "{HOOKS_LIB}/session-id.sh"']
        for sid, ag, _ in KEY_FIXTURES:
            lines.append(f'vco_hook_agent_key_from_ids "{sid}" "{ag}"; echo "|"')
        r = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert [x.strip() for x in r.stdout.split("|")[:-1]] == [w for *_, w in KEY_FIXTURES]

    @pytest.mark.skipif(not PWSH, reason="pwsh required")
    def test_ps1_leg(self, tmp_path: Path) -> None:
        script = [f'. "{HOOKS_LIB}/session-id.ps1"']
        for sid, ag, _ in KEY_FIXTURES:
            script.append(f'Write-Output ("r=" + (Get-VcoAgentKeyFromIds -SessionId "{sid}" -AgentId "{ag}"))')
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", "\n".join(script)],
                           capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        got = [ln.strip()[2:] for ln in r.stdout.splitlines() if ln.strip().startswith("r=")]
        assert got == [w for *_, w in KEY_FIXTURES]


# ── C1 residual: the fast (no-JSON) per-agent key, sh/ps1 parity ───────────

FAST_PAYLOADS = [
    ('{"session_id": "s9", "agent_id": "lane7", "tool_name": "Read"}', "s9__lane7"),
    ('{"session_id": "s9", "tool_name": "Read"}', "s9"),
    ('{"session_id": "s9", "agent_id": "bad id"}', "s9__default"),
    ('{"agent_id": "lane7"}', ""),
]


class TestFastAgentKey:
    @pytest.mark.skipif(not BASH, reason="bash required")
    def test_bash(self, tmp_path: Path) -> None:
        lines = [f'. "{HOOKS_LIB}/session-id.sh"']
        for raw, _ in FAST_PAYLOADS:
            lines.append(f"vco_hook_fast_agent_key '{raw}'; echo '|'")
        r = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert [x.strip() for x in r.stdout.split("|")[:-1]] == [w for _, w in FAST_PAYLOADS]

    @pytest.mark.skipif(not PWSH, reason="pwsh required")
    def test_ps1(self, tmp_path: Path) -> None:
        script = [f'. "{HOOKS_LIB}/session-id.ps1"']
        for raw, _ in FAST_PAYLOADS:
            script.append(f"Write-Output (\"r=\" + (Get-VcoHookFastAgentKey -HookStdin '{raw}'))")
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", "\n".join(script)],
                           capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        got = [ln.strip()[2:] for ln in r.stdout.splitlines() if ln.strip().startswith("r=")]
        assert got == [w for _, w in FAST_PAYLOADS]


def _hooks_copy_without(tmp_path: Path, lib_names: list[str]) -> Path:
    """A copy of templates/hooks with some _lib files removed (a damaged
    install), so the wrappers' broken-install notice path runs."""
    dst = tmp_path / "hooks"
    shutil.copytree(HOOKS, dst)
    for name in lib_names:
        f = dst / "_lib" / name
        if f.exists():
            f.unlink()
    return dst


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("wrapper", ["read-context-inject", "grep-context-inject",
                                     "prompt-context-inject", "agent-brief-kg-inject"])
def test_wrapper_notice_sentinel_is_per_agent(tmp_path: Path, flavour: str, wrapper: str) -> None:
    """C1 residual (F16 class): the missing-lib notice's once-per-session
    sentinel carries the agent component for a subagent's call, so it can
    no longer consume the parent's sentinel (act); the main chat's sentinel
    name is unchanged (leave-alone)."""
    _need(flavour)
    project = _project(tmp_path)
    lib = "inject-budget.sh" if flavour == "sh" else "inject-budget.ps1"
    hooks = _hooks_copy_without(tmp_path, [lib])
    state = project / ".claude" / "state"
    sub = {"session_id": "sessW", "agent_id": "lane9", "tool_name": "Read",
           "hook_event_name": "PostToolUse", "prompt": "x",
           "tool_input": {"file_path": str(project / "src" / "main.py"), "pattern": "foo",
                          "prompt": "Task: x"}}
    # The wrappers resolve the venv + router before the notice; the router
    # itself is told to stand down (the kill switch it enforces).
    env = _env(project, VCT_VENV=str(Path(sys.executable).parent.parent),
               VCT_INSTALL_ROOT=str(REPO_ROOT), VCO_INJECT_PROFILE="off")
    _run_hook(wrapper, flavour, sub, project, env=env, hooks_dir=hooks)
    assert (state / "route_lib_missing_sessW__lane9_inject-budget").exists(), sorted(
        p.name for p in state.iterdir())
    assert not (state / "route_lib_missing_sessW_inject-budget").exists()
    main = dict(sub)
    main.pop("agent_id")
    _run_hook(wrapper, flavour, main, project, env=env, hooks_dir=hooks)
    assert (state / "route_lib_missing_sessW_inject-budget").exists()


# ── C1 residual: pre-edit replay filters through the PER-AGENT store ──────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_pre_edit_replay_uses_per_agent_store(tmp_path: Path, flavour: str) -> None:
    """A replay the parent already saw is suppressed for the PARENT
    (leave-alone of the dedupe) but still reaches a SUBAGENT editing the
    same file (act): the subagent has seen nothing."""
    _need(flavour)
    project = _project(tmp_path)
    target = str(project / "src" / "main.py")
    cache_dir = project / ".claude" / "state" / "edit_cache_sessE"
    cache_dir.mkdir(parents=True)
    blob = "KG: ReplayNode | concept | score=0.95\nREPLAYBODY marker\n"
    (cache_dir / hashlib.md5(target.encode()).hexdigest()).write_text(blob, encoding="utf-8")
    payload = {"session_id": "sessE", "tool_name": "Edit",
               "tool_input": {"file_path": target, "old_string": "x", "new_string": "y"}}
    env = _env(project, VCT_VENV=str(tmp_path / "no-venv"))
    r1 = _run_hook("pre-edit-context-inject", flavour, payload, project, env=env)
    assert "REPLAYBODY" in r1.stdout, (r1.stdout, r1.stderr)
    r2 = _run_hook("pre-edit-context-inject", flavour, payload, project, env=env)
    assert "REPLAYBODY" not in r2.stdout, "parent replay must stay deduped"
    sub = dict(payload, agent_id="lane3")
    r3 = _run_hook("pre-edit-context-inject", flavour, sub, project, env=env)
    assert "REPLAYBODY" in r3.stdout, (
        "a subagent's replay must not be suppressed by the parent's store")
    r4 = _run_hook("pre-edit-context-inject", flavour, sub, project, env=env)
    assert "REPLAYBODY" not in r4.stdout, "the subagent's own store dedupes it"


# ── R1 C2: warm_leg_* claim files are garbage-collected ───────────────────


def test_warm_leg_claims_swept_after_a_week(tmp_path: Path) -> None:
    import hook_context_router as router

    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    old = state / "warm_leg_0123456789abcdef"
    fresh = state / "warm_leg_fedcba9876543210"
    unrelated = state / "seen_inject_s.txt"
    for f in (old, fresh, unrelated):
        f.write_text("", encoding="utf-8")
    eight_days = time.time() - 8 * 24 * 3600
    os.utime(old, (eight_days, eight_days))
    os.utime(unrelated, (eight_days, eight_days))
    router._budget_gc(str(tmp_path))
    assert not old.exists(), "a week-old claim must be swept (act)"
    assert fresh.exists(), "a live claim must be kept (leave-alone)"
    assert unrelated.exists(), "the sweep touches only its own prefixes"


# ── F13: edit_outcome rows carry agent_id ─────────────────────────────────


def _fake_venv(tmp_path: Path, module_rel: str, body: str) -> Path:
    """A VCT_VENV whose python records what a hook hands a module."""
    fake = tmp_path / "fakepkgs"
    mod = fake / module_rel
    mod.parent.mkdir(parents=True, exist_ok=True)
    for parent in mod.relative_to(fake).parents:
        if str(parent) != ".":
            (fake / parent / "__init__.py").touch()
    mod.write_text(body, encoding="utf-8")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f'#!/bin/sh\nPYTHONPATH="{fake}" exec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return venv


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake venv")
@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("agent,want", [("acb0a3ac69f7894db", "acb0a3ac69f7894db"),
                                        ("", ""), ("bad id", "default")])
def test_edit_outcome_carries_agent_id(tmp_path: Path, flavour: str, agent: str, want: str) -> None:
    project = _project(tmp_path)
    out = tmp_path / "emitted.json"
    venv = _fake_venv(tmp_path, "claude_mcp_servers/rl_client/outcome_emit.py",
                      "import json\n"
                      "def emit_outcome_event(**kw):\n"
                      f"    open({str(out)!r}, 'w').write(json.dumps(kw))\n")
    payload = {"session_id": "sessO", "tool_name": "Edit",
               "tool_input": {"file_path": str(project / "src" / "main.py"),
                              "old_string": "x", "new_string": "yy"}}
    if agent:
        payload["agent_id"] = agent
    r = _run_hook("post-edit-outcome", flavour, payload, project,
                  env=_env(project, VCT_VENV=str(venv)))
    assert r.returncode == 0, r.stderr
    assert out.exists(), r.stderr
    assert json.loads(out.read_text())["payload"]["agent_id"] == want


# ── F15: kg-sync failure rows name the subagent ────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("agent", ["lane5", ""])
def test_kg_sync_failure_row_attributes_agent(tmp_path: Path, flavour: str, agent: str) -> None:
    _need(flavour)
    project = _project(tmp_path)
    metrics = tmp_path / "vct"
    env = _env(project, VCT_STATE_DIR=str(metrics), VCT_SESSION_ID="sessK")
    env.pop("VCT_AGENT_ID", None)
    env.pop("VCT_AGENT_TYPE", None)
    if agent:
        env.update(VCT_AGENT_ID=agent, VCT_AGENT_TYPE="expert-coder")
    if flavour == "sh":
        cmd = (f'. "{HOOKS_LIB}/kg-sync-debounce.sh"; '
               f'_kg_debounce_record_failure "{project}" kg 3 ""')
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    else:
        cmd = (f'. "{HOOKS_LIB}/kg-sync-debounce.ps1"; '
               f'Write-KgDebounceFailureRow -ProjectRoot "{project}" -Channel kg -ExitCode 3 -LogPath ""')
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", cmd], capture_output=True,
                           text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    rows = [json.loads(ln) for ln in (metrics / "metrics" / "kg_sync_failures.jsonl")
            .read_text().splitlines() if ln.strip()]
    assert rows[-1]["agent_id"] == agent
    assert rows[-1]["agent_type"] == ("expert-coder" if agent else "")


# ── F12: subagent citations are scored against the subagent's transcript ──


def _stage(project: Path, sid: str, task: str, agent: str | None, ts_ms: int | None = None,
           extra: dict | None = None) -> Path:
    from claude_mcp_servers.rl_client.citation_pending import stage_pending

    path = stage_pending(session_id=sid, task_id=task, seq=None, query="q",
                         ctx={"nodes": []}, source="hook", project_root=project,
                         agent_id=agent)
    assert path is not None
    if ts_ms is not None or extra:
        d = json.loads(path.read_text())
        if ts_ms is not None:
            d["ts_ms"] = ts_ms
        d.update(extra or {})
        path.write_text(json.dumps(d))
    return path


def _young() -> int:
    """A retrieval one minute old: inside the TTL, far below the terminal
    age — so any terminal-floor compute is the FINAL-window rule's doing."""
    return int(time.time() * 1000) - 60_000


def _transcript(path: Path, text: str) -> str:
    rows = [{"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


class TestAgentCitationDrain:
    def _drain(self, project, sid, tp, agent=""):
        import rl_drain_citations as drain

        computed: list = []

        def fake_compute(task_id, answer, ctx, write=True):
            computed.append((task_id, answer, ctx.get("fire_reason")))
            return {"ok": True}
        summary = drain.drain_session(sid, tp, agent_id=agent, project_root=str(project),
                                      compute_fn=fake_compute,
                                      token_count_fn=lambda t: 3000)
        return summary, computed

    def test_stage_tags_agent_from_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VCT_AGENT_ID", "lane4")
        p = _stage(tmp_path, "sessD", "t-env", None)
        assert json.loads(p.read_text())["agent_id"] == "lane4"
        monkeypatch.delenv("VCT_AGENT_ID")
        p2 = _stage(tmp_path, "sessD", "t-main", None)
        assert json.loads(p2.read_text())["agent_id"] == ""

    def test_parent_drain_never_scores_agent_file_with_parent_transcript(self, tmp_path: Path) -> None:
        f = _stage(tmp_path, "sessD", "t-agent", "lane4", ts_ms=_young())
        parent_tp = _transcript(tmp_path / "parent.jsonl", "parent answer " * 50)
        summary, computed = self._drain(tmp_path, "sessD", parent_tp)
        assert computed == [], "an agent's citation must never be scored on the parent's answer"
        assert f.exists() and summary["left"] == 1

    def test_agent_drain_scores_own_files_final(self, tmp_path: Path) -> None:
        mine = _stage(tmp_path, "sessD", "t-mine", "lane4", ts_ms=_young())
        main = _stage(tmp_path, "sessD", "t-main", "", ts_ms=_young())
        other = _stage(tmp_path, "sessD", "t-other", "lane8", ts_ms=_young())
        agent_tp = _transcript(tmp_path / "agent.jsonl", "subagent answer " * 50)
        summary, computed = self._drain(tmp_path, "sessD", agent_tp, agent="lane4")
        assert [c[0] for c in computed] == ["t-mine"]
        assert "subagent answer" in computed[0][1]
        # 3000 tokens < the 25k gate: a FINAL window takes the terminal floor.
        assert computed[0][2] == "terminal_floor"
        assert not mine.exists()
        assert main.exists() and other.exists(), "other agents'/main files untouched"
        # Leave-alone: the PARENT drain keeps accumulate-don't-drop for a
        # young main-chat file (below gate, not final -> left, not computed).
        parent_tp = _transcript(tmp_path / "parent.jsonl", "parent answer " * 50)
        _summary, computed2 = self._drain(tmp_path, "sessD", parent_tp)
        assert [c[0] for c in computed2] == [] and main.exists()

    def test_agent_drain_stamps_left_file_and_parent_retries_it(self, tmp_path: Path) -> None:
        f = _stage(tmp_path, "sessD", "t-late", "lane4", ts_ms=_young())
        agent_tp = tmp_path / "agent.jsonl"  # not flushed yet: no messages
        agent_tp.write_text("", encoding="utf-8")
        summary, computed = self._drain(tmp_path, "sessD", str(agent_tp), agent="lane4")
        assert computed == [] and f.exists()
        assert json.loads(f.read_text())["agent_transcript_path"] == str(agent_tp)
        _transcript(agent_tp, "late subagent answer " * 40)
        parent_tp = _transcript(tmp_path / "parent.jsonl", "parent answer " * 50)
        summary, computed = self._drain(tmp_path, "sessD", parent_tp)
        assert [c[0] for c in computed] == ["t-late"]
        assert "late subagent answer" in computed[0][1]



@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake venv")
@pytest.mark.parametrize("flavour", FLAVOURS)
def test_stop_drain_hook_on_subagent_stop_uses_agent_transcript(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    project = _project(tmp_path)
    argv_log = tmp_path / "argv.json"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    # A real python for everything (the hook's own JSON parse may resolve to
    # it), except the drain run, whose argv it records.
    py.write_text(
        "#!/bin/sh\n"
        'case "$1" in *rl_drain_citations.py)\n'
        f'  exec "{sys.executable}" -c \'import json,sys; '
        f'open("{argv_log}","w").write(json.dumps(sys.argv[1:]))\' "$@" ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    payload = {"hook_event_name": "SubagentStop", "session_id": "sessS",
               "transcript_path": "/parent.jsonl", "agent_id": "lane2",
               "agent_transcript_path": "/agent-lane2.jsonl"}
    r = _run_hook("stop-drain-citations", flavour, payload, project,
                  env=_env(project, VCT_VENV=str(venv)))
    assert r.returncode == 0, r.stderr
    assert _wait_for(argv_log.exists, 30), "the drain was never spawned"
    argv = json.loads(argv_log.read_text())
    assert argv[argv.index("--transcript-path") + 1] == "/agent-lane2.jsonl"
    assert argv[argv.index("--agent-id") + 1] == "lane2"
    # No agent transcript -> nothing is drained (never the parent's).
    argv_log.unlink()
    payload.pop("agent_transcript_path")
    _run_hook("stop-drain-citations", flavour, payload, project,
              env=_env(project, VCT_VENV=str(venv)))
    time.sleep(2)
    assert not argv_log.exists()


# ── F8: alerts a subagent hits reach the parent ────────────────────────────


class TestAgentAlertsModule:
    def test_main_chat_files_nothing(self, tmp_path: Path) -> None:
        assert _agent_alerts().append_alert(str(tmp_path), "s1", "", "", "credential", "m") is None

    def test_foreground_delivery_by_agent_id(self, tmp_path: Path) -> None:
        _agent_alerts().append_alert(str(tmp_path), "s1", "lane1", "coder", "credential",
                                  "possible credential in a.py line 3")
        _agent_alerts().append_alert(str(tmp_path), "s1", "lane2", "coder", "credential", "other")
        out = _agent_alerts().deliver_for_payload(str(tmp_path), {
            "hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "s1",
            "tool_response": {"agentId": "lane1", "content": "report"}})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "a.py line 3" in ctx and "other" not in ctx
        state = tmp_path / ".claude" / "state"
        assert not (state / "agent_alerts_s1__lane1.jsonl").exists(), "delivered -> deleted"
        assert (state / "agent_alerts_s1__lane2.jsonl").exists(), "undelivered kept"
        # Delivered once: a second delivery finds nothing.
        assert _agent_alerts().deliver_for_payload(str(tmp_path), {
            "hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "s1",
            "tool_response": {"agentId": "lane1"}}) == ""

    def test_prompt_sweep_takes_only_this_session(self, tmp_path: Path) -> None:
        _agent_alerts().append_alert(str(tmp_path), "s1", "lane1", "c", "worktree_kept", "kept A")
        _agent_alerts().append_alert(str(tmp_path), "s2", "lane3", "c", "worktree_kept", "kept B")
        out = _agent_alerts().deliver_for_payload(str(tmp_path), {
            "hook_event_name": "UserPromptSubmit", "session_id": "s1",
            "prompt": "<task-notification>done</task-notification>"})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "kept A" in ctx and "kept B" not in ctx
        assert (tmp_path / ".claude" / "state" / "agent_alerts_s2__lane3.jsonl").exists()

    def test_background_launch_without_agent_id_delivers_nothing(self, tmp_path: Path) -> None:
        _agent_alerts().append_alert(str(tmp_path), "s1", "lane1", "c", "credential", "x")
        assert _agent_alerts().deliver_for_payload(str(tmp_path), {
            "hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "s1",
            "tool_response": {"status": "async_launched"}}) == ""

    @pytest.mark.parametrize("flavour", FLAVOURS)
    def test_shell_path_helpers_match_python(self, tmp_path: Path, flavour: str) -> None:
        _need(flavour)
        cases = [("s1", "lane1"), ("default", "lane1"), ("s1", ""), ("s1", "bad id")]
        want = [str(_agent_alerts().alert_file(str(tmp_path), s, a) or "") for s, a in cases]
        if flavour == "sh":
            lines = [f'. "{HOOKS_LIB}/session-id.sh"']
            lines += [f'vco_agent_alerts_file "{tmp_path}" "{s}" "{a}"; echo "|"' for s, a in cases]
            r = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True)
            got = [x.strip() for x in r.stdout.split("|")[:-1]]
        else:
            lines = [f'. "{HOOKS_LIB}/session-id.ps1"']
            lines += [f'Write-Output ("r=" + (Get-VcoAgentAlertsFile -ProjectRoot "{tmp_path}" '
                      f'-SessionId "{s}" -AgentId "{a}"))' for s, a in cases]
            r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command", "\n".join(lines)],
                               capture_output=True, text=True, timeout=120)
            got = [ln.strip()[2:] for ln in r.stdout.splitlines() if ln.strip().startswith("r=")]
        assert got == want, r.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_deliver_hook_end_to_end(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    project = _project(tmp_path)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hi"}
    env = _env(project, VCT_VENV=str(Path(sys.executable).parent.parent),
               PYTHONPATH=str(REPO_ROOT))
    # Nothing pending: silent.
    r0 = _run_hook("agent-alerts-deliver", flavour, payload, project, env=env)
    assert r0.returncode == 0 and r0.stdout.strip() == ""
    _agent_alerts().append_alert(str(project), "s1", "lane1", "coder", "worktree_kept",
                              "worktree KEPT at /x (has_changes)")
    r1 = _run_hook("agent-alerts-deliver", flavour, payload, project, env=env)
    assert r1.returncode == 0, r1.stderr
    ctx = json.loads(r1.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "worktree KEPT at /x" in ctx


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_post_tool_security_files_subagent_alert_for_parent(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    project = _project(tmp_path)
    target = project / "src" / "cfg.py"
    target.write_text(f'KEY = "{AWS_KEY}"\n', encoding="utf-8")
    payload = {"session_id": "sessP", "tool_name": "Write", "hook_event_name": "PostToolUse",
               "tool_input": {"file_path": str(target), "content": f'KEY = "{AWS_KEY}"\n'}}
    state = project / ".claude" / "state"
    # Leave-alone first: the main chat already sees its own alert.
    r = _run_hook("post-tool-security", flavour, payload, project)
    assert r.returncode == 0, r.stderr
    assert list(state.glob("agent_alerts_*.jsonl")) == []
    # Act: a subagent's edit is filed for the parent.
    r = _run_hook("post-tool-security", flavour,
                  dict(payload, agent_id="lane6", agent_type="coder"), project)
    assert r.returncode == 0, r.stderr
    files = list(state.glob("agent_alerts_*.jsonl"))
    assert [f.name for f in files] == ["agent_alerts_sessP__lane6.jsonl"], r.stderr
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["kind"] == "credential" and "cfg.py" in row["message"]
    assert AWS_KEY not in files[0].read_text(), "never the value"


# ── F6/L1 + F7 + F5: the reconciler ────────────────────────────────────────


def _start_and_stop(project: Path, flavour: str, payload: dict, mutate, env: dict | None = None):
    _run_hook("subagent-start-kg-inject", flavour, payload, project)
    mutate()
    return _run_hook("subagent-stop-reconcile", flavour, payload, project, env=env)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_reconcile_runs_detached(tmp_path: Path, flavour: str) -> None:
    """L1/F6 (act): without the inline switch the hook returns after the
    audit row and ONE detached job does the work."""
    project = _project(tmp_path)
    payload = {"session_id": "sessR", "agent_id": "lane1", "agent_type": "coder"}
    env = _env(project)
    env.pop("VCO_SUBAGENT_RECONCILE_INLINE", None)
    r = _start_and_stop(project, flavour, payload,
                        lambda: (project / "src" / "main.py").write_text(f"k = '{AWS_KEY}'\n"),
                        env=env)
    assert r.returncode == 0, r.stderr
    audit = project / ".claude" / "logs" / "subagent-reconciliation.jsonl"
    assert audit.exists(), "the audit row is written in the foreground"
    files_log = project / ".claude" / "logs" / "subagent-reconcile-files.jsonl"
    assert _wait_for(lambda: files_log.exists() and
                     not list((project / ".claude" / "state").glob("subagent-snapshot-*")), 60)
    row = json.loads(files_log.read_text().splitlines()[-1])
    assert row["attribution"] == "unattributed_shared_tree"
    assert (project / ".claude" / "state" / "agent_alerts_sessR__lane1.jsonl").exists()
    assert len(audit.read_text().splitlines()) == 1, "the job writes no second audit row"
    assert not list((project / ".claude" / "state").glob("subagent-reconcile-job.*"))


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_reconcile_job_file_gc(tmp_path: Path, flavour: str) -> None:
    """A day-old orphan job file is swept (act); a fresh one may still be
    about to be read and is kept (leave-alone). No agent id: no job."""
    project = _project(tmp_path)
    state = project / ".claude" / "state"
    old = state / "subagent-reconcile-job.x.1.json"
    young = state / "subagent-reconcile-job.y.2.json"
    old.write_text("{}")
    young.write_text("{}")
    two_days = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days, two_days))
    env = _env(project)
    env.pop("VCO_SUBAGENT_RECONCILE_INLINE", None)
    r = _run_hook("subagent-stop-reconcile", flavour,
                  {"session_id": "sessR"}, project, env=env)
    assert r.returncode == 0
    assert old.exists() and young.exists(), "no agent id: nothing spawned, nothing swept"
    r = _run_hook("subagent-stop-reconcile", flavour,
                  {"session_id": "sessR", "agent_id": "lane2"}, project, env=env)
    assert r.returncode == 0
    assert not old.exists()
    assert young.exists()


def _worktree_project(tmp_path: Path, flavour: str) -> tuple[Path, Path]:
    if not GIT:
        pytest.skip("git required")
    project = _project(tmp_path)
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")
    base = _git(project, "rev-parse", "HEAD")
    wt = project / ".claude" / "worktrees" / "agent-lane7"
    _git(project, "worktree", "add", "-q", "--detach", str(wt), base)
    rec_dir = project / ".claude" / "state" / "worktree-guard"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "wt-agent-lane7.json").write_text(json.dumps({
        "name": "agent-lane7", "mode": "head", "path": str(wt), "base": base,
        "toplevel": str(project), "status": "created", "created_at": "2026-10-13T00:00:00Z"}))
    return project, wt


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_isolated_agent_reconciled_from_its_worktree(tmp_path: Path, flavour: str) -> None:
    """F7: an isolated agent's changes come from ITS worktree (exact), the
    shared-tree snapshot is skipped at start (L2), and a credential it wrote
    there is found and filed for the parent."""
    _need(flavour)
    project, wt = _worktree_project(tmp_path, flavour)
    payload = {"session_id": "sessI", "agent_id": "lane7", "agent_type": "coder"}
    _run_hook("subagent-start-kg-inject", flavour, payload, project)
    assert not list((project / ".claude" / "state").glob("subagent-snapshot-*")), (
        "an isolated agent needs no shared-tree snapshot")
    (wt / "src" / "leak.py").write_text(f"k = '{AWS_KEY}'\n")
    # A parent edit in the shared tree meanwhile must NOT be attributed.
    (project / "src" / "main.py").write_text("x = 2\n")
    r = _run_hook("subagent-stop-reconcile", flavour, payload, project)
    assert r.returncode == 0, r.stderr
    row = json.loads((project / ".claude" / "logs" / "subagent-reconcile-files.jsonl")
                     .read_text().splitlines()[-1])
    assert row["attribution"] == "agent_worktree"
    assert any(f.endswith("src/leak.py") for f in row["changed_files"])
    assert not any(f.endswith("src/main.py") for f in row["changed_files"])
    alerts = (project / ".claude" / "state" / "agent_alerts_sessI__lane7.jsonl").read_text()
    assert "leak.py" in alerts and "worktree_kept" in alerts
    # L2 leave-alone: an agent WITHOUT its own worktree keeps the snapshot.
    _run_hook("subagent-start-kg-inject", flavour,
              {"session_id": "sessI", "agent_id": "lane8"}, project)
    assert (project / ".claude" / "state" / "subagent-snapshot-lane8.json").exists()


@pytest.mark.skipif(not BASH or sys.platform == "win32", reason="flock: POSIX")
def test_work_record_waits_for_the_nudge_lock(tmp_path: Path) -> None:
    """F5: Step 5 holds the nudge's sidecar flock — while another holder has
    it, the counter row is not rewritten; it lands once the lock is free."""
    import fcntl

    project = _project(tmp_path)
    vct = tmp_path / "vct"
    (vct / "metrics").mkdir(parents=True)
    counter = vct / "metrics" / "kg_update_tokens.jsonl"
    counter.write_text(json.dumps({"session_id": "sessL", "baseline": 7}) + "\n")
    env = _env(project, VCT_STATE_DIR=str(vct))
    payload = {"session_id": "sessL", "agent_id": "lane1"}
    _run_hook("subagent-start-kg-inject", "sh", payload, project, env=env)
    (project / "src" / "main.py").write_text("x = 3\n")
    fd = os.open(str(counter) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    proc = subprocess.Popen(["bash", str(HOOKS / "subagent-stop-reconcile.sh")],
                            stdin=subprocess.PIPE, env=env, text=True)
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()
    time.sleep(2.0)
    held = json.loads(counter.read_text().splitlines()[-1])
    assert "subagent_work_units" not in held, "written while the nudge held the lock"
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    assert proc.wait(timeout=60) == 0
    row = json.loads(counter.read_text().splitlines()[-1])
    assert row["subagent_work_units"] > 0 and row["baseline"] == 7


# ── F9: suggestions from the brief; the dead SubagentStart hook retired ───


def _suggest_project(tmp_path: Path, tools_line: str) -> Path:
    project = _project(tmp_path)
    skill = project / ".claude" / "skills" / "zebra-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: zebra-skill\ndescription: Zebra things.\nkeywords: [zebrafoo]\n---\nbody\n")
    agents = project / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "zebra-agent.md").write_text(
        "---\nname: zebra-agent\ndescription: Zebra agent.\nkeywords: [zebrafoo]\n---\nbody\n")
    (agents / "worker.md").write_text(
        f"---\nname: worker\ndescription: w\n{tools_line}---\nbody\n")
    return project


def _router(project: Path, payload: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    from tests.common.child_env import child_env

    stub = tmp_path / "kg_stub.py"
    stub.write_text("import sys\ndef main():\n    sys.stdout.write('')\n")
    env = child_env(CLAUDE_PROJECT_DIR=str(project), VCO_ROUTER_KG_SCRIPT=str(stub),
                    VCO_CG_SCRIPT=str(stub))
    env.pop("VCO_INJECT_PROFILE", None)
    return subprocess.run([sys.executable, str(ROUTER), "agent"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=120, cwd=str(project), env=env)


@pytest.mark.parametrize("tools_line,want_agent", [
    ("", True),                       # no tools: -> inherits Agent
    ("tools: Read, Agent\n", True),
    ("tools: Read, Edit\n", False),   # cannot spawn -> skills only
])
def test_agent_brief_carries_keyword_suggestions(tmp_path: Path, tools_line: str, want_agent: bool) -> None:
    project = _suggest_project(tmp_path, tools_line)
    payload = {"session_id": "sessG", "prompt_id": "p1", "cwd": str(project), "tool_name": "Agent",
               "tool_input": {"prompt": "please handle the zebrafoo migration",
                              "subagent_type": "worker", "description": "d"}}
    r = _router(project, payload, tmp_path)
    assert r.returncode == 0, r.stderr
    updated = json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]
    assert updated["subagent_type"] == "worker" and updated["description"] == "d"
    assert updated["prompt"].startswith("please handle the zebrafoo migration")
    assert "zebra-skill" in updated["prompt"]
    assert ("zebra-agent" in updated["prompt"]) is want_agent
    assert "permissionDecision" not in r.stdout
    # Leave-alone: a brief that matches nothing is not rewritten at all.
    quiet = dict(payload, tool_input={"prompt": "nothing relevant here",
                                      "subagent_type": "worker"})
    r2 = _router(project, quiet, tmp_path)
    assert r2.returncode == 0 and r2.stdout.strip() == ""


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_suggest_hook_retired_and_upgrades_scrub_it(os_name: str) -> None:
    from vco_lib.hook_retirements import match_retired_registration

    ext = "sh" if os_name == "linux" else "ps1"
    text = TEMPLATES[os_name].read_text(encoding="utf-8")
    assert "subagent-start-suggest" not in text
    assert not (HOOKS / f"subagent-start-suggest.{ext}").exists()
    cmd = ('bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/subagent-start-suggest.sh"'
           if ext == "sh" else
           'powershell -NoProfile -ExecutionPolicy Bypass -File '
           '"${CLAUDE_PROJECT_DIR}/.claude/hooks/subagent-start-suggest.ps1"')
    assert match_retired_registration("SubagentStart", cmd,
                                      hook_identity=f"subagent-start-suggest.{ext}") is not None


# ── registrations: the drain on SubagentStop, delivery, no killing timeouts ─


def _hooks_for(doc: dict, event: str, stem: str) -> list[tuple[str, dict]]:
    return [(g.get("matcher", ""), h) for g in doc["hooks"].get(event, [])
            for h in g["hooks"] if stem in h.get("command", "")]


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_registrations(os_name: str) -> None:
    doc = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    ((_m, drain),) = _hooks_for(doc, "SubagentStop", "stop-drain-citations")
    assert drain.get("async") is True
    ((m, deliver),) = _hooks_for(doc, "PostToolUse", "agent-alerts-deliver")
    assert m == "Agent|Task"
    assert len(_hooks_for(doc, "UserPromptSubmit", "agent-alerts-deliver")) == 1
    for event, stem in (("SubagentStop", "subagent-stop-reconcile"),
                        ("SubagentStart", "subagent-start-kg-inject"),
                        ("PreCompact", "pre-compact-save"),
                        ("WorktreeCreate", "worktree-guard")):  # L3: confirmed
        ((_m, h),) = _hooks_for(doc, event, stem)
        assert "timeout" not in h, f"{event}/{stem} must not carry a killing timeout"


# ── PreCompact for a subagent leaves the main chat's snapshot alone ───────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_pre_compact_save_ignores_subagent_compaction(tmp_path: Path, flavour: str) -> None:
    project = _project(tmp_path)
    (project / ".claude" / "CONTEXT_STATE.md").write_text("# ctx\n")
    snap = project / ".claude" / "context" / "pre-compact-snapshot.md"
    r = _run_hook("pre-compact-save", flavour,
                  {"session_id": "sessC", "trigger": "auto", "agent_id": "lane1"}, project)
    assert r.returncode == 0, r.stderr
    assert not snap.exists(), "a subagent's compaction must not write the main snapshot"
    r = _run_hook("pre-compact-save", flavour, {"session_id": "sessC", "trigger": "auto"}, project)
    assert r.returncode == 0, r.stderr
    assert snap.exists(), "the main chat's compaction still saves it"
