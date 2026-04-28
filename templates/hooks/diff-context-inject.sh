#!/usr/bin/env bash
# Diff-based context injection — only inject CHANGED sections of CONTEXT_STATE.md
# First prompt: create baseline snapshot (full injection handled by session-start hooks)
# Subsequent prompts: output only changed sections (or nothing if unchanged)
# After /compact: reset baseline (detected via compact flag)

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

CONTEXT_FILE=".claude/CONTEXT_STATE.md"
SNAPSHOT_DIR="${TMPDIR:-/tmp}/claude_ctx_snapshots"
SESSION_ID="${CLAUDE_SESSION_ID:-default}"
SNAPSHOT_FILE="$SNAPSHOT_DIR/snapshot_${SESSION_ID}"
COMPACT_FLAG="$SNAPSHOT_DIR/compact_flag_${SESSION_ID}"

mkdir -p "$SNAPSHOT_DIR"

# If compact flag exists, reset baseline
if [ -f "$COMPACT_FLAG" ]; then
    rm -f "$COMPACT_FLAG" "$SNAPSHOT_FILE"
fi

# If CONTEXT_STATE.md doesn't exist, nothing to do
[ -f "$CONTEXT_FILE" ] || exit 0

# If no snapshot exists, create baseline (first prompt — full injection done by session-start hook)
if [ ! -f "$SNAPSHOT_FILE" ]; then
    cp "$CONTEXT_FILE" "$SNAPSHOT_FILE"
    exit 0
fi

# Quick check: if files are identical, nothing to do
if cmp -s "$CONTEXT_FILE" "$SNAPSHOT_FILE"; then
    exit 0
fi

# Files differ — find which ## sections changed using diff
# Get line numbers that changed
changed_lines=$(diff --unchanged-line-format='' --new-line-format='%dn ' "$SNAPSHOT_FILE" "$CONTEXT_FILE" 2>/dev/null || true)

if [ -z "$changed_lines" ]; then
    # diff found differences but no new lines — file got shorter
    # Output a note and update snapshot
    echo "[Context updated — sections removed from CONTEXT_STATE.md]"
    cp "$CONTEXT_FILE" "$SNAPSHOT_FILE"
    exit 0
fi

# Find which ## sections contain the changed lines
changed_sections=""
current_section=""
line_num=0
while IFS= read -r line; do
    line_num=$((line_num + 1))
    if [[ "$line" =~ ^##\  ]]; then
        current_section="$line"
    fi
    # Check if this line number is in our changed set
    for cl in $changed_lines; do
        if [ "$line_num" -eq "$cl" ] && [ -n "$current_section" ]; then
            # Add to changed sections (dedup later)
            if [[ "$changed_sections" != *"$current_section"* ]]; then
                changed_sections="${changed_sections}${current_section}"$'\n'
            fi
            break
        fi
    done
done < "$CONTEXT_FILE"

# Output changed sections
if [ -n "$changed_sections" ]; then
    echo "[Context update — changed sections:]"
    echo ""
    while IFS= read -r section_header; do
        [ -z "$section_header" ] && continue
        # Extract this section from current file (from header to next ## or EOF)
        awk -v header="$section_header" '
            $0 == header { found=1 }
            found && /^## / && $0 != header { exit }
            found { print }
        ' "$CONTEXT_FILE"
        echo ""
    done <<< "$changed_sections"
fi

# Update snapshot
cp "$CONTEXT_FILE" "$SNAPSHOT_FILE"
exit 0
