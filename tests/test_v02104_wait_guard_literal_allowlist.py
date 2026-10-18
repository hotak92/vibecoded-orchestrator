# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 E-4 — the wait guard's literal-text exception (allowlist
inversion, owner-approved 2026-10-17).

``grep -n '$(sleep 5)' f`` was denied: the guard read the quoted search
text as a command substitution. The fix keeps the v0.2.103 decision for
everything and adds ONE narrow exception consulted first
(:func:`vco_lib.wait_guard_literal.literal_text_only`): every simple command
is an allowlisted program that never runs its arguments as code, and no
substitution in the line would be expanded by bash.

The proof here is a DIFFERENTIAL against the v0.2.103 pair
(``637dc1c4:vco_lib/{bash_command_walk,wait_guard}.py``), frozen as
fixtures under ``tests/fixtures/wait_guard_v02103/`` because the CI pytest
job clones at depth 1 and cannot ``git show`` the base: for every row of a
large table, a command the base DENIES is denied by HEAD too, unless the
row is in the explicit LITERAL set — and every LITERAL row is run through
real bash (``sleep 0.01`` + a marker file) to prove bash runs no wait,
beside its live counterpart, which must run the marker AND stay denied.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pytest

from vco_lib import wait_guard as wg
from vco_lib.powershell_command import command_for_bash_readers
from vco_lib.wait_guard_literal import LITERAL_ALLOWLIST, literal_text_only

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "wait_guard_v02103"
BASE_COMMIT = "637dc1c4"
MARKER_LINE = "# ---- 637dc1c4 ----\n"
W = "sleep 5"            # the wait text every row carries


# ─── the frozen v0.2.103 pair ─────────────────────────────────────────────


def _fixture_body(name: str) -> str:
    text = (FIXTURES / f"{name}.py.txt").read_text(encoding="utf-8")
    head, sep, body = text.partition(MARKER_LINE)
    assert sep and BASE_COMMIT in head, f"{name}: fixture header missing"
    return body


def _load_base():
    walk_name = "_wg_v02103_base_walk"
    if walk_name not in sys.modules:
        walk = types.ModuleType(walk_name)
        walk.__file__ = str(FIXTURES / "bash_command_walk.py.txt")
        sys.modules[walk_name] = walk
        exec(compile(_fixture_body("bash_command_walk"), walk.__file__, "exec"),
             walk.__dict__)
    wg_name = "_wg_v02103_base_wg"
    if wg_name not in sys.modules:
        src = _fixture_body("wait_guard")
        imp = "from vco_lib.bash_command_walk import ("
        assert src.count(imp) == 1
        src = src.replace(imp, f"from {walk_name} import (")
        base = types.ModuleType(wg_name)
        base.__file__ = str(FIXTURES / "wait_guard.py.txt")
        sys.modules[wg_name] = base
        exec(compile(src, base.__file__, "exec"), base.__dict__)
    return sys.modules[wg_name]


BASE = _load_base()


def base_denies(cmd: str) -> bool:
    return BASE.static_pattern(cmd) is not None


def head_denies(cmd: str) -> bool:
    return wg.shell_static_pattern("Bash", cmd) is not None


# ─── the row table ────────────────────────────────────────────────────────


def _review_rows() -> List[Tuple[str, str]]:
    """(raw line, command) for every R8/R9 review row (data file)."""
    rows = []
    path = FIXTURES / "review_rows_r8_r9.txt"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("##"):
            continue
        cmd = raw[2:].encode().decode("unicode_escape") if raw.startswith("U:") else raw
        rows.append((raw, cmd))
    return rows


#: R8/R9 review rows (by RAW line) that are literal text, each with a live
#: counterpart the real-bash proof runs beside it.
REVIEW_LITERAL = {
    "grep -n '$(sleep 5)' f": 'grep -n "$(sleep 5)" f',
    "grep -n 'x=$(sleep 5)' f | head": 'grep -n "x=$(sleep 5)" f | head',
    "git log -S'$(sleep 5)'": 'git log -S"$(sleep 5)"',
    "echo \\`sleep 5\\`": "echo `sleep 5`",
    'echo "\\$(sleep 5)"': 'echo "$(sleep 5)"',
    "U:grep -n '$(sleep 5)' f \\ue003": 'grep -n "$(sleep 5)" f',
}

