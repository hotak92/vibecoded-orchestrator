# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared helpers for the per-language code-graph extractors (P2f stage 2).

Moved VERBATIM out of ``templates/scripts/analyze_code_graph.py`` (v0.2.76)
— behavior is pinned byte-identically by the golden snapshot suite
(``tests/test_codegraph_golden.py``); treat any output drift as a regression,
not a refactor opportunity.

Contents (all previously module-level in the analyzer, used ONLY by the
extractors / the per-language method helpers):

* ``_is_minified_content`` (+ the ``_MINIFIED_*`` thresholds) — CG-5 walk-time
  skip heuristic for machine-minified files.
* ``_extract_balanced_block`` + ``_scrub_for_brace_balance`` — V52-O.11.E
  brace-balanced body extraction (every brace-language extractor).
* ``_extract_external_calls`` (+ the ``_HTTP/GRPC/MQ/WS_LIBS`` gates and
  ``_strip_triple_quoted``) — cross-language interaction extraction.

Helpers the extractors share WITH non-extractor analyzer code — the
``embed_function`` / ``embed_class`` / ``generate_embedding`` /
``_shape_for_insert`` embedding-service seams — deliberately STAY in the
analyzer (module state + test monkeypatch seam live there); extractor modules
reach those via ``ctx.`` (see the analyzer's "module-global seams" block).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Tuple


# ── P2f stage 3 (v0.2.77 Part 6): the NARROW helpers protocol ───────────────
class ExtractorHelpers:
    """The narrow surface a pure ``extract_<lang>_file`` producer is allowed to
    reach on the analyzer — deliberately NOT the analyzer itself.

    A pure producer reads source and builds a ``FileExtraction``; it never
    mutates analyzer state (caches, visited_uuids, the module row — those are
    the writer's job). But two dependencies are genuinely needed at PRODUCE
    time:

      * the embedding seams (``embed_class`` / ``embed_function`` /
        ``generate_embedding``) that the deferred-embed closures fire lazily —
        routed through the analyzer instance so they keep late-resolving the
        module-global stub the golden suite / seam tests monkeypatch;
      * python-only AST helpers (module summary, complexity, imports, source
        slicing, name/type extraction) that live on the analyzer next to the
        ``ast`` machinery — exposed as thin passthroughs so the python producer
        can build entities without importing the analyzer.

    Holding ``_ctx`` privately (never handed to the extractor) keeps the
    "extractor cannot mutate analyzer state" invariant a code-review-checkable
    property: the extractor only sees the whitelisted methods below.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    # ---- embedding seams (late-resolving via the analyzer delegators) --------
    def embed_class(self, *args: Any, **kwargs: Any) -> Any:
        return self._ctx.embed_class(*args, **kwargs)

    def embed_function(self, *args: Any, **kwargs: Any) -> Any:
        return self._ctx.embed_function(*args, **kwargs)

    def generate_embedding(self, *args: Any, **kwargs: Any) -> Any:
        return self._ctx.generate_embedding(*args, **kwargs)

    def shape_for_insert(self, *args: Any, **kwargs: Any) -> Any:
        return self._ctx._shape_for_insert(*args, **kwargs)

    # ---- python-only AST helpers (thin passthroughs to analyzer methods) -----
    @property
    def project_name(self) -> Any:
        return self._ctx.project_name

    def extract_imports(self, tree: Any) -> Any:
        return self._ctx._extract_imports(tree)

    def generate_module_summary(self, tree: Any, source_lines: Any, path: str) -> Any:
        return self._ctx._generate_module_summary(tree, source_lines, path)

    def calculate_complexity(self, tree: Any) -> Any:
        return self._ctx._calculate_complexity(tree)

    def extract_source_code(self, node: Any, source_lines: Any) -> Any:
        return self._ctx._extract_source_code(node, source_lines)

    def get_name(self, node: Any) -> Any:
        return self._ctx._get_name(node)

    def extract_field_types(self, node: Any) -> Any:
        return self._ctx._extract_field_types(node)

    def extract_annotation_type_names(self, annotation: Any) -> Any:
        return self._ctx._extract_annotation_type_names(annotation)


def run_pure_extractor(
    ctx: Any,
    file_path: Path,
    repo_root: Path,
    extract: Callable[[str, Path, Path, "ExtractorHelpers"], Any],
    empty_stats: Dict[str, int],
) -> Dict[str, int]:
    """The shared thin-shim body for a pure ``extract_<lang>_file`` producer.

    Owns the walk-time I/O + the two analyzer-side skip gates that MUST run
    BEFORE extraction (preserving today's short-circuit economics — the pure
    producer is only invoked when the file is NOT skipped):

      1. CG-5 minified-content skip (skip + log, never deletes rows);
      2. the unchanged-file gate ``ctx._get_existing_module`` (path + hash +
         embed-revision aware).

    On a skip it returns ``empty_stats`` verbatim (byte-identical to the
    per-language ``return {'modules': 0, ...}`` / ``return stats`` the imperative
    extractors used). Otherwise: ``extract`` -> ``ctx.write_file_extraction``
    -> stats dict.

    ``empty_stats`` is the language's own zero-stats dict (they differ:
    js/csharp/proto also carry ``apis``) so the returned shape stays identical
    to the pre-Part-6 body on the skip paths.
    """
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if _is_minified_content(content):
        try:
            _rel_min = file_path.relative_to(repo_root).as_posix()
        except Exception:  # noqa: BLE001
            _rel_min = str(file_path)
        print(f"⏭️  Skipping {_rel_min} (looks minified/generated)")
        return dict(empty_stats)

    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()
    if ctx._get_existing_module(relative_path, file_hash):
        print(f"⏭️  Skipping {relative_path} (unchanged)")
        return dict(empty_stats)

    fx = extract(content, file_path, repo_root, ExtractorHelpers(ctx))
    return ctx.write_file_extraction(fx)


# CG-5 (v0.2.75 P3d): minified-CONTENT heuristic. The name-suffix denylist
# (``CODEGRAPH_SKIP_SUFFIXES``) only catches conventionally-named build output
# (``*.min.js`` …). Vendored / generated files that DON'T carry the suffix
# (a bundled ``vendor.js``, a generated ``schema.js``, a one-line CSS-in-JS
# blob) still get walked, and their single-giant-line bodies produce garbage
# entities that pollute retrieval. This content check skips a file whose lines
# are pathologically long — the signature of minification — regardless of name.
# Skip + log ONLY; NEVER deletes existing rows (a genuine long-line first-party
# file simply isn't re-indexed; the orphan-clear owns deletion).
_MINIFIED_MAX_LINE_LEN = 2000      # any single line this long → almost certainly minified
_MINIFIED_MEDIAN_LINE_LEN = 400    # typical hand-written code medians well under 100
_MINIFIED_MIN_CONTENT_LEN = 4000   # don't judge tiny files (a short dense config is fine)


def _is_minified_content(content: str) -> bool:
    """True when ``content`` looks machine-minified (skip it at walk time).

    Heuristic (conservative — errs toward KEEPING first-party code):
      * only judged for non-trivial files (>= ``_MINIFIED_MIN_CONTENT_LEN``);
      * flagged when the MAX line length is huge (a bundler's single-line output)
        OR the MEDIAN line length is far above what hand-written code produces.
    Empty / short / unreadable content → False (never skip on uncertainty).
    """
    if not content or len(content) < _MINIFIED_MIN_CONTENT_LEN:
        return False
    try:
        lines = content.split("\n")
        lengths = [len(ln) for ln in lines]
        if not lengths:
            return False
        max_len = max(lengths)
        if max_len >= _MINIFIED_MAX_LINE_LEN:
            return True
        srt = sorted(lengths)
        median = srt[len(srt) // 2]
        return median >= _MINIFIED_MEDIAN_LINE_LEN
    except Exception:  # noqa: BLE001 — a heuristic must never crash the walk
        return False


def _extract_balanced_block(
    source_lines: List[str],
    start_line: int,
    *,
    opener: str = "{",
    closer: str = "}",
    max_lookahead: int = 400,
    language: Optional[str] = None,
) -> int:
    """V52-O.11.E (v0.2.52, 2026-06-09): find the real end-line of a
    code block by counting balanced ``opener``/``closer`` pairs.

    Replaces the broken ``end_line = min(start_line + N, len(source_lines))``
    heuristic used at 17 sites in this file pre-V52-O.11.E. Audit a79152
    confirmed the heuristic systematically over-clusters sequential
    functions by writing each function's ``function_body`` extending up
    to N lines past its real close brace (e.g. ``is_blocklisted_agent_file``
    in project_state_populate.rs: real end line 281, stored end line 315,
    body contains 34 lines of the NEXT function).

    Algorithm:
      1. Scan ``source_lines[start_line-1:]`` looking for the first
         ``opener``. Once found, increment a brace-counter.
      2. Continue scanning; for every additional ``opener`` increment,
         for every ``closer`` decrement. When counter reaches 0, the
         current line is the close-brace line — return its 1-indexed
         line number.
      3. Skip openers/closers inside comments and string literals, via
         ``_scrub_line_stateful`` — ONE left-to-right pass per line with
         LEXER STATE CARRIED ACROSS LINES, so block comments, template /
         raw / verbatim strings, here-strings and Lua long brackets that
         span lines are handled rather than mis-read (v0.2.91). Which
         markers apply is decided by ``language`` (a ``lang_dispatch``
         key): ``--`` is a comment only in Lua, ``#`` only in the
         shell/ruby/python family, ``//`` only in the C family. Omitting
         ``language`` selects the generic C-family profile.
      4. If no balanced close is found within ``max_lookahead`` lines,
         return ``min(start_line + max_lookahead, len(source_lines))``
         (graceful degradation — gives the caller the existing-pattern
         behavior for runaway functions).

    Returns the **1-indexed line number of the closing brace**. Callers
    consume it via the existing pattern:

        end_line = _extract_balanced_block(source_lines, start_line)
        body = '\\n'.join(source_lines[max(0, start_line - 1):end_line])

    The 1-indexed return matches the existing ``end_line`` convention
    at every caller site — drop-in replacement, no off-by-one.

    Language coverage: works for any brace-balanced language (C, C++,
    Java, JavaScript, TypeScript, Go, Rust, C#) — but PASS ``language``.
    Comment markers are not universal (``--`` is a Lua comment and a C++
    pre-decrement; ``#`` is a shell comment and a C string character), and
    the line-spanning string forms are per-language (JS template literals,
    Rust raw strings, C# verbatim strings, Lua long brackets). Omitting
    ``language`` selects the C-family profile, which will mis-lex those.
    Every in-package call site threads its key; the registry↔table parity
    test keeps that true. ``end``-keyword
    languages (Lua) do NOT use this helper — their extractors
    (``vco_lib/codegraph_lang/lua.py``) key on the ``end`` token
    directly. Indent-significant languages don't use it either (Python
    uses AST so it bypasses this helper entirely; Ruby uses ``end``
    keywords — callers there may still use this helper since Ruby's
    bodies are short enough that brace-balance over a 400-line window
    won't over-extend, but it's a less precise fit).

    Performance: ~O(end_line - start_line) lines scanned per call. With
    ``max_lookahead=400`` and typical function bodies of 10-50 lines,
    this adds ~1ms per function vs the old fixed-window approach. The
    correctness gain (no body-bleed contamination in embeddings) is
    worth the cost.

    v0.2.91 re-measured after the per-language lexer replaced the regex
    scrub: 0.38 ms for a typical 30-line body, 5.0 ms for a 400-line
    runaway (~6.5x the regex version, still inside the ~1 ms/function
    budget above). At whole-repo scale that is ~0.4 s per 1000 entities —
    negligible against the embedding round-trips that dominate an analyze
    run, so the scrubber is deliberately kept simple (one obvious
    character loop) rather than fast: this is the path where a clever
    optimisation buys milliseconds and risks another truncated-body class
    of bug.
    """
    if start_line < 1 or start_line > len(source_lines):
        return min(start_line + 40, len(source_lines))  # legacy fallback

    counter = 0
    found_opener = False
    lookahead_end = min(start_line - 1 + max_lookahead, len(source_lines))
    syn = _syntax_for(language)
    scrub_state: Optional[_ScrubState] = None

    for line_idx in range(start_line - 1, lookahead_end):
        line = source_lines[line_idx]
        # Strip comments + string literals with the per-language lexer, carrying
        # its state across lines so multi-line strings / block comments don't
        # feed their contents to the brace counter. A construct that opened
        # BEFORE ``start_line`` is unknowable from here — the scan starts at the
        # block's own first line by construction.
        scrubbed, scrub_state = _scrub_line_stateful(line, syn, scrub_state)
        for ch in scrubbed:
            if ch == opener:
                counter += 1
                found_opener = True
            elif ch == closer:
                counter -= 1
                if found_opener and counter == 0:
                    # +1 because line_idx is 0-indexed; end_line is 1-indexed
                    return line_idx + 1

    # No balanced close within lookahead — fall back to the legacy
    # behavior so callers don't crash. This is the runaway-function
    # branch; in practice almost never hit.
    return min(start_line + 40, len(source_lines))


# ---------------------------------------------------------------------------
# Brace-balance scrubbing — the per-language comment/string lexer
# ---------------------------------------------------------------------------
#
# v0.2.91 (plan decision #29): the pre-fix scrubber stripped from the EARLIEST
# of ``#`` / ``//`` / ``--`` — every marker applied to EVERY language — and did
# so BEFORE removing string literals. Both halves were wrong, and the failures
# are C-family commonplaces rather than the "exotic multi-line constructs" the
# old docstring blamed:
#
#   ``for (int i = n; i > 0; --i) {``    → truncated at the pre-decrement
#   ``if (u == "http://x") { return; }`` → truncated inside the URL string
#   ``log("#tag"); if (c) {``            → truncated inside the string
#   ``x=${VAR#pre}; ... ; then``         → truncated inside the shell expansion
#
# Each drops a real ``{``/``}`` from the counter, so ``_extract_balanced_block``
# returns a SHORT end-line and the stored ``function_body`` is a truncated
# fragment — degraded embeddings for every non-Python extractor (Python builds
# bodies from the AST and never reaches here).
#
# THE FIX HAS THREE PARTS:
#
# 1. ONE left-to-right pass, not two sequential regex passes. "Strings first"
#    and "comments first" are BOTH wrong as a global ordering: comments-first
#    truncates on a marker inside a string (the bug above), strings-first makes
#    the apostrophe in ``// don't do this`` open a string. A single scan that
#    tracks which construct it is inside makes the ordering question moot —
#    neither construct can begin inside the other.
# 2. PER-LANGUAGE markers from ONE table (``_LANG_SYNTAX``), threaded to the
#    scrubber as ``language=`` by every extractor call site. ``--`` is a comment
#    only in Lua, ``#`` only in the shell/ruby/python family, ``//`` only in the
#    C family.
# 3. CROSS-LINE state for the constructs that genuinely span lines (block
#    comments, template/raw/verbatim strings, here-strings, Lua long brackets).
#    A stateless stripper mis-reading a multi-line string is the failure that
#    silently un-scanned ~400 lines elsewhere in this cycle — see the KG node
#    ``source-text-gates-fail-toward-green-2026-08-27``.
#
# BLAST-RADIUS RULE (conservative default): only constructs explicitly marked
# as line-spanning carry state across a newline. A single-line string left open
# at end-of-line RESETS to normal, so a lexer mistake can never poison more than
# the line that caused it — the same bound the pre-fix scrubber had.


class _MultilineForm(NamedTuple):
    """A construct that may span lines and whose closer depends on its opener.

    ``closer`` builds the literal closing token from the opener match, which is
    what Rust's ``r##"`` → ``"##`` and Lua's ``[=[`` → ``]=]`` need.
    """

    opener: "re.Pattern[str]"
    closer: Callable[["re.Match[str]"], str]
    escapes: bool = False        # backslash escapes the next char inside
    doubled_close: bool = False  # a doubled closer is an escaped literal (C# @"")
    ident_guard: bool = False    # opener must not continue an identifier (Rust r")


class _LangSyntax(NamedTuple):
    """Comment/string syntax for ONE language key."""

    line_comments: Tuple[str, ...] = ()
    #: markers that only open a comment at a word boundary — ``${VAR#pre}`` and
    #: ``${#VAR}`` are shell parameter expansions, NOT comments. See
    #: :data:`_WORD_START_BEFORE` for which characters count as a boundary.
    word_start_line_comments: Tuple[str, ...] = ()
    #: (open, close, nested) — all span lines.
    block_comments: Tuple[Tuple[str, str, bool], ...] = ()
    multiline: Tuple[_MultilineForm, ...] = ()
    #: ``'`` delimits a bounded char literal (and may also be a Rust lifetime or
    #: a C++ digit separator, which must NOT be read as an unterminated string).
    char_quote: bool = False
    #: ``'`` delimits an ordinary single-line string.
    single_quote_string: bool = False
    #: a trailing backslash continues a ``"…`` string onto the next line.
    string_line_continuation: bool = False


class _ScrubState(NamedTuple):
    """What the lexer is currently inside. ``None`` means normal code."""

    closer: str
    escapes: bool = False
    doubled_close: bool = False
    spans_lines: bool = False
    opener: str = ""      # non-empty only for a NESTABLE block comment (Rust)
    depth: int = 1
    continuation: bool = False  # a trailing backslash may extend this string


# Characters a shell/PowerShell ``#`` must follow to begin a comment.
#
# Braces are deliberately ABSENT (v0.2.91 wave-5 review MAJOR-3, a #29
# residual). ``{`` and ``}`` are shell RESERVED WORDS, not metacharacters: a
# brace-group opener is always followed by whitespace, so ``{ # comment`` still
# opens a comment via the space rule, while bash reads an adjacent ``{#…`` as
# part of a word. Treating ``{`` as a word boundary made ``${#arr[@]}`` /
# ``${#VAR}`` — parameter LENGTH expansion, and PowerShell's braced-variable
# form ``${…#…}`` — scrub to ``n=${``, which both truncates the line AND leaves
# the counter an unmatched ``{``, so ``_extract_balanced_block`` overruns past
# the real body end. Five shipped hooks use the shape
# (``post-tool-security.sh``, ``pre-bash-context-inject.sh``,
# ``subagent-stop-reconcile.sh``, ``verify-container-ports.sh``), so this
# mis-extracted in every install's own code graph.
_WORD_START_BEFORE: FrozenSet[str] = frozenset(" \t;&|()`")

# A bounded char literal: 'a', '\n', '\x41', '\u{1F600}'. Deliberately does NOT
# match a Rust lifetime ('a followed by anything but a quote) or a C++ digit
# separator (1'000'000) — those stay ordinary characters.
_CHAR_LITERAL_RE = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{1,8}|.)|[^'\\])'")

# ── reusable multi-line forms ──────────────────────────────────────────────
_ML_TEMPLATE_LITERAL = _MultilineForm(re.compile(r"`"), lambda m: "`", escapes=True)
_ML_GO_RAW_STRING = _MultilineForm(re.compile(r"`"), lambda m: "`")
_ML_RUST_RAW_STRING = _MultilineForm(
    re.compile(r'(?:br|rb|r)(#*)"'), lambda m: '"' + m.group(1), ident_guard=True
)
_ML_CPP_RAW_STRING = _MultilineForm(
    re.compile(r'R"([^()\\ \t]{0,16})\('), lambda m: ")" + m.group(1) + '"', ident_guard=True
)
_ML_CSHARP_VERBATIM = _MultilineForm(
    re.compile(r'@"'), lambda m: '"', doubled_close=True
)
_ML_TRIPLE_DOUBLE = _MultilineForm(re.compile(r'"""'), lambda m: '"""')
_ML_LUA_LONG_COMMENT = _MultilineForm(
    re.compile(r"--\[(=*)\["), lambda m: "]" + m.group(1) + "]"
)
_ML_LUA_LONG_STRING = _MultilineForm(
    re.compile(r"\[(=*)\["), lambda m: "]" + m.group(1) + "]"
)
_ML_PS_HERESTRING_D = _MultilineForm(re.compile(r'@"'), lambda m: '"@')
_ML_PS_HERESTRING_S = _MultilineForm(re.compile(r"@'"), lambda m: "'@")
# ``^`` anchors to the true start of the string, and a scrubbed line never
# contains a newline — so this only ever matches at column 0, which is exactly
# Ruby's rule for =begin/=end.
_ML_RUBY_BLOCK_COMMENT = _MultilineForm(re.compile(r"^=begin\b"), lambda m: "=end")

_C_BLOCK_COMMENT: Tuple[Tuple[str, str, bool], ...] = (("/*", "*/", False),)

_C_FAMILY = _LangSyntax(
    line_comments=("//",),
    block_comments=_C_BLOCK_COMMENT,
    char_quote=True,
    string_line_continuation=True,
)
_JS_FAMILY = _LangSyntax(
    line_comments=("//",),
    block_comments=_C_BLOCK_COMMENT,
    multiline=(_ML_TEMPLATE_LITERAL,),
    single_quote_string=True,
    string_line_continuation=True,
)

#: language key -> syntax. Keys are the analyzer's ``lang_dispatch`` keys — the
#: SAME keys ``codegraph_lang.EXTRACTORS`` is keyed by. Registry↔table parity is
#: pinned by ``tests/test_v0291_scrub_language_markers.py`` (which enumerates
#: ``EXTRACTORS`` as the denominator), so a new language cannot land without
#: declaring its markers here.
_LANG_SYNTAX: Dict[str, _LangSyntax] = {
    # ── C family ───────────────────────────────────────────────────────────
    "cpp": _C_FAMILY._replace(multiline=(_ML_CPP_RAW_STRING,)),
    "csharp": _C_FAMILY._replace(multiline=(_ML_TRIPLE_DOUBLE, _ML_CSHARP_VERBATIM)),
    "java": _C_FAMILY._replace(multiline=(_ML_TRIPLE_DOUBLE,)),
    "go": _C_FAMILY._replace(
        multiline=(_ML_GO_RAW_STRING,), string_line_continuation=False
    ),
    "rust": _C_FAMILY._replace(
        block_comments=(("/*", "*/", True),),  # Rust block comments NEST
        multiline=(_ML_RUST_RAW_STRING,),
    ),
    "proto": _LangSyntax(
        line_comments=("//",), block_comments=_C_BLOCK_COMMENT, single_quote_string=True
    ),
    # ── JS family (svelte's extracted bodies are <script> JavaScript) ──────
    "javascript": _JS_FAMILY,
    "typescript": _JS_FAMILY,
    "svelte": _JS_FAMILY._replace(
        block_comments=_C_BLOCK_COMMENT + (("<!--", "-->", False),)
    ),
    # ── hash-comment family ────────────────────────────────────────────────
    # Python bypasses this helper entirely (AST bodies); the entry exists so the
    # registry-parity test has an explicit row for every dispatch key.
    "python": _LangSyntax(
        line_comments=("#",),
        multiline=(_ML_TRIPLE_DOUBLE, _MultilineForm(re.compile(r"'''"), lambda m: "'''")),
        single_quote_string=True,
    ),
    "ruby": _LangSyntax(
        line_comments=("#",),
        multiline=(_ML_RUBY_BLOCK_COMMENT,),
        single_quote_string=True,
    ),
    # POSIX: ``#`` opens a comment only at the start of a word, so ``${VAR#pre}``
    # and ``${VAR%suf}`` keep their closing brace.
    "shell": _LangSyntax(word_start_line_comments=("#",), single_quote_string=True),
    "powershell": _LangSyntax(
        word_start_line_comments=("#",),
        block_comments=(("<#", "#>", False),),
        multiline=(_ML_PS_HERESTRING_D, _ML_PS_HERESTRING_S),
        single_quote_string=True,
    ),
    # ── other ──────────────────────────────────────────────────────────────
    "lua": _LangSyntax(
        line_comments=("--",),
        multiline=(_ML_LUA_LONG_COMMENT, _ML_LUA_LONG_STRING),
        single_quote_string=True,
    ),
}

#: Used when a caller passes no language. Matches this module's documented
#: coverage claim ("any brace-balanced language: C, C++, Java, JavaScript, Go,
#: Rust, C#") — the C-family profile. Callers inside this package always pass an
#: explicit key; the fallback exists for ad-hoc/legacy callers.
_GENERIC_BRACE_SYNTAX = _C_FAMILY


def _syntax_for(language: Optional[str]) -> _LangSyntax:
    """Resolve a ``lang_dispatch`` key to its syntax, falling back to the
    generic brace-language profile for an unknown/absent key."""
    if not language:
        return _GENERIC_BRACE_SYNTAX
    return _LANG_SYNTAX.get(language.strip().lower(), _GENERIC_BRACE_SYNTAX)


def _ends_with_odd_backslash(line: str) -> bool:
    """True when ``line`` ends with an unescaped backslash (a line continuation)."""
    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1


def _scan_construct(line: str, i: int, state: _ScrubState) -> Tuple[int, Optional[_ScrubState]]:
    """Scan forward from ``i`` while inside ``state``.

    Returns ``(index just past the closer, None)`` when the construct closes on
    this line, or ``(len(line), state)`` when it runs past the end of the line.
    """
    n = len(line)
    while i < n:
        if state.escapes and line[i] == "\\":
            i += 2
            continue
        if state.opener and line.startswith(state.opener, i):
            state = state._replace(depth=state.depth + 1)
            i += len(state.opener)
            continue
        if line.startswith(state.closer, i):
            j = i + len(state.closer)
            if state.doubled_close and line.startswith(state.closer, j):
                i = j + len(state.closer)  # an escaped literal delimiter ("" in @"")
                continue
            if state.depth > 1:
                state = state._replace(depth=state.depth - 1)
                i = j
                continue
            return j, None
        i += 1
    return n, state


def _carry_state(state: _ScrubState, line: str) -> Optional[_ScrubState]:
    """Decide whether an unterminated construct survives the newline.

    Only explicitly line-spanning constructs (and a backslash-continued string in
    a language that allows it) carry over; everything else resets, bounding a
    mis-lex to the single line that caused it.
    """
    if state.spans_lines:
        return state
    if state.continuation and _ends_with_odd_backslash(line):
        return state
    return None


def _open_construct_at(
    line: str, i: int, syn: _LangSyntax
) -> Optional[Tuple[int, _ScrubState]]:
    """If a line-spanning construct opens at ``line[i]``, return
    ``(index past the opener, state)``. Checked BEFORE line comments so Lua's
    ``--[[`` beats ``--`` and PowerShell's ``<#`` beats ``#``."""
    for form in syn.multiline:
        if form.ident_guard and i > 0 and (line[i - 1].isalnum() or line[i - 1] == "_"):
            continue
        m = form.opener.match(line, i)
        if m is not None:
            return m.end(), _ScrubState(
                closer=form.closer(m),
                escapes=form.escapes,
                doubled_close=form.doubled_close,
                spans_lines=True,
            )
    for opener, closer, nested in syn.block_comments:
        if line.startswith(opener, i):
            return i + len(opener), _ScrubState(
                closer=closer, spans_lines=True, opener=opener if nested else ""
            )
    return None


def _line_comment_at(line: str, i: int, syn: _LangSyntax) -> bool:
    for marker in syn.line_comments:
        if line.startswith(marker, i):
            return True
    for marker in syn.word_start_line_comments:
        if line.startswith(marker, i) and (i == 0 or line[i - 1] in _WORD_START_BEFORE):
            return True
    return False


def _scrub_line_stateful(
    line: str, syn: _LangSyntax, state: Optional[_ScrubState] = None
) -> Tuple[str, Optional[_ScrubState]]:
    """Remove comments + string literals from ONE line, carrying lexer state.

    Returns ``(code-only text, state for the next line)``. The removed regions
    are dropped entirely (delimiters included) — the only consumers are the
    ``{``/``}`` counters in ``_extract_balanced_block``, and no delimiter this
    lexer recognises is a brace.
    """
    out: List[str] = []
    i = 0
    n = len(line)

    if state is not None:
        i, state = _scan_construct(line, 0, state)
        if state is not None:
            return "", _carry_state(state, line)

    while i < n:
        opened = _open_construct_at(line, i, syn)
        if opened is not None:
            i, state = _scan_construct(line, opened[0], opened[1])
            if state is not None:
                return "".join(out), _carry_state(state, line)
            continue

        if _line_comment_at(line, i, syn):
            return "".join(out), None  # the rest of the line is a comment

        ch = line[i]
        if ch == '"':
            i, state = _scan_construct(
                line,
                i + 1,
                _ScrubState(
                    closer='"', escapes=True, continuation=syn.string_line_continuation
                ),
            )
            if state is not None:
                return "".join(out), _carry_state(state, line)
            continue

        if ch == "'":
            if syn.char_quote:
                m = _CHAR_LITERAL_RE.match(line, i)
                if m is not None:
                    i = m.end()
                    continue
                # A Rust lifetime ('a) or a C++ digit separator — ordinary text.
                out.append(ch)
                i += 1
                continue
            if syn.single_quote_string:
                i, state = _scan_construct(
                    line, i + 1, _ScrubState(closer="'", escapes=True)
                )
                if state is not None:
                    return "".join(out), _carry_state(state, line)
                continue

        out.append(ch)
        i += 1

    return "".join(out), None


def _scrub_for_brace_balance(line: str, language: Optional[str] = None) -> str:
    """Remove comments + string literals from ``line`` so the brace-counter in
    ``_extract_balanced_block`` doesn't mis-count braces inside them.

    The single-line entry point: a thin wrapper over ``_scrub_line_stateful``
    with fresh state (ONE lexer implementation, two entry points). Multi-line
    constructs therefore can't be recognised through THIS entry point — use the
    stateful form, as ``_extract_balanced_block`` does, when scanning a span.

    ``language`` is a ``lang_dispatch`` key (``"rust"``, ``"shell"``, …); it
    selects the comment/string markers from ``_LANG_SYNTAX``. Omitting it falls
    back to the generic brace-language (C-family) profile.

    THE REAL RISK this handles — and what the pre-v0.2.91 version got wrong — is
    a comment marker appearing inside a STRING or as an operator in another
    language: ``--i`` (C++ pre-decrement), ``"http://…"`` (a URL), ``"#tag"``,
    ``${VAR#pre}``. Each used to truncate the line and drop a real brace. It is
    NOT "exotic multi-line constructs", which the old docstring blamed and which
    lose no braces at all when they contain none.
    """
    scrubbed, _ = _scrub_line_stateful(line, _syntax_for(language))
    return scrubbed


# ---------------------------------------------------------------------------
# Cross-language call extraction
# ---------------------------------------------------------------------------

# HTTP client library → canonical name (used as import gate)
_HTTP_LIBS: Dict[str, str] = {
    # Python
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp",
    "urllib.request": "urllib", "urllib3": "urllib3",
    # JS/TS
    "axios": "axios", "node-fetch": "node-fetch", "got": "got",
    "cross-fetch": "cross-fetch",
    # Ruby
    "net/http": "net/http", "faraday": "faraday", "httparty": "httparty",
    "rest-client": "rest-client",
}
_GRPC_LIBS = {"grpc", "grpc-js", "@grpc/grpc-js", "grpc.io", "google.golang.org/grpc"}
_MQ_LIBS: Dict[str, str] = {
    "kafka-python": "kafka", "confluent-kafka": "kafka", "kafka": "kafka",
    "kafkajs": "kafka", "pika": "rabbitmq", "amqplib": "rabbitmq",
    "aio-pika": "rabbitmq", "redis": "redis",
}
_WS_LIBS = {"websocket", "websocket-client", "websockets", "socket.io-client", "ws"}


def _strip_triple_quoted(content: str) -> str:
    """Remove Python/JS triple-quoted strings to avoid extracting URLs from docstrings."""
    content = re.sub(r'""".*?"""', '""', content, flags=re.DOTALL)
    content = re.sub(r"'''.*?'''", "''", content, flags=re.DOTALL)
    return content


def _extract_external_calls(
    content_clean: str,
    imports: List[str],
    language: str,
    source_file: str = "",
) -> List[Dict[str, str]]:
    """
    Extract cross-language / cross-service communication calls from source code.

    False-positive prevention strategy:
    1. Import gate: only trigger when the relevant client library is imported.
    2. Literal gate: only extract calls where a literal string (not a plain variable)
       is used as the target. Partial templates (f"{VAR}/literal") yield medium confidence.
    3. Scope gate: strip triple-quoted strings so URLs in docstrings are ignored.

    Returns list of dicts with keys:
        interaction_type, direction, protocol, endpoint, raw_target, confidence
    """
    results: List[Dict[str, str]] = []

    # Normalise imports to a flat set of lowercase strings
    import_set = {i.lower().strip() for i in imports}

    def _has_any(lib_keys) -> bool:
        return any(k in import_set for k in lib_keys)

    # Work on comment-stripped, triple-quote-stripped content
    c = _strip_triple_quoted(content_clean)

    # -----------------------------------------------------------------------
    # HTTP calls
    # -----------------------------------------------------------------------
    http_lib = None
    for k, v in _HTTP_LIBS.items():
        if k in import_set:
            http_lib = v
            break

    # Shell: gate on literal `curl` or `wget` command
    if language == "shell":
        http_lib = "curl/wget"  # always check shell files for curl/wget

    if http_lib or language in ("csharp",):
        # Literal URL patterns — only http(s):// or ws(s):// URLs
        # Match: method("URL"  or  method('URL'  or  method(`URL`  (no ${} inside)
        literal_url = re.compile(
            r'(?:'
            # requests/httpx/aiohttp style: lib.method(["']url["']
            r'(?:requests|httpx|aiohttp|http|client|session|RestTemplate|HttpClient|'
            r'fetch|axios|got|Faraday|HTTParty|Net::HTTP|curl)\s*[.(]\s*'
            r'(?:["\']([A-Za-z][^"\'<>\s]{4,})["\']'        # literal string arg
            r'|`((?!.*\$\{)[A-Za-z][^`<>\s]{4,})`)'         # template literal, no ${
            r'|'
            # Shell: curl/wget "url" or curl url (without quotes, not $VAR)
            r'(?:curl|wget)(?:\s+-[^\s]+)*\s+'
            r'(?:["\']?(https?://[^\s"\'$<>]{5,})["\']?)'
            r')',
            re.MULTILINE,
        )
        for m in literal_url.finditer(c):
            raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not raw or raw.startswith("$"):
                continue
            # Infer HTTP method from context
            ctx = c[max(0, m.start() - 60):m.start() + len(raw) + 10].lower()
            method = "GET"
            for verb in ("post", "put", "patch", "delete"):
                if verb in ctx:
                    method = verb.upper()
                    break
            # Extract just the path if it's a full URL
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.path or raw
                if parsed.scheme in ("ws", "wss"):
                    results.append({
                        "interaction_type": "websocket", "direction": "outbound",
                        "protocol": parsed.scheme.upper(), "endpoint": endpoint,
                        "raw_target": raw, "confidence": "high",
                    })
                    continue
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "http", "direction": "outbound",
                "protocol": method, "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

        # Partial template: f"{VAR}/literal/path" or `${VAR}/literal/path`
        partial_template = re.compile(
            r'(?:f["\']|`)'                     # f-string or template literal
            r'(?:\{[^}]+\}|\$\{[^}]+\})'        # variable substitution at start
            r'(/[A-Za-z0-9/_-]{3,})'            # literal path segment follows
        )
        for m in partial_template.finditer(c):
            path = m.group(1)
            if http_lib and len(path) >= 4:
                # Only emit if there's a call context nearby
                ctx = c[max(0, m.start() - 100):m.start() + 10].lower()
                if any(k in ctx for k in ("get(", "post(", "put(", "delete(", "patch(", "fetch(", "request(")):
                    results.append({
                        "interaction_type": "http", "direction": "outbound",
                        "protocol": "HTTP", "endpoint": path,
                        "raw_target": m.group(0), "confidence": "medium",
                    })

    # -----------------------------------------------------------------------
    # gRPC calls
    # -----------------------------------------------------------------------
    if _has_any(_GRPC_LIBS):
        # Python/JS: SomeStub(channel).MethodName(request) or stub.MethodName(request)
        # Go: conn, _ := grpc.Dial("host:port", ...)
        grpc_dial = re.compile(r'grpc\.(?:Dial|dial|insecure_channel|secure_channel)\s*\(\s*["\']([^"\']+)["\']')
        for m in grpc_dial.finditer(c):
            raw = m.group(1)
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{raw}",
                "raw_target": raw, "confidence": "high",
            })

        # Stub method call: SomeServiceStub.MethodName( or stub.MethodName(
        stub_call = re.compile(r'\b(\w*(?:Stub|Client|ServiceClient))\s*\.\s*(\w+)\s*\(')
        for m in stub_call.finditer(c):
            stub, method = m.group(1), m.group(2)
            if method.lower() in ("__init__", "new", "create", "connect", "close", "init"):
                continue
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{stub}.{method}",
                "raw_target": f"{stub}.{method}()", "confidence": "medium",
            })

    # -----------------------------------------------------------------------
    # Message queue calls
    # -----------------------------------------------------------------------
    mq_lib = None
    for k, v in _MQ_LIBS.items():
        if k in import_set:
            mq_lib = v
            break

    if mq_lib == "kafka":
        # Python kafka: producer.send("topic-name", ...)
        # JS kafkajs: producer.send({ topic: "literal", ... })
        kafka_send = re.compile(
            r'(?:'
            r'(?:producer|kafka)\s*\.\s*send\s*\(\s*["\']([^"\']+)["\']'  # Python style
            r'|topic:\s*["\']([^"\']+)["\']'                               # JS object style
            r')'
        )
        for m in kafka_send.finditer(c):
            topic = (m.group(1) or m.group(2) or "").strip()
            if topic:
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "kafka", "endpoint": f"topic:{topic}",
                    "raw_target": topic, "confidence": "high",
                })

    if mq_lib == "rabbitmq":
        # Python pika: channel.basic_publish(exchange='x', routing_key='queue')
        rmq_pub = re.compile(
            r'basic_publish\s*\([^)]*routing_key\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_pub.finditer(c):
            key = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "rabbitmq", "endpoint": f"queue:{key}",
                "raw_target": key, "confidence": "high",
            })
        # exchange
        rmq_exch = re.compile(
            r'basic_publish\s*\([^)]*exchange\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_exch.finditer(c):
            exch = m.group(1)
            if exch:  # skip empty exchange (default direct exchange)
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "rabbitmq", "endpoint": f"exchange:{exch}",
                    "raw_target": exch, "confidence": "high",
                })

    if mq_lib == "redis":
        # Redis pub/sub: r.publish("channel", message)
        redis_pub = re.compile(r'\.publish\s*\(\s*["\']([^"\']+)["\']')
        for m in redis_pub.finditer(c):
            ch = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "redis", "endpoint": f"channel:{ch}",
                "raw_target": ch, "confidence": "high",
            })

    # -----------------------------------------------------------------------
    # WebSocket calls (when WS library imported but not caught by HTTP block)
    # -----------------------------------------------------------------------
    if _has_any(_WS_LIBS):
        ws_connect = re.compile(
            r'(?:WebSocketApp|create_connection|WebSocket|io)\s*\(\s*["\']'
            r'(wss?://[^"\'<>\s]{5,})["\']'
        )
        for m in ws_connect.finditer(c):
            raw = m.group(1)
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.netloc + parsed.path
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "websocket", "direction": "outbound",
                "protocol": "WS", "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

    # Deduplicate by (interaction_type, protocol, endpoint)
    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for r in results:
        key = (r["interaction_type"], r["protocol"], r["endpoint"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped
