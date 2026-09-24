# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Review R6 F50: the bundled set pointed at ``vct-ollama``, a manifest deleted
in v0.2.11 together with the Ollama MCP server it installed.

* ``vco_lib/mcp_scan_rules.toml`` ``[deprecated.ollama] opt_in_manifest``
  named it, and its reader (``install_mcp._detect_deprecated_mcp_entries``)
  printed "Opt-in: … inspect launcher/bundled_manifests/vct-ollama.json" in
  every deferral — a file the key never shipped with (the key arrived in
  v0.2.13, two releases after the deletion).
* ``vct-kg.json`` ``requirements.depends_on`` named it.

The capability is superseded (Claude's native Read / vision / reasoning, per
the table's own reason); Ollama is infrastructure, not a module. The Rust twin
over the EMBEDDED list is
``bundled_manifests::tests::every_referenced_module_id_is_an_embedded_manifest``.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from vco_lib import install_mcp, mcp_scan_rules
from tests.common.shipped_programs import command_trees
from vco_lib.deferral_report import DeferralReport

REPO = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO / "launcher" / "bundled_manifests"


def _manifests() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(MANIFEST_DIR.glob("*.json"))}


def test_every_bundled_manifest_id_is_its_file_stem() -> None:
    for name, manifest in _manifests().items():
        assert f"{manifest['id']}.json" == name


def test_every_depends_on_names_a_shipped_manifest() -> None:
    manifests = _manifests()
    ids = {m["id"] for m in manifests.values()}
    dangling = [
        f"{name} -> {dep}"
        for name, m in manifests.items()
        for dep in m.get("requirements", {}).get("depends_on", [])
        if dep not in ids
    ]
    assert dangling == []


def test_every_opt_in_manifest_the_scan_rules_name_ships() -> None:
    """What the deferral tells a user to inspect must exist and be a bundled
    manifest."""
    dangling = []
    for name, info in mcp_scan_rules.deprecated_default_mcps().items():
        path = info["opt_in_manifest"]
        if not path:
            continue
        if not (path.startswith("launcher/bundled_manifests/") and (REPO / path).is_file()):
            dangling.append(f"[deprecated.{name}] -> {path}")
    assert dangling == []


def _ollama_deferral_text(tmp_path: Path) -> str:
    root = tmp_path / "install_root"
    (root / "claude_mcp_servers" / "ollama_mcp").mkdir(parents=True)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"ollama": {
        "type": "stdio",
        "command": "python",
        "args": [str(root / "claude_mcp_servers" / "ollama_mcp" / "server.py")],
    }}}))
    report = DeferralReport()
    install_mcp._detect_deprecated_mcp_entries(root, claude_json, report)
    entries = [e for e in report.entries if e.condition_id == "deprecated_mcp_ollama"]
    assert len(entries) == 1
    return entries[0].detected


def test_the_ollama_deferral_points_at_nothing_missing(tmp_path: Path) -> None:
    text = _ollama_deferral_text(tmp_path)
    assert "vct-ollama" not in text
    assert "Opt-in:" not in text
    assert "Claude's native capabilities" in text


def test_the_opt_in_key_still_reaches_the_deferral(tmp_path: Path) -> None:
    """Leave-alone half: the key keeps its reader — a deprecation that DOES
    name a shipped manifest still gets its "Opt-in:" line."""
    patched = dict(install_mcp._DEPRECATED_DEFAULT_MCPS)
    patched["ollama"] = {**patched["ollama"], "opt_in_manifest": "launcher/bundled_manifests/vct-kg.json"}
    with mock.patch.dict(install_mcp._DEPRECATED_DEFAULT_MCPS, patched):
        text = _ollama_deferral_text(tmp_path)
    assert "Opt-in:" in text and "inspect launcher/bundled_manifests/vct-kg.json" in text


@pytest.mark.parametrize("key", ["opt_in_manifest"])
def test_the_key_stays_declared_in_the_table(key: str) -> None:
    """Configuration is never deleted for being unread — the field stays in
    the table's schema comment and in both loaders' shapes."""
    table = (REPO / "vco_lib" / "mcp_scan_rules.toml").read_text(encoding="utf-8")
    assert f"#   {key} —" in table
    for info in mcp_scan_rules.deprecated_default_mcps().values():
        assert key in info


# ─── round 2: shipped text and manifest paths ────────────────────────────

#: Manifests committed to this repo outside the bundled set (paid-module
#: fixtures the launcher's own tests use) — their ids are real modules too.
FIXTURE_MANIFESTS = REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "tests" / "fixtures" / "manifests"

