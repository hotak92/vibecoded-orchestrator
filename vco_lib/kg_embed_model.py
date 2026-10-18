# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which text-embedding model a KG collection is searched and written with.

v0.2.102 W6-I — the knowledge-graph half of the design the code graph got in
W6-B2 (:mod:`vco_lib.code_embed_model`); the Weaviate-side core both use is
:mod:`vco_lib.embed_slot_fill`.

Two questions, kept apart (owner ruling 2026-10-10):

* the **target** — the selected embedding profile's text model
  (:func:`vco_lib.embedding_service.resolve_active_text_model_id`, the ONE
  resolver: ``EMBEDDING_MODEL`` → ``ACTIVE_EMBEDDING`` → launcher.db
  ``embedding.active_profile`` → hardware pick → qwen3);
* the **record** — the model whose named vector is COMPLETE in one collection,
  kept per collection in the launcher DB (``app_state`` key
  ``kg_embed_record:<Collection>``; the shared KG and the development
  collection have no per-project binding row, so the record is keyed by the
  collection itself).

Before W6-I every KG reader and writer used the target: after a profile switch
the stored nodes kept only the previous model's slot, so every search on the
new slot missed them (the duplicate scan's abort was the visible symptom), and
every rewrite of a node dropped the old vector.

Now:

* **searches** use the record (per collection — a fan-out embeds the query
  once per distinct recorded model; :func:`recorded_query_model` is ``""`` in
  the steady state, so every search path stays byte-identical there);
* **writes** embed the target and, while its slot fills, also CARRY the
  recorded model's vector (:func:`complete_write_vectors`), so a rewritten node
  never leaves the slot searches still read. A write whose target vector did
  not come back fails LOUDLY — never a row holding only another model's vector;
* the **fill** is the existing UPDATE-only, idempotent slot enrichment
  (:func:`vco_lib.embedding_enrichment.enrich_collection_vectors`): it embeds
  only the rows that lack the target vector and keeps every other vector. It
  runs on the ordinary whole-tree sync (``kg-sync --all`` — launcher Sync,
  install seed, the detached retry driver), before the walk, so unchanged nodes
  then skip with ZERO re-embeds;
* the record **flips** to the target once a cursor scan proves its slot
  complete (:func:`settle`); switching back to a model whose slot is still
  complete is an instant flip;
* a scoped run (one file, the MCP store) that meets a pending fill leaves the
  auto-retryable ledger row ``kg_embed_fill_pending``, whose retry is the
  existing ``retry:py:kg_seed`` handler (``kg-sync --all``). A collection with
  NO record (pre-v0.2.102, stamp skipped) still resolves off the live sample
  on these paths (W6-J, review F1): the scoped rewrite CARRIES the sampled
  slot and the row is emitted, so the window cannot strand a node on the new,
  nearly-empty slot;
* the bundle update self-heals: it stamps a missing record off live data,
  settles a pending fill (the instant flip) and keeps that row truthful.

Never a substitute model: a target whose backend is down fails loudly
(writes, fill); queries embed with the recorded model only. OpenAI targets
need the embeddings consent (:mod:`vco_lib.openai_consent`); a cost-bearing
fill prints the object count first.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

#: ``app_state`` key prefixes (launcher.db) — one row per collection.
RECORD_KEY_PREFIX = "kg_embed_record:"
FILL_KEY_PREFIX = "kg_embed_fill:"
#: The ledger row a scoped run / the bundle self-heal leaves while a fill is
#: owed (declared in ``deferral_conditions.toml``; retry = ``kg_seed``).
DEFERRAL_CID = "kg_embed_fill_pending"

SOURCE_RECORDED = "recorded"
SOURCE_LIVE = "live"
SOURCE_TARGET = "target"

#: Canonical model per KG text slot, for reading a record off live data. The
#: legacy ``ollama_embed`` / ``openai_embed`` slots are ambiguous (several
#: models wrote them) and are deliberately absent: no guess.
_SLOT_MODEL = {
    "qwen3_embed": "qwen3-embedding:0.6b",
    "arctic2_embed": "snowflake-arctic-embed2:latest",
}
_OPENAI_TEXT_SLOT = "openai_text_embed"
#: Slots several models share — there the model NAME decides the space too.
_SHARED_TEXT_SLOTS = ("ollama_embed", "openai_embed", _OPENAI_TEXT_SLOT)
_LIVE_SAMPLE = 20
#: Readers re-read a collection's record at most this often per process.
_RECORD_TTL_SECS = 30.0


@dataclass(frozen=True)
class KgModelRecord:
    """What one KG collection's complete slot holds."""

    model: str
    dim: int
    slot: str


@dataclass(frozen=True)
class KgModelResolution:
    """The resolver's answer for one collection and one ROLE.

    ``model`` — what this role embeds with (the record for searches; the
    target for writes). ``filling`` — the target differs from the record and
    is owed a fill. ``carry`` — the model a WRITE also embeds with during the
    fill (the recorded one)."""

    collection: str
    model: str
    source: str
    target: str
    recorded: Optional[KgModelRecord]
    filling: bool
    role: str = "query"
    carry: Optional[str] = None
    target_refusal: Optional[str] = None

    @property
    def slot(self) -> str:
        return text_model_slot(self.model)[0]

    @property
    def target_refused(self) -> bool:
        return self.target_refusal is not None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def text_model_slot(model: str) -> "tuple[str, int]":
    """``(slot, dim)`` — the ONE text slot map (``embedding_service.TEXT_SLOT_MAP``)."""
    from vco_lib.embedding_service import _resolve_text_slot, _to_openai_api_model

    return _resolve_text_slot(_to_openai_api_model(str(model or "")))


def _norm(model: str) -> str:
    from vco_lib.embedding_service import _to_openai_api_model

    m = _to_openai_api_model(str(model or "").strip().lower())
    if m.endswith(":latest"):
        m = m[: -len(":latest")]
    return m.rsplit("/", 1)[-1]


def same_text_model(a: str, b: str) -> bool:
    """Do two text model ids embed into the same vector space?"""
    from vco_lib.embed_slot_fill import same_model

    return same_model(a, b, text_model_slot, _norm, _SHARED_TEXT_SLOTS)


def is_openai_text_model(model: str) -> bool:
    return text_model_slot(model)[0] in (_OPENAI_TEXT_SLOT, "openai_embed")


def consent_refusal(model: str, app_state: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """The refusal text when ``model`` is an OpenAI model and the EMBEDDINGS
    consent is off; None otherwise."""
    if not is_openai_text_model(model):
        return None
    from vco_lib.openai_consent import embeddings_consent_granted, refusal_message

    if embeddings_consent_granted(app_state):
        return None
    return refusal_message(model, "the KG text profile")


def target_text_model(env: Optional[Mapping[str, str]] = None) -> str:
    from vco_lib.embedding_service import resolve_active_text_model_id

    return resolve_active_text_model_id() if env is None else resolve_active_text_model_id(env)


# ---------------------------------------------------------------------------
# The record + fill progress (launcher DB ``app_state``)
# ---------------------------------------------------------------------------


def _parse_json(raw: Optional[str]) -> Optional[dict]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def read_record(collection: str) -> Optional[KgModelRecord]:
    """The collection's recorded model (no cache). None when absent."""
    from vco_lib.launcher_db_reader import read_app_state_value

    data = _parse_json(read_app_state_value(RECORD_KEY_PREFIX + collection)) if collection else None
    model = str((data or {}).get("model") or "").strip()
    if not model:
        return None
    slot, dim = text_model_slot(model)
    try:
        dim = int(data.get("dim") or dim)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        pass
    return KgModelRecord(model=model, dim=dim, slot=slot)


def _write_key(key: str, value: str) -> bool:
    from vco_lib.launcher_db_reader import _discover_db_path
    from vco_lib.launcher_db_writer import write_app_state_key

    db_path = _discover_db_path()
    if db_path is None:
        return False
    write_app_state_key(db_path, key, value)
    return True


def write_record(collection: str, record: KgModelRecord) -> bool:
    """Stamp the record; the collection's fill progress is cleared with it."""
    ok = _write_key(RECORD_KEY_PREFIX + collection, json.dumps(
        {"model": record.model, "dim": record.dim, "slot": record.slot,
         "at": int(time.time() * 1000)}))
    if ok:
        _write_key(FILL_KEY_PREFIX + collection, "")
        _RECORD_CACHE.pop((_db_key(), collection), None)
    return ok


def read_fill(collection: str) -> Optional[dict]:
    from vco_lib.launcher_db_reader import read_app_state_value

    return _parse_json(read_app_state_value(FILL_KEY_PREFIX + collection)) if collection else None


def write_fill(collection: str, target: str, filled: int, total: int) -> bool:
    return _write_key(FILL_KEY_PREFIX + collection, json.dumps(
        {"target": target, "filled": int(filled), "total": int(total),
         "at": int(time.time() * 1000)}))


#: Reader cache: (launcher.db path, collection) → (monotonic time, record or None).
_RECORD_CACHE: "dict[tuple[str, str], tuple[float, Optional[KgModelRecord]]]" = {}
#: Live-derived records (no DB record yet) — one sample per collection per process.
_LIVE_CACHE: "dict[tuple[str, str], Optional[KgModelRecord]]" = {}


def _db_key() -> str:
    from vco_lib.launcher_db_reader import _discover_db_path

    return str(_discover_db_path() or "")


def _cached_record(collection: str) -> Optional[KgModelRecord]:
    now = time.monotonic()
    key = (_db_key(), collection)
    hit = _RECORD_CACHE.get(key)
    if hit is not None and now - hit[0] < _RECORD_TTL_SECS:
        return hit[1]
    try:
        record = read_record(collection)
    except Exception:  # noqa: BLE001 — an unreadable record is "none"
        record = None
    _RECORD_CACHE[key] = (now, record)
    return record


def _record_for_slot(slot: str, dim: int) -> Optional[KgModelRecord]:
    if slot in _SLOT_MODEL:
        return KgModelRecord(_SLOT_MODEL[slot], dim, slot)
    if slot == _OPENAI_TEXT_SLOT:
        model = "text-embedding-3-large" if dim == 3072 else "text-embedding-3-small"
        return KgModelRecord(model, dim, slot)
    return None


def record_from_live(collection: str, target: str, *,
                     weaviate_url: Optional[str] = None) -> Optional[KgModelRecord]:
    """A record read off a SAMPLE of stored objects (readers and the scoped
    write path, never stamped): the target when its slot is populated on every
    sampled object and no fill toward it is in progress, else the first
    unambiguous slot a MAJORITY of the sampled objects hold. None when nothing
    is stored, no slot holds a majority, or Weaviate cannot be asked.

    Why a majority and not the every-object intersection (W6-J, review F1): a
    node rewritten while no record existed can carry the target's vector ONLY,
    and one such object empties the intersection — the old read then returned
    None and the resolver fell to the TARGET, searching the new, nearly-empty
    slot and missing the whole collection. The majority slot is where the data
    still is. First-seen order breaks count ties (the sample payload's own
    order), so a collection with two complete slots reads the same slot the
    pre-W6-J intersection read."""
    from vco_lib.embed_slot_fill import sample_slot_population

    try:
        population, sampled = sample_slot_population(collection, weaviate_url, _LIVE_SAMPLE)
    except Exception:  # noqa: BLE001
        return None
    if sampled <= 0 or not population:
        return None
    t_slot, t_dim = text_model_slot(target)
    fill = read_fill(collection) or {}
    target_filling = bool(fill.get("target")) and same_text_model(str(fill["target"]), target)
    t_dim_seen, t_count = population.get(t_slot, (0, 0))
    if not target_filling and t_count == sampled and t_dim_seen == t_dim:
        return KgModelRecord(target, t_dim, t_slot)
    best: Optional[KgModelRecord] = None
    best_count = 0
    for slot, (dim, count) in population.items():
        if target_filling and slot == t_slot:
            continue
        if count * 2 <= sampled:
            continue  # no majority — not the collection's serving slot
        rec = _record_for_slot(slot, dim)
        if rec is None:
            continue  # ambiguous/unknown slot — never a guess
        if best is None or count > best_count:
            best, best_count = rec, count
    return best


def _live_record_cached(collection: str, target: str) -> Optional[KgModelRecord]:
    key = (collection, _norm(target))
    if key not in _LIVE_CACHE:
        _LIVE_CACHE[key] = record_from_live(collection, target)
    return _LIVE_CACHE[key]


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

_UNSET: Any = object()


def resolve_kg_model(
    collection: str, *, role: str = "query", target: Optional[str] = None,
    record: Any = _UNSET, live: bool = True,
    app_state: Optional[Callable[[str], Optional[str]]] = None,
) -> KgModelResolution:
    """Which text model ``collection`` uses for ``role``.

    * no record (and nothing readable live) → the target, both roles;
    * record == target → the record;
    * record != target → searches keep the record; writes embed the target and
      carry the record (the fill). A target the embeddings consent refuses is
      NOT filled (``target_refusal``) — searches keep the record, and the
      write path's own consent gate refuses the target loudly.

    ``target`` defaults to the ONE text resolver; callers holding a live
    ``EmbeddingService`` pass its ``text_model_id``. ``record`` supersedes the
    launcher.db read (``None`` = known absent). ``live=False`` skips the
    Weaviate sample when no record exists; the default (``True``) pays one
    cached 20-object sample per collection per process, which is what keeps a
    pre-record collection's writes and ledger truthful (W6-J, review F1)."""
    target = target or target_text_model()
    source = SOURCE_RECORDED
    if record is _UNSET:
        record = _cached_record(collection) if collection else None
        if record is None and live and collection:
            record = _live_record_cached(collection, target)
            source = SOURCE_LIVE
    refusal = consent_refusal(target, app_state)
    if record is None:
        return KgModelResolution(collection, target, SOURCE_TARGET, target, None, False,
                                 role=role, target_refusal=refusal)
    if same_text_model(target, record.model):
        return KgModelResolution(collection, record.model, source, target, record, False,
                                 role=role, target_refusal=refusal)
    owed = refusal is None
    if role == "query" or not owed:
        return KgModelResolution(collection, record.model, source, target, record, owed,
                                 role=role, target_refusal=refusal)
    carry = record.model if consent_refusal(record.model, app_state) is None else None
    return KgModelResolution(collection, target, SOURCE_TARGET, target, record, True,
                             role=role, carry=carry, target_refusal=refusal)


def recorded_query_model(collection: str, *, service: Any = None) -> str:
    """The recorded model when it DIFFERS from the target (a selected model
    still filling), else ``""`` — the steady state, where callers keep their
    own (target) path untouched. ``service`` supplies the target when in
    hand; otherwise the ONE text resolver does (the same value the process's
    service is built from)."""
    if not collection:
        return ""
    target = str(getattr(service, "text_model_id", "") or "") or None
    res = resolve_kg_model(collection, role="query", target=target)
    if res.recorded is None or same_text_model(res.recorded.model, res.target):
        return ""
    return res.recorded.model


def recorded_query_slot(collection: str, *, service: Any = None) -> str:
    """:func:`recorded_query_model`'s slot, or ``""`` in the steady state."""
    model = recorded_query_model(collection, service=service)
    return text_model_slot(model)[0] if model else ""


# ---------------------------------------------------------------------------
# Sibling services (one per text model, sharing the parent's adapters)
# ---------------------------------------------------------------------------


def sibling_text_service(svc: Any, model: str) -> Any:
    """An EmbeddingService identical to ``svc`` but embedding text with
    ``model`` — shares the session and adapters (no probe, no new
    connection); memoised on ``svc``."""
    if same_text_model(model, str(getattr(svc, "text_model_id", "") or "")):
        return svc
    cache = getattr(svc, "_kg_text_siblings", None)
    if cache is None:
        cache = {}
        try:
            svc._kg_text_siblings = cache
        except Exception:  # noqa: BLE001 — a frozen double: no memo
            pass
    if model in cache:
        return cache[model]
    cls = type(svc)
    sib = cls(
        project_root=getattr(svc, "project_root", None),
        ollama_url=getattr(svc, "ollama_url", ""),
        code_embed_url=getattr(svc, "code_embed_url", ""),
        text_model_id=model,
        code_model_id=getattr(svc, "code_model_id", ""),
        openai_api_key=getattr(svc, "openai_api_key", ""),
        session=getattr(svc, "session", None),
        ollama_adapter=getattr(svc, "ollama", None),
        code_adapter=getattr(svc, "codeembed", None),
        openai_adapter=getattr(svc, "openai", None),
    )
    sib._openai_embeddings_consent = getattr(svc, "_openai_embeddings_consent", True)
    cache[model] = sib
    return sib


# ---------------------------------------------------------------------------
# Writes: the target is required, the record rides along during a fill
# ---------------------------------------------------------------------------


class KgWriteVectorMissing(RuntimeError):
    """A KG write whose TARGET vector did not come back (its backend failed).
    Raised instead of storing a row that holds only another model's vector."""


def complete_write_vectors(svc: Any, slots: "dict[str, list]", text: str,
                           collection: str,
                           truncated: "Optional[list[str]]" = None, *,
                           active_slot: Optional[str] = None) -> "dict[str, list]":
    """Finish a KG write's ``{slot: vector}`` map.

    1. The target (``svc``'s) slot MUST be present — otherwise this raises
       :class:`KgWriteVectorMissing` (loud; the caller counts a failure and the
       ledger retries). W6-H's "fallback-only write" (a secondary slot written
       while the active backend failed) left the node invisible to every search
       on the active slot; it is never stored now.
    2. While ``collection`` fills toward the target, the RECORDED model's
       vector is added (the carry) so a rewritten node stays in the slot
       searches read. A carry that cannot be embedded fails the write too —
       storing the row without it would drop the node from searches.
       ``truncated`` (the write's per-call truncation record) gains the carry
       slot when its vector came from a bounded leading window. ``active_slot``
       — the writer's own active-slot answer when it holds one (kg-sync's
       ``WeaviateWrapper.text_vector_slot``), else ``svc``'s."""
    active = active_slot if active_slot is not None else str(getattr(svc, "text_vector_slot", "") or "")
    if active and not slots.get(active):
        raise KgWriteVectorMissing(
            f"the KG's embedding model {getattr(svc, 'text_model_id', '?')} produced no "
            f"vector for this write ({active}); its backend did not answer. No other "
            "model's vector is stored in its place — the write fails and is retried.")
    if not collection:
        return slots
    # W6-J (review F1): the resolution SAMPLES live data when no record exists
    # (cached once per collection per process — only the first write pays it).
    # A pre-v0.2.102 collection whose stamp was skipped (Weaviate down at the
    # bundle run) is exactly where a scoped rewrite must not go target-only:
    # without the carry the node leaves the slot searches read.
    res = resolve_kg_model(collection, role="write",
                           target=str(getattr(svc, "text_model_id", "") or "") or None)
    if res.carry and svc is not None:
        cslot = text_model_slot(res.carry)[0]
        if not slots.get(cslot):
            sib = sibling_text_service(svc, res.carry)
            slots[cslot] = sib.embed_text(text)
            if truncated is not None and (getattr(sib, "_last_truncated_slots", None) or {}).get(cslot):
                truncated.append(cslot)
    return slots


# ---------------------------------------------------------------------------
# Fill, settle, self-heal
# ---------------------------------------------------------------------------


def _known_slots(target: str) -> "list[str]":
    return list(dict.fromkeys([text_model_slot(target)[0], *_SLOT_MODEL, _OPENAI_TEXT_SLOT]))


def derive_record_by_scan(collection: str, target: str, *,
                          client_factory: Optional[Callable[[], Any]] = None) -> Optional[KgModelRecord]:
    """The objective record: a cursor scan. The target when its slot is
    complete, else the first known slot every object holds. None when the
    collection is empty, nothing is complete, or Weaviate cannot be asked."""
    from vco_lib.embed_slot_fill import counters_complete, sample_common_vectors, scan_slot_counts

    slots = _known_slots(target)
    measured = scan_slot_counts([collection], slots, client_factory=client_factory)
    if measured is None:
        return None
    counts, total = measured
    t_slot, t_dim = text_model_slot(target)
    if counters_complete(counts, total, t_slot):
        return KgModelRecord(target, t_dim, t_slot)
    for slot in slots[1:]:
        if counters_complete(counts, total, slot):
            dim = 0
            if slot == _OPENAI_TEXT_SLOT:
                try:
                    dim = int(sample_common_vectors(collection, None, 1).get(slot, 0))
                except Exception:  # noqa: BLE001
                    dim = 0
            rec = _record_for_slot(slot, dim or text_model_slot(_SLOT_MODEL.get(slot, ""))[1])
            if rec is not None:
                return rec
    return None


def settle(collection: str, target: str, *,
           client_factory: Optional[Callable[[], Any]] = None) -> Optional[str]:
    """Measure the target's slot: COMPLETE → flip the record (searches move;
    ``"complete"``); otherwise stamp the progress (``"filling"``). None when it
    cannot be measured, or the collection is empty."""
    from vco_lib.embed_slot_fill import counters_complete, scan_slot_counts

    slot, dim = text_model_slot(target)
    measured = scan_slot_counts([collection], [slot], client_factory=client_factory)
    if measured is None:
        return None
    counts, total = measured
    if total <= 0:
        return None
    if counters_complete(counts, total, slot):
        write_record(collection, KgModelRecord(target, dim, slot))
        return "complete"
    write_fill(collection, target, counts.get(slot, 0), total)
    return "filling"


def _ensure_record(collection: str, target: str, *,
                   client_factory: Optional[Callable[[], Any]] = None) -> Optional[KgModelRecord]:
    record = read_record(collection)
    if record is None:
        record = derive_record_by_scan(collection, target, client_factory=client_factory)
        if record is not None:
            write_record(collection, record)
    return record


def _fill_line(collection: str, res: KgModelResolution, weaviate_url: Optional[str] = None) -> str:
    from vco_lib.embed_slot_fill import object_count

    try:
        n: Any = object_count(collection, weaviate_url)
    except Exception:  # noqa: BLE001
        n = "?"
    cost = " through the OpenAI API (cost-bearing)" if is_openai_text_model(res.target) else ""
    if res.carry and is_openai_text_model(res.carry):
        cost += (f"; nodes rewritten meanwhile also refresh their {res.carry} vector "
                 "through the OpenAI API (cost-bearing)")
    return (f"[vct] kg-embed: {collection} moves to {res.target} (slot {res.slot}); "
            f"filling up to {n} object(s){cost} — existing vectors are kept, searches stay "
            f"on {res.recorded.model if res.recorded else '?'} until the slot is complete.")


def fill_collection(
    collection: str, svc: Any, *, log: Callable[[str], None] = print,
    enrich: Optional[Callable[..., Any]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
) -> Optional[str]:
    """The fill for one collection, run by a whole-tree sync BEFORE its walk:
    stamp a missing record (scan), and — when the target differs — make the
    target's slot exist (additive), print the count (+ cost), run the
    UPDATE-only enrichment (only rows lacking the target vector are embedded,
    every other vector kept), then settle. Returns ``settle``'s verdict, or
    None when nothing was owed. A down target backend propagates as the
    enrichment's loud ``NoEmbeddingBackendError`` — never a substitute model."""
    from vco_lib.embed_slot_fill import ensure_slot
    from vco_lib.weaviate_schema import KG_NAMED_VECTORS

    target = str(getattr(svc, "text_model_id", "") or "") or target_text_model()
    record = _ensure_record(collection, target, client_factory=client_factory)
    res = resolve_kg_model(collection, role="write", target=target, record=record)
    if res.target_refused:
        log(f"[vct] {res.target_refusal} {collection} keeps "
            f"{res.recorded.model if res.recorded else target}.")
        return None
    if not res.filling:
        return None
    slot, dim = text_model_slot(target)
    err = ensure_slot([collection], slot, dim, KG_NAMED_VECTORS, model=target, add=True)
    if err:
        log(f"[vct] kg-embed: cannot fill {target} into {collection} yet ({err}); "
            f"searches stay on {res.recorded.model if res.recorded else '?'}.")
        return None
    log(_fill_line(collection, res))
    if enrich is None:
        from vco_lib.embedding_enrichment import enrich_collection_vectors as enrich
    report = enrich(collection, slot, embedding_service=svc,
                    **({"weaviate_client_factory": client_factory} if client_factory else {}))
    failed = int(getattr(report, "failed", 0) or 0)
    if failed:
        log(f"[vct] kg-embed: {collection}: {failed} object(s) could not get a {target} "
            "vector this run (named in the enrichment warnings); the fill resumes next run.")
    return settle(collection, target, client_factory=client_factory)


def fill_owed(collection: str, target: Optional[str] = None) -> bool:
    """A recorded collection whose target differs (and is not refused)."""
    try:
        res = resolve_kg_model(collection, role="write", target=target, live=False)
    except Exception:  # noqa: BLE001
        return False
    return bool(res.filling and res.recorded is not None)


def _emit_fill_deferral(folder: Path, owed: "list[tuple[str, str, str]]") -> None:
    try:
        from vco_lib.deferral_emit import DeferralEntry, emit

        names = ", ".join(f"{c} ({r} → {t})" for c, r, t in owed)
        emit(Path(folder), DeferralEntry(
            condition_id=DEFERRAL_CID,
            title=f"KG embedding model change: {len(owed)} collection(s) still filling",
            detected=(f"The selected embedding model differs from the model these KG "
                      f"collection(s) are complete in: {names}. Searches keep using the "
                      "recorded model (nothing is missed meanwhile); writes already embed "
                      "both. The new model's vectors are filled additively — no existing "
                      "vector is dropped."),
            why_deferred=("A single-file sync or the bundle update does not embed a whole "
                          "collection. The fill runs on the next whole-tree sync "
                          "(`kg-sync --all`), which VCO's retry driver starts by itself once "
                          "the embedding backend answers; the row clears when every listed "
                          "collection's new slot is complete."),
            command_to_apply="# Run the whole-tree sync now (only missing vectors are embedded):\n"
                             ".claude/scripts/kg-sync --all",
            severity="info",
        ))
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (kg-embed deferral emit failed: {exc})", file=sys.stderr)


def _fill_deferral_present(folder: Path) -> bool:
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(folder))
        return bool(report) and report.has_condition(DEFERRAL_CID)
    except Exception:  # noqa: BLE001 — unreadable ledger ⇒ treat as absent
        return False


def _resolve_fill_deferral(folder: Path) -> None:
    if not _fill_deferral_present(folder):
        return  # nothing to clear — no ledger write at all
    try:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(Path(folder), (DEFERRAL_CID,))
    except Exception as exc:  # noqa: BLE001
        print(f"   (kg-embed deferral clear failed: {exc})", file=sys.stderr)


def _owed_list(collections: Iterable[str], target: str) -> "list[tuple[str, str, str]]":
    owed = []
    for name in dict.fromkeys(c for c in collections if c):
        # W6-J (review F1): live sampling is allowed here so a collection with
        # NO record whose sample shows a non-target slot still owes the fill —
        # this is what emits `kg_embed_fill_pending` from a scoped run (the
        # sample is cached once per collection per process; a recorded
        # collection never pays it).
        res = resolve_kg_model(name, role="write", target=target)
        if res.filling and res.recorded is not None:
            owed.append((name, res.recorded.model, target))
    return owed


def fill_before_walk(collections: Iterable[str], svc: Any, *,
                     log: Callable[[str], None] = print) -> "dict[str, Optional[str]]":
    """``kg-sync --all``'s pre-walk hook: :func:`fill_collection` for each
    collection the run writes. Never raises (a failure is printed loudly and
    the fill stays owed — the walk still runs)."""
    out: "dict[str, Optional[str]]" = {}
    for name in dict.fromkeys(c for c in collections if c):
        try:
            out[name] = fill_collection(name, svc, log=log)
        except Exception as exc:  # noqa: BLE001 — loud, never aborts the sync
            log(f"❌ kg-embed: filling {name} failed: {exc} — searches stay on the "
                "recorded model; the fill is retried.")
            out[name] = None
    return out


def after_walk(collections: Iterable[str], folder: Any, svc: Any, *, full_tree: bool) -> None:
    """``kg-sync``'s post-walk hook. A whole-tree run settles each owed
    collection (the rows its walk wrote carry the target) and stamps a missing
    record; any run then keeps ``kg_embed_fill_pending`` truthful (emit while
    owed, resolve when nothing is). Never raises."""
    try:
        target = str(getattr(svc, "text_model_id", "") or "") or target_text_model()
        names = list(dict.fromkeys(c for c in collections if c))
        if full_tree:
            for name in names:
                if read_record(name) is None:
                    _ensure_record(name, target)
                elif fill_owed(name, target):
                    settle(name, target)
        owed = _owed_list(names, target)
        if owed:
            # A scoped run (every edit) writes the row once; a whole-tree run
            # refreshes it with what is still owed.
            if full_tree or not _fill_deferral_present(Path(folder)):
                _emit_fill_deferral(Path(folder), owed)
        elif full_tree:
            _resolve_fill_deferral(Path(folder))
    except Exception as exc:  # noqa: BLE001
        print(f"   (kg-embed settle skipped: {exc})", file=sys.stderr)


def _project_env(folder: Path) -> "dict[str, str]":
    """The project's projected env (``.claude/settings.json`` ``env`` merged
    with ``.claude/settings.local.json``, as Claude Code merges them — the ONE
    reader, :func:`vco_lib.claude_settings_env.read_settings_env`) — the
    channel its ACTIVE_EMBEDDING / EMBEDDING_MODEL reach its own processes by."""
    from vco_lib.claude_settings_env import read_settings_env

    return {str(k): str(v) for k, v in read_settings_env(Path(folder)).items()}


def folder_collections(folder: Any) -> "tuple[list[str], Optional[str]]":
    """``(collections, target)`` for a REGISTERED folder: its KG, shared KG and
    development collection, and the text model its own env selects."""
    from vco_lib.project_identity import resolve_identity

    identity, _snap = resolve_identity(folder)
    if identity is None or not identity.registered:
        return [], None
    names = [n for n in (identity.kg_primary, identity.kg_shared, identity.development) if n]
    env = _project_env(Path(folder))
    target = target_text_model(env) if env else target_text_model()
    return list(dict.fromkeys(names)), target


def reconcile_folder_kg_embedding(folder: Any, *, dry_run: bool = False) -> "dict[str, Optional[str]]":
    """The bundle update's self-heal (no embedding here): per collection,
    stamp a missing record off live data (scan), settle a pending fill (the
    instant switch back to a still-complete slot), and keep the ledger row
    truthful. Soft-fail — never aborts a bundle run."""
    out: "dict[str, Optional[str]]" = {}
    if dry_run:
        return out
    try:
        names, target = folder_collections(folder)
        if not names or not target:
            return out
        for name in names:
            if read_record(name) is None:
                rec = _ensure_record(name, target)
                out[name] = "recorded" if rec is not None else None
            elif fill_owed(name, target):
                out[name] = settle(name, target)
        owed = _owed_list(names, target)
        if owed:
            _emit_fill_deferral(Path(folder), owed)
        else:
            _resolve_fill_deferral(Path(folder))
    except Exception as exc:  # noqa: BLE001
        print(f"[vct] kg-embed: self-heal skipped for {folder} ({exc})", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Status (launcher)
# ---------------------------------------------------------------------------


def collection_status(collection: str, target: str, *, count: bool = True) -> "dict[str, Any]":
    record = read_record(collection)
    res = resolve_kg_model(collection, target=target, record=record)
    fill = read_fill(collection) or {}
    progress = fill if fill.get("target") and same_text_model(str(fill["target"]), target) else None
    total = progress.get("total") if progress else None
    if res.filling and progress is None and count:
        try:
            from vco_lib.embed_slot_fill import object_count

            total = object_count(collection, None)
        except Exception:  # noqa: BLE001
            total = None
    # W6-J: how many objects an UNRECORDED collection holds. A record is
    # stamped only off a non-empty collection with a complete slot, so "no
    # record" alone also describes an empty or absent collection (a fresh
    # project, a development collection nothing was written to) — the
    # launcher's re-scan alert needs to tell those apart from a stored KG
    # whose stamp failed or whose slots are all incomplete. None = not
    # counted (recorded collection, ``count=False``, or Weaviate unreachable).
    objects = None
    if record is None and count:
        try:
            from vco_lib.embed_slot_fill import object_count

            objects = int(object_count(collection, None))
        except Exception:  # noqa: BLE001
            objects = None
    return {
        "collection": collection,
        "recorded_model": record.model if record else None,
        "objects": objects,
        "target_model": target,
        "filling": res.filling,
        "filled": progress.get("filled") if progress else None,
        "total": total,
        "complete": bool(record) and not res.filling and not res.target_refused,
        "cost_bearing": is_openai_text_model(target),
        "target_refused": res.target_refused,
    }


def status_all(*, count: bool = True) -> "dict[str, Any]":
    """Per registered project: each KG collection's recorded vs target model
    and fill progress (DB reads; object counts only for a fill with no
    progress stamped yet)."""
    from vco_lib.launcher_db_reader import list_registered_projects

    projects = []
    for ref in list_registered_projects() or []:
        folder = str(getattr(ref, "folder", "") or "")
        if not folder:
            continue
        try:
            names, target = folder_collections(folder)
        except Exception:  # noqa: BLE001
            continue
        if not names or not target:
            continue
        projects.append({
            "project_id": getattr(ref, "id", None),
            "name": getattr(ref, "name", "") or Path(folder).name,
            "folder": folder,
            "target_model": target,
            "collections": [collection_status(n, target, count=count) for n in names],
        })
    return {"target": target_text_model(), "projects": projects}


def main(argv: Optional["list[str]"] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.kg_embed_model")
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="JSON: per-project KG recorded vs target model + fill")
    st.add_argument("--json", action="store_true", help="No-op: output is always JSON.")
    st.add_argument("--no-count", action="store_true", help="Skip the Weaviate object counts.")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        json.dump(status_all(count=not args.no_count), sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
