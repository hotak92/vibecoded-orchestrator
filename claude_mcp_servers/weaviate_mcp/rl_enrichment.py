# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL reranking + RL-telemetry-enrichment layer for the Weaviate MCP server.

v0.2.73 M-1: extracted VERBATIM from ``server.py`` (these 37 functions were
previously inline in the ~10k-line monolith). Behaviour is unchanged — this
is a pure move refactor. The ONLY textual changes to the bodies are
mechanical, behaviour-preserving qualifications:

  * References to server-module-level config constants / mutable caches
    (``ACTIVE_EMBEDDING``, ``EMBEDDING_MODEL``, ``logger``,
    ``_rl_client_instances``, ``_rl_telemetry_writers``,
    ``_rl_node_content_cache``, the ``_RL_*`` thresholds,
    ``_SERVER_INFERRED_BASE`` …) are now read/written through the ``server``
    module object. Those globals STAY in ``server.py`` because
    ``rl_client.search_pipeline`` and the test-suite already reach them via
    ``srv.<name>`` — moving them would fork the storage. Reading them via
    ``server.<name>`` at call time is value-identical (they are resolved
    once at server import and never rebound).
  * Cross-calls to server-side helpers (``get_weaviate_client``,
    ``_try_resolve_project_config``) and to SIBLING RL helpers
    (``_cosine``, ``_get_rl_telemetry_writer_for`` …) go through
    ``server.<fn>`` so the existing tests — which
    ``monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", …)`` /
    ``patch("…server._get_rl_client")`` and expect internal calls to observe
    the patch — keep working. ``server`` re-exports every function below into
    its own namespace, so ``server.<fn>`` is always a valid, patchable
    attribute.
  * One module-introspection idiom (``"ACTIVE_EMBEDDING" in globals()``) is
    rewritten to ``hasattr(server, "ACTIVE_EMBEDDING")`` so the guard checks
    the SERVER module's namespace (where the constant lives) rather than this
    module's.

Import-order: this module is only ever imported by ``server`` itself, at the
END of server's own body (the re-export block near the bottom of server.py).
By that point ``server`` is far enough initialised that the module-level
``from . import server`` below binds the real (in-``sys.modules``) module
object, and the 29 functions here that read ``server.<name>`` resolve it as a
normal module global. Nothing in this module touches ``server`` at its OWN
import time (only inside function bodies), so the circular edge never fires on
the real load path. Do NOT reorder: importing this module BEFORE ``server``
(which no shipped entrypoint, test, or script does — every caller imports
``weaviate_mcp.server``) would run server's re-export against a
half-initialised ``rl_enrichment``.

The sibling ``claude_mcp_servers.rl_client`` package (``search_pipeline.py``,
``citation_compute.py``, ``embed_regen.py``) reaches into ``server`` the same
way, but via CALL-TIME imports because those modules can be imported
standalone; this module cannot (server is its sole importer), so the
module-level binding is both sufficient and cheaper than 29 in-function imports.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

# The 29 functions below read server-module state as ``server.<name>`` (a
# module global). Rather than an EAGER ``from . import server`` — which binds
# ONE specific server module object at import time and desyncs if ``server`` is
# later re-imported while this module isn't (a partial ``sys.modules`` purge
# leaves the re-exported functions pointing at a STALE server, so a
# ``monkeypatch.setattr(new_server, …)`` isn't seen) — ``server`` is a LAZY
# PROXY that forwards every attribute access to the LIVE
# ``sys.modules["weaviate_mcp.server"]``. This is:
#   * import-order-safe: nothing touches ``server`` at THIS module's load time
#     (the proxy resolves lazily on first attribute access, after both modules
#     have finished importing), so the circular edge never fires;
#   * re-import-safe: bare ``server.<name>`` always hits the CURRENT server
#     module, so a test that purges + re-imports ``weaviate_mcp`` (and patches
#     the fresh server) is observed correctly even by re-exported functions.
import sys as _sys
import types as _types
import importlib as _importlib


class _LazyServerProxy(_types.ModuleType):
    """Attribute access forwards to the live ``weaviate_mcp.server`` module."""

    def __init__(self) -> None:
        super().__init__("weaviate_mcp._server_proxy")

    def _live(self):
        mod = _sys.modules.get("weaviate_mcp.server")
        if mod is None or mod is self:
            mod = _importlib.import_module("weaviate_mcp.server")
        return mod

    def __getattr__(self, name):
        return getattr(self._live(), name)


server = _LazyServerProxy()  # noqa: F811 — module-level attr source for server.<name>

# Answer-window matcher: single source of truth lives in
# ``rl_client.answer_window`` (imported here the same way server.py did — one
# concern, one home; the monitor + Stop-hook drain must agree byte-for-byte).
from claude_mcp_servers.rl_client.answer_window import (
    match_position_for_query as _match_position_for_query,
)

def _rl_load_messages(transcript_path: "Path") -> list[dict]:
    """Load all JSONL messages from a transcript file.

    v0.2.70 (S2): thin shim to ``rl_client.answer_window.load_messages`` — one
    home shared by the MCP monitor AND the Stop-hook drain.
    """
    from claude_mcp_servers.rl_client.answer_window import load_messages
    return load_messages(transcript_path)

def _rl_find_kg_positions(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (msg_idx, blk_idx) for every KG search tool_use block.

    v0.2.70 (S2): thin shim to ``rl_client.answer_window.find_kg_positions``.
    """
    from claude_mcp_servers.rl_client.answer_window import find_kg_positions
    return find_kg_positions(messages)

def _rl_extract_answer_window(
    messages: list[dict],
    start_msg_idx: int,
    start_blk_idx: int,
) -> tuple[str, bool]:
    """
    Extract text produced by Claude after the KG search at (start_msg_idx, start_blk_idx).

    V52-N rewrite (2026-06-09): the extractor is now TOOL-AGNOSTIC and does
    NOT stop on a human turn. User direction verbatim:

      * "make it agnostic from used tool, just log the 'input' of the tool
        use, and discard its 'output'"
      * "if tool name ends up in the output it's ok"
      * "human input should not interrupt 'accumulation'"

    Concretely, this function scans every assistant message from
    ``start_msg_idx`` to the end of the transcript and accumulates:

      * All ``text`` blocks.
      * All ``thinking`` blocks (Claude's internal scratch -- useful RL
        signal).
      * For EVERY ``tool_use`` block (regardless of tool name): the tool
        name + a JSON dump of the ``input`` field. ``toolUseResult`` /
        tool-output blocks are explicitly EXCLUDED -- they would dominate
        the answer window with shell output, file dumps, and API JSON,
        drowning out the citation signal.

    Stop conditions (whichever comes first):
      * Token-equivalent accumulation reaches
        ``_RL_MONITOR_ANSWER_THRESHOLD_TOKENS`` (~4x chars). Returns
        ``complete=True`` and a truncated window.
      * End of transcript. Returns ``complete=False`` -- the monitor keeps
        polling.

    Crucially, human turns are NO LONGER a stop condition. Subsequent
    assistant blocks count as part of the SAME answer accumulation; this
    captures the realistic case where the user types a quick follow-up
    ("yes, continue") and Claude resumes producing citation-bearing text.
    The legacy human-turn stop caused the accumulator to fire on a tiny
    sub-25k-token slice and then the citation gate rejected it as too
    short -- silent drop.

    Per-tool-use input is truncated to ``_RL_TOOL_CONTENT_LIMIT`` chars
    to bound any single tool call's contribution; the max-over-chunks
    cosine downstream means the relevant chunk still dominates.

    Returns (text, complete).

    v0.2.70: the extraction logic now lives in the shared module
    ``claude_mcp_servers.rl_client.answer_window`` so the in-MCP monitor AND
    the Stop-hook drain (``scripts/rl_drain_citations.py``) use ONE home
    (modularity ruling). This is a thin shim that forwards the MCP's tunable
    thresholds; the body is byte-equivalent to the pre-extraction V52-N code.
    """
    from claude_mcp_servers.rl_client.answer_window import extract_answer_window
    return extract_answer_window(
        messages,
        start_msg_idx,
        start_blk_idx,
        threshold_tokens=server._RL_MONITOR_ANSWER_THRESHOLD_TOKENS,
        tool_content_limit=server._RL_TOOL_CONTENT_LIMIT,
    )

async def _resolve_claude_session_dir(workspace_path: Path) -> "Path | None":
    """Resolve the Claude session-transcript directory for a workspace.

    Source of truth: ``vct-hub`` (per the launcher-as-router pattern —
    the hub knows ``projects.folder_path`` for every registered project
    and computes the slug once at registration time using the canonical
    ``vco_lib.project_config.claude_session_dir_for`` helper). Falls
    back to a local slug heuristic when the hub is unreachable so MCPs
    still function during:

    * Hub-startup races (MCP subprocess imports before ``session-start-
      ensure-hub.sh`` finishes spinning up vct-hub).
    * Free-tier installs that don't run the launcher GUI at all.
    * The brief window after a launcher restart when the in-process 5 s
      discovery cache has expired but the new token hasn't been read.

    The fallback path implements the FULL Claude Code slug rule (``/`` +
    ``_`` + ``.`` → ``-``) rather than the half-rule that was inlined
    here in v0.2.30 and earlier. That half-rule caused the 97.7%
    orphan-citation rate documented in the v0.2.31 bug report
    (``.claude/context/plans/rl-citation-monitor-bug-report-2026-05-23.md``)
    for any workspace whose absolute path contained underscores.

    Returns the resolved :class:`Path` (which may or may not exist on
    disk — caller checks ``.exists()``), or ``None`` if the resolved
    candidate doesn't exist on disk (fresh workspace that hasn't been
    opened in Claude Code yet → no session-jsonl dir → nothing to
    poll). The ``None`` sentinel is preserved from the pre-v0.2.31
    implementation so the calling code path stays the same.
    """
    # Primary path: ask vct-hub. Reuses the cached resolver result
    # populated at module import time (`_try_resolve_project_config`)
    # so we don't issue a fresh HTTP call on every poll iteration.
    cfg = server._try_resolve_project_config()
    if cfg is not None:
        # ProjectConfig has `claude_session_dir` from v0.2.31 onward;
        # older hubs paired with new MCPs omit the field, in which case
        # getattr returns "" and we fall through to the local rule.
        # The empty-string check is defensive — the field is required
        # in the v0.2.31+ contract, but a pre-v0.2.31 hub paired with
        # a v0.2.31 MCP would emit a body without it and the resolver
        # would error out at the dataclass level. The fallback covers
        # that mismatch.
        candidate_str = getattr(cfg, "claude_session_dir", "") or ""
        if candidate_str:
            candidate = Path(candidate_str)
            return candidate if candidate.exists() else None

    # Fallback: replicate Claude Code's slug rule locally. Mirrors the
    # canonical helper in vco_lib.project_config.claude_session_dir_for
    # (`/` + `_` + `.` → `-`). Inlined here rather than imported so
    # this MCP keeps working even when vco_lib fails to import (the
    # `_HAS_PROJECT_CONFIG=False` branch above).
    slug = str(workspace_path).replace("/", "-").replace("_", "-").replace(".", "-")
    candidate = Path.home() / ".claude" / "projects" / slug
    return candidate if candidate.exists() else None

def _rl_find_all_transcripts_in_dir(slug_dir: Path) -> "list[Path]":
    """Return all .jsonl transcripts in a given slug dir, newest first.

    Split out of :func:`_rl_find_all_transcripts` for testability —
    the dir-resolution path is async (hub-aware), but the actual
    file-glob is pure I/O and benefits from being a separate sync
    helper.

    v0.2.47 RL-7.5 (2026-06-04): also includes subagent transcripts at
    ``<slug>/<parentSessionId>/subagents/agent-<agentId>.jsonl`` so the
    monitor finds KG searches performed BY subagents. Subagent transcripts
    have the same ``{type, message: {content: [blocks]}}`` shape as parent
    transcripts (verified via Claude Code docs + filesystem probe
    2026-06-04), so ``_rl_extract_answer_window`` works unchanged. Each
    subagent file is independent — the seq-based tiebreak inside
    ``_rl_answer_monitor`` still matches a KG call to its rightful
    transcript because each subagent only sees its own KG search history.
    """
    if not slug_dir.exists():
        return []
    parent_transcripts = list(slug_dir.glob("*.jsonl"))
    # Subagent transcripts live under each parent session's subdirectory.
    # We don't need to filter by parent session — every subagent file is
    # a potential candidate for the KG-call lookup.
    subagent_transcripts = list(slug_dir.glob("*/subagents/agent-*.jsonl"))
    all_transcripts = parent_transcripts + subagent_transcripts
    return sorted(
        all_transcripts,
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

async def _rl_find_all_transcripts() -> "list[Path]":
    """Return all .jsonl transcripts in the project slug dir, newest first.

    v0.2.31: resolves the slug dir via :func:`_resolve_claude_session_dir`
    (hub-primary, local-slug fallback with the COMPLETE rule). Replaces
    the broken inline ``str(_SERVER_INFERRED_BASE).replace("/", "-")``
    that only handled the path-separator substitution and missed the
    underscore rule.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    slug_dir = await server._resolve_claude_session_dir(server._SERVER_INFERRED_BASE)
    if slug_dir is None:
        return []
    return server._rl_find_all_transcripts_in_dir(slug_dir)

