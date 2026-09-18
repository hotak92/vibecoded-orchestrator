# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which files does this Bash command WRITE? (v0.2.95, lane F10)

Used by:

* ``templates/hooks/post-bash-file-sync.{sh,ps1}`` — the PostToolUse(Bash)
  hook that gives a CLI write the same KG / docs / code-graph routing an
  ``Edit``/``Write`` tool call gets through ``post-file-edit``. Until
  v0.2.95 a ``cat > knowledge/foo.md <<EOF`` reached Weaviate NEVER.
* ``templates/hooks/pre-bash-context-inject.{sh,ps1}`` — so the PRE-bash
  retrieval query is built from the TARGET PATH (module name + extension
  → language + ``--anchor``), exactly the way ``pre-edit-context-inject``
  builds it, instead of from the raw first 500 chars of the command.

The chain walk itself (``;`` / ``&&`` / ``|`` splitting, ``KEY=val``
prefixes, ``sudo``/``nice`` wrappers, ``bash -c "..."`` recursion) is
NOT reimplemented here — it is the shared
``vco_lib.bash_command_walk``, the same one
``vco_lib.diagram_delete_parser`` walks for DELETE targets.

What is recognised
------------------
* redirections — ``> f``, ``>> f``, ``>f``, ``2> f``, ``&> f``, ``>| f``
  (fd duplications like ``2>&1`` and ``/dev/null`` sinks are rejected);
* heredocs — ``cat > f <<EOF … EOF`` (the BODY is stripped before
  tokenising, so text inside it can never be mistaken for a redirect);
* ``tee [-a] f…``;
* ``sed -i`` / ``sed --in-place`` / ``perl -i -pe`` (script argument
  excluded, the rest are files);
* ``cp`` / ``mv`` / ``install`` destinations (including ``-t DIR`` and the
  ``cp a b DIR/`` shape, which expand to ``DIR/<basename>`` per source);
* ``touch f…``;
* ``dd of=f``;
* long ``--output <path>`` / ``--output=<path>`` / ``--outfile`` /
  ``--output-file`` flags — long forms ONLY. Short ``-o`` is deliberately
  NOT recognised: ``curl -o file`` writes a file but ``ssh -o Opt=val``
  does not, and a parser that cannot tell them apart would route an SSH
  option string as a file path;
* PowerShell ``Set-Content`` / ``Add-Content`` / ``Out-File`` /
  ``New-Item`` / ``Copy-Item`` ``-Path``/``-FilePath``/``-Destination``
  values (same cross-OS reasoning as the delete parser's ``Remove-Item``).

What it provably CANNOT recognise
---------------------------------
A write performed by an interpreter from its own source text —
``python - <<EOF`` … ``open(p, 'w')`` … ``EOF``, ``python -c "…"``,
``perl -e``, a ``patch``/``git checkout``/``rsync`` restore — is not
recoverable from the command string without executing it. Guessing there
would be strictly worse than knowing: an invented path routes the WRONG
file into the KG.

So those are handled by an explicit, bounded, *watermarked* fallback:
``scan_recent_writes`` walks ONLY ``knowledge/`` and ``docs/`` and returns
files whose mtime is newer than the last scan (capped at a 300 s lookback,
at most 32 results). ``should_fallback_scan`` gates it on THREE conditions,
all required: the parser found nothing, the command text shows a write at
all (``command_has_write_shape`` — a redirect, a heredoc, a write verb, an
interpreter, or one of the opaque writers named above), and that write
could plausibly have landed in the two scanned directories. The middle
condition is what keeps a read-only ``cat docs/x.md`` or
``grep -rn foo knowledge/`` from paying for a scan — and, before v0.2.95,
from re-routing files another tool had just written and synced.

Cost, stated so it cannot be read as more than it is: the walk itself is
sub-10 ms on a 1 000-file tree, but it is preceded by a Python interpreter
start (tens of ms), so the number that matters is "how often does this
run", not "how fast is the walk". That is what the gating above, and the
hook's pure-shell prefilter (which keeps ``ls`` / ``git status`` /
``pytest`` from reaching Python at all), are for.

Code files are NOT scanned: walking a whole repo on a Bash call is a real
cost with no bound, and a code file written that way is re-queued by the
next edit. That miss is documented in the hook header, per the lane brief.

