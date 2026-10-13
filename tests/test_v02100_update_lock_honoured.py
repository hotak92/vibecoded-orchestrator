# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 W2R-06 — ``install.py --update`` honours the launcher's
``<vct_root>/update.lock``: a live FOREIGN holder → refuse with a clear
message; the launcher's own child (the holder is an ancestor) → proceeds; a
stale claim (dead pid, reused pid) → proceeds. All probes are injected.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import update_lock as ul  # noqa: E402

RUST = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "single_flight.rs"
HOLDER = 4242


def _write(root: Path, body: str) -> None:
    (root / ul.LOCK_BASENAME).write_text(body, encoding="utf-8")


def _probes(*, alive=True, started=100.0, chain=(os.getpid(), 1)):
    return dict(alive=lambda pid: alive, started=lambda pid: started,
                lineage=lambda: list(chain))


RUST_TOKEN = REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "process.rs"


def test_format_matches_the_rust_writer():
    """The Rust writer's literal body + basename (the ONE format both read).
    v0.2.102 W6-Z3: three lines — pid, claim time, the holder's start token."""
    src = RUST.read_text(encoding="utf-8")
    assert f'pub const UPDATE_LOCK_BASENAME: &str = "{ul.LOCK_BASENAME}";' in src
    assert re.search(
        r'format!\(\s*"\{\}\\n\{\}\\n\{\}\\n",\s*own_pid,\s*'
        r'chrono::Utc::now\(\)\.timestamp\(\),\s*token\s*\)', src)
    assert re.search(r'let token = vct_launcher_core::process::process_start_token\(own_pid\)', src)
    assert ul.parse_claim("4242\n1727700000\n") == ul.Claim(4242, 1727700000.0)
    assert ul.parse_claim("4242\n1727700000\nproc:991\n") == ul.Claim(
        4242, 1727700000.0, "proc:991")
    # An empty third line (token unreadable on the launcher side) = no token.
    assert ul.parse_claim("4242\n1727700000\n\n") == ul.Claim(4242, 1727700000.0)
    assert ul.parse_claim("") is None and ul.parse_claim("not-a-pid\n") is None


def test_rust_start_token_mirrors_the_python_format():
    """Rust↔Python parity of the token grammar: the Rust mirror
    (vct-launcher-core ``process_start_token``) names the Python home and
    builds the same two token kinds from the same ``/proc/<pid>/stat``
    field; the Python home answers ``proc:<ticks>`` on Linux. The Rust side
    pins its own output in ``update_lock_writes_the_holders_start_token_as_line_three``."""
    from vco_lib.deferral_probes import process_start_token

    src = RUST_TOKEN.read_text(encoding="utf-8")
    fn = src[src.index("pub fn process_start_token"):]
    fn = fn[:fn.index("\n}\n")]
    assert "MUST MATCH `vco_lib/deferral_probes.py::process_start_token`" in src
    assert 'format!("proc:{ticks}")' in fn and ".nth(19)" in fn and "rsplit_once(')')" in fn
    assert 'format!("wall:{secs}.000")' in fn
    tok = process_start_token(os.getpid())
    if sys.platform.startswith("linux"):
        assert tok is not None and re.fullmatch(r"proc:\d+", tok), tok
    elif tok is not None:
        assert re.fullmatch(r"wall:\d+\.\d{3}", tok), tok


# ── v0.2.102 W6-Z3: the start token decides pid reuse, not the wall clock ──


def _identity(same):
    return {"identity": lambda pid, recorded: same}


def test_clock_jump_forward_with_a_live_token_holder_refuses(tmp_path, capsys):
    """A forward wall-clock step after the claim makes psutil's create_time
    for the LIVE holder read far later than the claim time (started=10_000 vs
    claimed 200) — the old rule called that "stale (pid reused)" and let a
    second update through. With a token on line 3 that still names the same
    process, the claim is LIVE → REFUSE."""
    _write(tmp_path, f"{HOLDER}\n200\nproc:777\n")
    probes = _probes(started=10_000.0) | _identity(True)
    d = ul.check(tmp_path, **probes)
    assert d.verdict == ul.REFUSE, d
    assert "reused" not in d.reason
    with pytest.raises(SystemExit):
        ul.refuse_if_foreign_update_running(tmp_path, **probes)


def test_reused_pid_with_a_different_token_proceeds(tmp_path):
    """Same pid number, different process instance (token differs) → stale,
    even though its start time (150) is BEFORE the claim (200) — exactly the
    backward-step case the time rule would have wrongly refused."""
    _write(tmp_path, f"{HOLDER}\n200\nproc:777\n")
    d = ul.check(tmp_path, **(_probes(started=150.0) | _identity(False)))
    assert d.verdict == ul.PROCEED and "reused" in d.reason, d


def test_token_comparison_uses_the_shared_home(tmp_path):
    """No injected identity → the real comparison
    (``deferral_probes.process_identity_matches``) runs against the real
    process: this process's own token → same instance → REFUSE (not an
    ancestor); a forged token of the same kind → reused → PROCEED."""
    from vco_lib.deferral_probes import process_start_token

    me = os.getpid()
    tok = process_start_token(me)
    if tok is None:
        pytest.skip("process start token not readable on this host")
    probes = dict(alive=lambda pid: True, started=lambda pid: 10_000.0,
                  lineage=lambda: [12345, 1])
    _write(tmp_path, f"{me}\n200\n{tok}\n")
    assert ul.check(tmp_path, **probes).verdict == ul.REFUSE
    kind = tok.split(":", 1)[0]
    forged = f"{kind}:1" if kind == "proc" else f"{kind}:1.000"
    _write(tmp_path, f"{me}\n200\n{forged}\n")
    assert ul.check(tmp_path, **probes).verdict == ul.PROCEED


