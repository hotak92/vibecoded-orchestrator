# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE writer for a project's ``.claude/settings.json`` ``hooks`` block.

v0.2.91 (decision #27). Until this module existed, the launcher's Hooks
tab was a **full placebo**: register / toggle / delete wrote rows into
``launcher.db::project_hooks``, which *nothing* reads. Claude Code's hook
engine reads ``<project>/.claude/settings.json`` directly, so unchecking a
hook did not stop it firing and registering one did not make it fire
(evidence: ``.claude/context/reviews/v0291-wave5-phase2-ux-completeness``
P2-B2 — zero Python readers of ``project_hooks``; ``apply_fs_disable_hook``
never existed while its agent/skill siblings did).

Enforcement therefore has to be an edit to ``settings.json`` itself. This
module is that edit, and it is deliberately the ONLY implementation:

* **Why Python, not a second Rust JSON writer (A>B>C, A-leg).** The
  settings.json *shape* is already owned Python-side —
  :func:`vco_lib.project_init._merge_settings_template_for_bundle` and its
  ``_smart_merge_for_bundle`` / ``_merge_hooks_for_bundle`` helpers are
  what create and update the file on every install and bundle update, and
  the canonical on-disk form (``json.dumps(..., indent=2)`` + trailing
  newline) is *their* output. A Rust writer would be a second home for
  that shape knowledge, and the drift that produces is already documented
  in :mod:`vco_lib.settings_merge`'s docstring for the install.py /
  project_init.py pair. The launcher calls this module as
  ``python -m vco_lib.hooks_settings`` over the RT-4 interpreter ladder
  (``python_resolve::resolve_python_for_vco_lib``) — the same shape
  ``projects_v2.rs`` already uses for ``vco_lib.project_init
  migrate-collections --json``. Hook toggling is a user-action-triggered,
  ms-scale path, so the subprocess cost is irrelevant and the A-leg
  applies.
* The invoked-script tokenizer is shared with
  :func:`vco_lib.project_init._vco_hook_script_identity` (which now
  delegates here) rather than re-derived — same rule.

Operations
==========

``list``
    Report what the file ACTUALLY declares. The launcher renders from
    this; ``project_hooks`` is a mirror + the parked-entry store, never
    the truth about what runs.

``disable``
    Surgically REMOVE one inner hook item from its group. The removed
    item — plus its matcher, its position, and (when the whole group had
    to go) the group's other keys — is returned as a *parked entry* that
    the caller stores verbatim in the DB row so re-enable can restore it.

``enable``
    Restore a parked entry into its block, at its recorded position when
    that position still makes sense.

``register``
    Add a new entry, optionally seeding a starter hook script when the
    referenced file does not exist yet (the ``create_starter_diagram_file``
    pattern: never clobber an existing file).

``unregister``
    Remove the entry. **Never deletes the script file** — same removal
    primitive as ``disable``, different caller intent (the caller drops
    the DB row instead of parking the entry).

Safety contract (every mutating op)
===================================

1. Parse-validate BEFORE: an unparseable ``settings.json`` — or a
   structurally impossible ``hooks`` block — is an honest refusal
   (``ok: false`` + a stable ``code``), never a clobber and never a
   partial write.
2. Refuse to write through a symlinked ``settings.json`` / ``.claude``
   (:func:`vco_lib.symlink_handler.is_symlink_blocking`). The install path
   redirects such writes to a ``.vco-new`` sibling; an interactive toggle
   must NOT do that silently — a redirect the user cannot see is a second
   placebo. Refuse and say why.
3. Parse-validate AFTER: the rendered text is re-parsed and compared to
   the in-memory document. A mismatch aborts the write.
4. The write itself goes through :func:`vco_lib.atomic.atomic_write_text`
   (tempfile + fsync + ``os.replace``) — the house primitive.
5. Only ``doc["hooks"]`` is ever touched. Every other key — ``env``,
   ``permissions``, ``$schema``, ``mcpServers``, anything the user added —
   passes through the parsed document untouched, and key order is
   preserved because :func:`json.loads` preserves document order.

Concurrency — a stated boundary, not a lock
===========================================

There is no cross-process lock on ``settings.json``, and adding one only
here would be worse than none: the OTHER writer of this file is the
bundle-install merge
(:func:`vco_lib.project_init._merge_settings_template_for_bundle`), which
would not take it, so the lock would buy false confidence rather than
mutual exclusion. What holds instead:

* Each write is atomic (tempfile + ``os.replace``), so a reader never
  sees a torn file and a lost race loses a whole edit, never half of one.
* The launcher renders from the FILE on every load, so a lost edit is
  visible immediately rather than remembered wrongly.
* The worst outcome is a parked DB row whose hook is back in the file.
  The effective-hooks view resolves that in the file's favour (the hook
  shows as *Running*), and the next disable overwrites the stale parked
  entry. No corruption, no silent divergence.

Real mutual exclusion across both writers would have to live at a level
that owns them both; it is deliberately not faked here.

