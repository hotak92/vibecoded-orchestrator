# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE declaration of RETIRED hook REGISTRATIONS (v0.2.95).

Retiring a hook has two halves, and until this module existed VCO only did
the first one:

  1. the SCRIPT stops shipping — the bundle's manifest reconcile deletes
     ``.claude/hooks/<name>`` from the project, because the file is gone from
     ``templates/``;
  2. the REGISTRATION stops firing — the entry in the project's
     ``.claude/settings.json`` ``hooks`` block has to go too.

Nothing did (2). :func:`vco_lib.settings_merge.merge_hooks_block`
supersedes a shipped hook only while the TEMPLATE still ships a command with
the same identity; the moment a hook stops being shipped, its stale
registration stops being recognised as VCO's and is preserved byte-for-byte
as "the user's own hook" — forever, on every update. Field evidence
(field report 2026-09-14): two ``sync_knowledge_graph.py`` registrations VCO wrote
in 2026-04/05 were still firing on every ``Edit``/``Write`` a year later,
failing with ``ModuleNotFoundError: No module named 'weaviate_mcp'`` behind a
``|| true`` that hid the failure completely.

So the retirement has to be DECLARED, not inferred: this table is what a
bundle update matches the project's on-disk registrations against.

DESIGN RULES
------------

**State-keyed (ruling R26).** Every entry matches on content that is PRESENT
in the project's settings.json right now. Nothing here asks "what changed
since the previous release" — a project three releases behind, a project
updated yesterday and a project restored from an old backup all get the same
answer from the same table.

**Conservative matching.** Two matcher kinds, both anchored, neither of which
can fire on a command that merely CONTAINS a retired path as a substring:

  * ``KIND_HOOK_SCRIPT`` — the command INVOKES ``.claude/hooks/<basename>``.
    The identity is computed by :func:`vco_hook_script_identity` (in this
    module since the v0.2.95 extraction; built on
    :func:`vco_lib.hooks_settings.invoked_script_tokens`, the ONE anchor-walk,
    shared with the launcher's settings.json hook editor), so a command that
    names the path as an ARGUMENT or a pipe operand
    (``bash my-wrapper.sh --target .claude/hooks/x.sh``) yields no identity
    and is never matched. This is the right matcher for a hook VCO shipped
    and then deleted: whatever wrapper invokes it, the script is gone.
  * ``KIND_COMMAND`` — the WHOLE command, normalised
    (:func:`normalize_command`), equals a command string VCO itself shipped.
    Equality on the whole command is what makes this safe for inline
    one-liners, where there is no script-path anchor to walk: a user command
    that embeds a retired snippet inside something larger is a different
    string and is left alone.

**Only shapes VCO provably shipped.** Each ``KIND_COMMAND`` entry below was
read out of this repository's own git history (or, for the guard-less
variant, off a live install that predates the public repo). Guessing at
plausible variants would trade a preserved-but-dead hook for a deleted
user hook, which is the worse error — so an unrecognised variant is left
alone and reported by nothing, exactly as today.

Consumers: :func:`vco_lib.settings_merge.merge_hooks_block` — the hooks half
of the settings merge the ONE bundle engine runs (ruling R17), reached from
``project_init.install_project_bundle`` — calls
:func:`scrub_retired_registrations` before its supersede pass. It runs wherever an EXISTING ``settings.json`` is
merged — every ``--update``, and a first install onto a project that already
has the file. A first install that CREATES the file has nothing to scrub, by
construction. The engine surfaces the removals in its bundle envelope and
writes one ``record_auto_resolution`` audit row per removal via
:func:`emit_removal_audit_rows`. The scrub, the identity walk it matches
with and the audit emitters were extracted here from ``project_init`` in
v0.2.95 — that module is under a line-count ratchet that (correctly)
refuses further growth.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from vco_lib.hooks_settings import invoked_script_tokens

#: Matcher kinds. See the module docstring for what each one is safe for.
KIND_HOOK_SCRIPT = "hook_script"
KIND_COMMAND = "command"

