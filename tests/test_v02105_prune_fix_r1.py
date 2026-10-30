# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 review fix-round 1 (lane prune) — the destructive paths.

* The removed-extras purge acts on POSITIVE knowledge only: the hub's
  ``code_graph_extra_paths_gone`` (a disabled row, or a removed row with its
  audit entry). An enabled list the hub emptied on a DB error (gone = None),
  an older hub, a hub outage, or a stored source nobody registered as
  removed purges nothing.
* A purge with failures does not record the sweep as done: the next refresh
  retries it.
* The lost-extras detection runs at session start even when the
  UPDATE_DEFERRED ledger is EMPTY (both hook siblings, real subprocesses).
* ``ProjectConfig.code_graph_extra_paths_gone`` is tri-state.

Pure fakes for Weaviate and the hub; the hook tests run the shipped hook
scripts with a wrapper interpreter whose ``sitecustomize`` stubs the
Weaviate-touching detection. ``WEAVIATE_URL`` is pinned to the dead port.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import vco_lib.codegraph_extras_refresh as xrf
import vco_lib.codegraph_resync as cr
import vco_lib.codegraph_source_purge as sp

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"


class _Extra:
    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.last_indexed_commit = None


def _cfg(enabled=(), gone=(), prefix="P", moved=()):
    return SimpleNamespace(
        code_graph_collection_prefix=prefix,
        project_id="pid",
        code_graph_extra_paths=tuple(_Extra(p) for p in enabled),
        code_graph_extra_paths_gone=None if gone is None else tuple(gone),
        code_graph_moved_from_sources=None if moved is None else tuple(moved),
    )


class _Purger:
    def __init__(self, failures: int = 0):
        self.calls: list = []
        self.failures = failures

    def __call__(self, prefix, src):
        self.calls.append((prefix, src))
        return {"counts": {f"{prefix}_CodeModule": 3}, "failures": self.failures}


@pytest.fixture(autouse=True)
def _primary(monkeypatch):
    monkeypatch.setattr(cr, "primary_sources_for", lambda root: {"/proj"})


def _sweep(tmp_path, cfg, purger, **kw):
    return sp.purge_gone_extra_paths(
        "/proj", tmp_path / "state", resolve=lambda r: cfg, purge=purger,
        log=kw.pop("log", lambda m: None), **kw)


# ---------------------------------------------------------------------------
# HIGH 1 — positive knowledge only
# ---------------------------------------------------------------------------


def test_error_shaped_empty_extras_list_purges_nothing(tmp_path):
    """THE incident class: the hub failed to read its DB, served the enabled
    list EMPTY and the gone list as unknown. Nothing may be purged — not
    the enabled extra whose rows are stored, nothing at all."""
    purger = _Purger()
    logs: list = []
    out = _sweep(tmp_path, _cfg(enabled=(), gone=None), purger,
                 log=logs.append)
    assert out == {} and purger.calls == []
    assert any(line.startswith(sp.SKIP_PREFIX) for line in logs)
    assert not list((tmp_path / "state").glob("codegraph_extra_keep_*.json")), (
        "no stamp: nothing was swept")


def test_positively_gone_path_is_purged(tmp_path):
    """ACT: a path the hub reports gone (disabled / removed with audit)."""
    purger = _Purger()
    out = _sweep(tmp_path, _cfg(enabled=("/clone",), gone=("/removed",)),
                 purger)
    assert purger.calls == [("P", "/removed")]
    assert set(out) == {"/removed"}


def test_unregistered_stored_source_is_kept(tmp_path):
    """LEAVE-ALONE (review LOW: an old project root after a move, or any
    source nobody registered as removed): with no positive knowledge the
    sweep never enumerates stored sources to guess — the rows stay."""
    purger = _Purger()
    out = _sweep(tmp_path, _cfg(enabled=("/clone",), gone=()), purger)
    assert out == {} and purger.calls == []


def test_gone_path_that_is_enabled_or_primary_is_never_purged(tmp_path):
    """A gone list that (by a race) still names an enabled extra or the
    primary root's form never purges it: the keep set wins."""
    purger = _Purger()
    _sweep(tmp_path, _cfg(enabled=("/clone",), gone=("/clone", "/proj")),
           purger)
    assert purger.calls == []


def test_older_hub_without_the_field_purges_nothing(tmp_path):
    purger = _Purger()
    cfg = SimpleNamespace(code_graph_collection_prefix="P",
                          code_graph_extra_paths=())
    out = sp.purge_gone_extra_paths(
        "/proj", tmp_path / "state", resolve=lambda r: cfg, purge=purger,
        log=lambda m: None)
    assert out == {} and purger.calls == []


