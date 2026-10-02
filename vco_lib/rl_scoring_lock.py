# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Loader for ``rl_scoring_lock.toml`` — the one home of the RL scoring lock
(v0.2.100, W5R-02).

Tier (B) of the A>B>C rule: ONE committed table that Rust embeds at compile
time (``vct-launcher-core/src/rl_scoring_lock.rs``) and Python parses here.
The launcher GUI never holds its own copy; it reads the value the Rust side
serves (``ModuleEnableState.lock_reason`` / the ``rl_scoring_lock`` command).

The lock gates RL SCORING only. Event logging never consults it.

Failure mode
------------
:func:`load_rl_scoring_lock` raises ``RuntimeError`` on a missing, unreadable
or malformed table — vco_lib ships with every healthy install, so that is a
BROKEN install and must be loud. The scoring gate that calls
:func:`rl_scoring_lock_reason` treats such a failure as LOCKED (scoring off):
when the precondition "scoring is allowed" cannot be positively confirmed, the
conservative answer is not to rerank. Search itself is never affected.
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_DEFAULT_TABLE_PATH: Path = Path(__file__).resolve().parent / "rl_scoring_lock.toml"

_SUPPORTED_FORMAT_VERSION = 1

#: Module id whose scoring the lock governs. Same literal as the Rust
#: ``RL_RERANKER_MODULE_ID``.
RL_RERANKER_MODULE_ID = "vct-rl-reranker"


def table_path() -> Path:
    """Absolute path to the committed lock table."""
    return _DEFAULT_TABLE_PATH


def load_rl_scoring_lock(path: Path | None = None) -> dict[str, Any]:
    """Parse and validate the lock table. Returns ``{"locked": bool, "reason": str}``.

    Raises ``RuntimeError`` when the file is missing, unparseable, of an
    unsupported ``format_version``, or has a non-boolean ``locked`` / a
    missing ``reason`` while locked.
    """
    p = path or _DEFAULT_TABLE_PATH
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(
            f"vco_lib RL scoring lock table unreadable at {p}: {exc}. "
            "This is a broken install; re-run the orchestrator update."
        ) from exc
    if raw.get("format_version") != _SUPPORTED_FORMAT_VERSION:
        raise RuntimeError(
            f"{p}: unsupported format_version {raw.get('format_version')!r} "
            f"(this loader reads {_SUPPORTED_FORMAT_VERSION})."
        )
    locked = raw.get("locked")
    if not isinstance(locked, bool):
        raise RuntimeError(f"{p}: `locked` must be a boolean, got {locked!r}.")
    reason = raw.get("reason")
    if not isinstance(reason, str) or (locked and not reason.strip()):
        raise RuntimeError(f"{p}: `reason` must be a non-empty string while locked.")
    return {"locked": locked, "reason": reason}


@lru_cache(maxsize=1)
def _shipped() -> dict[str, Any]:
    return load_rl_scoring_lock()


def rl_scoring_lock_reason() -> Optional[str]:
    """The reason RL scoring is locked off, or ``None`` when it is not locked.

    Raises ``RuntimeError`` (see module docstring) when the shipped table is
    broken; callers on a best-effort path treat that as locked.
    """
    lock = _shipped()
    return lock["reason"] if lock["locked"] else None


def rl_scoring_locked() -> bool:
    """True while RL scoring is locked off."""
    return rl_scoring_lock_reason() is not None
