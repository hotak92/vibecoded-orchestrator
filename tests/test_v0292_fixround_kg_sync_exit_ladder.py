# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""m6 — the kg-sync venv refusal must not collide with the script's ladder.

`sync_knowledge_graph.py` publishes a three-rung contract in its own usage
block: **0** clean, **1** the run happened and some nodes/docs failed, **2**
usage error or refused project root. The `kg-sync` / `kg-sync.ps1` wrappers
sit in front of that script and can refuse to start it at all when no venv
carries VCO's KG dependencies.

Until v0.2.92 that refusal exited **1** — the same rung as "I ran, some
nodes failed". A caller reading only the exit status therefore could not
tell *I never started* from *I finished with failures*, two states whose
correct responses are opposite (repair the install vs. read the named
per-node failures). A check that cannot distinguish "could not determine"
from a real result is not a check.

The refusal now exits **3**. These tests pin that it is (a) what the bash
wrapper actually returns when executed, (b) disjoint from every code the
Python script can emit, and (c) identical in the PowerShell sibling.
"""
from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
KG_SYNC = REPO_ROOT / "templates" / "scripts" / "kg-sync"
KG_SYNC_PS1 = REPO_ROOT / "templates" / "scripts" / "kg-sync.ps1"
SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

#: The rung the wrappers use for "I did not run". Named once, here.
REFUSAL_EXIT_CODE = 3

#: The rung `sync_knowledge_graph.py` uses for "I ran, some nodes failed".
PER_NODE_FAILURE_EXIT_CODE = 1


def _script_exit_codes() -> set[int]:
    """Every integer literal `sync_knowledge_graph.py` can pass to
    ``sys.exit``. Read from the AST rather than a grep so a code inside a
    comment or a docstring cannot inflate the set."""
    tree = ast.parse(SYNC_SCRIPT.read_text(encoding="utf-8"))
    codes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_sys_exit = (
            isinstance(func, ast.Attribute)
            and func.attr == "exit"
            and isinstance(func.value, ast.Name)
            and func.value.id == "sys"
        )
        if not is_sys_exit or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            codes.add(arg.value)
        elif isinstance(arg, ast.IfExp):
            # `sys.exit(0 if total_fail == 0 else 1)`
            for branch in (arg.body, arg.orelse):
                if isinstance(branch, ast.Constant) and isinstance(branch.value, int):
                    codes.add(branch.value)
    return codes


class TestTheRefusalCodeDoesNotCollide(unittest.TestCase):
    def test_refusal_differs_from_the_per_node_failure_code(self):
        """The whole point of the fix, stated as one assertion."""
        self.assertNotEqual(
            REFUSAL_EXIT_CODE,
            PER_NODE_FAILURE_EXIT_CODE,
            "a venv refusal that exits with the per-node-failure code makes "
            "'I did not run' and 'I ran and some nodes failed' the same "
            "answer to a caller",
        )

    def test_refusal_is_disjoint_from_every_code_the_script_emits(self):
        """`1` is not the only rung to avoid — `0` and `2` are taken too."""
        emitted = _script_exit_codes()
        self.assertIn(
            PER_NODE_FAILURE_EXIT_CODE,
            emitted,
            "the premise of this test: the script really does use 1 for "
            "per-node failures",
        )
        self.assertNotIn(
            REFUSAL_EXIT_CODE,
            emitted,
            f"kg-sync's refusal code {REFUSAL_EXIT_CODE} collides with a code "
            f"sync_knowledge_graph.py already emits ({sorted(emitted)}); the "
            f"wrapper and the script share one ladder and must not overlap",
        )

    def test_the_scripts_printed_contract_still_names_1_as_per_node(self):
        """Pins the OTHER half of the ladder so this test notices if the
        script's own documented meaning of `1` moves under us."""
        source = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"Exit codes:\s*0 clean · 1 per-node/per-doc failures · 2 usage",
            "sync_knowledge_graph.py::_print_usage no longer prints the "
            "ladder this wrapper was aligned against",
        )


