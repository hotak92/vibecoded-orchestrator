---
name: Bug report
about: Something is broken or behaves unexpectedly
title: "[Bug] "
labels: bug
assignees: ''
---

## Description

A clear description of what is wrong.

## Steps to reproduce

1. ...
2. ...
3. ...

## Expected behaviour

What you expected to happen.

## Actual behaviour

What actually happened. Include error messages, stack traces, log lines.

## Environment

- OS: (e.g. Ubuntu 24.04, macOS 14.5, Windows 11 + WSL2)
- Python: (output of `python3 --version`)
- Claude Code version: (output of `claude --version`, if applicable)
- vibecoded-orchestrator commit: (output of `git rev-parse --short HEAD` in the repo)
- Install mode: clean install / update / dev / from launcher

## Logs / output

```
paste relevant log output here
```

If the bug involves an MCP server, include the output of `claude mcp list`.

## Additional context

Anything else that helps — recent config changes, related issues, screenshots.
