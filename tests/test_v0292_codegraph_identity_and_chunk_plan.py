# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the two code-graph write-path defects, at the PURE-GUARD level.

DEFECT A — ``guards.plan_chunk_texts``: the chunk DECISION, split out of
``_maybe_chunk_and_write`` so ``_dedup_insert`` can take it BEFORE the
embed-skip resolver. The resolver hashes the full body as a single chunk, which
can never match a multi-chunk entity's stored chunk-0 hash, so it always
embedded and the fan-out always discarded that vector: one wasted MAX-SIZE
embed per multi-chunk entity per walk.

DEFECT B — ``guards.assign_duplicate_identity_suffixes``: two same-named
symbols in ONE file shared a deterministic UUID, so the later write silently
overwrote the earlier.

The analyzer-level wiring (and the "did the embed actually go away" /
"do chunked rows still carry their stamps" questions) is in
``tests/test_v0292_codegraph_write_path.py``. This file pins the pure rules.

RISK COVERAGE (from PLAN-v0292-codegraph-write-path §2.5 / §3.6):
  * B-RISK-1 — ``#n`` must never enter the ``::n`` chunk-key space.
  * B-RISK-2 — the suffix assignment must be INJECTIVE on pathological input.
  * B-RISK-3 — it must be a pure function of the input sequence.
  * A — the chunker is called exactly once, with the shared budget rule
    (``chunk_or_truncate_*``) owning "is this over budget", never a local
    threshold.
  * §4.1 — ``all_chunks_skippable`` moved to ``codegraph_guards`` and gains the
    direct unit test it never had.
