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
import itertools
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
PREFIX = "bash .sh leg; Windows runs the .ps1 sibling: "
SAME_TEST_PS1 = "this test's [ps1] param"

#: Windows skips WITHOUT the sibling convention (POSIX-only primitives) as of
#: v0.2.103. Lowering it is always fine; raising it needs a reason in review.
#: 76 -> 177 (R20 L3, 2026-10-15): not new skips — the scan used to see only
#: conditions spelled ``sys.platform == 'win32'``; it now also counts
#: ``os.name == 'nt'``, ``platform.system()``, module constants
#: (``IS_WINDOWS``, ``not POSIX``), ``skipUnless`` and imperative
#: ``if <windows>: pytest.skip(...)``, all of which were already there.
#: 177 -> 178 (Fable FINAL4, 2026-10-16): the uninstall stopper's live `-S`
#: test sends real POSIX signals and reads /proc — a POSIX-only primitive with
#: no .ps1 sibling; its Windows leg is unit-tested with a fake runner
#: (tests/test_v02103_uninstall_state.py::test_windows_listing_survives_*).
#: 178 -> 179 (v0.2.104 scratch dir): tests/test_v02104_scratch_dir.py's
#: POSIX_ONLY marker guards 0700 mode bits and unprivileged symlinks (the
#: sweep's never-follow arms) — POSIX-only primitives; the Windows ACL arm is
#: unit-tested with a fake runner (test_windows_arm_*) and its bash legs use
#: the sibling convention.
NON_CONVENTION_BASELINE = 179


# -- which conditions skip on Windows --------------------------------------
#
# A Windows skip is recognised by what its condition DOES, not by how it is
# spelled: ``sys.platform == "win32"``, ``os.name == "nt"``,
# ``platform.system() == "Windows"``, ``sys.platform.startswith("win")``,
# ``not POSIX`` (``POSIX = os.name == "posix"``), a module constant such as
# ``IS_WINDOWS``, ``skipUnless(not IS_WINDOWS)`` and an imperative
# ``if os.name == "nt": pytest.skip(...)`` all count. A condition skips on
# Windows when, for some value of its non-platform parts (``BASH is None`` …),
# it is true on Windows and false on Linux.

_PLATFORM_VALUES = {
    "windows": {"sys.platform": "win32", "os.name": "nt", "platform.system()": "Windows"},
    "linux": {"sys.platform": "linux", "os.name": "posix", "platform.system()": "Linux"},
}
_SKIP_MARKS = ("skipif", "skipIf", "skipUnless")


def _module_env(tree: ast.Module) -> "dict[str, ast.expr]":
    """Module-level ``NAME = <expr>`` bindings, so a condition or a marks list
    spelled through a constant (``IS_WINDOWS``, ``FLAVOURS``) is followed."""
    env: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            env[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            env[node.target.id] = node.value
    return env


def _module_ref(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Name):
        return node.id.lstrip("_")
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "__import__" and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def _platform_key(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Attribute):
        ref = _module_ref(node.value)
        if node.attr == "platform" and ref == "sys":
            return "sys.platform"
        if node.attr == "name" and ref == "os":
            return "os.name"
    if (isinstance(node, ast.Call) and not node.args and isinstance(node.func, ast.Attribute)
            and node.func.attr == "system" and _module_ref(node.func.value) == "platform"):
        return "platform.system()"
    return None


def _str_consts(node: ast.AST) -> "list[str] | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]  # type: ignore[attr-defined]
    return None


def _evaluate(node, env, values, leaf, seen=frozenset()) -> bool:
    """Truth of *node* on the platform described by *values*; anything that is
    not a platform test is a free leaf whose value *leaf* supplies."""
    if isinstance(node, ast.BoolOp):
        results = [_evaluate(v, env, values, leaf, seen) for v in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate(node.operand, env, values, leaf, seen)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right, op = node.left, node.comparators[0], node.ops[0]
        key, other = _platform_key(left), right
        if key is None:
            key, other = _platform_key(right), left
        consts = _str_consts(other)
        if key is not None and consts is not None:
            actual = values[key]
            if isinstance(op, ast.Eq):
                return actual == consts[0]
            if isinstance(op, ast.NotEq):
                return actual != consts[0]
            if isinstance(op, ast.In) and other is right:
                return actual in consts
            if isinstance(op, ast.NotIn) and other is right:
                return actual not in consts
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("startswith", "endswith") and len(node.args) == 1):
        key = _platform_key(node.func.value)
        consts = _str_consts(node.args[0])
        if key is not None and consts is not None:
            return getattr(values[key], node.func.attr)(tuple(consts))
    if isinstance(node, ast.Name) and node.id in env and node.id not in seen:
        return _evaluate(env[node.id], env, values, leaf, seen | {node.id})
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):  # pytest's string-condition form
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return bool(node.value)
            return _evaluate(parsed, env, values, leaf, seen)
        return bool(node.value)
    return leaf(ast.dump(node))


