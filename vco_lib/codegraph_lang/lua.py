# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Lua extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_lua_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
(``ctx`` IS the analyzer instance) and the analyzer-resident embedding
seams reached via ``ctx.``. Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

from vco_lib.codegraph_entities import CodeEntity, KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    _is_minified_content,
)


def analyze_lua_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a Lua file using regex-based parsing."""
    stats = {'modules': 0, 'classes': 0, 'functions': 0}

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
    loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('--')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    # Imports: require('mod') or require "mod"
    imports = re.findall(r"require\s*[(\s]*[\"']([^\"']+)[\"']", content)

    # Detect table-based classes: uppercase Name = {} or Name.__index = Name
    class_names: Set[str] = set()
    for m in re.finditer(r'^([A-Z][\w]*)\s*=\s*\{\}', content, re.MULTILINE):
        class_names.add(m.group(1))
    for m in re.finditer(r'^([\w]+)\.__index\s*=\s*\1', content, re.MULTILINE):
        class_names.add(m.group(1))

    # Function pattern: covers function f(), local function f(), Obj.f = function()
    func_pattern = re.compile(
        r'^(?:local\s+)?function\s+([\w.:]+)\s*\(([^)]*)\)|'
        r'^([\w.]+)\.([\w]+)\s*=\s*function\s*\(([^)]*)\)',
        re.MULTILINE
    )
    all_func_names = []
    for m in func_pattern.finditer(content):
        name = m.group(1) if m.group(1) else f'{m.group(3)}.{m.group(4)}'
        all_func_names.append(name)

    # Module summary from leading comments
    first_comments: List[str] = []
    for line in source_lines[:15]:
        s = line.strip()
        if s.startswith('--'):
            first_comments.append(s.lstrip('-').strip())
        elif s:
            break

    summary_parts = [f"Lua module: {relative_path}"]
    if first_comments:
        summary_parts.append(' '.join(first_comments[:3]))
    if class_names:
        summary_parts.append(f"Classes: {', '.join(sorted(class_names))}")
    if all_func_names:
        summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content.count(kw) for kw in ['if ', 'elseif ', 'while ', 'for ', 'repeat ']))

    module_uuid = ctx._create_or_update_module(
        path=relative_path, language="Lua", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    stats['modules'] = 1

    # Extract classes (table-OOP pattern)
    for class_name in class_names:
        methods: List[str] = []
        for m in re.finditer(
            rf'(?:function\s+{re.escape(class_name)}[.:]([\w]+)\s*\(|'
            rf'{re.escape(class_name)}\.([\w]+)\s*=\s*function\s*\()',
            content
        ):
            methods.append(m.group(1) or m.group(2))

        class_match = re.search(rf'^{re.escape(class_name)}\s*=\s*\{{', content, re.MULTILINE)
        start_line = content[:class_match.start()].count('\n') + 1 if class_match else 1

        body = f"{class_name} = {{}}\n" + '\n'.join(
            f"function {class_name}.{mth}(...) end" for mth in methods
        )
        signature = f"{class_name} = {{}} -- Lua table class"
        # V52-O.11.H (v0.2.52, 2026-06-09): pass language="lua" — pre-
        # V52-O.11.H this passed language="javascript", routing Lua
        # embeddings through the JS code-path and mislabeling retrieval
        # scores by language. Embeddings stay 2048-dim either way
        # (model-agnostic), but downstream language-filtered retrieval
        # (--language=lua scopes) was silently never matching.

        ctx.store_entity(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=class_name, full_name=f"{file_path.stem}.{class_name}",
            body=body, signature=signature,
            doc="", start_line=start_line, end_line=start_line,
            project=ctx.project_name,
            extras={"methods": methods},
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_class(signature, "", methods=methods, language="lua"),
        ))
        stats['classes'] += 1

    # Extract standalone functions (skip class methods already indexed)
    class_prefixes = tuple(cn + '.' for cn in class_names) + tuple(cn + ':' for cn in class_names)

    for m in func_pattern.finditer(content):
        if m.group(1):
            func_name, args_str = m.group(1), m.group(2)
        else:
            func_name, args_str = f'{m.group(3)}.{m.group(4)}', m.group(5) or ''

        if any(func_name.startswith(p) for p in class_prefixes):
            continue

        start_line = content[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        body = '\n'.join(source_lines[start_line - 1:end_line])

        func_full_name = f"{file_path.stem}.{func_name}"
        # V52-O.11.H (v0.2.52, 2026-06-09): see comment on the Lua class
        # embed_class call above — same fix for the function path.

        ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=func_name.split('.')[-1].split(':')[-1],
            full_name=func_full_name,
            body=body, signature=f"{func_name}({args_str})",
            doc="", start_line=start_line, end_line=end_line,
            is_async=False, project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(f"function {func_name}({args_str})", body, language="lua"),
        ))
        stats['functions'] += 1

    # Cross-language interactions
    ix = _extract_external_calls(content, imports, "Lua", relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, "Lua", module_uuid, file_path_rel=relative_path)

    return stats
