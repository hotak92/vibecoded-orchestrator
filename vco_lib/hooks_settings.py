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
  :func:`vco_lib.project_init._merge_settings_template_for_bundle` and the
  merge algorithm it delegates to (:mod:`vco_lib.settings_merge`) are
  what create and update the file on every install and bundle update, and
  the canonical on-disk form (``json.dumps(..., indent=2)`` + trailing
  newline) is *their* output. A Rust writer would be a second home for
  that shape knowledge, and a second home for this exact shape has already
  had to be closed once: install.py carried a mirror of the merge until
  v0.2.85 (D2) routed the root install through the one bundle engine. The launcher calls this module as
  ``python -m vco_lib.hooks_settings`` over the RT-4 interpreter ladder
  (``python_resolve::resolve_python_for_vco_lib``) — the same shape
  ``projects_v2.rs`` already uses for ``vco_lib.project_init
  migrate-collections --json``. Hook toggling is a user-action-triggered,
  ms-scale path, so the subprocess cost is irrelevant and the A-leg
  applies.
* The invoked-script tokenizer is shared with
  :func:`vco_lib.hook_retirements.vco_hook_script_identity` (which delegates
  here, and which ``project_init`` re-exports under its historical private
  name ``_vco_hook_script_identity``) rather than re-derived — same rule.

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
    When the natural key (event, matcher, command) matches SEVERAL items
    in one group — the ``if``-rule group v0.2.101 ships (ten rules that
    differ only by their ``if`` filter) — ALL of them are parked as ONE
    entry (``items``) and removed together, so the group is toggled as a
    unit and re-enable restores every rule byte-identically.

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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from vco_lib import jsonc_edit
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
# them can begin a fresh invocation. STILL LOAD-BEARING post-v0.2.97: the
# settings templates stopped shipping the ``||``-prefixed disable guard
# (``[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/x.sh``) in v0.2.97,
# but installs written before that release carry the prefixed command and
# bundle update must keep recognising (and superseding) those entries; and
# a USER's own compound command (``cmd1 && cmd2``, ``a; b``) is ordinary
# input in any era.
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
    for index in _anchor_indices(tokens):
        yield tokens[index]


def _anchor_indices(tokens: Sequence[str]) -> Iterator[int]:
    """The anchor walk behind :func:`invoked_script_tokens`, over a token list
    — the indices of the tokens at an invocation anchor. Empty tokens are
    skipped (:func:`anchor_hook_command` walks a whitespace-preserving split,
    which yields one when the command starts with whitespace)."""
    at_command_start = True
    expect_script_value = False  # set after a -File/-Command flag
    for index, tok in enumerate(tokens):
        if not tok:
            continue
        low = tok.lower()
        if expect_script_value:
            # This token is the explicit script value of -File/-Command.
            yield index
            expect_script_value = False
            at_command_start = False
            continue
        if at_command_start:
            yield index
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


# ═══════════════════════════════════════════════════════════════════════
# Anchoring hook paths at the project root (v0.2.97)
# ═══════════════════════════════════════════════════════════════════════

#: Claude Code's "project root where the session started". It substitutes
#: this placeholder in a hook's ``command`` AND exports it as an environment
#: variable, so the double-quoted shell form resolves either way: where
#: Claude Code substitutes it the shell sees a literal quoted path; where it
#: only exports it, ``sh`` / Git Bash expand ``${CLAUDE_PROJECT_DIR}`` inside
#: the double quotes. The quotes keep a project path with spaces one argument.
PROJECT_DIR_PLACEHOLDER = "${CLAUDE_PROJECT_DIR}"

#: The form VCO ships on Linux/macOS (v0.2.97 review R8, G2/M1): the POSIX
#: ``:-`` default makes the command degrade to the project cwd when NEITHER
#: mechanism is present (a client that neither substitutes the placeholder
#: nor exports the variable) instead of expanding to ``/.claude/hooks/x.sh`` —
#: never worse than the pre-v0.2.97 relative form, which resolved against the
#: session cwd exactly like ``.`` does here. NOT used on Windows: ``:-``
#: parameter expansion is POSIX-only and would break under the PowerShell
#: shell fallback Claude Code uses when Git Bash is absent, so Windows
#: commands keep the exact placeholder — substitution is the documented
#: mechanism and works whichever shell runs the command.
PROJECT_DIR_FALLBACK_PLACEHOLDER = "${CLAUDE_PROJECT_DIR:-.}"

