# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-18 (W14) — delivery, promise audit, and cross-language parity.

Four things the engine's unit tests cannot answer:

1. **Does the command actually run?** Argv-shape tests miss parser rejections;
   this repo has the standing lesson. The CLI is invoked as a real subprocess.
2. **Is every printed command real?** A printed command is shipped code. W3
   shipped ``vco codegraph analyze`` in two remediations for a verb that does
   not exist. Same AST gate here, over this package's modules.
3. **Does the never-drop rule hold structurally?** Not "we were careful" — the
   AST is walked to prove the only call to a class deletion in the engine is
   inside the guarded drop.
4. **Does the Rust fixer match the Python classification?** The registry says
   ``kg_dir_path`` is ``targeted-update``; the fix is Rust. A split with no
   lock drifts.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import collection_rename as cr  # noqa: E402
from vco_lib import path_bearing_keys as pbk  # noqa: E402
from vco_lib import project_move as pm  # noqa: E402

BINDINGS_WRITER = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
    / "bindings_writer.rs"
)
MOVE_WRITER = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
    / "projects.rs"
)
HUB_CLI_API = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "cli_api.rs"
)
#: The shared Python<->Rust corpus for the path-ancestry mirror (MAJOR-12).
#: Read by `TestPathAncestryParity` below and by
#: `bindings_writer.rs::path_ancestry_matches_shared_fixture`.
ANCESTRY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "path_ancestry_parity.json"
TAURI_LIB = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"
PROJECTS_V2 = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke the REAL CLI with PYTHONPATH pinned to THIS checkout, so the
    test cannot silently exercise a different installed copy."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.cli", *args],
        capture_output=True, text=True, env=env,
    )


