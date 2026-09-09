#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Knowledge Graph Weaviate Sync Script

Syncs knowledge graph markdown files to Weaviate collection.
Called by Claude hooks after file edits in knowledge/ directory.

Handles chunking for large files (>6k tokens) to stay within embedding model limits.

Usage:
    python .claude/scripts/sync_knowledge_graph.py <file_path>
    python .claude/scripts/sync_knowledge_graph.py --all  # Sync all knowledge files
    python .claude/scripts/sync_knowledge_graph.py --project-root <path> --all
        # v0.2.89: pin the TARGET project root explicitly (outranks every
        # env channel — the supported way to run a manual cross-project sync)

v0.2.92 WP-B1 — the COMPLETE flag vocabulary (any other `-`-prefixed argv
token is a hard usage error, exit 2, BEFORE any backend connection):

    --all            sync knowledge/ + docs/ trees, then refresh summaries
    --all-docs       sync docs/ only
    --check-drift    read-only drift scan (detect, never repairs)
    --rechunk        force the chunk-plan comparison for this run even when
                     NO chunker-revision crossing is pending in the deferral
                     ledger — the by-hand remedy for a re-cloned /
                     manifest-deleted project, which the revision gate
                     classifies as fresh and therefore never arms via the
                     ledger (MAJOR-R5-4). Entries whose stored plan already
                     matches still skip; only stale plans re-embed.
    --project-root <path> | --project-root=<path>   pin the target root
                     (consumed at import; at most once)
    -h | --help      print usage

