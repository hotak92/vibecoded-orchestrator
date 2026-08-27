# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The code-graph analyzer's deferral tenancy — emit + paired clear.

ONE home for the three ledger operations `templates/scripts/analyze_code_graph.py`
owns (v0.2.91 wave-3). They lived inline in the analyzer, which the
`tests/test_analyze_code_graph_ratchet.py` ratchet caps: "put the new logic in
a ``vco_lib`` module the analyzer imports, not inline". The wave-3 MAJOR-1 fix
needed a THIRD one (the paired clear), so the whole family moved here and the
analyzer keeps three thin wrappers.

The tenancy
-----------
Two conditions, one owner:

* ``code_graph_no_embedding_backend`` — the analyzer could not construct an
  :class:`~vco_lib.embedding_service.EmbeddingService` at all.
* ``code_graph_code_backend_unreachable`` — it constructed one, and then found
  the backend serving the CODE slot unreachable. A distinct condition because
  a CodeEmbed container can be down while Ollama is up, and the remediation
  command differs.

Both emit and then ``return 0``: a missing backend is a soft-fail at the
install boundary, never a failed install.

Why the CLEAR belongs here too (MAJOR-1)
----------------------------------------
Because both skip paths exit 0, an exit code says NOTHING about whether the
walk happened. Before this, the only thing that ever resolved these conditions
was :mod:`vco_lib.deferral_retry` treating that zero exit as success — so a
retry launched while the backend was still down deleted (and tombstoned) the
entry the child had just re-written, and logged "completed" for work that never
ran. :func:`clear_backend_deferrals` is the missing pair: the analyzer calls it
at the one point that proves the walk happened, and the dispatcher now reads
the LEDGER rather than the exit code.

WHICH ledger — one root for emit AND clear (MAJOR-A)
----------------------------------------------------
Every function here takes an explicit ``install_root``, and the analyzer must
pass the SAME value to all three. It historically derived that value from
``$VCT_ORCHESTRATOR_ROOT`` (else ``repo_path``), which is right for a direct
invocation and wrong for a RETRY:

* A launcher-/bundle-managed user project P emits into P's ledger — the
  launcher deliberately leaves ``VCT_ORCHESTRATOR_ROOT`` unset
  (``codegraph.rs``), so ``install_root`` IS P.
* A session-start retry (:mod:`vco_lib.deferral_retry`) runs with the session
  env, which DOES carry ``VCT_ORCHESTRATOR_ROOT`` (projected via
  ``.claude/env``), and the analyzer child inherits it.

So the retried child cleared — and re-emitted — in the ORCHESTRATOR clone: a
cross-ledger resolve made on P's evidence, while the dispatcher re-read P,
still found the entry, and returned INCONCLUSIVE on every attempt until the
cap burned and the false entry was immortal in P.

The fix is an argv seam: ``analyze_code_graph.py --deferral-root <folder>``
(``_resolve_deferral_root``), which the dispatcher pins to the folder whose
ledger it will re-read — the analyzer's counterpart to the
``KG_SYNC_PROJECT_ROOT`` pin ``retry_kg_seed`` already carried. Emit-root ==
clear-root == dispatcher-read-root, so a still-down retry's re-emit also lands
where the reader looks. Absent, resolution is unchanged.

Import contract
---------------
The analyzer imports this module INSIDE a ``try`` (as it already did for
``deferral_emit``) and prints a one-line note if the import fails. Ledger
bookkeeping must never change the analyzer's exit code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

#: The two condition ids the analyzer owns. Named once so the emitters and the
#: paired clear can never drift apart.
CODE_GRAPH_BACKEND_CIDS: tuple[str, ...] = (
    "code_graph_no_embedding_backend",
    "code_graph_code_backend_unreachable",
)

#: KG node every entry in this family points at.
_KG_REFS = ["knowledge/concepts/embedding-service-v0218.md"]