def _skips_on_windows(cond: ast.AST, env: "dict[str, ast.expr]") -> bool:
    keys: list[str] = []

    def record(key: str) -> bool:
        if key not in keys:
            keys.append(key)
        return False

    _evaluate(cond, env, _PLATFORM_VALUES["windows"], record)
    if len(keys) <= 8:
        assignments = [dict(zip(keys, bits))
                       for bits in itertools.product((False, True), repeat=len(keys))]
    else:
        assignments = [dict.fromkeys(keys, False), dict.fromkeys(keys, True)]
    for a in assignments:
        def leaf(key: str, a=a) -> bool:
            return a.get(key, False)
        if (_evaluate(cond, env, _PLATFORM_VALUES["windows"], leaf)
                and not _evaluate(cond, env, _PLATFORM_VALUES["linux"], leaf)):
            return True
    return False


def _mark_name(call: ast.Call) -> "str | None":
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _skip_condition(call: ast.Call) -> "ast.expr | None":
    """The condition under which a skip mark SKIPS (``skipUnless`` inverted)."""
    name = _mark_name(call)
    if name not in _SKIP_MARKS:
        return None
    cond = call.args[0] if call.args else next(
        (kw.value for kw in call.keywords if kw.arg == "condition"), None)
    if cond is None:
        return None
    return ast.UnaryOp(op=ast.Not(), operand=cond) if name == "skipUnless" else cond


def _skip_reason(call: ast.Call) -> "str | None":
    for kw in call.keywords:
        if kw.arg in ("reason", "msg"):
            value = kw.value
            return value.value if isinstance(value, ast.Constant) \
                and isinstance(value.value, str) else None
    index = 0 if _mark_name(call) in ("skip", "skipTest") else 1
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _is_win32_skip(node: ast.AST, env: "dict[str, ast.expr]") -> bool:
    if not isinstance(node, ast.Call):
        return False
    cond = _skip_condition(node)
    return cond is not None and _skips_on_windows(cond, env)


def _imperative_skips(stmt: ast.If) -> "list[ast.Call]":
    """``pytest.skip(...)`` / ``self.skipTest(...)`` directly in an if-body."""
    return [
        s.value for s in stmt.body
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
        and _mark_name(s.value) in ("skip", "skipTest")
    ]


def _win32_skips(tree: ast.Module) -> "list[tuple[ast.Call, str | None]]":
    env = _module_env(tree)
    out = []
    for node in ast.walk(tree):
        if _is_win32_skip(node, env):
            out.append((node, _skip_reason(node)))  # type: ignore[arg-type]
        elif isinstance(node, ast.If) and _skips_on_windows(node.test, env):
            out.extend((call, _skip_reason(call)) for call in _imperative_skips(node))
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


def _resolved(node: ast.AST, env: "dict[str, ast.expr]", depth: int = 0) -> ast.AST:
    while isinstance(node, ast.Name) and node.id in env and depth < 8:
        node, depth = env[node.id], depth + 1
    return node


