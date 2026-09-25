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
line still holding a placeholder a retired writer made up — the
unsubstituted ``<project>``, or the exact ``Project`` / ``Project_*`` values
of :data:`_RETIRED_PLACEHOLDER_PAIRS`. Anything
else in the section — a commented ``# GITHUB_TOKEN=`` placeholder, a key
the block does not carry — stays, and so does every line outside those
sections. A section left with no key lines loses its header too. The
result is ONE set: each managed key is assigned once, either by the block
or by the user.

A folded line whose value DIFFERS from what VCO now renders (owner ruling,
review R5 F35) is not lost: on the launcher path VCO's value goes in the
block and the old one stays OUTSIDE it as ``<KEY>_old=<value>`` (original
quoting; ``_old2``, ``_old3``… when ``<KEY>_old`` already holds something
else — never overwritten, never duplicated), recorded in the project's
auto-resolution trail by key name only, under ONE comment line
(:data:`PRESERVED_VALUES_COMMENT`). Placeholders are not kept; fill-only
(``install.py``) carries the old value into the block instead — its RAW text,
quotes included (review R6 F45). Secret-shaped keys are never folded.

The unregister (:func:`strip_project_env`) removes only what VCO authored —
the block, these legacy sections, that comment — and leaves (and reports) a
user's own assignment of a managed key and the ``<KEY>_old`` values
(review R6 F47).

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
from typing import Callable, Mapping, Optional, Sequence

from vco_lib.atomic import atomic_rewrite_text
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
    weaviate_port_default: int | None = None,
    ollama_port_default: int | None = None,
    code_embed_port_default: int | None = None,
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
            so the call can add keys but never change or drop one. A
            placeholder a retired writer made up (:func:`_is_placeholder`) is
            not kept.

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
    preserved: list[tuple[str, str]] = []
    if keep_existing_values:
        # Precedence: the old block's value > a migrated legacy line's value
        # > the caller's. Nothing a previous run settled changes. A legacy
        # value is carried as its RAW text (review R6 F45): the block renders
        # values verbatim, so `PROJECT_NAME="My Proj"` must keep its quotes —
        # parsing is for comparisons only.
        kept = {k: v for k, v in migrated.items() if v is not None}
        if block is not None:
            kept.update(
                (k, v) for k, v in _block_values(block).items()
                if not _is_placeholder(k, v)
            )
        wanted = {**{k: kept.get(k, v) for k, v in keys.items()}, **kept}
        user_set = _user_set_keys(before + after) & set(wanted)
    else:
        # Owner ruling (review R5 F35): VCO's value goes in the block, and a
        # DIFFERENT value a folded legacy line held stays discoverable as
        # `<KEY>_old` outside it — never overwritten, never duplicated.
        before, preserved = _preserve_replaced_values(before, after, migrated, keys)
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
        _record_preserved(project_folder, preserved)
    report = _report(rendered, user_set, set(migrated), previously_set, action)
    report["preserved"] = [f"{key}->{name}" for key, name in preserved]
    return report


#: The comment VCO puts above the `<KEY>_old` lines it keeps (owner ruling F35).
#: Written ONCE per file (a later preservation reuses it) and worded to stay
#: true wherever the block is — or after an unregister removed it, which also
#: removes this comment and leaves the `_old` lines (review R6 F47).
PRESERVED_VALUES_COMMENT = (
    "# vco: your earlier value(s) of keys VCO sets in its VCO-MANAGED block — "
    "kept for reference, not read by VCO"
)


def _parsed_value(raw: str) -> str:
    """The value a reader gets from ``KEY=<raw>`` — THE line grammar
    (:func:`vco_lib.envfile.parse_env_line`: one quote pair stripped)."""
    from vco_lib.envfile import parse_env_line

    pair = parse_env_line(f"K={raw}")
    return pair[1] if pair is not None else raw


