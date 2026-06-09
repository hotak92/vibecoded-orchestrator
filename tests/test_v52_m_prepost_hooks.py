# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for V52-M pre/post hooks + outcome event recognition (v0.2.52).

V52-M adds three new hooks:

  - templates/hooks/pre-bash-context-inject.{sh,ps1}
    PreToolUse(Bash). Fires when command length > 500 chars. Mints a
    task_id, writes a state file at .claude/state/bash_task_<sess>_<hash>.json,
    runs rl_kg_search.py with the command as query, injects results as
    additionalContext.

  - templates/hooks/post-bash-context-record.{sh,ps1}
    PostToolUse(Bash). Re-derives cmd_hash from stdin, reads the state
    file, emits a bash_outcome event via outcome_emit, deletes the
    state file.

  - templates/hooks/post-edit-outcome.{sh,ps1}
    PostToolUse(Edit|Write). Emits an edit_outcome event with diff_size +
    file_existed_before. Pairs by (session_id, file_path, ts_window).

Plus backend:

  - claude_mcp_servers/rl_client/outcome_emit.py — emit_outcome_event()
    helper for the new event types.
  - launcher/src-tauri/vct-hub/src/rl_events_api.rs — extended event_type
    validation gate to accept bash_outcome / edit_outcome / pre_bash.

These tests cover:
  - File existence + .sh/.ps1 sibling parity
  - bash -n syntax check on every .sh
  - 500-char threshold logic via simulated invocation
  - task_id pairing via state file
  - Cross-language parity: OUTCOME_EVENT_TYPES (Python) matches
    allowed_event_types (Rust source string-grep)
  - settings.json registers the three new hooks
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOK_NAMES = [
    "pre-bash-context-inject",
    "post-bash-context-record",
    "post-edit-outcome",
]

HOOK_DIR = REPO_ROOT / "templates" / "hooks"


class HookFilesExistAndHaveSiblings(unittest.TestCase):
    """Every .sh must have a matching .ps1 sibling.

    See feedback_multi_os_sibling_check_at_pr_time: the hook-os-parity
    CI gate covers templates/hooks/*.sh ↔ .ps1 — these tests are a
    pre-PR fast-fail before CI catches it.
    """

    def test_sh_files_exist(self) -> None:
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.sh"
            self.assertTrue(p.is_file(), f"missing .sh hook: {p}")

    def test_ps1_siblings_exist(self) -> None:
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.ps1"
            self.assertTrue(
                p.is_file(),
                f"missing .ps1 sibling for {name}: {p} "
                "(multi-OS sibling discipline per feedback_multi_os_sibling_check_at_pr_time)",
            )

    def test_files_are_non_empty(self) -> None:
        for name in HOOK_NAMES:
            for ext in (".sh", ".ps1"):
                p = HOOK_DIR / f"{name}{ext}"
                self.assertGreater(
                    p.stat().st_size, 100,
                    f"{p} suspiciously small ({p.stat().st_size} bytes)",
                )


