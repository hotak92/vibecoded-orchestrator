# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-1 — the code-graph CLI read the wrong collection, silently.

TWO LIVE DEFECTS, ONE FILE.

**B1 — a private prefix rule that diverged from the writer's.**
``templates/scripts/query_code_graph.py`` carried its own
``_sanitize_collection_prefix``::

    re.sub(r'[^a-zA-Z0-9_]', '_', name)   # then upper-first

The ANALYZER (``templates/scripts/analyze_code_graph.py``) names the classes it
WRITES with ``vco_lib.codegraph_naming.canonical_class_prefix``. The two rules
agree on ``-``, ``.``, ``_`` and bare names, and DISAGREE on **whitespace**: the
private rule maps a space to ``_``; the canonical rule drops it and capitalises
the next word. So for any project name containing a space::

    'VibeCoded Orchestrator'  CLI -> 'VibeCoded_Orchestrator_CodeFunction'
                         analyzer -> 'VibeCodedOrchestrator_CodeFunction'

An absent Weaviate class yields **no results, not an error**, so the CLI simply
returned nothing. Measured on the dev machine at the time of the fix: 6 live
``VibeCodedOrchestrator_*`` classes, 0 ``VibeCoded_Orchestrator_*``.

The blast radius was every mode that goes through ``_coll()`` — ``similar``,
all ``structure`` modes, and ``search``'s anchor/sibling enrichment (13 call
sites). ``search``'s main fan-out went through ``kg_access`` instead and used
the CORRECT rule, so ONE FILE answered the same question two ways.

**B2 — the access matrix was dead on every installed project.**
The ``kg_access`` import resolved its directory from
``Path(__file__).parent.parent.parent / "claude_mcp_servers" / "scripts"`` with
**no env arm**. On an installed project that is the USER PROJECT root, which has
no ``claude_mcp_servers/`` — so the import ALWAYS failed there and the
self-only fallback silently dropped every peer granted through the launcher's
``VCT_CODE_GRAPH_ACCESS_LIST``.

**The shared shape.** Six Python ladders answered "where is the orchestrator?"
in ``templates/scripts/`` (three of them inside ``query_code_graph.py`` alone).
These scripts cannot import ``vco_lib`` to find ``vco_lib``, so the shared home
is an INLINE ``_resolve_orchestrator_root()`` copied VERBATIM into each script
and pinned byte-identical here — a documented class-C mirror with an enforcing
test rather than a silent copy (the ``tests/common/rust_source.py`` precedent).

Every test below is OS-independent: paths are built with ``pathlib`` from
``tmp_path``, never by string concatenation, and nothing shells out.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from fnmatch import fnmatch as _fnmatch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "templates" / "scripts"

CLI_SRC = SCRIPTS / "query_code_graph.py"
GET_NODE_INFO = SCRIPTS / "get_node_info.py"
SEARCH_KNOWLEDGE = SCRIPTS / "search_knowledge.py"
PROCESS_DOCUMENTS = SCRIPTS / "process_documents.py"
ANALYZER_SRC = SCRIPTS / "analyze_code_graph.py"

#: The scripts WP-1 owns and unified. ``analyze_code_graph.py``,
#: ``sync_knowledge_graph.py`` and ``maintain_knowledge_graph.py`` also carry an
#: orchestrator-root ladder but are owned by other lanes — see
#: :func:`test_unmigrated_root_ladders_are_named_not_forgotten`.
SHARED_RESOLVER_SCRIPTS = (
    CLI_SRC,
    GET_NODE_INFO,
    SEARCH_KNOWLEDGE,
    PROCESS_DOCUMENTS,
)

_SHARED_BEGIN = (
    "# VCO-SHARED-BEGIN: _resolve_orchestrator_root "
    "(verbatim across templates/scripts/*.py)\n"
)
_SHARED_END = "# VCO-SHARED-END: _resolve_orchestrator_root"


