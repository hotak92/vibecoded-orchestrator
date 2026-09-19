# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which schema-migration plan entries may be applied without asking the user.

v0.2.95 **WP-9** (surface-map duplicate **D4**): this policy existed in two
languages. ``vco_lib.project_init`` decided the LOSSY half (which plan entries
force a ``schema_migration_required`` deferral) and
``projects_v2.rs::should_auto_apply_additive`` re-derived BOTH halves in Rust
to decide whether to issue the wet apply. Two implementations, one question,
and the expensive direction of a disagreement is not subtle: an action wrongly
classified additive is auto-applied without consent, and ``rebuild`` means
dropping a collection and re-embedding every object in it.

So the classification lives here, once, and the launcher READS the answer off
the ``migrate-collections --json`` envelope instead of recomputing it. That is
rung **(A)** of the CLAUDE.md cross-language ladder — one implementation, the
other language reaching it over a process boundary that already exists (the
launcher shells out to ``python -m vco_lib.project_init migrate-collections``
either way, so the classification costs no extra subprocess).

WHY THESE THREE SETS
--------------------
``migrate_collections`` picks one action per collection
(``project_init._classify_action``); this module says what each action MEANS
for consent:

* :data:`ADDITIVE_ACTIONS` — ``copy`` and ``patch_props``. Lossless, and
  provably so: ``copy`` round-trips every existing UUID, named vector and
  property byte-for-byte through staging (``_copy_collection_with_vectors``)
  and never drops the live collection until the swap's count-match assertion
  passes; ``patch_props`` POSTs new properties onto the live class. Neither
  re-embeds. That proof — not convenience — is the whole justification for
  applying them unattended.
* :data:`LOSSY_ACTIONS` — ``rebuild``. Drop + re-embed. Consent-gated, always;
  it is what ``schema_migration_required`` exists to defer.
* Everything else (``noop``, ``create``, anything a future
  ``_classify_action`` adds) is NEITHER. It cannot make a plan auto-appliable
  and it cannot force a deferral. A new action therefore lands as "do nothing
  automatically", which is the only safe default for an action whose data
  semantics nobody has written down yet.

The asymmetry is deliberate: ``auto_apply_additive`` requires POSITIVE
evidence of an additive action (an all-``noop`` plan has nothing to apply, so
it is ``False`` rather than a vacuous ``True``), while a single lossy entry
vetoes the whole plan. A mixed ``copy`` + ``rebuild`` plan is NOT split: the
rebuild defers, and the additive subset waits for the same consent rather than
being applied behind it.

WHAT THIS MODULE DOES NOT DECIDE
--------------------------------
Whether the PROBE itself was trustworthy is only half-knowable here.
:func:`classify_migration_plan` folds in ``errors[]`` (which it can see) and
the caller ANDs its own evidence that the process exited cleanly (which the
plan JSON cannot self-report — a run that dies after printing, or exits 2 on
argparse, is not a clean probe). Keeping those two separate is why
``should_auto_apply_additive`` still takes the exit status.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

__all__ = [
    "ADDITIVE_ACTIONS",
    "LOSSY_ACTIONS",
    "AUTO_APPLY_KEY",
    "ADDITIVE_COLLECTIONS_KEY",
    "additive_collections",
    "classify_migration_plan",
    "lossy_plan_entries",
]

#: Lossless actions — safe to apply unattended (see the module docstring).
ADDITIVE_ACTIONS = frozenset({"copy", "patch_props"})

#: Data-destroying actions — always consent-gated via `schema_migration_required`.
LOSSY_ACTIONS = frozenset({"rebuild"})

#: The wire keys `migrate-collections --json` publishes and the launcher reads.
#: Pinned cross-language by `tests/test_v0295_migration_plan_classify.py`.
AUTO_APPLY_KEY = "auto_apply_additive"
ADDITIVE_COLLECTIONS_KEY = "additive_collections"


def _plan_entries(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The plan rows, defensively — a malformed envelope classifies as empty.

    A caller that hands us something shaped wrong gets "nothing is
    auto-appliable", never an exception: this runs inside a soft-fail update
    path, and the conservative answer is the one that does not mutate.
    """
    plan = result.get("plan")
    if not isinstance(plan, (list, tuple)):
        return []
    return [e for e in plan if isinstance(e, dict)]


def lossy_plan_entries(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The plan entries that require explicit user consent.

    This is the list `_cmd_migrate_collections` hands to
    `_emit_migrate_required_deferral`, so the deferral and the auto-apply
    decision are driven by ONE reading of the plan.
    """
    return [
        e for e in _plan_entries(result)
        if e.get("action") in LOSSY_ACTIONS
    ]


def additive_collections(result: Mapping[str, Any]) -> List[str]:
    """The collections this plan's ADDITIVE actions apply to.

    A pure function of the plan, deliberately: it reproduces exactly what
    `projects_v2.rs` used to compute for its post-apply toast (filter the plan
    by additive action, take the collection names), so moving the vocabulary
    here changes no rendering. In a WET run the plan describes what was done,
    which is why the launcher can name these collections as migrated.
    """
    return [
        str(e.get("collection"))
        for e in _plan_entries(result)
        if e.get("action") in ADDITIVE_ACTIONS and e.get("collection")
    ]


def classify_migration_plan(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify a `migrate_collections` result dict.

    Returns a dict with:

    ``has_additive``
        at least one `copy` / `patch_props` entry — something to apply.
    ``has_lossy``
        at least one `rebuild` entry — consent required.
    ``errors_empty``
        the probe reported no `errors[]`.
    ``auto_apply_additive``
        the conjunction: additive work exists, nothing lossy vetoes it, and
        the probe was clean. The caller ANDs its own "the process exited 0"
        evidence on top (see the module docstring).
    ``lossy_entries``
        the entries behind `has_lossy`, for the deferral text.
    """
    entries = _plan_entries(result)
    actions = [e.get("action") for e in entries]
    has_additive = any(a in ADDITIVE_ACTIONS for a in actions)
    has_lossy = any(a in LOSSY_ACTIONS for a in actions)
    errors = result.get("errors")
    errors_empty = not errors
    return {
        "has_additive": has_additive,
        "has_lossy": has_lossy,
        "errors_empty": errors_empty,
        "auto_apply_additive": has_additive and not has_lossy and errors_empty,
        "lossy_entries": [e for e in entries if e.get("action") in LOSSY_ACTIONS],
    }
