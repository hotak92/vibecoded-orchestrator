# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``codegraph_guards`` — the ONE pure decision module for the code-graph
re-embed / patch / skip guard (v0.2.82 G1).

WHY THIS EXISTS
---------------
Before v0.2.82 the "should this row re-embed, get a cheap metadata patch, or
be skipped entirely?" decision lived in THREE scattered places inside the
analyzer monolith (``templates/scripts/analyze_code_graph.py``):

  * ``_fingerprint_matches`` — the SKIP gate (hash + exact-revision);
  * ``_stale_row_needs_only_revision_stamp`` — the D1 STAMP classifier
    (hash + vector-present + floor ≤ rev < current + single-chunk);
  * the per-file gate ``_get_existing_module`` — its own inline
    ``int(stored_rev) == CURRENT`` equality check.

Three copies of a correctness-critical rule that MUST agree (a divergence is a
silent mass re-embed or, worse, a wrong SKIP that freezes a stale vector). This
module is that rule, extracted ONCE. The analyzer's method NAMES survive as
thin delegators (they are test/monkeypatch seams the golden + unit suites bind),
but the BODIES defer here. ``codegraph_resync`` reuses the same helpers so the
resync driver's "owed work" counting can never drift from what the analyzer
actually does on a re-walk.

PURITY
------
Zero I/O, zero Weaviate imports, zero analyzer state. Parameters in, an action
out. The analyzer owns the constants (``CODEGRAPH_EMBED_REVISION``,
``_EMBED_SPACE_COMPATIBLE_FROM_REVISION``, ``_EMBED_REVISION_VECTORLESS``) and
passes them as arguments — moving them here would break
``codegraph_resync._resolve_embed_revision`` (which parses the analyzer file for
the literal) and perturb the golden module-load. This module never imports the
analyzer.

THE FLOOR / NULL SEMANTICS (the single documented decision, per plan C3)
-----------------------------------------------------------------------
A NULL / non-int ``embed_revision`` means the row was written BEFORE revision
tracking existed (pre-v0.2.72), i.e. BEFORE the last vectors-invalid break (the
P3 chunking change shipped in the SAME release that introduced revision
tracking, so no NULL row can post-date the break). The user's binding directive
#1 classifies such a row's vector as legitimately re-embeddable (below the
compatibility floor). So: NULL / non-int → **below-floor → EMBED**, defined
HERE, once. ``embed_revision == 0`` (``_EMBED_REVISION_VECTORLESS``) or negative
means "no valid vector was ever written for this row" → EMBED. ``0 < rev <
floor`` means the vector lives in a stale embedding space (a model/chunking
bump raised the floor) → EMBED. Only ``floor ≤ rev < current`` with a
byte-identical content hash and a matching chunk shape is a cheap metadata
STAMP.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from vco_lib import git_meta


def provenance_line(
    model: Any, dim: Any, embed_revision: int, repo_path: Any,
) -> str:
    """v0.2.82 (G6): build the ONE machine-readable provenance line WP-3 parses.

    Format (NORMATIVE — the launcher parser keys on it verbatim):
    ``CODEGRAPH_PROVENANCE model=<model> dim=<dim> embed_revision=<int>
    analyzed_commit=<sha|none>``. ``model``/``dim`` come from the caller's
    EmbeddingService config; the commit via :func:`vco_lib.git_meta.git_head_sha`
    on ``repo_path`` (v0.2.92 WP-G — was an ad hoc ``subprocess.run(["git",
    "rev-parse", "HEAD"], ...)`` here; migrated onto the shared runner so this
    module has one fewer private git spawn to keep in sync), soft-failing to
    ``none`` (non-git tree / git absent / any error, including a ``repo_path``
    that is not a valid path at all — callers pass plain strings, see
    ``test_provenance_line_soft_fails_on_bad_inputs``). Never raises — a
    provenance failure must not fail a build. (This is the single I/O
    exception to the module's zero-I/O rule: a read-only git probe, isolated
    here so the analyzer keeps only a one-line print.)
    """
    try:
        model_s = str(model) if model else "unknown"
    except Exception:  # noqa: BLE001
        model_s = "unknown"
    try:
        dim_i = int(dim or 0)
    except Exception:  # noqa: BLE001
        dim_i = 0
    commit = "none"
    try:
        sha = git_meta.git_head_sha(Path(repo_path))
        if sha:
            commit = sha
    except Exception:  # noqa: BLE001 — non-git tree / git absent → none
        pass
    return (
        f"CODEGRAPH_PROVENANCE model={model_s} dim={dim_i} "
        f"embed_revision={embed_revision} analyzed_commit={commit}"
    )


class RowAction(Enum):
    """The three mutually-exclusive outcomes for one code-graph row.

    * ``SKIP``  — content identical AND already at the current revision:
      nothing to do (today's ``_fingerprint_matches``).
    * ``STAMP`` — content identical, stored vector still valid, revision merely
      stale (``floor ≤ rev < current``): patch ``embed_revision`` to current
      via ``data.update`` ONLY — no re-embed, no tombstone (today's D1).
    * ``EMBED`` — content changed, vector missing/invalid, below the
      compatibility floor, NULL/vectorless, or any uncertainty: re-embed
      (fail-safe default).
    """

    SKIP = "skip"
    STAMP = "stamp"
    EMBED = "embed"


