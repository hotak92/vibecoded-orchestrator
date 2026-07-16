# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 (WP-B3): third-party MCP preservation across install/update.

A user may run their OWN MCP servers — searxng, Jira, Gmail, or anything else
— and they must stay PRESENT AND WORKING after ``install.py --update`` and the
bundle-update path. This suite pins the Python-side guarantees:

  A. FILESYSTEM. A user-added directory/file under ``claude_mcp_servers/``
     (e.g. ``claude_mcp_servers/my_custom_mcp/``) survives a bundle update
     untouched — no deletion, no overwrite, no deferral noise. The bundle
     orphan-deletion sweep only ever touches paths previously recorded in the
     manifest (== VCO-shipped), never user-added ones.

  B. ~/.claude.json REGISTRATIONS (Python detectors). The deprecated- and
     stale-MCP scanners (``vco_lib.install_mcp``) leave third-party entries
     alone:
       * ``searxng`` is NOT in the deprecated registry — VCO never registered
         a searxng MCP and no longer ships searxng at all, so a ``searxng``
         entry is USER property (they may run their own) and is never scanned
         for removal.
       * even a name that IS in the deprecated registry (``ollama``) is left
         alone when its command path is OUTSIDE the install root (user-added).
       * the stale scanner only flags vco-install-shaped absolute paths
         outside the install root — third-party entries at unrelated paths
         (or via ``npx``) are never classified stale.

Hermetic + OS-agnostic: the ``~/.claude.json`` used by the detectors is
dependency-injected as a ``claude_json`` parameter, so tests point it at a
``tmp_path`` file (never the real user home). The global-home resolver
``install._user_home_for_install`` honors ``VCT_USER_HOME_OVERRIDE`` for the
callers that resolve it — asserted here so the OS-specific home derivation is
pinned too (never a unix literal).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.install_mcp import (  # noqa: E402
    _DEPRECATED_DEFAULT_MCPS,
    _scan_deprecated_mcp_entries,
    _scan_stale_mcp_entries,
)
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


