# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.102 W6-R6FIX — the three residuals of batch review R6.

R6-2 (LOW): a pending citation that reached its 60-minute TTL was DELETED
by the sweep without a score. v0.2.102 W6-RL2 (owner rulings R7-3 / R7-4)
then retired the TTL itself; the R6-2 scenarios are covered under the new
rules in ``test_v02102_w6rl2_no_scoring_deadline.py``. What stays here is
the same-folder rule of ``session_transcript_for`` (no guesses, no
cross-folder search).

R6-1 (INFO): ``sweep_orphan_alerts`` also sweeps a ``.delivering.`` claim
older than ``ORPHAN_AFTER_SECONDS`` (act / leave-alone / log-unwritable).

R6-3 (INFO): ``pre-bash-context-inject.ps1`` says on stderr that
``_lib/utf8-nobom.ps1`` is missing (a broken install) instead of skipping
the pairing write in silence; the hook still exits 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import citation_pending as cp  # noqa: E402


def _write_transcript(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _holder() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            env=child_env())


# ── R6-2: the TTL sweep these tests drove is gone ───────────────────────────
#
# v0.2.102 W6-RL2 (owner rulings R7-3 / R7-4) retired the 60-minute TTL: a
# pending citation now waits for its own chat's end, and the only time bound
# left is the abandoned-file collector. The R6-2 scenarios (scored once,
# against the right transcript; unscored files counted and logged; the claim
# keeping scorers apart) are covered under the new rules in
# ``tests/test_v02102_w6rl2_no_scoring_deadline.py``.


def test_session_transcript_for_refuses_guesses(tmp_path: Path) -> None:
    tdir = tmp_path / "t"
    parent = _write_transcript(tdir / "s1.jsonl", "x")
    agent = _write_transcript(tdir / "s1" / "subagents" / "agent-a.jsonl", "y")
    assert cp.session_transcript_for(parent, "") == parent
    assert cp.session_transcript_for(agent, "") == parent
    assert cp.session_transcript_for(agent, "s1") == parent
    assert cp.session_transcript_for(parent, "s2") == "", "no such file"
    assert cp.session_transcript_for(parent, "../s1") == "", "not filesystem-safe"
    assert cp.session_transcript_for("", "s1") == ""
    assert cp.session_transcript_for(str(tdir / "x.txt"), "s1") == ""


# ── R6-1: an abandoned delivery claim is swept after 7 days ────────────────


def _backdate(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


class TestOrphanDeliveryClaim:
    def _claim(self, root: str, holder_pid: int, age_s: float) -> Path:
        from vco_lib import agent_alerts as aa

        p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "stuck in a claim")
        assert p
        claim = p.with_name(f"{p.name}{aa.DELIVERING_MARK}{holder_pid}")
        p.replace(claim)
        _backdate(claim, age_s)
        return claim

    def test_eight_day_old_live_claim_moves_to_the_undelivered_log(
            self, tmp_path: Path, monkeypatch) -> None:
        """The R6-1 case end-to-end: pid alive, start time unknown, so the
        re-adoption keeps it; the orphan sweep (run by every delivery) logs
        its rows and removes it."""
        from vco_lib import agent_alerts as aa
        from vco_lib import update_lock

        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: None)
        root = str(tmp_path)
        h = _holder()
        try:
            claim = self._claim(root, h.pid, 8 * 86400)
            assert aa.readopt_stale_deliveries(root) == 0 and claim.exists(), (
                "precondition: the re-adoption cannot take it")
            assert aa.deliver_for_payload(root, {"hook_event_name": "PostToolUse",
                                                 "tool_name": "Bash", "session_id": "s1",
                                                 "tool_input": {"command": "ls"}}) == ""
            assert not claim.exists()
            log = tmp_path / aa.UNDELIVERED_LOG_REL
            rows = [json.loads(ln) for ln in log.read_text().splitlines()]
            assert [(r["message"], r["undelivered_reason"]) for r in rows] == [
                ("stuck in a claim", "orphaned_delivery_claim")]
            assert rows[0]["swept_at"]
            assert not list((tmp_path / ".claude" / "state").glob("agent_alerts_*"))
        finally:
            h.kill()
            h.wait()

    def test_six_day_old_claim_is_left(self, tmp_path: Path, monkeypatch) -> None:
        from vco_lib import agent_alerts as aa
        from vco_lib import update_lock

        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: None)
        h = _holder()
        try:
            claim = self._claim(str(tmp_path), h.pid, 6 * 86400)
            assert aa.sweep_orphan_alerts(str(tmp_path)) == 0
            assert claim.exists()
            assert not (tmp_path / aa.UNDELIVERED_LOG_REL).exists()
        finally:
            h.kill()
            h.wait()

    def test_unwritable_log_keeps_the_claim(self, tmp_path: Path) -> None:
        from vco_lib import agent_alerts as aa

        h = _holder()
        try:
            claim = self._claim(str(tmp_path), h.pid, 8 * 86400)
            (tmp_path / aa.UNDELIVERED_LOG_REL).mkdir(parents=True)
            assert aa.sweep_orphan_alerts(str(tmp_path)) == 0
            assert [r["message"] for r in aa._read_rows(claim)] == ["stuck in a claim"]
        finally:
            h.kill()
            h.wait()