def _coerce_rev(stored_rev: Any) -> Optional[int]:
    """Parse a raw stored ``embed_revision`` property into an int, or ``None``.

    Returns ``None`` for NULL, non-numeric junk, or anything that does not
    round-trip to an int — every such value is treated as "revision unknown"
    (below-floor) by the callers below. A ``bool`` is deliberately rejected
    (``isinstance(True, int)`` is True in Python, but a bool revision is junk).
    """
    if stored_rev is None or isinstance(stored_rev, bool):
        return None
    try:
        return int(stored_rev)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_object_fingerprint(
    collection: Any,
    det_uuid: str,
    embed_revision_prop: str,
    want_total_chunks: bool = False,
) -> Optional[dict]:
    """Point-read an object's tombstone-skip fingerprint by UUID (I/O helper).

    v0.2.73 (FIX-B2), extracted to vco_lib in v0.2.82: the single home for the
    ``content_hash`` + ``embed_revision`` point-read shared by the EMBED-skip
    precheck and the WRITE-skip decision. Returns
    ``{"content_hash": str, "embed_revision": Any, "total_chunks": int|None}``
    on success, or ``None`` when the object is absent OR the read cannot be
    performed (no ``.query`` on a mocked/older client, fetch raises, …). A
    ``None`` means "unknown" → callers MUST fall through to embed+write
    (fail-safe; never skip on uncertainty). ``want_total_chunks`` also requests
    ``total_chunks`` (valid only for chunkable Function/Class collections;
    Module/API/Interaction schemas lack it and an unknown return_property errors
    the read).
    """
    try:
        query = getattr(collection, "query", None)
        fetch_by_id = getattr(query, "fetch_object_by_id", None) if query else None
        if not callable(fetch_by_id):
            return None
        read_props = ["content_hash", embed_revision_prop]
        if want_total_chunks:
            read_props.append("total_chunks")
        existing = fetch_by_id(det_uuid, return_properties=read_props)
        if existing is None:
            return None
        existing_props = getattr(existing, "properties", None) or {}
        total_chunks: Optional[int] = None
        if want_total_chunks:
            try:
                total_chunks = int(existing_props.get("total_chunks") or 1)
            except (TypeError, ValueError):
                total_chunks = 1
        return {
            "content_hash": existing_props.get("content_hash") or "",
            "embed_revision": existing_props.get(embed_revision_prop),
            "total_chunks": total_chunks,
        }
    except Exception:  # noqa: BLE001 — any read failure → unknown → write
        return None


def classify_row(
    stored_hash: Optional[str],
    stored_rev: Any,
    computed_hash: str,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
    is_chunkable: bool = False,
    stored_total_chunks: Any = None,
    computed_total_chunks: int = 1,
    embedding_space_matches: bool = True,
) -> RowAction:
    """Decide SKIP / STAMP / EMBED for one row. PURE — parameters in, action out.

    Args:
        stored_hash: the row's stored ``content_hash`` (``None`` / empty when
            absent / pre-migration — treated as unknown → EMBED).
        stored_rev: the RAW stored ``embed_revision`` property (int | None |
            junk); coerced via :func:`_coerce_rev`.
        computed_hash: the content hash the analyzer computed for the row this
            run (empty when hashing failed → EMBED, fail-safe).
        current_revision: ``CODEGRAPH_EMBED_REVISION`` (the analyzer constant).
        floor_revision: ``_EMBED_SPACE_COMPATIBLE_FROM_REVISION`` — the lowest
            positive revision whose stored vector is still valid.
        vectorless_sentinel: ``_EMBED_REVISION_VECTORLESS`` (0) — a row stamped
            this value has NO valid vector.
        is_chunkable: True for CodeFunction / CodeClass (which can split into
            N chunks); False for Module / API / Interaction.
        stored_total_chunks: the row's stored ``total_chunks`` (``None`` when
            not read / not chunkable → treated as 1).
        computed_total_chunks: the chunk count this run intends to write
            (1 for the single-object path; N for the fan-out).
        embedding_space_matches: run-level provenance probe result. This
            release: a ``False`` value does NOT change the returned action (the
            caller emits the loud provenance warning; enforcement is staged to
            its own release — see plan DEFERRALS D1). The parameter exists now
            so the enforcement release does not change this signature.

    Returns:
        A :class:`RowAction`.

    Fail-safe: EVERY uncertainty (empty/absent hash, NULL/junk revision, a
    revision below the floor, a chunk-count mismatch) resolves to EMBED. A wrong
    STAMP would freeze a stale vector; a wrong SKIP would hide a genuine change.
    Only a positively-confirmed match ever avoids the embed.
    """
    # (1) Content changed or unknown → EMBED (fail-safe). Covers both empties.
    if not computed_hash or not stored_hash or stored_hash != computed_hash:
        return RowAction.EMBED

    rev = _coerce_rev(stored_rev)

    # (2) NULL / non-int → pre-revision-tracking ⇒ pre-floor break → EMBED (C3).
    if rev is None:
        return RowAction.EMBED

    # (3) Vectorless sentinel (0) or negative → no valid vector exists → EMBED.
    if rev <= vectorless_sentinel:
        return RowAction.EMBED

    # (4) Below the compatibility floor → vector in a stale space → EMBED.
    if rev < floor_revision:
        return RowAction.EMBED

    # (5) Already at (or ahead of) the current revision → SKIP. ``>=`` mirrors
    # the analyzer's ``stored_rev == CODEGRAPH_EMBED_REVISION`` skip gate; a
    # forward-dated row (rev > current, e.g. a downgrade) is content-identical
    # with a valid vector, so re-embedding it would be wasted work — SKIP.
    if rev >= current_revision:
        return RowAction.SKIP

    # (6) floor ≤ rev < current AND content-identical → the STAMP candidate.
    # A chunk-count change is a genuine re-chunk (the bodies differ per chunk),
    # so it can never be a metadata-only patch → EMBED.
    if is_chunkable:
        stored_total = _coerce_rev(stored_total_chunks)
        if stored_total is None:
            stored_total = 1
        if stored_total != computed_total_chunks:
            return RowAction.EMBED
        # A multi-chunk entity whose count is unchanged AND every chunk row
        # hash-matches is stampable PER CHUNK (the caller drives the per-chunk
        # read); a single-chunk chunkable entity is the ordinary D1 case.
    return RowAction.STAMP


