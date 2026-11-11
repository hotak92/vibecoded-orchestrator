# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundled-weights layout: which trained checkpoint ships for which profile.

The RL reranker's trained weights are committed to this repository and baked
into the container image built from it. This module is the ONE reader of that
layout; the server (``rl_server``), the image build (``Dockerfile`` runs
``python -m rl_reranker.bundled_weights verify``) and the repository test that
pins the shipped files all go through it.

Layout (relative to the weights root — ``claude_mcp_servers/rl_reranker/weights``
in the checkout, ``/app/weights`` inside the image)::

    manifest.json
    <profile>/<version>/rl_model_<profile>_<version>.pt

``manifest.json``::

    {
      "schema_version": 1,
      "profiles": {
        "qwen3":  {"current": "0.3.0",
                   "versions": {"0.3.0": {"sha256": "<64 hex>"}}},
        "arctic": {"current": null, "versions": {}}
      }
    }

``<profile>`` is the embedding profile name the server resolves from
``ACTIVE_EMBEDDING`` (``qwen3``, ``arctic``). ``current`` names the version the
image serves for that profile; ``null`` means no trained model ships for it and
the server serves "not trained" (fresh weights, never reported as trained).
The checkpoint file name matches ``rl_server._build_weights_filename`` so a
seeded copy in ``state/`` is indistinguishable from any other versioned
checkpoint.

Failure policy:

* no weights root, or no ``manifest.json`` in it, or a profile the manifest
  does not list, or ``current: null`` → :func:`resolve_bundled_weights` returns
  ``None`` ("not trained"). That is a legitimate state, not an error.
* a manifest that is present but wrong — unparseable, an unknown schema, a
  ``current`` that names no listed version, a listed file that is missing, a
  sha256 that does not match — raises :class:`BundledWeightsError`. That is a
  broken checkout or image, and it must be loud: serving "not trained" there
  would hide a packaging bug behind a plausible state.

This module imports only the standard library so the image can run it in the
build stage before anything else is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

#: Name of the manifest file at the weights root.
MANIFEST_NAME = "manifest.json"

#: The manifest schema this reader understands.
MANIFEST_SCHEMA_VERSION = 1

#: Profiles the shipped manifest must declare (the owner rule: qwen3 and
#: arctic ship together). A declared profile may still have ``current: null``.
SHIPPED_PROFILES: tuple[str, ...] = ("qwen3", "arctic")

# A profile name becomes a directory and a filename fragment; a version
# becomes both too. Keep both to a conservative alphabet so a manifest can
# never address a path outside its own profile directory.
_PROFILE_RE = re.compile(r"^[a-z0-9_]+$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BundledWeightsError(ValueError):
    """The bundled-weights manifest is present but inconsistent."""


@dataclass(frozen=True)
class BundledWeights:
    """The checkpoint the image ships for one profile."""

    profile: str
    version: str
    path: Path
    sha256: str


def weights_relpath(profile: str, version: str) -> PurePosixPath:
    """Return ``<profile>/<version>/rl_model_<profile>_<version>.pt``."""
    _check_profile(profile)
    _check_version(version)
    return PurePosixPath(profile, version, f"rl_model_{profile}_{version}.pt")


def _check_profile(profile: str) -> None:
    if not isinstance(profile, str) or not _PROFILE_RE.match(profile):
        raise BundledWeightsError(f"invalid profile name {profile!r}")


def _check_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise BundledWeightsError(f"invalid weights version {version!r}")


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, streamed."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(weights_root: Optional[Path]) -> Optional[dict[str, Any]]:
    """Parse and validate ``<weights_root>/manifest.json``.

    Returns ``None`` when there is no weights root or no manifest in it.
    Raises :class:`BundledWeightsError` when the manifest exists but is not a
    valid schema-1 document (file existence and hashes are checked by
    :func:`resolve_bundled_weights` / :func:`verify`, not here).
    """
    if weights_root is None:
        return None
    manifest_path = Path(weights_root) / MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundledWeightsError(f"{manifest_path}: unreadable manifest: {exc}") from exc
    if not isinstance(doc, dict):
        raise BundledWeightsError(f"{manifest_path}: manifest must be a JSON object")
    if doc.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BundledWeightsError(
            f"{manifest_path}: unsupported schema_version "
            f"{doc.get('schema_version')!r} (expected {MANIFEST_SCHEMA_VERSION})"
        )
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict):
        raise BundledWeightsError(f"{manifest_path}: 'profiles' must be an object")
    for profile, entry in profiles.items():
        _check_profile(profile)
        if not isinstance(entry, dict):
            raise BundledWeightsError(f"{manifest_path}: profile {profile!r} must be an object")
        versions = entry.get("versions", {})
        if not isinstance(versions, dict):
            raise BundledWeightsError(
                f"{manifest_path}: profile {profile!r}: 'versions' must be an object"
            )
        for version, meta in versions.items():
            _check_version(version)
            if not isinstance(meta, dict) or not _SHA256_RE.match(str(meta.get("sha256", ""))):
                raise BundledWeightsError(
                    f"{manifest_path}: {profile} {version}: 'sha256' must be 64 lowercase hex"
                )
        current = entry.get("current")
        if current is not None:
            _check_version(current)
            if current not in versions:
                raise BundledWeightsError(
                    f"{manifest_path}: profile {profile!r} current={current!r} "
                    f"is not one of its listed versions {sorted(versions)}"
                )
    return doc


