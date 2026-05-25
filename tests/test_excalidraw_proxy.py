# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the Excalidraw wrapper MCP (Phase 2 of the
diagrams-integration plan, 2026-05-25).

Mocks ``shutil.which`` + the bundled-versions manifest + the vendored
entry-point file existence so the tests don't depend on a real npm /
node install or on the vendored fork being present at its on-disk
location. Coverage mirrors ``test_mermaid_proxy.py`` so both wrappers'
contracts stay enforceable by a single suite shape:

  * ``_resolve_upstream_argv`` honours the ``file:`` pin and builds
    ``[node, <repo>/.../dist/mcp/index.js]``.
  * ``_resolve_upstream_argv`` honours a registry-style pin (fallback)
    and builds ``[npx, -y, <pkg>@<version>]``.
  * Missing node → SystemExit(1) with a clear stderr.
  * Missing vendored entry point (file: pin pointing at a non-existent
    dir) → SystemExit(1) with a clear stderr.
  * Scoped-path validation accepts a good ``.excalidraw`` save path.
  * Scoped-path validation rejects a flat-folder ``.excalidraw`` save.
  * Scoped-path validation BYPASSED for non-``.excalidraw`` extensions
    (``.svg`` / ``.png`` exports go where the user wants).
  * Allowlist-only filtering applies to non-save tools (they don't
    trip the path validator).
  * Path-arg key fallback chain (path / file_path / output / ...).
  * Post-tool hook is inert (v2.0.0 upstream has no MCP-routed disk
    save — indexing lives in the editor + hook chain).
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


_FILE_PIN_MANIFEST = {
    "npm": {
        "excalidraw_mcp": {
            # Moved from `claude_mcp_servers/excalidraw_mcp_fork` to
            # `vco_lib/excalidraw_mcp_fork` in v0.2.34 so the vendored
            # tree ships in the Python wheel (see
            # vco_lib/bundled_versions.py docstring).
            "package": "file:vco_lib/excalidraw_mcp_fork",
            "version": "git+vendored-2.0.0-2026-05-25",
            "shasum": "",
        },
    },
}

_REGISTRY_PIN_MANIFEST = {
    "npm": {
        "excalidraw_mcp": {
            "package": "excalidraw-mcp-server",
            "version": "2.0.0",
            "shasum": "15afa7b636830ebb97d0f474d2001253016d9bb1",
        },
    },
}


def _patch_manifest(manifest):
    """Patch the loader at BOTH the source module path AND the wrapper's
    local binding.

    Why both: the wrapper does ``from vco_lib.bundled_versions import
    load_bundled_versions``, which captures a direct reference at
    import time. Patching only the source module attribute leaves the
    wrapper's local binding pointing at the original function. The
    Mermaid proxy's test gets away with this because its fake-manifest
    values happen to match the real on-disk manifest — coincidence, not
    correctness. For the Excalidraw test the registry-pin variant
    DIFFERS from the real file-pin manifest, so the patch must reach
    the wrapper's binding too. Stacking two mock.patch context managers
    via ExitStack-equivalent — `mock.patch.multiple` doesn't accept
    `return_value`, so we use the dual-patch idiom directly.
    """
    # Import lazily so the wrapper module is loaded before we try to
    # patch its attribute (which only exists after first import).
    from claude_mcp_servers.wrappers import excalidraw_proxy  # noqa: F401

    class _DualPatch:
        def __init__(self) -> None:
            self._patches = [
                mock.patch(
                    "vco_lib.bundled_versions.load_bundled_versions",
                    return_value=manifest,
                ),
                mock.patch(
                    "claude_mcp_servers.wrappers.excalidraw_proxy."
                    "load_bundled_versions",
                    return_value=manifest,
                ),
            ]

        def __enter__(self):
            for p in self._patches:
                p.start()
            return self

        def __exit__(self, exc_type, exc, tb):
            for p in reversed(self._patches):
                p.stop()
            return False

    return _DualPatch()


