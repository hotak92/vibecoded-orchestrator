# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 V47-B: symlinks under the install path are never touched.

This module centralizes VCO's "hands-off" rule for symlinks living under
any path that VCO would otherwise write to during install / update.

Hard rule (user decision 2026-06-03)
------------------------------------
If a path VCO is about to write to (or recurse-into for writes) is a
**symlink**, regardless of where it points, **VCO leaves it alone**:

- Does NOT ``unlink()`` the symlink.
- Does NOT ``mkdir()`` over it.
- Does NOT follow the symlink to its target.
- Does NOT recurse into a symlinked directory for further writes.

Instead, VCO writes its intended content to a sibling path with a
``.vco-new`` suffix and emits a structured deferral entry to
``UPDATE_DEFERRED.md`` so the user can reconcile manually.

Why ``lexists`` everywhere
--------------------------
``os.path.exists()`` follows symlinks: a dangling symlink returns
``False``, a symlink-to-existing-file returns ``True`` based on the
target's existence. That makes it the wrong gate for write-decisions —
we'd either overwrite the target (polluting whatever it points at) or
fail confusingly when the link dangles.

``os.path.lexists()`` returns ``True`` iff the path itself exists, no
follow-through. That's the correct gate for "is something already at
this dest?" decisions in write code paths.

Cross-platform
--------------
- Linux/macOS: ``os.path.islink`` handles regular symlinks.
- Windows: ``os.path.islink`` returns True for both NTFS symbolic links
  and junctions / reparse points (Python normalizes these). Behaviour
  is verified by Python's CPython tests on Win10+.

Test caveat: creating symlinks on Windows GitHub Actions runners
requires developer-mode OR running as administrator. Tests should
``pytest.skip`` cleanly when ``os.symlink`` raises ``OSError`` /
``NotImplementedError`` on Windows, not xfail-everywhere.

Mode-agnostic
-------------
This rule does NOT depend on the new ``adopt_project_mode`` from
v0.2.46 V47-G-stub. VCO never replaces a symlink under the install
path regardless of mode (per-user explicit decision: to accept VCO's
defaults over a symlink, the user manually deletes the symlink first).
The mode is read only for log-message differentiation (banner / event
label), never for behaviour gating.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vco_lib.deferral_report import DeferralReport


__all__ = [
    "is_symlink_blocking",
    "compute_vco_new_path",
    "emit_symlink_deferral",
    "emit_symlink_deferral_multi",
    "check_vco_new_collision",
    "SYMLINK_PRESERVED_CONDITION_ID",
    "VCO_NEW_SUFFIX",
    "VCO_NEW_COLLISION_CONDITION_ID",
]


# Stable condition_id for the V47-B-followup (post-adversarial L1) check:
# a .vco-new sibling from a PRIOR install run already exists at the path
# VCO would write to now. The caller may have hand-edited it between runs
# and we'd silently clobber that work without this guard.
VCO_NEW_COLLISION_CONDITION_ID = "vco_new_sibling_collision"


SYMLINK_PRESERVED_CONDITION_ID = "symlink_preserved_under_install_path"
"""Condition ID used for the ``UPDATE_DEFERRED.md`` entry per Gap B.

Stable string — referenced by tests, by the launcher's deferred-reader,
and by future ``--apply-deferred`` workflows. Do not rename without a
migration entry in CHANGELOG.md.
"""


VCO_NEW_SUFFIX = ".vco-new"
"""Suffix appended to the sibling path where VCO lands its intended
content when the target is a symlink. Kept distinct from ``.new`` (which
is the suffix the bundle-apply preserve-user-modified flow uses at
``install.py::_new_sibling_path``) so a future audit can grep precisely
for symlink-driven sibling-writes versus user-modification preserves.
"""


def is_symlink_blocking(dest: Path) -> bool:
    """Return True iff ``dest`` exists AND is a symlink.

    Uses ``os.path.islink`` so it correctly identifies:
      - Regular POSIX symlinks (Linux/macOS).
      - NTFS symbolic links AND junctions / reparse points (Windows).
      - Dangling symlinks (path itself is a symlink even when target is
        gone).

    Returns False for:
      - Non-existent paths.
      - Regular files, regular directories.

    The Path object is converted to a string via ``os.fspath`` so callers
    can pass either ``pathlib.Path`` or plain strings interchangeably.

    Args:
        dest: The path VCO would write to / recurse-into.

    Returns:
        True if VCO must STOP at this path; False otherwise.
    """
    try:
        return os.path.islink(os.fspath(dest))
    except (TypeError, ValueError, OSError):
        # Defensive: anything weird about the path (bad encoding,
        # un-decodable bytes, etc.) → treat as "not a symlink" so the
        # caller's existing logic runs. The lexists check at the caller
        # site will still cover the does-it-exist question.
        return False


