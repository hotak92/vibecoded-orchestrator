# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the two code-graph write-path defects, WIRED THROUGH THE ANALYZER.

The pure rules are pinned in ``tests/test_v0292_codegraph_identity_and_chunk_plan.py``.
This file drives the REAL ``_dedup_insert`` / ``write_file_extraction`` against
fake collections and pins the things only the wiring can answer.

DEFECT A — the pre-chunk embed that was always discarded
--------------------------------------------------------
``_resolve_deferred_embed`` hashes the FULL body as a single chunk
(``chunk_num=0, total_chunks=1``). A multi-chunk entity's stored canonical row
holds the CHUNK-0 text, which carries a ``[chunk 1/N]`` header, so the hashes
can never match: the resolver ALWAYS embedded and ``_maybe_chunk_and_write``
ALWAYS threw that vector away. One wasted MAX-SIZE embed per multi-chunk entity
per walk — 3,147 such entities on the dev machine, ~53% of the residual cost of
a converged ``--force-rewalk``, and now on the UPDATE path for every user since
the re-index trigger wired ``--force-rewalk`` into ``install_project_bundle``.

Several tests below assert BOTH directions by neutering
``_plan_chunk_texts_for`` (→ ``None``), which restores the pre-fix order
exactly. That makes the defect itself a permanent regression pin, not just a
one-off red-proof.

DEFECT B — same-named symbols in one file shared a UUID
-------------------------------------------------------
``full_name`` is ``{file_stem}.{symbol}`` in every producer, so the later write
silently overwrote the earlier. Same both-directions treatment, by neutering
``guards.assign_duplicate_identity_suffixes``.

RISKS COVERED (PLAN-v0292-codegraph-write-path §2.5 / §3.6)
  A-1 chunked rows still carry language/project_source/file_path/is_test/doc
  A-2 ``_deferred_embed`` never strands in the Weaviate kwargs
  A-3 the decision and the write cannot disagree (chunker called ONCE)
  A-4 the content hash does not drift
  A-5 a chunker raise behaves exactly as before
  A-6 legacy unbound-method stubs keep working
  A-7 chunk 0 never inherits the parent's full-body vector
  B-1 occurrence uuids never enter the chunk-key space
  B-4 ``extras`` is copied, never mutated in the producer's dict
  B-5 ``_identity_key`` never becomes a stored property
  B-9 a mid-file failure leaves the reconcile scope uncommitted
