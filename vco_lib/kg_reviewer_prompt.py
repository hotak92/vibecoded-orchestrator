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
* extend an existing node rather than create a near-duplicate — search for
  a node on the same subject (any folder, any title) first;
* write only facts the diff/sources state — no inferred provenance,
  motivation or status; never "blocker"/"in-progress"/investigation nodes;
  never raise an evidence level (CONFIRMED/VERIFIED/TESTED/...) or cite a
  run/commit as evidence unless that wording is in a source read (field
  2026-10-16; both rules are also enforced by the guard);
* KG only — ``docs/`` is never edited;
* skip trivial/cosmetic/test-only commits; when unsure, write nothing.

Source tracking (v0.2.103, after a field defect: a node summarised a report
that was still being revised; later commits superseded parts of the report
but the node was never updated — the reviewer found related nodes only via
hybrid_search, and nothing recorded which revision a node reflected):

* :func:`citing_nodes` — a DETERMINISTIC lookup handed to the prompt as
  "Nodes that cite files this commit changed": a plain-text match of the
  commit's changed project-relative paths inside ``knowledge/**/*.md``
  (Windows-style ``\\`` separators, leading ``./``/``../`` link prefixes,
  the absolute ``project_root/…`` spelling and — in a project nested in a
  parent repo — the REPO-relative spelling all matched too, on path
  boundaries only so ``install.py`` does not match ``first-install.py``),
  grouped per changed path, capped per path and overall
  (``[prompt] citing_nodes_max``) with UNLOCKED nodes listed before
  locked/archived ones (the guard's own ``lock_reason`` decides), the
  prompt reporting the truncation per path; changed paths left with no
  slot after the overall cap fold into ONE capped summary line, never one
  line each; frontmatter parsing and lock state are computed only for
  MATCHED nodes (a plain substring pre-check runs before the boundary
  regex), symlinks, non-regular files and files over
  ``citing_nodes_max_bytes`` are skipped, and a shallow clone reports
  "changed paths unknown";
* prompt rules — a node created or extended from a document records its
  source path and commit ("``Source: <path> @ <short-sha>``"), and a
  summary of a document visibly in progress is marked a snapshot of that
  commit (a LOCKED node never takes the snapshot marker); when a citing
  node's source changed in this commit, a "``## Status after
  <short-sha>``" block is APPENDED (append-and-annotate — superseded lines
  kept and marked in place, never deleted or reworded; locked nodes take
  their Proposed-change section instead). Reconciled with the
  no-status-tracking-nodes rule: a Status-after block annotates an EXISTING
  node with one concrete change — it is not a status-tracking node.

