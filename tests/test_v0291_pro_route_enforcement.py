# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.91 P2-B4 (plan decision #28) — the Pro-gated routes must enforce.

Pre-fix, `Sidebar.svelte`'s `proOnly` flag gated the sidebar *link* and
nothing else, under a comment claiming otherwise:

    "Server-side gates back this client-side hint: a free user clicking a
     proOnly link gets redirected to /store, and the underlying Tauri
     commands the page would call also enforce tier."

Both halves of that sentence were false for both routes. `/coordination`
(five commands, including `coordination_apply_schema`, which the page's own
copy calls destructive) and `/hub` (four commands) contained ZERO tier
references, so a free-tier user reaching either by typed URL, bookmark or
back-button got the complete working feature.

The fix has two halves, and a unit test can only see one of them — the Rust
tests in `commands/coordination.rs` and `commands/hub_proxy.rs` pin what the
gate DECIDES per tier; this module pins that every command actually CALLS it
and that each route ships its deny layout. A new `#[command]` added to either
module without the gate fails here, which is the failure mode that produced
the finding in the first place.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.common.rust_source import cfg_test_line_numbers

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SRC = REPO_ROOT / "launcher" / "src"
COMMANDS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"

SIDEBAR = LAUNCHER_SRC / "lib" / "components" / "Sidebar.svelte"
COORDINATION_RS = COMMANDS / "coordination.rs"
HUB_PROXY_RS = COMMANDS / "hub_proxy.rs"
DASHBOARD_RS = COMMANDS / "dashboard.rs"

# `hub_proxy.rs` also hosts the Preferences boot-autostart pair. Those are
# NOT part of the `/hub` route surface and must stay reachable on the free
# tier — gating them would break a free user's ability to manage the hub
# service they actually run.
HUB_UNGATED_BY_DESIGN = {"get_hub_boot_autostart", "set_hub_boot_autostart"}

# NIT-8: the locator must accept BOTH attribute spellings tauri commands can
# carry (`#[command]` via `use tauri::command;`, or the unimported
# `#[tauri::command]` form), plus any attribute interposed between the
# command marker and the `pub fn` line (e.g. a future `#[allow(...)]`) — a
# command that used either shape the ORIGINAL regex didn't parse would drop
# out of the denominator and ship ungated while the gate stayed green.
_COMMAND_ATTR = r"#\[(?:tauri::)?command\]"
_COMMAND_RE = re.compile(
    _COMMAND_ATTR + r"\s*\n" r"(?:#\[[^\]]*\]\s*\n)*" r"pub (?:async )?fn (?P<name>\w+)\s*\(",
    re.MULTILINE,
)

# A structurally independent second signal for the self-check below: a plain
# per-line count of command-attribute lines, with none of `_COMMAND_RE`'s
# fn-matching or interposed-attribute logic. If this count and
# `_COMMAND_RE`'s match count ever diverge, the locator is dropping a
# `#[command]`/`#[tauri::command]`-attributed function (KG:
# source-text-gates-fail-toward-green — "a source-text gate must carry
# self-checks asserting it still SEES the constructs it polices").
_COMMAND_ATTR_LINE_RE = re.compile(r"^\s*" + _COMMAND_ATTR + r"\s*$", re.MULTILINE)


def _plain_command_attribute_count(src: str) -> int:
    return len(_COMMAND_ATTR_LINE_RE.findall(src))


def _command_bodies_from_src(src: str) -> dict[str, str]:
    """Map `#[command]`/`#[tauri::command]` fn name → its body text (up to the
    next top-level match, or EOF)."""
    out: dict[str, str] = {}
    matches = list(_COMMAND_RE.finditer(src))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        out[m.group("name")] = src[m.start() : end]
    return out


