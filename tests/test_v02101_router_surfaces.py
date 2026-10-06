# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-C1/C3 — the bash/edit/write wrappers drive the router.

RED on the base tree (HEAD before this lane): pre-write-context-inject.{sh,ps1}
do not exist, and pre-bash/pre-edit still run the OLD inline paths (no router
spawn → the stub producers never record → every argv/envelope assertion
fails).

Shape (plan §WP-F row 3, bash/edit/write rows): each wrapper is driven with a
fake stdin payload and STUB producers (the router's documented test seams
``VCO_ROUTER_KG_SCRIPT`` / ``VCO_CG_SCRIPT``), asserting:
  * MECHANICAL → no producer spawn, no output, no state;
  * READ/SEARCH/EDIT/WRITE → the EXACT producer argv the §2.1 design demands
    (structure callers <symbol> --hook-format …; --injection-profile <surface>;
    --task-type <rl type>) recorded by the stubs;
  * the envelope shape emit_additional_context produces (hookSpecificOutput /
    hookEventName / additionalContext, NO permissionDecision);
  * the kill switches (VCT_DISABLE_HOOKS, VCO_INJECT_PROFILE=off) suppress
    everything before any spawn;
  * the pre-edit per-file replay cache serves a second identical edit WITHOUT
    re-spawning the router (and dedupes it to silence through the CURRENT
    seen-state);
  * one pwsh-driven parity case per wrapper family where pwsh exists.

The stub producers parse their pinned argv through the SAME
``hook_dual_search._pin_argv`` shim the real producers see (REMAINDER
positional), so the recorded argv is byte-for-byte what the router passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed")

# --- stub producers (the router's documented seams) ---------------------------

_KG_STUB = '''\
import argparse, json, os

def main():
    rec = os.environ.get("VCO_STUB_KG_RECORD", "")
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args()
    if rec:
        with open(rec, "a", encoding="utf-8") as fh:
            json.dump(ns.rest, fh)
            fh.write("\\n")
    print("KG: Stub Node | concept | score=0.90 | TITLES")
    return 0
'''

_CG_STUB = '''\
import argparse, json, os

def main():
    rec = os.environ.get("VCO_STUB_CG_RECORD", "")
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args()
    if rec:
        with open(rec, "a", encoding="utf-8") as fh:
            json.dump(ns.rest, fh)
            fh.write("\\n")
    if "--indexed-revision" in ns.rest:
        print("CODE-REV: stubrev000")
    print("CODE: stub.symbol | function | def stub.py:1 | callers: (none) | src=stub.py")
    return 0
'''


class Rig:
    """The wrapper-test rig: a fake project + a fake venv whose python runs
    the REAL router (VCT_ORCHESTRATOR_ROOT=checkout) with STUB producers."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.proj = tmp_path / "proj"
        (self.proj / ".claude" / "state").mkdir(parents=True)
        venv = tmp_path / "orch" / ".venv"
        (venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, venv / "bin" / "python")
        self.kg_stub = tmp_path / "kg_stub.py"
        self.cg_stub = tmp_path / "cg_stub.py"
        self.kg_stub.write_text(_KG_STUB, encoding="utf-8")
        self.cg_stub.write_text(_CG_STUB, encoding="utf-8")
        self.kg_record = tmp_path / "kg_records.jsonl"
        self.cg_record = tmp_path / "cg_records.jsonl"

    def env(self, **overrides: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("VCT_DISABLE_HOOKS", "VCT_INSTALL_ROOT",
                            "VCT_ORCHESTRATOR_ROOT", "VCO_INJECT_PROFILE",
                            "VCO_RL_TASK_TYPE", "VCT_VENV")}
        env.update({
            "CLAUDE_PROJECT_DIR": str(self.proj),
            "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),  # the REAL router
            "VCT_VENV": str(self.tmp / "orch" / ".venv"),
            "VCO_ROUTER_KG_SCRIPT": str(self.kg_stub),
            "VCO_CG_SCRIPT": str(self.cg_stub),
            "VCO_STUB_KG_RECORD": str(self.kg_record),
            "VCO_STUB_CG_RECORD": str(self.cg_record),
            "VCT_STATE_DIR": str(self.tmp / "vctstate"),
            "HOME": str(self.tmp / "home"),
            "KG_COLLECTION": "RouterProjKG",
            "RL_HUB_POST_DISABLED": "1",
        })
        env.update(overrides)
        return env

    def run(self, hook: str, payload: dict, shell: str = "sh",
            env_overrides: dict | None = None) -> subprocess.CompletedProcess:
        env = self.env(**(env_overrides or {}))
        if shell == "ps1":
            argv = ["pwsh", "-NoProfile", "-File", str(HOOKS / f"{hook}.ps1")]
        else:
            argv = ["bash", str(HOOKS / f"{hook}.sh")]
        return subprocess.run(
            argv, input=json.dumps(payload), capture_output=True, text=True,
            timeout=120, cwd=str(self.proj), env=env,
        )

    def kg_records(self) -> list:
        return _read_records(self.kg_record)

    def cg_records(self) -> list:
        return _read_records(self.cg_record)

    def state_files(self) -> list:
        return sorted((self.proj / ".claude" / "state").glob("bash_task_*.json"))


def _read_records(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text("utf-8").splitlines() if ln.strip()]


def _bash_payload(command: str, session: str = "sess-rs-1", prompt: str = "p-1",
                  cwd: str | None = None) -> dict:
    return {"tool_name": "Bash", "session_id": session, "prompt_id": prompt,
            "cwd": cwd or "", "tool_input": {"command": command}}


def _envelope(stdout: str) -> dict:
    """Parse the emit_additional_context envelope (last JSON line)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON envelope in stdout: {stdout!r}")


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- C1: the bash wrapper ------------------------------------------------------


