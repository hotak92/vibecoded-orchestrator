# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What the bundle engine does when it KEEPS an installed file instead of
updating it: the bookkeeping, and the deferral that tells the user why.

Extracted from ``vco_lib.project_init`` in v0.2.96, following the
``vco_lib.bundle_skip_deferral`` / ``vco_lib.migrate_deferral`` precedent —
that module is under a line-count ratchet which (correctly) refuses further
growth, and this is a self-contained unit with no dependency on the
installer's mutable state beyond the collections its caller hands it.

Two functions, one concern:

* :func:`record_preserve` — the ONE home for preserve bookkeeping.
  ``install_project_bundle`` reaches a preserve outcome from two places (the
  ``elif action == "preserve"`` branch and the adopt branch's
  backup-FAILURE fallback) and used to carry verbatim twin blocks whose own
  comment asked them to "stay identical" (duplication register D-5). A
  comment asking two copies to stay identical is a request for extraction.
* :func:`emit_user_modified_deferral` — the
  ``bundle_user_modified_preserved`` ledger entry.

The v0.2.96 ship-gate finding this extraction carries (F-B1): since v0.2.84
the ONLY way a code-surface file reaches that deferral on an update is the
adoption backup failing to write, so the entry names that cause. See the
emitter's docstring for the full argument.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from vco_lib.project_init import _BundleFileOp

__all__ = ["record_preserve", "emit_user_modified_deferral"]


def record_preserve(
    op: "_BundleFileOp",
    shipped_hash: str,
    *,
    manifest: dict,
    new_files: dict,
    new_preserved: dict,
    user_modified_paths: list,
    knowledge_preserved_paths: list,
) -> None:
    """Book a ``preserve`` outcome: route the path, keep the manifest
    baseline, and record the schema-v2 ``preserved`` row.

    Three effects, all through the caller's own collections (the caller owns
    the run's state; this function owns the RULE):

    * **NEW-1 routing** — a user-owned ``knowledge/**`` node goes to the
      SILENT ``knowledge_preserved_paths`` list (no deferral: its divergence
      is the expected steady state, and ``--force`` is a deliberate no-op for
      it per B-1). Every other dest goes to ``user_modified_paths``, which
      feeds the ``bundle_user_modified_preserved`` deferral.
    * **manifest baseline** — the PRIOR entry is carried forward unchanged
      (never the new shipped hash) so the next update still recognises the
      same baseline.
    * **schema-v2 ``preserved`` row** — records what VCO would have written,
      so a later audit can answer "did VCO ever try to install file X here?".

    The knowledge/code routing uses ``project_init._is_knowledge_dest`` (its
    single home — separator-normalised; never inline the prefix test, the
    v0.2.81 lesson). Imported lazily to keep the module-load graph acyclic:
    ``project_init`` imports THIS module.
    """
    from vco_lib.project_init import _is_knowledge_dest

    if _is_knowledge_dest(op.dest_rel):
        knowledge_preserved_paths.append(op.dest_rel)
        reason = "knowledge-preserve"
    else:
        user_modified_paths.append(op.dest_rel)
        reason = "preserve"
    existing = manifest.get("files", {}).get(op.dest_rel)
    if existing is not None:
        new_files[op.dest_rel] = existing
    new_preserved[op.dest_rel] = {
        "shipped_sha256": shipped_hash,
        "preserved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "shipped_source": op.source_rel,
        "reason": reason,
    }


