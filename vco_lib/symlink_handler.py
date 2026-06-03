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
    "SYMLINK_PRESERVED_CONDITION_ID",
    "VCO_NEW_SUFFIX",
]


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

    # Try to read the symlink target for the "detected" prose; soft-fail
    # if the read raises (e.g., permission denied, race during install).
    try:
        target = os.readlink(os.fspath(dest))
    except OSError:
        target = "<unreadable>"

    if install_root is not None:
        try:
            dest_display = str(Path(dest).relative_to(install_root))
            vco_new_display = str(Path(vco_new).relative_to(install_root))
        except ValueError:
            # Path is not under install_root — fall back to absolute.
            dest_display = str(dest)
            vco_new_display = str(vco_new)
    else:
        dest_display = str(dest)
        vco_new_display = str(vco_new)

    detected = (
        f"`{dest_display}` is a symlink → `{target}`. VCO's intended "
        f"content was written to `{vco_new_display}` instead. The "
        f"symlink itself was NOT modified."
    )

    why_deferred = (
        "VCO never replaces or follows symlinks under the install path "
        "(hard rule, v0.2.46). To replace this symlink with VCO's defaults "
        "the user must delete the symlink first, then re-run install.py."
    )

    command_to_apply = (
        f"# Option A — accept VCO's defaults over the symlink:\n"
        f"rm '{dest_display}' && mv '{vco_new_display}' '{dest_display}'\n"
        f"\n"
        f"# Option B — keep the existing symlink (delete VCO's sibling):\n"
        f"rm -rf '{vco_new_display}'"
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
