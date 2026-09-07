# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared helpers for the per-language code-graph extractors (P2f stage 2).

Moved VERBATIM out of ``templates/scripts/analyze_code_graph.py`` (v0.2.76).
The MOVE was byte-identical; the module has since GROWN corrections and new
shared helpers (v0.2.92 and WP-5b), so it is no longer output-identical to
the analyzer's original. The golden snapshot suite
(``tests/test_codegraph_golden.py``) pins what it does TODAY, and unexplained
drift is still a regression — but a snapshot records behaviour, not
correctness (see ``tests/fixtures/codegraph_golden/README.md``).

Contents (all previously module-level in the analyzer, used ONLY by the
extractors / the per-language method helpers):

* ``_is_minified_content`` (+ the ``_MINIFIED_*`` thresholds) — CG-5 walk-time
  skip heuristic for machine-minified files.
* ``_extract_balanced_block`` + ``_scrub_for_brace_balance`` — V52-O.11.E
  brace-balanced body extraction (every brace-language extractor).
* ``extract_end_keyword_block`` (v0.2.92 WP-5b) — its sibling for the two
  languages that close a block with the WORD ``end`` (Ruby, Lua), where a
  brace count finds no opener at all and runs the body to end-of-file. Shares
  the lexer and the 1-indexed closing-line return convention with the brace
  scanner, and nothing else: an ``end`` count additionally has to decide, per
  occurrence, whether the keyword opens anything (Ruby's statement modifiers
  spell ``if`` exactly like its block form).
* ``_extract_external_calls`` (+ the ``_HTTP/GRPC/MQ/WS_LIBS`` gates and
  ``_strip_triple_quoted``) — cross-language interaction extraction.
* ``build_api_entity`` (v0.2.92) — the ONE constructor for a ``KIND_API``
  ``CodeEntity``, extracted from the three byte-identical copies that had
  accumulated in ``javascript`` / ``csharp`` / ``proto``.

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
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from vco_lib.codegraph_entities import CodeEntity, KIND_API


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


# ── v0.2.92: the ONE CodeAPI entity constructor ─────────────────────────────
def build_api_entity(
    *,
    file_path_rel: str,
    endpoint: str,
    method: str,
    description: str,
    project: Any,
    embed: Callable[[str], Any],
    parameters: Optional[List[str]] = None,
    returns: str = "",
    proxy_target: str = "",
    handler_full_name: Optional[str] = None,
) -> CodeEntity:
    """Build the ``KIND_API`` :class:`CodeEntity` for one route/endpoint.

    Single-homes what were three byte-identical hand-rolled copies (javascript
    Fastify routes, csharp ASP.NET attributes, proto RPC entries) before the
    python producer would have made a fourth. The copies each had to re-state
    the same four easy-to-get-wrong details:

      * the EXACT extras key set the CodeAPI schema + content-hash contract
        expects (``endpoint`` / ``method`` / ``api_description`` /
        ``parameters`` / ``returns`` / ``project`` / ``proxy_target``) — the
        first five feed ``CONTENT_HASH_FIELDS['CodeAPI']``, so a missing or
        renamed key silently re-hashes every stored row;
      * ``project`` travels in ``extras``, NOT in the ``CodeEntity.project``
        named field (CodeAPI's property set is wholly extras-driven);
      * ``handler`` is a REFERENCE a pure producer cannot mint (it needs the
        target function's UUID). It is requested via the private
        ``extras['_handler_full_name']`` control key, which
        ``write_file_extraction`` resolves against the entities it has ALREADY
        written for this file — so the handler's ``CodeEntity`` must be emitted
        BEFORE this one, and an unresolvable name drops the edge rather than
        fabricating it;
      * the embed must be DEFERRED (v0.2.82 G1 task 2) as a ZERO-arg closure
        with the description captured by DEFAULT ARGUMENT — a late-binding
        closure over a loop variable would embed the wrong text.

    ``endpoint`` + ``method`` are also the dedup identity
    (``CodeEntity.identity_key()`` → ``"<endpoint>:<method>"``, seeded into the
    deterministic UUID together with the project + ``file_path_rel``), so two
    routes that differ only by method are two rows, and the same route declared
    in two files does not collide.

    Behaviour is pinned byte-identically for all three pre-existing call-sites
    by ``tests/test_codegraph_golden.py`` (the fixture repo exercises a C#
    ``[HttpGet]``/``[HttpPost]``, two proto RPCs and two Fastify routes).
    """
    extras: Dict[str, Any] = {
        "endpoint": endpoint,
        "method": method,
        "api_description": description,
        "parameters": list(parameters) if parameters else [],
        "returns": returns,
        "project": project,
        "proxy_target": proxy_target,
    }
    if handler_full_name:
        extras["_handler_full_name"] = handler_full_name
    return CodeEntity(
        kind=KIND_API,
        file_path_rel=file_path_rel,
        extras=extras,
        deferred_embed=(lambda d=description: embed(d)),
    )


