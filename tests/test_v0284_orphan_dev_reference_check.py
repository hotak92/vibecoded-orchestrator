# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 (WP-1 / D5 / P3) — honest orphan-collection reference check.

The orphan-Development-collection detector used to claim "no callers, safe to
drop" while a freshly-written ``.claude/settings.json`` LITERALLY referenced the
collection (the dogfood incident). D5 adds
``vco_lib.install_weaviate.dev_collection_is_referenced`` — consulted across ALL
live config surfaces (settings.json env, .claude/env managed block CRLF-safe,
process env, launcher.db resolution) — and delegates the whole emit decision to
``build_orphan_dev_deferral``.

Pins:
  * FAIL-WITHOUT-FIX (P3 leave-alone): candidate referenced by
    ``.claude/settings.json::env`` ⇒ NO deferral emitted.
  * Act: unreferenced + 0 rows ⇒ deferral emitted, sibling-with-rows named in
    ``detected``.
  * CRLF (A4): a ``.claude/env`` managed block with ``\\r\\n`` line endings is
    parsed identically.
  * KG-sibling reference: ``KG_COLLECTION`` whose suffix-swap == candidate ⇒
    referenced (P2's repoint will converge it).
  * Non-root (A3): every act path runs against a NON-ROOT folder (distinct from
    an orchestrator root).
  * drop-when-absent: ``orphan_orchestrator_development_collection`` stays in
    ``install._INSTALL_OWNED_CONDITION_IDS`` (self-clear intact).

Every fixture is constructed in a tmp folder — nothing depends on machine state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import install_weaviate as iw  # noqa: E402

_CANDIDATE = "VibeCodedOrchestrator_Development"
_MANAGED_BEGIN = "# vco-managed-begin"
_MANAGED_END = "# vco-managed-end"


# ── fixtures: write a NON-ROOT project folder's env surfaces ──────────────────
def _write_settings_json(folder: Path, env: dict) -> None:
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"env": env}, indent=2), encoding="utf-8"
    )


def _write_claude_env(folder: Path, pairs: dict, *, crlf: bool = False,
                      unmanaged: dict | None = None) -> None:
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    lines = []
    if unmanaged:
        for k, v in unmanaged.items():
            lines.append(f'export {k}="{v}"')
    lines.append(_MANAGED_BEGIN)
    for k, v in pairs.items():
        lines.append(f'export {k}="{v}"')
    lines.append(_MANAGED_END)
    sep = "\r\n" if crlf else "\n"
    (folder / ".claude" / "env").write_text(sep.join(lines) + sep, encoding="utf-8")


@pytest.fixture
def non_root(tmp_path):
    """A NON-ROOT project folder (A3) — distinct from any orchestrator root."""
    folder = tmp_path / "client-alpha"
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(autouse=True)
def _isolate_process_env(monkeypatch):
    """Neutralize the process-env surface + launcher.db surface so each test
    exercises exactly the surface it sets up (no bleed from the runner's env)."""
    monkeypatch.delenv("DEVELOPMENT_COLLECTION", raising=False)
    monkeypatch.delenv("KG_COLLECTION", raising=False)
    # Force the launcher.db surface to "unreachable" unless a test opts in.
    from vco_lib import config_projection as cp
    monkeypatch.setattr(cp, "_resolve_launcher_db_path", lambda: Path("/nonexistent/x"))
    yield


# ── dev_collection_is_referenced ──────────────────────────────────────────────
def test_settings_json_direct_dev_reference(non_root):
    """FAIL-WITHOUT-FIX PIN (P3 leave-alone): settings.json env names the
    candidate as DEVELOPMENT_COLLECTION ⇒ referenced."""
    _write_settings_json(non_root, {"DEVELOPMENT_COLLECTION": _CANDIDATE})
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is True
    assert surface == ".claude/settings.json::env"


def test_settings_json_kg_sibling_reference(non_root):
    """A KG_COLLECTION whose suffix-swap sibling == candidate ⇒ referenced
    (P2's repoint will converge the dev pointer)."""
    # VibeCodedOrchestrator_KnowledgeGraph → VibeCodedOrchestrator_Development
    _write_settings_json(
        non_root, {"KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph"}
    )
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is True
    assert surface == ".claude/settings.json::env"


def test_claude_env_managed_reference(non_root):
    _write_claude_env(non_root, {"DEVELOPMENT_COLLECTION": _CANDIDATE})
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is True
    assert surface == ".claude/env managed block"


def test_claude_env_CRLF_reference(non_root):
    """A4 (CRLF): a Windows \\r\\n-terminated managed block parses identically."""
    _write_claude_env(non_root, {"DEVELOPMENT_COLLECTION": _CANDIDATE}, crlf=True)
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is True
    assert surface == ".claude/env managed block"


def test_claude_env_reference_scoped_to_managed_block(non_root):
    """A user's OWN out-of-managed-block export must NOT count as a VCO
    reference (only the managed block is read)."""
    _write_claude_env(
        non_root,
        {"KG_COLLECTION": "ClientAlpha_KnowledgeGraph"},  # managed: unrelated
        unmanaged={"DEVELOPMENT_COLLECTION": _CANDIDATE},  # user's own line
    )
    referenced, _ = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is False


