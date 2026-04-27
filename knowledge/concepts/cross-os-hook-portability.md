---
title: Cross-OS Hook Portability
type: concept
tags: [hooks, cross-os, portability, podman, bash, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
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

CI runs:
```bash
for f in .claude/hooks/*.sh; do
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

## Why it matters

**Reach**: a hook that silently fails on `/tmp` writes on macOS sandboxed apps is a non-trivial support burden. Same code, three OSs, instead of "Linux script + macOS port + Windows port" with the drift that implies.

**Defensive baseline**: TMPDIR fallback, `os.pathsep`, podman-first, `bash -n` are all small fixes; collectively they turn "we tested on Ubuntu" into "we know it runs on the three big OSs."

## Files

- `.claude/hooks/*.sh` — TMPDIR pattern + VCT_DISABLE_HOOKS guard + env-scrub
- `install.py` — os.pathsep, runtime detection, lean-ctx detection
- `launcher/src-tauri/src/services/runtime.rs` — launcher's runtime-detection mirror
- `tests/test_install_python_version.py`, `tests/test_install_shared_containers.py` — CI matrix on Ubuntu + macOS + Windows

## Related discipline

- [[Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch]] — escape hatch in every hook
- [[uses::Podman]]
- [[Safe-Install — Content-Based Service Detection]]
