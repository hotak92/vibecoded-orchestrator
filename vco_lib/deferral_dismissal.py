# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Generalized dismissal memory for deferral conditions (v0.2.91 WP-B item 5).

ONE keying helper, one storage shape, for EVERY condition that supports
"dismiss this until the underlying state actually changes".

The problem it replaces
-----------------------
v0.2.83 gave exactly one condition (``template_review_pending``) a dismissal
memory, hand-implemented inside ``project_init``: dismissal snapshotted the
sha256 of each reference sidecar into the bundle manifest, and re-emission was
suppressed while every stored hash still matched. It worked — but it was a
BESPOKE code path. The obvious generalization ("dismiss until the entry's
``detected`` prose changes") was rejected on sight, and correctly: cosmetically
rewording a message would re-fire every user's dismissal.

The mechanism
-------------
The registry (:mod:`vco_lib.deferral_registry`) declares, per condition, an
ORDERED list of stable **field names** — the `dismiss_key`. The emitter supplies
the current VALUES for those fields on the entry
(:attr:`~vco_lib.deferral_report.DeferralEntry.dismiss_fields`). This module
computes ONE identity::

    key = sha256( cid \x1f name1=value1 \x1e name2=value2 … )

A dismissal holds while the KEY is unchanged. Prose is not an input, so
rewording can never re-fire a dismissal; a genuine state change (a new
reference shipped, the Ollama port pair changed, a different set of upstream
sidecars) changes a declared field and the nudge returns.

A condition with NO declared ``dismiss_key`` gets ``manual`` semantics: the
dismissal is recorded with ``key = "manual"`` and holds until the condition is
cleared by its clear_probe (or the user dismisses again). It NEVER falls back to
a prose hash.

Storage
-------
The bundle manifest ``<folder>/.claude/.vco-manifest.json``::

    "dismissals": {
      "<condition_id>": {
        "key": "sha256:…" | "manual",
        "fields": {"name": "value", …},     # human-diagnosable
        "dismissed_at": "2026-08-26T…Z",
        # legacy (pre-v0.2.91, template_review_pending only):
        "reference_hashes": {"CLAUDE.md": "…", …}
      }
    }

``schema_version`` stays 2 — the key is additive and optional, exactly as the
v0.2.83 addition was.

Back-compat is load-bearing: an EXISTING ``template_review_pending`` dismissal
that predates v0.2.91 carries only ``reference_hashes``. :func:`stored_key`
recomputes the modern key from that legacy payload, so upgrading VCO does not
silently un-dismiss a nudge the user already silenced (user state is never
destroyed by an update).

Everything here is best-effort and NEVER raises into a caller: a dismissal is a
convenience, and losing one must not break an install run.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from vco_lib.deferral_registry import dismiss_key_fields

#: Manifest path relative to a managed project folder. MUST match
#: ``vco_lib.project_init._MANIFEST_REL`` — asserted by
#: ``tests/test_deferral_dismissal_memory_v0291.py``.
MANIFEST_REL = Path(".claude") / ".vco-manifest.json"

#: Top-level manifest key holding every dismissal.
DISMISSALS_KEY = "dismissals"

#: The key value recorded for a condition with no declared dismiss_key.
MANUAL_KEY = "manual"

#: Field-name → legacy manifest key, for dismissals written before v0.2.91.
#: Only ``template_review_pending``'s v0.2.83 shape needs this.
_LEGACY_FIELD_KEYS: dict[str, str] = {"reference_hashes": "reference_hashes"}

_FIELD_SEP = "\x1e"
_CID_SEP = "\x1f"


def normalize_value(value: Any) -> str:
    """Canonical string form of one dismiss-key field value.

    Containers render as sorted-key compact JSON so ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` produce the SAME key; scalars render as ``str``. Stable
    across processes and platforms (no ``repr``, no hash randomisation).
    """
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, tuple):
            value = list(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def compute_key(condition_id: str, fields: Mapping[str, Any], *, path=None) -> str:
    """The dismissal identity for ``condition_id`` given current field values.

    Only fields the registry DECLARES are read, in the declared ORDER — an
    emitter that attaches extra fields cannot perturb the key, and a missing
    declared field contributes an empty value rather than raising (a partially
    populated entry still gets a stable, comparable identity).

    Returns :data:`MANUAL_KEY` when the condition declares no dismiss_key.
    """
    declared = dismiss_key_fields(condition_id, path=path)
    if not declared:
        return MANUAL_KEY
    parts = [f"{name}={normalize_value(fields.get(name))}" for name in declared]
    payload = f"{condition_id}{_CID_SEP}{_FIELD_SEP.join(parts)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _fallback_template_reference_hashes(folder: Path, _entry: Any) -> dict:
    """``template_review_pending`` fields, computed from disk.

    Lazy import: ``project_init`` owns the template→sidecar mapping (derived
    from ``_PROJECT_LEVEL_TEMPLATES``, so it cannot drift from the set the
    divergence check actually walks) and this must not become a second copy.
    """
    from vco_lib.project_init import _current_template_reference_hashes

    return {"reference_hashes": _current_template_reference_hashes(Path(folder))}


def _fallback_upstream_sidecars(_folder: Path, entry: Any) -> dict:
    """``orchestrator_user_modified_preserved`` fields, read off the entry.

    Shares ONE extractor with that condition's clear probe, so a dismissal is
    keyed on exactly the sidecar set whose disappearance would have cleared the
    entry anyway.
    """
    from vco_lib.deferral_probes import dismiss_fields_for_sidecars

    return dismiss_fields_for_sidecars(entry)


#: condition_id → provider computing its dismiss-key fields when the ENTRY does
#: not carry them.
#:
#: These are FIELD PROVIDERS, not alternative dismissal mechanisms — there is
#: still exactly one keying helper, one storage shape, one suppression rule.
#: They exist for two real cases:
#:   * an entry written by an older VCO (or by a Rust emitter over the Python
#:     bridge, which passes no dismiss_fields) — the values are recoverable
#:     from disk or from the entry's own recorded state;
#:   * an emitter that wants the suppression CHECK before it has built an entry.
#: A cid with no provider and no entry-carried fields simply gets a `manual`
#: dismissal, which is the honest outcome.
_FALLBACK_PROVIDERS = {
    "template_review_pending": _fallback_template_reference_hashes,
    "orchestrator_user_modified_preserved": _fallback_upstream_sidecars,
}


def fields_for(folder: Path, condition_id: str, entry: Any = None) -> dict:
    """Current dismiss-key values: entry-carried → fallback provider → ``{}``.

    Never raises; a provider that fails yields ``{}`` (which produces a stable
    key of its own, so a dismissal recorded that way still round-trips).
    """
    carried = getattr(entry, "dismiss_fields", None)
    if isinstance(carried, dict) and carried:
        return dict(carried)
    provider = _FALLBACK_PROVIDERS.get(condition_id)
    if provider is None:
        return {}
    try:
        return provider(Path(folder), entry)
    except Exception:  # noqa: BLE001 — a provider must never break a dismissal
        return {}


def _read_manifest(folder: Path) -> dict:
    """Read the bundle manifest through project_init's reader.

    Delegated (lazy import to avoid a module-level cycle: project_init imports
    THIS module) so there is exactly ONE manifest reader in the tree. A missing
    or corrupt manifest yields ``{}`` = no dismissals recorded.
    """
    try:
        from vco_lib.project_init import _read_manifest as _pi_read

        data = _pi_read(Path(folder))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a missing/corrupt manifest = no dismissals
        return {}


def _dismissal_entry(folder: Path, condition_id: str) -> Optional[dict]:
    dismissals = _read_manifest(folder).get(DISMISSALS_KEY)
    if not isinstance(dismissals, dict):
        return None
    entry = dismissals.get(condition_id)
    return entry if isinstance(entry, dict) else None


def stored_key(folder: Path, condition_id: str, *, path=None) -> Optional[str]:
    """The recorded dismissal identity, or ``None`` when nothing is recorded.

    Migration arm: a pre-v0.2.91 dismissal carries only its legacy payload
    (``reference_hashes``). Rather than rewrite the manifest on read, the key is
    RECOMPUTED from that payload so a user's existing dismissal keeps
    suppressing across the upgrade. The next dismissal writes the modern shape.
    """
    entry = _dismissal_entry(folder, condition_id)
    if entry is None:
        return None
    key = entry.get("key")
    if isinstance(key, str) and key:
        return key

    declared = dismiss_key_fields(condition_id, path=path)
    if not declared:
        # Pre-v0.2.91 dismissal of a cid with no declared key: honour it as a
        # manual dismissal rather than discarding the user's choice.
        return MANUAL_KEY
    legacy_fields: dict[str, Any] = {}
    for name in declared:
        legacy_name = _LEGACY_FIELD_KEYS.get(name)
        if legacy_name is not None and legacy_name in entry:
            legacy_fields[name] = entry[legacy_name]
    if not legacy_fields:
        # Recorded, but in a shape we cannot map — treat as no dismissal rather
        # than guessing (a wrong key would suppress a nudge forever).
        return None
    return compute_key(condition_id, legacy_fields, path=path)


def dismissal_suppresses(
    folder: Path,
    condition_id: str,
    current_fields: Optional[Mapping[str, Any]] = None,
    *,
    path=None,
) -> bool:
    """True when a recorded dismissal still covers the CURRENT state.

    * No dismissal recorded ⇒ ``False`` (never suppress).
    * Declared dismiss_key ⇒ suppress only while the recomputed key matches.
    * No declared key (``manual``) ⇒ suppress while the manual dismissal stands.

    Never raises: any I/O or parse problem yields ``False`` (emit), because
    failing to suppress is a nuisance while failing to emit hides real state.
    """
    try:
        recorded = stored_key(folder, condition_id, path=path)
        if recorded is None:
            return False
        current = compute_key(condition_id, current_fields or {}, path=path)
        return recorded == current
    except Exception:  # noqa: BLE001 — suppression is best-effort
        return False


def record_dismissal(
    folder: Path,
    condition_id: str,
    current_fields: Optional[Mapping[str, Any]] = None,
    *,
    path=None,
) -> bool:
    """Snapshot the dismissal identity into the manifest. Returns success.

    Best-effort AND SILENT: called after a dismissal already succeeded, and the
    ``dismiss-deferral`` command's JSON-stdout contract must stay byte-stable,
    so nothing is printed and nothing propagates. A missing manifest is created;
    a corrupt one is replaced with a minimal shell carrying only the dismissal
    (``files``/``preserved_files`` rebuild on the next install run).
    """
    try:
        # Delegated writer (lazy import — project_init imports THIS module):
        # ONE manifest serializer in the tree, so the on-disk byte shape
        # (indent=2, sort_keys=True, trailing newline) can never fork.
        from vco_lib.project_init import (
            _MANIFEST_SCHEMA_VERSION,
            _write_manifest_atomic,
        )

        manifest = _read_manifest(folder)
        dismissals = manifest.get(DISMISSALS_KEY)
        if not isinstance(dismissals, dict):
            dismissals = {}
        fields = {
            name: normalize_value((current_fields or {}).get(name))
            for name in dismiss_key_fields(condition_id, path=path)
        }
        dismissals[condition_id] = {
            "key": compute_key(condition_id, current_fields or {}, path=path),
            "fields": fields,
            "dismissed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        manifest[DISMISSALS_KEY] = dismissals
        manifest.setdefault("schema_version", _MANIFEST_SCHEMA_VERSION)
        _write_manifest_atomic(Path(folder), manifest)
        return True
    except Exception:  # noqa: BLE001 — dismissal memory is best-effort
        return False


__all__ = [
    "DISMISSALS_KEY",
    "MANIFEST_REL",
    "MANUAL_KEY",
    "compute_key",
    "dismissal_suppresses",
    "fields_for",
    "normalize_value",
    "record_dismissal",
    "stored_key",
]
