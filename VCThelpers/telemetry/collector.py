# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Collection layer for telemetry events.

Every `collect_*` helper:
    - checks the telemetry feature flag (VIBECODED_TELEMETRY env + consent),
    - builds a TelemetryEvent with scrubbed payload,
    - enqueues it to the local SQLite queue.

No collector spawns threads or does network I/O. Uploads are scheduled
externally (see uploader.upload_pending).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import platform
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# Local imports — kept at module level but used behind feature checks so
# import cost is negligible for telemetry-disabled runs.
from .consent import load_consent
from .queue import get_queue

# ---- version ------------------------------------------------------------

try:
    # If a top-level version module exists, use it. Otherwise fall back.
    from VCThelpers import __version__ as _COMMERCIAL_VERSION  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - optional attribute
    _COMMERCIAL_VERSION = None


def _orchestrator_version() -> str:
    if _COMMERCIAL_VERSION:
        return str(_COMMERCIAL_VERSION)
    return os.environ.get("VIBECODED_VERSION", "0.0.0-dev")


# ---- event schema -------------------------------------------------------


@dataclass
class TelemetryEvent:
    """Schema for every event enqueued by a collect_* helper.

    Fields not relevant to a given event_type are simply left empty / None.
    The uploader serializes the `payload` dict; the outer fields
    (event_type, timestamp, orchestrator_version, etc.) are promoted to
    columns server-side for indexing.
    """
    event_type: str
    payload: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat())
    machine_hash: str = ""
    orchestrator_version: str = ""
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---- feature flag -------------------------------------------------------


def telemetry_enabled(category: Optional[str] = None) -> bool:
    """Return True if telemetry is enabled globally (and for this category).

    Default-OFF policy (Bug: README promises "no telemetry unless you opt in"):

        1. VIBECODED_TELEMETRY=false (or 0/no/off) → everything OFF.
        2. VIBECODED_TELEMETRY=true (or 1/yes/on) → opt-in. No category given:
           always-on events pass. Category given: still gated by per-category
           consent in ~/.vibecoded/config.json.
        3. VIBECODED_TELEMETRY unset/empty → treated as OFF (default-OFF).
           Match `_disabled()` in uploader.py for consistent defense-in-depth.

    The VCT Launcher install flow surfaces an explicit consent prompt
    ("Enable anonymous telemetry to help us improve?"). The default answer is
    No; the .env file is written with VIBECODED_TELEMETRY=false unless the
    user explicitly opts in.
    """
    env = os.environ.get("VIBECODED_TELEMETRY", "").strip().lower()
    if env in ("false", "0", "no", "off", ""):
        return False
    # env in {"true", "1", "yes", "on"} or any other truthy → opt-in active.
    if category is None:
        return True
    consent = load_consent()
    return bool(consent.get(category, False))


# ---- PII scrubbing ------------------------------------------------------

# Run all patterns over every collected string. Order matters: scrub paths
# before emails (usernames may appear inside both).

