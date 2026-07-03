# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Smart code truncation for embedding models.

Instead of naive character-based truncation (body[:500]), this module
truncates code intelligently to preserve the most semantically meaningful
parts within the embedding model's token budget.

Priority order for code embedding text:
  1. Signature / declaration (always included)
  2. Docstring / leading comment (always included if present)
  3. Method/field names (for classes)
  4. Body — truncated at statement boundaries, not mid-line

Token budget is model-aware:
  - CodeSage-Large-v2: 2048 tokens (~8000 chars)
  - jina-v2-base-code: 8192 tokens (~32000 chars)
  - Fallback: 2048 tokens (conservative)
"""

from __future__ import annotations

import os
import re

# Model token limits for code embedding models.
# Chars-per-token ratio for code is ~3.5 (more punctuation than prose).
CODE_MODEL_TOKEN_LIMITS: dict[str, int] = {
    "codesage/codesage-large-v2": 2048,
    "codesage-large-v2": 2048,
    "unclemusclez/jina-embeddings-v2-base-code:latest": 8192,
    "jina-embeddings-v2-base-code": 8192,
}

_CHARS_PER_TOKEN = 3.5  # conservative for code
_DEFAULT_TOKEN_LIMIT = 2048


def _max_chars_for_model(model: str | None = None) -> int:
    """Return max character budget for a given code embedding model."""
    if model is None:
        model = os.getenv("CODE_EMBED_MODEL", "codesage/codesage-large-v2")
    token_limit = CODE_MODEL_TOKEN_LIMITS.get(model, _DEFAULT_TOKEN_LIMIT)
    return int(token_limit * _CHARS_PER_TOKEN)


def _extract_docstring(body: str, language: str = "python") -> str:
    """Extract leading docstring or comment block from a function/class body."""
    lines = body.split("\n")
    doc_lines: list[str] = []

    if language == "python":
        # Look for triple-quoted docstring
        in_doc = False
        for line in lines[1:]:  # skip first line (def/class)
            stripped = line.strip()
            if not in_doc:
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    in_doc = True
                    doc_lines.append(stripped)
                    if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                        break  # single-line docstring
                elif stripped.startswith("#"):
                    doc_lines.append(stripped)
                elif stripped == "":
                    continue
                else:
                    break  # hit actual code
            else:
                doc_lines.append(stripped)
                if '"""' in stripped or "'''" in stripped:
                    break

    elif language in ("javascript", "typescript", "java", "csharp", "go", "rust", "cpp",
                      # v0.2.73 (M3): svelte functions come from <script>
                      # blocks — js/ts comment styles apply verbatim.
                      "svelte"):
        # Look for /** ... */ or // comments
        in_block = False
        for line in lines[1:]:
            stripped = line.strip()
            if not in_block:
                if stripped.startswith("/**") or stripped.startswith("/*"):
                    in_block = True
                    doc_lines.append(stripped)
                    if stripped.endswith("*/"):
                        break
                elif stripped.startswith("//") or stripped.startswith("///"):
                    doc_lines.append(stripped)
                elif stripped == "":
                    continue
                else:
                    break
            else:
                doc_lines.append(stripped)
                if stripped.endswith("*/"):
                    break

    elif language in ("ruby", "shell", "lua"):
        # v0.2.73 (M3): leading `#` comment block (lua also `--`). NOTE:
        # this function feeds the EMBED text assembly too — for these NEW
        # languages the embed text gains the docstring ahead of the body on
        # the next re-analyze of a CHANGED file (same content, better
        # ordering; no stored-vector invalidation, no revision bump).
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("#") or (language == "lua" and stripped.startswith("--")):
                doc_lines.append(stripped)
            elif stripped == "":
                if doc_lines:
                    break
                continue
            else:
                break

    elif language == "powershell":
        # v0.2.73 (M3): `<# ... #>` comment block (comment-based help) or
        # leading `#` lines.
        in_block = False
        for line in lines[1:]:
            stripped = line.strip()
            if not in_block:
                if stripped.startswith("<#"):
                    in_block = True
                    doc_lines.append(stripped)
                    if stripped.endswith("#>"):
                        break
                elif stripped.startswith("#"):
                    doc_lines.append(stripped)
                elif stripped == "":
                    continue
                else:
                    break
            else:
                doc_lines.append(stripped)
                if stripped.endswith("#>"):
                    break

    return "\n".join(doc_lines)


