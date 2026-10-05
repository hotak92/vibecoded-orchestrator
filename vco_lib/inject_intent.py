# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-A — the PURE injection-intent core (PLAN-V02101 §2, §3 WP-A).

One Python home shared by every injection surface (the
``hook_context_router.py`` runner), the ``rl_kg_search.py`` injection-profile
gates, and the offline replay evaluator (WP-E). Pure stdlib: NO weaviate /
network imports here, so every function is unit-testable in isolation.

What lives here
---------------
* **A1 Bash intent classification** — :func:`classify_bash` maps a Bash tool
  command to READ / EDIT / SEARCH / MECHANICAL plus its target paths,
  symbols, write snippet and ``git show <rev>:<path>`` revision pins. The
  classifier NEVER uses command text as an embedding query (owner rule,
  plan-v0300 lines 118-123): the query is always built from the target
  path/symbols.
* **A1 gate ports** — :func:`pattern_gate`, :func:`bash_gate`,
  :func:`extract_symbol` are the Python port of
  ``templates/hooks/_lib/codegraph-query.sh``'s gates. This module is the
  ONE HOME; the shell/PowerShell copies remain ONLY as the legacy hooks'
  code path until Wave 2 retires them with their callers, and
  ``tests/test_v02101_inject_intent_classifier.py`` pins them against each
  other (drift is a finding — plan §3 WP-A1).
* **A1 EDIT parser reuse** — write targets / heredoc snippets come from
  :mod:`vco_lib.bash_write_targets` (already the one home the
  ``_lib/bash-write-targets.{sh,ps1}`` delegators call). No second parser.
* **A2 symbol extraction** — :func:`edit_enclosing_symbols`,
  :func:`file_pub_symbols`, :func:`agent_task_section`.
* **§2.1 noise-gate table** — the per-surface floors / tiers / caps as DATA
  (:data:`KG_GATES`, :data:`CG_POLICIES`, and the bound constants). Tuning
  the redesign = editing this table, not code.
* Small identity helpers shared with the router and ``query_code_graph.py``:
  :func:`language_for_path` (:data:`EXT_TO_LANG` — MUST MATCH the analyzer's
  ``_EXT_TO_DISPATCH_NAME``; a parity test pins it),
  :func:`sanitize_session_id` (mirrors ``_lib/session-id.sh``),
  :func:`budget_state_path` (mirrors ``_lib/inject-budget.{sh,ps1}``).

CLI (for shell delegators and debugging — command text goes through STDIN,
never argv, per the R31 privacy discipline)::

    python -m vco_lib.inject_intent classify [--cwd DIR] [--out FILE]   # stdin: command
    python -m vco_lib.inject_intent task-type <profile>
    python -m vco_lib.inject_intent kg-gate <profile>                   # JSON
    python -m vco_lib.inject_intent budget-path <session_id> <prompt_id> <project_root>
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# The EDIT-side parser is ALREADY a vco_lib one-home (the shell lib
# _lib/bash-write-targets.{sh,ps1} is a thin delegator to it since v0.2.95) —
# reuse it; do NOT re-port the grammar (plan §3 WP-A1 "do not leave two
# parsers").
from vco_lib.bash_command_walk import (
    peel_wrapper_verbs,
    split_chain,
    strip_env_assignments,
    tokenize,
)
from vco_lib.bash_write_targets import (
    _REDIRECT_NOISE,
    _SECRETISH_RE,
    extract_write_targets,
    prebash_query_parts,
    strip_heredocs,
)

# --- Intents -----------------------------------------------------------------

INTENT_READ = "READ"
INTENT_EDIT = "EDIT"
INTENT_SEARCH = "SEARCH"
INTENT_MECHANICAL = "MECHANICAL"

#: Ordering used to merge per-segment intents of a chained command: the
#: STRONGEST intent in the chain wins (an edit in a pipeline is an edit).
_INTENT_STRENGTH = {
    INTENT_MECHANICAL: 0,
    INTENT_READ: 1,
    INTENT_SEARCH: 2,
    INTENT_EDIT: 3,
}


@dataclass(frozen=True)
class BashIntent:
    """A1 output. Pure data — the router turns this into queries."""

    intent: str = INTENT_MECHANICAL
    #: target paths named by the command (as written; may be relative)
    targets: Tuple[str, ...] = ()
    #: clean code identifiers recovered from the command (SEARCH patterns,
    #: symbol-bearing READ args)
    symbols: Tuple[str, ...] = ()
    #: heredoc body snippet, ONLY for knowledge/docs targets (the
    #: bash_write_targets secrecy rule — never for code files)
    write_snippet: str = ""
    #: ``(rev, path)`` pairs from ``git show <rev>:<path>`` — the router
    #: compares <rev> against the code graph's indexed-revision stamp and
    #: stays SILENT on mismatch (wave-4 caveat (a))
    rev_paths: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "targets": list(self.targets),
            "symbols": list(self.symbols),
            "write_snippet": self.write_snippet,
            "rev_paths": [list(p) for p in self.rev_paths],
        }


# Verb tables (the §2.1 / plan-v0300 seed list; deliberately explicit).
_READ_VERBS = frozenset({
    "cat", "head", "tail", "less", "more", "diff", "bat", "nl", "sed", "awk",
})
_SEARCH_VERBS = frozenset({"grep", "rg", "ag", "ack", "egrep", "fgrep"})
#: git subcommands that READ (targets = the named paths)
_GIT_READ_SUBS = frozenset({"show", "diff", "log", "blame"})

_SINK_NOISE = _REDIRECT_NOISE  # one home: the bash_write_targets strip list


def _strip_sinks(command: str) -> str:
    """Remove /dev/null sinks + fd dups before write-shape detection (the
    ``vco_bash_write_prefilter`` idiom, Python side)."""
    out = command
    for noise in _SINK_NOISE:
        out = out.replace(noise, "")
    return out


