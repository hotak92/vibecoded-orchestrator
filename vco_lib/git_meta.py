# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Git HEAD / rev resolution (vco_lib.git_meta — v0.2.53).

Consolidates read-only git plumbing that was re-implemented per call site:

* ``install.py:1132`` (``_bootstrap_resolve_vco_version``) — VERSION file
  read + ``git rev-parse --short HEAD``.
* ``vco_lib/project_init.py:3589`` (``_resolve_vco_version``) — subprocess
  ``git rev-parse --short HEAD``.
* ``vco_lib/deferral_probes.py`` (``_git_is_usable``) — ``git rev-parse
  --verify HEAD`` as a probe precondition. **Migrated onto this module in
  v0.2.92** (:func:`head_state`).
* ``vco_lib/dist_binary_repair.py`` (private ``_run_git``) — ``git status
  --porcelain``, ``git checkout HEAD --``, ``git show HEAD:<path>``.
  **Migrated onto this module in v0.2.92** (WP-G; :func:`run_git` for the
  text-mode calls, :func:`run_git_binary` for the binary blob read in
  ``stage_paths_from_head`` — see that function's docstring for why the two
  runners are not one).
* ``vco_lib/codegraph_guards.py`` (``provenance_line``) — ad hoc ``git
  rev-parse HEAD`` spawn for the ``analyzed_commit`` provenance field.
  **Migrated onto this module in v0.2.92** (WP-G; :func:`git_head_sha`).

Per docs/INSTALL_ARCHITECTURE_v2.md §7.6.

v0.2.92 — TWO defects closed, both recorded because the record is the point:

1. **The module had ZERO production consumers.** The v0.2.53 docstring said
   "the two callsites migrate onto it in v0.2.54"; that never happened, so for
   thirty-eight releases this file was a promise with no backing code (R16
   category 1). Ruling R24 says a declared-but-unwired capability gets WIRED,
   not deleted — so :func:`head_state` now backs ``deferral_probes``'s probe
   precondition, and the remaining two version call sites have recipes in the
   v0.2.92 regression-cleanup report rather than a repeated promise here.
   **Do not re-add a "the callsites migrate in vX" sentence.** If a migration
   is owed, it is owed in a plan or a report — not as an assertion in shipped
   source that a reader will believe.

