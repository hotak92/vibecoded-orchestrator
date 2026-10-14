# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Extractor-generation re-index: decide + trigger (v0.2.92).

The problem this closes
-----------------------
v0.2.92 fixed two code-graph EXTRACTORS:

* Python emitted **no** ``CodeAPI`` entity at all — every Python project on
  every install has ``CodeAPI == 0`` however many routes it declares;
* C# bound route attributes by proximity, collapsing two actions onto one row
  and dropping the leading ``/`` from every endpoint.

Both fixes change what the analyzer EXTRACTS from a file. Neither changes the
file. That distinction is the whole problem, because the analyzer's cheapest
gate is keyed on the FILE:

``CodeGraphAnalyzer._get_existing_module`` short-circuits a whole file when its
stored module row has the same ``file_hash`` and a current ``embed_revision``.
It runs at the top of every walker on EVERY walk — incremental *and* full. So
after an extractor-only fix, an ordinary re-analysis (of any flavour) walks
nothing, and the fixes are delivered to precisely nobody who already has a
graph. Measured on a real 766-entity tree: deleting every ``CodeAPI`` row and
re-running a FULL, non-incremental analyze restored **zero** of them.

The per-ENTITY content-hash gate (``_dedup_insert`` →
``_resolve_deferred_embed`` → ``guards.classify_row``) is the gate that does
the right thing — unchanged entity SKIPs with no embed, new/changed entity
embeds once — but it is downstream of the file gate and never gets to run.

The fix therefore has exactly two halves:

1. the analyzer gains ``--force-rewalk`` (env: ``VCT_CODEGRAPH_FORCE_REWALK``),
   which bypasses the per-FILE gate ONLY. Re-extraction is cheap; every write
   and every embed still goes through the unchanged per-entity hash gate, so a
   converged project re-embeds nothing.
2. this module decides WHICH projects owe that walk, and triggers it in the
   background from the per-project bundle update.

What this module is NOT
-----------------------
It is **not** a second hashing rule. It never decides whether an entity
changed — ``analyze_code_graph`` + ``vco_lib.codegraph_guards`` own that, and
this module's whole contribution is letting that decision run at all.

It also does **not** bump ``CODEGRAPH_EMBED_REVISION``. That constant governs
whether a stored VECTOR is stale; ours are all fine (same model, same chunking
— only the set of extracted entities changed). Bumping it would make every
content-identical row take the D1 STAMP path — one ``data.update`` per row,
project-wide — to achieve exactly the same delivery this module gets with zero
writes on unchanged rows. Same reasoning rules out bumping
``schema_versions.CODEGRAPH_COLLECTION_SCHEMA_VERSION``: the class SHAPE did
not change (``CodeAPI`` already existed and already had every property), so
there is no migration edge to write.

Invariants (project rules, all load-bearing here)
-------------------------------------------------
* **Never blocks an update.** Every entry point soft-fails to a
  :class:`ReindexResult` with a message; nothing raises at the caller.
* **No global timeout.** The walk is spawned detached via
  :func:`vco_lib.codegraph_resync.spawn_background_resync`, which owns the
  per-embed-request guard. We add no wall-clock deadline.
* **Resumable.** The walk is idempotent (converged rows hash-skip), and the
  completion stamp is written by the analyzer only at the END of a clean
  whole-repo force walk — so an interrupted run leaves the project still owed
  and the next update re-triggers it.
* **Safe default on the unknown.** A project with no recorded generation and
  an existing code graph is treated as OWED, never as done.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The generation ladder
# ---------------------------------------------------------------------------

#: Orchestrator versions at which an EXTRACTOR started producing entities it
#: previously did not produce (or produced differently) from UNCHANGED source.
#: Ordered oldest → newest. A project whose graph predates the newest entry
#: owes one ``--force-rewalk`` pass.
#:
#: ⚠️ MAINTAINER RULE — append here (never edit an existing entry) when, and
#: only when, a release changes what the analyzer EXTRACTS from source that
#: itself did not change. Do NOT append for:
#:   * a change to an entity's stored SHAPE  → that is
#:     ``schema_versions.CODEGRAPH_COLLECTION_SCHEMA_VERSION`` + a migration edge;
#:   * a change to how text becomes a VECTOR → that is
#:     ``analyze_code_graph.CODEGRAPH_EMBED_REVISION`` (+ the compatibility
#:     floor when the embedding space itself moved).
#:
#: 0.2.92 — Python ``CodeAPI`` emission added (was structurally absent);
#:          C# route-attribute binding corrected + leading-slash normalisation.
#: 0.2.93 — no extractor changes. Appended so the ladder keeps COVERING the
#:          release (the pinned delivery guard in test_v0292_wp5*_delivery
#:          requires the newest bump >= the package version); the cost is one
#:          incremental background re-walk per project on its next bundle
#:          update (unchanged entities are not re-embedded).
#: 0.2.94 — no extractor changes either, but the RECOVERY entry for the
#:          2026-09-09 field defect. The 0.2.92 and 0.2.93 re-walks were
#:          TRIGGERED on eight projects and every one of them died on
#:          `ModuleNotFoundError: No module named 'vco_lib'` (the detached child
#:          got a bare-PATH `python3`; see `vco_lib.python_exe`). Those runs
#:          wrote no completion stamp — correctly — but they DID advance each
#:          project's recorded manifest version to 0.2.93, and rule 3 of
#:          :func:`decide` ("prev_version at/past the newest bump ⇒ the graph
#:          was produced by an analyzer that already had the fixes") would then
#:          have stamped them as done WITHOUT ever walking. Appending 0.2.94 is
#:          how the ladder re-owes work it never actually delivered.
#:
#:          THE COST, stated plainly rather than understated: appending to the
#:          ladder moves the head, so a project stamped `0.2.93` is no longer
#:          `generation_is_current` either — EVERY project that has a code graph
#:          pays ONE forced re-extraction pass on its next bundle update, not
#:          just the eight that were stranded. What that pass costs is a re-walk
#:          of the tree with the analyzer's per-file skip gate bypassed; the
#:          EMBEDS stay content-hash gated, so entities whose content is
#:          unchanged are not re-embedded (no GPU-hours, no vector churn).
#:          Projects with no graph still short-circuit at rule 2 and pay
#:          nothing. That cost is accepted because the alternative is rule 3
#:          declaring the stranded projects finished — permanently, silently,
#:          and with no error anywhere. The asymmetry that decides it is the
#:          same one :func:`decide` is built on.
#: 0.2.102 — tree-sitter DEFINITIONS (``vco_lib.codegraph_defs``) became the
#:          default for TypeScript, JavaScript, Svelte ``<script>`` blocks,
#:          Rust, Go, Java, C#, C++, Ruby, Lua and shell; the regex extractors
#:          remain only as the fallback when a grammar cannot load. Output
#:          changed from unchanged source for every one of those languages
#:          (new symbols: TS generic functions / interfaces / annotated
#:          arrows, Go generics, C++ out-of-line constructors, shell
#:          next-line-brace functions …; dropped phantoms: Rust ``fn`` in
#:          string literals, ``#[tokio::test]`` / ``#[cfg(test)] mod`` code;
#:          corrected line ranges and therefore bodies; and — W6-G — every
#:          class/function of a Rust file loaded only through a test-gated
#:          ``#[cfg(test)] mod x;`` in ANOTHER file, which
#:          ``vco_lib.codegraph_rust_modules`` now resolves; such a file's
#:          module ``file_hash`` is salted, so this rung's re-walk also
#:          re-stamps it). W6-L, same rung: PowerShell (functions /
#:          filters, PowerShell 5 classes, enums and methods) and Protocol
#:          Buffers (messages and enums at any depth, services as classes,
#:          streaming rpcs as CodeAPI rows) gained tree-sitter definitions
#:          through OPTIONAL parsers VCO offers on demand; and a module row's
#:          ``file_hash`` is now salted while its file is extracted by the
#:          regex FALLBACK (grammar unavailable —
#:          ``codegraph_defs.regex_fallback_hash_salt``), so a parser
#:          installed LATER re-extracts that language's unchanged files on the
#:          next walk without another rung. W6-O, same rung: Rust module
#:          rows' ``import_names`` come from the parse (multi-line ``use``
#:          trees, out-of-line ``mod x;`` as ``"mod x"``, ``use`` inside
#:          test-gated code dropped) and shell rows' from real ``source``/``.``
#:          commands only (the regex stored comment words); the cross-reference
#:          pass resolves imports language-aware (``vco_lib.codegraph_imports``)
#:          and adds the Rust/TS/Svelte/shell edges it never found; CodeAPI rows
#:          gain ``is_test`` and a skipped row's missing ``file_path``/``is_test``
#:          is PATCHed (``codegraph_guards.heal_skipped_row``). W6-O's OWN
#:          delta, measured on this repository: 178 Rust and 133 shell module
#:          rows change content and re-embed once, and W6-O re-embeds nothing
#:          else (its Python change is edge resolution only). The W6-E, W6-G
#:          and W6-L output changes above re-embed further rows in the
#:          languages they name. The per-entity content-hash
#:          gate re-embeds only rows whose extracted text changed;
#:          ``CODEGRAPH_EMBED_REVISION`` is NOT bumped (model, chunking and
#:          embed text construction are unchanged). W6-CGD, same rung: the
#:          unchanged-file gate counts only the walked root's own rows (D-1),
#:          so this rung's forced re-walk also writes the project rows an
#:          identical, earlier-indexed extra-code-folder copy had shadowed
#:          (those files embed once under the project; nothing is deleted).
EXTRACTOR_GENERATION_BUMPS: tuple[str, ...] = ("0.2.92", "0.2.93", "0.2.94", "0.2.102")

