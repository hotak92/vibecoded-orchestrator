# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The fixture-shaped class write guard (v0.2.94).

The incident this closes
------------------------
The maintainer's live Weaviate held ``Alpha_KnowledgeGraph`` — 70 REAL
knowledge nodes (``knowledge/concepts/*.md`` of the orchestrator project),
written in bursts on review/test days between 2026-06-13 and 2026-09-07.
``Alpha`` is not a project. It exists in this repository ONLY as a pytest
fixture's project name (``tests/test_kg_access_list.py`` and friends set
``KG_COLLECTION=Alpha_KnowledgeGraph``). So a process running under a test's
environment reached the maintainer's LIVE backend and wrote a real corpus into
a class nothing will ever read.

The mechanism was two ordinary leaks meeting:

  1. ``tests/test_kg_access_list.py::_fresh_server`` applies its env overrides
     with ``os.environ[k] = v`` and documents that it does NOT restore them
     ("Tests are responsible for cleaning up if needed"), so
     ``KG_COLLECTION=Alpha_KnowledgeGraph`` survives into the rest of the
     pytest session.
  2. Nothing pinned ``WEAVIATE_URL``. ``scripts/pre-ship-check.sh``'s full-suite
     leg and CI's ``pytest tests/ -q`` both run with the ambient environment,
     and every shipped resolver defaults to ``http://localhost:8081`` — which,
     on the maintainer's box, is the live instance holding every project's data.
     (``tests/test_deferral_report.py`` even sets that URL explicitly and
     restores with ``os.environ.update(env_backup)``, which cannot REMOVE a key
     the backup did not have — so on a plain run it leaks too.)

Any later test that spawns a real ``sync_knowledge_graph.py`` child — children
inherit ``os.environ`` — then synced the real ``knowledge/`` tree into the
fixture's class on the real backend.

The rule
--------
A write to a class whose project stem is a KNOWN FIXTURE NAME is REFUSED
unless the process explicitly declares that it owns that class, by setting
``VCT_ALLOW_FIXTURE_CLASS_WRITES=1``. ``tests/conftest.py`` sets it for the
whole suite (so pytest keeps working), and any ad-hoc probe harness must set
it deliberately. **An unmarked process writing to ``Alpha_*`` is exactly the
incident**, so an unmarked process is what gets refused.

Refusal is loud and named — the class, the reason, and the env var — never a
silent skip. A silent skip would leave the caller believing it wrote.

Why the table is deliberately SMALL
-----------------------------------
The suite mentions ~200 distinct ``<Stem>_KnowledgeGraph`` literals, and the
set is NOT usable as-is: it contains the REAL shipped names
(``VibeCodedOrchestrator``, ``VCODev``) and generic stems a real user's folder
plausibly carries (``Demo``, ``Test``, ``Project``, ``Shared``). A table that
large would refuse real users' writes — the guard's own failure mode. So the
table holds only placeholder vocabulary that no one names a project: the Greek
letters and the metasyntactic set. Every entry is GROUNDED — it must actually
appear in ``tests/`` as a collection name — and pinned by
``tests/test_v0294_fixture_class_guard.py``, which also forbids any shipped
default from entering it.

This is a companion to :data:`vco_lib.codegraph_naming.WORKTREE_PATH_SEGMENTS`,
the other "this string is not a canonical project name" rule: that one keeps a
throwaway worktree from minting ``<Worktree>_Code*`` classes, this one keeps a
fixture from minting ``<Fixture>_*`` ones. Both answer the same question about
a different source of not-a-project names.

The second half of the containment lives in ``tests/conftest.py``, which pins
``WEAVIATE_URL`` at the unroutable sentinel ``http://127.0.0.1:9`` by default,
so a plain ``pytest`` cannot reach a live backend at all. Each leg holds
alone; neither is a reason to skip the other.
"""

from __future__ import annotations

import os
from typing import Optional

from vco_lib.manifest_paths import MANIFEST_REL_POSIX
from vco_lib.weaviate_schema import _CODE_COLLECTION_SUFFIXES

__all__ = [
    "ALLOW_FIXTURE_WRITES_ENV",
    "COLLECTION_FAMILY_SUFFIXES",
    "FIXTURE_PROJECT_NAMES",
    "FixtureClassWriteRefused",
    "UNREGISTERED_FOLDER_OPENERS",
    "UNROUTABLE_SENTINEL_URL",
    "fixture_stem_of",
    "fixture_writes_allowed",
    "guard_fixture_class_write",
    "guarded_collection_create",
    "is_fixture_shaped_class",
    "refusal_text",
    "unregistered_folder_warning_text",
]

#: The env var a process sets to declare "I am a test/harness and I OWN the
#: fixture-named classes I am about to write." ``tests/conftest.py`` sets it
#: for the whole session; every other caller must set it deliberately.
ALLOW_FIXTURE_WRITES_ENV = "VCT_ALLOW_FIXTURE_CLASS_WRITES"

#: The address the suite points at instead of a real backend. Port 9 is IANA
#: "discard" and nothing listens on it, so a connection attempt fails fast
#: rather than hanging — the property that makes it usable as a default.
UNROUTABLE_SENTINEL_URL = "http://127.0.0.1:9"

#: Project stems that exist ONLY as test-fixture names. Contents are the
#: contract (the `NON_PROJECT_BASE_CLASSES` convention); order is irrelevant.
#: Comparison is case-insensitive because Weaviate capitalises the first
#: letter of a class name on POST, so ``foo_KnowledgeGraph`` is stored as
#: ``Foo_KnowledgeGraph`` and both spellings must match the same entry.
#:
#: Two provenances, both grounded in ``tests/``:
#:   * Greek letters — the multi-project fan-out fixtures (``Alpha`` is the
#:     one with 70 rows on the live box; ``Beta``/``Gamma`` are its peers in
#:     ``VCT_KG_ACCESS_LIST`` fixtures).
#:   * Metasyntactic — ``Foo`` has live residue too (an empty ``Foo_Diagrams``
#:     class), which is the same shape one creation-only step earlier.
#:   * Placeholder-company and negative-space words the suite uses for
#:     "a project that is not real" — ``Acme``/``AcmeCorp`` (the world's
#:     placeholder company), ``Fake``/``FakeProject``, ``Ghost``/``GhostName``
#:     (the unclaimed-class fixtures), ``Phantom``, and the enumerations
#:     ``P1``/``ProjA``. Added v0.2.94 after review: they are fixture names by
#:     provenance, and the guard is only as good as its coverage of them.
#:
#: NOT admitted: stems grounded only in LIVE residue on one machine. The
#: maintainer's Weaviate carries ``Bart_CodeClass/Function/Module`` (55/92/7
#: real objects) whose stem appears in no test file — and that is exactly a
#: name a real project could own. Live residue on one box is hearsay for every
#: other user; the doctor's unclaimed-class report is the right surface for it
#: (it NAMES the class and leaves the decision to the human), not this table,
#: which silently refuses writes. Every entry must be grounded in ``tests/``.
FIXTURE_PROJECT_NAMES: frozenset[str] = frozenset({
    "Acme",
    "AcmeCorp",
    "Alpha",
    "Bar",
    "Baz",
    "Beta",
    "Fake",
    "FakeProject",
    "Foo",
    "Foobar",
    "Gamma",
    "Ghost",
    "GhostName",
    "P1",
    "Phantom",
    "ProjA",
    "Quux",
})

#: Class-name families VCO mints per project. A fixture-shaped class is a
#: table stem followed by ``_`` and one of these.
#:
#: The code-graph five come from :mod:`vco_lib.weaviate_schema` rather than a
#: literal here — a third copy of that list is how the second one drifts.
COLLECTION_FAMILY_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        {"KnowledgeGraph", "Development", "Diagrams"} | set(_CODE_COLLECTION_SUFFIXES)
    )
)

#: Case-folded lookup set, built once. The public frozenset keeps the display
#: spellings so error text and the doctor's report read naturally.
_FIXTURE_FOLDED: frozenset[str] = frozenset(n.casefold() for n in FIXTURE_PROJECT_NAMES)


class FixtureClassWriteRefused(RuntimeError):
    """Raised when an undeclared process tries to write a fixture-shaped class.

    Carries the parts a caller needs to build its own surface's error payload
    (the MCP returns JSON, the scripts print to stderr and exit non-zero) so
    nobody has to re-parse the message.
    """

    def __init__(self, class_name: str, stem: str, operation: str, message: str) -> None:
        super().__init__(message)
        self.class_name = class_name
        self.stem = stem
        self.operation = operation


def fixture_stem_of(class_name: object) -> Optional[str]:
    """The fixture project name behind *class_name*, or ``None``.

    ``"Alpha_KnowledgeGraph"`` -> ``"Alpha"``; ``"VCODev_KnowledgeGraph"`` and
    ``"Alpha"`` (no family suffix) -> ``None``. The stem is returned in the
    TABLE's spelling, not the caller's, so downstream text is consistent.

    The match is suffix-anchored rather than a split on the first ``_``:
    shipped prefixes contain underscores (``VCT_transcrypt_CodeAPI``,
    ``Orchestrator_root_CodeFunction``), so splitting would read
    ``VCT_transcrypt_CodeAPI``'s stem as ``VCT``.
    """
    if not isinstance(class_name, str) or not class_name:
        return None
    for suffix in COLLECTION_FAMILY_SUFFIXES:
        tail = "_" + suffix
        if len(class_name) <= len(tail):
            continue
        if class_name[-len(tail):].casefold() != tail.casefold():
            continue
        stem = class_name[: -len(tail)]
        if stem.casefold() in _FIXTURE_FOLDED:
            for canonical in FIXTURE_PROJECT_NAMES:
                if canonical.casefold() == stem.casefold():
                    return canonical
    return None


def is_fixture_shaped_class(class_name: object) -> bool:
    """True when *class_name* is ``<fixture stem>_<shipped family>``."""
    return fixture_stem_of(class_name) is not None


def fixture_writes_allowed() -> bool:
    """Has this process DECLARED that it owns the fixture classes it writes?

    Resolved at call time (not import) so a harness that sets the variable
    mid-run is honoured — the same contract the shared-KG write gate uses.
    """
    raw = os.environ.get(ALLOW_FIXTURE_WRITES_ENV, "")
    return raw.strip().lower() in ("1", "true", "yes")


def refusal_text(
    class_name: str,
    stem: str,
    *,
    operation: str = "write",
    weaviate_url: Optional[str] = None,
) -> str:
    """The one message every surface prints. Names the class, why, and the fix."""
    where = f" at {weaviate_url}" if weaviate_url else ""
    return (
        f"Refusing to {operation} '{class_name}'{where}: '{stem}' is a TEST "
        f"FIXTURE project name, not a project. A class named "
        f"'<fixture>_<family>' on a real backend is test or probe-harness "
        f"residue — this is the guard for the 2026-09 incident in which 70 "
        f"real knowledge nodes were written into 'Alpha_KnowledgeGraph' on "
        f"the maintainer's live Weaviate, where nothing reads them. "
        f"If this process really is a test or harness that OWNS this class, "
        f"declare it: {ALLOW_FIXTURE_WRITES_ENV}=1 (tests/conftest.py already "
        f"sets it for the whole suite). If '{stem}' is genuinely your "
        f"project's name, set the same variable — and consider renaming, "
        f"because VCO's own fixtures use that name too."
    )


#: Opening clause per surface for :func:`unregistered_folder_warning_text`.
#: TWO entries, because the same condition reaches the user as a completed
#: write (``store_knowledge_node``) and as a failed lookup (the weaviate-kg
#: MCP's schema-error hint), and one text that claimed "this write" on a
#: search would be a false statement in a message whose whole job is to
#: correct a false impression. The parallel to :func:`refusal_text`'s
#: ``operation`` parameter is deliberate.
UNREGISTERED_FOLDER_OPENERS: "dict[str, str]" = {
    "write": "This write went to",
    "search": "This search targeted",
}


def unregistered_folder_warning_text(
    collection: str,
    reason: str,
    *,
    operation: str = "write",
    weaviate_url: Optional[str] = None,
) -> str:
    """The ONE text every surface shows for a call from an UNREGISTERED folder.

    Sibling of :func:`refusal_text`, and here rather than in its caller because
    the two are one vocabulary family: an unmarked process writing where
    nothing will read it. The severities differ — that one REFUSES, this one
    ALLOWS and says so — which is exactly why they must not be written by two
    authors in two files. Keeping them adjacent is what makes a user who meets
    both read one rule instead of two.

    The cases are different enough that this does not CALL ``refusal_text``:
    there, a class name is fixture-shaped and the fix is an env declaration;
    here, the folder carries no bundle manifest and the fix is the launcher's
    Adopt flow. What is shared is the shape (name the class, say why, name the
    fix) and the incident both cite.

    Args:
        collection: the class the call ACTUALLY targeted — never the project
            default, because a ``scope="shared"`` write lands somewhere else
            and naming the wrong one sends the user to check the wrong place.
        reason: why this process counts as unregistered, quoted verbatim from
            the caller's own rule (the two branches — "no manifest here" vs
            "no folder to check" — are different problems and only the user
            can tell which they are in).
        operation: which surface is speaking; see
            :data:`UNREGISTERED_FOLDER_OPENERS`. An unknown value falls back
            to the write opener rather than raising — a warning that can
            raise would be a worse defect than the silence it replaces.
        weaviate_url: the backend, when the caller knows it.

    Pure: no environment, no filesystem, no import of the caller. Everything
    the sentence states is passed in.
    """
    where = f" at {weaviate_url}" if weaviate_url else ""
    opener = UNREGISTERED_FOLDER_OPENERS.get(
        operation, UNREGISTERED_FOLDER_OPENERS["write"]
    )
    return (
        f"{opener} '{collection}'{where}, but this folder is NOT registered "
        f"with VCO ({reason}). VCO's MCP servers are registered globally and "
        f"stay callable from any folder, so nothing refused the call — it used "
        f"whatever collection this process happened to resolve, which nothing "
        f"in this folder reads. That is the same shape as the 2026-09 "
        f"fixture-class incident, in which 70 real knowledge nodes were "
        f"written where nothing reads them. Register the folder and knowledge "
        f"lands in its OWN collection: launcher -> Projects -> Add project -> "
        f"Adopt this folder. Adopt creates {MANIFEST_REL_POSIX}, "
        f"binds this folder to its own collections, and syncs the existing "
        f"knowledge/**/*.md nodes during setup (no manual kg-sync needed). "
        f"Until then every write from here keeps landing in '{collection}'."
    )


