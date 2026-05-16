---
title: Cross-OS Hook Portability
type: concept
tags: [hooks, cross-os, portability, podman, bash, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# Cross-OS Hook Portability

vibecoded-orchestrator's `.claude/hooks/*.sh` and `install.py` ship to Linux, macOS, and Windows-via-Git-Bash. Several hardcoded assumptions had to be replaced to make the same scripts portable across all three.

## What it is

Five concrete portability fixes that turned the orchestrator from "works on Linux" to "works on Linux + macOS + Git-Bash on Windows":

1. **TMPDIR fallback chain** — replace hardcoded `/tmp` with `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}`.
2. **`os.pathsep` for PATH joining** — Python `install.py` used a `:` literal; broken on Windows where the path separator is `;`.
3. **Podman-first universal preference** — install.py and the launcher detect podman before docker, so users on rootless podman setups don't get Docker errors.
4. **`bash -n` syntax-check discipline** — every commit touching `.claude/hooks/` runs `bash -n` over each script in CI.
5. **`shutil.which` single-detect** — lean-ctx detection in install.py uses `shutil.which("lean-ctx")` once, caches result, instead of polling shell `command -v` repeatedly.

## TMPDIR fallback chain

Before:
```bash
SCRATCH_DIR=/tmp/vct-something
```

After:
```bash
SCRATCH_DIR="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vct-something"
```

Why all three:

- **`TMPDIR`**: macOS sets this to a per-user dir like `/var/folders/xx/.../T/`. Hardcoding `/tmp` writes outside the user's tmp namespace and may fail on sandboxed setups.
- **`XDG_RUNTIME_DIR`**: Linux desktop sessions set this to `/run/user/1000/` for ephemeral per-user sockets/locks. Preferred over `/tmp` because it's auto-cleaned at session end.
- **`/tmp`**: final fallback; works everywhere.

Applied to every hook that needs scratch space.

## os.pathsep for PATH joining

`install.py` had:
```python
new_path = ".venv/bin" + ":" + os.environ["PATH"]
```

Wrong on Windows. Replaced with:
```python
new_path = ".venv/bin" + os.pathsep + os.environ["PATH"]
```

`os.pathsep` is `:` on POSIX, `;` on Windows. Trivial fix; very-not-trivial debugging if you don't know to look for it.

## Podman-first universal preference

`detect_runtime()` in install.py checks `podman` before `docker`:

```python
for runtime in ("podman", "docker"):
    if shutil.which(runtime):
        return runtime
```

Why podman first:

- **Rootless** by default on Linux; no daemon, no `sudo` required.
- **macOS** users running podman-machine want podman, not Docker Desktop (which competes for resources).
- **CI** containers often have podman pre-installed and not Docker.

Users with Docker explicitly preferred can override via `VCT_RUNTIME=docker`. Detection result is cached at module load and surfaced as `runtime` in the install summary.

The launcher's runtime detection (`launcher/src-tauri/src/services/runtime.rs`) follows the same order, including macOS Podman Machine handling (`podman machine list` to detect a started VM).

## bash -n syntax-check discipline

The source-of-truth for hook scripts is `templates/hooks/*.sh` (rendered into `.claude/hooks/` at install time). CI runs `bash -n` against the templates:

```bash
for f in templates/hooks/*.sh; do
  bash -n "$f" || exit 1
done
```

Catches missing `fi` / `esac` / `done` before the hook reaches a user. A typo'd hook fails closed at commit time, not when a user's first session goes sideways.

## shutil.which single-detect

`install.py` Step 2b detects whether `lean-ctx` is installed:

```python
LEAN_CTX = shutil.which("lean-ctx")  # cached once
if LEAN_CTX:
    settings["env"]["BASH_ENV"] = ".claude/scripts/leanctx-bash-env.sh"
else:
    print("hint: install lean-ctx for shell-output compression in hooks")
```

Single `shutil.which` call, cached. Beats spawning `bash -c "command -v lean-ctx"` repeatedly. Non-interactive by design — no prompt; install.py is headless-friendly.

## Bash 3.2 compatibility for hook .sh files (Apple macOS)

Apple ships **bash 3.2** as the system default (`/bin/bash`) for GPLv3-licensing reasons and never updates it. Bash 4+ features must NOT appear in `.sh` hooks if we want them to run on a stock macOS:

| Bash 4+ feature | Bash 3.2 alternative |
|---|---|
| `declare -A name` (associative array) | A file-backed set queried via `grep -Fxq -- "$key" "$setfile"` + `echo "$key" >> "$setfile"` for insert. Slower per-lookup (O(N) per query, no index) but the typical hook batch is <20 lookups against <2000 entries — sub-millisecond total. |
| `mapfile -t arr < file` | `arr=(); while IFS= read -r l; do arr+=("$l"); done < file` |
| `${var^^}` / `${var,,}` (case conversion) | `tr '[:lower:]' '[:upper:]'` / `tr '[:upper:]' '[:lower:]'` |
| `${arr[@]:start:len}` works on assoc arrays | Use indexed arrays only |
| `coproc` | Named pipes + manual fd plumbing |
| `[[ string =~ regex ]]` capture groups | Works on bash 3.2+ (this one's fine, just don't use bash-4-only escape classes) |

**Why this comes up in v0.2.12**: PR-38 ported the working `.ps1` cache + `_filter_seen` block-atomic dedup to `.sh`. The first draft used `declare -A seen_titles` for in-memory dedup lookups — would have hard-failed on Apple bash with "declare: -A: invalid option". Fixed in PR-38 follow-up `c2093f2` by collapsing the in-memory check into a `grep -Fxq` against the SEEN_NODES_FILE (which is already the durable source-of-truth for cross-invocation dedup). Net: one mechanism instead of two, +macOS compat, sub-millisecond perf penalty.

**Detection**: there's no CI gate for "uses bash 4+ syntax" today. Pre-commit human discipline: when writing a `.sh` hook, mentally substitute `/bin/bash` (macOS) for `/usr/bin/env bash` (Linux GNU 5+) and verify the script still parses. `bash -n` against Apple bash specifically would catch this, but most contributors don't have Apple bash on their dev machines. A future CI matrix job on macOS-latest running `bash --version && bash -n` against every hook would close the gap.

**Prior precedent**: commit `cb3df13` fixed `first-install.{sh,command}` for the same reason (empty-array expansion under `set -u`).

**Decision rationale (2026-05-16)**: collapse-to-file approach was chosen over "use bash 4 + document the requirement" because:
1. macOS is explicitly Tier-2 in KNOWN_ISSUES.md — we shouldn't add NEW bash-4 dependencies without an explicit reason.
2. The SEEN_NODES_FILE was already on disk; the in-memory hash was a redundant cache layer.
3. `grep -Fxq` is fixed-string (no regex compile per call), and the lookup count is bounded by the cache replay batch size (~10-20 blocks typically).

## Why it matters

**Reach**: a hook that silently fails on `/tmp` writes on macOS sandboxed apps is a non-trivial support burden. Same code, three OSs, instead of "Linux script + macOS port + Windows port" with the drift that implies.

**Defensive baseline**: TMPDIR fallback, `os.pathsep`, podman-first, `bash -n`, bash-3.2-compat for assoc-array-shaped algorithms — all small fixes; collectively they turn "we tested on Ubuntu" into "we know it runs on the three big OSs."

## Files

- `templates/hooks/*.sh` — canonical hook source (TMPDIR pattern + VCT_DISABLE_HOOKS guard + env-scrub + bash-3.2-compat). Rendered into `.claude/hooks/*.sh` at install time — do not edit the rendered copies.
- `install.py` — os.pathsep, runtime detection, lean-ctx detection
- `launcher/src-tauri/src/services/runtime.rs` — launcher's runtime-detection mirror
- `tests/test_install_python_version.py`, `tests/test_install_shared_containers.py` — CI matrix on Ubuntu + macOS + Windows

## Related discipline

- [[Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch]] — escape hatch in every hook
- [[uses::Podman]]
- [[Safe-Install — Content-Based Service Detection]]
