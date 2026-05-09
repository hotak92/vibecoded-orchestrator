# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests that shell hooks propagate VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST
env vars to their downstream subprocesses.

Centralization contract (PR #171 / 0.1.7)
-----------------------------------------
Hooks under ``.claude/hooks/`` and ``templates/hooks/`` are pure
delegators with respect to KG / codegraph access. The launcher writes
``VCT_KG_ACCESS_LIST=Foo,Bar`` and ``VCT_CODE_GRAPH_ACCESS_LIST=Foo,Bar``
into the per-project environment, and the access-aware downstreams
(``rl_kg_search.py``, ``kg-search`` CLI, the weaviate MCP server, etc.)
read those env vars to fan out searches across peer collections.

For this to work, hooks **must not**:

1. Strip or unset ``VCT_KG_ACCESS_LIST`` / ``VCT_CODE_GRAPH_ACCESS_LIST``
   before spawning subprocesses.
2. Use ``env -i`` / ``env -u`` to wipe the inherited environment.
3. Use ``Start-Process -UseNewEnvironment`` (the PowerShell equivalent of
   ``env -i``) without explicitly forwarding the access-list vars.

This test pins both invariants:

* **Static check** — every shell hook is grep'd for the forbidden
  patterns, including a check that the secret-scrub list (which IS
  expected to call ``unset``) does not accidentally include the access
  matrix vars.
* **Dynamic check** — a representative hook (``pre-tool-use.sh`` /
  ``pre-tool-use.ps1``, which is the on-by-default path that calls
  ``kg-search``) is invoked with ``VCT_KG_ACCESS_LIST=Beta,Gamma`` set
  and a stubbed ``kg-search`` script; the stub records its received env
  and the test asserts the access-list var arrived intact.

Static check is the load-bearing one — it covers all 16 .sh + .ps1
hooks, and it's deterministic. The dynamic check is a belt-and-braces
sanity probe on the most-fired hook in normal operation.

Cross-OS
--------
We run the bash dynamic check on the .sh side. The .ps1 side is
covered by static analysis only on this CI host (no powershell
runner). The ``check_hook_parity.py`` gate ensures both shells are
modified together when either changes, so a hook that breaks the
contract on one side and not the other can't be merged.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hook directories. Both must be checked — the orchestrator runs out of
# .claude/hooks, but newly-registered user projects get templates/hooks
# dropped in by the install bundle.
HOOKS_DIRS = [
    REPO_ROOT / ".claude" / "hooks",
    REPO_ROOT / "templates" / "hooks",
]

# Env-var names the hooks must NOT strip from the inherited environment.
ACCESS_VARS = ("VCT_KG_ACCESS_LIST", "VCT_CODE_GRAPH_ACCESS_LIST")

# Subset of hooks whose bash side is fast to invoke and reaches the KG-search
# delegation path within ~1.5s wall-clock with VCT_DISABLE_HOOKS unset. We
# only run the dynamic test against this subset to keep test latency
# predictable — the static test already covers all 32 files.
DYNAMIC_TEST_HOOKS = [
    # pre-tool-use.sh fires on every tool call; the KG-suggestion branch
    # spawns kg-search when concept keywords show up in the user message.
    "pre-tool-use.sh",
]


def _hook_files(suffix: str) -> list[Path]:
    out: list[Path] = []
    for d in HOOKS_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"*{suffix}")):
            out.append(p)
    return out


# --- Static checks -----------------------------------------------------


def _strip_inline_comment(line: str) -> str:
    """Drop trailing `# ...` comment from a bash line.

    Crude but sufficient — splits on the first ``#`` that's preceded by
    whitespace (so URLs like `http://x#y` are not split). Does not handle
    ``#`` inside single/double quotes, but in practice no hook puts
    `unset VCT_KG_ACCESS_LIST` inside a quoted string.
    """
    # Common case: full-line comment.
    if line.lstrip().startswith("#"):
        return ""
    # Inline comment: split on first " #" that's not inside quotes (best-effort).
    out_chars: list[str] = []
    in_single = False
    in_double = False
    prev_was_space = True  # leading position counts as boundary
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and prev_was_space:
            break
        out_chars.append(ch)
        prev_was_space = ch in (" ", "\t")
    return "".join(out_chars)


