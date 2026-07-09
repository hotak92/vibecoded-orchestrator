# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""JavaScript / TypeScript extractor for the code-graph analyzer
(P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``_js_methods_for_class`` (V52-O.11.F.2-JS per-class method attribution)
and ``CodeGraphAnalyzer._analyze_js_file`` — one extractor serves BOTH the
``javascript`` and ``typescript`` dispatch keys, exactly as the analyzer's
``lang_dispatch`` table wired it. Only body edits: the mechanical ``self.``
-> ``ctx.`` rename and the analyzer-resident ``embed_class`` /
``embed_function`` / ``generate_embedding`` / ``_shape_for_insert`` seams
reached via ``ctx.``. Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vco_lib.codegraph_entities import (
    CodeEntity,
    KIND_API,
    KIND_CLASS,
    KIND_FUNCTION,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    _is_minified_content,
)


def _js_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-JS (v0.2.52, 2026-06-09): extract methods declared
    inside ``class <class_name> { ... }`` (with optional ``extends``) in
    JS/TS source.

    Replaces the pre-V52-O.11.F.2-JS regex (``method_inside`` over
    ``class_body``) which had three correctness defects:

      1. Missed method shapes: ``static name()``, ``get name()``,
         ``set name()``, ``*name()`` (generator), ``#name()`` (private),
         ``static async name()``, ``static *name()``.
      2. Matched ANY ``name(args) {`` pattern, including nested function
         calls inside method bodies (``otherFn(arg) { ... }`` invoked
         inside a method body would be miscounted as a method of the
         outer class).
      3. Incorrectly skipped ``constructor`` (constructor IS a method
         that should be tracked, not filtered).

    Algorithm:

      1. Find every ``class <class_name>`` header (with optional
         ``extends <Base>``). The class name must match EXACTLY — a
         class named ``Foo`` does not match ``FooBar``. Optional
         ``export`` / ``export default`` prefix supported.
      2. For each matched class block, find its closing brace via the
         existing ``_extract_balanced_block`` helper (V52-O.11.E).
      3. Walk the class body brace-by-brace tracking depth. At depth 1
         (immediate body, NOT inside any nested block), scan for
         method declarations matching the JS method-shorthand patterns:
           - ``name(args)``
           - ``async name(args)``
           - ``static name(args)``
           - ``static async name(args)``
           - ``get name()`` / ``set name(v)``
           - ``*name(args)`` (generator)
           - ``async *name(args)`` (async generator)
           - ``#name(args)`` (private field)
           - ``static #name(args)`` / ``#async name`` etc. combinations
      4. Return deduplicated list, preserving first-seen order.

    Returns empty list if no class blocks match — common for classes
    whose body is purely fields/properties without method shorthand.

    Handles ``extends``: ``class Foo extends Bar { ... }`` — the class
    block IS Foo's body, methods of Bar are NOT picked up (Bar is in a
    DIFFERENT class declaration entirely, scoped separately).

    Limitations:
      - Object-literal method shorthand inside method bodies isn't
        confused with class-level methods because of the depth-tracking
        (object literals are at depth>=2 inside any method).
      - Multi-line strings / template literals with unbalanced braces
        may confuse depth tracking — inherits the same constraint as
        ``_extract_balanced_block``. Rare in practice; callers degrade
        gracefully (under-count by 1-2 methods at worst).
      - Decorators (``@decorator``) on a method line are skipped (the
        pattern starts at the method name itself).

    Language coverage: works for both JavaScript (``.js``, ``.mjs``,
    ``.jsx``) and TypeScript (``.ts``, ``.tsx``) — TS class syntax is
    a strict superset (adds type annotations on params + return types)
    and our pattern ignores those by matching only up to the opening
    paren.
    """
    # Find every class header whose name matches class_name exactly.
    # Optional `export` / `export default` prefix; optional `extends Base`.
    # The class name capture group is constrained to NOT be followed by
    # any identifier character so e.g. searching for "Foo" doesn't match
    # `class FooBar`.
    escaped = re.escape(class_name)
    class_pattern = re.compile(
        r"(?:export\s+(?:default\s+)?)?"
        rf"class\s+{escaped}\b"
        r"(?:\s+extends\s+[\w.]+)?"
        r"\s*\{",
        re.MULTILINE,
    )

    # Inner method-shorthand patterns. Matched anchored at the start of
    # the candidate position; collectively cover the JS class method
    # shapes documented above. Each variant captures the method name in
    # group 1.
    #
    # Order matters for the alternation: we test specific (longer)
    # prefixes first so "static async foo" matches as static-async, not
    # static + "async" (would attribute the wrong name).
    method_patterns = [
        # static async generator: `static async *name(...)`
        re.compile(r"static\s+async\s+\*\s*([#\w]+)\s*\("),
        # static generator: `static *name(...)`
        re.compile(r"static\s+\*\s*([#\w]+)\s*\("),
        # static async: `static async name(...)`
        re.compile(r"static\s+async\s+([#\w]+)\s*\("),
        # static get/set: `static get name()` / `static set name(v)`
        re.compile(r"static\s+(?:get|set)\s+([#\w]+)\s*\("),
        # plain static: `static name(...)`
        re.compile(r"static\s+([#\w]+)\s*\("),
        # async generator: `async *name(...)`
        re.compile(r"async\s+\*\s*([#\w]+)\s*\("),
        # async: `async name(...)`
        re.compile(r"async\s+([#\w]+)\s*\("),
        # generator: `*name(...)`
        re.compile(r"\*\s*([#\w]+)\s*\("),
        # get/set: `get name()` / `set name(v)`
        re.compile(r"(?:get|set)\s+([#\w]+)\s*\("),
        # plain shorthand: `name(...)` — must be tested LAST since it
        # would otherwise eat any of the prefixed forms.
        re.compile(r"([#\w]+)\s*\("),
    ]

    # Keywords that match the plain shorthand pattern but are NOT methods.
    # `constructor` is INTENTIONALLY NOT in this list — it IS a method.
    keyword_skip = {
        "if", "else", "while", "for", "switch", "return",
        "class", "new", "catch", "throw", "do", "try",
        "function", "yield", "await", "void", "typeof",
        "instanceof", "in", "of", "case", "break",
        "continue", "delete", "var", "let", "const",
    }

    methods: List[str] = []
    seen: set = set()

    for m in class_pattern.finditer(content_clean):
        # Locate the opening brace position (the regex anchors on it).
        class_open_pos = m.end() - 1  # position of `{`
        class_open_line = content_clean[:class_open_pos].count("\n") + 1
        class_close_line = _extract_balanced_block(
            source_lines, class_open_line, max_lookahead=800
        )
        block_start_pos = class_open_pos + 1  # skip `{`

        # Find char-offset for class_close_line in content_clean. Same
        # newline-counting walker as _rust_methods_for_struct uses.
        target_newlines = class_close_line - class_open_line
        if target_newlines <= 0:
            continue
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        class_body_text = content_clean[block_start_pos:block_end_pos]

        # Walk the class body char-by-char tracking brace depth so we
        # only look for method declarations at depth 0 of the class
        # body (which is depth 1 of the file's brace nesting).
        # Method bodies/blocks at depth >=1 are skipped — this prevents
        # nested function-call statements like `cb(arg) { ... }`
        # inside a method body from being miscounted as class methods.
        depth = 0
        i = 0
        n = len(class_body_text)
        while i < n:
            ch = class_body_text[i]
            if ch == "{":
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth -= 1
                i += 1
                continue
            # Skip strings (single-line only, mirroring the existing
            # content_clean comment-strip pre-pass which removed line +
            # block comments — backtick-delimited template literals
            # remain in content_clean and can throw off depth tracking
            # if they contain unescaped braces, but that's an inherited
            # constraint from V52-O.11.E).
            if ch in ('"', "'"):
                quote = ch
                i += 1
                while i < n and class_body_text[i] != quote:
                    if class_body_text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    i += 1
                i += 1  # skip the closing quote
                continue
            if ch == "`":
                # Template literal — skip naively to matching backtick.
                i += 1
                while i < n and class_body_text[i] != "`":
                    if class_body_text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    i += 1
                i += 1
                continue
            if depth != 0:
                i += 1
                continue
            # At depth 0 of class body — try every method pattern at
            # this position. Use match() (anchored) on the substring
            # from i so we don't have to write start anchors in each
            # regex.
            matched = False
            substr = class_body_text[i:]
            for mp in method_patterns:
                pm = mp.match(substr)
                if not pm:
                    continue
                name = pm.group(1)
                # Filter out reserved keywords that look like methods.
                # (`constructor` deliberately preserved.)
                if name in keyword_skip:
                    break  # don't try lower-priority patterns; this is a keyword
                if name in seen:
                    matched = True
                    i += pm.end()
                    break
                seen.add(name)
                methods.append(name)
                matched = True
                # Advance past the matched prefix so we don't re-match
                # the same method header from a substring position.
                i += pm.end()
                break
            if matched:
                continue
            i += 1

    return methods


def analyze_js_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a JavaScript/TypeScript file using regex-based parsing.

    Extracts imports, functions, classes, Fastify route definitions, and
    external HTTP calls (fetch). Handles both .js/.mjs and .ts/.tsx files.
    """
    stats = {'modules': 0, 'classes': 0, 'functions': 0, 'apis': 0}

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

    # Determine language label from extension
    suffix = file_path.suffix.lower()
    if suffix in ('.ts', '.tsx'):
        language = "TypeScript"
    else:
        language = "JavaScript"

    # Strip single-line and multi-line comments for cleaner pattern matching
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # --- Imports ---
    imports: List[str] = []
    # ES module imports: import { x } from './y' / import x from './y' / import './y'
    for m in re.finditer(r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+)?['"]([^'"]+)['"]""", content):
        imports.append(m.group(1))
    # CommonJS require: require('./y') / require('y')
    for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
        imports.append(m.group(1))

    # --- Classes ---
    class_names: List[str] = []
    class_pattern = re.compile(
        r'(?:export\s+(?:default\s+)?)?class\s+([\w]+)\s*(?:extends\s+([\w.]+)\s*)?{',
        re.MULTILINE
    )
    class_info: Dict[str, Tuple[int, Optional[str]]] = {}  # name -> (start_line, base_class)
    for m in class_pattern.finditer(content_clean):
        cname = m.group(1)
        base = m.group(2)
        start_line = content_clean[:m.start()].count('\n') + 1
        class_info[cname] = (start_line, base)
        class_names.append(cname)

    # --- Functions ---
    # Covers: export async function name(, export function name(,
    #         async function name(, function name(
    func_pattern = re.compile(
        r'(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([\w]+)\s*\(',
        re.MULTILINE
    )
    all_func_names: List[str] = []
    func_matches: List[Tuple[str, int, bool]] = []  # (name, start_line, is_async)
    for m in func_pattern.finditer(content_clean):
        fname = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        # Check if 'async' appears before 'function' in the matched text
        match_text = content_clean[m.start():m.end()]
        is_async = 'async' in match_text
        func_matches.append((fname, start_line, is_async))
        all_func_names.append(fname)

    # Also catch arrow function exports: export const name = async (...) =>
    arrow_pattern = re.compile(
        r'(?:export\s+)?(?:const|let|var)\s+([\w]+)\s*=\s*(async\s+)?(?:\([^)]*\)|[\w]+)\s*=>',
        re.MULTILINE
    )
    for m in arrow_pattern.finditer(content_clean):
        fname = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        is_async = m.group(2) is not None
        func_matches.append((fname, start_line, is_async))
        all_func_names.append(fname)

    # --- Fastify route definitions ---
    # Pattern: { secure: true/false, method: 'POST', url: '/tx/build', handler, schema }
    # May span multiple lines
    route_pattern = re.compile(
        r'\{[^}]*method:\s*[\'"](\w+)[\'"][^}]*url:\s*[\'"]([^\'"]+)[\'"][^}]*\}',
        re.DOTALL
    )
    routes: List[Dict[str, Any]] = []
    for m in route_pattern.finditer(content):
        route_block = m.group(0)
        method = m.group(1).upper()
        url = m.group(2)
        # Extract secure flag
        secure_match = re.search(r'secure:\s*(true|false)', route_block)
        secure = secure_match.group(1) == 'true' if secure_match else False
        # Extract handler reference
        handler_match = re.search(r'handler:\s*([\w.]+)', route_block)
        handler_ref = handler_match.group(1) if handler_match else None
        routes.append({
            'method': method,
            'url': url,
            'secure': secure,
            'handler': handler_ref,
        })

    # --- External HTTP calls (fetch) ---
    # Match: fetch(`${EXAMPLE_API_URL}/api/tx/build`, ...) or fetch('http://example:8000/api/somepath', ...)
    external_calls: List[str] = []
    # Template literal with env var prefix: fetch(`${VAR}/path/here`...)
    for m in re.finditer(r'fetch\s*\(\s*`\$\{[\w]+\}(/[^`]*)`', content):
        external_calls.append(m.group(1))
    # Literal URL with protocol: fetch('http://host:port/path'...)
    for m in re.finditer(r"""fetch\s*\(\s*['"]https?://[^/'"]*(/[^'"]*?)['"]""", content):
        external_calls.append(m.group(1))
    # Template literal without env var but with http prefix: fetch(`http://host:port/path`...)
    for m in re.finditer(r'fetch\s*\(\s*`https?://[^/`]*(\/[^`]*?)`', content):
        path = m.group(1)
        if path not in external_calls:
            external_calls.append(path)

    # --- Module summary ---
    first_comments: List[str] = []
    for line in source_lines[:15]:
        s = line.strip()
        if s.startswith('//'):
            first_comments.append(s.lstrip('/').strip())
        elif s.startswith('*') and not s.startswith('*/'):
            first_comments.append(s.lstrip('*').strip())
        elif s and not s.startswith('/*'):
            break

    summary_parts = [f"{language} module: {relative_path}"]
    if first_comments:
        summary_parts.append(' '.join(first_comments[:3]))
    if class_names:
        summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
    if all_func_names:
        summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
    if routes:
        route_strs = [f"{r['method']} {r['url']}" for r in routes[:5]]
        summary_parts.append(f"Routes: {', '.join(route_strs)}")
    if external_calls:
        summary_parts.append(f"External calls: {', '.join(external_calls[:5])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if (', 'if(', 'else if', '? ', 'while (', 'while(',
                                          'for (', 'for(', 'switch (', 'switch(', 'catch (', 'catch(']))

    module_uuid = ctx._create_or_update_module(
        path=relative_path, language=language, loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    stats['modules'] = 1

    # --- Store classes ---
    for cname, (start_line, base_class) in class_info.items():
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 80)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        # V52-O.11.F.2-JS (v0.2.52, 2026-06-09): scope `methods` to
        # method declarations inside this class's brace-balanced body
        # (with full method-shape coverage: static, get/set, generators,
        # private, async + combinations — plus depth-tracking that
        # prevents nested call statements like `cb(x) { ... }` inside
        # method bodies from being miscounted as class methods).
        # Pre-V52-O.11.F.2-JS this inlined a `method_inside.finditer`
        # over `class_body` with an under-covering pattern; the new
        # helper mirrors the V52-O.11.F Rust pattern (parallel fix
        # for JS/TS class method attribution).
        methods = _js_methods_for_class(content_clean, cname, source_lines)

        signature = f"class {cname}"
        if base_class:
            signature += f" extends {base_class}"


        ctx.store_entity(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname,
            full_name=f"{file_path.stem}.{cname}",
            body=class_body,
            signature=signature,
            doc="",
            start_line=start_line,
            end_line=start_line + len(class_lines),
            project=ctx.project_name,
            extras={"methods": methods[:20]},
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_class(signature, class_body, methods=methods[:10], language="javascript"),
        ))
        stats['classes'] += 1

    # --- Store functions ---
    for fname, start_line, is_async in func_matches:
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        full_name = f"{file_path.stem}.{fname}"
        signature = f"{'async ' if is_async else ''}function {fname}()"

        ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname,
            full_name=full_name,
            body=body,
            signature=signature,
            doc="",
            start_line=start_line,
            end_line=end_line,
            is_async=is_async,
            project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(signature, body, language="javascript"),
        ))
        stats['functions'] += 1

    # --- Store Fastify routes as CodeAPI ---
    for route in routes:
        # Check if route handler calls fetch to a known path (proxy detection)
        proxy_target = None
        if route['handler'] and external_calls:
            # Simple heuristic: if there are external calls in the same file and
            # the route URL has a matching path segment, link them
            for ext_call in external_calls:
                # If external call path matches or contains the route URL
                if route['url'] in ext_call or ext_call.endswith(route['url']):
                    proxy_target = ext_call
                    break

        description = (
            f"{route['method']} {route['url']} "
            f"({'authenticated' if route['secure'] else 'public'})"
        )
        if route['handler']:
            description += f" -> {route['handler']}"
        if proxy_target:
            description += f" [proxies to {proxy_target}]"

        embedding = ctx.generate_embedding(description)

        ctx.store_entity(CodeEntity(
            kind=KIND_API, file_path_rel=relative_path,
            extras={
                "endpoint": route['url'],
                "method": route['method'],
                "api_description": description,
                "parameters": [],
                "returns": "",
                "project": ctx.project_name,
                "proxy_target": proxy_target or "",
            },
            vector=ctx._shape_for_insert(embedding) if embedding else None,
        ))
        stats['apis'] += 1

    # Cross-language interactions
    ix = _extract_external_calls(content_clean, imports, language, relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, language, module_uuid, file_path_rel=relative_path)

    return stats
