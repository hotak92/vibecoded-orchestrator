# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F7 + D9: content-keyed template_review dismissal memory.

`template_review_pending` re-fires on every bundle update for a project whose
CLAUDE.md / CONTEXT_STATE.md / MEMORY.md diverge from the reference templates.
B-F7 gives the dismissal a MEMORY: at dismissal time we snapshot the sha256 of
each reference sidecar into `.claude/.vco-manifest.json` under
`dismissals.template_review_pending.reference_hashes`. The producer suppresses
re-emission WHILE every stored reference hash still matches the current sidecar;
it re-emits the moment VCO ships a genuinely new reference (any stored hash
changes, or a tracked sidecar appears/disappears).

Trio:
  * dismiss ⇒ hashes stored.
  * unchanged references ⇒ suppressed on next run.
  * ONE reference changed ⇒ re-emitted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0283_deferral_emit_fake import install_fake_deferral_emit  # noqa: E402

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# The reference-sidecar rel-paths, keyed by the template base name.
_REF_RELS = {
    "CLAUDE.md": Path(".claude") / "context" / "templates" / "CLAUDE.md.reference.md",
    "CONTEXT_STATE.md": Path(".claude") / "context" / "templates" / "CONTEXT_STATE.md.reference.md",
    "MEMORY.md": Path(".claude") / "context" / "templates" / "MEMORY.md.reference.md",
}


def _write_ref(folder: Path, base: str, body: str) -> None:
    p = folder / _REF_RELS[base]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _emit(folder: Path, diverged=("CLAUDE.md",)) -> None:
    project_init._emit_template_review_pending_deferral(
        folder, diverged_files=list(diverged),
    )


def _has_review(folder: Path) -> bool:
    return DeferralReport.read(folder).has_condition("template_review_pending")


def _dismiss(folder: Path) -> int:
    args = argparse.Namespace(
        folder=str(folder),
        condition_id="template_review_pending",
        json=True,
    )
    return project_init._cmd_dismiss_deferral(args)


# ---------------------------------------------------------------------------
# 1. dismiss ⇒ reference hashes stored in the manifest.
# ---------------------------------------------------------------------------

def test_dismiss_stores_reference_hashes(tmp_path: Path, capsys) -> None:
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _write_ref(tmp_path, "CONTEXT_STATE.md", "# state ref v1\n")
    _emit(tmp_path)
    assert _has_review(tmp_path)

    rc = _dismiss(tmp_path)
    capsys.readouterr()  # drain
    assert rc == 0

    manifest = json.loads(
        (tmp_path / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
    )
    stored = manifest["dismissals"]["template_review_pending"]["reference_hashes"]
    # Both existing sidecars hashed; MEMORY.md sidecar absent → omitted.
    assert set(stored.keys()) == {"CLAUDE.md", "CONTEXT_STATE.md"}
    assert "dismissed_at" in manifest["dismissals"]["template_review_pending"]
    # schema_version stays 2 (additive key).
    assert manifest["schema_version"] == 2


# ---------------------------------------------------------------------------
# 2. unchanged references ⇒ suppressed on the next run.
# ---------------------------------------------------------------------------

def test_unchanged_references_suppress_next_emit(tmp_path: Path, capsys) -> None:
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _emit(tmp_path)
    assert _has_review(tmp_path)
    _dismiss(tmp_path)
    capsys.readouterr()

    # Producer runs again on the next update — references unchanged ⇒ suppressed.
    _emit(tmp_path)
    assert not _has_review(tmp_path), (
        "unchanged references since dismissal must suppress re-emission"
    )


# ---------------------------------------------------------------------------
# 3. ONE reference changed ⇒ re-emitted.
# ---------------------------------------------------------------------------

def test_one_reference_changed_reemits(tmp_path: Path, capsys) -> None:
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _write_ref(tmp_path, "CONTEXT_STATE.md", "# state ref v1\n")
    _emit(tmp_path)
    _dismiss(tmp_path)
    capsys.readouterr()

    # VCO ships a NEW CLAUDE.md reference (the sidecar content changes).
    _write_ref(tmp_path, "CLAUDE.md", "# reference v2 — new section shipped\n")

    _emit(tmp_path)
    assert _has_review(tmp_path), (
        "a changed reference sidecar must re-emit the nudge"
    )


def test_new_reference_sidecar_appears_reemits(tmp_path: Path, capsys) -> None:
    """A sidecar that did NOT exist at dismissal time but appears later is new
    content → re-emit."""
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _emit(tmp_path)
    _dismiss(tmp_path)
    capsys.readouterr()

    # A new MEMORY.md reference sidecar appears.
    _write_ref(tmp_path, "MEMORY.md", "# memory ref v1\n")
    _emit(tmp_path)
    assert _has_review(tmp_path)


def test_no_dismissal_recorded_never_suppresses(tmp_path: Path) -> None:
    """Without a recorded dismissal, the producer always emits (no memory)."""
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _emit(tmp_path)
    assert _has_review(tmp_path)


def test_dismiss_json_payload_and_stderr_contract_preserved(tmp_path: Path, capsys) -> None:
    """The D9 hash-write must NOT perturb the dismiss command's JSON payload or
    stderr-quiet contract (JSON mode)."""
    _write_ref(tmp_path, "CLAUDE.md", "# reference v1\n")
    _emit(tmp_path)
    rc = _dismiss(tmp_path)
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload == {
        "dismissed": True,
        "condition_id": "template_review_pending",
        "remaining": 0,
        "reason": "dismissed",
    }
    assert err == "", "JSON mode must keep stderr quiet even with the D9 hook"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
