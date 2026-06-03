"""V47-C (v0.2.46 Part 2 Gap C) — .env secret-shaped key detection +
migration tests.

Two complementary suites:

1. **Pure-function tests** for ``vco_lib.secrets_audit``:
   - ``audit_env_secrets`` — detects API_KEY / TOKEN / SECRET / PAT keys,
     skips placeholders and comments, returns [] on missing file.
   - ``rewrite_env_with_sentinels`` — replaces values atomically while
     preserving comments + ``export`` prefix.
   - ``harden_env_perms`` — Unix-only chmod 0o600 when perms are loose.
   - ``is_secret_shaped_env_key`` — substring-match with segment
     boundaries to avoid false positives.

2. **Install.py interactive-flow tests**: mock ``input()`` /
   ``sys.stdin.isatty`` / ``_post_secrets_to_hub`` so we don't need a
   running launcher to verify the prompt + deferral semantics.

Pinned contract:
   * ``--yes`` / non-TTY → keep-in-env + ``env_secrets_retained_in_plaintext``
     deferral entry.
   * Interactive "Y" → POSTs to hub, rewrites .env on success.
   * Interactive "n" → keep-in-env + info-severity deferral.
   * Hub call failure → keep-in-env + warning-severity deferral.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

# ─── Load secrets_audit + install.py without running their main() side
# ─── effects (mirrors the loader pattern in test_v0246_v47gstub_adopt_contract).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from vco_lib import secrets_audit  # noqa: E402

_INSTALL_PY = _REPO / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47c", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47c"] = install_py
_spec.loader.exec_module(install_py)


# ───────────────────────────────────────────────────────────────────────
# Section 1: pure-function tests for vco_lib.secrets_audit
# ───────────────────────────────────────────────────────────────────────


class TestIsSecretShapedEnvKey:
    """The predicate matches install.py's heuristic exactly."""

    @pytest.mark.parametrize("key", [
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "STRIPE_KEY",
        "DB_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "MY_PAT",
        "AUTH_HEADER",
        "DB_PASS",
        "KEY",          # bare suffix-only
        "_KEY",         # underscore-prefixed
        "OPENAI_KEY",   # ends with _KEY
    ])
    def test_known_secret_shapes_match(self, key: str) -> None:
        assert secrets_audit.is_secret_shaped_env_key(key) is True

    @pytest.mark.parametrize("key", [
        # Negatives: install.py's `_is_secret_shaped_env_key` should not
        # match these. PYTHONPATH and COMPASS are the historical
        # false-positive cases the substring-as-segment rule was
        # designed to defeat.
        "PYTHONPATH",   # contains "PAT" as a substring but not as a segment
        "COMPASS",      # contains "PASS" as a substring but not as a segment
        "WEAVIATE_URL",
        "OLLAMA_URL",
        "KG_COLLECTION",
        "ACTIVE_EMBEDDING",
        "PROJECT_NAME",
        "VCT_PROJECT_HOST",
    ])
    def test_unrelated_keys_do_not_match(self, key: str) -> None:
        assert secrets_audit.is_secret_shaped_env_key(key) is False

    def test_install_py_helper_matches_this_module(self) -> None:
        """The two heuristics must agree on every key — they are two
        copies of the same rule.
        """
        for k in [
            "GITHUB_TOKEN", "OPENAI_API_KEY", "PYTHONPATH", "COMPASS",
            "STRIPE_KEY", "WEAVIATE_URL", "MY_PAT", "DB_PASS",
        ]:
            assert (
                secrets_audit.is_secret_shaped_env_key(k)
                == install_py._is_secret_shaped_env_key(k)
            ), f"helpers diverged on key={k!r}"


