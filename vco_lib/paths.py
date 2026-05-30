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

import os
from pathlib import Path
from typing import Optional


def vct_root_dir() -> Path:
    """Return the launcher's state-root directory.

    Resolution order:
      1. ``VCT_STATE_DIR`` env var (absolute path; not mkdir'd here).
      2. ``~/.vct/`` — production default.
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"


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
    except Exception:  # noqa: BLE001 — hub may be down; fall through to env
        pass
    env_cg = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
    if env_cg:
        return env_cg
    env_pn = os.environ.get("PROJECT_NAME", "").strip()
    if env_pn:
        return env_pn
    return None
