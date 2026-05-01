"""Shared token-counting helpers for Claude Code transcript JSONL.

Field reliability (verified 2026-05-01 against live transcript + web docs):
  - input_tokens: streaming placeholder, ~75% are 0 or 1 — UNRELIABLE
  - output_tokens: undercounted ~10-17x vs statusbar — UNRELIABLE
  - cache_creation_input_tokens: matches statusbar 1x — RELIABLE
  - cache_read_input_tokens: matches statusbar 1x for cache hits — RELIABLE

Claude Code emits ~3 JSONL entries per actual API request during streaming;
all three entries share the same requestId. Without dedup, totals inflate ~3x.
A 16,355-entry transcript dedups to 6,954 unique requests (57.5% redundant).

Use TranscriptScanner to read a session-total cache_creation count.
Use extract_usage_fields to pull canonical fields from a single message dict.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class UsageFields:
    """Canonical token fields extracted from a Claude Code usage dict."""

    input_tokens: int = 0           # UNRELIABLE — streaming placeholder
    output_tokens: int = 0           # UNRELIABLE — undercounted vs statusbar
    cache_creation_input_tokens: int = 0   # RELIABLE — new context digested
    cache_read_input_tokens: int = 0       # RELIABLE — cache hits


def extract_usage_fields(usage: dict | None) -> UsageFields:
    """Pull the four canonical fields from a usage dict; missing → 0."""
    if not isinstance(usage, dict):
        return UsageFields()
    return UsageFields(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
    )


@dataclass
class ScanResult:
    """Aggregated session totals from a transcript scan."""

    cache_creation_total: int = 0
    cache_read_total: int = 0
    deduped_requests: int = 0
    raw_entries_with_usage: int = 0
    seen_request_ids: set[str] = field(default_factory=set)


class TranscriptScanner:
    """Scan a Claude Code JSONL transcript with requestId dedup.

    Use the `recommended` total (cache_creation_input_tokens, requestId-deduped)
    as the reliable session-work signal. Other fields are still summed for
    callers who want them (e.g. for diagnostics or cost calcs that already
    accept the under-count).

    Typical use:
        scanner = TranscriptScanner()
        result = scanner.scan(transcript_path)
        if result.cache_creation_total >= threshold:
            ...
    """

    MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024  # 256 MB hard cap to avoid OOM

    def scan(
        self,
        transcript_path: str | os.PathLike,
        on_assistant_message: callable | None = None,
    ) -> ScanResult:
        """Scan a transcript file. Returns aggregated totals.

        on_assistant_message: optional callback receiving (entry_dict, msg_dict,
                              running_cache_creation_total). Use for side-channel
                              detection like escape-hatch markers without
                              re-scanning the file.
        """
        result = ScanResult()
        path = os.fspath(transcript_path)
        if not path or not os.path.exists(path):
            return result
        try:
            if os.path.getsize(path) >= self.MAX_TRANSCRIPT_BYTES:
                return result
        except OSError:
            return result

        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    self._process_line(line, result, on_assistant_message)
        except OSError:
            pass

        return result

    def _process_line(
        self,
        line: str,
        result: ScanResult,
        on_assistant_message: callable | None,
    ) -> None:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return

        usage = msg.get("usage")
        if isinstance(usage, dict):
            result.raw_entries_with_usage += 1
            req_id = entry.get("requestId") or msg.get("id") or ""
            is_new_request = (not req_id) or (req_id not in result.seen_request_ids)
            if is_new_request:
                if req_id:
                    result.seen_request_ids.add(req_id)
                fields_ = extract_usage_fields(usage)
                result.cache_creation_total += fields_.cache_creation_input_tokens
                result.cache_read_total += fields_.cache_read_input_tokens
                result.deduped_requests += 1

        if on_assistant_message and msg.get("role") == "assistant":
            on_assistant_message(entry, msg, result.cache_creation_total)


def iter_assistant_text(msg: dict) -> Iterator[str]:
    """Yield top-level text fragments from an assistant message."""
    content = msg.get("content")
    if isinstance(content, str):
        yield content
        return
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text") or ""
                if t:
                    yield t
