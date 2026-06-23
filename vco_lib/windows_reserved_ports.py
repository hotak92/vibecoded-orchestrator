# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Windows reserved-port-range detection (vco_lib.windows_reserved_ports — v0.2.64).

The genuine bug this closes:

    On Windows, WinNAT / Hyper-V (and the "dynamicport" Winsock range)
    auto-allocate a block of TCP ports — e.g. 11410-11509 — and mark them
    EXCLUDED. Any port inside an excluded range can no longer be bound by a
    normal process. VCO's default ports 11435 (ollama) and 11440 (code_embed)
    sometimes land inside that window. The container starts ("Up" in
    `docker ps`) but the host-side port forward silently FAILS to bind, so the
    KG goes mute with NoEmbeddingBackendError and `docker compose up` errors:

        Error: ports are not available: exposing port TCP 0.0.0.0:11435 ->
        ...: bind: An attempt was made to access a socket in a way forbidden
        by its access permissions.

    The reserved range MOVES across Windows updates / reboots, so a port that
    worked yesterday can be swallowed tomorrow.

Why install.py's existing detection misses it:

    install.py::_detect_existing_services / _probe_service_identity classify a
    port conflict by "is something listening here?" (an HTTP probe). A reserved
    range INVERTS that signature: NOTHING is listening (the probe says
    not-running → VCO proceeds to `compose up`), but the OS still refuses the
    bind. So the foreign-service → alt-port path never fires.

This module supplies the missing piece: parse the OS's own list of excluded
ranges (`netsh int ipv4 show excludedportrange tcp`) and decide whether a
given port falls inside one. The parse logic (`parse_excluded_ranges`,
`port_in_reserved_range`) is pure and unit-testable without touching Windows;
the OS-coupled bits (`query_excluded_ranges`, `is_elevated`,
`reserve_port_persistent`, `check_ports`) are guarded so the whole module is a
clean no-op on Linux/macOS.

Single source of truth: install.py (install-time, before compose up) AND
templates/hooks/ensure-containers.ps1 (session-start, returning users) both
warn about the same condition. The PowerShell hook re-implements the netsh
parse natively (it cannot import Python pre-venv), with a "must match
vco_lib/windows_reserved_ports.py" comment so the two can't drift.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import List, Optional, Tuple

# A reserved range as (start_port, end_port), inclusive on both ends — which is
# how `netsh` reports them ("Start Port" + "Number of Ports", expanded here to
# an inclusive end).
PortRange = Tuple[int, int]


def is_windows() -> bool:
    """True only on Windows. The single OS gate every public entry point
    checks first so Linux/macOS callers short-circuit to a no-op."""
    return sys.platform.startswith("win")


def parse_excluded_ranges(netsh_output: str) -> List[PortRange]:
    """Parse `netsh int ipv4 show excludedportrange tcp` into inclusive ranges.

    The command's output looks like (whitespace/locale aside)::

        Protocol tcp Port Exclusion Ranges

        Start Port    End Port
        ----------    --------
             11410       11509
             50000       50059
             ...

        * - Administered port exclusions.

    Newer Windows builds and some locales render the same data as
    "Start Port" / "Number of Ports" instead of an explicit "End Port". We
    handle the canonical two-number-per-row shape: each data row carries two
    integers. The FIRST is always the start port; the SECOND is interpreted as
    an *end port* when it is >= start (the common "Start/End" layout), otherwise
    as a *count* (the "Start Port / Number of Ports" layout) — a count is never
    larger than the start port in practice, and treating an ambiguous small
    second number as a count is the conservative reading (it widens, never
    narrows, the reserved set we warn about).

    Pure function: no I/O, no platform calls — safe to unit-test anywhere.

    Args:
        netsh_output: Raw stdout from the netsh command (any locale; we only
            key off the digit pairs, not the English header text).

    Returns:
        A list of inclusive (start, end) tuples, in the order encountered.
        Empty list when no data rows parse (header-only output, error text,
        or empty string).
    """
    ranges: List[PortRange] = []
    # Match exactly two whitespace-separated integers on a line, ignoring any
    # leading/trailing whitespace. The header rows ("Start Port  End Port",
    # the dashes, the asterisk footnote) contain no bare integer pairs, so they
    # never match. This keeps us locale-independent — we don't parse English.
    row_re = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")
    for line in netsh_output.splitlines():
        m = row_re.match(line)
        if not m:
            continue
        first = int(m.group(1))
        second = int(m.group(2))
        if first <= 0 or first > 65535:
            # Not a plausible port row — skip defensively.
            continue
        if second >= first and second <= 65535:
            # Start/End layout: second is the (inclusive) end port.
            start, end = first, second
        else:
            # Start/Count layout: second is a count of ports from `first`.
            start = first
            end = first + max(second, 1) - 1
        if end > 65535:
            end = 65535
        ranges.append((start, end))
    return ranges


def port_in_reserved_range(port: int, ranges: List[PortRange]) -> Optional[PortRange]:
    """Return the first reserved range that contains *port*, else None.

    Pure function. Inclusive on both ends.

    Args:
        port: The TCP port to test.
        ranges: Reserved ranges as returned by `parse_excluded_ranges`.

    Returns:
        The matching (start, end) tuple, or None when *port* is free of every
        reserved range.
    """
    for start, end in ranges:
        if start <= port <= end:
            return (start, end)
    return None