#: An INVOKED hook script under the project's ``.claude/hooks/`` — relative
#: (``.claude/hooks/x.sh``, ``./.claude/hooks/x.sh``) or already anchored at
#: the project root (``$CLAUDE_PROJECT_DIR/…``, ``${CLAUDE_PROJECT_DIR}/…``,
#: ``${CLAUDE_PROJECT_DIR:-.}/…``), after quote-stripping and ``\`` → ``/``.
#: The group is the basename.
_PROJECT_HOOK_SCRIPT_RE = re.compile(
    r"^(?:\./|\$CLAUDE_PROJECT_DIR/|\$\{CLAUDE_PROJECT_DIR\}/|\$\{CLAUDE_PROJECT_DIR:-\.\}/)?"
    r"\.claude/hooks/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:sh|ps1))$"
)

#: An INVOKED script under the project's ``.claude/scripts/`` (v0.2.101,
#: NB-13) — the same relative / anchored spellings as the hook regex above.
#: The subpath allows ``/`` segments and extension-less scripts
#: (``kg-sync``, ``lib/vct_project_config.sh``, ``lib/vct_project_config.ps1``),
#: so the group is the project-relative SUBPATH, not a basename — basenames
#: collide across the script subfolders.
_PROJECT_SCRIPT_RE = re.compile(
    r"^(?:\./|\$CLAUDE_PROJECT_DIR/|\$\{CLAUDE_PROJECT_DIR\}/|\$\{CLAUDE_PROJECT_DIR:-\.\}/)?"
    r"\.claude/scripts/([A-Za-z0-9][A-Za-z0-9._/-]*)$"
)


def _invoked_project_script(token: str) -> Optional[Tuple[str, str]]:
    """``(identity, project-relative path)`` for an INVOKED project script
    token, or ``None``.

    * ``.claude/hooks/<basename>.{sh,ps1}`` → identity is the BASENAME
      (``x.sh``) — the same key :func:`vco_lib.hook_relative_paths.relative_scripts`
      returns for a hook and the same key the ``only=`` filter has always used.
    * ``.claude/scripts/<subpath>`` (v0.2.101 NB-13) → identity is the
      project-relative path (``.claude/scripts/kg-sync``), because a script
      basename collides across subfolders.

    The token must already be quote-stripped and ``\\`` → ``/`` normalised
    (the caller's ``norm`` list); an anchored spelling resolves to the same
    identity as its relative form, which is what makes the rewrite idempotent.
    """
    match = _PROJECT_HOOK_SCRIPT_RE.match(token)
    if match is not None:
        return match.group(1), f".claude/hooks/{match.group(1)}"
    match = _PROJECT_SCRIPT_RE.match(token)
    if match is not None:
        relative = f".claude/scripts/{match.group(1)}"
        return relative, relative
    return None


def anchor_hook_command(command: str, *, only: Optional[Iterable[str]] = None) -> str:
    """``command`` with every INVOKED project script anchored at the project
    root, in the per-OS form VCO ships:

    * a ``.sh`` script (or an extension-less one, e.g. ``kg-sync``) →
      ``"${CLAUDE_PROJECT_DIR:-.}/.claude/…"``
      (:data:`PROJECT_DIR_FALLBACK_PLACEHOLDER` — the POSIX ``:-`` default
      keeps the command resolving when Claude Code provides neither the
      placeholder substitution nor the exported variable);
    * a ``.ps1`` script → ``"${CLAUDE_PROJECT_DIR}/.claude/…"``
      (:data:`PROJECT_DIR_PLACEHOLDER` — the exact placeholder, because
      ``:-`` is POSIX-only and breaks under the PowerShell shell fallback).

    Why (v0.2.97): Claude Code runs a hook command in the session's CURRENT
    directory, and that follows ``cd`` and worktrees. Every hook VCO shipped
    was ``bash .claude/hooks/x.sh`` (or ``… -File .claude/hooks/x.ps1``), so
    once a session's cwd moved every one of them failed with "No such file or
    directory". Anchored at ``${CLAUDE_PROJECT_DIR}`` they resolve from
    anywhere; at the session's starting directory the two forms name the same
    file, so the rewrite changes nothing else.

    Scope (v0.2.101, NB-13): TWO token shapes are anchored — an
    ``.claude/hooks/<basename>`` hook and an ``.claude/scripts/<subpath>``
    script. Both are PROJECT-relative paths that fail after a ``cd`` for the
    same reason, and both are matched only at an invocation anchor.

    Only tokens at an invocation anchor (the same walk as
    :func:`invoked_script_tokens`) are rewritten — a path that is an ARGUMENT
    (``bash wrap.sh --target .claude/hooks/x.sh``) is left alone — and
    everything else in the command (interpreter, flags, trailing arguments,
    whitespace) is kept byte-for-byte. An already-anchored token in EITHER
    spelling (exact placeholder or ``:-.`` fallback) is normalised to the one
    per-OS quoted form, so the result is idempotent AND an install migrated to
    the exact-placeholder Linux form earlier in the cycle is rewritten to the
    fallback form, never duplicated.

    ``only`` limits the rewrite to those script IDENTITIES: a hook's identity
    is its basename (``x.sh``), a script's identity is its project-relative
    path (``.claude/scripts/kg-sync``) — exactly the values
    :func:`vco_lib.hook_relative_paths.relative_scripts` returns. ``None``
    anchors every project script (hook or ``.claude/scripts/``); that is the
    shared :func:`vco_lib.hook_retirements.hook_command_key` path, where two
    spellings of one registration must compare equal — including a
    ``.claude/scripts/`` invocation a user rewrote with
    ``python -m vco_lib.hook_relative_paths anchor`` while a launcher DB mirror
    row still holds the old relative form.
    """
    if not isinstance(command, str) or not command:
        return command
    allowed = None if only is None else set(only)
    pieces = re.split(r"(\s+)", command)
    tokens = pieces[0::2]
    norm = [t.replace("\\", "/").strip("\"'") for t in tokens]
    changed = False
    for index in _anchor_indices(norm):
        found = _invoked_project_script(norm[index])
        if found is None:
            continue
        identity, relative = found
        if allowed is not None and identity not in allowed:
            continue
        form = PROJECT_DIR_PLACEHOLDER if relative.endswith(".ps1") else PROJECT_DIR_FALLBACK_PLACEHOLDER
        anchored = f'"{form}/{relative}"'
        if tokens[index] != anchored:
            tokens[index] = anchored
            changed = True
    if not changed:
        return command
    pieces[0::2] = tokens
    return "".join(pieces)


