# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""C/C++ extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_cpp_file`` — the move itself was verbatim apart from
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

# ── v0.2.92 WP-5c: free functions (R25) ────────────────────────────────────
# Until this release ``cpp.py`` captured ONE function shape — the out-of-line
# ``Class::method(...) { … }`` definition — so a free function, a ``main()``
# and every function in a C translation unit produced no row at all. WP-5b
# documented the gap instead of closing it, on the grounds that the failure
# mode of a return-type-shaped pattern is SPURIOUS ROWS over every ``.cpp``
# and ``.h`` in every indexed project: the same defect class this release
# spent itself removing from the C# extractor. R25 refused that disposition
# and made the negative space the acceptance bar instead, so the guards below
# are the deliverable and the pattern is what they permit.
# ``tests/test_v0292_wp5c_cpp_negative_space.py`` holds the anti-fixture.

#: One token of a modifier / return-type run: an identifier, optionally
#: ``::``-qualified, optionally carrying template arguments.
#:
#: Keeping ``::`` INSIDE the token is what makes an out-of-line member
#: unmatchable rather than merely rejected afterwards. In
#: ``int Gadget::total() {`` the run's tokens must each be followed by
#: ``[\s*&]+``; ``Gadget::total`` is followed by ``(``, and every shorter
#: split leaves a bare ``:`` where a separator must be. So no split of that
#: line yields an unqualified name, ``method_pattern`` keeps sole ownership
#: of the shape, and nothing is double-counted.
_CPP_RUN_TOKEN = (
    r'[A-Za-z_]\w*'
    r'(?:\s*::\s*[A-Za-z_]\w*)*'
    r'(?:\s*<[^<>;{}()]*(?:<[^<>;{}()]*>[^<>;{}()]*)*>)?'
)

_CPP_FREE_FUNCTION_PATTERN = re.compile(
    # (1) The declaration begins at the first non-whitespace character of a
    #     line. `\s*` spans newlines, so it always lands there — which admits
    #     the INDENTED namespace-scope function (the common case a column-0
    #     anchor misses) while excluding every mid-line lookalike: a lambda
    #     passed as an argument, a `return f(x)` call, a member-initialiser
    #     entry. `\s` also matches `\r`, so a CRLF checkout behaves identically.
    r'(?:^|\n)\s*'
    # A `template<…>` clause is PART of the declaration — the same rule
    # `class_pattern` follows, so `template <typename T>` on its own line
    # lands in the stored body instead of being dropped. Bounded by `;{}` so
    # it can never run away across a statement.
    r'(?:template\s*<[^;{}<>]*(?:<[^;{}<>]*>[^;{}<>]*)*>\s*)?'
    # (2) A NON-EMPTY modifier/return-type run is required. `if (c) {`,
    #     `for (…) {`, `switch (v) {` and a bare macro invocation have no run
    #     at all and cannot match — and a member-initialiser list wrapped onto
    #     its own line (`      items_(n, 0) {`) opens with the member name,
    #     which is a run of length zero too.
    rf'((?:{_CPP_RUN_TOKEN}[\s*&]+)+)'
    r'([A-Za-z_]\w*)\s*'
    # `[^)]*` cannot cross a `)`, which bounds how far a match can reach while
    # still spanning the newlines of a signature split across lines.
    r'\(([^)]*)\)'
    r'(?:\s*(?:const|volatile|noexcept|override|final|mutable)\b)*'
    r'(?:\s*noexcept\s*\([^)]*\))?'
    r'(?:\s*->\s*[^;{]+?)?'
    # (3) The terminator is `{`, NEVER `;`. This is the load-bearing guard.
    #     C++ function-style initialization (`std::vector<int> v(10, 0);`) is
    #     syntactically a function DECLARATION — the most vexing parse — so no
    #     regex can tell a prototype from a variable definition. Java could
    #     accept `;` for its interface methods (WP-5b) because its statement
    #     grammar has no such form; C++ cannot, and accepting `;` here would
    #     also re-admit `return f(x);`, `MY_ASSERT(x);` and every macro call.
    r'\s*\{',
    re.MULTILINE,
)

