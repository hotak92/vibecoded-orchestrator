# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pytest fixtures shared across the orchestrator test suite.

v0.2.46 KG-AUTO-HEAL-E + v0.2.47 RL-6c (paired ship): the autouse fixture
below forces ``VCT_DISABLE_HUB_RESOLVER=1`` for every test EXCEPT those
in the opt-out list (which explicitly exercise the hub-resolver path).

**Why this is needed**: on developer machines where the launcher's
``vct-hub`` is running (the maintainer box, every contributor's local
setup), tests that monkey-patch ``KG_COLLECTION`` /
``SHARED_KG_COLLECTION`` / ``VCT_KG_ACCESS_LIST`` env vars were silently
losing to the hub-resolved values — both ``_try_resolve_project_config()``
in ``claude_mcp_servers/weaviate_mcp/server.py`` AND
``vco_lib.project_config.resolve()`` itself were called BEFORE the env-
fallback chain, and the hub returned the project's REAL bindings. Tests
passed on CI (no hub running) but failed locally with confusing diff
messages like:

    AssertionError: '[peer:Alpha]' != '[self]'
    AssertionError: 'VibeCodedOrchestrator_KnowledgeGraph' != 'AcmeTeam_SharedKG'

The gate at ``vco_lib.project_config.resolve`` short-circuits to
``HubUnreachable`` when ``VCT_DISABLE_HUB_RESOLVER`` is truthy. The
calling script's try/except then falls through to its env-var
fallback path, which is what the tests have been setting up.

The opt-out list contains tests that EXPLICITLY exercise the resolver
path (they spawn a mock hub and want the production code path to
actually reach the mocked function). The opt-out is intentionally
explicit so an accidentally-broken hub-resolver test surfaces loudly
rather than silently picking up the live machine's hub config.

References:
- ``vco_lib/project_config.py::resolve`` — the gate (v0.2.46).
- ``claude_mcp_servers/weaviate_mcp/server.py::_try_resolve_project_config``
  — the same gate at the MCP layer (v0.2.47 RL-6c).
- ``knowledge/concepts/launcher-hub-single-writer-principle.md`` — why
  the hub is the production source of truth (but tests need a hatch).
- ``knowledge/concepts/parallel-pr-coordination-gotchas-2026-05-10.md``
  §14 — the lesson cluster this conftest closes.
