# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 (P7): revision-gated forced code-graph resync.

Covers:
  * FINGERPRINT GATE (analyze_code_graph._write_one_object):
      - stored embed_revision behind current → FORCED re-embed even when the
        content_hash matches (the ~7-9% over-budget-entity resync case).
      - stored embed_revision == current AND content_hash matches → SKIP
        (no write, no tombstone) — makes the resync idempotent.
      - NULL stored embed_revision (pre-migration row) → FORCED re-embed.
      - absent object → normal write (fail-safe).
  * every write stamps embed_revision = CODEGRAPH_EMBED_REVISION.
  * the resync trigger (vco_lib.codegraph_resync):
      - launches a background analyze when the code-embed service is up.
      - DEGRADES to a deferral (no spawn) when the service is down.
      - is host-agnostic (root vs non-root) — the gate is revision-based.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_ANALYZER_PATH = _THIS_DIR.parent / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0272_p7_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


# ─────────────────────────── fakes ───────────────────────────


class _FakeData:
    def __init__(self):
        self.replaced = []
        self.inserted = []

    def replace(self, uuid, **kwargs):
        self.replaced.append({"uuid": uuid, **kwargs})

    def insert(self, uuid, **kwargs):
        self.inserted.append({"uuid": uuid, **kwargs})


class _Existing:
    def __init__(self, properties):
        self.properties = properties


class _FakeQuery:
    """Returns a preconfigured stored-object property snapshot by UUID.

    `stored` is a dict keyed by uuid → {content_hash, embed_revision}; a UUID
    not present returns None (object absent).
    """

    def __init__(self, stored):
        self.stored = stored

    def fetch_object_by_id(self, uuid, return_properties=None):
        props = self.stored.get(uuid)
        if props is None:
            return None
        # Only surface the requested props (mirror Weaviate's projection).
        if return_properties:
            props = {k: props.get(k) for k in return_properties}
        return _Existing(props)


class _FakeColl:
    def __init__(self, name, stored=None):
        self.name = name
        self.data = _FakeData()
        self.query = _FakeQuery(stored or {})


class _StubAnalyzer:
    """Carries the attrs `_write_one_object` reads + the real method."""

    def __init__(self, analyzer_mod):
        self.project_name = "P"
        self._track_visited = True
        self._current_language = "python"
        self._current_source = ""
        self.visited_uuids = set()
        cls = analyzer_mod.CodeGraphAnalyzer
        self._write_one_object = cls._write_one_object.__get__(self, _StubAnalyzer)


def _small_func_props():
    return {
        "name": "small", "full_name": "mod.small",
        "function_body": "def small():\n    return 1\n", "signature": "def small()",
        "type_uses": [], 
        "language": "python",
    }


def _compute_hash(analyzer_mod, coll_name, props):
    return analyzer_mod._content_hash_for_object(coll_name, props)


# ─────────────── stamping: every write carries the revision ───────────────


def test_write_stamps_current_embed_revision(analyzer_mod):
    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")  # empty store → object absent → write
    params = {"properties": _small_func_props(), "vector": [0.1, 0.2]}
    stub._write_one_object(coll, "uuid-A", params, "mod.small")
    # Absent object → replace() is attempted (succeeds on _FakeData.replace).
    written = coll.data.replaced or coll.data.inserted
    assert len(written) == 1
    props = written[0]["properties"]
    assert props["embed_revision"] == analyzer_mod.CODEGRAPH_EMBED_REVISION


# ─────────────── fingerprint gate: revision mismatch forces re-embed ──────


def test_revision_mismatch_forces_reembed(analyzer_mod):
    """Stored embed_revision behind current → re-embed even if hash matches."""
    stub = _StubAnalyzer(analyzer_mod)
    coll_name = "P_CodeFunction"
    props = _small_func_props()
    content_hash = _compute_hash(analyzer_mod, coll_name, props)
    # Stored row: SAME content hash but an OLDER revision (0 < current).
    stale_rev = analyzer_mod.CODEGRAPH_EMBED_REVISION - 1
    coll = _FakeColl(coll_name, stored={
        "uuid-A": {"content_hash": content_hash, "embed_revision": stale_rev},
    })
    params = {"properties": dict(props), "vector": [0.1, 0.2]}
    stub._write_one_object(coll, "uuid-A", params, "mod.small")
    # FORCED write despite the matching content hash.
    assert len(coll.data.replaced) == 1, "revision mismatch must force a re-embed"


