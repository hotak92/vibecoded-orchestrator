# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — Python HTTP-route extraction into ``CodeAPI``.

WHY THIS EXISTS
---------------
Until v0.2.92 ``vco_lib/codegraph_lang/python.py`` emitted NO ``KIND_API``
entities at all (``grep -n apis python.py`` returned nothing): only proto,
csharp and javascript produced them. Every Python project on every install
therefore had an empty CodeAPI slice, so ``search_code_graph(scope=
"interaction")`` / ``query_code_structure("interactions", ...)`` could never
surface a Python endpoint. These tests pin the new producer.

WHAT IS PINNED
--------------
1. TRUE POSITIVES — FastAPI ``APIRouter`` (including a router bound to a
   non-obvious name, a constructor ``prefix=``, and a same-file
   ``include_router(..., prefix=...)`` mount), FastAPI/Starlette ``@app.<verb>``
   and ``@app.websocket``, Flask ``@app.route`` / ``@blueprint.route`` with the
   ``methods=`` kwarg, and a router that is only CONVENTIONALLY named (imported
   from another module — the common real-world case).
2. FALSE POSITIVES — ``@property`` / ``@staticmethod`` / ``@classmethod`` /
   ``@lru_cache`` / a custom decorator factory named ``get`` / an HTTP-verb
   call on an object that is demonstrably not an app, and the non-route
   decorators of a REAL app (``@app.on_event`` / ``@app.middleware`` /
   ``@app.exception_handler``) must all yield ZERO api rows. The
   false-positive half matters as much as the true-positive half: a fabricated
   endpoint pollutes retrieval for everyone.
3. THE EMIT CONTRACT — exact extras key set, ``project`` inside extras, NO
   module reference, a DEFERRED (zero-arg) embed carrying the right text, the
   ``"<endpoint>:<method>"`` dedup identity, and emission AFTER every
   class/function entity so the writer can resolve the ``handler`` edge.
4. THE HANDLER EDGE END-TO-END — through the real
   ``CodeGraphAnalyzer.write_file_extraction``, the API row's ``handler``
   reference must be the UUID of the CodeFunction row for the decorated
   function, and must be ABSENT (never fabricated) when the handler has no row.
5. THE SHARED BUILDER — ``_shared.build_api_entity`` single-homes what were
   three hand-rolled copies; its output shape is pinned here, and the
   pre-existing producers' byte-identical behaviour is pinned by
   ``tests/test_codegraph_golden.py`` (which covers all three call-sites).
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

from vco_lib.codegraph_entities import (
    KIND_API,
    KIND_CLASS,
    KIND_FUNCTION,
)
from vco_lib.codegraph_lang._shared import ExtractorHelpers, build_api_entity
from vco_lib.codegraph_lang.python import extract_python_file

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

_PROJECT = "ApiProj"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0292_pyapi_acg", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── fake collections / analyzer wiring (same idiom as the writer suite) ─────
class _FakeCollectionData:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self._store[str(uuid)] = kwargs
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self._store[str(uuid)] = kwargs
        return str(uuid)

    def update(self, uuid: str, **kwargs: Any) -> None:
        existing = self._store.setdefault(str(uuid), {})
        props = existing.setdefault("properties", {})
        props.update(kwargs.get("properties", {}))
        return None


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.store: Dict[str, Dict[str, Any]] = {}
        self.data = _FakeCollectionData(self.store)


def _wire(analyzer_mod: types.ModuleType) -> Any:
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = _PROJECT
    inst.client = object()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = "python"
    inst._current_source = ""
    inst.modules_collection = _FakeCollection(f"{_PROJECT}_CodeModule")
    inst.classes_collection = _FakeCollection(f"{_PROJECT}_CodeClass")
    inst.functions_collection = _FakeCollection(f"{_PROJECT}_CodeFunction")
    inst.apis_collection = _FakeCollection(f"{_PROJECT}_CodeAPI")
    inst.interactions_collection = _FakeCollection(f"{_PROJECT}_CodeInteraction")
    return inst


def _stub_embeddings(analyzer_mod: types.ModuleType) -> None:
    analyzer_mod.generate_embedding = lambda text: None
    analyzer_mod.embed_module = lambda summary: None
    analyzer_mod.embed_function = lambda sig, body, language="python": None
    analyzer_mod.embed_class = (
        lambda sig, body, methods=None, language="python": None
    )


@pytest.fixture()
def ctx(analyzer_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: None)
    monkeypatch.setattr(analyzer_mod, "embed_module", lambda summary: None)
    monkeypatch.setattr(
        analyzer_mod, "embed_function",
        lambda sig, body, language="python": None,
    )
    monkeypatch.setattr(
        analyzer_mod, "embed_class",
        lambda sig, body, methods=None, language="python": None,
    )
    return _wire(analyzer_mod)