Exit codes: 0 = clean (skips are fine) · 1 = per-node/per-doc sync
failures · 2 = usage error or refused project root. Positional args are
sync targets (file list). The `.sh`/`.ps1` wrappers are dumb forwarders —
flags are validated HERE, in ONE home, so the vocabulary cannot drift
between the three entry points.
"""

import sys

# v0.2.49 Bug L: reconfigure stdout/stderr to UTF-8 so emoji + non-ASCII
# error messages don't crash on Windows cp1252 consoles. Without this,
# `print(f"❌ ...")` raises UnicodeEncodeError on the default Windows
# Python console (which inherits the system codepage, often cp1252 on
# Western European installs). The launcher's `installer.rs` sets
# PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 on every Python child it
# spawns (v0.2.27 fix), but kg-sync invokes this script directly from
# a Windows shell without those env vars. Reconfiguring here defends
# against the direct-CLI path. `errors='backslashreplace'` ensures we
# never crash even if the terminal can't render a character — it'll
# print the escape sequence instead.
#
# Python 3.7+ supports the `reconfigure` method on TextIOWrapper.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    # AttributeError: stdout/stderr not a TextIOWrapper (rare, e.g.
    # captured by pytest or redirected to a non-tty). OSError: stream
    # already detached/closed. Both are benign — fall through, and any
    # subsequent emoji print may still crash on Windows-cp1252-direct
    # invocations, but at least we tried.
    pass

import os
import re
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Mapping
import uuid

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# v0.2.92 (R4/R21) — INSTALL-TIME BAKED ROOT. `vco_lib/rewire.py` substitutes
# the placeholder below when this file is installed into a project, so the
# installed script can reach its orchestrator clone with NOTHING in the
# environment (the `_PROJECT_HOME` fallback below is the USER project root on
# an install, which has no vco_lib/). In the clone the placeholder stays
# literal, `Path("{{ORCHESTRATOR_ROOT}}")/"vco_lib"` is not a directory, and
# this block is inert — the validation IS the placeholder guard.
# It is used ONLY when NEITHER env pin ($VCT_ORCHESTRATOR_ROOT,
# $VCT_INSTALL_ROOT) names a real orchestrator root — a VALID pin always
# wins, a provably stale one is healed — and the ladder below is unchanged.
_VCO_BAKED_ORCHESTRATOR_ROOT = "{{ORCHESTRATOR_ROOT}}"
_vco_env_pins = [os.environ.get(_k, "").strip()
                 for _k in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT")]
if (Path(_VCO_BAKED_ORCHESTRATOR_ROOT) / "vco_lib").is_dir() and not any(
    _p and (Path(_p) / "vco_lib").is_dir() for _p in _vco_env_pins
):
    os.environ["VCT_ORCHESTRATOR_ROOT"] = _VCO_BAKED_ORCHESTRATOR_ROOT

# Resolve vco_lib (lives next to claude_mcp_servers/ in the orchestrator clone).
# EmbeddingService is the v0.2.18 central dispatcher for embedding calls.
#
# weaviate_mcp is pip-installed as an editable package by install.py
# (A1, v0.2.38), so `from weaviate_mcp.chunking import TokenCounter` works
# without a sys.path entry.  vco_lib IS pip-installable too (pyproject
# `packages = ["vco_lib"]`), so this arm is the fallback for a Python that
# is NOT the install's venv — a bare `python3 .claude/scripts/...`.
# Resolution order for vco_lib:
#   1. $VCT_ORCHESTRATOR_ROOT               (set by .claude/env)
#   2. <project_home> in-tree fallback      (orchestrator clone)
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_HOME = _SCRIPT_DIR.parent.parent  # .claude/scripts/X → .claude → project

_env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
if _env_root and Path(_env_root).is_dir():
    _VCO_LIB_PARENT = Path(_env_root)
else:
    _VCO_LIB_PARENT = _PROJECT_HOME
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))
# VCO-REWIRE-END: orchestrator-root-resolution


# ──────────────────────────────────────────────────────────────────────
# v0.2.89 BUG 3 (Windows field audit): explicit project-root resolution.
#
# Pre-fix, the target root came from the inherited `KG_BASE_DIR` env with a
# script-location fallback. Every Claude Code session exports `KG_BASE_DIR`
# (via `.claude/settings.json env`), so a wrapper run from a session whose
# env belongs to ANOTHER project inherited the foreign root — and because
# `_resolve_collections()` keyed the hub resolver off the same value, the
# sync misrouted BOTH the walked tree AND the target collections coherently:
# it "succeeded" against the wrong project with zero diagnostics.
#
# Two legitimate callers disagree about who knows the true root:
#   * The LAUNCHER already sets the root env correctly — and relies on it
#     for the v0.2.77 orchestrator-copy wrapper fallback (when the
#     project-local wrapper is missing, the ORCHESTRATOR's wrapper runs,
#     whose location-derived root would be the orchestrator clone — wrong).
#   * A DIRECT CLI run from a foreign session has a POISONED env; the
#     wrapper's own location is correct.
#
# A single channel cannot serve both, so the fix is layered precedence with
# a NEW, non-leaking env name (set ONLY by the launcher and the kg-sync
# wrappers, never exported by Claude sessions → "set ⇒ deliberate"):
#
#   1. --project-root <path>   argv   (explicit human/tooling intent)
#   2. KG_SYNC_PROJECT_ROOT    env    (launcher + wrappers only — non-leaking)
#   3. KG_BASE_DIR             env    (LEGACY; still honored, logged as such)
#   4. script location                (_PROJECT_HOME)
#
# `main()` prints an unconditional resolution banner naming the root AND the
# channel it came from, and refuses (`exit 2`) to run a tree sync against a
# root that has neither `knowledge/` nor a docs root — converting silent
# wrong-tree runs into diagnosable failures.
# ──────────────────────────────────────────────────────────────────────


def _extract_cli_project_root(argv: "List[str]") -> "Optional[Path]":
    """Extract and REMOVE ``--project-root <path>`` / ``--project-root=<path>``
    from *argv* (mutates the list in place). Returns the path or None.

    Runs at module import — BEFORE ``_resolve_collections()`` and the
    ``PROJECT_ROOT`` assignment — so ``main()``'s manual positional dispatch
    (``--all`` / ``--all-docs`` / explicit file list) never sees the flag and
    stays untouched. A missing/empty value is a hard usage error (exit 2):
    an explicit flag pointing at nothing is always a caller mistake.
    """
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--project-root":
            if i + 1 >= len(argv) or not argv[i + 1].strip():
                print("❌ --project-root requires a path argument", file=sys.stderr)
                sys.exit(2)
            value = argv[i + 1]
            del argv[i:i + 2]
            return Path(os.path.abspath(value))
        if tok.startswith("--project-root="):
            value = tok.split("=", 1)[1]
            if not value.strip():
                print("❌ --project-root= requires a non-empty path", file=sys.stderr)
                sys.exit(2)
            del argv[i]
            return Path(os.path.abspath(value))
        i += 1
    return None


_CLI_PROJECT_ROOT: "Optional[Path]" = _extract_cli_project_root(sys.argv)


def _extract_rechunk_flag(argv: "List[str]") -> bool:
    """Extract and REMOVE every ``--rechunk`` token from *argv* (mutates
    the list in place). Returns True when the flag was present.

    Why the flag exists: the plan comparison that re-chunks stale-boundary
    entries is armed by :func:`_chunker_resync_pending`, which reads the
    deferral ledger — and a re-cloned / manifest-deleted project is
    classified FRESH by the revision gate, never receives that entry, and
    so was told by an old KNOWN_ISSUES remedy (``kg-sync --all``) to run a
    repair that hash-skipped everything. ``--rechunk`` makes the remedy
    TRUE for every population: the comparison runs for THIS run; entries
    whose stored plan already matches the current chunker still skip (no
    blind re-embed), everything else re-chunks under the current revision.

    Runs at module import — same pattern as ``--project-root`` — so the
    token is gone before ``_validate_argv_flags`` and ``main()``'s manual
    dispatch. Repeat occurrences are idempotent (all removed, one boolean).

    The ``tok == "--flag"`` comparison below is deliberate, not incidental:
    this script has no argparse, so the emitted-remediation gate
    (``tests/test_deferral_command_argparse_sweep.py``) recovers its accepted
    flags by SOURCE-MATCHING that exact idiom on the pre-scanners. Written any
    other way the flag is real at runtime but invisible to the gate, and the
    gate then reports every doc that names it as an invalid remediation — which
    is exactly what happened when this function first landed. Keep the shape
    aligned with :func:`_extract_cli_project_root`.
    """
    saw = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--rechunk":
            del argv[i]
            saw = True
            continue
        i += 1
    return saw


#: True when this run was invoked with ``--rechunk`` (MAJOR-R5-4): forces
#: the chunk-plan comparison even with no revision crossing pending in the
#: deferral ledger. Set ONCE at import by :func:`_extract_rechunk_flag`,
#: which also removes the token from ``sys.argv`` so ``main()``'s dispatch
#: and flag validation never see it.
_RECHUNK_FORCED = _extract_rechunk_flag(sys.argv)


def _resolve_project_root() -> "Tuple[Path, str]":
    """Resolve the target project root with the v0.2.89 layered precedence.

    Returns ``(root, source)`` where *source* names the channel that won —
    printed by ``main()``'s resolution banner so a misrouted run is
    diagnosable from its output. Empty/whitespace-only env values are
    treated as unset (skip to the next channel).
    """
    if _CLI_PROJECT_ROOT is not None:
        return _CLI_PROJECT_ROOT, "--project-root"
    new_env = os.environ.get("KG_SYNC_PROJECT_ROOT", "").strip()
    if new_env:
        return Path(new_env), "KG_SYNC_PROJECT_ROOT"
    legacy = os.environ.get("KG_BASE_DIR", "").strip()
    if legacy:
        return Path(legacy), "KG_BASE_DIR (legacy env — consider --project-root)"
    return _PROJECT_HOME, "script location"


# Resolved ONCE at module top so `_resolve_collections()` below keys the hub
# resolver off the SAME root the sync walks (the pre-fix asymmetry between
# the walked tree and the collection names is what made BUG 3 silent).
PROJECT_ROOT, _PROJECT_ROOT_SOURCE = _resolve_project_root()

# v0.2.52 (Known Issue 6, Sub-issue A): silence
# ``AuthlibDeprecationWarning: authlib.jose module is deprecated`` from
# ``weaviate-client``'s transitive ``authlib`` dep during module import.
# Without this filter the warning lands in the user's terminal on every
# fresh ``install.py`` KG-seed run, which is alarming (and is the warning
# user-reported as Known Issue 6).  MUST run BEFORE ``import weaviate``.
# See ``claude_mcp_servers/weaviate_mcp/server.py`` for the matching
# filter at the MCP-server level.
import warnings as _kg_warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDeprecationWarning  # type: ignore
    _kg_warnings.filterwarnings("ignore", category=_AuthlibDeprecationWarning)
except ImportError:
    _kg_warnings.filterwarnings(
        "ignore",
        message=r".*authlib.*deprecated.*",
        category=DeprecationWarning,
    )

# NOTE: this bare `import weaviate` is a DELIBERATE, load-bearing side-effect
# import — NOT dead code (do not "clean it up"). It must appear AFTER the
# AuthlibDeprecationWarning filter block above and BEFORE any other weaviate
# submodule import, because importing weaviate transitively imports authlib,
# which force-installs `simplefilter('always', AuthlibDeprecationWarning)` at
# import time. Pinned by tests/test_no_authlib_deprecation_warning.py
# (::test_sync_knowledge_graph_filter_block_present). The v0.2.77 Part 7a
# connect-helper convergence removed the only *runtime* `weaviate.` reference,
# so ruff now flags F401 — suppressed here because the import's value is the
# import-time side effect, not the bound name.
import weaviate  # noqa: F401
from weaviate.classes.query import Filter
# W8 (v0.2.92): `Chunker` is no longer imported here — this script does not
# construct one any more. The plan (gate + boundaries) comes from
# `kg_chunk_plan.plan_node_chunks`, which binds its OWN sibling `Chunker`;
# a second binding here could resolve to a different checkout's chunking
# module and re-open the very divergence W8 closed.
from weaviate_mcp.chunking import TokenCounter

# v0.2.18: central embedding dispatcher. Replaces the inline Ollama call
# that was hardcoded to qwen3-embedding (and threw RuntimeError when
# ACTIVE_EMBEDDING was anything else — audit finding KG-W1, 2026-04-30).
# EmbeddingService.for_project() picks the right backend (ollama / openai)
# AND the right named-vector slot (qwen3_embed / openai_text_embed /
# arctic2_embed / ...) from the environment, so this script no longer
# cares about ACTIVE_EMBEDDING or EMBEDDING_MODEL directly.
from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)
# W3 (v0.2.92 wiring audit): the truncation-tag properties are derived by the
# ONE shared stamper (vco_lib home — importable from every writer layer), so
# kg-sync, the MCP store, and the single-slot patch writers cannot drift.
from vco_lib.kg_truncation_tags import truncation_tag_properties
# v0.2.94: the named-vector slot has ONE home. The WRITER (this script) and
# every READER (detect_duplicates.py, search_knowledge.py, the MCP write path)
# resolve it through the same helper, so a scan cannot target a slot the sync
# never populated — the divergence that left the duplicate scanner querying no
# slot at all on multi-vector collections.
from vco_lib.kg_vector_slot import active_text_vector_slot  # noqa: E402 — must follow the AuthlibDeprecationWarning filter block above, like every import in this group
# v0.2.92 WP-B1 (D13): the canonical file_path shape helper. `to_posix_rel`
# is pure + dependency-free (see its docstring); importing it loudly here
# (never an inline copy) because every Weaviate write below must store ONE
# shape so delete-by-file_path upserts stay idempotent across OSes.
from vco_lib.paths import to_posix_rel

# Try to import query logger.
#
# v0.2.81 FN-2: target the SHIPPED `weaviate_mcp.query_logger` package
# module and DROP the dead `.claude/logs` sys.path insert — that dir holds
# no query_logger.py, so the pre-fix bare `from query_logger import ...`
# could never resolve → HAS_LOGGER silently False on every install → every
# kg-sync (the hook that fires on knowledge/ edits) lost its telemetry
# row. The guard is retained because telemetry is optional-by-design (a
# partial install must still sync the node); with the package target it
# passes on every healthy install and only trips on a broken one.
try:
    from weaviate_mcp.query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False

# Configuration - Read from environment variables (set by MCP servers or project settings)
# Note (v0.2.18 + v0.2.52 V52-AJ): EMBEDDING_MODEL / ACTIVE_EMBEDDING are NOT
# read here directly. They are resolved by `EmbeddingService.for_project()`
# which consults (in order):
#   1. `os.environ[ACTIVE_EMBEDDING / EMBEDDING_MODEL]` — explicit caller env.
#   2. `launcher.db app_state[embedding.active_profile]` — what the
#      launcher's Identity tab + install.py preset chooser stored.
#   3. `"qwen3"` final fallback (free-tier install, no launcher).
# Install.py threads the resolved env into this script's subprocess on
# fresh / --update runs (via `_subprocess_env_with_embedding` in install.py),
# so this script sees a non-empty ACTIVE_EMBEDDING even when the user
# shell has no such env set — this is the fix for the Windows + CPU-only
# stuck-at-40-with-qwen3 install bug (v0.2.52 V52-AJ, 2026-06-09).
# Keeping the env names in `_redacted_env_snapshot()` failure log helps
# diagnose drift.
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))


# v0.2.21 Step 18 (caller migration): resolve project-scoped collection
# names via the launcher's vct-hub. Falls back to env vars when the hub
# is unreachable (launcher not running, project not registered). The
# resolver emits its own rate-limited warning so callers don't need to
# log anything extra. See `.claude/context/plans/v0.2.21-resolver-design.md`.
def _resolve_collections() -> tuple[str, str, str]:
    """Return (kg_collection, development_collection, shared_kg_collection)
    via hub, env-fallback.

    The hub resolver is authoritative when reachable (v0.2.21 contract: the
    launcher's per-project resolution wins over ambient env, so a stale env
    var can't misroute the normal in-project sync). The resolver is queried
    against the TARGET PROJECT ROOT — the SAME ``PROJECT_ROOT`` the sync
    walks, resolved at module top with the v0.2.89 layered precedence
    (``--project-root`` argv > ``KG_SYNC_PROJECT_ROOT`` env > legacy
    ``KG_BASE_DIR`` env > script location):

      * Normal in-project run: no explicit channel is set, so we resolve
        from the script's location → the project's own config.
      * Manual cross-project seed: run the script by hand against a
        DIFFERENT project with ``--project-root <path>`` (the supported
        channel since v0.2.89; the legacy ``KG_BASE_DIR`` env export still
        works but is outranked by the explicit flag and by the launcher's
        ``KG_SYNC_PROJECT_ROOT``). The hub is resolved against that root so
        the collection names match the project whose tree is actually being
        walked — keying the resolver off the target root preserves the
        file-root vs collection-name symmetry without inverting hub
        precedence.

    v0.2.89 BUG 6: the third element is the shared-KG collection name
    (``cfg.shared_kg_collection``, env fallback ``SHARED_KG_COLLECTION``,
    empty allowed) — consumed by the ``scope: shared`` frontmatter routing
    in ``sync_node``.

    VCT_DISABLE_HUB_RESOLVER short-circuit for the test session. See
    ``server.py::_try_resolve_project_config`` for the matching guard +
    ``tests/conftest.py`` for the autouse fixture.
    """
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("DEVELOPMENT_COLLECTION", ""),
            os.getenv("SHARED_KG_COLLECTION", ""),
        )
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        # Resolve against the TARGET project root (v0.2.89 BUG 3: the same
        # module-top resolution the sync walks — argv/env/location layered).
        cfg = resolve(PROJECT_ROOT)
        return (
            cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            cfg.development_collection or os.getenv("DEVELOPMENT_COLLECTION", ""),
            cfg.shared_kg_collection or os.getenv("SHARED_KG_COLLECTION", ""),
        )
    except Exception:
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("DEVELOPMENT_COLLECTION", ""),
            os.getenv("SHARED_KG_COLLECTION", ""),
        )


COLLECTION_NAME, _RESOLVED_DEV_COLLECTION, SHARED_COLLECTION_NAME = (
    _resolve_collections()
)
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Chunking configuration for embedding limits.
#
# v0.2.28 (2026-05-23): every embedding model gets a chunk size tuned to its
# context window instead of the pre-v0.2.28 hardcoded `max_tokens=2500`
# (correct for qwen3-embedding:0.6b, but over-chunking a 512-token model ~5x
# and under-using a 32k one).
#
# W8 (v0.2.92 wiring audit): the plan itself — the single-vs-multi gate AND
# the boundaries — is `_plan_for(server, content)` below, one call into
# `weaviate_mcp.kg_chunk_plan.plan_node_chunks`, shared with the MCP
# `store_knowledge_node` write, the `--rechunk` plan comparison and the
# shipped-sidecar generator. The old module-level `MAX_EMBEDDING_TOKENS = 2500`
# fallback lives there now as `LEGACY_FALLBACK_MAX_TOKENS`: two writers with
# two *different* no-model fallbacks (2 500 here vs 2 000 in the MCP) is
# exactly the divergence W8 removed, so there is one number and it is not
# here.


def _active_model_id(server) -> str:
    """The ACTIVE text model id this sync embeds with, or ``""``.

    ONE resolution home for every plan helper below. The channel is
    ``server.embedding_service.text_model_id`` — the id the service will
    actually embed with, which ``EmbeddingService.for_project()`` sets from
    ``resolve_active_text_model_id()``, the SAME resolver the MCP store's
    ``_active_chunk_model_id()`` uses. So both W8 writers plan for the
    model that embeds, and for the same one. ``""`` (no server / no
    service — test harnesses that construct chunks directly) selects the
    shared module's legacy fallback preset.
    """
    try:
        return server.embedding_service.text_model_id  # type: ignore[attr-defined]
    except Exception:
        return ""


def _plan_for(server, content: str, *, source_id: str = "",
              metadata: "Optional[dict]" = None):
    """The chunk plan for ``content`` — W8's ONE computation.

    Both KG WRITE paths in this script (``sync_node``, ``sync_doc``) call
    THIS, and so does the MCP ``store_knowledge_node`` and the
    ``--rechunk`` plan comparison (``_stored_plan_matches_current``), via
    ``weaviate_mcp.kg_chunk_plan.plan_node_chunks``. Deriving the gate and
    the boundaries from two separate calls is what let the two writers
    drift in the first place; there is now one call and one answer.
    """
    from weaviate_mcp.kg_chunk_plan import plan_node_chunks as _shared_plan
    return _shared_plan(
        content, _active_model_id(server),
        source_id=source_id, metadata=metadata or {},
    )


# ──────────────────────────────────────────────────────────────────────
# v0.2.92 — chunk-plan transition repair (revision-crossing re-chunk).
#
# `_CHUNKER_REVISION` (weaviate_mcp/chunking.py) moved from v0.2.88 to
# v0.2.92 with the qwen3 chunk budget clamped 13 500 → 8 192 counter-units.
# Rows written under the old budget keep their old boundaries (for content
# between the two budgets: silently Ollama-truncated vectors). The embed-skip
# gate above cannot see this: `chunk_count_ok` compares the stored rows
# against THEMSELVES, so a boundary change is invisible and every entry is
# skipped forever. While the project's deferral ledger carries
# `chunker_preset_overhaul_pending` — emitted by the revision gate
# (`vco_lib.chunker_revision.gate` / the launcher's R2-4 boot flow) exactly
# when the sentinel crossed — the skip additionally requires the stored
# plan to match what the CURRENT chunker would produce for this content.
# Entries whose plan is unchanged still skip (the overwhelming majority:
# measured 17 of 998 across templates/knowledge + the maintainer's private
# knowledge/ + repo docs/ for this transition); only changed plans pay an
# embed. The per-run plan CPU for all 998 files measured 11 ms total.
# ──────────────────────────────────────────────────────────────────────

#: The deferral condition the revision gate emits on a `_CHUNKER_REVISION`
#: crossing. Its presence in the project ledger is the ONE durable signal
#: that a boundary change is pending repair — no new app_state key or
#: kg_syncs column is needed: the ledger already carries exactly this
#: "crossing detected, remedy owed" state, and it is what the user is told
#: to act on. It clears through its existing lifecycle (next update
#: reconcile), at which point the comparison stops being paid.
_CHUNKER_RESYNC_CID = "chunker_preset_overhaul_pending"

#: Lazily-filled per-process cache of the ledger probe (the ledger does not
#: change during one sync run; a fresh process re-probes).
_resync_pending_cache: "Optional[bool]" = None

#: Run-level count of entries re-embedded ONLY because their stored chunk
#: plan predates the current chunker revision (mirrors _SHARED_ROUTED_COUNT).
_RECHUNKED_COUNT = 0


def _chunker_resync_pending() -> bool:
    """True while a chunker-revision crossing is pending repair for THIS
    project (the deferral ledger carries `chunker_preset_overhaul_pending`),
    OR this run passed ``--rechunk`` (MAJOR-R5-4).

    Scoped to the transition by design (brief constraint): an install
    already at the current revision has no such entry — the revision gate
    emits it only on a crossing — so the plan comparison is never paid
    there. An UNREADABLE ledger is "cannot determine whether a repair is
    owed", and the conservative direction is to run the comparison (pure
    CPU, milliseconds; it can only repair, never damage) rather than skip
    it and freeze stale boundaries.

    ``--rechunk`` overrides the ledger signal for ONE run: a re-cloned /
    manifest-deleted project never receives the entry (the revision gate
    classifies it fresh), so the by-hand remedy must be able to arm the
    comparison without it. The comparison is still a comparison — entries
    whose stored plan matches the current chunker keep skipping.
    """
    global _resync_pending_cache
    if _RECHUNK_FORCED:
        # Checked BEFORE the cache: the cache is only ever written on the
        # ledger path, and --rechunk must arm the comparison even when the
        # cached ledger answer is False.
        return True
    if _resync_pending_cache is not None:
        return _resync_pending_cache
    try:
        from vco_lib.deferral_report import DeferralReport

        pending = DeferralReport.read(PROJECT_ROOT).has_condition(
            _CHUNKER_RESYNC_CID
        )
    except Exception:  # noqa: BLE001 — cannot determine → compare (cheap)
        pending = True
    _resync_pending_cache = pending
    return pending


def _stored_plan_matches_current(
    server: "WeaviateMCPServer",
    collection,
    canonical_fp: str,
    content: str,
    stored_row_count: int,
) -> bool:
    """True only when the STORED chunk rows are provably identical to what
    the CURRENT chunker would produce for ``content``.

    Compares the chunk DECISION (single vs multi, via the same
    ``_plan_for`` gate the write path uses) and, for multi-chunk
    plans, the boundaries themselves (stored chunk contents vs the current
    chunker's chunk contents, chunk_num by chunk_num) — a budget change can
    move boundaries WITHOUT changing the count, and a count-only check
    would skip exactly those rows (measured: a 20 763-unit node re-plans
    3→3 chunks with different boundaries).

    "Could not determine" is NOT "current": a missing/non-int ``chunk_num``,
    a row-set that changed between the two fetches, or ANY planner error
    returns False, so the caller falls through to re-embed — the repair
    runs rather than a skip that would freeze stale boundaries. Raises are
    NOT caught here on purpose: the embed-skip block's existing soft-fail
    ``except`` turns any raise into the same conservative fall-through.
    """
    # W8: the comparison plans through the SAME call the two WRITE paths in
    # this script make (``_plan_for`` → ``kg_chunk_plan.plan_node_chunks``,
    # which the MCP store also calls), so what this judges "current" against
    # is by construction what either writer stored.
    _plan = _plan_for(server, content, source_id="plan-check")
    plan_total = _plan.total
    plan_contents: List[str] = (
        [] if _plan.is_single else [c.content for c in _plan.chunks]
    )

    if stored_row_count != plan_total:
        return False
    if plan_total == 1:
        return True

    # Multi-chunk plan with a matching row COUNT — the boundaries must
    # match too. Second fetch pulls just this entry's chunk contents.
    fetched = collection.query.fetch_objects(
        filters=_file_path_filter(canonical_fp),
        limit=100,
        return_properties=["chunk_num", "content"],
    )
    numbered: List[Tuple[int, str]] = []
    for obj in fetched.objects:
        props = obj.properties or {}
        num = props.get("chunk_num")
        if not isinstance(num, int) or isinstance(num, bool):
            return False  # cannot order the rows → cannot judge → re-embed
        numbered.append((num, props.get("content")))
    if len(numbered) != plan_total:
        return False  # row set changed between fetches → not judgeable
    numbered.sort(key=lambda pair: pair[0])
    if [n for n, _ in numbered] != list(range(1, plan_total + 1)):
        return False  # duplicate/gap in chunk_num → not judgeable
    return all(
        stored_content == plan_content
        for (_, stored_content), plan_content in zip(numbered, plan_contents)
    )

# Project root — resolved ONCE at module top (v0.2.89 BUG 3, see
# `_resolve_project_root` above) with the layered precedence
# `--project-root` argv > `KG_SYNC_PROJECT_ROOT` env > legacy `KG_BASE_DIR`
# env > script location. `PROJECT_ROOT` / `_PROJECT_ROOT_SOURCE` are
# assigned right after the resolver definition so `_resolve_collections()`
# keys the hub off the SAME root the sync walks.
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

# Development docs collection (project-scoped). Uses the same chunker, named
# vectors, and `index_null_state=True` schema as the KG collection — the only
# differences are: docs may have no frontmatter, no typed WikiLinks, no
# tags/status (we synthesize a small set from the filesystem).
# v0.2.21 Step 18: name resolved alongside COLLECTION_NAME via the hub
# (_resolve_collections above); env-fallback preserved.
DEV_COLLECTION_NAME = _RESOLVED_DEV_COLLECTION

# v0.2.70 FIX #6 (enhancement): the dev-docs root defaults to ``docs/`` but
# can be overridden via the ``DEV_DOCS_ROOT`` env var for projects that keep
# their documentation under a different folder (e.g. ``documentation/``). The
# value may be a bare subdirectory name (joined under PROJECT_ROOT) or an
# absolute path. Empty / unset → the historical ``docs/`` default, so existing
# projects are unaffected.
_dev_docs_root_env = os.getenv("DEV_DOCS_ROOT", "").strip()
if _dev_docs_root_env:
    _dev_docs_candidate = Path(_dev_docs_root_env)
    DOCS_ROOT = (
        _dev_docs_candidate
        if _dev_docs_candidate.is_absolute()
        else PROJECT_ROOT / _dev_docs_candidate
    )
else:
    DOCS_ROOT = PROJECT_ROOT / "docs"

# v0.2.18: named-vector slot is resolved per-instance from
# `EmbeddingService.text_vector_slot` (see WeaviateWrapper below). The
# old `_KG_NAMED_VECTOR_SLOTS` tuple + `_active_named_vector_for_kg()`
# qwen3-only assertion were removed — they predated the central
# dispatcher and silently broke arctic/openai installs (audit finding
# KG-W1, 2026-04-30, fixed in v0.2.18).


class WeaviateWrapper:
    """Weaviate client + EmbeddingService bundle.

    Replaces the v0.2.17 ``WeaviateWrapper`` which hardcoded
    ``qwen3-embedding:0.6b`` for every embed call. v0.2.18: the embed
    backend + active named-vector slot are resolved from environment by
    ``EmbeddingService.for_project()`` — supports ollama (qwen3 / arctic /
    mxbai / nomic), openai (text-embedding-3-small / -large), and any
    future model registered in the embedding-service slot maps.

    Lifecycle: instantiate once at script entry, call ``close()`` (or use
    as context manager) to release HTTP sessions on both the Weaviate
    client and the embedding service.
    """
    def __init__(self, weaviate_url, embedding_service, grpc_port=None):
        # v0.2.77 Part 7a: connect via the shared connect_v4 factory. Behaviour
        # is preserved exactly — plaintext HTTP (http_secure=False), the same
        # gRPC-port fallback, and skip_init_checks=False (this batch path always
        # did the startup readiness handshake and we don't change that here).
        from vco_lib import weaviate_helpers as _wh
        self.client = _wh.connect_v4(
            weaviate_url,
            grpc_port=grpc_port or 50051,
            http_secure=False,
            skip_init_checks=False,
        )
        # The EmbeddingService is the single source of truth for: which
        # model to call, which named-vector slot to write, whether the
        # backend is currently reachable, and whether to fan out to
        # multiple slots (multi-slot enrichment writes).
        self.embedding_service = embedding_service

    @property
    def text_vector_slot(self) -> str:
        """Active named-vector slot for KG writes (e.g. 'qwen3_embed').

        v0.2.94: resolved through the ONE home (``vco_lib.kg_vector_slot``)
        that every READER now shares — the duplicate scanner, the MCP write
        path and the KG search CLI. The service stays authoritative here (it
        is the object that produces the vectors); the shared helper only
        guarantees the writer and the readers cannot name different slots.
        """
        return active_text_vector_slot(self.embedding_service)

    def close(self) -> None:
        """Close the Weaviate connection (and embedding HTTP session)
        to prevent resource leaks."""
        try:
            self.client.close()
        except Exception:
            pass
        # The EmbeddingService is closed by main() — it may be shared
        # across multiple wrappers in future, so we don't auto-close it
        # here.

    def _get_embedding(self, text: str) -> List[float]:
        """Embed via the active text backend (ollama / openai / ...)."""
        return self.embedding_service.embed_text(text)

    def _get_all_kg_embeddings(self, text: str) -> Dict[str, List[float]]:
        """Embed into every CONFIGURED + REACHABLE text backend.

        Returns ``{slot_name: vector}``. Used for the enrichment-migration
        write path: when the user switches text model (e.g. qwen3 →
        openai), this populates BOTH slots on every new node so search
        continues to work with either model active.

        Empty dict on total backend failure (caller decides whether to
        skip or fail).
        """
        return self.embedding_service.embed_text_all_configured(text)

    def _get_all_kg_embeddings_tagged(
        self, text: str
    ) -> "Tuple[Dict[str, List[float]], List[str]]":
        """Tagged variant: the vectors AND the per-call truncation record.

        W3 (v0.2.92 wiring audit): ``_build_vector_arg`` persists the
        record as the ``truncated_slots`` / ``secondary_truncated_slots``
        chunk properties, so EVERY kg-sync-written row (the install-time
        seed, every post-file-edit hook sync, the ``--rechunk`` remedy)
        carries the partition capability — pre-fix only the MCP
        multi-chunk branch tagged, which is ~no rows a real user has.

        Delegates to ``EmbeddingService.embed_text_all_configured_tagged``
        — the ONE atomic capture (same call, before returning), NEVER the
        derived ``last_*_truncated`` properties (race-prone under
        concurrency). Under DUAL_EMBEDDING_ENABLED=false the underlying
        fan-out returns exactly the ACTIVE slot, so the flat-vector legacy
        branch uses this too and gains the same record.
        """
        return self.embedding_service.embed_text_all_configured_tagged(text)


# For backward compatibility
WeaviateMCPServer = WeaviateWrapper


def _content_signature_excluding_updated(content: str) -> str:
    """Return a SHA256 of the file content excluding the `updated:` line.

    Used to detect whether a re-sync actually contains substantive changes
    or just an unchanged file passing through the post-file-edit hook. If
    the signature is unchanged, the `updated:` timestamp is not bumped —
    avoiding KG-wide timestamp churn on every install run (v0.2.14).

    STORAGE-LAYER hash, intentionally distinct from the RETRIEVAL content-
    identity hash (rl_client/content_dedup.content_sha, sha1[:12]): this one
    answers "is the stored object unchanged so I can skip the re-embed" and
    carries the deliberate "exclude the `updated:` line" nuance so a timestamp-
    only edit hashes identically. The retrieval hash answers "drop this
    duplicate before it reaches Claude". The v0.2.70 dedup triage keeps them
    separate on purpose — do NOT converge.
    """
    if not content.strip().startswith('---'):
        return _sha256_text(content)
    parts = content.split('---', 2)
    if len(parts) < 3:
        return _sha256_text(content)
    fm_text = parts[1]
    body = parts[2]
    # Strip the `updated:` line from the frontmatter for the signature.
    fm_no_updated = re.sub(r'^updated:.*$\n?', '', fm_text, flags=re.MULTILINE)
    return _sha256_text(fm_no_updated + body)


def _sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def _embedding_failures_jsonl_hint() -> str:
    """The jsonl path to name in a failure message — and MAKE TRUE.

    Two things were wrong with the literal this replaces. It named
    ``~/.claude/metrics/embedding_failures.jsonl``, which v0.2.92 W7 turned
    into a FROZEN ARCHIVE (the live stream is ``vct_metrics_dir()``, i.e.
    ``~/.vct/metrics/``), and the file it named contains outage rows written
    only when ``EmbeddingService.for_project()`` raises at CONSTRUCTION —
    never the case here, where construction SUCCEEDED and every per-slot
    embed failed at call time. So the pointer was doubly false for exactly
    the population it was shown to.

    This resolves the real path AND appends the matching outage row through
    the shared writer, so the message and the file agree. Soft-fails to the
    canonical string if either step is unavailable — a broken hint must
    never replace the caller's real error.
    """
    try:
        from vco_lib.embedding_fidelity import append_outage_row
        from vco_lib.paths import vct_metrics_dir

        append_outage_row(
            "kg-sync: no embedding backend produced a vector for a KG write "
            "(service constructed, every configured slot failed at call time)"
        )
        return str(vct_metrics_dir() / "embedding_failures.jsonl")
    except Exception:  # noqa: BLE001 — never mask the real failure
        return "<vct-state-dir>/metrics/embedding_failures.jsonl"


def _build_vector_arg(
    server: "WeaviateMCPServer",
    text: str,
) -> "Tuple[object, Mapping[str, List[float]], Optional[List[str]]]":
    """Embed *text* and shape it for `Weaviate.collection.data.insert(vector=)`.

    Returns ``(vector_arg, slots_map, truncated_slots)`` (W3: the third
    element is the per-call truncation record — the sorted names of every
    slot whose vector came from a bounded leading sub-window, the ACTIVE
    slot included — captured ATOMICALLY with the vectors; the caller stamps
    it on the stored row via ``truncation_tag_properties``). The record
    names ONLY slots this call actually STORES, and is ``None`` when the
    stored vector did not come from the tagged capture at all (see the
    legacy branch below) — the stamper then writes no property and the row
    resolves UNKNOWN rather than claiming a fidelity nobody measured.

    Behaviour:
      * ``DUAL_EMBEDDING_ENABLED=true`` (default) → multi-slot write.
        Calls ``server._get_all_kg_embeddings_tagged()`` which fans out to
        every reachable backend (qwen3 always tried; openai if the key is
        valid). The returned ``vector_arg`` is a ``{slot: vec}`` dict,
        and ``slots_map`` is the same dict (so the caller can log which
        slots got populated).
        Failure modes:
          - All backends fail → empty dict → raises RuntimeError so the
            caller's exception handler kicks in and counts a failure.
          - Active backend fails but a fallback succeeds → dict only has
            the fallback's slot. Search will still work with the
            fallback's model. The caller logs which slots landed.
      * ``DUAL_EMBEDDING_ENABLED=false`` (legacy) → single flat vector
        from the active backend. ``vector_arg`` is a ``list[float]``,
        ``slots_map`` is ``{slot_name: vec}`` so logging stays uniform.
        The tagged capture still runs (with the default write-all-slots
        toggle OFF the fan-out IS exactly the ACTIVE embed the flat path
        took), so the record is persisted in this mode too — narrowed to
        the ACTIVE slot, because that is the only vector this branch
        stores; a secondary the fan-out happened to embed has no vector on
        this row and must not appear in its record. On a total failure the
        original flat-path behaviour (raise) is preserved, and when the
        untagged fallback embed supplies the vector the record is ``None``
        (no honest answer exists for it).
    """
    if DUAL_EMBEDDING_ENABLED:
        slots, truncated = server._get_all_kg_embeddings_tagged(text)
        if not slots:
            raise RuntimeError(
                "No embedding backend produced a vector. "
                f"See {_embedding_failures_jsonl_hint()} for details."
            )
        return slots, slots, list(truncated)
    # Legacy flat-vector path (DUAL_EMBEDDING_ENABLED=false).
    slots, truncated = server._get_all_kg_embeddings_tagged(text)
    active_slot = server.text_vector_slot
    vec = slots.get(active_slot) if slots else None
    if vec is None:
        # Tagged gather produced no active-slot vector (service down →
        # inline fallback shapes, or a total failure): fall back to the
        # original flat embed, whose failure semantics (raise) the caller
        # has always relied on. That embed is NOT covered by the capture,
        # so no honest record exists for the vector actually stored.
        fallback_vec = server._get_embedding(text)
        return fallback_vec, {active_slot: fallback_vec}, None
    return vec, {active_slot: vec}, [s for s in truncated if s == active_slot]


# ──────────────────────────────────────────────────────────────────────
# v0.2.70 Part 2: pre-shipped embedding INGEST support.
#
# A future orchestrator update may ship pre-computed embeddings for the
# curated KG nodes alongside the (already-shipped) summaries, so a 3rd-party
# install does not have to re-embed all ~117 curated nodes on first sync
# (the arctic-on-CPU install-hang class). The vectors live in a per-slot
# sidecar under knowledge/:
#
#     knowledge/.node_embeddings.<slot>.json   (one file per named-vector slot)
#
# Format (schema_version 1) — see knowledge/.node_embeddings.README.txt:
#     {
#       "schema_version": 1,
#       "slot": "qwen3_embed",                 # the named-vector slot these
#                                              #   vectors belong to
#       "model_id": "qwen3-embedding:0.6b",    # informational / provenance
#       "dim": 1024,                           # informational / provenance
#       "nodes": {
#         "<signature>": {                     # _content_signature_excluding_
#                                              #   updated (FULL 64-hex sha256,
#                                              #   `updated:` line excluded) —
#                                              #   NOT the 16-hex summary
#                                              #   content_hash
#           "total_chunks": 1,
#           "chunks": [
#             {"chunk_num": 1, "vector": [<float>, ...]}
#           ]
#         },
#         ...
#       }
#     }
#
# v0.2.70 shipped the plumbing only (no data file → strict NO-OP). v0.2.89
# ships the data for the bundled curated set (qwen3_embed + arctic2_embed
# sidecars, root-only materialization) AND extends ingest with the §8.5
# dual-slot merge: an active-slot hit also pulls every OTHER configured
# slot's sidecar hit for the same signature/chunk into the {slot: vec} map
# (never computing for a missing secondary). Absent sidecars still mean
# "compute locally" — nothing breaks without the data.
#
# Two non-negotiable guards on ingest (the cross-model invariant from the
# v0.2.70 same-active-slot ruling):
#   (a) STALENESS  — the shipped vector's content_hash MUST equal the node's
#                    CURRENT content signature. A stale vector (node edited
#                    since the vector was computed) is never ingested.
#   (b) SLOT-MATCH — the sidecar's slot MUST equal the install's ACTIVE
#                    named-vector slot. A qwen3 vector is NEVER written into
#                    an arctic install (and vice-versa) — that would mix
#                    embedding spaces and silently corrupt search.
# Either guard failing → fall back to computing the embedding (today's
# behaviour). The sidecar is loaded once and cached per (knowledge_root, slot).
# ──────────────────────────────────────────────────────────────────────

#: Cache: (knowledge_root_str, slot) -> parsed sidecar dict OR None (absent /
#: unreadable / slot-mismatch). None is cached too, so a missing sidecar is
#: probed at most once per run.
_SHIPPED_EMBED_CACHE: Dict[Tuple[str, str], Optional[dict]] = {}


def _shipped_embeddings_path(knowledge_root: Path, slot: str) -> Path:
    """Path to the per-slot shipped-embeddings sidecar under knowledge/."""
    return knowledge_root / f".node_embeddings.{slot}.json"


def _load_shipped_embeddings(knowledge_root: Path, slot: str) -> Optional[dict]:
    """Load the shipped-embeddings sidecar for *slot*, or None.

    Returns None (cached) when the sidecar is absent, unparseable, schema-
    incompatible, or declares a DIFFERENT slot than requested (slot-mismatch
    guard at the file level — a defensive second check on top of the
    filename, in case a file is mis-named). Soft-fail: never raises.
    """
    key = (str(knowledge_root), slot)
    if key in _SHIPPED_EMBED_CACHE:
        return _SHIPPED_EMBED_CACHE[key]

    result: Optional[dict] = None
    path = _shipped_embeddings_path(knowledge_root, slot)
    try:
        if path.is_file():
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and int(data.get("schema_version", 0)) == 1
                and isinstance(data.get("nodes"), dict)
                # File-level slot guard: the declared slot must match the slot
                # we were asked for. A mismatch means this file is for another
                # model — treat as absent (never cross-model ingest).
                and data.get("slot") == slot
            ):
                result = data
    except Exception:
        # Corrupt JSON, permission error, etc. — treat as absent. The embed
        # path computes vectors as usual; nothing breaks.
        result = None

    _SHIPPED_EMBED_CACHE[key] = result
    return result


def _shipped_slot_chunk_vector(
    knowledge_root: Path,
    slot: str,
    content_hash: str,
    chunk_num: int,
    expected_chunks: int,
) -> Optional[List[float]]:
    """Pure per-(slot, chunk) sidecar lookup with EVERY ingest guard applied.

    One home for the guard chain shared by ``_shipped_vector_for`` (single-
    object path, chunk 1) and ``_shipped_chunk_vector`` (multi-chunk path),
    and reused verbatim for the v0.2.89 §8.5 secondary-slot merge. Returns
    the validated vector or None when ANY guard fails:

      * no sidecar for *slot* (absent / unparseable / slot-mismatched file),
      * no entry for *content_hash* (staleness guard: a vector computed
        against a now-edited node is never reused),
      * the entry's chunk count != *expected_chunks* (the node would chunk
        differently than the shipped vectors cover),
      * no chunk with this 1-indexed *chunk_num*, or its vector is
        missing / empty / non-numeric (incl. a non-int ``chunk_num`` field
        in the sidecar — malformed data falls back to compute, never raises).
    """
    data = _load_shipped_embeddings(knowledge_root, slot)
    if data is None:
        return None

    entry = data["nodes"].get(content_hash)
    if not isinstance(entry, dict):
        return None
    chunks = entry.get("chunks")
    if not isinstance(chunks, list) or not chunks or len(chunks) != expected_chunks:
        return None

    target = None
    for c in chunks:
        try:
            if isinstance(c, dict) and int(c.get("chunk_num", -1)) == chunk_num:
                target = c
                break
        except (TypeError, ValueError):
            continue  # malformed chunk_num → skip this chunk entry
    if target is None:
        return None
    return _coerce_vector(target.get("vector"))


def _shipped_secondary_slots(knowledge_root: Path, active_slot: str) -> List[str]:
    """Discover NON-active slots that ship a sidecar under *knowledge_root*.

    v0.2.89 §8.5: candidates come from the ``.node_embeddings.<slot>.json``
    files actually present (each is still subject to the file-level slot
    guard in ``_load_shipped_embeddings``). Slots are filtered against the
    ``KG_NAMED_VECTORS`` CODE catalog — the static set of slots the codebase
    can ever configure — NOT the live collection schema. A pre-v0.2.18
    collection could therefore still lack a catalog slot and fail the
    insert; that residual exposure is identical to the dual-write COMPUTE
    path's (which populates the same catalog slots without a schema check),
    so shipped ingest is no worse than a normal sync there. A live-schema
    intersection was considered and skipped: an extra schema roundtrip per
    run buys protection only for that near-nil pre-v0.2.18 case. Soft-fail:
    any error returns [] (active-slot-only behaviour, exactly pre-v0.2.89).
    """
    slots: List[str] = []
    try:
        catalog: Optional[set] = None
        try:
            from vco_lib.weaviate_schema import KG_NAMED_VECTORS
            catalog = {s.name for s in KG_NAMED_VECTORS}
        except Exception:
            catalog = None  # no catalog available → accept discovered slots
        prefix = ".node_embeddings."
        suffix = ".json"
        for p in sorted(knowledge_root.glob(f"{prefix}*{suffix}")):
            name = p.name[len(prefix):-len(suffix)]
            if not name or name == active_slot:
                continue
            if catalog is not None and name not in catalog:
                continue
            slots.append(name)
    except Exception:
        return []
    return slots


def _merge_secondary_shipped_slots(
    knowledge_root: Path,
    active_slot: str,
    slots: "Dict[str, List[float]]",
    content_hash: str,
    chunk_num: int,
    expected_chunks: int,
) -> None:
    """v0.2.89 §8.5 dual-slot ingest merge (mutates *slots* in place).

    When the ACTIVE slot had a shipped hit, also look up every OTHER
    configured slot's sidecar for the SAME signature/chunk and merge hits
    into the ``{slot: vec}`` map — mirroring what the compute path's
    multi-backend fan-out would have populated. NEVER computes for a
    missing secondary (the v0.2.70 "never synthesise" rule): a secondary
    with no valid entry simply stays unpopulated, exactly as a
    partial-backend compute run leaves it. Only called when
    ``DUAL_EMBEDDING_ENABLED`` (the legacy single-slot mode keeps its
    active-slot-only shape).
    """
    for other_slot in _shipped_secondary_slots(knowledge_root, active_slot):
        vec2 = _shipped_slot_chunk_vector(
            knowledge_root, other_slot, content_hash, chunk_num, expected_chunks
        )
        if vec2 is not None:
            slots[other_slot] = vec2


def _shipped_vector_for(
    server: "WeaviateMCPServer",
    knowledge_root: Path,
    content_hash: str,
    expected_chunks: int,
) -> Optional[Tuple[object, Mapping[str, List[float]]]]:
    """Return a ready ``(vector_arg, slots_map)`` from the shipped sidecar, or None.

    Mirrors ``_build_vector_arg``'s return shapes so the embed path can drop
    in the shipped vector with no other changes:

      * ``DUAL_EMBEDDING_ENABLED`` (default) → ``({slot: vec, ...}, same)``.
        The active slot's hit is REQUIRED; on that hit, every other
        configured slot's sidecar is consulted for the same signature/chunk
        and merged (v0.2.89 §8.5 — see ``_merge_secondary_shipped_slots``).
        Secondaries are best-effort: a miss is NEVER computed for.
      * legacy single-slot mode → ``(vec_list, {slot: vec})`` — the active
        slot only, matching the legacy ``_build_vector_arg`` shape.

    Returns None (→ caller computes the embedding) when any guard fails for
    the ACTIVE slot — see ``_shipped_slot_chunk_vector`` for the guard
    chain. The active slot is ``server.text_vector_slot``, so a vector is
    only ever placed in the slot whose embedding space matches the model
    that produced it (the never-cross-model invariant); the merged
    secondaries carry their OWN slots' vectors, so the invariant holds for
    them too. This function is the single-object path (``expected_chunks``
    must be 1); the multi-chunk path goes through ``_shipped_chunk_vector``.
    """
    if expected_chunks != 1:
        return None

    slot = server.text_vector_slot
    vec = _shipped_slot_chunk_vector(
        knowledge_root, slot, content_hash, chunk_num=1,
        expected_chunks=expected_chunks,
    )
    if vec is None:
        return None  # no sidecar / stale / malformed → compute (pre-.89 path)

    if DUAL_EMBEDDING_ENABLED:
        slots: Dict[str, List[float]] = {slot: vec}
        _merge_secondary_shipped_slots(
            knowledge_root, slot, slots, content_hash,
            chunk_num=1, expected_chunks=expected_chunks,
        )
        return slots, slots
    return vec, {slot: vec}


def _shipped_chunk_vector(
    server: "WeaviateMCPServer",
    knowledge_root: Path,
    content_hash: str,
    chunk_num: int,
    expected_chunks: int,
) -> Optional[Tuple[object, Mapping[str, List[float]]]]:
    """Per-chunk variant of ``_shipped_vector_for`` for the multi-chunk path.

    ``chunk_num`` is 1-indexed (matches the stored ``chunk_num`` and the
    multi-chunk insert loop). Same guards (via ``_shipped_slot_chunk_vector``)
    and the same v0.2.89 §8.5 dual-slot merge semantics as the single-object
    path: active-slot hit required; secondary slots merged per-chunk when
    ``DUAL_EMBEDDING_ENABLED``; never computed for.
    """
    slot = server.text_vector_slot
    vec = _shipped_slot_chunk_vector(
        knowledge_root, slot, content_hash, chunk_num, expected_chunks
    )
    if vec is None:
        return None

    if DUAL_EMBEDDING_ENABLED:
        slots: Dict[str, List[float]] = {slot: vec}
        _merge_secondary_shipped_slots(
            knowledge_root, slot, slots, content_hash,
            chunk_num=chunk_num, expected_chunks=expected_chunks,
        )
        return slots, slots
    return vec, {slot: vec}


def _coerce_vector(raw: object) -> Optional[List[float]]:
    """Validate + coerce a shipped vector to ``list[float]``, or None.

    Rejects empty lists and non-numeric contents (a malformed sidecar must
    fall back to computing, never insert a bad vector).
    """
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return None


def _update_frontmatter_timestamp(file_path: Path, content: str) -> str:
    """
    Update the `updated:` field in YAML frontmatter to current UTC time
    ONLY IF the file's content (excluding the `updated:` line itself) has
    changed since the last sync.

    Writes the updated content back to the file and returns it.

    Content-aware skip (v0.2.14, fix 3): the previous behavior touched
    `updated:` on EVERY sync, even for re-sync passes where the file
    bytes weren't actually changed. Result: every install.py --update
    run produced 60+ KG-node-timestamp-only commits in the working
    tree. Now we hash the (frontmatter-minus-updated + body) and
    compare to the on-disk version of the same hash. If equal,
    skip the write. The user's actual content edits via Edit/Write
    tools always change the body and will pass through unchanged.
    """
    if not content.strip().startswith('---'):
        return content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return content

    # Content-aware skip: if the file on disk has identical
    # signature-excluding-updated, we are in a pass-through re-sync.
    # Don't bump the timestamp.
    try:
        on_disk = file_path.read_text(encoding='utf-8')
        if _content_signature_excluding_updated(on_disk) == _content_signature_excluding_updated(content):
            return content
    except (OSError, UnicodeDecodeError):
        # If we can't read the file (race / permissions / encoding), fall
        # through to the unconditional update — preserves prior behavior
        # in edge cases.
        pass

    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    fm_text = parts[1]

    updated_pattern = re.compile(r'^updated:.*$', re.MULTILINE)
    if updated_pattern.search(fm_text):
        new_fm = updated_pattern.sub(f'updated: {now_iso}', fm_text)
    else:
        # Add after 'created:' line if present, else append before end of block
        created_pattern = re.compile(r'^(created:.*)$', re.MULTILINE)
        if created_pattern.search(fm_text):
            new_fm = created_pattern.sub(r'\1\nupdated: ' + now_iso, fm_text)
        else:
            new_fm = fm_text.rstrip('\n') + f'\nupdated: {now_iso}\n'

    new_content = '---' + new_fm + '---' + parts[2]
    file_path.write_text(new_content, encoding='utf-8')
    return new_content


def parse_frontmatter(content: str) -> Tuple[Optional[Dict], str]:
    """
    Parse YAML frontmatter from markdown content.

    Args:
        content: Markdown file content

    Returns:
        Tuple of (frontmatter_dict, content_without_frontmatter)
    """
    if not content.strip().startswith('---'):
        return None, content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return None, content

    try:
        frontmatter = yaml.safe_load(parts[1])
        content_without_fm = parts[2].strip()
        return frontmatter, content_without_fm
    except yaml.YAMLError:
        return None, content


# Node types shipped with every project. The vocabulary is deliberately OPEN:
# a project extends it by declaring additional classes in its
# knowledge/VOCABULARY.md ontology (`#### **`co:X`** (alias: `x`)` headings —
# see that file's "Declaring your own node types" section), which
# _load_vocabulary_node_types() picks up alongside these built-ins.
# Validation must not hardcode a closed set; note the RL reranker's
# type-embedding capacity lives in the private RL module (the SSOT parser
# records a soft warning past 256 types — verify against your module).
#
# SSOT: vco_lib/kg_vocabulary.py (task #33, v0.2.91). This literal MUST MATCH
# vco_lib.kg_vocabulary.BUILTIN_NODE_TYPES (parity pinned by
# tests/test_v0291_kg_vocabulary_consumers.py) — kept inline only for the
# version-skew fallback below.
_BUILTIN_NODE_TYPES = frozenset(
    {"project", "concept", "tool", "research", "model", "hardware", "pattern", "insight", "guide"}
)
_VOCABULARY_TYPES_CACHE: Optional[frozenset] = None
_VOCAB_IMPORT_WARNED = False

# Fallback parser — MUST MATCH vco_lib/kg_vocabulary.py's type extraction
# (_CLASS_HEADING_RE / _FENCE_RE semantics; same parity test as above).
# Heading-anchored + fence-aware so prose or documentation examples that
# merely DESCRIBE a declaration (e.g. "(alias: `x`)" in a fenced code block)
# can never declare a type — a naive `alias:` scan fails toward green.
_VOCAB_CLASS_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+\*\*`co:[A-Za-z0-9_-]+`\*\*\s*"
    r"\(alias:\s*`([A-Za-z0-9_-]+)`\)\s*$"
)
_VOCAB_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _parse_vocabulary_types_fallback(text: str) -> set:
    """Aliases declared by REAL class-heading lines (fenced blocks inert)."""
    aliases: set = set()
    fence_delim = None
    for line in text.splitlines():
        fence_m = _VOCAB_FENCE_RE.match(line)
        if fence_m:
            delim = fence_m.group(1)
            if fence_delim is None:
                fence_delim = delim
            elif delim == fence_delim:
                fence_delim = None
            continue
        if fence_delim is not None:
            continue
        m = _VOCAB_CLASS_HEADING_RE.match(line)
        if m:
            aliases.add(m.group(1).lower())
    return aliases


def _load_vocabulary_node_types() -> frozenset:
    """Valid node types = built-ins ∪ aliases declared in the project's
    knowledge/VOCABULARY.md. Missing/unreadable ontology → built-ins only.

    A-leg (one home): delegates to the SSOT ``vco_lib.kg_vocabulary``.
    vco_lib is already a hard dependency of this script (see the
    ``embedding_service`` import at module top), so the ImportError branch
    only fires on VERSION SKEW — a project whose bundled script is newer
    than the orchestrator clone's vco_lib (predating ``kg_vocabulary``).
    Per-node validation must not crash kg-sync for that, so we warn once
    and fall back to the inline parser above (parity-locked to the SSOT).
    """
    global _VOCABULARY_TYPES_CACHE, _VOCAB_IMPORT_WARNED
    if _VOCABULARY_TYPES_CACHE is not None:
        return _VOCABULARY_TYPES_CACHE
    try:
        from vco_lib.kg_vocabulary import load_vocabulary
        types = frozenset(load_vocabulary(PROJECT_ROOT).node_types)
    except ImportError as exc:
        if not _VOCAB_IMPORT_WARNED:
            _VOCAB_IMPORT_WARNED = True
            print(
                f"⚠️  vco_lib.kg_vocabulary unavailable ({exc}) — validating "
                f"node types with the built-in fallback parser. Run "
                f"`python install.py --update` from the orchestrator root "
                f"to refresh vco_lib.",
                file=sys.stderr,
            )
        merged = set(_BUILTIN_NODE_TYPES)
        try:
            text = (PROJECT_ROOT / "knowledge" / "VOCABULARY.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass  # missing/unreadable/mis-encoded ontology → built-ins only
        else:
            merged |= _parse_vocabulary_types_fallback(text)
        types = frozenset(merged)
    _VOCABULARY_TYPES_CACHE = types
    return _VOCABULARY_TYPES_CACHE


def validate_node_against_vocabulary(node_data: Dict, file_path: Path) -> List[str]:
    """
    Validate node against vocabulary and tag hierarchy rules.

    Args:
        node_data: Parsed node data
        file_path: Path to node file

    Returns:
        List of validation warnings (empty if all valid)
    """
    warnings = []

    # 1. Type validation (open vocabulary: built-ins + VOCABULARY.md aliases)
    valid_types = _load_vocabulary_node_types()
    node_type = node_data.get("node_type", "")
    if node_type not in valid_types:
        warnings.append(
            f"Node type '{node_type}' not declared (known: {', '.join(sorted(valid_types))}). "
            f"Declare custom types in knowledge/VOCABULARY.md as a class heading with (alias: `{node_type}`)"
        )

    # 2. Tag validation
    tags = node_data.get("tags", [])

    # Check tag count (3-10 recommended)
    if len(tags) < 3:
        warnings.append(f"Too few tags ({len(tags)}) - recommended 3-10 tags")
    elif len(tags) > 10:
        warnings.append(f"Too many tags ({len(tags)}) - recommended 3-10 tags")

    # Check tag format
    for tag in tags:
        # Tags should be lowercase or UPPERCASE (acronyms)
        # Multi-word tags should use hyphens
        if " " in tag:
            warnings.append(f"Tag '{tag}' contains spaces - use hyphens instead")
        if "_" in tag:
            warnings.append(f"Tag '{tag}' uses underscores - use hyphens instead")
        # Check for camelCase (not allowed except for acronyms)
        if any(c.isupper() for c in tag) and not tag.isupper() and "-" not in tag:
            # Could be acronym like "AI" or camelCase like "MyTag"
            if len([c for c in tag if c.isupper()]) > 1 and not tag.isupper():
                warnings.append(f"Tag '{tag}' uses camelCase - use lowercase with hyphens")

    # Check for recommended tag categories (for technical nodes)
    if node_type in {"project", "concept", "tool", "pattern"}:
        # Should have at least 1 domain tag
        domain_tags = {"AI", "ML", "NLP", "CV", "database", "workflow", "tooling",
                      "infrastructure", "frontend", "backend", "security"}
        has_domain = any(tag in domain_tags for tag in tags)

        # Should have abstraction level (except for tools)
        abstraction_tags = {"high-level-plan", "mid-level-architecture",
                          "low-level-implementation", "function-description"}
        has_abstraction = any(tag in abstraction_tags for tag in tags)

        if not has_domain:
            warnings.append("No domain tag found (recommended: #AI, #database, #workflow, etc.)")

        if not has_abstraction and node_type != "tool":
            warnings.append("No abstraction level tag (recommended: #high-level-plan, #mid-level-architecture, #low-level-implementation)")

    # 3. External links validation (if present)
    external_links = node_data.get("external_links", "")
    if external_links:
        try:
            import json
            links = json.loads(external_links) if isinstance(external_links, str) else external_links
            if not isinstance(links, dict):
                warnings.append("external_links should be a dictionary")
        except (json.JSONDecodeError, TypeError):
            warnings.append("external_links is not valid JSON")

    return warnings


def parse_markdown_node(content: str, file_path: Path) -> Dict:
    """
    Parse markdown file to extract knowledge node data

    Args:
        content: Markdown file content
        file_path: Path to markdown file

    Returns:
        Dictionary with node data (title, tags, links, etc.)
    """
    # Parse YAML frontmatter (if present)
    frontmatter, content_body = parse_frontmatter(content)

    lines = content.strip().split('\n')

    # Extract title (from frontmatter or first # heading)
    if frontmatter and 'title' in frontmatter:
        title = frontmatter['title']
    else:
        title = file_path.stem  # Default to filename
        for line in lines:
            if line.startswith('# '):
                title = line[2:].strip()
                break

    # Extract tags (from frontmatter or inline)
    tags = []
    if frontmatter and 'tags' in frontmatter:
        # Frontmatter tags (array format) - convert all to strings
        raw_tags = frontmatter['tags'] if isinstance(frontmatter['tags'], list) else []
        tags = [str(tag) for tag in raw_tags]
    else:
        # Inline tags (Obsidian style: #tag)
        tag_pattern = r'#([a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)'
        for match in re.finditer(tag_pattern, content):
            tag = match.group(1)
            if tag not in tags:
                tags.append(tag)

    # Extract WikiLinks - supports both typed and untyped
    # Typed: [[uses::Redis]], [[implements::Pattern]]
    # Untyped: [[Redis]] (defaults to "relatedTo")
    links = []  # Untyped links (backward compatibility)
    typed_links = []  # New: Typed relationships

    # Updated pattern to capture optional relationship type
    # Matches: [[type::target]] or [[target]]
    link_pattern = r'\[\[(?:([a-zA-Z_]+)::)?([^\]]+)\]\]'

    for match in re.finditer(link_pattern, content):
        relation_type = match.group(1)  # None if untyped
        target_title = match.group(2).strip()

        if relation_type:
            # Typed relationship
            typed_link = {
                "relation_type": relation_type,
                "target_title": target_title
            }
            if typed_link not in typed_links:
                typed_links.append(typed_link)
        else:
            # Untyped (backward compatibility)
            if target_title not in links:
                links.append(target_title)

    # Node type (from frontmatter or directory)
    if frontmatter and 'type' in frontmatter:
        node_type = frontmatter['type']
    else:
        rel_path = file_path.relative_to(KNOWLEDGE_ROOT)
        node_type = str(rel_path.parts[0]) if len(rel_path.parts) > 1 else "general"

    # Temporal metadata from frontmatter
    temporal_data = {}
    if frontmatter:
        # Created/updated timestamps (YAML parses ISO timestamps as datetime objects)
        if 'created' in frontmatter and frontmatter['created'] != 'unknown':
            try:
                val = frontmatter['created']
                # If already a datetime object, use it directly
                if isinstance(val, datetime):
                    temporal_data['created'] = val.isoformat()
                else:
                    # Parse string format
                    val_str = str(val)
                    if 'T' in val_str:
                        created_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        created_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['created'] = created_dt.isoformat()
            except (ValueError, TypeError) as e:
                pass

        if 'updated' in frontmatter and frontmatter['updated'] != 'unknown':
            try:
                val = frontmatter['updated']
                if isinstance(val, datetime):
                    temporal_data['updated'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        updated_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        updated_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['updated'] = updated_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Valid from/until timestamps
        if 'valid_from' in frontmatter:
            try:
                val = frontmatter['valid_from']
                if isinstance(val, datetime):
                    temporal_data['valid_from'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_from_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_from_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_from'] = valid_from_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # `valid_until` semantics:
        #   - Frontmatter omits it OR sets it to None → "never expires"; the
        #     property is left unset (null) in the DB.
        #   - Frontmatter sets a real date → write it.
        #
        # Null is filterable because the collection is created with
        # `inverted_index_config=Configure.inverted_index(index_null_state=True)`
        # (see `_create_kg_collection`). The MCP `_stale_filter()` then uses
        # `valid_until is_none(True) | valid_until > now`. Setting that
        # config at create time is required — Weaviate doesn't allow toggling
        # it later (`Reconfigure.inverted_index` lacks `index_null_state`).
        if 'valid_until' in frontmatter and frontmatter['valid_until'] is not None:
            try:
                val = frontmatter['valid_until']
                if isinstance(val, datetime):
                    temporal_data['valid_until'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_until_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_until_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_until'] = valid_until_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Status
        if 'status' in frontmatter:
            temporal_data['status'] = frontmatter['status']

    # External links from frontmatter (RDF-inspired)
    external_links = ""
    if frontmatter and 'external_links' in frontmatter:
        ext_links = frontmatter['external_links']
        if isinstance(ext_links, dict):
            # Convert dict to JSON string for storage (Weaviate TEXT field)
            import json
            external_links = json.dumps(ext_links)

    # Fallback: File timestamps for old created_at/updated_at fields
    stat = file_path.stat()
    created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    result = {
        "title": title,
        "content": content,
        "file_path": _canonical_file_path(file_path),
        "node_type": node_type,
        "tags": tags,
        "links": links,
        "typed_links": typed_links,  # Typed relationships
        "external_links": external_links,  # External links (DBpedia, official docs, etc.)
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat()
    }

    # v0.2.89 BUG 6: pass the raw frontmatter `scope:` value through for the
    # per-node routing decision in `sync_node` (normalized + validated there
    # via `_node_scope`). NOT a Weaviate property — `data_obj` is built from
    # an explicit key list, so this never reaches the collection schema.
    if frontmatter and 'scope' in frontmatter:
        result["scope"] = frontmatter['scope']

    # Add temporal metadata if present
    result.update(temporal_data)

    return result


# v0.2.38 A4: Canonical scalar-property registry for KG collections.
#
# BOTH the fresh-create path AND the additive-migrate path inside
# `ensure_collection_exists` must stay in sync. Previously they were
# independent inline dicts — V37-C Gap 6d found that chunking props
# (chunk_num / total_chunks / source_node_id) existed in the create
# branch but not the migrate branch, causing "no such prop" failures
# on legacy collections. Hoisting into ONE constant here guarantees
# the two paths can never diverge again.
#
# Mapping: prop_name → DataType sentinel.  DataType is a lazy import
# inside ensure_collection_exists so we use string sentinels at the
# module level and resolve them at runtime (avoids importing weaviate
# at parse-time for scripts that only need the constant for inspection,
# e.g. unit tests).
#
# String sentinels match DataType attribute names: "TEXT", "INT",
# "DATE", "TEXT_ARRAY".  Non-scalar props (typed_links OBJECT_ARRAY,
# linksTo cross-reference) are handled separately because they require
# nested_properties / ReferenceProperty which can't be expressed as
# a simple name→DataType mapping.
_KG_NODE_SCALAR_PROPERTIES: dict[str, str] = {
    # Core identity
    "title":           "TEXT",
    "content":         "TEXT",
    "file_path":       "TEXT",
    "node_type":       "TEXT",
    # Multi-value arrays (TEXT_ARRAY is still a "scalar" Weaviate primitive)
    "tags":            "TEXT_ARRAY",
    "links":           "TEXT_ARRAY",
    # External links (RDF-inspired; stored as JSON text since Weaviate OBJECT
    # requires nested properties)
    "external_links":  "TEXT",
    # Legacy filesystem timestamps (back-compat)
    "created_at":      "DATE",
    "updated_at":      "DATE",
    # Canonical temporal metadata (from frontmatter, PR-24 2026-05-16)
    "created":         "DATE",
    "updated":         "DATE",
    "valid_from":      "DATE",
    "valid_until":     "DATE",
    # v0.2.17: status + content-hash for embed-skip on re-sync
    "status":          "TEXT",
    "content_hash":    "TEXT",
    # v0.2.37 Gap 6d: chunking props — MUST be present in both fresh-create
    # and additive-migrate paths to avoid "no such prop 'chunk_num'" failures
    # on legacy collections.  Validated by test_kg_schema_consistency.py.
    "chunk_num":       "INT",
    "total_chunks":    "INT",
    "source_node_id":  "TEXT",
    # W3 (v0.2.92 wiring audit): the per-slot truncation record — the
    # complete ``truncated_slots`` list (presence = the row can answer for
    # every slot) and the frozen-meaning ``secondary_truncated_slots`` view.
    # Declared here so FRESH collections get them up-front and EXISTING
    # collections additively migrate them (the same A4 invariant path as
    # every other scalar prop; autoschema remains the belt-and-braces).
    "truncated_slots":             "TEXT_ARRAY",
    "secondary_truncated_slots":   "TEXT_ARRAY",
    # The slots the write MEASURED — what scopes the record above from a
    # "presence means complete" claim to "answers for exactly these".
    "truncation_measured_slots":   "TEXT_ARRAY",
}


def ensure_collection_exists(server: WeaviateMCPServer) -> bool:
    """
    Ensure the project's KG_COLLECTION (env-resolved, fallback "KnowledgeGraph")
    exists with proper schema.

    Named-vector slots (v0.2.18): sourced from
    `vco_lib.weaviate_schema.KG_NAMED_VECTORS` for parity with the
    `project_init.kg_class_definition` canonical path. Falls back to the
    legacy 3-slot config if the import fails (one-off script runs outside
    the orchestrator clone). Mirrors `ensure_dev_collection_exists`.

    Scalar properties sourced from `_KG_NODE_SCALAR_PROPERTIES` (v0.2.38 A4)
    so fresh-create and additive-migrate paths cannot diverge.

    Args:
        server: Weaviate MCP server instance

    Returns:
        True if collection exists or was created
    """
    try:
        from weaviate.classes.config import Configure, Property, DataType

        # Resolve DataType values from the module-level string sentinels.
        # Done once per call so tests can inspect _KG_NODE_SCALAR_PROPERTIES
        # without importing weaviate.
        _dt_map: dict[str, object] = {
            "TEXT":       DataType.TEXT,
            "INT":        DataType.INT,
            "DATE":       DataType.DATE,
            "TEXT_ARRAY": DataType.TEXT_ARRAY,
        }
        _scalar_props: dict[str, object] = {
            name: _dt_map[sentinel]
            for name, sentinel in _KG_NODE_SCALAR_PROPERTIES.items()
        }

        if server.client.collections.exists(COLLECTION_NAME):
            print(f"✓ Collection '{COLLECTION_NAME}' exists")

            # Additive-migrate path: add any scalar prop missing from an
            # existing collection (temporal + chunking + hash).  Uses the
            # same canonical list as the fresh-create path below — A4
            # invariant enforced by test_kg_schema_consistency.py.
            try:
                collection = server.client.collections.get(COLLECTION_NAME)
                config = collection.config.get()
                existing_props = {prop.name for prop in config.properties}
                existing_refs = {ref.name for ref in (config.references or [])}

                # Add every scalar prop that's missing.
                for prop_name, prop_type in _scalar_props.items():
                    if prop_name not in existing_props:
                        print(f"  Adding property: {prop_name}")
                        collection.config.add_property(
                            Property(name=prop_name, data_type=prop_type)
                        )

                # Add typed_links property if missing
                if 'typed_links' not in existing_props:
                    print(f"  Adding property: typed_links (nested objects)")
                    collection.config.add_property(
                        Property(
                            name="typed_links",
                            data_type=DataType.OBJECT_ARRAY,
                            nested_properties=[
                                Property(name="relation_type", data_type=DataType.TEXT),
                                Property(name="target_title", data_type=DataType.TEXT)
                            ]
                        )
                    )

                # Add external_links property if missing (RDF-inspired)
                if 'external_links' not in existing_props:
                    print(f"  Adding property: external_links (JSON text)")
                    collection.config.add_property(
                        Property(name="external_links", data_type=DataType.TEXT)
                    )

                # Add cross-reference property if missing
                from weaviate.classes.config import ReferenceProperty
                if 'linksTo' not in existing_refs:
                    print(f"  Adding cross-reference: linksTo")
                    collection.config.add_reference(
                        ReferenceProperty(
                            name="linksTo",
                            target_collection=COLLECTION_NAME
                        )
                    )

                print(f"✓ Schema up to date")
            except Exception as e:
                print(f"⚠️  Could not update schema: {e}")

            return True

        print(f"Creating collection '{COLLECTION_NAME}'...")

        # v0.2.18: pull the 5-slot named-vector catalog from the canonical
        # source (`vco_lib.weaviate_schema.KG_NAMED_VECTORS`) so this
        # runtime fallback creates the KG collection at the same shape as
        # `vco_lib.project_init.kg_class_definition`. Fall back to the
        # legacy 3-slot config when the import fails (one-off script runs
        # outside an orchestrator clone where vco_lib isn't on the path).
        # The migrate dispatcher's additive `copy` action picks up any
        # missing slot later when the user does run install/update.
        #
        # Mirrors the Dev-collection variant at `ensure_dev_collection_exists`
        # (landed bcacfc0). Both sites stay in lockstep with the canonical
        # `project_init.{kg,development}_class_definition` so the migrate
        # dispatcher's additive patch_props diff doesn't trip phantom
        # missing-slot loops.
        try:
            from vco_lib.weaviate_schema import KG_NAMED_VECTORS
            named_vectors = [
                Configure.NamedVectors.none(name=slot.name)
                for slot in KG_NAMED_VECTORS
            ]
        except Exception as import_err:  # noqa: BLE001 — best-effort fallback
            print(f"  ⚠️  Could not import KG_NAMED_VECTORS ({import_err}); "
                  "falling back to legacy 3-slot config")
            named_vectors = [
                Configure.NamedVectors.none(name="qwen3_embed"),     # active
                Configure.NamedVectors.none(name="ollama_embed"),    # legacy
                Configure.NamedVectors.none(name="openai_embed"),    # optional
            ]

        # Fresh-create path: build Property list from the canonical scalar
        # registry (_KG_NODE_SCALAR_PROPERTIES) so this path and the
        # additive-migrate path above are always identical in coverage.
        # Non-scalar props (typed_links, content_hash note, etc.) are
        # appended inline below.
        scalar_property_list = [
            Property(name=name, data_type=dt)
            for name, dt in _scalar_props.items()
        ]

        server.client.collections.create(
            name=COLLECTION_NAME,
            description="Claude knowledge graph nodes with semantic search (chunked for large files)",
            properties=scalar_property_list + [
                # Typed relationships as JSON objects (non-scalar — needs
                # nested_properties, cannot be expressed in the scalar registry)
                Property(
                    name="typed_links",
                    data_type=DataType.OBJECT_ARRAY,
                    nested_properties=[
                        Property(name="relation_type", data_type=DataType.TEXT),
                        Property(name="target_title", data_type=DataType.TEXT)
                    ]
                ),
            ],
            # Named vectors must match `vco_lib.weaviate_schema.KG_NAMED_VECTORS`
            # (the canonical v0.2.18 catalog). Without these the collection
            # accepts only the unnamed default vector, and per-named-vector
            # inserts fail at runtime ("collection configured without
            # multiple named vectors but received named vectors:
            # map[ollama_embed:...]"). Vectors are still computed manually
            # (Configure.NamedVectors.none).
            vectorizer_config=named_vectors,
            # `index_null_state=True` enables `is_none(True)` filters on date
            # properties (notably `valid_until`). Required for the MCP
            # `_stale_filter` to filter out expired/archived nodes at query
            # time. CANNOT be added later via Reconfigure — must be set at
            # create time. (Weaviate 1.28; verified 2026-04-30 against the
            # python client v4.)
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )

        print(f"✓ Created collection '{COLLECTION_NAME}' "
              f"({len(named_vectors)} named vectors + index_null_state=True)")
        return True

    except Exception as e:
        print(f"❌ Error ensuring collection: {e}")
        return False


def ensure_dev_collection_exists(server: WeaviateMCPServer) -> bool:
    """Create the development docs collection if missing.

    Schema is a **near-subset** of the KG schema. Matches
    `vco_lib.project_init.development_class_definition` exactly — this is
    the runtime-fallback path used when `project_init` didn't get there
    first (one-off `python -m sync_knowledge_graph --all-docs` runs from a
    project that hasn't been re-installed since the v0.2.18 schema bump).
    Both write sites MUST stay in lockstep so the migrate dispatcher's
    additive patch_props diff doesn't trip a phantom missing-prop loop.

    Properties:
      - title, content, file_path (the load-bearing trio)
      - created_at, updated_at (legacy filesystem timestamps; back-compat)
      - created, updated, valid_from, valid_until (canonical temporal,
        PR-24 2026-05-16) — required by MCP `_stale_filter` (valid_until
        is_none(True) | valid_until > now)
      - status (v0.2.18 2026-05-19) — KG parity for archived-doc filter
      - content_hash (v0.2.18 2026-05-19) — KG parity, powers the
        embed-skip fast-path in `sync_doc`
      - chunk_num, total_chunks, source_node_id (chunking support)

    Explicitly NOT mirrored from KG (user direction 2026-05-19):
      - tags / links / typed_links — KG-only graph metadata
      - external_links — KG-only RDF metadata
      - node_type — redundant (every row in a Dev collection is unambiguously
        a "doc" by class name)

    Named-vector slots (v0.2.18): sourced from
    `vco_lib.weaviate_schema.KG_NAMED_VECTORS` for parity with the
    `project_init.development_class_definition` canonical path. With
    fallback to the legacy 3-slot config if the import fails (one-off
    script runs outside the orchestrator clone).

    Returns True if the collection exists or was created.
    """
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION env not set — skipping dev collection")
        return False
    try:
        from weaviate.classes.config import Configure, Property, DataType

        if server.client.collections.exists(DEV_COLLECTION_NAME):
            print(f"✓ Dev collection '{DEV_COLLECTION_NAME}' exists")
            return True

        # v0.2.18: pull the 5-slot named-vector catalog from the canonical
        # source (`vco_lib.weaviate_schema.KG_NAMED_VECTORS`) so this
        # runtime fallback creates collections at the same shape as the
        # `project_init.development_class_definition` path. Fall back to
        # the legacy 3-slot config when the import fails (one-off script
        # runs outside an orchestrator clone where vco_lib isn't on the
        # path). The migrate dispatcher's additive `copy` action picks up
        # any missing slot later when the user does run install/update.
        try:
            from vco_lib.weaviate_schema import KG_NAMED_VECTORS
            named_vectors = [
                Configure.NamedVectors.none(name=slot.name)
                for slot in KG_NAMED_VECTORS
            ]
        except Exception as import_err:  # noqa: BLE001 — best-effort fallback
            print(f"  ⚠️  Could not import KG_NAMED_VECTORS ({import_err}); "
                  "falling back to legacy 3-slot config")
            named_vectors = [
                Configure.NamedVectors.none(name="qwen3_embed"),
                Configure.NamedVectors.none(name="ollama_embed"),
                Configure.NamedVectors.none(name="openai_embed"),
            ]

        print(f"Creating dev collection '{DEV_COLLECTION_NAME}'...")
        server.client.collections.create(
            name=DEV_COLLECTION_NAME,
            description="Project development documentation (docs/) — chunked, "
                        "schema-paired with KG, auto-bound when project is "
                        "given KG access via the launcher.",
            properties=[
                Property(name="title", data_type=DataType.TEXT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="file_path", data_type=DataType.TEXT),
                # Legacy filesystem timestamps (kept for back-compat; older
                # docs were ingested with these names).
                Property(name="created_at", data_type=DataType.DATE),
                Property(name="updated_at", data_type=DataType.DATE),
                # Canonical temporal metadata — mirrors KG schema +
                # vco_lib.project_init.development_class_definition.
                # Required so the MCP `_stale_filter` (valid_until is_none
                # OR > now) doesn't fail with "no such prop" on Dev
                # collections. PR-24 (2026-05-16).
                Property(name="created", data_type=DataType.DATE),
                Property(name="updated", data_type=DataType.DATE),
                Property(name="valid_from", data_type=DataType.DATE),
                Property(name="valid_until", data_type=DataType.DATE),
                # v0.2.18 (2026-05-19): KG parity. `status` lets archived
                # docs be filtered out by `hybrid_search`; `content_hash`
                # powers the embed-skip fast-path in `sync_doc`. Must
                # match `project_init.development_class_definition`
                # exactly so the migrate dispatcher's additive patch_props
                # diff doesn't loop.
                Property(name="status", data_type=DataType.TEXT),
                Property(name="content_hash", data_type=DataType.TEXT),
                # Chunking support
                Property(name="chunk_num", data_type=DataType.INT),
                Property(name="total_chunks", data_type=DataType.INT),
                Property(name="source_node_id", data_type=DataType.TEXT),
            ],
            vectorizer_config=named_vectors,
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )
        print(f"✓ Created dev collection '{DEV_COLLECTION_NAME}' "
              f"({len(named_vectors)} named vectors + index_null_state=True)")
        return True
    except Exception as e:
        print(f"❌ Error ensuring dev collection: {e}")
        return False


def _doc_title_from_file(file_path: Path, content: str) -> str:
    """Pick a title for a doc file (no frontmatter assumed).

    Order: first H1 heading; first H2 heading; filename stem (humanized).
    """
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("## "):
            return s[3:].strip()
    return file_path.stem.replace("-", " ").replace("_", " ").strip().title()


def parse_doc_file(content: str, file_path: Path) -> Dict:
    """Parse a docs/ file. Returns the same shape as `parse_markdown_node`
    but with KG-specific fields (tags, links, etc.) absent or empty.

    Docs lack frontmatter. We synthesize:
      - title: first H1, falling back to H2, falling back to filename stem
      - created_at / updated_at: filesystem stat, since git history is more
        expensive to compute and the chunker doesn't need exact provenance
    """
    title = _doc_title_from_file(file_path, content)
    try:
        st = file_path.stat()
        # Both timestamps from filesystem; we don't have richer provenance
        # without git. Good enough: the index respects updated_at for
        # `days=N` recency filters.
        created_at = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
        updated_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except OSError:
        now = datetime.now(timezone.utc)
        created_at = now
        updated_at = now
    return {
        "title": title,
        "content": content,
        "file_path": _canonical_file_path(file_path),
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        # Empty KG-specific fields — kept for symmetry with sync_node's
        # data_obj structure but never written into the dev collection
        # (its schema doesn't have them).
        "tags": [],
        "links": [],
        "typed_links": [],
        "external_links": "",
        "node_type": "doc",
    }


# ──────────────────────────────────────────────────────────────────────
# v0.2.92 WP-B1 — per-node outcome + run tally (terminal honesty).
#
# Field report (D12): archived / frontmatter-skipped nodes returned True
# and were counted as SUCCEEDED, so "117/117 succeeded" could hide nodes
# that are intentionally absent from Weaviate, and no run ever listed
# WHICH paths failed or were skipped or WHY. `sync_node`/`sync_doc` now
# return a `SyncOutcome` carrying the category + path + reason; the
# `--all` / file-list drivers tally them into a `SyncTally` whose summary
# line appends ", K skipped" and whose details block names every
# non-synced path. `SyncOutcome.__bool__` preserves the historical
# truthiness contract (skip == not-a-failure) for external callers such
# as `maintain_knowledge_graph.py`'s `if sync_node(...):`.
# ──────────────────────────────────────────────────────────────────────

#: Outcome categories. The first five are non-failures; only "failed"
#: makes a run exit 1.
OUTCOME_SYNCED = "synced"
OUTCOME_EMBED_SKIPPED = "embed-skipped"          # unchanged, already current in Weaviate
OUTCOME_ARCHIVED_SKIPPED = "archived-skipped"    # path or frontmarker: intentionally not indexed
OUTCOME_FRONTMATTER_SKIPPED = "frontmatter-skipped"
OUTCOME_EXCLUDED_SKIPPED = "excluded-skipped"    # meta files / out-of-root targets / dev-unset
OUTCOME_FAILED = "failed"

_NON_FAILURE_OUTCOMES = frozenset({
    OUTCOME_SYNCED,
    OUTCOME_EMBED_SKIPPED,
    OUTCOME_ARCHIVED_SKIPPED,
    OUTCOME_FRONTMATTER_SKIPPED,
    OUTCOME_EXCLUDED_SKIPPED,
})


class SyncOutcome:
    """What happened to ONE node/doc during a sync_node/sync_doc call.

    ``status`` is one of the ``OUTCOME_*`` constants, ``path`` the
    canonical relative file_path (best effort), ``reason`` a one-line
    human explanation for the tally's details block (empty for plain
    successes). Boolean truth == "not a failure" — identical to the
    pre-v0.2.92 ``bool`` return for every caller that only asked
    "did this file fail?".
    """

    __slots__ = ("status", "path", "reason")

    def __init__(self, status: str, path: str = "", reason: str = "") -> None:
        self.status = status
        self.path = path
        self.reason = reason

    def __bool__(self) -> bool:
        return self.status != OUTCOME_FAILED

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"SyncOutcome({self.status!r}, {self.path!r}, {self.reason!r})"


class SyncTally:
    """Aggregated per-run outcomes for one tree (`--all`) or file list.

    ``succeeded`` counts real writes only; ``failed`` counts failures;
    ``skipped`` sums every intentional non-sync (embed-skip because the
    content hash matched, archived, frontmarker, excluded). ``records``
    keeps every non-synced outcome so the terminal details block and the
    run log can name paths WITH reasons.
    """

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {c: 0 for c in _NON_FAILURE_OUTCOMES}
        self.counts[OUTCOME_FAILED] = 0
        self.records: List[SyncOutcome] = []

    def add(self, outcome: SyncOutcome) -> None:
        self.counts[outcome.status] = self.counts.get(outcome.status, 0) + 1
        if outcome.status != OUTCOME_SYNCED:
            self.records.append(outcome)

    @property
    def succeeded(self) -> int:
        return self.counts[OUTCOME_SYNCED]

    @property
    def failed(self) -> int:
        return self.counts[OUTCOME_FAILED]

    @property
    def skipped(self) -> int:
        return (
            self.counts[OUTCOME_EMBED_SKIPPED]
            + self.counts[OUTCOME_ARCHIVED_SKIPPED]
            + self.counts[OUTCOME_FRONTMATTER_SKIPPED]
            + self.counts[OUTCOME_EXCLUDED_SKIPPED]
        )

    @property
    def total(self) -> int:
        """Every outcome this tally saw — i.e. how many files the run CONSIDERED.

        v0.2.94: distinct from ``succeeded``, and the distinction is what the
        paired ledger clears need. Zero here means the tree was not walked at
        all (no such directory, or nothing in it), which is not the same as
        "walked and found nothing owed" — a clear predicated on the second must
        not fire on the first.
        """
        return sum(self.counts.values())

    def summary_fragment(self) -> str:
        """`S succeeded, F failed, K skipped` — the exact fragment the
        launcher's ``parse_summary_line`` reads (which accepts both this
        and the legacy two-count shape)."""
        return f"{self.succeeded} succeeded, {self.failed} failed, {self.skipped} skipped"


def sync_doc(server: WeaviateMCPServer, file_path: Path) -> "SyncOutcome":
    """Sync a single docs/ file to the development collection.

    Mirrors `sync_node` minus the KG-specific concerns (no frontmatter
    parsing, no WikiLink resolution, no tag-from-typed-links inference, no
    cross-references). Same chunker, same active-vector-slot logic.

    v0.2.18 (2026-05-19): mirrors the v0.2.17 KG content_hash embed-skip
    fast-path. Before re-embedding, query existing objects for this
    `file_path` and check (a) every existing chunk has a non-empty
    `content_hash` equal to the current file's hash, (b) chunk-count
    matches what we'd reproduce, and (c) the active named-vector slot
    (`server.text_vector_slot`) is populated on every chunk. When all
    three hold → skip the delete-and-re-embed entirely. Saves the entire
    Ollama embed roundtrip + Weaviate delete/insert per unchanged file.

    Conservative gating: any missing chunk-vector, any empty hash, any
    mismatched chunk-count, or any exception in the fast-path check falls
    through to the existing delete-and-re-embed path (and that path
    writes `content_hash` so the NEXT re-sync will hit the fast path).
    This handles the warm-up case where an existing v0.2.17 Dev collection
    just gained the `content_hash` property via additive patch_props but
    none of its rows have a value yet.
    """
    if not DEV_COLLECTION_NAME:
        print(f"⊘ DEVELOPMENT_COLLECTION not set — skipping {file_path}")
        return SyncOutcome(
            OUTCOME_EXCLUDED_SKIPPED,
            _relative_file_path(file_path),
            "DEVELOPMENT_COLLECTION not set — dev collection sync disabled",
        )

    start_time = time.time()

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            return SyncOutcome(
                OUTCOME_FAILED,
                _relative_file_path(file_path),
                "file not found",
            )

        # Same archive-skip logic as KG (path contains 'archive/' segment).
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived doc: {reason}")
            # v0.2.70 FIX #1: delete by file_path (unique), NOT by title — a
            # title-scoped delete here removed any active doc sharing this
            # archived doc's synthesized title during a --all docs run.
            try:
                fp_value = _relative_file_path(file_path)
                removed = _delete_doc_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior dev entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior dev entry: {e}")
            return SyncOutcome(
                OUTCOME_ARCHIVED_SKIPPED, fp_value, f"archived doc: {reason}"
            )

        content = file_path.read_text(encoding="utf-8")
        doc_data = parse_doc_file(content, file_path)

        print(f"🔄 Syncing doc: {doc_data['title']}")

        coll = server.client.collections.get(DEV_COLLECTION_NAME)

        # v0.2.18: compute content_hash BEFORE the delete-and-re-embed
        # pipeline so we can short-circuit on the unchanged-file case.
        # The hash function is the same one used by the KG path
        # (`_content_signature_excluding_updated`); for a docs/ file with
        # no frontmatter it degenerates to plain SHA-256 of the body —
        # exactly what we want.
        current_content_hash = _content_signature_excluding_updated(content)

        # Active named-vector slot for the running backend (e.g.
        # 'qwen3_embed' for Ollama qwen3, 'openai_text_embed' for OpenAI).
        # The fast-path requires this slot to be populated on every
        # existing chunk; otherwise we're in the v0.2.17 -> v0.2.18 warm-up
        # case where the user just switched backends and the new slot is
        # empty, and we MUST re-embed to populate it.
        try:
            active_slot = server.text_vector_slot
        except Exception:  # noqa: BLE001 — soft-fail on degenerate wrapper
            active_slot = ""

        # Pull existing objects WITH vectors so we can verify the active
        # slot is populated. `include_vector=True` returns `obj.vector` as
        # a dict keyed by slot name for named-vector collections.
        try:
            existing = coll.query.fetch_objects(
                filters=_file_path_filter(doc_data["file_path"]),
                limit=100,
                return_properties=[
                    "file_path", "content_hash", "chunk_num", "total_chunks",
                ],
                include_vector=True,
            )
        except Exception as fetch_err:  # noqa: BLE001
            # Older Weaviate clients / mocked clients that don't accept
            # `include_vector` keyword → fall back to the basic fetch and
            # skip the active-slot check (defer to content_hash + chunk
            # count). Any real client supports this kw since Weaviate v4.
            print(f"   (fetch_objects(include_vector=True) failed: "
                  f"{fetch_err}; falling back to hash-only check)")
            try:
                existing = coll.query.fetch_objects(
                    filters=_file_path_filter(doc_data["file_path"]),
                    limit=100,
                    return_properties=[
                        "file_path", "content_hash", "chunk_num", "total_chunks",
                    ],
                )
            except Exception:
                existing = None  # forces fall-through to re-embed

        # EMBED-SKIP fast path. Mirrors sync_node's v0.2.17 implementation
        # with the added active-slot check (which sync_node's fast-path
        # also relies on implicitly via the chunk_count gate, but Dev gets
        # it explicit because Dev rows are more likely to have a chunk
        # written under one slot and not yet enriched under another).
        if existing is not None and existing.objects:
            try:
                existing_hashes: List[str] = []
                existing_total_chunks: List[int] = []
                existing_file_paths: List[str] = []
                active_slot_populated: List[bool] = []
                for obj in existing.objects:
                    props = obj.properties or {}
                    existing_hashes.append(props.get("content_hash", "") or "")
                    tc = props.get("total_chunks", 0)
                    try:
                        existing_total_chunks.append(int(tc) if tc is not None else 0)
                    except (TypeError, ValueError):
                        existing_total_chunks.append(0)
                    existing_file_paths.append(props.get("file_path", "") or "")
                    # `obj.vector` is a dict {slot: list[float]} for
                    # named-vector collections; missing/None when the
                    # fetch didn't include vectors (older client).
                    vec_field = getattr(obj, "vector", None)
                    if isinstance(vec_field, dict) and active_slot:
                        slot_vec = vec_field.get(active_slot)
                        active_slot_populated.append(
                            bool(slot_vec) and len(slot_vec) > 0
                        )
                    else:
                        # Couldn't inspect → be conservative, treat as
                        # NOT populated so we re-embed. Exception: if
                        # active_slot is empty (no wrapper info), skip
                        # the active-slot gate altogether (back to
                        # content_hash + chunk_count).
                        active_slot_populated.append(not active_slot)

                chunk_count_ok = (
                    len(existing_total_chunks) > 0
                    and all(
                        tc == len(existing_total_chunks)
                        for tc in existing_total_chunks
                    )
                )
                hashes_ok = (
                    len(existing_hashes) > 0
                    and all(h == current_content_hash for h in existing_hashes)
                    and all(h for h in existing_hashes)  # no empty strings
                )
                # v0.2.92 WP-B1 (D13): skip only when no found row
                # reports a NON-canonical (legacy backslash) file_path —
                # same rule as sync_node's fast path. A row not reporting
                # a file_path at all cannot be judged and keeps the
                # pre-v0.2.92 skip semantics (conservative default).
                shapes_ok = all(
                    fp in ("", doc_data["file_path"]) for fp in existing_file_paths
                )
                slots_ok = all(active_slot_populated)
                # v0.2.92 chunk-plan transition repair — same rule as
                # sync_node's fast path (see the block above it): while a
                # chunker-revision crossing is pending, a self-consistent
                # row set must ALSO match the CURRENT chunker's plan for
                # this content, or the doc re-chunks.
                _self_consistent = (
                    chunk_count_ok and hashes_ok and slots_ok and shapes_ok
                )
                _plan_ok = True
                if _self_consistent and _chunker_resync_pending():
                    _plan_ok = _stored_plan_matches_current(
                        server, coll, doc_data["file_path"], content,
                        len(existing_hashes),
                    )
                    if not _plan_ok:
                        global _RECHUNKED_COUNT
                        _RECHUNKED_COUNT += 1
                        print(
                            f"   ♻️  Re-chunking: stored chunk plan predates "
                            f"the current chunker revision "
                            f"(revision-crossing repair)"
                        )
                if _self_consistent and _plan_ok:
                    elapsed = time.time() - start_time
                    print(
                        f"   ⏭️  Embed-skip: content_hash matches "
                        f"({current_content_hash[:12]}…); "
                        f"{len(existing_hashes)} chunk(s) preserved "
                        f"in {active_slot or '<no-slot>'} "
                        f"({elapsed*1000:.0f} ms)"
                    )
                    return SyncOutcome(
                        OUTCOME_EMBED_SKIPPED,
                        doc_data["file_path"],
                        "content_hash match — already current in Weaviate",
                    )
            except Exception as skip_err:  # noqa: BLE001
                # Soft-fail: fall through to the delete-and-re-embed path.
                print(f"   (embed-skip check failed: {skip_err}; re-embedding)")

        # Fast path didn't apply (or no existing objects). Delete old
        # versions and re-embed. The `content_hash` written below means
        # the NEXT re-sync will hit the fast path.
        if existing is not None:
            for obj in existing.objects:
                coll.data.delete_by_id(obj.uuid)

        source_id = str(uuid.uuid4())
        # W8: ONE shared plan — same computation as `sync_node` and the MCP
        # store (see `_plan_for`); the gate and the boundaries below are the
        # same call's answer, never two. (The separate `token_count` /
        # `_max_tokens` locals this replaced had no reader on the docs path
        # once the plan carried the decision.)
        _plan = _plan_for(
            server, content,
            source_id=source_id,
            metadata={
                "title": doc_data["title"],
                "file_path": doc_data["file_path"],
            },
        )

        if _plan.is_single:
            vec_arg, slots_written, truncated_slots = _build_vector_arg(server, content)
            data_obj = {
                "title": doc_data["title"],
                "content": doc_data["content"],
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": source_id,
                # v0.2.18 (2026-05-19): persist content_hash so the next
                # re-sync can take the embed-skip fast-path above. Same
                # value for all chunks of the same file (computed once
                # over the whole file content above).
                "content_hash": current_content_hash,
            }
            # W3: persist the per-call truncation record (the shared stamper
            # derives BOTH properties from the ONE atomic capture).
            data_obj.update(
                truncation_tag_properties(
                    truncated_slots, server.text_vector_slot,
                    measured_slots=slots_written,
                )
            )
            coll.data.insert(properties=data_obj, vector=vec_arg)
            print(f"   ✓ Stored doc as single chunk (vectors={sorted(slots_written)})")
            return SyncOutcome(OUTCOME_SYNCED, doc_data["file_path"])

        # Chunked path — mirrors `sync_node` chunked branch. W8: the
        # boundaries are the SAME plan the gate above decided on.
        chunks = _plan.chunks
        print(f"   Split into {len(chunks)} chunks", flush=True)
        last_slots: Mapping[str, List[float]] = {}
        for i, chunk in enumerate(chunks):
            vec_arg, last_slots, truncated_slots = _build_vector_arg(
                server, chunk.content
            )
            data_obj = {
                "title": doc_data["title"],
                "content": chunk.content,
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": i + 1,
                "total_chunks": len(chunks),
                "source_node_id": source_id,
                # v0.2.18 (2026-05-19): every chunk of the same file
                # shares the same content_hash (computed over the whole
                # file). The embed-skip fast-path above requires ALL
                # chunks for a file_path to carry an identical, non-empty
                # hash before it skips — writing the same value here
                # keeps that invariant.
                "content_hash": current_content_hash,
            }
            # W3: per-chunk truncation record — a chunk whose SECONDARY (or,
            # on a runner refusal, ACTIVE) vector came from a bounded leading
            # sub-window is distinguishable from STORED DATA alone.
            data_obj.update(
                truncation_tag_properties(
                    truncated_slots, server.text_vector_slot,
                    measured_slots=last_slots,
                )
            )
            coll.data.insert(properties=data_obj, vector=vec_arg)
            # v0.2.69 FIX 3 (review SHOULD-FIX): per-chunk heartbeat. The
            # launcher's kg-sync stall watchdog re-arms on every output
            # line; without a per-chunk print here, a large multi-chunk
            # doc would embed+insert silently (N × ~30 s on a slow CPU)
            # and could exceed the watchdog window with no output —
            # false-tripping it. The KG path (`sync_node`) already prints
            # per chunk; this mirrors that on the docs path so the
            # window's "no-output ⇒ wedge" assumption holds on both.
            # `flush=True` guarantees the line is emitted even if stdout
            # isn't running unbuffered (the launcher exports
            # PYTHONUNBUFFERED, but a direct-CLI run might not).
            print(
                f"   ✓ Stored chunk {i + 1}/{len(chunks)}",
                flush=True,
            )
        print(f"   ✓ Stored {len(chunks)} chunks (vectors={sorted(last_slots)})", flush=True)
        return SyncOutcome(OUTCOME_SYNCED, doc_data["file_path"])
    except Exception as e:
        import traceback
        print(f"❌ Error syncing doc {file_path}: {e}")
        traceback.print_exc()
        return SyncOutcome(
            OUTCOME_FAILED, _relative_file_path(file_path), f"error: {e}"
        )


def _delete_doc_by_file_path(server: WeaviateMCPServer, file_path_value: str) -> int:
    """File_path-scoped dev-collection cleanup (v0.2.70 FIX #1).

    Mirror of :func:`_delete_node_by_file_path` for the development
    collection. ``file_path`` is unique per doc, so this never collides
    with an active sibling the way the title-scoped delete did.
    """
    if not DEV_COLLECTION_NAME:
        return 0
    try:
        coll = server.client.collections.get(DEV_COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=_file_path_filter(file_path_value),
            limit=100,
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


def sync_all_docs(server: WeaviateMCPServer) -> "SyncTally":
    """Walk DOCS_ROOT and sync every .md to the dev collection."""
    tally = SyncTally()
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION not set — skipping dev sync")
        return tally
    if not DOCS_ROOT.exists():
        print(f"ℹ️  No docs/ at {DOCS_ROOT} — skipping")
        return tally
    md_files = list(DOCS_ROOT.rglob("*.md"))
    total = len(md_files)
    print(f"📚 Found {total} markdown files in docs/")
    # v0.2.70 FIX C: running "doc M/N" counter (flush=True), same rationale as
    # sync_all_nodes — visibility for a long re-embed, no watchdog/timeout.
    for idx, md in enumerate(sorted(md_files), start=1):
        print(f"[{idx}/{total}] {md.name}", flush=True)
        tally.add(sync_doc(server, md))
        print(f"  → progress: {idx}/{total} docs processed "
              f"({tally.succeeded} ok, {tally.failed} failed, "
              f"{tally.skipped} skipped)", flush=True)
    return tally


def infer_tags_from_typed_links(
    server: WeaviateMCPServer,
    node_data: Dict,
    collection_name: Optional[str] = None,
) -> List[str]:
    """
    Infer tags from typed relationships BEFORE storing to Weaviate.

    Inference rules:
    1. Inherit capability tags from used/implemented tools
    2. Propagate domain tags through relationships

    Args:
        server: Weaviate MCP server instance
        node_data: Parsed node data with typed_links
        collection_name: Collection to read link targets from (v0.2.89
            BUG 6 — the node's TARGET collection so shared-scoped nodes
            infer from shared-collection siblings). Defaults to the
            project collection.

    Returns:
        List of inferred tags
    """
    typed_links = node_data.get("typed_links", [])
    existing_tags = set(node_data.get("tags", []))
    inferred_tags = []

    if not typed_links:
        return inferred_tags

    # Relationship types that propagate properties
    CAPABILITY_RELATIONS = ["uses", "implements", "buildsOn"]
    TAG_RELATIONS = ["uses", "implements", "extends", "buildsOn"]

    try:
        collection = server.client.collections.get(
            collection_name or COLLECTION_NAME
        )

        for link in typed_links:
            relation = link.get("relation_type", "")
            target_title = link.get("target_title", "")

            # Query target node
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(target_title) &
                       Filter.by_property("chunk_num").equal(1),
                limit=1,
                return_properties=["tags", "node_type"]
            )

            if not results.objects:
                continue

            target_props = results.objects[0].properties
            target_tags = target_props.get("tags", [])

            # Rule 1: Inherit capability tags from used/implemented tools
            if relation in CAPABILITY_RELATIONS:
                capability_tags = [t for t in target_tags if "-" in t]
                for cap in capability_tags:
                    if cap not in existing_tags and cap not in inferred_tags:
                        inferred_tags.append(cap)

            # Rule 2: Propagate domain tags through relationships
            if relation in TAG_RELATIONS:
                domain_tags = [t for t in target_tags if t.upper() == t or len(t) < 15]
                for tag in domain_tags:
                    if (tag not in existing_tags and
                        tag not in inferred_tags and
                        tag not in ["test", "project", "concept", "tool"]):
                        inferred_tags.append(tag)

    except Exception as e:
        # Inference is best-effort - don't fail sync if it errors
        pass

    return inferred_tags


def resolve_wikilinks_to_uuids(
    server: WeaviateMCPServer,
    wikilinks: List[str],
    collection_name: Optional[str] = None,
) -> List[str]:
    """
    Resolve WikiLink titles to Weaviate UUIDs.

    Args:
        server: Weaviate MCP server instance
        wikilinks: List of WikiLink titles (e.g., ["Node Title 1", "Node Title 2"])
        collection_name: Collection to resolve within (v0.2.89 BUG 6 — the
            node's TARGET collection, so cross-references from shared-scoped
            nodes point at shared-collection objects). Defaults to the
            project collection.

    Returns:
        List of UUIDs for matching nodes
    """
    if not wikilinks:
        return []

    try:
        collection = server.client.collections.get(
            collection_name or COLLECTION_NAME
        )
        uuids = []

        for link_title in wikilinks:
            # Query for nodes with matching title (case-insensitive)
            # Note: For chunked nodes, we want the parent node, not chunks
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(link_title) &
                       Filter.by_property("chunk_num").equal(1),  # Get first chunk (has full metadata)
                limit=1
            )

            if results.objects:
                uuids.append(str(results.objects[0].uuid))

        return uuids

    except Exception as e:
        print(f"    ⚠️  Could not resolve WikiLinks: {e}")
        return []


def _canonical_file_path(file_path: Path) -> str:
    """Return the project-relative file_path string used as a node's
    Weaviate dedup key, in canonical POSIX form (forward slashes).

    v0.2.92 WP-B1 (D13, Windows field audit): pre-fix this returned
    ``str(file_path.relative_to(PROJECT_ROOT))`` — host-OS-shaped, i.e.
    BACKSLASHES on Windows — while the MCP ``store_knowledge_node`` path
    stores the caller's POSIX spelling (server.py C-7 has normalized to
    POSIX at write since v0.2.75). Two shapes for one file meant the
    delete-by-file_path leg of this script's upsert MISSED the other
    writer's rows, so every alternating write INSERTED a duplicate set
    instead of replacing — exactly the 4x/2x duplicate objects observed
    in the field. ONE canonical shape (``vco_lib.paths.to_posix_rel``,
    the repo's shared normalizer) closes the class at the source.

    Every write site (``parse_markdown_node``, ``parse_doc_file``) and
    every delete/lookup site (``_relative_file_path`` → here,
    ``_file_path_filter``) routes through THIS function so they cannot
    drift apart again. Falls back to ``str(file_path)`` (POSIX-swapped)
    when the path isn't under PROJECT_ROOT — defensive, same as before.
    """
    try:
        rel = file_path.relative_to(PROJECT_ROOT)
    except ValueError:
        rel = file_path
    return to_posix_rel(rel)


def _relative_file_path(file_path: Path) -> str:
    """Canonical (POSIX) project-relative file_path — see
    :func:`_canonical_file_path`. Kept as the named entry point the
    archived-node cleanup and shared-scope migration already call."""
    return _canonical_file_path(file_path)


def _file_path_filter(canonical: str):
    """Weaviate filter matching ``file_path`` rows for *canonical* — BOTH
    spellings when a legacy backslash variant exists.

    v0.2.92 WP-B1 transition rule (state-keyed, not version-keyed): rows
    written by a pre-canonical Windows sync carry ``knowledge\\concepts\\
    foo.md``. A POSIX-only exact filter would MISS them, so the delete
    that accompanies every re-write would leave them behind and the
    insert would add a duplicate set — the exact defect this closes.
    Mirrors server.py's C-7 delete filter (OR of two EXACT ``.equal()``
    predicates — never ``contains_any``, which is token-based) so the
    sync script and the MCP delete with the same semantics.
    """
    backslash_variant = canonical.replace("/", "\\")
    if backslash_variant != canonical:
        return Filter.any_of([
            Filter.by_property("file_path").equal(canonical),
            Filter.by_property("file_path").equal(backslash_variant),
        ])
    return Filter.by_property("file_path").equal(canonical)


def _delete_node_by_file_path(server: WeaviateMCPServer, file_path_value: str) -> int:
    """Remove all Weaviate entries (incl. chunks) for a specific file_path.

    v0.2.70 FIX #1 (silent batch data loss): the archived-node cleanup
    previously deleted by ``title``. During a ``--all`` run, an archived node
    sharing its ``title`` with a DIFFERENT active node would delete the active
    node's rows too — so the active node silently vanished while ``sync_node``
    still returned True (counted as a success). ``file_path`` is unique per
    node, so scoping the cleanup to it removes only the archived file's own
    rows and never collides with an active sibling.

    Returns the number of objects deleted. Silent (returns 0) when the
    collection is missing or the connection is down — sync must not block
    on best-effort cleanup.
    """
    try:
        coll = server.client.collections.get(COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=_file_path_filter(file_path_value),
            limit=100,
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────
# v0.2.89 BUG 6: `scope: shared` frontmatter routing (Windows field audit).
#
# Frontmatter contract (top-level key):
#     scope: shared    # valid: project (default) | shared; any other
#                      # value → warn + treat as project
#
# `sync_node` mirrors `store_knowledge_node`'s semantics (server.py):
#   * targets_shared = scope=="shared" AND SHARED_COLLECTION_NAME nonempty
#     AND SHARED_COLLECTION_NAME != COLLECTION_NAME (identity case — the
#     orchestrator root — routes to the project collection, no special-
#     casing and no migration delete).
#   * Write gate keyed on the REQUESTED scope, not the resolved name
#     (v0.2.44 fix-now-6): scope=="shared" + SHARED_KG_WRITE_DISABLED →
#     the node FAILS with an explicit error — NO silent reroute.
#   * Access-matrix gate composes on top for shared writes (fail-open with
#     a warning on resolver absence/crash, deny only on explicit verdicts).
#   * project → shared transition: after a successful shared write the
#     same-file_path rows are deleted from the PROJECT collection.
#   * shared → project transition (key removed): shared rows are NOT
#     auto-deleted — deleting from a cross-project store on the evidence
#     of a local frontmatter edit risks destroying a colliding sibling
#     project's rows (`file_path` is not project-qualified in the shared
#     store). A one-line notice names the leftover rows instead.
# ──────────────────────────────────────────────────────────────────────

#: Count of nodes routed to the shared collection this run (successful
#: writes + embed-skips). Surfaced in main()'s `--all` summary line.
_SHARED_ROUTED_COUNT = 0


def _node_scope(node_data: Dict) -> Tuple[str, str]:
    """Normalize the node's requested scope. Returns ``(scope, warning)``.

    Absent key → ``("project", "")`` — zero behavior change for every
    existing node. Invalid value → ``("project", <warning text>)`` so the
    caller can print the warning once per node.
    """
    raw = node_data.get("scope")
    if raw is None:
        return "project", ""
    val = str(raw).strip().lower()
    if val in ("project", "shared"):
        return val, ""
    return "project", (
        f"Invalid frontmatter scope: {raw!r} (valid: project | shared) — "
        f"treating as project"
    )


def _resolve_shared_kg_write_disabled() -> bool:
    """Resolve the shared-write gate from env, honouring the legacy alias.

    MUST match ``weaviate_mcp.server._resolve_shared_kg_write_disabled``
    (the store_knowledge_node gate) so the sync script and the MCP agree on
    whether a shared write is allowed. Precedence:

      1. SHARED_KG_WRITE_DISABLED (canonical) — wins if SET, even to a
         falsy spelling ("false"/"0"/"") so users can explicitly RE-ENABLE
         writes on a project that had the legacy opt-out.
      2. SHARED_KG_OPT_OUT (legacy alias) — read only when the canonical
         key is literally absent from the environment.
      3. False (default: writes allowed).

    Resolved at CALL time (not import) so a mid-session override is
    honoured — same rationale as the MCP's call-time resolution.
    """
    canonical = os.environ.get("SHARED_KG_WRITE_DISABLED")
    if canonical is not None:
        return canonical.strip().lower() in ("1", "true", "yes")
    legacy = os.environ.get("SHARED_KG_OPT_OUT")
    if legacy is not None:
        return legacy.strip().lower() in ("1", "true", "yes")
    return False


def _shared_write_matrix_allows(target_collection_name: str) -> Tuple[bool, str]:
    """Access-matrix gate for shared writes — parity with store_knowledge_node.

    Returns ``(allowed, note)``; *note* (possibly empty) is a caller-printed
    one-liner. Mirrors server.py's composition rules:

      * resolver module not installed (pre-v0.2.49 path) → allow silently;
        any OTHER ImportError re-raises (a real bug must stay visible — MF5).
      * VCT_PROJECT_ID unset → allow + visible note (the MCP's v0.2.49 SB1
        silent-allow default; the JSONL metric/deferral machinery is
        MCP-local, the sync script surfaces the skip inline instead).
      * resolver crash → allow + warning (fail-open, loud — MF6 parity).
      * explicit non-"write" verdict → deny.

    Project-scope writes never consult this gate (plan §3.3.4).
    """
    try:
        from vco_lib.access_resolver import check_access_level
    except ImportError as imp_err:
        if "access_resolver" not in str(imp_err):
            raise
        return True, ""  # pre-v0.2.49 install: no resolver, no gate
    project_id = os.environ.get("VCT_PROJECT_ID", "")
    if not project_id:
        return True, "access-matrix gate skipped (VCT_PROJECT_ID unset)"
    try:
        level = check_access_level(project_id, target_collection_name)
    except Exception as gate_exc:  # noqa: BLE001 — fail-open by contract
        return True, f"access-matrix gate crashed ({gate_exc}); failing open"
    if level != "write":
        return False, (
            f"access matrix denies writes to '{target_collection_name}' "
            f"(level={level})"
        )
    return True, ""


def _finish_shared_scope_write(server: "WeaviateMCPServer", fp_value: str) -> None:
    """Post-success bookkeeping for a shared-routed node.

    Counts the node for the summary line and performs the project → shared
    MIGRATION DELETE: same-``file_path`` rows are removed from the PROJECT
    collection so the node doesn't surface twice. Runs on BOTH success exits
    (full write AND embed-skip fast path) so leftover project rows from a
    partially-failed earlier migration still get cleaned. Never fires in the
    identity case (shared == kg ⇒ ``targets_shared`` is False ⇒ not called).
    ``_delete_node_by_file_path`` is soft-fail by design (returns 0 on any
    error) — a failed cleanup never fails the node.
    """
    global _SHARED_ROUTED_COUNT
    _SHARED_ROUTED_COUNT += 1
    removed = _delete_node_by_file_path(server, fp_value)
    if removed:
        print(
            f"   ↪ moved out of project collection (scope: shared): removed "
            f"{removed} row(s) from '{COLLECTION_NAME}'"
        )


def _notice_leftover_shared_rows(server: "WeaviateMCPServer", fp_value: str) -> None:
    """Leftover-shared-rows notice (NO delete — conservative gate).

    Probe the shared collection for rows with the same ``file_path``. If
    any exist, print a one-line notice: they may be leftovers from a
    removed ``scope: shared`` key — or a colliding sibling project's
    legitimate shared rows (``file_path`` is not project-qualified in the
    shared store), which is exactly why they are NEVER auto-deleted here.
    Soft-fail: any probe error is silent.

    Called on the full-write PROJECT-routed path (removing a
    ``scope: shared`` key changes the content signature, so the first
    post-flip sync always takes the full-write path and the notice fires
    exactly when it matters; subsequent unchanged syncs hit the embed-skip
    fast path and stay quiet) AND on the archived-node skip path when the
    node declares ``scope: shared`` (wave-2 review F8 — archiving a shared
    node never auto-deletes its shared rows; this makes the persistence
    visible instead of silent).
    """
    if not SHARED_COLLECTION_NAME or SHARED_COLLECTION_NAME == COLLECTION_NAME:
        return
    try:
        coll = server.client.collections.get(SHARED_COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=_file_path_filter(fp_value),
            limit=100,
        )
        n = len(existing.objects)
        if n:
            print(
                f"   ℹ️  {n} row(s) for '{fp_value}' remain in shared "
                f"collection '{SHARED_COLLECTION_NAME}' — not auto-deleted "
                f"(a sibling project may own them; and if this file_path "
                f"collides with a curated path, the shared rows may be the "
                f"ROOT's canonical curated node). If this node was "
                f"previously `scope: shared`, verify the shared rows' "
                f"title/content actually match this node before any manual "
                f"delete (e.g. delete-by-file_path via the launcher's "
                f"Weaviate panel)."
            )
    except Exception:
        pass


def _is_archived_node(file_path: Path, frontmatter: dict | None = None) -> tuple[bool, str]:
    """Decide whether a node should be excluded from Weaviate sync.

    Returns (is_archived, reason). A node is archived if either:
      - its filesystem path contains an `archive/` segment (knowledge/archive/...
        or any docs subtree), including the dot/underscore-prefixed
        conventions `.archive/` and `_archive/`, OR
      - its frontmatter `status` is `"archived"`, `"deprecated"`, or
        `"superseded"`.

    `superseded` was added 2026-05-22: nodes marked `status: superseded` (in
    favour of a canonical replacement) were silently still being synced to
    Weaviate because this check only recognised `archived|deprecated`. The
    cleanup pass that day discovered three such nodes still appearing in MCP
    results: `weaviate-usage-patterns`, `VLM_Prompt_Engineering_Best_Practices_2026`,
    `WD14_Tag_Rotation_Strategy`. Authors who write `status: superseded` mean
    "should disappear from KG queries"; honour that.

    v0.2.70 FIX #5: the path leg previously matched only the exact segment
    ``"archive"``. The widely-used dot/underscore variants ``.archive`` and
    ``_archive`` (Obsidian's hidden-folder convention; common ``_archive/``
    layouts) slipped through and got indexed. We now match those three exact
    segment forms. We deliberately do NOT do a substring match — that would
    wrongly skip legitimate dirs like ``architecture/`` or ``archived-specs/``.

    Archived nodes are kept on disk (so future-anyone can grep / read history)
    but skipped on Weaviate sync — they shouldn't return from KG queries.
    The `_stale_filter()` at query time provides a second layer (in case an
    archived node slips through with a real `valid_until` in the past), but
    upstream skipping is the cleaner default: it keeps the index lean and
    avoids paying embedding cost for content that won't surface.
    """
    parts = file_path.parts
    # Exact segment match for `archive`, `.archive`, `_archive` — NOT a
    # substring match (would catch `architecture/`, `archived-notes/`).
    _ARCHIVE_DIR_SEGMENTS = {"archive", ".archive", "_archive"}
    archive_hit = next((p for p in parts if p in _ARCHIVE_DIR_SEGMENTS), None)
    if archive_hit is not None:
        return True, f"path contains {archive_hit!r} segment ({file_path})"
    if frontmatter is not None:
        status = (frontmatter.get("status") or "").strip().lower()
        if status in ("archived", "deprecated", "superseded"):
            return True, f"frontmatter status={status!r}"
    return False, ""


# NEW-11 (2026-05-28): normalize typed_links to list-of-objects before any
# Weaviate insert.  Pre-canonicalization writers emitted list-of-strings in
# "relation::target" form; Weaviate's gRPC serializer cannot pack
# []interface{} and raises "creating primitive value for typed_links: proto:
# invalid type: []interface {}" which crashes the whole iterator.
#
# Canonical shape: [{"relation_type": str, "target_title": str}, ...]
#
# Three cases handled:
#   • list-of-objects with correct keys  → returned unchanged
#   • list-of-strings ("rel::target")    → parsed and converted
#   • anything else (single str, None…)  → warning logged, field dropped
def _normalize_typed_links(typed_links: object, context: str = "") -> list:
    """Return typed_links in the canonical list-of-objects shape.

    Args:
        typed_links: raw value from node_data (any type coming from disk).
        context: description of the node being written (for warning messages).

    Returns:
        list of {"relation_type": str, "target_title": str} dicts (may be empty).
    """
    if not typed_links:
        # None, [], empty string — treat as empty; no warning needed
        return []

    if not isinstance(typed_links, list):
        print(
            f"   ⚠ typed_links: unexpected type {type(typed_links).__name__!r} "
            f"for {context!r} — dropping field to avoid gRPC crash"
        )
        return []

    normalized: list = []
    for item in typed_links:
        if isinstance(item, dict):
            # Canonical shape — validate required keys are present
            if "relation_type" in item and "target_title" in item:
                normalized.append(item)
            else:
                print(
                    f"   ⚠ typed_links item missing required keys {list(item.keys())!r} "
                    f"for {context!r} — skipping item"
                )
        elif isinstance(item, str):
            # Legacy "relation::target" string form — parse and convert
            if "::" in item:
                relation, _, target = item.partition("::")
                normalized.append({"relation_type": relation.strip(), "target_title": target.strip()})
            else:
                # Plain string with no separator — treat as relatedTo
                print(
                    f"   ⚠ typed_links string {item!r} has no '::' separator "
                    f"for {context!r} — storing as relatedTo"
                )
                normalized.append({"relation_type": "relatedTo", "target_title": item.strip()})
        else:
            print(
                f"   ⚠ typed_links item type {type(item).__name__!r} unexpected "
                f"for {context!r} — skipping item"
            )
    return normalized


def sync_node(server: WeaviateMCPServer, file_path: Path) -> "SyncOutcome":
    """
    Sync a single knowledge node to Weaviate (with chunking support)

    Args:
        server: Weaviate MCP server instance
        file_path: Path to markdown file

    Returns:
        SyncOutcome — truthy (bool(outcome) is True) when the file did not
        FAIL; see the v0.2.92 WP-B1 block above `sync_doc` for why the
        plain-bool return became a categorized outcome.
    """
    start_time = time.time()
    chunks_created = 0
    error_msg = None

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            error_msg = "File not found"
            return SyncOutcome(
                OUTCOME_FAILED, _relative_file_path(file_path), "file not found"
            )

        # Skip archived nodes — see _is_archived_node docstring. Do this
        # BEFORE the timestamp-update side effect so editing an archived
        # node doesn't bump its `updated:` field for no reason.
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived node: {reason}")
            # If it was previously synced (archived after sync), drop it
            # from Weaviate so stale content stops surfacing.
            # v0.2.70 FIX #1: delete by file_path (unique per node), NOT by
            # title — a title-scoped delete here silently removed any active
            # sibling sharing this archived node's title during a --all run.
            try:
                fp_value = _relative_file_path(file_path)
                removed = _delete_node_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior Weaviate entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            # Wave-2 review F8: an archived `scope: shared` node's SHARED
            # rows are never auto-deleted (collision class — see the
            # `_node_scope` contract block); surface the persistence with
            # the one-line leftover-rows notice instead of staying silent.
            # Frontmatter isn't parsed on this path — sniff the scope key,
            # soft-fail (any read/parse error ⇒ no notice).
            try:
                fm, _fm_body = parse_frontmatter(
                    file_path.read_text(encoding="utf-8")
                )
                if _node_scope(fm or {})[0] == "shared":
                    _notice_leftover_shared_rows(
                        server, _relative_file_path(file_path)
                    )
            except Exception:
                pass
            return SyncOutcome(
                OUTCOME_ARCHIVED_SKIPPED,
                _relative_file_path(file_path),
                f"archived node: {reason}",
            )  # not a sync failure — intentional skip (now counted as
            # "skipped", never "succeeded" — v0.2.92 WP-B1 / D12)

        # Read, auto-update `updated:` timestamp, write back, then parse
        content = file_path.read_text(encoding='utf-8')
        content = _update_frontmatter_timestamp(file_path, content)
        node_data = parse_markdown_node(content, file_path)

        # Defence in depth: in case the path-based check missed a frontmatter-only
        # archive marker (e.g. status: archived but path doesn't contain 'archive/'),
        # check again after parsing. Same skip + delete behaviour.
        archived2, reason2 = _is_archived_node(file_path, frontmatter=node_data)
        if archived2:
            print(f"⊘ Skipping (frontmatter): {reason2}")
            # v0.2.70 FIX #1: delete by file_path (the stored dedup key), NOT
            # by title — see the path-based archive branch above for the
            # cross-node title-collision data-loss this prevents. node_data
            # carries the canonical relative file_path already.
            try:
                fp_value = node_data.get("file_path") or _relative_file_path(file_path)
                removed = _delete_node_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior Weaviate entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            # Wave-2 review F8: same shared-rows visibility as the
            # path-based archive branch above — an archived `scope: shared`
            # node's shared rows persist by design (never auto-deleted);
            # say so instead of staying silent.
            try:
                if _node_scope(node_data)[0] == "shared":
                    _notice_leftover_shared_rows(
                        server,
                        node_data.get("file_path")
                        or _relative_file_path(file_path),
                    )
            except Exception:
                pass
            return SyncOutcome(
                OUTCOME_FRONTMATTER_SKIPPED,
                node_data.get("file_path") or _relative_file_path(file_path),
                f"archived node (frontmatter): {reason2}",
            )

        # ── v0.2.89 BUG 6: per-node target selection (`scope:` frontmatter).
        # See the routing-contract comment block above `_node_scope`.
        requested_scope, scope_warning = _node_scope(node_data)
        if scope_warning:
            print(f"⚠️  {scope_warning}")
        targets_shared = (
            requested_scope == "shared"
            and bool(SHARED_COLLECTION_NAME)
            and SHARED_COLLECTION_NAME != COLLECTION_NAME
        )
        # Write gate — keyed on the REQUESTED scope, not the resolved name
        # (v0.2.44 fix-now-6 semantics, mirrored from store_knowledge_node):
        # after the orchestrator-root rebind (KG == SHARED) a name-equality
        # predicate would never fire; the gate's intent is "block cross-
        # project shared writes", which is scope=='shared' regardless of the
        # physical collection. Refuse LOUDLY — no silent reroute to the
        # project collection (the node's declared destination was refused).
        if (
            requested_scope == "shared"
            and SHARED_COLLECTION_NAME
            and _resolve_shared_kg_write_disabled()
        ):
            # NOT dead: consumed by the finally-block ToolUsageLogger row
            # (success=error_msg is None, error=error_msg) — removing it
            # would log this refused write as success=True.
            error_msg = "shared KG writes disabled (SHARED_KG_WRITE_DISABLED)"
            print(
                f"❌ {_relative_file_path(file_path)}: scope: shared refused — "
                "shared KG writes are disabled for "
                "this project (SHARED_KG_WRITE_DISABLED). Either set "
                "SHARED_KG_WRITE_DISABLED=false to enable shared writes, or "
                "drop the `scope: shared` frontmatter key to keep the node "
                "project-scoped. Not rerouting to the project collection."
            )
            return SyncOutcome(
                OUTCOME_FAILED,
                _relative_file_path(file_path),
                "scope: shared refused — SHARED_KG_WRITE_DISABLED",
            )
        if targets_shared:
            matrix_allowed, matrix_note = _shared_write_matrix_allows(
                SHARED_COLLECTION_NAME
            )
            if matrix_note:
                print(f"   ⚠️  {matrix_note}")
            if not matrix_allowed:
                # NOT dead: consumed by the finally-block ToolUsageLogger
                # row — see the write-gate branch above.
                error_msg = "access matrix denies shared write"
                print(
                    f"❌ {_relative_file_path(file_path)}: scope: shared refused — "
                    f"the launcher's access matrix "
                    f"denies this project write access to "
                    f"'{SHARED_COLLECTION_NAME}'. Grant write access in the "
                    f"launcher GUI, or drop the `scope: shared` key."
                )
                return SyncOutcome(
                    OUTCOME_FAILED,
                    _relative_file_path(file_path),
                    f"scope: shared refused — access matrix denies writes to "
                    f"'{SHARED_COLLECTION_NAME}'",
                )
        target_collection_name = (
            SHARED_COLLECTION_NAME if targets_shared else COLLECTION_NAME
        )
        if targets_shared:
            print(f"   ↪ scope: shared → routing to '{target_collection_name}'")

        # Validate against vocabulary (report warnings, don't block sync)
        validation_warnings = validate_node_against_vocabulary(node_data, file_path)
        if validation_warnings:
            print(f"⚠️  Vocabulary validation warnings ({len(validation_warnings)}):")
            for warning in validation_warnings[:3]:  # Show first 3
                print(f"   - {warning}")
            if len(validation_warnings) > 3:
                print(f"   ... and {len(validation_warnings) - 3} more warnings")

        # Run inference BEFORE storing (enrich with inferred tags).
        # v0.2.89 BUG 6: inference reads the TARGET collection so a shared-
        # scoped node inherits tags from its shared-collection link targets.
        inferred_tags = infer_tags_from_typed_links(
            server, node_data, collection_name=target_collection_name
        )
        if inferred_tags:
            # Add inferred tags to node data (will be stored with original tags)
            node_data["tags"] = list(set(node_data["tags"] + inferred_tags))
            print(f"🧠 Inferred {len(inferred_tags)} tags from typed relationships")

        print(f"🔄 Syncing node: {node_data['title']} ({node_data['node_type']})")
        print(f"   Tags: {', '.join(node_data['tags']) if node_data['tags'] else 'none'}")
        total_links = len(node_data['links']) + len(node_data['typed_links'])
        typed_count = len(node_data['typed_links'])
        print(f"   Links: {total_links} connections ({typed_count} typed)")

        # v0.2.17 (plan 0.2): compute content-hash for embed-skip.
        # Uses the same signature function as the file-write skip
        # (_content_signature_excluding_updated), so a file whose only
        # delta is the `updated:` timestamp hashes identically to its
        # pre-sync state — exactly what we want for the no-op fast
        # path on every install.py --update.
        current_content_hash = _content_signature_excluding_updated(content)

        # Delete old version (by file_path).
        # v0.2.89 BUG 6: EVERY downstream step (existing-object query,
        # embed-skip fast path, delete-and-reinsert, chunked path) operates
        # on the SELECTED collection — the fast path in particular MUST
        # query the TARGET collection, or the skip check would silently
        # read the wrong store.
        collection = server.client.collections.get(target_collection_name)

        # Query for existing nodes with same file_path — BOTH spellings
        # (v0.2.92 WP-B1 / D13): a legacy Windows-written row carries the
        # backslash variant; an exact POSIX-only filter would miss it, the
        # delete below would skip it, and the insert would duplicate it.
        where_filter = _file_path_filter(node_data["file_path"])
        existing = collection.query.fetch_objects(
            filters=where_filter,
            limit=100,
            return_properties=["file_path", "content_hash", "chunk_num", "total_chunks"],
        )

        # v0.2.17 (plan 0.2): EMBED-SKIP fast path. If every existing
        # object for this file_path has content_hash matching the
        # current source hash AND the count of objects matches what
        # we'd reproduce (single-chunk → 1, multi-chunk → N), skip
        # the delete-and-re-embed pipeline entirely. Saves hundreds
        # of Ollama embed calls + Weaviate roundtrips per re-sync
        # pass when content is unchanged.
        #
        # Conservative gating (Reviewer A finding E2 + original
        # design): skip ONLY when ALL existing objects' content_hash
        # matches AND at least one is non-empty AND the count of
        # existing objects matches the `total_chunks` recorded on
        # each (so a previous crash mid-chunk-write — leaving e.g.
        # 3/4 chunks with the new hash — does NOT cause a permanent
        # skip with missing chunk 4). If anything looks off, fall
        # through to the delete-and-re-embed path. Soft-fail: any
        # exception here also falls through.
        try:
            existing_hashes: List[str] = []
            existing_total_chunks: List[int] = []
            existing_file_paths: List[str] = []
            for obj in existing.objects:
                props = obj.properties or {}
                existing_hashes.append(props.get("content_hash", "") or "")
                # total_chunks may be int OR (legacy) missing/None.
                # Treat missing as 0 → forces fall-through.
                tc = props.get("total_chunks", 0)
                try:
                    existing_total_chunks.append(int(tc) if tc is not None else 0)
                except (TypeError, ValueError):
                    existing_total_chunks.append(0)
                existing_file_paths.append(props.get("file_path", "") or "")

            chunk_count_ok = (
                len(existing_total_chunks) > 0
                and all(tc == len(existing_total_chunks) for tc in existing_total_chunks)
            )
            # v0.2.92 WP-B1 (D13): the skip may only fire when no found
            # row reports a NON-canonical (legacy backslash) spelling — a
            # legacy-shaped row reached through the dual-shape filter must
            # NOT be preserved by the fast path; it falls through to
            # delete-and-rewrite so the row shape itself heals. A row that
            # does not report a file_path at all (older clients / fixtures
            # that ignore return_properties) cannot be judged and keeps
            # the pre-v0.2.92 skip semantics (conservative default: no
            # new re-embed on unverifiable data).
            shapes_canonical = all(
                fp in ("", node_data["file_path"]) for fp in existing_file_paths
            )
            # v0.2.92 chunk-plan transition repair: a revision crossing is
            # pending when the deferral ledger carries
            # `chunker_preset_overhaul_pending`. In that state a
            # self-consistent row set is NOT sufficient to skip — compare
            # the stored plan against what the CURRENT chunker produces for
            # this content. Unchanged plan → still skip (the overwhelming
            # majority); changed plan → fall through to delete-and-re-embed
            # so this entry re-chunks. No crossing pending → exactly the
            # pre-v0.2.92 semantics (no plan CPU paid).
            _self_consistent = (
                len(existing_hashes) > 0
                and all(h == current_content_hash for h in existing_hashes)
                and all(h for h in existing_hashes)  # no empty strings
                and chunk_count_ok
                and shapes_canonical
            )
            _plan_ok = True
            if _self_consistent and _chunker_resync_pending():
                _plan_ok = _stored_plan_matches_current(
                    server, collection, node_data["file_path"], content,
                    len(existing_hashes),
                )
                if not _plan_ok:
                    global _RECHUNKED_COUNT
                    _RECHUNKED_COUNT += 1
                    print(
                        f"   ♻️  Re-chunking: stored chunk plan predates the "
                        f"current chunker revision (revision-crossing repair)"
                    )
            all_match = _self_consistent and _plan_ok
            if all_match:
                elapsed = time.time() - start_time
                print(
                    f"   ⏭️  Embed-skip: content_hash matches "
                    f"({current_content_hash[:12]}…); "
                    f"{len(existing_hashes)} chunk(s) preserved "
                    f"({elapsed*1000:.0f} ms)"
                )
                # Return success without delete/embed/insert. The
                # caller's success_count/fail_count tally still
                # counts this as a successful sync — the data is
                # already in Weaviate.
                # v0.2.89 BUG 6: a shared-routed node still runs the
                # project→shared migration cleanup on this exit, so
                # leftover project rows from a partially-failed earlier
                # migration get cleaned even when the shared rows are
                # already up to date.
                if targets_shared:
                    _finish_shared_scope_write(server, node_data["file_path"])
                return SyncOutcome(
                    OUTCOME_EMBED_SKIPPED,
                    node_data["file_path"],
                    "content_hash match — already current in Weaviate",
                )
        except Exception as skip_err:  # noqa: BLE001 — soft-fail by design
            # Fall through to delete-and-re-embed. Log so future
            # debugging knows the fast path tried but didn't apply.
            print(f"   (embed-skip check failed: {skip_err}; re-embedding)")

        deleted_count = 0
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

        if deleted_count > 0:
            print(f"   ✓ Deleted {deleted_count} old version(s)")

        # Check if content needs chunking.
        # W8 (v0.2.92 wiring audit): the gate AND the boundaries come from
        # the ONE shared plan (`_plan_for` → `kg_chunk_plan.plan_node_chunks`)
        # that the MCP `store_knowledge_node` and the `--rechunk` comparison
        # also use — two separate calls here (threshold, then chunker) is
        # what let the two writers drift for the same node.
        source_node_id = str(uuid.uuid4())
        _plan = _plan_for(
            server, content,
            source_id=source_node_id,
            metadata={
                "title": node_data["title"],
                "file_path": node_data["file_path"],
                "node_type": node_data["node_type"],
            },
        )
        token_count = TokenCounter.count_tokens(content)
        print(f"   Content size: {token_count} tokens")
        _max_tokens = _plan.threshold

        if _plan.is_single:
            # Single chunk - store as-is
            print("   Storing as single object")

            # v0.2.70 Part 2 + v0.2.89 §8.5: try a pre-shipped embedding
            # FIRST. Both guards (content_hash staleness + active-slot
            # match) live inside _shipped_vector_for; with dual embedding
            # enabled, a hit also merges every OTHER configured slot's
            # sidecar vector for this node (never computing for a missing
            # secondary). expected_chunks=1 for the single-object path.
            _shipped = _shipped_vector_for(
                server, KNOWLEDGE_ROOT, current_content_hash, expected_chunks=1
            )
            if _shipped is not None:
                vec_arg, slots_written = _shipped
                # Shipped sidecar vectors carry NO truncation record (computed
                # elsewhere, at release time) — the row deliberately gets NO
                # truncated_slots property, so its state resolves UNKNOWN for
                # every slot (never a guessed False). Do not stamp here.
                print(
                    f"   📦 Ingested shipped vector(s) "
                    f"(slots={sorted(slots_written)}, no embed call)"
                )
            else:
                # v0.2.18: build vector arg via EmbeddingService. With
                # DUAL_EMBEDDING_ENABLED=true (default) this fans out to every
                # reachable text backend so multiple slots get populated.
                vec_arg, slots_written, truncated_slots = _build_vector_arg(
                    server, content
                )

            # Prepare data object
            data_obj = {
                "title": node_data["title"],
                "content": node_data["content"],
                "file_path": node_data["file_path"],
                "node_type": node_data["node_type"],
                "tags": node_data["tags"],
                "links": node_data["links"],
                # NEW-11 (2026-05-28): guard against legacy list-of-strings form
                # that crashes Weaviate gRPC serialiser with "invalid type:
                # []interface {}".  Canonical shape: list-of-objects.
                "typed_links": _normalize_typed_links(
                    node_data["typed_links"], context=node_data.get("title", "")
                ),
                "external_links": node_data["external_links"],  # External links (RDF)
                "created_at": node_data["created_at"],
                "updated_at": node_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": source_node_id,
                # v0.2.17 (plan 0.2): persist content_hash so the next
                # re-sync can skip the embed pipeline when unchanged.
                "content_hash": current_content_hash,
            }
            # W3: persist the per-call truncation record on the COMPUTED
            # embed path only — a shipped-sidecar ingest leaves the row
            # without the property (UNKNOWN, never a guessed False).
            if _shipped is None:
                data_obj.update(
                    truncation_tag_properties(
                        truncated_slots, server.text_vector_slot,
                        measured_slots=slots_written,
                    )
                )

            # Add temporal metadata if present
            for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                if field in node_data:
                    data_obj[field] = node_data[field]

            # Insert into the configured named-vector slots. With multi-
            # slot writes (DUAL_EMBEDDING_ENABLED=true, the default since
            # v0.2.18) every reachable backend's vector lands in its own
            # slot — so a project switched from qwen3 → openai still has
            # qwen3_embed populated and search-with-qwen3 keeps working
            # during the transition.
            #
            # NEVER cross-write a vector under a name that implies a
            # different model: each backend's vectors go ONLY into the
            # slot whose embedding-space matches. The EmbeddingService
            # multi-slot fan-out enforces this — same model → same slot.
            #
            # Note for Weaviate 1.31+: `Reconfigure.NamedVectors.add()`
            # lets us add new named vectors after creation. The Wave A
            # `vco_lib.weaviate_schema.add_named_vector_slot` helper uses
            # this when available; the schema-creation block in
            # `ensure_collection_exists` declares every slot up-front for
            # older Weaviate versions where post-create adds aren't
            # supported.
            obj_uuid = collection.data.insert(
                properties=data_obj,
                vector=vec_arg
            )

            chunks_created = 1
            print(f"   ✓ Stored node with UUID: {str(obj_uuid)[:8]}... (vectors={sorted(slots_written)})")

            # Create cross-references for WikiLinks.
            # v0.2.89 BUG 6: resolve within the TARGET collection — Weaviate
            # cross-references must point at objects in the collection the
            # `linksTo` property targets, so a shared-routed node resolves
            # its links against the shared store.
            if node_data["links"]:
                target_uuids = resolve_wikilinks_to_uuids(
                    server, node_data["links"],
                    collection_name=target_collection_name,
                )
                if target_uuids:
                    for target_uuid in target_uuids:
                        try:
                            collection.data.reference_add(
                                from_uuid=obj_uuid,
                                from_property="linksTo",
                                to=target_uuid
                            )
                        except Exception as e:
                            # Silently skip if reference already exists or target not found
                            pass
                    print(f"   ✓ Created {len(target_uuids)} cross-references")

        else:
            # Multiple chunks needed
            print(f"   ⚠️  Content exceeds {_max_tokens} tokens - chunking required")

            # W8: the boundaries come from the SAME plan the gate above
            # decided on (one `_plan_for` call), and `source_node_id` is the
            # per-write id that plan was built with — so the gate, the chunk
            # sizes and the stored ids cannot disagree.
            chunks = _plan.chunks

            print(f"   Split into {len(chunks)} chunks")

            # Store each chunk
            last_slots: Mapping[str, List[float]] = {}
            _total_chunks = len(chunks)
            for i, chunk in enumerate(chunks):
                # v0.2.70 Part 2: per-chunk shipped-vector ingest (NO-OP this
                # release). Same guards as the single-object path. The shipped
                # entry must cover EXACTLY this node's chunk count; if the node
                # would chunk differently than the shipped vectors expect, every
                # chunk falls back to compute (the count guard is enforced
                # inside _shipped_chunk_vector, so we never mix shipped + freshly
                # computed chunk vectors for the same node).
                _shipped = _shipped_chunk_vector(
                    server,
                    KNOWLEDGE_ROOT,
                    current_content_hash,
                    chunk_num=chunk.chunk_number + 1,
                    expected_chunks=_total_chunks,
                )
                if _shipped is not None:
                    vec_arg, last_slots = _shipped
                    # Shipped sidecar vector — no truncation record: the row
                    # keeps NO truncated_slots property (UNKNOWN for every
                    # slot, never a guessed False). See the single-chunk
                    # path's shipped note.
                else:
                    # v0.2.18: embed via EmbeddingService (multi-slot when
                    # DUAL_EMBEDDING_ENABLED — see _build_vector_arg).
                    vec_arg, last_slots, truncated_slots = _build_vector_arg(
                        server, chunk.content
                    )

                # Prepare data object (tags, links, typed_links, external_links shared across all chunks)
                data_obj = {
                    "title": node_data["title"],
                    "content": chunk.content,
                    "file_path": node_data["file_path"],
                    "node_type": node_data["node_type"],
                    "tags": node_data["tags"],
                    "links": node_data["links"],
                    # NEW-11 (2026-05-28): same guard as single-chunk path above.
                    "typed_links": _normalize_typed_links(
                        node_data["typed_links"], context=node_data.get("title", "")
                    ),
                    "external_links": node_data["external_links"],  # External links (RDF)
                    "created_at": node_data["created_at"],
                    "updated_at": node_data["updated_at"],
                    "chunk_num": chunk.chunk_number + 1,  # 1-indexed
                    "total_chunks": chunk.total_chunks,
                    "source_node_id": source_node_id,
                    # v0.2.17 (plan 0.2): every chunk of the same file
                    # shares the same content_hash (computed over the
                    # whole file). The embed-skip check in sync_node
                    # requires ALL chunks for a file_path to carry an
                    # identical, non-empty hash before it skips —
                    # writing the same value here keeps that invariant.
                    "content_hash": current_content_hash,
                }
                # W3: per-chunk truncation record on the COMPUTED embed path
                # only (shipped-ingest chunks stay record-less → UNKNOWN).
                if _shipped is None:
                    data_obj.update(
                        truncation_tag_properties(
                            truncated_slots, server.text_vector_slot,
                            measured_slots=last_slots,
                        )
                    )

                # Add temporal metadata if present
                for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                    if field in node_data:
                        data_obj[field] = node_data[field]

                # Insert with the v0.2.18 multi-slot vector arg from
                # _build_vector_arg. See single-chunk path comment for
                # the rationale.
                obj_uuid = collection.data.insert(
                    properties=data_obj,
                    vector=vec_arg
                )

                # Create cross-references only from first chunk (represents the main node)
                # v0.2.89 BUG 6: resolve within the TARGET collection (see
                # the single-chunk path comment).
                if chunk.chunk_number == 0 and node_data["links"]:
                    target_uuids = resolve_wikilinks_to_uuids(
                        server, node_data["links"],
                        collection_name=target_collection_name,
                    )
                    if target_uuids:
                        for target_uuid in target_uuids:
                            try:
                                collection.data.reference_add(
                                    from_uuid=obj_uuid,
                                    from_property="linksTo",
                                    to=target_uuid
                                )
                            except Exception as e:
                                pass
                        print(f"   ✓ Created {len(target_uuids)} cross-references")

                chunks_created += 1
                # v0.2.69 FIX 3 (review SHOULD-FIX): per-chunk heartbeat
                # feeds the launcher's re-armed-per-line stall watchdog.
                # `flush=True` guarantees prompt emission even on a
                # direct-CLI run that doesn't inherit PYTHONUNBUFFERED.
                print(
                    f"   ✓ Stored chunk {chunk.chunk_number + 1}/{chunk.total_chunks} ({chunk.token_count} tokens)",
                    flush=True,
                )

            if last_slots:
                print(f"   ✓ All chunks written to vectors={sorted(last_slots)}")

        # v0.2.89 BUG 6 scope transitions (see the contract block above
        # `_node_scope`): shared-routed → migrate same-file_path rows OUT of
        # the project collection; project-routed → advisory-only probe for
        # leftover shared rows (never auto-deleted — collision class).
        if targets_shared:
            _finish_shared_scope_write(server, node_data["file_path"])
        else:
            _notice_leftover_shared_rows(server, node_data["file_path"])

        print(f"✅ Successfully synced {node_data['title']}")
        return SyncOutcome(OUTCOME_SYNCED, node_data["file_path"])

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error syncing node {file_path}: {e}")
        import traceback
        traceback.print_exc()
        return SyncOutcome(
            OUTCOME_FAILED, _relative_file_path(file_path), f"error: {e}"
        )

    finally:
        # Log usage
        if HAS_LOGGER:
            duration_ms = (time.time() - start_time) * 1000
            # v0.2.40 H1: stamp the resolved project name on the
            # tool_usage.jsonl row so per-event metadata matches the
            # KG / code-graph project identifier. Pre-fix the entry's
            # ``project`` field always fell back to the logger's
            # ``"claude-orchestrator"`` default, regardless of which
            # workspace the script ran in. Resolution is best-effort
            # via the canonical helper in ``vco_lib.paths`` — None
            # preserves the historic default downstream.
            try:
                from vco_lib.paths import resolve_project_name as _resolve_project_name
                _project = _resolve_project_name()
            except Exception:
                _project = None
            ToolUsageLogger.log_kg_sync(
                file_path=str(file_path),
                chunks_created=chunks_created,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg,
                project=_project,
            )


def sync_all_nodes(server: WeaviateMCPServer) -> "SyncTally":
    """
    Sync all knowledge graph markdown files

    Args:
        server: Weaviate MCP server instance

    Returns:
        SyncTally — succeeded counts real writes only; archived /
        frontmarker / excluded / embed-skipped nodes are counted as
        SKIPPED, never as succeeded (v0.2.92 WP-B1 / D12).
    """
    tally = SyncTally()

    # Find all .md files in knowledge/
    md_files = list(KNOWLEDGE_ROOT.rglob("*.md"))

    # Exclude meta files (schema/reference documentation, not searchable content)
    EXCLUDED_FILES = {'TAG_HIERARCHY.md', 'VOCABULARY.md'}
    # v0.2.92 WP-B1: excluded meta files are recorded in the tally (with a
    # reason) instead of silently vanishing from every count — "Found N"
    # below deliberately still reports the post-exclusion total, unchanged.
    for f in md_files:
        if f.name in EXCLUDED_FILES:
            tally.add(SyncOutcome(
                OUTCOME_EXCLUDED_SKIPPED,
                _relative_file_path(f),
                f"excluded meta file ({f.name}) — never synced by design",
            ))
    md_files = [f for f in md_files if f.name not in EXCLUDED_FILES]

    total = len(md_files)
    print(f"📚 Found {total} markdown files in knowledge/")
    print()

    # v0.2.70 FIX C: emit a running "node M/N" counter (flush=True) so a long
    # full re-embed (e.g. an arctic model-swap over thousands of shared-KG
    # nodes) shows forward motion on install.py's inherited stdout — the cure
    # for "appears hung" is visibility, NOT a watchdog/timeout. Pure feedback:
    # no timer, no kill. Per-chunk heartbeats inside sync_node remain the
    # finer-grained signal for big single nodes.
    for idx, md_file in enumerate(sorted(md_files), start=1):
        print(f"[{idx}/{total}] {md_file.name}", flush=True)
        tally.add(sync_node(server, md_file))
        print(f"  → progress: {idx}/{total} nodes processed "
              f"({tally.succeeded} ok, {tally.failed} failed, "
              f"{tally.skipped} skipped)", flush=True)
        print()  # Blank line between nodes

    return tally


def _classify_sync_target(raw: str) -> Tuple[Path, bool, bool]:
    """Classify an explicit sync-target path as knowledge / docs / neither.

    v0.2.70 FIX #6: a symlink physically located under ``docs/`` (or
    ``knowledge/``) whose TARGET lives outside the tree used to be rejected.
    The old code ran ``Path(raw).resolve()`` first — which rewrites a symlink
    to its out-of-tree target — then checked ``relative_to(DOCS_ROOT)``, so
    the file was reported "not in knowledge/ or docs/ — skipping".

    We classify by the path's LOCATION first. ``os.path.abspath`` normalises
    ``..`` / cwd lexically WITHOUT resolving the final component's symlink, so
    a link sitting under ``docs/`` is recognised by where the user placed it.
    The resolved form is checked as a fallback so a user who passes a path
    THROUGH a symlinked ancestor (e.g. a symlinked repo root) still matches.

    Returns ``(file_path, in_knowledge, in_docs)``. ``file_path`` is the
    location path when that form is in-tree (so the stored ``file_path``
    property reflects the docs/ location and ``read_text()`` follows the link
    to load content), otherwise the resolved path.
    """
    loc = Path(os.path.abspath(raw))
    try:
        resolved = Path(raw).resolve()
    except OSError:
        resolved = loc

    def _under(cand: Path, root: Path) -> bool:
        try:
            cand.relative_to(root)
            return True
        except ValueError:
            return False

    loc_in_knowledge = _under(loc, KNOWLEDGE_ROOT)
    loc_in_docs = _under(loc, DOCS_ROOT)
    res_in_knowledge = _under(resolved, KNOWLEDGE_ROOT)
    res_in_docs = _under(resolved, DOCS_ROOT)

    in_knowledge = loc_in_knowledge or res_in_knowledge
    in_docs = loc_in_docs or res_in_docs

    # Prefer the location path when it is itself in-tree; otherwise the
    # resolved path carried us in-tree (symlinked-ancestor case).
    if loc_in_knowledge or loc_in_docs:
        file_path = loc
    else:
        file_path = resolved

    return file_path, in_knowledge, in_docs


#: Cap on per-path detail lines printed to stdout at the end of a run —
#: the FULL list always goes to the run log (below), so a 400-node drift
#: fix doesn't bury the terminal in lines it can't act on anyway.
_DETAILS_PRINT_CAP = 50


def _details_log_path() -> "Optional[Path]":
    """`<vct_root>/logs/kg-sync-<UTC ts>.log` — the full-list sink for a
    run's not-synced details. Honors ``VCT_STATE_DIR`` through
    ``vco_lib.paths.vct_root_dir()``. Returns None (and never raises) when
    the state root can't be resolved."""
    try:
        from vco_lib.paths import vct_root_dir

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return vct_root_dir() / "logs" / f"kg-sync-{ts}.log"
    except Exception:  # noqa: BLE001 — diagnostics must never break the run
        return None