# GLM review nit-6: the INTENT write-verb set is DELIBERATELY narrower than
# bash_write_targets._WRITE_VERB_RE — it drops the ``--output``/``--outfile``/
# ``--out-file``/``--output-file`` compiler-flag arm. The SYNC surface needs
# that arm (a build's --output file must still be synced); the INTENT surface
# must not classify `cargo build --output x` as an EDIT injection trigger
# (plan §2.1: build/test = MECHANICAL, no spawn). Not a mirror drift: two
# different questions, each with its own pinned vocabulary.
_INTENT_WRITE_VERB_RE = re.compile(
    r"(?:^|[\s;&|(])(?:tee|cp|mv|touch|dd)(?:\s|$)"
    r"|sed\s+-i|sed\s+--in-place|perl\s+-i|ruby\s+-i"
    r"|Set-Content|Add-Content|Out-File|Tee-Object"
)


def _edit_shape(text: str) -> bool:
    """Does the command TEXT show an explicit write?

    NARROWER than ``bash_write_targets.command_has_write_shape`` on purpose:
    that predicate also fires on opaque interpreters (``python build.py``)
    and writers (``git checkout``) because the SYNC surface must not miss a
    possible write. The INTENT surface must not classify a build/test/run
    command as an EDIT injection trigger (plan §2.1: MECHANICAL = everything
    else → no spawn at all), so only text-visible writes count: a redirect
    (after sink stripping) or one of the write verbs.

    GLM review nit-7: a bare heredoc OPENER is NOT by itself an edit —
    ``cat <<EOF`` pipes its body to stdout and writes nothing. The redirect /
    tee-verb checks already catch every heredoc that DOES write
    (``cat > f <<EOF``, ``tee f <<EOF``), so the old ``had_heredoc`` arm only
    produced EDIT mislabels with no targets (no spawn, but a wrong
    --intent-out label for the WP-D outcome payload).
    """
    stripped = _strip_sinks(text)
    if ">" in stripped:
        return True
    return bool(_INTENT_WRITE_VERB_RE.search(text))


def _path_like(tok: str) -> bool:
    """A token that plausibly names a file (has a `/` or a dotted extension),
    is not a flag/URL/sink."""
    if not tok or tok.startswith("-") or tok in ("/dev/null", "/dev/stdout"):
        return False
    if tok.startswith(("http://", "https://", "ssh://", "git@")):
        return False
    if "=" in tok and not tok.startswith(("=", "./", "../", "/")):
        # env-assignment leftovers are not paths
        head = tok.split("=", 1)[0]
        if head.replace("_", "").isalnum() and not head.startswith("."):
            return False
    if "/" in tok:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]+$", tok))


# GLM review SF-2: `~` and `^` join the class — `HEAD~2:path` / `HEAD^:path`
# are the commonest relative revs and must keep their revision pin (a `~`/`^`
# cannot legitimately start a path component on the rev side of the colon).
_REV_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.@/~^-]+$")


def _split_rev_path(tok: str) -> Optional[Tuple[str, str]]:
    """``<rev>:<path>`` → (rev, path); None when the token is not a git
    rev-path (guards single-letter drive prefixes and URLs)."""
    if tok.startswith("-") or "://" in tok or ":" not in tok:
        return None
    rev, _, path = tok.partition(":")
    if not rev or not path:
        return None
    if len(rev) == 1 and rev.isalpha():
        return None  # windows drive letter shape (C:\...), not a git rev
    if not _REV_TOKEN_RE.match(rev) or not _REV_TOKEN_RE.match(path):
        return None
    return rev, path


def _search_pattern(tokens: Sequence[str]) -> str:
    """The PATTERN of a search segment (tokens start at the search verb).

    ``grep [-flags] PATTERN [paths...]``; ``-e PAT`` / ``-e=PAT`` honoured;
    ``--`` ends flags. Returns "" when no positional pattern is present."""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok in ("-e", "--regexp"):
            return tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.startswith("-e=") or tok.startswith("--regexp="):
            return tok.split("=", 1)[1]
        if tok.startswith("-"):
            # grouped short flags (-rn, -n, --color=auto): keep scanning. A
            # flag that swallows the next token (-m N, -A N, --include X) is
            # rare enough in this position that treating the next positional
            # as the pattern is the same trade the legacy shell gate made.
            if tok in ("-m", "-A", "-B", "-C", "--include", "--exclude",
                       "--include-dir", "--exclude-dir", "-f", "--file",
                       "--max-count", "--context", "--after-context",
                       "--before-context", "--glob", "-g", "--type", "-t",
                       "--sort", "-j", "--threads"):
                i += 2
                continue
            i += 1
            continue
        return tok
    while i < len(tokens):
        if not tokens[i].startswith("-"):
            return tokens[i]
        i += 1
    return ""


# --- A1 gate ports (ONE HOME; shell copies are interim, parity-pinned) -------
# MUST MATCH (until Wave 2 retires them with their callers):
#   templates/hooks/_lib/codegraph-query.sh  codegraph_pattern_gate /
#                                            codegraph_bash_gate /
#                                            codegraph_extract_symbol
#   templates/hooks/_lib/codegraph-query.ps1 Test-VcoCodegraphPatternGate /
#                                            Test-VcoCodegraphBashGate /
#                                            Get-VcoCodegraphSymbol
# tests/test_v02101_inject_intent_classifier.py::TestShellParity drives both
# sides over one corpus — if you change a rule HERE, change it THERE in the
# same cycle or the parity test goes red (that is the design).

