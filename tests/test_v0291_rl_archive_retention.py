# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R1 (v0.2.91) — the rl_events retention prune is ARCHIVE-then-delete.

Context (telemetry verification 2026-08-27, §6): the shipped 90-day prune was a
bare ``DELETE FROM rl_events WHERE ts < ?`` with no export path, armed to start
destroying the oldest slice of the RL training corpus on ~2026-09-03. Every
destroyed row carries a query embedding, per-node embeddings, ``linked_embs``,
``answer_chunk_embs`` and a cosine-derived label that cannot be recomputed.

The DELETE authority + archive writer are Rust (``db/rl_events.rs``; both-sides
coverage lives in that module's ``#[test]`` block: archives-then-deletes,
failed-archive-aborts, pair-integrity, serving-path-unchanged). THIS suite
covers the Python half of the contract:

  * the archive READER (``vco_lib.rl_archive``) parses the sidecar shape the
    Rust writer emits, in the row shape the RL loader consumes;
  * ``.pending`` (in-flight) sidecars are SKIPPED — reading one would
    double-count rows that are still in launcher.db;
  * the format constants + hub-row FIELD SET are parity-locked against the Rust
    source, so a rename on either side fails here instead of silently producing
    archives nothing can read;
  * the retention-days env override actually moves the cutoff.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import rl_retention  # noqa: E402
from vco_lib import rl_archive  # noqa: E402

_RUST_RL_EVENTS = (
    PROJECT_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "rl_events.rs"
)
_RUST_HUB_API = (
    PROJECT_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "rl_events_api.rs"
)

_DAY_MS = 86_400_000


# --------------------------------------------------------------------------
# Fixtures: a synthetic archive in the exact shape the Rust writer emits
# --------------------------------------------------------------------------


def _row(
    row_id: int,
    *,
    event_type: str = "retrieval",
    ts_ms: int = 1_700_000_000_000,
    project_id: str | None = "proj-a",
    task_id: str = "task-1",
    payload: str = '{"event":"retrieval","schema_version":3,"query_emb":[0.5,-0.25]}',
) -> dict:
    """One hub-ROW object, matching vct-hub's ``RlEventOut`` field-for-field."""
    return {
        "id": row_id,
        "event_type": event_type,
        "schema_version": 3,
        "ts_ms": ts_ms,
        "project_id": project_id,
        "project_name": "VCO_dev",
        "task_id": task_id,
        "task_type": "pre_edit_kg_search",
        "embedding_source": "qwen3",
        "embedding_dim": 1024,
        "embedding_model": "qwen3-embedding:0.6b",
        "payload_json": payload,
        "quarantined_at": None,
        "quarantine_reason": None,
    }


def _write_archive(directory: Path, stem: str, rows: list[dict], *, pending: bool = False) -> Path:
    suffix = (
        rl_archive.ARCHIVE_PENDING_SUFFIX if pending else rl_archive.ARCHIVE_SUFFIX
    )
    path = directory / f"{stem}{suffix}"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def archive(tmp_path: Path):
    d = tmp_path / "rl_archive"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# 1. The reader round-trips what the writer emits
# --------------------------------------------------------------------------


def test_reader_round_trips_rows_and_preserves_payload_verbatim(archive: Path):
    payload = '{"event":"retrieval","nodes":[{"title":"N","emb":[1.0,2.0]}]}'
    _write_archive(
        archive,
        "rl_events-20260903T000000Z-proj_a-1-2-2rows",
        [_row(1, payload=payload), _row(2, event_type="citation")],
    )
    rows = list(rl_archive.iter_archived_rows(archive))
    assert [r["id"] for r in rows] == [1, 2]
    # payload_json is a STRING, byte-for-byte — this is what makes the archived
    # embeddings replayable at all.
    assert rows[0]["payload_json"] == payload
    assert json.loads(rows[0]["payload_json"])["nodes"][0]["emb"] == [1.0, 2.0]


def test_reader_filters_match_the_live_loader_filters(archive: Path):
    _write_archive(
        archive,
        "rl_events-20260903T000000Z-a-1-4-4rows",
        [
            _row(1, project_id="proj-a", ts_ms=100, event_type="retrieval"),
            _row(2, project_id="proj-a", ts_ms=200, event_type="citation"),
            _row(3, project_id="proj-b", ts_ms=300, event_type="retrieval"),
            _row(4, project_id=None, ts_ms=400, event_type="retrieval"),
        ],
    )
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive, project_id="proj-a")] == [1, 2]
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive, event_type="citation")] == [2]
    assert [
        r["id"] for r in rl_archive.iter_archived_rows(archive, since_ms=200, until_ms=300)
    ] == [2, 3]


