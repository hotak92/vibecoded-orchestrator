"""v0.2.46 post-adversarial — venv-resolver drift gate.

Every hook under `templates/hooks/` that needs a Python interpreter capable
of importing VCO's own packages (`weaviate`, `weaviate_mcp`, `vco_lib`, …)
MUST source the shared resolver at `templates/hooks/_lib/resolve-vco-venv.{sh,ps1}`.
The shared resolver enforces the canonical precedence (`VCT_VENV` →
`$VCT_INSTALL_ROOT/.venv` → `$VCT_INSTALL_ROOT/claude_mcp_servers/.venv` →
clone-relative gated by `install.py + first-install.sh` discriminator) and
NEVER falls back to `$PROJECT_ROOT/.venv` (the user's project venv, which
won't have weaviate-client + vco_lib).

Before this gate landed, 5 bash hooks + 4 PowerShell hooks each had their
own inline resolver, and several fell back to `$PROJECT_ROOT/.venv` when
`$VCT_INSTALL_ROOT` was unset — silently activating the user's venv and
crashing with confusing `ImportError: No module named 'weaviate'`
messages. The shared helper closes that drift; this test prevents
re-drift on future edits.

The gate works in two directions:

  1. **Forbid the bad fallback pattern**. Any hook source containing
     ``VCT_INSTALL_ROOT:-$PROJECT_ROOT`` (bash) or
     ``VCT_INSTALL_ROOT.*ProjectRoot`` (PowerShell ternary) fails the
     test with a pointer to the shared helper.

  2. **Forbid the silent ``$SCRIPT_DIR/../..`` clone-root assumption**
     in any contexts where that path then feeds straight into a
     ``.venv`` lookup (without the `install.py + first-install.sh`
     discriminator check). The bash hook
     ``code-graph-incremental.sh`` previously did this via
     ``DEFAULT_REPO_ROOT="$SCRIPT_DIR/../.."``.

When this test fails after an intentional new venv-resolver pattern,
either (a) extend the shared helper to cover the new case, or (b)
allowlist the specific file + line in this test with a comment
explaining why it's legitimate.
"""
from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PROJECT_ROOT / "templates" / "hooks"
SHARED_HELPER_SH = HOOKS_DIR / "_lib" / "resolve-vco-venv.sh"
SHARED_HELPER_PS1 = HOOKS_DIR / "_lib" / "resolve-vco-venv.ps1"


# Files exempted from the gate. Empty today — every drift was eliminated
# in the post-adversarial sweep. Future entries must include a comment
# explaining why the file is exempt (e.g. "documents the OLD pattern in
# a fixed comment block — not real code").
_ALLOWLIST: set[str] = set()


# Bash drift signatures.
_BASH_DRIFT_PATTERNS = [
    # The canonical bad fallback: substitute $PROJECT_ROOT when
    # $VCT_INSTALL_ROOT is unset. NEVER correct for VCO hooks.
    re.compile(r"\$\{VCT_INSTALL_ROOT:-\$PROJECT_ROOT\}"),
    # The disguised version via an intermediate variable that derives
    # from $SCRIPT_DIR/../.. and feeds straight into a .venv path
    # without VCO-clone discriminator check.
    re.compile(r"DEFAULT_REPO_ROOT/(claude_mcp_servers/)?\.venv"),
]

# PowerShell drift signatures.
_PS1_DRIFT_PATTERNS = [
    # `if ($env:VCT_INSTALL_ROOT) { ... } else { $ProjectRoot }` —
    # the PS1 ternary form of the bash drift. Match the joined
    # one-liner form ($env:VCT_INSTALL_ROOT ... else ... ProjectRoot).
    re.compile(r"\$env:VCT_INSTALL_ROOT.*else.*\$ProjectRoot",
               flags=re.IGNORECASE),
]


def _scan_for_drift(path: Path, patterns: list[re.Pattern]) -> list[tuple[int, str, str]]:
    """Return (line_no, pattern_str, line_text) tuples for any match.

    Skips lines that are pure comments — we want to allow the explanatory
    comments that name the OLD behavior without re-triggering the gate.
    Pure-comment heuristic: line's first non-whitespace char is `#`
    (bash) or `#` (PS1 doesn't use #, uses <#...#> blocks or `# `).
    """
    out: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # comment-only line
        for pat in patterns:
            if pat.search(line):
                out.append((lineno, pat.pattern, line.strip()))
                break
    return out


def test_shared_helper_files_exist():
    """The shared resolver files must exist — every hook depends on them."""
    assert SHARED_HELPER_SH.is_file(), (
        f"Missing {SHARED_HELPER_SH.relative_to(PROJECT_ROOT)} — hooks "
        f"that source it will fail at runtime."
    )
    assert SHARED_HELPER_PS1.is_file(), (
        f"Missing {SHARED_HELPER_PS1.relative_to(PROJECT_ROOT)} — "
        f"PowerShell hooks that dot-source it will fail at runtime."
    )