def _production_source(src: str) -> str:
    """`src` with every `#[cfg(test)]`-gated ITEM blanked out, line-for-line.

    Two things have to be true at once here, and until v0.2.95 this file got
    the second one wrong:

    1. **Test-module text must not reach a command body.** `_command_bodies_*`
       slices each command's body up to the NEXT command, so a trailing
       `#[cfg(test)] mod tests` lands inside the LAST command's slice — and
       that module's assertions name the commands and quote `require_tier(`.
       A gate whose whole job is "this command calls `require_tier`" would
       then be satisfied by the TEST that says it should. That is the hazard
       the original `src.find("\\n#[cfg(test)]")` truncation was written for,
       and it is real.

    2. **Production code AFTER a gated item must stay visible.** Truncating at
       the FIRST marker assumes the test module is last. The moment a file
       gates a mid-file helper, every command below it disappears from the
       scan — silently, and toward GREEN, because a command that is not seen
       cannot be reported as ungated. A concurrent v0.2.95 lane hit exactly
       this shape in `git_user_editable_merge.rs` (a `#[cfg(test)]` helper
       near the top hid a production emitter 3 000 lines below it), and it is
       the same v0.2.90 lesson `tests/common/rust_source.py` was extracted to
       hold.

    So the span question is answered by the ONE home for it —
    `cfg_test_line_numbers`, per-item and brace-balanced, with the Rust lexer
    that knows a brace inside a string is not a brace — and the gated lines
    are BLANKED rather than removed so line-for-line correspondence (and the
    ordering `_command_bodies_from_src` slices by) survives.

    `include_any_test` stays at its strict default: `#[cfg(any(test,
    debug_assertions))]` compiles into a debug build, so such a command is
    reachable by a user running one and must keep being policed.
    """
    gated = cfg_test_line_numbers(src)
    if not gated:
        return src
    return "\n".join(
        "" if n in gated else line
        for n, line in enumerate(src.splitlines(), start=1)
    )


def _command_bodies(path: Path) -> dict[str, str]:
    """Map `#[command] fn name` → its body text (up to the next top-level `}`)."""
    return _command_bodies_from_src(
        _production_source(path.read_text(encoding="utf-8"))
    )


def _pro_only_routes(sidebar_src: str) -> set[str]:
    """Every `href` in `Sidebar.svelte`'s nav marked `proOnly: true`."""
    routes: set[str] = set()
    for block in re.finditer(
        r"\{\s*href:\s*'(?P<href>[^']+)'.*?\}", sidebar_src, re.DOTALL
    ):
        text = block.group(0)
        if "proOnly: true" in text:
            routes.add(block.group("href"))
    return routes


class ProOnlyRoutesShipADenyLayout(unittest.TestCase):
    """Half 1 — the client deny screen, on the route itself (not the link)."""

    def setUp(self) -> None:
        self.routes = _pro_only_routes(SIDEBAR.read_text(encoding="utf-8"))

    def test_the_pro_only_set_is_the_expected_two_routes(self) -> None:
        # If a third proOnly route appears, it needs both halves too —
        # this assertion is the trip-wire that makes that explicit.
        self.assertEqual(
            self.routes,
            {"/coordination", "/hub"},
            "the proOnly route set changed; give the new route a deny layout "
            "+ server-side tier checks before updating this expectation",
        )

    def test_every_pro_only_route_has_a_gated_layout(self) -> None:
        for href in sorted(self.routes):
            layout = LAUNCHER_SRC / "routes" / href.lstrip("/") / "+layout.svelte"
            self.assertTrue(
                layout.is_file(),
                f"{href} has no +layout.svelte — a typed URL bypasses the sidebar gate",
            )
            src = layout.read_text(encoding="utf-8")
            self.assertIn(
                "ProRouteGate",
                src,
                f"{layout} must render the shared deny screen",
            )
            self.assertRegex(
                src,
                r"<ProRouteGate[^>]*feature=",
                f"{layout} must name the gated feature for the deny copy",
            )

    def test_the_deny_screen_derives_from_the_shared_predicate(self) -> None:
        gate = LAUNCHER_SRC / "lib" / "components" / "ProRouteGate.svelte"
        src = gate.read_text(encoding="utf-8")
        self.assertIn("hasProTier", src)
        self.assertIn("$lib/license-gate", src)
        # ...and the sidebar uses the SAME predicate rather than an inline
        # copy of the tier list (the shape that let the two drift).
        sidebar = SIDEBAR.read_text(encoding="utf-8")
        self.assertIn("hasProTier", sidebar)