# ── v0.2.92: the ONE mount-prefix + route-path join ────────────────────────
def join_route(prefix: str, path: str) -> str:
    """Join a mount prefix (router / blueprint / controller ``[Route]``) with a
    route path, producing the leading-slash endpoint every producer stores.

    Extracted from the two byte-equivalent copies that had accumulated —
    ``python._py_join_route`` and ``csharp._csharp_join_route``. They became
    equivalent in v0.2.92 when the C# side stopped emitting a slash-less
    endpoint for the no-controller-route case; before that the C# copy could
    not be shared. Their ONLY remaining divergence was that C# also stripped
    whitespace off the method template, which is a C#-input concern (the route
    regex captures verbatim between the quotes of ``[HttpGet(" all ")]``), not
    part of the join — so the C# producer normalises its own argument at the
    call site and this function stays a pure refactor of both.

    Normalizes the SEAM only. A trailing slash on ``path`` is semantically
    meaningful in Flask (``/users/`` and ``/users`` are different rules) and is
    preserved verbatim. An EMPTY path yields the bare prefix, matching
    FastAPI's ``self.prefix + path`` concatenation: ``APIRouter(prefix="/v1")``
    with ``@router.get("")`` serves ``/v1``, NOT ``/v1/``; a prefix that is
    itself empty yields ``"/"`` rather than ``""``.

    Output is pinned byte-identically by ``tests/test_codegraph_golden.py`` and
    by the old-vs-new parity matrix in
    ``tests/test_v0292_shared_join_route.py``; treat drift as a regression.
    """
    p = (prefix or "").strip().rstrip("/")
    if p and not p.startswith("/"):
        p = "/" + p
    if not path:
        return p or "/"
    if not path.startswith("/"):
        path = "/" + path
    return p + path


# ── v0.2.92: the ONE "where does the declaration actually start" walk ──────
def skip_leading_whitespace(text: str, match_start: int, match_end: int) -> int:
    """First non-whitespace offset in ``text[match_start:match_end]``.

    Every regex-based producer in this package opens its class/method pattern
    with a modifier alternation that INCLUDES ``\\s`` — ``(?:public|private|
    …|\\s)+`` (java, csharp), ``(?:^|\\n)\\s*`` (cpp). That group starts
    matching at the whitespace which follows the PREVIOUS token, so
    ``match.start()`` routinely sits on the previous LINE and
    ``text[:match.start()].count('\\n') + 1`` is one or more lines too high.
    Measured on the golden corpus before v0.2.92: the Java class ``Account``
    was stored starting on ``package golden;``, ``Account.deposit`` on the
    constructor's closing ``}`` (with a ``body`` that began with that brace),
    and the C++ class ``Point`` on the previous class's ``};``.

    Bounded by ``match_end`` so an all-whitespace match cannot walk past its
    own span. Returns ``match_start`` when the first character is already
    non-whitespace, which is the no-op every already-correct caller sees.
    """
    pos = match_start
    while pos < match_end and text[pos].isspace():
        pos += 1
    return pos