Safety
------
``extract_write_targets`` is side-effect free except for the optional
``os.path.exists`` filter. The security boundary for the hook is
**containment + existence**: a resolved path must live under the project
root and must be a regular file that exists (the hook runs AFTER the
command). ``sed -i /etc/passwd`` therefore collects a candidate and is
then dropped, exactly like the delete parser's diagrams filter drops it.
"""
from __future__ import annotations

import os
import posixpath
import re
import time
from typing import List, Optional, Sequence, Tuple

from vco_lib.bash_command_walk import tokenize, walk_command

# --- Vocabulary ----------------------------------------------------------

# Verbs whose positional arguments are all written files.
_TEE_VERBS: frozenset[str] = frozenset({"tee"})
_TOUCH_VERBS: frozenset[str] = frozenset({"touch"})

# Verbs whose LAST positional is the destination (or `-t DIR`).
_COPY_VERBS: frozenset[str] = frozenset({"cp", "mv", "install"})

# In-place editors: only write when an in-place flag is present.
_INPLACE_VERBS: frozenset[str] = frozenset({"sed", "perl", "ruby"})

# PowerShell content writers (the Bash tool is POSIX-shaped even on
# Windows, but the .ps1 hook feeds this same parser and a user can run a
# pwsh one-liner through it).
_PS_WRITE_VERBS: frozenset[str] = frozenset({
    "set-content",
    "add-content",
    "out-file",
    "new-item",
    "copy-item",
    "tee-object",
})
_PS_PATH_FLAGS: frozenset[str] = frozenset({
    "-path", "-literalpath", "-filepath", "-destination", "-outfile",
})

# Long output flags. SHORT `-o` is deliberately absent — see module docstring.
_OUTPUT_FLAGS: frozenset[str] = frozenset({
    "--output", "--outfile", "--out-file", "--output-file",
})

# Flags that consume the NEXT token as a value, per verb family. Without
# this, `install -m 644 src dst` counts `644` as a positional and the
# destination computation shifts by one.
_VALUE_FLAGS_COPY: frozenset[str] = frozenset({
    "-m", "--mode", "-o", "--owner", "-g", "--group", "-S", "--suffix",
    "-t", "--target-directory",
})
_VALUE_FLAGS_TOUCH: frozenset[str] = frozenset({"-r", "--reference", "-d", "--date", "-t"})
_VALUE_FLAGS_SED: frozenset[str] = frozenset({"-e", "--expression", "-f", "--file", "-l"})

# Redirect operator shapes.
_REDIR_BARE_RE = re.compile(r"^(?:\d*|&)>{1,2}\|?$")
_REDIR_ATTACHED_RE = re.compile(r"^(?:\d*|&)>{1,2}\|?(?P<rest>.+)$")

# Heredoc opener: `<<EOF`, `<< EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`,
# `<<\EOF`. Deliberately does NOT match the `<<<` here-string (the third
# `<` is not a valid delimiter start).
_HEREDOC_RE = re.compile(
    r"<<-?\s*(?P<q>['\"]?)\\?(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
)

# Tokens that carry shell expansion / globbing are never usable as a
# literal path. `$VAR`, backticks, `*`, `?`, `[...]`, `{a,b}`.
_UNRESOLVABLE_CHARS: str = "$`*?[]{}"

# Sinks that are not project files.
_NON_FILE_PREFIXES: Tuple[str, ...] = ("/dev/", "/proc/", "/sys/")

# Hard cap on how many paths one command may route. A pathological
# command must not fan out into hundreds of Weaviate writes.
DEFAULT_LIMIT: int = 32

# Fallback-scan bounds.
_SCAN_SUBDIRS: Tuple[str, ...] = ("knowledge", "docs")
_SCAN_MAX_LOOKBACK_S: float = 300.0
_SCAN_FIRST_RUN_LOOKBACK_S: float = 60.0
_SCAN_MAX_FILES: int = 20000
_SCAN_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".git", "venv", ".venv", "target", "dist",
})

# Interpreter shapes whose writes we cannot read from the command text.
_OPAQUE_INTERPRETER_RE = re.compile(
    r"(?:^|[\s;&|(])(?:python3?|perl|ruby|node|php|Rscript)(?:\s|$)"
)

# The OTHER tools that write files the command text cannot express — the ones
# this module's "What it provably CANNOT recognise" paragraph names by hand:
# `patch -p1 < x.diff`, `git checkout -- f`, an rsync restore. Kept as a
# SEPARATE pattern from the interpreters because the two are different
# arguments: an interpreter is opaque because its program is data, these are
# opaque because their input is. Only the WRITING git subcommands are listed —
# `git diff docs/x.md` and `git log docs/` are reads and must stay cheap.
_OPAQUE_WRITER_RE = re.compile(
    r"(?:^|[\s;&|(])(?:patch|rsync)(?:\s|$)"
    r"|(?:^|[\s;&|(])git\s+(?:checkout|restore|apply|stash|reset)(?:\s|$)"
)

# Redirections and in-place/copy verbs, i.e. "the command text itself shows a
# write". Used ONLY to qualify a bare `knowledge/` / `docs/` mention; the
# authoritative extraction is still the tokenising parser above.
#
# MUST MATCH the prefilters in `templates/hooks/_lib/bash-write-targets.sh`
# (`vco_bash_write_prefilter`) and `.ps1` (`Test-VcoWriteSuspicious`) — those
# two decide whether this module is spawned at all, so a shape they reject can
# never reach these predicates. `tests/test_v0295_bash_write_sync.py` pins all
# three against one corpus.
_REDIRECT_NOISE = (
    "2>&1", "1>&2", ">&2", "&>/dev/null", "2>/dev/null",
    "1>/dev/null", ">/dev/null", "> /dev/null",
)
_WRITE_VERB_RE = re.compile(
    r"(?:^|[\s;&|(])(?:tee|cp|mv|touch|dd)(?:\s|$)"
    r"|sed\s+-i|sed\s+--in-place|perl\s+-i|ruby\s+-i"
    r"|--output\b|--outfile\b|--out-file\b|--output-file\b"
    r"|Set-Content|Add-Content|Out-File|Tee-Object"
)


def command_has_write_shape(command: str) -> bool:
    """Does the command TEXT show that something was written?

    True for a redirection (after `/dev/null` sinks and fd duplications are
    stripped — `2>&1` appears in a large fraction of all commands), a heredoc
    opener, one of the write verbs, an interpreter, or one of the opaque
    writers above. False for a command that merely NAMES a path.

    This is the predicate that stops `cat docs/x.md` from being treated as a
    write; see :func:`should_fallback_scan` for why that mattered.
    """
    if not command:
        return False
    stripped = command
    for noise in _REDIRECT_NOISE:
        stripped = stripped.replace(noise, "")
    if ">" in stripped or "<<" in command:
        return True
    if _WRITE_VERB_RE.search(command):
        return True
    if _OPAQUE_INTERPRETER_RE.search(command):
        return True
    return bool(_OPAQUE_WRITER_RE.search(command))

# Secret-shaped lines: a heredoc body matching this never becomes a
# retrieval query (it would put the value in argv, visible to `ps`).
_SECRETISH_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key)\s*[:=]"
)


# --- Heredoc stripping ---------------------------------------------------


def strip_heredocs(command: str) -> Tuple[str, List[str]]:
    """Remove heredoc BODIES from *command*, returning ``(command, bodies)``.

    Why this must happen before tokenising: the body of
    ``cat > f <<EOF`` is raw text. A body line like ``see foo > bar``
    would otherwise tokenise into a redirect whose "target" is ``bar`` —
    a file the command never touched.

    An opener whose terminator never appears is treated as literal text
    (no lines are consumed), so a quoted ``"a << b"`` cannot swallow the
    rest of the command.
    """
    if "<<" not in command:
        return command, []

    lines = command.split("\n")
    out: List[str] = []
    bodies: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        openers = [
            (m.group("delim"), "<<-" in m.group(0))
            for m in _HEREDOC_RE.finditer(line)
        ]
        i += 1
        if not openers:
            out.append(line)
            continue
        kept = _HEREDOC_RE.sub(" ", line)
        out.append(kept)
        for delim, dash in openers:
            end = _find_heredoc_terminator(lines, i, delim, dash)
            if end is None:
                # Unterminated: not a heredoc after all (quoted text).
                continue
            bodies.append("\n".join(lines[i:end]))
            i = end + 1
    return "\n".join(out), bodies


def _find_heredoc_terminator(
    lines: Sequence[str], start: int, delim: str, dash: bool
) -> Optional[int]:
    """Index of the line that closes a heredoc opened before *start*."""
    for j in range(start, len(lines)):
        probe = lines[j]
        if dash:
            probe = probe.lstrip("\t")
        if probe.strip() == delim:
            return j
    return None


# --- Candidate collection ------------------------------------------------


def _looks_like_path(tok: str) -> bool:
    """Reject anything that cannot be a literal path we may act on."""
    if not tok or tok.startswith("-"):
        return False
    if tok.startswith("&"):  # fd duplication remnant (`2>&1` → `&1`)
        return False
    if any(c in tok for c in _UNRESOLVABLE_CHARS):
        return False
    if "://" in tok:
        return False
    if tok.startswith(_NON_FILE_PREFIXES):
        return False
    if tok in (".", "..", "/"):
        return False
    return True


class _WriteTargetCollector:
    """Stateful per-segment collector handed to ``walk_command``.

    Stateful because ``cd`` matters: in ``cd /tmp && cat > knowledge/x.md``
    the redirect target is ``/tmp/knowledge/x.md``, NOT a project KG node.
    Segments arrive in source order, so a running prefix is enough.
    ``cd`` with no argument / ``cd -`` / ``cd "$D"`` makes the prefix
    UNKNOWN and every later RELATIVE target is dropped — doing nothing
    beats guessing.
    """

    def __init__(self) -> None:
        self.cwd: Optional[str] = ""  # "" = the hook's project root; None = unknown

    # -- helpers --
    def _resolve(self, tok: str) -> Optional[str]:
        if not _looks_like_path(tok):
            return None
        if posixpath.isabs(tok):
            return posixpath.normpath(tok)
        if self.cwd is None:
            return None
        joined = posixpath.join(self.cwd, tok) if self.cwd else tok
        return posixpath.normpath(joined)

    def _track_cd(self, tokens: List[str]) -> None:
        args = [t for t in tokens[1:] if not t.startswith("-")]
        if len(args) != 1 or not _looks_like_path(args[0]):
            self.cwd = None
            return
        target = args[0]
        if posixpath.isabs(target):
            self.cwd = posixpath.normpath(target)
        elif self.cwd is None:
            return
        else:
            self.cwd = posixpath.normpath(posixpath.join(self.cwd, target))

    # -- the collector --
    def __call__(self, tokens: List[str]) -> List[str]:
        tokens = _strip_group_punctuation(tokens)
        if not tokens:
            return []

        found: List[str] = []
        redirect_idx: set[int] = set()

        # 1. Redirections — verb-independent, so scan every token.
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _REDIR_BARE_RE.match(tok):
                redirect_idx.add(i)
                if i + 1 < len(tokens):
                    redirect_idx.add(i + 1)
                    resolved = self._resolve(tokens[i + 1])
                    if resolved:
                        found.append(resolved)
                    i += 2
                    continue
            else:
                m = _REDIR_ATTACHED_RE.match(tok)
                if m:
                    redirect_idx.add(i)
                    resolved = self._resolve(m.group("rest"))
                    if resolved:
                        found.append(resolved)
            i += 1

        # 2. Long --output flags — verb-independent and unambiguous.
        for idx, tok in enumerate(tokens):
            low = tok.lower()
            if "=" in low and low.split("=", 1)[0] in _OUTPUT_FLAGS:
                resolved = self._resolve(tok.split("=", 1)[1])
                if resolved:
                    found.append(resolved)
            elif low in _OUTPUT_FLAGS and idx + 1 < len(tokens):
                resolved = self._resolve(tokens[idx + 1])
                if resolved:
                    found.append(resolved)

        verb = os.path.basename(tokens[0]).lower()
        rest = [t for idx, t in enumerate(tokens) if idx not in redirect_idx][1:]

        if verb == "cd":
            self._track_cd(tokens)
            return found
        if verb in ("pushd", "popd"):
            self.cwd = None
            return found

        if verb in _TEE_VERBS:
            found.extend(self._positionals(rest, frozenset()))
        elif verb in _TOUCH_VERBS:
            found.extend(self._positionals(rest, _VALUE_FLAGS_TOUCH))
        elif verb in _COPY_VERBS:
            found.extend(self._copy_destinations(verb, rest))
        elif verb in _INPLACE_VERBS:
            found.extend(self._inplace_files(rest))
        elif verb == "dd":
            for tok in rest:
                if tok.lower().startswith("of="):
                    resolved = self._resolve(tok[3:])
                    if resolved:
                        found.append(resolved)
        elif verb in _PS_WRITE_VERBS:
            found.extend(self._powershell_paths(rest))

        return found

    def _positionals(self, rest: List[str], value_flags: frozenset) -> List[str]:
        out: List[str] = []
        skip_next = False
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                base = tok.split("=", 1)[0].lower()
                if base in value_flags and "=" not in tok:
                    skip_next = True
                continue
            resolved = self._resolve(tok)
            if resolved:
                out.append(resolved)
        return out

    def _raw_positionals(self, rest: List[str], value_flags: frozenset) -> List[str]:
        out: List[str] = []
        skip_next = False
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                base = tok.split("=", 1)[0].lower()
                if base in value_flags and "=" not in tok:
                    skip_next = True
                continue
            out.append(tok)
        return out

    def _copy_destinations(self, verb: str, rest: List[str]) -> List[str]:
        # `install -d a b` only creates directories — no file is written.
        if any(t == "-d" or t == "--directory" for t in rest):
            return []
        # `-t DIR` / `--target-directory=DIR`: every positional is a source.
        target_dir: Optional[str] = None
        for idx, tok in enumerate(rest):
            low = tok.lower()
            if low in ("-t", "--target-directory") and idx + 1 < len(rest):
                target_dir = rest[idx + 1]
            elif low.startswith("--target-directory="):
                target_dir = tok.split("=", 1)[1]
        args = self._raw_positionals(rest, _VALUE_FLAGS_COPY)
        if target_dir is not None:
            return self._expand_into_dir(target_dir, args)
        if len(args) < 2:
            return []
        dest, sources = args[-1], args[:-1]
        if dest.endswith("/") or len(sources) > 1:
            return self._expand_into_dir(dest, sources)
        resolved = self._resolve(dest)
        return [resolved] if resolved else []

    def _expand_into_dir(self, dest_dir: str, sources: List[str]) -> List[str]:
        out: List[str] = []
        for src in sources:
            if not _looks_like_path(src):
                continue
            resolved = self._resolve(posixpath.join(dest_dir, posixpath.basename(src)))
            if resolved:
                out.append(resolved)
        return out

    def _inplace_files(self, rest: List[str]) -> List[str]:
        in_place = False
        has_script_flag = False
        for tok in rest:
            low = tok.lower()
            if low == "--in-place" or low.startswith("--in-place="):
                in_place = True
            elif re.match(r"^-[A-Za-z]*i", tok):
                in_place = True
            if low.split("=", 1)[0] in ("-e", "--expression", "-f", "--file"):
                # An explicit script flag means EVERY positional is a file.
                has_script_flag = True
        if not in_place:
            return []
        args = [a for a in self._raw_positionals(rest, _VALUE_FLAGS_SED) if a != ""]
        if not has_script_flag and args:
            # The first positional is the sed/perl program, not a file.
            args = args[1:]
        out: List[str] = []
        for tok in args:
            resolved = self._resolve(tok)
            if resolved:
                out.append(resolved)
        return out

    def _powershell_paths(self, rest: List[str]) -> List[str]:
        out: List[str] = []
        for idx, tok in enumerate(rest):
            low = tok.lower()
            if low in _PS_PATH_FLAGS and idx + 1 < len(rest):
                resolved = self._resolve(rest[idx + 1])
                if resolved:
                    out.append(resolved)
            elif "=" in low and low.split("=", 1)[0] in _PS_PATH_FLAGS:
                resolved = self._resolve(tok.split("=", 1)[1])
                if resolved:
                    out.append(resolved)
        return out


def _strip_group_punctuation(tokens: List[str]) -> List[str]:
    """Trim subshell/group punctuation that shlex leaves glued to tokens.

    ``(cd /tmp && cat > f)`` tokenises as ``['(cd', '/tmp', '&&', 'cat',
    '>', 'f)']``. Without this the verb is ``(cd`` and the target is
    ``f)``.
    """
    if not tokens:
        return tokens
    out = list(tokens)
    out[0] = out[0].lstrip("({")
    out[-1] = out[-1].rstrip(")};")
    return [t for t in out if t]


# --- Public API ----------------------------------------------------------


def collect_candidates(command: str) -> List[str]:
    """Parse *command* and return raw candidate paths (no filesystem access).

    Paths are either absolute or relative to the shell's starting
    directory. Order is source order; duplicates are collapsed.
    """
    if not command or not command.strip():
        return []
    stripped, _bodies = strip_heredocs(command)
    collector = _WriteTargetCollector()
    lines = stripped.split("\n")
    if len(lines) > 1 and all(tokenize(line) is not None for line in lines):
        # shlex treats a newline as ordinary whitespace, so a multi-line
        # script would otherwise collapse into ONE segment and only its
        # first verb would be classified. Walk line by line, sharing the
        # collector so `cd` state still flows forward.
        raw: List[str] = []
        for line in lines:
            raw.extend(walk_command(line, collector))
    else:
        raw = walk_command(stripped, collector)
    seen: set[str] = set()
    out: List[str] = []
    for p in raw:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def extract_write_targets(
    command: str,
    project_root: Optional[str] = None,
    require_exists: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> List[str]:
    """Absolute paths *command* writes, constrained to *project_root*.

    ``require_exists=True`` (the PostToolUse hook) additionally demands
    that the path is a regular file that exists — the command has already
    run, so a candidate that isn't on disk was a parse artefact.
    ``require_exists=False`` (the PreToolUse hook) keeps candidates that
    are about to be created.
    """
    candidates = collect_candidates(command)
    if not candidates:
        return []
    root = os.path.realpath(project_root) if project_root else None
    out: List[str] = []
    for cand in candidates:
        if os.path.isabs(cand):
            abs_path = os.path.normpath(cand)
        elif root:
            abs_path = os.path.normpath(os.path.join(root, cand))
        else:
            continue
        if root and not _is_within(abs_path, root):
            continue
        if require_exists and not os.path.isfile(abs_path):
            continue
        if abs_path not in out:
            out.append(abs_path)
        if len(out) >= limit:
            break
    return out


def _is_within(path: str, root: str) -> bool:
    """True when *path* is inside *root* (no symlink resolution of path:
    the file may not exist yet on the PreToolUse surface)."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


