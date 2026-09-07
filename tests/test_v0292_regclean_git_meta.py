# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 4 — `vco_lib.git_meta` is tri-state, and it is WIRED.

Two defects, both closed here.

**The promise.** The module shipped in v0.2.53 saying *"the two callsites
migrate onto it in v0.2.54"*. They never did, so for thirty-eight releases the
file had ZERO production consumers and a docstring asserting the opposite —
R16 category 1. Ruling R24 says a declared-but-unwired capability gets WIRED,
not deleted, so `deferral_probes._git_is_usable` now runs on
`git_meta.head_state` and this file pins that.

**The collapse.** `git_branch()` returned `None` for detached HEAD AND for
every error, which is the exact shape v0.2.92 exists to end (Rust
`CheckState`'s module docs: *"a check that cannot distinguish 'I could not
determine this' from 'this is fine' is not a check"*). `branch_state()` is the
non-collapsing resolver; `git_branch()` survives as a DERIVED accessor with a
docstring that names its own loss, because a lossy helper that says so is
useful and a lossy helper that pretends otherwise is a trap.

Vocabulary is deliberately shared with the two tri-states already in the tree
(Rust `CheckState::{Ok,NotApplicable,Unknown}` and `doctor.SourceFacts.detached`
+ `doctor._git`'s ``rc is None``), not invented — a third dialect for the same
idea is how a reviewer stops being able to check any of them.

Tri-OS (R12/R14): every assertion here is OS-independent. `git init` and
`symbolic-ref` behave identically on all three platforms, path comparison is
`Path`-based with no separator literal, and the git-absent arm is driven by
monkeypatching the spawn rather than by mangling `PATH` (which is
shell-specific).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vco_lib import git_meta
from vco_lib.git_meta import (
    BranchState,
    GitState,
    HeadState,
    branch_state,
    git_branch,
    git_short_sha,
    head_state,
)

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "vco test",
    "GIT_AUTHOR_EMAIL": "vco@example.invalid",
    "GIT_COMMITTER_NAME": "vco test",
    "GIT_COMMITTER_EMAIL": "vco@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.update(_GIT_ENV)
    # `GIT_CONFIG_GLOBAL=/dev/null` is POSIX-shaped; on Windows the equivalent
    # is any non-existent path, and git treats an unreadable config as empty
    # either way. Kept simple because the identity is passed via GIT_AUTHOR_*.
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc


@pytest.fixture
def repo(tmp_path) -> Path:
    """A throwaway one-commit repo on a branch named ``trunk``.

    Entirely inside ``tmp_path``: `git -C` pins every invocation to it, so
    nothing here can reach the checkout this suite runs from.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "trunk")
    (r / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


# --------------------------------------------------------------------------- #
# 1. branch_state — the three answers, told apart
# --------------------------------------------------------------------------- #


@needs_git
def test_attached_head_reports_ok_and_the_name(repo):
    st = branch_state(repo)

    assert st.state is GitState.OK
    assert st.name == "trunk"
    assert st.detached is False
    assert st.is_known


@needs_git
def test_detached_head_is_not_applicable_not_an_error(repo):
    """The arm the old API destroyed.

    ``symbolic-ref -q`` exits 1 to SAY "detached" — a positive answer. The
    alternative spelling, ``rev-parse --abbrev-ref HEAD``, returns the literal
    string ``"HEAD"`` here, which is how five inline call sites in this repo
    lost the fact while normalising it.
    """
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "--detach", sha)

    st = branch_state(repo)

    assert st.state is GitState.NOT_APPLICABLE
    assert st.detached is True
    assert st.name is None
    assert st.is_known, "detached is a FACT — never an undetermined result"
    assert "detached" in st.describe()


def test_a_non_repo_is_not_applicable_with_detached_false(tmp_path):
    st = branch_state(tmp_path)

    assert st.state is GitState.NOT_APPLICABLE
    assert st.detached is False
    assert st.is_known
    assert "not a git work tree" in st.reason


@needs_git
def test_git_that_cannot_run_is_unknown(repo, monkeypatch):
    """A spawn failure must NEVER read as a determinate answer."""
    def boom(*_a, **_kw):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(git_meta.subprocess, "run", boom)

    st = branch_state(repo)

    assert st.state is GitState.UNKNOWN
    assert not st.is_known
    assert st.detached is None, "an unknown probe must not claim an attachment"
    assert "could not run" in st.reason


@needs_git
def test_a_git_timeout_is_unknown(repo, monkeypatch):
    def slow(*_a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kw.get("timeout", 5))

    monkeypatch.setattr(git_meta.subprocess, "run", slow)

    st = branch_state(repo)

    assert st.state is GitState.UNKNOWN
    assert "timed out" in st.reason


@needs_git
def test_the_three_outcomes_are_distinguishable(repo, tmp_path, monkeypatch):
    """THE assertion this item exists for, stated in one place.

    All three cases below make `git_branch()` return `None`. The tri-state
    resolver tells them apart; that difference is the whole fix.
    """
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    detached_repo = tmp_path / "detached"
    shutil.copytree(repo, detached_repo)
    _git(detached_repo, "checkout", "-q", "--detach", sha)

    outcomes = {
        "detached": branch_state(detached_repo),
        "not_a_repo": branch_state(tmp_path / "nowhere"),
    }
    with monkeypatch.context() as m:
        m.setattr(git_meta.subprocess, "run",
                  lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git")))
        outcomes["git_unavailable"] = branch_state(repo)

    assert all(git_branch_is_none(st) for st in outcomes.values())
    assert outcomes["detached"].detached is True
    assert outcomes["not_a_repo"].detached is False
    assert outcomes["git_unavailable"].detached is None
    assert len({(st.state, st.detached) for st in outcomes.values()}) == 3, (
        "two of the three collapsed into the same value — that is the defect"
    )


def git_branch_is_none(st: BranchState) -> bool:
    """What the lossy accessor would return for ``st`` (it returns ``.name``)."""
    return st.name is None


# --------------------------------------------------------------------------- #
# 2. git_branch stays, and stays DERIVED
# --------------------------------------------------------------------------- #


@needs_git
def test_git_branch_returns_the_name_when_attached(repo):
    assert git_branch(repo) == "trunk"


def test_git_branch_returns_none_for_a_non_git_dir(tmp_path):
    """Back-compat with `tests/test_vco_lib_v0253_modules.py`'s pin."""
    assert git_branch(tmp_path) is None


def test_git_branch_is_derived_from_branch_state(monkeypatch, tmp_path):
    """No second resolution — so the two cannot drift.

    A re-implementation is the realistic regression here (someone "optimises"
    the wrapper into its own `rev-parse`), and it would silently restore the
    ``"HEAD"``-literal bug this module now avoids.
    """
    monkeypatch.setattr(
        git_meta, "branch_state",
        lambda _repo: BranchState(state=GitState.OK, name="sentinel-branch"),
    )
    assert git_branch(tmp_path) == "sentinel-branch"


def test_git_branch_docstring_names_its_own_loss():
    doc = git_branch.__doc__ or ""
    assert "Lossy" in doc or "lossy" in doc
    assert "branch_state" in doc


def test_module_docstring_carries_no_future_migration_promise():
    """R16: the "migrates in v0.2.54" sentence may only appear RETRACTED.

    That sentence is why this module went thirty-eight releases with no
    consumer and a docstring saying otherwise, so the docstring now records it
    — which means a plain "this string is absent" check would fail on the
    correction itself and pressure the next editor to delete the history.

    The checkable property instead: **every mention of the retired promise is
    co-located with its retraction**, and the "do not re-add it" instruction is
    present. A future editor who pastes the forward-looking sentence back in
    without the retraction goes red.
    """
    doc = git_meta.__doc__ or ""
    assert "ZERO production consumers" in doc
    assert "Do not re-add" in doc

    marker = "migrate onto it in v0.2.54"
    idx = 0
    seen = 0
    while (idx := doc.find(marker, idx)) != -1:
        seen += 1
        window = doc[max(0, idx - 200): idx + 200]
        assert "never happened" in window, (
            "the retired v0.2.54 migration promise appears without its "
            "retraction nearby — a reader will act on it, which is exactly "
            "how it survived thirty-eight releases"
        )
        idx += len(marker)
    assert seen == 1, f"expected the promise quoted exactly once, saw {seen}"


# --------------------------------------------------------------------------- #
# 3. head_state — and the wiring that gives the module a consumer
# --------------------------------------------------------------------------- #


@needs_git
def test_head_state_ok_on_a_repo_with_a_commit(repo):
    st = head_state(repo)

    assert st.state is GitState.OK
    assert st.is_usable
    assert st.sha and len(st.sha) >= 7


def test_head_state_not_applicable_without_a_git_dir(tmp_path):
    st = head_state(tmp_path)

    assert st.state is GitState.NOT_APPLICABLE
    assert not st.is_usable
    assert st.is_known


@needs_git
def test_head_state_not_applicable_on_a_repo_with_no_commits(tmp_path):
    """An empty repo is a determinate "nothing to resolve", not a failure."""
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q", "-b", "trunk")

    st = head_state(r)

    assert st.state is GitState.NOT_APPLICABLE
    assert st.is_known
    assert not st.is_usable


@needs_git
def test_head_state_unknown_when_git_cannot_run(repo, monkeypatch):
    monkeypatch.setattr(
        git_meta.subprocess, "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git")),
    )
    st = head_state(repo)

    assert st.state is GitState.UNKNOWN
    assert not st.is_usable
    assert not st.is_known