def test_null_revision_forces_reembed(analyzer_mod):
    """Pre-migration row (NULL embed_revision) → forced re-embed (fail-safe)."""
    stub = _StubAnalyzer(analyzer_mod)
    coll_name = "P_CodeFunction"
    props = _small_func_props()
    content_hash = _compute_hash(analyzer_mod, coll_name, props)
    coll = _FakeColl(coll_name, stored={
        "uuid-A": {"content_hash": content_hash, "embed_revision": None},
    })
    params = {"properties": dict(props), "vector": [0.1, 0.2]}
    stub._write_one_object(coll, "uuid-A", params, "mod.small")
    assert len(coll.data.replaced) == 1, "NULL revision must force a re-embed"


# ─────────────── fingerprint gate: revision + hash match → skip ───────────


def test_revision_and_hash_match_skips(analyzer_mod):
    """content_hash matches AND embed_revision current → SKIP (no write)."""
    stub = _StubAnalyzer(analyzer_mod)
    coll_name = "P_CodeFunction"
    props = _small_func_props()
    content_hash = _compute_hash(analyzer_mod, coll_name, props)
    coll = _FakeColl(coll_name, stored={
        "uuid-A": {
            "content_hash": content_hash,
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
        },
    })
    params = {"properties": dict(props), "vector": [0.1, 0.2]}
    ret = stub._write_one_object(coll, "uuid-A", params, "mod.small")
    assert not coll.data.replaced and not coll.data.inserted, "current row must skip"
    assert ret == "uuid-A"
    # Skipped rows are still tracked as visited (prune-safe).
    assert ("P_CodeFunction", "uuid-A") in stub.visited_uuids


def test_idempotent_rerun_after_resync_is_noop(analyzer_mod):
    """After the resync stamps the current revision, a re-run writes nothing.

    Simulates the post-resync steady state: run 1 writes (absent object), then
    run 2 sees the stored row at the current revision + matching hash → skip.
    """
    stub = _StubAnalyzer(analyzer_mod)
    coll_name = "P_CodeFunction"
    props = _small_func_props()
    content_hash = _compute_hash(analyzer_mod, coll_name, props)

    # Run 1: object absent → forced write (the resync).
    coll1 = _FakeColl(coll_name)  # empty store
    stub._write_one_object(coll1, "uuid-A", {"properties": dict(props), "vector": [0.1]}, "mod.small")
    assert coll1.data.replaced or coll1.data.inserted

    # Run 2: the row now exists at the current revision + same hash → skip.
    coll2 = _FakeColl(coll_name, stored={
        "uuid-A": {
            "content_hash": content_hash,
            "embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION,
        },
    })
    stub2 = _StubAnalyzer(analyzer_mod)
    stub2._write_one_object(coll2, "uuid-A", {"properties": dict(props), "vector": [0.1]}, "mod.small")
    assert not coll2.data.replaced and not coll2.data.inserted, "re-run must be a no-op"


def test_absent_object_writes_normally(analyzer_mod):
    """No stored row at all → normal write (never skip on absence)."""
    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")  # empty store
    props = _small_func_props()
    stub._write_one_object(coll, "uuid-Z", {"properties": dict(props), "vector": [0.1]}, "mod.small")
    assert coll.data.replaced or coll.data.inserted


# ─────────────── revision is host-agnostic (root == non-root) ─────────────


def test_gate_is_revision_based_not_host_based(analyzer_mod):
    """The gate keys on embed_revision only — same behaviour for any project
    name (root 'VibeCodedOrchestrator' or a user project). Two collections with
    different prefixes but stale rows both force re-embed."""
    props = _small_func_props()
    stale_rev = analyzer_mod.CODEGRAPH_EMBED_REVISION - 1
    for prefix in ("VibeCodedOrchestrator", "SomeUserProject"):
        coll_name = f"{prefix}_CodeFunction"
        content_hash = _compute_hash(analyzer_mod, coll_name, props)
        coll = _FakeColl(coll_name, stored={
            "u": {"content_hash": content_hash, "embed_revision": stale_rev},
        })
        stub = _StubAnalyzer(analyzer_mod)
        stub._write_one_object(coll, "u", {"properties": dict(props), "vector": [0.1]}, "mod.small")
        assert len(coll.data.replaced) == 1, f"{prefix}: stale row must re-embed"