#: VCO's own "the user switched hooks off" guard, which prefixed most
#: shipped hook commands until v0.2.97. KEPT, not deleted: matching here is
#: state-keyed — retirements and command-identity comparisons run against
#: settings.json files written by EARLIER releases, and a pre-v0.2.97 install
#: still carries the prefixed form for as long as it never runs a bundle
#: update (and launcher-parked disabled entries restore it verbatim). Strip
#: it here so both eras of the same registration compare equal.
_DISABLE_GUARD_RE = re.compile(
    r"""^\[\s*-n\s+["']?\$\{?VCT_DISABLE_HOOKS(?::-)?\}?["']?\s*\]\s*\|\|\s*"""
)


def normalize_command(command: str) -> str:
    """Normalise a hook command for EQUALITY comparison.

    Three normalisations, each of which preserves meaning:

      * surrounding whitespace stripped;
      * internal whitespace runs collapsed to one space (a re-indented or
        re-wrapped settings.json must not defeat the match);
      * a leading ``[ -n "$VCT_DISABLE_HOOKS" ] || `` guard dropped — the
        guard stopped shipping in v0.2.97, but installs written before that
        release carry it (see ``_DISABLE_GUARD_RE``), so both eras of one
        registration must compare equal.

    Does NOT touch path separators or quoting: those are meaningful inside a
    ``python -c`` payload, and the ``KIND_HOOK_SCRIPT`` matcher (not this one)
    is where separator normalisation belongs.

    Returns ``""`` for a non-string / empty command, which matches nothing.
    """
    if not command or not isinstance(command, str):
        return ""
    collapsed = " ".join(command.split())
    return _DISABLE_GUARD_RE.sub("", collapsed).strip()


@dataclass(frozen=True)
class RetiredRegistration:
    """One retired hook registration, as DATA.

    Attributes:
        event: the settings.json hook event the registration sits under
            (``PostToolUse``, ``Stop``, …). Matching is scoped to it so a
            retired ``Stop`` command cannot be removed from ``PreToolUse``.
        kind: :data:`KIND_HOOK_SCRIPT` or :data:`KIND_COMMAND`.
        target: for ``KIND_HOOK_SCRIPT``, the hook script BASENAME
            (``cost-tracker.sh``) — the same value
            ``_vco_hook_script_identity`` returns. For ``KIND_COMMAND``, the
            :func:`normalize_command` form of the command VCO shipped.
        reason: why it is dead, in a form a human reading
            ``auto-resolutions.jsonl`` can act on.
        retired_in: the release that retired it.
        replacement: what does the job now — named in the audit row so the
            removal never reads as "VCO deleted a thing and told you nothing".
            ``""`` when the capability itself was removed.
    """

    event: str
    kind: str
    target: str
    reason: str
    retired_in: str
    replacement: str

    @property
    def audit_replacement(self) -> str:
        """The replacement clause for the audit row (never empty prose)."""
        return self.replacement or "nothing (the capability was removed)"


