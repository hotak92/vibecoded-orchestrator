# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M2 — code-summary sidecar generator + summary_backends extraction.

Covers (plan §3 Tests):
  * FROZEN v1 key composition (``file_path::full_name``) + entry shape.
  * content_hash-gated skip (unchanged row → no LLM call).
  * --max-per-run cap honored; a second run resumes where the first stopped.
  * priority order (n_callers DESC, total_chunks DESC, name).
  * multi-chunk entities get per-chunk summaries.
  * triviality skip (short body → one_liner only).
  * GC of dead keys (scanned collections only; foreign entries preserved).
  * backend=skip → exit 0, sidecar untouched.
  * atomic write (tmp+rename — no partial JSON).
  * ladder extraction: summary_backends selection order + env-alias fallback;
    generate-kg-summary.py thin-wrapper identity (behaviour preserved — the
    heavier consent-gate suite lives in test_kg_summary_openai_consent.py and
    runs UNCHANGED against the wrapper).

All synthetic fixtures — no Weaviate, no LLM, no project-identifying strings.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GEN_PATH = _REPO_ROOT / "templates" / "scripts" / "generate-code-summary.py"
_KG_PATH = _REPO_ROOT / "templates" / "scripts" / "generate-kg-summary.py"
_SB_PATH = _REPO_ROOT / "templates" / "scripts" / "summary_backends.py"


def _load(name: str, path: Path) -> types.ModuleType:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gen(monkeypatch, tmp_path):
    """Fresh generator module with a fake backend + no real I/O targets."""
    monkeypatch.setenv("KG_PROJECT_ROOT", str(tmp_path))
    mod = _load("_m2_gen_under_test", _GEN_PATH)
    mod._sb.reset_backend_cache()
    # Deterministic fake ladder: backend picked = "cli"; every LLM call is
    # recorded so tests can count/inspect them.
    calls: list[str] = []
    monkeypatch.setattr(mod, "select_backend", lambda: "cli")
    monkeypatch.setattr(mod, "call_llm", lambda p: (calls.append(p), f"S{len(calls)}")[1])
    mod._sb._BACKEND_CACHE["choice"] = "cli"
    mod._test_calls = calls
    return mod


def _row(full_name, file_path, *, content_hash="h1", n_callers=None,
         total_chunks=1, body="x" * 400, signature="def f()", doc=""):
    return {
        "full_name": full_name, "file_path": file_path,
        "content_hash": content_hash, "n_callers": n_callers,
        "total_chunks": total_chunks, "_body": body,
        "signature": signature, "doc": doc, "language": "python",
    }


class _FakeClient:
    def close(self):
        pass


def _wire_rows(monkeypatch, mod, func_rows, class_rows=()):
    monkeypatch.setattr(mod, "_collection_prefix", lambda name: "Proj")
    monkeypatch.setattr(mod, "_connect_weaviate", lambda: _FakeClient())
    rows_by_base = {"CodeFunction": list(func_rows), "CodeClass": list(class_rows)}
    monkeypatch.setattr(
        mod, "_iter_canonical_rows",
        lambda client, prefix, base: rows_by_base.get(base, []),
    )
    monkeypatch.setattr(
        mod, "_fetch_chunk_bodies",
        lambda client, prefix, base, full_name: [(1, "part one"), (2, "part two")],
    )


def _sidecar(tmp_path) -> Path:
    return tmp_path / ".claude" / ".code_formats.json"


# ─────────────────────────── key + entry shape ───────────────────────────────


def test_entry_key_is_composite(gen):
    assert gen.entry_key("src/a.py", "a.f") == "src/a.py::a.f"


