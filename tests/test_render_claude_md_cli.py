"""Tests for the `re-render-claude-md` CLI subcommand (Phase 1.5.B,
2026-05-25).

Covers:
  - CLI invokes the full render pipeline and writes CLAUDE.md correctly.
  - Module active produces a CLAUDE.md WITH the conditional diagrams
    section.
  - Module inactive produces a CLAUDE.md WITHOUT the diagrams section.
  - The two outputs are visibly different.
  - User content below the managed-region closing marker is preserved
    across a re-render.
  - The CLI emits valid JSON on stdout with `--json`.
  - Stub fallback works (no launcher DB available).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.project_init import (  # noqa: E402
    MANAGED_REGION_OPEN,
    MANAGED_REGION_CLOSE,
    render_claude_md,
)


def _run_cli(args: list[str], *, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the project_init CLI as a subprocess, capturing stdout/stderr.

    Sets `VCT_STATE_DIR` to a value that points at a nonexistent path so
    the resolver's no-DB fallback kicks in (predictable defaults).
    """
    env = os.environ.copy()
    # Force the stub fallback by pointing at a nonexistent state dir.
    env.setdefault("VCT_STATE_DIR", "/nonexistent-vct-state-for-tests")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.project_init", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


class CliRendersClaudeMdTests(unittest.TestCase):
    """End-to-end: `re-render-claude-md` writes a sensible CLAUDE.md."""

    def test_default_modules_active_diagrams_section_present(self):
        # With the stub fallback (no DB), diagrams is default-on, so the
        # rendered CLAUDE.md must contain the Diagrams section.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            result = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder),
                "--project-name", "TestProject",
                "--orchestrator-root", str(REPO_ROOT),
                "--json",
            ])
            self.assertEqual(
                result.returncode, 0,
                f"CLI failed: stderr={result.stderr!r} stdout={result.stdout!r}",
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertIn("diagrams", payload["active_modules"])

            claude_md = (folder / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Diagrams (Mermaid + Excalidraw)", claude_md)
            # Conditional tag lines must NOT leak into the rendered file.
            self.assertNotIn("{{#if_module_active", claude_md)
            self.assertNotIn("{{/if_module_active", claude_md)
            # Managed-region markers must wrap the body.
            self.assertIn(MANAGED_REGION_OPEN, claude_md)
            self.assertIn(MANAGED_REGION_CLOSE, claude_md)
            # PROJECT_NAME placeholder must have been resolved.
            self.assertIn("TestProject", claude_md)
            self.assertNotIn("{{PROJECT_NAME}}", claude_md)

    def test_module_inactive_via_db_diagrams_section_absent(self):
        # Provision a launcher DB where project_modules.diagrams=0 for
        # this project. The rendered CLAUDE.md must NOT contain the
        # Diagrams section.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "project"
            folder.mkdir()
            state_dir = Path(td) / "vct-state"
            state_dir.mkdir()
            db = state_dir / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("test-project", "diagrams", 0),
            )
            conn.commit()
            conn.close()

            result = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder),
                "--project-name", "TestProject",
                "--orchestrator-root", str(REPO_ROOT),
                "--project-id", "test-project",
                "--db-path", str(db),
                "--json",
            ])
            self.assertEqual(
                result.returncode, 0,
                f"CLI failed: stderr={result.stderr!r} stdout={result.stdout!r}",
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertNotIn("diagrams", payload["active_modules"])

            claude_md = (folder / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertNotIn("## Diagrams (Mermaid + Excalidraw)", claude_md)
            # The KG-First Search Policy section (which precedes the
            # conditional block in the template) must still be present.
            self.assertIn("KG-First Search Policy", claude_md)
            # The VCO-Managed Files section (which follows) too.
            self.assertIn("VCO-Managed Files", claude_md)

    def test_active_vs_inactive_outputs_differ(self):
        # Render both ways and confirm they differ in a meaningful way.
        with tempfile.TemporaryDirectory() as td:
            folder_a = Path(td) / "active"
            folder_a.mkdir()
            folder_i = Path(td) / "inactive"
            folder_i.mkdir()

            state_dir = Path(td) / "vct-state"
            state_dir.mkdir()
            db = state_dir / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("inactive-project", "diagrams", 0),
            )
            conn.commit()
            conn.close()

            # Active: uses default-on stub fallback (project_id with no
            # rows → defaults).
            res_a = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder_a),
                "--project-name", "P",
                "--orchestrator-root", str(REPO_ROOT),
                "--project-id", "active-project",
                "--db-path", str(db),
                "--json",
            ])
            self.assertEqual(res_a.returncode, 0)

            res_i = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder_i),
                "--project-name", "P",
                "--orchestrator-root", str(REPO_ROOT),
                "--project-id", "inactive-project",
                "--db-path", str(db),
                "--json",
            ])
            self.assertEqual(res_i.returncode, 0)

            text_a = (folder_a / "CLAUDE.md").read_text(encoding="utf-8")
            text_i = (folder_i / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertNotEqual(text_a, text_i)
            self.assertIn("Diagrams", text_a)
            self.assertNotIn("Diagrams (Mermaid + Excalidraw)", text_i)

    def test_user_content_below_managed_region_preserved_on_re_render(self):
        # Render once, append user content, re-render with a different
        # active-modules set, confirm user content survives.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            # Render 1: default-on (diagrams active).
            res1 = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder),
                "--project-name", "P",
                "--orchestrator-root", str(REPO_ROOT),
                "--json",
            ])
            self.assertEqual(res1.returncode, 0)

            target = folder / "CLAUDE.md"
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(
                    "\n## User custom\n"
                    "this is a user-added section that must survive\n"
                )

            # Render 2: explicitly disable diagrams via DB.
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            db = state_dir / "launcher.db"
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE project_modules "
                "(project_id TEXT, module_name TEXT, enabled INTEGER)"
            )
            conn.execute(
                "INSERT INTO project_modules VALUES (?, ?, ?)",
                ("toggled", "diagrams", 0),
            )
            conn.commit()
            conn.close()

            res2 = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder),
                "--project-name", "P",
                "--orchestrator-root", str(REPO_ROOT),
                "--project-id", "toggled",
                "--db-path", str(db),
                "--json",
            ])
            self.assertEqual(
                res2.returncode, 0,
                f"CLI failed: {res2.stderr!r}",
            )

            final = target.read_text(encoding="utf-8")
            # User content survives.
            self.assertIn("## User custom", final)
            self.assertIn("this is a user-added section that must survive", final)
            # Diagrams section gone.
            self.assertNotIn("## Diagrams (Mermaid + Excalidraw)", final)

    def test_cli_human_prose_mode(self):
        # Without --json, the CLI emits a one-line summary on stderr.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            result = _run_cli([
                "re-render-claude-md",
                "--folder", str(folder),
                "--project-name", "P",
                "--orchestrator-root", str(REPO_ROOT),
            ])
            self.assertEqual(result.returncode, 0)
            # stdout must be empty in human-prose mode.
            self.assertEqual(result.stdout, "")
            # stderr has the summary.
            self.assertIn("re-rendered", result.stderr)
            self.assertIn("active_modules", result.stderr)

    def test_cli_missing_folder_exits_1(self):
        result = _run_cli([
            "re-render-claude-md",
            "--folder", "/nonexistent-folder-for-test",
            "--project-name", "P",
            "--orchestrator-root", str(REPO_ROOT),
            "--json",
        ])
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])


