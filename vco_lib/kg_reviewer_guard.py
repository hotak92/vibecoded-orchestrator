# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hard boundaries around the post-commit KG reviewer (v0.2.103).

The reviewer is a background ``claude -p`` session spawned after every commit
(``templates/scripts/kg_reviewer_backend.py``, from the
``post-git-commit-kg-sync`` hook pair). In a user project an earlier version
rewrote a "do not change without discussion" decision node and stripped its
frontmatter, edited an ADR under ``docs/``, minted duplicate and stale status
nodes from a partial diff, and left its edits uncommitted in the user's tree
where ``git commit -a`` swept them into an unrelated commit. The prompt now
forbids all of that; this module ENFORCES it, independent of the prompt.

The overriding rule (owner bar): the guard must NEVER destroy or misattribute
the USER's work. The review runs for minutes while the user keeps editing the
same tree, so every decision below is taken only on bytes PROVABLY written by
the reviewer.

1. :func:`take_snapshot` — before the run: a COPY of every ``knowledge/``
   file (derived sidecars excepted), every ``knowledge/`` symlink (its link
   text, plus a copy of the file it points at, wherever that is), the HEAD
   the run starts from, and the paths the user already had changes on
   (staged, unstaged or untracked; their bytes are copied aside too).
2. :func:`attributed_writes` — after the run: every write the reviewer
   made, in order, read from its ``--output-format stream-json`` transcript
   (``Write`` content; ``Edit`` / ``MultiEdit`` replacements; failed calls,
   whose ``tool_result`` is an error, are dropped). ``None`` when the
   transcript carries no stream events (an older CLI, a crash).
3. :func:`expected_bytes` — REPLAYS those writes from the snapshot to get
   the exact bytes the reviewer left in each file. A path whose current
   bytes differ from that replay (or whose replay is impossible: an ``Edit``
   whose ``old_string`` is no longer there, an opaque tool) was changed by
   someone else during the run — the user. Such a path is NEVER reverted and
   NEVER committed: its current bytes are backed up, a loud log line names
   it, and it is left exactly as it is.
