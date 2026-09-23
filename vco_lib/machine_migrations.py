# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One-time, MACHINE-level migrations, run on every install and update.

Not project migrations (those belong to the bundle engine) and not schema
migrations (those are consent-gated and never automatic). These are the
small, one-way corrections a machine needs exactly once, where "once" is
enforced by each migration's own record rather than by the caller: a legacy
metrics archive copied forward, a settings key a ruling retired.

Why a module and not another block in ``install.py``
-----------------------------------------------------
``install.py`` is the project's mega-file and its size is ratcheted (see
``tests/test_install_main_ratchet.py``: "never raise the pin to fit a change
that could have been an extraction"). Its side of this is one helper that
resolves ``_log_install_event`` and calls :func:`run_every_run`; everything
below — the order, the soft-fail, the disclosure — lives here, where it can
be driven by a test without running an installer.

Where install.py calls it (v0.2.97), always once the venv exists — the panel
leg runs in it:

* a full install or ``--update``: in ``main()`` right AFTER step 5 (venv
  created, requirements and the editable ``claude_mcp_servers`` package
  installed), so a FIRST install is covered as well as an update. Before
  v0.2.97 the call sat ahead of venv creation;
* ``--lightweight`` (the launcher's re-install / relocate path): in
  ``_run_lightweight`` after venv triage, into that run's own deferral report;
* ``--uninstall``: NOT called, deliberately — removing VCO is no moment to
  rewrite the user's VS Code settings, and the ledger would land in the very
  state being removed. main() dispatches it before step 5.

Rules every leg here follows
-----------------------------
* **Soft-fail, always.** A migration that cannot run must not stop an
  install: the next run tries again. Nothing here is on the critical path of
  a working install. Soft-fail is not SILENT-fail, though: a leg that could
  not do its job says so where the user looks (below).
* **Its own idempotence.** Each leg decides whether it has already run, from
  its own record. This module never keeps a "migrations applied" list, which
  would be a second source of truth about the same question.
* **Say what was changed to a USER-OWNED file.** The metrics copy is
  invisible bookkeeping under ``~/.vct``; removing a key from VS Code's
  ``settings.json`` is not. A removal is printed (the CLI surface), logged,
  and written as an auto-resolution record naming the value, the backup and
  the command that puts it back. A leg that COULD NOT run is printed, logged
  and recorded in ``UPDATE_DEFERRED.md`` — the ledger the launcher and every
  session start surface — because a launcher-driven update shows neither
  install.py's plain stdout nor a ``warn`` event (``progress_event``: only
  ``start``/``ok`` reach the GUI). Three releases of this leg failing into
  ``install.jsonl`` alone is the incident that rule comes from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from vco_lib.child_process import last_json_object

if TYPE_CHECKING:  # pragma: no cover — typing only
    from vco_lib.deferral_report import DeferralReport

#: ``(step, phase, detail) -> None`` — ``install.py``'s ``_log_install_event``
#: signature, which is what every caller in the tree already has.
EventSink = Callable[[str, str, str], None]

#: Step ids used in the install event log. ``metrics_migration`` is unchanged
#: from v0.2.92 WP-D on purpose: a log reader that greps for it keeps working.
STEP_METRICS = "metrics_migration"
STEP_PANEL_PIN = "panel_default_pin_migration"

#: Deferral raised when the panel leg could not do its job (no venv, the
#: child failed, a settings file it could not read). Registered in
#: ``deferral_conditions.toml`` as ``paired-resolution``: the run whose
#: migration succeeds clears it (:func:`_panel_default_pin`).
CID_PANEL_PIN_FAILED = "vscode_default_pin_migration_failed"

#: Auto-resolution record id for a removal — a trail row, not a deferral
#: (nothing is owed; the ``bundle_retired_hook_registration`` shape).
AUTO_RESOLUTION_PANEL_PIN = "vscode_default_pin_removed"

#: The child re-reads a handful of small JSON files; two minutes is a hang,
#: not a slow disk.
PIN_CHILD_TIMEOUT_S = 120


def _noop_event(step: str, phase: str, detail: str) -> None:  # pragma: no cover
    """Default sink: a caller that supplies none still gets the migrations."""


