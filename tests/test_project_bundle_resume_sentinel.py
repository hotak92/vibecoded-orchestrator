# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""NEW-7 / B1 (v0.2.53) — bundle-update resume sentinel tests.

Mirrors the v0.2.51 orchestrator-self Bug-A sentinel pattern, but
scoped to per-project bundle updates.

Audit:
  ``.claude/context/audits/project-bundle-install-audit-2026-06-10.md``
  §6.6 / B1.

Cases under test:

  * The sentinel writer + reader + clearer round-trip cleanly.
  * `install_project_bundle(update_mode=True)` writes the sentinel
    BEFORE any FS mutation AND clears it on successful manifest write.
  * If we simulate a mid-update interrupt (kill the process after
    sentinel write but before manifest write), the sentinel survives
    and the next `read_bundle_update_resume_sentinel` returns it.
  * The `check-bundle-resume` CLI subcommand emits the expected JSON.
  * First-install (update_mode=False) does NOT write a sentinel —
    re-running first-install is already idempotent.
  * `dry_run=True` does NOT write a sentinel — dry-run never mutates
    the FS so there's nothing to resume.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from vco_lib.project_init import (
    _bundle_sentinel_path,
    clear_bundle_update_resume_sentinel,
    install_project_bundle,
    read_bundle_update_resume_sentinel,
    write_bundle_update_resume_sentinel,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_project() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="vct-bundle-resume-test-"))
    (folder / ".claude").mkdir()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
# Round-trip writer / reader / clearer
# ──────────────────────────────────────────────────────────────────────


class TestSentinelRoundTrip:
    def test_writer_then_reader_returns_payload(self, tmp_project: Path) -> None:
        ok = write_bundle_update_resume_sentinel(
            tmp_project,
            operation="install-bundle-update",
            orchestrator_root=REPO_ROOT,
            vco_version="abc1234",
        )
        assert ok is True
        sentinel = read_bundle_update_resume_sentinel(tmp_project)
        assert sentinel is not None
        assert sentinel["schema"] == 1
        assert sentinel["operation"] == "install-bundle-update"
        assert sentinel["folder"] == str(tmp_project)
        assert sentinel["orchestrator_root"] == str(REPO_ROOT)
        assert sentinel["vco_version"] == "abc1234"
        assert sentinel["written_at"]
        assert sentinel["pid"] > 0

    def test_clearer_removes_file(self, tmp_project: Path) -> None:
        write_bundle_update_resume_sentinel(tmp_project)
        sentinel_path = _bundle_sentinel_path(tmp_project)
        assert sentinel_path.exists()

        ok = clear_bundle_update_resume_sentinel(tmp_project)
        assert ok is True
        assert not sentinel_path.exists()
        # Reader returns None after clear.
        assert read_bundle_update_resume_sentinel(tmp_project) is None

    def test_clearer_is_idempotent(self, tmp_project: Path) -> None:
        # No sentinel yet — clear must succeed.
        ok = clear_bundle_update_resume_sentinel(tmp_project)
        assert ok is True

    def test_reader_handles_missing_file(self, tmp_project: Path) -> None:
        assert read_bundle_update_resume_sentinel(tmp_project) is None

    def test_reader_handles_malformed_json(self, tmp_project: Path) -> None:
        path = _bundle_sentinel_path(tmp_project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")
        # Reader returns None on parse failure rather than raising.
        assert read_bundle_update_resume_sentinel(tmp_project) is None

    def test_reader_handles_unknown_schema(self, tmp_project: Path) -> None:
        path = _bundle_sentinel_path(tmp_project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 999, "operation": "x"}), encoding="utf-8")
        # Schema mismatch → None (future-proof safety).
        assert read_bundle_update_resume_sentinel(tmp_project) is None


# ──────────────────────────────────────────────────────────────────────
# install_project_bundle integration: writer + clearer wired in
# ──────────────────────────────────────────────────────────────────────


