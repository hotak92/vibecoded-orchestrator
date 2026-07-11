# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 L3-F2: vct_retrieval_tuning_get propagates the resolver's exit 5
(forbidden) WITHOUT falling back to the local retrieval-tuning.toml.

A 403 refusal from the hub on the gated /env|/config route must never be
masked by an env/file fallback (that would silently paper over a scoped-token
misconfiguration and emit a bogus "hub unreachable" diagnostic). This test
stubs the sibling resolver client to exit 5 and asserts the getter propagates
exit 5 and does NOT read the TOML.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
GET_SCRIPT = SCRIPTS_DIR / "vct_retrieval_tuning_get.sh"


@unittest.skipIf(platform.system() == "Windows", "bash sibling; .ps1 covered separately")
class RetrievalTuningForbiddenExit5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-tuning-403-"))
        # Copy the real getter into a temp scripts dir so its sibling
        # `vct_project_config.sh` resolution picks up our stub.
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        shutil.copy(GET_SCRIPT, self.scripts / "vct_retrieval_tuning_get.sh")
        # Stub resolver client that always exits 5 (forbidden).
        stub = self.scripts / "vct_project_config.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "echo '[vct-project-config] 403 forbidden (stub)' >&2\n"
            "exit 5\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        (self.scripts / "vct_retrieval_tuning_get.sh").chmod(0o755)
        # A retrieval-tuning.toml the getter MUST NOT read on a 403.
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        (self.state_dir / "retrieval-tuning.toml").write_text(
            "[retrieval_tuning]\nkg_tier_min = 0.99\n", encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.scripts / "vct_retrieval_tuning_get.sh"), *args],
            capture_output=True,
            text=True,
            env={**os.environ, "VCT_STATE_DIR": str(self.state_dir)},
            check=False,
        )

    def test_forbidden_propagates_exit_5(self):
        res = self._run("/some/project")
        self.assertEqual(
            res.returncode, 5,
            f"a 403 must propagate exit 5, got {res.returncode}; stderr:\n{res.stderr}",
        )

    def test_forbidden_does_not_read_toml(self):
        res = self._run("/some/project", "--field", "kg_tier_min")
        # The poisoned TOML value (0.99) must NOT appear in stdout — no fallback.
        self.assertNotIn(
            "0.99", res.stdout,
            "a 403 refusal must not fall back to the local retrieval-tuning.toml",
        )
        self.assertEqual(res.returncode, 5)

    def test_forbidden_message_is_honest_not_hub_unreachable(self):
        res = self._run("/some/project")
        # The stderr must NOT claim "hub unreachable" (the mislabel this fixes).
        self.assertNotIn("hub unreachable", res.stderr.lower())
        self.assertIn("forbidden", res.stderr.lower())


class BothSiblingsHaveExit5Arm(unittest.TestCase):
    """Source-level parity: both getter siblings carry the forbidden arm."""

    def test_sh_has_exit_5_arm(self):
        body = GET_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("exit 5", body)
        self.assertIn("5)", body, "sh must have a `5)` case arm")

    def test_ps1_has_exit_5_arm(self):
        ps1 = SCRIPTS_DIR / "vct_retrieval_tuning_get.ps1"
        body = ps1.read_text(encoding="utf-8")
        self.assertIn("exit 5", body)
        self.assertIn("5 {", body, "ps1 must have a `5 { ... }` switch arm")


if __name__ == "__main__":
    unittest.main()