def run_every_run(
    *,
    on_event: Optional[EventSink] = None,
    emit: Optional[Callable[[str], None]] = None,
    install_root: Optional[Path] = None,
    report: Optional["DeferralReport"] = None,
) -> dict[str, Any]:
    """Run every machine migration. Never raises.

    Args:
        on_event: ``install.py``'s ``_log_install_event``-shaped sink.
        emit: where a user-visible line goes; ``print`` by default. A caller
            that wants silence passes ``lambda _line: None`` rather than
            having this module guess from a flag it cannot see.
        install_root: the orchestrator checkout whose venv runs the panel leg
            (``install.py``'s ``PROJECT_ROOT``). Defaults to the checkout this
            ``vco_lib`` was imported from.
        report: the run's :class:`DeferralReport`. A leg that could not run
            records itself there; ``None`` leaves only the printed line + log.

    Returns a per-leg result dict, so a caller (and a test) can assert what
    happened without reading the log.
    """
    event: EventSink = on_event or _noop_event
    say: Callable[[str], None] = emit if emit is not None else print
    root = Path(install_root) if install_root is not None else Path(__file__).resolve().parents[1]
    return {
        "metrics_archive": _metrics_archive(event),
        "panel_default_pin": _panel_default_pin(event, say, root, report),
    }


def _metrics_archive(event: EventSink) -> dict[str, Any]:
    """v0.2.92 WP-D (register item 20): the legacy metrics-archive copy.

    Was SessionStart-hook-only, so the history copy ran late for frequent
    updaters. COPY-never-MOVE; cheap enough to call on every run.
    """
    try:
        from vco_lib.metrics_migration import ensure_metrics_migrated

        result = ensure_metrics_migrated()
        if result.status == "failed":
            event(
                STEP_METRICS, "warn",
                f"metrics migration failed: {'; '.join(result.errors)}",
            )
        return {"ok": result.status != "failed", "status": result.status}
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        event(STEP_METRICS, "warn", f"metrics migration could not run: {exc}")
        return {"ok": False, "status": "error", "error": str(exc)}


class _PinLegFailed(Exception):
    """The panel leg could not run; the message is the user-facing reason."""


