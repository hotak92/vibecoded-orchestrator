# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Rust extractor for the code-graph analyzer.

P2f stage 3 (v0.2.77 Part 6): converted to a PURE PRODUCER.
``extract_rust_file(source_text, file_path, repo_root, helpers) ->
FileExtraction`` reads source and RETURNS what to write — it mutates NO analyzer
state. The thin ``analyze_rust_file(ctx, file_path, repo_root)`` shim keeps the
unchanged-skip gate analyzer-side (short-circuit preserved), then drives
``extract`` -> ``ctx.write_file_extraction`` -> stats dict. The dispatch table
and ``EXTRACTORS`` entry are unchanged; behaviour is pinned byte-identically by
``tests/test_codegraph_golden.py``.

``_rust_methods_for_struct`` (V52-O.11.F per-impl method attribution) and
``_is_rust_test_fn`` (V52-O.11.J ``#[cfg(test)]`` gate) are unchanged.
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


def _rust_methods_for_struct(
    content_clean: str,
    struct_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F (v0.2.52, 2026-06-09): extract methods declared inside
    every ``impl <struct_name>`` (or ``impl <Trait> for <struct_name>``)
    block in the source.

    Replaces the pre-V52-O.11.F line that ran
    ``func_pattern.finditer(content_clean)`` unconditionally — that
    attributed EVERY function in the file to EVERY struct. Audit a79152
    confirmed: a 50-fn file with 3 structs produced 150 incorrect
    method attributions, drowning the real ``methods`` signal in noise.

    Algorithm:

      1. Find every ``impl`` block whose target type matches ``struct_name``.
         Two shapes accepted:
           - ``impl <generics?> <struct_name> <generics?> {``      (inherent)
           - ``impl <generics?> <Trait> for <struct_name> <generics?> {`` (trait)
      2. For each matched impl block, find its closing brace via the
         existing ``_extract_balanced_block`` helper (already brace-balanced
         per V52-O.11.E).
      3. Scan the block body for ``fn name(...)`` patterns; collect names.
      4. Return deduplicated list, preserving first-seen order.

    Returns empty list if no impl blocks match — common for plain data
    structs that only carry fields. The caller renders ``methods: []`` in
    the CodeClass row, which is correct (no methods exist).

    Limitations:
      - Doesn't handle ``impl<T: Trait> ...`` where the type parameter
        appears literally in the type position (rare in practice).
      - Doesn't extract methods declared via macros (``impl_trait!``).
      - Doesn't follow ``impl`` in nested ``mod`` blocks transparently —
        the brace-balance helper correctly skips over them, so methods
        in nested mods that DO impl on ``struct_name`` from the outer
        scope get picked up. Methods inside nested impl blocks on a
        DIFFERENT struct are correctly excluded.
    """
    # Find every impl header line whose target type matches struct_name.
    # The regex covers both shapes:
    #   - impl <generics>? <Name> <generics>? {
    #   - impl <generics>? <Trait> for <Name> <generics>? {
    # Generics + trailing-where are non-capturing; we only need the impl
    # start position. Generics are matched with a simple ``<[^>]*>`` since
    # Rust generics rarely nest in impl headers (where-clauses move
    # complex bounds to after the type).
    escaped = re.escape(struct_name)
    # Shape A: `impl Foo` / `impl<T> Foo<T>` / `impl<T> Foo`
    # Shape B: `impl Trait for Foo` / `impl<T> Trait<T> for Foo<T>`
    impl_pattern = re.compile(
        # `impl` keyword
        r"impl"
        # Optional generic parameters on impl
        r"(?:\s*<[^>]*>)?"
        # The target type. Either Shape A (just struct_name) or
        # Shape B (Trait + `for` + struct_name).
        r"\s+(?:"
        # Shape B alternative: any token sequence + `for` + struct_name
        rf"(?:[\w:]+(?:\s*<[^>]*>)?\s+for\s+){escaped}"
        rf"|{escaped}"
        # Shape A — struct_name alone
        r")"
        # Optional generics on the target (rare but valid)
        r"(?:\s*<[^>]*>)?"
        # Optional where clause + opening brace
        r"\s*(?:where\s+[^{]*)?\{",
        re.MULTILINE,
    )

    method_pattern_inner = re.compile(
        r"(?:pub\s+(?:\([^)]*\)\s+)?)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
        r"fn\s+([\w]+)\s*",
    )

    methods: List[str] = []
    seen: set = set()
    for m in impl_pattern.finditer(content_clean):
        # Locate the opening brace position (the regex anchors on it).
        # m.end() is just past the `{`, so the impl body starts there.
        impl_open_pos = m.end() - 1  # position of `{`
        # Find the matching close via brace-balance on source_lines.
        # We need a source-line index: convert char-offset to line.
        impl_open_line = content_clean[:impl_open_pos].count("\n") + 1
        impl_close_line = _extract_balanced_block(
            source_lines, impl_open_line, max_lookahead=800
        )
        # Slice content_clean (NOT source_lines — content_clean has
        # comments stripped, mirroring how the original method extraction
        # worked).
        block_start_pos = impl_open_pos + 1  # skip `{`
        # Find char-offset for impl_close_line. Sum line lengths +1 for \n.
        # Cheaper: count lines from block_start_pos and stop when we've
        # passed (impl_close_line - impl_open_line) newlines.
        target_newlines = impl_close_line - impl_open_line
        if target_newlines <= 0:
            continue
        # Find the position by counting newlines from block_start_pos.
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        impl_body = content_clean[block_start_pos:block_end_pos]
        for fm in method_pattern_inner.finditer(impl_body):
            name = fm.group(1)
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)
    return methods


def _is_rust_test_fn(content: str, fn_offset: int) -> bool:
    """V52-O.11.J (v0.2.52, 2026-06-09): return True if the Rust ``fn``
    starting at ``content[fn_offset:]`` is gated by a ``#[cfg(test)]``,
    ``#[test]``, or ``#[cfg(any(test, ...))]`` attribute on an
    immediately-preceding line (test-only code, not production).

    Pre-V52-O.11.J the Rust parser indexed every function the regex
    matched into ``CodeFunction``, including unit-test helpers and
    ``#[test]``-annotated test functions. That pollutes search results
    (``query_code_structure(callers, ...)`` returns test fixtures), wastes
    embedding budget, and conflates production behaviour with test
    scaffolding. After V52-O.11.J the analyzer skips them.

    Detection algorithm:
      1. Walk backwards from ``fn_offset`` over whitespace and
         line-continuation characters until we find the start of the line
         that contains the ``fn`` keyword (the "fn line").
      2. Walk backwards from the start of the fn line through one OR more
         immediately-preceding lines that are EITHER blank OR start with
         ``#[`` (Rust attributes). Stop at the first line that is neither.
      3. Across all the attribute lines collected, look for ``cfg(test)``,
         ``cfg(any(test``, ``cfg(all(test``, or bare ``[test]``.

    Args:
        content: The source (or content_no_strings — string-literal
            content can't contain ``#[cfg(test)]`` because attributes
            don't appear inside strings, so either input works).
        fn_offset: Byte offset where the matched ``fn`` (or its prefix
            modifier) starts.

    Returns:
        True if a test-gating attribute is found on a preceding attribute
        line, False otherwise.
    """
    # Find start-of-line for the fn-offset.
    line_start = content.rfind('\n', 0, fn_offset) + 1
    # ``rfind`` returns -1 if no newline; +1 makes it 0 — correct for
    # offset at very start of file.

    # Walk backwards collecting preceding attribute lines.
    # Each iteration: find the line PRECEDING ``line_start`` and check
    # whether it's blank or an attribute. Stop at the first non-attribute
    # non-blank line.
    cursor = line_start
    attr_lines: list[str] = []
    # Cap the scan at 16 lines back to bound worst case (test fns rarely
    # have more than 2-3 attribute lines).
    for _ in range(16):
        if cursor <= 0:
            break
        # ``cursor`` points at start of "current" line (which we've
        # already classified or want to skip past). Find the line BEFORE
        # this one.
        # Previous newline ends the previous line.
        prev_nl = content.rfind('\n', 0, cursor - 1)
        prev_line_start = prev_nl + 1  # 0 if rfind returned -1
        prev_line = content[prev_line_start: cursor - 1]
        stripped = prev_line.strip()
        if not stripped:
            # Blank line — keep walking (Rust allows blank lines between
            # attributes and the fn declaration, though it's unusual).
            cursor = prev_line_start
            continue
        if stripped.startswith('#['):
            attr_lines.append(stripped)
            cursor = prev_line_start
            continue
        # Non-blank, non-attribute line: stop scanning.
        break

    # Inspect collected attribute lines for any test gate.
    for attr in attr_lines:
        # Normalise: collapse whitespace so ``#[ cfg ( test ) ]`` etc.
        # all reduce to the same pattern.
        compact = re.sub(r'\s+', '', attr)
        # ``#[test]`` (bare test attribute)
        if '#[test]' in compact:
            return True
        # ``#[cfg(test)]`` — direct test cfg
        if '#[cfg(test)]' in compact:
            return True
        # ``#[cfg(any(test, ...))]`` and ``#[cfg(all(test, ...))]``
        if '#[cfg(any(test,' in compact or '#[cfg(all(test,' in compact:
            return True
        # cfg(...) where test appears later in the predicate
        if '#[cfg(' in compact and ('(test,' in compact or ',test)' in compact or ',test,' in compact):
            return True

    return False


def extract_rust_file(
    source_text: str,
    file_path: Path,
    repo_root: Path,
    helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Rust file and RETURN a :class:`FileExtraction`.

    Mutates no analyzer state. ``helpers`` is the narrow
    :class:`vco_lib.codegraph_lang._shared.ExtractorHelpers` protocol — the
    embedding seams (``embed_class`` / ``embed_function``) the deferred-embed
    closures fire lazily, and ``project_name``. The module reference is NOT
    baked into the entities here (the writer stamps it after minting the module
    UUID). Regex/detection logic is byte-identical to the pre-Part-6 imperative
    extractor; the golden suite pins the stored output.
    """
    content = source_text
    source_lines = content.split('\n')
    loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # use statements
    imports = re.findall(r'use\s+([\w::{}, ]+);', content)

    # struct, enum, trait types
    type_pattern = re.compile(
        r'(?:pub\s+)?(?:struct|enum|trait)\s+([\w]+)', re.MULTILINE
    )
    struct_info: Dict[str, int] = {}
    for m in type_pattern.finditer(content_clean):
        name = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        struct_info[name] = start_line

    # Functions: fn name(...)
    # V52-O.11.G (v0.2.52, 2026-06-09): expand prefix regex to capture the
    # full Rust modifier set — `pub`, `pub(crate)`, `pub(super)`,
    # `pub(in path)`, `async`, `unsafe`, `const`, `extern "ABI"`,
    # `default` — in any order, any combination. Pre-V52-O.11.G this
    # regex only matched `pub` + `async`, silently dropping every
    # `unsafe fn`, `const fn`, `extern "C" fn`, `pub(crate) fn`, and
    # `default fn` in the codebase. Mirrors the modifier-set used in
    # `_rust_methods_for_struct`'s inner method pattern (V52-O.11.F).
    func_pattern = re.compile(
        # Zero or more modifier tokens, any order. Each token is one of:
        #   pub | pub(crate) | pub(super) | pub(in path::to::mod)
        #   async | unsafe | const | extern | extern "ABI" | default
        r'(?:(?:pub(?:\s*\([^)]*\))?|async|unsafe|const|default'
        r'|extern(?:\s+"[^"]*")?)\s+)*'
        r'fn\s+([\w]+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)',
        re.MULTILINE
    )

    # Module summary
    crate_comment = ''
    for line in source_lines[:15]:
        s = line.strip()
        if s.startswith('//!') or s.startswith('///'):
            crate_comment = s.lstrip('/!').strip()
            break
    summary_parts = [f"Rust module: {relative_path}"]
    if crate_comment:
        summary_parts.append(crate_comment)
    if struct_info:
        summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if ', 'while ', 'for ', 'match ', 'loop {']))

    module = ModuleDescriptor(
        path=relative_path, language="Rust", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )

    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    for sname, start_line in struct_info.items():
        _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        signature = f"struct/enum/trait {sname}"
        # V52-O.11.F (v0.2.52, 2026-06-09): scope `methods` to functions
        # declared INSIDE `impl <sname>` blocks (or `impl <Trait> for
        # <sname>` blocks). Pre-V52-O.11.F this line iterated
        # func_pattern over the WHOLE file's content_clean, attributing
        # EVERY fn in the file to EVERY struct — audit a79152 reproduced:
        # a 50-fn file with 3 structs produced 150 incorrect method
        # attributions, drowning real signal in noise for the
        # `query_code_structure(methods, StructName)` MCP path.
        methods = _rust_methods_for_struct(content_clean, sname, source_lines)
        # Bind the loop-varying values so each closure captures ITS OWN
        # signature/body (a bare `lambda` would close over the loop variable
        # and every deferred embed would fire with the LAST struct's text).
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=sname, full_name=f"{file_path.stem}.{sname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=start_line + len(class_lines),
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, cb=class_body:
                helpers.embed_class(sig, cb, language="rust")
            ),
        ))
        stats['classes'] += 1

    for m in func_pattern.finditer(content_clean):
        fname, args_str = m.group(1), m.group(2)
        # V52-O.11.J (v0.2.52, 2026-06-09): skip functions gated by
        # `#[cfg(test)]` / `#[test]` / `#[cfg(any(test, ...))]` /
        # `#[cfg(all(test, ...))]` — test functions are not production
        # code and indexing them confuses the offline trainer + bloats
        # the CodeFunction collection. Audit a79152.
        if _is_rust_test_fn(content_clean, m.start()):
            continue
        is_async = bool(re.search(rf'async\s+fn\s+{re.escape(fname)}', content_clean))
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        full_name = f"{file_path.stem}.{fname}"
        signature = f"fn {fname}({args_str})"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=fname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=is_async, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="rust")
            ),
        ))
        stats['functions'] += 1

    # Cross-language interactions (writer replays with the module UUID).
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, imports, "Rust", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="Rust"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_rust_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim over the pure :func:`extract_rust_file` producer.

    v0.2.82 (G4): the hand-copied walk-time I/O + skip-gate twin was BYTE-
    EQUIVALENT to :func:`vco_lib.codegraph_lang._shared.run_pure_extractor`
    (same CG-5 minified skip + message, same ``_get_existing_module`` gate +
    message, same ``extract`` -> ``write_file_extraction`` -> stats). Routing
    through the shared shim removes the duplication (A>B>C modularity rule); the
    golden suite pins that stored output is unchanged. Signature + dispatch
    registration are unchanged."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_rust_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
