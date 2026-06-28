# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream D — KG-presentation fixes (formatter-only, no schema change).

D-1: a Development-collection result with NO `node_type` property must render as
     node_type="doc" (NOT "unknown"). True KG nodes that legitimately lack a type
     keep "unknown" — the "doc" default is GATED on the Development collection.
     (The docs collection deliberately has no node_type property — adding it was
     rejected; this is the formatter-only fix in _format_obj.)
D-2: `file_path` must appear in rl_kg_search.py --hook-format output (as a
     "| src=<path>" trailer) so injected blocks are openable + the seen-store
     reads-ledger can match.
D-3: pre-bash-context-inject must strip command-noise tokens before building the
     KG query (so a bare cd/ls doesn't inject directory-keyword KG).
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
HOOKS = REPO_ROOT / "templates" / "hooks"


@pytest.fixture
def fresh_server(monkeypatch) -> Iterator:
    for p in (str(MCP_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for name in [n for n in list(sys.modules) if n == "weaviate_mcp" or n.startswith("weaviate_mcp.")]:
        sys.modules.pop(name, None)

    def _do_import():
        try:
            return importlib.import_module("weaviate_mcp.server")
        except Exception as exc:
            pytest.fail(f"weaviate_mcp.server import failed: {exc}")

    yield _do_import

    for name in list(sys.modules):
        if name == "weaviate_mcp" or name.startswith("weaviate_mcp."):
            sys.modules.pop(name, None)


def _set_env(monkeypatch, **kwargs: str) -> None:
    for key, value in kwargs.items():
        if value is None or value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _make_obj(**properties) -> SimpleNamespace:
    metadata = SimpleNamespace(distance=properties.pop("distance", 0.1))
    return SimpleNamespace(properties=dict(properties), metadata=metadata)


# --------------------------------------------------------------------------
# D-1 — node_type "doc" default for Development-collection results
# --------------------------------------------------------------------------
def test_d1_development_result_defaults_to_doc(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    assert server.DEVELOPMENT_COLLECTION == "MyProject_Development"
    # A docs object with NO node_type property.
    obj = _make_obj(title="Some Doc", content="hello", file_path="docs/foo.md")
    formatted = server._format_obj(obj, "MyProject_Development", distance=0.1)
    assert formatted["node_type"] == "doc", (
        f"Development result must default node_type to 'doc'; got "
        f"{formatted['node_type']!r}"
    )


def test_d1_kg_result_without_type_stays_unknown(monkeypatch, fresh_server):
    """A true KG-collection result lacking node_type must NOT silently become
    'doc' — it keeps the KG-appropriate 'unknown' default."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="KG Node", content="x", file_path="knowledge/foo.md")
    formatted = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert formatted["node_type"] == "unknown", (
        f"KG result without a type must stay 'unknown', never 'doc'; got "
        f"{formatted['node_type']!r}"
    )


def test_d1_explicit_node_type_preserved(monkeypatch, fresh_server):
    """An explicit node_type is preserved on BOTH collections."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="Typed", node_type="concept", content="x", file_path="k/foo.md")
    fmt_dev = server._format_obj(obj, "MyProject_Development", distance=0.1)
    fmt_kg = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert fmt_dev["node_type"] == "concept"
    assert fmt_kg["node_type"] == "concept"


def test_d1_no_development_collection_configured(monkeypatch, fresh_server):
    """With DEVELOPMENT_COLLECTION unset, no result is mis-stamped 'doc'."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="N", content="x", file_path="k/foo.md")
    formatted = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert formatted["node_type"] == "unknown"


# --------------------------------------------------------------------------
# D-2 — file_path in rl_kg_search.py --hook-format
# --------------------------------------------------------------------------
def test_d2_rl_kg_search_emits_src_trailer() -> None:
    """rl_kg_search.py --hook-format must append a '| src=<file_path>' trailer
    (only in hook-format mode, only when a path exists)."""
    src = (MCP_DIR / "scripts" / "rl_kg_search.py").read_text(encoding="utf-8")
    assert 'file_path = entry.get("file_path", "")' in src, (
        "rl_kg_search.py must read file_path from the entry dict"
    )
    assert 'src_trailer = f" | src={file_path}"' in src, (
        "rl_kg_search.py must build a '| src=<path>' trailer for hook-format"
    )
    assert "args.hook_format and file_path" in src, (
        "the src trailer must be gated on hook-format mode AND a non-empty path"
    )
    # All three render branches must append the trailer.
    assert src.count("{src_trailer}") >= 3, (
        "all three --hook-format render branches must append the src trailer"
    )


# --------------------------------------------------------------------------
# D-3 — command-noise strip in pre-bash KG query
# --------------------------------------------------------------------------
def _has_bash() -> bool:
    return shutil.which("bash") is not None


def test_d3_prebash_sources_shared_strip_no_inline() -> None:
    """N-3 (one home): the hook must SOURCE _lib/command-noise-strip.sh and call
    vco_strip_command_noise — NOT carry an inline `for t in cmd.split()` copy.
    Same for the .ps1 sibling."""
    body = (HOOKS / "pre-bash-context-inject.sh").read_text(encoding="utf-8")
    assert "_lib/command-noise-strip.sh" in body and "vco_strip_command_noise" in body, (
        "pre-bash.sh must source + call the shared noise-strip helper"
    )
    assert "for t in cmd.split()" not in body, (
        "pre-bash.sh must NOT carry an inline copy of the noise-strip"
    )
    # The 500-char skip must still be present (not removed by D-3).
    assert "THRESHOLD" in body and "CMD_LEN" in body
    ps1 = (HOOKS / "pre-bash-context-inject.ps1").read_text(encoding="utf-8")
    assert "command-noise-strip.ps1" in ps1 and "Get-VcoCommandNoiseStripped" in ps1, (
        "pre-bash.ps1 must source + call the shared noise-strip helper"
    )
    assert "for t in cmd.split()" not in ps1, (
        "pre-bash.ps1 must NOT carry an inline copy of the noise-strip"
    )


NOISE_STRIP_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "command-noise-strip.sh"
NOISE_STRIP_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "command-noise-strip.ps1"


def test_d3_noise_strip_ps1_sibling_exists() -> None:
    assert NOISE_STRIP_SH.exists()
    assert NOISE_STRIP_PS1.exists(), (
        "command-noise-strip.ps1 MISSING — _lib/ is excluded from the parity "
        "gate, so this must be hand-verified here."
    )


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_d3_strip_removes_flags_and_paths(tmp_path: Path) -> None:
    """Drive the SHARED _lib helper function (not a re-inlined copy — N-3) and
    assert it removes flags / path tokens / shell ops."""
    py = shutil.which("python3") or "python3"

    def strip(cmd: str) -> str:
        script = (
            f'export PY="{py}"\n'
            f'. "{NOISE_STRIP_SH}"\n'
            'vco_strip_command_noise "$1"\n'
        )
        r = subprocess.run(["bash", "-c", script, "_", cmd], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()

    # bare cd /some/dir -> only "cd" survives (path stripped) -> low signal.
    assert strip("cd /some/dir/project") == "cd"
    # ls -la /tmp -> "ls" only.
    assert strip("ls -la /tmp") == "ls"
    # a code-file path keeps its BASENAME (meaningful signal).
    assert "server.py" in strip("python claude_mcp_servers/weaviate_mcp/server.py")
    # flags + bare-dot dropped; identifier kept.
    assert strip("grep -rn migrate_collections .") == "grep migrate_collections"
