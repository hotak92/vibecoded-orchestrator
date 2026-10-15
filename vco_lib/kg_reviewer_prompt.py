# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The post-commit KG reviewer's commit capture + prompt (v0.2.103).

ONE home for what both hook siblings used to build in shell: the commit's
stat, its (capped) diff and the reviewer instructions. The hooks are thin
wrappers; ``templates/scripts/kg_reviewer_backend.py run`` calls
:func:`collect_commit` and :func:`build_prompt`.

What the prompt demands (owner ruling 2026-10-15, after a reviewer rewrote a
"do not change without discussion" decision node, deleted its frontmatter,
edited an ADR and minted duplicate/stale status nodes from a partial diff):

* research FIRST — search the KG for each topic, read every node in full
  before editing it, read the full files the diff touches, and prefer the
  NEWEST source when sources disagree (``git log`` dates);
* extend an existing node rather than create a near-duplicate;
* write only facts the diff/sources state — no inferred provenance,
  motivation or status; never "blocker"/"in-progress"/investigation nodes;
* KG only — ``docs/`` is never edited;
* skip trivial/cosmetic/test-only commits; when unsure, write nothing.

Source tracking (v0.2.103, after a field defect: a node summarised a report
that was still being revised; later commits superseded parts of the report
but the node was never updated — the reviewer found related nodes only via
hybrid_search, and nothing recorded which revision a node reflected):

* :func:`citing_nodes` — a DETERMINISTIC lookup handed to the prompt as
  "Nodes that cite files this commit changed": a plain-text match of the
  commit's changed project-relative paths inside ``knowledge/**/*.md``
  (Windows-style ``\\`` separators matched too), capped (``[prompt]
  citing_nodes_max``), with the prompt saying when the list was truncated;
* prompt rules — a node created or extended from a document records its
  source path and commit ("``Source: <path> @ <short-sha>``"); when a
  citing node's source changed in this commit, a "``## Status after
  <short-sha>``" block is APPENDED (append-and-annotate — superseded lines
  kept and marked in place, never deleted or reworded; locked nodes take
  their Proposed-change section instead); a summary of a document visibly
  in progress is marked as a snapshot of that commit. Reconciled with the
  no-status-tracking-nodes rule: a Status-after block annotates an EXISTING
  node with one concrete change — it is not a status-tracking node.

The diff is capped (``[prompt]`` in ``kg_reviewer_config.toml``); the prompt
says when it was truncated. A root commit is shown against the empty tree;
any other commit against its FIRST parent. Capture reads the git pipe
incrementally and stops at the cap, so a huge commit never buffers whole.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from vco_lib.kg_reviewer_model import config

__all__ = ["CommitInfo", "collect_commit", "changed_paths", "citing_nodes",
           "build_prompt"]


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    short: str
    subject: str
    stat: str
    diff: str
    diff_truncated: bool
    diff_lines: int


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _capped(root: Path, args: List[str], max_lines: int,
            max_chars: int) -> Tuple[str, bool, int]:
    """``(text, truncated, lines_kept)`` of ``git <args>``, read line by line
    and cut at ``max_lines`` / ``max_chars`` (the process is then killed —
    a closed pipe is the intended end, not an error)."""
    proc = subprocess.Popen(["git", *args], cwd=root, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    kept: List[str] = []
    chars = 0
    truncated = False
    try:
        for line in proc.stdout:
            if len(kept) >= max_lines or chars + len(line) > max_chars:
                truncated = True
                break
            kept.append(line)
            chars += len(line)
    finally:
        if truncated:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    return "".join(kept).rstrip("\n"), truncated, len(kept)


def collect_commit(root: Path, rev: str = "HEAD") -> CommitInfo:
    """The commit's identity, stat and capped diff."""
    caps = config()["prompt"]
    sha = _git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    short = _git(root, "rev-parse", "--short", sha).strip()
    subject = _git(root, "log", "-1", "--format=%s", sha).strip()
    has_parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{sha}~1"], cwd=root,
        capture_output=True).returncode == 0
    if has_parent:
        base = ["diff", f"{sha}~1", sha, "--no-color"]
    else:  # root commit: shown against the empty tree
        base = ["show", "--format=", "--no-color", sha]
    stat, _t, _n = _capped(root, base + ["--stat"], int(caps["stat_max_lines"]),
                           int(caps["diff_max_chars"]))
    diff, truncated, kept = _capped(root, base, int(caps["diff_max_lines"]),
                                    int(caps["diff_max_chars"]))
    return CommitInfo(sha=sha, short=short, subject=subject, stat=stat,
                      diff=diff, diff_truncated=truncated, diff_lines=kept)


