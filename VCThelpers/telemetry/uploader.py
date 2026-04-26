# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Batch uploader for queued telemetry events.

Design:
    - Pulls up to BATCH_SIZE oldest un-uploaded events from the queue.
    - POSTs a single JSON body { "events": [...] } to the endpoint.
    - Retries transient failures (connection errors, 5xx, 429 with
      Retry-After) with exponential backoff (1s, 4s, 16s — 3 attempts).
    - On success, marks those ids as uploaded and returns.
    - On permanent failure (4xx other than 429), returns retryable=False
      and leaves rows in the queue so the user can inspect them.
    - Never spawns background threads. Callers are expected to call
      upload_pending() opportunistically (session start / stop hooks,
      once per N enqueues, nightly cron, etc).

Endpoint:
    Default https://api.vibecodedtools.it/telemetry (stub — Fabio deploys).
    Override via VIBECODED_TELEMETRY_URL.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from .queue import TelemetryQueue, get_queue

log = logging.getLogger(__name__)

# Public alias for the telemetry edge function. Internal infra URLs are not
# committed to public source — operators set VIBECODED_TELEMETRY_URL to override.
#
# v0.1.0 status: the upload endpoint is NOT yet deployed (Fabio's pending
# infra task — see docs/TELEMETRY.md). Until it's live, opted-in
# telemetry events are written to ~/.vibecoded/telemetry_pending.jsonl
# instead of posted, so users can see what would have been sent. Set
# VIBECODED_TELEMETRY_URL to a real endpoint when one is deployed.
DEFAULT_URL = "https://api.vibecodedtools.it/telemetry"
BATCH_SIZE = 100
RETRY_DELAYS = (1.0, 4.0, 16.0)  # len = number of retries after the first try
REQUEST_TIMEOUT = 10.0

# The pending file the uploader writes to when DEFAULT_URL is in effect
# (i.e. operator hasn't pointed at a real endpoint yet).
_PENDING_FILE = "telemetry_pending.jsonl"


@dataclass
class UploadResult:
    uploaded_count: int
    error: Optional[str] = None
    retryable: bool = False
    attempts: int = 0
    status_code: Optional[int] = None