# --- Fallback scan (the unparseable-write case) --------------------------


def should_fallback_scan(command: str, targets: Sequence[str]) -> bool:
    """Should we pay for an mtime scan of ``knowledge/`` + ``docs/``?

    Three conditions, ALL required:

    1. the parser found nothing (a recovered target is always better than a
       scan — it names the file instead of guessing from mtimes);
    2. the command text shows a write at all
       (:func:`command_has_write_shape`);
    3. and that write could have landed in the two directories we scan —
       either because the command NAMES one of them, or because it has a
       heredoc / interpreter shape whose target is unknowable.

    Condition 2 is the v0.2.95 review fix (MAJOR-3). Until then a bare
    mention of ``knowledge/`` or ``docs/`` was sufficient, so a READ-ONLY
    ``cat docs/x.md`` / ``grep -rn foo knowledge/`` / ``ls docs/`` spawned
    this module, ran the walk, and re-routed every knowledge/docs file
    modified in the last 300 s — including files the Edit tool had written
    and already synced seconds earlier, costing a second debounced
    ``kg-sync`` (a content-hash no-op at Weaviate, but a Python + weaviate
    process each). The hook header promised the scan ran only for "an
    opaque-write shape"; it now does.
    """
    if targets:
        return False
    if not command:
        return False
    if not command_has_write_shape(command):
        return False
    if "knowledge/" in command or "docs/" in command:
        return True
    if "<<" in command:
        return True
    return bool(
        _OPAQUE_INTERPRETER_RE.search(command)
        or _OPAQUE_WRITER_RE.search(command)
    )