def _extract_method_signatures(class_body: str, language: str = "python") -> list[str]:
    """Extract method/function signatures from a class body."""
    sigs: list[str] = []

    if language == "python":
        for match in re.finditer(r"^\s+(def\s+\w+\s*\([^)]*\))", class_body, re.MULTILINE):
            sigs.append(match.group(1).strip())
    elif language in ("javascript", "typescript"):
        for match in re.finditer(r"^\s+(?:async\s+)?(\w+\s*\([^)]*\))\s*[{:]", class_body, re.MULTILINE):
            sigs.append(match.group(1).strip())
    elif language in ("java", "csharp", "cpp"):
        for match in re.finditer(
            r"^\s+(?:public|private|protected|static|async|virtual|override|\s)*"
            r"(\w+\s+\w+\s*\([^)]*\))",
            class_body, re.MULTILINE,
        ):
            sigs.append(match.group(1).strip())
    elif language == "go":
        for match in re.finditer(r"^func\s+(\([^)]*\)\s+\w+\s*\([^)]*\))", class_body, re.MULTILINE):
            sigs.append(match.group(1).strip())
    elif language == "rust":
        for match in re.finditer(r"^\s+(?:pub\s+)?fn\s+(\w+\s*\([^)]*\))", class_body, re.MULTILINE):
            sigs.append(match.group(1).strip())

    return sigs


def truncate_function_for_embedding(
    signature: str,
    body: str,
    language: str = "python",
    model: str | None = None,
) -> str:
    """Smart truncation of a function for embedding.

    Prioritizes: signature > docstring > body (at statement boundaries).
    Respects the embedding model's token limit.

    Args:
        signature: Function signature (e.g. "def foo(x, y)")
        body: Full function body
        language: Programming language
        model: Code embedding model name (for token budget)

    Returns:
        Truncated text suitable for embedding
    """
    max_chars = _max_chars_for_model(model)

    parts: list[str] = [signature]
    used = len(signature)

    # Add docstring
    docstring = _extract_docstring(body, language)
    if docstring and used + len(docstring) + 1 < max_chars:
        parts.append(docstring)
        used += len(docstring) + 1

    # Add body, truncated at line boundaries
    remaining = max_chars - used - 1
    if remaining > 50:
        # Skip signature line(s) and docstring lines already included
        body_lines = body.split("\n")
        skip = 1  # skip first line (signature)
        if docstring:
            skip += len(docstring.split("\n"))

        truncated_lines: list[str] = []
        char_count = 0
        for line in body_lines[skip:]:
            line_len = len(line) + 1  # +1 for newline
            if char_count + line_len > remaining:
                break
            truncated_lines.append(line)
            char_count += line_len

        if truncated_lines:
            parts.append("\n".join(truncated_lines))

    return "\n".join(parts)


def truncate_class_for_embedding(
    signature: str,
    class_body: str,
    methods: list[str] | None = None,
    language: str = "python",
    model: str | None = None,
) -> str:
    """Smart truncation of a class for embedding.

    Prioritizes: signature > docstring > method signatures > body.
    Respects the embedding model's token limit.

    Args:
        signature: Class signature (e.g. "class Foo(Base)")
        class_body: Full class body
        methods: Pre-extracted method names (optional, for fallback)
        language: Programming language
        model: Code embedding model name (for token budget)

    Returns:
        Truncated text suitable for embedding
    """
    max_chars = _max_chars_for_model(model)

    parts: list[str] = [signature]
    used = len(signature)

    # Add docstring
    docstring = _extract_docstring(class_body, language)
    if docstring and used + len(docstring) + 1 < max_chars:
        parts.append(docstring)
        used += len(docstring) + 1

    # Add method signatures
    method_sigs = _extract_method_signatures(class_body, language)
    if method_sigs:
        sig_text = "Methods: " + ", ".join(method_sigs)
        if used + len(sig_text) + 1 < max_chars:
            parts.append(sig_text)
            used += len(sig_text) + 1
    elif methods:
        # Fallback: use pre-extracted method names
        sig_text = "Methods: " + ", ".join(methods[:20])
        if used + len(sig_text) + 1 < max_chars:
            parts.append(sig_text)
            used += len(sig_text) + 1

    # Add remaining body at line boundaries
    remaining = max_chars - used - 1
    if remaining > 50:
        body_lines = class_body.split("\n")
        skip = 1
        if docstring:
            skip += len(docstring.split("\n"))

        truncated_lines: list[str] = []
        char_count = 0
        for line in body_lines[skip:]:
            line_len = len(line) + 1
            if char_count + line_len > remaining:
                break
            truncated_lines.append(line)
            char_count += line_len

        if truncated_lines:
            parts.append("\n".join(truncated_lines))

    return "\n".join(parts)


def truncate_module_for_embedding(
    module_summary: str,
    model: str | None = None,
) -> str:
    """Truncate module summary for embedding. Usually short enough to fit."""
    max_chars = _max_chars_for_model(model)
    if len(module_summary) <= max_chars:
        return module_summary
    # Truncate at line boundary
    lines = module_summary.split("\n")
    result: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        result.append(line)
        used += len(line) + 1
    return "\n".join(result)