def test_reader_reads_files_in_chronological_stem_order(archive: Path):
    _write_archive(archive, "rl_events-20260910T000000Z-a-3-3-1rows", [_row(3)])
    _write_archive(archive, "rl_events-20260903T000000Z-a-1-1-1rows", [_row(1)])
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive)] == [1, 3]


def test_malformed_line_is_skipped_not_fatal(archive: Path):
    path = archive / f"rl_events-20260903T000000Z-a-1-2-2rows{rl_archive.ARCHIVE_SUFFIX}"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row(1)) + "\n")
        fh.write("{not json\n")
        fh.write(json.dumps(_row(2)) + "\n")
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive)] == [1, 2]


# --------------------------------------------------------------------------
# 2. `.pending` sidecars must never be read (double-count guard)
# --------------------------------------------------------------------------


def test_pending_archive_is_skipped_by_reader_and_reported(archive: Path):
    """A `.pending` file is an archive whose DELETE never ran — its rows are
    still in launcher.db. Reading it would double-count those events in
    training. It must be skipped, and visibly reported."""
    _write_archive(archive, "rl_events-20260903T000000Z-a-1-1-1rows", [_row(1)])
    _write_archive(
        archive, "rl_events-20260904T000000Z-a-2-2-1rows", [_row(2)], pending=True
    )
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive)] == [1]
    assert [p.name for p in rl_archive.archive_files(archive)] == [
        f"rl_events-20260903T000000Z-a-1-1-1rows{rl_archive.ARCHIVE_SUFFIX}"
    ]
    report = rl_archive.archive_report(archive)
    assert report["rows"] == 1
    assert report["files"] == 1
    assert len(report["pending_files"]) == 1


def test_archive_report_summarizes_without_leaking_payloads(archive: Path):
    _write_archive(
        archive,
        "rl_events-20260903T000000Z-a-1-3-3rows",
        [
            _row(1, project_id="proj-a", ts_ms=100),
            _row(2, project_id="proj-a", ts_ms=500, event_type="citation"),
            _row(3, project_id="proj-b", ts_ms=900),
        ],
    )
    rep = rl_archive.archive_report(archive)
    assert rep["rows"] == 3
    assert rep["oldest_ts_ms"] == 100 and rep["newest_ts_ms"] == 900
    assert rep["rows_by_project"] == {"proj-a": 2, "proj-b": 1}
    assert rep["rows_by_event_type"] == {"retrieval": 2, "citation": 1}
    # No payload content in the report (it is printed by rl-doctor / CLI).
    assert "payload_json" not in json.dumps(rep)


# --------------------------------------------------------------------------
# 2b. Quarantined rows: archived, but NOT served by default (wave-4 MAJOR-4)
# --------------------------------------------------------------------------


def _quarantined(row_id: int, **kw) -> dict:
    row = _row(row_id, **kw)
    row["quarantined_at"] = 1_700_000_500_000
    row["quarantine_reason"] = "score_out_of_range"
    return row


def test_quarantined_rows_are_excluded_from_the_default_read(archive: Path):
    """WAVE-4 MAJOR-4. `iter_archived_rows` is the module's own prescribed
    trainer recipe (`[_reshape_hub_row(r) for r in iter_archived_rows(...)]`),
    and the live loader path it claims to mirror EXCLUDES quarantined rows by
    default (`rl_events_api.rs::list_events` → `include_quarantined=false`,
    RL-14). The reader had no quarantine filter at all, and `_reshape_hub_row`
    does not check the field either — so the documented recipe re-ingested
    poisoned rows verbatim. The oldest slice, which is what gets archived
    FIRST, is exactly where the `score_out_of_range` era lives.

    RED-PROOF: pre-fix this returns [1, 2, 3] — the quarantined row is served.
    """
    _write_archive(
        archive,
        "rl_events-20260903T000000Z-a-1-3-3rows",
        [_row(1), _quarantined(2), _row(3)],
    )
    assert [r["id"] for r in rl_archive.iter_archived_rows(archive)] == [1, 3]


def test_quarantined_rows_are_visible_on_explicit_opt_in(archive: Path):
    """The inspection surface opts in exactly as the GET's
    `include_quarantined=true` does. Archiving them was always correct — the
    prune must never drop a row it cannot reproduce — so nothing is lost."""
    _write_archive(
        archive, "rl_events-20260903T000000Z-a-1-3-3rows",
        [_row(1), _quarantined(2), _row(3)],
    )
    rows = list(rl_archive.iter_archived_rows(archive, include_quarantined=True))
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert rows[1]["quarantine_reason"] == "score_out_of_range"


