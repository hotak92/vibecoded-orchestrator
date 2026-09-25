# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — ``legacy_bash_env_cleanup_pending`` clears itself.

It was ``manual-dismiss``: once the legacy lean-ctx ``BASH_ENV`` pointer was
gone from ``.claude/settings.json`` the entry still stood until someone
dismissed it. It now declares ``probe:py:bash_env_cleanup_still_owed``, keyed
on the file's STATE with the cleanup's own read and shim rule: gone ⇒ clear,
still pointing or unreadable ⇒ keep.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vco_lib import deferral_emit, deferral_probes, deferral_registry, doctor  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402

CID = "legacy_bash_env_cleanup_pending"
PROBE = "bash_env_cleanup_still_owed"
SHIM = "${CLAUDE_PROJECT_DIR}/.claude/scripts/leanctx-bash-env.sh"


def _settings(folder: Path, text: str) -> Path:
    path = folder / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _verdict(folder: Path):
    return deferral_probes.run_probe(
        PROBE, deferral_probes.ProbeContext(folder=folder, entry=None, extras={}))


def test_the_row_declares_the_state_keyed_probe():
    assert deferral_registry.clear_probe_for(CID) == f"probe:py:{PROBE}"
    assert PROBE in deferral_probes.PROBES


@pytest.mark.parametrize("text,expected", [
    (None, False),                                                    # no file
    (json.dumps({"env": {"KEEP": "1"}}), False),                      # key gone
    (json.dumps({"env": {"BASH_ENV": "/opt/mine.sh"}}), False),       # not the shim
    ('// note\n{"env": {"BASH_ENV": "' + SHIM + '"},}\n', True),     # JSONC, still there
    (json.dumps({"env": {"BASH_ENV": SHIM}}), True),                  # still there
    ('{ "env": { "BASH_ENV": ', True),                                # unreadable
])
def test_the_probe_is_keyed_on_the_file_state(tmp_path, text, expected):
    if text is not None:
        _settings(tmp_path, text)
    assert _verdict(tmp_path) is expected


def _seed(folder: Path) -> None:
    deferral_emit.emit(folder, DeferralEntry(
        condition_id=CID, title="t", detected="d", why_deferred="w",
        command_to_apply="c", severity="warning"))


def _cids(folder: Path) -> set:
    return {e.condition_id for e in DeferralReport.read(folder).entries}


def test_the_reconcile_clears_the_entry_once_the_key_is_gone(tmp_path):
    """ACT. RED before: manual-dismiss, so the reconcile never touched it."""
    _settings(tmp_path, json.dumps({"env": {"KEEP": "1"}}))
    _seed(tmp_path)
    cleared = doctor.reconcile_probe_cleared(tmp_path, log=lambda _l: None)
    assert CID in cleared and CID not in _cids(tmp_path)


def test_the_reconcile_keeps_the_entry_while_the_pointer_stands(tmp_path):
    """KEEP: the fork-bomb pointer is still in the file."""
    _settings(tmp_path, json.dumps({"env": {"BASH_ENV": SHIM}}))
    _seed(tmp_path)
    assert CID not in doctor.reconcile_probe_cleared(tmp_path, log=lambda _l: None)
    assert CID in _cids(tmp_path)


def test_a_cleanup_that_now_succeeds_clears_in_the_same_pass(tmp_path):
    """The cleanup runs before every re-probe pass: once it can edit the file
    it removes the key, and the probe then reads the entry as over."""
    _settings(tmp_path, json.dumps({"env": {"BASH_ENV": SHIM}}))
    _seed(tmp_path)
    assert project_init._cleanup_legacy_bash_env_in_project(tmp_path)["action"] == "removed"
    assert CID in doctor.reconcile_probe_cleared(tmp_path, log=lambda _l: None)
