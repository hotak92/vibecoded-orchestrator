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
    # v0.2.102 W6-Z3: `echo x > f`'s twin. Its `if` rule exists for the
    # redirect write; without a file target it classifies MECHANICAL.
    _Spec("Write-Output", ("echo", "write"), ("inputobject",),
          _p(inputobject=(), noenumerate=()),
          frozenset({"noenumerate"}), "echo"),
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
    elif canon == "Write-Output":
        seg = ["echo", *named.get("inputobject", []), *rest]
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


#: The per-item pipeline cmdlet and its aliases (``foreach`` is the alias
#: only in COMMAND position after a pipe; the keyword takes ``(``).
_PS_FOREACH_OBJECT = frozenset({"foreach-object", "foreach", "%"})


def _brace_body(toks: List[_Tok], open_idx: int) -> Tuple[List[_Tok], int]:
    """Tokens inside the ``{`` at ``open_idx`` up to its matching ``}``."""
    depth = 0
    for k in range(open_idx, len(toks)):
        t = toks[k]
        if t.op == "{":
            depth += 1
        elif t.op == "}":
            depth -= 1
            if depth == 0:
                return toks[open_idx + 1 : k], k
    return toks[open_idx + 1 :], len(toks)


#: PowerShell loop-body verbs that do NO real work — the Bash busy-wait
#: rule's ``echo``/``printf`` twins (output-only cmdlets). A bare
#: expression statement (``$l``, a literal) is output too.
_PS_NOWORK_VERBS = frozenset({
    "write-output", "write-host", "echo", "write-debug", "write-verbose",
    "write-information", "write-progress", "out-null", "out-default",
})

#: A wait ANYWHERE in a loop (condition or body) is a wait in the loop —
#: the same rule the Bash side applies to sleeps in loops. ``sleep`` is
#: Start-Sleep's alias; ``timeout`` covers ``timeout /t``.
_PS_LOOP_WAIT_WORDS = frozenset({"start-sleep", "sleep", "timeout"}) | set(
    PS_WAIT_VERBS)

#: A loop condition that consumes input bounds the loop (it ends at EOF):
#: the ReadLine/read-host family — the PowerShell twin of the Bash
#: ``while read`` exemption.
_PS_INPUT_COND_RE = re.compile(
    r"(?i)(\.read(line)?\b|read-host|\[console\]::in)")

_PS_WAIT_FLAG_RE = re.compile(r"(?i)^-wait$")

#: ``[Threading.Thread]::Sleep(n)`` and its fully-qualified twin (R14 A4).
_PS_STATIC_SLEEP_RE = re.compile(r"(?i)::sleep")

#: The PowerShell no-op sinks: a redirect to ``$null``, a pipe to
#: ``Out-Null``, and an fd duplication write nowhere (R14 A4 — the
#: ``/dev/null`` twin of R10 F2).
_PS_NULL_TARGETS = frozenset({"$null", "null"})

_PS_OUT_NULL_RE = re.compile(r"(?i)^out-null$")

#: A condition's variable references (`$r.StatusCode` → ``r``).
_PS_COND_VAR_RE = re.compile(r"\$(\w+)")


def _ps_loop_has_wait(toks: List[_Tok]) -> bool:
    """A wait anywhere in the token list: a wait verb, a ``-Wait`` flag, a
    static ``[Threading.Thread]::Sleep(n)`` call, or the Windows sleep
    shapes ``ping -n <count>`` / ``timeout /t <secs>`` (R14 A4)."""
    for k, t in enumerate(toks):
        if t.quoted or t.op:
            continue
        low = t.text.lower()
        if low == "ping":
            # `ping` is a sleeper only in its Windows count shape
            # (`ping -n 2 host`); pinging as work stays work.
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if (nxt is not None and not nxt.quoted and nxt.op == ""
                    and nxt.text.lower().startswith("-n")):
                return True
            continue
        if low in _PS_LOOP_WAIT_WORDS or _PS_WAIT_FLAG_RE.match(t.text):
            return True
        if _PS_STATIC_SLEEP_RE.search(low):
            return True
    return False


