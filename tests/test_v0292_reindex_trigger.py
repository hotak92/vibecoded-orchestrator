# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the extractor-generation TRIGGER and its bundle-update wiring.

The trigger is the half that must never hurt anyone: it runs inside
``install_project_bundle``, i.e. on the update path the launcher drives for
every project. So the properties pinned here are mostly about restraint —

* it launches a DETACHED walk and returns, with no wall-clock deadline;
* it bypasses the embed-revision owed-probe, which is structurally blind to
  this axis (after an extractor-only fix every row IS at the current
  revision, so the probe correctly answers "nothing owed" and would refuse);
* it degrades to a DeferralEntry when the code-embed service is down;
* a failure anywhere becomes a warning, never an aborted update;
* it says nothing and does nothing for a project that does not owe the work.
"""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from vco_lib import codegraph_extractor_generation as ceg


# ---------------------------------------------------------------------------
# A spawner stand-in that records what it was asked to do
# ---------------------------------------------------------------------------
class RecordingSpawn:
    def __init__(self, status="launched", pid=4242, message="", deferral=None):
        self.calls = []
        self.env_at_call = []
        self._result = types.SimpleNamespace(
            status=status, pid=pid, message=message, deferral=deferral)

    def __call__(self, repo_root, project_name, **kwargs):
        self.calls.append((repo_root, project_name, kwargs))
        # Capture the env AS THE CHILD WOULD INHERIT IT.
        self.env_at_call.append(os.environ.get(ceg.FORCE_REWALK_ENV))
        return self._result


def _probe_graph_present(names, _url):
    return {n: True for n in names}


def _probe_graph_absent(names, _url):
    return {n: False for n in names}


def _ensure(tmp_path, **kw):
    base = dict(prev_version="0.2.91", running_version="0.2.92",
                project_name="DemoProj", probe=_probe_graph_present)
    base.update(kw)
    return ceg.ensure_extractor_generation(tmp_path, **base)


# ===========================================================================
# ACT — an owed project launches the walk
# ===========================================================================
def test_owed_project_launches_a_background_walk(tmp_path: Path):
    spawn = RecordingSpawn()
    out = _ensure(tmp_path, spawn=spawn)

    assert out.status == "launched"
    assert out.pid == 4242
    assert len(spawn.calls) == 1
    repo_root, project, kwargs = spawn.calls[0]
    assert Path(repo_root) == tmp_path
    assert project == "DemoProj"


def test_the_spawn_bypasses_the_embed_revision_owed_probe(tmp_path: Path):
    """The crux of why this could not just reuse the P7 resync trigger.

    ``spawn_background_resync``'s default gate counts rows whose stored
    ``embed_revision`` is stale. An extractor-only fix leaves every row at the
    current revision, so that probe answers "not owed" and NOTHING spawns. Our
    detector has already established the work is owed on a different axis, so
    the probe is bypassed — not weakened, and only for this call.
    """
    spawn = RecordingSpawn()
    _ensure(tmp_path, spawn=spawn)
    assert spawn.calls[0][2]["check_owed"] is False


def test_the_force_rewalk_decision_reaches_the_child_as_argv(tmp_path: Path):
    """The child is two spawn hops away and this process builds neither argv.

    v0.2.92: the decision travels as an EXPLICIT ``force_rewalk=True`` kwarg,
    which `spawn_background_resync` forwards to the `--run-resync` driver and
    the driver forwards to the analyzer. It previously rode ambient env, which
    worked but made the decision invisible at every seam it crossed and
    inheritable by anything else this process later spawned.
    """
    spawn = RecordingSpawn()
    _ensure(tmp_path, spawn=spawn)
    assert spawn.calls[0][2]["force_rewalk"] is True


def test_the_trigger_never_mutates_the_environment(tmp_path: Path, monkeypatch):
    """Replaces three env-restore tests that would now pass VACUOUSLY.

    Those tests asserted the env was set at the spawn and restored afterwards.
    With argv transport we never touch the env at all, so "restored correctly"
    is trivially true and no longer evidence of anything. The property they
    collectively protected — nothing downstream inherits a force-rewalk it did
    not ask for — is now structural, and this pins it directly: a pre-existing
    user value survives untouched, an unset var stays unset, and both hold even
    when the spawn raises.
    """
    monkeypatch.setenv(ceg.FORCE_REWALK_ENV, "user-set-value")
    _ensure(tmp_path, spawn=RecordingSpawn())
    assert os.environ[ceg.FORCE_REWALK_ENV] == "user-set-value"

    monkeypatch.delenv(ceg.FORCE_REWALK_ENV, raising=False)
    _ensure(tmp_path, spawn=RecordingSpawn())
    assert ceg.FORCE_REWALK_ENV not in os.environ

    def _boom(*_a, **_kw):
        raise RuntimeError("Popen exploded")

    out = _ensure(tmp_path, spawn=_boom)
    assert out.status == "failed"
    assert ceg.FORCE_REWALK_ENV not in os.environ


def test_the_env_var_remains_supported_for_manual_invocation(tmp_path: Path):
    """`FORCE_REWALK_ENV` is not dead: `resolve_force_rewalk` still honours it
    so a human can force a rewalk by hand. Only the TRIGGER stopped relying on
    inheritance."""
    assert ceg.resolve_force_rewalk(False, {ceg.FORCE_REWALK_ENV: "1"}) is True
    assert ceg.resolve_force_rewalk(False, {}) is False
    assert ceg.resolve_force_rewalk(True, {}) is True
    assert ceg.FORCE_REWALK_ENV not in os.environ


def test_index_dot_claude_is_forwarded(tmp_path: Path):
    """The spawn's own probes must classify ``.claude/**`` the same way the
    walk will, or they disagree about what is owed."""
    spawn = RecordingSpawn()
    _ensure(tmp_path, spawn=spawn, index_dot_claude=True)
    assert spawn.calls[0][2]["index_dot_claude"] is True


# ===========================================================================
# LEAVE-ALONE — a project that does not owe the work is untouched
# ===========================================================================
def test_an_already_reindexed_project_does_not_spawn(tmp_path: Path):
    ceg.write_stamp(tmp_path, ceg.CURRENT_EXTRACTOR_GENERATION)
    spawn = RecordingSpawn()
    out = _ensure(tmp_path, spawn=spawn)
    assert out.status == "skipped"
    assert out.reason == ceg.REASON_STAMP_CURRENT
    assert spawn.calls == []


def test_a_project_with_no_code_graph_does_not_spawn_a_build(tmp_path: Path):
    """A first install must NOT get a surprise full code-graph build out of
    the repair path. Whatever builds the graph next already has the fixes."""
    spawn = RecordingSpawn()
    out = _ensure(tmp_path, spawn=spawn, prev_version="",
                  probe=_probe_graph_absent)
    assert out.status == "stamped"
    assert out.reason == ceg.REASON_NO_GRAPH
    assert spawn.calls == []
    assert ceg.read_stamp_generation(tmp_path) == ceg.CURRENT_EXTRACTOR_GENERATION


def test_a_second_update_after_a_completed_walk_is_a_no_op(tmp_path: Path):
    """Idempotence at the trigger level: the analyzer stamps on completion, so
    the next update reads the stamp and does nothing."""
    spawn = RecordingSpawn()
    _ensure(tmp_path, spawn=spawn)
    assert len(spawn.calls) == 1
    ceg.write_stamp(tmp_path, ceg.CURRENT_EXTRACTOR_GENERATION)   # walk finished
    out = _ensure(tmp_path, spawn=spawn, prev_version="0.2.92",
                  running_version="0.2.93")
    assert out.status == "skipped"
    assert len(spawn.calls) == 1, "the completed project must not re-spawn"


def test_an_interrupted_walk_is_retried_on_the_next_update(tmp_path: Path):
    """No stamp ⇒ still owed. The walk is idempotent (converged rows
    hash-skip), so the retry finishes what the first attempt started."""
    spawn = RecordingSpawn()
    _ensure(tmp_path, spawn=spawn)
    # ... the child dies mid-walk: no stamp is ever written ...
    assert ceg.read_stamp_generation(tmp_path) is None
    _ensure(tmp_path, spawn=spawn, prev_version="0.2.91",
            running_version="0.2.93")
    assert len(spawn.calls) == 2, "an unfinished walk must be re-triggered"


# ===========================================================================
# DEGRADE — never block, always leave a signal
# ===========================================================================
def test_code_embed_down_defers_with_an_entry_instead_of_spawning(tmp_path: Path):
    from vco_lib.deferral_report import DeferralEntry

    entry = DeferralEntry(
        condition_id="codegraph_embed_resync_pending", title="t",
        detected="d", why_deferred="w",
        command_to_apply="/py /analyze.py /repo --project DemoProj")
    spawn = RecordingSpawn(status="deferred", pid=None,
                           message="code-embed service unreachable",
                           deferral=entry)
    out = _ensure(tmp_path, spawn=spawn)
    assert out.status == "deferred"
    assert out.deferral is not None
    assert "unreachable" in out.message


def test_the_deferred_resume_command_actually_repairs_the_problem(tmp_path: Path):
    """The borrowed resync deferral's command is a PLAIN analyze, which
    delivers nothing here (the per-file gate skips every unchanged file — the
    whole defect). A remediation a user can follow and still stay broken is
    worse than none, so the command must carry ``--force-rewalk``."""
    from vco_lib.deferral_report import DeferralEntry

    entry = DeferralEntry(
        condition_id="codegraph_embed_resync_pending", title="t",
        detected="d", why_deferred="w",
        command_to_apply="/py /analyze.py /repo --project DemoProj")
    out = _ensure(tmp_path, spawn=RecordingSpawn(status="deferred",
                                                 deferral=entry))
    cmd = out.deferral.command_to_apply
    assert "--force-rewalk" in cmd
    assert "--project DemoProj" in cmd, "the original command must survive"
    # The registered condition_id is reused deliberately (an unregistered one
    # would fail the deferral-registry completeness gate).
    assert out.deferral.condition_id == "codegraph_embed_resync_pending"


def test_retargeting_never_drops_a_deferral(tmp_path: Path):
    """Any shape we cannot safely amend passes through untouched."""
    from vco_lib import codegraph_extractor_generation as m

    assert m._retarget_deferral(None, "0.2.92") is None
    weird = object()
    assert m._retarget_deferral(weird, "0.2.92") is weird

    class _Frozen:
        command_to_apply = "  "

    obj = _Frozen()
    assert m._retarget_deferral(obj, "0.2.92") is obj


def test_retargeting_is_idempotent(tmp_path: Path):
    """The ledger is re-emitted on every update while the condition holds, so
    the entry must not accumulate flags or notes."""
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import codegraph_extractor_generation as m

    entry = DeferralEntry(
        condition_id="codegraph_embed_resync_pending", title="t",
        detected="d", why_deferred="w",
        command_to_apply="/py /analyze.py /repo --project P")
    once = m._retarget_deferral(entry, "0.2.92")
    twice = m._retarget_deferral(once, "0.2.92")
    thrice = m._retarget_deferral(twice, "0.2.92")
    assert once.command_to_apply == twice.command_to_apply == \
        thrice.command_to_apply
    # The runnable first line carries the flag exactly once.
    first_line = once.command_to_apply.splitlines()[0]
    assert first_line.count("--force-rewalk") == 1
    assert first_line.endswith("--project P --force-rewalk")


def test_a_command_that_already_has_the_flag_is_not_doubled(tmp_path: Path):
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import codegraph_extractor_generation as m

    entry = DeferralEntry(
        condition_id="codegraph_embed_resync_pending", title="t",
        detected="d", why_deferred="w",
        command_to_apply="/py /analyze.py /repo --force-rewalk")
    out = m._retarget_deferral(entry, "0.2.92")
    assert out.command_to_apply.splitlines()[0].count("--force-rewalk") == 1


def test_a_declining_spawner_leaves_the_project_owed(tmp_path: Path):
    """``VCT_RESYNC_SPAWN_DISABLED`` / no analyzer on disk / empty project name
    all come back as ``skipped``. No stamp may be written for any of them —
    the work was NOT done."""
    spawn = RecordingSpawn(status="skipped", pid=None,
                           message="background resync spawn disabled")
    out = _ensure(tmp_path, spawn=spawn)
    assert out.status == "skipped"
    assert ceg.read_stamp_generation(tmp_path) is None


def test_a_missing_helper_module_is_a_message_not_an_exception(tmp_path: Path,
                                                               monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_resync(name, *a, **kw):
        if name == "vco_lib.codegraph_resync":
            raise ImportError("simulated partial install")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_resync)
    out = _ensure(tmp_path)          # spawn=None ⇒ resolves the real helper
    assert out.status == "failed"
    assert "unavailable" in out.message


def test_a_raising_detector_never_escapes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ceg, "read_stamp_generation",
                        lambda *_a, **_kw: (_ for _ in ()).throw(
                            RuntimeError("disk on fire")))
    out = ceg.ensure_extractor_generation(
        tmp_path, prev_version="0.2.91", running_version="0.2.92",
        project_name="DemoProj", spawn=RecordingSpawn())
    assert out.status == "failed"
    assert out.reason == "plan_raised"


# ===========================================================================
# The bundle-update wiring in project_init
# ===========================================================================
def _fake_log():
    entries = []

    def log(step, phase, detail="", *, data=None):
        entries.append((step, phase, detail, data))

    return log, entries


def test_bundle_wiring_surfaces_a_launch_to_the_user(tmp_path: Path,
                                                     monkeypatch):
    """Unbounded silent background work is exactly what the project rules
    forbid — a launch must reach ``result["warnings"]`` so the launcher toast
    tells the user something is running."""
    from vco_lib import project_init

    monkeypatch.setattr(
        ceg, "ensure_extractor_generation",
        lambda *a, **kw: ceg.ReindexResult(status="launched", pid=99,
                                           reason=ceg.REASON_CROSSES_BUMP,
                                           message="launched"))
    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name",
                        lambda _f: "DemoProj")
    log, entries = _fake_log()
    result = {"warnings": []}
    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.91", running_version="0.2.92",
        is_root_target=False, result=result, log=log)

    assert result["extractor_reindex"]["status"] == "launched"
    assert any("re-index started in the background" in w
               for w in result["warnings"])
    assert any(step == "4.bundle.extractor_reindex" and phase == "ok"
               for step, phase, _d, _dt in entries)


def test_bundle_wiring_never_aborts_the_install(tmp_path: Path, monkeypatch):
    """RED-PROOF of the soft-fail contract: a helper that raises must become a
    warning. An update that dies because a best-effort re-index failed would be
    strictly worse than the broken graph it was trying to repair."""
    from vco_lib import project_init

    def _boom(*_a, **_kw):
        raise RuntimeError("helper exploded")

    monkeypatch.setattr(ceg, "ensure_extractor_generation", _boom)
    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name",
                        lambda _f: "DemoProj")
    log, _entries = _fake_log()
    result = {"warnings": []}
    with pytest.raises(RuntimeError):
        # The helper itself propagates ...
        project_init._trigger_extractor_generation_reindex(
            tmp_path, prev_version="0.2.91", running_version="0.2.92",
            is_root_target=False, result=result, log=log)
    # ... and `install_project_bundle`'s own try/except is what converts it.
    import inspect

    src = inspect.getsource(project_init.install_project_bundle)
    assert "_trigger_extractor_generation_reindex(" in src
    idx = src.index("_trigger_extractor_generation_reindex(")
    window = src[max(0, idx - 400): idx]
    assert "try:" in window, (
        "the bundle call site must be inside a try/ — a re-index failure must "
        "never abort an update"
    )
    assert "extractor-generation re-index skipped" in src


def test_bundle_wiring_emits_the_deferral_when_the_backend_is_down(
    tmp_path: Path, monkeypatch,
):
    from vco_lib import project_init

    emitted = []
    sentinel = object()
    monkeypatch.setattr(
        ceg, "ensure_extractor_generation",
        lambda *a, **kw: ceg.ReindexResult(
            status="deferred", reason=ceg.REASON_CROSSES_BUMP,
            message="code-embed service (:11440) unreachable",
            deferral=sentinel))
    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name",
                        lambda _f: "DemoProj")
    monkeypatch.setattr("vco_lib.deferral_emit.emit",
                        lambda folder, entry: emitted.append((folder, entry)))
    log, _ = _fake_log()
    result = {"warnings": []}
    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.91", running_version="0.2.92",
        is_root_target=False, result=result, log=log)

    assert emitted and emitted[0][1] is sentinel
    assert any("deferred" in w for w in result["warnings"])


def test_bundle_wiring_does_nothing_without_a_project_name(tmp_path: Path,
                                                           monkeypatch):
    """Conservative default: never guess a project name — a wrong guess would
    aim a walk at another project's collections."""
    from vco_lib import project_init

    called = []
    monkeypatch.setattr(ceg, "ensure_extractor_generation",
                        lambda *a, **kw: called.append(1))
    monkeypatch.setattr(project_init, "_resolve_codegraph_project_name",
                        lambda _f: "")
    log, entries = _fake_log()
    result = {"warnings": []}
    project_init._trigger_extractor_generation_reindex(
        tmp_path, prev_version="0.2.91", running_version="0.2.92",
        is_root_target=False, result=result, log=log)
    assert called == []
    assert any("no code-graph project name" in d for _s, _p, d, _dt in entries)