def _run_pin_child(install_root: Path) -> dict[str, Any]:
    """Run ``python -m vco_lib.vscode_settings migrate-default-pin`` in the VENV.

    In the install's venv, never in-process. ``vscode_settings`` imports
    ``model_router`` (the editable ``claude_mcp_servers`` package), which only
    the venv provides; install.py's own interpreter is whatever the launcher
    found on PATH, and three releases of this leg died on exactly that import
    (``No module named 'model_router'``). ``cwd`` is the checkout so ``-m``
    resolves ITS ``vco_lib`` ahead of anything in site-packages.

    Raises :class:`_PinLegFailed` with the reason when the child cannot give
    an answer.
    """
    from vco_lib import install_companions

    venv_python = install_companions.resolve_install_venv_python(install_root)
    if venv_python is None:
        raise _PinLegFailed(
            f"no venv interpreter under {install_root} (looked for .venv and "
            "claude_mcp_servers/.venv)"
        )
    cmd = [str(venv_python), "-m", "vco_lib.vscode_settings", "migrate-default-pin"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(install_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PIN_CHILD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _PinLegFailed(f"`{' '.join(cmd)}` did not run: {exc}") from exc
    payload = last_json_object(proc.stdout)
    if payload is None:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise _PinLegFailed(
            f"`{' '.join(cmd)}` exited {proc.returncode} without a result"
            + (f": {tail}" if tail else "")
        )
    return payload


def _panel_default_pin(
    event: EventSink,
    say: Callable[[str], None],
    install_root: Path,
    report: Optional["DeferralReport"],
) -> dict[str, Any]:
    """Q1 (USER RULING 2026-09-17): the panel's ``ANTHROPIC_MODEL`` pin, gone.

    The env pin outranks the model the user picks in Claude Code's GUI on
    every launch, so a machine that already carries one keeps overriding that
    choice no matter how carefully VCO declines to write new ones. This is the
    path that reaches an EXISTING machine, and it is why the migration hangs
    off install/update rather than off the panel button: a user who never
    presses that button again would otherwise never be fixed.

    What counts as "ours" (first-party ids only), the once-per-machine ledger
    and the backup all live in :func:`vco_lib.vscode_settings.migrate_default_pins`;
    this leg runs it and reports.
    """
    try:
        result = _run_pin_child(install_root)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        return _pin_leg_could_not_run(event, say, report, str(exc))

    if result.get("status") == "already-migrated":
        # The migration has run on this machine (the ledger says so), so an
        # entry a failed earlier run left behind is no longer true.
        if report is not None:
            report.mark_resolved(CID_PANEL_PIN_FAILED)
        return result
    removed = [t for t in result.get("targets", []) if t.get("status") == "removed"]
    refused = [t for t in result.get("targets", []) if t.get("status") == "refused"]
    for one in removed:
        _report_removal(say, install_root, one)
    for one in result.get("kept", []):
        say(
            f"[vct] VS Code panel: {one['path']} pins ANTHROPIC_MODEL="
            f"{one['value']}, which VCO never wrote and did not touch. A "
            "restarted panel resumes on it; the launcher's “Clear "
            "default” removes it."
        )
    if refused:
        # The ledger records these as ``refused``, and ``migrate_default_pins``
        # retries exactly those files on every later run; the entry stands
        # until a run reads them (or the user dismisses it).
        detail = "; ".join(f"{t['path']}: {t.get('message') or t.get('reason')}" for t in refused)
        _pin_leg_could_not_run(
            event, say, report,
            f"could not check {len(refused)} settings file(s) for a pin; every "
            f"update retries them — {detail}",
        )
        result["ok"] = False
        return result
    if report is not None:
        report.mark_resolved(CID_PANEL_PIN_FAILED)
    event(
        STEP_PANEL_PIN,
        "ok" if result.get("ok", True) else "warn",
        str(result.get("message", "")),
    )
    return result


def _report_removal(say: Callable[[str], None], install_root: Path, one: dict) -> None:
    """Print + auto-resolution trail for one removed pin: value, backup, undo."""
    restore = (
        f"python -m vco_lib.vscode_settings point --path {one['path']} "
        f"--model {one['value']}"
    )
    backup = one.get("backup_path") or "none written"
    say(
        f"[vct] VS Code panel: removed the ANTHROPIC_MODEL pin "
        f"({one['value']}) from {one['path']} (backup: {backup}) — the model "
        f"you pick in Claude Code now governs. To pin one on purpose again: "
        f"{restore}"
    )
    try:
        from vco_lib.deferral_emit import record_auto_resolution

        record_auto_resolution(
            install_root,
            AUTO_RESOLUTION_PANEL_PIN,
            "removed the pre-ruling ANTHROPIC_MODEL pin",
            f"ANTHROPIC_MODEL={one['value']} removed from {one['path']}; "
            f"backup: {backup}; to pin it again: {restore}",
        )
    except Exception:  # noqa: BLE001 — the trail is observability, never a gate
        pass


def _pin_leg_could_not_run(
    event: EventSink,
    say: Callable[[str], None],
    report: Optional["DeferralReport"],
    reason: str,
) -> dict[str, Any]:
    """Soft-fail, but LOUD: printed, logged, and recorded where the user looks."""
    say(
        "[vct] VS Code panel: the one-time ANTHROPIC_MODEL pin migration did "
        f"not complete ({reason}). See UPDATE_DEFERRED.md "
        f"({CID_PANEL_PIN_FAILED})."
    )
    event(STEP_PANEL_PIN, "warn", f"panel Default-pin migration could not run: {reason}")
    if report is not None:
        try:
            from vco_lib.deferral_report import DeferralEntry

            report.add_entry(
                DeferralEntry(
                    condition_id=CID_PANEL_PIN_FAILED,
                    title="VS Code panel: the one-time Default-pin migration did not complete",
                    detected=reason,
                    why_deferred=(
                        "VCO no longer pins the panel's Default model (owner "
                        "ruling 2026-09-17): the model you pick in Claude Code's "
                        "GUI is meant to govern. A pre-ruling ANTHROPIC_MODEL "
                        "pin in VS Code's settings.json outranks that choice on "
                        "every launch, and the update step that removes it once "
                        "could not finish. The rest of the update was not "
                        "affected."
                    ),
                    command_to_apply=(
                        "# If the migration did not RUN, run it by hand from the orchestrator root:\n"
                        ".venv/bin/python -m vco_lib.vscode_settings migrate-default-pin\n"
                        "# (Windows: .venv\\Scripts\\python.exe -m vco_lib.vscode_settings "
                        "migrate-default-pin)\n"
                        "# If it could not CHECK a settings file, the reason is named above: fix\n"
                        "# it (every update retries that file), or delete the ANTHROPIC_MODEL\n"
                        "# entry under claudeCode.environmentVariables yourself, if there is one.\n"
                        "# An update on which the migration completes clears this entry. To silence it:\n"
                        "#   python -m vco_lib.project_init dismiss-deferral --folder . "
                        f"--condition-id {CID_PANEL_PIN_FAILED}"
                    ),
                    severity="warning",
                )
            )
        except Exception as exc:  # noqa: BLE001 — never let reporting break an install
            event(STEP_PANEL_PIN, "warn", f"could not record the deferral: {exc}")
    return {"ok": False, "status": "error", "error": reason}


__all__ = [
    "AUTO_RESOLUTION_PANEL_PIN",
    "CID_PANEL_PIN_FAILED",
    "EventSink",
    "STEP_METRICS",
    "STEP_PANEL_PIN",
    "run_every_run",
]
