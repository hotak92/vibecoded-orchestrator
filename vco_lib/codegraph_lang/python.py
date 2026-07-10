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

Behavior is pinned byte-identically by ``tests/test_codegraph_golden.py``.
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
    run_pure_extractor,
)


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
            stats={'modules': 0, 'classes': 0, 'functions': 0},
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
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # Track methods to avoid double-counting
    methods_seen = set()

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

    # Extract only top-level functions (not methods)
    for node in tree.body:  # Only check top-level items
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if id(node) not in methods_seen:
                entities.append(build_python_function_entity(
                    node, file_path, repo_root, source_lines, helpers,
                ))
                stats['functions'] += 1

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
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