def _marks_skip_on_windows(marks: "ast.AST | None", env: "dict[str, ast.expr]") -> bool:
    """A mark, a mark constant, or a list of either, that skips on Windows."""
    if marks is None:
        return False
    marks = _resolved(marks, env)
    if isinstance(marks, (ast.List, ast.Tuple)):
        return any(_marks_skip_on_windows(m, env) for m in marks.elts)
    return _is_win32_skip(marks, env)


def _pytestmark(body: "list[ast.stmt]") -> "ast.expr | None":
    return next((n.value for n in body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                         for t in n.targets)), None)


def _decorated_win32_skip(node: ast.AST, env: "dict[str, ast.expr]") -> bool:
    """A Windows skip on the WHOLE test/class (decorator or a class-level
    ``pytestmark``) — a skip nested in one ``pytest.param`` (another param's
    bash leg) does not count."""
    if any(_marks_skip_on_windows(d, env) for d in getattr(node, "decorator_list", [])):
        return True
    return isinstance(node, ast.ClassDef) and _marks_skip_on_windows(
        _pytestmark(node.body), env)


def _param_marks(func: ast.AST, env: "dict[str, ast.expr]") -> "dict[str, list[ast.AST | None]]":
    """Each parametrize value of *func* mapped to the marks of every
    occurrence (``None`` = a bare value with no marks), following module
    constants (``FLAVOURS``) into ``pytest.param(..., marks=...)``."""
    found: dict[str, list[ast.AST | None]] = {}
    for dec in getattr(func, "decorator_list", []):
        if not (isinstance(dec, ast.Call) and _mark_name(dec) == "parametrize"):
            continue
        argvalues = dec.args[1] if len(dec.args) > 1 else next(
            (kw.value for kw in dec.keywords if kw.arg == "argvalues"), None)
        argvalues = _resolved(argvalues, env) if argvalues is not None else None
        if not isinstance(argvalues, (ast.List, ast.Tuple)):
            continue
        for elt in argvalues.elts:
            elt = _resolved(elt, env)
            if isinstance(elt, ast.Call) and _mark_name(elt) == "param":
                marks = next((kw.value for kw in elt.keywords if kw.arg == "marks"), None)
                for value in _string_constants(elt.args):
                    found.setdefault(value, []).append(marks)
            else:
                for value in _string_constants([elt]):
                    found.setdefault(value, []).append(None)
    return found


def _resolve(sibling: str, root: Path = REPO_ROOT) -> "str | None":
    """``None`` when *sibling* resolves to a test that runs on Windows,
    else the reason it does not."""
    if "::" not in sibling:
        return f"{sibling!r} is not a test id (want tests/<file>.py::<test>)"
    rel, *names = sibling.split("::")
    path = root / rel
    if not path.is_file():
        return f"{rel} does not exist"
    param = None
    if names and names[-1].endswith("]") and "[" in names[-1]:
        names[-1], param = names[-1][:-1].split("[", 1)
    tree = _parse(path)
    env = _module_env(tree)
    if _marks_skip_on_windows(_pytestmark(tree.body), env):
        return f"{rel} is skipped on Windows as a whole (pytestmark)"
    scope: "list[ast.stmt]" = list(tree.body)
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
    if any(_decorated_win32_skip(n, env) for n in chain):
        return f"{sibling} is itself skipped on Windows"
    if param is not None:
        values = _param_marks(chain[-1], env)
        if param not in values and param not in _string_constants(
                getattr(chain[-1], "decorator_list", [])):
            return f"[{param}] is not a parametrize value of {sibling}"
        occurrences = values.get(param, [])
        if occurrences and all(_marks_skip_on_windows(m, env) for m in occurrences):
            return f"{sibling} is itself skipped on Windows (its pytest.param marks)"
    return None