def emitted_strings(module) -> list[str]:
    """String literals a module can EMIT — docstrings excluded.

    Scanning raw source would flag the prose that documents these very
    defects. A scanner that cannot tell code from prose forces the comments to
    be reworded around it, which is how a gate teaches people to hide things
    from it. (Same approach as W3's gate; the helper is duplicated rather than
    imported because importing across test modules couples two suites'
    collection order — if a third copy appears, extract to tests/common.)
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


# ───────────────────────────────────────────────────────────────────────────
# The live CLI
# ───────────────────────────────────────────────────────────────────────────


class TestLiveCli(unittest.TestCase):
    def test_the_verb_parses(self):
        proc = run_cli(["project", "rename-collections", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--drop-retired", proc.stdout)

    def test_the_verb_is_mounted_on_the_project_family(self):
        proc = run_cli(["project", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rename-collections", proc.stdout)

    def test_missing_arguments_are_refused_not_crashed(self):
        proc = run_cli(["project", "rename-collections"])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("required", proc.stderr)

    def test_an_unknown_project_is_a_clean_json_refusal(self):
        proc = run_cli(["project", "rename-collections",
                        "definitely-not-a-project", "--to", "X",
                        "--dry-run", "--json"])
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        import json

        body = json.loads(proc.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["refused"], "project_not_found")

    def test_status_on_a_folder_with_nothing_in_flight_is_honest(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            proc = run_cli(["project", "rename-collections", "--status",
                            "--folder", td])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("No rename is in flight", proc.stdout)

    def test_drop_retired_without_confirm_refuses_and_exits_nonzero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            proc = run_cli(["project", "rename-collections", "--drop-retired",
                            "--folder", td])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("REFUSED", proc.stdout)

    def test_resume_without_a_sentinel_refuses(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            proc = run_cli(["project", "rename-collections", "--resume",
                            "--folder", td])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("Nothing to resume", proc.stderr)


# ───────────────────────────────────────────────────────────────────────────
# Promise audit (R16)
# ───────────────────────────────────────────────────────────────────────────


class TestPrintedCommands(unittest.TestCase):
    """A printed command is shipped code and is reviewed as code."""

    def _cli_verbs(self) -> set[str]:
        import argparse

        from vco_lib.cli.__main__ import _build_parser

        parser = _build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        return set()

    def _project_verbs(self) -> set[str]:
        import argparse

        from vco_lib.cli.__main__ import _build_parser

        parser = _build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                proj = action.choices["project"]
                for sub in proj._actions:
                    if isinstance(sub, argparse._SubParsersAction):
                        return set(sub.choices)
        return set()

    def test_every_vco_verb_the_engine_prints_exists(self):
        from vco_lib.cli import project_cmd

        verbs = self._cli_verbs()
        printed: set[str] = set()
        for module in (cr, project_cmd):
            for literal in emitted_strings(module):
                printed.update(re.findall(r"\bvco ([a-z][a-z0-9-]*)", literal))
        unknown = {v for v in printed if v not in verbs}
        self.assertEqual(
            unknown, set(),
            f"the engine PRINTS `vco <verb>` for verbs the CLI does not "
            f"define, so following the remediation exits 2. "
            f"Known verbs: {sorted(verbs)}")
        self.assertIn("project", printed)

    def test_every_project_subverb_the_engine_prints_exists(self):
        """The level W3's gate did not reach: `vco project <subverb>`.

        `vco project` is a real verb, so a gate that stops at the first token
        would happily pass `vco project drop-collections`.
        """
        from vco_lib.cli import project_cmd

        subverbs = self._project_verbs()
        printed: set[str] = set()
        for module in (cr, project_cmd):
            for literal in emitted_strings(module):
                printed.update(
                    re.findall(r"\bvco project ([a-z][a-z0-9-]*)", literal))
        unknown = {v for v in printed if v not in subverbs}
        self.assertEqual(unknown, set(),
                         f"unknown `vco project <subverb>`; real subverbs: "
                         f"{sorted(subverbs)}")
        self.assertIn("rename-collections", printed)

    def test_every_flag_the_drop_command_prints_is_a_real_option(self):
        cmd = cr.drop_retired_command(Path("/w/p"))
        proc = run_cli(["project", "rename-collections", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in re.findall(r"(--[a-z][a-z0-9-]*)", cmd):
            self.assertIn(flag, proc.stdout,
                          f"{flag} is printed but is not a real option")

    def test_the_resume_command_flags_are_real(self):
        proc = run_cli(["project", "rename-collections", "--help"])
        for flag in ("--resume", "--folder"):
            self.assertIn(flag, proc.stdout)

    def test_the_analyzer_remediation_names_a_shipped_wrapper(self):
        for platform, name in (("posix", "code-graph-analyze"),
                               ("nt", "code-graph-analyze.ps1")):
            cmd = cr._analyze_wrapper(Path("/w/p"), platform=platform)
            self.assertTrue(cmd.endswith(name))
            shipped = REPO_ROOT / "templates" / "scripts" / name
            self.assertTrue(
                shipped.is_file(),
                f"the remediation names {name}, which must be a shipped "
                f"template or the command cannot exist in a user project")

    def test_the_remediation_carries_project_IDENTITY_not_a_folder_basename(self):
        """`--project` selects the collection family. Deriving it from the
        folder basename would walk the code into a DIFFERENT collection."""
        source = Path(cr.__file__).read_text(encoding="utf-8")
        self.assertIn("quote_for_shell(plan.new_name)", source)
        self.assertNotIn("folder.name", source)

    def test_every_condition_this_package_emits_is_in_the_registry(self):
        from vco_lib import deferral_registry as reg

        for cid in (cr.CID_OLD_RETAINED, cr.CID_RECONCILE_PENDING,
                    cr.CID_IDENTITY_REMINT_INCOMPLETE):
            spec = reg.condition(cid)
            self.assertIsNotNone(
                spec, f"{cid} is emitted but has no lifecycle declared")
            self.assertEqual(spec.owner, "vco_lib.collection_rename")
        # The record must NOT be install-owned: an install run that did not
        # re-detect an owned cid DROPS it, which would delete the only pointer
        # to a retired class family while the classes still exist.
        self.assertNotIn(cr.CID_OLD_RETAINED, reg.install_owned_ids())

    def test_every_paired_resolution_this_package_declares_is_wired(self):
        """The registry PROMISES that these entries clear themselves. This is
        the other half: the module must actually call `resolve_conditions`."""
        source = Path(cr.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("resolve_conditions("), 2,
                         "one call site per paired-resolution promise; a "
                         "declared pairing with no call is a promise")
        for cid in ("CID_OLD_RETAINED", "CID_RECONCILE_PENDING"):
            self.assertIn(cid, source)

    def test_the_refusal_table_covers_every_reason_the_module_raises(self):
        """A refusal whose reason is not in the table renders as a bare code."""
        tree = ast.parse(Path(cr.__file__).read_text(encoding="utf-8"))
        raised = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "RenameRefused"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                raised.add(node.args[0].value)
        missing = raised - set(cr.REFUSAL_REASONS)
        self.assertEqual(missing, set(),
                         f"refusal reasons with no explanation: {missing}")


# ───────────────────────────────────────────────────────────────────────────
# The never-drop invariant, proven structurally
# ───────────────────────────────────────────────────────────────────────────


class TestNeverDrops(unittest.TestCase):
    def test_class_deletion_is_reachable_only_from_the_guarded_drop(self):
        """Not "we were careful" — the AST is walked.

        Every `ops.delete_class(...)` in the engine must be lexically inside
        `drop_retired`. A cleanup path that deletes a half-copied destination
        would look reasonable in review and would be the one line that turns
        this feature into data loss.
        """
        tree = ast.parse(Path(cr.__file__).read_text(encoding="utf-8"))
        offenders = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "delete_class"
                        and func.name != "drop_retired"):
                    offenders.append(f"{func.name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            f"class deletion outside the guarded drop: {offenders}")

    def test_the_engine_never_calls_delete_class_from_module_scope(self):
        tree = ast.parse(Path(cr.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "delete_class"
                        and isinstance(node, (ast.Expr, ast.Assign))):
                    self.fail("module-scope class deletion")

    def test_verify_failure_does_not_clean_up_by_deleting(self):
        source = Path(cr.__file__).read_text(encoding="utf-8")
        verify = source[source.index("def verify_copy("):
                        source.index("def execute_reconcile(")]
        self.assertNotIn("delete_class", verify)
        self.assertIn("LEFT IN PLACE", verify)


# ───────────────────────────────────────────────────────────────────────────
# Cross-language parity + Rust delivery
# ───────────────────────────────────────────────────────────────────────────


class TestRustSide(unittest.TestCase):
    def setUp(self):
        self.writer = BINDINGS_WRITER.read_text(encoding="utf-8")
        self.move = MOVE_WRITER.read_text(encoding="utf-8")

    def test_the_flip_lives_in_the_sanctioned_single_writer_home(self):
        self.assertIn("pub fn commit_collection_rename", self.writer)
        # …and NOT in the move writer, whose own gate forbids identity SQL.
        self.assertNotIn("commit_collection_rename(", self.move.split(
            "#[cfg(test)]")[0])

    def test_the_flip_is_one_transaction(self):
        section = self.writer[self.writer.index("pub fn commit_collection_rename"):]
        section = section[:section.index("// Observability AFTER")]
        self.assertEqual(section.count(".transaction()"), 1)
        self.assertEqual(section.count("tx.commit()"), 1)
        for stmt in ("UPDATE projects SET name",
                     "SET collection_name = ?1",
                     "SET collection_prefix = ?1"):
            self.assertIn(stmt, section,
                          f"{stmt} must be inside the one transaction")

    def test_the_flip_never_touches_the_shared_role_implicitly(self):
        section = self.writer[self.writer.index("pub fn commit_collection_rename"):]
        self.assertIn("AND role = ?4", section,
                      "the KG update must be role-scoped; an unscoped UPDATE "
                      "would move the SHARED binding every project reads")

    def test_the_fixer_exists_for_the_targeted_update_column(self):
        """The classification is Python, the fix is Rust. Lock the split."""
        entries = list(pbk.columns_with_policy(pbk.POLICY_TARGETED_UPDATE))
        kg = [e for e in entries if e.column == "kg_dir_path"]
        self.assertTrue(kg, "kg_dir_path must now be targeted-update")
        self.assertIn("pub fn repoint_kg_dir_path", self.writer)
        self.assertIn("repoint_kg_dir_path", self.move,
                      "the fixer must be CALLED from the move's commit")

    def test_the_fixer_is_component_wise_not_a_string_replace(self):
        self.assertIn("fn is_ancestor", self.writer)
        self.assertNotIn("REPLACE(kg_dir_path", self.writer)

    def test_the_ancestry_mirror_names_its_python_home(self):
        """A tier-C cross-language mirror must say what it mirrors."""
        self.assertIn("MUST MATCH", self.writer)
        self.assertIn("project_move.py", self.writer)

    def test_the_mirror_comment_names_a_corpus_that_actually_exists(self):
        """v0.2.92 MAJOR-12. The comment used to name THIS file as the thing
        bounding the divergence risk, "driving the SAME fixture table through
        both implementations". There was no table; the only checks were the
        two `assertIn` above, which a comment satisfies. Pin the real one."""
        self.assertIn("path_ancestry_parity.json", self.writer,
                      "the mirror comment must name the shared corpus")
        self.assertTrue(ANCESTRY_FIXTURE.is_file(),
                        f"{ANCESTRY_FIXTURE} is the shared corpus both sides "
                        "assert against — it must exist")
        self.assertIn("fn path_ancestry_matches_shared_fixture", self.writer,
                      "the Rust side must READ the corpus, not just mention "
                      "it — a named-but-unread fixture is the same silence")

    def test_the_rust_rebuild_uses_the_same_component_rule(self):
        """`repoint_kg_dir_path` slices the ORIGINAL-case components at an
        offset computed from the compare-key components. Two different `..`
        rules would slice at the wrong offset the moment a stored path held a
        `..`, so the rebuild must go through `path_parts` too."""
        section = self.writer[self.writer.index("pub fn repoint_kg_dir_path_ci"):]
        section = section[:section.index("\n// ═")]
        self.assertIn("let original = path_parts(&current, false);", section)
        self.assertNotIn('.filter(|p| !p.is_empty() && *p != ".")', section,
                         "an inline splitter here re-forks the rule that "
                         "path_parts owns")


class TestPathAncestryParity(unittest.TestCase):
    """The tier-C mirror in `bindings_writer.rs`, actually pinned.

    Rust has ONE `path_parts` used on three OSes; Python has three shapes
    selected by `platform=`. Every vector declares its shape, and both
    languages assert against the same file — so a one-sided edit to either
    implementation fails on BOTH sides, which is what CLAUDE.md's class-C
    mirror rule requires and what the comment previously only claimed.
    """

    #: shape name -> the `platform=` value that selects it in Python.
    SHAPES = {
        "posix-sensitive": "linux",
        "posix-insensitive": "darwin",
        "windows": "win32",
    }

    @classmethod
    def setUpClass(cls):
        # A missing or unparsable corpus is a FAILURE, never a skip: a skip
        # here restores exactly the silence this class exists to end.
        cls.data = json.loads(ANCESTRY_FIXTURE.read_text(encoding="utf-8"))

    def test_the_corpus_is_not_empty_and_declares_known_shapes(self):
        self.assertTrue(self.data["parts"], "no `parts` vectors")
        self.assertTrue(self.data["ancestry"], "no `ancestry` vectors")
        for row in self.data["parts"] + self.data["ancestry"]:
            self.assertIn(row["shape"], self.SHAPES)

    def test_python_components_match_the_corpus(self):
        for row in self.data["parts"]:
            with self.subTest(shape=row["shape"], path=row["path"]):
                self.assertEqual(
                    list(pm._parts(row["path"],
                                   platform=self.SHAPES[row["shape"]])),
                    row["expected"])

    def test_python_ancestry_matches_the_corpus(self):
        for row in self.data["ancestry"]:
            with self.subTest(shape=row["shape"], a=row["ancestor"],
                              d=row["descendant"]):
                self.assertEqual(
                    pm.is_ancestor(row["ancestor"], row["descendant"],
                                   platform=self.SHAPES[row["shape"]]),
                    row["expected"])

    def test_the_corpus_covers_the_dot_dot_case_that_diverged(self):
        """Guard against a future edit quietly deleting the vector that this
        whole exercise exists for: `/a/b/../..` is NOT under `/a`."""
        wanted = {("/a", "/a/b/../..", False)}
        got = {(r["ancestor"], r["descendant"], r["expected"])
               for r in self.data["ancestry"]}
        self.assertTrue(wanted <= got,
                        f"the corpus lost the MAJOR-12 vector: {wanted - got}")
        with_dotdot = [r for r in self.data["parts"] if ".." in r["path"]]
        self.assertGreaterEqual(
            len(with_dotdot), 6,
            "the corpus must keep exercising `..` on every shape")

    def test_dot_dot_cannot_climb_above_a_root(self):
        """Stated as behaviour, not only as data — this is the chosen rule."""
        self.assertEqual(pm._parts("/../a", platform="linux"), ("a",))
        self.assertEqual(pm._parts("a/../../b", platform="linux"), ("..", "b"))
        self.assertEqual(pm._parts(r"C:\..\x", platform="win32"), ("c:", "x"))

    def test_the_windows_shape_collapses_dot_dot_like_every_other_shape(self):
        """MAJOR-12 side-finding: `path_compare_key`'s Windows-emulation branch
        skipped `normpath` entirely, so Python disagreed with ITSELF across
        shapes before it could disagree with Rust."""
        self.assertEqual(pm._parts(r"C:\a\b\..\..", platform="win32"), ("c:",))
        self.assertFalse(
            pm.is_ancestor(r"C:\Proj", r"C:\Proj\kg\..\..", platform="win32"))

    def test_the_hub_route_is_registered(self):
        api = HUB_CLI_API.read_text(encoding="utf-8")
        self.assertIn('"/cli/projects/{id_or_slug}/rename-collections"', api)
        self.assertIn("async fn rename_collections", api)
        self.assertIn("orchestrator_root", api,
                      "the hub surface must refuse the root project too")

    def test_the_tauri_command_has_a_frontend_consumer(self):
        """A registered command nothing calls is R16's unwired promise.

        The Settings tab is the consumer; the typed shapes live beside the
        existing `RenameProjectResult` in the shared types module.
        """
        tab = (REPO_ROOT / "launcher" / "src" / "lib" / "project-state"
               / "SettingsTab.svelte").read_text(encoding="utf-8")
        self.assertIn("'rename_collections_v2'", tab)
        types = (REPO_ROOT / "launcher" / "src" / "lib" / "types"
                 / "launcher.ts").read_text(encoding="utf-8")
        for name in ("RenameCollectionsResult", "RenameCollectionsPreview",
                     "RenameClassMove"):
            self.assertIn(f"export interface {name}", types)

    def test_the_gui_never_says_the_old_classes_were_removed(self):
        """The single most dangerous UI copy this feature could ship."""
        tab = (REPO_ROOT / "launcher" / "src" / "lib" / "project-state"
               / "SettingsTab.svelte").read_text(encoding="utf-8")
        panel = tab[tab.index("<h3>Rename collections</h3>"):]
        panel = panel[:panel.index("</section>")]
        self.assertIn("never dropped", panel)
        for forbidden in ("deleted", "removed", "will be dropped"):
            self.assertNotIn(forbidden, panel.lower(),
                             f"the panel must not imply the old classes are "
                             f"{forbidden}")

    def test_the_tauri_command_is_registered_with_the_handler(self):
        self.assertIn("commands::projects_v2::rename_collections_v2",
                      TAURI_LIB.read_text(encoding="utf-8"))
        self.assertIn("pub async fn rename_collections_v2",
                      PROJECTS_V2.read_text(encoding="utf-8"))

    def test_the_gui_drives_the_same_cli_rather_than_reimplementing(self):
        src = PROJECTS_V2.read_text(encoding="utf-8")
        section = src[src.index("pub async fn rename_collections_v2"):]
        self.assertIn("run_rename_cli", section)
        self.assertNotIn("_copy_collection_with_vectors", section,
                         "the GUI surface must drive the CLI, never carry a "
                         "second copy implementation")

    def test_the_gui_reuses_the_shared_interpreter_ladder(self):
        src = PROJECTS_V2.read_text(encoding="utf-8")
        self.assertIn("python_resolve::resolve_python_for_vco_lib", src,
                      "a second interpreter resolver is the duplication the "
                      "house rules forbid")


# ───────────────────────────────────────────────────────────────────────────
# Delivery (R17)
# ───────────────────────────────────────────────────────────────────────────


class TestContainerRuntimeNeutrality(unittest.TestCase):
    """R13: never hardcode a container runtime in user-facing text.

    This package talks to Weaviate over HTTP at a RESOLVED url and never
    invokes a runtime, so compliance here is about the remediation text: a
    message telling a podman user to run `docker compose up` is a command that
    cannot work, and this repo has shipped that shape before. The gate pins
    the absence so a future edit cannot casually add one.
    """

    def test_no_remediation_names_a_container_runtime(self):
        from vco_lib.cli import project_cmd

        for module in (cr, project_cmd):
            for literal in emitted_strings(module):
                low = literal.lower()
                for banned in ("docker", "podman", "docker compose",
                               "podman-compose"):
                    self.assertNotIn(
                        banned, low,
                        f"{module.__name__} emits {banned!r}; the user's "
                        f"runtime is whichever one they actually have")

    def test_the_weaviate_url_is_resolved_not_hardcoded(self):
        source = Path(cr.__file__).read_text(encoding="utf-8")
        self.assertIn("weaviate_url_default()", source)
        self.assertNotIn("localhost:8081", source,
                         "the port belongs to the shared resolver")


class TestDelivery(unittest.TestCase):
    def test_the_engine_ships_inside_the_installed_package(self):
        """`vco_lib` is installed with `pip install -e .`, so a new module in
        it reaches every user with no manifest entry. Assert the package is
        actually declared rather than assuming it."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("vco_lib", pyproject)
        self.assertIn('vco = "vco_lib.cli.__main__:main"', pyproject)

    def test_this_package_adds_no_templates_file(self):
        """Stated rather than assumed: WP-18 ships no new `templates/**` file,
        so there is no bundle-glob row to verify. If that ever changes, this
        test fails and the author must add the glob check."""
        for name in ("collection-rename", "rename-collections"):
            matches = list((REPO_ROOT / "templates").rglob(f"*{name}*"))
            self.assertEqual(matches, [], f"unexpected template: {matches}")

    def test_a_downgrade_ignores_the_new_state_files(self):
        """An older launcher reads neither the sentinel nor the completed
        record; both live under `.claude/` and are pure additions. Pin the
        paths so a future move into a shared file is a conscious decision."""
        self.assertEqual(cr.SENTINEL_REL.parts[0], ".claude")
        self.assertEqual(cr.COMPLETED_REL.parts[0], ".claude")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
