# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — the C# no-template route default stops fabricating a segment.

WHY THIS EXISTS
---------------
``[HttpPost]`` with no template, under ``[Route("api/items")]``, is served by
ASP.NET at ``POST /api/items``. The producer stored ``/api/items/add`` — a
path built from the METHOD NAME, which does not exist on the running service.
A user searching the code graph for the real endpoint found nothing, and a
user who found the stored one was given a route that 404s.

WHY IT WAS LEFT IN PLACE UNTIL NOW, and what had to be true to remove it: the
fabricated segment was the only thing keeping two no-template actions that
share a verb apart. Both resolve to ONE ``"<endpoint>:<method>"`` dedup
identity, so pre-v0.2.92 the second silently OVERWROTE the first and an
endpoint was lost — the same class of loss the ``[Http*]`` lookback bug caused.

The occurrence disambiguation that landed earlier this cycle covers it, and
this file VERIFIES that rather than assuming it:
``assign_duplicate_identity_suffixes`` groups on ``(kind, identity_key)`` for
EVERY entity kind — ``KIND_API`` included — so the second row is keyed
``/api/items:POST#2`` and both are written. The test below drives the REAL
writer over a two-action controller and asserts two distinct stored UUIDs, so
the guarantee is checked where it actually has to hold.

WHAT IS PINNED
--------------
* the ACT: no template + a controller ``[Route]`` -> the controller route
  verbatim; no template + no controller ``[Route]`` -> ``"/"``, which is what
  the ONE shared ``join_route`` already documents for an empty path;
* the collision the removal exposes is genuinely covered, end to end;
* the LEAVE-ALONE: every templated shape is byte-identical to before, and an
  undecorated method still yields no row.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from vco_lib import codegraph_guards as _guards
from vco_lib.codegraph_entities import KIND_API
from vco_lib.codegraph_lang._shared import build_api_entity
from vco_lib.codegraph_lang.csharp import extract_csharp_file

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


class _Helpers:
    project_name = "CsRoute"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _extract(tmp_path: Path, source: str, name: str = "C.cs"):
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return extract_csharp_file(source, target, tmp_path, _Helpers())


def _routes(fx) -> List[Tuple[str, str]]:
    return [
        (e.extras["method"], e.extras["endpoint"])
        for e in fx.entities
        if e.kind == KIND_API
    ]


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT
# ═══════════════════════════════════════════════════════════════════════════
def test_no_template_under_a_controller_route_serves_the_controller_route(tmp_path) -> None:
    """Pre-fix: ``/api/items/add``. ASP.NET serves ``POST /api/items``."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/items")]
    public class ItemsController
    {
        [HttpPost]
        public Item Add(Item item)
        {
            return item;
        }
    }
}
''')
    assert _routes(fx) == [("POST", "/api/items")]


def test_no_template_and_no_controller_route_is_the_root(tmp_path) -> None:
    """Pre-fix: ``/ping``. ASP.NET combines an empty controller template with
    an empty action template to the application root — the same rule
    ``join_route`` already documents for ``APIRouter(prefix="")`` +
    ``@router.get("")``."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class PingController
    {
        [HttpPost]
        public string Ping()
        {
            return "ok";
        }
    }
}
''')
    assert _routes(fx) == [("POST", "/")]


def test_the_method_name_never_appears_in_a_fabricated_segment(tmp_path) -> None:
    """The defect stated as a property, over several verbs at once."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/orders")]
    public class OrdersController
    {
        [HttpGet]
        public string Fetch() { return "a"; }
        [HttpDelete]
        public string Purge() { return "b"; }
    }
}
''')
    endpoints = [ep for _m, ep in _routes(fx)]
    assert endpoints == ["/api/orders", "/api/orders"]
    for bad in ("fetch", "purge"):
        assert not any(bad in ep.lower() for ep in endpoints), endpoints


# ═══════════════════════════════════════════════════════════════════════════
# THE COLLISION THE REMOVAL EXPOSES — verified, not assumed
# ═══════════════════════════════════════════════════════════════════════════
_TWO_BARE_POSTS = '''namespace Shop
{
    [Route("api/items")]
    public class ItemsController
    {
        [HttpPost]
        public Item Add(Item item)
        {
            return item;
        }

        [HttpPost]
        public Item Insert(Item item)
        {
            return item;
        }
    }
}
'''


def test_two_bare_posts_share_an_identity_key_before_disambiguation(tmp_path) -> None:
    """The hazard itself, made explicit: this is what the fabricated segment
    was hiding."""
    fx = _extract(tmp_path, _TWO_BARE_POSTS)
    apis = [e for e in fx.entities if e.kind == KIND_API]
    assert len(apis) == 2
    assert apis[0].identity_key() == apis[1].identity_key() == "/api/items:POST"


