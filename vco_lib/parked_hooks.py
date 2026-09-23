# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hooks the user disabled from the launcher — what a bundle update must NOT
put back (v0.2.97).

THE DEFECT THIS CLOSES. Disabling a hook on the launcher's Hooks tab REMOVES
its entry from ``<project>/.claude/settings.json`` (``vco_lib.hooks_settings``
``disable``) and parks the removed bytes in ``launcher.db`` —
``project_hooks.disabled_entry_json``. The bundle merge
(:func:`vco_lib.settings_merge.merge_hooks_block`) then saw a SHIPPED
registration missing from settings.json and appended it again, so every
bundle update — the launcher's "Update bundle" button, "Update all", the CLI
``install-bundle --update``, the root install — switched the hook back on
while the Hooks tab still showed it Disabled. Agents and skills never had this
bug: a file found at a ``.disabled/`` location is skipped
(``project_init._agent_or_skill_already_present``).

WHERE THE TRUTH COMES FROM — the launcher DB, read directly. The parked row is
the ONLY record of a disable, and it is what the Hooks tab renders as
Disabled, so reading the same row makes "what the update keeps out" and "what
the tab shows as off" the same set by construction. It is read straight from
the file (read-only SQLite URI), never through vct-hub, so the answer is the
same whether the launcher is running, the hub is down, or the update came from
the CLI.

A durable in-project marker (a ``hooks.disabled.json`` next to settings.json,
the analogue of ``agents.disabled/``) was weighed and NOT built. It would
carry the choice to a clone on another machine, which the DB cannot. But the
DB would still hold the only restorable bytes, so the marker would be a second
home for "this hook is parked" with split writers — the Python writer would
own the marker while the Rust launcher/hub own the DB row, with no atomicity
between them — and a marker the launcher does not read would keep a hook out
of settings.json on that other machine while its Hooks tab showed nothing at
all: an invisible disable, the placebo the Hooks tab work set out to end.
Making it visible would mean teaching the launcher view, the enable path and
the hub routes a second store. The cross-machine case is therefore a stated
boundary: the choice lives on the machine that made it.

WHEN THE ANSWER CANNOT BE READ. :class:`ParkedHooksState` is tri-state in
effect:

  * ``readable`` and non-empty — keep exactly those registrations out;
  * ``readable`` and empty — nothing is parked. This includes NO launcher.db
    at all (nothing on this machine can have parked a hook), a DB whose schema
    predates parking, and a folder the DB does not register (unregistering a
    project cascades its hook rows away);
  * NOT ``readable`` — a DB exists but could not be asked (corrupt, locked
    past the timeout, the folder path would not resolve). The merge then
    re-adds NO missing shipped registration to an existing settings.json and
    the caller says so in a warning. Delaying a genuinely new shipped hook by
    one update (the next update with a readable DB adds it) is recoverable;
    switching back on a hook the user turned off is the defect itself, and
    for some hooks (one that calls out to a network, one that was
    misbehaving) it is not harmless. A settings.json being CREATED from
    scratch is the exception: there is no prior file whose absences could
    record a choice, so it gets every shipped hook and the warning tells the
    user to re-check the Hooks tab.

MATCHING — identity, not string equality. A parked row stores the command as
it was when disabled; the template may have moved on since (v0.2.97 dropped
the ``[ -n "$VCT_DISABLE_HOOKS" ] || `` prefix; v0.2.70 fixed path
separators). Two commands are the same hook when they invoke the same
``.claude/hooks/<name>`` script
(:func:`vco_lib.hook_retirements.vco_hook_script_identity` — the identity the
supersede pass already uses), or, for an inline command, when they are equal
after :func:`vco_lib.hook_retirements.normalize_command` (which strips that
guard prefix). The matcher must match too, because the template ships some
scripts under several matchers in one event (``kg-summary-generator.sh`` under
``Edit``, ``Write`` and a store tool) and disabling one of them must not keep
the others out. If the template has since CHANGED the matcher, the parked row
still matches as long as the template ships that hook under exactly one
matcher in the event — otherwise it is ambiguous which one the user meant, and
the registration is treated as not parked.

A hook the template RETIRED while it was parked simply matches nothing here:
the template no longer ships it, so there is nothing to keep out. Its parked
row is released by the launcher (``prune_retired_parked_rows``) and a restore
of it is refused by ``hooks_settings.insert_hook``.

