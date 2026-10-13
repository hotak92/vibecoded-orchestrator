#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Knowledge Graph Maintenance Script

Ensures consistency between markdown files and Weaviate:
1. Finds orphaned Weaviate entries (no corresponding file)
2. Finds orphaned files (not in Weaviate)
3. Validates WikiLinks (no broken links)
4. Checks node timestamps: reports nodes whose stored ``updated``
   property disagrees with the file's frontmatter ``updated:`` (the
   field the sync writer stores into that property). A file that
   declares no frontmatter ``updated:`` is NEVER a timestamp finding:
   the pre-rework fallback compared file mtime against the stored
   ``updated_at``, but mtime churns on every git checkout / branch
   switch / copy (the exact comparison vco_lib.kg_metadata_repair
   deliberately excludes — "mtime churns, so comparing them would
   write on every run"), so that branch was noise, not signal, and is
   gone. With --fix the flagged rows are repaired IN PLACE
   (``collection.data.update(uuid, properties=...)``) — no deletion,
   no re-embed (property drift needs no re-embedding), and the source
   .md files are never rewritten.
5. Rebuilds missing cross-references: reports nodes whose WikiLinks in
   the file are missing from the stored ``links`` / ``typed_links``
   and, with --fix, repairs them the same in-place way (writing the
   file-derived lists, exactly what the sync writer would store).
   Broken links whose target node does not exist stay item 3's report
   — --fix never invents nodes.
   Items 4-5 key every comparison by the row's stored ``file_path``
   (each row is compared only with the file at that path — duplicate
   H1s across files can never be cross-flagged or collateral-damaged),
   delete NOTHING (a failed update leaves the row exactly as it was),
   and isolate failures per row (one failing update never aborts the
   rest).
6. Checks required frontmatter fields: reports nodes whose frontmatter
   is missing any of the required fields (the ONE definition lives in
   sync_knowledge_graph.REQUIRED_FRONTMATTER_FIELDS). REPORT-ONLY in
   BOTH modes — a missing field can only be fixed by editing the .md,
   and KG files are user data this script never rewrites, so --fix
   keeps reporting them (with the exact field names) and writes
   nothing.

Usage:
    python .claude/scripts/maintain_knowledge_graph.py --check    # Check only — writes nothing
    python .claude/scripts/maintain_knowledge_graph.py --fix      # Check and fix
    python .claude/scripts/maintain_knowledge_graph.py --rebuild  # Full rebuild

Destructive paths (--fix orphan deletion, --rebuild) prompt for
confirmation; pass --yes for non-interactive runs (CI, agents). On a
non-interactive shell without --yes the destructive step is SKIPPED
(exit 3) — never silently applied. The items 4-5 repair is an IN-PLACE
property update on the flagged rows (nothing is deleted, so a failed
update can never lose a node, and no re-embed happens) and runs in
--fix without a further prompt; it touches only the flagged nodes'
Weaviate properties, never the source .md files. Item 6 is report-only
even under --fix: the missing fields are named so the user can add
them to the .md.

DATA-SAFETY (v0.2.54 Track D / audit P0-2): when the resolved
KNOWLEDGE_COLLECTION is the SHARED KG collection (orchestrator-root
rebind, the v0.2.44 scenario), --fix / --rebuild are REFUSED: shared
nodes written by OTHER projects carry file_paths that resolve only in
their own project trees, so orphan-pruning/rebuilding from any single
project root would classify them all as orphans and delete them. Set
VCO_MAINTAIN_SHARED_KG_CONSENT=1 to override (accepting that loss).
"""

import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import re

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# v0.2.92 (R4/R21) — INSTALL-TIME BAKED ROOT. `vco_lib/rewire.py` substitutes
# the placeholder below when this file is installed into a project, so the
# installed script can reach its orchestrator clone with NOTHING in the
# environment. In the clone the placeholder stays literal,
# `Path("{{ORCHESTRATOR_ROOT}}")/"vco_lib"` is not a directory, and this block
# is inert — the validation IS the placeholder guard.
# It is used ONLY when NEITHER env pin ($VCT_ORCHESTRATOR_ROOT,
# $VCT_INSTALL_ROOT) names a real orchestrator root — a VALID pin always
# wins, a provably stale one is healed — and the ladder in
# `_resolve_mcp_servers_dir` below is unchanged.
_VCO_BAKED_ORCHESTRATOR_ROOT = "{{ORCHESTRATOR_ROOT}}"
_vco_env_pins = [os.environ.get(_k, "").strip()
                 for _k in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT")]
if (Path(_VCO_BAKED_ORCHESTRATOR_ROOT) / "vco_lib").is_dir() and not any(
    _p and (Path(_p) / "vco_lib").is_dir() for _p in _vco_env_pins
):
    os.environ["VCT_ORCHESTRATOR_ROOT"] = _VCO_BAKED_ORCHESTRATOR_ROOT

# Add paths.
#
# PR-2 portability (2026-05-06): claude_mcp_servers/ only lives in the
# orchestrator clone, never bundled to user projects. Resolution order:
#   1. $VCT_ORCHESTRATOR_ROOT/claude_mcp_servers (set by .claude/env)
#   2. $CLAUDE_PROJECT_ROOT/claude_mcp_servers   (legacy override)
#   3. <project>/claude_mcp_servers              (orchestrator clone fallback)
# Pure utility import (WeaviateMCPServer used directly, no service spawn).
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))


def _resolve_mcp_servers_dir() -> Path:
    """Return the Path to claude_mcp_servers/, or raise with a hint."""
    env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "claude_mcp_servers"
        if candidate.is_dir():
            return candidate
    candidate = PROJECT_ROOT / "claude_mcp_servers"
    if candidate.is_dir():
        return candidate
    raise RuntimeError(
        "claude_mcp_servers/ not found. Set VCT_ORCHESTRATOR_ROOT in your "
        "shell or .claude/env to point at the orchestrator clone."
    )


_MCP_DIR = _resolve_mcp_servers_dir()
sys.path.insert(0, str(_MCP_DIR / "weaviate_mcp"))
# v0.2.18: make vco_lib (EmbeddingService) importable.
_VCO_LIB_PARENT = _MCP_DIR.parent
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))
# Also expose templates/scripts/ so the local sync_knowledge_graph wrapper
# class is importable. PROJECT_ROOT/.claude/scripts is where these files
# live in a project install; the orchestrator clone has templates/scripts.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# VCO-REWIRE-END: orchestrator-root-resolution

# v0.2.18: pre-v0.2.18 imported `WeaviateMCPServer` from
# `claude_mcp_servers/weaviate_mcp/server.py` — that symbol never existed
# in server.py, so this script was broken at import-time on any path that
# actually exercised it. Switch to the WeaviateWrapper defined in
# `sync_knowledge_graph` (the only working WeaviateMCPServer-alike), and
# the central EmbeddingService that owns embed/slot decisions now.
from sync_knowledge_graph import WeaviateWrapper as WeaviateMCPServer  # noqa: E402 - must follow the sys.path bootstrap above that makes this module importable
from vco_lib.embedding_service import (  # noqa: E402 - must follow the sys.path bootstrap above that makes this module importable
    EmbeddingService,
    NoEmbeddingBackendError,
)
# v0.2.96: the Weaviate base-URL precedence (WEAVIATE_URL > WEAVIATE_PORT >
# the canonical port) has ONE home. This is a CALL, not a mirror: the
# `vco_lib.embedding_service` import directly above is unguarded and runs
# first, so vco_lib is importable wherever this script runs at all.
from vco_lib.weaviate_helpers import weaviate_url_default  # noqa: E402 - must follow the sys.path bootstrap above that makes this module importable

# Configuration
# v0.2.18: EMBEDDING_MODEL is no longer read here. This script delegates
# embed calls to sync_knowledge_graph.sync_node, which constructs its own
# EmbeddingService. Keeping WEAVIATE_URL / OLLAMA_URL / GRPC_PORT for the
# Weaviate client only.
WEAVIATE_URL = weaviate_url_default()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


# v0.2.21 Step 18 (caller migration): resolve KG collection via the
# launcher's vct-hub. Falls back to env (PR-7 / v0.2.11 behaviour) when
# the hub is unreachable. Pre-v0.2.11 this was hardcoded
# "ClaudeKnowledgeGraph", which made every project's maintain run query
# the legacy cross-project collection.
def _resolve_kg_collection() -> str:
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        cfg = resolve(PROJECT_ROOT)
        return cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph")
    except Exception:
        return os.getenv("KG_COLLECTION", "KnowledgeGraph")


KNOWLEDGE_COLLECTION = _resolve_kg_collection()
DOCUMENTS_COLLECTION = "DocumentChunks"

# v0.2.54 Track D (P0-2): shared-KG hazard detection. Canonical name +
# legacy aliases mirror vco_lib/project_init.py (_SHARED_KG_NAME,
# _LEGACY_SHARED_KG_NAME, _LEGACY_SHARED_KG_NAME_LOWERCASE_C). Compared
# case-insensitively because install.py's case-insensitive adoption can
# rebind to whichever casing the live class actually carries.
_SHARED_KG_NAMES = {
    os.getenv("SHARED_KG_COLLECTION", "").strip().lower(),
    "vibecodedorchestrator_knowledgegraph",
    "vibecodedtools_knowledgegraph",
} - {""}


def _is_shared_collection(name: str) -> bool:
    return name.strip().lower() in _SHARED_KG_NAMES


def _confirm_destructive(prompt: str, assume_yes: bool) -> bool:
    """Gate for destructive steps. Returns True iff the user consented.

    Interactive shell → input() prompt; non-interactive shell → True
    only with --yes. Never destructive-by-default.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"  ⚠️  {prompt}")
        print("  Non-interactive shell without --yes — SKIPPING the "
              "destructive step. Re-run with --yes to apply.")
        return False
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def get_all_knowledge_files() -> Dict[str, Path]:
    """
    Get all markdown files in knowledge/

    Returns dict: {title: file_path}
    """
    files = {}
    for md_file in KNOWLEDGE_ROOT.rglob("*.md"):
        content = md_file.read_text(encoding='utf-8')

        # Extract title
        for line in content.split('\n'):
            if line.startswith('# '):
                title = line[2:].strip()
                files[title] = md_file
                break

    return files


