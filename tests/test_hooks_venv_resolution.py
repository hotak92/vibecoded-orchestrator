# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for dual-layout venv resolution in PR-25 hooks (v0.2.12).

Background
----------
Three hooks previously hardcoded the venv at
`<repo_root>/claude_mcp_servers/.venv`:

  - templates/hooks/code-graph-incremental.{sh,ps1}
  - templates/hooks/kg-summary-generator.{sh,ps1}
  - templates/hooks/pre-edit-context-inject.{sh,ps1}

Modern installs put the venv at `<repo_root>/.venv` (top-level). The old
logic silently fell through to system python (no `weaviate-client`
installed) and crashed inside the hook's subprocess — silently breaking
code-graph + KG-summary functionality on modern installs.

PR-25 makes each hook check in priority order:

  1. `$VCT_VENV` env override
  2. `<repo_root>/.venv` (modern top-level layout)
  3. `<repo_root>/claude_mcp_servers/.venv` (legacy layout)
  4. Graceful fallback (empty / null — caller short-circuits)

These tests assert that the resolution priority is present in each hook's
source text and that each hook tolerates the "no venv anywhere" case
without exiting non-zero (soft-fail contract — hooks must NOT block the
host Edit tool just because the venv is missing).

We test against the shipped source files in ``templates/hooks/``.

Note (PR-39, v0.2.12, 2026-05-16): before PR-39 the public repo also
shipped ``.claude/hooks/`` byte-identical with ``templates/hooks/`` —
the second entry in ``LAYOUT_DIRS`` covered that mirror. PR-39 deleted
the duplicate; install.py now renders ``.claude/hooks/`` from
``templates/hooks/`` at install time. Templates are the sole source of
truth, so this test now exercises only the template layout.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOKS_UNDER_TEST = [
    "code-graph-incremental",
    "kg-summary-generator",
    "pre-edit-context-inject",
]

LAYOUT_DIRS = ["templates/hooks"]


class HookHasDualLayoutResolutionSh(unittest.TestCase):
    """Each .sh hook must include all three candidate paths in priority
    order: $VCT_VENV → top-level .venv → claude_mcp_servers/.venv."""

    def test_sh_hooks_reference_all_three_candidates(self) -> None:
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.sh"
                self.assertTrue(path.exists(), f"missing hook: {path}")
                text = path.read_text(encoding="utf-8")
                with self.subTest(hook=hook, layout=layout_dir):
                    # All three candidates must appear somewhere in the file.
                    self.assertIn("VCT_VENV", text,
                                  f"{path}: missing VCT_VENV override")
                    self.assertRegex(
                        text,
                        r"/\.venv(/bin/python|/Scripts/python\.exe|\b)",
                        f"{path}: missing top-level .venv candidate",
                    )
                    self.assertIn(
                        "claude_mcp_servers/.venv",
                        text,
                        f"{path}: missing legacy claude_mcp_servers/.venv candidate",
                    )

    def test_sh_hooks_check_vct_venv_before_layout_candidates(self) -> None:
        """VCT_VENV must be checked BEFORE either layout candidate so the
        explicit override always wins."""
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.sh"
                text = path.read_text(encoding="utf-8")
                # Position of first VCT_VENV reference (excluding the comment block).
                # We strip comment-only lines containing "PR-25" markers first.
                non_comment_lines = []
                for line in text.splitlines():
                    if line.lstrip().startswith("#"):
                        continue
                    non_comment_lines.append(line)
                non_comment = "\n".join(non_comment_lines)

                vct_pos = non_comment.find("VCT_VENV")
                modern_pos = non_comment.find(".venv")
                # The first .venv match might be inside "VCT_VENV" itself —
                # skip past it to find a "real" .venv path reference.
                if modern_pos != -1 and modern_pos == vct_pos + 4:
                    # ".venv" inside "VCT_VENV/.venv" — search past
                    modern_pos = non_comment.find(".venv", modern_pos + 5)
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertGreater(vct_pos, -1,
                                       f"{path}: VCT_VENV not in non-comment body")
                    # Both must appear and VCT_VENV must come first.
                    self.assertLess(
                        vct_pos, modern_pos,
                        f"{path}: VCT_VENV must be checked before layout fallback",
                    )


class HookHasDualLayoutResolutionPs1(unittest.TestCase):
    """Each .ps1 hook must include all three candidate paths."""

    def test_ps1_hooks_reference_all_three_candidates(self) -> None:
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.ps1"
                self.assertTrue(path.exists(), f"missing hook: {path}")
                text = path.read_text(encoding="utf-8")
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertIn("VCT_VENV", text,
                                  f"{path}: missing VCT_VENV override")
                    # PS uses both forward and back slashes; match either.
                    # The top-level candidate may appear as:
                    #   Join-Path $DefaultRepoRoot ".venv"
                    #   Join-Path $VenvBase ".venv\Scripts\python.exe"
                    #   $DefaultRepoRoot/.venv
                    # — match any of these forms.
                    self.assertRegex(
                        text,
                        r'(?:["\s])\.venv["\\/]?',
                        f"{path}: missing top-level .venv candidate",
                    )
                    self.assertRegex(
                        text,
                        r"claude_mcp_servers[\\/]\.venv",
                        f"{path}: missing legacy candidate",
                    )


