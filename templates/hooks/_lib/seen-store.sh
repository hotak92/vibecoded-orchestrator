# shellcheck shell=bash
# _lib/seen-store.sh
# Unified per-session read/inject dedup store, sourced by every injecting hook
# (pre-edit-context-inject, pre-bash-context-inject, pre-tool-use). One home for
# the dedup logic that used to live inline in pre-edit-context-inject.sh
# (_filter_seen) and was simply ABSENT in pre-bash (which injected KG blind).
#
# Why this exists (one concern, one home — CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# v0.2.70 Stream E. Before this helper:
#   * KG/codegraph inject-dedup lived only in pre-edit's _filter_seen, keyed
#     COARSELY by node title, so a genuinely-NEW chunk of a seen node was
#     dropped wholesale.
#   * pre-bash injected KG with no dedup at all → re-provided nodes pre-edit
#     had already shown (and vice-versa).
#   * the explicit-Read ledger (reads_<sid>.txt) was written by pre-tool-use
#     but never consulted by any injector — a node whose source the model
#     already Read explicitly was still re-injected.
#   * session_id fallback diverged across hooks ("default" vs date) — a parse
#     glitch could route two chats into one shared "default" store (silent
#     cross-session context loss).
# This file unifies all of that behind one set of functions.
#
# Granularity (per the read-dedup design):
#   * KG    → PER-CHUNK. Key = "<title>#<sha1(body)[:12]>". A new chunk of an
#             already-seen node has a different body → different key → STILL
#             injects (desirable; KG nodes are multi-chunk documents). Re-showing
#             the SAME chunk has the same body → same key → suppressed.
#   * CODE  → PER-ENTITY. Key = "<full_name>" (code entities are atomic units
#             retrieved whole; chunk-splitting adds no value).
#
# NOTE (v0.2.70 content-dedup triage): the CODE key is full_name-ONLY by
# design, NOT a gap. Adding a body-hash would make a re-injected entity with a
# CHANGED body inject AGAIN (the opposite of dedup); and two DISTINCT entities
# that share a body are distinct functions that must both surface (over-collapse
# guard). The Python retrieval helper (claude_mcp_servers/rl_client/content_dedup.py)
# pins the SAME sha1(body)[:12] hash where the layers interoperate (the KG
# title#hash key here), but the code-entity identity axis is intentionally
# full_name on both sides.
#
# TOP RISK (maintainer-flagged): cross-session bleed via a shared "default"
# bucket. vco_hook_session_id (_lib/session-id.sh) returns "" for a missing/
# malformed payload AND the literal "default" for a hostile id (chars outside
# [A-Za-z0-9_-]). EITHER value means "we don't have a trustworthy per-chat key"
# → vco_seen_store_path returns EMPTY, and vco_filter_seen_blocks then DEDUPS
# NOTHING (inject blind) rather than write to a shared bucket. Better to
# occasionally re-inject than to silently suppress one chat's context for
# another.
#
# MUST MATCH: templates/hooks/_lib/seen-store.ps1 — the KEY FORMAT
# ("<title>#<sha1(body)[:12]>" for KG, "<full_name>" for CODE), the
# inject-blind-on-empty/default policy, and the header regex must agree
# cross-OS. Any change to the key shape here MUST be mirrored there or the two
# OSes will dedup differently.
#
# This file is sourced, never executed — no shebang. It is a LIBRARY, not a
# hook; it is NOT registered in settings.json.template.