"""
from __future__ import annotations

import hashlib
import logging
import importlib.util
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from vco_lib import codegraph_guards as guards
from vco_lib.codegraph_entities import (
    KIND_CLASS,
    KIND_FUNCTION,
    CodeEntity,
    FileExtraction,
    ModuleDescriptor,
)

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0292_writepath_analyzer", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def am() -> types.ModuleType:
    return _load_analyzer()


# ── fakes ───────────────────────────────────────────────────────────────────


class _Existing:
    def __init__(self, properties):
        self.properties = properties


class _Data:
    def __init__(self):
        self.replaced: List[dict] = []
        self.inserted: List[dict] = []
        self.updated: List[dict] = []

    def replace(self, uuid, **kw):
        self.replaced.append({"uuid": uuid, **kw})

    def insert(self, uuid, **kw):
        self.inserted.append({"uuid": uuid, **kw})

    def update(self, uuid=None, properties=None, **kw):
        self.updated.append({"uuid": uuid, "properties": properties})

    @property
    def written(self) -> List[dict]:
        return self.replaced + self.inserted


class _Query:
    def __init__(self, stored: Dict[str, dict]):
        self.stored = stored

    def fetch_object_by_id(self, uuid, return_properties=None):
        props = self.stored.get(uuid)
        if props is None:
            return None
        if return_properties:
            props = {k: props.get(k) for k in return_properties}
        return _Existing(props)


class _Coll:
    def __init__(self, name: str, stored: Optional[Dict[str, dict]] = None):
        self.name = name
        self.data = _Data()
        self.query = _Query(stored or {})


def _analyzer(am, *, language="python", source=""):
    inst = am.CodeGraphAnalyzer.__new__(am.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst._track_visited = True
    inst._current_language = language
    inst._current_source = source
    inst.visited_uuids = set()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst._embed_skip_fingerprint = None
    return inst


_BIG = "def big():\n" + "\n".join(
    f"    r_{i} = compute(input_{i}) + offset_{i} * scale_{i}" for i in range(600)
) + "\n    return r_0\n"


def _fn_props(body: str = _BIG, **over):
    props = {
        "name": "big", "full_name": "mod.big", "function_body": body,
        "signature": "def big()", "type_uses": [], "cfg_summary": "",
        "data_flow_vars": [],
    }
    props.update(over)
    return props


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT A — the measurable outcome
# ═══════════════════════════════════════════════════════════════════════════


def _seed_converged_chunks(am, coll, identity, chunk_bodies, base_props, rev=None):
    """Pre-store every chunk row hash-matched at the CURRENT revision — i.e. a
    fully converged multi-chunk entity, exactly what a re-walk finds."""
    rev = am.CODEGRAPH_EMBED_REVISION if rev is None else rev
    for i, cb in enumerate(chunk_bodies):
        key = identity if i == 0 else f"{identity}::{i}"
        det = am._deterministic_uuid("P", "src/mod.py", key, project_source="")
        hp = dict(base_props)
        hp["function_body"] = cb
        hp["chunk_num"] = i
        hp["total_chunks"] = len(chunk_bodies)
        coll.query.stored[det] = {
            "content_hash": am._content_hash_for_object("P_CodeFunction", hp),
            "embed_revision": rev,
            "total_chunks": len(chunk_bodies),
        }


def _drive_converged_multichunk(am, monkeypatch, *, neuter_hoist: bool):
    """Run one converged 3-chunk function through the real ``_dedup_insert``.

    Returns ``(full_body_embeds, per_chunk_embeds, coll)``.
    """
    chunk_bodies = ["[chunk 1/3]\n\ndef big(): #c0", "[chunk 2/3]\n\n#c1",
                    "[chunk 3/3]\n\n#c2"]
    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding",
                        lambda s, b, **kw: list(chunk_bodies))
    per_chunk = {"n": 0}
    monkeypatch.setattr(am, "generate_embedding",
                        lambda t: per_chunk.__setitem__("n", per_chunk["n"] + 1) or [0.9])
    if neuter_hoist:
        # Restore the PRE-FIX order EXACTLY: the HOIST (soft=True) reports
        # nothing, so the resolver runs first; the fan-out's own derivation
        # (soft=False) still works, so it still chunks and still discards the
        # resolver's vector — which is the defect.
        _real = am._plan_chunk_texts_for
        monkeypatch.setattr(
            am, "_plan_chunk_texts_for",
            lambda *a, soft=True, **kw: None if soft else _real(*a, soft=soft, **kw),
        )

    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    base = _fn_props(body="def big(): ...")
    _seed_converged_chunks(am, coll, "mod.big", chunk_bodies, base)

    full_body = {"n": 0}

    def _embed_full():
        full_body["n"] += 1
        return [0.1]

    params = {"properties": dict(base), "_deferred_embed": _embed_full}
    inst._dedup_insert(coll, params, "mod.big", file_path_rel="src/mod.py")
    return full_body["n"], per_chunk["n"], coll, params


def test_A_converged_multichunk_entity_wastes_no_embed(am, monkeypatch):
    """THE HEADLINE. A fully converged multi-chunk entity now costs ZERO embeds.

    Both directions asserted: neutering the hoist reproduces the defect (one
    full-body embed, discarded), so this is the defect's permanent regression
    pin as well as the fix's proof.
    """
    full, per_chunk, coll, _ = _drive_converged_multichunk(
        am, monkeypatch, neuter_hoist=False)
    assert full == 0, (
        f"the full-body embed must be skipped for a multi-chunk entity "
        f"(got {full}) — it was always discarded by the fan-out"
    )
    assert per_chunk == 0, "a converged entity must not re-embed its chunks"
    assert coll.data.written == [], "a converged entity writes nothing"


def test_A_defect_reproduces_when_the_hoist_is_neutered(am, monkeypatch):
    """The red half of the pin: with ``_plan_chunk_texts_for`` returning None
    the pre-fix order is restored and the wasted embed comes back."""
    full, per_chunk, coll, _ = _drive_converged_multichunk(
        am, monkeypatch, neuter_hoist=True)
    assert full == 1, "pre-fix: the resolver embeds the full body"
    assert per_chunk == 0, "…and the fan-out still discards it"
    assert coll.data.written == []


def test_A_single_chunk_entity_still_uses_the_resolver(am, monkeypatch):
    """The 91% case is untouched: an in-budget entity keeps the SKIP/STAMP/EMBED
    resolver, so a converged single-chunk row still costs zero embeds and a
    changed one still costs exactly one."""
    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding",
                        lambda s, b, **kw: ["only one"])
    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    props = _fn_props(body="def small(): return 1", full_name="mod.small")

    stored = dict(props)
    stored["chunk_num"] = 0
    stored["total_chunks"] = 1
    det = am._deterministic_uuid("P", "src/mod.py", "mod.small", project_source="")
    coll.query.stored[det] = {
        "content_hash": am._content_hash_for_object("P_CodeFunction", stored),
        "embed_revision": am.CODEGRAPH_EMBED_REVISION,
        "total_chunks": 1,
    }
    calls = {"n": 0}
    params = {
        "properties": dict(props),
        "_deferred_embed": lambda: calls.__setitem__("n", calls["n"] + 1) or [0.1],
    }
    inst._dedup_insert(coll, params, "mod.small", file_path_rel="src/mod.py")
    assert calls["n"] == 0 and coll.data.written == []


def test_A_RISK1_chunked_rows_still_carry_every_stamped_property(am, monkeypatch):
    """RISK-1, the single most dangerous way to get this fix wrong.

    Hoisting the chunk WRITE (rather than only the DECISION) would emit every
    chunk row without ``language`` / ``project_source`` / ``file_path`` /
    ``is_test`` / ``doc`` — exactly the properties the language-scoped
    ``--prune-stale`` filter and the prune-anchor resolution key on. The result
    is rows a later prune cannot see: silent corruption surfacing far from its
    cause. The fan-out must stay BELOW the stamping block.
    """
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda s, b, **kw: ["[chunk 1/3]\n\nc0", "[chunk 2/3]\n\nc1",
                            "[chunk 3/3]\n\nc2"])
    monkeypatch.setattr(am, "generate_embedding", lambda t: [0.5])
    inst = _analyzer(am, language="rust", source="/src/root")
    coll = _Coll("P_CodeFunction")
    params = {
        "properties": _fn_props(body="fn big() {\n    // Doc line.\n}"),
        "_deferred_embed": lambda: [0.1],
    }
    inst._dedup_insert(coll, params, "mod.big",
                       file_path_rel="tests/mod_test.rs")

    written = coll.data.written
    assert len(written) == 3, f"expected a 3-object fan-out, got {len(written)}"
    for obj in written:
        p = obj["properties"]
        assert p.get("language") == "rust", f"chunk {p.get('chunk_num')}: no language"
        assert p.get("project_source") == "/src/root"
        assert p.get("file_path") == "tests/mod_test.rs"
        assert p.get("is_test") is True, "is_test must reach every chunk row"
        assert p.get("doc"), "doc must reach every chunk row"


def test_A_RISK1_single_object_path_carries_the_same_stamps(am, monkeypatch):
    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding",
                        lambda s, b, **kw: ["one"])
    inst = _analyzer(am, language="rust", source="/src/root")
    coll = _Coll("P_CodeFunction")
    params = {
        "properties": _fn_props(body="fn small() {\n    // Doc line.\n}"),
        "_deferred_embed": lambda: [0.1],
    }
    inst._dedup_insert(coll, params, "mod.small",
                       file_path_rel="tests/mod_test.rs")
    p = coll.data.written[0]["properties"]
    assert p["language"] == "rust" and p["project_source"] == "/src/root"
    assert p["file_path"] == "tests/mod_test.rs" and p["is_test"] is True
    assert p["doc"]


@pytest.mark.parametrize("n_chunks", [1, 3])
def test_A_RISK2_deferred_embed_never_strands_in_the_weaviate_kwargs(
    am, monkeypatch, n_chunks
):
    """RISK-2. ``build_chunk_write_params`` does ``dict(insert_params)`` and
    ``_write_one_object`` splats it into ``data.replace(uuid=…, **params)``. A
    stranded callable is a TypeError → ``insert_errors > 0`` →
    ``walk_certifies_generation`` refuses the stamp → EVERY future update
    re-spawns a full force-rewalk, forever."""
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda s, b, **kw: [f"[chunk {i+1}/{n_chunks}]\n\nc{i}" for i in range(n_chunks)])
    monkeypatch.setattr(am, "generate_embedding", lambda t: [0.5])
    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    params = {"properties": _fn_props(), "_deferred_embed": lambda: [0.1]}
    inst._dedup_insert(coll, params, "mod.big", file_path_rel="src/mod.py")

    assert "_deferred_embed" not in params
    for obj in coll.data.written:
        assert "_deferred_embed" not in obj, (
            "a deferred-embed callable reached the Weaviate write kwargs"
        )


def test_A_RISK3_multi_chunk_decision_always_produces_a_fan_out(am, monkeypatch):
    """RISK-3. If the hoist decided "multi-chunk" (so the resolver was skipped)
    but the fan-out then declined to chunk, the single-object write would run
    with NO vector → the row is stamped vectorless and stays invisible to
    ``near_vector`` until the next walk. "Compute once, pass down" makes that
    structurally impossible; pin it."""
    chunker_calls = {"n": 0}

    def _chunk(s, b, **kw):
        chunker_calls["n"] += 1
        return ["[chunk 1/3]\n\nc0", "[chunk 2/3]\n\nc1", "[chunk 3/3]\n\nc2"]

    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding", _chunk)
    monkeypatch.setattr(am, "generate_embedding", lambda t: [0.5])
    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    params = {"properties": _fn_props(), "_deferred_embed": lambda: [0.1]}
    returned = inst._dedup_insert(coll, params, "mod.big",
                                  file_path_rel="src/mod.py")

    assert chunker_calls["n"] == 1, (
        f"the chunker must run EXACTLY once per entity (got {chunker_calls['n']}) "
        "— a second derivation is a second chance to disagree with the first"
    )
    assert len(coll.data.written) == 3
    canonical = next(o for o in coll.data.written
                     if o["properties"]["chunk_num"] == 0)
    assert returned == canonical["uuid"]
    for obj in coll.data.written:
        assert obj.get("vector") == am._shape_for_insert([0.5]), (
            "every chunk needs its OWN vector"
        )


def test_A_RISK4_property_stamps_do_not_move_the_content_hash(am):
    """RISK-4. The hoist is licensed by the stamped properties being outside
    the content hash. Prove it for all five collection bases: hashing the props
    BEFORE and AFTER the stamping block must be byte-identical, or the fix
    re-writes and re-embeds every row in every project."""
    stamped = {
        "language": "rust", "project_source": "/root", "file_path": "a/b.rs",
        "is_test": True, "doc": "d",
    }
    bases = {
        "CodeModule": {"path": "a.py", "module_summary": "m", "import_names": []},
        "CodeClass": {"full_name": "a.C", "signature": "class C",
                      "class_body": "b", "methods": [], "composes": [],
                      "chunk_num": 0, "total_chunks": 1},
        "CodeFunction": {"full_name": "a.f", "signature": "def f()",
                         "function_body": "b", "type_uses": [],
                         "cfg_summary": "", "data_flow_vars": [],
                         "chunk_num": 0, "total_chunks": 1},
        "CodeAPI": {"endpoint": "/x", "method": "GET", "api_description": "d",
                    "parameters": [], "returns": ""},
        "CodeInteraction": {"interaction_type": "http", "protocol": "GET",
                            "endpoint": "/x", "raw_target": "http://x",
                            "direction": "outbound", "description": "d"},
    }
    for base, props in bases.items():
        coll = f"P_{base}"
        before = am._content_hash_for_object(coll, dict(props))
        after = am._content_hash_for_object(coll, {**props, **stamped})
        assert before == after, (
            f"{base}: the stamping block moves the content hash — the hoist "
            "would re-write and re-embed every stored row"
        )


def test_A_RISK5_chunker_raise_behaves_exactly_as_before(am, monkeypatch, caplog):
    """RISK-5. A chunker exception must not change WHERE the failure lands. The
    hoist is a soft probe (falls back to today's order); the fan-out's own
    derivation stays hard, so the raise still propagates out of
    ``_dedup_insert`` exactly as it did before the change.

    v0.2.92 addendum (user, 2026-09-02): the FALLBACK is unchanged, but it is no
    longer SILENT. The soft path counts the degrade and emits a warning naming
    the entity and the consequence — a swallowed exception with no trace is the
    exact failure class this release exists to remove.
    """
    def _raise(*a, **kw):
        raise RuntimeError("chunker exploded")

    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding", _raise)
    inst = _analyzer(am, language="rust")
    coll = _Coll("P_CodeFunction")
    embeds = {"n": 0}
    params = {
        "properties": _fn_props(),
        "_deferred_embed": lambda: embeds.__setitem__("n", embeds["n"] + 1) or [0.1],
    }
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="chunker exploded"):
            inst._dedup_insert(coll, params, "mod.big", file_path_rel="src/mod.py")

    # UNCHANGED half: same fallback, same place the failure lands.
    assert embeds["n"] == 1, (
        "the resolver must still have run (the soft hoist fell back to today's "
        "order)"
    )
    # NEW half: the degrade is observable.
    assert inst._chunk_plan_failures == 1
    warned = [r.getMessage() for r in caplog.records
              if r.levelno >= logging.WARNING]
    assert any("chunk planning FAILED" in m for m in warned), warned
    msg = next(m for m in warned if "chunk planning FAILED" in m)
    assert "mod.big" in msg, "the warning must NAME the entity"
    assert "rust" in msg and "CodeFunction" in msg
    assert "UN-CHUNKED" in msg and "DEGRADED" in msg, (
        "the warning must state the CONSEQUENCE, not just the failure"
    )
    assert "chunker exploded" in msg and "RuntimeError" in msg


def test_A_soft_probe_success_warns_nothing_and_counts_nothing(am, monkeypatch, caplog):
    """The leave-alone half of the decision: a chunker that WORKS must produce
    no warning and leave the degrade counter untouched. Without this, a warning
    that fires unconditionally would pass the test above and drown the signal."""
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda s, b, **kw: ["[chunk 1/2]\n\nc0", "[chunk 2/2]\n\nc1"])
    monkeypatch.setattr(am, "generate_embedding", lambda t: [0.5])
    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    with caplog.at_level(logging.WARNING):
        inst._dedup_insert(
            coll, {"properties": _fn_props(), "_deferred_embed": lambda: [0.1]},
            "mod.big", file_path_rel="src/mod.py")
    assert getattr(inst, "_chunk_plan_failures", 0) == 0
    assert not [r for r in caplog.records if "chunk planning" in r.getMessage()]


def test_A_degrade_warning_is_itself_exception_safe(am, monkeypatch, caplog):
    """The whole point of ``soft`` is that a probe must not break the walk — so
    the WARNING must not either. A broken message builder degrades to silence,
    never to a raise."""
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        guards, "chunk_plan_degrade_warning",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("warning builder broke")))
    inst = _analyzer(am)
    # soft=True path only: assert it RETURNS rather than raising the ValueError.
    assert am._plan_chunk_texts_for(
        inst, _Coll("P_CodeFunction"), {"properties": _fn_props()}, "mod.big",
    ) is None


def test_A_degrade_warnings_are_rate_limited_then_aggregated(am, monkeypatch, caplog):
    """A chunker broken for a whole LANGUAGE must not emit thousands of
    identical lines — that is its own denial of signal. The first
    ``CHUNK_PLAN_WARN_LIMIT`` degrades are named individually, then ONE
    suppression notice, then silence; the walk's total carries the scale."""
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    inst = _analyzer(am)
    coll = _Coll("P_CodeFunction")
    n = guards.CHUNK_PLAN_WARN_LIMIT
    with caplog.at_level(logging.WARNING):
        for i in range(n + 25):
            am._plan_chunk_texts_for(
                inst, coll, {"properties": _fn_props(full_name=f"mod.f{i}")},
                f"mod.f{i}")

    assert inst._chunk_plan_failures == n + 25, "every degrade must be COUNTED"
    msgs = [r.getMessage() for r in caplog.records]
    named = [m for m in msgs if "chunk planning FAILED" in m]
    suppressed = [m for m in msgs if "further per-entity warnings suppressed" in m]
    assert len(named) == n, f"expected {n} named degrades, got {len(named)}"
    assert len(suppressed) == 1, "exactly one suppression notice"
    assert len(msgs) == n + 1, "nothing after the suppression notice"


def test_A_degrade_total_reaches_the_walk_stats_and_the_json_consumer(
    am, monkeypatch, tmp_path, capsys
):
    """Requirement 3, end to end: the DEGRADE TOTAL must leave the analyzer.

    A per-entity warning in a 20k-entity walk scrolls away; the total does not.
    This drives the real ``analyze_repository`` over a one-file repo with a
    chunker that always raises, and asserts the count reaches ``stats`` — which
    is the dict ``main()`` forwards into the ``{"final": true, ...}``
    ``--json-progress`` envelope the launcher's modal parses.

    Red-proof: dropping the ``stats['chunk_plan_failures'] = ...`` assignment
    leaves every other test in this file green.
    """
    (tmp_path / "m.py").write_text(
        "def big():\n" + "\n".join(f"    x{i} = {i}" for i in range(50)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(am, "generate_embedding", lambda text: None)
    monkeypatch.setattr(am, "embed_module", lambda summary: None)
    monkeypatch.setattr(
        am, "embed_function", lambda sig, body, language="python": None)
    monkeypatch.setattr(
        am, "embed_class",
        lambda sig, body, methods=None, language="python": None)

    inst = _analyzer(am)
    inst.client = object()
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    inst._track_visited = False
    for base, attr in (("CodeModule", "modules_collection"),
                       ("CodeClass", "classes_collection"),
                       ("CodeFunction", "functions_collection"),
                       ("CodeAPI", "apis_collection"),
                       ("CodeInteraction", "interactions_collection")):
        setattr(inst, attr, _Coll(f"P_{base}"))

    stats = inst.analyze_repository(tmp_path, language="python")

    assert "chunk_plan_failures" in stats, (
        "the degrade total must be IN the stats dict — that dict is what "
        "--json-progress forwards to the launcher modal; a count that never "
        "leaves the analyzer is not a signal"
    )
    assert stats["chunk_plan_failures"] >= 1, stats
    assert stats["chunk_plan_failures"] == inst._chunk_plan_failures
    err = capsys.readouterr().err
    assert "UN-CHUNKED" in err and "DEGRADED" in err, (
        "the walk must print ONE aggregate line naming the consequence"
    )


def test_A_walk_with_no_degrades_reports_zero_and_prints_nothing(
    am, monkeypatch, tmp_path, capsys
):
    """The leave-alone half of the aggregate: a healthy walk reports 0 and adds
    no noise. Without this, an unconditional aggregate line would pass the test
    above while making every clean walk look degraded."""
    (tmp_path / "m.py").write_text("def small():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(am, "generate_embedding", lambda text: None)
    monkeypatch.setattr(am, "embed_module", lambda summary: None)
    monkeypatch.setattr(
        am, "embed_function", lambda sig, body, language="python": None)
    monkeypatch.setattr(
        am, "embed_class",
        lambda sig, body, methods=None, language="python": None)

    inst = _analyzer(am)
    inst.client = object()
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    inst._track_visited = False
    for base, attr in (("CodeModule", "modules_collection"),
                       ("CodeClass", "classes_collection"),
                       ("CodeFunction", "functions_collection"),
                       ("CodeAPI", "apis_collection"),
                       ("CodeInteraction", "interactions_collection")):
        setattr(inst, attr, _Coll(f"P_{base}"))

    stats = inst.analyze_repository(tmp_path, language="python")
    assert stats["chunk_plan_failures"] == 0
    assert "UN-CHUNKED" not in capsys.readouterr().err


def test_A_json_progress_envelope_forwards_the_degrade_total(am):
    """The MACHINE consumer. ``main()``'s ``{"final": true, ...}`` line is what
    the Tauri re-analyze modal's stdout reader parses; a stat that stops at the
    human print is invisible to it. Pinned at the source level because building
    the envelope requires a full ``main()`` run against a live Weaviate."""
    import inspect

    src = inspect.getsource(am.main) if hasattr(am, "main") else ""
    if not src:
        src = _ANALYZER_PATH.read_text(encoding="utf-8")
    marker = '"chunk_plan_failures": stats.get("chunk_plan_failures", 0),'
    assert marker in src, (
        "the --json-progress final payload must forward chunk_plan_failures "
        "alongside insert_errors/prune_failures — otherwise the launcher can "
        "render a degraded build as a clean one"
    )


def test_A_RISK6_legacy_unbound_method_stub_still_chunks(am, monkeypatch):
    """RISK-6. Historical stubs bind ``_dedup_insert`` as an unbound method with
    only a handful of siblings. The planner is a MODULE-level function, not a
    ``self`` attribute, so there is no new method for them to be missing."""
    monkeypatch.setattr(
        am, "chunk_or_truncate_for_embedding",
        lambda s, b, **kw: ["[chunk 1/2]\n\nc0", "[chunk 2/2]\n\nc1"])
    monkeypatch.setattr(am, "generate_embedding", lambda t: [0.5])

    class _Stub:
        pass

    stub = _Stub()
    stub.project_name = "P"
    stub._track_visited = True
    stub._current_language = "python"
    stub._current_source = ""
    stub.visited_uuids = set()
    cls = am.CodeGraphAnalyzer
    for meth in ("_dedup_insert", "_maybe_chunk_and_write",
                 "_stamp_single_chunk_props", "_write_one_object"):
        setattr(stub, meth, getattr(cls, meth).__get__(stub, _Stub))

    coll = _Coll("P_CodeFunction")
    stub._dedup_insert(coll, {"properties": _fn_props(), "vector": [0.9]},
                       "mod.big", file_path_rel="src/mod.py")
    assert len(coll.data.written) == 2, (
        "a minimal stub (no _resolve_deferred_embed / _maybe_stamp_all_chunks) "
        "must still fan out"
    )


def test_A_RISK7_chunk_rows_never_inherit_the_parent_vector(am):
    """RISK-7. Reusing the parent's full-body vector for chunk 0 would put a
    vector describing a DIFFERENT text on a correct-looking row."""
    params = {"properties": {}, "vector": ["PARENT-VECTOR"]}
    with_vec = guards.build_chunk_write_params(
        params, {"function_body": "x"}, "c0", True, 0, 3, ["CHUNK-VECTOR"])
    assert with_vec["vector"] == ["CHUNK-VECTOR"]
    without = guards.build_chunk_write_params(
        params, {"function_body": "x"}, "c0", True, 0, 3, None)
    assert "vector" not in without, (
        "a failed chunk embed must leave the row vectorless, never carry the "
        "parent's full-body vector"
    )


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT B — identity collision, through the real writer
# ═══════════════════════════════════════════════════════════════════════════


def _entity(kind, name, body, *, doc=""):
    return CodeEntity(
        kind=kind, name=name.split(".")[-1], full_name=name, body=body,
        signature=f"sig {name}", doc=doc, start_line=1, end_line=2,
        project="P", file_path_rel="src/mod.rs",
        extras={"type_uses": [], "cfg_summary": "", "data_flow_vars": []}
        if kind == KIND_FUNCTION else {"methods": [], "composes": []},
        vector=[0.1],
    )


def _fx(entities):
    return FileExtraction(
        module=ModuleDescriptor(
            path="src/mod.rs", language="rust", loc=10, complexity=1.0,
            last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
            file_hash=hashlib.sha256(b"x").hexdigest(),
        ),
        entities=entities,
        stats={"modules": 1, "functions": len(entities)},
    )


def _writer(am, monkeypatch, *, neuter=False):
    monkeypatch.setattr(am, "chunk_or_truncate_for_embedding",
                        lambda s, b, **kw: [b])
    monkeypatch.setattr(am, "chunk_or_truncate_class_for_embedding",
                        lambda s, b, **kw: [b])
    if neuter:
        monkeypatch.setattr(
            guards, "assign_duplicate_identity_suffixes",
            lambda keyed: [None] * len(keyed))
    inst = _analyzer(am, language="rust")
    fns = _Coll("P_CodeFunction")
    cls = _Coll("P_CodeClass")
    inst.functions_collection = fns
    inst.classes_collection = cls
    inst.modules_collection = _Coll("P_CodeModule")
    inst.apis_collection = _Coll("P_CodeAPI")
    inst.interactions_collection = _Coll("P_CodeInteraction")
    inst._create_or_update_module = lambda **kw: "MODULE-UUID"
    return inst, fns, cls


def test_B_two_same_named_functions_now_get_two_rows(am, monkeypatch):
    """The defect, and its fix, in one test. ``impl A { fn new() }`` +
    ``impl B { fn new() }`` in one file: pre-fix ONE row survived (arbitrary
    body); post-fix both are stored and individually addressable."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "fn new() -> A { A }"),
        _entity(KIND_FUNCTION, "mod.new", "fn new() -> B { B }"),
    ]))
    written = fns.data.written
    assert len(written) == 2, f"expected 2 rows, got {len(written)}"
    assert len({o["uuid"] for o in written}) == 2, "the two rows must differ"
    bodies = {o["properties"]["function_body"] for o in written}
    assert bodies == {"fn new() -> A { A }", "fn new() -> B { B }"}


def test_B_defect_reproduces_when_the_disambiguator_is_neutered(am, monkeypatch):
    """The red half: with the suffix assignment neutered, the second write
    clobbers the first — one uuid, one surviving body."""
    inst, fns, _ = _writer(am, monkeypatch, neuter=True)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "fn new() -> A { A }"),
        _entity(KIND_FUNCTION, "mod.new", "fn new() -> B { B }"),
    ]))
    assert len({o["uuid"] for o in fns.data.written}) == 1, (
        "pre-fix: both occurrences collapse onto ONE deterministic uuid"
    )


def test_B_occurrence_1_keeps_byte_identical_uuid(am, monkeypatch):
    """THE MIGRATION-IS-A-NO-OP PROPERTY, and the most important assertion in
    this change. Occurrence 1 must mint EXACTLY the uuid it mints today, so
    every row currently stored is still written by the next walk: nothing is
    orphaned, ``--prune-stale`` sees nothing stale, and the unconditional
    per-file entity reconcile deletes nothing."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "A"),
        _entity(KIND_FUNCTION, "mod.new", "B"),
        _entity(KIND_FUNCTION, "mod.new", "C"),
        _entity(KIND_FUNCTION, "mod.solo", "S"),
    ]))
    today = {
        "mod.new": am._deterministic_uuid("P", "src/mod.rs", "mod.new",
                                          project_source=""),
        "mod.solo": am._deterministic_uuid("P", "src/mod.rs", "mod.solo",
                                           project_source=""),
    }
    by_uuid = {o["uuid"]: o["properties"] for o in fns.data.written}
    assert today["mod.new"] in by_uuid, (
        "occurrence 1 changed uuid — the migration would ORPHAN every stored "
        "colliding row instead of being a pure add"
    )
    assert by_uuid[today["mod.new"]]["function_body"] == "A", (
        "the bare-key row must hold occurrence 1's body (first-wins)"
    )
    assert today["mod.solo"] in by_uuid, "a non-colliding entity must not move"


def test_B_occurrences_2_to_n_are_distinct_and_miss_the_chunk_key_space(
    am, monkeypatch
):
    """B-RISK-1 through the real writer: the 2nd..Nth uuids are distinct from
    each other AND from every chunk uuid occurrence 1 could ever mint."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", f"body{i}") for i in range(5)
    ]))
    uuids = [o["uuid"] for o in fns.data.written]
    assert len(uuids) == 5 and len(set(uuids)) == 5

    chunk_uuids, _ = guards.chunk_identities(
        [f"c{i}" for i in range(8)], {"function_body": ""}, True, "mod.new", 8,
        uuid_fn=lambda k: am._deterministic_uuid("P", "src/mod.rs", k,
                                                 project_source=""),
        hash_fn=lambda p: "h",
    )
    overlap = set(uuids[1:]) & set(chunk_uuids)
    assert not overlap, (
        f"an occurrence uuid collides with a chunk uuid of occurrence 1: "
        f"{overlap} — the `#n` and `::n` key spaces are not disjoint"
    )


def test_B_classes_and_functions_do_not_disambiguate_each_other(am, monkeypatch):
    """Grouping is ``(kind, key)``: they live in different collections, so
    disambiguating them would needlessly change a uuid."""
    inst, fns, cls = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.X", "fn"),
        _entity(KIND_CLASS, "mod.X", "struct"),
    ]))
    assert fns.data.written[0]["uuid"] == am._deterministic_uuid(
        "P", "src/mod.rs", "mod.X", project_source="")
    assert cls.data.written[0]["uuid"] == am._deterministic_uuid(
        "P", "src/mod.rs", "mod.X", project_source="")


def test_B_RISK4_extras_is_copied_not_mutated_in_place(am, monkeypatch):
    """B-RISK-4. Producers may reuse an extras dict; mutating it in place would
    poison an unrelated entity's identity."""
    inst, fns, _ = _writer(am, monkeypatch)
    shared = {"type_uses": [], "cfg_summary": "", "data_flow_vars": []}
    e1 = _entity(KIND_FUNCTION, "mod.new", "A")
    e2 = _entity(KIND_FUNCTION, "mod.new", "B")
    e1.extras = shared
    e2.extras = shared
    inst.write_file_extraction(_fx([e1, e2]))
    assert "_identity_key" not in shared, (
        "the writer mutated the producer's shared extras dict"
    )
    assert e2.extras is not shared and e2.extras.get("_identity_key") == "mod.new#2"
    assert e1.extras is shared, "occurrence 1 needs no override → no copy"


def test_B_RISK5_identity_key_never_becomes_a_stored_property(am, monkeypatch):
    """B-RISK-5. ``_identity_key`` is a control key; a leak would add an
    unhashed-but-stored property to every disambiguated row (and would change
    the digest on the unknown-collection hash fallback)."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "A"),
        _entity(KIND_FUNCTION, "mod.new", "B"),
    ]))
    for obj in fns.data.written:
        assert "_identity_key" not in obj["properties"]
        assert "_handler_full_name" not in obj["properties"]


def test_B_full_name_is_deliberately_left_ambiguous(am, monkeypatch):
    """§3.7, stated as a test so nobody "fixes" it by accident. Changing
    ``full_name`` is in the content hash (whole-collection re-write + re-embed),
    is the LLM-summary sidecar key (``file_path::full_name``) and is the search
    de-dup key. Scope-qualified display names are their own release."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "A"),
        _entity(KIND_FUNCTION, "mod.new", "B"),
    ]))
    assert {o["properties"]["full_name"] for o in fns.data.written} == {"mod.new"}