Formatting: the file is re-serialised, not byte-patched. Indent width is
sniffed from the existing file (falling back to the house default of 2),
the presence/absence of a trailing newline is preserved, and the
non-ASCII escape convention is the house one
(:data:`CANONICAL_ENSURE_ASCII` — ``json.dumps``'s ``ensure_ascii=True``
default, what the bundle-merge writer emits and what both shipped
templates store), so a round-trip on an already-canonical file is
byte-identical and a non-canonical file is normalised exactly once.
``settings.json`` is frequently VCS-tracked — the launcher copy says so
at the point of action.

Output contract: every subcommand prints exactly ONE JSON object on
stdout and nothing else. Diagnostics go to stderr. Exit code 0 means the
operation was evaluated (read ``ok``); non-zero means it was refused or
errored, and the JSON still carries ``code`` + ``error``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from vco_lib.atomic import atomic_write_text
from vco_lib.symlink_handler import is_symlink_blocking

# ═══════════════════════════════════════════════════════════════════════
# Errors
# ═══════════════════════════════════════════════════════════════════════


class HooksSettingsError(Exception):
    """A refusal with a stable machine-readable ``code``.

    The launcher surfaces ``message`` verbatim to the user and may branch
    on ``code``; codes are part of the contract and must not be renamed
    without updating the Rust caller + its tests.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ═══════════════════════════════════════════════════════════════════════
# Invoked-script tokenizer (shared with project_init's hook-identity)
# ═══════════════════════════════════════════════════════════════════════

# Interpreter tokens: the token AFTER one of these is the script it runs.
# Mirrors the set project_init used before the extraction.
INTERPRETER_TOKENS = frozenset(
    {
        "bash",
        "sh",
        "dash",
        "zsh",
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
    }
)

# PowerShell flags whose VALUE (the next token) is the script to run.
SCRIPT_FLAG_TOKENS = frozenset({"-file", "-command"})

# Shell control operators that reset "command start", so the token after
# them can begin a fresh invocation (e.g. the ``||`` in VCO's disable
# guard ``[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/x.sh``).
CMD_SEPARATOR_TOKENS = frozenset({"||", "&&", ";", "|", "&"})


def invoked_script_tokens(command: str) -> Iterator[str]:
    """Yield, in order, every token of ``command`` that sits at an
    *invocation anchor* — a position where the token would be the script
    or program actually being run.

    Anchors are: index 0, the token after a shell control operator, the
    token after a shell-interpreter token, and the token after a
    PowerShell ``-File`` / ``-Command`` flag. Tokens appearing anywhere
    else are ARGUMENTS (``bash wrapper.sh --target .claude/hooks/x.sh``)
    or pipe operands (``cat .claude/hooks/x.sh | grep foo``) and are
    never yielded — that distinction is what keeps a user's own hook from
    being mistaken for a VCO-shipped one.

    Path separators are normalised (``\\`` → ``/``) and quotes are
    stripped before splitting, so a quoted or Windows-shaped path token
    yields the same string as its POSIX form.

    This is the extracted core of what
    ``project_init._vco_hook_script_identity`` walked inline before
    v0.2.91; that function now consumes this generator so the two callers
    (bundle-merge identity, starter-script path extraction) share one
    tokenizer instead of carrying two copies that can drift.
    """
    if not command or not isinstance(command, str):
        return
    norm = command.replace("\\", "/").replace('"', " ").replace("'", " ")
    tokens = norm.split()
    if not tokens:
        return

    at_command_start = True
    expect_script_value = False  # set after a -File/-Command flag
    for tok in tokens:
        low = tok.lower()
        if expect_script_value:
            # This token is the explicit script value of -File/-Command.
            yield tok
            expect_script_value = False
            at_command_start = False
            continue
        if at_command_start:
            yield tok
            if low in INTERPRETER_TOKENS:
                # The NEXT token is the script this interpreter runs.
                continue
            # A real executable at command-start that isn't an
            # interpreter (`cat`, `my-wrapper.sh`) — everything after it
            # is its arguments, not invocations.
            at_command_start = False
            continue
        if low in CMD_SEPARATOR_TOKENS:
            at_command_start = True
            continue
        if low in SCRIPT_FLAG_TOKENS:
            expect_script_value = True
            continue
        # Plain argument — never an invocation.


# Script-looking tokens we are willing to seed a starter file for. A hook
# command that invokes something else entirely (a binary, an inline
# `python -c`) gets no starter file and says so.
_STARTER_SUFFIXES = (".sh", ".ps1")

# A relative path token with no drive letter, no leading slash, and no
# `..` component. Starter seeding is confined to project-relative paths
# for the same reason `create_starter_diagram_file` confines its write.
_SAFE_REL_TOKEN_RE = re.compile(r"^(?!/)(?![A-Za-z]:)[\w.][\w./-]*$")


def extract_hook_script_path(command: str) -> Optional[str]:
    """Return the project-relative path of the script ``command`` invokes,
    or ``None`` when the command does not invoke a seedable script.

    ``None`` is returned for: a command that invokes no ``.sh`` / ``.ps1``
    script at all, an absolute path, a drive-qualified Windows path, and
    any token containing a ``..`` component. Those are all cases where
    seeding a starter file would either be meaningless or would write
    outside the project — the caller reports "no starter created" rather
    than guessing.
    """
    for tok in invoked_script_tokens(command):
        low = tok.lower()
        if not low.endswith(_STARTER_SUFFIXES):
            continue
        if not _SAFE_REL_TOKEN_RE.match(tok):
            return None
        if ".." in tok.split("/"):
            return None
        return tok
    return None


# ═══════════════════════════════════════════════════════════════════════
# Document load / render
# ═══════════════════════════════════════════════════════════════════════

#: House default indent — what every VCO-written settings.json uses
#: (``json.dumps(..., indent=2)`` at ``project_init._merge_settings_
#: template_for_bundle``).
DEFAULT_INDENT = 2

#: The house canonical ``json.dumps`` convention for settings.json,
#: beyond indent width. There is exactly ONE canonical form and this
#: module does not get a second opinion about it: the file is created and
#: updated by
#: :func:`vco_lib.project_init._merge_settings_template_for_bundle`
#: (``json.dumps(merged, indent=2)`` — Python's ``ensure_ascii=True``
#: default), and both shipped templates are byte-identical to that
#: output, storing every non-ASCII character as a ``\uXXXX`` escape.
#:
#: v0.2.91 wave-5 review MAJOR-2: this module originally rendered with
#: ``ensure_ascii=False``, so the FIRST hook toggle on a real project
#: rewrote the template's ``_template_origin`` / ``_comment`` /
#: ``_env_comment`` lines from escapes to literal em-dashes — gratuitous
#: diff noise in a VCS-tracked file, and a no-op write that was not
#: byte-identical (0/49 entries round-tripped on either shipped
#: template). The mismatch survived the wave because the test fixture was
#: written with the SAME wrong convention as the code under test.
#: :class:`TestRealShippedTemplateRoundTrip` now derives its fixture by
#: RUNNING the house writer over the real templates, and
#: ``test_render_matches_the_house_writers_convention`` pins this
#: constant against that writer's actual output — so drift on either
#: side fails loudly instead of silently.
CANONICAL_ENSURE_ASCII = True

_INDENT_PROBE_RE = re.compile(r"^\n?\{\n(?P<indent>[ \t]+)\S", re.MULTILINE)


def detect_indent(raw: str) -> int:
    """Sniff the indent width of an existing settings.json body.

    Looks at the indentation of the first key inside the top-level
    object. Returns :data:`DEFAULT_INDENT` when the file is minified, uses
    tabs, or is otherwise unreadable as an indent signal — normalising to
    the house form exactly once rather than guessing per-op.
    """
    m = _INDENT_PROBE_RE.search(raw)
    if not m:
        return DEFAULT_INDENT
    indent = m.group("indent")
    if "\t" in indent:
        return DEFAULT_INDENT
    return len(indent) or DEFAULT_INDENT


class SettingsDoc:
    """A parsed ``settings.json`` plus the formatting facts needed to
    render it back without gratuitous diff noise."""

    def __init__(self, path: Path, data: Dict[str, Any], indent: int, trailing_newline: bool):
        self.path = path
        self.data = data
        self.indent = indent
        self.trailing_newline = trailing_newline

    def render(self) -> str:
        """Serialise in the house canonical form.

        Indent width and trailing-newline presence come from the file
        being edited; everything else is :data:`CANONICAL_ENSURE_ASCII`
        — the convention owned by the bundle-merge writer this module
        defers to. Do not localise it here.
        """
        body = json.dumps(
            self.data, indent=self.indent, ensure_ascii=CANONICAL_ENSURE_ASCII
        )
        return body + "\n" if self.trailing_newline else body


def load_settings(path: Path) -> SettingsDoc:
    """Read + parse-validate ``path``.

    Raises:
        HooksSettingsError: ``missing`` (no such file), ``unreadable``
            (I/O error), ``unparseable`` (invalid JSON), ``not_an_object``
            (valid JSON but not a JSON object), or ``hooks_block_malformed``
            (the ``hooks`` key exists with a structurally impossible
            shape). Every one of these leaves the file untouched.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HooksSettingsError(
            "missing",
            f"{path} does not exist. VCO will not create a settings.json "
            f"from a hook edit — run the project's bundle install first.",
        ) from None
    except OSError as exc:
        raise HooksSettingsError("unreadable", f"cannot read {path}: {exc}") from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HooksSettingsError(
            "unparseable",
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}). Refusing to edit it — fix the file by hand first; "
            f"nothing was written.",
        ) from None

    if not isinstance(data, dict):
        raise HooksSettingsError(
            "not_an_object",
            f"{path} parses as {type(data).__name__}, not a JSON object. "
            f"Refusing to edit it; nothing was written.",
        )

    _validate_hooks_block(data, path)
    return SettingsDoc(
        path=path,
        data=data,
        indent=detect_indent(raw),
        trailing_newline=raw.endswith("\n"),
    )