2. **``git_branch()`` returned ``None`` for detached HEAD AND for every
   error** — the exact collapse this release exists to end (see
   ``launcher/src-tauri/vct-launcher-core/src/check_state.rs``: *"a check that
   cannot distinguish 'I could not determine this' from 'this is fine' is not
   a check"*). :func:`branch_state` is the non-collapsing resolver;
   :func:`git_branch` is now DERIVED from it and documents its own loss.

v0.2.92 (WP-G) — two more production consumers migrated onto this module,
closing the same "declared but not wired" gap R24 names above:
``vco_lib.dist_binary_repair`` (three call sites, one of them binary-blob
sensitive — see :func:`run_git_binary`) and ``vco_lib.codegraph_guards``
(:func:`git_head_sha` backs ``provenance_line``'s ``analyzed_commit`` field).
Both previously ran their own private ``subprocess.run(["git", ...])`` call;
neither does anymore.

Vocabulary is deliberately borrowed, not invented — three tri-states in this
codebase must read the same way to a reviewer:

* Rust: ``CheckState::{Ok, NotApplicable, Unknown{error}}`` (wave 2, WP-13).
* Python doctor: ``SourceFacts.detached`` — ``True`` detached / ``False`` on a
  branch / ``None`` undetermined, and ``doctor._git`` returning ``rc is None``
  for "git could not be RUN at all" as distinct from "git ran and said no"
  (wave 4, N2-D).
* Here: :class:`GitState` — ``OK`` / ``NOT_APPLICABLE`` / ``UNKNOWN``, with the
  same rule that ``NOT_APPLICABLE`` is a DETERMINATE answer and never a
  success.

Everything in this module is READ-ONLY plumbing: nothing fetches, writes a
ref, takes a lock or mutates the caller's repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

#: Wall-clock ceiling for ONE git invocation, seconds. Local plumbing only —
#: nothing here touches the network, so five seconds is generous.
GIT_TIMEOUT_SECONDS = 5


class GitState(str, Enum):
    """What a probe in this module actually established.

    Python twin of Rust's ``CheckState``. ``str`` mixin so a state can be
    logged, JSON-serialised and compared to a literal without a conversion
    step at every call site.
    """

    #: The probe ran to completion and the accompanying value is usable.
    OK = "ok"
    #: The probe does not apply here — e.g. asking for a branch in a directory
    #: that is not a git work tree, or asking for a branch name at a detached
    #: HEAD. A DETERMINATE answer with its own meaning; never a success and
    #: never an error.
    NOT_APPLICABLE = "not_applicable"
    #: The probe could not complete (git absent, timed out, spawn failure, or
    #: an unexpected non-zero exit). ``detail`` says why.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BranchState:
    """The answer to "what branch is ``repo`` on?", without collapsing.

    Three outcomes, distinguishable by :attr:`state`:

    * ``OK`` with :attr:`name` set — attached to that branch.
    * ``NOT_APPLICABLE`` — a determinate "there is no branch here": either the
      repo is at a detached HEAD (:attr:`detached` ``True``) or the path is not
      a git work tree at all (:attr:`detached` ``False``). Both are facts, not
      failures, and :attr:`reason` says which.
    * ``UNKNOWN`` — git could not answer; :attr:`reason` carries the stderr
      line or the spawn error.

    :attr:`detached` is a tri-state of its own, matching
    ``doctor.SourceFacts.detached`` exactly: ``True`` detached, ``False`` on a
    branch or provably not a work tree, ``None`` undetermined.
    """

    state: GitState
    name: Optional[str] = None
    detached: Optional[bool] = None
    reason: str = ""

    @property
    def is_known(self) -> bool:
        """``True`` when the probe produced a DETERMINATE answer.

        The guard to put in front of "compute the verdict" — a verdict derived
        while this is ``False`` is a guess wearing a fact's clothes.
        """
        return self.state is not GitState.UNKNOWN

    def describe(self) -> str:
        """One line for a log or a status field. Never the GUI copy."""
        if self.state is GitState.OK:
            return f"on branch {self.name}"
        if self.state is GitState.NOT_APPLICABLE:
            return self.reason or "no branch (detached HEAD or not a work tree)"
        return f"could not determine branch: {self.reason or 'unknown'}"


@dataclass(frozen=True)
class HeadState:
    """Is ``repo`` a git work tree whose HEAD resolves to a commit?

    * ``OK`` — yes; :attr:`sha` is the full HEAD sha.
    * ``NOT_APPLICABLE`` — provably not usable as a git repo here: no ``.git``,
      or git says this is not a work tree, or the repo has no commits yet.
      A determinate answer.
    * ``UNKNOWN`` — git could not be run / timed out / failed unexpectedly.

    The distinction exists because the two non-OK arms justify different
    downstream verdicts. ``deferral_probes._dist_dirty`` deliberately merges
    them (both mean "cannot conclude anything about dirtiness"), and says so at
    that call site — merging with a stated reason is fine; merging because the
    type could not express the difference is the defect.
    """

    state: GitState
    sha: Optional[str] = None
    reason: str = ""

    @property
    def is_usable(self) -> bool:
        """``True`` only for ``OK``. Both other arms are "do not conclude"."""
        return self.state is GitState.OK

    @property
    def is_known(self) -> bool:
        return self.state is not GitState.UNKNOWN


def run_git(
    repo: Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT_SECONDS
) -> tuple[Optional[int], str, str]:
    """Run read-only ``git -C <repo> <args>``. Returns ``(rc, out, err)``.

    ``rc is None`` means git could not be RUN at all (absent, timed out, spawn
    failure) — distinct from a git that ran and exited non-zero, because the
    two justify different verdicts: the first is UNKNOWN, the second is often a
    positive answer (``symbolic-ref`` exits 1 to SAY "detached").

    Same contract as ``vco_lib.doctor._git``, minus the ``GIT_TERMINAL_PROMPT``
    scrub that only the network leg needs — nothing here talks to a remote.
    Decode is pinned to UTF-8 rather than left to the locale: on a Windows
    runner ``text=True`` decodes with cp1252 and a repo path holding a
    non-ASCII character would raise ``UnicodeDecodeError`` out of an arm that
    catches only ``OSError``/``SubprocessError``.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is ours, never shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"git {' '.join(args)} timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, "", f"git {' '.join(args)} could not run: {exc}"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def linked_worktree_main(repo: Path) -> Optional[Path]:
    """The main checkout ``repo`` is a LINKED WORKTREE of, or None.

    A linked worktree is told by its own git dir differing from the common
    dir it shares (``git rev-parse --absolute-git-dir`` ≠ ``--git-common-dir``,
    both resolved) — never by the repository toplevel differing from
    ``repo``: a project folder nested inside a larger repository (a monorepo
    sub-project, a folder under a home-directory repository) has a toplevel
    above it and is still the main checkout (v0.2.107 WAVE2-CHECK M-2).

    None also covers the main checkout itself, a submodule (its git dir is
    its own common dir), a folder that is not a repository, and git that
    cannot run — every caller reads None as "not a worktree". The returned
    path is the parent of the common ``.git`` dir (the common dir itself for
    a bare repository).
    """
    rc, out, _err = run_git(
        repo, ["rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"]
    )
    lines = out.splitlines() if rc == 0 else []
    if len(lines) != 2:
        # git < 2.31 has no --path-format: --git-common-dir may then be
        # relative to ``repo``.
        rc, out, _err = run_git(repo, ["rev-parse", "--absolute-git-dir", "--git-common-dir"])
        lines = out.splitlines() if rc == 0 else []
        if len(lines) != 2:
            return None
    git_dir_raw, common_raw = (ln.strip() for ln in lines)
    if not git_dir_raw or not common_raw:
        return None
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = Path(repo) / common_path
    try:
        git_dir = Path(git_dir_raw).resolve()
        common = common_path.resolve()
    except (OSError, RuntimeError):
        return None
    if git_dir == common:
        return None
    return common.parent if common.name == ".git" else common


def committed_paths(repo: Path) -> Optional[set[str]]:
    """POSIX paths, relative to ``repo``, of every file in HEAD's tree under
    ``repo`` (``git ls-tree -r --name-only -z HEAD`` run from ``repo``, so a
    project nested in a larger repository gets its own subtree, relative to
    itself). None when git cannot answer (git unrunnable, no HEAD, an error):
    callers must read None as "cannot tell", never as "nothing committed"."""
    # The binary runner: run_git strips its output, which would clip a path
    # that starts with a space.
    rc, out, _err = run_git_binary(
        repo, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], timeout=60,
    )
    if rc != 0:
        return None
    text = out.decode("utf-8", errors="surrogateescape")
    return {p for p in text.split("\0") if p}


def is_git_work_tree(repo: Path) -> Optional[bool]:
    """True inside a git work tree, False when git ran and said it is not
    one, None when git could not run (cannot tell)."""
    rc, out, _err = run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if rc is None:
        return None
    return rc == 0 and out.strip() == "true"


def run_git_binary(
    repo: Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT_SECONDS
) -> tuple[Optional[int], bytes, bytes]:
    """Run read-only ``git -C <repo> <args>``, RAW bytes, no text decoding.

    v0.2.92 (WP-G): added for :func:`vco_lib.dist_binary_repair.stage_paths_from_head`,
    whose ``git show HEAD:<path>`` output is a compiled binary blob written
    straight to disk with ``Path.write_bytes``. :func:`run_git` decodes with
    ``encoding="utf-8", errors="replace"`` — correct for plumbing that returns
    text (a SHA, a branch name, a status line), but "replace" silently mangles
    any non-UTF-8 byte sequence, which is exactly what a compiled binary is
    made of. Using the text-mode runner there would stage corrupted bytes
    without raising anything — the caller would report success. This function
    exists so a binary-blob caller never has to reach for the text runner and
    get that wrong by omission.

    Same ``rc is None`` convention as :func:`run_git`: could-not-run vs.
    ran-and-said-no.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is ours, never shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, b"", f"git {' '.join(args)} timed out after {timeout}s".encode()
    except (subprocess.SubprocessError, OSError) as exc:
        return None, b"", f"git {' '.join(args)} could not run: {exc}".encode()
    return proc.returncode, proc.stdout or b"", proc.stderr or b""


def resolve_vco_version(orchestrator_root: Path) -> str:
    """Resolve the VCO version string for ``orchestrator_root``.

    Resolution chain:
      1. ``VERSION`` file at repo root (canonical for release tarballs
         where .git/ is absent).
      2. git ``rev-parse --short HEAD`` (development clone).
      3. ``"unknown"`` sentinel (no .git, no VERSION).

    Returns:
        Either a ``"vX.Y.Z"``-style tag string OR a 7-char short SHA OR
        the literal ``"unknown"``.
    """
    version_file = orchestrator_root / "VERSION"
    if version_file.is_file():
        try:
            raw = version_file.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                return raw if raw.startswith("v") else "v" + raw
        except OSError:
            pass
    sha = git_short_sha(orchestrator_root)
    return sha or "unknown"


def git_short_sha(repo: Path) -> Optional[str]:
    """Return ``git rev-parse --short HEAD`` of ``repo``, or None on error.

    Intentionally lossy: its callers want a version string or a fallback, and
    "no .git", "no commits" and "git is missing" all produce the same
    ``"unknown"`` there. When the DIFFERENCE matters, call :func:`head_state`.
    """
    if not (repo / ".git").exists():
        return None
    rc, out, _err = run_git(repo, ["rev-parse", "--short", "HEAD"])
    if rc != 0:
        return None
    return out or None


def head_state(repo: Path, *, require_dot_git: bool = True) -> HeadState:
    """Tri-state: is ``repo`` a work tree whose HEAD resolves to a commit?

    ``git rev-parse --verify HEAD`` is the probe — it exits non-zero both for
    "not a repository" and for "repository with no commits", and both of those
    are determinate NOT_APPLICABLE answers rather than failures. Only a git we
    could not RUN yields UNKNOWN.

    The absent-``.git`` short-circuit is kept from the pre-v0.2.92
    ``git_short_sha`` (it saves a subprocess on the common tarball-install
    path) but is now reported as NOT_APPLICABLE with a reason, instead of
    being indistinguishable from a spawn failure.

    ``require_dot_git=False`` drops that short-circuit: a project folder
    NESTED inside a larger repository has no ``.git`` of its own and still
    has a HEAD (the code-graph catch-up asks this way, WAVE2-CHECK M-2).
    """
    if require_dot_git and not (repo / ".git").exists():
        return HeadState(
            state=GitState.NOT_APPLICABLE,
            reason=f"{repo} has no .git — not a git work tree",
        )
    rc, out, err = run_git(repo, ["rev-parse", "--verify", "HEAD"])
    if rc is None:
        return HeadState(state=GitState.UNKNOWN, reason=err)
    if rc != 0:
        return HeadState(
            state=GitState.NOT_APPLICABLE,
            reason=err or f"git rev-parse --verify HEAD exited {rc}",
        )
    if not out:
        return HeadState(
            state=GitState.UNKNOWN,
            reason="git rev-parse --verify HEAD exited 0 with no output",
        )
    return HeadState(state=GitState.OK, sha=out)


def git_head_sha(repo: Path, *, require_dot_git: bool = True) -> Optional[str]:
    """FULL SHA of ``repo``'s HEAD, or ``None`` when it is not determinable.

    v0.2.92 (WP-G): thin wrapper over :func:`head_state` for callers that only
    want the lossy "a SHA or nothing" shape — e.g.
    :func:`vco_lib.codegraph_guards.provenance_line`'s ``analyzed_commit``
    field, which soft-fails to the string ``"none"`` on any non-OK state.
    Unlike :func:`git_short_sha` (7-char, ``rev-parse --short HEAD``), this
    returns the FULL 40-char SHA (``rev-parse --verify HEAD`` via
    :func:`head_state`), matching what ``git rev-parse HEAD`` (no ``--short``)
    used to return at each of this function's call sites before migration.
    When the difference between "not a repo", "no commits yet" and "git is
    unavailable" matters to the caller, use :func:`head_state` directly
    instead — this collapses all three to ``None``, same lossy trade-off as
    :func:`git_branch` over :func:`branch_state`.
    """
    state = head_state(repo, require_dot_git=require_dot_git)
    return state.sha if state.state is GitState.OK else None


def branch_state(repo: Path) -> BranchState:
    """Tri-state: which branch is ``repo`` on — or why is there no answer?

    Uses ``git symbolic-ref -q --short HEAD``, NOT ``rev-parse --abbrev-ref
    HEAD``. The difference is the whole point: ``rev-parse --abbrev-ref``
    returns the literal string ``"HEAD"`` at a detached head, so every caller
    has to special-case a branch named ``HEAD``-that-is-not-a-branch, and the
    five inline call sites the doctor lane found had each destroyed the fact
    while normalising it. ``symbolic-ref -q`` exits **1 to SAY "not a symbolic
    ref"** — a positive answer, which is why it can be told apart from
    failure.

    Returns:
        ``OK`` + :attr:`~BranchState.name` when attached;
        ``NOT_APPLICABLE`` + ``detached=True`` at a detached HEAD;
        ``NOT_APPLICABLE`` + ``detached=False`` when ``repo`` is not a work
        tree; ``UNKNOWN`` + a reason when git could not answer.
    """
    if not (repo / ".git").exists():
        return BranchState(
            state=GitState.NOT_APPLICABLE,
            detached=False,
            reason=f"{repo} has no .git — not a git work tree",
        )
    rc, out, err = run_git(repo, ["symbolic-ref", "-q", "--short", "HEAD"])
    if rc is None:
        return BranchState(state=GitState.UNKNOWN, reason=err)
    if rc == 0 and out:
        return BranchState(state=GitState.OK, name=out, detached=False)
    if rc == 1:
        # Documented exit code for "HEAD is not a symbolic ref" — the positive
        # detached answer. `-q` is what makes 1 mean this and not "some error".
        return BranchState(
            state=GitState.NOT_APPLICABLE,
            detached=True,
            reason="detached HEAD — no branch is checked out",
        )
    if rc == 128:
        # git's "fatal:" class. The common member is "not a git repository",
        # which is determinate; but 128 also covers a corrupt/unreadable repo,
        # so it is only NOT_APPLICABLE when git says so in as many words.
        low = err.lower()
        if "not a git repository" in low or "does not exist" in low:
            return BranchState(
                state=GitState.NOT_APPLICABLE, detached=False, reason=err
            )
        return BranchState(state=GitState.UNKNOWN, reason=err)
    return BranchState(
        state=GitState.UNKNOWN,
        reason=err or f"git symbolic-ref exited {rc}",
    )


def git_branch(repo: Path) -> Optional[str]:
    """Branch NAME of ``repo``, or ``None``. **Lossy — see the warning.**

    ``None`` means any of: detached HEAD, not a git work tree, or git could not
    be asked. If your caller would BEHAVE DIFFERENTLY across those three — and
    most do — call :func:`branch_state` instead; that is the whole reason it
    exists.

    Kept as a thin DERIVED accessor rather than a second resolution, so the two
    can never disagree about what a branch is, and so the loss happens in
    exactly one place that names it. ``tests/test_v0292_regclean_git_meta.py``
    pins the derivation (every ``BranchState`` shape is checked against what
    this returns), which is what would fail if someone re-implemented it.

    Historical note for whoever reads this next: until v0.2.92 THIS was the
    only branch accessor, and it collapsed detached-HEAD into every error. It
    had no production consumers at the time, so nothing shipped a wrong verdict
    from it — the fix landed before the first caller, which is the only cheap
    moment to fix a shape like this.
    """
    return branch_state(repo).name
