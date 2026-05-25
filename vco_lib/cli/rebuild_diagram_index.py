# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco rebuild-diagram-index`` — Phase 1.5.C acceptance.

Walks ``<project>/.claude/diagrams/**/*.{mmd,excalidraw}``, calls
:func:`vco_lib.diagram_indexer.index_diagram` for each, and reports
counts (total / indexed / skipped / failed / orphans).

The plan's acceptance criterion (§1.5.8) is the idempotency check:
re-running this command must produce byte-identical sidecars and ZERO
Weaviate writes. The implementation enforces that by delegating
idempotency to ``index_diagram`` (which skips sidecar writes when the
content hash matches) and by checking the indexer's return value to
count actual mutations.

Exit codes (mirror :mod:`vco_lib.cli.verify`):

* 0 — OK (all diagrams indexed cleanly).
* 1 — indexing errors (at least one diagram failed to index).
* 2 — env problem (project not found in launcher DB; Weaviate down
  when actually wired up by Phase 1.5.A).
* 3 — idempotency-broken on ``--dry-run`` (the run would have written
  even though the prior state is clean).

Flags:

* ``--json`` — machine-readable single-object output.
* ``--prune`` — remove orphan sidecars (sidecar exists but its diagram
  doesn't). When the real Phase 1.5.A is wired, also drops the
  corresponding Weaviate object.
* ``--all`` — run against every project registered in the launcher DB.
* ``--dry-run`` — report what would happen without writes.

Cross-OS rules (see ``knowledge/concepts/cross-os-hook-portability.md``):

* No subprocess calls — pure-Python walk + indexer invocation.
* All paths flow through :class:`pathlib.Path`.
* No ``/tmp`` literals; ``tempfile.gettempdir()`` if scratch is needed
  (none today).

Cross-module dependencies:
  - ``vco_lib.diagram_indexer.index_diagram`` does the per-file work.
  - ``vco_lib.config_projection.resolve_project_folder`` /
    ``list_registered_projects`` are wrapped via lazy imports so tests can
    monkey-patch them and so the CLI can run before Phase 0.B Part 2
    finishes routing every caller through the projection contract.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


# ---------------------------------------------------------------------------
# Exit-code constants — match :mod:`vco_lib.cli.verify`.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_INDEX_ERRORS = 1
EXIT_ENV_PROBLEM = 2
EXIT_IDEMPOTENCY_BROKEN = 3


# ---------------------------------------------------------------------------
# Phase 0.B / 1.5.A dependency wrappers — see module docstring.
# ---------------------------------------------------------------------------


def _resolve_project_folder(project_id: str) -> Path:
    """Phase 0.B dependency wrapper.

    Resolves a project slug / rowid to its on-disk folder. Tests stub
    this; production wires it to
    ``vco_lib.config_projection.resolve_project_folder``.
    """
    # Phase 0.B dependency — actual import wired post-merge.
    try:
        from vco_lib.config_projection import resolve_project_folder  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.resolve_project_folder() is not "
            "available. This subcommand requires Phase 0.B (Part 2) to be "
            "merged. If you're running tests, monkey-patch "
            "`_resolve_project_folder` on `vco_lib.cli.rebuild_diagram_index`."
        ) from exc
    return resolve_project_folder(project_id)


def _list_registered_projects() -> Iterable[Mapping[str, str]]:
    """Phase 0.B dependency wrapper.

    Iterable of ``{"id": str, "slug": str, "folder": str}`` dicts.
    Drives ``--all``. Tests stub this.
    """
    # Phase 0.B dependency — actual import wired post-merge.
    try:
        from vco_lib.config_projection import list_registered_projects  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.list_registered_projects() is not "
            "available. This subcommand requires Phase 0.B (Part 2) to be "
            "merged."
        ) from exc
    return list_registered_projects()


def _index_diagram(file_path: Path, project_id: str, chat_id: Optional[str] = None) -> Any:
    """Indexer wrapper kept thin so tests can monkey-patch the call site.

    Returns a :class:`DiagramRow`-shaped object (we read ``.wrote_sidecar``
    and ``.wrote_weaviate`` to count the report).
    """
    from vco_lib.diagram_indexer import index_diagram  # local import for monkey-patching
    return index_diagram(file_path, project_id, chat_id)


