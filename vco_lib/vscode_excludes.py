# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``.vscode/settings.json`` exclude-block backfill — one home (extracted
from ``vco_lib/project_init.py`` in v0.2.103; that module is
line-ratchet-capped and must shrink, not grow — the same forced-extraction
pattern as ``vco_lib/bundle_backup`` in v0.2.100). ``project_init``
re-exports every name under its historical spellings, so its call-sites,
``tests/test_code_graph_project_env.py`` and ``install.py``'s
``_backfill_vscode_excludes`` mirror (which delegates here) are untouched.

PR-7 / addendum-4 (v0.2.11) — forensic context (00:56 live OOM-kill
incident, 2026-05-16): opening a large workspace (>10 GB / >50k files —
common for ML projects with venvs, model weights, cargo target/) in VS Code
with the Pylance extension active triggered:

  - the file watcher scanning every file under the workspace root,
  - Pylance indexing every Python file it discovered (including
    ``.venv/lib/python3.x/site-packages`` which is multi-GB of stdlib
    wheels),
  - the Chromium renderer holding the full DOM for the file tree,

and systemd-oomd killed VS Code's Chromium scope at 86.46% memory
pressure. The canonical ``files.watcherExclude`` + ``python.analysis.*``
blocks ship as launcher-managed defaults so every project the launcher
registers (and ``install-bundle --update``) gets them automatically.

Cross-OS notes:

  - JSON path patterns use forward slashes; VS Code normalizes on Windows,
    no per-OS branch is needed.
  - ``python.analysis.indexing: false`` disables Pylance's persistent
    index — the in-memory analysis still works, just doesn't write a
    multi-GB cache under ~/.cache/. Users who want indexing back can
    override per-project.

v0.2.103 (field report, Item 25) — ``files.exclude`` no longer hides the
user's own build outputs. A user could not see the PDFs their build had
just produced because ``**/dist`` / ``**/build`` / ``**/target`` were in
the shipped ``files.exclude``: performance is already covered by
``files.watcherExclude`` and ``search.exclude`` (which KEEP the entries),
so hiding them in the explorer only hides the user's outputs. The three
entries left the canonical block, and the backfill PRUNES them from
existing projects — but only an entry whose value is exactly the boolean
``true`` VCO wrote. An entry the user changed (``false``, a ``when``
clause) or a key the user added is theirs, never touched; the per-key
user-wins contract of the add pass is the prune's contract too.

Coordination with the Rust writer:

  Pre-PR-27, the launcher's Rust env writer (retired v0.2.97; at
  commands/projects_v2.rs) wrote a ``claude-code.env`` sub-object inside
  ``.vscode/settings.json``. That write was removed in PR-27 (v0.2.12,
  2026-05-16) because the key did NOT propagate to MCP subprocesses on
  Linux as of Claude Code 2.1.143 — empirical sentinel testing confirmed
  (``.claude/settings.json`` env is the channel that actually reaches
  MCPs). See docs/CLAUDE_CODE_COMPATIBILITY.md → "Per-project env files".

  After PR-27 the Rust writer does not touch ``.vscode/settings.json`` at
  all from the env-write code path, so this helper is the ONLY writer of
  that file across the launcher's project-init flow. It still adds only
  top-level keys (files.watcherExclude, etc.) so any pre-existing
  ``claude-code.env`` block authored by the user (or by a pre-PR-27
  launcher) is preserved verbatim — the by-key backfill never touches a
  key it doesn't own, and the prune touches only its own retired entries
  inside the one key it owns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from vco_lib import jsonc_edit
from vco_lib.atomic import atomic_write_bytes

