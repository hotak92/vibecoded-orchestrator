# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 L-KGREV2 — the KG reviewer keeps only the LATEST status.

Owner request 2026-11-04: the post-commit reviewer appends one
``## Status after <sha>`` section per commit (prompt rule 8), so a decision
refined several times in a row collects a CHAIN of sections, each
superseding the last, and readers and search hits land on the stale
statements first. Only the latest consolidated view may remain — but the
OWNER's own lines must never become removable.

What this file proves (real temp git repos, the ``claude`` CLI always
faked):

* **provenance, not trust** — a section is foldable only when ``git blame``
  attributes its EVERY non-blank line to a reviewer ``kg-sync:`` commit
  (``vco_lib.kg_reviewer_guard.blame_reviewer_lines`` /
  ``reviewer_sections``);
* **the fold exception** — a write may remove lines from a foldable
  section only under a ``Superseded: <sha> ->`` trail line naming it; a
  still-true fact dropped without its trail line is REVERTED (the
  coordinator's clarification: "old ones not superseded need to be kept");
* **everything else stays append-only** — a section with one owner line, an
  owner line anywhere, an uncommitted reviewer line, a blame failure: all
  revert exactly as before;
* **the prompt lists exactly the foldable sections** (the backend computes
  provenance, never the model);
* **the debounce** — commits arriving while a review runs join a queue and
  ONE later pass covers them all (``vco_lib.kg_reviewer_queue``), with no
  commit lost.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"

for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402
from vco_lib import kg_reviewer_queue as queue_mod  # noqa: E402

_REAL_RUN = subprocess.run

OWNER_NODE = """---
title: Deploy pipeline
type: concept
tags: [ci]
status: active
updated: 2026-01-01T00:00:00Z
---

# Deploy pipeline

Deploys are documented in docs/deploy.md and run through the blue pipeline.
The owner's own note survives every review.
"""

#: The reviewer's first Status section (committed under a `kg-sync:`
#: subject, so blame proves every line of it is the reviewer's).
SECTION_1 = """

## Status after 1111111

The blue pipeline was replaced by the green pipeline.
The deploy timeout is 300 s.
"""

#: A second, later Status section superseding part of the first.
SECTION_2 = """

## Status after 2222222

The deploy timeout is 600 s.
"""

#: The fold: one consolidated current-state section plus the trail, one
#: line per folded section.
FOLDED = """

## Current state (consolidated 2026-11-04)

- Built: deploys run through the green pipeline.
- Decided: the deploy timeout is 600 s.

Superseded: 1111111 -> 3333333 (2026-11-04)
Superseded: 2222222 -> 3333333 (2026-11-04)
"""

#: A third section the reviewer wrote but NEVER committed (its lines are
#: nobody's, as far as blame can prove).
SECTION_3_UNCOMMITTED = """

## Status after 4444444

An uncommitted reading that must not become removable.
"""


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose node carries TWO committed reviewer Status sections
    (each folded from a real ``kg-sync:`` commit, so blame proves them)."""
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs").mkdir(parents=True)
    rel = "knowledge/concepts/deploy.md"
    (r / rel).write_text(OWNER_NODE)
    (r / "docs" / "deploy.md").write_text("v1\n")
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "docs: the deploy node")
    # The reviewer's two earlier passes, each committed as kg-sync:
    (r / rel).write_text(OWNER_NODE + SECTION_1)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "kg-sync: 1111111 feat: green pipeline")
    (r / rel).write_text(OWNER_NODE + SECTION_1 + SECTION_2)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "kg-sync: 2222222 feat: timeout 600")
    # The commit the (third) review runs for: it changes the cited file.
    (r / "docs" / "deploy.md").write_text("v2\n")
    _git(r, "commit", "-q", "-am", "feat: 3333333 revise deploy doc")
    return r


REL = "knowledge/concepts/deploy.md"


def _judge(repo: Path, new_text: str
           ) -> Tuple[guard.ValidationResult, List[str], str]:
    """Snapshot, land the reviewer's write, validate; return (result, log
    lines, what the file holds now)."""
    snap = guard.take_snapshot(repo)
    try:
        writes: "dict[Path, list[guard.WriteOp]]" = {
            guard.path_key(repo / REL, repo.resolve()):
                [("write", new_text)]}
        (repo / REL).write_text(new_text)
        lines: List[str] = []
        res = guard.validate(snap, writes, lines.append, sources="")
        return res, lines, (repo / REL).read_text()
    finally:
        snap.cleanup()


# ═════════════════════════════════════════════════════════════════════════
# 1. The fold: provenance, the trail, and the owner's lines
# ═════════════════════════════════════════════════════════════════════════


def test_an_all_reviewer_status_chain_folds_into_one_current_state(repo):
    """The target shape: two committed reviewer sections become ONE
    consolidated section, both steps named on the trail."""
    res, lines, now = _judge(repo, OWNER_NODE + FOLDED)
    assert now == OWNER_NODE + FOLDED
    assert res.kept == [REL], (res.reverted, lines)
    assert res.reverted == []


def test_a_superseded_fact_dropped_with_its_trail_line_is_allowed(repo):
    """The 300 s timeout (section 1111111) is dropped, never carried over —
    allowed because the trail names 1111111."""
    folded = OWNER_NODE + FOLDED.replace(
        "- Decided: the deploy timeout is 600 s.",
        "- Decided: the deploy timeout is 600 s (was 300 s until 2222222).")
    res, _lines, now = _judge(repo, folded)
    assert now == folded and res.kept == [REL] and res.reverted == []


def test_a_still_true_fact_dropped_without_a_trail_line_is_reverted(repo):
    """Coordinator's clarification: a fact the fold drops while claiming
    nothing supersedes it (no trail line names its section) is LOST, and
    the whole write reverts."""
    folded = (OWNER_NODE + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- Built: deploys run through the green pipeline.\n\n"
              "Superseded: 1111111 -> 3333333 (2026-11-04)\n")
    # section 2222222's still-current "600 s" fact is gone and no trail
    # line names 2222222
    res, lines, now = _judge(repo, folded)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert "removed or reworded 2 original line(s)" in res.reverted[0][1]
    assert any("'## Status after 2222222'" in ln for ln in lines)


def test_a_trail_line_alone_does_not_unlock_a_fold_without_one(repo):
    """ASCII and unicode arrows are both trails — and neither unlocks
    anything when the section it names is not the one removed."""
    folded = (OWNER_NODE + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- Decided: the deploy timeout is 600 s.\n\n"
              "Superseded: 1111111 → 3333333 (2026-11-04)\n")
    # the green-pipeline fact of 1111111 was carried, 300 s dropped with
    # 1111111 named — but 2222222's section was removed unnamed
    res, _lines, now = _judge(repo, folded)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert res.reverted and res.kept == []


def test_a_section_with_one_owner_line_is_not_foldable(repo):
    """One owner line inside the section breaks the provenance of the WHOLE
    section: folding it (dropping that line too) reverts the write."""
    mixed = OWNER_NODE + SECTION_1.replace(
        "The deploy timeout is 300 s.",
        "The deploy timeout is 300 s.\nAn owner line inside the section.")
    (repo / REL).write_text(mixed + SECTION_2)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: owner note inside the section")
    # the fold write replaces BOTH sections (the trail names both) and so
    # drops the owner's mixed-in line with them
    folded = (OWNER_NODE
              + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- Built: deploys run through the green pipeline.\n"
              "- Decided: the deploy timeout is 600 s.\n\n"
              "Superseded: 1111111 -> 3333333 (2026-11-04)\n"
              "Superseded: 2222222 -> 3333333 (2026-11-04)\n")
    res, _lines, now = _judge(repo, folded)
    assert now == mixed + SECTION_2
    assert res.reverted and res.kept == []
    # the whole mixed section counts as unjustified: its heading, both
    # reviewer facts AND the owner's line (section 2222222 folds legally,
    # its trail line names it)
    assert "removed or reworded 4 original line(s)" in res.reverted[0][1]


def test_an_owner_line_anywhere_removed_is_reverted(repo):
    """The fold may never touch the owner's own body, trail or not."""
    folded = (OWNER_NODE.replace(
        "The owner's own note survives every review.\n", "")
        + FOLDED)
    res, lines, now = _judge(repo, folded)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert "removed or reworded 1 original line(s)" in res.reverted[0][1]
    assert any("owner's own note" in ln for ln in lines)


