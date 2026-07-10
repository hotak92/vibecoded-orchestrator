# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Go extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``_go_methods_for_struct`` (V52-O.11.F.2-GO per-struct method
attribution) and ``CodeGraphAnalyzer._analyze_go_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
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


def _go_methods_for_struct(
    content_clean: str,
    struct_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-GO (v0.2.52, 2026-06-09): extract methods declared on
    ``struct_name`` via Go's receiver syntax.

    Replaces the pre-V52-O.11.F.2-GO line that ran
    ``func_pattern.finditer(content_clean)`` unconditionally — the same
    bug as Rust's V52-O.11.F, applied to the Go parser. Audit a79152
    flagged the four parallel sites (Go, JS/TS, Java, C#); this fix
    closes the Go one. A 50-fn Go file with 3 structs produced 150
    incorrect method attributions, drowning real signal in the
    ``query_code_structure(methods, StructName)`` MCP path.

    Go's method-scoping model (CRITICAL difference from Rust):

      Go does NOT use ``impl <Type> { ... }``. Methods are functions
      with a **receiver** declared BEFORE the function name:

          func (recv *Foo) MethodName(args...) ReturnType { ... }
          func (recv  Foo) MethodName(args...) ReturnType { ... }

      The token between the receiver-paren and the close-paren is the
      type. A leading ``*`` denotes a pointer receiver; both pointer
      and value receivers are methods on the same type.

    Algorithm:

      1. Match every ``func (<recv_var> [*&]?<struct_name>) <Name>(``
         pattern in ``content_clean``. The receiver variable name is
         arbitrary (often a single letter), so we accept ``\\w+`` for it.
         Whitespace around the type is lenient because Go style varies.
      2. Collect ``<Name>`` values. Deduplicate, preserving first-seen
         order — a struct can have a method declared on both the
         pointer and value receiver in pathological code, but only one
         name lands in the methods list.
      3. We DO NOT pick up receiver-less ``func Name(...)`` declarations
         — those are package-level free functions, not methods. They
         get processed by the separate function-entry loop downstream.

    Returns empty list if no receiver-bound functions match — common
    for plain data structs (`type Config struct { ... }`) that only
    carry fields, and for interfaces whose method set is defined via
    the interface body itself (the interface body's method declarations
    don't use `func` keyword, so the regex correctly ignores them).

    Limitations:
      - Generic Go methods (Go 1.18+, ``func (s *Foo[T]) Method()...``)
        are matched: the ``[*&]?<struct_name>`` segment captures the
        bare name and the optional ``[T]`` generic parameter follows
        before the close-paren, which we tolerate via a permissive
        post-name pattern (``[^)]*``).
      - Methods declared in another file of the same package on the
        same type are correctly NOT picked up here — they only land
        in the methods list when the OTHER file is being analyzed
        (each file is parsed independently). This is consistent with
        the Rust path's per-file scoping and with how Go's tooling
        itself reports methods.
      - Doesn't follow embedded-struct method promotion (interfaces
        that embed another interface's method set, struct types that
        embed another struct). Those are intentionally out of scope
        for this regex-based parser; tree-sitter is the right tool
        for that level of fidelity (queued as V52-O.11.G in backlog).
    """
    escaped = re.escape(struct_name)
    # Receiver shape:
    #   func ( <recv_var> [*&]? <struct_name> [generics?] ) <Name> (
    # Notes on each piece:
    #   - `func\s*\(` — opening receiver paren, possibly with
    #     whitespace between `func` and `(` (rare but legal style).
    #   - `\s*\w+\s+` — the receiver variable name (e.g. `f`, `recv`,
    #     `self`). Mandatory in Go syntax: anonymous receivers don't
    #     exist (you can use `_` but it's still a word char).
    #   - `[*&]?` — optional pointer marker. `&` isn't legal Go syntax
    #     for receivers, but we include it for robustness against
    #     hand-written test fixtures and the cost of one extra char in
    #     the regex is negligible. Real Go code only uses `*`.
    #   - `{escaped}` — the literal struct name, regex-escaped.
    #   - `[^)]*` — anything else up to the closing receiver paren
    #     (covers generic parameters like `[T]`, type assertions, and
    #     trailing whitespace).
    #   - `\)` — closing receiver paren.
    #   - `\s+(\w+)\s*\(` — the method name (captured) followed by its
    #     own argument paren. The trailing `\(` anchors us to a real
    #     function declaration vs. a stray `func (...)` cast expression.
    method_pattern = re.compile(
        rf"func\s*\(\s*\w+\s+[*&]?{escaped}[^)]*\)\s+(\w+)\s*\(",
        re.MULTILINE,
    )

    methods: List[str] = []
    seen: set = set()
    for m in method_pattern.finditer(content_clean):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        methods.append(name)
    return methods


def extract_go_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Go file, RETURN a :class:`FileExtraction`."""
    content = source_text
    source_lines = content.split('\n')
    loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Strip comments
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # Imports
    single_imports = re.findall(r'import\s+"([^"]+)"', content)
    block_imports = re.findall(r'"([^"]+)"', re.sub(r'import\s*\(([^)]*)\)', r'\1', content, flags=re.DOTALL))
    imports = list(dict.fromkeys(single_imports + block_imports))

    # Structs / interfaces as "classes"
    type_pattern = re.compile(r'type\s+([\w]+)\s+(?:struct|interface)\s*\{', re.MULTILINE)
    struct_info: Dict[str, int] = {}
    for m in type_pattern.finditer(content_clean):
        name = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        struct_info[name] = start_line

    # Functions: func Name(...) and methods: func (recv Type) Name(...)
    func_pattern = re.compile(
        r'func\s+(?:\([^)]+\)\s+)?([\w]+)\s*\(([^)]*)\)',
        re.MULTILINE
    )

    # Module summary
    pkg_match = re.search(r'^package\s+(\w+)', content, re.MULTILINE)
    pkg_name = pkg_match.group(1) if pkg_match else file_path.stem
    file_comment = ''
    for line in source_lines[:15]:
        s = line.strip()
        if s.startswith('//'):
            file_comment = s.lstrip('/').strip()
            break
    summary_parts = [f"Go module: {relative_path} (package {pkg_name})"]
    if file_comment:
        summary_parts.append(file_comment)
    if struct_info:
        summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if ', 'for ', 'switch ', 'select {']))

    module = ModuleDescriptor(
        path=relative_path, language="Go", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # Struct/interface entries
    for sname, start_line in struct_info.items():
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        signature = f"type {sname} struct/interface"
        # V52-O.11.F.2-GO (v0.2.52, 2026-06-09): scope `methods` to
        # functions whose receiver is `sname`. Pre-fix this line
        # iterated func_pattern over the WHOLE file's content_clean,
        # attributing EVERY fn (including receiver-less package-level
        # functions and methods on OTHER structs) to EVERY struct —
        # audit a79152 reproduced: a 50-fn file with 3 structs
        # produced 150 incorrect method attributions, drowning real
        # signal in the `query_code_structure(methods, StructName)`
        # MCP path. Mirrors V52-O.11.F (Rust); Go uses receiver
        # syntax instead of `impl` blocks (see helper docstring).
        methods = _go_methods_for_struct(content_clean, sname, source_lines)
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=sname, full_name=f"{pkg_name}.{sname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=start_line + len(class_lines),
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, cb=class_body:
                helpers.embed_class(sig, cb, language="go")
            ),
        ))
        stats['classes'] += 1

    # Function entries
    for m in func_pattern.finditer(content_clean):
        fname, args_str = m.group(1), m.group(2)
        if fname[0].islower() and fname in ('if', 'for', 'switch', 'select'):
            continue
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        full_name = f"{pkg_name}.{fname}"
        signature = f"func {fname}({args_str})"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="go")
            ),
        ))
        stats['functions'] += 1

    # Cross-language interactions (writer replays with the module UUID).
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, imports, "Go", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="Go"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_go_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_go_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