# ── v0.2.92: the ONE declaration-terminator scan ───────────────────────────
def scan_to_declaration_terminator(
    text: str,
    from_pos: int,
    stops: str = ";{",
    limit: int = 4000,
) -> Tuple[Optional[str], int]:
    """First character from ``stops`` at bracket depth 0 at/after ``from_pos``.

    Answers "does this declaration OPEN A BLOCK, or does it just end?" — the
    question every brace-scanning producer in this package assumed away.
    ``_extract_balanced_block`` is only meaningful for a declaration that opens
    a ``{``; run it on a BODILESS declaration (a Rust trait method, a C#
    interface / abstract method, a C++ pure-virtual) and it finds no opener on
    that line, keeps scanning, and latches onto the NEXT construct's braces.
    Measured on the golden corpus before v0.2.92: ``engine.rs``'s trait
    ``fn reset(&mut self);`` (line 26) was stored with ``end_line`` 32 and a
    ``body`` containing the trait's ``}``, the ``impl Resettable for Counter``
    header AND the impl's own ``reset`` body — so the two ``reset`` rows the
    duplicate-identity fix had just separated carried nearly the same text.

    Depth counts ``()``, ``[]`` and ``{}``. The stop test runs BEFORE the depth
    update, so a ``{`` in ``stops`` is reported rather than counted. Depth is
    what keeps a nested terminator from ending the declaration early: a Rust
    ``-> [u8; 4] {`` return type, and a C# statement-lambda expression body
    (``=> Items.Select(x => { var y = x; return y; }).Count();``), both contain
    a ``;`` that is not the terminator.

    Returns ``(None, from_pos)`` when nothing is found inside ``limit`` — the
    conservative outcome; callers fall back to the declaration line rather
    than scanning away.
    """
    depth = 0
    for k in range(from_pos, min(len(text), from_pos + limit)):
        ch = text[k]
        if depth <= 0 and ch in stops:
            return ch, k
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    return None, from_pos


# ── v0.2.92: the ONE block-comment scrub ───────────────────────────────────
def blank_block_comments_preserving_lines(
    text: str,
    open_tok: str = "/*",
    close_tok: str = "*/",
    *,
    line_anchored: bool = False,
) -> str:
    """Remove block comments from ``text`` WITHOUT changing its line count.

    THE DEFECT THIS CLOSES (v0.2.92). Nine extractors each carried their own
    ``re.sub(<open>.*?<close>, " ", text, flags=re.DOTALL)``. Substituting a
    single space for a comment that spans N newlines DELETES N lines from the
    scrubbed copy — and eight of those nine extractors then derive entity line
    numbers from that copy (``content_clean[:m.start()].count('\\n') + 1``)
    while slicing bodies out of ``source_lines``, which is split from the
    ORIGINAL text. Every entity below a multi-line block comment was therefore
    stored with a ``start_line``/``end_line``/``body`` shifted UP by the number
    of lines the comments above it occupied, and the error accumulates down the
    file. Measured on a four-line ``/* … */`` above a controller: the class row
    started on the comment's second line and a method's ``body`` began with a
    comment fragment.

    THE RULE. Newline count in == newline count out, always:

      * a comment containing N >= 1 newlines becomes exactly ``"\\n" * N`` —
        the newlines both hold the line numbering steady AND keep the tokens on
        either side apart (a newline is whitespace, so nothing is glued);
      * a single-line comment (N == 0) becomes ``" "`` — byte-for-byte what
        every call site did before, so ``int/*x*/y`` stays ``int y`` and never
        becomes ``inty``.

    ``open_tok`` / ``close_tok`` are literal delimiters (escaped here, so no
    caller hand-rolls a pattern). ``line_anchored=True`` additionally requires
    both delimiters to start a line — the Ruby ``=begin`` / ``=end`` form,
    whose pre-existing pattern was ``^=begin.*?^=end`` and whose anchoring is
    load-bearing (an ``=end`` inside an expression must not close a comment).

    Non-greedy by construction, so ``/* a */ x /* b */`` is two comments rather
    than one span swallowing ``x``. An UNTERMINATED comment matches nothing and
    is left verbatim — the conservative outcome (a scrub that ran away to EOF
    would delete the rest of the file from the parser's view).

    Pinned by ``tests/test_v0292_wp5_block_comment_scrub.py`` (the newline-count
    invariant, per delimiter family) and by
    ``tests/test_codegraph_golden.py`` (the stored line numbers themselves).
    """
    o = re.escape(open_tok)
    c = re.escape(close_tok)
    if line_anchored:
        pattern = re.compile(rf"^{o}.*?^{c}", re.MULTILINE | re.DOTALL)
    else:
        pattern = re.compile(rf"{o}.*?{c}", re.DOTALL)

    def _blank(m: "re.Match[str]") -> str:
        newlines = m.group(0).count("\n")
        return "\n" * newlines if newlines else " "

    return pattern.sub(_blank, text)


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
    test keeps that true.

    ``end``-KEYWORD LANGUAGES DO NOT USE THIS HELPER (v0.2.92 WP-5b). Ruby and
    Lua close their blocks with the WORD ``end``; ``ruby.py`` and ``lua.py``
    call :func:`extract_end_keyword_block` instead, and nothing in this package
    passes ``language="ruby"`` or ``language="lua"`` here any more
    (``tests/test_v0292_wp5b_end_block_scanner.py`` pins which scanner each
    extractor module uses).

    The two releases of history, because it explains the shape of the fix:
    until WP-5b both of them DID call this helper, and idiomatic Ruby has no
    braces around a class or a method at all — so the scan found no opener,
    fell through to the ``min(start_line + 40, len(source_lines))`` runaway
    branch, and every entity's stored ``body`` ran from its declaration to
    end-of-file. Measured on the golden corpus at the time: all three
    ``ledger.rb`` classes AND all six of its methods ended at line 40 of a
    40-line file. Over-extension there was never a window-size effect; it was
    the no-opener fallback, which is why widening ``max_lookahead`` would not
    have helped and a different scanner was needed.

    Indent-significant languages do not use this helper either — Python goes
    through the AST and bypasses it entirely.

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
# v0.2.92 WP-5b — the ONE ``end``-keyword block scanner
# ---------------------------------------------------------------------------
#
# THE DEFECT THIS CLOSES. ``_extract_balanced_block`` counts BRACES. Ruby and
# Lua terminate their blocks with the WORD ``end``, and idiomatic Ruby has no
# braces around a class or a method at all — so every Ruby class and every Ruby
# method took the no-opener runaway branch above and stored a ``body`` running
# from its declaration to the end of the file. Measured on the shipped golden
# corpus at v0.2.92: all three ``ledger.rb`` classes AND all six of its methods
# ended at line 40 of a 40-line file, and ``vector.lua``'s ``clamp`` ended at
# 39 for a function that closes on 38. That is not an off-by-one: an entity's
# stored text (and therefore its embedding) was every line after it.
#
# WHY A SEPARATE FUNCTION rather than a mode of ``_extract_balanced_block``:
# brace balance is a CHARACTER count over a scrubbed line, ``end`` balance is a
# WORD count that additionally has to decide, per occurrence, whether the word
# opens anything at all. Ruby's statement modifiers (``value = 1 if flag``) use
# the same keyword as the block form and take no ``end`` — counting one of
# those as an opener runs the body on to the next unmatched ``end``, which is
# strictly worse than the bug being fixed. The two scanners share the lexer
# (``_scrub_line_stateful``) and the return convention, and nothing else.