@pytest.mark.parametrize(
    "hook_path", _hook_files(".sh"), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_sh_hook_does_not_strip_access_vars(hook_path: Path) -> None:
    """No bash hook may unset VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST.

    Static check. Every code line containing ``unset`` or ``env -i`` /
    ``env -u`` is scanned; if it includes either access-matrix var, the
    test fails. Comments (full-line and inline) are stripped first so
    documentation that *names* these env vars (e.g. the
    VCO-CENTRALIZED-KG marker block) doesn't trip the check.
    """
    for i, raw in enumerate(hook_path.read_text().splitlines(), start=1):
        line = _strip_inline_comment(raw)
        if not line.strip():
            continue

        for var in ACCESS_VARS:
            # `unset VCT_KG_ACCESS_LIST [...]` or `unset X Y VCT_KG_ACCESS_LIST`.
            if re.search(rf"\bunset\b[^\n]*\b{re.escape(var)}\b", line):
                pytest.fail(
                    f"{hook_path}:{i}: contains `unset ... {var} ...` — "
                    f"this would break the multi-source KG access matrix.\n"
                    f"  Line: {line.rstrip()}"
                )
            # `env -u VCT_KG_ACCESS_LIST`.
            if re.search(rf"\benv\s+-u\s+{re.escape(var)}\b", line):
                pytest.fail(
                    f"{hook_path}:{i}: contains `env -u {var}` — this "
                    f"would break the multi-source KG access matrix.\n"
                    f"  Line: {line.rstrip()}"
                )

        # `env -i ...` wipes the entire env. Allowed only if the same
        # line re-forwards the access vars (e.g.
        # `env -i VCT_KG_ACCESS_LIST="$VCT_KG_ACCESS_LIST" ...`).
        if re.search(r"\benv\s+-i\b", line):
            if any(v in line for v in ACCESS_VARS):
                continue
            pytest.fail(
                f"{hook_path}:{i}: `env -i` without forwarding "
                f"VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST. This "
                f"breaks the multi-source KG access matrix.\n"
                f"  Line: {line.rstrip()}"
            )


def _strip_ps1_inline_comment(line: str) -> str:
    """Drop trailing `# ...` comment from a PowerShell line.

    Crude but sufficient. PowerShell uses ``#`` for comments; the
    backtick is the line-continuation char, not part of comment syntax.
    Same caveats as :func:`_strip_inline_comment`.
    """
    if line.lstrip().startswith("#"):
        return ""
    out_chars: list[str] = []
    in_single = False
    in_double = False
    prev_was_space = True
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and prev_was_space:
            break
        out_chars.append(ch)
        prev_was_space = ch in (" ", "\t")
    return "".join(out_chars)


@pytest.mark.parametrize(
    "hook_path", _hook_files(".ps1"), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_ps1_hook_does_not_strip_access_vars(hook_path: Path) -> None:
    """No PowerShell hook may strip the access-matrix env vars.

    Static check. Scans for ``Remove-Item Env:VCT_KG_ACCESS_LIST`` /
    ``Remove-Item "Env:$..."`` patterns that touch the access vars, and
    for ``Start-Process -UseNewEnvironment`` (the PS equivalent of
    ``env -i``) used without explicitly setting the access vars on the
    spawned process. Comments (full-line and inline) are stripped first
    so documentation that *names* these env vars doesn't trip the check.
    """
    for i, raw in enumerate(hook_path.read_text().splitlines(), start=1):
        line = _strip_ps1_inline_comment(raw)
        if not line.strip():
            continue

        for var in ACCESS_VARS:
            # Direct removal: `Remove-Item Env:VCT_KG_ACCESS_LIST` or
            # `Remove-Item "Env:VCT_KG_ACCESS_LIST"`.
            rm_re = re.compile(
                rf'Remove-Item\s+["\']?Env:{re.escape(var)}["\']?',
                re.IGNORECASE,
            )
            if rm_re.search(line):
                pytest.fail(
                    f"{hook_path}:{i}: removes Env:{var} — this would "
                    f"break the multi-source KG access matrix.\n"
                    f"  Line: {line.rstrip()}"
                )

            # Indirect: secret-loop variable matches the access var. Most
            # hooks loop `foreach ($v in 'X','Y',...) { Remove-Item Env:$v }`.
            # If `'VCT_KG_ACCESS_LIST'` appears inside such an array, we
            # flag.
            loop_re = re.compile(
                rf"foreach\s*\(\s*\$\w+\s+in\s+[^)]*['\"]"
                + re.escape(var)
                + r"['\"]",
                re.IGNORECASE,
            )
            if loop_re.search(line):
                pytest.fail(
                    f"{hook_path}:{i}: includes '{var}' in a "
                    f"`Remove-Item Env:$v` loop — this would break the "
                    f"multi-source KG access matrix.\n"
                    f"  Line: {line.rstrip()}"
                )

        # `Start-Process -UseNewEnvironment` wipes the spawned-process env.
        # Detect it; allow only if the line ALSO forwards the access vars
        # (rare, but legal).
        if re.search(r"-UseNewEnvironment\b", line, re.IGNORECASE):
            if any(v in line for v in ACCESS_VARS):
                continue
            pytest.fail(
                f"{hook_path}:{i}: `Start-Process -UseNewEnvironment` "
                f"without forwarding VCT_KG_ACCESS_LIST / "
                f"VCT_CODE_GRAPH_ACCESS_LIST. Breaks the multi-source "
                f"KG access matrix.\n  Line: {line.rstrip()}"
            )


def test_centralization_marker_present_on_kg_touching_hooks() -> None:
    """Every KG/codegraph-touching hook carries a # VCO-CENTRALIZED-KG: marker.

    Pin the documentation contract: when someone adds a new hook that
    touches KG or codegraph paths, they must classify it (read-side
    delegator / write-side delegator / counter-only / spawns-claude /
    reference-provider) by adding the marker block. This test catches
    drift if a future hook is added without the marker.

    Marker convention: ``# VCO-CENTRALIZED-KG: <classification>``
    placed immediately after the VCT_DISABLE_HOOKS guard.
    """
    # Hooks that are confirmed in scope of the centralization audit
    # (PR #171 / 0.1.7). If new hooks touching KG land, they should be
    # added here AND carry the marker.
    expected = {
        "pre-edit-context-inject",
        "post-file-edit",
        "code-graph-incremental",
        "kg-summary-generator",
        "kg-update-nudge",
        "post-git-commit-kg-sync",
        "pre-tool-use",
        "session-start-kg-loader",
    }

    missing: list[str] = []
    for hook_dir in HOOKS_DIRS:
        if not hook_dir.is_dir():
            continue
        for name in expected:
            for ext in ("sh", "ps1"):
                path = hook_dir / f"{name}.{ext}"
                if not path.exists():
                    missing.append(f"{path}: file does not exist")
                    continue
                text = path.read_text()
                if "VCO-CENTRALIZED-KG:" not in text:
                    missing.append(f"{path}: missing # VCO-CENTRALIZED-KG: marker")

    assert not missing, (
        "The following KG-touching hooks must carry a "
        "# VCO-CENTRALIZED-KG: marker (see PR #171 / 0.1.7):\n  - "
        + "\n  - ".join(missing)
    )


# --- Dynamic check -----------------------------------------------------


@pytest.mark.parametrize("hook_name", DYNAMIC_TEST_HOOKS)
def test_sh_hook_propagates_access_vars_to_subprocess(
    hook_name: str, tmp_path: Path
) -> None:
    """Run a hook with VCT_KG_ACCESS_LIST set; assert the kg-search stub
    sees the var in its env.

    This is a sanity probe — the static test above covers all hooks; this
    one exercises the actual subprocess inheritance path on a real
    invocation. Uses a stubbed ``kg-search`` wrapper that records its env
    and exits, so we don't need a real Weaviate.
    """
    if sys.platform.startswith("win"):
        pytest.skip("dynamic .sh test runs on POSIX shells only")

    hook_path = REPO_ROOT / ".claude" / "hooks" / hook_name
    if not hook_path.exists():
        pytest.skip(f"{hook_path} not present")

    # Build a fake project layout under tmp_path that the hook will see
    # as PROJECT_ROOT (its `cd $SCRIPT_DIR/../..` resolution).
    project_root = tmp_path / "fake-project"
    (project_root / ".claude" / "hooks").mkdir(parents=True)
    (project_root / ".claude" / "scripts").mkdir(parents=True)
    (project_root / ".claude" / "logs").mkdir(parents=True)
    (project_root / ".claude" / "hooks" / "_lib").mkdir(parents=True)

    # Symlink the real hook's _lib helpers into the fake hooks dir so
    # `. _lib/stderr-cap.sh` works.
    real_lib = REPO_ROOT / ".claude" / "hooks" / "_lib"
    fake_lib = project_root / ".claude" / "hooks" / "_lib"
    for f in real_lib.iterdir():
        if f.is_file():
            (fake_lib / f.name).symlink_to(f)
    # Symlink the hook itself so its $SCRIPT_DIR/../.. resolves to project_root.
    fake_hook = project_root / ".claude" / "hooks" / hook_name
    fake_hook.symlink_to(hook_path)

    # Recorder kg-search stub: writes its env to a file and exits.
    env_dump = tmp_path / "stub_env.txt"
    stub_path = project_root / ".claude" / "scripts" / "kg-search"
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        f'env > "{env_dump}"\n'
        "echo 'knowledge/concepts/stub.md'\n"
        "echo 'knowledge/concepts/stub2.md'\n"
        "exit 0\n"
    )
    stub_path.chmod(0o755)

    # Stub kg-search via PATH override is harder than just placing the
    # file at the path the hook resolves: $PROJECT_ROOT/.claude/scripts/kg-search.
    # The hook resolves PROJECT_ROOT via $SCRIPT_DIR/../.. — done above by
    # symlinking the hook through project_root.

    # Build the JSON payload that triggers the KG-suggestion path:
    # tool_name=Edit, user_message contains a concept keyword.
    payload = (
        '{"tool_name": "Edit", '
        '"tool_input": {"file_path": "/tmp/anything.py"}, '
        '"user_message": "let us improve the authentication caching pattern", '
        '"session_id": "test-session"}'
    )

    env = {
        "VCT_KG_ACCESS_LIST": "Beta,Gamma",
        "VCT_CODE_GRAPH_ACCESS_LIST": "Beta,Gamma",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        # IMPORTANT: do NOT set VCT_DISABLE_HOOKS — we want the hook to run.
    }

    result = subprocess.run(
        ["bash", str(fake_hook)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(project_root),
    )

    # Assert the hook didn't crash (exit 2 means it blocked the tool call,
    # which only happens for SSRF / shell injection / build anchor — none
    # apply here).
    assert result.returncode == 0, (
        f"Hook returned non-zero ({result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Assert the stub recorded our access-list vars in its env.
    if not env_dump.exists():
        pytest.fail(
            f"Stub kg-search was never invoked — the hook may have skipped the "
            f"KG-suggestion branch. stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    dumped = env_dump.read_text()

    assert "VCT_KG_ACCESS_LIST=Beta,Gamma" in dumped, (
        f"VCT_KG_ACCESS_LIST not in subprocess env. Stub env was:\n{dumped}"
    )
    assert "VCT_CODE_GRAPH_ACCESS_LIST=Beta,Gamma" in dumped, (
        f"VCT_CODE_GRAPH_ACCESS_LIST not in subprocess env. Stub env was:\n{dumped}"
    )