class ProOnlyCommandSurfacesReCheckTier(unittest.TestCase):
    """Half 2 — the server-side gate, per command."""

    def test_every_coordination_command_calls_the_tier_gate(self) -> None:
        bodies = _command_bodies(COORDINATION_RS)
        self.assertGreaterEqual(
            len(bodies), 5, "expected the five coordination commands"
        )
        for name, body in sorted(bodies.items()):
            self.assertIn(
                "require_tier(",
                body,
                f"{name} does not re-check the orchestrator tier — a free-tier "
                f"caller reaching /coordination by typed URL would run it",
            )

    def test_the_destructive_apply_schema_gates_before_it_acts(self) -> None:
        body = _command_bodies(COORDINATION_RS)["coordination_apply_schema"]
        gate_at = body.index("require_tier(")
        for later in ("secrets::get(", "Command::new("):
            if later in body:
                self.assertLess(
                    gate_at,
                    body.index(later),
                    "the destructive command must refuse BEFORE reading a "
                    "secret or spawning a process",
                )

    def test_the_hub_route_command_surface_calls_the_tier_gate(self) -> None:
        bodies = _command_bodies(HUB_PROXY_RS)
        route_commands = {
            n: b for n, b in bodies.items() if n not in HUB_UNGATED_BY_DESIGN
        }
        self.assertEqual(
            set(route_commands),
            {"hub_info", "hub_list_apps", "hub_poll_messages", "hub_data_catalog"},
            "the /hub route command surface changed",
        )
        for name, body in sorted(route_commands.items()):
            self.assertIn("require_tier(", body, f"{name} does not re-check tier")

    def test_the_preferences_boot_autostart_pair_stays_ungated(self) -> None:
        # Leave-alone: the gate follows the ROUTE, not the module. These two
        # back a free-tier Preferences toggle.
        bodies = _command_bodies(HUB_PROXY_RS)
        for name in sorted(HUB_UNGATED_BY_DESIGN):
            self.assertIn(name, bodies, f"{name} disappeared from hub_proxy.rs")
            self.assertNotIn(
                "require_tier(",
                bodies[name],
                f"{name} backs the free-tier Preferences toggle and must NOT "
                f"be tier-gated",
            )

    def test_the_gate_is_the_existing_shared_helper(self) -> None:
        # A>B>C: one implementation, not a per-module re-derivation of the
        # tier ladder.
        dashboard = DASHBOARD_RS.read_text(encoding="utf-8")
        self.assertIn("pub(crate) fn require_tier(", dashboard)
        self.assertIn("pub(crate) fn require_tier_for_slug(", dashboard)
        self.assertIn("tier_required_message(min_tier, feature)", dashboard)
        for path in (COORDINATION_RS, HUB_PROXY_RS):
            src = path.read_text(encoding="utf-8")
            self.assertIn(
                "use crate::commands::dashboard::require_tier;",
                src,
                f"{path.name} must import the shared gate, not roll its own",
            )
            self.assertIn(
                'const MIN_TIER: &str = "pro";',
                src,
                f"{path.name}'s floor must match Sidebar's proOnly flag",
            )


class CommandLocatorSelfCheck(unittest.TestCase):
    """NIT-8 — the locator's match count must agree with an independent count
    of command-attribute lines, and must accept the `#[tauri::command]`
    spelling (not just the imported `#[command]` alias)."""

    def test_command_count_matches_a_regex_independent_line_count(self) -> None:
        for path in (COORDINATION_RS, HUB_PROXY_RS):
            # Same production view as `_command_bodies` — one answer to "where
            # does the test code stop", not a second one that can disagree.
            src = _production_source(path.read_text(encoding="utf-8"))
            self.assertEqual(
                len(_command_bodies_from_src(src)),
                _plain_command_attribute_count(src),
                f"{path.name}: _COMMAND_RE's match count diverges from a "
                f"plain count of #[command]/#[tauri::command] lines — the "
                f"locator dropped (or double-counted) a command",
            )

    def test_the_locator_accepts_the_fully_qualified_tauri_command_form(
        self,
    ) -> None:
        synthetic = (
            "#[tauri::command]\n"
            "pub async fn synthetic_command(db: State<'_, Db>) -> Result<(), String> {\n"
            "    Ok(())\n"
            "}\n"
        )
        bodies = _command_bodies_from_src(synthetic)
        self.assertIn("synthetic_command", bodies)
        self.assertIn("Ok(())", bodies["synthetic_command"])

    def test_the_locator_accepts_an_interposed_attribute(self) -> None:
        synthetic = (
            "#[command]\n"
            "#[allow(clippy::too_many_arguments)]\n"
            "pub fn another_synthetic_command() -> Result<(), String> {\n"
            "    Ok(())\n"
            "}\n"
        )
        bodies = _command_bodies_from_src(synthetic)
        self.assertIn("another_synthetic_command", bodies)


