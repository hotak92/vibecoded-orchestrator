# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``bundle_skipped_existing_files`` deferral emitter.

Extracted from ``vco_lib.project_init`` in v0.2.92, following the
``vco_lib.migrate_deferral`` precedent: a deferral emitter is a
self-contained unit of prose + one command, with no dependency on the
installer's mutable state, and the installer module is under a line-count
ratchet that (correctly) refuses further growth.

ONE caller: ``project_init.install_project_bundle``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["emit_skipped_existing_deferral"]


def emit_skipped_existing_deferral(
    folder: Path, skipped_files: list[str], orchestrator_root: Path,
) -> None:
    """Emit `bundle_skipped_existing_files`: one deferral entry per project
    listing pre-existing files that the first-install path SKIPPED because
    their content differs from the orchestrator's shipped version.

    Why: a Claude Code session opening this folder needs to know the bundle
    install was incomplete — the user may have a stale custom hook that
    will silently miss new orchestrator-side improvements until they
    explicitly run `--update --force`.

    Severity is `info` (not `warning`) — the project is functional, just
    not 100% in lockstep with the orchestrator's defaults.

    Per-project grouping (single entry, file list inside): one entry per
    file would be noisy and harder to action. The single entry's command
    fixes ALL of them in one go.
    """
    if not skipped_files:
        return
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de
    # Lazy, to keep the module-load graph acyclic: `project_init` imports THIS
    # module at load time. `_format_file_list_md` (the shared 100-path cap +
    # "... and N more" renderer) stays there because it serves several
    # emitters and is referenced by name in the bundle tests.
    from vco_lib.project_init import _format_file_list_md

    files_md = _format_file_list_md(sorted(skipped_files))
    # Item 4 (Gap 7, 2026-05-13): emit $VCT_ORCHESTRATOR_ROOT (set by
    # `.claude/env`) instead of a baked literal path so the command is
    # portable across machines / orchestrator clone relocations.
    # v0.2.92: `--update` (no `--force`) is the RECOMMENDED form and is
    # listed first. Since v0.2.84 D7/R2 an update ADOPTS a divergent file at a
    # VCO-shipped destination — backing the current bytes up under
    # `.claude/backups/bundle-adoptions/<ts>/` before writing the shipped
    # ones. `--force` short-circuits that to a plain overwrite with NO backup.
    # This entry used to print ONLY the `--force` form, i.e. the one variant
    # that discards the user's bytes unrecoverably.
    cmd = (
        f"# Run from a shell where `.claude/env` has been sourced, or\n"
        f"# prepend VCT_ORCHESTRATOR_ROOT=/path/to/vibecoded-orchestrator.\n"
        f"#\n"
        f"# RECOMMENDED — take the orchestrator's shipped versions, keeping a\n"
        f"# backup of your current bytes under\n"
        f"# .claude/backups/bundle-adoptions/<timestamp>/ :\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"\"$VCT_ORCHESTRATOR_ROOT\" --update --json\n"
        f"#\n"
        f"# Same thing WITHOUT a backup (your current bytes are discarded --\n"
        f"# only use this if you are certain you want none of them):\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"\"$VCT_ORCHESTRATOR_ROOT\" --update --force --json"
    )
    entry = DeferralEntry(
        condition_id="bundle_skipped_existing_files",
        title="Pre-existing files preserved during first-install",
        detected=(
            f"During the first-install of this project's bundle, "
            f"{len(skipped_files)} file(s) under `.claude/` and "
            f"`infrastructure/` already existed AND differed from the "
            f"orchestrator's shipped versions. They were preserved to "
            f"avoid overwriting user customizations:\n"
            f"{files_md}"
        ),
        why_deferred=(
            "These files already existed when the bundle was first "
            "installed and differ from the orchestrator's shipped "
            "versions, AND VCO could not prove they are stale VCO "
            "artifacts (they match no version VCO ever shipped, and they "
            "are not at a destination whose shipped version carries an "
            "invariant they violate). Files that WERE provably stale were "
            "adopted during the install, with a backup — see the "
            "shipped-file adoption notice. What is listed here is treated "
            "as your own work: nothing about it is broken, it simply will "
            "not track future orchestrator improvements. Use the command "
            "below if you would rather take VCO's defaults."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)
