# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 W5R-04 — extra code-graph paths are re-indexed automatically.

Before: only the panel's manual Sync button re-indexed an extra path, so a
path whose repo moved on kept serving stale entities forever. Now the Stop
hook (``stop-codegraph-drain.{sh,ps1}``) spawns
``vco_lib.codegraph_extras_refresh`` detached; it re-indexes each ENABLED,
stale path with the Sync button's argv and records the new commit through the
hub route (never launcher.db directly).

Covered here:
  * the shared argv + staleness rules match the committed table the Rust side
    also asserts (``tests/fixtures/codegraph_extra_sync_argv.json``);
  * behind → analyzer invoked with the Sync argv + commit recorded;
    up to date → nothing; disabled → nothing; analyzer failure → logged,
    nothing recorded; throttle and lock honoured;
  * the real analyzer runner + a real git repo end-to-end (stub analyzer);
  * the hub POST helper against a loopback fake hub (no network);
  * the hook spawns the refresh even when the turn queued nothing, once per
    check interval (bash, and pwsh when available).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import codegraph_extras_refresh as cer

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "codegraph_extra_sync_argv.json"
DRAIN_SH = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.sh"
DRAIN_PS1 = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.ps1"


# ── shared rules (parity with Rust) ─────────────────────────────────────────


def test_argv_matches_shared_fixture() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 4
    for c in cases:
        got = cer.build_extra_sync_argv(c["path"], c["prefix"], c["incremental"], c["since_commit"])
        assert got == c["argv"], c["name"]


def test_staleness_rule_matches_shared_fixture() -> None:
    for c in json.loads(FIXTURE.read_text(encoding="utf-8"))["stale_cases"]:
        assert cer.extra_path_is_stale(c["head"], c["last"]) is c["stale"], c


# ── driver with injected deps ───────────────────────────────────────────────


@dataclass(frozen=True)
class _Extra:
    path: str
    enabled: bool
    last_indexed_commit: Optional[str]


@dataclass(frozen=True)
class _Cfg:
    project_id: str
    code_graph_collection_prefix: str
    code_graph_extra_paths: tuple


class _Recorder:
    def __init__(self, heads: dict, ok: bool = True, record_ok: bool = True):
        self.heads = heads
        self.ok = ok
        self.record_ok = record_ok
        self.runs: list = []
        self.records: list = []

    def git_head(self, path: str) -> Optional[str]:
        return self.heads.get(path)

    def run_analyzer(self, python, analyzer, argv, timeout):
        self.runs.append(list(argv))
        if self.ok:
            return cer.AnalyzerResult(True, "ok", 3, 7, 11)
        return cer.AnalyzerResult(False, "exit 1: boom")

    def record(self, project_id, path, commit, res):
        self.records.append((project_id, path, commit, res.files_analyzed))
        return (self.record_ok, "recorded" if self.record_ok else "hub answered 500")


def _drive(tmp_path: Path, extras, rec: _Recorder, **kw):
    cfg = _Cfg("pid-1", "Acme", tuple(extras))
    lines: list = []
    deps = cer.Deps(
        resolve=lambda _root: cfg,
        git_head=rec.git_head,
        run_analyzer=rec.run_analyzer,
        record=rec.record,
        now=kw.pop("now", time.time),
    )
    out = cer.refresh_extras(
        str(tmp_path), "/x/analyze.py", "/x/python", tmp_path / "state", lines.append,
        deps=deps, **kw,
    )
    return out, lines


def test_behind_reindexes_with_sync_argv_and_records_commit(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/clone": "b" * 40})
    out, lines = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec)
    assert out == {"/srv/clone": "recorded"}
    assert rec.runs == [cer.build_extra_sync_argv("/srv/clone", "Acme", True, "a" * 40)]
    assert rec.records == [("pid-1", "/srv/clone", "b" * 40, 3)]
    assert any(line.startswith("done path=/srv/clone") for line in lines), lines
    # The per-path lock is released.
    assert not list((tmp_path / "state").glob("codegraph_drain_root_*.lock"))


