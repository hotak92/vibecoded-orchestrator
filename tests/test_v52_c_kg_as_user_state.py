# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-C (v0.2.52) + v0.2.81 — KG nodes as user-curated state, root-only.

The orchestrator's curated KG node set lives under ``templates/knowledge/``.

V52-C (v0.2.52) moved it there (out of the source-tree ``knowledge/`` that the
legacy ``ORCHESTRATOR_MANAGED_PATHS`` whitelist copied) and materialized it
into ``<project>/knowledge/`` via ``install_project_bundle`` with V47-A
manifest-hash preserve semantics.

**v0.2.81 evolution — root-only**: the curated set now ships ONLY to the
orchestrator-root target (``project_root is None`` or ``project_root ≡
orchestrator_root``). It lives once in the root's ``knowledge/`` (== the shared
collection by adopt-and-route definition) and non-root projects read it via the
shared-read fan-out — no per-project copy. Only the depth-1
``_PER_PROJECT_KNOWLEDGE_FILES`` allowlist (TAG_HIERARCHY.md, VOCABULARY.md,
.node_formats.json, .node_embeddings.README.txt) still ships per-project.

This file pins the contracts:

1. **Root-target materialization** — for a root install (folder ≡ orch root)
   every file under ``templates/knowledge/`` reaches ``<root>/knowledge/`` with
   the correct relative path; nested subdirectories preserved.
2. **Non-root gate** — for a non-root project only the allowlisted top-level
   files ship; the curated ``concepts/`` / ``models/`` / ``tools/`` /
   ``patterns/`` nodes are NOT materialized.
3. **Update flow (root)** preserves user-modified nodes / overwrites untouched
   nodes / honours ``force`` — unchanged semantics, at the root.
4. **User-authored nodes survive unconditionally** — nodes the orchestrator
   never shipped are never visited by the bundle machinery, root or non-root.
5. **``knowledge`` is out of the install whitelist** — the legacy
   ``apply_conflict_strategy`` path never touches ``knowledge/``.
6. **Source-tree invariant** — ``templates/knowledge/`` exists and is
   non-empty; root ``knowledge/`` is never git-TRACKED in the public repo
   (Step 4c may create it on an installed machine, so filesystem-absence is no
   longer the invariant — "never committed" is).

The fixtures mirror ``tests/test_install_bundle.py::_make_fake_orchestrator``
but extend it with a ``templates/knowledge/`` subtree.
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


# Curated (non-allowlisted) rels that should NEVER ship to a non-root project.
_CURATED_RELS = [
    "knowledge/concepts/foo.md",
    "knowledge/concepts/bar.md",
    "knowledge/models/qwen.md",
    "knowledge/tools/weaviate.md",
]
# Allowlisted top-level rels that ship to EVERY project.
_ALLOWLISTED_RELS = [
    "knowledge/TAG_HIERARCHY.md",
    "knowledge/VOCABULARY.md",
    "knowledge/.node_formats.json",
]


# ---------------------------------------------------------------------------
# Contract 5: `knowledge` out of the install whitelist
# ---------------------------------------------------------------------------


