# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W-STATE: the test suite may not touch the user's real `~/.vct`.

THE INCIDENT THIS CLOSES
------------------------
`tests/test_v0244_adversarial_fixes.py` drives the real
`install._seed_weaviate_shared_kg_only` with `_is_orchestrator_root_install`
faked True. Written in v0.2.44, it stubbed both write paths that existed then.
v0.2.76's R8 shim added a third one underneath —
`_converge_orchestrator_root_kg_pointer` (install.py:15208) — which resolves
`~/.vct/launcher.db` and UPSERTs `app_state.orchestrator_root_kg_collection`.
Nobody re-audited the stubs, so every local `pytest tests/` from 2026-08-28
rewrote the maintainer's live pointer to that file's last fixture literal
(`'NewKG'`, a string that exists nowhere in shipped source).

**A test's isolation is a claim about production code, and a production change
can silently expire it.** So the guard in `tests/conftest.py` is not another
per-symbol stub: it redirects `VCT_STATE_DIR` (the root of
`vco_lib.paths.vct_root_dir`, hence of every launcher-state consumer) for the
whole suite, and backs that with a `sqlite3.connect` tripwire for callers the
env cannot steer.

WHAT IS PINNED HERE
-------------------
* the redirect is ACTIVE at three levels of the resolution chain — the paths
  module, the launcher-db helper, and `install`'s own resolver;
* the redirect SURVIVES a test that pops the env var in its teardown (the
  failure mode that would silently un-protect the rest of a session);
* the ACT: the exact v0.2.44 call now lands its UPSERT in a fixture DB, and the
  real `~/.vct/launcher.db` is byte-for-byte unmoved;
* the tripwire blocks a read-WRITE handle on real state and LEAVES ALONE a
  read-only one.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from tests.common.launcher_db_fixture import create_empty_launcher_db  # noqa: E402
from vco_lib.paths import launcher_db_path, vct_root_dir  # noqa: E402


def _load_suite_conftest():
    """The already-imported `tests/conftest.py` module object.

    pytest imports it under an implementation-defined name (``conftest`` or
    ``tests.conftest`` depending on rootdir/import mode), so identify it by
    ``__file__`` rather than guessing — the guard's state lives in that ONE
    module instance and importing a second copy would inspect the wrong list.
    """
    wanted = (Path(__file__).parent / "conftest.py").resolve()
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if path and Path(path).resolve() == wanted:
            return mod
    raise AssertionError(f"tests/conftest.py is not loaded ({wanted})")


_suite_conftest = _load_suite_conftest()

_REAL_VCT_ROOT = Path.home() / ".vct"
_REAL_LAUNCHER_DB = _REAL_VCT_ROOT / "launcher.db"
_POINTER_KEY = "orchestrator_root_kg_collection"


def _is_under_real_vct(p: Path) -> bool:
    return p == _REAL_VCT_ROOT or _REAL_VCT_ROOT in p.parents


def _read_real_pointer():
    """`(value, updated_at)` for the live pointer row, or None if unreadable.

    Read-only URI so this probe can never be the thing that mutates it.
    """
    if not _REAL_LAUNCHER_DB.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{_REAL_LAUNCHER_DB}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM app_state WHERE key = ?", (_POINTER_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row


# ═══════════════════════════════════════════════════════════════════════════
# the redirect is active at every tier of the resolution chain
# ═══════════════════════════════════════════════════════════════════════════

def test_vct_root_dir_is_redirected_away_from_the_real_home():
    assert not _is_under_real_vct(vct_root_dir()), (
        f"vct_root_dir() resolved into the real state dir: {vct_root_dir()}"
    )


def test_launcher_db_path_is_redirected():
    assert launcher_db_path() != _REAL_LAUNCHER_DB
    assert not _is_under_real_vct(launcher_db_path())


def test_install_app_state_resolver_is_redirected():
    """The exact resolver the incident went through."""
    resolved = install._discover_app_state_db_path()
    assert not _is_under_real_vct(resolved), (
        f"install._discover_app_state_db_path() -> {resolved}"
    )


