# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PowerShell extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``: the
V52-O.11.N parser helpers (``_strip_powershell_comments`` /
``_parse_powershell_functions`` + their regexes — including the v0.2.75 P1b
deep-indent fix, regression-tested in ``tests/test_powershell_parser.py``)
and ``CodeGraphAnalyzer._analyze_powershell_file`` (only body edits: the
mechanical ``self.`` -> ``ctx.`` rename and the analyzer-resident
``embed_function`` seam reached as ``ctx.embed_function``). Behavior is
pinned byte-identically by ``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vco_lib.codegraph_entities import CodeEntity, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _is_minified_content,
)


# =============================================================================
# V52-O.11.N (v0.2.53 Track E) — PowerShell parser helpers.
#
# Before v0.2.53, the orchestrator's 168 .ps1 files (template hooks
# and Windows-side scripts) were indexed as ZERO functions in the code
# graph — same fall-through bug as Svelte (no extension match).
#
# PowerShell function-declaration syntax:
#
#   function Name { ... }
#   function Name() { ... }
#   function Name($a, $b) { ... }
#   function Name {
#     param([Parameter()] $a, [Parameter()] $b)
#     ...
#   }
#   function global:Name { ... }                  # scope prefix
#   function script:Name { ... }                  # scope prefix
#   function Verb-Noun { ... }                    # idiomatic PS naming
#   filter Name { ... }                           # filter is also fn-like
#
# We capture `function` and `filter` declarations and tolerate the
# optional scope prefix + the parenthesised parameter list. We also
# extract any leading `param(...)` block as the function's signature
# extra — useful for code-graph search by parameter type.
#
# Comment regions:
#   `# comment`           — single-line
#   `<# ... #>`           — multi-line block comment
#   `#region` / `#endregion` — folding markers; we strip them as
#                              comments (no semantic meaning beyond
#                              IDE folding).
# =============================================================================


# `function name { ... }` / `function name(...) { ... }` / `filter name { ... }`.
# Matches both `function` and `filter`; captures the optional scope
# prefix (`global:`, `script:`, `local:`, `private:`) and the name.
# Trailing `(...)` or `{` is required so we don't pick up bare
# `function` keyword mentions.
# v0.2.75 (P1b): the keyword is CAPTURED (`kind` group) instead of being
# re-derived by slicing the first 8 characters of the match. The old slice
# (`cleaned[line_start:m.start() + 8].strip().split()[0]`) raised IndexError
# on any declaration indented by >= 8 whitespace chars — `^[ \t]*` is part of
# the match, so the first 8 chars were ALL whitespace, strip() emptied them
# and split()[0] blew up. Live incident: a nested hook function at 8-space
# indent crashed the per-file walk deterministically on EVERY run; the R-3
# module-row invalidation then re-stamped the file's module row to
# embed_revision=0 each time, so the resync owed-probe counted it forever —
# an immortal convergence loop that looked like a "vectorless sentinel that
# never heals". Regression: tests/test_powershell_parser.py (deep-indent).
_POWERSHELL_FUNCTION_DECL = re.compile(
    r"""^[ \t]*(?P<kind>function|filter)\s+"""
    r"""(?:(?P<scope>global|script|local|private):)?"""
    r"""(?P<name>[A-Za-z_][\w-]*)\s*"""
    r"""(?:\([^)]*\))?\s*"""
    r"""(?=\{)""",
    re.MULTILINE | re.IGNORECASE,
)

# `[Parameter(...)] $name` / `[Parameter()] [string]$name` etc. We
# capture the parameter name (`$name`) and the bracketed attributes
# above it so callers can render a richer signature in the code-graph
# entity.
_POWERSHELL_PARAM_ATTR = re.compile(
    r"""\[\s*Parameter\s*\([^)]*\)\s*\]"""
    r"""(?:\s*\[[^\]]+\])*"""
    r"""\s*\$(?P<name>[A-Za-z_][\w]*)""",
    re.IGNORECASE,
)