_PATTERN_SNAKE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+")
_PATTERN_CAMEL_RE = re.compile(r"[A-Z][a-z]+[A-Z]")
_PATTERN_CALL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\(")
_PATTERN_KEYWORD_RE = re.compile(
    r"(^|[^A-Za-z0-9_])(def|class|func|function|fn)\s+[A-Za-z_]"
)


def pattern_gate(p: str) -> bool:
    """True when ``p`` looks like a CODE IDENTIFIER worth a code-graph lookup
    (port of ``codegraph_pattern_gate`` — see the MUST MATCH block)."""
    if not p:
        return False
    if _PATTERN_SNAKE_RE.search(p):
        return True
    if _PATTERN_CAMEL_RE.search(p):
        return True
    if _PATTERN_CALL_RE.search(p):
        return True
    if _PATTERN_KEYWORD_RE.search(p):
        return True
    return False


_BASH_GATE_CODE_FILE_RE = re.compile(
    r"(^|[\s/])[A-Za-z0-9_-]+\."
    r"(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto)"
    r"([^A-Za-z0-9]|$)"
)
_BASH_GATE_TOOL_RE = re.compile(r"(^|[\s|])(grep|rg|ag|ack)(\s|$)")


def bash_gate(command: str) -> bool:
    """Port of ``codegraph_bash_gate`` (the legacy pre-bash prefilter)."""
    if not command:
        return False
    if _BASH_GATE_CODE_FILE_RE.search(command):
        return True
    if _BASH_GATE_TOOL_RE.search(command) and pattern_gate(command):
        return True
    return False


_CGQ_NONCODE_EXT_RE = re.compile(
    r"\.(log|txt|json|jsonl|yaml|yml|toml|lock|tar|gz|zip|md|html|css)$"
)
_CGQ_SOURCE_EXT_RE = re.compile(
    r"\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$"
)
_CGQ_METACHARS = frozenset("\\|^$[*?")
_CGQ_DOT_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")
_CGQ_SNAKE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+")


def _strip_one_quote_pair(w: str) -> str:
    # bash: w="${word#\"}"; w="${w%\"}"; w="${w#\'}"; w="${w%\'}"
    if w.startswith('"'):
        w = w[1:]
    if w.endswith('"'):
        w = w[:-1]
    if w.startswith("'"):
        w = w[1:]
    if w.endswith("'"):
        w = w[:-1]
    return w


def extract_symbol(text: str) -> str:
    """Port of ``codegraph_extract_symbol`` (P1e semantics: NO whole-text
    fallback — "" means "no isolable symbol" and the caller skips injection).
    """
    if not text:
        return ""
    for word in text.split():
        w = _strip_one_quote_pair(word)
        if not w or w.startswith("-"):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            continue
        if re.match(r"^[0-9]*[<>]", w):
            continue
        if re.match(r"^https?://", w):
            continue
        if any(c in _CGQ_METACHARS for c in w):
            continue
        if "/" in w:
            if _CGQ_SOURCE_EXT_RE.search(w) and not _CGQ_NONCODE_EXT_RE.search(w):
                return w[:200]
            continue
        if (
            _CGQ_SOURCE_EXT_RE.search(w)
            or _CGQ_DOT_WORD_RE.search(w)
            or _PATTERN_CAMEL_RE.search(w)
            or _CGQ_SNAKE_RE.search(w)
            or _PATTERN_CALL_RE.search(w)
        ):
            return w[:200]
    return ""


_CLEAN_IDENT_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*"
)


def clean_identifier(tok: str) -> str:
    """A STRICT identifier test for the NEW classifier (the legacy gates have
    no equivalent — this is the ``pub(crate)`` wrong-query fix from the
    injection survey).

    Returns the cleaned identifier (a trailing call ``(`` is dropped) or ""
    when the token carries anything an exact code-graph lookup could not
    resolve: regex fragments, visibility qualifiers, shell metacharacters,
    embedded blanks. Feeds the structure (exact) leg, never a semantic query.
    """
    if not tok:
        return ""
    t = tok.strip().strip("\"'")
    if t.endswith("("):
        t = t[:-1]
    m = _CLEAN_IDENT_RE.fullmatch(t)
    return t if m else ""


# --- A1: classify_bash --------------------------------------------------------


#: launcher verbs the classifier sees through (GLM review SF-4): the REAL
#: verb follows the launcher plus its own flags/numerics. `xargs` matters
#: because `find … | xargs grep <ident>` is a common caller-hunt shape the
#: legacy whole-command regex gate used to fire on — without this the Wave-2
#: rewire would turn it from injected to silent (a recall regression).
_LAUNCHER_VERBS = frozenset({"xargs", "command"})


def _peel_launcher_verbs(tokens: List[str]) -> List[str]:
    """Drop leading ``xargs`` / ``command`` launchers plus their own flags
    (``-I{}``, ``-0``, ``-v``) and numeric values (``-n 1``), landing on the
    verb that does the real work."""
    out = list(tokens)
    safety = 0
    while out and safety < 8:
        safety += 1
        if os.path.basename(out[0]).lower() not in _LAUNCHER_VERBS:
            return out
        out = out[1:]
        while out and (out[0].startswith("-") or out[0].lstrip("-").isdigit()):
            out = out[1:]
    return out