class TheProductionViewKeepsBothProperties(unittest.TestCase):
    """v0.2.95 — the two things `_production_source` has to do at once.

    Both arms are here because they pull in OPPOSITE directions: cut too much
    and a production command vanishes (the gate silently stops policing it);
    cut too little and a test module's own text satisfies the gate FOR the
    command it is testing. `#[cfg(test)]` is not a file terminator, so neither
    property can be had by position alone.

    Synthetic sources, deliberately: `coordination.rs` and `hub_proxy.rs`
    happen to gate their test module LAST today, so a real-file test would
    pass under the old truncation too and prove nothing.
    """

    #: A gated item in the MIDDLE. `first` is gated on nothing; the helper
    #: module in between is; `second` is production code the old truncation
    #: could not see.
    MID_FILE_GATE = (
        "use tauri::command;\n"
        "\n"
        "#[command]\n"
        "pub fn first_production_command() -> Result<(), String> {\n"
        "    require_tier(MIN_TIER, \"coordination\")?;\n"
        "    Ok(())\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod helpers {\n"
        "    pub fn fixture() -> &'static str {\n"
        "        \"require_tier( lives in this fixture string, and a { brace }\"\n"
        "    }\n"
        "}\n"
        "\n"
        "#[command]\n"
        "pub fn second_production_command() -> Result<(), String> {\n"
        "    Ok(())\n"
        "}\n"
    )

    #: The ORIGINAL hazard: a trailing test module whose assertions name the
    #: gate. Nothing in `ungated_command` calls `require_tier`.
    TRAILING_TEST_MODULE = (
        "#[command]\n"
        "pub fn ungated_command() -> Result<(), String> {\n"
        "    Ok(())\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn the_command_is_gated() {\n"
        "        assert!(SRC.contains(\"require_tier(\"));\n"
        "    }\n"
        "}\n"
    )

    def test_a_command_after_a_gated_item_is_still_located(self) -> None:
        bodies = _command_bodies_from_src(
            _production_source(self.MID_FILE_GATE)
        )
        self.assertIn(
            "second_production_command",
            bodies,
            "a `#[command]` below a mid-file `#[cfg(test)]` item fell out of "
            "the scan — an ungated Pro command there would ship unnoticed",
        )
        self.assertIn("first_production_command", bodies)

    def test_the_gated_items_text_never_reaches_a_command_body(self) -> None:
        bodies = _command_bodies_from_src(
            _production_source(self.TRAILING_TEST_MODULE)
        )
        self.assertNotIn(
            "require_tier(",
            bodies["ungated_command"],
            "the trailing test module's text landed in the command's body — "
            "the gate would be satisfied by the test that asserts it",
        )

    def test_the_file_entry_point_shares_the_view(self) -> None:
        """`_command_bodies` (path in, bodies out) is what the gates call —
        pin the fix there, not only on the pure helper."""
        with tempfile.TemporaryDirectory() as td:
            synthetic = Path(td) / "synthetic_commands.rs"
            synthetic.write_text(self.MID_FILE_GATE, encoding="utf-8")
            bodies = _command_bodies(synthetic)
        self.assertIn("second_production_command", bodies)

    def test_the_self_check_counts_commands_below_a_gated_item(self) -> None:
        """The locator self-check runs on the same view, so its denominator
        includes commands below a gated item."""
        src = _production_source(self.MID_FILE_GATE)
        self.assertEqual(
            len(_command_bodies_from_src(src)),
            2,
            "the self-check's denominator lost a command to the test gate",
        )
        self.assertEqual(_plain_command_attribute_count(src), 2)


class SidebarClaimIsTrue(unittest.TestCase):
    """The comment that asserted enforcement must name the real mechanism."""

    def test_the_has_pro_comment_points_at_both_halves(self) -> None:
        src = SIDEBAR.read_text(encoding="utf-8")
        start = src.index("const hasPro")
        # The doc comment sits directly above the derivation.
        comment = src[max(0, start - 1400) : start]
        self.assertIn(
            "+layout.svelte",
            comment,
            "the comment must name the route deny layouts it relies on",
        )
        self.assertIn(
            "require_tier",
            comment,
            "the comment must name the server-side gate it claims exists",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
