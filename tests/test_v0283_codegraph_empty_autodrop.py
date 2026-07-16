# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F6: empty legacy code-graph candidate auto-drop.

`_autodrop_empty_codegraph_candidates` partitions legacy code-graph candidates:
an EMPTY (object_count==0) non-case_only candidate is dropped ONLY after a
RE-PROBE immediately before the drop still returns 0, followed by a POST-DROP
probe confirming the class is gone. Everything else stays in the deferred
remainder.

REGRESSION PIN: a candidate that is empty at DETECT but NON-empty at the
immediate pre-drop re-probe is NOT dropped (0-then-N via monkeypatched
_http_count_objects) — this pins the re-probe-before-acting invariant.

Never touches KG-family candidates (this helper is only ever called with
code-graph candidates) and NEVER drops a case_only candidate (BUG-1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0283_deferral_emit_fake import (  # noqa: E402
    install_fake_deferral_emit,
    read_auto_resolutions,
)

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402

URL = "http://localhost:8081"


def _cand(name: str, *, count, case_only: bool = False) -> dict:
    return {
        "class_name": name,
        "suffix": "_CodeFunction",
        "object_count": count,
        "embedding_dim": None,
        "canonical_name": "Canon_CodeFunction",
        "case_only": case_only,
    }


# ---------------------------------------------------------------------------
# ACT: empty at detect AND empty at re-probe → dropped + post-probe verified.
# ---------------------------------------------------------------------------

def test_act_empty_candidate_dropped(tmp_path: Path, monkeypatch) -> None:
    dropped_calls: list[str] = []

    def _fake_delete(name, weaviate_url=None):
        dropped_calls.append(name)

    # Re-probe (pre-drop) returns 0; post-drop probe returns None (class gone).
    counts = iter([0, None])
    monkeypatch.setattr(project_init, "_delete_class", _fake_delete)
    monkeypatch.setattr(
        project_init, "_http_count_objects",
        lambda name, url: next(counts),
    )

    candidates = [_cand("Old_CodeFunction", count=0)]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )

    assert dropped == ["Old_CodeFunction"]
    assert remaining == []
    assert dropped_calls == ["Old_CodeFunction"]
    rows = read_auto_resolutions(tmp_path)
    assert any(
        r["condition_id"] == "codegraph_collection_legacy_candidates"
        and r["action"] == "dropped_empty_legacy_codegraph_class"
        for r in rows
    ), rows


# ---------------------------------------------------------------------------
# REGRESSION PIN: empty at detect, NON-empty at pre-drop re-probe → NOT dropped.
# ---------------------------------------------------------------------------

def test_regression_pin_reprobe_before_drop_nonempty_kept(tmp_path: Path, monkeypatch) -> None:
    delete_called: list[str] = []
    monkeypatch.setattr(
        project_init, "_delete_class",
        lambda name, weaviate_url=None: delete_called.append(name),
    )
    # Re-probe returns N (a row filled in since detect) → must NOT drop.
    monkeypatch.setattr(
        project_init, "_http_count_objects",
        lambda name, url: 7,
    )

    candidates = [_cand("Old_CodeFunction", count=0)]  # empty at detect time
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )

    assert dropped == [], "a candidate non-empty at re-probe must NOT be dropped"
    assert remaining == candidates
    assert delete_called == [], "no _delete_class call when re-probe > 0"


def test_count_unknown_none_kept(tmp_path: Path, monkeypatch) -> None:
    """A candidate with object_count None (Weaviate unknown) is never dropped."""
    monkeypatch.setattr(
        project_init, "_delete_class",
        lambda name, weaviate_url=None: pytest.fail("must not drop unknown-count"),
    )
    candidates = [_cand("Old_CodeFunction", count=None)]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )
    assert dropped == []
    assert remaining == candidates


def test_reprobe_none_unreachable_kept(tmp_path: Path, monkeypatch) -> None:
    """Empty at detect but the pre-drop re-probe returns None (Weaviate
    unreachable) → conservative, NOT dropped."""
    monkeypatch.setattr(
        project_init, "_delete_class",
        lambda name, weaviate_url=None: pytest.fail("must not drop on None re-probe"),
    )
    monkeypatch.setattr(project_init, "_http_count_objects", lambda name, url: None)
    candidates = [_cand("Old_CodeFunction", count=0)]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )
    assert dropped == []
    assert remaining == candidates


def test_case_only_never_dropped(tmp_path: Path, monkeypatch) -> None:
    """A case_only candidate (BUG-1) is NEVER dropped even when empty — it
    refers to the SAME logical collection."""
    monkeypatch.setattr(
        project_init, "_delete_class",
        lambda name, weaviate_url=None: pytest.fail("case_only must never be dropped"),
    )
    # _http_count_objects should not even be consulted for a case_only cand.
    monkeypatch.setattr(
        project_init, "_http_count_objects",
        lambda name, url: pytest.fail("re-probe must not run for case_only"),
    )
    candidates = [_cand("old_codefunction", count=0, case_only=True)]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )
    assert dropped == []
    assert remaining == candidates


def test_post_drop_probe_still_populated_kept(tmp_path: Path, monkeypatch) -> None:
    """If the drop didn't take (post-probe still returns a positive count) the
    candidate stays in the remainder."""
    monkeypatch.setattr(
        project_init, "_delete_class",
        lambda name, weaviate_url=None: None,
    )
    # pre-drop 0, post-drop 3 (drop somehow failed to remove data).
    counts = iter([0, 3])
    monkeypatch.setattr(
        project_init, "_http_count_objects", lambda name, url: next(counts),
    )
    candidates = [_cand("Old_CodeFunction", count=0)]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )
    assert dropped == []
    assert remaining == candidates


def test_mixed_partition_drops_only_empty(tmp_path: Path, monkeypatch) -> None:
    """Mixed batch: one empty (dropped), one populated (kept), one case_only
    (kept)."""
    monkeypatch.setattr(
        project_init, "_delete_class", lambda name, weaviate_url=None: None,
    )
    # Only the empty candidate re-probes; sequence: pre=0, post=None.
    counts = iter([0, None])
    monkeypatch.setattr(
        project_init, "_http_count_objects", lambda name, url: next(counts),
    )
    candidates = [
        _cand("Empty_CodeFunction", count=0),
        _cand("Populated_CodeModule", count=42),
        _cand("case_codeclass", count=0, case_only=True),
    ]
    dropped, remaining = project_init._autodrop_empty_codegraph_candidates(
        tmp_path, candidates, URL,
    )
    assert dropped == ["Empty_CodeFunction"]
    remaining_names = {c["class_name"] for c in remaining}
    assert remaining_names == {"Populated_CodeModule", "case_codeclass"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
