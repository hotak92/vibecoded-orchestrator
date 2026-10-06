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
import shutil
import sys
from pathlib import Path

import pytest

from tests.common.pre_edit_hook_sandbox import (
    build_sandbox,
    install_dual_driver,
    install_router,
    invoke_hook,
    write_stub_producers,
)

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
    """W5R-03, router era: the code-graph leg's argv must carry NO --project
    override and no folder-name-derived project ("Bar") — the producer
    resolves the CALLING project itself (CLAUDE_PROJECT_DIR → hub). The
    argv recorder is the sandbox CG module stub (the router pins its argv
    through the hook_dual_search shim, recorded via VCO_TEST_CG_MARKER)."""
    env = build_sandbox(tmp_path)
    write_stub_producers(
        env, kg_lines=[],
        code_lines=["CODE: own.mod.f | CodeFunction | distance=0.2 |"],
    )
    install_dual_driver(env)
    install_router(env)
    # Ship the REAL detect-project.sh where the old hook sourced it from, so a
    # regression to the folder-name heuristic actually fires in this sandbox.
    (env["install_root"] / "templates" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "templates" / "scripts" / "detect-project.sh",
                env["install_root"] / "templates" / "scripts" / "detect-project.sh")
    argv_file = tmp_path / "argv.jsonl"
    target = _sibling_file(tmp_path)  # a sibling of the sandbox project root
    proc = invoke_hook(env, "sess-w5r03", str(target), old_string="pass",
                       extra_env={
                           "VCO_TEST_CG_MARKER": str(argv_file),
                           "CLAUDE_PROJECT_DIR": str(env["install_root"]),
                       })
    assert proc.returncode == 0, proc.stderr
    assert argv_file.is_file(), ("the code-graph leg did not run", proc.stdout, proc.stderr[-800:])
    argv = json.loads(argv_file.read_text("utf-8").splitlines()[0])
    assert "--project" not in argv, argv
    assert "Bar" not in argv, f"folder-name heuristic leaked into the argv: {argv}"


@needs_pwsh
def test_ps1_pre_edit_never_overrides_the_project_for_a_sibling_file(tmp_path):
    """The .ps1 heuristic was inert in practice (detect-project.ps1 run with
    -File only DEFINES a function and prints nothing), so this guarded the
    END STATE on Windows rather than reproducing a live leak there.

    v0.2.101 retarget: the .ps1 wrapper no longer calls any code-graph CLI —
    it drives hook_context_router.py, whose producer argv is cross-OS Python
    (the behavioural no---project pin is the .sh router test above; this row
    keeps the Windows-side END-STATE guard as a source scan of the wrapper:
    no project-override machinery, no direct producer call)."""
    body = (HOOKS / "pre-edit-context-inject.ps1").read_text(encoding="utf-8-sig")
    code = "\n".join(
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith(("#", "<#"))
    )
    assert "detect-project" not in code, "the folder-name heuristic crept back"
    assert "--project" not in code, (
        "the wrapper must never pass a project override — the producer "
        "resolves the CALLING project (W5R-03)"
    )
    assert "CODE_GRAPH_PROJECT_ARG" not in code
    assert "code-graph-query" not in code, (
        "direct CG producer call crept back into the wrapper — the router "
        "owns producer argv"
    )
    assert "hook_context_router.py" in code, (
        "the .ps1 wrapper must drive the router"
    )


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
