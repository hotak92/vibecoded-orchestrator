# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the KG reviewer keeps only the LATEST status (guard + prompt).

Owner request 2026-11-04: the post-commit reviewer appends one
``## Status after <sha>`` section per commit (prompt rule 8), so a decision
refined several times in a row collects a CHAIN of sections, each
superseding the last. A later pass may fold the reviewer's OWN sections
into one ``## Current state (consolidated <date>)`` section — but the
OWNER's lines must never become removable, and a still-true fact must
never be lost.

What this file proves (real temp git repos, the ``claude`` CLI never run):

* **provenance is a ledger, not an inference** (review V02105-KGREV2
  H1/H2): a section is foldable only while its bytes hash-match what the
  reviewer's own commit RECORDED (``vco_lib.kg_reviewer_provenance``) and
  that commit is still in history. Repros A (amend), B (annotation), C
  (CRLF->LF), F (a user commit titled ``kg-sync:``) and an owner line
  enclosed under a new reviewer heading all stay append-only;
* **carry-over per fact** (H3, repro D): every statement of a folded
  section must survive (carried over or quoted on its trail line);
* **the consolidated section folds again** (M1, repro E);
* **the prompt and the guard use one list** (M4) and the prompt lists
  exactly the proven sections; coalesced commits get their diff and the
  citing scan (M5), bounded (L2).

The lock / queue half lives in ``test_v02105_kg_reviewer_queue.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402
from vco_lib import kg_reviewer_provenance as prov  # noqa: E402

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

SECTION_1 = """

## Status after 1111111

The blue pipeline was replaced by the green pipeline.
The deploy timeout is 300 s.
"""

SECTION_2 = """

## Status after 2222222

The deploy timeout is 600 s.
"""

#: The fold: every still-true fact carried over VERBATIM (prefixed), the
#: superseded 300 s fact QUOTED on its trail line, one step line per
#: folded section.
FOLDED = """

## Current state (consolidated 2026-11-04)

- Built: The blue pipeline was replaced by the green pipeline.
- Decided: The deploy timeout is 600 s.

Superseded: 1111111 -> 3333333 (2026-11-04)
Superseded: 1111111 -> 3333333 (2026-11-04): The deploy timeout is 300 s.
Superseded: 2222222 -> 3333333 (2026-11-04)
"""

REL = "knowledge/concepts/deploy.md"


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


def _git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)


