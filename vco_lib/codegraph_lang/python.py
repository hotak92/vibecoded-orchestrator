# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Python extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``CodeGraphAnalyzer._analyze_python_file`` in
``templates/scripts/analyze_code_graph.py``; the only body edit is the
mechanical ``self.`` -> ``ctx.`` rename (``ctx`` IS the analyzer instance —
the AST walk delegates entity emission to ``ctx._extract_class`` /
``ctx._extract_function``, which stay on the analyzer alongside the write
path and caches they populate). Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from vco_lib.codegraph_lang._shared import (
    _extract_external_calls,
    _is_minified_content,
)


def analyze_python_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a single Python file and extract entities."""

    stats = {'modules': 0, 'classes': 0, 'functions': 0}

    # Read file
    content = file_path.read_text(encoding='utf-8', errors='ignore')
    # CG-5 (v0.2.75 P3d): skip machine-minified content at walk time (skip +
    # log; NEVER delete existing rows — the orphan-clear owns deletion). One
    # home: _is_minified_content. A genuine long-line first-party file simply
    # isn't re-indexed this run.
    if _is_minified_content(content):
        try:
            _rel_min = file_path.relative_to(repo_root).as_posix()
        except Exception:  # noqa: BLE001
            _rel_min = str(file_path)
        print(f"⏭️  Skipping {_rel_min} (looks minified/generated)")
        return {'modules': 0, 'classes': 0, 'functions': 0}
    source_lines = content.split('\n')

    # Parse AST
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        print(f"⚠️  Syntax error in {file_path.relative_to(repo_root)}: {e}")
        return stats

    # Calculate file metrics
    loc = len([line for line in source_lines if line.strip() and not line.strip().startswith('#')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()

    # Check if file already analyzed (by hash)
    relative_path = file_path.relative_to(repo_root).as_posix()
    existing_module = ctx._get_existing_module(relative_path, file_hash)

    if existing_module:
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    # Extract imports
    imports = ctx._extract_imports(tree)

    # Generate module summary (first docstring or file description)
    module_summary = ctx._generate_module_summary(tree, source_lines, relative_path)

    # Create/update module
    module_uuid = ctx._create_or_update_module(
        path=relative_path,
        language="Python",
        loc=loc,
        complexity=ctx._calculate_complexity(tree),
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash,
        imports=imports,
        module_summary=module_summary
    )

    # Cache imports for cross-reference linking
    ctx.module_imports[relative_path] = imports

    stats['modules'] = 1

    # Track methods to avoid double-counting
    methods_seen = set()

    # Extract classes first and track their methods
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            ctx._extract_class(node, module_uuid, file_path, repo_root, source_lines)
            stats['classes'] += 1
            # Track all methods in this class
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods_seen.add(id(item))

    # Extract only top-level functions (not methods)
    for node in tree.body:  # Only check top-level items
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if id(node) not in methods_seen:
                ctx._extract_function(node, module_uuid, file_path, repo_root, source_lines)
                stats['functions'] += 1

    # Cross-language interactions (Python: use raw content; _strip_triple_quoted handles docstrings)
    ix = _extract_external_calls(content, imports, "Python", relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, "Python", module_uuid, file_path_rel=relative_path)

    return stats
