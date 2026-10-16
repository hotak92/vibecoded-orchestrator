# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-23 (v0.2.12): install.py registers bundled MCP servers in ~/.claude.json.

Covers the Python side of the 4-tier launcher-binary resolution + the
pure-Python JSON merge fallback. Inline Rust tests in
``launcher/src-tauri/src/mcp_registration.rs`` cover the Rust side.

Critical invariants asserted here:

* The Rust-side launcher binary is the preferred writer (Tier 1).
* When no binary is bundled and no network/cargo, the Python writer
  still succeeds (Tier 4).
* Secret-shaped env keys (``GITHUB_TOKEN``, ``OPENAI_API_KEY``, etc.)
  are silently dropped before the entry is written to ``~/.claude.json``.
* Per-project keys (``KG_COLLECTION``, ``PROJECT_NAME``, etc.) are
  similarly absent — they live in each project's
  ``.claude/settings.json env``, not the global file.
* Ollama MCP is NOT written (deprecated in v0.2.11).
* Existing user-added MCP entries and other top-level keys are preserved
  through the merge (mirrors ``mcp_registration.rs`` discipline).
* Stale-MCP-entry detection emits a deferral entry instead of silently
  rewriting (consent-driven).

References:
  * ``.claude/context/mcp-install-pipeline-audit-2026-05-16.md``
  * ``.claude/context/PUBLIC_REPO_FIXES_REPORT_2026-05-16.md`` Fix #10
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.service_endpoints import urls_from_rows  # noqa: E402

# No rows: the compiled defaults (http://localhost:8081 / :11435 / :11440, gRPC 50052).
_DEFAULT_URLS = urls_from_rows({})


_IS_WINDOWS = platform.system().lower().startswith("win")


def _make_pseudo_install_root(tmp_path: Path) -> Path:
    """Create a minimal install-root layout with a fake venv-python so
    ``_resolve_venv_python_for_install`` finds something.
    """
    root = tmp_path / "example_install"
    root.mkdir()
    sub = "Scripts" if _IS_WINDOWS else "bin"
    py_name = "python.exe" if _IS_WINDOWS else "python"
    venv_bin = root / ".venv" / sub
    venv_bin.mkdir(parents=True)
    (venv_bin / py_name).write_text("#!/bin/sh\nexit 0\n")
    if not _IS_WINDOWS:
        (venv_bin / py_name).chmod(0o755)
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
    return root


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers (no install pipeline)
# ─────────────────────────────────────────────────────────────────────────


class SecretShapedKeysTests(unittest.TestCase):
    """``_is_secret_shaped_env_key`` must catch every credential pattern."""

    def test_positive_cases(self):
        for key in [
            "GITHUB_TOKEN",
            "github_token",
            "OPENAI_API_KEY",
            "MY_PAT",
            "SECRET_VALUE",
            "PASSWORD",
            "DB_PASS",
            "AUTH_HEADER",
            "KEY",
            "STRIPE_KEY",
            "my_key",
        ]:
            self.assertTrue(
                install._is_secret_shaped_env_key(key),
                f"expected `{key}` to be flagged as secret-shaped",
            )

    def test_negative_cases(self):
        """Non-secret keys must NOT trip the filter."""
        for key in [
            "WEAVIATE_URL",
            "OLLAMA_URL",
            "PYTHONPATH",
            "KG_COLLECTION",
            "KG_BASE_DIR",
            "ACTIVE_EMBEDDING",
            "EMBEDDING_MODEL",
            "CODE_EMBED_SERVICE_URL",
            "RL_SERVER_URL",
            "GRPC_PORT",
        ]:
            self.assertFalse(
                install._is_secret_shaped_env_key(key),
                f"expected `{key}` to be allowed (not secret-shaped)",
            )