def _rl_is_literal_cited(node: dict, answer_text_lower: str) -> bool:
    """v0.2.47 RL-7: True iff any identity-form of `node` appears in the answer.

    The forms checked (in order):
      1. ``[[Title]]`` exact-match wikilink (Obsidian style).
      2. ``[[Title]]`` lower-cased exact-match (case-insensitive markdown).
      3. ``\\bTitle\\b`` word-boundary regex against the lower-cased answer.
      4. Same three forms for the file_path slug (the stem, e.g.
         ``foo`` from ``knowledge/concepts/foo.md``).
      5. Same for the full file_path (with and without the ``.md`` suffix).

    Caller passes a pre-lowered ``answer_text_lower`` — the helper is called
    in a tight per-node loop and re-lowering each time would waste cycles.

    Skips titles shorter than ``_RL_LITERAL_CITED_MIN_TITLE_LEN`` (default 3)
    to avoid the "RL" / "AI" / "url" / "curl" false-positive class. Same
    rule as ``paid-modules/vct-rl-reranker/retrieval_rl.py``.
    """
    if not isinstance(node, dict):
        return False

    forms: list[str] = []
    title = (node.get("title") or "").strip()
    if title and len(title) >= server._RL_LITERAL_CITED_MIN_TITLE_LEN:
        forms.append(title)
    file_path = (node.get("file_path") or "").strip()
    if file_path:
        from pathlib import Path as _Path
        slug = _Path(file_path).stem
        if slug and len(slug) >= server._RL_LITERAL_CITED_MIN_TITLE_LEN:
            forms.append(slug)
        forms.append(file_path)
        if file_path.endswith(".md"):
            forms.append(file_path[:-3])

    if not forms:
        return False

    import re as _re
    for form in forms:
        form_lower = form.lower()
        # WikiLink form: `[[Title]]` exact match (case-insensitive).
        if f"[[{form_lower}]]" in answer_text_lower:
            return True
        # Word-boundary match for the bare form.
        try:
            if _re.search(r'\b' + _re.escape(form_lower) + r'\b', answer_text_lower):
                return True
        except _re.error:
            # Malformed regex — defensively skip; never crash the monitor.
            continue
    return False

async def _rl_compute_and_write_citations(
    task_id: str,
    answer: str,
    ctx: dict,
) -> "dict | None":
    """v0.2.47 RL-7: compute the citation event from a complete answer + write it.

    Reads the per-task cache populated by ``_rl_cache_and_rerank`` (the node
    list with `n_emb` + metadata, the query embedding, the active embedding
    model). Chunks the answer via ``Chunker.for_model(active_model)``,
    embeds each chunk via the cached ``EmbeddingService``, then for each
    node:

      - ``cosine_sims[title] = max(_cosine(answer_chunk_emb, node.n_emb))``
        across all answer chunks. Skips nodes without ``n_emb``.
      - ``literal_cited[title]`` via ``_rl_is_literal_cited(node, answer_lower)``.

    Calls ``compute_unified_targets(cosine_sims, literal_cited, None)`` to
    derive the binary ``cited`` map (target > 0.6), then writes the citation
    event via ``RLTelemetryWriter.log_citations`` — which POSTs to the
    launcher's hub per C6c.

    v0.2.47 RL-7/8 (C8): the function now ALSO mutates ``ctx`` in place,
    adding ``cosine_sims_computed`` and ``literal_cited_computed`` keys.
    The caller (``_rl_answer_monitor``) reads these to build the container
    POST payload without re-embedding. Returns the result dict on success
    (containing ``cosine_sims`` and ``literal_cited``) or ``None`` on
    soft-fail. Soft-fail throughout: missing cache / no embedding service
    / chunker error / hub POST 5xx all return ``None`` and log at debug.

    The caller decides whether to also POST to the RL container's
    ``/rl_update`` after this returns; the two writes are independent
    (citation logging is unconditional; container training is Pro-tier
    + container-running gated).

    v0.2.70: the compute core now lives in the shared module
    ``claude_mcp_servers.rl_client.citation_compute`` so the in-MCP monitor AND
    the Stop-hook drain (``scripts/rl_drain_citations.py``) use ONE home
    (modularity ruling). This is a thin shim; the body is behaviour-equivalent
    to the pre-extraction code (it mutates ``ctx`` in place with
    ``cosine_sims_computed`` / ``literal_cited_computed`` and returns the same
    ``{cosine_sims, literal_cited, cited}`` dict).
    """
    from claude_mcp_servers.rl_client.citation_compute import compute_citation
    return compute_citation(task_id, answer, ctx, write=True)

def _rl_force_flush_sentinel_path() -> "Path":
    """Resolve the V52-N compaction sentinel path relative to the project root.

    Path is ``<CLAUDE_PROJECT_DIR>/.claude/state/rl_monitors_force_flush.flag``
    with a fallback to ``_SERVER_INFERRED_BASE`` when the env var isn't
    populated (CLI invocations + tests). The sentinel is created by
    ``templates/hooks/pre-compact-save.{sh,ps1}`` immediately before
    Claude Code compacts the context; this monitor polls for it on every
    iteration and, when present, fires with whatever it has accumulated
    so far.
    """
    from pathlib import Path as _Path
    base = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if base:
        return _Path(base) / server._RL_MONITOR_FORCE_FLUSH_SENTINEL
    return server._SERVER_INFERRED_BASE / server._RL_MONITOR_FORCE_FLUSH_SENTINEL

def _rl_check_force_flush() -> bool:
    """Return True iff the V52-N compaction-sentinel file exists.

    Soft-fail: any filesystem error returns False so the monitor keeps
    polling normally rather than crashing on a transient ENOENT race.
    """
    try:
        return server._rl_force_flush_sentinel_path().exists()
    except Exception:
        return False

def _rl_clear_force_flush() -> None:
    """Delete the V52-N sentinel after a fire so subsequent compactions can re-arm.

    Soft-fail: missing file / permission error / race with the hook all
    return silently. Worst case a future poll sees a stale sentinel and
    fires once on an already-emitted task -- the per-task cache is popped
    so the second fire is a no-op.
    """
    try:
        path = server._rl_force_flush_sentinel_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass

def _rl_human_turn_after(messages: list[dict], start_msg_idx: int) -> bool:
    """F-B: True iff a human (user) turn appears after the KG search position.

    The natural end of "this answer" is the first user turn following the
    search tool_use. Used as the EARLIER-fire trigger so the monitor fires
    promptly instead of polling for up to 60 min. Tool-result messages are
    also ``type == "user"`` in the transcript, so we require a user message
    that carries a plain ``text`` (or string) content block — a real human
    turn, not a tool_result envelope.
    """
    for msg_idx in range(start_msg_idx + 1, len(messages)):
        msg = messages[msg_idx]
        if msg.get("type") != "user":
            continue
        content = msg.get("message", {}).get("content", [])
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str) and block.strip():
                    return True
                if isinstance(block, dict) and block.get("type") == "text":
                    if (block.get("text") or "").strip():
                        return True
    return False

def _rl_delete_own_pending_file(task_id: str) -> None:
    """F-QUEUE: delete THIS retrieval's pending file when the MCP monitor fires.

    The MCP path stages a pending file (as a backstop) AND keeps in-memory ctx;
    when the monitor fires successfully it deletes its own file so the Stop-hook
    drain only processes survivors (orphans = monitor never fired → drain
    recovers them). Soft-fail — a missing file is the common, fine case.
    """
    try:
        from claude_mcp_servers.rl_client.citation_pending import (
            list_pending_for_session,
            delete_pending,
        )
        # We don't know the session_id here, so match by the task_id suffix
        # across all pending files for the project (list with empty session
        # returns all). Cheap — the pending dir is tiny.
        for p in list_pending_for_session(""):
            if p.name.endswith(f"__{task_id}.json"):
                delete_pending(p)
    except Exception as exc:  # noqa: BLE001
        server.logger.debug("RL monitor: pending-file cleanup failed (%s)", exc)

