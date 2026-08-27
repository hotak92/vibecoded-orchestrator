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
      .claude/scripts/detect-project.sh            (stub)
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
_REAL_LIBS = (
    "seen-store.sh",
    "session-id.sh",
    "codegraph-query.sh",
    "query-cache.sh",
    "resolve-vco-venv.sh",
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

    # detect-project stub — multi-codebase detection is irrelevant here.
    (install_root / ".claude" / "scripts" / "detect-project.sh").write_text(
        'detect_project_for_file() { echo ""; }\n', encoding="utf-8"
    )

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
    hook that DROPPED the flag would still get prefixed stdout from the stub and
    a dedup regression would falsely pass.

    Producer-invocation quirks:
      - the KG producer is invoked through the venv Python, so the stub MUST be
        a Python script (a bash shebang would be ignored and Python would try to
        parse bash);
      - the code-graph producer is invoked via a shell wrapper, so a bash script
        with the execute bit is correct.
    """
    rl = env["scripts_dir"] / "rl_kg_search.py"
    cg = env["cg_dir"] / "code-graph-query"
    rl_lines_repr = ",\n        ".join(repr(line) for line in kg_lines) or "''"
    # MODULE-SHAPED (v0.2.91): `async def main()` + a `__main__` guard, so the
    # SAME stub serves BOTH the legacy path (spawned as a CLI) and the P2 merged
    # path (imported by hook_dual_search.py and called as `main()`).
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, argparse, asyncio\n"
        "async def main():\n"
        "    # Mirror the real producer's argparse so --hook-format is accepted.\n"
        "    # Without the flag, emit nothing (matches the real producer).\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('query')\n"
        "    ap.add_argument('--limit', type=int, default=1)\n"
        "    ap.add_argument('--hook-format', action='store_true')\n"
        "    args = ap.parse_args()\n"
        "    if not args.hook_format:\n"
        "        return\n"
        "    for _line in [\n        " + rl_lines_repr + ",\n    ]:\n"
        "        print(_line)\n"
        "if __name__ == '__main__':\n"
        "    asyncio.run(main())\n",
        encoding="utf-8",
    )
    cg_lines_emit = "\n    ".join(f'printf "%s\\n" "{line}"' for line in code_lines)
    cg.write_text(
        "#!/usr/bin/env bash\n"
        "# Mirror the real code-graph-query's --hook-format gate.\n"
        '# Records its invocation so a test can prove which path ran.\n'
        '[ -n "${VCO_TEST_CG_CLI_MARKER:-}" ] && : > "$VCO_TEST_CG_CLI_MARKER"\n'
        "_has_hook_format=0\n"
        'for a in "$@"; do\n'
        '    if [ "$a" = "--hook-format" ]; then _has_hook_format=1; fi\n'
        "done\n"
        'if [ "$_has_hook_format" = "1" ]; then\n'
        "    " + cg_lines_emit + "\n"
        "fi\n",
        encoding="utf-8",
    )
    # The merged path loads `query_code_graph.py` as a module from beside the
    # CLI, mirroring the real installed layout.
    cg_py_lines = "\n        ".join(f"print({line!r})" for line in code_lines) or "pass"
    (env["cg_dir"] / "query_code_graph.py").write_text(
        "import argparse\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    sub = ap.add_subparsers(dest='command')\n"
        "    s = sub.add_parser('search')\n"
        "    s.add_argument('query')\n"
        "    s.add_argument('--limit', type=int, default=2)\n"
        "    s.add_argument('--hook-format', action='store_true')\n"
        "    s.add_argument('--project', default=None)\n"
        "    s.add_argument('--anchor', default=None)\n"
        "    s.add_argument('--exclude-file', default=None)\n"
        "    a = ap.parse_args()\n"
        "    if a.hook_format:\n"
        "        " + cg_py_lines + "\n"
        "    return 0\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cg.chmod(cg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_dual_driver(env: dict) -> Path:
    """Copy the REAL ``hook_dual_search.py`` into the sandbox.

    Without it, ``vco_dual_search_cached`` returns its fallback signal and the
    hook uses the legacy two-process path — which is a valid state to test, but
    NOT the merged one. Call this to exercise P2 end-to-end through the hook.
    """
    src = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_dual_search.py"
    dest = env["scripts_dir"] / src.name
    dest.write_bytes(src.read_bytes())
    return dest


def invoke_hook(
    env: dict,
    session_id: str,
    file_path: str,
    *,
    extra_env: "dict | None" = None,
) -> subprocess.CompletedProcess:
    """Call the hook with a synthetic Edit payload on stdin."""
    payload = {
        "tool_name": "Edit",
        "session_id": session_id,
        "tool_input": {"file_path": file_path, "new_string": "def f(): pass\n"},
    }
    # v0.2.29 moved CACHE_BASE into `.claude/state/edit_cache_*`, so the legacy
    # `install_root/tmp/` is no longer created as a side effect — create it here
    # so the hook's `mktemp` calls succeed under the pinned TMPDIR.
    tmpdir = env["install_root"] / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    proc_env = {
        **os.environ,
        "VCT_INSTALL_ROOT": str(env["install_root"]),
        "TMPDIR": str(tmpdir),
    }
    if extra_env:
        proc_env.update(extra_env)
    return subprocess.run(
        ["bash", str(env["hook_path"])],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=proc_env,
    )