# ─────────────────── schema migration is additive ───────────────────────


def test_embed_revision_migration_method_exists_and_additive(analyzer_mod):
    """`_ensure_embed_revision_property` adds the prop to the 5 code collections
    without dropping/recreating (additive add_property, idempotent)."""
    added = []

    class _Coll:
        def __init__(self, has_prop):
            # config.get().properties must reflect has_prop
            self.config = types.SimpleNamespace(
                get=lambda: types.SimpleNamespace(
                    properties=(
                        [types.SimpleNamespace(name="embed_revision")] if has_prop else []
                    )
                ),
                add_property=lambda prop: added.append(prop.name),
            )

    stub = types.SimpleNamespace(
        modules_collection=_Coll(False),
        classes_collection=_Coll(False),
        functions_collection=_Coll(False),
        apis_collection=_Coll(False),
        interactions_collection=_Coll(False),
    )
    # Bind the real method.
    meth = analyzer_mod.CodeGraphAnalyzer._ensure_embed_revision_property
    meth(stub)
    # 5 collections, all lacking the prop → 5 additive adds.
    assert added.count("embed_revision") == 5

    # Idempotent: a collection that ALREADY has it is skipped.
    added.clear()
    stub_has = types.SimpleNamespace(
        modules_collection=_Coll(True),
        classes_collection=_Coll(True),
        functions_collection=_Coll(True),
        apis_collection=_Coll(True),
        interactions_collection=_Coll(True),
    )
    meth(stub_has)
    assert added == [], "already-present prop must be skipped (idempotent)"


# ─────────────────── resync trigger: launch vs degrade ───────────────────


def _load_resync():
    sys.path.insert(0, str(_THIS_DIR.parent))
    from vco_lib import codegraph_resync  # noqa: E402
    return codegraph_resync


def test_resync_degrades_to_deferral_when_service_down(monkeypatch, tmp_path):
    mod = _load_resync()
    # Force the health probe to report DOWN.
    monkeypatch.setattr(mod, "code_embed_service_healthy", lambda *a, **k: False)
    # Provide an analyzer script so the skip-precondition is satisfied.
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")

    result = mod.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3", check_owed=False,
    )
    assert result.status == "deferred", "service down → defer, never spawn"
    assert result.pid is None
    # A DeferralEntry should be attached when the type is importable.
    if result.deferral is not None:
        assert result.deferral.condition_id == "codegraph_embed_resync_pending"


def test_resync_launches_background_when_service_up(monkeypatch, tmp_path):
    mod = _load_resync()
    monkeypatch.setattr(mod, "code_embed_service_healthy", lambda *a, **k: True)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")

    spawned = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
    result = mod.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3", check_owed=False,
    )
    assert result.status == "launched"
    assert result.pid == 4321
    # NO --force-recreate (that would DROP the schema); NO global timeout kwarg.
    assert "--force-recreate" not in spawned["argv"]
    assert "timeout" not in spawned["kwargs"], "must not impose a process timeout"
    assert "--project" in spawned["argv"] and "MyProj" in spawned["argv"]


def test_resync_skips_when_analyzer_missing(monkeypatch, tmp_path):
    mod = _load_resync()
    monkeypatch.setattr(mod, "code_embed_service_healthy", lambda *a, **k: True)
    # No analyzer script anywhere under tmp_path.
    result = mod.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3", check_owed=False,
    )
    assert result.status == "skipped"


def test_resync_spawn_failure_degrades_to_deferral(monkeypatch, tmp_path):
    mod = _load_resync()
    monkeypatch.setattr(mod, "code_embed_service_healthy", lambda *a, **k: True)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")

    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(mod.subprocess, "Popen", _boom)
    result = mod.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3", check_owed=False,
    )
    assert result.status == "deferred", "spawn failure must degrade, not crash"


# ─────────────── M0 (F-GAP): the per-FILE gate is revision-aware ───────────────
#
# v0.2.72 M0: without the revision check INSIDE `_get_existing_module`, the
# P7 resync was INERT for unchanged files — the path+file_hash match at the
# top of every `_analyze_*_file` walker short-circuited the whole file BEFORE
# the per-object `embed_revision` gate (tested above) could ever run. These
# tests drive the gate itself (THROUGH the file-skip layer, not below it).


