# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ruby extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_ruby_file`` — the move itself was verbatim apart from
the mechanical ``self.`` -> ``ctx.`` rename (``ctx`` IS the analyzer
instance) and the analyzer-resident embedding seams reached via ``ctx.``.
Behaviour has since been CORRECTED here (v0.2.92 and WP-5b — see the notes
below), so it is no longer byte-identical to the analyzer's original;
``tests/test_codegraph_golden.py`` pins what it does TODAY, and the
corpus README explains why a snapshot is evidence of behaviour rather
than of correctness.

v0.2.92 WP-5b — four defects fixed here, all of them silent data loss that the
shipped golden corpus had ratified (see
``tests/test_v0292_wp5b_ruby_blocks_and_scoping.py``):

  * every class and every method body ran to END-OF-FILE, because both loops
    called ``_extract_balanced_block``, which counts BRACES that idiomatic Ruby
    does not have. Replaced by
    :func:`vco_lib.codegraph_lang._shared.extract_end_keyword_block`;
  * the per-class ``methods`` list was built over the WHOLE FILE, so all three
    ``ledger.rb`` classes carried the same six names. This is the V52-O.11.F
    antipattern fixed for Rust, Java, C#, Go and JS in v0.2.52 and never for
    Ruby; :func:`_ruby_methods_for_class` mirrors ``_csharp_methods_for_class``;
  * a REOPENED class kept only its LAST definition, because ``class_info`` was
    a dict keyed by name. ``ledger.rb`` declares ``class Account`` twice and
    stored ONE row, starting at the second — the first definition's body and
    its three methods were unreachable;
  * a class declared at any INDENT (``module X`` wrapping ``class Y``, the
    commonest Ruby file shape there is) matched nothing, because the class
    pattern was anchored at column 0.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    InteractionGroup,
    KIND_CLASS,
    KIND_FUNCTION,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_external_calls,
    blank_block_comments_preserving_lines,
    extract_end_keyword_block,
    run_pure_extractor,
)

#: ``class Foo``, ``module Foo``, ``class Foo < Bar``, at any indent. The
#: trailing anchor uses ``[ \t]`` rather than ``\s``: under ``re.MULTILINE``
#: ``\s`` matches a NEWLINE, so ``\s*$`` can run the match past the
#: declaration and pull a following line's text into the superclass group —
#: the same ``\s``-crosses-lines defect v0.2.92 fixed in ``powershell.py``.
_RUBY_CLASS_RE = re.compile(
    r'^[ \t]*(?:class|module)\s+([\w:]+)(?:[ \t]*<[ \t]*[\w:]+)?[ \t]*$',
    re.MULTILINE,
)

#: ``def name``, ``def self.name``, at any indent.
_RUBY_DEF_RE = re.compile(
    r'^[ \t]*def\s+(?:self\.)?([\w?!]+)\s*(?:\(([^)]*)\))?',
    re.MULTILINE,
)

#: How far a Ruby class body may run before the scanner gives up. Larger than
#: the method default for the same reason ``java.py`` passes 2000 and
#: ``csharp.py`` 800: a type body legitimately dwarfs a single method.
_RUBY_CLASS_LOOKAHEAD = 2000


def _ruby_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """Methods declared inside the body of ``class|module <class_name>``.

    v0.2.92 WP-5b. Closes the V52-O.11.F antipattern for the one language it
    was never closed for: this list used to be
    ``func_pattern.finditer(content_clean)`` over the WHOLE FILE, so every Ruby
    class in a file was stamped with every method in that file. The golden
    corpus shows the shape exactly — ``ledger.rb``'s ``Accounting``,
    ``Account`` and ``SavingsAccount`` each carried the identical six-name
    list, and four of the six were not in the class's own body.

    Deliberately mirrors ``_csharp_methods_for_class`` rather than inventing a
    shape, including its two documented properties:

      * **Union across declarations.** Every ``class|module`` block with this
        name contributes, and the union is returned. For C# that is the
        partial-class case; for Ruby it is class REOPENING, which is
        idiomatic — ``Account``'s two blocks in ``ledger.rb`` are two rows
        (see the class loop) that both describe the same runtime class, so
        both carry its whole method set.
      * **Nested types over-count.** A ``def`` inside a class nested in this
        one is inside this one's block too, so it lands in the list. C# and JS
        accept the same over-count and say so; Java strips nested bodies. Not
        silently different here: it is the C# behaviour, named.

    Returns ``[]`` when no block of that name is found — the same "extracted
    from a declaration whose body is elsewhere" case C# documents.
    """
    escaped = re.escape(class_name)
    header = re.compile(
        r'^[ \t]*(?:class|module)\s+(?:[\w:]*::)?' + escaped
        + r'(?:[ \t]*<[ \t]*[\w:]+)?[ \t]*$',
        re.MULTILINE,
    )
    methods: List[str] = []
    seen: set = set()
    for hdr in header.finditer(content_clean):
        start_line = content_clean[:hdr.start()].count('\n') + 1
        end_line = extract_end_keyword_block(
            source_lines, start_line, language="ruby",
            max_lookahead=_RUBY_CLASS_LOOKAHEAD,
        )
        if end_line <= start_line:
            continue
        # The body is the lines BETWEEN the header and its `end`.
        body = '\n'.join(source_lines[start_line:max(0, end_line - 1)])
        for fm in _RUBY_DEF_RE.finditer(body):
            name = fm.group(1)
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)
    return methods