def _shared_block(path: Path) -> str:
    """Return the bytes between the VCO-SHARED sentinels, or fail loudly."""
    src = path.read_text(encoding="utf-8")
    assert _SHARED_BEGIN in src and _SHARED_END in src, (
        f"{path.name} lost its VCO-SHARED _resolve_orchestrator_root block"
    )
    return src.split(_SHARED_BEGIN, 1)[1].split(_SHARED_END, 1)[0]


def _load_resolver(path: Path, script_location: Path, path_cls=None):
    """Exec ONLY the shared block from ``path``, pretending the script lives at
    ``script_location``.

    Executes the SHIPPED bytes (not a re-implementation) without importing
    weaviate / vco_lib / weaviate_mcp, so the test runs anywhere.

    ``path_cls`` substitutes the ``Path`` name the shipped code resolves from
    its module globals — the only portable way to provoke an OS-level path
    rejection (Windows raises ``OSError`` for reserved names; Linux cannot even
    hold such a value in an env var).
    """
    import os as _os

    ns: dict = {
        "os": _os,
        "Path": path_cls or Path,
        "__file__": str(script_location),
    }
    exec(compile(_shared_block(path), str(path), "exec"), ns)  # noqa: S102
    return ns["_resolve_orchestrator_root"]


def _path_exploding_on(marker: str):
    """A ``Path`` stand-in whose ``is_dir()`` raises for paths containing
    ``marker`` — i.e. an OS that refuses to answer the question."""

    class _P:
        def __init__(self, value):
            self._p = Path(value)

        def __truediv__(self, other):
            return _P(self._p / other)

        def resolve(self):
            return _P(self._p.resolve())

        @property
        def parent(self):
            return _P(self._p.parent)

        def is_dir(self):
            if marker in str(self._p):
                raise OSError(22, "simulated: the OS refused this path")
            return self._p.is_dir()

        def __str__(self):
            return str(self._p)

        def __eq__(self, other):
            return self._p == getattr(other, "_p", other)

        def __hash__(self):
            return hash(self._p)

    return _P


def _is_docstring(node) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _make_orchestrator(root: Path) -> Path:
    """Create a minimally-shaped orchestrator clone at ``root``."""
    (root / "claude_mcp_servers" / "scripts").mkdir(parents=True, exist_ok=True)
    return root


