# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Diagram indexer — Phase 1.5.A of the Diagrams Integration.

Idempotent indexer for `.claude/diagrams/<category>/<name>.{mmd,excalidraw}`
files. On every save (wrapper-MCP path) and every manual edit (hook path)
this module:

  1. Reads the file and parses derived metadata (title, kind, content_text,
     node/edge counts for Mermaid; scene name + text labels + element-type
     counts for Excalidraw).
  2. Upserts a row in SQLite `project_diagrams` (UPSERT on
     `project_id` + `diagram_name`).
  3. Writes a sidecar `<file_path>.meta.json` atomically (tempfile + os.replace).
  4. Upserts a Weaviate object in the per-project `<Project>_Diagrams`
     collection.

Failure handling (see `index_diagram` docstring):
  - DB write failure: raises, no sidecar, no Weaviate.
  - Sidecar failure: DB row committed, raises (caller decides retry).
  - Weaviate failure: DB + sidecar committed, row enqueued to
    `diagram_index_retry` (best-effort), warning logged, row returned.

Cross-module dependencies:
  - `project_diagrams` + `diagram_index_retry` table schemas live in
    launcher migration 022 (created in Phase 1.1; retry-queue folded in
    from Phase 1.5.A). The module degrades gracefully when the DB is
    absent or the schema hasn't been applied — sidecar + Weaviate still
    happen, the SQLite UPSERT is skipped.
  - Path validation delegates to `vco_lib.diagram_paths.validate_scoped_path`
    (single source of truth for the scoped-path regex + error wording).

CLI usage (called by the post-file-edit hook):
    python -m vco_lib.diagram_indexer index <file_path>
        Indexes a single file (resolves project_id from CWD, chat_id
        from CLAUDE_CODE_SESSION_ID env). Prints the resulting row as
        JSON. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Recognised Mermaid diagram-kind keywords (Mermaid 11.x).
# Longest-prefix-wins ordering matters: "graph" must come AFTER specific
# kinds that start with it (none currently do) but BEFORE the generic
# fallback. Sort by length DESC to avoid premature short-prefix matches
# (e.g. `classDiagram` must beat `class` if any future spec adds that).
_MERMAID_KINDS = sorted(
    [
        "flowchart",
        "classDiagram",
        "sequenceDiagram",
        "stateDiagram-v2",
        "stateDiagram",
        "erDiagram",
        "gantt",
        "pie",
        "journey",
        "gitGraph",
        "mindmap",
        "timeline",
        "quadrantChart",
        "requirementDiagram",
        "c4Context",
        "C4Context",
        "graph",
    ],
    key=len,
    reverse=True,
)

# Mermaid frontmatter detection — anchored to the file start with a
# permissive whitespace prefix. We deliberately accept `title` as the
# canonical key (per Mermaid 10+ spec); the wider `{}` YAML-style block
# is not parsed beyond the title field (out of scope).
_MERMAID_FRONTMATTER_RE = re.compile(
    r"\A\s*---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)",
    re.DOTALL,
)
_MERMAID_TITLE_RE = re.compile(
    r"^\s*title\s*:\s*(?P<title>[^\n]+?)\s*$",
    re.MULTILINE,
)

# Mermaid edge patterns: order matters — longer arrows MUST be matched
# before their shorter prefixes (e.g. `-->` before `--`). We use a
# single OR-alternation regex that scans left-to-right and greedily.
_MERMAID_EDGE_PATTERNS = [
    r"<-->",
    r"<==>",
    r"-\.->",
    r"==>",
    r"-->",
    r"<--",
    r"<==",
    r"===",
    r"---",
    r"==",
    r"--",
]
_MERMAID_EDGE_RE = re.compile("|".join(_MERMAID_EDGE_PATTERNS))

