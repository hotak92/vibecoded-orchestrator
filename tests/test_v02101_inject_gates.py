# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-A — the §2.1 noise-gate table, budget bounds, and the router's
dedupe/budget mechanics.

RED on the base tree: `vco_lib.inject_intent` and
`claude_mcp_servers/scripts/hook_context_router.py` are absent.

Covers (PLAN-V02101 §WP-F row 2 + §2.1):
  * floors/tiers per surface (0.75 bash-read vs 0.65 edit vs 0.70 read),
    titles-only below 0.85, three_chunks only on strong surfaces >= 0.85;
  * per-injection 2 500-char soft cap;
  * per-turn 6 000-char budget: exhaustion degrades to titles one-liners;
    missing prompt_id fails OPEN (no enforcement) rather than collapsing
    every turn onto one budget file;
  * session-id sanitisation matches `_lib/session-id.sh`;
  * the router's Python seen-store filter is byte-compatible with
    `_lib/seen-store.sh::vco_filter_seen_blocks` (same files, same key
    format — plan §3 WP-A3 "format unchanged");
  * `_lib/inject-budget.{sh,ps1}` path convention parity with the Python
    one-home + the VCO_INJECT_PROFILE=off kill switch;
  * router smoke: MECHANICAL bash → exit 0, no output, NO producer spawn;
    agent surface with an empty prompt → exit 0 untouched; --intent-out
    records the classification for the Wave-2 outcome-event wrapper;
  * every profile's RL task type is a member of rl_kg_search's
    KNOWN_TASK_TYPES (WP-B1: types exist before any wrapper sets them).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_LIB = REPO_ROOT / "templates" / "hooks" / "_lib"
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"

sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

from vco_lib.inject_intent import (  # noqa: E402
    PER_INJECTION_SOFT_CAP,
    PER_TURN_BUDGET_CHARS,
    STRONG_SCORE,
    budget_state_path,
    cap_block,
    cg_policy,
    kg_gate,
    sanitize_session_id,
    task_type_for,
    titles_one_liner,
)


# --- §2.1 table: floors and tiers -------------------------------------------


class TestKgFloors:
    @pytest.mark.parametrize(
        ("profile", "floor"),
        [
            ("bash_read", 0.75),
            ("bash_edit", 0.65),
            ("read_code", 0.70),
            ("read_docs", 0.70),
            ("edit", 0.65),
            ("write", 0.65),
            ("agent_brief", 0.65),
        ],
    )
    def test_floor(self, profile: str, floor: float) -> None:
        gate = kg_gate(profile)
        assert gate is not None
        assert gate.floor == pytest.approx(floor)

    @pytest.mark.parametrize("profile", ["bash_search", "grep"])
    def test_no_kg_leg(self, profile: str) -> None:
        """SEARCH/Grep surfaces run NO KG leg (§2.1: medium intent, exact
        code-graph only)."""
        assert kg_gate(profile) is None

    def test_strong_threshold_is_085(self) -> None:
        assert STRONG_SCORE == pytest.approx(0.85)
        for profile in ("bash_read", "edit", "agent_brief"):
            gate = kg_gate(profile)
            assert gate is not None
            assert gate.strong_threshold == pytest.approx(0.85)

    def test_titles_only_below_strong(self) -> None:
        for profile in ("bash_read", "bash_edit", "read_code", "read_docs",
                        "edit", "write", "agent_brief"):
            gate = kg_gate(profile)
            assert gate is not None
            assert gate.tier_below == "titles"

    def test_three_chunks_only_on_strong_surfaces(self) -> None:
        """three_chunks is allowed >= 0.85 ONLY on edit/write/agent (§2.1:
        owner — multi-chunk justified on strong matches); every other KG
        surface tops out at single_chunk."""
        for profile in ("edit", "write", "agent_brief"):
            gate = kg_gate(profile)
            assert gate is not None
            assert gate.tier_above == "three_chunks"
        for profile in ("bash_read", "bash_edit", "read_code", "read_docs"):
            gate = kg_gate(profile)
            assert gate is not None
            assert gate.tier_above == "single_chunk"

    def test_bash_read_titles_max_three_rows(self) -> None:
        gate = kg_gate("bash_read")
        assert gate is not None
        assert gate.max_rows == 3

    def test_agent_brief_char_bound(self) -> None:
        """The agent brief lands in the subagent's FIRST prompt — every byte
        is re-paid on every lane turn (§2.1/C4: <= 1 500 chars)."""
        gate = kg_gate("agent_brief")
        assert gate is not None
        assert gate.max_chars == 1500
        assert gate.max_rows == 3


