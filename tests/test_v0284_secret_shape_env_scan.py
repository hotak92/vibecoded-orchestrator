# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 PLAN-v0284 D6 (P4): key-shape env scan fix.

Pre-.84 `_has_user_secret_shaped_line`'s `.claude/env` branch matched a COARSE
regex (`export\\s+[A-Z_][A-Z0-9_]*="`) that flagged EVERY uppercase managed-block
export — so every safe-add project (23/23 pure-config lines, zero secrets)
re-emitted the `user_secret_values_retained_in_tree` deferral forever, and B-F8's
reconciler recomputed the same false positive so it could never self-clear.

D6: the `.claude/env` branch now routes through the single secret-shape home
`vco_lib.secrets_audit.is_secret_shaped_env_key` AND requires a non-empty quoted
value. settings.json branch is UNCHANGED.

FAIL-WITHOUT-FIX PIN: a config-only managed block ⇒ no deferral, and an existing
on-disk `user_secret_values_retained_in_tree` entry clears via the reconciler.
Leave-alone: a secret-shaped key WITH a value ⇒ deferral emitted; empty-value
secret-shaped key ⇒ no deferral.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.config_projection import (  # noqa: E402
    CLAUDE_ENV_MANAGED_BEGIN,
    CLAUDE_ENV_MANAGED_END,
)
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


# The canonical set of pure-CONFIG managed-block exports a safe-add project
# gets (routing keys, ports, URLs) — NONE of which is secret-shaped. This is the
# incident-machine shape (23 lines, zero secrets).
_CONFIG_KEYS = [
    "PROJECT_NAME", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DIAGRAMS_COLLECTION", "CODE_GRAPH_PROJECT",
    "SHARED_KG_WRITE_DISABLED", "SHARED_KG_OPT_OUT", "ACTIVE_EMBEDDING",
    "EMBEDDING_MODEL", "WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL",
    "OLLAMA_PORT", "GRPC_PORT", "CODE_EMBED_URL", "CODE_EMBED_PORT",
    "VCT_ORCHESTRATOR_ROOT", "VCT_INFRASTRUCTURE_DIR", "VCT_INSTALL_ROOT",
    "CODE_EMBED_SERVICE_URL", "KG_BASE_DIR", "MCP_PYTHONPATH",
]


def _write_env(folder: Path, lines: list[str]) -> Path:
    claude = folder / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    body = (
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        + "\n".join(lines)
        + f"\n{CLAUDE_ENV_MANAGED_END}\n"
    )
    (claude / "env").write_text(body, encoding="utf-8")
    return claude / "env"


def _config_only_lines() -> list[str]:
    return [f'export {k}="somevalue"' for k in _CONFIG_KEYS]


class SecretShapeEnvScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-secretshape-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # ---- FAIL-WITHOUT-FIX PIN ------------------------------------------

    def test_config_only_env_is_not_flagged(self):
        """PIN (D6): 23 config exports, zero secrets ⇒ scan reads CLEAN.

        Pre-fix the coarse regex flagged the first `export KEY="..."` line, so
        this returned True forever. Post-fix it is False.
        """
        env_path = _write_env(self.tmp, _config_only_lines())
        self.assertFalse(
            project_init._has_user_secret_shaped_line(env_path),
            "config-only managed block must not be flagged as secret-retaining",
        )
        self.assertFalse(
            project_init._scan_user_secret_values_retained(self.tmp),
            "config-only project must not scan dirty on either surface",
        )

    def test_config_only_env_no_deferral_emitted(self):
        """PIN (D6): the emitter does NOT write a deferral for a config-only env."""
        _write_env(self.tmp, _config_only_lines())
        project_init._emit_user_secret_values_retained_deferral(self.tmp)
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("user_secret_values_retained_in_tree"),
            "no secret-shaped value present ⇒ no deferral",
        )

    def test_stale_entry_self_clears_on_config_only_env(self):
        """PIN (D6): an existing on-disk `user_secret_values_retained_in_tree`
        entry — the incident-machine's permanent false positive — CLEARS via the
        reconciler once the scan reads clean (config-only env)."""
        # Seed the stale entry.
        report = DeferralReport.read(self.tmp)
        report.add_entry(DeferralEntry(
            condition_id="user_secret_values_retained_in_tree",
            title="stale false positive",
            detected="secret-shaped line found",
            why_deferred="one-time",
            command_to_apply="noop",
            severity="warning",
        ))
        report.write(self.tmp)
        # Config-only env on disk.
        _write_env(self.tmp, _config_only_lines())

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report_after = DeferralReport.read(self.tmp)
        self.assertFalse(
            report_after.has_condition("user_secret_values_retained_in_tree"),
            "config-only env must self-clear the stale secret-retention deferral",
        )

    # ---- LEAVE-ALONE ---------------------------------------------------

    def test_secret_shaped_key_with_value_is_flagged(self):
        """A genuine secret-shaped key WITH a value in the env managed block is
        still flagged (deferral emitted exactly as v0.2.83)."""
        lines = _config_only_lines() + ['export OPENAI_API_KEY="sk-realish-value"']
        env_path = _write_env(self.tmp, lines)
        self.assertTrue(
            project_init._has_user_secret_shaped_line(env_path),
            "OPENAI_API_KEY with a value must still be flagged",
        )
        project_init._emit_user_secret_values_retained_deferral(self.tmp)
        report = DeferralReport.read(self.tmp)
        self.assertTrue(
            report.has_condition("user_secret_values_retained_in_tree"),
            "secret-shaped key with a value ⇒ deferral (leave-alone)",
        )
        # Value NEVER printed into the deferral.
        body = (self.tmp / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sk-realish-value", body)

    def test_secret_shaped_key_with_empty_value_is_not_flagged(self):
        """An empty-value secret-shaped key carries no VALUE to worry about ⇒
        no deferral (D6 non-empty-value requirement)."""
        lines = _config_only_lines() + ['export GITHUB_PAT=""']
        env_path = _write_env(self.tmp, lines)
        self.assertFalse(
            project_init._has_user_secret_shaped_line(env_path),
            "empty-value secret-shaped key must not be flagged",
        )
        project_init._emit_user_secret_values_retained_deferral(self.tmp)
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("user_secret_values_retained_in_tree"),
            "empty-value secret-shaped key ⇒ no deferral",
        )

    def test_various_secret_needles_flagged_with_value(self):
        """The single secret-shape home flags TOKEN/SECRET/PAT/PASSWORD/AUTH/
        *_KEY tokens; a value-bearing one on the env surface trips the scan."""
        for key in (
            "MYPROJECT_JIRA_TOKEN", "DB_PASSWORD", "SERVICE_AUTH",
            "ANTHROPIC_API_KEY", "GH_PAT", "APP_SECRET",
        ):
            with self.subTest(key=key):
                folder = Path(tempfile.mkdtemp(prefix="vct-needle-"))
                try:
                    env_path = _write_env(folder, [f'export {key}="v"'])
                    self.assertTrue(
                        project_init._has_user_secret_shaped_line(env_path),
                        f"{key} with a value should be flagged",
                    )
                finally:
                    import shutil
                    shutil.rmtree(str(folder), ignore_errors=True)

    def test_routing_key_lookalikes_not_flagged(self):
        """Keys that merely CONTAIN a needle substring across a `_`/`-` boundary
        (PYTHONPATH ⊃ PAT, COMPASS ⊃ PASS) are NOT secret-shaped — the token
        splitter in `is_secret_shaped_env_key` guards this."""
        env_path = _write_env(
            self.tmp,
            ['export MCP_PYTHONPATH="/x"', 'export COMPASS_URL="http://x"'],
        )
        self.assertFalse(
            project_init._has_user_secret_shaped_line(env_path),
            "PYTHONPATH / COMPASS must not be mistaken for PAT / PASS",
        )

    # ---- A3 OS coverage: CRLF-safe managed-block parsing ----

    def _write_env_crlf(self, folder: Path, lines: list[str]) -> Path:
        """Write the managed block with WINDOWS CRLF line endings."""
        claude = folder / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        body = (
            f"{CLAUDE_ENV_MANAGED_BEGIN}\r\n"
            + "\r\n".join(lines)
            + f"\r\n{CLAUDE_ENV_MANAGED_END}\r\n"
        )
        env_path = claude / "env"
        env_path.write_bytes(body.encode("utf-8"))
        return env_path

    def test_crlf_config_only_env_not_flagged(self):
        """A3 (CRLF): a Windows-line-ended config-only managed block reads CLEAN
        (the D6 regex is CRLF-safe)."""
        env_path = self._write_env_crlf(self.tmp, [f'export {k}="v"' for k in _CONFIG_KEYS])
        self.assertFalse(
            project_init._has_user_secret_shaped_line(env_path),
            "CRLF config-only env must not be flagged",
        )

    def test_crlf_secret_shaped_env_flagged(self):
        """A3 (CRLF): a CRLF managed block WITH a secret-shaped value is still
        flagged (the closing quote is captured before any trailing `\\r`)."""
        env_path = self._write_env_crlf(
            self.tmp, ['export KG_COLLECTION="X"', 'export GH_TOKEN="ghp_realish"'],
        )
        self.assertTrue(
            project_init._has_user_secret_shaped_line(env_path),
            "CRLF secret-shaped value must still be flagged",
        )

    # ---- A3 non-root fixture: the scan works on any project folder ----

    def test_non_root_project_scan_and_selfclear(self):
        """A3 (non-root): the scan + reconciler self-clear operate identically on
        a NON-ROOT project folder (a plain project dir with its own .claude/env),
        which is the safe-add shape that regressed on the incident machine."""
        # A non-root project: just a project folder (not an orchestrator root).
        nonroot = self.tmp / "some-user-project"
        nonroot.mkdir()
        # Seed the stale false-positive entry a pre-.84 install would have left.
        report = DeferralReport.read(nonroot)
        report.add_entry(DeferralEntry(
            condition_id="user_secret_values_retained_in_tree",
            title="stale", detected="secret-shaped line found",
            why_deferred="one-time", command_to_apply="noop", severity="warning",
        ))
        report.write(nonroot)
        # Config-only managed env (the safe-add default).
        _write_env(nonroot, _config_only_lines())

        self.assertFalse(project_init._scan_user_secret_values_retained(nonroot))
        project_init._reconcile_bundle_deferrals(
            nonroot, still_user_modified=False, still_skipped_existing=False,
        )
        self.assertFalse(
            DeferralReport.read(nonroot).has_condition(
                "user_secret_values_retained_in_tree"
            ),
            "non-root config-only project must self-clear the stale deferral",
        )


if __name__ == "__main__":
    unittest.main()