class _M0ModulesColl:
    """modules_collection stub: fetch_objects returns the canned objects."""

    def __init__(self, objects):
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(objects=objects)
        )


def _m0_gate(analyzer_mod, objects):
    stub = types.SimpleNamespace(modules_collection=_M0ModulesColl(objects))
    return analyzer_mod.CodeGraphAnalyzer._get_existing_module(
        stub, "pkg/mod.py", "deadbeef"
    )


def _m0_obj(revision):
    return types.SimpleNamespace(uuid="uuid-1", properties={"embed_revision": revision})


def test_m0_file_gate_current_revision_skips(analyzer_mod):
    """path+hash match AND embed_revision == current → UUID (file skipped)."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    assert _m0_gate(analyzer_mod, [_m0_obj(rev)]) == "uuid-1"


def test_m0_file_gate_null_revision_not_skipped(analyzer_mod):
    """Pre-migration row (embed_revision NULL) → None → file re-walked.
    THE F-GAP regression guard: pre-fix this returned the UUID and the
    resync never reached the per-object gate for unchanged files."""
    assert _m0_gate(analyzer_mod, [_m0_obj(None)]) is None


def test_m0_file_gate_absent_property_not_skipped(analyzer_mod):
    """Row without the property at all (pre-additive-migration) → None."""
    obj = types.SimpleNamespace(uuid="uuid-1", properties={})
    assert _m0_gate(analyzer_mod, [obj]) is None


def test_m0_file_gate_older_revision_not_skipped(analyzer_mod):
    """Stored revision behind current → None → file re-walked."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    assert _m0_gate(analyzer_mod, [_m0_obj(rev - 1)]) is None


def test_m0_file_gate_unparseable_revision_not_skipped(analyzer_mod):
    """Garbage revision value → conservative None (re-walk, never skip)."""
    assert _m0_gate(analyzer_mod, [_m0_obj("not-a-number")]) is None


def test_m0_file_gate_no_row_not_skipped(analyzer_mod):
    """No matching path+hash row (pre-existing behavior) → None."""
    assert _m0_gate(analyzer_mod, []) is None


def test_m0_file_gate_fetch_error_not_skipped(analyzer_mod):
    """A read failure must never cause a skip (fail-safe direction)."""
    class _Boom:
        @property
        def query(self):
            raise RuntimeError("weaviate down")

    stub = types.SimpleNamespace(modules_collection=_Boom())
    assert (
        analyzer_mod.CodeGraphAnalyzer._get_existing_module(stub, "p", "h") is None
    )


def test_m0_float_revision_from_weaviate_still_skips(analyzer_mod):
    """Weaviate INT props can round-trip as float (1.0) — int() coercion in
    the gate must still recognize the current revision."""
    rev = float(analyzer_mod.CODEGRAPH_EMBED_REVISION)
    assert _m0_gate(analyzer_mod, [_m0_obj(rev)]) == "uuid-1"


# ─────────────── R-2 (C-1): vectorless writes must stay visibly stale ─────────
#
# `generate_embedding` returns None on any backend failure; call sites write
# anyway (only the `vector` key is guarded). Pre-fix, `_write_one_object`
# stamped CURRENT revision + content hash on those VECTORLESS rows → both
# gates passed forever → the row was invisible to near_vector search while
# counting as converged. These tests drive the real `_write_one_object`.


def test_r2_vectorless_write_stamps_revision_zero(analyzer_mod):
    """No vector in insert_params → embed_revision stamped 0, NOT current."""
    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")  # absent object → write
    params = {"properties": _small_func_props()}  # NO "vector" key
    stub._write_one_object(coll, "uuid-V", params, "mod.small")
    written = coll.data.replaced or coll.data.inserted
    assert len(written) == 1
    props = written[0]["properties"]
    assert props["embed_revision"] == 0, (
        "a vectorless write must NOT claim the current embed revision"
    )
    # content_hash is still stamped truthfully (the CONTENT is real; only
    # the vector is missing) — the revision mismatch alone forces the retry.
    assert props.get("content_hash")