def _make_installed_project(root: Path) -> Path:
    """Create an installed project: `.claude/scripts/`, NO claude_mcp_servers."""
    (root / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    return root


# ══════════════════════════════════════════════════════════════════════
# 1. The shared resolver: one shape, verbatim, in every script that asks
# ══════════════════════════════════════════════════════════════════════
class TestSharedRootResolver:
    def test_root_resolver_bodies_identical(self):
        """The §3.9 straggler proof: one shape, byte-for-byte.

        A class-C mirror is only legitimate with an enforcing test. This is it.
        """
        blocks = {p.name: _shared_block(p) for p in SHARED_RESOLVER_SCRIPTS}
        distinct = set(blocks.values())
        assert len(distinct) == 1, (
            "the _resolve_orchestrator_root copies have drifted:\n"
            + "\n".join(
                f"  {name}: {len(body)} bytes, sha-ish {hash(body) & 0xFFFFFF:06x}"
                for name, body in blocks.items()
            )
        )

    def test_every_copy_defines_exactly_one_function(self):
        for path in SHARED_RESOLVER_SCRIPTS:
            tree = ast.parse(_shared_block(path))
            fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
            assert fns == ["_resolve_orchestrator_root"], (
                f"{path.name}'s shared block must contain exactly the resolver, "
                f"got {fns}"
            )

    def test_env_arm_wins_over_the_in_tree_guess(self, tmp_path):
        """Rung 1 beats rung 3 — the arm `query_code_graph.py` was missing."""
        orch = _make_orchestrator(tmp_path / "orchestrator")
        proj = _make_installed_project(tmp_path / "userproject")
        script = proj / ".claude" / "scripts" / "query_code_graph.py"

        for path in SHARED_RESOLVER_SCRIPTS:
            resolve = _load_resolver(path, script)
            import os

            os.environ["VCT_ORCHESTRATOR_ROOT"] = str(orch)
            try:
                assert resolve() == orch, path.name
            finally:
                os.environ.pop("VCT_ORCHESTRATOR_ROOT", None)

    def test_install_root_alias_is_honoured(self, tmp_path, monkeypatch):
        """`$VCT_INSTALL_ROOT` is the alias some launcher spawns set instead.

        Before v0.2.92 three of the four scripts read only
        `$VCT_ORCHESTRATOR_ROOT`, so a launcher-spawned run fell through to the
        script-relative guess.
        """
        orch = _make_orchestrator(tmp_path / "orch")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.setenv("VCT_INSTALL_ROOT", str(orch))
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() == orch

    def test_canonical_key_outranks_the_legacy_alias(self, tmp_path, monkeypatch):
        orch_a = _make_orchestrator(tmp_path / "canonical")
        orch_b = _make_orchestrator(tmp_path / "legacy")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(orch_a))
        monkeypatch.setenv("VCT_INSTALL_ROOT", str(orch_b))
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() == orch_a

    def test_a_stale_env_value_is_skipped_not_returned(self, tmp_path, monkeypatch):
        """A candidate that does not CONTAIN claude_mcp_servers/ loses.

        "The env var is set" and "the env var points at an orchestrator" are
        different facts; conflating them is how a moved clone becomes a silent
        wrong answer.
        """
        stale = tmp_path / "moved-away"
        stale.mkdir()
        good = _make_orchestrator(tmp_path / "real")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(stale))
        monkeypatch.setenv("VCT_INSTALL_ROOT", str(good))
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() == good

    def test_in_tree_layout_still_resolves_on_the_orchestrator_clone(
        self, tmp_path, monkeypatch
    ):
        """Rung 3: the orchestrator's own `.claude/scripts/` copy, no env set."""
        orch = _make_orchestrator(tmp_path / "clone")
        (orch / ".claude" / "scripts").mkdir(parents=True)
        monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        resolve = _load_resolver(CLI_SRC, orch / ".claude" / "scripts" / "x.py")
        assert resolve() == orch

    def test_returns_none_rather_than_a_wrong_answer(self, tmp_path, monkeypatch):
        """Could-not-determine is its own value — never a plausible guess."""
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() is None

    def test_empty_and_whitespace_env_values_are_ignored(self, tmp_path, monkeypatch):
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", "   ")
        monkeypatch.setenv("VCT_INSTALL_ROOT", "")
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() is None

    def test_an_env_value_naming_a_file_is_skipped(self, tmp_path, monkeypatch):
        """"Set" is not "valid". A file is not an orchestrator root."""
        decoy = tmp_path / "not-a-directory"
        decoy.write_text("", encoding="utf-8")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(decoy))
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() is None

    def test_an_os_level_path_rejection_is_survived_not_propagated(
        self, tmp_path, monkeypatch
    ):
        """Windows raises ``OSError`` for reserved / malformed names; a CLI must
        skip that candidate, not die on it. Provoked by substituting ``Path``,
        because POSIX cannot even hold such a value in an env var."""
        good = _make_orchestrator(tmp_path / "real")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(tmp_path / "CON-reserved"))
        monkeypatch.setenv("VCT_INSTALL_ROOT", str(good))
        resolve = _load_resolver(
            CLI_SRC,
            proj / ".claude" / "scripts" / "x.py",
            path_cls=_path_exploding_on("CON-reserved"),
        )
        assert resolve() == good

    def test_a_trailing_separator_on_the_env_value_still_resolves(
        self, tmp_path, monkeypatch
    ):
        """Windows users routinely have `C:\\repo\\` in an env var; a POSIX
        user can have `/repo/`. `os.sep` makes the case native on each host."""
        import os

        orch = _make_orchestrator(tmp_path / "orch")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(orch) + os.sep)
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() == orch

    def test_a_foreign_os_path_shape_is_skipped_not_matched(
        self, tmp_path, monkeypatch
    ):
        """A Windows-shaped value read on POSIX (or the reverse) must lose the
        rung, not crash and not match. `is_dir()` is the arbiter, so the check
        is the same code on all three OSes — this pins the SHAPE decision that
        cannot be integration-tested off-platform (R14)."""
        good = _make_orchestrator(tmp_path / "real")
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", "C:\\Program Files\\nope")
        monkeypatch.setenv("VCT_INSTALL_ROOT", str(good))
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() == good

    def test_a_relative_env_value_is_not_silently_accepted(
        self, tmp_path, monkeypatch
    ):
        """A relative value would resolve against the CALLER's cwd, which is
        whatever shell invoked the hook — never a stable answer. It is allowed
        to match only if it really is an orchestrator root from here; the point
        of the assertion is that it is validated like every other rung."""
        proj = _make_installed_project(tmp_path / "proj")
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", "some-relative-dir")
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        resolve = _load_resolver(CLI_SRC, proj / ".claude" / "scripts" / "x.py")
        assert resolve() is None

    def test_no_separator_is_hardcoded_anywhere_in_the_resolver(self):
        """Tri-OS (R14): joins go through pathlib, never through a literal.

        v0.2.81 shipped a Windows ``\\``-separator mass-delete from exactly this
        habit; the resolver is the kind of code that invites it back.
        """
        tree = ast.parse(_shared_block(CLI_SRC))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        doc = fn.body[0] if isinstance(fn.body[0], ast.Expr) else None
        for node in ast.walk(fn):
            if node is doc or (doc is not None and node is doc.value):
                continue  # the docstring may spell paths out for a reader
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "/" not in node.value and "\\" not in node.value, (
                    f"path separator in a string literal: {node.value!r}"
                )

    def test_resolver_reads_both_env_keys_in_every_copy(self):
        for path in SHARED_RESOLVER_SCRIPTS:
            body = _shared_block(path)
            assert 'os.environ.get("VCT_ORCHESTRATOR_ROOT"' in body, path.name
            assert 'os.environ.get("VCT_INSTALL_ROOT"' in body, path.name