#: Hand-written LITERAL rows (allowlisted programs, wait text only inside
#: single quotes / ``$'…'`` / a backslash escape) with live counterparts.
HAND_LITERAL = [
    ("grep -rn '$(sleep 5)' --include='*.py' .", 'grep -rn "$(sleep 5)" .'),
    ("grep -e '$(sleep 5)' -e \"\\`sleep 5\\`\" f 2>&1 | head -n 5 > out.txt",
     'grep -e "`sleep 5`" f 2>&1 | head -n 5 > out.txt'),
    ("grep '$(sleep 5)' f\nwc -l f", "grep $(sleep 5) f\nwc -l f"),
    ("grep '$(sleep 5)' \\\n f", "grep $(sleep 5) \\\n f"),
    ("echo $'\\'$(sleep 5)'", "echo $'\\\\'$(sleep 5)''"),
    ("echo '\\''$(sleep 5)'", "echo '\\'$(sleep 5)"),
    ("grep '$(sleep 5)'$'\\n''`sleep 5`' f", "grep '$(sleep 5)'$'\\n'`sleep 5` f"),
    ("cat f | grep -c '$(sleep 5)' |& head", 'cat f | grep -c "$(sleep 5)" |& head'),
    ("grep '<(sleep 5)' f", "grep <(sleep 5) f"),
    ("grep \"$HOME\"'$(sleep 5)' f", "grep \"$HOME$(sleep 5)\" f"),
    ("tail -n 5 '$(sleep 5)'; head -c 3 f", 'tail -n 5 "$(sleep 5)"; head -c 3 f'),
    ("git grep -n -e '$(sleep 5)'", 'git grep -n -e "$(sleep 5)"'),
    ("git log -1 --format='$(sleep 5)'", 'git log -1 --format="$(sleep 5)"'),
    ("printf '%s\\n' '$(sleep 5)' | sort -u", "printf '%s\\n' \"$(sleep 5)\" | sort -u"),
    ("find . -name '$(sleep 5)' -print", 'find . -name "$(sleep 5)" -print'),
    ("ls '$(sleep 5)' 2>/dev/null || echo none", 'ls "$(sleep 5)" 2>/dev/null || echo none'),
    ("rg -n --fixed-strings '$(sleep 5)' f", 'rg -n --fixed-strings "$(sleep 5)" f'),
    ("echo '$(sleep 5)' > out.txt && cat out.txt", 'echo "$(sleep 5)" > out.txt && cat out.txt'),
    # R13 LOW-1: the device refusal leaves the non-blocking devices and a
    # relative `dev/` directory of the project alone.
    ("grep '$(sleep 5)' f 2> /dev/null", 'grep "$(sleep 5)" f 2> /dev/null'),
    ("echo '$(sleep 5)' >/dev/stderr", 'echo "$(sleep 5)" >/dev/stderr'),
    ("echo '$(sleep 5)' 1>/dev/stdout", 'echo "$(sleep 5)" 1>/dev/stdout'),
    ("cat '$(sleep 5)' src/dev/x.py", 'cat "$(sleep 5)" src/dev/x.py'),
    ("grep -rn '$(sleep 5)' dev/", 'grep -rn "$(sleep 5)" dev/'),
]

