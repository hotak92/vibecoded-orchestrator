# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-D — RL data continuity across the bash wrapper rework.

RED on the base tree: the old hook gated the state file + pre_bash outcome
event on the 500-char THRESHOLD (these fixtures are short), carried no
`intent` in either payload, and never spawned the router's stub seams — so
the state/outcome/retrieval assertions all fail.

WP-D contract (plan §3 WP-D 2 + 4): drive the bash wrapper with
READ/EDIT/SEARCH/MECHANICAL fixtures against stub producers; assert
retrieval + outcome events for the first three, ZERO spawns/events for
MECHANICAL.

Observables, per fixture:
  * RETRIEVAL side — the producer stub records (the router passed
    --task-type/--injection-profile to the KG leg; the CG leg got the exact
    structure argv). A stubbed producer cannot emit a real hub event, so the
    argv the REAL rl_kg_search would have received is the pinned proxy (the
    emission itself is covered by test_v02100_w5r_rl_telemetry_fixes and the
    Wave-1 task-type map).
  * OUTCOME side — the pre_bash event via a shadow `outcome_emit` package
    on PYTHONPATH (records instead of POSTing; RL_HUB_POST_DISABLED=1 as a
    second guard so NOTHING can reach the live hub from this suite).
  * PAIRING — the bash_task state file keeps its name/shape (post-bash-context-record
    joins on task_id; new keys are additive) and gains intent/targets/symbols.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from tests.test_v02101_router_surfaces import Rig, needs_bash, _bash_payload  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

_OUTCOME_STUB = '''\
import json, os

OUTCOME_EVENT_TYPES = ("bash_outcome", "edit_outcome", "pre_bash")


def emit_outcome_event(event_type=None, task_id=None, task_type=None,
                       payload=None, session_id=None, project_id=None,
                       project_name=None, **kw):
    rec = os.environ.get("VCO_TEST_OUTCOME_RECORD", "")
    if not rec or event_type not in OUTCOME_EVENT_TYPES:
        return False
    with open(rec, "a", encoding="utf-8") as fh:
        json.dump({"event_type": event_type, "task_id": task_id,
                   "task_type": task_type, "payload": payload or {},
                   "session_id": session_id}, fh)
        fh.write("\\n")
    return True
'''


class OutcomeRig(Rig):
    """Rig + the shadow outcome_emit package (PYTHONPATH-front)."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        pkg = tmp_path / "fakepkg" / "claude_mcp_servers" / "rl_client"
        pkg.mkdir(parents=True)
        (pkg.parent / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "outcome_emit.py").write_text(_OUTCOME_STUB, encoding="utf-8")
        self.outcome_record = tmp_path / "outcome_records.jsonl"
        self._fakepkg = tmp_path / "fakepkg"

    def env(self, **overrides: str) -> dict:
        env = super().env(**overrides)
        env["PYTHONPATH"] = f"{self._fakepkg}{os.pathsep}{REPO_ROOT}"
        env["VCO_TEST_OUTCOME_RECORD"] = str(self.outcome_record)
        env["RL_HUB_POST_DISABLED"] = "1"
        return env

    def outcomes(self, wait_s: float = 8.0) -> list:
        """The outcome child is backgrounded by the hook — bounded wait."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.outcome_record.exists():
                rows = [json.loads(ln) for ln in
                        self.outcome_record.read_text("utf-8").splitlines()
                        if ln.strip()]
                if rows:
                    return rows
            time.sleep(0.1)
        return []


@pytest.fixture()
def oririg(tmp_path: Path) -> OutcomeRig:
    return OutcomeRig(tmp_path)


def _classified_fixture(rig: OutcomeRig, intent: str) -> tuple[str, str]:
    """(command, session) fixtures per intent, with the files they need."""
    if intent == "READ":
        src = rig.proj / "src"
        src.mkdir(exist_ok=True)
        (src / "main.rs").write_text(
            "pub fn widget_loader() -> u32 {\n    1\n}\n", encoding="utf-8")
        return "cat src/main.rs", "sess-rl-read"
    if intent == "EDIT":
        (rig.proj / "app.py").write_text(
            "def update_config():\n    value = 1\n    return value\n",
            encoding="utf-8")
        return "sed -i 's/value = 1/value = 2/' app.py", "sess-rl-edit"
    if intent == "SEARCH":
        return "grep -rn vco_query_cache_put templates/", "sess-rl-search"
    return "git status", "sess-rl-mech"


