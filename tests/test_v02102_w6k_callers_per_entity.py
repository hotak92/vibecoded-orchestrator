# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-K item 9 — `structure callers --hook-format` must pair each
matched entity with ITS OWN callers, never one shared list.

The defect (seen live in v0.2.101, reproduced on this branch): the identity
lookup is exact-then-TOLERANT, so a bare-leaf target ("prompt") can resolve
to several DIFFERENT functions; the hook-format branch then built ONE
callers list from the requested target's name terms and stamped it on every
row — five different functions each carrying the same
``callers: ... (+43)`` line.

Driven hermetically: a fake Weaviate client + a fake ``Filter`` that records
the ``contains_any`` terms, so the per-entity fetches are distinguishable
from the requested-target fetch without any backend.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"


@pytest.fixture(scope="module")
def qcg():
    for p in (str(SCRIPTS_DIR), str(REPO_ROOT / "claude_mcp_servers"), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("query_code_graph")


class _Row:
    def __init__(self, uuid: str, **props):
        self.uuid = uuid
        self.properties = props
        self.references = None


class _FakeFilter:
    """Records the property + terms so the fake collection can route the
    identity fetches and the (per-entity) callers fetches apart."""

    prop = None
    terms = None
    value = None

    def __and__(self, other):
        return self

    @classmethod
    def by_property(cls, prop):
        f = _FakeFilter()
        f.prop = prop
        return f

    def contains_any(self, terms):
        self.terms = tuple(terms)
        return self

    def equal(self, value):
        self.value = value
        return self


class _FakeQuery:
    def __init__(self, coll):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=10, return_references=None,
                      **kwargs):
        f = filters if isinstance(filters, _FakeFilter) else None
        if f is not None and f.prop == "call_names":
            pool = self._coll.caller_pools.get(f.terms, [])
            return SimpleNamespace(objects=list(pool))
        # identity passes (full_name / name) — the tolerant read ignores the
        # filter, exactly like the real tokenized Equal does.
        return SimpleNamespace(objects=list(self._coll.target_rows))


class _FakeColl:
    def __init__(self, target_rows, caller_pools):
        self.target_rows = target_rows
        self.caller_pools = caller_pools
        self.query = _FakeQuery(self)


class _FakeClient:
    def __init__(self, coll):
        self.collections = SimpleNamespace(get=lambda _name: coll)

    def close(self):
        pass


def _drive(qcg, monkeypatch, capsys, target, target_rows, caller_pools):
    monkeypatch.setattr(qcg, "Filter", _FakeFilter)
    q = qcg.CodeGraphQuery(project=None)
    q.client = _FakeClient(_FakeColl(target_rows, caller_pools))
    q.query_structure("callers", target, hook_format=True,
                      indexed_revision=False, source_file=None,
                      exclude_file=None)
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("CODE:")]


def test_each_matched_entity_carries_its_own_callers(qcg, monkeypatch, capsys):
    """RED pre-fix: every row below carried the SAME callers list (the one
    built from the requested target's terms)."""
    target = "prompt"
    target_rows = [
        _Row("u-a", full_name="mod.matches_prompt", name="matches_prompt",
             file_path="a.py", start_line=1, language="python"),
        _Row("u-b", full_name="mod.prompt_consent", name="prompt_consent",
             file_path="b.py", start_line=2, language="python"),
        _Row("u-c", full_name="install.prompt", name="prompt",
             file_path="c.py", start_line=3, language="python"),
    ]

    def _callers(*names):
        return [_Row(f"caller-{n}", full_name=f"callermod.{n}", name=n,
                     file_path=f"{n}.py", start_line=9, language="python")
                for n in names]

    caller_pools = {
        # the REQUESTED target's terms — pre-fix this list reached every row
        ("prompt",): _callers("zz_shared_caller"),
        # each tolerant-matched entity's own terms
        ("mod.matches_prompt", "matches_prompt"): _callers("m1", "m2"),
        ("mod.prompt_consent", "prompt_consent"): _callers("c1"),
        ("install.prompt", "prompt"): _callers("p1", "p2", "p3"),
    }
    rows = _drive(qcg, monkeypatch, capsys, target, target_rows, caller_pools)
    assert len(rows) == 3, rows
    by_name = {r.split(" | ")[0][len("CODE: "):]: r for r in rows}
    assert set(by_name) == {
        "mod.matches_prompt", "mod.prompt_consent", "install.prompt"}, rows
    assert "callermod.m1" in by_name["mod.matches_prompt"]
    assert "callermod.m2" in by_name["mod.matches_prompt"]
    assert "callermod.c1" in by_name["mod.prompt_consent"]
    assert "callermod.p1" in by_name["install.prompt"]
    # THE defect: the requested target's shared list on every row
    for r in rows:
        assert "zz_shared_caller" not in r, (
            f"one symbol's callers stamped on a row it does not belong to: {r}"
        )
        assert "callermod.m1" not in r or "mod.matches_prompt" in r
    # and no cross-contamination between the entities
    assert "callermod.c1" not in by_name["mod.matches_prompt"]
    assert "callermod.m1" not in by_name["install.prompt"]


def test_exact_single_match_is_unchanged(qcg, monkeypatch, capsys):
    """The common case — one exact match — keeps the caller pairing it
    always had (the requested target's own fetch, reused, no extra read)."""
    target = "vco_lib.install._start_services"
    target_rows = [
        _Row("u-1", full_name="vco_lib.install._start_services",
             name="_start_services", file_path="vco_lib/install.py",
             start_line=42, language="python"),
    ]
    caller_pools = {
        ("vco_lib.install._start_services", "_start_services"): [
            _Row("caller-x", full_name="install.main", name="main",
                 file_path="install.py", start_line=7, language="python"),
        ],
    }
    rows = _drive(qcg, monkeypatch, capsys, target, target_rows, caller_pools)
    assert len(rows) == 1, rows
    assert "vco_lib.install._start_services" in rows[0]
    assert "install.main@install.py:7" in rows[0]


def test_chunked_rows_collapse_to_one_entity(qcg, monkeypatch, capsys):
    """A chunked function is N rows sharing a full_name; the block is "one
    CODE: row per matched ENTITY" — the canonical (chunk-0) row's def line,
    not one row per chunk."""
    target = "mod.big"
    target_rows = [
        _Row("u-c0", full_name="mod.big", name="big", file_path="mod.py",
             start_line=10, language="python", chunk_num=0),
        _Row("u-c1", full_name="mod.big", name="big", file_path="mod.py",
             start_line=50, language="python", chunk_num=1),
        _Row("u-c2", full_name="mod.big", name="big", file_path="mod.py",
             start_line=90, language="python", chunk_num=2),
    ]
    caller_pools = {
        ("mod.big", "big"): [
            _Row("caller-y", full_name="mod.other", name="other",
                 file_path="other.py", start_line=3, language="python"),
        ],
    }
    rows = _drive(qcg, monkeypatch, capsys, target, target_rows, caller_pools)
    assert len(rows) == 1, rows
    assert "def mod.py:10" in rows[0], "the canonical chunk's def line"
    assert "mod.other@other.py:3" in rows[0]