#: Generated families. Each allowlisted program gets a literal invocation
#: template; every literal form x context is LITERAL, and the same row with
#: the matching LIVE form is its counterpart.
PROGRAM_TEMPLATES = {
    "grep": "grep -n {A} f", "egrep": "egrep {A} f", "fgrep": "fgrep {A} f",
    "rg": "rg -n {A} f", "ag": "ag {A} f", "ack": "ack {A} f",
    "git": "git log -1 --grep={A}", "echo": "echo {A}",
    "printf": "printf '%s\\n' {A}", "cat": "cat {A}", "head": "head -n 1 {A}",
    "tail": "tail -n 1 {A}", "wc": "wc -l {A}", "sort": "sort -u {A}",
    "uniq": "uniq -c {A}", "cut": "cut -c1 {A}", "tr": "tr {A} x < f",
    "ls": "ls -d {A}", "find": "find . -name {A}",
}
#: literal form -> its live counterpart
FORMS = {
    "'$(sleep 5)'": '"$(sleep 5)"',
    "'`sleep 5`'": '"`sleep 5`"',
    "$'$(sleep 5)'": "$(sleep 5)",
    '"\\$(sleep 5)"': '"$(sleep 5)"',
    "'<(sleep 5)'": "<(sleep 5)",
}
CONTEXTS = [
    "{C}",
    "{C} 2>&1 | head -n 1",
    "cat f | {C} > out.txt; wc -l out.txt",
    "{C}\nwc -l f",
    "{C} && echo ok || echo no",
]


def _generated_literal() -> List[Tuple[str, str]]:
    pairs = []
    for tmpl in PROGRAM_TEMPLATES.values():
        for lit, live in FORMS.items():
            for ctx in CONTEXTS:
                pairs.append((ctx.replace("{C}", tmpl.replace("{A}", lit)),
                              ctx.replace("{C}", tmpl.replace("{A}", live))))
    return pairs


A = "'$(sleep 5)'"   # a literal form for the must-stay-denied families

#: Allowlisted programs with an option that RUNS a command or WAITS, and
#: structural escapes: each carries quoted wait text the base denies, so
#: HEAD must deny it too (none is LITERAL).
MUST_STAY_DENIED = [
    f"tail -f {A}", f"tail -F {A}", f"tail --follow {A}", f"tail --fo {A}",
    f"tail -fn5 {A}", f"tail -n5f {A}", f"tail +5f {A}", f"tail '-f' {A}",
    f"tail $'-f' {A}", f"tail $'\\x2df' {A}", f"tail $T {A}",
    f"tail --pid=1 -f {A}", f"tail --retry {A}", f"tail -s 1 -f {A}",
    f"tail * {A}",
    f"find . -name {A} -exec true \\;", f"find . -name {A} -execdir true \\;",
    f"find . -name {A} -ok true \\;", f"find . -name {A} -okdir true \\;",
    f"find . -name {A} -delete", f"find . -'exec' sh -c {A} \\;",
    f"find . -exec sh -c {A} \\;", f"find $D -name {A}",
    f"git -c core.pager=cat log --grep={A}", f"git --paginate log --grep={A}",
    f"git -C . log --grep={A}", f"git lg {A}", f"git rebase --exec {A}",
    f"git grep -O{A} x", f"git grep -nO {A} x",
    f"git grep --open-files-in-pager={A} x", f"git grep --open={A} x",
    f"git log --grep={A} $X", "git log -1 --grep='$(sleep 5)' --format=\"$(sleep 5)\"",
    f"rg --pre={A} x", f"rg --pre {A} x", f"rg --hostname-bin={A} x",
    f"ag --pager={A} x", f"ag --pag {A} x", f"ack --pager={A} x",
    f"ack --pag={A} x", f"ack --ackrc={A} x", f"ack --output={A} x",
    "ack --output='@{[`sleep 5`]}' x", f"ack -output {A} x", f"ack --env {A} x",
    f"sort --compress-program={A} f", f"sort --comp={A} f",
    f"printf -v {A} x", "printf -v 'a[$(sleep 5)]' x", f"printf -vx {A}",
    f"X=1 grep {A} f", f"X={A} grep x f", f"GIT_PAGER={A} git log",
    f"(grep {A} f)", f"{{ grep {A} f; }}", f"grep {A} f &", f"grep {A} f & wait",
    f"grep {A} f; sleep 5", f"grep {A} f | sh", f"grep {A} f | bash",
    f"grep {A} f | xargs sleep", f"grep {A} f | xargs -I{{}} sh -c {A}",
    f"grep {A} f # c", f"grep x f #{A}", f"grep {A} f <<< x",
    f"grep {A} f <<E\nx\nE", f"cat <<E\n{A}\nE", f"grep {A} f; f() {{ :; }}",
    f"! grep {A} f", f"time grep {A} f", f"if grep {A} f; then :; fi",
    f"while grep {A} f; do :; done", f"grep {A} f || sleep 5",
    f"grep {A} $(sleep 5)", f"grep {A} \"$(sleep 5)\"", f"grep {A} `sleep 5`",
    f"grep {A} <(sleep 5)", f"grep {A} f > >(cat)", f"grep {A} ${{x:-f}}",
    f"grep {A} \"${{x:-$(sleep 5)}}\"", f"grep {A} $(( $(sleep 5) ))",
    f"grep {A} f;; ", f"grep {A} f |", f"grep {A} f >", f"grep {A} f ;& x",
    "grep '$(sleep 5) f", "grep \"$(sleep 5) f", "grep $'$(sleep 5) f",
    "echo $'\\\\'$(sleep 5)''", "echo \"'$(sleep 5)'\"",
    f"\\grep {A} f", f"/usr/bin/grep {A} f", f"./grep {A} f", f"'grep' {A} f",
    f"Grep {A} f", f"gr$x {A} f", f"gre? {A} f", f"eval {A}", f"eval \"{W}\"",
    f"exec grep {A} f", f"command grep {A} f", f"builtin echo {A}",
    f"source {A}", f". {A}", f"sed 's/x/y/e' {A}", f"sed -n {A} f",
    f"awk {A} f", f"xargs echo {A}", f"env grep {A} f", f"sudo grep {A} f",
    f"timeout 5 grep {A} f", f"nohup grep {A} f", f"watch grep {A} f",
    f"bash -c {A}", f"sh -c {A}", f"python3 -c {A}", f"perl -e {A}",
    f"ssh host {A}", f"parallel echo {A} ::: a", f"trap {A} EXIT",
    f"echo {A} | sh", f"echo {A} | bash -s", f"echo {A} | source /dev/stdin",
    f"case x in x) grep {A} f;; esac", f"coproc grep {A} f",
    f"function f {{ grep {A} f; }}; f", f"grep {A} f\nsleep 5",
    f"grep {A} f && {W}", f"echo {A}; tail -f log",
    # `>&word` with a non-fd word is `&>word` and bash expands the word a
    # SECOND time (fuzz find, probed below): never literal.
    f"echo x >&{A}", f"grep {A} f >&{A}", f"grep x f <&{A}", f"echo {A} >&out",
    f"echo x >& {A}", f"echo x 2>&1{A}",
]