def _classify_segment(tokens: List[str],
                      cwd: str) -> Tuple[str, List[str], List[str], List[Tuple[str, str]]]:
    """(intent, targets, symbols, rev_paths) for ONE chain segment.
    ``tokens`` are already env-stripped and wrapper-peeled."""
    tokens = _peel_launcher_verbs(tokens)
    if not tokens:
        return INTENT_MECHANICAL, [], [], []
    seg_text = " ".join(tokens)
    verb = os.path.basename(tokens[0]).lower()

    # EDIT first: a redirect/write-verb outranks the verb's own class
    # (`cat > f <<EOF` is a WRITE, not a READ — survey wrong-trigger corpus).
    if _edit_shape(seg_text):
        targets = [
            os.path.relpath(t, cwd) if cwd and t.startswith(cwd + os.sep) else t
            for t in extract_write_targets(seg_text, project_root=cwd or None,
                                           require_exists=False)
        ]
        return INTENT_EDIT, targets, [], []

    if verb == "git" and len(tokens) > 1:
        sub = tokens[1].lower()
        if sub == "grep":
            return _classify_search(tokens[1:], cwd)
        if sub in _GIT_READ_SUBS:
            targets: List[str] = []
            rev_paths: List[Tuple[str, str]] = []
            saw_dashdash = False
            for tok in tokens[2:]:
                if tok == "--":
                    saw_dashdash = True
                    continue
                if tok.startswith("-") and not saw_dashdash:
                    continue
                rp = _split_rev_path(tok)
                if rp:
                    # Re-review nit-3: EVERY git read subcommand can carry a
                    # `<rev>:<path>` blob pin (`git diff HEAD~2:f` included) —
                    # the token becomes a pin + its clean path, never a
                    # polluted target.
                    rev_paths.append(rp)
                    targets.append(rp[1])
                    continue
                if _path_like(tok):
                    targets.append(tok)
            return INTENT_READ, targets, [], rev_paths
        return INTENT_MECHANICAL, [], [], []

    if verb in _SEARCH_VERBS:
        return _classify_search(tokens, cwd)

    if verb in _READ_VERBS:
        # `sed -i` never reaches here (edit shape caught it); sed/awk dumps
        # are READs (the survey's "sed dump" corpus — weak surface, 0.75 floor).
        targets = [t for t in tokens[1:] if _path_like(t)]
        return INTENT_READ, targets, [], []

    return INTENT_MECHANICAL, [], [], []


#: the code keywords `pattern_gate`'s fourth rule knows — when a multi-word
#: pattern LEADS with one, the identifier sits right behind it (GLM review
#: SF-3: `grep 'def authenticate'` is THE canonical exact-lookup shape).
_KEYWORD_PREFIXES = frozenset({"def", "class", "fn", "function", "func"})


def _classify_search(tokens: Sequence[str], cwd: str) -> Tuple[str, List[str], List[str], List[Tuple[str, str]]]:
    """SEARCH iff the PATTERN passes the identifier gate AND a CLEAN
    identifier is recoverable (exact-lookup only — a semantic query on a
    regex fragment is the ``pub(crate)`` noise class the redesign kills).

    SF-3: for a multi-word pattern the leading code keyword is stripped and
    the words are retried individually — ``def authenticate`` yields
    ``authenticate`` instead of falling through to MECHANICAL."""
    pattern = _search_pattern(list(tokens))
    if not pattern or not pattern_gate(pattern):
        return INTENT_MECHANICAL, [], [], []
    sym = clean_identifier(extract_symbol(pattern)) or clean_identifier(pattern)
    if not sym and re.search(r"\s", pattern):
        words = pattern.split()
        if words and words[0].lower() in _KEYWORD_PREFIXES:
            words = words[1:]
        for w in words:
            # A bare keyword word is never the symbol (`pub(crate) fn` must
            # stay MECHANICAL — "fn" is the regex fragment's own keyword, not
            # an identifier; the survey's wrong-query class).
            if w.lower() in _KEYWORD_PREFIXES:
                continue
            cand = clean_identifier(extract_symbol(w)) or clean_identifier(w)
            # Re-review nit-2: junk-rescue bound — a single character or an
            # all-underscore word is never a lookup key (`def _`, `class A`).
            if cand and len(cand) >= 2 and cand.strip("_"):
                sym = cand
                break
    if not sym:
        return INTENT_MECHANICAL, [], [], []
    targets = [t for t in tokens[1:] if _path_like(t)]
    return INTENT_SEARCH, targets, [sym], []


def classify_bash(command: str, cwd: str = "") -> BashIntent:
    """A1: classify a Bash tool command.

    Strongest intent across chain segments wins (EDIT > SEARCH > READ >
    MECHANICAL). Heredoc bodies are stripped BEFORE tokenising (a body line
    like ``see foo > bar`` must not read as a redirect — the
    ``bash_write_targets.strip_heredocs`` rationale). Unparseable quoting →
    MECHANICAL (conservative: no injection, no query).
    """
    if not command or not command.strip():
        return BashIntent()

    stripped, heredoc_bodies = strip_heredocs(command)
    stripped = _strip_sinks(stripped)

    segments: List[List[str]] = []
    for line in stripped.splitlines():
        toks = tokenize(line)
        if toks is None:
            continue  # malformed quoting on this line — skip (soft-fail)
        segments.extend(split_chain(toks))
    if not segments:
        return BashIntent()

    intent = INTENT_MECHANICAL
    targets: List[str] = []
    symbols: List[str] = []
    rev_paths: List[Tuple[str, str]] = []
    for seg in segments:
        seg = peel_wrapper_verbs(strip_env_assignments(seg))
        s_intent, s_targets, s_symbols, s_revs = _classify_segment(seg, cwd)
        if _INTENT_STRENGTH[s_intent] > _INTENT_STRENGTH[intent]:
            intent = s_intent
        for t in s_targets:
            if t not in targets:
                targets.append(t)
        for s in s_symbols:
            if s not in symbols:
                symbols.append(s)
        for rp in s_revs:
            if rp not in rev_paths:
                rev_paths.append(rp)

    write_snippet = ""
    if intent == INTENT_EDIT:
        # The snippet rule (knowledge/docs targets only, secret-shaped bodies
        # withheld) already lives in bash_write_targets.prebash_query_parts —
        # reuse it rather than mirroring the secrecy logic here.
        _target, snippet = prebash_query_parts(command, project_root=cwd or None)
        if snippet and not _SECRETISH_RE.search(snippet):
            write_snippet = snippet

    return BashIntent(
        intent=intent,
        targets=tuple(targets[:6]),
        symbols=tuple(symbols[:6]),
        write_snippet=write_snippet,
        rev_paths=tuple(rev_paths[:4]),
    )


