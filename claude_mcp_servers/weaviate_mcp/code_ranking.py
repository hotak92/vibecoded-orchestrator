# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Shared code-retrieval ranking pipeline (v0.2.72, T-FLOOR / P1 + P2).

THE HARD INVARIANT this module enforces
=======================================
The CLI code-retrieval path (``templates/scripts/query_code_graph.py::
CodeGraphQuery.search_by_concept``) and the MCP path
(``claude_mcp_servers/weaviate_mcp/server.py::search_code_graph``) MUST NOT
diverge. Before v0.2.72 they did: the CLI applied a per-slot semantic floor
while the MCP applied none, and neither reranked by code relationships.

This module is the SINGLE SHARED HOME for the code-retrieval ranking pipeline.
Both entry points gather their raw candidates (still Weaviate-specific — the
``near_vector`` fan-out stays in each caller), normalise each candidate to the
``{"_s": semantic_score, "_p": props, ...}`` shape, and then call
``run_code_retrieval_pipeline`` with one line. The floor / rerank / sort / trim
logic lives here and here only.

Design constraints
------------------
* PURE + Weaviate-agnostic + NO I/O. Every function is a deterministic
  transform over plain dicts/mappings. Env is read only through an injected
  ``Mapping`` (defaults to ``os.environ`` at the call boundary, never imported
  here for the resolver functions — the caller passes it) so tests pin it.
* The per-slot floor table is PRESERVED (C-H1): each active code-vector slot
  gets its OWN ``(retrieval_floor, post_rerank_floor)`` pair. Do NOT collapse
  to a single global floor — CodeSage / jina / qwen3 live in different distance
  bands and a single value re-creates the v0.2.70 Bug-C1b cross-scale-floor bug.
* The relationship boost is REORDER-ONLY and anchor-relative: it can rescue a
  near-margin LINKED result over the post_rerank_floor and reorder ties, but it
  never fabricates a match out of noise (a query whose whole pool is below the
  retrieval_floor stays empty even with maximum boost — the floor is not
  defeatable by boosting; validated in RESULTS-2026-07-01.md).

Measured floor values (RESULTS-2026-07-01.md, do NOT re-derive)
---------------------------------------------------------------
* CodeSage cosine: good matches score > 0.59, unrelated code ~0.14.
  retrieval_floor 0.16 (permissive, applied at fetch) / post_rerank_floor 0.22
  (the real gate, applied AFTER the boost). Both MEASURED — ship these.
* jina (768-dim code model): same compressed band as CodeSage → 0.16 / 0.22.
* qwen3 (code reuse of the text embedder): WIDER, more separable band →
  conservative UNMEASURED 0.20 / 0.30 + override hook.
* PENALTY_TEST_ENTITY 0.05 (ADDITIVE; E1-is-test-penalty-RESULTS.md,
  2026-07-03): subtracted from a test-entity's reranked score in step 2,
  BEFORE the post_rerank_floor gate. MEASURED — 0.05 is the no-collateral
  point (halves product-query contamination@5, zero test-intent regressions;
  additive dominates multiplicative because a multiplicative penalty is
  largest on exactly the high-scoring wanted tests of test-intent queries).

MUST MATCH (cross-surface contract): these floor VALUES + the boost weights +
the test penalty are the contract between the CLI, the MCP server, and any
hook that pre-filters code-graph results. Changing a value here without a
paired experiment re-run + doc update re-opens the cross-scale-floor bug.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Mapping, Optional

__all__ = [
    "CODE_FLOOR_BY_SLOT",
    "RELATIONSHIP_BOOST_CAP",
    "BOOST_CALL_LINKED",
    "BOOST_SAME_FILE",
    "BOOST_SHARED_TYPE",
    "PENALTY_TEST_ENTITY",
    "is_test_path",
    "resolve_test_penalty",
    "resolve_retrieval_floor",
    "resolve_post_rerank_floor",
    "rerank_score",
    "run_code_retrieval_pipeline",
]