def _identity_snapshot(monkeypatch, *, resolvable: bool, projects=()):
    """Pin the identity SSOT's view of launcher.db for one test."""
    from vco_lib import project_identity as pid

    snap = pid.IdentitySnapshot(resolvable=resolvable, projects=tuple(projects))
    monkeypatch.setattr(pid, "resolve_snapshot", lambda **_kw: snap)
    return snap


def _identity(name: str, folder=None):
    from vco_lib import project_identity as pid

    return pid.ProjectIdentity(
        name=name,
        project_id=f"id-{name}",
        slug=name.lower(),
        folder_path=str(folder) if folder is not None else None,
    )


def test_project_name_resolution_order(tmp_path: Path, monkeypatch):
    from vco_lib import project_init

    env = {}
    monkeypatch.setattr("vco_lib.knowledge_residue.project_settings_env",
                        lambda _f: dict(env))
    # A readable registry that does not know this folder: the basename is the
    # analyzer's own --project default and nothing collides with it.
    _identity_snapshot(monkeypatch, resolvable=True, projects=[_identity("Elsewhere")])

    env.clear()
    assert project_init._resolve_codegraph_project_name(tmp_path) == tmp_path.name

    env.update({"PROJECT_NAME": "DisplayName"})
    assert project_init._resolve_codegraph_project_name(tmp_path) == "DisplayName"

    env.update({"CODE_GRAPH_PROJECT": "GraphName"})
    assert project_init._resolve_codegraph_project_name(tmp_path) == "GraphName"


