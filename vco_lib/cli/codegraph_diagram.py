# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco codegraph-diagram`` — Phase 3 of the diagrams-integration plan.

Generates a Mermaid ``flowchart TD`` source from the per-project Weaviate
code-graph collections, writes it under ``.claude/diagrams/codegraph/``
(or wherever ``--output`` points), and triggers the diagram indexer so
the new diagram is searchable via ``hybrid_search``.

Usage::

    vco codegraph-diagram <seed_symbol> \
        [--hops N] [--scope <calls|imports|extends|composes|interactions|all>] \
        [--output PATH] [--max-nodes N] [--no-modules] [--title TEXT] \
        [--print] [--json] [--project NAME]

Exit codes (mirror ``vco_lib.cli.rebuild_diagram_index`` / ``vco_lib.cli.verify``):

* 0 — OK (diagram written / printed).
* 1 — render error (Weaviate query partially failed, file write failed,
  indexing raised).
* 2 — env problem (Weaviate unreachable; seed symbol not in the code
  graph; project not resolvable).

Cross-OS rules (see ``knowledge/concepts/cross-os-hook-portability.md``):

* No subprocess calls.
* Atomic file writes via ``tempfile.NamedTemporaryFile + os.replace`` in
  the SAME directory as the target (avoids cross-device rename failures).
* ``pathlib.Path`` everywhere.
* No ``/tmp`` literals; ``tempfile.gettempdir()`` if scratch were needed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Exit codes (keep stable; tests assert against these) ────────────────

EXIT_OK = 0
EXIT_RENDER_ERROR = 1
EXIT_ENV_PROBLEM = 2


# ─── Scope choices (kept in sync with ``codegraph_to_mermaid._ALLOWED_SCOPES``)

SCOPE_CHOICES = ("calls", "imports", "extends", "composes", "interactions", "all")
DEFAULT_SCOPE = "calls"
DEFAULT_HOPS = 2
DEFAULT_MAX_NODES = 50
MAX_HOPS = 3


# ─── Wrappers — kept thin so tests can monkey-patch the call sites. ──────


def _generate(spec: Any, project: Optional[str], title: Optional[str]) -> str:
    """Thin wrapper around ``vco_lib.codegraph_to_mermaid.generate``.

    Local-import so tests can monkey-patch this function without touching
    the renderer module (avoids "imported the wrong reference" failures
    when ``codegraph_to_mermaid`` is reimported by other tests).
    """
    from vco_lib.codegraph_to_mermaid import generate
    return generate(spec, project=project, title=title)


def _fetch_subgraph(spec: Any, project: Optional[str]) -> dict:
    """Wrapper around ``vco_lib.codegraph_to_mermaid.fetch_subgraph``."""
    from vco_lib.codegraph_to_mermaid import fetch_subgraph
    return fetch_subgraph(spec, project=project)


def _render(subgraph: dict, *, title: Optional[str], include_modules: bool) -> str:
    """Wrapper around ``vco_lib.codegraph_to_mermaid.render_mermaid``."""
    from vco_lib.codegraph_to_mermaid import render_mermaid
    return render_mermaid(
        subgraph, title=title, include_modules=include_modules,
    )


def _index_diagram(file_path: Path, project_id: str, chat_id: Optional[str]) -> Any:
    """Wrapper around ``vco_lib.diagram_indexer.index_diagram``.

    Lifted so tests can swap a no-op stub when they don't want the indexer
    to talk to Weaviate / the launcher DB.
    """
    from vco_lib.diagram_indexer import index_diagram
    return index_diagram(file_path, project_id, chat_id)


def _resolve_project_name() -> Optional[str]:
    """Best-effort project-name resolution for the Weaviate collection prefix.

    Tries the vct-hub resolver first (canonical per-project value via the
    launcher), falls back to ``CODE_GRAPH_PROJECT`` / ``PROJECT_NAME`` env
    vars. Returns None when nothing is set — the codegraph collections
    will then be queried bare (matching pre-multi-project behaviour).
    """
    try:
        from vco_lib.project_config import resolve as _vco_resolve  # type: ignore
        cfg = _vco_resolve(Path.cwd())
        if cfg.code_graph_project:
            return cfg.code_graph_project
    except Exception:  # noqa: BLE001 — hub may be down; fall through
        pass
    return os.environ.get("CODE_GRAPH_PROJECT") or os.environ.get("PROJECT_NAME") or None


def _resolve_project_id_for_indexing() -> Optional[str]:
    """Best-effort: project_id (UUID/slug) for the diagram indexer.

    The indexer needs the launcher's ``project_id``, NOT the code-graph
    project NAME (those are different identifiers — name is the
    Weaviate-prefix slug; id is the SQLite rowid/UUID).
    """
    try:
        from vco_lib.project_config import resolve as _vco_resolve  # type: ignore
        cfg = _vco_resolve(Path.cwd())
        return cfg.project_id or cfg.project_slug or None
    except Exception:  # noqa: BLE001 — hub may be down
        pass
    return None