class FilterEnvTests(unittest.TestCase):
    """``_filter_env_for_global_json`` enforces allowlist + secret denylist."""

    def test_secrets_are_dropped(self):
        """Critical security test — GITHUB_TOKEN MUST NOT survive the filter."""
        candidate = {
            "WEAVIATE_URL": "http://localhost:8081",
            "OLLAMA_URL": "http://localhost:11435",
            "GITHUB_TOKEN": "ghp_super_secret_xxx",
            "OPENAI_API_KEY": "sk-xyz",  # vco-secret-scan: test-placeholder
        }
        safe, dropped = install._filter_env_for_global_json(candidate)
        self.assertIn("WEAVIATE_URL", safe)
        self.assertIn("OLLAMA_URL", safe)
        self.assertNotIn(
            "GITHUB_TOKEN", safe,
            "SECURITY: GITHUB_TOKEN must never appear in ~/.claude.json",
        )
        self.assertNotIn(
            "OPENAI_API_KEY", safe,
            "SECURITY: OPENAI_API_KEY must never appear in ~/.claude.json",
        )
        self.assertIn("GITHUB_TOKEN", dropped)
        self.assertIn("OPENAI_API_KEY", dropped)

    def test_per_project_keys_are_dropped(self):
        """Per-project keys belong in .claude/settings.json env, not the global file."""
        candidate = {
            "WEAVIATE_URL": "http://localhost:8081",
            "KG_COLLECTION": "FooProj_KG",
            "PROJECT_NAME": "Foo",
            "DEVELOPMENT_COLLECTION": "FooDev",
        }
        safe, dropped = install._filter_env_for_global_json(candidate)
        self.assertIn("WEAVIATE_URL", safe)
        self.assertNotIn("KG_COLLECTION", safe)
        self.assertNotIn("PROJECT_NAME", safe)
        self.assertNotIn("DEVELOPMENT_COLLECTION", safe)
        for k in ("KG_COLLECTION", "PROJECT_NAME", "DEVELOPMENT_COLLECTION"):
            self.assertIn(k, dropped)


# ─────────────────────────────────────────────────────────────────────────
# Venv-Python + binary resolution
# ─────────────────────────────────────────────────────────────────────────


class VenvPythonResolutionTests(unittest.TestCase):
    """``_resolve_venv_python_for_install`` walks two candidate paths."""

    def test_canonical_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            result = install._resolve_venv_python_for_install(root)
            self.assertIsNotNone(result)
            self.assertTrue(result.exists())
            self.assertIn(".venv", str(result))

    def test_legacy_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "example_install"
            root.mkdir()
            sub = "Scripts" if _IS_WINDOWS else "bin"
            py_name = "python.exe" if _IS_WINDOWS else "python"
            legacy_bin = root / "claude_mcp_servers" / ".venv" / sub
            legacy_bin.mkdir(parents=True)
            (legacy_bin / py_name).write_text("#!/bin/sh\n")
            if not _IS_WINDOWS:
                (legacy_bin / py_name).chmod(0o755)
            result = install._resolve_venv_python_for_install(root)
            self.assertIsNotNone(result)
            self.assertIn("claude_mcp_servers", str(result))

    def test_no_venv_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty_install"
            root.mkdir()
            self.assertIsNone(install._resolve_venv_python_for_install(root))


class LauncherBinaryResolutionTests(unittest.TestCase):
    """Tier ordering of ``_ensure_launcher_binary``: bundled > download > cargo."""

    def test_bundled_binary_preferred(self):
        """Tier 1: bundled binary at launcher/dist/<os>-<arch>/ wins immediately."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "example_install"
            subdir, fname = install._launcher_binary_relative_path()
            bundled_dir = root / "launcher" / "dist" / subdir
            bundled_dir.mkdir(parents=True)
            bundled_path = bundled_dir / fname
            bundled_path.write_text("#!/bin/sh\nexit 0\n")
            if not _IS_WINDOWS:
                bundled_path.chmod(0o755)
            result = install._try_bundled_launcher_binary(root)
            self.assertIsNotNone(result)
            self.assertEqual(result.resolve(), bundled_path.resolve())

    def test_bundled_binary_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "example_install"
            root.mkdir()
            self.assertIsNone(install._try_bundled_launcher_binary(root))

    def test_ensure_falls_through_to_none_when_all_tiers_fail(self):
        """No bundled binary + no network + no cargo → returns None.

        Patches the download + cargo helpers to simulate offline + no
        cargo. The pure-Python fallback in _register_mcps handles None.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty_install"
            root.mkdir()  # no launcher/ subdir
            with mock.patch.object(install, "_try_download_launcher_binary", return_value=None), \
                 mock.patch.object(install, "_try_cargo_tauri_build", return_value=None):
                self.assertIsNone(install._ensure_launcher_binary(root))