# ─── Per-slot two-stage floor table (C-H1: PRESERVED, never one global) ──────
#
# slot -> (retrieval_floor, post_rerank_floor)
#   retrieval_floor  — permissive gate applied at fetch (before boost). Drops
#                      structurally-unrelated noise so the boost never operates
#                      on it. A candidate below this is discarded outright.
#   post_rerank_floor — the REAL gate, applied to the reranked (boosted) score.
#                      A near-margin LINKED result can be rescued over this by
#                      the relationship boost; an unlinked near-margin result is
#                      culled.
#
# CodeSage (0.16/0.22) + jina (0.16/0.22) are the compressed-band pair; qwen3
# (0.20/0.30) is the wider, more-separable band. See module docstring.
CODE_FLOOR_BY_SLOT: dict[str, tuple[float, float]] = {
    "codesage_embed": (0.16, 0.22),  # MEASURED (RESULTS-2026-07-01.md)
    "jina_embed": (0.16, 0.22),      # same compressed 768-dim code band
    "qwen3_embed": (0.20, 0.30),     # wider band; conservative UNMEASURED
}

# Fallback pair used when a slot is unknown. Mirrors the CodeSage measured pair
# (the shipped default backend) rather than a no-floor 0.0 — an unrecognised
# slot is more likely a codesage-family model than a reason to disable the gate.
_DEFAULT_FLOOR_PAIR: tuple[float, float] = (0.16, 0.22)

# Env override keys.
_ENV_RETRIEVAL_FLOOR = "VCO_CODE_GRAPH_RETRIEVAL_FLOOR"
_ENV_POST_RERANK_FLOOR = "VCO_CODE_GRAPH_POST_RERANK_FLOOR"
# Deprecated single-floor alias (pre-v0.2.72). It maps to the POST_RERANK floor
# — historically ``_resolve_code_score_floor`` applied its value to the final
# (only) score, which is semantically the post-rerank gate. Kept for ~3 releases
# so a user who pinned the old key still gets a real (post-rerank) floor.
_ENV_LEGACY_SCORE_FLOOR = "VCO_CODE_GRAPH_SCORE_FLOOR"

# ─── Relationship boost weights (P2; validated RESULTS-2026-07-01.md) ────────
BOOST_CALL_LINKED = 0.05    # candidate.leaf ∈ anchor.call_names OR vice-versa
BOOST_SAME_FILE = 0.03      # candidate.file_path == anchor.file_path
BOOST_SHARED_TYPE = 0.02    # anchor.type_uses ∩ candidate.type_uses non-empty
RELATIONSHIP_BOOST_CAP = 0.08  # sum of signals capped here

# ─── Test-entity penalty (M1; MEASURED, E1-is-test-penalty-RESULTS.md) ───────
#
# ADDITIVE penalty subtracted from a test-entity candidate's reranked score
# (rerank = semantic + boost − penalty), applied in pipeline step 2 BEFORE
# the post_rerank_floor gate — so a marginal test row (post_floor ≤ sem <
# post_floor + penalty) is culled outright, while a STRONG test hit merely
# ranks below equal product hits. Escape: when the ANCHOR itself is a test
# file (hook fired while editing a test), the penalty is zero → test-context
# retrieval is unaffected.
#
# E1 caveats (measured 2026-07-03, do NOT re-derive without a paired re-run):
#   * DOMINANT tests still win: a test whose semantic score exceeds the top
#     product hit by > 0.05 keeps rank 1 by design (the penalty is a nudge,
#     not a filter). Users hitting this dial the env override
#     VCO_CODE_GRAPH_TEST_PENALTY up to ~0.08-0.12 (aggressive demotion) or
#     down to 0 (off). Empty-string / unparseable env values fall through to
#     the default (v0.2.27 coercion discipline).
#   * Rust `#[cfg(test)]` in-file test modules (and in-file mocks/fixtures in
#     any language living beside product code) are INVISIBLE to this
#     path-only heuristic — they classify as product rows. Honestly out of
#     scope for a path heuristic.
PENALTY_TEST_ENTITY = 0.05
_ENV_TEST_PENALTY = "VCO_CODE_GRAPH_TEST_PENALTY"