def _init(r: Path, node: bytes) -> None:
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs").mkdir(parents=True)
    (r / REL).write_bytes(node)
    (r / "docs" / "deploy.md").write_text("v1\n")
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    # what every VCO project has: .claude/state/ is git-excluded
    (r / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (r / ".git" / "info" / "exclude").write_text(".claude/state/\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "docs: the deploy node")


def _reviewer_commit(repo: Path, text: str, short: str,
                     subject: str = "feat: change") -> str:
    """The reviewer's REAL write path: snapshot, the write, the guard, the
    isolated kg-sync commit (which records provenance)."""
    lines: List[str] = []
    snap = guard.take_snapshot(repo)
    try:
        key = guard.path_key(repo / REL, repo.resolve())
        (repo / REL).write_bytes(text.encode("utf-8"))
        res = guard.validate(snap, {key: [("write", text)]}, lines.append,
                             sources="")
        assert res.kept == [REL], (res.reverted, lines)
        sha = guard.commit_reviewer_changes(
            snap, res.kept, reviewer_bytes=res.reviewer_bytes, short=short,
            subject=subject, model="test", attributed=True, log=lines.append)
        assert sha, lines
        return sha
    finally:
        snap.cleanup()


def _user_commit(repo: Path, msg: str = "feat: revise deploy doc") -> str:
    doc = repo / "docs" / "deploy.md"
    doc.write_text(doc.read_text() + "more\n")
    _git(repo, "commit", "-q", "-am", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose node carries TWO reviewer Status sections, each
    written and committed through the reviewer's real path, then one user
    commit (the one the next review runs for)."""
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    _init(r, OWNER_NODE.encode())
    _reviewer_commit(r, OWNER_NODE + SECTION_1, "1111111")
    _reviewer_commit(r, OWNER_NODE + SECTION_1 + SECTION_2, "2222222")
    _user_commit(r)
    return r


def _judge(repo: Path, new_text: str, foldable=None
           ) -> Tuple[guard.ValidationResult, List[str], str]:
    """Snapshot, land the reviewer's write, validate; (result, log, file)."""
    snap = guard.take_snapshot(repo)
    try:
        writes = {guard.path_key(repo / REL, repo.resolve()):
                  [("write", new_text)]}
        (repo / REL).write_text(new_text)
        lines: List[str] = []
        res = guard.validate(snap, writes, lines.append, sources="",
                             foldable=foldable)
        return res, lines, (repo / REL).read_text()
    finally:
        snap.cleanup()


def _headings(repo: Path) -> List[str]:
    return [e["heading"] for e in prov.entries_for(repo, REL)]


# ═════════════════════════════════════════════════════════════════════════
# 1. The ledger: what the reviewer's own commits record
# ═════════════════════════════════════════════════════════════════════════


def test_the_reviewer_commit_records_its_sections(repo):
    entries = prov.entries_for(repo, REL)
    assert [e["heading"] for e in entries] == [
        "## Status after 1111111", "## Status after 2222222"]
    # the untouched first section keeps its ORIGINAL commit
    first, second = (e["commit"] for e in entries)
    assert first != second
    assert _git(repo, "log", "-1", "--format=%s", first).startswith(
        "kg-sync: 1111111")
    assert [s.heading for s in guard.proven_sections(
        repo, REL, (repo / REL).read_bytes(), "HEAD")] == [
        "## Status after 1111111", "## Status after 2222222"]


def test_an_owner_line_enclosed_under_a_new_reviewer_heading_is_not_recorded(
        tmp_path, monkeypatch):
    """The reviewer inserts its heading ABOVE an owner line (allowed: the
    line survives in order) — that owner line now sits in the new section,
    so the section is NOT the reviewer's and is never recorded."""
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    _init(r, OWNER_NODE.encode())
    enclosing = OWNER_NODE.replace(
        "The owner's own note survives every review.\n",
        "## Status after 3333333\n\nA reviewer fact.\n"
        "The owner's own note survives every review.\n")
    _reviewer_commit(r, enclosing, "3333333")
    assert _headings(r) == []


# ═════════════════════════════════════════════════════════════════════════
# 2. The fold: per-fact carry-over (H3, repro D)
# ═════════════════════════════════════════════════════════════════════════


def test_a_fold_carrying_every_fact_is_kept(repo):
    res, lines, now = _judge(repo, OWNER_NODE + FOLDED)
    assert now == OWNER_NODE + FOLDED
    assert res.kept == [REL], (res.reverted, lines)


def test_repro_d_a_still_true_fact_dropped_under_a_step_line_is_reverted(repo):
    """Repro D: the whole section 1111111 dropped, only a trail STEP line
    added. The step line names the section, but its green-pipeline fact is
    still true and was neither carried over nor named superseded."""
    folded = (OWNER_NODE + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- Decided: The deploy timeout is 600 s.\n\n"
              "Superseded: 1111111 -> 3333333 (2026-11-04)\n"
              "Superseded: 2222222 -> 3333333 (2026-11-04)\n")
    res, lines, now = _judge(repo, folded)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert res.kept == []
    reason = res.reverted[0][1]
    assert "removed or reworded 2 original line(s)" in reason, reason
    assert "green pipeline" in reason and "300 s" in reason


def test_a_superseded_fact_is_dropped_only_by_quoting_it_on_the_trail(repo):
    """The 300 s fact may leave the prose only by being QUOTED on a trail
    line; dropping it silently (step line alone) reverts."""
    silent = (OWNER_NODE + FOLDED).replace(
        "Superseded: 1111111 -> 3333333 (2026-11-04): The deploy timeout "
        "is 300 s.\n", "")
    res, _lines, _now = _judge(repo, silent)
    assert res.kept == [] and "300 s" in res.reverted[0][1]


def test_a_folded_heading_needs_its_step_line(repo):
    no_step = (OWNER_NODE + FOLDED).replace(
        "Superseded: 2222222 -> 3333333 (2026-11-04)\n", "")
    res, _lines, _now = _judge(repo, no_step)
    assert res.kept == []
    assert "'## Status after 2222222'" in res.reverted[0][1]


def test_an_owner_line_anywhere_removed_is_reverted(repo):
    folded = (OWNER_NODE.replace(
        "The owner's own note survives every review.\n", "") + FOLDED)
    res, lines, now = _judge(repo, folded)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2
    assert "removed or reworded 1 original line(s)" in res.reverted[0][1]
    assert any("owner's own note" in ln for ln in lines)


def test_pure_append_still_needs_nothing(repo):
    new = (OWNER_NODE + SECTION_1 + SECTION_2
           + "\n## Status after 3333333\n\nThe doc was revised.\n")
    res, _lines, now = _judge(repo, new)
    assert now == new and res.kept == [REL]


def test_uncommitted_reviewer_lines_are_not_foldable(repo):
    """A section the reviewer wrote but never committed has no ledger
    entry: folding it (even under a step line) reverts."""
    extra = "\n\n## Status after 4444444\n\nAn uncommitted reading.\n"
    (repo / REL).write_text(OWNER_NODE + SECTION_1 + SECTION_2 + extra)
    folded = (OWNER_NODE + FOLDED
              + "Superseded: 4444444 -> 3333333 (2026-11-04)\n")
    res, lines, _now = _judge(repo, folded)
    assert res.kept == [] and any("uncommitted reading" in ln for ln in lines)


def test_no_head_keeps_the_append_only_rule(repo):
    _git(repo, "update-ref", "-d", "HEAD")
    res, _lines, now = _judge(repo, OWNER_NODE + FOLDED)
    assert now == OWNER_NODE + SECTION_1 + SECTION_2 and res.kept == []


def test_a_malformed_ledger_is_logged_and_folds_nothing(repo):
    prov.ledger_path(repo).write_text("{not json", encoding="utf-8")
    res, lines, _now = _judge(repo, OWNER_NODE + FOLDED)
    assert res.kept == []
    assert any("provenance ledger" in ln and "malformed" in ln
               for ln in lines), lines


# ═════════════════════════════════════════════════════════════════════════
# 3. Laundering repros A, B, C, F (H1/H2): all stay append-only
# ═════════════════════════════════════════════════════════════════════════


def test_repro_a_amend_adding_an_owner_line_to_the_section(repo):
    """The user amends the reviewer's latest kg-sync commit (``--amend -a
    --no-edit``) with an owner line inside section 2222222: that section is
    no longer what the ledger recorded, and the commit is rewritten."""
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # back onto the kg-sync tip
    (repo / REL).write_text(OWNER_NODE + SECTION_1 + SECTION_2
                            + "OWNER: never delete me, still true.\n")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    _user_commit(repo)
    res, lines, now = _judge(repo, OWNER_NODE + FOLDED)
    assert res.kept == [], lines
    assert "OWNER: never delete me" in now


def test_repro_a_amend_elsewhere_still_unproves_the_rewritten_commit(repo):
    """An amend that leaves section 2222222's bytes alone still rewrites the
    commit that recorded it: 2222222 is no longer provable (append-only),
    while 1111111 (recorded by an untouched, ancestral commit) still is."""
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    (repo / "src.py").write_text("x = 2\n")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    _user_commit(repo)
    proven = guard.proven_sections(repo, REL, (repo / REL).read_bytes(),
                                   "HEAD")
    assert [s.heading for s in proven] == ["## Status after 1111111"]
    res, _lines, _now = _judge(repo, OWNER_NODE + FOLDED)
    assert res.kept == []
    only_1 = (OWNER_NODE
              + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- Built: The blue pipeline was replaced by the green pipeline.\n"
              "\nSuperseded: 1111111 -> 3333333 (2026-11-04)\n"
              "Superseded: 1111111 -> 3333333 (2026-11-04): The deploy "
              "timeout is 300 s.\n" + SECTION_2)
    res, lines, _now = _judge(repo, only_1)
    assert res.kept == [REL], (res.reverted, lines)


def test_repro_f_a_user_commit_titled_kg_sync_proves_nothing(repo):
    owner_status = "\n\n## Status after 5555555\n\nThe owner wrote this.\n"
    (repo / REL).write_text(OWNER_NODE + SECTION_1 + SECTION_2 + owner_status)
    _git(repo, "commit", "-q", "-am", "kg-sync: refresh nodes by hand")
    _user_commit(repo)
    assert "## Status after 5555555" not in _headings(repo)
    folded = (OWNER_NODE + FOLDED
              + "Superseded: 5555555 -> 3333333 (2026-11-04)\n")
    res, _lines, now = _judge(repo, folded)
    assert res.kept == [] and "The owner wrote this." in now


def test_repro_b_an_annotated_owner_line_is_never_laundered(repo):
    """The owner appends a fact after the last reviewer section (it joins
    that section: mixed, not foldable). A reviewer pass then annotates it
    in place (legal) and appends its own section, committed by the real
    path: the annotated section must NOT be recorded, so a later fold that
    drops the owner's fact still reverts. The new reviewer section IS
    recorded (and foldable) — the positive side."""
    owned = OWNER_NODE + SECTION_1 + SECTION_2 + "OWNER fact appended later.\n"
    (repo / REL).write_text(owned)
    _git(repo, "commit", "-q", "-am", "docs: owner fact")
    res, _lines, _now = _judge(repo, OWNER_NODE + FOLDED)
    assert res.kept == [], "pre-annotation: the mixed section is not foldable"
    annotated = owned.replace("OWNER fact appended later.\n",
                              "OWNER fact appended later. (still current)\n")
    annotated += "\n## Status after 3333333\n\nThe doc was revised.\n"
    _reviewer_commit(repo, annotated, "3333333")
    assert _headings(repo) == ["## Status after 1111111",
                               "## Status after 3333333"]
    _user_commit(repo)
    folded = (OWNER_NODE + FOLDED
              + "\n## Status after 3333333\n\nThe doc was revised.\n")
    res, _lines, now = _judge(repo, folded)
    assert res.kept == [] and "OWNER fact appended later." in now
    fold_3 = annotated.replace(
        "\n## Status after 3333333\n\nThe doc was revised.\n",
        "\n## Current state (consolidated 2026-11-05)\n\n"
        "- The doc was revised.\n\n"
        "Superseded: 3333333 -> 4444444 (2026-11-05)\n")
    res, lines, _now = _judge(repo, fold_3)
    assert res.kept == [REL], (res.reverted, lines)


def test_repro_c_a_crlf_to_lf_rewrite_proves_nothing(tmp_path, monkeypatch):
    """An owner-written Status section in a CRLF node; the reviewer's write
    converts the file to LF (the guard tolerates line endings) and appends
    its own section. Only the reviewer's own section is recorded."""
    _git_env(monkeypatch)
    r = tmp_path / "proj"
    owner_status = ("\n\n## Status after 1111111\n\n"
                    "The owner wrote this status by hand.\n")
    _init(r, (OWNER_NODE + owner_status).replace("\n", "\r\n").encode())
    mine = "\n\n## Status after 2222222\n\nThe reviewer's own fact.\n"
    _reviewer_commit(r, OWNER_NODE + owner_status + mine, "2222222")
    assert _headings(r) == ["## Status after 2222222"]
    _user_commit(r)
    folded = (OWNER_NODE + "\n## Current state (consolidated 2026-11-04)\n\n"
              "- The reviewer's own fact.\n\n"
              "Superseded: 1111111 -> 3333333 (2026-11-04)\n"
              "Superseded: 2222222 -> 3333333 (2026-11-04)\n")
    res, _lines, now = _judge(r, folded)
    assert res.kept == [] and "by hand" in now
    keep_owner = (OWNER_NODE + owner_status
                  + "\n## Current state (consolidated 2026-11-04)\n\n"
                  "- The reviewer's own fact.\n\n"
                  "Superseded: 2222222 -> 3333333 (2026-11-04)\n")
    res, lines, _now = _judge(r, keep_owner)
    assert res.kept == [REL], (res.reverted, lines)


def test_line_endings_are_normalized_for_hashing_only(repo):
    """The same recorded section read back with CRLF endings still matches
    (hashing normalizes CR), and nothing rewrote the file's own endings."""
    crlf = (repo / REL).read_bytes().replace(b"\n", b"\r\n")
    proven = guard.proven_sections(repo, REL, crlf, "HEAD")
    assert len(proven) == 2
    assert b"\r\n" not in (repo / REL).read_bytes()


# ═════════════════════════════════════════════════════════════════════════
# 4. The consolidated section folds again (M1, repro E)
# ═════════════════════════════════════════════════════════════════════════


def test_repro_e_two_fold_cycles_leave_one_current_state(repo):
    _reviewer_commit(repo, OWNER_NODE + FOLDED, "3333333")
    assert _headings(repo) == ["## Current state (consolidated 2026-11-04)"]
    _user_commit(repo)
    proven = guard.proven_sections(repo, REL, (repo / REL).read_bytes(),
                                   "HEAD")
    assert [(s.kind, s.key) for s in proven] == [("current", "3333333")]
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-05")
    assert ("`## Current state (consolidated 2026-11-04)` (key `3333333`"
            in text)
    assert "UPDATE that" in text
    second = OWNER_NODE + """

## Current state (consolidated 2026-11-05)

- Built: The blue pipeline was replaced by the green pipeline.
- Decided: The deploy timeout is 900 s.

Superseded: 1111111 -> 3333333 (2026-11-04)
Superseded: 1111111 -> 3333333 (2026-11-04): The deploy timeout is 300 s.
Superseded: 2222222 -> 3333333 (2026-11-04)
Superseded: 3333333 -> 4444444 (2026-11-05)
Superseded: 3333333 -> 4444444 (2026-11-05): Decided: The deploy timeout is 600 s.
"""
    res, lines, _now = _judge(repo, second)
    assert res.kept == [REL], (res.reverted, lines)
    (repo / REL).write_text(OWNER_NODE + FOLDED)  # undo the probe's write
    _reviewer_commit(repo, second, "4444444")
    assert (repo / REL).read_text().count("## Current state") == 1
    assert _headings(repo) == ["## Current state (consolidated 2026-11-05)"]
    # a second fold that drops an earlier trail line loses history: reverted
    _user_commit(repo)
    lossy = second.replace(
        "Superseded: 2222222 -> 3333333 (2026-11-04)\n", "")
    res, _lines, _now = _judge(repo, lossy)
    assert res.kept == []


# ═════════════════════════════════════════════════════════════════════════
# 5. The prompt: one list for the prompt and the guard (M4), coalesced
#    commits (M5, L2)
# ═════════════════════════════════════════════════════════════════════════


def test_prompt_lists_exactly_the_proven_sections(repo):
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04")
    assert "## Reviewer Status sections this pass may fold" in text
    assert "`## Status after 1111111` (key `1111111`" in text
    assert "`## Status after 2222222` (key `2222222`" in text
    assert "## Current state (consolidated 2026-11-04)" in text
    assert "Superseded: <key> → " + commit.short in text
    assert ": <the dropped statement>" in text
    assert f"## Status after {commit.short}" in text


def test_prompt_omits_a_section_the_owner_edited(repo):
    mixed = (OWNER_NODE + SECTION_1.replace(
        "The deploy timeout is 300 s.",
        "The deploy timeout is 300 s.\nAn owner line inside the section.")
        + SECTION_2)
    (repo / REL).write_text(mixed)
    _git(repo, "commit", "-q", "-am", "docs: owner note inside the section")
    _user_commit(repo, "feat: revise again")
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04")
    assert "`## Status after 1111111`" not in text
    assert "`## Status after 2222222`" in text


def test_the_guard_never_allows_more_than_the_prompt_listed(repo):
    """M4: ``validate(foldable=...)`` judges only against sections the
    prompt listed AND that are still proven."""
    proven = guard.proven_sections(repo, REL, (repo / REL).read_bytes(),
                                   "HEAD")
    res, _lines, _now = _judge(repo, OWNER_NODE + FOLDED, foldable={})
    assert res.kept == [], "proven but not listed -> append-only"
    res, lines, _now = _judge(repo, OWNER_NODE + FOLDED,
                              foldable={REL: proven})
    assert res.kept == [REL], (res.reverted, lines)


def test_coalesced_commits_get_their_diff_and_the_citing_scan(repo):
    """M5: a node citing a file only an OLDER coalesced commit changed is
    surfaced, and that commit's diff is in the prompt."""
    other = repo / "knowledge" / "concepts" / "src.md"
    other.write_text("---\ntitle: Src\ntype: concept\ntags: [x]\n"
                     "status: active\n---\n\nsrc.py holds x.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: src node")
    (repo / "src.py").write_text("x = 42\n")
    _git(repo, "commit", "-q", "-am", "feat: x is 42")
    old = prompt_mod.collect_commit(repo)
    new_sha = _user_commit(repo, "feat: doc again")
    new = prompt_mod.collect_commit(repo, new_sha)
    scan = prompt_mod.citing_nodes(repo, new.sha, also=(old.sha,))
    assert "src.py" in [e.path for e in scan.entries]
    text = prompt_mod.build_prompt(new, repo, today="2026-11-04",
                                   also=(old,))
    assert "### Also covered by this pass" in text
    assert "+x = 42" in text
    assert "`knowledge/concepts/src.md`" in text


def test_the_coalesced_list_is_bounded(repo, monkeypatch):
    """L2: at most ``coalesced_max`` commits are listed in full; the rest
    are named by short sha only."""
    monkeypatch.setattr(prompt_mod, "coalesced_max", lambda: 1)
    commit = prompt_mod.collect_commit(repo)
    a = prompt_mod.collect_commit(repo, "HEAD~1")
    b = prompt_mod.collect_commit(repo, "HEAD~2")
    text = prompt_mod.build_prompt(commit, repo, today="2026-11-04",
                                   also=(a, b))
    assert f"#### `{a.short}`" in text
    assert f"#### `{b.short}`" not in text
    assert f"(+1 further coalesced commit(s) not shown: `{b.short}`" in text


def test_the_ledger_is_bounded_and_logs_drops(repo, monkeypatch):
    monkeypatch.setattr(prov, "MAX_ENTRIES", 1)
    lines: List[str] = []
    prov.record(repo, "f" * 40, {"knowledge/x.md": [
        {"digest": "d" * 64, "heading": "## Status after x", "key": "x",
         "kind": "status"}]}, lines.append, now="2099-01-01T00:00:00Z")
    data = json.loads(prov.ledger_path(repo).read_text())
    assert sum(len(v) for v in data["paths"].values()) == 1
    assert any("dropping the oldest" in ln for ln in lines)


def test_an_unwritable_ledger_is_logged(repo, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr("vco_lib.atomic.atomic_write_json", boom)
    lines: List[str] = []
    assert not prov.record(repo, "f" * 40, {REL: []}, lines.append)
    assert any("provenance NOT recorded" in ln for ln in lines)
