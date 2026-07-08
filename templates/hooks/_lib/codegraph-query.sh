# shellcheck shell=bash
# _lib/codegraph-query.sh
# Shared code-graph retrieval helper sourced by every hook that injects code-graph
# context (pre-edit-context-inject, pre-bash-context-inject, pre-tool-use). One
# home for the "run code-graph-query search --hook-format" logic that used to be
# inline in pre-edit only.
#
# Why this exists (one concern, one home — CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# v0.2.70 Stream C. The maintainer wanted code-graph injection on FOUR surfaces
# (code-file Edit/Read, selective Bash, code-edit resync, symbol Grep). Without
# a shared helper that would be four copies of the same subprocess+format logic.
# This file is that one home; every surface calls codegraph_query_block. The
# callers own dedup (via _lib/seen-store.sh) — this helper returns RAW blocks.
#
# MUST MATCH: templates/hooks/_lib/codegraph-query.ps1 — the query/format
# contract (CODE:-prefixed --hook-format output, the inner timeout bound) AND the
# codegraph_bash_gate + codegraph_pattern_gate regexes must agree cross-OS. See
# the "MUST MATCH" notes on each gate below.
#
# This file is sourced, never executed — no shebang. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_CODEGRAPH_QUERY_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_CODEGRAPH_QUERY_SOURCED=1

# vco_codegraph_cli: locate the code-graph-query CLI. Echoes its path or empty.
# Resolves against $PROJECT_ROOT (canonical) then $CLAUDE_PROJECT_DIR.
vco_codegraph_cli() {
    local root="${PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-}}"
    [ -n "$root" ] || return 1
    local cli="$root/.claude/scripts/code-graph-query"
    if [ -x "$cli" ] || [ -f "$cli" ]; then
        printf '%s' "$cli"
        return 0
    fi
    return 1
}

# codegraph_query_block <query> <project_arg> <limit> <exclude_path> [anchor]
# Echo the raw "CODE:"-prefixed --hook-format block(s) for <query>, or nothing.
#   $1 query        — the search query (symbol / module name / bash symbol token)
#   $2 project_arg  — "--project Foo" or "" (already shell-token-shaped)
#   $3 limit        — max results (default 2)
#   $4 exclude_path — forwarded to the CLI as `--exclude-file` so candidates
#                     from that file are culled BEFORE the result trim
#                     (v0.2.72 B2 — replaces the old post-hoc line-wise
#                     `grep -v`, which stripped only the CODE: header line and
#                     left orphaned body lines when the anchor's same-file
#                     boost promoted the edited file's own entities)
#   $5 anchor       — optional edited-file path or grep symbol; forwarded as
#                     `--anchor` so the CLI's shared retrieval pipeline biases
#                     the rerank toward call-linked / same-module / shared-type
#                     code (v0.2.72 P2). Empty → pure semantic (MCP parity).
# Soft-fail: CLI absent / error / empty → echo nothing, return 0. Never writes to
# stderr-bound context, never exits non-zero. Bounded by an inner timeout so a
# hung subprocess can't blow the caller's settings.json budget.
codegraph_query_block() {
    local query="$1"
    local project_arg="$2"
    local limit="${3:-2}"
    local exclude_path="${4:-}"
    local anchor="${5:-}"

    [ -n "$query" ] || return 0
    local cli
    cli="$(vco_codegraph_cli)" || return 0
    [ -n "$cli" ] || return 0

    # Build the CLI argv in the function's positional params (bash-3.2-safe —
    # no arrays — and space-safe for the anchor path/symbol).
    set -- search "$query"
    if [ -n "$project_arg" ]; then
        # shellcheck disable=SC2086 — project_arg is intentionally word-split.
        set -- "$@" $project_arg
    fi
    set -- "$@" --limit "$limit" --hook-format
    if [ -n "$exclude_path" ]; then
        # B2: root-fix self-exclusion — the CLI drops the file's candidates
        # pre-trim, so the top-K fills with OTHER files' context instead of
        # being decapitated by a post-hoc grep. MUST MATCH codegraph-query.ps1.
        set -- "$@" --exclude-file "$exclude_path"
    fi
    if [ -n "$anchor" ]; then
        set -- "$@" --anchor "$anchor"
    fi

    # Inner hard bound. Prefer `timeout` (coreutils / busybox); when absent,
    # fall back to a bg-pid + sleep-kill guard so Git-Bash-on-Windows (no
    # timeout) still can't hang the hook.
    local raw=""
    if command -v timeout >/dev/null 2>&1; then
        raw="$(timeout 4 "$cli" "$@" 2>/dev/null || true)"
    else
        local _tmp
        _tmp="$(mktemp 2>/dev/null || printf '%s' "/tmp/cg_$$_$RANDOM")"
        ( "$cli" "$@" >"$_tmp" 2>/dev/null ) &
        local _pid=$!
        ( sleep 4; kill -9 "$_pid" 2>/dev/null ) >/dev/null 2>&1 &
        local _watchdog=$!
        wait "$_pid" 2>/dev/null || true
        kill "$_watchdog" 2>/dev/null || true
        raw="$(cat "$_tmp" 2>/dev/null || true)"
        rm -f "$_tmp" 2>/dev/null || true
    fi

    [ -n "$raw" ] || return 0
    # Cap the volume. Self-reference exclusion happens INSIDE the CLI via
    # --exclude-file (B2) — the old line-wise `grep -v` here stripped only the
    # CODE: header line and left orphaned body lines. Do not re-add it.
    printf '%s\n' "$raw" | head -20
}