@needs_bash
class TestBashWrapper:
    def test_mechanical_no_spawn_no_output(self, rig: Rig) -> None:
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("cargo clippy --workspace", cwd=str(rig.proj)))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == [] and rig.cg_records() == []
        assert rig.state_files() == []

    def test_search_exact_symbol_argv_and_envelope(self, rig: Rig) -> None:
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("grep -rn vco_seen_add templates/",
                                  cwd=str(rig.proj)))
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg, f"CG leg must run for SEARCH: {r.stderr[-500:]}"
        argv = cg[0]
        assert argv[:3] == ["structure", "callers", "vco_seen_add"]
        assert "--hook-format" in argv
        assert rig.kg_records() == [], "SEARCH runs NO KG leg (§2.1)"
        env = _envelope(r.stdout)
        assert env["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in json.dumps(env)
        ctx = env["hookSpecificOutput"]["additionalContext"]
        assert "CODE: stub.symbol" in ctx
        assert "[Pre-bash context for:" in ctx

    def test_read_file_target_uses_pub_symbols_and_profile(self, rig: Rig) -> None:
        src = rig.proj / "src"
        src.mkdir()
        (src / "main.rs").write_text("pub fn widget_loader() -> u32 {\n    1\n}\n",
                                     encoding="utf-8")
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("cat src/main.rs", cwd=str(rig.proj)))
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg, r.stderr[-500:]
        assert cg[0][:3] == ["structure", "callers", "widget_loader"]
        assert "--indexed-revision" in cg[0], "bash_read carries the rev stamp"
        kg = rig.kg_records()
        assert kg, "READ with a target runs the KG leg"
        argv = kg[0]
        assert "--injection-profile" in argv
        assert argv[argv.index("--injection-profile") + 1] == "bash_read"
        assert argv[argv.index("--task-type") + 1] == "pre_bash_kg_search"
        assert "--hook-format" in argv
        # the query is built from the TARGET PATH topic — never command text
        assert "widget" in argv[0] or "main" in argv[0]
        assert "cat" not in argv[0]

    def test_kill_switch_profile_off(self, rig: Rig) -> None:
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("grep -rn vco_seen_add templates/",
                                  cwd=str(rig.proj)),
                    env_overrides={"VCO_INJECT_PROFILE": "off"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == [] and rig.cg_records() == []
        assert rig.state_files() == []

    def test_disable_hooks_switch(self, rig: Rig) -> None:
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("grep -rn vco_seen_add templates/",
                                  cwd=str(rig.proj)),
                    env_overrides={"VCT_DISABLE_HOOKS": "1"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == [] and rig.cg_records() == []

    def test_non_bash_tool_skipped(self, rig: Rig) -> None:
        payload = {"tool_name": "Read", "session_id": "s", "prompt_id": "p",
                   "tool_input": {"file_path": str(rig.proj / "x.py")}}
        r = rig.run("pre-bash-context-inject", payload)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_kg_leg_receives_transcript_path(self, rig: Rig) -> None:
        """WP-E behavioural pin (the hook-level call-site pins retired with
        the wrappers): transcript_path travels payload → router → the KG
        producer's argv as a PATH — never the transcript's contents."""
        transcript = str(rig.tmp / "live-transcript.jsonl")
        payload = _bash_payload("cat src/main.rs", cwd=str(rig.proj))
        payload["transcript_path"] = transcript
        src = rig.proj / "src"
        src.mkdir(exist_ok=True)
        (src / "main.rs").write_text("pub fn w() -> u32 { 1 }\n", encoding="utf-8")
        r = rig.run("pre-bash-context-inject", payload)
        assert r.returncode == 0, r.stderr
        kg = rig.kg_records()
        assert kg, r.stderr[-800:]
        argv = kg[0]
        assert "--transcript" in argv
        assert argv[argv.index("--transcript") + 1] == transcript

    def test_state_file_written_for_classified_command(self, rig: Rig) -> None:
        cmd = "grep -rn vco_seen_add templates/"
        rig.run("pre-bash-context-inject", _bash_payload(cmd, cwd=str(rig.proj)))
        states = rig.state_files()
        assert len(states) == 1
        want_hash = hashlib.md5(cmd.encode()).hexdigest()[:16]
        assert states[0].name == f"bash_task_sess-rs-1_{want_hash}.json"
        state = json.loads(states[0].read_text())
        assert state["task_id"].startswith("pre_bash_")
        assert state["intent"] == "SEARCH"
        assert "vco_seen_add" in state["symbols"]


# --- C3: the edit wrapper ------------------------------------------------------


def _edit_payload(rig: Rig, name: str = "mod.py", body: str | None = None,
                  old: str = "original_line = 1", new: str = "original_line = 2",
                  session: str = "sess-rs-e") -> tuple[dict, Path]:
    f = rig.proj / name
    if body is None:
        body = "def target_function():\n    original_line = 1\n    return original_line\n"
    f.write_text(body, encoding="utf-8")
    return {"tool_name": "Edit", "session_id": session, "prompt_id": "p-1",
            "cwd": str(rig.proj),
            "tool_input": {"file_path": str(f), "old_string": old,
                           "new_string": new}}, f


@needs_bash
class TestEditWrapper:
    def test_edit_exact_symbol_and_profile(self, rig: Rig) -> None:
        payload, f = _edit_payload(rig)
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg, r.stderr[-800:]
        argv = cg[0]
        assert argv[:3] == ["structure", "callers", "target_function"]
        assert "--hook-format" in argv
        assert argv[argv.index("--source-file") + 1] == str(f)
        assert argv[argv.index("--exclude-file") + 1] == str(f)
        assert "--indexed-revision" not in argv  # edit rows carry no stamp
        kg = rig.kg_records()
        assert kg
        kargv = kg[0]
        assert kargv[kargv.index("--injection-profile") + 1] == "edit"
        assert kargv[kargv.index("--task-type") + 1] == "pre_edit_kg_search"
        assert "target_function" in kargv[0]
        env = _envelope(r.stdout)
        assert "[Pre-edit context for mod.py]" in \
            env["hookSpecificOutput"]["additionalContext"]

    def test_replay_cache_serves_second_edit_without_router(self, rig: Rig) -> None:
        payload, _f = _edit_payload(rig)
        r1 = rig.run("pre-edit-context-inject", payload)
        assert r1.returncode == 0 and r1.stdout.strip(), r1.stderr[-800:]
        n_kg, n_cg = len(rig.kg_records()), len(rig.cg_records())
        assert n_kg >= 1
        # Second identical edit: the per-file replay cache must serve it with
        # NO router spawn, and the seen-store (which the router populated on
        # run 1) dedupes the replay to silence.
        r2 = rig.run("pre-edit-context-inject", payload)
        assert r2.returncode == 0, r2.stderr
        assert len(rig.kg_records()) == n_kg, "replay must NOT re-spawn the router"
        assert len(rig.cg_records()) == n_cg
        assert r2.stdout.strip() == "", "replay of already-seen blocks is silence"
        cache_log = rig.proj / ".claude" / "state" / "preedit_cache_log.jsonl"
        statuses = [json.loads(ln)["status"]
                    for ln in cache_log.read_text().splitlines() if ln.strip()]
        assert statuses == ["miss", "hit"]

    def test_kill_switch_profile_off(self, rig: Rig) -> None:
        payload, _f = _edit_payload(rig)
        r = rig.run("pre-edit-context-inject", payload,
                    env_overrides={"VCO_INJECT_PROFILE": "off"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == [] and rig.cg_records() == []


# --- C3: the write wrapper ------------------------------------------------------


@needs_bash
class TestWriteWrapper:
    def test_hook_files_exist(self) -> None:
        assert (HOOKS / "pre-write-context-inject.sh").exists()
        assert (HOOKS / "pre-write-context-inject.ps1").exists()

    def test_new_file_kg_only_write_profile(self, rig: Rig) -> None:
        target = rig.proj / "brand_new.py"
        payload = {"tool_name": "Write", "session_id": "sess-rs-w",
                   "prompt_id": "p-1", "cwd": str(rig.proj),
                   "tool_input": {"file_path": str(target),
                                  "content": "def fresh_thing():\n    pass\n"}}
        r = rig.run("pre-write-context-inject", payload)
        assert r.returncode == 0, r.stderr
        kg = rig.kg_records()
        assert kg, r.stderr[-800:]
        kargv = kg[0]
        assert kargv[kargv.index("--injection-profile") + 1] == "write"
        assert kargv[kargv.index("--task-type") + 1] == "pre_write_kg_search"
        # module-name topic (kg_query_for_targets splits the stem into
        # words), never the file content
        assert "brand" in kargv[0] and "new" in kargv[0]
        assert "fresh_thing" not in kargv[0]
        assert rig.cg_records() == [], (
            "a brand-new file's symbols are not indexed — no CG leg")
        env = _envelope(r.stdout)
        assert "[Pre-write context for brand_new.py]" in \
            env["hookSpecificOutput"]["additionalContext"]

    def test_rewrite_of_existing_file_gets_cg_leg(self, rig: Rig) -> None:
        target = rig.proj / "existing.py"
        target.write_text("def legacy_symbol():\n    pass\n", encoding="utf-8")
        payload = {"tool_name": "Write", "session_id": "sess-rs-w2",
                   "prompt_id": "p-1", "cwd": str(rig.proj),
                   "tool_input": {"file_path": str(target),
                                  "content": "def legacy_symbol():\n    return 1\n"}}
        r = rig.run("pre-write-context-inject", payload)
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg and cg[0][:3] == ["structure", "callers", "legacy_symbol"]

    def test_non_write_tool_skipped(self, rig: Rig) -> None:
        payload = {"tool_name": "Edit", "session_id": "s", "prompt_id": "p",
                   "tool_input": {"file_path": str(rig.proj / "x.py")}}
        r = rig.run("pre-write-context-inject", payload)
        assert r.returncode == 0 and r.stdout.strip() == ""
        assert rig.kg_records() == []


# --- ps1 parity (one behavioural case per family where pwsh exists) -------------


@needs_pwsh
class TestPs1Parity:
    def test_bash_ps1_search_matches_sh(self, rig: Rig) -> None:
        r = rig.run("pre-bash-context-inject",
                    _bash_payload("grep -rn vco_seen_add templates/",
                                  session="sess-ps1-1", cwd=str(rig.proj)),
                    shell="ps1")
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg and cg[0][:3] == ["structure", "callers", "vco_seen_add"]
        env = _envelope(r.stdout)
        assert "CODE: stub.symbol" in env["hookSpecificOutput"]["additionalContext"]

    def test_edit_ps1_matches_sh(self, rig: Rig) -> None:
        payload, f = _edit_payload(rig, session="sess-ps1-e")
        r = rig.run("pre-edit-context-inject", payload, shell="ps1")
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg and cg[0][:3] == ["structure", "callers", "target_function"]
        assert rig.kg_records(), "the KG leg must run on the ps1 path too"

    def test_write_ps1_matches_sh(self, rig: Rig) -> None:
        target = rig.proj / "ps1_new.py"
        payload = {"tool_name": "Write", "session_id": "sess-ps1-w",
                   "prompt_id": "p-1", "cwd": str(rig.proj),
                   "tool_input": {"file_path": str(target), "content": "x = 1\n"}}
        r = rig.run("pre-write-context-inject", payload, shell="ps1")
        assert r.returncode == 0, r.stderr
        kg = rig.kg_records()
        assert kg and kg[0][kg[0].index("--injection-profile") + 1] == "write"