def _extract(ctx: Any, tmp_path: Path, source: str, name: str = "api.py"):
    """Run the pure producer over ``source`` written at ``tmp_path/name``."""
    repo = tmp_path
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return extract_python_file(source, target, repo, ExtractorHelpers(ctx))


def _apis(fx) -> List[Any]:
    return [e for e in fx.entities if e.kind == KIND_API]


def _routes(fx) -> List[tuple]:
    return [(e.extras["method"], e.extras["endpoint"]) for e in _apis(fx)]


# ═══════════════════════════════════════════════════════════════════════════
# 1 — TRUE POSITIVES
# ═══════════════════════════════════════════════════════════════════════════

_FASTAPI_ROUTER = '''
from fastapi import APIRouter, Depends

router = APIRouter()


@router.get("/users")
async def list_users(limit: int = 10):
    """Return every user."""
    return []


@router.post("/users")
async def create_user(payload: dict, db=Depends(get_db)) -> "User":
    return payload


@router.delete("/users/{user_id}")
def drop_user(user_id: int):
    return None
'''


def test_fastapi_apirouter_verbs_are_extracted(ctx, tmp_path):
    fx = _extract(ctx, tmp_path, _FASTAPI_ROUTER)
    assert _routes(fx) == [
        ("GET", "/users"),
        ("POST", "/users"),
        ("DELETE", "/users/{user_id}"),
    ]
    assert fx.stats["apis"] == 3


def test_fastapi_router_bound_to_non_obvious_name(ctx, tmp_path):
    """The identifier ``router`` must NOT be hardcoded — the binding is what
    proves the object is a router."""
    src = '''
from fastapi import APIRouter

zzz_totally_unconventional = APIRouter(prefix="/v9")


@zzz_totally_unconventional.patch("/thing")
def patch_thing():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("PATCH", "/v9/thing")]


def test_apirouter_constructor_prefix_is_applied(ctx, tmp_path):
    src = '''
from fastapi import APIRouter

v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def health():
    return {"ok": True}


@v1.get("")
def root():
    """An empty path means the prefix itself (a real FastAPI idiom)."""
    return {}
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/v1/health"), ("GET", "/v1")]


def test_include_router_prefix_composes_with_router_prefix(ctx, tmp_path):
    src = '''
from fastapi import APIRouter, FastAPI

app = FastAPI()
users = APIRouter(prefix="/users")


@users.get("/{user_id}")
def get_user(user_id: int):
    return {}


app.include_router(users, prefix="/api")
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/api/users/{user_id}")]


def test_unresolvable_prefix_records_route_without_it(ctx, tmp_path):
    """The router is imported and mounted elsewhere: the prefix cannot be known
    from this file. Record the un-prefixed path rather than dropping the route.
    """
    src = '''
from .deps import router


@router.get("/items")
def list_items():
    return []
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/items")]


def test_fastapi_app_verbs_and_websocket(ctx, tmp_path):
    src = '''
from fastapi import FastAPI

app = FastAPI()


@app.put("/config")
def set_config():
    return None


@app.head("/ping")
def ping():
    return None


@app.options("/cors")
def cors():
    return None


@app.websocket("/ws/feed")
async def feed(websocket):
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [
        ("PUT", "/config"),
        ("HEAD", "/ping"),
        ("OPTIONS", "/cors"),
        ("WEBSOCKET", "/ws/feed"),
    ]


def test_flask_route_methods_kwarg_yields_one_row_per_method(ctx, tmp_path):
    """A single decorator declaring N methods becomes N rows — the CodeAPI
    dedup identity is ``"<endpoint>:<method>"``, so one merged ``GET,POST`` row
    would be invisible to a method-filtered query."""
    src = '''
from flask import Flask