def test_refresh_extras_with_error_shaped_config_purges_nothing(tmp_path):
    """End to end through the Stop-drain refresh entry point."""
    purger = _Purger()
    deps = xrf.Deps(resolve=lambda r: _cfg(enabled=(), gone=None),
                    git_head=lambda p: None, purge=purger)
    xrf.refresh_extras("/proj", "/a.py", "/py", tmp_path / "state",
                       lambda m: None, deps=deps)
    assert purger.calls == []


# ---------------------------------------------------------------------------
# MEDIUM 4 — a partial purge stays owed
# ---------------------------------------------------------------------------


def test_failed_purge_does_not_stamp_and_retries(tmp_path):
    cfg = _cfg(enabled=("/clone",), gone=("/removed",))
    failing = _Purger(failures=2)
    _sweep(tmp_path, cfg, failing)
    assert failing.calls == [("P", "/removed")]
    assert not list((tmp_path / "state").glob("codegraph_extra_keep_*.json")), (
        "a purge with failures must not record the sweep as done")
    # The next refresh re-runs it (same keep/gone sets).
    ok = _Purger()
    _sweep(tmp_path, cfg, ok)
    assert ok.calls == [("P", "/removed")], "the owed sweep re-runs"
    # Now it is complete: the stamp short-circuits an unchanged pass.
    again = _Purger()
    _sweep(tmp_path, cfg, again)
    assert again.calls == []
    # …unless forced (the launcher's Remove / Disable flow).
    forced = _Purger()
    _sweep(tmp_path, cfg, forced, force=True)
    assert forced.calls == [("P", "/removed")]


def test_raising_purge_counts_as_failure(tmp_path):
    def _boom(prefix, src):
        raise RuntimeError("weaviate went away")

    out = _sweep(tmp_path, _cfg(gone=("/removed",)), _boom)
    assert out["/removed"]["failures"] == 1
    assert not list((tmp_path / "state").glob("codegraph_extra_keep_*.json"))


def test_changed_gone_set_sweeps_again(tmp_path):
    _sweep(tmp_path, _cfg(gone=("/a",)), _Purger())
    p2 = _Purger()
    _sweep(tmp_path, _cfg(gone=("/a", "/b")), p2)
    assert ("P", "/b") in p2.calls


def test_purge_rows_uses_a_narrowing_filter_but_decides_in_python(monkeypatch):
    """The per-source purge narrows the read with a project_source filter
    (cost) but the delete stays the exact Python compare."""
    seen = {}

    def _fake_delete(coll, path_prop, match_fn, **kw):
        seen.setdefault("kw", []).append(kw)
        return 0, 0

    monkeypatch.setattr(cr, "delete_file_rows_exact", _fake_delete)
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")

    class _Coll:
        name = "P_X"

    class _Client:
        class collections:  # noqa: N801 — mirror the client attribute
            @staticmethod
            def exists(name):
                return True

            @staticmethod
            def get(name):
                return _Coll()

    monkeypatch.setattr(cr, "_open_collection",
                        lambda client, name, ctx: (_Coll(), "ok"))
    sp.purge_rows_for_project_source("P", "/removed", client=_Client())
    assert seen["kw"], "every collection went through the exact delete"
    for kw in seen["kw"]:
        assert kw["project_source"] == "/removed"
        assert "narrow_filter" in kw


# ---------------------------------------------------------------------------
# The CLI the launcher calls
# ---------------------------------------------------------------------------


