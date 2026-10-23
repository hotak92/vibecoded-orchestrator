# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-A2 — W3R-3 / W3R-4 / W3R-10 fixes (pre/post-bash pairing,
outcome-emit loss evidence, router citation-ctx truth).

W3R-3 (B): the pre_bash/bash_outcome pairing state was keyed
``(session, md5(cmd)[:16])`` — no ``tool_use_id`` — so two concurrent
IDENTICAL commands collided (the second pre-bash overwrote the first's
pairing file; two concurrent post-bash spawns both passed the existence
check before either ``rm`` → duplicate outcome rows for one task_id; the
audit found 6 duplicated ``bash_outcome`` task_ids in 6 494 live rows).
Fixed by keying the pairing file per CALL (the same ``tool_use_id`` ladder
the N2 claim file uses) and making post-bash's read+delete an ATOMIC
``mv`` claim.

W3R-4 (SF): a pre-POST outcome-emit failure (missing/bogus venv, import
error, corrupt pairing JSON) deleted the state file unconditionally and
left NO ledger line — a permanent silent orphan. Fixed: the emit child
records an ``outcome_unsent`` line (via ``vco_lib.rl_telemetry_loss``, the
one loss-ledger home) on every pre-POST failure; the claim file is only
deleted after the emit succeeded OR the loss is ledgered.

W3R-10 (S): the router emitted the RL retrieval event + staged the
citation pending file INSIDE the KG leg, BEFORE its own seen-store
suppression / full-suppression exits decided — a repeat query staged
citation labels for nodes the model never saw. Fixed router-side: after
the decision, never-shown nodes are dropped from the staged ctx (a ctx
left with no nodes is deleted).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from tests.test_v02101_router_surfaces import (  # noqa: E402
    HOOKS,
    REPO_ROOT,
    Rig,
    _bash_payload,
    needs_bash,
    needs_pwsh,
)
from tests.common.pwsh import PWSH_BASE_FLAGS

SCRIPTS = REPO_ROOT / "claude_mcp_servers" / "scripts"
ROUTER = SCRIPTS / "hook_context_router.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hook_context_router as router  # noqa: E402

CMD = "grep -rn widget_loader src/"


# --- the pairing rig -----------------------------------------------------------


