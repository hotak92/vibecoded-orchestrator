# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pre-flight vendor-key probe for a subagent launch on a gateway model (v0.2.105).

Field report 2026-10-21: the hub had been down for two days, a
``deepseek-researcher`` lane was launched, and it died on its first request
with "503 no key found for vendor 'qwen'". The launch itself gave no hint;
the lane's whole setup was spent before the failure.

This module is called from the Agent|Task PreToolUse hook
(``agent-brief-kg-inject.sh`` / ``.ps1``) with the hook's stdin payload. When
the launched agent's definition pins a gateway model (``model:
claude-gw/<id>`` in its frontmatter, and the call does not override the
model), it asks the running gateway ``GET /vendor-keys?model=<id>`` — a
NAMES-ONLY answer, never a key value — and REFUSES the launch with a clear
reason when the gateway says the model cannot be served:

* the vendor's key definitely does not resolve (``usable: false`` on the
  vendor route), or
* the gateway's router refuses the id itself (``route: refused`` — the exact
  400 the lane's first request would get).

Everything that is not a definite "cannot" lets the launch through: a
first-party / alias model, an agent with no definition on disk, a gateway
that is not running or predates the route, a transient keychain answer, a
resolution still in flight. A pre-flight that blocked on uncertainty would
be a new way to fail, which is the opposite of its purpose.

A key read that finds the hub refusing connections makes the GATEWAY start
the hub (``model_router.secrets.default_hub_reviver``), so the refusal text
says to retry shortly when the hub was the cause.