def scan_recent_writes(
    project_root: str,
    since_ts: float,
    limit: int = DEFAULT_LIMIT,
    subdirs: Sequence[str] = _SCAN_SUBDIRS,
) -> List[str]:
    """Files under *subdirs* of *project_root* modified after *since_ts*.

    Bounded three ways: only two directories, at most ``_SCAN_MAX_FILES``
    stat calls, at most *limit* results. Hidden directories and the usual
    build/vendor trees are skipped. ~3 ms for a 1 134-file tree.
    """
    out: List[str] = []
    visited = 0
    for sub in subdirs:
        root = os.path.join(project_root, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in _SCAN_SKIP_DIRS
            ]
            for name in filenames:
                if name.startswith("."):
                    continue
                visited += 1
                if visited > _SCAN_MAX_FILES:
                    return out
                path = os.path.join(dirpath, name)
                try:
                    if os.stat(path).st_mtime > since_ts:
                        out.append(path)
                        if len(out) >= limit:
                            return out
                except OSError:
                    continue
    return out


def read_scan_watermark(state_file: str, now: Optional[float] = None) -> float:
    """Lower bound for the fallback scan's mtime window.

    First ever run → ``now - 60`` (catch the write that just happened, NOT
    the whole knowledge tree). Otherwise the last scan's timestamp, floored
    at ``now - 300`` so a project left idle for a week cannot mass-sync.
    """
    now = time.time() if now is None else now
    try:
        with open(state_file, "r", encoding="utf-8") as fh:
            mark = float(fh.read().strip())
    except (OSError, ValueError):
        return now - _SCAN_FIRST_RUN_LOOKBACK_S
    return max(mark, now - _SCAN_MAX_LOOKBACK_S)