def _strip_powershell_comments(content: str) -> str:
    """Strip PowerShell single-line and block comments from source.

    Order matters: block comments (`<# ... #>`) come first so that
    `#` inside a block comment isn't picked up by the single-line
    pattern. `#region` / `#endregion` markers are stripped via the
    single-line pass.
    """
    # Block comments `<# ... #>` (greedy across lines).
    stripped = re.sub(r"<#.*?#>", " ", content, flags=re.DOTALL)
    # Single-line `#` to end-of-line. Includes `#region`, `#endregion`,
    # `#!` shebangs (not idiomatic in .ps1 but possible in cross-platform
    # scripts).
    stripped = re.sub(r"#.*$", "", stripped, flags=re.MULTILINE)
    return stripped


def _parse_powershell_functions(content: str) -> List[Dict[str, Any]]:
    """Parse function/filter declarations from a PowerShell source file.

    Returns a list of dicts with keys:

      * `name` (str)        — function name (without scope prefix)
      * `scope` (str|None)  — `"global"` / `"script"` / `"local"` /
                              `"private"` / None when unscoped
      * `kind` (str)        — `"function"` or `"filter"`
      * `start_offset` (int) — character offset in the ORIGINAL
                               content; callers translate to a line
                               number via `content[:off].count('\n') + 1`
      * `params` (List[str]) — parameter names from a `param(...)`
                               block if present, else []. Parameter
                               names are returned WITHOUT the leading
                               `$` sigil.

    The parser strips comments first so block-comment text doesn't
    masquerade as a function decl (a common .ps1 footgun: docstring-
    style `<#  function Foo  #>` blocks above the real declaration).
    """
    cleaned = _strip_powershell_comments(content)
    results: List[Dict[str, Any]] = []

    for m in _POWERSHELL_FUNCTION_DECL.finditer(cleaned):
        name = m.group("name")
        scope = m.group("scope")
        # v0.2.75 (P1b): the kind comes straight from the regex capture —
        # the old first-8-chars re-slice raised IndexError on >=8-char
        # indentation (see the regex comment above for the live incident).
        kind = "filter" if m.group("kind").lower() == "filter" else "function"

        # Find the function body span to look for a `param(...)` block.
        # We scan from the brace (`{`) following the decl forward to
        # the matching close brace, balancing nesting.
        body_start = cleaned.find("{", m.end())
        params: List[str] = []
        if body_start != -1:
            depth = 1
            body_end = body_start + 1
            while body_end < len(cleaned) and depth > 0:
                ch = cleaned[body_end]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                body_end += 1
            body = cleaned[body_start:body_end]

            # Look for `param( ... )` at top of body. We can't use a
            # bare `\(...\)` regex because the param block contains
            # nested parens (`[Parameter()]` attributes), and `[^)]*`
            # stops at the first close paren which truncates the
            # block before the first $-variable. Hand-roll the
            # balanced-paren scan instead.
            param_keyword = re.search(
                r"\bparam\s*\(",
                body,
                re.IGNORECASE,
            )
            if param_keyword:
                paren_start = param_keyword.end() - 1  # index of the `(`
                depth = 1
                pos = paren_start + 1
                while pos < len(body) and depth > 0:
                    ch = body[pos]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    pos += 1
                # `param_block` is the content BETWEEN the outer
                # parens (depth was decremented to 0 on the close,
                # so pos is one past it).
                param_block = body[paren_start + 1:pos - 1]
                # Capture `$name` occurrences. We don't constrain on
                # the leading `[Parameter()]` attribute because not
                # all params carry the attribute (positional / simple
                # params are valid too).
                for pm in re.finditer(
                    r"\$([A-Za-z_][\w]*)",
                    param_block,
                ):
                    pname = pm.group(1)
                    if pname not in params:
                        params.append(pname)

        results.append(
            {
                "name": name,
                "scope": scope,
                "kind": kind,
                "start_offset": m.start(),
                "params": params,
            }
        )

    results.sort(key=lambda r: r["start_offset"])
    return results