#: The table. One row per retired registration shape.
RETIRED_REGISTRATIONS: Tuple[RetiredRegistration, ...] = (
    # ── the two inline sync_knowledge_graph.py registrations ───────────────
    #
    # Both bypass `.claude/scripts/kg-sync`, which is the wrapper that
    # resolves an interpreter able to import `weaviate`, `weaviate_mcp` AND
    # `vco_lib`. Invoked directly, the script dies at module import under any
    # interpreter that is merely first on PATH — and the trailing `|| true`
    # made that indistinguishable from a successful sync on every edit.
    # Superseded by `.claude/hooks/post-file-edit.{sh,ps1}`, which routes the
    # knowledge/ branch through the wrapper (v0.2.73 HK-3).
    RetiredRegistration(
        event="PostToolUse",
        kind=KIND_COMMAND,
        target=normalize_command(
            'python .claude/scripts/sync_knowledge_graph.py '
            '"$CLAUDE_TOOL_ARG_FILE_PATH" 2>&1 || true'
        ),
        reason=(
            "calls sync_knowledge_graph.py directly, bypassing the kg-sync "
            "wrapper's interpreter gate; fails with ModuleNotFoundError on "
            "every edit and the trailing `|| true` hides it"
        ),
        retired_in="v0.2.73",
        replacement=".claude/hooks/post-file-edit.sh (routes through .claude/scripts/kg-sync)",
    ),
    RetiredRegistration(
        event="PostToolUse",
        kind=KIND_COMMAND,
        target=normalize_command(
            'python3 -c "import json,sys,subprocess,os; '
            "d=json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}; "
            "fp=d.get('tool_input',{}).get('file_path',''); "
            "subprocess.run(['python','.claude/scripts/sync_knowledge_graph.py',fp]) "
            'if fp else None" 2>&1 || true'
        ),
        reason=(
            "inline python that shells into sync_knowledge_graph.py with a "
            "bare `python`, bypassing the kg-sync wrapper's interpreter gate; "
            "fails mutely on every edit"
        ),
        retired_in="v0.2.73",
        replacement=".claude/hooks/post-file-edit.sh (routes through .claude/scripts/kg-sync)",
    ),
    RetiredRegistration(
        event="PostToolUse",
        kind=KIND_COMMAND,
        target=normalize_command(
            'powershell -NoProfile -Command "try { python '
            ".claude/scripts/sync_knowledge_graph.py "
            '$env:CLAUDE_TOOL_ARG_FILE_PATH } catch { }"'
        ),
        reason=(
            "the Windows form of the same bypass — and its `catch { }` "
            "swallows the failure even more completely than `|| true`"
        ),
        retired_in="v0.2.73",
        replacement=".claude/hooks/post-file-edit.ps1 (routes through .claude/scripts/kg-sync.ps1)",
    ),
    # ── cost telemetry, removed in v0.2.95 ─────────────────────────────────
    #
    # The scripts are gone from `templates/hooks/`, so the manifest reconcile
    # deletes the FILE from every project on the next bundle update. Without
    # these two rows the registration would survive that deletion and every
    # session would end by invoking a script that is not there.
    RetiredRegistration(
        event="Stop",
        kind=KIND_HOOK_SCRIPT,
        target="cost-tracker.sh",
        reason=(
            "cost telemetry was removed in v0.2.95; the script no longer "
            "ships, so the registration invokes a file the bundle update "
            "deletes"
        ),
        retired_in="v0.2.95",
        replacement="",
    ),
    RetiredRegistration(
        event="Stop",
        kind=KIND_HOOK_SCRIPT,
        target="cost-tracker.ps1",
        reason=(
            "cost telemetry was removed in v0.2.95; the script no longer "
            "ships, so the registration invokes a file the bundle update "
            "deletes"
        ),
        retired_in="v0.2.95",
        replacement="",
    ),
)


def match_retired_registration(
    event: str,
    command: str,
    *,
    hook_identity: Optional[str],
) -> Optional[RetiredRegistration]:
    """Return the retirement this ``(event, command)`` matches, or ``None``.

    Args:
        event: the settings.json hook event the command sits under.
        command: the hook's ``command`` string, verbatim from settings.json.
        hook_identity: the result of
            ``project_init._vco_hook_script_identity(command)`` — the
            ``.claude/hooks/<name>`` basename when the command INVOKES such a
            script, else ``None``. Required (keyword-only, no default) so a
            caller cannot silently skip the ``KIND_HOOK_SCRIPT`` half by
            forgetting an argument; ``None`` is the correct value for every
            command that invokes no shipped hook.

    The walk is deliberately not re-implemented beside this table:
    :func:`vco_hook_script_identity` — the one home for "which token is the
    script actually being run", shared with the launcher's hook editor via
    ``hooks_settings`` — is what keeps this table from developing its own,
    subtly different, opinion about what a user's hook looks like.

    Never raises: a malformed entry (non-string command, empty event) simply
    matches nothing.
    """
    if not event or not isinstance(event, str):
        return None
    if not command or not isinstance(command, str):
        return None
    normalized = normalize_command(command)
    for entry in RETIRED_REGISTRATIONS:
        if entry.event != event:
            continue
        if entry.kind == KIND_HOOK_SCRIPT:
            if hook_identity and hook_identity == entry.target:
                return entry
        elif entry.kind == KIND_COMMAND:
            if normalized and normalized == entry.target:
                return entry
    return None


# ── the identity walk + the scrub (extracted from project_init, v0.2.95) ───
#
# Both moved here so the retirement TABLE, the MATCHERS it is matched with,
# and the SCRUB that applies them have ONE home — and so ``project_init``
# (under a line-count ratchet) keeps only a thin call site. The walk is
# unchanged from its v0.2.70 form; only its import of the anchor-walk
# primitive moved with it.

