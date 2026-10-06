# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for V52-M pre/post hooks + outcome event recognition (v0.2.52).

V52-M adds three new hooks:

  - templates/hooks/pre-bash-context-inject.{sh,ps1}
    PreToolUse(Bash). v0.2.101: a THIN WRAPPER around
    hook_context_router.py — the 500-char threshold was RETIRED (§C1,
    classification replaces it). For every READ/EDIT/SEARCH-classified
    command it mints a task_id, writes a state file at
    .claude/state/bash_task_<sess>_<hash>.json (now carrying intent/
    targets/symbols) and emits the pre_bash outcome event; MECHANICAL
    commands get nothing. Injection is the router's envelope.

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
  - settings.json wiring — dispatcher-era (v0.2.101): pre-bash-context-
    inject stays a DIRECT PreToolUse registration; the two post-* outcome
    producers fire THROUGH the single async post-tool-use-async
    dispatcher's routing table (the eight individual async PostToolUse
    registrations were merged to kill transcript bloat)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOK_NAMES = [
    "pre-bash-context-inject",
    "post-bash-context-record",
    "post-edit-outcome",
    # v0.2.101 §C3: the Write surface's own thin wrapper (new this cycle).
    "pre-write-context-inject",
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


class ClassificationGatePreBash(unittest.TestCase):
    """The pre-bash pairing gate is the router's INTENT classification.

    v0.2.101 §C1 (owner-approved): the user-locked Q6 500-char threshold
    (VCT_BASH_KG_THRESHOLD_CHARS) is RETIRED — READ/EDIT/SEARCH-classified
    commands get the state file + pre_bash outcome event regardless of
    length (MORE events, richer labels — WP-D 2); MECHANICAL commands get
    nothing regardless of length. The end-to-end RL pins live in
    tests/test_v02101_rl_continuity.py; this class keeps the v52m-side
    pairing-contract rows (state name/shape) on the classification gate.
    """

    def setUp(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        from tests.test_v02101_router_surfaces import Rig

        self._tmpd = tempfile.TemporaryDirectory(prefix="v52m_classgate_")
        self.addCleanup(self._tmpd.cleanup)
        self.rig = Rig(Path(self._tmpd.name))
        self.state_dir = self.rig.proj / ".claude" / "state"

    def _run_hook(self, command: str, session: str = "test_session_v52m"):
        from tests.test_v02101_router_surfaces import _bash_payload

        return self.rig.run(
            "pre-bash-context-inject",
            _bash_payload(command, session=session, cwd=str(self.rig.proj)),
        )

    def test_mechanical_command_does_not_create_state_file(self) -> None:
        """The OLD short-command row, restated for the new gate: `echo` is
        MECHANICAL at ANY length → no state file."""
        cmd = "echo " + ("x" * 50)
        r = self._run_hook(cmd)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(list(self.state_dir.glob("bash_task_*.json")), [])

    def test_long_mechanical_command_still_creates_no_state(self) -> None:
        """The threshold is gone: a 600-char `echo` is still MECHANICAL —
        length no longer buys a pairing event (the survey's noise class)."""
        cmd = "echo " + ("x" * 600)
        r = self._run_hook(cmd)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            list(self.state_dir.glob("bash_task_*.json")), [],
            "a long MECHANICAL command must NOT write a state file",
        )

    def test_classified_command_creates_state_with_pairing_fields(self) -> None:
        """A SHORT classified command (far under the old 500-char gate) now
        gets the full pairing treatment — the recall side of §C1."""
        cmd = "grep -rn vco_seen_add templates/"
        r = self._run_hook(cmd)
        self.assertEqual(r.returncode, 0)
        state_files = list(
            self.state_dir.glob("bash_task_test_session_v52m_*.json"))
        self.assertEqual(len(state_files), 1, f"found: {state_files}")
        state = json.loads(state_files[0].read_text())
        self.assertIn("task_id", state)
        self.assertTrue(state["task_id"].startswith("pre_bash_"))
        self.assertIn("start_ts_ms", state)
        self.assertIsInstance(state["start_ts_ms"], int)
        self.assertGreater(state["start_ts_ms"], 0)
        self.assertEqual(state["session_id"], "test_session_v52m")
        self.assertEqual(state["cmd_len"], len(cmd))
        # v0.2.101 WP-D 2 additions (additive — post-bash pairing unchanged)
        self.assertEqual(state["intent"], "SEARCH")
        self.assertIn("vco_seen_add", state["symbols"])
        # the pairing hash is still md5(command)[:16]
        import hashlib
        want = hashlib.md5(cmd.encode()).hexdigest()[:16]
        self.assertEqual(state["cmd_hash"], want)
        self.assertTrue(state_files[0].name.endswith(f"_{want}.json"))

    def test_threshold_knob_is_retired(self) -> None:
        """VCT_BASH_KG_THRESHOLD_CHARS must not be READ by the hook any more
        (a documented knob that changes nothing is the same defect one layer
        down — the docs row was retired with it)."""
        for ext in (".sh", ".ps1"):
            body = (HOOK_DIR / f"pre-bash-context-inject{ext}").read_text(
                encoding="utf-8")
            executable = [
                ln for ln in body.splitlines()
                if not ln.lstrip().startswith(("#", "<#"))
            ]
            self.assertNotIn("VCT_BASH_KG_THRESHOLD_CHARS", "\n".join(executable),
                             f"the threshold knob crept back into the {ext} wrapper")

    def test_non_bash_tool_is_skipped(self) -> None:
        """A tool_name other than Bash must short-circuit before any work."""
        result = self.rig.run("pre-bash-context-inject", {
            "tool_name": "Edit",  # NOT Bash
            "session_id": "test_session",
            "tool_input": {"command": "x" * 1000},
        })
        self.assertEqual(result.returncode, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "non-Bash tool_name must short-circuit without creating state",
        )

    def test_disable_hooks_short_circuits(self) -> None:
        """VCT_DISABLE_HOOKS=1 disables the hook entirely."""
        result = self.rig.run(
            "pre-bash-context-inject",
            {
                "tool_name": "Bash",
                "session_id": "test_session",
                "tool_input": {"command": "grep -rn vco_seen_add templates/"},
            },
            env_overrides={"VCT_DISABLE_HOOKS": "1"},
        )
        self.assertEqual(result.returncode, 0)
        state_files = list(self.state_dir.glob("bash_task_*.json"))
        self.assertEqual(
            state_files, [],
            "VCT_DISABLE_HOOKS=1 must fully short-circuit pre-bash",
        )
        self.assertEqual(self.rig.kg_records(), [])
        self.assertEqual(self.rig.cg_records(), [])


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
    """The V52-M hooks must be wired on both OSes — dispatcher-era form.

    v0.2.101 UPDATE (promise kept, mechanism moved): the two PostToolUse
    outcome producers (``post-bash-context-record``, ``post-edit-outcome``)
    are no longer REGISTERED individually — the eight async PostToolUse
    registrations merged into ONE async dispatcher registration
    (``post-tool-use-async.{sh,ps1}``, matcher ``*``) that routes by
    tool_name to the same unchanged scripts, killing the per-tool-call
    ``async_hook_response`` transcript bloat. The RL outcome-event pair
    (pre_bash producer → bash_outcome / edit_outcome recorder, allowed
    event types pinned in OutcomeEventTypeParity above and in
    ``launcher/src-tauri/vct-hub/src/rl_events_api.rs``) therefore still
    fires on every Bash / Edit / Write — THROUGH the dispatcher. These
    tests assert the dispatcher-era wiring; the full derived routing
    coverage (every retired stem must have a route row) lives in
    ``tests/test_v02101_async_posttooluse_dispatcher.py``.
    ``pre-bash-context-inject`` remains a DIRECT PreToolUse registration
    (it injects additionalContext in-turn; it was never async).
    """

    #: The dispatcher's routing-table rows for the V52-M outcome pair
    #: (line fingerprints — the table is the one declaration both
    #: siblings mirror byte-for-byte).
    DISPATCHER_ROWS_SH = (
        "Bash|post-bash-context-record|-",
        "Edit|post-edit-outcome|-",
        "Write|post-edit-outcome|-",
    )
    DISPATCHER_ROWS_PS1 = DISPATCHER_ROWS_SH  # table is extension-less

    def _dispatcher_text(self, ext: str) -> str:
        p = REPO_ROOT / "templates" / "hooks" / f"post-tool-use-async.{ext}"
        self.assertTrue(p.is_file(), f"missing dispatcher: {p}")
        return p.read_text(encoding="utf-8", errors="replace")

    def _async_posttooluse(self, template: str) -> list:
        p = REPO_ROOT / "templates" / f"settings.json.{template}.template"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return [
            (group.get("matcher", ""), h)
            for group in cfg["hooks"]["PostToolUse"]
            for h in group.get("hooks", [])
            if h.get("async")
        ]

    def test_linux_template_registers_three_hooks(self) -> None:
        """pre-bash DIRECTLY; the two post-* outcome producers THROUGH the
        dispatcher's routing table (dispatcher-era form of this pin)."""
        p = REPO_ROOT / "templates" / "settings.json.linux.template"
        self.assertTrue(p.is_file(), f"missing: {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn(".claude/hooks/pre-bash-context-inject.sh", text)
        self.assertIn(".claude/hooks/post-tool-use-async.sh", text)
        dispatcher = self._dispatcher_text("sh")
        # The producers are wired through the dispatcher's route table.
        for row in self.DISPATCHER_ROWS_SH:
            self.assertIn(row, dispatcher,
                          f"dispatcher route row missing: {row}")
        # And NOT registered directly under PostToolUse any more (a direct
        # registration beside the dispatcher would double-fire the event).
        for group_matcher, h in self._posttooluse_all("linux"):
            self.assertNotIn("post-bash-context-record.sh", h.get("command", ""))
            self.assertNotIn("post-edit-outcome.sh", h.get("command", ""))
        # Validate JSON well-formedness
        json.loads(text)

    def _posttooluse_all(self, template: str) -> list:
        p = REPO_ROOT / "templates" / f"settings.json.{template}.template"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return [
            (group.get("matcher", ""), h)
            for group in cfg["hooks"]["PostToolUse"]
            for h in group.get("hooks", [])
        ]

    def test_windows_template_registers_three_hooks(self) -> None:
        p = REPO_ROOT / "templates" / "settings.json.windows.template"
        self.assertTrue(p.is_file(), f"missing: {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn("pre-bash-context-inject.ps1", text)
        self.assertIn("post-tool-use-async.ps1", text)
        dispatcher = self._dispatcher_text("ps1")
        for row in self.DISPATCHER_ROWS_PS1:
            self.assertIn(row, dispatcher,
                          f".ps1 dispatcher route row missing: {row}")
        for _matcher, h in self._posttooluse_all("windows"):
            self.assertNotIn("post-bash-context-record.ps1", h.get("command", ""))
            self.assertNotIn("post-edit-outcome.ps1", h.get("command", ""))
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
        """Dispatcher-era form: the async PostToolUse registration is the
        dispatcher on matcher ``*`` (it fires for Bash too), and its route
        table sends Bash to post-bash-context-record."""
        async_regs = self._async_posttooluse("linux")
        self.assertEqual(
            len(async_regs), 1,
            "PostToolUse must carry exactly ONE async registration (the "
            f"merged dispatcher); found {async_regs}",
        )
        matcher, hook = async_regs[0]
        self.assertEqual(matcher, "*")
        self.assertIn("post-tool-use-async.sh", hook.get("command", ""))
        self.assertIn("Bash|post-bash-context-record|-", self._dispatcher_text("sh"),
                      "the bash_outcome producer must be routed for Bash")

    def test_post_edit_outcome_runs_on_edit_or_write(self) -> None:
        """Dispatcher-era form: the route table fires post-edit-outcome for
        BOTH Edit and Write (the retired registration's Edit|Write matcher).
        """
        dispatcher = self._dispatcher_text("sh")
        self.assertIn("Edit|post-edit-outcome|-", dispatcher)
        self.assertIn("Write|post-edit-outcome|-", dispatcher)
        ps1 = self._dispatcher_text("ps1")
        self.assertIn("Edit|post-edit-outcome|-", ps1)
        self.assertIn("Write|post-edit-outcome|-", ps1)


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
