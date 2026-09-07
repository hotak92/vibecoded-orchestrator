# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""m19 — a ratchet on `tests/common/child_env.py`, the convention 47 files
adopted and the rest of the suite silently kept bypassing.

`child_env()` exists because a test that spawns a child Python inherits
whatever ``sys.path`` the CHILD computes, not pytest's. On a machine whose
venv holds a non-editable COPY of ``vco_lib`` in ``site-packages`` — the
documented shadow on this repo's dev box — the child imports the STALE copy
and the test measures code that is not in the tree: green in one venv, red
in another, and neither result about the checkout.

**This is not a correctness fix.** The bypassing call-sites that were
sampled are benign (they spawn shell wrappers, or deliberately exercise the
UNPINNED case). It is a ratchet: the convention had no enforcement at all,
so it could only erode. Every bypassing site as of v0.2.92 is allow-listed
BY PATH WITH ITS COUNT below; a new one — in a new file, or an extra one in
an already-listed file — turns this red.

Fixing an allow-listed site is always safe: the recorded count is a
CEILING, so removals never go red. Tightening the numbers afterwards is
optional housekeeping, not a requirement.

To make a new spawn conform::

    from tests.common.child_env import child_env
    subprocess.run([sys.executable, "-m", "vco_lib.doctor"], env=child_env())

or take the ``child_env`` pytest fixture (registered in ``tests/conftest.py``)
and pass it as ``env=``. If a site must deliberately run UNPINNED, say so at
the call site and add it to `_ALLOWLIST` in the same commit — the point of
the ratchet is that the bypass becomes a decision someone writes down.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

#: `subprocess` entry points that actually start a process.
_SPAWN_FUNCS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)

#: Every file that spawns a child `sys.executable` WITHOUT `child_env`, and
#: how many such call-sites it had when this ratchet was installed
#: (v0.2.92). The count is a CEILING: fixing sites is always green, adding
#: one is red. Do not raise a number to silence a failure — either route the
#: new spawn through `child_env()` or record, at the call site, why it must
#: run unpinned.
_ALLOWLIST: dict[str, int] = {
    # DELIBERATELY UNPINNED (v0.2.92 BLOCKER-1): the child simulates the
    # shipped container image, whose whole point is that `vco_lib` is NOT
    # importable. `child_env()` would pin the checkout onto the child's path
    # and the test would stop measuring the thing it exists to measure.
    "tests/test_v0292_code_embed_image_rebuild.py": 1,
    "tests/test_codegraph_cli_readpath_v0270.py": 2,
    "tests/test_codegraph_naming.py": 1,
    "tests/test_detached_children_no_resourcewarning.py": 1,
    "tests/test_install_hooks.py": 1,
    "tests/test_install_self_materializes_claude_dir.py": 2,
    "tests/test_kg_resolution_context_and_scope_v0292.py": 1,
    # Both spawn the SAME deliberate unpinned qualification probe
    # (`import weaviate` on the host interpreter): the shipped wrapper asks
    # that question without our PYTHONPATH, so pinning it would answer a
    # different one and green-light a host the wrapper would refuse. Reason
    # recorded at both call sites (v0.2.92 delivery-audit m3/m8).
    "tests/test_v0292_kg_duplicates_ps1.py": 1,
    "tests/test_v0292_kg_sync_wrapper_stream_parity.py": 1,
    "tests/test_macos_linux_zip_download_filter.py": 1,
    "tests/test_model_router_cli.py": 1,
    "tests/test_model_router_packaging.py": 6,
    "tests/test_pr2_templates_portability.py": 2,
    "tests/test_pre_diagram_path_validation.py": 1,
    "tests/test_v0249_install_singleton_lock.py": 1,
    "tests/test_v0254_secrets_surface.py": 2,
    "tests/test_v0284_json_stdout_contract.py": 1,
    "tests/test_v0285_bundle_seams.py": 4,
    "tests/test_v0285_install_parity.py": 1,
    "tests/test_v0291_npx_resolver.py": 1,
    "tests/test_v0292_bundle_staleness.py": 5,
    "tests/test_v0292_install_editable_transition.py": 1,
    "tests/test_v0292_kg_dedup.py": 1,
    "tests/test_v0292_n35_rewire_transform.py": 1,
    "tests/test_v0292_regclean_rl_setup_state_root.py": 1,
    "tests/test_v0292_wp17_move_delivery.py": 4,
    "tests/test_v0292_wp18_rename_delivery.py": 1,
    "tests/test_v0292_wp8_cost_summary_reader.py": 1,
    "tests/test_v0292_wp8_metrics_migration.py": 1,
    "tests/test_v0292_wp8_metrics_shell_parity.py": 1,
    "tests/test_v52_l2_subagent_hooks.py": 1,
    "tests/test_wheel_install.py": 3,
}