def _print_run_details(*tallies: "SyncTally", run_kind: str) -> None:
    """End-of-run honesty block (v0.2.92 WP-B1 / D12).

    Names every path that did NOT end in a real sync — failures with their
    reason, and every skip category with its reason — instead of letting
    them hide inside a "succeeded" count. Bounded to
    ``_DETAILS_PRINT_CAP`` stdout lines; the complete list is written to
    the run log under ``<vct_root>/logs/`` (soft-fail: if the log can't be
    written, the bounded stdout block still prints).

    ``tallies`` may be empty (nothing to report → no output at all).
    """
    records: List[SyncOutcome] = [r for t in tallies for r in t.records]
    if not records:
        return

    counts: Dict[str, int] = {}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
    breakdown = ", ".join(
        f"{counts[c]} {c}" for c in (
            OUTCOME_FAILED, OUTCOME_EMBED_SKIPPED, OUTCOME_ARCHIVED_SKIPPED,
            OUTCOME_FRONTMATTER_SKIPPED, OUTCOME_EXCLUDED_SKIPPED,
        ) if counts.get(c)
    )
    print(f"📋 {len(records)} not-synced item(s) this run ({run_kind}): {breakdown}")

    log_path = None
    try:
        log_path = _details_log_path()
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            header = [
                f"kg-sync run {datetime.now(timezone.utc).isoformat()} "
                f"(project root: {PROJECT_ROOT})",
                f"not-synced records: {len(records)} ({breakdown})",
                "",
            ]
            body = [
                f"[{r.status}] {r.path} — {r.reason or '(no reason recorded)'}"
                for r in records
            ]
            log_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — soft-fail, see docstring
        log_path = None
        print(f"   (could not write the full-list log: {e})", file=sys.stderr)

    glyph = {OUTCOME_FAILED: "✗"}.get
    for r in records[:_DETAILS_PRINT_CAP]:
        print(f"   {glyph(r.status, '⊘')} {r.path} — {r.reason or '(no reason recorded)'}")
    if len(records) > _DETAILS_PRINT_CAP:
        print(f"   … and {len(records) - _DETAILS_PRINT_CAP} more "
              f"(full list in the run log)")
    if log_path is not None:
        print(f"   full list: {log_path}")


