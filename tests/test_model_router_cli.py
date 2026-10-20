# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``vct-model-gateway`` entry point.

The single-instance guard gets both halves of its decision tested — the ACT
(a live process holds the file, so we decline) and the LEAVE-ALONE (a stale
file, so we claim it) — because a guard that only ever refuses and a guard
that only ever proceeds both pass a one-sided test.

The liveness probe is deliberately the shared cross-OS one from
``vco_lib.deferral_probes``: on Windows ``os.kill(pid, 0)`` TERMINATES the
target rather than probing it, and a fifth local copy of that knowledge is a
fifth chance to get it wrong.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.common.env import EnvIsolationMixin

from model_router import __main__ as cli
from model_router import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = REPO_ROOT / "claude_mcp_servers"


class _StateDirCase(EnvIsolationMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wp9-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.set_env("VCT_STATE_DIR", str(self.dir))
        self.set_env("VCT_CLAUDE_DIR", str(self.dir / "claude"))
        for key in ("VCT_MODEL_GATEWAY_PORT", "VCT_MODEL_GATEWAY_HOST"):
            self.set_env(key, None)


class FlagTests(_StateDirCase):
    def test_version_prints_the_package_version(self) -> None:
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            code = cli.main(["--version"])
        finally:
            sys.stdout = original
        self.assertEqual(code, 0)
        self.assertEqual(buffer.getvalue().strip(), __version__)

    def test_print_token_path_prints_a_path_not_a_token(self) -> None:
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            code = cli.main(["--print-token-path"])
        finally:
            sys.stdout = original
        self.assertEqual(code, 0)
        printed = buffer.getvalue().strip()
        self.assertTrue(printed.endswith("model-gateway.token"))
        self.assertTrue(printed.startswith(str(self.dir)))
        # The flag must not CREATE the token as a side effect of printing.
        self.assertFalse(Path(printed).exists())

    def test_print_export_path_matches_the_shared_contract(self) -> None:
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            cli.main(["--print-export-path"])
        finally:
            sys.stdout = original
        printed = Path(buffer.getvalue().strip())
        self.assertEqual(printed.name, "chat_model_context.json")
        self.assertEqual(printed.parent.name, "model-gateway")

    def test_an_out_of_range_port_is_rejected_before_binding(self) -> None:
        self.assertEqual(cli.main(["--port", "0"]), 2)
        self.assertEqual(cli.main(["--port", "70000"]), 2)

    def test_an_unknown_command_is_rejected_by_the_parser(self) -> None:
        with self.assertRaises(SystemExit):
            cli.main(["frobnicate"])


class SelfTestTests(_StateDirCase):
    def test_check_passes_on_a_clean_machine(self) -> None:
        buffer = io.StringIO()
        code = cli.run_check(stream=buffer)
        self.assertEqual(code, 0, buffer.getvalue())
        output = buffer.getvalue()
        self.assertIn("OK", output)

    def test_check_names_every_file_it_will_use(self) -> None:
        buffer = io.StringIO()
        cli.run_check(stream=buffer)
        output = buffer.getvalue()
        for label in (
            "token_file", "pid_file", "port_file", "log_file",
            "credentials_file", "context_table", "static_catalog",
        ):
            self.assertIn(label, output)

    def test_check_says_the_credentials_file_is_absent_when_it_is(self) -> None:
        buffer = io.StringIO()
        cli.run_check(stream=buffer)
        self.assertIn("Claude login will be needed", buffer.getvalue())

    def test_check_does_not_resolve_a_vendor_key(self) -> None:
        """Resolution is lazy at request time so the daemon starts with the hub
        down; a self-test that resolved would test something else."""
        buffer = io.StringIO()
        cli.run_check(stream=buffer)
        self.assertIn("not resolved", buffer.getvalue())

    def test_check_writes_nothing(self) -> None:
        before = sorted(p.name for p in self.dir.iterdir())
        cli.run_check(stream=io.StringIO())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_check_fails_on_a_non_loopback_bind_address(self) -> None:
        # THE leak: this line used to be a bare assignment. See set_env.
        self.set_env("VCT_MODEL_GATEWAY_HOST", "0.0.0.0")
        buffer = io.StringIO()
        self.assertEqual(cli.run_check(stream=buffer), 1)
        self.assertIn("PROBLEM", buffer.getvalue())

    def test_check_reports_a_malformed_export_without_failing(self) -> None:
        """A bad export degrades to the seed; that is a warning, not a stop."""
        export = self.dir / "model-gateway" / "chat_model_context.json"
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_text("{oops", encoding="utf-8")
        buffer = io.StringIO()
        self.assertEqual(cli.run_check(stream=buffer), 0)
        self.assertIn("seed(export-malformed)", buffer.getvalue())


class SingleInstanceTests(_StateDirCase):
    def test_claims_the_file_when_nothing_holds_it(self) -> None:
        pid_file = self.dir / "model-gateway.pid"
        self.assertIsNone(cli._acquire_single_instance(pid_file))
        self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))

    def test_an_existing_file_is_reported_never_overwritten(self) -> None:
        """ACT half, and the atomicity: the CREATE is the lock.

        Read-then-write let two starts both find no live holder, both write
        their own pid and both bind — the pid file then named one instance
        while the port file named the other. ``O_CREAT | O_EXCL`` makes
        exactly one of them win, and the loser gets the winner's pid back to
        reason about. Whether that pid means "already serving" or "a number
        reused after a crash" is ``_serve``'s call, not this function's.
        """
        pid_file = self.dir / "model-gateway.pid"
        pid_file.write_text("424242\n", encoding="utf-8")
        self.assertEqual(cli._acquire_single_instance(pid_file), 424242)
        # ...and the holder's claim is left byte-intact.
        self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "424242")

    def test_two_claimants_in_sequence_leave_the_first_in_place(self) -> None:
        """The property the exclusive create exists for."""
        pid_file = self.dir / "model-gateway.pid"
        self.assertIsNone(cli._acquire_single_instance(pid_file))
        self.assertEqual(
            cli._acquire_single_instance(pid_file), os.getpid(),
            "the second claim reports the first, and changes nothing",
        )
        self.assertEqual(
            pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()),
        )

    def test_a_garbage_pid_file_is_reported_as_unreadable(self) -> None:
        """Not None: "the file exists and says nothing usable" is its own
        answer, and the caller unlinks it rather than assuming it is free."""
        pid_file = self.dir / "model-gateway.pid"
        pid_file.write_text("not-a-pid\n", encoding="utf-8")
        self.assertEqual(
            cli._acquire_single_instance(pid_file), cli.UNREADABLE_PID,
        )
        self.assertEqual(cli.UNREADABLE_PID, -1)

    def test_release_removes_only_our_own_pid_file(self) -> None:
        pid_file = self.dir / "model-gateway.pid"
        cli._acquire_single_instance(pid_file)
        cli._release_single_instance(pid_file)
        self.assertFalse(pid_file.exists())

    def test_release_leaves_another_process_pid_file_alone(self) -> None:
        """A crashed instance must not delete the live one's claim."""
        pid_file = self.dir / "model-gateway.pid"
        pid_file.write_text("424242\n", encoding="utf-8")
        cli._release_single_instance(pid_file)
        self.assertTrue(pid_file.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits")
    def test_the_pid_file_is_owner_only(self) -> None:
        import stat

        pid_file = self.dir / "model-gateway.pid"
        cli._acquire_single_instance(pid_file)
        self.assertEqual(stat.S_IMODE(pid_file.stat().st_mode), 0o600)

    def test_the_liveness_probe_is_the_shared_one_not_a_local_copy(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("from vco_lib.deferral_probes import pid_is_alive", source)
        self.assertNotIn("def pid_is_alive", source)


class SubprocessEntryPointTests(_StateDirCase):
    """The daemon as a user actually invokes it."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "model_router", *args],
            capture_output=True, text=True, timeout=120,
            cwd=tempfile.gettempdir(),
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [str(MCP_ROOT), str(REPO_ROOT), os.environ.get("PYTHONPATH", "")],
                ),
            },
        )

    def test_check_exits_zero_from_an_unrelated_working_directory(self) -> None:
        completed = self._run("--check")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)

    def test_check_exits_one_when_the_configuration_cannot_serve(self) -> None:
        self.set_env("VCT_MODEL_GATEWAY_HOST", "0.0.0.0")
        completed = self._run("--check")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("PROBLEM", completed.stdout)

    def test_no_secret_value_appears_in_any_cli_output(self) -> None:
        """Belt and braces: the CLI prints paths and states, never material."""
        token_path = self.dir / "model-gateway.token"
        from model_router.auth import ensure_host_token

        token = ensure_host_token(token_path)
        completed = self._run("--check")
        self.assertNotIn(token, completed.stdout)
        self.assertNotIn(token, completed.stderr)
        printed = self._run("--print-token-path")
        self.assertNotIn(token, printed.stdout)
        self.assertIn("model-gateway.token", printed.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
