# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco_lib.diagram_indexer`` — STUB (Phase 1.5.A territory).

This module is the agreed contract surface between Phase 1.5.A
(diagram parsing + Weaviate upsert; the real implementation) and
Phase 1.5.C (retrieval + ``vco rebuild-diagram-index`` CLI; this
agent's scope).

When Phase 1.5.A merges, this STUB file is REPLACED in-place by the
full implementation. Until then it exists so the CLI subcommand and
its tests have a working code path that can be exercised end-to-end.

Spec (from plan §1.5.4)::

    def index_diagram(
        file_path: Path, project_id: str, chat_id: str | None = None
    ) -> DiagramRow:
        '''Idempotent. Computes derived metadata, writes SQLite row,
        writes sidecar JSON, upserts Weaviate object. Returns the
        canonical DiagramRow.'''

    def parse_mermaid(source: str) -> MermaidMetadata: ...
    def parse_excalidraw(scene_json: dict) -> ExcalidrawMetadata: ...
    def humanize_filename(stem: str) -> str: ...

The STUB satisfies these signatures with the minimum behaviour the
CLI and tests rely on:

1. ``index_diagram`` writes a sidecar ``<name>.meta.json`` next to the
   diagram file. The sidecar is JSON with keys ``content_hash``
   (SHA-256 hex of the source bytes), ``project_id``, ``chat_id``,
   ``file_path`` (absolute), ``diagram_type`` (``mermaid``/
   ``excalidraw``), ``inferred_title``, ``category_path``,
   ``indexed_at`` (ISO-8601 UTC).

2. The sidecar is WRITE-IDEMPOTENT: if the existing sidecar's
   ``content_hash`` already matches the new content, the file is NOT
   re-written (preserves mtime, so ``vco rebuild-diagram-index`` can
   detect "zero writes" the way the plan's acceptance §1.5.8 requires).

3. ``parse_mermaid`` returns a minimal :class:`MermaidMetadata` with
   ``title`` parsed from ``---\\ntitle: ...\\n---`` frontmatter and
   ``diagram_kind`` set to the first non-comment token. Good enough
   for the CLI's "did anything change?" check.

4. ``parse_excalidraw`` returns a minimal :class:`ExcalidrawMetadata`
   parsed from the JSON: ``scene_name`` from ``appState.name`` and
   ``text_labels`` from ``elements[].text``. Element counts are
   computed too.

Weaviate writes: the STUB does NOT contact Weaviate. The real
Phase 1.5.A module will. The CLI rebuild flow tracks "Weaviate writes"
via a counter on the indexer's return value (``DiagramRow.wrote_weaviate``)
which the STUB always sets to ``False`` (since it doesn't actually
upsert). The Phase 1.5.A implementation overwrites this with the real
truth, and the CLI's idempotency assertion still works (zero re-writes
when content hash matches).

Cross-OS rules:
* All paths flow through :class:`pathlib.Path`.
* Sidecar JSON written with ``ensure_ascii=False`` + sorted keys for
  byte-stable output (matters for git + the idempotency test).
* UTF-8 encoding explicit everywhere; no platform-default fallback.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MermaidMetadata:
    """Subset of Mermaid metadata the CLI / sidecar cares about."""

    title: Optional[str]
    diagram_kind: Optional[str]
    node_count: int
    edge_count: int


@dataclasses.dataclass(frozen=True)
class ExcalidrawMetadata:
    """Subset of Excalidraw metadata the CLI / sidecar cares about."""

    scene_name: Optional[str]
    text_labels: list[str]
    element_counts: dict[str, int]


@dataclasses.dataclass(frozen=True)
class DiagramRow:
    """Canonical return shape for :func:`index_diagram`.

    Mirrors the SQLite row shape proposed by Phase 1.5.A so callers can
    handle either the STUB return or the real implementation
    interchangeably.
    """

    file_path: Path
    project_id: str
    chat_id: Optional[str]
    diagram_type: str           # ``mermaid`` | ``excalidraw``
    diagram_name: str           # filename stem
    category_path: str          # e.g. ``gui/auth``
    inferred_title: Optional[str]
    diagram_kind: Optional[str]
    content_hash: str           # sha256 hex of the source bytes
    sidecar_path: Path
    wrote_sidecar: bool         # True if the sidecar was (re)written
    wrote_weaviate: bool        # True if Weaviate was upserted (STUB: always False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_MERMAID_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL
)
_MERMAID_TITLE_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.MULTILINE)
# First non-comment, non-frontmatter token — flowchart/classDiagram/etc.
_MERMAID_KIND_RE = re.compile(
    r"^\s*(flowchart|graph|classDiagram|sequenceDiagram|stateDiagram|"
    r"erDiagram|gantt|journey|pie|gitGraph|mindmap|timeline|requirementDiagram|"
    r"C4Context|C4Container|C4Component|sankey-beta|xychart-beta|block-beta|"
    r"quadrantChart|packet-beta|kanban|architecture-beta)",
    re.MULTILINE,
)
# Edge regex — covers `-->`, `---`, `--text-->`, `==>` etc. Conservative
# (false-negatives over false-positives) since the count is metadata only.
_MERMAID_EDGE_RE = re.compile(r"--+>|==+>|---|===|-\.->")
# Crude node id regex — `A[Label]`, `A(Label)`, `A((Label))` etc. Counts
# distinct identifiers found before a bracket/paren shape opener.
_MERMAID_NODE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[\[\(\{]")


def humanize_filename(stem: str) -> str:
    """``auth-flow-v2`` → ``Auth Flow V2``.

    Empty input is preserved as empty string (so the caller can decide
    whether to fall back to anything else).
    """
    if not stem:
        return ""
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", stem) if part)


def parse_mermaid(source: str) -> MermaidMetadata:
    """Extract :class:`MermaidMetadata` from raw Mermaid source.

    Best-effort: a malformed file still returns a populated record
    (``title=None``, ``diagram_kind=None``, counts of ``0``). The CLI
    treats this as "indexed but no metadata" rather than failing.
    """
    if not isinstance(source, str):
        # Defensive: callers pass bytes-decoded strings, but a STUB
        # is the wrong place to crash on a type error.
        source = str(source or "")

    # 1. Frontmatter title
    title: Optional[str] = None
    frontmatter_match = _MERMAID_FRONTMATTER_RE.match(source)
    body_for_kind = source
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        title_match = _MERMAID_TITLE_RE.search(frontmatter)
        if title_match:
            raw_title = title_match.group(1).strip()
            # Trim surrounding quotes if present (`title: "Foo"`).
            if (
                len(raw_title) >= 2
                and raw_title[0] == raw_title[-1]
                and raw_title[0] in ("'", '"')
            ):
                raw_title = raw_title[1:-1]
            if raw_title:
                title = raw_title
        body_for_kind = source[frontmatter_match.end():]

    # 2. Diagram kind
    diagram_kind: Optional[str] = None
    kind_match = _MERMAID_KIND_RE.search(body_for_kind)
    if kind_match:
        diagram_kind = kind_match.group(1)

    # 3. Edge / node counts (crude, metadata-only)
    edge_count = len(_MERMAID_EDGE_RE.findall(body_for_kind))
    node_ids = {m.group(1) for m in _MERMAID_NODE_RE.finditer(body_for_kind)}
    node_count = len(node_ids)

    return MermaidMetadata(
        title=title,
        diagram_kind=diagram_kind,
        node_count=node_count,
        edge_count=edge_count,
    )


def parse_excalidraw(scene_json: dict) -> ExcalidrawMetadata:
    """Extract :class:`ExcalidrawMetadata` from a parsed Excalidraw scene.

    Accepts the already-parsed JSON dict (not raw text). Phase 1.5.A
    may add a ``parse_excalidraw_file(path)`` convenience; we don't
    need it for the STUB.
    """
    if not isinstance(scene_json, dict):
        return ExcalidrawMetadata(scene_name=None, text_labels=[], element_counts={})

    app_state = scene_json.get("appState") if isinstance(scene_json.get("appState"), dict) else {}
    scene_name = app_state.get("name") if isinstance(app_state.get("name"), str) else None
    if scene_name is not None:
        scene_name = scene_name.strip() or None

    elements = scene_json.get("elements")
    if not isinstance(elements, list):
        elements = []

    text_labels: list[str] = []
    element_counts: dict[str, int] = {}
    for el in elements:
        if not isinstance(el, dict):
            continue
        el_type = el.get("type")
        if isinstance(el_type, str):
            element_counts[el_type] = element_counts.get(el_type, 0) + 1
        # `text` element + any element with a `text` property (label-bearing).
        text = el.get("text")
        if isinstance(text, str) and text.strip():
            text_labels.append(text.strip())

    return ExcalidrawMetadata(
        scene_name=scene_name,
        text_labels=text_labels,
        element_counts=element_counts,
    )


# ---------------------------------------------------------------------------
# Filesystem layout helpers
# ---------------------------------------------------------------------------


def _sidecar_path_for(file_path: Path) -> Path:
    """Sibling sidecar path: ``foo.mmd`` → ``foo.meta.json``."""
    return file_path.with_suffix(file_path.suffix + ".meta.json") \
        if file_path.suffix.lower() in (".mmd", ".excalidraw") \
        else file_path.with_suffix(".meta.json")


def _diagram_type_for(file_path: Path) -> Optional[str]:
    """Map suffix → type. Returns ``None`` for unknown suffixes."""
    suffix = file_path.suffix.lower()
    if suffix == ".mmd":
        return "mermaid"
    if suffix == ".excalidraw":
        return "excalidraw"
    return None


def _category_path_for(file_path: Path, diagrams_root: Path) -> str:
    """Derive ``gui/auth`` from ``.claude/diagrams/gui/auth/login.mmd``.

    The category is the directory prefix UNDER ``diagrams_root``, joined
    with forward slashes (POSIX-style — sidecar JSON is cross-OS).
    Returns ``""`` if the file lives directly under ``diagrams_root``.
    """
    try:
        rel = file_path.parent.relative_to(diagrams_root)
    except ValueError:
        # File isn't under diagrams_root — caller's bug. STUB returns
        # empty so the CLI doesn't crash on a misplaced file.
        return ""
    parts = [p for p in rel.parts if p not in ("", ".")]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Idempotent indexer
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_sidecar(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_sidecar(path: Path, payload: dict) -> None:
    """Write sidecar as canonical JSON (sorted keys, UTF-8) so re-runs
    produce byte-identical output."""
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    # Trailing newline so editors don't keep adding one.
    if not serialized.endswith("\n"):
        serialized += "\n"
    path.write_text(serialized, encoding="utf-8")


def index_diagram(
    file_path: Path,
    project_id: str,
    chat_id: Optional[str] = None,
    *,
    diagrams_root: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> DiagramRow:
    """STUB implementation — see module docstring.

    Args:
        file_path: Absolute or project-relative path to the diagram.
        project_id: Launcher project id (slug or rowid).
        chat_id: Claude session id, or None for user-initiated saves.
        diagrams_root: Root directory for the project's diagrams, used
            to derive ``category_path``. Defaults to the file's
            ``.claude/diagrams`` ancestor.
        now: Test hook — fixed timestamp for ``indexed_at``.

    Returns:
        :class:`DiagramRow` with sidecar info. ``wrote_sidecar`` is
        ``True`` when the sidecar's prior ``content_hash`` did NOT match
        the new content (or the sidecar was absent); ``False`` otherwise.
        ``wrote_weaviate`` is always ``False`` in the STUB.

    Raises:
        FileNotFoundError: If the diagram file is missing.
        ValueError: If the suffix isn't a recognised diagram type.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"diagram not found: {file_path}")

    diagram_type = _diagram_type_for(file_path)
    if diagram_type is None:
        raise ValueError(
            f"not a recognised diagram type: {file_path.suffix} "
            f"(expected .mmd or .excalidraw)"
        )

    # Resolve diagrams_root: defaults to the closest `.claude/diagrams`
    # ancestor.
    if diagrams_root is None:
        for ancestor in file_path.parents:
            if ancestor.name == "diagrams" and ancestor.parent.name == ".claude":
                diagrams_root = ancestor
                break
    if diagrams_root is None:
        # Fall back to the file's parent — category will be empty.
        diagrams_root = file_path.parent
    diagrams_root = Path(diagrams_root).resolve()

    category_path = _category_path_for(file_path, diagrams_root)
    diagram_name = file_path.stem

    raw_bytes = file_path.read_bytes()
    content_hash = _sha256_bytes(raw_bytes)

    # Compute metadata — used for sidecar + (eventually) Weaviate.
    inferred_title: Optional[str] = None
    diagram_kind: Optional[str] = None
    if diagram_type == "mermaid":
        source = raw_bytes.decode("utf-8", errors="replace")
        meta = parse_mermaid(source)
        inferred_title = meta.title
        diagram_kind = meta.diagram_kind
    elif diagram_type == "excalidraw":
        try:
            scene = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except ValueError:
            scene = {}
        ex_meta = parse_excalidraw(scene)
        inferred_title = ex_meta.scene_name
        diagram_kind = "excalidraw"
    if not inferred_title:
        inferred_title = humanize_filename(diagram_name) or diagram_name

    sidecar_path = _sidecar_path_for(file_path)
    timestamp = (now or _dt.datetime.now(_dt.timezone.utc)).replace(microsecond=0)
    new_payload = {
        "schema": "vco-diagram-sidecar/v1",
        "content_hash": content_hash,
        "project_id": project_id,
        "chat_id": chat_id,
        "file_path": str(file_path),
        "diagram_type": diagram_type,
        "diagram_name": diagram_name,
        "category_path": category_path,
        "inferred_title": inferred_title,
        "diagram_kind": diagram_kind,
        "indexed_at": timestamp.isoformat(),
    }

    prior = _read_sidecar(sidecar_path)
    # Idempotency: skip write when content_hash matches AND the
    # non-timestamp fields are stable. We compare a "fingerprint" view
    # so a regenerated `indexed_at` doesn't cause a needless rewrite.
    def _fingerprint(payload: dict) -> tuple:
        return (
            payload.get("content_hash"),
            payload.get("project_id"),
            payload.get("chat_id"),
            payload.get("diagram_type"),
            payload.get("category_path"),
            payload.get("inferred_title"),
            payload.get("diagram_kind"),
        )

    wrote_sidecar = False
    if prior is None or _fingerprint(prior) != _fingerprint(new_payload):
        # Preserve the prior `indexed_at` when only the timestamp would
        # change — keeps sidecars stable across re-runs.
        if prior is not None and _fingerprint(prior) == _fingerprint(new_payload):
            new_payload["indexed_at"] = prior.get("indexed_at", new_payload["indexed_at"])
        else:
            _write_sidecar(sidecar_path, new_payload)
            wrote_sidecar = True
    # else: fingerprint matches → no write, preserve mtime + indexed_at.

    return DiagramRow(
        file_path=file_path,
        project_id=project_id,
        chat_id=chat_id,
        diagram_type=diagram_type,
        diagram_name=diagram_name,
        category_path=category_path,
        inferred_title=inferred_title,
        diagram_kind=diagram_kind,
        content_hash=content_hash,
        sidecar_path=sidecar_path,
        wrote_sidecar=wrote_sidecar,
        wrote_weaviate=False,  # STUB never touches Weaviate.
    )