def _regen_node_formats_after_full_sync() -> None:
    """KG-4 (v0.2.75): after a `--all` sync, refresh the
    `knowledge/.node_formats.json` sidecar so retrieval summaries don't stay
    stale until the next install/per-edit fire.

    The sidecar (descriptions + summaries surfaced by auto-tier retrieval) is
    written by `generate_node_formats.py` at install time and by the
    per-edit `generate-kg-summary.py` hook — but a bare `kg-sync --all` never
    touched it (grep 0), so a bulk resync left every node's summary stale.
    This calls the SAME install-time machinery as a capped, soft-fail
    post-sync step: any error (no generator, no summary backend, timeout) is
    swallowed with a log line — a summary refresh must never fail the sync.
    """
    import subprocess

    # v0.2.94 — ROOT-vs-NON-ROOT PARITY (field report 2026-09-09).
    #
    # This used to look for the generator under the SYNCED PROJECT's own
    # `claude_mcp_servers/scripts/` — a directory that exists ONLY in the
    # orchestrator clone. Every user project therefore fell through to
    # `.claude/scripts/generate-kg-summary.py --all`, which is the PER-EDIT
    # generator: its argparse takes a FILE, so `--all` is a usage error and it
    # exits 2. Observed live after a user project's `kg-sync --all`:
    # `(node-format refresh exited 2; summaries left as-is — non-fatal)`. Net
    # effect: after ANY bulk sync, no non-root project has EVER had its
    # `.node_formats.json` refreshed, and the only trace was one stderr line.
    #
    # The fix is the user's standing rule — ONE component for root and non-root.
    # `generate_node_formats.py` already accepts `--knowledge-dir`, and sets
    # `PROJECT_ROOT = KNOWLEDGE_DIR.parent` from it, so pointing the ROOT
    # generator at this project's `knowledge/` is the identical invocation in
    # both cases (for the root, install_root == PROJECT_ROOT and nothing
    # changes). `sys.executable` is right here: the kg-sync wrapper already
    # activated the orchestrator venv, which is why `vco_lib` imports below.
    gen = None
    install_root = None
    try:
        from vco_lib.python_exe import resolve_install_root

        install_root = resolve_install_root()
    except Exception as exc:  # noqa: BLE001 — a summary refresh never breaks a sync
        print(f"   (node-format refresh: install root unresolved: {exc})",
              file=sys.stderr)
    if install_root is not None:
        candidate = (
            Path(install_root) / "claude_mcp_servers" / "scripts"
            / "generate_node_formats.py"
        )
        if candidate.is_file():
            gen = candidate
    if gen is None:
        # No orchestrator clone reachable from here. NOT a silent skip any more:
        # the summaries genuinely stay stale, and the old fallback (the per-edit
        # generator with `--all`) never worked. Record it as owed work.
        _emit_node_formats_deferral(
            PROJECT_ROOT,
            reason=(
                "the orchestrator clone's "
                "`claude_mcp_servers/scripts/generate_node_formats.py` could "
                "not be located from this project "
                f"(install root: {install_root or 'unresolved'})"
            ),
        )
        return
    if not KNOWLEDGE_ROOT.is_dir():
        return  # nothing to summarise; not a failure
    try:
        # Cap the whole regen so a slow/hung summary backend can't wedge the
        # sync exit. --all over a large KG can be slow but is bounded here.
        py = sys.executable or "python3"
        # v0.2.92 WP-B1: this exact line is the STAGE MARKER the launcher's
        # kg_sync.rs maps to phase "finalize" (the regen below can legitimately
        # run for up to 600 s AFTER the 📊 summary lines — without this signal
        # the GUI showed a stalled "embedding (N/N)" while the process was
        # honestly still working). Text is load-bearing: change it only with
        # the Rust matcher in kg_sync.rs. flush=True so the marker reaches the
        # launcher's line reader BEFORE the (potentially long) regen starts,
        # including on direct-CLI runs without PYTHONUNBUFFERED.
        print("📝 Refreshing .node_formats.json summaries (KG-4, soft-fail) ...",
              flush=True)
        # NO `--force`: the generator skips nodes whose formats already exist,
        # so a re-run over an already-summarised project regenerates nothing.
        # (Standing rule: never re-embed / re-generate hash-unchanged content.)
        proc = subprocess.run(
            [py, str(gen), "--all", "--knowledge-dir", str(KNOWLEDGE_ROOT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1]
            print(
                "   (node-format refresh exited "
                f"{proc.returncode}; summaries left as-is — non-fatal)",
                file=sys.stderr,
            )
            _emit_node_formats_deferral(
                PROJECT_ROOT,
                reason=(
                    f"`{gen.name} --all --knowledge-dir {KNOWLEDGE_ROOT}` exited "
                    f"{proc.returncode}: {tail[:300] or '<no output>'}"
                ),
            )
        else:
            # Paired resolution (decision-#12 shape): a refresh that exited 0 is
            # the proof the earlier failure no longer holds. Nothing else clears
            # this entry.
            _clear_node_formats_deferral(PROJECT_ROOT)
    except subprocess.TimeoutExpired:
        print("   (node-format refresh timed out; summaries left as-is — non-fatal)",
              file=sys.stderr)
        _emit_node_formats_deferral(
            PROJECT_ROOT,
            reason="the generator did not finish within its 600 s cap",
        )
    except Exception as e:  # noqa: BLE001 — soft-fail, never break the sync
        print(f"   (node-format refresh skipped: {e} — non-fatal)", file=sys.stderr)
        _emit_node_formats_deferral(PROJECT_ROOT, reason=f"{type(e).__name__}: {e}")


#: Condition id for a failed post-sync `.node_formats.json` refresh (v0.2.94).
#: Declared in `vco_lib/deferral_conditions.toml` as `auto_retryable`.
_NODE_FORMATS_CID = "kg_node_formats_refresh_failed"


def _clear_drift_deferral(project_root: Path) -> None:
    """Resolve ``kg_sync_drift_detected`` after a FULLY successful tree sync.

    v0.2.94. ``--check-drift`` is read-only against WEAVIATE but it does write
    the project ledger (that is how a finding reaches the user), and since the
    launcher's bundle-update gate started running it automatically, an
    ``action_required`` entry saying "run `kg-sync --all`" outlived the run that
    had already done exactly that.

    The clear is NARROW, like its two siblings: only an ``--all`` that finished
    with zero per-node failures may retire it, because only that run proves the
    missing/stale nodes the scan named actually landed.

    Soft-fail: a sync's exit code never depends on ledger bookkeeping.
    """
    try:
        from vco_lib.deferral_emit import resolve_conditions
        from vco_lib.kg_sync_drift import CID_DRIFT

        resolve_conditions(project_root, (CID_DRIFT,))
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral clear failed: {inner})", file=sys.stderr)


def _clear_node_formats_deferral(project_root: Path) -> None:
    """Resolve :data:`_NODE_FORMATS_CID` after a refresh that exited 0.

    The paired half of :func:`_emit_node_formats_deferral`. Soft-fail: the
    sync's exit code never depends on ledger bookkeeping.
    """
    try:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(project_root, (_NODE_FORMATS_CID,))
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral clear failed: {inner})", file=sys.stderr)


