# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Authoritative per-project IDENTITY resolution (v0.2.92 W8).

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

Every destructive-candidate detector in ``vco_lib.project_init`` (legacy-KG,
legacy-code-graph, the code-prefix drift forward-guard) needs the answer to one
question: **which Weaviate classes does this project actually read?** Before
v0.2.92 each of them answered it by sanitizing the project FOLDER BASENAME.

That derivation is wrong for any registered project whose folder basename
differs from its registered ``projects.name`` — the commonest cause being a
folder move, but a rename in the launcher GUI, or simply adding a folder under
a different display name, reproduces it identically. When it is wrong the
detectors classify the project's OWN live, correctly-bound collections as
"legacy data under a non-canonical prefix" and emit migrate/DROP commands
against them.

The fix is a single identity SSOT. ``launcher.db`` is the ground truth (it is
what vct-hub itself serves on ``/api/v1/projects/{id}/config``); this module
reads it ONCE, read-only, and returns the registered name plus **every live
bound collection** — KG primary, KG shared, the derived development and
diagrams siblings, and the code-graph class prefix — for every registered
project on the machine. The folder basename survives only as the LAST-RESORT
identity for a folder the launcher has genuinely never seen.

Composition, not duplication
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module owns NO new naming rule and NO new DB-open rule. It composes the
existing single-home helpers:

* :func:`vco_lib.launcher_db_reader._open_db_readonly` — the ONE read-only
  ``launcher.db`` open (``file:...?mode=ro``, honours ``VCT_LAUNCHER_DB_PATH``
  via ``vco_lib.paths``, accepts an explicit path as a test seam). Imported
  intra-package deliberately: a third copy of that open is exactly the
  duplication CLAUDE.md's modularity rule forbids.
* :func:`vco_lib.config_projection._fetch_kg_bindings` /
  :func:`~vco_lib.config_projection._fetch_codegraph_binding_prefix` — the
  existing per-project binding reads, both of which already take an
  ALREADY-OPEN connection, so all reads here share ONE open (the SEV-3 #3
  TOCTOU discipline: ``resolvable`` and the rows it describes come from the
  same connection).
* :func:`vco_lib.config_projection._derive_dev_diagrams_from_kg` — the ONE
  python dev/diagrams derivation (v0.2.84 D1), pinned to the Rust
  ``collection_naming::derive_sibling_collection`` by parity tests. Re-deriving
  here would break that pin.
* :func:`vco_lib.codegraph_naming.sanitize_for_weaviate_class` (the KG basename
  SSOT) and ``codegraph_to_mermaid._sanitize_collection_prefix`` (the endorsed
  never-raising wrapper over the code-prefix SSOT ``canonical_class_prefix`` —
  the same one ``project_init.derive_project_code_prefix`` delegates to), used
  only for the no-binding-row fallbacks.

Conservative posture
~~~~~~~~~~~~~~~~~~~~

:attr:`IdentitySnapshot.resolvable` is ``False`` whenever the identity could
not be POSITIVELY READ — the DB could not be opened, the file is not a
database, the ``projects`` table is absent (foreign / half-migrated schema), or
a binding read failed part-way. Callers that gate a destructive suggestion MUST
treat that as "cannot confirm what is live → propose nothing", never as "no
project is live". ``resolvable=True`` with an empty ``projects`` tuple means
the tables were READ and hold no registered project — that state is safe to act
on.

v0.2.92 F-1 fixed the inverse of that rule: ``_rows_to_identities`` mapped a
FAILED ``SELECT … FROM projects`` to ``()`` while ``resolve_snapshot`` still
reported ``resolvable=True``. SQLite opens lazily, so a non-SQLite byte blob
(or a valid DB with no ``projects`` table) at ``VCT_LAUNCHER_DB_PATH`` read as
"genuinely empty machine", and both run-time drop guards in ``project_init``
then ALLOWED the drop of live, named collections. "The DB says there are no
rows" and "I could not read the DB" are different answers; they no longer share
a representation.