def is_row_revision_stale(stored_rev: Any, current_revision: int) -> bool:
    """Single home for the resync's "owed work" staleness test.

    A row is stale (owes a re-walk) when its stored revision is NOT the current
    one: ``None`` / non-int junk (pre-migration) counts as stale, and any int
    ``!= current_revision`` counts as stale. Mirrors the analyzer's per-file
    gate ``_get_existing_module`` (which skips a file only when its module row
    is at the current revision) and ``_fingerprint_matches`` (skip only at exact
    equality) — so the resync counts exactly the rows a re-walk will touch.

    NOTE the deliberate asymmetry with :func:`classify_row`: a forward-dated
    ``rev > current`` counts as NOT stale here (a re-walk would SKIP it — there
    is no owed work), whereas ``classify_row`` also returns SKIP for it. Both
    agree "no work owed"; the difference is only in how a (never-occurring in
    practice) downgrade is described.
    """
    rev = _coerce_rev(stored_rev)
    if rev is None:
        return True
    return rev != current_revision


# ═══════════════════════════════════════════════════════════════════════════
# ENTITY IDENTITY — the two key spaces, side by side so they cannot collide
# ═══════════════════════════════════════════════════════════════════════════
#
# A row's deterministic UUID is minted from
# ``(project, file_path_rel, identity_key, project_source)``. TWO independent
# disambiguators extend ``identity_key``, and they MUST use disjoint suffix
# alphabets:
#
#   * CHUNK ordinal      ``<key>::<i>``  (i >= 1) — :func:`chunk_identities`.
#     Chunk 0 keeps the BARE key, which is what makes a newly-chunked entity's
#     canonical row byte-identical to its pre-chunking single-object row.
#   * OCCURRENCE ordinal ``<key>#<n>``   (n >= 2) —
#     :func:`assign_duplicate_identity_suffixes`. Occurrence 1 keeps the BARE
#     key, which is what makes the v0.2.92 identity fix a PURE ADD: every UUID
#     stored today is still written by the next walk, so nothing is orphaned
#     and no delete path is engaged.
#
# Using ``::<n>`` for the occurrence ordinal would give occurrence 2 of
# ``mod.foo`` the SAME uuid as CHUNK 1 of occurrence 1 of ``mod.foo`` — a
# wrong body on a canonical-looking row, silently. That is why the alphabets
# differ, and why both derivations live in this one module where the next
# editor sees them together.

_DUP_IDENTITY_SUFFIX = "#"


def assign_duplicate_identity_suffixes(
    keyed: Sequence[Tuple[Any, str]],
) -> List[Optional[str]]:
    """v0.2.92 (Defect B): per-entity ``_identity_key`` OVERRIDE, or ``None``.

    Input is ``(kind, identity_key)`` in the file's EMISSION order. Output is
    positionally aligned: ``None`` means "keep the bare key" and a string is
    the override the writer stamps into ``extras['_identity_key']``.

    THE DEFECT. Every producer builds ``full_name`` as ``{file_stem}.{symbol}``
    with no enclosing scope, so two same-named symbols in ONE file share a
    UUID and the later ``replace()`` silently overwrites the earlier: the graph
    stores one arbitrary occurrence and re-embeds it on EVERY walk. Real cases:
    ``secrets.rs`` has 11 ``drop`` and 9 ``new``; ``vct-hub/src/boot.rs`` has
    the systemd-user / launchd / Windows-Scheduled-Task implementations of
    ``register`` / ``unregister`` / ``status`` collapsed onto one row each, so
    ``search_code_graph("register boot auto-start")`` can only ever find one
    OS's body. Measured 4,561 lost rows over 146,326 entities.

    THE RULE. The FIRST occurrence of a ``(kind, key)`` pair keeps the bare key
    (``None``); the n-th (n >= 2) gets ``f"{key}#{n}"``. First-wins rather than
    last-wins because it is stable under APPEND: adding a new duplicate at the
    end of a file never re-keys the occurrences before it.

    Grouping is ``(kind, key)``, not ``key`` alone, because UUIDs are
    per-Weaviate-class: a CodeClass and a CodeFunction with the same key land
    in different collections and never collide, so disambiguating them would
    needlessly change a UUID.

    INJECTIVITY is by construction, not by argument: the ordinal escalates
    until the candidate collides with neither a bare key already claimed nor a
    previously-assigned override. An input that already CONTAINS ``"a#2"`` as a
    real key therefore still yields a duplicate-free set of final keys.

    PURE: a deterministic function of the input sequence. No state, no I/O.
    """
    taken: set = set()
    seen_count: Dict[Tuple[Any, str], int] = {}
    out: List[Optional[str]] = []
    for kind, key in keyed:
        pair = (kind, key)
        n_seen = seen_count.get(pair, 0) + 1
        seen_count[pair] = n_seen
        if n_seen == 1 and pair not in taken:
            taken.add(pair)
            out.append(None)
            continue
        # 2nd..Nth occurrence (or a bare key some earlier entity already
        # claimed): escalate until free. Starts at 2 — never ``#1``.
        n = n_seen if n_seen >= 2 else 2
        candidate = f"{key}{_DUP_IDENTITY_SUFFIX}{n}"
        while (kind, candidate) in taken:
            n += 1
            candidate = f"{key}{_DUP_IDENTITY_SUFFIX}{n}"
        taken.add((kind, candidate))
        out.append(candidate)
    return out


