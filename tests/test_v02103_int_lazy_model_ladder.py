# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration pin (v0.2.103): the ladder's VRAM guard resolves the summary
model lazily.

summary-venv made ``OLLAMA_DEFAULT_MODEL`` a first-use attribute (PEP 562
module ``__getattr__``), and the summary ladder's ``_ollama_local_deferred``
was written against the old eager global. A bare in-module name is a global
lookup that ``__getattr__`` never sees, so on a fresh import the guard raised
``NameError`` — hidden by every ladder test, which monkeypatches the global
into existence first. The guard must call ``ollama_default_model()``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SB_PATH = Path(__file__).resolve().parents[1] / "templates" / "scripts" / "summary_backends.py"


def _fresh_module():
    spec = importlib.util.spec_from_file_location("sb_lazy_pin", _SB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sb_lazy_pin"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_vram_guard_resolves_the_model_without_the_eager_global(monkeypatch):
    mod = _fresh_module()
    mod.__dict__.pop("OLLAMA_DEFAULT_MODEL", None)  # the fresh-import state
    monkeypatch.setattr(mod, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(mod, "_free_vram_mb", lambda: None)
    monkeypatch.setattr(mod, "ollama_default_model", lambda: "pinned-model")
    probed = []
    monkeypatch.setattr(mod, "_ollama_model_resident", lambda m: probed.append(m) or False)

    assert mod._ollama_local_deferred() is None
    assert probed == ["pinned-model"]