Nothing in this module raises: every public entry point soft-fails to an
unresolvable snapshot / ``None`` identity.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "ProjectIdentity",
    "IdentitySnapshot",
    "SOURCE_BINDING",
    "SOURCE_DERIVED",
    "normalise_for_match",
    "canonical_folder_key",
    "resolve_snapshot",
    "resolve_identity",
]


# ---------------------------------------------------------------------------
# Normalisation (the ONE home — mirrors Rust `project_identity.rs`)
# ---------------------------------------------------------------------------

def normalise_for_match(s: str) -> str:
    """Strip every non-ASCII-alphanumeric character and lowercase.

    Python mirror of the Rust ``project_identity.rs::normalise_prefix_for_match``.
    Must match the Rust rule byte-for-byte so the Python detectors and the Rust
    wizard agree on which prefixes / class names are "the same":
    ``"VibeCoded_Orchestrator"``, ``"vibecodedorchestrator"`` and
    ``"VibeCoded Orchestrator"`` all normalise equal.

    NOTE (contract, do not "fix"): this normalises the WHOLE string. It does
    NOT reduce a class name to its prefix — ``"Foo_KnowledgeGraph"`` normalises
    to ``"fooknowledgegraph"``, not ``"foo"``. Callers that want prefix
    semantics must strip the suffix themselves before calling.

    ``vco_lib.project_init._normalise_prefix_for_match`` delegates here so the
    rule has exactly one Python home.
    """
    return "".join(c.lower() for c in (s or "") if c.isascii() and c.isalnum())


def _code_prefix(name: str) -> str:
    """Never-raising code-graph prefix for ``name``.

    Delegates to the endorsed wrapper the analyzer + binding writer already
    use (``codegraph_to_mermaid._sanitize_collection_prefix`` over the
    underscore-PRESERVING ``canonical_class_prefix`` SSOT) — the identical
    delegation ``project_init.derive_project_code_prefix`` performs. Returns
    ``""`` when no usable prefix can be derived.
    """
    try:
        from vco_lib.codegraph_to_mermaid import _sanitize_collection_prefix
        return _sanitize_collection_prefix(name or "") or ""
    except Exception:  # noqa: BLE001 — a name helper must never break a caller
        return ""


def expected_kg_primary_class(name: str) -> str:
    """The name-derived primary KG class for ``name`` — the ONE home for the rule.

    ``f"{sanitize_for_weaviate_class(name)}_KnowledgeGraph"`` — the last-resort
    name the hub/launcher/``config_projection`` fall back to when no binding
    row exists (both call sites below used to inline their own copy of the
    f-string), and the comparison baseline ``vco_lib.kg_binding_doctor``
    measures binding drift against. Extracted (v0.2.92 D18 re-closure)
    because a third inlining was about to appear and the modularity rule is
    explicit: the fallback and the drift probe must agree on what a project's
    class "should" be called, forever, and agreement is cheaper to keep in one
    function than in three copies.
    """
    from vco_lib.codegraph_naming import sanitize_for_weaviate_class

    return f"{sanitize_for_weaviate_class(name)}{PRIMARY_KG_SUFFIX}"


def canonical_folder_key(folder: Path | str) -> Path:
    """Canonicalise a folder for comparison against ``projects.folder_path``.

    ``Path.resolve()`` collapses symlinks, ``..`` segments and trailing
    separators. ``strict=False`` semantics (the default since 3.6) keep a
    non-existent path (a stale DB row pointing at a deleted folder) resolvable
    rather than raising — such a row then simply fails to match.

    Same rule as the ``_canon`` closure inside
    ``config_projection.resolve_collection_names_for_folder``; that one is a
    closure and importing this module from ``config_projection`` would create a
    cycle (this module depends on it). ``tests/test_v0292_project_identity.py``
    pins the two to the same answers.
    """
    p = Path(folder)
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