app = Flask(__name__)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate a user."""
    return ""


@app.route("/about")
def about():
    return ""
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [
        ("GET", "/login"),
        ("POST", "/login"),
        ("GET", "/about"),   # Flask's implicit default when methods= is absent
    ]


def test_flask_blueprint_url_prefix_and_registration(ctx, tmp_path):
    src = '''
from flask import Blueprint, Flask

app = Flask(__name__)
admin = Blueprint("admin", __name__, url_prefix="/admin")


@admin.route("/users", methods=("GET",))
def admin_users():
    return ""


app.register_blueprint(admin, url_prefix="/backoffice")
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/backoffice/admin/users")]


def test_flask2_verb_decorators(ctx, tmp_path):
    src = '''
from flask import Flask

app = Flask(__name__)


@app.post("/submit")
def submit():
    return ""
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("POST", "/submit")]


def test_conventional_name_without_binding_is_accepted(ctx, tmp_path):
    """aiohttp-style ``routes`` and a bare imported ``app`` carry no in-file
    binding; the conventional-name tier covers them (leading-slash required)."""
    src = '''
from somewhere import app, routes, custom_bp


@app.get("/a")
def a():
    return None


@routes.get("/b")
def b():
    return None


@custom_bp.route("/c", methods=["DELETE"])
def c():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/a"), ("GET", "/b"), ("DELETE", "/c")]


def test_stacked_route_decorators_yield_one_row_each(ctx, tmp_path):
    """Aliasing one handler under several paths is a standard Flask idiom."""
    src = '''
from flask import Flask

app = Flask(__name__)


@app.route("/")
@app.route("/index")
def index():
    """Landing page."""
    return ""
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/"), ("GET", "/index")]
    assert {e.extras["_handler_full_name"] for e in _apis(fx)} == {"api.index"}


def test_route_decorators_inside_strings_and_comments_are_ignored(ctx, tmp_path):
    """The producer parses the AST rather than grepping — text that merely
    LOOKS like a route (documentation, a test fixture, a commented-out line)
    must not become an endpoint."""
    src = '''
from fastapi import APIRouter

router = APIRouter()

EXAMPLE = """
@router.get("/from-a-string")
def documented():
    return None
