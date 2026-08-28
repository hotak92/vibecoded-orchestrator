# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 (#31): TERMINAL report for the detached codegraph resync walk.

THE SEAM: R-4 shipped only the registration half of the detached-walk build
contract — `_register_spawn_with_hub` wrote `status='running'` + pid, and
NOTHING ever posted a terminal status. Field failure 2026-08-28: a walk
SUCCEEDED (analyzer exit 0, converged), the driver exited, and ~24 min later
the launcher's pid-aliveness reconciler classified the dead pid as "the walk
died before completing" — a false failure on a healthy code graph
(files_analyzed=0, log_tail NULL as the fingerprint).

The rule (KG: pid-liveness-tracking-needs-a-terminal-report-2026-08-28):
registration and completion are ONE contract. The driver now posts
success/partial/failed + stats through the SAME wire it registered on
(`_hub_post_codegraph_build`, the one shared helper), with the same
soft-fail posture — observability, never a gate on the driver's exit.

These tests pin the Python half; the Rust half (handler finalize + binding
stamp + the reconciler's leave-alone for reported walks) is pinned in
`modules_api.rs` / `code_graph_builds.rs` unit tests.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_guards  # noqa: E402
from vco_lib import codegraph_resync as cr  # noqa: E402

_ANALYZER_SRC = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_GUARDS_SRC = REPO_ROOT / "vco_lib" / "codegraph_guards.py"


# ─────────────────────────── fixtures ───────────────────────────


class _RunRecord:
    """subprocess.run stand-in preserving the driver's historical seam."""

    def __init__(self, returncode=0):
        self.calls = []
        self._rc = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self._rc)


class _FakeProc:
    pid = 4321


def _seed_hub_env(monkeypatch, tmp_path: Path) -> None:
    """Point the wire helper at a stubbable hub (tmp state dir + token)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    (tmp_path / "hub.port").write_text("7700", encoding="utf-8")
    (tmp_path / "hub.token").write_text("tok-not-a-real-secret", encoding="utf-8")
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("VCT_HUB_PORT", raising=False)


def _capture_urlopen(captured: list):
    def _fake(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "body": json.loads(req.data.decode("utf-8")),
        })

        class _R:
            status = 200

        return _R()

    return _fake


def _quiet_driver_probes(monkeypatch, *, stale=None):
    """Silence the driver's non-terminal machinery (ledger probes, identity
    sweep) so the tests exercise ONLY the terminal-report seam."""
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: stale)


#: The sha the fixture's stubbed git probe reports — flows through the REAL
#: provenance producer into the log, and out through the parser.
_FIXTURE_SHA = "abc123def"


def _real_provenance_line(repo_path: Path) -> str:
    """Build the CODEGRAPH_PROVENANCE fixture line via the REAL producer
    (``codegraph_guards.provenance_line``) — never hand-written, so a format
    change in the producer breaks this fixture instead of the parser
    silently (M6). Only the producer's git probe is stubbed (deterministic
    sha without needing a real git repo); the patch is scoped to this call
    so it can never clobber a test's own ``subprocess.run`` stub."""
    with mock.patch.object(
        codegraph_guards.subprocess, "run",
        return_value=types.SimpleNamespace(returncode=0, stdout=f"{_FIXTURE_SHA}\n"),
    ):
        return codegraph_guards.provenance_line(
            "codesage-large-v2", 2048, 7, repo_path
        )


def _write_summary_log(tmp_path: Path, *, prune_failures: int = 0) -> Path:
    """A shared resync log carrying the analyzer's end-of-walk summary lines.

    The provenance line comes from the real producer (see
    ``_real_provenance_line``). The ``Files analyzed:`` / ``PRUNE_FAILURES=``
    lines have no importable producer (inline analyzer prints) — their
    emit-site shapes are pinned against the analyzer SOURCE by
    ``test_analyzer_emit_sites_match_parser_shapes`` below, so a reworded
    print breaks that test rather than silently zeroing every report."""
    log = tmp_path / "resync-test.log"
    log.write_text(
        "# codegraph resync for MyProj — spawned test\n"
        "[resync-driver] running: python analyze_code_graph.py\n"
        "   Files analyzed: 1784\n"
        f"PRUNE_FAILURES={prune_failures}\n"
        f"{_real_provenance_line(tmp_path)}\n",
        encoding="utf-8",
    )
    return log