def _emit_node_formats_deferral(project_root: Path, reason: str) -> None:
    """Record a failed summary refresh as owed work instead of one stderr line.

    Pre-v0.2.94 this failure printed `(node-format refresh exited 2; ...)` and
    vanished — which is how a refresh that had NEVER worked on a non-root
    project survived unnoticed. Soft-fail: the bookkeeping never breaks a sync.
    """
    try:
        from vco_lib.deferral_emit import DeferralEntry, emit

        entry = DeferralEntry(
            condition_id=_NODE_FORMATS_CID,
            title="KG summaries (.node_formats.json) not refreshed after the sync",
            detected=(
                f"The post-sync `.node_formats.json` refresh did not complete: "
                f"{reason}. The nodes ARE in Weaviate; what is stale is the "
                f"description/summary sidecar `hybrid_search`'s `summary` and "
                f"`titles` tiers read, so retrieval will show older summaries "
                f"(or none) for nodes this sync changed."
            ),
            why_deferred=(
                "The summary refresh is a soft-fail rider on the sync: it must "
                "never fail a run that successfully embedded every node. The "
                "condition is auto-retryable — the next successful `--all` "
                "tree sync runs the refresh again and clears this entry. "
                "Nothing is lost meanwhile except summary freshness."
            ),
            command_to_apply=(
                "# Refresh the summary sidecar for this project "
                "(existing summaries are skipped):\n"
                "python <orchestrator-root>/claude_mcp_servers/scripts/"
                "generate_node_formats.py --all --knowledge-dir "
                f"{project_root / 'knowledge'}"
            ),
            severity="warning",
        )
        emit(project_root, entry)
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


