# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Doctor probes for the infrastructure boundary (v0.2.102 W5-A).

Kept out of :mod:`vco_lib.doctor` (past 5 000 lines — the modularity rule):
``doctor`` registers each probe through a one-line shim in its ``PROBES``
table, and the deferral entry through ``_ENTRY_BUILDERS``. The ``Finding``
type, the status constants and the resolver seam stay ``doctor``'s, imported
lazily so this module never imports at ``doctor``'s import time.

* :func:`probe_infra_declarations` (M3) — what the projects on this machine
  declared their own (external collections, unmanaged containers, local
  ports), and who declared each: the "reason shown" every skipping surface
  points back to. Informational; no condition.
* :func:`probe_compose_override_pair` (P299-B2) — do the two override
  auto-load names carry the same config, in every directory the install-time
  rename pass walks? Self-resolving ``compose_override_pair_diverged``, the
  one condition for that state (it superseded the rename pass's
  ``compose_override_filename_conflict``).
"""

from __future__ import annotations

from pathlib import Path

#: condition_id of a divergent override pair (registered ``action_required``;
#: clear probe ``compose_override_pair_still_diverged`` = the same
#: :func:`vco_lib.compose_override_pair.pair_state` reading).
from vco_lib.compose_override_pair import CID_PAIR_DIVERGED as CID_COMPOSE_OVERRIDE_PAIR_DIVERGED  # noqa: E402


def _is_install_root(folder: Path) -> bool:
    return (Path(folder) / "vco_lib" / "__init__.py").is_file()


def probe_infra_declarations(folder: Path, res, ctx: dict) -> list:
    """Report every honoured declaration with its declarer.

    Nothing declared → no finding (the majority machine; a line saying "none"
    on every run is noise). Declarations that exist but cannot be read →
    ``unknown``: every consumer that would act on a container refuses in that
    state, and the reader should know why.
    """
    from vco_lib import doctor as d
    from vco_lib import infra_declarations as infra

    decl = res.resolve_infra_declarations()
    if decl is None:
        return []
    if not decl.resolvable:
        return [d.Finding(
            probe="infra_declarations",
            status=d.STATUS_UNKNOWN,
            summary=("project infrastructure declarations could not be read from "
                     "launcher.db — heals of VCO's own containers refuse until they can"),
        )]
    if not decl.rows:
        return []
    labels = {
        infra.KIND_EXTERNAL_COLLECTION: "external collections",
        infra.KIND_UNMANAGED_CONTAINER: "unmanaged containers",
        infra.KIND_LOCAL_PORT: "local ports",
    }
    parts: list[str] = []
    detail: dict = {}
    for kind in infra.KINDS:
        values = sorted(decl.values(kind), key=(int if kind == infra.KIND_LOCAL_PORT else str))
        if not values:
            continue
        detail[kind] = [{"value": v, "declared_by": list(decl.sources(kind, v))} for v in values]
        listed = ", ".join(f"{v} ({', '.join(decl.sources(kind, v))})" for v in values)
        parts.append(f"{labels[kind]}: {listed}")
    return [d.Finding(
        probe="infra_declarations",
        status=d.STATUS_OK,
        summary=("honouring what projects declared their own — VCO never reports, heals, "
                 "re-creates or adopts these: " + "; ".join(parts)),
        detail=detail,
    )]


def probe_compose_override_pair(folder: Path, res, ctx: dict) -> list:
    """Is every override pair of the install one configuration? (P299-B2)

    Install roots only; the pairs walked are
    :data:`vco_lib.compose_override_pair.SEARCH_SUBDIRS` — the same ones the
    install-time rename pass walks, so both emitters of
    ``compose_override_pair_diverged`` read the same state. A divergent pair is
    a problem finding the doctor emits; none is an OK finding CARRYING the
    cid, so the same reading clears an earlier entry (the ``disk_space``
    contract).
    """
    from vco_lib import compose_override_pair as cop
    from vco_lib import doctor as d

    root = Path(folder)
    if not _is_install_root(root):
        return []
    states = {sub: cop.pair_state(root / sub) for sub in cop.SEARCH_SUBDIRS}
    pairs = cop.divergent_pairs(root)
    detail = {"states": states, "divergent": [[str(a), str(b)] for a, b in pairs]}
    if not pairs:
        return [d.Finding(
            probe="compose_override_pair",
            status=d.STATUS_OK,
            condition_id=CID_COMPOSE_OVERRIDE_PAIR_DIVERGED,
            summary="compose override pairs: " + ", ".join(f"{k} {v}" for k, v in states.items()),
            detail=detail,
        )]
    return [d.Finding(
        probe="compose_override_pair",
        status=d.STATUS_PROBLEM,
        summary=cop.divergent_pairs_summary(pairs),
        fix=d.FIX_DEFER,
        condition_id=CID_COMPOSE_OVERRIDE_PAIR_DIVERGED,
        command=cop.pair_remediation(pairs),
        detail=detail,
    )]


def compose_override_pair_entry(finding):
    """The deferral the divergent-pair reading owes — the ONE builder in
    :func:`vco_lib.compose_override_pair.divergent_pairs_entry`, shared with
    the install-time rename pass."""
    from vco_lib import compose_override_pair as cop

    pairs = [(Path(a), Path(b)) for a, b in finding.detail.get("divergent") or []]
    return cop.divergent_pairs_entry(pairs, detected=finding.summary)
