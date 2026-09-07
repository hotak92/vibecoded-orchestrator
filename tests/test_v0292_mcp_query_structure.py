# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — ``query_code_structure`` cross-reference queries + hint data-safety.

Four field-reproduced defects in ``claude_mcp_servers/weaviate_mcp/server.py``,
all verified live against a populated Weaviate before the fix:

1. ``dependencies`` passed ``return_references=["imports"]`` — a ``list[str]``.
   The weaviate v4 client validates that argument and rejects it before any
   network call ("Argument 'return_references' must be one of:
   [_QueryReference], but got <class 'str'>"), so the query failed 100% of the
   time on every project, independent of the data.
2. ``extends`` had the identical defect (``return_references=["extends"]``).
   The original field report only tested ``dependencies``; a grep of both
   ``return_references`` call sites in the MCP showed BOTH were wrong.
3. ``imports`` (the reverse of ``dependencies``) filtered with
   ``Filter.by_property("imports").contains_any([target])``, but ``imports``
   is a ``ReferenceProperty`` on ``CodeModule``, not a ``TEXT_ARRAY``.
   Weaviate reads a filter path naming a reference property as a reference
   COUNT filter, so it demanded ints: "nested query: nested clause at pos 0:
   value type should be []int but is []string".
4. ``_build_schema_error_hint`` matched the substring "nested query" and
   emitted a canned hint asserting the collection lacked
   ``invertedIndexConfig.indexNullState`` — then told the user to run
   ``scripts/migrate-shared-kg-schema.sh``, which ``DELETE``s
   ``$SHARED_KG_COLLECTION``. So defect 3 (a client-side query bug on a
   per-project ``*_CodeModule``) printed a paste-ready command to destroy an
   unrelated, populated shared KG. The premise was false as well: the failing
   collections all had ``indexNullState=true``.

A fifth defect, found while fixing 1 and 2 and NOT in the field report: even
with the argument type corrected, both branches then did
``obj.references.get(name, [])`` and iterated the result. ``.references`` is
``None`` when nothing resolved (``AttributeError``), and when it does resolve
the value is a ``_CrossReference`` wrapper which is **not iterable**
(``TypeError``) — the targets live on ``.objects``. Fixing only the argument
type would have swapped one exception for another.

The fakes below deliberately reproduce the REAL client/server rejections
(wrong ``return_references`` element type; reference-property filtered as a
value property; non-iterable cross-reference), so every test here fails
against the pre-fix code for the same reason the field did.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from weaviate.collections.classes.grpc import _QueryReference  # noqa: E402

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Fakes — faithful to the client/server behaviours that produced the bugs
# ─────────────────────────────────────────────────────────────────────


class _FakeObj:
    def __init__(self, uuid: str, properties: dict[str, Any]):
        self.uuid = uuid
        self.properties = properties
        # Set by the fake query when return_references is requested.
        self.references: Any = None


class _FakeCrossReference:
    """Stand-in for weaviate's ``_CrossReference``.

    Deliberately exposes ONLY ``.objects`` and defines no ``__iter__`` —
    matching the real class, whose non-iterability is defect 5.
    """

    def __init__(self, objects: list[_FakeObj]):
        self.objects = objects


class _FakeSchema:
    """Which properties on a collection are cross-references vs values."""

    def __init__(self, reference_props: set[str]):
        self.reference_props = reference_props


def _filter_matches(obj: _FakeObj, flt: Any, links: dict[str, dict[str, list[_FakeObj]]],
                    schema: _FakeSchema) -> bool:
    """Evaluate a real weaviate ``Filter`` object against a fake row.

    Supports the two shapes these branches build, and raises the SAME errors
    the real server raises for the shapes they used to build.
    """
    sub = getattr(flt, "filters", None)
    if sub is not None:  # _FilterAnd / _FilterOr
        return all(_filter_matches(obj, f, links, schema) for f in sub)

    target = getattr(flt, "target", None)
    value = getattr(flt, "value", None)
    operator = str(getattr(flt, "operator", ""))

    link_on = getattr(target, "link_on", None)
    if link_on is not None:
        # Filter.by_ref(link_on).by_property(target.target).equal(value)
        prop = getattr(target, "target", None)
        targets = links.get(link_on, {}).get(str(obj.uuid), [])
        return any((t.properties or {}).get(prop) == value for t in targets)

    prop = str(target)
    if prop in schema.reference_props:
        # THE DEFECT-3 SERVER BEHAVIOUR: a filter path naming a reference
        # property is read as a reference-COUNT filter, which wants ints.
        raise RuntimeError(
            "Query call with protocol GRPC search failed with message "
            "explorer: list class: search: object search at index "
            "fakeproject_codemodule: nested query: nested clause at pos 0: "
            "value type should be []int but is []string."
        )

    actual = (obj.properties or {}).get(prop)
    if "CONTAINS_ANY" in operator:
        haystack = actual if isinstance(actual, (list, tuple)) else []
        return any(v in haystack for v in (value or []))
    return actual == value


class _RecordingQuery:
    def __init__(self, coll_name: str, objects: list[_FakeObj],
                 links: dict[str, dict[str, list[_FakeObj]]], schema: _FakeSchema):
        self.coll_name = coll_name
        self.objects = objects
        self.links = links
        self.schema = schema
        self.calls: list[dict] = []

    def fetch_objects(self, **kwargs):
        self.calls.append(kwargs)

        want_refs = kwargs.get("return_references")
        resolved: list[str] = []
        if want_refs is not None:
            items = want_refs if isinstance(want_refs, (list, tuple)) else [want_refs]
            for item in items:
                # THE DEFECT-1/2 CLIENT BEHAVIOUR: verbatim client-side
                # validation, which rejects a bare property name.
                if not isinstance(item, _QueryReference):
                    raise TypeError(
                        "Invalid input provided: Argument 'return_references' "
                        "must be one of: [<class 'weaviate.collections.classes."
                        "grpc._QueryReference'>], but got "
                        f"{type(item)}."
                    )
                resolved.append(item.link_on)

        flt = kwargs.get("filters")
        matched = [o for o in self.objects
                   if flt is None or _filter_matches(o, flt, self.links, self.schema)]
        matched = matched[: kwargs.get("limit", len(matched))]

        for obj in matched:
            if not resolved:
                obj.references = None
                continue
            refs: dict[str, Any] = {}
            for name in resolved:
                targets = self.links.get(name, {}).get(str(obj.uuid), [])
                if targets:
                    # Real client wraps resolved targets; NOT a list.
                    refs[name] = _FakeCrossReference(targets)
            obj.references = refs
        return mock.Mock(objects=matched)


class _FakeCollection:
    def __init__(self, name, objects, links, schema):
        self.query = _RecordingQuery(name, objects, links, schema)


class _FakeClient:
    def __init__(self, collections: dict[str, _FakeCollection]):
        self._collections = collections
        self.collections = mock.Mock()
        self.collections.get = self._get

    def _get(self, name):
        if name not in self._collections:
            raise RuntimeError(f"could not find class {name} in schema")
        return self._collections[name]


# ─────────────────────────────────────────────────────────────────────
# A small two-module / two-class graph:  a.py --imports--> b.py
# ─────────────────────────────────────────────────────────────────────

PROJECT = "FakeProject"


def _build_module_graph(*, duplicate_beacons: int = 1):
    a = _FakeObj("uuid-a", {"path": "src/a.py", "project": PROJECT})
    b = _FakeObj("uuid-b", {"path": "src/b.py", "project": PROJECT})
    links = {"imports": {"uuid-a": [b] * duplicate_beacons}}
    schema = _FakeSchema(reference_props={"imports"})
    return _FakeCollection(f"{PROJECT}_CodeModule", [a, b], links, schema), links


def _build_class_graph(*, duplicate_beacons: int = 1):
    child = _FakeObj("uuid-child", {
        "full_name": "pkg.Child", "name": "Child",
        "file_path": "src/child.py", "chunk_num": 0, "project": PROJECT,
    })
    base = _FakeObj("uuid-base", {
        "full_name": "pkg.Base", "name": "Base",
        "file_path": "src/base.py", "chunk_num": 0, "project": PROJECT,
    })
    links = {"extends": {"uuid-child": [base] * duplicate_beacons}}
    schema = _FakeSchema(reference_props={"extends", "module"})
    return _FakeCollection(f"{PROJECT}_CodeClass", [child, base], links, schema)


def _run(collections: dict[str, _FakeCollection], query_type: str, target: str) -> dict:
    client = _FakeClient(collections)
    with mock.patch.object(srv, "get_weaviate_client", return_value=client):
        raw = srv.query_code_structure(query_type, target, project=PROJECT)
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────
# Defects 1 + 2 — return_references argument SHAPE
# ─────────────────────────────────────────────────────────────────────


class TestReturnReferencesArgumentShape(unittest.TestCase):
    """``return_references`` must carry ``_QueryReference``, never ``str``."""

    def test_helper_builds_real_query_reference(self):
        built = srv._code_return_references("imports")
        self.assertIsInstance(built, list)
        self.assertEqual(len(built), 1)
        # The exact type the weaviate client's validator demands.
        self.assertIsInstance(built[0], _QueryReference)
        self.assertNotIsInstance(built[0], str)
        self.assertEqual(built[0].link_on, "imports")

    def test_dependencies_sends_query_reference_not_str(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "dependencies", "src/a.py")
        self.assertTrue(payload["success"], payload)

        sent = coll.query.calls[0]["return_references"]
        for item in sent:
            self.assertNotIsInstance(
                item, str,
                "return_references must not contain bare property names — the "
                "weaviate client rejects them before any network call",
            )
            self.assertIsInstance(item, _QueryReference)
        self.assertEqual([i.link_on for i in sent], ["imports"])

    def test_extends_sends_query_reference_not_str(self):
        """Defect 2 — MISSED by the field report, which only tested
        ``dependencies``. ``extends`` was broken identically."""
        coll = _build_class_graph()
        payload = _run({f"{PROJECT}_CodeClass": coll}, "extends", "pkg.Child")
        self.assertTrue(payload["success"], payload)

        sent = coll.query.calls[0]["return_references"]
        for item in sent:
            self.assertNotIsInstance(item, str)
            self.assertIsInstance(item, _QueryReference)
        self.assertEqual([i.link_on for i in sent], ["extends"])

    def test_extends_returns_the_base_class(self):
        coll = _build_class_graph()
        payload = _run({f"{PROJECT}_CodeClass": coll}, "extends", "pkg.Child")
        self.assertEqual(
            [r["full_name"] for r in payload["results"]], ["pkg.Base"]
        )

    def test_no_return_references_call_site_passes_strings(self):
        """Cross-site guard: neither call site may regress to ``list[str]``.

        Comment lines are excluded — the fix's own explanatory comment quotes
        the defective form on purpose.
        """
        source = (
            REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        ).read_text(encoding="utf-8")
        code_lines = [
            ln for ln in source.splitlines() if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        for bad in ('return_references=["imports"]', 'return_references=["extends"]'):
            self.assertNotIn(
                bad, code,
                f"{bad} is the exact pre-v0.2.92 defect — build the argument "
                f"with _code_return_references()",
            )
        # And the two live call sites both route through the helper.
        self.assertEqual(
            code.count('_code_return_references("imports")')
            + code.count('_code_return_references("extends")'),
            2,
        )


# ─────────────────────────────────────────────────────────────────────
# Defect 5 — reading back a _CrossReference (not a list, maybe None)
# ─────────────────────────────────────────────────────────────────────


class TestCrossReferenceRead(unittest.TestCase):
    def test_fake_cross_reference_is_not_iterable(self):
        """Pins the premise: the pre-fix ``for x in refs.get(name, [])`` loop
        could not have worked even with the argument type fixed."""
        cross = _FakeCrossReference([_FakeObj("u", {"path": "p"})])
        with self.assertRaises(TypeError):
            iter(cross)

    def test_reads_targets_off_the_wrapper(self):
        target = _FakeObj("u", {"path": "p"})
        obj = _FakeObj("o", {})
        obj.references = {"imports": _FakeCrossReference([target])}
        self.assertEqual(srv._read_cross_reference(obj, "imports"), [target])

    def test_none_references_is_empty_not_an_error(self):
        obj = _FakeObj("o", {})
        obj.references = None
        self.assertEqual(srv._read_cross_reference(obj, "imports"), [])

    def test_missing_link_name_is_empty(self):
        obj = _FakeObj("o", {})
        obj.references = {"other": _FakeCrossReference([_FakeObj("u", {})])}
        self.assertEqual(srv._read_cross_reference(obj, "imports"), [])

    def test_plain_sequence_passes_through(self):
        """Older clients / simpler fakes hand back a list; still supported."""
        target = _FakeObj("u", {"path": "p"})
        obj = _FakeObj("o", {})
        obj.references = {"imports": [target]}
        self.assertEqual(srv._read_cross_reference(obj, "imports"), [target])

    def test_dependencies_resolves_through_a_non_iterable_wrapper(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "dependencies", "src/a.py")
        self.assertEqual([r["path"] for r in payload["results"]], ["src/b.py"])

    def test_dependencies_on_a_module_with_no_imports_is_empty_not_an_error(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "dependencies", "src/b.py")
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["results"], [])


# ─────────────────────────────────────────────────────────────────────
# Defect 3 — reverse ``imports`` must walk the reference, and the two
# directions must answer DIFFERENT questions
# ─────────────────────────────────────────────────────────────────────


class TestImportsDirection(unittest.TestCase):
    def test_imports_filters_by_reference_not_by_value_property(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "imports", "src/b.py")
        self.assertTrue(payload["success"], payload)

        flt = coll.query.calls[0]["filters"]
        targets = [getattr(f, "target", None) for f in getattr(flt, "filters", [flt])]
        ref_targets = [t for t in targets if getattr(t, "link_on", None) is not None]
        self.assertTrue(
            ref_targets,
            "reverse-imports must filter with Filter.by_ref(...) — filtering "
            "the reference property by value makes Weaviate demand []int",
        )
        self.assertEqual(ref_targets[0].link_on, "imports")
        self.assertEqual(
            ref_targets[0].target, "path",
            "the reference filter must compare the TARGET module's path",
        )

    def test_imports_returns_the_importing_module(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "imports", "src/b.py")
        self.assertEqual([r["path"] for r in payload["results"]], ["src/a.py"])

    def test_the_two_directions_are_not_the_same_question(self):
        """``dependencies(a)`` is outbound, ``imports(a)`` is inbound. In a
        graph where a imports b, they must NOT return the same thing."""
        coll_out, _ = _build_module_graph()
        outbound = _run({f"{PROJECT}_CodeModule": coll_out}, "dependencies", "src/a.py")
        coll_in, _ = _build_module_graph()
        inbound = _run({f"{PROJECT}_CodeModule": coll_in}, "imports", "src/a.py")

        self.assertEqual([r["path"] for r in outbound["results"]], ["src/b.py"])
        self.assertEqual(
            [r["path"] for r in inbound["results"]], [],
            "nothing imports src/a.py — an inbound query returning src/b.py "
            "would mean both directions collapsed into one",
        )

        coll_b_in, _ = _build_module_graph()
        inbound_b = _run({f"{PROJECT}_CodeModule": coll_b_in}, "imports", "src/b.py")
        self.assertEqual([r["path"] for r in inbound_b["results"]], ["src/a.py"])

    def test_imports_still_emits_the_truncation_signal(self):
        coll, _ = _build_module_graph()
        payload = _run({f"{PROJECT}_CodeModule": coll}, "imports", "src/b.py")
        self.assertIn("truncated", payload)
        self.assertEqual(payload["limit"], 20)
        self.assertFalse(payload["truncated"])


# ─────────────────────────────────────────────────────────────────────
# Duplicate reference beacons (live data carries up to 66 per edge)
# ─────────────────────────────────────────────────────────────────────


class TestDuplicateBeaconCollapse(unittest.TestCase):
    def test_dependencies_collapses_duplicate_beacons(self):
        coll, _ = _build_module_graph(duplicate_beacons=66)
        payload = _run({f"{PROJECT}_CodeModule": coll}, "dependencies", "src/a.py")
        self.assertEqual([r["path"] for r in payload["results"]], ["src/b.py"])
        self.assertEqual(payload["count"], 1)

    def test_extends_collapses_duplicate_beacons(self):
        coll = _build_class_graph(duplicate_beacons=66)
        payload = _run({f"{PROJECT}_CodeClass": coll}, "extends", "pkg.Child")
        self.assertEqual([r["full_name"] for r in payload["results"]], ["pkg.Base"])

    def test_distinct_targets_are_kept(self):
        objs = [_FakeObj("u1", {"path": "x.py"}), _FakeObj("u2", {"path": "y.py"})]
        kept = srv._dedup_ref_targets(objs, ("path",))
        self.assertEqual([o.properties["path"] for o in kept], ["x.py", "y.py"])

    def test_rows_without_the_identity_property_are_never_merged(self):
        objs = [_FakeObj("u1", {}), _FakeObj("u2", {})]
        self.assertEqual(len(srv._dedup_ref_targets(objs, ("path",))), 2)


# ─────────────────────────────────────────────────────────────────────
# Defect 4 — schema-error hints: verify the premise, never misdirect
# ─────────────────────────────────────────────────────────────────────

# Any command a user would paste that removes data.
DESTRUCTIVE_MARKERS = (
    "migrate-shared-kg-schema",
    "drop + recreate",
    "DELETE",
    "podman rm",
    "collections.delete",
)

FAKE_SCHEMA_CLASSES = {
    "FakeProject_CodeModule": True,      # indexNullState present
    "FakeProject_CodeClass": False,      # genuinely absent
    "Fake_SharedKnowledgeGraph": True,
    "Bystander_KnowledgeGraph": True,    # must never be named
    "Bystander_Development": True,
}


class _FakeConfig:
    def __init__(self, state):
        self.inverted_index_config = mock.Mock(index_null_state=state)


class _FakeSchemaClient:
    """Client exposing exactly what the probe path uses."""

    def __init__(self, classes: dict[str, Any], *, list_raises=False, get_raises=False):
        self._classes = classes
        self._list_raises = list_raises
        self._get_raises = get_raises
        self.collections = mock.Mock()
        self.collections.list_all = self._list_all
        self.collections.get = self._get

    def _list_all(self, simple=True):
        if self._list_raises:
            raise RuntimeError("weaviate unreachable")
        return {name: mock.Mock() for name in self._classes}

    def _get(self, name):
        if self._get_raises:
            raise RuntimeError("weaviate unreachable")
        if name not in self._classes:
            raise RuntimeError("404")
        return mock.Mock(config=mock.Mock(get=lambda: _FakeConfig(self._classes[name])))


def _nested_query_error(index_name: str) -> str:
    return (
        "Query call with protocol GRPC search failed with message explorer: "
        f"list class: search: object search at index {index_name}: local shard "
        f"object search {index_name}_abc123: nested query: nested clause at "
        "pos 0: value type should be []int but is []string."
    )


def _hint(message: str, *, client=None, shared_kg="Fake_SharedKnowledgeGraph") -> str:
    client = client or _FakeSchemaClient(FAKE_SCHEMA_CLASSES)
    with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
         mock.patch.object(srv, "SHARED_KG_COLLECTION", shared_kg):
        return srv._build_schema_error_hint(Exception(message), message.lower())


class TestSchemaErrorHintDataSafety(unittest.TestCase):
    """The decision, not just the happy path: BOTH the act (emit the
    destructive remedy) and the leave-alone (stay silent) cases."""

    # ---- LEAVE-ALONE: premise refuted by the probe -------------------

    def test_no_destructive_remedy_when_index_null_state_is_present(self):
        """The exact field case: a client-side query bug whose message merely
        contains 'nested query', on a collection that HAS the null index."""
        hint = _hint(_nested_query_error("fakeproject_codemodule"))
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, f"hint offered '{marker}': {hint}")
        self.assertIn("FakeProject_CodeModule", hint)
        self.assertIn("already True", hint)

    def test_refuted_premise_hint_does_not_claim_a_schema_defect(self):
        hint = _hint(_nested_query_error("fakeproject_codemodule"))
        self.assertNotIn("lacks", hint)
        self.assertIn("NO migration is needed", hint)

    def test_refuted_premise_points_at_the_real_cause(self):
        hint = _hint(_nested_query_error("fakeproject_codemodule"))
        self.assertIn("Filter.by_ref", hint)

    # ---- LEAVE-ALONE: probe unavailable -----------------------------

    def test_probe_unavailable_emits_nothing_destructive(self):
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES, get_raises=True)
        hint = _hint(_nested_query_error("fakeproject_codeclass"), client=client)
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, f"hint offered '{marker}' unverified: {hint}")
        self.assertIn("Could not verify", hint)

    def test_weaviate_down_emits_nothing_destructive(self):
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES, list_raises=True)
        hint = _hint(_nested_query_error("fakeproject_codeclass"), client=client)
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, hint)

    def test_unparseable_message_emits_nothing_destructive(self):
        hint = _hint("nested query: something inscrutable went wrong")
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, hint)
        self.assertIn("Could not verify", hint)

    # ---- ACT: premise confirmed by the probe ------------------------

    def test_confirmed_absent_on_the_shared_kg_does_offer_the_migration(self):
        """The one case the destructive script is correct: the collection that
        failed IS the configured shared KG and its null index really is gone."""
        classes = dict(FAKE_SCHEMA_CLASSES, Fake_SharedKnowledgeGraph=False)
        client = _FakeSchemaClient(classes)
        hint = _hint(_nested_query_error("fake_sharedknowledgegraph"), client=client)
        self.assertIn("migrate-shared-kg-schema.sh", hint)
        self.assertIn("Fake_SharedKnowledgeGraph", hint)
        self.assertIn("is absent", hint)

    def test_confirmed_absent_elsewhere_offers_no_paste_ready_drop(self):
        """Absent on a per-project code class: state the condition, name only
        that class, offer NO destructive command (no repopulation guarantee)."""
        hint = _hint(_nested_query_error("fakeproject_codeclass"))
        self.assertIn("FakeProject_CodeClass", hint)
        self.assertIn("is absent", hint)
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, f"hint offered '{marker}': {hint}")
        self.assertIn("do not migrate any other collection", hint)

    # ---- Cross-cutting: never name a bystander ----------------------

    def test_no_hint_ever_names_a_collection_other_than_the_failing_one(self):
        cases = [
            ("fakeproject_codemodule", "FakeProject_CodeModule", FAKE_SCHEMA_CLASSES),
            ("fakeproject_codeclass", "FakeProject_CodeClass", FAKE_SCHEMA_CLASSES),
            ("fake_sharedknowledgegraph", "Fake_SharedKnowledgeGraph",
             dict(FAKE_SCHEMA_CLASSES, Fake_SharedKnowledgeGraph=False)),
        ]
        for index_name, failing, classes in cases:
            with self.subTest(failing=failing):
                hint = _hint(_nested_query_error(index_name),
                             client=_FakeSchemaClient(classes))
                for other in classes:
                    if other == failing:
                        continue
                    self.assertNotIn(
                        other, hint,
                        f"hint for {failing} named unrelated collection {other}",
                    )

    def test_code_graph_failure_never_redirects_to_the_shared_kg(self):
        """The v0.2.92 BLOCKER in one assertion."""
        hint = _hint(_nested_query_error("fakeproject_codemodule"))
        self.assertNotIn("Fake_SharedKnowledgeGraph", hint)
        self.assertNotIn("migrate-shared-kg-schema", hint)

    # ---- The other branches --------------------------------------

    def test_missing_property_on_a_code_class_does_not_recommend_temporal_script(self):
        """migrate-development-temporal-props.sh only walks *_KnowledgeGraph /
        *_Development / *_Diagrams — recommending it for a *_Code* class is a
        wrong-target instruction that would silently do nothing."""
        msg = ("no such prop with name 'valid_from' found in class "
               "'FakeProject_CodeModule'")
        hint = _hint(msg)
        # The script may be NAMED (to say it does not apply) but never
        # RECOMMENDED for a collection it does not walk.
        self.assertNotIn("Run scripts/migrate-development-temporal-props", hint)
        self.assertIn("does NOT cover it", hint)
        self.assertIn("FakeProject_CodeModule", hint)

    def test_missing_property_on_a_development_class_does_recommend_it(self):
        msg = ("no such prop with name 'valid_from' found in class "
               "'Bystander_Development'")
        hint = _hint(msg)
        self.assertIn("migrate-development-temporal-props.sh", hint)
        self.assertIn("adds properties and deletes nothing", hint)

    def test_class_not_found_hint_stays_additive(self):
        hint = _hint("could not find class FakeProject_Missing in schema")
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(marker, hint, hint)
        self.assertIn("FakeProject_Missing", hint)


