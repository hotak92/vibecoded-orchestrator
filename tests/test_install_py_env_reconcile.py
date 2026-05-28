# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install.py's ``_reconcile_env_keys`` — A3 (v0.2.38).

Covers the additive-write behaviour that ``install.py --update`` uses to
append canonical env keys that were added to the orchestrator after the
user's original install.

Contract:
  1. Missing keys are appended with their default values.
  2. If all canonical keys are already present: action is noop, file unchanged.
  3. User-modified values for canonical keys are NOT overwritten.
  4. Non-canonical keys present in the existing .env are preserved.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402

from vco_lib.env_template import list_canonical_env_template_keys


class TestReconcileEnvKeys(unittest.TestCase):

    def test_missing_keys_are_appended(self) -> None:
        """Existing .env missing 2 canonical keys: both appended with defaults."""
        canonical = list_canonical_env_template_keys()
        all_keys = sorted(canonical)
        key_a, key_b = all_keys[0], all_keys[1]
        present_keys = {k for k in canonical if k not in (key_a, key_b)}

        existing_lines = "\n".join(f"{k}=existing_val" for k in sorted(present_keys)) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(existing_lines, encoding="utf-8")

            result = install._reconcile_env_keys(env_path)

            self.assertEqual(result["action"], "appended")
            added = set(result["added"])
            self.assertIn(key_a, added, f"{key_a} should have been added")
            self.assertIn(key_b, added, f"{key_b} should have been added")
            self.assertEqual(len(added), 2)

            # Verify both keys land in the file.
            written = env_path.read_text(encoding="utf-8")
        self.assertIn(f"{key_a}=", written)
        self.assertIn(f"{key_b}=", written)

    def test_noop_when_all_keys_present(self) -> None:
        """Existing .env already contains all canonical keys: action is noop."""
        canonical = list_canonical_env_template_keys()
        existing_lines = "\n".join(f"{k}=val" for k in sorted(canonical)) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(existing_lines, encoding="utf-8")
            original_mtime = env_path.stat().st_mtime

            result = install._reconcile_env_keys(env_path)

            self.assertEqual(result["action"], "noop")
            self.assertEqual(result["added"], [])
            # File must be untouched.
            self.assertEqual(env_path.stat().st_mtime, original_mtime)

    def test_user_modified_value_not_overwritten(self) -> None:
        """A canonical key with a user-set value is preserved exactly."""
        canonical = list_canonical_env_template_keys()
        all_keys = sorted(canonical)
        # Use KG_COLLECTION (likely) otherwise first key.
        user_key = "KG_COLLECTION" if "KG_COLLECTION" in canonical else all_keys[0]
        user_value = "my_custom_collection_9999"

        # Write all canonical keys so nothing is missing.
        lines = []
        for k in sorted(canonical):
            lines.append(f"{k}={user_value if k == user_key else 'default_val'}")
        existing_lines = "\n".join(lines) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(existing_lines, encoding="utf-8")

            result = install._reconcile_env_keys(env_path)

            self.assertEqual(result["action"], "noop")
            # The user's custom value must still be there verbatim.
            written = env_path.read_text(encoding="utf-8")

        self.assertIn(f"{user_key}={user_value}", written)
        # The function must not have overwritten it with a different value.
        # (Only check the user_key line, not an incidental match.)
        user_key_lines = [
            line for line in written.splitlines()
            if line.startswith(f"{user_key}=")
        ]
        self.assertTrue(
            all(f"{user_key}={user_value}" == ln for ln in user_key_lines),
            f"Expected {user_key}={user_value!r} in all occurrences, got: {user_key_lines}",
        )

    def test_non_canonical_key_preserved(self) -> None:
        """Keys not in the canonical set are preserved — never deleted."""
        canonical = list_canonical_env_template_keys()
        non_canonical_key = "MY_CUSTOM_PROJECT_TOKEN"
        non_canonical_val = "abc123secret"

        # All canonical keys present + one non-canonical key.
        lines = [f"{k}=val" for k in sorted(canonical)]
        lines.append(f"{non_canonical_key}={non_canonical_val}")
        existing_lines = "\n".join(lines) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(existing_lines, encoding="utf-8")

            result = install._reconcile_env_keys(env_path)

            # noop because all canonical keys are present.
            self.assertEqual(result["action"], "noop")
            written = env_path.read_text(encoding="utf-8")

        self.assertIn(f"{non_canonical_key}={non_canonical_val}", written)

    def test_skipped_when_env_missing(self) -> None:
        """If no .env file exists, reconcile is a no-op (fresh install handles it)."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            # File does not exist.
            result = install._reconcile_env_keys(env_path)

        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["added"], [])

    def test_added_keys_have_comment_marker(self) -> None:
        """Appended keys include the 'Added by install.py --update' comment."""
        canonical = list_canonical_env_template_keys()
        all_keys = sorted(canonical)
        missing_key = all_keys[0]
        present_keys = {k for k in canonical if k != missing_key}

        existing_lines = "\n".join(f"{k}=val" for k in sorted(present_keys)) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(existing_lines, encoding="utf-8")

            result = install._reconcile_env_keys(env_path)

            self.assertEqual(result["action"], "appended")
            written = env_path.read_text(encoding="utf-8")

        # Comment marker must be present.
        self.assertIn("Added by install.py --update on", written)
        # The key itself must appear.
        self.assertIn(f"{missing_key}=", written)


if __name__ == "__main__":
    unittest.main()