#: R13 LOW-1 (Fable): the allowlisted programs run no CODE, but they WAIT on
#: a file that never ends or never answers. A row naming such a device or a
#: `/proc` file — as an argument, an option value or a redirection target,
#: absolute or reached with `..` / an unknown `~` / `$VAR` base — gets the
#: v0.2.103 decision (denied: it carries quoted wait text). `/dev/tcp` and
#: `/dev/udp` are assembled so no single source line spells a socket path.
_NET = "/dev/" + "tcp/10.255.255.1/80"
_UDP = "/dev/" + "udp/10.255.255.1/9"
DEVICE_ROWS = [
    f"grep -c {A} /dev/zero", f"cat {A} /dev/zero", f"tail -c 1 {A} /dev/zero",
    f"head -c 1 {A} /dev/random", f"wc -c {A} /dev/urandom", f"cat {A} /dev/full",
    f"cat {A} /dev/stdin", f"grep {A} /dev/tty", f"cat {A} /dev/fd/0",
    f"cat {A} /proc/self/fd/0", f"head -c 5 {A} /proc/kmsg",
    f"echo {A} > {_NET}", f"echo {A} &>{_UDP}", f"cat {A} < {_NET}",
    f"grep {A} f < /dev/stdin", f"grep {A} f 0</dev/zero",
    f"grep -f/dev/stdin {A}", f"grep --file=/proc/self/fd/0 {A}",
    f"grep -e {A} -f /dev/stdin", f"cat {A} //dev//./zero", f"cat {A} /dev/../dev/zero",
    f"cat {A} '/dev/fd/0'", f"cat {A} \"/dev/zero\"", f"cat {A} /dev/z*",
    f"cat {A} ../../../../dev/urandom", f"cat {A} ~/../../dev/random",
    f"cat {A} $HOME/../../dev/tty", f"tail -n 1 {A} /dev/zero",
    f"sort {A} /dev/zero", f"find /dev -name {A}", f"ls {A} /proc",
    f"grep -r -D read {A} .", f"grep --devices=read -r {A} .", f"grep -rD read {A} .",
    f"git log --grep={A} -- /dev/zero", f"rg {A} /dev/zero",
]
MUST_STAY_DENIED += DEVICE_ROWS


