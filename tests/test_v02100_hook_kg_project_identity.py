# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 F3 — the hook KG search runs in EVERY project, with THAT project's
identity and permissions.

Before: the hooks looked for ``rl_kg_search.py`` (and the citation drain) under
the PROJECT root, where only the orchestrator root has it — so no user project
ever ran the hook KG leg or drained a citation. The owner's constraint on the
fix: locating the script in the orchestrator root must NOT make the search use
the orchestrator's permissions — project A sees its own KG + the shared KG +
A's grants (never the root's KG, never an ungranted project B), and the code
graph resolves A's prefix (where A's extra code-graph paths are indexed).

Layers covered:
  * the shell/PowerShell locator (``resolve_vco_orchestrator_script``);
  * a REAL hook run from a project that does not contain the script;
  * the identity the root-located script resolves (hub per-project config and
    the env fallback), run as a fresh process like a hook would;
  * the code-graph CLI's project identity when the cwd is another checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"
HARNESS = REPO_ROOT / "tests" / "common" / "rl_kg_search_harness.py"
RL_REL = "claude_mcp_servers/scripts/rl_kg_search.py"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook"
)


def _orch_and_project(tmp_path: Path):
    orch = tmp_path / "orch"
    (orch / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    vb = orch / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(shutil.which("python3") or sys.executable, vb / "python")
    proj = tmp_path / "proj_a"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "logs").mkdir(parents=True)
    return orch, proj


