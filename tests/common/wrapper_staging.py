# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Staging + source helpers for the shipped `.claude/scripts/` wrappers.

v0.2.94 extracted the dependency-gated orchestrator-venv ladder out of
``kg-sync`` / ``kg-dedup`` (where it was duplicated verbatim) and into
``templates/scripts/vct_venv_ladder.{sh,ps1}``, then gave ``kg-duplicates`` —
which had NO ladder at all — the same one. Two consequences for tests:

* **Staging**: a wrapper and its ladder ship as ONE unit (the same bundle op
  list copies both into ``.claude/scripts/``). A test that stages only the
  wrapper is staging a BROKEN install, and the wrapper correctly refuses with
  "missing vct_venv_ladder.sh" instead of exercising what the test came for.
  :func:`stage_scripts` copies the unit.

* **Source assertions**: the several existing gates that grep a wrapper for
  ``"${VCT_INSTALL_ROOT:-}/.venv"``, the import probe, ``VIRTUAL_ENV`` and
  friends are asking "does this wrapper's ladder do X?" — a question that
  follows the ladder to its one home. :func:`effective_wrapper_text` answers
  it by concatenating the wrapper with the ladder it actually sources, so the
  gates keep their meaning without pinning the code to one file.

Both helpers exist so the answer lives in ONE place: five test modules asked
the same two questions and would otherwise each grow their own copy.
"""
from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_SRC = REPO_ROOT / "templates" / "scripts"
LADDER_SH = SCRIPTS_SRC / "vct_venv_ladder.sh"
LADDER_PS1 = SCRIPTS_SRC / "vct_venv_ladder.ps1"

#: Name of the sourced ladder, as the wrappers spell it.
LADDER_SH_NAME = LADDER_SH.name
LADDER_PS1_NAME = LADDER_PS1.name


def stage_scripts(scripts_dir: Path, *names: str) -> Path:
    """Copy shipped wrapper(s) into *scripts_dir* WITH the ladder they source.

    Mirrors what the bundle installs: the wrapper plus the shared lib that is
    part of it. Executable bits are preserved (``copy2``) so a staged bash
    wrapper can be invoked directly.

    Returns *scripts_dir* for convenience.
    """
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = SCRIPTS_SRC / name
        if not src.is_file():
            raise FileNotFoundError(f"no shipped script named {name!r} in {SCRIPTS_SRC}")
        shutil.copy2(src, scripts_dir / name)
    # The ladder ships beside every wrapper; copying both flavours keeps this
    # helper flavour-agnostic (a test that stages the .ps1 does not have to
    # know which lib it needs).
    for lib in (LADDER_SH, LADDER_PS1):
        shutil.copy2(lib, scripts_dir / lib.name)
    return scripts_dir


def effective_wrapper_text(wrapper: Path, *, encoding: str = "utf-8") -> str:
    """The wrapper's text PLUS the ladder text when it sources one.

    The concatenation is what a reader of the wrapper effectively executes, so
    a gate asserting "this wrapper probes $VCT_INSTALL_ROOT first" stays true
    across the v0.2.94 extraction — it follows the ladder home instead of
    demanding the ladder stay inlined.
    """
    own = wrapper.read_text(encoding=encoding)
    text = own
    # Both conditions are evaluated against the WRAPPER's own text, never the
    # growing concatenation: the .sh ladder names its .ps1 sibling in a parity
    # comment, so testing the accumulator would append both flavours to every
    # bash wrapper.
    if LADDER_SH_NAME in own:
        text += "\n" + LADDER_SH.read_text(encoding="utf-8")
    if LADDER_PS1_NAME in own:
        text += "\n" + LADDER_PS1.read_text(encoding="utf-8-sig")
    return text