async def _rl_answer_monitor(task_id: str, seq: int, query: str) -> None:
    """
    Background asyncio task: poll the session transcript until Claude's answer
    after the KG search is available, then POST to rl_server /rl_update.

    V52-N stop conditions (whichever comes first):
      - Answer window accumulates >= ``_RL_MONITOR_ANSWER_THRESHOLD_TOKENS``
        (25 000 tokens, matching the citation gate).
      - The PreCompact hook drops ``.claude/state/rl_monitors_force_flush.flag``
        -- the monitor fires with whatever's accumulated so far (could be
        less than the threshold) and deletes the sentinel so subsequent
        compactions re-arm.
      - Hard timeout ``_RL_MONITOR_TIMEOUT`` seconds (60 min safety valve,
        raised from 10 min pre-V52-N).

    v0.2.70 (F-B): two lifecycle fixes:
      1. ``ctx`` is captured into THIS task's closure (``held_ctx``) on the
         first poll where it is available in ``_rl_node_content_cache`` — so a
         LATE fire no longer depends on the 256-entry process-global LRU still
         holding ``task_id``. Pre-F-B the monitor popped the cache at fire time;
         by then later traffic had evicted it (the cache aggregates across all
         transcripts a single long-lived MCP subprocess serves), yielding
         ``ctx=None`` and a silent skip. Holding it in the closure removes the
         global-state coupling entirely.
      2. An EARLIER fire is restored: once a NEW human turn appears after the
         search (the natural end of THAT answer) AND the gate is met, the
         monitor fires immediately rather than waiting up to 60 min — so it
         fires well before eviction would ever have been a risk, even for the
         closure-held ctx. (Accumulation across human turns is preserved for
         the durable pending-queue path; this in-process trigger is the belt to
         the queue's suspenders.)

    The `seq` value is the 1-based call counter for this MCP process; it maps to
    the (seq-1)'th KG search position in the transcript (0-based rank).

    Works across parallel chats: scans all transcripts in the project slug dir and
    picks the one that contains this query at the expected seq position, preventing
    cross-contamination between simultaneously open VS Code windows.
    """
    from pathlib import Path as _Path

    deadline = asyncio.get_event_loop().time() + server._RL_MONITOR_TIMEOUT
    pos_idx = seq - 1  # 0-based index into kg_positions list
    query_snippet = query[:120]  # used to verify we're reading the right transcript

    # F-B: hold the staged ctx in THIS task's closure so a late fire never
    # depends on the LRU still holding task_id. Captured lazily on the first
    # poll it's available (the populate runs just after the spawn).
    held_ctx: "dict | None" = None

    # Phase 1 + 2 combined: find the right transcript and poll for completion
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(server._RL_MONITOR_POLL_INTERVAL)

        # F-B: snapshot the ctx into the closure as soon as it's available.
        # We do NOT pop here — the populate path manages the cache size; we
        # only need a private reference that eviction can't strand.
        if held_ctx is None:
            _staged = server._rl_node_content_cache.get(task_id)
            if _staged is not None:
                held_ctx = _staged

        # V52-N: check the compaction sentinel BEFORE scanning transcripts.
        # When set, we force-fire on whatever's accumulated regardless of
        # the natural threshold. Clearing happens after the fire below so
        # subsequent compactions re-arm cleanly.
        force_flush = server._rl_check_force_flush()

        # Scan all transcripts for the one that contains our query at pos_idx
        candidates = await server._rl_find_all_transcripts()
        # Also try CLAUDE_SESSION_ID fallback (CLI mode)
        if not candidates:
            session_id = os.getenv("CLAUDE_SESSION_ID", "")
            if session_id:
                projects_dir = _Path.home() / ".claude" / "projects"
                for f in sorted(projects_dir.rglob(f"{session_id}.jsonl")):
                    candidates = [f]
                    break

        for candidate in candidates:
            messages = server._rl_load_messages(candidate)
            kg_positions = server._rl_find_kg_positions(messages)

            # Find matching position by query fingerprint + seq tiebreak.
            # v0.2.70 (S2): the matcher lives in rl_client.answer_window so the
            # MCP monitor and the Stop-hook drain agree byte-for-byte on which
            # answer window belongs to which retrieval (one home).
            matched_pos = _match_position_for_query(
                messages, kg_positions, query_snippet, pos_idx
            )

            if matched_pos is None:
                continue

            start_msg_idx, start_blk_idx = matched_pos
            # Right transcript — check if answer is complete
            answer, complete = server._rl_extract_answer_window(messages, start_msg_idx, start_blk_idx)
            fire_reason = "threshold" if complete else ""
            # V52-N: the sentinel forces fire even when the extractor returned
            # complete=False (i.e. we haven't accumulated enough yet but compaction
            # is about to happen and we'd lose access to the transcript).
            if force_flush and answer.strip():
                complete = True
                fire_reason = "force_flush"
                server.logger.debug(
                    "RL monitor %s: force-flush sentinel detected; firing with %d chars accumulated",
                    task_id[:8], len(answer),
                )
            # F-B: earlier fire on a new human turn after the search. The
            # natural end of THIS answer is the first human turn following the
            # KG call; firing there (rather than waiting up to 60 min) makes the
            # monitor fire promptly. Gated on the same token floor as the
            # threshold path so we don't fire on a noisy preamble.
            if not complete and answer.strip():
                if server._rl_human_turn_after(messages, start_msg_idx):
                    try:
                        from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter
                        _tok = TokenCounter.count_tokens(answer)
                    except Exception:
                        _tok = len(answer) // 4
                    if _tok >= server._RL_MIN_ANSWER_TOKENS_FOR_CITATION:
                        complete = True
                        fire_reason = "human_turn"
                        server.logger.debug(
                            "RL monitor %s: human-turn fire (%d tokens accumulated)",
                            task_id[:8], _tok,
                        )
            if complete and answer.strip():
                # v0.2.47 RL-7: MCP-side citation write (replaces the
                # pre-v0.2.47 container-coupled write path that silently
                # broke when the container was down — the failure mode
                # diagnosed in `mcp-rl-online-training-monitor.md`
                # §"Third silent failure mode"). Independent of the
                # container POST below: writes the citation event to
                # launcher.db via the hub regardless of whether the
                # container is running. Soft-fail: a False return
                # leaves the container leg unaffected.
                #
                # v0.2.47 RL-7.5: token-based gate (default 25k tokens via
                # RL_MIN_ANSWER_TOKENS_FOR_CITATION env). Protects against
                # cosine-noise from streaming snapshots — Claude often
                # emits a "Sure! Let me look that up..." prefix before
                # the substantive response, and using that early answer
                # would write a noisy event we'd never overwrite.
                # 25k tokens ≈ 2-3 chunks at qwen3's xlarge preset; enough
                # for max-over-chunks cosine to discriminate cited-vs-not.
                # Falls back to char-based approximation when TokenCounter
                # is unavailable (1 token ≈ 4 chars).
                try:
                    from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter
                    answer_token_count = TokenCounter.count_tokens(answer)
                except Exception:
                    answer_token_count = len(answer) // 4

                # F-B: use the closure-held ctx (captured on an earlier poll)
                # so a late fire is not stranded by LRU eviction. Fall back to
                # the cache for the (rare) case the very first poll already had
                # a complete answer. We still pop the cache to free the slot.
                ctx = held_ctx or server._rl_node_content_cache.get(task_id)
                server._rl_node_content_cache.pop(task_id, None)
                # F-QUEUE: the MCP monitor deletes its OWN pending file on fire
                # so the Stop-hook drain only processes survivors (no double
                # write). Soft-fail — a missing file is fine.
                server._rl_delete_own_pending_file(task_id)
                # F-LOG: persist answer-window length + the fire reason so the
                # "≥25k tokens" distribution and monitor lifecycle are auditable
                # from stored data (pre-F-LOG both were un-observable).
                server.logger.info(
                    "RL monitor %s: fire reason=%s window_tokens=%d window_chars=%d",
                    task_id[:8], fire_reason, answer_token_count, len(answer),
                )
                citation_result: "dict | None" = None
                if answer_token_count >= server._RL_MIN_ANSWER_TOKENS_FOR_CITATION:
                    if ctx is not None:
                        # v0.2.73 RL-6: stamp fire_reason + window_tokens onto
                        # the ctx so the citation EVENT stores them (pre-RL-6
                        # they existed only in the logger.info line above).
                        ctx["fire_reason"] = fire_reason
                        ctx["window_tokens"] = answer_token_count
                        try:
                            citation_result = await server._rl_compute_and_write_citations(
                                task_id, answer, ctx,
                            )
                        except Exception as exc:
                            server.logger.debug(
                                "RL monitor %s: citation write raised (%s); continuing",
                                task_id[:8], exc,
                            )
                    else:
                        # F-B observability: ctx genuinely unavailable (never
                        # staged) — make the skip visible instead of silent.
                        server.logger.info(
                            "RL monitor %s: gate met but ctx unavailable; "
                            "citation deferred to Stop-hook drain",
                            task_id[:8],
                        )
                else:
                    server.logger.debug(
                        "RL monitor %s: answer too short (%d tokens < %d); skip citation",
                        task_id[:8], answer_token_count, server._RL_MIN_ANSWER_TOKENS_FOR_CITATION,
                    )

                # v0.2.47 C8: container POST uses the pre-packed payload
                # shape. Only fires when (a) the container is reachable
                # (client.enabled), (b) we have the ctx + computed
                # cosine_sims/literal_cited (i.e. citation write ran),
                # AND (c) we have at least one trainable signal. Container
                # is pure-train: MCP supplies everything pre-frozen.
                #
                # Free-tier / no-container installs: the citation event is
                # already on disk via the writer above; training is
                # Pro-tier-only, so silent skip here is correct.
                client = server._get_rl_client()
                if (
                    client is not None
                    and ctx is not None
                    and citation_result is not None
                ):
                    nodes_packed = ctx.get("nodes") or []
                    query_emb = ctx.get("query_emb") or []
                    if nodes_packed and query_emb:
                        try:
                            resp = await client.rl_update_v3(
                                task_id=task_id,
                                nodes_packed=nodes_packed,
                                query_emb=query_emb,
                                cosine_sims=citation_result["cosine_sims"],
                                literal_cited=citation_result["literal_cited"],
                                cross_encoder_cited=None,
                                task_type="mcp_interactive",
                            )
                            if resp.ok:
                                server.logger.debug(
                                    "RL monitor %s: trained on %d nodes (transcript %s)",
                                    task_id[:8], len(nodes_packed), candidate.name[:8],
                                )
                            else:
                                server.logger.debug(
                                    "RL monitor %s: rl_update_v3 not ok (%s)",
                                    task_id[:8], resp.error or resp.skipped or "unknown",
                                )
                        except Exception as exc:
                            server.logger.debug(
                                "RL monitor %s: rl_update_v3 raised (%s); continuing",
                                task_id[:8], exc,
                            )
                # V52-N: if we fired because the sentinel was set, clear it
                # so the next PreCompact event can re-arm a fresh flush.
                # When we fired on natural threshold completion, the sentinel
                # may still be absent -- _rl_clear_force_flush handles that
                # case as a soft-fail.
                if force_flush:
                    server._rl_clear_force_flush()
                return
            # Found the right transcript but answer not complete yet — stop scanning candidates
            break

    server.logger.debug("RL monitor %s: timed out after %.0fs", task_id[:8], server._RL_MONITOR_TIMEOUT)

def _get_rl_client():
    """Lazy-build one ``RLClient`` per (active embedding, project_id) per process.

    v0.2.42 RT-1: keyed by the *current* ``ACTIVE_EMBEDDING`` env value
    rather than a bare singleton.  A mid-session flip (user switches
    embedding model via the launcher) yields a fresh client whose
    ``active_embedding`` attribute matches the new value — the old
    client stays in the dict as a tombstone for any in-flight requests
    but is never returned to new callers.

    v0.2.49: cache key now includes the resolved project_id so the
    ``X-VCT-Project-ID`` header attached to outbound rerank/update
    requests routes to the correct per-project model head in the
    vct-rl-reranker v0.2.10+ container. project_id is None on
    hub-unreachable or for free-tier hosts without a hub; the client
    in that case sends no header and the container falls back to the
    base model.

    Reads ``RL_SERVER_URL`` / ``RL_SERVER_PORT`` at first call via
    ``rl_client.client._resolve_base_url``. When neither is set,
    the client lives in "disabled mode" and every call returns the
    no-rerank fallback without touching the network.
    """
    # F-ENV (v0.2.70): resolve the active embedding the rerank request will be
    # tagged with. The container pins itself to ONE embedding tag and returns a
    # 409 ``active_embedding_mismatch`` when a request carries a different tag
    # (the audited ``qwen3`` vs ``legacy`` source mismatch). The module-level
    # ``ACTIVE_EMBEDDING`` is the HUB-RESOLVED value (via ``_config_field`` —
    # reflects the per-project Identity-tab selection); a bare
    # ``os.getenv("ACTIVE_EMBEDDING")`` ignores the hub resolution and can send
    # a stale tag that the container rejects. Prefer the resolved constant; fall
    # back to the live env (mid-session flips set the env), then to "qwen3".
    # The readiness-GATE that consumes this is module-side (NOT this code).
    current_embedding = (
        os.getenv("ACTIVE_EMBEDDING")
        or (server.ACTIVE_EMBEDDING if hasattr(server, "ACTIVE_EMBEDDING") else "")
        or "qwen3"
    )

    # v0.2.49: resolve the project_id from the cached ProjectConfig.
    # Soft-fail: on any resolver exception (hub unreachable / malformed
    # response / no config) we proceed with project_id=None, which
    # makes the client send no X-VCT-Project-ID header. The container
    # then falls back to the base model — the safe, paying-user-not-
    # cut-off behaviour. RLClient.__init__ sanitises project_id on its
    # own; passing it through unchecked is also safe.
    #
    # V52-AA (v0.2.52): the same resolver also surfaces ``rl_server_port``
    # — the per-project RL Reranker container port allocated by the
    # supervisor and persisted to ``module_ports``. We use it as a
    # fallback when the canonical ``RL_SERVER_URL`` / ``RL_SERVER_PORT``
    # env vars are unset (which they always are for MCP subprocesses on
    # the default install — the launcher deliberately does NOT propagate
    # them to ``.claude/settings.json env`` per the H.1 contract). With
    # this fallback the client transitions from "disabled mode" to
    # "wired to the supervisor-allocated container" without any env
    # ceremony. Env vars still WIN when set, preserving the existing
    # override path for tests + dev users.
    current_project_id: Optional[str] = None
    hub_rl_server_port: Optional[int] = None
    try:
        _cfg = server._try_resolve_project_config()
        if _cfg is not None:
            if getattr(_cfg, "project_id", None):
                current_project_id = _cfg.project_id
            hub_rl_server_port = getattr(_cfg, "rl_server_port", None)
    except Exception as exc:
        server.logger.debug("project_id resolve failed (%s); will send no X-VCT-Project-ID", exc)

    cache_key = (current_embedding, current_project_id)
    if cache_key in server._rl_client_instances:
        return server._rl_client_instances[cache_key]

    try:
        from claude_mcp_servers.rl_client import RLClient
    except Exception as exc:
        server.logger.debug("RLClient import failed (%s); RL features disabled", exc)
        return None
    # text_dim comes from the MCP's notion of the active embedding —
    # we pull it from the EmbeddingService when available, falling back
    # to a sensible default (1024 for qwen3/arctic; legacy alias).
    text_dim = 1024
    try:
        from vco_lib.embedding_service import EmbeddingService
        svc = EmbeddingService.for_project()
        try:
            text_dim = svc.text_dim
        finally:
            svc.close()
    except Exception as exc:
        server.logger.debug("EmbeddingService probe failed (%s); using default text_dim=%d", exc, text_dim)

    # V52-AA: derive base_url from hub-resolved rl_server_port as a
    # fallback when env vars are unset. Env precedence:
    #   1. RL_SERVER_URL env (canonical override; full URL incl. host)
    #   2. RL_SERVER_PORT env (composed against 127.0.0.1)
    #   3. hub-resolved ``rl_server_port`` from ProjectConfig (V52-AA)
    #   4. None → "disabled mode"
    # RLClient.__init__ already uses _resolve_base_url() to cover (1)+(2)
    # internally when ``base_url`` arg is None. We pre-resolve (3) here
    # and pass it explicitly as ``base_url`` ONLY when (1)+(2) are unset,
    # so the existing env-override path stays intact for tests + dev.
    base_url_override: Optional[str] = None
    _env_url = os.environ.get("RL_SERVER_URL", "").strip()
    _env_port = os.environ.get("RL_SERVER_PORT", "").strip()
    if not _env_url and not _env_port and hub_rl_server_port:
        base_url_override = f"http://127.0.0.1:{hub_rl_server_port}"
        server.logger.debug(
            "RL client: env unset; using hub-resolved rl_server_port=%d",
            hub_rl_server_port,
        )

    client = RLClient(
        text_dim=text_dim,
        active_embedding=current_embedding,
        project_id=current_project_id,
        base_url=base_url_override,
    )
    server._rl_client_instances[cache_key] = client
    return client

def _embedding_dim_for(model: str) -> int:
    """Best-effort embedding-dim resolution from a model id.

    Used as a fallback when ``EmbeddingService.for_project()`` is
    unavailable at writer-construction time. Keeps the mapping close
    to the active set documented in CLAUDE.md (qwen3 / arctic / openai
    / codesage). Returns 1024 for unknown models (the default text-emb
    width across the orchestrator).
    """
    m = (model or "").lower()
    if "qwen3" in m:
        return 1024
    if "arctic" in m:
        return 1024
    if "codesage" in m:
        return 2048
    if "openai" in m or "text-embedding" in m:
        return 1536
    return 1024

