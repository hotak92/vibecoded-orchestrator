# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Scratch directories for tests — created OUTSIDE the repository.

v0.2.95 review MINOR-3. Several `unittest` fixtures built their scratch dir
INSIDE ``tests/`` (``tests/_tmp_self_heal_acc_<pid>_<id>/``) and relied on
``tearDown`` to remove it. A run interrupted between the two — Ctrl-C, a
crashed helper thread, a killed CI job — leaves the directory in the source
tree, untracked and (until now) un-ignored, where the next ``git add -A``
would ship it in a release commit. One 618 KB ``launcher.db`` did survive
that way into the v0.2.95 tree, which is how this was found;
``tests/_tmp_orchroot_*/`` in ``.gitignore`` is the scar from the previous
occurrence of the same shape.

``tempfile.mkdtemp()`` puts any future leak somewhere the OS already sweeps
and git never sees. Nothing in these fixtures needs the directory to live
under ``tests/``: its only consumer is ``VCT_STATE_DIR``, an absolute path.

`pytest`'s ``tmp_path`` fixture is the right answer for pytest-style tests;
this helper exists for the ``unittest.TestCase`` fixtures, whose ``setUp``
cannot receive one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

__all__ = ["scratch_state_dir"]


def scratch_state_dir(tag: str) -> Path:
    """Return a fresh scratch directory named after *tag*.

    The caller owns removal (``shutil.rmtree(..., ignore_errors=True)`` in
    ``tearDown``); a missed removal now costs a directory under the system
    temp root instead of one in the repository.
    """
    return Path(tempfile.mkdtemp(prefix=f"vco_{tag}_"))