"""
from __future__ import annotations

import functools
import os
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ─── P5 (v0.2.91): keep the suite out of the PRODUCTION telemetry streams ────
#
# The 2026-08-27 perf audit found two live leaks from this test suite into the
# user's real `~/.vct/`:
#
#   * 6065 rows with FIXTURE titles ("Foo", "SharedConcept") in
#     `~/.vct/logs/weaviate_mcp/<date>_tool_usage.jsonl` — the kg-search /
#     kg-info / kg-sync CLI telemetry stream;
#   * 17 `resync-TProj-*.log` spawn records a day in `~/.vct/logs/` from
#     fixtures reaching `spawn_background_resync`.
#
# Neither is cosmetic: a production metrics stream polluted with fixture traffic
# is a stream no future perf audit can trust (this one had to hand-filter it).
# The fix is the same hermeticity convention the RL-hub and hub-resolver gates
# already use — an env pin, set for the whole suite, with an explicit opt-out
# list for the tests that genuinely exercise the pinned behaviour.
#
# `VCT_QUERY_LOG_DIR` MUST be set at conftest IMPORT time, not in a fixture:
# `weaviate_mcp/query_logger.py` resolves `LOG_DIR` / `TOOL_USAGE_LOG` at MODULE
# import, so a fixture that ran after the first test module imported it would be
# too late. conftest.py is imported before any test module, so this is the one
# hook early enough.
_VCO_TEST_STATE = Path(tempfile.mkdtemp(prefix="vco-test-state-"))
os.environ.setdefault("VCT_QUERY_LOG_DIR", str(_VCO_TEST_STATE / "query_logs"))


# ─── W-STATE (v0.2.92): no test may reach the user's REAL ~/.vct ────────────
#
# 2026-09-01 incident. Four tests in `test_v0244_adversarial_fixes.py` drove
# `install._seed_weaviate_shared_kg_only` with `_is_orchestrator_root_install`
# faked True. They stubbed the two write paths that existed when they were
# written (`_write_app_state_key`, `_rebind_orchestrator_root_to_canonical`) —
# and then v0.2.76's R8 shim added a THIRD one underneath them
# (`_converge_orchestrator_root_kg_pointer`), which resolves the real
# `~/.vct/launcher.db` and UPSERTs `app_state.orchestrator_root_kg_collection`.
# Every local `pytest tests/` since has rewritten that row to the last fixture
# literal in file order: the maintainer's live pointer read `'NewKG'` for four
# days (2026-08-28T18:37 -> repaired 2026-09-01T22:37), and `NewKG` exists
# nowhere in shipped source — only in that test file.
#
# The lesson is NOT "those four tests were sloppy". They were correct when
# written. **A test's isolation is a claim about production code, and a
# production change can silently expire it.** So the guard cannot be another
# per-symbol stub (the next new write path expires that too) — it redirects
# the PATH RESOLUTION at its root, which every present and future consumer
# goes through:
#
#   `VCT_STATE_DIR` is the first tier of `vco_lib.paths.vct_root_dir`, and
#   `tests/test_vct_root_dir_consolidation.py` already forbids reconstructing
#   `~/.vct` inline outside that module. So redirecting it moves launcher.db,
#   hub.db, `~/.vct/logs/`, the hub token/port files and the update lockfile in
#   ONE place — including for the subprocess-spawning tests, which inherit it.
#
# This INVERTS the previous convention: the state-dir redirect used to be
# opt-IN (only `_RESYNC_SPAWN_OPT_OUT_FILES` got it), so ~63 test files reached
# the real `~/.vct` by default. It is now the DEFAULT, and standing aside from
# it is the thing you opt into (`_SELF_ISOLATED_STATE_DIR_FILES`, below).
#
# Safety of the inversion: CI already runs `pytest tests/ -q` on a runner with
# NO `~/.vct` at all, so every test must already tolerate an absent launcher.db
# — the redirect just makes a developer's local run match CI instead of
# silently reading (and, per above, writing) their live install.
#
# Set at conftest IMPORT time, not only in the fixture, because module-scope
# code resolves state paths during COLLECTION, before any function fixture can
# run: `tests/test_agent_secrets.py` decides its module-level skip by reading
# `~/.vct/hub.port` + `hub.token` at import. The autouse fixture below then
# RE-ESTABLISHES the redirect per test, which matters for the opposite reason:
# several suites set `VCT_STATE_DIR` themselves and `os.environ.pop` it in
# tearDown, which would otherwise delete the import-time redirect for every
# test that follows.
_VCO_STATE_REDIRECT = _VCO_TEST_STATE / "vct_root"
_VCO_SECRETS_REDIRECT = _VCO_TEST_STATE / "vct_secrets"
_VCO_STATE_REDIRECT.mkdir(parents=True, exist_ok=True)
_VCO_SECRETS_REDIRECT.mkdir(parents=True, exist_ok=True)

_REAL_VCT_ROOT = Path.home() / ".vct"

# Escape hatch for a deliberate run against the real state dir (debugging a
# live install). Never set in CI or in a normal local run. Covers BOTH the
# `~/.vct` redirect above and the `~/.claude` one below — one hatch for "run
# against my real user state", not one per resource.
_ALLOW_REAL_STATE = os.environ.get("VCO_TEST_ALLOW_REAL_STATE", "") not in (
    "", "0", "false",
)

if not _ALLOW_REAL_STATE:
    os.environ["VCT_STATE_DIR"] = str(_VCO_STATE_REDIRECT)
    os.environ["VCT_SECRETS_DIR"] = str(_VCO_SECRETS_REDIRECT)
    # `VCT_LAUNCHER_DB_PATH` OUTRANKS `VCT_STATE_DIR` in
    # `vco_lib.paths.launcher_db_path`, so an ambient one would defeat the
    # redirect. Drop it; tests that set it themselves still win (they set it
    # after this fixture, inside their own setUp).
    os.environ.pop("VCT_LAUNCHER_DB_PATH", None)


# ─── W-CLAUDE (v0.2.92): the same discipline for the user's real ~/.claude ───
#
# W-STATE (above) closed `~/.vct`. It did NOT cover `~/.claude`, and that one
# was leaking at the same time: `vco_lib.embedding_service._failure_jsonl_path`
# returned `Path.home()/".claude"/"metrics"/"embedding_failures.jsonl"` with no
# override anywhere in the chain, so two tests in
# `tests/test_maintain_kg_guards.py` appended fixture-shaped rows
# (`"attempted_backends": []`, `"message": "none"`, `"install_root": null`) to
# the maintainer's REAL telemetry stream on every `pytest tests/` — measured
# 1257 -> 1265 rows across one lane's runs, and +2 more in the run that
# produced this fix. Same shape as the W-STATE incident: a per-call-site
# `Path.home()` with no root anyone could steer.
#
# Two levers, because there are two resources:
#
#   * `VCT_CLAUDE_DIR` -> `vco_lib.paths.claude_user_dir()`, the new root of
#     every `~/.claude/**` consumer (the metrics writers in
#     `embedding_service` / `install.py` / `vco_lib.cli.verify`, the
#     `workflow/config/mcp-config.json` read in
#     `templates/scripts/query_code_graph.py`, the legacy
#     `workflow/scripts` sys.path entry in
#     `templates/scripts/detect_duplicates.py`).
#   * `VCT_USER_HOME_OVERRIDE` -> `install._user_home_for_install()`, which
#     already existed (v0.2.11 PR-16) and already resolves `~/.claude.json`
#     for `_check_ollama_mcp_remnants` / `_check_search_mcp_env_obsolete`.
#     The lever was simply never pulled suite-wide, so four tests read the
#     developer's real global Claude config and branched on its contents —
#     de-hermeticising, since CI has no such file.
#
# `~/.claude.json` is a FILE beside `~/.claude/`, not inside it, which is why
# one env var cannot cover both. Do not add a third: teach a new consumer one
# of these two.
#
# Set at IMPORT time for the same reason as the others — module-scope code
# resolves these during COLLECTION (`query_code_graph.py` reads
# `mcp-config.json` at module import, and it is imported by 6 test files).
_VCO_CLAUDE_REDIRECT = _VCO_TEST_STATE / "claude_home"
_VCO_USER_HOME_REDIRECT = _VCO_TEST_STATE / "user_home"
_VCO_CLAUDE_REDIRECT.mkdir(parents=True, exist_ok=True)
_VCO_USER_HOME_REDIRECT.mkdir(parents=True, exist_ok=True)

_REAL_CLAUDE_DIR = Path.home() / ".claude"
_REAL_CLAUDE_JSON = Path.home() / ".claude.json"

if not _ALLOW_REAL_STATE:
    os.environ["VCT_CLAUDE_DIR"] = str(_VCO_CLAUDE_REDIRECT)
    os.environ["VCT_USER_HOME_OVERRIDE"] = str(_VCO_USER_HOME_REDIRECT)


# ─── W-WEAVIATE (v0.2.94): no test reaches a LIVE Weaviate by default ────────
#
# 2026-09 incident. The maintainer's live Weaviate held `Alpha_KnowledgeGraph`
# — 70 REAL `knowledge/concepts/*.md` nodes — and `Alpha` is not a project: it
# is THIS SUITE's fixture project name. Two ordinary leaks met.
#
#   * `test_kg_access_list.py::_fresh_server` applies its overrides with
#     `os.environ[k] = v` and documents that it does NOT restore them, so
#     `KG_COLLECTION=Alpha_KnowledgeGraph` survives into the rest of the
#     session.
#   * Nothing pinned `WEAVIATE_URL`. `scripts/pre-ship-check.sh`'s full-suite
#     leg and CI's `pytest tests/ -q` both run with the AMBIENT environment,
#     and every shipped resolver defaults to `http://localhost:8081` — on the
#     maintainer's box, the instance holding every project's data. (The
#     sentinel some lanes pass on the command line is an agent convention, not
#     something a shipped gate does.) `test_deferral_report.py` even sets that
#     URL explicitly and restores with `os.environ.update(env_backup)`, which
#     cannot REMOVE a key the backup did not have — so a plain run leaks the
#     live URL onward too.
#
# Any later test that spawns a real `sync_knowledge_graph.py` child — children
# inherit `os.environ` through `tests/common/child_env.py` — then synced the
# real `knowledge/` tree into the fixture's class on the real backend.
#
# Same shape as W-STATE above, same remedy: redirect the ROOT every consumer
# resolves through, BY DEFAULT, and make standing aside the thing you opt into.
# `http://127.0.0.1:9` is IANA discard — nothing listens, so a connect fails
# fast instead of hanging, and every live-gated test SKIPS exactly as it
# already does on a CI runner with no Weaviate.
#
# Set at IMPORT time (module-scope code resolves the URL during COLLECTION) and
# RE-ESTABLISHED per test by `_pin_weaviate_url` below — for the same reason
# the state-dir redirect is: a suite that sets `WEAVIATE_URL` itself and
# restores by `update(backup)` leaves the key behind for everyone after it.
#
# `VCT_ALLOW_FIXTURE_CLASS_WRITES` rides along: it is the DECLARATION half of
# `vco_lib.fixture_class_guard` — the suite owns the fixture-named classes it
# writes, so the guard must not refuse it. The two are independent legs of one
# containment: this pin means a plain `pytest` cannot REACH a live backend, and
# the guard means an unmarked non-pytest harness cannot WRITE a fixture-named
# class even where it can reach one. Neither leg is a reason to skip the other.
from vco_lib import fixture_class_guard as _fixture_guard  # noqa: E402

#: What `WEAVIATE_URL` was before this file touched it. `None` means the key
#: was absent — which the opt-out branch must reproduce exactly (a live test
#: reading `os.environ.get("WEAVIATE_URL", "http://localhost:8081")` needs the
#: ABSENCE, not an empty string, to reach its own default).
_AMBIENT_WEAVIATE_URL = os.environ.get("WEAVIATE_URL")

if not _ALLOW_REAL_STATE:
    os.environ["WEAVIATE_URL"] = _fixture_guard.UNROUTABLE_SENTINEL_URL
os.environ[_fixture_guard.ALLOW_FIXTURE_WRITES_ENV] = "1"


# Files that isolate the state dir THEMSELVES and must not have `VCT_STATE_DIR`
# pinned over the top of their own mechanism. The redirect is POPPED for these,
# so whatever they set up governs.
#
# Add a file here only with a written rationale, and only when it demonstrably
# cannot reach the real `~/.vct` — a file whose isolation covers SOME of its
# tests does not qualify. Preferred alternative: pin a fixture DB via
# `VCT_LAUNCHER_DB_PATH` (`tests/common/launcher_db_fixture.py`), which
# outranks `VCT_STATE_DIR` and needs no entry here.
#
#   * `test_launcher_db_reader.py` — its `isolated_env` fixture tests the
#     resolver's ``Path.home()`` FALLBACK, which it exercises by faking `$HOME`
#     to `tmp_path` and seeding `$HOME/.vct/launcher.db`. `VCT_STATE_DIR` is
#     an EARLIER tier of the same resolver, so pinning it hides the tier under
#     test. Verified safe: all 22 tests in the file take `isolated_env`, so
#     none can resolve the developer's real home. The sqlite tripwire (which
#     is session-scoped and unaffected by this list) still refuses a write.
_SELF_ISOLATED_STATE_DIR_FILES: frozenset = frozenset({
    "test_launcher_db_reader.py",
})


@pytest.fixture
def child_env() -> dict:
    """Environment for a child Python process with the repo root FIRST on
    ``PYTHONPATH`` (v0.2.92 §3.16). See ``tests/common/child_env.py`` — the
    function form is for unittest-style tests; this fixture is the same dict
    for pytest-style ones. A fresh copy per test: mutate freely."""
    from tests.common.child_env import child_env as _child_env

    return _child_env()


@pytest.fixture(autouse=True)
def _redirect_user_state_dir(request):
    """Re-establish the `~/.vct` redirect for EVERY test (see the block above).

    Re-establishing per test is the load-bearing part: a tearDown doing
    ``os.environ.pop("VCT_STATE_DIR", None)`` would otherwise strip the
    import-time redirect and silently un-protect the rest of the session.
    """
    if _ALLOW_REAL_STATE:
        yield
        return

    # NOTE `VCT_LAUNCHER_DB_PATH` is dropped ONCE at import (above) and NOT
    # re-dropped here. Per-test popping would defeat a MODULE-scoped pin —
    # `tests/test_detect_legacy_kg_collections.py::setUpModule` sets it via
    # `mock.patch.dict`, and pytest runs module-scoped setup BEFORE
    # function-scoped fixtures, so this fixture would clobber it. Safety does
    # not depend on it: `VCT_STATE_DIR` (re-established below) already moves
    # the default, and a leaked value from another test can only point at
    # another tmp path — never at the real DB, which the tripwire refuses.
    keys = {
        "VCT_STATE_DIR": str(_VCO_STATE_REDIRECT),
        "VCT_SECRETS_DIR": str(_VCO_SECRETS_REDIRECT),
    }
    # W-CLAUDE: the `~/.claude` levers are re-established UNCONDITIONALLY, and
    # are deliberately NOT part of the stand-aside set above. The one recorded
    # stand-aside (`test_launcher_db_reader.py`) exists because `VCT_STATE_DIR`
    # is an earlier TIER of the very `Path.home()` resolver that file tests —
    # a reason that is specific to the launcher-db resolver and says nothing
    # about `~/.claude`. Popping these two for it would open a hole in the new
    # guard to buy nothing.
    claude_keys = {
        "VCT_CLAUDE_DIR": str(_VCO_CLAUDE_REDIRECT),
        "VCT_USER_HOME_OVERRIDE": str(_VCO_USER_HOME_REDIRECT),
    }
    self_isolated = (
        request.node.fspath.basename in _SELF_ISOLATED_STATE_DIR_FILES
    )
    prev = {k: os.environ.get(k) for k in (*keys, *claude_keys)}
    if self_isolated:
        # Stand aside DETERMINISTICALLY (pop, don't restore the ambient value)
        # so the file's own isolation is what governs on every machine —
        # including a developer's shell that exports VCT_STATE_DIR.
        for key in keys:
            os.environ.pop(key, None)
    else:
        os.environ.update(keys)
    os.environ.update(claude_keys)
    try:
        yield
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _is_inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` IS ``root`` or lives under it. Never raises.

    ONE home for the containment test both tripwires make (the `~/.vct` sqlite
    guard and the `~/.claude` audit-hook guard). Both run inside code paths
    where an exception would surface as something other than a guard verdict,
    so a filesystem error answers "not inside" rather than propagating.
    """
    try:
        return candidate == root or root in candidate.parents
    except (OSError, ValueError):
        return False


