# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-C1 — per-agent keying of hook state (census F1-F4, F10, F11,
F14, F16, F17 + L4).

RED on the base tree: `vco_lib/hook_agent_key.py`, the `_lib/session-id.*`
agent-key helpers, the router's per-agent keying / agent-surface exemptions /
`--warm-legs` warmer, `SUBAGENT_PER_TURN_BUDGET_CHARS`, the nudge's
agent_id early exit, the `<task-notification>` skips and the per-agent
sentinel keys are all absent.

Harness fact everything here rests on (probe 2026-10-13): subagent tool
events carry ``agent_id`` while ``session_id`` / ``prompt_id`` /
``transcript_path`` stay the PARENT'S, so state keyed on session_id alone
was shared by the main chat and every subagent.
"""
from __future__ import annotations

import json
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

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

from vco_lib.hook_agent_key import agent_state_key, sanitize_agent_id  # noqa: E402
from vco_lib.inject_intent import (  # noqa: E402
    PER_TURN_BUDGET_CHARS,
    SUBAGENT_PER_TURN_BUDGET_CHARS,
    prompt_query,
    sanitize_session_id,
    turn_budget_chars,
)


def _has_bash() -> bool:
    return shutil.which("bash") is not None


def _has_pwsh() -> bool:
    return shutil.which("pwsh") is not None


# --- F1/F2/F3/F11: the ONE key helper, parity across the three legs ------------

#: Shared fixtures: (session_id, agent_id, expected key). The Python leg is
#: driven with a PRE-SANITISED session id (the router sanitises before
#: composing — that ordering is part of the contract), so the shell/ps1
#: legs' internal re-sanitise must be a no-op on these inputs.
KEY_FIXTURES = [
    ("sess-1", "ag-2", "sess-1__ag-2"),
    # v0.2.102 W6-C2 (R1 C1): "_" is outside the agent charset -> own bucket.
    ("sess-1", "ag_2", "sess-1__default"),
    ("sess-1", "", "sess-1"),
    ("sess-1", "hostile/id!", "sess-1__default"),
    # Untrustworthy session id passes through UNCHANGED (never a shared
    # default__<agent> bucket across sessions).
    ("default", "ag", "default"),
    ("", "ag", ""),
]


class TestAgentKeyParity:
    def test_python_leg(self) -> None:
        for sid, ag, want in KEY_FIXTURES:
            assert agent_state_key(sid, ag) == want, (sid, ag)

    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="bash .sh leg; Windows runs the .ps1 sibling: "
        "tests/test_v02102_w6c1_agent_keyed_hook_state.py::TestAgentKeyParity::test_ps1_leg")
    def test_bash_leg(self, tmp_path: Path) -> None:
        py = shutil.which("python3") or "python3"
        lines = [f'. "{HOOKS_LIB}/session-id.sh"']
        # The helper prints no trailing newline, so a "|" separator after
        # each call keeps the outputs (including an EMPTY one) splittable.
        for sid, ag, _ in KEY_FIXTURES:
            lines.append(f'vco_hook_agent_key_from_ids "{sid}" "{ag}"; echo "|"')
        r = subprocess.run(["bash", "-c", "\n".join(lines)],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path),
                           env={"PATH": "/usr/bin:/bin", "PY": py})
        assert r.returncode == 0, r.stderr
        got = [ln.strip() for ln in r.stdout.split("|")[:-1]]
        assert got == [want for _, _, want in KEY_FIXTURES]

    @pytest.mark.skipif(not _has_pwsh(), reason="pwsh required")
    def test_ps1_leg(self, tmp_path: Path) -> None:
        script = [f'. "{HOOKS_LIB}/session-id.ps1"']
        # The "r=" prefix makes an EMPTY result visible as a line
        # (PowerShell drops bare empty-string output).
        for sid, ag, _ in KEY_FIXTURES:
            script.append(
                f'Write-Output ("r=" + (Get-VcoAgentKeyFromIds '
                f'-SessionId "{sid}" -AgentId "{ag}"))')
        r = subprocess.run(["pwsh", *PWSH_BASE_FLAGS, "-Command",
                            "\n".join(script)],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        got = [ln.strip()[2:] for ln in r.stdout.splitlines()
               if ln.strip().startswith("r=")]
        assert got == [want for _, _, want in KEY_FIXTURES]

    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_payload_parse_agrees_with_python(self, tmp_path: Path) -> None:
        """The bash payload-parsing helper (used by post-compact and the
        retrieval recorder) must agree with the Python composition over the
        SAME payload — including the main-agent passthrough."""
        py = shutil.which("python3") or "python3"
        payloads = [
            ('{"session_id": "s9", "agent_id": "lane7"}', "s9__lane7"),
            ('{"session_id": "s9"}', "s9"),
            ('{"session_id": "bad id", "agent_id": "lane7"}', "default"),
            ("not json", ""),
        ]
        lines = [f'. "{HOOKS_LIB}/session-id.sh"']
        for raw, _ in payloads:
            lines.append(f"vco_hook_agent_key '{raw}'; echo '|'")
        r = subprocess.run(["bash", "-c", "\n".join(lines)],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path),
                           env={"PATH": "/usr/bin:/bin", "PY": py})
        assert r.returncode == 0, r.stderr
        got = [ln.strip() for ln in r.stdout.split("|")[:-1]]
        want = []
        for raw, expected in payloads:
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                d = {}
            want.append(agent_state_key(
                sanitize_session_id(str(d.get("session_id") or "")),
                str(d.get("agent_id") or "")) or expected)
        assert got == want


# --- F2: budget caps (6 000 main / 20 000 subagent), one home ------------------


class TestBudgetCaps:
    def test_constants(self) -> None:
        assert PER_TURN_BUDGET_CHARS == 6000
        assert SUBAGENT_PER_TURN_BUDGET_CHARS == 20000

    def test_resolver(self) -> None:
        assert turn_budget_chars(False) == PER_TURN_BUDGET_CHARS
        assert turn_budget_chars(True) == SUBAGENT_PER_TURN_BUDGET_CHARS

    def test_agent_id_sanitisation(self) -> None:
        assert sanitize_agent_id("lane-1X") == "lane-1X"
        # v0.2.102 W6-C2 (R1 C1): "_" is outside the agent charset, so the
        # composite <sid>__<agent> key decodes uniquely.
        assert sanitize_agent_id("lane-1_X") == "default"
        assert sanitize_agent_id("") == ""
        assert sanitize_agent_id("bad/id") == "default"


# --- Router keying: the stub-producer harness ----------------------------------


def _stub_producer(tmp_path: Path, name: str, body: str,
                   sleep_s: float = 0.0,
                   gate_env: str = "") -> Path:
    """A producer-stub shaped like the real ones: a main() whose stdout the
    leg runner captures (module-level side effects would fire at LOAD time
    in the router's main thread, corrupting its stdout).

    ``gate_env`` (v0.2.103 de-flake): when set, main() blocks until the
    file named by that env var exists — an EVENT-gated "slow" producer, so
    a leg's miss is caused by the gate's absence, never by a sleep racing
    the join deadline under machine load."""
    stub = tmp_path / name
    stub.write_text(
        "import os, sys, time\n"
        "def main():\n"
        + (f"    time.sleep({sleep_s})\n" if sleep_s else "")
        + (f"    _gate = os.environ.get({gate_env!r})\n"
           f"    if _gate:\n"
           f"        _end = time.monotonic() + 300\n"
           f"        while not os.path.exists(_gate) and time.monotonic() < _end:\n"
           f"            time.sleep(0.02)\n" if gate_env else "")
        + f"    sys.stdout.write({body!r})\n",
        encoding="utf-8")
    return stub


_KG_BLOCK = "KG: W6C1Node | concept | score=0.95\nBODYMARKER w6c1 content\n\n"


def _run_router(stdin_payload: dict, tmp_path: Path, surface: str,
                kg_stub: Path, extra_env: dict | None = None,
                ) -> subprocess.CompletedProcess:
    from tests.common.child_env import child_env

    env = child_env(
        CLAUDE_PROJECT_DIR=str(tmp_path),
        VCO_ROUTER_KG_SCRIPT=str(kg_stub),
        VCO_CG_SCRIPT=str(kg_stub),
    )
    env.pop("VCO_INJECT_PROFILE", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(ROUTER), surface],
        input=json.dumps(stdin_payload), capture_output=True, text=True,
        timeout=120, cwd=str(tmp_path), env=env)


def _prompt_payload(sid: str, pid: str, agent: str, salt: str) -> dict:
    p = {
        "session_id": sid, "prompt_id": pid, "cwd": "/tmp",
        "prompt": (f"explain the agent-keyed hook state design and its "
                    f"seen-store budget and cap file semantics in detail "
                    f"{salt}"),
    }
    if agent:
        p["agent_id"] = agent
    return p


class TestRouterPerAgentState:
    """F1/F2/F3 end-to-end: a subagent payload gets its OWN seen-store and
    budget files; the main chat's stay untouched — in both directions."""

    def test_subagent_gets_own_store_and_budget(self, tmp_path: Path) -> None:
        stub = _stub_producer(tmp_path, "kg_stub.py", _KG_BLOCK)
        r = _run_router(_prompt_payload("sessA", "p1", "lane1", "one"),
                        tmp_path, "prompt", stub)
        assert r.returncode == 0, r.stderr
        assert "KG: W6C1Node" in r.stdout
        state = tmp_path / ".claude" / "state"
        assert (state / "seen_inject_sessA__lane1.txt").exists()
        assert not (state / "seen_inject_sessA.txt").exists()
        assert (state / "inject_budget_sessA__lane1_p1").exists()
        assert not (state / "inject_budget_sessA_p1").exists()

    def test_main_not_suppressed_by_subagent(self, tmp_path: Path) -> None:
        """Direction 1: the subagent's injection must not silence the main
        chat (which never saw it)."""
        stub = _stub_producer(tmp_path, "kg_stub.py", _KG_BLOCK)
        r1 = _run_router(_prompt_payload("sessB", "p1", "lane1", "a"),
                         tmp_path, "prompt", stub)
        assert "KG: W6C1Node" in r1.stdout
        r2 = _run_router(_prompt_payload("sessB", "p2", "", "b"),
                         tmp_path, "prompt", stub)
        assert r2.returncode == 0, r2.stderr
        assert "KG: W6C1Node" in r2.stdout, (
            "the main chat was suppressed by a subagent's seen-store entry")

    def test_subagent_not_suppressed_by_main(self, tmp_path: Path) -> None:
        """Direction 2: the main chat's injection must not silence a fresh
        subagent."""
        stub = _stub_producer(tmp_path, "kg_stub.py", _KG_BLOCK)
        r1 = _run_router(_prompt_payload("sessC", "p1", "", "a"),
                         tmp_path, "prompt", stub)
        assert "KG: W6C1Node" in r1.stdout
        r2 = _run_router(_prompt_payload("sessC", "p2", "lane2", "b"),
                         tmp_path, "prompt", stub)
        assert r2.returncode == 0, r2.stderr
        assert "KG: W6C1Node" in r2.stdout, (
            "a fresh subagent was suppressed by the main chat's seen-store")

    def test_dedupe_still_works_within_one_agent(self, tmp_path: Path) -> None:
        stub = _stub_producer(tmp_path, "kg_stub.py", _KG_BLOCK)
        r1 = _run_router(_prompt_payload("sessD", "p1", "", "a"),
                         tmp_path, "prompt", stub)
        assert "KG: W6C1Node" in r1.stdout
        # Same agent, new turn (new pid → new cache key → producer reruns):
        # the seen-store now suppresses the identical block.
        r2 = _run_router(_prompt_payload("sessD", "p2", "", "a"),
                         tmp_path, "prompt", stub)
        assert r2.returncode == 0, r2.stderr
        assert "KG: W6C1Node" not in r2.stdout

    def test_main_budget_degrades_subagent_does_not(self, tmp_path: Path) -> None:
        """F2 behaviourally: with 5 990 chars already spent, the MAIN chat
        degrades to titles one-liners (6 000 cap) while a subagent run at
        the same spend still gets full bodies (20 000 cap)."""
        stub = _stub_producer(tmp_path, "kg_stub2.py", _KG_BLOCK)
        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "inject_budget_sessE_pmain").write_text("5990")
        (state / "inject_budget_sessE__lane_psub").write_text("5990")
        r_main = _run_router(_prompt_payload("sessE", "pmain", "", "x"),
                             tmp_path, "prompt", stub)
        assert "TITLES" in r_main.stdout, (
            "the main chat must degrade past its 6 000-char budget")
        r_sub = _run_router(_prompt_payload("sessE", "psub", "lane", "x"),
                            tmp_path, "prompt", stub)
        assert "BODYMARKER" in r_sub.stdout and "TITLES" not in r_sub.stdout, (
            "a subagent run at 5 990 chars is NOT past its 20 000-char cap")