class TestCgPolicies:
    def test_docs_and_agent_have_no_cg_leg(self) -> None:
        assert cg_policy("read_docs").enabled is False
        assert cg_policy("agent_brief").enabled is False

    def test_row_and_caller_caps(self) -> None:
        for profile in ("bash_read", "bash_edit", "bash_search", "read_code",
                        "grep", "edit", "write"):
            pol = cg_policy(profile)
            assert pol.enabled is True
            assert pol.max_rows <= 5
            assert pol.max_callers_per_row <= 5

    def test_bash_read_needs_clean_symbol_and_revision_stamp(self) -> None:
        pol = cg_policy("bash_read")
        assert pol.require_clean_symbol is True
        assert pol.revision_stamp is True

    def test_read_code_pays_no_stamp(self) -> None:
        """Round-3 nit-2 resolution (REMOVE the request, per coordinator):
        a Read has no pinned ref to compare against — the stamp's only
        CONSUMER is the bash_read rev gate (`git show <rev>`), and the
        router strips stamp lines before emitting, so requesting one on
        Read was a `git rev-parse` per CG leg with no reader."""
        assert cg_policy("read_code").revision_stamp is False
        assert cg_policy("bash_read").revision_stamp is True

    def test_edit_excludes_self_file(self) -> None:
        assert cg_policy("edit").exclude_self_file is True


# --- §2.1 bounds: per-injection cap + per-turn budget ------------------------


class TestCapsAndBudget:
    def test_per_injection_soft_cap_constant(self) -> None:
        assert PER_INJECTION_SOFT_CAP == 2500

    def test_cap_block_truncates_with_marker(self) -> None:
        text = "KG: t | concept | score=0.90 | TITLES\n" + ("x" * 4000)
        out = cap_block(text)
        assert len(out) <= PER_INJECTION_SOFT_CAP
        assert out.startswith("KG: t |")
        assert "[cut" in out  # an honest truncation marker, never a silent slice

    def test_cap_block_leaves_short_text_alone(self) -> None:
        text = "KG: t | concept | score=0.90 | TITLES\nsmall body"
        assert cap_block(text) == text

    def test_cap_block_multibyte_safe(self) -> None:
        text = "KG: t | concept | score=0.90 | TITLES\n" + ("é" * 3000)
        out = cap_block(text)
        assert len(out.encode("utf-8")) > 0
        out.encode("utf-8").decode("utf-8")  # must not raise

    def test_per_turn_budget_constant(self) -> None:
        assert PER_TURN_BUDGET_CHARS == 6000

    def test_titles_one_liner_degrades_block(self) -> None:
        block = (
            "KG: Some Node | concept | score=0.81 | 1 CHUNK:\n"
            "a body line\nanother body line\n"
        )
        out = titles_one_liner(block)
        assert out.splitlines() == ["KG: Some Node | concept | score=0.81 | TITLES"]

    def test_titles_one_liner_code_block(self) -> None:
        block = (
            "CODE: mod.fn | function | def mod.py:10 | callers: a.b@x.py:1\n"
            "  extra detail line\n"
        )
        out = titles_one_liner(block)
        lines = out.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("CODE: mod.fn")


