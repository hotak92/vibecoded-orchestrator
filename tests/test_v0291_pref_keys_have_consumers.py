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
# v0.2.91 WP-L — the log-level pref's consumer chain.
CORE_LOGGING = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "logging.rs"
)
APP_LOGGING = REPO_ROOT / "launcher" / "src-tauri" / "src" / "logging.rs"
LOGGING_PREFS_CMD = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "logging_prefs.rs"
)
CONFIG_PROJECTION = REPO_ROOT / "vco_lib" / "config_projection.py"

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


class LoggingLevelPrefHasBackendConsumers(unittest.TestCase):
    """v0.2.91 WP-L — the same wire-or-delete check, for the ONE pref this
    release RE-ADDS to this page.

    `logging_level` (underscore) was deleted above for having no consumer.
    Re-adding a level picker is only defensible because the consumers now
    exist, so every link of the chain gets an assertion here — the cheapest
    possible insurance against the exact regression this module was written
    for. The chain is:

        Preferences <Dropdown>
          → `set_logging_level` (dedicated command: validates, applies to the
            running process, re-projects env)
          → app_state `logging.level`
          → `vct_launcher_core::logging::resolve_log_level` (launcher + hub)
          → `vco_lib/config_projection.py` → `.claude/env` VCO_LOG_LEVEL
    """

    def setUp(self) -> None:
        self.page = PREFS_PAGE.read_text(encoding="utf-8")
        self.script = self.page.split("</script>")[0]

    def test_the_page_names_the_rust_symbol_that_reads_the_pref(self):
        m = re.search(r"LOG_LEVEL_CONSUMER = '([^']+)'", self.script)
        self.assertIsNotNone(
            m,
            "the log-level section must name the Rust symbol that reads it, "
            "the same `consumer:` discipline the window prefs carry",
        )
        assert m is not None  # for type checkers
        self.consumer = m.group(1)

    def test_the_claimed_consumer_exists_and_is_read(self):
        m = re.search(r"LOG_LEVEL_CONSUMER = '([^']+)'", self.script)
        assert m is not None, "consumer claim missing (see the test above)"
        consumer = m.group(1)
        core = CORE_LOGGING.read_text(encoding="utf-8")
        self.assertIn(
            f"pub fn {consumer}",
            core,
            f"the page claims consumer {consumer}, which is not defined in "
            f"{CORE_LOGGING.name}",
        )
        # Defined AND used elsewhere: a resolver nothing calls is a dead
        # toggle wearing a plausible name.
        app = APP_LOGGING.read_text(encoding="utf-8")
        self.assertIn(
            consumer,
            app,
            f"{consumer} is never called from {APP_LOGGING.name} — the "
            "launcher would persist a level it never applies",
        )

    def test_the_pref_writes_the_key_the_consumer_reads(self):
        """One key literal, asserted on both sides. The legacy `logging_level`
        name must not come back: reusing it would resurrect stale values
        written while it was a no-op."""
        core = CORE_LOGGING.read_text(encoding="utf-8")
        m = re.search(
            r'pub const LOG_LEVEL_APP_STATE_KEY\s*:\s*&str\s*=\s*"([^"]+)"', core
        )
        self.assertIsNotNone(m, "LOG_LEVEL_APP_STATE_KEY not found in core logging")
        assert m is not None
        key = m.group(1)
        self.assertEqual(key, "logging.level")
        self.assertNotEqual(key, "logging_level")
        cmd = LOGGING_PREFS_CMD.read_text(encoding="utf-8")
        self.assertIn(
            "LOG_LEVEL_APP_STATE_KEY",
            cmd,
            "the write command must address the key through the shared "
            "constant, not a literal that can drift from the reader",
        )

    def test_the_command_pair_is_registered_with_tauri(self):
        lib_rs = LIB_RS.read_text(encoding="utf-8")
        for cmd in (
            "logging_prefs::get_logging_level",
            "logging_prefs::set_logging_level",
        ):
            self.assertIn(
                cmd,
                lib_rs,
                f"{cmd} is not in the invoke_handler — the GUI could not call it",
            )

    def test_the_page_invokes_that_command_pair(self):
        for cmd in ("'get_logging_level'", "'set_logging_level'"):
            self.assertIn(
                cmd,
                self.script,
                f"the page must go through {cmd}; the generic app_state "
                "writer would skip validation AND the env re-projection",
            )

    def test_the_write_command_accepts_every_offered_level(self):
        """A level the picker offers but the command refuses would error on
        every click — the same lie with extra steps."""
        cmd = LOGGING_PREFS_CMD.read_text(encoding="utf-8")
        m = re.search(r"LOG_LEVELS:\s*\[&str;\s*4\]\s*=\s*\[([^\]]+)\]", cmd)
        self.assertIsNotNone(m, "LOG_LEVELS not found in logging_prefs.rs")
        assert m is not None
        accepted = set(re.findall(r'"([^"]+)"', m.group(1)))
        offered = set(
            re.findall(r"\{\s*value:\s*'([^']+)'", self.script.split("LOG_LEVEL_OPTIONS")[1])
        )
        self.assertTrue(offered, "no LOG_LEVEL_OPTIONS parsed from the page")
        self.assertTrue(
            offered <= accepted,
            f"the page offers {sorted(offered - accepted)}, which "
            f"set_logging_level would refuse (accepts {sorted(accepted)})",
        )

    def test_the_level_is_not_smuggled_into_the_window_pref_array(self):
        """WINDOW_PREF_KEYS is asserted elsewhere to hold exactly the three
        tray prefs. This pref has its own command pair and its own section —
        it must not be bolted onto that array."""
        keys = [k for k, _ in _window_pref_entries()]
        self.assertNotIn("logging_level", keys)
        self.assertNotIn("logging.level", keys)

    def test_the_projection_carries_the_pref_to_projects(self):
        """A pref the launcher stores but never projects would leave every
        hook and helper script on the default — wired at one end only."""
        proj = CONFIG_PROJECTION.read_text(encoding="utf-8")
        self.assertIn('APP_STATE_KEY_LOGGING_LEVEL = "logging.level"', proj)
        self.assertIn('_ENV_LOGGING_LEVEL = "VCO_LOG_LEVEL"', proj)
        self.assertIn(
            "SHELL_DEFAULTED_ENV_KEYS",
            proj,
            "the level must ride the shell-defaulted channel — a canonical "
            "key would clobber an operator's own VCO_LOG_LEVEL export",
        )

    def test_the_page_says_where_the_pref_does_and_does_not_reach(self):
        """The pref deliberately does NOT reach MCP servers (projecting it
        into `.claude/settings.json` env would overwrite an operator's
        export for exactly the processes they are debugging). A control that
        stays silent about its own reach is the shipped-lie shape in a
        subtler form, so the hint must say so."""
        # The rendered hint lives after the </script> block.
        markup = self.page.split("</script>", 1)[1]
        section = markup.split("Diagnostic log level", 1)
        self.assertEqual(
            len(section), 2, "the Diagnostic log level section is not rendered"
        )
        hint = section[1][:2000]
        self.assertIn("VCO_LOG_LEVEL", hint)
        self.assertIn("MCP", hint, "the hint must state the MCP-server carve-out")
        self.assertIn(
            "hub",
            hint,
            "the hint must state that the hub adopts the level on its next "
            "start rather than immediately",
        )


if __name__ == "__main__":
    unittest.main()
