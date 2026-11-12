# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Backfill a newly enabled SECONDARY text-embedding slot once (v0.2.106).

Owner ruling 2026-11-12, "backfill once": when a secondary text slot is
turned on (dual embeddings — ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` plus e.g.
``DUAL_EMBEDDING_ARCTIC_SECONDARY``, set from the launcher's embedding
settings through ``config_projection``), the rows already stored in the KG
collections a project writes (its KG, the shared KG it may write, the
development collection) get that slot's vector ONCE, in the background —
not only when each row is next rewritten.

What this module adds, and what it reuses
-----------------------------------------
It is a scheduler and a completion record, not a second embedding engine:

* the fill itself is the existing UPDATE-only slot enrichment
  (:func:`vco_lib.embedding_enrichment.enrich_collection_vectors`), driven
  with a sibling service whose text model IS the secondary
  (:func:`vco_lib.kg_embed_model.sibling_text_service`), so every vector a
  slot holds comes from that slot's own model and every other vector is kept;
* the slot is made to exist additively (:func:`vco_lib.embed_slot_fill.ensure_slot`);
* a row the secondary REFUSES (its text exceeds that model's window — the
  enrichment leaves it unfilled rather than cutting or averaging it) is
  re-laid by kg-sync's own writer in the same run: :func:`relay_reason` is a
  trigger of kg-sync's one re-embed gate (``_split_rows_heal_reason``), and
  the writer's row driver lays the node out at the finest split ANY model of
  its vector set needs, embeds first and replaces after — every row then
  carries every model's vector;
* it runs inside ``kg-sync --all`` (``kg_embed_model.fill_before_walk`` /
  ``after_walk``): launcher Sync, the install seed, and the detached retry
  driver (``retry:py:kg_seed``) all reach it;
* owed work is the auto-retryable ledger row ``kg_secondary_backfill_pending``
  (``deferral_conditions.toml``), retried by that same driver.

The stamp
---------
``<project>/.claude/state/kg-secondary-backfill.json`` (beside the
chunker-revision and extractor-generation stamps)::

    {"version": 1,
     "gen": {"arctic2_embed": 2},
     "complete": {"<Collection>": {"arctic2_embed":
         {"model": "snowflake-arctic-embed2:latest", "gen": 2, "rows": 812,
          "at": "2026-11-12T10:00:00+00:00"}}}}

A (collection, slot) pair is COMPLETE when its entry names the same model and
carries the slot's current generation. Two things make a stamp untrue, and
both are recorded where they happen (:func:`observe_document_write`, called
by every document fan-out of ``EmbeddingService``):

* a write whose configured secondary did not come back (backend down, breaker
  open) bumps that slot's generation — the next whole-tree sync re-checks and
  embeds only the rows that lack it;
* a process that writes with the secondary NOT configured drops that slot's
  stamps — turning the secondary off leaves every stored vector in place
  (nothing is cleared in Weaviate), and turning it on again resumes: only the
  rows without the vector are embedded.

A down backend
--------------
Before touching a collection the secondary model is probed with one tiny
embed. Down → nothing is scanned or embedded, the pair stays owed and the
ledger row names the reason; the retry driver re-runs the sync later (its
attempt cap applies), as does the next session, launcher Sync or update.
Never a whole-KG re-embed per sync: a stamped pair costs a file read, and an
owed pair embeds only rows that lack the vector.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

#: Per-project stamp, relative to the project folder.
STATE_REL = Path(".claude") / "state" / "kg-secondary-backfill.json"
#: The ledger row while a secondary slot is owed (``deferral_conditions.toml``).
DEFERRAL_CID = "kg_secondary_backfill_pending"
_STATE_VERSION = 1
_PROBE_TEXT = "vco secondary embedding probe"
#: Secondary slots only a live service can decide (key + embeddings consent).
_SERVICE_ONLY_SLOTS = ("openai_text_embed",)
_LOCK_TIMEOUT_S = 5.0

#: Outcomes of :func:`backfill_collection`.
COMPLETE = "complete"
RELAY = "relay"
INCOMPLETE = "incomplete"
DOWN = "down"

#: In-process state of the CURRENT kg-sync run (the backfill runs before the
#: walk in the same process): collection → file paths the walk must re-lay,
#: (collection, slot) → (model, generation) still to settle after the walk,
#: (collection, slot) → why the pair could not be filled this run.
_RELAY: "dict[str, set[str]]" = {}
_SETTLE: "dict[tuple[str, str], tuple[str, int]]" = {}
_BLOCKED: "dict[tuple[str, str], str]" = {}
#: Project roots whose "secondary switched off" check ran in this process.
_OBSERVED_OFF: "set[str]" = set()


# ---------------------------------------------------------------------------
# The stamp file
# ---------------------------------------------------------------------------


def state_path(folder: Any) -> Path:
    return Path(folder) / STATE_REL


def _empty_state() -> dict:
    return {"version": _STATE_VERSION, "gen": {}, "complete": {}}


def read_state(folder: Any) -> dict:
    """The stamp file's content; an empty state when absent or unreadable
    (unreadable = nothing proven complete — the conservative reading: the
    next whole-tree sync re-checks, embedding only rows that lack a vector)."""
    try:
        data = json.loads(state_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    gen = data.get("gen")
    complete = data.get("complete")
    return {"version": _STATE_VERSION,
            "gen": dict(gen) if isinstance(gen, dict) else {},
            "complete": dict(complete) if isinstance(complete, dict) else {}}


def _update_state(folder: Any, mutate: Callable[[dict], bool]) -> bool:
    """Locked read-modify-write of the stamp (concurrency: an exclusive
    sidecar file lock, ``vco_lib.atomic.exclusive_file_lock``, serialises the
    kg-sync process and the MCP server; the write is atomic). ``mutate``
    returns whether it changed anything — nothing is written otherwise."""
    from vco_lib.atomic import LockTimeout, atomic_write_json, exclusive_file_lock

    path = state_path(folder)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(path.with_suffix(".lock"), timeout_s=_LOCK_TIMEOUT_S):
            state = read_state(folder)
            if not mutate(state):
                return False
            atomic_write_json(path, state)
            return True
    except LockTimeout:
        print(f"   [vct] kg-secondary: {path} is locked; stamp not updated this time",
              file=sys.stderr)
    except OSError as exc:
        print(f"   [vct] kg-secondary: cannot write {path} ({exc})", file=sys.stderr)
    return False


def _gen(state: Mapping[str, Any], slot: str) -> int:
    try:
        return int((state.get("gen") or {}).get(slot, 0))
    except (TypeError, ValueError):
        return 0


def is_complete(state: Mapping[str, Any], collection: str, slot: str, model: str) -> bool:
    from vco_lib.kg_embed_model import same_text_model

    entry = ((state.get("complete") or {}).get(collection) or {}).get(slot)
    if not isinstance(entry, dict):
        return False
    try:
        same_gen = int(entry.get("gen", -1)) == _gen(state, slot)
    except (TypeError, ValueError):
        return False
    return same_gen and same_text_model(str(entry.get("model") or ""), model)


def stamp_complete(folder: Any, collection: str, slot: str, model: str, gen: int,
                   rows: int) -> bool:
    """Record (collection, slot) complete as of generation ``gen`` — the
    generation read BEFORE the work started, so a write that omitted the slot
    meanwhile (it bumped the generation) leaves the stamp already stale."""
    def _mutate(state: dict) -> bool:
        state["complete"].setdefault(collection, {})[slot] = {
            "model": model, "gen": int(gen), "rows": int(rows),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return True

    return _update_state(folder, _mutate)


def forget_disabled(folder: Any, configured_slots: Iterable[str]) -> bool:
    """Drop the stamps of every slot NOT in ``configured_slots``: rows are
    being written without it, so it is not complete any more. Stored vectors
    are untouched; a later re-enable embeds only the rows that lack one."""
    keep = set(configured_slots)

    def _mutate(state: dict) -> bool:
        changed = False
        for coll, slots in list(state["complete"].items()):
            if not isinstance(slots, dict):
                continue
            for slot in [s for s in slots if s not in keep]:
                del slots[slot]
                changed = True
            if not slots:
                del state["complete"][coll]
        return changed

    if not state_path(folder).is_file():
        return False
    if not any(s not in keep for slots in read_state(folder)["complete"].values()
               if isinstance(slots, dict) for s in slots):
        return False
    return _update_state(folder, _mutate)


def observe_document_write(project_root: Any, planned: Mapping[str, str],
                           written: Iterable[str]) -> None:
    """Called by every DOCUMENT fan-out (``EmbeddingService``): ``planned`` —
    the secondary slots this process is configured to write; ``written`` —
    the slots the write actually got. Cheap: a stat and, only when a stamp
    exists, a read; a write only when a stamp must be invalidated."""
    if not project_root:
        return
    folder = Path(project_root)
    root_key = str(folder)
    if root_key not in _OBSERVED_OFF:
        _OBSERVED_OFF.add(root_key)
        forget_disabled(folder, planned)
    missing = [s for s in planned if s not in set(written)]
    if not missing or not state_path(folder).is_file():
        return

    def _mutate(state: dict) -> bool:
        changed = False
        for slot in missing:
            current = _gen(state, slot)
            stamped = any(
                isinstance(slots, dict) and isinstance(slots.get(slot), dict)
                and slots[slot].get("gen") == current
                for slots in state["complete"].values()
            )
            if stamped:
                state["gen"][slot] = current + 1
                changed = True
        return changed

    _update_state(folder, _mutate)


# ---------------------------------------------------------------------------
# What is configured, what is owed
# ---------------------------------------------------------------------------


def secondary_write_models(svc: Any) -> "dict[str, str]":
    """``{slot: model}`` of the SECONDARY text slots ``svc``'s document
    fan-out writes (``EmbeddingService.configured_text_write_models`` — the
    ONE decision — minus the active slot)."""
    try:
        models = dict(svc.configured_text_write_models())
    except Exception:  # noqa: BLE001 — a degenerate double: nothing configured
        return {}
    models.pop(str(getattr(svc, "text_vector_slot", "") or ""), None)
    return {s: m for s, m in models.items() if s and m}


def secondary_models_for_env(env: Mapping[str, str]) -> "dict[str, str]":
    """``{slot: model}`` of the Ollama-served secondaries a project's
    projected ``env`` configures — for a scheduler that holds no service (the
    bundle update, the env projection, session start). The rule is the one
    the fan-out and the model pull plan share
    (:func:`vco_lib.embedding_pull_plan.kg_secondary_models`). The OpenAI
    secondary needs a key and the embeddings consent, which only a live
    service resolves; ``kg-sync --all`` (which has one) schedules it."""
    from vco_lib.embedding_pull_plan import kg_secondary_models
    from vco_lib.embedding_service import (
        _resolve_arctic_secondary,
        _resolve_write_all_slots,
        resolve_active_text_model_id,
    )
    from vco_lib.kg_embed_model import text_model_slot

    active = resolve_active_text_model_id(env)
    active_slot = text_model_slot(active)[0]
    out: "dict[str, str]" = {}
    for model in kg_secondary_models(active, write_all=_resolve_write_all_slots(env),
                                     arctic_secondary=_resolve_arctic_secondary(env)):
        slot = text_model_slot(model)[0]
        if slot != active_slot:
            out[slot] = model
    return out


def owed_pairs(folder: Any, collections: Iterable[str],
               secondaries: Mapping[str, str]) -> "list[tuple[str, str, str]]":
    """``(collection, slot, model)`` for every configured secondary not yet
    stamped complete on a collection. A file read — no Weaviate call."""
    state = read_state(folder)
    return [(c, slot, model)
            for c in dict.fromkeys(x for x in collections if x)
            for slot, model in secondaries.items()
            if not is_complete(state, c, slot, model)]


# ---------------------------------------------------------------------------
# The backfill (inside kg-sync --all)
# ---------------------------------------------------------------------------


def _probe(sib: Any) -> Optional[str]:
    """None when the secondary model answers one tiny embed, else the reason."""
    try:
        vecs = sib.embed_text_batch([_PROBE_TEXT])
    except Exception as exc:  # noqa: BLE001 — any failure is "down"
        return f"{type(exc).__name__}: {exc}"
    if not vecs or not vecs[0]:
        return "the model returned no vector (not pulled, or its backend is down)"
    return None


def _default_client():
    from vco_lib import weaviate_helpers as _wh

    return _wh.connect_v4()


def missing_rows(collection: str, slot: str, *,
                 client_factory: Optional[Callable[[], Any]] = None,
                 ) -> "Optional[list[tuple[str, str]]]":
    """``[(file_path, content)]`` of the rows of ``collection`` that hold
    text but no ``slot`` vector (a cursor scan); ``[]`` for a class that does
    not exist; None when Weaviate cannot be asked."""
    client = None
    try:
        client = (client_factory or _default_client)()
        if not client.collections.exists(collection):
            return []
        coll = client.collections.get(collection)
        out: "list[tuple[str, str]]" = []
        for obj in coll.iterator(include_vector=True,
                                 return_properties=["file_path", "content"]):
            vec = obj.vector if isinstance(obj.vector, dict) else {}
            if vec.get(slot):
                continue
            props = obj.properties if isinstance(obj.properties, dict) else {}
            content = str(props.get("content") or "")
            if content.strip():
                out.append((str(props.get("file_path") or ""), content))
        return out
    except Exception:  # noqa: BLE001 — unreachable → no verdict
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _refused(content: str, model: str) -> bool:
    """A row the enrichment leaves unfilled because the secondary may refuse
    it whole — only its writer can split it into rows."""
    from vco_lib.embedding_enrichment import EMBED_INPUT_CHAR_CAP
    from vco_lib.embedding_service import text_may_exceed_window

    return len(content) > EMBED_INPUT_CHAR_CAP or text_may_exceed_window(content, model)


def _count_line(collection: str, slot: str, model: str) -> str:
    from vco_lib.embed_slot_fill import object_count
    from vco_lib.kg_embed_model import is_openai_text_model

    try:
        n: Any = object_count(collection, None)
    except Exception:  # noqa: BLE001
        n = "?"
    cost = " through the OpenAI API (cost-bearing)" if is_openai_text_model(model) else ""
    return (f"[vct] kg-secondary: {collection}: backfilling the secondary {model} slot "
            f"({slot}) once — up to {n} object(s){cost}; only rows without it are "
            "embedded, every other vector is kept, searches keep working.")


def backfill_collection(
    collection: str, slot: str, model: str, svc: Any, folder: Any, *,
    log: Callable[[str], None] = print,
    enrich: Optional[Callable[..., Any]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
) -> str:
    """Fill ``slot`` (``model``'s) on ``collection``'s stored rows. Returns
    :data:`COMPLETE` (stamped), :data:`RELAY` (only refused rows remain; the
    walk re-lays them and :func:`after_walk` settles), :data:`INCOMPLETE`
    (rows still lack it — a transient failure; owed) or :data:`DOWN` (the
    model did not answer — nothing scanned or embedded; owed)."""
    from vco_lib.embed_slot_fill import ensure_slot
    from vco_lib.embedding_enrichment import CollectionNotFoundError
    from vco_lib.kg_embed_model import sibling_text_service, text_model_slot
    from vco_lib.weaviate_schema import KG_NAMED_VECTORS

    key = (collection, slot)
    gen0 = _gen(read_state(folder), slot)
    sib = sibling_text_service(svc, model)
    reason = _probe(sib)
    if reason is not None:
        _BLOCKED[key] = f"{model} did not answer ({reason})"
        log(f"[vct] kg-secondary: {collection}: the secondary {model} is not reachable "
            f"({reason}); nothing embedded, the backfill is retried later.")
        return DOWN
    err = ensure_slot([collection], slot, text_model_slot(model)[1], KG_NAMED_VECTORS,
                      model=model, add=True)
    if err:
        _BLOCKED[key] = err
        log(f"[vct] kg-secondary: cannot backfill {model} into {collection} yet ({err}).")
        return INCOMPLETE
    log(_count_line(collection, slot, model))
    if enrich is None:
        from vco_lib.embedding_enrichment import enrich_collection_vectors as enrich
    try:
        report = enrich(collection, slot, embedding_service=sib,
                        **({"weaviate_client_factory": client_factory} if client_factory else {}))
        log(f"[vct] kg-secondary: {collection}: {int(getattr(report, 'enriched', 0) or 0)} "
            f"row(s) got a {model} vector, {int(getattr(report, 'skipped', 0) or 0)} "
            "already had one.")
    except CollectionNotFoundError:
        pass  # nothing stored yet — the scan below answers []
    missing = missing_rows(collection, slot, client_factory=client_factory)
    if missing is None:
        _BLOCKED[key] = "Weaviate could not be scanned"
        return INCOMPLETE
    if not missing:
        stamp_complete(folder, collection, slot, model, gen0, _rows_hint(collection))
        _BLOCKED.pop(key, None)
        log(f"[vct] kg-secondary: {collection}: every row holds a {model} vector.")
        return COMPLETE
    refused = {fp for fp, content in missing if fp and _refused(content, model)}
    transient = [fp for fp, content in missing if not (fp and _refused(content, model))]
    if refused:
        _RELAY.setdefault(collection, set()).update(refused)
        log(f"[vct] kg-secondary: {collection}: {len(refused)} node(s) hold a row {model} "
            "refuses whole; this sync re-lays them for every model (finer rows, embed "
            "first, replace after).")
    if transient:
        _BLOCKED[key] = (f"{len(transient)} row(s) could not get a {model} vector "
                         "this run")
        log(f"[vct] kg-secondary: {collection}: {_BLOCKED[key]}; retried next run.")
        return INCOMPLETE
    _SETTLE[key] = (model, gen0)
    return RELAY


def _rows_hint(collection: str) -> int:
    from vco_lib.embed_slot_fill import object_count

    try:
        return int(object_count(collection, None))
    except Exception:  # noqa: BLE001
        return 0


def backfill_before_walk(collections: Iterable[str], svc: Any, folder: Any, *,
                         log: Callable[[str], None] = print,
                         enrich: Optional[Callable[..., Any]] = None,
                         client_factory: Optional[Callable[[], Any]] = None,
                         ) -> "dict[tuple[str, str], str]":
    """``kg-sync --all``'s pre-walk leg: every owed (collection, secondary)
    pair is backfilled; a stamped pair costs nothing. Never raises."""
    out: "dict[tuple[str, str], str]" = {}
    if folder is None:
        return out
    secondaries = secondary_write_models(svc)
    forget_disabled(folder, secondaries)
    down: "dict[str, str]" = {}
    for coll, slot, model in owed_pairs(folder, collections, secondaries):
        if model in down:
            # One probe per model per run: a down backend is not re-asked per
            # collection, and nothing is scanned.
            _BLOCKED[(coll, slot)] = down[model]
            out[(coll, slot)] = DOWN
            continue
        try:
            out[(coll, slot)] = backfill_collection(
                coll, slot, model, svc, folder, log=log, enrich=enrich,
                client_factory=client_factory)
        except Exception as exc:  # noqa: BLE001 — loud, never aborts the sync
            _BLOCKED[(coll, slot)] = f"{type(exc).__name__}: {exc}"
            log(f"❌ kg-secondary: backfilling {model} into {coll} failed: {exc} — "
                "retried later.")
            out[(coll, slot)] = INCOMPLETE
        if out[(coll, slot)] == DOWN:
            down[model] = _BLOCKED.get((coll, slot), f"{model} did not answer")
    return out


def relay_reason(collection: str, rows: Iterable[Any]) -> Optional[str]:
    """kg-sync's re-embed trigger: why a content-current node must be re-laid
    this run (one of its rows is a row the backfill's secondary refused
    whole), or None. Fires once per node per run."""
    pending = _RELAY.get(collection)
    if not pending:
        return None
    for obj in rows or ():
        fp = str(((getattr(obj, "properties", None) or {}).get("file_path")) or "")
        if fp and fp in pending:
            pending.discard(fp)
            return ("a stored row is refused whole by the newly enabled secondary "
                    "embedding model (re-laid once into rows every model accepts)")
    return None


def _settle_after_relay(folder: Any, *, client_factory: Optional[Callable[[], Any]] = None,
                        log: Callable[[str], None] = print) -> None:
    for (coll, slot), (model, gen0) in list(_SETTLE.items()):
        missing = missing_rows(coll, slot, client_factory=client_factory)
        if missing == []:
            stamp_complete(folder, coll, slot, model, gen0, _rows_hint(coll))
            _BLOCKED.pop((coll, slot), None)
            log(f"[vct] kg-secondary: {coll}: every row holds a {model} vector "
                "(refused rows re-laid).")
        elif missing:
            _BLOCKED[(coll, slot)] = (f"{len(missing)} row(s) still lack a {model} "
                                      "vector after the re-lay")
        _SETTLE.pop((coll, slot), None)
    _RELAY.clear()


def _emit_deferral(folder: Path, owed: "list[tuple[str, str, str]]") -> None:
    try:
        from vco_lib.deferral_emit import DeferralEntry, emit

        lines = []
        for coll, slot, model in owed:
            why = _BLOCKED.get((coll, slot))
            lines.append(f"- {coll}: {model} ({slot})" + (f" — {why}" if why else ""))
        emit(Path(folder), DeferralEntry(
            condition_id=DEFERRAL_CID,
            title=f"Secondary embedding backfill owed on {len(owed)} collection slot(s)",
            detected=("A secondary text-embedding slot is enabled, and these stored KG "
                      "rows do not all hold its vector yet:\n" + "\n".join(lines)),
            why_deferred=("The backfill runs once, in the background, inside the "
                          "whole-tree sync (`kg-sync --all`), which VCO's retry driver "
                          "starts by itself; a secondary that did not answer is retried "
                          "then. Searches keep working meanwhile; only rows without the "
                          "vector are embedded and no existing vector is dropped."),
            command_to_apply="# Run the whole-tree sync now (only missing vectors are embedded):\n"
                             ".claude/scripts/kg-sync --all",
            severity="info",
        ))
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (kg-secondary deferral emit failed: {exc})", file=sys.stderr)


def _deferral_present(folder: Path) -> bool:
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(folder))
        return bool(report) and report.has_condition(DEFERRAL_CID)
    except Exception:  # noqa: BLE001 — unreadable ledger ⇒ treat as absent
        return False


def sync_deferral(folder: Any, owed: "list[tuple[str, str, str]]", *, refresh: bool,
                  resolve: bool = True) -> None:
    """Keep ``kg_secondary_backfill_pending`` truthful: emitted while a pair
    is owed (refreshed on ``refresh`` — a whole-tree run — else written once),
    resolved when nothing is (``resolve``: only a caller that knows EVERY
    configured secondary — one holding a live service). The stamps are the
    proof either way."""
    folder = Path(folder)
    if owed:
        if refresh or not _deferral_present(folder):
            _emit_deferral(folder, owed)
        return
    if resolve and _deferral_present(folder):
        try:
            from vco_lib.deferral_emit import resolve_conditions

            resolve_conditions(folder, (DEFERRAL_CID,))
        except Exception as exc:  # noqa: BLE001
            print(f"   (kg-secondary deferral clear failed: {exc})", file=sys.stderr)


def after_walk(collections: Iterable[str], folder: Any, svc: Any, *, full_tree: bool,
               client_factory: Optional[Callable[[], Any]] = None,
               log: Callable[[str], None] = print) -> None:
    """``kg-sync``'s post-walk leg. A whole-tree run settles the pairs whose
    refused rows the walk re-laid; every run (a scoped one too — a file read)
    keeps the ledger row truthful. Never raises."""
    if folder is None:
        return
    try:
        secondaries = secondary_write_models(svc)
        forget_disabled(folder, secondaries)
        if full_tree:
            _settle_after_relay(folder, client_factory=client_factory, log=log)
        sync_deferral(folder, owed_pairs(folder, collections, secondaries),
                      refresh=full_tree)
    except Exception as exc:  # noqa: BLE001
        print(f"   (kg-secondary settle skipped: {exc})", file=sys.stderr)


def schedule_for_folder(folder: Any, collections: Optional[Iterable[str]] = None,
                        env: Optional[Mapping[str, str]] = None) -> "list[tuple[str, str, str]]":
    """The service-free scheduler (bundle update, env projection, session
    start): read the folder's projected env, drop stamps of switched-off
    secondaries, and emit / resolve the ledger row. No Weaviate call, no
    embedding. Returns what is owed. Never raises."""
    try:
        if env is None:
            from vco_lib.kg_embed_model import _project_env

            env = _project_env(Path(folder))
        if collections is None:
            from vco_lib.kg_embed_model import folder_collections

            collections, _target = folder_collections(folder)
        names = list(collections or [])
        if not names:
            return []
        secondaries = secondary_models_for_env(env)
        # The OpenAI secondary is not decidable without a service (key +
        # consent): its stamps are kept, and nothing is resolved here — the
        # next kg-sync (which knows every configured secondary) resolves.
        forget_disabled(folder, [*secondaries, *_SERVICE_ONLY_SLOTS])
        owed = owed_pairs(folder, names, secondaries)
        sync_deferral(folder, owed, refresh=False, resolve=False)
        return owed
    except Exception as exc:  # noqa: BLE001
        print(f"   [vct] kg-secondary: scheduling skipped for {folder} ({exc})",
              file=sys.stderr)
        return []


def reset_run_state() -> None:
    """Forget this process's per-run state (tests; a long-lived caller)."""
    _RELAY.clear()
    _SETTLE.clear()
    _BLOCKED.clear()
    _OBSERVED_OFF.clear()