def _same_test_ps1_problem(tree: ast.Module, call: ast.Call) -> "str | None":
    """A ``this test's [ps1] param`` skip is cover only when the file has a
    ``ps1`` param that runs on Windows, and the skip sits on the bash param
    alone — a decorator-level skip removes the [ps1] param with it. Checked
    per parametrize list, so another test's [ps1] cannot vouch for this one."""
    env = _module_env(tree)
    for node in ast.walk(tree):
        decorators = list(getattr(node, "decorator_list", []))
        if any(d is call or _resolved(d, env) is call for d in decorators):
            return "names its [ps1] param, but skips the whole test, [ps1] included"

    def carries_call(marks: "ast.AST | None") -> bool:
        marks = _resolved(marks, env) if marks is not None else None
        if isinstance(marks, (ast.List, ast.Tuple)):
            return any(carries_call(m) for m in marks.elts)
        return marks is call

    def is_param(node: ast.AST, value: str) -> bool:
        return (isinstance(node, ast.Call) and _mark_name(node) == "param" and bool(node.args)
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == value)

    def marks_of(node: ast.AST) -> "ast.AST | None":
        if not isinstance(node, ast.Call):
            return None
        return next((kw.value for kw in node.keywords if kw.arg == "marks"), None)

    # The parametrize list(s) whose bash param carries THIS skip: the [ps1]
    # beside it must exist and run on Windows.
    lists = [
        [_resolved(e, env) for e in n.elts] for n in ast.walk(tree)
        if isinstance(n, (ast.List, ast.Tuple))
        and any(is_param(_resolved(e, env), "sh") and carries_call(marks_of(_resolved(e, env)))
                for e in n.elts)
    ]
    if not lists:
        return ("names its [ps1] param, but it does not sit on an 'sh' pytest.param "
                "of a parametrize list")
    for elts in lists:
        running = [e for e in elts
                   if (isinstance(e, ast.Constant) and e.value == "ps1")
                   or (is_param(e, "ps1") and not _marks_skip_on_windows(marks_of(e), env))]
        if not running:
            if any(is_param(e, "ps1") for e in elts):
                return "names its [ps1] param, but that param is itself skipped on Windows"
            return "names its [ps1] param, but its parametrize list has no 'ps1' value"
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
            why = _same_test_ps1_problem(tree, call)
            if why:
                problems.append(f"{where}: {why}")
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


def _loose_and_convention(source: str) -> "list[str | None]":
    return [reason for _call, reason in _win32_skips(ast.parse(source))]


def test_a_windows_skip_is_recognised_however_it_is_spelled():
    """R20 L3: a skip spelled ``os.name == "nt"`` (or any other way) used to
    evade both the sibling convention and the baseline freeze."""
    spelled = [
        'import os, pytest\n@pytest.mark.skipif(os.name == "nt", reason="r")\ndef test_a(): pass',
        'import platform, pytest\n@pytest.mark.skipif(platform.system() == "Windows", reason="r")\n'
        'def test_a(): pass',
        'import sys, pytest\n@pytest.mark.skipif(sys.platform.startswith("win"), reason="r")\n'
        'def test_a(): pass',
        'import os, pytest\nIS_WINDOWS = os.name == "nt"\n'
        '@pytest.mark.skipif(IS_WINDOWS, reason="r")\ndef test_a(): pass',
        'import os, shutil, pytest\nPOSIX = os.name == "posix"\n'
        '@pytest.mark.skipif(not POSIX or shutil.which("bash") is None, reason="r")\n'
        'def test_a(): pass',
        'import sys, unittest\nclass T(unittest.TestCase):\n'
        '    @unittest.skipUnless(sys.platform != "win32", "r")\n    def test_a(self): pass',
        'import pytest\n@pytest.mark.skipif("sys.platform == \'win32\'", reason="r")\n'
        'def test_a(): pass',
        'import os, pytest\ndef test_a():\n    if os.name == "nt":\n        pytest.skip("r")',
        'import sys, pytest\n@pytest.mark.parametrize("x", [pytest.param("sh", marks='
        'pytest.mark.skipif(sys.platform == "win32", reason="r"))])\ndef test_a(x): pass',
    ]
    for source in spelled:
        assert _loose_and_convention(source) == ["r"], source
    not_windows = [
        'import sys, pytest\n@pytest.mark.skipif(sys.platform != "win32", reason="r")\n'
        'def test_a(): pass',
        'import os, pytest\n@pytest.mark.skipif(os.name == "posix", reason="r")\n'
        'def test_a(): pass',
        'import shutil, pytest\n@pytest.mark.skipif(shutil.which("bash") is None, reason="r")\n'
        'def test_a(): pass',
        'import os, pytest\ndef test_a():\n    if os.name != "nt":\n        pytest.skip("r")',
    ]
    for source in not_windows:
        assert _loose_and_convention(source) == [], source