#: Tokens that may not appear in a declaration's modifier / return-type run.
#: The mirror of ``java._JAVA_NON_DECL_TOKENS`` and
#: ``csharp._CSHARP_NON_DECL_TOKENS``, for the same reason: the captured NAME
#: of a lookalike is a valid identifier, so a keyword filter on the name alone
#: cannot see the problem — the tell is the run. ``if constexpr (x) {`` is the
#: instance that needs both (name ``constexpr``, run ``if``).
_CPP_NON_DECL_TOKENS = frozenset({
    # statement and control keywords
    "return", "throw", "new", "delete", "if", "else", "for", "while", "do",
    "switch", "case", "default", "try", "catch", "goto", "break", "continue",
    "co_return", "co_await", "co_yield", "sizeof", "alignof", "typeid",
    "static_assert", "operator",
    # type-declaration keywords — a type is not a function
    "class", "struct", "union", "enum", "namespace", "typedef", "using",
    "template", "concept", "requires",
    # access specifiers; the run charset already refuses their `:`, so
    # reaching one here means the run swept across a member boundary
    "public", "private", "protected",
})

#: Names that are never a function's, even with a plausible run in front.
_CPP_NON_FUNCTION_NAMES = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "try",
    "catch", "return", "throw", "new", "delete", "sizeof", "alignof",
    "typeid", "static_assert", "constexpr", "consteval", "noexcept",
    "decltype", "and", "or", "not", "xor", "bitand", "bitor", "compl",
})


def _cpp_run_is_a_declaration(run: str) -> bool:
    """True when ``run`` is a modifier / return-type run rather than the tail
    of a statement. Mirrors ``java._java_declaration_run_is_a_member``."""
    return not (_CPP_NON_DECL_TOKENS & set(re.findall(r"[A-Za-z_]\w*", run)))


def _cpp_is_macro_continuation(clean_lines: List[str], start_line: int) -> bool:
    """True when the declaration at ``start_line`` is the body of a multi-line
    ``#define``.

    A macro's FIRST line is refused by the pattern already (``#`` is not a
    run token), but its continuation lines are ordinary-looking C++::

        #define DEFINE_TRIVIAL_FN  \\
            int macro_bodied_fn() {  \\
                return 1;  \\
            }

    Without this the macro body mints a row for a function that exists only
    once the preprocessor has run, with a body full of trailing backslashes.
    ``clean_lines`` is the COMMENT-SCRUBBED text, so a backslash inside a
    comment cannot trip the check.
    """
    prev = start_line - 2  # 0-indexed line above the declaration
    return prev >= 0 and clean_lines[prev].rstrip().endswith("\\")