# --- A2: symbol extraction ----------------------------------------------------

#: File extension → canonical language id. ONE home for the injection
#: surfaces; MUST MATCH ``templates/scripts/analyze_code_graph.py``'s
#: ``_EXT_TO_DISPATCH_NAME`` (which writes the ``language`` property the
#: exact-symbol identity check compares against) — a parity test in
#: tests/test_v02101_structure_hook_format.py pins the two.
EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".lua": "lua",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
    ".cs": "csharp",
    ".proto": "proto",
    ".svelte": "svelte",
    ".ps1": "powershell",
    ".psm1": "powershell",
}

#: Read-tool output line prefix ("cat -n" / arrow shaped) — PostToolUse Read
#: delivers numbered content; the prefixes must not defeat indentation tests.
_READ_LINE_PREFIX_RE = re.compile(r"^\s*\d+(?:→|\t)")


def language_for_path(path: str) -> str:
    """Canonical language id for a file path ("" when unknown)."""
    if not path:
        return ""
    _, ext = os.path.splitext(path)
    return EXT_TO_LANG.get(ext.lower(), "")


def _read_content(file_path: str, content: Optional[str]) -> Optional[str]:
    if content is not None:
        return content
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _strip_read_tool_prefixes(text: str) -> str:
    lines = text.split("\n")
    hits = sum(1 for ln in lines if _READ_LINE_PREFIX_RE.match(ln))
    if hits == 0:
        return text
    return "\n".join(
        _READ_LINE_PREFIX_RE.sub("", ln, count=1) if _READ_LINE_PREFIX_RE.match(ln) else ln
        for ln in lines
    )


# Per-language ENCLOSING-symbol tables: (regex with indent group + name
# group). Kept small on purpose (plan §3 WP-A2: "a small per-language regex
# table"); Python and Rust are the pinned ones, the rest are best-effort.
_PY_DEF_RES = (
    re.compile(r"^(?P<ind>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<name>\w+)"),
    re.compile(r"^(?P<ind>[ \t]*)class[ \t]+(?P<name>\w+)"),
)
_RS_DEF_RES = (
    re.compile(
        r"^(?P<ind>[ \t]*)(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?"
        r"(?:unsafe[ \t]+)?(?:fn[ \t]+(?P<name>\w+)"
        r"|(?:struct|enum|trait|type)[ \t]+(?P<name2>\w+)"
        r"|impl(?:<[^<>]*>)?[ \t]+(?:(?P<trait>[\w:]+)(?:<[^<>]*>)?[ \t]+for[ \t]+)?"
        r"(?P<implname>[\w:]+))"),
)
_JS_DEF_RES = (
    re.compile(r"^(?P<ind>[ \t]*)(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*(?P<name>\w+)"),
    re.compile(r"^(?P<ind>[ \t]*)(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+(?P<name>\w+)"),
    re.compile(r"^(?P<ind>[ \t]*)(?:export[ \t]+)?(?:const|let|var)[ \t]+(?P<name>\w+)[ \t]*=[ \t]*(?:async[ \t]*)?(?:function|\()"),
)
_GO_DEF_RES = (
    re.compile(r"^(?P<ind>[ \t]*)func[ \t]+(?:\([^)]*\)[ \t]*)?(?P<name>\w+)"),
    re.compile(r"^(?P<ind>[ \t]*)type[ \t]+(?P<name>\w+)"),
)
_SH_DEF_RES = (
    re.compile(r"^(?P<ind>[ \t]*)(?:function[ \t]+)?(?P<name>\w+)[ \t]*\(\)"),
)
_C_FAMILY_DEF_RES = (
    re.compile(r"^(?P<ind>[ \t]*)(?:template[ \t]*<[^>]*>[ \t]*)?(?:class|struct)[ \t]+(?P<name>\w+)"),
    re.compile(r"^(?P<ind>[ \t]*)(?:public|private|protected|internal|static|virtual|sealed|override|abstract|[ \t])*[\w:<>\[\]*&,]+[ \t]+(?P<name>\w+)[ \t]*\([^;]*\)[ \t]*(?:const[ \t]*)?(?:override[ \t]*)?\{?[ \t]*$"),
)

_DEF_TABLE: Dict[str, Tuple[re.Pattern, ...]] = {
    "python": _PY_DEF_RES,
    "rust": _RS_DEF_RES,
    "javascript": _JS_DEF_RES,
    "typescript": _JS_DEF_RES,
    "svelte": _JS_DEF_RES,
    "go": _GO_DEF_RES,
    "shell": _SH_DEF_RES,
    "powershell": (re.compile(r"^(?P<ind>[ \t]*)function[ \t]+(?P<name>[\w-]+)"),),
    "cpp": _C_FAMILY_DEF_RES,
    "c": _C_FAMILY_DEF_RES,
    "java": _C_FAMILY_DEF_RES,
    "csharp": _C_FAMILY_DEF_RES,
}


def _def_name_from_match(m: re.Match) -> Optional[str]:
    """The def name from any table match (the tables use different named
    groups per alternative; `::`-qualified Rust paths collapse to the leaf)."""
    gd = m.groupdict()
    for key in ("name", "name2", "implname"):
        val = gd.get(key)
        if val:
            return val.split("::")[-1]
    return None


def _indent_width(ind: str) -> int:
    return ind.expandtabs(4).count(" ") if ind is not None else 0