def resolve_bundled_weights(
    weights_root: Optional[Path],
    profile: str,
    *,
    verify_sha: bool = True,
) -> Optional[BundledWeights]:
    """Return the checkpoint shipped for ``profile``, or ``None`` if none ships.

    See the module docstring for when this returns ``None`` and when it raises.
    """
    doc = load_manifest(weights_root)
    if doc is None or weights_root is None:
        return None
    entry = doc["profiles"].get(profile)
    if entry is None or entry.get("current") is None:
        return None
    version = entry["current"]
    expected_sha = entry["versions"][version]["sha256"]
    path = Path(weights_root) / Path(*weights_relpath(profile, version).parts)
    if not path.is_file():
        raise BundledWeightsError(
            f"bundled weights for {profile} {version} are listed in the manifest "
            f"but missing on disk: {path}"
        )
    if verify_sha:
        actual = sha256_file(path)
        if actual != expected_sha:
            raise BundledWeightsError(
                f"bundled weights {path}: sha256 {actual} does not match the "
                f"manifest's {expected_sha}"
            )
    return BundledWeights(profile=profile, version=version, path=path, sha256=expected_sha)


def verify(weights_root: Path) -> dict[str, Optional[str]]:
    """Check every LISTED version of every profile (not just ``current``).

    Returns ``{profile: current_version_or_None}``. Raises
    :class:`BundledWeightsError` on any inconsistency, including a missing
    manifest and a missing shipped profile — the image and the repository
    always carry the manifest, so its absence there is a packaging bug.
    """
    doc = load_manifest(weights_root)
    if doc is None:
        raise BundledWeightsError(f"{Path(weights_root) / MANIFEST_NAME}: manifest missing")
    profiles = doc["profiles"]
    missing = [p for p in SHIPPED_PROFILES if p not in profiles]
    if missing:
        raise BundledWeightsError(f"manifest does not declare shipped profile(s) {missing}")
    out: dict[str, Optional[str]] = {}
    for profile, entry in profiles.items():
        for version, meta in entry.get("versions", {}).items():
            path = Path(weights_root) / Path(*weights_relpath(profile, version).parts)
            if not path.is_file():
                raise BundledWeightsError(f"{profile} {version}: file missing: {path}")
            actual = sha256_file(path)
            if actual != meta["sha256"]:
                raise BundledWeightsError(
                    f"{profile} {version}: sha256 {actual} != manifest {meta['sha256']}"
                )
        out[profile] = entry.get("current")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rl_reranker.bundled_weights",
        description="Inspect or verify the bundled RL reranker weights layout.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_verify = sub.add_parser("verify", help="check the manifest and every listed file")
    p_verify.add_argument("weights_root", type=Path)
    p_show = sub.add_parser("show", help="print the shipped checkpoint for one profile")
    p_show.add_argument("weights_root", type=Path)
    p_show.add_argument("profile")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "verify":
            result = verify(args.weights_root)
            print(json.dumps({"ok": True, "current": result}, sort_keys=True))
            return 0
        bundled = resolve_bundled_weights(args.weights_root, args.profile)
        print(json.dumps({
            "profile": args.profile,
            "trained": bundled is not None,
            "version": bundled.version if bundled else None,
            "path": str(bundled.path) if bundled else None,
        }, sort_keys=True))
        return 0
    except BundledWeightsError as exc:
        print(f"bundled weights: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
