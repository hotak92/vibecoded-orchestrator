# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W5R-03: the pre-edit code-graph leg searches as the CALLING project.

Owner rule: "each project should access codegraph/KG with that project's
permissions". Before this fix the pre-edit hook (both shells) ran
``detect-project`` and, for a file outside the project root but under its
parent folder, passed ``--project <sibling folder name>`` to the code-graph CLI.
An explicit ``--project`` short-circuits the CLI's calling-project resolution,
so a session in project A editing ``../B/x.py`` queried ``B_*`` classes with no
grant (cross-tenant leak), and a file under one of A's registered extra paths
never reached A's own prefix (where that path is indexed).

Now the hook never passes ``--project``; the CLI resolves the calling project's
prefix and fans out over the calling project's own grants:
  * no grant  -> the sibling is NOT searched (leave-alone);
  * grant     -> the granted peer IS searched, through the grant (act).
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

from tests.common.pre_edit_hook_sandbox import build_sandbox, invoke_hook, write_stub_producers

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "templates" / "hooks"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook"
)
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None or sys.platform == "win32", reason="pwsh on POSIX"
)

_ARGV_RECORDER = (
    "#!/usr/bin/env bash\n"
    'printf "%s\\n" "$@" > "$VCO_TEST_CG_ARGV"\n'
    "printf 'CODE: own.mod.f | CodeFunction | distance=0.2 |\\n  body\\n'\n"
)


def _sibling_file(tmp_path: Path) -> Path:
    sib = tmp_path / "Bar"
    sib.mkdir()
    f = sib / "widget.py"
    f.write_text("def widget():\n    pass\n", encoding="utf-8")
    return f


@needs_bash
def test_sh_pre_edit_never_overrides_the_project_for_a_sibling_file(tmp_path):
    env = build_sandbox(tmp_path)
    write_stub_producers(env, kg_lines=[], code_lines=[])
    cli = env["cg_dir"] / "code-graph-query"
    cli.write_text(_ARGV_RECORDER, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    # Ship the REAL detect-project.sh where the old hook sourced it from, so a
    # regression to the folder-name heuristic actually fires in this sandbox.
    (env["install_root"] / "templates" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "templates" / "scripts" / "detect-project.sh",
                env["install_root"] / "templates" / "scripts" / "detect-project.sh")
    argv_file = tmp_path / "argv.txt"
    target = _sibling_file(tmp_path)  # a sibling of the sandbox project root
    proc = invoke_hook(env, "sess-w5r03", str(target), extra_env={
        "VCO_TEST_CG_ARGV": str(argv_file),
        "CLAUDE_PROJECT_DIR": str(env["install_root"]),
    })
    assert proc.returncode == 0, proc.stderr
    assert argv_file.is_file(), ("the code-graph leg did not run", proc.stdout, proc.stderr)
    argv = argv_file.read_text("utf-8").splitlines()
    assert "--project" not in argv and "Bar" not in argv, argv


@needs_pwsh
def test_ps1_pre_edit_never_overrides_the_project_for_a_sibling_file(tmp_path):
    """The .ps1 heuristic was inert in practice (detect-project.ps1 run with
    -File only DEFINES a function and prints nothing), so this guards the end
    state on Windows rather than reproducing a live leak there."""
    orch = tmp_path / "orch"
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    shutil.copytree(HOOKS, orch / "templates" / "hooks")
    shutil.copytree(REPO / "templates" / "scripts", orch / "templates" / "scripts")
    # Ship a REAL detect-project.ps1 where the old code looked for it, so a
    # regression to the folder-name heuristic would actually fire here.
    shutil.copy(REPO / "templates" / "scripts" / "detect-project.ps1",
                proj / ".claude" / "scripts" / "detect-project.ps1")
    cli = proj / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(_ARGV_RECORDER, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    target = _sibling_file(tmp_path)
    argv_file = tmp_path / "argv.txt"
    payload = {"tool_name": "Edit", "session_id": "sess-w5r03ps",
               "tool_input": {"file_path": str(target), "new_string": "def widget(): return 1"}}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VCT_", "VCO_", "CLAUDE_"))}
    env.update({
        "CLAUDE_PROJECT_DIR": str(proj),
        "HOME": str(tmp_path / "home"),
        "WEAVIATE_URL": "http://127.0.0.1:9",
        "VCO_TEST_CG_ARGV": str(argv_file),
    })
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(orch / "templates" / "hooks" / "pre-edit-context-inject.ps1")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert argv_file.is_file(), ("the code-graph leg did not run", proc.stdout, proc.stderr)
    argv = argv_file.read_text("utf-8").splitlines()
    assert "--project" not in argv and "Bar" not in argv, argv


@pytest.mark.parametrize("hook", ["pre-edit-context-inject.sh", "pre-edit-context-inject.ps1"])
def test_no_search_hook_consults_the_folder_name_heuristic(hook):
    text = (HOOKS / hook).read_text(encoding="utf-8-sig")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "detect-project" not in code and "detect_project_for_file" not in code


def _fanout(monkeypatch, access_list):
    for p in (str(REPO / "claude_mcp_servers" / "scripts"),):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    kg_access = importlib.import_module("kg_access")

    if access_list is None:
        monkeypatch.delenv("VCT_CODE_GRAPH_ACCESS_LIST", raising=False)
    else:
        monkeypatch.setenv("VCT_CODE_GRAPH_ACCESS_LIST", access_list)
    return [c for c, _ in kg_access.code_graph_collections_to_query("ProjA", bases=("CodeFunction",))]


def test_calling_project_without_a_grant_does_not_search_the_sibling(monkeypatch):
    cols = _fanout(monkeypatch, None)
    assert cols == ["ProjA_CodeFunction"], cols


def test_calling_project_with_a_grant_searches_the_granted_peer(monkeypatch):
    cols = _fanout(monkeypatch, "Bar")
    assert cols == ["ProjA_CodeFunction", "Bar_CodeFunction"], cols