def clear_backend_deferrals(
    install_root: Path,
    condition_ids: Sequence[str] = CODE_GRAPH_BACKEND_CIDS,
) -> int:
    """Resolve the backend-skip conditions after a SUCCESSFUL analysis run.

    The NARROW clear (decision #12), mirroring
    ``sync_knowledge_graph.py::_clear_sync_deferral_no_backend``: the caller
    invokes this only after the analyzer walked the tree, wrote its objects and
    passed the data-loss gates — which is precisely the claim the entries deny.

    Resolving BOTH ids is correct regardless of which was emitted: a completed
    walk falsifies both premises, and resolving an absent id is a no-op.

    Returns the number of ids that were actually present (0 on any I/O error —
    ``resolve_conditions`` is itself soft-fail).
    """
    from vco_lib.deferral_emit import resolve_conditions

    return resolve_conditions(Path(install_root), tuple(condition_ids))


def emit_no_backend(install_root: Path, exc: BaseException) -> bool:
    """Emit ``code_graph_no_embedding_backend``.

    Written through the LOCKED emitter (:mod:`vco_lib.deferral_emit`) — the raw
    read-modify-write it replaced in v0.2.91 WP-B ran from a subprocess while
    install.py's own deferral finalize was live.
    """
    from vco_lib.deferral_emit import DeferralEntry, emit

    return emit(Path(install_root), DeferralEntry(
        condition_id="code_graph_no_embedding_backend",
        title="Code-graph analysis skipped: no embedding backend reachable",
        detected=(
            "analyze_code_graph.py could not reach any configured embedding "
            f"backend (CodeEmbed / Ollama / OpenAI). Error: {exc}"
        ),
        why_deferred=(
            "Soft-fail policy: install must never block on transient service "
            "unavailability. The code graph for this project will be empty "
            "until the next analysis run succeeds. See "
            "~/.claude/metrics/embedding_failures.jsonl for the per-backend "
            "diagnostic written by EmbeddingService."
        ),
        command_to_apply=(
            "# Restart embedding services then re-run analysis:\n"
            "podman start vco_code_embed vco_ollama   # or: docker start ...\n"
            ".claude/scripts/code-graph-analyze . --project <name>"
        ),
        severity="warning",
        kg_node_refs=list(_KG_REFS),
    ))


def _service_hint(slot: str) -> tuple[str, str]:
    """(human name, restart command) for the backend serving ``slot``."""
    if "codesage" in slot:
        return (
            "CodeEmbed service (vco_code_embed container on port 11440)",
            "podman start vco_code_embed",
        )
    if "openai" in slot:
        return (
            "OpenAI API",
            "# Check OPENAI_API_KEY is set and the key is valid:\n"
            "# Preferences → Special Secrets → OpenAI → Re-check",
        )
    return (
        "Ollama (vco_ollama container on port 11435)",
        "podman start vco_ollama",
    )


def emit_code_backend_down(install_root: Path, slot: str, model: str) -> bool:
    """Emit ``code_graph_code_backend_unreachable``.

    Takes the slot + model as PRIMITIVES rather than an ``EmbeddingService``:
    this module then has no dependency on the service type, and the analyzer's
    wrapper stays the only place that knows how to read them off it.
    """
    from vco_lib.deferral_emit import DeferralEntry, emit

    service_hint, restart_cmd = _service_hint(slot)
    return emit(Path(install_root), DeferralEntry(
        condition_id="code_graph_code_backend_unreachable",
        title=f"Code-graph analysis skipped: {service_hint} not reachable",
        detected=(
            f"analyze_code_graph.py would write to slot '{slot}' "
            f"(model: {model}), but the backend serving that slot is "
            "currently unreachable. Refusing to proceed — a code graph with "
            "empty vectors is worse than no code graph (search would return "
            "all-zero scores)."
        ),
        why_deferred=(
            "Soft-fail policy: never produce a degraded code graph. Restart "
            "the service and re-run analysis."
        ),
        command_to_apply=(
            f"{restart_cmd}\n"
            ".claude/scripts/code-graph-analyze . --project <name>"
        ),
        severity="warning",
        kg_node_refs=list(_KG_REFS),
    ))


__all__ = [
    "CODE_GRAPH_BACKEND_CIDS",
    "clear_backend_deferrals",
    "emit_code_backend_down",
    "emit_no_backend",
]
