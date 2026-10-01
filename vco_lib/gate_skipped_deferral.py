# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE writer of the ``gate_skipped_no_project_id`` deferral entry.

v0.2.49 SB1 gave the Phase-8 WRITE gate's empty-``VCT_PROJECT_ID`` branch a
user-facing surface: an ``UPDATE_DEFERRED.md`` entry naming the remediation.
Two places reach that branch — the weaviate-kg MCP server
(``store_knowledge_node``) and the write hooks' routing library
(``templates/hooks/_lib/route-touched-path.{sh,ps1}``) — and until v0.2.100
the hooks wrote their copy with a bare shell/PowerShell APPEND: no lock, a
grep-then-append race, and a raw markdown block outside the deferral writer's
contract. Every other writer of that file reads, merges and writes it under
``<folder>/.claude/context/.update-deferred.lock``
(:func:`vco_lib.deferral_emit.locked_report`), so an unlocked append racing
``install.py``'s finalize could be overwritten or could overwrite (owner rule:
writers append/merge, never clobber).

So the entry is built HERE, once, for both surfaces, and written through the
locked emitter. The hooks call ``python -m vco_lib.gate_skipped_deferral``
(A>B>C, A-leg: a once-per-session, write-triggered path where an interpreter
start is irrelevant); the MCP server imports :func:`emit_gate_skipped`.

CLI::

    python -m vco_lib.gate_skipped_deferral --folder <project> \\
        --collection <class> [--surface hook|mcp]

Exit 0 when the entry was written (or refreshed), 1 when it could not be —
with the reason on stderr. stdout stays empty.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

CONDITION_ID = "gate_skipped_no_project_id"

_COMMAND_TO_APPLY = (
    "# Option A — orchestrator-root install / update:\n"
    "python install.py --update\n"
    "\n"
    "# Option B — per-project (pre-v0.2.49 install): re-register the\n"
    "# project via Launcher GUI → Projects → Identity tab. The\n"
    "# launcher's apply_project_env pass seeds VCT_PROJECT_ID\n"
    "# into <project>/.claude/env from launcher.db."
)


def build_entry(collection: str, surface: str = "hook"):
    """The deferral entry for ``surface`` (``"hook"`` or ``"mcp"``)."""
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    if surface == "mcp":
        title = "Phase-8 access-matrix gate skipped (VCT_PROJECT_ID missing from MCP env)"
        detected = (
            "The MCP server reached store_knowledge_node with no "
            "VCT_PROJECT_ID env. The Phase-8 WRITE gate cannot "
            "identify this project against the hub's access matrix, "
            "so writes are proceeding via the silent-allow path. "
            "The write itself was permitted; this entry records the "
            "remediation so future writes go through the gate "
            f"properly. (target collection: {collection})"
        )
        who = "the MCP server"
    else:
        title = "Phase-8 access-matrix gate skipped (VCT_PROJECT_ID missing from hook env)"
        detected = (
            "A VCO write hook reached the Phase-8 WRITE gate with no "
            "VCT_PROJECT_ID. The gate cannot identify this project against "
            "the hub access matrix, so the write was permitted via the "
            f"silent-allow path. Target collection: {collection}"
        )
        who = "the hook"
    return DeferralEntry(
        condition_id=CONDITION_ID,
        title=title,
        detected=detected,
        why_deferred=(
            "Seeding VCT_PROJECT_ID requires either an orchestrator "
            "install run (which queries launcher.db for the "
            "project's UUID) or a Launcher GUI project "
            f"re-registration. Both are user-initiated; {who} "
            "cannot self-heal."
        ),
        command_to_apply=_COMMAND_TO_APPLY,
        severity="warning",
    )


def emit_gate_skipped(folder: Path, collection: str, surface: str = "hook") -> bool:
    """Upsert the entry into ``folder``'s ledger under the shared lock.

    Returns whether it was written. Raises nothing the caller must handle
    beyond what :func:`vco_lib.deferral_emit.emit` reports (it is
    best-effort and returns ``False`` on an I/O failure).
    """
    from vco_lib.deferral_emit import emit  # noqa: PLC0415

    # keep_first_detected: a long-standing condition keeps the time it was
    # FIRST seen, the way every repeated emitter of one condition does.
    return emit(Path(folder), build_entry(collection, surface), keep_first_detected=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.gate_skipped_deferral")
    parser.add_argument("--folder", required=True, help="project root")
    parser.add_argument("--collection", default="", help="the write's target collection")
    parser.add_argument("--surface", choices=("hook", "mcp"), default="hook")
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    if not folder.is_dir():
        sys.stderr.write(f"gate_skipped_deferral: {folder} is not a directory; nothing written.\n")
        return 1
    try:
        ok = emit_gate_skipped(folder, args.collection, args.surface)
    except Exception as exc:  # noqa: BLE001 — a hook caller must get a reason, not a traceback
        sys.stderr.write(f"gate_skipped_deferral: could not write the deferral entry: {exc}\n")
        return 1
    if not ok:
        sys.stderr.write(
            f"gate_skipped_deferral: the deferral entry could not be written to "
            f"{folder / '.claude' / 'context' / 'UPDATE_DEFERRED.md'}.\n"
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