class _EndBlockProfile(NamedTuple):
    """Which words open an ``end``-terminated block, for ONE language."""

    #: keywords that open a block wherever they appear as a bare word.
    always_open: FrozenSet[str]
    #: keywords that open a block ONLY in expression-start position. Anywhere
    #: else they are Ruby statement modifiers, which take no ``end``. Empty for
    #: a language (Lua) that has no modifier form.
    open_at_expression_start: FrozenSet[str] = frozenset()
    #: keywords whose HEADER may be terminated by ``do`` on the same line
    #: (``while x do``, ``for i = 1, n do``). That ``do`` belongs to the header
    #: that already incremented the depth and must not count a second time.
    header_do: FrozenSet[str] = frozenset()
    #: ``def foo = expr`` — Ruby 3.0's endless method — opens no block.
    endless_def: bool = False


#: language key -> profile. Keys are ``lang_dispatch`` keys, the same alphabet
#: ``_LANG_SYNTAX`` and ``codegraph_lang.EXTRACTORS`` use. A language with no
#: row here has no ``end``-keyword blocks, and
#: :func:`extract_end_keyword_block` answers "this declaration opens nothing"
#: for it rather than raising — the walk must never crash on a heuristic.
#: ``tests/test_v0292_wp5b_end_block_scanner.py`` pins that every call site in
#: this package passes a key that IS in this table.
_END_BLOCK_PROFILES: Dict[str, _EndBlockProfile] = {
    # ``case``/``for``/``begin`` have no modifier form in Ruby, so they are
    # unconditional. ``if``/``unless``/``while``/``until`` do, so they are not.
    "ruby": _EndBlockProfile(
        always_open=frozenset({"class", "module", "def", "begin", "case", "for", "do"}),
        open_at_expression_start=frozenset({"if", "unless", "while", "until"}),
        header_do=frozenset({"while", "until", "for"}),
        endless_def=True,
    ),
    # Lua has no statement modifiers: ``if``/``for``/``while`` always open.
    # ``repeat``/``until`` is deliberately ABSENT — that pair is closed by
    # ``until``, not by ``end``, so counting ``repeat`` would never balance
    # while ignoring both is exactly right (any ``end`` inside a repeat body
    # belongs to a nested block that opens inside it).
    "lua": _EndBlockProfile(
        always_open=frozenset({"function", "if", "for", "while", "do"}),
        header_do=frozenset({"for", "while"}),
    ),
}