def _identity_producer(orch: Path) -> None:
    """Stub producer that reports the identity it was run with."""
    rl = orch / RL_REL
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print('KG: identity cpd=' + os.environ.get('CLAUDE_PROJECT_DIR', '') + "
        "' kg=' + os.environ.get('KG_COLLECTION', '') + ' | concept | score=0.90 | FULL NODE:')\n"
        "print('body')\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Locator
# ---------------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("which", ["VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"])
def test_shell_locator_finds_the_script_in_the_orchestrator_root(tmp_path, which):
    orch, proj = _orch_and_project(tmp_path)
    (orch / RL_REL).write_text("")
    env = {k: v for k, v in os.environ.items() if k not in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")}
    env[which] = str(orch)
    out = subprocess.run(
        ["bash", "-c",
         f'. "{HOOKS}/_lib/resolve-vco-venv.sh"; '
         f'resolve_vco_orchestrator_script "{proj}/.claude/hooks" "{RL_REL}" "{proj}"; '
         'printf %s "$VCO_ORCHESTRATOR_SCRIPT"'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.stdout == str(orch / RL_REL)


@needs_bash
def test_shell_locator_is_empty_when_nothing_ships_it(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")}
    out = subprocess.run(
        ["bash", "-c",
         f'. "{HOOKS}/_lib/resolve-vco-venv.sh"; '
         f'resolve_vco_orchestrator_script "{proj}/.claude/hooks" "{RL_REL}" "{proj}"; '
         'printf %s "$VCO_ORCHESTRATOR_SCRIPT"'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.stdout == ""


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_powershell_locator_matches_the_shell_one(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    (orch / RL_REL).write_text("")
    env = {k: v for k, v in os.environ.items() if k not in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")}
    env["VCT_ORCHESTRATOR_ROOT"] = str(orch)
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command",
         f'. "{HOOKS}/_lib/resolve-vco-venv.ps1"; '
         f'Resolve-VcoOrchestratorScript -ScriptDir "{proj}/.claude/hooks" -RelPath "{RL_REL}" -ProjectRoot "{proj}"'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert Path(out.stdout.strip()) == orch / RL_REL, out.stderr


# ---------------------------------------------------------------------------
# A real hook, run from a project that does not ship the script
# ---------------------------------------------------------------------------


def _hook_env(orch: Path, proj: Path) -> dict:
    env = os.environ.copy()
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_ORCHESTRATOR_ROOT", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["VCT_INSTALL_ROOT"] = str(orch)
    env["KG_COLLECTION"] = "ProjA_KnowledgeGraph"
    env["VCT_STATE_DIR"] = str(proj.parent / "vctstate")  # cold result caches
    env["VCT_BASH_KG_THRESHOLD_CHARS"] = "10"  # let a short command reach the KG leg
    return env


@needs_bash
def test_subagent_hook_runs_the_root_script_with_the_project_identity(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    _identity_producer(orch)
    payload = {"prompt": "implement the widget reranker", "session_id": "s-f3",
               "agent_id": "a1", "agent_type": "@agent-coder"}
    proc = subprocess.run(
        ["bash", str(HOOKS / "subagent-start-kg-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_hook_env(orch, proj), cwd=str(orch), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cpd={proj}" in proc.stdout, proc.stdout
    assert "kg=ProjA_KnowledgeGraph" in proc.stdout


@needs_bash
def test_pre_bash_hook_runs_the_root_script_with_the_project_identity(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    _identity_producer(orch)
    payload = {"tool_name": "Bash", "session_id": "s-f3b",
               "tool_input": {"command": "python -m pytest tests/test_widget_reranker.py"}}
    proc = subprocess.run(
        ["bash", str(HOOKS / "pre-bash-context-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_hook_env(orch, proj), cwd=str(orch), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cpd={proj}" in proc.stdout, (proc.stdout, proc.stderr[-1500:])


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_subagent_hook_ps1_runs_the_root_script_with_the_project_identity(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    _identity_producer(orch)
    payload = {"prompt": "implement the widget reranker ps", "session_id": "s-f3p",
               "agent_id": "a2", "agent_type": "@agent-coder"}
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "subagent-start-kg-inject.ps1")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_hook_env(orch, proj), cwd=str(orch), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"cpd={proj}" in proc.stdout, (proc.stdout, proc.stderr[-1500:])


# ---------------------------------------------------------------------------
# The identity the root-located script resolves (fresh process, like a hook)
# ---------------------------------------------------------------------------


def _run_harness(tmp_path: Path, *, project_dir: Path, configs, extra_env=None, cwd=None):
    res = tmp_path / f"res_{project_dir.name}.json"
    cfg = {"result_path": str(res), "project_configs": configs}
    cfg_path = tmp_path / f"cfg_{project_dir.name}.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {k: v for k, v in os.environ.items() if k not in (
        "KG_COLLECTION", "SHARED_KG_COLLECTION", "VCT_KG_ACCESS_LIST",
        "SHARED_KG_READ_DISABLED", "VCT_DISABLE_HUB_RESOLVER",
    )}
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if configs is None:
        env["VCT_DISABLE_HUB_RESOLVER"] = "1"  # hub "down": env fallback only
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(cfg_path)],
        env=child_env(env), capture_output=True, text=True, timeout=60, cwd=str(cwd or REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(res.read_text())


def _configs(proj_a: Path) -> dict:
    # Stems are registered fixture names (vco_lib.fixture_class_guard):
    # Foo = the orchestrator root, Beta = an ungranted peer, Gamma = A's grant.
    return {
        str(REPO_ROOT.resolve()): {
            "kg_collection": "Foo_KnowledgeGraph",
            "shared_kg_collection": "Shared_KnowledgeGraph",
            "kg_access_list": ["Foo_KnowledgeGraph", "Shared_KnowledgeGraph", "Beta_KnowledgeGraph"],
        },
        str(proj_a.resolve()): {
            "kg_collection": "ProjA_KnowledgeGraph",
            "shared_kg_collection": "Shared_KnowledgeGraph",
            "kg_access_list": ["ProjA_KnowledgeGraph", "Shared_KnowledgeGraph", "Gamma_KnowledgeGraph"],
        },
    }


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj_a"
    (p / ".claude").mkdir(parents=True, exist_ok=True)
    return p


def test_project_hook_search_queries_exactly_its_own_shared_and_grants(tmp_path):
    proj = _project(tmp_path)
    out = _run_harness(tmp_path, project_dir=proj, configs=_configs(proj))
    queried = out["queried"]
    assert queried == ["ProjA_KnowledgeGraph", "Shared_KnowledgeGraph", "Gamma_KnowledgeGraph"], queried
    assert "Foo_KnowledgeGraph" not in queried and "Beta_KnowledgeGraph" not in queried
    assert out["asked_paths"] and set(out["asked_paths"]) == {str(proj.resolve())}, out["asked_paths"]


def test_project_shared_read_gate_is_the_projects_own(tmp_path):
    proj = _project(tmp_path)
    out = _run_harness(tmp_path, project_dir=proj, configs=_configs(proj),
                       extra_env={"SHARED_KG_READ_DISABLED": "true"})
    assert out["queried"] == ["ProjA_KnowledgeGraph", "Gamma_KnowledgeGraph"], out["queried"]


def test_project_identity_holds_with_the_hub_down(tmp_path):
    """Hub unreachable: the env the hook inherited (the PROJECT's
    .claude/settings.json env) decides — never the root's."""
    proj = _project(tmp_path)
    out = _run_harness(tmp_path, project_dir=proj, configs=None, extra_env={
        "KG_COLLECTION": "ProjA_KnowledgeGraph",
        "SHARED_KG_COLLECTION": "Shared_KnowledgeGraph",
        "VCT_KG_ACCESS_LIST": "Gamma",
    })
    assert out["queried"] == ["ProjA_KnowledgeGraph", "Shared_KnowledgeGraph", "Gamma_KnowledgeGraph"]


def test_orchestrator_root_still_resolves_its_own_identity(tmp_path):
    out = _run_harness(tmp_path, project_dir=REPO_ROOT, configs=_configs(_project(tmp_path)))
    assert out["queried"] == ["Foo_KnowledgeGraph", "Shared_KnowledgeGraph", "Beta_KnowledgeGraph"]


# ---------------------------------------------------------------------------
# Code graph: the calling project's prefix, even with the cwd in another checkout
# ---------------------------------------------------------------------------


def test_code_graph_cli_uses_the_calling_projects_prefix_from_another_cwd(tmp_path, monkeypatch):
    """A project whose extra code-graph path points at another checkout has
    that checkout's entities indexed under ITS OWN prefix. A hook firing while
    the session's cwd sits in that checkout must still query the project's
    prefix — not the checkout's own registration."""
    proj = _project(tmp_path)
    extra = tmp_path / "public_clone"
    (extra / ".claude").mkdir(parents=True)
    import vco_lib.project_config as pc

    table = {
        str(proj.resolve()): "ProjA",
        str(extra.resolve()): "PublicClone",
    }

    class _Cfg:
        def __init__(self, prefix):
            self.code_graph_collection_prefix = prefix
            self.code_graph_project = prefix

    monkeypatch.setattr(pc, "resolve", lambda root: _Cfg(table[str(Path(root).resolve())]))
    for p in (str(REPO_ROOT / "templates" / "scripts"), str(REPO_ROOT / "claude_mcp_servers")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    sys.modules.pop("query_code_graph", None)  # fresh: bind the LIVE pipeline/server modules
    qcg = importlib.import_module("query_code_graph")
    seen = {}

    class _Q:
        def __init__(self, project=None):
            seen["project"] = project

        def connect(self):
            return False

    monkeypatch.setattr(qcg, "CodeGraphQuery", _Q)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    monkeypatch.chdir(extra)
    monkeypatch.setattr(sys, "argv", ["query_code_graph.py", "search", "widget", "--limit", "1"])
    qcg.main()
    assert seen["project"] == "ProjA"


@needs_bash
def test_pre_edit_hook_runs_the_root_script_with_the_project_identity(tmp_path):
    orch, proj = _orch_and_project(tmp_path)
    _identity_producer(orch)
    target = proj / "notes.md"
    target.write_text("hello\n")
    payload = {"tool_name": "Edit", "session_id": "s-f3e",
               "tool_input": {"file_path": str(target), "new_string": "widget reranker notes\n"}}
    proc = subprocess.run(
        ["bash", str(HOOKS / "pre-edit-context-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_hook_env(orch, proj), cwd=str(orch), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "cpd=" + str(proj) in proc.stdout, (proc.stdout, proc.stderr[-1500:])


@needs_bash
def test_stop_drain_runs_the_root_drain_for_the_project(tmp_path):
    """The turn-end citation drain also ships only in the orchestrator root;
    pre-F3 no user project ever drained a staged citation. The drain is
    detached by design, so the stub records what it was run with and the test
    waits (bounded) for that record."""
    orch, proj = _orch_and_project(tmp_path)
    marker = tmp_path / "drain_ran.json"
    drain = orch / "claude_mcp_servers" / "scripts" / "rl_drain_citations.py"
    drain.write_text(
        "import json, os, sys\n"
        f"open({str(marker)!r}, 'w').write(json.dumps({{'cpd': os.environ.get('CLAUDE_PROJECT_DIR', ''), 'argv': sys.argv[1:]}}))\n",
        encoding="utf-8",
    )
    payload = {"session_id": "s-drain", "transcript_path": str(tmp_path / "t.jsonl")}
    proc = subprocess.run(
        ["bash", str(HOOKS / "stop-drain-citations.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_hook_env(orch, proj), cwd=str(orch), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    import time as _t

    deadline = _t.monotonic() + 10
    while not marker.exists() and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert marker.exists(), "the root-located drain never ran"
    rec = json.loads(marker.read_text())
    assert rec["cpd"] == str(proj)
    assert "s-drain" in rec["argv"]