class RenderClaudeMdPythonAPITests(unittest.TestCase):
    """Direct Python API: bypass subprocess for faster iteration."""

    def test_render_returns_expected_keys(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            result = render_claude_md(
                folder,
                orchestrator_root=REPO_ROOT,
                project_name="TestProj",
                db_path=Path("/nonexistent/path/launcher.db"),
            )
            self.assertIn("wrote_path", result)
            self.assertIn("active_modules", result)
            self.assertIn("managed_region_present_before", result)
            self.assertIn("rendered_bytes", result)
            self.assertFalse(result["managed_region_present_before"])

            # Second render: markers now present.
            result2 = render_claude_md(
                folder,
                orchestrator_root=REPO_ROOT,
                project_name="TestProj",
                db_path=Path("/nonexistent/path/launcher.db"),
            )
            self.assertTrue(result2["managed_region_present_before"])

    def test_render_idempotent_same_inputs_byte_identical(self):
        # Re-rendering with the same inputs twice yields byte-identical
        # files (idempotency guarantee for safe automation).
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            render_claude_md(
                folder,
                orchestrator_root=REPO_ROOT,
                project_name="TestProj",
                db_path=Path("/nonexistent/path/launcher.db"),
            )
            after_first = (folder / "CLAUDE.md").read_bytes()
            render_claude_md(
                folder,
                orchestrator_root=REPO_ROOT,
                project_name="TestProj",
                db_path=Path("/nonexistent/path/launcher.db"),
            )
            after_second = (folder / "CLAUDE.md").read_bytes()
            self.assertEqual(after_first, after_second)


if __name__ == "__main__":
    unittest.main()