#: The PRIMARY KG class suffix — the family the primary binding names.
#: Public because :func:`expected_kg_primary_class` builds on it and
#: ``vco_lib.kg_binding_doctor`` (the read-only evidence probe) filters
#: candidate classes by it. One home for the string; a second literal would
#: let the naming rule and the evidence probe disagree about the family.
PRIMARY_KG_SUFFIX = "_KnowledgeGraph"

#: KG-family class suffixes whose stem is the shared per-project basename.
#: (Same family ``vco_lib.project_init._KG_SUFFIXES`` scans, plus ``_Diagrams``
#: which the diagram indexer owns — a bound class either way.)
_KG_FAMILY_SUFFIXES: tuple[str, ...] = (
    PRIMARY_KG_SUFFIX,
    "_Development",
    "_Diagrams",
)

#: The five code-graph entity suffixes (mirrors
#: ``vco_lib.project_init._CODEGRAPH_SUFFIXES``; kept local so this module has
#: no import edge back into the 15k-line mega-module).
_CODE_SUFFIXES: tuple[str, ...] = (
    "_CodeFunction",
    "_CodeModule",
    "_CodeClass",
    "_CodeAPI",
    "_CodeInteraction",
)


#: Provenance marker: the value came from a real binding row in launcher.db.
SOURCE_BINDING = "binding"
#: Provenance marker: the value was SANITIZED from a name (a guess, not a fact).
SOURCE_DERIVED = "derived"


