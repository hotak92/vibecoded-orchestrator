# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Filesystem path resolution for launcher state — Python side.

Mirrors the Rust helper at ``launcher/src-tauri/src/paths.rs``. All
launcher state files live under one root: ``~/.vct/`` in production,
or ``$VCT_STATE_DIR`` if set (so a dev launcher running against an
in-development VCO clone can keep its state isolated from the
production launcher's).

Usage (Python side — scripts that need state-dir paths)::

    from vco_lib.paths import vct_root_dir
    services_toml = vct_root_dir() / "services.toml"

The Rust launcher reads the same env var via ``crate::paths::vct_root_dir``;
both sides MUST agree, otherwise the Python install scripts will write
state where the launcher won't read it.

Why ``~/.vct-secrets/`` is NOT under this root: secrets live at a stable,
keychain-fallback location independent of state-dir. The dev/prod split
intentionally shares secrets (so dev launcher can decrypt the same
admin-license token).

v0.2.92 W7 — the metrics home moved OUT of ``~/.claude``. The standing
directive is *"VCO writes NOTHING under ``~/.claude/`` except what the
harness itself requires"*: ``~/.claude`` belongs to Claude Code, and VCO's
own telemetry (``costs.jsonl``, ``failures.jsonl``, ``compactions.jsonl``,
``kg_update_tokens.jsonl``, ``embedding_failures.jsonl``,
``bundled_versions.jsonl``) is not something the harness asked for. Three
resolvers express that split:

* :func:`vct_metrics_dir`          — THE home (``<vct_root_dir()>/metrics``).
* :func:`legacy_claude_metrics_dir` — the pre-v0.2.92 location, now a
  FROZEN ARCHIVE that VCO copies from and never writes to or deletes.
* :func:`metrics_read_dirs`        — the ordered pair a READER consults, so
  a machine mid-migration (or one whose user-modified hook still appends to
  the archive) is never shown a partial history.

The RL telemetry corpus made the same move for the same reason (v0.2.92,
register item 28) but WITHOUT a copy — its sink is frozen and its remaining
consumers all address the old path by name. Its current home lives with the
logger (``rl_client.rl_logger.default_rl_data_dir`` → ``<vct_root_dir()>/
retrieval_rl_data``); the archive side is :func:`legacy_claude_rl_data_dir`,
so the four readers that still want the OLD corpus have a steerable resolver
instead of an inline ``Path.home()``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def to_posix_rel(rel: "str | Path") -> str:
    """Normalize a relative path to POSIX separators (``\\`` → ``/``).

    v0.2.84 PLAN-v0284 AMENDMENTS A4 (one-concern-one-home): the ``str(rel).replace("\\",
    "/")`` idiom (v0.2.81 lesson — Windows manifest keys / ``dest_rel`` values
    carry ``\\`` separators) was duplicated across ~15 call-sites. This is the
    single shared home so manifest-key comparisons, orphan-scan prefix checks,
    and the P5 adoption-backup mirror all agree on the same normalization.

    Pure + dependency-free (safe to import from anywhere). Does NOT resolve,
    absolutize, or touch the filesystem — it only swaps the separator so a
    host-OS-shaped ``dest_rel`` can be compared / joined POSIX-uniformly.
    """
    return str(rel).replace("\\", "/")


def looks_like_orchestrator_root(repo_path: "str | Path") -> bool:
    """Best-effort check: does ``repo_path`` look like the orchestrator clone?

    The orchestrator clone is the ONE tree where ``.claude/`` is first-party
    source under active development (bundled agents, hooks, scripts, MCP
    servers), so tooling that must decide whether to INDEX ``.claude/`` asks
    this. Heuristic: the root both contains ``vco_lib/`` (unique to the
    orchestrator clone) AND has a ``.claude/`` directory; every other project
    with VCO installed has ``.claude/`` but not ``vco_lib/``.

    ONE home (v0.2.91 dogfood fix): ``analyze_code_graph._looks_like_orchestrator_root``
    delegates here, and :mod:`vco_lib.deferral_retry` needs the same answer to
    pick the resync driver's ``--index-dot-claude`` flag. A second copy would be
    a second chance for the two to disagree about which tree indexes ``.claude``,
    which is exactly the kind of split that makes a spawn say "owed" and the
    verify say "converged".

    Never raises — returns False on any filesystem error (conservative:
    unknown → treat as a user project → exclude ``.claude``).
    """
    try:
        root = Path(repo_path)
        return (root / "vco_lib").is_dir() and (root / ".claude").is_dir()
    except OSError:
        return False


def vct_root_dir() -> Path:
    """Return the launcher's state-root directory.

    Resolution order:
      1. ``VCT_STATE_DIR`` env var (absolute path; not mkdir'd here).
      2. ``~/.vct/`` — production default.

    v0.2.40+ cross-OS plan (NOT implemented yet; tracked by the X1 batch):

      - Linux:   ``~/.vct/``                            (current default)
      - macOS:   ``~/Library/Application Support/vct/`` (Apple HIG)
      - Windows: ``%LOCALAPPDATA%\\vct\\``              (per-user, non-roaming)

    Today the resolver is POSIX-only — every OS lands on ``~/.vct/``.
    Centralising path reconstruction here means the cross-OS branches
    only have to land in ONE place when X1 implements them; the dozen+
    callers across ``install.py`` / ``vco_lib/diagram_indexer.py`` /
    ``vco_lib/project_init.py`` pick up the change for free.

    Mirror in the Rust launcher: ``launcher/src-tauri/src/paths.rs::vct_root_dir``.
    Both sides MUST agree (Python writes; Rust reads, or vice versa).
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"


def user_home() -> Path:
    """Return the user's home directory for VCO config reads/writes.

    Resolution order:
      1. ``VCT_USER_HOME_OVERRIDE`` env var.
      2. ``Path.home()``.

    This is the SAME env key ``install._user_home_for_install`` has honoured
    since v0.2.11 (PR-16) — that helper now delegates here rather than
    carrying its own copy. Lifted into :mod:`vco_lib.paths` because callers
    outside ``install.py`` need the same answer and cannot import it:
    ``vco_lib.doctor._read_claude_json_mcp_servers`` and
    ``vco_lib.cli.verify_diagrams._claude_json_path`` both resolve
    ``~/.claude.json``, and the vco_lib -> install back-edge was deliberately
    broken in v0.2.77 (7a-bis), so "just import install" is not available.

    They each reconstructed ``Path.home()`` inline instead, which is how the
    doctor kept reading the developer's REAL global Claude config from the
    test suite after ``install.py``'s own readers had been redirected
    (v0.2.92 W-CLAUDE; found by the conftest audit-hook tripwire, not by
    inspection — `verify_diagrams` even documents "Monkey-patched in tests",
    the per-symbol bet this replaces).

    ``~/.claude.json`` is resolved from HERE, not from
    :func:`claude_user_dir`: it is a FILE beside ``~/.claude/``, and Claude
    Code treats the two independently.
    """
    override = os.environ.get("VCT_USER_HOME_OVERRIDE", "").strip()
    if override:
        return Path(override)
    return Path.home()


def claude_user_dir() -> Path:
    """Return the Claude Code user directory (``~/.claude``).

    Resolution order:
      1. ``VCT_CLAUDE_DIR`` env var (absolute path; not mkdir'd here).
      2. ``~/.claude/`` — production default.

    Same shape, and same reason, as :func:`vct_root_dir`: every consumer must
    go through ONE resolver so a single env pin can steer all of them.

    **What VCO does under here, as of v0.2.92 W7: it READS.**
    ``workflow/config/mcp-config.json`` and the frozen metrics archive
    (:func:`legacy_claude_metrics_dir`) are reads; the telemetry streams VCO
    used to WRITE here now live under :func:`vct_metrics_dir`, because
    ``~/.claude`` is Claude Code's directory and the standing directive is that
    VCO writes nothing under it the harness did not ask for. If you are adding
    a WRITE under this root, that is the thing to justify — not the path.

    v0.2.92 W-CLAUDE, the leak the override closes: ``embedding_service``'s
    failure capture reconstructed ``Path.home() / ".claude" / "metrics"``
    inline, with no override anywhere in the chain. Nothing could steer it, so
    the test suite appended fixture-shaped rows (``"attempted_backends": []``,
    ``"message": "none"``) to the maintainer's real
    ``~/.claude/metrics/embedding_failures.jsonl`` on every local run — the
    same class of pollution as the ``~/.vct`` incident one release earlier,
    and for the same structural reason: a per-call-site path with no root.
    W7 then removed the write entirely; the override still steers the READ of
    the archive, which is why it stays load-bearing.

    NOT the same thing as ``~/.claude.json`` — that is a FILE beside this
    directory, resolved from the user HOME (``install._user_home_for_install``
    honours ``VCT_USER_HOME_OVERRIDE`` for it). Two resources, two levers.

    Cross-OS: ``Path.home()`` resolves on Linux, macOS and Windows, and
    Claude Code itself uses ``~/.claude`` on all three, so there is no
    per-OS branch to add here (unlike the X1 plan for :func:`vct_root_dir`).
    """
    custom = os.environ.get("VCT_CLAUDE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".claude"


def vct_metrics_dir() -> Path:
    """Return ``<vct_root_dir()>/metrics`` — THE home for VCO's JSONL telemetry.

    Every stream VCO produces lands here: ``costs.jsonl``, ``failures.jsonl``,
    ``compactions.jsonl``, ``kg_update_tokens.jsonl`` (the shell hooks),
    ``embedding_failures.jsonl`` (:mod:`vco_lib.embedding_service`) and
    ``bundled_versions.jsonl`` (``install.py`` / :mod:`vco_lib.cli.verify`).

    v0.2.92 W7 moved them out of ``~/.claude/metrics``. ``~/.claude`` is Claude
    Code's own directory and the standing directive is that VCO writes nothing
    under it that the harness did not ask for; ``~/.vct`` is where every other
    piece of VCO state already lives, so the streams join the launcher DB, the
    hub token/port files and ``~/.vct/logs/`` under ONE root a single env var
    (``$VCT_STATE_DIR``) can steer.

    Migrating from the old location is :mod:`vco_lib.metrics_migration` — a
    COPY, never a move: :func:`legacy_claude_metrics_dir` is left byte-identical
    as a frozen archive and is deleted by nobody but the user.

    Cross-OS: inherits :func:`vct_root_dir`'s resolution, so Windows/macOS pick
    up the X1 per-OS branches for free when they land. Callers must NEVER
    reconstruct ``~/.vct/metrics`` inline — ``tests/test_vct_root_dir_consolidation.py``
    forbids it, and an inline copy is a path no test redirect can steer (the
    v0.2.92 W-CLAUDE leak was exactly that shape).

    Does not create the directory: writers already ``mkdir(parents=True,
    exist_ok=True)`` on their own append path.
    """
    return vct_root_dir() / "metrics"


def legacy_claude_metrics_dir() -> Path:
    """Return ``<claude_user_dir()>/metrics`` — the pre-v0.2.92 metrics home.

    **This is a read-only archive, not a write target.** Since v0.2.92 VCO
    copies out of it (:mod:`vco_lib.metrics_migration`) and reads it as the
    second tier of :func:`metrics_read_dirs`; nothing in VCO writes to it and
    nothing in VCO deletes it. Cleanup is the user's call, on their timetable —
    the copy leaves every original byte-identical precisely so that decision
    stays theirs.

    It still resolves through :func:`claude_user_dir` (``$VCT_CLAUDE_DIR``) so
    the suite's W-CLAUDE redirect steers the archive too; a test that seeds a
    fake archive gets a fake one, never the maintainer's real history.
    """
    return claude_user_dir() / "metrics"


def metrics_read_dirs() -> tuple[Path, ...]:
    """Directories a metrics READER must consult, most-current first.

    ``(vct_metrics_dir(), legacy_claude_metrics_dir())``.

    Readers consult BOTH because three legitimate states put rows in the
    archive after the switch:

    1. **Mid-migration** — writers only move to the new home once the copy has
       been verified (see :mod:`vco_lib.metrics_migration`), so on a machine
       that has not migrated yet EVERY new row is still in the archive.
    2. **A hook left behind on the archive.** ``templates/**`` files are
       bundled, and the COMMON case now resolves itself: since v0.2.84 D7
       (ruling R2) ``install-bundle --update`` **ADOPTS** a hook the user
       edited — ``project_init._file_action`` returns ``adopt``, the user's
       bytes are copied to ``.claude/backups/bundle-adoptions/<ts>/`` and the
       SHIPPED bytes are written, so that user gets the new home AND keeps
       their edit recoverable. ``preserve`` + ``bundle_user_modified_preserved``
       is reached only when the backup write FAILS (full disk, read-only
       ``.claude/``, a symlinked ancestor that redirects the copy), and only
       THAT user's ``cost-tracker.sh`` keeps its v0.2.91 body and keeps
       appending to ``~/.claude/metrics`` indefinitely.

       This docstring asserted plain "PRESERVES" until v0.2.92, which had been
       false since v0.2.84 — recorded here because the false version is the
       more alarming one and a future reader who finds only the ``preserve``
       branch in the source should not "restore" it.
    3. **Rollback** — a downgraded launcher/hook writes to the archive again.

    The tuple is ordered, not a set, so callers that need last-writer-wins
    semantics (rather than a union) have a defined precedence. It always names
    both directories whether or not they exist; probing is the caller's job.
    """
    return (vct_metrics_dir(), legacy_claude_metrics_dir())


def claude_metrics_dir() -> Path:
    """DEPRECATED historical name for :func:`vct_metrics_dir`. Same object.

    **It does NOT return anything under ``~/.claude`` any more** — v0.2.92 W7
    moved the metrics home to ``<vct_root_dir()>/metrics`` and this alias moved
    with it, so the three callers outside this module
    (``install.py::_BUNDLED_VERSIONS_AUDIT_LOG``,
    :func:`vco_lib.cli.verify` and
    ``vco_lib.embedding_service._failure_jsonl_path``) followed the move
    without an edit to files another lane holds. Renaming those call sites to
    :func:`vct_metrics_dir` and deleting this alias is a mechanical follow-up;
    the alias exists so the move could not half-land.

    If you actually want the OLD directory — you almost certainly want it only
    to read the frozen archive — call :func:`legacy_claude_metrics_dir`.

    Kept as a delegating one-liner (the extract-and-shim pattern used for
    ``project_init._normalise_prefix_for_match``) rather than a second
    resolution, so the two names cannot drift apart.
    """
    return vct_metrics_dir()


def legacy_claude_rl_data_dir() -> Path:
    """Return ``<claude_user_dir()>/retrieval_rl_data`` — the FROZEN RL corpus.

    The pre-v0.2.92 home of the retrieval-telemetry JSONL corpus, and the same
    posture as :func:`legacy_claude_metrics_dir`: **read-only archive, never a
    write target.** Nothing in VCO writes to it, copies it or deletes it — the
    metrics archive got a verified COPY because its stream has live readers
    that must see one continuous history; this one did not, because the sink
    has been frozen since v0.2.47 (the vct-hub ``rl_events`` table replaced it)
    and its four remaining consumers all address this location BY NAME in three
    languages: the paid RL container's bind mount, the launcher dashboard's
    ``rl_events_<slug>.jsonl`` reader, the Preferences "delete local retrieval
    data" action, and ``claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py``.
    Copying gigabytes to a location nothing reads would buy nothing; MOVING it
    would break all four.

    The CURRENT home is ``claude_mcp_servers/rl_client/rl_logger.py::
    default_rl_data_dir()`` — ``vct_root_dir() / "retrieval_rl_data"``. This
    resolver exists so the ARCHIVE's readers stop reconstructing
    ``Path.home() / ".claude" / ...`` inline: that shape is unsteerable by
    construction (register item 28 — the same defect, at the write end), so a
    test redirect could not protect the maintainer's real corpus and a reader
    could not be pointed at a fixture.

    The directory BASENAME is deliberately the same on both sides, and is
    repeated in ``rl_logger._RL_DATA_DIRNAME`` rather than shared: that module
    is byte-identical-VENDORED into the paid RL container image, which builds
    without ``vco_lib``, so it cannot import this one at module scope.
    ``tests/test_v0292_regclean_rl_migrate_archive.py`` pins the two strings
    equal, which is the mirror-with-a-parity-test tier of the CLAUDE.md A>B>C
    rule, chosen because tier A is closed by the vendoring constraint.

    Cross-OS: inherits :func:`claude_user_dir` (``$VCT_CLAUDE_DIR``), so it
    resolves on Linux, macOS and Windows with no per-OS branch. Does not create
    the directory — an archive that does not exist is a normal, common state
    (any machine installed after v0.2.92) and materialising it would be a write
    under ``~/.claude``, which is the thing this family exists to stop.
    """
    return claude_user_dir() / "retrieval_rl_data"


def launcher_db_path() -> Path:
    """Return the canonical path to the launcher SQLite DB.

    A convenience for the many callers across ``install.py`` /
    ``vco_lib/diagram_indexer.py`` / ``vco_lib/project_init.py`` that
    always want the launcher.db file rather than the state-root
    directory.

    Resolution priority:
      1. ``$VCT_LAUNCHER_DB_PATH`` env override (v0.2.54: previously
         honoured ONLY by ``launcher_db_reader._discover_db_path`` —
         split-brain: setting the var moved the reader's view of the DB
         but not ``config_projection`` / ``project_init``, so e.g.
         ``_rebind_orchestrator_root_to_canonical_locked`` operated on a
         DIFFERENT database than the reader reported on).
      2. :func:`vct_root_dir` — honours ``$VCT_STATE_DIR``, falls back
         to ``~/.vct/launcher.db``.

    Unlike the reader's discovery helper, this resolver does NOT
    require the file to exist (callers that create/await the DB need
    the would-be path).
    """
    override = os.environ.get("VCT_LAUNCHER_DB_PATH", "").strip()
    if override:
        return Path(override)
    return vct_root_dir() / "launcher.db"


def resolve_project_name(cwd: Optional[Path] = None) -> Optional[str]:
    """Best-effort canonical project name for the current workspace.

    Used by per-event loggers (``ToolUsageLogger``, the RL telemetry path,
    etc.) so JSONL rows are stamped with the same project identifier the
    KG / code-graph use. The same resolution sequence already appears
    inline in ``vco_lib/cli/codegraph_diagram.py::_resolve_project_name``
    and ``templates/scripts/query_code_graph.py``; this helper deduplicates
    it across the three KG-CLI scripts (``search_knowledge.py`` /
    ``get_node_info.py`` / ``sync_knowledge_graph.py``) per v0.2.40 H1.

    Resolution order:
      1. Hub-resolved ``ProjectConfig.code_graph_project`` (canonical
         Weaviate-prefix form like ``VCODev``), via
         ``vco_lib.project_config.resolve(cwd)``. The hub is the single
         source of truth when the launcher is running.
      2. ``CODE_GRAPH_PROJECT`` env var (same shape as the hub value;
         set by ``.claude/settings.json env`` in installed projects).
      3. ``PROJECT_NAME`` env var (display name; falls back to the same
         value when ``CODE_GRAPH_PROJECT`` isn't set explicitly).
      4. ``None`` — no project context available. Callers should treat
         this as "unknown" rather than substituting a placeholder.

    Args:
        cwd: Workspace root to query the hub against. Defaults to
            ``Path.cwd()``. Pass an explicit path when the caller knows
            the workspace and wants to avoid relying on the process CWD.

    Returns:
        The resolved project name, or ``None`` when nothing is set.
        Empty-string env values are treated as unset (consistent with
        the rest of the resolver chain).
    """
    if cwd is None:
        cwd = Path.cwd()
    try:
        # Lazy import — vco_lib.project_config pulls in the hub-discovery
        # machinery which we don't want to spin up unless we're actually
        # going to consult the hub.
        from vco_lib.project_config import resolve as _vco_resolve  # type: ignore
        cfg = _vco_resolve(cwd)
        if cfg.code_graph_project:
            return cfg.code_graph_project
    except Exception as e:  # noqa: BLE001 — hub may be down; fall through to env
        logger.warning(
            "resolve_project_name: hub-resolver failed: %s; falling back to env", e
        )
    env_cg = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
    if env_cg:
        return env_cg
    env_pn = os.environ.get("PROJECT_NAME", "").strip()
    if env_pn:
        return env_pn
    return None
