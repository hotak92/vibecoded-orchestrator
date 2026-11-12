# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Did VCO create this ``.claude/settings.json`` — and is it still what VCO wrote?

v0.2.106 (macOS field run): run 1 of a fresh install stopped after step 5b's
bundle had CREATED the root's ``settings.json`` from the template but before
step 9 stamped the ``_vco_managed_keys`` sentinel. Run 2's step 9 found a
sentinel-less file, and the only signal it had — the bundle's envelope of the
CURRENT process (:func:`vco_lib.self_install.root_settings_created_this_run`)
— said nothing, so VCO's own file was treated as user-owned (``.vco-new`` +
an ``action_required`` deferral that pointed at ``--adopt-project-replace-all``).

The fact "VCO created this file" now persists in the record VCO already keeps
for the files it installs, the bundle manifest (``.claude/.vco-manifest.json``),
as the additive key :data:`MANIFEST_KEY`: ``{"fingerprint": <sha256>}``.

What "still what VCO wrote" means — the fingerprint
---------------------------------------------------
The fingerprint is a SHA-256 of the parsed file with its ``env`` block left
out (:func:`fingerprint`). ``env`` is excluded because the canonical env
writers — the config projection (``apply_project_env``), install.py's root
env backfill, the machine-key move to ``settings.local.json`` — legitimately
rewrite it between the bundle's create and step 9, and on every later run; a
whole-file hash would make VCO's own writes look like a user edit. Leaving
``env`` out costs nothing on the adoption side: step 9's adoption is
fill-only (an env value already in the file wins), so a user's own env edit
survives adoption anyway. Every other key (``hooks``, ``permissions``, any key
the user added) is covered, and parsing first makes the comparison immune to
whitespace/key-order differences between writers.

Lifecycle (the bundle carries it, :func:`next_record`):

* the bundle CREATES the file → record its fingerprint (taken after the
  bundle's last write, at manifest-write time);
* a prior record whose fingerprint still matches the file as this run found
  it → carried forward with the fingerprint after this run's writes (a
  bundle merge of new template hooks keeps the file VCO's);
* anything else (no record, a user edit, an unreadable / JSONC file) → no
  record. Once step 9 stamps the sentinel, the fingerprint no longer matches
  and the next bundle run drops the record by itself — the sentinel is the
  durable marker from then on.

A record is a claim, never a guess from the file's shape: an older root whose
user-edited file happens to carry the template keys has no record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from vco_lib.hashing import sha256_bytes
from vco_lib.manifest_paths import manifest_path

#: The additive manifest key (schema_version unchanged — same precedent as
#: ``packs`` / ``dismissals``).
MANIFEST_KEY = "settings_created_by_vco"

#: The top-level key the canonical env writers own; left out of the fingerprint.
_ENV_KEY = "env"


def fingerprint(settings: dict) -> str:
    """SHA-256 of ``settings`` minus its ``env`` block, over a canonical
    (sorted-key, compact) JSON rendering."""
    body = {k: v for k, v in settings.items() if k != _ENV_KEY}
    rendered = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(rendered.encode("utf-8"))


def file_fingerprint(path: Path) -> Optional[str]:
    """:func:`fingerprint` of the strict-JSON object at ``path``; ``None`` when
    the file is absent, unreadable, not strict JSON (VCO never writes JSONC)
    or not an object — i.e. when it cannot be shown to be VCO's."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return fingerprint(data) if isinstance(data, dict) else None


def _recorded_fingerprint(record: Any) -> Optional[str]:
    if isinstance(record, dict):
        value = record.get("fingerprint")
        if isinstance(value, str) and value:
            return value
    return None


def next_record(prior: Any, *, settings_action: str,
                fingerprint_before: Optional[str],
                fingerprint_after: Optional[str]) -> Optional[dict]:
    """The manifest record after a bundle run (pure).

    ``settings_action`` is the bundle's own status for the settings step;
    ``fingerprint_before`` the file as this run found it (before the settings
    merge), ``fingerprint_after`` the file at manifest-write time.
    """
    if fingerprint_after is None:
        return None
    if settings_action == "created":
        return {"fingerprint": fingerprint_after}
    recorded = _recorded_fingerprint(prior)
    if recorded is not None and fingerprint_before == recorded:
        return {"fingerprint": fingerprint_after}
    return None


def _settings_path(project_root: Path) -> Path:
    return Path(project_root) / ".claude" / "settings.json"


def fingerprint_before(project_root: Path) -> Optional[str]:
    """The bundle's "as this run FOUND it" fingerprint, taken before its
    settings merge (``install_project_bundle``)."""
    return file_fingerprint(_settings_path(project_root))


def carry_record(payload: dict, prior_manifest: Any, project_root: Path,
                 settings_action: Any, before: Optional[str]) -> None:
    """Put this run's :func:`next_record` into the manifest ``payload`` the
    bundle is about to write (called right before that write, so the "after"
    fingerprint follows the bundle's last write). No record → no key."""
    prior = prior_manifest.get(MANIFEST_KEY) if isinstance(prior_manifest, dict) else None
    record = next_record(prior, settings_action=str(settings_action or ""),
                         fingerprint_before=before,
                         fingerprint_after=file_fingerprint(_settings_path(project_root)))
    if record is not None:
        payload[MANIFEST_KEY] = record


def created_by_vco_and_unchanged(project_root: Path) -> bool:
    """True iff the bundle manifest records that VCO created
    ``<project_root>/.claude/settings.json`` AND the file still fingerprints
    to what VCO last wrote (``env`` aside). Any read failure → ``False`` (the
    user-owned path is the conservative default)."""
    root = Path(project_root)
    try:
        manifest = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    recorded = _recorded_fingerprint(manifest.get(MANIFEST_KEY))
    if recorded is None:
        return False
    return file_fingerprint(_settings_path(root)) == recorded
