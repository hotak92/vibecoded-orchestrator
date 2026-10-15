# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — KG reviewer source tracking: citing-node lookup + prompt.

Field defect: the post-commit reviewer created a node summarising a report
that was still being revised; later commits superseded parts of the report
(a rejected proposal was removed), but the node was never updated — the
reviewer found related nodes only through hybrid_search, and nothing
recorded which revision a node reflected. This file pins the fix:

* a DETERMINISTIC lookup — a plain-text match of the commit's changed
  project-relative paths inside ``knowledge/**/*.md`` (Windows-style ``\\``
  separators matched too), capped with an honest truncation count, empty on
  a project with no ``knowledge/`` dir — handed to the prompt as "Nodes that
  cite files this commit changed";
* the prompt rules: a node built from a document records its source path
  and commit; a citing node whose source changed gets an appended
  "## Status after <short-sha>" block (append-and-annotate); a summary of a
  document visibly in progress is marked as a snapshot — reconciled with
  the no-status-tracking-nodes rule;
* the guard boundaries are unchanged: an appended "Status after" block on an
  UNLOCKED node is accepted, a rewording is still reverted, and a LOCKED
  node still takes only its Proposed-change section.

Every test drives the real code against a temp git repo; no real model, no
live Weaviate (the suite pins the unroutable sentinel).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402

_REAL_RUN = subprocess.run

REPORT_NODE = """---
title: Report summary
type: concept
tags: [report]
status: active
updated: 2026-01-01T00:00:00Z
---

# Report summary

Summarises docs/report.md. The report proposes X (docs/report.md, rev 1).
"""

OTHER_NODE = """---
title: Other summary
type: concept
tags: [report]
status: active
updated: 2026-01-01T00:00:00Z
---

# Other summary

Summarises docs/other.md instead.
"""

LOCKED_NODE = """---
title: Locked report summary
type: decision
tags: [decision]
status: active
updated: 2026-01-01T00:00:00Z
---

# Locked report summary

Summarises docs/report.md. Chosen: proposal X.
"""


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


def _git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)