def _extract_obj_vector(obj, target_name: str = "") -> list[float] | None:
    """Extract a Weaviate v4 object's vector for the matched named slot.

    ``obj.vector`` is a ``dict[str, list[float]]`` when the collection
    uses named vectors (the orchestrator's default — see e.g.
    `qwen3_embed`, `codesage_embed`) and an unwrapped list otherwise.
    Returns None when no vector is attached (e.g. the caller forgot
    ``include_vector=True``, or the requested slot doesn't exist).

    Used by hybrid_search + semantic_graph_search to attach `emb`
    to candidate dicts before they flow into log_retrieval (v0.2.31
    telemetry audit fix — Item 2.4).

    F-D (v0.2.70): when a ``target_name`` (the active named-vector slot,
    e.g. ``qwen3_embed``) is supplied, pull ONLY that slot. The pre-F-D
    body fell back to "first non-empty slot" when the requested slot was
    missing — which can pull a FOREIGN embedding space (e.g. a legacy
    ``ollama_embed``/arctic slot when the active model is qwen3, or a
    2048-dim codesage slot vs a 1024-dim query). Downstream ``_cosine``
    now refuses cross-dim comparisons, but the right fix is at the source:
    never hand back a vector from a slot the caller did not ask for.
    Returning None for a missing active slot is correct — the node simply
    has no comparable vector for this model, exactly like a node fetched
    without ``include_vector``. The first-slot fallback is retained ONLY
    for the slot-agnostic case (``target_name`` empty, e.g. legacy
    single-vector collections), where there is no active-slot ambiguity.
    """
    try:
        vec = getattr(obj, "vector", None)
        if vec is None:
            return None
        if isinstance(vec, dict):
            if target_name:
                # Active slot requested — pull ONLY that slot. A missing
                # active slot means "no comparable vector for this model"
                # (F-D: do NOT fall back to a foreign slot).
                v = vec.get(target_name)
            else:
                # Slot-agnostic caller (legacy single-vector mode): no
                # active-slot ambiguity, so the first non-empty slot is the
                # node's only vector. Multi-vector collections always pass a
                # target_name, so this branch never crosses embedding spaces.
                v = next((val for val in vec.values() if val), None)
        else:
            v = vec
        if v is None:
            return None
        # Coerce to a plain list of floats so downstream JSON serialization
        # doesn't choke on numpy-array-likes.
        return [float(x) for x in v]
    except Exception:
        return None

def _cosine(a, b) -> float:
    """Cosine similarity between two vectors. Returns 0.0 if either is
    zero-norm, **mismatched-length**, or non-iterable.

    F-D (v0.2.70): REFUSES on a dimension mismatch (``len(a) != len(b)``)
    by returning 0.0, making the docstring's long-standing claim true. The
    pre-F-D body did ``n = min(len(a), len(b))`` and computed cosine over
    the truncated overlap, which returns a *plausible* (~0.75) value for a
    1024-vs-2048 cross-model comparison — silently passing the 0.6 citation
    gate with garbage. The maintainer invariant is: never compare
    cross-model vectors; a length mismatch means the two embeddings live in
    different spaces (e.g. qwen3 1024 vs codesage 2048, or a node stored
    under a legacy slot vs an answer embedded with the active model), and
    the only correct answer is "no comparable signal" → 0.0.

    Pure-python — no numpy dep — so this stays usable in lean installs
    where numpy isn't pulled in transitively. Telemetry callsites
    should ALSO wrap calls in their own try/except so a bad shape
    never propagates into the rerank path (defence-in-depth).
    """
    try:
        if not a or not b:
            return 0.0
        # F-D: refuse on dimension mismatch instead of truncating. Different
        # lengths ⇒ different embedding spaces ⇒ no meaningful cosine.
        if len(a) != len(b):
            return 0.0
        n = len(a)
        if n == 0:
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for i in range(n):
            ai = float(a[i])
            bi = float(b[i])
            dot += ai * bi
            na += ai * ai
            nb += bi * bi
        if na == 0.0 or nb == 0.0:
            return 0.0
        import math
        return float(dot / (math.sqrt(na) * math.sqrt(nb)))
    except Exception:
        return 0.0

def _get_rl_telemetry_writer():
    """Lazy-build one ``RLTelemetryWriter`` per ``(project, embedding_source)`` tuple.

    v0.2.40 F2: was a single-instance singleton; mid-session env changes
    (ACTIVE_EMBEDDING flip qwen3→arctic2, PROJECT_NAME re-resolution from
    launcher.db adopt) would silently contaminate the offline training
    corpus because the writer froze its tags at first call. Now cached
    by ``(project, embedding_source)`` so each distinct env tuple gets
    its own writer with correct tags. All cached writers share the
    process and are eligible for shutdown reset via
    ``_reset_rl_telemetry_writers()``.

    v0.2.71 Sweep-C: this is now a thin wrapper over
    ``_get_rl_telemetry_writer_for`` that resolves the ACTIVE embedding triple
    from env / EmbeddingService. The dual-log fan-out reaches the SAME
    construction body for the OTHER slot via ``_get_rl_telemetry_writer_for``
    with an explicit ``embedding_source`` override (one home).
    """
    # ---- derive the ACTIVE embedding triple (source/model/dim) ----
    #
    # v0.2.31 telemetry audit fix (Item 2.2 — was 31% blank rows): when
    # EmbeddingService probe fails AND module-level constants happen to
    # be empty (env vars present but blank — observed on a small share
    # of installs), we still want a non-empty triple so the offline
    # trainer can join cohorts cleanly. Use os.getenv with defaults at
    # the writer-construction site too, in addition to the module-level
    # constants — defence-in-depth so the writer never ships blank
    # embedding_{source,model} into the JSONL.
    #
    # v0.2.40 F2: env reads happen on every call (not just on cache
    # miss) so a mid-session ACTIVE_EMBEDDING flip produces a NEW key
    # and constructs a NEW writer rather than returning the stale one.
    # `ACTIVE_EMBEDDING` (module constant) and `os.getenv(...)` are
    # both consulted; module-constant first matches pre-F2 priority,
    # but the env fallback now picks up runtime changes.
    emb_source = server.ACTIVE_EMBEDDING or os.getenv("ACTIVE_EMBEDDING", "qwen3") or "qwen3"
    # If the env was changed at runtime, prefer the live value over the
    # frozen module constant. (Empty/unset env → fall through to constant.)
    _live_active = os.getenv("ACTIVE_EMBEDDING")
    if _live_active:
        emb_source = _live_active
    emb_model = (
        server.EMBEDDING_MODEL
        or os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
        or "qwen3-embedding:0.6b"
    )
    _live_model = os.getenv("EMBEDDING_MODEL")
    if _live_model:
        emb_model = _live_model
    emb_dim = server._embedding_dim_for(emb_model)
    try:
        from vco_lib.embedding_service import EmbeddingService
        svc = EmbeddingService.for_project()
        try:
            emb_source = svc.text_model_short_id() or emb_source
            emb_dim = svc.text_dim or emb_dim
            emb_model = svc.text_model_id or emb_model
        finally:
            svc.close()
    except Exception as exc:
        server.logger.debug("EmbeddingService probe for telemetry failed (%s); using env defaults", exc)

    return server._get_rl_telemetry_writer_for(
        emb_source, embedding_dim=emb_dim, embedding_model=emb_model
    )

def _resolve_code_embedding_triple(
    slot: str, query_emb: "list[float] | None" = None
) -> "tuple[str, int, str]":
    """Resolve the CODE embedding (source, dim, model) triple — one home.

    v0.2.73: extracted from ``_emit_code_retrieval_telemetry`` so the
    structural-telemetry emit (``_emit_code_structure_telemetry``) shares the
    exact same resolution instead of forking it. Short source derives from the
    slot; dim prefers the ACTUAL query-vector length, then the service; model
    prefers the service-resolved id, then env, then the source tag. Soft-fail
    throughout — never raises, never blocks an emit.
    """
    code_source = server._slot_short_source(slot)
    code_dim = len(query_emb) if query_emb else 0
    code_model = ""
    try:
        svc = server._get_embedding_service()
        if svc is not None:
            code_model = getattr(svc, "code_model_id", "") or ""
            if not code_dim:
                code_dim = getattr(svc, "code_dim", 0) or 0
    except Exception:  # noqa: BLE001
        pass
    if not code_model:
        code_model = os.getenv("CODE_EMBED_MODEL", "") or code_source
    return code_source, code_dim, code_model

def _emit_code_structure_telemetry(
    *,
    query_type: str,
    target: str,
    results: "list[dict] | None",
    truncated: "bool | None" = None,
    session_id: "str | None" = None,
) -> bool:
    """v0.2.73 (RL follow-up): emit a retrieval event for ``query_code_structure``.

    Structural lookups return graph EDGES, not ranked candidates — there is no
    score, no rerank, and (deliberately) no citation staging and no
    ``answer_window.KG_SEARCH_TOOLS`` membership. The maintainer still wants
    uniform telemetry coverage across the code tools, so this emits a
    retrieval event carrying: the tool's (query_type, target) as the query
    string, per-result identity nodes (``tier="structural"``, ``score=0.0`` —
    the writer's clamp floor, honest for "unranked"), and event-level extras
    with ``retrieval_kind="code_structure"`` + the result count. Zero-result
    successes ARE emitted (a structural miss is a query-distribution signal);
    error paths are NOT (a failed tool call is not a retrieval).

    ``query_emb`` is None by design — structural queries never embed anything;
    the offline trainer's degraded/None handling already covers this shape.
    Soft-fail throughout; returns emit success.
    """
    try:
        from claude_mcp_servers.rl_client.telemetry_emit import (
            EmitValidationError,
            RetrievalEvent,
            emit_rl_event,
            new_task_id,
        )

        rows = results if isinstance(results, list) else []
        nodes: list[dict] = []
        for i, r in enumerate(rows[:server._CODE_STRUCTURE_TELEMETRY_MAX_NODES]):
            if not isinstance(r, dict):
                continue
            # Per-branch identity fallback (mirrors the collapse helper's F1
            # lesson): callers/methods/extends → full_name or name;
            # dependencies/imports → path; composes → composed_class;
            # interactions → endpoint or raw_target; path hops → full_name.
            title = (
                r.get("full_name")
                or r.get("name")
                or r.get("path")
                or r.get("composed_class")
                or r.get("endpoint")
                or r.get("raw_target")
                or ""
            )
            if not title:
                continue
            rec: dict = {
                "title": str(title),
                "score": 0.0,
                "tier": "structural",
                "shown_rank": i,
            }
            fp = r.get("file_path") or ""
            if fp:
                rec["file_path"] = fp
            nodes.append(rec)

        # Same slot resolution the code search path uses (best-effort).
        try:
            _svc = server._get_embedding_service() if server.DUAL_EMBEDDING_ENABLED else None
            _slot = _svc.code_vector_slot if _svc is not None else "codesage_embed"
        except Exception:  # noqa: BLE001
            _slot = "codesage_embed"
        code_source, code_dim, code_model = server._resolve_code_embedding_triple(_slot)

        extras: dict = {
            "retrieval_kind": "code_structure",
            "query_type": query_type,
            "target": target,
            "result_count": len(rows),
        }
        if truncated is not None:
            extras["truncated"] = bool(truncated)

        ev = RetrievalEvent(
            query=f"{query_type}:{target}",
            query_emb=None,
            embedding_source=code_source,
            embedding_dim=code_dim,
            embedding_model=code_model,
            nodes=nodes,
            task_id=new_task_id(),
            task_type="code_structure",
            session_id=session_id,
            rl_used=False,
            extras=extras,
        )

        def _structure_writer_factory():
            return server._get_rl_telemetry_writer_for(
                code_source, embedding_dim=code_dim, embedding_model=code_model,
            )

        try:
            return emit_rl_event(ev, writer_factory=_structure_writer_factory)
        except EmitValidationError as exc:
            server.logger.debug("code structure emit validation failed (%s)", exc)
            return False
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the tool
        server.logger.debug("code structure emit failed (%s)", exc)
        return False

