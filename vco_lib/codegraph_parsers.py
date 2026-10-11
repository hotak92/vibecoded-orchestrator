# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Code-graph parsers on demand: detect, offer, install (v0.2.102 W6-L).

The code graph reads non-Python source through tree-sitter grammars
(:mod:`vco_lib.codegraph_grammars`). The default set ships in the
``codegraph-ts`` extra, which ``install.py`` installs; two parsers are
OPTIONAL and never installed by default (owner ruling 2026-10-11, item 3.3):
PowerShell (``tree-sitter-powershell``) and Protocol Buffers (``proto`` from
``tree-sitter-language-pack``). Instead VCO finds out which languages each
registered project contains and the launcher OFFERS the parsers that are
missing, in a modal, before anything is installed.

This module is the one home for that:

* **Catalogue** (:func:`catalogue`) — one :class:`Parser` per installable
  unit, derived from the grammar table; pins come from ``pyproject.toml``
  (:func:`read_pins`), never from a second list.
* **Detection** (:func:`project_languages`) — the code graph's OWN language
  rules: the extension table :data:`vco_lib.inject_intent.EXT_TO_LANG`
  (parity-pinned to the analyzer's ``_EXT_TO_DISPATCH_NAME``) and the walk's
  ignore set :data:`vco_lib.codegraph_row_classify.CODEGRAPH_IGNORE_PARTS`
  (``.claude/`` too, except in the orchestrator root, as the analyzer does).
  Re-run at every project startup, cached by a cheap signature: the mtimes of
  the project's directories (adding, removing or renaming a file changes its
  directory's mtime; editing one does not change the language set). A
  matching signature costs one ``stat`` per directory and no listing.
* **Offers** (:func:`compute_offers`) — a parser is offered when a project
  needs one of its grammars, the grammar does not load, and the user has not
  declined it for that same need. Never offered: a parser whose grammars all
  load, and a package already installed at its pin that still does not load
  (reported as ``broken`` — reinstalling would change nothing).
* **"Not now"** (:func:`record_decline`) — remembered per parser as the set
  of ``(project, language)`` pairs that needed it when declined. The parser is
  offered again only when a NEW pair appears: its language shows up in a
  project that did not contain it (a blank project gaining PowerShell, or a
  newly registered project). Installing the parser clears its record.
* **Install** (:func:`install` / :func:`install_parsers`) — the ONE installer
  helper, shared by the launcher modal and ``install.py`` (which keeps
  installing the default extra as before). Installs into the venv of the
  interpreter it is given — VCO's one venv, so once per machine, never per
  project — then pre-fetches the language-pack grammars and verifies every
  grammar loads in a FRESH interpreter. Every step is logged to
  :func:`install_log_path`; a failure comes back as a result with the message
  and the log tail, never swallowed.

State lives in ``<vct_root_dir>/codegraph-parsers.json`` (per machine).

After an install the next code-graph walk re-extracts exactly the files of
the languages that gained a parser — see
:func:`vco_lib.codegraph_defs.regex_fallback_hash_salt`.

CLI (machine contract: one JSON object on stdout)::

    python -m vco_lib.codegraph_parsers offer   --project DIR [--project DIR ...]
    python -m vco_lib.codegraph_parsers decline --parsers a,b --project DIR [...]
    python -m vco_lib.codegraph_parsers install --parsers a,b | --all
    python -m vco_lib.codegraph_parsers verify  --parsers a,b   (internal)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_BASENAME = "codegraph-parsers.json"
LOG_BASENAME = "codegraph-parsers.log"

#: The core every grammar binds to; its pin is read from the default extra.
CORE_PACKAGE = "tree-sitter"
DEFAULT_EXTRA = "codegraph-ts"
PACK_PACKAGE = "tree-sitter-language-pack"

#: A project with more directories than this is still scanned, but its
#: directory list is not cached (the signature would cost more than it saves).
MAX_CACHED_DIRS = 20000

#: pip install timeout for the modal path (the pack's bundle download runs in
#: the verify step and has its own ~60 s per-attempt timeout).
PIP_TIMEOUT_S = 900
VERIFY_TIMEOUT_S = 600

#: install.py's robustness flags (``_pip_install_flags``) — repeated here as
#: the default for the launcher path, which has no install.py to ask.
DEFAULT_PIP_FLAGS: Tuple[str, ...] = ("--timeout", "60", "--retries", "5", "--prefer-binary")

#: Display names. The language ids are the code graph's dispatch keys.
LANGUAGE_LABELS: Dict[str, str] = {
    "python": "Python", "rust": "Rust", "javascript": "JavaScript",
    "typescript": "TypeScript", "go": "Go", "java": "Java", "csharp": "C#",
    "cpp": "C/C++", "ruby": "Ruby", "lua": "Lua", "shell": "Shell",
    "powershell": "PowerShell", "proto": "Protocol Buffers", "svelte": "Svelte",
}


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Parser:
    """One installable parser: a pip distribution and the grammars it gives."""

    id: str
    label: str
    package: str
    grammars: Tuple[str, ...]
    extra: str
    optional: bool
    pack_grammars: Tuple[str, ...] = ()


def catalogue() -> Dict[str, Parser]:
    """Every parser, keyed by id, derived from :mod:`codegraph_grammars`."""
    from vco_lib import codegraph_grammars as cg

    by_module: Dict[str, List[str]] = {}
    for gid, (module, _factory) in cg.GRAMMARS.items():
        by_module.setdefault(module, []).append(gid)
    out: Dict[str, Parser] = {}
    for module, gids in by_module.items():
        pid = gids[0]
        optional = pid in cg.OPTIONAL_GRAMMARS
        out[pid] = Parser(
            id=pid, label=LANGUAGE_LABELS.get(pid, pid),
            package=module.replace("_", "-"), grammars=tuple(gids),
            extra=f"{DEFAULT_EXTRA}-{pid}" if optional else DEFAULT_EXTRA,
            optional=optional,
        )
    for gid in cg.PACK_GRAMMARS:
        out[gid] = Parser(
            id=gid, label=LANGUAGE_LABELS.get(gid, gid), package=PACK_PACKAGE,
            grammars=(gid,), extra=f"{DEFAULT_EXTRA}-{gid}",
            optional=gid in cg.OPTIONAL_GRAMMARS, pack_grammars=(gid,),
        )
    return out


def parser_for_grammar(grammar: str) -> Optional[Parser]:
    return next((p for p in catalogue().values() if grammar in p.grammars), None)


def grammars_needed(language: str) -> "frozenset[str]":
    """Grammar ids the code graph uses for ``language`` (definitions + calls)."""
    from vco_lib.codegraph_calls import call_grammars
    from vco_lib.codegraph_defs import definition_grammars

    return definition_grammars(language) | call_grammars(language)


def orchestrator_root() -> Path:
    """The checkout this module runs from (``pyproject.toml`` lives there)."""
    return Path(__file__).resolve().parent.parent


def read_pins(pyproject: Optional[Path] = None) -> Dict[str, str]:
    """``package -> "package == X.Y.Z"`` from every ``codegraph-ts*`` extra.
    Raises when the file or an extra cannot be read (a broken checkout)."""
    import tomllib

    path = pyproject or (orchestrator_root() / "pyproject.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    pins: Dict[str, str] = {}
    for name, reqs in extras.items():
        if name != DEFAULT_EXTRA and not name.startswith(DEFAULT_EXTRA + "-"):
            continue
        for req in reqs:
            pkg = req.split("==")[0].strip()
            if "==" not in req:
                raise ValueError(f"{path}: [{name}] {req!r} is not an exact pin")
            prev = pins.get(pkg)
            if prev is not None and prev.replace(" ", "") != req.replace(" ", ""):
                raise ValueError(f"{path}: {pkg} pinned twice differently ({prev!r} / {req!r})")
            pins[pkg] = req
    return pins


def requirement_specs(parser_ids: Iterable[str], pyproject: Optional[Path] = None) -> List[str]:
    """The exact pip requirements for ``parser_ids``: the core pin first, then
    each parser's package pin. Raises on an unknown parser or a missing pin."""
    cat = catalogue()
    pins = read_pins(pyproject)
    specs = [pins[CORE_PACKAGE]] if CORE_PACKAGE in pins else []
    if not specs:
        raise ValueError(f"no '{CORE_PACKAGE}' pin in the {DEFAULT_EXTRA} extra")
    for pid in parser_ids:
        parser = cat.get(pid)
        if parser is None:
            raise ValueError(f"unknown parser '{pid}' (known: {', '.join(sorted(cat))})")
        spec = pins.get(parser.package)
        if spec is None:
            raise ValueError(f"no pin for {parser.package} in pyproject.toml [{parser.extra}]")
        if spec not in specs:
            specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def state_path() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / STATE_BASENAME


def install_log_path() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / "logs" / LOG_BASENAME


def load_state(path: Optional[Path] = None) -> Dict:
    p = path or state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        logger.warning("codegraph_parsers: unreadable state %s (%s); starting fresh", p, exc)
        data = {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        data = {}
    data.setdefault("version", STATE_VERSION)
    data.setdefault("projects", {})
    data.setdefault("declined", {})
    return data


def save_state(state: Dict, path: Optional[Path] = None) -> None:
    from vco_lib.atomic import atomic_write_json

    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, state, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _ignore_set(root: Path) -> "frozenset[str]":
    from vco_lib.codegraph_row_classify import CODEGRAPH_IGNORE_PARTS
    from vco_lib.paths import looks_like_orchestrator_root

    if looks_like_orchestrator_root(root):
        return CODEGRAPH_IGNORE_PARTS
    return CODEGRAPH_IGNORE_PARTS | frozenset({".claude"})


def scan_project(root: Path) -> Tuple[Set[str], List[Tuple[str, int]]]:
    """Walk ``root`` with the code graph's rules. Returns (languages found,
    ``[(relative dir, mtime_ns)]`` for every directory walked)."""
    from vco_lib.codegraph_row_classify import CODEGRAPH_SKIP_SUFFIXES
    from vco_lib.inject_intent import language_for_path

    ignore = _ignore_set(root)
    languages: Set[str] = set()
    dirs: List[Tuple[str, int]] = []
    for current, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(current, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        try:
            dirs.append((rel, os.stat(current).st_mtime_ns))
        except OSError:
            continue
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ignore and not (rel == ".claude" and d == "state")
        )
        for name in filenames:
            if name.startswith("vite.config") or name.endswith(tuple(CODEGRAPH_SKIP_SUFFIXES)):
                continue
            lang = language_for_path(name)
            if lang:
                languages.add(lang)
    return languages, dirs


def _signature(entries: Iterable[Tuple[str, int]]) -> str:
    h = hashlib.sha256()
    for rel, mtime in entries:
        h.update(f"{rel}\0{mtime}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _cached_signature_matches(root: Path, cached: Dict) -> bool:
    dirs = cached.get("dirs")
    if not isinstance(dirs, list) or not dirs or not cached.get("signature"):
        return False
    entries: List[Tuple[str, int]] = []
    for rel in dirs:
        try:
            entries.append((rel, os.stat(root / rel if rel else root).st_mtime_ns))
        except OSError:
            return False
    return _signature(entries) == cached["signature"]


def project_languages(root: Path, state: Dict) -> List[str]:
    """Languages ``root`` contains — from the cache when its signature still
    matches, else by a fresh scan (stored back into ``state``)."""
    key = str(root.resolve())
    cached = state["projects"].get(key)
    if isinstance(cached, dict) and _cached_signature_matches(root, cached):
        return sorted(cached.get("languages") or [])
    languages, dirs = scan_project(root)
    entry: Dict = {"languages": sorted(languages),
                   "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if len(dirs) <= MAX_CACHED_DIRS:
        entry["dirs"] = [rel for rel, _ in dirs]
        entry["signature"] = _signature(dirs)
    state["projects"][key] = entry
    return sorted(languages)


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------


def grammar_loads(grammar: str) -> bool:
    from vco_lib.codegraph_grammars import load_language

    return load_language(grammar) is not None


def package_installed_at_pin(package: str, pins: Dict[str, str]) -> bool:
    from importlib import metadata

    try:
        have = metadata.version(package)
    except metadata.PackageNotFoundError:
        return False
    want = pins.get(package, "").split("==")[-1].strip()
    return bool(want) and have == want


def _pack_grammar_on_disk(grammar: str) -> bool:
    from vco_lib.codegraph_grammars import pack_grammar_downloaded

    return pack_grammar_downloaded(grammar)


@dataclass
class Offer:
    id: str
    label: str
    package: str
    optional: bool
    languages: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)


@dataclass
class OfferReport:
    offers: List[Offer] = field(default_factory=list)
    #: installed at the pinned version yet not loading — never offered.
    broken: List[Dict[str, str]] = field(default_factory=list)
    #: per project: the languages found.
    languages: Dict[str, List[str]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {"offers": [asdict(o) for o in self.offers], "broken": self.broken,
                "languages": self.languages, "errors": self.errors}


def _declined_pairs(state: Dict, pid: str) -> Set[Tuple[str, str]]:
    raw = state.get("declined", {}).get(pid) or []
    return {(p, lang) for p, lang in raw if isinstance(p, str) and isinstance(lang, str)}


def compute_offers(
    project_paths: Sequence[Path], state: Dict, *,
    loads: Callable[[str], bool] = grammar_loads,
    installed: Optional[Callable[[str], bool]] = None,
    pack_on_disk: Callable[[str], bool] = _pack_grammar_on_disk,
) -> OfferReport:
    """Which parsers to offer for ``project_paths`` (see the module docstring
    for the rules). Mutates ``state`` only to refresh the detection cache."""
    report = OfferReport()
    cat = catalogue()
    if installed is None:
        try:
            pins = read_pins()
        except Exception as exc:  # noqa: BLE001 — a broken checkout is reported
            report.errors.append(f"cannot read the parser pins: {exc}")
            pins = {}
        installed = lambda pkg: package_installed_at_pin(pkg, pins)  # noqa: E731
    needs: Dict[str, Set[Tuple[str, str]]] = {}
    load_cache: Dict[str, bool] = {}
    for raw in project_paths:
        root = Path(raw)
        if not root.is_dir():
            report.errors.append(f"{root}: not a directory; skipped")
            continue
        key = str(root.resolve())
        try:
            langs = project_languages(root, state)
        except Exception as exc:  # noqa: BLE001 — one unreadable project never hides the others
            report.errors.append(f"{root}: scan failed ({type(exc).__name__}: {exc})")
            continue
        report.languages[key] = langs
        for lang in langs:
            for grammar in sorted(grammars_needed(lang)):
                if grammar not in load_cache:
                    load_cache[grammar] = loads(grammar)
                if load_cache[grammar]:
                    continue
                parser = next((p for p in cat.values() if grammar in p.grammars), None)
                if parser is not None:
                    needs.setdefault(parser.id, set()).add((key, lang))
    for pid in sorted(needs):
        parser = cat[pid]
        complete_on_disk = installed(parser.package) and all(
            pack_on_disk(g) for g in parser.pack_grammars)
        if complete_on_disk:
            report.broken.append({"id": pid, "package": parser.package,
                                  "reason": "installed at the pinned version but its "
                                            "grammar does not load; see the code-graph log"})
            continue
        pairs = needs[pid]
        if pairs <= _declined_pairs(state, pid):
            continue
        report.offers.append(Offer(
            id=pid, label=parser.label, package=parser.package, optional=parser.optional,
            languages=sorted({lang for _, lang in pairs}),
            projects=sorted({p for p, _ in pairs}),
        ))
    return report


def record_decline(state: Dict, parser_ids: Iterable[str], project_paths: Sequence[Path],
                   *, loads: Callable[[str], bool] = grammar_loads) -> Dict[str, List[List[str]]]:
    """"Not now": remember, per parser, the (project, language) pairs that
    needed it, so the same need is not offered again (a new pair is)."""
    cat = catalogue()
    wanted = [pid for pid in parser_ids if pid in cat]
    recorded: Dict[str, List[List[str]]] = {}
    for raw in project_paths:
        root = Path(raw)
        if not root.is_dir():
            continue
        key = str(root.resolve())
        for lang in project_languages(root, state):
            for pid in wanted:
                if set(cat[pid].grammars) & grammars_needed(lang):
                    recorded.setdefault(pid, []).append([key, lang])
    declined = state.setdefault("declined", {})
    for pid, pairs in recorded.items():
        merged = {tuple(p) for p in declined.get(pid, [])} | {tuple(p) for p in pairs}
        declined[pid] = sorted([list(p) for p in merged])
    return recorded


# ---------------------------------------------------------------------------
# Install — the ONE helper (launcher modal + install.py)
# ---------------------------------------------------------------------------

#: ``runner(argv, timeout) -> (returncode, stdout, stderr)``; raises
#: ``subprocess.TimeoutExpired`` / ``OSError`` on a hung or unstartable child.
Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]


def subprocess_runner(argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    """The real runner. Inherits the caller's environment (proxy variables
    reach pip and the pack's download). ``PYTHONPATH`` is emptied for pip —
    the same scrub as install.py's ``_pip_subprocess_env`` — and set to the
    orchestrator checkout for the ``vco_lib`` verify step."""
    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    is_pip = len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pip"
    env["PYTHONPATH"] = "" if is_pip else str(orchestrator_root())
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                          env=env, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class InstallResult:
    ok: bool
    message: str
    installed: List[str] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)
    log_path: str = ""
    log_tail: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def _log(lines: Iterable[str], log_path: Optional[Path]) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{stamp} {line.rstrip()}\n")
    except OSError as exc:
        logger.warning("codegraph_parsers: cannot write %s (%s)", log_path, exc)


def _tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def install(
    *, python: Path | str, targets: Sequence[str], label: str,
    verify_parsers: Sequence[str] = (), runner: Runner = subprocess_runner,
    pip_flags: Sequence[str] = DEFAULT_PIP_FLAGS, log_path: Optional[Path] = None,
    pip_timeout: int = PIP_TIMEOUT_S,
) -> InstallResult:
    """pip-install ``targets`` into ``python``'s venv, then (when
    ``verify_parsers``) download their pack grammars and check every grammar
    loads, in a fresh interpreter. Never raises."""
    argv = [str(python), "-m", "pip", "install", *pip_flags, *targets]
    _log([f"[{label}] $ {' '.join(argv)}"], log_path)
    try:
        code, out, err = runner(argv, pip_timeout)
    except subprocess.TimeoutExpired:
        msg = f"pip install timed out after {pip_timeout} s"
        _log([f"[{label}] FAILED: {msg}"], log_path)
        return InstallResult(False, f"Installing the {label} failed: {msg}.",
                             failed=[{"id": label, "error": msg}],
                             log_path=str(log_path or ""))
    except OSError as exc:
        msg = f"could not start pip ({exc})"
        _log([f"[{label}] FAILED: {msg}"], log_path)
        return InstallResult(False, f"Installing the {label} failed: {msg}.",
                             failed=[{"id": label, "error": msg}],
                             log_path=str(log_path or ""))
    _log([f"[{label}] pip exit {code}", *out.splitlines()[-40:], *err.splitlines()[-40:]], log_path)
    if code != 0:
        tail = _tail(err or out)
        return InstallResult(False, f"Installing the {label} failed: pip exited {code}.",
                             failed=[{"id": label, "error": tail or f"pip exited {code}"}],
                             log_path=str(log_path or ""), log_tail=tail)
    if not verify_parsers:
        return InstallResult(True, f"Installed the {label}.", installed=[label],
                             log_path=str(log_path or ""))
    vargv = [str(python), "-m", "vco_lib.codegraph_parsers", "verify",
             "--parsers", ",".join(verify_parsers)]
    _log([f"[{label}] $ {' '.join(vargv)}"], log_path)
    try:
        vcode, vout, verr = runner(vargv, VERIFY_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        msg = f"verification did not finish ({type(exc).__name__}: {exc})"
        _log([f"[{label}] FAILED: {msg}"], log_path)
        return InstallResult(False, f"The {label} were installed but {msg}.",
                             failed=[{"id": p, "error": msg} for p in verify_parsers],
                             log_path=str(log_path or ""))
    _log([f"[{label}] verify exit {vcode}", *vout.splitlines()[-40:], *verr.splitlines()[-40:]], log_path)
    try:
        verdict = json.loads(vout.strip().splitlines()[-1]) if vout.strip() else {}
    except ValueError:
        verdict = {}
    results = verdict.get("parsers") if isinstance(verdict, dict) else None
    if not isinstance(results, dict):
        tail = _tail(verr or vout)
        return InstallResult(False, f"The {label} were installed but could not be verified "
                             f"(verify exited {vcode}).",
                             failed=[{"id": p, "error": tail} for p in verify_parsers],
                             log_path=str(log_path or ""), log_tail=tail)
    installed = [p for p in verify_parsers if results.get(p) == "ok"]
    failed = [{"id": p, "error": str(results.get(p) or "not verified")}
              for p in verify_parsers if results.get(p) != "ok"]
    if failed:
        names = ", ".join(f["id"] for f in failed)
        return InstallResult(False, f"Installed, but these parsers do not work yet: {names}.",
                             installed=installed, failed=failed,
                             log_path=str(log_path or ""), log_tail=_tail(verr or vout))
    return InstallResult(True, f"Installed the {label}.", installed=installed,
                         log_path=str(log_path or ""))


def install_parsers(
    parser_ids: Sequence[str], *, python: Path | str = sys.executable,
    runner: Runner = subprocess_runner, state_file: Optional[Path] = None,
    log_path: Optional[Path] = None, pyproject: Optional[Path] = None,
) -> InstallResult:
    """Install the named parsers (the launcher modal's path). On success the
    parsers' "Not now" records are cleared."""
    ids = list(dict.fromkeys(p for p in parser_ids if p))
    log = log_path if log_path is not None else install_log_path()
    if not ids:
        return InstallResult(False, "No parser was selected.", log_path=str(log))
    try:
        specs = requirement_specs(ids, pyproject)
    except Exception as exc:  # noqa: BLE001 — reported, never raised at the caller
        _log([f"[parsers] FAILED before pip: {exc}"], log)
        return InstallResult(False, f"Cannot install: {exc}",
                             failed=[{"id": p, "error": str(exc)} for p in ids],
                             log_path=str(log))
    cat = catalogue()
    label = "parsers for " + ", ".join(cat[p].label for p in ids)
    result = install(python=python, targets=specs, label=label, verify_parsers=ids,
                     runner=runner, log_path=log)
    if result.installed:
        try:
            state = load_state(state_file)
            for pid in result.installed:
                state["declined"].pop(pid, None)
            save_state(state, state_file)
        except Exception as exc:  # noqa: BLE001 — the install itself succeeded
            logger.warning("codegraph_parsers: could not clear declines (%s)", exc)
    return result


def verify(parser_ids: Sequence[str]) -> Dict[str, str]:
    """In THIS (fresh) interpreter: download pack grammars, then load every
    grammar of each parser. ``{parser id: "ok" | error text}``."""
    from vco_lib import codegraph_grammars as cg

    cat = catalogue()
    out: Dict[str, str] = {}
    for pid in parser_ids:
        parser = cat.get(pid)
        if parser is None:
            out[pid] = "unknown parser"
            continue
        try:
            if parser.pack_grammars:
                cg.download_pack_grammars(parser.pack_grammars)
            missing = [g for g in parser.grammars if cg.load_language(g) is None]
            out[pid] = "ok" if not missing else f"grammar does not load: {', '.join(missing)}"
        except Exception as exc:  # noqa: BLE001 — the verdict carries it
            out[pid] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vco_lib.codegraph_parsers",
                                description="Detect, offer and install code-graph parsers.")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("offer", help="parsers to offer for these projects")
    o.add_argument("--project", action="append", default=[])
    d = sub.add_parser("decline", help='record "Not now" for parsers')
    d.add_argument("--parsers", action="append", default=[])
    d.add_argument("--project", action="append", default=[])
    i = sub.add_parser("install", help="install parsers into this interpreter's venv")
    i.add_argument("--parsers", action="append", default=[])
    i.add_argument("--all", action="store_true", help="every OPTIONAL parser (CI)")
    v = sub.add_parser("verify", help="internal: load the parsers' grammars")
    v.add_argument("--parsers", action="append", default=[])
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "verify":
        results = verify(_split(args.parsers))
        print(json.dumps({"parsers": results}))
        return 0 if all(v == "ok" for v in results.values()) else 1
    if args.cmd == "install":
        ids = [p.id for p in catalogue().values() if p.optional] if args.all else _split(args.parsers)
        result = install_parsers(ids)
        print(json.dumps(result.to_dict()))
        if not result.ok:
            print(f"codegraph_parsers: {result.message} Log: {result.log_path}", file=sys.stderr)
        return 0 if result.ok else 1
    state = load_state()
    if args.cmd == "offer":
        report = compute_offers([Path(p) for p in args.project], state)
        try:
            save_state(state)
        except OSError as exc:
            report.errors.append(f"cannot save the detection cache: {exc}")
        print(json.dumps(report.to_dict()))
        return 0
    recorded = record_decline(state, _split(args.parsers), [Path(p) for p in args.project])
    save_state(state)
    print(json.dumps({"declined": recorded}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
