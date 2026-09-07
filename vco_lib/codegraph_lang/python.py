# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Python extractor for the code-graph analyzer.

P2f stage 3 (v0.2.77 Part 6): converted to a PURE PRODUCER, and the class /
function ENTITY-BUILDING that used to live on the analyzer
(``_extract_class`` / ``_extract_function``) moved HERE, behind the narrow
``helpers`` protocol. The AST *helpers* (``extract_source_code`` /
``extract_field_types`` / ``extract_annotation_type_names`` / ``get_name``)
stay on the analyzer and are reached via ``helpers`` passthroughs — the pure
builders never touch the analyzer instance or its caches.

``extract_python_file(source, path, repo_root, helpers) -> FileExtraction``
emits entities in the SAME order the imperative walk did — for each class:
the class entity, then its method function entities (the recursion), then the
top-level function entities — so the writer's cache captures
(class_cache / function_cache by full_name) reproduce the pre-Part-6 state
byte-identically. The thin ``analyze_python_file(ctx, ...)`` shim keeps the
skip gate analyzer-side; the analyzer's ``_extract_class`` / ``_extract_function``
survive as thin shims over the pure builders (a direct-call + cache-write seam
that ``tests/test_analyze_code_graph_v0_2_16.py`` pins).

``tests/test_codegraph_golden.py`` pins what this extractor does TODAY.
(The v0.2.92 note below is why that is no longer the same thing as parity
with the pre-move analyzer.)

