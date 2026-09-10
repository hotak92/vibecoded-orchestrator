"""Regression test for NEW-6 (2026-05-28): MCP resolver must prefer
CLAUDE_PROJECT_DIR over Path(__file__) so different Claude Code workspaces
resolve to different projects.

Pre-fix: _try_resolve_project_config() always passed
Path(__file__).parent.parent.parent (the server.py installation path) to
_resolve_project_config, which meant every workspace got the same project
config regardless of which workspace Claude Code was opened in.

Post-fix: the function checks os.environ['CLAUDE_PROJECT_DIR'] first and
uses it when it points at an existing directory.
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_server_module():
    """Re-import the weaviate_mcp server module with a clean slate.

    The module has module-level globals (_resolved_project_config,
    _HAS_PROJECT_CONFIG, etc.) that cache state.  We patch sys.modules
    to get a fresh copy each time.
    """
    mod_name = "claude_mcp_servers.weaviate_mcp.server"
    # Remove cached copy so importlib gives us a fresh module.
    sys.modules.pop(mod_name, None)
    # Also remove any sub-packages that might have been cached.
    for key in list(sys.modules.keys()):
        if key.startswith("claude_mcp_servers.weaviate_mcp"):
            sys.modules.pop(key, None)
    return importlib.import_module(mod_name)


def _make_spy_resolve(captured_paths: list):
    """Return a function that records the project_root it receives."""
    def _spy_resolve(project_root):
        captured_paths.append(Path(project_root).resolve())
        cfg = MagicMock()
        cfg.project_slug = f"project-{len(captured_paths)}"
        return cfg
    return _spy_resolve


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProjectResolutionPrefersEnvVar:
    """NEW-6: CLAUDE_PROJECT_DIR must take precedence over __file__."""

    def test_three_distinct_workspaces_give_three_distinct_roots(self, tmp_path):
        """Set CLAUDE_PROJECT_DIR to 3 different dirs; assert 3 different
        project_root values were passed to _resolve_project_config.

        Pre-fix: all 3 calls passed Path(__file__).parent.parent.parent
        (always the same path), so captured_paths would contain 3 copies of
        the same value — this assertion would fail.
        """
        # Create three distinct workspace directories on disk.
        ws_a = tmp_path / "workspace_a"
        ws_b = tmp_path / "workspace_b"
        ws_c = tmp_path / "workspace_c"
        for ws in (ws_a, ws_b, ws_c):
            ws.mkdir()

        captured_paths: list[Path] = []
        spy = _make_spy_resolve(captured_paths)

        for ws in (ws_a, ws_b, ws_c):
            # Each iteration: fresh module + env var pointing at this workspace.
            env_patch = {"CLAUDE_PROJECT_DIR": str(ws)}
            with patch.dict(os.environ, env_patch, clear=False):
                with patch(
                    "claude_mcp_servers.weaviate_mcp.server._resolve_project_config",
                    spy,
                ):
                    with patch(
                        "claude_mcp_servers.weaviate_mcp.server._HAS_PROJECT_CONFIG",
                        True,
                    ):
                        # Clear the module-level cache between calls.
                        import claude_mcp_servers.weaviate_mcp.server as srv
                        srv._resolved_project_config = None
                        srv._try_resolve_project_config()

        # Each call must have used the workspace path, not __file__.
        assert len(captured_paths) == 3, f"Expected 3 calls, got {len(captured_paths)}"
        assert captured_paths[0] == ws_a.resolve()
        assert captured_paths[1] == ws_b.resolve()
        assert captured_paths[2] == ws_c.resolve()
        # All three must differ — the critical regression guard.
        assert len(set(captured_paths)) == 3, (
            "Pre-fix regression: all three calls resolved to the same path. "
            "CLAUDE_PROJECT_DIR is not being preferred over __file__."
        )

    def test_missing_env_falls_back_to_file_path(self):
        """When CLAUDE_PROJECT_DIR is absent, the resolver must still hand the
        hub a real directory rather than reusing a previous workspace.

        v0.2.92 SUPERSEDED the old ``Path(__file__).parent.parent.parent``
        fallback: ``_resolution_context()`` now walks CWD's ancestors for a
        bundled VCO project first (rung 2) and only then falls back to
        ``_MODULE_OWN_ROOT`` (rung 3). Which of the two it returns depends on
        where the suite is run from, so the portable statement is the SHAPE:
        a derived root is a cwd ancestor or the module's own root — and, since
        v0.2.94, never a home directory.

        ``is_dir()`` alone could not say this: pytest keeps its last three tmp
        trees, so a stale workspace path from a sibling test is a directory
        too, and the leak this test names would pass unnoticed.
        """
        captured_paths: list[Path] = []
        spy = _make_spy_resolve(captured_paths)

        env_without_cpd = {k: v for k, v in os.environ.items()
                           if k != "CLAUDE_PROJECT_DIR"}

        with patch.dict(os.environ, env_without_cpd, clear=True):
            with patch(
                "claude_mcp_servers.weaviate_mcp.server._resolve_project_config",
                spy,
            ):
                with patch(
                    "claude_mcp_servers.weaviate_mcp.server._HAS_PROJECT_CONFIG",
                    True,
                ):
                    import claude_mcp_servers.weaviate_mcp.server as srv
                    srv._resolved_project_config = None
                    srv._try_resolve_project_config()

        assert len(captured_paths) == 1
        captured = captured_paths[0]
        assert captured.is_dir(), (
            f"Fallback path must be an existing directory, got: {captured}"
        )
        # DERIVED, not inherited from a sibling test's workspace: rung 2 can
        # only return an ancestor of the cwd, rung 3 only _MODULE_OWN_ROOT.
        # Any other path means a leaked/remembered workspace.
        assert captured in (Path.cwd(), *Path.cwd().parents) or (
            captured == srv._MODULE_OWN_ROOT
        ), (
            f"resolver returned {captured}, which is neither a cwd ancestor "
            f"nor _MODULE_OWN_ROOT ({srv._MODULE_OWN_ROOT}) — it did not "
            f"derive the root, it remembered one"
        )
        # v0.2.94: and never the user's home. Before the home boundary landed,
        # THIS call returned $HOME on any machine whose checkout carries no
        # .claude/settings.json — ~/.claude/settings.json is Claude Code's
        # GLOBAL config and matched the project probe on the way up.
        assert captured not in srv._home_boundaries(), (
            f"resolver returned the user's home ({captured}) as the project "
            f"root — ~/.claude/settings.json is the global config, not a marker"
        )

    def test_empty_env_var_falls_back_to_file_path(self):
        """Empty string CLAUDE_PROJECT_DIR must not be used as a workspace."""
        captured_paths: list[Path] = []
        spy = _make_spy_resolve(captured_paths)

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ""}, clear=False):
            with patch(
                "claude_mcp_servers.weaviate_mcp.server._resolve_project_config",
                spy,
            ):
                with patch(
                    "claude_mcp_servers.weaviate_mcp.server._HAS_PROJECT_CONFIG",
                    True,
                ):
                    import claude_mcp_servers.weaviate_mcp.server as srv
                    srv._resolved_project_config = None
                    srv._try_resolve_project_config()

        assert len(captured_paths) == 1
        # The captured path must be an existing directory (server.py's
        # parent.parent.parent, not an empty string turned into a path).
        assert captured_paths[0].is_dir()

    def test_nonexistent_env_var_path_falls_back_to_file_path(self, tmp_path):
        """CLAUDE_PROJECT_DIR pointing at a non-existent path must fall back."""
        captured_paths: list[Path] = []
        spy = _make_spy_resolve(captured_paths)

        nonexistent = str(tmp_path / "does_not_exist")

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": nonexistent}, clear=False):
            with patch(
                "claude_mcp_servers.weaviate_mcp.server._resolve_project_config",
                spy,
            ):
                with patch(
                    "claude_mcp_servers.weaviate_mcp.server._HAS_PROJECT_CONFIG",
                    True,
                ):
                    import claude_mcp_servers.weaviate_mcp.server as srv
                    srv._resolved_project_config = None
                    srv._try_resolve_project_config()

        assert len(captured_paths) == 1
        assert captured_paths[0].is_dir()
