# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 R6 — the weaviate-kg MCP warns when it writes from an unregistered folder.

The ruling (owner, 2026-09-14) REVISES the 2026-06-08 Q1 ruling: the gate's
empty-``VCT_PROJECT_ID`` branch still ALLOWS the write, but it no longer stays
silent about it. SB1's two surfaces — the ``dropped_writes.jsonl`` row and the
``UPDATE_DEFERRED.md`` entry — are both after-the-fact, so the field case had a
user writing knowledge nodes from a folder VCO had never installed into and
finding out only when a later read came back empty.

The field case had TWO faces, and the tests below drive both, because which one
a user meets is decided by something they cannot see (whether the hub answered):

  * the write SUCCEEDS into a fallback collection nothing in that folder reads;
  * the write FAILS with "could not find class <bundled default>", because with
    no hub answer ``KG_COLLECTION`` falls through to its bundled default, a
    class that exists in no Weaviate.

Both are "this folder is not registered", so both carry the same warning and
the same remedy (the launcher's Adopt flow).

Hermetic: the real ``store_knowledge_node`` body runs against the fake Weaviate
client from ``tests/test_v0273_kg_write_path.py`` (reused, not re-copied), and
``tests/conftest.py`` pins ``WEAVIATE_URL`` at the unroutable sentinel. Nothing
here can reach a live backend.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.deferral_dismissal import MANIFEST_REL  # noqa: E402

# Reuse the established write-path harness rather than growing a second copy of
# the fake client (CLAUDE.md § "search before you add, extract before you
# duplicate"). `_patch_server_for_store` stubs the two SB1 emitters out; the
# no-regression test below puts the REAL ones back deliberately.
from tests.test_v0273_kg_write_path import (  # noqa: E402
    _FakeCollection,
    _patch_server_for_store,
    _server,
    _store,
)

#: Stems used here are already classified by
#: `tests/test_v0294_fixture_class_guard.py::_UNTABLED_STEMS_SNAPSHOT`.
UNREGISTERED_TARGET = "MyKG_KnowledgeGraph"
#: The MCP's bundled `KG_COLLECTION` default — the class the field report's
#: "could not find class ClaudeKnowledgeGraph" names. No `_`-family suffix, so
#: it is not a fixture-shaped class name.
BUNDLED_DEFAULT = "ClaudeKnowledgeGraph"


# ─── helpers ───────────────────────────────────────────────────────────


def _register(folder: Path) -> Path:
    """Give *folder* the bundle manifest — i.e. make it a VCO-installed project."""
    manifest = folder / MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    return manifest


def _patch(monkeypatch, tmp_path, coll, *, real_sb1=False, collection=UNREGISTERED_TARGET):
    """Harness + R6-specific pins.

    ``real_sb1=True`` restores the two SB1 writers the shared harness stubs, so
    the no-regression test drives the surfaces that already shipped.
    """
    srv = _server()
    real_metric = srv._emit_gate_skipped_metric
    real_deferral = srv._emit_gate_skipped_deferral

    _patch_server_for_store(monkeypatch, tmp_path, coll)
    if real_sb1:
        monkeypatch.setattr(srv, "_emit_gate_skipped_metric", real_metric)
        monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", real_deferral)

    # The folder this write comes from (conftest pins CLAUDE_PROJECT_DIR at a
    # scratch dir for the whole suite; every test here names its own). The
    # module-load snapshot moves with it: a real MCP serving this folder was
    # SPAWNED for it, so leaving the snapshot behind would trip the v0.2.74
    # workspace-drift backstop instead of the code under test.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(srv, "KG_COLLECTION", collection)
    monkeypatch.setattr(srv, "WEAVIATE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("VCT_SESSION_ID", "r6-session")

    srv._UNREGISTERED_FOLDER_SESSIONS_LOGGED.clear()
    srv._GATE_SKIPPED_SESSIONS_SEEN.clear()
    return srv


# ═══════════════════════════════════════════════════════════════════════
# 1. The rule itself — `unregistered_folder_reason`, both ways
# ═══════════════════════════════════════════════════════════════════════


def test_reason_is_none_when_the_gate_can_name_the_project(tmp_path):
    """A resolvable ``VCT_PROJECT_ID`` ends the question: the Phase-8 gate
    identifies this project, so nothing about the folder is in doubt."""
    srv = _server()
    assert srv.unregistered_folder_reason(tmp_path, {"VCT_PROJECT_ID": "uuid-1"}) is None


def test_reason_names_the_missing_manifest_and_the_folder(tmp_path):
    srv = _server()
    reason = srv.unregistered_folder_reason(tmp_path, {})
    assert reason is not None
    assert MANIFEST_REL.as_posix() in reason, "the reason must name what is missing"
    assert str(tmp_path) in reason, "the reason must name WHERE it looked"


def test_a_manifest_without_the_env_var_counts_as_REGISTERED(tmp_path):
    """PINNED DECISION (read alongside the SB1 comments in server.py).

    A folder with `.claude/.vco-manifest.json` but no `VCT_PROJECT_ID` is a
    project VCO DID install into, whose `.claude/env` predates v0.2.49 or whose
    launcher never seeded the id. SB1's own remediation — `install.py --update`
    / the launcher Identity tab — already covers it, and Adopt would be wrong
    advice (the folder is already adopted). So this case gets SB1's two
    surfaces and NOT the R6 warning; blurring the two would send half the users
    to the wrong remedy.
    """
    _register(tmp_path)
    srv = _server()
    assert srv.unregistered_folder_reason(tmp_path, {}) is None


def test_reason_says_so_when_no_folder_can_be_resolved():
    srv = _server()
    reason = srv.unregistered_folder_reason(None, {})
    assert reason is not None
    assert "no project root" in reason
    assert "CLAUDE_PROJECT_DIR" in reason and "KG_BASE_DIR" in reason


def test_blank_project_id_is_treated_as_absent(tmp_path):
    """The gate's own empty-PID branch fires on a blank value, so this rule
    must agree with it — a whitespace id would otherwise silence the warning
    while the gate stayed skipped."""
    srv = _server()
    assert srv.unregistered_folder_reason(tmp_path, {"VCT_PROJECT_ID": "  "}) is not None


def test_the_manifest_path_is_the_shared_constant_not_a_respelling():
    """One home: the MCP must not carry its own spelling of the manifest path.
    `vco_lib.deferral_dismissal.MANIFEST_REL` is already pinned to
    `project_init`'s copy by tests/test_deferral_dismissal_memory_v0291.py."""
    srv = _server()
    assert srv._BUNDLE_MANIFEST_REL == MANIFEST_REL


# ═══════════════════════════════════════════════════════════════════════
# 2. At WRITE time — the warning rides the tool result
# ═══════════════════════════════════════════════════════════════════════


def test_unregistered_write_warns_and_still_writes(monkeypatch, tmp_path):
    """The ruling changed VISIBILITY, not the allow: the node must still land."""
    coll = _FakeCollection()
    _patch(monkeypatch, tmp_path, coll)

    result = _store(_server())

    assert result["success"] is True
    assert [ev for ev, _ in coll.event_log if ev == "insert"], "the write must still happen"
    assert len(coll.objects) == 1
    assert result["file_written"] is True

    warning = result.get("warning")
    assert warning, "an unregistered-folder write must carry a warning field"
    assert UNREGISTERED_TARGET in warning, "the warning must NAME the collection"
    assert "NOT registered with VCO" in warning
    assert "Adopt this folder" in warning, "the warning must point at the remedy"
    assert MANIFEST_REL.as_posix() in warning


def test_warning_names_the_shared_collection_when_that_is_the_target(
    monkeypatch, tmp_path
):
    """It must name the collection the write ACTUALLY landed in, not the
    project default — a scope='shared' write goes somewhere else."""
    coll = _FakeCollection()
    srv = _patch(monkeypatch, tmp_path, coll)
    monkeypatch.setattr(srv, "SHARED_KG_COLLECTION", "TeamWide_KnowledgeGraph")
    monkeypatch.setattr(srv, "_resolve_shared_kg_write_disabled", lambda: False)

    result = _store(srv, scope="shared")

    assert result["success"] is True
    assert "TeamWide_KnowledgeGraph" in result["warning"]
    assert UNREGISTERED_TARGET not in result["warning"]


def test_no_warning_when_vct_project_id_is_set(monkeypatch, tmp_path):
    import vco_lib.access_resolver as access_resolver

    coll = _FakeCollection()
    srv = _patch(monkeypatch, tmp_path, coll)
    monkeypatch.setenv("VCT_PROJECT_ID", "11111111-2222-3333-4444-555555555555")
    # With an id present the Phase-8 matrix gate is LIVE; pin its verdict so
    # this test isolates the warning rather than the matrix (and so nothing
    # reaches for a hub).
    monkeypatch.setattr(access_resolver, "check_access_level", lambda *_a, **_k: "write")

    result = _store(srv)

    assert result["success"] is True
    assert "warning" not in result, (
        "an identified project must not be told its folder is unregistered"
    )


def test_no_warning_when_the_folder_carries_a_manifest(monkeypatch, tmp_path):
    """Registered-but-id-less: SB1's surfaces still fire, the R6 warning does not."""
    _register(tmp_path)
    coll = _FakeCollection()
    _patch(monkeypatch, tmp_path, coll)

    result = _store(_server())

    assert result["success"] is True
    assert "warning" not in result


# ═══════════════════════════════════════════════════════════════════════
# 3. Dedup discipline — the LOG speaks once, the JSON field every time
# ═══════════════════════════════════════════════════════════════════════


def test_log_fires_once_per_session_but_the_field_rides_every_write(
    monkeypatch, tmp_path, caplog
):
    coll = _FakeCollection()
    srv = _patch(monkeypatch, tmp_path, coll)

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        first = _store(srv, title="First", file_path="knowledge/concepts/first.md")
        second = _store(srv, title="Second", file_path="knowledge/concepts/second.md")

    logged = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "NOT registered with VCO" in r.getMessage()
    ]
    assert len(logged) == 1, (
        f"the warning must be logged ONCE per session; got {len(logged)} lines"
    )
    # The JSON field is not deduped: the second write is exactly where an agent
    # that missed the first line needs to be told again.
    assert first.get("warning") and second.get("warning")


def test_a_new_session_logs_again(monkeypatch, tmp_path, caplog):
    """Dedup is per SESSION, not per process lifetime — a fresh session id must
    re-arm the line, or a long-lived MCP silences every later session."""
    coll = _FakeCollection()
    srv = _patch(monkeypatch, tmp_path, coll)

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        _store(srv, title="First", file_path="knowledge/concepts/first.md")
        monkeypatch.setenv("VCT_SESSION_ID", "r6-session-2")
        _store(srv, title="Second", file_path="knowledge/concepts/second.md")

    logged = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "NOT registered with VCO" in r.getMessage()
    ]
    assert len(logged) == 2


