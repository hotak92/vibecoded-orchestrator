# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
""".vco-manifest.json schema + read/write (vco_lib.manifest — v0.2.53).

Consolidates the per-project manifest writer
(``project_init.py::_write_manifest_atomic``) and the orchestrator-self
manifest writer (``install.py::_refresh_orchestrator_self_vco_manifest``).

Per docs/INSTALL_ARCHITECTURE_v2.md §7.4.

v0.2.53 lands the module skeleton with the canonical types. Migrating
the two writers onto it is a v0.2.54 organisational refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ManifestEntry:
    """One file tracked by the manifest."""

    relative_path: str
    """Path relative to the project root, forward-slash separated
    (POSIX-style on all OSes for cross-platform stability)."""

    sha256: str
    """sha256 of the file content at install/update time."""

    source_template: Optional[str] = None
    """Templates path (when this file was rendered from a template).
    None for files that ship as-is (e.g. hooks scripts)."""

    user_modified: bool = False
    """True iff the on-disk file's hash differs from the manifest's
    `sha256` field. Computed at update time."""


@dataclass
class Manifest:
    """Versioned manifest for a project."""

    schema_version: int
    """Manifest schema version. Track in writer + reader so future
    changes can be detected."""

    vco_version: str
    """VCO version that wrote this manifest (e.g. "v0.2.53")."""

    written_at: str
    """ISO-8601 UTC timestamp of last write."""

    entries: Dict[str, ManifestEntry] = field(default_factory=dict)
    """Map from `relative_path` to :class:`ManifestEntry`."""


def read_manifest(path: Path) -> Optional[Manifest]:
    """Read a manifest from ``path``. Returns None on missing or invalid file.

    v0.2.53 STUB: implementation lands in v0.2.54.
    """
    raise NotImplementedError(
        "read_manifest is a v0.2.53 stub for v0.2.54 migration."
    )


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Atomically write ``manifest`` to ``path``.

    v0.2.53 STUB: implementation lands in v0.2.54.
    """
    raise NotImplementedError(
        "write_manifest is a v0.2.53 stub for v0.2.54 migration."
    )


def validate_manifest(data: dict) -> bool:
    """Validate a manifest dict against the schema.

    v0.2.53 STUB: implementation lands in v0.2.54.
    """
    raise NotImplementedError(
        "validate_manifest is a v0.2.53 stub for v0.2.54 migration."
    )