# ---------------------------------------------------------------------------
# v0.2.72 P3 — model-aware chunking for the ~7-9% of functions/classes that
# EXCEED the embedding budget (measured 8.6% CodeFunction).
#
# The `truncate_*_for_embedding` functions above are lossy: when an entity is
# larger than `_max_chars_for_model`, everything past the budget is silently
# dropped from the embedding — so a large function's tail is unsearchable.
# For the common case (91%+ of entities fit) that loss never happens. For the
# over-budget tail, chunking splits the assembled priority-text into N chunks
# so EVERY line of the entity contributes to some chunk's embedding.
#
# The chunk header format MUST match `server._parse_chunk_header`, which
# accepts `^\[chunk (\d+)/(\d+)\]\n\n` with a 1-INDEXED chunk number in the
# header text. The stored `chunk_num` property is 0-INDEXED (see P3 spec and
# the analyze_code_graph store path). So header text uses `i+1`; the Weaviate
# property uses `i`. The FIRST chunk (i==0) keeps the priority-truncated
# signature+docstring so signature is always in chunk 0.
# ---------------------------------------------------------------------------

# Chunk-header format shared with `server._parse_chunk_header`. The 1-indexed
# number and trailing blank line are load-bearing — the reader regex is
# `^\[chunk (\d+)/(\d+)\]\n\n`.
def _chunk_header(one_indexed: int, total: int) -> str:
    """Build the `[chunk N/total]` header (1-indexed N) that the read path parses."""
    return f"[chunk {one_indexed}/{total}]\n\n"


def _assemble_priority_head(
    signature: str,
    body: str,
    language: str,
) -> str:
    """Signature + docstring only (the always-kept priority head).

    This is what leads chunk 0 so the most semantically-meaningful part of the
    entity (declaration + docstring) is guaranteed present in the canonical
    chunk regardless of how the body is split.
    """
    parts: list[str] = [signature]
    docstring = _extract_docstring(body, language)
    if docstring:
        parts.append(docstring)
    return "\n".join(parts)


def _body_without_priority_lines(body: str, language: str) -> str:
    """The body MINUS the lines already carried by the priority head.

    F11-v (pre-gate audit): the over-budget test used to compare
    ``priority_head + "\\n" + body`` against the budget — but ``body`` STILL
    CONTAINS the signature line and the docstring the head already carries,
    so the docstring was counted twice and a borderline entity chunked (N=2
    objects, headers, extra embeds) when the DEDUPed assembly actually fits.

    Mirrors the skip logic of ``truncate_function_for_embedding`` /
    ``truncate_class_for_embedding`` (skip the first line + the docstring's
    line count) so the over-budget test measures exactly what the in-budget
    single text assembles.
    """
    lines = body.split("\n")
    skip = 1  # first line (signature)
    docstring = _extract_docstring(body, language)
    if docstring:
        skip += len(docstring.split("\n"))
    return "\n".join(lines[skip:])


