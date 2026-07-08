#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""rl-doctor (v0.2.73 RL-12) — RL retrieval health diagnostic.

WHY THIS EXISTS
---------------
RL soft-fails everywhere by design (a down container / broken hub / license
hiccup must never break the user-facing KG search — the candidates are always
returned in cosine order). The cost of that discipline is DIAGNOSABILITY: when a
paying Pro user's reranker silently stops reranking, every failure path is a
``logger.debug`` line the user never sees. "Why isn't RL reranking?" was, before
this tool, unanswerable without reading source.

``rl-doctor`` is the answer. It is a **read-only** diagnostic that reports every
gate and outcome on the RL path:

  1. License / feature gate — is ``rl_retrieval`` enabled for this install
     (and the per-module overlay), or is the user on free-tier cosine?
  2. Container reachability + negotiated protocol/embedding version (RL-10) —
     is the paid container up, and does its wire contract + embedding space
     match this client?
  3. Last rerank outcome — did the last rerank RPC actually run, or fall back
     to cosine, and WHY (RL-3 fallback counter on disk)?
  4. Telemetry write status — can the hub-backed rl_events writer reach the hub?
  5. Retention status (RL-5) — is the append-only rl_events table being bounded?

It NEVER mutates anything by default: no writes, no container start, no config
change. It prints a human-readable report by default, or ``--json`` for machine
consumption (support tooling / CI). Every probe soft-fails to a clear
"unknown / can't determine" status rather than crashing — a diagnostic that
dies on a degraded system is useless.

ONE deliberate exception (v0.2.75 NEW-2): ``--prune`` runs a single
``rl_events`` retention pass NOW, bypassing the hourly throttle. It exists as
the explicit trigger for opted-out users (whose search path only drives the
throttled opportunistic prune) and for operators who just tightened
``RL_EVENTS_RETENTION_MAX_AGE_DAYS``. Deletion bounds and the 6-h
in-flight-citation floor are identical to the automatic path
(``rl_retention.maybe_run_retention`` — one home).

USAGE
-----
    python claude_mcp_servers/scripts/rl_doctor.py            # human report
    python claude_mcp_servers/scripts/rl_doctor.py --json     # machine JSON
    python claude_mcp_servers/scripts/rl_doctor.py --project-root /path
    python claude_mcp_servers/scripts/rl_doctor.py --prune    # + one retention pass

Exit code: 0 when the RL path is healthy OR legitimately free-tier (nothing to
fix); 1 when RL is ENABLED but a fixable problem was found (container down,
version mismatch, hub unreachable) so the tool is CI/script-friendly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

# Allow ``from claude_mcp_servers...`` imports when run as a bare script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO_ROOT, os.path.join(_HERE, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── project-root resolution (mirrors _record_rl_fallback) ───────────────


def _resolve_project_root(explicit: Optional[str] = None) -> str:
    """CLAUDE_PROJECT_DIR → server.KG_BASE_DIR → cwd. Matches the fallback
    counter's writer so rl-doctor reads the file the pipeline wrote."""
    if explicit:
        return explicit
    root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if root:
        return root
    try:
        from claude_mcp_servers.weaviate_mcp import server as _srv

        root = getattr(_srv, "KG_BASE_DIR", "") or ""
    except Exception:  # noqa: BLE001
        root = ""
    return root or os.getcwd()


# ── individual probes (each soft-fails to a status dict) ────────────────