def _ps_stmt_has_real_output(stmt: List[_Tok]) -> bool:
    """True when the statement's output goes somewhere REAL: a redirect
    whose target is not ``$null`` and not an fd duplication, or a pipe
    whose consumer is not ``Out-Null`` (R14 A4)."""
    for k, t in enumerate(stmt):
        if t.op == "redir":
            nxt = stmt[k + 1] if k + 1 < len(stmt) else None
            if "&" in t.text:
                continue  # `2>&1`: a duplication, writes nowhere new
            if (nxt is not None and not nxt.op
                    and nxt.text.lower() in _PS_NULL_TARGETS):
                continue  # `> $null`
            return True
        if t.op == "|":
            nxt = stmt[k + 1] if k + 1 < len(stmt) else None
            if (nxt is not None and not nxt.op and not nxt.quoted
                    and _PS_OUT_NULL_RE.match(nxt.text)):
                continue  # `| Out-Null`
            return True  # a pipe feeds a consumer: work
    return False


def _ps_stmts(toks: List[_Tok]) -> List[List[_Tok]]:
    """The body split into statements at ``;``."""
    stmt: List[_Tok] = []
    stmts: List[List[_Tok]] = []
    for t in toks:
        if t.op == ";":
            stmts.append(stmt)
            stmt = []
        else:
            stmt.append(t)
    stmts.append(stmt)
    return stmts


def _ps_stmt_does_real_work(stmt: List[_Tok]) -> bool:
    """The Bash busy-wait rule's work test, for ONE PowerShell statement:
    output-only (``Write-Output`` kin, a bare value expression) with no
    real output → no work. ``if``/``switch``/``try`` carry structure,
    ``break``/``continue``/``return`` change control flow, and — R14 A1 —
    an ASSIGNMENT (``$x = …``, ``$i++``) or a method call (``$p.Refresh()``)
    is work: only a single VALUE token counts as a bare expression."""
    if not stmt:
        return False
    head = next((t for t in stmt if not t.op), None)
    if head is None:
        return False  # pure operators (a stray redirect): no command word
    word = head.text.lower().rsplit("\\", 1)[-1]
    if word in ("if", "elseif", "else", "switch", "try", "catch",
                "finally", "break", "continue", "return"):
        return True
    redirected = _ps_stmt_has_real_output(stmt)
    if word in _PS_NOWORK_VERBS and not redirected:
        return False
    if head.text.startswith("$"):
        # A bare expression ONLY as a single value: no assignment, no
        # increment, no call (R14 A1 — `$i++`, `$out += …`, `$x = …` and
        # `$p.Refresh()` are all work; `$l`, `$row.Name` are output).
        # The text includes OPERATOR tokens: a call's `(` is an op.
        text = " ".join(t.text for t in stmt)
        if ("=" not in text and "++" not in text and "--" not in text
                and "(" not in text and not redirected):
            return False
        return True
    if (head.quoted or head.text[0].isdigit()) and not redirected:
        return False  # a literal statement: output, not work
    return True


def _ps_body_does_real_work(toks: List[_Tok]) -> bool:
    """The Bash busy-wait rule's work test, for ONE PowerShell loop body:
    no work iff every statement is output-only (see
    :func:`_ps_stmt_does_real_work`)."""
    return any(_ps_stmt_does_real_work(s) for s in _ps_stmts(toks))


_PS_ASSIGN_RE = re.compile(r"^(\$\w+)\s*(=|\+=|-=)\s*(.*)$")


def _ps_condition_refresh_poll(cond: List[_Tok],
                               body: List[_Tok]) -> Optional[Tuple[str, str]]:
    """The condition-refresh poll rule, PowerShell side (coordinator
    ruling 2026-10-15 — the twin of the wait guard's Bash rule): a loop
    whose ONLY work is assigning the very variables its condition reads
    from an external fetch (``do { $r = Invoke-WebRequest u } until
    ($r.StatusCode -eq 200)``, ``while (-not $ok) { $ok = Test-Path x }``)
    is a poll. Precisely: the condition reads at least one variable, and
    every body statement is either no-work or an assignment to one of
    those variables whose value FETCHES (a command word or a method call
    on the right-hand side — not a literal, not arithmetic on the
    variable itself, which is a counted loop and stays allowed)."""
    cond_vars = set()
    for t in cond:
        if not t.op:
            cond_vars.update(_PS_COND_VAR_RE.findall(t.text))
    if not cond_vars:
        return None
    saw_fetch = False
    for stmt in _ps_stmts(body):
        if not _ps_stmt_does_real_work(stmt):
            continue
        # Full text INCLUDING operator tokens: a method call's `(` is an
        # op, and it is what makes the right-hand side a fetch.
        text = " ".join(t.text for t in stmt)
        m = _PS_ASSIGN_RE.match(text)
        if m is None:
            return None  # work that is not an assignment to a cond var
        if m.group(1)[1:] not in cond_vars:
            return None  # assigns a variable the condition never reads
        rhs = m.group(3).strip()
        if not rhs:
            return None
        first = rhs.split()[0]
        if "(" in rhs:
            saw_fetch = True  # a method call: $q.Dequeue(), a web call…
            continue
        if first.startswith("$") or first[0].isdigit() or first[0] in "'\"":
            continue  # a value/arithmetic: not a fetch (counted loop)
        saw_fetch = True  # a command word: Invoke-WebRequest, Test-Path…
    if saw_fetch:
        return ("condition-refresh poll", "")
    return None


