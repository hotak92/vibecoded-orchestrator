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

import re
from typing import Any, Dict, List


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
      3. Skip openers/closers inside:
         - String literals (``"..."`` and ``'...'``) — single-line only;
           multi-line raw/template strings are out of scope (caller
           accepts mild bleed when the function contains a multi-line
           string with unbalanced braces — that's a corner case).
         - Line comments (``//`` and ``#``).
         - Block comments (``/* ... */``) — single-line variant only.
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
    Java, JavaScript, TypeScript, Go, Rust, C#). ``end``-keyword
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
    """
    if start_line < 1 or start_line > len(source_lines):
        return min(start_line + 40, len(source_lines))  # legacy fallback

    counter = 0
    found_opener = False
    lookahead_end = min(start_line - 1 + max_lookahead, len(source_lines))

    for line_idx in range(start_line - 1, lookahead_end):
        line = source_lines[line_idx]
        # Strip line comment + single-line block comment + string literals.
        # This is a best-effort scrub — multi-line strings + multi-line
        # block comments are out of scope (callers degrade gracefully).
        scrubbed = _scrub_for_brace_balance(line)
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


def _scrub_for_brace_balance(line: str) -> str:
    """Best-effort: remove comments + single-line string literals from
    ``line`` so the brace-counter in ``_extract_balanced_block`` doesn't
    mis-count braces inside strings/comments.

    Order matters: comments first (so a quote inside a comment doesn't
    open a string), then strings. Multi-line constructs (raw strings,
    block comments spanning lines, template literals) are intentionally
    not handled — they're rare enough that the caller's graceful
    degradation suffices.
    """
    # Strip line comments. Handle Python ``#``, shell ``#``, C++ ``//``,
    # Lua ``--``. We strip whichever appears first.
    earliest = len(line)
    for marker in ("#", "//", "--"):
        idx = line.find(marker)
        if idx >= 0 and idx < earliest:
            earliest = idx
    line = line[:earliest]

    # Strip single-line block comments: /* ... */ on one line.
    line = re.sub(r"/\*.*?\*/", "", line)

    # Strip string literals. Single-line only — multi-line out of scope.
    line = re.sub(r"\"(?:\\.|[^\"\\])*\"", '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    # Template literals (backticks). Single-line only.
    line = re.sub(r"`(?:\\.|[^`\\])*`", "``", line)

    return line


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
