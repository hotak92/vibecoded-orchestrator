# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""INFO deferral emitters for a regenerated-sidecar schema bump (v0.2.73 M2;
extracted from ``vco_lib/project_init.py`` in v0.2.103 — the delivery ratchet
``tests/test_v0292_wp4_delivery_and_promises.py`` forbids growing that file).

When ``knowledge/.node_formats.json`` or ``.claude/.code_formats.json`` is at
an older schema than this orchestrator's canonical version and the inline
regeneration could not run (generator/backend unavailable), the existing cache
is left untouched and one of these emitters records the owed regeneration as
an INFO deferral with the exact recovery commands.

v0.2.103 (batch review L-1): every printed command names the RESOLVED
orchestrator-venv interpreter (``vco_lib.python_exe``) — a bare PATH
``python`` has no ``vco_lib``, so pasting it reproduced exactly the
broken-venv defect the resolver exists to fix.
"""

from __future__ import annotations

import sys
from pathlib import Path


def write_formats_migration_deferral(
    folder: Path,
    canonical: int,
    detail: str,
    *,
    condition_id: str,
    title: str,
    sidecar_desc: str,
    derived_from: str,
    regen_cmd: str,
    log_tag: str,
) -> None:
    """INFO deferral: a regenerated-sidecar schema bumped but the migration
    action couldn't run inline. The existing cache is untouched; this tells
    the project's Claude how to regenerate manually. Generic home (v0.2.73
    M2) — per-artifact wrappers below supply the artifact-specific text."""
    try:
        from vco_lib.deferral_report import DeferralEntry
        from vco_lib import deferral_emit as _de
        from vco_lib.python_exe import resolve_or_current
        # v0.2.103 (review L-1): the printed dismiss command runs
        # `-m vco_lib.project_init`, which a bare PATH `python` cannot
        # import — print the RESOLVED interpreter instead.
        _py = resolve_or_current()
        # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
        _de.emit(folder, DeferralEntry(
            condition_id=condition_id,
            severity="info",
            title=title,
            detected=(
                f"`{sidecar_desc}` is at an older schema than "
                f"this orchestrator's canonical v{canonical}. It is a "
                f"regenerated cache (derived from {derived_from}), so it was "
                f"NOT overwritten. Inline regeneration did not run: {detail}."
            ),
            why_deferred=(
                "Regenerated-data files are re-derived, never blind-overwritten. "
                "When the generator backend is available, re-running it rewrites "
                "the cache in the new schema with no data loss."
            ),
            command_to_apply=(
                f"{regen_cmd}\n"
                f"# Then dismiss:\n"
                f"{_py} -m vco_lib.project_init dismiss-deferral --folder {str(folder)!r} "
                f"--condition-id {condition_id}"
            ),
        ))
    except Exception as exc:  # never block the update flow
        print(f"[{log_tag}] deferral write failed (non-fatal): {exc}", file=sys.stderr)


def write_node_formats_migration_deferral(folder: Path, canonical: int, detail: str) -> None:
    """INFO deferral `regenerated_data_schema_migration_pending`: the
    .node_formats schema bumped but regeneration couldn't run inline. Thin
    wrapper over the generic writer (v0.2.73 M2) — text unchanged."""
    # v0.2.103 (review L-1): a printed command is shipped code. Bare `python`
    # is exactly the defect the venv resolver exists for (a system python has
    # no `vco_lib`, so the pasted command dies at the generator's import), so
    # the printed interpreter is the RESOLVED one (the orchestrator venv via
    # the ONE resolver home). The command is the generator's bulk `--all
    # --force` invocation — the same one `_regenerate_node_formats` spawns.
    from vco_lib.python_exe import resolve_or_current

    py = resolve_or_current()
    write_formats_migration_deferral(
        folder, canonical, detail,
        condition_id="regenerated_data_schema_migration_pending",
        title="KG-summary cache schema bumped — regeneration pending",
        sidecar_desc="knowledge/.node_formats.json",
        derived_from="your KG nodes",
        regen_cmd=(
            "# Regenerate every node summary in the new schema:\n"
            f"{py} .claude/scripts/generate-kg-summary.py --all --force"
        ),
        log_tag="node-formats",
    )


def write_code_formats_migration_deferral(folder: Path, canonical: int, detail: str) -> None:
    """INFO deferral `code_formats_schema_migration_pending`: the
    .code_formats schema bumped but the keep-regenerated action (delete +
    let the generator rebuild) couldn't complete."""
    from vco_lib.python_exe import resolve_or_current

    py = resolve_or_current()
    write_formats_migration_deferral(
        folder, canonical, detail,
        condition_id="code_formats_schema_migration_pending",
        title="Code-summary sidecar schema bumped — regeneration pending",
        sidecar_desc=".claude/.code_formats.json",
        derived_from="your project's code-graph rows",
        regen_cmd=(
            "# Delete the old-schema sidecar and regenerate:\n"
            "rm .claude/.code_formats.json\n"
            f"{py} .claude/scripts/generate-code-summary.py --project <name> --force"
        ),
        log_tag="code-formats",
    )