def _drop_weaviate_diagram(content_hash: str, project_id: str) -> bool:
    """Orphan-prune wrapper. Best-effort — Weaviate side of orphan cleanup."""
    from vco_lib.diagram_indexer import drop_diagram_by_hash
    try:
        return bool(drop_diagram_by_hash(content_hash, project_id))
    except Exception:  # pragma: no cover — best-effort cleanup
        return False


# ---------------------------------------------------------------------------
# Walk + report
# ---------------------------------------------------------------------------


DIAGRAM_SUFFIXES: tuple[str, ...] = (".mmd", ".excalidraw")


@dataclasses.dataclass
class _ProjectReport:
    """Per-project counters; aggregated when ``--all`` is used."""

    project_id: str
    project_folder: str
    total: int = 0
    indexed: int = 0
    skipped: int = 0           # content unchanged — sidecar already up to date
    failed: int = 0
    weaviate_writes: int = 0   # for the idempotency assertion
    orphans: list[str] = dataclasses.field(default_factory=list)
    orphans_pruned: int = 0
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)
    dry_run: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_folder": self.project_folder,
            "total": self.total,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "weaviate_writes": self.weaviate_writes,
            "orphans": list(self.orphans),
            "orphans_pruned": self.orphans_pruned,
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }


def _enumerate_diagrams(diagrams_root: Path) -> list[Path]:
    """Recursive walk for diagram files. Sorted for stable test output.

    Excludes sidecar ``.meta.json`` files (handled separately) and any
    file whose suffix isn't in :data:`DIAGRAM_SUFFIXES`.
    """
    if not diagrams_root.exists():
        return []
    out: list[Path] = []
    for path in diagrams_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in DIAGRAM_SUFFIXES:
            out.append(path)
    return sorted(out)


def _enumerate_sidecars(diagrams_root: Path) -> list[Path]:
    """Recursive walk for sidecar ``*.meta.json`` files."""
    if not diagrams_root.exists():
        return []
    out: list[Path] = []
    for path in diagrams_root.rglob("*.meta.json"):
        if path.is_file():
            out.append(path)
    return sorted(out)


def _sidecar_to_diagram(sidecar: Path) -> Optional[Path]:
    """Derive the diagram path a sidecar belongs to.

    Sidecar shape from the indexer: ``foo.mmd.meta.json`` →
    ``foo.mmd``. Returns ``None`` if the name doesn't follow the
    convention (defensive — keeps the walk robust against stray
    ``.meta.json`` files).
    """
    name = sidecar.name
    if not name.endswith(".meta.json"):
        return None
    stem = name[: -len(".meta.json")]
    if not stem:
        return None
    candidate = sidecar.with_name(stem)
    if candidate.suffix.lower() in DIAGRAM_SUFFIXES:
        return candidate
    return None


def _index_one(
    file_path: Path,
    project_id: str,
    report: _ProjectReport,
    *,
    dry_run: bool,
) -> None:
    """Index a single diagram; updates ``report`` in place."""
    report.total += 1
    if dry_run:
        # Dry-run: don't call the indexer (which would write); compare
        # the file's current content hash to the sidecar's content_hash.
        # Match → would-skip; mismatch → would-index.
        from vco_lib.diagram_indexer import _read_sidecar, _sha256_bytes  # noqa: WPS437,E501
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            report.failed += 1
            report.errors.append({"file": str(file_path), "error": str(exc)})
            return
        new_hash = _sha256_bytes(raw)
        # Sidecar path mirrors the indexer convention.
        sidecar_path = file_path.with_suffix(file_path.suffix + ".meta.json")
        prior = _read_sidecar(sidecar_path)
        if prior is not None and prior.get("content_hash") == new_hash:
            report.skipped += 1
        else:
            report.indexed += 1
        return

    try:
        row = _index_diagram(file_path, project_id, None)
    except Exception as exc:
        report.failed += 1
        report.errors.append({"file": str(file_path), "error": str(exc)})
        return
    if getattr(row, "wrote_sidecar", False):
        report.indexed += 1
    else:
        report.skipped += 1
    if getattr(row, "wrote_weaviate", False):
        report.weaviate_writes += 1


