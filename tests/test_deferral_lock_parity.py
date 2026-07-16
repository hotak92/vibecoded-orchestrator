# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity for the shared UPDATE_DEFERRED lock path (v0.2.83 WP-B6).

The ``UPDATE_DEFERRED.{md,json}`` read-modify-write cycle is serialized by an
exclusive advisory ``flock`` on ONE lockfile. Two languages take that lock:

  * **Python** — ``vco_lib.deferral_emit.locked_report`` (and its
    ``emit`` / ``emit_entries`` / ``resolve_conditions`` sugar) locks
    ``<folder> / vco_lib.deferral_emit.LOCK_REL``.
  * **Rust** — the launcher's DIRECT ``std::fs`` deferral writers
    (``installer::write_update_resume_deferral`` /
    ``clear_update_resume_deferral_if_solo``,
    ``git_user_editable_merge::write_launcher_update_diverged_deferral``,
    ``restart::clear_restart_deferral``) lock
    ``<folder> / vct_launcher_core::services::deferral_lock::LOCK_REL`` via
    ``lock_folder``.

flock is process-shared and advisory-but-cooperative: the two families
serialize ONLY when both point at the BYTE-IDENTICAL path. If either constant
drifts, the writers would lock DIFFERENT files and stop excluding each other —
silently reintroducing the WP-B6 clobber race. This test string-pins the two
constants (plus an independent expected literal) so a drift in EITHER trips
here, without spawning ``cargo`` (the Rust side's own behaviour is covered by
``cargo test -p vct-launcher-core deferral_lock`` and the per-writer
serialization tests in ``cargo test -p vct-launcher-temp --lib``).

Same discipline as ``tests/test_mcp_scan_rules_parity.py`` and the other
``*_parity.py`` string-pins.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_emit  # noqa: E402

# The one true lock path, relative to a managed project folder. Both languages
# MUST resolve to exactly this (POSIX-style forward slashes).
EXPECTED_LOCK_REL = ".claude/context/.update-deferred.lock"

RUST_LOCK_MODULE = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "services"
    / "deferral_lock.rs"
)


class DeferralLockPathParityTests(unittest.TestCase):
    def test_python_lock_rel_matches_expected(self) -> None:
        """Python ``deferral_emit.LOCK_REL`` renders the expected POSIX path.

        ``LOCK_REL`` is a ``Path`` on the Python side; its POSIX rendering is
        what matters for parity (the Rust side stores the string literal).
        """
        self.assertEqual(deferral_emit.LOCK_REL.as_posix(), EXPECTED_LOCK_REL)

    def test_rust_lock_module_exists(self) -> None:
        self.assertTrue(
            RUST_LOCK_MODULE.is_file(),
            f"missing Rust lock module: {RUST_LOCK_MODULE}",
        )

    def test_rust_lock_rel_literal_matches_expected(self) -> None:
        """The Rust ``LOCK_REL`` const literal equals the expected path.

        Grep the source (no cargo) for::

            pub const LOCK_REL: &str = ".claude/context/.update-deferred.lock";
        """
        src = RUST_LOCK_MODULE.read_text(encoding="utf-8")
        m = re.search(
            r'pub const LOCK_REL:\s*&str\s*=\s*"([^"]+)"\s*;',
            src,
        )
        self.assertIsNotNone(
            m,
            "could not find `pub const LOCK_REL: &str = \"...\";` in "
            f"{RUST_LOCK_MODULE}",
        )
        self.assertEqual(m.group(1), EXPECTED_LOCK_REL)

    def test_python_and_rust_lock_paths_are_byte_identical(self) -> None:
        """The two constants pin to the SAME string — the whole point.

        Triangulation: Python constant == expected == Rust literal. If any edge
        drifts, one of the three assertions here (or above) fails first.
        """
        py = deferral_emit.LOCK_REL.as_posix()
        src = RUST_LOCK_MODULE.read_text(encoding="utf-8")
        m = re.search(r'pub const LOCK_REL:\s*&str\s*=\s*"([^"]+)"\s*;', src)
        assert m is not None  # pinned by the test above; narrow for type-checkers
        rust = m.group(1)
        self.assertEqual(
            py,
            rust,
            "Python deferral_emit.LOCK_REL and Rust deferral_lock::LOCK_REL "
            f"diverged (py={py!r}, rust={rust!r}) — the direct Rust writers and "
            "the Python emitter would lock DIFFERENT files and stop serializing.",
        )
        self.assertEqual(py, EXPECTED_LOCK_REL)


if __name__ == "__main__":
    unittest.main()


def test_lightweight_install_write_is_lock_wrapped() -> None:
    """v0.2.83 re-review N-NEW-1: the lightweight install path's
    merge_from_disk + write was the LAST un-locked production RMW of
    UPDATE_DEFERRED. Structural pin (same idiom as the v0.2.75 flow tests):
    the write must sit inside an ``exclusive_file_lock`` on the shared
    ``deferral_emit.LOCK_REL`` path."""
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    start = src.find("_lightweight_deferral.merge_from_disk(")
    assert start != -1, "lightweight merge_from_disk site exists"
    # Look back a bounded window for the lock context manager.
    window = src[max(0, start - 600):start]
    assert "exclusive_file_lock" in window and "LOCK_REL" in window, (
        "the lightweight merge+write must run under exclusive_file_lock on "
        "the shared deferral LOCK_REL (N-NEW-1)"
    )
    # And the write itself must still be inside the same with-block: it must
    # appear before the enclosing try's except line that follows the block.
    tail = src[start:start + 600]
    assert "_lightweight_deferral.write(_lightweight_folder)" in tail
