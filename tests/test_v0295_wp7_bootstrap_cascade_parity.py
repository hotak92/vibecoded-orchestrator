# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.95 WP-7 — the two bootstrap candidate cascades, locked for real.

There are **two** distinct duplicated cascades in the bootstrap surface, and
they are routinely conflated because both are "a list of paths to probe":

1. **The Python-interpreter cascade** — the ordered interpreter names every
   entry-point shim probes before it can run ``install.py`` at all.
   Sites: ``install.sh``, ``install.ps1``, ``first-install.sh``,
   ``first-install.command`` and ``installer.rs`` (POSIX branch).
   ``tests/test_python_candidate_parity.py`` already pinned three of those
   five; this module adds the two ``first-install.*`` shims, which carried
   the same canonical core with **no** lock and **no** "must match" comment.

2. **The ``lean-ctx`` binary cascade** — where the PreToolUse hook looks for
   the ``lean-ctx`` binary when it is not on ``PATH``.
   Sites: ``install.py::_find_lean_ctx_binary``,
   ``templates/hooks/lean-ctx-rewrite.sh``,
   ``templates/hooks/lean-ctx-rewrite.ps1``.
   All three carry a "MUST MATCH" comment; the only test that referenced
   their parity asserted ``"MUST MATCH" in src`` — a **source scan**, which a
   name in a comment satisfies. Nothing could observe an actual drift.

Why both stay C-tier mirrors (data extracted where it can be, parity locked
by these tests) rather than collapsing to one implementation:

* The Python cascade runs at the chicken-and-egg moment — there is no
  interpreter yet, by definition — and ``docs/INSTALL_ARCHITECTURE_v2.md`` §1
  records the decision explicitly: "Shims stay multi-language and thin ... so
  those stay autonomous shell/BAT with parity tests guarding against drift."
* The ``lean-ctx`` cascade lives in a hook that is **installed into arbitrary
  project trees** (``<project>/.claude/hooks/``), where the orchestrator's
  ``scripts/lib/`` does not exist, and it runs on **every** Bash tool call.
  Neither a sourced library nor a parsed rule table is reachable from there.

These tests are the enforcement half of that bargain. They extract the real
lists and compare them; they do not scan for marker strings.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Family 1 - the Python interpreter cascade
# ---------------------------------------------------------------------------

# The canonical PATH-probed interpreter names, newest first. Kept identical to
# ``tests/test_python_candidate_parity.py::EXPECTED_POSIX`` (that module locks
# install.sh / install.ps1 / installer.rs; this one locks the two shims).
EXPECTED_POSIX = ["python3.13", "python3.12", "python3.11", "python3", "python"]


def _extract_for_cand_loop(script: Path) -> list[str]:
    """Return every candidate token of a ``for cand in ... do`` loop.

    Handles backslash line-continuations and a ``do`` on its own line, which
    is the shape both ``first-install.*`` shims use (and which the older
    ``for cmd in <one line>; do`` regex cannot match).
    """
    src = script.read_text(encoding="utf-8")
    m = re.search(r"\nfor\s+cand\s+in\s+(.*?)\n\s*do\b", src, re.DOTALL)
    assert m is not None, (
        f"could not find the `for cand in ... do` cascade in {script.name}"
    )
    body = m.group(1).replace("\\\n", " ")
    tokens: list[str] = []
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens.extend(line.split())
    return tokens


def _bare_names(tokens: list[str]) -> list[str]:
    """The PATH-probed names - i.e. everything that is not an absolute path.

    Absolute-path entries (Homebrew / Linuxbrew prefixes) are the legitimate
    per-OS divergence; the bare names are the shared canonical core.
    """
    return [t for t in tokens if not t.startswith("/")]


def test_first_install_sh_carries_the_canonical_core_in_order():
    """first-install.sh's PATH-probed names == the canonical cascade."""
    actual = _bare_names(_extract_for_cand_loop(REPO_ROOT / "first-install.sh"))
    assert actual == EXPECTED_POSIX, (
        "first-install.sh's Python candidate cascade drifted from canonical.\n"
        f"  expected: {EXPECTED_POSIX!r}\n"
        f"  actual:   {actual!r}\n"
        "Keep it in lock-step with install.sh / install.ps1 / installer.rs "
        "(see tests/test_python_candidate_parity.py)."
    )


