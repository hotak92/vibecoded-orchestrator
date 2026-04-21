# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 VibeCoded Tools
"""Telemetry collection for VibeCoded Tools Orchestrator.

Principles:
    - Always-on telemetry is the bare minimum for license validation and
      product health (OS, Python version, orchestrator version, session
      timestamps, license validation status). Never the license key itself.
    - Everything else is opt-in via first-launch consent prompt, written
      to ~/.vibecoded/config.json.
    - User can disable everything with VIBECODED_TELEMETRY=false env var.
    - Events are queued locally (SQLite) and uploaded in batches, so
      collection never blocks the orchestrator or fails on network error.
    - PII scrubbing happens at the collection layer, never at upload.
      No query text, code content, file paths with usernames, or tokens
      ever enter the queue.

Public entry points:
    collect_session_start()           — always-on session heartbeat
    collect_rl_retrieval(...)         — opt-in: embedding similarity data
    collect_qlearning_routing(...)    — opt-in: routing decisions metadata
    collect_instinct_event(...)       — opt-in: tool-use behavioral data
    collect_hardware()                — opt-in: CPU/RAM/GPU profile

    prompt_consent_if_needed()        — first-launch consent flow
    upload_pending(endpoint)          — batch upload (call from hook)

    TelemetryEvent                    — event schema
    TelemetryQueue                    — local SQLite queue
"""
from __future__ import annotations

from .collector import (
    TelemetryEvent,
    collect_hardware,
    collect_instinct_event,
    collect_qlearning_routing,
    collect_rl_retrieval,
    collect_session_start,
    telemetry_enabled,
)
from .consent import load_consent, prompt_consent_if_needed
from .queue import TelemetryQueue, get_queue
from .uploader import UploadResult, upload_pending

__all__ = [
    "TelemetryEvent",
    "TelemetryQueue",
    "UploadResult",
    "collect_hardware",
    "collect_instinct_event",
    "collect_qlearning_routing",
    "collect_rl_retrieval",
    "collect_session_start",
    "get_queue",
    "load_consent",
    "prompt_consent_if_needed",
    "telemetry_enabled",
    "upload_pending",
]
