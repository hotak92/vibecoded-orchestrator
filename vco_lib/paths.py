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