PARKED AND RUNNING. Installs that took a bundle update before v0.2.97 carry
the damage: the hook the user disabled is registered again while its parked
row remains. A line the user put back by hand looks identical, so VCO never
removes the running entry; it records the state as the ``action_required``
deferral :data:`CONFLICT_CID` naming each hook and the two remedies, and the
registry probe clears it once no hook is both parked and running.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from vco_lib.hook_retirements import normalize_command, vco_hook_script_identity
from vco_lib.hooks_settings import normalize_matcher

__all__ = [
    "KEPT_OUT_PARKED",
    "KEPT_OUT_UNREADABLE",
    "ParkedHook",
    "ParkedHooksState",
    "find_parked_match",
    "read_parked_hooks",
    "CONFLICT_CID",
    "conflict_still_present",
    "emit_conflict_deferral",
    "find_live_conflicts",
    "report_parked_hooks",
    "same_hook_command",
]

#: ``reason`` values on a kept-out record (see :func:`report_parked_hooks`).
KEPT_OUT_PARKED = "parked"
KEPT_OUT_UNREADABLE = "parked_state_unreadable"


@dataclass(frozen=True)
class ParkedHook:
    """One parked row's natural key — the same key the launcher uses."""

    event: str
    matcher: str
    command: str


@dataclass(frozen=True)
class ParkedHooksState:
    """What the launcher has parked for one project folder.

    ``source`` names where the answer came from (``launcher_db``,
    ``no_launcher_db``, ``not_registered``, ``no_parking_schema``) or, when
    ``readable`` is False, ``unreadable``; ``detail`` carries the reason.
    """

    readable: bool
    hooks: tuple[ParkedHook, ...] = ()
    source: str = ""
    detail: str = ""
    db_path: str = ""


def same_hook_command(a: str, b: str) -> bool:
    """True when two hook commands are the same registration across eras."""
    if a == b:
        return True
    ident_a = vco_hook_script_identity(a)
    ident_b = vco_hook_script_identity(b)
    if ident_a is not None or ident_b is not None:
        return ident_a == ident_b
    return normalize_command(a) == normalize_command(b)


def _template_matchers_for(command: str, template_groups: Iterable[Any]) -> set[str]:
    matchers: set[str] = set()
    for group in template_groups:
        if not isinstance(group, dict):
            continue
        for item in group.get("hooks") or []:
            cmd = item.get("command") if isinstance(item, dict) else None
            if isinstance(cmd, str) and cmd and same_hook_command(cmd, command):
                matchers.add(normalize_matcher(group))
    return matchers


def find_parked_match(
    parked: Sequence[ParkedHook],
    event: str,
    matcher: str,
    command: str,
    template_groups: Iterable[Any],
) -> Optional[ParkedHook]:
    """The parked row that covers the template registration
    ``(event, matcher, command)``, or ``None``.

    ``template_groups`` is the template's group list for ``event``; it is only
    consulted when no row matches on the matcher, to decide whether a drifted
    matcher is unambiguous (see the module docstring).
    """
    candidates = [
        p for p in parked if p.event == event and same_hook_command(p.command, command)
    ]
    if not candidates:
        return None
    for p in candidates:
        if p.matcher == matcher:
            return p
    shipped_under = _template_matchers_for(command, template_groups)
    if len(shipped_under) != 1:
        return None
    # The template ships this hook under ONE matcher, so a parked row under a
    # matcher it no longer uses can only mean this registration.
    return candidates[0]


def _unreadable(db_path: Path, detail: str) -> ParkedHooksState:
    return ParkedHooksState(
        readable=False, source="unreadable", detail=detail, db_path=str(db_path),
    )