def test_cli_reports_refusal_and_counts(tmp_path, monkeypatch, capsys):
    import vco_lib.project_config as pc

    monkeypatch.setattr(pc, "resolve", lambda root: _cfg(gone=None))
    assert sp.main(["--project-root", str(tmp_path), "--force", "--json"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is True and reply["purged"] == {}
    assert "could not say" in reply["refused"]

    monkeypatch.setattr(pc, "resolve", lambda root: _cfg(gone=("/removed",)))
    monkeypatch.setattr(sp, "purge_rows_for_project_source", _Purger())
    assert sp.main(["--project-root", str(tmp_path), "--force", "--json"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["purged"] == {"/removed": 3}
    assert reply["refused"] is None and reply["failures"] == 0


# ---------------------------------------------------------------------------
# The resolver field is tri-state
# ---------------------------------------------------------------------------


def _hub_body(**extra):
    from vco_lib import project_config as pc

    body = {
        "schema_version": 1, "project_id": "pid", "project_path": "/proj",
        "project_slug": "s", "project_display_name": "S",
        "code_graph_project": "s", "code_graph_collection_prefix": "S",
        "kg_collection": "S_KG", "shared_kg_collection": "S_KG",
        "development_collection": "S_Dev", "active_embedding": "qwen3",
        "embedding_models": {"text": "t", "code": "c"},
        "kg_access_list": [], "codegraph_access_list": [],
        "weaviate_url": "http://127.0.0.1:9", "ollama_url": "",
        "grpc_port": 9, "shared_kg_write_disabled": False,
    }
    body.update(extra)
    return pc._from_hub_body(body)


def test_project_config_gone_field_is_tri_state():
    assert _hub_body().code_graph_extra_paths_gone is None, "absent = unknown"
    assert _hub_body(code_graph_extra_paths_gone=None
                     ).code_graph_extra_paths_gone is None, "null = unknown"
    assert _hub_body(code_graph_extra_paths_gone=[]
                     ).code_graph_extra_paths_gone == (), "[] = none removed"
    assert _hub_body(code_graph_extra_paths_gone=["/a"]
                     ).code_graph_extra_paths_gone == ("/a",)


def test_project_config_moved_from_field_is_tri_state():
    """v0.2.105 fix round: the moved-from list follows the same tri-state —
    absent/null is UNKNOWN (the purge keeps every row), ``[]`` is "this
    project never moved"."""
    assert _hub_body().code_graph_moved_from_sources is None, "absent = unknown"
    assert _hub_body(code_graph_moved_from_sources=None
                     ).code_graph_moved_from_sources is None, "null = unknown"
    assert _hub_body(code_graph_moved_from_sources=[]
                     ).code_graph_moved_from_sources == (), "[] = never moved"
    assert _hub_body(code_graph_moved_from_sources=["/old"]
                     ).code_graph_moved_from_sources == ("/old",)


# ---------------------------------------------------------------------------
# v0.2.105 fix round (review NIT) — the rows of a folder the project MOVED
# away from are purged, once the current folder really holds rows
# ---------------------------------------------------------------------------


def _count(rows_by_source: dict, *, unknown: bool = False):
    def _fn(prefix, src):
        if unknown:
            return None
        return rows_by_source.get(src, 0)

    return _fn


def test_a_completed_move_purges_the_old_folders_rows(tmp_path):
    """ACT: the launcher's move ledger records that the project used to live
    at /old, so its rows are positive knowledge and get removed. RED-PROOF:
    without the moved-from leg the sweep purges nothing."""
    purger = _Purger()
    out = _sweep(tmp_path, _cfg(moved=("/old",)), purger,
                 count_rows=_count({"/proj": 12}))
    assert purger.calls == [("P", "/old")]
    assert set(out) == {"/old"}


def test_a_moved_from_source_is_kept_until_the_current_root_has_rows(tmp_path):
    """LEAVE-ALONE: a row's UUID mixes ``project_source``, so a walk of the
    NEW root writes new rows rather than rewriting the old ones. Purging the
    old root before that would leave the project with an empty graph — the
    rows stay until the destination holds rows."""
    purger = _Purger()
    logs: list = []
    out = _sweep(tmp_path, _cfg(moved=("/old",)), purger,
                 count_rows=_count({}), log=logs.append)
    assert purger.calls == [] and out == {}
    assert any("moved-from deferred" in ln for ln in logs), logs
    # …and an UNDETERMINABLE count is not "has rows" either.
    out2 = _sweep(tmp_path, _cfg(moved=("/old",)), _Purger(),
                  count_rows=_count({}, unknown=True))
    assert out2 == {}


def test_an_unknown_moved_from_list_purges_nothing(tmp_path):
    """A hub that could not read its DB (or is older than the field) proves
    nothing, exactly like the gone list — the rows stay."""
    purger = _Purger()
    logs: list = []
    out = _sweep(tmp_path, _cfg(moved=None), purger,
                 count_rows=_count({"/proj": 12}), log=logs.append)
    assert purger.calls == [] and out == {}
    assert any("moved-from unknown" in ln for ln in logs), logs
    # Unknown is reported as a plain line, never as the CLI's `refused` (the
    # gone sweep was authorized and ran).
    assert not any(ln.startswith(sp.SKIP_PREFIX) for ln in logs), logs


def test_a_moved_from_source_that_is_still_enabled_is_kept(tmp_path):
    """A move back, or an old folder re-added as an extra code folder: the
    keep set wins — its rows are live."""
    purger = _Purger()
    _sweep(tmp_path, _cfg(enabled=("/old",), moved=("/old",)), purger,
           count_rows=_count({"/proj": 12}))
    assert purger.calls == []


def test_the_primary_root_is_never_a_moved_from_target(tmp_path, monkeypatch):
    """A move BACK to the primary root records the root it came from; the
    live root itself is in the keep set and is never purged."""
    monkeypatch.setattr(cr, "primary_sources_for", lambda root: {"/proj", "/old"})
    purger = _Purger()
    _sweep(tmp_path, _cfg(moved=("/proj", "/old")), purger,
           count_rows=_count({"/proj": 5}))
    assert purger.calls == []


def test_a_new_move_sweeps_again(tmp_path):
    """The stamp keys on the moved list too, so a second move is not masked
    by the first sweep's stamp."""
    _sweep(tmp_path, _cfg(moved=("/old",)), _Purger(),
           count_rows=_count({"/proj": 1}))
    purger = _Purger()
    _sweep(tmp_path, _cfg(moved=("/old", "/older")), purger,
           count_rows=_count({"/proj": 1}))
    assert ("P", "/older") in purger.calls


# ---------------------------------------------------------------------------
# HIGH 2 — the session-start detection runs on an EMPTY ledger (both hooks)
# ---------------------------------------------------------------------------


_SITECUSTOMIZE = '''
import os
from pathlib import Path

import vco_lib.codegraph_extras_refresh as _xrf
import vco_lib.deferral_retry as _dr

_marker = Path(os.environ["VCO_TEST_MARKER"])


def _detect(project_root):
    _marker.write_text("detected", encoding="utf-8")
    entry = _xrf.build_missing_extras_deferral(
        project_root, "P", ["/media/clone"])
    from vco_lib.deferral_emit import emit
    emit(Path(project_root), entry, keep_first_detected=True)
    return ["/media/clone"]


_xrf.detect_and_emit_missing_extras = _detect
_dr.spawn_detached = lambda *a, **k: True
'''


def _wrapper_venv(tmp_path: Path) -> Path:
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    wrapper = venv / "bin" / "python"
    pythonpath = os.pathsep.join(
        [str(stub), str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")])
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{pythonpath}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                  | stat.S_IXOTH)
    return venv


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude" / "context").mkdir(parents=True)
    return project


def _env(tmp_path: Path, project: Path, venv: Path) -> dict:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "VCT_VENV": str(venv),
        "VCO_TEST_MARKER": str(tmp_path / "marker"),
        "WEAVIATE_URL": "http://127.0.0.1:9",
        "VCT_HUB_PORT": "9",
        "VCT_STATE_DIR": str(tmp_path / "vct-state"),
    }


def _assert_detected_and_surfaced(tmp_path: Path, project: Path, out: str):
    assert (tmp_path / "marker").exists(), (
        "the lost-extras detection must run although the ledger was EMPTY")
    assert xrf.MISSING_EXTRAS_CONDITION_ID in out, (
        "the entry the detection just registered is surfaced this session; "
        f"stdout={out!r}")
    ledger = project / ".claude" / "context" / "UPDATE_DEFERRED.json"
    assert ledger.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_sh_hook_detects_lost_extras_on_an_empty_ledger(tmp_path):
    project = _project(tmp_path)
    venv = _wrapper_venv(tmp_path)
    res = subprocess.run(
        ["bash", str(HOOKS / "session-start-deferral-surface.sh")],
        env=_env(tmp_path, project, venv), capture_output=True, text=True,
        timeout=120, cwd=str(tmp_path),
    )
    assert res.returncode == 0, res.stderr
    _assert_detected_and_surfaced(tmp_path, project, res.stdout)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_ps1_hook_detects_lost_extras_on_an_empty_ledger(tmp_path):
    project = _project(tmp_path)
    venv = _wrapper_venv(tmp_path)
    res = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File",
         str(HOOKS / "session-start-deferral-surface.ps1")],
        env=_env(tmp_path, project, venv), capture_output=True, text=True,
        timeout=180, cwd=str(tmp_path),
    )
    assert res.returncode == 0, res.stderr
    _assert_detected_and_surfaced(tmp_path, project, res.stdout)


def test_empty_ledger_with_nothing_detected_stays_silent(tmp_path):
    """LEAVE-ALONE: no entries and nothing detected → no output (the hook's
    silent no-op is kept)."""
    if shutil.which("bash") is None:
        pytest.skip("bash required")
    project = _project(tmp_path)
    venv = _wrapper_venv(tmp_path)
    # Override the stub: detect nothing.
    (tmp_path / "stub" / "sitecustomize.py").write_text(
        "import vco_lib.codegraph_extras_refresh as _x\n"
        "import vco_lib.deferral_retry as _d\n"
        "_x.detect_and_emit_missing_extras = lambda root: []\n"
        "_d.spawn_detached = lambda *a, **k: True\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(HOOKS / "session-start-deferral-surface.sh")],
        env=_env(tmp_path, project, venv), capture_output=True, text=True,
        timeout=120, cwd=str(tmp_path),
    )
    assert res.returncode == 0
    assert res.stdout.strip() == ""