class BashSyntaxCheck(unittest.TestCase):
    """bash -n parses each .sh hook without errors."""

    def test_all_sh_pass_bash_n(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.sh"
            with self.subTest(hook=name):
                result = subprocess.run(
                    [bash, "-n", str(p)],
                    capture_output=True, text=True,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"{p}: bash -n failed:\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}",
                )


class ThresholdLogicPreBash(unittest.TestCase):
    """The pre-bash-context-inject hook fires only when command length >500.

    Spec (user-locked Q6 2026-06-09): fixed 500-char threshold with
    VCT_BASH_KG_THRESHOLD_CHARS env override.
    """

    def setUp(self) -> None:
        self.hook = HOOK_DIR / "pre-bash-context-inject.sh"
        self.tmp = tempfile.mkdtemp(prefix="v52m_threshold_")
        self.state_dir = Path(self.tmp) / ".claude" / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, command: str, threshold_env: str | None = None) -> tuple[int, str, str]:
        """Invoke the hook with a synthesized stdin JSON payload."""
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        stdin_payload = json.dumps({
            "tool_name": "Bash",
            "session_id": "test_session_v52m",
            "tool_input": {"command": command},
        })
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.tmp
        env["VCT_DISABLE_HOOKS"] = ""  # explicit unset
        if "VCT_DISABLE_HOOKS" in env and not env["VCT_DISABLE_HOOKS"]:
            del env["VCT_DISABLE_HOOKS"]
        if threshold_env is not None:
            env["VCT_BASH_KG_THRESHOLD_CHARS"] = threshold_env
        result = subprocess.run(
            [bash, str(self.hook)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        return result.returncode, result.stdout, result.stderr

    def test_short_command_does_not_create_state_file(self) -> None:
        """100-char command (well below 500) → no state file written."""
        cmd = "echo " + ("x" * 50)  # ~55 chars total
        rc, out, err = self._run_hook(cmd)
        self.assertEqual(rc, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "short command (<500 chars) must NOT write a state file; "
            f"found: {state_files}",
        )

    def test_long_command_creates_state_file(self) -> None:
        """600-char command (>500) → state file written with task_id + start_ts_ms."""
        cmd = "echo " + ("x" * 600)  # 605 chars total
        rc, out, err = self._run_hook(cmd)
        self.assertEqual(rc, 0)
        state_files = list(self.state_dir.glob("bash_task_test_session_v52m_*.json"))
        self.assertEqual(
            len(state_files), 1,
            f"long command (>500 chars) must write exactly one state file; "
            f"found: {state_files}",
        )
        state = json.loads(state_files[0].read_text())
        self.assertIn("task_id", state)
        self.assertTrue(state["task_id"].startswith("pre_bash_"))
        self.assertIn("start_ts_ms", state)
        self.assertIsInstance(state["start_ts_ms"], int)
        self.assertGreater(state["start_ts_ms"], 0)
        self.assertEqual(state["session_id"], "test_session_v52m")
        self.assertGreaterEqual(state["cmd_len"], 600)

    def test_env_override_lowers_threshold(self) -> None:
        """Setting VCT_BASH_KG_THRESHOLD_CHARS=10 fires on a 50-char command."""
        cmd = "echo " + ("x" * 50)
        rc, out, err = self._run_hook(cmd, threshold_env="10")
        self.assertEqual(rc, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            len(state_files), 1,
            "override threshold=10 must let a 55-char command create state",
        )

    def test_env_override_raises_threshold(self) -> None:
        """Setting VCT_BASH_KG_THRESHOLD_CHARS=10000 silences a 600-char command."""
        cmd = "echo " + ("x" * 600)
        rc, out, err = self._run_hook(cmd, threshold_env="10000")
        self.assertEqual(rc, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "override threshold=10000 must suppress a 605-char command",
        )

    def test_non_bash_tool_is_skipped(self) -> None:
        """A tool_name other than Bash must short-circuit before any work."""
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        stdin_payload = json.dumps({
            "tool_name": "Edit",  # NOT Bash
            "session_id": "test_session",
            "tool_input": {"command": "x" * 1000},
        })
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.tmp
        result = subprocess.run(
            [bash, str(self.hook)],
            input=stdin_payload, capture_output=True, text=True,
            env=env, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "non-Bash tool_name must short-circuit without creating state",
        )

    def test_disable_hooks_short_circuits(self) -> None:
        """VCT_DISABLE_HOOKS=1 disables the hook entirely."""
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        stdin_payload = json.dumps({
            "tool_name": "Bash",
            "session_id": "test_session",
            "tool_input": {"command": "x" * 1000},
        })
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.tmp
        env["VCT_DISABLE_HOOKS"] = "1"
        result = subprocess.run(
            [bash, str(self.hook)],
            input=stdin_payload, capture_output=True, text=True,
            env=env, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "VCT_DISABLE_HOOKS=1 must fully short-circuit pre-bash",
        )


class TaskIdPairingViaStateFile(unittest.TestCase):
    """pre-bash writes a state file; post-bash re-derives the same path
    from its own stdin and reads it — the hash function must be identical.
    """

    def test_md5_hash_function_matches(self) -> None:
        """The md5 truncation must be the SAME 16-char prefix in both hooks.

        Implementation: both hooks use python hashlib.md5 → .hexdigest()[:16].
        The grep below catches if either hook drifts to a different scheme.
        """
        for ext in (".sh",):  # ps1 uses .NET MD5; covered by separate grep below
            for name in ("pre-bash-context-inject", "post-bash-context-record"):
                p = HOOK_DIR / f"{name}{ext}"
                text = p.read_text(encoding="utf-8")
                self.assertIn(
                    "hashlib.md5", text,
                    f"{p}: must use hashlib.md5 for cross-hook cmd_hash parity",
                )
                self.assertIn(
                    "[:16]", text,
                    f"{p}: must truncate md5 to first 16 chars (matches pre-bash)",
                )

    def test_ps1_uses_dot_net_md5_substring_16(self) -> None:
        """PowerShell siblings use .NET MD5 → Substring(0,16) — same 16-char prefix."""
        for name in ("pre-bash-context-inject", "post-bash-context-record"):
            p = HOOK_DIR / f"{name}.ps1"
            text = p.read_text(encoding="utf-8")
            self.assertIn(
                "MD5", text,
                f"{p}: must reference MD5 hashing for cmd_hash parity",
            )
            # PS1 uses Substring(0, 16) or similar — assert the literal 16 appears
            # alongside Substring (catches drift to a different prefix length).
            self.assertRegex(
                text, r"Substring\(0,\s*16\)",
                f"{p}: must truncate MD5 to first 16 chars (matches .sh sibling)",
            )

    def test_state_file_path_template_matches(self) -> None:
        """Both hooks must construct the same state file path:
        `.claude/state/bash_task_<session>_<cmdhash>.json`.
        """
        for name in ("pre-bash-context-inject", "post-bash-context-record"):
            p_sh = HOOK_DIR / f"{name}.sh"
            text_sh = p_sh.read_text(encoding="utf-8")
            self.assertIn(
                "bash_task_${SESSION_ID}_${CMD_HASH}.json", text_sh,
                f"{p_sh}: state file path template drift",
            )
            p_ps = HOOK_DIR / f"{name}.ps1"
            text_ps = p_ps.read_text(encoding="utf-8")
            self.assertIn(
                'bash_task_${SessionId}_${CmdHash}.json', text_ps,
                f"{p_ps}: state file path template drift (PS1)",
            )


class OutcomeEventTypeParity(unittest.TestCase):
    """OUTCOME_EVENT_TYPES tuple (Python) must match the allowed_event_types
    list (Rust) at the hub's POST /api/v1/rl/events gate. Cross-language
    drift here would silently drop outcome events at the hub.
    """

    def test_python_recognizes_three_outcome_types(self) -> None:
        py_path = REPO_ROOT / "claude_mcp_servers" / "rl_client" / "outcome_emit.py"
        self.assertTrue(py_path.is_file(), f"missing: {py_path}")
        text = py_path.read_text(encoding="utf-8")
        self.assertIn('"bash_outcome"', text)
        self.assertIn('"edit_outcome"', text)
        self.assertIn('"pre_bash"', text)

    def test_rust_gate_accepts_three_outcome_types(self) -> None:
        rs_path = REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "rl_events_api.rs"
        self.assertTrue(rs_path.is_file(), f"missing: {rs_path}")
        text = rs_path.read_text(encoding="utf-8")
        self.assertIn('"bash_outcome"', text)
        self.assertIn('"edit_outcome"', text)
        self.assertIn('"pre_bash"', text)
        # The original retrieval / citation must still be accepted (no regression).
        self.assertIn('"retrieval"', text)
        self.assertIn('"citation"', text)


class OutcomeEmitModuleSurface(unittest.TestCase):
    """The new outcome_emit module exposes the expected public API."""

    def test_module_is_importable_offline(self) -> None:
        """No top-level imports require a running hub / Weaviate."""
        py_path = REPO_ROOT / "claude_mcp_servers" / "rl_client" / "outcome_emit.py"
        text = py_path.read_text(encoding="utf-8")
        # The hub_writer + telemetry_emit imports must be inside functions
        # (lazy), so a fresh process can `import claude_mcp_servers.rl_client.outcome_emit`
        # without standing up the hub.
        # Verify no top-level (column-0) `from claude_mcp_servers...` import.
        top_level_imports = re.findall(
            r"^from claude_mcp_servers\..*import",
            text, flags=re.MULTILINE,
        )
        self.assertEqual(
            top_level_imports, [],
            "outcome_emit.py must lazy-import all server-side helpers "
            "(top-level claude_mcp_servers imports break the hook subprocess "
            f"that calls this module): {top_level_imports}",
        )

    def test_module_exposes_emit_outcome_event(self) -> None:
        py_path = REPO_ROOT / "claude_mcp_servers" / "rl_client" / "outcome_emit.py"
        text = py_path.read_text(encoding="utf-8")
        self.assertIn("def emit_outcome_event(", text)
        self.assertIn("OUTCOME_EVENT_TYPES", text)


class SettingsTemplatesRegisterNewHooks(unittest.TestCase):
    """templates/settings.json.{linux,windows}.template must register the
    three new hooks at the correct PreToolUse / PostToolUse positions.
    """

    def test_linux_template_registers_three_hooks(self) -> None:
        p = REPO_ROOT / "templates" / "settings.json.linux.template"
        self.assertTrue(p.is_file(), f"missing: {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn(".claude/hooks/pre-bash-context-inject.sh", text)
        self.assertIn(".claude/hooks/post-bash-context-record.sh", text)
        self.assertIn(".claude/hooks/post-edit-outcome.sh", text)
        # Validate JSON well-formedness
        json.loads(text)

    def test_windows_template_registers_three_hooks(self) -> None:
        p = REPO_ROOT / "templates" / "settings.json.windows.template"
        self.assertTrue(p.is_file(), f"missing: {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn("pre-bash-context-inject.ps1", text)
        self.assertIn("post-bash-context-record.ps1", text)
        self.assertIn("post-edit-outcome.ps1", text)
        json.loads(text)

    def test_pre_bash_hook_runs_on_bash_matcher(self) -> None:
        """Pre-bash injection must be PreToolUse, matcher=Bash."""
        p = REPO_ROOT / "templates" / "settings.json.linux.template"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        hooks = cfg["hooks"]["PreToolUse"]
        registered = []
        for group in hooks:
            for h in group.get("hooks", []):
                if "pre-bash-context-inject.sh" in h.get("command", ""):
                    registered.append(group.get("matcher", ""))
        self.assertIn(
            "Bash", registered,
            "pre-bash-context-inject.sh must be registered under PreToolUse matcher=Bash",
        )

    def test_post_bash_hook_runs_on_bash_matcher(self) -> None:
        """Post-bash recorder must be PostToolUse, matcher=Bash."""
        p = REPO_ROOT / "templates" / "settings.json.linux.template"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        hooks = cfg["hooks"]["PostToolUse"]
        registered = []
        for group in hooks:
            for h in group.get("hooks", []):
                if "post-bash-context-record.sh" in h.get("command", ""):
                    registered.append(group.get("matcher", ""))
        self.assertIn(
            "Bash", registered,
            "post-bash-context-record.sh must be registered under PostToolUse matcher=Bash",
        )

    def test_post_edit_outcome_runs_on_edit_or_write(self) -> None:
        """Post-edit-outcome must be PostToolUse with Edit|Write matcher."""
        p = REPO_ROOT / "templates" / "settings.json.linux.template"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        hooks = cfg["hooks"]["PostToolUse"]
        registered = []
        for group in hooks:
            for h in group.get("hooks", []):
                if "post-edit-outcome.sh" in h.get("command", ""):
                    registered.append(group.get("matcher", ""))
        # Accept either "Edit|Write" or two separate Edit/Write registrations
        self.assertTrue(
            any("Edit" in m and "Write" in m for m in registered) or
            ("Edit" in registered and "Write" in registered),
            f"post-edit-outcome.sh must run on Edit|Write; found matchers: {registered}",
        )


class HookEnvAndSecurityHygiene(unittest.TestCase):
    """All three .sh hooks scrub sensitive env vars + honor VCT_DISABLE_HOOKS.

    Lifted from feedback_lean_ctx_env_scrub: every hook subprocess must
    pre-emptively `unset` the credential env vars so a misconfigured
    child process can't surface them.
    """

    def test_all_sh_hooks_scrub_credentials(self) -> None:
        sensitive = (
            "SUPABASE_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
        )
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.sh"
            text = p.read_text(encoding="utf-8")
            with self.subTest(hook=name):
                for var in sensitive:
                    self.assertIn(
                        var, text,
                        f"{p}: must scrub {var} via `unset` at the top",
                    )

    def test_all_sh_hooks_honor_vct_disable_hooks(self) -> None:
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.sh"
            text = p.read_text(encoding="utf-8")
            with self.subTest(hook=name):
                self.assertIn(
                    "VCT_DISABLE_HOOKS", text,
                    f"{p}: must check VCT_DISABLE_HOOKS env",
                )


class HookExitCodeContract(unittest.TestCase):
    """Every hook ends with `exit 0` — never blocks the tool flow."""

    def test_sh_hooks_end_with_exit_0(self) -> None:
        for name in HOOK_NAMES:
            p = HOOK_DIR / f"{name}.sh"
            text = p.read_text(encoding="utf-8").rstrip()
            # Last non-empty line should be `exit 0`. We tolerate trailing
            # blank lines but reject any other exit code.
            last_line = [ln for ln in text.splitlines() if ln.strip()][-1]
            with self.subTest(hook=name):
                self.assertEqual(
                    last_line.strip(), "exit 0",
                    f"{p}: must end with `exit 0` (hooks never block); "
                    f"last line was: {last_line!r}",
                )


if __name__ == "__main__":
    unittest.main()