def shipped_hook_scripts(install_root: Optional[Path] = None) -> Optional[set]:
    """The ``.claude/hooks/<name>`` basenames VCO's settings templates
    register (both OS flavours), read from the orchestrator clone — or
    ``None`` when the templates cannot be read (the caller then anchors
    nothing: a user's own hook is never rewritten on a guess)."""
    from vco_lib.hook_retirements import vco_hook_script_identity  # noqa: PLC0415 — import cycle
    from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

    root = install_root if install_root is not None else resolve_install_root()
    if root is None:
        return None
    names: set = set()
    for flavour in ("linux", "windows"):
        path = Path(root) / "templates" / f"settings.json.{flavour}.template"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for groups in (data.get("hooks") or {}).values():
            for group in groups if isinstance(groups, list) else []:
                for item in (group.get("hooks") or []) if isinstance(group, dict) else []:
                    ident = vco_hook_script_identity(item.get("command") or "") if isinstance(item, dict) else None
                    if ident:
                        names.add(ident)
    return names




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
        # v0.2.97: the project-root-anchored forms VCO ships and suggests
        # (`bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/x.sh"` on Linux,
        # `powershell … -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.ps1"`
        # on Windows) name the same project-relative script.
        for prefix in (
            PROJECT_DIR_PLACEHOLDER + "/",
            PROJECT_DIR_FALLBACK_PLACEHOLDER + "/",
            "$CLAUDE_PROJECT_DIR/",
        ):
            if tok.startswith(prefix):
                tok = tok[len(prefix):]
                break
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

    def __init__(self, path: Path, data: Dict[str, Any], indent: int, trailing_newline: bool,
                 jsonc_text: Optional[str] = None):
        self.path = path
        self.data = data
        self.indent = indent
        self.trailing_newline = trailing_newline
        #: The original text when the file is JSONC (comments / trailing
        #: commas): it is then EDITED in place, never re-serialised.
        self.jsonc_text = jsonc_text

    def render(self) -> str:
        """Serialise in the house canonical form — or, for a JSONC file,
        the original text edited member by member with every comment kept
        (:func:`vco_lib.jsonc_edit.rewrite_preserving`, v0.2.97), which
        raises :class:`vco_lib.jsonc_edit.JsoncEditRefused` when that edit
        cannot be made and verified.

        Indent width and trailing-newline presence come from the file
        being edited; everything else is :data:`CANONICAL_ENSURE_ASCII`
        — the convention owned by the bundle-merge writer this module
        defers to. Do not localise it here.
        """
        if self.jsonc_text is not None:
            return jsonc_edit.rewrite_preserving(self.jsonc_text, self.data)
        body = json.dumps(
            self.data, indent=self.indent, ensure_ascii=CANONICAL_ENSURE_ASCII
        )
        return body + "\n" if self.trailing_newline else body


