# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""May this run DROP the ``<prefix>_Code*`` family? — the ONE decision.

Why this module exists (v0.2.92 BLOCKER-2)
------------------------------------------

``analyze_code_graph.py --force-recreate`` calls
``CodeGraphAnalyzer.create_collections(force=True)``, which ``collections
.delete()``s all five ``<prefix>_Code*`` classes before recreating them. The
``<prefix>`` it deletes comes from the analyzer's resolution ladder
(``--from-resolver > --project > $CODE_GRAPH_PROJECT > repo_path.name``), whose
LAST rung is the folder **basename** — and the wrapper
``templates/scripts/code-graph-analyze`` is a pure ``"$@"`` pass-through, so a
human terminal that never sourced ``.claude/env`` lands on that rung.

Until v0.2.92 the delete had no guard at all. Two failures followed from the
same root:

* a project whose basename differs from its bound ``project_codegraph_bindings
  .collection_prefix`` (moved folder, renamed in the GUI, display name that
  simply differs) rebuilt a NEW family under the basename while its real one
  kept the stale vectors — the "correctness fix" landed nowhere;
* if that basename-derived family happened to BE another registered project's
  family (fork / clone / two registrations sharing a folder name), that
  project's five classes were dropped and overwritten with this repository's
  entities. The delete itself is irreversible in Weaviate; the DATA is
  regenerable (the code graph is derived entirely from a source walk), so the
  victim loses a full re-embed of its tree — provided it still has one, and
  provided anyone notices, since the overwritten family looks structurally
  healthy afterwards.

This is the same basename-identity + unguarded-drop class that
``vco_lib.project_identity`` was written for in W8 and that
``project_init._legacy_codegraph_drop_revalidated`` closed on the *emitted*
legacy-cleanup command. This module closes it on the analyzer's own drop, and
it composes ``project_identity`` rather than re-deriving any identity rule.

Where the guard runs
--------------------

At the CHOKEPOINT: ``create_collections(force=True)``. Every force path — the
deferral remedy pasted into a terminal, ``vco_lib.schema_regenerate
._regenerate_codegraph``, the launcher's embedding-profile-change rebuild —
funnels through that one method, so one call site covers all of them. A guard
wired into some-but-not-all callers is the same defect one layer down.

The decision (:func:`decide`) is PURE — evidence in, verdict out — so both
arms (the refusal AND the legitimate rebuild that must keep working) are unit
testable without a launcher.db or a Weaviate.

Positive evidence only, and the three-way registry state
--------------------------------------------------------

``launcher.db`` is the ground truth, and "I could not read it" is NOT
permission to delete. But it is also not the same state as "there is no
launcher on this machine", and collapsing the two would refuse every
free-tier / standalone rebuild — a guard that refuses everything is not a fix.
So the registry is read three ways:

* **absent** (no ``launcher.db`` file at the resolved path) — positively
  observed: no project is registered, so no registered project's family can be
  the target. ALLOW.
* **present but unreadable** (``IdentitySnapshot.resolvable is False``: not a
  database, no ``projects`` table, corrupt page, a binding read that failed
  part-way) — cannot confirm. REFUSE.
* **readable** — decide from the rows.

Class-name equality
-------------------

The comparisons below use ``casefold()``, NOT
``project_identity.normalise_for_match``. Deliberate, and load-bearing in both
directions. Weaviate class names collide case-INsensitively, so ``casefold``
equality is exactly "these two names denote the same class". ``normalise_for_
match`` additionally strips underscores, which is right for a keep-set (where
over-matching only ever protects more) and wrong here: it would make
``ACME_widget`` and ``ACMEWidget`` — two families that genuinely coexist in
Weaviate — compare equal, which both refuses a legitimate rebuild of one and
authorises dropping the other under the "that's my own family" arm.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "CodeGraphDropRefused",
    "DropVerdict",
    "REASON_NO_REGISTRY",
    "REASON_OWN_FAMILY",
    "REASON_UNOWNED_FAMILY",
    "REASON_NO_REPO_PATH",
    "REASON_REGISTRY_UNREADABLE",
    "REASON_BOUND_TO_OTHER_PROJECT",
    "REASON_REGISTERED_PREFIX_MISMATCH",
    "decide",
    "enforce",
    "evaluate",
    "registry_present",
]