def is_test_path(path: str) -> bool:
    """True when *path* names a test/spec/fixture source file.

    SINGLE HOME for the test-file heuristic (M1). The inline fallback in
    ``templates/scripts/analyze_code_graph.py`` MUST STAY BYTE-IDENTICAL to
    this body — the parity test
    ``tests/test_codegraph_metadata_producers_v0273.py::
    test_is_test_path_parity_with_code_ranking`` locks the two together.
    Pure function: path string in, bool out. Directory matching is per
    PATH PART (not substring) — ``tests/x.py`` is a test;
    ``my_tests_helper/x.py`` is not. Windows backslashes normalized.
    Empty/unknown → False (never flag on uncertainty).
    """
    if not path:
        return False
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if not parts:
        return False
    dirs_lower = [d.lower() for d in parts[:-1]]
    for d in dirs_lower:
        if d in ("tests", "test", "__tests__", "spec", "specs",
                 "testdata", "fixtures"):
            return True
        if d.endswith(".tests"):  # csharp `Foo.Tests` project dirs
            return True
    for i in range(len(dirs_lower) - 1):  # java `src/test` part-pair
        if dirs_lower[i] == "src" and dirs_lower[i + 1] == "test":
            return True
    name = parts[-1]
    lower = name.lower()
    if lower == "conftest.py" or lower.endswith("_test.py"):
        return True
    if lower.startswith("test_") and lower.endswith((".py", ".cpp")):
        return True
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs"):
        if lower.endswith(".spec" + ext) or lower.endswith(".test" + ext):
            return True
    if lower.endswith("_test.go") or lower.endswith("_test.rs"):
        return True
    # CamelCase suffixes stay case-sensitive (`contest.java` is NOT a test).
    if name.endswith(("Test.java", "Tests.java", "IT.java")):
        return True
    if name.endswith(("Tests.cs", "Test.cs")):
        return True
    if lower.endswith(("_spec.rb", "_test.rb")):
        return True
    if lower.endswith(("_test.cpp", "_test.cc", "_test.cxx",
                       "_tests.cpp", "_tests.cc")):
        return True
    if lower.endswith("_spec.lua"):
        return True
    if lower.endswith("_test.sh") or lower.endswith(".bats"):
        return True
    if lower.endswith(".tests.ps1"):  # Pester
        return True
    return False


def resolve_test_penalty(env: Optional[Mapping[str, str]] = None) -> float:
    """Resolve the test-entity penalty value.

    Precedence:
      1. ``VCO_CODE_GRAPH_TEST_PENALTY`` env override — ``0`` disables the
         penalty entirely; a large value (~0.5) acts as a soft filter.
         Empty-string / unparseable → fall through (v0.2.27 coercion).
      2. :data:`PENALTY_TEST_ENTITY` (0.05, MEASURED — see module docstring).

    ``env=None`` (the default) reads the PROCESS environment — same rationale
    as :func:`resolve_retrieval_floor`: the override must reach the search
    path on BOTH surfaces (MCP + CLI) without call sites passing
    ``os.environ`` by hand. Pass ``{}`` to isolate in tests.
    """
    if env is None:
        env = os.environ
    override = _coerce_env_float(env, _ENV_TEST_PENALTY)
    if override is not None:
        return override
    return PENALTY_TEST_ENTITY


def _coerce_is_test(props: Optional[Mapping[str, object]]) -> bool:
    """Is this entity a test row?

    Prefers the STORED ``is_test`` property (stamped at analyze time since
    v0.2.73 M1 — the producer half in analyze_code_graph.py). NULL / absent
    (pre-backfill rows, peer-project rows whose collections predate v6) →
    derive from the stored path via :func:`is_test_path` — ``file_path`` for
    Function/Class, falling back to ``path`` for Module (mirrors the collapse
    adapter's fallback in server.py). No props / no path → False (fail-safe:
    never penalize on uncertainty).
    """
    if props is None:
        return False
    stored = props.get("is_test")
    if isinstance(stored, bool):
        return stored
    if stored is not None:
        # Tolerate a non-bool truthy/falsy stored value (defensive: Weaviate
        # BOOL props arrive as bool, but a fixture may hold 0/1).
        return bool(stored)
    path = props.get("file_path") or props.get("path") or ""
    return is_test_path(str(path))


