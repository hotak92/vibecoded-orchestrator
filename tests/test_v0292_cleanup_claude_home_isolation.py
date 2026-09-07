# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W-CLAUDE: the test suite may not touch the user's real `~/.claude`.

THE LEAK THIS CLOSES
--------------------
`vco_lib.embedding_service.NoEmbeddingBackendError` captures a telemetry row on
CONSTRUCTION, and `_failure_jsonl_path()` returned
``Path.home()/".claude"/"metrics"/"embedding_failures.jsonl"`` — reconstructed
inline, with no override anywhere in the chain, so nothing could steer it.
`tests/test_maintain_kg_guards.py` builds that exception as a mock
``side_effect``, which meant every local ``pytest tests/`` appended
fixture-shaped rows (``"attempted_backends": []``, ``"message": "none"``,
``"install_root": null``) to the maintainer's REAL telemetry stream: measured
1257 -> 1265 during one lane's runs, +2 more in the run that produced this fix.

That is the W-STATE lesson one resource over: **a `Path.home()` with no root
is unsteerable, and a test's isolation is a claim about production code**. The
guard therefore does not stub `_failure_jsonl_path` by name — it gives the
resource a root (``vco_lib.paths.claude_user_dir`` / ``$VCT_CLAUDE_DIR``) that
every present and future consumer goes through, and backs it with an audit-hook
tripwire for callers the env cannot steer.

TWO RESOURCES, TWO LEVERS
-------------------------
``~/.claude.json`` is a FILE beside ``~/.claude/``, resolved from the user HOME
rather than from the Claude dir, so it needs the OTHER lever:
``$VCT_USER_HOME_OVERRIDE`` (which existed since v0.2.11 but had never been
pulled suite-wide). Pulling it moved `install.py`'s two readers; the tripwire
then caught `vco_lib.doctor._read_claude_json_mcp_servers` still reading the
developer's real global MCP registrations from `run_doctor` — found by the
guard, not by inspection, which is the whole argument for having one. That
file belongs to another lane in this cycle, so the read is recorded in
``_CLAUDE_READ_PENDING_OWNER`` with its exact fix rather than taken.

WHAT IS PINNED HERE
-------------------
* the redirect is ACTIVE at every tier — paths module, user-home helper,
  install's own resolver (parity), and the failure-capture writer;
* the ACT: constructing the real exception lands its row in the fixture stream
  and leaves the real ``embedding_failures.jsonl`` byte-for-byte unmoved;
* the tripwire REFUSES a write the env cannot steer, and refuses it BEFORE the
  syscall so the guard itself creates nothing — demonstrated on a call site
  that IS currently unsteered (`install._BUNDLED_VERSIONS_AUDIT_LOG`), which is
  the two-layer design's whole point;
* the deliberate asymmetry: reads are RECORDED, not refused;
* the pending-read list is exactly the reviewed set, and an entry on it
  downgrades a READ only — a write is red for those files too;
* a SWALLOWED refusal still reds the test — the property without which a
  near-miss looks green, since these writers soft-fail on ``OSError``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import embedding_service  # noqa: E402
from vco_lib.paths import claude_metrics_dir, claude_user_dir, user_home  # noqa: E402


def _load_suite_conftest():
    """The already-imported `tests/conftest.py` module object.

    Identified by ``__file__`` rather than by name, because pytest imports it
    as ``conftest`` or ``tests.conftest`` depending on rootdir/import mode and
    the guard's recorded state lives in that ONE module instance.
    """
    wanted = (Path(__file__).parent / "conftest.py").resolve()
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if path and Path(path).resolve() == wanted:
            return mod
    raise AssertionError(f"tests/conftest.py is not loaded ({wanted})")


_suite_conftest = _load_suite_conftest()

_REAL_CLAUDE_DIR = Path.home() / ".claude"
_REAL_CLAUDE_JSON = Path.home() / ".claude.json"
_REAL_FAILURES_JSONL = _REAL_CLAUDE_DIR / "metrics" / "embedding_failures.jsonl"