# ── allow reasons ──────────────────────────────────────────────────────────
#: No ``launcher.db`` on this machine — nothing is registered, nothing to hit.
REASON_NO_REGISTRY = "no_registry"
#: The target family is this folder's own bound family.
REASON_OWN_FAMILY = "own_family"
#: The registry was read and no project binds the target family.
REASON_UNOWNED_FAMILY = "unowned_family"

# ── refusal reasons ────────────────────────────────────────────────────────
#: The caller asked to force-drop without naming the folder being analyzed.
REASON_NO_REPO_PATH = "no_repo_path"
#: ``launcher.db`` exists but could not be READ (≠ "holds no projects").
REASON_REGISTRY_UNREADABLE = "registry_unreadable"
#: The target family is ANOTHER registered project's bound family.
REASON_BOUND_TO_OTHER_PROJECT = "bound_to_other_project"
#: This folder IS registered and its bound prefix is not the target one.
REASON_REGISTERED_PREFIX_MISMATCH = "registered_prefix_mismatch"


@dataclass(frozen=True)
class DropVerdict:
    """Outcome of the force-drop decision.

    ``allowed`` is the only thing a caller must branch on; ``reason`` is the
    stable machine token (tests and logs key on it) and ``message`` is the
    operator-facing text, which on a refusal always names the three values
    that disagree plus the exact command that resolves it.
    """

    allowed: bool
    reason: str
    message: str = ""
    resolved_prefix: str = ""
    bound_prefix: Optional[str] = None
    owner_name: Optional[str] = None


class CodeGraphDropRefused(RuntimeError):
    """The guard refused a ``--force-recreate`` drop.

    Carries the :class:`DropVerdict` so the analyzer's ``main()`` can print the
    operator message verbatim and exit with its distinct code. Raised BEFORE
    any ``collections.delete()``, so nothing has been deleted by the time a
    caller sees it. Lives here rather than in the analyzer because it is part
    of THIS module's contract — :func:`enforce` is the only thing that raises
    it — and because the analyzer is under a line ratchet.
    """

    def __init__(self, verdict: DropVerdict) -> None:
        super().__init__(verdict.message or "code-graph drop refused")
        self.verdict = verdict


def _same_class(a: Optional[str], b: Optional[str]) -> bool:
    """Do ``a`` and ``b`` name the SAME Weaviate class/prefix?

    See the module docstring for why this is ``casefold`` equality and not
    ``project_identity.normalise_for_match``.
    """
    if not a or not b:
        return False
    return a.strip().casefold() == b.strip().casefold()


def _remedy(bound_prefix: Optional[str]) -> str:
    """The one-line fix printed under a refusal."""
    if bound_prefix:
        return (
            f"Re-run with the project's own identity:\n"
            f"      --from-resolver   (asks vct-hub; what the hooks and the "
            f"launcher use)\n"
            f"  or  --project {bound_prefix!r}   (the bound prefix, verbatim)"
        )
    return (
        "Re-run with the project's own identity: --from-resolver (asks "
        "vct-hub; what the hooks and the launcher use), or --project "
        "<CanonicalProject>."
    )


