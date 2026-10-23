"""v0.2.104 E-2: the Write injection surface can no longer overrun its 10 s
PreToolUse registration.

Root causes measured on the pre-fix router (base 637dc1c4):

* the KG / code-graph producer modules were LOADED in the main thread before
  the run deadline existed — a producer whose import takes 8 s made the run
  take 8 s + the legs;
* a leg abandoned at its deadline whose producer had started NON-daemon
  threads (asyncio's default executor) kept the interpreter alive at exit
  until that work finished — a 4 s leg deadline, a 15 s process;
* the budget was counted from the router's ``_main``, not from the moment the
  harness spawned the wrapper.

Plus the wrapper spawned the router for files outside the project (a /tmp
scratch script), which have no KG context worth the cost.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
HOOKS = REPO_ROOT / "templates" / "hooks"
WRAP_SH = HOOKS / "pre-write-context-inject.sh"
WRAP_PS1 = HOOKS / "pre-write-context-inject.ps1"

_BASH = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3")), reason="bash required"
)
_PWSH = pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh required")

# Each producer is slow only in the INTERACTIVE run: the detached cache
# warmer the router spawns for a missed leg re-runs the same producer
# (``--warm-legs`` in its argv), and a test must not leave it sleeping.
SLOW_IMPORT_PRODUCER = """\
import os, sys, time
if "--warm-legs" not in sys.argv:
    open(os.environ["VCO_TEST_PRODUCER_MARKER"], "a").write("started")
    time.sleep(12)  # a cold producer import (weaviate-client, grpc, ...)
def main():
    print("KG: never | concept | score=0.9 | TITLES | src=x.md")
"""

SLOW_EXECUTOR_PRODUCER = """\
import asyncio, os, sys, time
async def main():
    if "--warm-legs" in sys.argv:
        return
    open(os.environ["VCO_TEST_PRODUCER_MARKER"], "a").write("started")
    loop = asyncio.get_running_loop()
    # A non-daemon executor worker: the interpreter joins it at exit.
    await loop.run_in_executor(None, time.sleep, 12)
