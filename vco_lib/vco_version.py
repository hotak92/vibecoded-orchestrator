# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE resolver for "what version is this orchestrator?" (v0.2.92 WP-D).

Before this module there were four answers and none of them was a version:

* ``vco_lib/project_init.py::_resolve_vco_version`` — ``git rev-parse --short
  HEAD`` or ``"unknown"``.
* ``install.py::_bootstrap_resolve_vco_version`` — a ``VERSION`` file that does
  not exist in this repository, else the same short SHA.
* ``vco_lib/git_meta.py::resolve_vco_version`` — the same VERSION-then-SHA
  chain (kept for tarball installs that DO ship a VERSION file).
* ``install.py:1154`` — an inline ``git rev-parse`` spawn.

The consequence was structural, not cosmetic: ``.vco-manifest.json``'s
``vco_version`` field has always held a 7-char git SHA on every clone
install, while every consumer that compares it (the chunker-preset boundary
gate) expects semver. A SHA does not parse as semver, so the comparison
returned ``False`` on every real install — an inert gate wearing a version
gate's clothes (WP-A's skip-safety map, row 2). A SHA is an excellent
*commit* identifier and a useless *version*; this module returns them as
two separate fields so neither can be silently substituted for the other.

The semver source of truth is ``pyproject.toml [project] version`` — the
file ``scripts/bump-version.sh`` names as the head of the release pin set
(Cargo workspace, pyproject, package.json, tauri.conf.json, vct-module.json
all follow it). The commit comes from :func:`vco_lib.git_meta.git_short_sha`
(the ONE git-spawn home for read-only plumbing).

Never raises, never spawns anything but read-only git, never writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: ``X.Y.Z`` (optionally ``v``-prefixed). PEP 440 pre/post/dev suffixes are
#: deliberately NOT accepted: every consumer of this resolver feeds the value
#: to :func:`vco_lib.codegraph_extractor_generation.parse_semver`, and a shape
#: that parser rejects would recreate the SHA-as-version defect one level up.
_SEMVER_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


@dataclass(frozen=True)
class VcoVersion:
    """What this orchestrator install is, split by what each field is FOR.

    Attributes:
        semver: ``"0.2.92"``-shaped release version from ``pyproject.toml``,
            or ``None`` when it could not be determined (missing/unparseable
            pyproject). ``None`` is a real answer — consumers must treat it as
            UNKNOWN, never coerce it to a string and compare it.
        commit: short git SHA from :func:`vco_lib.git_meta.git_short_sha`, or
            ``None`` on a non-git tree (release tarball). A commit identifies
            a checkout, not a release: two commits inside one version window
            differ here and match on :attr:`semver`.
        source: how the answer was derived — ``"pyproject+git"``,
            ``"pyproject"``, ``"git"``, or ``"unknown"``. For diagnostics; a
            consumer that needs to reason about derivability reads this, not
            the ``None``-ness of the other fields.
    """

    semver: Optional[str]
    commit: Optional[str]
    source: str

    @property
    def display(self) -> str:
        """One human line: ``0.2.92 (c81f4fde)`` — semver first, commit in
        parens, ``unknown`` for whatever half is missing."""
        sem = self.semver or "unknown"
        cmt = f" ({self.commit})" if self.commit else ""
        return f"{sem}{cmt}"


def resolve(orchestrator_root: Path) -> VcoVersion:
    """Resolve the running orchestrator's version + commit. Never raises.

    ``orchestrator_root`` is the clone/tarball root (the directory holding
    ``pyproject.toml`` and ``vct-module.json``).
    """
    root = Path(orchestrator_root)
    semver = _semver_from_pyproject(root)
    commit: Optional[str] = None
    try:
        from vco_lib import git_meta

        commit = git_meta.git_short_sha(root)
    except Exception:  # noqa: BLE001 — version resolution never raises
        commit = None

    if semver is not None and commit is not None:
        source = "pyproject+git"
    elif semver is not None:
        source = "pyproject"
    elif commit is not None:
        source = "git"
    else:
        source = "unknown"
    return VcoVersion(semver=semver, commit=commit, source=source)


def _semver_from_pyproject(root: Path) -> Optional[str]:
    """Read ``[project] version`` out of ``root/pyproject.toml``.

    ``tomllib`` is stdlib on the Python floor this project pins (3.11+), so
    there is no dependency to fail. Any problem — missing file, missing key,
    unparseable TOML, non-semver value — is ``None``: unknown, never a guess.
    """
    pyproject = root / "pyproject.toml"
    try:
        import tomllib

        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        return None
    raw = (data.get("project") or {}).get("version")
    if not isinstance(raw, str):
        return None
    match = _SEMVER_RE.match(raw.strip())
    if match is None:
        return None
    return match.group(1)


def recorded_manifest_version(manifest: dict) -> tuple[Optional[str], Optional[str]]:
    """Read ``(version, commit)`` out of a ``.vco-manifest.json`` payload.

    ONE reader for the manifest's version fields, tolerant of every era:

    * **Current manifests** carry ``vco_version`` (semver) + ``vco_commit``
      (short SHA) — returned as-is.
    * **Legacy manifests** (everything before v0.2.92) carry only
      ``vco_version``, and its value is a git short SHA. A SHA is not a
      version: it is returned as the COMMIT with ``version=None``, so callers
      can never compare it as semver — the exact substitution this module
      exists to prevent, applied at read time rather than left to each
      consumer's diligence.
    * Missing/garbage fields → ``(None, None)``.

    A ``vco_version`` value that already parses as semver is returned as the
    version even when ``vco_commit`` is absent (partial writes, hand edits).
    """
    if not isinstance(manifest, dict):
        return (None, None)
    raw_version = manifest.get("vco_version")
    raw_commit = manifest.get("vco_commit")
    version: Optional[str] = None
    commit: Optional[str] = None
    if isinstance(raw_version, str):
        stripped = raw_version.strip()
        if stripped:
            match = _SEMVER_RE.match(stripped)
            if match is not None:
                version = match.group(1)
            else:
                # Legacy SHA-shaped value. 7-40 hex chars is a commit; anything
                # else is an unknown string and is dropped rather than guessed.
                if re.fullmatch(r"[0-9a-fA-F]{7,40}", stripped):
                    commit = stripped
    if isinstance(raw_commit, str) and raw_commit.strip():
        # The dedicated field wins for the commit half (a semver vco_version
        # plus a vco_commit is the canonical current shape).
        commit = raw_commit.strip()
    return (version, commit)
