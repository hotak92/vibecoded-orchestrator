# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Loader for ``bundled_mcp_versions.toml`` (Phase 0 of the
diagrams-integration plan, 2026-05-24).

This module is the Python side of the cross-language pinning-manifest
loader. The Rust side lives at
``launcher/src-tauri/vct-launcher-core/src/bundled_versions.rs``; both
parse the SAME file with the SAME semantics so the install.py path and
the launcher path agree on what's pinned. A cross-language parity test
(``tests/test_bundled_versions_parity.py``) keeps them in lockstep —
same triangulation shape used for ``orchestrator-managed-paths.txt``.

The TOML file is plain stdlib `tomllib` (Python 3.11+ — install.py
already requires 3.11 via :data:`install.MIN_PYTHON`). No third-party
dependency is added.

Failure mode
------------
A missing or unreadable manifest is FATAL — raises ``RuntimeError`` with
a clear, recoverable message pointing the user at the file path and the
upstream repo. Silently falling back to a hard-coded default would
re-introduce the kind of two-language drift PR-5 was written to fix for
``ORCHESTRATOR_MANAGED_PATHS``.

Forward-compatibility
---------------------
The loader returns the parsed TOML as ``dict[str, dict[str, str]]``
unchanged — extra/unknown top-level sections or extra keys inside a
section are PRESERVED so a future bump (e.g. adding a ``[pip.*]`` table)
can land in the .toml without bumping the loader signature.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

# Path resolution mirrors install.py's ``_MANAGED_PATHS_FILE`` style:
# ``__file__`` → ``vco_lib/bundled_versions.py`` → parent.parent is the
# repo root, where the .toml sits next to install.py and the .txt
# allowlist. ``.resolve()`` so symlinks / relative-CWD invocations still
# land on the right file.
_DEFAULT_MANIFEST_PATH: Path = (
    Path(__file__).resolve().parent.parent / "bundled_mcp_versions.toml"
)


def manifest_path() -> Path:
    """Absolute path to the on-disk manifest. Exposed for diagnostics
    (e.g. error messages elsewhere in install.py that want to print
    where the loader was looking)."""
    return _DEFAULT_MANIFEST_PATH


def load_bundled_versions(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Parse ``bundled_mcp_versions.toml``.

    Args:
        path: Optional override for the manifest path. Defaults to the
            repo-root sibling of ``install.py`` (see ``manifest_path()``).
            Tests pass a tempfile path here; production code lets it
            default.

    Returns:
        Parsed TOML as a nested dict. The structure mirrors the .toml:

            {
              "npm": {
                "mermaid_mcp":     {"package": "...", "version": "...", "shasum": "..."},
                "excalidraw_mcp":  {...},
                "mermaid_lib":     {...},
                "excalidraw_lib":  {...},
              },
              "chromium": {"reuse_playwright": True},
            }

        Unknown sections and extra keys are preserved (forward-compat).

    Raises:
        RuntimeError: file missing or unreadable. The message names the
            absolute path it tried and points at the upstream repo so the
            user can recover by re-fetching.
        tomllib.TOMLDecodeError: malformed TOML. Propagated unwrapped so
            callers (and tests) get the exact stdlib parser error.
    """
    target = path if path is not None else _DEFAULT_MANIFEST_PATH
    try:
        raw_bytes = target.read_bytes()
    except OSError as e:
        raise RuntimeError(
            f"Could not read bundled-versions manifest at {target}: {e}. "
            f"This file pins the exact versions of external npm packages "
            f"(claude-mermaid, @excalidraw/excalidraw, ...) installed by "
            f"install.py. If you cloned the orchestrator repo correctly "
            f"the file should be present; otherwise re-fetch from "
            f"https://github.com/hotak92/vibecoded-orchestrator."
        ) from e

    # `tomllib.load(BinaryIO)` is the documented entry point; passing
    # bytes through BytesIO is documented too but we use `loads(str)` so
    # we can render decode errors with the source bytes in scope if
    # needed in future debugging.
    text = raw_bytes.decode("utf-8")
    parsed = tomllib.loads(text)
    return parsed
