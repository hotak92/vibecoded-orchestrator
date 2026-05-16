# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-34 (v0.2.13): deprecated-MCP detection and consent-prompted removal.

The key invariants tested here:

* _scan_deprecated_mcp_entries returns entries ONLY when the deprecated
  MCP name is present AND its command path is inside the current install_root.
* Entries with the deprecated name but a command path OUTSIDE install_root
  (user-customised) are never returned — they are left alone.
* _detect_deprecated_mcp_entries emits a deferral for each match.
* _detect_deprecated_mcp_entries is idempotent — the same entry name
  results in the same deferral condition_id (DeferralReport deduplicates).
* _remove_deprecated_mcp_entries requires --remove-deprecated-mcps;
  without that flag, detection fires but removal does not.
* With VCT_REMOVE_DEPRECATED_MCPS=all + flag, deprecated entries inside
  install_root are deleted from ~/.claude.json atomically.
* Non-deprecated user MCPs and other top-level keys survive every path.
* --quiet (non-TTY) with the flag set emits a clarifying deferral and
  does NOT write.
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


_IS_WINDOWS = platform.system().lower().startswith("win")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_pseudo_install_root(tmp_path: Path, name: str = "current_install") -> Path:
    """Minimal install-root with a fake venv-python so path checks resolve.

    Mirrors the helper in test_install_mcp_registration.py.
    """
    root = tmp_path / name
    root.mkdir()
    sub = "Scripts" if _IS_WINDOWS else "bin"
    py_name = "python.exe" if _IS_WINDOWS else "python"
    venv_bin = root / ".venv" / sub
    venv_bin.mkdir(parents=True)
    (venv_bin / py_name).write_text("#!/bin/sh\nexit 0\n")
    if not _IS_WINDOWS:
        (venv_bin / py_name).chmod(0o755)
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
    (root / "claude_mcp_servers" / "search_mcp").mkdir(parents=True)
    return root


def _make_ollama_entry_inside(install_root: Path) -> dict:
    """Return a claude.json mcpServers ollama entry whose command is
    inside install_root (simulates a pre-v0.2.11 registered entry).
    """
    python_path = install_root / ".venv" / ("Scripts" if _IS_WINDOWS else "bin") \
        / ("python.exe" if _IS_WINDOWS else "python")
    server_path = install_root / "claude_mcp_servers" / "ollama_mcp" / "server.py"
    return {
        "type": "stdio",
        "command": str(python_path),
        "args": [str(server_path)],
        "env": {
            "OLLAMA_URL": "http://localhost:11435",
        },
    }


def _make_ollama_entry_outside(other_root: Path) -> dict:
    """Return a claude.json mcpServers ollama entry whose command is
    OUTSIDE install_root (user-customised entry at a different path).
    """
    python_path = other_root / ".venv" / ("Scripts" if _IS_WINDOWS else "bin") \
        / ("python.exe" if _IS_WINDOWS else "python")
    server_path = other_root / "claude_mcp_servers" / "ollama_mcp" / "server.py"
    return {
        "type": "stdio",
        "command": str(python_path),
        "args": [str(server_path)],
        "env": {"OLLAMA_URL": "http://localhost:11435"},
    }


def _seed_claude_json(
    target: Path,
    mcp_servers: dict,
    extra_top_level: dict | None = None,
) -> None:
    """Write a claude.json fixture with the given mcpServers."""
    data: dict = {"mcpServers": mcp_servers}
    if extra_top_level:
        data.update(extra_top_level)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# _scan_deprecated_mcp_entries (pure)
# ---------------------------------------------------------------------------


