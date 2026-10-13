# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-AA — lane reservations: isolated subagent worktrees in ANOTHER repo.

A coordinator in project P reserves the next isolated agent's worktree in repo R
(P itself or one of P's ENABLED extra code-graph paths, read from the hub) at a
base commit on a new branch; worktree-guard consumes the oldest pending,
unexpired reservation on the next ``agent-*`` WorktreeCreate and creates the
worktree with ``git -C R worktree add -b``; SubagentStart tells the agent its
reservation; SubagentStop keeps a worktree with work and reaps an unchanged one
in R. Pinned here with real temp git repos, a fake launcher hub, and the real
.sh AND .ps1 hooks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import lane_reservation as lr
from vco_lib import project_config

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"
_PS = shutil.which("pwsh") or shutil.which("powershell")
_LEAK_ENV_KEYS = ("CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                  "CLAUDE_CONFIG_DIR", "VCT_DISABLE_HUB_RESOLVER", "VCT_DISABLE_HOOKS")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash flavour is POSIX-only; the pwsh arm runs where pwsh exists")

FLAVOURS = [
    pytest.param("sh", id="sh"),
    pytest.param("ps1", id="ps1", marks=pytest.mark.skipif(_PS is None, reason="no pwsh on PATH")),
]


# ── fixtures ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True, env=env)


def _repo(root: Path, commits: int = 1) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "--initial-branch=main")
    for i in range(commits):
        (root / "f.txt").write_text(f"v{i}\n", encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-q", "-m", f"c{i}")
    return root


def _sha(repo: Path, rev: str = "HEAD") -> str:
    return _git(repo, "rev-parse", rev).stdout.strip()


class _Hub:
    """A fake launcher hub: by-path + /config with configurable extra paths."""

    def __init__(self) -> None:
        self.extras: list[dict] = []
        self.project_path = ""
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — base signature
                return

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/api/v1/projects/by-path":
                    path = parse_qs(u.query).get("path", [""])[0]
                    if os.path.realpath(path) == os.path.realpath(hub.project_path):
                        return self._send(200, {"id": "pid-w6aa"})
                    return self._send(404, {"error": {"code": "project_not_found", "message": path}})
                if u.path == "/api/v1/projects/pid-w6aa/config":
                    return self._send(200, {
                        "project_id": "pid-w6aa", "project_path": hub.project_path,
                        "project_slug": "p", "project_display_name": "P",
                        "code_graph_project": "P", "code_graph_collection_prefix": "P",
                        "kg_collection": "P_KG", "active_embedding": "qwen3",
                        "embedding_models": {"text": "t", "code": "c"},
                        "kg_access_list": [], "codegraph_access_list": [],
                        "weaviate_url": "http://127.0.0.1:9", "ollama_url": "http://127.0.0.1:9",
                        "grpc_port": 9, "shared_kg_write_disabled": False,
                        "code_graph_extra_paths": hub.extras,
                    })
                return self._send(404, {"error": {"code": "not_found", "message": u.path}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def env(self) -> dict:
        return {"VCT_HUB_PORT": str(self.port), "VCT_HUB_TOKEN": "tok", "VCT_HUB_TOKEN_STRICT": "1"}


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch):
    h = _Hub()
    for k, v in h.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    project_config._test_clear_cache()
    try:
        yield h
    finally:
        h.server.shutdown()
        project_config._test_clear_cache()


@pytest.fixture
def pr(tmp_path: Path, hub: _Hub):
    """P (the coordinator project) and R (an enabled code folder of P)."""
    p = _repo(tmp_path / "P")
    r = _repo(tmp_path / "R", commits=2)
    hub.project_path = str(p)
    hub.extras = [{"path": str(r), "enabled": True, "last_indexed_commit": None}]
    return p, r


def _env(hub: _Hub | None, project: Path) -> dict:
    # child_env pins this checkout's vco_lib first on the child's path.
    env = child_env({k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS})
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["HOME"] = str(project.parent / "fakehome")
    env["VCT_VENV"] = str(Path(sys.executable).parent.parent)
    if hub is not None:
        env.update(hub.env())
    return env


def _hook(script: str, flavour: str, payload: dict, project: Path, env: dict) -> subprocess.CompletedProcess:
    path = HOOKS / f"{script}.{flavour}"
    argv = ["bash", str(path)] if flavour == "sh" else [_PS, *PWSH_BASE_FLAGS, "-File", str(path)]
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          env=env, cwd=str(project), timeout=120)


def _guard(flavour: str, name: str, project: Path, env: dict) -> subprocess.CompletedProcess:
    return _hook("worktree-guard", flavour,
                 {"hook_event_name": "WorktreeCreate", "name": name, "cwd": str(project)}, project, env)


def _cli(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "vco_lib.lane_reservation", *args],
                          capture_output=True, text=True, env=child_env(env), timeout=60)


