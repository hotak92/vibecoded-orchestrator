# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which CLAUDE.md conditional sections render for a folder — the ONE resolver.

v0.2.101 (12c, plan D2). Both CLAUDE.md render paths — the orchestrator ROOT
(``vco_lib.rendered_root_files``) and the PROJECT paths (``vco_lib
.project_templates``: the bundle install/update and the launcher's
``re-render-claude-md``) — ask this module which feature sections are live for
the folder they are rendering, and feed the answer straight into
``project_init.render_conditional_blocks`` as its ``active_modules`` set. There
is deliberately no second place that computes "which sections render here".

Feature ids (the conditional-tag vocabulary of both templates):

``diagrams``
    the ``project_modules`` verdict for this folder's project (default-on;
    an explicit row can disable it) — ``module_gated_delivery
    .active_modules_verdict``, reached through the folder→UUID resolution
    the old bundle path skipped (plan D4: it passed ``str(folder)`` where
    launcher.db keys on the project UUID, so explicit rows never reached
    the render).
``model_gateway``
    the SAME tri-state verdict that decides delivery of the gateway agent
    definitions (``module_gated_delivery.gateway_agents_gate``): an explicit
    per-project row wins; no row falls back to the machine signal; only a
    positive SKIP hides the section — DELIVER and UNKNOWN both render it,
    because a could-not-ask must never hide text (the delivery gate's own
    "never delete on could-not-ask" rule, applied to a render).
``lean_ctx``
    the lean-ctx rewrite hook is REGISTERED in this folder's
    ``.claude/settings.json`` (any event). The hook is the shipped artefact
    and disabling it from the launcher removes the entry, which is exactly
    "not installed here" (plan risk 7: the binary is not probed — the hook
    is a documented no-op without it).
``rl_retrieval``
    the paid RL module is enabled — the SAME call the retrieval pipeline
    gates on (``VCThelpers.license.feature_enabled("rl_retrieval",
    module_id="vct-rl-reranker")``, ``claude_mcp_servers/rl_client/
    search_pipeline.py``), so the section and the behaviour it describes can
    not drift apart.

Conservative failure contract (owner rule; plan D2 + risk 2): every probe
that CANNOT answer renders its section. A detection failure must never make
guidance for a feature that IS installed disappear; the render never crashes
and never raises — a failed probe is one stderr line, the same shape as
``model_selection._warn_line``.

``needed`` — probe only what the document can use. :func:`tagged_features`
extracts the conditional-tag names a template actually carries, and a caller
that holds the template text passes them as ``needed``. A probe skipped
because the document has no tag for it cannot change the output, and one
probe is NOT free to run speculatively: the license validator behind
``rl_retrieval`` resolves the machine's license key and may contact the
licensing backend, so running it for a template with no RL section would put
a network-shaped side effect into every project render. ``needed=None`` (the
default) probes everything and is what a caller without the template text
uses.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from vco_lib.module_gated_delivery import GATEWAY_MODULE_NAME

__all__ = [
    "ALL_FEATURES",
    "DIAGRAMS",
    "LEAN_CTX",
    "MODEL_GATEWAY",
    "RL_RETRIEVAL",
    "active_sections",
    "gateway_section_renders",
    "tagged_features",
]

#: The diagrams feature id — the default-on module of
#: ``module_gated_delivery.DEFAULT_ACTIVE_MODULES``.
DIAGRAMS = "diagrams"

#: The gateway feature id IS the delivery gate's module name (one spelling,
#: one home: GUI toggle, template tag and delivery gate all share it).
MODEL_GATEWAY = GATEWAY_MODULE_NAME

#: The lean-ctx feature id (tag name in the root template).
LEAN_CTX = "lean_ctx"

#: The RL-reranking feature id (tag name in the root template).
RL_RETRIEVAL = "rl_retrieval"

#: Every feature id this resolver knows. The completeness gate
#: (``tests/test_template_materialization_complete.py``) renders every
#: shipped template under this full set AND under the empty set.
ALL_FEATURES: frozenset[str] = frozenset(
    {DIAGRAMS, MODEL_GATEWAY, LEAN_CTX, RL_RETRIEVAL}
)