def test_process_env_reference(non_root, monkeypatch):
    monkeypatch.setenv("DEVELOPMENT_COLLECTION", _CANDIDATE)
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is True
    assert surface == "process env"


def test_unreferenced_returns_false(non_root):
    """A NON-ROOT project pointing at its OWN dev collection does NOT reference
    the orchestrator's orphan candidate."""
    _write_settings_json(non_root, {
        "DEVELOPMENT_COLLECTION": "ClientAlpha_Development",
        "KG_COLLECTION": "ClientAlpha_KnowledgeGraph",
    })
    referenced, surface = iw.dev_collection_is_referenced(_CANDIDATE, non_root)
    assert referenced is False
    assert surface == ""


def test_empty_candidate_is_unreferenced(non_root):
    assert iw.dev_collection_is_referenced("", non_root) == (False, "")


# ── paired_dev_sibling ────────────────────────────────────────────────────────
def test_paired_dev_sibling_from_settings(non_root):
    _write_settings_json(non_root, {
        "DEVELOPMENT_COLLECTION": "VCODev_Development",
    })
    assert iw.paired_dev_sibling(_CANDIDATE, non_root) == "VCODev_Development"


def test_paired_dev_sibling_from_kg_suffix_swap(non_root):
    _write_settings_json(non_root, {"KG_COLLECTION": "VCODev_KnowledgeGraph"})
    assert iw.paired_dev_sibling(_CANDIDATE, non_root) == "VCODev_Development"


def test_paired_dev_sibling_none_when_same_as_candidate(non_root):
    _write_settings_json(non_root, {"DEVELOPMENT_COLLECTION": _CANDIDATE})
    assert iw.paired_dev_sibling(_CANDIDATE, non_root) is None


# ── build_orphan_dev_deferral (whole emit decision) ───────────────────────────
def _count_fn_factory(counts):
    return lambda url, name: counts.get(name)


def test_build_deferral_leave_alone_when_referenced(non_root):
    """FAIL-WITHOUT-FIX PIN (P3): referenced ⇒ NO entry (returns None), honest
    log line."""
    _write_settings_json(non_root, {"DEVELOPMENT_COLLECTION": _CANDIDATE})
    logs = []
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map={_CANDIDATE: {}},
        count_fn=_count_fn_factory({_CANDIDATE: 0}),
        log_event=lambda step, phase, detail="": logs.append(detail),
    )
    assert entry is None
    assert any("referenced by .claude/settings.json::env" in d for d in logs)


def test_build_deferral_act_emits_with_sibling_enrichment(non_root):
    """ACT: unreferenced + 0 rows ⇒ entry emitted; the data-holding sibling is
    named in `detected`."""
    # Point the non-root env at its own dev collection (does NOT reference the
    # orphan candidate) — and that sibling holds rows.
    _write_settings_json(non_root, {"KG_COLLECTION": "VCODev_KnowledgeGraph"})
    class_map = {_CANDIDATE: {}, "VCODev_Development": {}}
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map=class_map,
        count_fn=_count_fn_factory({_CANDIDATE: 0, "VCODev_Development": 340}),
    )
    assert entry is not None
    assert entry.condition_id == "orphan_orchestrator_development_collection"
    assert "VCODev_Development" in entry.detected
    assert "340 row(s)" in entry.detected


def test_build_deferral_none_when_absent_from_schema(non_root):
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map={},  # candidate not present
        count_fn=_count_fn_factory({}),
    )
    assert entry is None


def test_build_deferral_none_when_non_zero_rows(non_root):
    """Never a destructive drop of a NON-empty collection."""
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map={_CANDIDATE: {}},
        count_fn=_count_fn_factory({_CANDIDATE: 5}),
    )
    assert entry is None


def test_build_deferral_none_when_row_count_unknown(non_root):
    """Undeterminable row count (None) ⇒ never emit (can't confirm empty)."""
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map={_CANDIDATE: {}},
        count_fn=_count_fn_factory({}),  # returns None for the candidate
    )
    assert entry is None


def test_build_deferral_conservative_on_check_error(non_root, monkeypatch):
    """An unexpected reference-check error ⇒ treated as REFERENCED (no
    destructive-drop deferral we cannot positively justify)."""
    monkeypatch.setattr(
        iw, "dev_collection_is_referenced",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    entry = iw.build_orphan_dev_deferral(
        _CANDIDATE, non_root, "http://localhost:8081",
        class_map={_CANDIDATE: {}},
        count_fn=_count_fn_factory({_CANDIDATE: 0}),
    )
    assert entry is None


# ── drop-when-absent self-clear invariant ─────────────────────────────────────
def test_orphan_condition_id_stays_install_owned():
    """The condition stays in the owned set → drop-when-absent self-clear is
    intact (an on-disk entry from a prior run clears when not re-emitted)."""
    import install  # noqa: E402
    assert (
        "orphan_orchestrator_development_collection"
        in install._INSTALL_OWNED_CONDITION_IDS
    )
