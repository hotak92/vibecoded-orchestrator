# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.cli — top-level CLI entry-points for the ``vco`` command.

This package wires the ``vco`` script (registered via ``pyproject.toml``
``[project.scripts]`` once packaging is added; until then, callable via
``python -m vco_lib.cli``) to subcommand modules:

* :mod:`vco_lib.cli.verify` — Phase 0 acceptance commands
  (``verify-pins``, ``verify-env-projection``).

Subcommand modules each expose:

* ``add_subparser(sub)`` — registers their argparse subparser onto a
  shared ``subparsers`` action.
* ``main(args)`` — runs the bound command, returns an integer exit code.

Keeping the dispatcher (``vco_lib.cli.__main__``) tiny makes future
subcommands a one-line registration and keeps the CLI surface easy to
audit. Mirrors the style already in ``vco_lib/project_init.py``.
"""
