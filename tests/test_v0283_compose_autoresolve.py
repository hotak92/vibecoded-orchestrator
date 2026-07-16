# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F2: compose-override auto-resolution.

`_detect_and_rename_legacy_compose_override` now classifies a coexisting
legacy `docker-compose.override.yml` + canonical `compose.override.yaml` pair:

  * byte-identical (the v0.2.54 C-RT-5 mirror) → SUPPRESS the deferral, KEEP
    BOTH files, record an auto-resolution. [REGRESSION PIN]
  * yaml-semantically-equal (comment/whitespace drift) → re-mirror the legacy
    file to the canonical bytes, no deferral, record an auto-resolution.
  * genuinely divergent (parse failure / parsed-unequal) → keep today's
    `compose_override_filename_conflict` deferral verbatim. [LEAVE-ALONE]

Plus compose reconciliation: a previously-deferred conflict that is now
absent/identical clears via resolve_conditions.

The FROZEN return contract (action/renamed/conflicts/errors) is preserved; the
additive `auto_resolved` key carries the resolution summary. install.py:6497
reads only the frozen keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Inject the WP-B1 deferral_emit fake BEFORE importing project_init so the
# function-level `from vco_lib import deferral_emit` resolves it. Degrades to a
# no-op once the real module lands.
from tests._v0283_deferral_emit_fake import (  # noqa: E402
    install_fake_deferral_emit,
    read_auto_resolutions,
)

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport, DeferralEntry  # noqa: E402

_LEGACY = "docker-compose.override.yml"
_CANONICAL = "compose.override.yaml"


def _infra(root: Path) -> Path:
    d = root / "infrastructure"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _has_conflict(folder: Path) -> bool:
    report = DeferralReport.read(folder)
    return report.has_condition("compose_override_filename_conflict")


# ---------------------------------------------------------------------------
# REGRESSION PIN: byte-identical pair → suppress, KEEP BOTH, no mutation.
# ---------------------------------------------------------------------------

def test_regression_pin_byte_identical_pair_auto_resolves(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    body = b"services:\n  weaviate:\n    volumes:\n      - ./data:/data\n"
    legacy = infra / _LEGACY
    canonical = infra / _CANONICAL
    legacy.write_bytes(body)
    canonical.write_bytes(body)

    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)

    # No conflict deferral.
    assert not _has_conflict(tmp_path), "byte-identical mirror must NOT defer"
    # BOTH files untouched byte-for-byte.
    assert legacy.read_bytes() == body
    assert canonical.read_bytes() == body
    # Additive auto_resolved key populated; frozen keys present + empty.
    assert result is not None
    assert result["conflicts"] == []
    assert result["renamed"] == []
    assert result["errors"] == []
    assert any("byte-identical" in a for a in result["auto_resolved"])
    # B-F9: an auto-resolutions.jsonl line was written.
    rows = read_auto_resolutions(tmp_path)
    assert any(
        r["condition_id"] == "compose_override_filename_conflict"
        and r["action"] == "kept_identical_mirror_pair"
        for r in rows
    ), rows


# ---------------------------------------------------------------------------
# LEAVE-ALONE: divergent pair → deferral exactly as v0.2.82, no file mutation.
# ---------------------------------------------------------------------------

def test_leave_alone_divergent_pair_defers(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    legacy = infra / _LEGACY
    canonical = infra / _CANONICAL
    legacy_body = b"services:\n  weaviate:\n    ports:\n      - 8081:8081\n"
    canonical_body = b"services:\n  weaviate:\n    ports:\n      - 9090:9090\n"
    legacy.write_bytes(legacy_body)
    canonical.write_bytes(canonical_body)

    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)

    # Conflict deferral present (as v0.2.82).
    assert _has_conflict(tmp_path), "genuinely divergent pair MUST defer"
    # No file mutated.
    assert legacy.read_bytes() == legacy_body
    assert canonical.read_bytes() == canonical_body
    assert result is not None
    assert (str(legacy), str(canonical)) in [
        (o, n) for o, n in result["conflicts"]
    ]


