# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 (WP-G) — the two NEW `vco_lib.git_meta` consumers are actually wired.

`tests/test_v0292_regclean_git_meta.py` already pins `deferral_probes`'s
wiring onto `git_meta.head_state` (the promise that survived thirty-eight
releases unwired, R16 category 1 / R24). This file adds the same class of
proof for the two consumers WP-G migrated in this cycle:

* `vco_lib.dist_binary_repair` — `dist_dirty_paths`, `restore_paths_from_head`,
  `stage_paths_from_head` used to carry a private `_run_git` wrapping
  `subprocess.run` directly. It is gone; all three now call
  `git_meta.run_git` / `git_meta.run_git_binary`.
* `vco_lib.codegraph_guards` — `provenance_line` used to spawn
  `subprocess.run(["git", "rev-parse", "HEAD"], ...)` inline. It now calls
  `git_meta.git_head_sha`.

Each test monkeypatches at the `git_meta` END (not at `subprocess`), so a
regression that reintroduces a private git spawn in either consumer — the
exact way `git_meta` lost its consumers the first time — fails here even if
the consumer's own git-shaped output still happens to look right.

Tri-OS (R12/R14): every assertion is either a pure monkeypatch (OS-independent
by construction) or drives a real repo through the shared `tempfile`/`Path`
machinery already used by `tests/test_v0291_dist_binary_repair.py` and
`tests/test_codegraph_guards_v0282.py` — no separator literals, no shell-outs
outside of git itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vco_lib import codegraph_guards as guards
from vco_lib import dist_binary_repair
from vco_lib import git_meta

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "WP-G Test",
    "GIT_AUTHOR_EMAIL": "wp-g@example.invalid",
    "GIT_COMMITTER_NAME": "WP-G Test",
    "GIT_COMMITTER_EMAIL": "wp-g@example.invalid",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One-commit repo with a tracked dist binary, mirroring
    `tests/test_v0291_dist_binary_repair.py`'s own fixture shape."""
    _git(tmp_path, "init", "-q", "-b", "trunk")
    (tmp_path / "launcher" / "dist" / "linux-x64").mkdir(parents=True)
    bin_path = tmp_path / "launcher" / "dist" / "linux-x64" / "vct-launcher"
    bin_path.write_bytes(b"ORIGINAL-BYTES")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


# ── dist_binary_repair: dist_dirty_paths ──────────────────────────────────