#: Prefix of the ONE machine-readable line ``--check-drift`` emits (v0.2.94).
#: MUST MATCH ``kg_sync.rs::KG_DRIFT_SENTINEL`` — the launcher's bundle-update
#: gate parses this line to decide whether an "on-disk unchanged" project still
#: owes a sync. A PREFIXED JSON line rather than a ``--json`` mode because this
#: script prints progress/setup chatter on stdout before ``main()`` even runs,
#: so "stdout is JSON" would be a contract it cannot keep; and rather than the
#: human summary line because a caller that regexes prose pins the prose.
DRIFT_SENTINEL_PREFIX = "KG_DRIFT_JSON "


def _print_drift_sentinel(binding, report) -> None:
    """Emit the machine-readable drift verdict. Best-effort; never raises.

    Always printed — including on the ``unbound`` early exit, where ``report``
    is ``None`` — so a caller can distinguish "checked, nothing owed" from
    "never got a verdict". Silence must never read as "every node is present".
    """
    import json  # local import — this script imports json per-function

    try:
        payload = {
            "binding": getattr(binding, "status", "") or "",
            "kg_collection": getattr(binding, "kg_collection", "") or "",
            "status": getattr(report, "status", "") if report is not None else "unknown",
            "scanned": int(getattr(report, "scanned", 0) or 0) if report is not None else 0,
            "missing": len(getattr(report, "missing", ()) or ()) if report is not None else 0,
            "stale": len(getattr(report, "stale", ()) or ()) if report is not None else 0,
            "detail": (
                getattr(report, "detail", "") if report is not None
                else (getattr(binding, "detail", "") or "no KG binding")
            ),
        }
        print(DRIFT_SENTINEL_PREFIX + json.dumps(payload), flush=True)
    except Exception as exc:  # noqa: BLE001 — a report line never breaks a scan
        print(f"   (drift sentinel not emitted: {exc})", file=sys.stderr)


