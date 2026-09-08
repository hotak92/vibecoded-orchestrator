# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Host token, file permissions, the OAuth reader, and path/port resolution.

The permission tests are the tri-OS ones. ``chmod 0600`` has no Windows
equivalent, so the gateway applies an owner-only ACL there instead — and the
rule is that a permission call which cannot be applied FAILS LOUDLY rather
than silently leaving a token file readable by every other local user. Each
permission assertion therefore has a POSIX arm and a Windows arm, and each is
skipped on the other OS with a reason NAMING the gap, so the tri-OS CI leg is
where the missing half runs rather than the gap being invisible.

The OAuth reader tests cover the states a real machine produces: never logged
in, logged in, lapsed, file rewritten by a refresh, and file present but not
parseable. "File absent" must be an actionable 401, never a crash — the
credentials file is harness-owned and this package only ever reads it.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.common.env import EnvIsolationMixin

from model_router import auth, config, fileperms

POSIX_ONLY = "POSIX mode bits do not exist on Windows; the Windows arm of this rule is test_host_token_acl_is_owner_only, which runs on the Windows CI leg"
WINDOWS_ONLY = "Windows ACLs cannot be inspected on POSIX; the POSIX arm of this rule is test_host_token_permissions_are_owner_only, which runs on the Linux and macOS CI legs"

FAKE_OAUTH = "wp9-oauth-synthetic-not-a-real-token"