# Mermaid node-ID heuristic: matches an identifier immediately preceding
# a `[...]`, `(...)`, `{...}`, `((...))`, `[[...]]`, etc. shape. Captures
# the bare identifier (alphanumeric + `_`). This is a rough count of
# UNIQUE node IDs — not a parse of Mermaid grammar. Documented as
# best-effort in the metadata table.
_MERMAID_NODE_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[\[\({]"
)

# Comment lines (Mermaid 10+): `%%` at line start (after optional
# whitespace). Used to skip past comments when detecting `diagram_kind`.
_MERMAID_COMMENT_RE = re.compile(r"^\s*%%")

# Filename humanizer: turn "auth-flow-v2" / "auth_flow_v2" / "AuthFlowV2"
# into "Auth Flow V2". Splits on `-`, `_`, and on lowercase-to-uppercase
# transitions; capitalises each token.
_HUMANIZE_SEP_RE = re.compile(r"[-_]+")
_HUMANIZE_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MermaidMetadata:
    """Parsed Mermaid diagram metadata."""

    title: Optional[str]
    diagram_kind: Optional[str]
    node_count: int
    edge_count: int
    content_text: str


@dataclass
class ExcalidrawMetadata:
    """Parsed Excalidraw scene metadata."""

    scene_name: Optional[str]
    text_labels: list[str] = field(default_factory=list)
    element_counts: dict[str, int] = field(default_factory=dict)
    content_text: str = ""


@dataclass
class DiagramRow:
    """Mirrors the DB row from Phase 1.1's project_diagrams table.

    `id` is None until the row is persisted. After UPSERT it carries the
    rowid (autoincrement on insert, lookup-by-unique on update).
    """

    project_id: str
    diagram_name: str
    diagram_type: str  # "mermaid" | "excalidraw"
    file_path: str
    category_path: str
    enabled: int
    inferred_title: Optional[str]
    diagram_kind: Optional[str]
    content_text: Optional[str]
    node_count: Optional[int]
    edge_count: Optional[int]
    chat_id: Optional[str]
    linked_session_summary: Optional[str]
    config_json: Optional[str]
    created_at: int
    updated_at: int
    id: Optional[int] = None

    def to_sidecar_dict(self) -> dict[str, Any]:
        """Project the row into a sidecar-friendly dict.

        Excludes DB rowid (sidecars travel with the file across machines
        where the rowid is meaningless). Includes a small schema version
        so future indexer changes can migrate older sidecars.
        """
        d = asdict(self)
        d.pop("id", None)
        d["_sidecar_schema_version"] = 1
        return d


# ---------------------------------------------------------------------------
# Filename / category helpers
# ---------------------------------------------------------------------------


def humanize_filename(stem: str) -> str:
    """Convert a filename stem into a human-readable title.

    Examples:
        humanize_filename("auth-flow-v2")    -> "Auth Flow V2"
        humanize_filename("auth_flow_v2")    -> "Auth Flow V2"
        humanize_filename("AuthFlowV2")      -> "Auth Flow V2"
        humanize_filename("login")           -> "Login"
        humanize_filename("")                -> ""

    Splits on `-`/`_` AND on camelCase boundaries, capitalises each
    surviving token, joins with single spaces. Token boundaries that
    produce empty strings (e.g. leading `-`) are collapsed.
    """
    if not stem:
        return ""
    # First, split on -/_ separators.
    parts = _HUMANIZE_SEP_RE.split(stem)
    # Then split each part again on camelCase boundaries.
    expanded: list[str] = []
    for p in parts:
        if not p:
            continue
        expanded.extend(_HUMANIZE_CAMEL_RE.split(p))
    # Capitalise and drop empties.
    return " ".join(t[:1].upper() + t[1:] for t in expanded if t)


def category_path_from_file(file_path: Path, diagrams_root: Path) -> str:
    """Extract the `<category>` portion of a diagram path.

    For `<diagrams_root>/gui/auth/login.mmd`, returns "gui/auth".
    For `<diagrams_root>/architecture/data-flow.mmd`, returns "architecture".
    For a file directly under `<diagrams_root>` (flat), returns "" — but
    callers should reject this via `validate_scoped_path` BEFORE reaching
    here. We don't enforce it twice; that's the path validator's job.

    Cross-OS: uses `Path.resolve()` and `relative_to`; the `/` separator
    is the canonical form returned (matching the SQLite `category_path`
    column convention defined in the plan).
    """
    rel = file_path.resolve().relative_to(diagrams_root.resolve())
    parts = rel.parts[:-1]  # drop the filename
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Mermaid parser
# ---------------------------------------------------------------------------


def _strip_mermaid_frontmatter(source: str) -> tuple[Optional[str], str]:
    """Return (title, body_without_frontmatter)."""
    m = _MERMAID_FRONTMATTER_RE.match(source)
    if not m:
        return None, source
    body_after = source[m.end():]
    title_m = _MERMAID_TITLE_RE.search(m.group("body"))
    title = title_m.group("title") if title_m else None
    return title, body_after


def _detect_mermaid_kind(body: str) -> Optional[str]:
    """First non-comment / non-blank line determines the diagram kind.

    Mermaid permits arbitrary leading comments and blank lines. We
    iterate lines, skip blanks and `%%...` comments, then match the
    longest-prefix-wins keyword. Returns the canonical keyword
    (case-preserved as it appears in the source) or None when no kind
    can be inferred (malformed Mermaid).
    """
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _MERMAID_COMMENT_RE.match(line):
            continue
        for kind in _MERMAID_KINDS:
            # Case-insensitive prefix match — Mermaid accepts both
            # `flowchart TD` and `Flowchart TD`; we normalise to the
            # spec form (`flowchart`, `classDiagram`, ...).
            if line.lower().startswith(kind.lower()):
                return kind
        # First non-comment line didn't match any kind — bail with None
        # (no point scanning further; downstream lines are content).
        return None
    return None


def parse_mermaid(source: str) -> MermaidMetadata:
    """Parse Mermaid source into derived metadata.

    All fields are best-effort: malformed Mermaid yields a metadata
    object with `diagram_kind=None`, `node_count=0`, `edge_count=0`,
    `content_text=source` (the raw file). The indexer never raises on
    parse failure — saving a broken Mermaid file should still produce a
    row (the user might be mid-edit).
    """
    title, body = _strip_mermaid_frontmatter(source)
    diagram_kind = _detect_mermaid_kind(body)

    # Edge count: scan body for arrow patterns. Subgraph fences and
    # comment lines are NOT excluded — overcounting on `%%` comment
    # lines that contain `-->` is acceptable for a "rough" metric.
    edge_count = len(_MERMAID_EDGE_RE.findall(body))

    # Node count: unique identifiers preceding a shape opener. Excludes
    # the diagram-kind keyword itself by matching on identifier + shape.
    node_ids = set(_MERMAID_NODE_RE.findall(body))
    node_count = len(node_ids)

    return MermaidMetadata(
        title=title,
        diagram_kind=diagram_kind,
        node_count=node_count,
        edge_count=edge_count,
        content_text=source,
    )


# ---------------------------------------------------------------------------
# Excalidraw parser
# ---------------------------------------------------------------------------


def parse_excalidraw(scene_json: dict) -> ExcalidrawMetadata:
    """Parse an Excalidraw scene-JSON dict into derived metadata.

    Excalidraw scene shape (excerpt):
        {
          "type": "excalidraw",
          "version": 2,
          "appState": {"name": "Auth flow sketch", ...},
          "elements": [
            {"type": "rectangle", ...},
            {"type": "text", "text": "Login", ...},
            ...
          ]
        }

    Best-effort: missing `appState` / `elements` keys yield empty
    defaults. Non-dict element entries are skipped silently.
    """
    if not isinstance(scene_json, dict):
        return ExcalidrawMetadata(scene_name=None)

    app_state = scene_json.get("appState") or {}
    scene_name: Optional[str] = None
    if isinstance(app_state, dict):
        raw_name = app_state.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            scene_name = raw_name.strip()

    elements = scene_json.get("elements") or []
    text_labels: list[str] = []
    element_counts: dict[str, int] = {}

    if isinstance(elements, list):
        for el in elements:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type")
            if isinstance(el_type, str) and el_type:
                element_counts[el_type] = element_counts.get(el_type, 0) + 1
            if el_type == "text":
                txt = el.get("text") or el.get("originalText")
                if isinstance(txt, str) and txt.strip():
                    text_labels.append(txt.strip())

    return ExcalidrawMetadata(
        scene_name=scene_name,
        text_labels=text_labels,
        element_counts=element_counts,
        content_text="\n".join(text_labels),
    )


# ---------------------------------------------------------------------------
# Path validation — delegates to vco_lib.diagram_paths (single source of truth)
# ---------------------------------------------------------------------------


def _validate_scoped_path(file_path: Path) -> tuple[str, str, str]:
    """Validate path via ``vco_lib.diagram_paths`` and return the parsed triple.

    The validation logic (regex, error wording, kind enforcement) lives in
    ``vco_lib.diagram_paths.validate_scoped_path``; this wrapper just
    extracts the ``(diagram_type, category_path, diagram_name)`` triple
    from the now-known-good path.

    Raises ``ValueError`` with the canonical corrective message on violation.
    """
    from vco_lib.diagram_paths import validate_scoped_path, extract_category_tags

    err = validate_scoped_path(str(file_path))
    if err is not None:
        raise ValueError(err)

    # Path is known-good; mechanical extraction.
    suffix = file_path.suffix.lower()
    diagram_type = "mermaid" if suffix == ".mmd" else "excalidraw"
    diagram_name = file_path.stem
    category_path = "/".join(extract_category_tags(str(file_path)))
    return diagram_type, category_path, diagram_name


# ---------------------------------------------------------------------------
# Atomic sidecar write
# ---------------------------------------------------------------------------


def _write_sidecar_atomic(sidecar_path: Path, payload: dict[str, Any]) -> None:
    """Write JSON payload to sidecar_path atomically.

    Uses tempfile + os.replace so a reader of the sidecar never observes
    a half-written file. Tempfile is created in the SAME directory as
    the target so os.replace is atomic (cross-device renames are not).
    """
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".meta.",
        suffix=".json.tmp",
        dir=str(sidecar_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, sidecar_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_sidecar(sidecar_path: Path) -> Optional[dict[str, Any]]:
    """Read a sidecar JSON file. Returns None when the sidecar doesn't exist
    or can't be parsed (corrupt sidecars are treated as absent so the
    indexer rewrites them rather than crashing on stale data).

    Exposed for the vco rebuild-diagram-index CLI's dry-run hash-compare
    path (Phase 1.5.C consumes this).
    """
    if not sidecar_path.exists():
        return None
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of raw bytes. Used for content-hash dedup across the
    indexer + rebuild CLI. Exposed (with leading underscore for the
    package-private contract) so Phase 1.5.C's dry-run uses the SAME
    hash function the real indexer uses — divergence would silently
    break idempotency."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def drop_diagram_by_path(
    file_path: Path,
    project_id: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
    weaviate_url: Optional[str] = None,
    diagrams_collection: Optional[str] = None,
    remove_sidecar: bool = True,
) -> dict[str, bool]:
    """Cascade-delete a diagram across all three persistence layers.

    Used by:
      - post-file-delete hook when the user / Claude removes a .mmd or
        .excalidraw file (real-time cleanup).
      - vco rebuild-diagram-index --prune for orphan cleanup (when a
        sidecar exists but its target file is gone).

    Returns a per-layer report so the caller can audit::

        {
          "sqlite_deleted": bool,
          "sidecar_deleted": bool,
          "weaviate_deleted": bool,
        }

    Idempotent: deleting an already-deleted diagram returns False for
    each layer that had nothing to remove. Best-effort across layers
    — if Weaviate is unreachable the SQLite + sidecar deletions still
    happen and the Weaviate failure is logged (caller checks the dict).

    Args:
      file_path: absolute or relative path to the diagram. Resolved to
        absolute before lookup so the file doesn't need to exist on disk.
      project_id: optional — narrows the SQLite DELETE to a specific
        project's rows. When None, deletes by file_path across all
        projects (the file_path column is functionally unique anyway).
      remove_sidecar: when True (default), unlink the `<file>.meta.json`
        sidecar too. Set False when the caller is iterating sidecars
        themselves (rebuild-CLI's orphan-prune walks sidecars directly).
    """
    result = {
        "sqlite_deleted": False,
        "sidecar_deleted": False,
        "weaviate_deleted": False,
    }
    abs_path = str(Path(file_path).resolve())

    # Layer 1: SQLite
    if db_path is None:
        from vco_lib.paths import launcher_db_path
        db_path = launcher_db_path()
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                if project_id:
                    cur.execute(
                        "DELETE FROM project_diagrams WHERE project_id = ? AND file_path = ?",
                        (project_id, abs_path),
                    )
                else:
                    cur.execute(
                        "DELETE FROM project_diagrams WHERE file_path = ?",
                        (abs_path,),
                    )
                result["sqlite_deleted"] = cur.rowcount > 0
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning(
                "SQLite delete failed for %s: %s",
                file_path, exc,
            )

    # Layer 2: sidecar file
    if remove_sidecar:
        sidecar_path = Path(file_path).with_suffix(
            Path(file_path).suffix + ".meta.json"
        )
        if sidecar_path.exists():
            try:
                sidecar_path.unlink()
                result["sidecar_deleted"] = True
            except OSError as exc:
                logger.warning(
                    "Sidecar delete failed for %s: %s",
                    sidecar_path, exc,
                )

    # Layer 3: Weaviate
    try:
        if _weaviate_delete_by_file_path(
            abs_path, weaviate_url=weaviate_url, collection_name=diagrams_collection,
        ):
            result["weaviate_deleted"] = True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "Weaviate delete failed for %s: %s",
            file_path, exc,
        )

    return result


def _weaviate_delete_by_file_path(
    file_path: str,
    *,
    weaviate_url: Optional[str] = None,
    collection_name: Optional[str] = None,
) -> bool:
    """Delete Weaviate objects in the diagrams collection matching file_path.

    Returns True if at least one object was deleted, False if skipped
    (no URL / no collection / client unavailable) or no match found.
    Raises on Weaviate errors so the caller logs + the audit report
    can flag the failure.
    """
    url = weaviate_url or os.environ.get("WEAVIATE_URL")
    if not url or not collection_name:
        logger.debug(
            "Weaviate delete skipped (url=%s, collection=%s)",
            url, collection_name,
        )
        return False

    # R4 defense-in-depth: refuse remote URLs.
    err = _validate_weaviate_url(url)
    if err is not None:
        logger.warning("Weaviate delete refused: %s", err)
        return False

    try:
        import weaviate  # type: ignore
        from weaviate.classes.query import Filter  # type: ignore
    except ImportError as exc:
        logger.warning(
            "weaviate-client not installed — skipping Weaviate delete: %s",
            exc,
        )
        return False

    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    http_port = parsed.port or 8081
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))

    client = weaviate.connect_to_custom(
        http_host=host,
        http_port=http_port,
        http_secure=parsed.scheme == "https",
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=parsed.scheme == "https",
    )
    deleted_any = False
    try:
        collection = client.collections.get(collection_name)
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("file_path").equal(file_path),
            limit=50,  # safety; should normally be 1
        )
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_any = True
    finally:
        client.close()
    return deleted_any


