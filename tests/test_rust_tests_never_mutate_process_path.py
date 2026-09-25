# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test hygiene (v0.2.97 review R6): Rust tests and the process environment.

Cargo runs a crate's tests as threads of one process, so the process
environment is shared by every running test — and by every child any of
them spawns. Two rules, both hard failures:

1. **No test sets the process ``PATH``** — directly
   (``set_var("PATH"`` / ``remove_var("PATH"``) or through a helper's
   ``("PATH", Some(..))`` / ``("PATH", None)`` pair (``with_env_vars``,
   ``env_guard``, a module's own ``with_env``). Taking a lock does not make
   it safe: a lock orders the tests that take it, while EVERY child spawned
   by bare name (``python3``, ``git``) reads ``PATH``. A test that blanked
   it made a concurrent ``python3 -m vco_lib…`` spawn fail with "not found"
   (an intermittent unregister-test failure, 2026-09-24). A test injects
   instead: the child's own env (``Command::env("PATH", …)``), an absolute
   program path, a pure function over an explicit PATH value
   (``runtime::augmented_path``), or the per-thread lookup hook
   ``vct_launcher_core::paths::with_lookup_path`` (which
   ``paths::spawn_program`` honours for bare program names).

3. **The process ``PATH`` is READ in one place** —
   ``vct_launcher_core::paths::lookup_path`` (behind ``which_on_path`` /
   ``spawn_program``), plus the listed non-lookup reads. A hand-rolled walk
   (``volumes``, ``storage_ux`` and ``runtime_install`` each had one) cannot
   be injected, and drifts (one missed ``podman.exe`` on Windows).

2. **Every other process-env mutation holds THE lock** —
   ``vct_launcher_core::test_env::GLOBAL_ENV_MUTEX``. A per-module
   ``static SERIALIZE`` (or ``#[serial]``) orders one module's tests; the
   environment is the whole binary's. The evidence, in the function that
   calls ``set_var`` / ``remove_var``:

   * the lock taken earlier in the same function — ``env_lock()``,
     ``env_guard(``, ``state_dir_guard(``/``state_dir_guard_with(``,
     ``with_env_vars(``, ``with_state_dir(``, or ``GLOBAL_ENV_MUTEX``;
   * or it calls, earlier, a same-file helper whose return type carries
     the proof (``fn scratch_root() -> StateDirGuard``);
   * or the function takes an ``EnvLock`` (a helper that mutates on its
     caller's behalf asks for the proof);
   * or it is ``drop`` of a type holding an ``EnvLock`` /
     ``test_env::EnvGuard`` / ``StateDirGuard`` (the restore runs before the
     lock is released).

   A function that takes ``env_lock()`` must not also take a guard: the
   mutex is not reentrant, so that shape deadlocks. And no module may define
   its own ``env_lock`` unless it returns ``EnvLock`` — a private mutex under
   that name would satisfy this check while ordering only its own module.

Grep-level on purpose: this is hygiene over a shape, not wiring. Comments and
string contents are blanked before matching, so prose never trips it.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Iterator, NamedTuple

REPO = Path(__file__).resolve().parents[1]
RUST_ROOT = REPO / "launcher" / "src-tauri"

_MUTATION = re.compile(r"\b(?:set_var|remove_var)\s*\(")
_PATH_DIRECT = re.compile(r"\b(?:set_var|remove_var)\s*\(\s*\"PATH\"")
_PATH_READ = re.compile(r"\benv::var(?:_os)?\s*\(\s*\"PATH\"\s*\)")
_PATH_PAIR = re.compile(r"\(\s*\"PATH\"\s*,\s*(?:Some\s*\(|None\b)")
_LOCK_TAKEN = re.compile(
    r"\benv_lock\s*\(\s*\)|\bGLOBAL_ENV_MUTEX\b|\benv_guard\s*\(|\bstate_dir_guard(?:_with)?\s*\("
    r"|\bwith_env_vars\s*\(|\bwith_state_dir\s*\("
)
_GUARD_TAKEN = re.compile(
    r"\benv_guard\s*\(|\bstate_dir_guard(?:_with)?\s*\(|\bwith_env_vars\s*\(|\bwith_state_dir\s*\("
)
#: Types that prove the lock is held. `EnvGuard` only when QUALIFIED: several
#: test modules define a local struct of that name that holds something else.
#: test_env's lock-taking functions — the names rule 2 accepts as evidence,
#: so no other module may define one (a local `env_lock` may only delegate,
#: returning `EnvLock`).
_EVIDENCE_NAMES = frozenset({
    "env_lock", "env_guard", "state_dir_guard", "state_dir_guard_with", "with_env_vars", "with_state_dir",
})
_LOCK_TYPES = re.compile(r"\bEnvLock\b|\btest_env::(?:EnvGuard|StateDirGuard)\b|\bStateDirGuard\b")

#: (file, fn) — production code whose JOB is to set the process PATH.
PATH_ALLOWED: dict[tuple[str, str], str] = {
    ("vct-launcher-core/src/services/runtime.rs", "augment_path_for_graphical_launch"):
        "the launcher's startup PATH augment (single-threaded, before any spawn); "
        "its logic is the pure augmented_path",
}

#: (file, fn) — the only places that READ the process PATH (rule 3), and why.
PATH_READ_ALLOWED: dict[tuple[str, str], str] = {
    ("vct-launcher-core/src/paths.rs", "lookup_path"):
        "THE lookup home: which_on_path / spawn_program / every ladder read PATH "
        "through it, so a test can inject one per thread",
    ("vct-launcher-core/src/services/runtime.rs", "augment_path_for_graphical_launch"):
        "the startup augment reads the PATH it extends",
    ("src/commands/coordination.rs", "coordination_apply_schema"):
        "hands the process PATH to a child after env_clear — a passthrough, not a lookup",
}

#: (file, fn) — env mutations that are not a test holding the lock, and why.
LOCK_EXEMPT: dict[tuple[str, str], str] = {
    **PATH_ALLOWED,
    ("src/webkit_preflight.rs", "probe_and_apply_workaround_if_needed"):
        "production: the launcher's WebKitGTK workaround, set in main() before the "
        "webview (and any other thread) starts",
    ("vct-launcher-core/src/test_env.rs", "drop"):
        "EnvRestore's restore: only ever a field of StateDirGuard / EnvGuard, which "
        "hold the lock and drop it AFTER this field",
}


def _blank(src: str, keep_strings: bool = False) -> str:
    """``src`` with comments (and, unless ``keep_strings``, string/char-literal
    CONTENTS) replaced, same length and line structure — so offsets map back
    to the original and braces inside literals cannot unbalance the function
    scan. Rule 1 matches the literal ``"PATH"``, so it reads the
    ``keep_strings`` form."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", src[i:j]))
            i = j
            continue
        if c == "r" and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            m = re.match(r'r(#*)"', src[i:i + 10])
            if m:
                end = '"' + m.group(1)
                start = i + len(m.group(0))
                j = src.find(end, start)
                j = n if j < 0 else j
                body = src[start:j] if keep_strings else re.sub(r"[^\n]", "x", src[start:j])
                out.append(src[i:start] + body + end)
                i = j + len(end)
                continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            body = src[i + 1:j] if keep_strings else re.sub(r"[^\n]", "x", src[i + 1:j])
            out.append('"' + body + '"')
            i = j + 1
            continue
        if c == "'":
            m = re.match(r"'(?:\\.|[^\\'])'", src[i:i + 6]) or re.match(
                r"'\\u\{[0-9a-fA-F]+\}'", src[i:i + 12]
            )
            if m:
                out.append(m.group(0) if keep_strings else "'x'" + " " * (len(m.group(0)) - 3))
                i += len(m.group(0))
                continue
        out.append(c)
        i += 1
    return "".join(out)


class Fn(NamedTuple):
    name: str
    start: int  # offset of `fn`
    body: int  # offset of the body's `{`
    end: int  # offset of the matching `}`


def _functions(blanked: str) -> list[Fn]:
    closing: dict[int, int] = {}
    stack: list[int] = []
    for i, ch in enumerate(blanked):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            closing[stack.pop()] = i
    found: list[Fn] = []
    for m in re.finditer(r"\bfn\s+(\w+)", blanked):
        brace = blanked.find("{", m.end())
        semi = blanked.find(";", m.end())
        if brace < 0 or 0 <= semi < brace:
            continue
        found.append(Fn(m.group(1), m.start(), brace, closing.get(brace, len(blanked))))
    return found


def _enclosing(fns: list[Fn], offset: int) -> Fn | None:
    around = [f for f in fns if f.body < offset < f.end]
    return min(around, key=lambda f: f.end - f.start) if around else None


def _drop_holds_the_lock(blanked: str, fn: Fn) -> bool:
    impl = None
    for m in re.finditer(r"\bimpl\s+Drop\s+for\s+(\w+)", blanked[: fn.start]):
        impl = m
    if impl is None:
        return False
    struct = re.search(r"\bstruct\s+" + re.escape(impl.group(1)) + r"\b[^{;]*\{", blanked)
    if struct is None:
        return False
    close = blanked.find("}", struct.end())
    return bool(_LOCK_TYPES.search(blanked[struct.end():close]))


class Finding(NamedTuple):
    rel: str
    line: int
    rule: str


def scan_source(rel: str, src: str) -> Iterator[Finding]:
    """Every rule-1 / rule-2 finding in one Rust file's text."""
    blanked = _blank(src)
    literal = _blank(src, keep_strings=True)
    fns = _functions(blanked)
    # A same-file helper that RETURNS the proof (`fn scratch() ->
    # StateDirGuard`, `fn isolate() -> (MutexGuard<..>, StateDirGuard)`)
    # takes the lock for its caller.
    helpers = sorted(
        fn.name for fn in fns
        if "->" in (sig := blanked[fn.start:fn.body]) and _LOCK_TYPES.search(sig.split("->", 1)[1])
    )
    helper_call = re.compile(r"\b(?:" + "|".join(map(re.escape, helpers)) + r")\s*\(") if helpers else None

    def line_of(offset: int) -> int:
        return src.count("\n", 0, offset) + 1

    def exempt(fn: Fn | None, table: dict[tuple[str, str], str]) -> bool:
        return fn is not None and (rel, fn.name) in table

    for m in _PATH_DIRECT.finditer(literal):
        if not exempt(_enclosing(fns, m.start()), PATH_ALLOWED):
            yield Finding(rel, line_of(m.start()), "sets the process PATH")
    for m in _PATH_PAIR.finditer(literal):
        yield Finding(rel, line_of(m.start()), "hands a helper a PATH value to set on the process")
    for m in _PATH_READ.finditer(literal):
        if not exempt(_enclosing(fns, m.start()), PATH_READ_ALLOWED):
            yield Finding(
                rel, line_of(m.start()),
                "walks the process PATH itself — use vct_launcher_core::paths::"
                "which_on_path / lookup_path (the one lookup, injectable per thread)",
            )

    for m in _MUTATION.finditer(blanked):
        fn = _enclosing(fns, m.start())
        if exempt(fn, LOCK_EXEMPT):
            continue
        if fn is None:
            yield Finding(rel, line_of(m.start()), "mutates the environment outside any function")
            continue
        signature = blanked[fn.start:fn.body]
        before = blanked[fn.body:m.start()]
        if _LOCK_TAKEN.search(before) or _LOCK_TYPES.search(signature):
            continue
        if helper_call is not None and helper_call.search(before):
            continue
        if fn.name == "drop" and _drop_holds_the_lock(blanked, fn):
            continue
        yield Finding(
            rel, line_of(m.start()),
            f"`{fn.name}` mutates the environment without holding GLOBAL_ENV_MUTEX",
        )

    for fn in fns:
        signature = blanked[fn.start:fn.body]
        if (fn.name in _EVIDENCE_NAMES and rel != "vct-launcher-core/src/test_env.rs"
                and not (fn.name == "env_lock" and re.search(r"->\s*[\w:]*EnvLock\b", signature))):
            yield Finding(
                rel, line_of(fn.start),
                f"a local `{fn.name}` that is not test_env's shadows it — every "
                f"`{fn.name}(` in this file would read as holding GLOBAL_ENV_MUTEX",
            )
        body = blanked[fn.body:fn.end]
        if re.search(r"\benv_lock\s*\(\s*\)", body) and _GUARD_TAKEN.search(body):
            yield Finding(
                rel, line_of(fn.start),
                f"`{fn.name}` takes env_lock() AND a guard — the mutex is not reentrant",
            )


def _rust_files() -> list[Path]:
    return [
        p for p in RUST_ROOT.rglob("*.rs")
        if "target" not in p.relative_to(RUST_ROOT).parts
    ]


@functools.lru_cache(maxsize=1)
def _findings() -> tuple[Finding, ...]:
    out: list[Finding] = []
    for path in _rust_files():
        rel = path.relative_to(RUST_ROOT).as_posix()
        out.extend(scan_source(rel, path.read_text(encoding="utf-8", errors="replace")))
    return tuple(sorted(out))


def test_no_rust_test_sets_the_process_path() -> None:
    offenders = [f for f in _findings() if "PATH" in f.rule]
    assert not offenders, (
        "A Rust test sets the shared process PATH — inject instead (the child's "
        "own env, an absolute program, a pure function over an explicit PATH, or "
        "vct_launcher_core::paths::with_lookup_path):\n  "
        + "\n  ".join(f"{f.rel}:{f.line}: {f.rule}" for f in offenders)
    )


def test_every_process_env_mutation_holds_the_global_lock() -> None:
    offenders = [f for f in _findings() if "PATH" not in f.rule]
    assert not offenders, (
        "Process-env mutations without vct_launcher_core::test_env::GLOBAL_ENV_MUTEX "
        "(take env_lock() / a guard, or inject the value through a seam instead):\n  "
        + "\n  ".join(f"{f.rel}:{f.line}: {f.rule}" for f in offenders)
    )


def test_the_path_read_exemptions_still_name_real_sites() -> None:
    for (rel, name), why in PATH_READ_ALLOWED.items():
        src = (RUST_ROOT / rel).read_text(encoding="utf-8")
        literal = _blank(src, keep_strings=True)
        fns = _functions(_blank(src))
        sites = [
            m for m in _PATH_READ.finditer(literal)
            if (fn := _enclosing(fns, m.start())) is not None and fn.name == name
        ]
        assert sites, f"{rel}::{name} may read PATH ({why}) but no longer does"


def test_the_exemptions_still_name_real_sites() -> None:
    """An exemption whose function no longer mutates the environment is
    stale — delete it rather than let it cover a future site."""
    for (rel, name), why in LOCK_EXEMPT.items():
        src = (RUST_ROOT / rel).read_text(encoding="utf-8")
        blanked = _blank(src)
        sites = [
            m for m in _MUTATION.finditer(blanked)
            if (fn := _enclosing(_functions(blanked), m.start())) is not None and fn.name == name
        ]
        assert sites, f"{rel}::{name} is exempt ({why}) but mutates nothing"


def test_the_guard_sees_every_shape_it_names() -> None:
    """The guard's own red proof, on synthetic sources."""
    def rules(src: str) -> list[str]:
        return [f.rule for f in scan_source("x.rs", src)]

    for shape in (
        'fn t() { std::env::set_var("PATH", ""); }',
        'fn t() { unsafe { std::env::set_var("PATH", p); } }',
        'fn t() { let _g = env_lock(); std::env::remove_var("PATH"); }',
        'fn t() { env::set_var(\n            "PATH",\n            "/usr/bin",\n        ); }',
        'fn t() { with_env(&[("PATH", Some("/nonexistent-dir"))], || f()); }',
        'fn t() { with_env_vars(&[("PATH", None)], || f()); }',
        'fn t() { let _e = env_guard(&[\n    ("PATH", Some(p.as_str())),\n]); }',
    ):
        assert any("PATH" in r for r in rules(shape)), shape

    for shape in (
        'fn t() { std::env::set_var("HOME", "/x"); }',
        'fn t() { let _g = SERIALIZE.lock().unwrap(); std::env::remove_var("VCT_HUB_BIN"); }',
        'fn helper(k: &str) { std::env::set_var(k, "v"); }',
        'struct R; impl Drop for R { fn drop(&mut self) { std::env::remove_var("A"); } }',
        'fn t() { std::env::set_var("A", "1"); let _g = env_lock(); }',
        # A LOCAL struct named EnvGuard proves nothing (it held a keychain lock).
        'struct EnvGuard { _l: KeychainGuard } impl Drop for EnvGuard { fn drop(&mut self) { std::env::remove_var("A"); } }',
        'fn setup() -> (PathBuf, EnvGuard) { std::env::set_var("A", "1"); }',
        'fn scratch() -> test_env::StateDirGuard { state_dir_guard() }\n'
        'fn t() { std::env::set_var("B", "1"); let _g = scratch(); }',
    ):
        assert any("without holding" in r for r in rules(shape)), shape
    assert any("not reentrant" in r for r in rules(
        'fn t() { let _l = env_lock(); let _g = env_guard(&[("A", None)]); }'
    ))
    assert any("shadows" in r for r in rules(
        'fn env_lock() -> &\'static Mutex<()> { &LOCK }\n'
        'fn t() { let _g = env_lock().lock(); std::env::set_var("A", "1"); }'
    ))
    assert not any("shadows" in r for r in rules(
        'fn env_lock() -> crate::test_env::EnvLock { crate::test_env::env_lock() }'
    ))
    assert any("shadows" in r for r in rules(
        'fn with_state_dir<F: FnOnce()>(f: F) { let _g = LOCAL.lock(); f() }'
    ))

    assert any("walks the process PATH" in r for r in rules(
        'fn which_runtime() { let p = std::env::var_os("PATH"); }'
    ))
    assert any("walks the process PATH" in r for r in rules(
        'fn which_simple() { let p = std::env::var("PATH")?; }'
    ))
    assert [f.rule for f in scan_source(
        "vct-launcher-core/src/paths.rs", 'fn lookup_path() { std::env::var_os("PATH") }'
    )] == [], "the one lookup home may read it"

    for fine in (
        'fn t() { Command::new("code").env("PATH", "").spawn(); }',
        'fn t() { with_lookup_path(Some(OsStr::new("")), || f()); }',
        'fn t() { let _l = env_lock(); std::env::set_var("PATHS", "x"); }',
        'fn t() { let _p = std::env::var_os("PATHEXT"); }',
        'fn t() { let _l = env_lock(); std::env::set_var("HOME", "/x"); }',
        'fn t() { let _e = env_guard(&[("HOME", Some("/x"))]); std::env::set_var("B", "1"); }',
        'fn t() { with_state_dir(|r| { std::env::set_var("B", "1"); }); }',
        'fn set(_held: &EnvLock, v: &str) { std::env::set_var("LVL", v); }',
        'fn scratch() -> test_env::StateDirGuard { state_dir_guard() }\n'
        'fn t() { let _g = scratch(); std::env::set_var("B", "1"); }',
        'struct G { _l: EnvLock } impl Drop for G { fn drop(&mut self) { std::env::remove_var("A"); } }',
        '// std::env::set_var("HOME", "/x");\nfn t() {}',
        'fn t() { let s = "std::env::set_var(\\"HOME\\", 1)"; }',
    ):
        assert rules(fine) == [], (fine, rules(fine))
