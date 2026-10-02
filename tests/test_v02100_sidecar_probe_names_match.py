# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 U17 — the ``orchestrator_user_modified_preserved`` entry and its
clear probe name THE SAME FILES.

Field case (genericized): the entry named only ``CLAUDE.md.from-upstream-<sha>``
(gone), yet stayed "still applies" because the probe's whole-root sweep found
three UNRELATED older ``knowledge/…md.from-upstream-*`` sidecars it never named.

The machine-readable list format is shared with the Rust emitter through
``tests/fixtures/sidecar_list_line.json`` (Rust side:
``git_user_editable_merge.rs::sidecar_list_line_matches_the_shared_fixture_and_is_never_capped``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_probes as dp  # noqa: E402
from vco_lib.deferral_report import DeferralEntry  # noqa: E402

FIXTURE = json.loads((REPO_ROOT / "tests/fixtures/sidecar_list_line.json").read_text())
UNRELATED = ("knowledge/concepts/gui-tabs.md.from-upstream-4c44eb8",
             "knowledge/concepts/gui-tabs.md.from-upstream-a5b2971",
             "knowledge/concepts/gui-tabs.md.from-upstream-d77e53e")


def _entry(sidecars: list, *, prose: str = "") -> DeferralEntry:
    """The Rust emitter's shape: bullets, then the machine-readable line."""
    line = next(c["line"] for c in FIXTURE["cases"] if c["sidecars"] == sidecars) \
        if any(c["sidecars"] == sidecars for c in FIXTURE["cases"]) else \
        "<!-- vco-sidecars: " + json.dumps(sidecars, separators=(",", ":")) + " -->"
    return DeferralEntry(
        condition_id="orchestrator_user_modified_preserved",
        title="1 orchestrator-root file preserved/merged during update",
        detected=f"VCO ran a per-path 3-way merge before `git pull`:\n{prose}\n{line}",
        why_deferred="w", command_to_apply="c", severity="info")


def _touch(root: Path, rel: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("x", encoding="utf-8")


def _probe(root: Path, entry) -> object:
    return dp.orchestrator_sidecars_still_present(dp.ProbeContext(folder=root, entry=entry))


def test_marker_and_every_fixture_line_parse_to_the_listed_names():
    assert FIXTURE["marker"] == dp.SIDECAR_LIST_MARKER
    for case in FIXTURE["cases"]:
        entry = DeferralEntry(condition_id="x", title="t", detected=case["line"],
                              why_deferred="w", command_to_apply="c")
        assert dp.machine_sidecar_list(entry) == tuple(case["parsed"]), case["name"]


def test_field_case_entry_clears_when_its_named_file_is_gone(tmp_path):
    """ACT: the entry named files; they are gone; unrelated sidecars parked by
    OTHER merges do not keep it (RED before U17: the sweep answered True)."""
    for rel in UNRELATED:
        _touch(tmp_path, rel)
    rendered_only = DeferralEntry(
        condition_id="orchestrator_user_modified_preserved", title="t",
        detected="  - `CLAUDE.md` — conflict; upstream saved as `CLAUDE.md.from-upstream-89a5530`",
        why_deferred="w", command_to_apply="c", severity="info")
    assert _probe(tmp_path, rendered_only) is False
    assert _probe(tmp_path, _entry(["docs/guide.md.from-upstream-4c44eb8"])) is False


def test_entry_stays_while_a_file_it_names_remains(tmp_path):
    """LEAVE-ALONE: a named sidecar still on disk keeps the entry."""
    _touch(tmp_path, "docs/guide.md.from-upstream-4c44eb8")
    assert _probe(tmp_path, _entry(["knowledge/concepts/gui-tabs.md.from-upstream-4c44eb8",
                                    "docs/guide.md.from-upstream-4c44eb8"])) is True


def test_the_machine_list_is_complete_past_the_display_cap(tmp_path):
    """A prose list cut at 100 bullets is unknown; the machine list is the
    complete set, so its entry can clear — and a name beyond the cap counts."""
    names = [f"knowledge/n{i}.md.from-upstream-7b255dd" for i in range(105)]
    entry = _entry(names, prose="  - ... and 5 more")
    assert _probe(tmp_path, entry) is False
    _touch(tmp_path, names[104])
    assert _probe(tmp_path, entry) is True


def test_windows_separators_in_the_list_resolve(tmp_path):
    case = next(c for c in FIXTURE["cases"] if "windows" in c["name"])
    _touch(tmp_path, case["parsed"][0])
    entry = DeferralEntry(condition_id="orchestrator_user_modified_preserved", title="t",
                          detected=case["line"], why_deferred="w", command_to_apply="c")
    assert _probe(tmp_path, entry) is True


def test_list_less_legacy_entry_still_sweeps(tmp_path):
    """The sweep keeps its one job: an entry that named NOTHING."""
    entry = DeferralEntry(condition_id="orchestrator_user_modified_preserved", title="t",
                          detected="1 file auto-merged (3-way)", why_deferred="w",
                          command_to_apply="c", severity="info")
    assert _probe(tmp_path, entry) is False
    _touch(tmp_path, UNRELATED[0])
    assert _probe(tmp_path, entry) is True