def _fetch_all_objects_paginated(collection, page_size: int = 1000):
    """Fetch every object in a Weaviate v4 collection via cursor pagination.

    v0.2.46 V46-D: fixes silent-truncation footgun. Previously
    `fetch_objects(limit=1000)` silently dropped nodes 1001+ when the
    collection grew, which broke orphan-detection and rebuild logic.
    """
    all_objects = []
    cursor = None
    while True:
        if cursor is not None:
            response = collection.query.fetch_objects(limit=page_size, after=cursor)
        else:
            response = collection.query.fetch_objects(limit=page_size)
        if not response.objects:
            break
        all_objects.extend(response.objects)
        if len(response.objects) < page_size:
            break
        cursor = response.objects[-1].uuid
    return all_objects


def get_all_weaviate_objects(
    server: WeaviateMCPServer,
) -> List[Tuple[str, str, str]]:
    """
    Get all objects from Weaviate as (uuid, title, file_path) triples.

    v0.2.46 V46-D: uses cursor pagination so collections > 1000 nodes
    are fully enumerated (orphan-detection previously missed nodes 1001+).

    v0.2.54 Track D (P0-2): returns UUIDs so orphan DELETION is keyed by
    the exact object identity. Pre-fix the delete step re-queried by
    TITLE equality (limit 10) and deleted every match — a live node that
    happened to share a title with an orphan was collateral damage.
    """
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        objects = _fetch_all_objects_paginated(collection)

        triples = []
        for obj in objects:
            title = obj.properties.get("title")
            file_path = obj.properties.get("file_path")
            if title and file_path:
                triples.append((str(obj.uuid), title, file_path))

        return triples

    except Exception as e:
        print(f"❌ Error fetching Weaviate nodes: {e}")
        return []