def test_first_install_command_carries_the_canonical_core_in_order():
    """first-install.command's PATH-probed names == the canonical cascade.

    The macOS shim probes Homebrew prefixes *before* the bare names; those
    absolute paths are the legitimate divergence and are filtered out here.
    """
    actual = _bare_names(
        _extract_for_cand_loop(REPO_ROOT / "first-install.command")
    )
    assert actual == EXPECTED_POSIX, (
        "first-install.command's Python candidate cascade drifted from "
        "canonical.\n"
        f"  expected: {EXPECTED_POSIX!r}\n"
        f"  actual:   {actual!r}"
    )


def test_shim_extra_candidates_are_absolute_paths_only():
    """A shim may add OS-specific *paths*, never an extra interpreter NAME.

    This is what stops a bare ``python3.10`` (or a Windows-only ``py``) being
    smuggled into a POSIX shim under cover of the "extras are allowed" rule.
    """
    for name in ("first-install.sh", "first-install.command"):
        tokens = _extract_for_cand_loop(REPO_ROOT / name)
        extras = [t for t in tokens if t not in EXPECTED_POSIX]
        offenders = [t for t in extras if not t.startswith("/")]
        assert not offenders, (
            f"{name} adds a non-absolute candidate outside the canonical "
            f"list: {offenders!r}. Extra candidates must be absolute paths "
            "(Homebrew/Linuxbrew prefixes); extra bare names belong in the "
            "canonical cascade."
        )


def test_all_four_shell_entry_points_agree_on_the_core():
    """install.sh == install.ps1 == first-install.sh == first-install.command."""
    sh_src = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"for\s+cmd\s+in\s+([^\n;]+?);\s*do", sh_src)
    assert m is not None, "could not find the Python candidate loop in install.sh"
    install_sh = m.group(1).split()

    ps1_src = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    m = re.search(r"\$candidates\s*=\s*@\(([^)]+)\)", ps1_src)
    assert m is not None, "could not find $candidates in install.ps1"
    install_ps1 = re.findall(r'"([^"]+)"', m.group(1))

    first_sh = _bare_names(_extract_for_cand_loop(REPO_ROOT / "first-install.sh"))
    first_cmd = _bare_names(
        _extract_for_cand_loop(REPO_ROOT / "first-install.command")
    )

    assert install_sh == install_ps1 == first_sh == first_cmd, (
        "Python candidate cascade drift across the shell entry points:\n"
        f"  install.sh            : {install_sh!r}\n"
        f"  install.ps1           : {install_ps1!r}\n"
        f"  first-install.sh      : {first_sh!r}\n"
        f"  first-install.command : {first_cmd!r}\n"
        "All four probe PATH in the same order; a divergence means one entry "
        "point picks a different interpreter than the others on the same box."
    )


# ---------------------------------------------------------------------------
# Family 2 - the lean-ctx binary cascade
# ---------------------------------------------------------------------------


def _normalise(path: str) -> str:
    """Canonical comparison form: ``~`` for home, forward slashes, no .exe."""
    p = path.strip().strip('"').strip("'").replace("\\", "/")
    for home_token in ("$HOME", "$env:USERPROFILE", "$home", "%USERPROFILE%"):
        if p.startswith(home_token):
            p = "~" + p[len(home_token) :]
    if p.endswith(".exe"):
        p = p[: -len(".exe")]
    return p.rstrip("/")


def _leanctx_install_py_posix() -> list[str]:
    """The POSIX ``candidates = [...]`` block of ``_find_lean_ctx_binary``."""
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    start = src.index("def _find_lean_ctx_binary")
    body = src[start : start + 4000]
    m = re.search(r"\n    else:\n(.*?)\n\s*for cand in candidates", body, re.DOTALL)
    assert m is not None, "could not isolate the POSIX arm of _find_lean_ctx_binary"
    arm = m.group(1)
    out: list[str] = []
    for line in arm.split("\n"):
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("home /"):
            parts = re.findall(r'"([^"]+)"', line)
            if parts:
                out.append("~/" + "/".join(parts))
            continue
        m2 = re.match(r'Path\("([^"]+)"\)', line)
        if m2:
            out.append(m2.group(1))
    assert out, "extracted no POSIX lean-ctx candidates from install.py"
    return [_normalise(p) for p in out]


