# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""kg-sync is no longer silent — on success OR on failure (v0.2.95).

THE DEFECT (field report 2026-09-14). A session's worth of knowledge
writes appeared to sync and did not:

* the EDIT path ran the sync with `>/dev/null 2>&1 || true`, so `kg-sync`'s
  exit-3 refusal ("I did NOT run: no interpreter with VCO's KG dependencies")
  was invisible on every edit, forever;
* the SUCCESS path said nothing at all, so "which of the four trees on this
  machine holding a `weaviate_mcp` did it actually use?" was unanswerable from
  the outside — three of those four were the wrong tree;
* SessionStart checked RETRIEVAL only, so the stale index kept reporting
  health while the write path was dead.

WHAT THESE TESTS PIN (bash side; the `.ps1` siblings carry the same logic and
are parse-gated + structurally mirrored — an end-to-end pwsh run of the
detached-child machinery is not in this lane's budget):

1. kg-sync prints ONE `[kg-sync] venv: … (tier: …)` line on STDERR and keeps
   stdout as the sync's own surface.
2. The tier label names the rung that actually answered.
3. A FAILING debounced sync writes its stderr to `.claude/logs/kg-sync-hook.log`
   and appends exactly ONE row to `kg_sync_failures.jsonl` per session+channel.
4. A SUCCEEDING sync writes NO row (the leave-alone half).
5. SessionStart reports the write path, and surfaces a recorded failure once.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import textwrap
import time
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "templates" / "scripts"
HOOKS = REPO_ROOT / "templates" / "hooks"
DEBOUNCE_LIB = HOOKS / "_lib" / "kg-sync-debounce.sh"

pytestmark = [
    pytest.mark.skipif(
        platform.system() == "Windows", reason="bash surfaces; .ps1 siblings mirror",
    ),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH"),
]


# ── fixtures ───────────────────────────────────────────────────────────────


def _fake_clone(root: Path) -> Path:
    """A directory the ladder accepts as an orchestrator clone, with a venv
    whose `python` PASSES the import probe.

    The interpreter is a shell shim: the ladder's qualification step runs
    `python -c "<probe>"`, so a shim that exits 0 for `-c` and execs the real
    interpreter otherwise qualifies without installing weaviate into a temp
    venv. What is under test is the ladder's ATTRIBUTION and the wrapper's
    disclosure, not the import itself.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "install.py").write_text("", encoding="utf-8")
    (root / "first-install.sh").write_text("", encoding="utf-8")
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = bindir / "python"
    py.write_text(
        '#!/bin/bash\nif [ "$1" = "-c" ]; then exit 0; fi\nexec /usr/bin/env python3 "$@"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    return root


def _stage_project(tmp_path: Path) -> Path:
    """A project with the shipped kg-sync unit installed where the bundle puts
    it, plus an observable fake sync target."""
    from tests.common.wrapper_staging import stage_scripts

    proj = tmp_path / "proj"
    scripts = proj / ".claude" / "scripts"
    stage_scripts(scripts, "kg-sync")
    (scripts / "sync_knowledge_graph.py").write_text(
        textwrap.dedent(
            """
            import sys
            sys.stdout.write("SYNC-RAN " + " ".join(sys.argv[1:]) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    return proj


def _clean_env(**overrides: str) -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")
    }
    env.update(overrides)
    return env


def _run_kg_sync(proj: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "kg-sync"), "--all"],
        capture_output=True, text=True, cwd=str(proj), env=env, timeout=120,
    )


# ── 1 + 2: success disclosure ──────────────────────────────────────────────


class SuccessDisclosureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-kgsync-vis-"))
        self.proj = _stage_project(self.tmp)
        self.clone = _fake_clone(self.tmp / "orch")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_success_names_the_interpreter_on_stderr(self):
        proc = _run_kg_sync(self.proj, _clean_env(VCT_VENV=str(self.clone / ".venv")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SYNC-RAN --all", proc.stdout)
        self.assertIn("[kg-sync] venv:", proc.stderr)
        self.assertIn(str(self.clone / ".venv" / "bin" / "python"), proc.stderr)
        self.assertNotIn(
            "[kg-sync] venv:", proc.stdout,
            "the disclosure must not pollute stdout — the launcher's kg_sync.rs "
            "parses that stream",
        )
        lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("[kg-sync] venv:")]
        self.assertEqual(len(lines), 1, f"exactly ONE line, got {lines}")

    def test_tier_label_names_the_rung_that_answered(self):
        proc = _run_kg_sync(self.proj, _clean_env(VCT_VENV=str(self.clone / ".venv")))
        self.assertIn("(tier: VCT_VENV)", proc.stderr)

        proc = _run_kg_sync(self.proj, _clean_env(VCT_INSTALL_ROOT=str(self.clone)))
        self.assertIn("(tier: VCT_INSTALL_ROOT)", proc.stderr)

    def test_file_backed_tier_is_attributed_to_claude_env(self):
        """The DURABLE tier — the one whose absence caused the field failure.
        When it is what answered, the disclosure says so by name."""
        (self.proj / ".claude").mkdir(parents=True, exist_ok=True)
        (self.proj / ".claude" / "env").write_text(
            f'export VCT_ORCHESTRATOR_ROOT="{self.clone}"\n', encoding="utf-8",
        )
        proc = _run_kg_sync(self.proj, _clean_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(tier: .claude/env VCT_ORCHESTRATOR_ROOT)", proc.stderr)

    def test_a_refusal_still_says_nothing_about_a_venv(self):
        """The leave-alone half of the disclosure: no success line on a run
        that did not run."""
        proc = _run_kg_sync(self.proj, _clean_env())
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertNotIn("[kg-sync] venv:", proc.stderr)
        self.assertIn("kg-sync: ERROR - no Python environment", proc.stderr)


# ── 3 + 4: the edit path records its failures ──────────────────────────────


class DebounceFailureRecordingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-kgfail-"))
        self.proj = self.tmp / "proj"
        (self.proj / ".claude" / "state").mkdir(parents=True)
        self.state = self.tmp / "vct-state"
        self.state.mkdir()
        # `_lib/metrics-dir.sh` keeps writers on the LEGACY archive while a
        # verified copy is still owed, so an empty archive is what pins the
        # write target at `$VCT_STATE_DIR/metrics` for this test. Without it
        # the rows land in whatever archive the host happens to have, and the
        # test would be asserting about the machine, not the code.
        self.claude_dir = self.tmp / "claude-home"
        self.claude_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _sync_script(self, name: str, body: str) -> Path:
        """Write the 'sync command' as a SCRIPT and schedule `sh <path>`.

        Deliberately not an inline one-liner: the command string is embedded in
        a shell script by the harness AND shell-quoted again by the lib, and an
        inline command carrying its own quotes silently re-splits into
        different words — which looks exactly like the lib failing to record.
        Invoking a script file is also the shipped shape (`post-file-edit.sh`
        schedules `.claude/scripts/kg-sync <path>`).
        """
        path = self.proj / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def _schedule(self, cmd: str, *, session: str = "sess-1") -> None:
        script = textwrap.dedent(
            f"""
            set -u
            . "{DEBOUNCE_LIB}"
            VCT_SESSION_ID="{session}" \\
            VCO_KG_SYNC_DEBOUNCE_SECONDS=0 _kg_debounce_schedule \\
                "{self.proj}" "{self.proj}/knowledge/x.md" "$(command -v python3)" \\
                "{self.proj}" "{cmd}" "kg"
            """
        )
        env = dict(os.environ)
        env["VCT_STATE_DIR"] = str(self.state)
        env["VCT_CLAUDE_DIR"] = str(self.claude_dir)
        env["VCT_SESSION_ID"] = session
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            cwd=str(self.proj), env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @property
    def _jsonl(self) -> Path:
        return self.state / "metrics" / "kg_sync_failures.jsonl"

    def _rows(self) -> list:
        if not self._jsonl.is_file():
            return []
        return [
            json.loads(ln)
            for ln in self._jsonl.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def _wait_for(self, path: Path, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.1)
        return path.exists()

    def _failing(self) -> str:
        """A refusal shaped like the real one: exit 3, diagnostic on stderr."""
        path = self._sync_script(
            "fail-sync.sh",
            "echo kg-sync: ERROR - no Python environment with VCO deps >&2\nexit 3",
        )
        return f"sh {path}"

    def test_failure_lands_in_the_log_and_one_row_in_the_stream(self):
        self._schedule(self._failing())
        self.assertTrue(self._wait_for(self._jsonl), "no failure row was recorded")
        log = self.proj / ".claude" / "logs" / "kg-sync-hook.log"
        self.assertTrue(log.is_file(), "the child's stderr was not captured")
        self.assertIn("no Python environment", log.read_text(encoding="utf-8"))

        rows = self._rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["kind"], "kg_sync_failed")
        self.assertEqual(row["exit"], 3)
        self.assertEqual(row["channel"], "kg")
        self.assertEqual(row["project_root"], str(self.proj))
        self.assertEqual(row["session"], "sess-1")
        self.assertIn("no Python environment", row["last_stderr"])

    def test_the_same_session_records_once_not_once_per_edit(self):
        failing = self._failing()
        for _ in range(3):
            self._schedule(failing)
            time.sleep(0.4)
        self.assertTrue(self._wait_for(self._jsonl))
        time.sleep(0.6)
        self.assertEqual(
            len(self._rows()), 1,
            "a failing edit path must report ONCE per session, not per edit",
        )

    def test_a_new_session_reports_again(self):
        failing = self._failing()
        self._schedule(failing)
        self.assertTrue(self._wait_for(self._jsonl))
        self._schedule(failing, session="sess-2")
        deadline = time.time() + 10
        while time.time() < deadline and len(self._rows()) < 2:
            time.sleep(0.1)
        self.assertEqual(len(self._rows()), 2, self._rows())

    def test_a_successful_sync_records_nothing(self):
        """The leave-alone test for the gate: success must not write a row,
        a log line, or a sentinel."""
        marker = self.proj / "ran.txt"
        ok = self._sync_script("ok-sync.sh", f"echo ok > {marker}")
        self._schedule(f"sh {ok}")
        self.assertTrue(self._wait_for(marker), "the sync did not run at all")
        time.sleep(0.6)
        self.assertEqual(self._rows(), [])
        sentinels = list((self.proj / ".claude" / "state").glob("kg_sync_failure_*"))
        self.assertEqual(sentinels, [], sentinels)


# ── 5: SessionStart surfaces both ──────────────────────────────────────────


class SessionStartWritePathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from tests.common.wrapper_staging import stage_scripts

        self.tmp = Path(tempfile.mkdtemp(prefix="vct-sshealth-"))
        self.proj = self.tmp / "proj"
        hooks = self.proj / ".claude" / "hooks"
        (hooks / "_lib").mkdir(parents=True)
        shutil.copy2(HOOKS / "session-start-retrieval-health.sh", hooks)
        for lib in ("find-python.sh", "metrics-dir.sh"):
            shutil.copy2(HOOKS / "_lib" / lib, hooks / "_lib" / lib)
        stage_scripts(self.proj / ".claude" / "scripts")
        self.clone = _fake_clone(self.tmp / "orch")
        self.state = self.tmp / "vct-state"
        self.state.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _run(self, **overrides: str) -> subprocess.CompletedProcess:
        env = _clean_env(
            VCT_STATE_DIR=str(self.state),
            CLAUDE_PROJECT_DIR=str(self.proj),
            # Unroutable on purpose: the retrieval half must not reach out.
            WEAVIATE_URL="http://127.0.0.1:9",
            KG_COLLECTION="",
            CODE_GRAPH_PROJECT="",
        )
        env.update(overrides)
        return subprocess.run(
            ["bash", str(self.proj / ".claude" / "hooks" / "session-start-retrieval-health.sh")],
            capture_output=True, text=True, cwd=str(self.proj), env=env, timeout=120,
        )

    def test_reports_ok_with_the_tier_when_the_write_path_resolves(self):
        proc = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("KG write path: OK (", proc.stdout)
        self.assertIn("tier: VCT_VENV", proc.stdout)

    def test_reports_the_refusal_on_stdout_where_sessionstart_can_see_it(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("KG write path: REFUSED", proc.stdout)
        # The ladder's own prose, re-emitted on stdout rather than re-worded.
        self.assertIn("no Python environment with VCO's KG dependencies", proc.stdout)
        self.assertIn("Fix by any ONE of:", proc.stdout)

    def test_the_retrieval_line_is_still_printed(self):
        proc = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertTrue(
            any(ln.startswith("Retrieval:") for ln in proc.stdout.splitlines()),
            proc.stdout,
        )

    def _seed_failure_row(self, **extra) -> None:
        metrics = self.state / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": "2026-09-16T10:00:00Z",
            "kind": "kg_sync_failed",
            "project_root": str(self.proj),
            "channel": "kg",
            "exit": 3,
            "session": "sess-1",
            "log": str(self.proj / ".claude" / "logs" / "kg-sync-hook.log"),
            "last_stderr": "kg-sync: ERROR - no Python environment",
        }
        row.update(extra)
        with open(metrics / "kg_sync_failures.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_a_recorded_failure_is_surfaced_once(self):
        self._seed_failure_row()
        first = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertIn("KG sync FAILED 1 time(s)", first.stdout)
        self.assertIn("no Python environment", first.stdout)
        self.assertIn("kg-sync --all", first.stdout)

        second = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertNotIn(
            "KG sync FAILED", second.stdout,
            "the notice must fire once per NEW row, not on every session",
        )

    def test_another_projects_rows_are_not_surfaced_here(self):
        self._seed_failure_row(project_root=str(self.tmp / "someone-else"))
        proc = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertNotIn("KG sync FAILED", proc.stdout)

    def test_nothing_recorded_prints_nothing(self):
        proc = self._run(VCT_VENV=str(self.clone / ".venv"))
        self.assertNotIn("KG sync FAILED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
