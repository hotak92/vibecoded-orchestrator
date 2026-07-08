# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 P3g (M-1 remainder): RL state ownership moved out of server.py.

The v0.2.73 M-1 split moved the 37 RL helpers into ``rl_enrichment.py`` but left
their mutable caches + tuning constants DEFINED on ``server.py`` — the moved
functions read them back through a lazy ``server`` proxy. That "state stayed
behind" coupling is the SUB-OPTIMAL remainder the v0.2.75 codegraph reconcile
named: every future extraction from the still-oversized ``server.py`` inherited
the proxy pattern.

P3g moves the DEFINITION home into ``weaviate_mcp.rl_state`` (RL caches +
thresholds) and ``weaviate_mcp.embeddings`` (pure-getenv embedding config).
``server.py`` becomes importer-only for those names — it re-exports them so the
public contract (``srv.<name>`` reads, test patches on the server object,
by-reference dict/set mutation) is unchanged bit-for-bit.

These tests are the grep-gate + behaviour lock so a future editor cannot silently
re-introduce the definitions on ``server.py``.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_PARENT = REPO_ROOT / "claude_mcp_servers"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

SERVER_PY = PKG_PARENT / "weaviate_mcp" / "server.py"

# RL state names that MUST no longer be DEFINED (own a literal/getenv value) on
# server.py — server may only re-export them (``NAME = _rl_state.NAME``).
_RL_STATE_NAMES = {
    "_RL_OVERFETCH",
    "_RL_MAX_LINKED",
    "_rl_call_seq",
    "_rl_monitor_tasks",
    "_rl_node_content_cache",
    "_RL_NODE_CACHE_MAX",
    "_RL_MONITOR_POLL_INTERVAL",
    "_RL_MONITOR_ANSWER_THRESHOLD_TOKENS",
    "_RL_MONITOR_ANSWER_THRESHOLD",
    "_RL_TOOL_CONTENT_LIMIT",
    "_RL_MONITOR_TIMEOUT",
    "_RL_MONITOR_FORCE_FLUSH_SENTINEL",
    "_RL_MIN_ANSWER_TOKENS_FOR_CITATION",
    "_RL_MIN_ANSWER_CHARS_FOR_CITATION",
    "_RL_LITERAL_CITED_MIN_TITLE_LEN",
    "_rl_client_instances",
    "_rl_telemetry_writers",
    "_rl_telemetry_writer_instance",
    "_rl_client_instance",
    "_CODE_STRUCTURE_TELEMETRY_MAX_NODES",
    "DUAL_RL_LOG_ENABLED_ENV",
}

# Embedding config that moved to embeddings.py (pure getenv, re-exported).
_EMBED_CONFIG_MOVED = {
    "LEGACY_TEXT_EMBEDDING_MODEL",
    "OPENAI_EMBEDDING_MODEL",
    "OPENAI_API_KEY",
    "CODE_EMBED_SERVICE_URL",
}


def _module_level_assignments(source: str) -> dict[str, ast.expr]:
    """Return {target_name: value_node} for every MODULE-LEVEL assignment.

    Only top-level (not nested in a function/class) simple-name assignments are
    returned — that is the "ownership" surface we gate on.
    """
    tree = ast.parse(source)
    out: dict[str, ast.expr] = {}
    for node in tree.body:  # module-level only
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                out[tgt.id] = node.value  # type: ignore[union-attr]
    return out


def _is_reexport_from(value: ast.expr, module_alias: str) -> bool:
    """True when ``value`` is ``<module_alias>.<attr>`` (a re-export, not a def)."""
    return (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == module_alias
    )


def test_server_does_not_own_rl_state_defs():
    """server.py must only RE-EXPORT the RL state names (``= _rl_state.<name>``),
    never DEFINE them with a literal / getenv / empty-container expression."""
    assigns = _module_level_assignments(SERVER_PY.read_text(encoding="utf-8"))
    offenders = []
    for name in sorted(_RL_STATE_NAMES):
        val = assigns.get(name)
        assert val is not None, (
            f"{name} has no module-level assignment in server.py — expected a "
            f"re-export ``{name} = _rl_state.{name}``."
        )
        if not _is_reexport_from(val, "_rl_state"):
            offenders.append(name)
    assert not offenders, (
        "server.py must be importer-only for RL state (P3g). These names are "
        "still DEFINED on server.py instead of re-exported from _rl_state — the "
        "proxy-pattern coupling the M-1 remainder was meant to remove:\n  "
        + "\n  ".join(offenders)
    )


