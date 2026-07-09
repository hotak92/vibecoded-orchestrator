# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Keep pytest OUT of the golden code-graph fixture repo.

The fixture repo under ``repo/`` is source-for-analysis, NOT a test suite:
``repo/tests/test_widgets.py`` deliberately looks like a test file (it
exercises the analyzer's ``is_test`` path heuristic), and it imports
``from src.widgets import ...`` relative to the fixture root — which is not
importable from the outer suite. Collecting it would raise an import error
and break the run. ``collect_ignore_glob`` tells pytest to skip every path
under this directory during collection; the golden harness reaches these
files directly via a filesystem walk (``analyze_repository``), never through
pytest collection.
"""

collect_ignore_glob = ["*"]
