# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""C/C++ extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_cpp_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
(``ctx`` IS the analyzer instance) and the analyzer-resident embedding
seams reached via ``ctx.``. Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from vco_lib.codegraph_entities import CodeEntity, KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    _is_minified_content,
)


def analyze_cpp_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a C++/header file using regex-based parsing."""
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
    loc = len([l for l in source_lines
               if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    # Strip comments for pattern matching
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # Includes
    includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)

    # Classes / structs
    class_pattern = re.compile(
        r'(?:^|\n)\s*(?:class|struct)\s+([\w]+)\s*'
        r'(?::[^{]*)?\{',
        re.MULTILINE
    )
    class_info: Dict[str, int] = {}
    for m in class_pattern.finditer(content_clean):
        cname = m.group(1)
        if cname in ('if', 'else', 'while', 'for', 'switch', 'namespace', 'return'):
            continue
        start_line = content_clean[:m.start()].count('\n') + 1
        class_info[cname] = start_line

    # Method implementations: ClassName::methodName(...)
    method_pattern = re.compile(
        r'\b([\w]+)\s*::\s*([\w~]+)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{',
        re.MULTILINE
    )

    # Module summary from file-level comment
    file_comment = ''
    for line in source_lines[:20]:
        s = line.strip()
        if s.startswith('//') or (s.startswith('*') and not s.startswith('*/')):
            cleaned = s.lstrip('/*').strip()
            if cleaned:
                file_comment = cleaned
                break

    summary_parts = [f"C++ module: {relative_path}"]
    if file_comment:
        summary_parts.append(file_comment)
    if class_info:
        summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if (', 'while (', 'for (', 'switch (', 'else if']))

    module_uuid = ctx._create_or_update_module(
        path=relative_path, language="C++", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=includes, module_summary=module_summary,
    )
    stats['modules'] = 1

    # Extract classes
    for cname, start_line in class_info.items():
        methods = [m.group(2) for m in method_pattern.finditer(content_clean)
                   if m.group(1) == cname]
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        signature = f"class {cname}"
        embedding = ctx.generate_embedding(
            f"{signature}\nMethods: {', '.join(methods[:10])}\n{class_body[:500]}"
        )
        ctx.store_entity(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{file_path.stem}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=start_line + len(class_lines),
            project=ctx.project_name,
            extras={"methods": methods[:20]},
            references={"module": module_uuid},
            vector=ctx._shape_for_insert(embedding) if embedding else None,
        ))
        stats['classes'] += 1

    # Extract method implementations
    for m in method_pattern.finditer(content_clean):
        class_name, method_name, args_str = m.group(1), m.group(2), m.group(3)
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        full_name = f"{file_path.stem}.{class_name}.{method_name}"
        signature = f"{class_name}::{method_name}({args_str})"
        ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=method_name, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(signature, body, language="cpp"),
        ))
        stats['functions'] += 1

    # Cross-language interactions (C++ uses #include as import gate)
    ix = _extract_external_calls(content_clean, includes, "C++", relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, "C++", module_uuid, file_path_rel=relative_path)

    return stats
