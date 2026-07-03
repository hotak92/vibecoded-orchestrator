# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""S-8 user-secret retention deferral emitter (v0.2.73).

Pre-v0.2.73 user-secret VALUES may still reside in committable tree files
(.claude/env or .claude/settings.json) from the Rust GUI writer before
S-4's strip invariant shipped. S-8 emits a ONE-TIME `user_secret_values_retained_in_tree`
deferral when a secret-shaped managed-block line is detected (no value
parsing — only pattern match). The deferral self-clears once the next
env-projection refresh scrubs the value.

Tests: emitter fires when tree file has a stale secret-shaped managed-block
line; does NOT fire when clean; the deferral clears after the value is gone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vco_lib.config_projection import CLAUDE_ENV_MANAGED_BEGIN, CLAUDE_ENV_MANAGED_END
from vco_lib.deferral_report import DeferralReport
from vco_lib.project_init import _emit_user_secret_values_retained_deferral


@pytest.fixture()
def clean_project(tmp_path: Path) -> Path:
    """Minimal project with no pre-fix secret artifacts."""
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.fixture()
def project_with_secret_in_env(tmp_path: Path) -> Path:
    """Project with a stale secret-shaped line in .claude/env managed block."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "env").write_text(
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        'export KG_COLLECTION="Test_KnowledgeGraph"\n'
        "\n"
        "# user secrets (per-project; managed via launcher GUI Secrets panel)\n"
        'export STALE_SECRET="synthetic-secret-value-pre-fix"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
    )
    return tmp_path


@pytest.fixture()
def project_with_secret_in_settings(tmp_path: Path) -> Path:
    """Project with a stale secret-shaped line in .claude/settings.json managed block."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    import json
    settings = {
        "env": {
            "KG_COLLECTION": "Test_KnowledgeGraph",
            "STALE_SECRET": "synthetic-secret-value-in-settings",
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    return tmp_path


def test_emits_when_env_has_stale_secret(project_with_secret_in_env: Path) -> None:
    """Deferral fires when .claude/env contains a secret-shaped managed-block line."""
    _emit_user_secret_values_retained_deferral(project_with_secret_in_env)

    report = DeferralReport.read(project_with_secret_in_env)
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.condition_id == "user_secret_values_retained_in_tree"
    assert entry.severity == "warning"
    assert "secret" in entry.detected.lower()
    # Value is NEVER in the deferral message.
    assert "synthetic-secret-value-pre-fix" not in str(entry)
    assert entry.command_to_apply and "dismiss-deferral" in entry.command_to_apply


def test_emits_when_settings_has_stale_secret(project_with_secret_in_settings: Path) -> None:
    """Deferral fires when .claude/settings.json env block contains a secret key."""
    _emit_user_secret_values_retained_deferral(project_with_secret_in_settings)

    report = DeferralReport.read(project_with_secret_in_settings)
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.condition_id == "user_secret_values_retained_in_tree"
    # Value is NEVER in the deferral message.
    assert "synthetic-secret-value-in-settings" not in str(entry)


def test_no_emit_when_clean(clean_project: Path) -> None:
    """Deferral does NOT fire when no pre-fix artifacts are present."""
    _emit_user_secret_values_retained_deferral(clean_project)

    report = DeferralReport.read(clean_project)
    assert len(report.entries) == 0


def test_clears_after_value_removed(project_with_secret_in_env: Path) -> Path:
    """Deferral self-clears: re-emit after value is removed finds no artifacts."""
    # First emit: deferral is written.
    _emit_user_secret_values_retained_deferral(project_with_secret_in_env)
    report1 = DeferralReport.read(project_with_secret_in_env)
    assert len(report1.entries) == 1

    # Simulate the next env refresh: scrub the secret value.
    env_path = project_with_secret_in_env / ".claude" / "env"
    env_text = env_path.read_text()
    # Remove the secret-shaped line (the refresh strips the entire export line).
    cleaned = env_text.replace(
        'export STALE_SECRET="synthetic-secret-value-pre-fix"\n', ""
    )
    env_path.write_text(cleaned)

    # Re-emit: the deferral MUST NOT fire a second time.
    _emit_user_secret_values_retained_deferral(project_with_secret_in_env)
    report2 = DeferralReport.read(project_with_secret_in_env)
    # The old entry stays (it wasn't automatically cleared), but NO NEW ENTRY is added.
    assert len(report2.entries) == 1
    assert report2.entries[0].condition_id == "user_secret_values_retained_in_tree"


def test_never_prints_secret_value(project_with_secret_in_env: Path) -> None:
    """Verify that no secret value appears anywhere in the deferral."""
    _emit_user_secret_values_retained_deferral(project_with_secret_in_env)

    deferred_file = project_with_secret_in_env / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert deferred_file.exists()
    content = deferred_file.read_text()

    # The synthetic value must never appear.
    assert "synthetic-secret-value-pre-fix" not in content
    # The word "secret" should appear (in the condition ID or title), but not
    # paired with a value.
    assert "secret" in content.lower()