@needs_git
def test_dist_dirty_paths_routes_through_git_meta_run_git_binary(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """WIRING: `dist_dirty_paths` calls `git_meta.run_git_binary`, not its own
    `subprocess.run`. Fails if the private `_run_git` this migration removed
    is reintroduced under a different name."""
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real = git_meta.run_git_binary

    def spy(install_root, args, **kwargs):
        calls.append((install_root, tuple(args)))
        return real(install_root, args, **kwargs)

    monkeypatch.setattr(git_meta, "run_git_binary", spy)

    (repo / "launcher" / "dist" / "linux-x64" / "vct-launcher").write_bytes(
        b"MODIFIED-BYTES"
    )
    result = dist_binary_repair.dist_dirty_paths(repo, "launcher/dist")

    assert calls, "dist_dirty_paths never called git_meta.run_git_binary"
    assert calls[0][1][:2] == ("status", "--porcelain")
    assert result == ["launcher/dist/linux-x64/vct-launcher"]


@needs_git
def test_dist_dirty_paths_porcelain_leading_space_is_not_eaten(repo: Path) -> None:
    """RED-PROOF for the migration bug this WP-G pass introduced and then
    fixed in the same session: routing this call through the TEXT-mode
    `git_meta.run_git` (which `.strip()`s the WHOLE output) ate the leading
    space of a single-line ` M <path>` porcelain status, shearing the first
    character off the reported path. `run_git_binary` (no strip) is required
    here specifically. This test fails immediately if `dist_dirty_paths` is
    ever changed back to `git_meta.run_git`.
    """
    target = repo / "launcher" / "dist" / "linux-x64" / "vct-launcher"
    target.write_bytes(b"MODIFIED-BYTES")
    result = dist_binary_repair.dist_dirty_paths(repo, "launcher/dist")
    assert result == ["launcher/dist/linux-x64/vct-launcher"]
    # The off-by-one this guards against would have produced:
    assert result != ["auncher/dist/linux-x64/vct-launcher"]


# ── dist_binary_repair: restore_paths_from_head ───────────────────────────


@needs_git
def test_restore_paths_from_head_routes_through_git_meta_run_git(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real = git_meta.run_git

    def spy(install_root, args, **kwargs):
        calls.append((install_root, tuple(args)))
        return real(install_root, args, **kwargs)

    monkeypatch.setattr(git_meta, "run_git", spy)

    rel = "launcher/dist/linux-x64/vct-launcher"
    (repo / rel).write_bytes(b"DIVERGED")
    restored, failed = dist_binary_repair.restore_paths_from_head(repo, [rel])

    assert calls, "restore_paths_from_head never called git_meta.run_git"
    assert calls[0][1] == ("checkout", "HEAD", "--", rel)
    assert restored == [rel]
    assert failed == []
    assert (repo / rel).read_bytes() == b"ORIGINAL-BYTES"


# ── dist_binary_repair: stage_paths_from_head ─────────────────────────────


@needs_git
def test_stage_paths_from_head_routes_through_git_meta_run_git_binary(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Also proves the binary-safety property end to end: staging a blob that
    is NOT valid UTF-8 must round-trip byte-for-byte. A text-mode runner using
    `errors="replace"` would corrupt this on the very first non-UTF-8 byte."""
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real = git_meta.run_git_binary

    def spy(install_root, args, **kwargs):
        calls.append((install_root, tuple(args)))
        return real(install_root, args, **kwargs)

    monkeypatch.setattr(git_meta, "run_git_binary", spy)

    rel = "launcher/dist/linux-x64/vct-launcher"
    non_utf8_blob = b"\xff\xfe\x00BINARY\x80\x81\xffMORE"
    (repo / rel).write_bytes(non_utf8_blob)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "non-utf8 binary")

    staged, failed = dist_binary_repair.stage_paths_from_head(repo, [rel])

    assert calls, "stage_paths_from_head never called git_meta.run_git_binary"
    assert calls[0][1] == ("show", f"HEAD:{rel}")
    assert staged == [rel]
    assert failed == []
    staged_sibling = dist_binary_repair.staged_sibling(repo / rel)
    assert staged_sibling.read_bytes() == non_utf8_blob, (
        "binary blob was corrupted in transit — the text-mode runner's "
        "errors='replace' would do exactly this"
    )


# ── codegraph_guards: provenance_line ─────────────────────────────────────


def test_provenance_line_routes_through_git_meta_git_head_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WIRING: `provenance_line` calls `git_meta.git_head_sha`, not its own
    `subprocess.run(["git", "rev-parse", "HEAD"])`."""
    calls: list[Path] = []

    def fake(repo):
        calls.append(repo)
        return "deadbeefcafefeed" * 2 + "dead0000"  # 40 hex chars

    monkeypatch.setattr(git_meta, "git_head_sha", fake)

    line = guards.provenance_line("m", 8, 1, "/some/repo")

    assert calls == [Path("/some/repo")]
    assert "analyzed_commit=deadbeefcafefeeddeadbeefcafefeeddead0000" in line


def test_provenance_line_soft_fails_when_git_head_sha_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_meta, "git_head_sha", lambda repo: None)
    line = guards.provenance_line("m", 8, 1, "/nope")
    assert "analyzed_commit=none" in line


def test_provenance_line_soft_fails_when_git_head_sha_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(repo):
        raise RuntimeError("simulated git_meta failure")

    monkeypatch.setattr(git_meta, "git_head_sha", boom)
    # Must not raise — a provenance failure must never fail a build.
    line = guards.provenance_line("m", 8, 1, "/nope")
    assert "analyzed_commit=none" in line


@needs_git
def test_provenance_line_on_a_real_repo_matches_git_rev_parse_head(
    tmp_path: Path,
) -> None:
    """End-to-end (no monkeypatch): a real repo's SHA reaches the line
    unmangled through the new `git_head_sha` wrapper."""
    _git(tmp_path, "init", "-q", "-b", "trunk")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    line = guards.provenance_line("m", 8, 1, str(tmp_path))
    assert f"analyzed_commit={expected}" in line
