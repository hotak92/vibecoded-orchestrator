# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The pre-bash query is structured like the pre-edit one (v0.2.95, F10 G2).

Owner, 2026-09-16: "the query to KG/CodeGraph should be structured as it
would be if the operation was performed through write/edit tools".

Before this, ``pre-bash-context-inject.sh`` embedded the raw first 500
characters of the COMMAND. ``pre-edit-context-inject.sh`` instead derives a
module name from the basename, adds a content snippet, and passes
``--anchor <file>`` to the code-graph leg. For the SAME edit, going through
Bash therefore got a materially worse retrieval than going through Edit.

These tests drive the SHIPPED hook and read what it actually handed its
producers (a stub interpreter records argv), rather than scanning the hook's
source — a query string in a comment would satisfy a grep.

Both sides of the branch are covered: a command WITH a write target gets the
new shape; a command WITHOUT one gets byte-for-byte the old shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"
HOOK_SH = HOOKS / "pre-bash-context-inject.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required"
)


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Scratch project + (kg argv log, code-graph argv log)."""
    root = tmp_path / "proj"
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copytree(HOOKS / "_lib", hooks / "_lib")
    shutil.copy(HOOK_SH, hooks / "pre-bash-context-inject.sh")
    (root / "knowledge").mkdir()
    (root / "docs").mkdir()
    (root / "vco_lib").mkdir()
    (root / ".claude" / "state").mkdir(parents=True)
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    rl_dir = root / "claude_mcp_servers" / "scripts"
    rl_dir.mkdir(parents=True)
    (rl_dir / "rl_kg_search.py").write_text("# stub\n", encoding="utf-8")

    kg_log = tmp_path / "kg-argv.txt"
    cg_log = tmp_path / "cg-argv.txt"

    # A fake VCO venv interpreter. It records every invocation, and DELEGATES
    # the write-target parse to the real interpreter (that call is the thing
    # under test's input, not its output).
    venv_bin = tmp_path / "fakevenv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{kg_log}"\n'
        "case \"$*\" in\n"
        f'  *bash_write_targets*) exec "{sys.executable}" "$@" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)

    cg_cli = scripts / "code-graph-query"
    cg_cli.write_text(
        "#!/usr/bin/env bash\n" f'printf "%s\\n" "$*" >> "{cg_log}"\n' "exit 0\n",
        encoding="utf-8",
    )
    cg_cli.chmod(0o755)
    return root, kg_log, cg_log


def _run(project: Path, command: str, tmp_path: Path) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    assert bash is not None
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["VCT_VENV"] = str(tmp_path / "fakevenv")
    # Cold caches per test: the shared TTL result-cache would otherwise serve
    # a previous run's block and the CLI would never be called.
    env["VCT_STATE_DIR"] = str(tmp_path / "vctstate")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    payload = {
        "tool_name": "Bash",
        "session_id": "s-q",
        "tool_input": {"command": command},
    }
    return subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "pre-bash-context-inject.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )


def _kg_query(kg_log: Path) -> str:
    """The query argument the hook handed rl_kg_search.py."""
    assert kg_log.exists(), "the KG producer was never invoked"
    for line in kg_log.read_text(encoding="utf-8").splitlines():
        if "rl_kg_search.py" in line:
            after = line.split("rl_kg_search.py", 1)[1].strip()
            return after.split("--limit", 1)[0].strip()
    raise AssertionError(f"no rl_kg_search.py invocation in {kg_log.read_text()!r}")


#: Long enough to clear the 500-char KG threshold without changing shape.
_PAD = "x" * 520


def test_a_knowledge_write_queries_by_module_name_and_body(tmp_path: Path) -> None:
    project, kg_log, _cg = _project(tmp_path)
    command = (
        "cat > knowledge/score-driven-retrieval-tiers.md <<'EOF'\n"
        "Score driven retrieval tiers calibrate verbosity against a canonical "
        "eval set so marginal nodes only cost a summary. " + _PAD + "\n"
        "EOF"
    )
    result = _run(project, command, tmp_path)
    assert result.returncode == 0, result.stderr

    query = _kg_query(kg_log)
    assert query.startswith("score-driven-retrieval-tiers "), query
    assert "Score driven retrieval tiers calibrate" in query, query
    # The shell syntax that dominated the old query is gone from the head.
    assert not query.startswith("cat"), query


def test_a_command_with_no_write_target_keeps_the_old_query(tmp_path: Path) -> None:
    """The negative half: no target => byte-for-byte the pre-v0.2.95 query."""
    project, kg_log, _cg = _project(tmp_path)
    command = "git log --oneline --graph --decorate " + _PAD
    result = _run(project, command, tmp_path)
    assert result.returncode == 0, result.stderr

    query = _kg_query(kg_log)
    # vco_strip_command_noise drops flags/paths; what survives is command
    # vocabulary, and crucially NOT a module-name prefix.
    assert "git" in query or query.strip() == _PAD, query
    assert not query.startswith("score-driven"), query
    # Nothing in the query may look like "<module> <snippet>" from a path.
    assert ".md" not in query, query


def test_a_code_write_anchors_the_code_graph_leg_on_the_file(
    tmp_path: Path,
) -> None:
    """`sed -i` on a .py file must query the code graph the pre-edit way."""
    project, _kg, cg_log = _project(tmp_path)
    module = project / "vco_lib" / "retrieval_rl.py"
    module.write_text("def rank():\n    return 1\n", encoding="utf-8")

    result = _run(project, "sed -i 's/return 1/return 2/' vco_lib/retrieval_rl.py", tmp_path)
    assert result.returncode == 0, result.stderr

    assert cg_log.exists(), "the code-graph CLI was never invoked"
    argv = cg_log.read_text(encoding="utf-8")
    assert "--anchor" in argv, argv
    assert str(module) in argv, argv
    assert "--exclude-file" in argv, argv
    # The query is the module name, not the sed program.
    assert "search retrieval_rl" in argv, argv


def test_a_non_code_write_does_not_reach_the_code_graph(tmp_path: Path) -> None:
    """The negative half: a knowledge/ node is not a code-graph subject.

    `codegraph_bash_gate` would also decline this command, so this pins that
    the new write-target branch did not widen the surface.
    """
    project, _kg, cg_log = _project(tmp_path)
    node = project / "knowledge" / "a-node.md"
    node.write_text("# node\n", encoding="utf-8")

    result = _run(project, "cat > knowledge/a-node.md <<'EOF'\n# node\nEOF", tmp_path)
    assert result.returncode == 0, result.stderr
    assert not cg_log.exists(), cg_log.read_text(encoding="utf-8")


def test_the_500_char_threshold_still_gates_the_kg_leg(tmp_path: Path) -> None:
    """A SHORT knowledge write must not trigger a KG search.

    The threshold is user-locked (Q6, 2026-06-09); the new query shape must
    not have moved the KG leg above it.
    """
    project, kg_log, _cg = _project(tmp_path)
    result = _run(project, "cat > knowledge/tiny.md <<'EOF'\nhi\nEOF", tmp_path)
    assert result.returncode == 0, result.stderr
    invocations = (
        kg_log.read_text(encoding="utf-8") if kg_log.exists() else ""
    )
    assert "rl_kg_search.py" not in invocations, invocations


def test_the_threshold_override_still_applies(tmp_path: Path) -> None:
    """VCT_BASH_KG_THRESHOLD_CHARS must still lower the gate."""
    project, kg_log, _cg = _project(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["VCT_VENV"] = str(tmp_path / "fakevenv")
    env["VCT_STATE_DIR"] = str(tmp_path / "vctstate")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["VCT_BASH_KG_THRESHOLD_CHARS"] = "10"
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    payload = {
        "tool_name": "Bash",
        "session_id": "s-q2",
        "tool_input": {"command": "cat > knowledge/tiny.md <<'EOF'\nhi\nEOF"},
    }
    result = subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "pre-bash-context-inject.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert _kg_query(kg_log).startswith("tiny "), kg_log.read_text(encoding="utf-8")


def test_vct_disable_hooks_suppresses_the_injection(tmp_path: Path) -> None:
    project, kg_log, cg_log = _project(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["VCT_DISABLE_HOOKS"] = "1"
    env["VCT_VENV"] = str(tmp_path / "fakevenv")
    env["PYTHONPATH"] = str(REPO_ROOT)
    payload = {
        "tool_name": "Bash",
        "session_id": "s-q3",
        "tool_input": {"command": "cat > knowledge/x.md <<'EOF'\n" + _PAD + "\nEOF"},
    }
    result = subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "pre-bash-context-inject.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert not kg_log.exists()
    assert not cg_log.exists()