def _validate_hooks_block(data: Dict[str, Any], path: Path) -> None:
    """Reject a ``hooks`` block whose shape we could not edit coherently.

    Tolerant where Claude Code is tolerant (a group may omit ``matcher``;
    an inner item may carry any extra keys) and strict only about the
    container shapes the edit operations index into.
    """
    hooks = data.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise HooksSettingsError(
            "hooks_block_malformed",
            f"{path}: `hooks` is {type(hooks).__name__}, expected an object "
            f"of event -> array. Refusing to edit; nothing was written.",
        )
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise HooksSettingsError(
                "hooks_block_malformed",
                f"{path}: `hooks.{event}` is {type(groups).__name__}, "
                f"expected an array. Refusing to edit; nothing was written.",
            )
        for idx, group in enumerate(groups):
            if not isinstance(group, dict):
                raise HooksSettingsError(
                    "hooks_block_malformed",
                    f"{path}: `hooks.{event}[{idx}]` is "
                    f"{type(group).__name__}, expected an object. Refusing "
                    f"to edit; nothing was written.",
                )
            inner = group.get("hooks")
            if inner is not None and not isinstance(inner, list):
                raise HooksSettingsError(
                    "hooks_block_malformed",
                    f"{path}: `hooks.{event}[{idx}].hooks` is "
                    f"{type(inner).__name__}, expected an array. Refusing "
                    f"to edit; nothing was written.",
                )