# ─────────────────────────────────────────────────────────────────────────
# Entry construction
# ─────────────────────────────────────────────────────────────────────────


class BuildEntriesTests(unittest.TestCase):
    """``_build_python_mcp_entries`` mirrors the Rust entry shape."""

    def test_entries_have_correct_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            entries = install._build_python_mcp_entries(root, py, _DEFAULT_URLS)
            names = [n for n, _, _ in entries]
            # Critical: ollama MCP was deprecated in v0.2.11 (see
            # install.py:_check_ollama_mcp_remnants). Must NOT be in the
            # bundled list. vct-coordination is Pro-tier and also excluded.
            # F-1 (v0.2.73): playwright appended. v0.2.101 retired the
            # diagram wrappers from default registration and deleted the
            # `search` MCP, so the builder emits exactly these two.
            self.assertEqual(names, ["weaviate-kg", "playwright"])

    def test_playwright_entry_shape(self):
        """F-1 (v0.2.73) + v0.2.102 W6-Z4: playwright registration is the
        PINNED launch `npx -y @playwright/mcp@<pin> --browser chromium` from
        bundled_mcp_versions.toml [npx.playwright_mcp], with an empty env and
        no venv-python involvement — same shape as the Rust builder
        (mcp_registration.rs) and the GUI catalog (types.rs). Never the
        floating `@latest`, which launched the system-Chrome channel."""
        from vco_lib import playwright_mcp
        pin = playwright_mcp.load_pin()
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            entries = install._build_python_mcp_entries(root, py, _DEFAULT_URLS)
            by_name = {n: (e, d) for n, e, d in entries}
            entry, dropped = by_name["playwright"]
            self.assertEqual(entry["type"], "stdio")
            self.assertEqual(entry["command"], "npx")
            self.assertEqual(
                entry["args"],
                ["-y", f"@playwright/mcp@{pin.version}", "--browser", "chromium"],
            )
            self.assertNotEqual(pin.version, "latest")
            self.assertEqual(entry["env"], {})
            self.assertEqual(dropped, [])

    def test_weaviate_entry_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            entries = install._build_python_mcp_entries(root, py, _DEFAULT_URLS)
            name, entry, _ = entries[0]
            self.assertEqual(name, "weaviate-kg")
            self.assertEqual(entry["type"], "stdio")
            self.assertEqual(entry["command"], str(py))
            self.assertEqual(len(entry["args"]), 1)
            self.assertTrue(entry["args"][0].endswith(
                "server.py" if not _IS_WINDOWS else "server.py"
            ))
            # Allowlisted env keys present:
            self.assertEqual(entry["env"]["WEAVIATE_URL"], "http://localhost:8081")
            self.assertEqual(entry["env"]["OLLAMA_URL"], "http://localhost:11435")
            self.assertEqual(entry["env"]["GRPC_PORT"], "50052")
            # v0.2.103: ACTIVE_EMBEDDING is NOT written here any more — a
            # global copy in ~/.claude.json overrode the launcher's embedding
            # profile projected into .claude/settings.json env.
            self.assertNotIn("ACTIVE_EMBEDDING", entry["env"])
            self.assertEqual(entry["env"]["CODE_EMBED_SERVICE_URL"], "http://localhost:11440")
            # Per-project keys MUST be absent (Claude Code's
            # ~/.claude.json mcpServers.*.env wins against
            # .claude/settings.json env, so any per-project-varying
            # value here would override the launcher's per-project value
            # the wrong direction):
            self.assertNotIn("KG_COLLECTION", entry["env"])
            self.assertNotIn("PROJECT_NAME", entry["env"])
            self.assertNotIn("DEVELOPMENT_COLLECTION", entry["env"])
            # PR-43 (post-PR-23): EMBEDDING_MODEL + RL_SERVER_URL removed
            # from _ALLOWED_GLOBAL_ENV_KEYS — these now live in
            # .claude/settings.json env per-project so users with custom
            # embedding models or alternate RL server ports don't get
            # shadowed.
            self.assertNotIn("EMBEDDING_MODEL", entry["env"])
            self.assertNotIn("RL_SERVER_URL", entry["env"])

    # v0.2.101: the `search` MCP (wrapper.sh on Unix / python on Windows) was
    # deleted, so its two builder-shape tests are gone.


