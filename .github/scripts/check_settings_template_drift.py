#!/usr/bin/env python3
"""Settings.json template drift gate.

Enforces logical equivalence between `.claude/settings.json` (the active
orchestrator config) and the OS-specific install templates
`templates/settings.json.linux.template` and
`templates/settings.json.windows.template`.

Why this exists
---------------
`.claude/settings.json` is what the orchestrator's own Claude Code session
reads at runtime. The templates are what `vco_lib.project_init.install_bundle`
drops into newly-registered projects. If silent drift accumulates, the
orchestrator works (it uses .claude/) but every newly-registered project
gets stale or broken hook wiring. This bit us at least once already
(install-flow audit 2026-05-08: 3 missing kg-update-nudge wirings in both
templates → fresh installs shipped a dead nudge hook).

What we check
-------------
For each (event, matcher_key) tuple:
- Every hook *command* present on one side should be present on the other.
- Hook *order* within a matcher block is NOT enforced (it doesn't affect
  Claude Code's hook firing semantics).
- Hook flags (`background`, `timeout`, `if`) are NOT enforced — those can
  legitimately differ by OS or by orchestrator-only tuning. We only enforce
  presence/absence.

Hook command normalisation:
- The OS-specific prefix is stripped: `bash .claude/hooks/X.sh` (Linux)
  and `powershell -NoProfile -ExecutionPolicy Bypass -File .claude\\hooks\\X.ps1`
  (Windows) both normalise to `X` (the hook basename, no extension).
- The optional `[ -n "$VCT_DISABLE_HOOKS" ] || ` prefix is stripped.

What's allowed to differ
------------------------
Top-level `env` block. The orchestrator clone has lean-ctx wiring
(`BASH_ENV: ${CLAUDE_PROJECT_DIR}/.claude/scripts/leanctx-bash-env.sh`)
that is orchestrator-specific and intentionally absent from templates.
This file's `EXPECTED_ENV_DIVERGENCE` set documents allowed differences.

Inline non-hook commands like `python -m py_compile "$CLAUDE_TOOL_ARG_PATH"`
are NOT in scope of this gate — they don't reference a hook script.

Usage:
    python3 .github/scripts/check_settings_template_drift.py

Exit codes:
    0 - pass
    1 - drift detected
    2 - script error
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ACTIVE_PATH = REPO_ROOT / ".claude" / "settings.json"
LINUX_TEMPLATE_PATH = REPO_ROOT / "templates" / "settings.json.linux.template"
WINDOWS_TEMPLATE_PATH = REPO_ROOT / "templates" / "settings.json.windows.template"


# Hook commands allowed to be active-only (orchestrator-specific tooling not
# shipped to managed projects). Format: hook basename without extension.
EXPECTED_ACTIVE_ONLY: set[str] = set()

# Hook commands allowed to be template-only (documented future wiring, etc).
EXPECTED_TEMPLATE_ONLY: set[str] = set()

# Top-level keys in `env` that are allowed to differ between active and
# templates. `BASH_ENV` is orchestrator-specific (lean-ctx integration).
EXPECTED_ENV_DIVERGENCE: set[str] = {"BASH_ENV"}


def normalise_hook_command(cmd: str) -> str | None:
    """Extract the hook basename from a shell/powershell command.

    Returns None if the command doesn't reference a hook script (e.g. inline
    python invocations, jq pipes). Inline commands are NOT compared by this
    gate; they're either present in both files or in neither, but we don't
    care about them here (other gates and tests cover them).

    Strips:
    - `[ -n "$VCT_DISABLE_HOOKS" ] || ` Linux guard prefix
    - `bash .claude/hooks/` or `bash hooks/` prefix
    - `powershell -NoProfile -ExecutionPolicy Bypass -File .claude\\hooks\\` prefix
    - file extension
    - any trailing arguments
    """
    if not cmd:
        return None

    # Drop VCT_DISABLE_HOOKS guard wrapper.
    cmd = re.sub(r'^\s*\[\s*-n\s+"\$VCT_DISABLE_HOOKS"\s*\]\s*\|\|\s+', "", cmd)

    # Bash invocation: `bash .claude/hooks/X.sh ...`
    m = re.match(r"^bash\s+(?:\.claude/)?hooks/([A-Za-z0-9_\-]+)\.sh\b", cmd)
    if m:
        return m.group(1)

    # PowerShell invocation: `powershell ... -File .claude\\hooks\\X.ps1`
    m = re.match(
        r"^powershell\s+.*-File\s+(?:\.claude\\)?hooks\\([A-Za-z0-9_\-]+)\.ps1\b",
        cmd,
    )
    if m:
        return m.group(1)

    # python invocation referencing a hook/script directory (Linux + Windows
    # variants: `python .claude/scripts/X.py` and
    # `powershell ... try { python .claude/scripts/X.py ... } catch { }`).
    m = re.search(r"python3?\s+\.claude[/\\](?:hooks|scripts)[/\\]([A-Za-z0-9_\-]+)\.py\b", cmd)
    if m:
        return m.group(1)

    # Inline command not referencing a hook script — out of scope.
    return None


def collect_hook_commands(settings: dict) -> dict[tuple[str, str], set[str]]:
    """Return {(event, matcher): {normalised_hook_command, ...}, ...}.

    Matcher key is empty string for blocks without a matcher (e.g. some
    UserPromptSubmit blocks).
    """
    out: dict[tuple[str, str], set[str]] = {}
    for event, items in settings.get("hooks", {}).items():
        if not isinstance(items, list):
            continue
        for h in items:
            matcher = h.get("matcher", "")
            key = (event, matcher)
            slot = out.setdefault(key, set())
            for c in h.get("hooks", []):
                norm = normalise_hook_command(c.get("command", ""))
                if norm:
                    slot.add(norm)
    return out


def diff_one(active: dict, template: dict, template_label: str) -> list[str]:
    """Compare a single template to active. Returns list of error strings."""
    errors: list[str] = []

    active_cmds = collect_hook_commands(active)
    template_cmds = collect_hook_commands(template)

    all_keys = set(active_cmds) | set(template_cmds)
    for key in sorted(all_keys):
        a = active_cmds.get(key, set())
        t = template_cmds.get(key, set())

        in_active_only = (a - t) - EXPECTED_ACTIVE_ONLY
        in_template_only = (t - a) - EXPECTED_TEMPLATE_ONLY

        for cmd in sorted(in_active_only):
            errors.append(
                f"  {template_label} drift: hook {cmd!r} wired in [{key[0]}] "
                f"matcher={key[1]!r} of .claude/settings.json but missing in template"
            )
        for cmd in sorted(in_template_only):
            errors.append(
                f"  {template_label} drift: hook {cmd!r} wired in [{key[0]}] "
                f"matcher={key[1]!r} of {template_label} but missing in .claude/settings.json"
            )

    # Top-level env block: keys not in EXPECTED_ENV_DIVERGENCE must match.
    a_env = set(active.get("env", {}))
    t_env = set(template.get("env", {}))
    for k in (a_env - t_env) - EXPECTED_ENV_DIVERGENCE:
        errors.append(
            f"  {template_label} drift: top-level env key {k!r} present in .claude/settings.json "
            f"but missing in template (add to EXPECTED_ENV_DIVERGENCE if intentional)"
        )
    for k in (t_env - a_env) - EXPECTED_ENV_DIVERGENCE:
        errors.append(
            f"  {template_label} drift: top-level env key {k!r} present in template "
            f"but missing in .claude/settings.json (add to EXPECTED_ENV_DIVERGENCE if intentional)"
        )

    return errors


def main() -> int:
    try:
        active = json.loads(ACTIVE_PATH.read_text())
        linux_tpl = json.loads(LINUX_TEMPLATE_PATH.read_text())
        windows_tpl = json.loads(WINDOWS_TEMPLATE_PATH.read_text())
    except FileNotFoundError as e:
        print(f"::error::settings.json file missing: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"::error::settings.json parse error: {e}", file=sys.stderr)
        return 2

    errors = []
    errors.extend(diff_one(active, linux_tpl, "templates/settings.json.linux.template"))
    errors.extend(diff_one(active, windows_tpl, "templates/settings.json.windows.template"))

    if errors:
        print(
            "Settings.json template drift detected — every hook command in "
            ".claude/settings.json should also appear in BOTH OS templates "
            "(and vice versa). Adding to EXPECTED_ACTIVE_ONLY / "
            "EXPECTED_TEMPLATE_ONLY in this script requires CR review.",
            file=sys.stderr,
        )
        for e in errors:
            print(f"::error::{e.strip()}", file=sys.stderr)
            print(e, file=sys.stderr)
        return 1

    print("OK: .claude/settings.json and templates/settings.json.{linux,windows}.template are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