# ─────────────── driver → terminal POST (act paths) ───────────────


def test_driver_success_posts_terminal_success(monkeypatch, tmp_path):
    """RED-PROOF: pre-#31 a successful walk posted NOTHING after the spawn's
    'running' registration — the reconciler then false-failed the row. The
    driver must now emit exactly one terminal POST with the walk's stats."""
    _seed_hub_env(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale={"P": 0})
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=0))
    captured: list = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", _capture_urlopen(captured))
    log = _write_summary_log(tmp_path)

    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log
    )

    assert rc == 0
    assert len(captured) == 1, "exactly one terminal POST (registration is the spawn's)"
    # M4: the terminal report rides the /terminal SUBPATH of the registration
    # route — an old hub 404s it (soft-skip = exact pre-fix degrade) instead
    # of executing it as a registration.
    assert captured[0]["url"].endswith(
        "/projects/MyProj/codegraph-builds/terminal"
    )
    payload = captured[0]["body"]
    assert payload["status"] == "success"
    assert payload["pid"] == os.getpid(), "driver pid == the registered pid"
    assert payload["source"] == "install_resync"
    assert payload["repo_root"] == str(tmp_path)
    assert payload["files_analyzed"] == 1784
    assert payload["analyzed_commit"] == _FIXTURE_SHA
    assert isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0
    assert "Files analyzed" in payload["log_tail"]
    assert "error_message" not in payload


def test_driver_partial_when_prune_failures(monkeypatch, tmp_path):
    """Exit 0 + PRUNE_FAILURES>0 → partial, count preserved — parity with
    the launcher-spawned reader (success_or_partial_status)."""
    _seed_hub_env(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale={"P": 0})
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=0))
    captured: list = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", _capture_urlopen(captured))
    log = _write_summary_log(tmp_path, prune_failures=3)

    cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log
    )

    payload = captured[0]["body"]
    assert payload["status"] == "partial"
    assert payload["files_analyzed"] == 1784, "partial keeps the insert count"


def test_driver_nonzero_exit_posts_failed(monkeypatch, tmp_path):
    """Analyzer exit != 0 → terminal failed with a named exit code and a
    zero count (mirror of the launcher path's failed arm)."""
    _seed_hub_env(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale=None)
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=4))
    captured: list = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", _capture_urlopen(captured))
    log = _write_summary_log(tmp_path)

    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log
    )

    assert rc == 0, "the driver's own exit contract is unchanged"
    payload = captured[0]["body"]
    assert payload["status"] == "failed"
    assert payload["files_analyzed"] == 0
    assert "exited 4" in payload["error_message"]


def test_driver_start_crash_posts_failed(monkeypatch, tmp_path):
    """Analyzer failed to even start → terminal failed naming the cause;
    the driver still exits 0 (nothing waits on it)."""
    _seed_hub_env(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale=None)

    def _boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(cr.subprocess, "run", _boom)
    captured: list = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", _capture_urlopen(captured))

    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )

    assert rc == 0
    payload = captured[0]["body"]
    assert payload["status"] == "failed"
    assert "could not start the analyzer" in payload["error_message"]


# ─────────────── soft-fail posture (leave-alone paths) ───────────────