class ScanDeprecatedEntriesTests(unittest.TestCase):
    """Pure scanner — no side effects, no writes."""

    def test_returns_empty_when_no_deprecated_names_present(self):
        """No deprecated-name entry in the file → empty list."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "weaviate-kg": {
                    "command": str(install_root / ".venv" / "bin" / "python"),
                    "args": ["server.py"],
                }
            })
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(result, [])

    def test_returns_entry_when_ollama_inside_install_root(self):
        """ollama entry with command inside install_root is returned."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(len(result), 1)
            name, matched_path, entry, dep_info = result[0]
            self.assertEqual(name, "ollama")
            self.assertTrue(matched_path.startswith(str(install_root)))
            self.assertIsInstance(entry, dict)
            self.assertIn("removed_in", dep_info)

    def test_user_customised_entry_outside_install_root_not_returned(self):
        """ollama entry whose command is OUTSIDE install_root → left alone."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td), "new_install")
            other_root = Path(td) / "user_custom_ollama"
            other_root.mkdir()
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_outside(other_root),
            })
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(
                result, [],
                "User-customised ollama entry outside install_root must not "
                "be returned — only 'our' deprecated entries should be flagged",
            )

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / "nonexistent.claude.json"
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(result, [])

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            target.write_text("{ invalid json }", encoding="utf-8")
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(result, [])

    def test_entry_with_relative_command_not_inside_root(self):
        """A relative command path (no leading /) is never classified as
        inside install_root — only absolute paths are considered.
        """
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": {
                    "command": "python",  # relative
                    "args": ["server.py"],
                }
            })
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(result, [], "Relative paths must never match install_root")

    def test_dep_info_contains_expected_keys(self):
        """The dep_info dict in the result must contain the registry fields."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            result = install._scan_deprecated_mcp_entries(install_root, target)
            self.assertEqual(len(result), 1)
            _, _, _, dep_info = result[0]
            self.assertIn("removed_in", dep_info)
            self.assertIn("reason", dep_info)
            self.assertIn("opt_in_manifest", dep_info)


# ---------------------------------------------------------------------------
# _detect_deprecated_mcp_entries (deferral emitter)
# ---------------------------------------------------------------------------