# ── Bounded-total-wait reading (v0.2.105 fix round, owner ruling) ─────
# Used ONLY for loops inside a script FILE (vco_lib.wait_guard passes the
# budget, vco_lib.wait_guard_bounded.WAIT_BUDGET_S). A loop whose
# iteration count is proved by a literal bound and whose every wait is a
# literal Start-Sleep, with iterations x waits-per-iteration within the
# budget, is a bounded retry. Whatever is not read exactly is unbounded.

_PS_SLEEP_VERBS = frozenset({"start-sleep", "sleep"})
_PS_NUM_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_PS_INT_RE = re.compile(r"^-?[0-9]+$")
_PS_RANGE_RE = re.compile(r"^(-?[0-9]+)\.\.(-?[0-9]+)$")
#: Words that make a loop body unreadable for the bound: a nested loop or
#: scriptblock-running construct, control flow that skips an increment,
#: and the variable cmdlets that write a counter by name.
_PS_BOUND_BREAKERS = frozenset({
    "while", "for", "foreach", "do", "until", "function", "filter",
    "workflow", "trap", "continue", "foreach-object", "%", "where-object",
    "?", "switch", "set-variable", "new-variable", "clear-variable",
    "remove-variable", "invoke-command", "icm", "start-job",
})
#: Blocks a counted wait/increment may sit in: they run at most once per
#: iteration. Any other ``{`` (a scriptblock argument, a switch arm) may
#: run any number of times — or never.
_PS_ONCE_BLOCK_KINDS = frozenset({"try", "catch", "finally", "else", "if",
                                  "elseif"})


def _ps_sleep_seconds(stmt: List[_Tok]) -> Optional[float]:
    """Seconds of a literal ``Start-Sleep N`` / ``-Seconds N`` /
    ``-Milliseconds N`` statement (unique-prefix parameter names, ``-ms``
    alias), else ``None``."""
    words = [t for t in stmt if not t.op]
    if (len(words) != len(stmt) or not words or words[0].quoted
            or words[0].text.lower() not in _PS_SLEEP_VERBS):
        return None
    args = [t.text.lower() for t in words[1:]]
    scale = 1.0
    if len(args) == 2:
        flag = args[0]
        if flag == "-ms" or (len(flag) >= 2 and "-milliseconds".startswith(flag)):
            scale = 0.001
        elif not (len(flag) >= 2 and "-seconds".startswith(flag)):
            return None
        args = args[1:]
    if len(args) != 1 or not _PS_NUM_RE.match(args[0]):
        return None
    return float(args[0]) * scale


def _ps_var(text: str) -> Optional[str]:
    """``$name`` → ``name`` (lower-cased; scope-qualified names refused)."""
    m = re.fullmatch(r"\$([A-Za-z_]\w*)", text)
    return m.group(1).lower() if m else None


def _ps_is_increment(stmt: List[_Tok], var: str) -> bool:
    """``$v++`` / ``++$v`` / ``$v += K`` / ``$v = $v + K`` (K > 0)."""
    texts = [t.text.lower() for t in stmt]
    if any(t.quoted for t in stmt):
        return False
    if texts in ([f"${var}++"], [f"++${var}"]):
        return True
    if (len(texts) == 3 and texts[0] == f"${var}" and texts[1] == "+="
            and _PS_INT_RE.match(texts[2]) and int(texts[2]) > 0):
        return True
    return (len(texts) == 5 and texts[0] == f"${var}" and texts[1] == "="
            and texts[2] == f"${var}" and texts[3] == "+"
            and bool(_PS_INT_RE.match(texts[4])) and int(texts[4]) > 0)