def test_the_resolver_rejects_a_sibling_whose_named_param_is_skipped(tmp_path):
    """R20 L3: a sibling ``[ps1]`` whose ``pytest.param`` carries a Windows
    skip (directly or through a marks constant) is no cover."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sib.py").write_text(
        "import os, pytest\n"
        "_WIN = pytest.mark.skipif(os.name == 'nt', reason='x')\n"
        "FLAVOURS = [pytest.param('ps1', marks=[_WIN]), 'sh']\n"
        "@pytest.mark.parametrize('f', FLAVOURS)\n"
        "def test_skipped_param(f): pass\n"
        "@pytest.mark.parametrize('f', ['ps1', pytest.param('sh', marks=_WIN)])\n"
        "def test_running_param(f): pass\n"
        "class TestK:\n"
        "    pytestmark = _WIN\n"
        "    def test_in_skipped_class(self): pass\n",
        encoding="utf-8")
    sib = "tests/test_sib.py::"
    assert "pytest.param marks" in (_resolve(sib + "test_skipped_param[ps1]", tmp_path) or "")
    assert _resolve(sib + "test_skipped_param[sh]", tmp_path) is None
    assert _resolve(sib + "test_running_param[ps1]", tmp_path) is None
    assert "itself skipped" in (_resolve(sib + "TestK::test_in_skipped_class", tmp_path) or "")
    (tests / "test_mod.py").write_text(
        "import sys, pytest\n"
        "pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
        "def test_a(): pass\n", encoding="utf-8")
    assert "as a whole" in (_resolve("tests/test_mod.py::test_a", tmp_path) or "")


def test_a_same_test_ps1_skip_must_leave_a_running_ps1_param():
    """The ``this test's [ps1] param`` form: a decorator-level skip takes the
    [ps1] param with it, and a [ps1] param skipped on Windows covers nothing."""
    reason = PREFIX + SAME_TEST_PS1

    def problem(source: str) -> "str | None":
        tree = ast.parse(source)
        call = next(c for c, r in _win32_skips(tree) if r == reason)
        return _same_test_ps1_problem(tree, call)

    good = ("import sys, pytest\n@pytest.mark.parametrize('f', [pytest.param('sh', marks="
            f"pytest.mark.skipif(sys.platform == 'win32', reason={reason!r})), 'ps1'])\n"
            "def test_a(f): pass")
    assert problem(good) is None
    whole = (f"import sys, pytest\n@pytest.mark.skipif(sys.platform == 'win32', reason={reason!r})\n"
             "@pytest.mark.parametrize('f', ['sh', 'ps1'])\ndef test_a(f): pass")
    assert "whole test" in (problem(whole) or "")
    ps1_skipped = (
        "import sys, pytest\n_W = pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
        "@pytest.mark.parametrize('f', [pytest.param('sh', marks="
        f"pytest.mark.skipif(sys.platform == 'win32', reason={reason!r})), "
        "pytest.param('ps1', marks=_W)])\ndef test_a(f): pass")
    assert "itself skipped" in (problem(ps1_skipped) or "")
