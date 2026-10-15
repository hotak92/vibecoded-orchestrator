# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Windows skips of a bash ``.sh`` leg must name the ``.ps1`` test covering it.

Owner ruling 2026-10-15 (v0.2.103 Windows triage): a test that drives a
``.sh`` asset through bare ``bash`` cannot run on Windows (``bash`` there is
WSL's, which cannot open a Windows path), and is skipped there ONLY when a
passing ``.ps1``-sibling test covers the same behaviour. The skip's reason
names that sibling, in one convention::

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="bash .sh leg; Windows runs the .ps1 sibling: <sibling>")

where ``<sibling>`` is ``tests/<file>.py::<Class>::<test>[<param>]`` (class
and param optional) or ``this test's [ps1] param`` for an sh/ps1-parametrized
test. This guard keeps the convention honest:

* every convention skip's sibling RESOLVES — the file exists, the test is
  defined there, a named param is among its parametrize values, and the
  sibling is not itself skipped on Windows (a skip pointing at a skip is no
  cover at all);
* the Windows skips that do NOT follow the convention — the POSIX-only
  primitives (flock, mode bits, signals, fake ``#!/bin/sh`` venvs) that have
  no sibling by nature — are frozen at their current count, so a new bash-leg
  skip cannot slip in under a free-form reason. Raising the baseline is a
  deliberate act in the same change that adds such a skip, and a reviewer
  sees it.

A fully general "every Windows skip names a sibling" rule is not feasible:
those POSIX-primitive skips guard behaviour that has no Windows counterpart
to point at.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
PREFIX = "bash .sh leg; Windows runs the .ps1 sibling: "
SAME_TEST_PS1 = "this test's [ps1] param"

#: Windows skips WITHOUT the sibling convention (POSIX-only primitives) as of
#: v0.2.103. Lowering it is always fine; raising it needs a reason in review.
NON_CONVENTION_BASELINE = 76


def _is_win32_condition(node: ast.AST) -> bool:
    return "sys.platform == 'win32'" in ast.unparse(node)


def _skip_reason(call: ast.Call) -> "str | None":
    for kw in call.keywords:
        if kw.arg == "reason":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    return None


def _win32_skips(tree: ast.AST) -> "list[tuple[ast.Call, str | None]]":
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("skipif", "skipIf")
            and node.args
            and _is_win32_condition(node.args[0])
        ):
            out.append((node, _skip_reason(node)))
    return out


def _test_files() -> "list[Path]":
    return sorted(p for p in TESTS.rglob("*.py") if "fixtures" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _string_constants(nodes) -> "set[str]":
    found: set[str] = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def _decorated_win32_skip(node: ast.AST) -> bool:
    """A Windows skip on the WHOLE test/class — a skip nested in one
    ``pytest.param`` (another param's bash leg) does not count."""
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr in ("skipif", "skipIf")
        and d.args
        and _is_win32_condition(d.args[0])
        for d in getattr(node, "decorator_list", [])
    )


def _resolve(sibling: str) -> "str | None":
    """``None`` when *sibling* resolves to a test that runs on Windows,
    else the reason it does not."""
    if "::" not in sibling:
        return f"{sibling!r} is not a test id (want tests/<file>.py::<test>)"
    rel, *names = sibling.split("::")
    path = REPO_ROOT / rel
    if not path.is_file():
        return f"{rel} does not exist"
    param = None
    if names and names[-1].endswith("]") and "[" in names[-1]:
        names[-1], param = names[-1][:-1].split("[", 1)
    scope: "list[ast.stmt]" = list(_parse(path).body)
    chain: list[ast.AST] = []
    for name in names:
        match = next(
            (n for n in scope
             if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == name),
            None,
        )
        if match is None:
            return f"{name} is not defined in {rel}"
        chain.append(match)
        scope = list(getattr(match, "body", []))
    if any(_decorated_win32_skip(n) for n in chain):
        return f"{sibling} is itself skipped on Windows"
    if param is not None:
        decorators = getattr(chain[-1], "decorator_list", [])
        if param not in _string_constants(decorators):
            return f"[{param}] is not a parametrize value of {sibling}"
    return None


def _convention_skips():
    for path in _test_files():
        tree = _parse(path)
        for call, reason in _win32_skips(tree):
            if isinstance(reason, str) and reason.startswith(PREFIX):
                yield path, tree, call, reason[len(PREFIX):].strip()


def test_every_bash_leg_skip_names_a_sibling_that_runs_on_windows():
    seen = 0
    problems = []
    for path, tree, call, sibling in _convention_skips():
        seen += 1
        where = f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
        if sibling == SAME_TEST_PS1:
            if "ps1" not in _string_constants([tree]):
                problems.append(f"{where}: names its [ps1] param, but the file "
                                "parametrizes no 'ps1' value")
            continue
        why = _resolve(sibling)
        if why:
            problems.append(f"{where}: {why}")
    assert seen, "no bash-leg skip found — the convention scan itself is broken"
    assert not problems, "bash-leg skips without a running sibling:\n" + "\n".join(problems)


def test_windows_skips_outside_the_convention_do_not_grow():
    loose = [
        f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
        for path in _test_files()
        for call, reason in _win32_skips(_parse(path))
        if not (isinstance(reason, str) and reason.startswith(PREFIX))
    ]
    assert len(loose) <= NON_CONVENTION_BASELINE, (
        f"{len(loose)} Windows skips do not name a .ps1 sibling (baseline "
        f"{NON_CONVENTION_BASELINE}). A skipped bash leg must use the reason "
        f"prefix {PREFIX!r} and name the sibling that covers it; a skip for a "
        "POSIX-only primitive with no sibling raises the baseline in the same "
        "change, deliberately.\n" + "\n".join(loose)
    )


def test_the_resolver_rejects_a_missing_or_skipped_sibling():
    """Both arms of the resolver, so the guard cannot pass vacuously."""
    assert _resolve("tests/test_v02103_win_skip_sibling_guard.py::"
                    "test_the_resolver_rejects_a_missing_or_skipped_sibling") is None
    assert _resolve("tests/no_such_file.py::test_x")
    assert _resolve("tests/test_v02103_win_skip_sibling_guard.py::test_no_such_test")
    assert _resolve(
        "tests/test_v02103_win_skip_sibling_guard.py::"
        "test_the_resolver_rejects_a_missing_or_skipped_sibling[nope]")
    # A sibling that is itself skipped on Windows is no cover.
    assert "itself skipped" in (_resolve(
        "tests/test_v02102_w6c2_subagent_hook_census.py::"
        "test_edit_outcome_carries_agent_id") or "")