def test_the_two_dedups_key_on_the_same_session_identity(monkeypatch):
    """One home: SB1's deferral dedup and the R6 log dedup must not disagree
    about what "this session" means."""
    srv = _server()
    monkeypatch.setenv("VCT_SESSION_ID", "explicit")
    assert srv._session_key() == "explicit"
    monkeypatch.delenv("VCT_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-claude")
    assert srv._session_key() == "from-claude"
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert srv._session_key().startswith("pid:")


# ═══════════════════════════════════════════════════════════════════════
# 4. No regression: SB1's two surfaces still fire, unchanged
# ═══════════════════════════════════════════════════════════════════════


def test_sb1_metric_and_deferral_still_written_exactly_as_before(
    monkeypatch, tmp_path
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    coll = _FakeCollection()
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    _patch(monkeypatch, tmp_path, coll, real_sb1=True)

    result = _store(_server())
    assert result["success"] is True

    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert len(rows) == 1
    assert rows[0]["reason"] == "gate_skipped_no_project_id"
    assert rows[0]["project_id"] == ""
    assert rows[0]["collection"] == UNREGISTERED_TARGET
    assert rows[0]["fail_open"] is True

    deferred = (tmp_path / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(
        encoding="utf-8"
    )
    assert "gate_skipped_no_project_id" in deferred
    assert UNREGISTERED_TARGET in deferred
    assert "install.py --update" in deferred
    assert "Launcher GUI" in deferred


# ═══════════════════════════════════════════════════════════════════════
# 5. The OTHER branch — "could not find class <bundled default>"
# ═══════════════════════════════════════════════════════════════════════


class _RaisingCollection(_FakeCollection):
    """A collection whose insert fails the way Weaviate fails on a class that
    was never created — the shape an unregistered folder produces once the hub
    answers nothing and ``KG_COLLECTION`` falls to its bundled default."""

    def __init__(self, message: str):
        super().__init__()
        self._message = message

        class _Raising:
            def __init__(self, msg, coll):
                self._msg = msg
                self._coll = coll

            def insert(self, properties=None, vector=None):
                raise RuntimeError(self._msg)

            def delete_by_id(self, uid):
                self._coll.objects = [
                    o for o in self._coll.objects if o.uuid != uid
                ]

        self.data = _Raising(message, self)


def test_class_not_found_write_carries_the_warning_not_a_bare_error(
    monkeypatch, tmp_path
):
    coll = _RaisingCollection(f"could not find class {BUNDLED_DEFAULT}")
    _patch(monkeypatch, tmp_path, coll, collection=BUNDLED_DEFAULT)

    result = _store(_server())

    assert result["success"] is False
    assert BUNDLED_DEFAULT in result["error"], "the raw cause stays visible"
    warning = result.get("warning")
    assert warning, (
        "a class-not-found failure from an unregistered folder must say WHY — "
        "bare, it reads as a schema problem and sends the user to migrate a "
        "collection they do not own"
    )
    assert "Adopt this folder" in warning
    assert BUNDLED_DEFAULT in warning


def test_a_registered_folders_failure_stays_a_bare_error(monkeypatch, tmp_path):
    """Both sides: the failure payload must not grow an Adopt nudge for a
    project that IS registered."""
    _register(tmp_path)
    coll = _RaisingCollection("some other weaviate failure")
    _patch(monkeypatch, tmp_path, coll)

    result = _store(_server())

    assert result["success"] is False
    assert "warning" not in result


def test_schema_hint_points_at_adopt_only_from_an_unregistered_folder(
    monkeypatch, tmp_path
):
    """The read-side hint for the same class-not-found message. Registered:
    unchanged (`install.py --update`). Unregistered: plus the real remedy."""
    srv = _server()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    exc = Exception(f"could not find class {BUNDLED_DEFAULT}")

    unregistered_hint = srv._build_schema_error_hint(exc, str(exc).lower())
    assert "install.py --update" in unregistered_hint, "the prior advice survives"
    assert "Adopt this folder" in unregistered_hint
    assert "This search targeted" in unregistered_hint, (
        "a read must not be described as a write"
    )

    _register(tmp_path)
    registered_hint = srv._build_schema_error_hint(exc, str(exc).lower())
    assert "Adopt this folder" not in registered_hint
    assert "install.py --update" in registered_hint


# ═══════════════════════════════════════════════════════════════════════
# 6. The warning text never breaks the write
# ═══════════════════════════════════════════════════════════════════════


def test_a_broken_resolver_cannot_break_the_write(monkeypatch, tmp_path):
    """Soft-fail: a warning that could raise would be a worse defect than the
    silence it replaces."""
    coll = _FakeCollection()
    srv = _patch(monkeypatch, tmp_path, coll)

    def _boom():
        raise OSError("resolver exploded")

    monkeypatch.setattr(srv, "_resolve_project_root_for_deferral", _boom)

    result = _store(srv)

    assert result["success"] is True
    assert "warning" not in result


@pytest.mark.parametrize("operation,opener", [
    ("write", "This write went to"),
    ("search", "This search targeted"),
    ("nonsense", "This write went to"),  # unknown surface falls back, never KeyErrors
])
def test_the_text_states_the_surface_it_is_describing(operation, opener):
    srv = _server()
    text = srv.unregistered_folder_warning_text(
        UNREGISTERED_TARGET, "because", operation=operation
    )
    assert text.startswith(opener)
    assert UNREGISTERED_TARGET in text


def test_the_wording_lives_beside_the_refusal_text_not_in_the_mcp():
    """One home for one vocabulary family (v0.2.95 cleanup).

    The refusal and this warning are the same defect one severity apart — an
    unmarked process writing where nothing will read it — so they are authored
    in the same module. IDENTITY, not equality: a copy in `server.py` that
    happened to produce the same string today would satisfy an equality check
    and drift tomorrow, which is the exact failure this pins against.
    """
    from vco_lib import fixture_class_guard as fcg

    srv = _server()
    assert srv.unregistered_folder_warning_text is fcg.unregistered_folder_warning_text
    # The sibling it is shaped after is still there, still separate: that one
    # REFUSES, this one ALLOWS and says so. Collapsing them would be wrong.
    assert callable(fcg.refusal_text)
    incident = "70 real knowledge nodes"
    assert incident in fcg.refusal_text("Alpha_KnowledgeGraph", "Alpha")
    assert incident in fcg.unregistered_folder_warning_text(
        UNREGISTERED_TARGET, "because"
    ), "both texts must cite the one incident they are both about"


def test_the_text_names_the_manifest_from_the_one_home():
    """The remedy sentence names the file Adopt creates — from the shared
    constant, so a rename cannot leave the user chasing a path VCO no longer
    writes."""
    from vco_lib import fixture_class_guard as fcg
    from vco_lib.manifest_paths import MANIFEST_REL_POSIX

    text = fcg.unregistered_folder_warning_text(UNREGISTERED_TARGET, "because")
    assert MANIFEST_REL_POSIX in text
    assert MANIFEST_REL_POSIX == MANIFEST_REL.as_posix()