def write_scan_watermark(state_file: str, value: Optional[float] = None) -> None:
    """Record the scan time. Soft-fail: a read-only state dir just means
    the next scan uses the 300 s floor."""
    value = time.time() if value is None else value
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as fh:
            fh.write("%d" % int(value))
    except OSError:
        pass


# --- Pre-bash query shaping ---------------------------------------------


def prebash_query_parts(
    command: str, project_root: Optional[str] = None
) -> Tuple[str, str]:
    """``(target_path, content_snippet)`` for the PRE-bash retrieval query.

    The target is the first write target the parser finds (preferring one
    under ``knowledge/`` / ``docs/``, then a code file). The snippet is the
    first heredoc body — the closest analogue to ``pre-edit``'s
    ``new_string`` — but ONLY when the target is a knowledge/docs file and
    the body carries no secret-shaped line, because the snippet ends up in
    the retrieval subprocess's argv where ``ps`` can read it.
    """
    targets = extract_write_targets(
        command, project_root=project_root, require_exists=False
    )
    if not targets:
        return "", ""
    target = _preferred_target(targets, project_root)
    snippet = ""
    rel = os.path.relpath(target, project_root) if project_root else target
    rel_posix = rel.replace(os.sep, "/")
    if rel_posix.startswith(("knowledge/", "docs/")):
        _stripped, bodies = strip_heredocs(command)
        if bodies and not _SECRETISH_RE.search(bodies[0]):
            snippet = " ".join(bodies[0].split())[:200]
    return target, snippet