# ---------------------------------------------------------------------------
# SQLite UPSERT
# ---------------------------------------------------------------------------


_UPSERT_SQL = """
INSERT INTO project_diagrams (
    project_id, diagram_name, diagram_type, file_path, category_path,
    enabled, inferred_title, diagram_kind, content_text,
    node_count, edge_count, chat_id, linked_session_summary,
    config_json, created_at, updated_at
) VALUES (
    :project_id, :diagram_name, :diagram_type, :file_path, :category_path,
    :enabled, :inferred_title, :diagram_kind, :content_text,
    :node_count, :edge_count, :chat_id, :linked_session_summary,
    :config_json, :created_at, :updated_at
)
ON CONFLICT(project_id, diagram_name) DO UPDATE SET
    diagram_type = excluded.diagram_type,
    file_path = excluded.file_path,
    category_path = excluded.category_path,
    enabled = excluded.enabled,
    inferred_title = excluded.inferred_title,
    diagram_kind = excluded.diagram_kind,
    content_text = excluded.content_text,
    node_count = excluded.node_count,
    edge_count = excluded.edge_count,
    chat_id = COALESCE(excluded.chat_id, project_diagrams.chat_id),
    linked_session_summary = COALESCE(
        excluded.linked_session_summary, project_diagrams.linked_session_summary
    ),
    config_json = excluded.config_json,
    updated_at = excluded.updated_at
"""

