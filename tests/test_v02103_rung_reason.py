# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 small-fix A — the re-index warning names the triggering RUNG.

The bundle update's "re-index started in the background" warning used to
hard-code v0.2.92's reason ("added Python API extraction and fixed C#
route attribution") for EVERY extractor-generation re-index, whatever rung
actually triggered it. The ladder / :func:`decide` knows the real trigger;
these tests pin that the user-facing warning follows it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vco_lib import codegraph_extractor_generation as ceg
from vco_lib import project_init


class _LaunchedSpawn:
    """Stand-in for ``spawn_background_resync``: everything launched."""

    def __call__(self, *_args, **_kwargs):
        return self

    status = "launched"
    pid = 4242
    message = ""


def _drive_bundle_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                         prev_version: str, running_version: str) -> list[str]:
    """Run the ONE bundle-update shim end-to-end (real decision + real
    trigger wiring, fake graph probe + fake spawn) and return its warnings."""
    import vco_lib.codegraph_resync as _resync

    monkeypatch.setattr(ceg, "code_graph_exists", lambda *a, **k: True)
    monkeypatch.setattr(_resync, "spawn_background_resync", _LaunchedSpawn())
    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name",
                        lambda _f: "DemoProj")
    result: dict = {"warnings": []}

    def log(_step, _phase, _detail="", *, data=None):  # noqa: ARG001
        pass

    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version=prev_version, running_version=running_version,
        is_root_target=False, result=result, log=log)
    return [w for w in result["warnings"] if "re-index started" in w]


def test_two_rungs_two_different_messages(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch):
    """The warning names the rung that crossed, not a fixed release."""
    v92 = _drive_bundle_update(tmp_path, monkeypatch,
                               prev_version="0.2.91", running_version="0.2.92")
    v102 = _drive_bundle_update(tmp_path, monkeypatch,
                                prev_version="0.2.101", running_version="0.2.102")

    assert len(v92) == 1 and len(v102) == 1
    assert "v0.2.92" in v92[0]
    assert "v0.2.102" in v102[0]
    assert v92[0] != v102[0], "each rung must carry its own reason"
    # The old defect: v0.2.92's reason shown for every trigger.
    assert "v0.2.92 added Python API extraction" not in v102[0]


def test_unknown_generation_falls_back_to_generic_wording(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No known rung (no recorded version) → generic wording, never a
    specific release's reason presented as the trigger."""
    warns = _drive_bundle_update(tmp_path, monkeypatch,
                                 prev_version="", running_version="0.2.102")
    assert len(warns) == 1
    assert "v0.2.92" not in warns[0]
    assert "v0.2.102 added" not in warns[0]      # no rung claimed as trigger
    assert "extractor generation" in warns[0]     # generic fallback shown


def test_every_rung_has_a_user_facing_reason():
    """A ladder append without its reason text would silently regress every
    future message to the generic fallback — keep them in lockstep."""
    assert set(ceg.EXTRACTOR_GENERATION_RUNG_REASONS) == set(
        ceg.EXTRACTOR_GENERATION_BUMPS)
    for bump, text in ceg.EXTRACTOR_GENERATION_RUNG_REASONS.items():
        assert text.strip(), f"rung {bump} has an empty reason"


def test_describe_rung_shapes_and_fallbacks():
    described = ceg.describe_rung("0.2.92")
    assert described is not None and described.startswith("v0.2.92 ")
    assert ceg.describe_rung("9.9.9") is None        # unknown rung
    assert ceg.describe_rung(None) is None           # unknown generation


def test_verdict_carries_the_crossed_bump():
    verdict = ceg.decide(prev_version="0.2.101", running_version="0.2.102",
                         stamp_generation=None, graph_exists=True)
    assert verdict.reason == ceg.REASON_CROSSES_BUMP
    assert verdict.bump == "0.2.102"
    # Unknown-generation verdicts name no rung.
    unknown = ceg.decide(prev_version="", running_version="0.2.102",
                         stamp_generation=None, graph_exists=True)
    assert unknown.reason == ceg.REASON_UNKNOWN_GENERATION
    assert unknown.bump is None