# --- Idempotent double-source guard ---------------------------------------
# Sourced by multiple hooks; some hooks may (transitively) source it twice.
# Re-defining functions is harmless but we still guard so a future top-level
# side-effect added here can't run twice.
if [ -n "${_VCO_SEEN_STORE_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_SEEN_STORE_SOURCED=1

# vco_seen_store_path: resolve the per-session store file for a given kind.
#   $1 kind        — "inject" (dedup of injected KG/CODE blocks) | "reads"
#                    (explicit-Read ledger).
#   $2 session_id  — already-sanitised id from vco_hook_session_id.
#   $3 project_root
# Echoes the absolute path, OR echoes EMPTY when the session_id is untrustworthy
# ("" or "default") — the caller MUST treat an empty path as "no dedup store;
# inject blind". This is the cross-session-bleed guard (see header).
#
# File-name convention:
#   inject → seen_inject_<sid>.txt  (the renamed-from-seen_kg_titles store)
#   reads  → seen_reads_<sid>.txt   (the INJECTOR reads-ledger, holding the
#                                    REPO-RELATIVE paths the producers' "| src="
#                                    trailers use)
# SF-1 fix: this is DISTINCT from pre-tool-use's Build-Anchor ledger
# `reads_<sid>.txt` (which holds the as-Read path for the harness exact-match
# gate). Conflating the two would (a) mix abs + relative shapes in one file and
# (b) make pre-tool-use's "skip if same path" guard drop the relative injector
# write entirely (the v0.2.70-RC1 bug the SF-1 review caught). post-compact wipes
# BOTH. MUST MATCH the seen-store.ps1 sibling's Get-VcoSeenStorePath.
vco_seen_store_path() {
    local kind="$1"
    local sid="$2"
    local proot="$3"
    # Untrustworthy session id → no store (inject blind). Never compose a
    # shared "default" bucket.
    if [ -z "$sid" ] || [ "$sid" = "default" ]; then
        printf '%s' ""
        return 0
    fi
    [ -n "$proot" ] || { printf '%s' ""; return 0; }
    printf '%s' "$proot/.claude/state/seen_${kind}_${sid}.txt"
}

# vco_to_repo_relative <path> <project_root>
# The ONE shell-side home (SF-1 / one-concern-one-home) for normalising a path to
# the REPO-RELATIVE shape the producers' "| src=<path>" trailers use (KG
# entry.file_path = "knowledge/..."; CODE file_path = repo-relative POSIX). Used
# by the reads-ledger writer + the codegraph self-exclude in pre-tool-use so the
# exact reads-ledger match (rule (b) in vco_filter_seen_blocks) actually fires.
# A path already relative (no project-root prefix) is returned unchanged; an
# absolute path outside the project root is returned unchanged (correctly never
# matches a producer src). Do NOT inline this conversion anywhere else — call it.
# MUST MATCH seen-store.ps1's ConvertTo-VcoRepoRelative.
vco_to_repo_relative() {
    local path="$1" proot="$2"
    [ -n "$path" ] || { printf '%s' ""; return 0; }
    case "$path" in
        "$proot"/*) printf '%s' "${path#"$proot"/}" ;;
        *)          printf '%s' "$path" ;;
    esac
}

# vco_seen_has <file> <key> — true (0) if key is present in the store.
# Soft-fail: a missing/unreadable file means "not seen" (returns 1).
vco_seen_has() {
    local file="$1" key="$2"
    [ -n "$file" ] || return 1
    [ -f "$file" ] || return 1
    grep -Fxq -- "$key" "$file" 2>/dev/null
}

# vco_seen_add <file> <key> — record key in the store. Soft-fail (|| true).
vco_seen_add() {
    local file="$1" key="$2"
    [ -n "$file" ] || return 0
    printf '%s\n' "$key" >> "$file" 2>/dev/null || true
}

# --- Per-session codegraph inject VOLUME cap (v0.2.72 P6) ------------------
# The seen-store above dedups by IDENTITY (same title#hash / full_name is not
# re-injected). That bounds RE-injection but NOT total injection: a long session
# that navigates many DISTINCT code entities injects a fresh block for each,
# unboundedly. This cap bounds the TOTAL number of codegraph injections per
# session_id so a marathon session can't accrete injection blocks without limit.
#
# Mechanism: a tiny per-session counter file (seen_cginject_count_<sid>.txt)
# under the SAME .claude/state/ dir + SAME session keying as the dedup store.
# vco_cg_inject_count_path resolves it (EMPTY for an untrustworthy session id →
# caller runs UNCAPPED, mirroring the inject-blind dedup policy: better to allow
# than to silently gate a session we can't key). The count is split into a
# READ-ONLY predicate (vco_cg_inject_capped) that the caller checks BEFORE the
# heavy codegraph query, and a mutator (vco_cg_inject_record) the caller calls
# ONLY when a block actually emits — so the cap counts REAL injections, not
# query attempts that dedup to nothing. Soft-fail: any read/write error → NOT
# capped / no-op (a broken counter must never break injection).
#
# The cap default is VCO_CG_INJECT_CAP (env-overridable, default 40). A new
# session_id → new counter file → fresh count. post-compact wipes the state dir
# alongside the other seen_* files, so the count resets on compaction too. The
# caller emits the "[codegraph injection cap reached]" note EXACTLY ONCE via the
# vco_cg_inject_note_once sentinel (not on every call past the cap).
#
# MUST MATCH: seen-store.ps1's Get-VcoCgInjectCountPath / Get-VcoCgInjectCap /
# Test-VcoCgInjectCapped / Add-VcoCgInjectRecord / Test-VcoCgInjectNoteOnce —
# the file-name convention (seen_cginject_count_<sid>.txt), the default cap (40),
# the read-only-predicate + record-on-emit split, the fail-open-on-error policy,
# and the emit-the-note-once contract.

# vco_cg_inject_count_path <session_id> <project_root>
# Echo the per-session codegraph-inject counter file path, or EMPTY when the
# session id is untrustworthy ("" or "default") — caller then runs uncapped.
vco_cg_inject_count_path() {
    local sid="$1" proot="$2"
    if [ -z "$sid" ] || [ "$sid" = "default" ]; then
        printf '%s' ""
        return 0
    fi
    [ -n "$proot" ] || { printf '%s' ""; return 0; }
    printf '%s' "$proot/.claude/state/seen_cginject_count_${sid}.txt"
}

# vco_cg_inject_cap — the effective cap (env override, else default 40). A
# non-numeric / non-positive override falls back to the default (a fat-fingered
# VCO_CG_INJECT_CAP must not silently disable ALL injection).
vco_cg_inject_cap() {
    local cap="${VCO_CG_INJECT_CAP:-40}"
    case "$cap" in
        ''|*[!0-9]*) cap=40 ;;
    esac
    [ "$cap" -gt 0 ] 2>/dev/null || cap=40
    printf '%s' "$cap"
}

# vco_cg_inject_capped <count_file>
# READ-ONLY predicate: return 0 (capped — suppress) when the per-session count
# has reached the cap, else 1 (still room). Does NOT mutate the counter, so the
# caller can cheaply short-circuit BEFORE the heavy codegraph query subprocess
# without consuming budget for a query that may yield nothing after dedup.
# Soft-fail: EMPTY count_file (untrustworthy session) OR read error → 1 (not
# capped / uncapped). A broken counter must never block injection.
vco_cg_inject_capped() {
    local file="$1"
    [ -n "$file" ] || return 1   # untrustworthy session → never capped
    [ -f "$file" ] || return 1   # no counter yet → not capped
    local cap n
    cap="$(vco_cg_inject_cap)"
    n="$(cat "$file" 2>/dev/null || printf '0')"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    [ "$n" -ge "$cap" ] 2>/dev/null && return 0
    return 1
}

# vco_cg_inject_record <count_file>
# Increment the per-session inject counter by one. Called ONLY when a block is
# actually emitted (so the cap counts real injections, not query attempts that
# dedup to nothing). Soft-fail: EMPTY count_file or write error → no-op, return
# 0. Idempotency is not required — each emitted injection counts once.
vco_cg_inject_record() {
    local file="$1"
    [ -n "$file" ] || return 0
    local n=0
    if [ -f "$file" ]; then
        n="$(cat "$file" 2>/dev/null || printf '0')"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
    fi
    n=$((n + 1))
    printf '%s\n' "$n" > "$file" 2>/dev/null || true
    return 0
}

# vco_cg_inject_note_once <session_id> <project_root>
# Return 0 (emit the cap note now) EXACTLY ONCE per session, 1 thereafter. Uses a
# sentinel file next to the counter so the "[codegraph injection cap reached]"
# note is emitted a single time, not on every capped call. Soft-fail: an
# unkeyable session (empty path) or a write error → return 1 (do NOT spam the
# note when we can't dedup it).
vco_cg_inject_note_once() {
    local sid="$1" proot="$2"
    [ -n "$sid" ] && [ "$sid" != "default" ] || return 1
    [ -n "$proot" ] || return 1
    local sentinel="$proot/.claude/state/seen_cginject_capnote_${sid}.txt"
    [ -f "$sentinel" ] && return 1
    printf '1\n' > "$sentinel" 2>/dev/null || return 1
    return 0
}

# vco_seen_key_for_header <prefix> <rest>
# Derive the dedup KEY for one injected block, given the header prefix
# ("KG"|"CODE") and the part after "<prefix>: ".
#   KG   → "<title>" (the per-chunk discriminator is appended by the caller via
#          vco_seen_key_kg, which has the block body available).
#   CODE → "<full_name>" (first " | "-delimited field).
# Returns the bare first field; callers add the per-chunk suffix for KG.
vco_seen_first_field() {
    local rest="$1"
    # Strip any accidentally-doubled prefix that an old cache could carry.
    rest="${rest#KG: }"
    rest="${rest#CODE: }"
    local field="${rest%% | *}"
    printf '%s' "${field:0:200}"
}

# vco_seen_hash <text> — short stable hash of a block body for the per-chunk KG
# key. Uses $PY (set by _lib/find-python.sh) for portability (md5sum is
# GNU-only, absent on macOS). Falls back to a length+head sketch when no
# interpreter is available — degrades to coarser dedup, never crashes.
# MUST MATCH seen-store.ps1's Get-VcoSeenHash (sha1, first 12 hex chars).
vco_seen_hash() {
    local text="$1"
    if [ -n "${PY:-}" ]; then
        printf '%s' "$text" | "$PY" -c "import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:12])" 2>/dev/null && return 0
    fi
    # No-interpreter fallback: a length-prefixed head sketch. Stable for the
    # same body, distinct enough for different bodies in practice.
    printf '%s' "$(printf '%s' "$text" | wc -c | tr -d ' ')_$(printf '%s' "$text" | head -c 24 | tr -c 'a-zA-Z0-9' '_')"
}

# vco_filter_seen_blocks <input> <inject_file> <reads_file>
# The migrated + extended _filter_seen. Parses the KG:/CODE: --hook-format block
# stream and emits only blocks that are NOT already-seen this session.
# Suppression rules (a block is dropped if ANY holds):
#   (a) its dedup KEY is already in <inject_file> (re-injection of same content);
#   (b) it is a KG/CODE block whose SOURCE path (header "| src=<abs>" suffix,
#       or the file_path the producer renders) is in <reads_file> — the model
#       already Read that source explicitly, so re-injecting is redundant.
# Newly-emitted blocks have their KEY appended to <inject_file>.
#
# When <inject_file> is EMPTY (untrustworthy session id), dedup is DISABLED:
# every block passes through and nothing is recorded (inject-blind guard).
#
# Block shape (producers, --hook-format):
#   KG:   <title> | <node_type> | score=<n.nn> | <body...>   [| src=<path>]
#   CODE: <full_name> | <collection> | distance=<d> [| source=<peer>] [| src=<path>]
#   <indented/continuation body lines>
#   <blank line terminates the block>
vco_filter_seen_blocks() {
    local input="$1"
    local inject_file="$2"
    local reads_file="${3:-}"

    # Inject-blind mode: no trustworthy store → pass everything through.
    local dedup_on=1
    if [ -z "$inject_file" ]; then
        dedup_on=0
    else
        touch "$inject_file" 2>/dev/null || dedup_on=0
    fi

    local filtered=""
    local cur_prefix=""        # "KG" | "CODE" | ""
    local cur_first=""         # first header field (title / full_name)
    local cur_src=""           # source path from "| src=" suffix
    local cur_block=""         # full text of the block (header + body)
    local cur_body=""          # body-only text (for the per-chunk KG hash)

    _vco_flush() {
        [ -n "$cur_prefix" ] || { cur_prefix=""; cur_first=""; cur_src=""; cur_block=""; cur_body=""; return 0; }
        # Compute the dedup key.
        local key=""
        if [ "$cur_prefix" = "KG" ]; then
            # Per-chunk KG key: title # sha1(body).
            local bh
            bh="$(vco_seen_hash "$cur_body")"
            key="${cur_first}#${bh}"
        else
            # Per-entity CODE key: full_name.
            key="$cur_first"
        fi

        local suppress=0
        if [ "$dedup_on" = "1" ]; then
            # (a) already-injected this session.
            if vco_seen_has "$inject_file" "$key"; then
                suppress=1
            fi
            # (b) source already Read explicitly this session.
            if [ "$suppress" = "0" ] && [ -n "$cur_src" ] && [ -n "$reads_file" ] \
                && vco_seen_has "$reads_file" "$cur_src"; then
                suppress=1
            fi
        fi

        if [ "$suppress" = "0" ]; then
            filtered="${filtered}${cur_block}"
            [ "$dedup_on" = "1" ] && vco_seen_add "$inject_file" "$key"
        fi
        cur_prefix=""; cur_first=""; cur_src=""; cur_block=""; cur_body=""
    }

    local line
    while IFS= read -r line; do
        if [[ "$line" =~ ^(KG|CODE):\ (.+)$ ]]; then
            _vco_flush
            cur_prefix="${BASH_REMATCH[1]}"
            local rest="${BASH_REMATCH[2]}"
            cur_first="$(vco_seen_first_field "$rest")"
            # Extract the optional source path from a "| src=<path>" suffix.
            cur_src=""
            case "$rest" in
                *"| src="*)
                    cur_src="${rest##*| src=}"
                    # src is the last field; trim ALL trailing whitespace (the
                    # trailing run of space/tab/etc.). MUST MATCH seen-store.ps1's
                    # .TrimEnd() (which strips all trailing whitespace, not just a
                    # single space) so the reads-ledger key matches cross-OS.
                    cur_src="${cur_src%"${cur_src##*[![:space:]]}"}"
                    ;;
            esac
            cur_block="${line}"$'\n'
            cur_body=""
        elif [ -n "$cur_prefix" ]; then
            cur_block="${cur_block}${line}"$'\n'
            cur_body="${cur_body}${line}"$'\n'
        else
            # Pre-amble / stray line not part of any block — pass through only
            # if it carries non-whitespace, so deduped-away blocks don't leak
            # a blank that makes downstream HAS_* checks think we have output.
            if [[ "$line" =~ [^[:space:]] ]]; then
                filtered="${filtered}${line}"$'\n'
            fi
        fi
    done <<< "$input"
    _vco_flush

    printf '%s' "$filtered"
}