class DetectDeprecatedEntriesTests(unittest.TestCase):
    """Detection-only path — emits deferral but never writes."""

    def test_emits_deferral_for_ollama_inside_install_root(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            ids = [e.condition_id for e in report.entries]
            self.assertIn(
                "deprecated_mcp_ollama", ids,
                "Expected 'deprecated_mcp_ollama' deferral entry",
            )

    def test_no_deferral_when_no_deprecated_entries(self):
        """Clean install → no deferral."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "weaviate-kg": {
                    "command": str(install_root / ".venv" / "bin" / "python"),
                    "args": ["server.py"],
                }
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            self.assertEqual(report.entries, [])

    def test_no_deferral_for_user_customised_entry_outside_install_root(self):
        """ollama at a user-controlled path → no deferral."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td), "new_install")
            other_root = Path(td) / "user_custom"
            other_root.mkdir()
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_outside(other_root),
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            self.assertEqual(
                report.entries, [],
                "User-customised ollama entry outside install_root must "
                "not trigger a deferral",
            )

    def test_idempotent_same_condition_id(self):
        """Running detection twice remains idempotent — exactly one entry.

        DeferralReport.add_entry deduplicates by condition_id (last write
        wins). Calling _detect_deprecated_mcp_entries twice must produce
        exactly one entry for 'deprecated_mcp_ollama', not two.
        """
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            install._detect_deprecated_mcp_entries(install_root, target, report)
            # DeferralReport.add_entry deduplicates — exactly one entry.
            ids = [e.condition_id for e in report.entries]
            self.assertEqual(
                ids.count("deprecated_mcp_ollama"), 1,
                "DeferralReport.add_entry deduplicates by condition_id; "
                "running detection twice must not produce duplicate entries",
            )

    def test_deferral_severity_is_info(self):
        """Deprecation is informational, not a hard error."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            entry = next(
                e for e in report.entries if e.condition_id == "deprecated_mcp_ollama"
            )
            self.assertEqual(entry.severity, "info")

    def test_deferral_command_mentions_remove_deprecated_mcps_flag(self):
        """The deferral's command_to_apply must mention the removal flag."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            target = Path(td) / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            report = DeferralReport()
            install._detect_deprecated_mcp_entries(install_root, target, report)
            entry = next(
                e for e in report.entries if e.condition_id == "deprecated_mcp_ollama"
            )
            self.assertIn("--remove-deprecated-mcps", entry.command_to_apply)


# ---------------------------------------------------------------------------
# _remove_deprecated_mcp_entries (consent-prompted removal)
# ---------------------------------------------------------------------------


class RemoveDeprecatedEntriesTests(unittest.TestCase):
    """Consent-prompted removal — only runs with the flag."""

    def _run_removal(
        self,
        install_root: Path,
        fake_home: Path,
        env_override: str = "",
        quiet: bool = False,
        input_answers: list[str] | None = None,
    ) -> DeferralReport:
        """Drive _remove_deprecated_mcp_entries under VCT_USER_HOME_OVERRIDE.

        Patches sys.stdin.isatty → True so that pytest's non-TTY stdin does
        not spuriously trigger the quiet-detection path. Mirrors the same
        pattern used in test_install_rewrite_stale_mcps.py.
        """
        report = DeferralReport()
        answers = iter(input_answers or [])

        def fake_input(_prompt: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                return ""  # default skip when prompts outnumber answers

        env_dict = {
            "VCT_USER_HOME_OVERRIDE": str(fake_home),
            "VCT_REMOVE_DEPRECATED_MCPS": env_override,
        }
        patches = [
            mock.patch.dict(os.environ, env_dict, clear=False),
            # Pretend stdin is a TTY so the quiet detection doesn't
            # spuriously trigger from pytest's redirected stdin.
            mock.patch("sys.stdin.isatty", return_value=True),
        ]
        for p in patches:
            p.start()
        try:
            install._remove_deprecated_mcp_entries(
                install_root,
                report,
                quiet=quiet,
                input_fn=fake_input,
                output_fn=lambda *a, **kw: None,
            )
        finally:
            for p in patches:
                p.stop()
        return report

    def test_auto_removes_when_env_override_all(self):
        """VCT_REMOVE_DEPRECATED_MCPS=all removes the entry without prompt."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
                "weaviate-kg": {
                    "command": str(install_root / ".venv" / "bin" / "python"),
                    "args": ["weaviate_mcp/server.py"],
                },
            })
            report = self._run_removal(
                install_root, fake_home, env_override="all",
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn(
                "ollama", data["mcpServers"],
                "ollama entry must be removed when VCT_REMOVE_DEPRECATED_MCPS=all",
            )
            # Non-deprecated entry is preserved.
            self.assertIn("weaviate-kg", data["mcpServers"])
            ids = [e.condition_id for e in report.entries]
            self.assertIn("deprecated_mcp_removal_summary", ids)

    def test_user_customised_entry_outside_install_root_preserved(self):
        """ollama at a user-controlled path is never touched, even with all."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td), "new_install")
            other_root = Path(td) / "user_custom"
            other_root.mkdir()
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            user_ollama = _make_ollama_entry_outside(other_root)
            _seed_claude_json(target, {"ollama": user_ollama})
            report = self._run_removal(
                install_root, fake_home, env_override="all",
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            # Not in deprecated scan → never touched.
            self.assertIn(
                "ollama", data["mcpServers"],
                "User-customised ollama entry outside install_root must survive",
            )
            ids = [e.condition_id for e in report.entries]
            # No removal summary (nothing was removed).
            self.assertNotIn("deprecated_mcp_removal_summary", ids)

    def test_prompt_y_removes_entry(self):
        """Interactive prompt: 'y' → entry removed."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            report = self._run_removal(
                install_root, fake_home, input_answers=["y"],
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("ollama", data["mcpServers"])
            ids = [e.condition_id for e in report.entries]
            self.assertIn("deprecated_mcp_removal_summary", ids)

    def test_prompt_n_preserves_entry(self):
        """Interactive prompt: 'n' (default) → entry preserved."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            original_json = target.read_text(encoding="utf-8")
            report = self._run_removal(
                install_root, fake_home, input_answers=["n"],
            )
            # File unchanged.
            self.assertEqual(target.read_text(encoding="utf-8"), original_json)
            ids = [e.condition_id for e in report.entries]
            self.assertIn("deprecated_mcp_removal_declined", ids)

    def test_quiet_mode_emits_deferral_and_does_not_write(self):
        """--quiet → no prompt, no write, clarifying deferral emitted."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            original_json = target.read_text(encoding="utf-8")
            report = self._run_removal(
                install_root, fake_home, quiet=True, env_override="",
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                original_json,
                "quiet=True must not modify the file",
            )
            ids = [e.condition_id for e in report.entries]
            self.assertIn("deprecated_mcp_removal_quiet_skipped", ids)

    def test_no_deprecated_entries_is_noop(self):
        """No deprecated entries present → no write, no deferral."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "weaviate-kg": {
                    "command": str(install_root / ".venv" / "bin" / "python"),
                    "args": ["server.py"],
                }
            })
            original_json = target.read_text(encoding="utf-8")
            report = self._run_removal(
                install_root, fake_home, env_override="all",
            )
            self.assertEqual(target.read_text(encoding="utf-8"), original_json)
            self.assertEqual(report.entries, [])

    def test_backup_created_before_write(self):
        """A timestamped backup file is created before removal writes."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
            })
            self._run_removal(
                install_root, fake_home, env_override="all",
            )
            # At least one .bak-depr-remove-* file should exist.
            bak_files = list(fake_home.glob(".claude.json.bak-depr-remove-*"))
            self.assertTrue(
                len(bak_files) >= 1,
                f"Expected a timestamped backup file; found: {list(fake_home.iterdir())}",
            )

    def test_other_top_level_keys_preserved_after_removal(self):
        """Top-level keys like 'permissions' survive the write."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(
                target,
                {"ollama": _make_ollama_entry_inside(install_root)},
                extra_top_level={"permissions": {"allow": ["Read", "Edit"]}},
            )
            self._run_removal(
                install_root, fake_home, env_override="all",
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                data["permissions"]["allow"], ["Read", "Edit"],
                "Top-level keys must survive the deprecated-MCP removal write",
            )

    def test_user_non_deprecated_mcp_entries_preserved(self):
        """Non-deprecated mcpServers entries survive the write."""
        with tempfile.TemporaryDirectory() as td:
            install_root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(install_root),
                "my-user-mcp": {
                    "command": "/usr/bin/my-custom-mcp",
                    "env": {"MY_TOKEN": "preserved"},
                },
            })
            self._run_removal(
                install_root, fake_home, env_override="all",
            )
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("ollama", data["mcpServers"])
            self.assertIn("my-user-mcp", data["mcpServers"])
            self.assertEqual(
                data["mcpServers"]["my-user-mcp"]["env"]["MY_TOKEN"], "preserved"
            )