def test_B_walk_reports_one_accounting_line_not_one_per_occurrence(
    am, monkeypatch, capsys
):
    """B3: a single project measured ~4,000 collisions. The counter accrues per
    walk; the summary is printed once by ``analyze_repository``."""
    inst, fns, _ = _writer(am, monkeypatch)
    inst.write_file_extraction(_fx([
        _entity(KIND_FUNCTION, "mod.new", "A"),
        _entity(KIND_FUNCTION, "mod.new", "B"),
        _entity(KIND_FUNCTION, "mod.new", "C"),
    ]))
    assert inst._identity_collisions == 2
    assert inst._identity_collision_files == 1
    out = capsys.readouterr().out
    assert "shared an identity" not in out, (
        "the writer must not print per file/occurrence — the summary is once "
        "per walk"
    )


def test_B_RISK9_mid_file_failure_leaves_the_reconcile_scope_uncommitted(
    am, monkeypatch
):
    """B-RISK-9. Ordinals depend on emission order, so a PARTIAL walk must never
    authorise the per-file entity reconcile to delete. The scope is committed
    only at the very end of ``write_file_extraction``."""
    inst, fns, _ = _writer(am, monkeypatch)

    def _boom(uuid, **kw):
        raise RuntimeError("weaviate down")

    fns.data.replace = _boom
    fns.data.insert = _boom
    with pytest.raises(Exception):
        inst.write_file_extraction(_fx([
            _entity(KIND_FUNCTION, "mod.new", "A"),
            _entity(KIND_FUNCTION, "mod.new", "B"),
        ]))
    walked = getattr(inst, "_reconcile_walked", {}) or {}
    assert ("", "src/mod.rs") not in walked or not walked[("", "src/mod.rs")], (
        "a failed mid-file walk must NOT leave a committed reconcile scope"
    )


