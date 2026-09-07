# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.90 invariant: no bare ``tokio::spawn`` in sync fns of the launcher crates.

THE INCIDENT (v0.2.89): the boot-resume path added a heartbeat-staleness
sweeper whose sync entry point called ``tokio::spawn``. ``resume_pending_syncs``
runs inside Tauri's ``setup()`` on the main thread, where no tokio reactor
context exists — the spawn panicked ("there is no reactor running, must be
called from the context of a Tokio 1.x runtime") and the launcher died at
every boot, before the window existed. The same latent class sat in the
``spawn_initial_*`` / ``spawn_setup_task`` entry points: sync fns reachable
from ``setup()`` whenever pending rows exist at boot.

THE RULE: detached tasks in the launcher crates spawn via
``tauri::async_runtime::spawn`` (lazy global runtime — works from ANY thread,
including the main thread during ``setup()`` and tray/window-event callbacks).
A bare ``tokio::spawn`` — or any sibling that resolves ``Handle::current()``:
``tokio::task::spawn``, ``spawn_blocking``, ``spawn_local`` — is only
legitimate inside an ``async fn`` body (which by construction runs on the
runtime) or in test code (``#[tokio::test]`` provides a reactor — which is
exactly why unit tests structurally CANNOT catch this class, and why this
source-level scan exists instead).

TEST-CODE EXCLUSION: every ``#[cfg(test)]``-gated item is skipped
INDIVIDUALLY (attributes, then one semicolon-terminated item or one
brace-balanced block — fn, const, use, mod alike) and scanning RESUMES after
it. Many files gate a mid-file test helper or an interior test mod and then
continue with production code; a first-marker file cutoff would silently
blind the scan to everything after it (found in review of the first version
of this test: ~17 files, including boot-relevant sweeps).

That skip, and the Rust lexer it needs, live in ``tests/common/rust_source.py``
— the shared home for the three ``.rs`` architectural lints (v0.2.92; see that
module's docstring). The private copy this file used to carry stripped only
``//`` tails, so a brace inside a multi-line string literal was counted as
code: on the real tree it ended four ``#[cfg(test)] mod`` spans EARLY
(``config.rs`` at line 317 of 416, ``secrets.rs`` at 3337 of 4559,
``secrets_ss_connection.rs`` at 650 of 1059) and ran one to EOF
(``db/access.rs``), so the scan was reading test code as production in the
first three and skipping ~17 lines of production in the last.

STRICT test-gate reading (``include_any_test=False``): an item is skipped only
when it is absent from EVERY non-test build. ``#[cfg(all(unix, any(test,
debug_assertions)))]`` — which ``secrets.rs`` uses — compiles into a debug
binary, so its ``tokio::spawn`` calls could panic in a developer's launcher
exactly like a release one; this scan must keep seeing them.

Scope: ``launcher/src-tauri/src`` (the Tauri app — has a non-runtime main
thread) and ``launcher/src-tauri/vct-launcher-core/src`` (library consumed by
the app, so its sync fns can be called from the same contexts). ``vct-hub``
is deliberately out of scope: it runs under ``#[tokio::main]``, so every call
path there is inside the runtime. ``vct-updater`` has no tokio dependency.

False-positive escape hatch: a ``tokio::spawn`` nested inside an async block
that itself runs on the runtime (e.g. inside a ``tauri::async_runtime::spawn``
closure in a sync fn) would trip this scan even though it is safe at runtime.
If you genuinely need that shape, extract the async body into an ``async fn``
— which both satisfies the scan and makes the execution context explicit.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.rust_source import (  # noqa: E402
    cfg_test_line_numbers,
    scrub_rust_lines,
)

SCAN_ROOTS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src",
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src",
)

# Rust fn-qualifier order: pub(...) default const async unsafe extern "abi" fn
_FN_DECL = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(async\s+)?"
    r"(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(\w+)"
)
_BARE_SPAWN = re.compile(r"\btokio::(?:task::)?spawn(?:_blocking|_local)?\(")