v0.2.92 ADDS ``KIND_API`` emission (Python was the only mainstream producer
emitting none — see the block above :func:`_py_extract_routes`). API entities
are appended AFTER all class/function entities so the writer can resolve each
route's ``handler`` edge from the rows it already wrote; the class/function
emission order itself is untouched, so the golden snapshots for route-free
Python files stay byte-identical.
"""
from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    InteractionGroup,
    KIND_CLASS,
    KIND_FUNCTION,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_external_calls,
    build_api_entity,
    join_route,
    run_pure_extractor,
)

# ── v0.2.92: Python HTTP-route extraction (CodeAPI) ─────────────────────────
#
# Until v0.2.92 the python producer emitted NO ``KIND_API`` entities at all —
# only proto / csharp / javascript did. Every Python project on every install
# therefore had an empty CodeAPI slice, so ``search_code_graph(scope=
# "interaction")`` and ``query_code_structure("interactions", ...)`` could
# never surface a Python endpoint. This block closes that gap.
#
# DETECTION IS AST-BASED (the idiom this producer already uses for classes and
# functions — no regex pass is added). A decorator is a route only in the
# ``<object>.<verb>(<path>, ...)`` CALL-ON-ATTRIBUTE shape, which is what makes
# the false-positive rate acceptable: ``@property`` / ``@staticmethod`` /
# ``@lru_cache(maxsize=8)`` are bare names or calls on bare names and are
# rejected structurally, before any name matching happens.
#
# Two confidence tiers decide whether ``<object>`` is a web app/router:
#
#   RESOLVED   — the name is assigned from a known framework constructor
#                somewhere in this file (``x = APIRouter(prefix="/v1")``). The
#                binding is proof, so the path may be any string literal
#                (``@router.get("")`` is a real FastAPI route meaning "the
#                prefix itself").
#   CONVENTIONAL — the name only LOOKS like an app/router (``app``, ``router``,
#                ``bp``, ``*_router`` …), typically because the object was
#                imported from another module. Guessing needs corroboration, so
#                the path must additionally be a string literal starting with
#                "/" — which both Flask (werkzeug: "urls must start with a
#                leading slash") and Starlette/FastAPI (assert path.startswith
#                ("/")) actually require of every route.
#
# Non-literal paths (f-strings, ``PREFIX + "/x"``) are SKIPPED rather than
# guessed at: a fabricated endpoint is worse than a missing one.
_PY_HTTP_VERBS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "trace",
})
# ``route`` (Flask, Starlette) and ``api_route`` (FastAPI) take the method list
# in a ``methods=`` kwarg instead of encoding it in the attribute name.
_PY_ROUTE_REGISTRARS = frozenset({"route", "api_route"})
# Not an HTTP method, but a real inbound endpoint contract worth indexing.
_PY_WEBSOCKET_REGISTRARS = frozenset({"websocket", "websocket_route"})

# Constructors whose assignment PROVES the target name is an app/router, mapped
# to the framework label used in ``api_description``.
_PY_APP_CONSTRUCTORS = {
    "APIRouter": "FastAPI",
    "FastAPI": "FastAPI",
    "Flask": "Flask",
    "Blueprint": "Flask",
    "APIBlueprint": "Flask",
    "Quart": "Quart",
    "Sanic": "Sanic",
    "Starlette": "Starlette",
}
# Kwarg names that carry a mount prefix on a constructor / include call.
_PY_PREFIX_KWARGS = ("prefix", "url_prefix")
# Calls that mount one router/blueprint under another with an extra prefix.
_PY_INCLUDE_CALLS = frozenset({"include_router", "register_blueprint"})

# Names that CONVENTIONALLY hold an app/router when the binding is not visible
# in this file (the overwhelmingly common case: ``from .deps import router``).
_PY_CONVENTIONAL_APP_NAMES = frozenset({
    "app", "application", "router", "api", "api_router", "bp", "blueprint",
    "routes", "server",
})
_PY_CONVENTIONAL_APP_SUFFIXES = (
    "_app", "_router", "_api", "_bp", "_blueprint",
)

_PY_API_DOC_CAP = 180      # docstring slice folded into api_description
_PY_API_RETURNS_CAP = 200  # rendered return annotation / response_model


def _py_trailing_name(node: ast.AST) -> str:
    """The trailing identifier of a dotted expression.

    ``router`` -> "router"; ``deps.router`` -> "router"; ``self.app`` -> "app".
    Anything else (a call, a subscript) -> "" (never guessed at).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _py_str_const(node: Optional[ast.expr]) -> Optional[str]:
    """The value of a string literal node, or ``None`` for anything dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _py_kwarg(call: ast.Call, *names: str) -> Optional[ast.expr]:
    """First keyword argument of ``call`` matching one of ``names``."""
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    return None


def _py_collect_app_bindings(
    tree: ast.AST,
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Resolve, from THIS FILE ALONE, which names hold an app/router.

    Returns ``(frameworks, prefixes)`` keyed by the bound name:

      * ``frameworks[name]`` — the label from :data:`_PY_APP_CONSTRUCTORS`
        (proof the name is a router/app);
      * ``prefixes[name]``   — the mount prefix, composed from the constructor's
        own ``prefix=`` / ``url_prefix=`` kwarg AND from a same-file
        ``include_router(name, prefix=...)`` / ``register_blueprint(name,
        url_prefix=...)`` mount.

    A prefix declared in ANOTHER file (the router is imported, or mounted by the
    caller) is not resolvable here. Per the design rule, such a route is still
    recorded — with the un-prefixed path — rather than dropped.
    """
    frameworks: Dict[str, str] = {}
    own_prefix: Dict[str, str] = {}
    mount_prefix: Dict[str, str] = {}

    for node in ast.walk(tree):
        # 1. `name = APIRouter(prefix="/v1")` / `name: APIRouter = APIRouter()`
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value

        if isinstance(value, ast.Call):
            ctor = _py_trailing_name(value.func)
            label = _PY_APP_CONSTRUCTORS.get(ctor)
            if label:
                pfx = _py_str_const(_py_kwarg(value, *_PY_PREFIX_KWARGS)) or ""
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        frameworks[tgt.id] = label
                        if pfx:
                            own_prefix[tgt.id] = pfx

        # 2. `app.include_router(v1, prefix="/api")` — a same-file mount.
        if isinstance(node, ast.Call) and node.args:
            callee = _py_trailing_name(node.func)
            if callee in _PY_INCLUDE_CALLS:
                mounted = node.args[0]
                if isinstance(mounted, ast.Name):
                    pfx = _py_str_const(
                        _py_kwarg(node, *_PY_PREFIX_KWARGS)
                    ) or ""
                    # First mount wins: a router mounted twice has two real
                    # endpoints; picking the first is deterministic (source
                    # order) and yields a genuine one, where picking neither
                    # would yield an endpoint that is true for no mount.
                    if pfx and mounted.id not in mount_prefix:
                        mount_prefix[mounted.id] = pfx

    prefixes: Dict[str, str] = {}
    for name in set(own_prefix) | set(mount_prefix):
        prefixes[name] = join_route(
            mount_prefix.get(name, ""), own_prefix.get(name, "") or "/",
        ).rstrip("/")
    return frameworks, prefixes