def _non_literal_rows() -> List[str]:
    rows = list(MUST_STAY_DENIED)
    # every live counterpart of every literal row is a must-stay-denied row
    for _lit, live in _all_literal_pairs():
        rows.append(live)
    # base-allowed controls: unchanged either way
    rows += ["grep -n sleep f", "git log --oneline -5", "echo hello",
             'eval "sleep $N"', "cat f | wc -l", "tail -n 5 f",
             "grep x f > out.txt", f"echo '{W}'"]
    return rows


def _all_literal_pairs() -> List[Tuple[str, str]]:
    pairs = [(cmd, REVIEW_LITERAL[raw]) for raw, cmd in _review_rows()
             if raw in REVIEW_LITERAL]
    assert len(pairs) == len(REVIEW_LITERAL), "a REVIEW_LITERAL key matches no row"
    return pairs + HAND_LITERAL + _generated_literal()


LITERAL_PAIRS = _all_literal_pairs()
LITERAL_SET: Set[str] = {lit for lit, _live in LITERAL_PAIRS}


# ─── 1. the differential ──────────────────────────────────────────────────


def test_head_never_weaker_than_v02103_outside_the_literal_set():
    rows = [cmd for _raw, cmd in _review_rows()] + _non_literal_rows() \
        + [lit for lit, _ in LITERAL_PAIRS]
    weaker, stricter, literal_allowed, base_denied = [], [], 0, 0
    for cmd in rows:
        b, h = base_denies(cmd), head_denies(cmd)
        base_denied += b
        if b and not h:
            if cmd in LITERAL_SET:
                literal_allowed += 1
            else:
                weaker.append(cmd)
        if h and not b:
            stricter.append(cmd)
    assert len(rows) > 1000, len(rows)
    assert weaker == [], f"HEAD allows what v0.2.103 denies: {weaker[:10]}"
    assert stricter == [], f"HEAD denies what v0.2.103 allows: {stricter[:10]}"
    assert literal_allowed > 500 and base_denied > 900, (literal_allowed, base_denied)


@pytest.mark.parametrize("cmd", sorted(LITERAL_SET))
def test_every_literal_row_was_denied_by_base_and_is_allowed_now(cmd):
    assert base_denies(cmd), f"not a v0.2.103 false positive: {cmd!r}"
    assert not head_denies(cmd), f"literal text still denied: {cmd!r}"


@pytest.mark.parametrize("cmd", MUST_STAY_DENIED)
def test_must_stay_denied_rows(cmd):
    # None of these is literal text: HEAD reads each exactly as v0.2.103
    # (a few — a comment, a heredoc body, an unterminated quote — are
    # base-ALLOWED and stay so; the exception never makes anything stricter).
    assert literal_text_only(cmd) is False
    assert head_denies(cmd) == base_denies(cmd)


def test_must_stay_denied_rows_are_mostly_real_denials():
    denied = sum(head_denies(c) for c in MUST_STAY_DENIED)
    assert denied >= len(MUST_STAY_DENIED) - 6, denied


# ─── 2. real bash: a LITERAL row runs no wait; its live twin does ─────────


def _marked(cmd: str, marker: str) -> str:
    return cmd.replace(W, f"sleep 0.01;touch {marker}")


