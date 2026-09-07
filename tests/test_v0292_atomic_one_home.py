# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 duplication-merge (PLAN-EXTENSION §3.13) — ONE atomic-write home.

The straggler proof as a test. Before the merge, ``vco_lib`` and the MCP
tree carried ~14 hand-rolled ``tmp + os.replace`` writers beside
:mod:`vco_lib.atomic`; the per-project bundle writer alone was a ~100-line
sibling of :func:`vco_lib.atomic.atomic_write_bytes` with its own copy of
the NEW-8 symlink walk. Every one of them is now a call into ``atomic.py``.

What this pins:

1. **No live ``os.replace(...)`` call outside ``vco_lib/atomic.py``** in
   ``vco_lib/`` and ``claude_mcp_servers/`` — by AST (a docstring that
   *mentions* ``os.replace`` is documentation, not a copy; register-34).
2. **The home itself still performs the rename** — a positive pin, so a
   refactor that hollowed ``atomic.py`` out (or a scanner that went blind)
   cannot pass this file.
3. **The two documented remaining copies are exactly the two documented
   ones** — ``templates/scripts/summary_backends.py`` (dependency-light by
   design: it runs under ``claude -p`` inside user projects with no
   orchestrator root resolved) and the Python heredoc inside
   ``templates/scripts/vct_retrieval_tuning_set.sh`` (shell-embedded, no
   ``vco_lib`` on its path). Each carries a comment naming the home; if a
   third appears, or one of these two is migrated, this list must move
   with it.

Red-proofed three ways: ``os.replace`` in a comment → PASS; a live
``os.replace(...)`` call in a fresh ``vco_lib`` module → FAIL; the home's
own rename removed → FAIL (check 2).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = REPO_ROOT / "vco_lib" / "atomic.py"

#: Shipped copies that resist extraction, each with the reason in-file.
DOCUMENTED_TEMPLATE_COPIES = {
    "templates/scripts/summary_backends.py",
    "templates/scripts/vct_retrieval_tuning_set.sh",
}

SCAN_ROOTS = ("vco_lib", "claude_mcp_servers")
_SKIP_PARTS = {"node_modules", "target", ".venv", "__pycache__", "excalidraw_mcp_fork"}


def _live_os_replace_calls(source: str) -> int:
    """Count ``<name>.replace(...)`` CALLS where ``<name>`` is an ``os``
    module alias (``os`` / ``_os``). ``str.replace`` on a string literal or
    variable does not match because the receiver is not a bare ``os`` name;
    comments and docstrings are invisible to the AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "replace"
            and isinstance(fn.value, ast.Name)
            and fn.value.id in {"os", "_os"}
        ):
            n += 1
    return n


def _python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        yield path


def test_the_home_still_performs_the_rename():
    """Positive pin — proves the scanner sees live calls, and that the home
    was not hollowed out."""
    assert _live_os_replace_calls(HOME.read_text(encoding="utf-8")) >= 1


def test_no_live_os_replace_outside_the_home():
    offenders: dict[str, int] = {}
    for root_name in SCAN_ROOTS:
        for path in _python_files(REPO_ROOT / root_name):
            if path == HOME:
                continue
            n = _live_os_replace_calls(path.read_text(encoding="utf-8", errors="replace"))
            if n:
                offenders[str(path.relative_to(REPO_ROOT))] = n
    assert not offenders, (
        "hand-rolled tmp+os.replace writers outside vco_lib/atomic.py — call "
        f"atomic_write_text/bytes/json instead: {offenders}"
    )


def test_template_scripts_carry_only_the_documented_copies():
    """``templates/scripts/`` ships into user projects, where ``vco_lib`` is
    reachable only through the sanctioned root ladder. The copies that
    remain are enumerated, and each must NAME the home in-file so the next
    editor finds it."""
    found: set[str] = set()
    scripts = REPO_ROOT / "templates" / "scripts"
    for path in scripts.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            n = _live_os_replace_calls(text)
        else:
            # Shell files: a Python heredoc is opaque to the AST, so count
            # the literal call form; prose in shell comments never carries
            # the parenthesised form.
            n = text.count("os.replace(")
        if n:
            found.add(str(path.relative_to(REPO_ROOT)))
    assert found == DOCUMENTED_TEMPLATE_COPIES, (
        f"template-script os.replace copies changed: now {sorted(found)}, "
        f"documented {sorted(DOCUMENTED_TEMPLATE_COPIES)} — migrate onto "
        "vco_lib.atomic or update the documented set WITH a reason"
    )
    for rel in DOCUMENTED_TEMPLATE_COPIES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert "vco_lib.atomic" in text, f"{rel} does not name the home it mirrors"


@pytest.mark.parametrize(
    "src, expected",
    [
        ("# os.replace(tmp, path) is what the home does\n", 0),
        ('"""docstring: then ``os.replace()``\'d into place."""\n', 0),
        ("import os\nos.replace(a, b)\n", 1),
        ("import os as _os\n_os.replace(a, b)\n", 1),
        ("s = 'x'.replace('a', 'b')\n", 0),
    ],
)
def test_scanner_counts_calls_not_prose(src: str, expected: int):
    assert _live_os_replace_calls(src) == expected