_SELECT_BY_KEY_SQL = """
SELECT id, project_id, diagram_name, diagram_type, file_path, category_path,
       enabled, inferred_title, diagram_kind, content_text,
       node_count, edge_count, chat_id, linked_session_summary,
       config_json, created_at, updated_at
FROM project_diagrams
WHERE project_id = :project_id AND diagram_name = :diagram_name
"""


def _upsert_row(db_path: Path, row: DiagramRow) -> DiagramRow:
    """UPSERT a DiagramRow into project_diagrams and return the persisted row.

    Returns a NEW DiagramRow with `id` populated (and `created_at`
    preserved from the existing row on UPDATE — the UPSERT only updates
    `updated_at`).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Look up existing created_at so UPSERT-as-update preserves it.
        existing = conn.execute(
            _SELECT_BY_KEY_SQL,
            {"project_id": row.project_id, "diagram_name": row.diagram_name},
        ).fetchone()
        if existing is not None:
            # Preserve original created_at on update. Column index 15
            # in _SELECT_BY_KEY_SQL (0=id, ..., 14=config_json,
            # 15=created_at, 16=updated_at).
            row.created_at = int(existing[15])

        params = {
            "project_id": row.project_id,
            "diagram_name": row.diagram_name,
            "diagram_type": row.diagram_type,
            "file_path": row.file_path,
            "category_path": row.category_path,
            "enabled": row.enabled,
            "inferred_title": row.inferred_title,
            "diagram_kind": row.diagram_kind,
            "content_text": row.content_text,
            "node_count": row.node_count,
            "edge_count": row.edge_count,
            "chat_id": row.chat_id,
            "linked_session_summary": row.linked_session_summary,
            "config_json": row.config_json,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        conn.execute(_UPSERT_SQL, params)
        conn.commit()

        # Re-fetch to get the canonical rowid.
        persisted = conn.execute(
            _SELECT_BY_KEY_SQL,
            {"project_id": row.project_id, "diagram_name": row.diagram_name},
        ).fetchone()
    finally:
        conn.close()

    if persisted is None:
        raise RuntimeError(
            f"UPSERT succeeded but row not found on re-fetch: "
            f"project_id={row.project_id}, diagram_name={row.diagram_name}"
        )

    return DiagramRow(
        id=int(persisted[0]),
        project_id=str(persisted[1]),
        diagram_name=str(persisted[2]),
        diagram_type=str(persisted[3]),
        file_path=str(persisted[4]),
        category_path=str(persisted[5]),
        enabled=int(persisted[6]),
        inferred_title=persisted[7],
        diagram_kind=persisted[8],
        content_text=persisted[9],
        node_count=persisted[10],
        edge_count=persisted[11],
        chat_id=persisted[12],
        linked_session_summary=persisted[13],
        config_json=persisted[14],
        created_at=int(persisted[15]),
        updated_at=int(persisted[16]),
    )


# ---------------------------------------------------------------------------
# Retry-table enqueue (Weaviate failures)
# ---------------------------------------------------------------------------


_RETRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS diagram_index_retry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_diagram_retry_next
    ON diagram_index_retry(next_attempt_at);
"""


def _enqueue_retry(
    db_path: Path,
    project_id: str,
    file_path: str,
    error: str,
) -> None:
    """Append (or refresh) a row in diagram_index_retry.

    Best-effort: the table is created on demand if missing (so callers
    don't depend on migration 021's retry-table fragment being applied
    yet). Failure to enqueue is logged at WARNING and swallowed — the
    primary indexing path already succeeded for SQLite + sidecar; this
    retry is purely the Weaviate catch-up safety net.
    """
    now = int(time.time())
    # 60-second back-off for the first retry; let the cron / sweeper
    # apply exponential backoff via attempt_count when it reads the row.
    next_attempt_at = now + 60
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_RETRY_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO diagram_index_retry "
                "(project_id, file_path, error, attempt_count, "
                "next_attempt_at, last_error_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (project_id, file_path, error, next_attempt_at, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Failed to enqueue retry for %s: %s", file_path, exc)


# ---------------------------------------------------------------------------
# Weaviate upsert (optional — best-effort)
# ---------------------------------------------------------------------------


# Allowlist of Weaviate hosts. Defense-in-depth (R4 from code review):
# WEAVIATE_URL can flow from a user's project env, which is attacker-
# controllable if they pull a hostile .env in a CI variable injection
# scenario. Restrict the indexer's network destinations to local loopback
# / private addresses + the user-pinned WEAVIATE_URL host. Anything
# remote-routable gets refused with a clear log line.
_WEAVIATE_ALLOWED_HOSTNAMES = frozenset(("localhost", "127.0.0.1", "::1", "0.0.0.0"))
_WEAVIATE_ALLOWED_SCHEMES = frozenset(("http", "https"))