# ─── Upstream argv resolution ────────────────────────────────────────────


class ResolveUpstreamArgvTests(unittest.TestCase):

    def test_file_pin_resolves_to_node_plus_vendor_entry(self):
        # The vendored fork actually exists in-tree (we vendored it as
        # part of Phase 2 itself), so this test doubles as a smoke
        # check that the vendor directory layout matches what the
        # wrapper expects.
        with _patch_manifest(_FILE_PIN_MANIFEST), \
             mock.patch("shutil.which", return_value="/usr/bin/node"):
            from claude_mcp_servers.wrappers import excalidraw_proxy
            argv = excalidraw_proxy._resolve_upstream_argv()
        self.assertEqual(argv[0], "/usr/bin/node")
        self.assertEqual(len(argv), 2)
        self.assertTrue(argv[1].endswith("/dist/mcp/index.js"))
        self.assertIn("excalidraw_mcp_fork", argv[1])
        self.assertTrue(Path(argv[1]).is_file(),
                        f"vendored entry point missing at {argv[1]}")

    def test_registry_pin_falls_through_to_npx(self):
        # Sanity: if a future release switches back to a registry
        # package, the wrapper's argv should mirror the Mermaid shape
        # (npx -y <pkg>@<version>). shutil.which is asked twice (for
        # both node and npx); easier to mock as a callable.
        def _which(name):
            return {"node": "/usr/bin/node", "npx": "/usr/bin/npx"}.get(name)
        with _patch_manifest(_REGISTRY_PIN_MANIFEST), \
             mock.patch("shutil.which", side_effect=_which):
            from claude_mcp_servers.wrappers import excalidraw_proxy
            argv = excalidraw_proxy._resolve_upstream_argv()
        self.assertEqual(argv, ["/usr/bin/npx", "-y", "excalidraw-mcp-server@2.0.0"])

    def test_missing_node_exits_with_clear_stderr(self):
        with _patch_manifest(_FILE_PIN_MANIFEST), \
             mock.patch("shutil.which", return_value=None):
            from claude_mcp_servers.wrappers import excalidraw_proxy
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    excalidraw_proxy._resolve_upstream_argv()
            self.assertEqual(ctx.exception.code, 1)
            out = err.getvalue()
            self.assertIn("node not found", out)
            self.assertIn("nodejs.org", out)

    def test_missing_vendor_dir_exits_with_clear_stderr(self):
        bad_manifest = {
            "npm": {
                "excalidraw_mcp": {
                    "package": "file:does/not/exist/anywhere",
                    "version": "vendored-noop",
                    "shasum": "",
                },
            },
        }
        with _patch_manifest(bad_manifest), \
             mock.patch("shutil.which", return_value="/usr/bin/node"):
            from claude_mcp_servers.wrappers import excalidraw_proxy
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    excalidraw_proxy._resolve_upstream_argv()
            self.assertEqual(ctx.exception.code, 1)
            out = err.getvalue()
            self.assertIn("vendored entry point not found", out)
            self.assertIn("VENDORED.md", out)


# ─── Scoped-path validation ──────────────────────────────────────────────