# ══════════════════════════════════════════════════════════════════════
# 2. B2 — the access matrix resolves on an INSTALLED project
# ══════════════════════════════════════════════════════════════════════
class TestKgAccessReachableFromAnInstalledProject:
    """The whole point of B2: the helper lives in the orchestrator clone, the
    script lives in the user's project, and only the env arm bridges them."""

    def _install(self, tmp_path: Path, script: Path) -> tuple[Path, Path]:
        orch = _make_orchestrator(tmp_path / "orchestrator")
        (orch / "claude_mcp_servers" / "scripts" / "kg_access.py").write_text(
            "SENTINEL = 'real-kg-access'\n", encoding="utf-8"
        )
        proj = _make_installed_project(tmp_path / "userproject")
        dest = proj / ".claude" / "scripts" / script.name
        dest.write_bytes(script.read_bytes())
        return orch, dest

    @pytest.mark.parametrize(
        "script", [CLI_SRC, GET_NODE_INFO, SEARCH_KNOWLEDGE], ids=lambda p: p.name
    )
    def test_helper_directory_is_reachable_when_env_root_is_set(
        self, tmp_path, monkeypatch, script
    ):
        orch, dest = self._install(tmp_path, script)
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(orch))
        resolve = _load_resolver(script, dest)
        root = resolve()
        assert root == orch
        # ...and this is the directory the P1-D blocks put on sys.path.
        helper_dir = root / "claude_mcp_servers" / "scripts"
        assert (helper_dir / "kg_access.py").is_file()

    @pytest.mark.parametrize(
        "script", [CLI_SRC, GET_NODE_INFO, SEARCH_KNOWLEDGE], ids=lambda p: p.name
    )
    def test_without_the_env_arm_the_installed_project_cannot_find_it(
        self, tmp_path, monkeypatch, script
    ):
        """The PRE-FIX shape, spelled out: script-relative alone finds nothing.

        This is why the fallback fired on every install and the launcher's
        cross-project grants were silently dropped.
        """
        orch, dest = self._install(tmp_path, script)
        monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
        script_relative = dest.resolve().parent.parent.parent
        assert not (script_relative / "claude_mcp_servers").is_dir()
        assert _load_resolver(script, dest)() is None

    def test_every_p1d_block_takes_the_shared_resolver(self):
        """No script may re-derive the root next to the shared one.

        Covers ``process_documents.py`` too — it has no `kg_access` block, but
        it asks the same question in `_resolve_mcp_servers_dir()` and must ask
        it the same way. A script that carries the shared function and then
        hand-rolls the ladder anyway is the worst of both.
        """
        for path in SHARED_RESOLVER_SCRIPTS:
            src = path.read_text(encoding="utf-8")
            outside = src.replace(_shared_block(path), "")
            assert "_resolve_orchestrator_root()" in outside, (
                f"{path.name} defines the shared resolver but never calls it"
            )
            assert 'parent.parent.parent / "claude_mcp_servers"' not in outside, (
                f"{path.name} still hardcodes an unvalidated script-relative "
                "orchestrator-root guess outside the shared resolver"
            )


