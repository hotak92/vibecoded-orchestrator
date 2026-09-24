# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Review R6 F48: texts the v0.2.97 moves left describing a mechanism that is
not the one in force. A description of a guard is part of the guard, so each
corrected text is pinned to the behaviour it now states.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="unix permission bits")
def test_the_sentinel_rewrite_keeps_the_env_files_mode(tmp_path: Path) -> None:
    """(a) ``replace_values_with_sentinel`` says the file keeps its mode across
    the atomic swap — through ``atomic_rewrite_text`` (no ``copystat``)."""
    from vco_lib.env_template import replace_values_with_sentinel

    for mode in (0o640, 0o600):
        env_path = tmp_path / f"env-{mode:o}"
        env_path.write_text("export OPENAI_API_KEY=sk-abc  # team\nUSER=1\n")
        env_path.chmod(mode)
        replaced, missed = replace_values_with_sentinel(env_path, ["OPENAI_API_KEY"], "__s__")
        assert (replaced, missed) == (1, [])
        assert env_path.read_text() == "export OPENAI_API_KEY=__s__  # team\nUSER=1\n"
        assert stat.S_IMODE(env_path.stat().st_mode) == mode


def test_the_sentinel_docstring_names_the_real_mechanism() -> None:
    """(a) The docstring and the write-site comment no longer credit a
    ``copystat`` / a local ``mode=`` that the function does not have."""
    from vco_lib import env_template

    doc = env_template.replace_values_with_sentinel.__doc__ or ""
    assert "copystat" not in doc
    assert "atomic_rewrite_text" in doc


def test_the_session_state_windows_note_matches_the_windows_template() -> None:
    """(c) ``vct-session-state.json`` said "PowerShell port is pending"; the
    Windows settings template registers every hook's ``.ps1`` sibling, and
    each of those siblings ships."""
    manifest = json.loads(
        (REPO / "launcher" / "bundled_manifests" / "vct-session-state.json").read_text()
    )
    note = manifest["requirements"]["_windows_note"]
    assert "pending" not in note.lower()
    assert ".ps1" in note

    template = (REPO / "templates" / "settings.json.windows.template").read_text(encoding="utf-8")
    hooks = re.findall(r"\.claude/hooks/([A-Za-z0-9_.-]+)", template)
    assert "context-size-check.ps1" in hooks
    for hook in hooks:
        assert hook.endswith(".ps1"), hook
        assert (REPO / "templates" / "hooks" / hook).is_file(), hook