def test_server_does_not_own_moved_embedding_config():
    """server.py must re-export the moved embedding config from ``_embeddings``,
    not define it with ``os.getenv`` (P3g embedding-config half)."""
    assigns = _module_level_assignments(SERVER_PY.read_text(encoding="utf-8"))
    offenders = []
    for name in sorted(_EMBED_CONFIG_MOVED):
        val = assigns.get(name)
        assert val is not None, f"{name} missing from server.py (expected re-export)."
        if not _is_reexport_from(val, "_embeddings"):
            offenders.append(name)
    assert not offenders, (
        "server.py must re-export moved embedding config from _embeddings, not "
        "define it inline:\n  " + "\n  ".join(offenders)
    )


def test_rl_state_module_owns_the_definitions():
    """rl_state.py is the real definition home — the caches are the right
    container TYPES (they are process-global mutable state other tests populate,
    so assert type not emptiness), the thresholds carry their literal values."""
    st = importlib.import_module("weaviate_mcp.rl_state")
    assert isinstance(st._rl_node_content_cache, dict)
    assert isinstance(st._rl_client_instances, dict)
    assert isinstance(st._rl_telemetry_writers, dict)
    assert isinstance(st._rl_monitor_tasks, set)
    assert st._RL_MAX_LINKED == 5
    assert st._RL_MONITOR_ANSWER_THRESHOLD_TOKENS == 25_000
    assert st._RL_MONITOR_ANSWER_THRESHOLD == 100_000
    assert st._RL_MONITOR_TIMEOUT == 3600.0
    assert st._RL_LITERAL_CITED_MIN_TITLE_LEN == 3
    assert st._CODE_STRUCTURE_TELEMETRY_MAX_NODES == 64
    assert st.DUAL_RL_LOG_ENABLED_ENV == "DUAL_RL_LOG_ENABLED"


def test_mutable_caches_are_reexported_by_reference():
    """server.<cache> must be the SAME object as rl_state.<cache> so every
    in-place mutation (rl_client.search_pipeline, tests) is observed on both."""
    srv = importlib.import_module("weaviate_mcp.server")
    st = importlib.import_module("weaviate_mcp.rl_state")
    assert srv._rl_node_content_cache is st._rl_node_content_cache
    assert srv._rl_client_instances is st._rl_client_instances
    assert srv._rl_telemetry_writers is st._rl_telemetry_writers
    assert srv._rl_monitor_tasks is st._rl_monitor_tasks
    # Prove by-reference: a mutation through server is visible in rl_state.
    srv._rl_node_content_cache["p3g_probe"] = {"ok": True}
    try:
        assert st._rl_node_content_cache["p3g_probe"] == {"ok": True}
    finally:
        srv._rl_node_content_cache.pop("p3g_probe", None)


def test_scalar_thresholds_reexport_value_and_stay_patchable_on_server():
    """Scalar thresholds re-export by value; the patch surface stays ``server``
    (rl_enrichment reads them via the server proxy, so a setattr on the server
    object is observed by the moved functions — bit-for-bit unchanged)."""
    srv = importlib.import_module("weaviate_mcp.server")
    rl = importlib.import_module("weaviate_mcp.rl_enrichment")
    assert srv._RL_MONITOR_POLL_INTERVAL == 2.0
    original = srv._RL_MONITOR_POLL_INTERVAL
    try:
        srv._RL_MONITOR_POLL_INTERVAL = 0.005  # simulate monkeypatch.setattr(srv, ...)
        # rl_enrichment reads server._RL_MONITOR_POLL_INTERVAL via the proxy.
        assert rl.server._RL_MONITOR_POLL_INTERVAL == 0.005
    finally:
        srv._RL_MONITOR_POLL_INTERVAL = original


def test_rl_call_seq_has_one_home_and_increments():
    """The counter's single authoritative store is rl_state; next_rl_call_seq
    increments it monotonically."""
    st = importlib.import_module("weaviate_mcp.rl_state")
    a = st.next_rl_call_seq()
    b = st.next_rl_call_seq()
    assert b == a + 1
