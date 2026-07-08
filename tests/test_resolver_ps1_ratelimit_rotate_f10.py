# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F-10 (v0.2.75): ps1 resolver rate-limit + Invoke-RWMaybeRotate coverage.

``templates/scripts/test_vct_project_config_rate_limit.sh`` used to carry a
false-premise OS-EXEMPT-PARITY marker citing a
``vct_project_config.Tests.ps1`` that does not exist — so the PowerShell
resolver's rate-limit + JSONL-rotation path (``Invoke-RWMaybeRotate``) was
untested. This pwsh-gated pytest is the real ps1 coverage the marker now
points at.

It dot-sources the resolver's FUNCTIONS (stripping the ``# ── Main`` entry
block so no CLI runs) and exercises:

  * ``Invoke-RWMaybeRotate`` on an OVERSIZED (>1 MiB) JSONL → rotated to the
    most-recent 100 rows (act);
  * the same on an UNDER-cap file → left untouched (leave-alone);
  * two back-to-back ``Emit-Warning`` calls for the same (pid, kind) →
    exactly ONE stderr line (suppression), and ``VCO_HOOK_DEBUG=1`` → two.

Mirrors the bash sibling ``test_vct_project_config_rate_limit.sh``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PS1_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.ps1"
_PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _lib_only_ps1() -> str:
    """Return the resolver ps1 source with the `# ── Main` entry block AND
    the top-level `[CmdletBinding()] param(...)` block stripped, so
    dot-sourcing it defines the FUNCTIONS without a script-level param block
    that would demand `-Project` binding (mirrors the bash test's
    `sed -e '/^main "$@"$/d'`, adapted to PowerShell's param semantics)."""
    text = PS1_CLIENT.read_text(encoding="utf-8")
    marker = "# ── Main"
    idx = text.find(marker)
    assert idx != -1, "could not find the '# ── Main' entry marker in the ps1"
    body = text[:idx]
    # Strip the leading `[CmdletBinding(...)]` + `param( ... )` block. The
    # param block ends at the first `)` on its own line after `param(`.
    cb = body.find("[CmdletBinding")
    assert cb != -1, "expected a [CmdletBinding] at the top of the ps1"
    pstart = body.find("param(", cb)
    assert pstart != -1, "expected a param( block"
    # Find the matching close: the param list is followed by `)` then a
    # blank line; the resolver ends it with a line that is exactly `)`.
    close = body.find("\n)", pstart)
    assert close != -1, "could not find the param() close"
    close_end = close + 2  # past the `\n)`
    return body[:cb] + body[close_end:]


@unittest.skipIf(
    _PWSH is None,
    "no PowerShell runtime on PATH (pwsh / powershell). F-10 ps1 rate-limit "
    "tests skipped — install PowerShell Core 7+ to run.",
)
class Ps1RateLimitRotateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._state_dir = Path(tempfile.mkdtemp(prefix="vct-f10-ps1-"))
        self._lib = self._state_dir / "lib.ps1"
        self._lib.write_text(_lib_only_ps1(), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self._state_dir, ignore_errors=True)

    def _run_ps(self, body: str, extra_env: dict | None = None):
        script = f". '{self._lib}'\n{body}\n"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._state_dir),
            "VCT_STATE_DIR": str(self._state_dir),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [_PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_rotate_oversized_jsonl_keeps_tail(self):
        """>1 MiB → rotated to the most-recent 100 rows (act)."""
        jsonl = self._state_dir / "big.jsonl"
        # ~1.2 MiB of numbered rows so we can assert the tail survived.
        with jsonl.open("w", encoding="utf-8") as fh:
            row = '{"n":%d,"pad":"' + ("p" * 200) + '"}\n'
            n = (1200 * 1024) // (len(row % 0)) + 200
            for i in range(n):
                fh.write(row % i)
        total = sum(1 for _ in jsonl.open(encoding="utf-8"))
        self.assertGreater(jsonl.stat().st_size, 1048576)

        res = self._run_ps(f"Invoke-RWMaybeRotate -Path '{jsonl}'")
        self.assertEqual(res.returncode, 0, res.stderr)

        kept = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(kept), 100, f"rotation must keep 100 rows, got {len(kept)}")
        # The LAST original row must be among the kept tail.
        last_n = total - 1
        self.assertTrue(
            any(f'"n":{last_n}' in ln for ln in kept),
            "the newest rows must be the ones kept after rotation",
        )

    def test_undercap_jsonl_untouched(self):
        """<=1 MiB → left intact (leave-alone)."""
        jsonl = self._state_dir / "small.jsonl"
        jsonl.write_text('{"n":1}\n{"n":2}\n', encoding="utf-8")
        before = jsonl.read_text(encoding="utf-8")
        res = self._run_ps(f"Invoke-RWMaybeRotate -Path '{jsonl}'")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(jsonl.read_text(encoding="utf-8"), before,
                         "under-cap file must be untouched")

    def test_rotate_missing_file_is_noop(self):
        missing = self._state_dir / "nope.jsonl"
        res = self._run_ps(f"Invoke-RWMaybeRotate -Path '{missing}'")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(missing.exists())

    def test_emit_warning_suppresses_second_same_kind(self):
        """Two back-to-back Emit-Warning (same pid+kind) → exactly one line."""
        body = (
            "Emit-Warning -ErrorKind hub_unreachable -Detail first\n"
            "Emit-Warning -ErrorKind hub_unreachable -Detail second\n"
        )
        res = self._run_ps(body)
        self.assertEqual(res.returncode, 0, res.stderr)
        n = len(re.findall(r"\[vct\] project_config:", res.stderr))
        self.assertEqual(n, 1, f"expected exactly 1 emit, got {n}: {res.stderr!r}")

    def test_emit_warning_debug_bypasses_suppression(self):
        body = (
            "Emit-Warning -ErrorKind hub_unreachable -Detail first\n"
            "Emit-Warning -ErrorKind hub_unreachable -Detail second\n"
        )
        res = self._run_ps(body, extra_env={"VCO_HOOK_DEBUG": "1"})
        self.assertEqual(res.returncode, 0, res.stderr)
        n = len(re.findall(r"\[vct\] project_config:", res.stderr))
        self.assertEqual(n, 2, f"VCO_HOOK_DEBUG=1 should emit twice, got {n}")


if __name__ == "__main__":
    unittest.main()