# --- F10: the agent surface skips dedupe and the turn budget -------------------


class TestAgentSurfaceNoDedupe:
    def _agent_payload(self, sid: str, pid: str) -> dict:
        return {
            "session_id": sid, "prompt_id": pid, "cwd": "/tmp",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Task: review the w6c1 lane report\nmodel: opus",
                "model": "opus",
            },
        }

    def test_brief_not_deduped_no_store_no_budget(self, tmp_path: Path) -> None:
        stub = _stub_producer(tmp_path, "kg_agent.py", _KG_BLOCK)
        r1 = _run_router(self._agent_payload("sessF", "p1"), tmp_path,
                         "agent", stub)
        assert r1.returncode == 0, r1.stderr
        envelope = json.loads(r1.stdout)
        injected = envelope["hookSpecificOutput"]["updatedInput"]["prompt"]
        assert "KG: W6C1Node" in injected
        # The SAME brief again (cache hit): must STILL be appended — the
        # brief's reader has seen nothing, and no store recorded it.
        r2 = _run_router(self._agent_payload("sessF", "p1"), tmp_path,
                         "agent", stub)
        assert r2.returncode == 0, r2.stderr
        injected2 = json.loads(r2.stdout)["hookSpecificOutput"][
            "updatedInput"]["prompt"]
        assert "KG: W6C1Node" in injected2, (
            "the agent surface must not be deduped by the seen-store")
        state = tmp_path / ".claude" / "state"
        assert not (state / "seen_inject_sessF.txt").exists()
        assert not any(state.glob("inject_budget_sessF*")), (
            "the agent surface must not charge the turn budget")