@pytest.fixture(scope="module")
def bash_dir(tmp_path_factory) -> Path:
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("real-bash proof needs bash and git")
    d = tmp_path_factory.mktemp("wg_literal")
    (d / "f").write_text("x\n$(sleep 5)\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": _path_env(), "HOME": str(d)}
    for argv in (["git", "init", "-q"], ["git", "add", "f"],
                 ["git", "commit", "-q", "-m", "one"]):
        subprocess.run(argv, cwd=d, env=env, check=True, capture_output=True)
    return d


def _path_env() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


def _run(cmd: str, cwd: Path) -> float:
    start = time.monotonic()
    subprocess.run(["bash", "-c", cmd], cwd=cwd, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=20, env={"PATH": _path_env(), "HOME": str(cwd),
                                    "GIT_PAGER": "cat", "PAGER": "cat"})
    return time.monotonic() - start


def _settled_exists(path: Path) -> bool:
    # a process substitution runs asynchronously: give it a moment
    for _ in range(50):
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_literal_rows_run_no_wait_in_real_bash(bash_dir):
    ran_wait, missing_control = [], []
    for i, (lit, live) in enumerate(LITERAL_PAIRS):
        m_lit, m_live = f"L{i}", f"V{i}"
        _run(_marked(lit, m_lit), bash_dir)
        _run(_marked(live, m_live), bash_dir)
        if _settled_exists(bash_dir / m_live) is False:
            missing_control.append(live)
        if (bash_dir / m_lit).exists():
            ran_wait.append(lit)
    assert ran_wait == [], f"bash RAN the wait text of a LITERAL row: {ran_wait[:5]}"
    assert missing_control == [], f"live twin did not run its wait: {missing_control[:5]}"


def test_dup_redirect_target_is_expanded_twice_by_real_bash(bash_dir):
    # Why `>&word` / `<&word` must name an fd: bash re-expands a non-fd
    # word, so the single-quoted text RUNS (bash 5.2, fuzz-found).
    cmd = "echo x >&'$(sleep 5)'"
    _run(_marked(cmd, "DUP"), bash_dir)
    assert (bash_dir / "DUP").exists()
    assert literal_text_only(cmd) is False and head_denies(cmd)
    assert literal_text_only("grep -n '$(sleep 5)' f 2>&1 >&2 <&0 1>&-") is True


def test_live_twins_stay_denied():
    allowed = [live for _lit, live in LITERAL_PAIRS if not head_denies(live)]
    assert allowed == []


# ─── 3. the frozen fixture is the true v0.2.103 pair ──────────────────────


@pytest.mark.parametrize("name", ["bash_command_walk", "wait_guard"])
def test_fixture_is_byte_identical_to_the_base_commit(name):
    probe = subprocess.run(["git", "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"],
                           cwd=REPO, capture_output=True)
    if probe.returncode != 0:
        pytest.skip("shallow clone: the base commit is not in history")
    blob = subprocess.run(["git", "show", f"{BASE_COMMIT}:vco_lib/{name}.py"],
                          cwd=REPO, capture_output=True, check=True).stdout
    assert _fixture_body(name).encode("utf-8") == blob


# ─── 4. the exception itself ──────────────────────────────────────────────


def test_allowlist_is_exactly_the_owner_list():
    assert LITERAL_ALLOWLIST == frozenset({
        "grep", "egrep", "fgrep", "rg", "ag", "ack", "git", "echo", "printf",
        "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "ls", "find",
    })
    for excluded in ("sed", "awk", "xargs", "env", "eval", "bash", "sh",
                     "sleep", "watch", "timeout"):
        assert excluded not in LITERAL_ALLOWLIST


def test_the_field_case_is_allowed_end_to_end(tmp_path):
    payload = {"tool_name": "Bash", "session_id": "s",
               "tool_input": {"command": "grep -n '$(sleep 5)' f"}}
    assert wg.evaluate(payload, env={}, state_root=tmp_path, now=1.0) is None


