#!/usr/bin/env bash
# leanctx-bash-env.sh - lean-ctx alias shim for non-interactive Bash subprocesses
#
# Sourced via BASH_ENV so Claude Code Bash tool subprocesses get the same
# command-output compression (~90-97%) that interactive shells get via ~/.bashrc.
#
# NOTES FOR MAINTAINERS
# ---------------------
# Claude Code Bash tool spawns non-interactive subprocesses; ~/.bashrc is NOT
# sourced. Setting BASH_ENV=<this file> in .claude/settings.json env block makes
# Bash source this shim before every non-interactive command.
#
# PLATFORM QUIRK: Claude Code reads settings.json on startup. BASH_ENV takes
# effect only after a full VS Code/Claude Code restart. If output looks
# uncompressed after install, restart VS Code and try again.
#
# ALIAS LIMITATION: bash non-interactive subprocesses don't expand aliases by
# default. We use shell functions instead - these work in all bash modes.
#
# BYPASS OPTIONS:
#   1. One-shot:     LEAN_CTX_OFF=1 git status
#   2. Helper:       lean-ctx bypass "git status"
#
# VERIFICATION (after install + VS Code restart):
#   SHIM=<project-root>/.claude/scripts/leanctx-bash-env.sh
#   env -i HOME=$HOME PATH=/usr/bin:/bin BASH_ENV=$SHIM bash -c 'git status' | wc -c
#   env -i HOME=$HOME PATH=/usr/bin:/bin bash -c 'git status' | wc -c
#   env -i HOME=$HOME PATH=/usr/bin:/bin BASH_ENV=$SHIM LEAN_CTX_OFF=1 bash -c 'git status' | wc -c

# Idempotency guard - safe to source multiple times
[ "${_LEANCTX_BASH_ENV_LOADED:-}" = "1" ] && return 0
_LEANCTX_BASH_ENV_LOADED=1

# Opt-out: LEAN_CTX_OFF=1 disables all wrapping for this subprocess
[ "${LEAN_CTX_OFF:-}" = "1" ] && return 0

# Locate the lean-ctx binary
_lc_bin=""
if command -v lean-ctx >/dev/null 2>&1; then
    _lc_bin="$(command -v lean-ctx)"
elif [ -x "$HOME/.cargo/bin/lean-ctx" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
    _lc_bin="$HOME/.cargo/bin/lean-ctx"
fi

# Bail silently if lean-ctx is not installed - never break Bash for users without it
[ -z "$_lc_bin" ] && return 0

# Define wrapper functions (not aliases) - functions work in non-interactive bash.
# Each wrapper passes the full command + args as a quoted string to lean-ctx -c,
# which is the invocation form that triggers output compression.
_lc_make_wrapper() {
    local cmd="$1" bin="$2"
    # shellcheck disable=SC2183
    eval "
    $cmd() {
        \"$bin\" -c \"$cmd \$*\"
    }
    "
}

for _lc_cmd in git npm pnpm yarn cargo docker docker-compose gh pip pip3 ruff go golangci-lint eslint prettier tsc grep curl wget; do
    _lc_make_wrapper "$_lc_cmd" "$_lc_bin"
done

ls() { "$_lc_bin" -c "ls $*"; }
find() { "$_lc_bin" -c "find $*"; }
kubectl() { "$_lc_bin" -c "kubectl $*"; }
k() { "$_lc_bin" -c "kubectl $*"; }

unset _lc_bin _lc_cmd
export LEAN_CTX_ENABLED=1
# No lean-ctx: ON banner - silent in non-interactive subprocesses
