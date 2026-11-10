# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Is a usable PowerShell present on Windows — and WHY not, when it is not.

The Windows install gate (`install.py::_check_windows_shell_prereqs`, audit
F1) needs PowerShell 5.1+ on PATH for the shipped ``.ps1`` hooks. Its probe
used to answer one boolean: a local ``pwsh``/``powershell`` that did not
answer within 8 s was folded into "absent", so an install under load refused
to run on a machine whose PowerShell was installed (v0.2.106 field report,
Windows). This module keeps the three answers apart:

* :data:`STATE_OK` — a launcher answered with 5.1+;
* :data:`STATE_MISSING` — neither ``pwsh`` nor ``powershell`` resolves on PATH;
* :data:`STATE_TOO_OLD` — one answered, but below 5.1;
* :data:`STATE_NO_ANSWER` — one resolves, but the version could not be read
  (a timeout, a non-zero exit, unparseable output). This is NOT absence:
  the caller must word it as "could not confirm" and must not refuse the
  install on it.

A timeout is retried once with a longer bound before it is reported, because
the 8 s default is generous on an idle machine and tight beside a busy
compiler. There is deliberately no global retry loop: one probe, two
attempts.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_TOO_OLD = "too_old"
STATE_NO_ANSWER = "no_answer"

#: First-attempt ceiling. The pre-v0.2.106 probe used 8 s and folded its
#: timeout into "absent".
DEFAULT_TIMEOUT_S = 8.0
#: Retry ceiling after a timeout — a loaded machine can take this long to
#: cold-start Windows PowerShell.
DEFAULT_RETRY_TIMEOUT_S = 25.0
#: The floor the shipped ``.ps1`` hooks need.
MINIMUM_VERSION = (5, 1)

_EXES = ("pwsh", "powershell")


@dataclass(frozen=True)
class PowerShellProbe:
    """The probe's answer. ``version`` is set only for OK/TOO_OLD."""

    state: str
    version: Optional[Tuple[int, int]] = None
    exe: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    def version_text(self) -> str:
        if self.version is None:
            return "unknown"
        return f"{self.version[0]}.{self.version[1]}"


def _run_version(
    run: Callable[..., "subprocess.CompletedProcess[str]"], exe: str, timeout: float,
) -> "subprocess.CompletedProcess[str]":
    return run(
        [exe, "-NoProfile", "-Command",
         "$PSVersionTable.PSVersion.Major; $PSVersionTable.PSVersion.Minor"],
        capture_output=True, text=True, timeout=timeout,
    )


def _attempt(
    run: Callable[..., "subprocess.CompletedProcess[str]"], exe: str,
    timeout: float, retry_timeout: float,
) -> "tuple[str, Optional[Tuple[int, int]], str]":
    """One executable, up to two attempts. Returns (state, version, detail)."""
    problems: "list[str]" = []
    for bound in (timeout, retry_timeout):
        if bound <= 0:
            continue
        try:
            result = _run_version(run, exe, bound)
        except subprocess.TimeoutExpired:
            problems.append(f"did not answer within {int(bound)}s")
            continue
        except (subprocess.SubprocessError, OSError) as exc:
            # Could not run it at all — still not interpreted as absence.
            problems.append(f"could not be run ({exc})")
            continue
        if result.returncode != 0:
            problems.append(f"exited {result.returncode}")
            continue
        nums = [int(x) for x in (result.stdout or "").split() if x.strip().isdigit()]
        if len(nums) < 2:
            problems.append("answered without a parseable version")
            continue
        version = (nums[0], nums[1])
        if version >= MINIMUM_VERSION:
            return STATE_OK, version, ""
        return STATE_TOO_OLD, version, (
            f"{exe} is PowerShell {version[0]}.{version[1]}, below the "
            f"{MINIMUM_VERSION[0]}.{MINIMUM_VERSION[1]} floor"
        )
    return STATE_NO_ANSWER, None, f"{exe} " + "; then ".join(problems)


def probe(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retry_timeout_s: float = DEFAULT_RETRY_TIMEOUT_S,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> PowerShellProbe:
    """Probe ``pwsh`` (preferred) then ``powershell`` and classify the result.

    ``which``/``run`` are injection seams for the tests. Never raises.
    """
    too_old: Optional[PowerShellProbe] = None
    no_answer: Optional[PowerShellProbe] = None
    for exe in _EXES:
        try:
            found = which(exe)
        except OSError:
            continue
        if not found:
            continue
        state, version, detail = _attempt(run, exe, timeout_s, retry_timeout_s)
        if state == STATE_OK:
            return PowerShellProbe(STATE_OK, version, exe, "")
        if state == STATE_TOO_OLD:
            too_old = too_old or PowerShellProbe(STATE_TOO_OLD, version, exe, detail)
            continue
        no_answer = no_answer or PowerShellProbe(STATE_NO_ANSWER, None, exe, detail)
    # A version that answered (even a too-old one) is positive evidence; only
    # when nothing answered do we report NO_ANSWER over absence.
    if too_old is not None:
        return too_old
    if no_answer is not None:
        return no_answer
    return PowerShellProbe(
        STATE_MISSING, None, None,
        "neither pwsh nor powershell resolves on PATH",
    )


__all__ = [
    "DEFAULT_RETRY_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "MINIMUM_VERSION",
    "PowerShellProbe",
    "STATE_MISSING",
    "STATE_NO_ANSWER",
    "STATE_OK",
    "STATE_TOO_OLD",
    "probe",
]