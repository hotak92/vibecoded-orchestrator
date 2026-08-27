# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the Mermaid wrapper MCP.

Mocks ``shutil.which`` + the bundled-versions manifest so the tests
don't depend on a real npm install or the live pin value. Coverage:

  * ``_resolve_upstream_argv`` builds ``[npx, -y, claude-mermaid@<pin>]``
  * Missing npx → SystemExit(1) with a helpful stderr
  * Scoped-path validation accepts a good save path
  * Scoped-path validation rejects a flat-folder save
  * Scoped-path validation rejects extension-vs-kind mismatch
  * Non-save tools (``validate_syntax``) bypass path validation
  * Path-arg key fallback chain (path / file_path / output / ...)
  * Post-tool hook is silent when the indexer module is absent
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


_FAKE_MANIFEST = {
    "npm": {
        "mermaid_mcp": {
            "package": "claude-mermaid",
            "version": "1.6.3",
            "shasum": "a5f1050ef7af6dc2595f5507366006489fef2879",
        },
    },
}


def _patch_manifest():
    return mock.patch(
        "vco_lib.bundled_versions.load_bundled_versions",
        return_value=_FAKE_MANIFEST,
    )


# ─── Upstream argv resolution ────────────────────────────────────────────


class ResolveUpstreamArgvTests(unittest.TestCase):

    def test_argv_built_from_manifest_pin(self):
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value="/usr/bin/npx"):
            # Import inside the patch so the manifest patch is active
            # at module-import time (the proxy reads the manifest at
            # spawn-argv resolution, not at import — but importing here
            # is still the safest fence).
            from claude_mcp_servers.wrappers import mermaid_proxy
            argv = mermaid_proxy._resolve_upstream_argv()
        self.assertEqual(argv, ["/usr/bin/npx", "-y", "claude-mermaid@1.6.3"])

    def test_missing_npx_and_npm_exits_with_clear_stderr(self):
        # v0.2.91 WP-D: exit-1 now requires BOTH to be absent — with npm
        # present the wrapper falls back to `npm exec` (see the test below).
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value=None):
            from claude_mcp_servers.wrappers import mermaid_proxy
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    mermaid_proxy._resolve_upstream_argv()
            self.assertEqual(ctx.exception.code, 1)
            out = err.getvalue()
            self.assertIn("neither npx nor npm found", out)
            self.assertIn("nodejs.org", out)

    def test_npm_exec_fallback_when_only_npm_present(self):
        """v0.2.91 WP-D (report 5 item 1c). RED on c67ef888: the pre-fix
        resolver was a bare ``shutil.which("npx")`` and exited 1 here, so an
        fnm/nvm machine with npm-but-no-npx could not run the Mermaid MCP at
        all even though ``npm exec`` is an equivalent invocation."""
        # The npm path must live in a directory that does NOT exist, or the
        # ladder's step-2 sibling probe ("<dirname npm>/npx") would find the
        # real /usr/bin/npx on a developer machine and make this test's
        # verdict depend on the host. Hermetic by construction.
        fake_npm = "/nonexistent-vco-npx-test/bin/npm"

        def _which(name):
            return {"npm": fake_npm}.get(name)

        with _patch_manifest(), mock.patch("shutil.which", side_effect=_which):
            from claude_mcp_servers.wrappers import mermaid_proxy
            argv = mermaid_proxy._resolve_upstream_argv()
        self.assertEqual(
            argv,
            [fake_npm, "exec", "--yes", "--", "claude-mermaid@1.6.3"],
        )


# ─── Scoped-path validation ──────────────────────────────────────────────