def _py_route_methods(call: ast.Call, verb: str) -> List[str]:
    """The HTTP methods a route decorator declares.

    ``@router.get(...)``           -> ``["GET"]``
    ``@app.websocket(...)``        -> ``["WEBSOCKET"]``
    ``@app.route("/x")``           -> ``["GET"]``  (Flask's default; the
                                     implicit HEAD/OPTIONS are noise, not API
                                     surface, so they are not recorded)
    ``@app.route("/x", methods=["GET", "POST"])`` -> ``["GET", "POST"]``

    A multi-method decorator becomes ONE ROW PER METHOD, because the CodeAPI
    dedup identity is ``"<endpoint>:<method>"`` — a single ``"GET,POST"`` row
    would be invisible to a method-filtered query and would diverge from how
    every other producer represents an endpoint.
    """
    if verb in _PY_WEBSOCKET_REGISTRARS:
        return ["WEBSOCKET"]
    if verb in _PY_HTTP_VERBS:
        return [verb.upper()]

    methods_node = _py_kwarg(call, "methods")
    methods: List[str] = []
    if isinstance(methods_node, (ast.List, ast.Tuple, ast.Set)):
        for elt in methods_node.elts:
            val = _py_str_const(elt)
            if val:
                token = val.strip().upper()
                if token and token not in methods:
                    methods.append(token)
    return methods or ["GET"]


def _py_route_path(call: ast.Call) -> Optional[str]:
    """The literal route path of a route decorator call, or ``None``.

    Accepts the first positional argument or the ``path=`` (FastAPI) /
    ``rule=`` (Flask) keyword. Dynamic expressions yield ``None``.
    """
    if call.args:
        return _py_str_const(call.args[0])
    return _py_str_const(_py_kwarg(call, "path", "rule"))