def _validate_weaviate_url(url: str) -> Optional[str]:
    """Return None if url is acceptable for indexer writes, else a reason
    string. Pinned to loopback by default; private-net 10/192.168/172.16
    accepted (Docker/Podman networks). Public IPs + non-http(s) schemes
    rejected. The MCP server's WEAVIATE_URL config can override the
    indexer's view but doesn't bypass this check — env-var injection is
    the exact threat model."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return f"unparseable Weaviate URL: {url!r}"
    if parsed.scheme not in _WEAVIATE_ALLOWED_SCHEMES:
        return f"refused Weaviate scheme {parsed.scheme!r}; expected http or https"
    host = (parsed.hostname or "").lower()
    if not host:
        return "Weaviate URL has no hostname"
    if host in _WEAVIATE_ALLOWED_HOSTNAMES:
        return None
    # Allow private-net IPv4 (Docker/Podman bridge networks).
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return None
    if host.startswith("172."):
        try:
            second = int(host.split(".", 2)[1])
            if 16 <= second <= 31:
                return None
        except (IndexError, ValueError):
            pass
    return (
        f"refused remote Weaviate URL {url!r}; indexer is restricted to "
        f"loopback / private-net hosts. Set WEAVIATE_URL=http://localhost:8081 "
        f"or a docker-bridge address."
    )


def _weaviate_upsert(
    row: DiagramRow,
    *,
    weaviate_url: Optional[str],
    collection_name: Optional[str],
) -> bool:
    """Upsert the row into `<Project>_Diagrams` Weaviate collection.

    Returns True if the upsert actually wrote, False if skipped (no URL,
    no collection name, weaviate-client not installed). Raises on
    Weaviate errors so the caller enqueues a retry.

    Raises on any Weaviate error so the caller can decide whether to
    enqueue a retry. Skipped silently when:
      - `weaviate_url` is None and no `WEAVIATE_URL` env is set, OR
      - `collection_name` is None (no per-project diagrams collection
        configured — common during early-stage installs).

    The actual embedding is computed server-side via Ollama (matching
    the KG / Development collections' pattern). We pass only the
    properties; the named-vector slot is populated by the MCP layer's
    embedding pipeline during a follow-on sync — same pattern as
    `<Project>_Development`.

    This is deliberately a thin shim: the heavyweight upsert path lives
    in `claude_mcp_servers/weaviate_mcp/server.py::store_knowledge_node`.
    For Phase 1.5.A we use a minimal direct insert (no chunking — diagram
    sources are small) so the indexer can run from the hook without
    pulling in the full MCP machinery.
    """
    url = weaviate_url or os.environ.get("WEAVIATE_URL")
    if not url or not collection_name:
        logger.debug(
            "Weaviate upsert skipped (url=%s, collection=%s)",
            url, collection_name,
        )
        return False

    # R4 defense-in-depth: refuse remote URLs.
    err = _validate_weaviate_url(url)
    if err is not None:
        logger.warning("Weaviate upsert refused: %s", err)
        return False

    try:
        import weaviate  # type: ignore
        from weaviate.classes.query import Filter  # type: ignore
    except ImportError as exc:
        logger.warning(
            "weaviate-client not installed — skipping Weaviate upsert: %s",
            exc,
        )
        return False

    # Parse host/port — match the pattern used by the MCP server.
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    http_port = parsed.port or 8081
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))

    client = weaviate.connect_to_custom(
        http_host=host,
        http_port=http_port,
        http_secure=parsed.scheme == "https",
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=parsed.scheme == "https",
    )
    try:
        collection = client.collections.get(collection_name)

        # Build properties. path_tags is derived from category_path at
        # write time — matches the schema in §1.5.3.
        path_tags = [t for t in row.category_path.split("/") if t]

        properties = {
            "title": row.inferred_title or row.diagram_name,
            "content": row.content_text or "",
            "path_tags": path_tags,
            "diagram_kind": row.diagram_kind or "",
            "chat_id": row.chat_id or "",
            "linked_session_summary": row.linked_session_summary or "",
            "file_path": row.file_path,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

        # Delete-then-insert (mirrors store_knowledge_node semantics).
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("file_path").equal(row.file_path),
            limit=10,
        )
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)

        collection.data.insert(properties=properties)
    finally:
        client.close()
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def index_diagram(
    file_path: Path,
    project_id: str,
    chat_id: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
    weaviate_url: Optional[str] = None,
    diagrams_collection: Optional[str] = None,
    enabled: int = 1,
) -> DiagramRow:
    """Idempotent diagram indexer.

    Steps:
      1. Validate scoped path (raises ValueError on bad layout).
      2. Read file content; parse derived metadata.
      3. Build DiagramRow and UPSERT into SQLite project_diagrams.
      4. Write sidecar `<file_path>.meta.json` atomically.
      5. Upsert Weaviate object in <Project>_Diagrams (best-effort).

    Failure modes (see module docstring):
      - DB write failure: raises, no sidecar, no Weaviate write.
      - Sidecar write failure: row committed, raises (caller retries).
      - Weaviate failure: row + sidecar committed, retry-table row
        enqueued, warning logged, RETURNS the row (no raise).

    Args:
        file_path: Absolute or relative path to the `.mmd` / `.excalidraw`
            file. Relative paths resolved against the CWD.
        project_id: Project UUID from the launcher's `projects` table.
            Required — no implicit resolution at the public entry point;
            see `index_diagram_async` for the convenience wrapper that
            resolves from CWD via vct-hub.
        chat_id: Optional Claude Code session UUID. None when the file
            is being indexed outside a Claude session (e.g. via
            `vco rebuild-diagram-index`).
        db_path: Path to the launcher's SQLite DB. Defaults to
            `${VCT_STATE_DIR:-$HOME/.vct}/launcher.db`.
        weaviate_url: Override for WEAVIATE_URL env var. None falls back
            to env, then skips Weaviate write if neither is set.
        diagrams_collection: Per-project Weaviate collection name (e.g.
            `MyProj_Diagrams`). Required for Weaviate upsert; if None,
            Weaviate write is skipped (DB + sidecar still happen).
        enabled: Initial enabled flag for new rows (existing rows
            preserve their enabled value via the UPSERT).

    Returns:
        The canonical DiagramRow (with persisted `id`).

    Raises:
        ValueError: scoped-path validation failed.
        FileNotFoundError: file_path doesn't exist on disk.
        sqlite3.Error: DB write failed.
        OSError: Sidecar write failed (DB row already committed).
    """
    file_path = file_path.resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Diagram file not found: {file_path}")

    diagram_type, category_path, diagram_name = _validate_scoped_path(file_path)

    source_bytes = file_path.read_bytes()
    source = source_bytes.decode("utf-8", errors="replace")

    inferred_title: Optional[str]
    diagram_kind: Optional[str]
    content_text: Optional[str]
    node_count: Optional[int]
    edge_count: Optional[int]

    if diagram_type == "mermaid":
        md = parse_mermaid(source)
        inferred_title = md.title or humanize_filename(diagram_name)
        diagram_kind = md.diagram_kind
        content_text = md.content_text
        node_count = md.node_count
        edge_count = md.edge_count
    elif diagram_type == "excalidraw":
        try:
            scene = json.loads(source)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Malformed Excalidraw JSON at %s: %s — indexing with empty metadata",
                file_path, exc,
            )
            scene = {}
        ed = parse_excalidraw(scene)
        inferred_title = ed.scene_name or humanize_filename(diagram_name)
        diagram_kind = "excalidraw"
        content_text = ed.content_text
        # node_count = total element count; edge_count = arrows/lines.
        node_count = sum(ed.element_counts.values()) if ed.element_counts else 0
        edge_count = (
            ed.element_counts.get("arrow", 0)
            + ed.element_counts.get("line", 0)
        )
    else:
        # Defensive: _validate_scoped_path already rejects other types.
        raise ValueError(f"Unsupported diagram_type: {diagram_type}")

    now = int(time.time())

    row = DiagramRow(
        project_id=project_id,
        diagram_name=diagram_name,
        diagram_type=diagram_type,
        file_path=str(file_path),
        category_path=category_path,
        enabled=enabled,
        inferred_title=inferred_title,
        diagram_kind=diagram_kind,
        content_text=content_text,
        node_count=node_count,
        edge_count=edge_count,
        chat_id=chat_id,
        linked_session_summary=None,  # populated by Phase 2 follow-up
        config_json=None,
        created_at=now,
        updated_at=now,
    )

    # Step 3: SQLite UPSERT (skipped gracefully when no launcher DB available
    # OR the diagrams schema hasn't been applied — enables ad-hoc CLI use
    # like `vco rebuild-diagram-index` against a project folder without a
    # launcher-managed DB; sidecar + Weaviate still happen).
    if db_path is None:
        from vco_lib.paths import launcher_db_path
        db_path = launcher_db_path()
    db_available = False
    if db_path.exists():
        try:
            persisted = _upsert_row(db_path, row)
            db_available = True
        except sqlite3.OperationalError as exc:
            # Most common cause: project_diagrams table missing because
            # migration 022 hasn't run yet on this DB. Fall back to sidecar-
            # only mode rather than crashing — the CLI doesn't always run
            # inside a launcher-managed environment.
            if "no such table" in str(exc).lower():
                logger.info(
                    "Diagrams schema absent in %s — indexing sidecar only.",
                    db_path,
                )
                persisted = row
            else:
                raise
    else:
        logger.info(
            "No launcher DB at %s — indexing sidecar only (no SQLite UPSERT).",
            db_path,
        )
        persisted = row  # No id assigned; sidecar carries content_hash for dedup.

    # Step 4: Atomic sidecar write (idempotency-aware: skip if existing
    # sidecar's content_hash matches the file's current hash — preserves
    # mtime for the Phase 1.5.C rebuild-CLI's "no-op on rerun" contract).
    sidecar_path = file_path.with_suffix(file_path.suffix + ".meta.json")
    new_sidecar_payload = persisted.to_sidecar_dict()
    file_hash = _sha256_bytes(source_bytes)
    new_sidecar_payload.setdefault("content_hash", file_hash)
    existing_sidecar = _read_sidecar(sidecar_path)
    sidecar_skipped = (
        existing_sidecar is not None
        and existing_sidecar.get("content_hash") == file_hash
    )
    if not sidecar_skipped:
        _write_sidecar_atomic(sidecar_path, new_sidecar_payload)
    # Phase 1.5.C instrumentation: expose what actually happened so the
    # rebuild CLI can count indexed-vs-skipped accurately.
    persisted.wrote_sidecar = not sidecar_skipped  # type: ignore[attr-defined]

    # Step 5: Weaviate upsert (best-effort).
    persisted.wrote_weaviate = False  # type: ignore[attr-defined]
    try:
        wrote = _weaviate_upsert(
            persisted,
            weaviate_url=weaviate_url,
            collection_name=diagrams_collection,
        )
        persisted.wrote_weaviate = bool(wrote)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — best-effort + retry queue
        logger.warning(
            "Weaviate upsert failed for %s: %s — enqueued for retry",
            file_path, exc,
        )
        if db_available:
            _enqueue_retry(db_path, project_id, str(file_path), str(exc))

    return persisted


async def index_diagram_async(
    file_path: Path,
    project_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    *,
    diagrams_collection: Optional[str] = None,
    db_path: Optional[Path] = None,
    weaviate_url: Optional[str] = None,
) -> DiagramRow:
    """Async variant for wrapper-MCP post_tool_success callbacks (Phase 1.2).

    Resolves `project_id` from CWD via the launcher's hub when not
    provided. Resolves `chat_id` from CLAUDE_CODE_SESSION_ID env var
    when not provided. Both falls-back to a hub-less default to keep
    the hook path resilient when the launcher isn't running.

    `diagrams_collection` resolution order (when caller passes None):
      1. ``$DIAGRAMS_COLLECTION`` env var (set by config_projection on
         every install + the per-project .claude/settings.json env
         block + .claude/env — see Fix 2 in fix/a1-indexing-pipeline).
      2. Falls back to ``None`` → ``index_diagram`` skips the Weaviate
         upsert silently (DB + sidecar still happen). This preserves
         the pre-fix behaviour for projects that haven't been
         re-projected yet via ``vco_lib.config_projection apply``.

    The actual work runs synchronously inside an executor — sqlite3 is
    not async-aware and the indexer has no IO that benefits from
    cooperative scheduling at this scale.
    """
    import asyncio

    if chat_id is None:
        chat_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None

    if project_id is None:
        project_id = _resolve_project_id_from_cwd() or "unknown"

    # Resolve diagrams_collection from env when caller didn't pass it.
    # Empty string is treated as unset (defensive coerce — same shape as
    # claude_mcp_servers/weaviate_mcp/server.py's ``empty_means_unset``
    # handling for KG_COLLECTION since v0.2.27).
    if diagrams_collection is None:
        env_val = os.environ.get("DIAGRAMS_COLLECTION", "")
        diagrams_collection = env_val.strip() or None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: index_diagram(
            file_path,
            project_id,
            chat_id,
            db_path=db_path,
            weaviate_url=weaviate_url,
            diagrams_collection=diagrams_collection,
        ),
    )


def _resolve_project_id_from_cwd() -> Optional[str]:
    """Best-effort: ask vct-hub for the project_id of the current CWD.

    Returns None on any failure (hub down, project not registered, etc.)
    so callers can fall back to a sentinel value. Hub discovery mirrors
    `vct_secrets_resolve.sh`.
    """
    try:
        from vco_lib.project_config import resolve_project_id  # type: ignore
        return resolve_project_id(Path.cwd())
    except Exception:  # noqa: BLE001 — best-effort
        return None


# ---------------------------------------------------------------------------
# Auto-snapshot (Phase 1, item 9 — A6 wire-up)
# ---------------------------------------------------------------------------
#
# When the post-file-edit hook fires on a `.mmd` / `.excalidraw` change,
# we want to capture a version snapshot BEFORE the next edit lands. The
# Tauri command `create_diagram_snapshot` exists for this but is only
# reachable from JS — the hook runs in bash. Rather than couple the two
# (the launcher might not be running when the hook fires), we go direct
# to the same SQLite table (`diagram_snapshots`) that the Tauri command
# writes.
#
# Contract (mirrors `create_diagram_snapshot` in
# `launcher/src-tauri/src/commands/diagrams_cmd.rs`):
#   * trigger is fixed to `'auto_pre_edit_save'`.
#   * dedup is by `(diagram_id, content_hash)` — the table's UNIQUE
#     constraint makes a repeat-content insert a no-op (we catch the
#     IntegrityError and return False).
#   * the snapshot's `content` BLOB stores the raw file bytes. The
#     Rust side already documents the column as opaque (gzipped or
#     raw — writer's choice); we go raw for now to keep this CLI a
#     drop-in match for the Rust command's behaviour.
#   * dispatch is soft-fail per the hook contract: a launcher-DB
#     hiccup must NOT block the user's edit from completing.


_SNAPSHOT_LOOKUP_SQL = """
SELECT id FROM project_diagrams
WHERE project_id = :project_id AND file_path = :file_path
"""

_SNAPSHOT_LATEST_HASH_SQL = """
SELECT content_hash FROM diagram_snapshots
WHERE diagram_id = :diagram_id
ORDER BY created_at DESC, id DESC
LIMIT 1
"""

_SNAPSHOT_INSERT_SQL = """
INSERT INTO diagram_snapshots
    (diagram_id, content_hash, content, created_at, trigger, label)
VALUES
    (:diagram_id, :content_hash, :content, :created_at, :trigger, :label)
"""


def snapshot_diagram_file(
    file_path: Path,
    project_id: str,
    *,
    db_path: Optional[Path] = None,
    trigger: str = "auto_pre_edit_save",
    label: Optional[str] = None,
) -> Optional[int]:
    """Create a snapshot row for ``file_path`` in ``diagram_snapshots``.

    Mirrors the Tauri command `create_diagram_snapshot` for the hook
    path. Look-up by ``(project_id, file_path)`` against the resolved
    absolute path — the indexer's UPSERT writes the same canonical form
    so this match is exact.

    Returns:
        The new ``snapshot_id`` (int) on insert.
        ``None`` on any of: file missing, no matching diagram row,
        DB unavailable, dedup hit (latest snapshot hash matches), or
        any SQLite error. All failure modes are soft (the hook should
        never break the user's edit because the snapshot table is
        temporarily unhappy).

    Raises:
        Never — the hook path is best-effort by design. Callers that
        want hard failure should use ``create_diagram_snapshot`` via
        the Tauri command.
    """
    abs_path = file_path.resolve()
    if not abs_path.is_file():
        logger.debug(
            "snapshot_diagram_file: %s does not exist; skipping",
            abs_path,
        )
        return None

    if db_path is None:
        from vco_lib.paths import launcher_db_path
        db_path = launcher_db_path()
    if not db_path.exists():
        logger.debug(
            "snapshot_diagram_file: launcher DB %s missing; skipping",
            db_path,
        )
        return None

    try:
        content_bytes = abs_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "snapshot_diagram_file: read %s failed: %s; skipping",
            abs_path, exc,
        )
        return None

    content_hash = _sha256_bytes(content_bytes)
    now_ms = int(time.time() * 1000)  # match Rust's `Utc::now().timestamp_millis()`

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # 1. Resolve diagram_id by (project_id, file_path).
            row = conn.execute(
                _SNAPSHOT_LOOKUP_SQL,
                {"project_id": project_id, "file_path": str(abs_path)},
            ).fetchone()
            if row is None:
                logger.debug(
                    "snapshot_diagram_file: no project_diagrams row for "
                    "(project_id=%s, file_path=%s); skipping",
                    project_id, abs_path,
                )
                return None
            diagram_id = int(row[0])

            # 2. Dedup: if the most recent snapshot has the same hash,
            #    no insert needed. The table's UNIQUE(diagram_id,
            #    content_hash) constraint would also reject the insert,
            #    but checking up-front avoids the IntegrityError noise.
            latest = conn.execute(
                _SNAPSHOT_LATEST_HASH_SQL,
                {"diagram_id": diagram_id},
            ).fetchone()
            if latest is not None and latest[0] == content_hash:
                logger.debug(
                    "snapshot_diagram_file: dedup hit for diagram_id=%d "
                    "hash=%s; skipping",
                    diagram_id, content_hash[:12],
                )
                return None

            # 3. Insert. The UNIQUE constraint still races us in theory
            #    (another writer between SELECT and INSERT) — we catch
            #    that case and return None as a graceful dedup.
            try:
                cursor = conn.execute(
                    _SNAPSHOT_INSERT_SQL,
                    {
                        "diagram_id": diagram_id,
                        "content_hash": content_hash,
                        "content": content_bytes,
                        "created_at": now_ms,
                        "trigger": trigger,
                        "label": label,
                    },
                )
                conn.commit()
                lastrowid = cursor.lastrowid
                if lastrowid is None:  # pragma: no cover — sqlite always
                    # sets lastrowid after a successful INSERT; guard for
                    # the Optional in the dbapi typeshed signature.
                    raise RuntimeError(
                        "sqlite INSERT returned no lastrowid for "
                        f"diagram_id={diagram_id}"
                    )
                snapshot_id = int(lastrowid)
                logger.debug(
                    "snapshot_diagram_file: inserted snapshot_id=%d "
                    "diagram_id=%d trigger=%s bytes=%d",
                    snapshot_id, diagram_id, trigger, len(content_bytes),
                )
                return snapshot_id
            except sqlite3.IntegrityError as exc:
                # UNIQUE(diagram_id, content_hash) — racing dedup. Same
                # outcome as the manual check above: no-op.
                logger.debug(
                    "snapshot_diagram_file: UNIQUE conflict for diagram_id=%d "
                    "(probably racing dedup): %s",
                    diagram_id, exc,
                )
                return None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(
            "snapshot_diagram_file: SQLite error against %s: %s; skipping",
            db_path, exc,
        )
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.diagram_indexer",
        description="Index a single diagram file (Phase 1.5.A).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── index ─────────────────────────────────────────────────────────
    p_index = sub.add_parser("index", help="Index one file.")
    p_index.add_argument("file_path", type=Path)
    p_index.add_argument(
        "--project-id",
        help="Project UUID. Resolved from CWD via vct-hub if omitted.",
    )
    p_index.add_argument(
        "--chat-id",
        help="Claude Code session UUID. Falls back to "
             "CLAUDE_CODE_SESSION_ID env var.",
    )
    p_index.add_argument(
        "--db-path",
        type=Path,
        help="Override launcher SQLite DB path "
             "(default ${VCT_STATE_DIR:-$HOME/.vct}/launcher.db).",
    )
    p_index.add_argument(
        "--diagrams-collection",
        help="Weaviate collection name (e.g. MyProj_Diagrams). Skips "
             "Weaviate upsert if omitted.",
    )

    # ── drop ──────────────────────────────────────────────────────────
    # post-file-delete hook calls this to cascade SQLite + sidecar +
    # Weaviate cleanup after a `rm` / `unlink` / `mv`. Idempotent —
    # already-deleted is a successful outcome.
    p_drop = sub.add_parser(
        "drop",
        help="Cascade-delete a diagram across SQLite + sidecar + Weaviate.",
    )
    p_drop.add_argument("file_path", type=Path)
    p_drop.add_argument(
        "--project-id",
        help="Project UUID. Resolved from CWD via vct-hub if omitted.",
    )
    p_drop.add_argument(
        "--db-path", type=Path,
        help="Override launcher SQLite DB path.",
    )
    p_drop.add_argument(
        "--diagrams-collection",
        help="Weaviate collection name.",
    )

    # ── snapshot create ───────────────────────────────────────────────
    # A6 wire-up: hook calls `snapshot create <file_path>` after the
    # indexer to capture a versioned snapshot. Trigger is fixed to
    # `auto_pre_edit_save` (the canonical value for hook-driven
    # snapshots; mirrors the Rust constant `VALID_SNAPSHOT_TRIGGERS`).
    # Soft-fail throughout — `--quiet` is on by default for the hook
    # path; errors are logged but exit code stays 0 unless arg parsing
    # itself fails.
    p_snap = sub.add_parser(
        "snapshot",
        help="Snapshot operations (create one).",
    )
    snap_sub = p_snap.add_subparsers(dest="snap_cmd", required=True)
    p_snap_create = snap_sub.add_parser(
        "create",
        help="Create an `auto_pre_edit_save` snapshot for a diagram file. "
             "Dedupped by content hash; no-op if the most-recent snapshot "
             "for the diagram already has the same hash.",
    )
    p_snap_create.add_argument("file_path", type=Path)
    p_snap_create.add_argument(
        "--project-id",
        help="Project UUID. Resolved from CWD via vct-hub if omitted.",
    )
    p_snap_create.add_argument(
        "--db-path",
        type=Path,
        help="Override launcher SQLite DB path "
             "(default ${VCT_STATE_DIR:-$HOME/.vct}/launcher.db).",
    )
    p_snap_create.add_argument(
        "--label",
        default=None,
        help="Optional human label stored in `diagram_snapshots.label`.",
    )
    p_snap_create.add_argument(
        "--trigger",
        default="auto_pre_edit_save",
        choices=("manual", "auto_pre_edit_save", "auto_interval"),
        help="Trigger string written to `diagram_snapshots.trigger`. "
             "Mirrors the launcher's VALID_SNAPSHOT_TRIGGERS constant.",
    )
    p_snap_create.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout / non-error stderr output (for hook use).",
    )

    args = parser.parse_args(argv)

    if args.cmd == "index":
        return _cli_index(args)
    if args.cmd == "drop":
        project_id = args.project_id or _resolve_project_id_from_cwd()
        # project_id is optional for drop (file_path is functionally unique).
        result = drop_diagram_by_path(
            args.file_path,
            project_id=project_id,
            db_path=args.db_path,
            diagrams_collection=args.diagrams_collection,
        )
        print(json.dumps(result, indent=2))
        # Exit 0: idempotent — already-deleted is a successful outcome.
        return 0
    if args.cmd == "snapshot":
        if args.snap_cmd == "create":
            return _cli_snapshot_create(args)
        parser.error(f"unknown snapshot subcommand: {args.snap_cmd}")
    parser.error(f"unknown command: {args.cmd}")  # pragma: no cover


def _cli_index(args: argparse.Namespace) -> int:
    project_id = args.project_id or _resolve_project_id_from_cwd()
    if not project_id:
        print(
            "ERROR: could not resolve project_id (pass --project-id or "
            "register the project with the launcher).",
            file=sys.stderr,
        )
        return 2

    chat_id = args.chat_id or os.environ.get("CLAUDE_CODE_SESSION_ID")

    try:
        row = index_diagram(
            args.file_path,
            project_id,
            chat_id,
            db_path=args.db_path,
            diagrams_collection=args.diagrams_collection,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR: SQLite write failed: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"ERROR: Sidecar write failed: {exc}", file=sys.stderr)
        return 4

    print(json.dumps(asdict(row), indent=2))
    return 0


def _cli_snapshot_create(args: argparse.Namespace) -> int:
    """``snapshot create <file_path> [--project-id ...] [--label ...]``.

    Soft-fail on everything that isn't an arg-parser error so the hook
    can call us with `|| true` and never break the user's edit flow.
    Exit codes:
      0 — success OR dedup hit OR soft-failed for any reason.
      2 — bad project_id resolution (truly unrecoverable: no project →
          no diagram row → nothing to snapshot ever).
    """
    project_id = args.project_id or _resolve_project_id_from_cwd()
    if not project_id:
        if not args.quiet:
            print(
                "ERROR: could not resolve project_id (pass --project-id "
                "or register the project with the launcher).",
                file=sys.stderr,
            )
        return 2

    snapshot_id = snapshot_diagram_file(
        args.file_path,
        project_id,
        db_path=args.db_path,
        trigger=args.trigger,
        label=args.label,
    )

    if args.quiet:
        return 0

    if snapshot_id is None:
        print(json.dumps({"snapshot_id": None, "reason": "noop"}))
    else:
        print(json.dumps({"snapshot_id": snapshot_id}))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    raise SystemExit(_cli())