def test_secrets_dir_is_redirected():
    from vco_lib.agent_secrets import _secrets_root  # local: optional import

    assert Path(_secrets_root()) != Path.home() / ".vct-secrets"


# ═══════════════════════════════════════════════════════════════════════════
# the redirect self-heals — a teardown that pops the var cannot disarm it
# ═══════════════════════════════════════════════════════════════════════════
#
# Ordered pair: several existing suites do `os.environ.pop("VCT_STATE_DIR")` in
# tearDown, which before W-STATE would have stripped an import-time-only
# redirect for every test that followed. Under a shuffled order the second test
# still passes (it asserts the invariant, not the sequence), so this is a pin
# that can only under-report, never false-red.

def test_aaa_a_teardown_may_pop_the_state_dir_var():
    os.environ.pop("VCT_STATE_DIR", None)
    assert "VCT_STATE_DIR" not in os.environ


def test_aab_the_next_test_still_has_the_redirect():
    assert os.environ.get("VCT_STATE_DIR"), "redirect did not self-heal"
    assert not _is_under_real_vct(vct_root_dir())


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT — the v0.2.44 call writes a fixture DB, not the user's install
# ═══════════════════════════════════════════════════════════════════════════

def test_r8_pointer_write_lands_in_the_redirected_db_not_the_real_one(
    monkeypatch, tmp_path, capsys,
):
    """Reproduce the incident call and prove the redirect CAPTURES its write.

    The stub set is deliberately the one the v0.2.44 tests use — i.e. the R8
    write path is NOT stubbed — so this exercises the very code that reached
    the live DB. If the redirect regresses, the tripwire refuses the handle and
    ``_fail_test_that_tried_to_write_real_state`` reds this test; it can no
    longer quietly succeed against `~/.vct`.
    """
    before = _read_real_pointer()

    state_dir = tmp_path / "vct"
    state_dir.mkdir()
    # Real launcher schema (shipped migrations), not a hand-rolled app_state:
    # the R8 write must land in a DB shaped like the one it would hit in
    # production (v0.2.92 §3.4).
    db_path = create_empty_launcher_db(state_dir / "launcher.db")

    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db_path))
    monkeypatch.delenv("SHARED_KG_WRITE_DISABLED", raising=False)
    monkeypatch.delenv("SHARED_KG_OPT_OUT", raising=False)
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install, "_rebind_orchestrator_root_to_canonical", lambda c: [],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: 0)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _Args:
        update = True

    install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="FixtureKG",
        current_kg_collection="FixtureKG",
    )
    capsys.readouterr()

    # the ACT: the UPSERT landed in the fixture
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (_POINTER_KEY,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "FixtureKG", (
        "the R8 pointer write did not reach the redirected DB — the redirect "
        "may have stopped steering install.py's resolver"
    )

    # the LEAVE-ALONE: the developer's live pointer is untouched
    after = _read_real_pointer()
    assert after == before, (
        f"the real launcher.db pointer moved during this test: "
        f"{before!r} -> {after!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# the tripwire — second layer, for callers the env cannot steer
# ═══════════════════════════════════════════════════════════════════════════

def test_tripwire_blocks_a_read_write_handle_on_real_state():
    """A hardcoded real-state path must be refused even though the env is set."""
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        sqlite3.connect(str(_REAL_LAUNCHER_DB))
    attempts = _suite_conftest.consume_state_write_attempts()
    assert attempts == [str(_REAL_LAUNCHER_DB)]


def test_tripwire_blocks_a_writable_uri_handle_on_real_state():
    """`file:` URI without `mode=ro` is a WRITE handle and is refused too."""
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        sqlite3.connect(f"file:{_REAL_LAUNCHER_DB}", uri=True)
    _suite_conftest.consume_state_write_attempts()


def test_tripwire_fires_before_the_connect_so_nothing_is_created():
    """`sqlite3.connect` CREATES a missing DB file. The tripwire must refuse
    BEFORE calling through, or the guard would itself litter `~/.vct/`."""
    victim = _REAL_VCT_ROOT / "__w_state_tripwire_probe__.db"
    assert not victim.exists(), f"stale probe file left behind: {victim}"
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        sqlite3.connect(str(victim))
    assert not victim.exists(), (
        "the tripwire let the connect through and sqlite created the file"
    )
    assert _suite_conftest.consume_state_write_attempts() == [str(victim)]


def test_tripwire_leaves_read_only_handles_alone():
    """The LEAVE-ALONE case: a `mode=ro` probe of real state is not this
    guard's business and must keep working (this module uses one itself)."""
    if not _REAL_LAUNCHER_DB.is_file():
        pytest.skip("no real launcher.db on this machine")
    conn = sqlite3.connect(f"file:{_REAL_LAUNCHER_DB}?mode=ro", uri=True, timeout=5.0)
    conn.close()
    assert _suite_conftest.consume_state_write_attempts() == []


def test_a_swallowed_refusal_still_reds_the_test():
    """Production callers soft-fail on ``Exception``; the refusal must survive.

    Drives the reporting fixture by hand (setup -> soft-failing connect ->
    teardown) because from inside a test one cannot observe its own teardown.
    This is the property that keeps a near-miss from looking green — the exact
    invisibility that let the 2026-09-01 incident run for four days.
    """
    fixture_fn = _suite_conftest._fail_test_that_tried_to_write_real_state
    fixture_fn = getattr(fixture_fn, "__wrapped__", fixture_fn)
    gen = fixture_fn()
    next(gen)  # setup
    try:
        sqlite3.connect(str(_REAL_LAUNCHER_DB))
    except Exception:  # noqa: BLE001 — deliberately production-shaped
        pass
    with pytest.raises(AssertionError, match="read-WRITE connection"):
        next(gen)  # teardown
    _suite_conftest.consume_state_write_attempts()


def test_tripwire_leaves_temp_databases_alone():
    """No collateral: a tmp DB opened read-write is untouched by the guard."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "scratch.db"))
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        conn.close()
    assert _suite_conftest.consume_state_write_attempts() == []


# ═══════════════════════════════════════════════════════════════════════════
# W-STATE-FILES (v0.2.92 WP-Q2) — the tripwire above only sees SQLITE handles
#
# `~/.vct` is not only databases. `hub.token`, `hub.port`, `hub.pid`,
# `logs/**.jsonl`, the update lockfile and `summary_backend_breaker.json` are
# ordinary files opened through Python, which `sqlite3.connect` never sees. A
# caller that resolved one of those with a hardcoded `Path.home() / ".vct"`
# would have written the developer's live install with the DB guard sitting
# right beside it, blind. The audit hook that already refuses writes into the
# real `~/.claude` now covers this root too.
#
# A guard nobody has seen trip is a guard nobody knows works — so these trip
# it on purpose, and also pin the two things it must NOT do.
# ═══════════════════════════════════════════════════════════════════════════

_VCT_PROBE_NAME = "__wpq2_state_file_tripwire_probe__.json"


def test_file_write_tripwire_blocks_a_plain_write_into_real_vct():
    """The ACT: a hardcoded write under the real `~/.vct` is refused."""
    victim = _REAL_VCT_ROOT / _VCT_PROBE_NAME
    assert not victim.exists(), f"stale probe file left behind: {victim}"
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        victim.write_text("poison", encoding="utf-8")
    assert not victim.exists(), (
        "the tripwire let the open through and the file was created"
    )
    assert _suite_conftest.consume_state_write_attempts() == [str(victim)]


def test_file_write_tripwire_covers_the_other_doors_too():
    """`Path.write_text` is one door of several. `os.open`, `open()` and
    `os.mkdir` must all be refused, because patching a chosen set of symbols
    by name is the per-symbol bet whose expiry caused the W-STATE incident."""
    victim = _REAL_VCT_ROOT / _VCT_PROBE_NAME
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        open(victim, "w").close()  # noqa: SIM115 — never reached
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        os.open(str(victim), os.O_WRONLY | os.O_CREAT)
    with pytest.raises(_suite_conftest.RealUserStateWriteBlocked):
        os.mkdir(str(_REAL_VCT_ROOT / "__wpq2_probe_dir__"))
    assert not victim.exists()
    assert len(_suite_conftest.consume_state_write_attempts()) == 3


def test_file_write_tripwire_leaves_reads_of_real_vct_alone():
    """The LEAVE-ALONE case, and a deliberate asymmetry with the `~/.claude`
    guard (which records reads). `~/.vct` reads are routine and harmless:
    conftest itself reads `hub.port` / `hub.token` at import to decide a
    module-level skip, and the sqlite guard already lets read-only DB handles
    through for the same reason. Recording them would red dozens of tests for
    no incident."""
    if not _REAL_VCT_ROOT.is_dir():
        pytest.skip("no real ~/.vct on this machine")
    for candidate in sorted(_REAL_VCT_ROOT.iterdir()):
        if candidate.is_file():
            candidate.read_bytes()
            break
    else:
        pytest.skip("no readable file directly under ~/.vct")
    assert _suite_conftest.consume_state_write_attempts() == []


def test_file_write_tripwire_leaves_other_dot_vct_paths_alone():
    """No collateral: the fast path keys on the substring `.vct`, so a tmp
    directory that merely CONTAINS that name must still be writable."""
    import tempfile

    with tempfile.TemporaryDirectory(suffix="-.vct-lookalike") as tmp:
        target = Path(tmp) / ".vct" / "scratch.json"
        target.parent.mkdir(parents=True)
        target.write_text("fine", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "fine"
    assert _suite_conftest.consume_state_write_attempts() == []


def test_a_swallowed_file_write_refusal_still_reds_the_test():
    """The property that keeps a near-miss from looking green: production
    callers here soft-fail on ``OSError`` / ``Exception``, so a refusal that
    was only raised would be absorbed. Drives the reporting fixture by hand
    (setup -> soft-failing write -> teardown), since a test cannot observe
    its own teardown."""
    fixture_fn = _suite_conftest._fail_test_that_tried_to_write_real_state
    fixture_fn = getattr(fixture_fn, "__wrapped__", fixture_fn)
    gen = fixture_fn()
    next(gen)  # setup
    try:
        (_REAL_VCT_ROOT / _VCT_PROBE_NAME).write_text("x", encoding="utf-8")
    except Exception:  # noqa: BLE001 — deliberately production-shaped
        pass
    with pytest.raises(AssertionError, match="read-WRITE connection|W-STATE"):
        next(gen)  # teardown
    _suite_conftest.consume_state_write_attempts()


def test_the_audit_hook_serves_both_roots_from_one_installation():
    """One hook, two roots. An audit hook runs on EVERY open in the process,
    so a second hook would double that cost to answer a question this one
    already has the path for."""
    assert hasattr(_suite_conftest, "_user_state_audit_hook")
    assert hasattr(_suite_conftest, "_under_real_vct")
    assert hasattr(_suite_conftest, "_under_real_claude")
    # Both legs record into lists the reporter fixtures already drain.
    assert _suite_conftest._under_real_vct(str(_REAL_VCT_ROOT / "hub.token")) is not None
    assert _suite_conftest._under_real_vct("/tmp/elsewhere/hub.token") is None


def test_stand_aside_list_is_exactly_the_reviewed_set():
    """The inversion's whole point: standing aside is opt-IN now.

    Spelled out rather than asserted non-empty, so growing the list has to
    touch two places and shows up in a diff as a deliberate act.
    """
    assert _suite_conftest._SELF_ISOLATED_STATE_DIR_FILES == frozenset({
        "test_launcher_db_reader.py",
    }), (
        "a file was granted a stand-aside from the state-dir redirect; record "
        "the rationale next to the list in tests/conftest.py and update this "
        "pin deliberately"
    )
