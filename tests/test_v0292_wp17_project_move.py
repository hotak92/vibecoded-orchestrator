# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-17 (W3) — ``vco project move``: the file-side engine.

EVERY DESTRUCTIVE-CAPABLE STEP IS TESTED FROM BOTH SIDES. The pattern this
repo settled on is act + leave-alone, and for a move the leave-alone side is
the one that matters most: a move that copies correctly but also quietly
overwrote the destination's file, or deleted something at the source, is worse
than a move that refused. So the leave-alone assertions here are HASH-BASED,
not inspection-based — a full-tree hash of the source before and after, and a
byte hash of every destination file the engine was not supposed to touch.

Test names say which side they assert:
    ``..._act``          the operation does the thing.
    ``..._leaves_alone`` the operation provably does NOT touch its neighbour.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import path_bearing_keys as pbk  # noqa: E402
from vco_lib import project_move as pm  # noqa: E402

MIGRATIONS_DIR = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "migrations"
)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def apply_all_migrations(db_path: Path) -> sqlite3.Connection:
    """A real launcher.db: every shipped migration, applied in order.

    NOT the minimal ``tests/common/launcher_db_fixture`` schema. The DB sweep
    walks EVERY registered TEXT column, so a three-table fixture would make
    the sweep pass by having nothing to find — a test that green-lights
    itself. The migrations are the schema's only source of truth, so the test
    uses them.
    """
    conn = sqlite3.connect(str(db_path))
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def live_schema() -> dict[str, list[str]]:
    """``table -> [TEXT columns]`` after every migration."""
    conn = sqlite3.connect(":memory:")
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(sql_file.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        out[table] = [
            row[1]
            for row in conn.execute(f'PRAGMA table_info("{table}")')
            if (row[2] or "").upper().startswith("TEXT")
        ]
    conn.close()
    return out


def tree_hash(root: Path) -> dict[str, str]:
    """``relpath -> sha256`` for every file under ``root``.

    The leave-alone oracle. Comparing this dict before and after an operation
    catches a deletion, an addition and a modification with one assertion —
    and it cannot be fooled by looking at the two or three files the author
    happened to think of.
    """
    from vco_lib.hashing import sha256_file

    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = sha256_file(path)
    return out


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_manifest(folder: Path, files: dict[str, str], preserved: dict[str, str]
                  ) -> None:
    """Write a schema-v2 manifest recording ``files`` shipped-hashes."""
    payload = {
        "schema_version": 2,
        "files": {rel: {"sha256": sha, "source": rel} for rel, sha in files.items()},
        "preserved_files": {
            rel: {"shipped_sha256": sha, "reason": "preserve"}
            for rel, sha in preserved.items()
        },
    }
    write(folder / ".claude" / ".vco-manifest.json", json.dumps(payload, indent=2))


# ───────────────────────────────────────────────────────────────────────────
# Path comparison — tri-OS shape (R14)
# ───────────────────────────────────────────────────────────────────────────


class TestPathComparisonTriOs(unittest.TestCase):
    """Windows, macOS and Linux shapes, all exercised on ONE host.

    "Only verifiable on <one OS>" is a disallowed acceptance criterion, so the
    platform is an injected parameter and all three shapes are asserted here.
    """

    def test_windows_case_and_separator_are_equal_act(self):
        self.assertTrue(
            pm.paths_equal(r"C:\Proj", "c:/proj", platform="win32"),
            "Windows paths differing only in case and separator name the same "
            "directory; treating them as different would let a project 'move' "
            "onto itself",
        )

    def test_macos_case_insensitive_act(self):
        self.assertTrue(
            pm.paths_equal("/Users/x/Proj", "/users/x/proj", platform="darwin"),
            "macOS default filesystems are case-insensitive. os.path.normcase "
            "is the IDENTITY on darwin, so relying on it alone silently gives "
            "macOS Linux semantics",
        )

    def test_linux_case_sensitive_leaves_alone(self):
        self.assertFalse(
            pm.paths_equal("/home/x/Proj", "/home/x/proj", platform="linux"),
            "on Linux those ARE two different directories and the engine must "
            "not merge them",
        )

    def test_ancestry_is_componentwise_not_prefix_leaves_alone(self):
        self.assertFalse(
            pm.is_ancestor("/a/proj", "/a/project", platform="linux"),
            "'/a/proj' is not an ancestor of '/a/project' — a startswith test "
            "says it is, and that is the bug this asserts against",
        )

    def test_ancestry_real_descendant_act(self):
        self.assertTrue(pm.is_ancestor("/a/proj", "/a/proj/x/y", platform="linux"))

    def test_windows_ancestry_mixed_separators_act(self):
        self.assertTrue(
            pm.is_ancestor(r"C:\Proj", "c:/proj/sub/dir", platform="win32")
        )


class TestCheckPathOverlap(unittest.TestCase):
    ROWS = [
        {"id": "p1", "name": "One", "folder_path": "/w/one"},
        {"id": "p2", "name": "Two", "folder_path": "/w/two"},
        {"id": "p3", "name": "Nested", "folder_path": "/w/two/inner"},
    ]

    def test_same_path_act(self):
        hits = pm.check_path_overlap("/w/one", self.ROWS, platform="linux")
        self.assertEqual([h.relation for h in hits], ["same"])
        self.assertEqual(hits[0].project_id, "p1")

    def test_inside_another_project_act(self):
        hits = pm.check_path_overlap("/w/two/deep", self.ROWS, platform="linux")
        self.assertIn("inside", [h.relation for h in hits])

    def test_contains_another_project_act(self):
        hits = pm.check_path_overlap("/w", self.ROWS, platform="linux")
        self.assertTrue(all(h.relation == "contains" for h in hits))
        self.assertEqual(len(hits), 3)

    def test_own_row_is_not_an_overlap_leaves_alone(self):
        hits = pm.check_path_overlap(
            "/w/one", self.ROWS, exclude_project_id="p1", platform="linux"
        )
        self.assertEqual(hits, ())

    def test_unrelated_sibling_is_not_an_overlap_leaves_alone(self):
        self.assertEqual(
            pm.check_path_overlap("/w/three", self.ROWS, platform="linux"), ()
        )

    def test_prefix_sibling_is_not_an_overlap_leaves_alone(self):
        self.assertEqual(
            pm.check_path_overlap("/w/onething", self.ROWS, platform="linux"),
            (),
            "'/w/onething' merely shares a string prefix with '/w/one'",
        )


# ───────────────────────────────────────────────────────────────────────────
# Preflight refusals — each with its OWN reason
# ───────────────────────────────────────────────────────────────────────────


class TestPreflightRefusals(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.project = {
            "id": "p1",
            "name": "P",
            "slug": "p",
            "folder_path": str(self.src),
        }
        self.rows = [self.project]
        self.addCleanup(self.td.cleanup)

    def _refusal(self, dst, **kw):
        with self.assertRaises(pm.MoveRefused) as ctx:
            pm.plan_move(
                project=self.project, dst=dst, registered=self.rows, **kw
            )
        return ctx.exception.reason

    def test_relative_destination_refused(self):
        self.assertEqual(self._refusal("relative/path"), "dst_not_absolute")

    def test_destination_is_a_file_refused(self):
        f = write(self.root / "afile", "x")
        self.assertEqual(self._refusal(str(f)), "dst_is_file")

    def test_destination_parent_missing_refused(self):
        self.assertEqual(
            self._refusal(str(self.root / "no" / "such" / "dir")),
            "dst_parent_missing",
        )

    def test_destination_equals_source_refused(self):
        self.assertEqual(self._refusal(str(self.src)), "dst_equals_src")

    def test_destination_inside_source_refused(self):
        (self.src / "inner").mkdir()
        self.assertEqual(self._refusal(str(self.src / "inner")), "dst_inside_src")

    def test_destination_contains_source_refused(self):
        self.assertEqual(self._refusal(str(self.root)), "dst_contains_src")

    def test_destination_registered_to_another_project_refused(self):
        other = self.root / "other"
        other.mkdir()
        self.rows.append(
            {"id": "p2", "name": "Other", "slug": "o", "folder_path": str(other)}
        )
        self.assertEqual(
            self._refusal(str(other)), "dst_registered_to_another_project"
        )

    def test_destination_inside_another_project_refused(self):
        other = self.root / "other"
        (other / "sub").mkdir(parents=True)
        self.rows.append(
            {"id": "p2", "name": "Other", "slug": "o", "folder_path": str(other)}
        )
        self.assertEqual(
            self._refusal(str(other / "sub")), "dst_inside_registered_project"
        )

    def test_destination_contains_another_project_refused(self):
        outer = self.root / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        self.rows.append(
            {"id": "p2", "name": "Inner", "slug": "i", "folder_path": str(inner)}
        )
        self.assertEqual(
            self._refusal(str(outer)), "dst_contains_registered_project"
        )

    def test_non_empty_destination_refused_by_default(self):
        dst = self.root / "dst"
        write(dst / "user-file.txt", "mine")
        self.assertEqual(self._refusal(str(dst)), "dst_not_empty")

    def test_non_empty_destination_allowed_with_opt_in_act(self):
        dst = self.root / "dst"
        write(dst / "user-file.txt", "mine")
        plan = pm.plan_move(
            project=self.project,
            dst=str(dst),
            registered=self.rows,
            into_existing=True,
        )
        self.assertEqual(plan.dst, str(dst.resolve()))

    def test_empty_destination_needs_no_opt_in_leaves_alone(self):
        dst = self.root / "dst"
        dst.mkdir()
        plan = pm.plan_move(
            project=self.project, dst=str(dst), registered=self.rows
        )
        self.assertTrue(plan.dst_empty)

    def test_missing_source_refused_by_default(self):
        """The ALREADY-DAMAGED axis: the registered folder is gone."""
        gone = self.root / "gone"
        self.project["folder_path"] = str(gone)
        dst = self.root / "dst"
        dst.mkdir()
        self.assertEqual(self._refusal(str(dst)), "src_registered_path_missing")

    def test_missing_source_allowed_with_opt_in_act(self):
        gone = self.root / "gone"
        self.project["folder_path"] = str(gone)
        dst = self.root / "dst"
        dst.mkdir()
        plan = pm.plan_move(
            project=self.project,
            dst=str(dst),
            registered=self.rows,
            from_missing=True,
        )
        self.assertFalse(plan.src_exists)
        self.assertEqual(plan.to_copy, (), "nothing can be copied from a gone folder")
        self.assertTrue(
            any("does not exist" in w for w in plan.warnings),
            "the user must be told nothing was copied",
        )

    def test_every_refusal_reason_has_user_facing_text(self):
        """A refusal key with no sentence is a promise the UI cannot keep."""
        for key, text in pm.REFUSAL_REASONS.items():
            self.assertTrue(text.strip(), f"{key} has no explanation")
            self.assertGreater(
                len(text), 30, f"{key}'s explanation is too terse to act on"
            )


# ───────────────────────────────────────────────────────────────────────────
# Source classification
# ───────────────────────────────────────────────────────────────────────────


class TestClassifyMoveSources(unittest.TestCase):
    def setUp(self):
        from vco_lib.hashing import sha256_text

        self.td = TemporaryDirectory()
        self.src = Path(self.td.name) / "src"
        self.src.mkdir(parents=True)
        self.addCleanup(self.td.cleanup)

        clean_body = "shipped\n"
        write(self.src / ".claude" / "hooks" / "clean.sh", clean_body)
        write(self.src / ".claude" / "hooks" / "edited.sh", "USER EDITED\n")
        write(self.src / "CLAUDE.md", "kept by the user\n")
        write(self.src / "knowledge" / "concepts" / "n.md", "node\n")
        write(self.src / ".claude" / "context" / "CONTEXT_STATE.md", "state\n")
        write(self.src / ".claude" / "agents.disabled" / "off.md", "disabled agent\n")
        write(self.src / ".claude" / "skills.disabled" / "s" / "SKILL.md", "sk\n")
        write(self.src / "src" / "main.py", "user source\n")
        write(self.src / ".claude" / "logs" / "run.jsonl", "{}\n")
        write(self.src / ".env", "SECRET=1\n")

        make_manifest(
            self.src,
            files={
                ".claude/hooks/clean.sh": sha256_text(clean_body),
                ".claude/hooks/edited.sh": sha256_text("ORIGINAL\n"),
            },
            preserved={"CLAUDE.md": sha256_text("shipped CLAUDE\n")},
        )
        self.by_rel = {
            s.rel: s
            for s in pm.classify_move_sources(self.src, pm._read_manifest_at(self.src))
        }

    def test_unmodified_bundle_file_is_not_copied_leaves_alone(self):
        entry = self.by_rel[".claude/hooks/clean.sh"]
        self.assertEqual(entry.bucket, "bundle-clean")
        self.assertFalse(
            entry.copied,
            "an unmodified bundle file must be RE-MATERIALIZED at the "
            "destination so its transforms are substituted for the new root; "
            "copying it would carry the old root's substitutions across",
        )

    def test_user_modified_bundle_file_is_copied_act(self):
        entry = self.by_rel[".claude/hooks/edited.sh"]
        self.assertEqual(entry.bucket, "user-modified")
        self.assertTrue(entry.copied)

    def test_preserved_files_entry_is_copied_act(self):
        self.assertEqual(self.by_rel["CLAUDE.md"].bucket, "user-modified")

    def test_knowledge_is_always_copied_act(self):
        self.assertEqual(
            self.by_rel["knowledge/concepts/n.md"].bucket, "user-adjacent"
        )

    def test_disabled_agent_dir_is_copied_act(self):
        """M-1: the whole reason the destination's skip-disabled guard fires."""
        self.assertIn(".claude/agents.disabled/off.md", self.by_rel)
        self.assertIn(".claude/skills.disabled/s/SKILL.md", self.by_rel)

    def test_user_source_is_out_of_scope_leaves_alone(self):
        self.assertNotIn("src/main.py", self.by_rel)

    def test_logs_are_not_copied_leaves_alone(self):
        self.assertNotIn(
            ".claude/logs/run.jsonl",
            self.by_rel,
            "run logs describe what happened at the OLD root — the same "
            "reasoning that keeps kg_syncs.log_tail out of the DB rewrite",
        )

    def test_dotenv_is_not_copied_leaves_alone(self):
        self.assertNotIn(
            ".env",
            self.by_rel,
            "copying credentials to a new location is an explicit act, never "
            "a side effect of a move",
        )

    def test_manifest_itself_is_not_copied_leaves_alone(self):
        self.assertNotIn(".claude/.vco-manifest.json", self.by_rel)

    def test_every_not_copied_spec_states_a_reason(self):
        for spec, reason in pm.NOT_COPIED_SPECS.items():
            self.assertGreater(
                len(reason), 40, f"{spec} is excluded without a stated reason"
            )


# ───────────────────────────────────────────────────────────────────────────
# Conflicts + the zero-overwrite / zero-deletion invariants
# ───────────────────────────────────────────────────────────────────────────


class TestConflictsAndInvariants(unittest.TestCase):
    def setUp(self):
        from vco_lib.hashing import sha256_text

        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.src = self.root / "src"
        self.dst = self.root / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        self.addCleanup(self.td.cleanup)

        write(self.src / "CLAUDE.md", "SOURCE VERSION\n")
        write(self.src / "knowledge" / "n.md", "same bytes\n")
        write(self.src / "knowledge" / "only-here.md", "new\n")
        make_manifest(
            self.src,
            files={"CLAUDE.md": sha256_text("shipped\n")},
            preserved={},
        )

        # Destination already holds a DIFFERENT CLAUDE.md and an IDENTICAL node.
        write(self.dst / "CLAUDE.md", "DESTINATION VERSION\n")
        write(self.dst / "knowledge" / "n.md", "same bytes\n")
        write(self.dst / "user-untouchable.txt", "not VCO's\n")

        self.project = {
            "id": "p1",
            "name": "P",
            "slug": "p",
            "folder_path": str(self.src),
        }
        self.plan = pm.plan_move(
            project=self.project,
            dst=str(self.dst),
            registered=[self.project],
            into_existing=True,
        )

    def test_identical_file_classified_identical(self):
        kinds = {c.rel: c.kind for c in self.plan.conflicts}
        self.assertEqual(kinds["knowledge/n.md"], "identical")

    def test_divergent_file_classified_divergent(self):
        kinds = {c.rel: c.kind for c in self.plan.conflicts}
        self.assertEqual(kinds["CLAUDE.md"], "divergent")

    def test_execute_leaves_source_byte_identical(self):
        """The zero-deletion invariant, ASSERTED rather than assumed."""
        before = tree_hash(self.src)
        pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        after = tree_hash(self.src)
        # The sentinel is the ONE file the engine writes under the source, and
        # it is removed when the move completes.
        after.pop(str(pm.SENTINEL_REL), None)
        self.assertEqual(
            before,
            after,
            "the move must never delete or modify anything at the source",
        )

    def test_execute_leaves_divergent_destination_file_untouched_leaves_alone(self):
        before = (self.dst / "CLAUDE.md").read_bytes()
        pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        self.assertEqual(
            (self.dst / "CLAUDE.md").read_bytes(),
            before,
            "the destination's own version is what the user has been working "
            "in; it is never overwritten",
        )

    def test_execute_writes_a_sibling_for_the_divergent_file_act(self):
        result = pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        sibling = self.dst / ("CLAUDE.md" + pm.MOVED_SIBLING_SUFFIX)
        self.assertTrue(sibling.is_file())
        self.assertEqual(sibling.read_text(), "SOURCE VERSION\n")
        self.assertEqual([s["rel"] for s in result.siblings], ["CLAUDE.md"])

    def test_execute_skips_the_identical_file_leaves_alone(self):
        result = pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        self.assertIn("knowledge/n.md", result.skipped_identical)
        self.assertFalse(
            (self.dst / ("knowledge/n.md" + pm.MOVED_SIBLING_SUFFIX)).exists(),
            "identical bytes need no sibling and no ledger entry",
        )

    def test_execute_copies_the_new_file_act(self):
        result = pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        self.assertIn("knowledge/only-here.md", result.copied)
        self.assertEqual(
            (self.dst / "knowledge" / "only-here.md").read_text(), "new\n"
        )

    def test_execute_leaves_unrelated_destination_files_untouched_leaves_alone(self):
        before = tree_hash(self.dst)
        pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        after = tree_hash(self.dst)
        for rel, digest in before.items():
            self.assertEqual(
                after.get(rel),
                digest,
                f"{rel} existed at the destination and was modified — the "
                f"engine performs ZERO overwrites",
            )

    def test_sibling_collision_never_overwrites_a_previous_sibling(self):
        existing = write(
            self.dst / ("CLAUDE.md" + pm.MOVED_SIBLING_SUFFIX), "EARLIER SIBLING\n"
        )
        pm.execute_pre_flip(self.plan, move_id="m1", run_bundle=False)
        self.assertEqual(
            existing.read_text(),
            "EARLIER SIBLING\n",
            "a second move must not clobber the first move's sibling — the "
            "sibling exists BECAUSE overwriting is forbidden",
        )
        stamped = [
            p
            for p in self.dst.iterdir()
            if p.name.startswith("CLAUDE.md" + pm.MOVED_SIBLING_SUFFIX + ".")
        ]
        self.assertEqual(len(stamped), 1, "a timestamped sibling was written")


# ───────────────────────────────────────────────────────────────────────────
# Env construction (B-as-run #3)
# ───────────────────────────────────────────────────────────────────────────


class TestChildEnvConstruction(unittest.TestCase):
    def test_operator_project_keys_are_scrubbed_act(self):
        base = {
            "KG_COLLECTION": "OperatorsOwnProject_KnowledgeGraph",
            "KG_BASE_DIR": "/operators/own/project",
            "PROJECT_NAME": "OperatorProject",
        }
        env = pm.build_child_env({}, base=base)
        for key in base:
            self.assertNotIn(
                key,
                env,
                f"{key} belongs to whichever project the OPERATOR's shell was "
                f"configured for; inheriting it is how the field move's first "
                f"sync ran against a foreign collection",
            )

    def test_unrelated_variables_pass_through_leaves_alone(self):
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "MY_OWN_VAR": "keep me",
            "LANG": "en_US.UTF-8",
        }
        env = pm.build_child_env({}, base=base)
        for key, value in base.items():
            self.assertEqual(
                env.get(key),
                value,
                "the scrub is a named, closed set — a child that lost PATH "
                "would fail far more confusingly than one with a stale "
                "KG_COLLECTION",
            )

    def test_target_values_win_over_the_ambient_env_act(self):
        env = pm.build_child_env(
            {"KG_COLLECTION": "TargetProject_KnowledgeGraph"},
            base={"KG_COLLECTION": "Operator_KnowledgeGraph"},
        )
        self.assertEqual(env["KG_COLLECTION"], "TargetProject_KnowledgeGraph")

    def test_scrub_set_covers_every_canonical_projection_key(self):
        """A new canonical key must not silently become inheritable."""
        from vco_lib.config_projection import list_canonical_keys

        missing = list_canonical_keys() - pm.projection_owned_keys()
        self.assertEqual(
            missing,
            set(),
            "these projected keys would leak from the operator's shell into "
            "the move's children",
        )

    def test_target_env_falls_back_to_the_destination_env_file(self):
        with TemporaryDirectory() as td:
            dst = Path(td)
            write(dst / ".claude" / "env", 'export KG_COLLECTION="FromFile"\n')
            resolved = pm.resolve_target_env("", dst)
            self.assertEqual(resolved.get("KG_COLLECTION"), "FromFile")


# ───────────────────────────────────────────────────────────────────────────
# The path-bearing registry
# ───────────────────────────────────────────────────────────────────────────


class TestPathBearingRegistry(unittest.TestCase):
    def test_every_text_column_is_classified(self):
        """A new TEXT column cannot ship without a move policy.

        The field defect was a column nobody thought about. This is the gate
        that makes "nobody thought about it" a CI failure instead of a bug
        report after someone's project breaks.
        """
        missing = pbk.unclassified_columns(live_schema())
        self.assertEqual(
            missing,
            (),
            "these launcher.db TEXT columns have no declared move policy. Add "
            "each to PATH_BEARING_DB_COLUMNS in vco_lib/path_bearing_keys.py "
            "with the policy AND the reason.",
        )

    def test_no_registry_row_names_a_column_that_no_longer_exists(self):
        stale = pbk.stale_registry_entries(live_schema())
        self.assertEqual(
            stale,
            (),
            "a rule that can never fire is a promise; it also hides a typo'd "
            "column name, which would silently skip that column forever",
        )

    def test_exactly_one_column_carries_the_flip_policy(self):
        flips = pbk.columns_with_policy(pbk.POLICY_FLIP)
        self.assertEqual([c.qualified for c in flips], ["projects.folder_path"])

    def test_identity_columns_are_never_rewritten(self):
        """THE invariant: a move must not change WHO the project is."""
        for qualified in (
            "project_kg_bindings.collection_name",
            "project_codegraph_bindings.collection_prefix",
            "projects.name",
            "projects.slug",
        ):
            table, column = qualified.split(".")
            policy = pbk.policy_for(table, column)
            self.assertEqual(
                policy,
                pbk.POLICY_NOT_PATH_BEARING,
                f"{qualified} is IDENTITY. Re-deriving it from the new folder "
                f"basename is the defect this feature exists to avoid "
                f"reproducing: the project keeps its data and loses the "
                f"ability to find it.",
            )

    def test_user_owned_paths_are_never_rewritten(self):
        for qualified in (
            "project_codegraph_extra_paths.path",
            "project_secret_refs.file_path",
            "project_permissions.value",
        ):
            table, column = qualified.split(".")
            self.assertEqual(
                pbk.policy_for(table, column),
                pbk.POLICY_USER_OWNED_FLAG,
                f"{qualified} is the USER's choice; rewriting it decides for "
                f"them",
            )

    def test_historical_columns_are_never_rewritten(self):
        for qualified in (
            "kg_syncs.log_tail",
            "kg_summaries.log_tail",
            "rl_events.payload_json",
            "audit_log.detail",
        ):
            table, column = qualified.split(".")
            self.assertEqual(
                pbk.policy_for(table, column),
                pbk.POLICY_HISTORICAL,
                f"{qualified} was TRUE AT EVENT TIME; rewriting it falsifies a "
                f"record",
            )

    def test_the_two_field_broken_columns_have_a_fixer(self):
        for qualified in ("project_agents.file_path", "project_skills.file_path"):
            table, column = qualified.split(".")
            self.assertEqual(
                pbk.policy_for(table, column),
                pbk.POLICY_TARGETED_UPDATE,
                f"{qualified} is the column that survived the field move "
                f"pointing at the old root (44 + 53 rows)",
            )

    def test_every_policy_value_is_legal(self):
        for entry in pbk.PATH_BEARING_DB_COLUMNS:
            self.assertIn(entry.policy, pbk.POLICIES, entry.qualified)

    def test_env_drift_gate_flags_an_unregistered_folder_bearing_key(self):
        rendered = {
            "KG_BASE_DIR": "/w/proj",
            "SOME_NEW_KEY": "/w/proj/sub",
            "KG_COLLECTION": "Proj_KnowledgeGraph",
        }
        self.assertEqual(
            pbk.unregistered_path_bearing_env_keys(rendered, "/w/proj"),
            ("SOME_NEW_KEY",),
        )

    def test_env_drift_gate_is_quiet_on_a_clean_projection_leaves_alone(self):
        rendered = {"KG_BASE_DIR": "/w/proj", "KG_COLLECTION": "Proj_KnowledgeGraph"}
        self.assertEqual(
            pbk.unregistered_path_bearing_env_keys(rendered, "/w/proj"), ()
        )


# ───────────────────────────────────────────────────────────────────────────
# Stale-reference scans
# ───────────────────────────────────────────────────────────────────────────


class TestStaleScans(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.addCleanup(self.td.cleanup)

    def test_file_scan_finds_a_seeded_reference_act(self):
        write(self.root / ".claude" / "env", 'KG_BASE_DIR="/old/root"\n')
        self.assertEqual(scan := pm.scan_files_for_path(self.root, "/old/root"), (".claude/env",))
        del scan

    def test_file_scan_is_quiet_on_a_clean_tree_leaves_alone(self):
        write(self.root / ".claude" / "env", 'KG_BASE_DIR="/new/root"\n')
        self.assertEqual(pm.scan_files_for_path(self.root, "/old/root"), ())

    def test_file_scan_finds_a_json_escaped_windows_path_act(self):
        write(
            self.root / ".claude" / "settings.json",
            '{"env": {"KG_BASE_DIR": "C:\\\\old\\\\root"}}',
        )
        self.assertEqual(
            pm.scan_files_for_path(self.root, "C:\\old\\root"),
            (".claude/settings.json",),
            "a JSON surface escapes backslashes; searching only the raw shape "
            "would make the verify pass a false negative",
        )

    def test_file_scan_finds_the_deferral_ledger_act(self):
        """M-4: an S-rooted `To apply` command poisons the project's session."""
        write(
            self.root / ".claude" / "context" / "UPDATE_DEFERRED.md",
            "run: source '/old/root/.claude/env'\n",
        )
        self.assertEqual(
            pm.scan_files_for_path(self.root, "/old/root"),
            (".claude/context/UPDATE_DEFERRED.md",),
        )

    def test_db_sweep_finds_a_seeded_reference_act(self):
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P','/old/root','base','p',0,0)"
        )
        conn.execute(
            "INSERT INTO project_agents (project_id, agent_name, source, "
            "enabled, file_path, installed_at, updated_at) "
            "VALUES ('p1','a','bundled',1,'/old/root/.claude/agents/a.md',0,0)"
        )
        conn.commit()
        conn.close()
        hits = {h.qualified: h for h in pm.sweep_db_for_path("/old/root", db_path=db)}
        self.assertIn("project_agents.file_path", hits)
        self.assertEqual(hits["project_agents.file_path"].rows, 1)
        self.assertEqual(
            hits["project_agents.file_path"].policy, pbk.POLICY_TARGETED_UPDATE
        )

    def test_db_sweep_is_quiet_after_the_repoint_leaves_alone(self):
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P','/new/root','base','p',0,0)"
        )
        conn.execute(
            "INSERT INTO project_agents (project_id, agent_name, source, "
            "enabled, file_path, installed_at, updated_at) "
            "VALUES ('p1','a','bundled',1,'/new/root/.claude/agents/a.md',0,0)"
        )
        conn.commit()
        conn.close()
        self.assertEqual(pm.sweep_db_for_path("/old/root", db_path=db), ())

    def test_db_sweep_does_not_use_like_wildcards(self):
        """`LIKE` treats `_` as a wildcard and project folders are full of them."""
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P','/w/VCOxdev','base','p',0,0)"
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            pm.sweep_db_for_path("/w/VCO_dev", db_path=db),
            (),
            "a LIKE '%/w/VCO_dev%' sweep matches '/w/VCOxdev' and reports a "
            "row that does not carry the path at all",
        )

    def test_db_sweep_reports_a_historical_hit_as_expected(self):
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P','/new/root','base','p',0,0)"
        )
        conn.execute(
            "INSERT INTO kg_syncs (project_id, status, log_tail) "
            "VALUES ('p1','success','walked /old/root ...')"
        )
        conn.commit()
        conn.close()
        hits = {h.qualified: h for h in pm.sweep_db_for_path("/old/root", db_path=db)}
        self.assertEqual(hits["kg_syncs.log_tail"].policy, pbk.POLICY_HISTORICAL)
        self.assertNotIn(
            pbk.POLICY_HISTORICAL,
            pbk.ACTIONABLE_POLICIES,
            "a historical hit is expected, never a warning",
        )


