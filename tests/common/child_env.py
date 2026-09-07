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

Tests that deliberately exercise the UNPINNED case (e.g. "does the shim find
the venv on its own?") must say so at the call site with a comment; the
§3.16 straggler grep lists every ``sys.executable`` spawn that bypasses this
helper, and each one needs that justification.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    env.update(overrides)
    return env