# A bare `.claude/hooks/<name>.{sh,ps1}` token (with optional `${VAR}/` /
# `%VAR%/` / path prefix ahead of `.claude/`, no embedded whitespace), with the
# capture group on the basename. Anchored to the FULL token (the token has
# already been split on whitespace + de-quoted by the caller).
_HOOK_TOKEN_RE = re.compile(
    r"^(?:[^\s]*/)?\.claude/hooks/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:sh|ps1))$"
)


def vco_hook_script_identity(command: str) -> Optional[str]:
    """Extract the canonical IDENTITY of a VCO-shipped hook command: the hook
    SCRIPT basename under `.claude/hooks/` (e.g. `ensure-containers.ps1`,
    `pre-tool-use.sh`), normalized across path-separator (`\\` vs `/`),
    `${...}` / `%...%` variable expansion, and quoting. Returns `None` when the
    command does NOT *invoke* a script under `.claude/hooks/` — i.e. it is a
    user's OWN custom hook, which must never be rewritten/dropped.

    v0.2.70 (Stream G): two commands with the SAME identity are the SAME VCO
    hook (one may be a stale form of the other, e.g. a backslash path
    pre-v0.2.70 vs the forward-slash form shipped now). This is the conservative
    matcher behind the supersede-not-stack merge.

    BLOCKER G-1 fix: the identity is resolved ONLY when `.claude/hooks/<name>`
    is the *invoked script*, NOT when it appears anywhere in the command string.
    A user command that merely *references* a VCO hook path as an ARGUMENT or a
    pipe/cat operand (e.g. `bash my-wrapper.sh --target .claude/hooks/x.sh` or
    `cat .claude/hooks/x.sh | grep foo`) is a CUSTOM user hook and returns
    `None` (never superseded/destroyed). A token is the invoked script iff it is
    (a) the first command-start token, (b) immediately follows a shell
    interpreter token (`bash`/`pwsh`/...), or (c) immediately follows a
    PowerShell `-File`/`-Command` flag. Tokens appearing later as arguments or
    after a pipe (without one of those anchors) are NOT invocations.
    """
    # The anchor-walk itself (which token is the script actually being RUN,
    # normalized across `\`/`/`, quoting and var-expansion) lives in
    # `vco_lib.hooks_settings.invoked_script_tokens` — imported at the top of
    # this module. Identity = the FIRST invocation-anchored token that is a
    # `.claude/hooks/<name>.{sh,ps1}` path; a command whose only such path sits
    # at an ARGUMENT position yields no anchored match and returns None, which
    # is the conservative "this is the user's own hook, never touch it" answer.
    for tok in invoked_script_tokens(command):
        m = _HOOK_TOKEN_RE.match(tok)
        if m:
            return m.group(1)
    return None