# ── A. Filesystem: user MCP dir under claude_mcp_servers/ survives update ──
class TestThirdPartyMcpDirSurvivesBundleUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-b3-tpmcp-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        self.logs: list = []

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _run_update(self):
        return project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
            log_event=lambda step, phase, detail="", *, data=None:
                self.logs.append((step, phase, detail)),
        )

    def test_user_added_mcp_dir_untouched_by_bundle_update(self):
        # A user's own MCP living under claude_mcp_servers/ inside the project.
        # (Even a searxng — VCO no longer ships one, so it's pure user data.)
        user_mcp = self.proj / "claude_mcp_servers" / "my_custom_mcp"
        user_mcp.mkdir(parents=True)
        server = user_mcp / "server.py"
        server_body = "def serve():\n    return 'my custom mcp'\n"
        server.write_text(server_body, encoding="utf-8")
        cfg = user_mcp / "settings.yml"
        cfg_body = "secret_key: \"user-owned-do-not-touch\"\n"
        cfg.write_text(cfg_body, encoding="utf-8")

        # First materialize the bundle (so a manifest exists), then update.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        result = self._run_update()

        # Files are byte-for-byte intact.
        self.assertTrue(server.exists(), "user MCP server.py must survive update")
        self.assertEqual(server.read_text(encoding="utf-8"), server_body)
        self.assertTrue(cfg.exists(), "user MCP settings.yml must survive update")
        self.assertEqual(cfg.read_text(encoding="utf-8"), cfg_body)

        # No orphan-deletion touched a user path.
        orphan_deleted = (
            result.get("actions", {}).get("orphan-deleted", [])
            if isinstance(result, dict) else []
        )
        for path in orphan_deleted:
            self.assertNotIn(
                "my_custom_mcp", str(path),
                f"a user MCP path was orphan-deleted: {path}",
            )

        # And no deferral names the user path.
        deferred = DeferralReport.read(self.proj)
        for e in deferred.entries:
            self.assertNotIn(
                "my_custom_mcp", e.detected + e.command_to_apply,
                f"a deferral referenced the user MCP: {e.condition_id}",
            )

    def test_second_update_still_leaves_user_mcp_alone(self):
        """Idempotency: a user MCP added AFTER the first update is still not
        swept on the next update (it was never in the manifest)."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # Add the user MCP only now — it will never appear in manifest["files"].
        user_mcp = self.proj / "claude_mcp_servers" / "searxng"
        user_mcp.mkdir(parents=True)
        marker = user_mcp / "settings.yml"
        marker.write_text("secret_key: \"mine\"\n", encoding="utf-8")

        self._run_update()
        self.assertTrue(
            marker.exists(),
            "a user's own searxng under claude_mcp_servers/ must survive update",
        )


# ── B. ~/.claude.json detectors leave third-party entries alone ──────────
class TestDeprecatedScanLeavesThirdPartyAlone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install_root = self.root / "install"
        self.install_root.mkdir()
        self.claude_json = self.root / ".claude.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_claude_json(self, servers: dict) -> None:
        self.claude_json.write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )

    def test_searxng_not_in_deprecated_registry(self):
        # The spicy case: VCO never registered a searxng MCP, and no longer
        # ships searxng at all, so it must NOT be a deprecated-default entry.
        self.assertNotIn(
            "searxng", _DEPRECATED_DEFAULT_MCPS,
            "a `searxng` MCP entry is user property — it must never be in the "
            "deprecated-removal registry",
        )

    def test_searxng_entry_never_scanned_for_removal(self):
        # Even at a vco-shaped path inside install_root, a `searxng` entry is
        # not scanned (name not in the registry).
        self._write_claude_json({
            "searxng": {
                "command": str(
                    self.install_root / "claude_mcp_servers" / "searxng" / "serve.py"
                )
            }
        })
        found = _scan_deprecated_mcp_entries(self.install_root, self.claude_json)
        names = [f[0] for f in found]
        self.assertNotIn(
            "searxng", names,
            "a searxng entry must never be classified as a deprecated default",
        )

    def test_deprecated_name_at_user_path_left_alone(self):
        # `ollama` IS in the deprecated registry, but a user-added ollama at a
        # path OUTSIDE install_root must be left alone (user property).
        self.assertIn("ollama", _DEPRECATED_DEFAULT_MCPS)  # premise
        self._write_claude_json({
            "ollama": {"command": "/opt/my-ollama/mcp-server"}
        })
        found = _scan_deprecated_mcp_entries(self.install_root, self.claude_json)
        names = [f[0] for f in found]
        self.assertNotIn(
            "ollama", names,
            "a user-added ollama outside install_root must not be scanned for "
            "removal (path-inside-install-root gate)",
        )

    def test_deprecated_name_via_npx_left_alone(self):
        # No absolute path at all (npx) ⇒ never matched.
        self._write_claude_json({
            "ollama": {"command": "npx", "args": ["-y", "@acme/ollama-mcp"]}
        })
        found = _scan_deprecated_mcp_entries(self.install_root, self.claude_json)
        self.assertEqual(
            [f[0] for f in found], [],
            "an npx-launched entry has no install-root path ⇒ never deprecated-scanned",
        )

    def test_stale_scan_leaves_third_party_absolute_paths_alone(self):
        # The stale scanner only flags vco-install-shaped absolute paths
        # OUTSIDE install_root. Third-party MCPs at unrelated paths are safe.
        self._write_claude_json({
            "searxng": {"command": "/usr/local/bin/searxng-mcp"},
            "jira": {"command": "/opt/jira-mcp/serve", "args": ["--project", "ACME"]},
            "gmail": {"command": "npx", "args": ["-y", "@acme/gmail-mcp"]},
        })
        stale = _scan_stale_mcp_entries(self.install_root, self.claude_json)
        stale_names = [s[0] if isinstance(s, tuple) else getattr(s, "name", s) for s in stale]
        for name in ("searxng", "jira", "gmail"):
            self.assertNotIn(
                name, stale_names,
                f"third-party MCP `{name}` must never be classified stale: {stale_names}",
            )


class TestGlobalHomeResolverIsOverridable(unittest.TestCase):
    """The global ~/.claude.json home is resolved via a single shared resolver
    that honors VCT_USER_HOME_OVERRIDE — so tests are hermetic and the Windows
    home derivation is never a unix literal (the resolver returns whatever
    Path.home() yields per-OS, and the override wins for tests)."""

    def test_home_resolver_honors_override(self):
        fake = Path(tempfile.mkdtemp(prefix="vct-fake-home-"))
        prev = os.environ.get("VCT_USER_HOME_OVERRIDE")
        try:
            os.environ["VCT_USER_HOME_OVERRIDE"] = str(fake)
            self.assertEqual(install._user_home_for_install(), fake)
            # The claude.json path the install-side readers use is derived from
            # this resolver — so it lands under the fake home, never the real one.
            self.assertEqual(
                install._user_home_for_install() / ".claude.json",
                fake / ".claude.json",
            )
        finally:
            if prev is None:
                os.environ.pop("VCT_USER_HOME_OVERRIDE", None)
            else:
                os.environ["VCT_USER_HOME_OVERRIDE"] = prev
            import shutil
            shutil.rmtree(str(fake), ignore_errors=True)

    def test_home_resolver_falls_back_to_path_home(self):
        prev = os.environ.get("VCT_USER_HOME_OVERRIDE")
        try:
            os.environ.pop("VCT_USER_HOME_OVERRIDE", None)
            self.assertEqual(install._user_home_for_install(), Path.home())
        finally:
            if prev is not None:
                os.environ["VCT_USER_HOME_OVERRIDE"] = prev


if __name__ == "__main__":
    unittest.main()
