# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 field bug (2026-09-05): safe-add kept pre-VCO wrappers and the KG
built nothing.

THE FIELD REPORT
----------------
A project was added with **safe add** on v0.2.91. Its `.claude/scripts/` held
five hand-rolled, pre-VCO wrappers from another orchestrator install, each of
the shape::

    CLAUDE_PROJECT="/home/user/PROGETTI/Claude"
    VENV="$CLAUDE_PROJECT/claude_mcp_servers/.venv"
    export KG_COLLECTION="${KG_COLLECTION:-ClaudeKnowledgeGraph}"
    source "$VENV/bin/activate"
    python "$CLAUDE_PROJECT/.claude/scripts/sync_knowledge_graph.py" ...

First-install classified every one of them `skip-existing` — whose deferral,
`bundle_skipped_existing_files`, is declared `informational_record`: *"nothing
is pending; do not surface it as a problem."* The launcher then spawned the
project-local `kg-sync`, which sourced a venv that does not exist on this
machine and ran ANOTHER project's sync script. Recorded outcome in
`launcher.db`::

    status = failed
    kg_total = 329, kg_succeeded = 0, kg_failed = 329
    error_message = ... ModuleNotFoundError: No module named 'weaviate'

`MultiagentOrchestrator_KnowledgeGraph`: 0 objects against 329 markdown files.

WHAT THESE TESTS PIN
--------------------
Not the classifier in isolation — the field scenario, driven through the REAL
``install_project_bundle`` entry point with ``safe_add=True,
update_mode=False``:

* a pre-VCO wrapper is ADOPTED (its bytes replaced with the shipped ones) and
  its previous bytes are captured under ``.claude/backups/bundle-adoptions/``;
* a file that is provably an OLDER VERSION VCO ITSELF SHIPPED is adopted too
  (git-history match);
* a genuinely hand-authored file at a shipped destination is still PRESERVED
  byte-for-byte — the narrowing is the whole point, "differs from shipped" is
  NOT evidence of staleness;
* a user-owned ``knowledge/**`` node is never adopted;
* the resulting project passes the stale-wrapper probe (so the KG/code-graph
  spawn sites resolve a working wrapper).

Each guard names the mutation that turns it red in its docstring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib import wrapper_health  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


# The pre-VCO wrapper shape, verbatim in structure from the field report: it
# activates a venv belonging to a DIFFERENT checkout, runs a script from that
# checkout, and defaults the collection to a FOREIGN class name.
PRE_VCO_WRAPPER = (
    "#!/bin/bash\n"
    "# KG search wrapper - delegates to another project's scripts\n"
    'CLAUDE_PROJECT="/somewhere/else/Claude"\n'
    'VENV="$CLAUDE_PROJECT/claude_mcp_servers/.venv"\n'
    'export KG_COLLECTION="${KG_COLLECTION:-ClaudeKnowledgeGraph}"\n'
    'source "$VENV/bin/activate"\n'
    'python "$CLAUDE_PROJECT/.claude/scripts/search_knowledge.py" "$@"\n'
)

# What VCO ships for the same destination: the resilient interpreter-discovery
# ladder keyed on $VCT_INSTALL_ROOT.
SHIPPED_WRAPPER = (
    "#!/bin/bash\n"
    "CANDIDATES=(\n"
    '  "${VCT_INSTALL_ROOT:-}/.venv"\n'
    '  "${VCT_ORCHESTRATOR_ROOT:-}/.venv"\n'
    ")\n"
    'exec python -m vco_lib.kg_search "$@"\n'
)

# A file the user genuinely wrote at a shipped destination. Nothing about it
# is VCO-shaped: no marker, and no version of it was ever shipped.
USER_AUTHORED_HOOK = (
    "#!/bin/bash\n"
    "# Bespoke hook for THIS project: notify our internal bus on every edit.\n"
    'curl -s -XPOST "$INTERNAL_BUS/edited" -d "$1" >/dev/null 2>&1 || true\n'
)

_ADOPT_BACKUP_ROOT = Path(".claude") / "backups" / "bundle-adoptions"


class _Fixture(unittest.TestCase):
    """Fixture orchestrator + project reproducing the field layout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-stalefirst-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

        # The fixture's `templates/scripts/kg-search` stands in for every
        # marker-bearing shipped wrapper. Give it the real shipped shape.
        (self.orch / "templates" / "scripts" / "kg-search").write_text(
            SHIPPED_WRAPPER, encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _plant(self, rel: str, text: str) -> Path:
        p = self.proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _install(self, **kw):
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
            safe_add=True,
            **kw,
        )
        self.assertEqual(result["errors"], [], "bundle install must not error")
        return result

    def _backup_copies(self, leaf: str) -> list[Path]:
        root = self.proj / _ADOPT_BACKUP_ROOT
        if not root.is_dir():
            return []
        return sorted(root.glob(f"*/**/{leaf}"))