def _preferred_target(targets: List[str], project_root: Optional[str]) -> str:
    if project_root:
        for want in ("knowledge/", "docs/"):
            for t in targets:
                rel = os.path.relpath(t, project_root).replace(os.sep, "/")
                if rel.startswith(want):
                    return t
    return targets[0]


# --- CLI -----------------------------------------------------------------


def _cli_main(argv: Optional[List[str]] = None) -> int:
    """Stdin → stdout, the same shape ``diagram_delete_parser`` uses so a
    shell hook can pipe the command in with no quoting games.

    ``--format paths``   one absolute path per line (PostToolUse).
    ``--format prebash`` line 1 = target path (may be empty),
                         line 2 = content snippet (may be empty).

    Always exits 0 — empty stdout is a valid "nothing to route" signal.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="vco_lib.bash_write_targets")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--format", choices=("paths", "prebash"), default="paths")
    parser.add_argument("--require-exists", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--scan-state",
        default="",
        help="watermark file; enables the bounded knowledge/docs mtime fallback",
    )
    args = parser.parse_args(argv)

    try:
        command = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return 0

    root = args.project_root or None

    if args.format == "prebash":
        target, snippet = prebash_query_parts(command, project_root=root)
        sys.stdout.write(target + "\n")
        sys.stdout.write(snippet.replace("\n", " ") + "\n")
        return 0

    targets = extract_write_targets(
        command,
        project_root=root,
        require_exists=args.require_exists,
        limit=args.limit,
    )
    if args.scan_state and root and should_fallback_scan(command, targets):
        since = read_scan_watermark(args.scan_state)
        targets = scan_recent_writes(root, since, limit=args.limit)
        write_scan_watermark(args.scan_state)
    for path in targets:
        sys.stdout.write(path + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