def chunk_or_truncate_for_embedding(
    signature: str,
    body: str,
    language: str = "python",
    model: str | None = None,
    *,
    full_name: str = "",
) -> list[str]:
    """Return a list of embedding texts for one function/class entity.

    Common case (91%+): the priority-assembled text (signature > docstring >
    body-at-line-boundaries, via `truncate_function_for_embedding`) already
    fits the model budget → returns ``[single_text]`` with the FULL body and
    NO chunk header (identical to the pre-chunking single-object behaviour).

    Over-budget case (~7-9%): the entity is larger than the budget. We split
    the FULL assembled text (signature + docstring + full body) into N chunks
    sized for the model via ``Chunker.for_model(model)`` (reusing the KG
    chunker — small_context = (550,1600,1100) for CodeSage), then prefix each
    chunk with a ``[chunk i/N]`` header (1-indexed in the header text). The
    first chunk leads with the priority head (signature + docstring) so the
    signature is always searchable in chunk 0.

    Args:
        signature: Entity signature (e.g. "def foo(x, y)").
        body: Full entity body.
        language: Programming language (for docstring extraction).
        model: Code embedding model name (for budget + chunker preset).
        full_name: Fully-qualified entity name — used as the chunker
            ``source_id`` so chunk provenance is traceable. Optional.

    Returns:
        A list of embedding-ready strings. Length 1 for the common
        (in-budget) case; length N (>= 2) for the chunked case. NEVER empty
        (a blank entity returns ``[signature]``).
    """
    max_chars = _max_chars_for_model(model)

    # The priority-assembled text is what we'd embed today. If it fits, we're
    # in the common case: return it verbatim (FULL body, no header, one object).
    single = truncate_function_for_embedding(signature, body, language=language, model=model)

    # Decide over-budget purely on the FULL assembled text (signature + full
    # body), NOT on the already-truncated `single` — `single` is capped at
    # max_chars by construction, so it can never look over-budget. The real
    # question is "did truncation drop content?", i.e. is the full text bigger
    # than the budget?
    #
    # F11-v (pre-gate audit): assemble head + body-WITHOUT-the-head's-lines
    # (mirrors the in-budget path's skip logic) — appending the raw body
    # double-counted the docstring, chunking borderline entities that fit.
    priority_head = _assemble_priority_head(signature, body, language)
    body_rest = _body_without_priority_lines(body, language) if body else ""
    full_text = f"{priority_head}\n{body_rest}" if body_rest else priority_head

    if len(full_text) <= max_chars:
        # In budget → the single truncated text already carries the whole
        # entity (truncation dropped nothing). Return it unchunked.
        return [single]

    # Over budget → chunk the FULL text so every line contributes to some
    # chunk's embedding. Reuse the KG chunker (do NOT reimplement chunking).
    try:
        from weaviate_mcp.chunking import Chunker
    except ImportError:  # pragma: no cover — same-package import, defensive only
        from .chunking import Chunker  # type: ignore[no-redef]

    resolved_model = model or os.getenv("CODE_EMBED_MODEL", "codesage/codesage-large-v2")
    chunker = Chunker.for_model(resolved_model)
    source_id = full_name or signature
    chunks = chunker.chunk_text(text=full_text, source_id=source_id, metadata={})

    if not chunks:
        # Defensive: chunker returned nothing (blank text) — fall back to the
        # single truncated form so the entity is never lost entirely.
        return [single]

    if len(chunks) == 1:
        # Chunker produced a single chunk (rare — full_text barely over budget
        # but the chunker's max_tokens window still swallowed it whole). Treat
        # as the in-budget case: one object, no header, no truncation loss.
        return [chunks[0].content]

    total = len(chunks)
    texts: list[str] = []
    for i, chunk in enumerate(chunks):
        header = _chunk_header(i + 1, total)  # 1-indexed in the header text
        if i == 0:
            # Guarantee the signature+docstring lead chunk 0. The chunker's
            # first chunk already begins with the priority head (full_text
            # starts with it), but re-prefix defensively so signature is
            # present even if a boundary landed oddly.
            content = chunk.content
            if signature and not content.lstrip().startswith(signature.strip()[:40]):
                content = f"{priority_head}\n{content}"
            texts.append(f"{header}{content}")
        else:
            texts.append(f"{header}{chunk.content}")
    return texts


def chunk_or_truncate_class_for_embedding(
    signature: str,
    class_body: str,
    methods: list[str] | None = None,
    language: str = "python",
    model: str | None = None,
    *,
    full_name: str = "",
) -> list[str]:
    """Class variant of :func:`chunk_or_truncate_for_embedding`.

    Common case → ``[truncate_class_for_embedding(...)]`` (full-body, no
    header). Over-budget → N chunks, chunk 0 leading with signature +
    docstring + method-signature summary so the class's API surface is always
    in the canonical chunk.
    """
    max_chars = _max_chars_for_model(model)
    single = truncate_class_for_embedding(
        signature, class_body, methods=methods, language=language, model=model
    )

    priority_head = _assemble_priority_head(signature, class_body, language)
    method_sigs = _extract_method_signatures(class_body, language)
    if method_sigs:
        priority_head = priority_head + "\nMethods: " + ", ".join(method_sigs)
    elif methods:
        priority_head = priority_head + "\nMethods: " + ", ".join(methods[:20])
    # F11-v: same docstring dedup as the function variant (the method-sig
    # summary line is head-only content, so it stays counted once).
    body_rest = _body_without_priority_lines(class_body, language) if class_body else ""
    full_text = f"{priority_head}\n{body_rest}" if body_rest else priority_head

    if len(full_text) <= max_chars:
        return [single]

    try:
        from weaviate_mcp.chunking import Chunker
    except ImportError:  # pragma: no cover
        from .chunking import Chunker  # type: ignore[no-redef]

    resolved_model = model or os.getenv("CODE_EMBED_MODEL", "codesage/codesage-large-v2")
    chunker = Chunker.for_model(resolved_model)
    source_id = full_name or signature
    chunks = chunker.chunk_text(text=full_text, source_id=source_id, metadata={})

    if not chunks:
        return [single]
    if len(chunks) == 1:
        return [chunks[0].content]

    total = len(chunks)
    texts: list[str] = []
    for i, chunk in enumerate(chunks):
        header = _chunk_header(i + 1, total)
        if i == 0:
            content = chunk.content
            if signature and not content.lstrip().startswith(signature.strip()[:40]):
                content = f"{priority_head}\n{content}"
            texts.append(f"{header}{content}")
        else:
            texts.append(f"{header}{chunk.content}")
    return texts