# ---------------------------------------------------------------------------
# Integration: --remove-deprecated-mcps does NOT fire without the flag
# ---------------------------------------------------------------------------


class DeprecatedMcpFlagNotSetTests(unittest.TestCase):
    """Detection fires (via _register_mcps) but removal does NOT run
    unless --remove-deprecated-mcps is explicitly passed.
    """

    def test_detection_runs_inside_register_mcps(self):
        """_detect_deprecated_mcp_entries is called by _register_mcps.

        This test verifies the integration: after _register_mcps, a
        deprecated ollama entry inside install_root produces a
        'deprecated_mcp_ollama' deferral in the report.
        """
        with tempfile.TemporaryDirectory() as td:
            root = _make_pseudo_install_root(Path(td))
            fake_home = Path(td) / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".claude.json"
            _seed_claude_json(target, {
                "ollama": _make_ollama_entry_inside(root),
            })
            report = DeferralReport()
            with mock.patch.dict(
                os.environ, {"VCT_USER_HOME_OVERRIDE": str(fake_home)}, clear=False
            ):
                with mock.patch.object(install, "_try_bundled_launcher_binary", return_value=None), \
                     mock.patch.object(install, "_try_download_launcher_binary", return_value=None), \
                     mock.patch.object(install, "_try_cargo_tauri_build", return_value=None):
                    install._register_mcps(root, report)
            ids = [e.condition_id for e in report.entries]
            self.assertIn(
                "deprecated_mcp_ollama", ids,
                "_register_mcps must trigger deprecated-MCP detection",
            )


if __name__ == "__main__":
    unittest.main()