class TestBudgetStatePath:
    def test_path_shape(self, tmp_path: Path) -> None:
        p = budget_state_path("sess123", "prompt9", str(tmp_path))
        assert p is not None
        assert p == str(tmp_path / ".claude" / "state" / "inject_budget_sess123_prompt9")

    @pytest.mark.parametrize("sid", ["", "default"])
    def test_untrustworthy_session_fails_open(self, sid: str, tmp_path: Path) -> None:
        assert budget_state_path(sid, "p1", str(tmp_path)) is None

    def test_missing_prompt_id_fails_open(self, tmp_path: Path) -> None:
        """No prompt_id → NO budget file: an empty component would collapse
        every turn of the session onto one budget (over-suppression)."""
        assert budget_state_path("sess1", "", str(tmp_path)) is None

    def test_hostile_ids_rejected(self, tmp_path: Path) -> None:
        assert budget_state_path("a/b", "p", str(tmp_path)) is None
        assert budget_state_path("s", "../../etc", str(tmp_path)) is None

    def test_sanitize_matches_shell(self) -> None:
        """sanitize_session_id mirrors _lib/session-id.sh: allow-list
        [A-Za-z0-9_-], anything else → 'default', empty stays empty."""
        assert sanitize_session_id("abc-123_XY") == "abc-123_XY"
        assert sanitize_session_id("hostile id!") == "default"
        assert sanitize_session_id("../x") == "default"
        assert sanitize_session_id("") == ""


# --- seen-store parity (Python filter == shell filter, same files/keys) ------

_BLOCK_CORPUS = (
    "KG: Node One | concept | score=0.80 | SUMMARY: body text here\n"
    "\n"
    "KG: Node One | concept | score=0.80 | SUMMARY: body text here\n"  # dup → suppressed
    "\n"
    "CODE: mod.fn | CodeFunction | distance=0.2 | src=src/mod.py\n"
    "  body\n"
    "\n"
    "CODE: mod.fn | CodeFunction | distance=0.2 | src=src/mod.py\n"  # dup → suppressed
    "\n"
    "KG: Node Two | concept | score=0.90 | 1 CHUNK:\n"
    "chunk body line\n"
)


def _has_bash() -> bool:
    return shutil.which("bash") is not None


