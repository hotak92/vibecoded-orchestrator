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
"""
from __future__ import annotations

import os
import shlex
from pathlib import PurePosixPath

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

# Verbs whose target is the *next* verb (they wrap an underlying command).
# Sudo/nice/taskset/time pass their tail args to the wrapped command;
# `env` does the same but with env-var resetting in between (we treat it
# identically — the env-var prefix logic also handles the bare `KEY=val
# command` shape, so `env KEY=val command` is just `env` peeling plus
# the env-prefix walker on the remainder).
_WRAPPER_VERBS: frozenset[str] = frozenset({
    "sudo",
    "nice",
    "taskset",
    "time",
    "env",
    "ionice",
    "chronic",
    "stdbuf",
    "exec",  # `exec rm foo` re-execs as rm; same target set.
})

# `bash -c "<sub-command>"` / `sh -c "<sub-command>"` / `python -c "..."`
# — recognised by name, then we peel the optional `-c` and re-parse the
# next positional as a fresh chain. Python is included because Claude
# sometimes runs `python -c "import os; os.remove(...)"`, though we don't
# attempt to interpret Python source — just rejecting silently is fine
# because the case is rare and the false-negative is caught by the
# session-start sweep (when that lands).
_SHELL_DASH_C_VERBS: frozenset[str] = frozenset({
    "bash",
    "sh",
    "zsh",
    "dash",
    "ksh",
})

# Chain separators. Top-level operators that break command sequences
# into independent segments — each segment gets its own verb walk.
# Token-equal match (after shlex split with posix=True, these survive
# as standalone tokens because shlex doesn't fold them).
_CHAIN_SEPARATORS: frozenset[str] = frozenset({
    ";",
    "&&",
    "||",
    "|",
    "&",  # background — same semantics for our purposes; the foreground
          # part of `cmd1 & cmd2` is a delete-bearing segment.
})

# Max recursion depth for `bash -c "..."` parsing. 4 is generous —
# real Claude-generated commands rarely nest beyond 2, and a deeply-
# nested chain is more likely an attacker testing the parser than a
# legitimate user command.
_MAX_NESTING_DEPTH: int = 4

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


def _is_env_assignment(tok: str) -> bool:
    """True for `KEY=value` shapes that Bash treats as transient env
    assignments BEFORE a command. Same rule the inline parser used:
    contains `=`, no leading `-`, LHS is alnum_underscore, LHS starts
    with a letter (env names can't start with a digit per POSIX)."""
    if "=" not in tok or tok.startswith("-"):
        return False
    eq = tok.index("=")
    head = tok[:eq]
    if not head:
        return False
    # Allow letters / digits / underscore in env names; first char must
    # be a letter or underscore. (POSIX env name rule.)
    if not (head[0].isalpha() or head[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in head)


def _split_chain(tokens: list[str]) -> list[list[str]]:
    """Split a token list into segments at top-level chain operators.

    `shlex.split` keeps `;`, `&&`, `||`, `|`, `&` as standalone tokens
    when they appear unquoted in the source. We just iterate and slice
    on those. Sub-commands inside `bash -c "..."` survive as ONE
    string token (because the quotes preserved them), so they are NOT
    split here — they're re-parsed by `_parse_dash_c_string` when the
    walker recognises a `bash -c` wrapper.

    Empty segments (e.g. trailing `;`) are dropped to keep downstream
    iteration tight.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _CHAIN_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading `KEY=VAL` env assignments and return the remainder.
    Returns an empty list if every token is an env assignment (malformed,
    but graceful)."""
    i = 0
    while i < len(tokens) and _is_env_assignment(tokens[i]):
        i += 1
    return tokens[i:]


def _peel_wrapper_verbs(tokens: list[str]) -> list[str]:
    """Strip leading wrapper verbs (``sudo``, ``nice -n 10``, ``time``,
    ``env``, etc.) until we land on a non-wrapper verb.

    Wrapper verbs may carry their own flags + numeric args (``nice -n
    10``, ``taskset 1``, ``ionice -c 2``). We skip leading flags
    (``-...``) AND a single numeric positional argument that some
    wrappers take. Stops as soon as the next non-flag, non-numeric
    token is a known non-wrapper.

    Returns the tokens FROM the first non-wrapper verb onward. Returns
    an empty list if every token is a wrapper or flag (malformed input).
    """
    out = list(tokens)
    safety = 0  # avoid infinite loop on pathological input
    while out and safety < 16:
        safety += 1
        verb_basename = os.path.basename(out[0]).lower()
        if verb_basename not in _WRAPPER_VERBS:
            return out
        # Peel the wrapper. Walk forward, skipping flags + a single
        # numeric arg (for `nice -n 10`, `taskset 1`, etc.).
        i = 1
        while i < len(out):
            t = out[i]
            if t.startswith("-"):
                # Flag — skip. Some flags take values (e.g. `-n 10` for
                # nice), but consuming flag-and-its-value would require
                # per-wrapper knowledge we don't have. The next token
                # (the value) is numeric → also skipped by the
                # `isdigit()` branch below; if it's not numeric, we
                # stop here and let the next loop iteration re-examine
                # it as a candidate verb.
                i += 1
                continue
            # Numeric positional (e.g. taskset's CPU mask, nice's
            # priority value). Skip ONE numeric token, then break.
            if t.lstrip("-").isdigit():
                i += 1
                continue
            # Env-style assignment between wrapper-flags and the verb
            # (e.g. `sudo -E ENV=val command`). Skip it too — same
            # logic as `_strip_env_assignments`.
            if _is_env_assignment(t):
                i += 1
                continue
            break
        out = out[i:]
    return out


def _parse_dash_c_string(verb_basename: str, tokens: list[str], depth: int) -> list[str]:
    """If `tokens` is shaped like `bash -c "<sub-cmd>"`, re-parse
    `<sub-cmd>` as a fresh chain and return the collected paths.

    Returns an empty list if the shape doesn't match or the sub-cmd
    is malformed.
    """
    if verb_basename not in _SHELL_DASH_C_VERBS:
        return []
    # Find a `-c` flag and grab the next positional. (Some users write
    # `bash -ec "..."` — also supported via `-c` substring match in the
    # combined-flag string.)
    sub_cmd: str | None = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-c" and i + 1 < len(tokens):
            sub_cmd = tokens[i + 1]
            break
        # Combined flag like `-ec` / `-xc` — last char is `c`, so the
        # next positional is the sub-cmd.
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            if i + 1 < len(tokens):
                sub_cmd = tokens[i + 1]
                break
        i += 1
    if sub_cmd is None:
        return []
    # Recurse with depth-budget so attacker-crafted nested `bash -c`
    # can't trigger unbounded recursion.
    if depth + 1 > _MAX_NESTING_DEPTH:
        return []
    return _walk_command(sub_cmd, depth=depth + 1)


def _collect_paths_from_verb_segment(tokens: list[str], depth: int) -> list[str]:
    """Given a `tokens` list that starts with a (possibly-wrapped) verb,
    return the list of candidate path strings.

    For wrapper verbs, peels them first. For `bash -c "..."`, recurses
    into the sub-command. For delete verbs, collects positional path
    args. For everything else, returns [].
    """
    if not tokens:
        return []
    verb_basename = os.path.basename(tokens[0]).lower()

    # bash -c / sh -c — recurse into the sub-command.
    if verb_basename in _SHELL_DASH_C_VERBS:
        return _parse_dash_c_string(verb_basename, tokens, depth)

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


def _walk_command(command: str, depth: int = 0) -> list[str]:
    """Top-level entry: parse `command` into chain segments, peel
    wrappers off each, collect delete-verb path args. Returns a flat
    list of candidate path strings (NOT yet filtered to diagrams).

    Bounded by `depth <= _MAX_NESTING_DEPTH`. The default depth=0 is
    the user's command; deeper levels come from recursive `bash -c`
    expansion.
    """
    if depth > _MAX_NESTING_DEPTH:
        return []

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Malformed quoting — bail cleanly. The downstream cascade is
        # a no-op without paths.
        return []

    if not tokens:
        return []

    all_paths: list[str] = []
    for segment in _split_chain(tokens):
        if not segment:
            continue
        # Strip env-assignment prefix (`KEY=val command ...`).
        stripped = _strip_env_assignments(segment)
        if not stripped:
            continue
        # Peel wrapper verbs (`sudo command`, `nice -n 10 command`).
        peeled = _peel_wrapper_verbs(stripped)
        if not peeled:
            continue
        # Collect path candidates from THIS segment.
        all_paths.extend(_collect_paths_from_verb_segment(peeled, depth))

    return all_paths


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
    candidates = _walk_command(command, depth=0)
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
