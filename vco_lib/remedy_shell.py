# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Rendering helpers for PRINTED REMEDIES — the commands VCO tells a user to
run (deferral ``command_to_apply`` blocks, ``vco doctor`` findings).

Why this module exists (R42, v0.2.92)
-------------------------------------
R42 is binding: *Windows parity is achieved by WRITING the ``.ps1``, never by
narrowing the feature.* A remedy a Windows user cannot paste narrows the
feature just as effectively as a missing script — the condition is detected on
their machine, the entry is printed on their machine, and the one line that
would resolve it is written in a shell they do not have.

Three shapes kept recurring across the emitters, each fixed independently and
then reintroduced by the next author:

* ``cmd_a && cmd_b`` — **Windows PowerShell 5.1**, the ``powershell.exe`` that
  ships on every Windows 10/11 box and the shell an "elevated terminal"
  usually opens, REJECTS ``&&`` outright (it is PowerShell 7+ / pwsh syntax).
* ``'single quotes'`` — ``cmd.exe`` does not strip them; the quotes reach the
  program as part of the argument, so a quoted path or refspec silently
  becomes a different (wrong) argument.
* POSIX-only script names — ``.claude/scripts/kg-sync`` is a bash wrapper; the
  Windows sibling is ``kg-sync.ps1`` and has to be launched through
  ``powershell.exe``.

So this module is the ONE home for those three decisions. Emitters call it
instead of re-deciding, which is also why the rules can be tested once
(``tests/test_remedy_shell_portability.py``) rather than per emitter.

Scope: the LOCAL machine. A deferral entry is written on the machine that
detected the condition and read by the person sitting at it, so branching on
``sys.platform`` renders the shell that user actually has. Where an emitter
deliberately prints BOTH shells (``vco_lib/symlink_handler.py`` labels a
``bash:`` line and a ``PowerShell:`` line), that is a different, equally valid
choice and this module does not override it.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Union

#: Characters that force quoting on Windows. cmd.exe splits on space and tab;
#: the rest are redirection / grouping metacharacters PowerShell also honours.
_WIN_NEEDS_QUOTES = set(' \t&()[]{}^=;!\'+,`~|<>"')


def is_windows() -> bool:
    """True when the remedy will be pasted into a Windows shell.

    Same predicate (and same ``sys.platform`` seam) as the OS branches
    already inline in :mod:`vco_lib.doctor`, so one
    ``mock.patch("vco_lib.doctor.sys.platform", "win32")`` — the idiom this
    repo's remediation tests already use — flips the doctor's own branches
    and everything rendered through here together.

    FOUND ALONG THE WAY, not fixed here: this predicate now exists in three
    places (``vco_lib.windows_reserved_ports.is_windows``,
    ``vco_lib.npx_resolver._is_windows``, and here) plus a scattering of
    inline ``sys.platform == "win32"`` tests. Consolidating them is a
    cross-module change touching files this lane does not own; it is
    recorded rather than done, and this module is at least the ONE home for
    the remedy layer's copy.
    """
    return sys.platform.startswith("win")


def quote(value: Union[str, Path]) -> str:
    """Quote one argument for the shell the user will paste into.

    POSIX gets :func:`shlex.quote` (single quotes). Windows gets DOUBLE
    quotes, and only when needed: ``cmd.exe`` treats a single quote as an
    ordinary character, so the POSIX form would hand the program a literal
    ``'C:\\Users\\x'`` — an argument that names a path nobody has.
    """
    text = str(value)
    if not is_windows():
        return shlex.quote(text)
    if text and not any(ch in _WIN_NEEDS_QUOTES for ch in text):
        return text
    return '"' + text.replace('"', '""') + '"'


def steps(*commands: str) -> str:
    """Render an ordered sequence of commands — ONE PER LINE, never ``&&``.

    Two lines paste-and-run in cmd, PowerShell (5.1 and 7+) and every POSIX
    shell; ``a && b`` does not. Use this wherever the chain is a LOOK-FIRST
    sequence (inspect, then act) or where step 2 is harmless after a failed
    step 1 — which is the case for every remedy VCO prints today, because the
    printed advice never chains a destructive step behind a probe.

    When a step genuinely MUST NOT run unless its predecessor succeeded, do
    not reach for ``&&``: say so in the surrounding text and let the user
    read the first command's output, the same discipline the LOOK-FIRST
    remediation blocks already use.
    """
    return "\n".join(c for c in commands if c)


def script_invocation(project_root: Union[str, Path], rel_script: str,
                      *args: str) -> str:
    """Invocation for one of a project's ``.claude/scripts/`` wrappers.

    ``rel_script`` is the POSIX-relative wrapper name as it ships, e.g.
    ``".claude/scripts/kg-sync"``. On Windows the ``.ps1`` sibling is the
    real code path (``install.py`` ships both; see
    ``vco_lib/project_init.py``'s bundle list), and it needs an explicit
    ``powershell.exe -NoProfile -ExecutionPolicy Bypass -File`` prefix
    because a ``.ps1`` is not directly executable from cmd.exe.

    The path is absolutised against ``project_root`` so the line works from
    any directory — a remedy that only runs from one cwd is a remedy with an
    unstated precondition.
    """
    root = Path(project_root)
    tail = " " + " ".join(args) if args else ""
    if is_windows():
        target = root.joinpath(*rel_script.split("/"))
        target = target.with_name(target.name + ".ps1")
        return (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
            f"{quote(target)}{tail}"
        )
    return f"{quote(root.joinpath(*rel_script.split('/')))}{tail}"


def copy_tree_command(src: Union[str, Path], dst: Union[str, Path]) -> str:
    """Create ``dst`` and copy the CONTENTS of ``src`` into it, without
    overwriting anything that is already there.

    Never a move: the zero-data-loss rule applies to the user's own files as
    much as to their KG, and the caller that needs this (a project move's
    harness-state carry-over) is explicitly a copy.

    POSIX: ``mkdir -p`` + ``cp -rn``. Windows: ``robocopy /E /XC /XN /XO``,
    which is the closest real equivalent — it creates the destination,
    recurses, and skips files that already exist rather than clobbering them.
    ``robocopy`` exits non-zero on SUCCESS (bit-flag exit codes), which is
    another reason this is a standalone line and not the right half of a
    ``&&``.
    """
    if is_windows():
        return f"robocopy {quote(src)} {quote(dst)} /E /XC /XN /XO"
    return steps(
        f"mkdir -p {quote(dst)}",
        f"cp -rn {quote(str(src).rstrip('/') + '/.')} {quote(dst)}",
    )
