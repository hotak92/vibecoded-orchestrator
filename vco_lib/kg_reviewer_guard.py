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
forbids all of that; this module ENFORCES it, independent of the prompt:

1. :func:`take_snapshot` — before the run: a COPY of every ``knowledge/``
   file (derived sidecars excepted), the HEAD the run starts from, and the
   set of paths the user already had changes on (staged, unstaged or
   untracked; their bytes are copied aside too).
2. :func:`attributed_paths` — after the run: the paths the reviewer itself
   wrote, read from its ``--output-format stream-json`` transcript
   (Write / Edit / MultiEdit / NotebookEdit inputs and the
   ``store_knowledge_node`` input / result path). Attribution is what keeps
   the validator and the commit off the USER's concurrent edits: the review
   can run for minutes while the user keeps working in the same tree.
3. :func:`validate` — every attributed change that breaks a rule is REVERTED
   from the snapshot copy (never ``git checkout``; a path that was clean at
   the start is restored from the starting commit's blob, a path that did
   not exist is removed). Rules, each logged with path + reason:

   * a path outside ``knowledge/`` (or outside the project);
   * a non-node file under ``knowledge/`` (anything but ``*.md``; the
     derived sidecars in ``[guard] derived_sidecars`` are left alone);
   * a deleted node;
   * an existing node whose YAML frontmatter was removed or rewritten
     (adding/updating the keys in ``[guard] frontmatter_mutable_keys`` —
     ``updated`` — is allowed);
   * a LOCKED node (``[guard] locked_tags`` / ``locked_types`` /
     ``locked_statuses`` / ``locked_body_markers``) edited in any way other
     than appending one ``## Proposed change (kg-reviewer, <YYYY-MM-DD>)``
     section.

   A reverted node is re-synced to the KG collection (soft-fail) so the
   store does not keep the reviewer's version.
4. :func:`commit_reviewer_changes` — the surviving reviewer files, and ONLY
   those, are committed as ``kg-sync: <short-hash> <short subject>`` with a
   pathspec-limited ``git commit --only -- <paths>``, so the user's staged and
   unstaged work is never included. The commit is SKIPPED (files left in
   place, one log line) when a merge / rebase / cherry-pick / revert / bisect
   is in progress, ``index.lock`` exists, HEAD moved since the run started, a
   target path already had user changes before the run, or the run's writes
   could not be attributed. A run that wrote nothing commits nothing.

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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import yaml

from vco_lib.kg_reviewer_model import config

__all__ = [
    "Snapshot",
    "ValidationResult",
    "take_snapshot",
    "attributed_paths",
    "validate",
    "commit_reviewer_changes",
    "is_reviewer_commit_subject",
    "commit_subject",
]

Log = Callable[[str], None]

KNOWLEDGE_DIR = "knowledge"
#: Tool names whose input carries the path they write.
_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path",
                "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_STORE_TOOL_SUFFIX = "store_knowledge_node"
#: Total bytes of the user's dirty files outside knowledge/ copied aside.
_DIRTY_COPY_BUDGET = 64 * 1024 * 1024
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_DATE_HEADING_TAIL = re.compile(r"\d{4}-\d{2}-\d{2}\)")