@dataclass(frozen=True)
class ProjectIdentity:
    """One project's authoritative identity.

    ``registered`` is ``False`` for the last-resort basename identity produced
    by :func:`resolve_identity` for a folder that is genuinely not in
    ``launcher.db``. Every other instance came from a real ``projects`` row.

    PROVENANCE (v0.2.92 F-3). ``kg_primary`` and ``codegraph_prefix`` are
    populated even when no binding row exists — from the same name-sanitizer
    fallback the hub/launcher use, so callers always have SOMETHING to name a
    collection with. That makes a guess indistinguishable from a fact at the
    call site, and ``project_init.detect_codegraph_prefix_drift`` consequently
    stamped a name-derived prefix into
    ``.claude/state/codegraph-prefix-generation.json`` labelled
    ``source="binding"``/authoritative. ``kg_primary_source`` /
    ``codegraph_prefix_source`` carry the distinction: only
    :data:`SOURCE_BINDING` may be treated as authoritative.
    """

    name: str
    project_id: Optional[str] = None
    slug: str = ""
    folder_path: Optional[str] = None
    kg_primary: Optional[str] = None
    kg_shared: Optional[str] = None
    development: Optional[str] = None
    diagrams: Optional[str] = None
    codegraph_prefix: Optional[str] = None
    registered: bool = True
    #: Where ``kg_primary`` came from — :data:`SOURCE_BINDING` or
    #: :data:`SOURCE_DERIVED`.
    kg_primary_source: str = SOURCE_DERIVED
    #: Where ``codegraph_prefix`` came from — :data:`SOURCE_BINDING` or
    #: :data:`SOURCE_DERIVED`.
    codegraph_prefix_source: str = SOURCE_DERIVED
    #: EVERY ``project_kg_bindings.collection_name`` row this project owns,
    #: role-UNFILTERED and verbatim (v0.2.92 F-4). ``kg_primary`` / ``kg_shared``
    #: model only the two roles the projection consumes; a row with any OTHER
    #: role (``archive`` on pre-v0.2.46 installs, or a future role) names a
    #: bound class that is just as much live data. The keep-set is a PROTECTION
    #: list — enumerating fewer rows than HEAD did is a data-loss regression, so
    #: every bound row lands here regardless of role.
    bound_kg_collections: tuple[str, ...] = ()
    #: Class names this project's bindings USED to name, recovered from the
    #: v0.2.92 D18 heal's ``config_json`` audit record
    #: (``evidence_repoint.from``). NOT live bindings — deliberately kept out
    #: of :attr:`bound_kg_collections`, whose contract is "verbatim rows" and
    #: whose readers (the doctor's ownership map) must keep seeing the old
    #: class as unbound. They exist for ONE purpose: :meth:`kg_keep_tokens`.
    #: A re-pointed class is still populated and named by no binding row, so
    #: without this an automated repair would quietly turn live data into a
    #: drop candidate for the legacy-collection detector. A keep-set is a
    #: PROTECTION list; what was protected before a repoint stays protected
    #: after it.
    previously_bound_kg_collections: tuple[str, ...] = ()

    def authoritative_codegraph_prefix(self) -> Optional[str]:
        """``codegraph_prefix`` iff it came from a real binding row, else None.

        The ONE accessor any caller that STAMPS or ACTS on the prefix should
        use. ``codegraph_prefix`` itself stays populated (with the derived
        guess) for keep-set widening, where over-matching is the safe direction.
        """
        if self.codegraph_prefix_source != SOURCE_BINDING:
            return None
        return (self.codegraph_prefix or "").strip() or None

    def kg_collections(self) -> tuple[str, ...]:
        """Every KG-family class this project READS (deduped, order-stable).

        Includes the shared-KG binding: a project reading the shared collection
        must never propose dropping it either.

        v0.2.92 F-4: also includes every ROLE-UNFILTERED
        ``project_kg_bindings.collection_name`` row
        (:attr:`bound_kg_collections`). The four modelled slots above cover the
        ``primary``/``shared`` roles plus their DERIVED siblings; a bound row
        under any other role (``archive``, or anything a future launcher
        writes) names a live class that none of them reproduce, and HEAD's
        role-unfiltered ``launcher_db_reader.kg_binding_keep_set()`` did
        protect it.
        """
        out: list[str] = []
        for value in (
            self.kg_primary,
            self.kg_shared,
            self.development,
            self.diagrams,
            *self.bound_kg_collections,
        ):
            if value and value not in out:
                out.append(value)
        return tuple(out)

    def code_collections(self) -> tuple[str, ...]:
        """The five ``<prefix>_Code*`` classes bound to this project, or ``()``
        when no code-graph prefix is resolvable."""
        pfx = (self.codegraph_prefix or "").strip()
        if not pfx:
            return ()
        return tuple(f"{pfx}{sfx}" for sfx in _CODE_SUFFIXES)

    def kg_canonical_prefix(self) -> str:
        """The KG-family basename this project's classes share.

        Taken from the RESOLVED primary binding (suffix-stripped) so a
        case-drifted or custom-cased binding wins over the sanitizer's guess;
        falls back to the name-derived sanitizer when there is no binding.
        """
        primary = (self.kg_primary or "").strip()
        for sfx in _KG_FAMILY_SUFFIXES:
            if primary.endswith(sfx) and len(primary) > len(sfx):
                return primary[: -len(sfx)]
        from vco_lib.codegraph_naming import sanitize_for_weaviate_class
        return sanitize_for_weaviate_class(self.name)

    def keep_tokens(self) -> set[str]:
        """Normalised tokens that mark this project's live data.

        Contains, for every KG-family class the project reads, BOTH the
        normalised full class name AND the normalised class PREFIX; plus the
        normalised code-graph prefix. See
        ``IdentitySnapshot.kg_keep_tokens`` for why both halves are kept.
        """
        tokens: set[str] = set()
        for cls in self.kg_collections():
            tokens.add(normalise_for_match(cls))
            tokens.add(normalise_for_match(_strip_family_suffix(cls)))
        pfx = (self.codegraph_prefix or "").strip()
        if pfx:
            tokens.add(normalise_for_match(pfx))
        tokens.discard("")
        return tokens


