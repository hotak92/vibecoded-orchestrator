# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: every ``-m vco_lib.<module> …`` argv VCO builds parses with that
module's REAL argparse parser.

Why: the project mover and the collection rename both built ``python -m
vco_lib.config_projection apply … --folder <dst>``; ``apply`` has no
``--folder``, so argparse exited 2 on every move and every rename. Their unit
tests pinned the argv SHAPE with an injected runner, which is exactly the
class "argv-shape tests miss live CLI parser rejections". This test closes the
class for every spawn site at once: it collects each argv from the shipped
source — Python by AST, Rust by its literal ``.arg(..)`` chains, literal
arrays, and the literal token lists handed to the bridge helpers that prepend
``-m vco_lib.<module>`` — and runs it through the module's own parser.

How the parser is obtained: the module's zero-argument ``_build_parser`` /
``build_parser`` when it has one, otherwise the module is executed exactly as
``python -m`` would (``runpy``) with ``ArgumentParser.parse_args`` patched to
hand back the parser instead of parsing. A module whose source never imports
``argparse`` is NOT executed (its ``__main__`` would do real work); those are
checked by :data:`HAND_ROLLED` below.

Non-literal argv elements (paths, ids) are synthesised from the parser's own
action for the option they follow (a choice, an int, or a string), so the
parse exercises the real flag set, verbs, choices and arity.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import inspect
import io
import re
import runpy
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Python trees that ship. ``tests/`` is excluded on purpose: a test may build
#: a deliberately-invalid argv to prove a refusal.
PY_ROOTS = ("vco_lib", "templates", "claude_mcp_servers", "scripts", "install.py")
RS_ROOT = REPO / "launcher" / "src-tauri"
SKIP_PARTS = {"node_modules", "target", ".venv", "dist", "__pycache__"}

VALUE = object()  # a non-literal argv element (a variable, a path, an id)

#: Rust bridge helpers that spawn ``-m <module>`` and then append the literal
#: token list passed to them (plus the listed suffix). Keyed by the helper's
#: name; the pattern captures the ``&[ ... ]`` array literal of each call.
RUST_HELPERS: dict[str, tuple[str, tuple]] = {
    # commands/project_hooks_settings.rs + vct-hub/src/hooks_enforcement.rs
    "run_hooks_cli": ("vco_lib.hooks_settings", ("--project-folder", VALUE)),
    # commands/model_gateway.rs
    "run_vscode_settings": ("vco_lib.vscode_settings", ()),
    # commands/gateway_freshness.rs (appends --install-root <root> when known)
    "run_freshness": ("vco_lib.gateway_freshness", ("--install-root", VALUE)),
}

#: Rust helpers that RETURN a ``Command`` already carrying ``-m <module>``;
#: the ``.arg`` chain the caller hangs on the returned value is the argv.
#: ``None`` = the module is the helper's second argument (a string literal).
RUST_COMMAND_HELPERS: dict[str, Optional[str]] = {
    "python_module_command": None,          # commands/model_gateway.rs
    "configure_vco_lib_command": None,      # commands/codegraph.rs
    "vscode_settings_command": "vco_lib.vscode_settings",
    "gateway_freshness_command": "vco_lib.gateway_freshness",
    "gateway_usage_command": "vco_lib.gateway_usage",
}

#: Argv vectors built WITHOUT the ``-m <module>`` prefix (a bridge adds it):
#: file → module → the verbs that open such a ``vec![..]`` in that file.
RUST_VERB_VECS: dict[str, dict[str, frozenset]] = {
    "src/commands/model_gateway.rs": {
        "vco_lib.vscode_settings": frozenset({"point", "mode"}),
    },
    "src/commands/kg_summary.rs": {
        "vco_lib.summary_health": frozenset({"summary-recheck"}),
    },
    "src/commands/projects_v2.rs": {
        "vco_lib.cli": frozenset({"project"}),
    },
}

