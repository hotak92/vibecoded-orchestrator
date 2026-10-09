# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PowerShell-tool command reader (v0.2.102 lane W5-D).

ONE home for "what does this PowerShell command do", shared by every
shell-inspecting decision that already knows how to read a Bash command:
the wait-loop guard (:mod:`vco_lib.wait_guard`), the injection-intent
classifier (:mod:`vco_lib.inject_intent`), and the CLI-write path finder
behind post-bash file sync (:mod:`vco_lib.bash_write_targets`).

Why it exists: Claude Code on Windows without Git Bash registers NO Bash
tool — the shell tool is ``PowerShell`` (vendor hooks doc, "PowerShell"
tool input: the same ``command`` field as Bash). Every hook VCO registers
on ``Bash`` therefore never fired there. The registrations now match
``Bash|PowerShell``; this module is what lets the decision modules read
the PowerShell syntax they then receive.

Shape (A>B>C): rather than a second classifier per consumer, the command
is TRANSLATED into its Bash-equivalent form (:func:`to_bash_equivalent`)
and handed to the existing Bash readers unchanged — ``Get-Content f
-Tail 20`` reads as ``tail -n 20 f``, ``Start-Sleep 5`` as ``sleep 5``,
``Set-Content -Path f -Value x`` as ``tee f``, ``Copy-Item a b`` as
``cp a b``, and so on. One translation table, every consumer. Commands
this module does not know (``git``, ``rg``, ``python``, ``npm`` …) pass
through verbatim, which is right: they mean the same thing in both shells.

What it understands:

* tokenization: ``'single'`` (``''`` escape) and ``"double"`` quotes
  (backtick escapes), backtick escapes and line continuations, ``#`` and
  ``<# … #>`` comments, here-strings ``@' … '@`` / ``@" … "@``,
  ``${braced}`` variables;
* statement boundaries: ``;``, newlines, ``|``, ``&&``, ``||`` and the
  ``{ }`` / ``( )`` / ``$( )`` / ``@( )`` block delimiters — so a loop body
  (``while ($true) { Get-Content f; Start-Sleep 5 }``) yields its inner
  statements, exactly like the Bash walker yields a ``do … done`` body;
* leading keywords (``while``/``do``/``for``/``if`` …), the ``&`` call and
  ``.`` dot-source operators, and ``$var =`` assignments are peeled;
* cmdlets by name, alias (``gc``, ``sls``, ``cpi`` …) and module-qualified
  name, parameters case-insensitively, by alias and by unique prefix
  (``-Sec 5``), ``-Name:value`` binding and positional binding;
* redirections (``>``, ``>>``, ``2>``, ``*>`` → ``&>``, ``2>&1``) and the
  ``$null`` sink (→ ``/dev/null``, which every Bash reader already strips).

Pure stdlib, no I/O, never raises on input (malformed quoting yields the
best-effort reading of what parsed; an empty string yields ``""``).

CLI (debugging; the command goes through STDIN, never argv)::

    python -m vco_lib.powershell_command translate   # stdin: one command
