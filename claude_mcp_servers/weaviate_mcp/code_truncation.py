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

    elif language in ("javascript", "typescript", "java", "csharp", "go", "rust", "cpp"):
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
