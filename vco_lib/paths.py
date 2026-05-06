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
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"