# ══════════════════════════════════════════════════════════════════════
# 3. B1 — the CLI resolves the class the ANALYZER wrote
# ══════════════════════════════════════════════════════════════════════
#: Names chosen so the whitespace axis (where the two rules disagree) and the
#: underscore/hyphen/dot axis (where they agree, and where the KG rule would
#: disagree) are both covered.
PROJECT_NAMES = [
    "VibeCoded Orchestrator",
    "My Cool App",
    "my cool app",
    "SimRacing AI",
    "ACME Corp",
    "Proj v2",
    "Foo-Bar",
    "Camel_Case",
    "foo.bar",
    "simple",
]


@pytest.fixture(scope="module")
def cli_mod():
    """Import the shipped CLI. Mirrors test_codegraph_cli_readpath_v0270.py."""

# v0.2.92 WP-1 — pin the orchestrator root to the REPO UNDER TEST while the
# script is exec'd.
#
# These CLIs resolve `$VCT_ORCHESTRATOR_ROOT` (that is the fix: it is the
# canonical channel, and without it an installed project can never find
# `kg_access`). `kg_access` then does `sys.path.insert(0, <that root>)`
# (claude_mcp_servers/scripts/kg_access.py:113). `claude_mcp_servers` is a
# NAMESPACE package, so on a machine that has a SECOND orchestrator clone and
# an ambient `$VCT_ORCHESTRATOR_ROOT` pointing at it, merely importing this
# script re-points `claude_mcp_servers.*` for the whole pytest process — and a
# later `import claude_mcp_servers.weaviate_mcp.server` gets the OTHER clone's
# code. That is a test whose meaning depends on the developer's shell.
#
# Pinning here is the same discipline as pinning PYTHONPATH: a repo's own
# suite tests THAT repo. Production behaviour is unchanged and correct — a real
# machine has one orchestrator and the env var names it.
    import os

    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    sys.path.insert(0, str(REPO_ROOT))
    _saved = os.environ.get("VCT_ORCHESTRATOR_ROOT")
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    try:
        spec = importlib.util.spec_from_file_location("_qcg_v0292_wp1", CLI_SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if _saved is None:
            os.environ.pop("VCT_ORCHESTRATOR_ROOT", None)
        else:
            os.environ["VCT_ORCHESTRATOR_ROOT"] = _saved
    return mod


class TestPrefixRuleIsTheWritersRule:
    def test_analyzer_writes_with_canonical_class_prefix(self):
        """Prove which rule the WRITER uses before asserting the reader matches.

        The analyzer's ``_sanitize_collection_prefix`` is a thin wrapper; assert
        that from its source rather than importing the 7k-line module.
        """
        src = ANALYZER_SRC.read_text(encoding="utf-8")
        assert "from vco_lib.codegraph_naming import (\n        canonical_class_prefix as _canonical_class_prefix,\n    )" in src, (
            "analyze_code_graph.py must import the canonical (underscore-"
            "PRESERVING) code rule from vco_lib.codegraph_naming"
        )
        fn = next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_sanitize_collection_prefix"
        )
        stmts = [n for n in fn.body if not _is_docstring(n)]
        assert len(stmts) == 1 and isinstance(stmts[0], ast.Return), (
            "the analyzer's prefix wrapper is no longer a thin delegation"
        )
        assert ast.unparse(stmts[0]) == "return _canonical_class_prefix(name)"

    @pytest.mark.parametrize("project", PROJECT_NAMES)
    @pytest.mark.parametrize(
        "base",
        ["CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction"],
    )
    def test_cli_class_equals_the_written_class(self, cli_mod, project, base):
        """THE regression. Red against the pre-fix source for every name with a
        space (4 of the 10 names here)."""
        from vco_lib.codegraph_naming import canonical_class_prefix

        expected = f"{canonical_class_prefix(project)}_{base}"
        assert cli_mod._collection_name(base, project) == expected

    def test_the_space_case_that_returned_nothing(self, cli_mod):
        """Named separately so a failure reads as the incident, not a matrix
        cell. 'VibeCoded Orchestrator' is the orchestrator's own project name;
        the pre-fix answer named a class that has never existed."""
        assert (
            cli_mod._collection_name("CodeFunction", "VibeCoded Orchestrator")
            == "VibeCodedOrchestrator_CodeFunction"
        )
        assert (
            cli_mod._collection_name("CodeFunction", "VibeCoded Orchestrator")
            != "VibeCoded_Orchestrator_CodeFunction"
        )

    def test_no_project_means_the_bare_base(self, cli_mod):
        for falsy in (None, ""):
            assert cli_mod._collection_name("CodeFunction", falsy) == "CodeFunction"

    def test_a_hub_resolved_prefix_round_trips(self, cli_mod):
        """`main()` feeds `_collection_name` the binding-row prefix, which is
        ALREADY sanitized. The rule must be idempotent or the launcher-managed
        path would break while fixing the hand-typed one."""
        from vco_lib.codegraph_naming import canonical_class_prefix

        for name in PROJECT_NAMES:
            once = canonical_class_prefix(name)
            assert cli_mod._collection_name("CodeClass", once) == f"{once}_CodeClass"

    def test_a_pathological_name_degrades_instead_of_crashing(self, cli_mod):
        """`canonical_class_prefix` RAISES for a leading-digit name. The shared
        MCP wrapper degrades to the "vct" sentinel; the CLI inherits that
        posture because it calls the same function."""
        assert cli_mod._collection_name("CodeFunction", "9lives") == "vct_CodeFunction"

    def test_the_code_rule_is_not_the_kg_rule(self, cli_mod):
        """Two rules coexist on purpose. Collapsing them is a data-routing bug:
        the KG rule DROPS underscores, the code rule PRESERVES them."""
        from vco_lib.codegraph_naming import sanitize_for_weaviate_class

        assert sanitize_for_weaviate_class("SimRacing_AI") == "SimRacingAI"
        assert cli_mod._collection_name("CodeFunction", "SimRacing_AI") == (
            "SimRacing_AI_CodeFunction"
        )

    def test_coll_and_the_fanout_agree(self, cli_mod):
        """One file, one answer. Pre-fix, `_coll()` and the kg_access fan-out
        disagreed for any spaced name — `search` found rows, `similar` and
        `structure` did not."""
        for name in PROJECT_NAMES:
            querier = cli_mod.CodeGraphQuery(project=name)
            pairs = cli_mod._code_graph_collections_to_query(
                self_project=name, bases=("CodeFunction",)
            )
            assert querier._coll("CodeFunction") == pairs[0][0], name