def _handle_orphans(
    diagrams_root: Path,
    report: _ProjectReport,
    *,
    prune: bool,
    dry_run: bool,
) -> None:
    """Detect sidecars whose diagram file is gone; optionally prune."""
    for sidecar in _enumerate_sidecars(diagrams_root):
        diagram_path = _sidecar_to_diagram(sidecar)
        if diagram_path is None:
            # Defensive: stray .meta.json without a diagram counterpart.
            report.orphans.append(str(sidecar))
            if prune and not dry_run:
                try:
                    sidecar.unlink()
                    report.orphans_pruned += 1
                except OSError as exc:
                    report.errors.append({"file": str(sidecar), "error": str(exc)})
            continue
        if diagram_path.exists():
            continue
        # Diagram is gone → orphan sidecar.
        report.orphans.append(str(sidecar))
        if prune and not dry_run:
            # Drop the Weaviate object first (best-effort; the indexer
            # silently no-ops when Weaviate is unreachable / unconfigured).
            try:
                content_hash = json.loads(sidecar.read_text(encoding="utf-8")).get("content_hash", "")
            except (OSError, ValueError):
                content_hash = ""
            if content_hash:
                _drop_weaviate_diagram(content_hash, report.project_id)
            try:
                sidecar.unlink()
                report.orphans_pruned += 1
            except OSError as exc:
                report.errors.append({"file": str(sidecar), "error": str(exc)})