class TestBothWrappersReturnTheNewCode(unittest.TestCase):
    def setUp(self):
        self.sh = KG_SYNC.read_text(encoding="utf-8")
        self.ps1 = KG_SYNC_PS1.read_text(encoding="utf-8-sig")

    @staticmethod
    def _executed_lines(text: str, comment_prefix: str = "#") -> list[str]:
        """Comments in these wrappers QUOTE the codes they discuss, so a
        whole-file scan matches the prose that explains the fix and reports
        the defect as still present."""
        return [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(comment_prefix)
        ]

    def test_bash_refusal_arm_exits_with_the_refusal_code(self):
        code = self._executed_lines(self.sh)
        self.assertIn(
            f"    exit {REFUSAL_EXIT_CODE}",
            code,
            "the bash refusal arm must exit with the dedicated refusal code",
        )
        self.assertNotIn(
            f"    exit {PER_NODE_FAILURE_EXIT_CODE}",
            code,
            "no executed line in kg-sync may exit with the per-node-failure code",
        )

    def test_ps1_refusal_arm_exits_with_the_refusal_code(self):
        code = self._executed_lines(self.ps1)
        self.assertIn(
            f"    exit {REFUSAL_EXIT_CODE}",
            code,
            "the PowerShell refusal arm must exit with the same code as bash",
        )
        self.assertNotIn(
            f"    exit {PER_NODE_FAILURE_EXIT_CODE}",
            code,
            "no executed line in kg-sync.ps1 may exit with the "
            "per-node-failure code",
        )

    def test_both_headers_document_the_extended_ladder(self):
        """A shipped exit code that is not written down is not a contract."""
        for text, name in ((self.sh, "kg-sync"), (self.ps1, "kg-sync.ps1")):
            for rung in ("0  clean run", "1  the sync RAN", "2  usage error"):
                self.assertIn(rung, text, f"{name}: header ladder missing {rung!r}")
            self.assertRegex(
                text,
                rf"{REFUSAL_EXIT_CODE}\s+the sync DID NOT RUN",
                f"{name}: the header must document the new refusal rung",
            )

    def test_ps1_keeps_its_bom(self):
        """OS-EXEMPT-PARITY: Windows PowerShell needs the UTF-8 BOM, and the
        header edit above rewrote the first bytes of the file."""
        self.assertTrue(KG_SYNC_PS1.read_bytes().startswith(b"\xef\xbb\xbf"))


class TestTheRefusalCodeLive(unittest.TestCase):
    """Executed, not grepped: run the real wrapper with every env channel
    stripped and read the status a caller would actually see."""

    def test_bash_wrapper_refuses_with_the_dedicated_code(self):
        import os

        from tests.common.wrapper_staging import stage_scripts

        with TemporaryDirectory() as td:
            scripts = Path(td) / ".claude" / "scripts"
            # v0.2.94: the wrapper and the `vct_venv_ladder.sh` it sources are
            # ONE shipped unit. Staging only the wrapper stages a BROKEN
            # install, and the wrapper would (correctly) refuse for a
            # different reason than the one under test.
            stage_scripts(scripts, "kg-sync")
            (scripts / "sync_knowledge_graph.py").write_text(
                "raise SystemExit('THE SYNC SCRIPT MUST NOT RUN')\n"
            )
            env = {
                k: v
                for k, v in os.environ.items()
                # v0.2.94 item 2a: VCT_ORCHESTRATOR_ROOT is a resolution
                # channel now — leaving it in lets a maintainer shell resolve a
                # real venv, and this test stops exercising the refusal.
                if k
                not in (
                    "VCT_INSTALL_ROOT",
                    "VCT_VENV",
                    "VCT_ORCHESTRATOR_ROOT",
                    "VIRTUAL_ENV",
                )
            }
            proc = subprocess.run(
                ["bash", str(scripts / "kg-sync"), "--all"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                proc.returncode,
                REFUSAL_EXIT_CODE,
                f"a refusal must be reported as {REFUSAL_EXIT_CODE} (did not "
                f"run), never as {PER_NODE_FAILURE_EXIT_CODE} (ran, some nodes "
                f"failed).\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
            )
            self.assertNotIn(
                "THE SYNC SCRIPT MUST NOT RUN",
                proc.stdout + proc.stderr,
                "the wrapper must refuse BEFORE invoking the sync script",
            )
            self.assertIn("no Python environment", proc.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
