# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 B1: ``knowledge/.node_formats.json`` keys are canonical POSIX.

Windows triage 2026-10-15: the summary writer keyed sidecar entries with
``str(Path.relative_to(...))`` — ``knowledge\\concepts\\a.md`` on Windows —
while the retrieval reader (``kg_query_core._get_node_format``) looks them up
by the canonical POSIX ``file_path`` Weaviate stores. Every summary and
description lookup missed on Windows, silently.

Pinned here, with Windows simulated on any host (``PureWindowsPath`` and
explicit backslash keys):

* the writer's key for a Windows path is POSIX;
* ``vco_lib.node_formats_keys.canonicalize_keys`` re-keys a legacy sidecar
  idempotently and losslessly (both arms of each collision rule);
* both generator modes rewrite a legacy backslash sidecar once, and treat
  the re-keyed entry as current (no regeneration);
* ``summary_health.scan_pending`` neither double-counts nor misses a node
  for its key's separator;
* the MCP reader finds a summary stored under a backslash key.

All synthetic: no LLM, no Weaviate, no network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import summary_health  # noqa: E402
from vco_lib.node_formats_keys import canonicalize_keys, sidecar_key  # noqa: E402

GENERATOR = REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"

NODE_TEXT = (
    "---\n"
    "title: Key Node\n"
    "type: concept\n"
    "---\n"
    "\n"
    "Body prose of the key test node.\n"
)
POSIX_KEY = "knowledge/concepts/node-0.md"
WIN_KEY = "knowledge\\concepts\\node-0.md"
KEPT = "A plausible stored summary of the key node, generated before the fix."


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _entry(summary: str = "S", *, at: str = "2026-10-01T00:00:00+00:00",
           chash: str = "h", backend: str = "cli") -> dict:
    return {
        "title": "Key Node",
        "description": summary,
        "summary": summary,
        "generated_at": at,
        "content_hash": chash,
        "backend": backend,
    }


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        f"_gks_b1_{uuid.uuid4().hex}", GENERATOR
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed(root: Path) -> Path:
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True, exist_ok=True)
    node = kn / "node-0.md"
    node.write_bytes(NODE_TEXT.encode("utf-8"))
    return node


# ═══════════════════════════════════════════════════════════════════════════
# The key shape
# ═══════════════════════════════════════════════════════════════════════════

def test_sidecar_key_of_a_windows_path_is_posix():
    key = sidecar_key(
        PureWindowsPath(r"C:\proj\knowledge\concepts\node-0.md"),
        PureWindowsPath(r"C:\proj"),
    )
    assert key == POSIX_KEY


def test_sidecar_key_of_a_posix_path_is_unchanged(tmp_path):
    assert sidecar_key(tmp_path / "knowledge" / "a.md", tmp_path) == "knowledge/a.md"


def test_the_generator_keys_a_windows_path_in_posix_form(monkeypatch):
    mod = _load_generator()
    monkeypatch.setattr(mod, "CLAUDE_PROJECT", PureWindowsPath(r"C:\proj"))
    key = mod._sidecar_key(PureWindowsPath(r"C:\proj\knowledge\concepts\node-0.md"))
    assert key == POSIX_KEY


# ═══════════════════════════════════════════════════════════════════════════
# canonicalize_keys — idempotent, lossless
# ═══════════════════════════════════════════════════════════════════════════

def test_a_backslash_key_is_rewritten_and_a_second_pass_is_a_no_op():
    db = {WIN_KEY: _entry("win"), "knowledge/b.md": _entry("b")}
    assert canonicalize_keys(db) == 1
    assert db == {POSIX_KEY: _entry("win"), "knowledge/b.md": _entry("b")}
    assert canonicalize_keys(db) == 0


def test_an_already_canonical_sidecar_is_left_alone():
    db = {POSIX_KEY: _entry("p")}
    snapshot = json.dumps(db)
    assert canonicalize_keys(db) == 0
    assert json.dumps(db) == snapshot


def test_collision_the_newer_entry_wins_either_way_round():
    old, new = "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"
    db = {WIN_KEY: _entry("win-new", at=new), POSIX_KEY: _entry("posix-old", at=old)}
    canonicalize_keys(db)
    assert db == {POSIX_KEY: _entry("win-new", at=new)}

    db = {WIN_KEY: _entry("win-old", at=old), POSIX_KEY: _entry("posix-new", at=new)}
    canonicalize_keys(db)
    assert db == {POSIX_KEY: _entry("posix-new", at=new)}


def test_collision_never_drops_a_summary_for_an_unusable_entry():
    newer_empty = {"title": "Key Node", "generated_at": "2026-10-02T00:00:00+00:00",
                   "content_hash": "h"}
    db = {WIN_KEY: _entry("kept"), POSIX_KEY: newer_empty}
    canonicalize_keys(db)
    assert db[POSIX_KEY]["summary"] == "kept"