def _enclosing_names(lines: Sequence[str], match_line: int,
                     regexes: Sequence[re.Pattern]) -> List[str]:
    """Walk BACKWARDS from ``match_line`` collecting enclosing defs: the
    first def at any indent (innermost), then only defs at STRICTLY smaller
    indent (walking out). Nearest first, max 3."""
    out: List[str] = []
    threshold: Optional[int] = None
    for idx in range(min(match_line, len(lines) - 1), -1, -1):
        line = lines[idx]
        for rx in regexes:
            m = rx.match(line)
            if not m:
                continue
            name = _def_name_from_match(m)
            if not name:
                continue
            ind = _indent_width(m.groupdict().get("ind", ""))
            if threshold is None or ind < threshold:
                out.append(name)
                threshold = ind
            break
        if len(out) >= 3:
            break
    return out


def edit_enclosing_symbols(file_path: str, old_string: str) -> List[str]:
    """A2: the def/class/fn/impl names enclosing the Edit's ``old_string``.

    Nearest-first, max 3. [] when the file is unreadable or the match is
    absent (the router then falls back to no exact-symbol leg — a wrong
    symbol is worse than none)."""
    if not file_path or not old_string:
        return []
    text = _read_content(file_path, None)
    if text is None:
        return []
    pos = text.find(old_string)
    if pos < 0:
        return []
    match_line = text.count("\n", 0, pos)
    regexes = _DEF_TABLE.get(language_for_path(file_path))
    if not regexes:
        return []
    return _enclosing_names(text.split("\n"), match_line, regexes)


#: top-level (pub) symbol patterns per language for the Read/Write surfaces
#: (ZERO-indent anchored — the "pub fns/classes of the file" of §2.1, not a
#: whole-file def walk; nested defs and methods are not module surface).
_JS_PUB_RES = (
    re.compile(r"^(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*(\w+)"),
    re.compile(r"^(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+(\w+)"),
    re.compile(r"^(?:export[ \t]+)?(?:const|let|var)[ \t]+(\w+)[ \t]*=[ \t]*(?:async[ \t]*)?(?:function|\()"),
)
_GO_PUB_RES = (
    re.compile(r"^func[ \t]+(?:\([^)]*\)[ \t]*)?(\w+)"),
    re.compile(r"^type[ \t]+(\w+)"),
)
_PUB_TABLE: Dict[str, Tuple[re.Pattern, ...]] = {
    "python": (
        re.compile(r"^(?:async[ \t]+)?def[ \t]+(\w+)"),
        re.compile(r"^class[ \t]+(\w+)"),
    ),
    "rust": (
        re.compile(r"^(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?(?:unsafe[ \t]+)?fn[ \t]+(\w+)"),
        re.compile(r"^(?:pub(?:\([^)]*\))?[ \t]+)?(?:struct|enum|trait)[ \t]+(\w+)"),
        re.compile(r"^impl(?:<[^<>]*>)?[ \t]+(?:(?:[\w:]+)(?:<[^<>]*>)?[ \t]+for[ \t]+)?([\w:]+)"),
    ),
    "javascript": _JS_PUB_RES,
    "typescript": _JS_PUB_RES,
    "svelte": _JS_PUB_RES,
    "go": _GO_PUB_RES,
    "shell": (re.compile(r"^(?:function[ \t]+)?(\w+)[ \t]*\(\)"),),
    "powershell": (re.compile(r"^function[ \t]+([\w-]+)"),),
}


def file_pub_symbols(file_path: str, content: Optional[str] = None) -> List[str]:
    """A2: the file's TOP-LEVEL symbols for the Read/Write surfaces.

    ``content`` (the PostToolUse ``tool_response`` text) is preferred over
    re-reading disk; Read-tool line-number prefixes are stripped first.
    File order, deduped, max 5."""
    text = _read_content(file_path, content)
    if text is None:
        return []
    text = _strip_read_tool_prefixes(text)
    regexes = _PUB_TABLE.get(language_for_path(file_path))
    if not regexes:
        return []
    out: List[str] = []
    for line in text.split("\n"):
        for rx in regexes:
            m = rx.match(line)
            if m:
                name = m.group(1).split("::")[-1]
                if name and name not in out:
                    out.append(name)
                break
        if len(out) >= 5:
            break
    return out


_TASK_LINE_RE = re.compile(r"^\s*Task\s*:\s*(.*)$", re.IGNORECASE)
_SECTION_LINE_RE = re.compile(r"^[A-Z][\w -]{2,}\s*:")
_PREAMBLE_LINE_RE = re.compile(r"(?i)first action|effort")


def agent_task_section(prompt: str) -> str:
    """C4: the brief's TASK section for the agent-surface KG query.

    Match order (plan §3 WP-C4): the shipped handoff format's ``Task:`` line
    (through to the next section header or blank line); else the first
    sentence after any FIRST ACTION / effort preamble lines; else the first
    sentence. Capped at 400 chars (the current subagent-start hook's cap
    rationale). "" for an empty/briefless prompt."""
    if not prompt or not prompt.strip():
        return ""
    lines = prompt.split("\n")

    # 1. explicit "Task:" section (the shipped handoff format)
    for i, line in enumerate(lines):
        m = _TASK_LINE_RE.match(line)
        if not m:
            continue
        parts = [m.group(1).strip()]
        for follow in lines[i + 1:]:
            if not follow.strip() or _SECTION_LINE_RE.match(follow):
                break
            parts.append(follow.strip())
        joined = " ".join(p for p in parts if p)
        if joined:
            return joined[:400]

    # 2. skip leading preamble lines (FIRST ACTION / effort directives)
    body = list(lines)
    while body and (not body[0].strip() or _PREAMBLE_LINE_RE.search(body[0])):
        body.pop(0)
    rest = " ".join(ln.strip() for ln in body if ln.strip())
    if not rest:
        return prompt.strip()[:400]

    # 3. first sentence
    m = re.search(r".*?\.(?:\s|$)", rest)
    sentence = m.group(0).strip() if m else rest
    return sentence[:400]


# --- §2.1 noise-gate table (the ONE tunable home) -----------------------------