def emit_user_modified_deferral(
    folder: Path,
    modified_files: list[str],
    orchestrator_root: Path,
    backup_failures: Optional[list[tuple[str, str]]] = None,
) -> None:
    """Emit ``bundle_user_modified_preserved``: one deferral entry per project
    listing every file that diverged from the prior-shipped hash during an
    ``--update`` run and was kept on disk rather than updated.

    WHY the file was kept (v0.2.96, ship-gate F-B1). Until v0.2.84 the answer
    was a POLICY — "default to safety: a divergent file is preserved". That
    policy is gone: ``_file_action`` classifies a divergent CODE-surface file
    as ``adopt`` (back the current bytes up, then write the shipped ones), and
    the only ``preserve`` it still returns is for user-owned ``knowledge/**``,
    which NEW-1 routes to the silent list and never into this entry. So on an
    update the one remaining way a file reaches this entry is the adopt
    branch's fallback: **the adoption backup could not be written** (disk
    full, permissions, a symlink redirect under ``.claude/backups/``). That is
    the actionable cause, it is what the text states, and it is why
    ``--force`` — which overwrites with NO backup, on the very machine whose
    backup writes just failed — is presented as the destructive last resort it
    is rather than as the headline remedy.

    :param backup_failures: ``(dest_rel, error)`` for each file whose adoption
        backup raised. Empty/None is tolerated (a caller that did not collect
        them): the entry then states the divergence without claiming a cause
        it cannot prove.

    The user has four options, in the order the command block lists them:

    1. Fix the backup destination and re-run the ORDINARY update — the files
       are then adopted with their backups captured, and this entry
       self-clears through the ordinary bundle reconciliation.
    2. Keep the customizations and dismiss the deferral via
       ``dismiss-deferral``.
    3. Inspect the differences per file first.
    4. Last resort: ``--update --force``, which takes the shipped versions
       with no backup at all.

    Per-project grouping (single entry, file list inside) is intentional —
    one entry per file would generate dozens of deferrals that all duplicate
    the same actionable command.
    """
    if not modified_files:
        return
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de
    # Lazy, to keep the module-load graph acyclic (`project_init` imports this
    # module at load time). `_format_file_list_md` stays in `project_init`
    # because it serves several emitters and the bundle tests reference it by
    # name there; `_ADOPT_BACKUPS_REL` is the adoption layout's single home.
    from vco_lib.project_init import _ADOPT_BACKUPS_REL, _format_file_list_md
    from vco_lib.paths import to_posix_rel

    files_md = _format_file_list_md(sorted(modified_files))
    # Item 4 (Gap 7, 2026-05-13): emit $VCT_ORCHESTRATOR_ROOT instead of a
    # baked literal path so the command stays portable across machines and
    # surviving orchestrator-clone relocations. The env var is set by
    # `.claude/env` (sourced by every VCO-installed project's tooling); if
    # the user runs from a shell without it, the prose tells them how to
    # set it manually.
    # v0.2.23 B5 (D18 short-term): when a preserved file is likely
    # CLAUDE.md (the common case — the user adds project-specific Dev
    # Constraints / KG conventions to it), the highest-leverage action is
    # NOT "diff manually" but "ask Claude to merge in this project session".
    # Claude has the orchestrator's intent (this CLAUDE.md text) AND the
    # user's project context loaded — it can produce a merged file in
    # seconds that preserves both.
    has_claude_md = any(
        Path(p).name.lower() in ("claude.md", "claude.local.md")
        for p in modified_files
    )
    claude_merge_hint = (
        "# RECOMMENDED for CLAUDE.md / CLAUDE.local.md (the common case):\n"
        "# open this folder in Claude Code and ask:\n"
        "#   \"Merge the orchestrator's shipped CLAUDE.md against my local\n"
        "#    one. Preserve project-specific Dev Constraints / KG conventions\n"
        "#    but adopt new orchestrator-shipped guidance. Show me the diff\n"
        "#    before writing.\"\n"
        "# Claude reads $VCT_ORCHESTRATOR_ROOT/CLAUDE.md and your local one,\n"
        "# proposes a 3-way merge, and writes the result with your approval.\n"
        "#\n"
        if has_claude_md else ""
    )
    # v0.2.96 F-B1: the backup-failure cause, rendered for the user. `errors_md`
    # is empty ONLY when the caller passed no failures — see the docstring for
    # why that case states the divergence without inventing a cause.
    failures = [
        (path, err) for path, err in (backup_failures or [])
        if path in set(modified_files)
    ]
    errors_md = "\n".join(f"  - `{p}` — {e}" for p, e in sorted(failures))
    backup_dir_rel = to_posix_rel(str(_ADOPT_BACKUPS_REL))
    cmd = (
        f"{claude_merge_hint}"
        f"# 1. RECOMMENDED — fix the backup destination, then re-run the\n"
        f"#    ORDINARY update. Each file is then adopted WITH its backup\n"
        f"#    captured under {backup_dir_rel}/<timestamp>/ and this entry\n"
        f"#    clears itself. Check free space, the write permission on\n"
        f"#    {folder}/{backup_dir_rel}, and whether any ancestor of it is a\n"
        f"#    symlink (VCO refuses to write through one):\n"
        f"df -h {str(folder)!r} && ls -ld {str(folder / _ADOPT_BACKUPS_REL)!r}\n"
        f"# 2. OR keep your customizations and dismiss this deferral:\n"
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder {str(folder)!r} "
        f"--condition-id bundle_user_modified_preserved\n"
        f"# 3. OR inspect the differences per file before deciding:\n"
        f"#   diff -u <orchestrator>/<source-rel> {folder}/<dest-rel>\n"
        f"#   (run from a shell where `.claude/env` has been sourced, or\n"
        f"#    prepend VCT_ORCHESTRATOR_ROOT=/path/to/vibecoded-orchestrator)\n"
        f"# 4. LAST RESORT — take the shipped versions with NO backup. This\n"
        f"#    DESTROYS your local edits, on the machine whose backup writes\n"
        f"#    just failed. Copy the files aside yourself first:\n"
        f"python -m vco_lib.project_init install-bundle "
        f"--folder {str(folder)!r} --orchestrator-root "
        f"\"$VCT_ORCHESTRATOR_ROOT\" --update --force --json"
    )
    entry = DeferralEntry(
        condition_id="bundle_user_modified_preserved",
        title="Bundle files kept on disk — their adoption backup could not be written",
        detected=(
            f"During an `install-bundle --update` run, "
            f"{len(modified_files)} file(s) under the project's `.claude/` "
            f"tree differed from the version this orchestrator originally "
            f"shipped. VCO updates such a file by backing the current bytes "
            f"up first; here the BACKUP WRITE FAILED, so the update was "
            f"abandoned for these files and your on-disk copies were left "
            f"untouched:\n"
            f"{files_md}"
            + (
                f"\n\nThe backup write failed with:\n{errors_md}"
                if errors_md else ""
            )
        ),
        why_deferred=(
            "VCO never replaces divergent bytes without first capturing a "
            f"copy under `{backup_dir_rel}/<timestamp>/`. When that capture "
            "cannot be written — no free space, no write permission, or a "
            "symlink under the backup path — the safe outcome is to leave "
            "the file alone and tell you, which is this entry. Fix the "
            "backup destination and re-run the ordinary update: the files "
            "are then adopted with their backups and this clears itself. "
            "`--force` is NOT the fix here — it overwrites with no backup "
            "at all, on the machine that just proved it cannot take one."
        ),
        command_to_apply=cmd,
        severity="info",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)