def extract_cpp_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a C++/header file, RETURN a :class:`FileExtraction`."""
    content = source_text
    source_lines = content.split('\n')
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('//')
               and not line.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Strip comments for pattern matching
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = blank_block_comments_preserving_lines(content_clean)

    # Includes
    includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)

    # Classes / structs
    class_pattern = re.compile(
        # v0.2.92: an optional `template <…>` clause is PART of the class
        # declaration, so the match must begin at it. Without this the
        # whitespace skip below would move `template <typename T>\nclass Box`
        # from the template line onto the `class` line and drop the template
        # clause out of the stored `body` — the old `m.start()` landed on the
        # template line only by accident (it was the previous line's newline).
        r'(?:^|\n)\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+'
        # v0.2.92 WP-5c (R23, pre-existing): tokens may stand between the
        # keyword and the NAME — an export macro (`class MYLIB_API Widget {`,
        # ubiquitous in shipped C++ headers) — and between the name and the
        # base-clause: `final`. Neither could be skipped, so `class Derived
        # final : public Base {` and `class MYLIB_API Widget {` produced NO
        # class row at all. That was a silent loss before this release; with
        # the free-function capture below it becomes a MISATTRIBUTION, because
        # every member defined in such a class body has no enclosing type to
        # be found by containment and is minted under the file stem instead.
        # Non-greedy, so the ordinary `class Foo {` still matches with an
        # empty prefix and every already-captured type keeps its name.
        r'(?:[\w]+\s+)*?([\w]+)\s*(?:final\s*)?'
        r'(?::[^{]*)?\{',
        re.MULTILINE
    )
    # v0.2.92 WP-5b: a LIST of (name, start_line), not a dict keyed by name —
    # see the same change in `ruby.py`, where the fixture proves the loss. Two
    # types with one name in ONE translation unit are legal in different
    # namespaces (`namespace a { struct Cfg {…}; } namespace b { struct Cfg
    # {…}; }`), and this extractor ignores namespaces when building
    # `full_name`, so the dict silently kept only the last. The writer's
    # occurrence disambiguator keys on `(kind, identity_key)` and already
    # covers KIND_CLASS, so the second lands as `Cfg#2`.
    #
    # v0.2.92 WP-5c: the tuple gained the END line, computed here rather than
    # inside the emit loop — the same shape `java.py` already carries. The
    # free-function pass below needs every type's full RANGE before it can
    # decide whether a definition sits inside a class body, and computing it
    # twice would be the duplication this cycle is removing.
    class_decls: List[Tuple[str, int, int]] = []
    for m in class_pattern.finditer(content_clean):
        cname = m.group(1)
        if cname in ('if', 'else', 'while', 'for', 'switch', 'namespace', 'return'):
            continue
        # v0.2.92: the pattern's `(?:^|\n)\s*` prefix makes `m.start()` the
        # newline ENDING the previous line, so the stored `start_line` was one
        # line too high — the golden fixture's `Circle` was stored starting on
        # `namespace shapes {` (and therefore took the WHOLE namespace as its
        # body) and `Point` on the previous class's `};`.
        start_line = content_clean[
            :skip_leading_whitespace(content_clean, m.start(), m.end())
        ].count('\n') + 1
        class_decls.append((
            cname, start_line,
            _extract_balanced_block(source_lines, start_line, language="cpp"),  # V52-O.11.E (was: start_line + 60)
        ))

    #: Unique type names in source order — what the module summary lists.
    class_names: List[str] = list(dict.fromkeys(n for n, _, _ in class_decls))

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
    if class_names:
        summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
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

    # ── v0.2.92 WP-5c: free functions + in-class definitions ───────────────
    # Runs BEFORE the class loop because a class row's `methods` list has to
    # name the members defined inside its own body: emitting a function row
    # for a member while its owning type omits it is how the graph came to
    # DISAGREE WITH ITSELF for every Java interface (WP-5b), and this shape
    # would reintroduce that for every header-only C++ class.
    clean_lines = content_clean.split('\n')

    def _enclosing_type(line: int) -> Optional[Tuple[str, int]]:
        """Innermost type whose RANGE contains ``line``, or None.

        Containment, not proximity. WP-5b had to correct a proximity
        heuristic in `ruby.py` that made a top-level `def` following a
        class's `end` a member of it; the same "nearest declaration above"
        rule here would attribute every free function in a file to whichever
        type happened to precede it.
        """
        best: Optional[Tuple[str, int]] = None
        for cname_, cstart, cend in class_decls:
            if cstart <= line <= cend and (best is None or cstart > best[1]):
                best = (cname_, cstart)
        return best

    #: (name, args, start_line, end_line, owner) — owner is the (name, start)
    #: of the enclosing type for an in-class definition, else None.
    fn_decls: List[Tuple[str, str, int, int, Optional[Tuple[str, int]]]] = []
    #: In-class member names per OWNING ROW, keyed by (name, start_line) so
    #: that two same-named types in different namespaces — the duplicate the
    #: WP-5b list change made representable — do not share one method list.
    in_class_methods: Dict[Tuple[str, int], List[str]] = {}

    for m in _CPP_FREE_FUNCTION_PATTERN.finditer(content_clean):
        run, fname, args_str = m.group(1), m.group(2), m.group(3)
        if fname in _CPP_NON_FUNCTION_NAMES:
            continue
        if not _cpp_run_is_a_declaration(run):
            continue
        # Same whitespace skip as `class_pattern`: the leading `(?:^|\n)\s*`
        # makes `m.start()` the newline ENDING the previous line.
        decl_pos = skip_leading_whitespace(content_clean, m.start(), m.end())
        start_line = content_clean[:decl_pos].count('\n') + 1
        if _cpp_is_macro_continuation(clean_lines, start_line):
            continue
        end_line = _extract_balanced_block(source_lines, start_line, language="cpp")
        owner = _enclosing_type(start_line)
        if owner is not None:
            in_class_methods.setdefault(owner, [])
            if fname not in in_class_methods[owner]:
                in_class_methods[owner].append(fname)
        fn_decls.append((fname, args_str, start_line, end_line, owner))

    # Extract classes
    # v0.2.92 — the `end_line` convention. `_extract_balanced_block` returns
    # the 1-indexed CLOSING line, and its docstring states that IS the
    # `end_line` at every caller site. This loop used to store
    # `start_line + len(class_lines)`, which is that line PLUS ONE, while the
    # FUNCTION loop below already used the returned value directly. One
    # convention now; the golden corpus had ratified the +1 across 7 languages.
    for cname, start_line, _class_end_line in class_decls:
        methods = [m.group(2) for m in method_pattern.finditer(content_clean)
                   if m.group(1) == cname]
        # v0.2.92 WP-5c: …plus the members DEFINED IN THIS BODY, which have no
        # `Class::` qualifier for `method_pattern` to find. Out-of-line names
        # keep their order and come first; the in-class names follow, deduped
        # (a member cannot legally be defined both ways, but a name repeated
        # by an overload set must not be listed twice).
        methods = methods + [
            n for n in in_class_methods.get((cname, start_line), [])
            if n not in methods
        ]
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        signature = f"class {cname}"
        # v0.2.82 (G1 task 2): defer the class embed so `_resolve_deferred_embed`
        # SKIP/STAMPs a hash-matched class row on a metadata-only revision bump
        # (this cpp CLASS site embedded EAGERLY pre-G1, defeating the guard).
        # The embed text is BYTE-IDENTICAL to the eager form — the closure just
        # delays the same `generate_embedding(<custom text>)` call. Default-arg
        # capture (sig/mth/cb) pins each loop iteration's own values. For an
        # over-budget (multi-chunk) class the chunk fan-out re-embeds per chunk
        # regardless — this vector is used only on the single-chunk write.
        _emb_methods = list(methods[:10])
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{file_path.stem}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=_class_end_line,
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, mth=_emb_methods, cb=class_body:
                helpers.generate_embedding(
                    f"{sig}\nMethods: {', '.join(mth)}\n{cb[:500]}"
                )
            ),
        ))
        stats['classes'] += 1

    # Extract method implementations
    for m in method_pattern.finditer(content_clean):
        class_name, method_name, args_str = m.group(1), m.group(2), m.group(3)
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line, language="cpp")  # V52-O.11.E (was: start_line + 50)
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

    # v0.2.92 WP-5c: emit the free functions and in-class definitions found
    # above. A member DEFINED INSIDE its class body takes the SAME
    # `<stem>.<Class>.<name>` full_name and `Class::name(args)` signature as
    # the out-of-line spelling, so one member does not appear as two
    # differently-shaped rows depending on where its author put the body;
    # a genuinely free function is keyed on the file stem, matching this
    # extractor's existing decision to ignore namespaces in `full_name`.
    for fname, args_str, start_line, end_line, owner in fn_decls:
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        if owner is not None:
            full_name = f"{file_path.stem}.{owner[0]}.{fname}"
            signature = f"{owner[0]}::{fname}({args_str})"
        else:
            full_name = f"{file_path.stem}.{fname}"
            signature = f"{fname}({args_str})"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname, full_name=full_name,
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
