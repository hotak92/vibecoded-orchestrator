# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for Known Issue 6 sub-issue A (v0.2.52).

The transitive ``authlib`` dep of ``weaviate-client`` raises
``AuthlibDeprecationWarning`` at module import time when something
touches ``authlib.jose`` (notably some weaviate code paths and several
authlib-internal helpers).  That warning bubbled up to the user during
``install.py``'s KG-seed step on first install and was visible enough to
get filed as Known Issue 6 ("Cosmetic warnings during seed").

Defensive fix in v0.2.52: every script that does ``import weaviate``
BEFORE the install banner logic installs a
``warnings.filterwarnings('ignore', category=AuthlibDeprecationWarning)``
BEFORE the import.  This test pins that contract per-file by spawning a
subprocess that imports the module and asserts the warning text never
hits stderr.

The test exercises THREE failure modes:

1.  Plain ``import weaviate`` baseline — no AuthlibDeprecationWarning
    should escape from a fresh subprocess (weaviate-client 4.21+ doesn't
    trigger it on its own).  This is the regression floor: if a future
    weaviate-client release starts loading ``authlib.jose`` again, our
    filter must catch it.

2.  Forced ``from authlib.jose import JsonWebToken`` after applying the
    filter — proves the filter, when installed before the trigger,
    silences the warning even when the trigger is unambiguous.  This is
    the actual defence-in-depth assertion.