# ── the plan's Rust evidence, on the real shipped files ─────────────────────


def _extract_rust(inst, src_text: str, rel: str, root: Path):
    """Run the REAL pure rust producer. It stats the file for ``last_modified``,
    so the source must exist on disk under ``root``."""
    from vco_lib.codegraph_lang._shared import ExtractorHelpers
    from vco_lib.codegraph_lang.rust import extract_rust_file

    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src_text, encoding="utf-8")
    return extract_rust_file(src_text, target, root, ExtractorHelpers(inst))


def _embed_stubs(inst):
    inst.embed_function = lambda *a, **kw: [0.1]
    inst.embed_class = lambda *a, **kw: [0.1]
    inst.generate_embedding = lambda *a, **kw: [0.1]
    inst._shape_for_insert = lambda v: v
    return inst


_BOOT_RS = '''
pub fn register() -> Result<PathBuf, BootError> {
    linux::register()
}

pub fn unregister() -> Result<(), BootError> {
    linux::unregister()
}

pub fn status() -> Result<BootStatus, BootError> {
    linux::status()
}

mod linux {
    pub(super) fn register() -> Result<PathBuf, BootError> { systemd_unit() }
    pub(super) fn unregister() -> Result<(), BootError> { systemd_remove() }
    pub(super) fn status() -> Result<BootStatus, BootError> { systemd_status() }
}

mod macos {
    pub(super) fn register() -> Result<PathBuf, BootError> { launchd_plist() }
    pub(super) fn unregister() -> Result<(), BootError> { launchd_remove() }
    pub(super) fn status() -> Result<BootStatus, BootError> { launchd_status() }
}

mod windows {
    pub(super) fn register() -> Result<PathBuf, BootError> { schtasks_create() }
    pub(super) fn unregister() -> Result<(), BootError> { schtasks_delete() }
    pub(super) fn status() -> Result<BootStatus, BootError> { schtasks_query() }
}
'''

