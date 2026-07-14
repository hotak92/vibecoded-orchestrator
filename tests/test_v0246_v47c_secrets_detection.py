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

    # v0.2.46 post-adversarial M2: pin the documented multi-line-value
    # limitation. These tests assert CURRENT (line-based) behavior so a
    # future "fix" for multi-line support doesn't silently regress how
    # we read industry-standard .env files. See docstring in
    # vco_lib/secrets_audit.audit_env_secrets for the rationale.

    def test_multiline_quoted_value_documented_misread(self, tmp_path: Path) -> None:
        """Multi-line quoted values read only the first line.

        Documented limitation: a value spanning multiple lines via embedded
        raw newlines inside quotes is not understood. Parser reads ONLY the
        first line of the value; subsequent lines are interpreted as
        separate lines (which may be skipped, treated as bad KEY=VAL, or
        flagged as their own secret-shaped entries).
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "JSON_KEY='{\"private_key\": \"-----BEGIN-----\n"
            "MIIEv...\n"
            "-----END-----\"}'\n"
        )
        result = secrets_audit.audit_env_secrets(env_path)
        # JSON_KEY is detected (secret-shaped name) but its value is just the
        # first-line fragment up to and including the opening newline.
        # The subsequent `MIIEv...` line has no `=` and is silently skipped.
        # The `-----END-----"}'` line also has no `=` and is silently skipped.
        json_key_entries = [s for s in result if s.key == "JSON_KEY"]
        assert len(json_key_entries) == 1
        # Value is the first-line content (with quote-stripping applied).
        # Just assert it does NOT contain `MIIEv` — proving the multi-line
        # span was NOT joined into a single value.
        assert "MIIEv" not in json_key_entries[0].value, (
            "Multi-line value should not be joined across raw newlines. "
            "If this test starts failing, the parser was extended to handle "
            "multi-line values — update both the test and the docstring."
        )

    def test_multiline_backslash_continuation_documented_misread(self, tmp_path: Path) -> None:
        """Shell line-continuation (``\\`` at EOL) inside a value reads as literal.

        Documented limitation: ``KEY=part1\\\\\\npart2`` (with a literal backslash
        before newline) is not interpreted as line continuation. The parser
        reads the value as ending at the first newline, and `part2` becomes
        its own (probably skipped) line.
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "API_TOKEN=ghp_part1\\\n"
            "part2_continues_here\n"
        )
        result = secrets_audit.audit_env_secrets(env_path)
        # API_TOKEN is detected (secret-shaped) but value ends at first \n.
        api_entries = [s for s in result if s.key == "API_TOKEN"]
        assert len(api_entries) == 1
        assert "part2" not in api_entries[0].value, (
            "Shell line-continuation should NOT be honored. "
            "If this test starts failing, the parser was extended; "
            "update both the test and the docstring."
        )

    def test_underflagging_is_the_safe_failure_mode(self, tmp_path: Path) -> None:
        """Document the safety property: parser UNDER-FLAGS rather than over-flags.

        If a real multi-line secret is in the .env, it gets missed (user can
        still add it via the launcher Secrets tab manually). The opposite
        failure mode — flagging a non-secret as if it were one — could lead
        VCO to migrate a sentinel/placeholder/comment to the keychain, which
        would be confusing. Today's parser errs on the safe side.
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# multi-line block follows; parser only sees its first line\n"
            "PEM_KEY='-----BEGIN PRIVATE KEY-----\n"
            "actual_key_material_here\n"
            "-----END PRIVATE KEY-----'\n"
        )
        result = secrets_audit.audit_env_secrets(env_path)
        # PEM_KEY matches the secret-shape heuristic on its key portion.
        pem_entries = [s for s in result if s.key == "PEM_KEY"]
        assert len(pem_entries) == 1
        # The captured value is just the first line — does NOT contain the
        # real key material.
        assert "actual_key_material_here" not in pem_entries[0].value


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


class TestMultiLineSecretPreservation:
    """v0.2.46 post-adversarial M2 (revised): the user-data-never-lost gate.

    The line-based parser knowingly misses multi-line secrets — that's a
    documented limitation. The critical safety property is that a missed
    multi-line secret is NEVER silently removed from ``.env``. These tests
    pin that property end-to-end:

      1. ``audit_env_secrets`` does not surface the multi-line secret's key.
      2. The interactive migration prompt therefore can't ask the user to
         accept it.
      3. ``rewrite_env_with_sentinels`` is only ever called with keys the
         user explicitly accepted, so a missed key is never in
         ``migrated_keys``.
      4. Lines whose key is not in ``migrated_keys`` are passed through
         byte-identical.

    Failure mode: a future "improvement" to ``rewrite_env_with_sentinels``
    that tries to be smarter — e.g. "also normalize quoted blocks" or
    "strip trailing whitespace from all lines" — would break these tests
    and tell the contributor that the user-data-never-lost property is at
    risk.
    """

    def test_missed_multiline_key_not_in_audit_result(self, tmp_path: Path) -> None:
        """A multi-line secret's key MAY or MAY NOT be in the audit list
        (depends on what the first line happens to look like). What
        matters is that the multi-line VALUE is never returned in full —
        the parser can never accept it for migration. The downstream
        rewrite is only safe because of that.
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "REGULAR_TOKEN=ghp_safe_to_migrate\n"
            "JSON_KEY='{\"private_key\": \"-----BEGIN-----\n"
            "MIIE_real_secret_material_PLACEHOLDER\n"
            "-----END-----\"}'\n"
        )
        result = secrets_audit.audit_env_secrets(env_path)
        # The multi-line value is NOT captured intact: even if JSON_KEY
        # was returned, the second-line material is NOT in its value.
        for entry in result:
            assert "MIIE_real_secret_material_PLACEHOLDER" not in entry.value, (
                f"audit_env_secrets returned a value that includes "
                f"second-line material from a multi-line secret. The "
                f"parser was extended; update this test + the doc."
            )

    def test_rewrite_skips_keys_not_in_migrated_list(self, tmp_path: Path) -> None:
        """Property: keys whose names are NOT in ``migrated_keys`` are
        passed through byte-identical, even if they look secret-shaped.

        This is the load-bearing property: it's why missing a multi-line
        secret doesn't lose user data.
        """
        env_path = tmp_path / ".env"
        original = (
            "OPENAI_API_KEY=sk-this_will_be_migrated\n"
            "JSON_KEY='{\"private_key\": \"-----BEGIN-----\n"
            "MIIE_PLACEHOLDER_material\n"
            "-----END-----\"}'\n"
            "ANOTHER_TOKEN=ghp_audit_missed_this_one_too\n"
        )
        env_path.write_text(original)
        # User explicitly accepted ONLY OPENAI_API_KEY. JSON_KEY and
        # ANOTHER_TOKEN (whatever the audit thought of them) are NOT in
        # this list.
        secrets_audit.rewrite_env_with_sentinels(env_path, ["OPENAI_API_KEY"])
        rewritten = env_path.read_text()

        # OPENAI_API_KEY value should have been replaced.
        assert "sk-this_will_be_migrated" not in rewritten
        assert "OPENAI_API_KEY=__vco_keychain__" in rewritten

        # CRITICAL — multi-line value lines must be exactly preserved.
        assert "MIIE_PLACEHOLDER_material" in rewritten
        assert "-----BEGIN-----" in rewritten
        assert "-----END-----" in rewritten

        # CRITICAL — ANOTHER_TOKEN (not in migrated_keys) preserved as-is.
        assert "ANOTHER_TOKEN=ghp_audit_missed_this_one_too" in rewritten

    def test_rewrite_preserves_every_non_migrated_line_byte_identical(
        self, tmp_path: Path,
    ) -> None:
        """Stronger form: for every line whose key is not in
        ``migrated_keys`` (or which is not a KEY=VAL line at all), the
        line is preserved byte-identical, modulo the trailing newline
        normalization that ``splitlines() + join()`` performs.
        """
        env_path = tmp_path / ".env"
        original_lines = [
            "# leading comment",
            "",
            "OPENAI_API_KEY=sk-migrate_me",
            "JSON_KEY='{\"k\": \"v_first_line",
            "v_second_line",
            "v_third_line\"}'",
            "# trailing comment",
            "PEM_DATA=-----BEGIN-----",
            "real_pem_body_PLACEHOLDER",
            "-----END-----",
            "PORT=8080",
        ]
        env_path.write_text("\n".join(original_lines) + "\n")
        secrets_audit.rewrite_env_with_sentinels(env_path, ["OPENAI_API_KEY"])
        rewritten_lines = env_path.read_text().splitlines()

        # Build expectation: same lines, only the OPENAI_API_KEY one's
        # value replaced by the sentinel.
        for orig, new in zip(original_lines, rewritten_lines):
            if orig.startswith("OPENAI_API_KEY="):
                # value replaced by sentinel
                assert new == "OPENAI_API_KEY=__vco_keychain__", (
                    f"OPENAI_API_KEY line not sentinel-rewritten: {new!r}"
                )
            else:
                assert new == orig, (
                    f"Non-migrated line was modified! "
                    f"original={orig!r} new={new!r} — "
                    f"this breaks the user-data-never-lost property."
                )

    def test_documented_misread_does_not_cause_data_loss(self, tmp_path: Path) -> None:
        """End-to-end: even when the audit returns garbage for a
        multi-line secret, the final ``.env`` retains the secret bytes.

        Simulates the worst case the documented limitation can produce —
        and proves it does not lose user data.
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# multi-line secret follows\n"
            "GH_DEPLOY_KEY='-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAA_PLACEHOLDER_BLOB\n"
            "-----END OPENSSH PRIVATE KEY-----'\n"
            "OTHER_TOKEN=ghp_normal_migrate_target\n"
        )
        # Audit returns what it returns. Caller would prompt user about
        # the audited keys; assume user accepts BOTH (worst case: GH_DEPLOY_KEY
        # is audited with garbage-first-line value, OTHER_TOKEN is normal).
        audited = secrets_audit.audit_env_secrets(env_path)
        accepted_keys = [c.key for c in audited]
        # The migration step is told these keys were "successfully migrated".
        secrets_audit.rewrite_env_with_sentinels(env_path, accepted_keys)
        final = env_path.read_text()

        # The multi-line BODY (lines 2 and 3 of the PEM block) must
        # survive — they were never recognized as KEY=VAL lines, so
        # they pass through byte-identical regardless of what the
        # caller did.
        assert "b3BlbnNzaC1rZXktdjEAAAAA_PLACEHOLDER_BLOB" in final, (
            "Multi-line secret's body line was LOST during rewrite. "
            "User-data-never-lost property is broken."
        )
        assert "-----END OPENSSH PRIVATE KEY-----" in final


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
             mock.patch(
                 "vco_lib.project_config._resolve_project_id",
                 return_value="proj-registered-id",
             ), \
             mock.patch.object(
                 install_py,
                 "_post_secrets_to_hub",
                 return_value=(
                     ["GITHUB_TOKEN", "OPENAI_API_KEY"], [], "per_project",
                 ),
             ) as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_called_once()
            payload = hub.call_args[0][0]
            payload_keys = {item["key"] for item in payload}
            assert payload_keys == {"GITHUB_TOKEN", "OPENAI_API_KEY"}
            # GAP-1: the resolved project id is forwarded to the hub.
            assert hub.call_args.kwargs.get("project_id") == "proj-registered-id"
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
             mock.patch(
                 "vco_lib.project_config._resolve_project_id",
                 return_value="proj-registered-id",
             ), \
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

    # ─── GAP-1 (2026-07-14): per-project CLI scope routing ────────────

    def test_unregistered_non_root_defers_instead_of_migrating(
        self, tmp_path: Path,
    ) -> None:
        """A fresh UNREGISTERED non-root adopt must DEFER (not migrate to
        Shared through the CLI back-door — that would leak per-project
        credentials machine-wide)."""
        from vco_lib.project_config import ProjectNotFound

        env_path = tmp_path / ".env"
        original = "CLIENTA_DB_PASSWORD=pw-real\n"
        env_path.write_text(original)
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch(
                 "vco_lib.project_config._resolve_project_id",
                 side_effect=ProjectNotFound("not registered"),
             ), \
             mock.patch.object(install_py, "_post_secrets_to_hub") as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            # The hub is NEVER called for an unregistered non-root project.
            hub.assert_not_called()
        # .env untouched.
        assert env_path.read_text() == original
        # A deferral was recorded pointing at registering + re-migrating.
        assert len(deferral.entries) == 1
        entry = deferral.entries[0]
        assert entry.condition_id == "env_secrets_project_not_registered"
        assert entry.severity == "info"

    def test_clone_root_unregistered_still_migrates_as_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The orchestrator clone root, even when not registered yet, migrates
        with project_id=None (hub writes Shared — correct for root)."""
        from vco_lib.project_config import ProjectNotFound

        env_path = tmp_path / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-real\n")
        deferral = install_py.DeferralReport()
        # Make the audited project_root look like the clone root by pointing
        # install.py's __file__ at tmp_path.
        monkeypatch.setattr(install_py, "__file__", str(tmp_path / "install.py"))
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch(
                 "vco_lib.project_config._resolve_project_id",
                 side_effect=ProjectNotFound("root not registered yet"),
             ), \
             mock.patch.object(
                 install_py,
                 "_post_secrets_to_hub",
                 return_value=(["OPENAI_API_KEY"], [], "shared"),
             ) as hub:
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
            hub.assert_called_once()
            # Root path → project_id forwarded as None (hub writes Shared).
            assert hub.call_args.kwargs.get("project_id") is None
        # No deferral — the root migration proceeded.
        assert deferral.entries == []

    def test_old_hub_no_scope_field_prints_reScope_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When a project_id was sent but the hub omits `scope` (old hub), the
        user is told the keys landed in Shared + to re-scope."""
        env_path = tmp_path / ".env"
        env_path.write_text("GITHUB_TOKEN=ghp_real\n")
        deferral = install_py.DeferralReport()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch(
                 "vco_lib.project_config._resolve_project_id",
                 return_value="proj-id",
             ), \
             mock.patch.object(
                 install_py,
                 "_post_secrets_to_hub",
                 # scope=None simulates a hub that predates GAP-1.
                 return_value=(["GITHUB_TOKEN"], [], None),
             ):
            install_py._audit_and_offer_env_secret_migration(
                tmp_path, deferral, None, _make_args(),
            )
        out = capsys.readouterr().out
        assert "predates per-project secret migration" in out
        assert "SHARED keychain bucket" in out