# ─────────────────────────────────────────────────────────────────────────
# Python-fallback JSON write (Tier 4)
# ─────────────────────────────────────────────────────────────────────────


class PythonFallbackWriterTests(unittest.TestCase):
    """``_python_fallback_write_mcp_entries`` mirrors Rust's atomic write."""

    def test_creates_file_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            entries = install._build_python_mcp_entries(root, py, _DEFAULT_URLS)
            target = Path(td) / "fake_home" / ".claude.json"
            self.assertFalse(target.exists())
            success, errors = install._python_fallback_write_mcp_entries(target, entries)
            # v0.2.101: exactly weaviate-kg + playwright.
            self.assertEqual(success, 2)
            self.assertEqual(errors, [])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("weaviate-kg", data["mcpServers"])
            self.assertIn("playwright", data["mcpServers"])
            # Retired defaults MUST NOT be written.
            for retired in ("search", "mermaid", "excalidraw", "ollama"):
                self.assertNotIn(retired, data["mcpServers"])

    def test_preserves_existing_keys(self):
        """Pre-existing user MCPs + top-level keys MUST survive the merge."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            entries = install._build_python_mcp_entries(root, py, _DEFAULT_URLS)
            target = Path(td) / ".claude.json"
            existing = {
                "permissions": {"allow": ["Read", "Edit"]},
                "mcpServers": {
                    "my-user-mcp": {
                        "command": "/usr/bin/my-mcp",
                        "type": "stdio",
                    }
                },
            }
            target.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            success, errors = install._python_fallback_write_mcp_entries(target, entries)
            # v0.2.101: two entries (weaviate-kg + playwright).
            self.assertEqual(success, 2)
            data = json.loads(target.read_text(encoding="utf-8"))
            # User's pre-existing MCP survives.
            self.assertEqual(
                data["mcpServers"]["my-user-mcp"]["command"],
                "/usr/bin/my-mcp",
            )
            # Pre-existing top-level keys survive.
            self.assertEqual(data["permissions"]["allow"], ["Read", "Edit"])
            # Orchestrator MCPs were added.
            self.assertIn("weaviate-kg", data["mcpServers"])
            self.assertIn("playwright", data["mcpServers"])

    def test_no_secrets_in_written_entries(self):
        """End-to-end: a candidate env with GITHUB_TOKEN never reaches disk."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            py = install._resolve_venv_python_for_install(root)
            # Tamper an entry to simulate a future bug that tries to
            # smuggle a secret into the env. The writer doesn't re-filter
            # (filtering is build-time), so this test asserts the contract
            # at the BUILD layer via _build_python_mcp_entries above.
            #
            # Belt-and-braces variant: pass through the public filter
            # explicitly and assert no secret survives.
            candidate = {
                "WEAVIATE_URL": "http://localhost:8081",
                "GITHUB_TOKEN": "ghp_xxx_should_never_appear_on_disk",
            }
            safe, dropped = install._filter_env_for_global_json(candidate)
            # Mutate the entry's env with the safe map and write.
            entries = [
                ("test-mcp", {
                    "type": "stdio",
                    "command": str(py),
                    "args": ["server.py"],
                    "env": safe,
                }, dropped),
            ]
            target = Path(td) / ".claude.json"
            success, _ = install._python_fallback_write_mcp_entries(target, entries)
            self.assertEqual(success, 1)
            raw = target.read_text(encoding="utf-8")
            self.assertNotIn(
                "GITHUB_TOKEN", raw,
                "SECURITY: GITHUB_TOKEN must NEVER appear on disk in ~/.claude.json",
            )
            self.assertNotIn(
                "ghp_xxx_should_never_appear_on_disk", raw,
                "SECURITY: secret value must NEVER appear on disk in ~/.claude.json",
            )


# ─────────────────────────────────────────────────────────────────────────
# Top-level _register_mcps orchestration
# ─────────────────────────────────────────────────────────────────────────