4. :func:`validate` — every reviewer-owned change that breaks a rule is
   REVERTED from the snapshot copy (never ``git checkout``; a path that was
   clean at the start is restored from the starting commit's blob, a path
   that did not exist is removed). The current bytes are ALWAYS backed up
   first, under ``.claude/backups/kg-reviewer/<utc-stamp>/`` (self-ignoring:
   :mod:`vco_lib.project_backups`, so ``git add -A`` never stages it), or —
   for a symlink target outside the project — under the user's VCO state dir
   (``backups/kg-reviewer-outside/``), never in the project tree; a file
   whose bytes cannot be backed up is not touched. A ``knowledge/`` symlink
   target over ``[guard] link_target_max_bytes`` is not copied at all: it is
   logged as not restorable. Rules, each logged with path + reason:

   * a path outside ``knowledge/`` (or outside the project);
   * a write through, or over, a ``knowledge/`` symlink (its target may be
     anywhere: the link and its target are both restored);
   * a non-node file under ``knowledge/`` (anything but ``*.md``; the
     derived sidecars in ``[guard] derived_sidecars`` are left alone);
   * an existing node whose YAML frontmatter was removed, rewritten, or ADDED
     where it had none (adding/updating the keys in
     ``[guard] frontmatter_mutable_keys`` — ``updated`` — is allowed);
   * a LOCKED node (``[guard] locked_tags`` / ``locked_types`` /
     ``locked_statuses`` / ``locked_body_markers``) edited in any way other
     than appending one ``## Proposed change (kg-reviewer, <YYYY-MM-DD>)``
     section;
   * an existing (unlocked) node whose body lost or reworded any original
     non-blank line — existing text is APPEND-AND-ANNOTATE ONLY (owner rule
     2026-10-15: stored information survives; a superseded line is kept and
     marked in place, e.g. "Original <date>: <line> (superseded by ...)" or
     "~~<line>~~"). Compared after CRLF->LF and trailing-whitespace strip;
     an original line survives verbatim or as a contiguous substring of a
     new line (:func:`removed_lines`); the log names the lost lines.
     ONE exception (v0.2.105, owner request 2026-11-04: a node refined
     commit after commit collected a CHAIN of "## Status after <sha>"
     sections, each superseding the last, and readers land on the stale
     ones first): a write may FOLD a PROVEN reviewer section — one whose
     current normalized bytes hash-match the provenance ledger the
     reviewer's own commits record (:func:`proven_sections`,
     :func:`record_provenance`, :mod:`vco_lib.kg_reviewer_provenance`) —
     and only per fact: every line of it must survive in the new body or
     be named by a new ``Superseded: <key> → <new> (<date>): <fact>`` trail
     line (:func:`_fold_removals`). Every other removed or reworded line —
     the owner's, an unproven section's, an uncommitted line's — still
     reverts the whole write: provenance is RECORDED, never inferred from
     a commit subject or ``git blame`` (:data:`UNKNOWN` bytes and
     uncommitted changes keep the append-only rule);
   * a NEW node without frontmatter carrying every
     ``[guard] new_node_required_keys`` key (title, type, tags, created,
     updated, status), or whose title duplicates an existing node's
     (case/whitespace-normalised) — the log points at the node to extend;
   * a NEW node on the SAME SUBJECT as an existing node (or as a new node
     this run already kept) under another title or folder: title/slug
     content tokens (title and slug compared both ways) overlapping with a
     Jaccard similarity >= ``[guard] duplicate_title_jaccard``, AND either
     exactly the same title/slug words or bodies sharing >= ``[guard]
     duplicate_body_min_shared`` content tokens (:func:`same_subject_node`;
     a subtopic with its own content is kept) — the log names the node to
     extend (field 2026-10-16: a second node on one subject);
   * a node that GAINS a backticked ``--flag`` appearing verbatim nowhere
     the reviewer read (its ``Read`` / ``Grep`` results and the commit diff
     it was given) — the field showed invented CLI flags. Invented numbers,
     defaults and sample output are forbidden by the prompt only: unlike a
     flag token they have no reliable lexical shape to check without
     reverting most legitimate prose;
   * a node that GAINS a line raising an evidence level — a ``[guard]
     evidence_markers`` word (CONFIRMED, VERIFIED, ...) used as a CLAIM
     (uppercase, followed by in/via/by/on/at/during, directly after
     is/are/was/were/be/been AND ending its clause (v0.2.105: "was
     validated input" is a noun phrase, not a claim), or after a ``[guard]
     evidence_label_words`` label and colon such as "Status:", or on a line
     citing a run/test id or commit hash) that no SAME-PASSAGE source
     carries — v0.2.105: the marker must occur on a corpus line sharing
     ``[guard] evidence_passage_min_shared_tokens`` content tokens with the
     claim line, not merely somewhere in the corpus — or a
     run/test id / commit hash it read nowhere cited on such a line
     (:func:`unsourced_evidence`; the field showed "CONFIRMED ... via test
     run <stamp>" over sources that said "inferred, untested"). Prose use
     ("the tested path"), headings without attribution, fenced code and
     frontmatter are not claims (a fence that never closes does not exempt
     the lines after it); lines that already carried the marker are not
     judged.

   A deleted node is never the reviewer's doing (none of its tools deletes
   — the shell is denied — and every write leaves bytes), so a vanished
   attributed node is someone else's act: logged and left, never
   resurrected over the user's decision.

   A reverted node is re-synced to the KG collection (soft-fail) so the
   store does not keep the reviewer's version; a reverted NEW node's rows
   are pruned instead — on the orchestrator root (KG collection == shared
   collection) only the rows carrying the reviewer's exact bytes' content
   hash, and on a non-root project the shared collection's rows of a
   ``scope: shared`` node too (same hash scoping; skipped with a reason
   when shared writes are disabled or no hash names the path) — and the
   log states what the sync reported (:func:`resync_with`).
   An UNATTRIBUTABLE run
   (no stream events) judges, reverts and commits NOTHING: without proof of
   who wrote what, any knowledge/ change since the snapshot may be the
   user's own.
5. :func:`commit_reviewer_changes` — the surviving reviewer files, and ONLY
   those, are committed as ``kg-sync: <short-hash> <short subject>`` with a
   pathspec-limited ``git commit --only -- <paths>``, so the user's staged and
   unstaged work is never included. A path is committed only while its bytes
   still equal what the reviewer wrote (a user edit landing after validation
   leaves that path uncommitted, logged). The commit is SKIPPED (files left
   in place, one log line) when a merge / rebase / cherry-pick / revert /
   bisect is in progress, ``index.lock`` exists, HEAD moved since the run
   started AND that move touched one of the reviewer's own paths, a target
   path already had user changes before the run, or the run's writes could
   not be attributed. A run that wrote nothing commits nothing.

   v0.2.105 L-KGREV — a skip is never silent again, and never cascades:

   * a HEAD that moved is only a reason to skip when ``start..HEAD`` touched
     one of the reviewer's files (:func:`head_changed_paths`); otherwise the
     nodes land as a clean commit on top;
   * a git operation in progress is retried once, for up to
     :data:`GIT_OP_RETRY_S` seconds, before the skip stands;
   * every path this call leaves uncommitted is recorded in
     :mod:`vco_lib.kg_reviewer_pending` together with the sha256 of the bytes
     the run PROVED the reviewer wrote, and every path it commits is dropped
     from it. The next run folds a ledgered path whose current bytes still
     hash to that sha into its own commit — the reviewer's own leftover, not
     a user change — while a ledgered path whose bytes differ is the user's
     and is never committed. The ledger is what
     :func:`vco_lib.kg_reviewer_pending.surface` turns into one alert for the
     session.

   The user's git hooks are NOT bypassed (no ``--no-verify``): a hook that
   rejects the commit leaves the files uncommitted and the reason in the
   reviewer log — silently skipping a project's own commit policy would be a
   worse surprise than an uncommitted node.

Derived sidecars (``knowledge/.node_formats.json`` and friends) are neither
validated nor committed: the summary generator that rewrites them is itself a
detached background job whose completion time is unknown, it also serves the
user's own edits, and the file is regenerated from node content — committing
it would race that job and fold unrelated summaries into a ``kg-sync:``
commit, while leaving it loses nothing.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

import yaml

from vco_lib import kg_prune_result
from vco_lib import kg_reviewer_pending as _pending
from vco_lib.kg_reviewer_model import config
from vco_lib.knowledge_residue import content_signature_of_file_bytes

__all__ = [
    "Snapshot",
    "ValidationResult",
    "UNKNOWN",
    "take_snapshot",
    "attributed_writes",
    "attributed_paths",
    "expected_bytes",
    "transcript_sources",
    "unsourced_flags",
    "unsourced_evidence",
    "evidence_ids",
    "subject_tokens",
    "same_subject_node",
    "new_node_violation",
    "removed_lines",
    "ReplaceableSection",
    "TrailEntry",
    "STATUS_HEADING_PREFIX",
    "CURRENT_STATE_HEADING_PREFIX",
    "TRAIL_PREFIX",
    "parse_trail",
    "reviewer_sections",
    "proven_sections",
    "reviewer_originated_sections",
    "record_provenance",
    "trail_keys",
    "validate",
    "commit_reviewer_changes",
    "commit_block_reason",
    "head_changed_paths",
    "is_git_op_block",
    "is_reviewer_commit_subject",
    "commit_subject",
]

Log = Callable[[str], None]

KNOWLEDGE_DIR = "knowledge"
#: Tool names whose input carries the path they write.
_PATH_KEYS = {"Write": "file_path", "Edit": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_STORE_TOOL_SUFFIX = "store_knowledge_node"
#: Total bytes of the user's dirty files outside knowledge/ copied aside.
_DIRTY_COPY_BUDGET = 64 * 1024 * 1024
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_DATE_HEADING_TAIL = re.compile(r"\d{4}-\d{2}-\d{2}\)")


class _Unknown:
    """The replay could not determine the reviewer's bytes."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()
#: A replayed file state: its bytes, ``None`` (absent) or :data:`UNKNOWN`.
Expected = Union[bytes, None, _Unknown]
#: One recorded write: ``("write", content)``, ``("edit", [(old, new,
#: replace_all), ...])`` or ``("opaque", tool_name)``.
WriteOp = Tuple[str, object]


def _guard() -> dict:
    return config()["guard"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _lf_text(data: bytes) -> str:
    """``data`` decoded for RULE comparison with CRLF read as LF (v0.2.103
    final review L-6). The CLI's edit tools keep a file's own line endings
    while the transcript carries LF, so a CRLF node's pre and post texts can
    differ in endings alone; judging them byte-for-byte made a legitimate
    proposal append to a locked node read as a rewrite. Comparison only —
    the guard never rewrites a file's line endings on disk (a revert
    restores the snapshot's exact bytes)."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _same(a: Optional[bytes], b: Optional[bytes]) -> bool:
    """Byte equality, tolerant only of CRLF/LF (the CLI's edit tools keep a
    file's line-ending style; the transcript carries LF)."""
    if a is None or b is None:
        return a is b
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def _git(root: Path, *args: str, check: bool = False,
         timeout: Optional[float] = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          check=check, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def is_derived_sidecar(rel: str) -> bool:
    """``knowledge/<name>`` matching ``[guard] derived_sidecars``."""
    parts = PurePosixPath(rel).parts
    if len(parts) != 2 or parts[0] != KNOWLEDGE_DIR:
        return False
    return any(fnmatch.fnmatchcase(parts[1], pat)
               for pat in _guard()["derived_sidecars"])


def commit_subject(short: str, subject: str) -> str:
    """``kg-sync: <short-hash> <short subject>`` (subject cut at 60 chars)."""
    s = " ".join(subject.split())
    if len(s) > 60:
        s = s[:57].rstrip() + "..."
    return f"{_guard()['commit_prefix']} {short} {s}".rstrip()


def is_reviewer_commit_subject(subject: str) -> bool:
    """True for the reviewer's own commits (the hook's recursion guard)."""
    return subject.lstrip().startswith(_guard()["commit_prefix"])


def path_key(raw: Union[str, Path], root: Path) -> Path:
    """The canonical key of a written path: absolute, ``..`` folded, every
    DIRECTORY resolved — but the last component NOT, so a write naming a
    symlink stays keyed on the link (the symlink rule needs to see it)."""
    p = Path(raw)
    p = p if p.is_absolute() else root / p
    p = Path(os.path.abspath(p))
    return Path(os.path.realpath(p.parent)) / p.name


# ── snapshot ─────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    root: Path
    start_head: Optional[str]
    copy_dir: Path
    #: ``knowledge/...`` rel path -> sha256 of its bytes at the start.
    knowledge: Dict[str, str] = field(default_factory=dict)
    #: ``knowledge/...`` rel path of a symlink -> its link text.
    symlinks: Dict[str, str] = field(default_factory=dict)
    #: absolute resolved target of a knowledge/ symlink -> copy path (the
    #: target may live outside the project).
    link_targets: Dict[str, Path] = field(default_factory=dict)
    #: resolved targets over ``[guard] link_target_max_bytes``: not copied,
    #: so not restorable.
    oversized_targets: Set[str] = field(default_factory=set)
    #: rel path -> sha256 (None = deleted) of every path the USER already
    #: had changes on at the start (staged, unstaged or untracked).
    dirty_before: Dict[str, Optional[str]] = field(default_factory=dict)
    #: dirty rel paths outside knowledge/ whose bytes were copied aside.
    dirty_copied: Set[str] = field(default_factory=set)
    #: normalised frontmatter ``title`` -> rel path, for every existing node.
    titles: Dict[str, str] = field(default_factory=dict)
    #: rel path -> normalised frontmatter title ('' when none), for every
    #: existing ``*.md`` node (the same-subject duplicate rule).
    nodes: Dict[str, str] = field(default_factory=dict)
    #: rel path -> :func:`body_tokens` of every existing ``*.md`` node (the
    #: same-subject rule's content-overlap confirmation).
    node_bodies: Dict[str, Set[str]] = field(default_factory=dict)

    def copy_of(self, rel: str) -> Path:
        return self.copy_dir / "files" / rel

    def rel(self, key: Path) -> Optional[str]:
        try:
            return key.relative_to(self.root).as_posix()
        except ValueError:
            return None

    def pre_bytes_key(self, key: Path) -> "Tuple[bool, Optional[bytes]]":
        """:meth:`pre_bytes` for any key, including a symlink target that
        lives outside the project."""
        copy = self.link_targets.get(str(key))
        if copy is not None:
            return True, _read_bytes(copy)
        rel = self.rel(key)
        if rel is None:
            return False, None
        return self.pre_bytes(rel)

    def pre_bytes(self, rel: str) -> "Tuple[bool, Optional[bytes]]":
        """``(known, bytes)`` — the path's content at the start (``None`` =
        absent). ``known`` is False when it cannot be determined (an ignored
        file outside knowledge/, a dirty file over the copy budget)."""
        copy = self.link_targets.get(str(self.root / rel))
        if copy is not None:
            return True, _read_bytes(copy)
        if rel in self.knowledge or rel in self.dirty_copied:
            return True, _read_bytes(self.copy_of(rel))
        if rel in self.symlinks:
            return False, None
        if rel.startswith(KNOWLEDGE_DIR + "/") and not is_derived_sidecar(rel):
            # Every knowledge file was copied: not in the copy = absent.
            return True, None
        if rel in self.dirty_before:
            if self.dirty_before[rel] is None:
                return True, None  # the user had deleted it
            return False, None
        if self.start_head:
            # `./` = relative to cwd (the project), not the repo toplevel.
            blob = _git(self.root, "cat-file", "blob",
                        f"{self.start_head}:./{rel}")
            if blob.returncode == 0:
                return True, blob.stdout
        ignored = _git(self.root, "check-ignore", "-q", "--", rel).returncode == 0
        if ignored:
            return False, None
        return True, None  # untracked, not ignored, not dirty -> did not exist

    def cleanup(self) -> None:
        shutil.rmtree(self.copy_dir, ignore_errors=True)


def repo_prefix(root: Path) -> str:
    """``root``'s path inside its enclosing repo (``git rev-parse
    --show-prefix``: ``""`` at the toplevel, ``"apps/foo/"`` for a project
    nested in a parent repo).

    Every guard path is PROJECT-relative (``knowledge/...`` under the folder
    that owns ``.claude/``), and every git command runs with ``cwd=root``, so
    pathspecs (``add``/``commit --only``/``check-ignore``/``ls-files``) and
    ``<rev>:./<rel>`` already resolve against the project. Only git OUTPUT
    that lists paths (``status --porcelain``, ``diff-tree --name-only``) is
    toplevel-relative and goes through :func:`project_rel` (v0.2.103 final
    review M-1)."""
    proc = _git(root, "rev-parse", "--show-prefix")
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="surrogateescape").strip()


def project_rel(top_rel: str, prefix: str) -> Optional[str]:
    """A toplevel-relative git path as a project-relative one; ``None`` when
    it lies outside the project (elsewhere in the parent repo)."""
    if not prefix:
        return top_rel
    if top_rel.startswith(prefix):
        return top_rel[len(prefix):]
    return None


def _status_paths(root: Path) -> Dict[str, str]:
    """Project-relative path -> porcelain XY code for every changed or
    untracked path under the project folder (paths elsewhere in a parent
    repo are not the project's and are left out)."""
    proc = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--", ".")
    if proc.returncode != 0:
        return {}
    prefix = repo_prefix(root)
    out: Dict[str, str] = {}
    entries = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    i = 0

    def _add(top_rel: str, code: str) -> None:
        rel = project_rel(top_rel, prefix)
        if rel:
            out[rel] = code

    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        _add(path, code)
        if code[0] in "RC":  # rename/copy: the next field is the source
            if i < len(entries) and entries[i]:
                _add(entries[i], code)
            i += 1
    return out


def walk_no_follow(top: Path) -> "Iterable[Path]":
    """Every entry under ``top`` — files, and symlinks (to files OR to
    directories) as entries — without ever descending through a directory
    symlink. The ONE walk of ``knowledge/`` for the guard's snapshot and the
    prompt's citing-node scan (review V02103-FINAL2 I-2): it states the
    no-follow rule in code instead of relying on ``Path.rglob``'s
    version-specific symlink semantics (``recurse_symlinks`` arrived in 3.13).
    Sorted, so callers are deterministic.
    """
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        base = Path(dirpath)
        out.extend(base / d for d in dirnames if (base / d).is_symlink())
        out.extend(base / f for f in filenames)
    return sorted(out)


def take_snapshot(root: Path) -> Snapshot:
    root = root.resolve()
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    start_head = head.stdout.decode().strip() if head.returncode == 0 else None
    snap = Snapshot(root=root, start_head=start_head,
                    copy_dir=Path(tempfile.mkdtemp(prefix="vco-kg-reviewer-")))
    kdir = root / KNOWLEDGE_DIR
    if kdir.is_dir():
        for path in walk_no_follow(kdir):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                snap.symlinks[rel] = os.readlink(path)
                target = path.resolve()
                if (target.is_file() and str(target) not in snap.link_targets
                        and target.stat().st_size > _link_target_cap()):
                    # Review R11 A2: never copy a huge link target on every
                    # commit; it is recorded as not restorable instead.
                    snap.oversized_targets.add(str(target))
                elif target.is_file() and str(target) not in snap.link_targets:
                    dest = snap.copy_dir / "link-targets" / str(len(snap.link_targets))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(target.read_bytes())
                    snap.link_targets[str(target)] = dest
                continue
            if not path.is_file() or is_derived_sidecar(rel):
                continue
            data = path.read_bytes()
            dest = snap.copy_of(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            snap.knowledge[rel] = _sha(data)
            if rel.endswith(".md"):
                text = data.decode("utf-8", errors="replace")
                title = _node_title(text)
                snap.nodes[rel] = title
                snap.node_bodies[rel] = body_tokens(text)
                if title:
                    snap.titles.setdefault(title, rel)
    budget = _DIRTY_COPY_BUDGET
    for rel in _status_paths(root):
        data = _read_bytes(root / rel)
        snap.dirty_before[rel] = None if data is None else _sha(data)
        if data is None or rel in snap.knowledge:
            continue
        if rel.startswith(KNOWLEDGE_DIR + "/") or len(data) > budget:
            continue
        dest = snap.copy_of(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        snap.dirty_copied.add(rel)
        budget -= len(data)
    return snap


# ── attribution ──────────────────────────────────────────────────────────


def _iter_events(stdout: str) -> Iterable[dict]:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content
                         if isinstance(c, dict))
    return ""


def _op_for(name: str, tool_input: dict) -> "Optional[WriteOp]":
    if name == "Write":
        content = tool_input.get("content")
        return ("write", content) if isinstance(content, str) else ("opaque", name)
    if name == "Edit":
        return ("edit", [(tool_input.get("old_string"), tool_input.get("new_string"),
                          bool(tool_input.get("replace_all")))])
    if name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return ("edit", [(e.get("old_string"), e.get("new_string"),
                          bool(e.get("replace_all")))
                         for e in edits if isinstance(e, dict)])
    return ("opaque", name)


def attributed_writes(stdout: str, root: Path
                      ) -> "Optional[Dict[Path, List[WriteOp]]]":
    """Every write the reviewer made, keyed by :func:`path_key`, in order.

    Calls whose ``tool_result`` is an error (a permission denial, an ``Edit``
    that did not match) are dropped — they changed nothing. A call with no
    result at all (the run was cut) is kept: it may have landed. A
    ``store_knowledge_node`` call is recorded as OPAQUE at its result's
    ``absolute_path`` (its bytes cannot be replayed). ``None`` when the
    transcript has no stream events at all (an older CLI, a crash)."""
    root = root.resolve()
    pending: Dict[str, Tuple[Optional[Path], WriteOp, bool]] = {}
    order: List[str] = []
    saw_stream = False

    for ev in _iter_events(stdout):
        kind = ev.get("type")
        if kind in ("assistant", "user", "system"):
            saw_stream = True
        message = ev.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                tool_input = block.get("input") or {}
                if not isinstance(tool_input, dict):
                    continue
                use_id = str(block.get("id") or f"anon-{len(order)}")
                if name in _PATH_KEYS:
                    raw = tool_input.get(_PATH_KEYS[name])
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    op = _op_for(name, tool_input)
                    assert op is not None
                    pending[use_id] = (path_key(raw.strip(), root), op, False)
                    order.append(use_id)
                elif name.endswith(_STORE_TOOL_SUFFIX):
                    raw = tool_input.get("file_path")
                    key = (path_key(raw.strip(), root)
                           if isinstance(raw, str) and raw.strip() else None)
                    pending[use_id] = (key, ("opaque", name), False)
                    order.append(use_id)
            elif block.get("type") == "tool_result":
                use_id = str(block.get("tool_use_id") or "")
                if use_id not in pending:
                    continue
                key, op, _failed = pending[use_id]
                failed = bool(block.get("is_error"))
                if op == ("opaque", op[1]) and str(op[1]).endswith(_STORE_TOOL_SUFFIX):
                    text = _result_text(block.get("content"))
                    found: Optional[str] = None
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            found = parsed.get("absolute_path")
                    except ValueError:
                        m = re.search(r'"absolute_path"\s*:\s*"([^"]+)"', text)
                        found = m.group(1) if m else None
                    if isinstance(found, str) and found.strip():
                        key = path_key(found.strip(), root)
                pending[use_id] = (key, op, failed)
    if not saw_stream:
        return None
    writes: Dict[Path, List[WriteOp]] = {}
    for use_id in order:
        key, op, failed = pending[use_id]
        if key is None or failed:
            continue
        writes.setdefault(key, []).append(op)
    return writes


def attributed_paths(stdout: str, root: Path) -> Optional[Set[Path]]:
    """The keys of :func:`attributed_writes` (``None`` = unattributable)."""
    writes = attributed_writes(stdout, root)
    return None if writes is None else set(writes)


def expected_bytes(start: Expected, ops: List[WriteOp]) -> Expected:
    """Replay ``ops`` from ``start``: the bytes the reviewer left behind, or
    :data:`UNKNOWN` when they cannot be derived (an ``Edit`` whose
    ``old_string`` is not in the replayed text — someone else changed the
    file — or an opaque tool)."""
    cur: Expected = start
    for kind, payload in ops:
        if kind == "write":
            assert isinstance(payload, str)
            cur = payload.encode("utf-8")
            continue
        if kind != "edit" or not isinstance(cur, bytes):
            return UNKNOWN
        text = cur.decode("utf-8", errors="surrogateescape").replace("\r\n", "\n")
        for old, new, replace_all in payload:  # type: ignore[union-attr]
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                return UNKNOWN
            if old not in text:
                return UNKNOWN
            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        cur = text.encode("utf-8", errors="surrogateescape")
    return cur


# ── validation ───────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    #: knowledge/ node paths (rel) the reviewer changed that passed.
    kept: List[str] = field(default_factory=list)
    #: rel path -> the reviewer's bytes for each kept path (the commit only
    #: takes a path while it still holds exactly these bytes).
    reviewer_bytes: Dict[str, bytes] = field(default_factory=dict)
    #: (rel or absolute path, reason) for every revert.
    reverted: List[Tuple[str, str]] = field(default_factory=list)
    #: paths a rule rejected but that could not be restored.
    unrestorable: List[Tuple[str, str]] = field(default_factory=list)
    #: paths someone else changed during the run: left as they are.
    left_for_user: List[str] = field(default_factory=list)
    #: where this run's backups went (None: nothing was backed up).
    backup_dir: Optional[Path] = None
    #: True when the run could not be attributed (nothing was judged).
    unattributed: bool = False


def _split_frontmatter(text: str) -> "Tuple[Optional[str], str]":
    m = _FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _parse_fm(block: str) -> Optional[dict]:
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def _as_list(value: object) -> List[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.replace("#", "").split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip().lstrip("#") for v in value]
    return []


def lock_reason(fm: Optional[dict], body: str) -> Optional[str]:
    """Why a node is LOCKED (``None`` = not locked)."""
    g = _guard()
    if fm:
        tags = {t.lower() for t in _as_list(fm.get("tags"))}
        hit = sorted(tags & {t.lower() for t in g["locked_tags"]})
        if hit:
            return f"tagged {', '.join(hit)}"
        ntype = str(fm.get("type") or "").strip().lower()
        if ntype and ntype in {t.lower() for t in g["locked_types"]}:
            return f"type {ntype}"
        status = fm.get("status")
        if status is None and isinstance(fm.get("metadata"), dict):
            status = fm["metadata"].get("status")
        status = str(status or "").strip().lower()
        if status and status in {s.lower() for s in g["locked_statuses"]}:
            return f"status {status}"
    lowered = body.lower()
    for marker in g["locked_body_markers"]:
        if marker.lower() in lowered:
            return f"says {marker!r}"
    return None


def _clip(text: str, width: int = 80) -> str:
    return text if len(text) <= width else text[:width - 3] + "..."


#: A leading Markdown list / quote / task marker: "- ", "* ", "+ ", "1. ",
#: "1) ", "> ", "- [ ] ", "- [x] " (combinations included).
_LINE_MARKER = re.compile(r"^(?:>\s*)*(?:(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?)?")


def _body_lines(body: str) -> List[str]:
    """Body lines after CRLF->LF and trailing-whitespace strip."""
    return [line.rstrip() for line in body.replace("\r\n", "\n").split("\n")]


def _missing_with_index(body_pre: str, body_post: str) -> List[Tuple[int, str]]:
    """The non-blank ORIGINAL body lines that do not survive in the new body,
    as ``(body line index, line)`` — the one matcher behind
    :func:`removed_lines` and the replaceable-section exception.

    Owner rule (2026-10-15): stored information must survive — an existing
    node's text is append-and-annotate only. An original line survives when
    it is still present verbatim or EXTENDED IN PLACE: its text (leading and
    trailing whitespace aside) is a contiguous substring of a new line, so
    "Original 2026-10-15: <line> (superseded by ...)" and "~~<line>~~" pass.
    Lines are matched in order, each to a distinct new line (greedy
    earliest match, which finds a match whenever one exists), so a duplicate
    original line that was dropped is caught too. Reordering original lines
    counts as removing them: a reorder is a rewrite."""
    new = _body_lines(body_post)
    missing: List[Tuple[int, str]] = []
    j = 0
    for i, line in enumerate(_body_lines(body_pre)):
        # The needle is the line's TEXT: a leading Markdown list/quote marker
        # is not text, so "- X" annotated as "- Original <date>: X ..." or
        # "- ~~X~~" survives (the marker stays, the annotation goes after it).
        needle = _LINE_MARKER.sub("", line.strip(), count=1).strip()
        if not needle:
            continue
        k = j
        while k < len(new) and needle not in new[k]:
            k += 1
        if k < len(new):
            j = k + 1
        else:
            missing.append((i, line.strip()))  # named as the user wrote it
    return missing


def removed_lines(body_pre: str, body_post: str) -> List[str]:
    """The non-blank ORIGINAL body lines that do not survive in the new body
    (:func:`_missing_with_index`, without the indices)."""
    return [line for _idx, line in _missing_with_index(body_pre, body_post)]


# ── reviewer-owned sections (v0.2.105) ───────────────────────────────────
#
# The reviewer appends one "## Status after <sha>" section per commit it
# reviews (prompt rule 8); a decision refined several times in a row collects
# a CHAIN of them, each superseding the last, and readers and search hits
# land on the stale statements first. The fix (owner request 2026-11-04): a
# later reviewer pass may FOLD its own earlier sections into one
# "## Current state (consolidated <date>)" section, carrying every
# still-true fact over and naming every dropped one as superseded.
#
# Provenance is a LEDGER, never an inference (review V02105-KGREV2 H1/H2):
# when the reviewer's own commit is made, :func:`commit_reviewer_changes`
# records each section the reviewer ORIGINATED in it
# (:func:`reviewer_originated_sections`) — the sha256 of the section's
# normalized bytes plus the commit — in :mod:`vco_lib.kg_reviewer_provenance`.
# A section is foldable only while its CURRENT bytes hash-match an entry of
# the same node whose commit is still an ancestor of the reviewed revision
# (:func:`proven_sections`). An amended or rebased reviewer commit, a user
# commit titled "kg-sync: ...", a line the owner added inside the section, a
# CRLF->LF rewrite of an owner section, an owner line the reviewer annotated:
# none of them yields a matching entry, so none of them is foldable.

#: The heading prefix of a reviewer Status section (prompt rule 8). ONE
#: home: the prompt builder renders its Status headings from this.
STATUS_HEADING_PREFIX = "## Status after "
#: The heading prefix of the consolidated section a fold writes (rule 8).
CURRENT_STATE_HEADING_PREFIX = "## Current state (consolidated "
#: The trail a fold carries, one line per step and one per dropped fact:
#: ``Superseded: <old key> → <new key> (<date>)`` names a folded section;
#: ``Superseded: <old key> → <new key> (<date>): <fact text>`` names one
#: dropped statement of that section (an ASCII ``->`` arrow is accepted).
TRAIL_PREFIX = "Superseded:"

_STATUS_HEADING_RE = re.compile(
    r"^## Status after ([0-9A-Za-z][0-9A-Za-z.-]{2,39})$")
_CURRENT_HEADING_RE = re.compile(
    r"^## Current state \(consolidated (\d{4}-\d{2}-\d{2})\)$")
_TRAIL_LINE_RE = re.compile(
    r"^(?:[-*+]\s+)?Superseded:\s*(?P<old>[0-9A-Za-z][\w.-]*)\s*(?:→|->)\s*"
    r"(?P<new>[0-9A-Za-z][\w.-]*)(?:\s*\((?P<date>[^)]*)\))?"
    r"(?:\s*:\s*(?P<fact>.*\S))?$")
#: A section ends at the next level-1/2 ATX heading (outside a code fence),
#: so a "### Built" sub-heading belongs to the section it groups.
_SECTION_END_RE = re.compile(r"^ {0,3}#{1,2}(?:\s|$)")
#: A foreign (non-reviewer) line at least this long marks every new line
#: that CONTAINS it as foreign too (an annotated or quoted owner line);
#: shorter ones only by equality (a "---" rule must not taint every line).
_FOREIGN_CONTAINS_MIN = 12
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ReplaceableSection:
    """A reviewer section of a node: a ``## Status after <sha>``, a rule-11
    ``## Proposed change (kg-reviewer, <date>)`` or a ``## Current state
    (consolidated <date>)`` heading and its lines up to the next level-1/2
    heading. Foldable only when :func:`proven_sections` returns it."""

    #: body line index of the section heading (0-based).
    start: int
    #: exclusive body line index of the section's end.
    end: int
    #: what a ``Superseded:`` trail line names to fold it: the sha of a
    #: Status heading, the date of a Proposed-change heading, the NEW key of
    #: the last trail line of a Current-state section (its heading date
    #: when it carries none).
    key: str
    #: the heading line, as the prompt lists it.
    heading: str
    #: ``status`` / ``proposed`` / ``current``.
    kind: str = "status"
    #: :func:`vco_lib.kg_reviewer_provenance.section_digest` of its lines.
    digest: str = ""


@dataclass(frozen=True)
class TrailEntry:
    """One parsed ``Superseded:`` line."""

    old: str
    new: str
    date: str
    #: the dropped fact it names, whitespace-collapsed ("" = a step line).
    fact: str


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _needle(line: str) -> str:
    """A line's TEXT for matching: list/quote marker dropped, whitespace
    collapsed (the same marker rule as :func:`_missing_with_index`)."""
    return _collapse(_LINE_MARKER.sub("", line.strip(), count=1))


def parse_trail(line: str) -> Optional[TrailEntry]:
    """The trail entry a body line carries, or ``None``."""
    m = _TRAIL_LINE_RE.match(line.strip())
    if m is None:
        return None
    return TrailEntry(m.group("old"), m.group("new"), m.group("date") or "",
                      _collapse(m.group("fact") or ""))


def trail_keys(body_post: str) -> Set[str]:
    """The section keys the body's ``Superseded:`` trail lines name (the old
    side of each step)."""
    return {t.old for line in _body_lines(body_post)
            for t in [parse_trail(line)] if t is not None}


def reviewer_sections(text: str) -> List[ReplaceableSection]:
    """Every reviewer-shaped section of a node (``text`` CRLF-tolerant),
    with body line indices (the body below the frontmatter, as
    :func:`edit_violation` indexes it). Shape only — :func:`proven_sections`
    decides which are the reviewer's."""
    from vco_lib.kg_reviewer_provenance import section_digest

    _fm, body = _split_frontmatter(text.replace("\r\n", "\n"))
    raw = body.split("\n")
    lines = [line.rstrip() for line in raw]
    proposed = _guard()["proposed_change_heading"]
    fence = [False] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fence[i] = True
            inside = not inside
        elif inside:
            fence[i] = True
    out: List[ReplaceableSection] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if fence[i]:
            i += 1
            continue
        m_status = _STATUS_HEADING_RE.match(stripped)
        m_current = _CURRENT_HEADING_RE.match(stripped)
        if m_status is None and m_current is None \
                and not stripped.startswith(proposed):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and (fence[j] or not _SECTION_END_RE.match(lines[j])):
            j += 1
        if m_status is not None:
            kind, key = "status", m_status.group(1)
        elif m_current is not None:
            kind, key = "current", m_current.group(1)
            for k in range(i + 1, j):
                t = parse_trail(lines[k])
                if t is not None:
                    key = t.new
        else:
            kind = "proposed"
            key = stripped[len(proposed):].rstrip(") \t").strip()
        out.append(ReplaceableSection(i, j, key, stripped, kind,
                                      section_digest(raw[i:j])))
        i = j
    return out


def _is_ancestor(root: Path, commit: str, rev: str,
                 cache: Dict[str, bool]) -> bool:
    if commit not in cache:
        try:
            cache[commit] = _git(root, "merge-base", "--is-ancestor", commit,
                                 rev, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            cache[commit] = False
    return cache[commit]


def proven_sections(root: Path, rel: str, data: Optional[bytes],
                    rev: Optional[str], log: Optional[Log] = None
                    ) -> List[ReplaceableSection]:
    """The sections of node ``rel`` (bytes ``data``) a reviewer write may
    fold: those whose CURRENT normalized bytes hash-match a provenance
    ledger entry of this node (:mod:`vco_lib.kg_reviewer_provenance`) whose
    commit is an ancestor of ``rev``. Anything unproven — no ledger entry,
    an edited section, a rewritten commit, no ``rev`` — is absent, so it
    keeps the append-only rule. Never raises."""
    if not rev or data is None:
        return []
    from vco_lib import kg_reviewer_provenance as _prov

    entries = _prov.entries_for(root, rel, log)
    if not entries:
        return []
    commits: Dict[str, List[str]] = {}
    for e in entries:
        commits.setdefault(str(e["digest"]), []).append(str(e["commit"]))
    cache: Dict[str, bool] = {}
    return [s for s in reviewer_sections(_lf_text(data))
            if any(_is_ancestor(root, c, rev, cache)
                   for c in commits.get(s.digest, ()))]


def reviewer_originated_sections(base: Optional[str], new: str,
                                 base_proven: List[ReplaceableSection]
                                 ) -> List[ReplaceableSection]:
    """The reviewer sections of ``new`` (a node the reviewer is committing
    over ``base``, its bytes in the parent commit; ``None`` = a new node)
    whose EVERY non-blank line the reviewer originated — what
    :func:`commit_reviewer_changes` records in the provenance ledger.

    A line is FOREIGN when it carries a ``base`` line that was not inside a
    proven reviewer section: the position the guard's own in-order matcher
    maps that base line to, every new line equal to one, and every new line
    containing one of at least :data:`_FOREIGN_CONTAINS_MIN` characters (an
    owner line the reviewer annotated, quoted or enclosed under a new
    heading). A section holding even one foreign line is not recorded, so a
    reviewer edit can never launder the owner's text into foldable text."""
    new_body = _split_frontmatter(new.replace("\r\n", "\n"))[1]
    post = _body_lines(new_body)
    foreign: Set[int] = set()
    if base is not None:
        base_body = _split_frontmatter(base.replace("\r\n", "\n"))[1]
        pre = _body_lines(base_body)
        inside: Set[int] = set()
        for s in base_proven:
            inside.update(range(s.start, s.end))
        needles = [n for i, line in enumerate(pre) if i not in inside
                   for n in [_needle(line)] if n]
        collapsed = [_collapse(line) for line in post]
        j = 0
        for n in needles:
            k = j
            while k < len(collapsed) and n not in collapsed[k]:
                k += 1
            if k < len(collapsed):
                foreign.add(k)
                j = k + 1
        exact = set(needles)
        long_needles = [n for n in exact if len(n) >= _FOREIGN_CONTAINS_MIN]
        for k, line in enumerate(post):
            n = _needle(line)
            if n and (n in exact or any(f in collapsed[k] for f in long_needles)):
                foreign.add(k)
    return [s for s in reviewer_sections(new)
            if not any(k in foreign for k in range(s.start, s.end)
                       if post[k].strip())]


def _fold_removals(pre_lines: List[str], body_post: str,
                   sections: List[ReplaceableSection]) -> List[str]:
    """The lines of proven ``sections`` a write dropped WITHOUT carrying or
    naming them (review V02105-KGREV2 H3 — the owner's "old ones not
    superseded need to be kept", enforced per FACT):

    * a fact line survives when its text (marker aside, whitespace
      collapsed) is still a substring of a line of the new body — carried
      into the consolidated section, regrouped as the write likes;
    * a dropped fact is justified only by a trail line NEW in this write
      that names its section (``Superseded: <key> → …``) and quotes it
      (``… (<date>): <fact text>``);
    * a dropped section HEADING is justified by any new trail line naming
      the section's key (the step line)."""
    post_lines = _body_lines(body_post)
    post_collapsed = [_collapse(line) for line in post_lines]
    pre_set = set(pre_lines)
    trails = [t for line in post_lines if line not in pre_set
              for t in [parse_trail(line)] if t is not None]
    out: List[str] = []
    for s in sections:
        named = any(t.old == s.key for t in trails)
        facts = [t.fact for t in trails if t.old == s.key and t.fact]
        for i in range(s.start, min(s.end, len(pre_lines))):
            n = _needle(pre_lines[i])
            if not n or any(n in p for p in post_collapsed):
                continue
            if i == s.start and named:
                continue
            if i != s.start and any(n in f for f in facts):
                continue
            out.append(pre_lines[i].strip())
    return out


def edit_violation(pre: str, post: str,
                   replaceable: Optional[List[ReplaceableSection]] = None
                   ) -> Optional[str]:
    """Why an edit of an EXISTING node breaks a rule (``None`` = allowed).

    ``replaceable`` (v0.2.105) names this node's PROVEN reviewer sections
    (:func:`proven_sections`, the provenance ledger). Their lines are judged
    per fact by :func:`_fold_removals` — each must survive in the new body
    or be named by a new ``Superseded:`` trail line of its section — and
    every OTHER original line by the plain in-order append-and-annotate
    matcher, with the proven sections' lines left out of that matching (a
    fold moves their facts; the owner's lines around them must still
    survive in order). ``None`` / empty keeps the pre-v0.2.105 append-only
    rule for the whole body."""
    g = _guard()
    mutable = set(g["frontmatter_mutable_keys"])
    fm_pre_raw, body_pre = _split_frontmatter(pre)
    fm_post_raw, body_post = _split_frontmatter(post)
    fm_pre = _parse_fm(fm_pre_raw) if fm_pre_raw is not None else None
    if fm_pre_raw is None and fm_post_raw is not None:
        # A bare note gaining tags/status/provenance is fabricated metadata;
        # only the machine-written `updated:` stamp may appear.
        added = _parse_fm(fm_post_raw)
        if added is None or set(map(str, added)) - mutable:
            return "added YAML frontmatter to a node that had none"
    if fm_pre_raw is not None:
        if fm_post_raw is None:
            return "removed the node's YAML frontmatter"
        fm_post = _parse_fm(fm_post_raw)
        if fm_post is None:
            return "made the node's YAML frontmatter unparseable"
        if fm_pre is not None:
            a = {k: v for k, v in fm_pre.items() if k not in mutable}
            b = {k: v for k, v in fm_post.items() if k not in mutable}
            if a != b:
                changed = sorted(str(k) for k in set(a) | set(b)
                                 if a.get(k) != b.get(k))
                return ("rewrote the node's YAML frontmatter (keys: "
                        f"{', '.join(changed)})")
    locked = lock_reason(fm_pre, body_pre)
    if locked is None:
        sections = replaceable or []
        if sections:
            pre_lines = _body_lines(body_pre)
            inside: Set[int] = set()
            for s in sections:
                inside.update(range(s.start, s.end))
            masked = "\n".join("" if i in inside else line
                               for i, line in enumerate(pre_lines))
            removed = [line for _idx, line
                       in _missing_with_index(masked, body_post)]
            removed += _fold_removals(pre_lines, body_post, sections)
        else:
            removed = removed_lines(body_pre, body_post)
        if removed:
            shown = "; ".join(repr(_clip(line)) for line in removed[:3])
            more = f" (+{len(removed) - 3} more)" if len(removed) > 3 else ""
            return (f"removed or reworded {len(removed)} original line(s) - "
                    f"existing text is append-and-annotate only: "
                    f"{shown}{more}")
        return None
    base = body_pre.rstrip()
    if body_post == body_pre:
        return None  # only `updated:` moved
    if not body_post.startswith(base):
        return f"edited a locked node ({locked}) instead of appending a proposal"
    tail = body_post[len(base):].lstrip()
    heading = g["proposed_change_heading"]
    if not tail.startswith(heading) or not _DATE_HEADING_TAIL.match(tail[len(heading):]):
        return (f"edited a locked node ({locked}); only an appended "
                f"'{heading}<YYYY-MM-DD>)' section is allowed")
    return None


def _norm_title(title: object) -> str:
    return " ".join(str(title or "").split()).casefold()


def _node_title(text: str) -> str:
    """The normalised frontmatter ``title`` of a node ('' when none)."""
    raw, _body = _split_frontmatter(text)
    fm = _parse_fm(raw) if raw is not None else None
    return _norm_title(fm.get("title")) if fm else ""


_TITLE_TOKEN = re.compile(r"[0-9a-z]+")


def subject_tokens(text: str) -> Set[str]:
    """The content tokens of a title or filename slug, for the same-subject
    duplicate rule: lower-cased runs of letters/digits, a plural ``s``
    folded (length > 3, not ``ss``), single-character, all-digit (dates,
    counters) and ``[guard] title_stopwords`` tokens dropped."""
    stop = {w.casefold() for w in _guard()["title_stopwords"]}
    out: Set[str] = set()
    for tok in _TITLE_TOKEN.findall(text.casefold()):
        if len(tok) < 2 or tok.isdigit() or tok in stop:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return out


def _jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _slug(rel: str) -> str:
    return PurePosixPath(rel).stem


def body_tokens(text: str) -> Set[str]:
    """The content tokens of a node's BODY, for the same-subject rule's
    content-overlap confirmation: the frontmatter is dropped, then the body
    goes through :func:`subject_tokens` (lower-cased letter/digit runs,
    plurals folded, all-digit runs — dates, counters, versions — and
    ``[guard] title_stopwords`` dropped) and every token shorter than
    ``[guard] duplicate_body_min_token_len`` characters is dropped too. The
    body's own ``# Title`` heading counts like any other line."""
    _raw, body = _split_frontmatter(text.replace("\r\n", "\n"))
    min_len = int(_guard()["duplicate_body_min_token_len"])
    return {t for t in subject_tokens(body) if len(t) >= min_len}


def subject_similarity(title: str, rel: Optional[str], other_rel: str,
                       other_title: str) -> "Tuple[float, bool]":
    """``(score, identical)`` between a new node (``title`` at ``rel``) and
    an existing one: ``score`` is the highest Jaccard similarity of the
    :func:`subject_tokens` of title-vs-title, slug-vs-slug, title-vs-slug
    and slug-vs-title (an existing node with an empty title is still
    reached through its slug); ``identical`` is True when one of those
    pairs has exactly the same (non-empty) token set."""
    new_sets = [subject_tokens(title)]
    if rel:
        new_sets.append(subject_tokens(_slug(rel)))
    other_sets = [subject_tokens(other_title), subject_tokens(_slug(other_rel))]
    score = 0.0
    identical = False
    for a in new_sets:
        for b in other_sets:
            score = max(score, _jaccard(a, b))
            identical = identical or (bool(a) and a == b)
    return score, identical


def same_subject_node(title: str, rel: Optional[str], nodes: Dict[str, str],
                      *, body: Optional[Set[str]] = None,
                      bodies: Optional[Dict[str, Set[str]]] = None
                      ) -> "Optional[Tuple[str, float, str]]":
    """The existing node a new node with ``title`` at ``rel`` duplicates by
    subject, as ``(rel path, similarity, why)``, or ``None``.

    Trigger: :func:`subject_similarity` reaches ``[guard]
    duplicate_title_jaccard``. A trigger alone is NOT a duplicate (review
    R30 M2: one topic per node means "Cache layer eviction" is a legitimate
    subtopic of "Cache layer"); it is confirmed only when the titles/slugs
    carry exactly the same content tokens (a pure retitle), or the two
    BODIES (``body`` = the new node's :func:`body_tokens`, ``bodies`` = rel
    -> body tokens of the existing nodes) share at least ``[guard]
    duplicate_body_min_shared`` content tokens."""
    g = _guard()
    threshold = float(g["duplicate_title_jaccard"])
    min_shared = int(g["duplicate_body_min_shared"])
    best: Optional[Tuple[str, float, str]] = None
    for other, other_title in sorted(nodes.items()):
        if other == rel:
            continue
        score, identical = subject_similarity(title, rel, other, other_title)
        if score < threshold:
            continue
        shared = (body & bodies.get(other, set())
                  if body is not None and bodies is not None else set())
        if identical:
            why = "the same title/slug words"
        elif len(shared) >= min_shared:
            why = (f"bodies share {len(shared)} content tokens >= {min_shared}: "
                   f"{', '.join(sorted(shared)[:8])}")
        else:
            continue
        if best is None or score > best[1]:
            best = (other, score, why)
    return best


def new_node_violation(text: str, titles: Dict[str, str], *,
                       rel: Optional[str] = None,
                       nodes: Optional[Dict[str, str]] = None,
                       bodies: Optional[Dict[str, Set[str]]] = None
                       ) -> Optional[str]:
    """Why a NEW node breaks a rule (``None`` = allowed): it must carry
    every ``[guard] new_node_required_keys`` frontmatter key, non-empty, its
    title must not duplicate an existing node's, and (given ``nodes``, rel ->
    normalised title of every existing node, and ``bodies``, rel -> their
    :func:`body_tokens`) it must not be about the same subject as an
    existing node (:func:`same_subject_node`)."""
    raw, _body = _split_frontmatter(text)
    if raw is None:
        return "a new node without YAML frontmatter"
    fm = _parse_fm(raw)
    if fm is None:
        return "a new node with unparseable YAML frontmatter"
    missing = [k for k in _guard()["new_node_required_keys"]
               if fm.get(k) in (None, "", [], {})]
    if missing:
        return f"a new node missing frontmatter keys: {', '.join(missing)}"
    existing = titles.get(_norm_title(fm.get("title")))
    if existing:
        return (f"a new node duplicating the title of {existing} - extend "
                f"the existing node instead")
    if nodes:
        hit = same_subject_node(str(fm.get("title") or ""), rel, nodes,
                                body=body_tokens(text), bodies=bodies or {})
        if hit:
            return (f"a new node on the same subject as {hit[0]} (title/slug "
                    f"similarity {hit[1]:.2f} >= "
                    f"{float(_guard()['duplicate_title_jaccard']):.2f}, "
                    f"{hit[2]}) - extend {hit[0]} instead")
    return None


#: Tools whose RESULTS count as sources the reviewer read verbatim.
_SOURCE_TOOLS = frozenset({"Read", "Grep"})
_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")
_CLI_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9_-]*)")


def transcript_sources(stdout: str) -> str:
    """Everything the reviewer READ during the run: the text of every
    ``Read`` / ``Grep`` tool result in its transcript."""
    names: Dict[str, str] = {}
    chunks: List[str] = []
    for ev in _iter_events(stdout):
        message = ev.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[str(block.get("id") or "")] = str(block.get("name") or "")
            elif (block.get("type") == "tool_result"
                  and not block.get("is_error")
                  and names.get(str(block.get("tool_use_id") or "")) in _SOURCE_TOOLS):
                chunks.append(_result_text(block.get("content")))
    return "\n".join(chunks)


def _backticked_flags(text: str) -> Set[str]:
    return {flag for span in _BACKTICK_SPAN.findall(text)
            for flag in _CLI_FLAG.findall(span)}


def unsourced_flags(pre: Optional[str], post: str, sources: str) -> List[str]:
    """Backticked ``--flags`` the write ADDED (absent from ``pre``) that do
    not appear verbatim in anything the reviewer read (``sources``: its
    Read/Grep results plus the commit diff it was given)."""
    added = _backticked_flags(post) - (_backticked_flags(pre) if pre else set())
    return sorted(f for f in added
                  if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", sources))


#: Lexical shapes of a cited run/test id or commit hash on an evidence line
#: (documented in ``[guard] evidence_markers``'s comment): an 8+ digit stamp
#: with an optional ``-``/``_``/``T`` time part; the digit-bearing token after
#: run/test/build/job/pipeline — unless it is VERSION-shaped (``0.2.103``,
#: ``v1.4``: :data:`_VERSION_TOKEN`); a 7-40 char hex token mixing digits
#: and a-f.
_RUN_WORD_ID = re.compile(
    r"(?<![0-9A-Za-z])(?:run|test|build|job|pipeline)(?![0-9A-Za-z])"
    r"\s*(?:id(?![0-9A-Za-z])\s*)?[#:]?\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)
_STAMP_ID = re.compile(r"(?<![0-9A-Za-z])\d{8,}(?:[-_T]\d{4,}Z?)?(?![0-9A-Za-z])")
_VERSION_TOKEN = re.compile(r"[vV]?\d+(?:\.\d+)+")
_HEX_ID = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])"
                     r"[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")


def _word(term: str) -> "re.Pattern[str]":
    """``term`` as a whole word (letters/digits around it break the match),
    case-insensitive."""
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])",
                      re.IGNORECASE)