_PS_WRITE_FORM_RE = re.compile(r"^(?:\+\+|--)?\$(?:\w+:)?(\w+)(?:\+\+|--)?$")


def _ps_writes_var(stmt: List[_Tok], var: str) -> bool:
    """The statement may write ``$var`` (any assignment operator,
    ``++``/``--`` in either position, scope-qualified, or ``[ref]``)."""
    for k, t in enumerate(stmt):
        if t.quoted or t.op:
            continue
        low = t.text.lower()
        if "[ref]" in low and var in low:
            return True
        m = _PS_WRITE_FORM_RE.match(low)
        if m is None or m.group(1) != var:
            continue
        if low.startswith(("++", "--")) or low.endswith(("++", "--")):
            return True
        nxt = stmt[k + 1] if k + 1 < len(stmt) else None
        if nxt is not None and not nxt.op and nxt.text in _ASSIGN_OPS:
            return True
    return False


def _ps_body_layout(body: List[_Tok]) -> Optional[Tuple[List[Tuple[List[_Tok], Tuple[int, ...]]], Dict[int, str]]]:
    """``(statements, block_kinds)``: every statement of ``body`` with the
    path of brace blocks enclosing it, and each block's kind (the word
    before its ``{``; ``if``/``elseif`` for a ``(…) {`` behind those).
    ``;`` inside parentheses is not a boundary. ``None`` when the braces
    or parens do not balance."""
    stmts: List[Tuple[List[_Tok], Tuple[int, ...]]] = []
    kinds: Dict[int, str] = {}
    stack: List[Tuple[int, int]] = []  # (block id, paren depth outside it)
    cur: List[_Tok] = []
    paren = 0
    paren_heads: List[str] = []  # the word before each open `(`
    last_paren_head = ""
    for k, t in enumerate(body):
        if t.op == "(":
            prev = body[k - 1] if k > 0 else None
            paren_heads.append(prev.text.lower() if prev is not None and not prev.op else "")
            paren += 1
            cur.append(t)
            continue
        if t.op == ")":
            if paren == 0:
                return None
            paren -= 1
            last_paren_head = paren_heads.pop()
            cur.append(t)
            continue
        if t.op == ";" and paren > 0:
            continue
        if t.op in (";", "{", "}"):
            if cur:
                stmts.append((cur, tuple(b for b, _p in stack)))
            if t.op == "{":
                prev = body[k - 1] if k > 0 else None
                if prev is None:
                    kind = ""
                elif prev.op == ")":
                    kind = last_paren_head
                elif not prev.op:
                    kind = prev.text.lower()
                    if kind.startswith("[") and len(cur) >= 2 \
                            and cur[0].text.lower() == "catch":
                        kind = "catch"  # `catch [Type] {`
                else:
                    kind = ""
                bid = len(kinds) + 1
                kinds[bid] = kind
                stack.append((bid, paren))
                paren = 0
            elif t.op == "}":
                if not stack or paren != 0:
                    return None
                _bid, paren = stack.pop()
            cur = []
            continue
        cur.append(t)
    if cur:
        stmts.append((cur, tuple(b for b, _p in stack)))
    if stack or paren:
        return None
    return stmts, kinds


def _ps_counter_init(prefix: List[_Tok], var: str) -> Optional[int]:
    """The literal integer ``$var`` is set to by the run of plain
    ``$x = <literal>`` statements immediately before the loop, else
    ``None``."""
    stmts = _ps_stmts(prefix)
    for stmt in reversed(stmts):
        if not stmt:
            continue
        if (len(stmt) != 3 or any(t.op or t.quoted for t in stmt[:2])
                or stmt[1].text != "=" or _ps_var(stmt[0].text) is None
                or stmt[2].op):
            return None
        value = stmt[2]
        if not (value.quoted or _PS_INT_RE.match(value.text)
                or value.text.lower() in ("$null", "$true", "$false")):
            return None  # a call could write the counter by scope
        if _ps_var(stmt[0].text) == var:
            return int(stmt[2].text) if (
                not stmt[2].quoted and _PS_INT_RE.match(stmt[2].text)) else None
    return None