def test_driver_terminal_hub_down_is_soft(monkeypatch, tmp_path):
    """Hub unreachable → the terminal report is a soft no-op; the driver's
    exit and its ledger work are untouched (same posture as registration)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    monkeypatch.setenv("VCT_HUB_PORT", "1")  # nothing listens on port 1
    monkeypatch.setenv("VCT_HUB_TOKEN", "t")
    _quiet_driver_probes(monkeypatch, stale={"P": 0})
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=0))

    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )  # no raise

    assert rc == 0


def test_collect_terminal_stats_soft_on_missing_or_absent_log(tmp_path):
    """No log path (DEVNULL degrade) or a vanished file → all-None stats and
    no tail; the terminal report still carries the status alone."""
    stats, tail = cr._collect_terminal_stats(None)
    assert tail is None
    assert stats == {
        "files_analyzed": None, "prune_failures": None, "analyzed_commit": None,
    }
    stats, tail = cr._collect_terminal_stats(tmp_path / "nope.log")
    assert tail is None
    assert stats["files_analyzed"] is None


# ─────────────── one shared wire helper (no drift) ───────────────


def test_both_halves_share_the_wire_helper(monkeypatch):
    """Registration and terminal report MUST route through the ONE wire
    helper (`_hub_post_codegraph_build`) — port/token/URL resolution can
    never drift between the two halves of the contract. M4: the halves
    differ ONLY in the path suffix — registration on the base route,
    terminal on the /terminal subpath."""
    calls: list = []

    def _fake_post(project_name, payload, path_suffix=""):
        calls.append((project_name, payload, path_suffix))
        return types.SimpleNamespace(status=200)

    monkeypatch.setattr(cr, "_hub_post_codegraph_build", _fake_post)

    cr._register_spawn_with_hub("P", 11, repo_root="/abs/r")
    cr._report_terminal_to_hub(
        "P", "success", repo_root="/abs/r", files_analyzed=5, duration_ms=9,
        log_tail="tail", analyzed_commit="sha",
    )

    assert [c[0] for c in calls] == ["P", "P"]
    assert [c[1]["status"] for c in calls] == ["running", "success"]
    assert all(c[1]["source"] == "install_resync" for c in calls)
    assert all(c[1]["repo_root"] == "/abs/r" for c in calls)
    assert calls[1][1]["files_analyzed"] == 5
    assert calls[1][1]["duration_ms"] == 9
    assert [c[2] for c in calls] == ["", "/terminal"], (
        "registration stays on the base route; terminal rides the subpath (M4)"
    )


def test_new_driver_old_hub_404_is_the_exact_pre_fix_degrade(monkeypatch, tmp_path):
    """M4 mixed-version pin: a NEW driver posting its terminal report to an
    OLD hub (which has no /terminal route) gets a 404 — which must land in
    the soft-skip, touching nothing and leaving the driver's exit
    untouched. Pre-M4, folding the terminal status into the base route made
    an old hub EXECUTE the report as a registration (serde ignores unknown
    fields), able to clobber a superseding walk's fresh running row."""
    import urllib.error

    _seed_hub_env(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale={"P": 0})
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=0))
    log = _write_summary_log(tmp_path)

    attempts: list = []

    from email.message import Message

    def _old_hub(req, timeout=None):
        attempts.append(req.full_url)
        # An old hub has no /terminal route → 404 on the subpath.
        assert req.full_url.endswith("/codegraph-builds/terminal")
        raise urllib.error.HTTPError(
            url=req.full_url, code=404, msg="Not Found", hdrs=Message(), fp=None
        )

    monkeypatch.setattr(cr.urllib.request, "urlopen", _old_hub)

    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py", log_path=log
    )  # no raise

    assert rc == 0, "old-hub 404 must not change the driver's exit"
    assert len(attempts) == 1, "404 is not a credential refusal — no retry"


def test_terminal_report_is_soft_when_wire_raises(monkeypatch):
    """The reporter never raises — hub errors degrade to a debug line."""

    def _boom(project_name, payload):
        raise OSError("hub gone")

    monkeypatch.setattr(cr, "_hub_post_codegraph_build", _boom)
    cr._report_terminal_to_hub("P", "failed", error_message="x")  # no raise


# ─────────────── pure helpers: status + stats parsing ───────────────


def test_terminal_status_parity_with_launcher_reader():
    """Must match commands/codegraph.rs: non-zero → failed; exit 0 with
    PRUNE_FAILURES>0 → partial; else success (None = unknown = not-partial)."""
    assert cr._terminal_status_for(0, None) == "success"
    assert cr._terminal_status_for(0, 0) == "success"
    assert cr._terminal_status_for(0, 3) == "partial"
    assert cr._terminal_status_for(4, 0) == "failed"
    assert cr._terminal_status_for(-1, None) == "failed"