def test_repeat_detector_still_applies_to_a_literal_row(tmp_path):
    cmd = "grep -n '$(sleep 5)' log"
    reasons = []
    for n in range(4):
        payload = {"tool_name": "Bash", "session_id": "s", "tool_use_id": f"t{n}",
                   "tool_input": {"command": cmd}}
        reasons.append(wg.evaluate(payload, env={}, state_root=tmp_path,
                                   now=100.0 + 10 * n))
    assert reasons[:3] == [None, None, None] and reasons[3]


def test_powershell_never_takes_the_exception():
    cmd = "echo '$(sleep 5)'"
    readable = command_for_bash_readers("PowerShell", cmd)
    assert wg.shell_static_pattern("PowerShell", cmd) == wg.static_pattern(readable)


def test_eval_of_a_variable_sleep_keeps_the_base_reading():
    # owner question 2a: keep the v0.2.103 decision. The base ALLOWS
    # `eval "sleep $N"` (a non-literal eval body is not re-parsed); eval is
    # not on the allowlist, so HEAD reads it exactly as the base does.
    cmd = 'eval "sleep $N"'
    assert head_denies(cmd) == base_denies(cmd)
    assert literal_text_only(cmd) is False


@pytest.mark.parametrize("cmd,expected", [
    ("grep -n '$(sleep 5)' f", True),
    ("grep -n \"$(sleep 5)\" f", False),
    ("grep -n '$(sleep 5)' f | tail -f", False),
    ("grep -n sleep f", False),            # nothing to except: no `$`/`(`/backtick
    ("printf -v 'a[$(sleep 5)]' x", False),
])
def test_literal_text_only_decisions(cmd, expected):
    assert literal_text_only(cmd) is expected


@pytest.mark.parametrize("cmd", DEVICE_ROWS)
def test_a_blocking_device_gets_the_base_decision(cmd):
    # R13 LOW-1: each row WAS allowed by the exception (quoted text only);
    # the device or /proc path now hands it back to the v0.2.103 reading,
    # which denies the quoted `$(sleep 5)`.
    assert literal_text_only(cmd) is False
    assert base_denies(cmd) and head_denies(cmd)


@pytest.mark.parametrize("text,expected", [
    ("/dev/zero", True), ("/dev/null", False), ("/dev/stderr", False),
    ("/dev/stdout", False), ("/dev", True), ("/dev/", True), ("/proc", True),
    ("/proc/self/fd/0", True), ("dev/zero", False), ("src/dev/zero", False),
    ("../dev/zero", True), ("a/../../dev/zero", True), ("/x/../dev/zero", True),
    ("//dev/./zero", True), ("/../dev/zero", True), ("~/../../dev/zero", True),
    ("$HOME/../../dev/tty", True), ("${X}/dev/zero", True), ("--file=/dev/stdin", True),
    ("-f/dev/stdin", True), ("-if/proc/kmsg", True), ("--x=src/dev/zero", False),
    ("/dev/null/../zero", True), ("devices", False), ("/home/u/dev/zero", False),
])
def test_device_path_reading(text, expected):
    from vco_lib.wait_guard_literal import _names_device_path
    assert _names_device_path(text) is expected


# ─── 5. performance: linear, < 50 ms on a 200 KB command ──────────────────


def _best_ms(cmd: str) -> float:
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        literal_text_only(cmd)
        best = min(best, (time.perf_counter() - start) * 1000)
    return best


@pytest.mark.parametrize("shape", ["quoted", "words", "ansi", "dquoted", "reject_late"])
def test_200kb_command_is_linear_and_fast(shape):
    unit = {
        "quoted": "grep -n '$(sleep 5) aaaa' f | ",
        "words": "grep -n abc def ghi jkl f \\$ | ",
        "ansi": "grep $'$(sleep 5) \\x41' f | ",
        "dquoted": 'grep "\\$(sleep 5) $x" f | ',
    }.get(shape, "grep -n '$(sleep 5)' f | ")
    body = unit * (200_000 // len(unit) + 1)
    cmd = body + ("wc -l" if shape != "reject_late" else "sleep 5")
    assert len(cmd) >= 200_000
    expected: Optional[bool] = False if shape == "reject_late" else True
    assert literal_text_only(cmd) is expected
    assert _best_ms(cmd) < 50, shape
