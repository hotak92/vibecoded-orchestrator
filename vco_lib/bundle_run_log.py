# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Each project's own record of its bundle runs (v0.2.100, F-W2-08(b)).

"Update all projects" used to leave no per-project result on disk — only a
toast. Every ``install_project_bundle`` run that touched the tree (not a dry
run) now appends its human summary to ``<project>/.claude/logs/bundle-install.log``,
whichever surface started it (the launcher's per-project update, "Update all",
a module toggle, ``install.py`` at the orchestrator root, the CLI). The block
is the ONE renderer's output (:func:`vco_lib.project_init.format_bundle_result_lines`)
under a timestamp header, so the log reads exactly like the CLI's own output.

Best-effort: a log write must never fail an install. The file is bounded —
past ``MAX_BYTES`` the oldest half is dropped at the next append.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

__all__ = ["LOG_REL", "MAX_BYTES", "append_run"]

LOG_REL = Path(".claude") / "logs" / "bundle-install.log"
MAX_BYTES = 512 * 1024


def append_run(folder: Path, result: dict) -> None:
    """Append ``result``'s summary block to the project's bundle log."""
    try:
        from vco_lib.project_init import format_bundle_result_lines

        if result.get("dry_run"):
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mode = "update" if result.get("update_mode") else "install"
        header = (f"== {ts}  bundle {mode}  vco {result.get('vco_version', 'unknown')}"
                  f" ({result.get('vco_commit') or 'no commit'})")
        block = "\n".join([header, *format_bundle_result_lines(result)]) + "\n\n"
        target = Path(folder) / LOG_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size > MAX_BYTES:
            data = target.read_bytes()
            target.write_bytes(data[len(data) // 2:])
        with target.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception:  # noqa: BLE001 — a log line never fails an install
        pass
