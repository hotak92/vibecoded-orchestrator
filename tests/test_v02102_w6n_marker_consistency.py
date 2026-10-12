# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-N — every VCO credential scanner honours the ONE marker.

The test-placeholder marker (``vco_lib.credential_shapes
.TEST_PLACEHOLDER_MARKER``, mirrored in ``_lib/credshapes.{sh,ps1}``) is a
documented user contract, so a marked fixture must be silent in EVERY
scanner that reads the credential-shape vocabulary, not only in
``post-tool-security``:

* ``_lib/credscan.sh`` / ``.ps1`` — the SubagentStop reconciler's scanner;
* ``scripts/check-no-secrets.sh`` — the repo / pre-commit scanner (its
  credential-SHAPE passes; the historical leaked-token BLOCKLIST is not a
  shape scan and a known leak is never a placeholder, so it does not).

Rule, in every scanner: the marker silences its OWN line only; an unmarked
match anywhere else in the file still alerts; nothing is inferred from how
a value looks. ``post-tool-security`` is covered in
``test_v02102_w6n_credscan_introduced.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib.credential_shapes import TEST_PLACEHOLDER_MARKER

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "templates" / "hooks" / "_lib"
CHECK_NO_SECRETS = REPO / "scripts" / "check-no-secrets.sh"

TAIL = "Q" * 36
TOKEN = "ghp_" + TAIL

MARKED = f'T = "{TOKEN}"  # {TEST_PLACEHOLDER_MARKER}\n'
UNMARKED = f'T = "{TOKEN}"\n'
_CASES = [
    ("marked_only", "x = 1\n" + MARKED, False),
    ("unmarked_only", "x = 1\n" + UNMARKED, True),
    # The marker covers its own line: one marked + one unmarked -> alert.
    ("marked_and_unmarked", MARKED + UNMARKED, True),
    # A marker on the line ABOVE does not silence the value.
    ("marker_on_other_line", f"# {TEST_PLACEHOLDER_MARKER}\n" + UNMARKED, True),
]


def _credscan_sh(path: Path) -> str:
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; scan_file_for_credentials "$2"', "_",
         str(LIB / "credscan.sh"), str(path)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _credscan_ps1(path: Path) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    lib = str(LIB / "credscan.ps1").replace("'", "''")
    target = str(path).replace("'", "''")
    proc = subprocess.run(
        [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command",
         f". '{lib}'; Scan-FileForCredentials -FilePath '{target}'"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("impl", ["sh", "ps1"])
@pytest.mark.parametrize("name, body, alerts", _CASES,
                         ids=[c[0] for c in _CASES])
def test_reconciler_credscan_honours_the_marker(tmp_path, impl, name, body,
                                                alerts):
    f = tmp_path / f"{name}.py"
    f.write_text(body, encoding="utf-8")
    out = (_credscan_sh if impl == "sh" else _credscan_ps1)(f)
    assert ("GitHub token" in out) is alerts, out
    assert TAIL not in out


def _check_no_secrets(cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    return subprocess.run(["bash", str(CHECK_NO_SECRETS)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("name, body, alerts", _CASES,
                         ids=[c[0] for c in _CASES])
def test_repo_scanner_shape_pass_honours_the_marker(tmp_path, name, body,
                                                    alerts):
    """The dist-text credential-SHAPE pass (pass 2) of check-no-secrets."""
    tree = tmp_path / "tree"
    (tree / "dist").mkdir(parents=True)
    (tree / "dist" / "meta.json").write_text(body, encoding="utf-8")
    proc = _check_no_secrets(tree)
    blocked = "dist text: credential shape" in proc.stderr
    assert blocked is alerts, (proc.returncode, proc.stdout, proc.stderr)
    assert (proc.returncode == 1) is alerts, proc.stderr
    assert TAIL not in proc.stdout + proc.stderr


def test_repo_scanner_blocklist_ignores_the_marker(tmp_path):
    """A known-leaked token prefix is never a placeholder: the -F
    BLOCKLIST passes do not honour the marker."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes.md").write_text(
        f"wh_vct_ls_example  <!-- {TEST_PLACEHOLDER_MARKER} -->\n",
        encoding="utf-8")
    proc = _check_no_secrets(tree)
    assert proc.returncode == 1 and "leaked token" in proc.stderr, proc.stderr


def test_marker_lives_in_one_home_for_every_scanner():
    """Each scanner reads the marker from the vocabulary mirror, never a
    literal of its own (a literal here would be a fourth copy to drift)."""
    for rel in ("templates/hooks/_lib/credscan.sh",
                "templates/hooks/_lib/credscan.ps1",
                "scripts/check-no-secrets.sh",
                "templates/hooks/post-tool-security.sh",
                "templates/hooks/post-tool-security.ps1"):
        code = "\n".join(ln for ln in (REPO / rel).read_text(
            encoding="utf-8-sig").splitlines()
            if not ln.lstrip().startswith("#"))
        assert TEST_PLACEHOLDER_MARKER not in code, rel
