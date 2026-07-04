# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 DEDUP-5 / CORRECT-1: atomic-write tempfile cleanup.

Verifies install.py's `.claude.json` atomic-write now routes through
vco_lib.env_template._atomic_write_text, which correctly unlinks the
tempfile on any exception (closing CORRECT-1).

The pre-v0.2.53 inline recipe (`tmp.write_text(...)` then
`os.replace(tmp, target)`) left behind <path>.tmp on partial-write
failures (disk full, write-mid-flush, sigterm).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"
# v0.2.73 (IN-1): the MCP-registration write site (the `~/.claude.json`
# mcpServers block writer, which carries the "# Backup + atomic write." marker)
# was extracted from install.py into vco_lib/install_mcp.py. The structural
# assertions below follow it to its new home.
INSTALL_MCP_PY = REPO_ROOT / "vco_lib" / "install_mcp.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location("install_under_test_d5", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_d5"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_atomic_write_text_helper_unlinks_tempfile_on_failure(tmp_path):
    """vco_lib.env_template._atomic_write_text deletes .tmp on failure."""
    from vco_lib.env_template import _atomic_write_text
    target = tmp_path / "out.json"

    # Force os.replace to raise to simulate a rename failure
    # (cross-filesystem, target locked, etc.). The helper's contract
    # is to unlink the tempfile and re-raise the exception.
    with patch("os.replace", side_effect=OSError("rename failed")):
        with pytest.raises(OSError):
            _atomic_write_text(target, '{"key": "value"}')

    # The helper's contract: no .tmp file leaked.
    leaked = list(tmp_path.glob("*.tmp"))
    assert leaked == [], (
        f"Expected no .tmp leftovers; found: {leaked}"
    )


def test_atomic_write_text_happy_path_replaces_target(tmp_path):
    """Happy path writes to target atomically + leaves no .tmp."""
    from vco_lib.env_template import _atomic_write_text
    target = tmp_path / "out.json"
    _atomic_write_text(target, '{"key": "value"}')
    assert target.read_text() == '{"key": "value"}'
    leaked = list(tmp_path.glob("*.tmp"))
    assert leaked == []


def test_register_mcp_entries_routes_through_atomic_helper(install_module):
    """The `.claude.json` write site uses _atomic_write_text, not inline."""
    # IN-1 moved the write site into vco_lib/install_mcp.py.
    src = INSTALL_MCP_PY.read_text(encoding="utf-8")
    # The site is inside the function that writes ~/.claude.json's
    # mcpServers block. Locate the marker then check the body around it.
    site_marker = "# Backup + atomic write."
    idx = src.find(site_marker)
    assert idx > 0, (
        "the '# Backup + atomic write.' write-site marker must exist in "
        "vco_lib/install_mcp.py (IN-1's new home for MCP registration)"
    )
    # The write call lives just after the marker.
    excerpt = src[idx:idx + 2000]
    assert "_atomic_write_text" in excerpt, (
        "The .claude.json write site must route through "
        "vco_lib.env_template._atomic_write_text (CORRECT-1) — the inline "
        "tmp.write_text + os.replace recipe leaked .tmp files on failure."
    )


def test_register_mcp_entries_does_not_use_inline_tmp_write(install_module):
    """The `.claude.json` write site must NOT use inline tmp.write_text."""
    src = INSTALL_MCP_PY.read_text(encoding="utf-8")
    site_marker = "# Backup + atomic write."
    idx = src.find(site_marker)
    assert idx > 0, "write-site marker must exist in vco_lib/install_mcp.py"
    excerpt = src[idx:idx + 2000]
    # Inline tmp.write_text + os.replace is the leaky pattern.
    assert "tmp.write_text(json.dumps(data" not in excerpt
    assert "os.replace(tmp, claude_json_path)" not in excerpt


def test_atomic_write_text_idempotent_overwrite(tmp_path):
    """Calling _atomic_write_text twice replaces the target each time."""
    from vco_lib.env_template import _atomic_write_text
    target = tmp_path / "out.json"
    _atomic_write_text(target, "first")
    assert target.read_text() == "first"
    _atomic_write_text(target, "second")
    assert target.read_text() == "second"
    assert list(tmp_path.glob("*.tmp")) == []