_END_BLOCK_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

#: A word preceded by one of these is a member/symbol/variable name, not a
#: keyword: ``range.end``, ``:end``, ``@if``, ``$do``.
_END_BLOCK_NOT_KEYWORD_BEFORE: FrozenSet[str] = frozenset(".:@$")

#: A prefix ending in one of these puts the next token at the start of an
#: expression, where Ruby reads ``if`` as a block rather than as a modifier.
#: Deliberately EXCLUDES ``?`` and ``!``: they are Ruby method-name suffixes,
#: so ``valid? if flag`` / ``save! if flag`` are modifiers, not blocks.
_EXPRESSION_START_CHARS: FrozenSet[str] = frozenset("=(,[{;|&")

#: Same, for a prefix ending in a word. ``return``/``next``/``break``/``raise``
#: are NOT here: ``return if done`` is the modifier form.
_EXPRESSION_START_WORDS: FrozenSet[str] = frozenset(
    {"and", "or", "not", "then", "do", "else", "elsif", "when", "in", "ensure", "rescue"}
)

_TRAILING_WORD_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)$")


def _at_expression_start(text: str, pos: int) -> bool:
    """Is ``text[pos:]`` at the beginning of an expression?

    The Ruby modifier test. ``pos`` is the offset of the keyword in a SCRUBBED
    line, so strings are already a single placeholder token and cannot make an
    operator the last visible character.
    """
    prefix = text[:pos].rstrip()
    if not prefix:
        return True
    if prefix[-1] in _EXPRESSION_START_CHARS:
        return True
    m = _TRAILING_WORD_RE.search(prefix)
    return m is not None and m.group(1) in _EXPRESSION_START_WORDS


def _is_endless_def(text: str, pos: int) -> bool:
    """Ruby 3.0 ``def name(args) = expr`` — a method with no ``end``.

    ``pos`` is the offset just past the ``def`` keyword. Reads the method-name
    token (which may itself be an operator: ``==``, ``[]=``, ``value=``), then
    an optional balanced parameter list, then asks whether what follows is a
    bare ``=``. ``def value=(v)`` and ``def ==(other)`` therefore stay ordinary
    methods: their ``=`` is part of the NAME, consumed before the test.
    """
    n = len(text)
    i = pos
    while i < n and text[i].isspace():
        i += 1
    while i < n and not text[i].isspace() and text[i] != "(":
        i += 1
    while i < n and text[i].isspace():
        i += 1
    if i < n and text[i] == "(":
        depth = 0
        while i < n:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        while i < n and text[i].isspace():
            i += 1
    if i >= n or text[i] != "=":
        return False
    return i + 1 >= n or text[i + 1] not in "=>~"