# ───────────────────────────────────────────────────────────────────────────
# The scanner
# ───────────────────────────────────────────────────────────────────────────


def _is_sys_executable(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "executable"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _argv_starts_with_sys_executable(node: ast.AST, argv_vars: set[str]) -> bool:
    """True when `node` is an argv that begins with `sys.executable`.

    Two shapes count: the literal ``[sys.executable, ...]`` handed straight
    to `subprocess.run`, and a NAME previously bound to such a literal
    (``cmd = [sys.executable, ...]; subprocess.run(cmd, ...)``). Missing the
    second shape would let the convention be bypassed by adding one line.
    """
    if isinstance(node, (ast.List, ast.Tuple)):
        return bool(node.elts) and _is_sys_executable(node.elts[0])
    if isinstance(node, ast.Name):
        return node.id in argv_vars
    if _is_sys_executable(node):
        return True  # string-command form: subprocess.run(sys.executable, ...)
    return False


def _argv_variables(tree: ast.AST) -> set[str]:
    """Names bound anywhere in the module to a `[sys.executable, ...]` list."""
    names: set[str] = set()
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        if not (value.elts and _is_sys_executable(value.elts[0])):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def _is_spawn_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_FUNCS:
        return True
    # bare `run(...)` after `from subprocess import run`
    return isinstance(func, ast.Name) and func.id in _SPAWN_FUNCS


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing_scope(node: ast.AST, parents: dict[int, ast.AST], root: ast.AST) -> ast.AST:
    cur = node
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return root


def _mentions_child_env(scope: ast.AST) -> bool:
    """`child_env` referenced anywhere in the enclosing function (or module).

    Scope-wide rather than call-site-only on purpose: the equally correct
    ``env = child_env(); subprocess.run(..., env=env)`` shape would otherwise
    be flagged, and a lint that punishes conforming code teaches people to
    work around it. `_child_env`-style local look-alikes do NOT count — only
    the exact identifier.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and node.id == "child_env":
            return True
        if isinstance(node, ast.arg) and node.arg == "child_env":
            return True  # the pytest fixture, taken as a parameter
        if isinstance(node, ast.keyword) and node.arg == "child_env":
            return True
    return False


def scan_file(path: Path) -> int:
    """Count of `sys.executable` spawns in `path` that bypass `child_env`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover — not our job
        return 0
    argv_vars = _argv_variables(tree)
    parents = _parent_map(tree)
    offenders = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawn_call(node) or not node.args:
            continue
        if not _argv_starts_with_sys_executable(node.args[0], argv_vars):
            continue
        has_env = any(kw.arg == "env" for kw in node.keywords)
        scope = _enclosing_scope(node, parents, tree)
        if has_env and _mentions_child_env(scope):
            continue
        offenders += 1
    return offenders


def scan_tree() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        count = scan_file(path)
        if count:
            found[path.relative_to(REPO_ROOT).as_posix()] = count
    return found


def _regenerate_allowlist_snippet() -> str:  # pragma: no cover — dev aid
    """Print a paste-ready `_ALLOWLIST` body. Used when installing the
    ratchet and when a deliberate bypass is genuinely added."""
    return "\n".join(f'    "{p}": {n},' for p, n in sorted(scan_tree().items()))


# ───────────────────────────────────────────────────────────────────────────


class TestChildEnvRatchet(unittest.TestCase):
    def test_no_new_sys_executable_spawn_bypasses_child_env(self):
        current = scan_tree()

        new_files = sorted(set(current) - set(_ALLOWLIST))
        self.assertEqual(
            new_files,
            [],
            "these test files spawn a child `sys.executable` without "
            "`child_env()`:\n  "
            + "\n  ".join(f"{p} ({current[p]} site(s))" for p in new_files)
            + "\n\nUse `from tests.common.child_env import child_env` and pass "
            "`env=child_env()`; without it the child may import a stale "
            "`vco_lib` from site-packages and the test measures code that is "
            "not in this checkout. If the site must run UNPINNED on purpose, "
            "say so at the call site and add the file to `_ALLOWLIST` in "
            "tests/test_v0292_fixround_child_env_lint.py in the same commit.",
        )

        grew = {
            p: (current[p], _ALLOWLIST[p])
            for p in sorted(set(current) & set(_ALLOWLIST))
            if current[p] > _ALLOWLIST[p]
        }
        self.assertEqual(
            grew,
            {},
            "these already-known files GAINED `sys.executable` spawns that "
            "bypass `child_env()` (path: now vs. allowed):\n  "
            + "\n  ".join(f"{p}: {now} > {was}" for p, (now, was) in grew.items())
            + "\n\nThe allowlist counts are a ceiling for pre-existing debt, "
            "not a budget for new debt. Route the new spawn through "
            "`child_env()`.",
        )

    def test_the_allowlist_is_not_a_blanket(self):
        """A ratchet that lists every file is not a ratchet.

        The convention IS adopted by a real share of the suite; if that share
        collapsed, this lint would be measuring nothing.
        """
        spawning_files = {
            p.relative_to(REPO_ROOT).as_posix()
            for p in TESTS_DIR.rglob("*.py")
            if "sys.executable" in p.read_text(encoding="utf-8", errors="ignore")
        }
        self.assertTrue(spawning_files, "no test spawns a child python at all?")
        self.assertLess(
            len(_ALLOWLIST),
            len(spawning_files),
            "every file that touches sys.executable is allow-listed — the "
            "ratchet has nothing left to hold",
        )

    def test_the_helper_the_lint_points_at_exists(self):
        """A lint whose remediation names a missing module is worse than none."""
        helper = REPO_ROOT / "tests" / "common" / "child_env.py"
        self.assertTrue(helper.is_file(), f"{helper} — the remedy this lint prescribes")
        self.assertIn("def child_env(", helper.read_text(encoding="utf-8"))

    def test_allowlist_entries_still_exist(self):
        """A stale path silently weakens the ratchet: a renamed file returns
        as 'new' anyway, but a DELETED one leaves a permanent free pass."""
        missing = [p for p in _ALLOWLIST if not (REPO_ROOT / p).is_file()]
        self.assertEqual(
            missing,
            [],
            "allow-listed files that no longer exist — drop them from "
            "`_ALLOWLIST`:\n  " + "\n  ".join(missing),
        )


class TestTheScannerItself(unittest.TestCase):
    """The lint's own detection rules, pinned on synthetic sources so a
    silent scanner regression cannot make the ratchet vacuously green."""

    def _count(self, source: str) -> int:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "t.py"
            f.write_text(source, encoding="utf-8")
            return scan_file(f)

    def test_bare_spawn_is_flagged(self):
        self.assertEqual(
            self._count("import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'x'])\n"),
            1,
        )

    def test_inline_child_env_conforms(self):
        self.assertEqual(
            self._count(
                "import subprocess, sys\n"
                "from tests.common.child_env import child_env\n"
                "def t():\n"
                "    subprocess.run([sys.executable, '-c', 'x'], env=child_env())\n"
            ),
            0,
        )

    def test_local_variable_form_conforms(self):
        self.assertEqual(
            self._count(
                "import subprocess, sys\n"
                "from tests.common.child_env import child_env\n"
                "def t():\n"
                "    env = child_env(FOO='1')\n"
                "    subprocess.run([sys.executable, '-c', 'x'], env=env)\n"
            ),
            0,
        )

    def test_fixture_parameter_form_conforms(self):
        self.assertEqual(
            self._count(
                "import subprocess, sys\n"
                "def test_x(child_env):\n"
                "    subprocess.run([sys.executable, '-c', 'x'], env=child_env)\n"
            ),
            0,
        )

    def test_env_without_child_env_is_flagged(self):
        """Passing SOME env is not the convention — a hand-built env is
        exactly the case that loses the PYTHONPATH pin."""
        self.assertEqual(
            self._count(
                "import os, subprocess, sys\n"
                "def t():\n"
                "    subprocess.run([sys.executable, '-c', 'x'], env=dict(os.environ))\n"
            ),
            1,
        )

    def test_lookalike_helper_does_not_count_as_conforming(self):
        self.assertEqual(
            self._count(
                "import subprocess, sys\n"
                "def _child_env():\n"
                "    return {}\n"
                "def t():\n"
                "    subprocess.run([sys.executable, '-c', 'x'], env=_child_env())\n"
            ),
            1,
        )

    def test_argv_bound_to_a_variable_is_still_seen(self):
        self.assertEqual(
            self._count(
                "import subprocess, sys\n"
                "def t():\n"
                "    cmd = [sys.executable, '-c', 'x']\n"
                "    subprocess.run(cmd)\n"
            ),
            1,
        )

    def test_a_non_python_child_is_not_our_business(self):
        self.assertEqual(
            self._count("import subprocess\nsubprocess.run(['bash', 'x.sh'])\n"), 0
        )

    def test_sys_executable_that_is_not_a_spawn_is_ignored(self):
        self.assertEqual(
            self._count("import sys\nprint(sys.executable)\nPY = sys.executable\n"), 0
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestChildEnvPinsTheOrchestratorRoot(unittest.TestCase):
    """`PYTHONPATH` alone does not pin the checkout.

    Shipped scripts resolve their `vco_lib` parent from
    `$VCT_ORCHESTRATOR_ROOT` and insert it at `sys.path[0]` — ahead of
    `PYTHONPATH`. A child inheriting the developer's value imports a different
    checkout regardless of what `child_env` puts on `PYTHONPATH`, which is how
    an audit in this cycle came to measure a stale chunk budget from the
    dogfood fork and nearly file it as a defect here.
    """

    def test_orchestrator_root_is_pinned_to_this_checkout(self) -> None:
        from tests.common.child_env import REPO_ROOT, child_env

        env = child_env({"VCT_ORCHESTRATOR_ROOT": "/somewhere/else"})
        self.assertEqual(env["VCT_ORCHESTRATOR_ROOT"], str(REPO_ROOT))

    def test_an_explicit_override_still_wins(self) -> None:
        """Opting out must remain possible — but written down at the call site."""
        from tests.common.child_env import child_env

        env = child_env(VCT_ORCHESTRATOR_ROOT="/deliberately/elsewhere")
        self.assertEqual(env["VCT_ORCHESTRATOR_ROOT"], "/deliberately/elsewhere")

    def test_the_child_actually_resolves_this_checkout(self) -> None:
        """Behavioural: a child asking where `vco_lib` came from must answer
        with THIS tree, even when the ambient variable names another."""
        import subprocess
        import sys as _sys

        from tests.common.child_env import REPO_ROOT, child_env

        proc = subprocess.run(
            [_sys.executable, "-c",
             "import vco_lib, pathlib, sys;"
             "print(pathlib.Path(vco_lib.__file__).resolve().parent.parent)"],
            env=child_env({"VCT_ORCHESTRATOR_ROOT": "/somewhere/else"}),
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(REPO_ROOT))