def evidence_ids(text: str) -> List[str]:
    """The run/test ids and commit hashes ``text`` cites, in order."""
    hits: List[Tuple[int, str]] = []
    for m in _RUN_WORD_ID.finditer(text):
        tok = m.group(1).rstrip(".-_")
        if any(c.isdigit() for c in tok) and not _VERSION_TOKEN.fullmatch(tok):
            hits.append((m.start(1), tok))
    hits += [(m.start(), m.group(0)) for m in _STAMP_ID.finditer(text)]
    hits += [(m.start(), m.group(0)) for m in _HEX_ID.finditer(text)]
    found = [tok for _pos, tok in sorted(hits)]
    out: List[str] = []
    for tok in found:  # de-duplicated, a token inside a longer one dropped
        if not any(tok != o and tok in o for o in found) and tok not in out:
            out.append(tok)
    return out


def _id_sourced(ident: str, sources: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ident):
        # A shortened sha matches the full one: left boundary only.
        return re.search(rf"(?<![0-9A-Za-z]){re.escape(ident)}", sources,
                         re.IGNORECASE) is not None
    return _word(ident).search(sources) is not None


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _judged_lines(text: str) -> List[str]:
    """The lines of a node the evidence rule judges: the body only (the
    YAML frontmatter is skipped — a ``title:`` is a name, not a claim) and
    no line inside, or opening/closing, a fenced code block (code is not a
    claim either).

    A fence that never closes does NOT hide the rest of the node (review
    R33 N3): the lines after an unclosed opener are judged as prose (any
    properly closed fence among them still skipped). KG retrieval reads the
    raw text, so a stray fence must not exempt every later line; judging
    them is not noisy because a line is only reverted for a marker used in a
    claim shape AND absent from everything the reviewer read. (Closing the
    fence at the next heading instead would still exempt a heading-less
    node's whole tail.)"""
    _raw, body = _split_frontmatter(text)
    lines = _body_lines(body)
    out: List[str] = []
    start = 0
    while start < len(lines):
        fence: Optional[str] = None
        opener = -1
        for i in range(start, len(lines)):
            line = lines[i]
            m = _FENCE.match(line)
            if m:
                if fence is None:
                    fence, opener = m.group(1), i
                elif m.group(1) == fence:
                    fence = None
                continue
            if fence is None:
                out.append(line)
        if fence is None:
            break
        # Unclosed: drop what was skipped as "inside" it and judge it.
        start = opener + 1
    return out