def plan_chunk_texts(
    coll_name: str,
    props: Any,
    identity_key: str,
    *,
    language_fallback: str,
    model_fn: Callable[[], Any],
    chunk_fn: Callable[..., Any],
    chunk_class_fn: Callable[..., Any],
) -> Optional[List[str]]:
    """v0.2.92 (Defect A): the chunk DECISION, split from the chunk WRITE.

    Returns the chunk texts for a chunkable entity, or ``None`` when chunking
    does not apply (collection is not CodeFunction/CodeClass, ``props`` is not
    a dict, the body property is empty, or the chunker yielded nothing).

    WHY THIS IS A SEPARATE FUNCTION. ``_dedup_insert`` resolves the deferred
    embed BEFORE it fans out chunks, and that resolver hashes the FULL body as
    a single chunk (``chunk_num=0, total_chunks=1``). A multi-chunk entity's
    stored canonical row holds the CHUNK-0 text, which
    ``code_truncation._chunk_header`` prefixes with ``[chunk 1/N]`` whenever
    ``N >= 2`` — so the two hashes can never match, the resolver ALWAYS
    embedded, and the fan-out ALWAYS discarded that vector
    (``build_chunk_write_params`` overwrites ``vector`` with the chunk's own or
    deletes it). One wasted MAX-SIZE embed per multi-chunk entity per walk,
    converged or not: 3,147 such entities on this machine, ~53% of the residual
    cost of a converged ``--force-rewalk``. Planning the chunks FIRST lets the
    caller skip that embed entirely.

    ONLY THE DECISION MOVES. The fan-out must keep running AFTER the analyzer's
    property-stamping block, because each chunk row copies ``language`` /
    ``project_source`` / ``file_path`` / ``is_test`` / ``doc`` out of ``props``
    and those are exactly the properties the language-scoped ``--prune-stale``
    filter and the prune anchor resolution key on. Hoisting the WRITE would
    write rows a later prune cannot see — silent corruption surfacing far from
    its cause. So the caller computes the texts ONCE here and PASSES THEM DOWN;
    the decision and the write can never disagree.

    ``language_fallback`` is the analyzer's ``_current_language`` — the same
    value its stamping block writes into an EMPTY ``props['language']``, so the
    language resolved here is provably the one the fan-out would have resolved
    after the stamp.

    PURE: chunkers + model id are injected. Exceptions are NOT swallowed here —
    the caller decides whether a failure is soft (the hoist: fall back to
    today's order) or hard (the fan-out: raise, exactly as it always has).
    """
    is_function = coll_name.endswith("CodeFunction")
    if not (is_function or coll_name.endswith("CodeClass")):
        return None
    if not isinstance(props, dict):
        return None
    body = props.get("function_body" if is_function else "class_body") or ""
    if not body:
        return None
    signature = props.get("signature") or ""
    language = props.get("language") or language_fallback or "python"
    model = model_fn()
    if is_function:
        texts = chunk_fn(
            signature, body, language=language, model=model,
            full_name=identity_key,
        )
    else:
        texts = chunk_class_fn(
            signature, body, methods=props.get("methods") or None,
            language=language, model=model, full_name=identity_key,
        )
    return list(texts) if texts else None


# v0.2.92: how many per-entity chunk-planning degrades to name individually
# before falling back to the aggregate. A chunker broken for a whole LANGUAGE
# would otherwise emit thousands of identical lines — which is its own denial of
# signal (the reader stops reading and the real per-entity names are buried).
# 20 names the sample; the walk's end-of-run total names the scale.
CHUNK_PLAN_WARN_LIMIT = 20