def _ps_while_iterations(cond: List[_Tok], prefix: List[_Tok],
                         body_stmts: List[List[_Tok]]) -> Optional[Tuple[str, int]]:
    """``(counter, max iterations)`` for ``while (… -and $v -lt N -and …)``
    whose counter starts at a literal and is only ever increased in the
    body, else ``None``."""
    if any(t.op or t.quoted for t in cond):
        return None
    words = [t.text.lower() for t in cond]
    if any(w in ("-or", "-xor", "-not", "-bor", "-band") or w.startswith("!")
           for w in words):
        return None
    terms: List[List[str]] = [[]]
    for w in words:
        if w == "-and":
            terms.append([])
        else:
            terms[-1].append(w)
    for term in terms:
        if (len(term) == 3 and _ps_var(term[0]) is not None
                and term[1] in ("-lt", "-le") and _PS_INT_RE.match(term[2])):
            var = _ps_var(term[0])
            assert var is not None
            if any(_ps_writes_var(s, var) and not _ps_is_increment(s, var)
                   for s in body_stmts):
                return None
            init = _ps_counter_init(prefix, var)
            if init is None:
                return None
            n = int(term[2]) - init + (1 if term[1] == "-le" else 0)
            return var, max(0, n)
    return None


def _ps_for_iterations(header: List[_Tok]) -> Optional[Tuple[str, int]]:
    """``(var, iterations)`` for ``for ($i = A; $i -lt B; $i++)``."""
    parts = _ps_stmts(header)
    if len(parts) != 3 or any(t.op or t.quoted for p in parts for t in p):
        return None
    init, cond, step = ([t.text.lower() for t in p] for p in parts)
    if not (len(init) == 3 and init[1] == "=" and _PS_INT_RE.match(init[2])):
        return None
    var = _ps_var(init[0])
    if (var is None or len(cond) != 3 or cond[0] != f"${var}"
            or cond[1] not in ("-lt", "-le") or not _PS_INT_RE.match(cond[2])):
        return None
    if step not in ([f"${var}++"], [f"++${var}"], [f"${var}", "+=", "1"]):
        return None
    n = int(cond[2]) - int(init[2]) + (1 if cond[1] == "-le" else 0)
    return var, max(0, n)


def has_loop(command: str) -> bool:
    """True when ``command`` contains a PowerShell loop of any kind — a
    loop keyword followed by its syntax, a ``do {`` or a per-item
    ``| ForEach-Object`` stage. The wait guard drops the bounded-wait
    budget for a script run from such a command: the loop around the call
    multiplies the script's wait by an unknown count."""
    toks = _tokenize(command) if isinstance(command, str) else []
    for i, t in enumerate(toks):
        if t.op or t.quoted:
            continue
        word = t.text.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is None:
            continue
        if word in ("while", "for", "foreach", "until") and nxt.op == "(":
            return True
        if word == "do" and nxt.op == "{":
            return True
        if (word in _PS_FOREACH_OBJECT and i > 0 and toks[i - 1].op == "|"):
            return True
    return False