STRONG_SCORE = 0.85


@dataclass(frozen=True)
class KgGate:
    """KG-leg gate for one injection profile (§2.1).

    ``tier_below`` applies to scores in [floor, strong_threshold);
    ``tier_above`` to scores >= strong_threshold. ``max_chars`` > 0 bounds
    the whole KG block (the agent brief's 1 500)."""

    floor: float
    tier_below: str = "titles"
    tier_above: str = "single_chunk"
    strong_threshold: float = STRONG_SCORE
    max_rows: int = 3
    max_chars: int = 0


@dataclass(frozen=True)
class CgPolicy:
    """Code-graph-leg policy for one injection profile (§2.1).

    ``exact`` lookups only (structure def+callers) — the redesign issues no
    semantic code-graph queries from injection surfaces."""

    enabled: bool
    max_rows: int = 5
    max_callers_per_row: int = 5
    exclude_self_file: bool = False
    require_clean_symbol: bool = False
    revision_stamp: bool = False


#: §2.1, row by row. `None` = NO KG leg on that surface.
KG_GATES: Dict[str, Optional[KgGate]] = {
    # Bash READ (cat/sed -n/head/tail/git show): weak intent → 0.75 floor
    "bash_read": KgGate(floor=0.75, tier_above="single_chunk", max_rows=3),
    # Bash EDIT (sed -i, >, heredoc): strong intent → 0.65 floor
    "bash_edit": KgGate(floor=0.65, tier_above="single_chunk", max_rows=3),
    # Bash SEARCH (grep identifier): no KG leg
    "bash_search": None,
    # Read(code) PostToolUse
    "read_code": KgGate(floor=0.70, tier_above="single_chunk", max_rows=3),
    # Read(docs/knowledge) PostToolUse
    "read_docs": KgGate(floor=0.70, tier_above="single_chunk", max_rows=3),
    # Grep(identifier) PreToolUse: no KG leg
    "grep": None,
    # Edit / Write(code) PreToolUse: three_chunks allowed >= 0.85 (owner)
    "edit": KgGate(floor=0.65, tier_above="three_chunks", max_rows=3),
    "write": KgGate(floor=0.65, tier_above="three_chunks", max_rows=3),
    # Agent brief: small + deterministic (lands in the lane's FIRST prompt)
    "agent_brief": KgGate(floor=0.65, tier_above="three_chunks", max_rows=3,
                          max_chars=1500),
}

#: §2.1 code-graph column.
CG_POLICIES: Dict[str, CgPolicy] = {
    "bash_read": CgPolicy(enabled=True, require_clean_symbol=True,
                          revision_stamp=True),
    "bash_edit": CgPolicy(enabled=True),
    "bash_search": CgPolicy(enabled=True),
    # Re-review nit-2 (coordinator ruling: remove the request): a Read has no
    # pinned ref to compare against — the stamp's only consumer is the
    # bash_read rev gate (`git show <rev>`), and the router strips stamp
    # lines before emitting, so a Read-side stamp was a `git rev-parse` per
    # CG leg with no reader. §2.1's "indexed-revision stamp" on the Read row
    # is honoured where it can act (bash_read); reported as a table deviation.
    "read_code": CgPolicy(enabled=True),
    "read_docs": CgPolicy(enabled=False),
    "grep": CgPolicy(enabled=True),
    "edit": CgPolicy(enabled=True, exclude_self_file=True),
    "write": CgPolicy(enabled=True, exclude_self_file=True),
    "agent_brief": CgPolicy(enabled=False),
}

#: WP-B1/WP-D: RL task_type per profile. ``pre_search_kg_search`` is
#: registered even though the SEARCH/Grep profiles run no KG leg today —
#: the value must be KNOWN before any wrapper can set it (risk table:
#: "RL corpus gap during the transition"), and it partitions the corpus
#: correctly the day a KG leg is enabled there.
TASK_TYPE_BY_PROFILE: Dict[str, str] = {
    "bash_read": "pre_bash_kg_search",
    "bash_edit": "pre_bash_kg_search",
    "bash_search": "pre_search_kg_search",
    "read_code": "pre_read_kg_search",
    "read_docs": "pre_read_kg_search",
    "grep": "pre_search_kg_search",
    "edit": "pre_edit_kg_search",
    "write": "pre_write_kg_search",
    "agent_brief": "agent_brief_kg_search",
}

#: Every profile name the router / --injection-profile accept.
KG_PROFILES: Tuple[str, ...] = tuple(KG_GATES)


def kg_gate(profile: str) -> Optional[KgGate]:
    """The KG gate for an injection profile; None = no KG leg (§2.1)."""
    return KG_GATES.get(profile)


def cg_policy(profile: str) -> CgPolicy:
    """The code-graph policy for an injection profile (§2.1)."""
    return CG_POLICIES.get(profile, CgPolicy(enabled=False))


def task_type_for(profile: str) -> str:
    """The RL task_type a profile's KG leg must carry (WP-B1/WP-D)."""
    return TASK_TYPE_BY_PROFILE.get(profile, "cli_kg_search")


# --- §2.1 additional bounds ---------------------------------------------------

#: per-injection soft cap (the 9 500 vco_cap_context hard cap still applies
#: underneath, in the wrapper)
PER_INJECTION_SOFT_CAP = 2500
#: per-turn budget across ALL surfaces, counted at emit time
PER_TURN_BUDGET_CHARS = 6000
#: budget state files older than this are GC'd
BUDGET_GC_AGE_S = 86400
#: per-session code-graph inject cap default — the seen-store's
#: VCO_CG_INJECT_CAP (unchanged; mirrored here so the router honours the
#: SAME counter file). MUST MATCH _lib/seen-store.sh vco_cg_inject_cap.
CG_SESSION_CAP_DEFAULT = 40