def extract_end_keyword_block(
    # ``Sequence`` rather than ``List``: this function only indexes and takes
    # ``len``, and ``List`` is INVARIANT — a caller holding a
    # ``list[LiteralString]`` (which is what ``"…".split("\n")`` on a literal
    # infers to) cannot pass it to a ``List[str]`` parameter.
    source_lines: Sequence[str],
    start_line: int,
    *,
    language: str,
    max_lookahead: int = 400,
) -> int:
    """End-line of an ``end``-terminated block, by the same contract as
    :func:`_extract_balanced_block`: the **1-indexed line of the token that
    closes the block**, consumed as
    ``'\\n'.join(source_lines[start_line - 1:end_line])``.

    ``start_line`` is the DECLARATION line (``class Foo``, ``def bar``,
    ``function baz()``), not the line after it.

    THREE OUTCOMES, all bounded:

      * the matching ``end`` is found → its line number;
      * the declaration line opens NOTHING (a Ruby endless method
        ``def size = @n``, a one-line construct already closed on it, an
        unknown language) → ``start_line``. That is the conservative answer:
        a one-line body, never the rest of the file;
      * no matching ``end`` inside ``max_lookahead`` → ``min(start_line + 40,
        len(source_lines))``, byte-for-byte the graceful-degradation branch
        ``_extract_balanced_block`` already uses, so the two scanners tell one
        story about runaway input.

    WHAT IT COUNTS. Comments and string literals are removed by the shared
    per-language lexer with state carried across lines, so an ``end`` inside a
    string or a ``=begin`` block is not a closer. A word preceded by ``.``,
    ``:``, ``@`` or ``$`` is a member/symbol/ivar name, and a word followed by
    ``:`` is a hash key (``end:``) — neither is a keyword.

    KNOWN LIMITS, stated rather than implied:
      * a Ruby heredoc body (``<<~SQL``) is not lexed as a string, so an
        ``end`` inside one is counted. The shared lexer has never modelled
        heredocs; this scanner inherits that gap and no more.
      * a ``while``/``for`` header split across lines puts its ``do`` on a
        later line, where the same-line ``header_do`` guard cannot see it, so
        the block is counted twice. Both forms are rare; the failure direction
        is a body that ends LATE, which is the pre-existing behaviour rather
        than a new class of error.
    """
    if start_line < 1 or start_line > len(source_lines):
        return min(start_line + 40, len(source_lines))  # legacy fallback

    profile = _END_BLOCK_PROFILES.get((language or "").strip().lower())
    if profile is None:
        return start_line

    syn = _syntax_for(language)
    scrub_state: Optional[_ScrubState] = None
    depth = 0
    found_opener = False
    lookahead_end = min(start_line - 1 + max_lookahead, len(source_lines))

    for line_idx in range(start_line - 1, lookahead_end):
        scrubbed, scrub_state = _scrub_line_stateful(
            source_lines[line_idx], syn, scrub_state, placeholder="_"
        )
        pending_header_do = False
        for m in _END_BLOCK_WORD_RE.finditer(scrubbed):
            word = m.group(0)
            if m.start() and scrubbed[m.start() - 1] in _END_BLOCK_NOT_KEYWORD_BEFORE:
                continue
            if m.end() < len(scrubbed) and scrubbed[m.end()] == ":":
                continue  # a hash key / label, e.g. `end:` or `if:`
            if word == "end":
                depth -= 1
                if found_opener and depth <= 0:
                    return line_idx + 1
                continue
            if word in profile.always_open:
                if word == "do" and pending_header_do:
                    pending_header_do = False  # belongs to this line's header
                    continue
                if word == "def" and profile.endless_def and _is_endless_def(
                    scrubbed, m.end()
                ):
                    continue
            elif word in profile.open_at_expression_start:
                if not _at_expression_start(scrubbed, m.start()):
                    continue  # a statement modifier — no `end` to match
            else:
                continue
            depth += 1
            found_opener = True
            if word in profile.header_do:
                pending_header_do = True

        if line_idx == start_line - 1 and not found_opener:
            # The declaration itself opened no block. Nothing later in the file
            # can belong to it, so a one-line body is the only honest answer.
            return start_line

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
    #: True only for a block comment. Distinguishes a comment construct from a
    #: string/char construct that happens to reuse the same span-tracking
    #: machinery, so ``keep_strings`` (v0.2.92 WP-G) knows which removed
    #: regions to restore verbatim and which to keep dropping.
    is_comment: bool = False


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
                closer=closer,
                spans_lines=True,
                opener=opener if nested else "",
                is_comment=True,
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
    line: str,
    syn: _LangSyntax,
    state: Optional[_ScrubState] = None,
    *,
    placeholder: str = "",
    keep_strings: bool = False,
) -> Tuple[str, Optional[_ScrubState]]:
    """Remove comments + string literals from ONE line, carrying lexer state.

    Returns ``(code-only text, state for the next line)``. By default the
    removed regions are dropped entirely (delimiters included) — the only
    consumer that wanted that is the ``{``/``}`` counter in
    ``_extract_balanced_block``, and no delimiter this lexer recognises is a
    brace.

    ``placeholder`` (v0.2.92 WP-5b) substitutes ONE occurrence of the given
    text for each removed STRING (not for a comment — nothing follows a line
    comment). Default ``""`` reproduces the drop-entirely behaviour byte for
    byte, so every pre-existing caller is unaffected.

    WHY THE OPTION EXISTS. ``extract_end_keyword_block`` has to decide whether
    a Ruby ``if`` is a block opener or a statement MODIFIER, and it decides it
    from the text preceding the keyword. Dropping a string leaves
    ``x = "hi" if flag`` as ``x =  if flag`` — a prefix ending in ``=``, which
    reads as expression-START position and would classify a modifier as an
    opener, over-running the body by everything up to the next stray ``end``.
    Substituting a single token (``x = _ if flag``) keeps the SHAPE of the line
    while still hiding the string's contents from the keyword scanner.

    ``keep_strings`` (v0.2.92 WP-G) answers a THIRD question, different from
    both defaults above: "what does this line print/emit?" A scanner looking
    for CLI commands baked into a Rust ``format!("…")`` call needs comments
    gone (prose false-positives a regex) but string literals INTACT (the
    command text lives inside them — dropping strings turns a false positive
    into a silent false negative, the worse direction). When set, every
    construct this lexer does NOT classify as ``is_comment`` (quote strings,
    char literals, raw/triple-quote string forms) is reproduced verbatim
    instead of dropped or replaced; comments are still stripped. Mutually
    exclusive with ``placeholder`` in practice (the two answer different
    questions) but not enforced — ``placeholder`` is simply ignored for any
    region ``keep_strings`` already preserved.
    """
    out: List[str] = []
    i = 0
    n = len(line)

    if state is not None:
        was_comment = state.is_comment
        i, state = _scan_construct(line, 0, state)
        if state is not None:
            if keep_strings and not was_comment:
                return line, _carry_state(state, line)
            return "", _carry_state(state, line)
        if keep_strings and not was_comment:
            out.append(line[0:i])
        elif placeholder:
            out.append(placeholder)

    while i < n:
        opened = _open_construct_at(line, i, syn)
        if opened is not None:
            start = i
            is_comment = opened[1].is_comment
            i, state = _scan_construct(line, opened[0], opened[1])
            if state is not None:
                if keep_strings and not is_comment:
                    return "".join(out) + line[start:], _carry_state(state, line)
                return "".join(out), _carry_state(state, line)
            if keep_strings and not is_comment:
                out.append(line[start:i])
            elif placeholder:
                out.append(placeholder)
            continue

        if _line_comment_at(line, i, syn):
            return "".join(out), None  # the rest of the line is a comment

        ch = line[i]
        if ch == '"':
            start = i
            i, state = _scan_construct(
                line,
                i + 1,
                _ScrubState(
                    closer='"', escapes=True, continuation=syn.string_line_continuation
                ),
            )
            if state is not None:
                if keep_strings:
                    return "".join(out) + line[start:], _carry_state(state, line)
                return "".join(out), _carry_state(state, line)
            if keep_strings:
                out.append(line[start:i])
            elif placeholder:
                out.append(placeholder)
            continue

        if ch == "'":
            if syn.char_quote:
                m = _CHAR_LITERAL_RE.match(line, i)
                if m is not None:
                    if keep_strings:
                        out.append(line[i : m.end()])
                    elif placeholder:
                        out.append(placeholder)
                    i = m.end()
                    continue
                # A Rust lifetime ('a) or a C++ digit separator — ordinary text.
                out.append(ch)
                i += 1
                continue
            if syn.single_quote_string:
                start = i
                i, state = _scan_construct(
                    line, i + 1, _ScrubState(closer="'", escapes=True)
                )
                if state is not None:
                    if keep_strings:
                        return "".join(out) + line[start:], _carry_state(state, line)
                    return "".join(out), _carry_state(state, line)
                if keep_strings:
                    out.append(line[start:i])
                elif placeholder:
                    out.append(placeholder)
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
