# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``child_env()`` — the ONE way a test builds the environment for a child
Python process (v0.2.92 duplication-merge, PLAN-EXTENSION §3.16).

Why this exists: a test that spawns ``[sys.executable, "-m", "vco_lib.x"]``
(or ``install.py``, or a ``templates/scripts/*.py`` that imports ``vco_lib``)
inherits whatever ``sys.path`` the CHILD computes — not pytest's. On a machine
whose venv holds a non-editable COPY of ``vco_lib`` in ``site-packages``
(the documented dogfood shadow on this repo's dev box), the child imports
the STALE copy and the test measures code that is not in the tree. That is
the "six residual failures" mechanism the plan names: green in one venv,
red in another, and neither result about the checkout.

The fix is to put the repo root FIRST on ``PYTHONPATH`` **and** to pin
``$VCT_ORCHESTRATOR_ROOT`` at it — but a two-liner copied into forty tests is
forty places to forget it. So it lives here, and every subprocess-spawning
test calls it::

    from tests.common.child_env import child_env
    subprocess.run([sys.executable, "-m", "vco_lib.doctor"], env=child_env())

or, as a pytest fixture (``tests/conftest.py`` registers ``child_env``)::

    def test_x(child_env):
        subprocess.run([...], env=child_env)

``PYTHONPATH`` keeps any value the caller's environment already had, AFTER
the repo root, so an outer harness that pins something else still sees it —
but the checkout wins. ``overrides`` are applied last.

**``PYTHONPATH`` alone is not enough** (v0.2.92, found live). Several shipped
scripts resolve their ``vco_lib`` parent from ``$VCT_ORCHESTRATOR_ROOT`` and
insert it at ``sys.path[0]`` — *ahead* of ``PYTHONPATH``, which is exactly what
that ordering is for in production. A child inheriting the developer's value
therefore imports a DIFFERENT CHECKOUT no matter what we put on ``PYTHONPATH``:
on this repo's dev box that variable names the dogfood fork, which lags the
public tree. So it is pinned here too.

The loud version is a ``ModuleNotFoundError`` for a module that plainly exists
in the tree. The quiet version is worse and has already happened twice this
cycle: when the two checkouts merely DISAGREE — a constant retuned in one and
not the other — nothing raises, and the test simply measures the wrong tree.
An audit read a stale 13 500-token chunk budget through this leak and nearly
filed a defect against code that was already fixed.

A test that deliberately wants the inherited value passes it explicitly:
``child_env(VCT_ORCHESTRATOR_ROOT=os.environ["VCT_ORCHESTRATOR_ROOT"])`` —
overrides are applied last, so opting out is possible but must be written down.

**``KG_BASE_DIR`` is pinned at a throwaway directory** (v0.2.94), for a
DIFFERENT reason, and it is a containment fix rather than a correctness one.
``$VCT_ORCHESTRATOR_ROOT`` above is read by shipped code for two unrelated
purposes: locating ``vco_lib`` (why we pin it) and answering "which project
root is this?" — and the second answer decides which project's deferral ledger
gets reconciled. ``EmbeddingService.for_project()`` reconciles that ledger on
BOTH its paths (``_clear_failure_deferral`` on success,
``_write_failure_deferral`` on failure), and a reconcile REWRITES
``<root>/CLAUDE.md``: the ``vco-deferral-reminder`` block is spliced in when
entries exist and stripped when none do. So every child that touched an
embedding backend was editing this checkout's TRACKED ``CLAUDE.md`` — measured
at four such children per full-suite run, which is where the stray
``M CLAUDE.md`` in a mid-run ``git status`` came from.

``KG_BASE_DIR`` is the precise lever: ``embedding_service._detect_project_root``
consults it BEFORE ``$VCT_ORCHESTRATOR_ROOT``, so pinning it reroutes the
project-root answer while leaving the import pin — the whole point of this
helper — untouched. The directory is per-process and empty; a child that
resolves a KG file path relative to it writes into the throwaway dir instead of
the working tree, which is the same containment by a second route.

Unlike ``$VCT_ORCHESTRATOR_ROOT``, this key is pinned ONLY when ``base`` is
omitted — i.e. only when we are handing the child THIS process's environment,
which is the case that leaks. A caller that BUILT the mapping owns what is in
it: ``tests/test_v0289_kg_sync_project_root.py`` passes ``_base_env(...)`` to
assert the sync script's root-precedence ladder, and two of its cases depend on
the child seeing a specific ``KG_BASE_DIR`` — or none at all, for the
"script location fallback" rung. Pinning over that made both untestable. As
always, ``overrides`` wins last.

Tests that deliberately exercise the UNPINNED case (e.g. "does the shim find
the venv on its own?") must say so at the call site with a comment; the
§3.16 straggler grep lists every ``sys.executable`` spawn that bypasses this
helper, and each one needs that justification.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Lazily-created, one per test process. Module-level rather than per call:
#: ``child_env`` is called thousands of times in a full run and a temp dir per
#: call would be thousands of directories.
_KG_BASE_SENTINEL: Optional[str] = None


def _kg_base_sentinel() -> str:
    """An empty throwaway directory to stand in as the child's project root."""
    global _KG_BASE_SENTINEL
    if _KG_BASE_SENTINEL is None:
        _KG_BASE_SENTINEL = tempfile.mkdtemp(prefix="vco-child-kg-base-")
    return _KG_BASE_SENTINEL


def child_env(
    base: Optional[Mapping[str, str]] = None, /, **overrides: str,
) -> dict[str, str]:
    """``base`` (default ``os.environ``) with the repo root FIRST on
    ``PYTHONPATH``, then ``overrides``. Returns a fresh dict every call."""
    env = dict(os.environ if base is None else base)
    prior = env.get("PYTHONPATH", "")
    root = str(REPO_ROOT)
    parts = [root] + [p for p in prior.split(os.pathsep) if p and p != root]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # Beats PYTHONPATH in the shipped scripts that read it — see the module
    # docstring. Set BEFORE `overrides` so a caller can still opt out.
    env["VCT_ORCHESTRATOR_ROOT"] = root
    # Keeps the child's project-root answer (and so its deferral-ledger writes,
    # and so this checkout's tracked CLAUDE.md) out of the working tree — see
    # the module docstring. Only for the inherited-environment case: a caller
    # that built `base` itself owns the root channels in it.
    if base is None:
        env["KG_BASE_DIR"] = _kg_base_sentinel()
    env.update(overrides)
    return env