def chunk_plan_degrade_warning(
    occurrence: int,
    exc: BaseException,
    *,
    coll_name: str,
    identity_key: str,
    language: str,
) -> Optional[str]:
    """v0.2.92: the WARNING for one soft chunk-planning failure — or ``None``.

    PURE: occurrence ordinal in, message out. The caller counts and logs; this
    owns WHAT to say and WHETHER to say it, beside the chunk lifecycle it
    describes.

    WHY THIS EXISTS. :func:`plan_chunk_texts` is reached twice for an
    over-budget entity: once as a HOIST probe (soft — a probe must never become
    a new failure point) and once from the fan-out (hard — a raise propagates
    exactly as it did before v0.2.92). The soft call therefore SWALLOWS, and a
    swallowed exception that leaves no trace is the exact failure class this
    release exists to remove. Two consequences would otherwise be invisible
    forever: the entity silently pays a full-body embed the fan-out discards,
    and — if the second call happens to succeed where the first did not — a
    real fault in the chunker or in code-model resolution is never observed at
    all.

    The message names the ENTITY and the CONSEQUENCE, not just the failure:
    "chunk planning failed" is nearly useless to whoever reads the log, whereas
    "chunk planning FAILED for boot.register (rust, CodeFunction) — entity
    embedded UN-CHUNKED, retrieval for it is DEGRADED: RuntimeError: …" is
    actionable.

    Rate limit: the first :data:`CHUNK_PLAN_WARN_LIMIT` occurrences are named
    individually; occurrence ``LIMIT + 1`` returns a single suppression notice;
    every later one returns ``None`` and is represented only by the walk's
    end-of-run total.
    """
    if occurrence <= 0 or occurrence > CHUNK_PLAN_WARN_LIMIT + 1:
        return None
    if occurrence == CHUNK_PLAN_WARN_LIMIT + 1:
        return (
            f"chunk planning has now failed for {occurrence} entities — "
            "further per-entity warnings suppressed; the walk's end-of-run "
            "summary reports the total"
        )
    kind = coll_name.rsplit("_", 1)[-1] if coll_name else "?"
    return (
        f"chunk planning FAILED for {identity_key or '?'} "
        f"({language or 'unknown-language'}, {kind}) — entity embedded "
        f"UN-CHUNKED, retrieval for it is DEGRADED: "
        f"{type(exc).__name__}: {exc}"
    )


def is_stale_tail_row(
    raw_full_name: str,
    props: Any,
    *,
    full_name: str,
    file_path_rel: str,
    min_chunk_num: int,
) -> bool:
    """F3 chunk-SHRINK: is this row a leftover TAIL chunk of ONE entity?

    True only for a row that (a) matches ``full_name`` EXACTLY — never a
    tokenized filter, because ``full_name`` is word-tokenized TEXT in Weaviate
    and an ``equal`` filter can match a DIFFERENT entity — (b) belongs to the
    same source file when one is known, and (c) carries
    ``chunk_num >= min_chunk_num``. A NULL / non-int ``chunk_num`` is never a
    tail row: an unparseable row is left alone rather than deleted.

    v0.2.92: moved out of the analyzer so the DELETE decision is unit-testable
    on its own; the analyzer keeps only the scan and the delete I/O.
    """
    if raw_full_name != full_name:
        return False
    if not isinstance(props, dict):
        return False
    if file_path_rel and (props.get("file_path") or "") != file_path_rel:
        return False
    try:
        return int(props.get("chunk_num")) >= int(min_chunk_num)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def survivor_needs_total_patch(
    props: Any,
    *,
    full_name: str,
    project: str,
    project_source: str,
    file_path_rel: str,
    new_total: int,
) -> bool:
    """C-13 chunk-SHRINK: does this SURVIVING chunk row need a ``total_chunks``
    patch?

    ``total_chunks`` is deliberately excluded from the content hash, so a shrink
    hash-SKIPS the surviving rows and they keep claiming the pre-shrink count —
    harmless for the write path, wrong for the collapse/tier readers. True only
    for a row scoped to exactly this entity (project / project_source /
    file_path / full_name, all compared in Python, never a tokenized filter),
    holding a SURVIVING ``chunk_num < new_total``, whose stored ``total_chunks``
    differs. A row already at the right value returns False — that is what makes
    the patch idempotent (a converged entity does zero writes); a NULL / non-int
    stored total returns True (patch it).

    v0.2.92: moved out of the analyzer beside its sibling decision above.
    """
    if not isinstance(props, dict):
        return False
    if (props.get("full_name") or "") != full_name:
        return False
    if project and (props.get("project") or "") != project:
        return False
    if project_source and (props.get("project_source") or "") != project_source:
        return False
    if file_path_rel and (props.get("file_path") or "") != file_path_rel:
        return False
    try:
        if int(props.get("chunk_num")) >= new_total:  # type: ignore[arg-type]
            return False  # a tail row (should already have been deleted)
    except (TypeError, ValueError):
        return False  # NULL/non-int chunk_num → not an addressable survivor
    try:
        return int(props.get("total_chunks")) != new_total  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True  # NULL/non-int stored total → patch it


