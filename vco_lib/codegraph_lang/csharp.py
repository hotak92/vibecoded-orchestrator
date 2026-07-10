# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""C# extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``_csharp_methods_for_class`` (V52-O.11.F.2-CSHARP per-class method
attribution) and ``CodeGraphAnalyzer._analyze_csharp_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
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

from vco_lib.codegraph_entities import CodeEntity, KIND_API, KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    _is_minified_content,
)


def _csharp_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): extract methods declared
    inside the lexical body of a C# ``class``, ``struct``, ``record``, or
    ``interface`` named ``class_name``.

    Replaces the pre-V52-O.11.F.2-CSHARP line at ``analyze_code_graph.py:2540``
    that ran ``method_pattern.finditer(content_clean)`` unconditionally —
    the same bug Rust V52-O.11.F closed, applied to the C# parser. Audit
    a79152 confirmed: a 50-method file with 3 classes produced 150
    incorrect method attributions per the
    ``query_code_structure(methods, ClassName)`` MCP path.

    C# method-scoping model (lexical, like Java):

      Methods live between a type's opening ``{`` and its matching close.
      Unlike Rust there is no separate ``impl`` block — declarations are
      lexically inside the class body. Same shape as Java, but with C#
      specifics:
        * Modifiers: ``public``, ``private``, ``protected``, ``internal``,
          ``protected internal``, ``private protected``, ``static``,
          ``virtual``, ``override``, ``sealed``, ``abstract``, ``async``,
          ``extern``, ``partial``, ``unsafe``, ``readonly`` (on structs).
        * Properties (``public int Foo { get; set; }``) — we INCLUDE the
          property name in the methods list. Rationale: mirrors how the
          Python parser counts ``@property`` decorated members, and the
          underlying ``get_Foo`` / ``set_Foo`` are real methods at the
          CLR level. The task brief calls this out explicitly.
        * Indexers (``public T this[int i] { get; set; }``) — surfaced
          as ``Item`` per the CLR property convention (``get_Item`` /
          ``set_Item``).
        * Records: ``record Foo(int X, int Y)`` — the positional
          parameters generate auto-property accessors. We do NOT try to
          extract those from the primary-constructor signature; only
          explicit declarations inside the record body land in methods.
        * Async methods (``async Task<T> Foo()``) — picked up.
        * Generic methods (``T Method<U>(U arg)``) — picked up.
        * Partial classes (``partial class Foo { ... } partial class Foo
          { ... }``) — every matching declaration's body contributes; the
          union is returned (mirrors Rust's multi-``impl`` behavior).

    Algorithm:

      1. Locate every class/struct/record/interface declaration whose
         name matches ``class_name``. Multiple declarations are allowed
         (partial classes). The class header regex tolerates the same
         modifier mix as the parser's outer ``class_pattern`` plus an
         optional inheritance clause (``: BaseClass, IInterface``).
      2. For each match, use ``_extract_balanced_block`` to find the
         body's closing brace (already brace-balanced via V52-O.11.E).
      3. Scan the body text (sliced from ``content_clean``, NOT
         ``source_lines`` — keeps the same comment-stripped surface the
         old per-file regex used) for:
            a. Method declarations: ``[modifiers] [returntype] Name(args) {`` or
               ``[modifiers] [returntype] Name(args) =>`` (expression-bodied)
               or ``[modifiers] [returntype] Name(args);`` (abstract /
               interface).
            b. Property declarations: ``[modifiers] [type] Name { get; set; }``
               (no parens after the name) — the body starts directly with
               ``{`` containing ``get``/``set``.
            c. Indexer declarations: ``[modifiers] [type] this[...] {`` —
               normalized to the literal ``Item``.
      4. Filter out C# keywords that the method-regex could otherwise
         hit (``if``, ``while``, ``for``, ``foreach``, ``switch``, ``try``,
         ``catch``, ``return``, ``new``, ``throw``, ``using``, ``lock``,
         ``yield``).
      5. Return deduplicated list, preserving first-seen order.

    Returns empty list if no class/struct/record/interface body matches
    ``class_name`` — common when ``class_name`` was extracted from a
    forward-declaration or a partial class whose other halves live in
    different files.

    Limitations:
      * Constructors are extracted as methods (their "name" matches the
        class name). The existing Java/JS parsers behave the same way;
        the embedding pipeline doesn't distinguish.
      * Static constructors (``static Foo() { ... }``) are also captured.
      * Operator overloads (``public static Foo operator+(Foo a, Foo b)``)
        are NOT captured — the name after ``operator`` isn't a valid
        identifier. This matches the upstream parser's behavior.
      * Nested types inside the class body are correctly scoped via
        brace-balance: members of nested types fall inside the outer
        class's braces but ALSO inside the nested type's own braces, so
        we'd double-count them if not careful. We handle this by
        recognizing that the nested type's header itself doesn't match
        the method pattern, but its INNER members do — so a nested class
        ``Inner`` inside ``Outer`` would leak ``Inner``'s members into
        ``Outer``'s methods. We accept this minor over-count for now
        (matches Java's behavior); a tree-sitter rewrite is the right
        fix (queued as V52-O.11.G).
      * Doesn't follow ``partial`` declarations across files (each file
        is parsed independently — same as Java's package-private split).
    """
    escaped = re.escape(class_name)
    # Class/struct/record/interface header. Modifiers + keyword + name +
    # optional generics + optional inheritance + opening brace. The name
    # capture is anchored by the `escaped` literal so we only match THE
    # target class (not other classes that happen to start with the same
    # prefix).
    #
    # Why not reuse the parser's outer `class_pattern`? That one captures
    # the name into group(1) for class-info population. Here we need to
    # gate on a SPECIFIC name and find the body opener — a different shape.
    class_header_pattern = re.compile(
        # Optional modifiers (zero or more, repeated). C# allows any order.
        r"(?:(?:public|private|protected|internal|abstract|sealed|partial|"
        r"static|unsafe|readonly|ref)\s+)*"
        # Keyword introducing the type
        r"(?:class|struct|record|interface)\s+"
        # The target name (exact match — escaped). Trailing word boundary
        # prevents `Stress` from matching `Stress2` (a strict-prefix
        # superset name). Critical: without `\b` the regex matches BOTH
        # the literal name AND any longer name starting with it, causing
        # body extraction to fall through to the next class's brace.
        rf"{escaped}\b"
        # Optional generic parameters: <T>, <T, U>, <T : IFoo>
        r"(?:\s*<[^>]*>)?"
        # Optional primary-constructor parameter list (record Foo(int X, int Y))
        r"(?:\s*\([^)]*\))?"
        # Optional pre-body trailer: covers BOTH the inheritance clause
        # (`: Base, IFoo<T>`) AND the generic constraints (`where T : new()`).
        # We accept any sequence of non-`{` chars; the `where` clause's
        # own `()` and `<>` are safely consumed because the only stopping
        # condition is the opening brace. This single permissive trailer
        # handles all of:
        #     class Foo : Base { ... }
        #     class Foo<T> where T : new() { ... }
        #     class Foo<T> : Base where T : new() { ... }
        #     class Foo<T, U> where T : class where U : struct, new() { ... }
        r"[^{]*"
        # Opening brace
        r"\{",
        re.MULTILINE,
    )

    # Inner method/property/indexer pattern. Three shapes packed into one
    # alternation so a single pass over the class body collects all three.
    #
    # Strategy:
    #   * Anchor on start-of-line (re.MULTILINE + `^[ \t]*`) — declarations
    #     in C# are always on their own line, statements inside methods
    #     are indented further or follow other statements.
    #   * Modifiers are OPTIONAL because (a) interface members have no
    #     explicit modifier prior to C# 8 default-interface-methods, and
    #     (b) struct members can also be implicit-private.
    #   * To compensate for optional modifiers, we filter out matches whose
    #     captured name is a C# control-flow keyword (`if`, `return`,
    #     `new`, etc.) — that catches the common false-positive shapes
    #     like `return new U();` and `where U : T, new()`.
    #   * We also filter out matches where the supposed return type IS
    #     itself a control-flow keyword (`return`, `throw`, etc.) — that
    #     filters `return new U();` more aggressively.
    #
    # Shape M (method):
    #   ^[ws] [modifier]* [returntype] Name(args) ( { | => | ; )
    # Shape P (property):
    #   ^[ws] [modifier]* [type] Name { get ...|set ...|init ... }
    # Shape I (indexer):
    #   ^[ws] [modifier]* [type] this[args] {
    method_modifier_alt = (
        r"(?:public|private|protected|internal|static|virtual|override|"
        r"async|abstract|sealed|extern|partial|new|unsafe|readonly)"
    )
    # Zero or more modifier tokens (with whitespace between). Optional
    # so interface methods (no modifier) and implicit-private members
    # still match. We compensate via the keyword filter below.
    method_modifiers_opt = rf"(?:{method_modifier_alt}\s+)*"
    # Compact type pattern — matches type-shaped tokens like:
    #   T, int, string, Task, Task<int>, IList<T>, T[], int?, Dictionary<K,V>,
    #   IList<KeyValuePair<string, object>>  (nested generics)
    # The `\w` alternative handles single-letter generic params; the
    # longer alternative handles compound generics.
    #
    # CRITICAL: type_shape MUST NOT allow whitespace at the top level,
    # only inside angle brackets. Otherwise `return new U(` matches with
    # type_shape spanning `return new` (taking the whitespace) and name
    # being `U` — false positive. We allow whitespace only via nested
    # generic groups that swallow arbitrary text including commas/spaces.
    #
    # Nested generics: Python regex doesn't support true recursion, but we
    # can hand-roll a 3-level nested-generic pattern that covers all
    # practical cases. Format builds bottom-up:
    #   level0: <...> with no nested angles
    #   level1: <... level0 ...> — one level of nesting
    #   level2: <... level1 ...> — two levels of nesting (e.g.
    #     IList<KeyValuePair<string, object>>)
    #   level3: <... level2 ...> — three levels (e.g.
    #     Task<Dictionary<int, List<string>>>)
    # A 4th level (Task<Dictionary<int, List<Dictionary<...>>>>) is
    # exceedingly rare and falls back to graceful failure (method not
    # captured; doesn't break the parser).
    _ang_lvl0 = r"<[^<>]*>"
    _ang_lvl1 = rf"<(?:[^<>]|{_ang_lvl0})*>"
    _ang_lvl2 = rf"<(?:[^<>]|{_ang_lvl1})*>"
    _generic_block = _ang_lvl2
    type_shape = (
        # First char: word or dot
        r"[\w.]"
        # Then any mix of:
        #   * word/dot/?/[]
        #   * nested generic block (covers up to 2 levels of nesting)
        rf"(?:[\w.\[\]\?]|{_generic_block})*"
    )
    method_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Method name (captured)
        + r"([\w]+)"
        # Optional generic type parameters on the method
        + r"(?:\s*<[^>]*>)?"
        # Argument paren (anchor)
        + r"\s*\(",
        re.MULTILINE,
    )
    property_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Property name (captured) followed by `{` (NOT `(`)
        + r"([\w]+)\s*\{"
        # Lookahead for accessor keyword to confirm this is a property
        + r"(?=\s*(?:[\[\w]|//|/\*)*\s*(?:get|set|init)\b)",
        re.MULTILINE,
    )
    indexer_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        + type_shape + r"\s+"
        + r"(this)\s*\[[^\]]*\]\s*\{",
        re.MULTILINE,
    )

    # Tokens that look like an identifier in capture position but are
    # actually C# control-flow keywords. Filter these out.
    _CSHARP_KW_FILTER = {
        "if", "else", "while", "for", "foreach", "switch", "try", "catch",
        "finally", "return", "new", "throw", "using", "lock", "yield",
        "do", "break", "continue", "goto", "case", "default", "checked",
        "unchecked", "fixed", "stackalloc", "await", "is", "as", "in",
        "out", "ref", "params", "where", "when", "var", "true", "false",
        "null", "this", "base", "typeof", "sizeof", "nameof",
    }

    methods: List[str] = []
    seen: set = set()
    for hdr in class_header_pattern.finditer(content_clean):
        # Locate the opening brace position (regex anchors on it).
        # hdr.end() is one past `{`; the brace itself is at end()-1.
        body_open_pos = hdr.end() - 1
        body_open_line = content_clean[:body_open_pos].count("\n") + 1
        body_close_line = _extract_balanced_block(
            source_lines, body_open_line, max_lookahead=800
        )
        # Convert close-line back to a char offset in content_clean by
        # counting newlines from body_open_pos onward.
        target_newlines = body_close_line - body_open_line
        if target_newlines <= 0:
            continue
        block_start_pos = body_open_pos + 1  # skip the `{`
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        body = content_clean[block_start_pos:block_end_pos]

        # Indexers FIRST — the indexer regex captures the literal `this`
        # which we map to `Item`. Doing this before the method regex
        # avoids the method regex over-matching on `this(` constructor
        # chains (rare, but defensive).
        for im in indexer_decl.finditer(body):
            if "Item" in seen:
                continue
            seen.add("Item")
            methods.append("Item")

        # Methods second.
        for mm in method_decl.finditer(body):
            name = mm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

        # Properties third — the property regex is anchored on `{` with
        # no preceding `(`, so it doesn't double-match methods.
        for pm in property_decl.finditer(body):
            name = pm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

    return methods


def analyze_csharp_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a C# file using regex-based parsing.

    Extracts using directives, classes, interfaces, methods, and ASP.NET route attributes.
    Also populates CodeAPI for [HttpGet/Post/Put/Delete/Patch] annotated methods.
    """
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
               if l.strip() and not l.strip().startswith('//')
               and not l.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # using directives
    imports = re.findall(r'^\s*using\s+([\w.]+)\s*;', content, re.MULTILINE)

    # namespace
    ns_match = re.search(r'namespace\s+([\w.]+)', content)
    ns = ns_match.group(1) if ns_match else file_path.stem

    # Classes / interfaces / records
    class_pattern = re.compile(
        r'(?:public|private|protected|internal|abstract|sealed|partial|\s)+'
        r'(?:class|interface|record|struct)\s+([\w<>, ]+?)(?:\s*:\s*[\w<>, ]+?)?\s*\{',
        re.MULTILINE
    )
    class_info: Dict[str, int] = {}
    for m in class_pattern.finditer(content_clean):
        raw = m.group(1).strip().split('<')[0].strip()  # strip generics
        if not raw or raw[0].islower():
            continue
        start_line = content_clean[:m.start()].count('\n') + 1
        class_info[raw] = start_line

    # Methods: access modifier + return type + name(...)
    method_pattern = re.compile(
        r'(?:public|private|protected|internal|static|virtual|override|async|abstract|\s)+'
        r'(?:[\w<>\[\]?]+\s+)+([\w]+)\s*\([^)]*\)\s*(?:\{|=>|;)',
        re.MULTILINE
    )

    # Route attribute (base route on controller or per-method)
    route_attr_pattern = re.compile(r'\[Route\s*\(\s*["\']([^"\']+)["\']')

    # Module summary
    file_comment = ''
    for line in source_lines[:20]:
        s = line.strip()
        if s.startswith('///') or s.startswith('//'):
            file_comment = s.lstrip('/').strip()
            break
    summary_parts = [f"C# module: {relative_path} (namespace {ns})"]
    if file_comment:
        summary_parts.append(file_comment)
    if class_info:
        summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if (', 'while (', 'for (', 'foreach (', 'switch (', 'catch (']))

    module_uuid = ctx._create_or_update_module(
        path=relative_path, language="C#", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    stats['modules'] = 1

    # Classes
    for cname, start_line in class_info.items():
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        # V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): scope `methods` to
        # method declarations INSIDE `class <cname> { ... }` (also struct,
        # record, interface). Pre-V52-O.11.F.2 this line ran
        # `method_pattern.finditer(content_clean)` over the WHOLE file —
        # attributing EVERY method to EVERY class. Same antipattern as
        # V52-O.11.F (Rust). Audit a79152.
        methods = _csharp_methods_for_class(content_clean, cname, source_lines)
        signature = f"class {cname}"
        ctx.store_entity(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{ns}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=start_line + len(class_lines),
            project=ctx.project_name,
            extras={"methods": methods[:20]},
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_class(signature, class_body, methods=methods[:10], language="csharp"),
        ))
        stats['classes'] += 1

    # Methods
    for m in method_pattern.finditer(content_clean):
        mname = m.group(1)
        if mname in ('if', 'while', 'for', 'foreach', 'switch', 'catch', 'try', 'return', 'new', 'throw'):
            continue
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        enclosing = next(
            (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
             if cl <= start_line), file_path.stem
        )
        is_async = bool(re.search(r'\basync\b', body[:200]))
        full_name = f"{ns}.{enclosing}.{mname}"
        signature = f"{mname}(...)"
        func_uuid = ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=mname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=is_async, project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(signature, body, language="csharp"),
        ))
        stats['functions'] += 1

        # ASP.NET route entries for HTTP-attributed methods
        # Check if this method has an [Http*] attribute in the lines just above it
        pre_lines = source_lines[max(0, start_line - 5):start_line]
        pre_ctx = '\n'.join(pre_lines)
        http_m = re.search(r'\[Http(Get|Post|Put|Delete|Patch|Options|Head)', pre_ctx, re.IGNORECASE)
        if http_m:
            http_method = http_m.group(1).upper()
            # Extract route from attribute or from class [Route] base
            route_m = re.search(r'\[Http\w+\s*\(\s*["\']([^"\']+)["\']', pre_ctx)
            route = route_m.group(1) if route_m else f"/{mname.lower()}"
            # Base controller route
            ctrl_route = ''
            base_route_m = route_attr_pattern.search(content_clean[:m.start()])
            if base_route_m:
                ctrl_route = '/' + base_route_m.group(1).strip('/')
            full_route = ctrl_route + ('/' if ctrl_route else '') + route.lstrip('/')
            api_desc = f"C# ASP.NET {http_method} {full_route} → {ns}.{enclosing}.{mname}"
            api_embedding = ctx.generate_embedding(api_desc)
            ctx.store_entity(CodeEntity(
                kind=KIND_API, file_path_rel=relative_path,
                extras={
                    "endpoint": full_route, "method": http_method,
                    "api_description": api_desc,
                    "parameters": [], "returns": "",
                    "project": ctx.project_name, "proxy_target": "",
                },
                references={"handler": func_uuid},
                vector=ctx._shape_for_insert(api_embedding) if api_embedding else None,
            ))
            stats.setdefault('apis', 0)
            stats['apis'] += 1

    # Cross-language interactions
    ix = _extract_external_calls(content_clean, imports, "csharp", relative_path)
    if ix:
        stats['interactions'] = ctx._store_interactions(ix, "C#", module_uuid, file_path_rel=relative_path)

    return stats
