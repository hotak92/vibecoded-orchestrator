# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the §7 HARD-CUT primitive (vco_lib/hard_cut.py).

Covers DESIGN-v0300 §7.4 execution order + §7.1 MUST-PRESERVE + §7.3.3
abort-on-bundle-failure, AND the INERT guarantee (the primitive is built but
not wired into any v0.2.60 update path).

Time/random are unavailable in the build env, so ``stamp`` + ``now_ms`` are
injected. Every subprocess is faked via the ``runner`` parameter, so no real
git/install runs and no live Weaviate is touched. The migration runner is
injected so step 5 reuses the real signature without a live registry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import hard_cut as hc  # noqa: E402


class _ScriptedRunner:
    """Returns a scripted CompletedProcess per command prefix; records calls.

    ``script`` maps a substring → returncode. First matching substring wins;
    default rc=0.
    """

    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        joined = " ".join(str(c) for c in cmd)
        rc = 0
        for needle, code in self.script.items():
            if needle in joined:
                rc = code
                break
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    def cmds(self):
        return [c[0] for c in self.calls]


def _fake_migration_runner_factory():
    captured = {}

    def _runner(**kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return object()

    return _runner, captured


@pytest.fixture
def install_layout(tmp_path):
    """A clone with .git + install.py, and a ~/.vct dir with the preserve set."""
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "install.py").write_text("# fake\n")
    (clone / "knowledge").mkdir()
    (clone / "knowledge" / "node.md").write_text("user node\n")
    (clone / ".claude" / "state").mkdir(parents=True)
    (clone / ".claude" / "state" / "x.json").write_text("{}\n")
    (clone / "migrations").mkdir()

    vct = tmp_path / ".vct"
    vct.mkdir()
    for name in hc.MUST_PRESERVE_VCT_FILES:
        (vct / name).write_text("PRESERVE\n")
    secrets = tmp_path / ".vct-secrets"
    secrets.mkdir()
    (secrets / "github_pat").write_text("secret\n")
    return clone, vct


# ---------------------------------------------------------------------------
# Happy path — §7.4 order
# ---------------------------------------------------------------------------


def test_hard_cut_full_success_in_order(install_layout):
    clone, vct = install_layout
    runner = _ScriptedRunner()
    mig, captured = _fake_migration_runner_factory()

    res = hc.hard_cut(
        "0.2.45", "0.3.0",
        clone_root=clone, vct_root=vct, project_id="p1",
        stamp="20260616T120000Z", upstream_remote="vco_upstream",
        env={"FOO": "bar"}, weaviate_url="http://x:8081",
        runner=runner, migration_runner=mig, now_ms=42,
    )

    assert res.ok is True
    assert res.bundle_verified and res.reset_done and res.install_done
    assert res.migrate_done and res.deferral_written
    assert res.aborted_before_reset is False
    # The backup landed under <vct_root>/backups with the injected stamp.
    assert res.bundle_path == str(vct / "backups" / "pre-hardcut-20260616T120000Z.bundle")
    # Step order: bundle create → verify → fetch → reset → install. (deferral
    # write is not a subprocess.)
    cmds = [" ".join(str(p) for p in c) for c in runner.cmds()]
    assert cmds[0].startswith("git bundle create")
    assert cmds[1].startswith("git bundle verify")
    assert "git fetch vco_upstream v0.3.0" in cmds[2]
    assert "git reset --hard v0.3.0" in cmds[3]
    assert "install.py --update" in cmds[4]
    # Migration runner reused (step 5), root scope.
    assert captured["called"] is True
    assert captured["kwargs"]["include_orchestrator_wide"] is True
    assert captured["kwargs"]["project_id"] == "p1"


# ---------------------------------------------------------------------------
# §7.3.3 — bundle failure ABORTS before any destructive step
# ---------------------------------------------------------------------------


