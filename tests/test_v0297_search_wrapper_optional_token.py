# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the search MCP wrapper treats GITHUB_TOKEN as optional, and
what it prints is true.

The search server reads no GitHub token (``search_papers`` only). The
wrapper nevertheless exited 1 whenever ``github_pat`` did not resolve, so the
MCP failed to start on every machine without a registered PAT; and its
message promised that "the launcher auto-writes GITHUB_TOKEN to every
registered project's .claude/env" — no writer has done that since v0.2.73.

The REAL wrapper is executed from a scratch repo layout (it locates the
resolver relative to itself), with a fake resolver and a fake interpreter, so
no hub, keychain or network is ever touched.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "claude_mcp_servers" / "search_mcp" / "wrapper.sh"


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "repo"
    wrapper = root / "claude_mcp_servers" / "search_mcp" / "wrapper.sh"
    wrapper.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, wrapper)
    _exe(root / "templates" / "scripts" / "vct_secrets_resolve.sh",
         '#!/usr/bin/env bash\n'
         '[ "${FAKE_RC:-0}" -eq 0 ] && printf %s "tok-123"\n'
         'exit "${FAKE_RC:-0}"\n')
    fake_py = tmp_path / "python"
    _exe(fake_py, '#!/usr/bin/env bash\necho "SERVER_STARTED TOKEN=${GITHUB_TOKEN-<unset>}"\n')
    server = tmp_path / "server.py"
    server.write_text("# fake\n", encoding="utf-8")
    return root, wrapper, fake_py, server


def _run(layout, **env_extra):
    root, wrapper, fake_py, server = layout
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    env.update(SEARCH_MCP_PYTHON=str(fake_py), SEARCH_MCP_SERVER=str(server),
               VCT_PROJECT_PATH=str(root), **env_extra)
    return subprocess.run(["bash", str(wrapper)], capture_output=True, text=True,
                          env=env, timeout=60)


def test_a_resolved_token_is_passed_through(layout):
    out = _run(layout, FAKE_RC="0")
    assert out.returncode == 0, out.stderr
    assert "SERVER_STARTED TOKEN=tok-123" in out.stdout


@pytest.mark.parametrize("rc", ["1", "2", "3", "4", "5", "6", "9"])
def test_an_unresolved_token_is_reported_and_the_server_still_starts(layout, rc):
    """RED before: exit 1, server never started."""
    out = _run(layout, FAKE_RC=rc)
    assert out.returncode == 0, out.stderr
    assert "SERVER_STARTED TOKEN=<unset>" in out.stdout
    assert "starting WITHOUT GITHUB_TOKEN" in out.stderr


def test_the_printed_instructions_are_true(layout):
    root, *_ = layout
    out = _run(layout, FAKE_RC="4")
    err = out.stderr
    assert "auto-writes" not in err and ".claude/env" not in err
    # The resolver it names is the one that exists, called the way it works.
    resolver = root / "templates" / "scripts" / "vct_secrets_resolve.sh"
    assert f'{resolver} "{root}" github_pat' in err
    assert "vct exec --secret github_pat=GITHUB_TOKEN -- <cmd>" in err


def test_an_exported_token_wins_without_calling_the_resolver(layout):
    out = _run(layout, GITHUB_TOKEN="from-env", FAKE_RC="3")
    assert out.returncode == 0
    assert "SERVER_STARTED TOKEN=from-env" in out.stdout
    assert "NOTE" not in out.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="bash wrapper is Unix-only")
def test_the_real_vct_cli_documents_the_printed_exec_form(tmp_path):
    """The printed `vct exec --secret KEY=ENV -- cmd` form is the one the
    shipped CLI's own usage names (``--help`` only, HOME isolated — nothing
    is resolved or written)."""
    vct = REPO_ROOT / "tools" / "vct-secrets" / "vct"
    if not vct.is_file():
        pytest.skip("vct CLI not in this checkout")
    env = dict(os.environ, HOME=str(tmp_path))
    help_out = subprocess.run(["bash", str(vct), "--help"], capture_output=True,
                              text=True, timeout=60, env=env)
    text = help_out.stdout + help_out.stderr
    assert "exec [--project NAME] [--secret KEY[=VAR_NAME]]... [--preserve-env] -- CMD" in text
