# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``install.py --update``'s model-gateway re-render, out of the monolith.

One function, called from one place (``install._rerender_model_gateway_boot_service``),
living here rather than in a 24k-line ``install.py`` because the repo's
modularity rule says so: new logic past a handful of lines belongs in a module,
and this carries a decision (what a REFUSAL means and who is told) that wants to
be readable and unit-testable without importing the installer.

What it is for
--------------
The gateway's login-time autostart is OPT-IN, so this creates nothing: it only
refreshes a registration the user already asked for, whose absolute paths were
baked in when they asked. Without it, a clone that moved leaves an ``ExecStart``
pointing at a path that no longer exists and the gateway silently stops coming
up — the failure the container stack's unit repair exists to prevent.

v0.2.95 (R5a) added the half that matters more: the entry point is resolved from
the INSTALL ROOT's venv and RUN (``--version``) before anything is written.
Until then this step baked whichever interpreter happened to run the installer —
on 2026-09-10 a system python that cannot import ``model_router``, which made
the re-rendered unit unrunnable from the moment it was written, for every user
who had opted in, silently, because the previous process kept serving.

Soft-fail, and what a refusal owes the user
-------------------------------------------
Nothing here may block an install, on any OS. A refusal leaves the existing
registration byte-identical and is reported to the install log; the LEDGER row
is not written here on purpose — the doctor phase later in the same run probes
the same state and owns the ``gateway_registered_but_unrunnable`` lifecycle, so
emitting it from here as well would fork that lifecycle for one condition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from vco_lib import boot_service

__all__ = ["rerender_on_update"]

#: How the install log is addressed. ``install._log_install_event`` takes
#: ``(phase, level, detail)``; keeping the phase here means the caller passes
#: the logger, not the vocabulary.
_LOG_PHASE = "boot-service"


def rerender_on_update(
    *,
    update: bool,
    templates_root: Path,
    install_root: "str | Path | None" = None,
    on_event: Optional[Callable[..., Any]] = None,
    log: Optional[Callable[[str, str, str], Any]] = None,
) -> "Optional[boot_service.GatewayRegistration]":
    """Refresh an EXISTING model-gateway registration. Returns ``None`` if none.

    Args:
        update: the ``--update`` flag. False means this is a plain install and
            nothing is touched — the gate is here rather than at the call site
            so it cannot be forgotten by a second caller.
        templates_root: the clone whose ``templates/`` the unit is rendered
            from (install.py passes its own ``PROJECT_ROOT``).
        install_root: the clone whose venv must be able to run the gateway.
        on_event: the boot-service event sink (phase, detail, data).
        log: ``(phase, level, detail)`` — the install log.

    Never raises: an exception in an autostart refresh must not fail an
    install that has otherwise succeeded.
    """
    if not update:
        return None

    def _log(level: str, detail: str) -> None:
        if log is not None:
            log(_LOG_PHASE, level, detail)

    try:
        outcome = boot_service.register_model_gateway(
            templates_root=Path(templates_root),
            on_event=on_event or boot_service._noop_event,
            update_only=True,
            install_root=install_root,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail catch-all
        _log(
            "warn",
            f"model-gateway boot re-render raised: {exc.__class__.__name__}: {exc}",
        )
        return None

    if outcome.refused:
        _log(
            "warn",
            "model-gateway boot unit left UNCHANGED — no entry point on this "
            f"machine could be verified: {outcome.reason}",
        )
    return outcome