"""
from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

__all__ = [
    "BASH_TOOL",
    "POWERSHELL_TOOL",
    "SHELL_TOOLS",
    "PS_WAIT_VERBS",
    "SPELLINGS",
    "CMDLET_BASH_VERB",
    "is_shell_tool",
    "loop_pattern",
    "statements",
    "to_bash_equivalent",
    "command_for_bash_readers",
    "spellings_for_bash_verbs",
]

#: The two shell tools a hook can receive. ``PowerShell`` exists on Windows
#: (always, when Git Bash is absent) and wherever the PowerShell tool is
#: enabled; its ``tool_input`` carries the same ``command`` field as Bash.
BASH_TOOL = "Bash"
POWERSHELL_TOOL = "PowerShell"
SHELL_TOOLS: FrozenSet[str] = frozenset({BASH_TOOL, POWERSHELL_TOOL})

#: Cmdlets that BLOCK the foreground until something else finishes — the
#: PowerShell spellings of a wait loop. They have no Bash equivalent, so
#: they translate to their own lower-case name and the wait guard knows
#: them by that name (a Bash command never starts with ``wait-job``).
PS_WAIT_VERBS: FrozenSet[str] = frozenset({"wait-job", "wait-process", "wait-event"})


def is_shell_tool(tool_name: object) -> bool:
    """True for the tool names whose ``tool_input.command`` is a shell
    command a VCO shell hook inspects (``Bash`` / ``PowerShell``)."""
    return isinstance(tool_name, str) and tool_name in SHELL_TOOLS


# ═══════════════════════════════════════════════════════════════════════
# Tokenizer
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _Tok:
    text: str
    quoted: bool = False
    #: "" for a word; else the operator: ";", "|", "&&", "||", "{", "}",
    #: "(", ")", "&", or "redir" (``text`` then holds the redirect form).
    op: str = ""


_BLOCK_OPS = frozenset({";", "|", "&&", "||", "{", "}", "(", ")"})


def _tokenize(command: str) -> List[_Tok]:
    out: List[_Tok] = []
    buf: List[str] = []
    quoted = False
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal buf, quoted
        if buf or quoted:
            out.append(_Tok("".join(buf), quoted))
        buf, quoted = [], False

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if c in " \t":
            flush()
            i += 1
        elif c in "\r\n":
            flush()
            out.append(_Tok(";", op=";"))
            i += 1
        elif c == "<" and nxt == "#":
            flush()
            end = command.find("#>", i + 2)
            i = n if end < 0 else end + 2
        elif c == "#" and not buf and not quoted:
            end = command.find("\n", i)
            i = n if end < 0 else end
        elif c == "`":
            if nxt in ("\n", "\r"):
                i += 2
                if nxt == "\r" and i < n and command[i] == "\n":
                    i += 1
            else:
                if nxt:
                    buf.append(nxt)
                i += 2
        elif c == "@" and nxt in ("'", '"') and command[i + 2:i + 3] in ("\n", "\r"):
            # Here-string: @'<NL> … <NL>'@ — content is one quoted word.
            flush()
            closer = "\n" + nxt + "@"
            start = command.find("\n", i)
            end = command.find(closer, start)
            body = command[start + 1:end] if end >= 0 else command[start + 1:]
            out.append(_Tok(body.rstrip("\r"), quoted=True))
            i = n if end < 0 else end + len(closer)
        elif c == "'":
            quoted = True
            i += 1
            while i < n:
                if command[i] == "'":
                    if i + 1 < n and command[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(command[i])
                i += 1
        elif c == '"':
            quoted = True
            i += 1
            while i < n:
                ch = command[i]
                if ch == "`" and i + 1 < n:
                    buf.append(command[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    if i + 1 < n and command[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(ch)
                i += 1
        elif c == "$" and nxt == "{":
            end = command.find("}", i + 2)
            end = n - 1 if end < 0 else end
            buf.append(command[i:end + 1])
            i = end + 1
        elif c in "$@" and nxt == "(" and not buf:
            flush()
            out.append(_Tok("(", op="("))
            i += 2
        elif c == "@" and nxt == "{" and not buf:
            flush()
            out.append(_Tok("{", op="{"))
            i += 2
        elif c == ";":
            flush()
            out.append(_Tok(";", op=";"))
            i += 1
        elif c == "|":
            flush()
            if nxt == "|":
                out.append(_Tok("||", op="||"))
                i += 2
            else:
                out.append(_Tok("|", op="|"))
                i += 1
        elif c == "&":
            flush()
            if nxt == "&":
                out.append(_Tok("&&", op="&&"))
                i += 2
            else:
                out.append(_Tok("&", op="&"))
                i += 1
        elif c in "{}()":
            flush()
            out.append(_Tok(c, op=c))
            i += 1
        elif c == ">":
            stream = ""
            if not quoted and len(buf) == 1 and buf[0] in "123456*":
                stream = buf[0]
                buf = []
            flush()
            j = i + 1
            arrow = ">"
            if j < n and command[j] == ">":
                arrow = ">>"
                j += 1
            merge = ""
            if j + 1 < n and command[j] == "&" and command[j + 1].isdigit():
                merge = command[j:j + 2]
                j += 2
            text = ("&" if stream == "*" else stream) + arrow + merge
            out.append(_Tok(text, op="redir"))
            i = j
        else:
            buf.append(c)
            i += 1
    flush()
    return out


# ═══════════════════════════════════════════════════════════════════════
# Statements
# ═══════════════════════════════════════════════════════════════════════

#: Statement-leading keywords: dropped so the command behind them is the
#: verb (``while`` itself never runs anything; its condition and body are
#: separate statements because ``(``/``{`` split them).
_KEYWORDS = frozenset({
    "while", "do", "until", "for", "foreach", "if", "elseif", "else",
    "switch", "try", "catch", "finally", "trap", "begin", "process", "end",
    "function", "filter", "param", "dynamicparam", "data", "clean",
})

_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "??="})


@dataclass
class _Stmt:
    words: List[_Tok] = field(default_factory=list)
    #: the operator that ended the previous statement ("" for the first)
    sep: str = ""


def _statements(command: str) -> List[_Stmt]:
    stmts: List[_Stmt] = []
    cur = _Stmt()
    for tok in _tokenize(command):
        if tok.op in _BLOCK_OPS:
            stmts.append(cur)
            cur = _Stmt(sep=tok.op)
            continue
        if tok.op == "&":
            if cur.words:  # trailing `&` = background job (PS 7): a boundary
                stmts.append(cur)
                cur = _Stmt(sep=";")
            continue  # leading `&` = the call operator: peel it
        cur.words.append(tok)
    stmts.append(cur)
    out: List[_Stmt] = []
    for st in stmts:
        words = st.words
        while words and not words[0].quoted and not words[0].op and (
            words[0].text.lower() in _KEYWORDS or words[0].text == "."
        ):
            words = words[1:]
        if (len(words) >= 2 and not words[0].quoted and words[0].text.startswith("$")
                and words[1].text in _ASSIGN_OPS):
            words = words[2:]
        if words:
            out.append(_Stmt(words=words, sep=st.sep))
    return out


def statements(command: str) -> List[List[str]]:
    """The command's statements as word lists (operators and quoting
    resolved). Exposed for tests and debugging."""
    if not isinstance(command, str):
        return []
    return [[w.text for w in st.words] for st in _statements(command)]


# ═══════════════════════════════════════════════════════════════════════
# Cmdlet table
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _Spec:
    canonical: str  # display casing, e.g. "Get-Content"
    aliases: Tuple[str, ...]
    positional: Tuple[str, ...]
    #: canonical param name (lower) -> accepted spellings (lower, incl. itself)
    params: Dict[str, Tuple[str, ...]]
    switches: FrozenSet[str]
    #: the Bash verb the cmdlet translates to by default (None: depends on
    #: the parameters, or no Bash equivalent)
    bash_verb: Optional[str]


_PATH = ("path", "pspath", "lp", "literalpath", "fullname")


def _p(**params: Tuple[str, ...]) -> Dict[str, Tuple[str, ...]]:
    return {k: tuple({k, *v}) for k, v in params.items()}


_SPECS: Tuple[_Spec, ...] = (
    _Spec("Get-Content", ("gc", "cat", "type"), ("path",),
          _p(path=_PATH, tail=("last",), totalcount=("first", "head"), wait=(),
             raw=(), encoding=(), readcount=(), delimiter=(), filter=(),
             include=(), exclude=(), force=(), stream=(), asbytestream=()),
          frozenset({"wait", "raw", "force", "asbytestream"}), "cat"),
    _Spec("Select-String", ("sls",), ("pattern", "path"),
          _p(pattern=(), path=_PATH, simplematch=(), casesensitive=(), quiet=(),
             list=(), notmatch=(), allmatches=(), context=(), encoding=(),
             include=(), exclude=(), inputobject=(), raw=(), noemphasis=(),
             culture=()),
          frozenset({"simplematch", "casesensitive", "quiet", "list", "notmatch",
                     "allmatches", "raw", "noemphasis"}), "grep"),
    _Spec("Start-Sleep", ("sleep",), ("seconds",),
          _p(seconds=("s",), milliseconds=("ms",), duration=()),
          frozenset(), "sleep"),
    _Spec("Get-ChildItem", ("gci", "ls", "dir"), ("path", "filter"),
          _p(path=_PATH, filter=(), include=(), exclude=(), recurse=("s",),
             depth=(), force=(), name=(), directory=("ad", "d"), file=("af",),
             hidden=("ah", "h"), readonly=("ar",), system=("as",),
             attributes=(), followsymlink=()),
          frozenset({"recurse", "force", "name", "directory", "file", "hidden",
                     "readonly", "system", "followsymlink"}), "ls"),
    _Spec("Measure-Object", ("measure",), ("property",),
          _p(property=(), line=(), word=(), character=(), sum=(), average=(),
             maximum=(), minimum=(), ignorewhitespace=(), inputobject=(),
             allstats=(), standarddeviation=()),
          frozenset({"line", "word", "character", "sum", "average", "maximum",
                     "minimum", "ignorewhitespace", "allstats",
                     "standarddeviation"}), "wc"),
    _Spec("Select-Object", ("select",), ("property",),
          _p(first=(), last=(), skip=(), skiplast=(), property=(),
             expandproperty=(), excludeproperty=(), unique=(), index=(),
             wait=(), inputobject=(), casesensitive=()),
          frozenset({"unique", "wait", "casesensitive"}), None),
    _Spec("Set-Location", ("sl", "cd", "chdir"), ("path",),
          _p(path=_PATH, stackname=(), passthru=()),
          frozenset({"passthru"}), "cd"),
    _Spec("Push-Location", ("pushd",), ("path",),
          _p(path=_PATH, stackname=(), passthru=()),
          frozenset({"passthru"}), "cd"),
    _Spec("Pop-Location", ("popd",), (),
          _p(stackname=(), passthru=()), frozenset({"passthru"}), "popd"),
    _Spec("Copy-Item", ("copy", "cp", "cpi"), ("path", "destination"),
          _p(path=_PATH, destination=(), recurse=(), force=(), container=(),
             passthru=(), filter=(), include=(), exclude=(), tosession=(),
             fromsession=()),
          frozenset({"recurse", "force", "container", "passthru"}), "cp"),
    _Spec("Move-Item", ("move", "mv", "mi"), ("path", "destination"),
          _p(path=_PATH, destination=(), force=(), passthru=(), filter=(),
             include=(), exclude=()),
          frozenset({"force", "passthru"}), "mv"),
    _Spec("Set-Content", (), ("path", "value"),
          _p(path=_PATH, value=(), encoding=(), force=(), passthru=(),
             nonewline=(), stream=(), filter=(), include=(), exclude=(),
             asbytestream=()),
          frozenset({"force", "passthru", "nonewline", "asbytestream"}), "tee"),
    _Spec("Add-Content", ("ac",), ("path", "value"),
          _p(path=_PATH, value=(), encoding=(), force=(), passthru=(),
             nonewline=(), stream=(), filter=(), include=(), exclude=(),
             asbytestream=()),
          frozenset({"force", "passthru", "nonewline", "asbytestream"}), "tee"),
    _Spec("Out-File", (), ("filepath", "encoding"),
          _p(filepath=("path",), literalpath=("lp", "pspath"), append=(),
             encoding=(), force=(), noclobber=("nc",), nonewline=(), width=(),
             inputobject=()),
          frozenset({"append", "force", "noclobber", "nonewline"}), "tee"),
    _Spec("Tee-Object", ("tee",), ("filepath",),
          _p(filepath=("path",), literalpath=("lp", "pspath"), variable=(),
             append=(), inputobject=(), encoding=()),
          frozenset({"append"}), "tee"),
    _Spec("New-Item", ("ni",), ("path",),
          _p(path=_PATH, name=(), itemtype=("type",), value=(), force=()),
          frozenset({"force"}), "touch"),
    _Spec("Remove-Item", ("rm", "ri", "del", "erase", "rd", "rmdir"), ("path",),
          _p(path=_PATH, recurse=(), force=(), filter=(), include=(),
             exclude=(), stream=()),
          frozenset({"recurse", "force"}), "rm"),
    _Spec("Receive-Job", ("rcjb",), ("job",),
          _p(job=(), id=(), name=(), instanceid=(), wait=(), keep=(),
             autoremovejob=(), writeevents=(), writejobinresults=(),
             norecurse=(), force=()),
          frozenset({"wait", "keep", "autoremovejob", "writeevents",
                     "writejobinresults", "norecurse", "force"}), None),
    _Spec("Wait-Job", ("wj",), ("job",),
          _p(job=(), id=(), name=(), instanceid=(), state=(), filter=(),
             timeout=(), any=(), force=()),
          frozenset({"any", "force"}), "wait-job"),
    _Spec("Wait-Process", (), ("name",),
          _p(name=("processname",), id=(), inputobject=(), timeout=(), any=(),
             passthru=()),
          frozenset({"any", "passthru"}), "wait-process"),
    _Spec("Wait-Event", (), ("sourceidentifier",),
          _p(sourceidentifier=(), timeout=()), frozenset(), "wait-event"),
    _Spec("Invoke-Expression", ("iex",), ("command",),
          _p(command=()), frozenset(), None),
)

#: Every spelling (lower-cased cmdlet name or alias) → its spec.
_BY_SPELLING: Dict[str, _Spec] = {}
for _s in _SPECS:
    _BY_SPELLING[_s.canonical.lower()] = _s
    for _a in _s.aliases:
        _BY_SPELLING[_a] = _s

#: Every spelling a user can type → the canonical cmdlet name (display
#: casing). Public: the settings-template ``if``-rule tests derive the
#: PowerShell twin rules from it.
SPELLINGS: Dict[str, str] = {k: v.canonical for k, v in _BY_SPELLING.items()}

#: Canonical cmdlet → its default Bash verb (or None).
CMDLET_BASH_VERB: Dict[str, Optional[str]] = {s.canonical: s.bash_verb for s in _SPECS}


def spellings_for_bash_verbs(verbs: Sequence[str]) -> List[str]:
    """The ``if``-rule spellings — canonical cmdlet names, display casing —
    of every cmdlet whose default Bash verb is in ``verbs``: the data the
    PowerShell twin rules are derived from.

    Cmdlet names ONLY. Claude Code canonicalizes common aliases before
    matching a ``PowerShell(...)`` rule, case-insensitively (vendor
    permissions doc, "PowerShell": ``PowerShell(Get-ChildItem *)`` matches
    ``gci``, ``ls`` and ``dir``), so an alias-named rule would be a
    duplicate. The alias table above still serves the TRANSLATOR, which
    reads what the model actually typed."""
    wanted = set(verbs)
    return [spec.canonical for spec in _SPECS if spec.bash_verb in wanted]


# ═══════════════════════════════════════════════════════════════════════
# Binding
# ═══════════════════════════════════════════════════════════════════════


def _param_name(tok: _Tok) -> Optional[Tuple[str, Optional[str]]]:
    """``(name_lower, inline_value)`` for a parameter token, else None."""
    if tok.quoted or tok.op or len(tok.text) < 2 or not tok.text.startswith("-"):
        return None
    body = tok.text[1:]
    if not (body[0].isalpha() or body[0] == "_"):
        return None  # a negative number or `--`
    name, sep, value = body.partition(":")
    return name.lower(), (value if sep else None)


def _resolve_param(spec: _Spec, name: str) -> Optional[str]:
    for canon, spellings in spec.params.items():
        if name in spellings:
            return canon
    hits = {canon for canon, spellings in spec.params.items()
            if any(s.startswith(name) for s in spellings)}
    return hits.pop() if len(hits) == 1 else None


def _bind(spec: _Spec, args: List[_Tok]) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    """``(named, positional_leftover, redirects)`` — redirect operators and
    their targets are returned separately so they survive translation."""
    named: Dict[str, List[str]] = {}
    positional: List[str] = []
    redirects: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.op == "redir":
            redirects.append(tok.text)
            if not _merges(tok.text) and i + 1 < len(args):
                redirects.append(_sink(args[i + 1].text))
                i += 1
            i += 1
            continue
        pn = _param_name(tok)
        if pn is not None:
            canon = _resolve_param(spec, pn[0])
            if canon is None or canon in spec.switches:
                i += 1
                continue
            if pn[1] is not None:
                named.setdefault(canon, []).append(pn[1])
                i += 1
                continue
            if i + 1 < len(args) and not args[i + 1].op:
                named.setdefault(canon, []).append(args[i + 1].text)
                i += 2
                continue
            i += 1
            continue
        positional.append(tok.text)
        i += 1
    # Positional binding, in the cmdlet's declared order, to params not
    # already given by name.
    leftover: List[str] = []
    slots = [p for p in spec.positional if p not in named]
    for value in positional:
        if slots:
            named.setdefault(slots.pop(0), []).append(value)
        else:
            leftover.append(value)
    return named, leftover, redirects


def _switch_set(spec: _Spec, args: List[_Tok]) -> FrozenSet[str]:
    on = set()
    for tok in args:
        pn = _param_name(tok)
        if pn is None:
            continue
        canon = _resolve_param(spec, pn[0])
        if canon in spec.switches and (pn[1] is None or pn[1].lower() != "$false"):
            on.add(canon)
    return frozenset(on)


def _merges(redir: str) -> bool:
    """``2>&1``-style stream merges take no file target."""
    return any(ch.isdigit() for ch in redir.split(">")[-1])


def _sink(target: str) -> str:
    return "/dev/null" if target.lower() == "$null" else _path(target)


def _path(value: str) -> str:
    return value.replace("\\", "/")


def _paths(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        out.extend(_path(p) for p in v.split(",") if p)
    return out


# ═══════════════════════════════════════════════════════════════════════
# Translation
# ═══════════════════════════════════════════════════════════════════════

_MAX_DEPTH = 4


def _first(named: Dict[str, List[str]], *keys: str) -> Optional[str]:
    for k in keys:
        if named.get(k):
            return named[k][0]
    return None


def _emit(spec: _Spec, args: List[_Tok], depth: int) -> List[List[str]]:
    named, rest, redirects = _bind(spec, args)
    sw = _switch_set(spec, args)
    canon = spec.canonical
    paths = _paths(named.get("path", []) + named.get("literalpath", []))
    seg: List[str]
    if canon == "Get-Content":
        if "wait" in sw:
            seg = ["tail", "-f", *paths]
        elif named.get("tail"):
            seg = ["tail", "-n", named["tail"][0], *paths]
        elif named.get("totalcount"):
            seg = ["head", "-n", named["totalcount"][0], *paths]
        else:
            seg = ["cat", *paths]
    elif canon == "Select-String":
        pattern = _first(named, "pattern")
        seg = ["grep"] + (["-F"] if "simplematch" in sw else [])
        seg += ([pattern] if pattern is not None else []) + paths
    elif canon == "Start-Sleep":
        ms = _first(named, "milliseconds")
        secs = _first(named, "seconds", "duration")
        seg = ["sleep", secs if secs is not None else (f"{ms}ms" if ms else "")]
        seg = [t for t in seg if t]
    elif canon == "Get-ChildItem":
        seg = ["ls", *paths]
    elif canon == "Measure-Object":
        seg = ["wc"]
    elif canon == "Select-Object":
        if named.get("last"):
            seg = ["tail", "-n", named["last"][0]]
        elif named.get("first"):
            seg = ["head", "-n", named["first"][0]]
        else:
            seg = ["select-object"]
    elif canon in ("Set-Location", "Push-Location"):
        seg = ["cd", *paths[:1]]
    elif canon == "Pop-Location":
        seg = ["popd"]
    elif canon in ("Copy-Item", "Move-Item"):
        dest = _paths(named.get("destination", []))
        seg = [spec.bash_verb or "cp", *paths, *dest[:1]]
    elif canon in ("Set-Content", "Add-Content"):
        seg = ["tee"] + (["-a"] if canon == "Add-Content" else []) + paths
    elif canon in ("Out-File", "Tee-Object"):
        targets = _paths(named.get("filepath", []) + named.get("literalpath", []))
        seg = ["tee"] + (["-a"] if "append" in sw else []) + targets
    elif canon == "New-Item":
        kind = (_first(named, "itemtype") or "").lower()
        name = _first(named, "name")
        targets = [p.rstrip("/") + "/" + _path(name) for p in paths] if name else paths
        if name and not paths:
            targets = [_path(name)]
        seg = ["mkdir" if kind.startswith("dir") else "touch", *targets]
    elif canon == "Remove-Item":
        seg = ["rm", *paths]
    elif canon == "Receive-Job":
        seg = ["wait-job"] if "wait" in sw else ["receive-job"]
    elif canon in ("Wait-Job", "Wait-Process", "Wait-Event"):
        seg = [canon.lower()] + [a.text for a in args if not a.op][:2]
    elif canon == "Invoke-Expression":
        inner = _first(named, "command")
        return _translate(inner, depth + 1) if inner else []
    else:  # pragma: no cover — every spec has a branch
        seg = [canon.lower()]
    return [seg + redirects] if seg else []


_SHELL_HOSTS = frozenset({"powershell", "pwsh"})


def _verb_key(text: str) -> str:
    base = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _passthrough(words: List[_Tok], depth: int) -> List[List[str]]:
    verb = _verb_key(words[0].text)
    rest = words[1:]
    if verb in _SHELL_HOSTS:
        # powershell / pwsh -Command '<sub>' (or -c): the sub-command runs
        # as PowerShell; read it as such.
        for idx, tok in enumerate(rest):
            pn = _param_name(tok)
            if pn and "command".startswith(pn[0]) and pn[0] != "":
                body = " ".join(w.text for w in rest[idx + 1:] if not w.op)
                return _translate(body, depth + 1)
    if verb == "timeout":
        # Windows timeout.exe (`timeout /t 10 /nobreak`) is a sleep.
        nums = [w.text for w in rest if w.text.isdigit()]
        flags = {w.text.lower() for w in rest if w.text.startswith("/")}
        if nums and len(rest) <= 4 and (flags <= {"/t", "/nobreak"}):
            return [["sleep", nums[0]]]
    # The `$null` redirect sink reads as /dev/null.
    seg = [verb]
    prev_redir = False
    for w in rest:
        seg.append(_sink(w.text) if prev_redir else w.text)
        prev_redir = w.op == "redir" and not _merges(w.text)
    return [seg]


def _segment(words: List[_Tok], depth: int) -> List[List[str]]:
    head = words[0]
    key = head.text.lower()
    if "\\" in key and "-" in key.rsplit("\\", 1)[-1]:
        key = key.rsplit("\\", 1)[-1]  # module-qualified cmdlet
    spec = None if head.quoted else _BY_SPELLING.get(key)
    if spec is not None:
        return _emit(spec, words[1:], depth)
    return _passthrough(words, depth)


def _translate(command: str, depth: int = 0) -> List[List[str]]:
    if depth > _MAX_DEPTH or not command or not command.strip():
        return []
    out: List[List[str]] = []
    for st in _statements(command):
        out.extend(_segment(st.words, depth))
    return out


def _quote(tok: str) -> str:
    if tok in (">", ">>", "|") or (tok and tok[0] in "0123456789&" and ">" in tok
                                     and all(ch in "0123456789&>" for ch in tok)):
        return tok
    return shlex.quote(tok)


def to_bash_equivalent(command: str) -> str:
    """The Bash-equivalent reading of a PowerShell ``command``.

    Statements join with `` ; `` except a pipeline, which keeps its `` | ``
    (``Get-Content f | Select-Object -Last 5`` → ``cat f | tail -n 5``).
    Never raises; ``""`` when nothing parsed."""
    if not isinstance(command, str) or not command.strip():
        return ""
    parts: List[str] = []
    for st in _statements(command):
        segs = _segment(st.words, 0)
        for k, seg in enumerate(segs):
            if not seg:
                continue
            if parts:
                parts.append("|" if (k == 0 and st.sep == "|") else ";")
            parts.append(" ".join(_quote(t) for t in seg))
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# Loop shapes (the wait guard's PowerShell-only rule)
# ═══════════════════════════════════════════════════════════════════════

#: A ``foreach`` range whose upper bound is a type's MaxValue never ends in
#: practice; a literal bound at or above this is treated the same.
UNBOUNDED_RANGE_MIN = 1_000_000

_MAXVALUE_RE = re.compile(
    r"\.\.\s*\[(?:system\.)?(?:int|int32|int64|long|uint32|uint64|double|decimal)\]::maxvalue",
    re.IGNORECASE,
)
_RANGE_LITERAL_RE = re.compile(r"\.\.\s*(\d[\d_]*)")


def _paren_body(toks: List[_Tok], open_idx: int) -> Tuple[List[_Tok], int]:
    """Tokens inside the ``(`` at ``open_idx`` up to its matching ``)``."""
    depth, body = 0, []
    for k in range(open_idx, len(toks)):
        t = toks[k]
        if t.op == "(":
            depth += 1
            if depth == 1:
                continue
        elif t.op == ")":
            depth -= 1
            if depth == 0:
                return body, k
        body.append(t)
    return body, len(toks)


def loop_pattern(command: str) -> Optional[Tuple[str, str]]:
    """``(pattern_name, evidence)`` for a PowerShell loop that waits, else
    ``None`` (v0.2.102 W5-D review W5D-2).

    A PowerShell loop keyword is not a command, so no ``PowerShell(...)``
    ``if`` rule can ever match it; the wait guard therefore runs on EVERY
    PowerShell call and asks this function. Detected, with or without a
    ``Start-Sleep`` inside (a loop with no sleep spins and re-polls just the
    same):

    * ``while (<cond>) { … }`` — any condition;
    * ``do { … } while (…)`` / ``do { … } until (…)``;
    * ``for (<init>; ; <step>)`` — an EMPTY condition (``for (;;)``); a
      ``for`` with a condition is bounded and passes;
    * ``foreach ($x in a..<bound>)`` where the bound is a type's
      ``MaxValue`` or a literal >= :data:`UNBOUNDED_RANGE_MIN`; a
      ``foreach`` over a collection (``foreach ($f in $files)``) passes.

    Keywords are recognised only as unquoted words followed by their
    syntax (``(`` / ``{``), so ``Write-Output "while (x)"`` and the
    ``ForEach-Object`` alias ``foreach { … }`` never match. Never raises."""
    if not isinstance(command, str) or not command.strip():
        return None
    toks = _tokenize(command)
    for i, t in enumerate(toks):
        if t.op or t.quoted:
            continue
        word = t.text.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if word == "do" and nxt is not None and nxt.op == "{":
            return ("do loop", "do { ... } while/until (...)")
        if nxt is None or nxt.op != "(":
            continue
        if word == "while":
            body, _end = _paren_body(toks, i + 1)
            cond = " ".join(b.text for b in body)[:60]
            return ("while loop", f"while ({cond})")
        if word == "for":
            body, _end = _paren_body(toks, i + 1)
            parts: List[List[_Tok]] = [[]]
            for b in body:
                if b.op == ";":
                    parts.append([])
                else:
                    parts[-1].append(b)
            if len(parts) >= 2 and not parts[1]:
                return ("for (;;) loop", "for (...; ; ...)")
        if word == "foreach":
            body, _end = _paren_body(toks, i + 1)
            src = " ".join(b.text for b in body)
            if _MAXVALUE_RE.search(src):
                return ("unbounded foreach", f"foreach ({src[:60]})")
            m = _RANGE_LITERAL_RE.search(src)
            if m and int(m.group(1).replace("_", "")) >= UNBOUNDED_RANGE_MIN:
                return ("unbounded foreach", f"foreach ({src[:60]})")
    return None


def command_for_bash_readers(tool_name: object, command: str) -> str:
    """``command`` as the Bash readers should see it: translated when the
    tool was PowerShell, verbatim otherwise. The one call every consumer
    makes, so the tool-name test lives in one place."""
    if tool_name == POWERSHELL_TOOL:
        return to_bash_equivalent(command)
    return command if isinstance(command, str) else ""


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def _cli_main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "translate":
        sys.stderr.write("usage: python -m vco_lib.powershell_command translate  (stdin: command)\n")
        return 2
    sys.stdout.write(to_bash_equivalent(sys.stdin.read()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
