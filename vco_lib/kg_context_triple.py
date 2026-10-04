# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What context the KG collection was last embedded against — ONE home (v0.2.101).

The "context triple" is three ``launcher.db`` ``app_state`` rows::

    last_installed_active_embedding   the embedding PROFILE (qwen3 / arctic / …)
    last_installed_kg_collection      the project's KG class
    last_installed_shared_kg_collection   the shared class it also feeds

install.py reads them to decide whether an update may take the cheap content-hash
diff or must re-walk (and possibly re-embed) the whole tree, so a value that is
WRONG is expensive in one of two ways: advanced while the work was undone ⇒ the
collection keeps previous-model vectors forever, unrecorded; withheld while the
work was done ⇒ every later update pays a full walk.

WHO WRITES IT (v0.2.101 — the caller audit's Gap 4/5/6):
the run that did the embedding records it, and there is exactly one such rule:

  * **a whole-tree run of the ORCHESTRATOR ROOT that finished with ZERO
    failures** — ``sync_knowledge_graph.py --all`` records it at the end of its
    own success path (``_record_context_triple``). That covers EVERY entry point
    that seeds the root's tree, because they all run that script: install's
    foreground seed, the detached driver the install spawns, the SESSION-START
    driver (no carried context needed — the child resolves its own), the
    launcher's Sync button, ``project_init migrate-collections``, and a hand-run
    ``kg-sync --all``. The ROOT scope is load-bearing (SF-1): the row is
    machine-global and install.py's comparison is the root's, so a registered
    project's own clean ``--all`` must leave it alone.
  * **install.py's partial (file-list) run keeps SEG-1's rule** — it records when
    the child RAN, because install.py (not the child) is the party that knows the
    stored context already equalled the current one; a file-list run cannot speak
    for the nodes it did not visit, so the child never claims it.
  * **install.py's empty-diff skip records it** — no child ran at all there; the
    hashes all match and the WP-6 enrichment has just filled the slots, which is
    install.py's own evidence.

``last_kg_sync_at`` rides along in :func:`record` on every one of those paths, so
the "last sync at" telemetry no longer depends on which transport ran the seed.
(``last_kg_sync_stats`` is deliberately NOT written here: only install.py sees the
node counts, and inventing zeros would be a worse record than a lag.)

The enumeration above is about RUN-based writers. Two more sites legitimately
write SUBSETS of these rows, and neither is a seed claim — both record an
IDENTITY decision made outside any seed: ``install.py``'s v0.2.44
orchestrator-root rebind (``last_installed_kg_collection`` +
``last_installed_shared_kg_collection`` := the canonical class) and
``vco_lib.kg_binding_heal``'s heal write of the canonical shared pick. They
pre-date this cycle and stay as they are.

The chain that answers "which profile is this process embedding with" lives here
too (:func:`active_embedding_profile`) so the child, install.py and the retry
handler cannot disagree about it.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Callable, Mapping, Optional

#: ``app_state`` keys. ``install.py``'s own ``_APP_STATE_KEY_*`` constants MUST
#: MATCH these (parity-pinned by ``tests/test_v02101_seed_and_data_keys.py``) —
#: they name the same rows, and install.py still writes the skip-path one.
APP_STATE_KEY_LAST_ACTIVE_EMBEDDING = "last_installed_active_embedding"
APP_STATE_KEY_LAST_KG_COLLECTION = "last_installed_kg_collection"
APP_STATE_KEY_LAST_SHARED_KG_COLLECTION = "last_installed_shared_kg_collection"
APP_STATE_KEY_LAST_KG_SYNC_AT = "last_kg_sync_at"

#: Set in a SHARED-seed child's env by the two spawners of one
#: (``install_weaviate.kg_seed_step(shared_target=…)`` and
#: ``deferral_retry.retry_kg_seed_shared``). A shared-targeted run must never
#: record the PER-PROJECT context triple — deciding that by comparing
#: ``COLLECTION_NAME == SHARED_COLLECTION_NAME`` would be wrong on the
#: orchestrator root, where the two names are equal BY DESIGN and the root's own
#: normal seed must still record. An explicit marker has no such coincidence.
SHARED_SEED_ENV = "VCT_KG_SHARED_SEED"

#: The profile recorded when nothing states one (a free-tier install with no
#: launcher and no env override embeds with qwen3 — the same default
#: ``EmbeddingService`` falls back to).
DEFAULT_ACTIVE_EMBEDDING = "qwen3"


def _resolve_profile(
    env: Mapping[str, str], db_path: "Optional[Path]" = None
) -> Optional[str]:
    """The shared chain; ``None`` when NO leg resolved. PURE except the DB reads.

    One implementation for the two public entry points below, so the variant
    that keeps a ``None`` sentinel cannot drift from the one that does not.
    """
    value = (env.get("ACTIVE_EMBEDDING") or "").strip().lower()
    if value:
        return value
    try:
        from vco_lib.launcher_db_reader import (
            profile_for_text_model,
            read_app_state_active_embedding,
            read_app_state_default_text_embedding,
        )

        stored = read_app_state_active_embedding(db_path)
        if stored:
            return str(stored).strip().lower()
        derived = profile_for_text_model(read_app_state_default_text_embedding(db_path))
        if derived:
            return str(derived).strip().lower()
    except Exception:  # noqa: BLE001 — an unreadable DB ⇒ "cannot tell"
        pass
    return None


def active_embedding_profile(
    env: Optional[Mapping[str, str]] = None, *, db_path: "Optional[Path]" = None
) -> str:
    """The embedding PROFILE this process embeds with. Never empty.

    Precedence — the chain ``EmbeddingService`` delegates to, kept here so the
    writers of the triple cannot disagree with the embedder:

      1. ``ACTIVE_EMBEDDING`` env (an explicit override);
      2. ``launcher.db app_state[embedding.active_profile]`` (what the
         launcher's Identity tab / install's preset chooser wrote);
      3. ``launcher.db app_state[default_text_embedding]`` mapped to its
         profile — the hardware-pick derive (v0.2.71 T-B-emb). Carried here so
         this home is a true SUPERSET of the private copy that lived in
         ``EmbeddingService._resolve_active_embedding``: without it, an install
         whose ONLY row is the hardware pick would resolve ``arctic`` in the
         embedder but ``qwen3`` here — the record and the work would disagree;
      4. ``"qwen3"`` — the default every other tier falls back to.

    *db_path* is install.py's seam (it threads its own ``_discover_app_state_db_path``,
    which many tests patch); ``None`` means the canonical resolution.
    """
    source = os.environ if env is None else env
    return _resolve_profile(source, db_path) or DEFAULT_ACTIVE_EMBEDDING


def active_embedding_profile_or_none(
    env: Optional[Mapping[str, str]] = None, *, db_path: "Optional[Path]" = None
) -> Optional[str]:
    """The same chain, but ``None`` when NO leg resolved.

    install.py needs the sentinel: it distinguishes "nothing is configured, use
    the qwen3 default" from "qwen3 was explicitly chosen", and it must not turn
    a hardware-pick-only box into a different value than the embedder uses —
    which is exactly why the chain lives HERE and this variant exists rather
    than a second copy in install.py.
    """
    source = os.environ if env is None else env
    return _resolve_profile(source, db_path)


def certified_from_run(
    *,
    whole_tree: bool,
    failures: int,
    kg_collection_resolved: bool,
    orchestrator_root: bool,
    shared_targeted: bool,
) -> bool:
    """May the run that just finished record the triple? PURE.

    ``whole_tree`` — the run was handed ``--all``. A file-list run judged only
    the files it was given; the nodes it did not visit may still hold another
    model's vectors, so it must not claim the collection.

    ``failures`` — per-node write failures. A non-zero count cannot distinguish
    "one node failed" from "died early, most of the tree never visited" (the
    WP-4/SEG-1 argument), and only the second shape is unrecoverable: it leaves
    rows whose ``content_hash`` still matches, so no later diff can see them.
    Withholding costs at most one further zero-embed walk.

    ``kg_collection_resolved`` — the run targeted the project's CONFIGURED
    class, not the script's literal ``KnowledgeGraph`` fallback (the v0.2.95
    system-gate MINOR). Recording a fallback class would retire the real one.

    ``orchestrator_root`` — the tree walked IS the orchestrator install
    (``vco_lib.orchestrator_identity.is_orchestrator_clone``). THE row is
    machine-global and install.py compares it for the ROOT's seed, so only a run
    that seeded the root's own KG may write it: a registered project's clean
    ``--all`` (launcher Sync, session-start repair, a hand-run) previously
    stamped its OWN class here, and the next root update read that as a context
    change and paid a full walk (v0.2.101 SF-1). The test is the TREE, not the
    collection name — on the root ``KG_COLLECTION == SHARED_KG_COLLECTION`` by
    design, so a name test cannot tell the root's own seed from a shared one.

    ``shared_targeted`` — the run was pointed at the SHARED class instead
    (:data:`SHARED_SEED_ENV`). That pass is about the shared collection, so it
    is not a statement about the per-project context at all.
    """
    return (
        bool(whole_tree)
        and int(failures) == 0
        and bool(kg_collection_resolved)
        and bool(orchestrator_root)
        and not bool(shared_targeted)
    )


def record(
    active_embedding: str,
    kg_collection: str,
    shared_kg_collection: str,
    *,
    write_key: Optional[Callable[[str, str], None]] = None,
    now: Optional[str] = None,
) -> bool:
    """Write the triple + the attempt record. Soft-fail. Returns True on write.

    *write_key* is injectable for tests; the default writes ``launcher.db``'s
    ``app_state`` through the ONE writer and path oracle. A missing/unwritable
    DB soft-fails: the triple then simply stays as it was, which costs one more
    walk, never a silent skip.
    """
    if write_key is None:
        def _default_write(key: str, value: str) -> None:
            from vco_lib.launcher_db_writer import write_app_state_key
            from vco_lib.paths import launcher_db_path

            write_app_state_key(launcher_db_path(), key, value)

        write_key = _default_write
    stamp = now or datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        write_key(APP_STATE_KEY_LAST_ACTIVE_EMBEDDING, active_embedding)
        write_key(APP_STATE_KEY_LAST_KG_COLLECTION, kg_collection)
        write_key(APP_STATE_KEY_LAST_SHARED_KG_COLLECTION, shared_kg_collection)
        write_key(APP_STATE_KEY_LAST_KG_SYNC_AT, stamp)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the caller
        return False
    return True


def record_from_run(
    *,
    whole_tree: bool,
    failures: int,
    kg_collection_resolved: bool,
    orchestrator_root: bool,
    shared_targeted: bool,
    kg_collection: str,
    shared_kg_collection: str,
    active_embedding: Optional[str] = None,
    write_key: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """The ONE gate every whole-tree seeding entry point calls.

    Pairs :func:`certified_from_run` with :func:`record`, so a caller cannot
    record on evidence the rule refuses (or refuse on evidence it accepts).
    """
    if not certified_from_run(
        whole_tree=whole_tree, failures=failures,
        kg_collection_resolved=kg_collection_resolved,
        orchestrator_root=orchestrator_root, shared_targeted=shared_targeted,
    ):
        return False
    return record(
        active_embedding or active_embedding_profile(),
        kg_collection, shared_kg_collection, write_key=write_key,
    )


__all__ = [
    "SHARED_SEED_ENV",
    "APP_STATE_KEY_LAST_ACTIVE_EMBEDDING",
    "APP_STATE_KEY_LAST_KG_COLLECTION",
    "APP_STATE_KEY_LAST_KG_SYNC_AT",
    "APP_STATE_KEY_LAST_SHARED_KG_COLLECTION",
    "DEFAULT_ACTIVE_EMBEDDING",
    "active_embedding_profile",
    "active_embedding_profile_or_none",
    "certified_from_run",
    "record",
    "record_from_run",
]
