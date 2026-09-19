# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 3 — the docstring promised PRESERVE; the code ADOPTS.

`vco_lib/paths.py::metrics_read_dirs` justified its dual-directory read
partly with this claim:

    the user edited a shipped hook, so ``install-bundle --update``
    PRESERVED their copy (``bundle_user_modified_preserved``)

**That has been false since v0.2.84 D7 / ruling R2.**
`vco_lib/project_init.py::_file_action` returns ``adopt`` for a divergent
bundle-shipped file: the loop backs the user's bytes up to
`.claude/backups/bundle-adoptions/<ts>/` and writes the SHIPPED bytes.
``preserve`` + the deferral is reached ONLY when that backup write fails.

The consequence runs in the user's FAVOUR — a user with a hand-edited hook does
get the fix, and their edit is recoverable — which is precisely why the stale
sentence was dangerous: it described a worse world than the real one, and a
future editor acting on it would have "fixed" a problem that no longer exists.

This file pins BOTH halves, because either alone would rot:

1. the BEHAVIOUR — `_file_action` really returns ``adopt`` for a divergent
   bundle-shipped file, and still ``preserve`` for user-owned `knowledge/**`;
2. the TEXT — the docstring no longer asserts the false claim and names
   the real terminal state.

It also verifies the thing the brief said to verify rather than assume: **no
code change was required**. `metrics_read_dirs` returns BOTH directories
unconditionally; states 1 (mid-migration) and 3 (rollback) never depended on
the preserve claim, and the residual backup-failure arm keeps state 2 real,
just rare.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The exact false sentence, in the spellings the docstring used.
_FALSE_CLAIMS = (
    "``install-bundle --update`` PRESERVES a copy the user edited",
    "``install-bundle --update``\n   PRESERVED their copy",
    "PRESERVED their copy",
)


# --------------------------------------------------------------------------- #
# 1. The behaviour the docstring now describes
# --------------------------------------------------------------------------- #


def _op(source: Path, dest_rel: str):
    from vco_lib.project_init import _BundleFileOp

    return _BundleFileOp(dest_rel=dest_rel, source_abs=source)


def test_a_divergent_shipped_hook_is_adopted_not_preserved(tmp_path):
    """The load-bearing behavioural assertion behind the docstring fix."""
    from vco_lib.project_init import _file_action

    shipped = tmp_path / "notify-stop.sh"
    shipped.write_bytes(b"#!/bin/bash\n# v0.2.92 shipped body\n")
    installed = tmp_path / "project" / ".claude" / "hooks" / "notify-stop.sh"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"#!/bin/bash\n# the user edited this\n")

    prior = hashlib.sha256(b"#!/bin/bash\n# v0.2.91 shipped body\n").hexdigest()
    manifest = {"files": {".claude/hooks/notify-stop.sh": {"sha256": prior}}}

    action, _bytes = _file_action(
        _op(shipped, ".claude/hooks/notify-stop.sh"),
        installed,
        update_mode=True,
        manifest=manifest,
    )

    assert action == "adopt", (
        f"_file_action returned {action!r}; the docstring this test "
        f"guards describes 'adopt', and if the engine really went back to "
        f"'preserve' it is the DOCS that were right and the code that "
        f"regressed — fix the engine, do not relax this."
    )


def test_a_divergent_knowledge_node_is_still_preserved(tmp_path):
    """The carve-out that must NOT move: KG nodes are user-owned state.

    Named here because it is the one case where "preserve" is still the right
    terminal state, so a reader of the fixed docstring does not conclude that
    adoption is universal.
    """
    from vco_lib.project_init import _file_action

    shipped = tmp_path / "node.md"
    shipped.write_bytes(b"# shipped seed\n")
    installed = tmp_path / "project" / "knowledge" / "concepts" / "node.md"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"# the user's own knowledge\n")

    prior = hashlib.sha256(b"# an older shipped seed\n").hexdigest()
    manifest = {"files": {"knowledge/concepts/node.md": {"sha256": prior}}}

    action, _bytes = _file_action(
        _op(shipped, "knowledge/concepts/node.md"),
        installed,
        update_mode=True,
        manifest=manifest,
    )

    assert action == "preserve"


def test_the_preserve_fallback_still_exists_in_the_loop():
    """State 2 is rare, not gone — so the dual read is still justified.

    The docstring now says ``preserve`` is reached only on a backup-write
    failure. That sentence is itself a promise, and this is its backing: the
    adopt branch has an `except` arm that sets ``action = "preserve"``.
    """
    src = (_REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
    assert 'action = "preserve"' in src, (
        "the adopt branch's backup-failure fallback is gone — then the "
        "docstrings' 'only when the backup write fails' clause is the next "
        "false promise"
    )


# --------------------------------------------------------------------------- #
# 2. The text
# --------------------------------------------------------------------------- #


def test_paths_docstring_no_longer_promises_preserve():
    from vco_lib.paths import metrics_read_dirs

    doc = metrics_read_dirs.__doc__ or ""
    for claim in _FALSE_CLAIMS:
        assert claim not in doc, f"metrics_read_dirs still asserts: {claim!r}"
    assert "ADOPT" in doc or "adopt" in doc
    assert "bundle-adoptions" in doc


def test_paths_docstring_names_the_release_that_changed_it():
    """The correction is dated, so the next reader can check it rather than trust it."""
    from vco_lib.paths import metrics_read_dirs

    assert "v0.2.84" in (metrics_read_dirs.__doc__ or "")


# --------------------------------------------------------------------------- #
# 3. The verification the brief asked for: no code change was required
# --------------------------------------------------------------------------- #


def test_metrics_read_dirs_still_returns_both_unconditionally(tmp_path, monkeypatch):
    from vco_lib.paths import (
        legacy_claude_metrics_dir,
        metrics_read_dirs,
        vct_metrics_dir,
    )

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))

    # Neither directory exists; the reader still names both. Probing is the
    # caller's job — states 1 and 3 do not depend on the adopt/preserve
    # question at all, which is why the docstring fix needed no code change.
    assert metrics_read_dirs() == (vct_metrics_dir(), legacy_claude_metrics_dir())