def decide(
    repo_path: Optional[Path],
    resolved_prefix: str,
    *,
    registry_exists: bool,
    snapshot,
) -> DropVerdict:
    """PURE decision: may ``resolved_prefix``'s five classes be dropped?

    Args:
        repo_path: the folder being analyzed. ``None`` → refuse: a caller that
            will not say WHICH project it is cannot be checked, and an
            uncheckable destructive call is not one we perform.
        resolved_prefix: the sanitized class prefix the analyzer is about to
            delete + recreate (``<prefix>_CodeModule`` and its four siblings).
        registry_exists: does a ``launcher.db`` file exist at the resolved
            path? Distinguishes "no launcher on this machine" (allow) from
            "launcher.db is there but unreadable" (refuse) — see the module
            docstring.
        snapshot: a :class:`vco_lib.project_identity.IdentitySnapshot`.

    Returns:
        A :class:`DropVerdict`. Never raises.
    """
    prefix = (resolved_prefix or "").strip()

    if repo_path is None:
        return DropVerdict(
            allowed=False,
            reason=REASON_NO_REPO_PATH,
            resolved_prefix=prefix,
            message=(
                "Refusing --force-recreate: the drop guard was not told which "
                "folder is being analyzed, so it cannot confirm that "
                f"'{prefix}_Code*' belongs to this project. This is an "
                "internal wiring error — report it."
            ),
        )

    if not registry_exists:
        # Positively observed absence: no launcher registry on this machine,
        # so no registered project can own the target family. This is the
        # free-tier / standalone-CLI case and it must keep working.
        return DropVerdict(
            allowed=True, reason=REASON_NO_REGISTRY, resolved_prefix=prefix,
        )

    if snapshot is None or not getattr(snapshot, "resolvable", False):
        return DropVerdict(
            allowed=False,
            reason=REASON_REGISTRY_UNREADABLE,
            resolved_prefix=prefix,
            message=(
                f"Refusing --force-recreate of '{prefix}_Code*': the launcher "
                "registry exists but could not be READ, so this run cannot "
                "confirm the family belongs to this project rather than to "
                "another one. Dropping five populated classes on a guess is "
                "not recoverable, so nothing was deleted.\n"
                "  Fix: make the registry readable (is the launcher mid-write? "
                "is VCT_LAUNCHER_DB_PATH pointing at the right file?), or pass "
                "--project explicitly once you have confirmed the target."
            ),
        )

    projects = tuple(getattr(snapshot, "projects", ()) or ())

    me = None
    try:
        me = snapshot.identity_for_folder(repo_path)
    except Exception:  # noqa: BLE001 — an evidence source may never authorise
        return DropVerdict(
            allowed=False,
            reason=REASON_REGISTRY_UNREADABLE,
            resolved_prefix=prefix,
            message=(
                f"Refusing --force-recreate of '{prefix}_Code*': the registry "
                "could not be matched against this folder. Nothing was deleted."
            ),
        )

    my_bound = None
    if me is not None:
        my_bound = me.authoritative_codegraph_prefix()

    # 1. Does ANOTHER registered project bind this exact family? Only a real
    #    binding row counts as ownership — a name-DERIVED guess is not evidence
    #    that anybody's data lives there, and treating it as such would refuse
    #    an unregistered clone its own legitimate rebuild.
    for proj in projects:
        bound = None
        try:
            bound = proj.authoritative_codegraph_prefix()
        except Exception:  # noqa: BLE001
            bound = None
        if not _same_class(bound, prefix):
            continue
        if me is not None and proj.project_id == me.project_id:
            continue  # that's us — handled by the own-family arm below
        return DropVerdict(
            allowed=False,
            reason=REASON_BOUND_TO_OTHER_PROJECT,
            resolved_prefix=prefix,
            bound_prefix=my_bound,
            owner_name=proj.name or None,
            message=(
                f"Refusing --force-recreate: the five '{prefix}_Code*' classes "
                f"are the bound code graph of a DIFFERENT registered project, "
                f"{proj.name!r} ({proj.folder_path or 'folder unknown'}). "
                "Dropping them would delete that project's embeddings and "
                "overwrite them with this repository's entities. Nothing was "
                "deleted.\n"
                f"  This folder:  {repo_path}\n"
                f"  Prefix asked: {prefix}   (from the folder name unless you "
                "passed --project / --from-resolver)\n"
                f"  Bound to:     {proj.name!r}\n"
                f"  {_remedy(my_bound)}"
            ),
        )

    # 2. This folder IS registered and HAS a binding of its own, but the run
    #    resolved a different family. Rebuilding it would leave the project's
    #    real vectors stale while minting/overwriting a family that is not its
    #    own — the "common" shape of the defect.
    if me is not None and my_bound and not _same_class(my_bound, prefix):
        return DropVerdict(
            allowed=False,
            reason=REASON_REGISTERED_PREFIX_MISMATCH,
            resolved_prefix=prefix,
            bound_prefix=my_bound,
            owner_name=me.name or None,
            message=(
                f"Refusing --force-recreate: this folder is registered as "
                f"{me.name!r}, whose code graph is bound to "
                f"'{my_bound}_Code*' — but this run resolved "
                f"'{prefix}_Code*'. Dropping and rebuilding that family would "
                "leave the project's real code graph untouched and stale, and "
                f"'{prefix}_Code*' is not this project's to delete. Nothing "
                "was deleted.\n"
                f"  This folder:  {repo_path}\n"
                f"  Prefix asked: {prefix}   (from the folder name unless you "
                "passed --project / --from-resolver)\n"
                f"  Bound prefix: {my_bound}\n"
                f"  {_remedy(my_bound)}"
            ),
        )

    if me is not None and _same_class(my_bound, prefix):
        return DropVerdict(
            allowed=True,
            reason=REASON_OWN_FAMILY,
            resolved_prefix=prefix,
            bound_prefix=my_bound,
            owner_name=me.name or None,
        )

    # 3. The registry was READ and nobody binds this family: an unregistered
    #    clone rebuilding its own basename-derived graph, or a registered
    #    project that has never been analyzed (no binding row yet). Both are
    #    the historical, legitimate behaviour.
    return DropVerdict(
        allowed=True,
        reason=REASON_UNOWNED_FAMILY,
        resolved_prefix=prefix,
        bound_prefix=my_bound,
        owner_name=(me.name if me is not None else None) or None,
    )


