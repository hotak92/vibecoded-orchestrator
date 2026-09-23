#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# gateway-usage-statusline.sh — one compact line of subscription usage for
# Claude Code's `statusLine`, e.g.
#
#   Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72% │ Qwen 1.2M tok/mo
#
# It prints what the local model gateway answers on
# `GET /usage/windows?format=line` — the line is RENDERED by the gateway
# (model_router.usage_windows.render_line), so this script and its .ps1
# sibling cannot disagree about the format. The gateway answers from its own
# cache; this script never calls a vendor.
#
# Enable it yourself (VCO does not write `statusLine` into your settings: a
# project-level value would override one you set in ~/.claude/settings.json):
#
#   "statusLine": {"type": "command",
#                  "command": "bash .claude/scripts/gateway-usage-statusline.sh"}
#
# The status line is drawn by the TERMINAL client (`claude`); the VS Code
# panel does not render it.
#
# Contract: exit 0, always; print the line or nothing. It runs on every
# status-line refresh, so it is bounded (curl --max-time 0.6) and SILENT on
# every failure — no gateway, no token, a 401, a timeout: an empty line, never
# an error message in the user's status bar.
#
# Secrets: the gateway's host token is read from its owner-only file and
# handed to curl on STDIN (`-H @-`), never in argv, where /proc/<pid>/cmdline
# would show it to every local user.
#
# Port resolution MUST MATCH vco_lib.vscode_settings.resolve_gateway_ports
# (env pin, live port file, the launcher's last-port record, then the
# documented default) — the parity test in
# tests/test_gateway_usage_statusline.py drives both with the same fixtures.

state_dir="${VCT_STATE_DIR:-$HOME/.vct}"

_valid_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 > 0 && 10#$1 < 65536 ))
}

_port_from_file() {
    local value=""
    [ -r "$1" ] || return 1
    IFS= read -r value < "$1" || [ -n "$value" ] || return 1
    value="${value//[[:space:]]/}"
    _valid_port "$value" || return 1
    printf '%s' "$((10#$value))"
}

port=""
pin="${VCT_MODEL_GATEWAY_PORT:-}"
pin="${pin//[[:space:]]/}"
if [ -n "$pin" ] && _valid_port "$pin"; then
    port="$((10#$pin))"
fi
[ -n "$port" ] || port="$(_port_from_file "$state_dir/model-gateway.port")"
[ -n "$port" ] || port="$(_port_from_file "$state_dir/model-gateway.last-port")"
# MUST MATCH model_router.config.DEFAULT_PORT.
[ -n "$port" ] || port=11436

token=""
[ -r "$state_dir/model-gateway.token" ] || exit 0
IFS= read -r token < "$state_dir/model-gateway.token" || [ -n "$token" ] || exit 0
token="${token//[[:space:]]/}"
[ -n "$token" ] || exit 0
command -v curl >/dev/null 2>&1 || exit 0

line="$(curl -fsS --noproxy '*' --connect-timeout 0.3 --max-time 0.6 \
    -H @- "http://127.0.0.1:${port}/usage/windows?format=line" \
    2>/dev/null <<<"Authorization: Bearer ${token}")" || exit 0
line="${line%%$'\n'*}"
line="${line%$'\r'}"
printf '%s' "$line"
exit 0