_VSCODE_EXCLUDE_DEFAULTS: dict[str, object] = {
    # Watcher: prevent inotify / FSEvents / ReadDirectoryChangesW from
    # firing for these dirs. Heavy churn (cargo target/, node_modules/)
    # otherwise saturates the watcher queue.
    "files.watcherExclude": {
        "**/.git/objects/**": True,
        "**/.git/subtree-cache/**": True,
        "**/node_modules/**": True,
        "**/__pycache__/**": True,
        "**/.pytest_cache/**": True,
        "**/.ruff_cache/**": True,
        "**/.mypy_cache/**": True,
        "**/.venv/**": True,
        "**/venv/**": True,
        "**/site-packages/**": True,
        "**/dist/**": True,
        "**/build/**": True,
        "**/target/**": True,
        "**/state/**": True,
        "**/.claude/logs/**": True,
        "**/.claude/worktrees/**": True,
    },
    # File tree: hide noise from the explorer (still searchable via
    # `search.exclude` carve-out below if user removes it). v0.2.103: the
    # build outputs (dist/build/target) LEFT this block — hiding them
    # hid the user's own outputs, and the watcher + search excludes above
    # already cover the performance half. Existing projects get the
    # retired entries pruned by `backfill_in_project` below.
    "files.exclude": {
        "**/.git": True,
        "**/node_modules": True,
        "**/__pycache__": True,
        "**/.pytest_cache": True,
        "**/.ruff_cache": True,
        "**/.mypy_cache": True,
        "**/.venv": True,
    },
    # Quick-search exclude (Cmd/Ctrl+P, full-text find): skip the heavy
    # build / cache / log dirs so search latency stays sub-second.
    "search.exclude": {
        "**/node_modules": True,
        "**/__pycache__": True,
        "**/.venv": True,
        "**/dist": True,
        "**/build": True,
        "**/target": True,
        "**/state": True,
        "**/.claude/logs": True,
        "**/.claude/worktrees": True,
        "**/*.lock": True,
    },
    # Pylance: skip these dirs from type-analysis. Indexing OFF avoids
    # the persistent multi-GB cache under ~/.cache.
    "python.analysis.exclude": [
        "**/.venv/**",
        "**/venv/**",
        "**/__pycache__/**",
        "**/.pytest_cache/**",
        "**/.mypy_cache/**",
        "**/.claude/worktrees/**",
    ],
    "python.analysis.indexing": False,
}

# Keys the backfill helpers consider — exposed for tests + the
# orchestrator-side mirror in install.py.
_VSCODE_EXCLUDE_KEYS: tuple[str, ...] = (
    "files.watcherExclude",
    "files.exclude",
    "search.exclude",
    "python.analysis.exclude",
    "python.analysis.indexing",
)

# The `files.exclude` entries VCO shipped through v0.2.102 and retired in
# v0.2.103 (Item 25): they hid the user's own build outputs from the
# explorer while `files.watcherExclude` / `search.exclude` already covered
# the performance. Pruned from existing projects — ONLY at the exact
# boolean `true` VCO wrote, so a user-set value or key is never touched.
_RETIRED_FILES_EXCLUDE_ENTRIES: tuple[str, ...] = (
    "**/dist",
    "**/build",
    "**/target",
)


def _prune_retired_files_exclude_entries(data: dict) -> list[str]:
    """Remove the retired build-output entries from ``files.exclude`` in
    ``data`` (in place). Returns the ``"<key>:<pattern>"`` removals.

    User-wins, per entry: an entry is removed only when its value ``is
    True`` — the exact boolean the canonical block wrote. ``false``, a
    ``when``-clause object or any other user-chosen shape stays, and every
    other key in the block stays. A ``files.exclude`` that is not a
    mapping (user's own shape) is left entirely alone.
    """
    block = data.get("files.exclude")
    if not isinstance(block, dict):
        return []
    removed: list[str] = []
    for pattern in _RETIRED_FILES_EXCLUDE_ENTRIES:
        if block.get(pattern) is True:
            del block[pattern]
            removed.append(f"files.exclude:{pattern}")
    return removed