def test_uncommitted_reviewer_lines_are_not_foldable(repo):
    """A Status section the reviewer wrote but never committed is provably
    nobody's: folding it (even with a trail line naming it) reverts."""
    uncommitted = OWNER_NODE + SECTION_1 + SECTION_2 + SECTION_3_UNCOMMITTED
    (repo / REL).write_text(uncommitted)
    folded = (OWNER_NODE + FOLDED + "\n"
              "Superseded: 4444444 -> 3333333 (2026-11-04)\n")
    res, lines, now = _judge(repo, folded)
    assert now == uncommitted
    assert res.reverted and res.kept == []
    assert any("uncommitted reading" in ln for ln in lines)


def test_blame_failure_keeps_the_append_only_rule(repo):
    """No commits to blame (HEAD deleted) => no provenance => append-only."""
    _git(repo, "update-ref", "-d", "HEAD")
    res, _lines, now = _judge(repo, OWNER_NODE + FOLDED)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert res.reverted and res.kept == []


def test_pure_append_still_needs_nothing(repo):
    """The ordinary rule-8 branch is unchanged: appending a new Status
    section is kept without any provenance."""
    new = (OWNER_NODE + SECTION_1 + SECTION_2
           + "\n## Status after 3333333\n\nThe doc was revised.\n")
    res, _lines, now = _judge(repo, new)
    assert now == new and res.kept == [REL] and res.reverted == []