class ValidateToolCallTests(unittest.TestCase):

    def setUp(self):
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value="/usr/bin/npx"):
            from claude_mcp_servers.wrappers.mermaid_proxy import MermaidProxy
            self.proxy = MermaidProxy()

    def test_save_diagram_with_valid_scoped_path_accepted(self):
        err = self.proxy.validate_tool_call(
            "save_diagram",
            {"path": ".claude/diagrams/gui/auth/login.mmd"},
        )
        self.assertIsNone(err)

    def test_save_diagram_with_flat_folder_rejected(self):
        err = self.proxy.validate_tool_call(
            "save_diagram",
            {"path": ".claude/diagrams/flat.mmd"},
        )
        self.assertIsNotNone(err)
        self.assertIn("flat-folder", err)

    def test_render_with_invalid_path_rejected(self):
        err = self.proxy.validate_tool_call(
            "render",
            {"output": ".claude/diagrams/bad.mmd"},
        )
        self.assertIsNotNone(err)
        self.assertIn("flat-folder", err)

    def test_save_with_excalidraw_extension_rejected_under_mermaid_kind(self):
        # Mermaid proxy enforces kind="mermaid" — .excalidraw must reject.
        err = self.proxy.validate_tool_call(
            "save_diagram",
            {"path": ".claude/diagrams/gui/login.excalidraw"},
        )
        self.assertIsNotNone(err)
        self.assertIn("mermaid", err)
        self.assertIn(".mmd", err)

    def test_non_save_tools_bypass_path_validation(self):
        # validate_syntax / list_themes don't write files — path rule
        # doesn't apply.
        for tool in ("validate_syntax", "list_themes", "export_png"):
            err = self.proxy.validate_tool_call(tool, {"path": "/anywhere"})
            self.assertIsNone(err, f"{tool} must NOT trigger path validation")

    def test_missing_path_arg_accepted(self):
        # No path key in arguments → no validation needed.
        # (Upstream will return its own usage error if a path was
        # actually required by the tool — we don't second-guess.)
        err = self.proxy.validate_tool_call("save_diagram", {"format": "svg"})
        self.assertIsNone(err)

    def test_path_arg_key_fallback_chain(self):
        # All these aliases must trigger validation when present.
        bad_path = ".claude/diagrams/flat.mmd"
        for key in ("path", "file_path", "output", "output_path", "dest", "destination"):
            err = self.proxy.validate_tool_call("save_diagram", {key: bad_path})
            self.assertIsNotNone(
                err,
                f"path-arg key {key!r} must trigger scoped-path validation",
            )

    def test_empty_path_string_treated_as_absent(self):
        # An empty string is not a real path; the validator skips
        # rather than rejecting (upstream will surface its own error).
        err = self.proxy.validate_tool_call("save_diagram", {"path": ""})
        self.assertIsNone(err)


# ─── Post-tool hook ──────────────────────────────────────────────────────


class PostToolHookTests(unittest.IsolatedAsyncioTestCase):

    async def test_post_hook_silent_when_indexer_absent(self):
        """Phase 1.5.A's diagram_indexer module hasn't landed yet.

        The wrapper MUST swallow the ImportError so the user-visible
        save flow still works. We assert it doesn't raise — no other
        observable behaviour expected.
        """
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value="/usr/bin/npx"):
            from claude_mcp_servers.wrappers.mermaid_proxy import MermaidProxy
            proxy = MermaidProxy()
        # Should not raise even though diagram_indexer doesn't exist.
        await proxy.post_tool_success(
            "save_diagram",
            {"path": ".claude/diagrams/gui/x.mmd"},
            {"saved": True},
        )

    async def test_post_hook_skips_non_save_tools(self):
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value="/usr/bin/npx"):
            from claude_mcp_servers.wrappers.mermaid_proxy import MermaidProxy
            proxy = MermaidProxy()

        # If the hook tries to import diagram_indexer, it'd ImportError;
        # but we use a fake module to detect inappropriate calls.
        fake_indexer = mock.MagicMock()
        fake_indexer.index_diagram_async = mock.AsyncMock()
        with mock.patch.dict(
            sys.modules, {"vco_lib.diagram_indexer": fake_indexer}
        ):
            await proxy.post_tool_success(
                "validate_syntax",
                {"source": "graph TD; A-->B"},
                {"valid": True},
            )
        fake_indexer.index_diagram_async.assert_not_called()

    async def test_post_hook_calls_indexer_on_save(self):
        with _patch_manifest(), \
             mock.patch("shutil.which", return_value="/usr/bin/npx"):
            from claude_mcp_servers.wrappers.mermaid_proxy import MermaidProxy
            proxy = MermaidProxy()

        fake_indexer = mock.MagicMock()
        fake_indexer.index_diagram_async = mock.AsyncMock()
        with mock.patch.dict(
            sys.modules, {"vco_lib.diagram_indexer": fake_indexer}
        ):
            await proxy.post_tool_success(
                "save_diagram",
                {"path": ".claude/diagrams/gui/x.mmd"},
                {"saved": True},
            )
        fake_indexer.index_diagram_async.assert_awaited_once_with(
            ".claude/diagrams/gui/x.mmd"
        )


if __name__ == "__main__":
    unittest.main()