def test_never_indexed_path_runs_a_full_pass(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/new": "c" * 40})
    _drive(tmp_path, [_Extra("/srv/new", True, None)], rec)
    assert rec.runs == [["/srv/new", "--project", "Acme", "--json-progress"]]


def test_up_to_date_does_nothing(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/clone": "a" * 40})
    out, _ = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec)
    assert out == {"/srv/clone": "up_to_date"}
    assert rec.runs == [] and rec.records == []


def test_disabled_path_does_nothing(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/off": "b" * 40})
    out, _ = _drive(tmp_path, [_Extra("/srv/off", False, "a" * 40)], rec)
    assert out == {}
    assert rec.runs == [] and rec.records == []


def test_non_git_path_does_nothing(tmp_path: Path) -> None:
    rec = _Recorder({})
    out, _ = _drive(tmp_path, [_Extra("/srv/plain", True, None)], rec)
    assert out == {"/srv/plain": "no_git_head"}
    assert rec.runs == []


def test_analyzer_failure_is_logged_and_records_nothing(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/clone": "b" * 40}, ok=False)
    out, lines = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec)
    assert out == {"/srv/clone": "analyzer_failed"}
    assert rec.records == []
    assert any("FAILED path=/srv/clone" in line and "boom" in line for line in lines), lines
    # The attempt is stamped, so the failing path is throttled, not retried
    # on every Stop.
    rec2 = _Recorder({"/srv/clone": "b" * 40})
    out2, _ = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec2)
    assert out2 == {"/srv/clone": "throttled"}
    assert rec2.runs == []


def test_throttle_window_elapsed_retries(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/clone": "b" * 40}, ok=False)
    _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec, now=lambda: 1_000_000.0)
    rec2 = _Recorder({"/srv/clone": "b" * 40})
    out, _ = _drive(
        tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec2,
        now=lambda: 1_000_000.0 + cer.DEFAULT_MIN_INTERVAL_SECONDS + 1,
    )
    assert out == {"/srv/clone": "recorded"}


def test_record_failure_is_logged(tmp_path: Path) -> None:
    rec = _Recorder({"/srv/clone": "b" * 40}, record_ok=False)
    out, lines = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec)
    assert out == {"/srv/clone": "record_failed"}
    assert any("NOT RECORDED" in line for line in lines), lines


def test_held_lock_skips_the_path(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / f"codegraph_drain_root_{cer.path_key('/srv/clone')}.lock").mkdir()
    rec = _Recorder({"/srv/clone": "b" * 40})
    out, _ = _drive(tmp_path, [_Extra("/srv/clone", True, "a" * 40)], rec)
    assert out == {"/srv/clone": "busy"}
    assert rec.runs == []


def test_hub_unreachable_is_a_logged_noop(tmp_path: Path) -> None:
    lines: list = []

    def _boom(_root):
        raise RuntimeError("hub down")

    out = cer.refresh_extras(
        str(tmp_path), "/x/a.py", "/x/py", tmp_path / "state", lines.append,
        deps=cer.Deps(resolve=_boom),
    )
    assert out == {}
    assert any("config unavailable" in line for line in lines)


# ── real runner + real git, stub analyzer ───────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