"""


def _project(tmp_path: Path) -> Path:
    proj = Path(os.path.realpath(tmp_path)) / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}")
    (proj / "pkg").mkdir()
    return proj


def _router_env(base: dict, tmp_path: Path, proj: Path, producer: Path,
                **extra: str) -> dict:
    env = {
        **base,
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCT_STATE_DIR": str(tmp_path / "vct-state"),
        "WEAVIATE_URL": "http://127.0.0.1:9",
        "PYTHONPATH": str(REPO_ROOT),
        "VCO_ROUTER_KG_SCRIPT": str(producer),
        "VCO_INJECT_BUDGET_S": "2",
        "VCO_INJECT_LEG_TIMEOUT_S": "1.5",
    }
    env.pop("VCO_HOOK_STARTED_AT", None)
    env.update(extra)
    return env


def _write_payload(path: Path, cwd: Path) -> str:
    return json.dumps({"session_id": "sessW", "tool_name": "Write", "cwd": str(cwd),
                       "tool_input": {"file_path": str(path), "content": "x = 1\n"}})


def _timed_router(tmp_path: Path, producer_src: str, name: str) -> float:
    proj = _project(tmp_path)
    producer = tmp_path / f"{name}.py"
    producer.write_text(producer_src)
    marker = tmp_path / f"{name}.ran"
    env = _router_env(child_env(), tmp_path, proj, producer,
                      VCO_TEST_PRODUCER_MARKER=str(marker))
    start = time.monotonic()
    r = subprocess.run([sys.executable, str(ROUTER), "write"],
                       input=_write_payload(proj / "pkg" / f"{name}_new.py", proj),
                       capture_output=True, text=True, timeout=60, env=env)
    elapsed = time.monotonic() - start
    assert r.returncode == 0, r.stderr
    assert "KG: never" not in r.stdout
    assert marker.exists(), f"the slow producer never ran: {r.stderr[-800:]}"
    return elapsed


def test_slow_producer_import_is_bounded_by_the_run_deadline(tmp_path: Path) -> None:
    """RED pre-fix: ~12 s (the import ran in the main thread, unbounded)."""
    elapsed = _timed_router(tmp_path, SLOW_IMPORT_PRODUCER, "slow_import")
    assert elapsed < 8.0, f"router took {elapsed:.1f} s with a 2 s budget"


def test_abandoned_executor_leg_does_not_hold_the_process(tmp_path: Path) -> None:
    """RED pre-fix: ~12 s (concurrent.futures joined the worker at exit)."""
    elapsed = _timed_router(tmp_path, SLOW_EXECUTOR_PRODUCER, "slow_exec")
    assert elapsed < 8.0, f"router took {elapsed:.1f} s with a 2 s budget"


def test_deadline_anchor_honours_the_wrapper_start(monkeypatch) -> None:
    sys.path.insert(0, str(ROUTER.parent))
    import hook_context_router as hcr

    now = time.monotonic()
    # A real hook run: anchored at process start, earlier still when the
    # wrapper says it started 3 s ago.
    monkeypatch.setattr(hcr, "_DEADLINE_ANCHOR", now - 0.5)
    monkeypatch.setenv("VCO_HOOK_STARTED_AT", str(time.time() - 3.0))
    assert abs(hcr._run_deadline_anchor() - (now - 3.0)) < 0.5
    # Implausible values are ignored (future, ancient, garbage).
    for bad in (str(time.time() + 30), str(time.time() - 3600), "nope"):
        monkeypatch.setenv("VCO_HOOK_STARTED_AT", bad)
        assert abs(hcr._run_deadline_anchor() - (now - 0.5)) < 0.2
    # An in-process caller anchors at the call; the env is not consulted.
    monkeypatch.setattr(hcr, "_DEADLINE_ANCHOR", None)
    monkeypatch.setenv("VCO_HOOK_STARTED_AT", str(time.time() - 3.0))
    assert hcr._run_deadline_anchor() >= now


def test_router_budget_sits_comfortably_below_the_write_registration() -> None:
    """The budget (counted from the wrapper start since E-2) leaves at least
    3 s of the registered timeout for interpreter exit + emit."""
    router = ROUTER.read_text(encoding="utf-8")
    m = re.search(r'\benv_float\("VCO_INJECT_BUDGET_S", ([0-9.]+)[,)]', router)
    assert m
    budget = float(m.group(1))
    for tpl in ("settings.json.linux.template", "settings.json.windows.template"):
        doc = json.loads((REPO_ROOT / "templates" / tpl).read_text(encoding="utf-8"))
        timeouts = [h.get("timeout") for g in doc["hooks"]["PreToolUse"]
                    for h in g.get("hooks", [])
                    if "pre-write-context-inject" in h.get("command", "")]
        assert timeouts, tpl
        for t in timeouts:
            assert t - budget >= 3, f"{tpl}: timeout {t} vs budget {budget}"


# --- the wrapper's out-of-project skip --------------------------------------


def _fake_install(tmp_path: Path) -> tuple[Path, Path]:
    """An install root whose ROUTER is a recorder: each spawn appends the
    surface argument and the VCO_HOOK_STARTED_AT it inherited. The real
    interpreter serves as the venv python (VCT_VENV), so the fake is plain
    Python and runs on every OS."""
    root = tmp_path / "install"
    scripts = root / "claude_mcp_servers" / "scripts"
    scripts.mkdir(parents=True)
    log = tmp_path / "spawns.log"
    (scripts / "hook_context_router.py").write_text(
        "import os, sys\n"
        "sys.stdin.read()\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(sys.argv[1] + ' started=' + os.environ.get('VCO_HOOK_STARTED_AT', '') + '\\n')\n",
        encoding="utf-8")
    return Path(sys.executable), log


def _wrapper_env(tmp_path: Path, proj: Path, venv: Path) -> dict:
    env = {**child_env(), "CLAUDE_PROJECT_DIR": str(proj), "VCT_VENV": str(venv),
           "VCT_INSTALL_ROOT": str(tmp_path / "install"),
           "VCT_STATE_DIR": str(tmp_path / "vct-state"),
           "WEAVIATE_URL": "http://127.0.0.1:9"}
    for k in ("VCT_DISABLE_HOOKS", "VCO_INJECT_PROFILE", "VCO_HOOK_STARTED_AT"):
        env.pop(k, None)
    return env


def _check_wrapper(tmp_path: Path, argv: list[str]) -> None:
    proj = _project(tmp_path)
    venv, log = _fake_install(tmp_path)
    env = _wrapper_env(tmp_path, proj, venv)
    scratch = Path(os.path.realpath(tmp_path)) / "scratch"
    scratch.mkdir()
    extra = Path(os.path.realpath(tmp_path)) / "extra-repo"
    (extra / "src").mkdir(parents=True)
    (proj / ".claude" / "state" / "codegraph_extra_paths_cache.txt").write_text(f"{extra}\n")

    def run(path: Path) -> None:
        r = subprocess.run(argv, input=_write_payload(path, proj), capture_output=True,
                           text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stderr

    run(scratch / "probe.py")
    assert not log.exists(), "a Write outside the project must not spawn the router"
    run(proj / "pkg" / "mod.py")
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("write started=")
    started = float(lines[0].split("=", 1)[1])
    assert abs(time.time() - started) < 60, "VCO_HOOK_STARTED_AT must be the wrapper's start"
    run(extra / "src" / "lib.py")
    assert len(log.read_text().splitlines()) == 2, (
        "an enabled extra code folder is part of the project"
    )


@_BASH
def test_sh_wrapper_skips_writes_outside_the_project(tmp_path: Path) -> None:
    _check_wrapper(tmp_path, ["bash", str(WRAP_SH)])


@_PWSH
def test_ps1_wrapper_skips_writes_outside_the_project(tmp_path: Path) -> None:
    _check_wrapper(tmp_path, ["pwsh", *PWSH_BASE_FLAGS, "-File", str(WRAP_PS1)])
