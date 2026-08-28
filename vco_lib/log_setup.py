# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared logging bootstrap for VCO's Python entry points (v0.2.91 Decision #21).

The global preference ``logging.level`` is projected by vct-hub into the
``VCO_LOG_LEVEL`` env var (values ``error|warn|info|debug``, case-insensitive;
the env var may be absent). This module is the ONE place that parses
``VCO_LOG_LEVEL`` into a stdlib :mod:`logging` level and wraps
``logging.basicConfig`` so every Python entry point in ``vco_lib/`` and
``claude_mcp_servers/`` honors the pref, instead of each call site
re-deriving the same parse (or ignoring the pref entirely, which is what the
pre-v0.2.91 hardcoded ``logging.basicConfig(level=logging.INFO)`` call sites
did).

SCOPE — DIAGNOSTICS ONLY. This governs stderr/stdout diagnostic logging.
It must NEVER be wired into the ``rl_events`` telemetry pipeline or the
audit/jsonl trails — those are data, not diagnostics, and are never
level-gated. Do not import this module from anything under ``rl_events``,
``rl_retention``, or ``rl_logger``.

Usage — a drop-in replacement for the old call sites::

    # before:
    logging.basicConfig(level=logging.INFO)

    # after:
    from vco_lib.log_setup import configure_logging
    configure_logging()

A call site that previously passed extra ``logging.basicConfig`` kwargs
(``format=``, ``stream=``, ...) keeps passing them through unchanged::

    configure_logging(format="%(levelname)s %(message)s")

``VCO_LOG_LEVEL`` absent, empty, or set to an unrecognized value falls
through to the caller-supplied ``default`` (``logging.INFO`` at every
existing call site this module replaces), preserving the pre-v0.2.91
observable output shape when the pref isn't set.
"""

from __future__ import annotations

import logging
import os

__all__ = ["configure_logging", "resolve_level", "VCO_LOG_LEVEL_ENV_VAR"]

#: Name of the env var vct-hub projects the ``logging.level`` global pref
#: into. MUST match the Rust-side projection (``launcher/src-tauri``) and the
#: shell/PowerShell resolver templates — see plan §F Decision #21.
VCO_LOG_LEVEL_ENV_VAR = "VCO_LOG_LEVEL"

#: Recognized values (case-insensitive) -> stdlib logging level. "warning" is
#: accepted as a defensive alias for "warn" (the documented canonical value)
#: since it's the stdlib's own level name and a plausible typo/habit.
_LEVEL_MAP = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def resolve_level(default: int = logging.INFO) -> int:
    """Resolve the effective logging level from ``VCO_LOG_LEVEL``.

    Falls back to ``default`` when the env var is absent, empty, or holds a
    value outside ``error|warn|info|debug`` (case-insensitive). Never raises.
    """
    raw = os.environ.get(VCO_LOG_LEVEL_ENV_VAR)
    if not raw:
        return default
    return _LEVEL_MAP.get(raw.strip().lower(), default)


def configure_logging(default: int = logging.INFO, **basic_config_kwargs) -> None:
    """Configure the root logger, honoring the ``VCO_LOG_LEVEL`` pref.

    Drop-in replacement for ``logging.basicConfig(level=logging.INFO, ...)``
    at VCO's Python entry points: resolves the level from ``VCO_LOG_LEVEL``
    (falling back to ``default`` when absent/invalid) and forwards every
    other kwarg (``format=``, ``stream=``, ``force=``, ...) unchanged to
    :func:`logging.basicConfig`, so each call site keeps its own format
    string / stream exactly as it had it pre-migration.

    Idempotent the same way ``logging.basicConfig`` itself is: per the
    stdlib contract, a call is a no-op if the root logger already has
    handlers, unless ``force=True`` is passed through. Safe to call twice.
    """
    level = resolve_level(default)
    logging.basicConfig(level=level, **basic_config_kwargs)