def backfill_in_project(folder: Path) -> dict:
    """Idempotent: add VS Code watcher/search/Pylance exclude blocks to
    a per-project `.vscode/settings.json` when keys are missing, and
    prune the retired `files.exclude` build-output entries VCO wrote.

    PR-7 (v0.2.11): without these excludes, opening a large workspace
    (>10 GB / >50k files — typical for ML projects with venvs, cargo
    target/, model weights) in VS Code triggers OOM kills (verified
    live on multiple large project trees on 2026-05-16). The launcher now
    ships the canonical exclude block as a backfill — existing projects
    catch up on `install-bundle --update`.

    v0.2.103 (Item 25): the same run retires the `**/dist` / `**/build` /
    `**/target` entries from `files.exclude` in existing projects. They
    hid the user's own build outputs from the explorer (a user could not
    see their PDFs under `build/`); `files.watcherExclude` and
    `search.exclude` keep theirs, so performance is unaffected.

    Idempotency contract:
      - Missing settings file → create it with just the exclude block
        + a marker comment. `_template_origin: "vibecoded-orchestrator
        v0.2.11+ — vscode-excludes backfill"` so the file is identifiable.
      - File not valid JSON or JSONC → action="unparseable" (no-op,
        preserves user file untouched). A JSONC file (comments, trailing
        commas — the common hand-edited case) is READ and the missing keys
        are inserted into its text, comments kept (v0.2.97); an insertion
        that cannot be verified is action="jsonc_edit_refused", untouched.
      - Top-level key already present → user-wins, leave alone (covers
        the "user set `files.watcherExclude: {}` to explicitly disable
        the feature" case the addendum calls out). The retired-entry prune
        applies the same user-wins rule PER ENTRY: only a value that is
        exactly the boolean `true` VCO wrote is removed.
      - Top-level key missing → add it with the canonical value.

    Args:
        folder: target user-project folder.

    Returns:
        `{"action": str, "added_keys": [str, ...],
        "removed_entries": [str, ...], "path": str}`. Action
        is one of:
          - "created"     — file didn't exist, written from canonical defaults
          - "backfilled"  — file existed; added one or more missing keys
                            (possibly alongside a prune)
          - "pruned"      — file existed; only retired entries were removed
          - "noop"        — file existed; nothing to add or prune
          - "unparseable" — file existed but couldn't be parsed; left alone
          - "jsonc_edit_refused" — JSONC file whose edit could not be
            verified; left alone
          - "write_failed:<ErrorClass>" — atomic write raised
    """
    settings_file = folder / ".vscode" / "settings.json"
    result: dict = {
        "action": "noop",
        "added_keys": [],
        "removed_entries": [],
        "path": str(settings_file),
    }

    if not settings_file.exists():
        # Fresh write: include just the exclude block (no claude-code.env
        # — PR-27 (v0.2.12, 2026-05-16) removed the Rust launcher's
        # write of that block from `.vscode/settings.json` because it
        # didn't propagate to MCP subprocesses on Linux. The canonical
        # channel for per-project MCP env is `.claude/settings.json`
        # env, written by the launcher's config_projection apply.)
        payload: dict = {
            "_template_origin": (
                "vibecoded-orchestrator v0.2.11+ — vscode-excludes backfill"
            ),
        }
        for key, value in _VSCODE_EXCLUDE_DEFAULTS.items():
            payload[key] = value
        try:
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, indent=2) + "\n"
            redirect = atomic_write_bytes(
                settings_file, text.encode("utf-8"),
                fsync=False, symlink_safe=True,
            )
            if redirect is not None:
                sys.stderr.write(
                    f"[vct] NEW-8 symlink-blocking: refusing to write "
                    f"through symlink at {settings_file}; redirecting to "
                    f"{redirect}. The .vco-new sibling holds the "
                    f"orchestrator's intended content; the symlink and "
                    f"its destination are untouched. Merge manually "
                    f"when ready.\n"
                )
        except OSError as e:
            result["action"] = f"write_failed:{type(e).__name__}"
            return result
        result["action"] = "created"
        result["added_keys"] = list(_VSCODE_EXCLUDE_DEFAULTS.keys())
        return result

    loaded = jsonc_edit.load_object(settings_file)
    if loaded is None:
        result["action"] = "unparseable"
        return result
    data, raw = loaded

    added: list[str] = []
    for key, value in _VSCODE_EXCLUDE_DEFAULTS.items():
        if key not in data:
            data[key] = value
            added.append(key)

    removed = _prune_retired_files_exclude_entries(data)

    if not added and not removed:
        return result  # action stays "noop"

    payload_text = jsonc_edit.dumps_preserving(raw, data, indent=2)
    if payload_text is None:
        result["action"] = "jsonc_edit_refused"
        return result
    try:
        redirect = atomic_write_bytes(
            settings_file, payload_text.encode("utf-8"),
            fsync=False, symlink_safe=True,
        )
        if redirect is not None:
            sys.stderr.write(
                f"[vct] NEW-8 symlink-blocking: refusing to write "
                f"through symlink at {settings_file}; redirecting to "
                f"{redirect}. The .vco-new sibling holds the "
                f"orchestrator's intended content; the symlink and "
                f"its destination are untouched. Merge manually "
                f"when ready.\n"
            )
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "backfilled" if added else "pruned"
    result["added_keys"] = added
    result["removed_entries"] = removed
    return result
