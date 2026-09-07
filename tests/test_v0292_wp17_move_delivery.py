# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-17 (W3) — delivery, cross-language parity, and the CLI surface.

Three things the engine's own unit tests cannot answer:

1. **Does the fixer exist for every column the registry says has one?**
   The classification lives in Python and the fix lives in Rust. That split is
   deliberate (a Rust TOML read per move would buy nothing), but a split with
   no lock drifts — so the Rust writer's source is scanned for each
   ``targeted-update`` column.
2. **Does the command actually run?** Argv-shape tests miss parser rejections;
   this repo has the standing lesson. The CLI is invoked as a real subprocess.
3. **Does it reach a third-party machine?** ``vco project`` ships through the
   ``vco`` console script, and ``kg-sync`` ships through the bundle globs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from fnmatch import fnmatch
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.rust_source import (  # noqa: E402
    cfg_test_line_numbers,
    strip_rust_comments,
)
from vco_lib import path_bearing_keys as pbk  # noqa: E402

RUST_WRITER = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "projects.rs"
)
KG_SYNC = REPO_ROOT / "templates" / "scripts" / "kg-sync"
KG_SYNC_PS1 = REPO_ROOT / "templates" / "scripts" / "kg-sync.ps1"


def run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke the REAL CLI as a subprocess with PYTHONPATH pinned to this
    checkout, so the test cannot silently exercise a different installed copy."""
    env = {
        **{k: v for k, v in __import__("os").environ.items()},
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


# ───────────────────────────────────────────────────────────────────────────
# Cross-language: classification (Python) vs fixer (Rust)
# ───────────────────────────────────────────────────────────────────────────


class TestRegistryFixerParity(unittest.TestCase):
    """The A>B>C decision, locked.

    The classification of all 194 columns is Python (tier A: one home). The
    FIX for the three columns that need one is Rust, because it must run
    inside the flip transaction in a sanctioned writer. That is a deliberate
    split, not a mirror — but a split with no test drifts the first time
    someone adds a fourth ``targeted-update`` column and forgets the Rust half.
    """

    #: The comment banner that opens the move writer. It is PROSE, so it can
    #: only ever be located in the RAW source — see the region note in
    #: :meth:`test_no_identity_column_is_written_by_the_move_writer`.
    MOVE_SECTION_ANCHOR = "Project MOVE (v0.2.92"

    def setUp(self):
        #: Raw source. Kept because the section anchor above lives in a
        #: comment, so the region's START can only be found here.
        self.rust = RUST_WRITER.read_text(encoding="utf-8")
        #: The CODE view: comments gone, string literals VERBATIM
        #: (``strip_rust_comments``, not ``scrub_rust_lines``). That asymmetry
        #: is the point and must not be "simplified":
        #:
        #: * a forbidden construct in a COMMENT is inert — a doc-comment that
        #:   quotes ``SET folder_path = ?1`` to explain the single writer, or
        #:   an ``// avoid REPLACE(file_path …)`` warning, is prose about the
        #:   rule, not a breach of it. Counting it fails the gate on its own
        #:   documentation, which is how a gate ends up teaching people to
        #:   reword their comments around it;
        #: * the same construct in a STRING LITERAL may be real: that is where
        #:   SQL about to be handed to ``conn.execute`` lives. Scrubbing
        #:   strings would turn every one of these gates into a permanent pass.
        #:
        #: One output line per input line, so line numbers index both views.
        self.code_lines = strip_rust_comments(self.rust)
        self.code = "\n".join(self.code_lines)
        #: 1-indexed lines inside a ``#[cfg(test)]`` ITEM, per-item and
        #: brace-balanced (NOT "everything after the first marker").
        self.test_lines = cfg_test_line_numbers(self.rust)

    def test_every_targeted_update_column_has_a_fixer_in_the_rust_writer(self):
        for entry in pbk.columns_with_policy(pbk.POLICY_TARGETED_UPDATE):
            self.assertIn(
                entry.column,
                self.code,
                f"{entry.qualified} is classified `targeted-update` — the "
                f"registry promises a fixer recomputes it — but "
                f"{RUST_WRITER.name} never names that column. Either write the "
                f"fixer or change the policy.",
            )
            self.assertIn(
                entry.table,
                self.code,
                f"{entry.qualified}: the fixer must name its table",
            )

    def test_the_flip_column_is_written_by_exactly_one_statement(self):
        #: Whole file, deliberately: a SECOND writer anywhere — including one
        #: reached only from a test harness — is a second opinion about what a
        #: move does. Narrowing this to non-test code would buy nothing today
        #: (there are no test-side occurrences) at the price of a blind spot.
        occurrences = self.code.count("SET folder_path = ?1")
        self.assertEqual(
            occurrences,
            1,
            "folder_path must have ONE writer. A second UPDATE is a second "
            "opinion about what a move does, and the two will drift. "
            f"Occurrences in the comment-stripped source: {occurrences} at "
            f"lines "
            + str(
                [
                    n
                    for n, line in enumerate(self.code_lines, start=1)
                    if "SET folder_path = ?1" in line
                ]
            ),
        )

    def test_the_fixer_uses_the_path_oracle_not_a_string_replace(self):
        self.assertIn(
            "resolve_kind_paths",
            self.code,
            "agent/skill paths are RECOMPUTED through the same oracle the "
            "enable-toggle uses. NOTE this asserts on the CODE view: a "
            "`/// … resolve_kind_paths` doc-comment does not satisfy it, a "
            "real call does.",
        )
        self.assertNotIn(
            "REPLACE(file_path",
            self.code,
            "a SQL REPLACE on a path rewrites any occurrence anywhere in the "
            "value and silently no-ops on a row whose path was already odd",
        )

    def test_no_identity_column_is_written_by_the_move_writer(self):
        """A grep-level guard for THE invariant.

        The behavioural proof is
        ``move_tests::commit_never_touches_identity_columns_leaves_alone``;
        this catches the shape at review time, when a new UPDATE is easier to
        remove than to explain.

        REGION (v0.2.92 WP-17 follow-up). The scan runs from the move
        writer's banner to EOF, minus the lines inside ``#[cfg(test)]``
        ITEMS. It used to stop at the FIRST ``#[cfg(test)]`` marker instead,
        which discarded 825 of this file's 1850 lines — including two live
        ``UPDATE project_moves SET …`` statements. Both happen to sit inside
        the test module today, so the blindness was LATENT; it goes live the
        moment production code is declared below that marker, which is the
        v0.2.90 lesson recorded in ``tests/common/rust_source.py``'s own
        docstring. Per-item skipping is strictly better in BOTH directions:
        the tail is scanned again, and a genuine test fixture is still
        excused.

        The region START stays anchored on a COMMENT and is therefore located
        in the raw source. It is load-bearing, not decoration: ``SET name =``
        appears twice in this file at lines 189/196, in the legitimate
        project-rename writer, so a whole-file scan would report the rename
        path as a move-writer violation.
        """
        raw_lines = self.rust.splitlines()
        anchors = [
            n
            for n, line in enumerate(raw_lines, start=1)
            if self.MOVE_SECTION_ANCHOR in line
        ]
        self.assertEqual(
            len(anchors),
            1,
            f"the move-writer banner {self.MOVE_SECTION_ANCHOR!r} must appear "
            f"exactly once in {RUST_WRITER.name} — this gate's region starts "
            f"there, and a missing or duplicated anchor silently rescopes it. "
            f"Found at lines {anchors}.",
        )
        scanned = [
            (n, self.code_lines[n - 1])
            for n in range(anchors[0], len(raw_lines) + 1)
            if n not in self.test_lines
        ]
        for forbidden in (
            "SET collection_name",
            "SET collection_prefix",
            "SET slug",
            "SET name =",
        ):
            offenders = [
                f"line {n}: {text.strip()}" for n, text in scanned if forbidden in text
            ]
            self.assertEqual(
                offenders,
                [],
                f"the move writer must never {forbidden!r}: identity is "
                f"row-keyed, not path-derived. Offenders: {offenders}",
            )


# ───────────────────────────────────────────────────────────────────────────
# Live CLI — argv-shape tests miss parser rejections
# ───────────────────────────────────────────────────────────────────────────


class TestLiveCli(unittest.TestCase):
    def test_project_move_help_parses(self):
        proc = run_cli(["project", "move", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("--verify", proc.stdout)

    def test_project_family_is_mounted_on_the_top_level_parser(self):
        proc = run_cli(["--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "project",
            proc.stdout,
            "the subcommand must be reachable as `vco project`, not only as "
            "`python -m vco_lib.project_move`",
        )

    def test_move_without_required_arguments_is_refused_not_crashed(self):
        proc = run_cli(["project", "move"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("required", proc.stderr.lower())

    def test_unknown_project_is_a_clean_refusal_with_json(self):
        proc = run_cli(
            ["project", "move", "no-such-project-xyz", "--to", "/tmp", "--dry-run",
             "--json"]
        )
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refused"], "project_not_found")

    def test_engine_plan_phase_requires_its_arguments(self):
        env = {
            **{k: v for k, v in __import__("os").environ.items()},
            "PYTHONPATH": str(REPO_ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_move", "--phase", "plan"],
            capture_output=True,
            text=True,
            env=env,
        )
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("--project-id", payload["error"])

    def test_engine_always_emits_one_json_envelope(self):
        """The subprocess contract the launcher parses.

        A phase that printed nothing on failure would make the Tauri command's
        parse fail and turn a clean refusal into 'the move engine produced no
        readable result'.
        """
        env = {
            **{k: v for k, v in __import__("os").environ.items()},
            "PYTHONPATH": str(REPO_ROOT),
        }
        for args in (
            ["--phase", "verify"],
            ["--phase", "pre-flip"],
            ["--phase", "post-flip"],
        ):
            proc = subprocess.run(
                [sys.executable, "-m", "vco_lib.project_move", *args],
                capture_output=True,
                text=True,
                env=env,
            )
            payload = json.loads(proc.stdout)
            self.assertIn("ok", payload, f"{args} produced no envelope")
            self.assertEqual(payload["schema"], 1)


# ───────────────────────────────────────────────────────────────────────────
# Delivery (§9)
# ───────────────────────────────────────────────────────────────────────────


class TestDelivery(unittest.TestCase):
    def test_kg_sync_pair_is_matched_by_the_bundle_globs(self):
        """VERIFIED fnmatch against the real pattern tuple, not assumed.

        A fix to a bundled script reaches a user project only if the script's
        NAME matches one of `script_patterns()`. `kg-sync` is extension-less,
        so it depends on the `kg-*` pattern specifically — this is exactly the
        kind of thing that is assumed rather than checked.
        """
        from vco_lib.bundle_globs import script_patterns

        patterns = script_patterns()
        for filename in ("kg-sync", "kg-sync.ps1"):
            hits = [p for p in patterns if fnmatch(filename, p)]
            self.assertTrue(
                hits,
                f"{filename} matches NO pattern in {patterns}, so the fix "
                f"would never reach a user project",
            )
        self.assertIn(
            "kg-*",
            patterns,
            "the extension-less `kg-sync` wrapper ships via the `kg-*` "
            "pattern; removing it would silently stop shipping the wrapper",
        )

    def test_vco_console_script_is_declared(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'vco = "vco_lib.cli.__main__:main"',
            pyproject,
            "the `project` family reaches a user only through the `vco` "
            "console script that `pip install -e .` puts on PATH",
        )

    def test_migration_044_is_registered_in_the_rust_runner(self):
        migrations = (
            REPO_ROOT
            / "launcher/src-tauri/vct-launcher-core/src/db/migrations.rs"
        ).read_text(encoding="utf-8")
        self.assertIn("044_project_moves.sql", migrations)
        self.assertIn("version: 44", migrations)

    def test_table_set_version_matches_the_highest_migration(self):
        from vco_lib.schema_versions import LAUNCHER_DB_TABLE_SET_VERSION

        self.assertEqual(
            LAUNCHER_DB_TABLE_SET_VERSION,
            44,
            "a Python-ahead or Rust-ahead bump stamps a phantom schema version",
        )

    def test_schema_versions_json_snapshot_is_in_sync(self):
        snapshot = json.loads(
            (REPO_ROOT / "vco_lib" / "schema_versions.json").read_text("utf-8")
        )
        from vco_lib.schema_versions import LAUNCHER_DB_TABLE_SET_VERSION

        self.assertEqual(
            snapshot["canonical_versions"]["launcher_db_table_set"],
            LAUNCHER_DB_TABLE_SET_VERSION,
        )


# ───────────────────────────────────────────────────────────────────────────
# §4bis — the kg-sync wrappers' honest failure
# ───────────────────────────────────────────────────────────────────────────


def _executed_lines(text: str) -> str:
    """The wrapper's CODE — comment lines dropped, joined back to one blob.

    v0.2.92 (review m29): every source scan in the class below is asserted
    against this, not against the raw file. The class already had ONE test
    doing it by hand (`test_bash_wrapper_has_no_bare_python3_fallback`) for
    exactly the right reason; the others were left scanning raw text, where
    a sentence in the header satisfies them.

    That was not theoretical. `assertIn("exit 1", …)` passed on BOTH wrappers
    while both actually `exit 3` — the only match in either file was the
    changelog line "the venv refusal below used to exit 1". The moment a
    sibling lane reworded that comment to "used to return 1", the assertions
    went red against wrappers that had not changed behaviour at all. An
    assertion a comment can satisfy is an assertion a comment can also break;
    either way it is not measuring the code.

    Both flavours use `#` for line comments. PowerShell's `<# … #>` block
    form is not used in these wrappers; if one appears, this helper must
    learn it rather than the scans quietly widening again.
    """
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


class TestKgSyncWrappers(unittest.TestCase):
    def setUp(self):
        self.sh = KG_SYNC.read_text(encoding="utf-8")
        self.ps1 = KG_SYNC_PS1.read_text(encoding="utf-8-sig")
        self.sh_code = _executed_lines(self.sh)
        self.ps1_code = _executed_lines(self.ps1)

    def test_bash_wrapper_has_no_bare_python3_fallback(self):
        """Asserted on EXECUTED lines only.

        The fix's own comment quotes the assignment it removed, so a naive
        substring scan of the whole file matches the explanation and reports
        the defect as still present. A scan that cannot tell code from prose
        is the kind of test whose name promises more than it asserts.
        """
        code = [
            line
            for line in self.sh.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        offenders = [line for line in code if 'VENV_PYTHON="python3"' in line]
        self.assertEqual(
            offenders,
            [],
            "a shipped component does not get a silent fallback: the bare "
            "python3 arm crashed later with a ModuleNotFoundError that named "
            "the wrong problem",
        )

    # NOTE (review m29): `test_bash_wrapper_exits_non_zero_when_nothing_
    # resolves` used to sit here, asserting `"exit 1" in self.sh` plus the
    # refusal sentence. It is DELETED rather than strengthened: it was
    # redundant with `test_bash_wrapper_refuses_with_no_env_channels_live`
    # below, which RUNS the wrapper and observes the exit code and the
    # message on stderr — and where the grep asserted an exit code the
    # wrapper does not use (1; it exits 3), the live test reads whatever the
    # wrapper really did. The refusal-sentence half was folded into that live
    # test so no coverage is lost. There is no live pwsh equivalent, so the
    # PowerShell sibling is strengthened in place instead of deleted.

    def test_ps1_wrapper_exits_three_when_nothing_resolves(self):
        """Asserted on EXECUTED lines, and on the code the ladder specifies.

        3 = "did not run", distinct from the script's own 1 = "ran, some
        nodes failed". The bash sibling's half of this is executed by
        `test_bash_wrapper_refuses_with_no_env_channels_live`; PowerShell has
        no live equivalent here, so the scan has to be worth something.
        """
        # Anchored to the STATEMENT, not to the substring: the refusal's own
        # `Write-Host "… (exit 3 = did not run; 1 would mean …)"` is executed
        # code, so a bare `"exit 3" in ps1_code` would be satisfied by the
        # explanatory message even if the statement below it said `exit 1`.
        # Prose in a comment and prose in an echo fail the same way.
        exits = re.findall(r"^\s*exit\s+(\S+)\s*$", self.ps1_code, re.MULTILINE)
        self.assertIn("3", exits, f"no literal `exit 3` statement; found {exits}")
        self.assertNotIn(
            "1", exits,
            "the refusal must not reuse the script's 'ran, some nodes "
            f"failed' code — that is the collision the ladder exists to fix "
            f"(exit statements found: {exits})",
        )
        self.assertIn(
            "Refusing to run with an unqualified interpreter", self.ps1_code
        )

    def test_both_siblings_read_the_project_env_tier(self):
        """The durable tier: file-backed, needs no env inheritance at all."""
        self.assertIn("VCT_ORCHESTRATOR_ROOT", self.sh_code)
        self.assertIn("VCT_ORCHESTRATOR_ROOT", self.ps1_code)

    def test_both_siblings_gate_the_clone_relative_tier(self):
        """Without the gate, `<script>/../..` IS the user's project root."""
        self.assertIn("is_vco_orchestrator_clone", self.sh_code)
        self.assertIn("Test-VcoOrchestratorClone", self.ps1_code)
        for text, name in ((self.sh_code, "kg-sync"), (self.ps1_code, "kg-sync.ps1")):
            self.assertIn("first-install.sh", text, f"{name}: clone discriminator")
            self.assertIn("install.py", text, f"{name}: clone discriminator")

    def test_both_siblings_keep_the_dual_import_probe(self):
        """Bug K regression pin: `weaviate` alone is not enough."""
        self.assertIn("import weaviate, weaviate_mcp", self.sh_code)
        self.assertIn("import weaviate, weaviate_mcp", self.ps1_code)

    def test_ps1_keeps_its_bom(self):
        """OS-EXEMPT-PARITY: Windows PowerShell needs the UTF-8 BOM."""
        self.assertTrue(KG_SYNC_PS1.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_bash_wrapper_refuses_with_no_env_channels_live(self):
        """The honest-failure path, executed rather than grepped."""
        import os
        import shutil

        with TemporaryDirectory() as td:
            scripts = Path(td) / ".claude" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy(KG_SYNC, scripts / "kg-sync")
            (scripts / "sync_knowledge_graph.py").write_text(
                "raise SystemExit('THE SYNC SCRIPT MUST NOT RUN')\n"
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("VCT_INSTALL_ROOT", "VCT_VENV", "VIRTUAL_ENV")
            }
            proc = subprocess.run(
                ["bash", str(scripts / "kg-sync"), "--all"],
                capture_output=True,
                text=True,
                env=env,
            )
            # 3 = "did not run". 1 is the SCRIPT's "ran, some nodes failed"
            # — the collision the v0.2.92 ladder was introduced to remove, so
            # asserting 1 here would re-encode the defect. (This assertion
            # said 1 and was red against the shipped wrapper.)
            self.assertEqual(proc.returncode, 3, proc.stdout)
            self.assertIn("no Python environment", proc.stderr)
            # Folded in from the deleted source-scan sibling: the refusal
            # sentence, observed in the output the user actually sees.
            self.assertIn(
                "Refusing to run with an unqualified interpreter", proc.stderr
            )
            # The two resolution tiers, proved by EXECUTION rather than by
            # grepping for their names. This branch is only reachable when
            # the candidate list came back empty, which means the env-file
            # tier was consulted and the clone gate ran and rejected the
            # temp dir — the gate whose absence would have selected the
            # user's own project `.venv`.
            self.assertIn("VCT_ORCHESTRATOR_ROOT in ", proc.stderr)
            self.assertIn("is not a VCO orchestrator clone", proc.stderr)
            self.assertNotIn(
                "THE SYNC SCRIPT MUST NOT RUN",
                proc.stdout + proc.stderr,
                "the wrapper must refuse BEFORE invoking the sync script",
            )


# ───────────────────────────────────────────────────────────────────────────
# Promise audit (§8) — printed commands, parity claims, registry claims
# ───────────────────────────────────────────────────────────────────────────


class TestPrintedCommands(unittest.TestCase):
    """A printed command is shipped code and is reviewed as code.

    An earlier draft of the move engine emitted ``vco codegraph analyze
    <path>`` in two remediations. That subcommand does not exist — the `vco`
    CLI has no ``codegraph`` verb — so both entries told the user to run
    something that exits 2. This class is the gate that would have caught it.
    """

    def _cli_verbs(self) -> set[str]:
        from vco_lib.cli.__main__ import _build_parser
        import argparse

        parser = _build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        return set()

    @staticmethod
    def _emitted_strings(module) -> list[str]:
        """String literals the module can EMIT — docstrings excluded.

        Scanning raw source would flag the prose that documents this very
        defect (``_codegraph_wrapper``'s docstring names the broken command it
        replaced). A scanner that cannot tell code from prose forces the
        comments to be reworded around it, which is how a gate ends up
        teaching people to hide things from it. Walking the AST and dropping
        docstrings answers the question that was actually asked: what can this
        module PRINT?
        """
        import ast

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ]

    def test_every_vco_verb_the_engine_prints_exists(self):
        import re

        from vco_lib import project_move

        verbs = self._cli_verbs()
        printed: set[str] = set()
        for literal in self._emitted_strings(project_move):
            printed.update(re.findall(r"\bvco ([a-z][a-z0-9-]*)", literal))
        unknown = {v for v in printed if v not in verbs}
        self.assertEqual(
            unknown,
            set(),
            f"the engine PRINTS `vco <verb>` for verbs the CLI does not "
            f"define, so following the remediation exits 2. "
            f"Known verbs: {sorted(verbs)}",
        )
        self.assertIn(
            "project", printed, "`vco project move --verify` must be emitted"
        )
        self.assertIn("project", verbs, "...and must be a real verb")

    def test_the_dismiss_command_matches_the_real_parser(self):
        """The exact argv the ledger tells the project's agent to run."""
        from vco_lib import project_move

        cmd = project_move._dismiss_command(Path("/w/new"), "some_cid")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "vco_lib.project_init",
                "dismiss-deferral",
                "--help",
            ],
            capture_output=True,
            text=True,
            env={
                **{k: v for k, v in __import__("os").environ.items()},
                "PYTHONPATH": str(REPO_ROOT),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--folder", "--condition-id"):
            self.assertIn(flag, cmd)
            self.assertIn(flag, proc.stdout, f"{flag} is not a real option")

    def test_the_codegraph_remediation_names_a_bundled_wrapper(self):
        from vco_lib import project_move

        cmd = project_move._codegraph_wrapper(Path("/w/new"))
        self.assertTrue(cmd.endswith(("code-graph-analyze", "code-graph-analyze.ps1")))
        shipped = REPO_ROOT / "templates" / "scripts" / Path(cmd).name
        self.assertTrue(
            shipped.is_file(),
            f"the remediation names {shipped.name}, which must be a shipped "
            f"template or the command cannot exist in a user project",
        )

    def test_the_codegraph_remediation_carries_project_IDENTITY(self):
        """`--project` selects the collection family.

        Deriving it from the destination folder's basename would walk the
        project's code into a DIFFERENT collection — the exact defect this
        package exists to avoid. So the emitter must read the project NAME,
        never the path.
        """
        from vco_lib import project_move

        source = Path(project_move.__file__).read_text(encoding="utf-8")
        self.assertIn("project_name = plan.project_name or plan.project_id", source)
        self.assertNotIn("--project '{dst.name}'", source)


class TestParityClaims(unittest.TestCase):
    """Every `PARITY:` / `Mirrors` comment this package adds is backed."""

    def test_kg_sync_siblings_probe_the_same_tiers_in_the_same_order(self):
        """Both wrappers claim `same tiers, same order`. This is that claim."""
        sh = KG_SYNC.read_text(encoding="utf-8")
        ps1 = KG_SYNC_PS1.read_text(encoding="utf-8-sig")

        # Scoped to the CANDIDATES-building region of each file. A whole-file
        # `index()` finds the first MENTION, which for `VCT_INSTALL_ROOT` is a
        # 2015-era header comment — the same prose-vs-code confusion this
        # package has now hit three times.
        def region(text: str, start: str, end: str) -> str:
            return text[text.index(start) : text.index(end)]

        sh_region = region(sh, "CANDIDATES=()", 'VENV_PATH=""')
        ps1_region = region(ps1, "$Candidates = @()", "$VenvPython = $null")

        def order(text: str, tokens: list[str]) -> list[int]:
            return [text.index(t) for t in tokens]

        sh_order = order(
            sh_region,
            ["VCT_VENV", "VCT_INSTALL_ROOT", "PROJECT_ENV_ROOT", "CLONE_ROOT"],
        )
        self.assertEqual(
            sh_order,
            sorted(sh_order),
            "bash tier order: explicit override, then install root, then the "
            "file-backed orchestrator root, then the gated clone",
        )

        ps1_order = order(
            ps1_region,
            [
                "env:VCT_VENV",
                "env:VCT_INSTALL_ROOT",
                "ProjectEnvRoot",
                "Test-VcoOrchestratorClone",
            ],
        )
        self.assertEqual(
            ps1_order, sorted(ps1_order), "PowerShell tier order must match"
        )

    def test_move_status_constants_mirror_the_sql_check(self):
        """`move_status` claims to mirror migration 044's CHECK."""
        import re

        rust = RUST_WRITER.read_text(encoding="utf-8")
        sql = (
            REPO_ROOT
            / "launcher/src-tauri/vct-launcher-core/src/db/migrations/"
            "044_project_moves.sql"
        ).read_text(encoding="utf-8")

        block = rust[rust.index("pub mod move_status {") :]
        block = block[: block.index("\n}")]
        rust_values = set(re.findall(r'&str = "([a-z]+)"', block))

        check = re.search(r"CHECK \(status IN \(([^)]*)\)\)", sql).group(1)
        sql_values = set(re.findall(r"'([a-z]+)'", check))

        self.assertEqual(
            rust_values,
            sql_values,
            "the Rust constants and the SQL CHECK have drifted; a status the "
            "code can write but the schema rejects fails at INSERT time in the "
            "field, not in CI",
        )


# ───────────────────────────────────────────────────────────────────────────
# git_exclude extraction
# ───────────────────────────────────────────────────────────────────────────


class TestGitExcludeExtraction(unittest.TestCase):
    def test_project_init_delegates_to_the_shared_home(self):
        from vco_lib import git_exclude, project_init

        self.assertIs(
            project_init._SAFE_ADD_VCO_EXCLUSIVE_TOPLEVEL,
            git_exclude.VCO_EXCLUSIVE_TOPLEVEL,
            "project_init must re-export the shared table, not hold a copy",
        )
        self.assertEqual(
            project_init._SAFE_ADD_SIDECAR_SUFFIX,
            git_exclude.SAFE_ADD_SIDECAR_SUFFIX,
        )

    def test_windows_separators_are_normalised_for_git_patterns(self):
        """git exclude patterns are POSIX-shaped on every OS; a `\\` there is
        git's ESCAPE character, so an un-normalised Windows path matches
        nothing at all."""
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            entries = git_exclude.exclude_entries_for_created_paths(
                [r"infrastructure\docker-compose.yml"], Path(td)
            )
            self.assertEqual(entries, ["/infrastructure/docker-compose.yml"])

    def test_vco_exclusive_namespaces_collapse_to_one_glob(self):
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            entries = git_exclude.exclude_entries_for_created_paths(
                [".claude/hooks/a.sh", ".claude/scripts/b", "CLAUDE.md"], Path(td)
            )
            self.assertEqual(entries, ["/.claude/", "/CLAUDE.md"])

    def test_user_ownable_directories_are_never_blanket_globbed(self):
        """A blanket `/knowledge/` would hide the USER's own same-named files,
        which is the opposite of what safe-add is for."""
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            entries = git_exclude.exclude_entries_for_created_paths(
                ["knowledge/TAG_HIERARCHY.md", ".vscode/settings.json"], Path(td)
            )
            self.assertNotIn("/knowledge/", entries)
            self.assertNotIn("/.vscode/", entries)
            self.assertEqual(
                entries,
                ["/knowledge/TAG_HIERARCHY.md", "/.vscode/settings.json"],
            )

    def test_append_is_idempotent(self):
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".git" / "info").mkdir(parents=True)
            first = git_exclude.append_git_info_exclude(folder, ("/.claude/",))
            self.assertEqual(first["action"], "appended")
            second = git_exclude.append_git_info_exclude(folder, ("/.claude/",))
            self.assertEqual(second["action"], "noop")

    def test_non_git_folder_is_a_no_op(self):
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            res = git_exclude.append_git_info_exclude(Path(td), ("/.claude/",))
            self.assertEqual(res["action"], "not_a_git_repo")
            self.assertEqual(res["added"], [])

    def test_git_file_worktree_layout_is_conservatively_skipped(self):
        from vco_lib import git_exclude

        with TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
            res = git_exclude.append_git_info_exclude(folder, ("/.claude/",))
            self.assertEqual(
                res["action"],
                "not_a_git_repo",
                "resolving a worktree's real gitdir is out of scope; guessing "
                "wrong would write into an unrelated repository",
            )


if __name__ == "__main__":
    unittest.main()
