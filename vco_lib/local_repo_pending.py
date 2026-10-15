# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tell a command-line bundle run that the project still has no git repo.

v0.2.103 (plan item 17, review R11 B3). The launcher creates a local-only git
repository for a repo-less project at add and on its next **Update bundle**
(``worktree_repo_mode::ensure_project_local_repo`` in the Rust launcher): the
decision there needs the launcher's record of the user's git choice
(``module_settings.worktree_repo_mode``), and the repository it makes carries
a ``.gitignore`` guard computed by the launcher's nested-repo walk. A user who
updates only from the command line
(``python -m vco_lib.project_init install-bundle --update``) never reaches
that code, so before this module the project silently stayed repo-less.

Why the command-line path REPORTS instead of performing the init: doing the
init here would need a second copy of the Rust decision (enclosing repo,
nested repos, recorded mode) and of the guard block — a cross-language mirror
for a rare path. Reporting needs only two facts Python can read on its own:
"is the folder inside a git repo?" and "has a choice been recorded?". So every
bundle run (launcher or CLI) reconciles ONE deferral entry,
``local_git_repo_pending``: present while the folder is repo-less and no
choice is recorded, cleared on the first run that finds a repo or a recorded
choice. Under the launcher's own Update bundle the init runs BEFORE the
bundle install, so the entry never appears there for a folder it initialised;
it does appear for a folder holding nested repos (the launcher's project page
asks about that case), and the entry's text covers both.
"""

from __future__ import annotations

import json
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from vco_lib.git_meta import run_git
from vco_lib.launcher_db_reader import ORCHESTRATOR_CORE_MODULE_ID, sqlite_ro_uri

__all__ = ["CID", "PendingState", "pending_state", "reconcile"]

#: The deferral condition id (registry: ``vco_lib/deferral_conditions.toml``).
CID = "local_git_repo_pending"

#: MUST MATCH ``SETTING_KEY`` in launcher/src-tauri/src/commands/worktree_repo_mode.rs
#: — executable parity: both sides pin tests/fixtures/worktree_repo_mode_parity.json.
_SETTING_KEY = "worktree_repo_mode"

#: MUST MATCH ``VALID_MODES`` in launcher/src-tauri/src/commands/worktree_repo_mode.rs
#: (same parity fixture) — a stored value outside this set counts as "no choice
#: recorded", as there.
_VALID_MODES = frozenset(
    {"use_existing", "local_init", "no_repo", "use_existing_at", "use_existing_remote"}
)


class PendingState(Enum):
    """``PENDING`` / ``NOT_PENDING`` are answers; ``UNKNOWN`` = could not ask."""

    PENDING = "pending"
    NOT_PENDING = "not_pending"
    UNKNOWN = "unknown"


def _recorded_mode(folder: Path, db_path: Optional[Path]) -> tuple[bool, Optional[str]]:
    """``(could_ask, mode)``. No launcher.db, or the folder not registered, is
    an ANSWER (no choice recorded); an unreadable DB is ``could_ask=False``."""
    from vco_lib import module_gated_delivery as _mgd

    kind, project_id = _mgd._resolve_folder(folder, db_path=db_path)
    if kind in (_mgd._FOLDER_NO_DB, _mgd._FOLDER_UNREGISTERED):
        return True, None
    if kind != _mgd._FOLDER_REGISTERED or project_id is None:
        return False, None
    target = db_path if db_path is not None else _mgd._launcher_db_path()
    try:
        conn = sqlite3.connect(sqlite_ro_uri(target), uri=True, timeout=2.0)
    except sqlite3.Error:
        return False, None
    try:
        row = conn.execute(
            "SELECT setting_value FROM module_settings "
            "WHERE project_id = ? AND module_id = ? AND setting_key = ?",
            (project_id, ORCHESTRATOR_CORE_MODULE_ID, _SETTING_KEY),
        ).fetchone()
    except sqlite3.Error:
        return False, None
    finally:
        conn.close()
    if row is None:
        return True, None
    try:
        value = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return True, None
    return True, value if isinstance(value, str) and value in _VALID_MODES else None


def pending_state(folder: Path, *, db_path: Optional[Path] = None) -> PendingState:
    """Is this folder still waiting for the launcher's local git repo?"""
    rc, out, _err = run_git(folder, ["rev-parse", "--show-toplevel"])
    if rc is None:
        # git cannot run: the launcher cannot init either (it warns itself).
        return PendingState.UNKNOWN
    if rc == 0 and out:
        return PendingState.NOT_PENDING
    could_ask, mode = _recorded_mode(folder, db_path)
    if not could_ask:
        return PendingState.UNKNOWN
    return PendingState.NOT_PENDING if mode else PendingState.PENDING


def _entry(folder: Path) -> Any:
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id="local_git_repo_pending",
        title="No git repository yet — the VCO launcher creates one",
        detected=(
            "This project folder is not inside a git repository and no git choice "
            "is recorded for it, so subagents declared `isolation: worktree` run in "
            "the project folder itself, without isolation (VCO's worktree-guard "
            "hook returns the folder). A bundle update run from the command line does "
            "not create the repository; the VCO launcher does, on this project's "
            "next Update bundle: a local-only repository (no remote, nothing "
            "committed). If the folder already holds other git repositories, the "
            "launcher's project page asks you how to set one up instead."
        ),
        why_deferred=(
            "The decision (enclosing repo, nested repos, the user's recorded "
            "choice) and the repository's .gitignore guard live in the launcher; "
            "the command-line bundle path reports instead of keeping a second copy."
        ),
        command_to_apply=(
            "# Either open this project in the VCO launcher and click Update bundle,\n"
            "# or create the local-only repository yourself (no remote):\n"
            f"git -C {str(folder)!r} init --initial-branch=main\n"
            "# This entry clears itself on the next bundle update once a repository\n"
            "# exists or a git choice is recorded."
        ),
        severity="info",
    )


def reconcile(folder: Path, *, db_path: Optional[Path] = None, log: Any = None) -> str:
    """Emit or clear ``local_git_repo_pending`` for ``folder``; return the state.

    ``UNKNOWN`` leaves the ledger untouched (conservative: never add or drop an
    entry on a question that could not be asked).
    """
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralReport

    state = pending_state(folder, db_path=db_path)
    # Write only on a CHANGE: re-emitting an entry already on file would
    # rewrite the ledger (its generation timestamp) on every run.
    present = DeferralReport.read(folder).has_condition(CID)
    if state is PendingState.PENDING and not present:
        _de.emit(folder, _entry(folder), log=log)
    elif state is PendingState.NOT_PENDING and present:
        _de.resolve_conditions(folder, ["local_git_repo_pending"], log=log)
    return state.value


def reconcile_into_result(folder: Path, result: dict, *, log: Any = None) -> None:
    """The bundle install's call: reconcile and record the outcome in its
    ``result`` (key ``local_git_repo_pending``). Soft-fails to a warning —
    this check never fails a bundle run."""
    try:
        result["local_git_repo_pending"] = reconcile(folder, log=log)
    except Exception as exc:  # noqa: BLE001 — best-effort report
        result.setdefault("warnings", []).append(
            f"local git repo check failed: {type(exc).__name__}: {exc}"
        )