def dispatch_deferred_embed(
    insert_params: Any,
    *,
    is_multi_chunk: bool,
    resolver: Optional[Callable[[], None]],
    eager_shape_fn: Callable[[Any], Any],
) -> None:
    """v0.2.73 (FIX-B2) + v0.2.92 (Defect A): route one entity's deferred embed.

    Walkers pass a zero-arg embed callable as ``insert_params['_deferred_embed']``
    instead of an eager ``vector``. Three outcomes, in order:

    * **multi-chunk** — POP the callable and embed NOTHING. The fan-out embeds
      each chunk separately and discards whatever the resolver would produce,
      so running the resolver is pure waste (Defect A). The pop is MANDATORY,
      not cosmetic: ``build_chunk_write_params`` does ``dict(insert_params)``
      and ``_write_one_object`` splats the result into
      ``collection.data.replace(uuid=..., **insert_params)`` — a stranded
      callable is a ``TypeError`` → ``insert_errors > 0`` → the run cannot
      certify the extractor generation → every future update re-spawns a full
      force-rewalk, forever.
    * **resolver present** — call it. It owns the SKIP / STAMP / EMBED decision
      and pops the key itself.
    * **no resolver** — a minimal test stub that binds ``_dedup_insert`` as an
      unbound method without the FIX-B2 helpers. Pop the callable so it cannot
      reach Weaviate's kwargs, and best-effort embed eagerly.

    No-op when ``insert_params`` is not a dict or carries no ``_deferred_embed``
    key (call sites that still set ``vector`` eagerly).
    """
    if not isinstance(insert_params, dict):
        return
    if "_deferred_embed" not in insert_params:
        return
    if is_multi_chunk:
        insert_params.pop("_deferred_embed", None)
        return
    if resolver is not None:
        resolver()
        return
    deferred = insert_params.pop("_deferred_embed", None)
    if not callable(deferred):
        return
    try:
        embedding = deferred()
    except Exception:  # noqa: BLE001 — an embed failure must not wedge a write
        embedding = None
    shaped = eager_shape_fn(embedding)
    if shaped:
        insert_params["vector"] = shaped


def fan_out_chunk_writes(
    insert_params: dict,
    props: dict,
    chunk_texts: Sequence[str],
    chunk_uuids: Sequence[str],
    is_function: bool,
    *,
    embed_fn: Callable[[str], Any],
    write_fn: Callable[[str, dict], str],
) -> Optional[str]:
    """v0.2.92: write one entity's N chunk rows; return the CANONICAL uuid.

    Each chunk gets its OWN vector from ``embed_fn(chunk_text)`` — never the
    parent's full-body vector, which describes a different text
    (:func:`build_chunk_write_params` drops an inherited ``vector`` when the
    chunk's own is falsy). ``write_fn(uuid, params)`` is the analyzer's single
    write primitive; what it returns for chunk 0 is the canonical uuid the
    caller's ``func_uuid`` / ``class_uuid`` captures.
    """
    total = len(chunk_texts)
    canonical: Optional[str] = None
    for i, chunk_text in enumerate(chunk_texts):
        chunk_params = build_chunk_write_params(
            insert_params, props, chunk_text, is_function, i, total,
            embed_fn(chunk_text),
        )
        written = write_fn(chunk_uuids[i], chunk_params)
        if i == 0:
            canonical = written
    return canonical


def chunk_identities(
    chunk_texts: List[str],
    props: dict,
    is_function: bool,
    identity_key: str,
    total: int,
    *,
    uuid_fn: Callable[[str], str],
    hash_fn: Callable[[dict], str],
) -> Tuple[List[str], List[str]]:
    """v0.2.82 (G1 task 3): derive (uuids, content_hashes) for every chunk.

    The ONE home for chunk identity + per-chunk-hash derivation. chunk 0 keys
    on the bare ``identity_key`` (UUID byte-identical to the pre-chunking
    single-object UUID); chunk ``i`` on ``<key>::<i>``. ``uuid_fn(key)`` mints
    the deterministic UUID for a chunk key; ``hash_fn(props)`` computes the
    content hash for a chunk's props (both injected so this stays I/O-free and
    the analyzer keeps its module-local UUID/hash seeds). A hash_fn failure is
    caught → empty hash → the stamp precheck treats it as unknown → re-embed.
    """
    uuids: List[str] = []
    hashes: List[str] = []
    body_key = "function_body" if is_function else "class_body"
    for i, chunk_text in enumerate(chunk_texts):
        key = identity_key if i == 0 else f"{identity_key}::{i}"
        uuids.append(uuid_fn(key))
        chunk_props = dict(props)
        chunk_props[body_key] = chunk_text
        chunk_props["chunk_num"] = i
        chunk_props["total_chunks"] = total
        try:
            hashes.append(hash_fn(chunk_props))
        except Exception:  # noqa: BLE001 — hashing must never wedge a write
            hashes.append("")
    return uuids, hashes


def build_chunk_write_params(
    insert_params: dict,
    props: dict,
    chunk_text: str,
    is_function: bool,
    chunk_num: int,
    total: int,
    chunk_vec: Any,
) -> dict:
    """v0.2.82 (G1 task 3): build one chunk's ``insert_params`` for the write.

    Copies the shared ``props``, overrides the body field with this chunk's
    text, stamps ``chunk_num``/``total_chunks``, drops any inherited
    ``content_hash`` (the body differs per chunk → ``_write_one_object``
    re-stamps it), and wires the chunk's own vector. A falsy ``chunk_vec``
    (embed failed for this chunk) leaves ``vector`` UNSET — never carries the
    parent's full-body vector, which belongs to a different text. Pure: builds
    and returns a fresh dict; mutates nothing the caller holds.
    """
    chunk_props = dict(props)
    chunk_props["function_body" if is_function else "class_body"] = chunk_text
    chunk_props["chunk_num"] = chunk_num
    chunk_props["total_chunks"] = total
    chunk_props.pop("content_hash", None)
    chunk_params = dict(insert_params)
    chunk_params["properties"] = chunk_props
    if chunk_vec:
        chunk_params["vector"] = chunk_vec
    elif "vector" in chunk_params:
        del chunk_params["vector"]
    return chunk_params


