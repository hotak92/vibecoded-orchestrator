# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-argparse sweep over every CLI command VCO's deferral emitters produce.

v0.2.75 (C-10 family fix). Three separate deferral emitters shipped a
``code-graph-analyze . --force`` / ``kg-sync --all --force`` remediation the
target CLIs REJECT (analyzer argparse: "unrecognized arguments") or silently
ignore (kg-sync's manual argv loop) — the user could never run the command as
written. Fixing each string individually does not stop the NEXT emitter from
minting a bogus flag, so this sweep validates the whole family:

1. **Source-scan sweep** — walk every non-test Python + Rust source file,
   find each LINE that names a registered VCO CLI inside a string literal /
   emitted text, regex-extract its ``--flags``, and assert every flag exists
   on that CLI's REAL parser. A future emitter (Python or Rust) that writes a
   command with a flag its target rejects fails here, at CI time.

   * Python emitters are covered by IMPORTING the target CLI's parser builder
     (``analyze_code_graph._build_arg_parser``,
     ``vco_lib.project_init._build_arg_parser``, …) — the actual argparse
     contract, never a hand-maintained flag list.
   * Rust-side emitted strings are SOURCE-PARSED: line-based scan of
     ``launcher/src-tauri/src/**/*.rs`` (documented pattern: skip ``//``
     comment lines; stop at the ``#[cfg(test)]`` / ``mod tests`` marker so
     negative test assertions like ``!content.contains("… --force")`` don't
     trip the sweep; ``format!`` placeholders are irrelevant because only
     ``--flag`` tokens are validated).
   * CLIs without a registered parser (git, podman, arbitrary shell) are out
     of scope — the sweep validates VCO's own CLIs only.

2. **Runtime emission check** — actually run the chunker-preset deferral
   emitter (the Python home the Rust launcher routes through) against a temp
   folder, extract the command lines it wrote to UPDATE_DEFERRED.md, and feed
   them through the real parsers via ``parse_known_args`` — asserting no
   unrecognized arguments. This validates the emitted TEXT end-to-end, not
   just the source literal.

3. **Canonical C-10 regression** — the exact rename-deferral argv shape
   (``. --project '<NewName>'``) must parse clean, and the historical bogus
   shape (``. --force``) must be REJECTED (proving the sweep would have
   caught the original bug).

No Weaviate, no network — parser construction only, nothing is executed.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import shlex
import sys
import tempfile
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_KG_SYNC_PATH = _REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
_INSTALL_PY_PATH = _REPO_ROOT / "install.py"

sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Registered CLIs: token(s) that identify an invocation → known-flag provider.
# Flag sets come from the REAL parsers (imported) or, for install.py /
# kg-sync (manual argv loop; importing the 25k-line install.py is not a unit
# test), from a source-parse of their argument declarations.
# ---------------------------------------------------------------------------


def _load_analyzer_parser():
    spec = importlib.util.spec_from_file_location(
        "_sweep_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.skip(f"cannot load analyzer from {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.skip("weaviate-client unavailable — analyzer cannot be loaded")
    return mod._build_arg_parser()


def _parser_option_strings(parser) -> set:
    """All ``--flag`` option strings a parser accepts, subparsers included."""
    out = set(getattr(parser, "_option_string_actions", {}).keys())
    for action in getattr(parser, "_actions", []):
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                if hasattr(sub, "_option_string_actions"):
                    out |= _parser_option_strings(sub)
    return out


def _kg_sync_known_flags() -> set:
    """kg-sync has NO argparse — its main() string-compares ``sys.argv[1]``.

    Enumerate the accepted ``--tokens`` from the source (the comparison
    literals), so the set tracks the code instead of a hand-list. Anything
    not enumerated (e.g. the historical ``--force``) is either rejected or
    silently ignored — both make an emitted remediation dishonest.
    """
    text = _KG_SYNC_PATH.read_text(encoding="utf-8", errors="replace")
    flags = set(re.findall(r"sys\.argv\[1\]\s*==\s*[\"'](--[a-z0-9-]+)[\"']", text))
    assert flags, "kg-sync source shape changed — update _kg_sync_known_flags"
    return flags


def _install_py_known_flags() -> set:
    """Source-parse install.py's ``add_argument("--…")`` declarations."""
    text = _INSTALL_PY_PATH.read_text(encoding="utf-8", errors="replace")
    flags = set()
    for m in re.finditer(r"add_argument\(([^)]*)", text):
        flags |= set(re.findall(r"[\"'](--[A-Za-z0-9][A-Za-z0-9-]*)[\"']", m.group(1)))
    assert flags, "install.py argparse shape changed — update _install_py_known_flags"
    return flags


@pytest.fixture(scope="module")
def cli_registry() -> dict:
    """CLI-identifying token → set of valid ``--flag`` strings."""
    analyzer_flags = _parser_option_strings(_load_analyzer_parser())
    project_init = importlib.import_module("vco_lib.project_init")
    project_init_flags = _parser_option_strings(project_init._build_arg_parser())
    kg_sync_flags = _kg_sync_known_flags()
    install_flags = _install_py_known_flags()
    return {
        "code-graph-analyze": analyzer_flags,
        "analyze_code_graph.py": analyzer_flags,
        "kg-sync": kg_sync_flags,
        "sync_knowledge_graph.py": kg_sync_flags,
        "vco_lib.project_init": project_init_flags,
        "install.py": install_flags,
    }


# ---------------------------------------------------------------------------
# 1. Source-scan sweep
# ---------------------------------------------------------------------------

# Source roots that hold emitters (deferral writers, error-remediation
# strings). tests/ is deliberately excluded: negative assertions there NAME
# the bogus flags on purpose.
_PY_SCAN_ROOTS = ("vco_lib", "templates", "claude_mcp_servers", "migrations", "tools")
_RS_SCAN_ROOT = "launcher/src-tauri"
# v0.2.75 P2d (C-11b): the launcher GUI also emits user-runnable remediation
# commands (e.g. the prune-failure drop-and-recreate in
# CodeGraphReanalysisModal / CodeGraphBuildBanner). Scan the Svelte frontend so
# a bogus flag in a displayed command is caught by the SAME sweep.
_SVELTE_SCAN_ROOT = "launcher/src"

# Directories never scanned (vendored / generated / caches).
_SCAN_EXCLUDE_PARTS = {
    ".git", ".venv", "node_modules", "target", "__pycache__", ".wt",
    "worktrees", "dist", "build", "vendor",
}

_FLAG_RE = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")
_RUST_TEST_MARKER = re.compile(r"^\s*(#\[cfg\(test\)\]|mod tests\b)")


def _iter_scan_files():
    for root in _PY_SCAN_ROOTS:
        base = _REPO_ROOT / root
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if any(part in _SCAN_EXCLUDE_PARTS for part in p.parts):
                continue
            if p.name.startswith("test_"):
                continue
            yield p, "py"
    yield _INSTALL_PY_PATH, "py"
    rs_base = _REPO_ROOT / _RS_SCAN_ROOT
    if rs_base.exists():
        for p in rs_base.rglob("*.rs"):
            if any(part in _SCAN_EXCLUDE_PARTS for part in p.parts):
                continue
            yield p, "rs"
    svelte_base = _REPO_ROOT / _SVELTE_SCAN_ROOT
    if svelte_base.exists():
        for p in svelte_base.rglob("*.svelte"):
            if any(part in _SCAN_EXCLUDE_PARTS for part in p.parts):
                continue
            yield p, "svelte"
        # Extracted GUI logic (e.g. codegraph-build-banner-logic.ts) builds the
        # displayed remediation commands; scan the .ts too. Test specs (*.test.ts)
        # are skipped — their negative assertions may name bogus flags on purpose.
        for p in svelte_base.rglob("*.ts"):
            if any(part in _SCAN_EXCLUDE_PARTS for part in p.parts):
                continue
            if p.name.endswith(".test.ts") or p.name.endswith(".spec.ts"):
                continue
            yield p, "svelte"  # same comment conventions (// and /* */)


def _scan_lines(path: Path, kind: str):
    """Yield (lineno, line) for scannable lines of one source file.

    Python: skip ``#`` comment lines (commands live in string literals).
    Rust: skip ``//`` comment lines AND everything from the first
    ``#[cfg(test)]`` / ``mod tests`` marker on — the test module is the
    tail-of-file convention in this codebase, and its negative assertions
    intentionally name bogus flags.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if kind == "py" and stripped.startswith("#"):
            continue
        if kind == "rs":
            if _RUST_TEST_MARKER.match(line):
                return  # test module reached — stop scanning this file
            if stripped.startswith("//"):
                continue
        if kind == "svelte":
            # Svelte commands live in string / template literals; skip comment
            # lines in both the <script> (`//`) and markup (`<!-- … -->`)
            # sections so explanatory prose naming a flag isn't mis-flagged.
            if (
                stripped.startswith("//")
                or stripped.startswith("<!--")
                or stripped.startswith("*")
            ):
                continue
        yield lineno, line


def test_every_emitted_cli_flag_exists_on_the_real_parser(cli_registry):
    """The family fix: any line in non-test source that names a VCO CLI and
    a ``--flag`` the CLI's real parser rejects fails this sweep.

    This is deliberately broader than `command_to_apply` construction sites:
    remediation text in error messages / log lines is user-runnable too, and
    scanning lines (instead of reconstructing emitter call graphs) means a
    NEW emitter is covered automatically, wherever it lives.
    """
    violations = []
    for path, kind in _iter_scan_files():
        for lineno, line in _scan_lines(path, kind):
            for token, known_flags in cli_registry.items():
                idx = line.find(token)
                if idx < 0:
                    continue
                # Only the invocation tail (after the CLI token) is checked —
                # prose BEFORE the token on the same line can't be its argv.
                tail = line[idx + len(token):]
                for flag in _FLAG_RE.findall(tail):
                    if flag not in known_flags:
                        violations.append(
                            f"{path.relative_to(_REPO_ROOT)}:{lineno}: "
                            f"'{token}' invoked with unknown flag '{flag}' "
                            f"(line: {line.strip()[:160]})"
                        )
                break  # first matching CLI token wins for this line
    assert not violations, (
        "Emitted command(s) name flags their target CLI rejects — the user "
        "cannot run these remediations as written:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 2. Runtime emission check — the chunker deferral (the Python home the Rust
#    launcher routes through since v0.2.73 A-5).
# ---------------------------------------------------------------------------


def _extract_command_lines(markdown: str) -> list:
    """Command lines from a deferral body: non-comment, non-blank lines of
    the fenced/indented command block(s). We simply take every line that
    invokes a registered CLI token."""
    out = []
    for line in markdown.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def test_chunker_deferral_commands_parse_clean(cli_registry):
    """Run the real emitter; every emitted CLI line must parse with zero
    unrecognized arguments on the target CLI's real parser."""
    from vco_lib.project_init import _emit_chunker_resync_deferral

    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _emit_chunker_resync_deferral(folder, "0.2.45", "0.2.46")
        content = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()

    analyzer_parser = _load_analyzer_parser()
    checked = 0
    for line in _extract_command_lines(content):
        if "code-graph-analyze" in line:
            argv = shlex.split(line)
            # argv[0] is the script path; the analyzer parser sees the rest.
            _, extras = analyzer_parser.parse_known_args(argv[1:])
            assert not extras, f"unrecognized arguments {extras} in: {line}"
            checked += 1
        elif "kg-sync" in line:
            argv = shlex.split(line)
            known = cli_registry["kg-sync"]
            bad = [a for a in argv[1:] if a.startswith("--") and a not in known]
            assert not bad, f"kg-sync does not accept {bad} in: {line}"
            checked += 1
    assert checked >= 2, (
        "expected the chunker deferral to emit both a kg-sync and a "
        f"code-graph-analyze command; found {checked} in:\n{content}"
    )


# ---------------------------------------------------------------------------
# 3. Canonical C-10 regression
# ---------------------------------------------------------------------------


def test_rename_deferral_command_shape_parses_and_old_shape_rejects():
    """The rename deferral emits `code-graph-analyze . --project '<name>'`
    (see projects_v2.rs::emit_codegraph_rename_deferral). The exact argv
    shape must parse clean; the historical `--force` shape must be rejected —
    proving this sweep would have caught the original v0.2.73 bug."""
    parser = _load_analyzer_parser()

    ok, extras = parser.parse_known_args([".", "--project", "New Name"])
    assert not extras
    assert str(ok.repo_path) == "."
    assert ok.project == "New Name"

    _, extras = parser.parse_known_args([".", "--force"])
    assert extras == ["--force"], (
        "the analyzer must NOT accept --force; the valid drop+rebuild flag "
        "is --force-recreate"
    )