# ─── Filename / path helpers ──────────────────────────────────────────────

#: Kebab-case enforcement for the auto-generated diagram filename so the
#: output path satisfies ``vco_lib.diagram_paths.validate_scoped_path``.
#: We keep this regex SEPARATE from the one in ``diagram_paths`` because
#: ``diagram_paths`` is the validator (rejects bad names); this is the
#: sanitiser (turns symbols into valid names).
_FILENAME_SANITISE_RE = re.compile(r"[^a-z0-9-]+")


def _sanitise_filename(seed_symbol: str) -> str:
    """Convert a code symbol into a kebab-case filename stem.

    Examples:
        sanitise("vco_lib.diagram_indexer.index_diagram") -> "index-diagram"
        sanitise("api.UserManager")                       -> "user-manager"
        sanitise("api/routes.py")                         -> "routes-py"
        sanitise("")                                       -> "untitled"

    We drop the module qualifier and keep only the final segment because
    full dotted paths produce 60+ char filenames that look terrible in
    listings. The collision risk (two seeds producing the same filename)
    is acceptable: the user passes ``--output`` to override, and the
    indexer's UPSERT on ``(project_id, diagram_name)`` would warn anyway.
    """
    if not seed_symbol or not seed_symbol.strip():
        return "untitled"
    stripped = seed_symbol.strip()
    # If the input looks like a file path (contains `/` or `\`), take the
    # basename and KEEP the extension by replacing the dot with a hyphen
    # ("routes.py" -> "routes-py"). Pure dotted symbols (no slash) instead
    # treat dots as module separators and drop all but the last segment
    # ("vco_lib.diagram_indexer.index_diagram" -> "index-diagram").
    if "/" in stripped or "\\" in stripped:
        last = re.split(r"[/\\:]+", stripped)[-1] or stripped
        # Hyphenate the file extension so "routes.py" survives.
        last = last.replace(".", "-")
    else:
        last = re.split(r"[\.:]+", stripped)[-1] or stripped
    # Lowercase + insert hyphen between camelCase, then squash invalids.
    cameled = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", last)
    snake_ish = cameled.replace("_", "-").lower()
    sanitised = _FILENAME_SANITISE_RE.sub("-", snake_ish).strip("-")
    if not sanitised:
        sanitised = "untitled"
    # Squash repeated hyphens introduced by the sanitiser.
    while "--" in sanitised:
        sanitised = sanitised.replace("--", "-")
    return sanitised


def _default_output_path(seed_symbol: str) -> Path:
    """``.claude/diagrams/codegraph/<sanitised>.mmd`` under CWD.

    Always uses the scoped path layout so the indexer accepts the file
    without the user needing to know the convention. Anchored at the CWD
    (where the CLI was invoked) rather than the project root — staying
    consistent with how ``rebuild-diagram-index`` resolves project folders
    (positional project_id → folder via resolver). Path is later validated.
    """
    stem = _sanitise_filename(seed_symbol)
    return Path.cwd() / ".claude" / "diagrams" / "codegraph" / f"{stem}.mmd"


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic UTF-8 text write via tempfile + os.replace.

    Tempfile is created in the SAME directory as the target so
    ``os.replace`` is guaranteed atomic (cross-device renames are not).
    Mirrors ``vco_lib.config_projection._atomic_write_text``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".mmd.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _is_under_diagrams_dir(path: Path) -> bool:
    """True iff ``path`` is under a ``.claude/diagrams/`` segment.

    The indexer demands this layout — files outside of it won't be picked
    up by ``hybrid_search`` even after a manual rebuild. We don't reject
    such paths in the CLI (the user might want to write to a docs/
    location for an explicit screenshot) but we skip the indexer call and
    warn so the user knows.
    """
    parts = path.resolve().as_posix().split("/")
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and i + 1 < len(parts) and parts[i + 1] == "diagrams":
            return True
    return False


# ─── argparse entry-point ────────────────────────────────────────────────