def query_excluded_ranges() -> List[PortRange]:
    """Run netsh and return the parsed reserved ranges. Windows-only.

    Soft-fail everywhere: returns an empty list on non-Windows, when netsh is
    missing, on timeout, or on any subprocess error. An empty list means
    "we could not positively confirm any reservation" → callers do nothing
    (conservative default — never block install/session on a probe failure).
    """
    if not is_windows():
        return []
    try:
        completed = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            # netsh is on the system PATH; no shell needed (and shell=False
            # avoids quoting hazards).
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return parse_excluded_ranges(completed.stdout or "")


def is_elevated() -> bool:
    """True when the current process has Administrator rights. Windows-only.

    Used to decide between "fix it automatically" (admin) and "print the exact
    admin commands for the user to run" (non-admin). Soft-fail to False (the
    non-destructive branch) on any error.
    """
    if not is_windows():
        return False
    try:
        import ctypes  # noqa: PLC0415 — lazy import; Windows-only path

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def reserve_command(port: int) -> str:
    """The exact `netsh` command that persistently reserves a single port for
    application use, so WinNAT's dynamic allocator stops swallowing it.

    Returned as a string for both (a) running it (admin branch) and (b)
    printing it for the user to copy-paste (non-admin branch) — one source of
    truth for the command text so the warning and the action can't diverge.
    """
    return (
        f"netsh int ipv4 add excludedportrange protocol=tcp "
        f"startport={port} numberofports=1 store=persistent"
    )


def reserve_port_persistent(port: int) -> bool:
    """Persistently reserve *port* for application use (requires admin).

    This is the standard remediation: claiming the single port as an
    application-reserved exclusion stops the WinNAT dynamic allocator from
    handing that exact port out, so the container's host bind succeeds. The
    reservation persists across reboots (`store=persistent`).

    Returns True on success, False otherwise (non-Windows, not elevated, netsh
    error). Never raises — soft-fail so a flaky netsh never aborts install.
    """
    if not is_windows():
        return False
    if not is_elevated():
        return False
    try:
        completed = subprocess.run(
            [
                "netsh", "int", "ipv4", "add", "excludedportrange",
                "protocol=tcp", f"startport={port}", "numberofports=1",
                "store=persistent",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def check_ports(
    ports: List[Tuple[str, int]],
    *,
    auto_reserve: bool = True,
    log=None,
) -> List[Tuple[str, int, PortRange]]:
    """Detect and (when elevated) remediate Windows reserved-range conflicts.

    The single entry point install.py calls. For each (label, port) pair, test
    whether the port falls inside a WinNAT/Hyper-V reserved range. On a hit:

      * If elevated AND *auto_reserve*: reserve the port (the standard fix) and
        log what was done. If the reservation succeeds the conflict is treated
        as resolved (not returned to the caller).
      * Otherwise: log a CLEAR, actionable warning with the exact admin command
        the user must run, and return the conflict so the caller can surface it.

    Cross-OS safety: an instant no-op on Linux/macOS (`is_windows()` gate →
    `query_excluded_ranges()` returns []). Soft-fail: a probe error yields zero
    reserved ranges → zero conflicts → nothing happens.

    Args:
        ports: (human-label, port) pairs to check — e.g. [("ollama", 11435)].
        auto_reserve: When True and elevated, reserve conflicting ports rather
            than only warning. Set False to force warn-only (e.g. a hook that
            should never mutate system state).
        log: Optional callable taking one string (defaults to print). Lets the
            caller route messages through its own logger.

    Returns:
        The list of UNRESOLVED conflicts as (label, port, reserved_range)
        tuples. Empty when there were no conflicts OR every conflict was
        auto-reserved successfully.
    """
    emit = log if callable(log) else print

    if not is_windows():
        return []

    ranges = query_excluded_ranges()
    if not ranges:
        # No data (non-Windows handled above; here it's netsh-missing /
        # error / genuinely no reservations) → conservatively do nothing.
        return []

    unresolved: List[Tuple[str, int, PortRange]] = []
    elevated = is_elevated()

    for label, port in ports:
        hit = port_in_reserved_range(port, ranges)
        if hit is None:
            continue
        start, end = hit
        if auto_reserve and elevated:
            if reserve_port_persistent(port):
                emit(
                    f"  [reserved-port] {label} port {port} was inside the "
                    f"Windows reserved range {start}-{end}; reserved it for "
                    f"application use (netsh excludedportrange, persistent). "
                    f"You may need to restart the WinNAT service "
                    f"(`net stop winnat && net start winnat`) for it to take "
                    f"effect."
                )
                continue
            # Reservation attempted but failed — fall through to the warning.
            emit(
                f"  [reserved-port] {label} port {port} is inside the Windows "
                f"reserved range {start}-{end} and the automatic reservation "
                f"failed."
            )
        else:
            emit(
                f"  [reserved-port] WARNING: {label} port {port} is inside a "
                f"Windows reserved TCP range ({start}-{end}). The container "
                f"will start but the host port WILL NOT bind, and the "
                f"knowledge graph will go silently mute."
            )
        # Non-admin (or failed auto-reserve): print the actionable fix.
        emit("  [reserved-port] To fix, run this in an ELEVATED (Administrator) terminal:")
        emit(f"      {reserve_command(port)}")
        emit("      net stop winnat && net start winnat")
        unresolved.append((label, port, hit))

    return unresolved