def _preserve_replaced_values(
    before: str,
    after: str,
    migrated: Mapping[str, Optional[str]],
    keys: Mapping[str, str],
) -> tuple[str, list[tuple[str, str]]]:
    """Owner ruling (review R5 F35). For every folded legacy line whose value
    DIFFERS from what VCO now renders for that key (placeholders were never
    carried — they are ``None`` in ``migrated``), append ``<KEY>_old=<raw>``
    — the original text after ``=``, quotes and all — to ``before``, under
    :data:`PRESERVED_VALUES_COMMENT`. An existing ``<KEY>_old`` holding a
    different value is never overwritten: the next free ``<KEY>_old2``,
    ``_old3``… is used; one already holding this value is reused (so a
    re-run adds nothing). The comment is written once: when ``before``
    already carries it, the new lines join the ones under it. Returns
    ``(before, [(KEY, name), ...])``."""
    from vco_lib.envfile import parse_env_line

    assigned: dict[str, set[str]] = {}
    for line in (before + after).splitlines():
        pair = parse_env_line(line)
        if pair is not None:
            assigned.setdefault(pair[0], set()).add(pair[1])
    lines: list[str] = []
    preserved: list[tuple[str, str]] = []
    for key in sorted(migrated):
        raw = migrated[key]
        if raw is None or key not in keys or _parsed_value(raw) == keys[key]:
            continue
        n = 1
        while True:
            name = f"{key}_old" if n == 1 else f"{key}_old{n}"
            held = assigned.get(name)
            if held is None:
                lines.append(f"{name}={raw}")
                assigned[name] = {_parsed_value(raw)}
                preserved.append((key, name))
                break
            if _parsed_value(raw) in held:
                break
            n += 1
    if not lines:
        return before, []
    existing = before.split("\n")
    for i, line in enumerate(existing):
        if line.rstrip("\r") != PRESERVED_VALUES_COMMENT:
            continue
        eol = "\r" if line.endswith("\r") else ""
        j = i + 1
        while j < len(existing) and _is_old_value_line(existing[j]):
            j += 1
        existing[j:j] = [f"{new}{eol}" for new in lines]
        return "\n".join(existing), preserved
    sep = "" if not before or before.endswith("\n") else "\n"
    return before + sep + PRESERVED_VALUES_COMMENT + "\n" + "\n".join(lines) + "\n", preserved


_OLD_VALUE_NAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*?)_old\d*$")


def _is_old_value_line(line: str) -> bool:
    """An active ``<KEY>_old`` / ``<KEY>_oldN`` assignment (a kept value)."""
    from vco_lib.envfile import parse_env_line

    pair = parse_env_line(line)
    return pair is not None and _OLD_VALUE_NAME.match(pair[0]) is not None


def _record_preserved(project_folder: Path, preserved: list[tuple[str, str]]) -> None:
    """One auto-resolution trail row per kept value — key NAMES and the file
    only, never a value."""
    if not preserved:
        return
    from vco_lib.deferral_emit import record_auto_resolution

    for key, name in preserved:
        record_auto_resolution(
            project_folder,
            "env_legacy_value_preserved",
            "kept your earlier value outside VCO's block",
            f"{key} -> {name} in .env",
        )


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
    """Remove the ``key`` assignment line sitting DIRECTLY under the
    VCO-authored comment line ``header`` (the only provenance a pre-v0.2.97
    line has) when ``remove_if(value)`` says so. ``None``: no such line (or
    no file); ``True``: removed (atomic write, every other byte — CRLF
    included — kept); ``False``: left.

    The line is read with THE line grammar,
    :func:`vco_lib.envfile.parse_env_line` (``export`` prefix, one matching
    quote pair, CRLF) — the same value tier 3 of the secret resolver returns
    for it — so ``remove_if`` sees the value a reader would, never the raw
    text after ``=`` (review R5 F34: a quoted line was stored WITH its
    quotes). The value goes only to ``remove_if`` — never into a return
    value, a log, or an exception message. Only the first such pair is
    handled."""
    from vco_lib.envfile import parse_env_line

    env_path = project_folder / ".env"
    if not env_path.is_file():
        return None
    with env_path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1):
        if lines[i].rstrip("\r\n") != header:
            continue
        pair = parse_env_line(lines[i + 1])
        if pair is None or pair[0] != key:
            continue
        if not remove_if(pair[1]):
            return False
        _atomic_write_text(env_path, "".join(lines[:i + 1] + lines[i + 2:]))
        return True
    return None