def cmd_codegraph_diagram(args: argparse.Namespace) -> int:
    """Entry-point for ``vco codegraph-diagram``."""
    # PRE-ALPHA banner — printed on every invocation so users know the
    # codegraph→Mermaid output should not be trusted without manual review.
    # Suppressed in --json mode (would corrupt machine-parseable stdout
    # if any tool mixed them; banner goes to stderr regardless but the
    # signal is redundant for scripting use cases).
    if not getattr(args, "json", False):
        print(
            "[PRE-ALPHA] codegraph→Mermaid is experimental. Output may be "
            "incomplete, inaccurate, or visually broken. Verify before "
            "sharing.",
            file=sys.stderr,
        )

    # 1. Build the spec (validates hops/scope/max-nodes via SubgraphSpec).
    try:
        from vco_lib.codegraph_to_mermaid import SubgraphSpec
        spec = SubgraphSpec(
            seed_symbol=args.seed_symbol,
            hops=args.hops,
            scope=args.scope,
            max_nodes=args.max_nodes,
            include_modules=not args.no_modules,
        )
    except ValueError as exc:
        _emit_error(
            args, EXIT_ENV_PROBLEM, "usage_error", str(exc),
        )
        return EXIT_ENV_PROBLEM
    except ImportError as exc:
        _emit_error(
            args, EXIT_ENV_PROBLEM, "import_error",
            f"vco_lib.codegraph_to_mermaid unavailable: {exc}",
        )
        return EXIT_ENV_PROBLEM

    # 2. Resolve the project name for the Weaviate prefix.
    project_name = args.project or _resolve_project_name()

    # 3. Fetch subgraph + render.
    try:
        subgraph = _fetch_subgraph(spec, project_name)
    except Exception as exc:
        _emit_error(
            args, EXIT_RENDER_ERROR, "fetch_error",
            f"fetch_subgraph raised: {exc}",
        )
        return EXIT_RENDER_ERROR

    if not subgraph.get("seed_found"):
        # Distinguish "Weaviate unreachable" from "seed not in graph" via
        # the truncation_reason marker the renderer leaves behind.
        reason = subgraph.get("truncation_reason") or "seed_symbol not in code graph"
        _emit_error(
            args, EXIT_ENV_PROBLEM, "seed_not_found",
            (
                f"seed '{spec.seed_symbol}' could not be resolved "
                f"(tried function → class → module). {reason}"
            ),
        )
        return EXIT_ENV_PROBLEM

    auto_title = subgraph.get("seed_full_name") or spec.seed_symbol
    effective_title = args.title or auto_title

    try:
        mermaid_src = _render(
            subgraph,
            title=effective_title,
            include_modules=spec.include_modules,
        )
    except Exception as exc:
        _emit_error(
            args, EXIT_RENDER_ERROR, "render_error",
            f"render_mermaid raised: {exc}",
        )
        return EXIT_RENDER_ERROR

    # 4. Output: --print → stdout, else write to --output path.
    if args.print:
        if args.json:
            payload = {
                "command": "codegraph-diagram",
                "exit_code": EXIT_OK,
                "overall": "ok",
                "mode": "print",
                "seed_symbol": spec.seed_symbol,
                "seed_full_name": subgraph.get("seed_full_name"),
                "nodes": len(subgraph.get("nodes") or []),
                "edges": len(subgraph.get("edges") or []),
                "truncated": bool(subgraph.get("truncated")),
                "truncation_reason": subgraph.get("truncation_reason"),
                "title": effective_title,
                "mermaid": mermaid_src,
            }
            json.dump(payload, sys.stdout)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(mermaid_src)
            if not mermaid_src.endswith("\n"):
                sys.stdout.write("\n")
        return EXIT_OK

    out_path: Path = args.output if args.output else _default_output_path(spec.seed_symbol)
    out_path = out_path.expanduser()

    try:
        _atomic_write_text(out_path, mermaid_src)
    except OSError as exc:
        _emit_error(
            args, EXIT_RENDER_ERROR, "write_error",
            f"failed to write {out_path}: {exc}",
        )
        return EXIT_RENDER_ERROR

    # 5. Index — only when the file is under .claude/diagrams/ AND a
    # project_id resolves. Failures degrade to a warning so the diagram
    # still lands on disk for the user to inspect manually.
    indexed = False
    index_error: Optional[str] = None
    if _is_under_diagrams_dir(out_path):
        project_id = _resolve_project_id_for_indexing()
        if project_id:
            try:
                _index_diagram(
                    out_path.resolve(), project_id,
                    os.environ.get("CLAUDE_CODE_SESSION_ID"),
                )
                indexed = True
            except Exception as exc:
                index_error = str(exc)
                logger.warning(
                    "Indexer failed for %s: %s — diagram written but won't be "
                    "searchable until rebuild-diagram-index runs.",
                    out_path, exc,
                )
        else:
            index_error = "no project_id resolvable (launcher not running?)"
    else:
        index_error = (
            "output path is NOT under .claude/diagrams/ — skipping indexer "
            "(file won't be picked up by hybrid_search)"
        )

    # 6. Report.
    payload = {
        "command": "codegraph-diagram",
        "exit_code": EXIT_OK,
        "overall": "ok",
        "mode": "write",
        "path": str(out_path),
        "seed_symbol": spec.seed_symbol,
        "seed_full_name": subgraph.get("seed_full_name"),
        "seed_kind": subgraph.get("seed_kind"),
        "nodes": len(subgraph.get("nodes") or []),
        "edges": len(subgraph.get("edges") or []),
        "truncated": bool(subgraph.get("truncated")),
        "truncation_reason": subgraph.get("truncation_reason"),
        "title": effective_title,
        "indexed": indexed,
        "index_error": index_error,
    }
    if args.json:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    else:
        _print_human_summary(payload)
    return EXIT_OK


