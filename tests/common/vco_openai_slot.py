# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Give VCO a key in its OWN OpenAI slot for one test — the ONE home (v0.2.98).

VCO's consumers resolve the shared ``openai_api_key`` slot and NOTHING else
(owner ruling 2026-09-26): ``$OPENAI_API_KEY`` is not read, a per-project
secret binding is not read, and a project's own ``.env`` is not read. So a
test that needs the OpenAI path to be *reachable* has to seed the slot;
setting the env var has no effect, and a test still setting it is pinning the
OLD rule by accident.

Two forms, both hermetic and both usable from a ``unittest.TestCase`` (this
repo's suite is mostly unittest, where the ``monkeypatch`` fixture is not
available):

    with vco_openai_slot("sk-slot-canary"):
        ...        # the OpenAI path is reachable, with no env var in sight

    with no_vco_openai_slot():
        ...        # VCO provably has no OpenAI key, whatever the env says

Each call gets its own ``VCT_SECRETS_DIR``, so nothing reaches
``~/.vct-secrets``. Both clear ``vco_lib.openai_key``'s per-project resolution
cache AND its once-per-process warning latch on entry and on exit: the
resolver answers once per project per process (a cached answer would leak
into the next test), and the warning is deliberately once-per-process, so a
test that observes it must not silence every later one.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

__all__ = ["vco_openai_slot", "no_vco_openai_slot"]


@contextlib.contextmanager
def _slot(value: str | None) -> Iterator[Path]:
    """Point VCO's slot at a fresh tmp store (optionally holding ``value``)."""
    from vco_lib import openai_key

    with tempfile.TemporaryDirectory(prefix="vco-openai-slot-") as td:
        root = Path(td)
        (root / "shared").mkdir(parents=True, exist_ok=True)
        if value is not None:
            stored = root / "shared" / openai_key.OPENAI_SECRET_NAME
            stored.write_text(value)
            stored.chmod(0o600)
        openai_key._resolved.clear()
        openai_key._warned_env_var = False
        try:
            with patch.dict(os.environ, {"VCT_SECRETS_DIR": str(root)}):
                yield root
        finally:
            openai_key._resolved.clear()
            openai_key._warned_env_var = False


@contextlib.contextmanager
def vco_openai_slot(value: str) -> Iterator[Path]:
    """VCO's slot holds ``value`` for the duration. Yields the store root."""
    with _slot(value) as root:
        yield root


@contextlib.contextmanager
def no_vco_openai_slot() -> Iterator[Path]:
    """VCO's slot is empty for the duration. Yields the (empty) store root."""
    with _slot(None) as root:
        yield root