#: Where shipped prose lives: what VCO delivers (templates/), the docs, the
#: README, the launcher's UI strings, the manifests and the rule tables.
SHIPPED_TEXT_ROOTS = ("templates", "docs", "README.md", "launcher/src", "launcher/bundled_manifests", "vco_lib")
SHIPPED_TEXT_SUFFIXES = {".md", ".template", ".svelte", ".ts", ".json", ".toml"}

#: The shapes that tell a reader a module id is something to install / use.
MODULE_REFERENCE_SHAPES = (
    re.compile(r"(?i)\bmodules?\b[^\n]{0,40}?→\s*[*`]*(vct-[a-z0-9][a-z0-9-]*)"),
    re.compile(r"(?i)[*`]*(vct-[a-z0-9][a-z0-9-]*)[`*]*\s+(?:opt-in\s+)?module\b"),
    re.compile(r"(?i)\b(?:install|installs|installing|installed|enable|enables|opted into|opt into)\s+(?:the\s+)?[*`]*(vct-[a-z0-9][a-z0-9-]*)"),
    re.compile(r"(?i)\bif\s+[*`]*(vct-[a-z0-9][a-z0-9-]*)[`*]*\s+is\s+(?:installed|enabled)"),
)
#: A line that states a retirement is history, not an instruction.
RETIREMENT_WORDS = re.compile(r"(?i)\b(retired|removed|deleted|no longer)\b")
#: The word after a matched id, past its closing markup.
_NEXT_WORD = re.compile(r"[`*]*[ \t]+([a-z][a-z0-9-]*)")


@functools.lru_cache(maxsize=1)
def _program_verbs() -> dict[str, frozenset[str]]:
    """Shipped program → its top-level verbs (``tests/common/shipped_programs``,
    the same source the program-name test reads)."""
    return {prog: frozenset(tree) for prog, tree in command_trees().items()}


def _is_a_command(line: str, match: re.Match, mid: str) -> bool:
    """``vct-cli module list`` names a program and its verb, not a module:
    a shipped program's name followed by one of its verbs is a command."""
    verbs = _program_verbs().get(mid)
    if not verbs:
        return False
    word = _NEXT_WORD.match(line, match.end(1))
    return bool(word and word.group(1) in verbs)


def known_module_ids() -> set[str]:
    ids = {m["id"] for m in _manifests().values()}
    for f in FIXTURE_MANIFESTS.glob("*.json"):
        ids.add(json.loads(f.read_text(encoding="utf-8"))["id"])
    return ids


def module_references(text: str) -> list[tuple[int, str]]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        if RETIREMENT_WORDS.search(line):
            continue
        for shape in MODULE_REFERENCE_SHAPES:
            found.extend(
                (n, m.group(1)) for m in shape.finditer(line)
                if not _is_a_command(line, m, m.group(1))
            )
    return found


def _shipped_text_files() -> list[Path]:
    out = []
    for root in SHIPPED_TEXT_ROOTS:
        base = REPO / root
        if base.is_file():
            out.append(base)
            continue
        out.extend(
            p for p in base.rglob("*")
            if p.is_file() and p.suffix in SHIPPED_TEXT_SUFFIXES and "node_modules" not in p.parts
        )
    return out


def test_no_shipped_text_names_a_module_that_does_not_exist() -> None:
    """Round 2, item 1: agents, knowledge nodes and docs told users to
    install ``vct-ollama`` from the launcher's Modules tab — a module retired
    in v0.2.11. Every module a shipped text tells a reader to install or use
    must be one VCO can actually deliver."""
    known = known_module_ids()
    bad = []
    for f in _shipped_text_files():
        try:
            text = f.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        bad.extend(
            f"{f.relative_to(REPO)}:{n}: {mid}"
            for n, mid in module_references(text)
            if mid not in known
        )
    assert bad == []


@pytest.mark.parametrize("line", [
    "opted into the `vct-ollama` module, local inference is available;",
    "- **Ollama MCP** *(opt-in module)*: Local LLM inference — install via launcher Modules → `vct-ollama`.",
    "4. Reason about it (or if vct-ollama is installed, `chat(...)`)",
    "launcher → Modules tab → **vct-ollama**.",
])
def test_the_scan_sees_the_retired_instruction_shapes(line: str) -> None:
    """The shapes the pre-fix texts used are all caught (the guard's red half)."""
    assert [mid for _, mid in module_references(line)] and all(
        mid not in known_module_ids() for _, mid in module_references(line)
    )


