#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# rc-native.sh — Claude Code Remote Control as a detached native-auth
# background server (POSIX sibling of rc-native.ps1; same commands, same
# state layout, same messages).
#
# Runs `claude remote-control` (server mode) with the gateway's routing
# env vars stripped from its environment, detached via setsid, and prints
# the claude.ai/code join URL. Why this exists: a VS Code panel pointed
# at the VCO model gateway sets ANTHROPIC_BASE_URL, and Remote Control is
# ENDPOINT-gated (Claude Code v2.1.196+): it initializes ONLY in sessions
# talking directly to api.anthropic.com, even when signed in with
# claude.ai — API keys and setup/OAuth env tokens are refused by a second,
# independent full-scope-login gate. No gateway or panel configuration
# can change that. The supported shape is the two coexisting: the panel
# keeps the gateway; this background server (native subscription auth)
# provides Remote Control for the machine. Do not downgrade Claude Code
# below v2.1.196 to get Remote Control through the gateway — auto-update
# reverts the downgrade and takes a month of fixes with it.
#
# Operational facts encoded here (verified against Claude Code 2.1.258):
#   1. Workspace trust is a CLI concept, separate from VS Code panel
#      usage: server mode from a directory used only in the panel fails
#      with "Workspace not trusted". Remedy: ONE interactive `claude` run
#      in that directory, accepting the trust dialog — not automatable
#      headlessly. The script detects the string and says exactly this.
#   2. The first server run asks "Enable Remote Control? (y/n)" once;
#      acceptance persists. A `y` is fed through a never-EOFing pipe on
#      every start (an unread `y` is harmless; the open pipe is also what
#      keeps the detached process's stdin alive).
#   3. The server is session-detached, not a service: it does NOT survive
#      a reboot. `start` brings it back (idempotent), which is expected.
#   4. Trust is never saved for $HOME — the script refuses to start there.
#
# Usage:
#   rc-native.sh start [name]    # detached, idempotent; prints the join URL
#   rc-native.sh status [name]   # pid + current join URL
#   rc-native.sh url [name]      # print just the URL
#   rc-native.sh stop [name]     # kill the whole process group
#   rc-native.sh logs [name]     # tail the log (URL redacted)
#
# `name` defaults to the current directory's basename. State lives under
# ~/.cache/rc-native/ (pidfile + log, mode 600).

set -euo pipefail
umask 077

DIR="$HOME/.cache/rc-native"
NAME="${2:-$(basename "$PWD")}"
PIDFILE="$DIR/$NAME.pid"
LOGFILE="$DIR/$NAME.log"
mkdir -p "$DIR"

die() { echo "rc-native: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
rc-native — Claude Code Remote Control as a detached background server.

  rc-native start [name]    detached, idempotent; prints the join URL
  rc-native status [name]   pid + current join URL
  rc-native url [name]      print just the URL
  rc-native stop [name]     kill the server (whole process group)
  rc-native logs [name]     tail the log (URL redacted)

`name` defaults to the current directory's basename. State lives under
~/.cache/rc-native/. The server does not survive a reboot; start brings
it back. Requires a full-scope claude.ai login (API keys and setup tokens
are refused by Remote Control).
USAGE
}

alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

find_url() {
  grep -oE 'https://claude\.ai/code\?environment=[A-Za-z0-9_-]+' "$LOGFILE" 2>/dev/null | tail -1
}

case "${1:-help}" in
  start)
    [ "$PWD" != "$HOME" ] || die "refusing to start from \$HOME — the trust dialog never saves trust for the home directory. cd into a project first."
    if alive; then
      echo "rc-native: '$NAME' already running (pid $(cat "$PIDFILE"))."
      u="$(find_url)"
      if [ -n "$u" ]; then echo "  $u"; fi
      exit 0
    fi
    command -v claude >/dev/null 2>&1 || die "claude not found on PATH."
    # Rotate the previous log (it holds the old join URL, now stale).
    [ -f "$LOGFILE" ] && mv -f "$LOGFILE" "$LOGFILE.old" || true
    # Detach: setsid gives the pipeline its own session; the leader's pid
    # is also the process-group id, so `stop` can `kill -- -PID` everything
    # (bash + sleep + claude) in one shot. The never-EOFing pipe answers
    # the one-time consent prompt and keeps the detached stdin alive; the
    # `env -u` list strips every gateway routing var the calling shell may
    # carry, so the session reaches api.anthropic.com with stock auth.
    setsid bash -c '
      (printf "y\n"; exec sleep infinity) |
        exec env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN \
          -u ANTHROPIC_API_KEY -u ANTHROPIC_MODEL \
          -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL \
          -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u ANTHROPIC_DEFAULT_FABLE_MODEL \
          -u ANTHROPIC_SMALL_FAST_MODEL -u CLAUDE_CODE_SUBAGENT_MODEL \
          -u CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY \
          claude remote-control --no-create-session-in-dir --name "'"$NAME"'"
    ' </dev/null >>"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    # Wait for readiness, surfacing the known failure mode by name.
    for _ in $(seq 1 25); do
      sleep 1
      if grep -q "Workspace not trusted" "$LOGFILE" 2>/dev/null; then
        echo "rc-native: '$NAME' failed — workspace $(pwd) is not trusted for the CLI." >&2
        echo "  Fix: run \`claude\` in this directory once (interactive) and accept the trust dialog, then re-run start. VS Code panel usage does NOT record CLI trust." >&2
        kill -- "-$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
        exit 1
      fi
      u="$(find_url)"
      if [ -n "$u" ] && grep -q "Ready" "$LOGFILE"; then
        echo "rc-native: '$NAME' running (pid $(cat "$PIDFILE"))."
        echo "  Join from phone app (Code tab) or browser:"
        echo "  $u"
        exit 0
      fi
      kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
    done
    echo "rc-native: '$NAME' did not report Ready within 25s — check the logs command." >&2
    tail -5 "$LOGFILE" >&2 || true
    exit 1
    ;;
  status)
    if alive; then
      echo "rc-native: '$NAME' running (pid $(cat "$PIDFILE"))."
      u="$(find_url)"
      if [ -n "$u" ]; then
        echo "  $u"
      else
        echo "  (no join URL in the log yet)"
      fi
    else
      echo "rc-native: '$NAME' not running."
      exit 1
    fi
    ;;
  url)
    alive || die "'$NAME' is not running (start it first)."
    u="$(find_url)"
    [ -n "$u" ] || die "no join URL in $LOGFILE yet."
    echo "$u"
    ;;
  stop)
    if alive; then
      PID="$(cat "$PIDFILE")"
      kill -- "-$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
      sleep 1
      kill -9 -- "-$PID" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo "rc-native: '$NAME' stopped."
    else
      rm -f "$PIDFILE"
      echo "rc-native: '$NAME' was not running."
    fi
    ;;
  logs)
    [ -f "$LOGFILE" ] || die "no log for '$NAME'."
    tail -40 "$LOGFILE" | sed -e 's/environment=[A-Za-z0-9_-]*/environment=<redacted>/g'
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    die "unknown command '$1' (start|status|url|stop|logs)."
    ;;
esac
