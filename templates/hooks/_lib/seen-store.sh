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
# File-name convention (intentional asymmetry, per the read-dedup design):
#   inject → seen_inject_<sid>.txt   (the renamed-from-seen_kg_titles store)
#   reads  → reads_<sid>.txt         (the SAME file pre-tool-use already writes
#                                     for Build Anchor + post-compact wipes; the
#                                     injectors now consult it for src dedup)
# Keeping the reads name as-is means writer (pre-tool-use), consumer (the
# injectors) and reset (post-compact) all agree on one path. MUST MATCH the
# seen-store.ps1 sibling's Get-VcoSeenStorePath.
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
    case "$kind" in
        reads) printf '%s' "$proot/.claude/state/reads_${sid}.txt" ;;
        *)     printf '%s' "$proot/.claude/state/seen_${kind}_${sid}.txt" ;;
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
                    # src is the last field; trim any trailing whitespace.
                    cur_src="${cur_src%% }"
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