@pytest.mark.parametrize("line", [
    "### `vct-cli module` Commands",
    "vct-cli module list | jq .",
    "Run **vct-cli** module installed <project> to see them.",
])
def test_a_program_name_followed_by_its_verb_is_a_command_not_a_module(line: str) -> None:
    assert module_references(line) == []


@pytest.mark.parametrize(("line", "expected"), [
    # an id that is no shipped program is still read as a module
    ("### `vct-ghost` module", [(1, "vct-ghost")]),
    # a program without a readable verb tree gets no exemption
    ("the `vct-hub` module", [(1, "vct-hub")]),
])
def test_only_a_known_verb_after_a_known_program_is_exempt(line: str, expected: list) -> None:
    assert module_references(line) == expected


def test_a_retirement_statement_is_not_an_instruction() -> None:
    assert module_references("the `vct-ollama` module was retired in v0.2.11") == []
    assert module_references("Installing vct-kg already brings the code graph") == [(1, "vct-kg")]


# Paths a bundled manifest names. `{install_dir}` is a clone of this repo, so
# `{install_dir}/X` is repo-relative X; a bare repo-rooted path is repo-relative;
# a bare `.claude/…` path is a PROJECT path, delivered from templates/.
_REPO_ROOTED = ("claude_mcp_servers", "templates", "infrastructure", "launcher", "vco_lib", "docs", "scripts", "tools", "src-tauri")
_PATH_TOKEN = re.compile(r"(\{install_dir\}[/\\]+)?((?:\.claude|" + "|".join(_REPO_ROOTED) + r")(?:[/\\][A-Za-z0-9_.*-]+)+[/\\]?)")
_PROJECT_TO_TEMPLATES = {".claude/hooks": "templates/hooks", ".claude/scripts": "templates/scripts"}


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _resolve(prefix: str | None, path: str) -> Path | None:
    """The shipped-tree path a manifest path refers to; None = not checkable
    (created at install time, e.g. `.venv`)."""
    path = path.replace("\\", "/").rstrip(".,;:")
    if prefix:
        if path.startswith(".venv/"):
            return None
        return REPO / path
    if path.startswith(".claude/"):
        for project, shipped in _PROJECT_TO_TEMPLATES.items():
            if path == project or path.startswith(project + "/"):
                return REPO / (shipped + path[len(project):])
        if path.startswith(".claude/settings.json"):
            return REPO / "templates" / "settings.json.linux.template"
        return REPO / path
    return REPO / path


def manifest_path_problems(manifest: dict) -> list[str]:
    problems = []
    for text in _strings(manifest):
        for m in _PATH_TOKEN.finditer(text):
            target = _resolve(m.group(1), m.group(2))
            if target is None:
                continue
            if "*" in target.name:
                if not list(target.parent.glob(target.name)):
                    problems.append(m.group(0))
            elif not target.exists():
                problems.append(m.group(0))
    return problems


def test_every_path_a_bundled_manifest_names_exists() -> None:
    """Round 2, item 2: `vct-session-state` ran a `health-check.sh` that never
    shipped, and other bundled manifests named `requirements.txt` files, a
    `.claude/scripts/` analyzer and a hub source dir that do not exist."""
    bad = {name: manifest_path_problems(m) for name, m in _manifests().items()}
    assert {k: v for k, v in bad.items() if v} == {}


def test_the_path_check_sees_the_old_shapes() -> None:
    old = {
        "runtime": {"args": ["{install_dir}/.claude/scripts/health-check.sh"]},
        "install": {"post_install": [{"cmd": "pip install -r claude_mcp_servers/weaviate_mcp/requirements.txt"}],
                    "_note": "The Hub server ships with the launcher binary (src-tauri/src/hub/)."},
    }
    assert len(manifest_path_problems(old)) == 3
    ok = {"a": "{install_dir}/templates/scripts/analyze_code_graph.py", "b": "{install_dir}/.venv/bin/python",
          "c": "copies .claude/hooks/ and templates/hooks/", "d": ".claude/settings.json"}
    assert manifest_path_problems(ok) == []


def test_session_state_declares_no_command_nothing_would_run() -> None:
    """Round 2, item 2: the runtime block names no command (nothing starts a
    bundled `cli` module), and says why."""
    runtime = _manifests()["vct-session-state.json"]["runtime"]
    assert "command" not in runtime and "args" not in runtime
    assert "health-check" in runtime["_note"] and "never shipped" in runtime["_note"]
    install_note = _manifests()["vct-session-state.json"]["install"]["_note"]
    assert "bundle install/update" in install_note and "not per module enable" in install_note