class _TmpCase(EnvIsolationMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wp9-auth-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class HostTokenTests(_TmpCase):
    def test_a_token_is_generated_on_first_use(self) -> None:
        path = self.dir / "model-gateway.token"
        token = auth.ensure_host_token(path)
        self.assertEqual(len(token), 64)
        self.assertTrue(path.is_file())

    def test_the_token_is_stable_across_calls(self) -> None:
        path = self.dir / "model-gateway.token"
        self.assertEqual(auth.ensure_host_token(path), auth.ensure_host_token(path))

    def test_the_parent_directory_is_created(self) -> None:
        path = self.dir / "nested" / "deeper" / "model-gateway.token"
        auth.ensure_host_token(path)
        self.assertTrue(path.is_file())

    @unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
    def test_host_token_permissions_are_owner_only(self) -> None:
        path = self.dir / "model-gateway.token"
        auth.ensure_host_token(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    @unittest.skipUnless(sys.platform == "win32", WINDOWS_ONLY)
    def test_host_token_acl_is_owner_only(self) -> None:  # pragma: no cover
        path = self.dir / "model-gateway.token"
        auth.ensure_host_token(path)
        self.assertEqual(fileperms.owner_only_state(path), "owner_only")

    @unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
    def test_a_loosened_existing_token_file_is_re_tightened(self) -> None:
        """An editor or a restore can widen it after the fact."""
        path = self.dir / "model-gateway.token"
        auth.ensure_host_token(path)
        os.chmod(path, 0o644)
        auth.ensure_host_token(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_token_is_not_returned_when_it_cannot_be_secured(self) -> None:
        """Serving with a world-readable token would hand any local user the
        ability to proxy under this user's login and paid subscription."""
        path = self.dir / "model-gateway.token"
        original = auth.restrict_to_owner

        def refuse(_path):
            raise fileperms.PermissionHardeningError("simulated ACL failure")

        auth.restrict_to_owner = refuse  # type: ignore[assignment]
        self.addCleanup(setattr, auth, "restrict_to_owner", original)
        with self.assertRaises(fileperms.PermissionHardeningError):
            auth.ensure_host_token(path)
        self.assertFalse(path.exists(), "an unsecurable token file was left behind")

    def test_read_host_token_of_a_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(auth.read_host_token(self.dir / "absent"), "")

    def test_trailing_whitespace_is_tolerated(self) -> None:
        path = self.dir / "t"
        path.write_text("abc123\n", encoding="utf-8")
        self.assertEqual(auth.read_host_token(path), "abc123")


class TokenComparisonTests(unittest.TestCase):
    def test_matching_tokens_compare_equal(self) -> None:
        self.assertTrue(auth.token_matches("abc", "abc"))

    def test_mismatched_tokens_do_not(self) -> None:
        self.assertFalse(auth.token_matches("abc", "abd"))
        self.assertFalse(auth.token_matches("ab", "abc"))

    def test_an_empty_expected_token_never_matches(self) -> None:
        """Otherwise a gateway that failed to load its token would accept
        an empty Authorization header from anyone."""
        self.assertFalse(auth.token_matches("", ""))
        self.assertFalse(auth.token_matches("anything", ""))

    def test_comparison_is_constant_time(self) -> None:
        import ast

        source = Path(auth.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "token_matches"
        )
        calls = [
            node.func.attr for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertIn("compare_digest", calls)


class OAuthReaderTests(_TmpCase):
    def _write(self, token, *, expires_in_ms=3_600_000, raw=None) -> Path:
        path = self.dir / ".credentials.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        section = {}
        if token is not None:
            section["accessToken"] = token
        section["expiresAt"] = int(time.time() * 1000) + expires_in_ms
        path.write_text(json.dumps({"claudeAiOauth": section}), encoding="utf-8")
        return path

    def test_a_valid_login_reads_as_present(self) -> None:
        state = auth.OAuthReader(self._write(FAKE_OAUTH)).read()
        self.assertEqual(state.state, "present")
        self.assertEqual(state.token, FAKE_OAUTH)
        self.assertIsNone(state.problem)
        self.assertTrue(state.present)

    def test_a_missing_file_is_absent_with_an_actionable_message(self) -> None:
        state = auth.OAuthReader(self.dir / "nope.json").read()
        self.assertEqual(state.state, "absent")
        self.assertIsNone(state.token)
        self.assertIn("does not exist", state.problem or "")
        self.assertIn("`claude`", state.problem or "")

    def test_an_empty_token_is_absent_not_present(self) -> None:
        state = auth.OAuthReader(self._write(None)).read()
        self.assertEqual(state.state, "absent")
        self.assertIn("no access token", state.problem or "")

    def test_an_expired_token_says_expired(self) -> None:
        state = auth.OAuthReader(self._write(FAKE_OAUTH, expires_in_ms=-1)).read()
        self.assertEqual(state.state, "expired")
        self.assertIn("expired", state.problem or "")
        self.assertIsNone(state.token)

    def test_unparseable_json_is_reported_not_raised(self) -> None:
        state = auth.OAuthReader(self._write(None, raw="{not json")).read()
        self.assertEqual(state.state, "unreadable")
        self.assertIn("readable JSON", state.problem or "")

    def test_a_missing_expiry_is_treated_as_no_expiry(self) -> None:
        path = self.dir / ".credentials.json"
        path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": FAKE_OAUTH}}),
            encoding="utf-8",
        )
        self.assertEqual(auth.OAuthReader(path).read().state, "present")

    def test_a_non_numeric_expiry_does_not_crash(self) -> None:
        path = self.dir / ".credentials.json"
        path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": FAKE_OAUTH, "expiresAt": "soon"}}),
            encoding="utf-8",
        )
        self.assertEqual(auth.OAuthReader(path).read().state, "present")

    def test_no_token_material_appears_in_any_problem_message(self) -> None:
        for path in (
            self._write(FAKE_OAUTH, expires_in_ms=-1),
            self._write(None),
        ):
            state = auth.OAuthReader(path).read()
            self.assertNotIn(FAKE_OAUTH, state.problem or "")

    def test_expiry_is_re_evaluated_without_the_file_changing(self) -> None:
        """A cached parse must not freeze the verdict: a token can lapse while
        the file sits untouched."""
        path = self._write(FAKE_OAUTH, expires_in_ms=1000)
        reader = auth.OAuthReader(path)
        self.assertEqual(reader.read().state, "present")
        future = int(time.time() * 1000) + 10_000
        self.assertEqual(reader.read(now_ms=future).state, "expired")

    def test_a_refreshed_file_is_picked_up(self) -> None:
        path = self._write(None)
        reader = auth.OAuthReader(path)
        self.assertEqual(reader.read().state, "absent")
        self._write("refreshed-synthetic")
        os.utime(path, (2_000_000, 2_000_000))
        self.assertEqual(reader.read().state, "present")

    def test_a_deleted_file_invalidates_the_cache(self) -> None:
        path = self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(path)
        self.assertEqual(reader.read().state, "present")
        path.unlink()
        self.assertEqual(reader.read().state, "absent")

    def test_the_reader_never_writes_to_the_credentials_file(self) -> None:
        """The file is harness-owned: the Claude CLI writes and refreshes it."""
        path = self._write(FAKE_OAUTH)
        before = path.stat().st_mtime_ns, path.read_bytes()
        auth.OAuthReader(path).read()
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)


class FilePermissionTests(_TmpCase):
    @unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
    def test_files_become_0600_and_directories_0700(self) -> None:
        target = self.dir / "f"
        target.write_text("x", encoding="utf-8")
        fileperms.restrict_to_owner(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        sub = self.dir / "d"
        sub.mkdir()
        fileperms.restrict_to_owner(sub)
        self.assertEqual(stat.S_IMODE(sub.stat().st_mode), 0o700)

    def test_a_missing_path_raises_rather_than_no_ops(self) -> None:
        with self.assertRaises(fileperms.PermissionHardeningError):
            fileperms.restrict_to_owner(self.dir / "absent")

    @unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
    def test_the_probe_is_a_tri_state(self) -> None:
        target = self.dir / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o644)
        self.assertEqual(fileperms.owner_only_state(target), "broader")
        os.chmod(target, 0o600)
        self.assertEqual(fileperms.owner_only_state(target), "owner_only")
        self.assertEqual(fileperms.owner_only_state(self.dir / "absent"), "unknown")

    def test_the_windows_classifier_is_unit_testable_on_any_os(self) -> None:
        """The ACL PARSE is OS-independent even though the ACL CALL is not, so
        the Windows decision logic is covered on every CI leg."""
        path = r"C:\Users\someone\.vct\model-gateway.token"
        owner_only = (
            f"{path} SOMEHOST\\someone:(R,W)\n"
            "                          NT AUTHORITY\\SYSTEM:(F)\n"
            "\nSuccessfully processed 1 files; Failed processing 0 files\n"
        )
        shared = owner_only.replace(
            "NT AUTHORITY\\SYSTEM:(F)", "SOMEHOST\\otheruser:(R)",
        )
        self.assertEqual(
            fileperms._classify_icacls_output(owner_only, "somehost\\someone", path),
            "owner_only",
        )
        self.assertEqual(
            fileperms._classify_icacls_output(shared, "somehost\\someone", path),
            "broader",
        )
        self.assertEqual(
            fileperms._classify_icacls_output("", "somehost\\someone", path),
            "unknown",
        )

    def test_a_windows_identity_cannot_be_guessed(self) -> None:
        self.set_env("USERNAME", "")
        with self.assertRaises(fileperms.PermissionHardeningError):
            fileperms._windows_grant_identity()


class ConfigResolutionTests(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        for key in (
            "VCT_MODEL_GATEWAY_PORT", "VCT_MODEL_GATEWAY_HOST",
            "VCT_MODEL_GATEWAY_CREDENTIALS", "VCT_MODEL_GATEWAY_CONTEXT_TABLE",
            "VCT_MODEL_GATEWAY_SECRET_PROJECT", "VCT_MODEL_GATEWAY_CATALOG_TTL",
            "VCT_MODEL_GATEWAY_STATIC_RETRY_TTL", "VCT_MODEL_GATEWAY_KEY_TTL",
        ):
            self.set_env(key, None)
        self.set_env("VCT_STATE_DIR", str(self.dir))

    def test_every_state_file_lives_under_the_resolved_state_root(self) -> None:
        """No ``Path.home()`` reconstruction: a redirected state dir moves all
        of them together."""
        for path in (
            config.token_path(), config.pid_path(), config.port_path(),
            config.export_path(), config.log_path(),
        ):
            with self.subTest(path=path):
                self.assertTrue(str(path).startswith(str(self.dir)), str(path))

    def test_port_defaults_to_the_documented_value(self) -> None:
        self.assertEqual(config.resolve_port(), config.DEFAULT_PORT)

    def test_port_file_is_used_when_present(self) -> None:
        config.port_path().write_text("12345\n", encoding="utf-8")
        self.assertEqual(config.resolve_port(), 12345)

    def test_env_beats_the_port_file(self) -> None:
        config.port_path().write_text("12345\n", encoding="utf-8")
        os.environ["VCT_MODEL_GATEWAY_PORT"] = "23456"
        self.assertEqual(config.resolve_port(), 23456)

    def test_a_nonsense_port_falls_back_rather_than_crashing(self) -> None:
        for value in ("banana", "0", "70000", "-1", ""):
            with self.subTest(value=value):
                os.environ["VCT_MODEL_GATEWAY_PORT"] = value
                self.assertEqual(config.resolve_port(), config.DEFAULT_PORT)

    def test_host_defaults_to_loopback(self) -> None:
        self.assertEqual(config.resolve_host(), "127.0.0.1")

    def test_localhost_is_accepted(self) -> None:
        os.environ["VCT_MODEL_GATEWAY_HOST"] = "localhost"
        self.assertEqual(config.resolve_host(), "localhost")

    def test_a_routable_bind_address_is_refused(self) -> None:
        """The token authorises proxying under a paid subscription; binding it
        to a routable interface is an error, not a footgun."""
        os.environ["VCT_MODEL_GATEWAY_HOST"] = "0.0.0.0"
        with self.assertRaises(config.HostNotLoopbackError):
            config.resolve_host()

    def test_an_unresolvable_host_is_not_treated_as_loopback(self) -> None:
        self.assertFalse(config.is_loopback("no-such-host.invalid"))

    def test_credentials_path_is_overridable(self) -> None:
        os.environ["VCT_MODEL_GATEWAY_CREDENTIALS"] = str(self.dir / "creds.json")
        self.assertEqual(config.credentials_path(), self.dir / "creds.json")

    def test_export_path_is_overridable(self) -> None:
        os.environ["VCT_MODEL_GATEWAY_CONTEXT_TABLE"] = str(self.dir / "t.json")
        self.assertEqual(config.export_path(), self.dir / "t.json")

    def test_secret_project_defaults_to_none(self) -> None:
        self.assertIsNone(config.GatewayConfig.from_env().secret_project)

    def test_secret_project_is_read_from_the_documented_env_key(self) -> None:
        os.environ["VCT_MODEL_GATEWAY_SECRET_PROJECT"] = "Acme"
        self.assertEqual(config.GatewayConfig.from_env().secret_project, "Acme")

    def test_every_documented_env_key_is_actually_read(self) -> None:
        """A knob nobody reads is a promise, not a feature."""
        documented = {
            "VCT_MODEL_GATEWAY_PORT", "VCT_MODEL_GATEWAY_HOST",
            "VCT_MODEL_GATEWAY_CREDENTIALS", "VCT_MODEL_GATEWAY_CONTEXT_TABLE",
            "VCT_MODEL_GATEWAY_SECRET_PROJECT", "VCT_MODEL_GATEWAY_CATALOG_TTL",
            "VCT_MODEL_GATEWAY_STATIC_RETRY_TTL", "VCT_MODEL_GATEWAY_KEY_TTL",
        }
        source = Path(config.__file__).read_text(encoding="utf-8")
        docstring = source.split('"""')[1]
        for key in documented:
            with self.subTest(key=key):
                self.assertIn(key, docstring, f"{key} is read but undocumented")
                self.assertGreaterEqual(
                    source.count(key), 2,
                    f"{key} appears only in the docstring — nothing reads it",
                )

    def test_no_undocumented_gateway_env_key_is_read(self) -> None:
        import re

        source = Path(config.__file__).read_text(encoding="utf-8")
        docstring = source.split('"""')[1]
        for key in set(re.findall(r"VCT_MODEL_GATEWAY_[A-Z_]+", source)):
            with self.subTest(key=key):
                self.assertIn(key, docstring)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
