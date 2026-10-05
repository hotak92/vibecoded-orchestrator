# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Project-own hooks registered by a RELATIVE ``.claude/hooks/...`` or
``.claude/scripts/...`` path — detected by the bundle update and OFFERED an
anchored rewrite (v0.2.100 WP-17, F-W1-16; ``.claude/scripts/`` added v0.2.101
NB-13).

Why
===
Claude Code runs a hook command in the session's CURRENT directory, and that
directory follows ``cd`` and worktrees. A command such as
``bash .claude/hooks/x.sh`` therefore fails with "No such file or directory"
(exit 127) as soon as the session's cwd moves — on every tool call, for the
rest of the session. Field evidence: one project's transcript held 23 780
``hook_non_blocking_error`` lines (19.8 MB) from relative hook commands, one
project-own guard failing 4 096 times; and every SubagentStart hook a lane ran
from a subdirectory failed the same way for months.

VCO-shipped hooks are already anchored at ``${CLAUDE_PROJECT_DIR}`` by the
bundle merge itself (v0.2.97, :func:`vco_lib.hooks_settings.anchor_hook_command`
with ``only=`` the shipped scripts). A project's OWN hooks are the user's
configuration, so VCO never rewrites them on its own. Instead:

* the bundle update DETECTS them (``.claude/settings.json`` and
  ``.claude/settings.local.json`` — Claude Code reads both) and records ONE
  ``project_hooks_relative_paths`` deferral entry naming every command and the
  exact rewrite it would make;
* the user applies it with ``python -m vco_lib.hook_relative_paths anchor
  --project-folder <project>`` — the ONE writer
  (:func:`vco_lib.hooks_settings.write_settings`: symlink refusal, JSONC kept,
  round-trip check, atomic write) — or edits the file by hand;
* the registry clear-probe (:func:`relative_hooks_still_present`) drops the
  entry once no relative command remains, whichever way it was fixed.

The rewrite is :func:`vco_lib.hooks_settings.anchor_hook_command` — the same
per-OS form the shipped hooks use — limited to the scripts found relative, so
nothing else in a command changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from vco_lib.hooks_settings import (
    HooksSettingsError,
    anchor_hook_command,
    invoked_script_tokens,
    load_settings,
    normalize_matcher,
    write_settings,
)
from vco_lib.jsonc_edit import load_object

#: Declared in ``vco_lib/deferral_conditions.toml`` (action_required, cleared
#: by the probe ``project_hooks_relative_paths_still_present``).
CID = "project_hooks_relative_paths"

#: The settings files Claude Code reads hooks from, project-relative.
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")

#: An invoked hook script under ``.claude/hooks/`` by a path that does NOT
#: start at the project root (after quote-stripping and ``\\`` -> ``/``).
_RELATIVE_HOOK_RE = re.compile(r"^(?:\./)?\.claude/hooks/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:sh|ps1))$")

#: An invoked script under ``.claude/scripts/`` by a relative path (v0.2.101,
#: NB-13). The subpath allows ``/`` segments and extension-less scripts
#: (``kg-sync``, ``lib/vct_project_config.sh``); the identity is the
#: project-relative path, NOT the basename — basenames collide across the
#: script subfolders.
_RELATIVE_SCRIPT_RE = re.compile(r"^(?:\./)?\.claude/scripts/([A-Za-z0-9][A-Za-z0-9._/-]*)$")


def relative_scripts(command: Any) -> List[str]:
    """Identities of the project scripts ``command`` INVOKES by a relative
    path: a ``.claude/hooks/<basename>`` hook contributes its basename; a
    ``.claude/scripts/<subpath>`` script contributes its project-relative path
    (``.claude/scripts/kg-sync``) — a script basename collides across
    subfolders (``lib/x.sh``), so the path is the identity. An already-anchored
    command, a path that is only an ARGUMENT, and a non-string all yield
    ``[]``."""
    if not isinstance(command, str) or not command:
        return []
    found: List[str] = []
    for token in invoked_script_tokens(command):
        match = _RELATIVE_HOOK_RE.match(token)
        if match:
            identity = match.group(1)
        else:
            match = _RELATIVE_SCRIPT_RE.match(token)
            if not match:
                continue
            identity = f".claude/scripts/{match.group(1)}"
        if identity not in found:
            found.append(identity)
    return found