@pytest.mark.parametrize("body", [f"{HOLDER}\n200\n", f"{HOLDER}\n200\n\n"])
def test_legacy_two_line_claim_keeps_the_time_rule(tmp_path, body):
    """No token (a pre-v0.2.102 launcher, or an unreadable token) → the old
    time rule, both arms: started after the claim → stale; before → live."""
    calls = []
    ident = {"identity": lambda pid, rec: calls.append(rec) or True}
    _write(tmp_path, body)
    assert ul.check(tmp_path, **(_probes(started=10_000.0) | ident)).verdict == ul.PROCEED
    assert ul.check(tmp_path, **(_probes(started=150.0) | ident)).verdict == ul.REFUSE
    assert calls == [], "no token → the identity probe is never consulted"


def test_uncomparable_token_falls_back_to_the_time_rule(tmp_path):
    """A token that cannot be compared (identity None: different kind, process
    not inspectable) keeps the time rule rather than guessing."""
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    assert ul.check(tmp_path, **(_probes(started=10_000.0) | _identity(None))).verdict == ul.PROCEED
    assert ul.check(tmp_path, **(_probes(started=150.0) | _identity(None))).verdict == ul.REFUSE


def test_live_foreign_holder_refuses_with_a_clear_message(tmp_path, capsys):
    _write(tmp_path, f"{HOLDER}\n200\n")
    with pytest.raises(SystemExit) as ei:
        ul.refuse_if_foreign_update_running(tmp_path, **_probes(started=150.0))
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "another update of this orchestrator is running" in err and str(HOLDER) in err
    assert (tmp_path / ul.LOCK_BASENAME).exists()  # never deleted here


def test_the_launchers_own_child_proceeds(tmp_path):
    """The launcher holds the lock and spawns install.py (update_run.rs step 8):
    the holder is an ANCESTOR of this process → proceed."""
    _write(tmp_path, f"{HOLDER}\n200\n")
    ul.refuse_if_foreign_update_running(
        tmp_path, **_probes(started=150.0, chain=(os.getpid(), 999, HOLDER, 1)))


@pytest.mark.parametrize("why,probes", [
    ("dead pid", _probes(alive=False)),
    ("pid reused by a newer process", _probes(started=10_000.0)),
])
def test_stale_claims_proceed(tmp_path, why, probes):
    _write(tmp_path, f"{HOLDER}\n200\n")
    assert ul.check(tmp_path, **probes).verdict == ul.PROCEED, why
    ul.refuse_if_foreign_update_running(tmp_path, **probes)


def test_no_lock_and_unknowable_states_proceed(tmp_path, capsys):
    assert ul.check(tmp_path, **_probes()).verdict == ul.PROCEED  # no file
    _write(tmp_path, "garbage\n")
    assert ul.check(tmp_path, **_probes()).verdict == ul.PROCEED
    _write(tmp_path, f"{HOLDER}\n200\n")
    ul.refuse_if_foreign_update_running(tmp_path, **_probes(started=150.0, chain=()) | {
        "lineage": lambda: None})
    assert "could not tell" in capsys.readouterr().err


def test_real_ancestry_of_this_process_contains_its_parent():
    chain = ul.ancestors()
    if chain is None:
        pytest.skip("process ancestry not readable on this host")
    assert chain[0] == os.getpid() and os.getppid() in chain


# ── W4R-08: Windows without psutil is enforced, and the warning says why ──


def test_windows_without_psutil_uses_the_win32_probes(monkeypatch):
    """No psutil on Windows used to make every probe ``None`` (advisory
    guard). The ctypes probes now answer; this pins the delegation."""
    monkeypatch.setattr(ul, "_psutil", lambda: None)
    monkeypatch.setattr(ul, "_IS_WINDOWS", True)
    monkeypatch.setattr(ul, "_win_pid_alive", lambda pid: pid == HOLDER)
    monkeypatch.setattr(ul, "_win_start_time", lambda pid: 150.0)
    monkeypatch.setattr(ul, "_win_parent_of", lambda pid: {HOLDER + 1: 1}.get(pid))
    assert ul.pid_alive(HOLDER) is True and ul.pid_alive(HOLDER + 7) is False
    assert ul.process_start_time(HOLDER) == 150.0
    assert ul.ancestors(HOLDER + 1, parent_of=ul._parent_of) == [HOLDER + 1, 1]
    # End to end: a live foreign holder is REFUSED on that interpreter.
    decision = ul.decide(ul.Claim(HOLDER, 200.0),
                         lineage=lambda: ul.ancestors(HOLDER + 1, parent_of=ul._parent_of))
    assert decision.verdict == ul.REFUSE


def test_the_unknowable_warning_names_the_cause_and_the_remedy(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ul, "_psutil", lambda: None)
    _write(tmp_path, f"{HOLDER}\n200\n")
    ul.refuse_if_foreign_update_running(
        tmp_path, alive=lambda pid: None, started=lambda pid: None, lineage=lambda: None)
    err = capsys.readouterr().err
    assert "could not tell" in err
    assert "psutil is not importable" in err and "venv" in err


@pytest.mark.skipif(os.name != "nt", reason="Win32 API probes run on Windows only")
def test_the_win32_probes_answer_for_real_processes():
    import subprocess

    from tests.common.child_env import child_env

    assert ul._win_pid_alive(os.getpid()) is True
    assert ul._win_parent_of(os.getpid()) == os.getppid()
    started = ul._win_start_time(os.getpid())
    assert started is not None and started > 1_600_000_000
    child = subprocess.Popen([sys.executable, "-c", "pass"], env=child_env())
    child.wait(timeout=60)
    assert ul._win_pid_alive(child.pid) in (False, None)