def test_frozen_v1_entry_shape(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.f", "src/m.py")])
    assert gen.run("AnyProj", project_root=tmp_path, max_per_run=10,
                   force=False) == 0
    data = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    entry = data["src/m.py::m.f"]
    assert set(entry) == {
        "full_name", "file_path", "collection", "one_liner", "summary",
        "generated_at", "content_hash", "backend",
    }
    assert entry["full_name"] == "m.f"
    assert entry["file_path"] == "src/m.py"
    assert entry["collection"] == "CodeFunction"
    assert entry["content_hash"] == "h1"
    assert entry["backend"] == "cli"
    assert entry["one_liner"] and entry["summary"]


def test_multichunk_entry_gains_chunk_summaries(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.big", "src/m.py", total_chunks=3)])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    entry = json.loads(_sidecar(tmp_path).read_text())["src/m.py::m.big"]
    assert entry["total_chunks"] == 3
    assert set(entry["chunk_summaries"]) == {"1", "2"}


# ─────────────────────────── staleness gating ────────────────────────────────


def test_unchanged_row_makes_no_llm_call(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.f", "src/m.py", content_hash="h1")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    n_first = len(gen._test_calls)
    assert n_first > 0
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    assert len(gen._test_calls) == n_first, "hash-matched entry must be skipped"


def test_hash_drift_regenerates(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.f", "src/m.py", content_hash="h1")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    n_first = len(gen._test_calls)
    _wire_rows(monkeypatch, gen, [_row("m.f", "src/m.py", content_hash="h2")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    assert len(gen._test_calls) > n_first
    entry = json.loads(_sidecar(tmp_path).read_text())["src/m.py::m.f"]
    assert entry["content_hash"] == "h2"


def test_needs_generation_no_row_hash_never_churns(gen):
    # Pre-v0.2.61 rows without content_hash: existing entry is kept as-is.
    assert gen.needs_generation({"content_hash": "old"}, "", force=False) is False
    assert gen.needs_generation(None, "", force=False) is True


# ─────────────────────────── cap + resume ────────────────────────────────────


def test_cap_honored_and_second_run_resumes(gen, monkeypatch, tmp_path):
    rows = [_row(f"m.f{i}", "src/m.py", n_callers=10 - i) for i in range(5)]
    _wire_rows(monkeypatch, gen, rows)
    gen.run("AnyProj", project_root=tmp_path, max_per_run=2, force=False)
    data = json.loads(_sidecar(tmp_path).read_text())
    assert len(data) == 2
    # Highest-priority (n_callers desc) first.
    assert "src/m.py::m.f0" in data and "src/m.py::m.f1" in data
    gen.run("AnyProj", project_root=tmp_path, max_per_run=2, force=False)
    data = json.loads(_sidecar(tmp_path).read_text())
    assert len(data) == 4, "second run must continue with the NEXT stale set"
    assert "src/m.py::m.f2" in data and "src/m.py::m.f3" in data


def test_resolve_max_per_run_env_coercion(gen):
    assert gen.resolve_max_per_run(None, {}) == 150
    assert gen.resolve_max_per_run(None, {"VCO_CODE_SUMMARY_MAX_PER_RUN": "25"}) == 25
    # Empty-string / unparseable env → default (v0.2.27 discipline).
    assert gen.resolve_max_per_run(None, {"VCO_CODE_SUMMARY_MAX_PER_RUN": ""}) == 150
    assert gen.resolve_max_per_run(None, {"VCO_CODE_SUMMARY_MAX_PER_RUN": "x"}) == 150
    assert gen.resolve_max_per_run(7, {"VCO_CODE_SUMMARY_MAX_PER_RUN": "25"}) == 7


# ─────────────────────────── priority order ──────────────────────────────────


def test_plan_work_priority_order(gen):
    rows = [
        _row("m.low", "a.py", n_callers=1),
        _row("m.hub", "a.py", n_callers=9),
        _row("m.null", "a.py", n_callers=None),          # missing → 0
        _row("m.chunky", "a.py", n_callers=1, total_chunks=4),
    ]
    ordered = gen.plan_work(rows, {}, force=False)
    names = [r["full_name"] for r in ordered]
    assert names == ["m.hub", "m.chunky", "m.low", "m.null"]


def test_plan_work_skips_unkeyable_rows(gen):
    rows = [_row("", "a.py"), _row("m.f", ""), _row("m.ok", "a.py")]
    assert [r["full_name"] for r in gen.plan_work(rows, {}, force=False)] == ["m.ok"]


# ─────────────────────────── triviality skip ─────────────────────────────────


def test_trivial_body_gets_one_liner_only(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.tiny", "src/m.py", body="def t(): pass")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    entry = json.loads(_sidecar(tmp_path).read_text())["src/m.py::m.tiny"]
    assert entry["one_liner"]
    assert entry["summary"] == "", "trivial body → one_liner only (D3)"
    assert len(gen._test_calls) == 1, "exactly ONE LLM call for a trivial body"


# ─────────────────────────── GC of dead keys ─────────────────────────────────


def test_gc_prunes_dead_keys_only_for_scanned_collections(gen):
    formats = {
        "a.py::m.live": {"collection": "CodeFunction"},
        "a.py::m.dead": {"collection": "CodeFunction"},
        "a.py::m.other": {"collection": "CodeSomethingElse"},
    }
    removed = gen.gc_dead_keys(formats, {"a.py::m.live"})
    assert removed == 1
    assert "a.py::m.dead" not in formats
    assert "a.py::m.other" in formats, "foreign collections must survive GC"


def test_run_gc_removes_renamed_entity_old_key(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.old", "src/m.py")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    _wire_rows(monkeypatch, gen, [_row("m.new", "src/m.py")])
    gen.run("AnyProj", project_root=tmp_path, max_per_run=10, force=False)
    data = json.loads(_sidecar(tmp_path).read_text())
    assert "src/m.py::m.new" in data
    assert "src/m.py::m.old" not in data


# ─────────────────────────── backend=skip / soft-fail ────────────────────────


def test_backend_skip_exits_zero_sidecar_untouched(gen, monkeypatch, tmp_path):
    _wire_rows(monkeypatch, gen, [_row("m.f", "src/m.py")])
    monkeypatch.setattr(gen, "select_backend", lambda: "skip")
    sidecar = _sidecar(tmp_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"pre": {"collection": "CodeFunction"}}')
    assert gen.run("AnyProj", project_root=tmp_path, max_per_run=10,
                   force=False) == 0
    assert json.loads(sidecar.read_text()) == {"pre": {"collection": "CodeFunction"}}
    assert gen._test_calls == []


def test_unresolvable_prefix_is_a_noop(gen, monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "_collection_prefix", lambda name: None)
    assert gen.run("AnyProj", project_root=tmp_path, max_per_run=10,
                   force=False) == 0
    assert not _sidecar(tmp_path).exists()


def test_per_entity_failure_isolated(gen, monkeypatch, tmp_path):
    rows = [_row("m.boom", "src/m.py", n_callers=9),
            _row("m.ok", "src/m.py", n_callers=1)]
    _wire_rows(monkeypatch, gen, rows)

    def _flaky(kind, full_name, signature, doc, body):
        if full_name == "m.boom":
            raise RuntimeError("llm exploded")
        return "fine"

    monkeypatch.setattr(gen, "generate_one_liner", _flaky)
    assert gen.run("AnyProj", project_root=tmp_path, max_per_run=10,
                   force=False) == 0
    data = json.loads(_sidecar(tmp_path).read_text())
    assert "src/m.py::m.ok" in data and "src/m.py::m.boom" not in data


# ─────────────────────────── atomic write ────────────────────────────────────


def test_atomic_write_no_tmp_leftover_valid_json(gen, tmp_path):
    target = tmp_path / ".claude" / ".code_formats.json"
    gen.atomic_write_json(target, {"k": {"v": 1}})
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": {"v": 1}}
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == [], "tmp file must be renamed away"


def test_load_formats_recovers_from_corrupt_sidecar(gen, tmp_path):
    p = tmp_path / ".claude" / ".code_formats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert gen.load_formats(p) == {}


# ─────────────────────────── ladder extraction ───────────────────────────────


def test_summary_backends_forced_env_and_alias_fallback(monkeypatch):
    sb = _load("_m2_sb_under_test", _SB_PATH)
    sb.reset_backend_cache()
    # Alias key wins when set...
    monkeypatch.setenv("CODE_SUMMARY_BACKEND", "ollama")
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "skip")
    choice = sb.select_backend(
        env_keys=("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND"))
    assert choice == "ollama"
    # ...and falls back to the shared knob when absent.
    sb.reset_backend_cache()
    monkeypatch.delenv("CODE_SUMMARY_BACKEND")
    choice = sb.select_backend(
        env_keys=("CODE_SUMMARY_BACKEND", "KG_SUMMARY_BACKEND"))
    assert choice == "skip"
    # KG default key path unchanged.
    sb.reset_backend_cache()
    assert sb.select_backend() == "skip"


def test_summary_backends_invalid_forced_value_falls_through(monkeypatch):
    sb = _load("_m2_sb_invalid", _SB_PATH)
    sb.reset_backend_cache()
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "bogus")
    # Auto-detect path: pin every probe to unavailable → "skip".
    monkeypatch.setattr(sb, "cli_available", lambda: False)
    monkeypatch.setattr(sb, "ollama_available", lambda: False)
    monkeypatch.setattr(sb, "openai_available", lambda: False)
    monkeypatch.setattr(sb, "api_available", lambda: False)
    assert sb.select_backend() == "skip"


def test_summary_backends_selection_order(monkeypatch):
    sb = _load("_m2_sb_order", _SB_PATH)
    monkeypatch.delenv("KG_SUMMARY_BACKEND", raising=False)
    # CLI wins over Ollama; Ollama over API tiers; API last before skip.
    for probes, expected in [
        ({"cli": True, "ollama": True, "openai": False, "api": True}, "cli"),
        ({"cli": False, "ollama": True, "openai": False, "api": True}, "ollama"),
        ({"cli": False, "ollama": False, "openai": False, "api": True}, "api"),
        ({"cli": False, "ollama": False, "openai": False, "api": False}, "skip"),
    ]:
        sb.reset_backend_cache()
        monkeypatch.setattr(sb, "cli_available", lambda p=probes: p["cli"])
        monkeypatch.setattr(sb, "ollama_available", lambda p=probes: p["ollama"])
        monkeypatch.setattr(sb, "openai_available", lambda p=probes: p["openai"])
        monkeypatch.setattr(sb, "api_available", lambda p=probes: p["api"])
        assert sb.select_backend() == expected, probes


def test_kg_wrapper_reexports_are_shared_objects(monkeypatch, tmp_path):
    """The KG generator's ladder names must BE the summary_backends objects
    (identity, not copies) — a fork re-opens the duplication M-2 closed."""
    monkeypatch.setenv("KG_PROJECT_ROOT", str(tmp_path))
    kg = _load("_m2_kg_wrapper", _KG_PATH)
    sb = kg._sb
    assert kg.cli_available is sb.cli_available
    assert kg.call_ollama is sb.call_ollama
    assert kg.openai_consent_granted is sb.openai_consent_granted
    assert kg._BACKEND_CACHE is sb._BACKEND_CACHE
    # _FORCE_API stays a wrapper-module flag read at call time.
    assert kg._FORCE_API is False


def test_kg_wrapper_force_api_flag_threads_through(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_PROJECT_ROOT", str(tmp_path))
    kg = _load("_m2_kg_wrapper2", _KG_PATH)
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return "skip"

    monkeypatch.setattr(kg._sb, "select_backend", _spy)
    kg._FORCE_API = True
    assert kg.select_backend() == "skip"
    assert seen["force_api"] is True