# --- F14: the synthetic task-notification prompt is skipped ---------------------


class TestTaskNotificationSkipped:
    def test_prompt_query_skips_notification(self) -> None:
        notification = (
            "<task-notification>Background task 4a2f completed: the lane "
            "report follows with many words of subagent prose that would "
            "otherwise become a KG query</task-notification>")
        assert len(notification) > 100  # past the min-chars gate
        assert prompt_query(notification) == ""
        assert prompt_query("/compact focus on the report") == ""

    def test_router_prompt_surface_skips_notification(self, tmp_path: Path) -> None:
        """The router must not spawn ANY producer for the synthetic prompt."""
        stub = tmp_path / "tripwire_kg.py"
        stub.write_text(
            "def main():\n"
            f"    open({str(tmp_path / 'tripwire.txt')!r}, 'a')"
            ".write('SPAWNED\\n')\n", encoding="utf-8")
        payload = _prompt_payload("sessG", "p1", "",
                                  "x" + " filler words " * 12)
        payload["prompt"] = (
            "<task-notification>Background lane completed: " + "z" * 150)
        r = _run_router(payload, tmp_path, "prompt", stub)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert not (tmp_path / "tripwire.txt").exists()

    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_keyword_suggest_skips_notification(self, tmp_path: Path) -> None:
        """agent-skill-keyword-suggest must exit BEFORE the first Python
        spawn for a task-notification prompt (the skip is pre-parse of the
        prompt body). A fake `python3` on PATH logs every invocation."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        log = tmp_path / "pylog.txt"
        real = shutil.which("python3") or shutil.which("python") or "python3"
        (fake_bin / "python3").write_text(
            "#!/bin/sh\n"
            f"echo called >> {log}\n"
            f'exec "{real}" "$@"\n', encoding="utf-8")
        (fake_bin / "python3").chmod(0o755)
        hook = HOOKS / "agent-skill-keyword-suggest.sh"
        env = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "VCT_FIND_PYTHON_PATH_ONLY": "1",
            "CLAUDE_PROJECT_DIR": str(tmp_path),
        }
        notification = json.dumps({
            "session_id": "sessH", "prompt_id": "p1",
            "prompt": "<task-notification>lane done: " + "y" * 150,
        })
        r = subprocess.run(["bash", str(hook)], input=notification,
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path), env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert not log.exists(), (
            "the hook spawned Python for a synthetic task-notification "
            "prompt — the skip must fire first")


# --- F4: the nudge early-exits on subagent PostToolUse -------------------------


class TestNudgeAgentEarlyExit:
    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_subagent_tool_call_never_touches_the_counter(self,
                                                          tmp_path: Path) -> None:
        state = tmp_path / "vctstate"
        hook = HOOKS / "kg-update-nudge.sh"
        env = {"PATH": "/usr/bin:/bin",
               "VCT_STATE_DIR": str(state),
               "VCT_FIND_PYTHON_PATH_ONLY": "1",
               "CLAUDE_PROJECT_DIR": str(tmp_path)}
        payload = json.dumps({
            "session_id": "sessI", "prompt_id": "p1",
            "tool_name": "Write",
            "tool_input": {"file_path": "/x/knowledge/node.md"},
            "agent_id": "lane9", "agent_type": "expert-coder",
        })
        r = subprocess.run(["bash", str(hook)], input=payload,
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path), env=env)
        assert r.returncode == 0, r.stderr
        assert not (state / "metrics" / "kg_update_tokens.jsonl").exists(), (
            "a subagent tool call must not touch the machine-wide nudge "
            "counter (F4: it resets the main chat's baseline and contends "
            "the lock)")

    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_user_prompt_still_counts(self, tmp_path: Path) -> None:
        """The UserPromptSubmit counting path (no agent_id, no tool_name)
        is unchanged: the counter row IS written."""
        state = tmp_path / "vctstate"
        hook = HOOKS / "kg-update-nudge.sh"
        env = {"PATH": "/usr/bin:/bin",
               "VCT_STATE_DIR": str(state),
               "VCT_FIND_PYTHON_PATH_ONLY": "1",
               "CLAUDE_PROJECT_DIR": str(tmp_path)}
        payload = json.dumps({
            "session_id": "sessI", "prompt_id": "p1",
            "prompt": "please continue the work",
        })
        r = subprocess.run(["bash", str(hook)], input=payload,
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path), env=env)
        assert r.returncode == 0, r.stderr
        metrics = state / "metrics" / "kg_update_tokens.jsonl"
        assert metrics.exists()
        rows = [json.loads(ln) for ln in
                metrics.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert any(row.get("session_id") == "sessI" for row in rows)


# --- F11: the Build-Anchor ledger is per agent ---------------------------------


class TestBuildAnchorPerAgent:
    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_read_ledger_keyed_per_agent(self, tmp_path: Path) -> None:
        hook = HOOKS / "pre-tool-use.sh"
        env = {"PATH": "/usr/bin:/bin",
               "VCT_FIND_PYTHON_PATH_ONLY": "1",
               "CLAUDE_PROJECT_DIR": str(tmp_path)}
        target = tmp_path / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        sub_payload = json.dumps({
            "session_id": "sessJ", "tool_name": "Read",
            "tool_input": {"file_path": str(target)},
            "agent_id": "laneK", "agent_type": "expert-coder",
        })
        r = subprocess.run(["bash", str(hook)], input=sub_payload,
                           capture_output=True, text=True, timeout=60,
                           cwd=str(tmp_path), env=env)
        assert r.returncode == 0, r.stderr
        state = tmp_path / ".claude" / "state"
        assert (state / "reads_sessJ__laneK.txt").exists()
        assert not (state / "reads_sessJ.txt").exists(), (
            "a subagent's Read must not satisfy the MAIN chat's Build-Anchor "
            "gate (and vice versa)")

        main_payload = json.dumps({
            "session_id": "sessJ", "tool_name": "Read",
            "tool_input": {"file_path": str(target)},
        })
        r2 = subprocess.run(["bash", str(hook)], input=main_payload,
                            capture_output=True, text=True, timeout=60,
                            cwd=str(tmp_path), env=env)
        assert r2.returncode == 0, r2.stderr
        assert (state / "reads_sessJ.txt").exists()


# --- F16: per-agent once-per-session sentinels ---------------------------------


class TestPerAgentSentinels:
    @pytest.mark.skipif(not _has_bash(), reason="bash required")
    def test_kg_sync_failure_sentinel_keyed_per_agent(self,
                                                      tmp_path: Path) -> None:
        """_kg_debounce_session_key gains the __<agent_id> component when
        the wrapper exported VCT_AGENT_ID (the failure notice is hook
        context — it reaches the subagent only, so the sentinel must not
        let one lane's notice silence the main chat's row)."""
        script = (
            'cd / 2>/dev/null\n'
            f'_KG_DEBOUNCE_LIB="{HOOKS_LIB}/kg-sync-debounce.sh"\n'
            f'. "{HOOKS_LIB}/kg-sync-debounce.sh"\n'
            'echo "$(VCT_SESSION_ID=sessL VCT_AGENT_ID=laneM '
            '_kg_debounce_session_key)"\n'
            'echo "$(VCT_SESSION_ID=sessL _kg_debounce_session_key)"\n'
            'echo "$(VCT_SESSION_ID=bad!id VCT_AGENT_ID=laneM '
            '_kg_debounce_session_key)"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        got = [ln for ln in r.stdout.splitlines() if ln]
        # The third row: an untrustworthy session id stays "default" (the
        # helper never composes a shared default__<agent> bucket).
        assert got == ["sessL__laneM", "sessL", "default"]


# --- L4: a missed leg is detached and fills the cache --------------------------


class TestLegWarmer:
    def test_spawn_spec_on_missed_leg(self, tmp_path: Path,
                                      monkeypatch) -> None:
        """A leg whose join deadline expires is handed to the detached
        warmer with everything needed to re-run it (mock the leg runner)."""
        import hook_context_router as hcr

        stub = _stub_producer(tmp_path, "kg_warm.py", _KG_BLOCK)
        monkeypatch.setenv("VCO_ROUTER_KG_SCRIPT", str(stub))
        captured: dict = {}

        def _fake_run_legs(legs, leg_timeout_s=None):
            return {name: "" for name in legs}  # both legs "missed"

        def _fake_spawn(project_root, warm_spec):
            captured.update(warm_spec)

        monkeypatch.setattr(hcr._hds, "run_legs", _fake_run_legs)
        monkeypatch.setattr(hcr, "_spawn_leg_warmer", _fake_spawn)

        plan = hcr._Plan()
        plan.kg_query = "w6c1 warmer query"
        plan.kg_profile = "prompt"
        plan.kg_limit = 3
        kg_text, _cg = hcr._run_legs(plan, "sessM", "p1", str(tmp_path), "",
                                     time.monotonic() + 6.0)
        assert kg_text == ""
        assert "kg" in captured, "a missed KG leg was not handed to the warmer"
        assert captured["kg"]["cache_key"] == hcr.cache_key(
            "kgi", "prompt", "w6c1 warmer query", "3", "p1")
        assert captured["kg"]["script"] == str(stub)
        assert "w6c1 warmer query" in " ".join(
            str(a) for a in captured["kg"]["argv"])

    def test_warm_legs_mode_caches_result(self, tmp_path: Path) -> None:
        """The --warm-legs child runs the specced producer and writes the
        cache; an empty producer output is NEVER cached (§9)."""
        import hook_context_router as hcr

        stub = _stub_producer(tmp_path, "kg_warm2.py", _KG_BLOCK)
        empty_stub = _stub_producer(tmp_path, "kg_empty.py", "")
        key = hcr.cache_key("kgi", "prompt", "q", "3", "p")
        spec = {"project_root": str(tmp_path), "legs": {
            "kg": {"script": str(stub), "argv": ["q", "--limit", "3"],
                   "cache_key": key}}}
        from tests.common.child_env import child_env

        r = subprocess.run(
            [sys.executable, str(ROUTER), "--warm-legs"],
            input=json.dumps(spec), capture_output=True, text=True,
            timeout=120, cwd=str(tmp_path), env=child_env(
                CLAUDE_PROJECT_DIR=str(tmp_path)))
        assert r.returncode == 0, r.stderr
        cached = tmp_path / ".claude" / "state" / "query_cache" / key
        assert cached.exists()
        assert "BODYMARKER" in cached.read_text(encoding="utf-8")

        key2 = hcr.cache_key("kgi", "prompt", "q2", "3", "p")
        spec2 = {"project_root": str(tmp_path), "legs": {
            "kg": {"script": str(empty_stub),
                   "argv": ["q2", "--limit", "3"], "cache_key": key2}}}
        r2 = subprocess.run(
            [sys.executable, str(ROUTER), "--warm-legs"],
            input=json.dumps(spec2), capture_output=True, text=True,
            timeout=120, cwd=str(tmp_path), env=child_env(
                CLAUDE_PROJECT_DIR=str(tmp_path)))
        assert r2.returncode == 0, r2.stderr
        assert not (tmp_path / ".claude" / "state" / "query_cache" /
                    key2).exists(), "an empty leg result was cached"

    def test_slow_leg_detached_then_next_call_hits(self, tmp_path: Path) -> None:
        """End to end: a producer slower than the leg deadline yields no
        injection on THIS call, the detached warmer fills the cache, and
        the NEXT identical call injects from the cache.

        v0.2.103 de-flake: the "slow" producer is EVENT-gated (it blocks
        until a gate file exists) instead of ``sleep(1.5)`` racing the
        0.3 s join deadline, and the gate is created only AFTER r1 has
        returned. Two races removed that way:

        * the interactive miss was previously a wall-clock margin
          (1.5 s sleep vs 0.3 s deadline) — safe in isolation, but the
          whole test also depended on the DETACHED warmer finishing within
          a fixed 30 s poll budget; under heavy machine load the warmer
          (a fresh python + router-module import + producer run) could
          exceed it and the test failed with "the detached warmer never
          filled the cache". The gate makes both halves causal: r1's leg
          provably had nothing to read (no gate → no output, ever), and
          the warmer is provably still pending when the gate opens, so
          observing the warmed cache is an EVENT, bounded only by the
          machine's speed (120 s — the same budget every other subprocess
          in this module gets), not by a margin chosen against one.
        """
        gate = tmp_path / "warm-gate.marker"
        slow = _stub_producer(tmp_path, "kg_slow.py", _KG_BLOCK,
                              gate_env="VCO_TEST_W6C1_GATE")
        env = {"VCO_INJECT_LEG_TIMEOUT_S": "0.3",
               "VCO_TEST_W6C1_GATE": str(gate)}
        r1 = _run_router(_prompt_payload("sessN", "pw", "", "warm"),
                         tmp_path, "prompt", slow, extra_env=env)
        assert r1.returncode == 0, r1.stderr
        assert "KG: W6C1Node" not in r1.stdout, (
            "the interactive run must still respect its deadline")
        # r1 has exited, so its interactive join provably expired while the
        # producer was still gate-blocked. Open the gate: the detached
        # warmer (spawned during r1, running the SAME gated producer with
        # no deadline) now completes and writes the cache.
        gate.write_text("go\n", encoding="utf-8")
        deadline = time.monotonic() + 120.0
        warmed = False
        while time.monotonic() < deadline:
            for f in (tmp_path / ".claude" / "state" / "query_cache").glob("*"):
                if "BODYMARKER" in f.read_text(encoding="utf-8",
                                               errors="replace"):
                    warmed = True
                    break
            if warmed:
                break
            time.sleep(0.3)
        assert warmed, "the detached warmer never filled the cache"
        r2 = _run_router(_prompt_payload("sessN", "pw", "", "warm"),
                         tmp_path, "prompt", slow, extra_env=env)
        assert r2.returncode == 0, r2.stderr
        assert "KG: W6C1Node" in r2.stdout, (
            "the next identical call must be a cache HIT and inject")
