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

Why this is the SUPPORTED path, not a fallback to a second copy (review
V02103-FINAL2 I-3, the "a failed import is a broken install" rule): the
editable install (``pip install -e claude_mcp_servers/``) only teaches the
VENV's interpreter where the package is, and what it points at is these very
files in this checkout. The system interpreter that runs ``install.py`` /
``install-bundle`` before (or outside) the venv resolves ``vco_lib`` the same
way — from the checkout on ``sys.path`` — so resolving the gateway package
from the same checkout serves the same bytes the venv would; it cannot mask a
stale or different copy. Removing the append would re-break the field case
above; there is no other interpreter-independent way to reach the package.

What a broken install looks like here — the shipped package directory missing
from the checkout — is LOUD: :func:`ensure_importable` raises
:class:`ImportError` naming the path and the remedy, instead of returning and
leaving the caller to a bare ``ModuleNotFoundError``.
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
    """Make ``import model_router`` resolve; returns True.

    Already importable → untouched. Else, when ``<checkout>/claude_mcp_servers
    /model_router/__init__.py`` exists, its parent is appended to ``sys.path``.
    Raises :class:`ImportError` when the package still cannot be found — the
    package is shipped, so that is a broken install, never a case to skip.
    """
    if importlib.util.find_spec("model_router") is not None:
        return True
    pkg = Path(checkout_root or _CHECKOUT_ROOT) / GATEWAY_PACKAGE_REL
    if (pkg / "__init__.py").is_file():
        parent = str(pkg.parent)
        if parent not in sys.path:
            sys.path.append(parent)
        importlib.invalidate_caches()
        if importlib.util.find_spec("model_router") is not None:
            return True
    raise ImportError(
        f"broken VCO install: the model gateway package is not importable and "
        f"{pkg} is missing or unusable — re-run the orchestrator update "
        f"(`python install.py --update` or the launcher's Update)",
        name="model_router",
    )


__all__ = ["GATEWAY_PACKAGE_REL", "ensure_importable"]