# ───────────────────────────────────────────────────────────────────────────
# Harness per-path state (M-5)
# ───────────────────────────────────────────────────────────────────────────


class TestHarnessState(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.src = self.root / "old"
        self.dst = self.root / "new"
        self.src.mkdir()
        self.dst.mkdir()
        self.addCleanup(self.td.cleanup)

    def test_reports_the_old_directory_when_it_exists_act(self):
        slug = pm.harness_slug(self.src)
        (self.home / ".claude" / "projects" / slug / "memory").mkdir(parents=True)
        report = pm.harness_state_report(self.src, self.dst, home=self.home)
        self.assertTrue(report["old_present"])
        self.assertIn("cp -rn", report["memory_copy_command"])
        self.assertNotIn(
            "mv ",
            report["memory_copy_command"],
            "the command must COPY — the zero-data-loss rule applies to the "
            "user's memory as much as to their KG",
        )

    def test_reports_nothing_when_the_project_never_hosted_a_session_leaves_alone(self):
        report = pm.harness_state_report(self.src, self.dst, home=self.home)
        self.assertFalse(
            report["old_present"],
            "a project that never hosted a session must get no entry at all",
        )

    def test_slug_rule_comes_from_its_one_home(self):
        from vco_lib.project_config import claude_session_dir_for

        self.assertEqual(
            pm.harness_slug("/home/x/VCO_dev"),
            claude_session_dir_for(Path("/home/x/VCO_dev")).name,
            "re-deriving the slug here would be a second home for a rule that "
            "has already drifted once",
        )


# ───────────────────────────────────────────────────────────────────────────
# verify — the paired-resolution site
# ───────────────────────────────────────────────────────────────────────────


class TestVerifyMove(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.dst = self.root / "new"
        self.dst.mkdir()
        self.addCleanup(self.td.cleanup)

    def test_no_previous_path_is_a_no_op(self):
        report = pm.verify_move(self.dst)
        self.assertEqual(report["resolved"], [])

    def test_clean_tree_resolves_the_stale_path_entry_act(self):
        write(self.dst / ".claude" / "env", 'KG_BASE_DIR="' + str(self.dst) + '"\n')
        report = pm.verify_move(
            self.dst, old_path="/old/root", db_path=self.root / "absent.db"
        )
        self.assertIn(pm.CID_STALE_PATH_REFERENCE, report["resolved"])

    def test_dirty_tree_does_not_resolve_the_stale_path_entry_leaves_alone(self):
        write(self.dst / ".claude" / "env", 'KG_BASE_DIR="/old/root"\n')
        report = pm.verify_move(
            self.dst, old_path="/old/root", db_path=self.root / "absent.db"
        )
        self.assertNotIn(pm.CID_STALE_PATH_REFERENCE, report["resolved"])
        self.assertEqual(report["stale_file_hits"], [".claude/env"])

    def test_retained_record_clears_only_once_the_old_folder_is_gone(self):
        old = self.root / "old"
        old.mkdir()
        report = pm.verify_move(
            self.dst, old_path=str(old), db_path=self.root / "absent.db"
        )
        self.assertNotIn(pm.CID_OLD_FOLDER_RETAINED, report["resolved"])
        old.rmdir()
        report = pm.verify_move(
            self.dst, old_path=str(old), db_path=self.root / "absent.db"
        )
        self.assertIn(pm.CID_OLD_FOLDER_RETAINED, report["resolved"])

    def test_codegraph_entry_is_not_cleared_on_an_unknown_queue(self):
        """Tri-state: `None` must not collapse into "nothing outstanding"."""
        report = pm.verify_move(
            self.dst, old_path="/old", project_id="p1", db_path=self.root / "absent.db"
        )
        self.assertIsNone(report["codegraph_build_outstanding"])
        self.assertNotIn(
            pm.CID_CODEGRAPH_REANALYZE,
            report["resolved"],
            "clearing a deferral because the queue could not be READ is how "
            "owed work disappears without running",
        )

    def test_codegraph_entry_clears_when_the_queue_is_empty_act(self):
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P',?,'base','p',0,0)",
            (str(self.dst),),
        )
        conn.execute(
            "INSERT INTO code_graph_builds (project_id, status) "
            "VALUES ('p1','success')"
        )
        conn.commit()
        conn.close()
        self.assertIs(pm.pending_codegraph_build("p1", db_path=db), False)

    def test_codegraph_entry_stays_while_a_build_is_pending_leaves_alone(self):
        db = self.root / "launcher.db"
        conn = apply_all_migrations(db)
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, "
            "created_at, updated_at) VALUES ('p1','P',?,'base','p',0,0)",
            (str(self.dst),),
        )
        conn.execute(
            "INSERT INTO code_graph_builds (project_id, status) "
            "VALUES ('p1','pending')"
        )
        conn.commit()
        conn.close()
        self.assertIs(pm.pending_codegraph_build("p1", db_path=db), True)