def evidence_claims(text: str, has_id: bool) -> List[str]:
    """The ``[guard] evidence_markers`` words ``text`` uses AS AN EVIDENCE
    CLAIM (review R30 M1), in config order. A whole-word marker is a claim
    when it is written UPPERCASE (a label: "CONFIRMED"), or is followed by
    one of ``[guard] evidence_attribution_words`` ("confirmed in game",
    "verified via the suite", "tested on staging"), or directly follows one
    of ``[guard] evidence_copula_words`` ("is verified", "has been tested";
    "is not tested" is not a claim), or directly follows one of ``[guard]
    evidence_label_words`` and a colon ("Status: confirmed", "**Result:**
    verified" — review R33 N2; "Status: not confirmed" is not a claim), or
    the line also cites a run/test id or commit hash (``has_id``). "The
    tested path keeps the cache warm" or a heading "## Validated
    configuration keys" is prose, not a claim.

    v0.2.105 (review D2.5 b): the copula arm requires the marker to be the
    PREDICATE — it must end its clause (:func:`_predicate_at`). "it was
    validated input" is a noun phrase whose "validated" modifies "input",
    not a claim about an evidence level, and it used to be reverted whenever
    the word was in nothing the reviewer read."""
    g = _guard()
    attrib = "|".join(re.escape(w) for w in g["evidence_attribution_words"])
    copula = "|".join(re.escape(w) for w in g["evidence_copula_words"])
    labels = "|".join(re.escape(w) for w in g["evidence_label_words"])
    after = re.compile(rf"\s+(?:{attrib})(?![0-9A-Za-z])", re.IGNORECASE)
    before = re.compile(rf"(?<![0-9A-Za-z])(?:{copula})\s+$", re.IGNORECASE)
    label = re.compile(rf"(?<![0-9A-Za-z])(?:`|\*\*|__)?(?:{labels})(?:`)?\s*(?:\*\*|__)?\s*:"
                       rf"\s*(?:\*\*|__|\*|_|`)?\s*$", re.IGNORECASE)
    out: List[str] = []
    for marker in g["evidence_markers"]:
        for m in _word(marker).finditer(text):
            head = text[:m.start()]
            if (has_id or m.group(0).isupper()
                    or after.match(text, m.end())
                    or (before.search(head) and _predicate_at(text, m.end()))
                    or label.search(head)):
                out.append(marker)
                break
    return out