class RegisterMcpsOrchestrationTests(unittest.TestCase):
    """``_register_mcps`` integrates the binary resolver + writer + deferrals."""

    def _run_with_overrides(self, install_root: Path, fake_home: Path,
                            bundled_binary: bool, allow_download: bool,
                            allow_cargo: bool) -> DeferralReport:
        """Drive _register_mcps with VCT_USER_HOME_OVERRIDE + helper patches."""
        report = DeferralReport()
        with mock.patch.dict(os.environ, {"VCT_USER_HOME_OVERRIDE": str(fake_home)}, clear=False):
            patches = []
            if not bundled_binary:
                patches.append(mock.patch.object(install, "_try_bundled_launcher_binary", return_value=None))
            if not allow_download:
                patches.append(mock.patch.object(install, "_try_download_launcher_binary", return_value=None))
            if not allow_cargo:
                patches.append(mock.patch.object(install, "_try_cargo_tauri_build", return_value=None))
            for p in patches:
                p.start()
            try:
                install._register_mcps(install_root, report)
            finally:
                for p in patches:
                    p.stop()
        return report

    def test_python_fallback_when_all_binary_tiers_fail(self):
        """No bundled binary, no download, no cargo → Python writer succeeds."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            report = self._run_with_overrides(
                root, fake_home,
                bundled_binary=False, allow_download=False, allow_cargo=False,
            )
            target = fake_home / ".claude.json"
            self.assertTrue(
                target.is_file(),
                "Python fallback must succeed even when launcher binary is unavailable",
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("weaviate-kg", data["mcpServers"])
            self.assertIn("playwright", data["mcpServers"])
            self.assertNotIn("ollama", data["mcpServers"])

            # Deferral emitted because Python fallback was used (informational).
            ids = [e.condition_id for e in report.entries]
            self.assertIn("mcp_registration_python_fallback", ids)

    def test_update_rewrites_a_legacy_latest_playwright_entry(self):
        """v0.2.102 W6-Z4 — the refresh path for EXISTING installs: a
        ~/.claude.json still carrying the pre-pin `npx -y
        @playwright/mcp@latest` entry is rewritten to the pinned
        `--browser chromium` launch by the ordinary registration pass
        (`_register_mcps`, which every install and `--update` runs), and a
        user's unrelated MCP is left byte-identical. Red-proof: restore the
        `@latest` literal in `_build_python_mcp_entries` → this fails."""
        from vco_lib import playwright_mcp
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            mine = {"command": "/opt/mine/serve", "args": ["--x"]}
            target.write_text(json.dumps({"mcpServers": {
                "playwright": {"type": "stdio", "command": "npx",
                               "args": ["-y", "@playwright/mcp@latest"], "env": {}},
                "mine": mine,
            }}), encoding="utf-8")
            self._run_with_overrides(
                root, fake_home,
                bundled_binary=False, allow_download=False, allow_cargo=False,
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                data["mcpServers"]["playwright"],
                playwright_mcp.registration_entry(),
            )
            self.assertIn("--browser", data["mcpServers"]["playwright"]["args"])
            self.assertEqual(data["mcpServers"]["mine"], mine)

    def test_update_drops_a_legacy_global_active_embedding(self):
        """v0.2.103 — an existing weaviate-kg registration written by an older
        VCO carries ``ACTIVE_EMBEDDING=qwen3`` in ~/.claude.json, which Claude
        Code applies AFTER .claude/settings.json env and so overrode the
        launcher's global embedding profile in every MCP subprocess. The
        ordinary registration pass must rewrite the entry without it, and the
        key must not be allowlisted for the global file. Red-proof: put
        ``"ACTIVE_EMBEDDING": "qwen3"`` back into ``_build_python_mcp_entries``
        and the allowlist → this fails."""
        from vco_lib import install_mcp
        self.assertNotIn("ACTIVE_EMBEDDING", install_mcp._ALLOWED_GLOBAL_ENV_KEYS)
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            target.write_text(json.dumps({"mcpServers": {
                "weaviate-kg": {"type": "stdio", "command": "/old/python",
                                "args": ["server.py"],
                                "env": {"ACTIVE_EMBEDDING": "qwen3",
                                        "WEAVIATE_URL": "http://localhost:8081"}},
            }}), encoding="utf-8")
            self._run_with_overrides(
                root, fake_home,
                bundled_binary=False, allow_download=False, allow_cargo=False,
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            env = data["mcpServers"]["weaviate-kg"]["env"]
            self.assertNotIn("ACTIVE_EMBEDDING", env)
            self.assertIn("WEAVIATE_URL", env)

    @unittest.skipIf(_IS_WINDOWS, "the fake launcher binary is a POSIX script")
    def test_stale_bundled_binary_active_embedding_is_stripped_by_the_update(self):
        """v0.2.103 F2 (Windows field run) — the REAL writer path: install's
        Path A runs the BUNDLED launcher binary, and a fresh clone's
        ``launcher/dist`` binary was built at the previous tag, before item
        31(d) — it writes ``weaviate-kg.env.ACTIVE_EMBEDDING=qwen3``. The
        ordinary install/update sequence (``_register_mcps`` then
        ``_auto_scrub_removed_mcp_entries``, main() order) must leave the
        key gone, keep the allowlisted keys, and leave a user's own MCP (and
        a user's same-named entry outside the install root) untouched.
        Red-proof: make ``strip_unallowlisted_global_env`` return early →
        this fails."""
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            server_py = root / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
            old_shape = {
                "weaviate-kg": {
                    "type": "stdio", "command": str(root / ".venv" / "bin" / "python"),
                    "args": [str(server_py)],
                    "env": {"WEAVIATE_URL": "http://localhost:8081",
                            "OLLAMA_URL": "http://localhost:11435",
                            "GRPC_PORT": "50052",
                            "PYTHONPATH": str(root / "claude_mcp_servers"),
                            "CODE_EMBED_SERVICE_URL": "http://localhost:11440",
                            "ACTIVE_EMBEDDING": "qwen3"},
                },
                "mine": {"command": "/opt/mine/serve", "args": [], "env": {"X_MODE": "1"}},
            }
            shape_file = Path(td) / "old_shape.json"
            shape_file.write_text(json.dumps({"mcpServers": old_shape}), encoding="utf-8")
            fake_bin = Path(td) / "vct-launcher"
            fake_bin.write_text(
                f"#!{sys.executable}\n"
                "import os, shutil, sys\n"
                "assert sys.argv[1] == '--register-default-mcps'\n"
                f"shutil.copy({str(shape_file)!r}, "
                "os.path.join(os.environ['VCT_USER_HOME_OVERRIDE'], '.claude.json'))\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)
            report = DeferralReport()
            with mock.patch.dict(os.environ, {"VCT_USER_HOME_OVERRIDE": str(fake_home)}), \
                    mock.patch.object(install, "_try_bundled_launcher_binary", return_value=fake_bin):
                install._register_mcps(root, report)
                written = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(  # the stale binary really wrote the retired key
                    written["mcpServers"]["weaviate-kg"]["env"]["ACTIVE_EMBEDDING"], "qwen3")
                lines: list[str] = []
                install._auto_scrub_removed_mcp_entries(root, output_fn=lines.append)
            data = json.loads(target.read_text(encoding="utf-8"))
            env = data["mcpServers"]["weaviate-kg"]["env"]
            self.assertNotIn("ACTIVE_EMBEDDING", env)
            self.assertEqual(
                {k: v for k, v in old_shape["weaviate-kg"]["env"].items() if k != "ACTIVE_EMBEDDING"},
                env,
            )
            self.assertEqual(data["mcpServers"]["mine"], old_shape["mine"])
            self.assertTrue(any("ACTIVE_EMBEDDING" in ln and "weaviate-kg" in ln for ln in lines), lines)

    def test_strip_leaves_foreign_entries_and_clean_files_alone(self):
        """A same-named entry OUTSIDE the install root is the user's (kept
        verbatim), and a file with nothing retired is not rewritten at all."""
        from vco_lib.install_mcp import strip_unallowlisted_global_env
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            foreign = {"command": "/elsewhere/python", "args": ["/elsewhere/server.py"],
                       "env": {"ACTIVE_EMBEDDING": "arctic"}}
            raw = json.dumps({"mcpServers": {"weaviate-kg": foreign}})
            target.write_text(raw, encoding="utf-8")
            self.assertEqual(strip_unallowlisted_global_env(root, target), [])
            self.assertEqual(target.read_text(encoding="utf-8"), raw)
            self.assertFalse(target.with_suffix(".json.bak").exists())

    def test_no_venv_emits_critical_deferral_and_does_not_crash(self):
        """No venv-python anywhere → soft-fail + critical deferral entry."""
        with tempfile.TemporaryDirectory() as td:
            # Install root with NO .venv at all.
            root = Path(td) / "no_venv_install"
            root.mkdir()
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            report = self._run_with_overrides(
                root, fake_home,
                bundled_binary=False, allow_download=False, allow_cargo=False,
            )
            ids = [e.condition_id for e in report.entries]
            self.assertIn("mcp_registration_no_venv", ids)
            # Critically: no exception bubbled. install completes.

    def test_secrets_never_leak_through_full_pipeline(self):
        """End-to-end: simulate a pre-existing entry with GITHUB_TOKEN in
        the candidate env, run _register_mcps via Python fallback, assert
        the secret does NOT appear in the written ~/.claude.json file.
        """
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            # Pre-seed with a user MCP that contains a token.
            target.write_text(
                json.dumps({
                    "mcpServers": {
                        "my-user-mcp": {
                            "command": "/usr/bin/my-mcp",
                            "env": {"GITHUB_TOKEN": "ghp_user_token_preserved"},
                        }
                    }
                }, indent=2),
                encoding="utf-8",
            )
            self._run_with_overrides(
                root, fake_home,
                bundled_binary=False, allow_download=False, allow_cargo=False,
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            # Critical: orchestrator-written entries must NOT contain GITHUB_TOKEN.
            for orch_name in ("weaviate-kg", "search", "playwright", "mermaid", "excalidraw"):
                env = data["mcpServers"].get(orch_name, {}).get("env", {})
                self.assertNotIn(
                    "GITHUB_TOKEN", env,
                    f"SECURITY: `{orch_name}` env must not contain GITHUB_TOKEN",
                )
            # User's pre-existing entry is preserved (we don't touch
            # entries the user created — only the bundled set is owned
            # by the orchestrator).
            user_env = data["mcpServers"]["my-user-mcp"].get("env", {})
            self.assertEqual(user_env.get("GITHUB_TOKEN"), "ghp_user_token_preserved")


# ─────────────────────────────────────────────────────────────────────────
# Stale-MCP-entry detection
# ─────────────────────────────────────────────────────────────────────────


class StaleMcpDetectionTests(unittest.TestCase):
    """``_detect_stale_mcp_entries`` emits a deferral, never auto-rewrites."""

    def test_emits_deferral_for_stale_path(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "new_install"
            install_root.mkdir()
            stale_root = Path(td) / "old_install"
            stale_root.mkdir()
            target = Path(td) / ".claude.json"
            # Seed a stale entry pointing at a different install path,
            # with the recognisable claude_mcp_servers/ token.
            stale_path = stale_root / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
            target.write_text(
                json.dumps({
                    "mcpServers": {
                        "weaviate-kg": {
                            "command": str(stale_root / ".venv" / "bin" / "python"),
                            "args": [str(stale_path)],
                        }
                    }
                }, indent=2),
                encoding="utf-8",
            )
            report = DeferralReport()
            install._detect_stale_mcp_entries(install_root, target, report)
            ids = [e.condition_id for e in report.entries]
            self.assertIn("stale_mcp_entry", ids)

    def test_no_deferral_when_no_stale(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "new_install"
            install_root.mkdir()
            target = Path(td) / ".claude.json"
            # Entry pointing inside the install_root → not stale.
            inside_path = install_root / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
            target.write_text(
                json.dumps({
                    "mcpServers": {
                        "weaviate-kg": {
                            "command": str(install_root / ".venv" / "bin" / "python"),
                            "args": [str(inside_path)],
                        }
                    }
                }, indent=2),
                encoding="utf-8",
            )
            report = DeferralReport()
            install._detect_stale_mcp_entries(install_root, target, report)
            self.assertEqual(report.entries, [])

    def test_user_mcps_outside_install_root_are_not_flagged(self):
        """An MCP at /usr/bin/foo is the user's own — not orchestrator stale."""
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "new_install"
            install_root.mkdir()
            target = Path(td) / ".claude.json"
            target.write_text(
                json.dumps({
                    "mcpServers": {
                        "my-custom": {
                            "command": "/usr/bin/my-custom-mcp",
                            "args": ["--port", "9999"],
                        }
                    }
                }, indent=2),
                encoding="utf-8",
            )
            report = DeferralReport()
            install._detect_stale_mcp_entries(install_root, target, report)
            self.assertEqual(
                report.entries, [],
                "user MCPs without claude_mcp_servers/ or .venv path tokens "
                "must not be flagged as orchestrator-stale",
            )


if __name__ == "__main__":
    unittest.main()