"""


def explain():
    """Usage::

        @router.post("/from-a-docstring")
        def handler(): ...
    """
    # @router.delete("/from-a-comment")
    return EXAMPLE


@router.get("/real")
def real():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/real")]


def test_api_route_registrar_with_methods(ctx, tmp_path):
    src = '''
from fastapi import FastAPI

app = FastAPI()


@app.api_route("/legacy", methods=["GET", "PUT"])
def legacy():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/legacy"), ("PUT", "/legacy")]


# ═══════════════════════════════════════════════════════════════════════════
# 2 — FALSE POSITIVES (must produce ZERO api rows)
# ═══════════════════════════════════════════════════════════════════════════

_NOT_ROUTES = '''
from functools import lru_cache


def get(path):
    """A custom decorator FACTORY that happens to be named `get`."""
    def deco(fn):
        return fn
    return deco


store = {}


class Widget:
    @property
    def size(self):
        return 1

    @staticmethod
    def build():
        return Widget()

    @classmethod
    def make(cls):
        return cls()

    @lru_cache(maxsize=8)
    def expensive(self):
        return 2


@lru_cache
def memoized():
    return 3


@get("/looks-like-a-route")
def not_a_route():
    return None


@store.get("/also-not-a-route")
def still_not_a_route():
    return None
'''


def test_non_route_decorators_produce_no_api_rows(ctx, tmp_path):
    fx = _extract(ctx, tmp_path, _NOT_ROUTES)
    assert _apis(fx) == []
    assert fx.stats["apis"] == 0
    # ...while the ordinary class/function extraction is unaffected.
    assert fx.stats["classes"] == 1
    assert {e.full_name for e in fx.entities if e.kind == KIND_FUNCTION} >= {
        "api.memoized", "api.not_a_route", "api.still_not_a_route",
    }


def test_real_app_non_route_decorators_produce_no_api_rows(ctx, tmp_path):
    """A PROVEN FastAPI app still only routes on route verbs."""
    src = '''
from fastapi import FastAPI

app = FastAPI()


@app.on_event("startup")
async def boot():
    return None


@app.middleware("http")
async def timing(request, call_next):
    return None


@app.exception_handler(404)
async def missing(request, exc):
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _apis(fx) == []


def test_conventional_tier_requires_leading_slash(ctx, tmp_path):
    """Without an in-file binding the name is only a GUESS, so the path must
    corroborate it. Both Flask and Starlette require routes to start with '/'.
    """
    src = '''
from somewhere import router


@router.get("cache-key-not-a-path")
def looks_wrong():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _apis(fx) == []


def test_dynamic_path_is_skipped_not_guessed(ctx, tmp_path):
    src = '''
from fastapi import APIRouter

PREFIX = "/x"
router = APIRouter()


@router.get(PREFIX + "/dynamic")
def dynamic():
    return None


@router.get(f"/fstring/{PREFIX}")
def fstring():
    return None


@router.get("/literal")
def literal():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    assert _routes(fx) == [("GET", "/literal")]


def test_file_with_no_routes_produces_nothing_and_does_not_crash(ctx, tmp_path):
    src = '''
"""A perfectly ordinary module."""


class Thing:
    def method(self):
        return 1


def helper(a, b):
    return a + b
'''
    fx = _extract(ctx, tmp_path, src)
    assert _apis(fx) == []
    assert fx.stats == {"modules": 1, "classes": 1, "functions": 1, "apis": 0}


def test_empty_and_syntax_error_files_carry_the_apis_stat(ctx, tmp_path):
    empty = _extract(ctx, tmp_path, "", name="empty.py")
    assert empty.stats["apis"] == 0
    broken = _extract(ctx, tmp_path, "def (:\n", name="broken.py")
    assert broken.module is None
    assert broken.stats == {
        "modules": 0, "classes": 0, "functions": 0, "apis": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3 — THE EMIT CONTRACT
# ═══════════════════════════════════════════════════════════════════════════

def test_api_entity_emit_contract(ctx, tmp_path):
    src = '''
from fastapi import APIRouter

router = APIRouter(prefix="/v1")


@router.get("/users/{user_id}", response_model=UserOut)
async def read_user(user_id: int, db=None):
    """Fetch a single user by id.

    Longer prose that must NOT land in the description.
    """
    return {}
'''
    fx = _extract(ctx, tmp_path, src)
    api = _apis(fx)[0]

    # Property set is EXACTLY what the CodeAPI schema + content-hash contract
    # expects — no more (a stray key re-hashes every stored row), no less.
    assert set(api.extras) == {
        "endpoint", "method", "api_description", "parameters", "returns",
        "project", "proxy_target", "_handler_full_name",
    }
    assert api.extras["endpoint"] == "/v1/users/{user_id}"
    assert api.extras["method"] == "GET"
    assert api.extras["parameters"] == ["user_id", "db"]
    assert api.extras["returns"] == "UserOut"       # response_model wins
    assert api.extras["project"] == _PROJECT        # in extras, NOT the field
    assert api.extras["proxy_target"] == ""
    assert api.project is None                      # named field stays unset

    # The vectorized property carries method + path + qualified handler +
    # signature + the docstring's first line.
    desc = api.extras["api_description"]
    assert "GET /v1/users/{user_id}" in desc
    assert "api.read_user(user_id, db)" in desc
    assert "Fetch a single user by id." in desc
    assert "Longer prose" not in desc

    # Dedup identity + no module reference + a DEFERRED embed.
    assert api.identity_key() == "/v1/users/{user_id}:GET"
    assert api.references == {}
    assert api.vector is None
    assert callable(api.deferred_embed)

    # ``_handler_full_name`` is a private control key: never a stored property.
    props = api.to_insert_params()["properties"]
    assert "_handler_full_name" not in props
    assert props["endpoint"] == "/v1/users/{user_id}"


def test_deferred_embed_captures_its_own_description(ctx, tmp_path, monkeypatch,
                                                     analyzer_mod):
    """Default-argument capture, not late binding: with N routes in one loop a
    closure over the loop variable would embed the LAST description N times."""
    seen: List[str] = []
    monkeypatch.setattr(
        analyzer_mod, "generate_embedding", lambda text: seen.append(text),
    )
    src = '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/one")
def one():
    return None


@router.route("/two", methods=["GET", "POST"])
def two():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    apis = _apis(fx)
    for api in apis:
        api.deferred_embed()
    assert len(seen) == 3
    assert [s == a.extras["api_description"] for s, a in zip(seen, apis)] == [
        True, True, True,
    ]
    # The two methods of one decorator carry DIFFERENT text.
    assert seen[1] != seen[2]


def test_api_entities_are_emitted_after_every_handler(ctx, tmp_path):
    """The writer resolves ``_handler_full_name`` only against rows it has
    ALREADY written, so no API entity may precede a function entity."""
    src = '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/first")
def first():
    return None


class Late:
    def helper(self):
        return None


@router.get("/second")
def second():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    kinds = [e.kind for e in fx.entities]
    first_api = kinds.index(KIND_API)
    assert KIND_API not in kinds[:first_api]
    assert all(k == KIND_API for k in kinds[first_api:])
    assert KIND_CLASS in kinds[:first_api]
    assert KIND_FUNCTION in kinds[:first_api]


def test_handler_full_name_matches_method_and_function_shapes(ctx, tmp_path):
    src = '''
from fastapi import APIRouter

router = APIRouter()


class Views:
    @router.get("/on-method")
    def on_method(self, item_id: int):
        return None


@router.get("/on-function")
def on_function(item_id: int):
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    by_endpoint = {e.extras["endpoint"]: e for e in _apis(fx)}
    assert (by_endpoint["/on-method"].extras["_handler_full_name"]
            == "api.Views.on_method")
    assert (by_endpoint["/on-function"].extras["_handler_full_name"]
            == "api.on_function")
    # ``self`` is the receiver, not an endpoint parameter.
    assert by_endpoint["/on-method"].extras["parameters"] == ["item_id"]


def test_handler_edge_omitted_when_the_function_has_no_row(ctx, tmp_path):
    """A route on a function nested inside a factory (the Flask app-factory
    pattern) gets NO CodeFunction row — record the endpoint, but never point
    the edge at a same-named function that is not the handler."""
    src = '''
from flask import Flask


def index():
    """A top-level function that shares the nested handler's name."""
    return "decoy"


