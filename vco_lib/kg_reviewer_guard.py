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
     new line (:func:`removed_lines`); the log names the lost lines;
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
     is/are/was/were/be/been or after a ``[guard] evidence_label_words``
     label and colon such as "Status:", or on a line citing a run/test id
     or commit hash) that the reviewer read nowhere, or a
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
   started, a target path already had user changes before the run, or the
   run's writes could not be attributed. A run that wrote nothing commits
   nothing.

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
    "validate",
    "commit_reviewer_changes",
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


def removed_lines(body_pre: str, body_post: str) -> List[str]:
    """The non-blank ORIGINAL body lines that do not survive in the new body.

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
    missing: List[str] = []
    j = 0
    for line in _body_lines(body_pre):
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
            missing.append(line.strip())  # named as the user wrote it
    return missing


def edit_violation(pre: str, post: str) -> Optional[str]:
    """Why an edit of an EXISTING node breaks a rule (``None`` = allowed)."""
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
    configuration keys" is prose, not a claim."""
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
                    or before.search(head) or label.search(head)):
                out.append(marker)
                break
    return out


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
                   if not _word(m).search(sources)]
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
             *, sources: str) -> ValidationResult:
    """Revert every rule-breaking change the reviewer PROVABLY made; return
    what survives. ``writes`` is :func:`attributed_writes`; ``None``
    (unattributable run) judges, reverts and keeps NOTHING. ``sources`` is
    what the reviewer read (:func:`transcript_sources` plus the commit diff,
    stat, sha and parent sha it was given): a backticked ``--flag``, an
    evidence-level word or a cited run/commit id a node gains must appear
    there."""
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


def _git_path(root: Path, name: str) -> Path:
    out = _git(root, "rev-parse", "--git-path", name).stdout.decode().strip()
    p = Path(out)
    return p if p.is_absolute() else root / p


def commit_block_reason(snap: Snapshot, paths: List[str],
                        attributed: bool) -> Optional[str]:
    """Why the reviewer must NOT commit now (``None`` = safe to commit)."""
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
        return f"HEAD moved since the review started ({snap.start_head} -> {now})"
    user_touched = sorted(p for p in paths if p in snap.dirty_before)
    if user_touched:
        return ("the user already had uncommitted changes on "
                + ", ".join(user_touched))
    return None


def commit_reviewer_changes(snap: Snapshot, kept: List[str], *,
                            reviewer_bytes: Dict[str, bytes],
                            short: str, subject: str, model: str,
                            attributed: bool, log: Log) -> Optional[str]:
    """Commit exactly ``kept`` — each path only while it still holds the
    reviewer's bytes (``reviewer_bytes``); return the new sha, or ``None``."""
    root = snap.root
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
    if not paths:
        log("reviewer wrote nothing to commit - no commit")
        return None
    block = commit_block_reason(snap, paths, attributed)
    if block:
        log(f"commit skipped: {block}; reviewer files left uncommitted: "
            + ", ".join(paths))
        return None
    ignored = [p for p in paths
               if _git(root, "check-ignore", "-q", "--", p).returncode == 0]
    if ignored:
        log("not committed (git-ignored, not version-controlled here): "
            + ", ".join(ignored))
    paths = [p for p in paths if p not in ignored]
    if not paths:
        return None
    new = [p for p in paths
           if _git(root, "ls-files", "--error-unmatch", "--", p).returncode != 0]
    if new:
        added = _git(root, "add", "--", *new)
        if added.returncode != 0:
            log(f"commit skipped: git add failed: "
                f"{added.stderr.decode(errors='replace').strip()}")
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
        return None
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
    return sha


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