def get_all_weaviate_nodes(server: WeaviateMCPServer) -> Dict[str, str]:
    """Get all nodes from Weaviate as {title: file_path} (back-compat view
    over :func:`get_all_weaviate_objects`)."""
    return {
        title: file_path
        for _uuid, title, file_path in get_all_weaviate_objects(server)
    }


def get_all_weaviate_rows(
    server: WeaviateMCPServer,
) -> List[Tuple[str, str, str, Dict[str, Any]]]:
    """Get all KG rows as (uuid, title, file_path, properties) 4-tuples.

    v0.2.102 W6-Y (docstring items 4-5): the timestamp and
    cross-reference checks compare STORED properties (``updated`` /
    ``updated_at`` / ``links`` / ``typed_links``) against the source
    files, so they need the full property dicts, not just the identity
    triples :func:`get_all_weaviate_objects` returns. Kept as a second
    named fetch (same pagination) rather than widening the triple
    contract — the triple shape is a patch point the existing guard
    tests monkeypatch.
    """
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        objects = _fetch_all_objects_paginated(collection)

        rows = []
        for obj in objects:
            title = obj.properties.get("title")
            file_path = obj.properties.get("file_path")
            if title and file_path:
                rows.append(
                    (str(obj.uuid), title, file_path, dict(obj.properties or {}))
                )

        return rows

    except Exception as e:
        print(f"❌ Error fetching Weaviate node properties: {e}")
        return []


def _parse_file_node(file_path: Path) -> Optional[Dict[str, Any]]:
    """Parse one KG file with the ONE parser the sync writer uses
    (``sync_knowledge_graph.parse_markdown_node`` — no second parser).

    Returns the parsed node dict, or None (with a note) when the file
    cannot be parsed. Unparseable is unjudgeable: never a finding, and
    the node is left alone by --fix.
    """
    from sync_knowledge_graph import parse_markdown_node

    try:
        return parse_markdown_node(
            file_path.read_text(encoding="utf-8"), file_path
        )
    except Exception as e:  # noqa: BLE001 — any parse failure skips the node
        print(f"  ⚠️  Cannot parse {file_path.name} for the "
              f"timestamp/link checks: {e}")
        return None


def _parse_instant(value: Any) -> Optional[datetime]:
    """Parse a timestamp (stored property or parsed frontmatter value)
    into an aware UTC datetime, or None when unparseable."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check_stale_timestamps(
    rows: List[Tuple[str, str, str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Find rows whose stored ``updated`` property disagrees with the
    source file (docstring item 4).

    Y-1 (batch review R3): keyed by the row's STORED ``file_path`` —
    each row is parsed against, and compared with, the file at exactly
    that path. Title-keying (the H1 map) is gone: when two files share
    an H1, title-keying compared BOTH rows against one file's parse and
    the fix step then damaged the other file's row.

    The expected value derives EXACTLY as the sync writer derives it
    (:func:`_parse_file_node` → ``parse_markdown_node``): the
    frontmatter ``updated:`` compared against the stored ``updated``
    property, which is what the sync writes from that field, at second
    granularity. A file that declares no frontmatter ``updated:`` is
    never a finding (the docstring's mtime-fallback removal).

    Returns one finding dict per flagged row: ``uuid`` / ``title`` /
    ``file_path`` / ``detail`` (for the report) and ``properties``
    (``{"updated": …}`` — what --fix writes in place). Conservative on
    every uncertainty: a row whose file no longer exists (item 1's
    territory), a stored property the row does not declare, or a value
    that will not parse, is unjudgeable — never a finding.
    """
    parsed: Dict[str, Optional[Dict[str, Any]]] = {}
    findings: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for row_uuid, title, weaviate_path, props in rows:
        if row_uuid in seen:
            continue
        file_path = PROJECT_ROOT / weaviate_path
        if not file_path.exists():
            continue  # missing file = the orphan check's finding, not ours
        if str(file_path) not in parsed:
            parsed[str(file_path)] = _parse_file_node(file_path)
        node_data = parsed[str(file_path)]
        if not node_data or "updated" not in node_data:
            continue
        expected_dt = _parse_instant(node_data["updated"])
        if expected_dt is None or "updated" not in props:
            continue
        stored_dt = _parse_instant(props["updated"])
        if stored_dt is None:
            continue
        if expected_dt.replace(microsecond=0) != stored_dt.replace(microsecond=0):
            seen.add(row_uuid)
            findings.append({
                "uuid": row_uuid,
                "title": title,
                "file_path": weaviate_path,
                "detail": (f"stored updated {props['updated']} ≠ "
                           f"file {node_data['updated']}"),
                "properties": {"updated": node_data["updated"]},
            })

    return findings


