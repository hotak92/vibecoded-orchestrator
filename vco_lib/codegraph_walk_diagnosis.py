# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Why did a code-graph walk index NOTHING? — the ONE home for the verdict
(v0.2.105).

THE FIELD DEFECT: on Windows, a project with 146 real source files ran
``code-graph-analyze``, which exited 0 with an empty log and ZERO modules.
Every path that can end a whole-tree walk with nothing dispatched used to
print at most a stdout banner ("NO FILES INDEXED") and exit 0, so a launcher
build, a deferral retry and a CLI run all recorded success.

Those paths, and what each now says (the analyzer feeds this module; the
wording and the exit code are decided here only):

* every file with a supported extension was EXCLUDED by a directory or
  file-name rule → names each rule with its count, exit
  :data:`EXIT_NOTHING_INDEXED` (the analyzer's documented "no files indexed"
  code, which the launcher already renders as a warning). The most damaging
  member of this class — the walk judging the ABSOLUTE path, so a project
  under ``…\\build\\…``, ``…\\env\\…``, ``…\\out\\…`` or a ``worktrees``
  folder lost every file — is fixed at its source
  (:func:`vco_lib.codegraph_source_filter.walk_rel_parts`); this verdict is
  what still speaks if a rule legitimately removes everything;
* ``--incremental`` found files but none changed in the diff range, and the
  root already HAS indexed rows → a clear stderr line naming the range and
  the count, exit 0 (the graph is current — nothing was owed). A root with
  NO indexed rows is never gated this way: the analyzer walks it in full
  (the incremental "nothing changed" gate on a never-built graph was the
  silent zero-module walk's second cause);
* no file with a supported extension exists at all (a docs-only folder, or a
  ``--language`` filter matching nothing) → a stderr line listing what the
  walk looks for, exit 0: nothing is owed there, and a non-zero exit would
  make every retry driver re-run a walk that can never index anything.

The verdicts are pure decisions over plain data (no Weaviate, no filesystem),
unit-testable both ways; :func:`root_incremental_gate` takes the module
collection as an argument (one ``limit=1`` read) so a test passes a stub.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence, Tuple

#: The analyzer's "no files were indexed" exit code. MUST MATCH the exit-code
#: contract in ``templates/scripts/analyze_code_graph.py::main`` (3 → the
#: launcher's "No files indexed — check repo path / filters" warning).
EXIT_NOTHING_INDEXED = 3

#: Prefix of every line this module produces, so a log reader can grep one
#: token for the whole family.
TAG = "code-graph walk:"


@dataclass(frozen=True)
class WalkCensus:
    """What a whole-tree walk of ONE source root sees."""

    root: str
    #: distinct files whose extension a finder globs for
    candidates: int
    #: candidates that survive every exclusion rule (what the walk dispatches
    #: before any incremental gate)
    walked: int
    #: (rule, count) for the excluded candidates, most frequent first
    excluded: Tuple[Tuple[str, int], ...] = ()


def census_from_pairs(root: str, pairs: Iterable[Tuple[object, Optional[str]]]) -> WalkCensus:
    """Fold ``(path, exclusion_reason_or_None)`` pairs into a census.

    A path seen twice (two globs matching one file) counts once; its first
    verdict wins.
    """
    seen: dict = {}
    for path, why in pairs:
        key = str(path)
        if key not in seen:
            seen[key] = why
    counts: dict = {}
    walked = 0
    for why in seen.values():
        if why is None:
            walked += 1
        else:
            counts[why] = counts.get(why, 0) + 1
    excluded = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return WalkCensus(root=root, candidates=len(seen), walked=walked, excluded=excluded)


@dataclass(frozen=True)
class EmptyWalkVerdict:
    """The lines to print (stderr) and the exit code for a walk that
    dispatched no file at all."""

    exit_code: int
    lines: Tuple[str, ...]


def _rules(excluded: Sequence[Tuple[str, int]]) -> str:
    return ", ".join(f"{rule} ({count})" for rule, count in excluded) or "none"


def empty_walk_verdict(
    censuses: Sequence[WalkCensus],
    *,
    incremental: bool = False,
    unchanged_files: int = 0,
    diff_range: str = "HEAD~1..HEAD",
    language: Optional[str] = None,
    extensions_hint: str = "",
) -> EmptyWalkVerdict:
    """The verdict for a whole-tree walk that dispatched NO file.

    ``censuses``: one per walked source root (the project, then each extra
    path). ``unchanged_files``: files the ``--incremental`` gate dropped as
    unchanged (only counted on roots that already had indexed rows).
    """
    candidates = sum(c.candidates for c in censuses)
    walked = sum(c.walked for c in censuses)
    roots = ", ".join(c.root for c in censuses) or "(no root)"
    lang_note = f" for --language {language}" if language else ""

    if walked > 0 and incremental and unchanged_files > 0:
        return EmptyWalkVerdict(0, (
            f"{TAG} incremental gate saw no change — {unchanged_files} file(s) "
            f"under {roots} are unchanged in {diff_range} and already indexed; "
            "nothing to do (run without --incremental to re-walk everything).",
        ))
    if walked > 0:
        # Files survive every rule yet nothing was dispatched: a state no
        # healthy walk reaches. Loud, non-zero — never a quiet success.
        return EmptyWalkVerdict(EXIT_NOTHING_INDEXED, (
            f"{TAG} {walked} file(s) under {roots} pass every exclusion rule "
            "but none was analyzed — nothing was indexed. Re-run with "
            "--verbose and report this output.",
        ))
    if candidates > 0:
        lines = [
            f"{TAG} every one of the {candidates} source file(s){lang_note} "
            f"under {roots} was EXCLUDED, so nothing was indexed.",
        ]
        for census in censuses:
            if census.candidates:
                lines.append(f"{TAG}   {census.root}: excluded by {_rules(census.excluded)}")
        lines.append(
            f"{TAG} these directory names are skipped anywhere below the project "
            "root (dependency, build and tool folders); move first-party source "
            "out of them, or analyze the folder that holds it."
        )
        return EmptyWalkVerdict(EXIT_NOTHING_INDEXED, tuple(lines))
    return EmptyWalkVerdict(0, (
        f"{TAG} no source file with a supported extension{lang_note} under "
        f"{roots} — nothing to index"
        + (f" (the walk looks for {extensions_hint})" if extensions_hint else "")
        + ". If this folder does hold code, check that the path is the "
        "project root.",
    ))


def report_empty_walk(stats: dict, *, incremental: bool, since_commit: Optional[str],
                      language: Optional[str], extensions_hint: str = "") -> int:
    """Print the verdict for a whole-tree walk that dispatched nothing (the
    analyzer stores ``stats['walk_censuses']`` only then) and return its exit
    code; ``0`` and silent when the walk dispatched files."""
    censuses = stats.get("walk_censuses")
    if censuses is None:
        return 0
    verdict = empty_walk_verdict(
        censuses, incremental=incremental,
        unchanged_files=int(stats.get("incremental_unchanged_files", 0) or 0),
        diff_range=f"{since_commit or 'HEAD~1'}..HEAD", language=language,
        extensions_hint=extensions_hint,
    )
    for line in verdict.lines:
        print(line, file=sys.stderr, flush=True)
    return verdict.exit_code


def root_incremental_gate(collection: Any, root: object, *,
                          extra_stamp: Optional[str] = None) -> bool:
    """Should ``--incremental`` filter ``root`` to its changed files?

    Only when the root already HAS indexed module rows. A never-indexed root
    (a fresh project, a graph lost to a collection recreate, an extra folder
    added since the last walk) owes EVERY file; the diff gate would drop all
    of them as "unchanged" and the walk would index nothing — exit 0, zero
    modules (the deferral retry and the launcher's Re-analyze both pass
    ``--incremental``). ``extra_stamp``: an extra path's ``project_source``
    stamp (its rows are told apart by it); ``None`` = the project root, where
    any row in the project's own module collection counts.

    Conservative on failure: no collection, or a probe that raised, keeps the
    gate (the pre-v0.2.105 behaviour) — "could not look" is not "empty".
    """
    if collection is None:
        return True
    try:
        if extra_stamp is not None:
            from weaviate.classes.query import Filter  # noqa: PLC0415 — only with a live collection

            found = collection.query.fetch_objects(
                filters=Filter.by_property("project_source").equal(extra_stamp), limit=1,
            ).objects
        else:
            found = collection.query.fetch_objects(limit=1).objects
    except Exception as exc:  # noqa: BLE001 — unknown is not empty
        print(f"{TAG} incremental: could not tell whether {root} was indexed before "
              f"({exc}); keeping the changed-files gate", file=sys.stderr, flush=True)
        return True
    if found:
        return True
    print(f"{TAG} incremental: {root} has no indexed rows yet — walking every file "
          "(the changed-files gate applies from the next run)", file=sys.stderr, flush=True)
    return False


__all__ = [
    "EXIT_NOTHING_INDEXED", "EmptyWalkVerdict", "TAG", "WalkCensus",
    "census_from_pairs", "empty_walk_verdict", "report_empty_walk", "root_incremental_gate",
]