def _coerce_env_float(env: Optional[Mapping[str, str]], key: str) -> Optional[float]:
    """Read ``key`` from ``env`` as a float, honouring the empty-string-coercion
    discipline (v0.2.27): an ABSENT key OR an explicit empty-string value both
    yield ``None`` (meaning "fall through to the default") — an empty string is
    NEVER parsed as a literal ``0.0``. An unparseable non-empty value also
    yields ``None`` (fall through) rather than raising, so a typo'd override
    degrades to the per-slot default instead of crashing retrieval.
    """
    if env is None:
        return None
    raw = env.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        # Tolerate a Mapping that already holds a float (test fixtures).
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    stripped = raw.strip()
    if stripped == "":
        return None
    try:
        return float(stripped)
    except (TypeError, ValueError):
        return None


def resolve_retrieval_floor(
    slot: str,
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """Resolve the RETRIEVAL floor (applied at fetch, before boost).

    Precedence:
      1. ``VCO_CODE_GRAPH_RETRIEVAL_FLOOR`` env override (empty-string →
         default; unparseable → default).
      2. Per-slot default from :data:`CODE_FLOOR_BY_SLOT` (index 0).
      3. :data:`_DEFAULT_FLOOR_PAIR` when the slot is unknown.

    The deprecated ``VCO_CODE_GRAPH_SCORE_FLOOR`` alias does NOT affect the
    retrieval floor — it was historically a single post-boost gate, so it maps
    only to the post-rerank floor (see :func:`resolve_post_rerank_floor`).

    ``env=None`` (the default) reads the PROCESS environment — that is how the
    launcher-projected ``VCO_CODE_GRAPH_RETRIEVAL_FLOOR`` (GUI → app_state →
    config_projection → settings.json env → subprocess env) reaches the search
    path on BOTH surfaces (MCP + CLI) without every call site having to
    remember to pass ``os.environ``. Pass an explicit mapping (e.g. ``{}``)
    to isolate from the process env in tests.
    """
    if env is None:
        env = os.environ
    override = _coerce_env_float(env, _ENV_RETRIEVAL_FLOOR)
    if override is not None:
        return override
    return CODE_FLOOR_BY_SLOT.get(slot, _DEFAULT_FLOOR_PAIR)[0]


def resolve_post_rerank_floor(
    slot: str,
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """Resolve the POST-RERANK floor (the real gate, applied after the boost).

    Precedence:
      1. ``VCO_CODE_GRAPH_POST_RERANK_FLOOR`` env override.
      2. Deprecated ``VCO_CODE_GRAPH_SCORE_FLOOR`` alias (maps here — it was the
         single post-boost gate pre-v0.2.72). The canonical key wins if both
         are set.
      3. Per-slot default from :data:`CODE_FLOOR_BY_SLOT` (index 1).
      4. :data:`_DEFAULT_FLOOR_PAIR` when the slot is unknown.

    Empty-string / unparseable overrides fall through to the default (v0.2.27
    coercion discipline).

    ``env=None`` (the default) reads the PROCESS environment — same rationale
    as :func:`resolve_retrieval_floor`: the launcher-projected override must
    reach the search path on both surfaces without call sites passing
    ``os.environ`` by hand. Pass ``{}`` to isolate in tests.
    """
    if env is None:
        env = os.environ
    override = _coerce_env_float(env, _ENV_POST_RERANK_FLOOR)
    if override is not None:
        return override
    legacy = _coerce_env_float(env, _ENV_LEGACY_SCORE_FLOOR)
    if legacy is not None:
        return legacy
    return CODE_FLOOR_BY_SLOT.get(slot, _DEFAULT_FLOOR_PAIR)[1]


def _leaf(name: object) -> str:
    """Bare leaf of a possibly-qualified name.

    Mirrors ``server.py::_caller_match_terms`` — split on Rust ``::`` or Python
    ``.`` and take the last segment. ``"module.func"`` → ``"func"``;
    ``"mod::fn"`` → ``"fn"``; bare ``"fn"`` → ``"fn"``; falsy → ``""``.
    """
    if not name:
        return ""
    return re.split(r"::|\.", str(name))[-1]


def _as_str_set(value: object) -> set[str]:
    """Coerce a candidate property (TEXT_ARRAY → list, or None/str) to a set of
    non-empty strings. Weaviate returns TEXT_ARRAY props as ``list[str]`` and
    absent props as ``None``; a lone string is treated as a one-element set.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    # Weaviate TEXT_ARRAY props arrive as list[str]; tolerate any real
    # collection shape. A non-iterable (int, dict-key misuse, ...) → empty set
    # (same soft behaviour the previous try/except gave, but statically typed).
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(v) for v in value if v}
    return set()


def rerank_score(
    candidate_props: Mapping[str, object],
    anchor_props: Optional[Mapping[str, object]],
    *,
    test_penalty: Optional[float] = None,
) -> tuple[float, dict]:
    """Compute the NET anchor-relative rerank delta for one candidate.

    Returns ``(delta, signals)`` where ``delta`` is the summed, capped
    relationship boost MINUS the test-entity penalty (M1) — the value to ADD
    to the candidate's semantic score — and ``signals`` is a diagnostic dict
    of which signals fired (``call_linked`` / ``same_file`` / ``shared_type``)
    plus the ``capped`` boolean and ``is_test_penalty`` (whether the M1
    penalty fired for this candidate).

    Test-entity penalty (M1; anchor-INDEPENDENT, computed before the
    no-anchor early return): fires when the candidate is a test row
    (:func:`_coerce_is_test` — stored ``is_test`` prop, NULL → path-derive)
    AND the anchor is NOT a test row (the anchor only provides the ESCAPE:
    editing a test file keeps test-context retrieval unpenalized). Value from
    ``test_penalty`` when given (the pipeline resolves it once per call), else
    :func:`resolve_test_penalty` against the process env.

    Signals (all anchor-relative; absent signals contribute +0):
      * call-linked (+0.05): candidate.leaf ∈ anchor.call_names OR
        anchor.leaf ∈ candidate.call_names. ``call_names`` holds BARE leaf
        names (analyzer contract), so we compare against the candidate/anchor
        LEAF of ``full_name`` (or ``name``).
      * same-file (+0.03): candidate.file_path == anchor.file_path (both
        non-empty).
      * shared-type (+0.02): anchor.type_uses ∩ candidate.type_uses non-empty.

    The boost sum is capped at :data:`RELATIONSHIP_BOOST_CAP` (0.08) BEFORE
    the penalty is subtracted (``capped`` describes the boost only). With no
    anchor (a direct MCP call with no seed to reorder around) the boost is 0
    but the penalty still applies → ``(-penalty_or_0, {"is_test_penalty":
    bool})``.
    """
    if test_penalty is None:
        test_penalty = resolve_test_penalty(None)
    candidate_is_test = _coerce_is_test(candidate_props)
    anchor_is_test = _coerce_is_test(anchor_props) if anchor_props else False
    penalty = test_penalty if (candidate_is_test and not anchor_is_test) else 0.0

    if anchor_props is None:
        return 0.0 - penalty, {"is_test_penalty": penalty != 0.0}

    signals: dict[str, bool] = {}
    delta = 0.0

    # --- call-linked (+0.05) --------------------------------------------
    cand_leaf = _leaf(candidate_props.get("full_name") or candidate_props.get("name"))
    anchor_leaf = _leaf(anchor_props.get("full_name") or anchor_props.get("name"))
    anchor_calls = _as_str_set(anchor_props.get("call_names"))
    cand_calls = _as_str_set(candidate_props.get("call_names"))
    call_linked = bool(
        (cand_leaf and cand_leaf in anchor_calls)
        or (anchor_leaf and anchor_leaf in cand_calls)
    )
    if call_linked:
        delta += BOOST_CALL_LINKED
    signals["call_linked"] = call_linked

    # --- same-file (+0.03) ----------------------------------------------
    cand_file = candidate_props.get("file_path") or ""
    anchor_file = anchor_props.get("file_path") or ""
    same_file = bool(cand_file and anchor_file and cand_file == anchor_file)
    if same_file:
        delta += BOOST_SAME_FILE
    signals["same_file"] = same_file

    # --- shared-type (+0.02) --------------------------------------------
    shared_type = bool(
        _as_str_set(anchor_props.get("type_uses"))
        & _as_str_set(candidate_props.get("type_uses"))
    )
    if shared_type:
        delta += BOOST_SHARED_TYPE
    signals["shared_type"] = shared_type

    capped = delta > RELATIONSHIP_BOOST_CAP
    if capped:
        delta = RELATIONSHIP_BOOST_CAP
    signals["capped"] = capped

    # M1: subtract the test penalty AFTER the boost cap (net delta = capped
    # boost − penalty). E1 §5 measured the boost-cancels-penalty interplay at
    # 1.7% of test candidates — no need to size the penalty above the cap.
    signals["is_test_penalty"] = penalty != 0.0
    return delta - penalty, signals


def _candidate_semantic(candidate: Mapping[str, object]) -> float:
    """Extract the semantic score from a normalised candidate.

    Contract: every candidate is a dict carrying at least ``"_s"`` (the semantic
    score, ``1.0 - distance``). Absent / non-numeric → treated as 0.0 (culled by
    any positive floor).
    """
    raw = candidate.get("_s")
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _candidate_props(candidate: Mapping[str, object]) -> Mapping[str, object]:
    """Extract the Weaviate property mapping from a normalised candidate.

    Contract: ``"_p"`` holds the props dict. Absent → empty mapping (boost
    signals simply won't fire).
    """
    props = candidate.get("_p")
    if isinstance(props, Mapping):
        return props
    return {}


def run_code_retrieval_pipeline(
    candidates: list[dict],
    *,
    retrieval_floor: float,
    post_rerank_floor: float,
    anchor_props: Optional[Mapping[str, object]] = None,
    limit: int,
    collapse_fn: Optional[Callable[[list[dict]], list[dict]]] = None,
    tier_fn: Optional[Callable[[list[dict]], list[dict]]] = None,
    key_fields: tuple[str, ...] = ("file_path", "full_name"),
    env: Optional[Mapping[str, str]] = None,
) -> list[dict]:
    """THE shared two-stage-floor + relationship-rerank pipeline.

    Both the CLI (``search_by_concept``) and the MCP (``search_code_graph``)
    call THIS after gathering + normalising their raw candidates, so the two
    paths cannot diverge on floor / rerank / sort / trim behaviour.

    Candidate shape (normalised by the caller BEFORE calling)
    ---------------------------------------------------------
    Each element of ``candidates`` is a mutable dict carrying at least:
      * ``"_s"`` — semantic score (``1.0 - distance``), float.
      * ``"_p"`` — the Weaviate property mapping (dict). Used for the boost
        signals and for de-dup keying via ``key_fields``.
    Callers may carry extra keys (``"_c"`` base collection, ``"_d"`` distance,
    ``"_src"`` peer label, etc.) — the pipeline preserves them untouched.
    The pipeline ADDS two keys to each surviving candidate:
      * ``"_rerank"`` — the reranked score (semantic + boost delta).
      * ``"_boost"`` — ``{"delta": float, "signals": {...}}`` diagnostics.

    Steps (in order)
    ----------------
    1. Drop candidates whose SEMANTIC score < ``retrieval_floor`` (stage-1
       gate, before boost — noise never reaches the boost math).
    2. Compute ``rerank = semantic + rerank_score(props, anchor_props).delta``
       for each survivor and stash ``_rerank`` / ``_boost``. The delta is the
       NET of the capped relationship boost MINUS the M1 test-entity penalty
       (a test row is nudged down before the stage-2 gate).
    3. Drop candidates whose RERANKED score < ``post_rerank_floor`` (stage-2
       gate — a near-margin LINKED result can be rescued here by its boost; an
       unlinked near-margin result is culled).
    4. Sort by ``_rerank`` descending (stable — ties keep input order).
    5. De-dup by ``key_fields`` (first-seen wins, i.e. highest reranked score
       after the sort). Keeps the pipeline robust to the multi-collection
       fan-out returning the same entity twice.
    6. OPTIONALLY apply ``collapse_fn`` then ``tier_fn`` (both default ``None``
       = no-op). See the injection contract below.
    7. Trim to ``limit`` and return.

    OVER-FETCH is the CALLER's responsibility
    -----------------------------------------
    The caller fetches ~``2 * limit`` candidates (per collection, before merge)
    so that stage-1/stage-2 culling + de-dup still leaves enough to fill
    ``limit``. This pipeline TRIMS to ``limit`` only at the very end (step 7);
    it never over-fetches on its own.

    Injected-callable contracts (dependency injection — v0.2.72 integrator wires
    T-CHUNK's helpers here; this module must NOT import T-CHUNK directly)
    --------------------------------------------------------------------------
    ``collapse_fn(results: list[dict]) -> list[dict]`` (P3, optional)
        Receives the de-duplicated, rerank-sorted survivor list (each dict in
        the normalised shape above, now also carrying ``_rerank`` / ``_boost``)
        and returns a possibly-shorter list in the SAME shape and the SAME sort
        order. Intended to collapse multiple chunk-rows of one logical entity
        into a single representative row (T-CHUNK / model-aware chunking).
        Called BEFORE ``tier_fn`` and BEFORE the final ``limit`` trim, so
        collapse sees more than ``limit`` rows and the trim counts collapsed
        entities, not raw chunks. Must be order-preserving and must not
        re-introduce dropped candidates. ``None`` (default) = identity.

    ``tier_fn(results: list[dict]) -> list[dict]`` (P4, optional)
        Receives the (optionally collapsed) list and returns rows in the SAME
        relative order with per-result verbosity/tier annotations added (e.g.
        a ``"_tier"`` key) so the downstream formatter can vary detail by
        rank/score. It MAY DROP discard-tier rows (score below the tier
        ``min`` gate — the code tier_fn from ``make_code_tier_fn`` does
        exactly that), but it must NOT reorder or add rows. Called AFTER
        ``collapse_fn``, BEFORE the ``limit`` trim. ``None`` (default) =
        identity.

    ``anchor_props`` ``None`` → boost is 0 for every candidate → pure semantic
    ordering (the direct-MCP-call path, no seed to reorder around) MINUS the
    M1 test penalty for test rows (the penalty is anchor-independent; only
    the ESCAPE — anchor-is-test — needs an anchor).

    ``env`` ``None`` (the default at BOTH call sites) → the M1 test penalty
    resolves against the PROCESS environment (``VCO_CODE_GRAPH_TEST_PENALTY``
    override → measured default 0.05), so the override reaches both surfaces
    without call-site changes. Pass ``{}`` to isolate in tests.
    """
    # M1: resolve the test penalty ONCE per pipeline call (not per candidate).
    test_penalty = resolve_test_penalty(env)

    # Step 1: stage-1 retrieval-floor gate (semantic, pre-boost).
    survivors: list[dict] = [
        c for c in candidates if _candidate_semantic(c) >= retrieval_floor
    ]

    # Step 2: rerank (semantic + boost delta − test penalty), stash
    # diagnostics. The penalty lands HERE — before the step-3 gate — so
    # marginal test rows are culled outright by the post_rerank_floor (E1 §6).
    for c in survivors:
        semantic = _candidate_semantic(c)
        delta, signals = rerank_score(
            _candidate_props(c), anchor_props, test_penalty=test_penalty
        )
        c["_rerank"] = semantic + delta
        c["_boost"] = {"delta": delta, "signals": signals}

    # Step 3: stage-2 post-rerank-floor gate (on the boosted score).
    survivors = [c for c in survivors if c["_rerank"] >= post_rerank_floor]

    # Step 4: sort by reranked score desc (stable → ties keep input order).
    survivors.sort(key=lambda c: c["_rerank"], reverse=True)

    # Step 5: de-dup by key_fields (first-seen after sort = highest reranked).
    def _dedup_key(c: dict) -> tuple:
        props = _candidate_props(c)
        parts = tuple((props.get(f) or "") for f in key_fields)
        # A candidate with an all-empty key is un-dedupable — key it by identity
        # so distinct empty-keyed rows are not collapsed into one.
        if not any(parts):
            return ("__id__", id(c))
        return parts

    seen: set[tuple] = set()
    deduped: list[dict] = []
    for c in survivors:
        k = _dedup_key(c)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)

    # Step 6: optional injected collapse (P3) then tier (P4).
    result = deduped
    if collapse_fn is not None:
        result = collapse_fn(result)
    if tier_fn is not None:
        result = tier_fn(result)

    # Step 7: trim to limit.
    return result[:limit]