def _resolve_endpoint(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    # Treat empty env var as "unset" so callers can clear it explicitly
    # (e.g. tests, or operators wanting the default behavior).
    env_url = os.environ.get("VIBECODED_TELEMETRY_URL", "").strip()
    return env_url or DEFAULT_URL


def _disabled() -> bool:
    """Return True iff telemetry is disabled.

    Default-OFF: an unset/empty VIBECODED_TELEMETRY env var disables uploads.
    Mirror of `collector.telemetry_enabled` so the collector and uploader
    agree on the default — defense-in-depth: even if events were somehow
    enqueued, the uploader still won't ship them without an explicit opt-in.

    Only explicit truthy values ("true", "1", "yes", "on") enable uploads.
    """
    env = os.environ.get("VIBECODED_TELEMETRY", "").strip().lower()
    if env in ("true", "1", "yes", "on"):
        return False
    return True


def _post_json(
    url: str,
    body: bytes,
    timeout: float,
) -> tuple[Optional[int], Optional[bytes], Optional[dict]]:
    """POST JSON. Returns (status_code, body_bytes, headers)."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "vibecoded-telemetry/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            hdrs = dict(resp.headers.items()) if resp.headers else {}
            return resp.status, data, hdrs
    except urllib.error.HTTPError as e:
        hdrs = dict(e.headers.items()) if getattr(e, "headers", None) else {}
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, data, hdrs
    except urllib.error.URLError as e:
        log.debug("Telemetry upload URLError: %s", e)
        return None, None, None
    except (TimeoutError, OSError) as e:
        log.debug("Telemetry upload network error: %s", e)
        return None, None, None


def _parse_retry_after(headers: Optional[dict]) -> Optional[float]:
    if not headers:
        return None
    val = headers.get("Retry-After") or headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _write_pending_jsonl(events: List[dict]) -> int:
    """Append events to ~/.vibecoded/telemetry_pending.jsonl (one JSON
    object per line). Returns count written.

    Used when the default upload URL is still pointing at the pre-launch
    stub endpoint — instead of HTTP-posting (and silently failing or
    spamming an unhealthy endpoint), we persist events to disk so users
    who opted in can inspect exactly what telemetry the orchestrator
    WOULD have shipped. Surfaced via `vct-cli telemetry pending` (TODO).
    """
    from pathlib import Path
    pending_dir = Path.home() / ".vibecoded"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_path = pending_dir / _PENDING_FILE
    written = 0
    with pending_path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str, separators=(",", ":")) + "\n")
            written += 1
    return written


def _is_pre_launch_stub_endpoint(url: str) -> bool:
    """True iff the URL is the not-yet-deployed default endpoint.

    Real custom endpoints set via VIBECODED_TELEMETRY_URL bypass this — the
    operator has explicitly accepted that they're posting somewhere live.
    """
    return url == DEFAULT_URL


def upload_pending(
    endpoint: Optional[str] = None,
    *,
    batch_size: int = BATCH_SIZE,
    queue: Optional[TelemetryQueue] = None,
) -> UploadResult:
    """Upload one batch of pending events.

    Returns UploadResult regardless of outcome. Never raises.

    v0.1.0: when the resolved endpoint is the pre-launch stub
    (DEFAULT_URL), events are written to ~/.vibecoded/telemetry_pending.jsonl
    and marked uploaded in the queue. Real upload resumes once
    VIBECODED_TELEMETRY_URL points at a deployed endpoint.
    """
    if _disabled():
        return UploadResult(uploaded_count=0, error="telemetry_disabled", retryable=False)

    url = _resolve_endpoint(endpoint)
    q = queue or get_queue()

    pending: List[dict] = q.pending_events(limit=batch_size)
    if not pending:
        return UploadResult(uploaded_count=0)

    ids = [e["id"] for e in pending]
    body_obj = {
        "events": [
            {
                "event_type": e["event_type"],
                "created_at": e["created_at"],
                **e["payload"],
            }
            for e in pending
        ],
    }

    # v0.1.0 pre-launch path: divert to pending file, mark queued events
    # as uploaded so they aren't infinitely re-tried, and return a
    # success-shaped result with a marker error code so callers can
    # distinguish from real uploads if they care.
    if _is_pre_launch_stub_endpoint(url):
        try:
            written = _write_pending_jsonl(body_obj["events"])
            marked = q.mark_uploaded(ids)
            return UploadResult(
                uploaded_count=marked,
                error="endpoint_pending_deployment",
                retryable=False,
                attempts=0,
                status_code=None,
            )
        except OSError as e:
            log.debug("Telemetry pending-file write failed: %s", e)
            return UploadResult(
                uploaded_count=0,
                error=f"pending_write_failed: {e}",
                retryable=True,
                attempts=0,
                status_code=None,
            )

    try:
        body = json.dumps(body_obj, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as e:
        log.debug("Telemetry body serialization failed: %s", e)
        return UploadResult(
            uploaded_count=0,
            error=f"serialization: {e}",
            retryable=False,
        )

    attempts = 0
    total_attempts = 1 + len(RETRY_DELAYS)
    last_status: Optional[int] = None
    last_error: Optional[str] = None

    for i in range(total_attempts):
        attempts += 1
        status, resp_body, headers = _post_json(url, body, REQUEST_TIMEOUT)
        last_status = status

        # Network-level failure → retryable.
        if status is None:
            last_error = "network_error"
            if i < total_attempts - 1:
                time.sleep(RETRY_DELAYS[i])
                continue
            return UploadResult(
                uploaded_count=0,
                error=last_error,
                retryable=True,
                attempts=attempts,
                status_code=None,
            )

        # Success: 2xx.
        if 200 <= status < 300:
            marked = q.mark_uploaded(ids)
            return UploadResult(
                uploaded_count=marked,
                error=None,
                retryable=False,
                attempts=attempts,
                status_code=status,
            )

        # Rate-limited.
        if status == 429:
            last_error = "rate_limited"
            retry_after = _parse_retry_after(headers)
            if i < total_attempts - 1:
                delay = retry_after if retry_after is not None else RETRY_DELAYS[i]
                time.sleep(max(0.0, delay))
                continue
            return UploadResult(
                uploaded_count=0,
                error=last_error,
                retryable=True,
                attempts=attempts,
                status_code=status,
            )

        # Server errors → retryable.
        if 500 <= status < 600:
            last_error = f"server_error_{status}"
            if i < total_attempts - 1:
                time.sleep(RETRY_DELAYS[i])
                continue
            return UploadResult(
                uploaded_count=0,
                error=last_error,
                retryable=True,
                attempts=attempts,
                status_code=status,
            )

        # 4xx (other than 429): do NOT retry, body is probably malformed
        # or the endpoint rejected schema. Leave events in queue so the
        # user can inspect via the dashboard CLI.
        body_snippet = ""
        if resp_body:
            try:
                body_snippet = resp_body.decode("utf-8", errors="replace")[:200]
            except Exception:
                body_snippet = "<unparseable>"
        last_error = f"client_error_{status}: {body_snippet}"
        return UploadResult(
            uploaded_count=0,
            error=last_error,
            retryable=False,
            attempts=attempts,
            status_code=status,
        )

    # Should be unreachable, but keep a defensive return.
    return UploadResult(
        uploaded_count=0,
        error=last_error or "unknown",
        retryable=True,
        attempts=attempts,
        status_code=last_status,
    )