def test_parse_analyzer_stats_reads_all_three_lines():
    text = (
        "noise\n"
        "   Files analyzed: 42\n"
        "PRUNE_FAILURES=2\n"
        "CODEGRAPH_PROVENANCE model=m dim=1024 embed_revision=7 "
        "analyzed_commit=deadbeef\n"
    )
    stats = cr._parse_analyzer_stats(text)
    assert stats == {
        "files_analyzed": 42, "prune_failures": 2, "analyzed_commit": "deadbeef",
    }


def test_parse_analyzer_stats_absent_lines_and_commit_none():
    """Absent lines stay None (never fabricated); analyzed_commit=none (a
    non-git tree) maps to None; malformed values are ignored."""
    assert cr._parse_analyzer_stats("just noise\n") == {
        "files_analyzed": None, "prune_failures": None, "analyzed_commit": None,
    }
    stats = cr._parse_analyzer_stats(
        "Files analyzed: many\n"
        "PRUNE_FAILURES=abc\n"
        "CODEGRAPH_PROVENANCE model=m dim=1024 embed_revision=7 "
        "analyzed_commit=none\n"
    )
    assert stats == {
        "files_analyzed": None, "prune_failures": None, "analyzed_commit": None,
    }


def test_parse_analyzer_stats_last_occurrence_wins():
    """Freshest line is authoritative — matches the Rust provenance parser's
    bottom-up scan."""
    stats = cr._parse_analyzer_stats(
        "CODEGRAPH_PROVENANCE model=m dim=1 embed_revision=1 analyzed_commit=old\n"
        "Files analyzed: 1\n"
        "Files analyzed: 2\n"
        "CODEGRAPH_PROVENANCE model=m dim=1 embed_revision=1 analyzed_commit=new\n"
    )
    assert stats["files_analyzed"] == 2
    assert stats["analyzed_commit"] == "new"


def test_registration_still_posts_to_the_base_route(monkeypatch, tmp_path):
    """M4 leave-alone: the registration half keeps its EXACT pre-#31 wire —
    base route, no suffix — so old hubs and old drivers are unaffected."""
    _seed_hub_env(monkeypatch, tmp_path)
    captured: list = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", _capture_urlopen(captured))

    cr._register_spawn_with_hub("MyProj", 4321, repo_root=str(tmp_path))

    assert len(captured) == 1
    assert captured[0]["url"].endswith("/projects/MyProj/codegraph-builds")
    assert not captured[0]["url"].endswith("/terminal")
    assert captured[0]["body"]["status"] == "running"


# ─────────────── M6: emit-site liveness (producer ↔ parser) ───────────────


def test_analyzer_emit_sites_match_parser_shapes():
    """M6: pin the analyzer's ACTUAL print sites to the exact shapes
    `_parse_analyzer_stats` keys on — a reworded analyzer print breaks THIS
    test instead of silently zeroing every success report's stats.
    Convention twin of test_codegraph_prune_failure_status.py::
    test_source_emits_machine_readable_prune_failures_line."""
    src = _ANALYZER_SRC.read_text(encoding="utf-8")
    # Summary count line — parser keys on find("Files analyzed:").
    assert "print(f\"   Files analyzed: {stats['files_analyzed']}\")" in src, (
        "analyze_code_graph.py no longer prints the 'Files analyzed: N' "
        "summary line the terminal report's parser keys on"
    )
    # Strict machine line — parser keys on startswith("PRUNE_FAILURES=").
    assert 'print(f"PRUNE_FAILURES={prune_failures}"' in src, (
        "analyze_code_graph.py no longer emits the strict PRUNE_FAILURES=N "
        "line (partial-status signal for both the launcher reader and the "
        "terminal report)"
    )
    # Provenance — the analyzer must still route through the SHARED producer
    # (whose line shape the fixture + parser tests exercise for real).
    assert "provenance_line(" in src, (
        "analyze_code_graph.py no longer routes provenance through "
        "codegraph_guards.provenance_line"
    )


