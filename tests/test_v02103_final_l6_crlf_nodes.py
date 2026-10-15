# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review L-6 — the KG reviewer guard on CRLF nodes.

The CLI's edit tools keep a file's own line endings while the transcript
carries LF. The guard's ownership check already tolerated that (``_same``),
but its RULES compared the raw pre/post texts: a CRLF locked node whose
reviewer edit came back with LF endings read as a rewrite of the body, so a
legitimate "Proposed change" append was reverted. The rules now read CRLF as
LF for comparison only; on disk a revert restores the snapshot's exact bytes
(the user's CRLF), and a kept CRLF node is committed as written.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import kg_reviewer_guard as guard  # noqa: E402

LOCKED = ("---\ntitle: Storage decision\ntype: concept\ntags: [decision]\n"
          "status: active\nupdated: 2026-01-01T00:00:00Z\n---\n\n"
          "# Storage decision\n\nWe store blobs on disk.\n")
PLAIN = ("---\ntitle: Cache layer\ntype: concept\ntags: [cache]\n"
         "status: active\nupdated: 2026-01-01T00:00:00Z\n---\n\n"
         "# Cache layer\n\nThe cache keeps 100 entries.\n")
PROPOSAL = "\n## Proposed change (kg-reviewer, 2026-10-15)\n\nBlobs moved to S3 (source: src.py).\n"


def _crlf(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "proj"
    (r / "knowledge").mkdir(parents=True)
    (r / "knowledge" / "decision.md").write_bytes(_crlf(LOCKED))
    (r / "knowledge" / "cache.md").write_bytes(_crlf(PLAIN))
    _git(r, "init", "-q")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _validate(repo: Path, rel: str, on_disk: bytes, op: tuple):
    snap = guard.take_snapshot(repo)
    (repo / rel).write_bytes(on_disk)
    writes = {guard.path_key(repo / rel, repo.resolve()): [op]}
    lines: List[str] = []
    res = guard.validate(snap, writes, lines.append, resync=lambda _r: None,
                         sources="")
    return snap, res, lines


def test_proposal_append_to_a_crlf_locked_node_is_kept_when_endings_changed(repo):
    """RED before the fix: the tool left LF endings on a CRLF node, the body
    no longer started with the CRLF original, and the legitimate append was
    REVERTED as "edited a locked node"."""
    rel = "knowledge/decision.md"
    post = LOCKED + PROPOSAL
    snap, res, lines = _validate(repo, rel, post.encode("utf-8"),
                                 ("write", post))
    try:
        assert res.kept == [rel], lines
        assert not res.reverted
    finally:
        snap.cleanup()


def test_proposal_append_keeping_crlf_is_kept_and_committed_as_written(repo):
    rel = "knowledge/decision.md"
    post = LOCKED + PROPOSAL
    old = "We store blobs on disk.\n"
    snap, res, lines = _validate(repo, rel, _crlf(post),
                                 ("edit", [(old, old + PROPOSAL, False)]))
    try:
        assert res.kept == [rel], lines
        log: List[str] = []
        sha = guard.commit_reviewer_changes(
            snap, res.kept, reviewer_bytes=res.reviewer_bytes, short="abc1234",
            subject="feat: s3", model="sonnet", attributed=True, log=log.append)
    finally:
        snap.cleanup()
    assert sha, log
    committed = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=repo,
                               check=True, capture_output=True).stdout
    assert committed == _crlf(post), "the user's CRLF endings are committed as written"


def test_rewrite_of_a_crlf_locked_node_is_reverted_to_its_exact_bytes(repo):
    """LEAVE-ALONE of the rules: a real body rewrite is still a violation,
    and the revert restores the user's CRLF bytes, not an LF copy."""
    rel = "knowledge/decision.md"
    original = (repo / rel).read_bytes()
    post = LOCKED.replace("We store blobs on disk.", "We store blobs in S3.")
    snap, res, lines = _validate(repo, rel, _crlf(post), ("write", post))
    try:
        assert res.kept == []
        assert res.reverted and "locked node" in res.reverted[0][1], lines
        assert (repo / rel).read_bytes() == original
    finally:
        snap.cleanup()


def test_frontmatter_rewrite_of_a_crlf_node_is_reverted(repo):
    rel = "knowledge/cache.md"
    original = (repo / rel).read_bytes()
    post = PLAIN.replace("tags: [cache]", "tags: [cache, decision]")
    snap, res, lines = _validate(repo, rel, _crlf(post), ("write", post))
    try:
        assert res.reverted and "frontmatter" in res.reverted[0][1], lines
        assert (repo / rel).read_bytes() == original
    finally:
        snap.cleanup()


def test_updated_stamp_only_on_a_crlf_node_is_kept(repo):
    rel = "knowledge/cache.md"
    post = PLAIN.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z")
    snap, res, lines = _validate(repo, rel, post.encode("utf-8"),
                                 ("write", post))
    try:
        assert res.kept == [rel], lines
    finally:
        snap.cleanup()