def _run_check_drift() -> None:
    """``--check-drift``: detect (never repair) knowledge/ content that
    never reached Weaviate — both "no KG collection binding at all" (a
    setup gap: this script's own `_resolve_collections()` silently falls
    back to the literal default `"KnowledgeGraph"` when nothing is
    configured, which `check_kg_binding` does NOT trust — it independently
    verifies a REAL binding was resolved) and "binding exists, some
    node(s) unsynced or stale" (drift; see `vco_lib.kg_sync_drift` module
    docstring for the double-sided finding this closes).

    Read-only end to end: no embedding calls, no Weaviate writes. Surfaces
    findings through the SAME deferral ledger every other soft-fail
    condition in this script uses (`kg_sync_no_embedding_backend`,
    `residue_cleanup_pending`) — see `vco_lib.kg_sync_drift.surface_drift` /
    `surface_binding_gap` for why the surfaced entries are explicitly
    `action_required` rather than `auto_retryable`. Always exits 0: every
    outcome here (unbound, drift, ok, could-not-determine) is a successful
    completion of a read-only scan, not a script failure — a non-zero exit
    would wrongly signal "this run failed" to a caller like install.py.

    NOTE this entry point is BUNDLE-DEPENDENT (it only exists where
    `.claude/scripts/sync_knowledge_graph.py` was installed) — it is NOT
    reachable for a "registered but unbundled" project, which is precisely
    the scenario `vco_lib.kg_sync_drift`'s own `python -m` CLI exists to
    cover instead (see that module's docstring).
    """
    from vco_lib.kg_sync_drift import check_kg_binding, scan_drift, surface_binding_gap, surface_drift

    # Pass the RAW env var (not COLLECTION_NAME, which _resolve_collections()
    # already defaulted to the literal "KnowledgeGraph" when nothing is
    # configured) — an operator-provided real value is honored as "bound"
    # without a hub/local-file round-trip; a genuinely absent env var still
    # falls through to check_kg_binding's own hub/local-config resolution.
    binding = check_kg_binding(
        PROJECT_ROOT, KNOWLEDGE_ROOT,
        kg_collection=os.environ.get("KG_COLLECTION", "").strip(),
    )
    b_icon = {"bound": "✅", "unbound": "⚠️ ", "ok": "✅"}.get(binding.status, "•")
    print(f"{b_icon} KG binding check: {binding.status} — {binding.detail}")
    surface_binding_gap(PROJECT_ROOT, binding)

    if binding.status == "unbound":
        print(
            "   → no drift scan possible without a binding. Register/"
            "bundle this project, then re-run --check-drift."
        )
        _print_drift_sentinel(binding, None)
        sys.exit(0)

    report = scan_drift(
        KNOWLEDGE_ROOT,
        weaviate_url=WEAVIATE_URL,
        kg_collection=binding.kg_collection or COLLECTION_NAME,
        shared_kg_collection=SHARED_COLLECTION_NAME,
    )
    icon = {"ok": "✅", "drift": "⚠️ ", "unknown": "❓"}.get(report.status, "•")
    print(f"{icon} KG sync drift check: {report.status} — {report.detail}")
    print(
        f"   scanned={report.scanned} archived_skipped={report.archived_skipped} "
        f"excluded_skipped={report.excluded_skipped} "
        f"shared_scope_skipped={report.shared_scope_skipped}"
    )
    if report.missing:
        print(f"   missing from Weaviate ({len(report.missing)}):")
        for p in report.missing:
            print(f"     - {p}")
    if report.stale:
        print(f"   stale in Weaviate ({len(report.stale)}):")
        for p in report.stale:
            print(f"     - {p}")
    if report.status == "drift":
        print(
            "   → run `.claude/scripts/kg-sync --all` to reconcile "
            "(content-hash gated: unaffected nodes are skipped, nothing "
            "is deleted)."
        )
    surface_drift(PROJECT_ROOT, report)
    _print_drift_sentinel(binding, report)
    sys.exit(0)


