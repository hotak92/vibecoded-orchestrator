# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco doctor`` — the on-demand invocation point of the WP-D probe engine.

The engine itself (probes, findings, emission, retry dispatch) lives in
:mod:`vco_lib.doctor`. This module is the argparse surface only, matching the
one-line-registration convention the other ``vco`` subcommands use.

Three invocation points share that ONE engine:

1. end of every ``install.py`` install/update — ``_post_install_probe_phase``;
2. launcher boot — the cheap ``--scope boot`` subset, spawned by
   ``deferral_ledger.rs::run_boot_doctor_and_retries``;
3. here, on demand.

``python -m vco_lib.doctor`` is the identical entry for environments where the
``vco`` console-script is not on PATH — and it is the ONE of the three that a
user in the v0.2.92 field-incident state can reach: their clone never updates,
so invocation point 1 never fires, and their launcher's boot doctor runs
whatever ``vco_lib`` their frozen checkout holds. Anyone told "run
``vco doctor``" gets the currency probes; nobody gets them without either that
or one completed update.
"""
from __future__ import annotations

import argparse
from typing import Any

from vco_lib import doctor as _doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    """Entry-point for ``vco doctor``. Exit 0=clean, 1=at least one problem."""
    return _doctor.run_from_args(args)


def add_subparsers(sub: Any) -> None:
    """Register ``doctor`` onto the parent subparsers action."""
    p = sub.add_parser(
        "doctor",
        help=(
            "Verify this install's environment assumptions against what is "
            "registered and delivered: npx/MCP spawnability, launcher binary "
            "freshness, deferral ledger, npm pins, prerequisites, disk space, "
            "vco_lib import origin, source currency (detached HEAD / distance "
            "from upstream), the diagnostic files a support request should "
            "include, and a stale deployed `vct`. "
            "Exit 0=clean, 1=problem found; 'could not check' never fails."
        ),
    )
    _doctor.add_arguments(p)
    p.set_defaults(func=cmd_doctor)


__all__ = ["add_subparsers", "cmd_doctor"]
