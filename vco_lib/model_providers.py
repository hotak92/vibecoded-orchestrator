# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The machine-wide per-provider ON/OFF switch (v0.2.105).

WHY THIS MODULE EXISTS
----------------------
Agent definitions and model-routing guidance follow the vendor KEYS a machine
holds: ``vco_lib.module_gated_delivery.select_gateway_agents`` ships a
definition only when its vendor's key resolves, and
``vco_lib.model_selection.reachable_providers`` renders only vendors whose key
resolves. That answers "can this machine reach the vendor", never "does the
user still want it". The field case (owner, 2026-11-03): a lapsed Kimi
subscription — the agents stay delivered and the grid keeps advertising a
provider the user has stopped paying for, and no surface records the choice.

This module is that record: ONE machine-wide, per-vendor ``enabled`` /
``disabled`` setting.

Storage
-------
``launcher.db`` ``app_state`` row ``model_providers.disabled``: a JSON array of
the DISABLED vendor ids, sorted. Absence of the row (a fresh install, a
CLI-only machine, an unreadable DB) means "nothing disabled" — every provider
is enabled, which keeps this switch's default identical to the behaviour
before it existed. Only the off-list is stored, so a newly shipped vendor is
enabled without a migration.

A>B>C: rung A, one implementation
---------------------------------
The setting is consumed by PYTHON (the delivery gate, the guidance render,
and — v0.2.106 — the running gateway, which TTL-caches it so the hot path
never reads the DB per request) and written by the launcher. Rust does not
re-implement this encoding: the launcher's ``model_providers_status`` /
``model_providers_set`` commands shell
``python -m vco_lib.model_providers status|set --json`` (rung A of the
cross-language ladder, exactly like ``model_gateway_agents_gate`` beside
them). Reasons: (1) the read/write halves must agree on the encoding or one
of them silently mis-reads the row, and only ONE implementation can promise
that; (2) the setting is a user click, so a subprocess is free; (3) a Rust
mirror would be a second copy of a rule nothing can compile-check against
this one.

What a stored ``set`` does at once (v0.2.106)
---------------------------------------------
Until v0.2.106 a flipped switch took effect only on the next bundle update.
Now a stored change also: (1) parks (off) or restores (on) the vendor's
gateway agent definitions in the orchestrator root and every registered
project — :func:`apply_provider_switch`, through the bundle engine's own
``park_provider_agent`` / ``restore_parked_agents``; (2) refreshes the
CLAUDE.md model guidance (the launcher fires ``vco_lib.model_guidance
refresh`` on the same stored answer); (3) is honoured by the running gateway
within its short cache TTL — the vendor leaves ``/v1/models``, requests
naming its ids are refused locally, the quota fallback ladder skips it, and
``/vendor-keys`` reports it so the subagent pre-flight refuses its launches.

Soft-fail, in the conservative direction
----------------------------------------
Every read is soft: an unreadable DB, a missing table or a corrupt row answers
"nothing disabled". Disabling is a subtraction (agents parked, guidance rows
hidden), so a read failure must not fabricate one — a broken launcher DB must
never silently take a provider away. A WRITE that cannot be stored says so
(``set_provider_enabled`` returns ``stored=False``) rather than pretending.

Names only, never values
------------------------
The payload carries each vendor's declared ``secret_keys`` NAMES (public: they
are the registry's own strings) and a tri-state key state. A key VALUE never
enters this module's output — the probe it calls (``model_router.secrets``) is
the gateway's own, and only booleans/state names leave it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional
from urllib.parse import urlsplit

__all__ = [
    "APP_STATE_KEY",
    "ProviderError",
    "apply_provider_switch",
    "disabled_vendors",
    "enabled_vendors",
    "is_enabled",
    "provider_vendor_ids",
    "providers_payload",
    "set_provider_enabled",
    "status_payload",
    "main",
]

#: The ``app_state`` key holding the JSON array of DISABLED vendor ids.
APP_STATE_KEY = "model_providers.disabled"