def _predicate_at(text: str, end: int) -> bool:
    """True when a marker ending at ``end`` closes its clause — i.e. it is
    the predicate the copula before it asserts, not an adjective modifying a
    following noun ("it was validated input" is a noun phrase).

    "closes its clause" = the marker is followed by nothing, by punctuation,
    or by one of ``[guard] evidence_predicate_followers`` (a conjunction, a
    preposition a passive predicate takes — "verified against", "validated
    with", "confirmed under", "tested end-to-end" (review R1 M6) — a manner
    adverb, or a predicative adjective). Any OTHER following word makes the
    marker a modifier, which is how ordinary prose reads."""
    g = _guard()
    followers = {str(w).casefold() for w in g.get("evidence_predicate_followers") or ()}
    rest = text[end:].lstrip()
    if not rest or rest[0] in ".,;:!?)]}`*-–—":
        return True
    word = re.match(r"[A-Za-z]+", rest)
    if word is None:
        return True
    return word.group(0).casefold() in followers


def _marker_token_set() -> Set[str]:
    """The ``[guard] evidence_markers`` as comparison tokens — a marker word
    is never a claim's own subject word."""
    return {str(m).casefold() for m in _guard()["evidence_markers"]}


def _marker_passages(sources: str) -> List[str]:
    """The LINES of the read corpus that carry any evidence marker: the only
    lines a claim can be sourced from (v0.2.105, review D2.5 a). A 30-file
    corpus is mostly lines about other subjects; searching it whole let a
    marker found in an unrelated passage source any claim."""
    g = _guard()
    markers = [re.escape(str(m)) for m in g["evidence_markers"]]
    if not markers or not sources:
        return []
    any_marker = re.compile("|".join(markers), re.IGNORECASE)
    return [line for line in sources.splitlines() if any_marker.search(line)]