#: Releases DELIBERATELY not added to the ladder, each with the evidence that
#: decided it. A release must appear in EXACTLY ONE of these two structures —
#: that is the conscious decision the delivery gate forces at every tag.
#:
#: Why this exists: the gate used to assert `newest_bump >= package_version`,
#: which is true on every release and says nothing about the extractor. The
#: 0.2.93 entry above is the proof — its own comment records that it was
#: appended ONLY to keep that assertion true. A proxy that fires every release
#: is answered by appending every release, and appending charges every project
#: with a graph one forced re-walk. Recording a non-bump costs users nothing.
EXTRACTOR_GENERATION_NON_BUMPS: dict[str, str] = {
    "0.2.95": (
        "No extraction-semantics change. `git diff v0.2.94..HEAD` across the "
        "extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) is "
        "14 lines in analyze_code_graph.py, all deferral bookkeeping "
        "(clear_backend_deferrals -> record_successful_walk). _CHUNKER_REVISION "
        "unchanged at v0.2.92.1; CODEGRAPH_EMBED_REVISION unchanged at 1; "
        "CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7. Projects stranded "
        "by the 2026-09-09 defect still get their owed walk: decide() rule 4 "
        "fires on crosses_version_boundary(prev, 0.2.95, 0.2.94)."
    ),
    "0.2.101": (
        "No extraction-semantics change. `git diff v0.2.100..HEAD` across the "
        "extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py): "
        "analyze_code_graph.py and every codegraph_* module are UNCHANGED "
        "(0 lines). schema_versions.py +10/-1 is the launcher-DB "
        "migration-048 ledger entry (chat-model image-capability column) — "
        "a launcher DB concern, not an extraction rule (same shape as the "
        "0.2.96 entry's migration-046 precedent). chunking.py's +137 lines "
        "are the v0.2.101 QUERY-PREFIX machinery (MODEL_QUERY_PREFIXES + "
        "query_prefix_for_model) — query-side embedding prefixes applied at "
        "SEARCH time; stored chunks, entity discovery, naming, hashing and "
        "truncation are untouched, so a forced re-walk would reproduce "
        "byte-identical rows. _CHUNKER_REVISION unchanged at v0.2.92.1; "
        "CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7. The cycle's "
        "code-graph work was read-path exactness (weaviate_exact_match, "
        "calls-as-references) — retrieval correctness over the EXISTING "
        "graph, gated on its own mechanisms, not extraction."
    ),
    "0.2.96": (
        "No extraction-semantics change. `git diff v0.2.95..HEAD` across the "
        "same extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) "
        "touches ONE file: schema_versions.py, +10/-1, and the change is the "
        "launcher.db migration-046 ledger entry (chat_model_context.max_output "
        "CHECK widened > 0 -> >= 0). That is a launcher DB constraint, not an "
        "extraction rule — nothing about how an entity is found, named, "
        "hashed, chunked or truncated moved. _CHUNKER_REVISION unchanged at "
        "v0.2.92.1; CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7. "
        "The cycle's code-graph work was the M-2 EXPOSURE repair, which "
        "re-EMBEDS rows whose vectors came from a silently-truncating "
        "code-embed image; it demotes those rows to a vectorless sentinel so "
        "the existing embed_revision gate re-embeds them. That is an embedding "
        "concern gated on its own mechanism, and appending here instead would "
        "charge EVERY project with a graph one forced re-extraction to fix a "
        "population identified by evidence."
    ),
    "0.2.97": (
        "No extraction-semantics change. `git diff v0.2.96..HEAD` across the "
        "same extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) "
        "touches ONE file: analyze_code_graph.py, +2/-2 — a printed recovery "
        "command replaced (the nonexistent `project_init drop-collection` verb "
        "-> a real single-collection DELETE). codegraph_resync.py (+3) only "
        "scrubs the install relaunch record from its detached child's env. "
        "_CHUNKER_REVISION unchanged at v0.2.92.1; CODEGRAPH_EMBED_REVISION "
        "unchanged at 1; CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7."
    ),
    "0.2.98": (
        "No extraction-semantics change. `git diff v0.2.97..HEAD` across the "
        "same extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) is "
        "EMPTY \u2014 zero files, zero lines; templates/scripts/"
        "analyze_code_graph.py is untouched, so CODEGRAPH_EMBED_REVISION "
        "cannot have moved. The three changed files nearest the surface "
        "carry no extraction rule between them: codegraph_deferrals.py, "
        "+4/-2, swaps the OpenAI recovery hint in _service_hint for the "
        "`vct can-read --key openai_api_key` / `vct set --shared` pair (the "
        "text it replaced pointed at a launcher page that no longer exists); "
        "weaviate_mcp/server.py, +9/-2, corrects the same OpenAI-slot wording "
        "in a docstring and in one backfill_embeddings error string; "
        "weaviate_mcp/embeddings.py, +3/-2, is a comment plus a resolution-"
        "scope change \u2014 OPENAI_API_KEY now comes from VCO's own shared "
        "slot only, never a project's. Nothing about how an entity is found, "
        "named, hashed, chunked or truncated moved. The rest of the release "
        "is the model gateway's context-table delivery (the [1m] window ids "
        "and the boot convergence), the qwen-vendor agent lane, the "
        "read-only git step-aside, and the usage-surfaces removal. "
        "_CHUNKER_REVISION unchanged at v0.2.92.1; "
        "CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7."
    ),
    "0.2.99": (
        "No extraction-semantics change. `git diff v0.2.98..HEAD` across the "
        "same extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) is "
        "EMPTY — the release touches exactly two files, both in the "
        "launcher: launcher/src/lib/gateway-freshness.ts (the "
        "scheduleStartupCheck Illegal-invocation fix — frontend timers, "
        "no extraction surface) and launcher/src-tauri/src/commands/"
        "self_update.rs (the forced tag refspec + fetch exit-status "
        "diagnostics — the update path, no extraction surface), plus "
        "their tests and the release bookkeeping (CHANGELOG, version pins, "
        "this ladder entry). _CHUNKER_REVISION unchanged at v0.2.92.1; "
        "CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7."
    ),
    "0.2.100": (
        "No extraction-semantics change. `git diff v0.2.99..HEAD` across the "
        "same extractor surface (analyze_code_graph.py, codegraph_{guards,"
        "content_hash,entities,lang,calls,references,schema,naming}, "
        "weaviate_mcp/{chunking,code_truncation}.py, schema_versions.py) "
        "touches ONE file, templates/scripts/analyze_code_graph.py, and only "
        "its row-REMOVAL side: an incremental walk now deletes the rows of "
        "files deleted or renamed away since the lower bound (the logic moved "
        "to vco_lib/codegraph_deleted_files.py, with _filter_changed_files), "
        "the new --as-extra-path flag makes every delete-by-source path leave "
        "an unprovable (legacy, empty project_source) row alone, and the "
        "deleted-file prune reports exit 5 on failure. How an entity is found, "
        "named, hashed, chunked or truncated is unchanged; no new row content "
        "would come from a re-walk, so a forced one would cost every user a "
        "walk for nothing. The code-summary batching (generate-code-summary.py) "
        "is outside the extractor surface. _CHUNKER_REVISION unchanged at "
        "v0.2.92.1; CODEGRAPH_COLLECTION_SCHEMA_VERSION unchanged at 7."
    ),
}