def find_relative_hook_commands(hooks_block: Any) -> List[Dict[str, Any]]:
    """Every hook item in ``hooks_block`` that invokes a project-own
    ``.claude/hooks/`` or ``.claude/scripts/`` script relatively, with the
    anchored command the rewrite would write."""
    out: List[Dict[str, Any]] = []
    if not isinstance(hooks_block, dict):
        return out
    for event, groups in hooks_block.items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for item in group.get("hooks") or []:
                command = item.get("command") if isinstance(item, dict) else None
                scripts = relative_scripts(command)
                if not scripts or not isinstance(command, str):
                    continue
                anchored = anchor_hook_command(command, only=scripts)
                if anchored == command:  # pragma: no cover — defensive
                    continue
                out.append({"event": event, "matcher": normalize_matcher(group),
                            "command": command, "anchored": anchored})
    return out


def scan(folder: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """``{settings-file: [findings]}`` for ``folder`` (files with none are
    omitted), or ``None`` when an existing settings file cannot be read — the
    caller then decides nothing (unknown is not resolved)."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for rel in SETTINGS_FILES:
        path = Path(folder) / rel
        try:
            if not path.exists():
                continue
        except OSError:
            return None
        loaded = load_object(path)
        if loaded is None:
            return None
        found = find_relative_hook_commands(loaded[0].get("hooks"))
        if found:
            result[rel] = found
    return result


def relative_hooks_still_present(folder: Path) -> Optional[bool]:
    """Clear probe for :data:`CID` (tri-state, read-only): ``False`` only when
    every settings file was read and none holds a relative hook command."""
    found = scan(folder)
    if found is None:
        return None
    return bool(found)


def anchor_relative_hooks(folder: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """Rewrite every relative ``.claude/hooks/`` or ``.claude/scripts/``
    invocation in ``folder``'s settings files to the anchored form, through the
    ONE settings writer.

    Returns ``{"changed": {file: [{event, matcher, command, anchored}]},
    "refused": {file: message}}``. A file the writer refuses (symlink,
    unparseable, JSONC edit it cannot verify) is reported, never forced.
    """
    changed: Dict[str, List[Dict[str, Any]]] = {}
    refused: Dict[str, str] = {}
    for rel in SETTINGS_FILES:
        path = Path(folder) / rel
        if not path.exists():
            continue
        try:
            doc = load_settings(path)
        except HooksSettingsError as exc:
            refused[rel] = exc.message
            continue
        done: List[Dict[str, Any]] = []
        for event, groups in (doc.data.get("hooks") or {}).items():
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                for item in group.get("hooks") or []:
                    if not isinstance(item, dict):
                        continue
                    command = item.get("command")
                    scripts = relative_scripts(command)
                    if not scripts or not isinstance(command, str):
                        continue
                    anchored = anchor_hook_command(command, only=scripts)
                    if anchored != command:
                        done.append({"event": event, "matcher": normalize_matcher(group),
                                     "command": command, "anchored": anchored})
                        item["command"] = anchored
        if not done:
            continue
        if not dry_run:
            try:
                write_settings(doc)
            except HooksSettingsError as exc:
                refused[rel] = exc.message
                continue
        changed[rel] = done
    return {"changed": changed, "refused": refused}


def _listing(found: Dict[str, List[Dict[str, Any]]], limit: int = 20) -> str:
    lines: List[str] = []
    for rel, rows in found.items():
        for row in rows:
            where = f"{rel} {row['event']}" + (f" (matcher `{row['matcher']}`)" if row["matcher"] else "")
            lines.append(f"  - {where}: `{row['command']}`\n      -> `{row['anchored']}`")
    if len(lines) > limit:
        lines = lines[:limit] + [f"  - ... +{len(lines) - limit} more"]
    return "\n".join(lines)


def emit_relative_hooks_deferral(folder: Path, *, log: Callable[..., Any]) -> bool:
    """Record every relative project-own hook as ONE :data:`CID` entry.

    Emits nothing when there is none — clearing a stale entry is the registry
    probe's job. Never raises: the ledger is best-effort and must not fail an
    update. Returns whether it emitted.
    """
    found = scan(folder)
    if not found:
        return False
    count = sum(len(rows) for rows in found.values())
    try:
        from vco_lib.deferral_emit import emit  # noqa: PLC0415
        from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

        emitted = emit(folder, DeferralEntry(
            condition_id=CID,
            title="Project hooks registered by a relative path fail after a `cd`",
            detected=(
                f"{count} hook command(s) in this project's own settings invoke a "
                f"project script (`.claude/hooks/` or `.claude/scripts/`) by a "
                f"RELATIVE path:\n{_listing(found)}\n"
                "Claude Code runs a hook in the session's CURRENT directory, so once the "
                "session (or a subagent) works from a subdirectory these fail with "
                "\"No such file or directory\" on every call — the hook silently stops "
                "doing its job and each failure is written into the session transcript."
            ),
            why_deferred=(
                "These are your project's own hooks, not VCO's, so VCO does not rewrite "
                "them without you. VCO's own hooks are already anchored at "
                "${CLAUDE_PROJECT_DIR}."
            ),
            command_to_apply=(
                "# From the orchestrator root, with its venv active — rewrites exactly the\n"
                "# commands listed above to the anchored form shown (nothing else changes):\n"
                f"python -m vco_lib.hook_relative_paths anchor --project-folder \"{folder}\"\n"
                "# Or edit .claude/settings.json by hand. This entry clears itself on the\n"
                "# next update once no relative hook command remains."
            ),
            severity="warning",
        ), keep_first_detected=True)
    except Exception as exc:  # noqa: BLE001 — ledger I/O is best-effort
        log("4.bundle.settings.relative_hooks", "warn",
            f"could not record {count} relative hook command(s): {exc}")
        return False
    log("4.bundle.settings.relative_hooks", "warn" if emitted else "ok",
        f"{count} project hook command(s) use a relative .claude/hooks/ or "
        ".claude/scripts/ path; anchored rewrite offered in UPDATE_DEFERRED.md",
        data={"found": found})
    return bool(emitted)


# ═══════════════════════════════════════════════════════════════════════
# CLI — every subcommand prints exactly ONE JSON object on stdout.
# ═══════════════════════════════════════════════════════════════════════


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.hook_relative_paths",
        description="Find / anchor project hooks registered by a relative "
                    ".claude/hooks/ or .claude/scripts/ path.")
    sub = parser.add_subparsers(dest="op", required=True)
    for name, help_text in (("list", "Report relative hook commands (read-only)."),
                            ("anchor", "Rewrite them to the ${CLAUDE_PROJECT_DIR}-anchored form.")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project-folder", required=True)
        if name == "anchor":
            p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    folder = Path(args.project_folder)
    if not folder.is_dir():
        sys.stdout.write(json.dumps({"ok": False, "error": f"{folder} is not a directory"}) + "\n")
        return 2
    if args.op == "list":
        found = scan(folder)
        if found is None:
            sys.stdout.write(json.dumps({"ok": False, "error": "a settings file could not be read"}) + "\n")
            return 1
        sys.stdout.write(json.dumps({"ok": True, "found": found}, ensure_ascii=False) + "\n")
        return 0
    outcome = anchor_relative_hooks(folder, dry_run=args.dry_run)
    ok = not outcome["refused"]
    sys.stdout.write(json.dumps({"ok": ok, "dry_run": args.dry_run, **outcome}, ensure_ascii=False) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