def _py_render_annotation(node: Optional[ast.expr]) -> str:
    """Best-effort source rendering of an annotation / kwarg expression."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)[:_PY_API_RETURNS_CAP]
    except Exception:  # noqa: BLE001 — a render failure never wedges a walk
        return ""


def _py_handler_parameters(func: ast.AST, is_method: bool) -> List[str]:
    """Endpoint parameter names, dropping the ``self`` / ``cls`` receiver."""
    args = func.args  # type: ignore[attr-defined]
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if is_method and names and names[0] in ("self", "cls"):
        names = names[1:]
    return names


def _py_extract_routes(
    tree: ast.Module,
    file_path: Path,
    relative_path: str,
    emitted_full_names: Dict[int, str],
    method_node_ids: set,
    helpers: Any,
) -> List[CodeEntity]:
    """Build the ``KIND_API`` entities for every route decorator in the file.

    ``emitted_full_names`` maps ``id(func_node)`` -> the ``full_name`` of the
    CodeFunction entity this producer emits for it. A route on a function that
    gets NO CodeFunction row (a handler nested inside a Flask app factory, which
    this producer does not walk) still yields its API row — just without a
    ``handler`` edge. Never point the edge at a same-named function that is not
    the actual handler; a wrong edge is worse than a missing one.
    """
    frameworks, prefixes = _py_collect_app_bindings(tree)
    candidates: List[tuple] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec_index, dec in enumerate(node.decorator_list):
            # STRUCTURAL GATE: only `<object>.<verb>(...)` can be a route.
            # `@property`, `@staticmethod`, `@lru_cache(...)`, `@get("/x")`
            # and every other bare-name decorator never reach the name checks.
            if not isinstance(dec, ast.Call):
                continue
            if not isinstance(dec.func, ast.Attribute):
                continue
            verb = dec.func.attr
            if (verb not in _PY_HTTP_VERBS
                    and verb not in _PY_ROUTE_REGISTRARS
                    and verb not in _PY_WEBSOCKET_REGISTRARS):
                continue

            obj = _py_trailing_name(dec.func.value)
            if not obj:
                continue
            framework = frameworks.get(obj)
            path = _py_route_path(dec)
            if path is None:
                continue  # dynamic path — record nothing rather than guess
            if framework is None:
                # CONVENTIONAL tier: the binding is not visible in this file, so
                # require BOTH a plausible name AND a leading-slash path.
                conventional = (
                    obj in _PY_CONVENTIONAL_APP_NAMES
                    or obj.endswith(_PY_CONVENTIONAL_APP_SUFFIXES)
                )
                if not conventional or not path.startswith("/"):
                    continue
                framework = "HTTP"

            endpoint = join_route(prefixes.get(obj, ""), path)
            candidates.append(
                (node.lineno, node.col_offset, dec_index, endpoint,
                 framework, dec, node)
            )

    entities: List[CodeEntity] = []
    # Deterministic emission order regardless of ``ast.walk`` traversal order.
    for lineno, _col, _di, endpoint, framework, dec, node in sorted(
        candidates, key=lambda c: (c[0], c[1], c[2], c[3])
    ):
        is_method = id(node) in method_node_ids
        handler_full_name = emitted_full_names.get(id(node))
        params = _py_handler_parameters(node, is_method)
        display = handler_full_name or f"{file_path.stem}.{node.name}"

        # ``returns``: the declared response shape. FastAPI's response_model is
        # the API's real contract and outranks the handler's return annotation.
        returns = _py_render_annotation(_py_kwarg(dec, "response_model"))
        if not returns:
            returns = _py_render_annotation(node.returns)

        doc = (ast.get_docstring(node) or "").strip()
        doc_line = doc.split("\n", 1)[0].strip()[:_PY_API_DOC_CAP] if doc else ""

        for method in _py_route_methods(dec, dec.func.attr):  # type: ignore[union-attr]
            # ``api_description`` is the ONLY vectorized CodeAPI property
            # (everything else is skip_vectorization) — so it carries the
            # method, the routed path, the qualified handler + its signature,
            # and the handler's summary line: the text someone actually
            # searches for when they ask "where do we handle user login?".
            description = (
                f"Python {framework} {method} {endpoint} "
                f"→ {display}({', '.join(params)})"
            )
            if returns:
                description += f" -> {returns}"
            if doc_line:
                description += f". {doc_line}"

            entities.append(build_api_entity(
                file_path_rel=relative_path,
                endpoint=endpoint,
                method=method,
                description=description,
                project=helpers.project_name,
                parameters=params,
                returns=returns,
                handler_full_name=handler_full_name,
                embed=helpers.generate_embedding,
            ))
    return entities


def build_python_function_entity(
    node: ast.AST,
    file_path: Path,
    repo_root: Path,
    source_lines: List[str],
    helpers: Any,
    parent_class: Optional[str] = None,
) -> CodeEntity:
    """Pure builder for a Python function/method CodeEntity (was
    ``CodeGraphAnalyzer._extract_function``'s entity construction).

    Returns the entity WITHOUT the module reference (the writer stamps it).
    ``type_uses`` is python-only. Mutates no analyzer state.
    """
    # Cross-OS UUID stability (v0.2.16 — bug 0.7): POSIX-normalize.
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Get signature
    args = [arg.arg for arg in node.args.args]  # type: ignore[attr-defined]
    signature = f"{node.name}({', '.join(args)})"  # type: ignore[attr-defined]

    # Get docstring
    doc = ast.get_docstring(node) or ""  # type: ignore[arg-type]

    # Extract full function body for embedding
    function_body = helpers.extract_source_code(node, source_lines)

    # Determine full name
    if parent_class:
        full_name = f"{file_path.stem}.{parent_class}.{node.name}"  # type: ignore[attr-defined]
    else:
        full_name = f"{file_path.stem}.{node.name}"  # type: ignore[attr-defined]

    # Extract SCG-style type_uses from argument annotations and return annotation
    type_uses: List[str] = []
    seen_type_uses: set = set()

    def _add_type_names(annotation: Optional[ast.expr]) -> None:
        for t in helpers.extract_annotation_type_names(annotation):
            if t not in seen_type_uses:
                seen_type_uses.add(t)
                type_uses.append(t)

    for arg in node.args.args:  # type: ignore[attr-defined]
        _add_type_names(arg.annotation)
    for arg in node.args.posonlyargs:  # type: ignore[attr-defined]
        _add_type_names(arg.annotation)
    for arg in node.args.kwonlyargs:  # type: ignore[attr-defined]
        _add_type_names(arg.annotation)
    if node.args.vararg:  # type: ignore[attr-defined]
        _add_type_names(node.args.vararg.annotation)  # type: ignore[attr-defined]
    if node.args.kwarg:  # type: ignore[attr-defined]
        _add_type_names(node.args.kwarg.annotation)  # type: ignore[attr-defined]
    _add_type_names(node.returns)  # type: ignore[attr-defined]

    return CodeEntity(
        kind=KIND_FUNCTION, file_path_rel=relative_path,
        name=node.name,  # type: ignore[attr-defined]
        full_name=full_name,
        body=function_body,
        signature=signature,
        doc=doc,
        start_line=node.lineno,  # type: ignore[attr-defined]
        end_line=node.end_lineno or node.lineno,  # type: ignore[attr-defined]
        is_async=isinstance(node, ast.AsyncFunctionDef),
        project=helpers.project_name,
        # type_uses is PYTHON-ONLY (SCG type-annotation edges).
        extras={"type_uses": type_uses},
        deferred_embed=(
            lambda sig=signature, fb=function_body:
            helpers.embed_function(sig, fb, language="python")
        ),
    )


def build_python_class_entities(
    node: ast.ClassDef,
    file_path: Path,
    repo_root: Path,
    source_lines: List[str],
    helpers: Any,
) -> List[CodeEntity]:
    """Pure builder for a Python class + its methods (was
    ``CodeGraphAnalyzer._extract_class``'s entity construction + the method
    recursion). Returns ``[class_entity, method_entity, ...]`` in emission
    order — class first, then each method (the imperative code wrote the class
    row THEN recursed into ``_extract_function`` per method). Mutates no
    analyzer state; the writer stamps module refs + populates the caches.
    """
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Get methods
    methods = [m.name for m in node.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

    # Get docstring
    doc = ast.get_docstring(node) or ""

    # Get base classes
    base_names = [helpers.get_name(base) for base in node.bases]

    # Extract full class body for embedding
    class_body = helpers.extract_source_code(node, source_lines)

    # Get signature (class definition line only)
    signature = f"class {node.name}"
    if node.bases:
        signature += f"({', '.join(base_names)})"

    # Extract SCG-style composition edges
    field_types = helpers.extract_field_types(node)
    # composes = unique class names that appear as field types
    composes: List[str] = []
    seen_composes: set = set()
    for pair in field_types:
        type_name = pair.split(':', 1)[1] if ':' in pair else ''
        if type_name and type_name not in seen_composes:
            seen_composes.add(type_name)
            composes.append(type_name)

    class_entity = CodeEntity(
        kind=KIND_CLASS, file_path_rel=relative_path,
        name=node.name,
        full_name=f"{file_path.stem}.{node.name}",
        body=class_body,
        signature=signature,
        doc=doc,
        start_line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        project=helpers.project_name,
        # field_types + composes are PYTHON-ONLY (SCG composition edges).
        extras={"methods": methods, "field_types": field_types, "composes": composes},
        deferred_embed=(
            lambda sig=signature, cb=class_body, mth=methods:
            helpers.embed_class(sig, cb, methods=mth, language="python")
        ),
    )

    entities: List[CodeEntity] = [class_entity]
    # Extract methods (the recursion order the imperative code used).
    for method in node.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entities.append(build_python_function_entity(
                method, file_path, repo_root, source_lines, helpers,
                parent_class=node.name,
            ))
    return entities


def extract_python_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Python file, RETURN a :class:`FileExtraction`.

    Emits entities in the imperative walk order (each class + its methods, then
    top-level functions). ``imports`` are surfaced so the writer populates the
    ``module_imports`` cross-ref cache (python-only).
    """
    content = source_text
    source_lines = content.split('\n')

    # Parse AST
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        print(f"⚠️  Syntax error in {file_path.relative_to(repo_root)}: {e}")
        # Walk-time no-op: no module, stats verbatim (matches the imperative
        # ``return stats`` with the zero-init dict).
        return FileExtraction(
            module=None,
            stats={'modules': 0, 'classes': 0, 'functions': 0, 'apis': 0},
        )

    # Calculate file metrics
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('#')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Extract imports
    imports = helpers.extract_imports(tree)

    # Generate module summary (first docstring or file description)
    module_summary = helpers.generate_module_summary(tree, source_lines, relative_path)

    module = ModuleDescriptor(
        path=relative_path,
        language="Python",
        loc=loc,
        complexity=helpers.calculate_complexity(tree),
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash,
        imports=imports,
        module_summary=module_summary,
    )

    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0,
                             'apis': 0}

    # Track methods to avoid double-counting
    methods_seen = set()
    # v0.2.92: id(func_node) -> the full_name of the CodeFunction entity emitted
    # for it, so a route decorator can request the ``handler`` edge for EXACTLY
    # the function that got a row (see _py_extract_routes).
    emitted_full_names: Dict[int, str] = {}

    # Extract classes first and track their methods
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_entities = build_python_class_entities(
                node, file_path, repo_root, source_lines, helpers,
            )
            entities.extend(class_entities)
            stats['classes'] += 1
            # Track all methods in this class
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods_seen.add(id(item))
                    emitted_full_names[id(item)] = (
                        f"{file_path.stem}.{node.name}.{item.name}"
                    )

    # Extract only top-level functions (not methods)
    for node in tree.body:  # Only check top-level items
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if id(node) not in methods_seen:
                entities.append(build_python_function_entity(
                    node, file_path, repo_root, source_lines, helpers,
                ))
                stats['functions'] += 1
                emitted_full_names[id(node)] = f"{file_path.stem}.{node.name}"

    # v0.2.92: HTTP routes -> CodeAPI. Appended LAST on purpose: the writer
    # resolves ``_handler_full_name`` only against entities it has ALREADY
    # written for this file, so every handler row must precede its API row.
    api_entities = _py_extract_routes(
        tree, file_path, relative_path, emitted_full_names, methods_seen,
        helpers,
    )
    entities.extend(api_entities)
    stats['apis'] = len(api_entities)

    # Cross-language interactions (Python: use raw content; _strip_triple_quoted handles docstrings)
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content, imports, "Python", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="Python"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=imports, stats=stats,
    )


def analyze_python_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim over the pure :func:`extract_python_file` producer.

    Keeps the walk-time I/O + minified + unchanged-skip gates analyzer-side
    (short-circuit preserved), then extract -> ``ctx.write_file_extraction``
    -> stats dict. The AST syntax-error skip lives INSIDE ``extract_python_file``
    (it needs the parsed tree) — it returns a module-less FileExtraction that
    the writer no-ops, matching the imperative early ``return stats``.
    """
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_python_file,
        {'modules': 0, 'classes': 0, 'functions': 0, 'apis': 0},
    )
