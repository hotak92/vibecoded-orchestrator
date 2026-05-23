# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the `dismiss-deferral` argparse subcommand (v0.2.31 item #21).

Resolves the silent breakage where four deferral-emission paths in
`vco_lib/project_init.py` instructed users to run
`python -m vco_lib.project_init dismiss-deferral` but the subcommand was
never registered with argparse. Each test drives `_cmd_dismiss_deferral`
directly (the `func` callback bound by `_build_arg_parser`) so we cover
both the JSON envelope and the human-readable stderr surface.

Coverage:
  * Happy path — the matching entry is removed; remaining entries (if
    any) survive; an empty file is unlinked from disk.
  * Idempotent re-run — second invocation against an already-dismissed
    condition_id exits 0 with `dismissed: false` / reason `no_match`.
  * No file — running on a folder without UPDATE_DEFERRED.md exits 0
    with `dismissed: false` / reason `no_deferrals_file`.
  * Multi-deferral filtering — only the matched condition_id is
    removed; siblings stay in place.
  * Multi-deferral, no match — file unchanged, exit 0, `dismissed: false`.
  * Malformed file — frontmatter advertises condition_ids but body
    contains no parseable entry sections → exit 1 with a clear stderr
    error message.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
    _DEFERRED_REL,
)
from vco_lib.project_init import _cmd_dismiss_deferral  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_entry(condition_id: str, *, title: str | None = None) -> DeferralEntry:
    """Build a minimal DeferralEntry suitable for round-tripping through
    DeferralReport.write/read."""
    return DeferralEntry(
        condition_id=condition_id,
        title=title or condition_id.replace("_", " ").title(),
        detected=f"Synthetic detection prose for {condition_id}.",
        why_deferred="Test fixture — no real reason.",
        command_to_apply=f"python -m vco_lib.project_init dismiss-deferral "
                         f"--folder /tmp/test --condition-id {condition_id}",
        severity="info",
        kg_node_refs=[],
        detected_at="2026-05-23T12:00:00Z",
    )


def _seed_deferrals(folder: Path, condition_ids: list[str]) -> Path:
    """Write a UPDATE_DEFERRED.md containing one entry per id in order."""
    report = DeferralReport()
    for cid in condition_ids:
        report.add_entry(_make_entry(cid))
    report.write(folder)
    target = folder / _DEFERRED_REL
    assert target.exists(), "fixture failed to seed deferrals file"
    return target


def _make_args(folder: Path, condition_id: str, *, json_mode: bool = True) -> argparse.Namespace:
    """Build the argparse.Namespace shape `_cmd_dismiss_deferral` consumes."""
    return argparse.Namespace(
        folder=str(folder),
        condition_id=condition_id,
        json=json_mode,
    )


def _run(
    args: argparse.Namespace,
    capsys: pytest.CaptureFixture[str],
) -> Tuple[int, str, str]:
    """Invoke the handler and return (exit_code, stdout, stderr)."""
    exit_code = _cmd_dismiss_deferral(args)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path_single_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """File contains exactly one matching entry → removed, file unlinked."""
    target = _seed_deferrals(tmp_path, ["bundle_user_modified_preserved"])

    exit_code, out, err = _run(
        _make_args(tmp_path, "bundle_user_modified_preserved"),
        capsys,
    )

    assert exit_code == 0
    payload = json.loads(out)
    assert payload == {
        "dismissed": True,
        "condition_id": "bundle_user_modified_preserved",
        "remaining": 0,
        "reason": "dismissed",
    }
    # DeferralReport.write deletes the file when no entries remain.
    assert not target.exists(), "single-entry dismiss should unlink the file"