def _is_under_real_claude(p: Path) -> bool:
    return p == _REAL_CLAUDE_DIR or _REAL_CLAUDE_DIR in p.parents


def _fake_request(basename: str, nodeid: str = "faked::node"):
    """Minimal stand-in for pytest's ``request`` — the reporter fixture reads
    only ``node.fspath.basename`` (pending-owner lookup) and ``node.nodeid``
    (the warning text)."""

    class _FsPath:
        pass

    _FsPath.basename = basename

    class _Node:
        fspath = _FsPath()

    _Node.nodeid = nodeid

    class _Request:
        node = _Node()

    return _Request()


def _real_failures_bytes() -> int | None:
    """Size of the real telemetry stream, or None when absent (CI)."""
    try:
        return _REAL_FAILURES_JSONL.stat().st_size
    except OSError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# the redirect is active at every tier of the resolution chain
# ═══════════════════════════════════════════════════════════════════════════

def test_claude_user_dir_is_redirected_away_from_the_real_home():
    assert not _is_under_real_claude(claude_user_dir()), claude_user_dir()
    assert claude_user_dir() != _REAL_CLAUDE_DIR


def test_claude_metrics_dir_is_redirected():
    assert not _is_under_real_claude(claude_metrics_dir()), claude_metrics_dir()


def test_user_home_is_redirected():
    """`~/.claude.json` hangs off THIS one, not off `claude_user_dir()`."""
    assert user_home() != Path.home()
    assert user_home() / ".claude.json" != _REAL_CLAUDE_JSON


def test_install_user_home_resolver_agrees_with_the_shared_one():
    """PARITY pin across the two copies that currently exist.

    ``install._user_home_for_install`` has honoured ``$VCT_USER_HOME_OVERRIDE``
    since v0.2.11; ``vco_lib.paths.user_home`` is the same resolution lifted
    somewhere callers outside install.py can reach (the vco_lib -> install
    back-edge was deliberately broken in v0.2.77). ``install.py`` belongs to
    another lane this cycle, so the two copies coexist for now and this asserts
    they cannot drift. Collapse install.py's body to ``return user_home()``
    when that lane lands, and this becomes a tautology worth keeping.
    """
    assert install._user_home_for_install() == user_home()
    assert install._user_home_for_install() != Path.home()


def test_embedding_failure_log_path_is_redirected():
    """The exact resolver the leak went through."""
    resolved = embedding_service._failure_jsonl_path()
    assert not _is_under_real_claude(resolved), resolved
    assert resolved != _REAL_FAILURES_JSONL


def test_the_bundled_versions_audit_log_is_steered_by_the_redirect():
    """``install._BUNDLED_VERSIONS_AUDIT_LOG`` resolves through the root.

    The follow-up this file was waiting for has landed: ``install.py``
    (23331-23333) now builds the constant from
    ``vco_lib.paths.claude_metrics_dir()`` instead of an inline
    ``Path.home()``, so the redirect steers it and the tripwire has nothing
    to refuse.

    What was here before was a conditional that ``return``ed the moment the
    constant was NOT under the real ``~/.claude`` — i.e. from the instant the
    routing landed, the body never executed again, and it would have gone on
    reporting green if the routing were reverted (the revert makes the
    tripwire arm reachable again, which the old body treated as success).
    A check whose body is skipped by the very condition it exists to prove is
    not a check. It is asserted unconditionally now.

    The two-layer tripwire it used to demonstrate is not lost: six tests
    below drive it directly (``test_tripwire_blocks_a_write_open_on_the_real_
    claude_dir`` and siblings), and on a call site that stays hardcoded by
    construction rather than one that was about to be fixed.
    """
    target = install._BUNDLED_VERSIONS_AUDIT_LOG
    assert not _is_under_real_claude(target), target
    assert target == claude_metrics_dir() / "bundled_versions.jsonl", target

    # End-to-end through the real writer, not a bare `open`: the wrapper
    # reads the module-level constant at call time and the DI core swallows
    # every exception, so "the row is on disk" is the only honest evidence
    # that the write both happened AND was not refused.
    install._append_bundled_versions_audit({"probe": "w-claude-m26"})

    assert target.is_file(), f"the audit append wrote nothing at {target}"
    assert '"probe": "w-claude-m26"' in target.read_text(encoding="utf-8")
    assert _suite_conftest.consume_claude_access_attempts() == []