def test_project_name_resolution_survives_a_broken_env_surface(tmp_path: Path,
                                                               monkeypatch):
    from vco_lib import project_init

    monkeypatch.setattr(
        "vco_lib.knowledge_residue.project_settings_env",
        lambda _f: (_ for _ in ()).throw(OSError("settings.json is a directory")))
    _identity_snapshot(monkeypatch, resolvable=True, projects=[])
    assert project_init._resolve_codegraph_project_name(tmp_path) == tmp_path.name


def test_a_registered_folder_uses_its_REGISTERED_name_not_its_basename(
        tmp_path: Path, monkeypatch):
    """The moved/renamed-folder case: the registry is the truth.

    Without this the re-index would walk a renamed folder's source into rows
    named after the NEW basename — the identity defect class this cycle spent
    two blockers on, one blast radius down.
    """
    from vco_lib import project_init

    monkeypatch.setattr("vco_lib.knowledge_residue.project_settings_env",
                        lambda _f: {})
    _identity_snapshot(
        monkeypatch, resolvable=True,
        projects=[_identity("TheRegisteredName", folder=tmp_path)],
    )
    assert project_init._resolve_codegraph_project_name(tmp_path) == "TheRegisteredName"


def test_a_basename_that_collides_with_another_project_resolves_to_nothing(
        tmp_path: Path, monkeypatch):
    """``~/a/myapp`` registered, ``~/b/myapp`` not: do NOT touch a's rows.

    This is the concrete route by which an unregistered folder's re-index
    would have written into a DIFFERENT project's code-graph collections.
    """
    from vco_lib import project_init

    monkeypatch.setattr("vco_lib.knowledge_residue.project_settings_env",
                        lambda _f: {})
    other = tmp_path / "a" / tmp_path.name
    _identity_snapshot(
        monkeypatch, resolvable=True,
        projects=[_identity(tmp_path.name, folder=other)],
    )
    assert project_init._resolve_codegraph_project_name(tmp_path) == ""


