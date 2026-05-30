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

    Equivalent to ``vct_root_dir() / "launcher.db"`` — a convenience for
    the many callers across ``install.py`` / ``vco_lib/diagram_indexer.py``
    / ``vco_lib/project_init.py`` that always want the launcher.db file
    rather than the state-root directory.

    Resolution rules inherit from :func:`vct_root_dir` — honours
    ``$VCT_STATE_DIR`` first, falls back to ``~/.vct/launcher.db``.
    """
    return vct_root_dir() / "launcher.db"
