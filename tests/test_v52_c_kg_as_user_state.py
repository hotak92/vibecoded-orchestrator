# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-C (v0.2.52) — KG nodes as user-curated state.

The orchestrator's curated KG node set lives under
``templates/knowledge/`` and is materialized into ``<project>/knowledge/``
by ``vco_lib.project_init._enumerate_bundle_files``. The
manifest-driven hash compare in ``install_project_bundle`` (V47-A
pattern) preserves user customizations across bundle updates — same
behavior as agents / skills / hooks.

This file pins five contracts:

1. **Fresh-install materialization** — every file under
   ``templates/knowledge/`` reaches ``<project>/knowledge/`` with the
   correct relative path. Nested subdirectories preserved.

2. **Update flow preserves user-modified nodes** — when a user has
   edited a previously-shipped node, a bundle update via
   ``install_project_bundle(update_mode=True)`` PRESERVES the user's
   bytes on disk (action: ``preserve``) and emits the
   ``bundle_user_modified_preserved`` deferral so Claude Code on next
   session knows about the conflict.

3. **Update flow overwrites untouched shipped nodes** — when the
   orchestrator's source bytes change but the user never edited the
   file (installed_hash == manifest's prior shipped hash), update
   OVERWRITES with the new version (action: ``overwrite``).

4. **User-authored nodes survive unconditionally** — nodes in
   ``<project>/knowledge/`` that the orchestrator never shipped are
   NOT in ``manifest["files"]`` and are never visited by the install
   bundle machinery. They survive every bundle install / update run
   even when the orchestrator's ``templates/knowledge/`` is empty.

5. **``knowledge`` is out of the install whitelist** — the legacy
   `apply_conflict_strategy` whitelist-copy path NEVER touches the
   user's ``knowledge/`` directory. This is the structural fix that
   prevents the v0.2.51 modify-vs-delete merge conflict.

The fixtures mirror ``tests/test_install_bundle.py::_make_fake_orchestrator``
but extend it with a ``templates/knowledge/`` subtree.
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib import project_init  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fake_orchestrator_with_knowledge(root: Path) -> None:
    """Build a minimal fake orchestrator tree with a
    ``templates/knowledge/`` subtree. Mirrors
    `tests/test_install_bundle.py::_make_fake_orchestrator` but trims
    the unrelated categories (we only exercise the knowledge path here)
    and adds a small KG template tree:

        <root>/vct-module.json
        <root>/templates/knowledge/TAG_HIERARCHY.md
        <root>/templates/knowledge/VOCABULARY.md
        <root>/templates/knowledge/.node_formats.json
        <root>/templates/knowledge/concepts/foo.md
        <root>/templates/knowledge/concepts/bar.md
        <root>/templates/knowledge/models/qwen.md
        <root>/templates/knowledge/tools/weaviate.md
    """
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")

    kg = root / "templates" / "knowledge"
    kg.mkdir(parents=True)
    (kg / "TAG_HIERARCHY.md").write_text(
        "# Tag hierarchy\nshipped-v1\n", encoding="utf-8",
    )
    (kg / "VOCABULARY.md").write_text(
        "# Vocabulary\nshipped-v1\n", encoding="utf-8",
    )
    (kg / ".node_formats.json").write_text(
        '{"shipped":"v1"}\n', encoding="utf-8",
    )

    concepts = kg / "concepts"
    concepts.mkdir()
    (concepts / "foo.md").write_text(
        "# Foo concept\nshipped-v1\n", encoding="utf-8",
    )
    (concepts / "bar.md").write_text(
        "# Bar concept\nshipped-v1\n", encoding="utf-8",
    )

    models = kg / "models"
    models.mkdir()
    (models / "qwen.md").write_text(
        "# Qwen model\nshipped-v1\n", encoding="utf-8",
    )

    tools = kg / "tools"
    tools.mkdir()
    (tools / "weaviate.md").write_text(
        "# Weaviate\nshipped-v1\n", encoding="utf-8",
    )

    # The bundle materializer also needs the other template dirs to
    # exist (settings.json templates etc.) for the full install_path
    # to succeed. Keep them minimal — we don't assert on them here.
    (root / "templates" / "settings.json.linux.template").write_text(
        '{"hooks":{}}', encoding="utf-8",
    )
    (root / "templates" / "settings.json.windows.template").write_text(
        '{"hooks":{}}', encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Contract 5: `knowledge` out of the install whitelist
# ---------------------------------------------------------------------------


class KnowledgeOutOfWhitelistTests(unittest.TestCase):
    """`knowledge` must NOT be in `ORCHESTRATOR_MANAGED_PATHS`.

    Mirrors the PR-31 `test_install_no_claude_md_copy.py` shape but for
    the V52-C `knowledge` removal. The legacy whitelist-copy path
    (`apply_conflict_strategy`) iterates this constant; with
    `knowledge` out of it, the user's `knowledge/` directory is
    never visited by that path. This is the structural fix that
    prevents the v0.2.51 modify-vs-delete merge conflict at root.
    """

    def test_knowledge_not_in_orchestrator_managed_paths(self):
        self.assertNotIn(
            "knowledge",
            install.ORCHESTRATOR_MANAGED_PATHS,
            "`knowledge` must NOT appear in ORCHESTRATOR_MANAGED_PATHS. "
            "Shipped KG nodes are bundle-materialized from "
            "`templates/knowledge/`, never copied through the whitelist. "
            "See V52-C / v0.2.52.",
        )

    def test_apply_conflict_strategy_does_not_copy_source_knowledge(self):
        """End-to-end: source has `knowledge/note.md`, target has
        `knowledge/note.md` with user content. After
        `apply_conflict_strategy(..., overwrite_all)` the user's bytes
        survive — proves `knowledge/` is not in the iteration set."""
        tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-whitelist-"))
        try:
            source = tmp / "src"
            source.mkdir()
            (source / "vct-module.json").write_text("{}")
            (source / "knowledge").mkdir()
            (source / "knowledge" / "shipped.md").write_text(
                "SOURCE BYTES\n",
            )

            target = tmp / "tgt"
            target.mkdir()
            (target / "knowledge").mkdir()
            (target / "knowledge" / "shipped.md").write_text(
                "USER BYTES\n",
            )
            # User-authored node never in source — must also survive.
            (target / "knowledge" / "user-only.md").write_text(
                "USER AUTHORED\n",
            )

            install.apply_conflict_strategy(
                source, target, "overwrite_all", [],
            )
            # User bytes survive even with OverwriteAll because
            # `knowledge/` is OUT of the allowlist (the iteration
            # skips it entirely).
            self.assertEqual(
                (target / "knowledge" / "shipped.md").read_text(),
                "USER BYTES\n",
            )
            self.assertEqual(
                (target / "knowledge" / "user-only.md").read_text(),
                "USER AUTHORED\n",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Contract 1: Fresh-install materialization
# ---------------------------------------------------------------------------


class FreshInstallMaterializationTests(unittest.TestCase):
    """`install_project_bundle` materializes every file under
    `templates/knowledge/` into `<project>/knowledge/` with the
    correct relative path. Nested subdirectories preserved."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-fresh-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_shipped_files_materialized(self):
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        # Every file we put under templates/knowledge/ must appear
        # under <project>/knowledge/ in the "create" action bucket.
        expected_rels = [
            "knowledge/TAG_HIERARCHY.md",
            "knowledge/VOCABULARY.md",
            "knowledge/.node_formats.json",
            "knowledge/concepts/foo.md",
            "knowledge/concepts/bar.md",
            "knowledge/models/qwen.md",
            "knowledge/tools/weaviate.md",
        ]
        for rel in expected_rels:
            # On Linux the dest_rel uses forward slashes from PosixPath;
            # we keep the assertion in `str(Path(...))` shape for
            # cross-platform stability.
            dest_rel = str(Path(rel))
            self.assertIn(
                dest_rel, result["actions"]["create"],
                f"{rel} missing from create actions: "
                f"{result['actions']['create']}",
            )
            # File must actually exist on disk with shipped bytes.
            on_disk = self.proj / Path(rel)
            self.assertTrue(on_disk.exists(),
                            f"{rel} not materialized on disk")
            # Markdown nodes embed `shipped-v1` as a literal string
            # for content provenance; the `.node_formats.json` metadata
            # file uses JSON shape `{"shipped":"v1"}`. Both forms imply
            # the same provenance — substring match `"v1"` covers both.
            self.assertIn(
                "v1",
                on_disk.read_text(encoding="utf-8"),
            )

    def test_nested_subdirectories_preserved(self):
        """`templates/knowledge/concepts/foo.md` must land at
        `<project>/knowledge/concepts/foo.md`, not flattened."""
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        nested = self.proj / "knowledge" / "concepts" / "foo.md"
        self.assertTrue(nested.exists(),
                        "Nested subdirectory structure lost during materialization")
        # And not at the flattened path.
        self.assertFalse((self.proj / "knowledge" / "foo.md").exists())

    def test_non_markdown_metadata_files_copied(self):
        """`.node_formats.json` (and any other non-.md files) must be
        byte-copied. The materializer is recursive over the whole
        `templates/knowledge/` tree, not filtered by extension."""
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        metadata = self.proj / "knowledge" / ".node_formats.json"
        self.assertTrue(metadata.exists())
        # Bytes byte-identical to source.
        src = self.orch / "templates" / "knowledge" / ".node_formats.json"
        self.assertEqual(
            metadata.read_bytes(),
            src.read_bytes(),
        )

    def test_manifest_tracks_knowledge_files(self):
        """Every shipped KG file lands in `.vco-manifest.json["files"]`
        with a SHA256. Without this entry, the update-mode hash compare
        can't tell whether the user edited the file (case b vs case c
        in `_plan_bundle_action`)."""
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        manifest_path = self.proj / ".claude" / ".vco-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Sample a few entries.
        for rel in (
            "knowledge/TAG_HIERARCHY.md",
            "knowledge/concepts/foo.md",
            "knowledge/.node_formats.json",
        ):
            self.assertIn(rel, manifest["files"],
                          f"{rel} missing from manifest")
            self.assertEqual(len(manifest["files"][rel]["sha256"]), 64)


# ---------------------------------------------------------------------------
# Contract 2 & 3: Update flow preserves modified, overwrites untouched
# ---------------------------------------------------------------------------


class UpdateFlowPreservationTests(unittest.TestCase):
    """V47-A hash-compare semantics applied to knowledge/ files:
    untouched → overwrite; modified → preserve + deferral."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-update-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)
        # First install: lay down the v1 baseline + manifest.
        first = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(first["errors"], [])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bump_shipped_node(self, rel: str, new_content: str) -> None:
        """Mutate the source to simulate the orchestrator publishing
        a new version of a shipped KG node."""
        (self.orch / "templates" / "knowledge" / Path(rel)).write_text(
            new_content, encoding="utf-8",
        )

    def test_update_overwrites_user_untouched_nodes(self):
        """User never edited `concepts/foo.md`; orchestrator publishes
        v2. Update overwrites with v2."""
        installed = self.proj / "knowledge" / "concepts" / "foo.md"
        # Sanity: file is at v1 after first install.
        self.assertIn("shipped-v1", installed.read_text(encoding="utf-8"))

        self._bump_shipped_node("concepts/foo.md",
                                "# Foo concept\nshipped-v2\n")
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        self.assertIn("knowledge/concepts/foo.md",
                      result["actions"]["overwrite"])
        self.assertNotIn("knowledge/concepts/foo.md",
                         result["actions"]["preserve"])
        # File on disk now carries v2 bytes.
        self.assertIn("shipped-v2", installed.read_text(encoding="utf-8"))

    def test_update_preserves_user_modified_nodes(self):
        """User edited `concepts/bar.md`; orchestrator publishes v2.
        Update PRESERVES the user's bytes on disk and emits the
        `bundle_user_modified_preserved` deferral."""
        installed = self.proj / "knowledge" / "concepts" / "bar.md"
        installed.write_text(
            "# Bar concept\nUSER EDIT\n",
            encoding="utf-8",
        )

        self._bump_shipped_node("concepts/bar.md",
                                "# Bar concept\nshipped-v2\n")
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        # File appears in PRESERVE bucket — not overwritten.
        self.assertIn("knowledge/concepts/bar.md",
                      result["actions"]["preserve"])
        self.assertNotIn("knowledge/concepts/bar.md",
                         result["actions"]["overwrite"])
        # User bytes on disk untouched.
        self.assertEqual(
            installed.read_text(encoding="utf-8"),
            "# Bar concept\nUSER EDIT\n",
        )
        # Deferral emitted so Claude Code on next session sees the
        # conflict and can offer a merge.
        report = DeferralReport.read(self.proj)
        self.assertTrue(
            report.has_condition("bundle_user_modified_preserved"),
            "`bundle_user_modified_preserved` deferral missing — "
            "the user-modification preservation path didn't emit it",
        )
        body = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md") \
            .read_text(encoding="utf-8")
        self.assertIn("knowledge/concepts/bar.md", body,
                      "deferral body must name the preserved file")

    def test_update_force_overwrites_user_modifications(self):
        """With `force=True`, even user-modified nodes get overwritten.
        Same escape hatch as agents/skills (`install-bundle --force`)."""
        installed = self.proj / "knowledge" / "concepts" / "foo.md"
        installed.write_text(
            "# Foo concept\nUSER EDIT\n",
            encoding="utf-8",
        )
        self._bump_shipped_node("concepts/foo.md",
                                "# Foo concept\nshipped-v2\n")
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
            force=True,
        )
        self.assertIn("shipped-v2",
                      installed.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract 4: User-authored nodes survive unconditionally
# ---------------------------------------------------------------------------


class UserAuthoredNodesSurviveTests(unittest.TestCase):
    """Nodes the orchestrator never shipped (user-authored locals) are
    NOT in `manifest["files"]`. The bundle install machinery never
    visits them — they survive every install / update run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-user-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_authored_node_survives_update(self):
        """Drop a user-only `.md` into `knowledge/` and run an update.
        File must be untouched (NOT in any action bucket)."""
        user_node = self.proj / "knowledge" / "concepts" / "my-local-pattern.md"
        user_node.write_text(
            "# My local pattern\nuser-authored, never shipped\n",
            encoding="utf-8",
        )

        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        # File still on disk, unchanged.
        self.assertTrue(user_node.exists())
        self.assertEqual(
            user_node.read_text(encoding="utf-8"),
            "# My local pattern\nuser-authored, never shipped\n",
        )
        # Not in ANY action bucket — the bundle never saw it.
        rel = "knowledge/concepts/my-local-pattern.md"
        for bucket in (
            "create", "overwrite", "always-overwrite", "preserve",
            "noop", "skip-existing", "skip-disabled",
            "orphan-deleted", "orphan-preserved",
        ):
            self.assertNotIn(rel, result["actions"][bucket],
                             f"User-authored node leaked into '{bucket}' bucket — "
                             "bundle path should never visit it")

    def test_user_authored_node_not_in_manifest(self):
        """The manifest tracks ONLY shipped files. User-authored locals
        must never be added to `manifest["files"]` — that would let
        the orphan-resolution loop see them on a future update."""
        user_node = self.proj / "knowledge" / "concepts" / "my-local-pattern.md"
        user_node.write_text("user-only\n", encoding="utf-8")

        # Run update to refresh manifest.
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "knowledge/concepts/my-local-pattern.md",
            manifest["files"],
            "User-authored node leaked into manifest — would expose it "
            "to the orphan-resolution path on next update",
        )

    def test_user_authored_node_survives_when_shipped_set_shrinks(self):
        """V52-C edge case: orchestrator removes ALL shipped knowledge
        files in a future release. User-authored nodes still must
        survive (they're never in the orphan-deletion candidate set
        because they were never in `manifest["files"]`)."""
        user_node = self.proj / "knowledge" / "concepts" / "my-local-pattern.md"
        user_node.write_text("user-only\n", encoding="utf-8")

        # Wipe the orchestrator's shipped set entirely.
        shutil.rmtree(self.orch / "templates" / "knowledge")
        (self.orch / "templates" / "knowledge").mkdir()

        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        # User node still present + untouched.
        self.assertTrue(user_node.exists())
        self.assertEqual(
            user_node.read_text(encoding="utf-8"),
            "user-only\n",
        )
        # Shipped nodes that the orchestrator no longer ships are
        # orphan-deleted (since they match the prior-shipped hash).
        # The user-authored node is NOT in that list — its presence
        # there would mean the orphan loop picked it up incorrectly.
        self.assertNotIn(
            "knowledge/concepts/my-local-pattern.md",
            result["actions"]["orphan-deleted"],
        )
        self.assertNotIn(
            "knowledge/concepts/my-local-pattern.md",
            result["actions"]["orphan-preserved"],
        )


# ---------------------------------------------------------------------------
# Source-tree invariant: templates/knowledge/ exists in this repo
# ---------------------------------------------------------------------------


class SourceTreeInvariantTests(unittest.TestCase):
    """Lightweight sanity check that the V52-C move actually landed at
    the orchestrator-source level. If a future refactor moves shipped
    KG nodes back to `knowledge/`, this test catches it before the
    install path does."""

    def test_templates_knowledge_directory_exists(self):
        tk = REPO_ROOT / "templates" / "knowledge"
        self.assertTrue(
            tk.is_dir(),
            f"templates/knowledge/ missing at {tk}. V52-C moved the "
            "orchestrator's curated KG set here — if you're seeing this "
            "fail, check that the directory wasn't accidentally renamed "
            "or moved back to `knowledge/`.",
        )

    def test_root_knowledge_directory_absent(self):
        """The legacy `knowledge/` directory at the orchestrator root
        must NOT exist. If a contributor accidentally re-adds it (e.g.
        copies user-state from VCO_dev back into the public repo),
        this test catches it before the install path does."""
        legacy = REPO_ROOT / "knowledge"
        self.assertFalse(
            legacy.exists(),
            f"Legacy `{legacy}` directory present. V52-C moved the "
            "shipped KG set to `templates/knowledge/`. A `knowledge/` "
            "directory at the orchestrator root would either get "
            "double-shipped (if `knowledge` is re-added to the install "
            "whitelist) or silently ignored — neither is desirable.",
        )

    def test_templates_knowledge_has_at_least_one_md_file(self):
        """The orchestrator's curated KG set should not be empty in a
        shipped release. A trivial `templates/knowledge/` with zero
        files would mean the V52-C move incorrectly dropped content."""
        tk = REPO_ROOT / "templates" / "knowledge"
        md_count = sum(1 for _ in tk.rglob("*.md"))
        self.assertGreater(
            md_count, 0,
            f"templates/knowledge/ has 0 markdown files. The V52-C "
            "move was supposed to relocate the curated KG set (113 .md "
            "files at v0.2.52 ship time) into this directory.",
        )


if __name__ == "__main__":
    unittest.main()