def test_bundle_create_failure_aborts_no_destructive(install_layout):
    clone, vct = install_layout
    runner = _ScriptedRunner(script={"git bundle create": 1})
    mig, captured = _fake_migration_runner_factory()
    res = hc.hard_cut(
        "0.2.45", "0.3.0", clone_root=clone, vct_root=vct, project_id="p1",
        stamp="S", runner=runner, migration_runner=mig, now_ms=1,
    )
    assert res.ok is False
    assert res.aborted_before_reset is True
    assert res.reset_done is False
    # NO fetch/reset/install ran — only the failed `git bundle create`.
    cmds = [" ".join(str(p) for p in c) for c in runner.cmds()]
    assert len(cmds) == 1 and cmds[0].startswith("git bundle create")
    assert captured == {}  # migration runner never reached


def test_bundle_verify_failure_aborts_no_destructive(install_layout):
    clone, vct = install_layout
    runner = _ScriptedRunner(script={"git bundle verify": 1})
    res = hc.hard_cut(
        "0.2.45", "0.3.0", clone_root=clone, vct_root=vct, project_id="p1",
        stamp="S", runner=runner, now_ms=1,
    )
    assert res.ok is False
    assert res.aborted_before_reset is True
    assert res.bundle_verified is False
    cmds = [" ".join(str(p) for p in c) for c in runner.cmds()]
    # create ran, verify ran (failed), nothing after.
    assert cmds[0].startswith("git bundle create")
    assert cmds[1].startswith("git bundle verify")
    assert all("reset" not in c for c in cmds)


# ---------------------------------------------------------------------------
# §7.1 MUST-PRESERVE — the executed command set touches no preserve-list path
# ---------------------------------------------------------------------------


def test_hard_cut_touches_no_preserve_list_path(install_layout):
    """The CRITICAL data-safety assertion: every command hard_cut runs must
    operate ONLY within the clone (git ops cwd=clone, install cwd=clone) and
    NONE of them may name a MUST-PRESERVE path as an argument. The only path a
    hard_cut writes under ~/.vct is the backups/ bundle file — never the DBs,
    services.toml, secrets, knowledge/**, or .claude/state/.

    NOTE (NIT, 2026-06-16): because this test uses a FAKED runner, the on-disk
    "files still exist" assertions below are a no-op proof (nothing actually
    ran). The REAL MUST-PRESERVE guarantee is STRUCTURAL, not behavioural:
    `git reset --hard <tag>` only mutates git-TRACKED files, and `knowledge/`
    + `.claude/state/` are NOT tracked in the public repo (they are
    user/runtime state), while `~/.vct` + `~/.vct-secrets` live OUTSIDE the
    clone entirely. That structural invariant is regression-guarded by
    `tests/test_hardcut_preserve_paths_untracked.py` (asserts
    `git ls-files knowledge/` and `git ls-files .claude/state/` stay EMPTY) so
    a future editor who accidentally tracks knowledge/ can't silently break the
    hard-cut's data safety. THIS test proves the argv/cwd discipline; that one
    proves the structural premise it relies on."""
    clone, vct = install_layout
    runner = _ScriptedRunner()
    mig, _ = _fake_migration_runner_factory()
    hc.hard_cut(
        "0.2.45", "0.3.0", clone_root=clone, vct_root=vct, project_id="p1",
        stamp="S", runner=runner, migration_runner=mig, now_ms=1,
    )

    must_preserve = hc.must_preserve_paths(vct, clone)
    preserve_strs = [str(p) for p in must_preserve]

    for cmd, kwargs in runner.calls:
        joined = " ".join(str(c) for c in cmd)
        # No command argument may reference a preserve-list path.
        for p in preserve_strs:
            assert p not in joined, (
                f"hard_cut command {joined!r} references MUST-PRESERVE path {p}"
            )
        # git ops + install run with cwd=clone (never cwd=~/.vct or secrets).
        cwd = kwargs.get("cwd", "")
        assert str(vct) != cwd, "a hard_cut command ran with cwd=~/.vct"
        assert ".vct-secrets" not in cwd
        # the reset target is the tag, never a path; reset is plain (no paths).
        if "reset" in joined:
            assert "--hard v0.3.0" in joined and str(clone) not in joined.replace(
                f"cwd={clone}", ""
            )

    # Sanity: all preserve files still exist on disk after the (faked) run.
    for p in must_preserve:
        if p.name in hc.MUST_PRESERVE_VCT_FILES:
            assert p.exists() and p.read_text().strip() == "PRESERVE"
    assert (clone / "knowledge" / "node.md").read_text().strip() == "user node"
    assert (vct.parent / ".vct-secrets" / "github_pat").exists()


