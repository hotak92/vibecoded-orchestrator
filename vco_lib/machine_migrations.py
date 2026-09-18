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

Rules every leg here follows
-----------------------------
* **Soft-fail, always.** A migration that cannot run must not stop an
  install: it logs and the next run tries again. Nothing here is on the
  critical path of a working install.
* **Its own idempotence.** Each leg decides whether it has already run, from
  its own record. This module never keeps a "migrations applied" list, which
  would be a second source of truth about the same question.
* **Say what was changed to a USER-OWNED file.** The metrics copy is
  invisible bookkeeping under ``~/.vct``; removing a key from VS Code's
  ``settings.json`` is not, so that one prints one line naming the value it
  took and how to put it back. A silent edit of a file the user owns is the
  thing this rule exists to prevent.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

#: ``(step, phase, detail) -> None`` — ``install.py``'s ``_log_install_event``
#: signature, which is what every caller in the tree already has.
EventSink = Callable[[str, str, str], None]

#: Step ids used in the install event log. ``metrics_migration`` is unchanged
#: from v0.2.92 WP-D on purpose: a log reader that greps for it keeps working.
STEP_METRICS = "metrics_migration"
STEP_PANEL_PIN = "panel_default_pin_migration"


def _noop_event(step: str, phase: str, detail: str) -> None:  # pragma: no cover
    """Default sink: a caller that supplies none still gets the migrations."""


def run_every_run(
    *,
    on_event: Optional[EventSink] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Run every machine migration. Never raises.

    Args:
        on_event: ``install.py``'s ``_log_install_event``-shaped sink.
        emit: where a user-visible line goes; ``print`` by default. A caller
            that wants silence passes ``lambda _line: None`` rather than
            having this module guess from a flag it cannot see.

    Returns a per-leg result dict, so a caller (and a test) can assert what
    happened without reading the log.
    """
    event: EventSink = on_event or _noop_event
    say: Callable[[str], None] = emit if emit is not None else print
    return {
        "metrics_archive": _metrics_archive(event),
        "panel_default_pin": _panel_default_pin(event, say),
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


def _panel_default_pin(
    event: EventSink, say: Callable[[str], None],
) -> dict[str, Any]:
    """Q1 (USER RULING 2026-09-17): the panel's ``ANTHROPIC_MODEL`` pin, gone.

    The env pin outranks the model the user picks in Claude Code's GUI on
    every launch, so a machine that already carries one keeps overriding that
    choice no matter how carefully VCO declines to write new ones. This is the
    path that reaches an EXISTING machine, and it is why the migration hangs
    off install/update rather than off the panel button: a user who never
    presses that button again would otherwise never be fixed.

    Removals are PRINTED, not merely logged: the file belongs to the user.
    """
    try:
        from vco_lib.vscode_settings import migrate_default_pins

        result = migrate_default_pins()
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        event(
            STEP_PANEL_PIN, "warn",
            f"panel Default-pin migration could not run: {exc}",
        )
        return {"ok": False, "status": "error", "error": str(exc)}

    if result["status"] == "already-migrated":
        return result
    for one in result["removed"]:
        say(
            f"[vct] VS Code panel: removed the ANTHROPIC_MODEL pin "
            f"({one['value']}) from {one['path']} — the model you pick in "
            f"Claude Code now governs. To pin one on purpose again: "
            f"python -m vco_lib.vscode_settings point --path {one['path']} "
            f"--model {one['value']}"
        )
    for one in result["kept"]:
        say(
            f"[vct] VS Code panel: {one['path']} pins ANTHROPIC_MODEL="
            f"{one['value']}, which VCO never wrote and did not touch. A "
            "restarted panel resumes on it; the launcher's “Clear "
            "default” removes it."
        )
    event(
        STEP_PANEL_PIN,
        "ok" if result["ok"] else "warn",
        result["message"],
    )
    return result


__all__ = [
    "EventSink",
    "STEP_METRICS",
    "STEP_PANEL_PIN",
    "run_every_run",
]