def _strip_family_suffix(class_name: str) -> str:
    """Return ``class_name`` minus a known KG-family / code suffix (or itself).

    A class whose suffix we do not recognise (a user-renamed custom primary
    like ``MyCustom_KG_Store``) yields the whole name — deliberately: its
    normalised full name is then the only token we can offer, which is exactly
    what the exact-name half of the keep-set is for.
    """
    for sfx in _KG_FAMILY_SUFFIXES + _CODE_SUFFIXES:
        if class_name.endswith(sfx) and len(class_name) > len(sfx):
            return class_name[: -len(sfx)]
    return class_name


@dataclass(frozen=True)
class IdentitySnapshot:
    """Every registered project's identity, read in one ``launcher.db`` open.

    ``resolvable`` is False whenever the identity could not be POSITIVELY READ
    — the DB could not be opened, the file is not a database, the ``projects``
    table is absent, or a binding read failed. Callers gating a destructive
    suggestion must propose nothing in that case (v0.2.92 F-1).
    """

    resolvable: bool
    projects: tuple[ProjectIdentity, ...] = field(default_factory=tuple)

    def identity_for_folder(self, folder: Path | str) -> Optional[ProjectIdentity]:
        """Return the registered identity whose ``folder_path`` canonicalises
        to ``folder``, or ``None``."""
        target = canonical_folder_key(folder)
        for proj in self.projects:
            stored = (proj.folder_path or "").strip()
            if not stored:
                continue
            if canonical_folder_key(stored) == target:
                return proj
        return None

    def kg_keep_tokens(self) -> set[str]:
        """Normalised keep-tokens covering EVERY project's live KG-family data.

        Two halves, unioned into one set so matching is a single membership
        test (``normalise(class) in tokens or normalise(prefix) in tokens``):

        (a) **exact class names** — every bound primary / shared collection AND
            the development / diagrams siblings derived from them. This is what
            protects the custom-rename case, where the dev name is derived from
            the SLUG and therefore shares no prefix with the primary.
        (b) **class-name prefixes** — the stem each family member shares. This
            is what protects a family member the enumeration in (a) does not
            know about: ``project_kg_bindings`` stores NO row for
            ``*_Development`` / ``*_Diagrams`` (the launcher derives them by
            suffix swap — hub Decision C, v0.2.46), so an exact-name-only
            keep-set built from binding rows can never contain them, which is
            precisely how a live 8-object ``*_Development`` class became a
            drop target in the field.

        Keeping both is deliberate: each half closes the other's residual hole,
        and both fail in the SAFE direction. The cost of over-matching is a
        genuinely-dead class that survives on disk; the cost of under-matching
        is unrecoverable deletion of live data.

        v0.2.92 F-4: the enumeration in (a) is now ROLE-UNFILTERED — it walks
        :meth:`ProjectIdentity.kg_collections`, which includes every
        ``project_kg_bindings`` row this project owns whatever its ``role``, not
        just the two roles the env projection consumes. Restoring that is a
        regression fix: HEAD's keep-set was ``kg_binding_keep_set()`` with
        "``role`` is not filtered" as an explicit SEV-2 #2 invariant, and
        routing it through a primary/shared-only read silently dropped
        ``archive``-role rows (pre-v0.2.46 installs) from a PROTECTION list.

        v0.2.92 D18: the enumeration also covers
        :attr:`ProjectIdentity.previously_bound_kg_collections` — the classes
        the evidence-backed repoint moved a binding OFF. Those are still
        populated and are now named by no binding row, so leaving them out
        would mean an automated repair had made live data a drop candidate.
        Protection only ever widens.
        """
        tokens: set[str] = set()
        for proj in self.projects:
            for cls in (*proj.kg_collections(),
                        *proj.previously_bound_kg_collections):
                tokens.add(normalise_for_match(cls))
                tokens.add(normalise_for_match(_strip_family_suffix(cls)))
        tokens.discard("")
        return tokens

    # NOTE — no `code_keep_prefixes()` counterpart on purpose. The code-graph
    # keep-set already has ONE home:
    # ``project_init._codegraph_keep_set_normalised`` over
    # ``launcher_db_reader.codegraph_binding_keep_set`` (bindings UNION
    # extra-path owners — a set this snapshot does not model). Adding a second
    # producer here would fork the rule that gates the orphan-reclaim path.
    # Per-project code identity is still available as
    # ``ProjectIdentity.codegraph_prefix`` / ``.keep_tokens()``.


