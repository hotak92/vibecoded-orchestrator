# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CVE-1 regression guard — `--omit=dev` on vendored npm installs.

GHSA-5xrq-8626-4rwp (CRITICAL severity): `vitest < 3.2.6` shipped under
``vco_lib/excalidraw_mcp_fork/``. Non-exploitable for us (vulnerable code
path is `vitest --ui`'s HTTP server which we never invoke; runtime entry
`dist/mcp/index.js` doesn't reference vitest), but defensive hygiene
mandates omitting devDeps from the bundled MCP install path anyway:

  * ~50 MB user-disk-footprint reduction.
  * Silences every user-side `npm audit` of the vendored package.
  * Aligns the file-pin branch's behaviour with npm's tarball convention
    (`npm publish` strips devDeps from the registry-format tarball, so the
    registry-pin branch already gets dev-less installs; file pins should
    too).

Patch lives in ``install.py::_install_pinned_npm`` is_file_pin branch:

    install_argv = [_NPM_PATH, "install", "-g", "--omit=dev", str(local_dir)]
                                              ^^^^^^^^^^^^^ added flag

This test ASSERTS THE PATCH LANDED. Until Track B applies the patch in
v0.2.53 Phase 2, this test fails with a precise diagnostic pointing at
the patch site. After Track B applies the patch, the test passes — and
stays in the suite as a regression guard against future install.py
refactors accidentally dropping the flag.

See `docs/cve-1-patch-spec.md` for the full spec + cross-track handoff.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INSTALL_PY = REPO_ROOT / "install.py"


def _read_install_pinned_npm_source() -> str:
    """Read the source of ``_install_pinned_npm`` from install.py.

    Uses ``ast`` to find the function body and returns the source
    span. We use the AST-anchored approach rather than a regex over
    the full file because install.py is 21k+ lines and the
    ``install_argv = [...]`` literal appears in multiple unrelated
    helpers (registry-vs-file pin branches + version-probe helpers).
    """
    src = INSTALL_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_install_pinned_npm"
        ):
            start = node.lineno - 1
            end = node.end_lineno
            return "\n".join(src.splitlines()[start:end])
    raise AssertionError(
        "Could not find `_install_pinned_npm` function in install.py — "
        "either the function was renamed (update this test) or install.py "
        "has been restructured beyond what this regression guard expected."
    )


class CVE1PatchTests(unittest.TestCase):
    """Assert ``--omit=dev`` is present on the file-pin install branch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fn_src = _read_install_pinned_npm_source()

    def test_file_pin_branch_includes_omit_dev(self) -> None:
        """The is_file_pin branch's install_argv must include ``--omit=dev``.

        We locate the branch by anchoring on ``if is_file_pin:`` followed
        within a small window by ``install_argv = [`` (the registry branch
        is on the other side of the else, and the version-probe branches
        don't construct an install_argv at all). Then we assert
        ``--omit=dev`` is one of the list literal's elements.
        """
        # Locate the file-pin install_argv assignment. There may be MULTIPLE
        # `if is_file_pin:` blocks in the function (the branch is taken
        # several times throughout the audit-logging chain). We want the
        # one that constructs install_argv.
        pattern = re.compile(
            r"if is_file_pin:\s*\n"
            r"\s*install_argv\s*=\s*\[([^\]]+)\]",
            re.MULTILINE,
        )
        matches = pattern.findall(self.fn_src)
        self.assertEqual(
            len(matches),
            1,
            "Expected exactly one `if is_file_pin: install_argv = [...]` "
            "assignment in `_install_pinned_npm`. Found "
            f"{len(matches)} matches. Either install.py has been refactored "
            "or the function has multiple install-argv construction sites "
            "now — update this test to anchor on the right one.",
        )

        argv_list_src = matches[0]
        # Normalise whitespace and quoting variations for the substring match.
        argv_normalised = re.sub(r"\s+", " ", argv_list_src)
        self.assertIn(
            "--omit=dev",
            argv_normalised,
            "CVE-1 patch MISSING: "
            "`install.py::_install_pinned_npm` is_file_pin branch does NOT "
            "include `--omit=dev` in its install_argv. Per "
            "`docs/cve-1-patch-spec.md`, Track B was supposed to apply "
            "the patch in v0.2.53 Phase 2 splice. If this test fails on "
            "Track E's worktree (chore/v0253-track-e), that's EXPECTED — "
            "Track E ships the spec + this test only. If it fails on the "
            "integration branch or on main, Track B's patch is missing "
            "or was applied to the wrong branch.\n"
            f"\nObserved install_argv list literal:\n  [{argv_list_src}]\n"
            "\nExpected (4 elements, in this order):\n"
            "  [_NPM_PATH, \"install\", \"-g\", \"--omit=dev\", str(local_dir)]"
        )

    def test_registry_pin_branch_does_NOT_include_omit_dev(self) -> None:
        """Defense: don't accidentally add ``--omit=dev`` to the registry branch.

        Registry tarballs already exclude devDeps by `npm publish` convention,
        so adding ``--omit=dev`` to the registry branch is redundant (harmless
        but conveys a false invariant about how registry installs behave).
        If a future refactor copy-pastes the file-pin shape into the registry
        branch, this test fires.
        """
        # Locate the registry branch — anchored on the `else:` after the
        # is_file_pin install_argv assignment.
        pattern = re.compile(
            r"if is_file_pin:\s*\n"
            r"\s*install_argv\s*=\s*\[[^\]]+\]\s*\n"
            r"\s*else:\s*\n"
            r"\s*install_argv\s*=\s*\[([^\]]+)\]",
            re.MULTILINE,
        )
        m = pattern.search(self.fn_src)
        self.assertIsNotNone(
            m,
            "Could not locate the if-is_file_pin / else install_argv "
            "branches. install.py may have been refactored beyond what "
            "this regression guard understands.",
        )
        assert m is not None  # narrow for mypy
        registry_argv_src = m.group(1)
        registry_normalised = re.sub(r"\s+", " ", registry_argv_src)
        self.assertNotIn(
            "--omit=dev",
            registry_normalised,
            "Registry-pin install branch unexpectedly includes "
            "`--omit=dev`. Registry tarballs already exclude devDeps via "
            "`npm publish` convention — the flag is redundant on this "
            "branch. Likely a copy-paste from the file-pin branch fix; "
            "remove from the registry branch to keep the invariant clean."
            f"\n\nObserved registry install_argv:\n  [{registry_argv_src}]"
        )


if __name__ == "__main__":
    unittest.main()