def scrub_retired_registrations(user_hooks: dict) -> Tuple[dict, list]:
    """Drop every registration this module's table declares dead.

    v0.2.95. Retiring a hook has two halves — the SCRIPT stops shipping (the
    manifest reconcile deletes it) and the REGISTRATION stops firing. Only the
    first was ever done, because ``settings_merge.merge_hooks_block``
    recognises a VCO hook by its presence in the CURRENT template: the moment
    a hook stops being shipped, its stale registration stops being recognised
    as VCO's and is preserved byte-for-byte as "the user's own", forever. Two
    ``sync_knowledge_graph.py`` registrations VCO wrote in 2026-04/05 were
    still firing — and failing behind a `|| true` — on a live install a year
    later.

    So the retirement is DECLARED (the table in this module) and matched
    against what is on disk RIGHT NOW (ruling R26: state-keyed, never "what
    changed since the previous release").

    Walks EVERY user event, not only the events the template ships: a
    registration under an event VCO no longer ships would otherwise be
    unreachable — and unreachable is exactly the state this fixes.

    Removal shape: the matching inner hook is dropped; a group left with no
    inner hooks is pruned; an event left with no groups loses its key (the
    caller's merge re-creates it from the template when the template still
    ships that event, which is the correct current wiring).

    Conservative by construction: matching is delegated to
    :func:`match_retired_registration` with the identity from
    :func:`vco_hook_script_identity`, so a user command that merely mentions
    a retired path as an argument — or embeds a retired snippet inside a
    larger command — resolves to no match and is preserved verbatim.

    Returns ``(new_mapping, removals)``: a NEW dict (the input is never
    mutated) plus one ``{"event", "command", "retirement"}`` record per
    removal, for the caller's audit trail. Idempotent: a second run finds
    nothing to remove and returns an equal mapping, so the settings merge
    reports ``unchanged``.
    """
    out: dict = {}
    removals: list = []
    for event, entries in user_hooks.items():
        if not isinstance(entries, list):
            # Not the array shape this scrub understands — leave it exactly as
            # found (the same posture the merge takes for a malformed block).
            out[event] = entries
            continue
        kept_entries: list = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            kept_hooks: list = []
            dropped_any = False
            for h in entry["hooks"]:
                cmd = h.get("command") if isinstance(h, dict) else None
                match = None
                if isinstance(cmd, str) and cmd:
                    match = match_retired_registration(
                        event, cmd,
                        hook_identity=vco_hook_script_identity(cmd),
                    )
                if match is None:
                    kept_hooks.append(h)
                    continue
                dropped_any = True
                removals.append(
                    {"event": event, "command": cmd, "retirement": match}
                )
            if not dropped_any:
                kept_entries.append(entry)
                continue
            if not kept_hooks:
                continue        # group is now empty → prune it
            new_entry = dict(entry)
            new_entry["hooks"] = kept_hooks
            kept_entries.append(new_entry)
        if kept_entries or not entries:
            out[event] = kept_entries
        # else: every group under this event was pruned → drop the event key.
    return out, removals


def removal_envelope_rows(removals: list) -> list:
    """The per-removal summary the bundle engine surfaces in its envelope.

    Built on BOTH paths (a dry-run reports what it WOULD remove); the
    caller writes the audit rows themselves only after a real write.
    """
    return [
        {
            "event": r["event"],
            "command": r["command"],
            "retired_in": r["retirement"].retired_in,
            "replacement": r["retirement"].audit_replacement,
        }
        for r in removals
    ]


def emit_removal_audit_rows(
    folder: Path, removals: list, *, log_auto: Any, log: Any,
) -> None:
    """One ``record_auto_resolution`` row per removal, then the count log.

    ``folder``, ``log_auto`` and ``log`` are the bundle engine's, passed in:
    the audit row needs the project FOLDER, which the pure merge helpers
    deliberately do not take, and both log channels are the engine's own
    closures. Bookkeeping only — a failure to record is logged as a warning
    and never fails the update.
    """
    from vco_lib import deferral_emit as _de
    # Lazy, to keep the module-load graph acyclic — the
    # ``vco_lib.bundle_skip_deferral`` precedent.
    try:
        for removal in removals:
            retirement = removal["retirement"]
            _de.record_auto_resolution(
                folder,
                # An auto-resolution-only condition id (no deferral row):
                # nothing was ever deferred here — the same shape
                # `codegraph_entity_rows_reconciled` already ships with.
                "bundle_retired_hook_registration",
                "removed_retired_hook_registration",
                f"removed the retired {removal['event']} registration "
                f"`{removal['command']}` from .claude/settings.json "
                f"(retired in {retirement.retired_in}: {retirement.reason}); "
                f"replaced by {retirement.audit_replacement}",
                log=log_auto,
            )
    except Exception as _exc:  # noqa: BLE001 — bookkeeping only
        log("4.bundle.settings", "warn",
            f"retired-registration auto-resolution record failed: {_exc}")
    log("4.bundle.settings", "info",
        f"removed {len(removals)} retired hook "
        f"registration(s) from settings.json",
        data={"count": len(removals)})


