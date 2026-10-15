# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where the model gateway package lives in a checkout, and making it
importable from an interpreter that is not the orchestrator venv (v0.2.103).

Field (fresh Windows install): install printed "model gateway not importable
(ModuleNotFoundError)" and held back every gateway agent definition. Not a
missing optional module and not a broken import: ``install.py`` — and the
``install-bundle`` child it spawns with ``sys.executable`` — runs under the
SYSTEM interpreter the user started it with. ``model_router`` is installed
into the orchestrator venv only (``pip install -e claude_mcp_servers/``), so
that interpreter cannot see it. The same holds on every OS.

The parts VCO's install-time code imports (``model_router.vendors``,
``.secrets``, ``.routing``) are stdlib-only by design (``model_router``'s
own package docstring), so :func:`ensure_importable` puts the checkout's
``claude_mcp_servers/`` on ``sys.path`` — APPENDED, so a venv's installed
copy always wins — whenever ``model_router`` is not already importable.
A checkout without the package (it is shipped, so that is a broken tree)
changes nothing and the caller's import still fails loudly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

#: The gateway package directory, relative to an orchestrator checkout.
GATEWAY_PACKAGE_REL = Path("claude_mcp_servers") / "model_router"

#: This checkout (``vco_lib/`` is a direct child of it).
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


def ensure_importable(checkout_root: Optional[Path] = None) -> bool:
    """Make ``import model_router`` resolve; True when it now can.

    Already importable → untouched. Else, when ``<checkout>/claude_mcp_servers
    /model_router/__init__.py`` exists, its parent is appended to ``sys.path``.
    """
    if importlib.util.find_spec("model_router") is not None:
        return True
    pkg = Path(checkout_root or _CHECKOUT_ROOT) / GATEWAY_PACKAGE_REL
    if not (pkg / "__init__.py").is_file():
        return False
    parent = str(pkg.parent)
    if parent not in sys.path:
        sys.path.append(parent)
    importlib.invalidate_caches()
    return importlib.util.find_spec("model_router") is not None


__all__ = ["GATEWAY_PACKAGE_REL", "ensure_importable"]