# ─── Output formatting ────────────────────────────────────────────────────


def _emit_error(
    args: argparse.Namespace,
    exit_code: int,
    overall: str,
    message: str,
) -> None:
    """Single-source-of-truth error emitter (JSON vs human)."""
    if args.json:
        json.dump(
            {
                "command": "codegraph-diagram",
                "exit_code": exit_code,
                "overall": overall,
                "error": message,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        print(f"codegraph-diagram: {message}", file=sys.stderr)


def _print_human_summary(payload: dict) -> None:
    lines = [
        f"Wrote: {payload['path']}",
        f"  Seed:           {payload['seed_full_name']}  ({payload.get('seed_kind', '?')})",
        f"  Title:          {payload['title']}",
        f"  Nodes:          {payload['nodes']}",
        f"  Edges:          {payload['edges']}",
    ]
    if payload.get("truncated"):
        lines.append(f"  Truncated:      YES — {payload.get('truncation_reason')}")
    if payload.get("indexed"):
        lines.append("  Indexed:        yes (visible via hybrid_search)")
    elif payload.get("index_error"):
        lines.append(f"  Indexed:        no — {payload['index_error']}")
    print("\n".join(lines))


# ─── argparse wiring ─────────────────────────────────────────────────────


def add_subparsers(sub: Any) -> None:
    """Register ``codegraph-diagram`` onto the parent subparsers.

    Called by :mod:`vco_lib.cli.__main__`.
    """
    parser = sub.add_parser(
        "codegraph-diagram",
        help=(
            "[PRE-ALPHA] Generate a Mermaid flowchart of a code subgraph "
            "centered on a function / class / module. Output may be "
            "incomplete, inaccurate, or visually broken — iterate manually "
            "before sharing. Writes under .claude/diagrams/codegraph/"
            "<sanitised>.mmd by default and indexes the result so "
            "hybrid_search can find it. Exit 0=OK, 1=render error, "
            "2=env problem (Weaviate down, seed not found)."
        ),
    )
    parser.add_argument(
        "seed_symbol",
        help=(
            "Code symbol to center the subgraph on. Resolved in order: "
            "CodeFunction.full_name -> CodeClass.full_name -> CodeModule.path."
        ),
    )
    parser.add_argument(
        "--hops", type=int, default=DEFAULT_HOPS,
        help=(
            f"BFS depth (default {DEFAULT_HOPS}, max {MAX_HOPS}). "
            f"Beyond {MAX_HOPS} the auto-layout becomes unreadable."
        ),
    )
    parser.add_argument(
        "--scope", default=DEFAULT_SCOPE, choices=SCOPE_CHOICES,
        help=(
            f"Edge type(s) to traverse (default '{DEFAULT_SCOPE}'). "
            "'all' fans out across every supported scope."
        ),
    )
    parser.add_argument(
        "--output", type=Path,
        help=(
            "Output .mmd path. Default: .claude/diagrams/codegraph/"
            "<sanitised-seed>.mmd under CWD. Use --print for stdout."
        ),
    )
    parser.add_argument(
        "--max-nodes", type=int, default=DEFAULT_MAX_NODES,
        help=(
            f"Cap on nodes in the rendered diagram (default "
            f"{DEFAULT_MAX_NODES}). Beyond this Mermaid auto-layout breaks "
            "down; truncation is reported in the summary."
        ),
    )
    parser.add_argument(
        "--no-modules", action="store_true",
        help=(
            "Disable per-module subgraph grouping. Useful for tiny "
            "diagrams where the wrapping blocks add more noise than signal."
        ),
    )
    parser.add_argument(
        "--title",
        help=(
            "Override the auto-derived title. Default: the resolved seed's "
            "full_name."
        ),
    )
    parser.add_argument(
        "--print", action="store_true",
        help=(
            "Write the Mermaid source to stdout instead of a file. "
            "Skips indexing."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable single-object output on stdout.",
    )
    parser.add_argument(
        "--project",
        help=(
            "Override the code-graph project name (otherwise resolved via "
            "the launcher's vct-hub or CODE_GRAPH_PROJECT / PROJECT_NAME env)."
        ),
    )
    parser.set_defaults(func=cmd_codegraph_diagram)