#: Features with a dedicated probe below (everything else — ``diagrams`` and
#: any future ``project_modules`` name — is answered by the modules verdict).
_DEDICATED = frozenset({MODEL_GATEWAY, LEAN_CTX, RL_RETRIEVAL})

#: The per-project settings file the lean-ctx probe reads.
_SETTINGS_REL = Path(".claude") / "settings.json"

#: Substring that identifies the lean-ctx rewrite hook inside a settings
#: ``hooks`` tree (the shipped registration commands name the hook script;
#: the launcher's toggle removes the entry when the hook is disabled).
_LEAN_CTX_HOOK_MARKER = "lean-ctx-rewrite"

#: The per-module license id the RL pipeline itself passes (one call shape,
#: so a per-module RL key unlocks the section exactly when it unlocks the
#: reranking).
_RL_MODULE_ID = "vct-rl-reranker"

#: Conditional-tag names a template carries. Deliberately LOOSER than the
#: renderer's whole-line grammar (``project_init.render_conditional_blocks``):
#: a superset only probes a feature whose tag the strict parser would reject
#: anyway — the conservative direction.
_TAG_NAME_RE = re.compile(
    r"\{\{#if_module_(?:in)?active\s+([a-z_][a-z0-9_]*)\s*\}\}"
)


def tagged_features(template_text: str) -> frozenset[str]:
    """The conditional-tag feature names ``template_text`` carries.

    Feeds :func:`active_sections`' ``needed`` filter so a render only probes
    what its document can actually use (see the module docstring).
    """
    return frozenset(_TAG_NAME_RE.findall(template_text))


def _warn(message: str) -> None:
    """One stderr line, never a crash (a warning must not break a render)."""
    try:
        print(f"[vco] claude-md-sections: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a warning must never raise
        pass


def _module_sections(
    folder: Path, *, db_path: Optional[Path], project_id: Optional[str],
) -> set[str]:
    """The ``project_modules`` verdict for this folder (defaults on failure).

    Resolves the folder to its launcher.db UUID first — the step the old
    bundle-path call skipped (plan D4). ``None`` from the resolution (no DB,
    folder not registered, could not ask) maps to the default-on set, which
    is the verdict's own "could not ask" answer one layer down.
    """
    from vco_lib.module_gated_delivery import (
        DEFAULT_ACTIVE_MODULES,
        active_modules_verdict,
        resolve_project_id_for_folder,
    )

    try:
        resolved = (
            project_id
            if project_id is not None
            else resolve_project_id_for_folder(folder, db_path=db_path)
        )
        if resolved is None:
            return set(DEFAULT_ACTIVE_MODULES)
        return set(active_modules_verdict(resolved, db_path=db_path).active)
    except Exception as exc:  # noqa: BLE001 — a probe answers, it never raises
        _warn(
            f"project_modules probe failed for {folder}: {exc} — "
            "rendering the default-on sections"
        )
        return set(DEFAULT_ACTIVE_MODULES)


def gateway_section_renders(verdict: Any) -> bool:
    """Map a :class:`~vco_lib.module_gated_delivery.GateVerdict` to "does the
    ``model_gateway`` CLAUDE.md section render?": only a positive SKIP hides.

    ONE home for the mapping. The render path (:func:`_gateway_renders`) and
    the launcher's Services page (through
    ``python -m vco_lib.module_gated_delivery status --json --folder``, whose
    payload carries the answer as ``claude_md_section.renders``) both call
    this, so the GUI toggle can never disagree with what the render actually
    did. DELIVER and UNKNOWN both render — UNKNOWN never hides text,
    mirroring the gate's own "a could-not-ask carries the previous state
    forward, it never deletes" contract.
    """
    from vco_lib.module_gated_delivery import GateState

    return getattr(verdict, "state", None) is not GateState.SKIP


def _gateway_renders(folder: Path, *, db_path: Optional[Path]) -> bool:
    """The delivery gate's verdict, mapped for a RENDER: only SKIP hides.

    DELIVER and UNKNOWN both render the section — UNKNOWN never hides text,
    mirroring the gate's own "a could-not-ask carries the previous state
    forward, it never deletes" contract.
    """
    from vco_lib.module_gated_delivery import gateway_agents_gate

    try:
        return gateway_section_renders(
            gateway_agents_gate(folder, db_path=db_path))
    except Exception as exc:  # noqa: BLE001 — a probe answers, it never raises
        _warn(
            f"model-gateway probe failed for {folder}: {exc} — "
            "rendering the gateway section"
        )
        return True


def _mentions(node: Any, needle: str) -> bool:
    """Does any string inside the JSON-ish ``node`` contain ``needle``?"""
    if isinstance(node, str):
        return needle in node
    if isinstance(node, dict):
        return any(_mentions(v, needle) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_mentions(v, needle) for v in node)
    return False


def _lean_ctx_renders(folder: Path) -> bool:
    """Is the lean-ctx rewrite hook registered in THIS folder's settings?

    A missing settings file is an ANSWER (nothing is registered here — the
    section is correctly absent); an unreadable or unparseable one is a
    DETECTION FAILURE and renders the section (conservative default).
    """
    path = Path(folder) / _SETTINGS_REL
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError) as exc:
        _warn(f"{path} could not be read: {exc} — rendering the lean-ctx section")
        return True
    try:
        data = json.loads(raw)
    except ValueError as exc:
        _warn(f"{path} does not parse: {exc} — rendering the lean-ctx section")
        return True
    if not isinstance(data, dict):
        _warn(f"{path} is not a settings object — rendering the lean-ctx section")
        return True
    hooks = data.get("hooks")
    if hooks is None:
        return False  # a parseable settings file with no hooks: not registered
    return _mentions(hooks, _LEAN_CTX_HOOK_MARKER)