def write_settings(doc: SettingsDoc) -> None:
    """Render, re-validate, and atomically write ``doc``.

    The render is re-parsed and compared to the in-memory document before
    the write happens: a serialise/parse mismatch (a non-JSON-safe value
    that slipped in, an encoding surprise) aborts with nothing written,
    rather than replacing a good file with a bad one.

    Raises:
        HooksSettingsError: ``symlink_blocked`` (the file or an ancestor
            is a symlink — VCO never writes through those), ``render_failed``
            (the document does not serialise), ``roundtrip_failed`` (the
            rendered text does not parse back equal), or ``write_failed``.
    """
    _refuse_symlinked_target(doc.path)

    try:
        rendered = doc.render()
    except (TypeError, ValueError) as exc:
        raise HooksSettingsError(
            "render_failed",
            f"refusing to write {doc.path}: the edited document does not "
            f"serialise to JSON ({exc}). Nothing was written.",
        ) from None

    try:
        reparsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise HooksSettingsError(
            "roundtrip_failed",
            f"refusing to write {doc.path}: the rendered document does not "
            f"parse back ({exc.msg}). Nothing was written.",
        ) from None
    if reparsed != doc.data:
        raise HooksSettingsError(
            "roundtrip_failed",
            f"refusing to write {doc.path}: the rendered document does not "
            f"round-trip equal to the edit. Nothing was written.",
        )

    try:
        atomic_write_text(doc.path, rendered)
    except OSError as exc:
        raise HooksSettingsError(
            "write_failed", f"cannot write {doc.path}: {exc}"
        ) from None


def _refuse_symlinked_target(path: Path) -> None:
    """Refuse when ``path`` or an ancestor inside the project is a symlink.

    The install path redirects such writes to a ``.vco-new`` sibling
    (``project_init._write_file_atomic``). An interactive hook toggle must
    NOT do that: a redirect the user cannot see means the toggle silently
    does nothing to the file Claude Code actually reads — which is exactly
    the placebo this module exists to end. Refuse loudly instead.
    """
    if is_symlink_blocking(path):
        raise HooksSettingsError(
            "symlink_blocked",
            f"{path} is a symlink. VCO never writes through symlinks. "
            f"Edit the link's target directly; nothing was written.",
        )
    # Walk a bounded number of ancestors (`.claude/`, the project root)
    # — deeper ancestors are the user's own filesystem layout, not
    # something a hook toggle should be reasoning about.
    for ancestor in list(path.parents)[:2]:
        if is_symlink_blocking(ancestor):
            raise HooksSettingsError(
                "symlink_blocked",
                f"{ancestor} is a symlink, so writing {path} would write "
                f"through it. VCO never does that. Edit the link's target "
                f"directly; nothing was written.",
            )


# ═══════════════════════════════════════════════════════════════════════
# Hook-entry model
# ═══════════════════════════════════════════════════════════════════════

#: Version stamp on parked entries, so a future shape change can be
#: detected rather than silently mis-restored.
#:
#: Still ``1`` after the v0.2.91 wave-5 byte-fidelity fixes: those added
#: ``group_key_index`` / ``event_index`` / ``hooks_key_index`` (+ their
#: ``*_removed`` flags), and every one is OPTIONAL on the read side — an
#: entry parked by an earlier build restores exactly as it did before,
#: just without the ordinal restoration. Bumping would have made those
#: older entries un-restorable ("refusing to guess"), i.e. traded a
#: cosmetic gap for real data loss.
PARKED_SCHEMA_VERSION = 1


def _insert_key_at(container: Dict[str, Any], key: str, value: Any, index: Any) -> None:
    """Put ``key`` back into ``container`` at ordinal position ``index``.

    ``dict`` preserves *insertion* order, so re-adding a key that an edit
    had removed lands it LAST — which silently reorders the rendered
    settings.json even though the data is identical. This rebuilds the
    mapping in place so the key returns where it was.

    ``index`` that is absent, not an ``int`` (``bool`` explicitly
    excluded — it is an ``int`` subclass and a stray ``True`` would mean
    "position 1"), negative, or past the end appends, which is the
    pre-v0.2.91-wave-5 behaviour and the correct fallback for a parked
    entry written by an older build. Existing keys are updated in place,
    never moved.
    """
    if key in container:
        container[key] = value
        return
    keys = list(container.keys())
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(keys):
        container[key] = value
        return
    tail = {k: container[k] for k in keys[index:]}
    for k in tail:
        del container[k]
    container[key] = value
    container.update(tail)