def loop_wait_bound(label: str, cond: List[_Tok], header: List[_Tok],
                    body: List[_Tok], prefix: List[_Tok]) -> Optional[float]:
    """Worst-case total wait, in seconds, of ONE loop :func:`loop_pattern`
    found a wait in — or ``None`` when it cannot be proved: the wait sits
    in the condition/header, a wait is not a literal ``Start-Sleep``, the
    body nests a loop or runs a scriptblock per item, or the iteration
    count has no literal bound. Bounded shapes: ``for ($i = A; $i -lt B;
    $i++)`` whose body never writes ``$i``; ``foreach ($x in A..B)``; and
    the counter retry ``$n = 0; while (… -and $n -lt N) { … }`` where
    every wait shares a once-per-iteration block (``try``/``catch``/
    ``finally``/``if``/``else``, never inside a ``try`` — an exception
    caught in the body could skip the increment) with a plain ``$n++``,
    and ``$n`` is never otherwise written."""
    if _ps_loop_has_wait(cond) or _ps_loop_has_wait(header):
        return None
    for t in body:
        if t.op or t.quoted:
            continue
        if t.text.lower() in _PS_BOUND_BREAKERS:
            return None
    layout = _ps_body_layout(body)
    if layout is None:
        return None
    stmts, kinds = layout
    per_iteration = 0.0
    waits: List[Tuple[int, ...]] = []
    for stmt, path in stmts:
        if not _ps_loop_has_wait(stmt):
            continue
        secs = _ps_sleep_seconds(stmt)
        if secs is None:
            return None
        if any(kinds[b] not in _PS_ONCE_BLOCK_KINDS or kinds[b] == "try"
               for b in path):
            return None
        per_iteration += secs
        waits.append(path)
    plain = [s for s, _p in stmts]
    if label == "for loop":
        found = _ps_for_iterations(header)
        if found is None or any(_ps_writes_var(s, found[0]) for s in plain):
            return None
        return found[1] * per_iteration
    if label == "foreach loop":
        words = [t for t in cond if not t.op]
        if (len(words) != 3 or len(cond) != 3 or words[1].text.lower() != "in"
                or words[2].quoted):
            return None
        m = _PS_RANGE_RE.match(words[2].text)
        if m is None:
            return None
        return (abs(int(m.group(2)) - int(m.group(1))) + 1) * per_iteration
    if label != "while loop":
        return None
    found = _ps_while_iterations(cond, prefix, plain)
    if found is None:
        return None
    var, n = found
    # Every wait shares a block (its own or an enclosing one, up to the
    # body) with a top-level plain increment of the counter.
    inc_paths = {p for s, p in stmts if _ps_is_increment(s, var)}
    for path in waits:
        if not any(path[:d] in inc_paths for d in range(len(path), -1, -1)):
            return None
    return n * per_iteration


