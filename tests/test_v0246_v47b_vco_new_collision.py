"""v0.2.46 post-adversarial L1 — `.vco-new` collision detection tests.

Pins the behavior of ``vco_lib.symlink_handler.check_vco_new_collision`` +
its wiring into ``install.py::_configure_claude_settings``.

Adversarial review S4 surfaced: if a user hand-edited a ``.vco-new``
sibling (e.g., ``.claude/agents.vco-new`` or ``.claude/settings.json.vco-new``)
between install runs, the next install would silently clobber it via
``shutil.copy2`` / ``Path.write_text`` with ``exist_ok=True``. No warning,
no recovery hint.

The L1 fix:
  1. ``check_vco_new_collision(path, install_root, deferral)`` returns True
     iff the slot is already occupied (``os.path.lexists``). Optionally
     emits a structured ``UPDATE_DEFERRED.md`` entry.
  2. Callers check the result and SKIP the write when True.
  3. The user sees a one-line message + deferral with reconciliation
     commands.

This test file covers BOTH the helper (unit) and the
``_configure_claude_settings`` integration (one representative caller
of many — wiring more sites is queued in v0.2.47 if the pattern
turns out to bite users).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from vco_lib import symlink_handler
from vco_lib.deferral_report import DeferralReport


# ---------------------------------------------------------------------------
# Unit tests — check_vco_new_collision helper
# ---------------------------------------------------------------------------


class TestCheckVcoNewCollision:
    """Pure-function behavior of the new helper."""

    def test_returns_false_when_path_does_not_exist(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json.vco-new"
        assert symlink_handler.check_vco_new_collision(target) is False

    def test_returns_true_when_file_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json.vco-new"
        target.write_text("prior-run content")
        assert symlink_handler.check_vco_new_collision(target) is True

    def test_returns_true_when_directory_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "agents.vco-new"
        target.mkdir()
        (target / "some-file.md").write_text("x")
        assert symlink_handler.check_vco_new_collision(target) is True

    def test_returns_true_for_dangling_symlink_at_slot(self, tmp_path: Path) -> None:
        """A symlink at the slot — even one pointing to a missing target —
        counts as "slot occupied". ``lexists`` (not ``exists``) is the
        right primitive: ``exists`` would return False for a dangling
        link and we'd silently overwrite the user's symlink.
        """
        target = tmp_path / "agents.vco-new"
        try:
            os.symlink(tmp_path / "non-existent-target", target)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation requires admin / dev-mode on this platform")
        assert symlink_handler.check_vco_new_collision(target) is True

    def test_no_deferral_when_report_none(self, tmp_path: Path) -> None:
        """Optional deferral arg: when None, helper still returns the bool
        but emits nothing. Callers that don't care about UPDATE_DEFERRED
        formatting (e.g., a dry-run probe) can skip the boilerplate."""
        target = tmp_path / "collision.vco-new"
        target.write_text("prior")
        assert symlink_handler.check_vco_new_collision(target, deferral=None) is True

    def test_emits_deferral_entry_when_report_given(self, tmp_path: Path) -> None:
        target = tmp_path / "collision.vco-new"
        target.write_text("prior")
        report = DeferralReport()
        result = symlink_handler.check_vco_new_collision(
            target,
            install_root=tmp_path,
            deferral=report,
        )
        assert result is True
        # One entry should have been added with the stable condition_id.
        entries = report.entries
        assert len(entries) == 1
        entry = entries[0]
        assert entry.condition_id == symlink_handler.VCO_NEW_COLLISION_CONDITION_ID
        # Severity is info — VCO did the safe thing.
        assert entry.severity == "info"
        # Reconciliation commands name BOTH escape hatches.
        assert "rm -rf" in entry.command_to_apply
        assert "kept-by-user" in entry.command_to_apply

    def test_deferral_uses_relative_path_when_install_root_provided(
        self, tmp_path: Path,
    ) -> None:
        # Build a nested structure so relative-path display is meaningful.
        install_root = tmp_path / "myproject"
        (install_root / ".claude").mkdir(parents=True)
        target = install_root / ".claude" / "settings.json.vco-new"
        target.write_text("prior")
        report = DeferralReport()
        symlink_handler.check_vco_new_collision(
            target,
            install_root=install_root,
            deferral=report,
        )
        entry = report.entries[0]
        # Display string should be the relative path, not absolute.
        assert ".claude/settings.json.vco-new" in entry.detected
        # Absolute path should NOT leak (helps deferred-report portability).
        assert str(install_root) not in entry.detected

    def test_deferral_falls_back_to_absolute_when_path_outside_install_root(
        self, tmp_path: Path,
    ) -> None:
        # vco_new is outside the install_root — relative_to() would raise.
        install_root = tmp_path / "myproject"
        install_root.mkdir()
        target = tmp_path / "elsewhere" / "stray.vco-new"
        target.parent.mkdir()
        target.write_text("prior")
        report = DeferralReport()
        symlink_handler.check_vco_new_collision(
            target,
            install_root=install_root,
            deferral=report,
        )
        entry = report.entries[0]
        # Falls back to absolute when relative_to() would have raised.
        assert str(target) in entry.detected


# ---------------------------------------------------------------------------
# Integration — _configure_claude_settings caller wiring
# ---------------------------------------------------------------------------


class TestConfigureClaudeSettingsCollisionWiring:
    """End-to-end: when called against a project where a prior run left a
    `.claude.vco-new/` or `.claude/settings.json.vco-new`, the function
    must refuse to clobber.
    """

    def _load_install_py_module(self) -> object:
        """Import install.py as a module without re-running argparse."""
        install_path = Path(__file__).resolve().parent.parent / "install.py"
        spec = importlib.util.spec_from_file_location(
            "install_py_v47b_l1", install_path,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["install_py_v47b_l1"] = module
        spec.loader.exec_module(module)
        return module

    def test_settings_file_collision_when_settings_json_is_symlink(
        self, tmp_path: Path,
    ) -> None:
        """`.claude/settings.json` is a symlink AND a prior
        `settings.json.vco-new` exists → function refuses to overwrite
        the prior `.vco-new`, returns silently.
        """
        install_py = self._load_install_py_module()

        # Build the fixture.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # settings.json is a symlink (so the function takes the .vco-new path).
        settings_file = claude_dir / "settings.json"
        target = tmp_path / "elsewhere-settings.json"
        target.write_text('{"external": true}')
        try:
            os.symlink(target, settings_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation requires admin / dev-mode on this platform")

        # Prior run's .vco-new sibling, hand-edited by user.
        prior_vco_new = claude_dir / "settings.json.vco-new"
        prior_content = "user-hand-edited-content-from-prior-run"
        prior_vco_new.write_text(prior_content)

        # Patch PROJECT_ROOT to our temp dir so install.py writes there.
        with mock.patch.object(install_py, "PROJECT_ROOT", tmp_path):
            embed_config = {
                "text_model": "qwen3-embedding:0.6b",
                "text_dims": 1024,
                "code_backend": "ollama",
                "code_model": "qwen3-embedding:0.6b",
                "code_dims": 1024,
                "active_embedding": "qwen3",
            }
            report = DeferralReport()
            install_py._configure_claude_settings(
                embed_config,
                adopt_project_mode=None,
                deferral_report=report,
            )

        # The prior .vco-new file's content MUST be byte-identical.
        assert prior_vco_new.read_text() == prior_content, (
            "Prior .vco-new content was overwritten — L1 guard failed."
        )
        # A collision deferral entry should have been recorded.
        entries = [e for e in report.entries
                   if e.condition_id == symlink_handler.VCO_NEW_COLLISION_CONDITION_ID]
        assert len(entries) == 1