# ---------------------------------------------------------------------------
# B-F2(ii): semantic-equal (comment-only drift) → legacy re-mirrored to canonical.
# ---------------------------------------------------------------------------

def test_semantic_equal_comment_drift_remirrors(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    legacy = infra / _LEGACY
    canonical = infra / _CANONICAL
    # Same YAML structure; legacy has an extra comment + different whitespace.
    legacy.write_bytes(
        b"# legacy hand-edit\nservices:\n  weaviate:\n    image: weaviate\n"
    )
    canonical_body = b"services:\n  weaviate:\n    image: weaviate\n"
    canonical.write_bytes(canonical_body)

    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)

    # No conflict deferral — semantically identical.
    assert not _has_conflict(tmp_path)
    # Legacy is now byte-identical to canonical (canonical won).
    assert legacy.read_bytes() == canonical_body
    assert canonical.read_bytes() == canonical_body
    assert result is not None
    assert any("re-mirrored" in a for a in result["auto_resolved"])
    rows = read_auto_resolutions(tmp_path)
    assert any(
        r["action"] == "remirrored_semantic_equal_override" for r in rows
    ), rows


def test_parse_failure_treated_as_divergent(tmp_path: Path) -> None:
    """Unparseable YAML on either side is conservative → keep the deferral."""
    infra = _infra(tmp_path)
    legacy = infra / _LEGACY
    canonical = infra / _CANONICAL
    # Legacy is not valid YAML mapping-vs-scalar structure but bytes differ.
    legacy.write_bytes(b": : : not : valid : yaml : {[}\n")
    canonical.write_bytes(b"services:\n  weaviate: {}\n")

    project_init._detect_and_rename_legacy_compose_override(tmp_path)

    assert _has_conflict(tmp_path), "parse failure must be treated as divergent"


# ---------------------------------------------------------------------------
# Compose reconciliation: a previously-deferred conflict now gone → self-clears.
# ---------------------------------------------------------------------------

def test_reconciliation_clears_stale_conflict_when_legacy_removed(tmp_path: Path) -> None:
    # Seed a stale conflict deferral (as if a prior run wrote it).
    report = DeferralReport.read(tmp_path)
    report.add_entry(DeferralEntry(
        condition_id="compose_override_filename_conflict",
        title="stale",
        detected="stale",
        why_deferred="stale",
        command_to_apply="noop",
        severity="warning",
    ))
    report.write(tmp_path)
    assert _has_conflict(tmp_path)

    # Now there is NO legacy file at all (user resolved it). The producer runs,
    # detects nothing, and reconciliation clears the stale conflict.
    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)

    assert not _has_conflict(tmp_path), "stale conflict must self-clear"
    assert result is not None  # returned because we reconciled something
    assert any("cleared stale" in a for a in result["auto_resolved"])


def test_reconciliation_clears_conflict_when_pair_became_identical(tmp_path: Path) -> None:
    infra = _infra(tmp_path)
    # Seed a stale conflict.
    report = DeferralReport.read(tmp_path)
    report.add_entry(DeferralEntry(
        condition_id="compose_override_filename_conflict",
        title="stale",
        detected="stale",
        why_deferred="stale",
        command_to_apply="noop",
        severity="warning",
    ))
    report.write(tmp_path)

    # The pair is now byte-identical (the mirror was re-synced).
    body = b"services:\n  x: {}\n"
    (infra / _LEGACY).write_bytes(body)
    (infra / _CANONICAL).write_bytes(body)

    project_init._detect_and_rename_legacy_compose_override(tmp_path)

    assert not _has_conflict(tmp_path), (
        "an identical pair this run must clear a stale conflict deferral"
    )


def test_no_legacy_file_is_true_noop(tmp_path: Path) -> None:
    """No legacy file AND no stale deferral → returns None (true no-op)."""
    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)
    assert result is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
