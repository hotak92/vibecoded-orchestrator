# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundle-update resume sentinel — one home (extracted from
``vco_lib/project_init.py`` in v0.2.101; the module is line-ratchet-capped
and must shrink, not grow — the same forced-extraction pattern as
``vco_lib/bundle_settings_io`` in v0.2.95 and ``vco_lib/bundle_backup`` in
v0.2.100). ``project_init`` re-exports every name under its historical
spellings, so its call-sites and ``tests/test_project_bundle_resume_sentinel``
are untouched.

NEW-7 / B1 (v0.2.53) — bundle-update resume sentinel.

Mirrors the v0.2.51 orchestrator-self pattern
(``launcher/src-tauri/src/commands/installer.rs::write_update_resume_sentinel``)
but scoped to per-project bundle updates rather than the orchestrator-self
update. Same recovery shape: a JSON file lands on disk BEFORE any FS
mutation; the engine deletes it after the manifest write succeeds. If the run
is killed mid-pass (Cmd-C, OOM, power loss), the sentinel survives and the
next session-start detects it + prompts the user to resume (or warns them to
re-run ``install-bundle --update``).

Without this, a mid-update interrupt leaves the manifest stale + files
partially overwritten. The next ``--update`` run sees a manifest pointing at
OLD shipped hashes for files we've already updated → ``_file_action`` returns
``("preserve", ...)`` for them → user-modified false-flagging → user-visible
"5 files preserved" toast for files the user never touched.

Audit: ``.claude/context/audits/project-bundle-install-audit-2026-06-10.md``
§6.6 / B1.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "BUNDLE_UPDATE_SENTINEL_REL",
    "BUNDLE_UPDATE_SENTINEL_SCHEMA",
    "bundle_sentinel_path",
    "clear_bundle_update_resume_sentinel",
    "read_bundle_update_resume_sentinel",
    "write_bundle_update_resume_sentinel",
]

BUNDLE_UPDATE_SENTINEL_REL = Path(".claude") / "state" / "bundle-update-resume-needed.json"
BUNDLE_UPDATE_SENTINEL_SCHEMA = 1


def bundle_sentinel_path(folder: Path) -> Path:
    """Absolute path to the bundle-update sentinel for ``folder``."""
    return folder / BUNDLE_UPDATE_SENTINEL_REL


def read_bundle_update_resume_sentinel(folder: Path) -> Optional[dict]:
    """Read the bundle-update resume sentinel, if any.

    Returns ``None`` when:
      * the file is absent, or
      * the file is malformed JSON, or
      * the schema_version is unknown.

    Caller treats any None outcome as "no resume pending" so a broken
    sentinel never wedges the next install.
    """
    path = bundle_sentinel_path(folder)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != BUNDLE_UPDATE_SENTINEL_SCHEMA:
        return None
    return payload


def write_bundle_update_resume_sentinel(
    folder: Path,
    *,
    operation: str = "install-bundle-update",
    orchestrator_root: Optional[Path] = None,
    vco_version: str = "unknown",
    redirect_sink: Optional[list] = None,
) -> bool:
    """Atomic-write the bundle-update resume sentinel.

    Best-effort: any I/O failure logs to stderr + returns False rather
    than raising. The bundle install MUST proceed even when sentinel
    write fails (sentinel is a recovery aid, not a hard requirement).

    v0.2.70 (Bug B / B-1): the sentinel lives under `.claude/state/`, so when
    `.claude` is a symlink VCO refused to write through, the write redirects to
    a `.vco-new` sibling. When `redirect_sink` (a list) is provided, an
    `(original_target, vco_new)` pair is appended to it on redirect so the
    caller can fold it into the consolidated symlink deferral. Default `None`
    keeps the `bool`-return contract unchanged for all other callers.
    """
    from vco_lib.project_init import _write_file_atomic  # lazy: patch point

    payload = {
        "schema": BUNDLE_UPDATE_SENTINEL_SCHEMA,
        "operation": operation,
        "folder": str(folder),
        "orchestrator_root": str(orchestrator_root) if orchestrator_root else "",
        "vco_version": vco_version,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
    }
    target = bundle_sentinel_path(folder)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: mkdir {parent} failed: {e} — "
            f"skipping sentinel write\n"
        )
        return False
    # Tempfile + rename for atomicity. _write_file_atomic already does
    # this for arbitrary bytes; reuse it.
    try:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        _redirect = _write_file_atomic(target, body)
        if _redirect is not None and redirect_sink is not None:
            redirect_sink.append((target, _redirect))
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: write {target} failed: {e}\n"
        )
        return False
    return True


def clear_bundle_update_resume_sentinel(folder: Path) -> bool:
    """Best-effort: delete the bundle-update sentinel. Returns True on
    success or when the file was already absent; False on any other
    error. The caller never blocks on this — failing to delete a stale
    sentinel just leaves a warning surface for the next session."""
    target = bundle_sentinel_path(folder)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        sys.stderr.write(
            f"[vct] bundle-update sentinel: unlink {target} failed: {e}\n"
        )
        return False