# codegraph_pattern_gate <pattern>
# True (0) if <pattern> looks like a CODE IDENTIFIER worth a codegraph lookup:
# snake_case-with-underscore, OR CamelCase, OR a `name(` call, OR a code keyword
# (def/class/func/function/fn) followed by an identifier. Deliberately does NOT
# fire on a bare all-caps word ("TODO"), a bare lowercase word ("hello"), or a
# bare dotted token ("foo.bar") — those are the Grep/pre-bash false-fire cases.
# Used by both the Grep surface and codegraph_bash_gate's tool branch (one home).
# MUST MATCH: codegraph-query.ps1 Test-VcoCodegraphPatternGate.
codegraph_pattern_gate() {
    local p="$1"
    [ -n "$p" ] || return 1
    # snake_case identifier (underscore between word chars):
    if [[ "$p" =~ [A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+ ]]; then
        return 0
    fi
    # CamelCase identifier:
    if [[ "$p" =~ [A-Z][a-z]+[A-Z] ]]; then
        return 0
    fi
    # call shape: name(
    if [[ "$p" =~ [A-Za-z_][A-Za-z0-9_]*\( ]]; then
        return 0
    fi
    # code keyword + identifier (def authenticate / class Foo):
    if [[ "$p" =~ (^|[^A-Za-z0-9_])(def|class|func|function|fn)[[:space:]]+[A-Za-z_] ]]; then
        return 0
    fi
    return 1
}

# codegraph_bash_gate <bash-command>
# The pre-bash-SPECIFIC gate. Fires the codegraph branch ONLY when the bash
# command is genuinely navigating code, short-circuiting (pure-bash, no
# subprocess) for routine ls/cd/git/cat/etc. Returns 0 (fire) / 1 (skip).
#
# THE #1-RISK GATE. Fires when EITHER:
#   (A) the command runs a code-search tool (grep|rg|ag|ack) AND its pattern is a
#       CODE-IDENTIFIER shape (delegates to codegraph_pattern_gate:
#       snake_case-with-underscore, OR CamelCase, OR a `name(` call, OR a code
#       keyword (def/class/func/function/fn) prefix); OR
#   (B) the command references a CODE-FILE path (foo.py, src/bar.rs, ...).
#
# Deliberately does NOT fire on (tested NEGATIVES):
#   ls, cd /tmp, git status, git log a.b.c, cat notes.txt, grep foo.bar
#   (dotted but non-identifier / could be a data filename), grep "TODO"
#   (bare all-caps word, no identifier shape), a bare dotted path arg.
# A bare dotted-token rule is INTENTIONALLY excluded (it would false-fire on
# `grep foo.bar` / `git log a.b.c`) — that is why this gate pairs a code-search
# tool context with codegraph_pattern_gate's identifier-shape check.
#
# MUST MATCH: codegraph-query.ps1 Test-VcoCodegraphBashGate.
codegraph_bash_gate() {
    local cmd="$1"
    [ -n "$cmd" ] || return 1

    # (B) code-file path → fire. The stem before the extension must be a clean
    # filename ([A-Za-z0-9_-]+, NO embedded dots) immediately preceded by a path
    # boundary (start, space, or slash). This deliberately REJECTS multi-dot
    # tokens like `a.b.c` (a git ref, NOT a C file — the `git log a.b.c`
    # negative) while accepting `foo.py`, `src/bar.rs`, ` baz.c`.
    if [[ "$cmd" =~ (^|[[:space:]/])[A-Za-z0-9_-]+\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto)([^A-Za-z0-9]|$) ]]; then
        return 0
    fi

    # (A) code-search tool present? (word-boundary-ish: tool at start of a
    # pipeline segment or after whitespace). Cheap substring + boundary check.
    # When a code-search tool is present, fire iff the command carries a
    # code-IDENTIFIER shape (shared codegraph_pattern_gate — one home).
    if [[ "$cmd" =~ (^|[[:space:]|])(grep|rg|ag|ack)([[:space:]]|$) ]]; then
        if codegraph_pattern_gate "$cmd"; then
            return 0
        fi
    fi

    return 1
}

# codegraph_extract_symbol <text>
# Echo the first code-symbol/path token in <text> (for use as the codegraph
# query), capped to 200 chars. Strips surrounding quotes. Returns the whole text
# capped when no discrete token is isolable (the helper's query embedding
# tolerates extra context). Matches dotted / CamelCase / snake_case / name( /
# code-file path shapes — the union of the gate rules so the QUERY is the symbol,
# not the noisy `grep -rn ...` wrapper.
# P1e (v0.2.75): the extractor previously matched env-assignments
# (LEAN_CTX_OFF=1 via the snake_case rule), non-code paths (/tmp/*.log via
# the dotted rule), grep regex/glob fragments, redirects, and URLs — and,
# worst, fell back to the WHOLE COMMAND TEXT when nothing matched, issuing
# garbage queries for e.g. `git diff <sha>..HEAD`. Now: reject those word
# shapes, require a `/`-bearing word to be a REAL source file (extension
# allow-list minus a non-code deny-list), and return EMPTY when no discrete
# symbol is isolable — the caller then skips injection entirely (a garbage
# query is worse than no query). MUST MATCH codegraph-query.ps1.
#
# Non-code extension deny-list for words containing `/` (path-shaped words).
# A path is only a useful codegraph query when it names a SOURCE file; a
# `/var/log/app.log` or `/etc/foo.yaml` is noise. Kept as a single string
# so the .ps1 sibling can mirror it verbatim.
_CGQ_NONCODE_EXT_RE='\.(log|txt|json|jsonl|yaml|yml|toml|lock|tar|gz|zip|md|html|css)$'
_CGQ_SOURCE_EXT_RE='\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'

codegraph_extract_symbol() {
    local text="$1"
    local tok=""
    for word in $text; do
        # strip surrounding quotes common in commands
        local w="${word#\"}"; w="${w%\"}"; w="${w#\'}"; w="${w%\'}"
        # skip flags / options
        case "$w" in -*) continue ;; esac
        [ -z "$w" ] && continue
        # P1e: skip env-assignments (FOO=bar / LEAN_CTX_OFF=1).
        [[ "$w" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && continue
        # P1e: skip redirects (`2>`, `>>`, `<`) and URLs (http/https).
        [[ "$w" =~ ^[0-9]*[\<\>] ]] && continue
        [[ "$w" =~ ^https?:// ]] && continue
        # P1e: skip words carrying regex/glob metacharacters (grep/rg
        # patterns, globs). `(` is deliberately NOT in this set so a
        # `symbol(` call-shape still matches below. Check each metachar
        # explicitly (a bracket-expression around these is brittle).
        case "$w" in
            *'\'*|*'|'*|*'^'*|*'$'*|*'['*|*'*'*|*'?'*) continue ;;
        esac
        # P1e: a word containing `/` qualifies ONLY as a real SOURCE file —
        # source extension AND not a non-code extension. Otherwise skip
        # (paths like /tmp/x.log or a bare dir are not symbols).
        if [[ "$w" == */* ]]; then
            if [[ "$w" =~ $_CGQ_SOURCE_EXT_RE ]] && ! [[ "$w" =~ $_CGQ_NONCODE_EXT_RE ]]; then
                tok="$w"; break
            fi
            continue
        fi
        if [[ "$w" =~ $_CGQ_SOURCE_EXT_RE ]] \
            || [[ "$w" =~ [A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_] ]] \
            || [[ "$w" =~ [A-Z][a-z]+[A-Z] ]] \
            || [[ "$w" =~ [A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+ ]] \
            || [[ "$w" =~ [A-Za-z_][A-Za-z0-9_]*\( ]]; then
            tok="$w"
            break
        fi
    done
    # P1e: NO whole-text fallback — empty means "no isolable symbol", and
    # the caller must then skip injection (verify pre-bash-context-inject.sh
    # + pre-tool-use.sh handle an empty symbol as no-injection).
    printf '%s' "${tok:0:200}"
}
