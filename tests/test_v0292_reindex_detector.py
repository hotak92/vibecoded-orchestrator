# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the extractor-generation DETECTOR (pure) + its stamp.

The decision "does this project's code graph owe a re-index because it was
built by an older EXTRACTOR" is pure, so it is pinned here without any I/O.
Every branch is tested for BOTH outcomes (repo rule: test the act AND the
leave-alone), because each one gates real work:

* a wrong YES costs one cheap re-extraction pass (no embeds — proved in
  ``test_v0292_reindex_analyzer_gate.py``);
* a wrong NO leaves the user permanently missing every Python ``CodeAPI`` row
  with nothing anywhere to tell them. The asymmetry is why every UNKNOWN
  resolves to YES.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import codegraph_extractor_generation as ceg


# ---------------------------------------------------------------------------
# The shared "was this built by an older version of us" primitive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prev,running,bump,expected",
    [
        ("0.2.91", "0.2.92", "0.2.92", True),    # exactly onto the bump
        ("0.2.50", "0.2.95", "0.2.92", True),    # straddles it
        ("0.2.92", "0.2.93", "0.2.92", False),   # already at/past it
        ("0.2.93", "0.2.95", "0.2.92", False),
        ("0.2.92", "0.2.92", "0.2.92", False),   # no upgrade at all
        ("", "0.2.92", "0.2.92", False),         # unparseable ⇒ not proof
        ("0.2.91", "", "0.2.92", False),
        ("nonsense", "0.2.92", "0.2.92", False),
        ("0.2", "0.2.92", "0.2.92", False),      # 2-part is not semver here
    ],
)
def test_crosses_version_boundary(prev, running, bump, expected):
    assert ceg.crosses_version_boundary(prev, running, bump) is expected


def test_chunker_boundary_uses_the_shared_primitive():
    """``project_init`` must not carry its own copy of the rule (modularity).

    Both boundary questions in this codebase — the v0.2.46 chunker preset and
    the v0.2.92 extractor generation — are ``prev < bump <= running``. Pinning
    the delegation here is what stops a future edit to one from silently
    diverging from the other.
    """
    from vco_lib import project_init

    assert project_init._crosses_chunker_boundary("0.2.45", "0.2.46") is True
    assert project_init._crosses_chunker_boundary("0.2.46", "0.2.92") is False
    # And the parser is the same one.
    assert project_init._parse_semver("0.2.91") == ceg.parse_semver("0.2.91")
    assert project_init._parse_semver("garbage") is None


# ---------------------------------------------------------------------------
# generation_is_current
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "generation,expected",
    [
        ("0.2.92", True),
        ("0.2.93", True),      # ahead of the ladder is still satisfied
        ("0.2.91", False),
        ("0.2.0", False),
        (None, False),         # unknown is never "current"
        ("", False),
        ("not-a-version", False),
    ],
)
def test_generation_is_current(generation, expected):
    assert ceg.generation_is_current(generation, ("0.2.92",)) is expected


# ---------------------------------------------------------------------------
# decide() — the whole matrix
# ---------------------------------------------------------------------------
def _decide(**kw):
    base = dict(prev_version="0.2.91", running_version="0.2.92",
                stamp_generation=None, graph_exists=True, bumps=("0.2.92",))
    base.update(kw)
    return ceg.decide(**base)


def test_already_reindexed_project_says_no():
    """LEAVE-ALONE: a completion stamp at the current generation ends it."""
    v = _decide(stamp_generation="0.2.92")
    assert v.needs_reindex is False
    assert v.reason == ceg.REASON_STAMP_CURRENT
    assert v.stamp_now is False        # nothing to re-write


def test_stamp_from_an_older_generation_still_says_yes():
    v = _decide(stamp_generation="0.2.91")
    assert v.needs_reindex is True


def test_project_with_no_stamp_says_yes():
    """ACT: the safe default. No recorded generation + a graph ⇒ owed."""
    v = _decide(stamp_generation=None, prev_version="", running_version="0.2.92")
    assert v.needs_reindex is True
    assert v.reason == ceg.REASON_UNKNOWN_GENERATION


def test_unparseable_manifest_version_says_yes():
    v = _decide(prev_version="whatever-this-is")
    assert v.needs_reindex is True
    assert v.reason == ceg.REASON_UNKNOWN_GENERATION