def extract_ruby_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Ruby file, RETURN a :class:`FileExtraction`."""
    content = source_text
    source_lines = content.split('\n')
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('#')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Strip inline comments
    content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
    # Strip =begin/=end blocks. Line-anchored: an `=end` mid-expression is not a
    # comment terminator. Newline-preserving (v0.2.92) — `start_line` below is
    # derived from this copy and indexes into `source_lines`.
    content_clean = blank_block_comments_preserving_lines(
        content_clean, "=begin", "=end", line_anchored=True
    )

    # require / require_relative
    imports = re.findall(r'require(?:_relative)?\s+[\'"]([^\'"]+)[\'"]', content)

    # class / module definitions
    #
    # v0.2.92 WP-5b: a LIST of (name, start_line, end_line), not a dict keyed
    # by name. A dict silently kept only the LAST declaration, and Ruby class
    # REOPENING — `class Account` twice in one file, once to define and once to
    # extend — is idiomatic, not exotic. The golden corpus stored ONE `Account`
    # row starting at the reopening, so the first definition's body and its
    # `initialize` / `deposit` / `default` were unreachable in the graph. The
    # writer's occurrence disambiguator keys on `(kind, identity_key)` and
    # already covers KIND_CLASS, so the second row lands as `Account#2`.
    class_decls: List[Tuple[str, int, int]] = []
    for m in _RUBY_CLASS_RE.finditer(content_clean):
        name = m.group(1).split('::')[-1]  # unqualified name
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = extract_end_keyword_block(
            source_lines, start_line, language="ruby",
            max_lookahead=_RUBY_CLASS_LOOKAHEAD,
        )
        class_decls.append((name, start_line, end_line))

    #: Unique class names in source order — what the module summary lists, and
    #: what `_ruby_methods_for_class` is called once per. Reopening a class
    #: does not make it two classes for either purpose.
    class_names: List[str] = list(dict.fromkeys(n for n, _, _ in class_decls))

    # methods: def name or def ctx.name
    func_pattern = _RUBY_DEF_RE

    def _enclosing_for(line_no: int) -> str:
        """The INNERMOST class/module block containing ``line_no``.

        v0.2.92 WP-5b: was "the nearest preceding declaration", which cannot
        tell a method inside a class from a top-level `def` written AFTER that
        class's `end` — the latter was attributed to the class. Containment is
        available now only because every declaration finally has a true
        `end_line`; before this release every block ran to EOF, so every line
        was "inside" every block and the question was unanswerable.
        """
        best: Tuple[str, int] = (file_path.stem, -1)
        for cname, cstart, cend in class_decls:
            if cstart <= line_no <= cend and cstart > best[1]:
                best = (cname, cstart)
        return best[0]

    # Module summary
    file_comment = ''
    for line in source_lines[:15]:
        s = line.strip()
        if s.startswith('#') and not s.startswith('#!'):
            file_comment = s.lstrip('#').strip()
            break
    summary_parts = [f"Ruby module: {relative_path}"]
    if file_comment:
        summary_parts.append(file_comment)
    if class_names:
        summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if ', 'unless ', 'while ', 'until ', 'case ', 'rescue ']))

    module = ModuleDescriptor(
        path=relative_path, language="Ruby", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # v0.2.92 — the `end_line` convention. The block scanner returns the
    # 1-indexed CLOSING line, and that IS the `end_line` at every caller site.
    #
    # v0.2.92 WP-5b — the SCANNER. This loop used to call
    # `_extract_balanced_block(language="ruby")`, which counts `{`/`}`. A Ruby
    # class has none, so the scan found no opener, fell through to the runaway
    # branch, and every class stored a body running to end-of-file: all three
    # `ledger.rb` classes ended at line 40 of a 40-line file.
    _methods_by_name: Dict[str, List[str]] = {}
    for cname, start_line, _class_end_line in class_decls:
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        # V52-O.11.F.2-RUBY (v0.2.92 WP-5b): scope `methods` to the `def`s
        # inside `class|module <cname>`. This line used to run the def pattern
        # over the WHOLE FILE — the antipattern V52-O.11.F closed for Rust in
        # v0.2.52 and its .2 siblings closed for Java, C#, Go and JS, never for
        # Ruby. Computed once per NAME (a reopened class's two rows describe
        # one runtime class and share its method set, as C# partials do).
        if cname not in _methods_by_name:
            _methods_by_name[cname] = _ruby_methods_for_class(
                content_clean, cname, source_lines
            )
        methods = _methods_by_name[cname]
        signature = f"class {cname}"
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{file_path.stem}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=_class_end_line,
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, cb=class_body, mth=methods:
                helpers.embed_class(sig, cb, methods=mth[:10], language="ruby")
            ),
        ))
        stats['classes'] += 1

    for m in func_pattern.finditer(content_clean):
        fname = m.group(1)
        args_str = m.group(2) or ''
        start_line = content_clean[:m.start()].count('\n') + 1
        # v0.2.92 WP-5b: the `end`-keyword scanner, for the same reason as the
        # class loop above — and it also answers the Ruby-3.0 endless method
        # (`def size = @n`), which opens no block and is one line long.
        end_line = extract_end_keyword_block(source_lines, start_line, language="ruby")
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        enclosing = _enclosing_for(start_line)
        full_name = f"{enclosing}.{fname}"
        signature = f"def {fname}({args_str})"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=False, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="ruby")
            ),
        ))
        stats['functions'] += 1

    # Cross-language interactions (writer replays with the module UUID).
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, imports, "Ruby", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="Ruby"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_ruby_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_ruby_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
