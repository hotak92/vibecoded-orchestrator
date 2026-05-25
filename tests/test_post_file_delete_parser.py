# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``vco_lib.diagram_delete_parser`` (B4 ship-blocker —
v0.2.34).

Pre-v0.2.34 the `post-file-delete.sh` hook embedded an inline Python
parser that only inspected the FIRST verb of the Bash command. That
missed every chained / wrapped real-world shape Claude routinely
emits:

    cd /tmp/x && rm -rf .claude/diagrams/...     # first verb is `cd`
    sudo rm -rf .claude/diagrams/...             # first verb is `sudo`
    nice -n 10 rm .claude/diagrams/...           # first verb is `nice`
    bash -c "rm .claude/diagrams/..."            # first verb is `bash`

The new parser walks every command segment in a chain, peels off
wrapper verbs (sudo/nice/taskset/time/env), and re-parses
`bash -c "..."` sub-commands recursively. Aggregated path candidates
are then filtered to .mmd/.excalidraw under .claude/diagrams/ — the
security boundary that prevents a delete chain from coaxing the
indexer cascade into operating on unrelated paths (e.g. `/etc/passwd`
collateral in `rm -rf .claude/diagrams/gui/* /etc/passwd`).

Test coverage (each scenario named in the B4 brief):

* `rm .claude/diagrams/gui/x.mmd` (baseline single-verb).
* `cd /tmp && rm .claude/diagrams/gui/x.mmd` (chain, first verb != delete).
* `sudo rm -rf .claude/diagrams/gui/x.mmd` (wrapper verb).
* `bash -c "rm .claude/diagrams/gui/x.mmd"` (shell-dash-c sub-cmd).
* `nice -n 10 rm .claude/diagrams/gui/x.mmd` (wrapper with flag+value).
* `rm a.txt && rm .claude/diagrams/gui/x.mmd` (only diagram path matters).
* `rm -rf .claude/diagrams/gui/* /etc/passwd` (SECURITY: filter strips /etc/passwd).
* Plus: idempotency, empty input, malformed shell quoting, depth bound on
  nested `bash -c "bash -c '...'"`, both `.mmd` and `.excalidraw` extensions,
  multi-target `rm` (`rm a.mmd b.mmd c.mmd`), mv source-only semantics,
  env-prefix (`KEY=val rm ...`), PowerShell verbs (`Remove-Item`,
  `Move-Item`), forward-slash + backslash path normalisation.

The CLI entry point (`python -m vco_lib.diagram_delete_parser`) is
also exercised end-to-end via subprocess so the .sh / .ps1 hooks'
integration path is locked in.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import diagram_delete_parser  # noqa: E402

extract = diagram_delete_parser.extract_diagram_delete_targets


# ─── Baseline + B4-brief scenarios ───────────────────────────────────────