Output contract: the hook's stdout. A refusal prints ONE PreToolUse envelope
with ``permissionDecision: "deny"`` (never ``"allow"`` — that would
auto-approve the spawn); every other answer prints nothing. Exit code is 0
for every answer. Kill switch: ``VCO_GATEWAY_PREFLIGHT=0`` (or ``off``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: The gateway route. MUST MATCH ``create_app`` in
#: ``claude_mcp_servers/model_router/server.py``.
ROUTE = "/vendor-keys"

#: Seconds to wait for the gateway. Above the route's own 6 s wait would
#: exceed the hook's 10 s budget together with the KG leg, so the client gives
#: up first and the launch goes through (unknown is never a refusal).
DEFAULT_TIMEOUT_S = 4.0

ENV_KILL_SWITCH = "VCO_GATEWAY_PREFLIGHT"

#: The words the hub-down revive answers with in a key problem (built by
#: ``vco_lib.agent_secrets`` / ``model_router.secrets``). Used only to add the
#: "retry shortly" hint; never to decide.
_HUB_DOWN_MARKERS = ("hub is not running", "HubUnreachable", "hub is not reachable", "vct-hub is not")


def preflight_disabled(environ: Optional[dict] = None) -> bool:
    raw = (environ if environ is not None else os.environ).get(ENV_KILL_SWITCH, "")
    return raw.strip().lower() in {"0", "off", "false", "no"}


def launch_of(payload: Any) -> tuple[Optional[str], Optional[str]]:
    """``(subagent_type, model_override)`` of an Agent/Task PreToolUse payload.

    ``subagent_type`` may be absent (the general-purpose default); the agent
    name is then ``""`` and only a gateway ``model`` override is judged."""
    if not isinstance(payload, dict):
        return None, None
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None, None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, None
    agent = tool_input.get("subagent_type")
    model = tool_input.get("model")
    return (
        agent.strip() if isinstance(agent, str) and agent.strip() else "",
        model.strip() if isinstance(model, str) and model.strip() else None,
    )


def agent_dirs(project_dir: Optional[Path]) -> list[Path]:
    """Where Claude Code finds agent definitions, highest precedence first:
    the project's ``.claude/agents``, then the user's ``~/.claude/agents``."""
    from vco_lib.paths import claude_user_dir

    dirs: list[Path] = []
    if project_dir is not None:
        dirs.append(project_dir / ".claude" / "agents")
    dirs.append(claude_user_dir() / "agents")
    return dirs


def _frontmatter_name(path: Path) -> Optional[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("'\"") or None
    return None


def agent_definition(agent: str, dirs: Iterable[Path]) -> Optional[Path]:
    """The definition file for ``agent``: ``<dir>/<agent>.md`` first, else a
    file whose frontmatter ``name:`` is ``agent``. ``None`` when none (a
    built-in agent type, a plugin agent, a typo — not this probe's to judge)."""
    if not agent or "/" in agent or "\\" in agent or agent.startswith("."):
        return None
    for directory in dirs:
        candidate = directory / f"{agent}.md"
        if candidate.is_file():
            return candidate
        try:
            files = sorted(directory.glob("*.md"))
        except OSError:
            continue
        for path in files:
            if _frontmatter_name(path) == agent:
                return path
    return None


def gateway_model_of(agent: str, model_override: Optional[str], dirs: Iterable[Path]) -> Optional[str]:
    """The gateway model id (``claude-gw/`` stripped) the launch will use, or
    ``None`` when it does not go through the gateway."""
    from vco_lib.vscode_settings import GATEWAY_ID_PREFIX

    if model_override:
        # The Agent tool's `model` parameter overrides the definition. Its
        # enum names first-party tiers, but a gateway id there is judged too.
        model: Optional[str] = model_override
    else:
        path = agent_definition(agent, dirs)
        if path is None:
            return None
        from vco_lib.module_gated_delivery import frontmatter_model

        model = frontmatter_model(path)
    if not model or not model.startswith(GATEWAY_ID_PREFIX):
        return None
    return model[len(GATEWAY_ID_PREFIX):] or None


def decide(agent: str, gateway_model: str, answer: dict) -> Optional[str]:
    """The refusal reason for a launch, or ``None`` to let it through.

    ``answer`` is :func:`vco_lib.gateway_client.get_json`'s result for
    ``/vendor-keys``. Only a DEFINITE "cannot serve" refuses."""
    if not answer.get("ok"):
        return None  # gateway down / outdated / no token: unknown, not "cannot"
    body = answer.get("payload")
    if not isinstance(body, dict):
        return None
    route = body.get("route")
    problem = str(body.get("problem") or "").strip()
    if route == "refused":
        return (
            f"Not launched: agent '{agent}' pins the gateway model "
            f"'claude-gw/{gateway_model}', and the model gateway does not serve "
            f"that id: {problem or 'unknown model'}. Fix the `model:` line of the "
            "agent's definition (or pick another agent)."
        )
    if route != "vendor" or body.get("usable") is not False:
        return None
    vendor = body.get("vendor") or "?"
    hint = ""
    if any(m in problem for m in _HUB_DOWN_MARKERS):
        hint = (
            " vct-hub was not running, so the OS keychain could not be read; "
            "the gateway has asked VCO to start it — retry this launch in about "
            "15 seconds."
        )
    return (
        f"Not launched: agent '{agent}' runs on 'claude-gw/{gateway_model}' "
        f"through the model gateway, and the gateway cannot resolve a key for "
        f"vendor '{vendor}' right now, so its first request would fail.{hint} "
        f"Gateway said: {problem or 'no key found'}"
    )


def deny_envelope(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _fetch_vendor_keys(model: str) -> dict:
    from vco_lib.gateway_client import get_json

    return get_json(ROUTE, timeout_s=DEFAULT_TIMEOUT_S, query={"model": model})


def run(
    payload: Any,
    *,
    project_dir: Optional[Path],
    fetch: Optional[Callable[[str], dict]] = None,
    environ: Optional[dict] = None,
) -> Optional[dict]:
    """The hook's whole decision: the deny envelope, or ``None``."""
    if preflight_disabled(environ):
        return None
    agent, override = launch_of(payload)
    if agent is None or (not agent and not override):
        return None
    gateway_model = gateway_model_of(agent, override, agent_dirs(project_dir))
    if not gateway_model:
        return None
    reason = decide(agent, gateway_model, (fetch or _fetch_vendor_keys)(gateway_model))
    return deny_envelope(reason) if reason else None


def main(argv: Optional[list[str]] = None) -> int:
    del argv
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    project = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    if not project and isinstance(payload, dict):
        project = str(payload.get("cwd") or "")
    try:
        envelope = run(payload, project_dir=Path(project) if project else None)
    except ImportError as exc:
        # A shipped module failed to import: a broken install. Loud on stderr,
        # but the launch is not refused for it (the probe is not the lane).
        print(f"[vco] gateway_preflight: broken install ({exc}); launch not checked", file=sys.stderr)
        return 0
    if envelope is not None:
        sys.stdout.write(json.dumps(envelope) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
