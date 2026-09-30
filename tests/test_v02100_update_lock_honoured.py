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


def test_format_matches_the_rust_writer():
    """The Rust writer's literal body + basename (the ONE format both read)."""
    src = RUST.read_text(encoding="utf-8")
    assert f'pub const UPDATE_LOCK_BASENAME: &str = "{ul.LOCK_BASENAME}";' in src
    assert re.search(r'format!\("\{\}\\n\{\}\\n", own_pid, chrono::Utc::now\(\)\.timestamp\(\)\)', src)
    assert ul.parse_claim("4242\n1727700000\n") == ul.Claim(4242, 1727700000.0)
    assert ul.parse_claim("") is None and ul.parse_claim("not-a-pid\n") is None


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
