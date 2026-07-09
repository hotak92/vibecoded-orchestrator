# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shell extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_shell_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
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

from vco_lib.codegraph_entities import CodeEntity, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    _is_minified_content,
)


def analyze_shell_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a Shell script using regex-based parsing."""
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
    loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('#')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)

    # source / . file
    imports = re.findall(r'(?:source|\.)\s+([\w./${}/_-]+)', content)

    # Functions: name() { or function name {
    func_pattern = re.compile(
        r'^[ \t]*(?:function\s+)?([\w:.-]+)\s*\(\s*\)\s*\{|^[ \t]*function\s+([\w:.-]+)\s*\{',
        re.MULTILINE
    )

    # Module summary from top comment
    file_comment = ''
    for line in source_lines[:20]:
        s = line.strip()
        if s.startswith('#') and not s.startswith('#!') and s.lstrip('#').strip():
            file_comment = s.lstrip('#').strip()
            break
    summary_parts = [f"Shell script: {relative_path}"]
    if file_comment:
        summary_parts.append(file_comment)
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if [', 'if [[', 'while ', 'for ', 'case ']))

    module_uuid = ctx._create_or_update_module(
        path=relative_path, language="Shell", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    stats['modules'] = 1

    for m in func_pattern.finditer(content_clean):
        fname = m.group(1) or m.group(2)
        if not fname:
            continue
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        full_name = f"{file_path.stem}.{fname}"
        signature = f"{fname}()"
        ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(signature, body, language="python"),
        ))
        stats['functions'] += 1

    # Cross-language interactions (shell: gate on curl/wget presence in content)
    ix = _extract_external_calls(content_clean, imports + ["curl", "wget"], "shell", relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, "Shell", module_uuid, file_path_rel=relative_path)

    return stats
