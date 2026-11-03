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
The setting is consumed by PYTHON (the delivery gate and the guidance render)
and written by the launcher. Rust does not re-implement this encoding: the
launcher's ``model_providers_status`` / ``model_providers_set`` commands shell
``python -m vco_lib.model_providers status|set --json`` (rung A of the
cross-language ladder, exactly like ``model_gateway_agents_gate`` beside them).
Reasons: (1) the read/write halves must agree on the encoding or one of them
silently mis-reads the row, and only ONE implementation can promise that;
(2) the setting is a user click, so a subprocess is free; (3) a Rust mirror
would be a second copy of a rule nothing can compile-check against this one.

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
    """A request names something the vendor table does not known."""


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
    no rows to render and no definitions to deliver); an unimportable gateway
    answers ``None``'s sibling — the empty set — because no vendor can be
    named. Callers that must tell "nothing enabled" from "cannot ask" use
    :func:`provider_vendor_ids` (which raises) plus :func:`disabled_vendors`.
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
        print("[vco] model-providers: the launcher database could not be "
              f"written (no launcher.db at {db_path or _launcher_db_path()}); "
              "the provider setting was NOT stored", file=sys.stderr)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{args.vendor}: {'enabled' if args.enabled else 'disabled'}"
              + ("" if stored else " (NOT stored)"))
    return 0 if stored else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())