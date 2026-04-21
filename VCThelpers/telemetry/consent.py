# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 VibeCoded Tools
"""First-launch consent flow for opt-in telemetry categories.

Consent is written to ~/.vibecoded/config.json and never re-prompted once
the version marker is present. Users can edit that file manually for
per-category granularity; the interactive prompt is binary (accept-all or
deny-all) to minimize friction.

Never blocks non-interactive runs (CI, cron, piped stdin) — defaults to
'always-on only' with no opt-in categories.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".vibecoded"
CONFIG_FILE = CONFIG_DIR / "config.json"

CONSENT_VERSION = "1.0"

# Categories beyond the always-on baseline.
OPTIN_CATEGORIES = ("rl_data", "routing_data", "instinct_data", "hardware")

_PROMPT_TEXT = """\
VibeCoded Tools collects:
  - License validation status (hashed machine ID only)       [ALWAYS]
  - Performance metrics (latency, error rates)               [ALWAYS]
  - Hardware specs (CPU, RAM, GPU — paired with exec times)  [OPT-IN]
  - RL training data (embeddings + similarity, no code/text) [OPT-IN]
  - Routing decisions metadata (agent selection, outcomes)   [OPT-IN]
  - Tool usage patterns (tool names + timing, no content)    [OPT-IN]

Full details: vibecodedtools.it/privacy
Opt out entirely: set VIBECODED_TELEMETRY=false in your environment

Accept opt-in items? [Y/n]: """


def _default_consent(accept_optin: bool) -> Dict[str, Any]:
    """Build a consent record with all opt-in flags set to `accept_optin`."""
    return {
        "consent_version": CONSENT_VERSION,
        "granted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "always_on": True,
        **{cat: bool(accept_optin) for cat in OPTIN_CATEGORIES},
    }


def load_consent() -> Dict[str, Any]:
    """Load consent flags from ~/.vibecoded/config.json.

    Returns a dict with always_on=True and all opt-in flags defaulting to
    False if the file is missing or unreadable. Never raises.
    """
    if not CONFIG_FILE.exists():
        return _default_consent(accept_optin=False)
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log.debug("Could not read consent file: %s", e)
        return _default_consent(accept_optin=False)

    # Backfill missing keys with conservative defaults.
    merged = _default_consent(accept_optin=False)
    for k, v in data.items():
        merged[k] = v
    return merged


def _save_consent(consent: Dict[str, Any]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_FILE.open("w", encoding="utf-8") as fh:
            json.dump(consent, fh, indent=2, sort_keys=True)
    except OSError as e:
        log.debug("Could not save consent file: %s", e)


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def prompt_consent_if_needed(*, force: bool = False) -> Dict[str, Any]:
    """Prompt the user for opt-in consent on first launch.

    Behavior:
        - If VIBECODED_TELEMETRY=false → writes always-on-only consent and
          returns immediately (opt-in flags still False; collector will
          short-circuit anyway).
        - If config file already has current consent_version → returns it
          unchanged (no re-prompt) unless `force=True`.
        - If stdin is not a tty → writes always-on-only consent (no block).
        - Otherwise, print prompt and read one line. Any answer starting
          with 'y' (or empty / Enter) is treated as accept-all; 'n' as
          deny-all. Invalid input defaults to deny-all.

    Returns the consent dict that was ultimately persisted.
    """
    env_opt_out = os.environ.get("VIBECODED_TELEMETRY", "").strip().lower()
    if env_opt_out in ("false", "0", "no", "off"):
        consent = _default_consent(accept_optin=False)
        _save_consent(consent)
        return consent

    existing = load_consent()
    if not force and existing.get("consent_version") == CONSENT_VERSION and CONFIG_FILE.exists():
        return existing

    if not _stdin_is_interactive():
        consent = _default_consent(accept_optin=False)
        _save_consent(consent)
        log.debug("Non-interactive stdin — defaulting to always-on-only telemetry.")
        return consent

    try:
        print(_PROMPT_TEXT, end="", flush=True)
        answer = sys.stdin.readline().strip().lower()
    except (OSError, KeyboardInterrupt):
        answer = "n"

    accept = answer in ("", "y", "yes")
    consent = _default_consent(accept_optin=accept)
    _save_consent(consent)

    if accept:
        print("Thanks — opt-in categories enabled. Edit ~/.vibecoded/config.json to change.")
    else:
        print("OK — only always-on telemetry will be sent. Edit ~/.vibecoded/config.json to change.")
    return consent