def compute_vco_new_path(dest: Path) -> Path:
    """Return the sibling ``.vco-new`` path for ``dest``.

    The transformation is shape-preserving:
      - ``.claude/agents``       → ``.claude/agents.vco-new``
      - ``.claude/settings.json`` → ``.claude/settings.json.vco-new``
      - ``CLAUDE.md``            → ``CLAUDE.md.vco-new``
      - ``.env``                 → ``.env.vco-new``

    Both file targets and directory targets get the same shape — the
    caller decides whether to write a file or ``mkdir`` a directory at
    the returned path. (For directories: caller does
    ``vco_new.mkdir(parents=True, exist_ok=True)`` then writes contents
    inside it.)

    Unlike ``install.py::_new_sibling_path``, which inserts ``.new``
    BEFORE the file extension (``CLAUDE.md`` → ``CLAUDE.new.md``), this
    helper APPENDS ``.vco-new`` as a final suffix. Rationale:

    1. Symlink targets are commonly directories (e.g.,
       ``.claude/agents → ~/.claude/workflow/agents``); a directory has
       no extension to split around, so the ``insert-before-ext`` rule
       would degenerate to a trailing ``.new`` anyway.

    2. The append-suffix form makes the intent unambiguous when reading
       a directory listing: ``agents`` (the symlink) sits next to
       ``agents.vco-new`` (VCO's intended content). No mental
       extension-arithmetic needed.

    3. The distinct suffix (``.vco-new`` vs ``.new``) keeps the symlink
       reconciliation flow grep-separable from the
       user-modified-file-preserved flow.

    Args:
        dest: The blocked write target (file or directory).

    Returns:
        Sibling path with ``.vco-new`` appended.
    """
    return dest.with_name(dest.name + VCO_NEW_SUFFIX)


def emit_symlink_deferral(
    deferral: "DeferralReport",
    dest: Path,
    vco_new: Path,
    install_root: Path | None = None,
) -> None:
    """Append a ``symlink_preserved_under_install_path`` entry to the
    deferral report.

    The entry is informational (severity ``info``) — VCO did the right
    thing. The user needs to know:

      1. A symlink lives at ``dest`` and was left alone.
      2. The symlink points to ``<target>`` (if readable; symlinks to
         non-existent paths are still named).
      3. VCO's intended content was written to ``vco_new``.
      4. The exact commands to (a) accept VCO's defaults or (b) keep
         the existing symlink.

    The condition_id is a stable slug (``symlink_preserved_under_install_path``)
    so multiple symlink encounters within a single install collapse into
    one entry per (run, condition_id) pair. Future runs that re-encounter
    the same condition append a fresh ``detected_at`` timestamp via
    ``DeferralReport.add_entry`` (last-write-wins on condition_id, see
    ``deferral_report.py::DeferralReport.add_entry``).

    Args:
        deferral: The ``DeferralReport`` to append to. Caller owns the
            object's lifecycle; this function only mutates it.
        dest: The symlink path VCO refused to touch.
        vco_new: The sibling path where VCO landed its intended content.
        install_root: Optional install root for relative-path display.
            When provided, ``dest`` and ``vco_new`` are rendered relative
            to it (more readable). When None, absolute paths are shown.
    """
    # Lazy import to avoid circular dependency at module import time
    # (deferral_report.py is import-heavy; this module is imported by
    # install.py at top level).
    from vco_lib.deferral_report import DeferralEntry

    detected, command_to_apply = _render_symlink_pair(dest, vco_new, install_root)

    why_deferred = (
        "VCO never replaces or follows symlinks under the install path "
        "(hard rule, v0.2.46). To replace this symlink with VCO's defaults "
        "the user must delete the symlink first, then re-run install.py."
    )

    entry = DeferralEntry(
        condition_id=SYMLINK_PRESERVED_CONDITION_ID,
        title="Symlink preserved under install path",
        detected=detected,
        why_deferred=why_deferred,
        command_to_apply=command_to_apply,
        severity="info",
    )
    deferral.add_entry(entry)