def test_r2_empty_vector_counts_as_vectorless(analyzer_mod):
    """`vector: []` / `vector: None` (shaped-empty embeds) also stamp 0."""
    for empty in ([], None):
        stub = _StubAnalyzer(analyzer_mod)
        coll = _FakeColl("P_CodeFunction")
        params = {"properties": _small_func_props(), "vector": empty}
        stub._write_one_object(coll, "uuid-V", params, "mod.small")
        written = coll.data.replaced or coll.data.inserted
        assert written[0]["properties"]["embed_revision"] == 0


def test_r2_vectorless_row_retried_on_next_visit(analyzer_mod):
    """THROUGH THE FINGERPRINT GATE: a rev-0 row with a MATCHING content hash
    must NOT skip — the next visit (embed succeeded this time) re-writes it
    at the current revision. This is the C-1 permanently-unsearchable-row
    regression guard."""
    stub = _StubAnalyzer(analyzer_mod)
    coll_name = "P_CodeFunction"
    props = _small_func_props()
    content_hash = _compute_hash(analyzer_mod, coll_name, props)
    coll = _FakeColl(coll_name, stored={
        "uuid-V": {"content_hash": content_hash, "embed_revision": 0},
    })
    params = {"properties": dict(props), "vector": [0.1, 0.2]}
    stub._write_one_object(coll, "uuid-V", params, "mod.small")
    assert len(coll.data.replaced) == 1, "rev-0 row must be re-written"
    assert coll.data.replaced[0]["properties"]["embed_revision"] == (
        analyzer_mod.CODEGRAPH_EMBED_REVISION
    )


def test_r2_vector_present_still_stamps_current(analyzer_mod):
    """Regression: the vector-bearing path keeps stamping CURRENT."""
    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    params = {"properties": _small_func_props(), "vector": [0.5]}
    stub._write_one_object(coll, "u", params, "mod.small")
    written = coll.data.replaced or coll.data.inserted
    assert written[0]["properties"]["embed_revision"] == (
        analyzer_mod.CODEGRAPH_EMBED_REVISION
    )


# ─────────────── R-1: the per-FILE gate consults the stale-file set ───────────
#
# The v0.2.72 M0 fix made the file gate revision-aware for the MODULE row
# only. RT-5 FINAL live-reproduced the residual gap: thousands of stale
# FUNCTION/CLASS rows lived in UNCHANGED files whose module row was already
# current → a full re-walk skipped every one of them and could never
# converge. R-1 probes the three file-anchored collections up-front and
# forces a re-walk of any file owning a stale row. Tests drive the REAL
# `_get_existing_module` + `_build_stale_file_set` (through the gate — the
# M0 lesson: never test only below the file gate).


class _ProbeAgg:
    def __init__(self, count, raises=False):
        self._count = count
        self._raises = raises

    def over_all(self, **kwargs):
        if self._raises:
            raise RuntimeError("aggregate unavailable")
        return types.SimpleNamespace(total_count=self._count)


class _ProbeColl:
    """Collection fake for the stale-file probe: aggregate + iterator +
    the module-row fetch used by the M0 conjunct."""

    def __init__(self, name, rows=None, agg_count=None, agg_raises=False,
                 iter_raises=False, module_objects=None):
        self.name = name
        self._rows = rows or []
        self.aggregate = _ProbeAgg(agg_count, raises=agg_raises)
        self._iter_raises = iter_raises
        self.iter_calls = 0
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(
                objects=module_objects or []
            )
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        if self._iter_raises:
            raise RuntimeError("scan failed")
        for props in self._rows:
            yield types.SimpleNamespace(uuid="u", properties=props)


class _R1Stub:
    """Binds the REAL gate + probe methods onto a minimal instance."""

    def __init__(self, analyzer_mod, modules, classes, functions):
        self.modules_collection = modules
        self.classes_collection = classes
        self.functions_collection = functions
        cls = analyzer_mod.CodeGraphAnalyzer
        for name in (
            "_get_existing_module", "_get_stale_file_set",
            "_build_stale_file_set", "_count_stale_rows_in_collection",
        ):
            setattr(self, name, getattr(cls, name).__get__(self, _R1Stub))


def _current_module_row(analyzer_mod):
    """A module row at the CURRENT revision (M0 conjunct passes)."""
    return [types.SimpleNamespace(
        uuid="uuid-mod",
        properties={"embed_revision": analyzer_mod.CODEGRAPH_EMBED_REVISION},
    )]