_USER_HOME_PATTERN = re.compile(
    r"(?P<prefix>/home/|/Users/|C:\\Users\\|/root/)([^/\\\s]+)",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# Common tokens: GitHub PAT, OpenAI, Anthropic, generic bearer + jwt.
_TOKEN_PATTERNS = [
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\bey[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),  # JWT
    re.compile(r"(?i)(?:bearer\s+)[A-Za-z0-9\-_\.=]{16,}"),
]
# IPv4 and IPv6.
_IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_PATTERN = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b")

_MAX_ARGS_LEN = 500


def _scrub_pii(text: str) -> str:
    """Replace common PII/secret patterns with redaction markers.

    Never raises. Input that isn't a str is coerced via str().
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return "<redacted>"

    # 1. User-home paths → /home/<user>/ etc.
    text = _USER_HOME_PATTERN.sub(r"\g<prefix><user>", text)
    # 2. Emails.
    text = _EMAIL_PATTERN.sub("<email>", text)
    # 3. Tokens (check each pattern in order).
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("<token>", text)
    # 4. IPs.
    text = _IPV4_PATTERN.sub("<ip>", text)
    text = _IPV6_PATTERN.sub("<ip>", text)
    return text


def _scrub_args(args: Any, max_len: int = _MAX_ARGS_LEN) -> str:
    """Summarize tool args into a scrubbed, length-limited string.

    Accepts dicts, lists, strings, or anything stringifiable. Structural
    details (keys) survive; values get scrubbed and the whole thing is
    truncated.
    """
    try:
        if isinstance(args, dict):
            # Keep keys (non-sensitive structural info) but scrub values.
            parts = []
            for k, v in args.items():
                parts.append(f"{k}={_scrub_pii(str(v))}")
            out = ", ".join(parts)
        elif isinstance(args, (list, tuple)):
            out = ", ".join(_scrub_pii(str(x)) for x in args)
        else:
            out = _scrub_pii(str(args))
    except Exception:
        return "<redacted>"
    if len(out) > max_len:
        out = out[: max_len - 3] + "..."
    return out


def _hash_session_id(session_id: str) -> str:
    """Stable sha256 hash of a session id. Never logs the raw value."""
    if not session_id:
        return ""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


# ---- common event envelope ---------------------------------------------


def _machine_hash() -> str:
    """Share the same hash algorithm as the license validator."""
    node = uuid.getnode()
    return hashlib.sha256(node.to_bytes(8, "big")).hexdigest()


def _envelope(event_type: str, payload: Dict[str, Any]) -> TelemetryEvent:
    return TelemetryEvent(
        event_type=event_type,
        payload=payload,
        machine_hash=_machine_hash(),
        orchestrator_version=_orchestrator_version(),
        os_name=platform.system() or "",
        os_version=platform.release() or "",
        python_version=platform.python_version() or "",
    )


def _enqueue(event: TelemetryEvent) -> bool:
    try:
        q = get_queue()
        return q.enqueue(event.event_type, event.to_dict())
    except Exception as e:
        log.debug("Telemetry enqueue error: %s", e)
        return False


# ---- always-on ---------------------------------------------------------


def collect_session_start(
    *,
    license_valid: Optional[bool] = None,
    license_tier: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Always-on session heartbeat.

    Collects: OS, Python, orchestrator version, machine hash, license
    validation status (NOT the key itself), and a hashed session id.
    Respects VIBECODED_TELEMETRY=false (disables everything).
    """
    if not telemetry_enabled():
        return False

    payload: Dict[str, Any] = {
        "license_valid": bool(license_valid) if license_valid is not None else None,
        "license_tier": license_tier or "",
        "session_id_hash": _hash_session_id(session_id or ""),
        "started_at": time.time(),
    }
    return _enqueue(_envelope("session_start", payload))


# ---- opt-in: RL retrieval ----------------------------------------------


def _round_vec(vec: Sequence[float], ndigits: int = 4) -> List[float]:
    try:
        return [round(float(x), ndigits) for x in vec]
    except (TypeError, ValueError):
        return []


def collect_rl_retrieval(
    query_emb: Sequence[float],
    node_embs: Sequence[Sequence[float]],
    scores: Sequence[float],
    latency_ms: float,
    *,
    result_count: Optional[int] = None,
) -> bool:
    """Opt-in: RL retrieval training data.

    Collects embeddings (rounded to 4 decimals) + similarity scores +
    latency. Never any query text, code, or file paths.
    """
    if not telemetry_enabled("rl_data"):
        return False

    try:
        q = _round_vec(query_emb)
        nodes = [_round_vec(v) for v in node_embs]
        sc = [round(float(s), 6) for s in scores]
    except Exception as e:
        log.debug("RL retrieval scrub failed: %s", e)
        return False

    payload = {
        "query_embedding": q,
        "node_embeddings": nodes,
        "scores": sc,
        "result_count": int(result_count if result_count is not None else len(nodes)),
        "latency_ms": float(latency_ms),
    }
    return _enqueue(_envelope("rl_retrieval", payload))


# ---- opt-in: Q-learning routing ----------------------------------------


def collect_qlearning_routing(
    *,
    task_type: str,
    chosen_agent: str,
    outcome: str,
    reward_signal: float,
    model_tier: str,
    available_agents: Optional[Sequence[str]] = None,
    agent_load: Optional[Dict[str, float]] = None,
    task_complexity_estimate: Optional[float] = None,
    routing_latency_ms: Optional[float] = None,
    fallback_used: bool = False,
) -> bool:
    """Opt-in: routing decision metadata.

    Generous metadata intended as training data for a future routing NN.
    Strings (task_type, chosen_agent, outcome, model_tier) are not scrubbed
    — callers are expected to pass enum-like labels, not user content.
    If you need to pass free text, scrub it first.
    """
    if not telemetry_enabled("routing_data"):
        return False

    payload = {
        "task_type": str(task_type),
        "chosen_agent": str(chosen_agent),
        "outcome": str(outcome),
        "reward_signal": float(reward_signal),
        "model_tier": str(model_tier),
        "available_agents": [str(a) for a in (available_agents or [])],
        "agent_load": {str(k): float(v) for k, v in (agent_load or {}).items()},
        "task_complexity_estimate": (
            float(task_complexity_estimate) if task_complexity_estimate is not None else None
        ),
        "routing_latency_ms": (
            float(routing_latency_ms) if routing_latency_ms is not None else None
        ),
        "fallback_used": bool(fallback_used),
    }
    return _enqueue(_envelope("qlearning_routing", payload))


# ---- opt-in: Instinct pipeline -----------------------------------------


def collect_instinct_event(
    *,
    tool_name: str,
    args: Any,
    outcome: str,
    session_id: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> bool:
    """Opt-in: tool-use behavioral event.

    `args` is summarized + scrubbed via _scrub_args (PII stripped,
    truncated to 500 chars). `tool_name` and `outcome` are passed through
    as-is — callers should use enum-like values.
    """
    if not telemetry_enabled("instinct_data"):
        return False

    payload = {
        "tool_name": str(tool_name),
        "args_summary": _scrub_args(args),
        "outcome": str(outcome),
        "session_id_hash": _hash_session_id(session_id or ""),
        "duration_ms": (float(duration_ms) if duration_ms is not None else None),
        "ts": time.time(),
    }
    return _enqueue(_envelope("instinct_event", payload))


# ---- opt-in: Hardware --------------------------------------------------


def collect_hardware(*, force: bool = False) -> bool:
    """Opt-in: hardware profile snapshot.

    Runs at most once per week via hardware.detect_hardware cache unless
    `force=True`. Pairs well with execution-time events (enqueued
    separately by the caller — this function just stores the profile).
    """
    if not telemetry_enabled("hardware"):
        return False

    try:
        from .hardware import detect_hardware
        data = detect_hardware(use_cache=not force)
    except Exception as e:
        log.debug("Hardware probe failed: %s", e)
        return False

    return _enqueue(_envelope("hardware_profile", {"hardware": data}))
