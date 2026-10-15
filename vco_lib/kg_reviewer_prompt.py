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

__all__ = ["CommitInfo", "collect_commit", "build_prompt"]


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
    locked_tags = ", ".join(guard["locked_tags"])
    locked_statuses = ", ".join(guard["locked_statuses"])
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
   node; no near-duplicates).
6. Write only facts the diff or the sources you read state explicitly. Do not
   infer provenance, motivation, history, ownership or status.
7. Never create or update "blocker", "in-progress", "TODO", "investigation"
   or other status-tracking nodes from a commit diff - a single commit is a
   partial view.
8. Keep each existing node's YAML frontmatter exactly as it is; you may only
   add or update its `updated:` field. Never delete a node.
9. LOCKED nodes (tags: {locked_tags}; status: {locked_statuses}; or text that
   says not to change it without discussion) are never rewritten. If such a
   node is now wrong, append ONE section at the end of the file:
   {heading}
   stating the fact and the source that shows it - and change nothing else.
10. Write nodes with the Write/Edit tools under {knowledge}/ (or the
    store_knowledge_node tool with a file_path under knowledge/ and
    scope="project"). Never use scope="shared".
11. When unsure about any of this, write nothing. An unchanged KG is always
    better than a wrong one.

Finish with a short list of the nodes you changed or created (or "no KG
change") and, for each, the source that justified it."""