class StaleWrapperIsAdoptedOnFirstInstall(_Fixture):
    def test_pre_vco_wrapper_is_replaced_and_backed_up(self):
        """THE FIELD SCENARIO. A pre-VCO `kg-search` present at add time is
        replaced with the shipped bytes, and the user's previous bytes are
        captured first.

        Mutation check: make the `not update_mode` branch of
        `_file_action` return `("skip-existing", source_bytes)`
        unconditionally (its pre-v0.2.92 body) and this fails — the wrapper
        still holds the foreign-collection default.
        """
        planted = self._plant(".claude/scripts/kg-search", PRE_VCO_WRAPPER)
        self._install()

        after = planted.read_text(encoding="utf-8")
        self.assertEqual(
            after, SHIPPED_WRAPPER,
            "the stale wrapper must be refreshed to the shipped version",
        )
        self.assertNotIn(
            "ClaudeKnowledgeGraph", after,
            "a wrapper defaulting to a FOREIGN collection must not survive "
            "the install — that is cross-project contamination, not a "
            "customization",
        )

        if os.name == "posix":
            self.assertTrue(
                os.access(planted, os.X_OK),
                "the adopted wrapper must stay executable — the launcher "
                "spawns it directly, so a 0644 copy would fail to run",
            )

        backups = self._backup_copies("kg-search")
        self.assertEqual(
            len(backups), 1,
            f"exactly one adoption backup expected, found {backups}",
        )
        self.assertEqual(
            backups[0].read_text(encoding="utf-8"), PRE_VCO_WRAPPER,
            "the backup must hold the user's original bytes verbatim",
        )

    def test_second_install_does_not_re_adopt(self):
        """Idempotency. After adoption the on-disk bytes ARE the shipped bytes,
        so a second run classifies `noop` and writes no second backup — an
        adopt that re-fired every run would grow a backup directory forever
        and re-emit the notice on every add/update.
        """
        self._plant(".claude/scripts/kg-search", PRE_VCO_WRAPPER)
        self._install()
        first = self._backup_copies("kg-search")
        self.assertEqual(len(first), 1)
        self._install()
        self.assertEqual(
            self._backup_copies("kg-search"), first,
            "a healthy (already-adopted) wrapper must not be adopted again",
        )

    def test_project_passes_the_stale_wrapper_probe_afterwards(self):
        """End state, stated the way the launcher asks it: after the install
        the project has NO stale wrapper, so the KG / code-graph spawn sites
        resolve a working one.

        Mutation check: same as above — with the unconditional skip the probe
        still reports the wrapper stale.
        """
        self._plant(".claude/scripts/kg-search", PRE_VCO_WRAPPER)
        self._install()
        self.assertEqual(
            wrapper_health.stale_project_wrappers(
                self.proj, orchestrator_root=self.orch
            ),
            [],
            "no marker-bearing wrapper may remain stale after the install",
        )

    def test_deferral_does_not_file_the_wrapper_as_an_informational_record(self):
        """The wrapper must not appear in `bundle_skipped_existing_files` —
        the entry whose declared disposition is `informational_record`
        ("nothing is pending"). That filing is what told the user nothing was
        wrong while the KG built nothing.

        Mutation check: with the unconditional skip, the path is listed there.
        """
        self._plant(".claude/scripts/kg-search", PRE_VCO_WRAPPER)
        self._install()
        report = DeferralReport.read(self.proj)
        body = ""
        deferred = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if deferred.is_file():
            body = deferred.read_text(encoding="utf-8")
        if report.has_condition("bundle_skipped_existing_files"):
            self.assertNotIn(
                "scripts/kg-search", body,
                "a provably-stale wrapper must not be filed as a preserved "
                "user customization",
            )