#: The newest generation a freshly-built graph satisfies.
CURRENT_EXTRACTOR_GENERATION: str = EXTRACTOR_GENERATION_BUMPS[-1]

#: One-line HUMAN reason per ladder rung — the text shown to the user when
#: that rung's re-index is launched (``describe_rung`` prefixes the version).
#: Append an entry beside every ladder append: a rung with no entry makes
#: the user-facing message fall back to the generic wording, which is worse
#: than naming what actually triggered the walk. Pinned to stay in lockstep
#: with the ladder by ``tests/test_v02103_rung_reason.py``.
EXTRACTOR_GENERATION_RUNG_REASONS: dict[str, str] = {
    "0.2.92": (
        "added Python API extraction and fixed C# route attribution"
    ),
    "0.2.93": (
        "coverage re-walk for the extractor fixes (no extractor change that release)"
    ),
    "0.2.94": (
        "recovery re-walk for background walks that never ran"
    ),
    "0.2.102": (
        "made tree-sitter definitions the default extractor for most "
        "languages — new symbols, corrected line ranges and bodies, Rust "
        "test-gated modules, PowerShell and Protobuf support, "
        "language-aware imports"
    ),
}

#: Env var the analyzer honours as an alternative to ``--force-rewalk``. It
#: exists because the background trigger reaches the analyzer through TWO
#: process hops (``spawn_background_resync`` → ``codegraph_resync --run-resync``
#: → analyzer argv) and env crosses both for free. The CLI flag is the primary,
#: documented surface; this is its transport.
FORCE_REWALK_ENV = "VCT_CODEGRAPH_FORCE_REWALK"

#: Per-project completion record, relative to the project root. Lives under
#: ``.claude/state/`` because that tree is explicitly EXCLUDED from code-graph
#: walks (v0.2.73 read-amplification cleanup), so the stamp can never index
#: itself into the graph it describes.
STAMP_REL = Path(".claude") / "state" / "codegraph-extractor-generation.json"

#: Weaviate class suffixes that prove a project HAS a code graph. ``CodeAPI``
#: is deliberately NOT in this set: its absence is the very symptom we are
#: repairing, so requiring it would make the detector answer "no graph" for
#: exactly the projects that need the walk.
_GRAPH_EVIDENCE_SUFFIXES = ("CodeFunction", "CodeModule")


# ---------------------------------------------------------------------------
# The shared "was this built by an older version of us" primitive
# ---------------------------------------------------------------------------

def parse_semver(version: str) -> "tuple[int, int, int] | None":
    """Parse ``"X.Y.Z"`` into ``(major, minor, patch)``; ``None`` if malformed.

    Deliberately does not pull in ``packaging`` — orchestrator version strings
    are plain semver with no pre-release tags. ``vco_lib.project_init``'s
    ``_parse_semver`` delegates here so there is ONE parser.

    Relation to the version SSOT (v0.2.100): the strict comparator home is
    :mod:`vco_lib.version_compare` (``X.Y.Z`` only, owner ruling Q7, raising
    ``VersionParseError``). This function keeps its ``None``-returning
    contract because its callers are boundary checks where "unparseable" must
    mean "no crossing proven", and it does not accept the ``v`` prefix. Its
    tuple ORDERING agrees with the SSOT on every unprefixed ``order`` row of
    ``tests/fixtures/version_order_cases.json`` and it rejects the suffixed /
    four-part / two-part ``reject`` rows — both pinned by
    ``tests/test_v02100_version_order.py``.
    """
    if not isinstance(version, str):
        return None
    parts = version.split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def crosses_version_boundary(prev_version: str, running_version: str,
                             bump_version: str) -> bool:
    """True iff an upgrade ``prev → running`` crosses ``bump`` (``prev < bump <= running``).

    The ONE way this codebase asks "was this artifact produced by an older
    version of us". Extracted from ``project_init._crosses_chunker_boundary``
    (v0.2.47 chunker-preset boundary), which now calls it, so the chunker
    boundary and the extractor boundary cannot drift apart.

    Malformed input on any of the three → ``False`` (the caller's other
    signals decide; a version we cannot parse must never be read as proof of
    a crossing).
    """
    prev = parse_semver(prev_version)
    running = parse_semver(running_version)
    bump = parse_semver(bump_version)
    if prev is None or running is None or bump is None:
        return False
    return prev < bump <= running


def generation_is_current(generation: Optional[str],
                          bumps: Sequence[str] = EXTRACTOR_GENERATION_BUMPS) -> bool:
    """True iff ``generation`` is at least the newest bump in ``bumps``.

    ``None`` / malformed → False (unknown is never "current").
    """
    if not bumps:
        return True
    newest = parse_semver(bumps[-1])
    have = parse_semver(generation or "")
    if newest is None:
        return True
    if have is None:
        return False
    return have >= newest