@pytest.mark.skipif(not _has_bash(), reason="bash required")
class TestSeenStoreParity:
    """The router dedupes through the SAME per-session files with the SAME
    key format as `_lib/seen-store.sh` (plan §3 WP-A3). This drives both
    implementations over one corpus and compares emitted text + file
    contents."""

    def _shell_filter(self, tmp_path: Path, corpus: str) -> tuple[str, str]:
        inject_f = tmp_path / "shell_inject.txt"
        reads_f = tmp_path / "shell_reads.txt"
        reads_f.write_text("")
        payload = tmp_path / "corpus.txt"
        payload.write_text(corpus, encoding="utf-8")
        py = shutil.which("python3") or "python3"
        script = (
            f'export PY="{py}"\n'
            f'export PROJECT_ROOT="{tmp_path}"\n'
            f'. "{HOOKS_LIB}/seen-store.sh"\n'
            f'vco_filter_seen_blocks "$(cat "{payload}")" "{inject_f}" "{reads_f}"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        return r.stdout, inject_f.read_text("utf-8")

    def _python_filter(self, tmp_path: Path, corpus: str) -> tuple[str, str]:
        from hook_context_router import filter_seen_blocks  # noqa: PLC0415

        inject_f = tmp_path / "py_inject.txt"
        reads_f = tmp_path / "py_reads.txt"
        reads_f.write_text("")
        out = filter_seen_blocks(corpus, str(inject_f), str(reads_f), str(tmp_path))
        return out, inject_f.read_text("utf-8") if inject_f.exists() else ""

    def test_same_output_and_keys(self, tmp_path: Path) -> None:
        sh_out, sh_keys = self._shell_filter(tmp_path, _BLOCK_CORPUS)
        py_out, py_keys = self._python_filter(tmp_path, _BLOCK_CORPUS)
        # Normalise the trailing-newline shape: the shell printf '%s' strips
        # nothing but the corpus reassembly differs in the final blank line.
        norm = lambda s: [ln for ln in s.splitlines() if ln.strip()]  # noqa: E731
        assert norm(py_out) == norm(sh_out)
        assert sorted(py_keys.splitlines()) == sorted(sh_keys.splitlines())

    def test_reads_ledger_suppression_parity(self, tmp_path: Path) -> None:
        """A CODE block whose src is in the reads-ledger is suppressed by
        BOTH implementations (rule (b), both path shapes)."""
        corpus = "CODE: mod.fn | CodeFunction | distance=0.2 | src=src/mod.py\n  body\n"
        inject_f = tmp_path / "inj.txt"
        reads_f = tmp_path / "reads.txt"
        reads_f.write_text(f"{tmp_path}/src/mod.py\n", encoding="utf-8")  # absolute shape
        from hook_context_router import filter_seen_blocks  # noqa: PLC0415

        py_out = filter_seen_blocks(corpus, str(inject_f), str(reads_f), str(tmp_path))
        assert "mod.fn" not in py_out
        # shell side, same inputs
        py = shutil.which("python3") or "python3"
        payload = tmp_path / "corpus2.txt"
        payload.write_text(corpus, encoding="utf-8")
        script = (
            f'export PY="{py}"\n'
            f'export PROJECT_ROOT="{tmp_path}"\n'
            f'. "{HOOKS_LIB}/seen-store.sh"\n'
            f'vco_filter_seen_blocks "$(cat "{payload}")" "{inject_f}" "{reads_f}"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert "mod.fn" not in r.stdout

    def test_blind_inject_on_untrustworthy_session(self, tmp_path: Path) -> None:
        """Empty inject-file path → dedupe DISABLED (inject blind), nothing
        recorded — same policy as the shell store's cross-session-bleed guard."""
        from hook_context_router import filter_seen_blocks  # noqa: PLC0415

        corpus = "KG: A | concept | score=0.9 | SUMMARY: x\n"
        out1 = filter_seen_blocks(corpus, "", "", str(tmp_path))
        out2 = filter_seen_blocks(corpus, "", "", str(tmp_path))
        assert "KG: A" in out1 and "KG: A" in out2


# --- _lib/inject-budget.{sh,ps1}: parity with the Python one-home ------------


@pytest.mark.skipif(not _has_bash(), reason="bash required")
class TestInjectBudgetLib:
    def _run_sh(self, snippet: str, tmp_path: Path) -> subprocess.CompletedProcess:
        py = shutil.which("python3") or "python3"
        script = (
            f'export PY="{py}"\n'
            f'export PROJECT_ROOT="{tmp_path}"\n'
            f'export VCO_LIB_ROOT="{REPO_ROOT}"\n'
            f'. "{HOOKS_LIB}/inject-budget.sh"\n'
            f"{snippet}\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=60, cwd=str(tmp_path))

    def test_path_matches_python(self, tmp_path: Path) -> None:
        r = self._run_sh('vco_inject_budget_path "sess1" "p1"', tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(
            tmp_path / ".claude" / "state" / "inject_budget_sess1_p1"
        )

    def test_untrustworthy_session_empty_path(self, tmp_path: Path) -> None:
        r = self._run_sh('vco_inject_budget_path "default" "p1"', tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""

    def test_kill_switch(self, tmp_path: Path) -> None:
        r = self._run_sh(
            'VCO_INJECT_PROFILE=off; export VCO_INJECT_PROFILE\n'
            'vco_inject_profile_off && echo OFF || echo ON\n'
            'unset VCO_INJECT_PROFILE\n'
            'vco_inject_profile_off && echo OFF || echo ON',
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["OFF", "ON"]

    def test_gc_removes_only_old_budget_files(self, tmp_path: Path) -> None:
        state = tmp_path / ".claude" / "state"
        state.mkdir(parents=True)
        old = state / "inject_budget_old_p"
        old.write_text("100")
        fresh = state / "inject_budget_new_p"
        fresh.write_text("50")
        other = state / "seen_inject_x.txt"
        other.write_text("keep")
        os.utime(old, (1_000_000, 1_000_000))  # ancient
        r = self._run_sh("vco_inject_budget_gc", tmp_path)
        assert r.returncode == 0, r.stderr
        assert not old.exists()
        assert fresh.exists()
        assert other.exists()

    def test_ps1_sibling_exists(self) -> None:
        assert (HOOKS_LIB / "inject-budget.sh").exists()
        assert (HOOKS_LIB / "inject-budget.ps1").exists(), (
            "inject-budget.ps1 sibling MISSING — check_hook_parity.py EXCLUDES "
            "_lib/, so this must be hand-verified here."
        )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
class TestInjectBudgetPs1:
    def test_path_and_kill_switch_parity(self, tmp_path: Path) -> None:
        script = (
            f'$env:CLAUDE_PROJECT_DIR = "{tmp_path}"\n'
            f'$env:VCO_LIB_ROOT = "{REPO_ROOT}"\n'
            f'. "{HOOKS_LIB}/inject-budget.ps1"\n'
            f'Get-VcoInjectBudgetPath "sess1" "p1"\n'
            f'$env:VCO_INJECT_PROFILE = "off"\n'
            f'if (Test-VcoInjectProfileOff) {{ "OFF" }} else {{ "ON" }}\n'
            f'Remove-Item Env:VCO_INJECT_PROFILE\n'
            f'if (Test-VcoInjectProfileOff) {{ "OFF" }} else {{ "ON" }}\n'
        )
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        assert lines[0].endswith(os.path.join(".claude", "state", "inject_budget_sess1_p1").replace("\\", "/")) or \
            lines[0].replace("\\", "/").endswith(".claude/state/inject_budget_sess1_p1")
        assert lines[1:] == ["OFF", "ON"]


# --- WP-B1: RL task types exist before any wrapper sets them -----------------


class TestTaskTypes:
    def test_profile_task_type_map(self) -> None:
        assert task_type_for("bash_read") == "pre_bash_kg_search"
        assert task_type_for("bash_edit") == "pre_bash_kg_search"
        assert task_type_for("read_code") == "pre_read_kg_search"
        assert task_type_for("read_docs") == "pre_read_kg_search"
        assert task_type_for("grep") == "pre_search_kg_search"
        assert task_type_for("bash_search") == "pre_search_kg_search"
        assert task_type_for("edit") == "pre_edit_kg_search"
        assert task_type_for("write") == "pre_write_kg_search"
        assert task_type_for("agent_brief") == "agent_brief_kg_search"

    def test_all_task_types_known_to_rl_kg_search(self) -> None:
        import rl_kg_search  # noqa: PLC0415 — module-scope imports are light

        for profile in ("bash_read", "bash_edit", "bash_search", "read_code",
                        "read_docs", "grep", "edit", "write", "agent_brief"):
            assert task_type_for(profile) in rl_kg_search.KNOWN_TASK_TYPES

    def test_new_wp_b1_types_present(self) -> None:
        import rl_kg_search  # noqa: PLC0415

        for t in ("pre_read_kg_search", "pre_write_kg_search",
                  "pre_search_kg_search", "agent_brief_kg_search"):
            assert t in rl_kg_search.KNOWN_TASK_TYPES

    def test_legacy_types_kept(self) -> None:
        import rl_kg_search  # noqa: PLC0415

        for t in ("pre_edit_kg_search", "pre_bash_kg_search",
                  "pre_tool_use_kg_search", "subagent_kg_search", "cli_kg_search"):
            assert t in rl_kg_search.KNOWN_TASK_TYPES

    def test_unknown_task_type_never_leaks_free_text(self) -> None:
        import rl_kg_search  # noqa: PLC0415

        assert rl_kg_search.resolve_task_type("bogus free text") == "cli_kg_search"


# --- Router smoke (no producer may spawn on MECHANICAL / empty surfaces) -----


def _make_tripwire_stub(tmp_path: Path) -> Path:
    """Producer seam: both legs point at this stub, so ANY producer spawn on
    a must-not-query path is visible as a file the assertions check for."""
    tripwire = tmp_path / "tripwire.txt"
    stub = tmp_path / "stub_producer.py"
    stub.write_text(
        f"open({str(tripwire)!r}, 'a').write('SPAWNED\\n')\n", encoding="utf-8")
    return stub


def _run_router(stdin_payload: dict, tmp_path: Path, surface: str,
                extra_env: dict | None = None,
                argv_extra: list[str] | None = None) -> subprocess.CompletedProcess:
    """Drive the router as a subprocess (tripwire producers, child_env)."""
    from tests.common.child_env import child_env

    stub = _make_tripwire_stub(tmp_path)
    env = child_env(
        CLAUDE_PROJECT_DIR=str(tmp_path),
        VCO_ROUTER_KG_SCRIPT=str(stub),
        VCO_CG_SCRIPT=str(stub),
    )
    # Clear the ambient kill switch FIRST, then apply the test's overrides
    # (a test that sets VCO_INJECT_PROFILE=off must keep it).
    env.pop("VCO_INJECT_PROFILE", None)
    env.update(extra_env or {})
    argv = [sys.executable, str(ROUTER), surface]
    if argv_extra:
        argv += argv_extra
    return subprocess.run(
        argv, input=json.dumps(stdin_payload),
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env,
    )


class TestRouterSmoke:
    def test_router_exists(self) -> None:
        assert ROUTER.exists()

    def test_mechanical_bash_no_spawn_no_output(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "sess-1", "prompt_id": "p-1",
            "tool_name": "Bash", "cwd": str(tmp_path),
            "tool_input": {"command": "cargo clippy --workspace"},
        }
        r = _run_router(payload, tmp_path, "bash")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert not (tmp_path / "tripwire.txt").exists(), (
            "MECHANICAL must not spawn ANY producer (no query, no RL event)")

    def test_kill_switch_disables(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "sess-1", "prompt_id": "p-1",
            "tool_name": "Bash", "cwd": str(tmp_path),
            "tool_input": {"command": "grep -rn vco_seen_add templates/"},
        }
        r = _run_router(payload, tmp_path, "bash",
                        extra_env={"VCO_INJECT_PROFILE": "off"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert not (tmp_path / "tripwire.txt").exists()

    def test_agent_empty_prompt_untouched(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "sess-2", "prompt_id": "p-2",
            "tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"prompt": "", "description": "x", "model": "glm"},
        }
        from tests.common.child_env import child_env

        argv = [sys.executable, str(ROUTER), "agent"]
        env = child_env(CLAUDE_PROJECT_DIR=str(tmp_path))
        r = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path), env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""  # no envelope → input untouched

    def test_intent_out_written_for_mechanical(self, tmp_path: Path) -> None:
        """Wave-2 seam: the bash wrapper needs the classification for the
        pre_bash outcome event payload — --intent-out records it even when
        no leg runs."""
        intent_out = tmp_path / "intent.json"
        payload = {
            "session_id": "sess-3", "prompt_id": "p-3",
            "tool_name": "Bash", "cwd": str(tmp_path),
            "tool_input": {"command": "git status"},
        }
        r = _run_router(payload, tmp_path, "bash",
                        argv_extra=["--intent-out", str(intent_out)])
        assert r.returncode == 0, r.stderr
        d = json.loads(intent_out.read_text("utf-8"))
        assert d["intent"] == "MECHANICAL"

    def test_malformed_stdin_exit_zero(self, tmp_path: Path) -> None:
        from tests.common.child_env import child_env

        argv = [sys.executable, str(ROUTER), "bash"]
        r = subprocess.run(argv, input="not json at all", capture_output=True,
                           text=True, timeout=60, cwd=str(tmp_path),
                           env=child_env(CLAUDE_PROJECT_DIR=str(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


# --- GLM review round 2: SF-1 run_legs abandoned-leg stdout leak -------------

_LEAK_DRIVER = '''
import sys, time
sys.path.insert(0, {scripts!r})
import hook_dual_search as hds

def slow():
    time.sleep(0.6)
    print("LEAKED-FROM-ABANDONED-LEG")

def fast():
    print("FAST-LEG")

res = hds.run_legs({{"fast": fast, "slow": slow}}, leg_timeout_s=0.2)
print("EMIT:" + res["fast"].strip())
time.sleep(0.8)   # stay alive: an abandoned leg's late print WOULD land here
print("DONE")
'''


class TestRunLegsNoLeak:
    """SF-1: a leg abandoned at the join deadline must NEVER write to the
    real stdout afterwards — its late output would interleave into the
    router's injection text (worst case: corrupting the agent envelope's
    JSON). Empirical: the reviewer's probe printed LEAKED-FROM-ABANDONED-LEG
    between the post-run_legs prints on the pre-fix code."""

    def test_abandoned_leg_write_is_dropped(self, tmp_path: Path) -> None:
        from tests.common.child_env import child_env

        driver = tmp_path / "leak_driver.py"
        driver.write_text(
            _LEAK_DRIVER.format(
                scripts=str(REPO_ROOT / "claude_mcp_servers" / "scripts")),
            encoding="utf-8",
        )
        r = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            timeout=60, cwd=str(tmp_path), env=child_env(),
        )
        assert r.returncode == 0, r.stderr
        assert "EMIT:FAST-LEG" in r.stdout, r.stdout
        assert "DONE" in r.stdout, r.stdout
        assert "LEAKED-FROM-ABANDONED-LEG" not in r.stdout, (
            "an abandoned leg's late print reached the real stdout — the "
            "injection text (or the agent JSON envelope) could be corrupted")

    def test_docstring_claim_is_true(self) -> None:
        """The safety comment must describe the guard the code provides
        (promise-fulfilment): the leak fence is named in run_legs' doc."""
        body = (REPO_ROOT / "claude_mcp_servers" / "scripts"
                / "hook_dual_search.py").read_text(encoding="utf-8")
        assert "_PostLegStdout" in body


# --- GLM review round 2: nit-3 agent envelope guard ---------------------------


class TestAgentEnvelopePromptGuard:
    """nit-3: on a description-only tool_input the router must NOT
    synthesize a `prompt` field that did not exist — an envelope is emitted
    only when there is a real prompt to append to."""

    def _drive(self, tmp_path, monkeypatch, tool_input: dict) -> str:
        import io

        sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))
        import hook_context_router as router_mod

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("VCO_INJECT_PROFILE", raising=False)
        monkeypatch.setattr(
            router_mod, "_run_legs",
            lambda *a, **k: ("KG: Node One | concept | score=0.90 | TITLES\n", ""),
        )
        payload = {
            "session_id": "sess-agent", "prompt_id": "p-agent",
            "tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": tool_input,
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)
        try:
            rc = router_mod.main(["agent"])
        finally:
            monkeypatch.undo()
        assert rc == 0
        return buf.getvalue()

    def test_prompt_present_gets_envelope(self, tmp_path, monkeypatch) -> None:
        out = self._drive(tmp_path, monkeypatch,
                          {"prompt": "Task: do the thing", "model": "glm"})
        env = json.loads(out)
        upd = env["hookSpecificOutput"]["updatedInput"]
        assert upd["model"] == "glm"
        assert upd["prompt"].startswith("Task: do the thing")
        assert "[KG context for this task]:" in upd["prompt"]
        assert "permissionDecision" not in json.dumps(env)

    def test_description_only_emits_nothing(self, tmp_path, monkeypatch) -> None:
        out = self._drive(tmp_path, monkeypatch, {"description": "d"})
        assert out.strip() == "", (
            "a synthesized `prompt` field must not be written back into the "
            "tool input (nit-3)")


# --- GLM review round 2: nit-4 budget charge under the shared lock ------------


class TestBudgetChargeLocked:
    """nit-4: the read-modify-write goes through vco_lib.atomic's ONE
    cross-platform lock so concurrent hooks in one turn cannot undercount."""

    def test_charge_takes_exclusive_lock(self, tmp_path, monkeypatch) -> None:
        sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))
        import hook_context_router as router_mod
        import vco_lib.atomic as atomic_mod

        calls = []
        real_lock = atomic_mod.exclusive_file_lock

        def _recording_lock(lock_path, **kw):
            calls.append(str(lock_path))
            return real_lock(lock_path, **kw)

        monkeypatch.setattr(atomic_mod, "exclusive_file_lock", _recording_lock)
        path = str(tmp_path / "budget_file")
        router_mod._budget_charge(path, 10)
        router_mod._budget_charge(path, 5)
        assert calls, "budget charge must go through vco_lib.atomic's exclusive lock"
        assert open(path, encoding="utf-8").read().strip() == "15"


class TestCgRecordLocked:
    """Round-3 nit-5: the CG session-cap counter is the same read-modify-
    write race class the budget charge fixed — it takes the same lock."""

    def test_record_takes_exclusive_lock(self, tmp_path, monkeypatch) -> None:
        sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))
        import hook_context_router as router_mod
        import vco_lib.atomic as atomic_mod

        calls = []
        real_lock = atomic_mod.exclusive_file_lock

        def _recording_lock(lock_path, **kw):
            calls.append(str(lock_path))
            return real_lock(lock_path, **kw)

        monkeypatch.setattr(atomic_mod, "exclusive_file_lock", _recording_lock)
        cf = str(tmp_path / "seen_cginject_count_sess.txt")
        router_mod._cg_record(cf)
        router_mod._cg_record(cf)
        assert calls, "CG-cap record must go through vco_lib.atomic's exclusive lock"
        assert open(cf, encoding="utf-8").read().strip() == "2"


# --- GLM re-review round 3: READ/EDIT symbol-recovery fallback ----------------


class TestSymbolRecoveryGuards:
    """Round-3 SF: the router's fallback symbol recovery must honour its own
    comment — no FILE-shaped keys, and heredoc BODY prose is never command
    shape (strip_heredocs before the scan)."""

    def _plan(self, cmd: str, tmp_path: Path):
        sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))
        import hook_context_router as router_mod

        return router_mod._bash_plan({"tool_input": {"command": cmd}}, str(tmp_path))

    @pytest.mark.parametrize(
        "cmd", ["cat notes.txt", "head README.md", "tail notes.md", "cat app.log"]
    )
    def test_bare_filename_is_not_a_cg_key(self, cmd: str, tmp_path: Path) -> None:
        assert self._plan(cmd, tmp_path).cg_symbols == []

    def test_heredoc_body_prose_never_recovered_read(self, tmp_path: Path) -> None:
        cmd = "cat <<EOF\nSome prose authenticate_user and vco_thing here\nEOF"
        assert self._plan(cmd, tmp_path).cg_symbols == []

    def test_heredoc_body_prose_never_recovered_edit(self, tmp_path: Path) -> None:
        out = tmp_path / "out.md"
        cmd = f"cat > {out} <<'EOF'\nbody mentions extract_write_targets\nEOF"
        plan = self._plan(cmd, tmp_path)
        assert plan.intent_out["intent"] == "EDIT"
        assert all("extract_write_targets" not in s for s, _ in plan.cg_symbols)

    def test_edit_recovery_rejects_dotted_filename(self, tmp_path: Path) -> None:
        cmd = "sed -i 's/a/b/' notes.txt"
        plan = self._plan(cmd, tmp_path)
        assert all(not s.endswith(".txt") for s, _ in plan.cg_symbols)

    def test_clean_symbol_still_recovered(self, tmp_path: Path) -> None:
        plan = self._plan("tail -n 5 deploy_log", tmp_path)
        assert any(s == "deploy_log" for s, _ in plan.cg_symbols)

    def test_source_file_target_uses_pub_symbols(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    pass\n", encoding="utf-8")
        plan = self._plan(f"cat {f}", tmp_path)
        assert any(s == "alpha" for s, _ in plan.cg_symbols)