class ValidateToolCallTests(unittest.TestCase):

    def setUp(self):
        with _patch_manifest(_FILE_PIN_MANIFEST), \
             mock.patch("shutil.which", return_value="/usr/bin/node"):
            from claude_mcp_servers.wrappers.excalidraw_proxy import ExcalidrawProxy
            self.proxy = ExcalidrawProxy()

    def test_export_scene_with_valid_excalidraw_path_accepted(self):
        err = self.proxy.validate_tool_call(
            "export_scene",
            {"path": ".claude/diagrams/gui/auth/login.excalidraw"},
        )
        self.assertIsNone(err)

    def test_export_scene_with_flat_excalidraw_rejected(self):
        err = self.proxy.validate_tool_call(
            "export_scene",
            {"path": ".claude/diagrams/flat.excalidraw"},
        )
        self.assertIsNotNone(err)
        self.assertIn("flat-folder", err)

    def test_export_scene_with_svg_path_bypasses_validation(self):
        # SVG/PNG exports go anywhere — they're not source diagrams,
        # they're rendered artefacts. Scoped-path rule doesn't apply.
        for path in (
            "renders/auth-flow.svg",
            "/tmp/test.png",
            "doc/figures/architecture.png",
            ".claude/diagrams/gui/login.svg",  # even under diagrams/, svg bypasses
        ):
            err = self.proxy.validate_tool_call(
                "export_scene",
                {"path": path},
            )
            self.assertIsNone(
                err,
                f"non-.excalidraw export path {path!r} must not trip scoped validation",
            )

    def test_non_save_tools_bypass_path_validation(self):
        # The element-centric tools don't write to disk — path rule
        # doesn't apply (and they typically don't carry a path arg).
        for tool in (
            "create_element", "update_element", "delete_element",
            "query_elements", "get_resource", "batch_create_elements",
            "align_elements", "distribute_elements", "create_view",
            "read_me", "group_elements", "create_from_mermaid",
        ):
            err = self.proxy.validate_tool_call(
                tool,
                {"path": ".claude/diagrams/flat.excalidraw"},  # would fail if checked
            )
            self.assertIsNone(err, f"{tool} must NOT trigger path validation")

    def test_missing_path_arg_accepted(self):
        err = self.proxy.validate_tool_call(
            "export_scene",
            {"format": "svg"},
        )
        self.assertIsNone(err)

    def test_path_arg_key_fallback_chain(self):
        bad_path = ".claude/diagrams/flat.excalidraw"
        for key in ("path", "file_path", "output", "output_path",
                    "dest", "destination"):
            err = self.proxy.validate_tool_call(
                "export_scene", {key: bad_path},
            )
            self.assertIsNotNone(
                err,
                f"path-arg key {key!r} must trigger scoped-path validation",
            )

    def test_empty_path_string_treated_as_absent(self):
        err = self.proxy.validate_tool_call(
            "export_scene", {"path": ""},
        )
        self.assertIsNone(err)

    def test_excalidraw_extension_case_insensitive(self):
        # The wrapper's lower() suffix check tolerates uppercase
        # extensions — paranoia for Windows users who sometimes get
        # uppercase suffixes from editors.
        err = self.proxy.validate_tool_call(
            "export_scene",
            {"path": ".claude/diagrams/flat.EXCALIDRAW"},
        )
        self.assertIsNotNone(err)
        self.assertIn("flat-folder", err)


# ─── Post-tool hook ──────────────────────────────────────────────────────


class PostToolHookTests(unittest.IsolatedAsyncioTestCase):

    async def test_post_hook_is_inert_for_all_tools(self):
        """v2.0.0 upstream has no MCP-routed disk save → indexing
        happens via the launcher editor's Tauri write + PostToolUse
        hook chain, NOT via this wrapper. The post hook is
        deliberately a no-op; assert it doesn't raise or call any
        indexer for ANY tool name.
        """
        with _patch_manifest(_FILE_PIN_MANIFEST), \
             mock.patch("shutil.which", return_value="/usr/bin/node"):
            from claude_mcp_servers.wrappers.excalidraw_proxy import ExcalidrawProxy
            proxy = ExcalidrawProxy()

        fake_indexer = mock.MagicMock()
        fake_indexer.index_diagram_async = mock.AsyncMock()
        with mock.patch.dict(
            sys.modules, {"vco_lib.diagram_indexer": fake_indexer}
        ):
            for tool in (
                "create_element", "update_element", "delete_element",
                "query_elements", "export_scene", "get_resource",
                "read_me",
            ):
                # Must not raise.
                await proxy.post_tool_success(
                    tool,
                    {"path": ".claude/diagrams/gui/x.excalidraw"},
                    {"ok": True},
                )
        # And must not invoke the indexer.
        fake_indexer.index_diagram_async.assert_not_called()


if __name__ == "__main__":
    unittest.main()