# ═════════════════════════════════════════════════════════════════════════
# 2. The prompt: the backend names the foldable sections
# ═════════════════════════════════════════════════════════════════════════


def test_prompt_lists_exactly_the_foldable_sections(repo):
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04")
    assert "## Reviewer Status sections this pass may fold" in text
    assert "`## Status after 1111111`" in text
    assert "`## Status after 2222222`" in text
    # rule 8 states the fold and the trail the guard enforces
    assert "## Current state (consolidated 2026-11-04)" in text
    assert "Superseded: <old short-sha> → " + commit.short in text
    assert "either survives in the new section or has its" in text
    # the append branch is still there for everything not listed
    assert f"## Status after {commit.short}" in text
    assert "mark superseded lines IN PLACE per rule 10" in text


def test_prompt_lists_only_the_proven_sections_of_a_mixed_node(repo):
    """Granularity: the mixed section (an owner line inside it) is not
    listed; the later, still-pure one is."""
    mixed = OWNER_NODE + SECTION_1.replace(
        "The deploy timeout is 300 s.",
        "The deploy timeout is 300 s.\nAn owner line inside the section.")
    (repo / REL).write_text(mixed + SECTION_2)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: owner note inside the section")
    (repo / "docs" / "deploy.md").write_text("v3\n")
    _git(repo, "commit", "-q", "-am", "feat: revise again")
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04")
    assert "`## Status after 1111111`" not in text
    assert "`## Status after 2222222`" in text


def test_prompt_names_the_coalesced_commits(repo):
    commit = prompt_mod.collect_commit(repo)
    other = prompt_mod.collect_commit(
        repo, _git(repo, "rev-parse", "HEAD~1").strip())
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04",
                                   also=(other,))
    assert "### Also covered by this pass" in text
    assert "1 further unreviewed commit(s)" in text
    assert f"`{other.short}` - {other.subject}" in text


# ═════════════════════════════════════════════════════════════════════════
# 3. The debounce queue
# ═════════════════════════════════════════════════════════════════════════


def test_the_queue_module_locks_enqueues_and_drains(repo):
    assert queue_mod.acquire(repo)
    assert not queue_mod.acquire(repo), "a second live run must not enter"
    queue_mod.release(repo)
    assert queue_mod.acquire(repo)
    queue_mod.release(repo)
    # enqueue/drain are FIFO, de-duplicated, and clear the file
    queue_mod.enqueue(repo, "a" * 40)
    queue_mod.enqueue(repo, "b" * 40)
    queue_mod.enqueue(repo, "a" * 40)
    assert queue_mod.drain(repo) == ["a" * 40, "b" * 40]
    assert queue_mod.drain(repo) == []
    assert queue_mod.queue_path(repo).read_text(encoding="utf-8") == ""


def test_release_never_removes_a_successors_lock(repo):
    assert queue_mod.acquire(repo)
    queue_mod.lock_path(repo).write_text(
        json.dumps({"pid": 999999, "started": 1}), encoding="utf-8")
    queue_mod.release(repo)  # not ours any more
    assert queue_mod.lock_path(repo).exists()
    queue_mod.lock_path(repo).unlink()


