#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Summary-degradation health: pending-set scan, once-per-event degradation
notice, backfill, and the recheck command (v0.2.96 WP-7, register issues
13 + 14).

The stopping half of quota exhaustion shipped in v0.2.92 WP-Q
(``summary_backends``' circuit breaker). This module is the THREE halves
that were missing:

1. NOTICE. When the preferred summary tier breaker-opens on a ``quota``
   (token/budget exhaustion) or ``trust`` (headless CLI refused: workspace
   not trusted) failure, the trip seam in ``summary_backends`` spawns
   ``python -m vco_lib.summary_health note-degradation`` detached, which
   emits ONE ledger entry (``kg_summaries_degraded``) stating: summaries
   are generating on a fallback tier (or not at all), exactly WHICH rows
   are pending, and that the retry happens on the 5 h cooldown or via the
   recheck command. ONE entry per event, not per attempt: the ledger's
   add-entry is last-write-wins per condition_id, so re-notify replaces
   rather than stacks.

2. KNOW-WHAT-IS-MISSING, by SCANNING not by a second ledger. Sidecars
   already record the backend that generated each summary, so the pending
   set is derivable: a KG node whose sidecar entry names a backend other
   than the preferred tier WITH a current content hash (a hash-drifted
   row is not pending — the ordinary run's hash gate regenerates it), a
   KG node with no summary at all, and a code entity whose sidecar entry
   names a non-preferred backend (row-hash currency is NOT checked here:
   it lives in Weaviate; the code leg regenerates every non-preferred
   entry and the generator's own staleness rules absorb any drift).

3. EXIT. ``summary-recheck`` clears the breaker latch (``persisted=True``
   — the cross-process file), re-derives the pending set, and
   regenerates EXACTLY it through the now-preferred tier, then resolves
   the ledger entry when the post-scan finds nothing pending. Backend
   selection re-runs inside each generator child (one process per node,
   latch cleared); this module never calls the LLM itself. Idempotent: a
   re-run finds an empty pending set and only re-checks.

``other``-storm demotions (v0.2.96: N consecutive unclassified failures)
do NOT emit the notice — an unknown failure is not known to be token
exhaustion, and the breaker log line already names it.

Sidecar shapes (read-only here; the generators own the writes):
  KG   ``<root>/knowledge/.node_formats.json`` — keyed by rel path,
       entry ``{title, description, summary, generated_at,
       content_hash, backend}`` where ``content_hash`` is
       sha256(full file text)[:16].
  code ``<root>/.claude/.code_formats.json`` — keyed
       ``file_path::full_name``, entry carries ``backend`` +
       ``content_hash`` taken from the Weaviate row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # direct-script execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "vco_lib"  # type: ignore[assignment]

from vco_lib import deferral_emit, launcher_db_reader, python_exe
from vco_lib.deferral_report import DeferralEntry

#: The ONE condition id this module emits / resolves. Declared in
#: ``vco_lib/deferral_conditions.toml`` (class action_required,
#: paired-resolution) — the completeness test source-scans this constant.
CONDITION_ID = "kg_summaries_degraded"

#: The ladder's top tier — what "regenerated" means for the recheck. It is
#: the first rung ``select_backend`` tries with the latch cleared.
PREFERRED_TIER = "cli"

#: Prefix of the ONE summary line ``summary-recheck`` prints. MUST MATCH
#: ``launcher/src-tauri/src/commands/kg_summary.rs`` — ``RECHECK_LINE_PREFIX``
#: and ``parse_recheck_summary_line``, which splits on the separators
#: :func:`format_recheck_summary_line` writes.
RECHECK_LINE_PREFIX = "[summary-health] recheck:"

#: The optional trailing segment, and the substring the Rust parser tests for
#: (``rest.contains("code leg SKIPPED")``) before splitting the counts off at
#: ``"; code leg"``. Kept as a constant so the parity test compares values,
#: not prose.
RECHECK_CODE_LEG_SKIPPED = "; code leg SKIPPED (unresolved project name)"

_ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_SCRIPTS = _ORCHESTRATOR_ROOT / "templates" / "scripts"
if str(_TEMPLATES_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_TEMPLATES_SCRIPTS))
# Path-imported template script (runtime sys.path insert three lines
# above; pyright cannot follow a dynamic insert — sanctioned single-line
# ignore per pyrightconfig.json's own contract).
import summary_backends as _sb  # noqa: E402  # pyright: ignore[reportMissingImports]

KG_SIDECAR_RELPATH = Path("knowledge") / ".node_formats.json"
CODE_SIDECAR_RELPATH = Path(".claude") / ".code_formats.json"

_log = print


def _kg_content_hash(text: str) -> str:
    """sha256(full text)[:16] — the KG sidecar's own hash scheme.

    MIRROR of ``generate-kg-summary.py::content_hash`` (cross-language
    rule tier C): the generator is a stdlib-only template script that
    cannot import vco_lib, and this module must not depend on loading it
    by path just for one hash line. Pinned by
    ``tests/test_v0296_summary_quota_breaker.py`` against the real
    function — if either side changes, the pin fails.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────
# Pending-set scan
# ──────────────────────────────────────────────────────────────────────
@dataclass
class PendingSet:
    """Exactly what a recheck regenerates. Lists are sorted rel paths /
    entry keys, so scans are deterministic and comparable."""

    #: KG nodes whose sidecar names a non-preferred backend AND whose
    #: content hash is CURRENT — the frozen rows ordinary runs skip.
    kg_stale: list = field(default_factory=list)
    #: KG nodes with no usable summary entry at all.
    kg_missing: list = field(default_factory=list)
    #: Code sidecar entry keys naming a non-preferred backend.
    code_stale: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.kg_stale) + len(self.kg_missing) + len(self.code_stale)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_pending(project_root: Path, *, preferred: str = PREFERRED_TIER) -> PendingSet:
    """Derive the degradation-pending set from the sidecars (no ledger).

    Reads ONLY the two sidecar files + the knowledge tree; never touches
    Weaviate, so it is safe to run anywhere (including the
    ``note-degradation`` child, which must stay cheap).
    """
    project_root = Path(project_root).resolve()
    pending = PendingSet()
    knowledge_dir = project_root / "knowledge"
    if knowledge_dir.is_dir():
        formats = _load_json(knowledge_dir / KG_SIDECAR_RELPATH.name)
        for node_path in sorted(knowledge_dir.rglob("*.md")):
            rel = str(node_path.relative_to(project_root))
            entry = formats.get(rel)
            if not isinstance(entry, dict) or not (
                entry.get("summary") or entry.get("description")
            ):
                pending.kg_missing.append(rel)
                continue
            if entry.get("backend") == preferred:
                continue
            try:
                current = _kg_content_hash(
                    node_path.read_text(encoding="utf-8"))
            except OSError:
                continue  # unreadable now; the ordinary run will surface it
            if entry.get("content_hash") == current:
                pending.kg_stale.append(rel)
    code_formats = _load_json(project_root / CODE_SIDECAR_RELPATH)
    for key, entry in code_formats.items():
        if isinstance(entry, dict) and entry.get("backend") != preferred:
            pending.code_stale.append(key)
    pending.code_stale.sort()
    return pending


# ──────────────────────────────────────────────────────────────────────
# Degradation notice (once per event; last-write-wins per condition_id)
# ──────────────────────────────────────────────────────────────────────
def note_degradation(project_root: Path, *, tier: str, reason: str) -> bool:
    """Emit/refresh the ONE ``kg_summaries_degraded`` ledger entry.

    Called by the breaker trip seam (detached child) on a FRESH quota or
    trust latch, and safe to re-run by hand. Re-notifying REPLACES the
    entry (the ledger's per-cid last-write-wins), which is what makes it
    once-per-event rather than once-per-attempt.
    """
    project_root = Path(project_root).resolve()
    pending = scan_pending(project_root, preferred=tier)
    if reason == "trust":
        flavour = (
            "the headless `claude -p` summary call was refused because the "
            "workspace has not been trusted (terminal for this tier — "
            "re-accept the trust dialog in that project, then recheck)"
        )
    else:
        flavour = (
            f"token/budget exhaustion on the '{tier}' backend (the 5 h or "
            f"one-week account window)"
        )
    detected = (
        f"Preferred summary backend '{tier}' opened its circuit breaker on a "
        f"{reason} failure: {flavour}. Until it returns, summaries generate on "
        f"a fallback tier (or not at all when no fallback answers). Pending "
        f"regeneration, derived from the sidecars' recorded backends: "
        f"{len(pending.kg_stale)} KG node(s) on a fallback backend, "
        f"{len(pending.kg_missing)} KG node(s) with no summary, "
        f"{len(pending.code_stale)} code entit(y/ies) on a fallback backend "
        f"— {pending.total} in total."
    )
    why = (
        "New and changed nodes resume on the preferred tier automatically "
        "when the breaker's cooldown expires (default 18 000 s = the 5 h "
        "re-check; VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN), but rows already "
        "summarized by a fallback tier are hash-frozen — ordinary runs "
        "correctly skip them — so they regenerate only via the recheck "
        "command below (also the GUI 'Recheck summary backend now' action)."
    )
    entry = DeferralEntry(
        condition_id=CONDITION_ID,
        title=f"KG/code summaries degraded (backend '{tier}' breaker-open: {reason})",
        detected=detected,
        why_deferred=why,
        command_to_apply=(
            f"python -m vco_lib.summary_health summary-recheck "
            f"--project-root {project_root}"
        ),
        severity="warning",
    )
    return deferral_emit.emit(project_root, entry)


# ──────────────────────────────────────────────────────────────────────
# Backfill — regenerate EXACTLY the pending set
# ──────────────────────────────────────────────────────────────────────
@dataclass
class BackfillResult:
    spawned: int = 0
    failures: int = 0
    code_leg_skipped: bool = False


def _resolve_script(project_root: Path, name: str) -> "Path | None":
    """Prefer the project's installed copy, fall back to the template.

    The ``_resolve_analyzer`` pattern (codegraph_resync): a project's
    ``.claude/scripts/`` copy is the one its hooks run; the template is
    the orchestrator root's own.
    """
    for candidate in (
        project_root / ".claude" / "scripts" / name,
        _TEMPLATES_SCRIPTS / name,
    ):
        if candidate.is_file():
            return candidate
    return None


def _resolve_project_name(project_root: Path) -> "str | None":
    """The registered project name for *project_root*, from launcher.db.

    POSITIVE resolution only: the code-summary generator's GC prunes
    sidecar entries matching no live canonical row, and it derives those
    rows from the collection prefix this name selects — an unresolved
    name must therefore SKIP the code leg, never guess one.
    """
    try:
        refs = launcher_db_reader.list_registered_projects() or []
        for ref in refs:
            try:
                if Path(ref.folder).resolve() == project_root:
                    return ref.name
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — resolution is best-effort
        return None
    return None


def _default_spawn(argv: list, *, cwd: str, env: dict) -> int:
    """Run one generator child synchronously; return its exit code."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True,
        )
        return result.returncode
    except OSError as exc:
        _log(f"[summary-health] spawn failed ({argv[0]}): {exc}")
        return 1


def _child_env(project_root: Path) -> dict:
    return {
        **os.environ,
        "KG_PROJECT_ROOT": str(project_root),
        "VCT_ORCHESTRATOR_ROOT": str(_ORCHESTRATOR_ROOT),
    }


def backfill(
    project_root: Path,
    pending: PendingSet,
    *,
    spawn=None,
    project_name: "str | None" = None,
) -> BackfillResult:
    """Regenerate EXACTLY *pending* through the (now-preferred) tier.

    KG leg: one ``generate-kg-summary.py <file> --force`` per pending
    node — the generator's own per-node invocation shape, forced because
    the stale rows are precisely the ones whose hash gate would skip
    them. Soft-fail per node; a failure leaves the row pending for the
    next recheck.

    Code leg: delete EXACTLY the stale-backend keys from the sidecar
    (atomic write), then run the generator once — deleted keys read as
    missing and regenerate; hash-drifted rows regenerate by the
    generator's own staleness rules. Gated on POSITIVE project-name
    resolution (see ``_resolve_project_name``): with no resolvable name
    the generator's GC could prune the sidecar against the wrong
    (empty) prefix, so the leg is skipped with a log line instead.
    """
    project_root = Path(project_root).resolve()
    if spawn is None:
        spawn = _default_spawn
    result = BackfillResult()

    python = python_exe.resolve_or_current() or sys.executable
    kg_script = _resolve_script(project_root, "generate-kg-summary.py")
    if kg_script is None:
        if pending.kg_stale or pending.kg_missing:
            _log("[summary-health] no generate-kg-summary.py found — "
                 "KG leg skipped")
    else:
        env = _child_env(project_root)
        for rel in [*pending.kg_stale, *pending.kg_missing]:
            rc = spawn(
                [str(python), str(kg_script), str(project_root / rel), "--force"],
                cwd=str(project_root), env=env,
            )
            result.spawned += 1
            if rc != 0:
                result.failures += 1

    if pending.code_stale:
        name = project_name if project_name is not None else \
            _resolve_project_name(project_root)
        code_script = _resolve_script(project_root, "generate-code-summary.py")
        if name is None or code_script is None:
            result.code_leg_skipped = True
            _log(
                "[summary-health] code leg skipped — "
                + ("no generate-code-summary.py found"
                   if code_script is None else
                   "project not positively resolved in launcher.db "
                   "(refusing to run the generator against a guessed "
                   "prefix: its GC would prune the sidecar)")
            )
        else:
            from vco_lib.atomic import atomic_write_text

            sidecar_path = project_root / CODE_SIDECAR_RELPATH
            formats = _load_json(sidecar_path)
            for key in pending.code_stale:
                formats.pop(key, None)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                sidecar_path,
                json.dumps(formats, indent=2, ensure_ascii=False) + "\n",
            )
            rc = spawn(
                [str(python), str(code_script),
                 "--project", name, "--project-root", str(project_root)],
                cwd=str(project_root), env=_child_env(project_root),
            )
            result.spawned += 1
            if rc != 0:
                result.failures += 1
    return result


# ──────────────────────────────────────────────────────────────────────
# Recheck — the GUI / CLI exit
# ──────────────────────────────────────────────────────────────────────
@dataclass
class RecheckResult:
    pending_before: int
    pending_after: int
    spawned: int
    failures: int
    code_leg_skipped: bool


def format_recheck_summary_line(result: "RecheckResult") -> str:
    """Render the ONE machine-read line ``summary-recheck`` prints.

    v0.2.96 (duplication register D-2): the format lived inline in
    :func:`main`'s ``print(...)`` while the launcher parsed it in
    ``kg_summary.rs::parse_recheck_summary_line`` — a cross-language mirror
    with no "must match" comment on either side and no parity test (the Rust
    tests HAND-WROTE the line they parse, so the two could drift with every
    test green). Pulling the format into a named function makes it something
    a parity test can call, which
    ``tests/test_v0296_lane_python_core.py::SummaryRecheckLineGrammarParity``
    does: it renders a line from a known result and drives the Rust parser's
    own splitting rules over it.

    The separators are load-bearing, not cosmetic. The parser splits the
    counts on ``';'`` and each half on ``','``, then reads the LEADING
    integer of each segment — so segment ORDER and the ``"; "`` / ``", "``
    separators are the contract, while the trailing prose in each segment
    ("pending before", "generator run(s)") is free text.
    """
    line = (
        f"{RECHECK_LINE_PREFIX} {result.pending_before} pending "
        f"before, {result.pending_after} after; {result.spawned} "
        f"generator run(s), {result.failures} failure(s)"
    )
    if result.code_leg_skipped:
        line += RECHECK_CODE_LEG_SKIPPED
    return line


def summary_recheck(project_root: Path, *, spawn=None) -> RecheckResult:
    """Clear the breaker, regenerate the pending set, resolve the entry.

    Covers tokens returning BEFORE the 5 h cooldown (the owner's GUI
    "Recheck summary backend now" action invokes this). Selection re-runs
    inside each generator child with the latch cleared; if the tier is
    STILL quota-dead the children fall to the fallback again, the
    post-scan finds the same pending set, and the ledger entry stays —
    which is correct, because the degradation is still real. Idempotent:
    an empty pending set performs no spawns and only re-resolves.
    """
    project_root = Path(project_root).resolve()
    _sb.reset_breaker(persisted=True)
    pending = scan_pending(project_root)
    if pending.total == 0:
        _settle(project_root, "nothing pending")
        return RecheckResult(0, 0, 0, 0, False)
    backfill_result = backfill(project_root, pending, spawn=spawn)
    after = scan_pending(project_root)
    if after.total == 0:
        _settle(project_root, f"regenerated {pending.total} pending item(s)")
    return RecheckResult(
        pending_before=pending.total,
        pending_after=after.total,
        spawned=backfill_result.spawned,
        failures=backfill_result.failures,
        code_leg_skipped=backfill_result.code_leg_skipped,
    )


def _settle(project_root: Path, detail: str) -> None:
    """Paired resolution of the degradation entry (the registry contract)."""
    removed = deferral_emit.resolve_conditions(project_root, [CONDITION_ID])
    if removed:
        deferral_emit.record_auto_resolution(
            project_root, CONDITION_ID, "summary-recheck", detail,
        )
        _log(f"[summary-health] resolved {CONDITION_ID} — {detail}")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def _default_project_root() -> Path:
    env_root = os.environ.get("KG_PROJECT_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return _ORCHESTRATOR_ROOT


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.summary_health",
        description="KG/code summary degradation: notice, pending scan, "
                    "backfill, recheck (v0.2.96 WP-7).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    recheck = sub.add_parser(
        "summary-recheck",
        help="Clear the summary-backend breaker and regenerate exactly the "
             "pending (fallback-tier / missing) summaries.",
    )
    recheck.add_argument("--project-root", default=None,
                         help="Project root (default: $KG_PROJECT_ROOT, else "
                              "the orchestrator root)")

    note = sub.add_parser(
        "note-degradation",
        help="Emit/refresh the kg_summaries_degraded ledger entry (used by "
             "the breaker trip seam; safe to run by hand).",
    )
    note.add_argument("--project-root", required=True)
    note.add_argument("--tier", required=True,
                      help="The tier that breaker-opened (e.g. 'cli')")
    note.add_argument("--reason", required=True,
                      help="Breaker reason: 'quota' or 'trust'")

    args = parser.parse_args(argv)
    if args.command == "summary-recheck":
        project_root = Path(
            args.project_root or _default_project_root()).resolve()
        result = summary_recheck(project_root)
        # ONE home for the format (v0.2.96 D-2) — the launcher parses this
        # exact line; see `format_recheck_summary_line`'s docstring.
        print(format_recheck_summary_line(result))
        return 0

    project_root = Path(args.project_root).resolve()
    ok = note_degradation(
        project_root, tier=args.tier, reason=args.reason)
    print(f"[summary-health] degradation notice {'emitted' if ok else 'FAILED'}"
          f" for {project_root} ({args.reason}/{args.tier})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
