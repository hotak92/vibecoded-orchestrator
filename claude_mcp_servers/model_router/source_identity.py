# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE definition of "which gateway source is this".

Why this module exists
----------------------
The gateway is a long-lived Python process started from an EDITABLE install
(``pip install -e claude_mcp_servers/``), so an orchestrator update rewrites
the files it was started from while the process keeps serving the code it
loaded. Nothing restarts it — deliberately: the VS Code panel routes every
chat through it, and a restart kills live agent sessions. Field proof: a
gateway up 14 h across the 0.2.96 update kept serving 0.2.95's catalog logic
until a manual restart.

Whether the running gateway is behind its checkout therefore has to be a
FACT, not a guess from timestamps, and this is where that fact comes from:

* the **daemon** hashes its own package directory once, at import, and
  reports the digest as ``/health.source_sha`` — it hashes the files it was
  started from, so it cannot overstate its freshness;
* the **host** (:mod:`vco_lib.gateway_freshness`) loads THIS file from the
  checkout by path and hashes the checkout's package directory.

One rule, one home: both sides run the same function, so there is no mirror
to drift (CLAUDE.md A>B>C, rule A). Like ``__init__``, this module imports
nothing beyond the stdlib — the host loads it without importing the package.

Covered surface (stated exactly, so the guarantee is not overstated): every
``*.py`` and ``*.json`` file directly inside the ``model_router`` package —
the routing, catalog and context-table logic plus the shipped data files it
reads at start. NOT covered: ``vco_lib`` modules the daemon imports. A release
always changes ``__version__`` (bumped and gated by
``scripts/check-version-pins.sh``), and ``__init__.py`` is inside the hashed
set, so every release is covered; what is not is a ``vco_lib``-only change
between two commits of the same version.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

#: Bumped when the HASHING RULE changes (not when the hashed files do). Part of
#: the digest, so a daemon hashing under an older rule never compares equal to
#: a host hashing under a newer one — and a rule change IS a code change.
SOURCE_SCHEME = "vco-model-gateway-source-v1"

#: Which files count. Suffixes, not names, so a module added to the package is
#: covered the day it lands instead of the day someone remembers a list.
SOURCE_SUFFIXES = (".py", ".json")

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def source_files(package_dir) -> Optional[list[Path]]:
    """The hashed files in ``package_dir``, sorted by name; ``None`` if unreadable."""
    base = Path(package_dir)
    try:
        return sorted(
            (p for p in base.iterdir() if p.is_file() and p.suffix in SOURCE_SUFFIXES),
            key=lambda p: p.name,
        )
    except OSError:
        return None


def source_sha(package_dir) -> Optional[str]:
    """sha256 over :func:`source_files` of ``package_dir``, or ``None``.

    ``None`` means "could not look" — a missing directory, an unreadable file,
    or a directory with no source in it — and every caller renders it as
    *unknown*, never as *current*. A digest that silently skipped a file could
    compare equal across a real change.

    Each file's name and byte length are folded in before its bytes, so moving
    content between two files cannot produce the same digest.
    """
    files = source_files(package_dir)
    if not files:
        return None
    digest = hashlib.sha256()
    digest.update(SOURCE_SCHEME.encode("utf-8"))
    digest.update(b"\0")
    for path in files:
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
    return digest.hexdigest()


def package_version(package_dir) -> Optional[str]:
    """``__version__`` as written in ``package_dir/__init__.py``, or ``None``.

    Read as text rather than imported: the host asks about a checkout whose
    package it must not import (that would be the running interpreter's copy,
    not necessarily the checkout's).
    """
    try:
        text = (Path(package_dir) / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


__all__ = [
    "SOURCE_SCHEME",
    "SOURCE_SUFFIXES",
    "package_version",
    "source_files",
    "source_sha",
]
