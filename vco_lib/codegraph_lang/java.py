# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Java extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``_java_methods_for_class`` (V52-O.11.F.2-JAVA per-class method
attribution, + its ``_strip_nested_java_classes`` / ``_find_matching_brace``
helpers) and ``CodeGraphAnalyzer._analyze_java_file`` — the move itself was verbatim apart from
the mechanical ``self.`` -> ``ctx.`` rename (``ctx`` IS the analyzer
instance) and the analyzer-resident embedding seams reached via ``ctx.``.
Behaviour has since been CORRECTED here (v0.2.92 and WP-5b — see the notes
below), so it is no longer byte-identical to the analyzer's original;
``tests/test_codegraph_golden.py`` pins what it does TODAY, and the
corpus README explains why a snapshot is evidence of behaviour rather
than of correctness.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    blank_block_comments_preserving_lines,
    run_pure_extractor,
    skip_leading_whitespace,
)

# v0.2.92 WP-5b — tokens that may not appear in a member declaration's
# MODIFIER / RETURN-TYPE run. The mirror of C#'s ``_CSHARP_NON_DECL_TOKENS``,
# needed for the SAME reason and introduced by the SAME change: once
# ``method_pattern`` accepts ``;`` as a terminator (so that interface and
# abstract methods finally produce a row), it also matches ordinary
# STATEMENTS that end in one —
#
#     new Thread(runnable);        → run ``new``,    name ``Thread``
#     return format(x);            → run ``return``, name ``format``
#     throw wrap(e);               → run ``throw``,  name ``wrap``
#
# In each case the captured NAME is a valid identifier, so a keyword filter on
# the name cannot see the problem; the tell is the run. ``new`` is here and is
# NOT in the C# set, because C# has a legal ``public new int F()`` member
# modifier and Java has no such form.
_JAVA_NON_DECL_TOKENS = frozenset({
    # statement keywords
    "return", "throw", "new", "case", "assert", "yield",
    # type-declaration keywords — a type is not a method
    "class", "interface", "enum", "record", "package", "import",
})


def _java_declaration_run_is_a_member(
    content_clean: str, decl_pos: int, name_pos: int
) -> bool:
    """True when ``content_clean[decl_pos:name_pos]`` is a member's modifier /
    return-type run rather than a statement. Mirrors
    ``csharp._csharp_declaration_run_is_a_member``."""
    return not (_JAVA_NON_DECL_TOKENS & set(
        re.findall(r"[A-Za-z_]\w*", content_clean[decl_pos:name_pos])
    ))