#: Modules whose CLI is hand-rolled (no argparse): the argv each accepts.
HAND_ROLLED = {
    "vco_lib.manifest_validation": 1,  # exactly one positional manifest path
}


@dataclass(frozen=True)
class Site:
    where: str
    module: str
    tokens: tuple
    strict: bool  # False: more args may be appended at runtime (skip "required")

    def __str__(self) -> str:  # pytest id
        return f"{self.where}::{self.module}"


# ─── collection ──────────────────────────────────────────────────────────


def _walk(root: Path, suffix: str):
    paths = [root] if root.is_file() else root.rglob(f"*{suffix}")
    for p in paths:
        if p.suffix == suffix and not (SKIP_PARTS & set(p.parts)):
            yield p


def _python_sites() -> list[Site]:
    out: list[Site] = []
    for rel in PY_ROOTS:
        for path in _walk(REPO / rel, ".py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                elts = node.elts
                for i in range(len(elts) - 1):
                    a, b = elts[i], elts[i + 1]
                    if not (isinstance(a, ast.Constant) and a.value == "-m"):
                        continue
                    if not (isinstance(b, ast.Constant) and isinstance(b.value, str)
                            and b.value.startswith("vco_lib.")):
                        continue
                    toks: list = []
                    strict = True
                    for e in elts[i + 2:]:
                        if isinstance(e, ast.Starred):
                            strict = False
                            break
                        toks.append(e.value if isinstance(e, ast.Constant)
                                    and isinstance(e.value, str) else VALUE)
                    out.append(Site(f"{path.relative_to(REPO)}:{node.lineno}",
                                    b.value, tuple(toks), strict))
    return out


_RS_STR = r'"((?:[^"\\]|\\.)*)"'
_RS_ARG = re.compile(r"\.arg\(\s*(" + _RS_STR + r"|[^()]*(?:\([^()]*\))?[^()]*)\s*\)")
_RS_ARGS = re.compile(r"\.args\(\s*(?:&)?\[(.*?)\]\s*\)", re.S)


def _rs_token(expr: str):
    expr = expr.strip()
    m = re.fullmatch(_RS_STR + r"(?:\.(?:into|to_string|to_owned)\(\))?", expr)
    return m.group(1) if m else VALUE


def _rs_array_tokens(body: str) -> list:
    parts = [p for p in (s.strip() for s in _split_top(body)) if p]
    return [_rs_token(p) for p in parts]


def _split_top(body: str) -> list[str]:
    out, depth, cur, in_str, esc = [], 0, [], False, False
    for ch in body:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _strip_rust_comments(text: str) -> str:
    """Blank ``//`` comments outside string literals (line numbers kept)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if not in_str:
            # Char literals that would otherwise flip the string state.
            for lit in ("'\"'", "'\\\"'", "'\\\\'"):
                if text.startswith(lit, i):
                    out.append(lit)
                    i += len(lit)
                    break
            else:
                lit = ""
            if lit:
                continue
            # Raw strings r#"..."# may contain bare quotes.
            raw = re.compile(r'r(#+)"').match(text, i)
            if raw:
                end = text.find('"' + raw.group(1), raw.end())
                end = n if end < 0 else end + 1 + len(raw.group(1))
                out.append(text[i:end])
                i = end
                continue
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            i = j
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _rs_chain_tail(text: str, pos: int) -> list:
    """Tokens of the ``.arg(..)`` / ``.args([..])`` calls chained from ``pos``."""
    toks: list = []
    while True:
        nxt = re.compile(r"\s*(\.arg\(|\.args\()").match(text, pos)
        if not nxt:
            return toks
        if nxt.group(1) == ".args(":
            am = _RS_ARGS.match(text, nxt.start(1))
            if not am:
                return toks
            toks += _rs_array_tokens(am.group(1))
        else:
            am = _RS_ARG.match(text, nxt.start(1))
            if not am:
                return toks
            toks.append(_rs_token(am.group(1)))
        pos = am.end()


def _rust_sites() -> list[Site]:
    out: list[Site] = []
    for path in _walk(RS_ROOT, ".rs"):
        text = _strip_rust_comments(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO)

        # 1. Literal arrays / vecs: ["-m", "vco_lib.x", ...] and vec!["-m".into(), ...]
        #    (plus any .arg/.args chained after an `.args([...])` array).
        for m in re.finditer(r"(?:vec!|&)?\[\s*\"-m\"(?:\.into\(\))?\s*,(.*?)\]", text, re.S):
            toks = _rs_array_tokens(m.group(1))
            if not toks or toks[0] is VALUE or not str(toks[0]).startswith("vco_lib."):
                continue
            close = re.compile(r"\s*\)").match(text, m.end())
            if close and text[max(0, m.start() - 7):m.start()].rstrip().endswith(".args("):
                toks += _rs_chain_tail(text, close.end())
            line = text.count("\n", 0, m.start()) + 1
            out.append(Site(f"{rel}:{line}", toks[0], tuple(toks[1:]), False))

        # 2. Method chains: .arg("-m").arg("vco_lib.x").arg(..)... up to the first
        #    non-arg call. Statements after the chain may append more (not strict).
        for m in re.finditer(r"\.arg\(\s*\"-m\"\s*\)\s*\.arg\(\s*\"(vco_lib\.[a-z_]+)\"\s*\)", text):
            toks = _rs_chain_tail(text, m.end())
            if toks:
                line = text.count("\n", 0, m.start()) + 1
                out.append(Site(f"{rel}:{line}", m.group(1), tuple(toks), False))

        # 3a. Command-returning helpers: `let mut cmd = helper(py, "vco_lib.x", ..);`
        #     followed by `cmd.arg(..)...` or a chain directly on the call.
        for helper, fixed in RUST_COMMAND_HELPERS.items():
            for m in re.finditer(
                rf"(?:let\s+(?:mut\s+)?(\w+)\s*=\s*(?:match\s+)?)?\b{helper}\(([^;{{}}]*)\)(?=\s*[;?])",
                text,
            ):
                if text[max(0, m.start() - 3):m.start()].endswith("fn "):
                    continue
                module = fixed
                if module is None:
                    lit = re.search(r'"(vco_lib\.[a-z_]+)"', m.group(2))
                    if not lit:
                        continue
                    module = lit.group(1)
                toks = []
                if m.group(1):
                    nxt = re.compile(rf"\s*\??\s*;\s*{m.group(1)}(?=\.arg)").match(text, m.end())
                    if nxt:
                        toks = _rs_chain_tail(text, nxt.end())
                if toks:
                    line = text.count("\n", 0, m.start()) + 1
                    out.append(Site(f"{rel}:{line}", module, tuple(toks), False))

        # 3b. Argv vecs a bridge prefixes with `-m <module>` (see RUST_VERB_VECS).
        for module, verbs in RUST_VERB_VECS.get(str(path.relative_to(RS_ROOT)), {}).items():
            for m in re.finditer(r"vec!\[(.*?)\]", text, re.S):
                toks = _rs_array_tokens(m.group(1))
                if toks and toks[0] in verbs:
                    line = text.count("\n", 0, m.start()) + 1
                    out.append(Site(f"{rel}:{line}", module, tuple(toks), False))

        # 3c. Bridge helpers that prepend `-m <module>` to a literal token array.
        for helper, (module, suffix) in RUST_HELPERS.items():
            for m in re.finditer(rf"\b{helper}\((?:[^;]*?,\s*)?&\[(.*?)\]", text, re.S):
                toks = _rs_array_tokens(m.group(1))
                if not toks or toks[0] is VALUE:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                out.append(Site(f"{rel}:{line}", module, tuple(toks) + suffix, False))
    return out


def _builder_sites() -> list[Site]:
    """Argv produced by CALLING the Python builders (not read from source)."""
    from vco_lib.config_projection import build_apply_argv

    sites = []
    for kw in ({}, {"db_path": "/tmp/x.db"}, {"orchestrator_root": "/tmp/orch"}):
        argv = build_apply_argv("python", "pid", **kw)
        assert argv[1:3] == ["-m", "vco_lib.config_projection"]
        sites.append(Site(f"build_apply_argv{sorted(kw)}", argv[2], tuple(argv[3:]), True))
    # Printed retry commands that a deferral hands the user (shell-split).
    import shlex

    from vco_lib.bundle_settings_io import retry_command
    printed = shlex.split(retry_command(Path("/tmp/some project")))
    assert printed[1:3] == ["-m", "vco_lib.project_init"]
    sites.append(Site("bundle_settings_io.retry_command", printed[2], tuple(printed[3:]), True))
    return sites


ALL_SITES = _python_sites() + _rust_sites() + _builder_sites()


# ─── the real parser ─────────────────────────────────────────────────────


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("captured")
        self.parser = parser


_PARSERS: dict[str, Optional[argparse.ArgumentParser]] = {}


def real_parser(module: str) -> Optional[argparse.ArgumentParser]:
    if module in _PARSERS:
        return _PARSERS[module]
    mod = importlib.import_module(module)
    assert mod.__file__ is not None, module
    source = Path(mod.__file__).read_text(encoding="utf-8")
    parser: Optional[argparse.ArgumentParser] = None
    for name in ("_build_parser", "build_parser"):
        fn = getattr(mod, name, None)
        if callable(fn) and not inspect.signature(fn).parameters:
            candidate = fn()
            if isinstance(candidate, argparse.ArgumentParser):
                parser = candidate
                break
    if parser is None and "argparse" in source and "__main__" in source:
        real_pa = argparse.ArgumentParser.parse_args
        real_pka = argparse.ArgumentParser.parse_known_args

        def _grab(self, *_a, **_k):
            raise _Captured(self)

        argparse.ArgumentParser.parse_args = _grab  # type: ignore[method-assign]
        argparse.ArgumentParser.parse_known_args = _grab  # type: ignore[method-assign]
        saved_argv = sys.argv[:]
        try:
            sys.argv = [module]
            with warnings.catch_warnings():
                # runpy notes the module is already imported — expected here.
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module(module, run_name="__main__", alter_sys=True)
        except _Captured as got:
            parser = got.parser
        finally:
            argparse.ArgumentParser.parse_args = real_pa  # type: ignore[method-assign]
            argparse.ArgumentParser.parse_known_args = real_pka  # type: ignore[method-assign]
            sys.argv = saved_argv
    _PARSERS[module] = parser
    return parser


def _subparsers(p: argparse.ArgumentParser):
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _option_action(p: argparse.ArgumentParser, flag: str):
    for a in p._actions:
        if flag in a.option_strings:
            return a
    return None


def _synth(action) -> str:
    if action is not None and action.choices:
        return str(next(iter(action.choices)))
    if action is not None and action.type in (int, float):
        return "1"
    return "x"


def _concrete(parser: argparse.ArgumentParser, tokens: tuple) -> list[str]:
    """Replace VALUE tokens using the action of the option they follow."""
    out: list[str] = []
    active = parser
    for tok in tokens:
        if tok is VALUE:
            prev = out[-1] if out else ""
            out.append(_synth(_option_action(active, prev) if prev.startswith("-") else None))
            continue
        sub = _subparsers(active)
        if sub is not None and tok in sub.choices and not any(
            t.startswith("-") for t in out[len(out):]
        ):
            active = sub.choices[tok]
        out.append(tok)
    return out


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[bool, str]:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code in (0, None), err.getvalue().strip()
    return True, ""


# ─── the contract ────────────────────────────────────────────────────────


def test_the_collectors_find_the_known_sites():
    """Guard the collectors themselves: a regex that silently matches nothing
    would make every parametrised case below vacuous."""
    wheres = {s.module for s in ALL_SITES}
    for expected in ("vco_lib.config_projection", "vco_lib.project_init",
                     "vco_lib.hooks_settings", "vco_lib.vscode_settings",
                     "vco_lib.codegraph_resync", "vco_lib.summary_health",
                     "vco_lib.embedding_enrichment", "vco_lib.gateway_ensure"):
        assert expected in wheres, f"no spawn site collected for {expected}"
    assert len(_python_sites()) >= 10
    assert len(_rust_sites()) >= 30


def test_the_env_block_bridge_verbs_are_collected():
    """v0.2.97 review F5: every Rust env-block edit goes through
    `vco_lib_bridge.rs`'s two literal argv chains — `write-env-block` and its
    removal-only twin `strip-env-keys` (plus the surface-free unregister
    evidence verb `strip-proven-secret-values`). Pin that the collector SEES all
    three (so the parametrised parse below covers their verb, flags and
    `--surface` choice), and that each carries the flags its verb requires."""
    bridge = [
        s for s in _rust_sites()
        if s.where.startswith("launcher/src-tauri/src/services/vco_lib_bridge.rs")
        and s.module == "vco_lib.config_projection"
    ]
    verbs = {s.tokens[0] for s in bridge if s.tokens}
    assert {"write-env-block", "strip-env-keys", "strip-proven-secret-values"} <= verbs, verbs
    for s in bridge:
        assert "--project-folder" in s.tokens, s
        if s.tokens[0] in ("write-env-block", "strip-env-keys"):
            assert "--surface" in s.tokens, s


def test_the_jsonc_env_read_bridge_verb_is_collected_and_parses():
    """v0.2.97: `vco_lib_bridge::read_settings_env_blocks` spawns
    `-m vco_lib.env_projection_check read-env` and appends one
    `--project-folder <f>` per folder. Pin that the collector SEES the chain
    (so the parametrised parse covers the verb) and that the full argv the
    bridge builds — verb plus repeated folders — parses with the real parser."""
    sites = [
        s for s in _rust_sites()
        if s.where.startswith("launcher/src-tauri/src/services/vco_lib_bridge.rs")
        and s.module == "vco_lib.env_projection_check"
    ]
    assert [s.tokens[:1] for s in sites] == [("read-env",)], sites
    parser = real_parser("vco_lib.env_projection_check")
    assert parser is not None
    ok, err = _parse(parser, ["read-env", "--project-folder", "/a", "--project-folder", "/b"])
    assert ok, err
    ok, err = _parse(parser, ["read-env"])
    assert not ok and "--project-folder" in err


@pytest.mark.parametrize("site", ALL_SITES, ids=str)
def test_built_argv_parses_with_the_real_parser(site: Site):
    if site.module in HAND_ROLLED:
        literal = [t for t in site.tokens if t is not VALUE]
        assert len(site.tokens) == HAND_ROLLED[site.module] and not literal, site
        return
    parser = real_parser(site.module)
    assert parser is not None, (
        f"{site.where}: {site.module} exposes no argparse CLI to parse against"
    )
    argv = _concrete(parser, site.tokens)
    ok, err = _parse(parser, argv)
    if not ok and not site.strict and "the following arguments are required" in err:
        ok = True  # the spawn appends more args after this literal prefix
    assert ok, f"{site.where}: `python -m {site.module} {' '.join(argv)}` -> {err}"


def test_a_rejected_flag_is_caught(monkeypatch):
    """Red twin: the exact pre-v0.2.97 mover argv must FAIL this contract."""
    parser = real_parser("vco_lib.config_projection")
    assert parser is not None
    ok, err = _parse(parser, ["apply", "--project-id", "p", "--folder", "/x"])
    assert not ok and "--folder" in err
