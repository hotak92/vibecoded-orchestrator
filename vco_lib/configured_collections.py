# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — create a positively-absent configured collection at
add/update time.

Extracted from ``vco_lib.project_init`` in the v0.2.105 fix round per the
file-size rule (``project_init.py`` was already past 14 000 lines when the
~96-line ensure logic landed as one contiguous block). One concern, one
home: this module owns the CREATE side of the v0.2.92 F-12(b)
configured-collection probe. The probe itself
(:func:`vco_lib.project_init.verify_configured_collections_exist`) stays in
``project_init`` — it is the evidence layer the standalone warning path
shares; ``project_init.ensure_configured_collections`` is a thin shim that
delegates here so the historical import path stays stable.

``vco_lib.project_init`` is imported lazily inside the function body: this
module must be importable from ``project_init``'s shim without an import
cycle, and nothing here is needed at ``project_init`` import time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

__all__ = ["configured_absent_warning_prefix", "ensure_configured_collections"]


def configured_absent_warning_prefix(name: str) -> str:
    """The identifying prefix of the F-12(b) "configured collection absent"
    warning for *name* — ONE home for the warning's shape (v0.2.105 fix
    round, review NIT). It lives here, beside the create side that retires
    the warning, rather than in ``project_init`` (whose line ratchet is at
    its measured floor).

    :func:`vco_lib.project_init.verify_configured_collections_exist` builds
    its warning starting with exactly this text, and
    :func:`ensure_configured_collections` retires the warning of a class it
    CREATED by matching this prefix — never by a bare ``name in w``
    substring test, which also dropped a DIFFERENT collection's warning
    whenever that name merely contained this one (the shared-collection
    warning this fix round says must STAND, e.g. creating
    ``X_KnowledgeGraph`` used to silence ``X_KnowledgeGraph_Shared``)."""
    return f"configured collection {name!r} ("


def ensure_configured_collections(
    folder: Path,
    weaviate_url: Optional[str] = None,
    *,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """v0.2.105: CREATE a positively-absent configured class — for the
    project's OWN collections only.

    The v0.2.92 F-12(b) probe
    (:func:`vco_lib.project_init.verify_configured_collections_exist`)
    only WARNED that a class named in ``.claude/settings.json`` ``env`` was
    absent from Weaviate and printed a manual ``bootstrap-collections``
    command — but a bundle install/update is exactly the moment the missing
    class should come into being, and an EMPTY class with the shipped
    schema is not destructive. Until this, a project whose configured KG
    class had never been created got "every KG query returns nothing" from
    every session, and the update report told the user to run a command
    instead of running it.

    Strictly additive, same safety shape as ``bootstrap_collections``:

    * creation runs ONLY for a name the probe answered **absent** — never
      for ``unknown`` (Weaviate unreachable is not evidence of absence,
      F-1's lesson) and never for a class that exists (``_create_class``
      is itself idempotent on an existing name);
    * the definitions POSTed are the SAME ``kg_class_definition`` /
      ``development_class_definition`` builders bootstrap-collections uses
      (one schema home, no second table);
    * nothing existing is ever dropped or rewritten.

    OWN collections only (v0.2.105 fix round): ``SHARED_KG_COLLECTION`` is
    NEVER auto-created. The shared class belongs to the orchestrator root
    — its curated nodes are materialised there only — so on any other
    project an absent shared name is a misconfiguration (a typo or a stale
    name), and an empty auto-created class would make the "every KG query
    returns nothing" state permanent and silent by satisfying the probe.
    The F-12(b) warning with the manual command STANDS for it. When
    ``KG_COLLECTION == SHARED_KG_COLLECTION`` (the orchestrator root: one
    class serving both roles) the name is created ONCE, through
    ``KG_COLLECTION`` — the probe dedupes it (first key wins), so it is
    never probed, warned about or created twice.

    Returns verify's dict extended with ``created`` (names this call
    created) and, for a creation that FAILED, ``create_errors`` — the
    original per-name warning then STANDS (it names the manual
    ``bootstrap-collections`` command, the remediation when the automatic
    create could not complete). Never raises.
    """
    from vco_lib import project_init as _pi

    out = _pi.verify_configured_collections_exist(
        folder, weaviate_url, log_event=log_event,
    )
    out["created"] = []
    out["create_errors"] = []
    if not out.get("absent"):
        return out
    try:
        from vco_lib.knowledge_residue import project_settings_env
        env = project_settings_env(Path(folder))
    except Exception:  # noqa: BLE001 — cannot map name→schema; warn as before
        return out

    schema_by_key = {
        "KG_COLLECTION": _pi.kg_class_definition,
        "DEVELOPMENT_COLLECTION": _pi.development_class_definition,
    }
    url = (weaviate_url or (env.get("WEAVIATE_URL") or "").strip()
           or _pi._weaviate_url_default())  # noqa: SLF001 — single-home alias

    for name in list(out["absent"]):
        key = next(
            (k for k in schema_by_key
             if (env.get(k) or "").strip() == name),
            None,
        )
        if key is None:
            # Not one of the creatable keys' values (e.g. a distinct
            # SHARED_KG_COLLECTION): the verify warning stands unchanged.
            continue
        try:
            _pi._create_class(schema_by_key[key](name), weaviate_url=url)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001 — never blocks the install
            err = f"{type(e).__name__}: {e}"
            out["create_errors"].append({"collection": name, "error": err})
            if log_event is not None:
                try:
                    log_event(
                        "4.bundle.configured-collections", "warn",
                        f"automatic create of absent configured collection "
                        f"{name!r} failed: {err}",
                        data={"collection": name, "error": err[:200]},
                    )
                except Exception:  # noqa: BLE001
                    pass
            continue  # the verify warning for this name stands
        out["created"].append(name)
        out["absent"].remove(name)
        # v0.2.105 fix round (review NIT): retire EXACTLY the warning verify
        # built for THIS name — the one-home prefix, never a bare
        # `name not in w` substring test, which also dropped a DIFFERENT
        # collection's warning whenever that name merely contained this one
        # (creating `X_KnowledgeGraph` used to silence the shared-collection
        # warning for `X_KnowledgeGraph_Shared` — the very warning this fix
        # round says must STAND).
        _marker = configured_absent_warning_prefix(name)
        out["warnings"] = [w for w in out["warnings"]
                           if not w.startswith(_marker)]
        if log_event is not None:
            try:
                log_event(
                    "4.bundle.configured-collections", "ok",
                    f"created absent configured collection {name!r} with the "
                    f"shipped schema (empty; nothing existing was touched)",
                    data={"collection": name, "action": "create"},
                )
            except Exception:  # noqa: BLE001
                pass
    return out