def load_settings(path: Path) -> SettingsDoc:
    """Read + parse-validate ``path``.

    Raises:
        HooksSettingsError: ``missing`` (no such file), ``unreadable``
            (I/O error or not UTF-8), ``unparseable`` (neither JSON nor
            JSONC — a JSONC file, which Claude Code accepts, is read and
            later edited in place, v0.2.97), ``not_an_object``
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
    except (OSError, ValueError) as exc:  # ValueError: not UTF-8
        raise HooksSettingsError("unreadable", f"cannot read {path}: {exc}") from None

    try:
        data = jsonc_edit.loads(raw)
    except ValueError as exc:
        raise HooksSettingsError(
            "unparseable",
            f"{path} is not valid JSON or JSONC ({exc}). Refusing to edit it — "
            f"fix the file by hand first; nothing was written.",
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
        jsonc_text=None if jsonc_edit.is_strict_json(raw) else raw,
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
    except jsonc_edit.JsoncEditRefused as exc:
        _record_jsonc_refusal(doc.path, exc)
        raise HooksSettingsError(
            "jsonc_edit_refused",
            f"refusing to write {doc.path}: it has comments or trailing commas "
            f"(JSONC) and this change could not be made in place without "
            f"risking other content ({exc.message}). Edit it by hand, or "
            f"remove the comments; nothing was written.",
        ) from None
    except (TypeError, ValueError) as exc:
        raise HooksSettingsError(
            "render_failed",
            f"refusing to write {doc.path}: the edited document does not "
            f"serialise to JSON ({exc}). Nothing was written.",
        ) from None

    try:
        reparsed = jsonc_edit.loads(rendered)
    except ValueError as exc:
        raise HooksSettingsError(
            "roundtrip_failed",
            f"refusing to write {doc.path}: the rendered document does not "
            f"parse back ({exc}). Nothing was written.",
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
    root = _project_root_of(doc.path)
    if root is not None:
        # The paired clear of a refusal THIS writer recorded. Writer-scoped on
        # purpose (v0.2.97 review F19): the env projection's refusal of the
        # same file is a different edit (a duplicated `env` key refuses its
        # edit while `hooks` edits fine), and only ITS next success proves it
        # over — clearing it here would be a false clear until the next
        # re-projection re-recorded it.
        from vco_lib import settings_refusal

        settings_refusal.clear_recorded(root, _SETTINGS_SURFACE)


#: This writer's ``settings_refusal`` surface — ``settings_write_refused_
#: hooks_claude_settings_json``. One surface per WRITER of the file, the rule
#: the bundle merge set (``bundle_claude_settings_json``): each refusal is a
#: different edit, so each is recorded and cleared by the writer that made it,
#: and the env projection keeps ``claude_settings_json`` for its own.
_SETTINGS_SURFACE = "hooks_claude_settings_json"


def _project_root_of(path: Path) -> Optional[Path]:
    """The project folder when ``path`` is ``<project>/.claude/settings.json``."""
    return path.parent.parent if path.parent.name == ".claude" else None


def _record_jsonc_refusal(path: Path, exc: "jsonc_edit.JsoncEditRefused") -> None:
    """Leave the refused JSONC edit in the project's deferral ledger
    (:mod:`vco_lib.settings_refusal`) so it is visible beyond the one error
    the Hooks tab shows. Soft-fail: the file is already safe."""
    root = _project_root_of(path)
    if root is None:
        return
    try:
        from vco_lib import settings_refusal

        settings_refusal.record(root, _SETTINGS_SURFACE, settings_refusal.Refusal(
            path, settings_refusal.KIND_EDIT_REFUSED,
            "it has comments or trailing commas (JSONC) and a hook change could "
            f"not be made in place without risking other content ({exc.message}); "
            "edit it by hand, or remove the comments",
        ), retry_command="# Retry the change from the launcher: Projects -> this project -> Hooks.")
    except Exception:  # noqa: BLE001 — the ledger is observability
        pass


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
#:
#: Still ``1`` after the v0.2.101 ``if``-group change, same reasoning:
#: a group park carries ``items`` (a list of per-rule records) INSTEAD of
#: ``item``, and the read side treats ``items`` as optional — a row parked
#: by an older build has no ``items`` and restores exactly as it did
#: before. The converse is a loud refusal by design: an OLD build handed
#: a group park finds no ``item`` and refuses with ``parked_entry_invalid``
#: rather than restoring one rule of ten and silently dropping nine.
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
                if_rule = item.get("if")
                entries.append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "command": command,
                        "timeout_seconds": timeout if isinstance(timeout, int) else None,
                        # v0.2.101: the entry's `if` filter (None when it has
                        # none). Several entries under one (event, matcher,
                        # command) key differ ONLY by this — the Hooks tab
                        # renders them as one toggleable row.
                        "if": if_rule if isinstance(if_rule, str) else None,
                        "group_index": g_idx,
                        "hook_index": h_idx,
                        "item": item,
                    }
                )
    return entries, skipped


#: Sentinel for "match any ``if`` filter" — distinct from ``None``, which
#: is a MEANINGFUL ``if`` value ("the item has no ``if`` key"). Callers
#: that predate the v0.2.101 ``if``-group change pass nothing and get the
#: historical, ``if``-blind behaviour.
_ANY_IF = object()


def _if_matches(item: Dict[str, Any], if_rule: Any) -> bool:
    """Whether ``item``'s ``if`` key equals ``if_rule``.

    Only called with a real ``if_rule`` (never the :data:`_ANY_IF`
    sentinel): the value must be a string or ``None`` and must equal the
    item's ``if`` (also normalised to string-or-``None``), so an if-less
    entry and a ``"Bash(cat *)"`` entry never compare equal.
    """
    actual = item.get("if")
    return (actual if isinstance(actual, str) else None) == (
        if_rule if isinstance(if_rule, str) else None
    )


def _locate(
    doc: SettingsDoc, event: str, matcher: str, command: str, if_rule: Any = _ANY_IF
) -> Optional[Tuple[int, int]]:
    """Return ``(group_index, hook_index)`` of the first inner item
    matching the natural key, or ``None``.

    An exact command match wins. Failing that (v0.2.97), the ONE item under
    the event + matcher whose command is the same registration in another
    spelling (:func:`vco_lib.hook_retirements.hook_command_key` — the relative
    ``.claude/hooks/`` form a launcher DB mirror row still holds vs the
    ``${CLAUDE_PROJECT_DIR}``-anchored form a bundle update wrote, or the
    pre-v0.2.97 guard prefix). More than one such item is ambiguous and
    matches nothing.

    ``if_rule`` (v0.2.101): pass a string or ``None`` to require the item's
    ``if`` filter to equal it — the identity of one rule inside an ``if``
    group. Unset (the sentinel) matches any ``if``, the pre-v0.2.101
    behaviour every existing caller relies on."""
    hooks = doc.data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return None
    candidates: List[Tuple[int, int, str]] = []
    for g_idx, group in enumerate(groups):
        if not isinstance(group, dict) or normalize_matcher(group) != matcher:
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            continue
        for h_idx, item in enumerate(inner):
            if not isinstance(item, dict):
                continue
            other = item.get("command")
            if other == command:
                if if_rule is not _ANY_IF and not _if_matches(item, if_rule):
                    continue
                return g_idx, h_idx
            if isinstance(other, str) and other:
                if if_rule is _ANY_IF or _if_matches(item, if_rule):
                    candidates.append((g_idx, h_idx, other))
    if not isinstance(command, str) or not command or not candidates:
        return None
    from vco_lib.hook_retirements import hook_command_key  # noqa: PLC0415 — import cycle

    key = hook_command_key(command)
    same = [(g, h) for g, h, other in candidates if hook_command_key(other) == key]
    return same[0] if len(same) == 1 else None


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

    v0.2.101, ``if`` groups: the natural key (event, matcher, command) is
    no longer unique — a group may legitimately carry SEVERAL items with
    the SAME command that differ only by their ``if`` filter (the shipped
    Bash PreToolUse injection group carries ten). When that is the shape
    on disk, the WHOLE set inside the first matching group is removed and
    parked as one entry carrying ``items`` (one per-rule record) instead
    of ``item`` — the launcher's Hooks tab toggles the set as ONE row, and
    re-enable restores every rule byte-identically. Removing the items
    one at a time is exactly what cannot work here: after the first
    removal the survivors still answer to the same key, so a later
    enable's idempotency check would see "already present" and restore
    nothing. Copies of the command in OTHER same-matcher groups (a
    user's hand-built shape) are left alone: they are indistinguishable
    from this group's items by any key the DB stores, and guessing would
    restructure the file.

    Raises:
        HooksSettingsError: ``not_found`` when no such entry exists.
    """
    hooks_block = doc.data.get("hooks")
    if isinstance(hooks_block, dict):
        groups = hooks_block.get(event)
        if isinstance(groups, list):
            for g_idx, group in enumerate(groups):
                if not isinstance(group, dict) or normalize_matcher(group) != matcher:
                    continue
                inner = group.get("hooks")
                if not isinstance(inner, list):
                    continue
                positions = [
                    (h_idx, item)
                    for h_idx, item in enumerate(inner)
                    if isinstance(item, dict) and item.get("command") == command
                ]
                if len(positions) >= 2:
                    return _remove_if_group(doc, event, matcher, g_idx, positions)
                # Zero or one exact copy in this group — keep walking; a
                # lone copy falls through to the historical `_locate`
                # path below, which resolves across groups (fuzzy
                # spellings included).
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