def test_the_occurrence_disambiguation_covers_the_api_kind(tmp_path) -> None:
    """``assign_duplicate_identity_suffixes`` groups on ``(kind, key)`` for
    every kind, so ``KIND_API`` is covered without a special case."""
    ents = [
        build_api_entity(
            file_path_rel="src/C.cs", endpoint="/api/items", method="POST",
            description=f"d{i}", project="P", handler_full_name=f"N.C.M{i}",
            embed=lambda d: None,
        )
        for i in range(2)
    ]
    suffixes = _guards.assign_duplicate_identity_suffixes(
        [(e.kind, e.identity_key()) for e in ents]
    )
    assert suffixes == [None, "/api/items:POST#2"]


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_wp5_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover - dependency regression
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


class _FakeData:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store

    def replace(self, uuid: str, **kw: Any) -> None:
        self._store[str(uuid)] = kw

    def insert(self, uuid: str, **kw: Any) -> str:
        self._store[str(uuid)] = kw
        return str(uuid)

    def update(self, uuid: str, **kw: Any) -> None:
        self._store.setdefault(str(uuid), {}).setdefault("properties", {}).update(
            kw.get("properties", {})
        )

    def delete_by_id(self, uuid: str) -> None:  # pragma: no cover
        self._store.pop(str(uuid), None)


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.store: Dict[str, Dict[str, Any]] = {}
        self.data = _FakeData(self.store)


def test_both_bare_post_rows_survive_the_real_writer(tmp_path, analyzer_mod) -> None:
    """END TO END. Two no-template POSTs under one controller are written as
    TWO rows with distinct UUIDs — the loss the fabricated segment prevented
    does not return when the fabrication is removed."""
    analyzer_mod.generate_embedding = lambda text: None
    analyzer_mod.embed_module = lambda summary: None
    analyzer_mod.embed_function = lambda sig, body, language="python": None
    analyzer_mod.embed_class = lambda sig, body, methods=None, language="python": None

    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "CsRoute"
    inst.client = object()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._current_source = ""
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    inst.modules_collection = _FakeCollection("CsRoute_CodeModule")
    inst.classes_collection = _FakeCollection("CsRoute_CodeClass")
    inst.functions_collection = _FakeCollection("CsRoute_CodeFunction")
    inst.apis_collection = _FakeCollection("CsRoute_CodeAPI")
    inst.interactions_collection = _FakeCollection("CsRoute_CodeInteraction")

    src = tmp_path / "src"
    src.mkdir()
    (src / "Items.cs").write_text(_TWO_BARE_POSTS, encoding="utf-8")
    inst.analyze_repository(tmp_path)

    rows = list(inst.apis_collection.store.items())
    assert len(rows) == 2, (
        "two no-template POSTs collapsed to one stored row — the identity "
        "collision the fabricated segment used to prevent has returned"
    )
    assert len({uuid for uuid, _ in rows}) == 2
    assert {r["properties"]["endpoint"] for _u, r in rows} == {"/api/items"}
    # and the two rows describe DIFFERENT handlers, so nothing was lost
    descs = {r["properties"]["api_description"] for _u, r in rows}
    assert any("Add" in d for d in descs) and any("Insert" in d for d in descs)


# ═══════════════════════════════════════════════════════════════════════════
# LEAVE-ALONE — every templated shape is unchanged
# ═══════════════════════════════════════════════════════════════════════════
def test_templated_shapes_are_byte_identical(tmp_path) -> None:
    """Passes both before and after the fix — a LEAVE-ALONE, labelled as such.
    It is the shape the golden fixture pins, which is why the corpus shows no
    CodeAPI diff for this change."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/items")]
    public class ItemsController
    {
        [HttpGet("all")]
        public string All() { return "ok"; }
        [HttpPost("add")]
        public string Add() { return "ok"; }
        [HttpDelete("/rooted")]
        public string Drop() { return "ok"; }
    }
}
''')
    assert _routes(fx) == [
        ("GET", "/api/items/all"),
        ("POST", "/api/items/add"),
        ("DELETE", "/api/items/rooted"),
    ]


def test_a_method_level_route_attribute_still_supplies_the_template(tmp_path) -> None:
    """`[HttpGet]` + `[Route("all")]` must resolve to the template, not to the
    new empty default."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/items")]
    public class ItemsController
    {
        [HttpGet]
        [Route("all")]
        public string All() { return "ok"; }
    }
}
''')
    assert _routes(fx) == [("GET", "/api/items/all")]


def test_an_undecorated_method_still_yields_no_row(tmp_path) -> None:
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder
    {
        public string Plain() { return "ok"; }
    }
}
''')
    assert _routes(fx) == []


def test_every_emitted_endpoint_still_starts_with_a_slash(tmp_path) -> None:
    fx = _extract(tmp_path, '''namespace Shop
{
    public class MixedController
    {
        [HttpGet("all")]
        public string All() { return "ok"; }
        [HttpPost]
        public string Add() { return "ok"; }
        [HttpDelete("/rooted")]
        public string Drop() { return "ok"; }
    }
}
''')
    endpoints = [ep for _m, ep in _routes(fx)]
    assert endpoints, "expected some routes"
    assert all(ep.startswith("/") for ep in endpoints), endpoints