_UNRESOLVABLE = IdentitySnapshot(resolvable=False, projects=())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _fetch_previously_bound_kg_collections(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[str, ...]:
    """Classes this project's bindings USED to name, per the D18 heal audit.

    ``vco_lib.kg_binding_heal`` records the previous ``collection_name`` in the
    binding row's ``config_json`` under ``evidence_repoint.from`` whenever it
    re-points a primary binding at the class holding the project's data. That
    old class is still populated and is now named by no binding row, so it
    would drop out of :meth:`IdentitySnapshot.kg_keep_tokens` — turning live
    data into a candidate for the legacy-collection drop detector as a
    side-effect of an automatic repair. Feeding it back into the keep-set is
    the conservative direction and the only one: a keep-set may over-match
    (a dead class survives on disk) but never under-match.

    Raises on a read failure so the caller can refuse the whole snapshot —
    the F-1 rule. A row with absent / unparsable / differently-shaped config
    simply contributes nothing.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT config_json FROM project_kg_bindings WHERE project_id = ?",
        (project_id,),
    )
    import json as _json

    out: list[str] = []
    for row in cur.fetchall():
        raw = row["config_json"]
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            cfg = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(cfg, dict):
            continue
        audit = cfg.get("evidence_repoint")
        if not isinstance(audit, dict):
            continue
        previous = audit.get("from")
        if isinstance(previous, str) and previous.strip():
            out.append(previous.strip())
    return tuple(dict.fromkeys(out))


def _rows_to_identities(
    conn: sqlite3.Connection,
) -> Optional[tuple[ProjectIdentity, ...]]:
    """Build every project's identity on an ALREADY-OPEN connection.

    Returns ``None`` — NOT ``()`` — when the read could not be COMPLETED:

    * the ``SELECT … FROM projects`` failed (the file is not a database, the
      table is absent on a foreign/half-migrated schema, the page is corrupt);
    * a project's ``project_kg_bindings`` read raised (absent table, corrupt
      page). Degrading that to a name-DERIVED primary, as this function used to,
      silently drops a custom-bound collection out of the keep-set while the
      snapshot still reports itself resolvable.
    * a project's ``project_codegraph_bindings`` read raised — i.e. the DB
      could not be READ (v0.2.92 W18). An ABSENT binding (no row, or no such
      table on a pre-migration DB) is NOT this case: that read succeeded, and
      it legitimately routes to the marked-as-derived placeholder prefix.

    ``()`` means the ``projects`` table was READ and is genuinely empty.
    :func:`resolve_snapshot` maps ``None`` to an unresolvable snapshot, which is
    what makes both ``project_init`` drop guards REFUSE rather than allow
    (v0.2.92 F-1).
    """
    # Imported here (not at module import time) so a partial install that lacks
    # config_projection still lets `resolve_snapshot` soft-fail to unresolvable
    # rather than exploding at import of `project_init`.
    from vco_lib.config_projection import (
        _derive_dev_diagrams_from_kg,
        _fetch_codegraph_binding_prefix,
        _fetch_kg_bindings,
    )

    try:
        rows = conn.execute(
            "SELECT id, name, slug, folder_path FROM projects"
        ).fetchall()
    except Exception:  # noqa: BLE001 — malformed / partial schema / not a DB
        return None

    out: list[ProjectIdentity] = []
    for row in rows:
        try:
            pid = str(row["id"])
            name = str(row["name"] or "")
            slug = str(row["slug"] or "")
            folder_path = str(row["folder_path"] or "")
        except Exception:  # noqa: BLE001 — unexpected row shape
            continue
        if not pid:
            continue

        try:
            bindings = _fetch_kg_bindings(conn, pid)
            previously_bound = _fetch_previously_bound_kg_collections(conn, pid)
        except Exception:  # noqa: BLE001 — CANNOT CONFIRM this project's KG
            # F-1/F-4: not "this project has no bindings" — we failed to read
            # them. A name-derived primary here would quietly shrink the
            # keep-set, so refuse the whole snapshot instead. Same rule for the
            # repoint-audit read: it too only ever WIDENS the keep-set, so a
            # failure that silently narrowed it is the exact regression F-1
            # exists to prevent.
            return None
        primary = (bindings.get("primary") or "").strip()
        primary_source = SOURCE_BINDING
        if not primary:
            # No binding row yet (fresh create before the seed). Same
            # last-resort the hub/launcher/config_projection all use — and a
            # GUESS, so marked as such (F-3).
            primary = expected_kg_primary_class(name)
            primary_source = SOURCE_DERIVED
        shared = (bindings.get("shared") or "").strip() or None
        # F-4: every bound row, role-unfiltered, verbatim. `_fetch_kg_bindings`
        # already SELECTs all roles; only the projection below narrows to two.
        bound = tuple(
            v.strip() for v in bindings.values()
            if isinstance(v, str) and v.strip()
        )
        # The ONE derivation home (v0.2.84 D1) — never re-derive here.
        development, diagrams = _derive_dev_diagrams_from_kg(primary, slug)

        try:
            code_prefix = _fetch_codegraph_binding_prefix(conn, pid)
        except Exception:  # noqa: BLE001 — CANNOT CONFIRM this project's code
            # v0.2.92 W18: same rule as the `_fetch_kg_bindings` arm above, and
            # for the same reason. `_fetch_codegraph_binding_prefix` now RAISES
            # `ProbeUnavailable` when the DB could not be READ (corrupt image,
            # locked, I/O error) and returns None only for a SUCCESSFUL read
            # that found no binding. Swallowing the raise here fell through to
            # the name-derived guess below, so "I could not read the binding"
            # produced a populated identity inside a snapshot still reporting
            # `resolvable=True` — the module docstring's promise that a binding
            # read failing part-way makes the snapshot unresolvable was true
            # for the KG half and false for this one. Refuse the whole snapshot
            # instead; `resolve_snapshot` maps None to `_UNRESOLVABLE`, and the
            # consumers that gate a destructive suggestion then propose nothing.
            return None
        code_source = SOURCE_BINDING if code_prefix else SOURCE_DERIVED
        if not code_prefix:
            # The read SUCCEEDED and no binding row names a prefix (no analyzer
            # run yet, or a pre-migration DB with no
            # `project_codegraph_bindings` table — a true, structural absence).
            # So: the prefix the analyzer WILL use. A GUESS, and marked as one,
            # because nothing may STAMP a derived prefix as authoritative
            # (see F-3).
            code_prefix = _code_prefix(name) or None

        out.append(
            ProjectIdentity(
                name=name,
                project_id=pid,
                slug=slug,
                folder_path=folder_path or None,
                kg_primary=primary,
                kg_shared=shared,
                development=development or None,
                diagrams=diagrams or None,
                codegraph_prefix=code_prefix,
                registered=True,
                kg_primary_source=primary_source,
                codegraph_prefix_source=code_source,
                bound_kg_collections=bound,
            previously_bound_kg_collections=previously_bound,
            )
        )
    return tuple(out)


def resolve_snapshot(*, db_path: Optional[Path] = None) -> IdentitySnapshot:
    """Read every registered project's identity from ``launcher.db``.

    ONE read-only open; every row read shares it (so ``resolvable`` and the
    rows it describes cannot disagree — the SEV-3 #3 TOCTOU discipline).

    Args:
        db_path: explicit ``launcher.db`` (test seam). ``None`` → the standard
            ``VCT_LAUNCHER_DB_PATH`` → ``vco_lib.paths.launcher_db_path()``
            discovery owned by ``launcher_db_reader``.

    Returns:
        An :class:`IdentitySnapshot`; ``resolvable=False`` when the DB could
        not be opened OR could not be READ (not a database / absent ``projects``
        or ``project_kg_bindings`` table / corrupt page). Never raises.
    """
    try:
        # Intra-package reuse of the ONE read-only launcher.db open helper.
        from vco_lib.launcher_db_reader import _open_db_readonly
    except Exception:  # noqa: BLE001 — broken/partial install
        return _UNRESOLVABLE
    try:
        conn = _open_db_readonly(db_path)
    except Exception:  # noqa: BLE001 — the helper soft-fails, belt-and-braces
        return _UNRESOLVABLE
    if conn is None:
        return _UNRESOLVABLE
    try:
        rows = _rows_to_identities(conn)
        # F-1: `None` = the read FAILED. Only a real, completed read (possibly
        # of zero rows) may claim `resolvable=True` — the guards downstream
        # treat that claim as licence to drop anything not in the keep-set.
        if rows is None:
            return _UNRESOLVABLE
        return IdentitySnapshot(resolvable=True, projects=rows)
    except Exception:  # noqa: BLE001
        return _UNRESOLVABLE
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def resolve_identity(
    folder: Path | str,
    *,
    fallback_name: Optional[str] = None,
    snapshot: Optional[IdentitySnapshot] = None,
    db_path: Optional[Path] = None,
) -> tuple[Optional[ProjectIdentity], IdentitySnapshot]:
    """Resolve the AUTHORITATIVE identity of ``folder``.

    Returns ``(identity, snapshot)``:

    * ``snapshot.resolvable is False`` → ``identity`` is ``None``. The caller
      cannot confirm what is live and MUST NOT propose any destructive action
      (and specifically must NOT fall back to a basename-derived identity —
      that fallback is the whole bug this module exists to kill).
    * folder IS registered → the registered identity (``registered=True``).
    * folder is NOT registered but the DB was readable → a last-resort identity
      derived from ``fallback_name`` or the folder basename, with
      ``registered=False``. This is the legitimate standalone-CLI /
      never-added-to-the-launcher case the legacy detectors were written for.

    Args:
        folder: the project folder.
        fallback_name: explicit display name for the unregistered case
            (``install-bundle --project-name``); defaults to the basename.
        snapshot: reuse an already-resolved snapshot (avoids re-reading the DB
            once per consumer within one install run).
        db_path: explicit ``launcher.db`` (test seam), used only when
            ``snapshot`` is None.
    """
    snap = snapshot if snapshot is not None else resolve_snapshot(db_path=db_path)
    if not snap.resolvable:
        return (None, snap)
    found = snap.identity_for_folder(folder)
    if found is not None:
        return (found, snap)

    from vco_lib.config_projection import _derive_dev_diagrams_from_kg

    name = (fallback_name or "").strip() or Path(folder).name or "Project"
    primary = expected_kg_primary_class(name)
    development, diagrams = _derive_dev_diagrams_from_kg(primary, name)
    return (
        ProjectIdentity(
            name=name,
            project_id=None,
            slug="",
            folder_path=str(Path(folder)),
            kg_primary=primary,
            kg_shared=None,
            development=development or None,
            diagrams=diagrams or None,
            codegraph_prefix=_code_prefix(name) or None,
            registered=False,
            # Every value above is sanitized from a NAME — no binding row backs
            # any of it. Marking it (v0.2.92 F-3) is what stops
            # `detect_codegraph_prefix_drift` from stamping a basename
            # derivation into the generation record labelled "binding": the
            # folder-match failure that lands here (stale `projects.folder_path`
            # after an out-of-protocol move, or a case-insensitive filesystem)
            # is exactly the state where the label would be a lie.
            kg_primary_source=SOURCE_DERIVED,
            codegraph_prefix_source=SOURCE_DERIVED,
            bound_kg_collections=(),
        ),
        snap,
    )
