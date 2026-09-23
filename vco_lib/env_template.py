# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""DB-as-source-of-truth contract for the per-project ``.env`` template.

This module is **Phase 0.D** of the diagrams-integration plan (2026-05-24,
see ``.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md``).
It is the parallel of :mod:`vco_lib.config_projection` for the FOURTH
per-project env surface that Phase 0.B explicitly carved out as out-of-
scope:

  * ``<project_root>/.env`` — the bare shell-source file CLI users add
    to their bash/zsh rc (``source /path/to/project/.env``). NOT the
    same as ``.claude/env`` (which the launcher writes between bracket
    markers AND which Claude Code's bash shim auto-sources via the
    ``tools/claude`` wrapper).

Why ``.env`` warrants its own module (and not a fourth surface inside
:mod:`vco_lib.config_projection`)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* **Different audience.** The three Phase 0.B surfaces are consumed by
  the launcher, Claude Code's MCP subprocesses, and the VS Code extension
  — all machine readers. ``.env`` is a HUMAN-EDITED file: CLI users add
  custom exports, comment out keys they don't want, override values for
  troubleshooting. The contract must not stomp those edits.
* **Different rules.** The three Phase 0.B surfaces use either deep-merge
  (JSON) or bracket-marker block-replace (``.claude/env``). ``.env`` uses
  a stricter bracket-marker model where the managed block is
  **wholesale-replaced** on every call (so re-runs are byte-identical),
  and everything outside the markers is preserved verbatim. A key the
  USER sets anywhere outside the markers (``KEY=value`` or ``export
  KEY=value``) is left out of the managed block entirely (v0.2.97), so
  the user's value is the only active assignment of that key — it wins
  wherever it sits in the file, and no key is ever assigned twice. A
  commented line (``# KEY=``) sets nothing, so it does not suppress the
  managed value.
* **Different key set.** The three Phase 0.B surfaces carry the full
  canonical key set (~20 keys including launcher-internal access lists
  like ``VCT_KG_ACCESS_LIST``). ``.env`` carries a STRICT SUBSET — only
  keys that make sense for a CLI user to source from a shell rc. Access
  lists are runtime concerns that change per session; baking them into
  a shell-rc'd file is misleading and creates a stale-data trap.

Canonical key subset (the closed set for ``.env``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:func:`list_canonical_env_template_keys` returns the strict subset of
:func:`vco_lib.config_projection.list_canonical_keys` that this module
projects to ``.env``. The choice tier (INCLUDE / EXCLUDE) is documented
per-key:

  INCLUDE (project identity + service URLs + feature flags):

  * ``PROJECT_NAME``           — display name; shell tools branch on it.
  * ``CODE_GRAPH_PROJECT``     — sanitized name; codegraph CLI uses it.
  * ``KG_COLLECTION``          — per-project KG Weaviate class.
  * ``DEVELOPMENT_COLLECTION`` — per-project docs Weaviate class.
  * ``SHARED_KG_COLLECTION``   — cross-project shared KG class.
  * ``SHARED_KG_WRITE_DISABLED`` / ``SHARED_KG_OPT_OUT``
                               — feature gates; users may want to flip
                                 them via shell rc for ad-hoc sessions.
  * ``SHARED_KG_READ_DISABLED`` — v0.2.46 symmetric read gate; same
                                 shell-rc-flip rationale as the write
                                 gate above.
  * ``ACTIVE_EMBEDDING``       — picks named-vector slot; shell tools
                                 (scripts that bypass the MCP) need it.
  * ``WEAVIATE_URL`` / ``WEAVIATE_PORT``
                               — service endpoint; needed for direct
                                 weaviate-client calls from shell.
  * ``OLLAMA_URL`` / ``OLLAMA_PORT``
                               — same, for the local LLM.
  * ``CODE_EMBED_URL`` / ``CODE_EMBED_PORT``
                               — same, for the code-embedding service.

  EXCLUDE (launcher-internal or per-session runtime):

  * ``VCT_KG_ACCESS_LIST`` / ``VCT_CODE_GRAPH_ACCESS_LIST``
                               — these change when the grant matrix is
                                 toggled in the launcher GUI. A shell
                                 sourcing them at session-start carries
                                 a stale snapshot until next ``source``.
                                 The three Phase 0.B surfaces are re-
                                 projected on every grant toggle; ``.env``
                                 isn't. Keep them out of ``.env`` to
                                 avoid the stale-data trap.
  * ``VCT_ORCHESTRATOR_ROOT`` / ``VCT_INFRASTRUCTURE_DIR``
                               — launcher-installation-local paths.
                                 The orchestrator clone moves when the
                                 user reinstalls; the launcher refreshes
                                 the three Phase 0.B surfaces but again
                                 not ``.env``. CLI users typically have
                                 their own VCT_ORCHESTRATOR_ROOT in
                                 their shell rc anyway (pointing at
                                 wherever they checked out vibecoded-
                                 orchestrator).
  * ``GITHUB_TOKEN``           — secret. Lives in the user's keychain
                                 (Rust resolver) or their shell rc with
                                 chmod 600. The launcher never writes
                                 secrets into ``.env`` (audited and
                                 enforced via the secret-leak hook).

  Total INCLUDE set: 15 keys. EXCLUDE set: 5 keys (4 launcher-internal +
  GITHUB_TOKEN). Re-derivation rule: any new canonical key added to
  :func:`vco_lib.config_projection.list_canonical_keys` defaults to
  EXCLUDE here unless explicitly INCLUDE'd. The single-writer lint
  (``tests/test_config_projection_single_writer.py``) catches the
  inverse case (a key written DIRECTLY to ``.env`` outside this module)
  but does NOT pin the INCLUDE set — that's a documentation-driven
  governance step.

Marker pattern (frozen byte string — DO NOT CHANGE once shipped)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    # >>> VCO-MANAGED ENV (do not edit between markers) >>>
    # added by vco — KEY=VALUE
    KEY=VALUE
    ...
    # <<< VCO-MANAGED ENV <<<

The two marker constants (:data:`ENV_TEMPLATE_BEGIN`,
:data:`ENV_TEMPLATE_END`) are byte-frozen for forward compatibility: a
user who has an older ``.env`` template written by an earlier vco must
get the in-place block replaced on the next ``apply_env_template`` run,
not appended-to. Renaming the markers would break that round-trip and
leave every existing user with two managed blocks stacked.

The format is intentionally DIFFERENT from the bracket markers used by
``.claude/env`` (``# vco-managed-begin`` / ``# vco-managed-end``).
``.claude/env`` markers are short because that file is machine-only;
``.env`` markers are loud / hard-to-miss because the file is human-
edited and we want users to SEE the boundary before they wreck it.

The ``# added by vco — KEY=VALUE`` comment-above-each-line is forensic
(tells the user where the value came from) — re-renderable from the
canonical key set; no semantic state lives in it.

Out of scope
~~~~~~~~~~~~

* Nothing. Since v0.2.97 this module is the ONLY writer of a project's
  ``.env``: the launcher's project create path runs
  ``python -m vco_lib.env_template apply`` (and ``reference`` for a
  Safe-add project, which writes the ``.env.vco.reference`` sidecar and
  never the live file), and ``install.py`` routes the orchestrator
  root's ``.env`` through :mod:`vco_lib.install_env`, which calls
  :func:`apply_env_template` with its install-time scaffold. The
  single-writer lint (``tests/test_config_projection_single_writer.py``)
  enforces it.

Legacy lines (migrated, v0.2.97)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Before v0.2.97 two append-only writers ran against the same file: the
launcher's Rust ``ensure_project_env_template`` / ``build_canonical_env_text``
and ``install.py``'s ``_ensure_env_template`` / ``_reconcile_env_keys``. Their
output is recognisable by the section headers they wrote (see
:func:`_migrate_legacy_sections`):

  * ``# added by vco YYYY-MM-DD: appended missing canonical keys``;
  * the Rust template's ``# === Service URLs …`` and
    ``# === Per-project Weaviate collections ===`` sections;
  * ``# --- Added by install.py --update on YYYY-MM-DD ---`` with its
    per-line ``# Added by install.py --update on …`` annotations.

Inside such a section, a line for a key the managed block now renders is
REMOVED (the block carries it, with the current value), as is an active
line still holding the unsubstituted ``<project>`` placeholder. Anything
else in the section — a commented ``# GITHUB_TOKEN=`` placeholder, a key
the block does not carry — stays, and so does every line outside those
sections. A section left with no key lines loses its header too. The
result is ONE set: each managed key is assigned once, either by the block
or by the user.

Cross-OS rules (non-negotiable)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* :class:`pathlib.Path` for all path construction.
* Atomic writes via :func:`tempfile.mkstemp` in the SAME directory as
  ``path``, then :func:`os.replace`. ``os.rename`` does NOT overwrite
  on Windows; ``os.replace`` does. Cross-filesystem rename fails with
  EXDEV on Linux — keep the tempfile next to the target.
* Line endings: ``.env`` is written with Unix-style **LF** even on
  Windows. CLI users on Windows typically source from bash via WSL2 or
  git-bash, both of which accept LF natively. CRLF would be rejected by
  POSIX shells.
* UTF-8 encoding; values are passed through verbatim. Double-quoted
  shell escaping is NOT applied because ``.env`` lines are
  ``KEY=value`` (no quotes around value) — the de-facto ``.env`` format
  consumed by python-decouple, python-dotenv, direnv, etc. If a value
  contains shell metacharacters the user must quote it themselves.

Public API
~~~~~~~~~~

::

    from pathlib import Path
    from vco_lib.env_template import (
        project_env_template_from_db,
        apply_env_template,
        list_canonical_env_template_keys,
    )

    keys = project_env_template_from_db("<project-uuid>")
    report = apply_env_template(keys, project_folder=Path("/path/to/project"))
    # report = {"env": ["KG_COLLECTION", "PROJECT_NAME", ...]}

CLI entry point
~~~~~~~~~~~~~~~

The launcher subprocesses into these (``services/vco_lib_bridge.rs``)::

    python -m vco_lib.env_template apply --project-id <uuid> --project-folder <path>
    python -m vco_lib.env_template reference --project-id <uuid> --project-folder <path>
    python -m vco_lib.env_template list-keys --json
    python -m vco_lib.env_template from-db --project-id <uuid> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

from vco_lib.atomic import atomic_write_text
from vco_lib.config_projection import (
    ConfigProjectionError,
    DbUnreachable,
    ProjectNotFound,
    list_canonical_keys,
    project_env_from_db,
)


# ─── Canonical key subset registry ───────────────────────────────────────
#
# The closed set of canonical env keys this module projects to ``.env``.
# Strict subset of ``vco_lib.config_projection.list_canonical_keys()``;
# see the module docstring for INCLUDE / EXCLUDE rationale per key.
#
# Order matters: it controls the order lines appear inside the managed
# block. Grouping by topic (identity → KG → services → flags) makes the
# block easier to scan for humans.

_CANONICAL_ENV_TEMPLATE_KEYS: tuple[str, ...] = (
    # Project identity
    "PROJECT_NAME",
    "CODE_GRAPH_PROJECT",
    # Knowledge graph collections
    "KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "SHARED_KG_COLLECTION",
    # Feature flags
    "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT",
    # v0.2.46 Decision B — symmetric read gate. No legacy alias because
    # pre-v0.2.46 the read path was unconditional. Must be a member of
    # ``config_projection._CANONICAL_KEYS`` for the subset invariant
    # check (`_assert_subset_invariant`) below to pass.
    "SHARED_KG_READ_DISABLED",
    # Embedding profile
    "ACTIVE_EMBEDDING",
    # Service endpoints
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
)


def list_canonical_env_template_keys() -> set[str]:
    """Return the closed set of canonical env keys VCO writes to ``.env``.

    Strict subset of :func:`vco_lib.config_projection.list_canonical_keys`.
    The CI lint at ``tests/test_config_projection_single_writer.py``
    consumes this list to detect direct writes to ``.env`` elsewhere in
    the codebase.

    Returns a fresh ``set`` each call so callers can mutate the result
    without affecting other callers.
    """
    return set(_CANONICAL_ENV_TEMPLATE_KEYS)


# ─── Bracket markers for the ``.env`` surface ────────────────────────────
#
# RESERVED — must not change once shipped. Existing ``.env`` files on
# disk match this exact substring to locate the managed block; changing
# the bytes would break in-place replacement and leave users with two
# stacked managed blocks on the next run.
#
# These are intentionally LOUDER than ``.claude/env``'s markers because
# ``.env`` is human-edited and we want the boundary to be hard to miss.

ENV_TEMPLATE_BEGIN: str = "# >>> VCO-MANAGED ENV (do not edit between markers) >>>"
ENV_TEMPLATE_END: str = "# <<< VCO-MANAGED ENV <<<"


# ─── Sanity check: subset of config_projection canonical keys ───────────


def _assert_subset_invariant() -> None:
    """Assert :data:`_CANONICAL_ENV_TEMPLATE_KEYS` is a subset of the
    Phase 0.B canonical key set. Imported-time check; trips loudly if
    someone adds a key here without first adding it to
    ``config_projection._CANONICAL_KEYS``.
    """
    full = list_canonical_keys()
    extras = set(_CANONICAL_ENV_TEMPLATE_KEYS) - full
    if extras:
        raise RuntimeError(
            "env_template canonical keys are NOT a subset of "
            "config_projection canonical keys; offending keys: "
            f"{sorted(extras)}. Add them to "
            "vco_lib.config_projection._CANONICAL_KEYS first."
        )


_assert_subset_invariant()


# ─── project_env_template_from_db ───────────────────────────────────────


def project_env_template_from_db(
    project_id: str,
    *,
    db_path: Path | None = None,
    weaviate_url_override: str | None = None,
    ollama_url_override: str | None = None,
    active_embedding_override: str | None = None,
    shared_kg_default: str | None = None,
    weaviate_port_default: int = 8081,
    ollama_port_default: int = 11435,
    code_embed_port_default: int = 11440,
    orchestrator_root: Path | None = None,
) -> dict[str, str]:
    """Return the canonical key→value map for the project's ``.env`` template.

    Source: the SAME launcher DB read as :func:`project_env_from_db`,
    filtered to the subset of canonical keys defined by
    :func:`list_canonical_env_template_keys`. Re-uses ``project_env_from_db``
    rather than duplicating the resolver logic — there is exactly one
    source of truth for canonical key VALUES (Phase 0.B's contract);
    ``.env`` is a different projection of the same data.

    Args:
        project_id: The project's UUID (the ``projects.id`` column).
        db_path: Optional override of the launcher DB location. Defaults
            to ``vct_root_dir() / "launcher.db"`` via
            ``project_env_from_db``.
        weaviate_url_override / ollama_url_override / active_embedding_override:
            Same shape and semantics as ``project_env_from_db``.
        shared_kg_default / weaviate_port_default / ollama_port_default /
        code_embed_port_default: Same shape and semantics as
            ``project_env_from_db``. **v0.2.40 W40-C**: ``shared_kg_default``
            now defaults to ``None``, which triggers a DB-read from
            ``project_kg_bindings(slug='orchestrator-root',
            role='primary')`` for the fallback name. Soft-fall back to
            the bundled const if launcher.db is unreachable / row
            absent. Explicit string overrides still bypass the DB-read.
        orchestrator_root: Forwarded; even though ``VCT_ORCHESTRATOR_ROOT``
            / ``VCT_INFRASTRUCTURE_DIR`` are NOT in the ``.env`` template
            subset, accepting this arg keeps the CLI surface symmetric
            with the Phase 0.B CLI and lets future callers extend the
            subset without changing the resolver's call site.

    Returns:
        A flat key→value dict containing ONLY keys from
        :func:`list_canonical_env_template_keys` whose value was resolved.
        Keys that the Phase 0.B resolver omitted (empty / unset) are
        absent here too — the writer treats absent keys as "do not
        render". This matches the Phase 0.B semantics: the launcher's
        decision to omit a key propagates through every surface.

    Raises:
        :class:`vco_lib.config_projection.DbUnreachable`: launcher DB
            missing / unopenable.
        :class:`vco_lib.config_projection.ProjectNotFound`: no row in
            ``projects`` matches.
    """
    bundle = project_env_from_db(
        project_id,
        db_path=db_path,
        weaviate_url_override=weaviate_url_override,
        ollama_url_override=ollama_url_override,
        active_embedding_override=active_embedding_override,
        shared_kg_default=shared_kg_default,
        weaviate_port_default=weaviate_port_default,
        ollama_port_default=ollama_port_default,
        code_embed_port_default=code_embed_port_default,
        orchestrator_root=orchestrator_root,
    )
    full_env: dict[str, str] = bundle["canonical_env"]
    keep = list_canonical_env_template_keys()
    # Preserve order from _CANONICAL_ENV_TEMPLATE_KEYS for deterministic
    # rendering (Python dicts are insertion-ordered).
    out: dict[str, str] = {}
    for key in _CANONICAL_ENV_TEMPLATE_KEYS:
        if key in keep and key in full_env:
            out[key] = full_env[key]
    return out


# ─── apply_env_template ─────────────────────────────────────────────────


def apply_env_template(
    keys: Mapping[str, str],
    *,
    project_folder: Path,
    scaffold: Optional[str] = None,
    keep_existing_values: bool = False,
) -> dict[str, list[str]]:
    """Project ``keys`` into ``<project_folder>/.env``'s managed block.

    Idempotent block-replace:

      * ``.env`` missing: create it as ``scaffold`` (when given — the
        user-owned header and commented placeholders a new file starts
        with) followed by the managed block.
      * ``.env`` present: first migrate the legacy VCO-authored lines
        (:func:`_migrate_legacy_sections`); then leave out of the block
        every key the user sets outside the markers
        (:func:`_user_set_keys`); then replace the block between the
        markers wholesale, or append it at EOF when the file has none.
        ``scaffold`` is ignored — content outside the markers belongs to
        the user once the file exists.
      * No block is APPENDED when every key is user-set (it would be
        empty), and nothing is written when the result equals the file.

    Re-running with the same ``keys`` produces byte-identical output
    (and, since v0.2.97, no write at all) — which is what makes it safe
    to invoke unconditionally.

    Atomic write: tempfile in the same directory as ``.env``, then
    :func:`os.replace`. A ``.env`` that exists but cannot be read is an
    error, never treated as absent (that would replace the user's file).

    Args:
        keys: The canonical key→value map to render. Typically from
            :func:`project_env_template_from_db`. Keys outside
            :func:`list_canonical_env_template_keys` are STILL rendered —
            the subset decision lives in the RESOLVER, not the writer.
        project_folder: The project's root directory. ``.env`` lives
            directly inside it (NOT under ``.claude/``).
        scaffold: Text a NEW ``.env`` starts with (see
            :func:`render_project_env_scaffold`); ``None`` = the block only.
        keep_existing_values: Fill-only mode for callers whose resolution
            is not authoritative over an existing file (``install.py``'s
            update / re-install): a key the existing block already renders
            keeps ITS value (and a key only the old block carries stays),
            and a key migrated from a legacy line keeps that line's value,
            so the call can add keys but never change or drop one. A value
            still holding ``<project>`` is not kept.

    Returns:
        ``{"env": [...], "added": [...], "user_set": [...],
        "migrated": [...], "action": ["created"|"updated"|"unchanged"]}`` —
        ``env`` the keys the block now renders, ``added`` those among them
        no line assigned before, ``user_set`` the keys of ``keys`` the user
        sets outside the block, ``migrated`` the keys whose legacy lines
        were removed. Lists sorted; ``action`` is a one-element list so the
        dict keeps one value type.

    Raises:
        :class:`OSError`: the file could not be read or written. The
            target ``.env`` is left untouched (atomic rename guarantee).
        :class:`UnicodeDecodeError`: the existing file is not UTF-8.
    """
    env_path = project_folder / ".env"
    project_folder.mkdir(parents=True, exist_ok=True)

    prior: Optional[str] = (
        env_path.read_text(encoding="utf-8") if env_path.exists() else None
    )

    if prior is None:
        rendered = dict(keys)
        new_text = _merge_managed_block(scaffold, _build_managed_block(rendered))
        _atomic_write_text(env_path, new_text)
        return _report(rendered, set(), set(), set(), "created")

    before, block, after = _split_managed(prior)
    previously_set = _user_set_keys(before + after) | _user_set_keys(block or "")
    before, migrated_before = _migrate_legacy_sections(before, set(keys))
    after, migrated_after = _migrate_legacy_sections(after, set(keys))
    migrated = {**migrated_before, **migrated_after}
    user_set = _user_set_keys(before + after) & set(keys)
    wanted = dict(keys)
    if keep_existing_values:
        # Precedence: the old block's value > a migrated legacy line's value
        # > the caller's. Nothing a previous run settled changes.
        kept = {k: v for k, v in migrated.items() if v is not None}
        if block is not None:
            kept.update(
                (k, v) for k, v in _block_values(block).items()
                if _PLACEHOLDER_TOKEN not in v
            )
        wanted = {**{k: kept.get(k, v) for k, v in keys.items()}, **kept}
        user_set = _user_set_keys(before + after) & set(wanted)
    rendered = {k: v for k, v in wanted.items() if k not in user_set}

    if block is None and not rendered and keys:
        new_text = before
    elif block is None:
        new_text = _merge_managed_block(before, _build_managed_block(rendered))
    else:
        new_text = before + _build_managed_block(rendered) + after

    action = "unchanged" if new_text == prior else "updated"
    if action == "updated":
        _atomic_write_text(env_path, new_text)
    return _report(rendered, user_set, set(migrated), previously_set, action)


def _report(
    rendered: Mapping[str, str],
    user_set: set[str],
    migrated: set[str],
    previously_set: set[str],
    action: str,
) -> dict[str, list[str]]:
    return {
        "env": sorted(rendered),
        "added": sorted(set(rendered) - previously_set),
        "user_set": sorted(user_set),
        "migrated": sorted(migrated),
        "action": [action],
    }


def _split_managed(text: str) -> tuple[str, Optional[str], str]:
    """``(before, block, after)`` around the managed block. ``block`` is
    ``None`` when the file has no BEGIN marker (then ``before`` is the
    whole text). A block missing its END runs to EOF (crash recovery); the
    single newline after END belongs to the block."""
    begin = text.find(ENV_TEMPLATE_BEGIN)
    if begin == -1:
        return text, None, ""
    end_off = text[begin:].find(ENV_TEMPLATE_END)
    if end_off == -1:
        return text[:begin], text[begin:], ""
    stop = begin + end_off + len(ENV_TEMPLATE_END)
    if stop < len(text) and text[stop] == "\n":
        stop += 1
    return text[:begin], text[begin:stop], text[stop:]


# ─── Keys the user sets outside the block ────────────────────────────────

_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


def _block_values(block: str) -> dict[str, str]:
    """``KEY -> value`` of the managed block's assignment lines, in order."""
    values: dict[str, str] = {}
    for raw in block.splitlines():
        match = _ASSIGNMENT.match(raw.strip())
        if match:
            values[match.group(1)] = raw.strip()[match.end():]
    return values


def _user_set_keys(text: str) -> set[str]:
    """Keys ASSIGNED by an active line (``KEY=…`` / ``export KEY=…``).
    Comment lines assign nothing, so they are not counted."""
    found: set[str] = set()
    for raw in text.splitlines():
        match = _ASSIGNMENT.match(raw.strip())
        if match:
            found.add(match.group(1))
    return found


def effective_assignment(text: str, key: str) -> tuple[Optional[str], bool]:
    """``(value, in_block)`` of the assignment of ``key`` that WINS when the
    file is sourced — the LAST active one — and whether it sits inside the
    VCO-managed block. ``(None, False)`` when no active line assigns it."""
    before, block, after = _split_managed(text)
    value: Optional[str] = None
    in_block = False
    for part, is_block in ((before, False), (block or "", True), (after, False)):
        for raw in part.splitlines():
            match = _ASSIGNMENT.match(raw.strip())
            if match and match.group(1) == key:
                value, in_block = raw.strip()[match.end():], is_block
    return value, in_block


def remove_line_under(
    project_folder: Path,
    header: str,
    key: str,
    *,
    remove_if: Callable[[str], bool],
) -> Optional[bool]:
    """Remove the ``key=`` line sitting DIRECTLY under the VCO-authored
    comment line ``header`` (the only provenance a pre-v0.2.97 line has)
    when ``remove_if(value)`` says so. ``None``: no such line (or no file);
    ``True``: removed (atomic write, every other byte kept); ``False``:
    left. The value goes only to ``remove_if`` — never into a return value,
    a log, or an exception message. Only the first such pair is handled."""
    env_path = project_folder / ".env"
    if not env_path.is_file():
        return None
    with env_path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    prefix = f"{key}="
    for i in range(len(lines) - 1):
        if lines[i].rstrip("\r\n") == header and lines[i + 1].startswith(prefix):
            value = lines[i + 1].rstrip("\r\n")[len(prefix):]
            if not remove_if(value):
                return False
            _atomic_write_text(env_path, "".join(lines[:i + 1] + lines[i + 2:]))
            return True
    return None


# ─── Legacy VCO-authored lines (pre-v0.2.97 writers) ────────────────────
#
# The headers the retired append-only writers put above their lines. Byte
# strings of shipped files — see the module docstring, "Legacy lines".

_LEGACY_APPEND_HEADER = re.compile(
    r"^# added by vco \d{4}-\d{2}-\d{2}: appended missing canonical keys$"
)
_LEGACY_TEMPLATE_HEADERS = (
    "# === Service URLs ",  # prefix: its parenthetical changed across releases
    "# === Per-project Weaviate collections ===",
)
_LEGACY_TEMPLATE_NOTES = frozenset({
    "# Resolved by the launcher when the project is registered. Don't",
    "# edit unless you know what you're doing.",
})
_LEGACY_UPDATE_HEADER = re.compile(
    r"^# --- Added by install\.py --update on \d{4}-\d{2}-\d{2} ---$"
)
_LEGACY_UPDATE_NOTE = re.compile(
    r"^# Added by install\.py --update on \d{4}-\d{2}-\d{2}$"
)
# The order both legacy writers emitted keys in. A body is the run of key
# lines in NON-DECREASING order of this list: a line out of order (or a
# key not in it) is not theirs — e.g. the user's ``echo KG_COLLECTION=…
# >> .env`` right under an append block — and ends the body.
_LEGACY_KEY_ORDER: tuple[str, ...] = (
    "WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "OLLAMA_PORT",
    "CODE_EMBED_URL", "CODE_EMBED_PORT",
    "KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION",
    "PROJECT_NAME", "CONVERSATION_COLLECTION", "ACTIVE_EMBEDDING",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
    "RL_SERVER_URL", "RL_SERVER_PORT", "RL_PROJECT_ROOT", "VCT_TELEMETRY",
)
_KEY_LINE = re.compile(r"^(#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_PLACEHOLDER_TOKEN = "<project>"


def _legacy_header_kind(line: str) -> Optional[str]:
    if _LEGACY_APPEND_HEADER.match(line):
        return "append"
    if line.startswith(_LEGACY_TEMPLATE_HEADERS):
        return "template"
    if _LEGACY_UPDATE_HEADER.match(line):
        return "update"
    return None


def _legacy_body_end(lines: list[str], start: int, kind: str) -> int:
    """Index one past the last line of the legacy body starting at ``start``."""
    i, rank = start, -1
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if kind == "update":
            nxt = lines[i + 1].rstrip("\r") if i + 1 < len(lines) else ""
            key = _KEY_LINE.match(nxt)
            if _LEGACY_UPDATE_NOTE.match(line) and key and not key.group(1):
                i += 2
                continue
            return i
        if kind == "template" and line in _LEGACY_TEMPLATE_NOTES:
            i += 1
            continue
        key = _KEY_LINE.match(line)
        if not key or key.group(2) not in _LEGACY_KEY_ORDER:
            return i
        key_rank = _LEGACY_KEY_ORDER.index(key.group(2))
        if key_rank < rank:
            return i
        rank = key_rank
        i += 1
    return i


def _legacy_line_is_retired(
    line: str, owned: set[str]
) -> Optional[tuple[str, Optional[str]]]:
    """``(key, value)`` of a legacy key line to REMOVE, else ``None``: its
    key is rendered by the block, or it is active and still holds
    ``<project>``. ``value`` is the line's value when it is an active,
    real one (a fill-only caller carries it into the block), else ``None``."""
    key = _KEY_LINE.match(line.rstrip("\r"))
    if not key:
        return None
    active = not key.group(1)
    placeholder = _PLACEHOLDER_TOKEN in key.group(3)
    if key.group(2) in owned or (active and placeholder):
        return key.group(2), key.group(3) if active and not placeholder else None
    return None


def _migrate_legacy_sections(
    text: str, owned: set[str]
) -> tuple[str, dict[str, Optional[str]]]:
    """Remove the retired writers' lines for keys the block now carries.

    Returns ``(new_text, removed)`` — ``removed`` maps each removed key to
    its last active value (``None`` for a commented or placeholder line).
    Lines outside a recognised legacy section are never touched. See the
    module docstring, "Legacy lines".
    """
    removed: dict[str, Optional[str]] = {}
    if not text:
        return text, removed
    lines = text.split("\n")
    out: list[str] = []

    def retire(found: tuple[str, Optional[str]]) -> None:
        key, value = found
        if value is not None or key not in removed:
            removed[key] = value
    i = 0
    while i < len(lines):
        kind = _legacy_header_kind(lines[i].rstrip("\r"))
        if kind is None:
            out.append(lines[i])
            i += 1
            continue
        end = _legacy_body_end(lines, i + 1, kind)
        kept: list[str] = []
        j = i + 1
        while j < end:
            if kind == "update":
                retired = _legacy_line_is_retired(lines[j + 1], owned)
                if retired:
                    retire(retired)
                else:
                    kept.extend((lines[j], lines[j + 1]))
                j += 2
                continue
            retired = _legacy_line_is_retired(lines[j], owned)
            if retired:
                retire(retired)
            else:
                kept.append(lines[j])
            j += 1
        if any(_KEY_LINE.match(k.rstrip("\r")) for k in kept):
            out.append(lines[i])
            out.extend(kept)
        elif out and not out[-1].strip():
            # The whole section is gone: drop the blank line its writer
            # put in front of it too, so migration leaves no gap behind.
            out.pop()
        i = end
    return "\n".join(out), removed


# ─── Managed block renderer ─────────────────────────────────────────────


def _build_managed_block(keys: Mapping[str, str]) -> str:
    """Render the managed block as a single string ending in newline.

    Format::

        # >>> VCO-MANAGED ENV (do not edit between markers) >>>
        # added by vco — KEY=VALUE
        KEY=VALUE
        # added by vco — OTHER=OTHERVALUE
        OTHER=OTHERVALUE
        ...
        # <<< VCO-MANAGED ENV <<<

    The per-key ``# added by vco — KEY=VALUE`` comment-line above each
    KEY=VALUE pair is forensic only — it tells the user where the value
    came from when they audit the file. Same value as the line below.
    Re-running with the same keys produces the same text byte-for-byte.

    Empty ``keys`` map produces a managed block with just the markers
    (no content lines). That's intentional: the marker pair is the
    semantic boundary; emptiness is information ("the launcher knows
    about this project but has no canonical values to project right
    now"), not a bug.

    Iteration order: whatever ``keys`` was given. Callers that want
    deterministic ordering should pass an insertion-ordered dict (Python
    3.7+ guarantees this for ``dict``). :func:`project_env_template_from_db`
    builds the dict in :data:`_CANONICAL_ENV_TEMPLATE_KEYS` order.
    """
    lines: list[str] = [ENV_TEMPLATE_BEGIN]
    for key, value in keys.items():
        lines.append(f"# added by vco — {key}={value}")
        lines.append(f"{key}={value}")
    lines.append(ENV_TEMPLATE_END)
    # Trailing newline after the END marker — matches the Rust writer
    # discipline and avoids surprises when a user appends content with
    # `>>` (which would otherwise glue onto the END line).
    return "\n".join(lines) + "\n"


def _merge_managed_block(prior: Optional[str], managed: str) -> str:
    """Splice ``managed`` into ``prior`` between the bracket markers.

    Behaviour matches the Phase 0.B ``_merge_managed_block`` in
    :mod:`vco_lib.config_projection` but with the
    :data:`ENV_TEMPLATE_BEGIN` / :data:`ENV_TEMPLATE_END` markers:

      * ``prior is None``: return ``managed`` as-is.
      * ``prior`` lacks BEGIN: append ``managed`` at EOF (ensuring a
        newline-separator if ``prior`` doesn't end with one).
      * ``prior`` has BEGIN: locate BEGIN, locate END after it. Replace
        the segment from BEGIN to (END + len(END) + 1 newline) with the
        new managed block. Lines outside the markers preserved byte-
        for-byte.
      * Edge case: BEGIN present but END missing (truncated managed
        block from a crash) → replace BEGIN-to-EOF with the new block.
    """
    if prior is None:
        return managed

    begin_idx = prior.find(ENV_TEMPLATE_BEGIN)
    if begin_idx == -1:
        # Append managed at EOF with newline separator.
        if prior and not prior.endswith("\n"):
            return prior + "\n" + managed
        return prior + managed

    # Find END after BEGIN.
    end_off = prior[begin_idx:].find(ENV_TEMPLATE_END)
    if end_off == -1:
        # Truncated managed block — replace BEGIN→EOF.
        after_end = len(prior)
    else:
        after_end = begin_idx + end_off + len(ENV_TEMPLATE_END)
        # Trim one trailing newline after END if present (avoids
        # accumulating blank lines on repeated calls).
        if after_end < len(prior) and prior[after_end] == "\n":
            after_end += 1

    return prior[:begin_idx] + managed + prior[after_end:]


# ─── Atomic write helper ────────────────────────────────────────────────


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically with LF line endings.

    Thin delegate to :func:`vco_lib.atomic.atomic_write_text` (v0.2.54
    Track J closed the consolidation that :mod:`vco_lib.atomic`'s
    module docstring queued when it landed in v0.2.53 — this module,
    ``config_projection``, ``deferral_report`` and
    ``cli/codegraph_diagram`` each carried their own copy of the
    mkstemp + fsync + ``os.replace`` recipe).

    The name is kept because install.py's ``.claude.json`` write site
    and ``tests/test_atomic_write_cleanup.py`` import it from here.

    LF preservation: the shared helper opens the tempfile with
    ``newline=""`` (no translation), which is write-equivalent to the
    previous ``newline="\\n"`` — ``\\n`` in ``content`` lands as LF on
    every OS, never CRLF. The ``.env`` consumers (bash via WSL2 /
    git-bash) keep getting the LF bytes they require.
    """
    atomic_write_text(path, content)


# ─── New-file scaffold + the Safe-add reference sidecar ──────────────────

# The header + commented placeholders a NEW project ``.env`` starts with
# (user-owned lines outside the managed block: the launcher never fills
# them — they are secrets or paid-module settings). Written once, at
# creation; after that the file's non-block content is the user's.
_PROJECT_ENV_SCAFFOLD = """\
# vibecoded-orchestrator per-project .env
# VCO keeps the block between the VCO-MANAGED markers below up to date.
# To override one of its keys, set it anywhere OUTSIDE the block — VCO
# then stops writing that key. A commented line sets nothing.

# === LLM API keys (optional) ===
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=

# === GitHub access for code-search MCP (optional) ===
# GITHUB_TOKEN=

# === RL retrieval module (Pro tier — uncomment when installed) ===
# RL_SERVER_URL=http://localhost:8090
# RL_SERVER_PORT=8090
# RL_PROJECT_ROOT={project_root}

# === Telemetry (off by default; on=opt-in only) ===
# VCT_TELEMETRY=off

"""

# Banner of the Safe-add sidecar: advisory, never the live file.
_REFERENCE_BANNER = """\
# vibecoded-orchestrator Safe-add REFERENCE — NOT the live .env.
# Safe add was ON, so VCO did NOT modify your project-root .env
# (it may be committed to your VCS). These are the keys VCO would
# have added. Diff against your .env and copy what you want:
#   diff .env .env.vco.reference
# See .claude/context/UPDATE_DEFERRED.md (safe_add_skipped_env_merge).

"""


def render_project_env_scaffold(project_folder: Path) -> str:
    """The text a new project ``.env`` starts with (see
    :data:`_PROJECT_ENV_SCAFFOLD`); ``RL_PROJECT_ROOT``'s placeholder
    carries the real folder."""
    return _PROJECT_ENV_SCAFFOLD.format(project_root=str(project_folder))


def write_env_reference(keys: Mapping[str, str], *, project_folder: Path) -> Path:
    """Write the Safe-add sidecar ``<project_folder>/.env.vco.reference``:
    the ``.env`` a new project would get (scaffold + managed block) under
    an advisory banner. NEVER reads or writes the live ``.env`` — that
    file may be committed, which is why Safe add exists. Always rewritten
    (the sidecar is advisory). Returns the sidecar path."""
    from vco_lib.git_exclude import SAFE_ADD_SIDECAR_SUFFIX

    project_folder.mkdir(parents=True, exist_ok=True)
    sidecar = project_folder / (".env" + SAFE_ADD_SIDECAR_SUFFIX)
    body = _merge_managed_block(
        render_project_env_scaffold(project_folder), _build_managed_block(keys)
    )
    _atomic_write_text(sidecar, _REFERENCE_BANNER + body)
    return sidecar


# ─── CLI entry points ───────────────────────────────────────────────────


def _cli_error(code: str, message: str) -> None:
    """One JSON error object on stderr (the documented contract) AND on
    stdout, so the launcher's bridge — which reads one JSON object from
    stdout — shows the real reason instead of "unreadable output"."""
    print(json.dumps({"ok": False, "error": code, "message": message}))
    print(json.dumps({"error": code, "message": message}), file=sys.stderr)


def _cli_resolve(args: argparse.Namespace) -> tuple[Optional[dict[str, str]], int]:
    """``(keys, 0)`` for ``--project-id``, or ``(None, exit_code)`` after
    printing the error (2 = project not found, 3 = DB unreachable)."""
    try:
        keys = project_env_template_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
            orchestrator_root=(
                Path(args.orchestrator_root) if args.orchestrator_root else None
            ),
            weaviate_port_default=args.weaviate_port,
            ollama_port_default=args.ollama_port,
            code_embed_port_default=args.code_embed_port,
        )
    except ProjectNotFound as exc:
        _cli_error("project_not_found", str(exc))
        return None, 2
    except DbUnreachable as exc:
        _cli_error("db_unreachable", str(exc))
        return None, 3
    return keys, 0


def _cli_apply(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template apply --project-id <id> --project-folder <path>``."""
    keys, failed = _cli_resolve(args)
    if keys is None:
        return failed
    folder = Path(args.project_folder)
    try:
        report = apply_env_template(
            keys, project_folder=folder, scaffold=render_project_env_scaffold(folder)
        )
    except (OSError, UnicodeDecodeError, ConfigProjectionError) as exc:
        _cli_error("apply_failed", str(exc))
        return 4

    print(
        json.dumps(
            {
                "ok": True,
                "report": report,
                "project_id": args.project_id,
                "project_folder": str(folder.resolve()),
            }
        )
    )
    return 0


def _cli_reference(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template reference --project-id <id> --project-folder <path>``
    — the Safe-add sidecar; the live ``.env`` is never touched."""
    keys, failed = _cli_resolve(args)
    if keys is None:
        return failed
    try:
        sidecar = write_env_reference(keys, project_folder=Path(args.project_folder))
    except OSError as exc:
        _cli_error("reference_failed", str(exc))
        return 4
    print(json.dumps({"ok": True, "path": str(sidecar), "keys": sorted(keys)}))
    return 0


def _cli_effective(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template effective --project-folder <p> --key K``
    — the winning assignment of one MANAGED key (never a secret: only keys
    of :func:`list_canonical_env_template_keys` are answered)."""
    if args.key not in list_canonical_env_template_keys():
        _cli_error("key_not_managed", f"{args.key} is not a key the managed block carries")
        return 2
    env_path = Path(args.project_folder) / ".env"
    try:
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        _cli_error("read_failed", str(exc))
        return 4
    value, in_block = effective_assignment(text, args.key)
    print(json.dumps({"ok": True, "key": args.key, "value": value, "in_block": in_block}))
    return 0


def _cli_list_keys(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template list-keys --json``."""
    keys = sorted(list_canonical_env_template_keys())
    if args.json:
        print(json.dumps(keys))
    else:
        for k in keys:
            print(k)
    return 0


def _cli_from_db(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template from-db --project-id <id> --json``.

    Resolve the subset map and print it without writing anything.
    """
    try:
        keys = project_env_template_from_db(
            args.project_id,
            db_path=Path(args.db_path) if args.db_path else None,
            orchestrator_root=(
                Path(args.orchestrator_root) if args.orchestrator_root else None
            ),
        )
    except ProjectNotFound as exc:
        print(
            json.dumps({"error": "project_not_found", "message": str(exc)}),
            file=sys.stderr,
        )
        return 2
    except DbUnreachable as exc:
        print(
            json.dumps({"error": "db_unreachable", "message": str(exc)}),
            file=sys.stderr,
        )
        return 3

    out = {
        "project_id": args.project_id,
        "canonical_env_template": keys,
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for k, v in keys.items():
            print(f"{k}={v}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.env_template",
        description=(
            "DB-as-source-of-truth contract for the per-project .env template. "
            "Phase 0.D parallel of vco_lib.config_projection for the fourth "
            "env surface."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    for verb, help_text, handler in (
        (
            "apply",
            "resolve env from launcher DB and write to <project>/.env managed block",
            _cli_apply,
        ),
        (
            "reference",
            "Safe add: write <project>/.env.vco.reference (the live .env is never touched)",
            _cli_reference,
        ),
    ):
        p_verb = sub.add_parser(verb, help=help_text)
        p_verb.add_argument("--project-id", required=True)
        p_verb.add_argument(
            "--project-folder",
            required=True,
            help="absolute path to the project root",
        )
        p_verb.add_argument(
            "--db-path",
            default=None,
            help="override launcher DB path (defaults to <vct_root_dir>/launcher.db)",
        )
        p_verb.add_argument(
            "--orchestrator-root",
            default=None,
            help=(
                "path to orchestrator clone (forwarded to project_env_from_db; "
                "no .env effect today)"
            ),
        )
        p_verb.add_argument("--weaviate-port", type=int, default=8081)
        p_verb.add_argument("--ollama-port", type=int, default=11435)
        p_verb.add_argument("--code-embed-port", type=int, default=11440)
        p_verb.set_defaults(handler=handler)

    p_eff = sub.add_parser(
        "effective",
        help="the winning (last) assignment of one managed key in <project>/.env (read-only)",
    )
    p_eff.add_argument("--project-folder", required=True)
    p_eff.add_argument("--key", required=True)
    p_eff.set_defaults(handler=_cli_effective)

    p_list = sub.add_parser(
        "list-keys",
        help="print the canonical key subset this module manages",
    )
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(handler=_cli_list_keys)

    p_from = sub.add_parser(
        "from-db",
        help="resolve and print the env subset as JSON (no writes)",
    )
    p_from.add_argument("--project-id", required=True)
    p_from.add_argument("--db-path", default=None)
    p_from.add_argument("--orchestrator-root", default=None)
    p_from.add_argument("--json", action="store_true", default=True)
    p_from.set_defaults(handler=_cli_from_db)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENV_TEMPLATE_BEGIN",
    "ENV_TEMPLATE_END",
    "apply_env_template",
    "effective_assignment",
    "list_canonical_env_template_keys",
    "project_env_template_from_db",
    "remove_line_under",
    "render_project_env_scaffold",
    "write_env_reference",
]