def all_chunks_stampable(
    fingerprints: List[Optional[dict]],
    chunk_hashes: List[str],
    total: int,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
) -> bool:
    """v0.2.82 (G1 task 3): pure verdict for the multi-chunk STAMP fast path.

    True iff EVERY chunk row is present, hash-matched, and classifies
    :attr:`RowAction.STAMP` (``floor ≤ rev < current`` with a positive vector
    and the SAME ``total_chunks``). Any absent fingerprint, empty hash, or
    non-STAMP chunk → False (the caller re-embeds all chunks — fail-safe). The
    analyzer keeps only the I/O (per-chunk reads + the ``data.update`` patches);
    this owns the DECISION so it can never drift from :func:`classify_row`.

    ``fingerprints[i]`` is the dict from ``_read_existing_object_fingerprint``
    (``{"content_hash", "embed_revision", "total_chunks"}``) or ``None`` when
    the row was absent/unreadable. ``chunk_hashes[i]`` is the content hash the
    analyzer computed for chunk ``i`` this run.
    """
    if not fingerprints or len(fingerprints) != len(chunk_hashes):
        return False
    for fp, ch in zip(fingerprints, chunk_hashes):
        if fp is None or not ch:
            return False
        action = classify_row(
            fp.get("content_hash") or "", fp.get("embed_revision"), ch,
            current_revision=current_revision,
            floor_revision=floor_revision,
            vectorless_sentinel=vectorless_sentinel,
            is_chunkable=True,
            stored_total_chunks=fp.get("total_chunks"),
            computed_total_chunks=total,
        )
        if action is not RowAction.STAMP:
            return False
    return True


def all_chunks_skippable(
    fingerprints: "Sequence[Optional[dict]]",
    chunk_hashes: "Sequence[str]",
    total: int,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
) -> bool:
    """PURE: is EVERY chunk of a multi-chunk entity a plain ``SKIP``?

    The sibling of :func:`all_chunks_stampable`, which answers the same shape
    of question for the STAMP action. That one returns True only when every
    chunk is a STAMP candidate (``floor <= rev < current``), so a FULLY
    CONVERGED multi-chunk entity — content-identical AND already at the current
    revision, i.e. ``SKIP`` — fell through it and the analyzer re-embedded every
    one of its chunks. Measured on a converged 766-entity tree: 109 wasted chunk
    embeds (14.2% of the graph) producing zero writes; 1,229 on the orchestrator
    repo. The per-FILE gate normally hides this by stopping the walk earlier,
    but it fires on any changed file today and ``--force-rewalk`` fires it on
    every file.

    The per-row DECISION is still :func:`classify_row` — the single home for
    SKIP/STAMP/EMBED. This only aggregates it across an entity's chunks.

    Fail-safe: an absent fingerprint, an empty hash, a length mismatch or ANY
    non-SKIP chunk → ``False`` (the caller then tries the STAMP path and, if
    that declines, re-embeds). Only a positively-confirmed all-SKIP avoids the
    embeds.

    v0.2.92: MOVED here from ``codegraph_extractor_generation`` (where it landed
    only because this file was held by another lane in v0.2.91). Its deferred
    ``from vco_lib.codegraph_guards import ...`` — which existed solely to break
    that circular import — is gone with the move.
    """
    if not fingerprints or len(fingerprints) != len(chunk_hashes):
        return False
    for fp, chunk_hash in zip(fingerprints, chunk_hashes):
        if fp is None or not chunk_hash:
            return False
        if classify_row(
            fp.get("content_hash") or "",
            fp.get("embed_revision"),
            chunk_hash,
            current_revision=current_revision,
            floor_revision=floor_revision,
            vectorless_sentinel=vectorless_sentinel,
            is_chunkable=True,
            stored_total_chunks=fp.get("total_chunks"),
            computed_total_chunks=total,
        ) is not RowAction.SKIP:
            return False
    return True


def stamp_all_chunks(
    chunk_uuids: List[str],
    chunk_hashes: List[str],
    total: int,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
    read_fp: Callable[[str], Optional[dict]],
    patch_rev: Callable[[str], bool],
) -> bool:
    """v0.2.82 (G1 task 3): the multi-chunk STAMP fast path, orchestration + all.

    Reads every chunk row's fingerprint (``read_fp(uuid)``), and — ONLY when
    :func:`all_chunks_stampable` says all are stampable — patches each chunk's
    ``embed_revision`` to ``current_revision`` via ``patch_rev(uuid)`` (which
    returns False on failure). Returns True iff every chunk was stamped (so the
    caller skips all chunk embeds); False otherwise (the caller re-embeds all
    chunks — fail-safe). The two callables are the analyzer's ONLY I/O; the
    read-all-first / patch-only-if-all-stampable discipline (never a half-
    stamped entity) lives here so it can't drift.

    ``patch_rev`` MUST also mark the row visited (so ``--prune-stale`` keeps
    it) — that side-effect is the analyzer's, threaded through the callable.
    """
    fingerprints = [read_fp(cu) for cu in chunk_uuids]
    if not all_chunks_stampable(
        fingerprints, chunk_hashes, total,
        current_revision=current_revision,
        floor_revision=floor_revision,
        vectorless_sentinel=vectorless_sentinel,
    ):
        return False
    for cu in chunk_uuids:
        if not patch_rev(cu):
            return False  # a patch failure → re-embed (safe)
    return True