def _node(title: str, body: str) -> str:
    return (f"---\ntitle: {title}\ntype: concept\ntags: [x]\n"
            f"created: 2026-10-15\nupdated: 2026-10-15\nstatus: active\n---\n\n{body}\n")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose HEAD commit changed docs/report.md; two nodes, one
    citing that path and one citing docs/other.md."""
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "knowledge" / "concepts" / "report-summary.md").write_text(REPORT_NODE)
    (r / "knowledge" / "concepts" / "other-summary.md").write_text(OTHER_NODE)
    (r / "docs" / "report.md").write_text("# Report\n\nProposes X.\n")
    (r / "docs" / "other.md").write_text("# Other\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "docs" / "report.md").write_text("# Report\n\nProposal X was removed.\n")
    _git(r, "commit", "-q", "-am", "feat: drop proposal X from the report")
    return r


# ── the deterministic lookup ──────────────────────────────────────────────


def test_lookup_returns_citing_nodes_for_changed_paths_only(repo):
    sha = _git(repo, "rev-parse", "HEAD").strip()
    nodes, more = prompt_mod.citing_nodes(repo, sha)
    assert nodes == ["knowledge/concepts/report-summary.md"], nodes
    assert more == 0


def test_lookup_matches_windows_style_separators(tmp_path, monkeypatch):
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs").mkdir()
    # node text written on Windows cites the path with backslashes
    (r / "knowledge" / "concepts" / "report-summary.md").write_text(
        REPORT_NODE.replace("docs/report.md", "docs\\report.md"))
    (r / "docs" / "report.md").write_text("# Report\n\nProposes X.\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "docs" / "report.md").write_text("v2\n")
    _git(r, "commit", "-q", "-am", "feat: revise")
    nodes, _more = prompt_mod.citing_nodes(r, _git(r, "rev-parse", "HEAD").strip())
    assert nodes == ["knowledge/concepts/report-summary.md"], nodes


def test_lookup_caps_the_list_and_reports_the_truncation(tmp_path, monkeypatch):
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs").mkdir()
    cap = prompt_mod.config()["prompt"]["citing_nodes_max"]
    assert isinstance(cap, int) and cap > 0
    for i in range(cap + 1):
        (r / "knowledge" / "concepts" / f"n{i:03d}.md").write_text(
            _node(f"N{i}", f"Cites docs/report.md (copy {i})."))
    (r / "docs" / "report.md").write_text("# Report\n\nProposes X.\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "docs" / "report.md").write_text("v2\n")
    _git(r, "commit", "-q", "-am", "feat: revise")
    sha = _git(r, "rev-parse", "HEAD").strip()
    nodes, more = prompt_mod.citing_nodes(r, sha)
    assert len(nodes) == cap and more == 1, (len(nodes), more)
    commit = prompt_mod.collect_commit(r)
    text = prompt_mod.build_prompt(commit, r)
    assert "(list truncated: 1 more citing node(s) not shown)" in text


def test_lookup_with_no_knowledge_dir_returns_empty(tmp_path, monkeypatch):
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    r.mkdir()
    (r / "docs").mkdir()
    (r / "docs" / "report.md").write_text("v1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "docs" / "report.md").write_text("v2\n")
    _git(r, "commit", "-q", "-am", "feat: revise")
    sha = _git(r, "rev-parse", "HEAD").strip()
    assert prompt_mod.citing_nodes(r, sha) == ([], 0)
    text = prompt_mod.build_prompt(prompt_mod.collect_commit(r), r)
    assert "Nodes that cite files this commit changed" in text
    assert "found no knowledge/ node citing" in text


def test_lookup_of_a_root_commit_finds_citing_nodes(tmp_path, monkeypatch):
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "knowledge" / "concepts" / "report-summary.md").write_text(
        _node("Report summary", "Cites docs/report.md."))
    (r / "docs").mkdir()
    (r / "docs" / "report.md").write_text("v1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "root")
    sha = _git(r, "rev-parse", "HEAD").strip()
    assert prompt_mod.citing_nodes(r, sha) == (
        ["knowledge/concepts/report-summary.md"], 0)


def test_lookup_in_a_nested_project_matches_project_relative_citations(
        tmp_path, monkeypatch):
    _git_env(monkeypatch)
    mono = tmp_path / "mono"
    proj = mono / "apps" / "foo"
    (proj / "knowledge" / "concepts").mkdir(parents=True)
    (proj / "src").mkdir(parents=True)
    (proj / "knowledge" / "concepts" / "cache.md").write_text(
        _node("Cache", "The cache is sized in src/tuning.py."))
    (proj / "src" / "tuning.py").write_text("CACHE = 100\n")
    (mono / "unrelated.txt").write_text("not the project's\n")
    _git(mono, "init", "-q")
    _git(mono, "add", "-A")
    _git(mono, "commit", "-q", "-m", "base")
    (proj / "src" / "tuning.py").write_text("CACHE = 200\n")
    (mono / "unrelated.txt").write_text("changed, but outside the project\n")
    _git(mono, "commit", "-q", "-am", "feat: retune")
    sha = _git(proj, "rev-parse", "HEAD").strip()
    # git prints apps/foo/src/tuning.py; the node cites src/tuning.py
    assert prompt_mod.citing_nodes(proj, sha) == (
        ["knowledge/concepts/cache.md"], 0)


# ── the prompt ────────────────────────────────────────────────────────────


def test_prompt_contains_the_citing_list_and_the_source_rules(repo):
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo)
    assert "## Nodes that cite files this commit changed" in text
    assert "- knowledge/concepts/report-summary.md" in text
    assert "knowledge/concepts/other-summary.md" not in text
    # rule (a): the source path and commit a node reflects are recorded
    assert "Source: docs/reports/cache.md @ " + commit.short in text
    # rule (b): the appended Status-after block, append-and-annotate
    assert f"## Status after {commit.short}" in text
    assert "mark superseded lines IN PLACE per rule 10" in text
    # rule (c): a snapshot marking for documents visibly in progress
    assert "Snapshot of <path> @ " + commit.short in text
    # rule 7 kept and reconciled with the new rules
    assert '"blocker", "in-progress", "TODO", "investigation"' in text
    assert "is not a status-tracking node" in text


def test_prompt_still_demands_research_first_in_order(repo):
    """The new section and rules do not reorder the research-first spine."""
    text = prompt_mod.build_prompt(prompt_mod.collect_commit(repo), repo)
    order = ["hybrid_search", "Read the FULL current text",
             "full files, not only the diff", "prefer the NEWEST source",
             "extending an existing node", "Write only facts",
             '"blocker", "in-progress"', "When unsure about any of this, write nothing"]
    positions = [text.find(s) for s in order]
    assert all(p >= 0 for p in positions), list(zip(order, positions))
    assert positions == sorted(positions), list(zip(order, positions))


# ── the guard: appended Status-after blocks vs rewording ─────────────────


def _judge(repo: Path, rel: str, new_text: str) -> tuple:
    """Snapshot, land the reviewer's write, validate; return (result, lines,
    what the file holds now)."""
    snap = guard.take_snapshot(repo)
    try:
        writes: "dict[Path, list[guard.WriteOp]]" = {
            guard.path_key(repo / rel, repo.resolve()):
                [("write", new_text)]}
        (repo / rel).write_text(new_text)
        lines: list = []
        res = guard.validate(snap, writes, lines.append, sources="")
        return res, lines, (repo / rel).read_text()
    finally:
        snap.cleanup()


def test_guard_accepts_an_appended_status_after_block(repo):
    rel = "knowledge/concepts/report-summary.md"
    new = (REPORT_NODE.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z")
           + "\nSource: docs/report.md @ abc1234\n"
             "\n## Status after abc1234\n\n"
             "Proposal X was removed from the report (docs/report.md at\n"
             "abc1234); the line above no longer reflects it.\n")
    res, _lines, now = _judge(repo, rel, new)
    assert now == new and res.kept == [rel] and res.reverted == []


def test_guard_still_reverts_a_rewording(repo):
    """The new rules loosen nothing: dropping the superseded wording is
    reverted exactly as before."""
    rel = "knowledge/concepts/report-summary.md"
    new = (REPORT_NODE.replace("The report proposes X",
                               "The report proposes nothing")
           + "\n## Status after abc1234\n\nProposal X was removed.\n")
    res, _lines, now = _judge(repo, rel, new)
    assert now == REPORT_NODE
    assert "removed or reworded 1 original line(s)" in res.reverted[0][1]


def test_guard_still_reverts_a_status_after_block_on_a_locked_node(repo):
    """A locked node takes only its dated Proposed-change section; the guard
    is unchanged, so the prompt routes locked nodes there (rule 8)."""
    rel = "knowledge/concepts/locked-summary.md"
    (repo / rel).write_text(LOCKED_NODE)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "add locked node")
    new = LOCKED_NODE + "\n## Status after abc1234\n\nProposal X removed.\n"
    res, _lines, now = _judge(repo, rel, new)
    assert now == LOCKED_NODE
    assert "locked node" in res.reverted[0][1]