def test_quarantine_filter_composes_with_the_other_filters(archive: Path):
    """The quarantine gate must not be bypassable through another filter."""
    _write_archive(
        archive,
        "rl_events-20260903T000000Z-a-1-4-4rows",
        [
            _row(1, project_id="proj-a", ts_ms=100),
            _quarantined(2, project_id="proj-a", ts_ms=200),
            _row(3, project_id="proj-b", ts_ms=300),
            _quarantined(4, project_id="proj-a", ts_ms=400, event_type="citation"),
        ],
    )
    assert [
        r["id"] for r in rl_archive.iter_archived_rows(archive, project_id="proj-a")
    ] == [1]
    assert [
        r["id"]
        for r in rl_archive.iter_archived_rows(archive, event_type="citation")
    ] == []
    assert [
        r["id"]
        for r in rl_archive.iter_archived_rows(archive, since_ms=100, until_ms=400)
    ] == [1, 3]


def test_archive_report_counts_quarantined_rows_separately(archive: Path):
    """The report is the operator's "what is in here" view: it counts the RAW
    totals AND how many of them a default (training) read skips."""
    _write_archive(
        archive, "rl_events-20260903T000000Z-a-1-3-3rows",
        [_row(1), _quarantined(2), _row(3)],
    )
    rep = rl_archive.archive_report(archive)
    assert rep["rows"] == 3
    assert rep["quarantined_rows"] == 1


def test_cli_exposes_the_quarantine_opt_in(archive: Path, capsys):
    """The CLI is the non-Python consumer's only door; the default there has to
    be the same default the library serves."""
    _write_archive(
        archive, "rl_events-20260903T000000Z-a-1-2-2rows",
        [_row(1), _quarantined(2)],
    )
    assert rl_archive.main(["--dir", str(archive)]) == 0
    default_ids = [json.loads(ln)["id"] for ln in capsys.readouterr().out.splitlines() if ln]
    assert default_ids == [1]

    assert rl_archive.main(["--dir", str(archive), "--include-quarantined"]) == 0
    optin_ids = [json.loads(ln)["id"] for ln in capsys.readouterr().out.splitlines() if ln]
    assert optin_ids == [1, 2]


def test_reader_default_matches_the_hub_get_default():
    """Parity lock: the exclusion is only correct because it mirrors the live
    trainer read. If the Rust route ever flips its default, this fails here
    instead of silently letting the archived and live corpora diverge."""
    api = _RUST_HUB_API.read_text(encoding="utf-8")
    assert "q.include_quarantined.unwrap_or(false)" in api, (
        "the hub GET no longer defaults include_quarantined to false — "
        "vco_lib.rl_archive.iter_archived_rows mirrors that default and must "
        "be updated in the same commit"
    )


def test_missing_archive_dir_is_empty_not_an_error(tmp_path: Path):
    missing = tmp_path / "never-created"
    assert rl_archive.archive_files(missing) == []
    assert list(rl_archive.iter_archived_rows(missing)) == []
    assert rl_archive.archive_report(missing)["rows"] == 0


def test_archive_dir_honors_env_override(tmp_path: Path):
    with patch.dict(os.environ, {rl_archive.ARCHIVE_DIR_ENV: str(tmp_path)}):
        assert rl_archive.archive_dir() == tmp_path
    with patch.dict(os.environ, {rl_archive.ARCHIVE_DIR_ENV: "   "}):
        # Whitespace-only is NOT an override — falls back to <vct root>/rl_archive.
        assert rl_archive.archive_dir().name == "rl_archive"


# --------------------------------------------------------------------------
# 3. Cross-language parity locks (Rust writer ↔ Python reader)
# --------------------------------------------------------------------------


def test_format_constants_match_the_rust_writer():
    """MUST MATCH db/rl_events.rs. A rename on either side silently produces
    archives the reader cannot see — the exact "delete with extra steps"
    failure this whole feature exists to prevent."""
    rust = _RUST_RL_EVENTS.read_text(encoding="utf-8")
    for name, value in (
        ("RL_ARCHIVE_DIR_ENV", rl_archive.ARCHIVE_DIR_ENV),
        ("RL_ARCHIVE_SUFFIX", rl_archive.ARCHIVE_SUFFIX),
        ("RL_ARCHIVE_PENDING_SUFFIX", rl_archive.ARCHIVE_PENDING_SUFFIX),
    ):
        assert f'pub const {name}: &str = "{value}"' in rust, (
            f"{name} drifted: Rust source does not declare it as {value!r}"
        )


