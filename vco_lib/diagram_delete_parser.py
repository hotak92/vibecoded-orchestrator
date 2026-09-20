# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Parser for Bash `Bash` tool commands that delete diagram files
(B4 ship-blocker — v0.2.34).

Used by ``templates/hooks/post-file-delete.{sh,ps1}`` (PostToolUse hook
on Bash) to detect which diagrams need to cascade-delete from SQLite +
sidecar + Weaviate via ``vco_lib.diagram_indexer drop <file>``.

Pre-v0.2.34 the parser was an inline shell-heredoc Python one-liner
that bailed if the FIRST verb of the command wasn't ``rm``/``unlink``/
``mv``. That missed every real-world Claude-generated Bash invocation
shaped like:

    cd /tmp && rm .claude/diagrams/gui/x.mmd
    sudo rm -rf .claude/diagrams/gui/
    bash -c "rm .claude/diagrams/gui/x.mmd"
    nice -n 10 rm .claude/diagrams/gui/x.mmd
    rm a.txt && rm .claude/diagrams/gui/x.mmd

— silently dropping every diagram delete in a chain whose first verb
isn't the delete itself. The cascade then leaked stale Weaviate
objects + sidecars + SQLite rows until the (also-non-existent)
``cleanup-orphan-diagrams.sh`` swept them up. Net result: zero
cleanup, ever.

This module fixes that with a full chain walker:

  1. Split the command on top-level ``;``, ``&&``, ``||``, ``|`` (the
     four common chain separators Bash recognises).
  2. For each segment, strip leading env-var assignments (``KEY=val
     KEY=val ...``), then peel off any "wrapper verbs" — commands
     whose actual delete target is the NEXT verb (``sudo``, ``nice``,
     ``taskset``, ``time``, ``bash -c "..."``, ``sh -c "..."``,
     ``env``).
  3. Once peeled to a real verb, if it's a delete verb (``rm``,
     ``unlink``, ``mv``, ``Remove-Item``, ``Move-Item``), collect its
     positional path args.
  4. Aggregate ALL collected paths across ALL segments, then filter
     to ``.mmd``/``.excalidraw`` under ``.claude/diagrams/`` — every
     other path (including malicious ones the user's ``rm`` happened
     to also target, like ``/etc/passwd``) is DROPPED, so the cascade
     cannot be coaxed into dropping unrelated indexer entries.