def _java_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-JAVA (v0.2.52, 2026-06-09): extract methods declared
    inside the body of ``class <class_name> { ... }`` (or interface/enum
    with the same name).

    Replaces the pre-V52-O.11.F.2 line that ran
    ``method_pattern.finditer(content_clean)`` unconditionally inside the
    per-class loop — that attributed EVERY method in the file to EVERY
    class. Same antipattern as the Rust bug fixed in V52-O.11.F; audit
    a79152 flagged it across Rust, Go, JS/TS, Java, and C# parsers.

    Java's method-scoping model is lexical (like JS): methods are declared
    inside the class braces themselves. So we:

      1. Find the ``class <class_name>`` declaration line (handling
         optional generics on the class, ``extends Parent``, ``implements
         Iface1, Iface2`` clauses).
      2. Use the existing ``_extract_balanced_block`` helper to find the
         matching close brace (V52-O.11.E, brace-balanced).
      3. Slice the class body. Strip nested class/interface/enum blocks
         from the body so methods declared in inner classes don't leak
         into the outer class's method list.
      4. Scan the stripped body for method declarations and collect names.

    Bug #2 — package-private capture (audit a79152):
      The pre-V52-O.11.F.2 method regex used ``(?:modifiers|\\s)+`` with
      a ``+`` quantifier, requiring AT LEAST ONE modifier keyword (or
      whitespace). Package-private methods (no visibility modifier,
      common in Java for package-scoped helpers) were silently missed.
      This helper's inner method pattern uses ``*`` instead, making the
      modifier section optional.

    Java method signature shape::

        [modifiers] [<generics>] return_type method_name(args) [throws ...] {

    Modifiers handled: public, private, protected, static, final,
    abstract, synchronized, native, default, strictfp. ``@Annotation``
    lines preceding a method are stripped from the per-line scrub by
    ``_scrub_for_brace_balance`` indirectly (the regex is anchored on the
    method-shape tail, so annotation lines simply don't match).

    Returns deduplicated list of method names, preserving first-seen
    order. Returns empty list if no class with that name is found, or if
    the class has no methods.

    Excludes:
      - Control-flow keywords that look like method calls (``if``,
        ``while``, ``for``, ``switch``, ``catch``, ``try``, ``else``,
        ``return``, ``synchronized`` as a statement).
      - Constructors (no return type — the regex requires at least one
        return-type token before the method name, matching the pre-fix
        behavior at line 3675).
      - Methods declared in nested classes (stripped before scanning).

    Limitations:
      - Doesn't handle multiple classes with the same name in different
        scopes (extremely rare in Java — only legal in nested-class
        scope, and the nested-class stripping handles the common case).
      - Multi-line generic type parameters (``Map<String,\\nList<...>>``
        on a method return type) may confuse the line-anchored regex;
        these are uncommon.
    """
    escaped = re.escape(class_name)
    # `class|interface|enum` declaration with optional generics on the
    # class, optional `extends`, optional `implements`. Anchor on `{` so
    # m.end() points to the position of the opening brace + 1.
    #
    # Generics handling: Java supports nested generics on the class
    # decl (`class Service<T extends Comparable<T>>`). A simple
    # `<[^>]*>` would fail because it stops at the first `>`. We accept
    # one level of nesting via `<[^<>]*(?:<[^<>]*>[^<>]*)*>` which
    # matches outermost-balanced generic blocks. Two+ levels of nesting
    # (e.g. `Map<String, List<Map<K,V>>>` as a TYPE PARAMETER bound) is
    # rare in class-decl position and falls back to the legacy
    # behavior — methods would simply be missed for those exotic
    # declarations.
    _GENERIC_BLOCK = r"<[^<>]*(?:<[^<>]*>[^<>]*)*>"
    class_decl_pattern = re.compile(
        # `class` | `interface` | `enum` keyword
        r"\b(?:class|interface|enum)"
        # Whitespace + the target class name (escaped)
        rf"\s+{escaped}"
        # Optional generic parameters on the class declaration (allows
        # one level of nested generics).
        rf"(?:\s*{_GENERIC_BLOCK})?"
        # Optional `extends Parent` clause (Parent may be generic)
        r"(?:\s+extends\s+[\w.<>,\s]+?)?"
        # Optional `implements Iface1, Iface2<T>` clause
        r"(?:\s+implements\s+[\w.<>,\s]+?)?"
        # Whitespace + opening brace
        r"\s*\{",
        re.MULTILINE,
    )

    # Inner method pattern. Modifiers section is OPTIONAL (`*` not `+`)
    # so package-private methods are captured. After the optional
    # modifiers/annotations section we require:
    #   - optional method-level generics: <T>, <T extends Foo>
    #   - return type token(s) (1+ — excludes constructors)
    #   - method name (captured)
    #   - argument list (captured for parity with line-3694 functions)
    #   - optional `throws Foo, Bar`
    #   - opening brace
    method_pattern_inner = re.compile(
        # Optional modifiers / annotations / whitespace prefix. Note this
        # is OPTIONAL — package-private methods have no modifier.
        r"(?:(?:public|private|protected|static|final|synchronized|"
        r"native|abstract|default|strictfp)\s+)*"
        # Optional method-level generic parameters: `<T>`, `<T extends Foo>`,
        # `<K, V>`. Greedy on the inside.
        r"(?:<[^>]*>\s+)?"
        # Return-type token(s). Requires at least one — this is what
        # excludes constructors (which have no return type). Generics +
        # array brackets allowed.
        r"(?:[\w<>\[\],\s]+\s+)"
        # Method name
        r"([\w]+)\s*"
        # Argument list
        r"\(([^)]*)\)\s*"
        # Optional throws clause
        r"(?:throws\s+[\w.,\s]+)?\s*"
        # v0.2.92 WP-5b: `;` accepted alongside `{`. It used to be `\{` alone,
        # with a comment claiming abstract methods were "intentionally captured
        # only when they have a body" — which made this list DISAGREE WITH THE
        # GRAPH the moment the entity loop started emitting a row for a
        # bodiless declaration. An interface's `methods` list was empty for
        # every interface in every project.
        r"(?:\{|;)",
        re.MULTILINE,
    )

    methods: List[str] = []
    seen: set = set()
    for m in class_decl_pattern.finditer(content_clean):
        # m.end() is just past the `{`. Find the matching close.
        class_open_pos = m.end() - 1  # position of `{`
        class_open_line = content_clean[:class_open_pos].count("\n") + 1
        class_close_line = _extract_balanced_block(
            source_lines, class_open_line, max_lookahead=2000, language="java"
        )
        target_newlines = class_close_line - class_open_line
        if target_newlines <= 0:
            continue
        # Find the byte offset for the close line by counting newlines.
        block_start_pos = class_open_pos + 1
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        class_body = content_clean[block_start_pos:block_end_pos]

        # Strip nested class/interface/enum blocks so their methods don't
        # leak into the outer class. We iteratively find every nested
        # declaration and excise its balanced body.
        stripped_body = _strip_nested_java_classes(class_body)

        for fm in method_pattern_inner.finditer(stripped_body):
            name = fm.group(1)
            # Skip control-flow keywords that pattern-match like methods.
            if name in (
                "if", "while", "for", "switch", "catch", "try", "else",
                "return", "synchronized", "do", "throw",
            ):
                continue
            # v0.2.92 WP-5b: and skip a STATEMENT whose captured name is a
            # perfectly good identifier (`new Thread(runnable);` → `Thread`),
            # which only became matchable when `;` joined the terminator set.
            # The run is taken from the START OF THE DECLARATION'S OWN LINE
            # rather than from `fm.start()`: this pattern's return-type group
            # includes `\s`, so a raw match can begin many lines earlier and
            # sweep an unrelated `return` into the run, rejecting a real
            # method. A declaration split across lines therefore gets a short
            # run and is ACCEPTED — the fail-safe direction for a methods list.
            line_start = stripped_body.rfind("\n", 0, fm.start(1)) + 1
            if not _java_declaration_run_is_a_member(
                stripped_body, line_start, fm.start(1)
            ):
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)
    return methods


def _strip_nested_java_classes(body: str) -> str:
    """Remove nested ``class|interface|enum <Name> { ... }`` blocks from
    ``body`` so methods declared in inner classes don't leak into the
    outer class's method list.

    Iteratively scans ``body`` for nested class/interface/enum
    declarations. For each match, walks the body character-by-character
    counting braces (with a small string-literal state machine to skip
    braces inside Java string/char literals) to find the matching close
    brace, then excises the entire nested span (from the ``class``
    keyword through the closing ``}``) replacing it with a single space.

    The caller (``_java_methods_for_class``) passes ``content_clean``
    where line + block comments are already stripped, so only string
    literals can confuse the brace counter — handled by the state machine.

    Returns the body with each nested block excised. The outer-class
    method scan operates on the result.
    """
    _GENERIC_BLOCK = r"<[^<>]*(?:<[^<>]*>[^<>]*)*>"
    nested_decl_pattern = re.compile(
        r"\b(?:class|interface|enum)\s+\w+"
        rf"(?:\s*{_GENERIC_BLOCK})?"
        r"(?:\s+extends\s+[\w.<>,\s]+?)?"
        r"(?:\s+implements\s+[\w.<>,\s]+?)?"
        r"\s*\{",
    )
    out_parts: List[str] = []
    cursor = 0
    body_len = len(body)
    while cursor < body_len:
        m = nested_decl_pattern.search(body, cursor)
        if not m:
            out_parts.append(body[cursor:])
            break
        decl_start = m.start()
        decl_open_brace_pos = m.end() - 1  # position of `{`
        out_parts.append(body[cursor:decl_start])
        close_pos = _find_matching_brace(body, decl_open_brace_pos)
        if close_pos < 0:
            # Unbalanced — append the rest verbatim and stop.
            out_parts.append(body[decl_start:])
            cursor = body_len
            break
        # Replace the nested span with a single space.
        out_parts.append(" ")
        cursor = close_pos + 1
    return "".join(out_parts)


def _find_matching_brace(s: str, open_pos: int) -> int:
    """Return the position of the brace that matches the ``{`` at
    ``open_pos``, or -1 if unbalanced.

    Uses a per-character state machine that skips braces inside Java
    string literals (``"..."``) and char literals (``'...'``). Assumes
    comments have already been stripped by the caller (the Java parser
    does this before calling ``_java_methods_for_class``).
    """
    if open_pos >= len(s) or s[open_pos] != "{":
        return -1
    counter = 0
    in_string: Optional[str] = None  # which quote char opened
    i = open_pos
    n = len(s)
    while i < n:
        ch = s[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Escaped char — skip both.
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            counter += 1
        elif ch == "}":
            counter -= 1
            if counter == 0:
                return i
        i += 1
    return -1


def extract_java_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Java file, RETURN a :class:`FileExtraction`."""
    content = source_text
    source_lines = content.split('\n')
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('//')
               and not line.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = blank_block_comments_preserving_lines(content_clean)

    # import statements
    imports = re.findall(r'import\s+([\w.]+);', content)

    # class / interface / enum
    class_pattern = re.compile(
        r'(?:public|private|protected|abstract|final|\s)*'
        r'(?:class|interface|enum)\s+([\w]+)'
        r'(?:\s+extends\s+[\w<>, ]+)?(?:\s+implements\s+[\w<>, ]+)?\s*\{',
        re.MULTILINE
    )
    # v0.2.92 WP-5b: a LIST of (name, start_line, end_line), not a dict keyed
    # by name — see the same change in `ruby.py`, where the fixture proves the
    # loss. Java forbids two TOP-LEVEL types with one name, but not two nested
    # ones (`class A { class Node {} } class B { class Node {} }` is legal and
    # this pattern matches both), and the dict kept only the last. The writer's
    # occurrence disambiguator keys on `(kind, identity_key)` and already
    # covers KIND_CLASS, so the second lands as `Node#2`.
    class_decls: List[Tuple[str, int, int]] = []
    for m in class_pattern.finditer(content_clean):
        name = m.group(1)
        if not name or name[0].islower():
            continue
        # v0.2.92: `class_pattern`'s leading `(?:public|…|\s)*` group starts
        # matching at the whitespace after the previous token, so `m.start()`
        # sits on the previous line — the golden fixture's `Account` class was
        # stored starting on `package golden;`.
        start_line = content_clean[
            :skip_leading_whitespace(content_clean, m.start(), m.end())
        ].count('\n') + 1
        class_decls.append((
            name, start_line,
            _extract_balanced_block(source_lines, start_line, language="java"),  # V52-O.11.E (was: start_line + 60)
        ))

    #: Unique type names in source order — what the module summary lists.
    class_names: List[str] = list(dict.fromkeys(n for n, _, _ in class_decls))

    # Methods
    method_pattern = re.compile(
        r'(?:public|private|protected|static|final|synchronized|native|abstract|\s)+'
        r'(?:[\w<>\[\]]+\s+)+([\w]+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, ]+)?\s*'
        # v0.2.92 WP-5b: the terminator is CAPTURED (group 3) and `;` is
        # accepted. This pattern used to end in a bare `\{`, so EVERY BODILESS
        # DECLARATION matched nothing: an interface's methods, an abstract
        # method, a `native` method. A Java interface is the one construct a
        # code-graph consumer most wants to find by name ("what implements
        # this?"), and the graph contained none of them. `_extract_balanced_block`
        # is meaningful only for the `{` branch — run it on a bodiless
        # declaration and it finds no opener on that line, keeps scanning, and
        # latches onto the NEXT member's braces (the same defect this release
        # fixed for the Rust trait method and the C# interface method).
        r'(\{|;)',
        re.MULTILINE
    )

    # Package + summary
    pkg_match = re.search(r'^package\s+([\w.]+);', content, re.MULTILINE)
    pkg_name = pkg_match.group(1) if pkg_match else ''
    summary_parts = [f"Java module: {relative_path}"]
    if pkg_name:
        summary_parts.append(f"Package: {pkg_name}")
    if class_names:
        summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if (', 'while (', 'for (', 'switch (', 'catch (']))

    module = ModuleDescriptor(
        path=relative_path, language="Java", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # v0.2.92 — the `end_line` convention. `_extract_balanced_block` returns
    # the 1-indexed CLOSING line, and its docstring states that IS the
    # `end_line` at every caller site. This loop used to store
    # `start_line + len(class_lines)`, which is that line PLUS ONE, while the
    # FUNCTION loop below already used the returned value directly. One
    # convention now; the golden corpus had ratified the +1 across 7 languages.
    _methods_by_name: Dict[str, List[str]] = {}
    for cname, start_line, _class_end_line in class_decls:
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        # V52-O.11.F.2-JAVA (v0.2.52, 2026-06-09): scope `methods` to
        # method declarations INSIDE `class <cname> { ... }` (or the
        # matching interface/enum). Pre-V52-O.11.F.2 this line ran
        # `method_pattern.finditer(content_clean)` unconditionally —
        # that attributed EVERY method in the file to EVERY class.
        # Same antipattern audit a79152 flagged in Rust, fixed there
        # in V52-O.11.F. Java fix mirrors via `_java_methods_for_class`.
        # Computed once per NAME: the helper already unions every declaration
        # with that name, so two rows for one name repeat no work.
        if cname not in _methods_by_name:
            _methods_by_name[cname] = _java_methods_for_class(
                content_clean, cname, source_lines
            )
        methods = _methods_by_name[cname]
        signature = f"class {cname}"
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname,
            full_name=f"{pkg_name}.{cname}" if pkg_name else cname,
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=_class_end_line,
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, cb=class_body, mth=methods:
                helpers.embed_class(sig, cb, methods=mth[:10], language="java")
            ),
        ))
        stats['classes'] += 1

    for m in method_pattern.finditer(content_clean):
        mname, args_str, terminator = m.group(1), m.group(2), m.group(3)
        if mname in ('if', 'while', 'for', 'switch', 'catch', 'try', 'else', 'return'):
            continue
        # v0.2.92: same leading-whitespace group as `class_pattern`. Before this
        # fix `Account.deposit` and `Account.getBalance` were both stored
        # starting on the PREVIOUS member's closing `}`, and their stored
        # `body` began with that brace.
        decl_pos = skip_leading_whitespace(content_clean, m.start(), m.end())
        # v0.2.92 WP-5b: reject a match whose modifier/return-type run is
        # actually a STATEMENT (`new Thread(runnable);`). Only reachable now
        # that `;` is an accepted terminator — see `_JAVA_NON_DECL_TOKENS`.
        if not _java_declaration_run_is_a_member(content_clean, decl_pos, m.start(1)):
            continue
        start_line = content_clean[:decl_pos].count('\n') + 1
        # v0.2.92 WP-5b: branch on the terminator. A `;` declaration has no
        # body and ends on its own line; only `{` opens a block a brace scan
        # can measure.
        if terminator == '{':
            end_line = _extract_balanced_block(source_lines, start_line, language="java")  # V52-O.11.E (was: start_line + 50)
        else:
            end_line = content_clean[:m.end()].count('\n') + 1
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        # Find enclosing class
        enclosing = next(
            (c for c, cl, _ in sorted(class_decls, key=lambda d: d[1], reverse=True)
             if cl <= start_line), file_path.stem
        )
        full_name = f"{enclosing}.{mname}"
        signature = f"{mname}({args_str})"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=mname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="java")
            ),
        ))
        stats['functions'] += 1

    # Cross-language interactions (writer replays with the module UUID).
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, imports, "Java", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="Java"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_java_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_java_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