# ───────────────────────────────────────────────────────────────────────────
# Deferral wiring
# ───────────────────────────────────────────────────────────────────────────


class TestDeferralWiring(unittest.TestCase):
    def test_every_emitted_condition_is_registered(self):
        """The emit sites and the registry land together or CI is red."""
        from vco_lib import deferral_registry as dr

        for cid in (
            pm.CID_CONFLICT_PREFIX + "example_file",
            pm.CID_STALE_PATH_REFERENCE,
            pm.CID_STALE_DB_PATH,
            pm.CID_EXTRA_CODEGRAPH_PREFIX + "example_path",
            pm.CID_CODEGRAPH_REANALYZE,
            pm.CID_OLD_FOLDER_RETAINED,
            pm.CID_HARNESS_STATE_REVIEW,
        ):
            self.assertTrue(
                dr.matches_registered_pattern(cid), f"{cid} is unregistered"
            )

    def test_conflict_slug_is_condition_id_safe(self):
        slug = pm._sanitize_rel(".claude/hooks/pre-tool-use.sh")
        self.assertTrue(
            all(ch.isalnum() or ch == "_" for ch in slug), f"unsafe slug: {slug}"
        )

    def test_dismiss_command_is_rooted_at_the_new_folder(self):
        """M-4: an old-rooted command dismisses in the ledger nobody reads."""
        cmd = pm._dismiss_command(Path("/new/root"), "some_cid")
        self.assertIn("/new/root", cmd)
        self.assertIn("dismiss-deferral", cmd)