def test_the_pending_read_list_is_exactly_the_reviewed_set():
    """One known de-hermeticising READ remains, and it has a named owner.

    ``vco_lib.doctor._read_claude_json_mcp_servers`` resolves
    ``Path.home() / ".claude.json"`` inline, so ``$VCT_USER_HOME_OVERRIDE``
    cannot steer it — ``run_doctor`` then branches on whatever MCP entries this
    machine has registered while CI branches on an empty mapping. The file is
    another lane's this cycle, so the entry is recorded rather than the read
    being silently tolerated. Spelled out here so growing the list has to touch
    two places and shows up in a diff as a deliberate act — and so that
    emptying it (the release-tag requirement) is a one-line, visible change.
    """
    assert _suite_conftest._CLAUDE_READ_PENDING_OWNER == {}, (
        "the pending-read list must be EMPTY at release-tag time; its only "
        "historical entry closed when vco_lib/doctor.py moved to "
        "vco_lib.paths.user_home()"
    )
    # Vacuous while the list is empty, and deliberately kept: it is the rule a
    # future entry must satisfy, and it fires the moment one is added.
    for reason in _suite_conftest._CLAUDE_READ_PENDING_OWNER.values():
        assert "FIX" in reason and "user_home" in reason, (
            "every pending entry must name the exact fix and its owner"
        )


def test_a_pending_entry_downgrades_reads_but_never_writes(monkeypatch):
    """The exemption is scoped to the leg that cannot damage anything.

    Drives the reporter by hand with a faked node id for the pending file:
    a READ is warned about, a WRITE in the same test still reds it.
    """
    fixture_fn = _suite_conftest._fail_test_that_touched_real_claude_home
    fixture_fn = getattr(fixture_fn, "__wrapped__", fixture_fn)

    # The real list is EMPTY (and must stay empty), so the mechanism is driven
    # with a SYNTHETIC entry. Without it this test would pass vacuously: the
    # downgrade branch would never be entered, and the "a write still reds it"
    # half — the property that actually matters — would prove nothing.
    _synthetic = "test__synthetic_pending_owner__.py"
    monkeypatch.setitem(
        _suite_conftest._CLAUDE_READ_PENDING_OWNER, _synthetic,
        "FIX (owner: synthetic fixture): resolve via vco_lib.paths.user_home().",
    )
    pending = _fake_request(_synthetic)

    # READ only -> warned, not raised.
    gen = fixture_fn(pending)
    next(gen)
    try:
        with open(_REAL_CLAUDE_DIR / "__w_claude_pending_read__", "r"):
            pass
    except OSError:
        pass
    with pytest.warns(UserWarning, match="known"):
        with pytest.raises(StopIteration):
            next(gen)

    # WRITE -> still red, exemption or not.
    gen = fixture_fn(pending)
    next(gen)
    try:
        with open(_REAL_CLAUDE_DIR / "__w_claude_pending_write__", "a"):
            pass
    except Exception:  # noqa: BLE001
        pass
    with pytest.raises(AssertionError, match="wrote: "):
        next(gen)
    _suite_conftest.consume_claude_access_attempts()


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT — the real failure capture writes the fixture stream, not the user's
# ═══════════════════════════════════════════════════════════════════════════

