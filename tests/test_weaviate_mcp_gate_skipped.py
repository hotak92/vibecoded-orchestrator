# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.49 SB1 — gate-skipped surface tests for the MCP server.

When ``VCT_PROJECT_ID`` is empty/missing, the Phase-8 access-matrix
WRITE gate at ``store_knowledge_node`` cannot identify the project
against the hub. Pre-SB1 this was a silent-bypass — gate effectively
disabled. SB1 closes the visibility hole by emitting two surfaces:

  1. ``dropped_writes.jsonl`` row with reason='gate_skipped_no_project_id'
     (audit trail; fires per-call).
  2. ``UPDATE_DEFERRED.md`` entry guiding the user to remediate
     (user-facing; deduped per session).

Per the user's 2026-06-08 Q1 directive, the gate's empty-PID branch
still ALLOWS the write — silent-allow is the contract. The surfaces
are visibility-only; no stderr WARNING by default.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


# ─── _emit_gate_skipped_metric ──────────────────────────────────────────


def test_emit_gate_skipped_metric_writes_jsonl_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metric writer appends a single JSONL row with the canonical
    ``reason`` discriminator + empty project_id."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    srv._emit_gate_skipped_metric("MyKG_KnowledgeGraph")

    jsonl_path = tmp_path / "cache" / "dropped_writes.jsonl"
    assert jsonl_path.exists(), "metric JSONL was not created"

    rows = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "gate_skipped_no_project_id"
    assert row["project_id"] == ""  # empty by definition for this branch
    assert row["collection"] == "MyKG_KnowledgeGraph"
    assert row["fail_open"] is True
    assert isinstance(row["ts"], int)


def test_emit_gate_skipped_metric_appends_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-call audit-trail granularity: every call appends, no dedup.
    Contrast with the deferral writer which IS deduped per session.
    """
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    for _ in range(3):
        srv._emit_gate_skipped_metric("X_KnowledgeGraph")

    jsonl_path = tmp_path / "cache" / "dropped_writes.jsonl"
    rows = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line]
    assert len(rows) == 3, (
        "metric must fire per-call (audit-trail surface). Got "
        f"{len(rows)} rows; expected 3."
    )


def test_emit_gate_skipped_metric_silent_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cache dir is unwritable (read-only filesystem etc.), the
    helper must NOT raise — the silent-allow contract must hold even
    when audit-trail writes fail.
    """
    # Point at a read-only path (root-owned typically). We use a file
    # that exists as a regular file in place of a directory — any
    # mkdir / open call will EEXIST/EISDIR fail.
    blocker = tmp_path / "blocker"
    blocker.touch()
    monkeypatch.setenv("VCT_STATE_DIR", str(blocker))

    # Must not raise.
    srv._emit_gate_skipped_metric("X")  # no assertion needed; no-throw is the assertion


# ─── _emit_gate_skipped_deferral ───────────────────────────────────────


def _reset_seen_set() -> None:
    """Clear the module-level dedup set between tests so each test
    starts from a clean slate."""
    srv._GATE_SKIPPED_SESSIONS_SEEN.clear()


def test_emit_gate_skipped_deferral_writes_update_deferred_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferral writer creates UPDATE_DEFERRED.md with the SB1
    condition_id section."""
    _reset_seen_set()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_SESSION_ID", "test-session-1")

    srv._emit_gate_skipped_deferral("MyKG_KnowledgeGraph")

    deferred = tmp_path / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert deferred.exists(), "UPDATE_DEFERRED.md was not written"

    body = deferred.read_text(encoding="utf-8")
    assert "gate_skipped_no_project_id" in body
    assert "MyKG_KnowledgeGraph" in body, "target collection should appear in detected text"
    # The remediation block must mention both options.
    assert "install.py --update" in body
    assert "Launcher GUI" in body


def test_emit_gate_skipped_deferral_idempotent_within_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling the deferral writer twice in the same session must NOT
    accumulate duplicate condition_id sections — the per-session dedup
    set in the MCP process suppresses the second write.
    """
    _reset_seen_set()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_SESSION_ID", "stable-session")

    srv._emit_gate_skipped_deferral("FirstColl")
    srv._emit_gate_skipped_deferral("SecondColl")  # would have differed pre-SB1

    deferred = tmp_path / ".claude" / "context" / "UPDATE_DEFERRED.md"
    body = deferred.read_text(encoding="utf-8")

    # Exactly ONE condition_id section.
    section_count = body.count("## gate_skipped_no_project_id")
    assert section_count == 1, (
        f"deferral writer is not idempotent within a session: found "
        f"{section_count} sections, expected 1"
    )

    # First call's collection name landed; second's was silently dropped.
    assert "FirstColl" in body
    # The second call was dedup-suppressed, so its collection name does
    # NOT appear in the deferral body.
    assert "SecondColl" not in body, (
        "second call leaked through dedup — within a single session "
        "only the first call may write to the deferral file"
    )


def test_emit_gate_skipped_deferral_silent_when_no_project_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No resolvable project root → skip silently. The silent-allow
    contract must hold even when the deferral write target can't be
    located.
    """
    _reset_seen_set()
    # Clear all project-root hints.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KG_BASE_DIR", raising=False)
    monkeypatch.setenv("VCT_SESSION_ID", "no-dir-session")

    # Also need to monkeypatch the module-level fallback so it doesn't
    # find a real project root via __file__ resolution. We do that by
    # pointing CLAUDE_PROJECT_DIR at a non-existent path explicitly so
    # the resolver function's "is_dir()" guard rejects each candidate.
    nonexistent = tmp_path / "definitely-does-not-exist"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(nonexistent))
    monkeypatch.setenv("KG_BASE_DIR", str(nonexistent))

    # The Path(__file__).parent.parent.parent fallback DOES find a real
    # directory (the repo root), so we can't fully assert "no write"
    # here without intrusive mocking — but we can assert no exception.
    srv._emit_gate_skipped_deferral("Coll")  # no-throw assertion


# ─── _resolve_project_root_for_deferral ────────────────────────────────


def test_resolve_project_root_prefers_claude_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$CLAUDE_PROJECT_DIR`` wins when it points to a real dir."""
    proj = tmp_path / "claude-workspace"
    proj.mkdir()
    other = tmp_path / "kg-base"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("KG_BASE_DIR", str(other))

    resolved = srv._resolve_project_root_for_deferral()
    assert resolved is not None
    assert resolved.resolve() == proj.resolve()


def test_resolve_project_root_falls_back_to_kg_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``$CLAUDE_PROJECT_DIR`` is empty / missing, fall back to
    ``$KG_BASE_DIR``."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    kg_base = tmp_path / "kg-base"
    kg_base.mkdir()
    monkeypatch.setenv("KG_BASE_DIR", str(kg_base))

    resolved = srv._resolve_project_root_for_deferral()
    assert resolved is not None
    assert resolved.resolve() == kg_base.resolve()