class KnowledgeOutOfWhitelistTests(unittest.TestCase):
    """`knowledge` must NOT be in `ORCHESTRATOR_MANAGED_PATHS`.

    The legacy whitelist-copy path (`apply_conflict_strategy`) iterates
    this constant; with `knowledge` out of it, the user's `knowledge/`
    directory is never visited by that path. This is the structural fix
    that prevents the v0.2.51 modify-vs-delete merge conflict at root.
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
# Contract 1 & 2: Root-target full materialization vs non-root gate
# ---------------------------------------------------------------------------


class FreshInstallMaterializationTests(unittest.TestCase):
    """v0.2.81: root-target installs (folder ≡ orch root) materialize every
    file under `templates/knowledge/`; non-root projects get only the
    allowlisted top-level files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-fresh-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- Root-target: full curated set -------------------------------------

    def test_root_target_all_shipped_files_materialized(self):
        """Root install (folder == orch root) ships the FULL curated set."""
        result = project_init.install_project_bundle(
            self.orch,  # folder == orchestrator root → root target
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        expected_rels = _ALLOWLISTED_RELS + _CURATED_RELS
        for rel in expected_rels:
            dest_rel = str(Path(rel))
            self.assertIn(
                dest_rel, result["actions"]["create"],
                f"{rel} missing from create actions: "
                f"{result['actions']['create']}",
            )
            on_disk = self.orch / Path(rel)
            self.assertTrue(on_disk.exists(),
                            f"{rel} not materialized on disk")
            self.assertIn("v1", on_disk.read_text(encoding="utf-8"))

    def test_root_target_none_project_root_ships_full_set(self):
        """`project_root=None` (legacy self-install default) is a root
        target — full curated set ships. Uses `_enumerate_bundle_files`
        directly since `install_project_bundle` always passes folder."""
        ops = project_init._enumerate_bundle_files(self.orch, project_root=None)
        dests = {op.dest_rel for op in ops}
        for rel in _CURATED_RELS + _ALLOWLISTED_RELS:
            self.assertIn(str(Path(rel)), dests, f"{rel} missing for root None")

    def test_root_target_nested_subdirectories_preserved(self):
        project_init.install_project_bundle(
            self.orch,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        nested = self.orch / "knowledge" / "concepts" / "foo.md"
        self.assertTrue(nested.exists(),
                        "Nested subdirectory structure lost during materialization")
        self.assertFalse((self.orch / "knowledge" / "foo.md").exists())

    def test_root_target_non_markdown_metadata_files_copied(self):
        project_init.install_project_bundle(
            self.orch,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        metadata = self.orch / "knowledge" / ".node_formats.json"
        self.assertTrue(metadata.exists())
        src = self.orch / "templates" / "knowledge" / ".node_formats.json"
        self.assertEqual(metadata.read_bytes(), src.read_bytes())

    def test_root_target_manifest_tracks_knowledge_files(self):
        project_init.install_project_bundle(
            self.orch,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        manifest_path = self.orch / ".claude" / ".vco-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for rel in (
            "knowledge/TAG_HIERARCHY.md",
            "knowledge/concepts/foo.md",
            "knowledge/.node_formats.json",
        ):
            self.assertIn(rel, manifest["files"], f"{rel} missing from manifest")
            self.assertEqual(len(manifest["files"][rel]["sha256"]), 64)

    # -- Non-root: allowlist-only ------------------------------------------

    def test_non_root_only_allowlisted_files_ship(self):
        """A non-root project gets ONLY the 4 allowlisted top-level files;
        curated nodes are NOT materialized."""
        result = project_init.install_project_bundle(
            self.proj,  # folder != orch root → non-root
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        created = set(result["actions"]["create"])
        for rel in _ALLOWLISTED_RELS:
            self.assertIn(str(Path(rel)), created,
                          f"allowlisted {rel} should ship per-project")
            self.assertTrue((self.proj / Path(rel)).exists())
        for rel in _CURATED_RELS:
            self.assertNotIn(str(Path(rel)), created,
                             f"curated {rel} MUST NOT ship to a non-root project")
            self.assertFalse((self.proj / Path(rel)).exists(),
                             f"curated {rel} leaked onto disk in a non-root project")

    def test_non_root_curated_subdirs_absent_on_disk(self):
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        # No curated subdirectories at all under the non-root knowledge/.
        for sub in ("concepts", "models", "tools", "patterns"):
            self.assertFalse((self.proj / "knowledge" / sub).exists(),
                             f"curated subdir {sub}/ present in non-root project")


# ---------------------------------------------------------------------------
# Contract 3: Update flow at the ROOT preserves modified, overwrites untouched
# ---------------------------------------------------------------------------


class UpdateFlowPreservationTests(unittest.TestCase):
    """V47-A hash-compare semantics applied to knowledge/ files AT THE ROOT
    (the only target that materializes curated nodes as of v0.2.81):
    untouched → overwrite; modified → preserve + deferral."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-update-"))
        # folder == orchestrator root → root target (curated nodes ship).
        self.orch = self.tmp / "orchestrator"
        self.orch.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)
        self.proj = self.orch  # root install target
        first = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(first["errors"], [])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bump_shipped_node(self, rel: str, new_content: str) -> None:
        (self.orch / "templates" / "knowledge" / Path(rel)).write_text(
            new_content, encoding="utf-8",
        )

    def test_update_overwrites_user_untouched_nodes(self):
        installed = self.proj / "knowledge" / "concepts" / "foo.md"
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
        self.assertIn("shipped-v2", installed.read_text(encoding="utf-8"))

    def test_update_preserves_user_modified_nodes_SILENTLY_no_deferral(self):
        """v0.2.85 NEW-1 (contract CHANGED): a divergent user-owned knowledge
        node is PRESERVED on disk (classified `preserve`, in the actions
        envelope) but does NOT emit a `bundle_user_modified_preserved` deferral.

        Rationale: that deferral's advertised remediation is `--update --force`,
        which is a deliberate NO-OP for knowledge (B-1: force never destroys user
        KG state). Emitting it would (a) advertise a dead command, and (b) let a
        later `--force` run dishonestly auto-resolve an entry naming a
        still-divergent node (the Fable v0.2.85 re-review's NEW-1). Divergent
        user knowledge is the EXPECTED steady state — silent, like
        `keep-regenerated`. (This test previously asserted the deferral DID fire,
        which encoded the emit/clear flap.)"""
        installed = self.proj / "knowledge" / "concepts" / "bar.md"
        installed.write_text("# Bar concept\nUSER EDIT\n", encoding="utf-8")

        self._bump_shipped_node("concepts/bar.md",
                                "# Bar concept\nshipped-v2\n")
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        # Preserved on disk + classified preserve (envelope stays truthful).
        self.assertIn("knowledge/concepts/bar.md",
                      result["actions"]["preserve"])
        self.assertNotIn("knowledge/concepts/bar.md",
                         result["actions"]["overwrite"])
        self.assertEqual(
            installed.read_text(encoding="utf-8"),
            "# Bar concept\nUSER EDIT\n",
        )
        # But NO deferral — knowledge preservation is silent (NEW-1).
        report = DeferralReport.read(self.proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_preserved"),
            "a divergent knowledge node must NOT emit the --force-remediated "
            "deferral (its force remediation is a no-op — NEW-1)",
        )

    def test_force_on_preserved_knowledge_does_not_flap_deferral(self):
        """v0.2.85 NEW-1 regression: the emit→dishonest-clear→re-emit flap. A
        normal update preserves a divergent knowledge node (no deferral, per the
        test above). A subsequent `--update --force` run must NOT write a false
        'condition no longer applies' auto-resolution for it (there was no entry
        to resolve, and the node is still divergent + still preserved). The
        knowledge stays on disk under force (B-1)."""
        installed = self.proj / "knowledge" / "concepts" / "bar.md"
        installed.write_text("# Bar concept\nUSER EDIT\n", encoding="utf-8")
        self._bump_shipped_node("concepts/bar.md",
                                "# Bar concept\nshipped-v2\n")
        # Normal update — preserves, no deferral.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True)
        self.assertFalse(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"))

        # Force run — knowledge survives (B-1), no deferral appears or flaps.
        res = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True, force=True)
        self.assertEqual(installed.read_text(encoding="utf-8"),
                         "# Bar concept\nUSER EDIT\n",
                         "force must not overwrite user knowledge (B-1)")
        self.assertIn("knowledge/concepts/bar.md", res["actions"]["preserve"])
        self.assertFalse(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"),
            "force run must not resurrect/flap a knowledge preserve deferral",
        )
        # No dishonest auto-resolution audit row for the knowledge node.
        auto_res = self.proj / ".claude" / "context" / "auto-resolutions.jsonl"
        if auto_res.exists():
            self.assertNotIn(
                "bundle_user_modified_preserved", auto_res.read_text("utf-8"),
                "force run wrote a false auto-resolution for a knowledge preserve",
            )

    def test_update_force_PRESERVES_user_knowledge_never_destroys(self):
        """v0.2.85 B-1 (data-loss fix): `--force` must NOT overwrite a
        user-edited `knowledge/**` KG node. Knowledge is USER-OWNED state
        (v0.2.81) and the root's `knowledge/` is gitignored (no git recovery)
        and takes NO adoption backup on the force→overwrite branch, so a force
        overwrite would be unrecoverable data loss. Force is scoped to the code
        surface (hooks/scripts/agents/skills/compose); knowledge stays
        `preserve` regardless of `--force`.

        (This test previously asserted the OPPOSITE — that force overwrites the
        node — which encoded the B-1 data-loss bug as intended behavior. The
        Fable v0.2.85 review reproduced the loss end-to-end; the contract is now
        inverted to the safe one.)"""
        installed = self.proj / "knowledge" / "concepts" / "foo.md"
        installed.write_text("# Foo concept\nUSER EDIT\n", encoding="utf-8")
        self._bump_shipped_node("concepts/foo.md",
                                "# Foo concept\nshipped-v2\n")
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
            force=True,
        )
        # The user's edit SURVIVES; shipped bytes did NOT land.
        body = installed.read_text(encoding="utf-8")
        self.assertIn("USER EDIT", body,
                      "force overwrote a user knowledge node — B-1 data loss")
        self.assertNotIn("shipped-v2", body)
        # Classified preserve (not overwrite), so it is surfaced, not silently lost.
        rels = [p for p in result["actions"]["preserve"]]
        self.assertTrue(
            any(str(p).replace("\\", "/").endswith("knowledge/concepts/foo.md")
                for p in rels),
            f"knowledge node must be classified preserve under force; got {rels}",
        )
        self.assertNotIn(
            "knowledge/concepts/foo.md",
            [str(p).replace("\\", "/") for p in result["actions"]["overwrite"]],
        )


# ---------------------------------------------------------------------------
# Contract 4: User-authored nodes survive unconditionally
# ---------------------------------------------------------------------------


class UserAuthoredNodesSurviveTests(unittest.TestCase):
    """Nodes the orchestrator never shipped (user-authored locals) are NOT in
    `manifest["files"]`. The bundle install machinery never visits them — they
    survive every install / update run. Exercised at the ROOT (where curated
    nodes materialize, so the orphan/retirement paths are fully in play)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v52c-user-"))
        self.orch = self.tmp / "orchestrator"
        self.orch.mkdir()
        _make_fake_orchestrator_with_knowledge(self.orch)
        self.proj = self.orch  # root target
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_authored_node_survives_update(self):
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
        self.assertTrue(user_node.exists())
        self.assertEqual(
            user_node.read_text(encoding="utf-8"),
            "# My local pattern\nuser-authored, never shipped\n",
        )
        rel = "knowledge/concepts/my-local-pattern.md"
        for bucket in (
            "create", "overwrite", "always-overwrite", "preserve",
            "noop", "skip-existing", "skip-disabled",
            "orphan-deleted", "orphan-preserved", "knowledge-retired",
        ):
            self.assertNotIn(rel, result["actions"][bucket],
                             f"User-authored node leaked into '{bucket}' bucket — "
                             "bundle path should never visit it")

    def test_user_authored_node_not_in_manifest(self):
        user_node = self.proj / "knowledge" / "concepts" / "my-local-pattern.md"
        user_node.write_text("user-only\n", encoding="utf-8")

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
        """Orchestrator removes ALL shipped knowledge files in a future
        release. User-authored nodes still survive (never in the orphan
        candidate set). At the ROOT the removed curated nodes orphan-delete."""
        user_node = self.proj / "knowledge" / "concepts" / "my-local-pattern.md"
        user_node.write_text("user-only\n", encoding="utf-8")

        shutil.rmtree(self.orch / "templates" / "knowledge")
        (self.orch / "templates" / "knowledge").mkdir()

        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=True,
        )
        self.assertTrue(user_node.exists())
        self.assertEqual(user_node.read_text(encoding="utf-8"), "user-only\n")
        self.assertNotIn(
            "knowledge/concepts/my-local-pattern.md",
            result["actions"]["orphan-deleted"],
        )
        self.assertNotIn(
            "knowledge/concepts/my-local-pattern.md",
            result["actions"]["orphan-preserved"],
        )


# ---------------------------------------------------------------------------
# Source-tree invariant: templates/knowledge/ exists; root knowledge/ untracked
# ---------------------------------------------------------------------------


class SourceTreeInvariantTests(unittest.TestCase):
    """Lightweight sanity check that the V52-C move landed and stays landed."""

    def test_templates_knowledge_directory_exists(self):
        tk = REPO_ROOT / "templates" / "knowledge"
        self.assertTrue(
            tk.is_dir(),
            f"templates/knowledge/ missing at {tk}. V52-C moved the "
            "orchestrator's curated KG set here — if you're seeing this "
            "fail, check that the directory wasn't accidentally renamed "
            "or moved back to `knowledge/`.",
        )

    def test_root_knowledge_directory_not_git_tracked(self):
        """v0.2.81: the root `knowledge/` directory must NOT be git-TRACKED
        in the public repo. install.py Step 4c CREATES it on any installed
        machine (materializing the curated set into root == shared), so
        filesystem-absence is no longer the invariant — "never committed to
        the public repo" is. A `.gitignore /knowledge/` rule keeps Step 4c's
        output out of `git status` noise + accidental commits.

        Skips gracefully when git is unavailable (CI without a checkout)."""
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--", "knowledge/"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            self.skipTest("git unavailable — cannot assert git-index state")
            return
        if proc.returncode != 0:
            self.skipTest(
                "git ls-files failed (not a repo checkout?) — "
                f"stderr: {proc.stderr.strip()}"
            )
            return
        tracked = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(
            tracked, [],
            "The root `knowledge/` directory has git-TRACKED files:\n"
            + "\n".join(tracked)
            + "\nv0.2.81 ships the curated KG set from `templates/knowledge/` "
            "and materializes it into root `knowledge/` at install time "
            "(Step 4c). The public repo must NOT commit those nodes — add "
            "`/knowledge/` to .gitignore and `git rm --cached` any tracked "
            "node. (The maintainer's private fork may commit curated root "
            "nodes; a `/knowledge/` ignore only affects UNTRACKED files, so "
            "already-tracked fork nodes stay tracked.)",
        )

    def test_templates_knowledge_has_at_least_one_md_file(self):
        tk = REPO_ROOT / "templates" / "knowledge"
        md_count = sum(1 for _ in tk.rglob("*.md"))
        self.assertGreater(
            md_count, 0,
            "templates/knowledge/ has 0 markdown files. The V52-C move was "
            "supposed to relocate the curated KG set into this directory.",
        )


if __name__ == "__main__":
    unittest.main()