def _display_path(p: Path, install_root: Path | None) -> str:
    """Render ``p`` relative to ``install_root`` when possible, else absolute."""
    if install_root is not None:
        try:
            return str(Path(p).relative_to(install_root))
        except ValueError:
            return str(p)
    return str(p)


def _render_symlink_pair(
    dest: Path, vco_new: Path, install_root: Path | None,
) -> tuple[str, str]:
    """Build the ``(detected, command_to_apply)`` text for ONE
    (symlink, .vco-new) redirect pair. Shared by the single-pair and
    multi-pair emitters so the rendering never drifts.
    """
    # Try to read the symlink target for the "detected" prose; soft-fail
    # if the read raises (e.g., permission denied, race during install).
    try:
        target = os.readlink(os.fspath(dest))
    except OSError:
        target = "<unreadable>"

    dest_display = _display_path(dest, install_root)
    vco_new_display = _display_path(vco_new, install_root)

    detected = (
        f"`{dest_display}` is a symlink → `{target}`. VCO's intended "
        f"content was written to `{vco_new_display}` instead. The "
        f"symlink itself was NOT modified."
    )
    command_to_apply = (
        f"# Option A — accept VCO's defaults over the symlink:\n"
        f"rm '{dest_display}' && mv '{vco_new_display}' '{dest_display}'\n"
        f"\n"
        f"# Option B — keep the existing symlink (delete VCO's sibling):\n"
        f"rm -rf '{vco_new_display}'"
    )
    return detected, command_to_apply


def emit_symlink_deferral_multi(
    deferral: "DeferralReport",
    events: list[tuple[Path, Path]],
    install_root: Path | None = None,
    *,
    cap: int = 5,
) -> None:
    """Append ONE consolidated ``symlink_preserved_under_install_path`` entry
    covering ALL ``(dest, vco_new)`` redirect pairs from a single install run.

    v0.2.70 (Bug B / W-F2): the single-pair :func:`emit_symlink_deferral`
    relies on ``DeferralReport.add_entry``'s last-write-wins per ``condition_id``,
    so calling it once per redirect would KEEP ONLY THE LAST path (silent report
    data-loss — the exact bug class Bug B fixes). When an install redirects many
    files (e.g. a symlinked ``.claude`` redirects every agent + settings.json),
    the user needs ALL of them listed. This builder emits a SINGLE entry whose
    ``detected`` / ``command_to_apply`` blocks list every pair.

    Uses the SAME ``SYMLINK_PRESERVED_CONDITION_ID`` slug so a re-run that
    re-encounters the condition replaces (not stacks) the prior entry, and so
    the launcher's deferred-reader + tests find it under the stable id.

    Args:
        deferral: the ``DeferralReport`` to append to (caller owns lifecycle).
        events: list of ``(dest, vco_new)`` pairs — ``dest`` is the symlink VCO
            refused to touch, ``vco_new`` is where VCO landed its content.
        install_root: optional root for relative-path display (more readable).
        cap: truncate the listed pairs to the first ``cap`` (with an
            "... and N more" trailer) so the deferral .md stays bounded when a
            symlinked ancestor redirects dozens of files. Defaults to 5.
    """
    if not events:
        return

    # Lazy import — same rationale as emit_symlink_deferral.
    from vco_lib.deferral_report import DeferralEntry

    # Deterministic order so the deferral .md doesn't churn between runs that
    # redirect the same set of files in a different enumeration order.
    ordered = sorted(events, key=lambda ev: _display_path(ev[0], install_root))
    shown = ordered[:cap]
    overflow = len(ordered) - len(shown)

    detected_blocks: list[str] = []
    command_blocks: list[str] = []
    for dest, vco_new in shown:
        det, cmd = _render_symlink_pair(dest, vco_new, install_root)
        detected_blocks.append(f"- {det}")
        command_blocks.append(cmd)

    detected = (
        f"{len(ordered)} path(s) under the install root are symlinks VCO "
        f"refused to write through; VCO's intended content was written to "
        f"`.vco-new` siblings instead. The symlinks themselves were NOT "
        f"modified:\n" + "\n".join(detected_blocks)
    )
    if overflow > 0:
        detected += f"\n- ... and {overflow} more"

    why_deferred = (
        "VCO never replaces or follows symlinks under the install path "
        "(hard rule, v0.2.46). To replace a symlink with VCO's defaults the "
        "user must delete the symlink first (Option A below), or keep the "
        "symlink and discard VCO's sibling (Option B). Until then, the "
        "stale symlinked files remain in place and the fresh content sits "
        "in the `.vco-new` siblings."
    )

    command_to_apply = "\n\n".join(command_blocks)
    if overflow > 0:
        command_to_apply += (
            f"\n\n# ... and {overflow} more redirected path(s) — list all with:\n"
            f"find . -name '*{VCO_NEW_SUFFIX}'"
        )

    entry = DeferralEntry(
        condition_id=SYMLINK_PRESERVED_CONDITION_ID,
        title="Symlink(s) preserved under install path",
        detected=detected,
        why_deferred=why_deferred,
        command_to_apply=command_to_apply,
        severity="info",
    )
    deferral.add_entry(entry)