_OUTCOME_STUB = '''\
import json, os

OUTCOME_EVENT_TYPES = ("bash_outcome", "edit_outcome", "pre_bash")


def emit_outcome_event(event_type=None, task_id=None, task_type=None,
                       payload=None, session_id=None, project_id=None,
                       project_name=None, **kw):
    if os.environ.get("VCO_TEST_OUTCOME_MODE") == "raise":
        raise RuntimeError("stub emit failure (W3R-4 test)")
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


class PairingRig(Rig):
    """Rig + a shadow outcome_emit package (record mode, or raise on call)."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        pkg = tmp_path / "fakepkg" / "claude_mcp_servers" / "rl_client"
        pkg.mkdir(parents=True)
        (pkg.parent / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "outcome_emit.py").write_text(_OUTCOME_STUB, encoding="utf-8")
        self.outcome_record = tmp_path / "outcome_records.jsonl"
        self.fakepkg = tmp_path / "fakepkg"

    def env(self, **overrides: str) -> dict:
        env = super().env(**overrides)
        env["PYTHONPATH"] = f"{self.fakepkg}{os.pathsep}{REPO_ROOT}"
        env["VCO_TEST_OUTCOME_RECORD"] = str(self.outcome_record)
        return env

    def state_dir(self) -> Path:
        return self.proj / ".claude" / "state"

    def ledger_path(self) -> Path:
        return self.tmp / "vctstate" / "metrics" / "rl_telemetry_loss.jsonl"

    def outcome_rows(self, wait_s: float = 5.0) -> list:
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

    def ledger_rows(self) -> list:
        from tests.common.rl_loss_ledger import loss_records

        return loss_records(self.ledger_path())

    def bash_task_files(self) -> list:
        return sorted(self.state_dir().glob("bash_task_*.json"))


def _cmd_hash(command: str) -> str:
    return hashlib.md5(command.encode()).hexdigest()[:16]


def _write_state(rig: PairingRig, session: str, tuid: str, task_id: str,
                 command: str = CMD) -> Path:
    """Write a pairing state file exactly as pre-bash does (W3R-3 shape)."""
    digest = _cmd_hash(command)
    stem = f"bash_task_{session}_{digest}" + (f"_{tuid}" if tuid else "")
    p = rig.state_dir() / f"{stem}.json"
    p.write_text(json.dumps({
        "task_id": task_id, "start_ts_ms": 1, "session_id": session,
        "cmd_hash": digest, "cmd_len": len(command), "intent": "SEARCH",
        "targets": [], "symbols": [], "tool_use_id": tuid,
    }), encoding="utf-8")
    return p


def _post_payload(command: str = CMD, session: str = "sess-w3r",
                  tuid: str = "", response: str = "done") -> dict:
    p = {"tool_name": "Bash", "session_id": session,
         "tool_input": {"command": command}, "tool_response": response}
    if tuid:
        p["tool_use_id"] = tuid
    return p


def _run_post(rig: PairingRig, payload: dict,
              **env_overrides: str) -> subprocess.CompletedProcess:
    env = rig.env(**env_overrides)
    return subprocess.run(
        ["bash", str(HOOKS / "post-bash-context-record.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, cwd=str(rig.proj), env=env,
    )


def _run_post_concurrent(rig: PairingRig, payloads: list,
                         **env_overrides: str) -> list:
    """Start N post-bash spawns at once, as concurrent tool calls would."""
    env = rig.env(**env_overrides)
    procs = [
        subprocess.Popen(
            ["bash", str(HOOKS / "post-bash-context-record.sh")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(rig.proj), env=env)
        for _ in payloads
    ]
    for p, payload in zip(procs, payloads):
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
    out = []
    for p in procs:
        stdout = p.stdout.read()
        stderr = p.stderr.read()
        p.wait(timeout=120)
        out.append((p.returncode, stdout, stderr))
    return out


# --- W3R-3: per-call pairing + atomic one-shot claim ----------------------------


@needs_bash
class TestW3R3PreBashWritesPerCallState:
    def test_two_identical_calls_with_distinct_tuids_get_two_state_files(
            self, tmp_path: Path) -> None:
        rig = PairingRig(tmp_path)
        for tuid in ("toolu_w3r_a", "toolu_w3r_b"):
            payload = _bash_payload(CMD, session="sess-w3r-w",
                                    cwd=str(rig.proj))
            payload["tool_use_id"] = tuid
            r = rig.run("pre-bash-context-inject", payload)
            assert r.returncode == 0, r.stderr
        states = rig.bash_task_files()
        assert len(states) == 2, (
            f"concurrent identical commands must not share one pairing "
            f"file (W3R-3): {[s.name for s in states]}")
        task_ids = {json.loads(s.read_text())["task_id"] for s in states}
        assert len(task_ids) == 2
        for tuid in ("toolu_w3r_a", "toolu_w3r_b"):
            assert any(s.name.endswith(f"_{tuid}.json") for s in states), (
                f"the pairing file must be keyed by the call: {tuid} missing")


@needs_bash
class TestW3R3PostBashAtomicPairing:
    def test_concurrent_identical_calls_pair_both_outcomes(
            self, tmp_path: Path) -> None:
        """W3R-3's orphan half: distinct tool_use_ids, same command — both
        outcomes recorded, each from its own pairing file."""
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "toolu_a", "pre_bash_aa")
        _write_state(rig, "sess-w3r", "toolu_b", "pre_bash_bb")
        results = _run_post_concurrent(rig, [
            _post_payload(tuid="toolu_a"), _post_payload(tuid="toolu_b")])
        assert all(rc == 0 for rc, _, _ in results), results
        rows = rig.outcome_rows()
        assert sorted(r["task_id"] for r in rows) == ["pre_bash_aa", "pre_bash_bb"], (
            f"both per-call pairings must produce their outcome: {rows}")
        assert rig.bash_task_files() == [], "claims must be released"

    def test_same_call_spawned_twice_records_one_outcome(
            self, tmp_path: Path) -> None:
        """W3R-3's duplicate half: one pairing file, two concurrent
        post-bash spawns → exactly ONE outcome row."""
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "toolu_c", "pre_bash_cc")
        results = _run_post_concurrent(rig, [
            _post_payload(tuid="toolu_c"), _post_payload(tuid="toolu_c")])
        assert all(rc == 0 for rc, _, _ in results), results
        rows = rig.outcome_rows()
        assert len(rows) == 1, (
            f"the atomic claim must keep one spawn per call (W3R-3 "
            f"duplicate-outcome race): {rows}")
        assert rows[0]["task_id"] == "pre_bash_cc"
        assert rig.bash_task_files() == []

    def test_legacy_state_file_still_pairs(self, tmp_path: Path) -> None:
        """Old-format tolerance: a pre-W3R-3 state file (hash-only name, no
        tool_use_id key) pairs even when the payload carries a tuid."""
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "", "pre_bash_legacy")
        r = _run_post(rig, _post_payload(tuid="toolu_new"))
        assert r.returncode == 0, r.stderr
        rows = rig.outcome_rows()
        assert [row["task_id"] for row in rows] == ["pre_bash_legacy"]
        assert rig.bash_task_files() == []


@needs_pwsh
class TestW3R3Ps1Parity:
    def test_ps1_per_call_and_legacy_names(self) -> None:
        """The .ps1 sibling derives the SAME two name shapes (W3R-3)."""
        text = (HOOKS / "post-bash-context-record.ps1").read_text("utf-8")
        assert "bash_task_${SessionId}_${CmdHash}_${Tuid}.json" in text
        assert "bash_task_${SessionId}_${CmdHash}.json" in text
        pre = (HOOKS / "pre-bash-context-inject.ps1").read_text("utf-8")
        assert "bash_task_${SessionId}_${CmdHash}_${Tuid}.json" in pre


@needs_pwsh
class TestBatch8Ps1ExecutedEndToEnd:
    """Batch-8 blocker: EXECUTE each rewritten .ps1 under pwsh — a body-text
    check cannot catch an invalid .NET type literal (the
    ``[System.Security.Encoding]`` class: it throws at RUNTIME, $CmdHash
    goes null, the hook exits 0 and every Windows outcome pairing was a
    silent no-op)."""

    def test_post_bash_ps1_emits_outcome_under_pwsh(self, tmp_path: Path) -> None:
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "", "pre_bash_ps1")
        env = rig.env()
        r = subprocess.run(
            ["pwsh", *PWSH_BASE_FLAGS, "-File",
             str(HOOKS / "post-bash-context-record.ps1")],
            input=json.dumps(_post_payload()), capture_output=True, text=True,
            timeout=120, cwd=str(rig.proj), env=env)
        assert r.returncode == 0, r.stderr
        rows = rig.outcome_rows()
        assert [row["task_id"] for row in rows] == ["pre_bash_ps1"], (
            f"the pwsh hook must emit the paired outcome end-to-end: "
            f"rows={rows} stderr={r.stderr[-500:]}")
        assert rig.bash_task_files() == [], "claim released"

    def test_pre_bash_ps1_writes_md5_named_state_under_pwsh(
            self, tmp_path: Path) -> None:
        """The pwsh pre-bash hook must reach the REAL md5-derived state
        file (an invalid Encoding type literal silently degrades the hash
        to the sanitized-command fallback → no pairing)."""
        rig = PairingRig(tmp_path)
        payload = _bash_payload("grep -rn vco_query_cache_put templates/",
                                session="sess-w3r-ps", cwd=str(rig.proj))
        payload["tool_use_id"] = "toolu_ps1_exec"
        r = rig.run("pre-bash-context-inject", payload, shell="ps1")
        assert r.returncode == 0, r.stderr
        digest = _cmd_hash("grep -rn vco_query_cache_put templates/")
        want = rig.state_dir() / f"bash_task_sess-w3r-ps_{digest}_toolu_ps1_exec.json"
        assert want.is_file(), (
            f"the pwsh hook must write the md5-named per-call state file; "
            f"found: {[p.name for p in rig.bash_task_files()]}")
        state = json.loads(want.read_text())
        assert state["task_id"].startswith("pre_bash_")
        assert state["cmd_hash"] == digest
        rows = rig.outcome_rows()
        assert rows and rows[0]["task_id"] == state["task_id"], (
            "the pwsh pre_bash event must join the pairing file")


# --- W3R-4: pre-POST emit failures leave evidence -------------------------------


@needs_bash
class TestW3R4OutcomeEmitLossLedger:
    def test_bogus_venv_ledgers_and_releases(self, tmp_path: Path) -> None:
        """The audit's repro: a venv whose python is not an interpreter —
        pre-fix the pairing file was deleted silently with no ledger."""
        rig = PairingRig(tmp_path)
        bogus = tmp_path / "bogusvenv" / "bin"
        bogus.mkdir(parents=True)
        py = bogus / "python"
        py.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IEXEC)
        _write_state(rig, "sess-w3r", "", "pre_bash_bogus")
        # VCO_FIND_PYTHON_PATH_ONLY: find-python's venv probe would pick
        # the BOGUS interpreter for the parse leg too (it probes $VCT_VENV
        # first) — keep the parse python real, the EMIT venv broken.
        r = _run_post(rig, _post_payload(),
                      VCT_VENV=str(tmp_path / "bogusvenv"),
                      VCO_FIND_PYTHON_PATH_ONLY="1")
        assert r.returncode == 0, r.stderr
        rows = rig.ledger_rows()
        unsent = [row for row in rows if row.get("kind") == "outcome_unsent"]
        assert unsent, (
            f"a pre-POST emit failure must leave a loss-ledger line "
            f"(W3R-4): {rows}")
        assert unsent[0]["reason"] == "emit_child_failed"
        assert unsent[0]["task_id"] == "pre_bash_bogus"
        # the loss is ledgered → the claim may be released
        assert rig.bash_task_files() == []

    def test_missing_venv_ledgers(self, tmp_path: Path) -> None:
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "", "pre_bash_novenv")
        env = rig.env()
        env.pop("VCT_VENV", None)
        env.pop("VCT_INSTALL_ROOT", None)
        r = subprocess.run(
            ["bash", str(HOOKS / "post-bash-context-record.sh")],
            input=json.dumps(_post_payload()), capture_output=True,
            text=True, timeout=120, cwd=str(rig.proj), env=env)
        assert r.returncode == 0, r.stderr
        unsent = [row for row in rig.ledger_rows()
                  if row.get("kind") == "outcome_unsent"]
        assert unsent, f"missing venv must ledger (W3R-4): {rig.ledger_rows()}"
        assert unsent[0]["reason"] == "venv_unavailable"
        assert unsent[0]["task_id"] == "pre_bash_novenv"

    def test_emit_raised_ledgers_and_releases(self, tmp_path: Path) -> None:
        """The emit child itself records the loss (exit 3 path)."""
        rig = PairingRig(tmp_path)
        _write_state(rig, "sess-w3r", "toolu_r", "pre_bash_raise")
        r = _run_post(rig, _post_payload(tuid="toolu_r"),
                      VCO_TEST_OUTCOME_MODE="raise")
        assert r.returncode == 0, r.stderr
        assert rig.outcome_rows() == [], "the stub raised — no row expected"
        unsent = [row for row in rig.ledger_rows()
                  if row.get("kind") == "outcome_unsent"]
        assert unsent, f"an emit exception must ledger (W3R-4): {rig.ledger_rows()}"
        assert unsent[0]["reason"] == "emit_raised"
        assert unsent[0]["task_id"] == "pre_bash_raise"
        assert rig.bash_task_files() == []

    def test_unledgerable_failure_keeps_the_claim(self, tmp_path: Path) -> None:
        """No orchestrator root → the hook cannot ledger → the pairing data
        SURVIVES (the 1-day GC eventually reclaims it); never a silent rm."""
        rig = PairingRig(tmp_path)
        bogus = tmp_path / "bogusvenv2" / "bin"
        bogus.mkdir(parents=True)
        py = bogus / "python"
        py.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        py.chmod(py.stat().st_mode | stat.S_IEXEC)
        _write_state(rig, "sess-w3r", "", "pre_bash_keep")
        env = rig.env(VCT_VENV=str(tmp_path / "bogusvenv2"),
                      VCO_FIND_PYTHON_PATH_ONLY="1")
        env.pop("VCT_ORCHESTRATOR_ROOT", None)
        env.pop("VCT_INSTALL_ROOT", None)
        r = subprocess.run(
            ["bash", str(HOOKS / "post-bash-context-record.sh")],
            input=json.dumps(_post_payload()), capture_output=True,
            text=True, timeout=120, cwd=str(rig.proj), env=env)
        assert r.returncode == 0, r.stderr
        assert rig.ledger_rows() == []
        leftovers = rig.bash_task_files()
        assert len(leftovers) == 1, (
            f"an unledgerable loss must KEEP the claim file (W3R-4): "
            f"{[f.name for f in leftovers]}")
        assert json.loads(leftovers[0].read_text())["task_id"] == "pre_bash_keep"


# --- W3R-10: the staged citation ctx matches the transcript truth ---------------


_KG_STUB_W3R10 = '''\
import argparse, json, os, sys, time