def _stored_link_sets(
    props: Dict[str, Any],
) -> Tuple[Optional[Set[str]], Optional[Set[Tuple[str, str]]]]:
    """(untyped link targets, typed (relation, target) pairs) read from
    one stored row — either side None when that aspect is unjudgeable.

    A None-valued property is a POSITIVE fact ("unset"), the same
    reading :func:`vco_lib.kg_metadata_repair._normalised_stored` gives
    it; a property the row does not declare at all is unjudgeable.
    Legacy list-of-strings ``typed_links`` items (``"uses::Redis"``)
    are read too.
    """
    raw_links = props.get("links")
    if "links" not in props:
        untyped: Optional[Set[str]] = None
    elif raw_links is None:
        untyped = set()
    elif isinstance(raw_links, list) and all(
        isinstance(item, str) for item in raw_links
    ):
        untyped = set(raw_links)
    else:
        untyped = None

    raw_typed = props.get("typed_links")
    typed: Optional[Set[Tuple[str, str]]] = None
    if "typed_links" in props:
        if raw_typed is None:
            typed = set()
        elif isinstance(raw_typed, list):
            typed = set()
            for item in raw_typed:
                if (
                    isinstance(item, dict)
                    and item.get("relation_type")
                    and item.get("target_title")
                ):
                    typed.add(
                        (str(item["relation_type"]), str(item["target_title"]))
                    )
                elif isinstance(item, str) and "::" in item:
                    relation, _, target = item.partition("::")
                    typed.add((relation, target))
                else:
                    typed = None  # unjudgeable item shape
                    break
    return untyped, typed