def test_crossing_the_bump_says_yes():
    v = _decide(prev_version="0.2.91", running_version="0.2.92")
    assert v.needs_reindex is True
    assert v.reason == ceg.REASON_CROSSES_BUMP


def test_project_built_at_or_past_the_bump_says_no_and_stamps():
    """A graph produced by a >=0.2.92 analyzer already has the fixes."""
    v = _decide(prev_version="0.2.92", running_version="0.2.95")
    assert v.needs_reindex is False
    assert v.reason == ceg.REASON_VERSION_AT_OR_PAST_BUMP
    assert v.stamp_now is True          # so we stop asking every update


def test_no_code_graph_says_no_and_stamps():
    """Nothing to repair — and a first install must NOT trigger a full build."""
    v = _decide(graph_exists=False, prev_version="", running_version="0.2.92")
    assert v.needs_reindex is False
    assert v.reason == ceg.REASON_NO_GRAPH
    assert v.stamp_now is True


def test_unprobeable_weaviate_does_not_become_absent():
    """``None`` means COULD NOT CHECK, which must never read as "no graph".

    Conflating the two is the F-1 defect restated: an offline install would
    stamp every project as done and permanently skip the repair.
    """
    v = _decide(graph_exists=None, prev_version="0.2.91")
    assert v.needs_reindex is True
    assert v.reason == ceg.REASON_CROSSES_BUMP


def test_a_verdict_never_both_acts_and_stamps():
    """``stamp_now`` short-circuits FUTURE runs; only a completed walk may
    certify the graph. The two must never be set together."""
    for prev in ("", "0.2.0", "0.2.91", "0.2.92", "9.9.9", "junk"):
        for exists in (True, False, None):
            for stamp in (None, "0.2.91", "0.2.92"):
                v = _decide(prev_version=prev, graph_exists=exists,
                            stamp_generation=stamp)
                assert not (v.needs_reindex and v.stamp_now), (prev, exists, stamp)


