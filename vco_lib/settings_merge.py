# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``.claude/settings.json`` merge ALGORITHM — one home (v0.2.95).

Two pure functions, extracted from ``vco_lib.project_init``:

  * :func:`smart_merge_settings` — the recursive, USER-WINS dict merge that
    decides what a bundle update is allowed to change in a project's
    ``settings.json``;
  * :func:`merge_hooks_block` — the per-event hook-array merge underneath it
    (scrub retired registrations, supersede stale VCO commands, append
    genuinely-new ones at per-command granularity).

WHY IT LIVES HERE. ``project_init`` is under a line-count ratchet
(``tests/test_v0292_wp4_delivery_and_promises.py``) that refuses further
growth, and the v0.2.95 retired-registration work added threading to exactly
this pair. The pair is also the best-shaped thing in that module to move: it
is pure — dicts in, a new dict out, no filesystem, no project state, no
module-level mutable anything — and it has ONE production caller,
``project_init._merge_settings_template_for_bundle``. That caller keeps the
I/O (read the template, read the target, write atomically, report the
symlink redirect) and is the thin orchestration shim CLAUDE.md sanctions for
a split like this; the decision it delegates to is here.

A RETIRED PROMISE. Both functions carried "mirror of
``install.py:_smart_merge_settings`` / ``_merge_hooks``" in their docstrings,
plus a note that they were inlined so ``vco_lib`` would not have to import
``install.py``. Neither name exists in ``install.py`` any more: v0.2.85 (D2,
ruling R17) deleted the bespoke Steps 5b + 9b and routed the root install
through the ONE bundle engine (``vco_lib.self_install`` ->
``project_init.install_project_bundle``), which reaches this code the same way
every project install does. So there is no mirror to keep in step, and the
claim is retired here rather than carried forward: the name
``smart_merge_settings`` is reclaimed by the only implementation there is.

THE CONTRACT, in one line each:

  * ``smart_merge_settings(user, template)`` — a key the user does not have is
    ADDED from the template; a key both have is merged when BOTH sides are
    dicts, and otherwise the USER's value stands. There is no path on which a
    user scalar is overwritten.
  * ``merge_hooks_block(user_hooks, template_hooks)`` — see its own docstring;
    the short version is that a command VCO cannot positively identify as its
    own shipped hook is preserved byte-for-byte.

