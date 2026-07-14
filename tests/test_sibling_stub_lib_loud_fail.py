# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.81 — sibling MCP servers LOUD-FAIL on a missing shipped ``_lib``.

The search-mcp and code-embedding-service servers used to end their
``_lib`` import dance with a SILENT soft-fail stub:

  * ``search_mcp/server.py``:
      - ``register_sighup_exit_handler`` fell back to ``lambda: False``
        (silently disabled SIGHUP env-reload).
      - ``exit_if_update_in_progress`` fell back to ``None`` (silently
        disabled the update-in-progress fork-bomb guard).
  * ``code_embedding_service/server.py``:
      - ``exit_if_update_in_progress`` fell back to ``None`` (silently
        disabled the mid-update GPU-load guard).

``_lib`` is a SHIPPED component of every healthy install
(``claude_mcp_servers/_lib/__init__.py`` + ``sighup_handler.py`` +
``update_gate.py``), so a missing ``_lib`` means a BROKEN install — the
silent stubs MASKED that, and in the update-gate case re-armed the very
fork-bomb the gate exists to break. The fix routes both imports through the
shared ``_lib.bootstrap.import_lib_member`` helper, which LOUD-FAILS
(``ImportError`` with a ``python install.py --update`` remediation) when the
shipped module can't be imported.

Tests here are two-pronged:

  T-SRC: source-text guards that the masking stubs are gone and the
         shared helper is used (cheap, no subprocess).
  T-BEHAVIOR: exercise ``import_lib_member`` directly — success path
         returns the real member; a missing module raises a LOUD
         ``ImportError`` naming the remediation. This is the behavioural
         contract the stubs violated.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


class BootstrapHelperBehaviorTests(unittest.TestCase):
    """T-BEHAVIOR — ``_lib.bootstrap.import_lib_member`` contract."""

    def _helper(self):
        from _lib.bootstrap import import_lib_member

        return import_lib_member

    def test_success_returns_real_member(self) -> None:
        """Healthy path: the real shipped member is returned (not a stub)."""
        import_lib_member = self._helper()
        fn = import_lib_member("sighup_handler", "register_sighup_exit_handler")
        self.assertTrue(callable(fn))
        # It's the genuine shipped function, not a soft-fail lambda.
        self.assertEqual(fn.__name__, "register_sighup_exit_handler")

        gate = import_lib_member("update_gate", "exit_if_update_in_progress")
        self.assertTrue(callable(gate))
        self.assertEqual(gate.__name__, "exit_if_update_in_progress")

    def test_missing_module_raises_loud_importerror(self) -> None:
        """A missing shipped ``_lib`` module raises a LOUD ImportError that
        names the remediation — never a silent None/False stub."""
        import_lib_member = self._helper()
        with self.assertRaises(ImportError) as ctx:
            import_lib_member("this_module_does_not_ship", "whatever")
        msg = str(ctx.exception)
        self.assertIn("BROKEN", msg)
        self.assertIn("install.py --update", msg)

    def test_missing_member_raises_loud_importerror(self) -> None:
        """A present module missing the requested member also LOUD-FAILS."""
        import_lib_member = self._helper()
        with self.assertRaises(ImportError) as ctx:
            import_lib_member("update_gate", "no_such_attribute")
        self.assertIn("install.py --update", str(ctx.exception))


class SiblingSourceGuardTests(unittest.TestCase):
    """T-SRC — the masking stubs are gone and the shared helper is used."""

    SEARCH_MCP = "claude_mcp_servers/search_mcp/server.py"
    CODE_EMBED = "claude_mcp_servers/code_embedding_service/server.py"

    def _src(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    @staticmethod
    def _code_lines(src: str) -> str:
        """Return only non-comment CODE lines.

        The fix's rationale comments intentionally NAME the removed stubs
        (``exit_if_update_in_progress = None``) so a future reader knows
        what was masked. A naive substring scan would match those
        comments; scope the assertions to real code by stripping
        ``#``-comment lines first.
        """
        out = []
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Drop trailing inline comments too (crude but sufficient: no
            # ``#`` appears inside the code tokens we assert against).
            if "#" in line:
                line = line[: line.index("#")]
            out.append(line)
        return "\n".join(out)

    def test_search_mcp_no_sighup_false_stub(self) -> None:
        code = self._code_lines(self._src(self.SEARCH_MCP))
        # The pre-fix stub returned False from a redefined handler.
        self.assertNotIn(
            "def register_sighup_exit_handler(_logger):",
            code,
            "search_mcp must not redefine register_sighup_exit_handler as a "
            "soft-fail stub — route through import_lib_member (loud-fail)",
        )

    def test_search_mcp_no_update_gate_none_stub(self) -> None:
        code = self._code_lines(self._src(self.SEARCH_MCP))
        self.assertNotIn(
            "exit_if_update_in_progress = None",
            code,
            "search_mcp must not set exit_if_update_in_progress = None "
            "(silently disables the fork-bomb guard)",
        )
        # And the None-guard branch must be gone too.
        self.assertNotIn(
            "if exit_if_update_in_progress is not None:",
            code,
            "search_mcp must call the gate unconditionally (it loud-fails on "
            "import now, so the None-guard is dead)",
        )

    def test_code_embed_no_update_gate_none_stub(self) -> None:
        code = self._code_lines(self._src(self.CODE_EMBED))
        self.assertNotIn(
            "exit_if_update_in_progress = None",
            code,
            "code_embedding_service must not set exit_if_update_in_progress "
            "= None (silently disables the mid-update GPU-load guard)",
        )
        self.assertNotIn(
            "if exit_if_update_in_progress is not None:",
            code,
            "code_embedding_service must call the gate unconditionally",
        )

    def test_both_siblings_use_shared_helper(self) -> None:
        for rel in (self.SEARCH_MCP, self.CODE_EMBED):
            with self.subTest(script=rel):
                src = self._src(rel)
                self.assertIn(
                    "from _lib.bootstrap import import_lib_member",
                    src,
                    f"{rel}: must import the shared _lib.bootstrap helper",
                )
                self.assertIn(
                    "import_lib_member(",
                    src,
                    f"{rel}: must call import_lib_member for its _lib imports",
                )


if __name__ == "__main__":
    unittest.main()