class TestAuditEnvSecrets:
    """Parser detects credentials, ignores placeholders + comments."""

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"  # doesn't exist
        assert secrets_audit.audit_env_secrets(env_path) == []

    def test_detects_canonical_secret_keys(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent("""\
            GITHUB_TOKEN=ghp_abc123
            OPENAI_API_KEY=sk-def456
            STRIPE_KEY=stripe_xyz
            DB_PASSWORD=hunter2
        """))
        result = secrets_audit.audit_env_secrets(env_path)
        keys = {s.key for s in result}
        assert keys == {"GITHUB_TOKEN", "OPENAI_API_KEY", "STRIPE_KEY", "DB_PASSWORD"}

    def test_skips_comments_and_blank_lines(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent("""\
            # comment line
            GITHUB_TOKEN=ghp_abc123

            # GITHUB_TOKEN=should_not_match
            #STRIPE_KEY=also_commented
        """))
        result = secrets_audit.audit_env_secrets(env_path)
        keys = [s.key for s in result]
        assert keys == ["GITHUB_TOKEN"]

    def test_supports_export_prefix(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("export GITHUB_TOKEN=ghp_abc\n")
        result = secrets_audit.audit_env_secrets(env_path)
        assert len(result) == 1
        assert result[0].key == "GITHUB_TOKEN"
        assert result[0].value == "ghp_abc"

    def test_strips_inline_quotes(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent('''\
            GITHUB_TOKEN="ghp_quoted"
            OPENAI_API_KEY='sk-singlequoted'
        '''))
        result = {s.key: s.value for s in secrets_audit.audit_env_secrets(env_path)}
        assert result["GITHUB_TOKEN"] == "ghp_quoted"
        assert result["OPENAI_API_KEY"] == "sk-singlequoted"

    def test_skips_placeholder_values(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent("""\
            GITHUB_TOKEN=
            OPENAI_API_KEY=<your-api-key>
            STRIPE_KEY=changeme
            DB_PASSWORD=__vco_keychain__
            REAL_TOKEN=ghp_actually_set
        """))
        result = secrets_audit.audit_env_secrets(env_path)
        keys = [s.key for s in result]
        assert keys == ["REAL_TOKEN"]

    def test_does_not_match_non_secret_keys(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent("""\
            WEAVIATE_URL=http://localhost:8081
            OLLAMA_URL=http://localhost:11435
            PROJECT_NAME=MyProject
            KG_COLLECTION=MyKG
        """))
        assert secrets_audit.audit_env_secrets(env_path) == []

    def test_strips_inline_unquoted_comment(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("GITHUB_TOKEN=ghp_abc # team key\n")
        result = secrets_audit.audit_env_secrets(env_path)
        assert len(result) == 1
        assert result[0].value == "ghp_abc"


class TestRewriteEnvWithSentinels:
    """Atomic sentinel rewrite preserves structure."""

    def test_replaces_value_with_sentinel(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("GITHUB_TOKEN=ghp_abc\n")
        replaced, missed = secrets_audit.rewrite_env_with_sentinels(
            env_path, ["GITHUB_TOKEN"],
        )
        assert replaced == 1
        assert missed == []
        assert env_path.read_text() == "GITHUB_TOKEN=__vco_keychain__\n"

    def test_preserves_export_prefix_and_comments(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(textwrap.dedent("""\
            # header comment
            export GITHUB_TOKEN=ghp_abc  # inline
            OTHER_VAR=keep_me
        """))
        replaced, _ = secrets_audit.rewrite_env_with_sentinels(
            env_path, ["GITHUB_TOKEN"],
        )
        assert replaced == 1
        out = env_path.read_text()
        assert "# header comment" in out
        assert "export GITHUB_TOKEN=__vco_keychain__  # inline" in out
        assert "OTHER_VAR=keep_me" in out

    def test_reports_missed_keys(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("GITHUB_TOKEN=ghp_abc\n")
        _, missed = secrets_audit.rewrite_env_with_sentinels(
            env_path, ["GITHUB_TOKEN", "NEVER_PRESENT"],
        )
        assert missed == ["NEVER_PRESENT"]

    def test_atomic_write_preserves_mode(self, tmp_path: Path) -> None:
        if os.name != "posix":
            pytest.skip("posix-only mode check")
        env_path = tmp_path / ".env"
        env_path.write_text("GITHUB_TOKEN=ghp_abc\n")
        env_path.chmod(0o600)
        secrets_audit.rewrite_env_with_sentinels(env_path, ["GITHUB_TOKEN"])
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600, f"mode changed: {oct(mode)}"


class TestHardenEnvPerms:
    """Unix: tightens permissions if loose. Windows: no-op."""

    def test_no_op_when_already_tight(self, tmp_path: Path) -> None:
        if os.name != "posix":
            pytest.skip("posix-only mode check")
        env_path = tmp_path / ".env"
        env_path.write_text("X=y\n")
        env_path.chmod(0o600)
        changed, msg = secrets_audit.harden_env_perms(env_path)
        assert changed is False
        assert msg == ""

    def test_tightens_loose_perms_on_unix(self, tmp_path: Path) -> None:
        if os.name != "posix":
            pytest.skip("posix-only mode check")
        env_path = tmp_path / ".env"
        env_path.write_text("X=y\n")
        env_path.chmod(0o644)  # world-readable — too loose
        changed, msg = secrets_audit.harden_env_perms(env_path)
        assert changed is True
        new_mode = stat.S_IMODE(env_path.stat().st_mode)
        # Group + other bits stripped; owner bits preserved.
        assert (new_mode & 0o077) == 0
        assert "0o644" in msg


# ───────────────────────────────────────────────────────────────────────
# Section 2: install.py interactive-flow tests
# ───────────────────────────────────────────────────────────────────────


def _make_args(yes: bool = False, quiet: bool = False) -> SimpleNamespace:
    """argparse Namespace shim for the helper's args.yes / args.quiet probes."""
    return SimpleNamespace(yes=yes, quiet=quiet)


class TestAuditAndOfferEnvSecretMigration:
    """End-to-end install.py wiring of _audit_and_offer_env_secret_migration."""

    def test_no_env_file_is_silent_noop(self, tmp_path: Path) -> None:
        # No .env at all — must NOT call hub, must NOT add deferral.
        deferral = install_py.DeferralReport()
        with mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_not_called()
        assert deferral.entries == []

    def test_yes_flag_defaults_to_keep_in_env_with_deferral(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".env").write_text("GITHUB_TOKEN=ghp_abc\n")
        deferral = install_py.DeferralReport()
        with mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(yes=True),
            )
            hub.assert_not_called()
        assert len(deferral.entries) == 1
        entry = deferral.entries[0]
        assert entry.condition_id == "env_secrets_retained_in_plaintext"
        assert entry.severity == "warning"

    def test_non_tty_defaults_to_keep_in_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("STRIPE_KEY=stripe_xyz\n")
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_not_called()
        assert len(deferral.entries) == 1
        assert deferral.entries[0].condition_id == "env_secrets_retained_in_plaintext"

    def test_interactive_yes_calls_hub_and_rewrites_env(
        self, tmp_path: Path,
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "GITHUB_TOKEN=ghp_real\nOPENAI_API_KEY=sk-real\n"
        )
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch.object(
                 install_py,
                 "_post_secrets_to_hub",
                 return_value=(["GITHUB_TOKEN", "OPENAI_API_KEY"], []),
             ) as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_called_once()
            payload = hub.call_args[0][0]
            payload_keys = {item["key"] for item in payload}
            assert payload_keys == {"GITHUB_TOKEN", "OPENAI_API_KEY"}
        # .env was rewritten with sentinels.
        rewritten = env_path.read_text()
        assert "GITHUB_TOKEN=__vco_keychain__" in rewritten
        assert "OPENAI_API_KEY=__vco_keychain__" in rewritten
        # No deferral for the success path.
        assert deferral.entries == []

    def test_interactive_no_emits_info_deferral(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("GITHUB_TOKEN=ghp_real\n")
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_not_called()
        assert len(deferral.entries) == 1
        entry = deferral.entries[0]
        assert entry.condition_id == "env_secrets_retained_in_plaintext"
        assert entry.severity == "info"  # user-choice → info, not warning

    def test_hub_failure_emits_warning_deferral_and_no_rewrite(
        self, tmp_path: Path,
    ) -> None:
        env_path = tmp_path / ".env"
        original = "GITHUB_TOKEN=ghp_real\n"
        env_path.write_text(original)
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch.object(
                 install_py,
                 "_post_secrets_to_hub",
                 side_effect=RuntimeError("hub unreachable"),
             ):
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
        # .env must be unchanged when the hub failed.
        assert env_path.read_text() == original
        # Deferral entry recorded.
        assert len(deferral.entries) == 1
        entry = deferral.entries[0]
        assert entry.condition_id == "env_secrets_hub_migration_failed"
        assert entry.severity == "warning"

    def test_replace_all_mode_still_prompts(self, tmp_path: Path) -> None:
        """--adopt-project-replace-all does NOT auto-migrate secrets."""
        (tmp_path / ".env").write_text("GITHUB_TOKEN=ghp_real\n")
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n") as inp, \
             mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, "replace-all", _make_args(),
            )
            # User was prompted (input() was called)
            inp.assert_called()
            # Hub was NOT called (user said "n")
            hub.assert_not_called()