def _reserve(p: Path, r: Path, base: str, branch: str, env: dict, *extra: str) -> str:
    proc = _cli("reserve", "--project", str(p), "--repo", str(r), "--base", base,
                "--branch", branch, *extra, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _record(project: Path, name: str) -> dict:
    f = project / ".claude" / "state" / "worktree-guard" / f"wt-{name}.json"
    return json.loads(f.read_text(encoding="utf-8-sig"))


# ── reserve → consume → worktree in R ───────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_reservation_creates_worktree_in_target_repo(flavour: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    base = _sha(r, "HEAD~1")
    rid = _reserve(p, r, base, "lane/feature-x", env, "--id", "lr-one")
    assert rid == "lr-one"

    proc = _guard(flavour, "agent-aaa111", p, env)

    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    assert wt == (r.parent / "R.vco-lanes" / "lane-feature-x-lr-one").resolve()
    assert _sha(wt) == base
    assert _git(wt, "branch", "--show-current").stdout.strip() == "lane/feature-x"
    assert f"worktree {wt}" in _git(r, "worktree", "list", "--porcelain").stdout
    # Nothing was created in P.
    assert not (p / ".claude" / "worktrees").exists()
    rec = _record(p, "agent-aaa111")
    assert rec["mode"] == "reservation" and rec["reservation_id"] == "lr-one"
    assert rec["toplevel"] == str(r.resolve()) and rec["base"] == base
    assert rec["branch"] == "lane/feature-x" and rec["branch_created"] == "yes"
    # The reservation is consumed: the next launch gets ordinary isolation in P.
    proc2 = _guard(flavour, "agent-bbb222", p, env)
    assert proc2.returncode == 0, proc2.stderr
    assert Path(proc2.stdout.strip().splitlines()[-1]).parent == (p / ".claude" / "worktrees").resolve()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_no_reservation_is_unchanged(flavour: str, pr) -> None:
    """No pending file: the guard never calls Python or the hub (hub absent here)."""
    p, _r = pr
    env = _env(None, p)
    env["VCT_HUB_PORT"] = "9"
    proc = _guard(flavour, "agent-plain1", p, env)
    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    assert wt.parent == (p / ".claude" / "worktrees").resolve()
    assert _record(p, "agent-plain1")["mode"] == "head"
    assert not (p / ".claude" / "state" / "lane-reservations").exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_non_agent_name_does_not_consume(flavour: str, pr, hub: _Hub) -> None:
    """`claude --worktree mine` must never take a lane's reservation."""
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", "lane-keep", env, "--id", "lr-keep")
    proc = _guard(flavour, "mine", p, env)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip().splitlines()[-1]).parent == (p / ".claude" / "worktrees").resolve()
    assert [x["id"] for x in lr.list_pending(p)] == ["lr-keep"]


# ── FIFO + atomic consumption ───────────────────────────────────────────────


