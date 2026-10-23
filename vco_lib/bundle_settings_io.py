# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The I/O half of the bundle's ``.claude/settings.json`` merge.

Read the shipped template, read the project's file, hand both to
:func:`vco_lib.settings_merge.smart_merge_settings` (the pure DECISION half,
v0.2.95), write the answer. Moved out of ``vco_lib.project_init`` in v0.2.97
(size ratchet); ``project_init._merge_settings_template_for_bundle`` is the
thin shim every caller and test still uses.

v0.2.97 — a JSONC ``settings.json`` is no longer skipped. Until now a file
with a comment or a trailing comma was left alone SILENTLY, so newly shipped
hooks never reached it and nothing said so. It is now edited in place through
:mod:`vco_lib.jsonc_edit` (only the members the merge changed are rewritten;
every comment elsewhere is kept; the result is re-parsed and verified), and a
file that cannot be edited that way — or cannot be read at all — is refused
through :mod:`vco_lib.settings_refusal`: left byte-identical, with a
``settings_write_refused_bundle_claude_settings_json`` deferral naming it. The
next successful merge clears that entry. The surface key is the bundle's OWN,
distinct from the env projection's ``claude_settings_json``: the two writers
edit different members, so one's success must not clear the other's refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from vco_lib import settings_refusal
from vco_lib.mcp_scan_rules import retired_settings_allow_patterns
from vco_lib.parked_hooks import ParkedHooksState
from vco_lib.settings_merge import smart_merge_settings

__all__ = ["REFUSAL_SURFACE", "STATUS_EDIT_REFUSED", "STATUS_UNPARSEABLE",
           "fresh_settings_from_template", "merge_settings_template", "retry_command",
           "settings_template_path"]

#: ``settings_write_refused_<surface>`` family member for this writer.
REFUSAL_SURFACE = "bundle_claude_settings_json"
STATUS_UNPARSEABLE = "unchanged (user file unparseable)"
STATUS_EDIT_REFUSED = "unchanged (JSONC edit refused)"

#: ``write(target, data) -> redirect`` — ``project_init._write_file_atomic``.
Writer = Callable[[Path, bytes], Optional[Path]]


def settings_template_path(orchestrator_root: Path) -> Path:
    """The OS-specific settings.json template the bundle merges — one home
    (moved from ``project_init`` in v0.2.102; the launcher's parked-hook
    carry check, ``vco_lib.parked_hooks carried``, reads the same file)."""
    import platform
    name = (
        "settings.json.windows.template"
        if platform.system() == "Windows"
        else "settings.json.linux.template"
    )
    return Path(orchestrator_root) / "templates" / name


def retry_command(project_root: Path) -> str:
    """The command that re-runs this writer for ``project_root``."""
    return (f"python -m vco_lib.project_init install-bundle "
            f"--folder '{project_root}' --update")


def fresh_settings_from_template(
    template_path: Path,
    *,
    parked: Optional[ParkedHooksState] = None,
    kept_out: Optional[list] = None,
) -> dict:
    """The settings a project gets when it has no ``settings.json`` yet: the
    shipped template, minus the hooks the launcher has parked for it (only
    when that answer is readable). One home for "a fresh file from the
    template" — the bundle's create branch below and ``install.py
    --adopt-project-replace-all`` (v0.2.104) both start from it, so a
    whole-file rewrite can never drop a template key (``hooks``,
    ``skillOverrides``, ``idleCompaction``, the permission denies).

    Raises ``OSError`` / ``ValueError`` when the template cannot be read or
    parsed — a missing shipped template is a broken install, never a case to
    paper over.
    """
    template_data = json.loads(template_path.read_text(encoding="utf-8"))
    return smart_merge_settings(
        {}, template_data, kept_out=kept_out,
        parked=parked if parked is not None and parked.readable else None)


def merge_settings_template(
    template_path: Path,
    target_path: Path,
    *,
    dry_run: bool,
    write: Writer,
    retired_removed: Optional[list] = None,
    parked: Optional[ParkedHooksState] = None,
    kept_out: Optional[list] = None,
    project_root: Optional[Path] = None,
) -> tuple[str, Optional[Path]]:
    """Merge the template into ``target_path``; see the shim's docstring in
    ``project_init`` for the full status contract.

    Returns ``(status, redirect)``; ``status`` is one of ``would-create`` /
    ``created`` / ``would-merge`` / ``merged`` / ``unchanged`` /
    :data:`STATUS_UNPARSEABLE` / :data:`STATUS_EDIT_REFUSED`. A refusal is
    recorded at ``project_root`` (default: the parent of the ``.claude``
    directory) on a real run only — a dry run reports, it never writes the
    ledger.
    """
    template_data = json.loads(template_path.read_text(encoding="utf-8"))

    if not target_path.exists():
        created = fresh_settings_from_template(
            template_path, parked=parked, kept_out=kept_out)
        if dry_run:
            return "would-create", None
        target_path.parent.mkdir(parents=True, exist_ok=True)
        return "created", write(
            target_path, (json.dumps(created, indent=2) + "\n").encode("utf-8"))

    root = project_root if project_root is not None else target_path.parent.parent
    loaded = settings_refusal.load_for_edit(target_path)
    if not isinstance(loaded, tuple):  # a Refusal — the file exists (checked above)
        if loaded is not None and not dry_run:
            settings_refusal.record(root, REFUSAL_SURFACE, loaded,
                                    retry_command=retry_command(root))
        return STATUS_UNPARSEABLE, None
    existing, raw = loaded

    merged = smart_merge_settings(
        existing, template_data, retired_removed=retired_removed,
        parked=parked, kept_out=kept_out,
        # v0.2.101: the settings-merge arm of the MCP retirement scrub —
        # same one-home rule table (mcp_scan_rules.toml) the ~/.claude.json
        # registration scrub reads. Only the merge path can carry it: the
        # create path starts from the template, which no longer ships the
        # retired pattern.
        retired_allow=retired_settings_allow_patterns(),
    )
    if merged == existing:
        if not dry_run:  # nothing is owed any more: a recorded refusal is over
            settings_refusal.clear_recorded(root, REFUSAL_SURFACE)
        return "unchanged", None
    if dry_run:
        return "would-merge", None

    text = settings_refusal.edit_text_or_record(
        root, REFUSAL_SURFACE, target_path, raw, merged,
        indent=2, retry_command=retry_command(root))
    if text is None:
        return STATUS_EDIT_REFUSED, None
    redirect = write(target_path, text.encode("utf-8"))
    settings_refusal.clear_recorded(root, REFUSAL_SURFACE)
    return "merged", redirect