def replace_values_with_sentinel(
    env_path: Path,
    migrated_keys: Sequence[str],
    sentinel: str,
) -> "tuple[int, list[str]]":
    """Replace migrated keys' values in ``.env`` with ``sentinel`` (v0.2.97:
    moved here from ``vco_lib.secrets_audit`` — the ``.env`` has ONE writer;
    ``secrets_audit.rewrite_env_with_sentinels`` and the launcher's
    "Migrate from .env" button both call this).

    Returns ``(num_replaced, missed)`` where ``missed`` is the list of
    keys that were not found in the file (defensive logging only — the
    caller usually trusts the audit step's output, but if the user edited
    the .env between audit and rewrite a key may have moved).

    Atomic-write semantics: through :func:`_atomic_write_text`, i.e.
    :func:`vco_lib.atomic.atomic_rewrite_text` — a ``mkstemp`` sibling (0600)
    is written, fsync'd and ``os.replace()``'d into place, and the mode
    ``env_path`` had BEFORE the rewrite (read with ``stat`` first) is
    re-applied to the final path after the rename. The file is therefore
    never briefly more permissive than before, and a 0640 ``.env`` stays
    0640 (pinned by ``tests/test_v0297_r6_stale_texts.py``).

    Each replaced line keeps its original key + ``export`` prefix (if
    any) + trailing comment (if any). Only the value bytes change:

    .. code-block:: text

       # before
       export OPENAI_API_KEY="sk-abc123"  # team key

       # after (with sentinel = "__vco_keychain__")
       export OPENAI_API_KEY=__vco_keychain__  # team key

    Comments preserved; ``export`` prefix preserved; quotes stripped (the
    sentinel doesn't need quoting and quotes-around-sentinel would just
    add noise).
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"cannot read {env_path}: {exc}") from exc

    keyset = set(migrated_keys)
    seen: set[str] = set()
    out_lines: list[str] = []
    replaced_count = 0

    for raw in text.splitlines(keepends=False):
        line = raw
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        # Preserve leading whitespace + `export` prefix; identify the key.
        leading_ws_len = len(line) - len(stripped)
        leading_ws = line[:leading_ws_len]
        body = stripped
        export_prefix = ""
        if body.startswith("export "):
            export_prefix = "export "
            body = body[len("export "):].lstrip()
            # Re-measure leading_ws to include the gap absorbed by lstrip
            # (rare, but happens with `export   KEY=val`).
            export_prefix = "export "
        eq = body.find("=")
        if eq <= 0:
            out_lines.append(line)
            continue
        key = body[:eq].strip()
        if key not in keyset:
            out_lines.append(line)
            continue
        if key in seen:
            # Duplicate key — preserve as-is (user has a malformed .env;
            # we don't try to "fix" it beyond the first-occurrence
            # replacement).
            out_lines.append(line)
            continue
        seen.add(key)
        raw_value = body[eq + 1:]
        # Detect trailing inline comment on an unquoted value.
        trailing_comment = ""
        val_str = raw_value
        val_stripped = val_str.strip()
        if not (val_stripped.startswith('"') or val_stripped.startswith("'")):
            hash_pos = val_stripped.find("#")
            if hash_pos >= 0:
                trailing_comment = "  " + val_stripped[hash_pos:]
        new_line = (
            f"{leading_ws}{export_prefix}{key}={sentinel}{trailing_comment}"
        )
        out_lines.append(new_line)
        replaced_count += 1

    missed = [k for k in keyset if k not in seen]

    # Preserve trailing newline if the original had one.
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"

    # Atomic write through the ONE rewrite home (`_atomic_write_text` →
    # `vco_lib.atomic.atomic_rewrite_text`): it reads the existing mode, writes
    # a 0600 mkstemp sibling, renames it into place and re-applies that mode —
    # so the file is never briefly MORE permissive than before and a 0600 /
    # 0640 `.env` keeps its bits.
    _atomic_write_text(env_path, new_text)

    return replaced_count, missed


#: The forensic tail B12 leaves on the line it repairs (byte-compatible with
#: the pre-v0.2.97 Rust repair, so an old repaired line reads the same).
_B12_NOTE = 'B12 auto-repaired 0.2.11: was "{old}"'


def repair_stale_kg_collection(
    project_folder: Path, canonical: str, stale_values: "list[str] | tuple[str, ...]"
) -> bool:
    """B12 (0.2.11), moved here from the launcher's Rust ``naming.rs`` in
    v0.2.97 so a project's ``.env`` has ONE writer. When no line already
    reads ``KG_COLLECTION=<canonical>``, the FIRST line reading exactly
    ``KG_COLLECTION=<stale>`` (one of ``stale_values``, trimmed) becomes
    ``KG_COLLECTION=<canonical> # B12 auto-repaired 0.2.11: was "<old>"``.
    Every other byte (CRLF included) is kept, and the file keeps its mode.
    Returns whether it rewrote. Which values are stale is the caller's
    naming policy (the launcher's sanitizer), not this writer's."""
    env_path = project_folder / ".env"
    if not env_path.is_file():
        return False
    with env_path.open(encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines(keepends=True)
    canonical_line = f"KG_COLLECTION={canonical}"
    stale_lines = {f"KG_COLLECTION={v}" for v in stale_values}
    stale_at: Optional[int] = None
    for i, line in enumerate(lines):
        body = line.strip()
        # A line B12 already repaired carries its forensic ` # B12 …` tail —
        # it IS the canonical line (the pre-v0.2.97 Rust check missed that,
        # so a second stale line would have been "repaired" on a re-run).
        if body == canonical_line or body.startswith(canonical_line + " #"):
            return False
        if stale_at is None and body in stale_lines:
            stale_at = i
    if stale_at is None:
        return False
    old = lines[stale_at]
    ending = old[len(old.rstrip("\r\n")):]
    lines[stale_at] = f"{canonical_line} # {_B12_NOTE.format(old=old.strip())}{ending}"
    _atomic_write_text(env_path, "".join(lines))
    return True


def strip_project_env(
    project_folder: Path, keys: "set[str] | frozenset[str]"
) -> dict[str, list[str]]:
    """The unregister's ``.env`` strip — VCO's lines only (the evidence rule,
    review R6 F47; it was a Rust read-modify-write until review R5 F40).

    Removes what VCO authored and nothing else:

      * the managed block WHOLE — markers, forensic comments, every key;
      * inside a recognised legacy section (the retired writers' headers,
        :func:`_migrate_legacy_sections`), the lines for the block's keys and
        ``keys``, active or commented — and the header when nothing is left;
      * the :data:`PRESERVED_VALUES_COMMENT` line VCO wrote above kept values;
      * the new-file header (:data:`_PROJECT_ENV_SCAFFOLD_HEADER`) when the
        file still starts with it byte-for-byte — it describes the block, so
        it would be false without it. An edited header is the user's text.
        The header the pre-v0.2.97 writers put on a new file
        (:data:`_LEGACY_SCAFFOLD_HEADER`) is recognised the same way.

    A user's OWN assignment of one of those keys anywhere else — the line
    :func:`apply_env_template` classifies ``user_set`` and never renders over —
    is LEFT and reported, never deleted and never called VCO's. The
    ``<KEY>_old`` lines are the user's earlier values: left and reported.
    A ``# KEY=`` comment outside those sections is the user's text and stays
    (it sets nothing). Every kept line is byte-for-byte (CRLF included) and
    the file keeps its mode. Nothing is written when nothing was VCO's. A missing file reports
    nothing; an unreadable one raises (never a rewrite from a guess).

    Returns ``{"removed": [...], "left": [...], "preserved": [...]}`` — key
    names only, sorted: removed (VCO's), left (the user's own assignments of
    a managed key), preserved (``<KEY>_old`` names still in the file).
    """
    from vco_lib.envfile import parse_env_line

    env_path = project_folder / ".env"
    if not env_path.is_file():
        return {"removed": [], "left": [], "preserved": []}
    with env_path.open(encoding="utf-8", newline="") as handle:
        prior = handle.read()
    before, block, after = _split_managed(prior)
    removed: set[str] = set(_block_values(block)) if block is not None else set()
    managed = set(keys) | removed
    before, migrated_before = _migrate_legacy_sections(before, managed)
    after, migrated_after = _migrate_legacy_sections(after, managed)
    removed |= set(migrated_before) | set(migrated_after)
    kept = [
        line for line in (before + after).splitlines(keepends=True)
        if line.rstrip("\r\n") != PRESERVED_VALUES_COMMENT
    ]
    new_text = "".join(kept)
    new_text = _without_scaffold_header(new_text)
    left: set[str] = set()
    preserved: set[str] = set()
    for line in kept:
        pair = parse_env_line(line)
        if pair is None:
            continue
        if pair[0] in managed:
            left.add(pair[0])
            continue
        old_of = _OLD_VALUE_NAME.match(pair[0])
        if old_of is not None and old_of.group(1) in managed:
            preserved.add(pair[0])
    if new_text != prior:
        _atomic_write_text(env_path, new_text)
    return {"removed": sorted(removed), "left": sorted(left), "preserved": sorted(preserved)}


def _without_scaffold_header(text: str) -> str:
    """``text`` minus the new-file header VCO wrote at its start — the
    current one or the pre-v0.2.97 one — when it is still exactly VCO's
    bytes. Anything else (an edited header, a line above it) is the user's
    and ``text`` comes back unchanged."""
    if text.startswith(_PROJECT_ENV_SCAFFOLD_HEADER):
        return text[len(_PROJECT_ENV_SCAFFOLD_HEADER):]
    legacy = _LEGACY_SCAFFOLD_HEADER.match(text)
    if legacy is not None:
        return text[legacy.end():]
    return text


# ─── Legacy VCO-authored lines (pre-v0.2.97 writers) ────────────────────
#
# The headers the retired append-only writers put above their lines. Byte
# strings of shipped files — see the module docstring, "Legacy lines".

#: The new-file header BOTH retired writers put on a fresh ``.env`` — the
#: launcher's Rust ``build_canonical_env_text`` and ``install.py``'s
#: ``_env_canonical_template`` (every release from its introduction in
#: 61f33bc4 until v0.2.97; ``git log -S "Edit values to override defaults"``
#: finds no other wording). The date is the one variable. Rust wrote LF;
#: ``install.py`` used ``Path.write_text``, so on Windows the same bytes
#: landed with CRLF — the backreference accepts either, but not a mix
#: (a mix means someone edited it). Like the current header it describes a
#: VCO-owned file, so the unregister removes it while it is exactly VCO's.
_LEGACY_SCAFFOLD_HEADER = re.compile(
    r"# vibecoded-orchestrator per-project \.env(\r?\n)"
    r"# Edit values to override defaults\. Empty / commented lines are\1"
    r'# treated as "use default"\. Created by vco [0-9]{4}-[0-9]{2}-[0-9]{2}\.\1\1'
)
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
#: Exact ``(KEY, value)`` pairs the retired writers MADE UP when they did not
#: know the project's name: the pre-v0.2.97 install.py re-install block
#: (``sanitized = "Project"``) and ``--update`` reconcile defaulted to
#: ``Project`` / ``Project_*``. Like ``<project>`` they are placeholders, not
#: values — but only where those writers put them: inside VCO's managed block
#: (on the fill-only path) or a recognised legacy section. A user's own line
#: saying ``PROJECT_NAME=Project`` elsewhere is theirs (review R5 F42).
_RETIRED_PLACEHOLDER_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("PROJECT_NAME", "Project"),
    ("CODE_GRAPH_PROJECT", "Project"),
    ("KG_COLLECTION", "Project_KnowledgeGraph"),
    ("DEVELOPMENT_COLLECTION", "Project_Development"),
})


def _secret_shaped(key: str) -> bool:
    from vco_lib.secrets_audit import is_secret_shaped_env_key

    return is_secret_shaped_env_key(key)


def _is_placeholder(key: str, value: str) -> bool:
    """A made-up value a retired VCO writer left (see
    :data:`_RETIRED_PLACEHOLDER_PAIRS`). Callers apply it only to lines that
    writer authored."""
    return _PLACEHOLDER_TOKEN in value or (key, value.strip()) in _RETIRED_PLACEHOLDER_PAIRS


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
    key is rendered by the block, or it is active and still holds a
    placeholder (:func:`_is_placeholder`). ``value`` is the line's value when
    it is an active, real one (a fill-only caller carries it into the block),
    else ``None``."""
    key = _KEY_LINE.match(line.rstrip("\r"))
    if not key or _secret_shaped(key.group(2)):
        # A secret-shaped key is never folded (owner ruling F35): its line is
        # the user's, whatever section it sits in.
        return None
    active = not key.group(1)
    placeholder = _is_placeholder(key.group(2), key.group(3))
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

    v0.2.97 (review R5 F36): an EXISTING file keeps its permission bits (the
    retired Rust writer rewrote in place, so a 0640 ``.env`` stayed 0640) —
    through the one home, :func:`vco_lib.atomic.atomic_rewrite_text`.
    """
    atomic_rewrite_text(path, content)


# ─── New-file scaffold + the Safe-add reference sidecar ──────────────────

# The header + commented placeholders a NEW project ``.env`` starts with
# (user-owned lines outside the managed block: the launcher never fills
# them — they are secrets or paid-module settings). Written once, at
# creation; after that the file's non-block content is the user's — except
# the header, which describes VCO's block: the unregister removes it when it
# is still byte-identical to this text (review R6), because without the block
# it would be false. An edited header is the user's text and stays.
_PROJECT_ENV_SCAFFOLD_HEADER = """\
# vibecoded-orchestrator per-project .env
# VCO keeps the block between the VCO-MANAGED markers below up to date.
# To override one of its keys, set it anywhere OUTSIDE the block — VCO
# then stops writing that key. A commented line sets nothing.

"""
_PROJECT_ENV_SCAFFOLD = _PROJECT_ENV_SCAFFOLD_HEADER + """\
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
            weaviate_url_override=args.weaviate_url,
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


def _cli_sentinel(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template sentinel --project-folder <p>`` with
    ``{"keys": [...]}`` on stdin — replace those keys' values in the
    project's ``.env`` with the keychain sentinel (the launcher's "Migrate
    from .env", after the hub confirmed each key). Never prints a value."""
    from vco_lib.secrets_audit import KEYCHAIN_SENTINEL

    try:
        request = json.loads(sys.stdin.read() or "{}")
        keys = [str(k) for k in request.get("keys", [])]
    except (ValueError, AttributeError) as exc:
        _cli_error("bad_request", f"stdin is not a {{\"keys\": [...]}} object: {exc}")
        return 2
    env_path = Path(args.project_folder) / ".env"
    if not env_path.is_file():
        print(json.dumps({"ok": True, "replaced": 0, "missed": sorted(keys)}))
        return 0
    try:
        replaced, missed = replace_values_with_sentinel(env_path, keys, KEYCHAIN_SENTINEL)
    except (OSError, UnicodeDecodeError, RuntimeError) as exc:
        _cli_error("sentinel_failed", str(exc))
        return 4
    print(json.dumps({"ok": True, "replaced": replaced, "missed": sorted(missed)}))
    return 0


def _cli_repair_kg(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template repair-kg --project-folder <p>
    --canonical <C> --stale <S> [--stale <S2>]`` — the B12 repair."""
    try:
        repaired = repair_stale_kg_collection(
            Path(args.project_folder), args.canonical, list(args.stale)
        )
    except (OSError, UnicodeDecodeError) as exc:
        _cli_error("repair_failed", str(exc))
        return 4
    print(json.dumps({"ok": True, "action": "repaired" if repaired else "unchanged"}))
    return 0


def _cli_strip(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template strip --project-folder <p>`` with
    ``{"keys": [...]}`` on stdin — the unregister's ``.env`` strip."""
    try:
        request = json.loads(sys.stdin.read() or "{}")
        keys = {str(k) for k in request.get("keys", [])}
    except (ValueError, AttributeError) as exc:
        _cli_error("bad_request", f"stdin is not a {{\"keys\": [...]}} object: {exc}")
        return 2
    try:
        outcome = strip_project_env(Path(args.project_folder), keys)
    except (OSError, UnicodeDecodeError) as exc:
        _cli_error("strip_failed", str(exc))
        return 4
    print(json.dumps({"ok": True, **outcome}))
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
        # v0.2.97 (lane W): unset = this machine's value
        # (vco_lib.service_endpoints); the launcher passes its own resolution.
        p_verb.add_argument("--weaviate-url", default=None)
        p_verb.add_argument("--weaviate-port", type=int, default=None)
        p_verb.add_argument("--ollama-port", type=int, default=None)
        p_verb.add_argument("--code-embed-port", type=int, default=None)
        p_verb.set_defaults(handler=handler)

    p_sentinel = sub.add_parser(
        "sentinel",
        help="replace the given keys' values in <project>/.env with the keychain "
             'sentinel (stdin: {"keys": [...]})',
    )
    p_sentinel.add_argument("--project-folder", required=True)
    p_sentinel.set_defaults(handler=_cli_sentinel)

    p_repair = sub.add_parser(
        "repair-kg",
        help="B12: rewrite a stale KG_COLLECTION= line of <project>/.env to the canonical name",
    )
    p_repair.add_argument("--project-folder", required=True)
    p_repair.add_argument("--canonical", required=True)
    p_repair.add_argument("--stale", action="append", default=[], required=True)
    p_repair.set_defaults(handler=_cli_repair_kg)

    p_strip = sub.add_parser(
        "strip",
        help="unregister: remove VCO's block and the given keys from <project>/.env "
             '(stdin: {"keys": [...]})',
    )
    p_strip.add_argument("--project-folder", required=True)
    p_strip.set_defaults(handler=_cli_strip)

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
    "repair_stale_kg_collection",
    "replace_values_with_sentinel",
    "strip_project_env",
    "render_project_env_scaffold",
    "write_env_reference",
]
