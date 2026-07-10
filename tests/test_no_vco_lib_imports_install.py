# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Guard: no module under ``vco_lib/`` imports the top-level ``install``
script.

``install.py`` is the top-level installer entry point; it depends ON
``vco_lib`` (import direction: install → vco_lib). A vco_lib module reaching
back UP to ``import install`` / ``from install import ...`` is a back-edge —
it makes vco_lib un-importable in any context where install.py isn't on the
path (the launcher, MCP subprocesses, standalone CLIs), and it inverts the
dependency layering.

v0.2.77 7a-bis removed the last such back-edge (``vco_lib.cli.verify``
imported ``install._install_pinned_npm``; the core moved to
``vco_lib.install_npm`` and verify.py now imports THAT). This test locks the
invariant so a future edit can't silently re-introduce one.

AST-based (not a text grep) so string literals / comments that merely mention
``install`` don't trip it — only real ``import install`` / ``from install
import ...`` statements are flagged. Names like ``install_npm``,
``install_weaviate``, ``installer`` are NOT the top-level ``install`` module
and are correctly ignored (the AST module name is compared exactly).
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VCO_LIB = REPO_ROOT / "vco_lib"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _imports_install(tree: ast.AST) -> list[str]:
    """Return a list of human-readable descriptions of any import of the
    top-level ``install`` module found in *tree*."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import install` or `import install as x`. `import
                # install_npm` has alias.name == "install_npm" (exact
                # match required, so it's excluded).
                if alias.name == "install" or alias.name.startswith("install."):
                    hits.append(f"import {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            # `from install import X`. Relative imports (level > 0) can never
            # target the top-level install script, so skip them.
            if node.level == 0 and (
                node.module == "install" or (node.module or "").startswith("install.")
            ):
                names = ", ".join(a.name for a in node.names)
                hits.append(
                    f"from {node.module} import {names} (line {node.lineno})"
                )
    return hits


class NoVcoLibImportsInstallTests(unittest.TestCase):
    def test_no_module_under_vco_lib_imports_install(self) -> None:
        offenders: dict[str, list[str]] = {}
        for py in sorted(VCO_LIB.rglob("*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            except SyntaxError as e:  # pragma: no cover — parse failure is its own bug
                self.fail(f"failed to parse {py}: {e}")
            hits = _imports_install(tree)
            if hits:
                offenders[str(py.relative_to(REPO_ROOT))] = hits

        self.assertEqual(
            offenders,
            {},
            "vco_lib modules must not import the top-level `install` script "
            "(back-edge — inverts install → vco_lib layering). Offenders:\n"
            + "\n".join(
                f"  {path}: {'; '.join(hits)}"
                for path, hits in offenders.items()
            ),
        )

    def test_guard_detects_a_planted_import(self) -> None:
        """Sanity: the AST scanner actually flags a real `import install`."""
        planted = ast.parse("import install\nfrom install import _x\n")
        self.assertEqual(len(_imports_install(planted)), 2)
        # And does NOT flag lookalikes.
        clean = ast.parse(
            "import install_npm\nfrom vco_lib import install_weaviate\n"
            "x = 'from install import y'  # string, not an import\n"
        )
        self.assertEqual(_imports_install(clean), [])


if __name__ == "__main__":
    unittest.main()