"""
from __future__ import annotations

import inspect

import pytest

from vco_lib import codegraph_guards as guards
from vco_lib.codegraph_guards import RowAction


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT B — assign_duplicate_identity_suffixes
# ═══════════════════════════════════════════════════════════════════════════


def _final_keys(keyed):
    """The identity key each entity ACTUALLY ends up with."""
    suffixes = guards.assign_duplicate_identity_suffixes(keyed)
    assert len(suffixes) == len(keyed)
    return [
        (kind, sfx if sfx is not None else key)
        for (kind, key), sfx in zip(keyed, suffixes)
    ]


def test_no_duplicates_means_no_overrides_at_all():
    """The 96.9%-of-rows case: nothing repeats → every entity keeps its bare
    key → every stored UUID is byte-identical to today's. This is the property
    that makes the migration a pure add."""
    keyed = [("f", "m.a"), ("f", "m.b"), ("c", "m.C"), ("api", "/x:GET")]
    assert guards.assign_duplicate_identity_suffixes(keyed) == [None] * 4


def test_first_occurrence_keeps_the_bare_key():
    """FIRST-wins, not last-wins: occurrence 1 keeps the bare key so its UUID
    is unchanged, and appending a new duplicate later never re-keys it."""
    two = guards.assign_duplicate_identity_suffixes([("f", "m.f"), ("f", "m.f")])
    assert two == [None, "m.f#2"]
    three = guards.assign_duplicate_identity_suffixes(
        [("f", "m.f"), ("f", "m.f"), ("f", "m.f")]
    )
    # Stable under append: the first two assignments are unchanged.
    assert three == [None, "m.f#2", "m.f#3"]
    assert three[:2] == two


def test_suffix_uses_hash_never_the_chunk_key_separator():
    """B-RISK-1, half one: the override alphabet. ``::<int>`` is the CHUNK key
    space (``chunk_identities`` keys chunk i on ``<key>::<i>``), so an
    occurrence suffix of ``::2`` would mint the same UUID as chunk 1 of
    occurrence 1 — a wrong body on a canonical-looking row."""
    out = guards.assign_duplicate_identity_suffixes(
        [("f", "m.f")] * 4 + [("c", "m.C")] * 3
    )
    overrides = [s for s in out if s is not None]
    assert overrides, "expected overrides for the repeated keys"
    for sfx in overrides:
        assert "#" in sfx, sfx
        assert "::" not in sfx, (
            f"{sfx!r} uses the chunk-key separator — it would collide with a "
            "chunk UUID of the first occurrence"
        )


def test_occurrence_uuids_never_collide_with_any_chunk_uuid():
    """B-RISK-1, the real property: build the FULL uuid set for 2 occurrences
    of one key, each chunked into 3, and assert it has no duplicates.

    This is the assertion that would have caught a ``::2`` suffix: with
    ``::``, occurrence 2's bare uuid equals ``uuid("m.f::2")`` == chunk 2 of
    occurrence 1.
    """
    import uuid as _uuid

    def det(key: str) -> str:
        return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"Proj|src/m.py|{key}|"))

    suffixes = guards.assign_duplicate_identity_suffixes([("f", "m.f"), ("f", "m.f")])
    identity_keys = [s if s is not None else "m.f" for s in suffixes]

    all_uuids = []
    for ik in identity_keys:
        uuids, _ = guards.chunk_identities(
            ["c0", "c1", "c2"], {"function_body": ""}, True, ik, 3,
            uuid_fn=det, hash_fn=lambda p: "h",
        )
        all_uuids.extend(uuids)

    assert len(all_uuids) == 6
    assert len(set(all_uuids)) == 6, (
        "an occurrence uuid collided with a chunk uuid — the two identity "
        f"suffix alphabets are not disjoint: {all_uuids}"
    )


def test_injective_on_pathological_keys_that_already_contain_the_suffix():
    """B-RISK-2: a key set that already contains ``a#2`` / ``a#2#2`` / ``a#3``
    with multiplicities must still map to a duplicate-free set of final keys.
    The escalation loop makes this true by construction, not by argument."""
    keyed = (
        [("f", "a")] * 3
        + [("f", "a#2")] * 2
        + [("f", "a#2#2")]
        + [("f", "a#3")] * 2
        + [("f", "a")]
    )
    final = _final_keys(keyed)
    assert len(set(final)) == len(final), f"not injective: {final}"


def test_injective_under_random_multiplicities():
    """B-RISK-2, generative: 200 pseudo-random key sequences drawn from an
    alphabet seeded with collision-shaped names. Injectivity must hold for
    every one."""
    import random

    alphabet = ["a", "b", "a#2", "a#3", "b#2", "m.f", "m.f#2"]
    rng = random.Random(20260902)
    for _ in range(200):
        keyed = [
            (rng.choice(("f", "c")), rng.choice(alphabet))
            for _ in range(rng.randint(1, 25))
        ]
        final = _final_keys(keyed)
        assert len(set(final)) == len(final), f"not injective for {keyed}"


def test_kind_scopes_the_grouping():
    """UUIDs are per-Weaviate-class, so a CodeClass and a CodeFunction with the
    same key land in different collections and never collide. Disambiguating
    them would needlessly change a UUID."""
    assert guards.assign_duplicate_identity_suffixes(
        [("c", "m.X"), ("f", "m.X")]
    ) == [None, None]


def test_is_a_pure_function_of_the_input_sequence():
    """B-RISK-3: identity must not flip between walks, or the churn is worse
    than the defect. Same input → same output, and no hidden state carries
    between calls."""
    keyed = [("f", "m.f"), ("f", "m.g"), ("f", "m.f"), ("c", "m.f")]
    first = guards.assign_duplicate_identity_suffixes(keyed)
    second = guards.assign_duplicate_identity_suffixes(keyed)
    third = guards.assign_duplicate_identity_suffixes(list(keyed))
    assert first == second == third == [None, None, "m.f#2", None]


def test_empty_input():
    assert guards.assign_duplicate_identity_suffixes([]) == []


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT A — plan_chunk_texts
# ═══════════════════════════════════════════════════════════════════════════


def _boom(*a, **kw):  # pragma: no cover — must never be called
    raise AssertionError("chunker called for a non-chunkable entity")


@pytest.mark.parametrize(
    "coll", ["P_CodeModule", "P_CodeAPI", "P_CodeInteraction", "", "Weird"]
)
def test_non_chunkable_collection_returns_none_without_calling_the_chunker(coll):
    assert guards.plan_chunk_texts(
        coll, {"function_body": "x"}, "m.f",
        language_fallback="python", model_fn=_boom,
        chunk_fn=_boom, chunk_class_fn=_boom,
    ) is None


@pytest.mark.parametrize("props", [None, "not a dict", 7, []])
def test_non_dict_props_returns_none(props):
    assert guards.plan_chunk_texts(
        "P_CodeFunction", props, "m.f",
        language_fallback="python", model_fn=_boom,
        chunk_fn=_boom, chunk_class_fn=_boom,
    ) is None


@pytest.mark.parametrize(
    "coll,props",
    [
        ("P_CodeFunction", {"function_body": ""}),
        ("P_CodeFunction", {}),
        ("P_CodeClass", {"class_body": None}),
        # Right body key for the WRONG kind → still "no body" for this kind.
        ("P_CodeFunction", {"class_body": "class C: ..."}),
    ],
)
def test_missing_body_returns_none_without_calling_the_chunker(coll, props):
    assert guards.plan_chunk_texts(
        coll, props, "m.f",
        language_fallback="python", model_fn=_boom,
        chunk_fn=_boom, chunk_class_fn=_boom,
    ) is None


def test_in_budget_entity_returns_exactly_one_text():
    calls = []

    def _chunk(sig, body, **kw):
        calls.append((sig, body, kw))
        return ["only one"]

    out = guards.plan_chunk_texts(
        "P_CodeFunction",
        {"function_body": "def f(): ...", "signature": "def f()"},
        "m.f",
        language_fallback="python", model_fn=lambda: "codesage",
        chunk_fn=_chunk, chunk_class_fn=_boom,
    )
    assert out == ["only one"]
    assert len(calls) == 1, "the chunker must be called EXACTLY once"
    assert calls[0][0] == "def f()" and calls[0][1] == "def f(): ..."
    assert calls[0][2] == {
        "language": "python", "model": "codesage", "full_name": "m.f",
    }


def test_over_budget_entity_returns_n_texts():
    out = guards.plan_chunk_texts(
        "P_CodeFunction",
        {"function_body": "b", "signature": "s"},
        "m.f",
        language_fallback="rust", model_fn=lambda: "M",
        chunk_fn=lambda s, b, **kw: ["c0", "c1", "c2"], chunk_class_fn=_boom,
    )
    assert out == ["c0", "c1", "c2"]


def test_class_path_uses_the_class_chunker_and_forwards_methods():
    seen = {}

    def _chunk_class(sig, body, **kw):
        seen.update(sig=sig, body=body, **kw)
        return ["c0", "c1"]

    out = guards.plan_chunk_texts(
        "P_CodeClass",
        {"class_body": "class C: ...", "signature": "class C",
         "methods": ["a", "b"], "language": "java"},
        "m.C",
        language_fallback="python", model_fn=lambda: "M",
        chunk_fn=_boom, chunk_class_fn=_chunk_class,
    )
    assert out == ["c0", "c1"]
    assert seen["methods"] == ["a", "b"]
    # Explicit props language WINS over the analyzer's _current_language.
    assert seen["language"] == "java"


def test_language_falls_back_to_the_stamp_value_then_to_python():
    """The hoist runs BEFORE the analyzer stamps ``props['language']``, so this
    fallback must be the SAME value the stamp would have written
    (``_current_language``) — otherwise the decision could chunk differently
    from the write."""
    seen = {}

    def _chunk(sig, body, **kw):
        seen.update(kw)
        return ["x"]

    guards.plan_chunk_texts(
        "P_CodeFunction", {"function_body": "b"}, "m.f",
        language_fallback="rust", model_fn=lambda: "M",
        chunk_fn=_chunk, chunk_class_fn=_boom,
    )
    assert seen["language"] == "rust"

    guards.plan_chunk_texts(
        "P_CodeFunction", {"function_body": "b"}, "m.f",
        language_fallback="", model_fn=lambda: "M",
        chunk_fn=_chunk, chunk_class_fn=_boom,
    )
    assert seen["language"] == "python"


def test_empty_chunker_result_is_none():
    assert guards.plan_chunk_texts(
        "P_CodeFunction", {"function_body": "b"}, "m.f",
        language_fallback="python", model_fn=lambda: "M",
        chunk_fn=lambda s, b, **kw: [], chunk_class_fn=_boom,
    ) is None


def test_chunker_exception_propagates_out_of_the_pure_guard():
    """The guard makes NO policy about failure — the analyzer seam decides
    whether a raise is soft (the hoist: fall back to today's order) or hard
    (the fan-out: raise exactly as it always has). Swallowing here would take
    that choice away from the caller."""
    with pytest.raises(RuntimeError):
        guards.plan_chunk_texts(
            "P_CodeFunction", {"function_body": "b"}, "m.f",
            language_fallback="python", model_fn=lambda: "M",
            chunk_fn=lambda s, b, **kw: (_ for _ in ()).throw(RuntimeError("x")),
            chunk_class_fn=_boom,
        )


def test_plan_chunk_texts_owns_no_size_threshold():
    """The budget rule lives in ``chunk_or_truncate_*`` →
    ``Chunker.for_model`` → ``chunking_preset_for_model``. A second copy of the
    max-tokens comparison here would let the DECISION plan N chunks while the
    WRITE produced M — silent and data-shaped, worse than the wasted embed
    being removed. Pin the absence."""
    src = inspect.getsource(guards.plan_chunk_texts)
    for forbidden in (
        "max_tokens", "num_ctx", "chunking_preset", "_preset_for_limit",
        "Chunker", "_max_chars", "len(body)", "len(text)",
    ):
        assert forbidden not in src, (
            f"plan_chunk_texts re-derives the chunk budget ({forbidden!r}); it "
            "must get that answer only from the injected chunker"
        )


# ═══════════════════════════════════════════════════════════════════════════
# §4.1 — all_chunks_skippable, moved to guards and finally unit-tested
# ═══════════════════════════════════════════════════════════════════════════

_CUR, _FLOOR, _VLESS = 5, 3, 0


def _fp(h, rev, total=3):
    return {"content_hash": h, "embed_revision": rev, "total_chunks": total}


def _skippable(fps, hashes, total=3):
    return guards.all_chunks_skippable(
        fps, hashes, total,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    )


def test_all_chunks_skippable_all_current_and_matched():
    assert _skippable(
        [_fp("a", _CUR), _fp("b", _CUR), _fp("c", _CUR)], ["a", "b", "c"]
    ) is True


def test_all_chunks_skippable_absent_fingerprint():
    assert _skippable([_fp("a", _CUR), None, _fp("c", _CUR)], ["a", "b", "c"]) is False


def test_all_chunks_skippable_empty_computed_hash():
    assert _skippable(
        [_fp("a", _CUR), _fp("b", _CUR), _fp("c", _CUR)], ["a", "", "c"]
    ) is False


def test_all_chunks_skippable_length_mismatch():
    assert _skippable([_fp("a", _CUR)], ["a", "b", "c"]) is False
    assert _skippable([], []) is False


def test_all_chunks_skippable_stale_but_above_floor_is_stamp_not_skip():
    """The asymmetry that makes this function necessary: a STAMP candidate is
    NOT a SKIP. ``all_chunks_stampable`` accepts it and this one must not."""
    fps = [_fp("a", _CUR), _fp("b", _FLOOR), _fp("c", _CUR)]
    assert _skippable(fps, ["a", "b", "c"]) is False
    assert guards.all_chunks_stampable(
        [_fp("a", _FLOOR), _fp("b", _FLOOR), _fp("c", _FLOOR)], ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    ) is True


def test_all_chunks_skippable_chunk_count_drift_below_current_revision():
    """A count drift on a STAMP-eligible row is a genuine re-chunk → EMBED.

    NOTE the ordering in ``classify_row``: gate (5) (``rev >= current`` → SKIP)
    runs BEFORE gate (6)'s chunk-count check, so a count drift at the CURRENT
    revision would report skippable. That is unreachable in practice and not a
    hole: ``chunk_or_truncate_*`` prefixes every chunk with ``[chunk i/N]``
    when N >= 2, and that header is part of the stored body — so a change in N
    changes every chunk's ``function_body`` and therefore its content hash,
    which gate (1) catches first. Pinned below.
    """
    assert _skippable(
        [_fp("a", _FLOOR, total=4), _fp("b", _FLOOR, 4), _fp("c", _FLOOR, 4)],
        ["a", "b", "c"], total=3,
    ) is False


def test_total_chunks_reaches_the_hash_through_the_chunk_header():
    """Why the gate-ordering note above is safe: ``total_chunks`` is NOT a
    content-hash field, but the chunk HEADER carries N into the body text, so
    a re-chunk always moves the hash."""
    from weaviate_mcp.code_truncation import _chunk_header

    assert "2" in _chunk_header(1, 2)
    assert _chunk_header(1, 2) != _chunk_header(1, 3)


def test_all_chunks_skippable_hash_mismatch():
    assert _skippable(
        [_fp("a", _CUR), _fp("X", _CUR), _fp("c", _CUR)], ["a", "b", "c"]
    ) is False


def test_all_chunks_skippable_no_longer_lives_in_extractor_generation():
    """§4.1: ONE home. A re-export shim would be the 'shared home WITH the
    copies still beside it' anti-pattern."""
    from vco_lib import codegraph_extractor_generation as gen

    assert not hasattr(gen, "all_chunks_skippable")
    assert "all_chunks_skippable" not in getattr(gen, "__all__", [])


def test_all_chunks_skippable_defers_to_classify_row():
    """It aggregates; it does not re-decide. Every per-row verdict must be
    ``classify_row``'s."""
    src = inspect.getsource(guards.all_chunks_skippable)
    assert "classify_row(" in src
    assert "RowAction.SKIP" in src
    # And the deferred import that only existed to break the extractor_generation
    # -> guards cycle is gone with the move (check the BODY, not the docstring).
    body = src.split('"""')[-1]
    assert "import" not in body


def test_skip_or_stamp_ordering_and_visited_marking():
    """The extracted orchestration: SKIP is tried first, and the SKIP branch
    still owes the visited mark (a skip that forgets to mark is a delete, via
    ``--prune-stale`` / the unconditional per-file reconcile)."""
    visited, patched, reads = [], [], []

    def _read(cu):
        reads.append(cu)
        return _fp(cu, _CUR)

    assert guards.skip_or_stamp_all_chunks(
        ["a", "b", "c"], ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        read_fp=_read, patch_rev=lambda cu: patched.append(cu) or True,
        note_visited=visited.append,
    ) is True
    assert visited == ["a", "b", "c"], "all-SKIP must still mark every chunk"
    assert patched == [], "all-SKIP must not patch"
    assert reads == ["a", "b", "c"], "reads must be memoized (one per chunk)"


def test_skip_or_stamp_falls_through_to_stamp_and_memoizes_the_reads():
    visited, patched, reads = [], [], []

    def _read(cu):
        reads.append(cu)
        return _fp(cu, _FLOOR, total=2)  # STAMP candidate, not SKIP

    assert guards.skip_or_stamp_all_chunks(
        ["a", "b"], ["a", "b"], 2,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        read_fp=_read, patch_rev=lambda cu: patched.append(cu) or True,
        note_visited=visited.append,
    ) is True
    assert patched == ["a", "b"]
    assert visited == [], "the STAMP branch marks visited via patch_rev, not here"
    assert reads == ["a", "b"], (
        "the STAMP path must reuse the SKIP precheck's reads, not re-issue them"
    )


def test_skip_or_stamp_unreadable_row_falls_through_to_embed():
    """Fail-safe: an UNREADABLE row (``read_fp`` → ``None``, which is what
    ``read_object_fingerprint`` returns for an absent object, a read error or a
    mocked client) declines BOTH branches → False → the caller re-embeds.
    ``read_object_fingerprint`` swallows every exception itself, so the seam
    never hands a raising reader to this function."""
    assert guards.skip_or_stamp_all_chunks(
        ["a"], ["a"], 1,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        read_fp=lambda cu: None, patch_rev=lambda cu: True,
        note_visited=lambda cu: None,
    ) is False


def test_skip_or_stamp_precheck_exception_is_contained(monkeypatch):
    """An exception from the all-SKIP precheck falls through to the STAMP
    attempt rather than wedging the walk. Scope note: the try/except covers the
    PRECHECK only — exactly the shape the pre-move analyzer had, preserved
    verbatim. Nothing downstream of it raises in production because
    ``read_object_fingerprint`` swallows every read error itself and returns
    ``None``."""
    def _boom_skip(*a, **kw):
        raise RuntimeError("malformed fingerprint")

    monkeypatch.setattr(guards, "all_chunks_skippable", _boom_skip)
    patched = []
    assert guards.skip_or_stamp_all_chunks(
        ["a", "b"], ["a", "b"], 2,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        read_fp=lambda cu: _fp(cu, _FLOOR, total=2),
        patch_rev=lambda cu: patched.append(cu) or True,
        note_visited=lambda cu: None,
    ) is True
    assert patched == ["a", "b"], "must still reach the STAMP path"


def test_stamp_declines_when_any_patch_fails():
    """Never a half-stamped entity: a patch failure returns False so the caller
    re-embeds every chunk."""
    assert guards.skip_or_stamp_all_chunks(
        ["a", "b"], ["a", "b"], 2,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        read_fp=lambda cu: _fp(cu, _FLOOR, total=2),
        patch_rev=lambda cu: cu != "b",
        note_visited=lambda cu: None,
    ) is False


def test_stamp_single_chunk_props_moved_and_still_defensive():
    props = {}
    guards.stamp_single_chunk_props("P_CodeFunction", props)
    assert props == {"chunk_num": 0, "total_chunks": 1}
    preset = {"chunk_num": 2, "total_chunks": 7}
    guards.stamp_single_chunk_props("P_CodeClass", preset)
    assert preset == {"chunk_num": 2, "total_chunks": 7}, "must not clobber"
    other = {}
    guards.stamp_single_chunk_props("P_CodeModule", other)
    assert other == {}, "non-chunkable collections carry no chunk props"
    guards.stamp_single_chunk_props("P_CodeFunction", None)  # must not raise


def test_row_action_semantics_unchanged_by_the_move():
    """Guardrail: the move must not perturb the shared per-row rule."""
    assert guards.classify_row(
        "h", _CUR, "h", current_revision=_CUR, floor_revision=_FLOOR,
    ) is RowAction.SKIP
    assert guards.classify_row(
        "h", _FLOOR, "h", current_revision=_CUR, floor_revision=_FLOOR,
    ) is RowAction.STAMP
    assert guards.classify_row(
        "h", None, "h", current_revision=_CUR, floor_revision=_FLOOR,
    ) is RowAction.EMBED


# ═══════════════════════════════════════════════════════════════════════════
# The soft-probe DEGRADE WARNING (user, 2026-09-02): keep the fallback, add
# the signal. A swallowed exception with no trace is the failure class this
# release exists to remove.
# ═══════════════════════════════════════════════════════════════════════════


def _warn(n, exc=None, coll="P_CodeFunction", key="mod.f", lang="rust"):
    return guards.chunk_plan_degrade_warning(
        n, exc if exc is not None else RuntimeError("chunker exploded"),
        coll_name=coll, identity_key=key, language=lang,
    )


def test_degrade_warning_names_the_entity_and_the_consequence():
    """"chunk planning failed" is nearly useless. The reader needs WHICH entity
    is degraded and WHAT the consequence is."""
    msg = _warn(1)
    assert msg is not None
    assert "mod.f" in msg, "must name the entity"
    assert "rust" in msg, "must name the language"
    assert "CodeFunction" in msg, "must name the entity kind"
    assert "UN-CHUNKED" in msg and "DEGRADED" in msg, "must state the consequence"
    assert "RuntimeError" in msg and "chunker exploded" in msg, "must carry the cause"


def test_degrade_warning_survives_missing_context():
    """The warning must not become a failure point of its own — an unnamed
    entity, an empty collection name or a blank language still produce a line."""
    msg = _warn(1, coll="", key="", lang="")
    assert msg and "unknown-language" in msg and "?" in msg


def test_degrade_warning_rate_limit_boundaries():
    """First N named, then ONE suppression notice, then silence. Thousands of
    identical lines would be their own denial of signal."""
    n = guards.CHUNK_PLAN_WARN_LIMIT
    assert _warn(1) and "chunk planning FAILED" in _warn(1)
    assert _warn(n) and "chunk planning FAILED" in _warn(n)
    boundary = _warn(n + 1)
    assert boundary is not None
    assert "suppressed" in boundary and "chunk planning FAILED" not in boundary
    assert _warn(n + 2) is None
    assert _warn(10_000) is None


def test_degrade_warning_rejects_a_non_positive_ordinal():
    """Defensive: a caller that has not counted yet gets nothing, never a
    misleading 'occurrence 0' line."""
    assert _warn(0) is None
    assert _warn(-3) is None


def test_degrade_warning_is_pure():
    """Same ordinal -> same message; no hidden state between calls, so the
    caller owns the counter and two walks cannot interfere."""
    assert _warn(3) == _warn(3)
    assert _warn(1) == _warn(2), "the per-entity line does not encode the ordinal"


# -- chunk-SHRINK decisions, extracted from the analyzer ---------------------


def _tail(raw, props, *, full_name="m.f", fp="src/m.py", mn=2):
    return guards.is_stale_tail_row(
        raw, props, full_name=full_name, file_path_rel=fp, min_chunk_num=mn,
    )


def test_is_stale_tail_row_acts_and_leaves_alone():
    assert _tail("m.f", {"file_path": "src/m.py", "chunk_num": 2}) is True
    assert _tail("m.f", {"file_path": "src/m.py", "chunk_num": 5}) is True
    # survivor, not a tail
    assert _tail("m.f", {"file_path": "src/m.py", "chunk_num": 1}) is False
    # a DIFFERENT entity whose tokens would match a Weaviate `equal` filter
    assert _tail("m.f.other", {"file_path": "src/m.py", "chunk_num": 3}) is False
    # a same-named entity in a DIFFERENT file
    assert _tail("m.f", {"file_path": "src/other.py", "chunk_num": 3}) is False
    # unparseable chunk_num -> never delete (conservative)
    assert _tail("m.f", {"file_path": "src/m.py", "chunk_num": None}) is False
    assert _tail("m.f", {"file_path": "src/m.py", "chunk_num": "x"}) is False
    assert _tail("m.f", {"file_path": "src/m.py"}) is False
    assert _tail("m.f", "not a dict") is False
    # no file scoping known -> fall back to full_name + chunk_num only
    assert _tail("m.f", {"chunk_num": 3}, fp="") is True


def _surv(props, **over):
    kw = dict(full_name="m.f", project="P", project_source="/root",
              file_path_rel="src/m.py", new_total=2)
    kw.update(over)
    return guards.survivor_needs_total_patch(props, **kw)


def _row(**over):
    p = {"full_name": "m.f", "project": "P", "project_source": "/root",
         "file_path": "src/m.py", "chunk_num": 0, "total_chunks": 4}
    p.update(over)
    return p


def test_survivor_needs_total_patch_acts_and_leaves_alone():
    assert _surv(_row()) is True                         # stale total -> patch
    assert _surv(_row(total_chunks=2)) is False          # already correct
    assert _surv(_row(chunk_num=2)) is False             # a TAIL row
    assert _surv(_row(full_name="m.g")) is False         # another entity
    assert _surv(_row(project="Q")) is False             # another project
    assert _surv(_row(project_source="/else")) is False  # another source root
    assert _surv(_row(file_path="src/o.py")) is False    # another file
    assert _surv(_row(chunk_num=None)) is False          # unparseable -> leave
    assert _surv(_row(total_chunks=None)) is True        # NULL stored -> patch
    assert _surv(_row(total_chunks="x")) is True
    assert _surv("not a dict") is False


def test_survivor_patch_is_idempotent_by_construction():
    """A converged entity must do ZERO writes — that property lives here, in
    the predicate, not in the caller's loop."""
    converged = _row(total_chunks=2)
    assert _surv(converged) is False
    assert _surv(converged) is False


def test_survivor_scoping_ignores_unknown_dimensions():
    """Empty project / source / file_path mean "not known" — they must widen
    the match, never reject every row."""
    assert _surv(_row(project="", project_source="", file_path=""),
                 project="", project_source="", file_path_rel="") is True