def test_failure_capture_lands_in_the_redirect_and_leaves_the_real_stream_alone():
    """Reproduce the leak exactly: construct the exception, capture fires.

    Nothing is stubbed — this is the same one-liner
    (`NoEmbeddingBackendError("none")`) that `tests/test_maintain_kg_guards.py`
    passes as a mock `side_effect`, i.e. the very call that was appending to
    the maintainer's telemetry.
    """
    before = _real_failures_bytes()
    target = embedding_service._failure_jsonl_path()
    lines_before = (
        target.read_text(encoding="utf-8").count("\n") if target.is_file() else 0
    )

    embedding_service.NoEmbeddingBackendError("none")

    assert target.is_file(), f"capture wrote nothing at {target}"
    lines_after = target.read_text(encoding="utf-8").count("\n")
    assert lines_after == lines_before + 1, (
        "the capture did not land in the redirected stream — the redirect may "
        "have stopped steering _failure_jsonl_path()"
    )

    after = _real_failures_bytes()
    assert after == before, (
        f"the real embedding_failures.jsonl changed size during this test: "
        f"{before!r} -> {after!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# the tripwire — second layer, for callers the env cannot steer
# ═══════════════════════════════════════════════════════════════════════════

def test_tripwire_blocks_a_write_open_on_the_real_claude_dir():
    """A hardcoded real path must be refused even though the env is set."""
    victim = _REAL_CLAUDE_DIR / "__w_claude_tripwire_probe__.jsonl"
    assert not victim.exists(), f"stale probe file left behind: {victim}"
    with pytest.raises(_suite_conftest.RealClaudeHomeWriteBlocked):
        with open(victim, "a", encoding="utf-8"):
            pass
    assert not victim.exists(), (
        "the tripwire let the open through and the file was created — it must "
        "refuse BEFORE the syscall, or the guard itself litters ~/.claude"
    )
    assert _suite_conftest.consume_claude_access_attempts() == [
        ("write", str(victim))
    ]


def test_tripwire_blocks_pathlib_write_text_too():
    """`Path.write_text` goes through `Path.open` -> `io.open`, which a
    patched `builtins.open` would NOT have seen. The audit hook does."""
    victim = _REAL_CLAUDE_DIR / "__w_claude_tripwire_probe2__.txt"
    with pytest.raises(_suite_conftest.RealClaudeHomeWriteBlocked):
        victim.write_text("nope", encoding="utf-8")
    assert not victim.exists()
    _suite_conftest.consume_claude_access_attempts()


def test_tripwire_blocks_mkdir_under_the_real_claude_dir():
    """`_write_failure_jsonl` mkdirs its parent before appending; the mkdir is
    part of the same act and is refused as one."""
    victim = _REAL_CLAUDE_DIR / "__w_claude_tripwire_dir__"
    with pytest.raises(_suite_conftest.RealClaudeHomeWriteBlocked):
        victim.mkdir(parents=True, exist_ok=True)
    assert not victim.exists()
    _suite_conftest.consume_claude_access_attempts()


def test_tripwire_blocks_a_write_to_the_real_claude_json():
    """`~/.claude.json` is Claude Code's global config, not ours to rewrite."""
    with pytest.raises(_suite_conftest.RealClaudeHomeWriteBlocked):
        with open(_REAL_CLAUDE_JSON, "a", encoding="utf-8"):
            pass
    assert _suite_conftest.consume_claude_access_attempts() == [
        ("write", str(_REAL_CLAUDE_JSON))
    ]


def test_tripwire_records_a_read_without_refusing_it():
    """The deliberate asymmetry, spelled out so it cannot drift by accident.

    A read cannot damage the install, and raising mid-read would push
    production code down a fallback branch — changing what the test proves
    while looking like a guard success. So reads are recorded (and red the
    test in teardown) rather than refused. The probe targets a path that does
    not exist, so this pins the behaviour identically on CI and on a developer
    box: the audit event fires before the syscall, the ``FileNotFoundError``
    is the syscall's own answer.
    """
    probe = _REAL_CLAUDE_DIR / "__w_claude_read_probe__"
    with pytest.raises(FileNotFoundError):
        with open(probe, "r", encoding="utf-8"):
            pass
    assert _suite_conftest.consume_claude_access_attempts() == [
        ("read", str(probe))
    ]


def test_tripwire_leaves_everything_outside_the_real_claude_dir_alone(tmp_path):
    """No collateral: the redirected tree is written freely, every test long."""
    (tmp_path / "scratch.jsonl").write_text("{}\n", encoding="utf-8")
    (claude_metrics_dir()).mkdir(parents=True, exist_ok=True)
    (claude_metrics_dir() / "scratch.jsonl").write_text("{}\n", encoding="utf-8")
    assert _suite_conftest.consume_claude_access_attempts() == []


def test_a_swallowed_refusal_still_reds_the_test():
    """The writers here soft-fail (`_write_failure_jsonl` catches `OSError`,
    `doctor` catches `Exception`), so a refusal that was only RAISED could be
    absorbed and the near-miss would look green — the same invisibility that
    let the sibling `~/.vct` incident run four days.

    Drives the reporting fixture by hand (setup -> swallowing write ->
    teardown) because a test cannot observe its own teardown.
    """
    fixture_fn = _suite_conftest._fail_test_that_touched_real_claude_home
    fixture_fn = getattr(fixture_fn, "__wrapped__", fixture_fn)
    gen = fixture_fn(_fake_request("test_not_pending.py"))
    next(gen)  # setup
    try:
        with open(_REAL_CLAUDE_DIR / "__w_claude_swallowed__", "a"):
            pass
    except Exception:  # noqa: BLE001 — deliberately production-shaped
        pass
    with pytest.raises(AssertionError, match="touched the user's real"):
        next(gen)  # teardown
    _suite_conftest.consume_claude_access_attempts()


def test_a_swallowed_read_also_reds_the_test():
    """Same reporter, read side — a de-hermeticising read must not pass green
    just because nothing raised."""
    fixture_fn = _suite_conftest._fail_test_that_touched_real_claude_home
    fixture_fn = getattr(fixture_fn, "__wrapped__", fixture_fn)
    gen = fixture_fn(_fake_request("test_not_pending.py"))
    next(gen)
    try:
        with open(_REAL_CLAUDE_DIR / "__w_claude_read_swallowed__", "r"):
            pass
    except OSError:
        pass
    with pytest.raises(AssertionError, match="read: "):
        next(gen)
    _suite_conftest.consume_claude_access_attempts()


# ═══════════════════════════════════════════════════════════════════════════
# the redirect self-heals — a teardown that pops the vars cannot disarm it
# ═══════════════════════════════════════════════════════════════════════════
#
# Ordered pair (same construction as the `~/.vct` sibling): under a shuffled
# order the second test still asserts the invariant, so this can only
# under-report, never false-red.

def test_aaa_a_teardown_may_pop_the_claude_vars():
    os.environ.pop("VCT_CLAUDE_DIR", None)
    os.environ.pop("VCT_USER_HOME_OVERRIDE", None)
    assert "VCT_CLAUDE_DIR" not in os.environ


def test_aab_the_next_test_still_has_the_redirect():
    assert os.environ.get("VCT_CLAUDE_DIR"), "VCT_CLAUDE_DIR did not self-heal"
    assert os.environ.get("VCT_USER_HOME_OVERRIDE"), (
        "VCT_USER_HOME_OVERRIDE did not self-heal"
    )
    assert not _is_under_real_claude(claude_user_dir())
    assert user_home() != Path.home()


# ═══════════════════════════════════════════════════════════════════════════
# the stand-aside list is about `~/.vct` ONLY
# ═══════════════════════════════════════════════════════════════════════════

def test_the_state_dir_stand_aside_does_not_leak_into_the_claude_guard():
    """`_SELF_ISOLATED_STATE_DIR_FILES` exists because `VCT_STATE_DIR` is an
    earlier TIER of the launcher-db resolver one file deliberately tests. That
    reason says nothing about `~/.claude`, so the claude vars are re-established
    unconditionally. Pinned here so a future edit that folds them into the
    popped set has to break a test that explains why not.
    """
    src = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    marker = "os.environ.update(claude_keys)"
    assert marker in src, (
        "conftest no longer sets the ~/.claude redirect unconditionally"
    )
    for key in ("VCT_CLAUDE_DIR", "VCT_USER_HOME_OVERRIDE"):
        assert key not in _suite_conftest._SELF_ISOLATED_STATE_DIR_FILES