def skip_or_stamp_all_chunks(
    chunk_uuids: List[str],
    chunk_hashes: List[str],
    total: int,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
    read_fp: Callable[[str], Optional[dict]],
    patch_rev: Callable[[str], bool],
    note_visited: Callable[[str], None],
) -> bool:
    """v0.2.92: "does this multi-chunk entity need ANY embed?" — both branches.

    Returns True when no chunk needs re-embedding, having done whatever that
    conclusion owes:

    * **all SKIP** (content-identical AND already at the current revision) →
      no embed, no write, no patch — but ``note_visited(uuid)`` for EVERY
      chunk, because a concurrent ``--prune-stale`` and the unconditional
      per-file entity reconcile both delete rows this walk did not visit. A
      skip that forgets to mark is a delete.
    * **all STAMP** (``floor <= rev < current``) → delegate to
      :func:`stamp_all_chunks`, which owns the read-all-first /
      patch-only-if-all-stampable discipline (never a half-stamped entity).
      ``patch_rev`` also owes the visited mark.

    The ORDERING is the decision this function owns: SKIP is tried first
    because :func:`all_chunks_stampable` accepts only STAMP candidates, so a
    FULLY CONVERGED entity fell through it and re-embedded every chunk (109
    wasted embeds on a converged 766-entity tree). The fingerprint point-reads
    are memoized across both branches, so the STAMP path never re-issues the
    reads the SKIP precheck already made.

    Fail-safe: any exception in the SKIP precheck falls through to the STAMP
    attempt, and a declining STAMP returns False → the caller re-embeds every
    chunk. Only a positively-confirmed verdict avoids the embeds.
    """
    memo: Dict[str, Optional[dict]] = {}

    def _fp(chunk_uuid: str) -> Optional[dict]:
        if chunk_uuid not in memo:
            memo[chunk_uuid] = read_fp(chunk_uuid)
        return memo[chunk_uuid]

    try:
        if all_chunks_skippable(
            [_fp(cu) for cu in chunk_uuids], chunk_hashes, total,
            current_revision=current_revision,
            floor_revision=floor_revision,
            vectorless_sentinel=vectorless_sentinel,
        ):
            for cu in chunk_uuids:
                note_visited(cu)
            return True
    except Exception:  # noqa: BLE001 — any doubt → fall through to stamp/embed
        pass
    return stamp_all_chunks(
        chunk_uuids, chunk_hashes, total,
        current_revision=current_revision,
        floor_revision=floor_revision,
        vectorless_sentinel=vectorless_sentinel,
        read_fp=_fp,
        patch_rev=patch_rev,
    )


def stamp_single_chunk_props(coll_name: str, props: Any) -> None:
    """Stamp ``chunk_num=0`` / ``total_chunks=1`` on a single-object write.

    Only Function/Class carry chunk props (they are the chunkable entities);
    Module/API/Interaction are left untouched. Defensive: never clobbers a
    caller-preset value, and no-ops on a non-Function/Class collection or a
    non-dict ``props``. Mutates ``props`` in place — the analyzer's single
    write path already owns that dict.

    v0.2.92: moved out of the analyzer to sit with the rest of the chunk
    identity/shape family.
    """
    if not (coll_name.endswith("CodeFunction") or coll_name.endswith("CodeClass")):
        return
    if not isinstance(props, dict):
        return
    if "chunk_num" not in props:
        props["chunk_num"] = 0
    if "total_chunks" not in props:
        props["total_chunks"] = 1


def classify_stale_kind(
    stored_rev: Any,
    *,
    current_revision: int,
    floor_revision: int,
    vectorless_sentinel: int = 0,
) -> str:
    """Resync-driver reporting: split an owed row into ``embed_owed`` vs
    ``stamp_owed`` vs ``current``.

    * ``current``    — ``rev`` is the current revision (or ahead): no work owed.
    * ``stamp_owed`` — ``floor ≤ rev < current`` with a positive vector: a
      re-walk STAMPS it (cheap ``data.update``, no re-embed).
    * ``embed_owed`` — everything else that is stale: NULL / non-int
      (pre-migration), vectorless (``<= sentinel``) / negative, or below the
      floor. A re-walk re-embeds it.

    This is REPORTING ONLY — the owed-gate semantics are unchanged (a
    ``stamp_owed`` row still counts as owed work; the pass that stamps it is
    cheap). It lets the driver's convergence report say WHY the remaining work
    is owed without a second scan.
    """
    rev = _coerce_rev(stored_rev)
    if rev is None:
        return "embed_owed"
    if rev >= current_revision:
        return "current"
    if rev <= vectorless_sentinel:
        return "embed_owed"
    if rev < floor_revision:
        return "embed_owed"
    # floor ≤ rev < current, positive vector present → cheap stamp.
    return "stamp_owed"