def _marker_sourced(marker: str, claim_text: str, passages: List[str]) -> bool:
    """True when ``marker`` is sourced by the SAME PASSAGE as the claim.

    v0.2.105 (review D2.5 a): the marker must occur on a corpus line that
    shares at least ``[guard] evidence_passage_min_shared_tokens`` content
    tokens with the claim line — the passage has to be about the claim's
    subject, not merely contain the word somewhere. A claim line with fewer
    distinctive tokens than the minimum cannot be tied to a passage at all
    (it has no subject of its own), so the word being present anywhere keeps
    it sourced; ``0`` restores the pre-v0.2.105 whole-corpus rule."""
    g = _guard()
    min_shared = int(g["evidence_passage_min_shared_tokens"])
    pattern = _word(marker)
    want = subject_tokens(claim_text) - _marker_token_set()
    if min_shared <= 0 or len(want) < min_shared:
        return any(pattern.search(line) for line in passages)
    for line in passages:
        if pattern.search(line) and len(want & subject_tokens(line)) >= min_shared:
            return True
    return False


def unsourced_evidence(pre: Optional[str], post: str, sources: str
                       ) -> "List[Tuple[str, List[str]]]":
    """Evidence-level labels the write ADDED without a source.

    For every judged line of ``post`` (:func:`_judged_lines`: body only, no
    fenced code) that is not an unchanged ``pre`` line, the text it added
    (an original line it extends in place — its text, list marker aside, as
    in :func:`removed_lines` — is cut out first) is checked for an evidence
    CLAIM (:func:`evidence_claims`: a ``[guard] evidence_markers`` word
    written uppercase, followed by an attribution word, directly after a
    copula, or on a line citing a run/test id or commit hash). Such a line must find that word in
    ``sources`` (what the reviewer read) and every run/test id or commit
    hash it cites (:func:`evidence_ids`) verbatim there too. Returns
    ``[(line, [missing evidence, ...]), ...]``; a pre-existing marker line
    is never judged."""
    pre_lines = [ln.strip() for ln in _body_lines(pre)] if pre else []
    pre_set = {ln for ln in pre_lines if ln}
    needles = sorted({_LINE_MARKER.sub("", ln, count=1).strip()
                      for ln in pre_set} - {""}, key=len, reverse=True)
    out: List[Tuple[str, List[str]]] = []
    passages = _marker_passages(sources)
    for line in _judged_lines(post):
        text = line.strip()
        if not text or text in pre_set:
            continue
        added = text
        for needle in needles:
            if needle in added:
                added = added.replace(needle, " \x00 ")
        ids = evidence_ids(added)
        hits = evidence_claims(added, has_id=bool(ids))
        if not hits:
            continue
        missing = [f"marker '{m}'" for m in hits
                   if not _marker_sourced(m, added, passages)]
        missing += [f"id '{i}'" for i in ids if not _id_sourced(i, sources)]
        if missing:
            out.append((text, missing))
    return out


def _link_target_cap() -> int:
    return int(_guard()["link_target_max_bytes"])


def _outside_backups_base() -> Path:
    """Where bytes from OUTSIDE the project are backed up: the user's VCO
    state dir, never the project tree (review R11 A2/A3 — foreign bytes
    must not become agent-readable, committable project content)."""
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / "backups" / "kg-reviewer-outside"