# ── R6-3: a missing _lib/utf8-nobom.ps1 is reported, not silent ────────────


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
class TestPreBashPs1MissingUtf8Helper:
    NOTICE = "[VCO broken install] .claude/hooks/_lib/utf8-nobom.ps1 is missing"

    def _run(self, tmp_path: Path, hooks_dir: Path):
        from tests.test_v02102_rl_w3r_pairing_and_suppression import PairingRig
        from tests.test_v02101_router_surfaces import _bash_payload

        rig = PairingRig(tmp_path)
        payload = _bash_payload("grep -rn vco_query_cache_put templates/",
                                session="sess-r63", cwd=str(rig.proj))
        payload["tool_use_id"] = "toolu_r63"
        r = subprocess.run(
            ["pwsh", *PWSH_BASE_FLAGS, "-File", str(hooks_dir / "pre-bash-context-inject.ps1")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=120,
            cwd=str(rig.proj), env=rig.env())
        return rig, r

    def test_missing_helper_is_one_loud_stderr_line_and_exit_0(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("utf8-nobom.ps1"))
        assert not (hooks / "_lib" / "utf8-nobom.ps1").exists()
        rig, r = self._run(tmp_path, hooks)
        assert r.returncode == 0, r.stderr
        assert r.stderr.count(self.NOTICE) == 1, r.stderr[-800:]
        assert rig.bash_task_files() == [], "no pairing state could be written"

    def test_healthy_install_says_nothing_and_writes_the_state(self, tmp_path: Path) -> None:
        rig, r = self._run(tmp_path, HOOKS)
        assert r.returncode == 0, r.stderr
        assert self.NOTICE not in r.stderr
        assert len(rig.bash_task_files()) == 1



def test_session_transcript_is_not_searched_in_another_project_folder(tmp_path):
    """Owner ruling R7-3 (W6-RL2): a VCO chat is always started in the
    project's root, so only the same-folder sibling is a session's
    transcript. A session file in a sibling project folder is NOT found —
    the citation stays unscored rather than being scored against a guess
    (the cross-folder fallback of the R6-2 coordinator commit is gone)."""
    from claude_mcp_servers.rl_client.citation_pending import session_transcript_for

    root = tmp_path / "projects"
    here = root / "-home-u-proj"
    there = root / "-home-u-proj-sub"
    here.mkdir(parents=True)
    there.mkdir(parents=True)
    known = here / "11111111-1111-1111-1111-111111111111.jsonl"
    known.write_text("{}\n", encoding="utf-8")
    other_sid = "22222222-2222-2222-2222-222222222222"
    (there / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_transcript_for(str(known), other_sid) == "", (
        "exactly one match in another folder is still not this project's chat")
    # The same-folder sibling is found.
    (here / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_transcript_for(str(known), other_sid) == str(here / f"{other_sid}.jsonl")