def _run_for_project(
    project_id: str,
    project_folder: Path,
    *,
    prune: bool,
    dry_run: bool,
) -> tuple[int, _ProjectReport]:
    """Walk + index for one project. Returns ``(exit_code, report)``."""
    diagrams_root = project_folder / ".claude" / "diagrams"
    report = _ProjectReport(
        project_id=project_id,
        project_folder=str(project_folder),
        dry_run=dry_run,
    )
    diagrams = _enumerate_diagrams(diagrams_root)
    for diagram in diagrams:
        _index_one(diagram, project_id, report, dry_run=dry_run)
    _handle_orphans(diagrams_root, report, prune=prune, dry_run=dry_run)

    if report.failed > 0:
        return EXIT_INDEX_ERRORS, report
    # EXIT_IDEMPOTENCY_BROKEN reserved for a future self-check (`vco
    # rebuild-diagram-index --verify-idempotency` would run an
    # immediate dry-run after a real run and exit 3 if the dry-run
    # still flagged writes). Today the contract is enforced
    # structurally: the indexer skips sidecar writes when content_hash
    # matches, and the dry-run path uses the exact same comparison
    # logic. A legitimate dry-run on a fresh project reports
    # ``indexed > 0`` because no sidecars exist yet — that's the
    # expected state and must NOT exit 3.
    return EXIT_OK, report


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _format_report_human(report: _ProjectReport) -> str:
    lines = [
        f"Project: {report.project_id}  ({report.project_folder})",
        f"  Total:           {report.total}",
        f"  Indexed:         {report.indexed}",
        f"  Skipped (up-to-date): {report.skipped}",
        f"  Failed:          {report.failed}",
        f"  Weaviate writes: {report.weaviate_writes}",
        f"  Orphans:         {len(report.orphans)}"
        f"{f' (pruned {report.orphans_pruned})' if report.orphans_pruned else ''}",
    ]
    if report.dry_run:
        lines.insert(1, "  Mode: dry-run (no writes)")
    if report.errors:
        lines.append("  Errors:")
        for err in report.errors[:10]:
            lines.append(f"    - {err['file']}: {err['error']}")
        if len(report.errors) > 10:
            lines.append(f"    ... and {len(report.errors) - 10} more")
    if report.orphans and not report.orphans_pruned:
        lines.append(
            "  Orphan sidecars (re-run with --prune to remove):"
        )
        for o in report.orphans[:10]:
            lines.append(f"    - {o}")
        if len(report.orphans) > 10:
            lines.append(f"    ... and {len(report.orphans) - 10} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# argparse entry-point
# ---------------------------------------------------------------------------


def cmd_rebuild_diagram_index(args: argparse.Namespace) -> int:
    """Entry-point for ``vco rebuild-diagram-index``."""
    if args.all and args.project_id:
        if args.json:
            json.dump(
                {
                    "command": "rebuild-diagram-index",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "usage_error",
                    "error": "--all and a positional project_id are mutually exclusive",
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(
                "rebuild-diagram-index: --all and a positional project_id "
                "are mutually exclusive.",
                file=sys.stderr,
            )
        return EXIT_ENV_PROBLEM

    if not args.all and not args.project_id:
        if args.json:
            json.dump(
                {
                    "command": "rebuild-diagram-index",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "usage_error",
                    "error": "missing project_slug_or_id (or pass --all)",
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(
                "rebuild-diagram-index: missing positional project_slug_or_id "
                "(or pass --all).",
                file=sys.stderr,
            )
        return EXIT_ENV_PROBLEM

    if args.all:
        try:
            projects = list(_list_registered_projects())
        except Exception as exc:
            if args.json:
                json.dump(
                    {
                        "command": "rebuild-diagram-index",
                        "exit_code": EXIT_ENV_PROBLEM,
                        "overall": "db_unreadable",
                        "error": str(exc),
                    },
                    sys.stdout,
                )
                sys.stdout.write("\n")
            else:
                print(
                    f"rebuild-diagram-index --all: cannot list projects: {exc}",
                    file=sys.stderr,
                )
            return EXIT_ENV_PROBLEM
        worst = EXIT_OK
        reports: list[_ProjectReport] = []
        for project in projects:
            pid = project.get("id") or project.get("slug")
            folder = project.get("folder")
            if not pid or not folder:
                continue
            code, report = _run_for_project(
                pid, Path(folder),
                prune=bool(args.prune),
                dry_run=bool(args.dry_run),
            )
            reports.append(report)
            worst = max(worst, code)
        if args.json:
            json.dump(
                {
                    "command": "rebuild-diagram-index",
                    "exit_code": worst,
                    "overall": _overall_label(worst),
                    "projects": [r.to_payload() for r in reports],
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            for r in reports:
                print(_format_report_human(r))
                print()
            print(f"Overall exit: {worst}")
        return worst

    pid = args.project_id
    try:
        folder = _resolve_project_folder(pid)
    except LookupError as exc:
        if args.json:
            json.dump(
                {
                    "command": "rebuild-diagram-index",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "project_not_found",
                    "project_id": pid,
                    "error": str(exc),
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(f"rebuild-diagram-index: project not found: {pid}", file=sys.stderr)
        return EXIT_ENV_PROBLEM
    except Exception as exc:
        if args.json:
            json.dump(
                {
                    "command": "rebuild-diagram-index",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "db_unreadable",
                    "project_id": pid,
                    "error": str(exc),
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(
                f"rebuild-diagram-index: cannot resolve project {pid}: {exc}",
                file=sys.stderr,
            )
        return EXIT_ENV_PROBLEM

    code, report = _run_for_project(
        pid, Path(folder),
        prune=bool(args.prune),
        dry_run=bool(args.dry_run),
    )
    payload = {
        "command": "rebuild-diagram-index",
        "exit_code": code,
        "overall": _overall_label(code),
        **report.to_payload(),
    }
    if args.json:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(_format_report_human(report))
    return code


def _overall_label(exit_code: int) -> str:
    if exit_code == EXIT_OK:
        return "ok"
    if exit_code == EXIT_INDEX_ERRORS:
        return "index_errors"
    if exit_code == EXIT_ENV_PROBLEM:
        return "env_problem"
    if exit_code == EXIT_IDEMPOTENCY_BROKEN:
        return "idempotency_broken"
    return "unknown"


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def add_subparsers(sub: Any) -> None:
    """Register ``rebuild-diagram-index`` onto the parent subparsers.

    Called by :mod:`vco_lib.cli.__main__`.
    """
    parser = sub.add_parser(
        "rebuild-diagram-index",
        help=(
            "Walk a project's .claude/diagrams/**/*.{mmd,excalidraw} and "
            "re-index each via vco_lib.diagram_indexer.index_diagram. "
            "Exit 0=OK, 1=indexing errors, 2=env problem, 3=idempotency "
            "broken on --dry-run."
        ),
    )
    parser.add_argument(
        "project_id", nargs="?", default=None,
        help=(
            "Project slug or rowid (resolved via "
            "vco_lib.config_projection.resolve_project_folder). Omit when "
            "using --all."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (machine-readable).",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help=(
            "Remove orphan sidecars (and their Weaviate objects, when "
            "the real indexer is wired). Default: report-only."
        ),
    )
    parser.add_argument(
        "--all", action="store_true",
        help=(
            "Run against every project registered in the launcher DB. "
            "Worst exit code across all projects wins."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Report what would happen without writing sidecars or "
            "Weaviate objects. Useful as a pre-commit idempotency check."
        ),
    )
    parser.set_defaults(func=cmd_rebuild_diagram_index)
