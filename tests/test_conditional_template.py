"""Tests for the conditional template primitive (Phase 1.5.B, 2026-05-25).

Covers:
  - Active module → block kept (without tag lines).
  - Inactive module → block stripped (no blank-line scar).
  - {{#if_module_inactive}} mirror logic.
  - Multiple blocks for same module — all consistent.
  - Multiple blocks for different modules — independent.
  - Nesting → TemplateError with line number.
  - Mismatched opening / closing → TemplateError.
  - Unknown tag → TemplateError with offending line number.
  - Variable substitution inside an active block survives the pre-pass.
  - resolve_active_modules stub behavior.

These tests run in isolation — no launcher DB required.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.project_init import (  # noqa: E402
    TemplateError,
    render_conditional_blocks,
    resolve_active_modules,
    _DEFAULT_ACTIVE_MODULES,
)


class RenderActiveTests(unittest.TestCase):
    """`{{#if_module_active}}` — keep when module is active, drop otherwise."""

    def test_active_block_kept(self):
        template = (
            "before\n"
            "{{#if_module_active diagrams}}\n"
            "## Diagrams section body\n"
            "{{/if_module_active}}\n"
            "after\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        # The tag lines themselves must be stripped from the output.
        self.assertNotIn("{{#if_module_active", out)
        self.assertNotIn("{{/if_module_active", out)
        # Body survives.
        self.assertIn("## Diagrams section body", out)
        # Surrounding content survives.
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_inactive_module_block_stripped_no_scar(self):
        # Block is dropped because "diagrams" is NOT in active_modules.
        # The trailing blank line after the closing tag should be consumed
        # too, so we don't get a "before\n\n\nafter" scar.
        template = (
            "before\n"
            "{{#if_module_active diagrams}}\n"
            "## Diagrams section body\n"
            "{{/if_module_active}}\n"
            "\n"
            "after\n"
        )
        out = render_conditional_blocks(template, active_modules=set())
        # Body must NOT appear.
        self.assertNotIn("## Diagrams section body", out)
        # No tag lines.
        self.assertNotIn("{{#if_module_active", out)
        self.assertNotIn("{{/if_module_active", out)
        # The blank-line scar test: "before" should be followed by "after"
        # with at most one separating newline (the consumed trailing blank
        # plus the now-gone block leaves just "before\nafter\n").
        self.assertEqual(out, "before\nafter\n")

    def test_active_block_with_leading_whitespace_on_tag(self):
        # Whole-line tag matching tolerates leading/trailing whitespace.
        template = (
            "before\n"
            "   {{#if_module_active diagrams}}   \n"
            "body\n"
            "   {{/if_module_active}}   \n"
            "after\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertIn("body", out)
        self.assertNotIn("{{#if_module_active", out)

    def test_active_block_preserves_trailing_newline_status(self):
        # No trailing newline in input → no trailing newline in output.
        template_no_nl = (
            "before\n"
            "{{#if_module_active diagrams}}\n"
            "body\n"
            "{{/if_module_active}}"
        )
        out = render_conditional_blocks(template_no_nl, active_modules={"diagrams"})
        self.assertEqual(out, "before\nbody")

        # Trailing newline in input → trailing newline in output.
        template_with_nl = template_no_nl + "\n"
        out2 = render_conditional_blocks(
            template_with_nl, active_modules={"diagrams"}
        )
        self.assertEqual(out2, "before\nbody\n")


class RenderInactiveTests(unittest.TestCase):
    """`{{#if_module_inactive}}` — keep when module is NOT active."""

    def test_inactive_block_kept_when_module_inactive(self):
        template = (
            "before\n"
            "{{#if_module_inactive diagrams}}\n"
            "Diagrams is OFF. Enable it in DiagramsTab.\n"
            "{{/if_module_inactive}}\n"
            "after\n"
        )
        out = render_conditional_blocks(template, active_modules=set())
        self.assertIn("Diagrams is OFF", out)

    def test_inactive_block_stripped_when_module_active(self):
        template = (
            "before\n"
            "{{#if_module_inactive diagrams}}\n"
            "Diagrams is OFF. Enable it in DiagramsTab.\n"
            "{{/if_module_inactive}}\n"
            "after\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertNotIn("Diagrams is OFF", out)
        self.assertNotIn("{{#if_module_inactive", out)
        self.assertEqual(out, "before\nafter\n")


class MultipleBlocksTests(unittest.TestCase):
    """Independence between blocks: same module → consistent; different
    modules → independent decisions."""

    def test_two_blocks_same_module_both_kept(self):
        template = (
            "{{#if_module_active diagrams}}\n"
            "block A\n"
            "{{/if_module_active}}\n"
            "middle\n"
            "{{#if_module_active diagrams}}\n"
            "block B\n"
            "{{/if_module_active}}\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertIn("block A", out)
        self.assertIn("block B", out)
        self.assertIn("middle", out)

    def test_two_blocks_same_module_both_dropped(self):
        template = (
            "{{#if_module_active diagrams}}\n"
            "block A\n"
            "{{/if_module_active}}\n"
            "middle\n"
            "{{#if_module_active diagrams}}\n"
            "block B\n"
            "{{/if_module_active}}\n"
        )
        out = render_conditional_blocks(template, active_modules=set())
        self.assertNotIn("block A", out)
        self.assertNotIn("block B", out)
        self.assertIn("middle", out)

    def test_different_modules_independent(self):
        # diagrams active, rl inactive → only diagrams block survives.
        template = (
            "{{#if_module_active diagrams}}\n"
            "DIAGRAMS BLOCK\n"
            "{{/if_module_active}}\n"
            "{{#if_module_active rl}}\n"
            "RL BLOCK\n"
            "{{/if_module_active}}\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertIn("DIAGRAMS BLOCK", out)
        self.assertNotIn("RL BLOCK", out)

    def test_active_and_inactive_mirror_pair(self):
        # An active and an inactive block for the same module are mutually
        # exclusive: exactly one survives.
        template = (
            "{{#if_module_active diagrams}}\n"
            "ON message\n"
            "{{/if_module_active}}\n"
            "{{#if_module_inactive diagrams}}\n"
            "OFF message\n"
            "{{/if_module_inactive}}\n"
        )
        out_active = render_conditional_blocks(
            template, active_modules={"diagrams"}
        )
        self.assertIn("ON message", out_active)
        self.assertNotIn("OFF message", out_active)

        out_inactive = render_conditional_blocks(template, active_modules=set())
        self.assertNotIn("ON message", out_inactive)
        self.assertIn("OFF message", out_inactive)


class ErrorTests(unittest.TestCase):
    """Malformed templates raise TemplateError with line numbers."""

    def test_nested_blocks_raise(self):
        # Even nesting of the same kind is unsupported in Phase 1.5.B.
        template = (
            "{{#if_module_active diagrams}}\n"
            "outer\n"
            "{{#if_module_active rl}}\n"
            "inner\n"
            "{{/if_module_active}}\n"
            "{{/if_module_active}}\n"
        )
        with self.assertRaises(TemplateError) as cm:
            render_conditional_blocks(template, active_modules={"diagrams"})
        # The nested-open is on line 3 (1-based).
        self.assertEqual(cm.exception.line_no, 3)
        self.assertIn("Nested", str(cm.exception))

    def test_unmatched_opening_raises(self):
        template = (
            "{{#if_module_active diagrams}}\n"
            "body\n"
        )
        with self.assertRaises(TemplateError) as cm:
            render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertEqual(cm.exception.line_no, 1)
        self.assertIn("never closed", str(cm.exception))

    def test_unmatched_closing_raises(self):
        template = (
            "body\n"
            "{{/if_module_active}}\n"
        )
        with self.assertRaises(TemplateError) as cm:
            render_conditional_blocks(template, active_modules={"diagrams"})
        # The orphan close is on line 2.
        self.assertEqual(cm.exception.line_no, 2)
        self.assertIn("Unmatched closing", str(cm.exception))

    def test_mismatched_kind_open_active_close_inactive(self):
        template = (
            "{{#if_module_active diagrams}}\n"
            "body\n"
            "{{/if_module_inactive}}\n"
        )
        with self.assertRaises(TemplateError) as cm:
            render_conditional_blocks(template, active_modules={"diagrams"})
        # The mismatched close is on line 3.
        self.assertEqual(cm.exception.line_no, 3)
        self.assertIn("Mismatched", str(cm.exception))

    def test_unknown_tag_raises(self):
        # Typo: `if_modul_active` (missing 'e') — won't match canonical
        # patterns, falls into the catch-all and raises with line number.
        template = (
            "before\n"
            "{{#if_modul_active diagrams}}\n"
            "body\n"
            "{{/if_modul_active}}\n"
        )
        with self.assertRaises(TemplateError) as cm:
            render_conditional_blocks(template, active_modules={"diagrams"})
        # The typoed opening tag is on line 2.
        self.assertEqual(cm.exception.line_no, 2)
        msg = str(cm.exception)
        self.assertIn("Unknown", msg)
        self.assertIn("line 2", msg)

    def test_invalid_module_name_uppercase_raises(self):
        # Module name must match [a-z_][a-z0-9_]* — uppercase rejected as
        # an unknown tag (the active regex won't match).
        template = (
            "{{#if_module_active Diagrams}}\n"
            "body\n"
            "{{/if_module_active}}\n"
        )
        with self.assertRaises(TemplateError):
            render_conditional_blocks(template, active_modules={"diagrams"})


class VariableSurvivesActiveBlockTests(unittest.TestCase):
    """A kept block's contents must be passed through unchanged so the
    downstream dict-substitution pass can resolve `{{KEY}}` placeholders."""

    def test_variable_placeholder_inside_active_block_preserved(self):
        template = (
            "{{#if_module_active diagrams}}\n"
            "Project: {{PROJECT_NAME}}\n"
            "Root: {{PROJECT_ROOT}}\n"
            "{{/if_module_active}}\n"
        )
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        # Variables remain as-is for the downstream pass to resolve.
        self.assertIn("Project: {{PROJECT_NAME}}", out)
        self.assertIn("Root: {{PROJECT_ROOT}}", out)


class NoTagTemplatePassThroughTests(unittest.TestCase):
    """Templates with no conditional tags must round-trip unchanged."""

    def test_no_tags_byte_identical_with_trailing_newline(self):
        template = "Plain template\n\nWith multiple lines.\n"
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertEqual(out, template)

    def test_no_tags_byte_identical_without_trailing_newline(self):
        template = "Plain template\n\nWith multiple lines."
        out = render_conditional_blocks(template, active_modules={"diagrams"})
        self.assertEqual(out, template)


class ResolveActiveModulesStubTests(unittest.TestCase):
    """`resolve_active_modules` stub fallback when no DB / no table exists."""

    def test_no_db_returns_defaults(self):
        # Point at a nonexistent DB path; resolver returns defaults.
        result = resolve_active_modules(
            "any-project-id",
            db_path=Path("/nonexistent/path/launcher.db"),
        )
        self.assertEqual(result, set(_DEFAULT_ACTIVE_MODULES))
        # Diagrams must be in the defaults (Phase 1.5 ships it default-on).
        self.assertIn("diagrams", result)

    def test_empty_db_no_project_modules_table_returns_defaults(self):
        # Create an empty SQLite DB with no project_modules table — the
        # resolver probes for the table and falls back to defaults.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE other_table (k TEXT)")
            conn.commit()
            conn.close()

            result = resolve_active_modules("p1", db_path=db)
            self.assertEqual(result, set(_DEFAULT_ACTIVE_MODULES))

    def test_db_with_disabled_diagrams_removes_from_active(self):
        # Real DB with project_modules.diagrams=0 → diagrams NOT active.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("p1", "diagrams", 0),
            )
            conn.commit()
            conn.close()

            result = resolve_active_modules("p1", db_path=db)
            self.assertNotIn("diagrams", result)

    def test_db_with_enabled_extra_module_adds_to_active(self):
        # A module not in defaults but enabled=1 in the DB → added.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("p1", "rl_retrieval", 1),
            )
            conn.commit()
            conn.close()

            result = resolve_active_modules("p1", db_path=db)
            self.assertIn("rl_retrieval", result)
            # Default-on modules still present.
            self.assertIn("diagrams", result)

    def test_different_project_unaffected_by_other_project_rows(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            # p1 disables diagrams; querying p2 must NOT inherit that.
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("p1", "diagrams", 0),
            )
            conn.commit()
            conn.close()

            result = resolve_active_modules("p2", db_path=db)
            self.assertIn("diagrams", result)


class ModuleGatewayDeliveryKeyTests(unittest.TestCase):
    """v0.2.96 WP-10 — the delivery gate's KEY must be the UUID, not the folder.

    ``project_modules`` rows are keyed by the projects-table UUID
    (``commands/projects_v2.rs`` ``Uuid::new_v4()``), while bundle-time
    rendering historically resolved modules with ``str(folder)`` — a key
    that can never match a UUID row. The survey (LANE-WP10-MODULE-SURVEY,
    §2) established the divergence from code paths only; these tests pin
    it on the wire. A delivery gate built on the folder key would be green
    in every test that forgot to register the project and dead in
    production for every project that IS registered.
    """

    UUID = "0f1e2d3c-4b5a-6978-8976-a5b4c3d2e1f0"
    OTHER_UUID = "99999999-1111-2222-3333-444444444444"

    def setUp(self):
        self._saved_db_override = os.environ.get("VCT_LAUNCHER_DB_PATH")
        self.tmp = tempfile.TemporaryDirectory(prefix="vct-wp10-keys-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "project"
        self.folder.mkdir()

    def tearDown(self):
        # Restore the conftest-managed env exactly as we found it.
        if self._saved_db_override is None:
            os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        else:
            os.environ["VCT_LAUNCHER_DB_PATH"] = self._saved_db_override

    def _fixture_db(
        self, *, enabled: int, project_id: str = UUID,
        register_folder: "Path | None" = None,
    ) -> Path:
        """Real-schema launcher.db with one project_modules row.

        Built through ``tests/common/launcher_db_fixture`` (the ONE home —
        no hand-rolled ``CREATE TABLE``), so the schema these tests resolve
        against is the schema the launcher actually writes.
        """
        from tests.common.launcher_db_fixture import insert_rows, make_launcher_db

        projects = []
        if register_folder is not None:
            projects.append({
                "project_id": project_id,
                "name": "Proj",
                "folder_path": register_folder,
            })
        db = make_launcher_db(Path(self.tmp.name), projects=projects)
        insert_rows(db, "project_modules", [{
            "project_id": project_id,
            "module_name": "model_gateway",
            "enabled": enabled,
        }])
        return db

    def test_folder_keyed_lookup_misses_the_uuid_keyed_row(self):
        """RED-PROOF (i), permanent form: ``str(folder)`` never matches a
        UUID-keyed ``project_modules`` row, while the UUID does. This is
        the divergence the survey flagged UNVERIFIED — now on the wire."""
        from vco_lib.module_gated_delivery import resolve_project_id_for_folder

        db = self._fixture_db(enabled=1, register_folder=self.folder)

        self.assertIn(
            "model_gateway",
            resolve_active_modules(self.UUID, db_path=db),
        )
        self.assertNotIn(
            "model_gateway",
            resolve_active_modules(str(self.folder), db_path=db),
        )
        # The resolver the gate uses: folder in, UUID out.
        self.assertEqual(
            resolve_project_id_for_folder(self.folder, db_path=db),
            self.UUID,
        )

    def test_the_gate_resolves_folder_to_uuid_and_fires(self):
        """The gate as built resolves folder→UUID BEFORE the active-check,
        so a registered project with the module enabled gets its agents."""
        from vco_lib.module_gated_delivery import module_gateway_agents_active

        db = self._fixture_db(enabled=1, register_folder=self.folder)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db)

        self.assertTrue(module_gateway_agents_active(self.folder))

    def test_module_disabled_row_leaves_the_gate_closed(self):
        """enabled=0 is an explicit opt-OUT: no delivery, by the same row."""
        from vco_lib.module_gated_delivery import module_gateway_agents_active

        db = self._fixture_db(enabled=0, register_folder=self.folder)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db)

        self.assertFalse(module_gateway_agents_active(self.folder))

    def test_unregistered_folder_is_not_active(self):
        """Conservative direction: a module row for SOMEONE ELSE's project
        must never deliver agents into this folder."""
        from vco_lib.module_gated_delivery import module_gateway_agents_active

        db = self._fixture_db(enabled=1, project_id=self.OTHER_UUID)
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db)

        self.assertFalse(module_gateway_agents_active(self.folder))

    def test_missing_db_is_not_active(self):
        """CLI-only install (no launcher.db): the gate fails toward NOT
        shipping — no ``claude-gw/*`` ids on an install that never opted
        in through the launcher."""
        from vco_lib.module_gated_delivery import module_gateway_agents_active

        os.environ["VCT_LAUNCHER_DB_PATH"] = str(
            Path(self.tmp.name) / "absent" / "launcher.db"
        )

        self.assertFalse(module_gateway_agents_active(self.folder))


class GatewayModuleNameCrossFilePins(unittest.TestCase):
    """WP-10 review MINOR-1: ``GATEWAY_MODULE_NAME`` had NO test importing
    it — a rename of the GUI's ``ROUTING_GUIDANCE_MODULE`` would silently
    kill delivery with zero red tests (the GUI-contract pin covers only the
    TS↔template pair). These pins close the triangle: Python ↔ TS ↔
    template gate must all name the same module id."""

    _REPO = Path(__file__).resolve().parent.parent

    def test_python_constant_matches_the_gui_constant(self) -> None:
        from vco_lib.module_gated_delivery import GATEWAY_MODULE_NAME
        ts = (self._REPO / "launcher" / "src" / "lib" / "api"
              / "model_gateway.ts").read_text(encoding="utf-8")
        needle = "export const ROUTING_GUIDANCE_MODULE = "
        self.assertIn(needle, ts)
        literal = ts.split(needle, 1)[1].split("\n", 1)[0].strip().rstrip(";")
        self.assertEqual(
            literal, f"'{GATEWAY_MODULE_NAME}'",
            "the GUI toggle and the Python delivery gate must write/read "
            "the same project_modules row — a rename on either side "
            "silences delivery",
        )

    def test_python_constant_matches_the_template_gate(self) -> None:
        from vco_lib.module_gated_delivery import GATEWAY_MODULE_NAME
        tpl = (self._REPO / "templates" / "CLAUDE.md.template"
               ).read_text(encoding="utf-8")
        self.assertIn(
            f"{{{{#if_module_active {GATEWAY_MODULE_NAME}}}}}", tpl,
            "the CLAUDE.md template gate and the delivery gate must key "
            "on the same module id, or guidance and agents diverge",
        )


class ModuleGatewayUnreadableDbLegTests(unittest.TestCase):
    """WP-10 review MINOR-2: the enumeration-level unreadable-DB leg was
    probed by the reviewer (chmod-000 / corrupt bytes / table-absent all
    close the gate) but had NO pin. A later "helpful" fallback that opens
    the gate when the DB cannot be read would ship silently."""

    def _gate_closed(self, db_path: Path) -> None:
        from vco_lib.module_gated_delivery import module_gateway_agents_active
        # The gate reads the DB via the VCT_LAUNCHER_DB_PATH env channel
        # (it does not expose a db_path kwarg — the env is its one input).
        saved = os.environ.get("VCT_LAUNCHER_DB_PATH")
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db_path)
        try:
            self.assertFalse(
                module_gateway_agents_active(self.folder),
                "an unreadable DB must close the delivery gate "
                "(conservative miss), never open it",
            )
        finally:
            if saved is None:
                os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
            else:
                os.environ["VCT_LAUNCHER_DB_PATH"] = saved

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vct-wp10-legd-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "project"
        self.folder.mkdir()

    def test_permission_denied_db_closes_the_gate(self):
        db = Path(self.tmp.name) / "launcher.db"
        db.write_bytes(b"x" * 64)
        db.chmod(0o000)
        self.addCleanup(db.chmod, 0o600)
        self._gate_closed(db)

    def test_corrupt_db_closes_the_gate(self):
        db = Path(self.tmp.name) / "launcher.db"
        db.write_bytes(b"definitely not sqlite" * 8)
        self._gate_closed(db)

    def test_missing_table_db_closes_the_gate(self):
        from tests.common.launcher_db_fixture import make_launcher_db
        db = make_launcher_db(Path(self.tmp.name), projects=[])
        # Table-absent: drop the module table entirely.
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("DROP TABLE IF EXISTS project_modules")
        con.commit()
        con.close()
        self._gate_closed(db)


# v0.2.96 (ship-gate F-N4): the run guard is LAST. Two classes below it
# (`GatewayModuleNameCrossFilePins`, `ModuleGatewayUnreadableDbLegTests`)
# were collected by pytest — the gate — but silently absent under a direct
# `python tests/test_conditional_template.py`, which is how someone
# debugging one of them would run it.
if __name__ == "__main__":
    unittest.main()