def describe_rung(bump: object) -> "Optional[str]":
    """Human reason for the rung that triggered a re-index.

    Returns ``"v<bump> <one-line reason>"`` (e.g. ``"v0.2.102 made
    tree-sitter definitions the default extractor…"``) — the text the
    bundle update shows the user when it launches a background re-index,
    so the message names the change that ACTUALLY triggered the walk
    instead of a hard-coded version's reason. ``None`` when the rung is
    unknown or missing from :data:`EXTRACTOR_GENERATION_RUNG_REASONS`;
    callers fall back to generic wording rather than guessing.
    """
    if not isinstance(bump, str):
        return None
    reason = EXTRACTOR_GENERATION_RUNG_REASONS.get(bump)
    if reason is None:
        return None
    return f"v{bump} {reason}"


# ---------------------------------------------------------------------------
# The DECISION (pure — no I/O, fully unit-testable)
# ---------------------------------------------------------------------------

#: Verdict reason codes. Stable strings: they are logged, and the tests assert
#: on them rather than on prose.
REASON_STAMP_CURRENT = "stamp_current"
REASON_NO_GRAPH = "no_graph"
REASON_VERSION_AT_OR_PAST_BUMP = "version_at_or_past_bump"
REASON_CROSSES_BUMP = "crosses_bump"
REASON_UNKNOWN_GENERATION = "unknown_generation"


@dataclass(frozen=True)
class Verdict:
    """Outcome of :func:`decide`.

    Attributes:
        needs_reindex: run the force-rewalk?
        reason: one of the ``REASON_*`` constants.
        stamp_now: write the completion stamp WITHOUT walking. True only when
            we can positively conclude the project's graph already satisfies
            the current generation (or has no graph to fix), so future updates
            stop asking. Never True together with ``needs_reindex``.
        generation: the generation the verdict is about.
    """

    needs_reindex: bool
    reason: str
    stamp_now: bool = False
    generation: str = CURRENT_EXTRACTOR_GENERATION
    #: The rung that triggered this verdict — set only when ``reason`` is
    #: ``REASON_CROSSES_BUMP`` (the first bump the upgrade crosses).
    #: :func:`describe_rung` turns it into the user-facing reason text.
    bump: Optional[str] = None


def decide(
    *,
    prev_version: str,
    running_version: str,
    stamp_generation: Optional[str],
    graph_exists: Optional[bool],
    bumps: Sequence[str] = EXTRACTOR_GENERATION_BUMPS,
) -> Verdict:
    """Decide whether this project owes an extractor-generation re-index.

    PURE. Every input is a value the caller resolved; no file, network or
    subprocess access happens here. :func:`plan` is the I/O half.

    Args:
        prev_version: ``vco_version`` recorded in the project's PRIOR
            ``.claude/.vco-manifest.json`` (``""`` when there was none).
        running_version: the orchestrator version installing right now.
        stamp_generation: generation from the project's completion stamp
            (``None`` when absent/unreadable).
        graph_exists: ``True`` / ``False`` / ``None`` (could not check —
            Weaviate unreachable). TRI-STATE on purpose: "could not check" is
            not "absent" (the F-1 lesson, restated).
        bumps: the generation ladder (injectable for tests).

    Decision order, and why:

    1. **Stamp says current** → done. The stamp is written only after a clean
       whole-repo force walk, so it is the strongest evidence available.
    2. **Positively no code graph** → nothing to repair. Whatever builds the
       graph next uses the current analyzer, so stamp and stop asking. (This
       is also what stops a first-install from spawning a surprise full build.)
    3. **``prev_version`` recorded and at/past the newest bump** → the graph
       was produced by an analyzer that already had the fixes. Stamp, stop.
    4. **``prev_version`` crosses a bump** → the migration case. OWED.
    5. **Anything else** — no manifest version, an unparseable one, a
       ``running_version`` we cannot parse — is UNKNOWN, and unknown with a
       graph present (or unprovable) is OWED. Re-walking an
       already-current project costs one cheap re-extraction pass and zero
       embeds; skipping a stale one leaves the user permanently broken with no
       error. The asymmetry decides it.
    """
    if generation_is_current(stamp_generation, bumps):
        return Verdict(False, REASON_STAMP_CURRENT, stamp_now=False)

    if graph_exists is False:
        return Verdict(False, REASON_NO_GRAPH, stamp_now=True)

    newest = bumps[-1] if bumps else CURRENT_EXTRACTOR_GENERATION
    prev = parse_semver(prev_version or "")
    newest_parsed = parse_semver(newest)
    if prev is not None and newest_parsed is not None and prev >= newest_parsed:
        return Verdict(False, REASON_VERSION_AT_OR_PAST_BUMP, stamp_now=True,
                       generation=newest)

    for bump in bumps:
        if crosses_version_boundary(prev_version or "", running_version or "", bump):
            return Verdict(True, REASON_CROSSES_BUMP, generation=newest, bump=bump)

    return Verdict(True, REASON_UNKNOWN_GENERATION, generation=newest)


# ---------------------------------------------------------------------------
# Analyzer-side pure decisions (kept HERE, not in the 7k-line template script —
# `tests/test_analyze_code_graph_ratchet.py` is a downward-only ratchet and
# CLAUDE.md's modularity rule sends >50-line additions out of mega-files.
# The analyzer keeps only the I/O seams that call these.)
# ---------------------------------------------------------------------------