def test_collision_the_entry_matching_current_content_beats_a_newer_stale_one():
    db = {
        WIN_KEY: _entry("current", at="2026-09-01T00:00:00+00:00", chash="cur"),
        POSIX_KEY: _entry("stale", at="2026-10-01T00:00:00+00:00", chash="old"),
    }
    canonicalize_keys(db, current_hash=lambda key: "cur" if key == POSIX_KEY else None)
    assert db[POSIX_KEY]["summary"] == "current"


# ═══════════════════════════════════════════════════════════════════════════
# The writer: both modes rewrite a legacy Windows sidecar once
# ═══════════════════════════════════════════════════════════════════════════

def _stub_ladder(monkeypatch, mod):
    mod._sb.reset_backend_cache()
    calls: list[str] = []

    def _select(**_kw):
        mod._sb._BACKEND_CACHE["choice"] = "cli"
        return "cli"

    monkeypatch.setattr(mod._sb, "select_backend", _select)
    monkeypatch.setattr(mod._sb, "call_llm", lambda p, **_kw: calls.append(p) or "GEN")
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    return calls


def test_bulk_mode_rekeys_a_windows_sidecar_without_regenerating(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "proj"
    _seed(root)
    sidecar = root / "knowledge" / ".node_formats.json"
    sidecar.write_text(json.dumps({WIN_KEY: _entry(KEPT, chash=_hash(NODE_TEXT))}),
                       encoding="utf-8")
    mod = _load_generator()
    calls = _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", "--all",
                                      "--knowledge-dir", str(root / "knowledge")])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    out = capsys.readouterr().out
    assert ei.value.code == 0, out
    db = json.loads(sidecar.read_text(encoding="utf-8"))
    assert list(db) == [POSIX_KEY]
    assert db[POSIX_KEY]["summary"] == KEPT
    assert calls == [], "the re-keyed entry is current — nothing may regenerate"
    assert "canonical POSIX paths" in out


def test_single_mode_rekeys_a_windows_sidecar_without_regenerating(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "proj"
    node = _seed(root)
    sidecar = root / "knowledge" / ".node_formats.json"
    sidecar.write_text(
        json.dumps({WIN_KEY: _entry(KEPT, chash=_hash(NODE_TEXT)),
                    "knowledge\\other.md": _entry("other")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load_generator()
    calls = _stub_ladder(monkeypatch, mod)
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    assert "hash match" in capsys.readouterr().out
    db = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sorted(db) == [POSIX_KEY, "knowledge/other.md"]
    assert db[POSIX_KEY]["summary"] == KEPT
    assert calls == []


# ═══════════════════════════════════════════════════════════════════════════
# summary_health.scan_pending — the separator alone never makes a node pending
# ═══════════════════════════════════════════════════════════════════════════

def test_scan_pending_finds_a_windows_keyed_entry(tmp_path):
    _seed(tmp_path)
    (tmp_path / "knowledge" / ".node_formats.json").write_text(
        json.dumps({WIN_KEY: _entry("ok", chash=_hash(NODE_TEXT),
                                    backend=summary_health.PREFERRED_TIER)}),
        encoding="utf-8",
    )
    pending = summary_health.scan_pending(tmp_path)
    assert pending.kg_missing == []
    assert pending.kg_stale == []


def test_scan_pending_counts_a_node_with_both_key_forms_once(tmp_path):
    _seed(tmp_path)
    (tmp_path / "knowledge" / ".node_formats.json").write_text(
        json.dumps({WIN_KEY: _entry("w", chash=_hash(NODE_TEXT), backend="ollama"),
                    POSIX_KEY: _entry("p", chash=_hash(NODE_TEXT), backend="ollama")}),
        encoding="utf-8",
    )
    pending = summary_health.scan_pending(tmp_path)
    assert pending.kg_stale == [POSIX_KEY]
    assert pending.kg_missing == []


# ═══════════════════════════════════════════════════════════════════════════
# The MCP reader
# ═══════════════════════════════════════════════════════════════════════════

def test_the_mcp_reader_finds_a_summary_stored_under_a_windows_key(
    tmp_path, monkeypatch
):
    core = pytest.importorskip("weaviate_mcp.kg_query_core")
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / ".node_formats.json").write_text(
        json.dumps({WIN_KEY: _entry("served")}), encoding="utf-8"
    )
    monkeypatch.setattr(core, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(core, "_node_formats_cache", None)
    monkeypatch.setattr(core, "_node_formats_by_collection", {})
    assert core._get_node_format(POSIX_KEY, "summary") == "served"
    assert core._get_node_format(
        POSIX_KEY, "description", core.KG_COLLECTION) == "served"
    # A caller holding a host-shaped path is served the same entry.
    assert core._get_node_format(WIN_KEY, "summary") == "served"