def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "real handler"

    return app
'''
    fx = _extract(ctx, tmp_path, src)
    api = _apis(fx)[0]
    assert api.extras["endpoint"] == "/"
    assert "_handler_full_name" not in api.extras


# ═══════════════════════════════════════════════════════════════════════════
# 4 — THE HANDLER EDGE, END-TO-END THROUGH THE REAL WRITER
# ═══════════════════════════════════════════════════════════════════════════

def test_writer_resolves_the_handler_reference_to_the_function_uuid(
    ctx, tmp_path, analyzer_mod,
):
    src = '''
from fastapi import APIRouter

router = APIRouter(prefix="/api")


@router.post("/orders")
def create_order(payload: dict):
    """Create an order."""
    return payload
'''
    fx = _extract(ctx, tmp_path, src)
    stats = ctx.write_file_extraction(fx)

    assert stats["apis"] == 1
    api_rows = list(ctx.apis_collection.store.values())
    assert len(api_rows) == 1
    row = api_rows[0]
    assert row["properties"]["endpoint"] == "/api/orders"
    assert row["properties"]["method"] == "POST"

    func_uuid = ctx.function_cache["api.create_order"]
    assert row["references"] == {"handler": func_uuid}
    # An API row must NOT carry a module reference (js/proto emit none, and
    # stamping one would diverge the stored edge).
    assert "module" not in row["references"]


def test_writer_drops_an_unresolvable_handler_edge(ctx, tmp_path):
    src = '''
from flask import Flask


def create_app():
    app = Flask(__name__)

    @app.get("/nested")
    def nested():
        return ""

    return app
'''
    fx = _extract(ctx, tmp_path, src)
    ctx.write_file_extraction(fx)
    row = list(ctx.apis_collection.store.values())[0]
    assert row["properties"]["endpoint"] == "/nested"
    assert not row.get("references")


def test_same_endpoint_different_methods_are_distinct_rows(ctx, tmp_path):
    src = '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/thing")
def read_thing():
    return None


@router.delete("/thing")
def drop_thing():
    return None
'''
    fx = _extract(ctx, tmp_path, src)
    ctx.write_file_extraction(fx)
    assert len(ctx.apis_collection.store) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 5 — THE SHARED CodeAPI BUILDER (single-homed in v0.2.92)
# ═══════════════════════════════════════════════════════════════════════════

def test_build_api_entity_shape_matches_the_pre_extraction_dict():
    """The three migrated call-sites (javascript / csharp / proto) had this
    dict hand-rolled. Byte-identity of their STORED output is pinned by
    tests/test_codegraph_golden.py; this pins the builder's shape directly."""
    calls: List[str] = []
    entity = build_api_entity(
        file_path_rel="src/routes.js",
        endpoint="/items/list",
        method="GET",
        description="GET /items/list (public) -> listItems",
        project="GoldenProj",
        embed=calls.append,
    )
    assert entity.kind == KIND_API
    assert entity.file_path_rel == "src/routes.js"
    assert entity.extras == {
        "endpoint": "/items/list",
        "method": "GET",
        "api_description": "GET /items/list (public) -> listItems",
        "parameters": [],
        "returns": "",
        "project": "GoldenProj",
        "proxy_target": "",
    }
    assert entity.identity_key() == "/items/list:GET"
    entity.deferred_embed()
    assert calls == ["GET /items/list (public) -> listItems"]


def test_build_api_entity_handler_key_only_when_requested():
    plain = build_api_entity(
        file_path_rel="a.py", endpoint="/x", method="GET",
        description="d", project="P", embed=lambda t: None,
    )
    assert "_handler_full_name" not in plain.extras

    linked = build_api_entity(
        file_path_rel="a.py", endpoint="/x", method="GET",
        description="d", project="P", embed=lambda t: None,
        handler_full_name="a.handler",
    )
    assert linked.extras["_handler_full_name"] == "a.handler"
    assert "_handler_full_name" not in linked.to_insert_params()["properties"]
