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
    """v0.2.46 post-adversarial F1 refactor: each .sh hook now SOURCES
    the shared `_lib/resolve-vco-venv.sh` helper instead of inlining its
    own resolver. The helper carries the canonical 3-tier precedence
    ($VCT_VENV → $VCT_INSTALL_ROOT/.venv → clone-relative). These tests
    now verify (a) each hook sources the helper, (b) the helper itself
    encodes the dual-layout (top-level + legacy claude_mcp_servers).
    """

    def test_sh_hooks_reference_all_three_candidates(self) -> None:
        # Pre-refactor: each hook had inline references to all three
        # candidates. Post-refactor: hooks delegate to the shared helper.
        # We verify each hook SOURCES the helper, and the helper itself
        # references all three candidates.
        helper_path = REPO_ROOT / "templates/hooks/_lib/resolve-vco-venv.sh"
        self.assertTrue(helper_path.is_file(),
                        f"shared helper missing: {helper_path}")
        helper_text = helper_path.read_text(encoding="utf-8")

        with self.subTest(component="helper"):
            self.assertIn("VCT_VENV", helper_text,
                          f"{helper_path}: missing VCT_VENV override")
            self.assertRegex(
                helper_text,
                r"/\.venv(/bin/python|/Scripts/python\.exe|\b)",
                f"{helper_path}: missing top-level .venv candidate",
            )
            self.assertIn(
                "claude_mcp_servers/.venv",
                helper_text,
                f"{helper_path}: missing legacy claude_mcp_servers/.venv candidate",
            )

        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.sh"
                self.assertTrue(path.exists(), f"missing hook: {path}")
                text = path.read_text(encoding="utf-8")
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertIn(
                        "resolve-vco-venv.sh",
                        text,
                        f"{path}: doesn't source the shared venv resolver "
                        f"(_lib/resolve-vco-venv.sh)",
                    )

    def test_sh_hooks_check_vct_venv_before_layout_candidates(self) -> None:
        """v0.2.46 post-adversarial F1: VCT_VENV-before-fallback ordering
        now lives in the shared helper. Verify it there once instead of
        repeating the check across each hook."""
        helper_path = REPO_ROOT / "templates/hooks/_lib/resolve-vco-venv.sh"
        text = helper_path.read_text(encoding="utf-8")
        non_comment_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        non_comment = "\n".join(non_comment_lines)

        vct_pos = non_comment.find("VCT_VENV")
        modern_pos = non_comment.find(".venv")
        # The first .venv match might be inside "VCT_VENV" itself — skip past.
        if modern_pos != -1 and modern_pos == vct_pos + 4:
            modern_pos = non_comment.find(".venv", modern_pos + 5)
        self.assertGreater(vct_pos, -1,
                           f"{helper_path}: VCT_VENV not in non-comment body")
        self.assertLess(
            vct_pos, modern_pos,
            f"{helper_path}: VCT_VENV must be checked before layout fallback",
        )


class HookHasDualLayoutResolutionPs1(unittest.TestCase):
    """v0.2.46 post-adversarial F1: PowerShell-side mirror of the
    helper-sourcing contract. Each .ps1 hook dot-sources
    `_lib/resolve-vco-venv.ps1`, and the helper itself has the
    canonical dual-layout references.
    """

    def test_ps1_hooks_reference_all_three_candidates(self) -> None:
        helper_path = REPO_ROOT / "templates/hooks/_lib/resolve-vco-venv.ps1"
        self.assertTrue(helper_path.is_file(),
                        f"shared PS helper missing: {helper_path}")
        helper_text = helper_path.read_text(encoding="utf-8")

        with self.subTest(component="helper"):
            self.assertIn("VCT_VENV", helper_text,
                          f"{helper_path}: missing VCT_VENV override")
            self.assertRegex(
                helper_text,
                r'(?:["\s])\.venv["\\/]?',
                f"{helper_path}: missing top-level .venv candidate",
            )
            # PS uses Join-Path with separated string args, so
            # `claude_mcp_servers` and `.venv` may not be joined by a literal
            # slash in the source. Loosen the regex to allow either form.
            self.assertRegex(
                helper_text,
                r"claude_mcp_servers",
                f"{helper_path}: missing legacy claude_mcp_servers candidate",
            )

        for layout_dir in LAYOUT_DIRS:
            for hook in HOOKS_UNDER_TEST:
                path = REPO_ROOT / layout_dir / f"{hook}.ps1"
                self.assertTrue(path.exists(), f"missing hook: {path}")
                text = path.read_text(encoding="utf-8")
                with self.subTest(hook=hook, layout=layout_dir):
                    self.assertIn(
                        "resolve-vco-venv.ps1",
                        text,
                        f"{path}: doesn't dot-source the shared venv resolver "
                        f"(_lib/resolve-vco-venv.ps1)",
                    )


class TopLevelVenvIsPreferredOverLegacy(unittest.TestCase):
    """v0.2.46 post-adversarial F1: top-level-vs-legacy precedence now
    lives in the shared `_lib/resolve-vco-venv.{sh,ps1}` helpers. Verify
    the ordering there once; the hooks delegate.
    """

    def test_sh_hooks_top_level_listed_before_legacy(self) -> None:
        # Refactor target: the shared bash helper, not each hook.
        helper_path = REPO_ROOT / "templates/hooks/_lib/resolve-vco-venv.sh"
        text = helper_path.read_text(encoding="utf-8")
        non_comment_text = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        top_match = re.search(r'[\$/]\w*[\.\$\w_-]*/\.venv', non_comment_text)
        legacy_match = re.search(r'claude_mcp_servers/\.venv', non_comment_text)
        self.assertIsNotNone(top_match, f"{helper_path}: no top-level .venv path")
        self.assertIsNotNone(legacy_match, f"{helper_path}: no legacy path")
        self.assertLess(
            top_match.start(), legacy_match.start(),
            f"{helper_path}: top-level .venv must be listed before "
            f"claude_mcp_servers/.venv (resolution priority)",
        )

    def test_ps1_hooks_top_level_listed_before_legacy(self) -> None:
        helper_path = REPO_ROOT / "templates/hooks/_lib/resolve-vco-venv.ps1"
        text = helper_path.read_text(encoding="utf-8")
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
            preceding = non_comment_text[max(0, start - 64):start]
            # The PS Join-Path idiom puts `claude_mcp_servers` upstream of
            # `.venv` (in a chained Join-Path call), not directly adjacent.
            # 64 chars of preceding context covers that span.
            if "claude_mcp_servers" in preceding:
                continue
            top_match = m
            break
        # Loosened: PS Join-Path puts `claude_mcp_servers` and `.venv` as
        # separate string args. We only care that BOTH appear in order.
        legacy_match = re.search(r'claude_mcp_servers', non_comment_text)
        self.assertIsNotNone(top_match, f"{helper_path}: no top-level candidate")
        self.assertIsNotNone(legacy_match, f"{helper_path}: no legacy candidate")
        self.assertLess(
            top_match.start(), legacy_match.start(),
            f"{helper_path}: top-level must come before legacy",
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