def normalize_matcher(group: Dict[str, Any]) -> str:
    """The launcher's matcher key for a settings.json group.

    A group may legitimately omit ``matcher`` (7 of the shipped template's
    groups do — ``UserPromptSubmit``, ``Stop``, ``SubagentStop``, …), and
    ``project_hooks.matcher`` stores ``''`` for that case. Absent and
    empty therefore normalise to the same key, and the *omission* is
    preserved on the way back out: a group that never had the key does not
    grow one from an edit.
    """
    m = group.get("matcher")
    return m if isinstance(m, str) else ""


def list_hooks(doc: SettingsDoc) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Enumerate every inner hook item the file declares.

    Returns ``(entries, skipped)``. ``entries`` carries one dict per
    innermost command — the same granularity ``populate_hooks`` mirrors
    into ``project_hooks``. ``skipped`` names the positions that were not
    representable (an inner item that is not an object, or has no string
    command) so the caller can report them instead of quietly dropping
    them.
    """
    entries: List[Dict[str, Any]] = []
    skipped: List[str] = []
    hooks = doc.data.get("hooks")
    if not isinstance(hooks, dict):
        return entries, skipped

    for event, groups in hooks.items():
        for g_idx, group in enumerate(groups):
            matcher = normalize_matcher(group)
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for h_idx, item in enumerate(inner):
                if not isinstance(item, dict):
                    skipped.append(f"hooks.{event}[{g_idx}].hooks[{h_idx}]: not an object")
                    continue
                command = item.get("command")
                if not isinstance(command, str) or not command:
                    skipped.append(
                        f"hooks.{event}[{g_idx}].hooks[{h_idx}]: no string `command`"
                    )
                    continue
                timeout = item.get("timeout")
                entries.append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "command": command,
                        "timeout_seconds": timeout if isinstance(timeout, int) else None,
                        "group_index": g_idx,
                        "hook_index": h_idx,
                        "item": item,
                    }
                )
    return entries, skipped


def _locate(
    doc: SettingsDoc, event: str, matcher: str, command: str
) -> Optional[Tuple[int, int]]:
    """Return ``(group_index, hook_index)`` of the first inner item
    matching the natural key, or ``None``."""
    hooks = doc.data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return None
    for g_idx, group in enumerate(groups):
        if not isinstance(group, dict) or normalize_matcher(group) != matcher:
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            continue
        for h_idx, item in enumerate(inner):
            if isinstance(item, dict) and item.get("command") == command:
                return g_idx, h_idx
    return None


def remove_hook(
    doc: SettingsDoc, event: str, matcher: str, command: str
) -> Dict[str, Any]:
    """Surgically remove one inner hook item, returning its parked entry.

    Only the identified item is removed. Sibling hooks in the same group
    stay exactly where they are; a group is dropped only when removing the
    item empties it, and the event key is dropped only when that empties
    the event. The parked entry records enough to put it all back:
    position indices, the item verbatim, and — when the whole group went —
    the group's other keys (so a group that had no ``matcher`` key comes
    back without one) plus the ORDINAL of every key the cascade deleted
    (``group_key_index`` / ``event_index`` / ``hooks_key_index``), so the
    restore puts each one back where it was instead of appending it.

    Raises:
        HooksSettingsError: ``not_found`` when no such entry exists.
    """
    located = _locate(doc, event, matcher, command)
    if located is None:
        raise HooksSettingsError(
            "not_found",
            f"no hook entry `{command}` under {event}"
            + (f" / matcher {matcher!r}" if matcher else " (no matcher)")
            + f" in {doc.path}. Nothing was written.",
        )
    g_idx, h_idx = located
    hooks_block = doc.data["hooks"]
    groups = hooks_block[event]
    group = groups[g_idx]
    inner = group["hooks"]
    item = inner.pop(h_idx)

    group_removed = False
    group_extra: Dict[str, Any] = {}
    # Ordinals of the keys the cascade deletes. A deleted key re-added on
    # restore would land last and reorder the file, so each level records
    # where it was (v0.2.91 wave-5 MAJOR-2 residuals: `PostToolUse[9]`
    # stores `hooks` BEFORE `matcher`, and 6 of the shipped template's
    # events hold a single hook, so disabling it drops the event key).
    group_key_index: Optional[int] = None
    event_index: Optional[int] = None
    hooks_key_index: Optional[int] = None
    if not inner:
        group_extra = {k: v for k, v in group.items() if k != "hooks"}
        group_key_index = list(group.keys()).index("hooks")
        groups.pop(g_idx)
        group_removed = True
        if not groups:
            event_index = list(hooks_block.keys()).index(event)
            del hooks_block[event]
            if not hooks_block:
                hooks_key_index = list(doc.data.keys()).index("hooks")
                del doc.data["hooks"]

    return {
        "schema": PARKED_SCHEMA_VERSION,
        "event": event,
        "matcher": matcher,
        "group_index": g_idx,
        "hook_index": h_idx,
        "item": item,
        "group_removed": group_removed,
        "group_extra": group_extra,
        "group_key_index": group_key_index,
        "event_index": event_index,
        "hooks_key_index": hooks_key_index,
    }


def insert_hook(doc: SettingsDoc, parked: Dict[str, Any]) -> bool:
    """Restore a parked entry. Returns ``True`` when the document changed.

    Position is honoured when it still makes sense and clamped otherwise.
    When the disable left the group in place, the recorded group is
    reused if it still carries the recorded matcher, else the first group
    with that matcher. When the disable REMOVED the group
    (``group_removed``), the group is recreated from ``group_extra`` at
    the recorded index (clamped to the array length) — never merged into
    a surviving same-matcher sibling, which would silently restructure
    the file. Within the group the item goes back at its recorded index,
    clamped the same way. Every key the disable deleted (the group's
    ``hooks``, the event, the whole ``hooks`` block) returns at its
    recorded ordinal rather than at the end.

    Idempotent: if an entry with the same natural key
    (event, matcher, command) is already present anywhere in the event,
    nothing changes and ``False`` is returned — so a double-click, or a
    re-enable after the user restored the line by hand, cannot produce a
    duplicate invocation. That check runs before any structural edit, so
    the ``False`` return leaves the document untouched.

    Raises:
        HooksSettingsError: ``parked_entry_invalid`` when the stored blob
            is not a restorable parked entry.
    """
    if not isinstance(parked, dict):
        raise HooksSettingsError(
            "parked_entry_invalid", "parked entry is not a JSON object."
        )
    if parked.get("schema") != PARKED_SCHEMA_VERSION:
        raise HooksSettingsError(
            "parked_entry_invalid",
            f"parked entry schema {parked.get('schema')!r} is not "
            f"{PARKED_SCHEMA_VERSION}; refusing to guess how to restore it.",
        )
    event = parked.get("event")
    item = parked.get("item")
    if not isinstance(event, str) or not event or not isinstance(item, dict):
        raise HooksSettingsError(
            "parked_entry_invalid",
            "parked entry is missing a string `event` or an object `item`.",
        )
    raw_matcher = parked.get("matcher")
    matcher = raw_matcher if isinstance(raw_matcher, str) else ""
    command = item.get("command")

    # Idempotency FIRST, before any structural edit: the natural key is
    # (event, matcher, command), so a double-click — or a re-enable after
    # the user put the line back by hand in a sibling group with the same
    # matcher — must change nothing at all. Checking event-wide (the same
    # `_locate` `register_hook` uses, one convention) rather than only
    # inside the target group is what makes the `False` return provably
    # non-mutating and keeps the restore from adding a duplicate
    # invocation to a different group.
    if isinstance(command, str) and _locate(doc, event, matcher, command) is not None:
        return False

    hooks = doc.data.get("hooks")
    if hooks is None:
        hooks = {}
        _insert_key_at(doc.data, "hooks", hooks, parked.get("hooks_key_index"))
    if not isinstance(hooks, dict):  # pragma: no cover — load_settings rejects this
        raise HooksSettingsError("hooks_block_malformed", "`hooks` is not an object.")
    groups = hooks.get(event)
    if groups is None:
        groups = []
        _insert_key_at(hooks, event, groups, parked.get("event_index"))
    if not isinstance(groups, list):  # pragma: no cover — load_settings rejects this
        raise HooksSettingsError(
            "hooks_block_malformed", f"`hooks.{event}` is not an array."
        )

    g_idx = parked.get("group_index")
    target: Optional[Dict[str, Any]] = None
    # Only a group that SURVIVED the disable may be reused. When the
    # disable emptied and removed the group, the surviving groups have
    # shifted down into its index — and events legitimately carry several
    # groups with the SAME matcher (the shipped template has three
    # `PreToolUse`/`Bash` groups), so both the index probe and the
    # first-matching-matcher fallback would drop the item into a
    # *different* group and change the file's structure. Recreating the
    # recorded group is the faithful restore. (v0.2.91 wave-5 MAJOR-2
    # residual: 9 of the template's 49 entries relocated this way.)
    if not parked.get("group_removed"):
        if (
            isinstance(g_idx, int)
            and 0 <= g_idx < len(groups)
            and isinstance(groups[g_idx], dict)
            and normalize_matcher(groups[g_idx]) == matcher
        ):
            target = groups[g_idx]
        else:
            for group in groups:
                if isinstance(group, dict) and normalize_matcher(group) == matcher:
                    target = group
                    break

    if target is None:
        extra = parked.get("group_extra")
        target = dict(extra) if isinstance(extra, dict) else {}
        # A group that never carried `matcher` must not grow one; only a
        # non-empty matcher with no surviving group_extra needs the key
        # written back explicitly.
        if matcher and "matcher" not in target:
            target["matcher"] = matcher
        _insert_key_at(target, "hooks", [], parked.get("group_key_index"))
        at = g_idx if isinstance(g_idx, int) and 0 <= g_idx <= len(groups) else len(groups)
        groups.insert(at, target)

    inner = target.setdefault("hooks", [])
    if not isinstance(inner, list):  # pragma: no cover — load_settings rejects this
        raise HooksSettingsError(
            "hooks_block_malformed", f"`hooks.{event}[].hooks` is not an array."
        )

    h_idx = parked.get("hook_index")
    at = h_idx if isinstance(h_idx, int) and 0 <= h_idx <= len(inner) else len(inner)
    inner.insert(at, item)
    return True


def register_hook(
    doc: SettingsDoc,
    event: str,
    matcher: str,
    command: str,
    timeout_seconds: Optional[int] = None,
) -> bool:
    """Add a new hook entry. Returns ``True`` when the document changed.

    Appends the inner item to the existing group with the same matcher
    when one exists (so registering a second ``Stop`` hook joins the
    group rather than creating a parallel one), else appends a new group.

    Idempotent by natural key: registering an entry that is already
    present returns ``False`` and changes nothing.
    """
    if not isinstance(event, str) or not event.strip():
        raise HooksSettingsError("invalid_event", "event must be a non-empty string.")
    if not isinstance(command, str) or not command.strip():
        raise HooksSettingsError("invalid_command", "command must be a non-empty string.")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, int) or timeout_seconds <= 0
    ):
        raise HooksSettingsError(
            "invalid_timeout", "timeout must be a positive whole number of seconds."
        )

    if _locate(doc, event, matcher, command) is not None:
        return False

    item: Dict[str, Any] = {"type": "command", "command": command}
    if timeout_seconds is not None:
        item["timeout"] = timeout_seconds

    hooks = doc.data.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    for group in groups:
        if isinstance(group, dict) and normalize_matcher(group) == matcher:
            inner = group.setdefault("hooks", [])
            if isinstance(inner, list):
                inner.append(item)
                return True
    new_group: Dict[str, Any] = {}
    if matcher:
        new_group["matcher"] = matcher
    new_group["hooks"] = [item]
    groups.append(new_group)
    return True


# ═══════════════════════════════════════════════════════════════════════
# Starter-script seeding
# ═══════════════════════════════════════════════════════════════════════

_STARTER_SH = """\
#!/usr/bin/env bash
# Starter hook created by the VCT launcher for the `{event}` event.
#
# Claude Code runs this on every matching event. The JSON event payload
# arrives on stdin; anything you print on stdout is shown to Claude.
# Exit 0 to allow the event to proceed.
#
# This file is yours — VCO will not overwrite it.
set -euo pipefail