The diff is capped (``[prompt]`` in ``kg_reviewer_config.toml``); the prompt
says when it was truncated. A root commit is shown against the empty tree;
any other commit against its FIRST parent. Capture reads the git pipe
incrementally and stops at the cap, so a huge commit never buffers whole.
"""
from __future__ import annotations

import re
import stat as _stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from vco_lib.kg_reviewer_guard import _parse_fm, _split_frontmatter, lock_reason, walk_no_follow
from vco_lib.kg_reviewer_model import config

__all__ = ["CommitInfo", "collect_commit", "changed_paths",
           "PathCiters", "CitingScan", "citing_nodes", "build_prompt"]


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    short: str
    subject: str
    stat: str
    diff: str
    diff_truncated: bool
    diff_lines: int
    #: the first parent's full sha ("" for a root commit). The commit's own
    #: sha and its parent count as READ for the guard's evidence rule.
    parent: str = ""


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
    parent = (_git(root, "rev-parse", "--verify", f"{sha}~1").strip()
              if has_parent else "")
    return CommitInfo(sha=sha, short=short, subject=subject, stat=stat,
                      diff=diff, diff_truncated=truncated, diff_lines=kept,
                      parent=parent)


def _show_prefix(root: Path) -> str:
    """The project's path prefix inside its parent repo (``""`` at a repo
    root) — ONE home for every caller that needs it."""
    try:
        return _git(root, "rev-parse", "--show-prefix").strip()
    except (RuntimeError, OSError):
        return ""


def changed_paths(root: Path, sha: str,
                  has_parent: Optional[bool] = None) -> "List[str]":
    """The paths ``sha`` changed, PROJECT-relative.

    First-parent diff, like the diff the prompt embeds; a root commit
    against the empty tree. Git prints paths relative to the REPO root, so a
    project nested in a parent repo (v0.2.103 final review M-1) gets the
    prefix stripped — nodes cite project-relative paths — and a changed path
    elsewhere in the parent repo is dropped (not the project's, the same
    rule the guard's ``project_rel`` applies to git path output).
    ``has_parent`` (does ``sha~1`` resolve?) may be passed by a caller that
    already probed it — :func:`citing_nodes` does, so the probe runs once
    per commit, not twice."""
    if has_parent is None:
        has_parent = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}~1"],
            cwd=root, capture_output=True).returncode == 0
    if has_parent:
        args = ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                f"{sha}~1", sha]
    else:  # root commit: against the empty tree
        args = ["diff-tree", "--root", "--no-commit-id", "--name-only",
                "-r", "-z", sha]
    out = _git(root, *args)
    prefix = _show_prefix(root)
    paths: List[str] = []
    for top_rel in out.split("\0"):
        if not top_rel:
            continue
        if not prefix:
            paths.append(top_rel)
        elif top_rel.startswith(prefix):
            paths.append(top_rel[len(prefix):])
    return paths


@dataclass(frozen=True)
class PathCiters:
    """One changed path and the ``knowledge/`` nodes shown citing it."""
    #: the changed project-relative path the nodes cite.
    path: str
    #: the citing nodes shown, UNLOCKED ones first (each group sorted by
    #: node path, so the prompt is stable for one commit).
    nodes: Tuple[str, ...]
    #: citing nodes of THIS path not shown (cut by the per-path or the
    #: overall cap) - the per-path truncation the prompt reports.
    more: int


@dataclass(frozen=True)
class CitingScan:
    """What :func:`citing_nodes` found, grouped per changed path."""
    #: one :class:`PathCiters` per changed path that has citers, in the
    #: commit's changed-path order.
    entries: Tuple[PathCiters, ...]
    #: True when the changed paths could NOT be determined (a shallow
    #: clone whose commit has no parent object) - nothing was scanned.
    unknown: bool


def _is_shallow(root: Path) -> bool:
    try:
        return _git(root, "rev-parse",
                    "--is-shallow-repository").strip() == "true"
    except (RuntimeError, OSError):
        return False


def _citer_needles(rel: str, project_root: Path, prefix: str) -> "List[str]":
    """Every spelling of one changed path a node may cite, in addition to
    the optional ``./``/``../`` prefix the PATTERN matches: the
    project-relative path and its ``\\``-separated twin (node text written
    on Windows), the REPO-relative form in a project nested in a parent
    repo (a ``Source:`` line the reviewer writes carries the diff's
    repo-relative spelling), and the absolute path anchored at the project
    root — as given and resolved, so a symlinked root's spelling matches
    too (v0.2.103 review N2)."""
    forms = [rel]
    if prefix:
        forms.append(prefix + rel)
    for root in dict.fromkeys((project_root, project_root.resolve())):
        absolute = Path(root) / rel
        forms += [str(absolute), absolute.as_posix()]
    needles: "List[str]" = []
    # every spelling also in its other separator (a node written on the
    # other OS; ``C:/x`` and ``C:\\x`` alike) - review R28 N6
    for form in forms:
        for spelling in (form, form.replace("\\", "/"), form.replace("/", "\\")):
            if spelling not in needles:
                needles.append(spelling)
    return needles


def _citer_pattern(needles: "List[str]") -> "re.Pattern[str]":
    """The matcher for one changed path: any of its spellings
    (:func:`_citer_needles`) on PATH BOUNDARIES only, each optionally
    preceded by ``./``/``../`` segments (a relative Markdown link) or a
    ``file://`` scheme in front of an absolute spelling - a
    non-path character (or the string's edge) before the whole match, and
    after the path neither a path character nor a dot starting more path
    text, so ``install.py`` does not match ``first-install.py``,
    ``docs/report.md`` does not match ``mydocs/report.md.bak``, and a
    sentence-ending period still matches."""
    alts = "|".join(re.escape(n) for n in needles)
    return re.compile(rf"(?<![\w./\\-])(?:file:///?)?(?:\.{{1,2}}[\\/])*(?:{alts})"
                      rf"(?![\w/\\-])(?!\.[\w/\\-])")


def citing_nodes(project_root: Path, sha: str) -> CitingScan:
    """The ``knowledge/**/*.md`` nodes whose text CITES a path ``sha``
    changed, GROUPED per changed path (:class:`CitingScan`).

    A plain-text match on path boundaries - the changed project-relative
    path, and its other spellings (``\\`` separators, a leading ``./`` or
    ``../`` link prefix, the absolute ``project_root/…`` form and, in a
    nested project, the REPO-relative form - :func:`_citer_needles`) - so
    it is deterministic and cheap: no embedding, no KG query, no
    similarity ordering. That determinism is the point (source tracking,
    v0.2.103): hybrid_search alone missed the node summarising a report
    whose later revisions superseded it.

    Cheap on big trees too (v0.2.103 review N1): a plain substring
    pre-check runs before the boundary regex, and frontmatter parsing +
    lock state are computed only for nodes that MATCHED at least one path -
    the output is identical to the eager scan either way.

    A hot path cannot starve the others (v0.2.103 review H1): the cap
    (``[prompt] citing_nodes_max``) applies PER PATH and to the total, the
    total is allocated one slot per path per round, and within a path
    UNLOCKED nodes are listed before locked/archived ones (the guard's own
    ``lock_reason`` decides, so the two never drift). Symlinks,
    non-regular files (a FIFO named ``*.md`` must not hang the scan) and
    files over ``[prompt] citing_nodes_max_bytes`` are skipped. A shallow
    clone whose commit has no parent object cannot know its changed paths
    (every tracked file would count as changed): ``unknown=True``,
    nothing scanned."""
    cfg = config()["prompt"]
    cap = int(cfg["citing_nodes_max"])
    max_bytes = int(cfg["citing_nodes_max_bytes"])
    has_parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{sha}~1"],
        cwd=project_root, capture_output=True).returncode == 0
    if not has_parent and _is_shallow(project_root):
        return CitingScan(entries=(), unknown=True)
    prefix = _show_prefix(project_root)
    patterns = []
    for rel in changed_paths(project_root, sha, has_parent=has_parent):
        needles = _citer_needles(rel, project_root, prefix)
        patterns.append((rel, needles, _citer_pattern(needles)))
    if not patterns:
        return CitingScan(entries=(), unknown=False)
    kdir = project_root / "knowledge"
    if not kdir.is_dir():
        return CitingScan(entries=(), unknown=False)
    found: Dict[str, List[str]] = {rel: [] for rel, _p, _n in patterns}
    locked: Dict[str, bool] = {}
    for path in walk_no_follow(kdir):  # never descends through a dir symlink (I-2)
        if path.suffix != ".md":
            continue
        try:
            if path.is_symlink():  # never follow a link out of the tree
                continue
            st = path.stat()
        except OSError:
            continue
        if not _stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            continue
        try:
            with path.open("rb") as fh:
                text = fh.read(max_bytes).decode("utf-8", errors="replace")
        except OSError:
            continue
        rel_node = path.relative_to(project_root).as_posix()
        node_locked: Optional[bool] = None  # only MATCHED nodes pay for it
        for rel, needles, pattern in patterns:
            # cheap substring pre-check first; the regex then only runs on
            # candidate nodes (N1) - it cannot match what none of its
            # needles is a substring of
            if not any(n in text for n in needles):
                continue
            if not pattern.search(text):
                continue
            if node_locked is None:
                fm_block, body = _split_frontmatter(text)
                fm = _parse_fm(fm_block) if fm_block is not None else None
                node_locked = lock_reason(fm, body) is not None
                locked[rel_node] = node_locked
            found[rel].append(rel_node)
    order = [rel for rel, _p, _n in patterns if found[rel]]
    if not order:
        return CitingScan(entries=(), unknown=False)
    ranked = {rel: sorted(found[rel], key=lambda n: (locked[n], n))
              for rel in order}
    # Fair allocation: one slot per path per round (a hot path fills the
    # cap only when it is alone), each path capped at `cap` nodes itself.
    shown: Dict[str, List[str]] = {rel: [] for rel in order}
    taken = {rel: 0 for rel in order}
    allocated = 0
    while allocated < cap:
        progressed = False
        for rel in order:
            if taken[rel] >= len(ranked[rel]) or taken[rel] >= cap \
                    or allocated >= cap:
                continue
            shown[rel].append(ranked[rel][taken[rel]])
            taken[rel] += 1
            allocated += 1
            progressed = True
        if not progressed:
            break
    return CitingScan(entries=tuple(
        PathCiters(path=rel, nodes=tuple(shown[rel]),
                   more=len(found[rel]) - len(shown[rel]))
        for rel in order), unknown=False)


#: Unicode characters that render as a line/paragraph break even though
#: they are not ASCII control characters (v0.2.103 review N4) - NEL,
#: LINE SEPARATOR, PARAGRAPH SEPARATOR.
_LINE_SEPS = "\x85\u2028\u2029"


def _inline(path: str) -> str:
    """A path rendered so it cannot inject prompt structure: control
    characters and line/paragraph separators - newlines, U+0085, U+2028,
    U+2029 included - replaced by spaces, backticks defused (so the
    inline-code quoting holds)."""
    return ("".join(" " if ord(c) < 32 or ord(c) == 127 or c in _LINE_SEPS
                    else c for c in path).replace("`", "'").strip())


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
    prefix = _show_prefix(project_root)
    if prefix:
        # A project nested in a parent repo (v0.2.103 final review M-1):
        # git prints paths from the REPO root, the reviewer works from here.
        diff_note += (f" This project is the folder `{prefix}` of a larger "
                      f"git repository: the paths in the diff and the file "
                      f"list are relative to that repository's root, so a "
                      f"path `{prefix}x` is the file `x` here.")
    locked_tags = ", ".join(guard["locked_tags"])
    locked_statuses = ", ".join(guard["locked_statuses"])
    scan = citing_nodes(project_root, commit.sha)
    if scan.unknown:
        citing_block = (
            "The commit's changed-file list could NOT be determined (this "
            "repository is a shallow clone and the commit's parent object "
            "is missing), so no citing-node scan ran. Work from the Files "
            "changed and Diff above; hybrid_search in step 2 is still the "
            "way to find nodes by TOPIC.")
    elif scan.entries:
        # N3 (v0.2.103 review): a changed path the overall cap left with
        # nothing to show gets no line of its own - all of them fold into
        # ONE capped summary line, so a commit touching thousands of cited
        # files cannot add thousands of lines to the prompt.
        shown_entries = [e for e in scan.entries if e.nodes]
        folded = [e for e in scan.entries if not e.nodes]
        cite_lines = "\n".join(
            "- `{path}`: {nodes}{more}".format(
                path=_inline(entry.path),
                nodes=", ".join(f"`{_inline(n)}`" for n in entry.nodes),
                more=(f" ({entry.more} more citing node(s) of this path "
                      f"not shown)" if entry.more else ""))
            for entry in shown_entries)
        if folded:
            fold_cap = int(config()["prompt"]["citing_nodes_max"])
            named = ", ".join(f"`{_inline(e.path)}`" for e in folded[:fold_cap])
            further = (f" (+{len(folded) - fold_cap} further)"
                       if len(folded) > fold_cap else "")
            cite_lines += (f"\n- (+{len(folded)} more changed paths with "
                           f"citing nodes not shown: {named}{further})")
        citing_block = (
            "A deterministic text scan (not hybrid_search) found these "
            "knowledge/ nodes citing a path this commit changed - what they "
            "say about that path may be stale after this commit (grouped by "
            "changed path, a cap per path and overall; nodes not locked or "
            "archived listed first; changed paths left with no slot by the "
            "overall cap are named once on a single summary line):\n"
            f"{cite_lines}")
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
5. Prefer extending an existing node over creating a new one. BEFORE
   creating any node, run hybrid_search for its subject and look for an
   existing node on the same subject in ANY folder, under any title; if one
   exists, extend it. Create a new node only for a genuinely new topic with
   no existing node (one topic per node; no near-duplicates). A new node whose
   title or file name is the same as an existing node's, or mostly the same
   words as one and repeating its content, is reverted - this includes a
   node you created earlier in this run - and the log names the node to
   extend. A distinct subtopic with its own content is allowed. A new
   node must open with YAML frontmatter carrying title, type, tags, created,
   updated and status (ISO dates) - one without it is reverted.
6. Write only facts the diff or the sources you read state explicitly. Do not
   infer provenance, motivation, history, ownership or status. Never write a
   CLI flag, option name, default value, number or sample output/log that you
   did not read VERBATIM in the diff or in a file you opened (Read/Grep)
   during this run, and name the source path next to each such fact. A
   backticked `--flag` that appears in nothing you read is reverted.
   hybrid_search results do NOT count as a source for this (KG text can be
   wrong): Read the file a flag comes from before writing it.
   Never raise a confidence or evidence level: do not label a fact
   CONFIRMED, VERIFIED, TESTED, PROVEN or VALIDATED, or write that it is
   (or was) confirmed / verified / tested / proven / validated, or was so
   in, via, by, on, at or during something, or write it after a label such
   as "Status:" / "Result:" / "Outcome:", and never cite a test run, run
   id, build or commit as evidence for a fact, unless that exact word - and
   that exact id - is in the diff, its commit stat or sha, or a Read/Grep
   result from this run. When the source says "inferred", "untested" or
   "unverified", the node says so too. A line that makes such a claim
   without a source, or cites a run/commit found in nothing you read, is
   reverted with its whole write.
7. Record where a node's facts came from. A node you create or extend from a
   document or other source file records the source path and the commit it
   reflects, as a line like "Source: docs/reports/cache.md @ {commit.short}".
   When you later extend the node from a newer revision, APPEND the new
   source line and keep the earlier ones (rule 10); never reword or drop an
   existing source line. When the document you record is visibly still in
   progress (a draft, an open review round), mark the node's summary as a
   snapshot of that commit: "Snapshot of <path> @ {commit.short}". A LOCKED
   node (rule 11) never takes the snapshot marker - it takes only the
   Proposed change section of rule 11.
8. A node listed under "Nodes that cite files this commit changed" may state
   something this commit superseded. Read the changed file it cites and,
   when the commit changed what the node states, APPEND one section at the
   end of the node:
   ## Status after {commit.short}
   stating what changed and which of the node's statements it supersedes;
   mark superseded lines IN PLACE per rule 10 - never delete or reword
   them. A LOCKED node (rule 11) takes only the Proposed change section of
   rule 11 instead.
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