def test_an_unreadable_registry_refuses_to_guess(tmp_path: Path, monkeypatch):
    """A launcher.db that EXISTS but will not read: propose nothing.

    ``resolve_identity``'s contract — when nothing can be positively read the
    caller must not fall back to a basename-derived identity.
    """
    from vco_lib import project_init

    monkeypatch.setattr("vco_lib.knowledge_residue.project_settings_env",
                        lambda _f: {})
    _identity_snapshot(monkeypatch, resolvable=False)
    db = tmp_path / "launcher.db"
    db.write_bytes(b"not a database")
    monkeypatch.setattr("vco_lib.launcher_db_reader._discover_db_path", lambda: db)
    assert project_init._resolve_codegraph_project_name(tmp_path) == ""


def test_no_launcher_db_at_all_still_uses_the_analyzer_default(
        tmp_path: Path, monkeypatch):
    """The free-tier / CLI-only install must keep getting its re-index.

    LEAVE-ALONE side of the guard above: a machine with no launcher has no
    registry for this folder to be absent from, so the basename is the
    analyzer's own ``--project`` default rather than a guess about someone
    else's rows.
    """
    from vco_lib import project_init

    monkeypatch.setattr("vco_lib.knowledge_residue.project_settings_env",
                        lambda _f: {})
    _identity_snapshot(monkeypatch, resolvable=False)
    monkeypatch.setattr("vco_lib.launcher_db_reader._discover_db_path", lambda: None)
    assert project_init._resolve_codegraph_project_name(tmp_path) == tmp_path.name