class TestNoPrivatePrefixRegexSurvives:
    """Mirrors the retrieval lane's precedent (a test forbidding a sanitizer
    NAME in the body of the script that must not have one)."""

    def test_the_private_sanitizer_is_gone(self):
        src = CLI_SRC.read_text(encoding="utf-8")
        assert "def _sanitize_collection_prefix" not in src

    def test_no_live_prefix_regex_anywhere_in_the_file(self):
        """AST-level, so the historical note in the docstring does not trip it —
        a comment describing the removed rule is documentation, a Call is a
        second implementation."""
        tree = ast.parse(CLI_SRC.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name not in {"sub", "subn", "compile"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            pattern = node.args[0].value
            if isinstance(pattern, str) and re.search(
                r"[aA]-[zZ]A?-?[zZ]?0-9", pattern
            ):
                offenders.append((node.lineno, pattern))
        assert offenders == [], (
            "query_code_graph.py grew a private character-class prefix rule "
            f"again: {offenders}"
        )

    def test_the_prefix_comes_from_the_shared_home(self):
        src = CLI_SRC.read_text(encoding="utf-8")
        assert "_code_sanitize_collection_prefix," in src, (
            "the code-prefix rule must be imported from weaviate_mcp.server, "
            "the home the MCP uses"
        )
        assert "return f\"{_code_sanitize_collection_prefix(project)}_{base}\"" in src

    def test_the_degraded_fanout_also_uses_the_shared_home(self, cli_mod):
        """The except-branch used to carry its OWN inline copy of the rule.
        What the fallback loses is the access MATRIX, never the prefix."""
        src = CLI_SRC.read_text(encoding="utf-8")
        fallback = src.split("def _code_graph_collections_to_query(  # type: ignore", 1)
        assert len(fallback) == 2, "the self-only fallback disappeared"
        body = fallback[1].split("\n# ", 1)[0]
        assert "_code_sanitize_collection_prefix(self_project)" in body
        assert "re.sub" not in body and "_re.sub" not in body


# ══════════════════════════════════════════════════════════════════════
# 4. Delivery audit (R17 check 1) — verified, not assumed
# ══════════════════════════════════════════════════════════════════════
WP1_SHIPPED_FILES = (
    "query_code_graph.py",
    "get_node_info.py",
    "search_knowledge.py",
    "process_documents.py",
    "vct_project_config.sh",
    "vct_project_config.ps1",
    "vct_secrets_resolve.sh",
    "vct_secrets_resolve.ps1",
)


class TestDelivery:
    def test_every_wp1_file_matches_a_bundle_glob(self):
        """`install-bundle --update` only ships what `script_patterns()` matches.
        Quote the match; never assume it."""
        from vco_lib.bundle_globs import script_patterns

        patterns = script_patterns()
        for name in WP1_SHIPPED_FILES:
            matched = [p for p in patterns if _fnmatch(name, p)]
            assert matched, f"{name} matches NO bundle glob in {patterns}"

    def test_the_files_exist_where_the_bundle_walks(self):
        for name in WP1_SHIPPED_FILES:
            assert (SCRIPTS / name).is_file(), f"templates/scripts/{name} missing"

    def test_no_machine_specific_path_leaked_in(self):
        """R17 check 4 — nothing may assume this machine's layout."""
        needles = ("/home/martino", "PROGETTI", "VCO_dev", "C:\\Users\\martino")
        for name in WP1_SHIPPED_FILES:
            src = (SCRIPTS / name).read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                assert needle not in src, f"{name} carries {needle!r}"


# ══════════════════════════════════════════════════════════════════════
# 5. Boundaries WP-1 must not cross (and the ones it hands off)
# ══════════════════════════════════════════════════════════════════════
class TestMarkerRegionsAreIntactForTheRewriter:
    """WP-16 builds the install-time rewriter for these regions (R4/R21). WP-1
    settles the RUNTIME resolution inside them and must leave the sentinels
    exactly where they are, in exactly these ten files."""

    MARKER_BEARING = (
        "query_code_graph.py",
        "get_node_info.py",
        "process_documents.py",
        "sync_knowledge_graph.py",
        "analyze_code_graph.py",
        "maintain_knowledge_graph.py",
        "vct_project_config.sh",
        "vct_project_config.ps1",
        "vct_secrets_resolve.sh",
        "vct_secrets_resolve.ps1",
    )

    def test_exactly_ten_balanced_regions(self):
        found = {}
        for path in sorted(SCRIPTS.iterdir()):
            if not path.is_file():
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            begins = src.count("VCO-REWIRE-BEGIN: orchestrator-root-resolution")
            ends = src.count("VCO-REWIRE-END: orchestrator-root-resolution")
            if begins or ends:
                assert begins == ends == 1, f"{path.name}: {begins} begin/{ends} end"
                found[path.name] = begins
        assert tuple(sorted(found)) == tuple(sorted(self.MARKER_BEARING))


class TestTheShellPairDecision:
    """WP-1's answer for `vct_{project_config,secrets_resolve}.{sh,ps1}`:
    there is NOTHING to unify. Their marker regions are comment-only — those
    scripts resolve nothing; the hub owns the lookup. Pinned so a future editor
    cannot quietly add a fifth, divergent shell ladder."""

    SHELL_PAIR = (
        "vct_project_config.sh",
        "vct_project_config.ps1",
        "vct_secrets_resolve.sh",
        "vct_secrets_resolve.ps1",
    )

    @pytest.mark.parametrize("name", SHELL_PAIR)
    def test_no_orchestrator_root_resolution_in_the_shell_pair(self, name):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        body = "\n".join(
            line
            for line in src.splitlines()
            if not line.lstrip().startswith("#")
        )
        for key in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT"):
            assert key not in body, (
                f"{name} grew an orchestrator-root ladder. It has none by "
                "design (the hub owns the lookup); adding one here means "
                "unifying it with _resolve_orchestrator_root's contract first."
            )

    @pytest.mark.parametrize("name", SHELL_PAIR)
    def test_both_flavours_stay_in_lockstep(self, name):
        """Tri-OS: a `.sh` without its `.ps1` sibling is a Windows outage."""
        sibling = name.replace(".sh", ".ps1") if name.endswith(".sh") else name.replace(".ps1", ".sh")
        assert (SCRIPTS / sibling).is_file(), f"{name} lost its {sibling} sibling"


class TestUnmigratedLaddersAreNamedNotForgotten:
    """Three more marker-bearing scripts carry their own root ladder and are
    owned by other lanes. Naming them here means the next editor inherits the
    list instead of re-deriving it — and the assertion goes red the moment one
    of them is unified, which is the prompt to add it above."""

    OTHER_OWNERS = {
        "analyze_code_graph.py": "not WP-1's file; ratchet-pinned at exactly 7227 lines",
        "sync_knowledge_graph.py": "WP-4's file",
        "maintain_knowledge_graph.py": "no lane owns it this cycle",
    }

    @pytest.mark.parametrize("name", sorted(OTHER_OWNERS))
    def test_still_carries_its_own_ladder(self, name):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert _SHARED_BEGIN not in src, (
            f"{name} adopted the shared resolver — add it to "
            "SHARED_RESOLVER_SCRIPTS so the byte-equality test covers it."
        )