# ---------------------------------------------------------------------------
# The stamp file
# ---------------------------------------------------------------------------
def test_stamp_roundtrip(tmp_path: Path):
    assert ceg.read_stamp_generation(tmp_path) is None      # absent
    assert ceg.write_stamp(tmp_path, "0.2.92") is True
    assert ceg.read_stamp_generation(tmp_path) == "0.2.92"
    payload = json.loads(ceg.stamp_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["generation"] == "0.2.92"
    assert payload["written_at"].endswith("Z")


def test_stamp_lives_under_a_walk_excluded_directory():
    """``.claude/state/`` is excluded from code-graph walks, so the stamp can
    never index itself into the graph it describes."""
    assert ceg.STAMP_REL.parts[:2] == (".claude", "state")


def test_corrupt_stamp_reads_as_absent_not_current(tmp_path: Path):
    """A truncated / hand-mangled stamp must fail toward OWED."""
    p = ceg.stamp_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"generation": "0.2.9', encoding="utf-8")   # truncated JSON
    assert ceg.read_stamp_generation(tmp_path) is None
    assert _decide(stamp_generation=ceg.read_stamp_generation(tmp_path)
                   ).needs_reindex is True


def test_stamp_with_wrong_shape_reads_as_absent(tmp_path: Path):
    p = ceg.stamp_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    for body in ('["0.2.92"]', '{"generation": 292}', '{}', '{"generation": " "}'):
        p.write_text(body, encoding="utf-8")
        assert ceg.read_stamp_generation(tmp_path) is None, body


def test_write_stamp_soft_fails(tmp_path: Path, monkeypatch):
    """A stamp that cannot be written returns False — never raises. The project
    simply stays owed and is asked again next update."""
    def _boom(*_a, **_kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("vco_lib.atomic.atomic_write_json", _boom)
    assert ceg.write_stamp(tmp_path, "0.2.92") is False


# ---------------------------------------------------------------------------
# code_graph_exists — tri-state, and it reuses the ONE schema probe
# ---------------------------------------------------------------------------
def test_code_graph_exists_is_tri_state():
    def probe_present(names, _url):
        return {n: n.endswith("CodeFunction") for n in names}

    def probe_absent(names, _url):
        return {n: False for n in names}

    def probe_unknown(names, _url):
        return {n: None for n in names}

    assert ceg.code_graph_exists("DemoProj", probe=probe_present) is True
    assert ceg.code_graph_exists("DemoProj", probe=probe_absent) is False
    assert ceg.code_graph_exists("DemoProj", probe=probe_unknown) is None
    assert ceg.code_graph_exists("", probe=probe_present) is None


def test_code_graph_exists_never_requires_codeapi():
    """CodeAPI's absence is the SYMPTOM being repaired. Requiring it as
    evidence of "has a graph" would make the detector answer "no graph" for
    exactly the projects that need the walk."""
    seen = {}

    def probe(names, _url):
        seen["names"] = list(names)
        return {n: True for n in names}

    ceg.code_graph_exists("DemoProj", probe=probe)
    assert not any(n.endswith("CodeAPI") for n in seen["names"])
    assert any(n.endswith("CodeFunction") for n in seen["names"])


def test_code_graph_exists_soft_fails_on_a_raising_probe():
    def probe(_names, _url):
        raise RuntimeError("weaviate exploded")

    assert ceg.code_graph_exists("DemoProj", probe=probe) is None


# ---------------------------------------------------------------------------
# plan() — the I/O half, short-circuits before probing when it can
# ---------------------------------------------------------------------------
def test_plan_does_not_probe_weaviate_when_the_stamp_is_current(tmp_path: Path):
    calls = []

    def probe(names, _url):
        calls.append(names)
        return {n: True for n in names}

    # v0.2.93 release-time pin move: "current stamp" means AT the newest
    # ladder bump — derive it so this stays true at every future append.
    ceg.write_stamp(tmp_path, ceg.EXTRACTOR_GENERATION_BUMPS[-1])
    v = ceg.plan(tmp_path, prev_version="0.2.50",
                 running_version=ceg.EXTRACTOR_GENERATION_BUMPS[-1],
                 project_name="DemoProj", probe=probe)
    assert v.needs_reindex is False
    assert calls == [], "a current stamp must answer without touching Weaviate"


def test_plan_probes_when_the_stamp_is_absent(tmp_path: Path):
    calls = []

    def probe(names, _url):
        calls.append(names)
        return {n: False for n in names}

    v = ceg.plan(tmp_path, prev_version="0.2.50", running_version="0.2.92",
                 project_name="DemoProj", probe=probe)
    assert calls, "an absent stamp must consult the graph before concluding"
    assert v.reason == ceg.REASON_NO_GRAPH


# ---------------------------------------------------------------------------
# The generation ladder itself
# ---------------------------------------------------------------------------
def test_ladder_is_ordered_and_current_is_its_head():
    parsed = [ceg.parse_semver(v) for v in ceg.EXTRACTOR_GENERATION_BUMPS]
    assert all(p is not None for p in parsed), ceg.EXTRACTOR_GENERATION_BUMPS
    assert parsed == sorted(parsed), "bumps must be oldest → newest"
    assert ceg.CURRENT_EXTRACTOR_GENERATION == ceg.EXTRACTOR_GENERATION_BUMPS[-1]
    # v0.2.94: STRICTLY increasing, and therefore unique. The module's rule is
    # "append, never edit", and appending a version that is already in the list
    # (or below its head) is the accident that rule invites — a duplicate would
    # be silently inert (`generation_is_current` reads only the head), so
    # nothing else in the system would notice.
    assert len(set(ceg.EXTRACTOR_GENERATION_BUMPS)) == len(
        ceg.EXTRACTOR_GENERATION_BUMPS
    ), f"duplicate entry in the ladder: {ceg.EXTRACTOR_GENERATION_BUMPS}"
    assert all(
        a < b for a, b in zip(parsed, parsed[1:])
    ), f"ladder must be STRICTLY increasing: {ceg.EXTRACTOR_GENERATION_BUMPS}"


def test_the_0292_bump_is_declared():
    """The v0.2.92 Python-CodeAPI + C#-route fixes are what this ladder is for.
    Dropping the entry would silently stop repairing every existing graph."""
    assert "0.2.92" in ceg.EXTRACTOR_GENERATION_BUMPS
