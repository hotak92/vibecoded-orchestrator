"""v0.2.104 E-4: the wait guard no longer denies a one-off grep whose PATTERN
looks like a substitution — and (R7 N1) still denies every substitution bash
really runs.

Finding: the walker's substitution scan (``bash_command_walk.
substitution_bodies``) runs on tokens AFTER ``shlex`` has removed the quotes,
so ``grep -n '$(sleep 5)' f`` -- a literal search pattern in single quotes,
the natural thing to grep for while working on the guard or a shell script --
read exactly like ``echo $(sleep 5)`` and was denied as a sole foreground
wait. Bash runs a substitution only outside single quotes (and ``$'...'``,
and not when escaped); inside double quotes ``$(...)`` and backticks still
run, process substitution does not.

R7 N1: the first fix compared NORMALISED TEXT (a set of live body strings
against the token-side body) and dropped every live body whose text differed
from its token shape -- ``$(sleep '5')``, ``$(sleep $'5')``, ``$(sleep \\5)``,
an ``eval`` argument behind ``timeout`` / ``command`` / ``builtin``, the outer
body of ``$(eval '...')``. The liveness decision is now made AT THE
CHARACTER: the one quote-aware scanner swaps each quoted / escaped opener for
a placeholder before tokenisation (``tokenize(..., literal_openers=True)``)
and the walker restores it on every token it emits or re-parses, so emitted
tokens are byte-identical to the plain walk and no text comparison exists any
more.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import wait_guard as wg
from vco_lib.bash_command_walk import (
    WRAPPER_VERBS,
    peel_wrapper_verbs,
    restore_literal_openers,
    tokenize,
    walk_segments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "wait-guard.sh"
HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "wait-guard.ps1"

#: The walker as it stood before E-4 (v0.2.104 R5 merge): no quote reading,
#: every substitution body walked. The differential test below re-loads it.
PRE_E4_WALKER_COMMIT = "637dc1c4"

LITERAL = [
    "grep -n '$(sleep 5)' wait_guard.py",
    "grep -n '`sleep 5`' notes.md",
    "grep -rn 'diff <(sleep 5)' tests",
    'grep -n "<(sleep 5)" tests/x.py',
    'grep -n "\\$(sleep 5)" x.sh',
    "grep -n $'$(sleep 5)' x.sh",
    "rg -n 'x=$(sleep 5)' .",
    # R7 N1 siblings: escaped word, a loop in the pattern, two greps, sed,
    # an echo of the literal text, an escaped `$` outside quotes (a syntax
    # error in bash — no wait runs).
    "grep -n \\$\\(sleep\\ 5\\) f",
    "grep -n 'while true; do $(sleep 5); done' f",
    "grep -n '$(sleep 5)' f && grep -n '`sleep 5`' g",
    "sed -n '/\\$(sleep 5)/p' f",
    "echo '$(sleep 5)'",
    "echo \\$(sleep 5)",
]
LIVE = [
    "echo $(sleep 5)",
    'echo "$(sleep 5)"',
    "echo `sleep 5`",
    "diff <(sleep 5) f",
    "x=$(sleep 5)",
    'grep -n "$(sleep 5)" x',
    "grep -n '$(literal)' x $(sleep 5)",
    # R6 F2: the quote reading holds only at depth 0. A `-c` body is
    # re-parsed by a nested shell (its `\$` re-expands there) and `eval`
    # re-parses its arguments, so their substitutions are live.
    'bash -c "x=\\$(sleep 5)"',
    'sh -c "echo \\$(sleep 5)"',
    "eval 'x=$(sleep 5)'",
    'eval "x=\\$(sleep 5)"',
    "eval 'echo $(sleep 5)'",
    # R7 N1: quoted / escaped text INSIDE a live body is still a live body.
    "echo $(sleep $'5')",
    "echo $(sleep '5')",
    "echo $(sleep \\5)",
    'echo $(sleep "5")',
    "echo \"$(sleep '5')\"",
    "x=$(sleep '5')",
    "x=$(sleep $'5')",
    "echo `sleep '5'`",
    "diff <(sleep '5') f",
    "echo $(sleep 5 '#')",
    "grep -n '$(literal)' x $(sleep '5')",
    "x=1 echo $(sleep '5')",
    "true; echo $(sleep '5')",
    "(echo $(sleep '5'))",
    "{ echo $(sleep '5'); }",
    "f() { echo $(sleep '5'); }; f",
    # R7 N1: a shell or eval behind a verb the walker did not peel, or
    # behind one the wait guard scans through itself.
    'timeout 60 bash -c "x=\\$(sleep 5)"',
    'nohup bash -c "x=\\$(sleep 5)"',
    'setsid sh -c "echo \\$(sleep 5)"',
    "timeout -k 5 60 bash -c 'echo $(sleep 5)'",
    "command eval 'x=$(sleep 5)'",
    "command -p eval 'x=$(sleep 5)'",
    "builtin eval 'x=$(sleep 5)'",
    "sudo command eval 'x=$(sleep 5)'",
    "timeout 60 eval 'x=$(sleep 5)'",
    "nohup eval 'x=$(sleep 5)'",
    # R7 N1: an eval / shell INSIDE a live substitution.
    "echo $(eval 'x=$(sleep 5)')",
    'echo $(eval "x=\\$(sleep 5)")',
    "echo $(bash -c 'echo $(sleep 5)')",
    "echo $(command eval 'x=$(sleep 5)')",
]
#: Waits bash runs that the pre-E-4 walker MISSED and the lane denies: a
#: quoted paren / backtick in an assignment value no longer swallows the
#: command after it (the value's open depth is read from live openers only),
#: and a dash-c segment's arguments beside the `-c` body run in this shell.
STRICTER = [
    "x='(' sleep 5",
    "x='`' sleep 5",
    'x="(" sleep 5',
    "bash -c 'echo' $(sleep 5)",
    "bash script.sh $(sleep 5)",
]
#: Verdict unchanged, token layout changed: a quoted `)` no longer closes a
#: subshell-bodied function early, so the body is laid out as bash reads it.
LAYOUT_CHANGED = [
    "f() ( echo ')'; sleep 5; echo x ); f",
    # Three quoted backticks: the plain walk swallowed `cmd` into the
    # assignments (an odd backtick count opened a value that never closed).
    "x='`' y=\"\\`\" z=$'`' cmd",
]
#: Shapes whose verdict must not move at all (base and lane agree).
UNCHANGED = [
    ("echo $(sleep '5' && true)", None),  # body torn by split_chain (documented limit)
    ('eval "$cmd"', None),
    ("eval \"$(cat <<EOF\nx=\\$(sleep 5)\nEOF\n)\"", None),  # pre-existing
    ("timeout 5 eval 'foo; sleep 2; bar'", None),
    ("timeout 5 eval 'sleep $N'", None),
    ("timeout 60 make test", None),
    ("command -v python3", None),
    ("builtin cd /tmp && sleep 2 && ls", None),
    ("echo 'done;' && sleep 2 && ls", None),
    ("x && timeout 5 bash -c 'sleep 3' && y", None),
    ("timeout 30 bash -c 'foo; sleep 2; bar'", None),
    ("f() ( echo ')'; sleep 5; echo x ); f", None),
    ("sudo bash -c 'x=$(sleep 5)'", ("sleep", "sleep 5")),
    ("a=1 eval 'x=$(sleep 5)'", ("sleep", "sleep 5")),
    ("true; eval 'x=$(sleep 5)'", ("sleep", "sleep 5")),
    ("(eval 'x=$(sleep 5)')", ("sleep", "sleep 5")),
    ("{ eval 'x=$(sleep 5)'; }", ("sleep", "sleep 5")),
    ("eval x='$(sleep 5)'", ("sleep", "sleep 5")),
    ("bash -c 'grep -n \"$(sleep 5)\" f'", ("sleep", "sleep 5")),
    ("eval 'echo \\$(sleep 5)'", ("sleep", "sleep 5")),
    ("while true; do echo $(sleep '5'); done", ("sleep", "sleep 5")),
    ("echo $(sleep '5') # $(sleep 5)", ("sleep", "sleep 5")),
    ("timeout 5 sleep 3", ("sleep", "sleep 3")),
    # A multi-token eval behind a wrapper: not a literal body, nothing live
    # in it — the wrapper's tail scan still reaches the `sleep` word.
    ("timeout 5 eval sleep 30", ("sleep", "sleep 30")),
    ("nohup eval sleep 2", ("sleep", "sleep 2")),
    ("echo '(x)' | sleep 5", ("sleep", "sleep 5")),
    ("f() ( echo '(x)'; sleep 5 ); f", ("sleep", "sleep 5")),
]


@pytest.mark.parametrize("cmd", LITERAL)
def test_quoted_substitution_text_is_not_a_wait(cmd: str) -> None:
    """RED pre-fix: denied as the sole-wait `sleep` pattern."""
    assert wg.static_pattern(cmd) is None


@pytest.mark.parametrize("cmd", LIVE)
def test_live_substitution_still_denies(cmd: str) -> None:
    assert wg.static_pattern(cmd) == ("sleep", "sleep 5")


@pytest.mark.parametrize("cmd", STRICTER)
def test_waits_the_pre_e4_walker_missed_are_denied(cmd: str) -> None:
    assert wg.static_pattern(cmd) == ("sleep", "sleep 5")


@pytest.mark.parametrize("cmd,expected", UNCHANGED)
def test_unchanged_shapes(cmd: str, expected) -> None:
    assert wg.static_pattern(cmd) == expected


def test_unreadable_quoting_walks_nothing() -> None:
    """An unterminated quote is unparseable for every consumer: the walker
    yields no segment (the pre-E-4 contract), with or without the quote
    reading."""
    assert tokenize("echo 'unterminated $(sleep 5)") is None
    assert tokenize("echo 'unterminated $(sleep 5)", literal_openers=True) is None
    assert walk_segments("echo 'unterminated $(sleep 5)", _with_substitutions=True) == []


# --- The mechanism: placeholders never leave the walker -------------------

CORPUS = LITERAL + LIVE + STRICTER + [c for c, _e in UNCHANGED] + [
    'echo "a\\\\$(sleep 5)"',            # escaped backslash, then a LIVE `$(`
    'echo "\\\\\\$(sleep 5)"',           # escaped backslash, then an ESCAPED `$`
    "printf '%s\\n' \"$(( 1 + 2 ))\"",
    "echo \"$(echo '(x)')\" '(y)' \\(z\\)",
    "grep -n '<(x)' f && diff <(cat a) <(cat b)",
    "x='`' y=\"\\`\" z=$'`' cmd",
]


def _rows(segs):
    return [(list(s.tokens), list(s.assignments)) for s in segs]


@pytest.mark.parametrize("cmd", CORPUS)
def test_emitted_tokens_are_byte_identical_to_the_plain_walk(cmd: str) -> None:
    """The opt-in neutralises openers BEFORE tokenisation and restores them
    on emission: a collector sees the same bytes the plain walk produces,
    and no placeholder ever reaches it."""
    plain = walk_segments(cmd, _with_function_bodies=True, _with_assignments=True)
    opted = walk_segments(cmd, _with_substitutions=True,
                          _with_function_bodies=True, _with_assignments=True)
    for seg in opted:
        for tok in list(seg.tokens) + list(seg.assignments):
            assert restore_literal_openers(tok) == tok, (cmd, tok)
    if cmd in STRICTER or cmd in LAYOUT_CHANGED:
        return  # the rows whose layout the quote-faithful reading changes
    # The opted walk adds substitution-body rows (depth >= 1); every row of
    # the plain walk is still there, in order, byte for byte.
    remaining = _rows(opted)
    for row in _rows(plain):
        assert row in remaining, (cmd, row)
        remaining = remaining[remaining.index(row) + 1:]


def test_tokenize_restores_to_the_same_tokens() -> None:
    for cmd in CORPUS:
        plain = tokenize(cmd)
        opted = tokenize(cmd, literal_openers=True)
        assert plain is not None and opted is not None
        assert [restore_literal_openers(t) for t in opted] == plain, cmd


def test_a_command_already_carrying_a_placeholder_is_walked_plainly() -> None:
    """A placeholder character in the input could not be told from one the
    scanner inserted, so such a command keeps the conservative walk: every
    substitution is kept, and the character comes back untouched."""
    cmd = "grep -n '$(sleep 5)' f\ue002"
    segs = walk_segments(cmd, _with_substitutions=True)
    assert [s.tokens for s in segs] == [["grep", "-n", "$(sleep 5)", "f\ue002"], ["sleep", "5"]]
    assert wg.static_pattern(cmd) == ("sleep", "sleep 5")


def test_command_and_builtin_are_wrapper_verbs() -> None:
    assert {"command", "builtin"} <= WRAPPER_VERBS
    assert peel_wrapper_verbs(["command", "-p", "eval", "x"]) == ["eval", "x"]
    assert peel_wrapper_verbs(["builtin", "eval", "x"]) == ["eval", "x"]
    assert peel_wrapper_verbs(["sudo", "command", "-v", "git"]) == ["git"]


# --- Differential proof against the pre-E-4 walker ------------------------


def _load_pre_e4_wait_guard():
    """HEAD's wait guard bound to the PRE-E-4 walker (loaded from git as a
    separate module). `vco_lib/wait_guard.py` differs between the two
    commits only by this change's own edits (depth passed through, a
    non-literal eval tail walked), so the walker is the one variable."""
    try:
        src = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show",
             PRE_E4_WALKER_COMMIT + ":vco_lib/bash_command_walk.py"],
            capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip("pre-E-4 walker not loadable from git: %s" % exc)
    walk = types.ModuleType("_pre_e4_walk")
    exec(compile(src, "<pre-e4 walk>", "exec"), walk.__dict__)
    sys.modules["_pre_e4_walk"] = walk
    wg_src = (REPO_ROOT / "vco_lib" / "wait_guard.py").read_text(encoding="utf-8")
    assert "from vco_lib.bash_command_walk import" in wg_src
    wg_src = wg_src.replace("from vco_lib.bash_command_walk import",
                            "from _pre_e4_walk import")
    mod = types.ModuleType("_pre_e4_wg")
    mod.__file__ = str(REPO_ROOT / "vco_lib" / "wait_guard.py")
    exec(compile(wg_src, "<pre-e4 wg>", "exec"), mod.__dict__)
    return mod


def test_differential_every_wait_the_pre_e4_walker_denied_is_denied() -> None:
    """R7 N1's acceptance: over the whole table, the lane denies everything
    the pre-E-4 walker denied, EXCEPT the literal shapes (bash runs no
    substitution there — the false positive E-4 set out to fix); and it
    denies the STRICTER rows the pre-E-4 walker missed."""
    base = _load_pre_e4_wait_guard()
    mismatches = []
    for cmd in LIVE:
        if base.static_pattern(cmd) is None:
            mismatches.append(("expected base DENY", cmd))
        if wg.static_pattern(cmd) is None:
            mismatches.append(("lane allows a wait base denied", cmd))
    for cmd in LITERAL:
        if base.static_pattern(cmd) is None:
            mismatches.append(("expected base DENY (the false positive)", cmd))
        if wg.static_pattern(cmd) is not None:
            mismatches.append(("lane still denies the literal", cmd))
    for cmd in STRICTER:
        if base.static_pattern(cmd) is not None:
            mismatches.append(("expected base allow (the miss)", cmd))
        if wg.static_pattern(cmd) is None:
            mismatches.append(("lane allows a wait bash runs", cmd))
    for cmd, expected in UNCHANGED:
        if base.static_pattern(cmd) != expected or wg.static_pattern(cmd) != expected:
            mismatches.append(("verdict moved", cmd))
    assert mismatches == []


# --- Hook end to end --------------------------------------------------------


def _payload(cmd: str) -> str:
    return json.dumps({"session_id": "s1", "tool_name": "Bash",
                       "tool_use_id": "tu1", "tool_input": {"command": cmd}})


def _env(tmp_path: Path) -> dict:
    from tests.common.child_env import child_env

    env = child_env()
    env.update({"VCT_STATE_DIR": str(tmp_path / "state"), "VCT_VENV": sys.executable,
                "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path / "home")})
    return env


@pytest.mark.parametrize("impl", ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed"))])
def test_hook_allows_the_grep_and_denies_the_wait(impl: str, tmp_path: Path) -> None:
    argv = (["bash", str(HOOK_SH), "pretooluse"] if impl == "sh" else
            ["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOK_PS1), "pretooluse"])
    allow = subprocess.run(argv, input=_payload(LITERAL[0]), capture_output=True,
                           text=True, env=_env(tmp_path), timeout=120)
    assert allow.returncode == 0, allow.stderr
    assert allow.stdout.strip() == ""
    for cmd in (LIVE[0], "echo $(sleep '5')", "command eval 'x=$(sleep 5)'"):
        deny = subprocess.run(argv, input=_payload(cmd), capture_output=True,
                              text=True, env=_env(tmp_path), timeout=120)
        assert deny.returncode == 0, deny.stderr
        assert json.loads(deny.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
