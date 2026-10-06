# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared sandbox for BEHAVIOURAL tests of ``pre-edit-context-inject.sh``.

Extracted from ``tests/test_pre_edit_hook_dedup_regression.py`` in v0.2.91 when
a second suite (``test_v0291_perf_quickwins.py``) needed the same rig. Per
CLAUDE.md "extract before you duplicate": ONE home for the layout + stub
producers + invoker, two callers.

What it builds
--------------
A throwaway ``$VCT_INSTALL_ROOT`` that satisfies every path probe the hook does,
without touching the real project tree:

    install/
      templates/hooks/pre-edit-context-inject.sh   (the REAL hook under test)
      templates/hooks/_lib/…                       (REAL helpers + minimal stubs)
      claude_mcp_servers/scripts/rl_kg_search.py   (STUB producer)
      .claude/scripts/code-graph-query             (STUB producer)
      .claude/state/                               (per-session stores + caches)
      .venv/bin/python -> system python3

The hook computes ``PROJECT_ROOT`` as ``$SCRIPT_DIR/../..``, which from
``templates/hooks/`` is the sandbox root — so the state dir, the per-file cache
and the shared query cache all land inside the sandbox.

Requires bash + a system ``python3``; Linux/macOS only (the ``.ps1`` mirror is
covered by the body-parity suites).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_SRC = REPO_ROOT / "templates" / "hooks" / "pre-edit-context-inject.sh"

# REAL production helpers copied into the sandbox so the tests exercise the
# shipped dedup / session-id / codegraph / query-cache code paths rather than
# the partial-install fallbacks.
# v0.2.101 Wave 2: the pre-edit hook is a thin ROUTER wrapper — the
# codegraph-query / query-cache / code-extensions libs are no longer sourced
# by it (the router owns querying, caching and the code-file decision), so
# they left this list WITH their caller. inject-budget.sh joins (the kill
# switch the wrapper checks before spawning).
_REAL_LIBS = (
    "seen-store.sh",
    "session-id.sh",
    "resolve-vco-venv.sh",
    "inject-budget.sh",
)

# Minimal emit_additional_context that wraps the context in the PreToolUse JSON
# envelope on stdout, mirroring the production helper's contract
# (whitespace-only context -> no emit).
_EMIT_STUB = (
    "emit_additional_context() {\n"
    '    local ctx="$1"; local phase="$2"\n'
    "    case \"$ctx\" in\n"
    "        *[![:space:]]*) ;;\n"
    "        *) return 0 ;;\n"
    "    esac\n"
    "    local json_ctx\n"
    "    json_ctx=$(printf '%s' \"$ctx\" | python3 -c "
    "'import sys,json; print(json.dumps(sys.stdin.read()))')\n"
    "    printf '{\"hookSpecificOutput\":{\"additionalContext\":%s,"
    '"hookEventName":"%s"}}\\n\' "$json_ctx" "$phase"\n'
    "}\n"
)