def test_deferral_probes_git_precondition_runs_on_git_meta(monkeypatch, tmp_path):
    """The WIRING: `_git_is_usable` consults `git_meta.head_state`.

    Monkeypatched at the git_meta end, so this fails if the probe grows its
    own `subprocess.run` back — which is exactly how the module lost its
    consumers the first time.
    """
    from vco_lib import deferral_probes

    calls: list = []

    def fake(repo):
        calls.append(Path(repo))
        return HeadState(state=GitState.OK, sha="deadbeef")

    monkeypatch.setattr(git_meta, "head_state", fake)

    assert deferral_probes._git_is_usable(tmp_path) is True
    assert calls == [tmp_path]


@needs_git
def test_the_precondition_behaviour_is_unchanged(repo, tmp_path):
    """Same answers as the hand-rolled version it replaced."""
    from vco_lib import deferral_probes

    assert deferral_probes._git_is_usable(repo) is True
    assert deferral_probes._git_is_usable(tmp_path / "nope") is False


def test_git_meta_has_at_least_one_production_consumer():
    """The R16 half: the promise is now backed by an import that exists."""
    consumers = [
        p
        for p in (_repo_root() / "vco_lib").rglob("*.py")
        if p.name != "git_meta.py" and "git_meta" in p.read_text(encoding="utf-8")
    ]
    assert consumers, (
        "no vco_lib module imports git_meta — the module is back to being a "
        "promise with no backing code"
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 4. The unchanged surface
# --------------------------------------------------------------------------- #


@needs_git
def test_git_short_sha_still_works(repo):
    assert git_short_sha(repo)


def test_git_short_sha_still_none_for_non_git_dir(tmp_path):
    assert git_short_sha(tmp_path) is None


@needs_git
def test_resolve_vco_version_prefers_the_version_file(tmp_path):
    from vco_lib.git_meta import resolve_vco_version

    (tmp_path / "VERSION").write_text("0.2.92\n", encoding="utf-8")
    assert resolve_vco_version(tmp_path) == "v0.2.92"


def test_resolve_vco_version_falls_back_to_unknown(tmp_path):
    from vco_lib.git_meta import resolve_vco_version

    assert resolve_vco_version(tmp_path) == "unknown"