def _drain_and_report(attempts: list, describe) -> None:
    """Drain ``attempts``; raise ``AssertionError`` if it was non-empty.

    The reporting half shared by the two guard fixtures below. Kept as one
    function because the property is identical and load-bearing in both: a
    refusal that production code SWALLOWED (these callers soft-fail on
    ``Exception`` / ``OSError``) must still red the test, or the near-miss
    looks green. Only the message differs, so only the message is a parameter.
    """
    recorded = list(attempts)
    del attempts[:]
    if recorded:
        raise AssertionError(describe(recorded))


class RealUserStateWriteBlocked(RuntimeError):
    """A test tried to open the user's real `~/.vct` DB read-WRITE."""


_real_sqlite3_connect = sqlite3.connect
_state_write_attempts: list = []


def consume_state_write_attempts() -> list:
    """Drain + return the recorded tripwire refusals.

    ONLY for the guard's own tests, which trip it on purpose and must not then
    be failed by ``_fail_test_that_tried_to_write_real_state``.
    """
    attempts = list(_state_write_attempts)
    del _state_write_attempts[:]
    return attempts


def _tripwire_sqlite_connect(database, *args, **kwargs):
    """`sqlite3.connect` wrapper that REFUSES a writable handle on real state.

    Second layer, deliberately independent of the env redirect above: it fires
    even for a path the env cannot steer — a future hardcoded
    ``Path.home() / ".vct" / "launcher.db"`` (that shape already exists, e.g.
    ``templates/scripts/summary_backends.py``), or any caller that bypasses
    ``vco_lib.paths``. Read-only URI handles (``file:...?mode=ro``) pass: they
    cannot corrupt anything, and forbidding them is not this guard's job.

    The refusal is recorded as well as raised, because most production callers
    soft-fail on ``Exception``; the autouse fixture below turns a swallowed
    refusal into a RED test rather than a silent near-miss.
    """
    target = database
    readonly = False
    if isinstance(target, (str, bytes, os.PathLike)):
        text = os.fspath(target)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if text.startswith("file:"):
            body, _, query = text[5:].partition("?")
            readonly = "mode=ro" in query or "immutable=1" in query
            text = body
        try:
            resolved = Path(text).expanduser()
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and not readonly:
            if _is_inside(_REAL_VCT_ROOT, resolved):
                _state_write_attempts.append(str(resolved))
                raise RealUserStateWriteBlocked(
                    f"test opened {resolved} read-WRITE; the suite must never "
                    f"write the user's real launcher state (see tests/conftest.py "
                    f"W-STATE)"
                )
    return _real_sqlite3_connect(database, *args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _install_real_state_sqlite_tripwire():
    """Install the `sqlite3.connect` tripwire for the whole session."""
    if _ALLOW_REAL_STATE:
        yield
        return
    sqlite3.connect = _tripwire_sqlite_connect
    try:
        yield
    finally:
        sqlite3.connect = _real_sqlite3_connect


@pytest.fixture(autouse=True)
def _fail_test_that_tried_to_write_real_state():
    """Turn a SWALLOWED tripwire refusal into a red test.

    Without this, a production soft-fail (``except Exception: return False``)
    would absorb the block and the test would pass green while having tried to
    write the developer's live install — exactly the invisibility that let the
    2026-09-01 incident run for four days.
    """
    del _state_write_attempts[:]
    yield
    _drain_and_report(
        _state_write_attempts,
        lambda attempts: (
            "test attempted a read-WRITE connection to real user state: "
            + ", ".join(sorted(set(attempts)))
        ),
    )


class RealClaudeHomeWriteBlocked(RuntimeError):
    """A test tried to WRITE inside the user's real `~/.claude` (or
    `~/.claude.json`)."""


#: Recorded accesses to the real `~/.claude`, as ``(kind, path)`` where kind is
#: ``"write"`` or ``"read"``. Drained per test by the reporter fixture below.
_claude_access_attempts: list = []

_O_WRITE_MASK = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Audit events that MUTATE. ``open`` is classified per-call (read vs write).
_MUTATION_EVENTS = frozenset({
    "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.link", "os.symlink",
    "os.truncate", "os.chmod",
})
_WATCHED_EVENTS = frozenset({"open"}) | _MUTATION_EVENTS


def consume_claude_access_attempts() -> list:
    """Drain + return the recorded `~/.claude` accesses.

    ONLY for the guard's own tests, which trip it on purpose and must not then
    be failed by ``_fail_test_that_touched_real_claude_home``. Sibling of
    :func:`consume_state_write_attempts`.
    """
    attempts = list(_claude_access_attempts)
    del _claude_access_attempts[:]
    return attempts


def _under_real_claude(raw) -> "Path | None":
    """Resolved path if ``raw`` names the real `~/.claude` tree or
    `~/.claude.json`, else None. Never raises (it runs inside an audit hook)."""
    try:
        text = os.fspath(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str) or ".claude" not in text:
        return None  # fast path: the overwhelming majority of opens
    try:
        resolved = Path(text)
        if not resolved.is_absolute():
            resolved = Path(os.path.abspath(text))
    except (OSError, ValueError):
        return None
    if resolved == _REAL_CLAUDE_JSON or _is_inside(_REAL_CLAUDE_DIR, resolved):
        return resolved
    return None


def _under_real_vct(raw) -> "Path | None":
    """Resolved path if ``raw`` names something inside the real `~/.vct`,
    else None. Never raises (it runs inside an audit hook).

    Sibling of :func:`_under_real_claude`, same shape and same fast path.
    """
    try:
        text = os.fspath(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str) or ".vct" not in text:
        return None  # fast path: the overwhelming majority of opens
    try:
        resolved = Path(text)
        if not resolved.is_absolute():
            resolved = Path(os.path.abspath(text))
    except (OSError, ValueError):
        return None
    return resolved if _is_inside(_REAL_VCT_ROOT, resolved) else None


def _open_is_write(mode, flags) -> bool:
    """Classify an ``open`` audit event as a write.

    ``io.open`` supplies the string mode; ``os.open`` supplies ``mode=None``
    plus the real flags. Directory handles are excluded: opening a directory
    carries O_RDWR-ish flags on some paths and can never append a row.
    """
    if isinstance(flags, int) and flags > 0 and (flags & _O_DIRECTORY):
        return False
    if isinstance(mode, str):
        return any(ch in mode for ch in "wax+")
    if isinstance(flags, int) and flags > 0:
        return bool(flags & _O_WRITE_MASK)
    return False


def _user_state_audit_hook(event: str, args) -> None:
    """Refuse WRITES into the real `~/.claude` or `~/.vct`; record reads of
    the former.

    ONE hook for two roots, because the resource is the same one — a file
    open — and an audit hook runs on EVERY open in the process, so a second
    hook would double that cost to answer a question this one already has
    the path for.

    Second layer for both, deliberately independent of the env redirects
    above — exactly as the `sqlite3.connect` tripwire is for `~/.vct`
    databases. It fires for a path the env cannot steer: a future hardcoded
    ``Path.home() / ".claude" / ...`` (that shape still exists in
    `vco_lib/doctor.py`, `vco_lib/cli/verify_diagrams.py`,
    `claude_mcp_servers/rl_client/rl_logger.py`) or ``Path.home() / ".vct"``
    (that shape exists in `templates/scripts/summary_backends.py`), or any
    caller that bypasses `vco_lib.paths`.

    **Why an audit hook and not monkeypatched symbols.** The thing to hook is
    the RESOURCE, and the file-open resource has several doors:
    ``builtins.open``, ``io.open``, ``pathlib.Path.open`` (which is what
    ``write_text`` / ``write_bytes`` go through and which does NOT see a
    patched ``builtins.open``), ``os.open``, ``os.mkdir``. Patching that set
    by name is the per-symbol bet whose expiry caused the W-STATE incident —
    and it would be re-placed every time CPython or a library adds a door.
    ``sys.addaudithook`` sits under all of them at the C level.

    **Why the `sqlite3.connect` tripwire below still exists.** `~/.vct` now
    has TWO guards, and they cover different doors rather than overlapping:
    launcher state is a SQLite handle that never passes through Python's
    ``open`` at all (sqlite opens the file in C), so only the
    ``sqlite3.connect`` wrapper can see it; everything ELSE under `~/.vct`
    — `hub.token`, `hub.port`, `hub.pid`, `logs/**.jsonl`, the update
    lockfile, `summary_backend_breaker.json` — is an ordinary file that
    only this hook can see. Before v0.2.92 WP-Q2 the second set had no
    guard at all: a hardcoded ``Path.home() / ".vct" / ...`` write would
    have landed in the developer's live install with the DB guard sitting
    right beside it, blind.

    The two SHARE what can be shared — the containment test
    (``_is_inside``), the redirect fixture, the escape hatch, the recorded-
    attempt list (``_state_write_attempts``, so ONE reporter fixture reds a
    swallowed refusal from either door) and the exception type.

    They COULD converge further: CPython also raises a ``sqlite3.connect``
    audit event, so this hook could absorb the DB door too. That means
    rewriting the shipped `~/.vct` tripwire (its own tests assert on the
    monkeypatched ``sqlite3.connect`` identity and on read-only URI
    handling), which is a restructure of a guard landed one lane earlier in
    this same cycle — recorded here as the recommendation, deliberately not
    taken mid-cycle.

    **Reads are recorded, not refused** — a deliberate asymmetry. A read
    cannot damage the user's install, and raising mid-read would send
    production code down a fallback branch, changing what the test proves
    while looking like a guard success. But a read of the real home still
    de-hermeticises (the same test then means something different on the
    maintainer's box and on CI), so it reds the test in teardown with the path
    named. Writes get both: refused AND recorded.

    The recording matters as much as the raising, for the reason W-STATE
    found: most production callers here soft-fail on ``Exception``
    (``_write_failure_jsonl`` catches ``OSError``, ``doctor`` catches
    ``Exception``), so a refusal that was only raised would be swallowed and
    the near-miss would look green.
    """
    if _ALLOW_REAL_STATE or event not in _WATCHED_EVENTS:
        return
    if not args:
        return
    mode = args[1] if event == "open" and len(args) > 1 else None
    flags = args[2] if event == "open" and len(args) > 2 else None
    is_write = event != "open" or _open_is_write(mode, flags)

    resolved = _under_real_claude(args[0])
    if resolved is not None:
        if not is_write:
            _claude_access_attempts.append(("read", str(resolved)))
            return
        _claude_access_attempts.append(("write", str(resolved)))
        raise RealClaudeHomeWriteBlocked(
            f"test tried to write {resolved}; the suite must never write the "
            f"user's real Claude Code state (see tests/conftest.py W-CLAUDE — "
            f"route the path through vco_lib.paths.claude_user_dir())"
        )

    # W-STATE-FILES (v0.2.92 WP-Q2): the same refusal for PLAIN FILES under
    # the real `~/.vct`. The sqlite tripwire below covers `launcher.db` and
    # `hub.db` — a handle sqlite opens in C, which never passes through
    # Python's `open` — but `~/.vct` is not only databases: `hub.token`,
    # `hub.port`, `hub.pid`, `logs/**.jsonl`, the update lockfile and
    # `summary_backend_breaker.json` are ordinary files, and a caller that
    # resolved one of those with a hardcoded `Path.home() / ".vct"` would
    # have written the developer's live install with nothing to stop it.
    # The env redirect (`VCT_STATE_DIR`) is the first layer and steers every
    # caller that goes through `vco_lib.paths`; this is the second layer,
    # for the ones that do not — exactly the two-layer shape W-STATE and
    # W-CLAUDE already use.
    #
    # WRITES ONLY, deliberately — the asymmetry is the OPPOSITE of the
    # `~/.claude` leg above and is not an oversight. `~/.claude` records
    # reads because a test that branches on the maintainer's real global
    # config means something different on CI. `~/.vct` reads are routine
    # and harmless by construction: conftest itself reads `hub.port` /
    # `hub.token` at import to decide a module-level skip, and the sqlite
    # guard already lets read-only DB handles through for the same reason.
    # Recording them would red dozens of tests for no incident.
    resolved = _under_real_vct(args[0])
    if resolved is None or not is_write:
        return
    _state_write_attempts.append(str(resolved))
    raise RealUserStateWriteBlocked(
        f"test tried to write {resolved}; the suite must never write the "
        f"user's real launcher state (see tests/conftest.py W-STATE-FILES — "
        f"route the path through vco_lib.paths.vct_root_dir())"
    )


# Installed at IMPORT, not from a fixture, for two reasons: collection-time
# module-scope code already resolves these paths (six test files import
# `query_code_graph.py`, which reads `mcp-config.json` at module import), and
# `sys.addaudithook` is one-way by design — CPython has no removal API,
# because an audit hook a caller could pop would not be a guard. The
# `_ALLOW_REAL_STATE` check therefore lives INSIDE the hook rather than around
# its installation.
sys.addaudithook(_user_state_audit_hook)


#: Test files whose READ of the real `~/.claude` is owned by an in-flight work
#: package in this same cycle, keyed to the reason and the exact fix. Same
#: idiom (and same rule) as ``PENDING_MIGRATION`` in
#: ``tests/test_v0291_no_bare_prints_in_rust_crates.py``: **MUST be empty at
#: release-tag time** — the no-deferred-fixes rule applies to this list like
#: any other backlog.
#:
#: WRITES are NEVER exempt: an entry here only downgrades a de-hermeticising
#: READ from red to reported. The audit hook still refuses every write.
_CLAUDE_READ_PENDING_OWNER: dict = {
    # EMPTY, and must stay empty at release-tag time.
    #
    # The one historical entry (test_v0291_dogfood_deferral_selfclear.py) was
    # owed by vco_lib/doctor.py, which read `Path.home() / ".claude.json"`
    # inline so the VCT_USER_HOME_OVERRIDE redirect could not steer it. That
    # fix has LANDED (doctor.py resolves via vco_lib.paths.user_home()), so the
    # entry was deleted exactly as its own text instructed.
    #
    # An entry here downgrades the REAL-`~/.claude` READ guard to a warning for
    # one test file. The WRITE leg is never downgraded. Add one only with the
    # exact fix and its owner named in the reason string, and delete it in the
    # same change that lands the fix.
}


@pytest.fixture(autouse=True)
def _fail_test_that_touched_real_claude_home(request):
    """Turn a swallowed refusal — or any read of the real `~/.claude` — red.

    Sibling of ``_fail_test_that_tried_to_write_real_state``; kept separate
    rather than generalised because the two report different resources with
    different remediations, and that fixture is driven by hand by its own test.
    They share the drain-and-raise half (``_drain_and_report``).
    """
    del _claude_access_attempts[:]
    yield
    pending = request.node.fspath.basename in _CLAUDE_READ_PENDING_OWNER
    if pending:
        # Reported, not raised: the write leg is untouched, so the leak this
        # guard exists for is still hard-blocked for these files too.
        leftover = [a for a in _claude_access_attempts if a[0] != "read"]
        reads = [a for a in _claude_access_attempts if a[0] == "read"]
        del _claude_access_attempts[:]
        if reads:
            warnings.warn(
                f"{request.node.nodeid} read the real ~/.claude "
                f"({', '.join(sorted({p for _k, p in reads}))}) — known "
                f"pending item, see _CLAUDE_READ_PENDING_OWNER in "
                f"tests/conftest.py",
                UserWarning, stacklevel=1,
            )
        _claude_access_attempts.extend(leftover)
    _drain_and_report(_claude_access_attempts, _describe_claude_accesses)


def _describe_claude_accesses(attempts: list) -> str:
    writes = sorted({p for kind, p in attempts if kind == "write"})
    reads = sorted({p for kind, p in attempts if kind == "read"})
    detail = []
    if writes:
        detail.append("wrote: " + ", ".join(writes))
    if reads:
        detail.append("read: " + ", ".join(reads))
    return (
        "test touched the user's real ~/.claude (" + "; ".join(detail)
        + ") — route the path through vco_lib.paths.claude_user_dir() "
        "(or vco_lib.paths.user_home() for ~/.claude.json) so the conftest "
        "redirect can steer it"
    )


# ─── W-RL (v0.2.92, register item 28): the RL corpus home is STEERABLE ───
#
# The two guards above are RUNTIME tripwires: they fire when a test opens a
# file. This one is STRUCTURAL, and it exists because the leak it closes could
# never have tripped them.
#
# `RLDataLogger.DEFAULT_DIR` was `Path.home() / ".claude" / "retrieval_rl_data"`
# evaluated in the CLASS BODY, i.e. once at import. Two consequences, both
# measured before the fix:
#
#   * no override steered it — with `VCT_CLAUDE_DIR` *and* `VCT_STATE_DIR` both
#     pointing at temp dirs, it still resolved to the maintainer's real
#     `~/.claude/retrieval_rl_data`; and
#   * constructing `RLDataLogger()` with no arguments `mkdir(parents=True)`s
#     that directory, so the first test (or MCP subprocess, or paid-module
#     caller) to take the default would have created it under the real home.
#
# The audit hook would have caught the mkdir, but only if something took the
# default — nothing in the tree does today, so the loaded gun sat there
# silently. A guard that only fires on use is not enough for a default; this
# one asserts the RESOLVED VALUE itself, once per session, whether or not any
# test constructs a logger.
#
# It must also survive the thing that made the original bug invisible: the
# value being frozen at import. Asserting containment is exactly what proves
# it is not — the conftest redirect is established at conftest import, which
# happens AFTER `rl_logger` may already have been imported by a test module,
# so a frozen value cannot be inside the redirect. That is why this assertion
# bites rather than being a tautology.


class RealRlDataHomeNotRedirected(AssertionError):
    """`RLDataLogger`'s default corpus dir escaped the suite's redirect."""


def assert_rl_data_home_is_redirected(resolved: Path) -> None:
    """Raise unless ``resolved`` is inside the suite's ``~/.vct`` redirect.

    Split out from the fixture as a pure decision so its own test can drive it
    with the pre-v0.2.92 value and prove it FAILS — a guard nobody has watched
    fail is a guard nobody can trust (the W-STATE lesson: an isolation claim
    that expired silently).

    Cross-OS: pure ``Path`` containment via ``_is_inside``; no separator
    literal, no ``~`` expansion, no ``os.sep``. It compares against the
    redirect the conftest itself created, so it is correct on Windows, macOS
    and Linux without a per-OS branch.
    """
    if _is_inside(_VCO_STATE_REDIRECT, resolved):
        return
    raise RealRlDataHomeNotRedirected(
        f"RLDataLogger's default RL-data directory resolved to {resolved}, "
        f"which is outside the suite redirect {_VCO_STATE_REDIRECT}. It must "
        f"resolve LAZILY through vco_lib.paths.vct_root_dir() (see "
        f"claude_mcp_servers/rl_client/rl_logger.py::default_rl_data_dir). A "
        f"value computed in the class body freezes at import and no redirect "
        f"can steer it — that was register item 28, and it created "
        f"~/.claude/retrieval_rl_data on the real home."
    )


@pytest.fixture(scope="session", autouse=True)
def _guard_rl_data_home_is_redirected():
    """Assert the RL corpus default is steerable, once per session.

    Session-scoped and lazily importing, rather than run at conftest import:
    `claude_mcp_servers.rl_client` pulls in `pydantic` via its package
    `__init__`, and a missing optional dependency should fail the tests that
    need it, not collection of the entire suite.
    """
    if _ALLOW_REAL_STATE:
        yield
        return
    from claude_mcp_servers.rl_client.rl_logger import RLDataLogger

    assert_rl_data_home_is_redirected(RLDataLogger.DEFAULT_DIR)
    assert_rl_data_home_is_redirected(RLDataLogger.DEFAULT_PATH.parent)
    yield


def _weaviate_importable(python_exe: str) -> bool:
    """True iff `python_exe -c 'import weaviate'` succeeds. Soft: any spawn
    error (missing interpreter, timeout) is treated as not-importable."""
    try:
        r = subprocess.run(
            [python_exe, "-c", "import weaviate"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


@functools.lru_cache(maxsize=None)
def _resolve_analyzer_python_cached(vct_venv: str, install_root: str) -> str | None:
    """Resolution body, memoized on the env inputs that steer it (so a test that
    manipulates ``$VCT_VENV`` / ``$VCT_INSTALL_ROOT`` gets a fresh resolution
    rather than a stale cached path). The subprocess ``import weaviate`` probe is
    the expensive part; caching per-env avoids re-spawning it across the several
    analyzer-spawning tests in one run."""
    candidates: list[str] = []

    def _venv_pythons(venv_dir: Path) -> list[str]:
        return [
            str(venv_dir / "bin" / "python"),
            str(venv_dir / "bin" / "python3"),
            str(venv_dir / "Scripts" / "python.exe"),
        ]

    if vct_venv:
        candidates.extend(_venv_pythons(Path(vct_venv)))
        candidates.append(vct_venv)  # in case $VCT_VENV is the python itself

    if install_root:
        candidates.extend(_venv_pythons(Path(install_root) / ".venv"))

    candidates.extend(_venv_pythons(_REPO_ROOT / ".venv"))

    for cand in candidates:
        if Path(cand).exists() and _weaviate_importable(cand):
            return cand

    # Last resort: the pytest runner's own interpreter, but ONLY if it can
    # import weaviate (the canonical case — the suite runs under the VCO venv).
    if _weaviate_importable(sys.executable):
        return sys.executable

    return None


def resolve_analyzer_python() -> str | None:
    """v0.2.84 PLAN-v0284 (review T-2, one-concern-one-home): resolve the Python
    interpreter that tests must use to spawn ``analyze_code_graph.py`` and other
    scripts that ``import weaviate`` at module load.

    Bare ``shutil.which("python3")`` resolves the SYSTEM python, which on most
    machines (and CI without a global weaviate-client) fails the analyzer's
    module-level ``import weaviate`` — the analyzer then exits 1 with
    "weaviate-client not installed" BEFORE reaching the G5 worktree guard, so the
    g5-guard tests observed the wrong exit/stderr (false red).

    Resolution order (mirrors ``templates/hooks/_lib/resolve-vco-venv.sh`` tiers
    1→2→4, the canonical VCO-venv chain), returning the FIRST candidate whose
    interpreter can ``import weaviate``:

      1. ``$VCT_VENV`` explicit override — ``$VCT_VENV/bin/python`` (POSIX) /
         ``$VCT_VENV/Scripts/python.exe`` (Windows).
      2. ``$VCT_INSTALL_ROOT/.venv`` — launcher-provided canonical venv.
      3. ``<repo>/.venv`` — orchestrator-clone fallback.
      4. ``sys.executable`` — the interpreter running pytest, IF it can import
         weaviate (the suite is meant to run under the VCO venv, so this is the
         common hit).

    Returns the resolved interpreter path, or ``None`` when NONE can import
    weaviate — callers must ``pytest.skip`` with a clear reason rather than
    spawn a python that will crash at import and yield a misleading result.
    """
    return _resolve_analyzer_python_cached(
        os.environ.get("VCT_VENV", "").strip(),
        os.environ.get("VCT_INSTALL_ROOT", "").strip(),
    )


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_tracked_files_against_install_pollution():
    """Restore the repo's tracked ``CLAUDE.md`` + remove a repo-root
    ``UPDATE_DEFERRED.md`` after the test session.

    Several tests run the real ``install.py --update`` as a subprocess.
    install.py's orchestrator-self step materializes
    ``<install_root>/CLAUDE.md`` and the deferral flow writes
    ``<install_root>/.claude/context/UPDATE_DEFERRED.md`` — and ``install_root``
    resolves to the directory install.py LIVES in (this repo), regardless of
    the subprocess cwd. So those tests splice a ``vco-deferral-reminder`` block
    into the repo's TRACKED ``CLAUDE.md`` and drop a (gitignored)
    ``UPDATE_DEFERRED.md``. The ``CLAUDE.md`` mutation is the real hazard: it is
    a tracked file, so a later ``git add -A`` could commit install cruft into
    the public repo (which is NOT an installed clone and must carry no install
    artifacts).

    This session-scoped guard snapshots both at session start and restores /
    removes them at session end, so the suite never leaves the repo dirty —
    independent of WHICH test pollutes (current or future). Best practice for
    new tests remains: run install.py with ``--skip-materialize-claude-dir`` or
    target a tmp install root. Soft-fail: cleanup errors never fail the session.
    """
    repo_root = Path(__file__).resolve().parent.parent
    claude_md = repo_root / "CLAUDE.md"
    deferred = repo_root / ".claude" / "context" / "UPDATE_DEFERRED.md"

    claude_before = claude_md.read_bytes() if claude_md.is_file() else None
    deferred_existed = deferred.is_file()

    try:
        yield
    finally:
        try:
            if claude_before is not None:
                if not claude_md.is_file() or claude_md.read_bytes() != claude_before:
                    claude_md.write_bytes(claude_before)
            elif claude_md.is_file():
                claude_md.unlink()  # didn't exist before the session
        except OSError:
            pass
        try:
            if not deferred_existed and deferred.is_file():
                deferred.unlink()
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _rootless_embedding_root_never_resolves_the_checkout():
    """PREVENT the pollution the fixture above only repairs.

    ``EmbeddingService.for_project()`` ends by reconciling the deferral ledger
    of the root it resolved — ``_clear_failure_deferral`` on success,
    ``_write_failure_deferral`` on failure — and reconciling a ledger REWRITES
    ``<root>/CLAUDE.md`` (entries ⇒ the ``vco-deferral-reminder`` block is
    spliced in; none ⇒ it is stripped). ``_detect_project_root`` falls back to
    ``Path.cwd()`` when that directory holds a ``.claude/``, and pytest's cwd
    is this checkout — whose ``CLAUDE.md`` is TRACKED.

    So every rootless ``for_project()`` anywhere in the suite edited a
    versioned file mid-run. The guard above restores it at session END, which
    is why a completed run looked clean and only an in-flight ``git status``
    (or a killed run) showed ``M CLAUDE.md``. Repairing at the end is not the
    same as not doing it: a suite that is interrupted, or read concurrently by
    another agent's ``git status``, still sees the damage.

    ONE home rather than N call sites, deliberately: the offenders are not a
    fixed list. Because the strip is idempotent, only the FIRST of them is
    observable in any given run — so "fix the file the watcher named" would
    have to be repeated for an unknown number of rounds and could never be
    proven complete. Routing the ROOTLESS answer is complete by construction.

    An EXPLICIT ``project_root=`` is delegated to the real resolver untouched,
    so every test that passes one still exercises real resolution. A test that
    needs the genuine rootless behaviour captures the function at import time
    (see ``tests/test_v0294_no_test_writes_repo_root_claude_md.py``), which
    predates this patch.

    An in-process patch cannot reach a CHILD; children are contained at the
    seams that build their environment, not here. ``tests/common/child_env.py``
    pins ``KG_BASE_DIR`` at a throwaway directory — the key
    ``_detect_project_root`` consults FIRST, ahead of ``$VCT_ORCHESTRATOR_ROOT``,
    so the import pin that helper exists for is untouched. Exporting the same
    key from THIS fixture was tried and reverted: it is read by more than the
    project-root resolver, and ``tests/test_v0289_kg_sync_project_root.py``
    pins its precedence against a subprocess — a session-wide value makes those
    assertions untestable.

    Two cases the ``KG_BASE_DIR`` lever cannot reach, each handled at its call
    site: a child handed an EXPLICIT root (``analyze_code_graph.py`` passes
    ``$VCT_ORCHESTRATOR_ROOT`` straight into ``for_project()``, and an explicit
    argument outranks both env vars), and a child spawned with a hand-built env
    that never went through ``child_env``.
    """
    from unittest.mock import patch

    import vco_lib.embedding_service as _es_mod

    sentinel = Path(tempfile.mkdtemp(prefix="vco-rootless-project-root-"))
    _real_detect = _es_mod._detect_project_root

    def _detect_into_sentinel(explicit=None):
        return _real_detect(explicit) if explicit is not None else sentinel

    with patch.object(_es_mod, "_detect_project_root", _detect_into_sentinel):
        yield


# Test files that explicitly exercise the hub-resolver and MUST run with
# the gate UNSET (so `vco_lib.project_config.resolve` reaches its HTTP
# probe + their mock-patches actually fire). The autouse fixture below
# clears the env var for these files; sets it for everyone else.
_RESOLVER_OPT_OUT_FILES = frozenset({
    # Tests that mock or call resolve() directly and need the production
    # code path to NOT short-circuit:
    "test_caller_migration_step18.py",
    "test_project_resolution.py",
    "test_project_config.py",       # v0.2.46: also exercises resolve() directly
})


@pytest.fixture(autouse=True)
def _disable_hub_resolver_in_tests(request):
    """Force ``_try_resolve_project_config`` to fall through to env-only
    resolution for tests that DON'T explicitly exercise the resolver.

    The KG / access-list / diagrams / shared-KG cluster (~26 tests across
    6+ files) needs env-only resolution to keep their injected env vars
    intact. The resolver-test cluster (`test_caller_migration_step18.py`,
    `test_project_resolution.py`, `test_project_config.py`) needs the
    resolver enabled so their ``mock.patch("vco_lib.project_config.
    resolve", ...)`` calls have an effect. We discriminate by test file
    name; the opt-out list above is the canonical record of "tests that
    test the hub-resolver itself".

    The fixture restores the prior env state in its finally block (so a
    test that sets the var itself isn't broken by this fixture).
    """
    test_file = request.node.fspath.basename
    if test_file in _RESOLVER_OPT_OUT_FILES:
        # Resolver tests: ensure the env var is NOT set so the production
        # code's guard doesn't short-circuit.
        prev = os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
        try:
            yield
        finally:
            if prev is not None:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev
    else:
        # Default path: env-only resolution. Set the var if it wasn't
        # already; restore the prior value (which may be None) afterward.
        prev = os.environ.get("VCT_DISABLE_HUB_RESOLVER")
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
            else:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev


# Test files that EXPLICITLY exercise the real hub poster (``post_rl_event`` /
# ``post_rl_prune``) against a mock HTTP server and assert the POST happens — the
# WP-R hermeticity guard must be OFF for these so their mock-hub round-trip fires.
# This is the sibling of ``_RESOLVER_OPT_OUT_FILES`` for the hub-write axis; keep
# it explicit so an accidentally-broken poster test surfaces loudly.
_RL_HUB_WRITE_OPT_OUT_FILES = frozenset({
    "test_v0247_hub_writer.py",  # directly tests post_rl_event/post_rl_prune HTTP
})


@pytest.fixture(autouse=True)
def _disable_rl_hub_writes_in_tests(request):
    """WP-R (2026-07-22): make RL telemetry hermetic — no test may write into the
    real ``~/.vct/launcher.db`` ``rl_events`` table via vct-hub.

    Root cause this closes: several suites drive a REAL code-search entry point
    (``search_code_graph`` MCP tool, ``CodeGraphQuery.search_by_concept`` CLI)
    with only Weaviate + the embedder stubbed. Those entry points call the
    SHARED retrieval-telemetry emitter, which — when a local launcher hub is
    running and ``RL_LOCAL_LOGGING_DISABLED`` is unset (the default in a bare
    test run) — POSTs the fixture event to the live hub. Result: FIXTURE junk
    (dim 3/4 codesage ``code_hook``/``code_search`` events with titles like
    ``a.f`` / ``b.g`` / ``mod.self_fn``) landed in the live ``rl_events`` table
    on EVERY test run — thousands of accumulated junk rows plus a fresh trickle
    each run on any install where the hub is up during tests.

    The structural fix is hermeticity: this autouse fixture sets
    ``RL_HUB_POST_DISABLED=1`` for the WHOLE suite, which the sole real-hub
    poster (``claude_mcp_servers.rl_client.hub_writer`` — both ``post_rl_event``
    and ``post_rl_prune``) honours by short-circuiting to its "event lost"
    soft-fail return. Tests that WANT to assert the emitted envelope already
    inject a fake ``hub_post_fn`` into ``RLTelemetryWriter`` (e.g. the G3 / R2-11
    / telemetry-triple suites) — those are unaffected because they never reach
    the default poster. This mirrors the sibling
    ``_disable_hub_resolver_in_tests`` convention (an autouse env-hardening
    fixture is how this repo isolates the hub from tests).

    Belt-and-braces: ``hub_writer`` ALSO checks ``PYTEST_CURRENT_TEST`` directly,
    so any test that reaches the poster is covered even if this fixture were
    somehow bypassed. Because that leg is unconditional under pytest, the files
    in ``_RL_HUB_WRITE_OPT_OUT_FILES`` — which DO want to exercise the real
    poster against a mock hub — receive a ``VCT_HUB_ALLOW_TEST_POST=1`` sentinel
    that ``hub_writer._in_test_context`` treats as an explicit override, so those
    (and only those) files bypass BOTH legs. The fixture restores the prior env
    in ``finally`` so a test that sets the vars itself isn't clobbered.
    """
    test_file = request.node.fspath.basename
    if test_file in _RL_HUB_WRITE_OPT_OUT_FILES:
        # Poster-under-test: clear the disable AND set the explicit override so
        # the PYTEST_CURRENT_TEST leg is neutralised for this file's mock-hub
        # round-trip assertions.
        prev_disable = os.environ.pop("RL_HUB_POST_DISABLED", None)
        prev_allow = os.environ.get("VCT_HUB_ALLOW_TEST_POST")
        os.environ["VCT_HUB_ALLOW_TEST_POST"] = "1"
        try:
            yield
        finally:
            if prev_disable is not None:
                os.environ["RL_HUB_POST_DISABLED"] = prev_disable
            if prev_allow is None:
                os.environ.pop("VCT_HUB_ALLOW_TEST_POST", None)
            else:
                os.environ["VCT_HUB_ALLOW_TEST_POST"] = prev_allow
        return
    prev = os.environ.get("RL_HUB_POST_DISABLED")
    os.environ["RL_HUB_POST_DISABLED"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("RL_HUB_POST_DISABLED", None)
        else:
            os.environ["RL_HUB_POST_DISABLED"] = prev


# Test files that MUST see the AMBIENT `WEAVIATE_URL` — the live-backend tests
# a shipped gate runs on purpose. The W-WEAVIATE pin stands aside for these;
# every other file gets the unroutable sentinel. Sibling of
# `_RESOLVER_OPT_OUT_FILES` for the backend-URL axis, kept explicit so a live
# gate that stops reaching its backend surfaces here rather than silently.
#
# Add a file only when a shipped gate or CI job invokes it AGAINST a real
# backend, and only when it creates and drops its OWN uniquely-named classes.
#
#   * `test_v0246_v46b_live_ci10_diff_gate.py` — `scripts/pre-ship-check.sh`
#     Section 5 runs it with the ambient env ("live diff-gate fetches stored
#     hashes correctly", "live prune deletes stale rows"). Pinned over, it
#     would SKIP, pytest would exit 0, and the gate would print PASS for a run
#     that asserted nothing. Creates `V46BTestDiffGate<hex>` /
#     `V46BTestPrune<hex>` in setUp, drops them in tearDown.
#   * `test_v0246_kg_sync_live.py` — the sibling that file's docstring names:
#     the same round-trip against `vco_lib.kg_sync`, same create/drop
#     discipline.
#   * `test_codegraph_retrieval_quality_smoke.py` — `installer-smoke.yml`'s X-2
#     step runs it with `WEAVIATE_URL=http://localhost:8081` against the job's
#     own Weaviate. Read-only (queries); it creates nothing.
#   * `test_v0294_live_weaviate_optin_canary.py` — NOT a live test. It is the
#     wiring proof that this stand-aside actually fires: a list nobody can
#     observe is a list that can silently stop working. It asserts the process
#     sees the ambient value and touches no backend.
#
# Deliberately NOT here — the live-CAPABLE files no shipped gate invokes:
# `test_weaviate_schema.py::LiveSchemaMigrationTest` (creates + drops
# `VCO218SchemaTest`), `test_weaviate_tombstone_skip_on_unchanged_vector.py`
# (`VcoD2ScratchTombstone`), `test_vco_lib_migrate.py::LiveMigrateIntegration`,
# and the code-graph live-gated set (`test_analyze_code_graph_retry_cap.py`,
# `test_codegraph_hook_gates_v0270.py`, `test_codegraph_cli_readpath_v0270.py`,
# `test_codegraph_single_file_scope.py`, `test_embedding_enrichment.py`'s live
# class). They skip cleanly without a backend — which is what they already do
# on a CI runner — and several of them CREATE classes on whatever instance they
# find. Measured: before this pin, three of them were writing scratch classes
# into the maintainer's live Weaviate on every full-suite run, reaching it
# through the popped-`WEAVIATE_URL` leak described above even when the
# command line named the sentinel. Containing them by default is the point.
# A developer who wants them live runs with `VCO_TEST_ALLOW_REAL_STATE=1`, the
# one hatch for "run against my real install".
_LIVE_WEAVIATE_OPT_OUT_FILES: frozenset = frozenset({
    "test_v0246_v46b_live_ci10_diff_gate.py",
    "test_v0246_kg_sync_live.py",
    "test_codegraph_retrieval_quality_smoke.py",
    "test_v0294_live_weaviate_optin_canary.py",
})


def _weaviate_url_pin_for(test_file: str) -> "str | None":
    """The value `WEAVIATE_URL` must carry for *test_file*; `None` = remove it.

    Pure and importable, so both branches are provable without a backend
    (`tests/test_v0294_fixture_class_guard.py` drives it directly).
    """
    if _ALLOW_REAL_STATE or test_file in _LIVE_WEAVIATE_OPT_OUT_FILES:
        return _AMBIENT_WEAVIATE_URL
    return _fixture_guard.UNROUTABLE_SENTINEL_URL


@pytest.fixture(autouse=True)
def _pin_weaviate_url(request):
    """W-WEAVIATE: re-establish the backend pin for EVERY test.

    The import-time assignment covers collection; this covers the rest of the
    session, because a suite that sets `WEAVIATE_URL` itself and restores with
    `os.environ.update(backup)` cannot remove a key the backup lacked — so
    without this, one such test un-pins every test that follows it. That is
    not hypothetical: it is half of how the incident happened.

    `VCT_ALLOW_FIXTURE_CLASS_WRITES` is re-established unconditionally,
    including for the opt-out files: they are still tests, and they still own
    whatever classes they create.
    """
    prev_url = os.environ.get("WEAVIATE_URL")
    prev_allow = os.environ.get(_fixture_guard.ALLOW_FIXTURE_WRITES_ENV)
    target = _weaviate_url_pin_for(request.node.fspath.basename)
    if target is None:
        os.environ.pop("WEAVIATE_URL", None)
    else:
        os.environ["WEAVIATE_URL"] = target
    os.environ[_fixture_guard.ALLOW_FIXTURE_WRITES_ENV] = "1"
    try:
        yield
    finally:
        if prev_url is None:
            os.environ.pop("WEAVIATE_URL", None)
        else:
            os.environ["WEAVIATE_URL"] = prev_url
        if prev_allow is None:
            os.environ.pop(_fixture_guard.ALLOW_FIXTURE_WRITES_ENV, None)
        else:
            os.environ[_fixture_guard.ALLOW_FIXTURE_WRITES_ENV] = prev_allow


# Test files that EXPLICITLY exercise `spawn_background_resync`'s launch path
# and assert `status == "launched"` (with `subprocess.Popen` faked). The P5 gate
# below must be OFF for these, or they would all see the new `skipped` status.
# Sibling of `_RL_HUB_WRITE_OPT_OUT_FILES` for the resync-spawn axis; kept
# explicit so an accidentally-broken spawn test surfaces loudly.
_RESYNC_SPAWN_OPT_OUT_FILES = frozenset({
    "test_codegraph_embed_revision_resync.py",
    "test_codegraph_metadata_producers_v0273.py",
    "test_codegraph_resync_v0273.py",
    "test_codegraph_spawn_identity_v0282.py",
    "test_v0272_pregate_audit_fixes.py",
    "test_v0283_embed_resync_selfclear_pin.py",
    "test_v0284_identity_sweep.py",
    # v0.2.94: the spawn-seam guard. It asserts BOTH halves — that a healthy
    # interpreter still reaches `launched` with a `-m` argv, and that a broken
    # one is refused BEFORE any Popen — so the P5 kill-switch (which
    # short-circuits to `skipped` ahead of both) must be off for it.
    "test_v0294_python_exe_spawn_guard.py",
})


@pytest.fixture(autouse=True)
def _seed_python_exe_preflight():
    """v0.2.94: record, for the preflight memo, what THIS process already proved.

    ``vco_lib.python_exe.preflight`` gates every real spawn on "can this
    interpreter import ``vco_lib`` + ``weaviate``", and answers it by running a
    fresh child. Two reasons that child must not run inside the suite:

    * it is REDUNDANT for ``sys.executable`` — pytest already imported
      ``vco_lib`` with this very interpreter, and ``weaviate-client`` is a
      ``requirements.txt`` dependency of the same environment. The answer is
      known before the probe starts;
    * dozens of spawn tests monkeypatch ``subprocess.Popen`` module-wide, which
      ``subprocess.run`` (and therefore the probe) would then walk into.

    This is NOT a bypass of the guard. It seeds ONE fact about ONE interpreter;
    any other interpreter — notably the deliberately-broken fakes in
    ``tests/test_v0294_python_exe_spawn_guard.py`` — is probed for real, which is
    what keeps the red-proof honest.

    FUNCTION-scoped, not session-scoped: the two ``test_v0294_python_exe_*``
    files clear the memo in their own ``setUp``/cleanup (they must, to measure
    real verdicts), and a session-scoped seed would be wiped by the first of
    them and absent for every later test in the run — green in isolation, red in
    the full suite. Re-seeding per test costs one dict write.
    """
    from vco_lib import python_exe as _px

    _px._PREFLIGHT_CACHE[
        (
            sys.executable,
            _px.DEFAULT_REQUIRED_MODULES,
            _px._preflight_env_fingerprint(),
        )
    ] = (True, "")
    yield


@pytest.fixture(autouse=True)
def _disable_resync_spawn_in_tests(request):
    """P5 (v0.2.91): no test may spawn a background codegraph resync driver, or
    deposit its spawn log in the user's real ``~/.vct/logs/``.

    Root cause this closes: `spawn_background_resync` opens a per-spawn log file
    under `<vct_root_dir>/logs/resync-<project>-<ts>.log` and writes its header
    BEFORE the `subprocess.Popen` call — so even the many tests that correctly
    fake `Popen` still littered the PRODUCTION state dir (the perf audit counted
    17 `resync-TProj-*.log` files in one day). Tests that do NOT fake `Popen`
    additionally spawned real detached venv children.

    The gate (`VCT_RESYNC_SPAWN_DISABLED`) short-circuits the function before the
    log file and before any `Popen`, returning `status="skipped"`. Files in
    `_RESYNC_SPAWN_OPT_OUT_FILES` — which assert the launch path itself — get the
    gate CLEARED plus `VCT_STATE_DIR` pointed at a per-session temp dir, so their
    log headers land in tmp instead of `~/.vct/logs/`. Restores prior env in
    ``finally`` so a test that sets the vars itself isn't clobbered.

    NOTE (v0.2.92 W-STATE): `VCT_STATE_DIR` is now redirected for the WHOLE
    suite by `_redirect_user_state_dir`, so this fixture's own redirect is
    belt-and-braces rather than the only thing keeping these seven files out of
    `~/.vct/logs/`. Kept because it pins the spawn axis explicitly (and because
    a file removed from the list below must not silently lose the redirect).
    """
    key = "VCT_RESYNC_SPAWN_DISABLED"
    test_file = request.node.fspath.basename
    if test_file in _RESYNC_SPAWN_OPT_OUT_FILES:
        prev_gate = os.environ.pop(key, None)
        prev_state = os.environ.get("VCT_STATE_DIR")
        spawn_state = _VCO_TEST_STATE / "resync_state"
        spawn_state.mkdir(parents=True, exist_ok=True)
        os.environ["VCT_STATE_DIR"] = str(spawn_state)
        try:
            yield
        finally:
            if prev_gate is not None:
                os.environ[key] = prev_gate
            if prev_state is None:
                os.environ.pop("VCT_STATE_DIR", None)
            else:
                os.environ["VCT_STATE_DIR"] = prev_state
        return
    prev = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev
