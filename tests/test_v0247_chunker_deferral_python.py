# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-7.5: tests for the Python-side chunker-preset deferral
emitted by `install_project_bundle` during per-project bundle updates.

Sibling to the Rust-side launcher hook in
`launcher/src-tauri/src/commands/chunker_revision_deferral.rs` —
both hooks fire in their own update flow (launcher binary swap vs.
per-project `install-bundle --update`), each writing the same
`chunker_preset_overhaul_pending` deferral entry but to the appropriate
project's UPDATE_DEFERRED.md.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vco_lib.project_init import (
    _CHUNKER_BUMP_VERSION,
    _crosses_chunker_boundary,
    _emit_chunker_resync_deferral,
    _parse_semver,
)


# ----------------------------------------------------------------------
# 1. _parse_semver — basic parsing
# ----------------------------------------------------------------------


class TestParseSemver:
    def test_parses_normal_versions(self) -> None:
        assert _parse_semver("0.2.45") == (0, 2, 45)
        assert _parse_semver("0.2.46") == (0, 2, 46)
        assert _parse_semver("1.0.0") == (1, 0, 0)
        assert _parse_semver("10.20.30") == (10, 20, 30)

    def test_rejects_malformed(self) -> None:
        assert _parse_semver("") is None
        assert _parse_semver("0.2") is None
        assert _parse_semver("0.2.46.1") is None
        assert _parse_semver("0.2.x") is None
        assert _parse_semver("0.2.46-dev") is None


# ----------------------------------------------------------------------
# 2. _crosses_chunker_boundary — the gating decision
# ----------------------------------------------------------------------


class TestCrossesBoundary:
    def test_chunker_bump_version_is_0_2_46(self) -> None:
        assert _CHUNKER_BUMP_VERSION == "0.2.46"

    def test_pre_to_post_crosses(self) -> None:
        assert _crosses_chunker_boundary("0.2.45", "0.2.46") is True
        assert _crosses_chunker_boundary("0.2.45", "0.2.47") is True
        assert _crosses_chunker_boundary("0.2.40", "0.3.0") is True

    def test_pre_to_pre_does_not_cross(self) -> None:
        assert _crosses_chunker_boundary("0.2.40", "0.2.45") is False
        assert _crosses_chunker_boundary("0.2.44", "0.2.45") is False

    def test_post_to_post_does_not_cross(self) -> None:
        assert _crosses_chunker_boundary("0.2.46", "0.2.47") is False
        assert _crosses_chunker_boundary("0.2.46", "0.3.0") is False
        assert _crosses_chunker_boundary("0.2.47", "0.2.48") is False

    def test_same_version_does_not_cross(self) -> None:
        # The boundary helper is symmetric in the trivial case.
        assert _crosses_chunker_boundary("0.2.46", "0.2.46") is False
        assert _crosses_chunker_boundary("0.2.45", "0.2.45") is False

    def test_malformed_versions_return_false(self) -> None:
        # Soft-fail: never trigger the deferral on bad input.
        assert _crosses_chunker_boundary("garbage", "0.2.46") is False
        assert _crosses_chunker_boundary("0.2.45", "garbage") is False
        assert _crosses_chunker_boundary("", "0.2.46") is False


# ----------------------------------------------------------------------
# 3. _emit_chunker_resync_deferral — writes the entry
# ----------------------------------------------------------------------


class TestEmitChunkerResyncDeferral:
    def test_writes_deferral_to_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _emit_chunker_resync_deferral(folder, "0.2.45", "0.2.46")
            deferral_path = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            assert deferral_path.exists()
            content = deferral_path.read_text()
            assert "chunker_preset_overhaul_pending" in content
            assert "0.2.45" in content
            assert "0.2.46" in content
            # v0.2.75 (C-10 family fix): the emitted commands must be ones
            # the target CLIs accept — `kg-sync` has no `--force` (its manual
            # argv loop silently ignored it) and the analyzer's argparse
            # REJECTS `--force`; the real drop+rebuild flag is
            # `--force-recreate`. Family guard:
            # tests/test_deferral_command_argparse_sweep.py.
            assert "kg-sync --all" in content
            assert "kg-sync --all --force" not in content
            assert "code-graph-analyze . --force-recreate" in content

    def test_severity_is_info(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _emit_chunker_resync_deferral(folder, "0.2.45", "0.2.46")
            content = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            # Severity hint is part of the deferral_report format.
            assert "info" in content.lower()

    def test_idempotent_within_same_install_run(self) -> None:
        """Calling the emitter twice with the same versions replaces the
        entry rather than appending a duplicate."""
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _emit_chunker_resync_deferral(folder, "0.2.45", "0.2.46")
            content_after_first = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            _emit_chunker_resync_deferral(folder, "0.2.45", "0.2.46")
            content_after_second = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            # Test the section header appears EXACTLY ONCE (the YAML
            # frontmatter and the markdown header both contain the
            # condition_id, but each entry has one ## header in the body).
            assert content_after_second.count("## chunker_preset_overhaul_pending ") == 1
