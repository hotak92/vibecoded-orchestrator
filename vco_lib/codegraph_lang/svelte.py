# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Svelte extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
the V52-O.11.B parser helpers
(``_extract_svelte_script_blocks`` / ``_parse_svelte_functions`` + their
regex constants) and ``CodeGraphAnalyzer._analyze_svelte_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
(``ctx`` IS the analyzer instance) and the analyzer-resident embedding
seams reached via ``ctx.``. Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
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
    KIND_FUNCTION,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    run_pure_extractor,
)


# =============================================================================
# V52-O.11.B (v0.2.53 Track E) — Svelte parser helpers.
#
# Before v0.2.53, the orchestrator's 244 .svelte files in launcher/src/
# were indexed as ZERO functions in the code graph (they fell through
# every language detector because no extension match existed). This
# module-level helper extracts the parsing logic from
# `_analyze_svelte_file` so it can be unit-tested in isolation.
#
# Svelte component structure:
#   * Optional top-level <script lang="ts"|"js"> block — host for
#     reactive state, lifecycle hooks, and named functions.
#   * Optional <script context="module"> block — module-level state
#     and helpers shared across component instances (own scope).
#   * Optional <style> block — CSS / preprocessor. Ignored for
#     code-graph purposes.
#   * Template body — HTML-like markup, mustaches, control flow.
#     Ignored for code-graph purposes (no semantic functions there).
#
# The parser:
#   1. Extracts the <script> block bodies (both default + module).
#   2. Parses top-level `function name(...)` declarations.
#   3. Parses top-level `export function name(...)` exports.
#   4. Parses arrow-function exports
#      (`export const name = () => ...` / `let` / `var`).
#   5. Parses `$: name = ...` reactive declarations as pseudo-functions
#      (they're function-shaped — a reactive expression — and useful
#      to surface in code-graph search even though they're not
#      callable directly).
#   6. Extracts the component name from the file stem (Svelte
#      convention; no class-name analogue inside the file).
# =============================================================================


# Match an opening <script ...> tag, capturing the full opening tag for
# attribute inspection (we look for context="module" to label module-
# scoped blocks).
_SVELTE_SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SVELTE_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_SVELTE_MODULE_CONTEXT = re.compile(
    r"""context\s*=\s*['"]module['"]""", re.IGNORECASE
)

# `function name(...)` and `export function name(...)`. We capture the
# `export` prefix so the caller can tag exported functions.
_SVELTE_FUNCTION_DECL = re.compile(
    r"""^[ \t]*(export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(""",
    re.MULTILINE,
)

# Arrow-function exports: `export const name = (...) => ...`,
# `export let name = ...`, `export var name = ...`. Bound to const/let/var
# so we don't pick up arbitrary `name = (...) => ...` re-assignments
# inside function bodies.
_SVELTE_ARROW_EXPORT = re.compile(
    r"""^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"""
    r"""(async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>""",
    re.MULTILINE,
)

# Svelte reactive declarations: `$: name = ...`. We treat each as a
# pseudo-function because they're function-shaped reactive expressions
# (an effect that re-runs on dependency change). Surfaces in code-graph
# searches for "what reactive declarations exist".
_SVELTE_REACTIVE_DECL = re.compile(
    r"""^[ \t]*\$:\s*([A-Za-z_$][\w$]*)\s*=""",
    re.MULTILINE,
)


def _extract_svelte_script_blocks(
    content: str,
) -> List[Tuple[str, bool, int]]:
    """Extract <script>...</script> bodies from a Svelte source.

    Returns a list of (block_body, is_module_context, body_start_offset)
    tuples. `body_start_offset` is the absolute character offset in the
    original content of the first character of the block body — used by
    callers to translate per-block match offsets back to file-level
    line numbers.

    Multi-block tolerance: a Svelte file may have at most one default
    <script> + at most one <script context="module">; we return all
    matches we find rather than enforcing this constraint at parse
    time (let the orchestrator's downstream linters surface invalid
    Svelte structure).
    """
    blocks: List[Tuple[str, bool, int]] = []
    pos = 0
    while pos < len(content):
        open_match = _SVELTE_SCRIPT_OPEN.search(content, pos)
        if not open_match:
            break
        attrs = open_match.group(1) or ""
        is_module = bool(_SVELTE_MODULE_CONTEXT.search(attrs))
        body_start = open_match.end()
        close_match = _SVELTE_SCRIPT_CLOSE.search(content, body_start)
        if not close_match:
            # Unclosed script — treat the rest of the file as the body.
            blocks.append((content[body_start:], is_module, body_start))
            break
        blocks.append(
            (content[body_start:close_match.start()], is_module, body_start)
        )
        pos = close_match.end()
    return blocks


