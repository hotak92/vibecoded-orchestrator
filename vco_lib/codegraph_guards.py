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

import subprocess
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple


def provenance_line(
    model: Any, dim: Any, embed_revision: int, repo_path: Any,
) -> str:
    """v0.2.82 (G6): build the ONE machine-readable provenance line WP-3 parses.

    Format (NORMATIVE — the launcher parser keys on it verbatim):
    ``CODEGRAPH_PROVENANCE model=<model> dim=<dim> embed_revision=<int>
    analyzed_commit=<sha|none>``. ``model``/``dim`` come from the caller's
    EmbeddingService config; the commit via ``git rev-parse HEAD`` on
    ``repo_path``, soft-failing to ``none`` (non-git tree / git absent / any
    error). Never raises — a provenance failure must not fail a build. (This is
    the single I/O exception to the module's zero-I/O rule: a read-only git
    probe, isolated here so the analyzer keeps only a one-line print.)
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
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            sha = (out.stdout or "").strip()
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