``retired_removed`` is an optional accumulator threaded from the caller down
to the hooks merge, which appends one record per retired registration it
removed. It is threaded through the RECURSION too: the hooks block is
top-level today, and a threading hole would be invisible until the day it
isn't. ``parked`` / ``kept_out`` (v0.2.97) ride the same way: the hooks the
user disabled from the launcher, which the merge must not put back, and the
accumulator of registrations it therefore left out — see
:mod:`vco_lib.parked_hooks` for where that state comes from and the rule when
it cannot be read.
"""
from __future__ import annotations

from typing import Any, Optional

from vco_lib.hook_retirements import (
    scrub_retired_registrations,
    vco_hook_script_identity,
)
from vco_lib.hooks_settings import normalize_matcher
from vco_lib.parked_hooks import (
    KEPT_OUT_PARKED,
    KEPT_OUT_UNREADABLE,
    ParkedHooksState,
    find_parked_match,
)

__all__ = ["merge_hooks_block", "smart_merge_settings"]


def smart_merge_settings(
    user: dict, template: dict, *, retired_removed: Optional[list] = None,
    parked: Optional[ParkedHooksState] = None, kept_out: Optional[list] = None,
) -> dict:
    """Recursive dict merge with a hooks-block special-case.

    USER-WINS on every leaf: the template can only ADD a key the user's file
    lacks, or merge deeper when both sides are dicts. A user scalar is never
    replaced — that is the whole reason a bundle update may touch a file the
    user edits.

    ``retired_removed`` (v0.2.95) is threaded down to the hooks-block merge,
    which appends one record per retired registration it removed. Recursion
    carries it too: the hooks block is top-level today, and a threading hole
    would be invisible until the day it isn't.

    ``parked`` / ``kept_out`` (v0.2.97) are threaded the same way. With
    ``parked`` set, a ``hooks`` block the user's file LACKS is no longer
    copied from the template wholesale: it goes through the hooks merge too,
    because the launcher deletes the whole key when the last hook is disabled,
    and copying it back would switch every one of them on again.
    """
    out = dict(user)
    for key, tval in template.items():
        if key not in out:
            if key == "hooks" and parked is not None and isinstance(tval, dict):
                merged_hooks = merge_hooks_block(
                    {}, tval, retired_removed=retired_removed,
                    parked=parked, kept_out=kept_out,
                )
                if merged_hooks or not tval:
                    out[key] = merged_hooks
                continue
            out[key] = tval
            continue
        uval = out[key]
        if key == "hooks" and isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = merge_hooks_block(
                uval, tval, retired_removed=retired_removed,
                parked=parked, kept_out=kept_out,
            )
        elif isinstance(uval, dict) and isinstance(tval, dict):
            out[key] = smart_merge_settings(
                uval, tval, retired_removed=retired_removed,
                parked=parked, kept_out=kept_out,
            )
        # else: user wins.
    return out


def merge_hooks_block(
    user_hooks: dict, template_hooks: dict,
    *, retired_removed: Optional[list] = None,
    parked: Optional[ParkedHooksState] = None, kept_out: Optional[list] = None,
) -> dict:
    """Per-event hook array merge.

    v0.2.97 (no resurrection of launcher-disabled hooks): a template
    registration this merge would ADD — to an event the user already has, or
    as a whole event the user lacks — is first checked against ``parked``.
    Readable state: a registration a parked row covers
    (:func:`vco_lib.parked_hooks.find_parked_match`, identity-based) is left
    out. Unreadable state: NO registration is added, since any of them could
    be one the user switched off. Either way one record per left-out
    registration goes to ``kept_out``. Only additions are filtered: a
    registration already present is superseded/kept exactly as before, and a
    user's own hooks are never consulted against ``parked`` at all.
    ``parked=None`` is the pre-v0.2.97 behaviour (no filter).

    v0.2.95 (retired registrations): before anything else, the user's block is
    run through ``hook_retirements.scrub_retired_registrations``, which drops
    the registrations VCO itself shipped and has since retired. That has to
    happen HERE rather than in the supersede pass below, because the supersede
    pass can only recognise a hook the template still ships — a retired one
    is, by definition, no longer in the template. ``retired_removed`` collects
    one record per removal for the caller's audit trail.

    v0.2.70 (Stream G — supersede-not-stack): historically this was APPEND-ONLY
    by exact command-STRING identity, so when a VCO-shipped hook's command form
    changed (e.g. a path-separator fix `...\\hooks\\x.ps1` -> `.../hooks/x.ps1`,
    a flag change, or an interpreter change) bundle-update did NOT heal existing
    projects — it STACKED the new command next to the stale one, leaving the
    BROKEN command actively firing (and failing) at every event alongside the
    working one. That is worse than dead code: the stale invocation keeps
    running.

    Now: a template entry whose hook-script IDENTITY (the `.claude/hooks/<name>`
    basename, normalized across `\\`/`/`, var-expansion, quoting) matches an
    EXISTING user entry's identity but whose command STRING differs SUPERSEDES
    the stale one — the stale command is dropped and the template's current
    command installed, leaving exactly ONE invocation per VCO hook. Identity +
    string both match → left as-is (idempotent). Identity absent from the user's
    set → appended (genuinely new VCO hook; today's behavior).

    CONSERVATIVE GUARD (critical): a command is only treated as a VCO hook when
    `vco_hook_script_identity` resolves it to a `.claude/hooks/<name>` script
    VCO actually ships (i.e. the same identity appears in the TEMPLATE). A
    user's OWN custom hook (a script not under `.claude/hooks/`, or one VCO
    doesn't ship) returns `None` / has no template match and is PRESERVED
    byte-for-byte — never rewritten or dropped. When in doubt, fall back to the
    pre-v0.2.70 append behavior (a wrong replace that clobbers a user hook is
    worse than a missed supersede).
    """
    out, scrub_removals = scrub_retired_registrations(user_hooks)
    if retired_removed is not None:
        retired_removed.extend(scrub_removals)

    def _keep_out(event: str, group: Any, command: str) -> bool:
        """True (and recorded) when adding this registration must not happen."""
        if parked is None:
            return False
        matcher = normalize_matcher(group) if isinstance(group, dict) else ""
        record = {"event": event, "matcher": matcher, "command": command}
        if not parked.readable:
            record["reason"] = KEPT_OUT_UNREADABLE
        else:
            hit = find_parked_match(
                parked.hooks, event, matcher, command, template_hooks.get(event) or [],
            )
            if hit is None:
                return False
            record.update(reason=KEPT_OUT_PARKED, parked_command=hit.command)
        if kept_out is not None:
            kept_out.append(record)
        return True

    def _entry_cmds(entry: dict) -> list[str]:
        if not isinstance(entry, dict):
            return []
        cmds: list[str] = []
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            # Keep only non-empty string commands (drops None + falsy) — this
            # also narrows the element type to `str` for the return contract.
            if isinstance(cmd, str) and cmd:
                cmds.append(cmd)
        return cmds

    for event, t_entries in template_hooks.items():
        if event not in out:
            added = _without_kept_out(event, t_entries, _keep_out)
            if added or not t_entries:  # an event emptied by the filter is not added
                out[event] = added
            continue
        u_entries = out[event] if isinstance(out[event], list) else []

        # Exact command strings already present (idempotent skip).
        existing_cmds: set[str] = set()
        for entry in u_entries:
            for c in _entry_cmds(entry):
                existing_cmds.add(c)

        # Build the merged entry list. First pass: SUPERSEDE stale VCO hook
        # commands in the USER entries whose identity matches a template
        # identity but whose string differs from the current template command.
        # Map identity -> current template command (first occurrence wins;
        # the template ships at most one command per identity per event). The
        # KEYS of this map ARE the set of VCO-shipped identities eligible to
        # supersede — a user command whose identity is absent here (e.g. a
        # user's own hook, or a hook this template doesn't ship) is never
        # rewritten. This is the eligibility guard (no separate set needed).
        template_cmd_for_identity: dict[str, str] = {}
        for t_entry in t_entries:
            for c in _entry_cmds(t_entry):
                ident = vco_hook_script_identity(c)
                if ident and ident not in template_cmd_for_identity:
                    template_cmd_for_identity[ident] = c

        # v0.2.97: which matchers the template ships each identity under in
        # THIS event, and which (matcher, identity) / (matcher, command) pairs
        # the user's file already has. See `_cmd_handled` for why.
        template_matchers: dict[str, set[str]] = {}
        for t_entry in t_entries:
            for c in _entry_cmds(t_entry):
                ident = vco_hook_script_identity(c)
                if ident:
                    template_matchers.setdefault(ident, set()).add(normalize_matcher(t_entry))
        present_idents: set[tuple[str, str]] = set()
        present_cmds: set[tuple[str, str]] = set()

        merged_entries: list = []
        superseded_identities: set[str] = set()
        for entry in u_entries:
            if not isinstance(entry, dict):
                merged_entries.append(entry)
                continue
            u_matcher = normalize_matcher(entry)
            new_entry = dict(entry)
            new_hooks: list = []
            for h in entry.get("hooks", []):
                if not isinstance(h, dict) or not h.get("command"):
                    new_hooks.append(h)
                    continue
                cmd = h["command"]
                ident = vco_hook_script_identity(cmd)
                present_cmds.add((u_matcher, cmd))
                # CONSERVATIVE: only supersede when the identity is a VCO hook
                # the template ships AND the string actually differs (stale
                # form). A user's own hook (ident None, or ident not in the
                # template) is preserved verbatim.
                if (
                    ident
                    and ident in template_cmd_for_identity
                    and cmd != template_cmd_for_identity[ident]
                ):
                    new_h = dict(h)
                    new_h["command"] = template_cmd_for_identity[ident]
                    new_hooks.append(new_h)
                    superseded_identities.add(ident)
                    present_idents.add((u_matcher, ident))
                else:
                    new_hooks.append(h)
                    if ident and cmd == template_cmd_for_identity.get(ident):
                        # Already current — record so the append pass skips it.
                        superseded_identities.add(ident)
                        present_idents.add((u_matcher, ident))
            new_entry["hooks"] = new_hooks
            merged_entries.append(new_entry)

        # Second pass: APPEND genuinely-new template hooks at PER-COMMAND
        # (inner-hook) granularity. A template command is "handled" (so it
        # must NOT be re-appended) when EITHER its exact string is already
        # present verbatim OR its VCO-hook identity was just superseded/
        # confirmed-current in a user entry above.
        #
        # WHY per-command, not per-entry (pre-existing bug, A3): the template
        # ships several inner-hooks in ONE event group (e.g. the `Stop` group
        # carries notify-stop + stop-drain-citations + the two codegraph stop
        # hooks together). Appending the WHOLE group whenever ANY one
        # inner-hook is new would re-introduce the already-present
        # notify-stop/stop-drain-citations commands as a second entry → a
        # double desktop notification and a second citation drain at every
        # turn-end. Adding a new Stop hook makes this fire on every existing
        # project's next bundle update. So append only the inner-hooks that are
        # NOT already handled, preserving their per-hook config
        # (timeout/async).
        #
        # v0.2.97 (multi-matcher gap): the template ships a few scripts under
        # SEVERAL matchers in one event (`kg-summary-generator.sh` under
        # `Edit`, `Write` and a store tool). Deduplicating those event-wide
        # meant a lost `Edit` registration was never re-added, because the
        # `Write` one carried the same command string. For such a script the
        # identity is (script, matcher), the same key the parked-hook matching
        # uses. A script the template ships under ONE matcher keeps the
        # event-wide rule, so a user who regrouped it under their own matcher
        # does not get a second, duplicate invocation.
        def _cmd_handled(c: str, t_matcher: str) -> bool:
            ident = vco_hook_script_identity(c)
            if ident is not None and len(template_matchers.get(ident, ())) > 1:
                return (t_matcher, ident) in present_idents or (t_matcher, c) in present_cmds
            if c in existing_cmds:
                return True
            return ident is not None and ident in superseded_identities

        for t_entry in t_entries:
            if not isinstance(t_entry, dict):
                continue
            # Carry forward only the template inner-hooks whose command is not
            # already present (a command-less hook item, if any, is dropped on
            # the append path — it has no identity to dedup and the user's
            # existing group already covers any structural hooks).
            new_inner = [
                h
                for h in t_entry.get("hooks", [])
                if isinstance(h, dict)
                and h.get("command")
                and not _cmd_handled(h["command"], normalize_matcher(t_entry))
                and not _keep_out(event, t_entry, h["command"])
            ]
            if not new_inner:
                continue
            appended = dict(t_entry)
            appended["hooks"] = new_inner
            merged_entries.append(appended)

        out[event] = merged_entries
    return out


def _without_kept_out(event: str, t_entries: Any, keep_out: Any) -> list:
    """A whole template event the user lacks, minus the registrations
    ``keep_out`` rejects. Groups left with no hooks are dropped; groups that
    lose nothing are the template's own objects, exactly as before v0.2.97.
    """
    if not isinstance(t_entries, list):
        return list(t_entries)
    result: list = []
    for t_entry in t_entries:
        inner = t_entry.get("hooks") if isinstance(t_entry, dict) else None
        if not isinstance(inner, list):
            result.append(t_entry)
            continue
        kept = [
            h for h in inner
            if not (
                isinstance(h, dict)
                and isinstance(h.get("command"), str)
                and h["command"]
                and keep_out(event, t_entry, h["command"])
            )
        ]
        if len(kept) == len(inner):
            result.append(t_entry)
        elif kept:
            result.append({**t_entry, "hooks": kept})
    return result