def test_a_real_bundle_update_actually_reaches_the_trigger(tmp_path: Path,
                                                           monkeypatch):
    """END-TO-END wiring: run the REAL ``install_project_bundle`` in update
    mode and prove the trigger is reached with the manifest's prior version.

    The structural pins above would survive a call site that is dead code;
    this one would not.
    """
    from vco_lib import project_init

    repo_root = Path(__file__).resolve().parent.parent
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)

    seen = []

    def _recorder(target, **kwargs):
        seen.append((Path(target), kwargs))
        return ceg.ReindexResult(status="skipped", reason=ceg.REASON_NO_GRAPH,
                                 message="probe says no graph")

    monkeypatch.setattr(ceg, "ensure_extractor_generation", _recorder)
    # Hermeticity: the name resolver consults launcher.db, so without this pin
    # the test's outcome depends on whether the DEVELOPER's machine happens to
    # have a project registered under this tmp folder's basename.
    _identity_snapshot(monkeypatch, resolvable=True, projects=[])

    # First install lays down the manifest that records `vco_version`.
    first = project_init.install_project_bundle(
        folder, orchestrator_root=repo_root, update_mode=False, dry_run=False)
    assert first.get("manifest_written") is True
    installed_version = first["vco_version"]
    seen.clear()

    # The update is the path the launcher drives for every project.
    project_init.install_project_bundle(
        folder, orchestrator_root=repo_root, update_mode=True, dry_run=False)

    assert len(seen) == 1, "the bundle update must reach the trigger exactly once"
    target, kwargs = seen[0]
    assert target == folder
    assert kwargs["prev_version"] == installed_version
    assert kwargs["running_version"] == installed_version
    assert kwargs["index_dot_claude"] is False   # a user project, not the root
    assert kwargs["project_name"]