# payload=$(cat)
echo "[{script_name}] fired on {event}" >&2
exit 0
"""

_STARTER_PS1 = """\
# Starter hook created by the VCT launcher for the `{event}` event.
#
# Claude Code runs this on every matching event. The JSON event payload
# arrives on stdin; anything you write to stdout is shown to Claude.
# Exit 0 to allow the event to proceed.
#
# This file is yours - VCO will not overwrite it.
$ErrorActionPreference = 'Stop'

# $payload = [Console]::In.ReadToEnd()
Write-Error "[{script_name}] fired on {event}"
exit 0
"""


def starter_content(script_name: str, event: str) -> str:
    """Minimal runnable starter body for a hook script, chosen by suffix.

    Mirrors ``diagrams_cmd::starter_content_for``: a valid, immediately
    runnable seed with a comment explaining the contract, not a stub that
    errors on first fire.
    """
    template = _STARTER_PS1 if script_name.lower().endswith(".ps1") else _STARTER_SH
    return template.format(script_name=script_name, event=event)


def create_starter_script(
    project_folder: Path, command: str, event: str
) -> Optional[Dict[str, Any]]:
    """Seed the hook script ``command`` invokes, when it does not exist.

    Returns ``None`` when the command names no seedable script; otherwise
    a dict with ``path`` and ``created`` (``False`` = the file already
    existed and was left untouched, the non-clobber rule
    ``create_starter_diagram_file`` follows).

    The write is confined to ``project_folder``: the path token is
    already vetted as project-relative by
    :func:`extract_hook_script_path`, and the resolved destination is
    re-checked against the resolved project root before anything is
    written.
    """
    rel = extract_hook_script_path(command)
    if rel is None:
        return None
    dest = project_folder / rel

    try:
        root_resolved = project_folder.resolve()
        dest_resolved = (project_folder / rel).resolve()
    except OSError as exc:
        raise HooksSettingsError(
            "starter_path_unresolvable", f"cannot resolve {dest}: {exc}"
        ) from None
    if root_resolved != dest_resolved and root_resolved not in dest_resolved.parents:
        raise HooksSettingsError(
            "starter_path_escapes_project",
            f"refusing to create {dest_resolved}: it is outside {root_resolved}.",
        )

    if dest.exists():
        return {"path": str(dest), "created": False}
    if is_symlink_blocking(dest.parent):
        raise HooksSettingsError(
            "symlink_blocked",
            f"{dest.parent} is a symlink; refusing to create a hook script "
            f"through it.",
        )

    try:
        atomic_write_text(dest, starter_content(Path(rel).name, event))
    except OSError as exc:
        raise HooksSettingsError(
            "starter_write_failed", f"cannot create {dest}: {exc}"
        ) from None
    if not dest.name.lower().endswith(".ps1") and os.name != "nt":
        # Shell hooks are invoked as `bash <path>` by the shipped
        # template, so the executable bit is not load-bearing — but a user
        # who rewrites the command to invoke the script directly will
        # expect it, and chmod is free. Soft-fail: a filesystem without
        # mode bits must not fail the registration.
        try:
            dest.chmod(0o755)
        except OSError:
            pass
    return {"path": str(dest), "created": True}


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def _settings_path(args: argparse.Namespace) -> Path:
    if args.settings:
        return Path(args.settings)
    return Path(args.project_folder) / ".claude" / "settings.json"


def _emit(payload: Dict[str, Any]) -> None:
    """Print exactly one JSON object on stdout. stdout is a machine
    contract here — nothing else may be written to it."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cmd_list(args: argparse.Namespace) -> int:
    path = _settings_path(args)
    doc = load_settings(path)
    entries, skipped = list_hooks(doc)
    _emit(
        {
            "ok": True,
            "settings_path": str(path),
            "hooks": [
                {k: v for k, v in e.items() if k != "item"} for e in entries
            ],
            "skipped": skipped,
        }
    )
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    path = _settings_path(args)
    doc = load_settings(path)
    parked = remove_hook(doc, args.event, args.matcher, args.command)
    write_settings(doc)
    _emit(
        {
            "ok": True,
            "settings_path": str(path),
            "changed": True,
            # `parked` is for humans reading the output. `parked_json` is the
            # canonical thing a caller must STORE, and callers must store it
            # verbatim. Rust's `serde_json::Value` is backed by a BTreeMap
            # unless the `preserve_order` feature is on, so parsing `parked`
            # into a Value and re-serialising it SORTS the inner hook item's
            # keys — `{type, command, timeout}` comes back as
            # `{command, timeout, type}` and the restored file no longer
            # matches the original byte-for-byte. Handing the caller a
            # pre-serialised string removes the opportunity.
            "parked": parked,
            "parked_json": json.dumps(parked, ensure_ascii=False),
        }
    )
    return 0