def _probe_license() -> Dict[str, Any]:
    """License / feature gate. Never raises."""
    try:
        from VCThelpers.license import feature_enabled
    except ImportError:
        return {
            "status": "free_tier",
            "enabled": False,
            "detail": "VCThelpers.license not importable (pure free-tier install).",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "enabled": False, "detail": f"license import raised: {exc}"}
    try:
        enabled = bool(feature_enabled("rl_retrieval", module_id="vct-rl-reranker"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "enabled": False, "detail": f"feature_enabled raised: {exc}"}
    return {
        "status": "enabled" if enabled else "free_tier",
        "enabled": enabled,
        "detail": (
            "rl_retrieval feature is licensed (Pro/MAO or module overlay)."
            if enabled
            else "rl_retrieval not licensed → cosine ordering (nothing to fix)."
        ),
    }


def _probe_per_project_toggle() -> Dict[str, Any]:
    """Per-project enable toggle (hub-resolved). Soft-fail → unknown."""
    try:
        from claude_mcp_servers.weaviate_mcp.server import _try_resolve_project_config

        cfg = _try_resolve_project_config()
        if cfg is None:
            return {"status": "unknown", "enabled": None, "detail": "no project config resolved."}
        enabled = bool(getattr(cfg, "rl_reranker_enabled_for_project", True))
        return {
            "status": "enabled" if enabled else "disabled_for_project",
            "enabled": enabled,
            "detail": (
                "reranker enabled for this project."
                if enabled
                else "reranker explicitly disabled for this project (launcher Modules panel)."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "enabled": None, "detail": f"toggle probe raised: {exc}"}


def _probe_container() -> Dict[str, Any]:
    """Container reachability + RL-10 negotiation. Never raises."""
    try:
        from claude_mcp_servers.rl_client.client import RLClient
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reachable": False, "detail": f"RLClient import raised: {exc}"}

    # Resolve the active embedding tag/dim the same way the MCP does, so the
    # RL-2b space check is meaningful. Defaults are the qwen3 floor.
    text_dim = 1024
    active = os.environ.get("ACTIVE_EMBEDDING", "qwen3") or "qwen3"
    try:
        client = RLClient(text_dim=text_dim, active_embedding=active)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reachable": False, "detail": f"RLClient construct raised: {exc}"}

    if not client.enabled:
        return {
            "status": "disabled",
            "reachable": False,
            "detail": "no RL_SERVER_URL/RL_SERVER_PORT (container not wired).",
        }

    async def _run():
        try:
            neg = await client.negotiate()
        finally:
            await client.aclose()
        return neg

    try:
        neg = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reachable": False, "detail": f"negotiate raised: {exc}"}

    return {
        "status": neg.status,
        "reachable": neg.status not in ("unreachable", "disabled"),
        "compatible": neg.compatible,
        "server_protocol": neg.server_protocol,
        "server_embedding_dim": neg.server_embedding_dim,
        "server_embedding_space": neg.server_embedding_space,
        "client_base_url": client.base_url,
        "detail": neg.detail,
    }


def _probe_fallback_counter(project_root: str) -> Dict[str, Any]:
    """Read the RL-3 rerank-fallback counter from disk (read-only)."""
    path = os.path.join(project_root, ".claude", "state", "rl_fallback_counter.json")
    if not os.path.exists(path):
        return {
            "status": "none",
            "count": 0,
            "detail": "no fallback counter file — no recorded rerank fallbacks.",
            "path": path,
        }
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "count": None, "detail": f"counter unreadable: {exc}", "path": path}
    count = int(data.get("count", 0) or 0) if isinstance(data, dict) else 0
    return {
        "status": "fallbacks_recorded" if count > 0 else "clean",
        "count": count,
        "last_reason": (data.get("last_reason") if isinstance(data, dict) else None),
        "last_ts": (data.get("last_ts") if isinstance(data, dict) else None),
        "detail": (
            f"{count} rerank fallback(s) recorded; last reason: "
            f"{data.get('last_reason', 'n/a')!r}"
            if count > 0
            else "no rerank fallbacks recorded."
        ),
        "path": path,
    }


def _probe_telemetry_hub() -> Dict[str, Any]:
    """Can the telemetry writer reach the hub? Probes token presence + port.

    Read-only: does NOT post an event. Presence of hub.token = hub running."""
    try:
        from claude_mcp_servers.rl_client import hub_writer
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reachable": False, "detail": f"hub_writer import raised: {exc}"}
    try:
        token = hub_writer._read_hub_token()
        port = hub_writer._read_hub_port()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reachable": False, "detail": f"hub probe raised: {exc}"}
    if token is None:
        return {
            "status": "hub_down",
            "reachable": False,
            "port": port,
            "detail": "no hub.token found → hub not running; telemetry writes are lost (soft-fail).",
        }
    return {
        "status": "hub_up",
        "reachable": True,
        "port": port,
        "detail": f"hub.token present; telemetry writes route to 127.0.0.1:{port}.",
    }


def _probe_retention() -> Dict[str, Any]:
    """RL-5 retention configuration + resolved plan (read-only, no prune)."""
    try:
        from claude_mcp_servers.rl_client import rl_retention
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"rl_retention import raised: {exc}"}
    if rl_retention.retention_disabled():
        return {
            "status": "disabled",
            "detail": "RL_EVENTS_RETENTION_DISABLED set → rl_events grows unbounded (user opt-out).",
        }
    try:
        plan = rl_retention.compute_retention_plan()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"compute_retention_plan raised: {exc}"}
    return {
        "status": "noop" if plan.is_noop() else "active",
        "cutoff_ms": plan.cutoff_ms,
        "max_rows": plan.max_rows,
        "reason": plan.reason,
        "detail": (
            "no age or row bound configured → rl_events unbounded."
            if plan.is_noop()
            else f"retention active ({plan.reason})."
        ),
    }


