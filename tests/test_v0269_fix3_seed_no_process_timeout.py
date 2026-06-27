# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.69 FIX 3: install.py KG-seed subprocesses carry NO per-process timeout.

install.py used to wrap the whole ``sync_knowledge_graph.py`` subprocess in
per-PROCESS timeouts (``timeout=600`` for the shared-KG seed, ``timeout=900``
for the kg/docs seed). Those fired on legitimate slow re-embeds (a
snowflake-arctic re-embed on a cold CPU can run well past any wall-clock cap).
Per the maintainer ruling there is NO per-process timeout on the seed path;
the only guard lives at chunk granularity inside the Python embed path
(``VCT_EMBED_REQUEST_TIMEOUT_SECS`` in ``EmbeddingService``).

This is a source-structure guard: it walks install.py's AST and asserts that
EVERY ``subprocess.run(...)`` whose first arg invokes ``sync_knowledge_graph.py``
(via the ``sync_kg`` variable) does NOT pass a ``timeout=`` keyword. It would
fail if a future edit re-introduced a per-process cap on the seed.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INSTALL_PY = REPO_ROOT / "install.py"


def _is_subprocess_run(call: ast.Call) -> bool:
    """True if ``call`` is ``subprocess.run(...)``."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _first_arg_references_sync_kg(call: ast.Call) -> bool:
    """True if the call's first positional arg is a list literal whose
    elements include the ``sync_kg`` variable (the path to
    ``sync_knowledge_graph.py``).

    Both seed call sites build the command as
    ``[str(venv_py), str(sync_kg), ...]`` or
    ``[str(venv_py), str(sync_kg)] + cmd_args`` — so we look for a list
    literal anywhere in the first arg's expression tree that contains a
    ``str(sync_kg)`` call or a bare ``sync_kg`` name.
    """
    if not call.args:
        return False
    first = call.args[0]
    for node in ast.walk(first):
        if isinstance(node, ast.Name) and node.id == "sync_kg":
            return True
    return False


class SeedSubprocessNoProcessTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = ast.parse(INSTALL_PY.read_text(encoding="utf-8"))

    def _seed_run_calls(self) -> list[ast.Call]:
        calls: list[ast.Call] = []
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Call)
                and _is_subprocess_run(node)
                and _first_arg_references_sync_kg(node)
            ):
                calls.append(node)
        return calls

    def test_found_both_seed_call_sites(self):
        # Sanity: the AST walk must find exactly the two seed call sites.
        # If this drops to <2, the matcher drifted (e.g. the cmd-building
        # expression changed) and the timeout assertion below would pass
        # vacuously. If it grows, a new seed call site appeared and must
        # also be timeout-free.
        calls = self._seed_run_calls()
        self.assertGreaterEqual(
            len(calls),
            2,
            "expected at least the two sync_knowledge_graph.py seed "
            "subprocess.run call sites in install.py",
        )

    def test_no_seed_call_passes_timeout(self):
        for call in self._seed_run_calls():
            kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
            self.assertNotIn(
                "timeout",
                kwarg_names,
                f"install.py line {call.lineno}: a sync_knowledge_graph.py "
                "seed subprocess.run must NOT pass timeout= (v0.2.69 FIX 3 — "
                "no per-process cap on the seed path; the guard is per-embed-"
                "request in EmbeddingService).",
            )
            # The real-failure handlers must remain (check=True is what makes
            # CalledProcessError fire).
            self.assertIn(
                "check",
                kwarg_names,
                f"install.py line {call.lineno}: seed subprocess.run should "
                "keep check=True so real non-zero exits still raise.",
            )


if __name__ == "__main__":
    unittest.main()