def test_a_stale_lock_is_stolen_with_a_log_line(repo):
    path = queue_mod.lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # a pid that is provably dead (a finished child)
    proc = subprocess.Popen(["true"])
    proc.wait()
    path.write_text(json.dumps({"pid": proc.pid, "started": 0}),
                    encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    lines: List[str] = []
    assert queue_mod.acquire(repo, stale_s=60.0, log=lines.append)
    assert any("stale" in ln for ln in lines)
    queue_mod.release(repo)


def _load_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sb = _load("_v02105_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_v02105_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    monkeypatch.delenv("VCO_KG_REVIEW_MAX_TURNS", raising=False)
    monkeypatch.delenv("VCO_KG_REVIEW_MODEL", raising=False)
    return mod


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_backend(chain, monkeypatch, prompts: List[str]) -> None:
    """Make every pass a no-op review that records its prompt."""
    monkeypatch.setattr(
        chain, "select_reviewer_backend",
        lambda log=None, model=None: {"mode": "claude", "model": "sonnet",
                                      "base_url": None, "reason": "test"})
    monkeypatch.setattr(chain, "resolve_claude_executable",
                        lambda: "/usr/bin/claude")

    def fake_run(cmd, prompt, env, cwd, timeout_s, *, direct_rung, log):
        prompts.append(prompt)
        return chain.ReviewRun("", "", 0, ("", "", 0))

    monkeypatch.setattr(chain, "run_with_quota_fallback", fake_run)


def test_one_pass_covers_the_queued_commits_and_loses_none(
        repo, tmp_path, monkeypatch):
    chain = _load_backend(tmp_path, monkeypatch)
    prompts: List[str] = []
    _fake_backend(chain, monkeypatch, prompts)
    shas = [_git(repo, "rev-parse", "HEAD~2").strip(),
            _git(repo, "rev-parse", "HEAD~1").strip(),
            _git(repo, "rev-parse", "HEAD").strip()]
    shorts = [_git(repo, "rev-parse", "--short", s).strip() for s in shas]
    # a review is "in flight": two commits arrive and must only queue
    assert queue_mod.acquire(repo)
    rc1 = chain.main(["run", "--project-root", str(repo), "--commit",
                      shas[1][:12], "--log", str(tmp_path / "r.log")])
    rc2 = chain.main(["run", "--project-root", str(repo), "--commit",
                      shas[2][:12], "--log", str(tmp_path / "r.log")])
    assert rc1 == rc2 == 0 and prompts == []
    # the queue holds both commits (nothing lost so far)
    queued = _read_queue(repo)
    assert shas[1] in queued and shas[2] in queued
    # the in-flight run ends; the next commit's run coalesces the queue
    queue_mod.release(repo)
    rc = chain.main(["run", "--project-root", str(repo), "--commit",
                     shas[2][:12], "--log", str(tmp_path / "r.log")])
    assert rc == 0
    # ONE pass for its own commit + ONE coalesced pass for the queue: the
    # queued commits are covered by that single pass, not one run each
    assert len(prompts) == 2, [p[:80] for p in prompts]
    assert shorts[2] in "\n".join(prompts)  # its own commit
    assert "### Also covered by this pass" in prompts[1]
    assert "1 further unreviewed commit(s)" in prompts[1]
    assert shorts[1] in prompts[1]  # the older queued commit is named
    assert queue_mod.drain(repo) == []  # nothing left behind


def _read_queue(repo: Path) -> List[str]:
    path = queue_mod.queue_path(repo)
    try:
        return [ln.strip() for ln in
                path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def test_arrivals_past_the_pass_budget_spawn_a_successor(
        repo, tmp_path, monkeypatch):
    chain = _load_backend(tmp_path, monkeypatch)
    prompts: List[str] = []
    _fake_backend(chain, monkeypatch, prompts)
    late = _git(repo, "rev-parse", "HEAD").strip()
    # drain answers: first loop check -> one queued commit, second -> empty,
    # then (after release) -> one late arrival
    answers = iter([[late], [], [late]])
    monkeypatch.setattr(chain._queue, "drain", lambda root: next(answers))
    spawned: List[str] = []
    monkeypatch.setattr(chain, "_spawn_successor",
                        lambda args, log, root, sha: spawned.append(sha)
                        or True)
    rc = chain.main(["run", "--project-root", str(repo),
                     "--log", str(tmp_path / "r.log")])
    assert rc == 0
    assert len(prompts) == 2  # the pass + the one coalesced pass
    assert spawned == [late], "the late arrival must reach a successor"