def guarded_collection_create(client, name: str, **create_kwargs):
    """``client.collections.create(name=..., **kw)`` with the guard in front.

    The seam for callers whose create is a bare v4 call: they swap one line
    and inherit the refusal, instead of growing an inline copy of it. Used by
    ``templates/scripts/analyze_code_graph.py``, whose five ``<Prefix>_Code*``
    classes are the families with the most live residue on the maintainer's
    box (``Wt_foo_Code*``, ``LaneCProbe*_Code*``) and which sits under a
    downward-only line ratchet, so an inline guard there was not available.
    """
    guard_fixture_class_write(name, operation="create")
    return client.collections.create(name=name, **create_kwargs)


def guard_fixture_class_write(
    class_name: str,
    *,
    operation: str = "write",
    weaviate_url: Optional[str] = None,
) -> None:
    """Raise :class:`FixtureClassWriteRefused` for an undeclared fixture write.

    A no-op for every real project's class, and a no-op for any process that
    has set :data:`ALLOW_FIXTURE_WRITES_ENV`. Call it before the write, not
    after: the point is that nothing lands.
    """
    stem = fixture_stem_of(class_name)
    if stem is None or fixture_writes_allowed():
        return
    raise FixtureClassWriteRefused(
        class_name,
        stem,
        operation,
        refusal_text(
            class_name, stem, operation=operation, weaviate_url=weaviate_url
        ),
    )