class OldShippedVersionIsAdoptedOnFirstInstall(_Fixture):
    """Rule 1: bytes VCO itself shipped in an earlier release."""

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.orch), *args],
            check=True, capture_output=True,
        )

    def test_previous_shipped_version_is_adopted(self):
        """A project holding v1 of a shipped script while the orchestrator now
        ships v2 is holding VCO's OWN bytes, one release old. Adopt.

        Mutation check: delete the `old-shipped-version` rule from
        `_stale_shipped_artifact_reason` — `notify.py` carries no marker, so
        rule 2 cannot catch it and the assertion fails.
        """
        script = self.orch / "templates" / "scripts" / "notify.py"
        v1 = "def notify():\n    return 'v1'\n"
        script.write_text(v1, encoding="utf-8")

        self._git("init", "-q")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "t")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "shipped v1")

        v2 = "def notify():\n    return 'v2'\n"
        script.write_text(v2, encoding="utf-8")

        planted = self._plant(".claude/scripts/notify.py", v1)
        self._install()

        self.assertEqual(
            planted.read_text(encoding="utf-8"), v2,
            "an older version VCO itself shipped is not user work — adopt it",
        )
        backups = self._backup_copies("notify.py")
        self.assertEqual(len(backups), 1, f"expected one backup, got {backups}")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), v1)


class GenuineUserWorkIsStillPreserved(_Fixture):
    """LEAVE-ALONE side. The narrowing is the point: 'differs from shipped' is
    not evidence of staleness, and only two PROVABLE cases are adopted."""

    def test_hand_authored_file_at_a_shipped_destination_is_untouched(self):
        """Mutation check: broaden `_file_action`'s first-install branch to
        `return ("adopt", source_bytes)` for every divergent file (full
        symmetry with update mode) and this fails — the user's hook is
        overwritten.
        """
        planted = self._plant(".claude/hooks/foo.sh", USER_AUTHORED_HOOK)
        self._install()
        self.assertEqual(
            planted.read_text(encoding="utf-8"), USER_AUTHORED_HOOK,
            "a file with no marker and no shipped-history match is user work "
            "and must be preserved byte-for-byte",
        )
        self.assertEqual(
            self._backup_copies("foo.sh"), [],
            "a preserved file must not be backed up — nothing was replaced",
        )

    def test_user_knowledge_node_is_never_adopted(self):
        """`knowledge/**` is user-owned state (v0.2.81) and is carved out
        BEFORE the staleness predicate runs — otherwise a KG node that happens
        to mention `$VCT_INSTALL_ROOT` in the shipped seed would be classified
        stale and the user's own knowledge overwritten.

        Driven through `_file_action`, the classification home the install
        loop calls per file. The knowledge tree is root-only (v0.2.81), so a
        non-root fixture project has no `knowledge/**` op to drive — asserting
        this through `install_project_bundle` on such a project would pass
        vacuously, which is how a guard comes to be credited without firing.

        Mutation check: drop `not _is_knowledge_dest(op.dest_rel) and` from
        the first-install branch and this fails.
        """
        shipped = self.orch / "templates" / "knowledge" / "concepts" / "n.md"
        shipped.parent.mkdir(parents=True, exist_ok=True)
        # Marker-bearing shipped bytes: rule 2 WOULD fire if the carve-out
        # were not there.
        shipped.write_text(
            "# shipped node\nuses $VCT_INSTALL_ROOT\n", encoding="utf-8"
        )
        mine = "# MY node\nnotes I wrote by hand\n"
        planted = self._plant("knowledge/concepts/n.md", mine)

        op = project_init._BundleFileOp(
            dest_rel=str(Path("knowledge") / "concepts" / "n.md"),
            source_abs=shipped,
            source_rel="templates/knowledge/concepts/n.md",
        )
        action, _ = project_init._file_action(
            op, planted,
            update_mode=False,
            manifest={},
            orchestrator_root=self.orch,
            project_root=self.proj,
        )
        self.assertEqual(
            action, "skip-existing",
            "a user-owned KG node must never be classified adopt",
        )

    def test_a_marker_bearing_script_dest_would_have_been_adopted(self):
        """Companion to the test above: the SAME predicate, on a
        `.claude/scripts` destination, does return `adopt`. Without this the
        knowledge assertion could pass because the predicate never fires at
        all rather than because the carve-out held.
        """
        shipped = self.orch / "templates" / "scripts" / "kg-search"
        planted = self._plant(".claude/scripts/kg-search", PRE_VCO_WRAPPER)
        op = project_init._BundleFileOp(
            dest_rel=str(Path(".claude") / "scripts" / "kg-search"),
            source_abs=shipped,
            source_rel="templates/scripts/kg-search",
        )
        action, _ = project_init._file_action(
            op, planted,
            update_mode=False,
            manifest={},
            orchestrator_root=self.orch,
            project_root=self.proj,
        )
        self.assertEqual(action, "adopt")


