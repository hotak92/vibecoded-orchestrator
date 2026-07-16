# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F8: user_secret_values_retained_in_tree self-clear.

Pre-.83 the S-8 `user_secret_values_retained_in_tree` deferral was described as
"self-clearing" but the reconciler did NOT cover it — a scrubbed tree left the
stale entry on disk forever. B-F8 adds `still_user_secret_retained` to
`_reconcile_bundle_deferrals` (recomputed from the SAME scan the emitter uses),
so once the next env-projection refresh scrubs the value, the entry clears.

PIN: a detection-clean run clears the on-disk entry; a still-dirty run keeps it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0283_deferral_emit_fake import (  # noqa: E402
    install_fake_deferral_emit,
    read_auto_resolutions,
)

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402
from vco_lib.config_projection import (  # noqa: E402
    CLAUDE_ENV_MANAGED_BEGIN,
    CLAUDE_ENV_MANAGED_END,
)
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


def _seed_secret_deferral(folder: Path) -> None:
    report = DeferralReport.read(folder)
    report.add_entry(DeferralEntry(
        condition_id="user_secret_values_retained_in_tree",
        title="pre-.83 secret retention",
        detected="secret-shaped line found",
        why_deferred="one-time",
        command_to_apply="noop",
        severity="warning",
    ))
    report.write(folder)


# The self-clear is exercised on the settings.json surface because its
# secret-shape detector (`is_secret_shaped_env_key`) precisely distinguishes a
# real secret key (STALE_SECRET) from a routing key (KG_COLLECTION). The
# `.claude/env` surface uses a COARSE regex that flags ANY managed-block
# `export KEY="..."` line (a pre-existing S-8 property), so a routing-only env
# never reads "clean" there — the settings surface is the realistic self-clear
# signal and is what these tests drive.
def _write_dirty_settings(folder: Path) -> None:
    claude = folder / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(
        json.dumps({
            "env": {
                "KG_COLLECTION": "Test_KnowledgeGraph",
                "STALE_SECRET": "synthetic-value",  # secret-shaped
            }
        }, indent=2),
        encoding="utf-8",
    )


def _write_clean_settings(folder: Path) -> None:
    claude = folder / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(
        json.dumps({
            "env": {
                "KG_COLLECTION": "Test_KnowledgeGraph",  # routing key only
            }
        }, indent=2),
        encoding="utf-8",
    )


def _write_dirty_env(folder: Path) -> None:
    """Dirty on the `.claude/env` surface (any managed-block export trips it)."""
    claude = folder / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "env").write_text(
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        'export STALE_SECRET="synthetic-value"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n",
        encoding="utf-8",
    )


class UserSecretSelfClearTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-b2-secret-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_scan_detects_dirty_and_clean_settings(self):
        _write_dirty_settings(self.tmp)
        self.assertTrue(project_init._scan_user_secret_values_retained(self.tmp))
        _write_clean_settings(self.tmp)
        self.assertFalse(project_init._scan_user_secret_values_retained(self.tmp))

    def test_scan_detects_dirty_env_surface(self):
        """The `.claude/env` managed-block export also trips the scan."""
        _write_dirty_env(self.tmp)
        self.assertTrue(project_init._scan_user_secret_values_retained(self.tmp))

    def test_act_clean_tree_self_clears_entry(self):
        """PIN: detection-clean run clears the on-disk entry (via the new
        reconciler flag, probed from disk)."""
        _seed_secret_deferral(self.tmp)
        _write_clean_settings(self.tmp)  # secret value already scrubbed

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("user_secret_values_retained_in_tree"),
            "a scrubbed tree must clear the stale secret-retention deferral",
        )
        # B-F9: auto-resolution recorded.
        rows = read_auto_resolutions(self.tmp)
        self.assertTrue(
            any(
                r["condition_id"] == "user_secret_values_retained_in_tree"
                for r in rows
            ),
            f"expected a self-clear auto-resolution row; got {rows}",
        )

    def test_leave_alone_dirty_tree_keeps_entry(self):
        """LEAVE-ALONE: a still-dirty tree keeps the entry."""
        _seed_secret_deferral(self.tmp)
        _write_dirty_settings(self.tmp)  # secret value still present

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertTrue(
            report.has_condition("user_secret_values_retained_in_tree"),
            "a still-dirty tree must keep the secret-retention deferral",
        )

    def test_explicit_flag_overrides_probe(self):
        """An explicit still_user_secret_retained=True keeps the entry even
        when the tree is clean (mirrors the stale-wrapper override contract)."""
        _seed_secret_deferral(self.tmp)
        _write_clean_settings(self.tmp)

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
            still_user_secret_retained=True,
        )
        report = DeferralReport.read(self.tmp)
        self.assertTrue(
            report.has_condition("user_secret_values_retained_in_tree"),
            "explicit still_user_secret_retained=True must override the probe",
        )

    def test_no_entry_present_probe_not_run(self):
        """When the entry is not on disk, the reconciler must not touch it (and
        the absence of the entry means nothing to clear)."""
        # No seed, dirty tree — nothing to reconcile.
        _write_dirty_settings(self.tmp)
        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("user_secret_values_retained_in_tree")
        )


if __name__ == "__main__":
    unittest.main()
