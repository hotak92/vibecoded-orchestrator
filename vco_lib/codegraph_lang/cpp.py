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
from typing import Any, Dict, List

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    InteractionGroup,
    KIND_CLASS,
    KIND_FUNCTION,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    run_pure_extractor,
)


def extract_cpp_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a C++/header file, RETURN a :class:`FileExtraction`."""
    content = source_text
    source_lines = content.split('\n')
    loc = len([l for l in source_lines
               if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

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

    module = ModuleDescriptor(
        path=relative_path, language="C++", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=includes, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # Extract classes
    for cname, start_line in class_info.items():
        methods = [m.group(2) for m in method_pattern.finditer(content_clean)
                   if m.group(1) == cname]
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        signature = f"class {cname}"
        # Eager embed (computed at produce time — matches the imperative site).
        embedding = helpers.generate_embedding(
            f"{signature}\nMethods: {', '.join(methods[:10])}\n{class_body[:500]}"
        )
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{file_path.stem}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=start_line + len(class_lines),
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            vector=helpers.shape_for_insert(embedding) if embedding else None,
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
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=method_name, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="cpp")
            ),
        ))
        stats['functions'] += 1

    # Cross-language interactions (C++ uses #include as import gate)
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, includes, "C++", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="C++"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_cpp_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_cpp_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