def test_archived_row_fields_match_the_hub_row_shape():
    """The archive line must carry EVERY field of vct-hub's ``RlEventOut`` —
    that is what lets ``hub_event_loader._reshape_hub_row`` consume an archived
    row with no new parser (report §6.7 "enumerable by the loader")."""
    api = _RUST_HUB_API.read_text(encoding="utf-8")
    block = api.split("pub struct RlEventOut", 1)[1].split("}", 1)[0]
    hub_fields = set(re.findall(r"pub (\w+):", block))
    assert hub_fields, "could not parse RlEventOut fields from the hub source"

    rust = _RUST_RL_EVENTS.read_text(encoding="utf-8")
    fn = rust.split("fn archive_line(", 1)[1].split("\n}", 1)[0]
    archived_fields = set(re.findall(r'"(\w+)":\s*e\.', fn))

    assert archived_fields == hub_fields, (
        "archive_line() and RlEventOut disagree — archived rows would not be "
        f"loader-shaped. only-in-archive={archived_fields - hub_fields} "
        f"only-in-hub={hub_fields - archived_fields}"
    )
    # And the Python fixture (which stands in for a real sidecar line) agrees.
    assert set(_row(1)) == hub_fields


def test_hub_prune_route_resolves_the_archive_dir_itself():
    """The archive destination must NOT come from the request body: an authed
    localhost caller could otherwise steer an arbitrary-path write, and the
    deletion authority would depend on a caller remembering to name one."""
    api = _RUST_HUB_API.read_text(encoding="utf-8")
    assert "rl_events::rl_archive_dir()" in api, (
        "the prune route must resolve the archive dir hub-side"
    )
    body = api.split("pub struct PruneEventsBody", 1)[1].split("}", 1)[0]
    assert "archive" not in body.lower(), (
        "PruneEventsBody must not accept an archive path from the caller"
    )


def test_hub_side_env_knobs_are_documented_in_the_python_driver():
    """The hub-side knobs (archive dir, per-pass cap) are invisible from the
    Python driver's own env reads, so they MUST be named in its config docstring
    — otherwise an operator hunting for "where do I change this" finds nothing."""
    doc = (
        PROJECT_ROOT / "claude_mcp_servers" / "rl_client" / "rl_retention.py"
    ).read_text(encoding="utf-8")
    rust = _RUST_RL_EVENTS.read_text(encoding="utf-8")
    for env in ("RL_EVENTS_ARCHIVE_DIR", "RL_EVENTS_PRUNE_MAX_TASKS_PER_PASS"):
        assert env in doc, f"{env} is not documented in rl_retention.py"
        assert f'"{env}"' in rust, f"{env} is not the name the Rust side reads"


def test_prune_signature_requires_an_archive_destination():
    """There is deliberately NO no-archive code path: `archive_dir` is a
    required parameter of the deletion authority."""
    rust = _RUST_RL_EVENTS.read_text(encoding="utf-8")
    sig = rust.split("pub fn prune_rl_events(", 1)[1].split(")", 1)[0]
    assert "archive_dir: &Path" in sig, (
        "prune_rl_events must take a REQUIRED archive_dir — an Option would "
        "re-open the bare-DELETE path"
    )


# --------------------------------------------------------------------------
# 4. Retention-days override (the operator's knob)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_throttle():
    rl_retention._reset_throttle_for_test()
    yield
    rl_retention._reset_throttle_for_test()


def test_retention_days_env_override_moves_the_cutoff():
    now = 1_800_000_000_000
    with patch.dict(os.environ, {"RL_EVENTS_RETENTION_MAX_AGE_DAYS": "365"}):
        plan = rl_retention.compute_retention_plan(now_ms=now)
    assert plan.cutoff_ms == now - 365 * _DAY_MS
    assert "age>365d" in plan.reason

    with patch.dict(os.environ, {"RL_EVENTS_RETENTION_MAX_AGE_DAYS": "0"}):
        plan = rl_retention.compute_retention_plan(now_ms=now)
    assert plan.cutoff_ms is None, "0 days disables the age bound entirely"
    assert plan.is_noop()


def test_default_retention_is_still_90_days():
    """Pin the shipped default so a silent change is caught — the archive makes
    a prune survivable, it does not make an accidental default change fine."""
    now = 1_800_000_000_000
    keys = (
        "RL_EVENTS_RETENTION_MAX_AGE_DAYS",
        "RL_EVENTS_RETENTION_MAX_ROWS",
        "RL_EVENTS_RETENTION_DISABLED",
    )
    with patch.dict(os.environ, {}, clear=False):
        for k in keys:
            os.environ.pop(k, None)
        plan = rl_retention.compute_retention_plan(now_ms=now)
    assert plan.cutoff_ms == now - 90 * _DAY_MS