_KG_LABEL_FIELD_RE = re.compile(
    r"^(TITLES|SUMMARY|FULL NODE|\d+ CHUNKS?( \(\d+/\d+ chunks?\))?):?"
)


def cap_block(text: str, cap: int = PER_INJECTION_SOFT_CAP) -> str:
    """Per-injection soft cap with an HONEST truncation marker (never a
    silent slice; the 10 000-char harness cap's silent file-path downgrade
    is the failure this prevents at 2 500)."""
    if len(text) <= cap:
        return text
    marker = f"\n[cut to {cap} chars]"
    keep = max(cap - len(marker), 0)
    return text[:keep] + marker


def titles_one_liner(block: str) -> str:
    """Degrade ONE KG:/CODE: block to its titles one-liner (the per-turn
    budget exhaustion fallback, §2.1). Keeps the dedup identity (first
    field) and the `src=` trailer (reads-ledger suppression)."""
    lines = block.split("\n")
    header = ""
    for ln in lines:
        if ln.startswith("KG: ") or ln.startswith("CODE: "):
            header = ln
            break
    if not header:
        return ""
    prefix, _, rest = header.partition(": ")
    fields = rest.split(" | ")
    srcs = [f for f in fields if f.startswith("src=")]
    if prefix == "CODE":
        head = fields[:1]
    else:
        head = [f for f in fields
                if not _KG_LABEL_FIELD_RE.match(f) and not f.startswith("src=")]
    out = f"{prefix}: " + " | ".join(head + ["TITLES"] + srcs)
    return out


_SESSION_ID_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def sanitize_session_id(raw: str) -> str:
    """Mirror of ``_lib/session-id.sh::vco_hook_sanitize_session_id``:
    allow-list [A-Za-z0-9_-]; anything else → "default"; empty stays empty.
    MUST MATCH the shell (same cross-session-bleed guard)."""
    if not raw:
        return ""
    return raw if _SESSION_ID_ALLOWED_RE.match(raw) else "default"


def _safe_state_component(raw: str) -> str:
    return raw if raw and _SESSION_ID_ALLOWED_RE.match(raw) else ""


def budget_state_path(session_id: str, prompt_id: str, project_root: str) -> Optional[str]:
    """The per-turn budget counter file (§2.1), or None when enforcement
    must fail OPEN: an untrustworthy session ("" / "default" — the
    seen-store's cross-session-bleed guard), a MISSING prompt_id (an empty
    component would collapse every turn of the session onto one budget), or
    a missing project root. MUST MATCH _lib/inject-budget.{sh,ps1}
    vco_inject_budget_path / Get-VcoInjectBudgetPath."""
    sid = _safe_state_component(session_id)
    pid = _safe_state_component(prompt_id)
    if not sid or sid == "default" or not pid or not project_root:
        return None
    return os.path.join(project_root, ".claude", "state",
                        f"inject_budget_{sid}_{pid}")


def kg_query_for_targets(targets: Sequence[str], cwd: str = "") -> str:
    """KG query text for a set of file targets: the topic of the path
    (module stem + parent dir), NEVER command text (owner rule). "" when no
    usable target."""
    for t in targets:
        base = os.path.basename(t)
        stem, _ext = os.path.splitext(base)
        if not stem or stem.startswith("."):
            continue
        parent = os.path.basename(os.path.dirname(t.rstrip("/\\")))
        stem_words = re.split(r"[_\-.]+", stem)
        parts = [w for w in stem_words if w]
        if parent and parent not in ("", ".", ".."):
            parts.append(parent)
        if parts:
            return " ".join(parts)[:200]
    return ""


# --- CLI (shell delegators + debugging; command text via STDIN only) ----------


def _cli_main(argv: Optional[List[str]] = None) -> int:
    """``python -m vco_lib.inject_intent <subcommand>``. Always exits 0 for
    ``classify`` (empty/MECHANICAL classification is a valid result, never an
    error); exit 2 for an unknown subcommand/profile (a caller bug)."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="vco_lib.inject_intent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_classify = sub.add_parser("classify", help="stdin: one Bash command; stdout: JSON BashIntent")
    p_classify.add_argument("--cwd", default="", help="project/call cwd for relative target resolution")
    p_classify.add_argument("--out", default="", help="also write the JSON to this file (the router's --intent-out seam)")
    p_task = sub.add_parser("task-type", help="stdout: the RL task_type of an injection profile")
    p_task.add_argument("profile")
    p_gate = sub.add_parser("kg-gate", help="stdout: JSON KgGate (null when the profile has no KG leg)")
    p_gate.add_argument("profile")
    p_budget = sub.add_parser("budget-path", help="stdout: the per-turn budget state path (empty when enforcement fails open)")
    p_budget.add_argument("session_id")
    p_budget.add_argument("prompt_id")
    p_budget.add_argument("project_root")
    args = parser.parse_args(argv)

    if args.cmd == "classify":
        try:
            command = sys.stdin.read()
        except (OSError, UnicodeDecodeError):
            command = ""
        bi = classify_bash(command, args.cwd)
        payload = json.dumps(bi.to_dict())
        sys.stdout.write(payload + "\n")
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(payload + "\n")
            except OSError:
                pass  # best-effort: the wrapper's outcome pairing degrades, never blocks
        return 0
    if args.cmd == "task-type":
        if args.profile not in KG_GATES:
            return 2
        sys.stdout.write(task_type_for(args.profile) + "\n")
        return 0
    if args.cmd == "kg-gate":
        if args.profile not in KG_GATES:
            return 2
        gate = kg_gate(args.profile)
        sys.stdout.write(
            json.dumps(asdict(gate) if gate else None) + "\n"
        )
        return 0
    if args.cmd == "budget-path":
        path = budget_state_path(args.session_id, args.prompt_id, args.project_root)
        sys.stdout.write((path or "") + "\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())
