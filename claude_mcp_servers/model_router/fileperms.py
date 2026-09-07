# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Owner-only file permissions, cross-OS, in STRICT mode.

The gateway writes three files a second local user must not be able to read:
the host token (it authorises proxying under the user's Claude login and
subscription key), the pid file and the log file. On POSIX that is
``chmod 0600``; Windows has no mode bits, so it is an ACL that breaks
inheritance and grants only the current user.

Why this module exists next to two other answers to the same question
---------------------------------------------------------------------
Three implementations of "restrict this file to its owner" exist in the tree
today and none of them can serve this call site:

* ``vco_lib.secrets_audit.harden_env_perms`` — POSIX only, and returns
  ``(False, "Windows — default ACL")`` on Windows. Soft-fail by contract.
* ``vct_launcher_core::services::boot_token::restrict_file_to_owner_windows``
  (Rust) — has the ``icacls`` invocation this module mirrors, but its
  documented contract is "NEVER fails the caller", and it is not callable
  from Python.
* ``vco_lib.secrets_bootstrap`` — a bare ``os.chmod(target, 0o600)`` with no
  Windows arm at all.

The gateway's ruling is the opposite of the first two: a permission call that
cannot be applied must FAIL LOUDLY rather than silently leave a token file
readable. A silent no-op here is exactly the defect the ruling names. So this
module implements the strict variant. ``vco_lib/`` is outside this change's
file set, so the consolidation of all three into one home is recorded as a
recipe in this package's delivery report rather than attempted here.

This is NOT a mirror of the Rust helper and does not claim to be: it uses the
same two ``icacls`` flags (``/inheritance:r`` and ``/grant:r``) because those
are what the operation requires, and then deliberately differs on both axes
that matter — it grants ``(R,W)`` where the Rust one grants ``F``, and it
raises where the Rust one warns. Calling it a mirror would assert a parity
that no test enforces and that the code does not have.

Probing, not just applying
--------------------------
:func:`owner_only_state` answers "is this file owner-only?" as a TRI-STATE —
``owner_only`` / ``broader`` / ``unknown`` — because the honest answer when a
probe fails is neither yes nor no. ``/health`` and the GUI status card show
``unknown`` rather than a reassuring default.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Literal

#: Tri-state result of :func:`owner_only_state`.
OwnerOnlyState = Literal["owner_only", "broader", "unknown"]

_IS_WINDOWS = sys.platform == "win32"

#: Seconds to wait for ``icacls``. It is an in-box, local-only operation;
#: anything slower than this is a broken system, and hanging the gateway's
#: startup on it would be worse than failing with a named error.
_ICACLS_TIMEOUT_S = 15


class PermissionHardeningError(RuntimeError):
    """Owner-only permissions could not be applied. Never swallowed."""


def _windows_grant_identity() -> str:
    """``DOMAIN\\USER`` when both are known, else ``USER``.

    Raises :class:`PermissionHardeningError` when no identity is resolvable —
    guessing one would either fail or, worse, grant the wrong principal.
    """
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        raise PermissionHardeningError(
            "cannot restrict file to owner: %USERNAME% is unset, so there is "
            "no identity to grant to. Run the gateway as a normal interactive "
            "user, or set USERNAME in the service environment.",
        )
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable/writable by its owner only. Loud on failure.

    Args:
        path: an existing file or directory.

    Raises:
        PermissionHardeningError: the restriction could not be applied. The
            message names the path and the underlying cause.
    """
    if not path.exists():
        raise PermissionHardeningError(
            f"cannot restrict {path}: it does not exist",
        )
    if _IS_WINDOWS:
        _restrict_windows(path)
        return
    target_mode = 0o700 if path.is_dir() else 0o600
    try:
        os.chmod(path, target_mode)
    except OSError as exc:
        raise PermissionHardeningError(
            f"cannot restrict {path} to {oct(target_mode)}: {exc}",
        ) from exc


def _restrict_windows(path: Path) -> None:
    """``icacls <path> /inheritance:r /grant:r "<identity>:(R,W)"``.

    ``/inheritance:r`` removes inherited ACEs; ``/grant:r`` REPLACES the grant
    list so only the named identity survives. Arguments are passed as a list
    (no shell), so a path with spaces or metacharacters cannot inject.
    """
    grant = f"{_windows_grant_identity()}:(R,W)"
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise PermissionHardeningError(
            f"cannot restrict {path}: 'icacls' not found on PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PermissionHardeningError(
            f"cannot restrict {path}: 'icacls' timed out after "
            f"{_ICACLS_TIMEOUT_S}s",
        ) from exc
    except OSError as exc:
        raise PermissionHardeningError(
            f"cannot restrict {path}: 'icacls' could not be run ({exc})",
        ) from exc
    if completed.returncode != 0:
        # Exit code only. icacls echoes the full path and the identity on
        # stderr; relaying that into a log file adds nothing diagnostic and
        # widens what an unprivileged reader of the log learns.
        raise PermissionHardeningError(
            f"cannot restrict {path}: 'icacls' exited {completed.returncode}",
        )


def owner_only_state(path: Path) -> OwnerOnlyState:
    """Is ``path`` restricted to its owner? Tri-state; never guesses.

    POSIX: ``broader`` when any group/other bit is set.
    Windows: parses ``icacls`` output; ``broader`` when any ACE names a
    principal other than the current identity and the well-known
    always-present ``NT AUTHORITY\\SYSTEM`` / ``BUILTIN\\Administrators``
    entries, which every user-profile file carries and which cannot be
    removed without breaking backup/admin tooling.
    """
    if not path.exists():
        return "unknown"
    if not _IS_WINDOWS:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return "unknown"
        return "broader" if mode & 0o077 else "owner_only"
    try:
        identity = _windows_grant_identity().lower()
    except PermissionHardeningError:
        return "unknown"
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return _classify_icacls_output(completed.stdout, identity, str(path))


#: ACE principals every user-profile file carries and which are not evidence
#: that another *user* can read the file.
_WINDOWS_BENIGN_PRINCIPALS = (
    "nt authority\\system",
    "builtin\\administrators",
    "owner rights",
)


def _classify_icacls_output(
    output: str,
    identity: str,
    path_str: str,
) -> OwnerOnlyState:
    """Pure classifier for ``icacls <path>`` output. Unit-testable on any OS."""
    saw_any = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("successfully processed"):
            continue
        # The first line is "<path> <PRINCIPAL>:(perms)"; later lines are
        # just "<PRINCIPAL>:(perms)". Strip a leading path if present.
        if line.startswith(path_str):
            line = line[len(path_str):].strip()
        if ":" not in line:
            continue
        principal = line.rsplit(":(", 1)[0].strip().lower() if ":(" in line else ""
        if not principal:
            continue
        saw_any = True
        if principal == identity:
            continue
        if principal in _WINDOWS_BENIGN_PRINCIPALS:
            continue
        return "broader"
    return "owner_only" if saw_any else "unknown"


__all__ = [
    "OwnerOnlyState",
    "PermissionHardeningError",
    "owner_only_state",
    "restrict_to_owner",
]