def registry_present(db_path: Optional[Path] = None) -> bool:
    """Does a ``launcher.db`` FILE exist at the resolved path?

    Uses the same resolution ``vco_lib.launcher_db_reader`` uses
    (``$VCT_LAUNCHER_DB_PATH`` → ``vco_lib.paths.launcher_db_path``, which
    honours ``$VCT_STATE_DIR``), so "the registry is absent" here means the
    same file the identity snapshot would have opened is absent. Soft-fails to
    ``True`` — claiming a registry EXISTS is the conservative direction: it
    routes an unresolvable snapshot to a refusal rather than to a blanket
    allow.
    """
    try:
        if db_path is not None:
            return Path(db_path).is_file()
        from vco_lib.paths import launcher_db_path

        return launcher_db_path().is_file()
    except Exception:  # noqa: BLE001 — cannot confirm absence → assume present
        return True


def evaluate(
    repo_path: Optional[Path],
    resolved_prefix: str,
    *,
    db_path: Optional[Path] = None,
) -> DropVerdict:
    """Read the evidence, then :func:`decide`. Never raises.

    The I/O half: one ``launcher.db`` existence probe plus the ONE read-only
    snapshot read owned by :func:`vco_lib.project_identity.resolve_snapshot`.
    A failure to import or read the identity module yields an unresolvable
    snapshot, which (when a registry file is present) refuses the drop.
    """
    exists = registry_present(db_path)
    snapshot = None  # None ⇒ could not be read (never "read, and empty")
    if exists:
        try:
            from vco_lib.project_identity import resolve_snapshot

            snapshot = resolve_snapshot(db_path=db_path)
        except Exception:  # noqa: BLE001 — unreadable, never "empty"
            snapshot = None
    return decide(
        repo_path, resolved_prefix, registry_exists=exists, snapshot=snapshot,
    )


def enforce(
    repo_path: Optional[Path],
    resolved_prefix: str,
    *,
    db_path: Optional[Path] = None,
) -> DropVerdict:
    """:func:`evaluate`, raising :class:`CodeGraphDropRefused` on a refusal.

    The form destructive call sites use: one line at the chokepoint, no
    branch for a caller to forget or to invert. Returns the allowing verdict
    so a caller that wants to log the reason can.
    """
    verdict = evaluate(repo_path, resolved_prefix, db_path=db_path)
    if not verdict.allowed:
        raise CodeGraphDropRefused(verdict)
    return verdict