def check_missing_links(
    rows: List[Tuple[str, str, str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Find rows whose WikiLinks in the file at the row's stored
    ``file_path`` are missing from the stored ``links`` /
    ``typed_links`` (docstring item 5). Keyed by file_path exactly as
    :func:`check_stale_timestamps` (Y-1) — never by title.

    The file side derives from the ONE sync parser, so what is compared
    against — and what --fix writes back in place — is exactly what a
    re-sync through the writer would store. Missing-only: stored extras
    are not a finding (this rebuilds references, it does not prune
    them).

    Returns one finding dict per flagged row (same shape as
    :func:`check_stale_timestamps`; ``properties`` carries the
    file-derived ``links`` / ``typed_links``).
    """
    parsed: Dict[str, Optional[Dict[str, Any]]] = {}
    findings: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for row_uuid, title, weaviate_path, props in rows:
        if row_uuid in seen:
            continue
        file_path = PROJECT_ROOT / weaviate_path
        if not file_path.exists():
            continue
        if str(file_path) not in parsed:
            parsed[str(file_path)] = _parse_file_node(file_path)
        node_data = parsed[str(file_path)]
        if not node_data:
            continue
        stored_untyped, stored_typed = _stored_link_sets(props)
        miss: List[str] = []
        if stored_untyped is not None:
            miss.extend(sorted(set(node_data["links"]) - stored_untyped))
        if stored_typed is not None:
            file_typed = {
                (item["relation_type"], item["target_title"])
                for item in node_data["typed_links"]
            }
            for relation, target in sorted(file_typed - stored_typed):
                miss.append(f"{relation}::{target}")
        if miss:
            seen.add(row_uuid)
            findings.append({
                "uuid": row_uuid,
                "title": title,
                "file_path": weaviate_path,
                "detail": ", ".join(miss),
                "properties": {
                    "links": list(node_data["links"]),
                    "typed_links": list(node_data["typed_links"]),
                },
            })

    return findings


def apply_property_updates(
    server: WeaviateMCPServer,
    findings: List[Dict[str, Any]],
) -> int:
    """Repair the flagged rows IN PLACE (docstring items 4-5, Y-2/Y-3).

    ``collection.data.update(uuid, properties=...)`` merges the named
    properties into the stored row: no deletion (a failed update leaves
    the row exactly as it was — a down embedding backend can never lose
    a node, because no re-embed happens at all), no re-embedding
    (property drift does not change the content the vectors encode).

    A row flagged by BOTH checks is updated once, with the merged
    property set. Failures are isolated per row (Y-3): one failing
    update prints an error and the loop continues with the rest.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for finding in findings:
        entry = merged.setdefault(
            finding["uuid"],
            {"title": finding["title"], "properties": {}},
        )
        entry["properties"].update(finding["properties"])

    updated = 0
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
    except Exception as e:  # noqa: BLE001 — cannot even resolve the collection
        print(f"  ❌ Error reaching collection for in-place updates: {e}")
        return 0

    for row_uuid, entry in merged.items():
        try:
            collection.data.update(
                uuid=row_uuid, properties=entry["properties"]
            )
            updated += 1
            print(f"  ✓ Updated in place: {entry['title']} ({row_uuid})")
        except Exception as e:  # noqa: BLE001 — one row never aborts the rest
            print(f"  ❌ Failed to update {entry['title']} ({row_uuid}): {e} "
                  f"— row left as-is")

    return updated


def check_missing_frontmatter_fields(
    knowledge_root: Optional[Path] = None,
) -> Dict[str, List[str]]:
    """Find KG files whose frontmatter is missing required fields
    (docstring item 6). REPORT-ONLY by design: the caller never feeds
    these files into any fix path — a missing field can only be fixed
    by editing the .md, and KG files are user data this script never
    rewrites.

    Iterates the FILES under ``knowledge/`` (never the title-keyed H1
    map — two files can share an H1, and each file is judged on its
    own). The ontology meta-files at the knowledge root
    (``VOCABULARY.md``, ``TAG_HIERARCHY.md``) are not KG nodes and are
    skipped. "Required" has ONE definition — ``sync_knowledge_graph
    .REQUIRED_FRONTMATTER_FIELDS`` — and each file is read with the
    ONE frontmatter parser (``parse_frontmatter``), so the nested
    ``metadata:`` dialect and ``name:``-for-``title:`` promotion count
    as declared fields, exactly as the sync writer reads them. A file
    with no frontmatter block at all is missing every required field.

    Returns {file path (POSIX, relative to the knowledge root's
    parent): [missing field names]} for the report.
    """
    from sync_knowledge_graph import (
        REQUIRED_FRONTMATTER_FIELDS,
        parse_frontmatter,
    )

    root = knowledge_root if knowledge_root is not None else KNOWLEDGE_ROOT
    meta_files = {"vocabulary.md", "tag_hierarchy.md"}
    missing: Dict[str, List[str]] = {}
    for md_file in sorted(root.rglob("*.md")):
        if md_file.parent == root and md_file.name.lower() in meta_files:
            continue
        try:
            frontmatter, _body = parse_frontmatter(
                md_file.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as e:
            print(f"  ⚠️  Cannot read {md_file.name} for the "
                  f"frontmatter check: {e}")
            continue
        declared = frontmatter or {}
        absent = [f for f in REQUIRED_FRONTMATTER_FIELDS if f not in declared]
        if absent:
            try:
                key = md_file.relative_to(root.parent).as_posix()
            except ValueError:
                key = str(md_file)
            missing[key] = absent

    return missing


def extract_wikilinks(content: str) -> Set[str]:
    """Extract all [[WikiLinks]] from content"""
    pattern = r'\[\[([^\]]+)\]\]'
    matches = re.finditer(pattern, content)
    return {match.group(1) for match in matches}


def check_orphaned_weaviate_entries(
    weaviate_objects: List[Tuple[str, str, str]],
    file_nodes: Dict[str, Path]
) -> List[Tuple[str, str, str]]:
    """Find Weaviate objects without corresponding files.

    Takes and returns (uuid, title, file_path) triples so the deletion
    step can target exact object identities (v0.2.54 Track D — see
    :func:`get_all_weaviate_objects`).
    """
    orphaned = []

    for uuid, title, weaviate_path in weaviate_objects:
        # Check if file exists
        file_path = PROJECT_ROOT / weaviate_path
        if not file_path.exists():
            orphaned.append((uuid, title, weaviate_path))
            continue

        # Check if title matches
        if title not in file_nodes:
            orphaned.append((uuid, title, weaviate_path))

    return orphaned


def check_orphaned_files(
    file_nodes: Dict[str, Path],
    weaviate_nodes: Dict[str, str]
) -> List[str]:
    """Find files not in Weaviate"""
    orphaned = []

    for title in file_nodes:
        if title not in weaviate_nodes:
            orphaned.append(title)

    return orphaned


def check_broken_links(file_nodes: Dict[str, Path]) -> Dict[str, List[str]]:
    """
    Find broken WikiLinks

    Returns dict: {source_title: [broken_link1, broken_link2, ...]}
    """
    broken_links = {}

    for title, file_path in file_nodes.items():
        content = file_path.read_text(encoding='utf-8')
        links = extract_wikilinks(content)

        # Check each link
        broken = []
        for link in links:
            if link not in file_nodes:
                broken.append(link)

        if broken:
            broken_links[title] = broken

    return broken_links


def delete_orphaned_weaviate_entries(
    server: WeaviateMCPServer,
    orphaned: List[Tuple[str, str, str]]
) -> int:
    """Delete orphaned entries from Weaviate.

    v0.2.54 Track D (P0-2): deletion is keyed by UUID — exactly the
    objects the orphan check flagged. Pre-fix this re-queried by TITLE
    equality (limit 10) and deleted every match, so a LIVE node sharing
    a title with an orphan (duplicate-titled nodes are common after
    renames) was silently destroyed alongside it.
    """
    deleted_count = 0

    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)

        for uuid, title, _file_path in orphaned:
            collection.data.delete_by_id(uuid)
            deleted_count += 1
            print(f"  🗑️  Deleted orphaned entry: {title} ({uuid})")

    except Exception as e:
        print(f"  ❌ Error deleting orphaned entries: {e}")

    return deleted_count


def sync_orphaned_files(
    orphaned_titles: List[str],
    file_nodes: Dict[str, Path],
    server: WeaviateMCPServer,
) -> int:
    """Sync orphaned files to Weaviate.

    v0.2.18: takes the WeaviateMCPServer (sync_knowledge_graph's
    WeaviateWrapper) from the caller instead of constructing its own —
    the wrapper requires an EmbeddingService at construction time, which
    main() already owns. Avoids a second-EmbeddingService-instance
    footgun (each instance probes backends afresh).
    """
    from sync_knowledge_graph import sync_node

    synced_count = 0

    try:
        for title in orphaned_titles:
            if title in file_nodes:
                file_path = file_nodes[title]
                if sync_node(server, file_path):
                    synced_count += 1
                    print(f"  ✓ Synced orphaned file: {title}")

    except Exception as e:
        print(f"  ❌ Error syncing orphaned files: {e}")

    return synced_count


def check_consistency(
    server: WeaviateMCPServer,
    fix: bool = False,
    assume_yes: bool = False,
) -> Dict[str, int]:
    """
    Check consistency between files and Weaviate

    Returns statistics dict
    """
    print("=" * 60)
    print("KNOWLEDGE GRAPH CONSISTENCY CHECK")
    print("=" * 60)
    print()

    stats = {
        "total_files": 0,
        "total_weaviate": 0,
        "orphaned_weaviate": 0,
        "orphaned_files": 0,
        "broken_links": 0,
        "stale_timestamps": 0,
        "missing_links": 0,
        "missing_frontmatter_fields": 0,
        "fixed": 0
    }

    # Get all nodes
    print("📚 Scanning files...")
    file_nodes = get_all_knowledge_files()
    stats["total_files"] = len(file_nodes)
    print(f"  Found {len(file_nodes)} files")

    print("\n🔍 Scanning Weaviate...")
    weaviate_objects = get_all_weaviate_objects(server)
    weaviate_nodes = {
        title: fp for _uuid, title, fp in weaviate_objects
    }
    stats["total_weaviate"] = len(weaviate_objects)
    print(f"  Found {len(weaviate_objects)} objects")

    # Check orphaned Weaviate entries
    print("\n🗑️  Checking for orphaned Weaviate entries...")
    orphaned_weaviate = check_orphaned_weaviate_entries(weaviate_objects, file_nodes)
    stats["orphaned_weaviate"] = len(orphaned_weaviate)

    if orphaned_weaviate:
        print(f"  ⚠️  Found {len(orphaned_weaviate)} orphaned Weaviate entries:")
        for _uuid, title, fp in orphaned_weaviate[:10]:
            print(f"    - {title} ({fp})")
        if len(orphaned_weaviate) > 10:
            print(f"    ... and {len(orphaned_weaviate) - 10} more")

        if fix:
            # v0.2.54 Track D (P0-2): destructive step is confirmation-
            # gated. The orphan list above shows EXACTLY what would be
            # deleted before the user consents.
            if _confirm_destructive(
                f"Delete these {len(orphaned_weaviate)} Weaviate object(s)?",
                assume_yes,
            ):
                print("\n  Fixing orphaned Weaviate entries...")
                deleted = delete_orphaned_weaviate_entries(server, orphaned_weaviate)
                stats["fixed"] += deleted
            else:
                print("  Skipped orphan deletion (no confirmation).")
    else:
        print("  ✓ No orphaned Weaviate entries")

    # Check orphaned files
    print("\n📄 Checking for orphaned files...")
    orphaned_files = check_orphaned_files(file_nodes, weaviate_nodes)
    stats["orphaned_files"] = len(orphaned_files)

    if orphaned_files:
        print(f"  ⚠️  Found {len(orphaned_files)} orphaned files:")
        for title in orphaned_files[:10]:
            print(f"    - {title}")
        if len(orphaned_files) > 10:
            print(f"    ... and {len(orphaned_files) - 10} more")

        if fix:
            print("\n  Fixing orphaned files...")
            synced = sync_orphaned_files(orphaned_files, file_nodes, server)
            stats["fixed"] += synced
    else:
        print("  ✓ No orphaned files")

    # Check broken links
    print("\n🔗 Checking for broken WikiLinks...")
    broken_links = check_broken_links(file_nodes)
    stats["broken_links"] = sum(len(links) for links in broken_links.values())

    if broken_links:
        print(f"  ⚠️  Found {stats['broken_links']} broken links in {len(broken_links)} files:")
        for title, links in list(broken_links.items())[:5]:
            print(f"    {title}:")
            for link in links[:3]:
                print(f"      - [[{link}]]")
            if len(links) > 3:
                print(f"      ... and {len(links) - 3} more")
        if len(broken_links) > 5:
            print(f"    ... and {len(broken_links) - 5} more files")
    else:
        print("  ✓ No broken links")

    # v0.2.102 W6-Y (docstring items 4-5): timestamp + cross-reference
    # checks. `rows` is fetched AFTER the orphan steps so it reflects
    # any nodes this run already synced (those are current by
    # construction). Both checks key by the row's stored file_path
    # (Y-1) and --fix repairs the flagged rows IN PLACE (Y-2/Y-3):
    # nothing is deleted, nothing re-embeds, and one failing update
    # never aborts the rest.
    print("\n🕒 Checking node timestamps...")
    rows = get_all_weaviate_rows(server)
    stale_timestamps = check_stale_timestamps(rows)
    stats["stale_timestamps"] = len(stale_timestamps)

    if stale_timestamps:
        print(f"  ⚠️  Found {len(stale_timestamps)} node(s) with a stale stored timestamp:")
        for finding in stale_timestamps[:10]:
            print(f"    - {finding['title']}: {finding['detail']}")
        if len(stale_timestamps) > 10:
            print(f"    ... and {len(stale_timestamps) - 10} more")
    else:
        print("  ✓ No stale timestamps")

    print("\n🔗 Checking stored cross-references...")
    missing_links = check_missing_links(rows)
    stats["missing_links"] = len(missing_links)

    if missing_links:
        print(f"  ⚠️  Found {len(missing_links)} node(s) with WikiLinks missing from Weaviate:")
        for finding in missing_links[:5]:
            print(f"    {finding['title']} ({finding['file_path']}):")
            for link in finding["detail"].split(", ")[:3]:
                print(f"      - [[{link}]]")
            if len(finding["detail"].split(", ")) > 3:
                print(f"      ... and {len(finding['detail'].split(', ')) - 3} more")
        if len(missing_links) > 5:
            print(f"    ... and {len(missing_links) - 5} more files")
    else:
        print("  ✓ No missing cross-references")

    # v0.2.102 W6-Y follow-up 2 (docstring item 6): required-frontmatter
    # check. REPORT-ONLY in BOTH modes — these files are deliberately
    # NOT added to the --fix update set below: a missing field can only
    # be fixed by editing the .md, and this script never rewrites the
    # user's KG files.
    print("\n📝 Checking required frontmatter fields...")
    missing_fields = check_missing_frontmatter_fields()
    stats["missing_frontmatter_fields"] = len(missing_fields)

    if missing_fields:
        print(f"  ⚠️  Found {len(missing_fields)} file(s) missing required "
              f"frontmatter fields (report-only, also under --fix — add "
              f"the fields to the .md yourself):")
        for file_key, absent in list(missing_fields.items())[:10]:
            print(f"    {file_key}: missing {', '.join(absent)}")
        if len(missing_fields) > 10:
            print(f"    ... and {len(missing_fields) - 10} more")
    else:
        print("  ✓ No missing required frontmatter fields")

    if fix:
        findings = stale_timestamps + missing_links
        if findings:
            print(f"\n  Updating {len(findings)} flagged row(s) in place "
                  "(no deletion, no re-embed)...")
            stats["fixed"] += apply_property_updates(server, findings)

    return stats


def rebuild_all(server: WeaviateMCPServer, assume_yes: bool = False):
    """Full rebuild: delete all Weaviate nodes and resync from files"""
    print("=" * 60)
    print("FULL REBUILD")
    print("=" * 60)
    print()

    # Delete all nodes
    # v0.2.46 V46-D: cursor-paginate so collections > 1000 nodes are
    # fully drained (previously rebuild left nodes 1001+ undeleted,
    # producing stale ghosts after resync).
    print("🗑️  Deleting all Weaviate nodes...")
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        objects = _fetch_all_objects_paginated(collection)

        # v0.2.54 Track D (P0-2): full rebuild used to delete EVERYTHING
        # with zero prompt. Now: show the blast radius, require consent.
        local_files = len(list(KNOWLEDGE_ROOT.rglob("*.md"))) if KNOWLEDGE_ROOT.is_dir() else 0
        if not _confirm_destructive(
            f"Delete ALL {len(objects)} object(s) in '{KNOWLEDGE_COLLECTION}' "
            f"and resync from {local_files} local .md file(s)?",
            assume_yes,
        ):
            print("  Rebuild aborted (no confirmation). Nothing was deleted.")
            sys.exit(3)

        deleted_count = 0
        for obj in objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

        print(f"  ✓ Deleted {deleted_count} nodes")

    except Exception as e:
        print(f"  ❌ Error deleting nodes: {e}")
        return

    # Resync all files
    print("\n📚 Resyncing all files...")
    file_nodes = get_all_knowledge_files()

    from sync_knowledge_graph import sync_node

    synced_count = 0
    for title, file_path in file_nodes.items():
        if sync_node(server, file_path):
            synced_count += 1
        print()

    print(f"\n✅ Rebuild complete: {synced_count}/{len(file_nodes)} files synced")


def main():
    """Main entry point"""
    argv = sys.argv[1:]
    assume_yes = "--yes" in argv
    argv = [a for a in argv if a != "--yes"]

    if len(argv) < 1:
        print("Usage: maintain_knowledge_graph.py --check")
        print("       maintain_knowledge_graph.py --fix     [--yes]")
        print("       maintain_knowledge_graph.py --rebuild [--yes]")
        print("Checks: orphaned Weaviate entries, orphaned files, broken")
        print("WikiLinks, stale node timestamps, WikiLinks missing from")
        print("stored cross-references, missing required frontmatter")
        print("fields (report-only in every mode). --check writes nothing;")
        print("--fix also repairs the rows flagged by the timestamp and")
        print("cross-reference checks IN PLACE (no deletion, no re-embed,")
        print("never rewriting the .md files).")
        sys.exit(1)

    mode = argv[0]

    if mode not in ["--check", "--fix", "--rebuild"]:
        print(f"❌ Invalid mode: {mode}")
        print("Use --check, --fix, or --rebuild (optionally --yes)")
        sys.exit(1)

    # v0.2.54 Track D (P0-2): shared-collection refusal for destructive
    # modes. When KNOWLEDGE_COLLECTION is the SHARED KG (orchestrator-
    # root rebind), nodes contributed by OTHER projects have file_paths
    # that resolve only inside their own project trees — from here they
    # ALL look orphaned, and --fix/--rebuild would delete every one of
    # them. install.py documents this exact hazard as the reason it
    # never auto-adopts foreign KGs; this script must not carry the
    # same footgun. --check stays available (read-only).
    if mode in ("--fix", "--rebuild") and _is_shared_collection(KNOWLEDGE_COLLECTION):
        if os.getenv("VCO_MAINTAIN_SHARED_KG_CONSENT", "") != "1":
            print(
                f"❌ REFUSED: '{KNOWLEDGE_COLLECTION}' is the SHARED KG "
                f"collection.\n"
                f"   {mode} would classify every node written by OTHER "
                f"projects as orphaned\n"
                f"   (their .md sources live in those projects' own "
                f"knowledge/ trees) and DELETE them.\n"
                f"   Use --check for a read-only report. To restore the "
                f"shared KG instead, run each\n"
                f"   contributing project's `.claude/scripts/kg-sync --all`.\n"
                f"   To override anyway (accepting that loss): set "
                f"VCO_MAINTAIN_SHARED_KG_CONSENT=1.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            "⚠️  VCO_MAINTAIN_SHARED_KG_CONSENT=1 — operating destructively "
            "on the SHARED KG collection as instructed.",
            file=sys.stderr,
        )

    embedding_service = None
    server = None
    try:
        # v0.2.18: construct EmbeddingService at script entry. Even
        # --check (read-only) needs it because the WeaviateWrapper now
        # takes it at construction time (the wrapper's `text_vector_slot`
        # property reads from it).
        try:
            embedding_service = EmbeddingService.for_project(PROJECT_ROOT)
        except NoEmbeddingBackendError as e:
            # --check is read-only and doesn't actually need to embed
            # anything, but the WeaviateWrapper requires an
            # EmbeddingService at construction. Without a backend, we
            # can still query Weaviate (no embed needed for the
            # consistency check itself) — but --fix and --rebuild WILL
            # need to embed via sync_node, so they have to abort.
            print(f"❌ No embedding backend reachable: {e}", file=sys.stderr)
            # The path is RESOLVED (v0.2.92): the literal that used to be here
            # named ~/.claude/metrics, which W7 turned into a read-only archive.
            from vco_lib.embedding_fidelity import failures_jsonl_display_path
            print("   See .claude/context/EMBEDDING_FAILURES.md + "
                  + failures_jsonl_display_path(), file=sys.stderr)
            sys.exit(1)

        # Initialize Weaviate client + bind to the embedding service
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            embedding_service=embedding_service,
            grpc_port=GRPC_PORT
        )

        print()

        if mode == "--rebuild":
            rebuild_all(server, assume_yes=assume_yes)
        else:
            fix = (mode == "--fix")
            stats = check_consistency(server, fix=fix, assume_yes=assume_yes)

            # Print summary
            print("\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"Total files: {stats['total_files']}")
            print(f"Total Weaviate nodes: {stats['total_weaviate']}")
            print(f"Orphaned Weaviate entries: {stats['orphaned_weaviate']}")
            print(f"Orphaned files: {stats['orphaned_files']}")
            print(f"Broken links: {stats['broken_links']}")
            print(f"Stale timestamps: {stats['stale_timestamps']}")
            print(f"Missing cross-references: {stats['missing_links']}")
            print("Missing required frontmatter fields: "
                  f"{stats['missing_frontmatter_fields']}")
            # v0.2.18: surface the active named-vector slot so operators
            # can sanity-check which model maintain is using for write
            # paths (--fix / --rebuild). Read-only check doesn't depend
            # on it, but logging it is cheap and useful.
            print(f"Active text slot: {embedding_service.text_vector_slot}")

            if fix:
                print(f"Fixed: {stats['fixed']}")
                print("\n✅ Maintenance complete")
                if stats['missing_frontmatter_fields'] > 0:
                    print("⚠️  Frontmatter findings are report-only — add "
                          "the missing fields to the .md files yourself.")
            else:
                if (
                    stats['orphaned_weaviate'] + stats['orphaned_files']
                    + stats['stale_timestamps'] + stats['missing_links'] > 0
                ):
                    print("\n⚠️  Run with --fix to repair issues")
                    if stats['missing_frontmatter_fields'] > 0:
                        print("⚠️  Frontmatter findings are report-only — "
                              "add the missing fields to the .md files "
                              "yourself.")
                elif stats['missing_frontmatter_fields'] > 0:
                    print("\n⚠️  Frontmatter findings only — report-only in "
                          "every mode; add the missing fields to the .md "
                          "files yourself.")
                else:
                    print("\n✅ All checks passed")

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        if embedding_service is not None:
            try:
                embedding_service.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
