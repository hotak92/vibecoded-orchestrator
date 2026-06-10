# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""settings.json template merge (vco_lib.settings_merge — v0.2.53).

Single home for the settings.json merge logic shared between:

* ``install.py::_merge_settings_template`` (orchestrator-self).
* ``vco_lib.project_init::_merge_settings_template_for_bundle``
  (per-project bundle install).

The two implementations were declared independently and have drifted
across releases — see :file:`.claude/context/audits/vco-lib-python-dedup-2026-06-10.md`
finding 4. The drift bug is mostly cosmetic at v0.2.53 but is a
latent risk because the same operator (settings.json merge) is now
done in two different places.

Per docs/INSTALL_ARCHITECTURE_v2.md §7.3.

v0.2.53 lands the module skeleton with the canonical merge function
shape. Migrating install.py + project_init.py onto it is a v0.2.54
organisational refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SettingsMergeResult:
    """Outcome of a settings.json merge."""

    wrote_file: bool
    """True iff the function wrote to ``target`` (False on dry-run or
    no-change-needed)."""

    keys_added: List[str] = field(default_factory=list)
    """Top-level keys that were ADDED (present in template, absent in
    existing target)."""

    keys_overwritten: List[str] = field(default_factory=list)
    """Top-level keys that were OVERWRITTEN (template won over a
    different existing value)."""

    keys_preserved: List[str] = field(default_factory=list)
    """Top-level keys PRESERVED from the existing target (template was
    silent, target had it)."""

    diff_summary: str = ""
    """Human-readable summary of changes for log output."""


def merge_settings_template(
    target: Path,
    template_data: Dict[str, Any],
    *,
    dry_run: bool = False,
    overwrite_keys: Optional[List[str]] = None,
) -> SettingsMergeResult:
    """Merge ``template_data`` into ``target`` (settings.json file).

    Args:
        target: Path to the settings.json file (may or may not exist).
        template_data: Dict from the template (shape per
            templates/settings.json.template).
        dry_run: If True, compute the merge but do NOT write.
        overwrite_keys: Top-level keys to ALWAYS overwrite from the
            template even when present in target. Default empty (preserve
            user customisations).

    Returns:
        :class:`SettingsMergeResult` describing the merge outcome.

    v0.2.53 STUB: this implementation is not yet wired into install.py
    + project_init.py callers. The module exists so future tracks can
    target it for migration. Full implementation lands in v0.2.54.

    Raises:
        NotImplementedError: when called in v0.2.53 (placeholder).
    """
    raise NotImplementedError(
        "merge_settings_template is a v0.2.53 stub for v0.2.54 migration. "
        "Use install.py::_merge_settings_template or "
        "vco_lib.project_init::_merge_settings_template_for_bundle for now."
    )