def _rl_renders() -> bool:
    """Is the paid RL reranking module enabled on this machine's license?

    The SAME call the retrieval pipeline gates on, so the section describes
    exactly the behaviour the pipeline will have. An unavailable validator is
    a broken install on a root that ships it — the conservative direction
    keeps the text.
    """
    try:
        from VCThelpers.license import feature_enabled
    except Exception as exc:  # noqa: BLE001 — import-guarded by design (plan D2)
        _warn(f"license validator unavailable: {exc} — rendering the RL section")
        return True
    try:
        return bool(feature_enabled(RL_RETRIEVAL, module_id=_RL_MODULE_ID))
    except Exception as exc:  # noqa: BLE001 — a probe answers, it never raises
        _warn(f"RL license probe failed: {exc} — rendering the RL section")
        return True


def active_sections(
    folder: Path,
    *,
    db_path: Optional[Path] = None,
    project_id: Optional[str] = None,
    needed: Optional[Iterable[str]] = None,
) -> frozenset[str]:
    """The feature ids whose CLAUDE.md sections render for ``folder``.

    The return feeds ``project_init.render_conditional_blocks`` directly as
    its ``active_modules`` set. Never raises: every probe soft-fails to
    RENDER its section with one stderr line (see the module docstring).

    Args:
        folder: the project folder (or orchestrator root) being rendered.
        db_path: override the default ``~/.vct/launcher.db`` resolution
            (tests, and the launcher path that already holds one).
        project_id: the launcher.db UUID to read ``project_modules`` for,
            when the caller already holds it (the launcher's
            ``re-render-claude-md`` passes the id it toggled). ``None``
            resolves it from ``folder``.
        needed: only probe these feature ids — pass
            :func:`tagged_features` of the template being rendered. ``None``
            probes :data:`ALL_FEATURES`.
    """
    folder = Path(folder)
    probe = set(ALL_FEATURES) if needed is None else set(needed)
    sections: set[str] = set()

    # The modules verdict answers `diagrams` and any other row-driven name.
    if probe - _DEDICATED:
        sections |= _module_sections(folder, db_path=db_path, project_id=project_id)

    if MODEL_GATEWAY in probe:
        # The delivery gate is the SOLE authority for the gateway id: an
        # enabled row read through the verdict can never disagree (the gate
        # reads the same row first), and a project_id override that does
        # disagree must not leak a gateway section the gate would skip.
        sections.discard(MODEL_GATEWAY)
        if _gateway_renders(folder, db_path=db_path):
            sections.add(MODEL_GATEWAY)

    if LEAN_CTX in probe and _lean_ctx_renders(folder):
        sections.add(LEAN_CTX)

    if RL_RETRIEVAL in probe and _rl_renders():
        sections.add(RL_RETRIEVAL)

    return frozenset(sections)