def analyze_powershell_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Analyze a PowerShell script file (V52-O.11.N, v0.2.53 Track E).

    Extracts function / filter declarations + their `param(...)`
    blocks. Imports are best-effort: `Import-Module Foo`,
    `. .\\Path\\Common.ps1` (dot-sourcing). External calls are
    gated on `Invoke-WebRequest` / `Invoke-RestMethod` presence
    in content (same gating pattern as the shell analyser).
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
               if l.strip() and not l.strip().startswith('#')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return stats

    cleaned = _strip_powershell_comments(content)

    # --- Imports ---
    imports: List[str] = []
    # `Import-Module Foo` / `Import-Module -Name Foo`
    for m in re.finditer(
        r"\bImport-Module\b\s+(?:-Name\s+)?['\"]?([\w./-]+)['\"]?",
        cleaned,
        re.IGNORECASE,
    ):
        imports.append(m.group(1))
    # Dot-source: `. .\Common.ps1` / `. $PSScriptRoot\Common.ps1`
    for m in re.finditer(
        r"^\s*\.\s+([\w$.\\/-]+\.ps1)",
        cleaned,
        re.MULTILINE,
    ):
        imports.append(m.group(1))

    # --- Module summary ---
    # Look for a leading `<# .SYNOPSIS ... #>` block; fall back to
    # the first single-line comment.
    synopsis = ''
    m_syn = re.search(
        r"<#\s*\.SYNOPSIS\s+(.*?)\s*(?:\.[A-Z]+|\#>)",
        content,
        re.DOTALL,
    )
    if m_syn:
        synopsis = m_syn.group(1).strip().split('\n')[0][:200]
    else:
        for line in source_lines[:20]:
            s = line.strip()
            if (
                s.startswith('#')
                and not s.startswith('#!')
                and not s.startswith('#region')
                and s.lstrip('#').strip()
            ):
                synopsis = s.lstrip('#').strip()
                break
    summary_parts = [f"PowerShell script: {relative_path}"]
    if synopsis:
        summary_parts.append(synopsis)
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(
        cleaned.count(kw) for kw in [
            'if (', 'if(', 'elseif ', 'while (', 'while(',
            'for (', 'for(', 'foreach (', 'foreach(',
            'switch (', 'switch(',
        ]
    ))

    module_uuid = ctx._create_or_update_module(
        path=relative_path,
        language="PowerShell",
        loc=loc,
        complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash,
        imports=imports,
        module_summary=module_summary,
    )
    stats['modules'] = 1

    # --- Functions / filters ---
    for decl in _parse_powershell_functions(content):
        name: str = decl["name"]
        scope: Optional[str] = decl["scope"]
        kind: str = decl["kind"]
        params: List[str] = decl["params"]

        # Re-derive line numbers from the ORIGINAL content (the
        # parser used a comment-stripped copy, so its offsets are
        # not directly comparable to the original; re-search by
        # name to find the start line for the orchestrator's
        # entity insertion).
        #
        # We anchor on the `function NAME` / `filter NAME` pattern
        # at start-of-line in the original content. If multiple
        # functions share a name (e.g. accidental redefinition),
        # the first match wins — the orchestrator's `_dedup_insert`
        # dedup on `full_name` + `file_path_rel` then either
        # collapses them or surfaces the dedup conflict downstream.
        scope_prefix = f"{scope}:" if scope else ""
        anchor = re.compile(
            r"^\s*(?:function|filter)\s+"
            + re.escape(scope_prefix)
            + re.escape(name)
            + r"\b",
            re.MULTILINE | re.IGNORECASE,
        )
        anchor_match = anchor.search(content)
        if not anchor_match:
            continue
        start_line = content[:anchor_match.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])

        param_sig = ", ".join(f"${p}" for p in params)
        signature = (
            f"{kind} {scope_prefix}{name}({param_sig})"
            if params
            else f"{kind} {scope_prefix}{name}"
        )
        full_name = f"{file_path.stem}.{name}"
        ctx.store_entity(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=name,
            full_name=full_name,
            body=body,
            signature=signature,
            doc="",
            start_line=start_line,
            end_line=end_line,
            is_async=False,
            project=ctx.project_name,
            references={"module": module_uuid},
            deferred_embed=lambda: ctx.embed_function(signature, body, language="python"),
        ))
        stats['functions'] += 1

    return stats