def loop_pattern(command: str,
                 wait_budget_s: float = 0.0) -> Optional[Tuple[str, str]]:
    """``(pattern_name, evidence)`` for a PowerShell loop the wait guard
    denies, else ``None`` (v0.2.102 W5-D; ALIGNED with the Bash rule by
    review R10, 2026-10-15 — before, EVERY ``while``/``do`` loop was
    denied, which was stricter than the owner's ruling).

    A PowerShell loop keyword is not a command, so no ``PowerShell(...)``
    ``if`` rule can ever match it; the wait guard therefore runs on EVERY
    PowerShell call and asks this function. A loop is denied exactly when
    its Bash twin would be:

    * it CONTAINS A WAIT — ``Start-Sleep``/``sleep``, a ``Wait-*`` cmdlet,
      a ``-Wait`` flag, ``[Threading.Thread]::Sleep(n)`` or the Windows
      sleep shapes ``ping -n <count>`` / ``timeout /t`` in the condition
      or the body (a sleep inside a loop is denied on the Bash side
      whatever its position);
    * or it is a BUSY-WAIT — an unbounded loop (``while``/``do … while/
      until``, ``for (;;)``, an unbounded ``foreach`` range) whose body
      does no real work: every statement empty, output-only
      (``Write-Output`` kin, a bare value expression — an assignment,
      increment or method call IS work, R14 A1) with no real output
      (``> $null`` and ``| Out-Null`` write nowhere, R14 A4);
    * or it is a CONDITION-REFRESH POLL (coordinator ruling 2026-10-15,
      the twin of the wait guard's Bash rule) — its only work assigns the
      very variables the condition reads from an external fetch.

    A loop doing real work passes (``foreach ($f in Get-ChildItem) { ruff
    check $f }``); a condition that consumes input bounds the loop
    (``while (($l = $r.ReadLine()) -ne $null) { $l }`` — the ``while
    read`` twin); a bounded ``for`` (non-empty middle clause), a
    ``foreach`` over a collection and a ``… | ForEach-Object { … }``
    stage are never busy-waits or polls — but a WAIT inside them still
    denies, as a sleep inside a Bash ``for`` or an xargs per-item command
    does (v0.2.103 Windows verification: before, a bounded loop's wait
    was never looked at).

    Keywords are recognised only as unquoted words followed by their
    syntax (``(`` / ``{``), so ``Write-Output "while (x)"`` and the
    ``ForEach-Object`` alias ``foreach { … }`` never match. Never raises.

    ``wait_budget_s`` (v0.2.105 fix round; the wait guard passes it only
    for a loop inside a script FILE): a loop whose worst-case total wait
    :func:`loop_wait_bound` proves within the budget is not denied for
    its wait — it is still judged as a busy-wait / condition-refresh
    poll."""
    if not isinstance(command, str) or not command.strip():
        return None
    toks = _tokenize(command)
    skip_past = -1  # a do-loop's trailing condition is not a NEW loop head
    for i, t in enumerate(toks):
        if t.op or t.quoted or i <= skip_past:
            continue
        word = t.text.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        # (label, condition tokens, body tokens, unbounded)
        label: Optional[str] = None
        evidence = ""
        cond: List[_Tok] = []
        body: List[_Tok] = []
        # A `for (init; cond; repeat)` header: every clause runs as part
        # of the loop, so a wait in ANY of them is a loop wait (R20 M2 —
        # the repeat clause escaped when only `cond` was scanned; the Bash
        # twin is wait_guard._arith_for_clauses).
        header: List[_Tok] = []
        unbounded = False
        if word == "do" and nxt is not None and nxt.op == "{":
            body, end = _brace_body(toks, i + 1)
            rest = toks[end + 1:]
            label, evidence, unbounded = "do loop", "do { ... } while/until (...)", True
            if (len(rest) >= 2 and rest[0].op == ""
                    and rest[0].text.lower() in ("while", "until")
                    and rest[1].op == "("):
                cond, cond_end = _paren_body(rest, 1)
                skip_past = end + 1 + cond_end  # absolute index of the `)`
        elif nxt is not None and nxt.op == "(":
            paren, pend = _paren_body(toks, i + 1)
            if word == "while":
                cond = paren
                body, _ = _brace_body(toks, pend + 1)
                label = "while loop"
                evidence = f"while ({' '.join(b.text for b in cond)[:60]})"
                unbounded = True
            elif word == "for":
                parts: List[List[_Tok]] = [[]]
                for b in paren:
                    if b.op == ";":
                        parts.append([])
                    else:
                        parts[-1].append(b)
                bounded = len(parts) >= 2 and bool(parts[1])
                cond = parts[1] if bounded else []
                header = paren
                body, _ = _brace_body(toks, pend + 1)
                if not bounded:
                    label = "for (;;) loop"
                    evidence = "for (...; ; ...)"
                    unbounded = True
                else:
                    # A bounded for is judged for a WAIT only (the Bash
                    # twin `for ((i=0;i<n;i++)); do sleep 1; …` denies).
                    label = "for loop"
                    evidence = f"for ({' '.join(b.text for b in paren)[:60]})"
            elif word == "foreach":
                src = " ".join(b.text for b in paren)
                cond = paren
                body, _ = _brace_body(toks, pend + 1)
                if _MAXVALUE_RE.search(src) or (
                    (m := _RANGE_LITERAL_RE.search(src))
                    and int(m.group(1).replace("_", "")) >= UNBOUNDED_RANGE_MIN
                ):
                    label = "unbounded foreach"
                    evidence = f"foreach ({src[:60]})"
                    unbounded = True
                else:
                    # A foreach over a collection: judged for a WAIT only
                    # (the Bash twin `for f in a b; do sleep 1; …` denies).
                    label = "foreach loop"
                    evidence = f"foreach ({src[:60]})"
        elif (word in _PS_FOREACH_OBJECT and nxt is not None and nxt.op == "{"
                and i > 0 and toks[i - 1].op == "|"):
            # The per-item pipeline stage — `… | ForEach-Object { … }` (and
            # its `foreach` / `%` aliases): the twin of an xargs per-item
            # command, judged for a WAIT only (bounded by its input).
            body, _ = _brace_body(toks, i + 1)
            label = "ForEach-Object loop"
            evidence = f"{t.text} {{ ... }}"
        if label is None:
            continue
        if (_ps_loop_has_wait(cond) or _ps_loop_has_wait(header)
                or _ps_loop_has_wait(body)):
            bound = (loop_wait_bound(label, cond, header, body, toks[:i])
                     if wait_budget_s > 0 else None)
            if bound is None or bound > wait_budget_s:
                return (label, evidence)
        cond_text = " ".join(b.text for b in cond)
        if _PS_INPUT_COND_RE.search(cond_text):
            continue  # bounded by its input: never a busy-wait
        if not unbounded:
            continue  # a bounded for/foreach: never a busy-wait
        if _ps_body_does_real_work(body):
            poll = _ps_condition_refresh_poll(cond, body)
            if poll is not None:
                return ("condition-refresh poll", evidence)
            continue
        return ("busy-wait loop", evidence)
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
