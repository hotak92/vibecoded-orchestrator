# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-T post-deploy smoke test: confirm V52-I's defensive filter held.

V52-I added defence against the `partial_fan_out_schema_missing`
failure mode that mis-routed retrievals at the shared-KG /
per-project-KG boundary. The fix is implementation-level (Fix A + Fix B
in the V52-I plan); this test is the post-deploy CANARY:

    Within 48h of the v0.2.52 tag, no rl_events row with
    `event_type='retrieval'` and
    `payload_json.failure_mode='partial_fan_out_schema_missing'`
    should have been written.

If this test fails post-deploy, V52-I has a regression. Run it
manually after the tag has been live 48h+ — the TODO below names the
exact timestamp that needs to be filled in at v0.2.52 ship time.

Skip behaviour:
  * launcher.db absent → SKIP (test only runs on a live install)
  * V0252_TAG_TIME_MS env var unset → SKIP (we don't know what "post-tag"
    means until the tag exists). Operators fill this in via the
    post-deploy runbook.

Why a stand-alone module vs adding to an existing smoke file:
  * Different timing semantics (this one wants to run 48h AFTER deploy,
    not at CI time) — keeping it discoverable + clearly opt-in.
  * Lets V52-K's re-collect verdict reference it by stable path.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


# TODO(v0.2.52 ship): replace this placeholder with the actual tag time
# (epoch milliseconds) once v0.2.52 is tagged. Until then the test
# auto-skips and the failure mode goes uncaught — that's intentional
# (test would otherwise produce a noisy false positive on any pre-tag
# CI run). Set ``V0252_TAG_TIME_MS`` env var to override locally.
V0252_TAG_TIME_MS_DEFAULT: int = 0  # 0 = unset → skip


def _resolve_db_path() -> Path | None:
    """Discover launcher.db. Matches launcher_db_reader._discover_db_path."""
    override = os.environ.get("VCT_LAUNCHER_DB_PATH", "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None

    state_dir = os.environ.get("VCT_STATE_DIR", "").strip()
    if state_dir:
        p = Path(state_dir).expanduser() / "launcher.db"
    else:
        p = Path.home() / ".vct" / "launcher.db"
    return p if p.is_file() else None


def _resolve_tag_time_ms() -> int:
    """Read the v0.2.52 tag time from env or fall back to the constant."""
    env_val = os.environ.get("V0252_TAG_TIME_MS", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            return V0252_TAG_TIME_MS_DEFAULT
    return V0252_TAG_TIME_MS_DEFAULT


def test_no_partial_fan_out_schema_missing_post_v0252():
    """Zero ``partial_fan_out_schema_missing`` events post v0.2.52 tag.

    Probes launcher.db for retrieval events with the V52-I failure mode
    logged AFTER the v0.2.52 tag time. Any such row indicates V52-I's
    defensive filter has regressed and needs investigation.
    """
    tag_time = _resolve_tag_time_ms()
    if tag_time <= 0:
        pytest.skip(
            "V0252_TAG_TIME_MS not set — fill in post-tag per the "
            "post-deploy runbook (TODO comment in this file)"
        )

    db_path = _resolve_db_path()
    if db_path is None:
        pytest.skip("launcher.db not found (test only runs on a live install)")

    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        pytest.skip(f"failed to open {db_path}: {exc}")

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM rl_events
            WHERE event_type='retrieval'
              AND json_extract(payload_json, '$.failure_mode')
                  = 'partial_fan_out_schema_missing'
              AND created_at >= ?
            """,
            (tag_time,),
        ).fetchone()
    finally:
        conn.close()

    count = (row["n"] or 0) if row else 0
    assert count == 0, (
        f"V52-I REGRESSION: {count} rl_events row(s) with "
        f"failure_mode='partial_fan_out_schema_missing' logged after the "
        f"v0.2.52 tag time ({tag_time} ms). Investigate the schema-fan-out "
        f"site in claude_mcp_servers/weaviate_mcp/server.py "
        f"(hybrid_search + semantic_graph_search)."
    )