class WrapperHealthEnumeration(unittest.TestCase):
    """The enumeration itself: derived from the shipped templates, never
    hand-listed."""

    def test_real_shipped_set_covers_every_ladder_bearing_wrapper(self):
        """Mutation check: restore the pre-v0.2.92 hardcoded tuple
        `("code-graph-analyze", "code-graph-analyze.ps1", "kg-sync",
        "kg-sync.ps1")` and the `kg-search` / `kg-info` / `code-graph-query`
        assertions fail — those are three of the five wrappers the field
        project actually carried.
        """
        names = wrapper_health.marker_bearing_basenames(REPO_ROOT)
        for expected in (
            "code-graph-analyze", "code-graph-analyze.ps1",
            "kg-sync", "kg-sync.ps1",
            "kg-search", "kg-search.ps1",
            "kg-info", "kg-info.ps1",
            "code-graph-query", "code-graph-query.ps1",
        ):
            self.assertIn(
                expected, names,
                f"{expected} ships the $VCT_INSTALL_ROOT ladder and must be "
                "stale-checked",
            )
        # Derived exclusions: these carry no ladder in their shipped form, so
        # marker-checking them would condemn every healthy copy.
        self.assertNotIn("kg-duplicates", names)
        self.assertNotIn("generate-kg-summary.py", names)

    def test_marker_survives_every_install_transform(self):
        """COUPLING GUARD. `stale_project_wrappers` reads the SHIPPED TEMPLATE
        to decide which files are marker-bearing, while what lands on disk is
        the POST-TRANSFORM bytes (placeholder substitution, and since v0.2.92
        `vco_lib.rewire` on the ten `VCO-REWIRE` scripts). If a transform ever
        strips `$VCT_INSTALL_ROOT`, a freshly-installed HEALTHY copy would read
        as stale forever: the `stale_codegraph_wrapper_pending` deferral would
        never self-clear and every update would re-adopt the same file.

        Mutation check: add `{{VCT_ORCHESTRATOR_ROOT}}`-style rewriting that
        removes the literal from any marker-bearing script and this fails.
        """
        from vco_lib import project_init as pi

        tmp = Path(tempfile.mkdtemp(prefix="vct-marker-transform-"))
        try:
            lost = []
            for op in pi._enumerate_bundle_files(REPO_ROOT, tmp):
                name = Path(op.dest_rel.replace("\\", "/")).name
                if not wrapper_health.shipped_requires_marker(name, REPO_ROOT):
                    continue
                raw = op.source_abs.read_bytes()
                post = op.transform(raw) if op.transform else raw
                if not wrapper_health.bytes_are_resilient(post):
                    lost.append(name)
            self.assertEqual(
                lost, [],
                "these shipped files carry the ladder in templates/ but lose "
                "it during install — the staleness probe would never clear "
                "for them",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_marker_literal_matches_the_rust_mirror(self):
        """The one literal that must stay in sync across the language
        boundary. The Rust launcher answers this question at script-resolution
        time, where no Python subprocess is available.

        Mutation check: change either constant and this fails.
        """
        rust = (
            REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "codegraph.rs"
        ).read_text(encoding="utf-8")
        self.assertIn(
            f'RESILIENT_WRAPPER_MARKER: &str = "{wrapper_health.RESILIENT_WRAPPER_MARKER}"',
            rust,
            "the Rust mirror of RESILIENT_WRAPPER_MARKER has drifted from "
            "vco_lib/wrapper_health.py",
        )


if __name__ == "__main__":
    unittest.main()
