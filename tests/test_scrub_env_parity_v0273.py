# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 HK-2 — secret-env scrub-list parity gate.

The list of sensitive env vars every hook scrubs before spawning a
subprocess was copy-pasted verbatim across ~40 .sh + ~40 .ps1 hooks
(risk R10): adding a new secret meant editing 80 files and a single miss
is a credential-leak surface. HK-2 introduces the canonical list at
``_lib/scrub-env.{sh,ps1}``; this gate asserts:

  1. the .sh and .ps1 canonical lists match each other;
  2. every hook's inline scrub covers EXACTLY the canonical set (no hook
     drifts a key out — or in — silently).

So adding a key to the lib immediately points at every hook whose inline
list needs the same key.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"
LIB_SH = HOOKS_DIR / "_lib" / "scrub-env.sh"
LIB_PS1 = HOOKS_DIR / "_lib" / "scrub-env.ps1"


def _canonical_from_sh() -> list[str]:
    text = LIB_SH.read_text(encoding="utf-8")
    m = re.search(r'VCT_SCRUB_SECRET_KEYS="([^"]+)"', text)
    assert m, "canonical VCT_SCRUB_SECRET_KEYS not found in scrub-env.sh"
    return m.group(1).split()


def _canonical_from_ps1() -> list[str]:
    text = LIB_PS1.read_text(encoding="utf-8")
    # Grab the @( ... ) array body.
    m = re.search(r"\$VctScrubSecretKeys\s*=\s*@\((.*?)\)", text, re.DOTALL)
    assert m, "canonical $VctScrubSecretKeys not found in scrub-env.ps1"
    return re.findall(r"'([A-Z0-9_]+)'", m.group(1))


def test_lib_sh_and_ps1_lists_match():
    assert set(_canonical_from_sh()) == set(_canonical_from_ps1()), (
        "scrub-env.sh and scrub-env.ps1 canonical secret lists diverge"
    )


def _sh_hooks() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.sh"))


def _ps1_hooks() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.ps1"))


def _inline_sh_scrub_keys(text: str) -> set[str] | None:
    """Extract the keys from a hook's inline `unset ... GITHUB_TOKEN ...`."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("unset ") and "GITHUB_TOKEN" in s:
            body = s[len("unset "):]
            # Drop trailing redirection / comments.
            body = re.split(r"\s+2>/dev/null|\s+#", body)[0]
            return set(tok for tok in body.split() if re.fullmatch(r"[A-Z0-9_]+", tok))
    return None


def _inline_ps1_scrub_keys(text: str) -> set[str] | None:
    """Extract keys from the .ps1 scrub declaration.

    Handles both the single-line `foreach ($v in 'A','B',...)` form and the
    multi-line `@( 'A', 'B', ... )` / `= @(...)` array form by scanning a
    window from the line that first mentions GITHUB_TOKEN until the closing
    `)` or `{`.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if "GITHUB_TOKEN" in ln), None)
    if start is None:
        return None
    # Collect from the GITHUB_TOKEN line forward until the array/foreach
    # closes (a line containing `)`), so a multi-line array is captured
    # whole. The keys always start on the GITHUB_TOKEN line (SUPABASE_KEY,
    # SUPABASE_URL, GITHUB_TOKEN are the first three in canonical order).
    window: list[str] = []
    for ln in lines[start:]:
        window.append(ln)
        if ")" in ln:
            break
    blob = "\n".join(window)
    # Only quoted UPPER_SNAKE tokens are keys; the `Env:$v` / Remove-Item
    # lines carry no quoted uppercase tokens so they don't pollute the set.
    return set(re.findall(r"['\"]([A-Z][A-Z0-9_]+)['\"]", blob))


@pytest.mark.parametrize("hook", _sh_hooks(), ids=lambda p: p.name)
def test_sh_hook_scrub_matches_canonical(hook: Path):
    canonical = set(_canonical_from_sh())
    keys = _inline_sh_scrub_keys(hook.read_text(encoding="utf-8"))
    assert keys is not None, f"{hook.name}: no inline scrub line found"
    missing = canonical - keys
    extra = keys - canonical
    assert not missing and not extra, (
        f"{hook.name}: inline scrub drifts from canonical "
        f"(missing={sorted(missing)}, extra={sorted(extra)}). "
        f"Update it to match _lib/scrub-env.sh."
    )


@pytest.mark.parametrize("hook", _ps1_hooks(), ids=lambda p: p.name)
def test_ps1_hook_scrub_matches_canonical(hook: Path):
    canonical = set(_canonical_from_ps1())
    keys = _inline_ps1_scrub_keys(hook.read_text(encoding="utf-8"))
    assert keys is not None, f"{hook.name}: no inline scrub line found"
    missing = canonical - keys
    extra = keys - canonical
    assert not missing and not extra, (
        f"{hook.name}: inline scrub drifts from canonical "
        f"(missing={sorted(missing)}, extra={sorted(extra)}). "
        f"Update it to match _lib/scrub-env.ps1."
    )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