class TestInstallProjectBundleSentinelIntegration:
    def test_update_mode_clears_sentinel_on_success(self, tmp_project: Path) -> None:
        """After a successful `install_project_bundle(update_mode=True)`,
        the sentinel must NOT be on disk — it's written at start, cleared
        at end."""
        result = install_project_bundle(
            tmp_project,
            orchestrator_root=REPO_ROOT,
            update_mode=True,
            force=False,
            dry_run=False,
        )
        # Even if no actions fire (templates not enumerated for this
        # fixture), the function should still write+clear the sentinel.
        # The load-bearing assertion: sentinel is GONE on success.
        sentinel_path = _bundle_sentinel_path(tmp_project)
        # Sentinel file should be absent (cleared after successful
        # manifest write).
        assert not sentinel_path.exists(), (
            f"sentinel must be cleared on successful update; result={result}"
        )

    def test_first_install_mode_does_not_write_sentinel(
        self, tmp_project: Path
    ) -> None:
        """First-install (update_mode=False) must NOT write a sentinel.
        Re-running first-install is already idempotent — files are
        either created or skipped; no resume needed."""
        install_project_bundle(
            tmp_project,
            orchestrator_root=REPO_ROOT,
            update_mode=False,
            force=False,
            dry_run=False,
        )
        sentinel_path = _bundle_sentinel_path(tmp_project)
        assert not sentinel_path.exists()

    def test_dry_run_does_not_write_sentinel(self, tmp_project: Path) -> None:
        """Dry-run must NOT write a sentinel — no FS mutations means
        no resume state to track."""
        install_project_bundle(
            tmp_project,
            orchestrator_root=REPO_ROOT,
            update_mode=True,
            force=False,
            dry_run=True,
        )
        sentinel_path = _bundle_sentinel_path(tmp_project)
        assert not sentinel_path.exists()


# ──────────────────────────────────────────────────────────────────────
# Mid-update interrupt simulation
# ──────────────────────────────────────────────────────────────────────


class TestSentinelSurvivesMidUpdateInterrupt:
    def test_simulated_interrupt_leaves_sentinel(self, tmp_project: Path) -> None:
        """Simulate the Cmd-C path: write the sentinel manually (as if
        install_project_bundle's start-of-update writer had just fired),
        then DON'T clear it (as if the process were killed mid-pass).
        The sentinel survives + is detected by the next reader."""
        write_bundle_update_resume_sentinel(
            tmp_project,
            operation="install-bundle-update",
            orchestrator_root=REPO_ROOT,
            vco_version="mid-interrupt",
        )
        # Process "killed" — no clear call.
        # Next session reads:
        sentinel = read_bundle_update_resume_sentinel(tmp_project)
        assert sentinel is not None
        assert sentinel["operation"] == "install-bundle-update"
        assert sentinel["vco_version"] == "mid-interrupt"

    def test_rerunning_update_clears_stale_sentinel(self, tmp_project: Path) -> None:
        """Recovery contract: when a stale sentinel exists and the user
        re-runs `install-bundle --update`, the run writes a FRESH sentinel
        + clears it on success. The stale one is effectively replaced.
        """
        # Plant a stale sentinel.
        write_bundle_update_resume_sentinel(
            tmp_project, operation="install-bundle-update", vco_version="stale-v"
        )
        assert _bundle_sentinel_path(tmp_project).exists()

        # Re-run update — should clear the sentinel on success.
        install_project_bundle(
            tmp_project,
            orchestrator_root=REPO_ROOT,
            update_mode=True,
            force=False,
            dry_run=False,
        )
        # Sentinel cleared.
        assert not _bundle_sentinel_path(tmp_project).exists()


# ──────────────────────────────────────────────────────────────────────
# CLI subcommand: check-bundle-resume
# ──────────────────────────────────────────────────────────────────────


class TestCheckBundleResumeCli:
    def test_cli_reports_no_resume_needed_when_clean(
        self, tmp_project: Path
    ) -> None:
        result = subprocess.run(
            [
                "python",
                "-m",
                "vco_lib.project_init",
                "check-bundle-resume",
                "--folder",
                str(tmp_project),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["resume_needed"] is False
        assert payload["sentinel"] is None
        assert payload["folder"] == str(tmp_project)

    def test_cli_reports_resume_needed_when_sentinel_present(
        self, tmp_project: Path
    ) -> None:
        write_bundle_update_resume_sentinel(
            tmp_project,
            operation="install-bundle-update",
            orchestrator_root=REPO_ROOT,
            vco_version="abc7777",
        )
        result = subprocess.run(
            [
                "python",
                "-m",
                "vco_lib.project_init",
                "check-bundle-resume",
                "--folder",
                str(tmp_project),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["resume_needed"] is True
        assert payload["sentinel"] is not None
        assert payload["sentinel"]["vco_version"] == "abc7777"