def resolve_force_rewalk(cli_value: bool,
                         env: "Optional[dict]" = None) -> bool:
    """Resolve ``--force-rewalk`` against its env transport. ONE resolver.

    ``--force-rewalk`` is the primary, documented surface;
    :data:`FORCE_REWALK_ENV` is how the detached background trigger reaches the
    analyzer, because it must cross two spawn hops whose argv the trigger does
    not construct (this module → ``codegraph_resync.spawn_background_resync``
    → the ``--run-resync`` driver → the analyzer). Either being set turns it
    on, so the two surfaces cannot disagree about what "on" means.
    """
    if cli_value:
        return True
    source = os.environ if env is None else env
    return str(source.get(FORCE_REWALK_ENV, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------------------
# Persistently failing inserts (v0.2.102 W6-U, ship-gate finding #1)
#
# A walk with ANY insert error used to be refused the stamp, so a project with
# one row Weaviate rejects every time (a 422 on an oversized row is enough)
# stayed owed and re-walked on EVERY bundle update, forever, silently. The
# rule now: a failing set is ACCEPTED (the stamp is written, the set recorded
# beside it) only when the previous forced walk recorded the SAME fingerprint
# for the SAME generation — a failure seen twice in a row is persistent, not a
# transient outage. The first sighting is held back as ``pending_failing_set``
# (the old ``generation`` is kept, so the project stays owed); an accepted set
# is surfaced by the ``codegraph_insert_failures`` doctor probe, so the
# accepted state is visible rather than silent.
# ---------------------------------------------------------------------------

#: Stamp keys for the two failing-set records.
PENDING_FAILING_SET_KEY = "pending_failing_set"
ACCEPTED_FAILING_SET_KEY = "accepted_failing_set"

#: How many failing rows the resync log names before "… and N more".
FAILING_ROWS_LOG_CAP = 20

#: :func:`failing_set_verdict` reason codes (stable strings — logged, tested).
FAILING_SET_STABLE = "stable"
FAILING_SET_FIRST_SIGHTING = "first_sighting"
FAILING_SET_CHANGED = "changed"
FAILING_SET_UNATTRIBUTED = "unattributed"

_ROW_FIELDS = ("file", "collection", "uuid", "reason")


def _attributed_insert_error(exc: object) -> object:
    """The analyzer's ``_DedupInsertError`` on ``exc``'s cause chain (duck
    typed on ``collection_name`` + ``uuid``), or ``None``. Bounded walk, the
    same cap as the analyzer's own chain search."""
    cur = exc
    for _ in range(8):
        if cur is None:
            return None
        if getattr(cur, "collection_name", None) and getattr(cur, "uuid", None):
            return cur
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return None


def insert_failure_reason(exc: object) -> str:
    """Stable reason for an insert failure: the ORIGINAL exception's type, plus
    its HTTP status when it carries one (``UnexpectedStatusCodeError:422``).

    Never the free text: messages embed request ids, timings and payload
    excerpts that differ run to run, and a reason that changes on every walk
    would make every failing set look "changed" — never accepted, the very loop
    this closes.
    """
    attributed = _attributed_insert_error(exc)
    original = getattr(attributed, "original", None) if attributed is not None else None
    target = original if original is not None else exc
    reason = type(target).__name__
    for attr in ("status_code", "status"):
        code = getattr(target, attr, None)
        if isinstance(code, int) and not isinstance(code, bool):
            return f"{reason}:{code}"
    return reason


def insert_failure_row(file_rel: object, exc: object) -> dict:
    """One failing-insert row: file, collection, the row's deterministic uuid,
    and :func:`insert_failure_reason`. ``collection`` / ``uuid`` are ``""``
    when the exception carries no attribution — such a row makes the whole set
    unattributed, and an unattributed set is never accepted."""
    attributed = _attributed_insert_error(exc)
    return {
        "file": str(file_rel or "").replace("\\", "/"),
        "collection": str(getattr(attributed, "collection_name", "") or ""),
        "uuid": str(getattr(attributed, "uuid", "") or ""),
        "reason": insert_failure_reason(exc),
    }


def _row_is_attributed(row: object) -> bool:
    return isinstance(row, dict) and all(
        isinstance(row.get(k), str) and row.get(k, "").strip() for k in _ROW_FIELDS)


def failing_set_fingerprint(rows: Sequence[dict]) -> str:
    """Order-independent sha256 over the failing rows' identifying fields."""
    import hashlib  # noqa: PLC0415

    canon = sorted(
        json.dumps([str((r or {}).get(k, "")) for k in _ROW_FIELDS], ensure_ascii=False)
        for r in rows
    )
    return hashlib.sha256("\n".join(canon).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FailingSetVerdict:
    """Outcome of :func:`failing_set_verdict`."""

    stable: bool
    reason: str
    fingerprint: Optional[str] = None


def failing_set_verdict(rows: Sequence[dict], prior: object,
                        generation: str = CURRENT_EXTRACTOR_GENERATION,
                        *, insert_errors: Optional[int] = None) -> FailingSetVerdict:
    """PURE: is this walk's failing set the SAME one the previous forced walk
    recorded, for the same generation?

    * never stable on the first sighting (``prior`` absent / malformed);
    * never stable when any row is unattributed, or when ``insert_errors``
      counted more failures than there are rows (a failure the analyzer could
      not name is not a set we can claim to know);
    * stable only when ``prior`` carries this fingerprint AND this generation.
    """
    rows = list(rows or [])
    if not rows or not all(_row_is_attributed(r) for r in rows) or (
            insert_errors is not None and int(insert_errors) != len(rows)):
        return FailingSetVerdict(False, FAILING_SET_UNATTRIBUTED, None)
    fp = failing_set_fingerprint(rows)
    if not isinstance(prior, dict) or not prior.get("fingerprint"):
        return FailingSetVerdict(False, FAILING_SET_FIRST_SIGHTING, fp)
    if prior.get("fingerprint") == fp and prior.get("generation") == generation:
        return FailingSetVerdict(True, FAILING_SET_STABLE, fp)
    return FailingSetVerdict(False, FAILING_SET_CHANGED, fp)


def walk_certifies_generation(
    force_rewalk: bool,
    *,
    only_file: object = None,
    only_files_from: object = None,
    language: "Optional[str]" = None,
    insert_errors: int = 0,
    files_analyzed: int = 0,
    failing_set_stable: bool = False,
) -> bool:
    """PURE: may this analyzer run's outcome certify the extractor generation?

    Only a run that (a) actually bypassed the per-file gate, (b) covered the
    WHOLE repo (not ``--only-file`` / ``--only-files-from``), (c) covered EVERY
    language (not ``--language``), (d) hit zero insert errors — or a failing
    set :func:`failing_set_verdict` found stable (``failing_set_stable``) — and
    (e) analyzed at least one file can claim that every file was re-extracted
    by the current producers.

    Anything else leaves the stamp absent, which :func:`decide` reads as OWED.
    That direction is deliberate: a missing stamp costs one more cheap
    re-extraction pass; a wrong stamp costs the user a permanently incomplete
    code graph with no error anywhere.
    """
    if not force_rewalk:
        return False
    if only_file is not None or only_files_from is not None:
        return False
    if language:
        return False
    if int(insert_errors or 0) > 0 and not failing_set_stable:
        return False
    return int(files_analyzed or 0) > 0


def _failing_set_record(rows: Sequence[dict], fingerprint: str, generation: str,
                        *, project: str, first_seen: str) -> dict:
    return {
        "fingerprint": fingerprint,
        "generation": generation,
        "rows": [{k: r.get(k, "") for k in _ROW_FIELDS} for r in rows],
        "count": len(rows),
        "first_seen": first_seen,
        "project": project,
    }


def _report_failing_rows(rows: Sequence[dict], insert_errors: int, emit) -> None:
    emit(f"   Insert failures this walk: {insert_errors}")
    shown = list(rows)[:FAILING_ROWS_LOG_CAP]
    for r in shown:
        emit(f"     - {r.get('file') or '?'} [{r.get('collection') or '?'}] "
             f"{r.get('reason') or '?'}")
    more = max(int(insert_errors or 0), len(rows)) - len(shown)
    if more > 0:
        emit(f"     … and {more} more")


def stamp_after_walk(
    repo_path: Path,
    *,
    force_rewalk: bool,
    only_file: object = None,
    only_files_from: object = None,
    language: "Optional[str]" = None,
    insert_errors: int = 0,
    files_analyzed: int = 0,
    project: str = "",
    failing_rows: "Optional[Sequence[dict]]" = None,
    emit=None,
) -> Optional[str]:
    """Write the completion stamp iff this run certifies it. Soft-fail always.

    Returns the generation recorded, or ``None`` when nothing was written (not
    certified, held back, or the write failed). The analyzer calls this AFTER
    the walk returns — never earlier — so a run that dies mid-walk leaves no
    stamp and the project stays owed.

    A whole-repo force walk WITH insert failures names each failing file +
    collection + reason through ``emit`` (default: ``print`` — the analyzer's
    stdout is the resync log), capped at :data:`FAILING_ROWS_LOG_CAP`, and says
    whether the stamp was held back or recorded:

    * first sighting / changed set → ``pending_failing_set`` written, the
      prior ``generation`` KEPT (the project stays owed); a prior generation
      that rested on a DIFFERENT accepted set is dropped, since that
      acceptance no longer describes the graph;
    * same set as the previous forced walk → stamp written with
      ``accepted_failing_set``;
    * unattributed → nothing recorded, never stamped.

    A clean walk rewrites the stamp from scratch, dropping both records.
    """
    say = emit if emit is not None else (lambda line: print(line, flush=True))
    try:
        if not walk_certifies_generation(
            force_rewalk, only_file=only_file, only_files_from=only_files_from,
            language=language, insert_errors=0, files_analyzed=files_analyzed,
        ):
            return None
        errors = int(insert_errors or 0)
        if errors <= 0:
            if write_stamp(repo_path, CURRENT_EXTRACTOR_GENERATION,
                           note="force-rewalk completed"):
                return CURRENT_EXTRACTOR_GENERATION
            return None
        rows = list(failing_rows or [])
        payload = read_stamp_payload(repo_path) or {}
        prior = payload.get(PENDING_FAILING_SET_KEY) or payload.get(ACCEPTED_FAILING_SET_KEY)
        verdict = failing_set_verdict(rows, prior, CURRENT_EXTRACTOR_GENERATION,
                                      insert_errors=errors)
        _report_failing_rows(rows, errors, say)
        if verdict.reason == FAILING_SET_UNATTRIBUTED:
            say("   Extractor generation: stamp held back — the failing inserts "
                 "could not all be attributed to a row; the project stays owed")
            return None
        assert verdict.fingerprint is not None
        first_seen = (prior.get("first_seen") if verdict.stable and isinstance(prior, dict)
                      else "") or _utc_now()
        record = _failing_set_record(rows, verdict.fingerprint,
                                     CURRENT_EXTRACTOR_GENERATION,
                                     project=project, first_seen=first_seen)
        if verdict.stable and walk_certifies_generation(
            force_rewalk, only_file=only_file, only_files_from=only_files_from,
            language=language, insert_errors=errors, files_analyzed=files_analyzed,
            failing_set_stable=True,
        ):
            if write_stamp(repo_path, CURRENT_EXTRACTOR_GENERATION,
                           note="force-rewalk completed with an accepted failing set",
                           extra={ACCEPTED_FAILING_SET_KEY: record}):
                say(f"   Extractor generation: recorded — the same {errors} insert "
                     f"failure(s) as the previous forced walk are accepted "
                     f"(`vco doctor` lists them)")
                return CURRENT_EXTRACTOR_GENERATION
            return None
        held = {k: v for k, v in payload.items()
                if k not in (PENDING_FAILING_SET_KEY, ACCEPTED_FAILING_SET_KEY)}
        if payload.get(ACCEPTED_FAILING_SET_KEY):
            held.pop("generation", None)  # its acceptance no longer holds
        held[PENDING_FAILING_SET_KEY] = record
        held["written_at"] = _utc_now()
        held["written_by"] = "vco_lib.codegraph_extractor_generation"
        _write_stamp_payload(repo_path, held)
        say(f"   Extractor generation: stamp held back ({verdict.reason}) — "
             f"recorded as pending; the same set on the next forced walk is accepted")
    except Exception as exc:  # noqa: BLE001 — a stamp never gates a build
        logger.warning("extractor-generation stamp not written for %s: %s",
                       repo_path, exc)
    return None


# ---------------------------------------------------------------------------
# Stamp I/O (thin; every function soft-fails)
# ---------------------------------------------------------------------------

def stamp_path(folder: Path) -> Path:
    """Absolute path of ``folder``'s completion stamp."""
    return Path(folder) / STAMP_REL


def read_stamp_payload(folder: Path) -> Optional[dict]:
    """The whole stamp object; ``None`` when absent/unreadable/not an object."""
    try:
        raw = stamp_path(folder).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — absent / unreadable ⇒ unknown ⇒ owed
        return None
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def read_stamp_generation(folder: Path) -> Optional[str]:
    """Generation recorded for ``folder``; ``None`` when absent/unreadable.

    An unreadable stamp reads as ABSENT (→ owed), never as current.
    """
    payload = read_stamp_payload(folder)
    if payload is None:
        return None
    gen = payload.get("generation")
    return gen if isinstance(gen, str) and gen.strip() else None


def write_stamp(folder: Path, generation: str = CURRENT_EXTRACTOR_GENERATION,
                *, note: str = "", extra: Optional[dict] = None) -> bool:
    """Record that ``folder``'s code graph satisfies ``generation``.

    Atomic (``vco_lib.atomic.atomic_write_json``) so a crash mid-write can
    never leave a truncated stamp that parses as a DIFFERENT generation.
    Returns True on success; False on any failure (a failed stamp only means
    the project is asked again next update — never a corrupt state).
    A clean write carries no failing-set record: ``extra`` (the accepted set)
    is the only way one lands beside a generation.
    """
    payload = {
        "generation": generation,
        "written_at": _utc_now(),
        "written_by": "vco_lib.codegraph_extractor_generation",
    }
    if note:
        payload["note"] = note
    if extra:
        payload.update(extra)
    return _write_stamp_payload(folder, payload)


def _write_stamp_payload(folder: Path, payload: dict) -> bool:
    """Atomically write ``payload`` as ``folder``'s stamp. Soft-fail → False."""
    target = stamp_path(folder)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_json

        atomic_write_json(target, payload, sort_keys=True)
        return True
    except Exception as exc:  # noqa: BLE001 — a stamp failure never blocks
        logger.warning("extractor-generation stamp write failed for %s: %s",
                       folder, exc)
        return False


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Graph-existence probe (REUSES project_init.probe_classes_exist — the ONE
# tri-state schema probe; this module adds no second one)
# ---------------------------------------------------------------------------

def code_graph_exists(project_name: str,
                      weaviate_url: Optional[str] = None,
                      *, probe=None) -> Optional[bool]:
    """Tri-state: does ``project_name`` have code-graph classes in Weaviate?

    ``True`` / ``False`` / ``None`` (could not check). Delegates the actual
    schema read to :func:`vco_lib.project_init.probe_classes_exist` (ONE
    ``GET /v1/schema``, case-insensitive, tri-state) and the prefix derivation
    to :func:`vco_lib.codegraph_resync._collection_prefix` (which itself wraps
    the ``project_naming`` SSOT). No new sanitizer, no new probe.

    ``probe`` is the injection seam for tests; production leaves it ``None``.
    """
    if not project_name:
        return None
    try:
        from vco_lib.codegraph_resync import _collection_prefix

        prefix = _collection_prefix(project_name)
    except Exception as exc:  # noqa: BLE001 — partial install ⇒ unknown
        logger.warning("extractor-generation: prefix resolver unavailable: %s", exc)
        return None
    if not prefix:
        return None

    names = [f"{prefix}_{suffix}" for suffix in _GRAPH_EVIDENCE_SUFFIXES]
    if probe is None:
        try:
            from vco_lib.project_init import probe_classes_exist as probe
        except Exception as exc:  # noqa: BLE001
            logger.warning("extractor-generation: class probe unavailable: %s", exc)
            return None
    try:
        results = probe(names, weaviate_url)
    except Exception as exc:  # noqa: BLE001 — could not check ≠ absent
        logger.warning("extractor-generation: class probe raised: %s", exc)
        return None

    states = [results.get(n) for n in names]
    if any(s is True for s in states):
        return True
    if all(s is False for s in states):
        return False
    return None


# ---------------------------------------------------------------------------
# PLAN — the decision with its inputs resolved (thin I/O over `decide`)
# ---------------------------------------------------------------------------

@dataclass
class ReindexResult:
    """What :func:`ensure_extractor_generation` did. Never an exception."""

    status: str                     # skipped | stamped | launched | deferred | failed
    reason: str = ""
    message: str = ""
    generation: str = CURRENT_EXTRACTOR_GENERATION
    #: Human reason for the rung that triggered the re-index
    #: (:func:`describe_rung`), or a generic "may predate" line when the
    #: triggering rung is unknown. What the bundle update's user-facing
    #: warning shows — never hard-coded to one release's reason.
    reason_text: str = ""
    pid: Optional[int] = None
    deferral: object = None         # a DeferralEntry when status == "deferred"
    warnings: list = field(default_factory=list)


def plan(
    folder: Path,
    *,
    prev_version: str,
    running_version: str,
    project_name: str,
    weaviate_url: Optional[str] = None,
    graph_exists: Optional[bool] = None,
    probe=None,
) -> Verdict:
    """Resolve :func:`decide`'s inputs from disk + Weaviate, then decide.

    ``graph_exists`` may be supplied by a caller that already knows (or by a
    test); otherwise it is probed. The probe runs ONLY when it can change the
    answer — a stamp that is already current short-circuits before any I/O.
    """
    stamp_generation = read_stamp_generation(folder)
    if generation_is_current(stamp_generation):
        return Verdict(False, REASON_STAMP_CURRENT)
    if graph_exists is None:
        graph_exists = code_graph_exists(project_name, weaviate_url, probe=probe)
    return decide(
        prev_version=prev_version,
        running_version=running_version,
        stamp_generation=stamp_generation,
        graph_exists=graph_exists,
    )


# ---------------------------------------------------------------------------
# TRIGGER — background, non-blocking, soft-fail
# ---------------------------------------------------------------------------

#: Marker that makes :func:`_retarget_deferral` idempotent (the ledger is
#: re-emitted on every update while the condition holds).
_NOTE_MARKER = "NOTE (extractor generation"


def _retarget_deferral(deferral, generation: str):
    """Make a borrowed resync deferral's resume command actually fix OUR problem.

    ``spawn_background_resync`` hands back a ``codegraph_embed_resync_pending``
    entry whose ``command_to_apply`` is a PLAIN analyzer invocation. Run as
    written it would deliver nothing here — a plain walk hits the per-file gate
    and re-extracts no file (that is the entire defect). So the resume command
    gets ``--force-rewalk`` appended and the text says which repair is owed.

    The ``condition_id`` is deliberately left alone: it is the registered
    condition in ``vco_lib/deferral_conditions.toml``, both entries mean "the
    code-embed backend was down so an owed code-graph walk did not run", and
    minting an unregistered id would fail the registry-completeness gate.

    Returns the entry unchanged on ANY doubt (``None``, an unexpected shape, a
    command we cannot safely amend) — a slightly-wrong remediation string is
    much better than a dropped deferral.
    """
    if deferral is None:
        return None
    try:
        import dataclasses

        cmd = getattr(deferral, "command_to_apply", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return deferral
        if _NOTE_MARKER in cmd:
            return deferral            # already retargeted — idempotent
        if "--force-rewalk" not in cmd:
            cmd = cmd.rstrip() + " --force-rewalk"
        note = (
            f"\n\n# {_NOTE_MARKER} {generation}): --force-rewalk is REQUIRED "
            "here. Without it the analyzer's per-file gate skips every "
            "unchanged file, so an extractor fix (v0.2.92 Python CodeAPI "
            "emission + C# route attribution) reaches nothing. Unchanged "
            "entities are still neither re-written nor re-embedded."
        )
        return dataclasses.replace(deferral, command_to_apply=cmd + note)
    except Exception as exc:  # noqa: BLE001 — never drop a deferral
        logger.warning("could not retarget resync deferral: %s", exc)
        return deferral


def ensure_extractor_generation(
    folder: Path,
    *,
    prev_version: str,
    running_version: str,
    project_name: str,
    weaviate_url: Optional[str] = None,
    python_exe: Optional[str] = None,
    index_dot_claude: bool = False,
    graph_exists: Optional[bool] = None,
    probe=None,
    spawn=None,
) -> ReindexResult:
    """Decide, and when owed, launch the background force-rewalk.

    Called from the per-project bundle update
    (``project_init.install_project_bundle``) — the ONE path the launcher runs
    for every project AND, since v0.2.85, for the orchestrator root.

    NEVER raises, NEVER blocks: the spawn is detached with no wall-clock
    deadline, and any failure returns ``status="failed"`` with a message the
    caller surfaces as a warning.

    ``spawn`` / ``probe`` are the injection seams for tests; production leaves
    both ``None`` so the real
    :func:`vco_lib.codegraph_resync.spawn_background_resync` runs.
    """
    try:
        verdict = plan(
            folder,
            prev_version=prev_version,
            running_version=running_version,
            project_name=project_name,
            weaviate_url=weaviate_url,
            graph_exists=graph_exists,
            probe=probe,
        )
    except Exception as exc:  # noqa: BLE001 — a broken detector never blocks
        return ReindexResult(
            status="failed", reason="plan_raised",
            message=f"extractor-generation planning failed: {exc}",
        )

    if not verdict.needs_reindex:
        if verdict.stamp_now:
            ok = write_stamp(folder, verdict.generation, note=verdict.reason)
            return ReindexResult(
                status="stamped" if ok else "failed",
                reason=verdict.reason,
                generation=verdict.generation,
                message=(
                    f"code graph already at extractor generation "
                    f"{verdict.generation} ({verdict.reason})"
                    if ok else "completion stamp could not be written"
                ),
            )
        return ReindexResult(
            status="skipped", reason=verdict.reason,
            generation=verdict.generation,
            message=f"no extractor re-index owed ({verdict.reason})",
        )

    # The user-facing reason for the owed walk: the triggering rung's own
    # text when the verdict names one, generic otherwise (never one
    # release's hard-coded reason for every rung).
    reason_text = describe_rung(verdict.bump) or (
        f"the graph may predate extractor generation v{verdict.generation}"
    )

    if spawn is None:
        try:
            from vco_lib.codegraph_resync import spawn_background_resync as spawn
        except Exception as exc:  # noqa: BLE001
            return ReindexResult(
                status="failed", reason=verdict.reason,
                generation=verdict.generation, reason_text=reason_text,
                message=f"codegraph_resync helper unavailable: {exc}",
            )

    # v0.2.92: the force-rewalk decision travels as EXPLICIT ARGV across both
    # process hops (`spawn_background_resync` → `codegraph_resync --run-resync`
    # → analyzer). It used to ride ambient env, which worked but made the
    # decision invisible at every seam it crossed and inheritable by anything
    # else the process spawned. `FORCE_REWALK_ENV` remains supported by
    # `resolve_force_rewalk` for MANUAL invocation; no VCO code path relies on
    # inheritance any more.
    try:
        result = spawn(
            Path(folder),
            project_name,
            python_exe=python_exe,
            # The embed-revision owed-probe CANNOT see this axis: after an
            # extractor-only fix every stored row is at the current revision,
            # so the probe correctly reports "nothing owed" and would refuse
            # to spawn. Our detector has already established that work IS
            # owed, on a different axis — so the probe is bypassed, not
            # weakened.
            check_owed=False,
            force_rewalk=True,
            index_dot_claude=index_dot_claude,
        )
    except Exception as exc:  # noqa: BLE001 — a spawn failure never blocks
        return ReindexResult(
            status="failed", reason=verdict.reason,
            generation=verdict.generation, reason_text=reason_text,
            message=f"extractor re-index spawn raised: {exc}",
        )

    status = getattr(result, "status", "skipped")
    message = getattr(result, "message", "") or ""
    if status == "launched":
        return ReindexResult(
            status="launched", reason=verdict.reason,
            generation=verdict.generation, reason_text=reason_text,
            pid=getattr(result, "pid", None),
            message=(
                f"extractor-generation re-index launched in background for "
                f"{project_name}"
            ),
        )
    if status == "deferred":
        return ReindexResult(
            status="deferred", reason=verdict.reason,
            generation=verdict.generation, reason_text=reason_text,
            deferral=_retarget_deferral(getattr(result, "deferral", None),
                                        verdict.generation),
            message=message or "extractor re-index deferred",
        )
    if status == "failed":
        # v0.2.94: the spawner refused because the resolved interpreter cannot
        # import what the detached child needs. This is a BROKEN install, not a
        # decline — it must NOT be flattened into `skipped` (which the caller
        # logs at phase "ok" and never surfaces to the user). Surfacing it is
        # the whole point: the 2026-09-09 field defect was invisible for two
        # releases because the GUI said "started in the background" while eight
        # detached children died on `import vco_lib`. The stamp stays unwritten,
        # so the next bundle update retries.
        return ReindexResult(
            status="failed", reason=verdict.reason,
            generation=verdict.generation, reason_text=reason_text,
            message=message or "extractor re-index could not be started",
        )

    # not_owed / skipped from the spawner: it declined for a reason of its own
    # (spawn kill-switch, analyzer not found, empty project name). The stamp is
    # deliberately NOT written — the project stays owed and the next update
    # asks again.
    return ReindexResult(
        status="skipped", reason=verdict.reason,
        generation=verdict.generation, reason_text=reason_text,
        message=message or f"extractor re-index not launched ({status})",
    )


__all__ = [
    "CURRENT_EXTRACTOR_GENERATION",
    "EXTRACTOR_GENERATION_BUMPS",
    "EXTRACTOR_GENERATION_RUNG_REASONS",
    "FORCE_REWALK_ENV",
    "STAMP_REL",
    "REASON_CROSSES_BUMP",
    "REASON_NO_GRAPH",
    "REASON_STAMP_CURRENT",
    "REASON_UNKNOWN_GENERATION",
    "REASON_VERSION_AT_OR_PAST_BUMP",
    "ReindexResult",
    "Verdict",
    "code_graph_exists",
    "crosses_version_boundary",
    "decide",
    "describe_rung",
    "ensure_extractor_generation",
    "generation_is_current",
    "parse_semver",
    "plan",
    "read_stamp_generation",
    "resolve_force_rewalk",
    "stamp_after_walk",
    "stamp_path",
    "walk_certifies_generation",
    "write_stamp",
]


# ---------------------------------------------------------------------------
# Doctor probe body (the `codegraph_insert_failures` shim in vco_lib.doctor)
# ---------------------------------------------------------------------------

#: Probe name in ``vco_lib.doctor.PROBES``.
DOCTOR_PROBE = "codegraph_insert_failures"


def _doctor_roots(folder: Path) -> "list[Path]":
    """``folder`` plus every registered project folder (the same read-only
    launcher.db enumeration the KG census uses). Enumeration failure is not a
    verdict: the folder itself is still checked."""
    roots: "list[Path]" = [Path(folder)]
    try:
        from vco_lib.project_identity import resolve_snapshot  # noqa: PLC0415

        snapshot = resolve_snapshot()
        if getattr(snapshot, "resolvable", False):
            for ident in snapshot.projects:
                fp = (getattr(ident, "folder_path", "") or "").strip()
                if fp:
                    roots.append(Path(fp))
    except Exception:  # noqa: BLE001 — could not enumerate ⇒ folder only
        pass
    seen: set = set()
    out: "list[Path]" = []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def accepted_failing_sets(roots: "Sequence[Path]") -> "list[tuple[Path, dict]]":
    """``(root, accepted_failing_set)`` for every root whose stamp carries one."""
    found = []
    for root in roots:
        payload = read_stamp_payload(root) or {}
        acc = payload.get(ACCEPTED_FAILING_SET_KEY)
        if isinstance(acc, dict) and acc.get("rows"):
            found.append((Path(root), acc))
    return found


def doctor_insert_failures(folder: Path, *, roots: "Optional[Sequence[Path]]" = None) -> list:
    """Findings for the ``codegraph_insert_failures`` doctor probe.

    A PROBLEM (the ``retired_endpoint_env`` shape — no registered condition,
    full scope only) whenever a code graph was certified with an accepted
    failing set: those rows are missing from the graph and nothing else says
    so. The command names every failing file and the analyzer run that
    re-tries them once the cause is fixed (a clean force walk drops the
    record). Read-only: one small JSON read per root.
    """
    from vco_lib.doctor import (  # noqa: PLC0415
        FIX_DEFER, STATUS_OK, STATUS_PROBLEM, Finding,
    )

    hits = accepted_failing_sets(list(roots) if roots is not None else _doctor_roots(folder))
    if not hits:
        return [Finding(
            probe=DOCTOR_PROBE, status=STATUS_OK,
            summary="no code graph was certified with persistently failing inserts",
        )]
    parts, lines, detail = [], [], {}
    for root, acc in hits:
        rows = [r for r in acc.get("rows") or [] if isinstance(r, dict)]
        name = str(acc.get("project") or "").strip() or root.name
        parts.append(f"{name}: {len(rows)} row(s) not in the graph since "
                     f"{acc.get('first_seen') or '?'}")
        lines.append(f"# {name} ({root}) — fix the cause, then re-walk:")
        for r in rows:
            lines.append(f"#   {r.get('file')} [{r.get('collection')}] {r.get('reason')}")
        lines.append(f".claude/scripts/code-graph-analyze {root} --project {name} --force-rewalk")
        detail[str(root)] = acc
    return [Finding(
        probe=DOCTOR_PROBE, status=STATUS_PROBLEM,
        summary="code graph certified with persistently failing inserts — "
                + "; ".join(parts),
        fix=FIX_DEFER, command="\n".join(lines), detail={"accepted": detail},
    )]