# ───────────────────────────────────────────────────────────────────────────
# Sentinel
# ───────────────────────────────────────────────────────────────────────────


class TestSentinel(unittest.TestCase):
    def test_written_advanced_and_cleared(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            self.assertIsNone(pm.read_sentinel(folder))
            pm.write_sentinel(folder, {"phase": "copy", "src": "/a", "dst": "/b"})
            self.assertEqual(pm.read_sentinel(folder)["phase"], "copy")
            pm.write_sentinel(folder, {"phase": "bundle", "src": "/a", "dst": "/b"})
            self.assertEqual(pm.read_sentinel(folder)["phase"], "bundle")
            pm.clear_sentinel(folder)
            self.assertIsNone(pm.read_sentinel(folder))

    def test_execute_writes_the_sentinel_under_both_folders(self):
        """A move whose destination was never opened still leaves a trace at
        the source, and vice versa — whichever folder the user opens next."""
        with TemporaryDirectory() as td:
            root = Path(td)
            src, dst = root / "s", root / "d"
            src.mkdir()
            dst.mkdir()
            project = {"id": "p", "name": "P", "slug": "p", "folder_path": str(src)}
            plan = pm.plan_move(project=project, dst=str(dst), registered=[project])
            pm.execute_pre_flip(plan, move_id="m1", run_bundle=False)
            self.assertIsNotNone(pm.read_sentinel(src))
            self.assertIsNotNone(pm.read_sentinel(dst))


if __name__ == "__main__":
    unittest.main()