def test_provenance_producer_shape_matches_parser():
    """M6: the parser keys on the producer's leading token and its
    analyzed_commit=<sha|none> key — pin the producer SOURCE shape, and
    round-trip the REAL producer output through the parser both for a
    known sha and for the non-git 'none' soft-fail."""
    guards_src = _GUARDS_SRC.read_text(encoding="utf-8")
    assert 'f"CODEGRAPH_PROVENANCE model={model_s} dim={dim_i} "' in guards_src
    assert 'f"embed_revision={embed_revision} analyzed_commit={commit}"' in guards_src

    # Round-trip: real producer → parser (sha known via the scoped stub).
    line = _real_provenance_line(Path("."))
    stats = cr._parse_analyzer_stats(line + "\n")
    assert stats["analyzed_commit"] == _FIXTURE_SHA

    # Round-trip: git probe fails → producer emits analyzed_commit=none →
    # parser maps it to None (commit honestly unknown).
    with mock.patch.object(
        codegraph_guards.subprocess, "run",
        side_effect=OSError("git absent"),
    ):
        none_line = codegraph_guards.provenance_line("m", 1024, 7, Path("."))
    assert "analyzed_commit=none" in none_line
    assert cr._parse_analyzer_stats(none_line + "\n")["analyzed_commit"] is None


def test_collect_terminal_stats_caps_tail_at_4kib(tmp_path):
    log = tmp_path / "big.log"
    log.write_text(("x" * 10_000) + "\nFiles analyzed: 3\n", encoding="utf-8")
    stats, tail = cr._collect_terminal_stats(log)
    assert stats["files_analyzed"] == 3
    assert tail is not None
    assert len(tail.encode("utf-8")) <= cr._TERMINAL_LOG_TAIL_MAX_BYTES
    assert tail.endswith("Files analyzed: 3\n")


# ─────────────── spawn/driver plumbing for the stats source ───────────────


def test_spawn_forwards_log_path_to_driver(monkeypatch, tmp_path):
    """The spawn tells the driver WHERE the shared log lives (--log-path) so
    the terminal report can read the analyzer's summary lines back out."""
    # P5 spawn gate: the suite-wide autouse fixture disables the launch path;
    # this test asserts the launch path itself, so clear the gate locally —
    # with VCT_STATE_DIR pinned to tmp the per-spawn log header lands in tmp,
    # never in the user's real ~/.vct/logs/ (the condition P5 exists for).
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    repo = tmp_path / "repo"
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")

    spawned: list = []
    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (spawned.append(argv), _FakeProc())[1],
    )
    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    driver = [a for a in spawned if "--run-resync" in a][0]
    assert "--log-path" in driver
    log_arg = driver[driver.index("--log-path") + 1]
    assert "resync-MyProj" in log_arg and log_arg.endswith(".log")


def test_main_forwards_log_path_to_the_driver(monkeypatch, tmp_path):
    seen: dict = {}

    def _fake_run(project, repo_root, analyzer, *, prune_stale=False,
                  index_dot_claude=True, log_path=None):
        seen.update(project=project, log_path=log_path)
        return 0

    monkeypatch.setattr(cr, "run_resync_and_verify", _fake_run)
    rc = cr._main([
        "--run-resync", "--project", "P",
        "--repo-root", str(tmp_path),
        "--analyzer", str(tmp_path / "a.py"),
        "--log-path", str(tmp_path / "l.log"),
    ])
    assert rc == 0
    assert seen["project"] == "P"
    assert seen["log_path"] == tmp_path / "l.log"


def test_main_log_path_is_optional(monkeypatch, tmp_path):
    """Back-compat: a driver launched by an older spawn (no --log-path)
    still runs — the terminal report is then status-only."""
    seen: dict = {}

    def _fake_run(project, repo_root, analyzer, *, prune_stale=False,
                  index_dot_claude=True, log_path=None):
        seen["log_path"] = log_path
        return 0

    monkeypatch.setattr(cr, "run_resync_and_verify", _fake_run)
    rc = cr._main([
        "--run-resync", "--project", "P",
        "--repo-root", str(tmp_path),
        "--analyzer", str(tmp_path / "a.py"),
    ])
    assert rc == 0
    assert seen["log_path"] is None