def test_r1_stale_function_row_forces_rewalk_of_unchanged_file(analyzer_mod):
    """THE RT-5 REGRESSION GUARD: module row current + hash matching, but a
    FUNCTION row of the same file is at a stale revision → the file gate
    must return None (re-walk) instead of skipping."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    functions = _ProbeColl(
        "P_CodeFunction", agg_count=1,
        rows=[{"file_path": "pkg/mod.py", "embed_revision": rev - 1}],
    )
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") is None, (
        "a stale entity row must pull its unchanged file back through the gate"
    )


def test_r1_null_revision_entity_row_forces_rewalk(analyzer_mod):
    """Pre-migration (NULL revision) entity rows count as stale too —
    Weaviate comparisons ignore NULLs, so the client-side classification
    must catch them."""
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl(
        "P_CodeClass", agg_count=None,  # count unknown → scan
        rows=[{"file_path": "pkg/mod.py", "embed_revision": None}],
    )
    functions = _ProbeColl("P_CodeFunction", agg_count=0)
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") is None


def test_r1_converged_collections_skip_file_without_scanning(analyzer_mod):
    """Steady state: all three aggregates report 0 stale → NO iteration runs
    (the per-edit hook's cost bound) and the current-revision file skips."""
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    functions = _ProbeColl("P_CodeFunction", agg_count=0)
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") == "uuid-mod"
    assert modules.iter_calls == 0
    assert classes.iter_calls == 0
    assert functions.iter_calls == 0


def test_r1_other_files_stale_rows_do_not_block_this_file(analyzer_mod):
    """A stale row in a DIFFERENT file must not force this file to re-walk."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    functions = _ProbeColl(
        "P_CodeFunction", agg_count=1,
        rows=[{"file_path": "other/file.py", "embed_revision": rev - 1}],
    )
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") == "uuid-mod"


def test_r1_probe_failure_falls_back_to_m0_behavior(analyzer_mod):
    """Every probe fails (aggregate AND scan) → set is None → the gate keeps
    the M0 module-row-only behaviour (fail-open, never a crash)."""
    modules = _ProbeColl(
        "P_CodeModule", agg_raises=True, iter_raises=True,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_raises=True, iter_raises=True)
    functions = _ProbeColl("P_CodeFunction", agg_raises=True, iter_raises=True)
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") == "uuid-mod"


def test_r1_per_collection_failure_does_not_wedge_others(analyzer_mod):
    """One collection's scan failing must not hide another's stale rows."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    modules = _ProbeColl(
        "P_CodeModule", agg_raises=True, iter_raises=True,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    functions = _ProbeColl(
        "P_CodeFunction", agg_count=2,
        rows=[{"file_path": "pkg/mod.py", "embed_revision": rev - 1}],
    )
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") is None


def test_r1_stale_set_cached_per_run(analyzer_mod):
    """The probe runs ONCE per analyzer instance (per-run cache)."""
    functions = _ProbeColl(
        "P_CodeFunction", agg_count=1,
        rows=[{"file_path": "pkg/mod.py", "embed_revision": 0}],
    )
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    stub._get_existing_module("pkg/mod.py", "deadbeef")
    stub._get_existing_module("pkg/mod.py", "deadbeef")
    stub._get_existing_module("other/one.py", "cafebabe")
    assert functions.iter_calls == 1, "stale-file scan must be cached per run"


def test_r1_vectorless_rev0_row_is_in_stale_set(analyzer_mod):
    """R-1 x R-2 interlock: a row stamped 0 by a vectorless write pulls its
    file back through the gate on the next walk — the convergence loop the
    two fixes form together (this is why R-1 and R-2 land atomically)."""
    modules = _ProbeColl(
        "P_CodeModule", agg_count=0,
        module_objects=_current_module_row(analyzer_mod),
    )
    classes = _ProbeColl("P_CodeClass", agg_count=0)
    functions = _ProbeColl(
        "P_CodeFunction", agg_count=1,
        rows=[{"file_path": "pkg/mod.py", "embed_revision": 0}],
    )
    stub = _R1Stub(analyzer_mod, modules, classes, functions)
    assert stub._get_existing_module("pkg/mod.py", "deadbeef") is None