def _stub_analyzer(path: Path, log: Path, *, exit_code: int = 0, insert_errors: int = 0) -> None:
    path.write_text(textwrap.dedent(f"""\
        import json, sys
        with open({json.dumps(str(log))}, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")
        print(json.dumps({{"progress": 0.5, "message": "x"}}))
        print(json.dumps({{"final": True, "files_analyzed": 2, "modules": 1,
                          "classes": 1, "functions": 3, "apis": 0,
                          "insert_errors": {insert_errors}}}))
        sys.exit({exit_code})
    """))


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_end_to_end_real_git_and_runner(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    (clone / "a.py").write_text("x = 1\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "one")
    first = _git(clone, "rev-parse", "HEAD")
    (clone / "a.py").write_text("x = 2\n")
    _git(clone, "commit", "-qam", "two")
    head = _git(clone, "rev-parse", "HEAD")

    log = tmp_path / "argv.jsonl"
    stub = tmp_path / "analyze.py"
    _stub_analyzer(stub, log)
    recorded: list = []
    cfg = _Cfg("pid-1", "Acme", (_Extra(str(clone), True, first),))
    lines: list = []
    out = cer.refresh_extras(
        str(tmp_path), str(stub), sys.executable, tmp_path / "state", lines.append,
        deps=cer.Deps(
            resolve=lambda _r: cfg,
            record=lambda pid, p, c, res: (recorded.append((pid, p, c, res)) or (True, "recorded")),
        ),
    )
    assert out == {str(clone): "recorded"}, lines
    assert json.loads(log.read_text().splitlines()[0]) == [
        str(clone), "--project", "Acme", "--json-progress", "--incremental", "--since-commit", first,
    ]
    (pid, p, c, res) = recorded[0]
    assert (pid, p, c) == ("pid-1", str(clone), head)
    assert (res.files_analyzed, res.entities_indexed) == (2, 5)


@pytest.mark.parametrize("exit_code,insert_errors", [(1, 0), (0, 4)])
def test_runner_failure_shapes(tmp_path: Path, exit_code: int, insert_errors: int) -> None:
    stub = tmp_path / "analyze.py"
    _stub_analyzer(stub, tmp_path / "log", exit_code=exit_code, insert_errors=insert_errors)
    res = cer.run_analyzer(sys.executable, str(stub), ["/p"], 60)
    assert not res.ok, res


def test_runner_timeout_is_a_failure(tmp_path: Path) -> None:
    stub = tmp_path / "slow.py"
    stub.write_text("import time; time.sleep(30)\n")
    res = cer.run_analyzer(sys.executable, str(stub), [], 1)
    assert not res.ok and "timed out" in res.reason


# ── hub POST helper against a loopback fake hub ─────────────────────────────


class _FakeHub(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        _FakeHub.seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
        self.send_response(_FakeHub.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"recorded": true}')

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def fake_hub(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    monkeypatch.setenv("VCT_HUB_PORT", str(srv.server_address[1]))
    monkeypatch.setenv("VCT_HUB_TOKEN", "tok-test")
    from vco_lib import project_config
    project_config._test_clear_cache()
    _FakeHub.seen = []
    _FakeHub.status = 200
    yield _FakeHub
    srv.shutdown()
    project_config._test_clear_cache()


def test_record_indexed_posts_to_the_hub_route(fake_hub) -> None:
    ok, why = cer.record_indexed("pid-1", "/srv/clone", "abc1234",
                                 cer.AnalyzerResult(True, "ok", 2, 5, 9))
    assert ok, why
    path, auth, body = fake_hub.seen[0]
    assert path == "/api/v1/projects/pid-1/codegraph/extras/indexed"
    assert auth == "Bearer tok-test"
    assert body == {"path": "/srv/clone", "commit": "abc1234", "files_analyzed": 2,
                    "entities_indexed": 5, "duration_ms": 9}


def test_record_indexed_reports_a_refusal(fake_hub) -> None:
    fake_hub.status = 409
    ok, why = cer.record_indexed("pid-1", "/srv/clone", "abc1234",
                                 cer.AnalyzerResult(True, "ok"))
    assert not ok and "409" in why


def test_record_indexed_under_the_test_gate_is_unreachable() -> None:
    # conftest sets VCT_DISABLE_HUB_RESOLVER=1 — the POST must not leave.
    ok, why = cer.record_indexed("pid", "/p", "abc1234", cer.AnalyzerResult(True, "ok"))
    assert not ok and "unreachable" in why


# ── the hook spawns the refresh (detached), once per check interval ────────


def _shim_python(path: Path, log: Path) -> None:
    """A stand-in interpreter: records argv, does nothing else."""
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
    )
    path.chmod(0o755)


def _wait_lines(p: Path, want: int, tries: int = 100) -> list:
    for _ in range(tries):
        if p.exists():
            lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
            if len(lines) >= want:
                return lines
        time.sleep(0.05)
    return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _hook_env(root: Path, shim: Path, stub: Path, interval: str) -> dict:
    return {**os.environ, "CLAUDE_PROJECT_DIR": str(root), "VCT_PYTHON": str(shim),
            "VCT_ANALYZER_SCRIPT": str(stub),
            "VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS": interval}


def _hook_case(tmp_path: Path, runner: list) -> None:
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    spawn_log = tmp_path / "spawned.log"
    shim = tmp_path / "py.sh"
    _shim_python(shim, spawn_log)
    stub = tmp_path / "analyze.py"
    stub.write_text("")
    stdin = json.dumps({"session_id": "s1"})

    r = subprocess.run(runner, input=stdin, capture_output=True, text=True, timeout=60,
                       env=_hook_env(root, shim, stub, "600"))
    assert r.returncode == 0, r.stderr
    lines = _wait_lines(spawn_log, 1)
    assert len(lines) == 1, lines
    assert lines[0].startswith("-m vco_lib.codegraph_extras_refresh --project-root "), lines
    assert f"--analyzer {stub}" in lines[0] and "--state-dir " in lines[0], lines
    assert (root / ".claude" / "state" / "codegraph_extras_check.ts").is_file()

    # Inside the check interval: no second spawn.
    subprocess.run(runner, input=stdin, capture_output=True, text=True, timeout=60,
                   env=_hook_env(root, shim, stub, "600"))
    time.sleep(1.0)
    assert len(_wait_lines(spawn_log, 1)) == 1

    # Interval 0 → the next Stop checks again.
    subprocess.run(runner, input=stdin, capture_output=True, text=True, timeout=60,
                   env=_hook_env(root, shim, stub, "0"))
    assert len(_wait_lines(spawn_log, 2)) == 2


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("python3")), reason="bash required")
def test_sh_hook_spawns_refresh_without_a_queue(tmp_path: Path) -> None:
    _hook_case(tmp_path, ["bash", str(DRAIN_SH)])


@pytest.mark.skipif(not shutil.which("pwsh") or os.name == "nt", reason="pwsh (POSIX) required")
def test_ps1_hook_spawns_refresh_without_a_queue(tmp_path: Path) -> None:
    _hook_case(tmp_path, ["pwsh", "-NoProfile", "-File", str(DRAIN_PS1)])