def _emit_code_retrieval_telemetry(
    *,
    query: str,
    query_emb: "list[float] | None",
    survivors: list[dict],
    limit: int,
    slot: str,
    task_type: str = "code_search",
    retrieval_floor: "float | None" = None,
    post_rerank_floor: "float | None" = None,
    anchor_present: bool = False,
    scope: str = "",
    session_id: "str | None" = None,
    task_id: "str | None" = None,
) -> bool:
    """v0.2.73 RL-2: emit a retrieval event for the CODE search path.

    ONE home shared by the MCP tool (``search_code_graph``) and the CLI
    (``query_code_graph.py search_by_concept`` — hooks route through it), so
    the code path stops being a telemetry black hole (pre-RL-2 it emitted
    ZERO events: no queries, no ``_boost`` diagnostics, no floors — the RL
    corpus was KG-only).

    Per-node record: title = ``full_name`` (or the per-collection identity
    fallback), fused score (clamped by the writer), tier = the score-tier the
    renderer applied (``_tier``), shown_rank = final order, plus the code
    diagnostics ``collection`` / ``file_path`` / ``rerank_score`` /
    ``boost_delta`` / ``boost_signals`` / ``chunks_matched``. Event-level
    ``extras`` carries the resolved floors + anchor presence + scope;
    ``rl_used=False`` always (the code rerank is the deterministic
    relationship rerank, not the RL container).

    The writer is resolved for the CODE embedding triple (slot-derived short
    source + the ACTUAL query-vector dim) so these events partition into
    their own cohort and never contaminate the text-embedding corpus.

    v0.2.73 RL-2b: code retrievals now ALSO stage citation ctx — when
    survivors carry per-node vectors (``n_emb``, fetched with the candidates
    via ``include_vector`` in the MCP path), the helper stages a pending file
    (``source="hook"``, drain-owned — no in-process monitor for code) marked
    ``retrieval_kind="code"`` so ``citation_compute`` embeds the answer in
    the CODE model space and writes via the code-slot writer. Callers without
    vectors (the CLI today) stage nothing — behaviour is unchanged for them.
    ``task_id`` may be supplied by the caller; the SAME id is used for the
    retrieval event and the staged citation ctx so the pair joins in the
    corpus. Soft-fail throughout; returns emit success.
    """
    try:
        from claude_mcp_servers.rl_client.telemetry_emit import (
            EmitValidationError,
            RetrievalEvent,
            emit_rl_event,
            new_task_id,
        )

        nodes: list[dict] = []
        for i, c in enumerate(survivors):
            if not isinstance(c, dict):
                continue
            p = c.get("_p") or {}
            coll = c.get("_c", "")
            title = (
                p.get("full_name")
                or p.get("path")
                or p.get("endpoint")
                or p.get("file_path")
                or ""
            )
            if not title:
                continue
            rec: dict = {
                "title": title,
                "score": c.get("_s", 0.0),
                "tier": str(c.get("_tier") or "top_k"),
                "shown_rank": i,
                "collection": coll,
            }
            fp = p.get("file_path") or p.get("path") or ""
            if fp:
                rec["file_path"] = fp
            if c.get("_rerank") is not None:
                rec["rerank_score"] = c.get("_rerank")
            boost = c.get("_boost")
            if isinstance(boost, dict):
                if boost.get("delta") is not None:
                    rec["boost_delta"] = boost.get("delta")
                if boost.get("signals"):
                    rec["boost_signals"] = boost.get("signals")
            if c.get("chunks_matched") is not None:
                rec["chunks_matched"] = c.get("chunks_matched")
            # v0.2.73 RL-2b: carry the per-node stored vector (attached by the
            # candidate fetch via include_vector) so the retrieval event has
            # the same unified-target shape as KG events AND citation staging
            # below has real vectors to cosine against.
            if c.get("n_emb"):
                rec["n_emb"] = c["n_emb"]
            nodes.append(rec)

        if not nodes:
            return False

        # CODE embedding triple: slot-derived short source, the query
        # vector's ACTUAL dim, and the service-resolved model id when
        # available. Falls back to env — never blocks the emit.
        code_source, code_dim, code_model = server._resolve_code_embedding_triple(
            slot, query_emb
        )

        extras: dict = {"retrieval_kind": "code", "anchor": bool(anchor_present)}
        if scope:
            extras["scope"] = scope
        if retrieval_floor is not None:
            extras["retrieval_floor"] = retrieval_floor
        if post_rerank_floor is not None:
            extras["post_rerank_floor"] = post_rerank_floor

        _task_id = task_id or new_task_id()

        # v0.2.73 RL-2b: stage the citation ctx BEFORE the emit (mirrors
        # rerank_and_emit's ordering — staging never depends on emit
        # success). No-op when no node carries a vector.
        try:
            server._stage_code_citation_pending(
                task_id=_task_id,
                nodes=nodes,
                query=query,
                query_emb=query_emb,
                code_source=code_source,
                code_dim=code_dim,
                code_model=code_model,
                task_type=task_type,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — staging never breaks search
            server.logger.debug("code citation staging failed (%s)", exc)

        ev = RetrievalEvent(
            query=query,
            query_emb=query_emb,
            embedding_source=code_source,
            embedding_dim=code_dim,
            embedding_model=code_model,
            nodes=nodes,
            task_id=_task_id,
            task_type=task_type,
            session_id=session_id,
            rl_used=False,
            extras=extras,
        )

        def _code_writer_factory():
            return server._get_rl_telemetry_writer_for(
                code_source, embedding_dim=code_dim, embedding_model=code_model,
            )

        try:
            return emit_rl_event(ev, writer_factory=_code_writer_factory)
        except EmitValidationError as exc:
            server.logger.debug("code retrieval emit validation failed (%s)", exc)
            return False
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks search
        server.logger.debug("code retrieval emit failed (%s)", exc)
        return False

def _stage_code_citation_pending(
    *,
    task_id: str,
    nodes: list[dict],
    query: str,
    query_emb: "list[float] | None",
    code_source: str,
    code_dim: int,
    code_model: str,
    task_type: str,
    session_id: "str | None" = None,
) -> "str | None":
    """v0.2.73 RL-2b: stage the CODE citation ctx as a drain-owned pending file.

    Mirrors the KG path's ``search_pipeline._populate_citation_cache`` staging
    (same ``citation_pending.stage_pending`` single home, same ctx shape) with
    the code-specific differences named explicitly:

      * ``retrieval_kind="code"`` marks the ctx so ``citation_compute`` embeds
        the answer window in the CODE model space (``svc.embed_code``) and
        writes via the code-slot writer — a text-model answer embedding
        cosined against a CodeSage node vector is cross-space garbage that
        the ``_cosine`` dim-guard would zero out anyway.
      * ``source="hook"`` ALWAYS — the code path spawns NO in-process monitor,
        so the turn-end Stop-hook drain owns every code pending file (the
        "mcp" tag is reserved for monitor-owned files that self-delete).
      * ``active_model`` is the CODE model id, so the drain's chunker preset
        resolves against the right model family.

    No-op (returns None) when no node carries ``n_emb`` — a ctx without
    vectors can never produce a citation, so staging it would only feed the
    TTL sweep. Soft-fail throughout; returns the staged path or None.
    """
    if not any(isinstance(n, dict) and n.get("n_emb") for n in nodes):
        return None
    try:
        from claude_mcp_servers.rl_client.citation_pending import stage_pending
        from claude_mcp_servers.rl_client.telemetry_emit import resolve_session_id

        project_id_for_cache = None
        try:
            _cfg = server._try_resolve_project_config()
            if _cfg is not None:
                project_id_for_cache = getattr(_cfg, "project_id", None)
        except Exception:  # noqa: BLE001
            pass
        _staged_session = resolve_session_id(session_id or "")
        ctx_dict = {
            "nodes": nodes,
            "query_emb": list(query_emb) if query_emb else None,
            "active_model": code_model,
            "embedding_source": code_source,
            "embedding_dim": code_dim,
            "project_id": project_id_for_cache,
            # Same resolution the KG stage uses (getattr(srv, "PROJECT_NAME")
            # there) — server.py has no module-level PROJECT_NAME, so the env
            # is the actual source; empty string is the accepted degrade.
            "project_name": globals().get("PROJECT_NAME")
            or os.getenv("PROJECT_NAME", ""),
            "task_type": task_type,
            "session_id": _staged_session,
            # The marker citation_compute branches on (embed_code + code
            # writer). Kept as a ctx field (not inferred from the source
            # tag) because qwen3 can legitimately serve as BOTH the text
            # and the code model on mid-tier hardware.
            "retrieval_kind": "code",
        }
        return stage_pending(
            session_id=_staged_session,
            task_id=task_id,
            seq=None,
            query=query,
            ctx=ctx_dict,
            source="hook",
        )
    except Exception as exc:  # noqa: BLE001 — staging never breaks search
        server.logger.debug("_stage_code_citation_pending failed (%s)", exc)
        return None

def _get_rl_telemetry_writer_for(
    embedding_source: str,
    *,
    embedding_dim: int = 0,
    embedding_model: str = "",
):
    """Lazy-build / lookup the ``RLTelemetryWriter`` for an EXPLICIT slot triple.

    v0.2.71 Sweep-C: factored out of ``_get_rl_telemetry_writer`` so BOTH the
    active path (which resolves the live triple then delegates here) AND the
    dual-log fan-out (which passes the OTHER slot's triple) share ONE
    construction body. The per-``(project, embedding_source)`` cache
    (``_rl_telemetry_writers``) already isolates the two writers, so the
    second (other-slot) writer is just a second cache entry — no new caching
    code. The project tag is resolved the SAME way the active path always did
    (hub-resolved slug, env fallback) — it is identical for both slots since
    the two events describe the SAME retrieval in two embedding spaces.

    Args:
        embedding_source: Short source tag (``qwen3`` / ``arctic`` / ``openai``
            / ``codesage`` / ``legacy``) that partitions the offline corpus.
        embedding_dim: Vector dim for this source; 0 → resolve from
            ``embedding_model`` via ``_embedding_dim_for``.
        embedding_model: Full model id for this source (e.g.
            ``qwen3-embedding:0.6b``). Empty → kept empty (writer ships the
            source tag, which is the indexed cohort key).
    """
    # V52-AC contract: the ONLY no-write exit is the RLTelemetryWriter
    # import-failure path (lean install / shim test contexts) — soft-skip with a
    # DEBUG log so callers degrade gracefully rather than crash every search.
    # Lazy import keeps the server.py → rl_client edge off the module-load path
    # (rl_client lazy-imports back into server.py). v0.2.71 Sweep-C: this guarded
    # import was inadvertently dropped during the writer-factory extraction WIP;
    # it is restored here in the shared body so BOTH the active path and the
    # dual-log other-slot path inherit the soft-fail contract.
    try:
        from claude_mcp_servers.rl_client import RLTelemetryWriter
    except Exception as exc:
        server.logger.debug("RLTelemetryWriter import failed (%s); telemetry disabled", exc)
        return None

    emb_source = embedding_source or "qwen3"
    emb_model = embedding_model or ""
    emb_dim = embedding_dim or server._embedding_dim_for(emb_model)

    # ---- derive the project tag ----
    #
    # v0.2.21 Step 18: prefer the hub-resolved project slug/display name,
    # falling back to PROJECT_NAME / KG_COLLECTION env (historical
    # precedence) when the hub is unreachable.
    #
    # v0.2.28 (2026-05-23): canonicalize to the project SLUG when
    # available — the stable, machine-derived identifier (e.g.
    # "orchestrator-root", "vco-dev"). Pre-v0.2.28 the writer used
    # `project_display_name` first, which produced 4 distinct values
    # for the SAME project across the migration history (per the
    # rl-logging-audit-report-2026-05-23 finding #3: "Claude",
    # "VibeCoded Orchestrator", "VibeCodedOrchestrator", "VCODev" all
    # ended up in the JSONL as separate cohorts despite being one
    # project). Slugs are canonical, lowercase, hyphen-separated, and
    # match what the launcher's `list_rl_global_training_source_projects`
    # uses as the cohort key — so cohort analysis at training time can
    # join cleanly.
    project = ""
    _cfg_for_telemetry = server._try_resolve_project_config()
    if _cfg_for_telemetry is not None:
        project = (
            _cfg_for_telemetry.project_slug
            or _cfg_for_telemetry.code_graph_project
            or _cfg_for_telemetry.project_display_name
            or ""
        )
    if not project:
        # Env-fallback path also canonicalizes via sanitize_for_weaviate_class
        # so multi-workspace setups (same project opened with different env
        # casing/spacing) still produce one cohort.
        raw_name = os.getenv("PROJECT_NAME", "") or os.getenv("KG_COLLECTION", "")
        if raw_name:
            try:
                from vco_lib.project_init import sanitize_for_weaviate_class
                project = sanitize_for_weaviate_class(raw_name)
            except Exception:
                project = raw_name

    # ---- look up / construct the writer for this key ----
    key = (project, emb_source)
    writer = server._rl_telemetry_writers.get(key)
    if writer is not None:
        return writer
    # V52-J Edit 1 (2026-06-09): also pass project_id resolved above.
    # Pre-fix, project_id was never threaded through → 100% NULL in
    # launcher.db rl_events.project_id (see telemetry_emit.py module
    # docstring for the pre-v0.2.52 baseline).
    writer = RLTelemetryWriter(
        project=project,
        project_id=(
            _cfg_for_telemetry.project_id
            if _cfg_for_telemetry is not None
            else None
        ),
        embedding_source=emb_source,
        embedding_dim=emb_dim,
        embedding_model=emb_model,
    )
    server._rl_telemetry_writers[key] = writer
    return writer

def _other_model_for_source(other_source: str) -> str:
    """Map a short embedding-source tag to a concrete model id (best-effort).

    v0.2.71 Sweep-C. Used by the dual-log other-slot embed when the staged ctx
    did not carry an explicit ``other_embedding_model``. Mirrors the canonical
    family→model mapping the rest of the orchestrator uses (qwen3 is the
    default-text model; arctic / openai are the alternates a dual-write install
    keeps populated). Unknown tags fall through to the qwen3 default so the
    embed call still produces a comparable-space vector rather than raising.
    """
    s = (other_source or "").lower()
    if "qwen3" in s:
        return "qwen3-embedding:0.6b"
    if "arctic" in s:
        return "snowflake-arctic-embed2"
    if "openai" in s:
        return os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    return "qwen3-embedding:0.6b"

def _embed_text_in_other_model(
    svc, text: str, other_source: str, other_model: str = ""
) -> "list[float] | None":
    """Embed ONE text in a NON-active model's space (v0.2.71 Sweep-C).

    The dual-log other-slot citation compute re-embeds the answer chunks in the
    OTHER model's space so the cosine it logs against the OTHER slot's per-node
    vectors lives in the same space (a cross-space cosine is meaningless). This
    reuses the SAME backend adapters ``EmbeddingService.embed_text_all_configured``
    drives its secondary fan-out through — ``svc.ollama.embed`` for the Ollama
    families (qwen3 / arctic / legacy) and ``svc.openai.embed`` for the OpenAI
    slot — so a stored dual-write vector and an on-the-fly other-slot embed come
    from the identical backend path (no third embed implementation).

    Returns the vector, or None on soft-fail (empty text / backend down / unknown
    source). Never raises into the citation compute — the caller drops the chunk.
    """
    if not text or not text.strip():
        return None
    model = other_model or server._other_model_for_source(other_source)
    src = (other_source or "").lower()
    try:
        if "openai" in src:
            openai = getattr(svc, "openai", None)
            if openai is None:
                return None
            from vco_lib.embedding_service import _to_openai_api_model

            vec = openai.embed(_to_openai_api_model(model), text)
            return vec or None
        ollama = getattr(svc, "ollama", None)
        if ollama is None:
            return None
        vec = ollama.embed(model, text)
        return vec or None
    except Exception as exc:  # noqa: BLE001
        server.logger.debug(
            "_embed_text_in_other_model: embed in %s (%s) failed (%s)",
            other_source, model, exc,
        )
        return None

def _reset_rl_telemetry_writers() -> None:
    """Test/shutdown helper: drop all cached telemetry writers.

    v0.2.40 F2: the writer cache is per-process and per-(project, embedding)
    tuple. ``RLTelemetryWriter`` holds no persistent file handles or
    network sockets — its underlying ``RLDataLogger`` opens+closes the
    JSONL on every write via context manager — so clearing the dict is
    sufficient teardown. This helper exists so test fixtures can isolate
    state between tests and so any future shutdown hook has a single
    canonical reset path.
    """
    server._rl_telemetry_writers.clear()

def _rl_pack_linked_embs_for_node(
    node: dict,
    sibling_objs_by_source_id: dict[str, list],
    link_objs_by_title: dict[str, object],
    target_vector_name: str,
) -> tuple[list[list[float]], list[str]]:
    """Pack up to ``_RL_MAX_LINKED`` linked-slot vectors for one node.

    Order matches what ``paid-modules/vct-rl-reranker/retrieval_rl.py::_train_rl_model``
    builds online today (lines 914-918):

        linked_raws = extra_chunks_of_same_node + actual_linked_nodes
        linked_type_idxs_final = [n_type_idx] * len(extra_chunks) + linked_type_idxs

    Offline replay reads this exact order from the v3 event and feeds it
    into ``_rl_model.update(..., linked_raws=...)`` with NO repacking.

    Args:
        node: The retrieval result dict for which we're building linked_embs.
            Must carry ``source_node_id`` (or fall through to title) and
            ``chunk_number`` so we can filter out the matched chunk itself
            from the sibling pool. ``links`` (a list of wikilink titles)
            drives the actual_linked_nodes side.
        sibling_objs_by_source_id: Pre-fetched Weaviate objects keyed by
            ``source_node_id``. Each value is a list of objects from the
            same KG row family. Vectors are pulled via
            ``_extract_obj_vector(obj, target_vector_name)``.
        link_objs_by_title: Pre-fetched Weaviate objects keyed by ``title``,
            one per linked-node title we resolved.
        target_vector_name: Active named-vector slot (e.g. "qwen3_embed")
            for ``_extract_obj_vector`` to look up.

    Returns:
        ``(packed_embs, packed_type_names)`` — both lists length
        ≤ ``_RL_MAX_LINKED``. ``packed_type_names`` carries the per-slot
        ``node_type`` string the container resolves to an int via
        ``self._rl_model.get_type_idx(name)`` (D1' invariant: type
        indices are process-local; ALWAYS ship names across the wire).
    """
    packed_embs: list[list[float]] = []
    packed_types: list[str] = []
    node_type = node.get("node_type") or "concept"

    # Step 1: extra chunks of THIS node (same source_node_id, different chunk_num).
    # `_format_obj` exports this field as `source_id` (already resolved from
    # `obj.properties.source_node_id` at format time, falling through to title
    # when the property is absent). Read `source_id` first; fall back to the
    # raw `source_node_id` property name for callers that pass un-formatted
    # node dicts (none today, but keep the helper schema-agnostic).
    source_id = node.get("source_id") or node.get("source_node_id") or node.get("title") or ""
    matched_chunk = node.get("chunk_number")
    if source_id:
        siblings = sibling_objs_by_source_id.get(source_id) or []
        for sib in siblings:
            try:
                sib_chunk = sib.properties.get("chunk_num")
            except (AttributeError, KeyError):
                continue
            if sib_chunk == matched_chunk:
                continue  # the matched chunk itself; that's n_emb, not a linked slot
            vec = server._extract_obj_vector(sib, target_vector_name)
            if not vec:
                continue
            packed_embs.append(vec)
            packed_types.append(node_type)  # extra chunks share THIS node's type
            if len(packed_embs) >= server._RL_MAX_LINKED:
                return packed_embs, packed_types

    # Step 2: actual linked nodes (one vector per resolved link title).
    raw_links = node.get("links") or []
    for raw in raw_links:
        if len(packed_embs) >= server._RL_MAX_LINKED:
            break
        link_str = raw if isinstance(raw, str) else str(raw)
        # Strip wikilink decorations the typed-link parser may have left:
        # "uses::Tool" -> "Tool"; "[[Tool]]" -> "Tool".
        if "::" in link_str:
            link_str = link_str.split("::", 1)[1]
        link_str = link_str.strip().strip("[").strip("]")
        if not link_str:
            continue
        obj = link_objs_by_title.get(link_str)
        if obj is None:
            continue
        vec = server._extract_obj_vector(obj, target_vector_name)
        if not vec:
            continue
        packed_embs.append(vec)
        # Per-link node_type from the fetched object's properties; "concept" fallback.
        try:
            ltype = (obj.properties.get("node_type") or "concept") if hasattr(obj, "properties") else "concept"
        except (AttributeError, KeyError):
            ltype = "concept"
        packed_types.append(str(ltype))

    return packed_embs, packed_types

def _rl_regenerate_node_vector(text: str, model_name: str) -> "list[float] | None":
    """F-G (v0.2.70, CORRECTED): regenerate a node's embedding from its TEXT.

    Thin shim to the shared ``rl_client.embed_regen.regenerate_node_vector`` so
    the MCP enrich path AND the hook enrich path share ONE copy of this logic
    (modularity ruling). Passes the MCP's cached EmbeddingService so the
    regenerated vector lives in the active model's space (F-D invariant). See
    that module for the full rationale.
    """
    from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector
    return regenerate_node_vector(
        text, model_name, embedding_service=server._get_embedding_service()
    )

def _rl_refetch_node_vector(
    node: dict,
    sibling_objs_by_source_id: dict[str, list],
    link_objs_by_title: dict[str, object],
    target_vector_name: str,
    *,
    model_name: str = "",
    allow_regen: bool = True,
) -> "list[float] | None":
    """F-G (v0.2.70): recover a node's own active-slot vector for citation cosine.

    Used by ``_rl_enrich_nodes_with_linked_embs`` ONLY when the node has no
    ``emb`` already attached (the dominant ~96%-absent case: hook-path nodes
    that never ran search-time emb enrichment, and collapse-survivors whose
    winning chunk was keyword-only / an adjacent chunk). Pulls a representative
    chunk's vector from the SAME ``include_vector=True`` fetch_objects the
    enrich pass already issued — no extra Weaviate roundtrip.

    Selection order (deterministic):
      1. The sibling under the node's source_id whose ``chunk_num`` equals the
         node's matched ``chunk_number`` (the exact matched chunk = the truest
         n_emb). Single-chunk nodes (chunk_num=1, total_chunks=1) match here.
      2. Any first sibling under the source_id with a non-empty active-slot
         vector (covers legacy ``chunk_num=0``/absent storage — the maintainer's
         single-chunk-bookkeeping concern: treat absent/0/1 as a valid node,
         never skip).
      3. The title-keyed link object's vector (last resort, when the
         source_id index missed but the title fetch hit).
      4. F-G CORRECTED: REGENERATE the vector from the node's chunk TEXT (the
         fetched object's ``content``) using the active model, so cosine is
         ALWAYS computable for a node whose text we have. Only when this also
         fails (no text / embed service down) does the caller drop the node.
         GATED by ``allow_regen`` (v0.2.73): the caller sets it False when the
         active slot is absent across the whole group, so this synchronous
         Ollama embed is skipped rather than fired per-node (retrieval-lock
         guard — see ``_rl_enrich_nodes_with_linked_embs``' slot-presence probe).

    Pulls ONLY ``target_vector_name`` via ``_extract_obj_vector`` so a recovered
    vector can never come from a foreign embedding space (F-D invariant).
    Returns the vector list, or None when no comparable vector exists.
    """
    source_id = (
        node.get("source_id")
        or node.get("source_node_id")
        or node.get("title")
        or ""
    )
    matched_chunk = node.get("chunk_number")
    siblings = sibling_objs_by_source_id.get(source_id) or [] if source_id else []

    # 1. Exact matched chunk.
    if matched_chunk is not None:
        for sib in siblings:
            try:
                if sib.properties.get("chunk_num") == matched_chunk:
                    vec = server._extract_obj_vector(sib, target_vector_name)
                    if vec:
                        return vec
            except (AttributeError, KeyError, TypeError):
                continue

    # 2. Any sibling with a vector (covers absent/0 chunk_num — single-chunk
    #    nodes must be non-blocking; never gate on chunk metadata being >1).
    for sib in siblings:
        vec = server._extract_obj_vector(sib, target_vector_name)
        if vec:
            return vec

    # 3. Title-keyed link object fallback.
    title = node.get("title") or ""
    if title:
        link_obj = link_objs_by_title.get(str(title))
        if link_obj is not None:
            vec = server._extract_obj_vector(link_obj, target_vector_name)
            if vec:
                return vec

    # 4. F-G CORRECTED — regenerate from text so cosine is ALWAYS computable.
    # Prefer the fetched object's full ``content`` (matches the stored chunk
    # text), preferring the matched chunk; fall back to the node dict's content.
    #
    # v0.2.73 retrieval-lock guard: this step runs a SYNCHRONOUS Ollama embed
    # (blocking, on the retrieval coroutine). The caller passes
    # ``allow_regen=False`` when it has already determined the active slot is
    # absent on EVERY object in this group — in that state this would fire once
    # per node (up to limit*2 blocking embeds) and produce a citation vector
    # against a slot-mismatched collection that can't be trusted anyway. Skip
    # the regen; the node simply carries no comparable vector (same outcome as
    # a node fetched without include_vector).
    if not allow_regen:
        return None
    regen_text = ""
    for sib in siblings:
        try:
            if matched_chunk is not None and sib.properties.get("chunk_num") == matched_chunk:
                regen_text = sib.properties.get("content") or ""
                if regen_text:
                    break
        except (AttributeError, KeyError, TypeError):
            continue
    if not regen_text:
        for sib in siblings:
            try:
                regen_text = sib.properties.get("content") or ""
            except (AttributeError, KeyError, TypeError):
                regen_text = ""
            if regen_text:
                break
    if not regen_text and title:
        link_obj = link_objs_by_title.get(str(title))
        if link_obj is not None:
            try:
                regen_text = link_obj.properties.get("content") or ""
            except (AttributeError, KeyError, TypeError):
                regen_text = ""
    if not regen_text:
        regen_text = node.get("content") or ""
    if regen_text and model_name:
        regen = server._rl_regenerate_node_vector(regen_text, model_name)
        if regen:
            server.logger.info(
                "RL refetch: regenerated embedding from text for node %r "
                "(stored vector unavailable)",
                (title or source_id)[:60],
            )
            return regen
    return None

def _rl_find_representative_obj(
    node: dict,
    sibling_objs_by_source_id: dict[str, list],
    link_objs_by_title: dict[str, object],
):
    """Return the representative Weaviate object for a node (v0.2.71 Sweep-C).

    Same deterministic selection order ``_rl_refetch_node_vector`` uses to pick a
    vector, but returns the OBJECT itself so the dual-log path can (a) pull the
    OTHER slot's vector off it via ``_extract_obj_vector(obj, other_slot)`` and
    (b) read its UUID + ``content`` for an on-the-fly other-slot backfill — all
    from the SAME ``include_vector=True`` fetch the enrich pass already issued
    (no second Weaviate query, case-(a) 1:1 fan-out). Returns None when no
    representative object is indexed for the node.
    """
    source_id = (
        node.get("source_id")
        or node.get("source_node_id")
        or node.get("title")
        or ""
    )
    matched_chunk = node.get("chunk_number")
    siblings = sibling_objs_by_source_id.get(source_id) or [] if source_id else []

    # 1. Exact matched chunk (the truest representative).
    if matched_chunk is not None:
        for sib in siblings:
            try:
                if sib.properties.get("chunk_num") == matched_chunk:
                    return sib
            except (AttributeError, KeyError, TypeError):
                continue
    # 2. Any first sibling under the source_id.
    if siblings:
        return siblings[0]
    # 3. Title-keyed link object fallback.
    title = node.get("title") or ""
    if title:
        link_obj = link_objs_by_title.get(str(title))
        if link_obj is not None:
            return link_obj
    return None

def _rl_attach_other_slot_for_node(
    node: dict,
    rep_obj,
    *,
    other_slot: str,
    other_query_emb: "list[float] | None",
    other_model_name: str,
    backfill_other: bool,
    coll_for_backfill=None,
) -> None:
    """Attach ``emb_other`` / ``cos_qn_other`` to one node (v0.2.71 Sweep-C).

    Pulls the OTHER slot's vector off the already-fetched representative object.
    When the other slot is genuinely empty (node embedded BEFORE dual-write was
    enabled) AND ``backfill_other`` is on, generates it on-the-fly via the shared
    ``ensure_slot_embedding`` (model-aware chunking keyed on ``other_model_name``,
    async store-back) — the lazy "fill it" that replaces the "skip it" path. When
    backfill is off, a missing other slot leaves the node without ``emb_other``
    (the second event then drops it). Soft-fail throughout.
    """
    if rep_obj is None or not other_slot:
        return
    emb_other = server._extract_obj_vector(rep_obj, other_slot)
    if not emb_other and backfill_other and other_model_name:
        # Lazy on-use backfill: compute the OTHER slot's vector from the
        # representative object's stored content (full chunk) sized to the
        # OTHER model's preset, and schedule the store-back. The freshly
        # computed vector is used for THIS request immediately.
        try:
            uid = getattr(rep_obj, "uuid", None)
            content = ""
            try:
                content = rep_obj.properties.get("content") or ""
            except (AttributeError, KeyError, TypeError):
                content = ""
            if not content:
                content = node.get("content") or ""
            if content and uid is not None and coll_for_backfill is not None:
                from claude_mcp_servers.rl_client.embed_regen import (
                    ensure_slot_embedding,
                )

                svc = server._get_embedding_service()
                # Derive the short source tag from the slot so the other-model
                # embed routes to the right backend family (qwen3/arctic/openai).
                other_src = server._slot_short_source(other_slot)

                def _other_embed(sized_text: str):
                    return server._embed_text_in_other_model(
                        svc, sized_text, other_src, other_model_name
                    )

                emb_other = ensure_slot_embedding(
                    uid,
                    content,
                    other_slot,
                    other_model_name,
                    coll_for_backfill,
                    svc,
                    embed_fn=_other_embed,
                )
        except Exception as exc:  # noqa: BLE001
            server.logger.debug(
                "dual-log: other-slot backfill failed for node %r (%s)",
                (node.get("title") or "")[:60], exc,
            )
    if not emb_other:
        return
    node["emb_other"] = emb_other
    if other_query_emb:
        try:
            node["cos_qn_other"] = server._cosine(other_query_emb, emb_other)
        except Exception:  # noqa: BLE001
            pass

def _rl_enrich_nodes_with_linked_embs(
    nodes: list[dict],
    query_emb: "list[float] | None",
    active_slot: str,
    *,
    coll_resolver=None,
    model_name: str = "",
    other_slot: str = "",
    other_query_emb: "list[float] | None" = None,
    other_model_name: str = "",
    backfill_other: bool = False,
) -> None:
    """Attach v3 training fields to each node in-place.

    For every node (post-collapse, one entry per file):
      - ``n_emb``: best-chunk vector (the matched chunk; pulled from the
        node's existing ``emb`` field, which the search path already
        populated from Weaviate's returned object). Logging both ``emb``
        AND ``n_emb`` is redundant but cheap; offline_trainer prefers
        ``n_emb`` when both are present (v3 contract).
      - ``linked_embs``: MAX_LINKED packed vectors built by
        ``_rl_pack_linked_embs_for_node`` (extras_of_this_node + actual_links,
        truncated).
      - ``linked_type_names``: parallel array of per-slot node_type strings.
      - ``cos_qn``: max cos(query_emb, n_emb). Already present in many
        cases (set by the search path's per-result enrichment block at
        server.py:4179-4185); recomputed here only when missing.
      - ``cos_ql``: mean cos(query_emb, link_i) over linked_embs.
        Pre-computed scalar so offline replay matches online byte-identically
        without re-fetching link embeddings (the locked design from the
        2026-06-04 spec).
      - ``cos_nl``: mean cos(n_emb, link_i) over linked_embs. Same rationale.

    ONE batched Weaviate ``fetch_objects`` per collection (grouped by the
    ``collection`` field on each node dict) does the heavy lifting:

        Filter.by_property("source_node_id").contains_any([all source ids])
        | Filter.by_property("title").contains_any([all link titles])
        + include_vector=True

    Soft-fail: if Weaviate is unreachable or the fetch raises, the node
    keeps whatever fields were already set (typically just ``emb`` +
    ``cos_qn``); ``linked_embs`` stays absent and the v3 event ships a
    truncated payload. The offline trainer defaults missing ``linked_embs``
    to ``[]`` and the gradient step degenerates to "no linked-slot input"
    — same as a node with no actual links would produce.

    Args:
        nodes: Mutable list of per-node dicts (post-collapse). Modified
            in place — function returns None.
        query_emb: Active-slot query vector. When None, ``cos_qn`` /
            ``cos_ql`` are not computed.
        active_slot: Named-vector slot to pull from Weaviate objects
            (e.g. ``"qwen3_embed"``).
        coll_resolver: Optional callable ``(collection_name) -> coll
            handle`` for testing. Defaults to ``get_weaviate_client()
            .collections.get(name)``.
        other_slot: v0.2.71 Sweep-C dual-log — the OTHER embedding slot
            (e.g. ``"qwen3_embed"`` on an arctic-active install). When set,
            each node also gets ``emb_other`` (the OTHER slot's per-node
            vector pulled from the SAME already-fetched objects — no second
            Weaviate query, case-(a) 1:1 fan-out) and ``cos_qn_other``
            (cos(other_query_emb, emb_other)). Empty → no dual-log enrichment.
        other_query_emb: The query vector in the OTHER slot's space, used to
            compute ``cos_qn_other``. Required for ``cos_qn_other``; absent →
            ``emb_other`` may still be attached, ``cos_qn_other`` is skipped.
        other_model_name: Model id for the OTHER slot (drives the chunk preset
            for the on-the-fly backfill). Only consulted when ``backfill_other``.
        backfill_other: When True AND a node's other slot is genuinely empty
            (node embedded BEFORE dual-write was enabled), compute the OTHER
            slot's vector on-the-fly via ``ensure_slot_embedding`` (model-aware
            chunking) and store it back async. Gated by the caller to dual-write
            installs only — the lazy "fill it" that replaces the "skip it" path.
            False → a missing other slot is simply skipped (node has no
            ``emb_other`` and is dropped from the second event).
    """
    if not nodes:
        return

    # Group nodes by collection so we can issue ONE fetch per collection.
    by_collection: dict[str, list[dict]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        c = str(n.get("collection") or "")
        if not c:
            continue
        by_collection.setdefault(c, []).append(n)

    if not by_collection:
        return

    if coll_resolver is None:
        try:
            client = server.get_weaviate_client()
        except Exception as exc:
            server.logger.debug("RL enrich: get_weaviate_client failed (%s); skipping", exc)
            return

        def coll_resolver(name: str):  # noqa: E306 — local fallback
            return client.collections.get(name)

    # Import Filter lazily; the search path already does this elsewhere.
    try:
        from weaviate.classes.query import Filter  # type: ignore
    except Exception as exc:
        server.logger.debug("RL enrich: Filter import failed (%s); skipping", exc)
        return

    for collection_name, group_nodes in by_collection.items():
        # Skip collections whose schema doesn't match the KG row shape this
        # helper expects. The Code* family (CodeFunction / CodeClass / CodeAPI /
        # CodeInteraction / CodeModule) has no `source_node_id`, no `links`,
        # and (per `_format_obj`'s "Untitled" default) all rows would group
        # under a single bogus source_id — every per-call enrichment would
        # then crash inside fetch_objects (caught by the per-group try/except
        # below but logging debug-level noise on every search). Pre-filtering
        # here is cleaner. See KG node concepts/code-graph-schema for the
        # canonical schema reference.
        if collection_name.startswith("Code"):
            continue

        # Collect identifiers we'll need from Weaviate.
        # Read `source_id` (the `_format_obj` output field name; resolved at
        # format time from `obj.properties.source_node_id` with title fallback)
        # first; fall back to raw `source_node_id` for callers that pass
        # un-formatted node dicts.
        source_ids: list[str] = []
        link_titles: list[str] = []
        for n in group_nodes:
            sid = n.get("source_id") or n.get("source_node_id") or n.get("title")
            if sid:
                source_ids.append(str(sid))
            for raw in n.get("links") or []:
                link_str = raw if isinstance(raw, str) else str(raw)
                if "::" in link_str:
                    link_str = link_str.split("::", 1)[1]
                link_str = link_str.strip().strip("[").strip("]")
                if link_str:
                    link_titles.append(link_str)

        # Dedup before sending across the wire (Weaviate accepts repeats but
        # the in-process post-filter dicts only key by unique strings).
        unique_source_ids = list({s for s in source_ids if s})
        unique_link_titles = list({t for t in link_titles if t})

        if not unique_source_ids and not unique_link_titles:
            continue

        # Build filter: source_node_id in [...] OR title in [...].
        try:
            filt = None
            if unique_source_ids:
                filt = Filter.by_property("source_node_id").contains_any(unique_source_ids)
            if unique_link_titles:
                title_filt = Filter.by_property("title").contains_any(unique_link_titles)
                filt = title_filt if filt is None else (filt | title_filt)
        except Exception as exc:
            server.logger.debug("RL enrich: filter build failed (%s); skipping group", exc)
            continue

        # ONE Weaviate roundtrip for this collection. Generous limit upper-bound:
        # MAX_LINKED chunks per node + actual links per node, both capped.
        max_rows = len(group_nodes) * (server._RL_MAX_LINKED * 2 + 8)
        try:
            coll = coll_resolver(collection_name)
            resp = coll.query.fetch_objects(
                filters=filt,
                include_vector=True,
                limit=max_rows,
            )
            fetched = list(resp.objects)
        except Exception as exc:
            server.logger.debug(
                "RL enrich: fetch_objects on %s failed (%s); skipping group",
                collection_name, exc,
            )
            continue

        # Index by source_node_id (for sibling chunks) and by title (for actual links).
        sibling_objs_by_source_id: dict[str, list] = {}
        link_objs_by_title: dict[str, object] = {}
        for obj in fetched:
            try:
                props = obj.properties
            except AttributeError:
                continue
            sid = props.get("source_node_id")
            if sid:
                sibling_objs_by_source_id.setdefault(str(sid), []).append(obj)
            title = props.get("title")
            if title:
                # title -> single object; if multiple chunks of the same title
                # land in the result, the highest-chunk-num one wins (arbitrary
                # but deterministic). For linked-node embeddings the chunk
                # choice is asymmetric — we want ONE representative emb per
                # linked node.
                existing = link_objs_by_title.get(str(title))
                if existing is None:
                    link_objs_by_title[str(title)] = obj
                else:
                    try:
                        if (props.get("chunk_num") or 0) < (
                            getattr(existing, "properties", {}).get("chunk_num") or 0
                        ):
                            continue
                        link_objs_by_title[str(title)] = obj
                    except (AttributeError, TypeError):
                        pass

        # SLOT-PRESENCE PROBE (v0.2.73 retrieval-lock fix): does ANY fetched
        # object in this group actually carry the active named-vector slot?
        # When the collection was embedded under a different/renamed slot (or
        # predates the active slot), _extract_obj_vector returns None for every
        # object (F-D: never fall back to a foreign slot). In that state the
        # per-node step-4 fallback in _rl_refetch_node_vector would fire a
        # SYNCHRONOUS Ollama re-embed for EVERY node — up to `limit*2` blocking
        # embeds serialized on the retrieval coroutine = the "lock on retrieval"
        # the maintainer hit. Detect it ONCE here and disable the per-node
        # text-regen for this group (steps 1-3 already can't find a vector, so
        # step 4 is the only thing that would run, and re-embedding against a
        # slot-mismatched collection yields a citation vector that can't be
        # trusted anyway). One WARN instead of N blocking embeds.
        slot_present_in_group = any(
            server._extract_obj_vector(obj, active_slot) is not None
            for obj in fetched
        )
        if fetched and not slot_present_in_group:
            server.logger.warning(
                "RL enrich: active slot %r absent on all %d fetched objects in "
                "%s — skipping per-node text-regen (would block the retrieval "
                "on %d synchronous embeds). Collection likely embedded under a "
                "different slot / predates the active model; re-analyze to "
                "backfill.",
                active_slot, len(fetched), collection_name, len(group_nodes),
            )

        # Per-node enrichment.
        for n in group_nodes:
            packed_embs, packed_types = server._rl_pack_linked_embs_for_node(
                n,
                sibling_objs_by_source_id,
                link_objs_by_title,
                active_slot,
            )
            n["linked_embs"] = packed_embs
            n["linked_type_names"] = packed_types

            # n_emb: prefer the existing emb (already on the dict from search).
            n_emb = n.get("emb")

            # F-G (v0.2.70): when `emb` is absent — the dominant case on the
            # hook path (rl_kg_search.py never runs the search-time emb
            # enrichment) and on any node whose collapse-winning chunk was a
            # keyword-only / adjacent chunk — RE-PULL the matched node's own
            # active-slot vector from THIS collection's already-fetched objects
            # (the same include_vector=True fetch_objects above that built the
            # sibling/link indices). Deep-bugsweep BUG #2 measured n_emb absent
            # on ~96% of retrievals, which makes cosine citations structurally
            # impossible no matter how the lifecycle (F-A/B) is fixed. Pulling
            # the vector here, at citation-cache populate time, closes that gap
            # without an extra Weaviate roundtrip.
            #
            # Match a representative chunk for the node: prefer the matched
            # chunk_number, else the first sibling under the node's source_id,
            # else (no source_id index hit) the title-keyed link object. The
            # vector comes from the SAME active_slot the answer will be embedded
            # with — never a foreign slot (F-D invariant via _extract_obj_vector).
            if not n_emb:
                refetched = server._rl_refetch_node_vector(
                    n, sibling_objs_by_source_id, link_objs_by_title, active_slot,
                    model_name=model_name or server.EMBEDDING_MODEL,
                    # Only allow the step-4 synchronous text-regen when the slot
                    # is actually present somewhere in this group. If it's absent
                    # everywhere, steps 1-3 can't succeed for ANY node and step 4
                    # would block the retrieval on N Ollama embeds (see the
                    # slot-presence probe above). Steps 1-3 still run (cheap,
                    # no-op here) so a stray per-node hit is still recovered.
                    allow_regen=slot_present_in_group,
                )
                if refetched:
                    n_emb = refetched
                    n["emb"] = refetched  # mirror so _build_log_nodes carries it

            if n_emb:
                n["n_emb"] = n_emb

            # cos_qn (re-)compute when we have both inputs.
            if query_emb and n_emb and "cos_qn" not in n:
                try:
                    n["cos_qn"] = server._cosine(query_emb, n_emb)
                except Exception:
                    pass

            # cos_ql = mean cos(query, link_i)
            if query_emb and packed_embs:
                try:
                    cosines = [server._cosine(query_emb, e) for e in packed_embs]
                    n["cos_ql"] = sum(cosines) / max(len(cosines), 1)
                except Exception:
                    pass

            # cos_nl = mean cos(n_emb, link_i)
            if n_emb and packed_embs:
                try:
                    cosines = [server._cosine(n_emb, e) for e in packed_embs]
                    n["cos_nl"] = sum(cosines) / max(len(cosines), 1)
                except Exception:
                    pass

            # v0.2.71 Sweep-C dual-log: attach the OTHER slot's per-node vector
            # (emb_other / cos_qn_other) from the SAME already-fetched objects.
            # Case-(a) 1:1 fan-out — no second Weaviate query. When the other
            # slot is empty AND backfill is on (dual-write install), generate it
            # on-the-fly + store back (lazy fill replacing skip). Soft-fail.
            if other_slot:
                try:
                    rep_obj = server._rl_find_representative_obj(
                        n, sibling_objs_by_source_id, link_objs_by_title
                    )
                    server._rl_attach_other_slot_for_node(
                        n,
                        rep_obj,
                        other_slot=other_slot,
                        other_query_emb=other_query_emb,
                        other_model_name=other_model_name,
                        backfill_other=backfill_other,
                        coll_for_backfill=coll,
                    )
                except Exception as exc:  # noqa: BLE001
                    server.logger.debug(
                        "dual-log: other-slot enrich failed for node %r (%s)",
                        (n.get("title") or "")[:60], exc,
                    )

def _resolve_dual_rl_log_enabled() -> bool:
    """Whether dual-RL-log fan-out is on for this process (v0.2.71 Sweep-C).

    HARD precondition: dual-log only makes sense when dual-WRITE is on, because
    the second event needs the OTHER slot's per-node vectors which ONLY exist
    when ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` populated them (or the lazy backfill
    fills them). So this returns True iff BOTH:

      1. ``DUAL_RL_LOG_ENABLED`` env is truthy, AND
      2. ``_resolve_write_all_slots()`` (the dual-WRITE gate) is True.

    If dual-log is requested but dual-write is OFF, this returns False (forced
    off) — the second event is never emitted (see the tests). TODO(T-B-flags):
    the launcher.db → ProjectConfig → settings.json projection of this flag is a
    SEPARATE track; this reads the env channel only (the canonical override for
    CLI/dev + what settings.json mirrors), NOT launcher.db directly — do not add
    a DB read here.
    """
    raw = os.environ.get(server.DUAL_RL_LOG_ENABLED_ENV, "").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return False
    try:
        from vco_lib.embedding_service import _resolve_write_all_slots

        if not _resolve_write_all_slots():
            server.logger.debug(
                "dual-log requested but dual-write (DUAL_EMBEDDING_WRITE_ALL_SLOTS) "
                "is OFF; forcing dual-log off (the other slot's vectors won't exist)"
            )
            return False
    except Exception as exc:  # noqa: BLE001
        server.logger.debug("_resolve_dual_rl_log_enabled: write-gate probe raised (%s)", exc)
        return False
    return True

async def _resolve_dual_rl_log_inputs(
    query: str, active_slot: str
) -> "dict | None":
    """Resolve the OTHER-slot inputs for the dual-log fan-out (v0.2.71 Sweep-C).

    Returns a dict ``{other_slot, other_source, other_dim, other_model,
    other_query_emb}`` when dual-log is on AND a single distinct OTHER text slot
    is resolvable, else None (caller does the bare single-log path). The OTHER
    slot is whatever ``embed_text_all_configured(query)`` returns that is NOT the
    active slot — so the second query vector comes from the SAME canonical embed
    fan-out the dual-WRITE path uses (no third embed implementation). The other
    slot's (source, model, dim) is derived via the EmbeddingService slot maps.

    Soft-fail: any resolver error → None (no dual-log this call), never raises.
    """
    if not server._resolve_dual_rl_log_enabled():
        return None
    try:
        svc = server._get_embedding_service()
        if svc is None:
            return None
        # embed_text_all_configured returns {active_slot: vec} PLUS the secondary
        # slots (only when dual-write is on — already guaranteed by the gate).
        slots = await asyncio.to_thread(svc.embed_text_all_configured, query)
    except Exception as exc:  # noqa: BLE001
        server.logger.debug("_resolve_dual_rl_log_inputs: embed fan-out raised (%s)", exc)
        return None
    if not slots:
        return None
    # Pick the OTHER slot: the single non-active text slot with a vector.
    others = [
        (slot, vec) for slot, vec in slots.items() if slot != active_slot and vec
    ]
    if not others:
        return None
    # Deterministic pick when multiple secondaries exist (e.g. qwen3 + openai):
    # the first by sorted slot name. Multiple secondaries is an edge case; the
    # offline corpus is partitioned by source so picking one is correct (the
    # others simply aren't dual-logged this cycle).
    others.sort(key=lambda kv: kv[0])
    other_slot, other_query_emb = others[0]
    try:
        from vco_lib.embedding_service import TEXT_SLOT_MAP
    except Exception as exc:  # noqa: BLE001
        server.logger.debug("_resolve_dual_rl_log_inputs: slot-map import raised (%s)", exc)
        return None
    # Resolve the other slot's short source + model + dim. Map slot -> source
    # short tag the SAME way EmbeddingService.text_model_short_id does, and
    # model/dim via the first TEXT_SLOT_MAP entry that targets this slot.
    other_source = server._slot_short_source(other_slot)
    other_dim = 0
    for _substr, slot, dim in TEXT_SLOT_MAP:
        if slot == other_slot:
            other_dim = dim
            break
    other_model = server._other_model_for_source(other_source)
    if not other_dim:
        other_dim = server._embedding_dim_for(other_model)
    return {
        "other_slot": other_slot,
        "other_source": other_source,
        "other_dim": other_dim,
        "other_model": other_model,
        "other_query_emb": other_query_emb,
    }

def _slot_short_source(slot: str) -> str:
    """Map a named-vector slot to its short RL embedding-source tag.

    Mirrors ``EmbeddingService.text_model_short_id``'s slot-driven dispatch
    (the canonical mapping) so the dual-log other-slot event carries the SAME
    short tag the offline loader partitions by.
    """
    s = (slot or "").lower()
    if s == "qwen3_embed":
        return "qwen3"
    if s == "arctic2_embed":
        return "arctic"
    if s == "openai_text_embed":
        return "openai"
    if s == "codesage_embed":
        return "codesage"
    if "arctic" in s:
        return "arctic"
    if "qwen3" in s:
        return "qwen3"
    if "openai" in s:
        return "openai"
    return "legacy"

async def _rl_cache_and_rerank(
    task_id: str,
    query: str,
    all_nodes: list[dict],
    limit: int,
    *,
    failure_mode: str | None = None,
    failed_collections: list[str] | None = None,
    query_emb: list[float] | None = None,
    dual_log_inputs: "dict | None" = None,
) -> list[dict]:
    """
    V52-J Edit 2 (2026-06-09): thin adapter that delegates to the canonical
    rerank-and-emit pipeline in ``claude_mcp_servers.rl_client.search_pipeline``.

    The pre-v0.2.52 body (tier gate + per-project toggle + rerank + telemetry +
    citation cache populate, all inline) has been moved verbatim into
    ``search_pipeline.rerank_and_emit()`` so every KG-search entry point
    (MCP ``hybrid_search`` / ``semantic_graph_search``, the CLI scripts
    ``rl_kg_search.py`` and ``search_knowledge.py``, PreToolUse hooks that
    invoke those CLIs) shares one canonical chokepoint instead of each
    re-implementing the orchestration. See ``search_pipeline.py`` module
    docstring for full rationale.

    Call sites (4 of them in this file, search ``_rl_cache_and_rerank``)
    only care about the returned ``list[dict]`` — they don't see the
    ``RerankResult`` diagnostic fields (``rl_used``, ``emit_success``)
    at all. The pre-v0.2.52 inline body was preserved as
    ``_rl_cache_and_rerank_LEGACY_v0251`` for one commit during the
    refactor and removed in the follow-up; full history in git log.
    """
    # Lazy import keeps the rl_client → server.py edge from becoming a
    # circular dep at module-load time (search_pipeline lazy-imports back
    # into this module to reach _rl_node_content_cache et al).
    from claude_mcp_servers.rl_client.search_pipeline import (
        RerankRequest,
        rerank_and_emit,
    )

    # v0.2.71 Sweep-C dual-log: when the caller resolved the other-slot inputs
    # (dual-log on AND dual-write on AND a distinct other slot exists), thread
    # them into the request so the pipeline emits the second (other-slot) event
    # on a ``:slot``-suffixed task_id. The per-node ``emb_other`` / ``cos_qn_other``
    # were attached upstream by ``_rl_enrich_nodes_with_linked_embs`` on these
    # same dicts. ``dual_log_inputs is None`` → bare single-log path (unchanged).
    di = dual_log_inputs or {}
    dual_log = bool(di)
    req = RerankRequest(
        query=query,
        candidates=all_nodes,
        limit=limit,
        query_emb=query_emb,
        embedding_source=server.EMBEDDING_SOURCE,
        embedding_dim=server._embedding_dim_for(server.EMBEDDING_MODEL),
        embedding_model=server.EMBEDDING_MODEL,
        task_id=task_id,
        task_type="mcp_interactive",
        failure_mode=failure_mode,
        failed_collections=failed_collections or [],
        dual_log=dual_log,
        other_query_emb=di.get("other_query_emb"),
        other_embedding_source=di.get("other_source", ""),
        other_embedding_dim=di.get("other_dim", 0),
        other_embedding_model=di.get("other_model", ""),
    )
    result = await rerank_and_emit(req)
    return result.ranked