def test_a_dry_run_bundle_update_never_reaches_the_trigger(tmp_path: Path,
                                                            monkeypatch):
    """A dry run enumerates and classifies; it must mutate nothing and start
    nothing."""
    from vco_lib import project_init

    repo_root = Path(__file__).resolve().parent.parent
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    project_init.install_project_bundle(
        folder, orchestrator_root=repo_root, update_mode=False, dry_run=False)

    seen = []
    monkeypatch.setattr(ceg, "ensure_extractor_generation",
                        lambda *a, **kw: seen.append(1))
    project_init.install_project_bundle(
        folder, orchestrator_root=repo_root, update_mode=True, dry_run=True)
    assert seen == []


def test_the_bundle_seam_runs_on_the_update_path(monkeypatch):
    """Structural pin: the trigger must sit in ``install_project_bundle`` —
    the ONE path the launcher runs for every project AND (since v0.2.85) for
    the orchestrator root. ``install.py``'s own resync shim covers PROJECT_ROOT
    only, so wiring it there would leave every user project unrepaired."""
    import inspect

    from vco_lib import project_init

    src = inspect.getsource(project_init.install_project_bundle)
    assert "_trigger_extractor_generation_reindex(" in src
    # And it must be skipped on a dry run (a dry run mutates nothing).
    idx = src.index("_trigger_extractor_generation_reindex(")
    assert "if not dry_run:" in src[max(0, idx - 400): idx]