class TopLevelVenvIsPreferredOverLegacy(unittest.TestCase):
    """When both candidates exist, the top-level `.venv` must be tried FIRST.
    Verified by checking the textual ordering of the two candidate paths
    in each hook (the resolution scans top-down).
    """

    def test_sh_hooks_top_level_listed_before_legacy(self) -> None:
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.sh"
                text = path.read_text(encoding="utf-8")
                # Find first non-comment occurrence of each. Some hooks list
                # them inside a for-loop with explicit ordering; others use
                # an if/elif chain — both forms preserve the priority.
                non_comment_text = "\n".join(
                    line for line in text.splitlines()
                    if not line.lstrip().startswith("#")
                )
                top_match = re.search(
                    r'[\$/]\w*[\.\$\w_-]*/\.venv', non_comment_text
                )
                legacy_match = re.search(
                    r'claude_mcp_servers/\.venv', non_comment_text
                )
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertIsNotNone(top_match, f"{path}: no top-level .venv path")
                    self.assertIsNotNone(legacy_match, f"{path}: no legacy path")
                    self.assertLess(
                        top_match.start(), legacy_match.start(),
                        f"{path}: top-level .venv must be listed before "
                        f"claude_mcp_servers/.venv (resolution priority)",
                    )

    def test_ps1_hooks_top_level_listed_before_legacy(self) -> None:
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.ps1"
                text = path.read_text(encoding="utf-8")
                non_comment_text = "\n".join(
                    line for line in text.splitlines()
                    if not line.lstrip().startswith("#")
                )
                # Look for the top-level .venv candidate. PowerShell idioms
                # used: Join-Path $base ".venv", Join-Path $base
                # ".venv\Scripts\...", etc. The marker is `".venv` (after a
                # quote or whitespace) NOT preceded by `claude_mcp_servers`.
                # We find all occurrences of `.venv` then drop the legacy
                # ones (where `claude_mcp_servers/` precedes immediately).
                top_match = None
                for m in re.finditer(r'\.venv', non_comment_text):
                    start = m.start()
                    preceding = non_comment_text[max(0, start - 24):start]
                    if "claude_mcp_servers" in preceding:
                        continue
                    top_match = m
                    break
                legacy_match = re.search(
                    r'claude_mcp_servers[\\/]\.venv', non_comment_text
                )
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertIsNotNone(top_match, f"{path}: no top-level candidate")
                    self.assertIsNotNone(legacy_match, f"{path}: no legacy candidate")
                    self.assertLess(
                        top_match.start(), legacy_match.start(),
                        f"{path}: top-level must come before legacy",
                    )


