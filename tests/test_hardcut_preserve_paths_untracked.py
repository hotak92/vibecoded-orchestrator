# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Structural regression guard for the §7 hard-cut MUST-PRESERVE guarantee.

The hard-cut primitive (`vco_lib/hard_cut.py`) preserves `knowledge/**` and
`.claude/state/` NOT by any runtime check but STRUCTURALLY: a hard cut does a
code-only `git reset --hard <tag>`, and `git reset --hard` only ever mutates
git-TRACKED files. So the data-safety guarantee for those two trees rests
entirely on them being UNTRACKED in the public repo (they are user/runtime
state, gitignored).

If a future editor accidentally `git add`s a file under `knowledge/` or
`.claude/state/`, the hard cut would silently start overwriting/removing that
file on a reset — a silent data-safety regression that NO unit test of
`hard_cut.py` (which fakes the runner) could catch. This test is the cheap
structural backstop: it asserts those trees stay empty in `git ls-files`.

(Companion: `tests/test_hard_cut.py::test_hard_cut_touches_no_preserve_list_path`
proves the argv/cwd discipline; this test proves the structural premise that
discipline relies on.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _git_ls_files(pathspec: str) -> list[str]:
    """`git ls-files <pathspec>` against the repo root. Returns tracked paths."""
    try:
        out = subprocess.run(
            ["git", "ls-files", pathspec],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"git ls-files unavailable ({exc})")
    if out.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip(f"not a git checkout ({out.stderr.strip()})")
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


@pytest.mark.parametrize("tree", ["knowledge/", ".claude/state/"])
def test_hardcut_preserve_tree_stays_untracked(tree):
    """`knowledge/` and `.claude/state/` MUST stay untracked so the hard-cut's
    code-only `git reset --hard` can never touch them. A non-empty result here
    means a file was accidentally committed into a MUST-PRESERVE tree → the
    hard cut would silently overwrite user/runtime state. Untrack it
    (`git rm --cached`) + ensure it's gitignored."""
    tracked = _git_ls_files(tree)
    assert tracked == [], (
        f"MUST-PRESERVE tree {tree!r} has {len(tracked)} git-TRACKED file(s) "
        f"(e.g. {tracked[:3]}). The §7 hard cut preserves this tree only "
        f"because `git reset --hard` can't touch UNTRACKED files — tracking "
        f"any file here silently breaks that data-safety guarantee. Run "
        f"`git rm --cached <file>` and confirm it's gitignored."
    )
