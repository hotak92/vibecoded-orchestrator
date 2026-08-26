# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.91 WP-F2 — every Preferences toggle must have a real backend consumer.

The field report that motivated this: the Preferences page had been shipping
"Close button minimizes to tray (doesn't exit)" — *defaulted ON* — since the
pref was introduced, while `on_window_event` unconditionally showed the
3-button quit dialog. `tray_start_minimized` had never done anything either.
Repo-wide grep found ZERO readers for either key: two visible toggles that
persisted a value nobody read.

A toggle with no consumer is a shipped lie, and nothing in the test suite
could tell. This module is that missing check. It asserts, for every window
preference the GUI renders:

  1. the key is declared with an explicit `consumer:` claim (no silent
     "trust me" entries),
  2. the claimed Rust symbol EXISTS and is actually USED (a constant defined
     and never read is the dead-toggle shape wearing a disguise),
  3. the key literal reaches the Rust side at all,
  4. the write command accepts it — a key the command would reject is a
     toggle that errors on click, which is the same lie with extra steps.

Scope note: this covers the launcher-global window prefs, which is where the
dead toggles lived. v0.2.91 also DELETED three further consumer-less entries
from the same page (`auto_update_enabled` — the wired one lives on
Preferences → Updates; `logging_level`; `default_embedding_mode`), and the
last assertion pins that the generic write-anything-nobody-reads block does
not come back.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFS_PAGE = REPO_ROOT / "launcher" / "src" / "routes" / "preferences" / "+page.svelte"
QUIT_DIALOG = REPO_ROOT / "launcher" / "src-tauri" / "src" / "quit_dialog.rs"
LIB_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"

_ENTRY_RE = re.compile(
    r"\{\s*key:\s*'(?P<key>[^']+)'.*?consumer:\s*'(?P<consumer>[^']+)'\s*,?\s*\}",
    re.DOTALL,
)


def _window_pref_entries() -> list[tuple[str, str]]:
    """Parse `WINDOW_PREF_KEYS` entries as (key, consumer) pairs."""
    src = PREFS_PAGE.read_text(encoding="utf-8")
    start = src.find("const WINDOW_PREF_KEYS = [")
    assert start > 0, "WINDOW_PREF_KEYS declaration not found in the Preferences page"
    end = src.find("\n  ];", start)
    assert end > start, "WINDOW_PREF_KEYS declaration is not terminated"
    return [(m.group("key"), m.group("consumer")) for m in _ENTRY_RE.finditer(src[start:end])]


class WindowPrefsHaveBackendConsumers(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = _window_pref_entries()
        self.quit_dialog = QUIT_DIALOG.read_text(encoding="utf-8")

    def test_the_page_declares_the_expected_window_prefs(self):
        keys = [k for k, _ in self.entries]
        self.assertEqual(
            sorted(keys),
            ["tray_close_to_tray", "tray_minimize_to_tray", "tray_start_minimized"],
            "the three window prefs must all be rendered (wire-or-delete: none of "
            "them may quietly disappear from the GUI either)",
        )

    def test_every_rendered_key_declares_a_consumer(self):
        for key, consumer in self.entries:
            self.assertTrue(
                consumer.strip(),
                f"pref '{key}' renders without naming the Rust symbol that reads it",
            )

    def test_every_claimed_consumer_exists_and_is_read(self):
        for key, consumer in self.entries:
            self.assertIn(
                f"const {consumer}",
                self.quit_dialog,
                f"pref '{key}' claims consumer {consumer}, which is not defined in "
                f"{QUIT_DIALOG.name}",
            )
            # Defined AND used: a constant that is never read is exactly what a
            # dead toggle looks like once someone adds a plausible-sounding name.
            self.assertGreaterEqual(
                self.quit_dialog.count(consumer),
                2,
                f"{consumer} is declared but never read — pref '{key}' would still "
                f"be a dead toggle",
            )

    def test_every_rendered_key_reaches_the_rust_side(self):
        for key, _ in self.entries:
            self.assertIn(
                key,
                self.quit_dialog,
                f"the GUI persists '{key}' but the string never appears in "
                f"{QUIT_DIALOG.name}",
            )

    def test_the_write_command_accepts_every_rendered_key(self):
        """`set_tray_window_pref` rejects unknown keys (by design). A key the
        GUI renders but the command refuses would error on every click."""
        start = self.quit_dialog.find("pub async fn set_tray_window_pref(")
        self.assertGreater(start, 0, "set_tray_window_pref not found")
        body = self.quit_dialog[start : start + 2000]
        # Arms are either the legacy-key constants or a literal.
        arm_constants = {
            "tray_close_to_tray": "LEGACY_SETTING_CLOSE_TO_TRAY",
            "tray_start_minimized": "LEGACY_SETTING_START_MINIMIZED",
        }
        for key, _ in self.entries:
            arm = arm_constants.get(key, f'"{key}"')
            self.assertIn(
                arm,
                body,
                f"set_tray_window_pref has no match arm for '{key}' — the GUI "
                f"toggle would fail with 'unknown tray window preference'",
            )

    def test_the_command_pair_is_registered_with_tauri(self):
        lib_rs = LIB_RS.read_text(encoding="utf-8")
        for cmd in ("quit_dialog::get_tray_window_prefs", "quit_dialog::set_tray_window_pref"):
            self.assertIn(
                cmd,
                lib_rs,
                f"{cmd} is not in the invoke_handler — the GUI could not call it",
            )

    def test_the_consumerless_generic_settings_block_does_not_return(self):
        """The removed block persisted arbitrary keys to per-project
        `module_settings` rows that nothing ever read (that is how
        `logging_level` and `default_embedding_mode` shipped as no-ops, and how
        `auto_update_enabled` came to contradict the real toggle on
        Preferences → Updates). Window prefs now go through their own command;
        nothing on this page may write settings generically again."""
        src = PREFS_PAGE.read_text(encoding="utf-8")
        code = src.split("</script>")[0]
        self.assertNotIn(
            "'set_setting_v2'",
            code,
            "the Preferences page must not persist settings through the generic "
            "per-project setting writer",
        )


if __name__ == "__main__":
    unittest.main()