def _cmd_unregister(args: argparse.Namespace) -> int:
    """Same removal primitive as ``disable`` — one home. The difference is
    caller intent: ``unregister`` drops the DB row instead of parking the
    entry, and it NEVER deletes the hook script file."""
    path = _settings_path(args)
    doc = load_settings(path)
    removed = remove_hook(doc, args.event, args.matcher, args.command)
    write_settings(doc)
    _emit(
        {
            "ok": True,
            "settings_path": str(path),
            "changed": True,
            "removed": removed,
            "script_file_deleted": False,
        }
    )
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    path = _settings_path(args)
    try:
        parked = json.loads(args.entry_json)
    except json.JSONDecodeError as exc:
        raise HooksSettingsError(
            "parked_entry_invalid", f"--entry-json is not valid JSON: {exc.msg}"
        ) from None
    doc = load_settings(path)
    changed = insert_hook(doc, parked)
    if changed:
        write_settings(doc)
    _emit({"ok": True, "settings_path": str(path), "changed": changed})
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    path = _settings_path(args)
    doc = load_settings(path)
    changed = register_hook(
        doc, args.event, args.matcher, args.command, args.timeout_seconds
    )
    starter = None
    if changed:
        write_settings(doc)
    if args.create_starter:
        if not args.project_folder:
            raise HooksSettingsError(
                "project_folder_required",
                "--create-starter needs --project-folder to resolve the "
                "script path against.",
            )
        starter = create_starter_script(
            Path(args.project_folder), args.command, args.event
        )
    _emit(
        {
            "ok": True,
            "settings_path": str(path),
            "changed": changed,
            "starter": starter,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.hooks_settings",
        description=(
            "Edit a project's .claude/settings.json hooks block. Machine "
            "interface: every subcommand prints one JSON object on stdout."
        ),
    )
    sub = parser.add_subparsers(dest="op", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--settings",
            help="Path to settings.json. Defaults to "
            "<project-folder>/.claude/settings.json.",
        )
        p.add_argument(
            "--project-folder",
            help="Project root. Required unless --settings is given.",
        )

    def _key(p: argparse.ArgumentParser) -> None:
        p.add_argument("--event", required=True)
        p.add_argument(
            "--matcher",
            default="",
            help="Empty (the default) matches a group with no `matcher` key.",
        )
        p.add_argument("--command", required=True)

    p_list = sub.add_parser("list", help="Report what settings.json declares.")
    _common(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_disable = sub.add_parser(
        "disable", help="Remove one entry, returning it as a parked entry."
    )
    _common(p_disable)
    _key(p_disable)
    p_disable.set_defaults(func=_cmd_disable)

    p_enable = sub.add_parser("enable", help="Restore a parked entry.")
    _common(p_enable)
    p_enable.add_argument("--entry-json", required=True)
    p_enable.set_defaults(func=_cmd_enable)

    p_register = sub.add_parser("register", help="Add a new entry.")
    _common(p_register)
    _key(p_register)
    p_register.add_argument("--timeout-seconds", type=int, default=None)
    p_register.add_argument(
        "--create-starter",
        action="store_true",
        help="Seed the invoked hook script when it does not exist "
        "(never overwrites an existing file).",
    )
    p_register.set_defaults(func=_cmd_register)

    p_unregister = sub.add_parser(
        "unregister",
        help="Remove an entry. Never deletes the hook script file.",
    )
    _common(p_unregister)
    _key(p_unregister)
    p_unregister.set_defaults(func=_cmd_unregister)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.settings and not args.project_folder:
        _emit(
            {
                "ok": False,
                "code": "no_target",
                "error": "one of --settings / --project-folder is required.",
            }
        )
        return 2
    try:
        return args.func(args)
    except HooksSettingsError as exc:
        _emit({"ok": False, "code": exc.code, "error": exc.message})
        return 1


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())
