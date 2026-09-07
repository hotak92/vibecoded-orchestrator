# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Rust source-scan helpers shared by the ``.rs`` architectural lint tests.

WHY THIS MODULE EXISTS
----------------------
Several tests grep the Rust tree for a forbidden construct (a bare
``println!``, a bare ``tokio::spawn`` in a sync fn, raw binding-table SQL).
Every one of them needs the same two things, and getting either wrong makes
the gate fail toward GREEN:

1. **A Rust-aware lexer** — a brace, or the forbidden token itself, inside a
   string literal, a doc comment or a nested block comment is not code. The
   v0.2.91 bare-prints scan learned this the hard way: a stateless stripper
   mis-read the closing quote of a ``"...text \\``-continued literal as an
   opening one, unbalanced the brace counter, and ended a ``#[cfg(test)]``
   skip 400 lines early.
2. **Per-item ``#[cfg(test)]`` skipping** — cutting the file at the first
   marker is wrong, because a file may gate a mid-file test helper and then
   continue with production code (the v0.2.90 lesson). The skip must follow
   the annotated ITEM: one semicolon-terminated item, or one brace-balanced
   block.

The lexer is NOT reimplemented here. It is
``vco_lib.codegraph_lang._shared._scrub_line_stateful`` — the same
cross-line, per-language scrubber the code-graph extractors use, with its own
dedicated suite (``tests/test_v0291_scrub_language_markers.py``) covering Rust
nested block comments, ``r#"…"#`` raw strings and backslash-continued string
literals. Brace COUNTING is done here rather than through
``_extract_balanced_block`` for one reason: that helper degrades an unbalanced
block to "end of lookahead", and for a skip-region that direction blinds the
rest of the file. Here an unbalanced item yields ``None`` and the caller skips
NOTHING — a lexer mistake can only produce a false POSITIVE (noisy gate), never
a false negative (silent gate).

MIGRATION COMPLETE (extract-before-duplicate, CLAUDE.md)
--------------------------------------------------------
All three lints now share this module. The two that carried private copies
until v0.2.92 were:

* ``tests/test_v0290_no_bare_tokio_spawn_in_sync_fns.py`` —
  ``_skip_cfg_test_item`` + ``_strip_line_comment`` (line-comment stripping
  only; no string/block-comment state);
* ``tests/test_v0291_no_bare_prints_in_rust_crates.py`` — ``_skip_gated_item``
  + ``_cfg_predicate_mentions_test``.

Their ``ScannerBehavior`` / ``RealTreeCfgTestSkipFixture`` suites were the
acceptance tests for that migration and still are. **Do not add a fourth
copy.** If a new lint needs a different question answered, add a PARAMETER
here (as ``include_any_test`` is) rather than a private variant.

v0.2.91's ``_strip_line`` survives inside the bare-prints lint on purpose and
is NOT a leftover duplicate: that scanner needs a code view that BLANKS
removed regions so ``len(code[i])`` still marks where a ``//`` comment begins
(``_comment_text`` / the contract-marker lookup depend on it), whereas
:func:`scrub_rust_lines` DROPS them. Two different outputs for two different
questions — column-preserving vs. token-preserving.

A DELIBERATE SEMANTIC KNOB
--------------------------
``#[cfg(any(test, debug_assertions))]`` compiles in NON-test builds. Whether
that counts as "test code" depends on the question the lint asks:

* "will this print in a user's run?" — no, the item is excluded from release;
  the bare-prints lint therefore treats any predicate MENTIONING test as test
  (``include_any_test=True``).
* "does this SQL exist in a shipped binary?" / "can this ``tokio::spawn``
  panic in a user's launcher?" — yes it can; those gates must still see it.

So ``include_any_test`` is a parameter, defaulting to the STRICT reading:
the item must be absent from EVERY non-test build, evaluated recursively over
the ``cfg`` grammar (see :func:`_predicate_is_test_only`) rather than by
prefix — ``all(unix, any(test, debug_assertions))`` is NOT test-only.