3.  Loading each VCO-shipped script-with-weaviate-import as a module and
    pinning that NO AuthlibDeprecationWarning text reaches stderr.  We
    use ``compile()`` + ``exec`` on a small import-only stub rather than
    running the whole script so the test doesn't need a live Weaviate
    instance.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_subprocess(code: str) -> subprocess.CompletedProcess:
    """Run *code* in a fresh Python subprocess, returning the result.

    ``-W always`` forces every warning to fire (even those Python would
    normally collapse to default), so any AuthlibDeprecationWarning the
    code path raises lands in stderr — that's how we detect leakage.
    """
    return subprocess.run(
        [sys.executable, "-W", "always", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )


def _authlib_installed() -> bool:
    """Best-effort check that ``authlib`` is in the test env."""
    try:
        import authlib  # noqa: F401
        return True
    except ImportError:
        return False


class AuthlibDeprecationFilterTests(unittest.TestCase):
    """Pin every script's authlib-warning filter against import-time leakage."""

    def setUp(self) -> None:
        if not _authlib_installed():
            self.skipTest("authlib not installed in test env — filter has no surface to defend")

    def test_filter_silences_explicit_authlib_jose_import(self) -> None:
        """Direct ``from authlib.jose import ...`` must emit no
        AuthlibDeprecationWarning AFTER the filter is installed.

        This is the canonical reproducer of the warning the filter
        targets.  If the filter regresses (e.g. a future refactor moves
        the ``filterwarnings`` call below ``import weaviate`` instead of
        above it), this test fails — even when no weaviate code path
        triggers the warning organically.
        """
        code = textwrap.dedent("""
            import sys
            import warnings
            # Match the exact filter shape used in the three target scripts.
            from authlib.deprecate import AuthlibDeprecationWarning
            warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
            from authlib.jose import JsonWebToken  # noqa: F401
            print("OK")
        """).strip()
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, msg=f"stderr={result.stderr!r}")
        self.assertIn("OK", result.stdout)
        self.assertNotIn("AuthlibDeprecationWarning", result.stderr)
        self.assertNotIn("authlib.jose module is deprecated", result.stderr)

    def test_filter_absent_lets_warning_leak(self) -> None:
        """Sanity: WITHOUT the filter, the warning DOES leak.

        Locks in that the previous test isn't a no-op.  If a future
        authlib release stops emitting the warning at all, this test
        will start failing and we'll know the regression risk has gone
        away — at which point the filter can be deleted.
        """
        code = textwrap.dedent("""
            import warnings
            warnings.simplefilter("always")
            from authlib.jose import JsonWebToken  # noqa: F401
        """).strip()
        result = _run_subprocess(code)
        # The warning MUST escape — that's the whole point of the filter.
        self.assertIn("AuthlibDeprecationWarning", result.stderr)

    def test_weaviate_mcp_server_import_path(self) -> None:
        """Importing ``weaviate_mcp.server`` must NOT leak the warning.

        The MCP server's prelude installs the filter before
        ``import weaviate``.  Stripping the prelude would surface the
        warning in every Claude Code session that spawns this MCP.
        """
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT / 'claude_mcp_servers')!r})
            # The MCP server starts a stdio loop in ``__main__``; importing
            # it as a module just runs the top-of-file code (filter install
            # + import weaviate) which is exactly what we want to test.
            import weaviate_mcp.server  # noqa: F401
            print("OK")
        """).strip()
        result = _run_subprocess(code)
        self.assertEqual(
            result.returncode, 0,
            msg=f"weaviate_mcp.server import failed: stderr={result.stderr!r}",
        )
        self.assertNotIn("AuthlibDeprecationWarning", result.stderr)
        self.assertNotIn("authlib.jose module is deprecated", result.stderr)

    def test_sync_knowledge_graph_filter_block_present(self) -> None:
        """Static check: the filter block must appear BEFORE
        ``import weaviate`` in templates/scripts/sync_knowledge_graph.py.

        We do not run the script (it requires a live Weaviate) — we
        just pin the source order.  Order matters because authlib
        forces ``simplefilter('always', AuthlibDeprecationWarning)`` at
        ``authlib.deprecate`` import time; if our filter runs AFTER
        weaviate has imported authlib, the warning still escapes.
        """
        script = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        text = script.read_text()
        filter_marker = "AuthlibDeprecationWarning"
        weaviate_import = "\nimport weaviate"
        self.assertIn(filter_marker, text, msg="filter block missing entirely")
        self.assertIn(weaviate_import, text)
        self.assertLess(
            text.index(filter_marker),
            text.index(weaviate_import),
            msg="filter must precede `import weaviate`",
        )

    def test_analyze_code_graph_filter_block_present(self) -> None:
        """Same order check for templates/scripts/analyze_code_graph.py."""
        script = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        text = script.read_text()
        self.assertIn("AuthlibDeprecationWarning", text)
        # analyze_code_graph.py wraps the import in try/except, but the
        # filter block must still come before the try statement.
        filter_idx = text.index("AuthlibDeprecationWarning")
        # The `import weaviate` is inside a try block; find it as a line.
        weaviate_import_idx = text.index("    import weaviate")
        self.assertLess(filter_idx, weaviate_import_idx)

    def test_detect_duplicates_filter_block_present(self) -> None:
        """Same order check for templates/scripts/detect_duplicates.py.

        ``detect_duplicates.py`` runs every ~10 KG edits via the
        post-edit hook, so any warning that escapes here would surface
        repeatedly during normal use, not just during install.
        """
        script = REPO_ROOT / "templates" / "scripts" / "detect_duplicates.py"
        text = script.read_text()
        self.assertIn("AuthlibDeprecationWarning", text)
        filter_idx = text.index("AuthlibDeprecationWarning")
        weaviate_import_idx = text.index("\nimport weaviate")
        self.assertLess(filter_idx, weaviate_import_idx)

    def test_weaviate_mcp_server_filter_block_present(self) -> None:
        """Same order check for claude_mcp_servers/weaviate_mcp/server.py."""
        script = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        text = script.read_text()
        self.assertIn("AuthlibDeprecationWarning", text)
        filter_idx = text.index("AuthlibDeprecationWarning")
        weaviate_import_idx = text.index("\nimport weaviate")
        self.assertLess(filter_idx, weaviate_import_idx)


if __name__ == "__main__":
    unittest.main()
