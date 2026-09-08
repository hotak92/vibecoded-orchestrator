# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The gateway's ``__version__`` is a release pin, and now it is gated.

On a 0.2.93 install ``/health`` reported ``"version": "0.2.92"``, because
``scripts/bump-version.sh`` did not touch
``claude_mcp_servers/model_router/__init__.py`` and
``scripts/check-version-pins.sh`` did not look at it. During an incident that
is worse than a cosmetic slip: "which build am I talking to?" is one of the
first questions asked, and the answer was a version that had not existed for a
release.

Both halves are pinned here — the VALUE (it equals the distribution version)
and the MECHANISM (the bump script writes it, the gate reads it). A gate that
nobody runs is the defect one layer out, so the shell files are read as text
rather than executed: the assertions hold on Windows, where the CI job that
runs the bash gate does not.
"""
from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MR_INIT = REPO_ROOT / "claude_mcp_servers" / "model_router" / "__init__.py"
BUMP = REPO_ROOT / "scripts" / "bump-version.sh"
GATE = REPO_ROOT / "scripts" / "check-version-pins.sh"


def _distribution_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


class VersionPinTests(unittest.TestCase):
    def test_the_gateway_version_matches_the_distribution(self) -> None:
        from model_router import __version__

        self.assertEqual(__version__, _distribution_version())

    def test_the_file_literal_matches_too(self) -> None:
        """Read from disk, so an installed stale copy cannot mask a drift."""
        match = re.search(
            r'^__version__ = "([^"]+)"', MR_INIT.read_text(encoding="utf-8"), re.M,
        )
        assert match is not None, "no __version__ literal in model_router/__init__.py"
        self.assertEqual(match.group(1), _distribution_version())

    def test_the_bump_script_writes_this_file(self) -> None:
        body = BUMP.read_text(encoding="utf-8")
        self.assertIn("claude_mcp_servers/model_router/__init__.py", body)
        self.assertIn("__version__", body)

    def test_the_gate_checks_this_file(self) -> None:
        body = GATE.read_text(encoding="utf-8")
        self.assertIn("claude_mcp_servers/model_router/__init__.py", body)
        self.assertIn("DUNDER_PIN_FILES", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