def _stamped_dir(base: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = base / stamp
    n = 1
    while d.exists():
        n += 1
        d = base / f"{stamp}-{n}"
    d.mkdir(parents=True)
    return d


class _Backups:
    """Every revert and every left-for-the-user file is copied aside FIRST,
    so no byte of the user's is ever lost to the guard:

    * a project path -> ``<project>/.claude/backups/kg-reviewer/<utc-stamp>/``
      (the backups tree is self-ignoring: :mod:`vco_lib.project_backups`);
    * a path outside the project (a ``knowledge/`` symlink's target) ->
      ``<VCO state dir>/backups/kg-reviewer-outside/<utc-stamp>/``.

    :meth:`save` returns ``(ok, where)``: ``ok`` is False when a copy was
    needed and could not be made — the caller must then NOT modify the file.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir: Optional[Path] = None
        self.outside_dir: Optional[Path] = None

    def save(self, key: Path) -> "Tuple[bool, Optional[Path]]":
        if not (key.is_file() or key.is_symlink()):
            return True, None  # nothing there: nothing to lose
        try:
            try:
                rel = key.relative_to(self.root).as_posix()
                if self.dir is None:
                    from vco_lib.project_backups import ensure_backups_dir

                    self.dir = _stamped_dir(ensure_backups_dir(self.root)
                                            / "kg-reviewer")
                dest = self.dir / rel
            except ValueError:
                if (not key.is_symlink()
                        and key.stat().st_size > _link_target_cap()):
                    return False, None  # too large to copy; never modified
                if self.outside_dir is None:
                    self.outside_dir = _stamped_dir(_outside_backups_base())
                dest = self.outside_dir / str(key).lstrip("/\\").replace(":", "")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if key.is_symlink():
                # A link is backed up as its LINK TEXT only — never followed
                # (review R15): restoring a link replaces the link, not its
                # target, so the target's bytes are never at risk here, and
                # following it would copy a target of any size (or one
                # outside the project) into the backups. A target the guard
                # does restore is backed up on its own, under the snapshot's
                # size cap (_restore_symlink).
                link_note = dest.with_name(dest.name + ".symlink")
                link_note.write_text(os.readlink(key), encoding="utf-8")
                return True, link_note
            shutil.copyfile(key, dest, follow_symlinks=False)
            return True, dest
        except OSError:
            return False, None


def _restore(key: Path, data: Optional[bytes]) -> None:
    if data is None:
        if key.is_file() or key.is_symlink():
            key.unlink()
        return
    if key.is_symlink():
        key.unlink()
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(data)


def _restore_symlink(snap: Snapshot, rel: str, log: Log, backups: _Backups,
                     res: ValidationResult, writes_on_link: List[WriteOp]) -> None:
    """A write through or over ``knowledge/<rel>``, a symlink at the start:
    out of bounds. Restore the link and, when the reviewer's bytes are what
    the target holds, the target too — never a target someone else edited."""
    key = snap.root / rel
    link_text = snap.symlinks[rel]
    reason = "wrote through or over a knowledge/ symlink"
    if not (key.is_symlink() and os.readlink(key) == link_text):
        ok, saved = backups.save(key)
        if not ok:
            log(f"REVERT-IMPOSSIBLE {rel}: {reason}, and its current bytes "
                f"could not be backed up - left as is, inspect it by hand")
            res.unrestorable.append((rel, reason))
            return
        if key.exists() or key.is_symlink():
            key.unlink()
        key.symlink_to(link_text)
        log(f"REVERTED {rel}: {reason} (the link is restored; backup: {saved})")
        res.reverted.append((rel, reason))
    target = key.resolve()
    if str(target) in snap.oversized_targets:
        # Never copied (review R11 A2): it cannot be restored, and nothing
        # written through this link is ever committed (the symlink branch
        # keeps nothing).
        log(f"REVERT-IMPOSSIBLE {target}: reached through {rel}, too large to "
            f"snapshot (> {_link_target_cap()} bytes) - not restorable; "
            f"inspect it by hand if the reviewer wrote to it")
        res.unrestorable.append((str(target), f"{reason}; target too large"))
        return
    copy = snap.link_targets.get(str(target))
    if copy is None:
        return
    pre = _read_bytes(copy)
    cur = _read_bytes(target)
    if _same(pre, cur):
        return
    exp = expected_bytes(pre, writes_on_link)
    if exp is UNKNOWN or not _same(exp if isinstance(exp, bytes) else None, cur):
        _ok, saved = backups.save(target)
        log(f"LEFT AS IS {target}: changed during the run by someone other "
            f"than the reviewer (backup: {saved}) - not reverted")
        res.left_for_user.append(str(target))
        return
    ok, saved = backups.save(target)
    if not ok:
        log(f"REVERT-IMPOSSIBLE {target}: {reason}, and its current bytes "
            f"could not be backed up - left as is")
        res.unrestorable.append((str(target), reason))
        return
    _restore(target, pre)
    log(f"REVERTED {target}: {reason} (its target; backup: {saved})")
    res.reverted.append((str(target), reason))


#: ``resync(rels, prune_hashes)``: re-sync the reverted ``knowledge/`` nodes
#: ``rels``; ``prune_hashes`` maps each reverted NEW node (its file is gone)
#: to the KG ``content_hash`` of the bytes the reviewer wrote there.
Resync = Callable[[List[str], Dict[str, str]], None]


def validate(snap: Snapshot, writes: "Optional[Dict[Path, List[WriteOp]]]",
             log: Log, resync: Optional[Resync] = None,
             *, sources: str,
             foldable: "Optional[Dict[str, List[ReplaceableSection]]]" = None
             ) -> ValidationResult:
    """Revert every rule-breaking change the reviewer PROVABLY made; return
    what survives. ``writes`` is :func:`attributed_writes`; ``None``
    (unattributable run) judges, reverts and keeps NOTHING. ``sources`` is
    what the reviewer read (:func:`transcript_sources` plus the commit diff,
    stat, sha and parent sha it was given): a backticked ``--flag``, an
    evidence-level word or a cited run/commit id a node gains must appear
    there.

    ``foldable`` (v0.2.105) is the section list the PROMPT showed the
    reviewer (:func:`vco_lib.kg_reviewer_prompt.foldable_sections`, the same
    ledger at the prompt's revision): when given, a fold is judged only
    against the sections that are BOTH listed there and still proven now
    (:func:`proven_sections` at the snapshot's HEAD) — the guard never
    allows more than the reviewer was told, and never anything unproven.
    ``None`` (a caller without a prompt) judges against the proven set."""
    res = ValidationResult()
    if writes is None:
        res.unattributed = True
        log("writes not attributable - nothing judged, reverted or committed "
            "(any knowledge/ change since the snapshot may be the user's own)")
        return res
    root = snap.root
    backups = _Backups(root)
    resynced: List[str] = []
    prune_hashes: Dict[str, str] = {}
    # The comparison set for the new-node rules: every node at the start
    # PLUS each new node this run keeps (review R30 L3 — a second node on a
    # subject the reviewer created moments earlier is a duplicate too).
    titles = dict(snap.titles)
    nodes = dict(snap.nodes)
    bodies = dict(snap.node_bodies)
    for key in sorted(writes):
        ops = writes[key]
        rel = snap.rel(key)
        if rel is not None and rel in snap.symlinks:
            _restore_symlink(snap, rel, log, backups, res, ops)
            continue
        under_knowledge = rel is not None and rel.startswith(KNOWLEDGE_DIR + "/")
        if under_knowledge and rel is not None and is_derived_sidecar(rel):
            continue
        if key.is_symlink():
            # A write named a symlink that is NOT a knowledge/ symlink from the
            # snapshot (those took the branch above). Restoring "its bytes"
            # would replace the link with a regular file, and its target's
            # pre-run bytes were never captured — so it is never modified and
            # never kept: logged for the user, the link text backed up.
            name = rel if rel is not None else str(key)
            _ok, saved = backups.save(key)
            reason = "a write through a symlink outside the snapshot"
            log(f"REVERT-IMPOSSIBLE {name}: {reason} (link text backed up: "
                f"{saved}) - inspect its target by hand")
            res.unrestorable.append((name, reason))
            continue
        known, pre = snap.pre_bytes_key(key)
        cur = _read_bytes(key)  # not a symlink here (handled above)
        if known and _same(pre, cur):
            continue
        name = rel if rel is not None else str(key)
        exp = expected_bytes(pre if known else UNKNOWN, ops)
        # A vanished file is never the reviewer's doing: none of its tools
        # deletes (no shell, no delete tool), and every write leaves bytes —
        # so a deletion is someone else's and falls in the branch below.
        owned = isinstance(exp, bytes) and _same(exp, cur)
        if not owned:
            _ok, saved = backups.save(key)
            log(f"LEFT AS IS {name}: its bytes cannot be proven to be the "
                f"reviewer's (changed or deleted during the run by someone "
                f"else, or its starting content is unknown; backup: {saved}) "
                f"- not reverted, not committed")
            res.left_for_user.append(name)
            continue
        reason: Optional[str] = None
        if rel is None:
            reason = "outside the project tree"
        elif not under_knowledge:
            reason = "outside knowledge/ (the reviewer edits KG nodes only)"
        elif not rel.endswith(".md"):
            reason = "not a KG node (only knowledge/**/*.md may be written)"
        elif cur is not None:
            post_text = _lf_text(cur)
            pre_text = _lf_text(pre) if pre is not None else None
            if pre_text is None:
                reason = new_node_violation(post_text, titles, rel=rel,
                                            nodes=nodes, bodies=bodies)
            else:
                reason = edit_violation(pre_text, post_text)
                if reason is not None and pre is not None:
                    # v0.2.105: the removals may sit in a PROVEN reviewer
                    # section (provenance ledger, computed only now that a
                    # write removed lines) and are re-judged per fact with
                    # it; nothing proven keeps the append-only verdict above.
                    replaceable = proven_sections(root, rel, pre,
                                                  snap.start_head, log)
                    if foldable is not None:
                        listed = {s.digest for s in foldable.get(rel, ())}
                        replaceable = [s for s in replaceable
                                       if s.digest in listed]
                    if replaceable:
                        reason = edit_violation(pre_text, post_text,
                                                replaceable=replaceable)
            if reason is None:
                bad = unsourced_flags(pre_text, post_text, sources)
                if bad:
                    reason = (f"wrote CLI flags found in no file it read: "
                              f"{', '.join(bad)}")
            if reason is None:
                inflated = unsourced_evidence(pre_text, post_text, sources)
                if inflated:
                    shown = "; ".join(
                        f"{_clip(line)!r} (missing: {', '.join(miss)})"
                        for line, miss in inflated[:3])
                    more = (f" (+{len(inflated) - 3} more)"
                            if len(inflated) > 3 else "")
                    reason = (f"raised an evidence/confidence label or cited a "
                              f"run/commit found in no file it read: "
                              f"{shown}{more}")
        if reason is None:
            assert rel is not None and cur is not None
            res.kept.append(rel)
            res.reviewer_bytes[rel] = cur
            if pre is None and rel.endswith(".md"):
                kept_text = _lf_text(cur)
                kept_title = _node_title(kept_text)
                nodes[rel] = kept_title
                bodies[rel] = body_tokens(kept_text)
                if kept_title:
                    titles.setdefault(kept_title, rel)
            continue
        if not known:
            _ok, saved = backups.save(key)
            log(f"REVERT-IMPOSSIBLE {name}: {reason} (its pre-run content is "
                f"unknown; backup: {saved}) - inspect it by hand")
            res.unrestorable.append((name, reason))
            continue
        ok, saved = backups.save(key)
        if not ok:
            log(f"REVERT-IMPOSSIBLE {name}: {reason}, and its current bytes "
                f"could not be backed up - left as is, not committed")
            res.unrestorable.append((name, reason))
            continue
        _restore(key, pre)
        log(f"REVERTED {name}: {reason}"
            + (f" (backup: {saved})" if saved else ""))
        res.reverted.append((name, reason))
        if under_knowledge and rel is not None and rel.endswith(".md"):
            resynced.append(rel)
            if pre is None and cur is not None:
                # A reverted NEW node: its file is gone now, but the project's
                # edit-time sync may have embedded these exact bytes. Their
                # content_hash (the sync's own signature) lets the prune
                # remove only those rows, even in the shared collection (R33 N1).
                try:
                    prune_hashes[rel] = content_signature_of_file_bytes(cur)
                except UnicodeDecodeError:
                    pass  # the sync could not have read them either
    res.backup_dir = backups.dir
    if resynced and resync is not None:
        resync(resynced, prune_hashes)
    return res


# ── commit isolation ─────────────────────────────────────────────────────


_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
                "rebase-merge", "rebase-apply", "BISECT_LOG")

#: The LONGEST a commit that found a git operation in progress waits for that
#: operation to END before it gives up and ledgers the files (v0.2.105
#: L-KGREV, brief item 4). The wait is also capped at what is left of the
#: run's own wall-clock budget (``retry_deadline``), so the retry never runs
#: past it: a review that used its whole budget gets no retry at all, and
#: the failure mode is the pre-v0.2.105 one (files left uncommitted) plus the
#: ledger line that names them.
GIT_OP_RETRY_S = 20.0
GIT_OP_POLL_S = 0.5


def _git_path(root: Path, name: str) -> Path:
    out = _git(root, "rev-parse", "--git-path", name).stdout.decode().strip()
    p = Path(out)
    return p if p.is_absolute() else root / p


def _head_bytes(root: Path, rel: str) -> "Tuple[bool, Optional[bytes]]":
    """``(known, bytes)`` of ``rel`` in the CURRENT commit (``None`` = not in
    it / no commit). The "is this path still a change?" test for a LEDGERED
    leftover, whose snapshot-time bytes ARE its current bytes."""
    proc = _git(root, "cat-file", "blob", f"HEAD:./{rel}")
    if proc.returncode != 0:
        return False, None
    return True, proc.stdout


def head_changed_paths(snap: Snapshot, end: str) -> "Optional[Set[str]]":
    """The PROJECT-relative paths ``snap.start_head..end`` changed, or
    ``None`` when that cannot be determined (no start commit, a git failure).

    Used by :func:`commit_block_reason` to decide whether a moved HEAD
    actually touched the reviewer's files (v0.2.105 L-KGREV, brief item 1):
    a commit that merged / rebased / landed while the review ran is not a
    reason to strand the nodes when none of them is in the diff. Paths
    outside the project are named ``<repo>/<path>`` so they can never equal
    a project-relative reviewer path."""
    start = snap.start_head
    if not start or not end:
        return None
    proc = _git(snap.root, "diff", "--name-only", "-z", f"{start}..{end}")
    if proc.returncode != 0:
        return None
    prefix = repo_prefix(snap.root)
    out: Set[str] = set()
    for top_rel in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not top_rel:
            continue
        rel = project_rel(top_rel, prefix)
        out.add(rel if rel is not None else f"<repo>/{top_rel}")
    return out


def is_git_op_block(reason: str) -> bool:
    """True for the two block reasons that name an IN-FLIGHT git operation
    (the ones :func:`commit_reviewer_changes` retries once, brief item 4)."""
    return (reason.startswith("a git operation is in progress")
            or reason.startswith("index.lock exists"))


def commit_block_reason(snap: Snapshot, paths: List[str],
                        attributed: bool,
                        own_leftover: "Optional[Set[str]]" = None
                        ) -> Optional[str]:
    """Why the reviewer must NOT commit now (``None`` = safe to commit).

    ``own_leftover`` (v0.2.105 L-KGREV) names the paths the ledger proves the
    reviewer itself wrote in an earlier skipped run: they are dirty in this
    run's snapshot too, but they are not the user's changes, so they do not
    block the commit.

    A HEAD that moved does not block by itself any more: the commit goes
    through when none of ``paths`` appears in ``start_head..HEAD``, so the
    reviewer's nodes land as a clean commit on top. When the moved HEAD DID
    touch one of them the skip stands, exactly as before."""
    root = snap.root
    if not attributed:
        return "the run's writes could not be attributed to the reviewer"
    for name in _IN_PROGRESS:
        if _git_path(root, name).exists():
            return f"a git operation is in progress ({name})"
    if _git_path(root, "index.lock").exists():
        return "index.lock exists (another git process is running)"
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    now = head.stdout.decode().strip() if head.returncode == 0 else None
    if now != snap.start_head:
        moved = f"HEAD moved since the review started ({snap.start_head} -> {now})"
        changed = head_changed_paths(snap, now) if now else None
        if changed is None:
            return moved
        overlap = sorted(set(paths) & changed)
        if overlap:
            return moved + (" and it changed " + ", ".join(overlap))
    own = own_leftover or set()
    user_touched = sorted(p for p in paths
                          if p in snap.dirty_before and p not in own)
    if user_touched:
        return ("the user already had uncommitted changes on "
                + ", ".join(user_touched))
    return None


def _retry_window(retry_deadline: Optional[float]) -> float:
    """Seconds the git-op retry may wait: :data:`GIT_OP_RETRY_S`, capped at
    what is left of the run's budget (``0`` when it is spent)."""
    if retry_deadline is None:
        return GIT_OP_RETRY_S
    return max(0.0, min(GIT_OP_RETRY_S, retry_deadline - time.monotonic()))


def _after_git_op(snap: Snapshot, paths: List[str], attributed: bool,
                  own: "Set[str]", window_s: float) -> Optional[str]:
    """Wait (bounded by ``window_s``) for an in-flight git operation to end,
    then judge the commit once more. Returns the block reason that still
    applies, or ``None`` when the operation ended and the commit may
    proceed."""
    deadline = time.monotonic() + window_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return commit_block_reason(snap, paths, attributed, own_leftover=own)
        time.sleep(min(GIT_OP_POLL_S, remaining))
        block = commit_block_reason(snap, paths, attributed, own_leftover=own)
        if block is None or not is_git_op_block(block):
            return block


def _foldable(snap: Snapshot, paths: List[str], own: "Set[str]") -> List[str]:
    """The paths whose current bytes are the reviewer's ALONE: clean when
    this run started, or dirty only with a verified leftover of its own. A
    path the user had changes on is ledgered too (so it is still named) but
    never as foldable -- a later run must not commit the user's edit."""
    return [p for p in paths if p not in snap.dirty_before or p in own]


def commit_reviewer_changes(snap: Snapshot, kept: List[str], *,
                            reviewer_bytes: Dict[str, bytes],
                            short: str, subject: str, model: str,
                            attributed: bool, log: Log,
                            retry_deadline: Optional[float] = None
                            ) -> Optional[str]:
    """Commit exactly ``kept`` -- each path only while it still holds the
    reviewer's bytes (``reviewer_bytes``) -- plus the reviewer's OWN
    leftovers from earlier skipped runs; return the new sha, or ``None``.

    v0.2.105 L-KGREV: a path the ledger
    (:mod:`vco_lib.kg_reviewer_pending`) proves the reviewer wrote in an
    earlier run and that still holds exactly those bytes is folded into this
    commit, so the field cascade ("once a file was left dirty every later
    review touching it skipped too") ends by itself. A ledgered path whose
    bytes changed is the user's: it is never committed here. Every path this
    call leaves uncommitted is recorded in the ledger with the sha the run
    proved, and the committed ones are dropped from it."""
    root = snap.root
    proofs: Dict[str, str] = {}
    paths: List[str] = []
    for p in sorted(set(kept)):
        cur = _read_bytes(root / p)
        if _same(cur, snap.pre_bytes(p)[1]):
            continue  # back to its starting bytes: not a change
        if p not in reviewer_bytes or not _same(cur, reviewer_bytes[p]):
            log(f"not committed: {p} changed after the reviewer wrote it "
                f"(someone else's edit) - left uncommitted")
            continue
        paths.append(p)
        if cur is not None:
            proofs[p] = _sha(cur)
    # The reviewer's own leftovers from an earlier skipped run: the ledger
    # holds the sha of the bytes it wrote, so their CURRENT bytes prove it.
    # Only a FOLDABLE entry whose bytes at the START of this run still hash
    # to the ledger is exempt from the "the user already had uncommitted
    # changes" block (that dirt was the reviewer's own -- the cascade this
    # fixes). A ledgered path the user edited since, or one ledgered while
    # the user had changes on it, is NOT exempt: committing it would commit
    # the user's edit under the reviewer's name. Entries whose file now
    # equals HEAD (committed or reverted by anybody) are dropped first.
    try:
        _pending.prune_settled(root, root, lambda rel: _head_bytes(root, rel))
        own: Set[str] = _pending.verified_own(
            root, lambda rel: snap.pre_bytes(rel)[1])
        leftovers = _pending.own_leftovers(root, root)
    except Exception:  # noqa: BLE001 -- the fold is a convenience, never a stop
        own, leftovers = set(), {}
    for p in sorted(leftovers):
        if p in paths:
            continue
        cur = _read_bytes(root / p)
        if cur is None:
            continue
        # A leftover EQUALS its own snapshot: what says "nothing to do" here
        # is whether the working tree still differs from HEAD, not whether it
        # differs from the run's start (it is the same dirty file).
        known, head_bytes = _head_bytes(root, p)
        if known and _same(cur, head_bytes):
            # Committed (or reverted) by someone since: nothing to fold and
            # nothing left to warn about.
            _pending.clear_paths(root, [p])
            continue
        paths.append(p)
        proofs[p] = _sha(cur)
    if not paths:
        log("reviewer wrote nothing to commit - no commit")
        return None
    block = commit_block_reason(snap, paths, attributed, own_leftover=own)
    window = _retry_window(retry_deadline)
    if block and is_git_op_block(block) and window > 0:
        log(f"commit deferred: {block}; retrying once it ends "
            f"(up to {window:.0f}s, within the run's budget)")
        block = _after_git_op(snap, paths, attributed, own, window)
    if block:
        log(f"commit skipped: {block}; reviewer files left uncommitted: "
            + ", ".join(paths))
        _pending.record_skips(root, root, {p: proofs[p] for p in paths
                                           if p in proofs},
                              reason=block, short=short,
                              foldable=_foldable(snap, paths, own))
        return None
    ignored = [p for p in paths
               if _git(root, "check-ignore", "-q", "--", p).returncode == 0]
    if ignored:
        log("not committed (git-ignored, not version-controlled here): "
            + ", ".join(ignored))
    paths = [p for p in paths if p not in ignored]
    # A git-ignored path can never be committed, so it must never keep
    # alerting either.
    _pending.clear_paths(root, ignored)
    if not paths:
        return None
    new = [p for p in paths
           if _git(root, "ls-files", "--error-unmatch", "--", p).returncode != 0]
    if new:
        added = _git(root, "add", "--", *new)
        if added.returncode != 0:
            detail = added.stderr.decode(errors="replace").strip()
            log(f"commit skipped: git add failed: {detail}")
            _pending.record_skips(root, root,
                                  {p: proofs[p] for p in paths if p in proofs},
                                  reason=f"git add failed: {detail}",
                                  short=short,
                                  foldable=_foldable(snap, paths, own))
            return None
    message = (f"{commit_subject(short, subject)}\n\n"
               f"KG nodes written by the post-commit KG reviewer ({model}) "
               f"for {short}.\n")
    try:
        proc = _git(root, "commit", "--only", "-q", "-m", message, "--", *paths,
                    timeout=300)
    except subprocess.TimeoutExpired:
        proc = None
    if proc is None or proc.returncode != 0:
        detail = ("timed out" if proc is None
                  else (proc.stderr or proc.stdout).decode(errors="replace").strip())
        if new:  # undo only OUR index additions; the worktree is untouched
            _git(root, "rm", "--cached", "-q", "--", *new)
        log(f"commit failed (files left uncommitted): {detail}")
        _pending.record_skips(root, root,
                              {p: proofs[p] for p in paths if p in proofs},
                              reason=f"commit failed: {detail}",
                              short=short,
                              foldable=_foldable(snap, paths, own))
        return None
    _pending.clear_paths(root, paths)
    sha = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    prefix = repo_prefix(root)
    files = set()
    for top_rel in _git(root, "diff-tree", "--no-commit-id", "--name-only",
                        "-r", "-z", sha).stdout.decode(
                            "utf-8", errors="surrogateescape").split("\0"):
        if top_rel:
            rel = project_rel(top_rel, prefix)
            # Outside the project: named from the repo toplevel.
            files.add(rel if rel is not None else f"<repo>/{top_rel}")
    extra = sorted(files - set(paths))
    if extra:
        log(f"WARNING: commit {sha[:12]} also contains {', '.join(extra)} "
            f"(added by a git hook?) - review it")
    log(f"committed {sha[:12]}: {', '.join(paths)}")
    record_provenance(root, sha, paths, log)
    return sha


def record_provenance(root: Path, sha: str, paths: List[str], log: Log
                      ) -> bool:
    """Ledger the reviewer sections commit ``sha`` (the reviewer's own,
    just made) holds in each of ``paths`` (v0.2.105, review V02105-KGREV2
    H1/H2/M1): every section :func:`reviewer_originated_sections` proves the
    reviewer wrote against the node's bytes in the parent commit — a new
    Status section, a consolidated Current-state section, an earlier proven
    section it left untouched (that one keeps its ORIGINAL commit) — with
    its digest, in :mod:`vco_lib.kg_reviewer_provenance`. Read from the
    COMMITTED blobs, so what is ledgered is exactly what is in history.
    ``False`` (logged) when it could not be recorded: those sections then
    stay append-only. Never raises."""
    from vco_lib import kg_reviewer_provenance as _prov

    parent_proc = _git(root, "rev-parse", "--verify", "--quiet", f"{sha}^")
    parent = (parent_proc.stdout.decode().strip()
              if parent_proc.returncode == 0 else None)
    out: Dict[str, List[Dict[str, str]]] = {}
    try:
        for p in paths:
            if not p.endswith(".md") or not p.startswith(KNOWLEDGE_DIR + "/"):
                continue
            blob = _git(root, "cat-file", "blob", f"{sha}:./{p}")
            if blob.returncode != 0:
                continue
            base_bytes: Optional[bytes] = None
            if parent:
                prev = _git(root, "cat-file", "blob", f"{parent}:./{p}")
                if prev.returncode == 0:
                    base_bytes = prev.stdout
            base_proven = proven_sections(root, p, base_bytes, parent, log)
            kept_commit: Dict[str, str] = {}
            for e in _prov.entries_for(root, p, log):
                if any(s.digest == e["digest"] for s in base_proven):
                    kept_commit.setdefault(str(e["digest"]), str(e["commit"]))
            secs = reviewer_originated_sections(
                _lf_text(base_bytes) if base_bytes is not None else None,
                _lf_text(blob.stdout), base_proven)
            out[p] = [{"digest": s.digest, "heading": s.heading,
                       "key": s.key, "kind": s.kind,
                       "keep_commit": kept_commit.get(s.digest, "")}
                      for s in secs]
    except Exception as exc:  # noqa: BLE001 — logged, the safe direction
        log(f"provenance NOT recorded for {sha[:12]} ({exc}): its reviewer "
            f"sections stay append-only (never foldable)")
        return False
    if not out:
        return True
    return _prov.record(root, sha, out, log)


def resync_with(project_root: Path, log: Log,
                timeout_s: int = 1800) -> Resync:
    """A ``validate(resync=...)`` callback: re-sync reverted nodes into the
    KG collection through the project's shipped sync script (soft-fail).

    A reverted NEW node no longer exists on disk, while the project's own
    edit-time sync may already have embedded the reviewer's write (review
    R30 M3): when any path is gone, the script runs with
    ``--prune-missing`` (plus ``--prune-hash <rel>=<content_hash>`` for each
    gone path validate hashed — what lets the orchestrator root, whose KG
    collection is the shared one, delete only the rows carrying the
    reviewer's exact bytes; R33 N1 — and, on a non-root project, what lets
    the sync delete a ``scope: shared`` node's rows from the SHARED
    collection under the same hash scoping; FINAL3 L-2). The log states,
    per gone path, what the script REPORTED (``vco_lib.kg_prune_result``):
    rows removed (per collection when more than one was touched), nothing
    matched, skipped and why, or failed — and "not confirmed" when the
    script printed no result, never an assumed removal. A failure (Weaviate
    down) is logged, never raised."""
    script = project_root / ".claude" / "scripts" / "sync_knowledge_graph.py"

    def _run(rels: List[str], prune_hashes: Optional[Dict[str, str]] = None
             ) -> None:
        hashes = prune_hashes or {}
        if not script.is_file():
            log(f"reverted nodes not re-synced ({script} is missing): "
                + ", ".join(rels))
            return
        gone: List[str] = []
        proc: "Optional[subprocess.CompletedProcess[str]]" = None
        failure = ""
        try:
            gone = [r for r in rels
                    if not (project_root / r).exists()
                    and not (project_root / r).is_symlink()]
            prune_args: List[str] = []
            if gone:
                prune_args.append("--prune-missing")
                for r in gone:
                    if r in hashes:
                        prune_args += ["--prune-hash", f"{r}={hashes[r]}"]
            proc = subprocess.run(
                [sys.executable, str(script), "--project-root",
                 str(project_root), *prune_args,
                 *[str(project_root / r) for r in rels]],
                cwd=project_root, capture_output=True, text=True,
                timeout=timeout_s, env=dict(os.environ),
                stdin=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001 — soft-fail, logged
            failure = str(exc) or type(exc).__name__
        results = kg_prune_result.parse_lines(proc.stdout if proc else "")
        removal = "; ".join(
            f"{r}: {kg_prune_result.describe(results.get(r))}" for r in gone)
        removal = f" [file gone - {removal}]" if removal else ""
        if proc is None:
            log(f"re-sync of reverted nodes failed: {failure}{removal}")
        elif proc.returncode != 0:
            log(f"re-sync of reverted nodes exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[-500:]}{removal}")
        else:
            log(f"re-sync of reverted nodes finished: {', '.join(rels)}"
                f"{removal}")
    return _run