def test_fifo_oldest_first(pr) -> None:
    p, r = pr
    lr.reserve(p, r, "HEAD", "b-first", res_id="first")
    time.sleep(0.01)
    lr.reserve(p, r, "HEAD", "b-second", res_id="second")
    a = lr.consume(p, "agent-1")
    b = lr.consume(p, "agent-2")
    assert a is not None and b is not None
    assert a.extra["reservation_id"] == "first" and b.extra["reservation_id"] == "second"
    assert lr.consume(p, "agent-3") is None


def test_concurrent_consumers_one_wins(pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    lr.reserve(p, r, "HEAD", "b-race", res_id="race")
    procs = [subprocess.Popen([sys.executable, "-m", "vco_lib.lane_reservation", "consume",
                               "--project", str(p), "--name", f"agent-r{i}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env(env))
             for i in range(4)]
    outs = [pp.communicate(timeout=60) for pp in procs]
    rcs = [pp.returncode for pp in procs]
    assert sorted(rcs) == [0, 3, 3, 3], [err for _out, err in outs]
    assert len(list((p / lr.STATE_SUBDIR / "claimed").glob("res-*.json"))) == 1


def test_refire_same_agent_is_idempotent(pr) -> None:
    p, r = pr
    lr.reserve(p, r, "HEAD", "b-re", res_id="re")
    a = lr.consume(p, "agent-x")
    b = lr.consume(p, "agent-x")
    assert a is not None and b is not None and a.path == b.path


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_expired_reservation_is_never_consumed(pr) -> None:
    p, r = pr
    t0 = time.time()
    lr.reserve(p, r, "HEAD", "b-old", res_id="old", ttl_s=60, now=t0)
    assert lr.consume(p, "agent-late", now=t0 + 61) is None
    assert list((p / lr.STATE_SUBDIR / "expired").glob("res-*old.json"))
    assert not (r.parent / "R.vco-lanes").exists()


# ── allow-list + hub ────────────────────────────────────────────────────────


def test_not_allow_listed_repo_refused(pr, tmp_path: Path) -> None:
    p, _r = pr
    other = _repo(tmp_path / "Other")
    with pytest.raises(lr.ReservationError, match="not allowed"):
        lr.reserve(p, other, "HEAD", "b-x")


def test_disabled_code_folder_refused(pr, hub: _Hub) -> None:
    p, r = pr
    hub.extras = [{"path": str(r), "enabled": False}]
    with pytest.raises(lr.ReservationError, match="not allowed"):
        lr.reserve(p, r, "HEAD", "b-x")


def test_project_itself_allowed(pr) -> None:
    p, _r = pr
    rec = lr.reserve(p, p, "HEAD", "b-self", res_id="self")
    assert rec["repo_toplevel"] == str(p.resolve())


def test_hub_unreachable_refuses(pr, monkeypatch: pytest.MonkeyPatch) -> None:
    p, r = pr
    monkeypatch.setenv("VCT_HUB_PORT", "9")
    project_config._test_clear_cache()
    proc = _cli("reserve", "--project", str(p), "--repo", str(r), "--base", "HEAD",
                "--branch", "b-down", env={**_env(None, p), "VCT_HUB_PORT": "9", "VCT_HUB_TOKEN": "tok"})
    assert proc.returncode == lr.EXIT_FAILED
    assert "REFUSED" in proc.stderr and "hub" in proc.stderr
    assert not (p / lr.STATE_SUBDIR / "pending").exists() or not lr.list_pending(p)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hub_down_at_consume_aborts_loudly(flavour: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", "b-later", env, "--id", "later")
    env["VCT_HUB_PORT"] = "9"
    proc = _guard(flavour, "agent-down1", p, env)
    assert proc.returncode != 0
    assert "REFUSED" in proc.stderr and "ABORT" in proc.stderr
    assert proc.stdout.strip() == "", "never a silent fallback to a worktree of P"
    assert not (p / ".claude" / "worktrees").exists()
    claimed = json.loads(next((p / lr.STATE_SUBDIR / "claimed").glob("res-*.json")).read_text())
    assert claimed["status"] == "failed"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_folder_disabled_after_reserve_aborts(flavour: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", "b-gone", env, "--id", "gone")
    hub.extras = []
    proc = _guard(flavour, "agent-gone1", p, env)
    assert proc.returncode != 0 and "not allowed" in proc.stderr
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_no_vco_python_aborts_loudly(flavour: str, pr, tmp_path: Path, hub: _Hub) -> None:
    p, r = pr
    lr.reserve(p, r, "HEAD", "b-nopy", res_id="nopy")
    env = _env(hub, p)
    empty = tmp_path / "empty"
    empty.mkdir()
    env["VCT_VENV"] = str(empty)
    env["VCT_INSTALL_ROOT"] = str(empty)
    env.pop("VCT_ORCHESTRATOR_ROOT", None)
    # A copy of the hooks outside any VCO clone, so no clone-relative venv is found.
    hooks = tmp_path / "proj-hooks" / ".claude" / "hooks"
    shutil.copytree(HOOKS, hooks)
    path = hooks / f"worktree-guard.{flavour}"
    argv = ["bash", str(path)] if flavour == "sh" else [_PS, *PWSH_BASE_FLAGS, "-File", str(path)]
    proc = subprocess.run(argv, input=json.dumps({"name": "agent-nopy", "cwd": str(p)}),
                          capture_output=True, text=True, env=env, cwd=str(p), timeout=120)
    assert proc.returncode != 0 and "broken install" in proc.stderr
    assert proc.stdout.strip() == ""


# ── validation ──────────────────────────────────────────────────────────────


def test_bad_inputs_refused(pr, tmp_path: Path) -> None:
    p, r = pr
    with pytest.raises(lr.ReservationError, match="does not resolve"):
        lr.reserve(p, r, "no-such-rev", "b-1")
    with pytest.raises(lr.ReservationError, match="not a valid branch"):
        lr.reserve(p, r, "HEAD", "bad..name")
    with pytest.raises(lr.ReservationError, match="already checked out"):
        lr.reserve(p, r, "HEAD", "main")
    _git(r, "branch", "exists-already")
    with pytest.raises(lr.ReservationError, match="already exists"):
        lr.reserve(p, r, "HEAD", "exists-already")
    with pytest.raises(lr.ReservationError, match="reservation id"):
        lr.reserve(p, r, "HEAD", "b-2", res_id="../x")
    lr.reserve(p, r, "HEAD", "b-dup", res_id="dup")
    with pytest.raises(lr.ReservationError, match="already exists"):
        lr.reserve(p, r, "HEAD", "b-dup2", res_id="dup")
    with pytest.raises(lr.ReservationError, match="already targets branch"):
        lr.reserve(p, r, "HEAD", "b-dup", res_id="dup3")


def test_cancel_and_list(pr) -> None:
    p, r = pr
    lr.reserve(p, r, "HEAD", "b-c", res_id="c1")
    assert [x["id"] for x in lr.list_pending(p)] == ["c1"]
    assert lr.cancel(p, "c1") is True
    assert lr.list_pending(p) == [] and lr.cancel(p, "c1") is False


# ── existing-path adoption ──────────────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_existing_path_adopted(flavour: str, pr, tmp_path: Path, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    pre = tmp_path / "pre-wt"
    _git(r, "worktree", "add", "-q", "-b", "lane-pre", str(pre), "HEAD~1")
    _reserve(p, r, "HEAD~1", "lane-pre", env, "--id", "pre", "--existing-path", str(pre))
    proc = _guard(flavour, "agent-pre1", p, env)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip().splitlines()[-1]) == pre.resolve()
    rec = _record(p, "agent-pre1")
    assert rec["mode"] == "reservation_existing" and rec["branch_created"] == "no"


def test_existing_path_must_match(pr, tmp_path: Path) -> None:
    p, r = pr
    pre = tmp_path / "pre2"
    _git(r, "worktree", "add", "-q", "-b", "lane-pre2", str(pre), "HEAD")
    with pytest.raises(lr.ReservationError, match="not on branch"):
        lr.reserve(p, r, "HEAD", "other-branch", existing_path=str(pre))
    with pytest.raises(lr.ReservationError, match="not a registered worktree"):
        lr.reserve(p, r, "HEAD", "lane-pre2", existing_path=str(tmp_path))
    with pytest.raises(lr.ReservationError, match="main checkout"):
        lr.reserve(p, r, "HEAD", "main", existing_path=str(r))
    (pre / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(lr.ReservationError, match="uncommitted"):
        lr.reserve(p, r, "HEAD", "lane-pre2", existing_path=str(pre))


# ── SubagentStart injection ─────────────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_subagent_start_names_the_reservation(flavour: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", "lane-start", env, "--id", "lr-start")
    g = _guard(flavour, "agent-st1", p, env)
    assert g.returncode == 0, g.stderr
    wt = g.stdout.strip().splitlines()[-1]
    proc = _hook("subagent-start-isolation-check", flavour,
                 {"hook_event_name": "SubagentStart", "agent_id": "st1", "agent_type": "x"}, p, env)
    assert proc.returncode == 0, proc.stderr
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(f"VCO lane reservation lr-start: your worktree is {wt} (repo {r.resolve()}, "
                          f"branch lane-start, base {_sha(r)})")
    assert "If your brief names a different reservation id, STOP and report." in ctx


# ── SubagentStop reap, in R ─────────────────────────────────────────────────


def _stop(flavour: str, p: Path, agent_id: str, env: dict) -> None:
    proc = _hook("subagent-stop-reconcile", flavour,
                 {"hook_event_name": "SubagentStop", "agent_id": agent_id, "session_id": "s"}, p, env)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_reap_removes_unchanged_and_its_branch(flavour: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", "lane-clean", env, "--id", "clean")
    wt = Path(_guard(flavour, "agent-cl1", p, env).stdout.strip().splitlines()[-1])
    assert wt.is_dir() and wt.parent == (r.parent / "R.vco-lanes").resolve()
    assert _git(r, "show-ref", "--verify", "-q", "refs/heads/lane-clean", check=False).returncode == 0
    _stop(flavour, p, "cl1", env)
    assert not wt.exists()
    assert f"worktree {wt}" not in _git(r, "worktree", "list", "--porcelain").stdout
    assert _git(r, "show-ref", "--verify", "-q", "refs/heads/lane-clean", check=False).returncode != 0
    rec = _record(p, "agent-cl1")
    assert rec["status"] == "removed" and rec["status_reason"] == "clean"


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("change", ["edit", "commit"])
def test_reap_keeps_changed(flavour: str, change: str, pr, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    _reserve(p, r, "HEAD", f"lane-{change}", env, "--id", change)
    wt = Path(_guard(flavour, f"agent-{change}9", p, env).stdout.strip().splitlines()[-1])
    (wt / "f.txt").write_text("work\n", encoding="utf-8")
    if change == "commit":
        _git(wt, "commit", "-q", "-am", "work")
    _stop(flavour, p, f"{change}9", env)
    assert wt.is_dir()
    assert _git(r, "show-ref", "--verify", "-q", f"refs/heads/lane-{change}", check=False).returncode == 0
    rec = _record(p, f"agent-{change}9")
    assert rec["status"] == "kept"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_reap_never_removes_adopted_worktree(flavour: str, pr, tmp_path: Path, hub: _Hub) -> None:
    p, r = pr
    env = _env(hub, p)
    pre = tmp_path / "pre3"
    _git(r, "worktree", "add", "-q", "-b", "lane-pre3", str(pre), "HEAD")
    _reserve(p, r, "HEAD", "lane-pre3", env, "--id", "pre3", "--existing-path", str(pre))
    g = _guard(flavour, "agent-pre3", p, env)
    assert g.returncode == 0 and Path(g.stdout.strip().splitlines()[-1]) == pre.resolve()
    _stop(flavour, p, "pre3", env)
    assert pre.is_dir()
    rec = _record(p, "agent-pre3")
    assert rec["status"] == "created"  # skipped: never touched


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