class ProviderError(ValueError):
    """A request names something the vendor table does not know."""


def _launcher_db_path() -> Path:
    from vco_lib.paths import launcher_db_path

    return launcher_db_path()


def _warn_line(message: str) -> None:
    try:
        print(f"[vco] model-providers: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a log line must never raise
        pass


def _registry() -> Dict[str, Any]:
    """The gateway vendor table (``vendor_id -> Vendor``).

    Raises ``ImportError`` when the gateway package cannot be imported — the
    same "broken install" shape the delivery gate and the grid already have;
    callers decide what "cannot tell" means. The vendor LIST is never
    hard-coded here: adding a vendor is a row in ``model_router.vendors``.
    """
    from vco_lib.gateway_package import ensure_importable

    ensure_importable()
    from model_router.vendors import VENDORS  # pyright: ignore[reportMissingImports]

    return dict(VENDORS)


def provider_vendor_ids() -> List[str]:
    """Every vendor id the gateway registry knows, sorted.

    Raises ``ImportError`` when the gateway package is not importable (a
    broken install, never silently an empty list)."""
    return sorted(_registry())


def _parse_disabled(raw: Optional[str]) -> FrozenSet[str]:
    """The stored off-list as a frozenset of vendor ids.

    Anything unusable (no row, not JSON, not a list, non-string entries)
    answers the empty set — see the soft-fail rule in the module docstring.
    """
    if not raw:
        return frozenset()
    try:
        data = json.loads(raw)
    except ValueError:
        _warn_line(f"the stored provider switch ({APP_STATE_KEY}) is not JSON; "
                   "treating every provider as enabled")
        return frozenset()
    if not isinstance(data, list):
        _warn_line(f"the stored provider switch ({APP_STATE_KEY}) is not a "
                   "list; treating every provider as enabled")
        return frozenset()
    return frozenset(v for v in data if isinstance(v, str) and v)


def disabled_vendors(*, db_path: Optional[Path] = None) -> FrozenSet[str]:
    """The vendor ids switched OFF on this machine (empty = all enabled)."""
    from vco_lib.launcher_db_reader import read_app_state_value

    try:
        raw = read_app_state_value(APP_STATE_KEY, Path(db_path) if db_path else None)
    except Exception as exc:  # noqa: BLE001 — a read never disables a provider
        _warn_line(f"could not read the provider switch "
                   f"({type(exc).__name__}); treating every provider as enabled")
        return frozenset()
    return _parse_disabled(raw)


def enabled_vendors(*, db_path: Optional[Path] = None) -> FrozenSet[str]:
    """The registry's vendor ids minus the ones switched off.

    A vendor id the current registry no longer knows is NOT returned (it has
    no rows to render and no definitions to deliver). An unimportable gateway
    RAISES ``ImportError`` (from :func:`provider_vendor_ids`) — "cannot ask"
    is never answered as "nothing enabled".
    """
    off = disabled_vendors(db_path=db_path)
    return frozenset(v for v in provider_vendor_ids() if v not in off)


def is_enabled(vendor_id: str, *, db_path: Optional[Path] = None) -> bool:
    """True iff ``vendor_id`` is not on the machine's off-list."""
    return vendor_id not in disabled_vendors(db_path=db_path)


def _write_disabled(vendor_ids: Iterable[str], *, db_path: Optional[Path]) -> bool:
    """Store the off-list; ``True`` when the row was verifiably written."""
    from vco_lib.launcher_db_reader import read_app_state_value
    from vco_lib.launcher_db_writer import write_app_state_key

    target = Path(db_path) if db_path else _launcher_db_path()
    value = json.dumps(sorted(set(vendor_ids)))
    try:
        if not target.is_file():
            return False
        write_app_state_key(target, APP_STATE_KEY, value)
        # Read back: `write_app_state_key` is deliberately soft-fail (it backs
        # telemetry/cache), but this is a USER CHOICE — a switch that flips and
        # does not stick must say so instead of looking applied.
        return read_app_state_value(APP_STATE_KEY, target) == value
    except Exception:  # noqa: BLE001 — reported through the return value
        return False


def set_provider_enabled(
    vendor_id: str,
    enabled: bool,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """Switch one provider on or off machine-wide. Returns "stored".

    Raises :class:`ProviderError` for a vendor id the registry does not know
    (a typo must not silently create a setting nothing reads). ``False`` means
    the launcher DB could not be written — the caller reports it; nothing is
    half-applied.
    """
    known = provider_vendor_ids()
    if vendor_id not in known:
        raise ProviderError(
            f"unknown provider {vendor_id!r} (known: {', '.join(known)})")
    current = set(disabled_vendors(db_path=db_path))
    if enabled:
        current.discard(vendor_id)
    else:
        current.add(vendor_id)
    return _write_disabled(current, db_path=db_path)


# ---------------------------------------------------------------------------
# The immediate apply (v0.2.106): the agents follow the switch AT ONCE
# ---------------------------------------------------------------------------
#
# Until v0.2.106 the parked/restored copies moved only on the next bundle
# update (the engine's orphan walk calls ``park_provider_agent`` /
# ``restore_parked_agents``). The owner's ask (2026-11-10): "I should be able
# to manually enable/disable providers with the projects/gateway
# auto-updating on what I set." A stored ``set`` therefore calls
# :func:`apply_provider_switch`, which runs the SAME two functions against
# the orchestrator root and every registered project, so the CLI and the GUI
# (which shells the CLI) produce the same effect through one function.
#
# Soft-fail per project: one unreadable folder or manifest fails THAT row in
# the summary, never the run. The manifest is rewritten only when the apply
# actually changed it, byte-compatible with the engine's own writer.


def _install_root() -> Path:
    """The orchestrator root this module runs from.

    ``vco_lib`` is installed editable (``pip install -e .``), so the
    checkout's parent IS the install root — the same resolution
    ``vco_lib.gateway_package`` documents for the gateway package.
    """
    return Path(__file__).resolve().parent.parent


def _switch_targets(root: Path, db_path: Optional[Path]) -> tuple:
    """``([(display name, folder)], registry_error)`` — the orchestrator root
    first, then every registered project (the root deduplicated out of the
    rows). ``registry_error`` is non-empty when the project list could not be
    read (review C-L1): the apply then reaches the root only and must say so,
    never report "1 of 1" as if that were every project."""
    from vco_lib.project_move import registered_projects_or_none

    targets: List[tuple] = [("orchestrator root", root)]
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    rows = registered_projects_or_none(db_path)
    if rows is None:
        return targets, ("the launcher's project list could not be read, so only "
                         "the orchestrator root was reached; switch the provider "
                         "again once the launcher database is readable")
    for row in rows:
        folder = Path(str(row["folder_path"]))
        try:
            if folder.resolve() == root_resolved:
                continue
        except OSError:
            pass
        targets.append((str(row.get("name") or folder), folder))
    return targets, ""


def _read_project_manifest(folder: Path) -> tuple:
    """``(manifest, "")`` / ``(None, "absent")`` / ``(None, "corrupt …")``.

    Unlike the engine's ``_read_manifest`` (which collapses every miss into an
    empty manifest, the right posture for an unattended install), the apply is
    a USER-triggered action: a corrupt manifest is reported as a failed row,
    and a missing one as "not bundle-installed — nothing to park".
    """
    from vco_lib.manifest_paths import manifest_path

    target = manifest_path(folder)
    if not target.is_file():
        return None, "absent"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"corrupt ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "corrupt (not a JSON object)"
    return data, ""


def _write_project_manifest(folder: Path, manifest: dict) -> None:
    """Byte-compatible with the engine's writer. MUST MATCH
    ``vco_lib.project_init._write_manifest_atomic`` (indent 2, sorted keys,
    trailing newline) — that function is private to the engine, so the
    payload SHAPE is mirrored here and locked by
    ``tests/test_v02106_provider_switch_apply.py``."""
    from vco_lib.atomic import atomic_write_text
    from vco_lib.manifest_paths import manifest_path

    atomic_write_text(
        manifest_path(folder),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _row(name: str, folder: Path) -> dict:
    return {
        "project": name,
        "folder": str(folder),
        "status": "ok",
        "parked": [],
        "already_parked": [],
        "kept": [],
        "restored": [],
        "notes": [],
        "error": "",
    }


def _leaf(rel: str) -> str:
    return str(rel).replace("\\", "/").rsplit("/", 1)[-1]


def _held_outcomes(src_dir: Path, vendor_id: str) -> list:
    """The delivery gate's held outcomes when ONLY this vendor is off.

    Computed by :func:`select_gateway_agents` itself (the one home of "which
    definitions belong to a switched-off vendor"): every other vendor probes
    reachable, so only the disabled vendor's definitions are held, each with
    the ``SIGNAL_PROVIDER_DISABLED`` verdict :func:`park_provider_agent`
    keys on.
    """
    from vco_lib import module_gated_delivery as mgd

    verdict = mgd.GateVerdict(
        mgd.GateState.DELIVER, mgd.SIGNAL_MACHINE,
        "the provider switch's immediate apply")
    _deliver, held = mgd.select_gateway_agents(
        src_dir, verdict,
        vendor_probe=lambda _v: True,
        disabled_vendors=frozenset({vendor_id}),
    )
    return held


def _apply_off_to(
    name: str,
    folder: Path,
    vendor_id: str,
    outcomes: list,
) -> dict:
    """Park ``vendor_id``'s delivered definitions in one project."""
    from vco_lib import module_gated_delivery as mgd

    row = _row(name, folder)
    manifest, problem = _read_project_manifest(folder)
    if manifest is None:
        row["status"] = "skipped" if problem == "absent" else "failed"
        row["error"] = "" if problem == "absent" else f"manifest {problem}"
        if problem == "absent":
            row["notes"].append("no bundle manifest — nothing was delivered here")
        return row
    files = manifest.get("files")
    if not isinstance(files, dict):
        files = {}
    new_files = dict(files)
    moved: List[str] = []
    for rel, entry in files.items():
        result = mgd.park_provider_agent(
            folder, rel, entry if isinstance(entry, dict) else None, outcomes,
            log=row["notes"].append, warn=row["notes"].append)
        if result == "parked":
            row["parked"].append(_leaf(rel))
            moved.append(rel)
            new_files.pop(rel, None)
        elif result == "already-parked":
            row["already_parked"].append(_leaf(rel))
            new_files.pop(rel, None)
        elif result == "keep-entry":
            row["kept"].append(_leaf(rel))
    registry = mgd.parked_agents_registry(
        folder, manifest.get(mgd.PARKED_AGENTS_MANIFEST_KEY), moved, outcomes)
    if new_files == files and registry == (manifest.get(
            mgd.PARKED_AGENTS_MANIFEST_KEY) or {}):
        return row  # nothing changed: the manifest keeps its bytes
    manifest["files"] = dict(sorted(new_files.items()))
    if registry:
        manifest[mgd.PARKED_AGENTS_MANIFEST_KEY] = registry
    else:
        manifest.pop(mgd.PARKED_AGENTS_MANIFEST_KEY, None)
    try:
        _write_project_manifest(folder, manifest)
    except OSError as exc:
        row["status"] = "failed"
        row["error"] = (f"the definitions moved but the manifest write failed "
                        f"({type(exc).__name__}): {exc}")
    for key in ("parked", "already_parked", "kept"):
        row[key].sort()
    return row


def _apply_on_to(
    name: str,
    folder: Path,
    src_dir: Path,
    vendor_id: str,
    *,
    db_path: Optional[Path],
    held: bool,
    held_reason: str,
    gate: Dict[str, tuple],
) -> dict:
    """Restore what THIS switch parked of ``vendor_id`` in one project.

    ``gate`` is :func:`vco_lib.module_gated_delivery.restore_gate`'s answer
    for the vendor — the SAME rule the bundle update's restore applies, so a
    definition held here for a missing key is held there too (review C-M4).
    """
    from vco_lib import module_gated_delivery as mgd

    row = _row(name, folder)
    manifest, problem = _read_project_manifest(folder)
    if manifest is None:
        row["status"] = "skipped" if problem == "absent" else "failed"
        row["error"] = "" if problem == "absent" else f"manifest {problem}"
        return row
    prior = manifest.get(mgd.PARKED_AGENTS_MANIFEST_KEY)
    mine = {rel: v for rel, v in prior.items()
            if v == vendor_id and isinstance(rel, str)} \
        if isinstance(prior, dict) else {}
    if not mine:
        return row  # this switch parked nothing here
    if held:
        row["status"] = "held"
        row["notes"].append(
            f"left parked: {held_reason}; the definitions stay in "
            ".claude/agents.disabled/ until the key resolves")
        return row
    verdict = mgd.gateway_agents_gate(folder, db_path=db_path)
    if not verdict.delivers:
        row["status"] = "skipped"
        row["notes"].append(f"the project's module gate does not deliver: "
                            f"{verdict.reason}")
        return row
    restored = mgd.restore_parked_agents(
        folder, src_dir, {mgd.PARKED_AGENTS_MANIFEST_KEY: mine},
        verdict=verdict, log=row["notes"].append, gate=gate)
    files = manifest.get("files")
    if not isinstance(files, dict):
        files = {}
        manifest["files"] = files
    from vco_lib.hashing import sha256_file

    for rel in restored:
        try:
            digest = sha256_file(folder / rel)
        except OSError:
            continue  # moved but unreadable: the next update reconciles it
        files[rel] = {
            # The ON-DISK bytes (what the next update compares against), not
            # the current shipped hash — the parked copy predates any source
            # change since it was parked.
            "sha256": digest,
            "source": f"{mgd.GATEWAY_AGENTS_SOURCE_PREFIX}{_leaf(rel)}",
        }
    registry = mgd.parked_agents_registry(
        folder, prior, (), (), restored=restored)
    if registry:
        manifest[mgd.PARKED_AGENTS_MANIFEST_KEY] = registry
    else:
        manifest.pop(mgd.PARKED_AGENTS_MANIFEST_KEY, None)
    try:
        _write_project_manifest(folder, manifest)
    except OSError as exc:
        row["status"] = "failed"
        row["error"] = (f"the definitions moved but the manifest write failed "
                        f"({type(exc).__name__}): {exc}")
    row["restored"] = sorted(_leaf(rel) for rel in restored)
    return row


def apply_provider_switch(
    vendor_id: str,
    enabled: bool,
    *,
    db_path: Optional[Path] = None,
    orchestrator_root: Optional[Path] = None,
    vendor_probe: Any = None,
) -> dict:
    """Park (off) or restore (on) one vendor's gateway agent definitions in
    the orchestrator root and every registered project. Never raises.

    Runs the bundle engine's OWN park/restore functions
    (:func:`vco_lib.module_gated_delivery.park_provider_agent` /
    :func:`restore_parked_agents`) so a ``set`` and the next bundle update can
    never disagree about what a switch does. The restore leg respects the key
    gate: a vendor switched ON whose key does not resolve stays held (parked),
    exactly as the delivery gate would decide, and the summary's ``held`` /
    ``held_reason`` say so. The bundle update's restore applies the same gate
    (:func:`vco_lib.module_gated_delivery.restore_gate`, one rule), so held
    definitions come back by the first update — or switch flip — after the key
    resolves, never before.

    Returns the per-project summary the CLI payload and the GUI card render::

        {"vendor": "kimi", "enabled": false, "held": false, "held_reason": "",
         "parked_projects": 3, "restored_projects": 0, "failed_projects": 0,
         "projects": [{"project": "orchestrator root", "folder": "...",
                       "status": "ok" | "skipped" | "held" | "failed",
                       "parked": [...], "already_parked": [...], "kept": [...],
                       "restored": [...], "notes": [...], "error": ""}, ...]}
    """
    from vco_lib import module_gated_delivery as mgd

    root = Path(orchestrator_root) if orchestrator_root else _install_root()
    src_dir = root / "templates" / "agents" / mgd.GATEWAY_AGENTS_DIR
    summary: Dict[str, Any] = {
        "vendor": vendor_id,
        "enabled": enabled,
        "held": False,
        "held_reason": "",
        "parked_projects": 0,
        "restored_projects": 0,
        "failed_projects": 0,
        "registry_error": "",
        "projects": [],
    }
    targets, registry_error = _switch_targets(root, db_path)
    if registry_error:
        summary["registry_error"] = registry_error
        summary["failed_projects"] += 1
        _warn_line(registry_error)
    if not src_dir.is_dir():
        for name, folder in targets:
            row = _row(name, folder)
            row["status"] = "skipped"
            row["notes"].append(
                f"no shipped gateway definitions under {root} — nothing to do")
            summary["projects"].append(row)
        return summary

    gate: Dict[str, tuple] = {}
    if enabled:
        # The restore gate is machine-wide, so it is decided ONCE for the run
        # — by the same function the bundle update's restore uses. The
        # off-list is read from THIS run's database (review C-L2).
        gate = mgd.restore_gate(
            [vendor_id], disabled_vendors=disabled_vendors(db_path=db_path),
            vendor_probe=vendor_probe)
        key_ok, key_reason = gate.get(
            vendor_id, (False, "the key probe answered nothing"))
        summary["held"] = not key_ok
        summary["held_reason"] = "" if key_ok else key_reason

    outcomes = [] if enabled else _held_outcomes(src_dir, vendor_id)
    from vco_lib.atomic import LockTimeout
    from vco_lib.bundle_manifest_lock import APPLY_WAIT_S, bundle_manifest_lock

    for name, folder in targets:
        try:
            # Review C-M2: the bundle engine rewrites the same manifest; one
            # writer at a time (vco_lib.bundle_manifest_lock).
            with bundle_manifest_lock(folder, timeout_s=APPLY_WAIT_S):
                if enabled:
                    row = _apply_on_to(
                        name, folder, src_dir, vendor_id, db_path=db_path,
                        held=summary["held"], held_reason=summary["held_reason"],
                        gate=gate)
                else:
                    row = _apply_off_to(name, folder, vendor_id, outcomes)
        except LockTimeout:
            row = _row(name, folder)
            row["status"] = "failed"
            row["error"] = (
                "a bundle update is running in this project, so nothing was moved "
                "here; run Update bundle on it once that update finishes — the "
                "update applies the stored switch")
        except Exception as exc:  # noqa: BLE001 — one project fails, not the run
            row = _row(name, folder)
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
        if row["status"] == "failed":
            summary["failed_projects"] += 1
        if row["parked"]:
            summary["parked_projects"] += 1
        if row["restored"]:
            summary["restored_projects"] += 1
        summary["projects"].append(row)
        if row["error"]:
            _warn_line(f"{name}: {row['error']}")
    return summary


# ---------------------------------------------------------------------------
# The launcher's read (Home card)
# ---------------------------------------------------------------------------


def _key_state(vendor_id: str, probe: Any) -> str:
    """One vendor's key state: ``present`` / ``absent`` / ``unavailable`` /
    ``unknown`` (the gateway package could not be asked at all)."""
    if probe is None:
        return "unknown"
    try:
        state = probe(vendor_id)
    except Exception:  # noqa: BLE001 — one vendor, one answer, never a raise
        return "unavailable"
    return str(getattr(state, "value", state) or "unknown")


def providers_payload(
    *,
    db_path: Optional[Path] = None,
    key_state_probe: Any = None,
) -> dict:
    """Everything the Home "Model providers" card draws, per vendor.

    Shape::

        {
          "disabled": ["kimi"],            # the off-list, sorted
          "gateway_available": true,       # the vendor table could be read
          "providers": [
            {"vendor_id": "kimi", "display": "Kimi Code",
             "enabled": false, "key_state": "present",
             "secret_key_names": ["kimi_api_key", "KIMI_API_KEY"],
             "docs_url": "...", "definitions": ["kimi-implementer", ...]},
            ...
          ],
        }

    ``key_state`` is the live tri-state of that vendor's key — a NAME-level
    answer only; no key value is ever read out of the resolver by this module.
    ``definitions`` are the shipped gateway agent stems per vendor, from the
    same enumeration the delivery gate uses (one home).
    """
    try:
        registry = _registry()
    except ImportError as exc:
        return {
            "disabled": sorted(disabled_vendors(db_path=db_path)),
            "gateway_available": False,
            "reason": f"model gateway not importable ({type(exc).__name__})",
            "providers": [],
        }
    from model_router.vendors import vendor_display_name  # pyright: ignore[reportMissingImports]

    off = disabled_vendors(db_path=db_path)
    probe = key_state_probe
    if probe is None:
        try:
            from vco_lib.model_selection import vendor_key_state_probe

            probe = vendor_key_state_probe()
        except Exception:  # noqa: BLE001 — answered as "unknown", never raised
            probe = None
    definitions = _definitions_by_vendor()
    out: List[dict] = []
    for vendor_id in sorted(registry):
        vendor = registry[vendor_id]
        upstream = str(getattr(vendor, "upstream", "") or "")
        out.append({
            "vendor_id": vendor_id,
            "display": vendor_display_name(vendor),
            "enabled": vendor_id not in off,
            "key_state": _key_state(vendor_id, probe),
            # NAMES only — the registry's own spelling, never a value.
            "secret_key_names": list(getattr(vendor, "secret_keys", ()) or ()),
            "docs_url": str(getattr(vendor, "docs_url", "") or ""),
            # The mailbox host only, so the card can say where the key goes
            # without shipping a full endpoint as UI text.
            "upstream_host": urlsplit(upstream).netloc if upstream else "",
            "definitions": definitions.get(vendor_id, []),
        })
    return {
        "disabled": sorted(off),
        "gateway_available": True,
        "providers": out,
    }


def _definitions_by_vendor() -> Dict[str, List[str]]:
    """``{vendor_id: [definition stem, ...]}`` for the shipped gated set.

    Delegates to the delivery gate's own vendor resolution
    (:func:`vco_lib.module_gated_delivery.definition_vendor`), so the card and
    the bundle can never disagree about which definitions belong to a vendor.
    An unreadable/short shipped set answers ``{}``.
    """
    try:
        from vco_lib import module_gated_delivery as _mgd
    except Exception:  # noqa: BLE001 — the card degrades to "no definitions"
        return {}
    out: Dict[str, List[str]] = {}
    try:
        files = sorted(Path(_mgd.SHIPPED_GATEWAY_AGENTS).glob("*.md"))
    except OSError:
        return {}
    for path in files:
        try:
            vendor_id = _mgd.definition_vendor(path)
        except Exception:  # noqa: BLE001 — one unreadable file, no answer
            continue
        if vendor_id:
            out.setdefault(vendor_id, []).append(path.stem)
    return {v: sorted(names) for v, names in out.items()}


def status_payload(*, db_path: Optional[Path] = None,
                   key_state_probe: Any = None) -> dict:
    """The CLI/GUI read: :func:`providers_payload` plus the row it came from."""
    payload = providers_payload(db_path=db_path, key_state_probe=key_state_probe)
    payload["app_state_key"] = APP_STATE_KEY
    return payload


# ---------------------------------------------------------------------------
# CLI — the launcher's call surface (rule A: Rust/TS call this, never mirror)
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m vco_lib.model_providers status|set [--json] …``"""
    parser = argparse.ArgumentParser(
        prog="vco_lib.model_providers",
        description="Machine-wide per-provider on/off switch (launcher.db "
                    "app_state). Reads only for `status`; `set` writes one row.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="Print every provider with its key "
                                       "state (names only) and on/off state.")
    st.add_argument("--json", action="store_true")
    st.add_argument("--db-path", default=None, help="launcher.db override")
    se = sub.add_parser("set", help="Switch ONE provider on or off.")
    se.add_argument("--vendor", required=True, help="vendor id (e.g. kimi)")
    group = se.add_mutually_exclusive_group(required=True)
    group.add_argument("--enabled", dest="enabled", action="store_true")
    group.add_argument("--disabled", dest="enabled", action="store_false")
    se.add_argument("--json", action="store_true")
    se.add_argument("--db-path", default=None, help="launcher.db override")
    se.add_argument("--orchestrator-root", default=None,
                    help="install root whose templates/ are the source of the "
                         "agent definitions (default: the checkout this "
                         "vco_lib runs from)")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path) if args.db_path else None

    if args.command == "status":
        payload = status_payload(db_path=db_path)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        for row in payload["providers"]:
            state = "on" if row["enabled"] else "off"
            print(f"{row['display']} ({row['vendor_id']}): {state}, key "
                  f"{row['key_state']} — {', '.join(row['definitions']) or '-'}")
        return 0

    try:
        stored = set_provider_enabled(args.vendor, bool(args.enabled),
                                      db_path=db_path)
    except ProviderError as exc:
        print(f"[vco] model-providers: {exc}", file=sys.stderr)
        return 2
    payload = {"vendor": args.vendor, "enabled": bool(args.enabled),
               "stored": stored}
    if not stored:
        # A user choice that could not be stored is a FAILURE, never a silent
        # no-op: the GUI switch must not read back as applied.
        target = Path(db_path) if db_path else _launcher_db_path()
        cause = (f"no launcher.db at {target}" if not target.is_file()
                 else f"the write to {target} failed or did not read back")
        print(f"[vco] model-providers: the launcher database could not be "
              f"written ({cause}); the provider setting was NOT stored",
              file=sys.stderr)
    else:
        # v0.2.106: the agents follow the switch AT ONCE — the same run parks
        # (off) or restores (on) the vendor's definitions in the orchestrator
        # root and every registered project, through the bundle engine's own
        # park/restore functions. The launcher shells THIS command, so the GUI
        # toggle and a shell `set` produce the same effect through one
        # function; the payload's "agents" summary is what the card reports.
        payload["agents"] = apply_provider_switch(
            args.vendor, bool(args.enabled),
            db_path=db_path,
            orchestrator_root=(Path(args.orchestrator_root)
                               if args.orchestrator_root else None),
        )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{args.vendor}: {'enabled' if args.enabled else 'disabled'}"
              + ("" if stored else " (NOT stored)"))
        if stored:
            agents = payload["agents"]
            if agents["held"]:
                print(f"agent definitions stay parked: {agents['held_reason']}")
            else:
                verb = "restored" if args.enabled else "parked"
                count = (agents["restored_projects"] if args.enabled
                         else agents["parked_projects"])
                print(f"agent definitions {verb} in {count} of "
                      f"{len(agents['projects'])} project(s)")
                for row in agents["projects"]:
                    if row["status"] in ("failed", "held"):
                        print(f"  {row['project']}: {row['status']}"
                              + (f" — {row['error']}" if row["error"] else ""))
            # The launcher's toggle refreshes the CLAUDE.md guidance itself;
            # from a shell that is one more command.
            print("to refresh the model guidance in every project's CLAUDE.md "
                  "now, run: python -m vco_lib.model_guidance refresh "
                  "--orchestrator-root <install root> --trigger providers")
    return 0 if stored else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())