class TestFailingCollectionIdentification(unittest.TestCase):
    def test_extracts_lowercased_index_name_from_grpc_error(self):
        token = srv._extract_failing_collection(
            _nested_query_error("vibecodedorchestrator_codemodule")
        )
        self.assertEqual(token, "vibecodedorchestrator_codemodule")

    def test_extracts_class_name_from_missing_property_error(self):
        token = srv._extract_failing_collection(
            "no such prop with name 'valid_from' found in class 'X_Development'"
        )
        self.assertEqual(token, "X_Development")

    def test_extracts_class_name_from_class_not_found_error(self):
        token = srv._extract_failing_collection("could not find class X_KnowledgeGraph in schema")
        self.assertEqual(token, "X_KnowledgeGraph")

    def test_returns_none_rather_than_guessing(self):
        self.assertIsNone(srv._extract_failing_collection("something else entirely"))
        self.assertIsNone(srv._extract_failing_collection(""))

    def test_resolves_lowercased_index_to_the_exact_class_name(self):
        """weaviate only capitalises the first character, so
        ``collections.get('vibecodedorchestrator_codemodule')`` 404s — the
        mapping has to be a case-insensitive schema lookup."""
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client):
            self.assertEqual(
                srv._resolve_schema_class_name("fakeproject_codemodule"),
                "FakeProject_CodeModule",
            )

    def test_resolution_returns_none_when_weaviate_is_down(self):
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES, list_raises=True)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client):
            self.assertIsNone(srv._resolve_schema_class_name("fakeproject_codemodule"))

    def test_probe_reports_present_absent_and_unknown(self):
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client):
            self.assertIs(srv._probe_index_null_state("FakeProject_CodeModule"), True)
            self.assertIs(srv._probe_index_null_state("FakeProject_CodeClass"), False)
            self.assertIsNone(srv._probe_index_null_state("Not_In_Schema"))
            self.assertIsNone(srv._probe_index_null_state(""))


if __name__ == "__main__":
    unittest.main()
