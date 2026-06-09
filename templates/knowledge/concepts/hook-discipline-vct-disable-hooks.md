---
title: Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch
type: concept
tags: [hooks, debugging, ci, security, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-05-22T00:00:00Z
status: active
---

# Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch

A single env-driven kill-switch for every `.claude/hooks/*.sh` shell hook in vibecoded-orchestrator. Set `VCT_DISABLE_HOOKS=1` and every hook exits 0 cleanly without doing its work.

## What it is

A short guard placed near the top of every shell hook in `.claude/hooks/`:

```bash
# After the standard env-scrub block, before any work:
if [ "${VCT_DISABLE_HOOKS:-}" = "1" ]; then
  exit 0
fi
```

Coverage: every shell hook in the orchestrator. Verified in CI by `pytest tests/test_hooks_disable_guard.py` which greps each hook for the guard and asserts presence. Adding a new hook without the guard fails CI.

**Coverage**: all 31 shell hooks under `.claude/hooks/` carry the guard.

**`set -e` / `pipefail` discipline**: CI gate `check_hook_set_directives.py` enforces that any hook using `set -e` MUST also enable `pipefail` (i.e., `set -euo pipefail`). New hooks that violate the pattern fail the gate.

## How it works

1. **Env-scrub first, then guard**. The guard sits AFTER the credential-scrub block. Order matters: a hook should not be able to leak secrets even when disabled.
2. **Exit 0, not exit 1**. Hooks that exit non-zero block the parent tool call (per Claude Code hook protocol). The escape hatch is a graceful no-op.
3. **No partial application**. Either all hooks honor it or none — anything in between makes the user's mental model unreliable. The CI guard test enforces "all".
4. **Persists across sessions if exported in shell rc**. For ad-hoc disable: `VCT_DISABLE_HOOKS=1 claude`. For persistent debug: `export VCT_DISABLE_HOOKS=1` in a debug shell.

## Why it matters

**Debugging**: when a hook misbehaves (slow, hangs, eats tokens, breaks on an edge case), users need a one-knob disable that does not require editing each hook or `.claude/settings.json` matchers. Without this, the debugging loop is "comment hook out → run → uncomment → forget → discover later".

**CI**: GitHub Actions / GitLab CI runners do not have the user's container runtime, KG state, or interactive session. Hooks that probe Weaviate, write KG sidecars, or capture telemetry are noise (or outright failures) in CI. The escape hatch lets test scripts opt out without rewriting workflow YAML to skip individual hooks.

**Trust**: when a user pulls the orchestrator, the install runs `install.py` which fires hooks via PostToolUse. If hooks fail silently or hang, the install looks broken. `VCT_DISABLE_HOOKS=1 python install.py` gives a clean install path with hooks deferred to first session.

**Regression test**: `tests/test_hooks_disable_guard.py` parses every `.claude/hooks/*.sh`, looks for the guard pattern (`VCT_DISABLE_HOOKS:-` followed by `exit 0` within 3 lines), and fails if any hook lacks it.

## Files

- `.claude/hooks/*.sh` — every hook carries the guard
- `tests/test_hooks_disable_guard.py` — coverage assertion
- `docs/CONFIGURATION.md` — user-facing documentation
- `CLAUDE.md` — quick reference

## Related discipline

- **Env-scrub block**: every hook strips ~15 secret-bearing env vars before running. See [[Orchestrator Security Model]].
- **Cross-OS portability**: hooks use `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}` instead of hardcoded `/tmp`. See [[Cross-OS Hook Portability]].
- **`bash -n` syntax check**: every commit touching `.claude/hooks/` runs `bash -n` in CI. Catches missing fi / esac / done before they reach a user.

## Failure modes prevented

1. **User commits a new hook without the guard** — caught by the regression test.
2. **User assumes "set it and forget it"** — documented prominently in `docs/CONFIGURATION.md` and `CLAUDE.md` so users find it before they reach for `.claude/settings.json` matcher edits.
3. **Hooks-leak-secrets-when-disabled** — guard placement after env-scrub means even a busted hook that crashes between env-scrub and the guard cannot leak.

## See also

- `docs/CONFIGURATION.md` "Disabling hooks for debugging or CI"
- [[uses::Claude Code Hooks]]
- [[Cross-OS Hook Portability]]