def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args()
    repo = os.environ.get("VCO_TEST_ROUTER_REPO", "")
    if repo and repo not in sys.path:
        sys.path.insert(0, repo)
    from claude_mcp_servers.rl_client.citation_pending import stage_pending
    stage_pending(
        session_id=os.environ.get("VCO_TEST_STUB_SID", ""),
        task_id=os.environ.get("VCO_STUB_TASK_ID", "pre_edit_stub"),
        seq=None, query="stub query",
        ctx={"nodes": [{"title": "Stub Node", "n_emb": [0.1, 0.2]}],
             "active_model": "stub-model",
             "task_type": "pre_edit_kg_search"},
        source="hook",
        project_root=os.environ.get("CLAUDE_PROJECT_DIR", ""),
    )
    if os.environ.get("VCO_STUB_WRITE_SIBLING"):
        # Simulate a SIBLING concurrent router staging its own ctx mid-run:
        # written DURING this run's legs (newer than any pre-legs snapshot)
        # but carrying NO router_run_id of THIS run.
        d = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""),
                         ".claude", "state", "rl_pending")
        with open(os.path.join(d, "sess-w3r10__pre_edit_sib.json"), "w") as fh:
            json.dump({"session_id": os.environ.get("VCO_TEST_STUB_SID", ""),
                       "task_id": "pre_edit_sib", "seq": None, "query": "q",
                       "ts_ms": int(time.time() * 1000), "source": "hook",
                       "ctx": {"nodes": [{"title": "Stub Node",
                                          "n_emb": [0.1, 0.2]}],
                               "active_model": "stub-model",
                               "task_type": "pre_edit_kg_search"}}, fh)
    print("KG: Stub Node | concept | score=0.90 | TITLES")
    return 0
