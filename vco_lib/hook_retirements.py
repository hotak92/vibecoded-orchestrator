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

v0.2.101 consumers of the SAME table: :func:`carry_parked_async_disables`
(the bundle update's migration of a parked — disabled — retired async
registration into the per-project ``DISABLED_STEMS_ENV_KEY``), and the
``match`` CLI's ``carry_pending`` answer, with which the launcher's eager
prune holds a carry-pending row's bytes until the project's update has run
(``commands/project_hooks_settings.rs::parked_rows_to_prune``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from vco_lib.hooks_settings import anchor_hook_command, invoked_script_tokens

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


def hook_command_key(command: str) -> str:
    """The key under which two spellings of ONE hook registration compare
    equal: :func:`normalize_command` of the command with its project hook
    scripts anchored (:func:`vco_lib.hooks_settings.anchor_hook_command`).

    v0.2.97: the relative ``bash .claude/hooks/x.sh`` a pre-v0.2.97 install
    (or a launcher-parked entry, or the launcher DB's mirror row) still holds
    and the anchored form a bundle update now writes
    (``bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/x.sh"`` on Linux/macOS,
    ``powershell … -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.ps1"`` on
    Windows) are the same hook; so are the guard-prefixed and bare forms, and
    every anchored spelling the cycle wrote.
    Narrower than ``parked_hooks.same_hook_command`` (script basename): it is
    what the hooks editor matches a named entry by, and what the launcher's
    Hooks tab keys rows by, so it must not merge two DIFFERENT commands that
    happen to run the same script.
    """
    return normalize_command(anchor_hook_command(command))


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
        async_only: (v0.2.101, review SF-3) match ONLY registrations that
            positively carry ``"async": true``. The v0.2.101 dispatcher rows
            set it: what VCO retired is its own ASYNC registration shape —
            a user's hand-made SYNCHRONOUS PostToolUse registration of the
            same script is theirs and is left alone (scrub, parked
            re-enable refusal and the launcher's prune classifier all take
            the evidence through ``match_retired_registration``'s
            ``is_async`` argument). Callers that cannot tell pass ``None``
            and async-only rows do NOT match — no positive evidence, no
            removal.
    """

    event: str
    kind: str
    target: str
    reason: str
    retired_in: str
    replacement: str
    async_only: bool = False

    @property
    def audit_replacement(self) -> str:
        """The replacement clause for the audit row (never empty prose)."""
        return self.replacement or "nothing (the capability was removed)"


#: The six scripts whose ASYNC PostToolUse registrations merged into the
#: single ``post-tool-use-async`` dispatcher in v0.2.101 (the scripts keep
#: shipping; the dispatcher routes to them). ONE list, three consumers: the
#: retirement rows below, :func:`carry_parked_async_disables`, and — by
#: derivation through the routing-table parity test — the dispatchers'
#: ROUTE_TABLEs (tests/test_v02101_async_posttooluse_dispatcher.py pins the
#: row set and the table stems to each other, so this list cannot drift
#: from what the dispatcher actually routes).
_ASYNC_MERGED_STEMS_V02101: Tuple[str, ...] = (
    "post-edit-outcome",
    "post-bash-context-record",
    "kg-summary-generator",
    "post-git-commit-kg-sync",
    "post-file-delete",
    "kg-update-nudge",
)

#: Deferral recorded when the SF-2 carry's env-file write FAILS (v0.2.101
#: re-review SF-A). While this row is open in a project's UPDATE_DEFERRED
#: ledger the launcher's prune gate keeps the parked bytes (the user's only
#: copy of their pre-merge disable), the next bundle update retries the
#: carry, and the probe ``async_subhook_disable_carry_still_owed`` clears
#: the row once the key holds every parked stem (or the evidence is gone).
#: Declared in ``vco_lib/deferral_conditions.toml``.
CARRY_FAILED_CID = "async_subhook_disable_carry_failed"

#: The per-project sub-hook disable key (v0.2.101 review SF-2 — the
#: per-registration toggle the merge would otherwise have lost). Lives in
#: ``<project>/.claude/env`` — the SAME per-project hook-knob channel the
#: lean-ctx toggle uses: the launcher writes it with its existing
#: ``set_claude_env_value`` command (``commands/claude_env.rs``, one home
#: for the file format on the GUI side), both dispatcher siblings read it
#: (the ``.sh`` sources the file like ``lean-ctx-rewrite.sh`` does, the
#: ``.ps1`` scans it like ``lean-ctx-rewrite.ps1`` does), and
#: :func:`vco_lib.envfile.upsert_env_key` is the Python writer the bundle
#: migration uses (mirroring the Rust ``write_key`` semantics). Value:
#: comma-separated hook STEMS (``kg-summary-generator,post-file-delete``);
#: a routed stem in the list is skipped by the dispatcher.
DISABLED_STEMS_ENV_KEY = "VCO_ASYNC_DISABLED_HOOKS"

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
    # ── the eight async PostToolUse registrations, merged in v0.2.101 ──────
    #
    # Unlike the retirements above, these SCRIPTS STILL SHIP — what is
    # retired is their individual PostToolUse REGISTRATIONS. Pre-v0.2.101
    # the settings templates carried eight async PostToolUse entries across
    # the six scripts below; one Bash tool call spawned up to 3 async
    # processes, and every async run that spoke (any stdout/stderr — e.g.
    # the kg-update-nudge E2BIG "Argument list too long" class) or died
    # (timeout) wrote a ~660 B `async_hook_response` attachment into the
    # session transcript. Measured on one maintainer machine: 700,330 such
    # records / 462.3 MB in a single transcript. The single
    # `post-tool-use-async.{sh,ps1}` dispatcher registration (matcher `*`,
    # async, timeout 15) now reads the hook stdin once and routes it to the
    # SAME unchanged scripts by tool_name, guaranteeing silence (child
    # stdout discarded; child stderr/non-zero exits condense to one line in
    # `<VCO metrics dir>/post-tool-use-async.log`).
    #
    # EVENT-SCOPED on purpose: every row is PostToolUse only, so
    # `kg-update-nudge`'s SYNC UserPromptSubmit + SessionStart(compact)
    # registrations (and any user registration of these scripts under
    # another event) are untouched by the scrub. Rows exist per OS
    # extension because the identity walk returns the BASENAME WITH
    # extension — a Windows install's `.ps1` registration must match too.
    #
    # ASYNC-ONLY (v0.2.101 review SF-3): every row also carries
    # `async_only=True`, so ONLY a registration that positively carries
    # `"async": true` matches — VCO shipped these six scripts under
    # PostToolUse exclusively as async registrations, and a user's own
    # SYNCHRONOUS registration of one of them is theirs: the scrub, the
    # parked re-enable refusal and the launcher's prune classifier all
    # leave it alone. Callers that cannot see the flag pass `is_async=None`
    # and get the same conservative leave-alone.
    #
    # DISABLE CARRIED ACROSS THE MERGE (review SF-2): pre-merge each of the
    # eight registrations was an individually toggleable Hooks-tab row, and
    # that granularity is kept — see `DISABLED_STEMS_ENV_KEY` and
    # :func:`carry_parked_async_disables`. The classifier's
    # `carry_pending` answer (below) additionally keeps a PARKED retired
    # row's bytes out of the launcher's eager prune until the project's
    # bundle update has run the carry — otherwise a Hooks-tab load before
    # the update would release the user's only copy of their disable.
    *(
        RetiredRegistration(
            event="PostToolUse",
            kind=KIND_HOOK_SCRIPT,
            target=f"{stem}.{ext}",
            reason=(
                "the ASYNC PostToolUse registration (async_only: a "
                "synchronous registration of this script is the user's own "
                "and is never matched) was superseded in v0.2.101 by the "
                "merged async dispatcher: each separate async registration "
                "wrote a ~660 B async_hook_response transcript record "
                "whenever it spoke or died (measured: 700k records / "
                "462 MB in one session transcript). The script itself "
                "still ships — the dispatcher routes to it"
            ),
            retired_in="v0.2.101",
            replacement=(
                f".claude/hooks/post-tool-use-async.{'sh' if ext == 'sh' else 'ps1'} "
                f"(the single async PostToolUse dispatcher; routes payloads "
                f"to {stem} by tool_name — switch this sub-hook off "
                f"individually with {DISABLED_STEMS_ENV_KEY} in "
                f"<project>/.claude/env or the launcher's Hooks tab)"
            ),
            async_only=True,
        )
        for stem in _ASYNC_MERGED_STEMS_V02101
        for ext in ("sh", "ps1")
    ),
)


def match_retired_registration(
    event: str,
    command: str,
    *,
    hook_identity: Optional[str],
    is_async: Optional[bool] = None,
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
        is_async: (v0.2.101, review SF-3) the registration's ``"async"``
            flag — ``True``/``False`` when the caller can see it (the scrub
            walks the inner-hook dicts; the parked paths parse the entry
            blob), ``None`` when it cannot. Rows with ``async_only`` match
            ONLY on ``is_async is True``: no positive evidence, no removal.
            Callers WITH the evidence must pass it; omitting it silently
            degrades to the conservative leave-alone, never to a deletion.

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
        if entry.async_only and is_async is not True:
            continue  # SF-3: only a positively-async registration matches
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
                    _async = h.get("async") if isinstance(h, dict) else None
                    match = match_retired_registration(
                        event, cmd,
                        hook_identity=vco_hook_script_identity(cmd),
                        # SF-3: the scrub SEES the inner-hook dict, so the
                        # async-only rows get their positive evidence here.
                        is_async=None if not isinstance(_async, bool) else _async,
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


def carry_parked_async_disables(
    folder: Path,
    parked: Any,
    *,
    dry_run: bool = False,
    log_auto: Any = None,
    log: Any = None,
) -> list:
    """Carry the user's DISABLED retired async registrations across the
    v0.2.101 merge (review SF-2, the migration half).

    Pre-merge, disabling an async PostToolUse hook from the launcher parked
    its registration (bytes in ``launcher.db``). The merge retires that
    registration — and a retired registration the dispatcher replaces would
    otherwise come back ON: the routed sub-hook runs unless something says
    otherwise. So the bundle update reads the parked state it ALREADY holds
    for the merge (``parked`` is the caller's
    :class:`vco_lib.parked_hooks.ParkedHooksState`, duck-typed to keep this
    module's import graph acyclic) and unions the stem of every parked row
    that matches an ``async_only`` retirement into the per-project
    ``DISABLED_STEMS_ENV_KEY`` in ``<folder>/.claude/env`` — the same key
    both dispatcher siblings read and the Hooks tab writes, so a disable
    that predates the merge keeps working through it with ONE channel.

    Conservative throughout: an unreadable parked state carries nothing
    (the caller's merge already warns about it); a row without positive
    ``is_async`` evidence carries nothing (a user's own sync registration
    of a shipped script stays theirs — SF-3); a failed env write is a
    logged warning, never a failed update. Idempotent: stems already in
    the key are not re-added, the writer skips a no-op rewrite, and the
    return value is the list of NEWLY carried stems (empty on a repeat
    run). ``dry_run`` reports what WOULD be carried without writing —
    the envelope on both paths, same rule as the removals.

    Returns the newly-carried stems, in parked-row order.

    FAILURE (re-review SF-A): when the env-file write (or read) fails, the
    owed work is RECORDED — a :data:`CARRY_FAILED_CID` deferral row — and
    the classifier's ``carry_pending`` answer keeps the launcher's prune
    from releasing the parked bytes while that row is open, so a failed
    carry can never silently re-enable a sub-hook the user had turned off.
    The next update retries (this function runs on every update); a
    successful run closes the row, and the registry probe
    ``async_subhook_disable_carry_still_owed`` closes it on the hand-fix
    path too (same computation, one home — it can never clear an entry the
    next update would re-emit).
    """
    if parked is None or not getattr(parked, "readable", False):
        return []

    stems = _carry_candidate_stems(parked)
    existing = _read_disabled_stems(folder)
    if existing is None:
        # The env file exists but cannot be read — the same owed-work class
        # as a failed write. No evidence, no guess: record and keep.
        if stems:
            if log is not None:
                log("4.bundle.settings", "warn",
                    f"cannot read .claude/env to carry the parked async-hook "
                    f"disables ({DISABLED_STEMS_ENV_KEY}); the owed work is "
                    f"deferred")
            if not dry_run:
                _emit_carry_failed(
                    folder, stems,
                    "<project>/.claude/env cannot be read", log,
                )
        return []
    new = [s for s in stems if s not in existing]
    if dry_run:
        return new
    if new:
        from vco_lib import envfile  # lazy: keeps the module graph acyclic
        try:
            envfile.upsert_env_key(
                folder / ".claude" / "env",
                DISABLED_STEMS_ENV_KEY, ",".join(existing + new),
            )
        except OSError as exc:
            if log is not None:
                log("4.bundle.settings", "warn",
                    f"could not carry the parked async-hook disables into "
                    f".claude/env ({DISABLED_STEMS_ENV_KEY}): {exc}")
            _emit_carry_failed(folder, new, str(exc), log)
            return []
    # Landed (or nothing was owed): close our own owed-work row. Cheap read
    # first — the resolve cycle rewrites the ledger, and an update on a
    # healthy project must not churn it.
    if _carry_deferral_open(folder):
        try:
            from vco_lib import deferral_emit as _de
            _de.resolve_conditions(folder, [CARRY_FAILED_CID], log=log_auto)
        except Exception as _exc:  # noqa: BLE001 — bookkeeping only
            if log is not None:
                log("4.bundle.settings", "warn",
                    f"could not close the {CARRY_FAILED_CID} deferral: {_exc}")
    if not new:
        return []
    if log_auto is not None:
        try:
            from vco_lib import deferral_emit as _de
            _de.record_auto_resolution(
                folder,
                # The same auto-resolution-only condition id the retired-
                # registration removals use: nothing was deferred here.
                "bundle_retired_hook_registration",
                "carried_parked_async_disable",
                f"carried the parked (user-disabled) retired async "
                f"PostToolUse registration(s) for {', '.join(new)} into "
                f"{DISABLED_STEMS_ENV_KEY} in .claude/env, so the merged "
                f"post-tool-use-async dispatcher keeps them switched off "
                f"(retired in v0.2.101; the launcher's Hooks tab toggles "
                f"the same key)",
                log=log_auto,
            )
        except Exception as _exc:  # noqa: BLE001 — bookkeeping only
            if log is not None:
                log("4.bundle.settings", "warn",
                    f"async-disable carry auto-resolution record failed: {_exc}")
    if log is not None:
        log("4.bundle.settings", "info",
            f"carried {len(new)} parked async-hook disable(s) into "
            f".claude/env {DISABLED_STEMS_ENV_KEY}",
            data={"stems": new})
    return new


def _carry_candidate_stems(parked: Any) -> list:
    """The stems of parked rows matching an ``async_only`` retirement — the
    ONE computation shared by the carry writer, its owed-work probe and (via
    them) the prune gate, so the probe can never clear an entry the next
    update would re-emit. Duck-typed :class:`~vco_lib.parked_hooks.ParkedHooksState`."""
    stems: list = []
    for ph in getattr(parked, "hooks", ()) or ():
        command = getattr(ph, "command", "")
        if not command:
            continue
        match = match_retired_registration(
            getattr(ph, "event", ""), command,
            hook_identity=vco_hook_script_identity(command),
            is_async=getattr(ph, "is_async", None),
        )
        if match is None or not match.async_only:
            continue
        stem = match.target.rsplit(".", 1)[0]
        if stem not in stems:
            stems.append(stem)
    return stems


def _read_disabled_stems(folder: Path) -> Optional[list]:
    """The current ``DISABLED_STEMS_ENV_KEY`` list for ``folder``.

    ``[]`` when the file or the key is absent; ``None`` when the file
    exists but cannot be read — no evidence, and every caller treats that
    as "keep / still owed", never as a guess. LAST occurrence wins (the
    semantics the .sh dispatcher's `source` and the launcher's `read_key`
    give the key).
    """
    from vco_lib import envfile  # lazy: keeps the module graph acyclic

    env_path = Path(folder) / ".claude" / "env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    value: Optional[str] = None
    for k, v in envfile.parse_env_lines(text):
        if k == DISABLED_STEMS_ENV_KEY:
            value = v
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _carry_deferral_open(folder: Path) -> bool:
    """True when the project's UPDATE_DEFERRED ledger holds an open
    :data:`CARRY_FAILED_CID` row. Used by the carry's success path to close
    its own row without churning a healthy project's ledger (the prune GATE
    keys on :func:`carry_still_owed`, not on this row — re-review-2 nit 3:
    a row that failed to write cannot be the evidence). Any doubt (an
    unreadable ledger) answers True — the conservative direction, which
    here only means "run the idempotent resolve"."""
    try:
        from vco_lib.deferral_report import DeferralReport
        return any(
            getattr(e, "condition_id", None) == CARRY_FAILED_CID
            for e in DeferralReport.read(Path(folder)).entries
        )
    except Exception:  # noqa: BLE001 — unreadable is not "cleared"
        return True


def _emit_carry_failed(
    folder: Path, stems: list, error: str, log: Any,
) -> None:
    """Record the owed carry as an UPDATE_DEFERRED row (SF-A). Best-effort
    like every ledger write here: a failure to record is logged, never
    raised — the update itself must not fail."""
    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        emit(folder, DeferralEntry(
            condition_id=CARRY_FAILED_CID,
            title=(
                "A hook you disabled could not be carried into the async "
                "dispatcher's disable list"
            ),
            detected=(
                f"the bundle update tried to carry the parked (user-disabled) "
                f"retired async PostToolUse registration(s) for "
                f"{', '.join(stems)} into {DISABLED_STEMS_ENV_KEY} in "
                f".claude/env, and the env file could not be used: {error}"
            ),
            why_deferred=(
                "until the key is written, the merged post-tool-use-async "
                "dispatcher would run the sub-hook(s) you had switched off, "
                "and the launcher keeps your parked disable bytes protected "
                "while this row is open — releasing them now would destroy "
                "the only record of your choice"
            ),
            command_to_apply=(
                "make <project>/.claude/env writable as a regular FILE "
                "(permission bit / immutable flag / a directory in the way), "
                "then re-run the bundle update — it retries the carry and "
                f"clears this row. Or write the key by hand: "
                f"{DISABLED_STEMS_ENV_KEY}={','.join(stems)} in "
                "<project>/.claude/env (the launcher's Hooks tab toggles the "
                "same key)."
            ),
        ), log=log)
    except Exception as _exc:  # noqa: BLE001 — best-effort ledger
        if log is not None:
            log("4.bundle.settings", "warn",
                f"could not record the {CARRY_FAILED_CID} deferral: {_exc}")


def carry_still_owed(folder: Path) -> Optional[bool]:
    """``async_subhook_disable_carry_failed`` probe semantics (registered in
    :mod:`vco_lib.deferral_probes`).

    True  — at least one parked candidate stem is still missing from the key
            (the next update would re-emit; the entry must stand);
    False — the key holds every candidate (or there is no parked evidence
            left): the owed work is done;
    None  — the parked state or the env file cannot be read: no verdict,
            keep the entry (positive evidence only, the house probe rule).
    """
    try:
        from vco_lib.parked_hooks import read_parked_hooks  # lazy: import cycle
        state = read_parked_hooks(Path(folder))
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None
    if not getattr(state, "readable", False):
        return None
    stems = _carry_candidate_stems(state)
    if not stems:
        return False
    have = _read_disabled_stems(Path(folder))
    if have is None:
        return None
    return any(s not in have for s in stems)


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
# path — the Hooks tab, the hub's two PATCH routes, the ``vct-cli hooks enable``
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


def _dispatcher_registration_present(project_folder: Any) -> bool:
    """True when the project's settings.json already carries the merged
    ``post-tool-use-async`` registration — the positive signal that the
    bundle update (and with it :func:`carry_parked_async_disables`) has
    run. False on ANY doubt (absent folder, unreadable or unparseable
    settings, no PostToolUse array, no dispatcher identity): an answer
    that cannot be positive keeps the launcher's eager prune OFF a parked
    row, which is the conservative direction — releasing the bytes is
    irreversible, keeping them costs one honest row in the Hooks tab.
    """
    if not isinstance(project_folder, str) or not project_folder:
        return False
    try:
        raw = (
            Path(project_folder) / ".claude" / "settings.json"
        ).read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    post = hooks.get("PostToolUse") if isinstance(hooks, dict) else None
    if not isinstance(post, list):
        return False
    for group in post:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks") or []:
            cmd = h.get("command") if isinstance(h, dict) else None
            if not isinstance(cmd, str) or not cmd:
                continue
            if vco_hook_script_identity(cmd) in (
                "post-tool-use-async.sh", "post-tool-use-async.ps1",
            ):
                return True
    return False


def _match_pairs(pairs: Any, project_folder: Any = None) -> list:
    """One verdict per input pair, in input order.

    Every pair gets a row, retired or not, so the caller can zip the answer
    against what it sent instead of re-deriving identity on its side. The three
    descriptive fields are ``""`` on a non-retired row rather than absent: a
    stable shape is what lets a typed consumer parse the answer without
    branching.

    v0.2.101 (review SF-2/SF-3) extension, optional and additive: the
    request may carry a top-level ``project_folder``. It enables BOTH
    halves the ``async_only`` rows need, and it keeps the launcher's
    side of the contract unchanged — pairs stay identity-level
    ``(event, matcher?, command)``; the parked BYTES never travel (the
    Rust-side pin ``the_request_carries_every_parked_pair_and_nothing_else``
    stays true). With the folder, this process:

      * resolves the ``async`` evidence ITSELF, by reading the project's
        parked rows through :func:`vco_lib.parked_hooks.read_parked_hooks`
        (read-only) — the entry blob's shape stays owned by Python, one
        home. No folder / no parked row / unreadable DB ⇒ no positive
        evidence ⇒ ``async_only`` rows answer not-retired (conservative:
        the launcher keeps the row, and the refusal half in
        ``insert_hook`` — which DOES hold the blob — still catches it at
        click time);
      * answers ``carry_pending`` on a matched ``async_only`` row: True
        unless the carry has POSITIVE evidence of success (re-review SF-A,
        hardened by re-review-2 nit 3) — the project's settings.json holds
        the dispatcher registration AND :func:`carry_still_owed` answers
        exactly ``False`` (the key holds every parked stem; the same
        one-home computation the deferral probe uses). Still-owed (True —
        including a double failure whose ledger row never landed) and
        unreadable evidence (None) both keep the launcher's eager prune
        OFF the row's bytes: they are the user's only disable evidence
        until the carry lands. ``carry_pending`` is always present (False
        when not applicable) — stable shape.

    Raises:
        ValueError: if ``pairs`` is not a list of objects. A malformed request
            is REFUSED, never silently read as "nothing is retired" — that
            answer would look identical to a successful call and would leave
            dead rows parked forever.
    """
    if not isinstance(pairs, list):
        raise ValueError("`pairs` must be a list of {event, command} objects")
    out: list = []
    carry_gate_open: Optional[bool] = None
    parked_index: Optional[dict] = None
    _MISSING = object()

    def _parked_is_async(event: str, matcher: Any, command: str) -> Optional[bool]:
        """The parked blob's async flag for this identity, or None.

        Lazy (one read-only DB open per request, only when an async_only
        candidate exists) and keyed (event, matcher, command) with an
        (event, command) fallback for callers on the pre-matcher shape."""
        nonlocal parked_index
        if parked_index is None:
            parked_index = {}
            if isinstance(project_folder, str) and project_folder:
                try:
                    from vco_lib.parked_hooks import read_parked_hooks
                    state = read_parked_hooks(Path(project_folder))
                    if state.readable:
                        for ph in state.hooks:
                            parked_index[(ph.event, ph.matcher, ph.command)] = ph.is_async
                            # (event, command) fallback for a pre-matcher
                            # request shape: True when ANY parked row for
                            # that command is positively async, else the
                            # last non-True evidence seen.
                            prev = parked_index.get((ph.event, ph.command))
                            parked_index[(ph.event, ph.command)] = (
                                True if (prev is True or ph.is_async is True)
                                else ph.is_async
                            )
                except Exception:  # noqa: BLE001 — no evidence, conservative
                    parked_index = {}
        if isinstance(matcher, str):
            # A row FOUND with is_async None (blob without the flag) is the
            # answer for that exact row — it must not fall through to the
            # any-positive (event, command) fallback and borrow a twin's
            # evidence. Sentinel distinguishes "absent" from "None".
            keyed = parked_index.get((event, matcher, command), _MISSING)
            if keyed is not _MISSING:
                return keyed  # type: ignore[return-value]
        return parked_index.get((event, command))

    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ValueError(f"pairs[{index}] is not an object")
        event = pair.get("event")
        command = pair.get("command")
        if not isinstance(event, str) or not isinstance(command, str):
            raise ValueError(
                f"pairs[{index}] needs string `event` and `command` fields"
            )
        identity = vco_hook_script_identity(command)
        is_async: Optional[bool] = None
        if identity and any(
            e.async_only and e.event == event and e.target == identity
            for e in RETIRED_REGISTRATIONS
        ):
            is_async = _parked_is_async(event, pair.get("matcher"), command)
        match = match_retired_registration(
            event, command, hook_identity=identity, is_async=is_async,
        )
        carry_pending = False
        if match is not None and match.async_only:
            if carry_gate_open is None:
                # SF-A, hardened by re-review-2 nit 3: the gate requires
                # POSITIVE EVIDENCE of carry success — the dispatcher
                # registration present AND the owed computation (the carry
                # deferral probe's OWN one-home rule) answering exactly
                # False ("the key holds every parked stem"). True (still
                # owed — including the DOUBLE-failure case where the carry's
                # own failure row could not be recorded either) and None
                # (unreadable evidence) both KEEP the bytes: the absence of
                # a failure row cannot distinguish "carry landed" from
                # "recording the failure failed too". The
                # `async_subhook_disable_carry_failed` ledger row remains —
                # for user visibility and the update-retry trail — but is no
                # longer a gate input.
                try:
                    owed = carry_still_owed(Path(project_folder))
                except Exception:  # noqa: BLE001 — no verdict is a keep
                    owed = None
                carry_gate_open = (
                    _dispatcher_registration_present(project_folder)
                    and owed is False
                )
            carry_pending = not carry_gate_open
        out.append(
            {
                "event": event,
                "command": command,
                "retired": match is not None,
                "retired_in": match.retired_in if match else "",
                "replacement": match.audit_replacement if match else "",
                "reason": match.reason if match else "",
                "carry_pending": carry_pending,
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
        matches = _match_pairs(
            request.get("pairs", []),
            project_folder=request.get("project_folder"),
        )
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
            'retired or not. Request: {"pairs":[{"event":..,"command":..,'
            '"matcher":..?}],"project_folder":..?}. The optional '
            "project_folder lets the classifier resolve the v0.2.101 "
            "async-only rows' evidence itself (read-only, from the "
            "project's parked rows) and adds the carry_pending answer that "
            "gates the launcher's eager prune until the bundle update has "
            "carried a parked disable into .claude/env. Pairs stay "
            "identity-level: parked bytes never travel."
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
    "DISABLED_STEMS_ENV_KEY",
    "KIND_COMMAND",
    "KIND_HOOK_SCRIPT",
    "RETIRED_REGISTRATIONS",
    "RetiredRegistration",
    "build_parser",
    "carry_parked_async_disables",
    "emit_removal_audit_rows",
    "hook_command_key",
    "main",
    "match_retired_registration",
    "normalize_command",
    "removal_envelope_rows",
    "scrub_retired_registrations",
    "vco_hook_script_identity",
]


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