def test_no_bash_hook_falls_back_to_project_root_venv():
    """No `templates/hooks/*.sh` falls back to $PROJECT_ROOT/.venv.

    The drift pattern is `${VCT_INSTALL_ROOT:-$PROJECT_ROOT}` (or the
    intermediate-variable disguise via `DEFAULT_REPO_ROOT=...`). Either
    activates the user's project venv when `$VCT_INSTALL_ROOT` is
    unset — wrong for every VCO hook.
    """
    failures: list[str] = []
    for hook in HOOKS_DIR.glob("*.sh"):
        if hook.name in _ALLOWLIST:
            continue
        for lineno, pat, line in _scan_for_drift(hook, _BASH_DRIFT_PATTERNS):
            failures.append(
                f"  {hook.relative_to(PROJECT_ROOT)}:{lineno}\n"
                f"    matched: {pat}\n"
                f"    line:    {line}"
            )
    if failures:
        raise AssertionError(
            "Bash venv-resolver drift detected. Source the shared helper "
            "at `templates/hooks/_lib/resolve-vco-venv.sh` instead of "
            "rolling an inline resolver:\n\n"
            + "\n\n".join(failures)
            + "\n\nIf this hook legitimately needs a different resolver, "
            "either extend the shared helper or add the file to "
            "_ALLOWLIST in this test with a comment explaining why."
        )


def test_no_ps1_hook_falls_back_to_project_root_venv():
    """No `templates/hooks/*.ps1` does the PS1 form of the same drift."""
    failures: list[str] = []
    for hook in HOOKS_DIR.glob("*.ps1"):
        if hook.name in _ALLOWLIST:
            continue
        for lineno, pat, line in _scan_for_drift(hook, _PS1_DRIFT_PATTERNS):
            failures.append(
                f"  {hook.relative_to(PROJECT_ROOT)}:{lineno}\n"
                f"    matched: {pat}\n"
                f"    line:    {line}"
            )
    if failures:
        raise AssertionError(
            "PowerShell venv-resolver drift detected. Dot-source the "
            "shared helper at `templates/hooks/_lib/resolve-vco-venv.ps1` "
            "and call `Resolve-VcoVenvPython` instead of rolling an "
            "inline resolver:\n\n"
            + "\n\n".join(failures)
            + "\n\nIf this hook legitimately needs a different resolver, "
            "either extend the shared helper or add the file to "
            "_ALLOWLIST in this test with a comment explaining why."
        )


def test_every_venv_using_bash_hook_sources_the_helper():
    """If a bash hook resolves a Python interpreter for VCO scripts,
    it should source the shared helper (or use `find-python.sh` for
    the "any python at all" use case).

    Heuristic: a hook is considered "venv-using" if it references a
    `.venv` directory in its body. Such hooks MUST source either
    `resolve-vco-venv.sh` (the canonical helper) OR `find-python.sh`
    (the last-resort fallback for hooks that don't need VCO's deps).
    """
    failures: list[str] = []
    for hook in HOOKS_DIR.glob("*.sh"):
        if hook.name in _ALLOWLIST:
            continue
        try:
            text = hook.read_text(encoding="utf-8")
        except OSError:
            continue
        # Skip lines that are pure comments to avoid matching the
        # explanatory comments we added that mention `.venv`.
        non_comment_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        non_comment_body = "\n".join(non_comment_lines)
        if ".venv" not in non_comment_body:
            continue
        if "resolve-vco-venv.sh" in text or "find-python.sh" in text:
            continue
        failures.append(
            f"  {hook.relative_to(PROJECT_ROOT)}: references `.venv` in "
            f"code but doesn't source resolve-vco-venv.sh or "
            f"find-python.sh."
        )
    if failures:
        raise AssertionError(
            "Bash hooks referencing `.venv` must source the shared "
            "helper:\n\n" + "\n".join(failures)
        )


def test_every_venv_using_ps1_hook_sources_the_helper():
    """PowerShell mirror of the bash hook gate above."""
    failures: list[str] = []
    for hook in HOOKS_DIR.glob("*.ps1"):
        if hook.name in _ALLOWLIST:
            continue
        try:
            text = hook.read_text(encoding="utf-8")
        except OSError:
            continue
        non_comment_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        non_comment_body = "\n".join(non_comment_lines)
        if ".venv" not in non_comment_body:
            continue
        if "resolve-vco-venv.ps1" in text or "find-python.ps1" in text:
            continue
        failures.append(
            f"  {hook.relative_to(PROJECT_ROOT)}: references `.venv` in "
            f"code but doesn't dot-source resolve-vco-venv.ps1 or "
            f"find-python.ps1."
        )
    if failures:
        raise AssertionError(
            "PowerShell hooks referencing `.venv` must dot-source the "
            "shared helper:\n\n" + "\n".join(failures)
        )


def test_shared_helper_refuses_project_root_fallback():
    """The shared helper itself must not contain the drift pattern.

    Defense-in-depth: a future "make the helper more permissive" edit
    that adds back `$PROJECT_ROOT/.venv` would re-introduce the original
    bug everywhere. Pin the helper too.
    """
    text = SHARED_HELPER_SH.read_text(encoding="utf-8")
    non_comment_lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    non_comment_body = "\n".join(non_comment_lines)
    assert "$PROJECT_ROOT" not in non_comment_body, (
        "resolve-vco-venv.sh must not reference $PROJECT_ROOT — that "
        "would re-introduce the user-venv fallback bug everywhere."
    )

    text_ps1 = SHARED_HELPER_PS1.read_text(encoding="utf-8")
    non_comment_lines_ps1 = [
        line for line in text_ps1.splitlines()
        if not line.lstrip().startswith("#")
    ]
    non_comment_body_ps1 = "\n".join(non_comment_lines_ps1)
    assert "ProjectRoot" not in non_comment_body_ps1, (
        "resolve-vco-venv.ps1 must not reference $ProjectRoot — that "
        "would re-introduce the user-venv fallback bug everywhere."
    )