def test_must_preserve_paths_enumeration(install_layout):
    clone, vct = install_layout
    paths = hc.must_preserve_paths(vct, clone)
    names = {p.name for p in paths}
    # All ~/.vct files + secrets dir + knowledge + state are enumerated.
    assert {"launcher.db", "hub.db", "services.toml"} <= names
    assert any(p.name == ".vct-secrets" for p in paths)
    assert any(str(p).endswith("knowledge") for p in paths)
    assert any(str(p).endswith("state") for p in paths)


# ---------------------------------------------------------------------------
# Corrupt .git → sibling-clone fallback (secondary path), no destructive action
# ---------------------------------------------------------------------------


def test_corrupt_git_routes_to_sibling_clone_no_destructive(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()  # NO .git
    (clone / "install.py").write_text("# fake\n")
    vct = tmp_path / ".vct"
    vct.mkdir()
    runner = _ScriptedRunner()
    res = hc.hard_cut(
        "0.2.45", "0.3.0", clone_root=clone, vct_root=vct, project_id="p1",
        stamp="S", runner=runner, now_ms=1,
    )
    assert res.sibling_clone_required is True
    assert res.aborted_before_reset is True
    assert res.ok is False
    assert runner.calls == []  # nothing ran — no bundle, no reset


# ---------------------------------------------------------------------------
# Mid-flow failures leave the bundle (recoverable) and surface the error
# ---------------------------------------------------------------------------


def test_install_failure_after_reset_surfaces_error(install_layout):
    clone, vct = install_layout
    runner = _ScriptedRunner(script={"install.py --update": 2})
    mig, captured = _fake_migration_runner_factory()
    res = hc.hard_cut(
        "0.2.45", "0.3.0", clone_root=clone, vct_root=vct, project_id="p1",
        stamp="S", runner=runner, migration_runner=mig, now_ms=1,
    )
    assert res.ok is False
    assert res.reset_done is True       # reset DID happen
    assert res.install_done is False
    assert res.migrate_done is False
    assert captured == {}               # migration runner not reached
    assert res.bundle_path is not None  # backup exists for restore
    assert res.error is not None and "install.py" in res.error


# ---------------------------------------------------------------------------
# INERT guarantee — hard_cut is NOT called by the normal update path
# ---------------------------------------------------------------------------


def test_hard_cut_not_invoked_by_normal_update():
    """Grep-style proof that no v0.2.60 Python update path calls hard_cut.

    The ONLY non-test references to ``hard_cut(`` / ``perform_hard_cut`` should
    be the module itself + the (inert) Rust command. install.py and the
    schema-migration runner — the live update paths — must NOT call it.
    """
    install_py = (_REPO / "install.py").read_text(encoding="utf-8")
    assert "hard_cut(" not in install_py, (
        "install.py must NOT call hard_cut() in v0.2.60 (it is INERT until the "
        "v0.3.0 floor bump)"
    )
    runner_py = (_REPO / "vco_lib" / "schema_migration_runner.py").read_text(
        encoding="utf-8"
    )
    assert "hard_cut(" not in runner_py
    assert "import hard_cut" not in runner_py and "from .hard_cut" not in runner_py