# ── report assembly ─────────────────────────────────────────────────────


def run_diagnostics(project_root: str) -> Dict[str, Any]:
    """Run every probe and assemble the report dict. Pure aggregation."""
    lic = _probe_license()
    toggle = _probe_per_project_toggle()
    container = _probe_container()
    fallback = _probe_fallback_counter(project_root)
    telemetry = _probe_telemetry_hub()
    retention = _probe_retention()

    rl_enabled = bool(lic.get("enabled")) and toggle.get("enabled") is not False

    # Healthy iff: free-tier (nothing to fix) OR (enabled AND container
    # compatible AND hub up). Version mismatch / container down / hub down on
    # an ENABLED install = unhealthy (fixable).
    healthy: bool
    if not rl_enabled:
        healthy = True  # free-tier / opted-out → legitimately cosine
    else:
        healthy = bool(container.get("compatible")) and telemetry.get("reachable") is True

    return {
        "rl_enabled": rl_enabled,
        "healthy": healthy,
        "project_root": project_root,
        "license": lic,
        "per_project_toggle": toggle,
        "container": container,
        "last_fallback": fallback,
        "telemetry_hub": telemetry,
        "retention": retention,
    }


def format_human(report: Dict[str, Any]) -> str:
    """Render the report as a readable multi-line block."""
    lines = []
    lines.append("=== rl-doctor — RL retrieval health ===")
    lines.append(f"project root : {report['project_root']}")
    lines.append(f"RL enabled   : {report['rl_enabled']}")
    lines.append(f"OVERALL      : {'HEALTHY' if report['healthy'] else 'NEEDS ATTENTION'}")
    lines.append("")

    def _sec(title: str, d: Dict[str, Any]) -> None:
        lines.append(f"[{title}] {d.get('status', '?')}")
        lines.append(f"    {d.get('detail', '')}")

    _sec("license gate", report["license"])
    _sec("per-project toggle", report["per_project_toggle"])
    _sec("container / negotiation", report["container"])
    _sec("last rerank fallback", report["last_fallback"])
    _sec("telemetry hub", report["telemetry_hub"])
    _sec("retention (RL-5)", report["retention"])
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rl-doctor",
        description=(
            "Read-only RL retrieval health diagnostic (v0.2.73 RL-12). "
            "The single mutating exception is --prune (v0.2.75 NEW-2)."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--project-root",
        default=None,
        help="override project root (default: CLAUDE_PROJECT_DIR → KG_BASE_DIR → cwd)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "run ONE rl_events retention pass now (bypasses the hourly "
            "throttle). The only mutating flag on this otherwise read-only "
            "tool — explicit trigger for opted-out users / freshly-tightened "
            "retention bounds (v0.2.75 NEW-2)."
        ),
    )
    args = parser.parse_args(argv)

    project_root = _resolve_project_root(args.project_root)
    report = run_diagnostics(project_root)

    if args.prune:
        report["prune_run"] = _run_prune_now()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_human(report))
        if args.prune:
            pr = report["prune_run"]
            print(
                f"\n[prune (--prune)] ran={pr.get('ran')} "
                f"deleted={pr.get('deleted')} skipped={pr.get('skipped')} "
                f"reason={pr.get('reason')}"
            )

    return 0 if report["healthy"] else 1


def _run_prune_now() -> Dict[str, Any]:
    """NEW-2 (v0.2.75): one forced retention pass. Same single home as the
    automatic drivers (``rl_retention.maybe_run_retention``) — the 6-h
    in-flight-citation floor and the no-bounds no-op guard apply identically.
    Soft-fails to a status dict; never raises."""
    try:
        from claude_mcp_servers.rl_client.rl_retention import maybe_run_retention
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "skipped": "import_failed", "deleted": None, "reason": str(exc)}
    try:
        return maybe_run_retention(force=True)
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "skipped": "prune_error", "deleted": None, "reason": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