_SECRETS_RS = '''
pub struct KeyringStore;
impl KeyringStore {
    pub fn new() -> Self { KeyringStore }
}
impl Drop for KeyringStore {
    fn drop(&mut self) { self.close(); }
}

pub struct FileStore;
impl FileStore {
    pub fn new() -> Self { FileStore }
}
impl Drop for FileStore {
    fn drop(&mut self) { self.flush(); }
}

pub struct HubStore;
impl HubStore {
    pub fn new() -> Self { HubStore }
}
impl Drop for HubStore {
    fn drop(&mut self) { self.disconnect(); }
}
'''


@pytest.mark.parametrize(
    "src,rel,symbol,least",
    [
        (_BOOT_RS, "boot.rs", "boot.register", 4),
        (_BOOT_RS, "boot.rs", "boot.unregister", 4),
        (_BOOT_RS, "boot.rs", "boot.status", 4),
        (_SECRETS_RS, "secrets.rs", "secrets.new", 3),
        (_SECRETS_RS, "secrets.rs", "secrets.drop", 3),
    ],
    ids=["boot.register", "boot.unregister", "boot.status",
         "secrets.new", "secrets.drop"],
)
def test_B_rust_evidence_every_occurrence_is_individually_addressable(
    am, monkeypatch, tmp_path, src, rel, symbol, least
):
    """The plan's Rust evidence, reproduced hermetically.

    Rust ``full_name`` is ``{file_stem}.{fn_name}`` with no ``impl`` / ``mod``
    qualification, so ``vct-hub/src/boot.rs``'s systemd-user / launchd /
    Windows-Scheduled-Task implementations of ``register`` / ``unregister`` /
    ``status`` collapsed onto ONE row each — the code graph stored exactly one
    arbitrary OS's body and ``search_code_graph("register boot auto-start")``
    could not find the other three. That is a retrieval-correctness defect in
    shipped product code, not an efficiency nit.
    """
    inst, fns, _ = _writer(am, monkeypatch)
    _embed_stubs(inst)

    fx = _extract_rust(inst, src, rel, tmp_path)
    occurrences = [e for e in fx.entities if e.full_name == symbol]
    assert len(occurrences) >= least, (
        f"fixture regression: expected >= {least} {symbol} occurrences, "
        f"got {len(occurrences)}"
    )
    inst.write_file_extraction(fx)

    rows = [o for o in fns.data.written
            if o["properties"].get("full_name") == symbol]
    assert len(rows) == len(occurrences), (
        f"{symbol}: {len(occurrences)} occurrences produced {len(rows)} rows — "
        "some were silently overwritten"
    )
    assert len({o["uuid"] for o in rows}) == len(rows), "uuids must be distinct"
    bodies = {o["properties"]["function_body"] for o in rows}
    assert len(bodies) == len(rows), (
        f"{symbol}: distinct implementations must be individually addressable, "
        f"got {len(bodies)} distinct bodies for {len(rows)} rows"
    )


def test_B_real_boot_rs_collides_before_the_fix(am, monkeypatch, tmp_path):
    """Evidence check against the SHIPPED file (skipped if it moves). Confirms
    the collision the plan measured is real in this repo right now."""
    boot = _REPO_ROOT / "launcher/src-tauri/vct-hub/src/boot.rs"
    if not boot.exists():  # pragma: no cover — file moved
        pytest.skip("boot.rs not present")
    inst, fns, _ = _writer(am, monkeypatch)
    _embed_stubs(inst)

    fx = _extract_rust(inst, boot.read_text(encoding="utf-8"),
                       "vct-hub/src/boot.rs", tmp_path)
    keys = [e.identity_key() for e in fx.entities]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert dupes, (
        "boot.rs no longer collides — the producer may have gained scope "
        "qualification; re-check the plan's evidence before relaxing this"
    )
    suffixes = guards.assign_duplicate_identity_suffixes(
        [(e.kind, e.identity_key()) for e in fx.entities])
    final = [s if s is not None else k for s, k in zip(suffixes, keys)]
    assert len(set(final)) == len(final), (
        "the disambiguation is incomplete on a real shipped file"
    )