def _parse_svelte_functions(content: str) -> List[Dict[str, Any]]:
    """Parse top-level function-shaped declarations from a Svelte file.

    Returns a list of dicts with keys:

      * `name` (str)         — function / variable / reactive name
      * `kind` (str)         — one of `"function"`, `"export"`,
                              `"arrow_export"`, `"reactive"`
      * `is_async` (bool)    — True for `async function`, `async () =>`,
                              False for sync forms and reactive decls
      * `start_offset` (int) — character offset in the ORIGINAL file
                              content where the declaration starts
                              (callers translate this to a line
                              number via `content[:off].count('\n') + 1`)
      * `module_scope` (bool) — True when the declaration appeared
                              inside a `<script context="module">`
                              block; False for default-script and
                              fallback cases.

    Deduplication: if the same name appears as both a `function` decl
    and an `arrow_export` (unusual but possible if a file defines a
    helper and then re-exports a const-arrow with the same name), we
    keep BOTH entries — the dedup contract is the caller's job at
    insert time (the orchestrator's `_dedup_insert` keys on
    `full_name` + `file_path_rel`, which preserves the distinction).
    """
    results: List[Dict[str, Any]] = []
    blocks = _extract_svelte_script_blocks(content)

    # Empty-block fallback: no <script> at all → no functions to extract.
    if not blocks:
        return results

    for block_body, is_module, body_start in blocks:
        # `function` / `export function` / `async function`
        for m in _SVELTE_FUNCTION_DECL.finditer(block_body):
            name = m.group(2)
            kind = "export" if m.group(1) else "function"
            is_async = "async" in block_body[m.start():m.end()]
            results.append(
                {
                    "name": name,
                    "kind": kind,
                    "is_async": is_async,
                    "start_offset": body_start + m.start(),
                    "module_scope": is_module,
                }
            )

        # `export const name = (...) => ...` (+ let / var)
        for m in _SVELTE_ARROW_EXPORT.finditer(block_body):
            name = m.group(1)
            is_async = m.group(2) is not None
            results.append(
                {
                    "name": name,
                    "kind": "arrow_export",
                    "is_async": is_async,
                    "start_offset": body_start + m.start(),
                    "module_scope": is_module,
                }
            )

        # `$: name = ...` reactive declarations (Svelte-specific).
        # Only meaningful in the DEFAULT script (module context blocks
        # don't get the reactive runtime), so we skip them in
        # module-scoped blocks.
        if not is_module:
            for m in _SVELTE_REACTIVE_DECL.finditer(block_body):
                name = m.group(1)
                results.append(
                    {
                        "name": name,
                        "kind": "reactive",
                        "is_async": False,
                        "start_offset": body_start + m.start(),
                        "module_scope": False,
                    }
                )

    # Stable sort by start_offset so test assertions are deterministic
    # even when the regex iterators visit declarations in a non-source
    # order (which they don't today, but stable-sort future-proofs).
    results.sort(key=lambda r: r["start_offset"])
    return results


def extract_svelte_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a Svelte component (V52-O.11.B), RETURN a
    :class:`FileExtraction`.

    Extracts top-level functions, exports, arrow-export consts, and
    reactive declarations from <script> and <script context="module">
    blocks. The component name is taken from the file stem (Svelte
    convention; the file IS the component) and recorded as the
    module summary alongside the imports list.

    Imports come from ES module syntax in any <script> block. Class
    declarations are extracted too if present (Svelte allows
    utility classes inside a component's script block).
    """
    content = source_text
    source_lines = content.split('\n')
    loc = len([l for l in source_lines if l.strip()])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    component_name = file_path.stem

    # --- Imports across all <script> blocks ---
    imports: List[str] = []
    for block_body, _is_module, _start in _extract_svelte_script_blocks(content):
        for m in re.finditer(
            r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+)?['"]([^'"]+)['"]""",
            block_body,
        ):
            imports.append(m.group(1))

    # --- Module summary ---
    # Component name + first non-empty HTML comment from the template,
    # if present (the Svelte convention is `<!-- ... -->` at file top).
    leading_doc = ''
    m_html_comment = re.search(r"<!--\s*(.*?)\s*-->", content, re.DOTALL)
    if m_html_comment:
        leading_doc = m_html_comment.group(1).strip().split('\n')[0][:200]
    summary_parts = [f"Svelte component: {component_name}"]
    if leading_doc:
        summary_parts.append(leading_doc)
    module_summary = '\n'.join(summary_parts)

    # Cyclomatic-ish complexity: count common branches/loops across
    # script + template (matches the JS analyser's heuristic). Cheap
    # over-approximation — exact CFG analysis would need a Svelte
    # AST parser which we don't bundle.
    complexity = float(
        1 + sum(content.count(kw) for kw in [
            'if (', 'if(', '{#if ', '{:else if ', '{#each ', '{#await ',
            '? ', 'while (', 'while(', 'for (', 'for('
        ])
    )

    module = ModuleDescriptor(
        path=relative_path,
        language="Svelte",
        loc=loc,
        complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash,
        imports=imports,
        module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # --- Functions / arrow exports / reactive decls ---
    for decl in _parse_svelte_functions(content):
        name: str = decl["name"]
        start_line = content[:decl["start_offset"]].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])

        kind = decl["kind"]
        is_async = bool(decl["is_async"])
        if kind == "reactive":
            signature = f"$: {name} = ..."
        elif kind == "arrow_export":
            signature = (
                f"{'async ' if is_async else ''}const {name} = (...) => ..."
            )
        else:
            # function / export function
            prefix = "export " if kind == "export" else ""
            signature = (
                f"{prefix}{'async ' if is_async else ''}function {name}()"
            )

        full_name = f"{component_name}.{name}"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=name,
            full_name=full_name,
            body=body,
            signature=signature,
            doc="",
            start_line=start_line,
            end_line=end_line,
            is_async=is_async,
            project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="javascript")
            ),
        ))
        stats['functions'] += 1

    return FileExtraction(
        module=module, entities=entities, interactions=[],
        imports=[], stats=stats,
    )


def analyze_svelte_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_svelte_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