#: The complete flag vocabulary `main()` accepts after import-time
#: ``--project-root`` extraction. ONE home, HERE — the ``.sh``/``.ps1``
#: wrappers stay dumb forwarders so the vocabulary cannot drift between
#: three entry points (v0.2.92 WP-B1; field report: "unknown flags
#: degrade into sync targets").
_ACCEPTED_MODE_FLAGS = ("--all", "--all-docs", "--check-drift")
_ACCEPTED_HELP_FLAGS = ("-h", "--help")


def _print_usage(stream=None) -> None:
    """The usage block — printed for no-args (exit 2), `-h`/`--help`
    (exit 0), and every flag-validation failure (exit 2). Lists EXACTLY
    the accepted flags; a printed contract is shipped code."""
    if stream is None:
        stream = sys.stdout
    print("Usage: sync_knowledge_graph.py <file_path>", file=stream)
    print("       sync_knowledge_graph.py --all              (knowledge/ + docs/)", file=stream)
    print("       sync_knowledge_graph.py --all-docs         (docs/ only)", file=stream)
    print("       sync_knowledge_graph.py --check-drift      (detect unsynced nodes, never repairs)", file=stream)
    print("       sync_knowledge_graph.py <f1> <f2> ...      (explicit file list)", file=stream)
    print("       (any form accepts --project-root <path> to pin the target project)", file=stream)
    print("       (any form accepts --rechunk to force the chunk-plan comparison even", file=stream)
    print("        with no revision crossing pending — repairs stale chunk boundaries)", file=stream)
    print("Exit codes: 0 clean · 1 per-node/per-doc failures · 2 usage error or refused root", file=stream)


def _validate_argv_flags(argv: "List[str]") -> None:
    """Reject every argv token `main()` does not understand — exit 2.

    v0.2.92 WP-B1 (field report: unknown flags degrade into sync targets):
    pre-fix, ``--typo value`` fell into the file-list branch (both tokens
    became "sync targets", each reported "not under knowledge/ or docs/ —
    skipping", exit 0 with ``0 succeeded, 0 failed``) and a LONE
    ``--typo`` matched NO dispatch branch at all — the script connected
    to Weaviate, printed nothing after the banner, and exited 0. Both
    silent-success shapes are now hard usage errors BEFORE any backend
    connection: this runs after the import-time ``--project-root``
    extraction and before ``EmbeddingService.for_project``.

    Exit 2 = usage (distinct from 1 = per-node failures, and the same
    code the wrong-root tree check uses). ``-h``/``--help`` print usage
    and exit 0 from here.
    """
    # Mode flags take NO file arguments — pre-fix `--all extra.md` silently
    # ignored the extra token and synced the whole tree (same silent-drop
    # class as the unknown-flag bug above).
    mode = argv[1] if len(argv) > 1 else ""
    if mode in _ACCEPTED_MODE_FLAGS and len(argv) > 2:
        print(
            f"❌ {mode} takes no file arguments (got: {' '.join(argv[2:])})",
            file=sys.stderr,
        )
        _print_usage(sys.stderr)
        sys.exit(2)

    for tok in argv[1:]:
        if not tok.startswith("-"):
            continue  # positional sync targets are classified downstream
        if tok in _ACCEPTED_MODE_FLAGS:
            continue
        if tok in _ACCEPTED_HELP_FLAGS:
            _print_usage(sys.stdout)
            sys.exit(0)
        if tok == "--project-root" or tok.startswith("--project-root="):
            # Import-time extraction consumed the FIRST --project-root and
            # removed it; a leftover one means it appeared twice.
            print(
                "❌ --project-root may appear at most once (the first "
                "occurrence was already consumed)",
                file=sys.stderr,
            )
            _print_usage(sys.stderr)
            sys.exit(2)
        print(f"❌ Unrecognized option: {tok}", file=sys.stderr)
        _print_usage(sys.stderr)
        sys.exit(2)


def main():
    """Main entry point.

    Routes by path:
      - file under knowledge/  → sync_node (KG collection)
      - file under docs/       → sync_doc (development collection)
      - --all                  → sync_all_nodes + sync_all_docs
      - --all-docs             → sync_all_docs only (dev collection bootstrap)

    Every `-`-prefixed token must be in the accepted vocabulary (see
    `_validate_argv_flags`); unknown flags exit 2 BEFORE any backend
    connection. Exit codes: 0 clean (skips are fine) · 1 per-node
    failures · 2 usage error / refused root.
    """
    if len(sys.argv) < 2:
        _print_usage(sys.stderr)
        # v0.2.92 WP-B1: aligned with the exit contract (usage = 2, distinct
        # from 1 = per-node failures). Was exit 1.
        sys.exit(2)

    # v0.2.92 WP-B1: reject unknown flags BEFORE the banner and before any
    # backend construction — see _validate_argv_flags.
    _validate_argv_flags(sys.argv)

    # v0.2.89 BUG 3: loud, unconditional resolution banner — names WHICH
    # root won and via WHICH channel, so a misrouted run is diagnosable
    # from its output instead of "succeeding" against the wrong project.
    print(
        f"🧭 project root: {Path(os.path.abspath(str(PROJECT_ROOT)))} "
        f"(source: {_PROJECT_ROOT_SOURCE}) "
        f"→ KG={COLLECTION_NAME} DEV={DEV_COLLECTION_NAME or '(unset)'}",
        flush=True,
    )

    # v0.2.92: --check-drift is a READ-ONLY reconciliation (compares on-disk
    # knowledge/ content hashes against what Weaviate actually holds) — it
    # never embeds anything, so it's handled BEFORE the EmbeddingService
    # construction below and works even when the embedding backend is down
    # (Weaviate itself still needs to be reachable; scan_drift degrades to
    # status="unknown" — never a false "everything is missing" — when it
    # isn't). See vco_lib.kg_sync_drift for the full contract.
    if sys.argv[1] == "--check-drift":
        _run_check_drift()
        return

    # v0.2.89 BUG 3 validation leg: refuse to run a TREE sync against a
    # root that has neither knowledge/ nor a docs root — the exact shape of
    # the silent wrong-tree run this converts into a diagnosable failure.
    # Exit 2 (distinct from exit 1 = per-node sync failures).
    if sys.argv[1] in ("--all", "--all-docs"):
        if not KNOWLEDGE_ROOT.is_dir() and not DOCS_ROOT.is_dir():
            print(
                f"❌ Resolved project root '{PROJECT_ROOT}' "
                f"(source: {_PROJECT_ROOT_SOURCE}) contains neither "
                f"'knowledge/' nor a docs root ('{DOCS_ROOT}') — refusing to "
                f"run a tree sync against it. If this is the wrong project, "
                f"pass --project-root <path> (or fix the env channel named "
                f"above).",
                file=sys.stderr,
            )
            sys.exit(2)

    embedding_service = None
    try:
        # v0.2.18: construct EmbeddingService at script entry. Probes all
        # configured backends once; raises NoEmbeddingBackendError when
        # zero are reachable (auto-writes the embedding-failure jsonl under
        # <vct_root_dir()>/metrics + .claude/context/EMBEDDING_FAILURES.md for
        # Claude diagnostic). The path is RESOLVED, never restated — see
        # `embedding_fidelity.failures_jsonl_display_path`.
        try:
            embedding_service = EmbeddingService.for_project(PROJECT_ROOT)
        except NoEmbeddingBackendError as e:
            # Soft-fail at the install seed boundary (same pattern as the
            # KG-summary "no backend available" deferral). Emit a deferral
            # entry so install.py can surface it via UPDATE_DEFERRED.md and
            # exit 0 — KG sync simply won't happen this run.
            _emit_sync_deferral_no_backend(PROJECT_ROOT, e)
            print(f"⚠️  KG sync skipped: {e}", file=sys.stderr)
            # Resolved, not restated. Deliberately NOT this file's
            # `_embedding_failures_jsonl_hint()`: that helper APPENDS an outage
            # row, and the NoEmbeddingBackendError capture has already written
            # one — reusing it here would double-count the same outage.
            from vco_lib.embedding_fidelity import failures_jsonl_display_path

            print("   See .claude/context/EMBEDDING_FAILURES.md + "
                  f"{failures_jsonl_display_path()}",
                  file=sys.stderr)
            sys.exit(0)

        # Initialize Weaviate client + bind to the embedding service
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            embedding_service=embedding_service,
            grpc_port=GRPC_PORT
        )

        # Ensure both collections exist (dev one only if env var set)
        if not ensure_collection_exists(server):
            print("❌ Cannot proceed without KG collection")
            sys.exit(1)
        ensure_dev_collection_exists(server)  # graceful no-op if env unset

        print()

        # Sync files
        if sys.argv[1] == "--all":
            kg_tally = sync_all_nodes(server)
            doc_tally = sync_all_docs(server)
            total_fail = kg_tally.failed + doc_tally.failed
            # v0.2.89 BUG 6: surface how many nodes routed to the shared
            # collection via `scope: shared` frontmatter.
            _shared_note = (
                f" ({_SHARED_ROUTED_COUNT} → shared)"
                if _SHARED_ROUTED_COUNT else ""
            )
            # v0.2.92 WP-B1: skipped (archived / frontmarker / excluded /
            # embed-skip) is now part of the terminal line. The `📊 KG:` /
            # `📊 Docs:` prefixes and the "N succeeded, M failed" fragment
            # are load-bearing — kg_sync.rs::parse_summary_line parses them
            # (and accepts both this and the legacy two-count shape).
            print(f"📊 KG:   {kg_tally.summary_fragment()}{_shared_note}")
            print(f"📊 Docs: {doc_tally.summary_fragment()}")
            # v0.2.92 chunk-plan transition repair: name how many entries
            # re-embedded ONLY because their stored chunk plan predates the
            # current chunker revision — distinct from ordinary content
            # re-syncs, so the revision-crossing repair is visible as such
            # in the run report (and in the launcher's log_tail).
            if _RECHUNKED_COUNT:
                print(
                    f"♻️ Re-chunked {_RECHUNKED_COUNT} entrie(s) whose stored "
                    f"chunk plan predates the current chunker revision "
                    f"(boundaries rewritten; unchanged entries were skipped)"
                )
            # v0.2.92 WP-B1 / D12: name every non-synced path (failures AND
            # skips, with reasons) instead of burying them in the counts.
            _print_run_details(kg_tally, doc_tally, run_kind="--all")
            # KG-4 (v0.2.75): refresh the .node_formats.json summaries after a
            # full resync (soft-fail — never changes the sync exit code).
            # v0.2.92 WP-B1: this step prints the `📝 Refreshing …` STAGE
            # MARKER the launcher maps to phase "finalize" — the regen can
            # run up to 600 s AFTER these final counts, and without the
            # marker the GUI showed a stalled bar on an honest, live process.
            _regen_node_formats_after_full_sync()
            # v0.2.91 WP-B: the paired clear (decision #12 — NARROW home). A
            # tree sync that completed with zero failures is the proof the
            # entry's premise ("the seed was skipped") no longer holds. A
            # PARTIAL sync deliberately does not clear: the next clean run will.
            if total_fail == 0:
                _clear_sync_deferral_no_backend(PROJECT_ROOT)
                # v0.2.92 D17: a clean tree sync also retires the owed-work
                # entry a failing run left behind — same narrow-clear rule:
                # only a FULLY successful --all proves the failed nodes
                # from an earlier run actually landed.
                _clear_sync_failures_deferral(PROJECT_ROOT)
                # v0.2.94: and it retires the DRIFT entry `--check-drift` wrote.
                #
                # The launcher's bundle-update gate now runs `--check-drift`
                # automatically and spawns THIS run when it reports drift. The
                # scan surfaces `kg_sync_drift_detected` (action_required, "run
                # kg-sync --all") — and pre-fix nothing here cleared it, because
                # its only paired resolution was a LATER scan returning `ok`.
                # So every automatic repair left the project carrying an
                # action-required entry telling the user to do the thing that
                # had just been done for them.
                #
                # A `--all` that finished with ZERO failures AND actually
                # covered the knowledge tree IS the paired proof: it wrote every
                # node the scan found missing or stale. Same narrow-clear rule
                # as the two above — a partial run proves nothing.
                #
                # `kg_tally.total` is load-bearing, not belt-and-braces:
                # `total_fail` sums KG **and** docs failures, so a project with
                # a populated `docs/` and a missing or empty `knowledge/` would
                # otherwise reach zero failures having considered ZERO knowledge
                # nodes — and retire a drift entry about nodes it never looked
                # at. `not KNOWLEDGE_ROOT.exists()` is the honest exception: with
                # no tree at all there is nothing a drift entry could still be
                # true about, so a stale one is safe to retire.
                if kg_tally.total > 0 or not KNOWLEDGE_ROOT.exists():
                    _clear_drift_deferral(PROJECT_ROOT)
            else:
                # v0.2.92 D17: record the per-node failures as owed,
                # auto-retryable work — pre-fix, failed nodes were counted
                # (WP-B1) but NOTHING ever retried them.
                _emit_sync_failures_deferral(
                    PROJECT_ROOT, total_fail, run_kind="--all",
                    detail=(
                        f"{kg_tally.failed} knowledge node(s), "
                        f"{doc_tally.failed} doc(s)"
                    ),
                )
            sys.exit(0 if total_fail == 0 else 1)
        elif sys.argv[1] == "--all-docs":
            doc_tally = sync_all_docs(server)
            print(f"📊 Docs: {doc_tally.summary_fragment()}")
            _print_run_details(doc_tally, run_kind="--all-docs")
            # v0.2.92 D17: docs-only failures are owed work too. NO clear
            # on a clean docs-only run — it proves nothing about knowledge
            # nodes an earlier run failed on (narrow clear lives in --all).
            if doc_tally.failed:
                _emit_sync_failures_deferral(
                    PROJECT_ROOT, doc_tally.failed, run_kind="--all-docs",
                    detail=f"{doc_tally.failed} doc(s)",
                )
            sys.exit(0 if doc_tally.failed == 0 else 1)
        elif len(sys.argv) > 2 or (len(sys.argv) == 2 and not sys.argv[1].startswith("--")):
            # v0.2.42 CI-10: accept a list of file paths as positional args.
            # When multiple files are given, sync only those files rather than
            # the full tree — used by install.py's content-hash diff gate to
            # sync only the files that changed since the last install.
            # Single-file path (the original behaviour) also falls through here
            # when it has no `--` prefix.
            raw_args = sys.argv[1:]
            tally = SyncTally()
            for raw in raw_args:
                file_path, in_knowledge, in_docs = _classify_sync_target(raw)
                if in_knowledge:
                    outcome = sync_node(server, file_path)
                elif in_docs:
                    outcome = sync_doc(server, file_path)
                else:
                    # v0.2.89 BUG 3: name the resolved root + its source —
                    # the bare "not in knowledge/ or docs/" message was
                    # undiagnosable when the root itself was misrouted.
                    print(
                        f"ℹ️  {raw}: not under knowledge/ or docs/ of project "
                        f"root '{PROJECT_ROOT}' "
                        f"(source: {_PROJECT_ROOT_SOURCE}) — skipping"
                    )
                    outcome = SyncOutcome(
                        OUTCOME_EXCLUDED_SKIPPED,
                        to_posix_rel(raw),
                        "not under knowledge/ or docs/ of the resolved project root",
                    )
                tally.add(outcome)

            if len(raw_args) > 1:
                print(f"📊 List: {tally.summary_fragment()}")
            _print_run_details(tally, run_kind="file list")
            # v0.2.92 D17: explicit-file sync failures (the kg-sync-on-edit
            # hook path) are owed work too. NO clear on a clean list run —
            # it proves nothing about nodes an earlier run failed on
            # (narrow clear lives in --all).
            if tally.failed:
                _emit_sync_failures_deferral(
                    PROJECT_ROOT, tally.failed, run_kind="file list",
                    detail=f"{tally.failed} sync target(s)",
                )
            sys.exit(0 if tally.failed == 0 else 1)

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            server.close()
        except Exception:
            pass
        if embedding_service is not None:
            try:
                embedding_service.close()
            except Exception:
                pass


#: The one condition id this script owns. Named once so the emitter and the
#: paired clear can never drift apart (the v0.2.91 WP-B pairing).
_SYNC_NO_BACKEND_CID = "kg_sync_no_embedding_backend"

#: v0.2.92 D17: owed-work condition for per-node write FAILURES (distinct
#: from ``_SYNC_NO_BACKEND_CID``, which records the seed being SKIPPED
#: entirely). Registered in ``vco_lib/deferral_conditions.toml`` as
#: ``auto_retryable`` with ``retry_action = "retry:py:kg_seed"`` — a real
#: retry: the WP-H dispatcher re-runs this script's ``--all`` for the
#: project once the backend answers. Named once, same discipline.
_SYNC_FAILURES_CID = "kg_sync_failures_pending"


def _clear_sync_deferral_no_backend(install_root: Path) -> None:
    """Resolve ``kg_sync_no_embedding_backend`` after a SUCCESSFUL tree sync.

    v0.2.91 WP-B (decision #12 — the NARROW clear home). Before this, the
    condition had no clear anywhere: not in install.py's owned set, not in the
    bundle reconcile map, no ``resolve_conditions`` site. A user could fix their
    backend, run a full successful sync, and the "KG sync skipped" entry stayed
    in their ledger forever — while its sibling ``kg_summary_no_backend``
    self-healed on its own success path (``embedding_service._clear_failure_deferral``).
    This is that missing pair.

    NARROW on purpose: only the end of a tree sync that FULLY succeeded clears
    it. "A backend is reachable again" is NOT the same claim as "the seed
    actually ran" — the entry says the seed was skipped, so only a completed
    seed may retire it. (Re-running the seed automatically when the backend
    returns is v0.2.91 WP-H's retry class, a separate mechanism.)

    Soft-fail: the sync's exit code must never depend on ledger bookkeeping.
    """
    try:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(install_root, (_SYNC_NO_BACKEND_CID,))
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral clear failed: {inner})", file=sys.stderr)


def _emit_sync_deferral_no_backend(install_root: Path, exc: Exception) -> None:
    """Soft-fail deferral when no embedding backend is reachable at seed time.

    Adds an entry to ``<install_root>/.claude/context/UPDATE_DEFERRED.md``
    so install.py / the launcher can surface the issue. Idempotent (the
    emitter is last-write-wins per ``condition_id``). Soft-fail on any IO /
    import error — we're already in an error path.

    v0.2.91 WP-B: routed through the LOCKED emitter ``vco_lib.deferral_emit``.
    This was the LAST shipped writer still hand-rolling the raw
    ``DeferralReport.read/add_entry/write`` triplet that v0.2.83 WP-B1
    eliminated everywhere else — and the most dangerous place for it, because
    this script runs as a SUBPROCESS while install.py's own flow is live, so its
    unlocked read/write pair could interleave with ``finalize()``'s and drop
    entries. ``tests/test_deferral_registry_completeness_v0291.py`` now
    source-scans for that triplet so it cannot come back.
    """
    try:
        # Import locally because vco_lib is on sys.path now (added at top
        # of file), but the emitter isn't needed in the happy path — keep
        # it lazy.
        from vco_lib.deferral_emit import DeferralEntry, emit
        # Same laziness, same reason. The jsonl path is RESOLVED here rather
        # than restated: it moved out of ~/.claude in W7 and four shipped
        # scripts kept printing the old location.
        from vco_lib.embedding_fidelity import failures_jsonl_display_path

        entry = DeferralEntry(
            condition_id=_SYNC_NO_BACKEND_CID,
            title="KG sync skipped: no embedding backend reachable",
            detected=(
                "sync_knowledge_graph.py at install/seed time could not "
                "reach any configured embedding backend (Ollama / CodeEmbed / "
                f"OpenAI). Error: {exc}"
            ),
            why_deferred=(
                "Soft-fail policy: install must never block on transient "
                "service unavailability. Knowledge-graph search stays empty "
                "for this project until a sync run succeeds. This entry "
                "clears itself at the end of the next FULLY successful tree "
                "sync (`--all` with zero failures) — including the one the "
                "next `install.py --update` runs for you once a backend is "
                "reachable. Nothing else clears it: a backend simply being "
                "up again does not mean the seed ran. See "
                f"{failures_jsonl_display_path()} for the "
                "per-backend diagnostic written by EmbeddingService."
            ),
            command_to_apply=(
                "# Restart embedding services then re-run the seed:\n"
                "podman start vco_ollama vco_code_embed   # or: docker start ...\n"
                "python templates/scripts/sync_knowledge_graph.py --all"
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/embedding-service-v0218.md",
            ],
        )
        emit(install_root, entry)
    except Exception as inner:
        # Soft-fail — don't escalate. The failure JSONL written by
        # NoEmbeddingBackendError already captures the diagnostic.
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


def _clear_sync_failures_deferral(install_root: Path) -> None:
    """Resolve ``kg_sync_failures_pending`` after a FULLY successful tree sync.

    v0.2.92 D17 — the recovery half the field report was missing: ~210
    nodes existed as ``.md`` files with no Weaviate object and nothing ever
    retried them. The emit side (``_emit_sync_failures_deferral``) records
    the owed work as ``auto_retryable``; THIS narrow clear is its
    paired resolution, the same decision-#12 shape as
    ``_clear_sync_deferral_no_backend``: only the end of an ``--all`` run
    with ZERO failures proves the failed nodes from an earlier run actually
    landed, so only that run may retire the entry. A clean file-list or
    docs-only run proves nothing about those nodes and deliberately does
    not clear.

    Soft-fail: the sync's exit code must never depend on ledger bookkeeping.
    """
    try:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(install_root, (_SYNC_FAILURES_CID,))
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral clear failed: {inner})", file=sys.stderr)


def _emit_sync_failures_deferral(
    install_root: Path,
    failed: int,
    run_kind: str,
    detail: str = "",
) -> None:
    """Record per-node write FAILURES as owed, auto-retryable work (D17).

    Fired at the end of any run shape (``--all``, ``--all-docs``, file
    list) whose tally counted at least one ``OUTCOME_FAILED``. The entry
    names the failure count; its registry row
    (``kg_sync_failures_pending``) declares ``retry_action =
    "retry:py:kg_seed"`` so the WP-H retry dispatcher re-runs this
    script's ``--all`` for this project on its own — the retry this
    defect never had. Last-write-wins per condition_id: a later failing
    run refreshes the count rather than stacking entries.

    Soft-fail on any IO / import error — the run's own exit code and
    ``_print_run_details`` output already carry the failure facts.
    """
    try:
        from vco_lib.deferral_emit import DeferralEntry, emit

        breakdown = f" ({detail})" if detail else ""
        entry = DeferralEntry(
            condition_id=_SYNC_FAILURES_CID,
            title=(
                f"KG sync left {failed} node(s)/doc(s) unsynced — retry pending"
            ),
            detected=(
                f"A `{run_kind}` sync run finished with {failed} per-node "
                f"write failure(s){breakdown}. Those files exist on disk but "
                f"have no Weaviate object — they are invisible to KG "
                f"retrieval until a later sync succeeds. The failing paths "
                f"and reasons are named in this run's output and its "
                f"kg-sync log."
            ),
            why_deferred=(
                "Soft-fail policy: a tree sync never aborts on per-node "
                "errors — one failed write must not block the rest of the "
                "tree. This entry records the owed work so it is retried "
                "instead of forgotten: the condition is auto-retryable, and "
                "VCO's retry dispatcher re-runs "
                "`sync_knowledge_graph.py --all` for this project once the "
                "backend answers (content-hash gated — only what never "
                "landed is re-embedded). It clears at the end of the next "
                "FULLY successful `--all` tree sync; nothing else clears it."
            ),
            command_to_apply=(
                "# Re-run the tree sync (content-hash gated — unaffected nodes skip):\n"
                "python templates/scripts/sync_knowledge_graph.py --all"
            ),
            severity="warning",
        )
        emit(install_root, entry)
    except Exception as inner:  # noqa: BLE001 — bookkeeping is best-effort
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


if __name__ == "__main__":
    main()