class HooksAreSyntacticallyValid(unittest.TestCase):
    """Static syntax check on each .sh hook (bash -n). No external deps
    invoked; just parses the script."""

    def test_sh_hooks_parse_cleanly(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.sh"
                result = subprocess.run(
                    [bash, "-n", str(path)],
                    capture_output=True, text=True, check=False,
                )
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertEqual(
                        result.returncode, 0,
                        f"{path}: bash -n failed:\n{result.stderr}",
                    )


class NoVenvPresentSoftFailContract(unittest.TestCase):
    """When neither layout's venv exists and VCT_VENV is unset, the hooks
    MUST exit 0 (not crash) — the soft-fail contract guarantees that a
    broken venv never blocks the host Edit tool.

    We exercise this end-to-end by running the hook against a temp
    directory with no `.venv` anywhere. The hook reads JSON from stdin;
    we feed a minimal `Edit` payload."""

    def _run_hook_with_no_venv(
        self, hook_relpath: str, stdin_json: str
    ) -> subprocess.CompletedProcess:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            # Build a minimal project skeleton with the hook at the
            # expected location (.claude/hooks/<file>) and the helper
            # _lib/ symlinks copied so the hook can source them.
            project = Path(tmpdir) / "fakeproj"
            hooks_dest = project / ".claude" / "hooks"
            hooks_dest.mkdir(parents=True)
            # Copy _lib subdir (the hooks `source` files from there).
            # PR-39 (v0.2.12): templates/ is the single source of truth.
            # Before PR-39 this read from .claude/hooks/_lib (byte-identical
            # mirror); the duplicate was removed when install.py started
            # rendering .claude/ from templates/ at install time.
            src_lib = REPO_ROOT / "templates" / "hooks" / "_lib"
            if src_lib.exists():
                shutil.copytree(src_lib, hooks_dest / "_lib")
            # Copy the hook under test.
            src_hook = REPO_ROOT / hook_relpath
            shutil.copy2(src_hook, hooks_dest / src_hook.name)
            # Also copy any sibling helper scripts the hook references
            # (detect-project.sh under templates/scripts/, formerly
            # mirrored at .claude/scripts/ before PR-39).
            src_scripts = REPO_ROOT / "templates" / "scripts"
            scripts_dest = project / ".claude" / "scripts"
            scripts_dest.mkdir(parents=True, exist_ok=True)
            for helper in ("detect-project.sh",):
                src_helper = src_scripts / helper
                if src_helper.exists():
                    shutil.copy2(src_helper, scripts_dest / helper)
            # Scrub any inherited VCT_VENV / VCT_INSTALL_ROOT.
            env = os.environ.copy()
            env.pop("VCT_VENV", None)
            env.pop("VCT_INSTALL_ROOT", None)
            env["CLAUDE_PROJECT_DIR"] = str(project)
            return subprocess.run(
                [bash, str(hooks_dest / src_hook.name)],
                input=stdin_json,
                env=env,
                capture_output=True, text=True, check=False,
                cwd=str(project),
                timeout=10,
            )

    def test_kg_summary_generator_no_venv_exits_zero(self) -> None:
        payload = (
            '{"tool_name":"Edit","tool_input":{"file_path":"knowledge/x.md"},'
            '"session_id":"t"}'
        )
        cp = self._run_hook_with_no_venv(
            "templates/hooks/kg-summary-generator.sh", payload
        )
        self.assertEqual(
            cp.returncode, 0,
            f"hook crashed: stderr={cp.stderr!r}",
        )

    def test_code_graph_incremental_no_venv_exits_zero(self) -> None:
        # This hook takes positional args, not stdin JSON. Pass a
        # made-up code file as arg 1.
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "fakeproj"
            hooks_dest = project / ".claude" / "hooks"
            hooks_dest.mkdir(parents=True)
            # PR-39 (v0.2.12): templates/ is the single source of truth.
            # Before PR-39 this read from .claude/hooks/_lib (byte-identical
            # mirror); the duplicate was removed when install.py started
            # rendering .claude/ from templates/ at install time.
            src_lib = REPO_ROOT / "templates" / "hooks" / "_lib"
            if src_lib.exists():
                shutil.copytree(src_lib, hooks_dest / "_lib")
            src_hook = REPO_ROOT / "templates/hooks/code-graph-incremental.sh"
            shutil.copy2(src_hook, hooks_dest / src_hook.name)
            env = os.environ.copy()
            env.pop("VCT_VENV", None)
            env.pop("VCT_INSTALL_ROOT", None)
            (project / "test.py").write_text("# noop\n")
            cp = subprocess.run(
                [bash, str(hooks_dest / src_hook.name), "test.py", str(project)],
                env=env,
                capture_output=True, text=True, check=False,
                cwd=str(project),
                timeout=10,
            )
            self.assertEqual(
                cp.returncode, 0,
                f"hook crashed: stderr={cp.stderr!r}",
            )


class VctVenvOverrideAcceptedWhenSet(unittest.TestCase):
    """When `VCT_VENV` is set to a real (Python-bearing) directory, the
    hooks must prefer it over either layout candidate. We can't easily
    verify which interpreter the hook USED without instrumentation, but
    we can verify that the env override doesn't crash the resolution
    logic — i.e. the hook still exits 0."""

    def test_kg_summary_generator_with_vct_venv_set(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "fakeproj"
            hooks_dest = project / ".claude" / "hooks"
            hooks_dest.mkdir(parents=True)
            # PR-39 (v0.2.12): templates/ is the single source of truth.
            # Before PR-39 this read from .claude/hooks/_lib (byte-identical
            # mirror); the duplicate was removed when install.py started
            # rendering .claude/ from templates/ at install time.
            src_lib = REPO_ROOT / "templates" / "hooks" / "_lib"
            if src_lib.exists():
                shutil.copytree(src_lib, hooks_dest / "_lib")
            src_hook = REPO_ROOT / "templates/hooks/kg-summary-generator.sh"
            shutil.copy2(src_hook, hooks_dest / src_hook.name)

            # Build a fake VCT_VENV with `bin/python` symlinked to the
            # current interpreter so the hook treats it as valid.
            fake_venv = project / "custom-venv"
            (fake_venv / "bin").mkdir(parents=True)
            fake_py = fake_venv / "bin" / "python"
            os.symlink(sys.executable, fake_py)

            env = os.environ.copy()
            env["VCT_VENV"] = str(fake_venv)
            env.pop("VCT_INSTALL_ROOT", None)
            env["CLAUDE_PROJECT_DIR"] = str(project)
            payload = (
                '{"tool_name":"Edit","tool_input":{"file_path":"knowledge/x.md"},'
                '"session_id":"t"}'
            )
            cp = subprocess.run(
                [bash, str(hooks_dest / src_hook.name)],
                input=payload, env=env,
                capture_output=True, text=True, check=False,
                cwd=str(project),
                timeout=10,
            )
            self.assertEqual(
                cp.returncode, 0,
                f"hook crashed with VCT_VENV set: stderr={cp.stderr!r}",
            )


if __name__ == "__main__":
    unittest.main()