def _scan_file(path: Path) -> list[str]:
    """Return violation descriptions for one .rs file."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    # Code-only view: strings, char literals and comments removed with
    # cross-line lexer state. Both the spawn match and the enclosing-fn
    # lookup read THIS, so a `tokio::spawn(` inside a doc comment or an
    # error-message template cannot be mistaken for a call.
    code = scrub_rust_lines(source)
    # STRICT reading — see the module docstring: an item gated behind a
    # predicate that still compiles in a debug build is production here.
    skipped = cfg_test_line_numbers(source, include_any_test=False)
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        rel = path

    violations: list[str] = []
    for i in range(len(lines)):
        if (i + 1) in skipped:
            continue
        if not _BARE_SPAWN.search(code[i]):
            continue
        # Nearest preceding fn declaration decides the context.
        enclosing_async = None
        enclosing_name = "<module scope>"
        for back in range(i, -1, -1):
            m = _FN_DECL.match(code[back])
            if m:
                enclosing_async = bool(m.group(1))
                enclosing_name = m.group(2)
                break
        if enclosing_async is False:
            violations.append(
                f"{rel}:{i + 1}: bare tokio spawn in SYNC fn "
                f"`{enclosing_name}` — panics when called without a "
                f"reactor context (setup()/main thread; v0.2.89 boot "
                f"incident). Use tauri::async_runtime::spawn."
            )
    return violations


class NoBareTokioSpawnInSyncFns(unittest.TestCase):
    def test_scan_roots_exist_and_are_nonempty(self) -> None:
        """Guard the scan itself against path drift going silently green."""
        total = 0
        for root in SCAN_ROOTS:
            self.assertTrue(root.is_dir(), f"scan root missing: {root}")
            total += sum(1 for _ in root.rglob("*.rs"))
        self.assertGreater(
            total, 30, "suspiciously few .rs files — scan roots drifted?"
        )

    def test_no_bare_tokio_spawn_in_sync_fns(self) -> None:
        violations: list[str] = []
        for root in SCAN_ROOTS:
            for rs in sorted(root.rglob("*.rs")):
                violations.extend(_scan_file(rs))
        self.assertEqual(
            violations,
            [],
            "bare tokio spawn in sync fn(s):\n" + "\n".join(violations),
        )


class ScannerBehavior(unittest.TestCase):
    """The scanner's own contract — each case red-proofs a reviewed gap."""

    def _scan_source(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe.rs"
            p.write_text(source, encoding="utf-8")
            return _scan_file(p)

    def test_flags_bare_spawn_in_sync_fn(self) -> None:
        v = self._scan_source(
            "pub fn spawn_thing() {\n    tokio::spawn(async move {});\n}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn("spawn_thing", v[0])

    def test_allows_spawn_in_async_fn_and_async_runtime_anywhere(self) -> None:
        v = self._scan_source(
            "async fn worker() {\n    tokio::spawn(async move {});\n}\n"
            "pub fn boot() {\n"
            "    tauri::async_runtime::spawn(async move {});\n"
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_cfg_test_item_does_not_blind_the_rest_of_the_file(self) -> None:
        """A mid-file #[cfg(test)] helper must not exempt later prod code —
        the first version of this scan cut the whole file at the first
        marker and left ~17 files partially unscanned."""
        v = self._scan_source(
            "#[cfg(test)]\nfn test_helper() {\n    tokio::spawn(async {});\n}\n"
            "pub fn prod_entry() {\n    tokio::spawn(async move {});\n}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn("prod_entry", v[0])

    def test_cfg_test_mod_and_semicolon_items_are_skipped(self) -> None:
        v = self._scan_source(
            "#[cfg(test)]\nmod tests {\n"
            "    fn helper() {\n        tokio::spawn(async {});\n    }\n"
            "}\n"
            "#[cfg(test)]\nmod more_tests;\n"
            "#[cfg(test)]\nuse std::fs;\n"
        )
        self.assertEqual(v, [], v)

    def test_sibling_spawn_forms_are_flagged(self) -> None:
        v = self._scan_source(
            "fn a() { tokio::task::spawn(async {}); }\n"
            "fn b() { tokio::spawn_blocking(|| {}); }\n"
            "fn c() { tokio::task::spawn_local(async {}); }\n"
        )
        self.assertEqual(len(v), 3, v)

    def test_debug_assertions_gate_is_still_scanned(self) -> None:
        """STRICT reading: `any(test, debug_assertions)` is NOT test-only.

        The item compiles into a debug build, so its spawn can panic in a
        developer's launcher exactly like a release one. This is the case the
        shared module's first strict rule got wrong (it asked
        ``predicate.startswith("all(")``), and the case that decides whether
        migrating this lint onto that module preserved its gate: the private
        copy this file used to carry matched only the literal
        ``#[cfg(test)]``, so it scanned these regions.
        """
        v = self._scan_source(
            "#[cfg(all(unix, any(test, debug_assertions)))]\n"
            "pub fn debug_helper() {\n    tokio::spawn(async {});\n}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn("debug_helper", v[0])

        v = self._scan_source(
            "#[cfg(any(test, debug_assertions))]\n"
            "pub fn debug_helper2() {\n    tokio::spawn(async {});\n}\n"
        )
        self.assertEqual(len(v), 1, v)

    def test_all_test_and_whitespace_variants_are_skipped(self) -> None:
        """The other side of the same knob: a predicate that IS absent from
        every non-test build is skipped, including forms the old literal
        `#[cfg(test)]` regex missed (`all(test, …)`, spaced attributes)."""
        for gate in (
            "#[cfg(all(test, unix))]",
            "#[cfg(all(unix, test))]",
            "#[ cfg ( test ) ]",
        ):
            with self.subTest(gate=gate):
                v = self._scan_source(
                    f"{gate}\nfn helper() {{\n    tokio::spawn(async {{}});\n}}\n"
                )
                self.assertEqual(v, [], v)

    def test_string_literal_braces_do_not_end_the_skip_early(self) -> None:
        """The private copy counted braces on a `//`-stripped view, so a brace
        inside a string literal unbalanced it and ended a `#[cfg(test)]` span
        early — on the real tree that mis-read four files (see module
        docstring). The shared lexer removes literals first."""
        v = self._scan_source(
            "#[cfg(test)]\nmod tests {\n"
            '    const SNIPPET: &str = "fn main() {";\n'
            "    fn helper() {\n        tokio::spawn(async {});\n    }\n"
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_comment_mentions_are_ignored(self) -> None:
        v = self._scan_source(
            "/// doc says tokio::spawn(run_task) here\n"
            "pub fn documented() {\n"
            "    let x = 1; // historic tokio::spawn(...) note\n"
            "    let _ = x;\n"
            "}\n"
        )
        self.assertEqual(v, [], v)


if __name__ == "__main__":
    unittest.main()
