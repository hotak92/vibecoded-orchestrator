---
name: rc-native
description: Start/manage Claude Code Remote Control (claude.ai/code + mobile app) as a detached native-auth background server on this machine. AUTO-STARTS the backend on every invocation (idempotent). Use when the user wants Remote Control, phone access to their sessions, or asks about /remote-control failing with "Remote Control initialization failed". If this project's VS Code panel is pointed at the VCO model gateway, Remote Control can never initialize there (endpoint-gated) — this skill runs the native-auth server alongside the panel instead.
short_desc: "rc-native: detached native-auth Remote Control server alongside the gateway panel"
keywords: [remote-control, "remote control", rc-native, claude.ai, "phone access", "mobile app", "/remote-control", "Remote Control initialization failed", endpoint gate, native auth]
argument-hint: "[start|status|url|stop|logs]"
---

# rc-native — Remote Control alongside the gateway panel

Remote Control is **endpoint-gated** (Claude Code v2.1.196+): it initializes
only in sessions talking **directly to `api.anthropic.com`**. When
`ANTHROPIC_BASE_URL` points anywhere else — including the VCO model gateway
— `/remote-control` always fails with "Remote Control initialization
failed", *even when signed in with claude.ai* (OAuth does not bypass the
gate), and env-token auth (API key, `setup-token`, `CLAUDE_CODE_OAUTH_TOKEN`)
is refused by a second, independent full-scope-login gate. No configuration
of the panel or the gateway can change this, and a per-workspace
`.vscode/settings.json` override cannot split the panel either
(`claudeCode.environmentVariables` has VS Code setting scope `machine`).
Verified against Claude Code 2.1.258. **Do not downgrade below v2.1.196 to
get it back** — auto-update reverts the downgrade and takes a month of
fixes with it.

The supported shape is **two coexisting surfaces**:

- The **VS Code panel** keeps the model gateway (mixed picker).
- A **detached native-auth server** provides Remote Control for the whole
  machine: sessions opened from claude.ai/code or the Claude mobile app
  spawn and run locally, on the Anthropic subscription.

## On EVERY invocation: auto-start the backend first

The whole point of this skill is that the backend is available, so the
first action is always a `start` (pick the sibling for the platform):

```
bash .claude/scripts/rc-native.sh start          # POSIX (Linux/macOS)
powershell -NoProfile -ExecutionPolicy Bypass -File .claude/scripts/rc-native.ps1 start   # Windows
```

Both are **idempotent**: an already-running server just prints the pid and
the join URL. The Windows line uses `powershell` (Windows PowerShell 5.1,
present on every stock Windows since 2009) with `-ExecutionPolicy Bypass`
because stock Windows' default policy is Restricted and would otherwise
refuse the script — do not shorten it to bare `pwsh`, which is PowerShell 7,
a separate install most Windows machines do not have (PowerShell 7 users can
substitute `pwsh -File`; the script supports both). Do this before answering, whatever the user asked for — then
give them the join URL. Both siblings take an optional server name
(defaulting to the current directory's basename) and keep state under
`~/.cache/rc-native/` (pidfile + log).

Exceptions to auto-start:

- The user explicitly asked to **stop** Remote Control → run `stop`, don't
  restart.
- `start` fails with **"Workspace not trusted"** → the current directory
  was never used with the plain `claude` CLI. Tell the user the one manual
  step: run `claude` in this directory once, interactively, and accept the
  trust dialog — it cannot be automated headlessly, and VS Code panel usage
  does NOT record CLI trust. After they've done it, re-run `start`.
- `start` fails with an **auth error** → have the user run `claude` once in
  a terminal to refresh their OAuth login, then retry. Never substitute an
  API key or `CLAUDE_CODE_OAUTH_TOKEN` — Remote Control rejects both.

Note: the server does not survive a reboot (it is session-detached, not a
boot service — by design). If `start` reports "not running" after a fresh
boot, that is expected — auto-start brings it back. The first-ever start
answers the one-time **"Enable Remote Control? (y/n)"** consent prompt
automatically (acceptance persists; the unread answer on later starts is
harmless — both scripts keep the server's stdin open for its lifetime,
which the detached process needs).

## Commands (both siblings, same names and messages)

```
start [name]   # detached, idempotent; prints the claude.ai/code join URL
status [name]  # pid + current join URL
url [name]     # print just the URL
stop [name]    # kill the server (whole process group / process tree)
logs [name]    # tail the log (join URL redacted for safe pasting)
```

POSIX: `bash .claude/scripts/rc-native.sh <command> [name]` ·
Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .claude/scripts/rc-native.ps1 <command> [name]`.

## Prerequisites to check before `start`

1. **Not the home directory** — both siblings refuse to start there (trust
   is never saved for `$HOME`).
2. **CLI-trusted working directory** — see the "Workspace not trusted"
   exception above; the fix is one interactive `claude` run in that
   directory.
3. **Full-scope claude.ai login** — see the auth exception above.

## What to tell the user after `start`

Give them the URL and say: open it on the phone (Claude app → Code tab) or
in a browser to open sessions that run on this machine. The server keeps
running after the terminal/editor closes; `stop` (only on explicit request)
ends it.
