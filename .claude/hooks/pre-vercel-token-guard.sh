#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# PreToolUse hook: Prevent vercel CLI invocations that would echo the token
# back to stdout (which leaks `vcp_*` strings into Claude's tool-result view).
#
# Hook capability note (verified against code.claude.com/docs/en/hooks):
#   - Hooks CANNOT modify stdout/stderr of a tool that has already run.
#   - PostToolUse fires AFTER execution and cannot scrub output.
#   - PreToolUse is the only way to prevent the leak — by blocking the
#     specific invocation pattern before it runs.
#
# What this hook blocks:
#   `vercel ... --token=...` and `vercel ... --token ...`
#   The vercel CLI echoes the token back in its `next:` block on success.
#
# What it tells Claude to do instead:
#   `export VERCEL_TOKEN=$(cat ~/.vct-secrets/shared/vercel_token) && vercel ... --yes`
#   The CLI reads VERCEL_TOKEN env transparently and never prints it.
#
# Bypass: VCT_DISABLE_HOOKS=1 in the shell, or remove this hook from
# .claude/settings.json.

TOOL_NAME="$1"
TOOL_ARGS="$3"

# Only inspect Bash invocations
[[ "$TOOL_NAME" != "Bash" ]] && exit 0

CMD="$TOOL_ARGS"

# Match `vercel` (or its absolute/local-bin paths) followed somewhere by
# --token=... or --token <value>. We accept any preceding pipeline stages.
if echo "$CMD" | LEAN_CTX_OFF=1 command grep -qE '(^|[[:space:]/])vercel([[:space:]]|$)'; then
    if echo "$CMD" | LEAN_CTX_OFF=1 command grep -qE -- '--token[= ]'; then
        cat >&2 <<'EOF'
BLOCKED: `vercel --token=...` echoes the token back in stdout (in the
`next:` block on success), which leaks `vcp_*` strings into the tool-result
view. Use the env var instead, which the CLI reads transparently and never
prints:

    export VERCEL_TOKEN=$(cat ~/.vct-secrets/shared/vercel_token) && \
      ./node_modules/.bin/vercel deploy --prod --yes

If you really need --token (e.g. machine that has no env access), pipe the
output through `2>&1 | tail -3` or redirect stderr to /dev/null and only
keep stdout (vercel writes the deployment URL on stdout, errors on stderr).
EOF
        exit 2
    fi
fi

exit 0