def build_sandbox(tmp_path: Path) -> dict:
    """Materialize the sandbox under ``tmp_path``; return its key paths."""
    install_root = tmp_path / "install"
    (install_root / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    (install_root / ".claude" / "scripts").mkdir(parents=True)
    (install_root / ".claude" / "state").mkdir(parents=True)
    lib_dir = install_root / "templates" / "hooks" / "_lib"
    lib_dir.mkdir(parents=True)

    (lib_dir / "stderr-cap.sh").write_text("# noop stderr-cap stub\n", encoding="utf-8")
    (lib_dir / "emit-context.sh").write_text(_EMIT_STUB, encoding="utf-8")
    (lib_dir / "find-python.sh").write_text('PY="$(command -v python3)"\n', encoding="utf-8")

    for name in _REAL_LIBS:
        src = REPO_ROOT / "templates" / "hooks" / "_lib" / name
        if src.exists():
            shutil.copy(src, lib_dir / name)

    # (No detect-project stub: v0.2.100 W5R-03 removed the hook's folder-name
    # project override; the code-graph leg always runs as the calling project.)

    # Fake .venv pointing at system python3 (the hook resolves the venv via the
    # REAL resolve-vco-venv.sh against $VCT_INSTALL_ROOT).
    venv_bin = install_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    system_python = shutil.which("python3") or sys.executable
    os.symlink(system_python, venv_bin / "python")

    sandbox_hook = install_root / "templates" / "hooks" / "pre-edit-context-inject.sh"
    sandbox_hook.write_bytes(HOOK_SRC.read_bytes())
    sandbox_hook.chmod(0o755)

    return {
        "install_root": install_root,
        "hook_path": sandbox_hook,
        "lib_dir": lib_dir,
        "state_dir": install_root / ".claude" / "state",
        "scripts_dir": install_root / "claude_mcp_servers" / "scripts",
        "cg_dir": install_root / ".claude" / "scripts",
    }


def write_stub_producers(env: dict, kg_lines: list, code_lines: list) -> None:
    """Install stub producers that emit the given lines ONLY with --hook-format.

    Mirroring the real producers' ``--hook-format`` gate matters: without it, a
    caller that DROPPED the flag would still get prefixed stdout from the stub
    and a dedup regression would falsely pass.

    v0.2.101 Wave 2: the hook is a thin ROUTER wrapper — the router LOADS both
    producers in-process (``hook_dual_search._load_cg_module`` + the pinned-argv
    shim) and calls ``main()``, so both stubs are Python MODULES accepting the
    router's argv (KG: ``query --limit N --hook-format --injection-profile P
    --task-type T [--transcript …]``; CG: ``structure callers <sym>
    --hook-format [--source-file …] [--exclude-file …] [--indexed-revision]``).
    Each stub appends its pinned argv to its marker file (``VCO_TEST_KG_MARKER``
    / ``VCO_TEST_CG_MARKER``) so tests can prove WHICH path ran and how often.
    The legacy bash ``code-graph-query`` CLI stub is kept for suites that still
    pin the shell ``codegraph_query_block`` helper directly.
    """
    rl = env["scripts_dir"] / "rl_kg_search.py"
    rl_lines_repr = ",\n        ".join(repr(line) for line in kg_lines) or "''"
    # The router pins each producer's argv through hook_dual_search's shim
    # (NOT sys.argv), so the stubs capture it with a REMAINDER positional —
    # the recorded argv is byte-for-byte what the router passed.
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, os\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser(add_help=False)\n"
        "    ap.add_argument('rest', nargs=argparse.REMAINDER)\n"
        "    ns = ap.parse_args()\n"
        "    marker = os.environ.get('VCO_TEST_KG_MARKER', '')\n"
        "    if marker:\n"
        "        with open(marker, 'a') as fh:\n"
        "            fh.write(json.dumps(ns.rest) + '\\n')\n"
        "    if '--hook-format' in ns.rest:\n"
        "        for _line in [\n            " + rl_lines_repr + ",\n        ]:\n"
        "            print(_line)\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    # (v0.2.101 wave-2 SF-2: the legacy bash `code-graph-query` CLI stub was
    # retired with codegraph_query_block — nothing spawns the CLI wrapper any
    # more; the router loads the MODULE below.)
    # The router loads `query_code_graph.py` as a MODULE from the project's
    # .claude/scripts (mirroring the real installed layout). REMAINDER
    # capture records the router's PINNED argv (not sys.argv).
    cg_py_lines = "\n        ".join(f"print({line!r})" for line in code_lines) or "pass"
    (env["cg_dir"] / "query_code_graph.py").write_text(
        "import argparse, json, os\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser(add_help=False)\n"
        "    ap.add_argument('rest', nargs=argparse.REMAINDER)\n"
        "    ns = ap.parse_args()\n"
        "    marker = os.environ.get('VCO_TEST_CG_MARKER', '')\n"
        "    if marker:\n"
        "        with open(marker, 'a') as fh:\n"
        "            fh.write(json.dumps(ns.rest) + '\\n')\n"
        "    if '--hook-format' in ns.rest:\n"
        "        " + cg_py_lines + "\n"
        "    return 0\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_dual_driver(env: dict) -> Path:
    """Copy the REAL ``hook_dual_search.py`` into the sandbox (the router
    imports its run_legs/_pin_argv/_load_cg_module mechanism from beside
    itself). Historical name kept for existing callers."""
    src = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_dual_search.py"
    dest = env["scripts_dir"] / src.name
    dest.write_bytes(src.read_bytes())
    return dest


def install_router(env: dict) -> Path:
    """Copy the REAL ``hook_context_router.py`` into the sandbox's
    orchestrator-root scripts dir (v0.2.101: the hook resolves and runs it
    from $VCT_INSTALL_ROOT). Pair with ``invoke_hook``'s PYTHONPATH pin so
    the router's ``vco_lib`` imports resolve from the checkout."""
    src = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
    dest = env["scripts_dir"] / src.name
    dest.write_bytes(src.read_bytes())
    return dest


def invoke_hook(
    env: dict,
    session_id: str,
    file_path: str,
    *,
    old_string: str = "",
    extra_env: "dict | None" = None,
) -> subprocess.CompletedProcess:
    """Call the hook with a synthetic Edit payload on stdin.

    v0.2.101 Wave 2: PYTHONPATH pins the checkout so the router the hook
    spawns resolves ``vco_lib`` from THIS tree (the sandbox's fake
    orchestrator root has no vco_lib), and the kill-switch envs are scrubbed
    so an ambient VCT_DISABLE_HOOKS/VCO_INJECT_PROFILE can't silently skip
    the run. ``old_string`` feeds the router's enclosing-symbol extraction.
    """
    tool_input = {"file_path": file_path, "new_string": "def f(): pass\n"}
    if old_string:
        tool_input["old_string"] = old_string
    payload = {
        "tool_name": "Edit",
        "session_id": session_id,
        "cwd": str(env["install_root"]),
        "tool_input": tool_input,
    }
    # v0.2.29 moved CACHE_BASE into `.claude/state/edit_cache_*`, so the legacy
    # `install_root/tmp/` is no longer created as a side effect — create it here
    # so the hook's `mktemp` calls succeed under the pinned TMPDIR.
    tmpdir = env["install_root"] / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    proc_env = {
        k: v for k, v in os.environ.items()
        if k not in ("VCT_DISABLE_HOOKS", "VCO_INJECT_PROFILE",
                     "VCO_RL_TASK_TYPE", "VCT_VENV")
    }
    proc_env.update({
        "VCT_INSTALL_ROOT": str(env["install_root"]),
        "TMPDIR": str(tmpdir),
        "PYTHONPATH": str(REPO_ROOT),
        "RL_HUB_POST_DISABLED": "1",
    })
    if extra_env:
        proc_env.update(extra_env)
    return subprocess.run(
        ["bash", str(env["hook_path"])],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=proc_env,
    )