'''


def _router_run(proj: Path, prompt_id: str, stub: Path, sid: str,
                extra_env: dict | None = None) -> str:
    from tests.common.child_env import child_env

    payload = {
        "tool_name": "Bash", "session_id": sid, "prompt_id": prompt_id,
        "cwd": str(proj), "tool_input": {"command": "cat notes.md"},
    }
    # child_env(): the router child imports vco_lib from THIS checkout
    # (the child-env ratchet) — never a stale site-packages shadow.
    env = child_env(
        CLAUDE_PROJECT_DIR=str(proj),
        VCT_STATE_DIR=str(proj / "vctstate"),
        VCO_ROUTER_KG_SCRIPT=str(stub),
        VCO_CG_SCRIPT=str(proj / "absent_cg.py"),
        VCO_STUB_TASK_ID=f"pre_edit_{prompt_id}",
        VCO_TEST_STUB_SID=sid,
        VCO_TEST_ROUTER_REPO=str(REPO_ROOT),
        PYTHONPATH=str(REPO_ROOT),
        RL_HUB_POST_DISABLED="1",
        **(extra_env or {}),
    )
    env.pop("VCO_INJECT_PROFILE", None)
    r = subprocess.run(
        [sys.executable, str(ROUTER), "bash"],
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, cwd=str(proj), env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


@needs_bash
class TestW3R10RouterSuppressesStagedCitations:
    def test_repeat_query_stages_no_citation_labels_for_unshown_nodes(
            self, tmp_path: Path) -> None:
        """The audit's red-proof: a repeat-within-session query (cache miss,
        same nodes already injected) must NOT leave a pending ctx whose
        nodes the model never saw."""
        proj = tmp_path / "proj"
        (proj / ".claude" / "state").mkdir(parents=True)
        (proj / "notes.md").write_text("note\n", encoding="utf-8")
        stub = tmp_path / "kg_stub_w3r10.py"
        stub.write_text(_KG_STUB_W3R10, encoding="utf-8")
        sid = "sess-w3r10"

        out_a = _router_run(proj, "pa", stub, sid)
        assert "KG: Stub Node" in out_a, "run A injects the block"
        pend = proj / ".claude" / "state" / "rl_pending"
        file_a = pend / f"{sid}__pre_edit_pa.json"
        assert file_a.is_file(), "run A stages its pending ctx"
        assert json.loads(file_a.read_text())["ctx"]["nodes"], "with nodes"

        # Run B: different prompt_id → cache MISS → the leg runs again
        # (fresh event + fresh pending file), but the seen-store suppresses
        # the block → nothing reaches the transcript.
        out_b = _router_run(proj, "pb", stub, sid)
        assert "KG: Stub Node" not in out_b, "run B's block is suppressed"
        file_b = pend / f"{sid}__pre_edit_pb.json"
        assert not file_b.exists(), (
            "a fully-suppressed retrieval must not keep a citation ctx for "
            "nodes the model never saw (W3R-10)")
        # run A's ctx is untouched — its nodes WERE shown
        assert json.loads(file_a.read_text())["ctx"]["nodes"]

    def test_sibling_concurrent_staging_in_same_session_is_not_amended(
            self, tmp_path: Path) -> None:
        """Batch-8 MEDIUM: a sibling router's ctx staged DURING this run's
        legs (same session, newer than any pre-legs snapshot, but NOT this
        run's) must survive this run's suppression decision untouched."""
        proj = tmp_path / "proj"
        (proj / ".claude" / "state").mkdir(parents=True)
        (proj / "notes.md").write_text("note\n", encoding="utf-8")
        stub = tmp_path / "kg_stub_w3r10.py"
        stub.write_text(_KG_STUB_W3R10, encoding="utf-8")
        sid = "sess-w3r10"

        _router_run(proj, "pa", stub, sid)  # seen-store now knows the block
        out_b = _router_run(proj, "pb", stub, sid,
                            extra_env={"VCO_STUB_WRITE_SIBLING": "1"})
        assert "KG: Stub Node" not in out_b, "run B's own block is suppressed"
        pend = proj / ".claude" / "state" / "rl_pending"
        assert not (pend / f"{sid}__pre_edit_pb.json").exists(), (
            "run B's OWN fully-suppressed ctx is removed (W3R-10)")
        sibling = pend / f"{sid}__pre_edit_sib.json"
        assert sibling.is_file(), (
            "a sibling's staged ctx must not be amended by this run's "
            "decision (batch-8 MEDIUM)")
        assert json.loads(sibling.read_text())["ctx"]["nodes"], (
            "the sibling's shown nodes must survive" )


class TestW3R10AmendPendingCitations:
    def _stage(self, proj: Path, sid: str, task: str, titles: list,
               run_id: str = "") -> Path:
        """Stage a pending ctx directly (run_id "" = not this router's)."""
        from claude_mcp_servers.rl_client.citation_pending import stage_pending

        ctx = {"nodes": [{"title": t, "n_emb": [0.1]} for t in titles],
               "active_model": "m", "task_type": "pre_edit_kg_search"}
        if run_id:
            ctx["router_run_id"] = run_id
        return stage_pending(
            session_id=sid, task_id=task, seq=None, query="q", ctx=ctx,
            source="hook", project_root=str(proj))

    def test_partial_suppression_keeps_file_drops_node(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        (proj / ".claude" / "state" / "rl_pending").mkdir(parents=True)
        p = self._stage(proj, "sess-p", "pre_edit_p", ["Shown", "Hidden"],
                        run_id="router_1")
        router._amend_pending_citations(str(proj), "sess-p", ["Hidden"],
                                        "router_1")
        ctx = json.loads(p.read_text())["ctx"]
        assert [n["title"] for n in ctx["nodes"]] == ["Shown"]
        assert ctx["router_suppressed_titles"] == ["Hidden"]

    def test_full_suppression_deletes_the_ctx(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        (proj / ".claude" / "state" / "rl_pending").mkdir(parents=True)
        p = self._stage(proj, "sess-f", "pre_edit_f", ["Only"],
                        run_id="router_1")
        router._amend_pending_citations(str(proj), "sess-f", ["Only"],
                                        "router_1")
        assert not p.exists(), "no shown nodes → nothing citable → delete"

    def test_other_sessions_and_sibling_runs_untouched(
            self, tmp_path: Path) -> None:
        """Batch-8 MEDIUM: a sibling concurrent router's ctx in the SAME
        session (freshly staged, its OWN run id — or unstamped) must never
        be amended by this run's decision."""
        proj = tmp_path / "proj"
        (proj / ".claude" / "state" / "rl_pending").mkdir(parents=True)
        other = self._stage(proj, "sess-other", "pre_edit_o", ["Hidden"],
                            run_id="router_1")
        sibling = self._stage(proj, "sess-s", "pre_edit_sib", ["Hidden"],
                              run_id="router_sibling")
        unstamped = self._stage(proj, "sess-s", "pre_edit_old", ["Hidden"])
        router._amend_pending_citations(str(proj), "sess-s", ["Hidden"],
                                        "router_1")
        for p, what in ((other, "other session"), (sibling, "sibling run"),
                        (unstamped, "unstamped pre-fix file")):
            assert json.loads(p.read_text())["ctx"]["nodes"], what

    def test_stage_pending_passthrough_outside_a_router_run(
            self, tmp_path: Path) -> None:
        """Full-suite regression (batch-8 follow-up): an IN-PROCESS
        ``router.main()`` (the replay harness does this) must not leave the
        staged-ctx wrapper stamping later, unrelated ``stage_pending``
        calls — the run id is cleared on exit and the wrapper is a pure
        passthrough while none is in flight."""
        assert router.main(["bash"]) == 0
        assert router._CURRENT_RUN_ID == "", "run id must clear on exit"
        p = self._stage(tmp_path, "sess-after", "pre_edit_after", ["N"],
                        run_id="")
        ctx = json.loads(p.read_text())["ctx"]
        assert "router_run_id" not in ctx, (
            f"a stage outside a router run must be untouched: {ctx}")


class TestW3R10FilterSuppressedOut:
    def test_suppressed_blocks_report_their_title(self, tmp_path: Path) -> None:
        inject = tmp_path / "seen_inject_s10.txt"
        text = "KG: A | concept | score=0.90 | TITLES\nKG: B | concept | score=0.90 | TITLES\n"
        # first call records A
        router.filter_seen_blocks("KG: A | concept | score=0.90 | TITLES\n",
                                  str(inject), "", str(tmp_path))
        supp: list = []
        out = router.filter_seen_blocks(text, str(inject), "", str(tmp_path),
                                        suppressed_out=supp)
        assert "KG: A" not in out and "KG: B" in out
        assert supp == ["A"], supp

    def test_lock_contention_decides_nothing(self, tmp_path: Path) -> None:
        from vco_lib.atomic import exclusive_file_lock

        inject = tmp_path / "seen_inject_s11.txt"
        inject.write_text("", encoding="utf-8")
        supp: list = []
        with exclusive_file_lock(Path(str(inject) + ".lock")):
            out = router.filter_seen_blocks(
                "KG: B | concept\nbody\n", str(inject), "", str(tmp_path),
                suppressed_out=supp)
        assert out == ""
        assert supp == [], "contention silence is NOT a suppression decision"