Security note
-------------
The path filter at step 4 is the security boundary. If the user runs
``rm -rf .claude/diagrams/gui/* /etc/passwd``, the parser sees
``/etc/passwd`` as a candidate; the filter rejects it (not a diagram
file, not under ``.claude/diagrams/``). The cascade-delete code
downstream (``vco_lib.diagram_indexer drop``) is therefore never
asked to operate on a path outside the diagrams scope — even when the
user's actual ``rm`` shell-glob expanded to a wider set.

The downstream ``drop`` is also idempotent + path-bound (it only
removes rows whose ``file_path`` matches the input verbatim, then
removes the sibling sidecar at the same parent dir). It can't be
coaxed into recursive removal.

Recursive chain expansion (``bash -c "..."``)
---------------------------------------------
For ``bash -c "rm .claude/diagrams/gui/x.mmd && rm y.mmd"`` we
recursively parse the quoted sub-command string with the same
algorithm, then aggregate the results. Depth is bounded at 4 (deeply-
nested ``bash -c "bash -c '...'"`` is suspicious-shaped enough that we
refuse rather than risk an attacker-crafted DoS via infinite
recursion).

Cross-OS
--------
The parser also recognises PowerShell-style ``Remove-Item`` and
``Move-Item`` so the same module backs both the .sh and .ps1 hooks
without divergence (PowerShell hooks invoke Python the same way the
Bash hooks do, just via a different `Get-Command python` lookup).

Shared chain walker (v0.2.95, lane F10)
---------------------------------------
Steps 1-3 above (chain split, env-prefix strip, wrapper peel, ``bash
-c`` recursion) are NO LONGER implemented here: they moved verbatim to
``vco_lib.bash_command_walk`` when ``vco_lib.bash_write_targets``
became a second consumer of exactly the same walk. This module now
contributes only the parts that are specific to DELETES: the delete-verb
vocabulary, the positional-arg collector, and the diagrams
security-boundary filter. Behaviour is unchanged -- the walker is a
byte-for-byte move of the private helpers that used to live here.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath

from vco_lib.bash_command_walk import walk_command

# ─── Verb classification ─────────────────────────────────────────────────

# Verbs that themselves do a delete-equivalent operation. The verb name
# is compared case-insensitively after `os.path.basename` (so
# ``/bin/rm`` and ``rm`` both match).
_DELETE_VERBS: frozenset[str] = frozenset({
    # Unix
    "rm",
    "unlink",
    "mv",  # mv is "delete + create"; the SOURCE counts as a delete.
    # PowerShell
    "remove-item",
    "move-item",
})

# File-extension allowlist for the post-filter step. Anything not
# ending in one of these is discarded — including paths under
# ``.claude/diagrams/`` that aren't a diagram source (e.g. PNG renders
# the indexer doesn't track, or accidental directory deletes).
_DIAGRAM_SUFFIXES: tuple[str, ...] = (".mmd", ".excalidraw")

# Path filter: every collected target must contain ``.claude/diagrams/``
# somewhere in its normalized path. Cross-OS-aware (works for both
# forward-slash and backslash; we normalise to forward before checking).
_DIAGRAMS_FRAGMENT: str = ".claude/diagrams/"


# ─── Helpers ─────────────────────────────────────────────────────────────


def _collect_paths_from_verb_segment(tokens: list[str]) -> list[str]:
    """Given a `tokens` list that starts with an already-peeled verb,
    return the list of candidate path strings.

    Chain-splitting, env-prefix stripping, wrapper peeling and `bash -c`
    recursion all happen in ``vco_lib.bash_command_walk.walk_command``
    BEFORE this collector is called — it only sees a real verb segment.
    For delete verbs it collects positional path args; for everything
    else it returns [].
    """
    if not tokens:
        return []
    verb_basename = os.path.basename(tokens[0]).lower()

    # Real delete verb.
    if verb_basename not in _DELETE_VERBS:
        return []

    # Collect positional args (skip flags). Stop at the first
    # chain-separator (defense in depth — _split_chain should have
    # already removed these, but a `--` end-of-options sentinel can
    # appear too).
    args: list[str] = []
    for tok in tokens[1:]:
        if tok == "--":
            # POSIX end-of-options. Subsequent tokens are paths even
            # if they start with `-`.
            continue
        if tok.startswith("-"):
            continue
        args.append(tok)

    # For move-equivalent verbs, only the SOURCE (first positional)
    # counts as a delete — the destination is a CREATE.
    if verb_basename in ("mv", "move-item") and len(args) >= 2:
        args = args[:1]

    return args


def _filter_to_diagrams(candidates: list[str]) -> list[str]:
    """Apply the security-boundary filter: keep only paths that
    end in a known diagram extension AND live under
    ``.claude/diagrams/``. Returns normalised paths (forward-slashes,
    redundant ``.`` / ``..`` collapsed).

    This is the canonical place to add new safety checks if the
    diagram scope grows (e.g. additional extensions). Any path the
    parser collects but this filter rejects is silently dropped — the
    downstream cascade is never asked to operate on it.
    """
    out: list[str] = []
    for p in candidates:
        if not p:
            continue
        # Normalise to a canonical form. PurePosixPath collapses
        # redundant separators + `.` segments without resolving
        # symlinks (we don't want filesystem touches in a hook).
        # On Windows paths the parser still sees forward slashes
        # because Claude's Bash invocations always use forward
        # slashes (it normalises before invoking the tool).
        try:
            norm = str(PurePosixPath(p))
        except (TypeError, ValueError):
            continue
        # Suffix check.
        if not norm.lower().endswith(_DIAGRAM_SUFFIXES):
            continue
        # Scope check — must contain the diagrams folder fragment.
        # Match both forward-slash AND backslash variants for paranoia
        # (a future Windows-Bash invocation that doesn't normalise
        # would still match the backslash form).
        if _DIAGRAMS_FRAGMENT not in norm and ".claude\\diagrams\\" not in p:
            continue
        out.append(norm)
    return out


# ─── Public API ──────────────────────────────────────────────────────────


def extract_diagram_delete_targets(command: str) -> list[str]:
    """Top-level public function. Given a Bash command string (e.g.
    the `tool_input.command` field from a PostToolUse(Bash) hook
    payload), return the list of diagram-file paths the command
    deletes.

    Returns a (possibly empty) list of normalised path strings. Empty
    list means "no diagrams to cascade-delete" — caller exits cleanly.

    Idempotent + side-effect-free: no filesystem reads, no subprocess
    spawns. Safe to call repeatedly on the same input.
    """
    if not command or not command.strip():
        return []
    candidates = walk_command(command, _collect_paths_from_verb_segment)
    return _filter_to_diagrams(candidates)


def _cli_main() -> int:
    """Stdin → stdout one-liner for shell-hook embedding.

    Reads a Bash command from stdin (so the shell can quote-pipe it via
    `printf '%s' "$COMMAND" | python -m vco_lib.diagram_delete_parser`),
    prints one diagram path per line on stdout. Always exits 0 — empty
    stdout is a valid "no cascade needed" signal.
    """
    import sys
    try:
        cmd = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return 0
    for path in extract_diagram_delete_targets(cmd):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