def _remove_if_group(
    doc: SettingsDoc,
    event: str,
    matcher: str,
    g_idx: int,
    positions: List[Tuple[int, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Remove every ``if``-rule item parked as one group entry.

    ``positions`` is the ascending ``(hook_index, item)`` list of the
    group's items carrying the same command. They are popped in DESCENDING
    index order so each recorded index stays valid as the list shrinks;
    the parked entry stores them ASCENDING (restore order), each with its
    own ``hook_index``, under ``items`` — and deliberately WITHOUT an
    ``item`` key, so an older build handed this blob refuses with
    ``parked_entry_invalid`` instead of restoring one rule of the group
    and silently dropping the rest.
    """
    hooks_block = doc.data["hooks"]
    groups = hooks_block[event]
    group = groups[g_idx]
    inner = group["hooks"]
    for h_idx, _item in sorted(positions, key=lambda p: p[0], reverse=True):
        inner.pop(h_idx)

    group_removed = False
    group_extra: Dict[str, Any] = {}
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
        # The FIRST rule's index — same meaning as a single park's
        # `hook_index`, and what a human reading the blob sees first.
        "hook_index": positions[0][0],
        "items": [
            {"hook_index": h_idx, "item": item} for h_idx, item in positions
        ],
        "group_removed": group_removed,
        "group_extra": group_extra,
        "group_key_index": group_key_index,
        "event_index": event_index,
        "hooks_key_index": hooks_key_index,
    }


def _hook_present_in_another_form(
    doc: SettingsDoc, event: str, matcher: str, command: str, if_rule: Any = _ANY_IF
) -> bool:
    """True when ``event`` / ``matcher`` already registers the same hook as
    ``command`` under a different command string.

    ``if_rule`` (v0.2.101): pass a string or ``None`` to also require the
    ``if`` filter to match — one rule of an ``if`` group, not the group's
    other rules."""
    # Lazy: `parked_hooks` imports this module (see the retirement import in
    # `insert_hook` for the same cycle).
    from vco_lib.parked_hooks import same_hook_command

    hooks = doc.data.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or normalize_matcher(group) != matcher:
            continue
        for item in group.get("hooks") or []:
            if not isinstance(item, dict):
                continue
            other = item.get("command")
            if (
                isinstance(other, str)
                and other
                and same_hook_command(other, command)
                and (if_rule is _ANY_IF or _if_matches(item, if_rule))
            ):
                return True
    return False


def _parked_restore_records(
    parked: Dict[str, Any],
) -> List[Tuple[Any, Dict[str, Any]]]:
    """The ``(hook_index, item)`` records a parked entry restores.

    v0.2.101: a GROUP park (an ``if``-rule group disabled as one unit)
    carries ``items`` — one ``{"hook_index", "item"}`` record per rule —
    and deliberately no ``item``. A row parked by an older build carries
    ``item`` only. Both shapes come back as a record list, so
    :func:`insert_hook` has ONE restore path.
    """
    raw_items = parked.get("items")
    if isinstance(raw_items, list) and raw_items:
        records: List[Tuple[Any, Dict[str, Any]]] = []
        for rec in raw_items:
            if not isinstance(rec, dict) or not isinstance(rec.get("item"), dict):
                raise HooksSettingsError(
                    "parked_entry_invalid",
                    "parked group entry has a record without an object `item`.",
                )
            h = rec.get("hook_index")
            records.append(
                (h if isinstance(h, int) and not isinstance(h, bool) else None, rec["item"])
            )
        return records
    item = parked.get("item")
    if not isinstance(item, dict):
        raise HooksSettingsError(
            "parked_entry_invalid",
            "parked entry is missing a string `event` or an object `item`.",
        )
    h = parked.get("hook_index")
    return [(h, item)]


def _parked_command_of(records: List[Tuple[Any, Dict[str, Any]]]) -> Optional[str]:
    """The command a parked entry's records restore — the FIRST record's,
    when it is a string. A group park's records all share one command
    (that is what makes them a group); a non-string command means "not
    usable for group matching", returned as ``None``."""
    for _h_idx, item in records:
        command = item.get("command")
        if isinstance(command, str) and command:
            return command
    return None


def insert_hook(
    doc: SettingsDoc,
    parked: Dict[str, Any],
    *,
    anchor_scripts: Optional[Iterable[str]] = None,
) -> bool:
    """Restore a parked entry. Returns ``True`` when the document changed.

    ``anchor_scripts`` (v0.2.97): the ``.claude/hooks/`` script basenames VCO
    ships (:func:`shipped_hook_scripts`). A parked entry for one of THOSE that
    still invokes it by the relative path every release before v0.2.97 wrote —
    or by any anchored spelling an earlier v0.2.97 build wrote — is restored
    in the CURRENT per-OS anchored form (:func:`anchor_hook_command`), the
    form the bundle update now writes, rather than as the relative command,
    which fails as soon as the session's cwd moves. Everything else in the
    parked item is restored verbatim, and a user's own hook is never rewritten.

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

    v0.2.101, ``if`` groups: a group park (``items``) restores every rule
    at its own recorded index, in ascending order, into the one target
    group — the mirror image of the all-at-once removal, so the file
    comes back byte-identically. Each rule's identity is
    (event, matcher, command, ``if``): the presence check for idempotency
    AND the restore both key on the ``if`` value, so restoring a group
    beside nine surviving rules of the same command adds only the missing
    one instead of duplicating all ten.

    Idempotent: if an entry with the same natural key
    (event, matcher, command) is already present anywhere in the event — or
    (v0.2.97) the same hook in another command form under the same matcher —
    nothing changes and ``False`` is returned — so a double-click, or a
    re-enable after the user restored the line by hand, cannot produce a
    duplicate invocation. That check runs before any structural edit, so
    the ``False`` return leaves the document untouched.

    Raises:
        HooksSettingsError: ``parked_entry_invalid`` when the stored blob
            is not a restorable parked entry; ``hook_retired`` when it names a
            registration :mod:`vco_lib.hook_retirements` declares dead.
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
    if not isinstance(event, str) or not event:
        raise HooksSettingsError(
            "parked_entry_invalid",
            "parked entry is missing a string `event` or an object `item`.",
        )
    records = _parked_restore_records(parked)
    raw_matcher = parked.get("matcher")
    matcher = raw_matcher if isinstance(raw_matcher, str) else ""

    # Per-rule preprocessing: anchor shipped scripts, then the retirement
    # check. Both operate on the COMMAND, so a group park runs them once
    # per rule — with copies, never mutating the caller's parked bytes.
    restores: List[Tuple[Any, Dict[str, Any], Any]] = []
    for h_idx, item in records:
        item = dict(item)
        command = item.get("command")
        if anchor_scripts is not None and isinstance(command, str) and command:
            anchored = anchor_hook_command(command, only=anchor_scripts)
            if anchored != command:
                item["command"] = anchored
                command = anchored
        restores.append((h_idx, item, command))

    # F7 (v0.2.95). A parked entry OUTLIVES the thing it restores.
    #
    # Disabling a hook from the launcher REMOVES its settings.json entry and
    # parks the removed bytes in `project_hooks.disabled_entry_json`. The
    # bundle scrub (`hook_retirements.scrub_retired_registrations`) then walks
    # settings.json on every update — and a parked entry is BY DEFINITION not
    # in settings.json, so the scrub cannot see it. A hook the user disabled
    # before its retirement therefore keeps a row that says "Disabled
    # (restorable)", and Enable would put a dead registration back: the script
    # it invokes was deleted by the same update that retired it.
    #
    # The refusal lives HERE, in the restore itself, because this is the one
    # place every restorer passes through — the launcher's Hooks tab, the
    # hub's `PATCH /hooks/{id}` routes, and the shipped `vct-cli hooks enable`
    # CLI. Putting it in any one caller would leave the other two able to
    # write the dead entry back.
    #
    # Refusal, not silent removal: the parked bytes are the user's, and a
    # click that produces nothing with no reason is the placebo this whole
    # subsystem was built to end. The message names the replacement.
    from vco_lib.hook_retirements import (
        match_retired_registration,
        vco_hook_script_identity,
    )

    for _h_idx, item, command in restores:
        if not (isinstance(command, str) and command):
            continue
        # Imported lazily: `hook_retirements` imports `invoked_script_tokens`
        # from THIS module, so a module-level import here would be a cycle.
        # v0.2.101 (review SF-3): the parked blob carries the registration's
        # `async` flag — hand it over, because the async-only retirement rows
        # (the dispatcher merge) refuse to match without positive evidence.
        # A user's own SYNC registration of one of those scripts restores.
        _parked_async = item.get("async")
        retired = match_retired_registration(
            event, command, hook_identity=vco_hook_script_identity(command),
            is_async=(
                _parked_async if isinstance(_parked_async, bool) else None
            ),
        )
        if retired is not None:
            raise HooksSettingsError(
                "hook_retired",
                f"`{command}` was retired in {retired.retired_in} and will not be "
                f"restored: {retired.reason}. It is replaced by "
                f"{retired.audit_replacement}. Nothing was written.",
            )

    # Idempotency FIRST, before any structural edit: the natural key of a
    # rule is (event, matcher, command, `if`), so a double-click — or a
    # re-enable after the user put the line back by hand in a sibling
    # group with the same matcher — must change nothing at all. Checking
    # event-wide (the same `_locate` convention `register_hook` uses)
    # rather than only inside the target group is what makes the `False`
    # return provably non-mutating and keeps the restore from adding a
    # duplicate invocation to a different group. Rules already present
    # are skipped INDIVIDUALLY, so a partial restore (the user re-added
    # one rule of a parked group by hand) adds only the missing ones.
    missing: List[Tuple[Any, Dict[str, Any], Any]] = []
    for h_idx, item, command in restores:
        if isinstance(command, str) and command:
            if_rule = item.get("if")
            if_rule = if_rule if isinstance(if_rule, str) else None
            if _locate(doc, event, matcher, command, if_rule) is not None:
                continue
            # v0.2.97: the same hook in ANOTHER command form (the
            # pre-v0.2.97 `[ -n "$VCT_DISABLE_HOOKS" ] || ` guard, an older
            # path separator) counts as present too. A parked legacy entry
            # whose hook an older bundle update re-added in the current form
            # is exactly this case, and restoring the parked bytes beside it
            # would run the hook twice — and the next update would supersede
            # BOTH copies to the same command. Same identity rule as the
            # bundle merge (`vco_lib.parked_hooks.same_hook_command`).
            if _hook_present_in_another_form(doc, event, matcher, command, if_rule):
                continue
        missing.append((h_idx, item, command))
    if not missing:
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
    elif isinstance(parked.get("items"), list) and parked.get("items"):
        # v0.2.101 exception, GROUP parks only: the disable removed the
        # group, but a same-matcher group out there ALREADY carries the
        # parked command — the user hand-restored one or more rules into
        # it. Reuse THAT group so the missing rules join their siblings
        # instead of founding a second same-matcher group beside it. In
        # the plain round trip no such group exists (the shipped
        # template's sibling Bash groups carry DIFFERENT commands), so
        # the faithful recreation below still runs and the file comes
        # back byte-identically.
        wanted = _parked_command_of(records)
        for group in groups:
            if not isinstance(group, dict) or normalize_matcher(group) != matcher:
                continue
            inner_scan = group.get("hooks")
            if not isinstance(inner_scan, list):
                continue
            if any(
                isinstance(it, dict) and it.get("command") == wanted
                for it in inner_scan
            ):
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

    for h_idx, item, _command in missing:
        at = (
            h_idx
            if isinstance(h_idx, int) and 0 <= h_idx <= len(inner)
            else len(inner)
        )
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
    from vco_lib.hook_retirements import hook_command_key  # noqa: PLC0415 — import cycle

    path = _settings_path(args)
    doc = load_settings(path)
    entries, skipped = list_hooks(doc)
    payload: Dict[str, Any] = {
        "ok": True,
        "settings_path": str(path),
        # `--with-items` keeps each inner hook object: the launcher's
        # `project_hooks` mirror stores it as the row's config when it
        # reads a JSONC settings.json through this verb (v0.2.97).
        "hooks": [
            e if args.with_items else {k: v for k, v in e.items() if k != "item"}
            for e in entries
        ],
        "skipped": skipped,
    }
    if args.keys_for_json is not None:
        # v0.2.97: the launcher's Hooks tab matches its DB rows (mirror +
        # parked, which may hold an older spelling of a command) to these
        # entries by `hook_command_key` — computed HERE, the one home of the
        # rule, instead of in a Rust copy of it.
        try:
            extra = json.loads(args.keys_for_json)
        except json.JSONDecodeError as exc:
            raise HooksSettingsError(
                "invalid_argument", f"--keys-for-json is not valid JSON: {exc.msg}"
            ) from None
        if not isinstance(extra, list) or not all(isinstance(c, str) for c in extra):
            raise HooksSettingsError(
                "invalid_argument", "--keys-for-json must be a JSON array of strings."
            )
        commands = [e["command"] for e in entries if isinstance(e.get("command"), str)]
        payload["keys"] = {c: hook_command_key(c) for c in [*commands, *extra]}
    _emit(payload)
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
    # Anchor a VCO-shipped hook restored from a pre-v0.2.97 park; the set
    # comes from the orchestrator clone's templates (None → anchor nothing).
    changed = insert_hook(doc, parked, anchor_scripts=shipped_hook_scripts())
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
    p_list.add_argument(
        "--with-items", action="store_true",
        help="Include each entry's whole inner hook object as `item`.",
    )
    p_list.add_argument(
        "--keys-for-json", default=None,
        help="A JSON array of extra commands; the output then carries `keys`, "
        "mapping every listed and extra command to its registration key "
        "(two spellings of one hook share a key).",
    )
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