def _guard() -> dict:
    return config()["guard"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _git(root: Path, *args: str, check: bool = False,
         input_bytes: Optional[bytes] = None,
         timeout: Optional[float] = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          input=input_bytes, check=check, timeout=timeout,
                          stdin=None if input_bytes is not None else subprocess.DEVNULL)


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


# ── snapshot ─────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    root: Path
    start_head: Optional[str]
    copy_dir: Path
    #: ``knowledge/...`` rel path -> sha256 of its bytes at the start.
    knowledge: Dict[str, str] = field(default_factory=dict)
    #: rel path -> sha256 (None = deleted) of every path the USER already
    #: had changes on at the start (staged, unstaged or untracked).
    dirty_before: Dict[str, Optional[str]] = field(default_factory=dict)
    #: dirty rel paths outside knowledge/ whose bytes were copied aside.
    dirty_copied: Set[str] = field(default_factory=set)

    def copy_of(self, rel: str) -> Path:
        return self.copy_dir / "files" / rel

    def pre_bytes(self, rel: str) -> "Tuple[bool, Optional[bytes]]":
        """``(known, bytes)`` — the path's content at the start (``None`` =
        absent). ``known`` is False when it cannot be determined (an ignored
        file outside knowledge/, a dirty file over the copy budget)."""
        if rel in self.knowledge or rel in self.dirty_copied:
            return True, _read_bytes(self.copy_of(rel))
        if rel.startswith(KNOWLEDGE_DIR + "/") and not is_derived_sidecar(rel):
            # Every knowledge file was copied: not in the copy = absent.
            return True, None
        if rel in self.dirty_before:
            if self.dirty_before[rel] is None:
                return True, None  # the user had deleted it
            return False, None
        if self.start_head:
            blob = _git(self.root, "cat-file", "blob", f"{self.start_head}:{rel}")
            if blob.returncode == 0:
                return True, blob.stdout
        ignored = _git(self.root, "check-ignore", "-q", "--", rel).returncode == 0
        if ignored:
            return False, None
        return True, None  # untracked, not ignored, not dirty -> did not exist

    def cleanup(self) -> None:
        shutil.rmtree(self.copy_dir, ignore_errors=True)


def _status_paths(root: Path) -> Dict[str, str]:
    """rel path -> porcelain XY code for every changed/untracked path."""
    proc = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        return {}
    out: Dict[str, str] = {}
    entries = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        out[path] = code
        if code[0] in "RC":  # rename/copy: the next field is the source
            if i < len(entries) and entries[i]:
                out[entries[i]] = code
            i += 1
    return out


def take_snapshot(root: Path) -> Snapshot:
    root = root.resolve()
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    start_head = head.stdout.decode().strip() if head.returncode == 0 else None
    snap = Snapshot(root=root, start_head=start_head,
                    copy_dir=Path(tempfile.mkdtemp(prefix="vco-kg-reviewer-")))
    kdir = root / KNOWLEDGE_DIR
    if kdir.is_dir():
        for path in kdir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if is_derived_sidecar(rel):
                continue
            data = path.read_bytes()
            dest = snap.copy_of(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            snap.knowledge[rel] = _sha(data)
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


def attributed_paths(stdout: str, root: Path) -> Optional[Set[Path]]:
    """The absolute paths the reviewer wrote, from its stream-json
    transcript. ``None`` when the transcript has no stream events at all
    (an older CLI, a crash): the writes cannot be attributed."""
    root = root.resolve()
    found: Set[Path] = set()
    store_ids: Set[str] = set()
    saw_stream = False

    def add(raw: object) -> None:
        if not isinstance(raw, str) or not raw.strip():
            return
        p = Path(raw.strip())
        found.add((p if p.is_absolute() else root / p).resolve())

    for ev in _iter_events(stdout):
        kind = ev.get("type")
        if kind not in ("assistant", "user", "system", "result"):
            continue
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
                if name in _WRITE_TOOLS:
                    add(tool_input.get(_WRITE_TOOLS[name]))
                elif name.endswith(_STORE_TOOL_SUFFIX):
                    store_ids.add(str(block.get("id") or ""))
                    raw = tool_input.get("file_path")
                    if isinstance(raw, str) and raw.strip():
                        rp = Path(raw.strip())
                        add(str(rp if rp.is_absolute() else root / rp))
            elif block.get("type") == "tool_result" and \
                    str(block.get("tool_use_id") or "") in store_ids:
                text = _result_text(block.get("content"))
                try:
                    parsed = json.loads(text)
                    add(parsed.get("absolute_path") if isinstance(parsed, dict) else None)
                except ValueError:
                    m = re.search(r'"absolute_path"\s*:\s*"([^"]+)"', text)
                    if m:
                        add(m.group(1))
    return found if saw_stream else None


# ── validation ───────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    #: knowledge/ node paths (rel) the reviewer changed that passed.
    kept: List[str] = field(default_factory=list)
    #: (rel or absolute path, reason) for every revert.
    reverted: List[Tuple[str, str]] = field(default_factory=list)
    #: paths a rule rejected but that could not be restored.
    unrestorable: List[Tuple[str, str]] = field(default_factory=list)


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


def edit_violation(pre: str, post: str) -> Optional[str]:
    """Why an edit of an EXISTING node breaks a rule (``None`` = allowed)."""
    g = _guard()
    fm_pre_raw, body_pre = _split_frontmatter(pre)
    fm_post_raw, body_post = _split_frontmatter(post)
    fm_pre = _parse_fm(fm_pre_raw) if fm_pre_raw is not None else None
    if fm_pre_raw is not None:
        if fm_post_raw is None:
            return "removed the node's YAML frontmatter"
        fm_post = _parse_fm(fm_post_raw)
        if fm_post is None:
            return "made the node's YAML frontmatter unparseable"
        if fm_pre is not None:
            mutable = set(g["frontmatter_mutable_keys"])
            a = {k: v for k, v in fm_pre.items() if k not in mutable}
            b = {k: v for k, v in fm_post.items() if k not in mutable}
            if a != b:
                changed = sorted(str(k) for k in set(a) | set(b)
                                 if a.get(k) != b.get(k))
                return ("rewrote the node's YAML frontmatter (keys: "
                        f"{', '.join(changed)})")
    locked = lock_reason(fm_pre, body_pre)
    if locked is None:
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


def _restore(snap: Snapshot, rel: str, data: Optional[bytes]) -> None:
    path = snap.root / rel
    if data is None:
        if path.is_file() or path.is_symlink():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _rel(path: Path, root: Path) -> Optional[str]:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def validate(snap: Snapshot, attributed: Optional[Set[Path]], log: Log,
             resync: Optional[Callable[[List[str]], None]] = None) -> ValidationResult:
    """Revert every rule-breaking reviewer change; return what survives.

    ``attributed`` None (unattributable run): every ``knowledge/`` change
    since the snapshot is judged as the reviewer's — the validator still
    reverts violations — and the caller skips the commit."""
    res = ValidationResult()
    root = snap.root
    if attributed is None:
        candidates: Set[Path] = {root / r for r in snap.knowledge}
        kdir = root / KNOWLEDGE_DIR
        if kdir.is_dir():
            candidates |= {p for p in kdir.rglob("*") if p.is_file()}
    else:
        candidates = set(attributed)
    resynced: List[str] = []
    for path in sorted(candidates):
        rel = _rel(path, root)
        if rel is None:
            reason = "outside the project tree"
            log(f"REVERT-IMPOSSIBLE {path}: {reason} (no pre-run copy) - "
                f"inspect it by hand")
            res.unrestorable.append((str(path), reason))
            continue
        under_knowledge = rel.startswith(KNOWLEDGE_DIR + "/")
        if under_knowledge and is_derived_sidecar(rel):
            continue
        known, pre = snap.pre_bytes(rel)
        post = _read_bytes(path)
        if known and pre == post:
            continue
        reason: Optional[str] = None
        if not under_knowledge:
            reason = "outside knowledge/ (the reviewer edits KG nodes only)"
        elif not rel.endswith(".md"):
            reason = "not a KG node (only knowledge/**/*.md may be written)"
        elif pre is not None and post is None:
            reason = "deleted a node"
        elif pre is not None and post is not None:
            reason = edit_violation(pre.decode("utf-8", errors="replace"),
                                    post.decode("utf-8", errors="replace"))
        if reason is None:
            res.kept.append(rel)
            continue
        if not known:
            log(f"REVERT-IMPOSSIBLE {rel}: {reason} (its pre-run content is "
                f"unknown) - inspect it by hand")
            res.unrestorable.append((rel, reason))
            continue
        _restore(snap, rel, pre)
        log(f"REVERTED {rel}: {reason}")
        res.reverted.append((rel, reason))
        if under_knowledge and rel.endswith(".md"):
            resynced.append(rel)
    if resynced and resync is not None:
        resync(resynced)
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
                            short: str, subject: str, model: str,
                            attributed: bool, log: Log) -> Optional[str]:
    """Commit exactly ``kept``; return the new commit sha, or ``None``."""
    root = snap.root
    # Re-check the bytes: only paths still differing from the start count.
    paths = sorted({p for p in kept
                    if _read_bytes(root / p) != snap.pre_bytes(p)[1]})
    if not paths:
        log("reviewer wrote nothing - no commit")
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
    files = set(_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
                     sha).stdout.decode().split())
    extra = sorted(files - set(paths))
    if extra:
        log(f"WARNING: commit {sha[:12]} also contains {', '.join(extra)} "
            f"(added by a git hook?) - review it")
    log(f"committed {sha[:12]}: {', '.join(paths)}")
    return sha


def resync_with(project_root: Path, log: Log,
                timeout_s: int = 1800) -> Callable[[List[str]], None]:
    """A ``validate(resync=...)`` callback: re-sync reverted nodes into the
    KG collection through the project's shipped sync script (soft-fail)."""
    script = project_root / ".claude" / "scripts" / "sync_knowledge_graph.py"

    def _run(rels: List[str]) -> None:
        if not script.is_file():
            log(f"reverted nodes not re-synced ({script} is missing): "
                + ", ".join(rels))
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--project-root",
                 str(project_root), *[str(project_root / r) for r in rels]],
                cwd=project_root, capture_output=True, text=True,
                timeout=timeout_s, env=dict(os.environ),
                stdin=subprocess.DEVNULL)
            if proc.returncode != 0:
                log(f"re-sync of reverted nodes exited {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout).strip()[-500:]}")
            else:
                log(f"re-synced reverted nodes: {', '.join(rels)}")
        except Exception as exc:  # noqa: BLE001 — soft-fail, logged
            log(f"re-sync of reverted nodes failed: {exc}")
    return _run