@needs_bash
class TestRlContinuity:
    @pytest.mark.parametrize("intent", ["READ", "EDIT", "SEARCH"])
    def test_classified_command_gets_state_outcome_and_retrieval(
            self, oririg: OutcomeRig, intent: str) -> None:
        cmd, session = _classified_fixture(oririg, intent)
        r = oririg.run("pre-bash-context-inject",
                       _bash_payload(cmd, session=session, cwd=str(oririg.proj)))
        assert r.returncode == 0, r.stderr

        # --- PAIRING: the state file (unchanged name/shape + additive keys)
        states = oririg.state_files()
        assert len(states) == 1, f"{intent}: expected exactly one state file"
        want_hash = hashlib.md5(cmd.encode()).hexdigest()[:16]
        assert states[0].name == f"bash_task_{session}_{want_hash}.json"
        state = json.loads(states[0].read_text())
        for legacy_key in ("task_id", "start_ts_ms", "session_id",
                           "cmd_hash", "cmd_len"):
            assert legacy_key in state, f"legacy pairing key lost: {legacy_key}"
        assert state["task_id"].startswith("pre_bash_")
        assert state["intent"] == intent
        assert isinstance(state["targets"], list)
        assert isinstance(state["symbols"], list)
        if intent == "SEARCH":
            assert "vco_query_cache_put" in state["symbols"]

        # --- OUTCOME: the pre_bash event, JOINable on task_id
        outcomes = oririg.outcomes()
        assert outcomes, f"{intent}: no pre_bash outcome event recorded"
        ev = outcomes[0]
        assert ev["event_type"] == "pre_bash"
        assert ev["task_id"] == state["task_id"], "pairing join broken"
        assert ev["payload"]["intent"] == intent
        assert ev["payload"]["cmd_len"] == len(cmd)

        # --- RETRIEVAL: producer argv carries the RL partition + profile
        if intent in ("READ", "EDIT"):
            kg = oririg.kg_records()
            assert kg, f"{intent}: the KG leg must run"
            kargv = kg[0]
            assert kargv[kargv.index("--task-type") + 1] == "pre_bash_kg_search"
            assert kargv[kargv.index("--injection-profile") + 1] == (
                "bash_read" if intent == "READ" else "bash_edit")
        if intent in ("READ", "EDIT", "SEARCH"):
            cg = oririg.cg_records()
            assert cg, f"{intent}: the exact-symbol CG leg must run"
            assert cg[0][:2] == ["structure", "callers"]
        if intent == "SEARCH":
            assert oririg.kg_records() == [], "SEARCH runs no KG leg (§2.1)"

    def test_mechanical_zero_spawns_zero_events(self, oririg: OutcomeRig) -> None:
        cmd, session = _classified_fixture(oririg, "MECHANICAL")
        r = oririg.run("pre-bash-context-inject",
                       _bash_payload(cmd, session=session, cwd=str(oririg.proj)))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert oririg.kg_records() == []
        assert oririg.cg_records() == []
        assert oririg.state_files() == []
        time.sleep(0.6)  # give a wrongly-spawned outcome child the chance to write
        assert not oririg.outcome_record.exists() or oririg.outcomes(wait_s=0.2) == []

    def test_long_mechanical_command_no_longer_emits(self, oririg: OutcomeRig) -> None:
        """The old 500-char gate fired on ANY long command; the intent gate
        must not (a long build is still MECHANICAL — the survey's noise class)."""
        cmd = "cargo build --release " + " ".join(
            f"--feature-flag-{i}" for i in range(60))
        assert len(cmd) > 500
        r = oririg.run("pre-bash-context-inject",
                       _bash_payload(cmd, session="sess-rl-long",
                                     cwd=str(oririg.proj)))
        assert r.returncode == 0, r.stderr
        assert oririg.state_files() == []
        assert oririg.kg_records() == [] and oririg.cg_records() == []

    def test_multi_match_spawns_emit_one_outcome(self, oririg: OutcomeRig) -> None:
        """Review nit-5: a multi-match command (`cat x | grep y`) fires the
        §C1 if-group TWICE — two handler spawns for ONE tool call. The
        injection side is idempotent (seen-store + router cache); the
        pairing side must be too: exactly ONE state file and ONE pre_bash
        event, never an orphan with a distinct task_id."""
        cmd = "cat notes.md | grep -rn vco_seen_add templates/"
        payload = _bash_payload(cmd, session="sess-rl-multi",
                                cwd=str(oririg.proj))
        r1 = oririg.run("pre-bash-context-inject", payload)
        r2 = oririg.run("pre-bash-context-inject", payload)
        assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
        time.sleep(1.2)  # let a wrongly-second outcome child land
        assert len(oririg.state_files()) == 1
        rows = []
        if oririg.outcome_record.exists():
            rows = [json.loads(ln)
                    for ln in oririg.outcome_record.read_text("utf-8").splitlines()
                    if ln.strip()]
        assert len(rows) == 1, (
            f"a multi-match double-spawn must emit ONE pre_bash event, got "
            f"{len(rows)}: {[r.get('task_id') for r in rows]}"
        )

    def test_short_classified_command_now_emits(self, oririg: OutcomeRig) -> None:
        """…and the recall side: a SHORT classified command (far under the
        old 500-char gate) now gets the full pairing treatment."""
        cmd = "grep -rn seen_store templates/"
        assert len(cmd) < 100
        r = oririg.run("pre-bash-context-inject",
                       _bash_payload(cmd, session="sess-rl-short",
                                     cwd=str(oririg.proj)))
        assert r.returncode == 0, r.stderr
        assert len(oririg.state_files()) == 1
        assert oririg.outcomes(), "short SEARCH commands are training data now"