def _leanctx_hook_sh() -> list[str]:
    """The ``for _cand in ...`` fallback list in the bash hook."""
    src = (REPO_ROOT / "templates/hooks/lean-ctx-rewrite.sh").read_text(
        encoding="utf-8"
    )
    # The loop terminates with `"; do` on the final candidate line, not with
    # a `do` on its own line — accept either shape.
    m = re.search(
        r"for\s+_cand\s+in\s+(.*?)(?:;\s*|\n\s*)do\b", src, re.DOTALL
    )
    assert m is not None, (
        "could not find the `for _cand in ...` list in lean-ctx-rewrite.sh"
    )
    body = m.group(1).replace("\\\n", " ")
    return [_normalise(t) for t in re.findall(r'"([^"]+)"', body)]


def _leanctx_hook_ps1_posix() -> list[str]:
    """The POSIX tail of the ``$cands`` build-up in the PowerShell hook.

    The .ps1 probes Windows locations first, then appends the same
    extensionless POSIX candidates so a pwsh-on-Linux host behaves like the
    .sh sibling. Only that POSIX tail is compared here.
    """
    src = (REPO_ROOT / "templates/hooks/lean-ctx-rewrite.ps1").read_text(
        encoding="utf-8"
    )
    start = src.index("$cands = @()")
    end = src.index("foreach ($c in $cands)", start)
    block = src[start:end]
    out: list[str] = []
    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("#") or "$cands +=" not in line:
            continue
        raw = None
        m = re.search(r'Join-Path\s+\$home\s+"([^"]+)"', line)
        if m:
            raw = "~/" + m.group(1)
        elif re.search(r'Join-Path\s+\$env:\w+\s+"[^"]+"', line):
            continue  # Windows-only (ProgramData / ProgramFiles)
        else:
            m = re.search(r'\$cands \+= "([^"]+)"', line)
            if m:
                raw = m.group(1)
        if raw is None:
            continue
        # The Windows arm is exactly the ``.exe`` half; the POSIX tail is the
        # extensionless one. Compare only the latter (install.py's Windows
        # arm is a different list with scoop/chocolatey entries).
        if raw.endswith(".exe"):
            continue
        out.append(_normalise(raw))
    return out


def test_lean_ctx_cascade_agrees_across_install_py_and_both_hooks():
    """The three "MUST MATCH" lean-ctx lists really do match.

    Replaces a source scan (``assert "MUST MATCH" in src``) that a comment
    alone satisfied. The failure this pins is real and was user-visible: when
    install.py's list and the hook's list disagreed, install reported
    "lean-ctx detected" for a binary the hook shell could not see, so
    compression silently never activated.
    """
    from_py = _leanctx_install_py_posix()
    from_sh = _leanctx_hook_sh()
    from_ps1 = _leanctx_hook_ps1_posix()

    assert from_py == from_sh == from_ps1, (
        "lean-ctx candidate cascade drift:\n"
        f"  install.py::_find_lean_ctx_binary (POSIX) : {from_py!r}\n"
        f"  templates/hooks/lean-ctx-rewrite.sh       : {from_sh!r}\n"
        f"  templates/hooks/lean-ctx-rewrite.ps1      : {from_ps1!r}\n"
        "All three probe the same locations in the same order. A divergence "
        "means install.py can declare lean-ctx 'installed' at a path the hook "
        "never probes (or vice-versa)."
    )


def test_lean_ctx_cascade_starts_at_cargo_bin():
    """``~/.cargo/bin`` must stay FIRST - it is the whole reason the fallback
    probe exists (``cargo install lean-ctx`` lands there, and a hook shell's
    PATH usually lacks it)."""
    for label, lst in (
        ("install.py", _leanctx_install_py_posix()),
        ("lean-ctx-rewrite.sh", _leanctx_hook_sh()),
        ("lean-ctx-rewrite.ps1", _leanctx_hook_ps1_posix()),
    ):
        assert lst and lst[0] == "~/.cargo/bin/lean-ctx", (
            f"{label} no longer probes ~/.cargo/bin/lean-ctx first: {lst!r}"
        )