def read_parked_hooks(
    folder: Path, *, db_path: Optional[Path] = None,
) -> ParkedHooksState:
    """Read the hooks the launcher has parked for ``folder``. Never raises.

    Read-only: opens launcher.db through the house read-only URI and never
    writes. ``db_path`` defaults to :func:`vco_lib.paths.launcher_db_path`
    (``$VCT_LAUNCHER_DB_PATH`` > ``$VCT_STATE_DIR`` > ``~/.vct``).
    """
    from vco_lib.launcher_db_reader import sqlite_ro_uri
    from vco_lib.module_gated_delivery import project_id_for_folder_on_conn
    from vco_lib.paths import launcher_db_path

    target = db_path if db_path is not None else launcher_db_path()
    try:
        exists = target.exists()
        is_file = target.is_file()
    except OSError as exc:
        return _unreadable(target, f"cannot stat launcher.db: {exc}")
    if not exists:
        return ParkedHooksState(readable=True, source="no_launcher_db", db_path=str(target))
    if not is_file:
        return _unreadable(target, "launcher.db path is not a regular file")
    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError) as exc:
        return _unreadable(target, f"cannot resolve the project folder: {exc}")
    try:
        conn = sqlite3.connect(sqlite_ro_uri(target), uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return _unreadable(target, f"{type(exc).__name__}: {exc}")
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = (
            {row[1] for row in conn.execute("PRAGMA table_info(project_hooks)")}
            if "project_hooks" in tables
            else set()
        )
        if "projects" not in tables or "disabled_entry_json" not in columns:
            # A DB that cannot record a parked hook has none.
            return ParkedHooksState(
                readable=True, source="no_parking_schema", db_path=str(target),
            )
        project_id = project_id_for_folder_on_conn(conn, folder_canonical)
        if project_id is None:
            return ParkedHooksState(
                readable=True, source="not_registered", db_path=str(target),
            )
        rows = conn.execute(
            "SELECT event, matcher, command FROM project_hooks"
            " WHERE project_id = ? AND disabled_entry_json IS NOT NULL"
            " ORDER BY event, matcher, id",
            (project_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        return _unreadable(target, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return ParkedHooksState(
        readable=True,
        hooks=tuple(ParkedHook(str(e), str(m or ""), str(c)) for e, m, c in rows),
        source="launcher_db",
        db_path=str(target),
    )


def _describe(records: Sequence[dict], limit: int = 8) -> str:
    shown = [f"{r['event']} `{r['command']}`" for r in records[:limit]]
    if len(records) > limit:
        shown.append(f"... +{len(records) - limit} more")
    return "; ".join(shown)


def report_parked_hooks(
    folder: Path,
    result: dict,
    state: ParkedHooksState,
    kept_out: Sequence[dict],
    *,
    settings_action: str,
    dry_run: bool,
    log: Callable[..., Any],
) -> None:
    """Put the outcome where the user sees it: the result envelope, the
    install log, and — whenever the rule ran on an answer it could not read —
    ``result["warnings"]`` (printed as ``WARNING`` lines by the CLI and
    carried to the launcher). Then, on a real run, record any hook that is
    both parked AND running as a :data:`CONFLICT_CID` deferral
    (:func:`emit_conflict_deferral`).
    """
    if not dry_run and state.readable:
        emit_conflict_deferral(folder, state, log=log)
    if kept_out:
        result["parked_hooks_kept_out"] = [dict(r) for r in kept_out]
    parked = [r for r in kept_out if r.get("reason") == KEPT_OUT_PARKED]
    unreadable = [r for r in kept_out if r.get("reason") == KEPT_OUT_UNREADABLE]
    verb = "would stay" if dry_run else "stayed"
    if parked:
        log("4.bundle.settings.parked", "ok",
            f"{len(parked)} hook(s) disabled from the launcher {verb} disabled: "
            + _describe(parked),
            data={"kept_out": parked})
    if state.readable:
        return
    where = f"{state.db_path}: {state.detail}"
    if unreadable:
        result["warnings"].append(
            f"settings.json: the hooks you disabled from the launcher could not be "
            f"read ({where}), so {len(unreadable)} shipped hook registration(s) "
            f"missing from settings.json were NOT re-added — one of them may be a "
            f"hook you turned off: {_describe(unreadable)}. If you did not disable "
            f"them, make launcher.db readable and re-run the bundle update to add "
            f"them."
        )
    elif settings_action in ("created", "would-create"):
        result["warnings"].append(
            f"settings.json: created with every shipped hook, but the hooks you "
            f"disabled from the launcher could not be read ({where}) — if you had "
            f"disabled any for this project, check the Hooks tab and disable them "
            f"again."
        )
    log("4.bundle.settings.parked", "warn",
        f"parked-hook state unreadable ({where}); "
        f"{len(unreadable)} registration(s) withheld",
        data={"detail": state.detail, "withheld": unreadable})


# ---------------------------------------------------------------------------
# Parked AND running — the state an older update left behind
# ---------------------------------------------------------------------------

#: The deferral naming hooks that are parked in launcher.db while their
#: registration is ALSO live in settings.json. Declared in
#: ``vco_lib/deferral_conditions.toml`` (action_required, cleared by the probe
#: ``parked_hook_conflict_still_present``).
CONFLICT_CID = "parked_hook_live_conflict"


def find_live_conflicts(
    settings_hooks: Any, parked: Sequence[ParkedHook],
) -> list[dict]:
    """Parked rows whose hook is nevertheless registered in ``settings_hooks``.

    Same hook = same event, same matcher and :func:`same_hook_command` — the
    identity the rest of this module uses. Every bundle update before v0.2.97
    produced this state for each hook the user had disabled; so does a user
    who puts the line back by hand. VCO cannot tell those apart, which is why
    this is REPORTED and never repaired by removing the running entry.
    """
    if not isinstance(settings_hooks, dict):
        return []
    conflicts: list[dict] = []
    for row in parked:
        groups = settings_hooks.get(row.event)
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict) or normalize_matcher(group) != row.matcher:
                continue
            live = [
                item["command"] for item in group.get("hooks") or []
                if isinstance(item, dict) and isinstance(item.get("command"), str)
                and item["command"] and same_hook_command(item["command"], row.command)
            ]
            if live:
                conflicts.append({"event": row.event, "matcher": row.matcher,
                                  "parked_command": row.command, "live_command": live[0]})
                break
    return conflicts


def _live_conflicts(folder: Path, state: ParkedHooksState) -> Optional[list[dict]]:
    """Conflicts for ``folder``, or ``None`` when settings.json cannot be read.
    A missing settings.json registers nothing, so it has no conflicts."""
    path = folder / ".claude" / "settings.json"
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return find_live_conflicts(hooks or {}, state.hooks)


def conflict_still_present(folder: Path) -> Optional[bool]:
    """Clear probe for :data:`CONFLICT_CID` (tri-state, read-only).

    ``False`` only on positive evidence: the parked state AND settings.json
    were both read and no hook is in both. Anything unreadable is ``None``,
    which keeps the entry.
    """
    state = read_parked_hooks(folder)
    if not state.readable:
        return None
    conflicts = _live_conflicts(folder, state)
    if conflicts is None:
        return None
    return bool(conflicts)


def emit_conflict_deferral(
    folder: Path, state: ParkedHooksState, *, log: Callable[..., Any],
) -> bool:
    """Record every parked-and-running hook as ONE :data:`CONFLICT_CID` entry.

    Emits nothing when there is no conflict — clearing a stale entry is the
    registry probe's job (:func:`conflict_still_present`), which the bundle
    update and ``install.py --update`` both run. Returns whether it emitted.
    Never raises: the ledger is best-effort and must not fail an update.
    """
    conflicts = _live_conflicts(folder, state)
    if not conflicts:
        return False
    listed = "\n".join(
        f"  - {c['event']}"
        + (f" (matcher `{c['matcher']}`)" if c["matcher"] else "")
        + f": `{c['live_command']}`"
        for c in conflicts
    )
    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        emitted = emit(folder, DeferralEntry(
            condition_id=CONFLICT_CID,
            title="A hook you disabled from the launcher is running",
            detected=(
                f"{len(conflicts)} hook(s) are parked as disabled in the launcher "
                f"AND registered in .claude/settings.json, so they run:\n{listed}\n"
                "Bundle updates before v0.2.97 re-added hooks you had disabled; "
                "putting the line back by hand produces the same state."
            ),
            why_deferred=(
                "VCO cannot tell whether an older update re-added the hook or you "
                "turned it back on yourself, so it does not remove a running entry "
                "for you. You decide which state you want."
            ),
            command_to_apply=(
                "Open the launcher -> Projects -> this project -> Hooks. "
                "To keep the hook OFF: turn off the row that shows it running. "
                "To keep it ON: click Enable on its Disabled row (VCO sees the hook "
                "is already registered, adds nothing, and drops the stale parked "
                "entry); if the tab shows no Disabled row for it, turn it off and "
                "on again. This entry clears itself on the next update once no "
                "hook is both parked and running."
            ),
            severity="warning",
        ))
    except Exception as exc:  # noqa: BLE001 — ledger I/O is best-effort
        emitted, why = False, str(exc)
    else:
        why = "the deferral ledger could not be written"
    if not emitted:
        log("4.bundle.settings.parked", "warn",
            f"could not record the parked-and-running hooks: {why}")
        return False
    log("4.bundle.settings.parked", "warn",
        f"{len(conflicts)} hook(s) parked as disabled but running; "
        f"recorded as {CONFLICT_CID}",
        data={"conflicts": conflicts})
    return True