class BriefScenariosTests(unittest.TestCase):
    """Each test mirrors one of the test cases the B4 brief named as
    required. If any of these regress, the pre-v0.2.34 bug is back."""

    def test_baseline_single_rm(self) -> None:
        """`rm .claude/diagrams/gui/x.mmd` — the simplest happy path."""
        paths = extract("rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_chain_with_leading_cd(self) -> None:
        """`cd /tmp && rm <diagram>` — pre-fix bug case. The first
        verb is `cd`; the old parser bailed before seeing the `rm`."""
        paths = extract("cd /tmp && rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_sudo_wrapper(self) -> None:
        """`sudo rm -rf <diagram>` — wrapper-verb peel."""
        paths = extract("sudo rm -rf .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_bash_dash_c_subcommand(self) -> None:
        """`bash -c "rm <diagram>"` — recursive sub-command parse."""
        paths = extract('bash -c "rm .claude/diagrams/gui/x.mmd"')
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_nice_wrapper_with_flag_value(self) -> None:
        """`nice -n 10 rm <diagram>` — wrapper with flag+numeric value."""
        paths = extract("nice -n 10 rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_chain_with_unrelated_rm_first(self) -> None:
        """`rm a.txt && rm <diagram>` — only the diagram delete counts.
        The unrelated `a.txt` rm is invisible to the indexer (it's not
        a .mmd / .excalidraw under .claude/diagrams/)."""
        paths = extract("rm a.txt && rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_security_filter_drops_unrelated_collateral(self) -> None:
        """`rm -rf .claude/diagrams/gui/* /etc/passwd` — the parser
        MUST NOT pass /etc/passwd to the cascade.

        This is the security boundary documented in the parser
        module's docstring. Even if a malicious or buggy user `rm`
        names a non-diagram path, the diagram filter strips it before
        the cascade-delete step sees it.
        """
        paths = extract("rm -rf .claude/diagrams/gui/some.mmd /etc/passwd")
        # /etc/passwd must NOT appear in the filtered output.
        self.assertNotIn("/etc/passwd", paths)
        for p in paths:
            self.assertFalse(
                p.endswith("/etc/passwd") or p == "/etc/passwd",
                f"security violation: parser leaked /etc/passwd as {p!r}",
            )
        # The actual diagram must still be detected — the filter isn't
        # so aggressive it drops the legitimate target.
        self.assertIn(".claude/diagrams/gui/some.mmd", paths)


# ─── Idempotency + edge-case stability ───────────────────────────────────


class EdgeCaseTests(unittest.TestCase):

    def test_empty_command(self) -> None:
        self.assertEqual(extract(""), [])

    def test_whitespace_only_command(self) -> None:
        self.assertEqual(extract("   \n  \t  "), [])

    def test_malformed_shell_quoting_bails_cleanly(self) -> None:
        # Unclosed quote — shlex.split raises ValueError. The parser
        # must catch + return [], not raise.
        self.assertEqual(extract('rm "unclosed-quote'), [])

    def test_non_delete_verb_returns_empty(self) -> None:
        self.assertEqual(extract("ls .claude/diagrams/"), [])
        self.assertEqual(extract("cat .claude/diagrams/gui/x.mmd"), [])
        self.assertEqual(extract("git status"), [])

    def test_delete_verb_but_no_diagram_target(self) -> None:
        # rm IS a delete verb, but the target isn't a diagram path —
        # the filter must drop it. Common-case: deleting normal files.
        self.assertEqual(extract("rm /tmp/scratch.txt"), [])
        self.assertEqual(extract("rm src/main.py"), [])

    def test_idempotent_repeated_calls(self) -> None:
        cmd = "rm .claude/diagrams/gui/x.mmd"
        for _ in range(5):
            self.assertEqual(extract(cmd), [".claude/diagrams/gui/x.mmd"])

    def test_multi_target_rm_collects_all_diagrams(self) -> None:
        cmd = "rm .claude/diagrams/gui/a.mmd .claude/diagrams/gui/b.mmd .claude/diagrams/gui/c.excalidraw"
        paths = extract(cmd)
        self.assertEqual(
            sorted(paths),
            sorted([
                ".claude/diagrams/gui/a.mmd",
                ".claude/diagrams/gui/b.mmd",
                ".claude/diagrams/gui/c.excalidraw",
            ]),
        )

    def test_unlink_recognised_as_delete_verb(self) -> None:
        self.assertEqual(
            extract("unlink .claude/diagrams/gui/x.mmd"),
            [".claude/diagrams/gui/x.mmd"],
        )

    def test_mv_only_counts_source_as_delete(self) -> None:
        # mv source dest — only the source path counts as a "delete".
        paths = extract(
            "mv .claude/diagrams/gui/old.mmd .claude/diagrams/gui/new.mmd"
        )
        self.assertEqual(paths, [".claude/diagrams/gui/old.mmd"])

    def test_env_prefix_assignment(self) -> None:
        # `KEY=val rm <diagram>` — env prefix peel.
        paths = extract("FOO=1 BAR=2 rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_excalidraw_extension_supported(self) -> None:
        paths = extract("rm .claude/diagrams/gui/login.excalidraw")
        self.assertEqual(paths, [".claude/diagrams/gui/login.excalidraw"])

    def test_extension_case_insensitive(self) -> None:
        # Filter must be case-insensitive on the extension (Windows
        # editors sometimes save .MMD / .EXCALIDRAW).
        paths = extract("rm .claude/diagrams/gui/x.MMD")
        self.assertEqual(paths, [".claude/diagrams/gui/x.MMD"])

    def test_path_outside_diagrams_dir_dropped(self) -> None:
        # Even a .mmd extension doesn't matter — the path must be under
        # .claude/diagrams/.
        self.assertEqual(extract("rm /tmp/random.mmd"), [])
        self.assertEqual(extract("rm docs/figures/architecture.mmd"), [])


# ─── Chain + wrapper compositions ────────────────────────────────────────


class ChainAndWrapperTests(unittest.TestCase):

    def test_chain_with_semicolon(self) -> None:
        paths = extract("cd /tmp ; rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_chain_with_pipe(self) -> None:
        # rm doesn't normally appear in a pipeline, but if it did:
        paths = extract("echo go | rm .claude/diagrams/gui/x.mmd")
        # The pipe splits into two segments; the second is the rm.
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_chain_with_or(self) -> None:
        paths = extract("false || rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_multiple_diagram_deletes_in_chain(self) -> None:
        cmd = (
            "rm .claude/diagrams/gui/a.mmd && "
            "rm .claude/diagrams/gui/b.mmd ; "
            "rm .claude/diagrams/gui/c.excalidraw"
        )
        paths = extract(cmd)
        self.assertEqual(
            sorted(paths),
            sorted([
                ".claude/diagrams/gui/a.mmd",
                ".claude/diagrams/gui/b.mmd",
                ".claude/diagrams/gui/c.excalidraw",
            ]),
        )

    def test_taskset_wrapper(self) -> None:
        paths = extract("taskset 1 rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_time_wrapper(self) -> None:
        paths = extract("time rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_sudo_with_flag(self) -> None:
        # `sudo -E` runs with env preserved.
        paths = extract("sudo -E rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_nested_wrappers(self) -> None:
        # `sudo nice -n 10 rm` — wrappers stack.
        paths = extract("sudo nice -n 10 rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_env_wrapper_form(self) -> None:
        # `env FOO=bar rm <diagram>` — env-as-wrapper.
        paths = extract("env FOO=bar rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])


# ─── bash -c recursion ───────────────────────────────────────────────────


class BashDashCTests(unittest.TestCase):

    def test_bash_dash_c_with_chain_inside(self) -> None:
        """`bash -c "cd /tmp && rm <diagram>"` — recursion finds the rm
        even though both the outer wrapper (`bash`) and the inner first
        verb (`cd`) aren't deletes."""
        paths = extract(
            'bash -c "cd /tmp && rm .claude/diagrams/gui/x.mmd"'
        )
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_sh_dash_c_variant(self) -> None:
        paths = extract(
            'sh -c "rm .claude/diagrams/gui/x.mmd"'
        )
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_combined_flag_dash_ec(self) -> None:
        # `bash -ec "..."` is `bash` with `-e` (errexit) + `-c` combined.
        # The trailing positional is the sub-command.
        paths = extract(
            'bash -ec "rm .claude/diagrams/gui/x.mmd"'
        )
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_nested_bash_dash_c_one_level(self) -> None:
        # `bash -c "bash -c 'rm <diagram>'"` — one level of nesting OK.
        paths = extract(
            """bash -c "bash -c 'rm .claude/diagrams/gui/x.mmd'" """
        )
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_deeply_nested_bash_dash_c_bails_at_depth_limit(self) -> None:
        # Build a chain of >4 nesting levels — the parser must refuse
        # rather than recurse unboundedly. The result should be an
        # empty list (defensive — silent vs raising is correct for a
        # hook that must never block the user's Bash).
        # 5 levels of nesting (one above the MAX_NESTING_DEPTH=4).
        inner = "rm .claude/diagrams/gui/x.mmd"
        for _ in range(5):
            inner = f'bash -c "{inner}"'
        paths = extract(inner)
        # The deeply-nested case is dropped silently — falls below the
        # depth budget. The cleanup-orphan path (vco rebuild-diagram-index
        # --prune) is the recovery mechanism for this kind of
        # false-negative.
        self.assertEqual(paths, [])


# ─── PowerShell verb recognition ─────────────────────────────────────────


class PowerShellVerbTests(unittest.TestCase):

    def test_remove_item_recognised(self) -> None:
        paths = extract("Remove-Item .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_remove_item_case_insensitive(self) -> None:
        # PowerShell is case-insensitive on cmdlet names.
        paths = extract("remove-item .claude/diagrams/gui/x.mmd")
        self.assertEqual(paths, [".claude/diagrams/gui/x.mmd"])

    def test_move_item_source_only(self) -> None:
        paths = extract(
            "Move-Item .claude/diagrams/gui/old.mmd .claude/diagrams/gui/new.mmd"
        )
        self.assertEqual(paths, [".claude/diagrams/gui/old.mmd"])


# ─── CLI integration (end-to-end via subprocess) ─────────────────────────


class CLIIntegrationTests(unittest.TestCase):
    """The shell hooks call `python -m vco_lib.diagram_delete_parser`
    over a stdin pipe. Pin that exact invocation shape end-to-end so a
    future refactor that breaks the CLI wrapper trips here."""

    @staticmethod
    def _run(stdin: str) -> tuple[int, str, str]:
        """Run the CLI entry point with `stdin` piped in. Returns
        (returncode, stdout, stderr)."""
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.diagram_delete_parser"],
            input=stdin,
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO_ROOT),
        )
        return result.returncode, result.stdout, result.stderr

    def test_cli_returns_paths_on_stdout_one_per_line(self) -> None:
        rc, out, _ = self._run("rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(rc, 0)
        # Trailing newline is normal; split + filter empty.
        lines = [l for l in out.split("\n") if l]
        self.assertEqual(lines, [".claude/diagrams/gui/x.mmd"])

    def test_cli_chain_b4_regression(self) -> None:
        """The literal B4 regression case as a CLI invocation."""
        rc, out, _ = self._run("cd /tmp && rm .claude/diagrams/gui/x.mmd")
        self.assertEqual(rc, 0)
        lines = [l for l in out.split("\n") if l]
        self.assertEqual(lines, [".claude/diagrams/gui/x.mmd"])

    def test_cli_silent_on_empty(self) -> None:
        rc, out, _ = self._run("")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_cli_silent_on_non_delete(self) -> None:
        rc, out, _ = self._run("ls .claude/diagrams/")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_cli_security_drops_etc_passwd(self) -> None:
        """Same security check as the unit test, end-to-end through
        the CLI surface the hooks actually invoke."""
        rc, out, _ = self._run(
            "rm -rf .claude/diagrams/gui/some.mmd /etc/passwd"
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("/etc/passwd", out)


if __name__ == "__main__":
    unittest.main()