def test_idempotent_rerun(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Second invocation against an already-dismissed id exits 0."""
    _seed_deferrals(tmp_path, ["bundle_user_modified_preserved"])

    # First call: dismisses.
    first_exit, _, _ = _run(
        _make_args(tmp_path, "bundle_user_modified_preserved"),
        capsys,
    )
    assert first_exit == 0

    # Second call: no file now → no_deferrals_file.
    second_exit, second_out, second_err = _run(
        _make_args(tmp_path, "bundle_user_modified_preserved"),
        capsys,
    )
    assert second_exit == 0
    payload = json.loads(second_out)
    assert payload["dismissed"] is False
    assert payload["reason"] == "no_deferrals_file"


def test_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Running on a folder without UPDATE_DEFERRED.md exits 0."""
    # tmp_path has no .claude/ tree at all.
    exit_code, out, err = _run(
        _make_args(tmp_path, "template_review_pending"),
        capsys,
    )

    assert exit_code == 0
    payload = json.loads(out)
    assert payload == {
        "dismissed": False,
        "condition_id": "template_review_pending",
        "remaining": 0,
        "reason": "no_deferrals_file",
    }


def test_no_file_human_mode_writes_stderr_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """In non-JSON mode the no-file path emits a stderr line and no stdout."""
    exit_code, out, err = _run(
        _make_args(tmp_path, "template_review_pending", json_mode=False),
        capsys,
    )
    assert exit_code == 0
    assert out == ""
    assert "no matching deferral" in err


def test_multi_deferral_one_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the matched condition_id is removed; siblings stay."""
    target = _seed_deferrals(
        tmp_path,
        [
            "bundle_user_modified_preserved",
            "template_review_pending",
            "schema_migration_required",
        ],
    )

    exit_code, out, _ = _run(
        _make_args(tmp_path, "template_review_pending"),
        capsys,
    )

    assert exit_code == 0
    payload = json.loads(out)
    assert payload["dismissed"] is True
    assert payload["remaining"] == 2
    assert payload["reason"] == "dismissed"

    # File still exists, contains the other two.
    assert target.exists()
    remaining = DeferralReport.read(tmp_path)
    remaining_ids = {e.condition_id for e in remaining.entries}
    assert remaining_ids == {
        "bundle_user_modified_preserved",
        "schema_migration_required",
    }


def test_multi_deferral_no_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No matching condition_id → file unchanged, exit 0."""
    target = _seed_deferrals(
        tmp_path,
        ["bundle_user_modified_preserved", "template_review_pending"],
    )
    original_bytes = target.read_bytes()

    exit_code, out, _ = _run(
        _make_args(tmp_path, "nonexistent_condition_id"),
        capsys,
    )

    assert exit_code == 0
    payload = json.loads(out)
    assert payload == {
        "dismissed": False,
        "condition_id": "nonexistent_condition_id",
        "remaining": 2,
        "reason": "no_match",
    }
    # File byte-identical: no rewrite happened.
    assert target.read_bytes() == original_bytes


def test_malformed_file_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Frontmatter advertises condition_ids but body has no entries → exit 1."""
    target = tmp_path / _DEFERRED_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    # Hand-rolled malformed content: frontmatter promises an entry,
    # body is empty.
    target.write_text(
        "---\n"
        "title: VCO Update Deferred\n"
        "generated_at: 2026-05-23T12:00:00Z\n"
        "condition_ids: [orphaned_entry]\n"
        "severity_max: warning\n"
        "---\n"
        "\n"
        "# VCO Update Deferred\n"
        "\n"
        "(body was truncated by a botched merge)\n",
        encoding="utf-8",
    )

    exit_code, out, err = _run(
        _make_args(tmp_path, "orphaned_entry"),
        capsys,
    )

    assert exit_code == 1
    assert out == ""
    assert "malformed deferral file" in err
    # File untouched on error so the user can inspect it.
    assert target.exists()


def test_json_mode_writes_only_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """In JSON mode, stdout carries the envelope and stderr stays empty
    for the happy and no-match paths (subprocess parsers tolerate empty
    stderr)."""
    _seed_deferrals(tmp_path, ["bundle_user_modified_preserved"])

    exit_code, out, err = _run(
        _make_args(tmp_path, "bundle_user_modified_preserved", json_mode=True),
        capsys,
    )

    assert exit_code == 0
    # stdout = pure JSON, parseable in one go.
    parsed = json.loads(out)
    assert parsed["dismissed"] is True
    # stderr quiet (the human "dismissed X" line is suppressed in JSON mode).
    assert err == ""


# ---------------------------------------------------------------------------
# argparse wiring smoke test — confirms the subcommand is actually
# registered (the bug we're fixing: it was missing). This is the gate
# that would have caught the original silent breakage.
# ---------------------------------------------------------------------------

def test_argparse_registers_dismiss_deferral_subcommand() -> None:
    """The `dismiss-deferral` subcommand must be reachable via the
    top-level arg parser. Pre-fix this raised 'invalid choice'."""
    from vco_lib.project_init import _build_arg_parser

    parser = _build_arg_parser()
    ns = parser.parse_args([
        "dismiss-deferral",
        "--folder", "/tmp/whatever",
        "--condition-id", "some_id",
        "--json",
    ])
    assert ns.subcommand == "dismiss-deferral"
    assert ns.folder == "/tmp/whatever"
    assert ns.condition_id == "some_id"
    assert ns.json is True
    # The handler must be wired so `main()` can dispatch.
    assert ns.func is _cmd_dismiss_deferral