def changed_paths(root: Path, sha: str) -> "List[str]":
    """The paths ``sha`` changed, PROJECT-relative.

    First-parent diff, like the diff the prompt embeds; a root commit
    against the empty tree. Git prints paths relative to the REPO root, so a
    project nested in a parent repo (v0.2.103 final review M-1) gets the
    prefix stripped — nodes cite project-relative paths — and a changed path
    elsewhere in the parent repo is dropped (not the project's, the same
    rule the guard's ``project_rel`` applies to git path output)."""
    has_parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{sha}~1"], cwd=root,
        capture_output=True).returncode == 0
    if has_parent:
        args = ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                f"{sha}~1", sha]
    else:  # root commit: against the empty tree
        args = ["diff-tree", "--root", "--no-commit-id", "--name-only",
                "-r", "-z", sha]
    out = _git(root, *args)
    try:
        prefix = _git(root, "rev-parse", "--show-prefix").strip()
    except (RuntimeError, OSError):
        prefix = ""
    paths: List[str] = []
    for top_rel in out.split("\0"):
        if not top_rel:
            continue
        if not prefix:
            paths.append(top_rel)
        elif top_rel.startswith(prefix):
            paths.append(top_rel[len(prefix):])
    return paths


def citing_nodes(project_root: Path, sha: str) -> "Tuple[List[str], int]":
    """``(nodes, more)`` — the ``knowledge/**/*.md`` nodes whose text CITES
    a path ``sha`` changed, and how many citing nodes the cap cut.

    A plain-text match — the changed project-relative path, and the same
    path with ``\\\\`` separators (node text written on Windows cites it
    that way) — so it is deterministic and cheap: no embedding, no KG
    query, no similarity ordering. That determinism is the point (source
    tracking, v0.2.103): hybrid_search alone missed the node summarising a
    report whose later revisions superseded it. The list is capped
    (``[prompt] citing_nodes_max``) and sorted, so the prompt is stable for
    one commit; ``more`` is 0 unless the cap cut citing nodes."""
    cap = int(config()["prompt"]["citing_nodes_max"])
    needles: List[str] = []
    for rel in changed_paths(project_root, sha):
        needles.append(rel)
        if "/" in rel:
            needles.append(rel.replace("/", "\\"))
    if not needles:
        return [], 0
    kdir = project_root / "knowledge"
    if not kdir.is_dir():
        return [], 0
    hits: List[str] = []
    for path in sorted(kdir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.append(path.relative_to(project_root).as_posix())
    more = max(0, len(hits) - cap)
    return hits[:cap], more


def build_prompt(commit: CommitInfo, project_root: Path,
                 today: Optional[str] = None) -> str:
    """The reviewer prompt for ``commit``. ``today`` (``YYYY-MM-DD``) is the
    date the locked-node "Proposed change" heading must carry."""
    import datetime as _dt

    guard = config()["guard"]
    today = today or _dt.date.today().isoformat()
    heading = f"{guard['proposed_change_heading']}{today})"
    knowledge = project_root / "knowledge"
    if commit.diff_truncated:
        diff_note = (f"The diff below is TRUNCATED after {commit.diff_lines} "
                     f"lines. Do not guess what the rest contains: Read the "
                     f"changed files themselves.")
    else:
        diff_note = ("The diff below is complete, but it shows only the "
                     "changed hunks: Read the full files for context.")
    try:
        prefix = _git(project_root, "rev-parse", "--show-prefix").strip()
    except (RuntimeError, OSError):
        prefix = ""
    if prefix:
        # A project nested in a parent repo (v0.2.103 final review M-1):
        # git prints paths from the REPO root, the reviewer works from here.
        diff_note += (f" This project is the folder `{prefix}` of a larger "
                      f"git repository: the paths in the diff and the file "
                      f"list are relative to that repository's root, so a "
                      f"path `{prefix}x` is the file `x` here.")
    locked_tags = ", ".join(guard["locked_tags"])
    locked_statuses = ", ".join(guard["locked_statuses"])
    citing, citing_more = citing_nodes(project_root, commit.sha)
    if citing:
        cite_lines = "\n".join(f"- {rel}" for rel in citing)
        cite_trunc = (f"\n(list truncated: {citing_more} more citing node(s) "
                      f"not shown)" if citing_more else "")
        citing_block = (
            "A deterministic text scan (not hybrid_search) found these "
            "knowledge/ nodes citing a path this commit changed - what they "
            "say about that path may be stale after this commit:\n"
            f"{cite_lines}{cite_trunc}")
    else:
        citing_block = (
            "The deterministic text scan found no knowledge/ node citing a "
            "path this commit changed (hybrid_search in step 2 is still the "
            "way to find nodes by TOPIC).")
    return f"""You are the post-commit knowledge-graph (KG) reviewer for this project.
Your ONLY job: keep the KG nodes under {knowledge}/ accurate for what this commit
changed. You never edit anything outside {knowledge}/ - not docs/, not code,
not configuration. Edits elsewhere are blocked and reverted.

## Commit: {commit.short} - {commit.subject}

### Files changed:
{commit.stat}

### Diff
{diff_note}

{commit.diff}

## Nodes that cite files this commit changed
{citing_block}

## Procedure - research FIRST, then write
1. Decide whether this commit needs any KG change at all. Skip trivial,
   cosmetic, formatting, typo, dependency-bump and test-only commits: finish
   with "no KG change" and write nothing.
2. For each topic the commit touches, run hybrid_search to find the existing
   nodes on it.
3. Read the FULL current text of every node you might change BEFORE editing
   it (Read the file under {knowledge}/). Never edit a node you have not read.
4. Read the files the diff references - the full files, not only the diff
   hunks - and any doc or node they cite. When two sources disagree, check
   their dates (git log) and prefer the NEWEST source; never re-assert what a
   newer source contradicts.
5. Prefer extending an existing node over creating a new one. Create a new
   node only for a genuinely new topic with no existing node (one topic per
   node; no near-duplicates; a title an existing node already uses is
   reverted). A new node must open with YAML frontmatter carrying title,
   type, tags, created, updated and status (ISO dates) - one without it is
   reverted.
6. Write only facts the diff or the sources you read state explicitly. Do not
   infer provenance, motivation, history, ownership or status. Never write a
   CLI flag, option name, default value, number or sample output/log that you
   did not read VERBATIM in the diff or in a file you opened (Read/Grep)
   during this run, and name the source path next to each such fact. A
   backticked `--flag` that appears in nothing you read is reverted.
   hybrid_search results do NOT count as a source for this (KG text can be
   wrong): Read the file a flag comes from before writing it.
7. Record where a node's facts came from. A node you create or extend from a
   document or other source file records the source path and the commit it
   reflects, as a line like "Source: docs/reports/cache.md @ {commit.short}".
   When you later extend the node from a newer revision, APPEND the new
   source line and keep the earlier ones (rule 10); never reword or drop an
   existing source line.
8. A node listed under "Nodes that cite files this commit changed" may state
   something this commit superseded. Read the changed file it cites and,
   when the commit changed what the node states, APPEND one section at the
   end of the node:
   ## Status after {commit.short}
   stating what changed and which of the node's statements it supersedes;
   mark superseded lines IN PLACE per rule 10 - never delete or reword
   them. A LOCKED node (rule 11) takes only the Proposed change section of
   rule 11 instead. When the cited document is visibly still in progress
   (a draft, an open review round), mark the node's summary as a snapshot
   of that commit: "Snapshot of <path> @ {commit.short}".
9. Never create or update "blocker", "in-progress", "TODO", "investigation"
   or other status-tracking NODES from a commit diff - a single commit is a
   partial view. (A "Status after" block appended to an EXISTING node by
   rule 8 is not a status-tracking node: it records one concrete change,
   with its source, inside the node that already owns the topic.)
10. Keep each existing node's YAML frontmatter exactly as it is; you may only
    add or update its `updated:` field. Never delete a node.
    Existing node text is APPEND-AND-ANNOTATE ONLY: the stored information
    must survive. Add new facts after the existing text. When something is
    superseded, keep the original line and mark it IN PLACE, e.g.
    "Original 2026-10-15: <original text> (superseded by <new fact>, see
    below)" or "~~<original text>~~" - never delete, shorten or reword an
    existing line. A write that removes or rewords any existing line is
    reverted.
11. LOCKED nodes (tags: {locked_tags}; status: {locked_statuses}; or text that
    says not to change it without discussion) are never rewritten. If such a
    node is now wrong, append ONE section at the end of the file:
    {heading}
    stating the fact and the source that shows it - and change nothing else.
12. Write nodes ONLY with the Write and Edit tools, under {knowledge}/ (the
    project syncs them into the KG itself). Nothing else may write: no
    shared-KG writes, no other tool, no symlinked paths.
13. When unsure about any of this, write nothing. An unchanged KG is always
    better than a wrong one.

Finish with a short list of the nodes you changed or created (or "no KG
change") and, for each, the source that justified it."""
