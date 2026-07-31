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
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src",
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src",
)

# Rust fn-qualifier order: pub(...) default const async unsafe extern "abi" fn
_FN_DECL = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(async\s+)?"
    r"(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(\w+)"
)
_CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
_ATTR = re.compile(r"^\s*#\[")
_BARE_SPAWN = re.compile(r"\btokio::(?:task::)?spawn(?:_blocking|_local)?\(")


def _strip_line_comment(line: str) -> str:
    return line.split("//", 1)[0]


def _skip_cfg_test_item(lines: list[str], i: int) -> int:
    """``lines[i]`` is a ``#[cfg(test)]`` attribute line. Return the index
    just past the gated item: any further attribute lines, then either a
    semicolon-terminated item (``mod tests;``, ``use ...;``, ``const ...;``)
    or one brace-balanced block (fn/mod/impl/struct alike). Brace counting
    ignores ``//`` line-comment tails; string literals containing braces
    inside test code could in principle skew it — acceptable for a scan
    whose failure mode is then a human-reviewed false positive/negative on
    one file, not silent whole-file blindness."""
    j = i + 1
    while j < len(lines) and _ATTR.match(lines[j]):
        j += 1
    depth = 0
    seen_open = False
    while j < len(lines):
        code = _strip_line_comment(lines[j])
        depth += code.count("{") - code.count("}")
        if "{" in code:
            seen_open = True
        if seen_open and depth <= 0:
            return j + 1
        if not seen_open and code.rstrip().endswith(";"):
            return j + 1
        j += 1
    return j


def _scan_file(path: Path) -> list[str]:
    """Return violation descriptions for one .rs file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        rel = path

    violations: list[str] = []
    i = 0
    while i < len(lines):
        if _CFG_TEST.match(lines[i]):
            i = _skip_cfg_test_item(lines, i)
            continue
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("//") and _BARE_SPAWN.search(
            _strip_line_comment(line)
        ):
            # Nearest preceding fn declaration decides the context.
            enclosing_async = None
            enclosing_name = "<module scope>"
            for back in range(i, -1, -1):
                m = _FN_DECL.match(lines[back])
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
        i += 1
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