def check_vco_new_collision(
    vco_new: Path,
    install_root: Path | None = None,
    deferral: "DeferralReport | None" = None,
) -> bool:
    """v0.2.46 post-adversarial L1: detect pre-existing `.vco-new` siblings.

    Returns True iff a prior install run already wrote content at
    ``vco_new``. The caller should:
      - Skip the write (don't silently clobber).
      - Print a one-line warning to the user.
      - Optionally emit a structured deferral so UPDATE_DEFERRED.md
        names the collision and tells the user how to reconcile.

    Adversarial review S4 surfaced this: if a user hand-edited a
    ``.claude/agents.vco-new`` between runs (perhaps mid-reconciliation,
    perhaps to tweak the bundled defaults), the NEXT install run would
    silently overwrite that work via ``shutil.copy2`` / ``write_text``
    with ``exist_ok=True``. No timestamp, no warning, no recovery hint.

    The L1 fix is conservative: detect the collision (presence check via
    ``os.path.lexists`` so dangling symlinks at the sibling path also
    trip the gate), let the caller skip the write, emit a deferral
    instructing the user to either (a) delete the prior ``.vco-new``
    to accept a fresh stage on the next run, or (b) move the prior
    ``.vco-new`` somewhere safe and re-run.

    Why presence-check, not timestamp-suffix re-naming:
        The adversarial proposed "use ``.vco-new.<timestamp>`` so old
        siblings are preserved." That accumulates noise — every re-run
        adds a new dated dir/file, the user has no way to know which
        one they meant to keep, and grep-discoverability for
        ``find . -name '*.vco-new'`` degrades. Presence-check + skip
        preserves the simple naming scheme and pushes the conflict to
        the user (the right place — they're the one who hand-edited).

    Args:
        vco_new: The would-be write target (already computed via
            ``compute_vco_new_path``).
        install_root: For relative-path display in the deferral entry.
        deferral: Optional ``DeferralReport`` to emit a structured
            collision entry. When None, the function only returns the
            boolean (caller logs / warns however it wants).

    Returns:
        True iff a collision is detected (caller must skip the write).
        False iff the slot is free (caller proceeds normally).
    """
    if not os.path.lexists(os.fspath(vco_new)):
        return False  # slot is free — caller proceeds

    if deferral is not None:
        # Lazy import — same rationale as emit_symlink_deferral.
        from vco_lib.deferral_report import DeferralEntry

        if install_root is not None:
            try:
                display = str(Path(vco_new).relative_to(install_root))
            except ValueError:
                display = str(vco_new)
        else:
            display = str(vco_new)

        detected = (
            f"`{display}` already exists from a prior install run. VCO "
            f"refused to overwrite it; the original .vco-new content was "
            f"preserved untouched. The new content VCO would have written "
            f"was NOT staged this run."
        )
        why_deferred = (
            "Silently clobbering a `.vco-new` sibling from a prior run "
            "could destroy user work (the user may have hand-edited the "
            "sibling between runs to tweak the bundled defaults). VCO "
            "preserves the prior content and asks the user to reconcile."
        )
        command_to_apply = (
            f"# Option A — discard prior .vco-new and re-stage fresh on next run:\n"
            f"rm -rf '{display}'  &&  python install.py --update\n"
            f"\n"
            f"# Option B — move prior .vco-new aside, then re-stage:\n"
            f"mv '{display}' '{display}.kept-by-user'  &&  python install.py --update"
        )

        entry = DeferralEntry(
            condition_id=VCO_NEW_COLLISION_CONDITION_ID,
            title=".vco-new sibling collision (prior run)",
            detected=detected,
            why_deferred=why_deferred,
            command_to_apply=command_to_apply,
            severity="info",
        )
        deferral.add_entry(entry)

    return True
