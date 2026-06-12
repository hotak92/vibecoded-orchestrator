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
  and everything outside the markers — including user-added KEY=value
  lines for the SAME key — is preserved verbatim. This means a user who
  copies ``KG_COLLECTION=`` out of the managed block to override it gets
  what they want: the managed block re-renders the launcher's value
  every run, but their override (lower in the file, last-write-wins
  under shell sourcing rules) takes effect.
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

* The legacy append-only ``# added by vco YYYY-MM-DD`` block format
  emitted by ``install.py::_ensure_env_template`` and Rust's
  ``ensure_project_env_template``. Both writers are being MIGRATED to
  call into :func:`apply_env_template` (block-replace) instead of
  append-only — see Step 2 of the Phase 0.D task brief. Existing
  ``.env`` files with legacy ``# added by vco YYYY-MM-DD`` annotations
  will pick up the new managed block on the next run; their old
  annotations sit outside the markers and are preserved verbatim. (They
  become inert: the same keys are re-emitted inside the managed block,
  and shell-sourcing rules mean the LAST assignment wins. The old
  annotated lines were appended AFTER any user values, so on most files
  they end up dominated by the new block — same effective behaviour
  as before; users who care can manually delete the legacy lines.)
* The full template body (banner comments, commented-out RL section,
  telemetry section, LLM API keys) emitted by the legacy
  ``_build_canonical_env_template_text``. Phase 0.D's contract is for
  the MANAGED block only; the legacy renderer can keep emitting the
  commented-placeholder lines for OPTIONAL keys (ANTHROPIC_API_KEY,
  OPENAI_API_KEY, GITHUB_TOKEN, RL_SERVER_URL, VCT_TELEMETRY) outside
  the managed markers. Those keys are intentionally outside the
  contract because they are user-secrets-or-modules — the launcher
  never autopopulates them.

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
  POSIX shells. (The Rust legacy writer also uses LF; this matches.)
* UTF-8 encoding; values are passed through verbatim. Double-quoted
  shell escaping is NOT applied because ``.env`` lines are
  ``KEY=value`` (no quotes around value) — the de-facto ``.env`` format
  consumed by python-decouple, python-dotenv, direnv, etc. If a value
  contains shell metacharacters the user must quote it themselves; the
  legacy writer didn't quote either.

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

For Rust callers that subprocess into Python (mirrors the Phase 0.B
Part 2 pattern for ``write_project_env_files``)::

    python -m vco_lib.env_template apply --project-id <uuid> --project-folder <path>
    python -m vco_lib.env_template list-keys --json
    python -m vco_lib.env_template from-db --project-id <uuid> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping, Optional

from vco_lib.atomic import atomic_write_text
from vco_lib.config_projection import (
    ConfigProjectionError,
    DbUnreachable,
    ProjectEnvBundle,
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
) -> dict[str, list[str]]:
    """Project ``keys`` into ``<project_folder>/.env``'s managed block.

    Idempotent block-replace:

      * If ``.env`` doesn't exist: create it containing just the managed
        block.
      * If ``.env`` exists and contains :data:`ENV_TEMPLATE_BEGIN`: locate
        the segment from BEGIN through END (inclusive) and replace it
        wholesale. Content outside the markers is preserved verbatim.
      * If ``.env`` exists but does NOT contain BEGIN (legacy format
        from ``_ensure_env_template`` / ``ensure_project_env_template``,
        or a hand-written file): append the managed block at EOF
        (ensuring a leading newline so we don't glue onto the last line).
        On the next call BEGIN is present and in-place replace kicks in.

    Re-running with the same ``keys`` produces byte-identical output —
    this is what makes it safe to invoke unconditionally from every
    project-create / refresh / grant-toggle code path.

    Atomic write: tempfile in the same directory as ``.env``, then
    :func:`os.replace`. No ``.tmp`` leaks on success; the tempfile is
    cleaned up on every error path before re-raising.

    Args:
        keys: The canonical key→value map to render. Typically from
            :func:`project_env_template_from_db`; arbitrary maps are
            also accepted (useful for tests and for callers that mix in
            extra resolved values). Keys outside
            :func:`list_canonical_env_template_keys` are STILL rendered
            (no allowlist filter at the writer) — the SUBSET decision
            lives in the RESOLVER, not the writer, so callers can pass
            an extended map without surprises. The single-writer lint
            governs which CALLERS may invoke this module, not which
            keys land inside.
        project_folder: The project's root directory. ``.env`` lives
            directly inside it (NOT under ``.claude/``).

    Returns:
        ``{"env": [keys_written, ...]}`` — a one-key dict for audit
        symmetry with :func:`vco_lib.config_projection.apply_project_env`
        (which returns a multi-key dict for its multi-surface case).
        Keys are sorted for deterministic logging.

    Raises:
        :class:`OSError`: write failed (perms, disk full, etc.). The
            target ``.env`` is left untouched (atomic rename guarantee).
    """
    env_path = project_folder / ".env"
    project_folder.mkdir(parents=True, exist_ok=True)

    prior: Optional[str]
    if env_path.exists():
        try:
            prior = env_path.read_text(encoding="utf-8")
        except OSError:
            prior = None
    else:
        prior = None

    managed = _build_managed_block(keys)
    new_text = _merge_managed_block(prior, managed)
    _atomic_write_text(env_path, new_text)

    return {"env": sorted(keys.keys())}


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


# ─── CLI entry points ───────────────────────────────────────────────────


def _cli_apply(args: argparse.Namespace) -> int:
    """``python -m vco_lib.env_template apply --project-id <id> --project-folder <path>``."""
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

    try:
        report = apply_env_template(
            keys, project_folder=Path(args.project_folder)
        )
    except OSError as exc:
        print(
            json.dumps({"error": "apply_failed", "message": str(exc)}),
            file=sys.stderr,
        )
        return 4
    except ConfigProjectionError as exc:
        # Forwarded for symmetry with Phase 0.B's CLI — apply_env_template
        # doesn't raise this directly today, but the contract surface
        # leaves room for future validation errors that share the type.
        print(
            json.dumps({"error": "apply_failed", "message": str(exc)}),
            file=sys.stderr,
        )
        return 4

    print(
        json.dumps(
            {
                "ok": True,
                "report": report,
                "project_id": args.project_id,
                "project_folder": str(Path(args.project_folder).resolve()),
            }
        )
    )
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

    p_apply = sub.add_parser(
        "apply",
        help="resolve env from launcher DB and write to <project>/.env managed block",
    )
    p_apply.add_argument("--project-id", required=True)
    p_apply.add_argument(
        "--project-folder",
        required=True,
        help="absolute path to the project root (where .env will be written)",
    )
    p_apply.add_argument(
        "--db-path",
        default=None,
        help="override launcher DB path (defaults to <vct_root_dir>/launcher.db)",
    )
    p_apply.add_argument(
        "--orchestrator-root",
        default=None,
        help=(
            "path to orchestrator clone (forwarded to project_env_from_db; "
            "no .env effect today)"
        ),
    )
    p_apply.add_argument("--weaviate-port", type=int, default=8081)
    p_apply.add_argument("--ollama-port", type=int, default=11435)
    p_apply.add_argument("--code-embed-port", type=int, default=11440)
    p_apply.set_defaults(handler=_cli_apply)

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
    "list_canonical_env_template_keys",
    "project_env_template_from_db",
]