KNOWN LEXER LIMITATION (inherited, NOT introduced here)
--------------------------------------------------------
``_scrub_line_stateful``'s Rust profile marks the plain ``"`` string as not
spanning lines: state is carried to the NEXT line only when the line ends with
a backslash (the C/Java/JS rule). Rust allows a plain ``"…"`` to span lines
with the newline as literal content, so a multi-line literal whose interior
holds unbalanced braces desynchronises brace arithmetic for the rest of that
literal. Live instance: ``launcher/src-tauri/src/logging.rs`` line 273
(a ``"`` opened and immediately backslash-continued), whose body contains
``fn main() {`` … ``}``; the
``#[cfg(test)]`` span there ends at line 299 instead of 300.

Direction of the error is the safe one — a span that ends EARLY means MORE
lines are scanned, i.e. a possible false positive, never a silent miss — which
is why this module ships with it rather than reimplementing the lexer. The fix
belongs in the Rust syntax profile in ``vco_lib/codegraph_lang/_shared.py``
(production code shared with every code-graph extractor), not here.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set, Tuple

from vco_lib.codegraph_lang._shared import _scrub_line_stateful, _syntax_for

__all__ = [
    "scrub_rust_lines",
    "strip_rust_comments",
    "cfg_test_spans",
    "cfg_test_line_numbers",
    "cfg_test_gate_indices",
    "cfg_test_item_resume_index",
]

_RUST_SYNTAX = _syntax_for("rust")

#: An OUTER attribute opening a ``cfg`` predicate: ``#[cfg(`` / ``#[ cfg (``.
_CFG_ATTR_RE = re.compile(r"#\s*\[\s*cfg\s*\(")
#: The INNER form ``#![cfg(test)]`` gates the whole enclosing module/file.
_INNER_CFG_ATTR_RE = re.compile(r"#\s*!\s*\[\s*cfg\s*\(")
#: Any outer attribute (skipped while looking for the gated item).
_ANY_ATTR_RE = re.compile(r"^\s*#\s*\[")
#: ``test`` as a whole identifier inside a predicate.
_TEST_IDENT_RE = re.compile(r"\btest\b")

#: How far to look for the end of a multi-line ``cfg(...)`` predicate, and for
#: the start of the item it annotates. Both are small in real code; the bound
#: only stops a malformed file from being scanned to EOF.
_PREDICATE_LOOKAHEAD = 20
_ITEM_START_LOOKAHEAD = 40


def scrub_rust_lines(source: str) -> List[str]:
    """Strip strings, char literals and comments, one output line per input.

    Cross-line lexer state is carried, so nested ``/* /* */ */`` comments,
    ``r#"…"#`` raw strings and backslash-continued literals are handled.
    Removed regions are DROPPED, not blanked (that is what the underlying
    scrubber does), so column offsets do not survive — line NUMBERING does,
    which is all the span arithmetic here needs.
    """
    scrubbed: List[str] = []
    state = None
    for line in source.splitlines():
        text, state = _scrub_line_stateful(line, _RUST_SYNTAX, state)
        scrubbed.append(text)
    return scrubbed


def strip_rust_comments(source: str) -> List[str]:
    """Strip ONLY comments (line + nested block), one output line per input.

    String and char literals are reproduced VERBATIM — the inverse tradeoff
    from :func:`scrub_rust_lines`. Some lints need the opposite question
    answered: a scan for CLI-invocation text baked into a Rust
    ``format!("…")`` call needs comment prose gone (a doc-comment mentioning
    ``vco codegraph analyze`` as an example must not misfire the gate) but
    the STRING CONTENTS intact, because that is exactly where the text being
    scanned for lives. Stripping strings there would not just miss the false
    positive — it would turn a genuine bogus emitted command into a silent
    false negative, the worse failure direction for a gate whose whole job is
    catching emitted commands that don't exist. Per this module's own
    "add a parameter here" rule this is the SECOND parameter added to the
    shared engine (``keep_strings`` on ``_scrub_line_stateful``) rather than a
    private variant.

    Cross-line lexer state is carried the same way :func:`scrub_rust_lines`
    does, so nested ``/* /* */ */`` comments spanning many lines are handled
    identically; only the treatment of strings differs.
    """
    stripped: List[str] = []
    state = None
    for line in source.splitlines():
        text, state = _scrub_line_stateful(
            line, _RUST_SYNTAX, state, keep_strings=True
        )
        stripped.append(text)
    return stripped


def _predicate_mentions_test_only(
    code: Sequence[str], index: int, *, include_any_test: bool,
) -> Optional[bool]:
    """Is the ``cfg`` predicate starting on ``code[index]`` test-only?

    Returns ``None`` when the predicate does not parse (unbalanced within the
    lookahead) — the caller then skips nothing.
    """
    text = ""
    depth = 0
    started = False
    for line in code[index:index + _PREDICATE_LOOKAHEAD]:
        for ch in line:
            if ch == "(":
                depth += 1
                started = True
            elif ch == ")":
                depth -= 1
            text += ch
            if started and depth == 0:
                break
        if started and depth == 0:
            break
        text += " "
    if not started or depth != 0:
        return None

    body = text[text.index("(") + 1:text.rindex(")")]
    if not _TEST_IDENT_RE.search(body):
        return False
    if include_any_test:
        return True
    return _predicate_is_test_only(body)


def _split_top_level(body: str) -> List[str]:
    """Split a ``cfg`` predicate body on its TOP-LEVEL commas."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _predicate_is_test_only(pred: str) -> bool:
    """STRICT reading: is ``pred`` absent from EVERY non-test build?

    Evaluated recursively over the ``cfg`` grammar rather than pattern-matched
    on a prefix:

    * ``test``                     → yes
    * ``all(a, b, …)`` (AND)       → yes iff ANY conjunct is test-only
    * ``any(a, b, …)`` (OR)        → yes iff EVERY alternative is test-only
    * ``not(…)``, any other ident  → no

    v0.2.92: the first implementation asked ``body.startswith("all(")``, which
    reads "mentions test AND is a conjunction" as test-only. That is wrong for
    a shape this repo actually has —
    ``#[cfg(all(unix, any(test, debug_assertions)))]``
    (``vct-launcher-core/src/secrets.rs``, ``secrets_ss_connection.rs``) is
    ``unix AND (test OR debug_assertions)``, so it COMPILES INTO A DEBUG
    BINARY and is production for exactly the question the strict reading
    exists to answer. Measured cost of the prefix rule: 1249 lines of
    ``secrets.rs`` and 409 of ``secrets_ss_connection.rs`` treated as test
    code. Found by diffing this module against the private copies it is
    absorbing — the older `#[cfg(test)]`-literal matchers happened to get
    those files right.
    """
    pred = pred.strip()
    if pred == "test":
        return True
    match = re.match(r"^(all|any|not)\s*\((.*)\)$", pred, re.S)
    if not match:
        return False
    kind, inner = match.group(1), match.group(2)
    args = _split_top_level(inner)
    if not args:
        return False
    if kind == "all":
        return any(_predicate_is_test_only(arg) for arg in args)
    if kind == "any":
        return all(_predicate_is_test_only(arg) for arg in args)
    return False  # not(...) — negation is never test-only for this question


def _item_end_line(code: Sequence[str], attr_index: int) -> Optional[int]:
    """0-indexed last line of the item annotated at ``code[attr_index]``.

    ``None`` when the item cannot be delimited confidently (no ``{``/``;``
    within the lookahead, or an unbalanced block) — fail CLOSED so the caller
    skips nothing rather than blinding the scan.
    """
    depth = 0
    seen_open = False
    limit = len(code)
    for i in range(attr_index, limit):
        line = code[i]
        # Further attributes on their own line belong to the same item.
        if i > attr_index and not seen_open and _ANY_ATTR_RE.match(line):
            continue
        for ch in line:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                if seen_open:
                    depth -= 1
                    if depth == 0:
                        return i
            elif ch == ";" and not seen_open:
                # `#[cfg(test)] mod tests;` / `use …;` — a one-line item.
                return i
        if not seen_open and i - attr_index > _ITEM_START_LOOKAHEAD:
            return None
    return None


def cfg_test_spans(
    source: str, *, include_any_test: bool = False,
) -> List[Tuple[int, int]]:
    """1-indexed ``(first_line, last_line)`` spans of test-gated Rust items.

    An inner ``#![cfg(test)]`` yields a single whole-file span. Everything
    that cannot be delimited confidently yields NO span (fail closed).
    """
    raw = source.splitlines()
    code = scrub_rust_lines(source)
    spans: List[Tuple[int, int]] = []

    # `#![cfg(test)]` is an INNER attribute: it gates the whole enclosing
    # module, i.e. this entire file.
    for i, line in enumerate(code):
        if _INNER_CFG_ATTR_RE.search(line) and _predicate_mentions_test_only(
            code, i, include_any_test=include_any_test,
        ):
            return [(1, len(raw))]

    i = 0
    while i < len(code):
        if not _CFG_ATTR_RE.search(code[i]) or _INNER_CFG_ATTR_RE.search(code[i]):
            i += 1
            continue
        verdict = _predicate_mentions_test_only(
            code, i, include_any_test=include_any_test,
        )
        if not verdict:
            i += 1
            continue
        end = _item_end_line(code, i)
        if end is None:
            i += 1  # undelimitable — skip nothing, keep scanning
            continue
        spans.append((i + 1, end + 1))
        i = end + 1
    return spans


def cfg_test_line_numbers(
    source: str, *, include_any_test: bool = False,
) -> Set[int]:
    """The 1-indexed line numbers covered by :func:`cfg_test_spans`."""
    covered: Set[int] = set()
    for first, last in cfg_test_spans(source, include_any_test=include_any_test):
        covered.update(range(first, last + 1))
    return covered


def cfg_test_gate_indices(
    source: str, *, include_any_test: bool = False,
) -> List[int]:
    """0-indexed lines carrying a test-gate attribute that STARTS a skip.

    The positional half of the API the two migrated lints were written
    against (they scanned line-by-line asking "does a skip start here?").
    Derived from :func:`cfg_test_spans`, so there is exactly one span
    algorithm, not two.
    """
    return [
        first - 1
        for first, _last in cfg_test_spans(
            source, include_any_test=include_any_test,
        )
    ]


def cfg_test_item_resume_index(
    source: str, gate_index: int, *, include_any_test: bool = False,
) -> Optional[int]:
    """0-indexed index just past the item gated at ``gate_index``.

    The other positional half: the private ``_skip_cfg_test_item`` /
    ``_skip_gated_item`` copies both returned "where the scan resumes", and
    the acceptance suites for the migration assert on THAT number (that it is
    < EOF, that it agrees with an independent column-0 oracle). Returns None
    when ``gate_index`` does not start a skipped item — including the
    fail-closed case where the item could not be delimited, for which the
    private copies returned EOF and thereby blinded the rest of the file.
    """
    for first, last in cfg_test_spans(source, include_any_test=include_any_test):
        if first - 1 == gate_index:
            return last
    return None