# ── the machine interface (v0.2.95 F7, the eager-prune half) ──────────────
#
# The scrub above walks a project's ``settings.json``. A PARKED entry is, by
# construction, not in that file: disabling a hook MOVES its entry into
# ``launcher.db``'s ``project_hooks.disabled_entry_json``. So a hook the user
# disabled BEFORE its retirement keeps a launcher row labelled "Disabled
# (restorable)" for a script the same update deleted — a control offering an
# action that can never succeed.
#
# The refusal half of that is already closed Python-side
# (``hooks_settings.insert_hook`` raises ``hook_retired``, so every restore
# path — the Hooks tab, the hub's two PATCH routes, the ``vco hooks enable``
# CLI — refuses through ONE decision). This CLI closes the EAGER half: the
# launcher asks, at Hooks-tab load, which of its parked rows are dead, and
# releases those bytes before the user clicks anything.
#
# Batched by design: a tab load holds N parked rows, and N interpreter starts
# is not a load. One call, one answer per pair, in input order.
#
# Why a CLI and not a table Rust could read (A>B>C, the A leg): matching needs
# `normalize_command` AND the anchored invoked-script walk. A Rust copy of the
# TABLE alone would be useless, and a Rust copy of the MATCHERS is how a
# near-miss eventually deletes a user's own hook. The subprocess is paid once
# per tab load — a user action, not a hot loop.


def _match_pairs(pairs: Any) -> list:
    """One verdict per input pair, in input order.

    Every pair gets a row, retired or not, so the caller can zip the answer
    against what it sent instead of re-deriving identity on its side. The three
    descriptive fields are ``""`` on a non-retired row rather than absent: a
    stable shape is what lets a typed consumer parse the answer without
    branching.

    Raises:
        ValueError: if ``pairs`` is not a list of objects. A malformed request
            is REFUSED, never silently read as "nothing is retired" — that
            answer would look identical to a successful call and would leave
            dead rows parked forever.
    """
    if not isinstance(pairs, list):
        raise ValueError("`pairs` must be a list of {event, command} objects")
    out: list = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ValueError(f"pairs[{index}] is not an object")
        event = pair.get("event")
        command = pair.get("command")
        if not isinstance(event, str) or not isinstance(command, str):
            raise ValueError(
                f"pairs[{index}] needs string `event` and `command` fields"
            )
        match = match_retired_registration(
            event, command, hook_identity=vco_hook_script_identity(command)
        )
        out.append(
            {
                "event": event,
                "command": command,
                "retired": match is not None,
                "retired_in": match.retired_in if match else "",
                "replacement": match.audit_replacement if match else "",
                "reason": match.reason if match else "",
            }
        )
    return out


def _emit(payload: dict) -> None:
    """One JSON object on stdout and nothing else — the house machine contract
    (``stdout is a machine contract``, v0.2.84)."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cmd_match(_args: "argparse.Namespace") -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else None
    except ValueError as exc:
        _emit(
            {
                "ok": False,
                "code": "bad_request",
                "error": f"stdin is not valid JSON: {exc}",
            }
        )
        return 2
    if not isinstance(request, dict):
        _emit(
            {
                "ok": False,
                "code": "bad_request",
                "error": 'stdin must be a JSON object of the form {"pairs": [...]}',
            }
        )
        return 2
    try:
        matches = _match_pairs(request.get("pairs", []))
    except ValueError as exc:
        _emit({"ok": False, "code": "bad_request", "error": str(exc)})
        return 2
    _emit({"ok": True, "matches": matches})
    return 0


def build_parser() -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.hook_retirements",
        description=(
            "Ask the retirement table about hook registrations. Machine "
            "interface: one JSON object on stdout, on every path."
        ),
    )
    sub = parser.add_subparsers(dest="op", required=True)
    p_match = sub.add_parser(
        "match",
        help=(
            "Classify a batch of (event, command) pairs read from stdin as "
            'retired or not. Request: {"pairs":[{"event":..,"command":..}]}.'
        ),
    )
    p_match.add_argument(
        "--json",
        action="store_true",
        help=(
            "Accepted for symmetry with the other vco_lib machine CLIs. "
            "Output is JSON either way — this command has no human mode, so "
            "the flag can never change the contract a caller depends on."
        ),
    )
    p_match.set_defaults(func=_cmd_match)
    return parser


def main(argv: "Optional[list]" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


__all__ = [
    "KIND_COMMAND",
    "KIND_HOOK_SCRIPT",
    "RETIRED_REGISTRATIONS",
    "RetiredRegistration",
    "build_parser",
    "emit_removal_audit_rows",
    "main",
    "match_retired_registration",
    "normalize_command",
    "removal_envelope_rows",
    "scrub_retired_registrations",
    "vco_hook_script_identity",
]


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
