# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.11 removal of the Ollama MCP server.

The Ollama MCP exposed three tools that all duplicated capabilities
Claude itself already ships:
    chat          — Claude itself IS the chat
    read_document — Claude's Read tool
    read_image    — Claude's native vision

The server was deleted; Ollama-as-infrastructure (HTTP API used by
Weaviate to generate embeddings) is untouched. These tests pin the
removal so a future refactor can't quietly re-add the server module.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ollama_mcp_directory_is_absent():
    """`claude_mcp_servers/ollama_mcp/` must not exist on disk."""
    ollama_mcp_dir = REPO_ROOT / "claude_mcp_servers" / "ollama_mcp"
    assert not ollama_mcp_dir.exists(), (
        f"ollama_mcp directory still present at {ollama_mcp_dir} — "
        "it should have been removed in v0.2.11"
    )


def test_ollama_mcp_server_module_not_importable():
    """`claude_mcp_servers.ollama_mcp.server` must raise on import."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # Clear any cached entry so a stale import doesn't mask a real removal.
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("claude_mcp_servers.ollama_mcp"):
            del sys.modules[mod_name]
    try:
        importlib.import_module("claude_mcp_servers.ollama_mcp.server")
    except ModuleNotFoundError:
        return  # expected
    raise AssertionError(
        "claude_mcp_servers.ollama_mcp.server is still importable — "
        "the package should have been deleted in v0.2.11"
    )


def test_no_python_module_imports_from_ollama_mcp():
    """No `.py` file in-repo may import from the removed module.

    Knowledge graph nodes and docs (markdown) are allowed to mention
    the old module historically; we filter to .py only and exclude
    the venv tree.
    """
    # Use git grep so we naturally skip .git/ and untracked junk;
    # fall back to plain grep when git is unavailable (CI shells with
    # no git? rare but cheap to handle).
    patterns = [
        r"from claude_mcp_servers\.ollama_mcp",
        r"import claude_mcp_servers\.ollama_mcp",
        r"from ollama_mcp\b",
        r"import ollama_mcp\b",
    ]
    extended_re = "|".join(patterns)

    git_cmd = [
        "git", "-C", str(REPO_ROOT), "grep", "-l", "-E", extended_re,
        "--", "*.py",
    ]
    proc = subprocess.run(git_cmd, capture_output=True, text=True)

    if proc.returncode == 0:
        # git grep exit 0 means "matches found"
        matches = [line for line in proc.stdout.splitlines() if line.strip()]
        # Exclude this very test file (it references the patterns as data).
        own_relpath = str(
            Path(__file__).resolve().relative_to(REPO_ROOT)
        ).replace("\\", "/")
        matches = [m for m in matches if m != own_relpath]
        assert not matches, (
            "Python files still import from removed ollama_mcp module: "
            f"{matches}"
        )
    elif proc.returncode == 1:
        # exit 1 = no matches found, the desired state
        return
    else:
        # exit >1 or git missing → fall back to a Python-side scan so the
        # test still runs in shells without git.
        offenders: list[str] = []
        import re
        regex = re.compile(extended_re)
        for py_file in REPO_ROOT.rglob("*.py"):
            # Skip venv and worktree management dirs
            parts = py_file.parts
            if any(p in {".venv", "venv", ".git", "node_modules"} for p in parts):
                continue
            if py_file.resolve() == Path(__file__).resolve():
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if regex.search(content):
                offenders.append(str(py_file.relative_to(REPO_ROOT)))
        assert not offenders, (
            "Python files still import from removed ollama_mcp module: "
            f"{offenders}"
        )


def test_code_embedding_service_is_still_importable():
    """The CodeSage code-embedding service is unrelated to Ollama MCP
    removal — it talks to Ollama (or its own GPU backend) over HTTP, not
    via the deleted Python module. Verify it still imports cleanly.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # The code-embedding service has heavyweight optional deps
    # (sentence-transformers, torch). We don't import the server — that
    # would pull them in. Importing the package is enough to prove the
    # source tree wasn't accidentally collateral-damaged.
    pkg_dir = REPO_ROOT / "claude_mcp_servers" / "code_embedding_service"
    assert pkg_dir.is_dir(), (
        f"code_embedding_service directory missing at {pkg_dir} — "
        "this was NOT supposed to be removed alongside ollama_mcp"
    )
    # Must contain a server.py (the FastAPI app entry-point).
    server_py = pkg_dir / "server.py"
    assert server_py.is_file(), (
        f"code_embedding_service/server.py missing at {server_py}"
    )
