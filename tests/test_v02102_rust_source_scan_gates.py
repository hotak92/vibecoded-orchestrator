# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.102 (W6-Z2): the launcher's Rust source-scan gates, rebuilt on
per-item ``#[cfg(test)]`` blanking.

WHY THESE GATES MOVED HERE
--------------------------
Each gate below used to be a Rust ``#[test]`` that read a source file and cut
it at the FIRST occurrence of ``#[cfg(test)]`` (or ``mod tests``) to get "the
production code". That marker is very often not the test module:

* ``installer.rs`` gates a ``use`` at line 21, so the destructive-subprocess
  audit checked 20 lines of an ~8,700-line production file;
* launcher ``licensing.rs`` quotes the marker in a COMMENT on line 11, so the
  maintainer-token bypass audit scanned almost nothing;
* ``projects_v2.rs`` gates a helper at line 3895 and carries ~600 production
  lines AFTER its test module; ``storage_ux.rs`` gates a helper at line 2218;
* the launch-DDL census cut 20 files early on a gated non-module item, and
  missed 3 files' production code below their test module.

Every one of those gates failed toward GREEN, under comments claiming they
covered production code. The fix is ``tests/common/rust_source.production_source``:
blank each test-gated ITEM (its own ``;`` or balanced braces, found by a
Rust-aware lexer), keep everything else. Python was chosen over a Rust
helper because none of these gates needs Rust-only context — they are text
scans — and the Python span algorithm already exists, has its own suite, and
is shared by the other Rust lints (one home, not a second copy in Rust).

Needles and allowed exceptions are carried over verbatim from the Rust
originals; where a Rust file's comments are stripped, the Rust lexer
(``strip_rust_comments``) does it instead of the old "cut the line at the first
``//`` or ``#``" heuristic, which also cut real code at ``#[derive]``,
``r#"…"#`` and ``"#"``. Python and shell sources keep the original heuristic.
Every scanned file is REQUIRED to exist: the Rust originals skipped a missing
file silently, which turns a rename into a green gate.

Each gate has a mutation test that plants its needle in production code
BELOW the first gated item of the real file — exactly the span the old cut
could not see — and asserts the gate reports it.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Set, Tuple

from tests.common.rust_source import cfg_test_spans, production_source

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = REPO_ROOT / "launcher" / "src-tauri"
COMMANDS = SRC_TAURI / "src" / "commands"

INSTALL_PY = REPO_ROOT / "install.py"
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALLER_RS = COMMANDS / "installer.rs"
PROJECTS_V2_RS = COMMANDS / "projects_v2.rs"
STORAGE_UX_RS = COMMANDS / "storage_ux.rs"
LICENSING_RS = COMMANDS / "licensing.rs"
RESTART_RS = COMMANDS / "restart.rs"
CORE_LICENSING_RS = SRC_TAURI / "vct-launcher-core" / "src" / "licensing.rs"
VALIDATOR_PY = REPO_ROOT / "VCThelpers" / "license" / "validator.py"
BINARY_FRESHNESS_RS = SRC_TAURI / "src" / "services" / "binary_freshness.rs"
UPDATER_MAIN_RS = SRC_TAURI / "vct-updater" / "src" / "main.rs"
CORE_DB_MOD_RS = SRC_TAURI / "vct-launcher-core" / "src" / "db" / "mod.rs"


# ── shared text views ───────────────────────────────────────────────────────


def _read(path: Path) -> str:
    assert path.is_file(), (
        f"{path} is missing — a gate that skips a missing file passes "
        "vacuously; update the path here if the file moved"
    )
    return path.read_text(encoding="utf-8")


def _cut_line_comments(text: str) -> str:
    """The original Rust gates' comment heuristic, for Python/shell sources:
    each line cut at its first ``//``, else at its first ``#``."""
    out = []
    for line in text.split("\n"):
        cut = line.find("//")
        if cut == -1:
            cut = line.find("#")
        out.append(line if cut == -1 else line[:cut])
    return "\n".join(out)


def _strip_python_docstrings(text: str) -> str:
    """Blank ``\"\"\"…\"\"\"`` / ``'''…'''`` regions, newlines kept (an
    unterminated one runs to EOF), as the Rust ``strip_python_docstrings`` did.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        marker = text[i:i + 3]
        if marker in ('"""', "'''"):
            j = text.find(marker, i + 3)
            end = n if j == -1 else j + 3
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _rust_code(text: str) -> str:
    """Production code of a Rust source: test items blanked, comments gone,
    string literals verbatim."""
    return production_source(text, strip_comments=True)


def _script_code(text: str) -> str:
    """Production code of a Python/shell source, as the Rust gates read it."""
    return _cut_line_comments(_strip_python_docstrings(text))


def _code_view(path: Path, text: str) -> str:
    return _rust_code(text) if path.suffix == ".rs" else _script_code(text)


# ── the gates (pure: text in, findings out) ─────────────────────────────────

#: Bug 32 #1 + #2 — verbatim from installer.rs's
#: `test_no_destructive_subprocess_calls_in_install_path`.
DESTRUCTIVE_NEEDLES = (
    "podman volume rm",
    "docker volume rm",
    "podman volume prune",
    "docker volume prune",
    "compose down --volumes",
    "compose down -v",
    "podman-compose down",
    "docker-compose down",
)


def destructive_findings(sources: Dict[Path, str]) -> List[str]:
    """``path -> text`` → every forbidden destructive invocation found."""
    found = []
    for path, text in sources.items():
        code = _rust_code(text) if path.suffix == ".rs" else _cut_line_comments(text)
        for needle in DESTRUCTIVE_NEEDLES:
            if needle in code:
                found.append(f"{needle!r} in {path}")
    return found


VOLUME_RM_NEEDLES = ("volume rm", '"volume", "rm"')


def volume_rm_install_path_findings(sources: Dict[Path, str]) -> List[str]:
    """Bug 31 + Bug 32 #4, part 1: no install-path file invokes `volume rm`."""
    return [
        f"{needle!r} in {path}"
        for path, text in sources.items()
        for needle in VOLUME_RM_NEEDLES
        if needle in _code_view(path, text)
    ]


def _split_migrate_volumes(storage_src: str) -> Tuple[str, str]:
    """``(body of pub async fn migrate_volumes, everything else)`` in the
    production code of storage_ux.rs."""
    code = _rust_code(storage_src)
    fn_start = code.find("pub async fn migrate_volumes(")
    assert fn_start != -1, "migrate_volumes defined"
    body_open = code.index("{", fn_start) + 1
    depth = 1
    idx = body_open
    for ch in code[body_open:]:
        idx += 1
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
    return code[body_open:idx], code[:fn_start] + code[idx:]


def volume_rm_outside_migrate_volumes(storage_src: str) -> List[str]:
    """Part 2: in storage_ux.rs, `volume rm` lives ONLY inside the body of
    ``pub async fn migrate_volumes(``."""
    _body, outside = _split_migrate_volumes(storage_src)
    return [n for n in VOLUME_RM_NEEDLES if n in outside]


#: Bug 33 — verbatim from licensing.rs's
#: `no_local_bypass_paths_in_licensing_or_validator`.
BYPASS_NEEDLES = (
    "MAINTAINER_TOKEN",
    "_verify_maintainer_token",
    "_verify_maintainer",
    "VCT_MAINTAINER_TOKEN",
    "ed25519_dalek",
    "Ed25519",
    "maintainer_token",
    "maintainer_signing_key",
)


def bypass_findings(sources: Dict[Path, str]) -> List[str]:
    found = []
    for path, text in sources.items():
        code = _code_view(path, text)
        for needle in BYPASS_NEEDLES:
            if needle in code:
                found.append(f"{needle!r} in {path}")
    return found


def collapse_ws(text: str) -> str:
    """The projects_v2 gates' view: `\\` continuations become spaces and every
    whitespace run collapses to one space."""
    return " ".join(text.replace("\\", " ").split())


def migrate_warning_findings(projects_src: str) -> List[str]:
    """W-F5 (v0.2.70): the dry-run deferral warning is the rebuild-only
    consent string, and the old false framing is gone from production."""
    src = collapse_ws(production_source(projects_src))
    problems = []
    if "(`rebuild`) requires consent and was NOT auto-applied" not in src:
        problems.append("dry-run warning must scope consent to the rebuild action")
    if ("additive `copy` migrations are lossless and were applied automatically"
            not in src.lower()):
        problems.append("dry-run warning must still carry the additive-copy clause text")
    if "the destructive migration was NOT auto-applied" in src:
        problems.append("the old blanket 'destructive migration was NOT "
                        "auto-applied' phrasing must be removed")
    if "drops the collection mid-swap" in src:
        problems.append("the false 'copy drops the collection mid-swap' claim "
                        "must not be present")
    return problems


def additive_clause_findings(projects_src: str) -> List[str]:
    """v0.2.70 FIX-4: the additive clause is gated on `auto_apply_additive`."""
    src = collapse_ws(production_source(projects_src))
    problems = []
    if "let additive_clause = if auto_apply_additive" not in src:
        problems.append("the additive-copy clause must be gated on auto_apply_additive")
    if '} else { "" };' not in src:
        problems.append("the additive clause must fall back to an empty string")
    if "if auto_apply_additive {" not in src:
        problems.append("the wet apply must reuse the precomputed auto_apply_additive flag")
    return problems


#: v0.2.60 Piece 6 — verbatim from vct-updater main.rs's
#: `engine_entrypoint_is_not_wired_into_main`.
DORMANT_ENGINE_NEEDLES = ("run_engine", "engine::run_plan", "bootstrap::decide", "--engine")


def dormant_engine_findings(main_src: str) -> List[str]:
    live = production_source(main_src)
    return [n for n in DORMANT_ENGINE_NEEDLES if n in live]


PRESENCE_NEEDLE = "condition_is_recorded(install_path, "
PRESENCE_ALLOWED = {"CID_BINARY_STALE", "CID_HANDOFF_SKIPPED"}


def presence_check_call_args(freshness_src: str) -> List[str]:
    """Every first argument after ``install_path`` passed to
    ``condition_is_recorded`` in binary_freshness.rs production code."""
    prod = production_source(freshness_src)
    args = []
    start = 0
    while True:
        idx = prod.find(PRESENCE_NEEDLE, start)
        if idx == -1:
            return args
        tail = prod[idx + len(PRESENCE_NEEDLE):]
        args.append(tail.split(")", 1)[0].strip())
        start = idx + 1


def stale_cid_findings(restart_src: str) -> List[str]:
    code = production_source(restart_src)
    problems = []
    if "binary_freshness::CID_BINARY_STALE" not in code:
        problems.append("restart.rs must read the cid from its emitter")
    if '"launcher_binary_stale"' in code:
        problems.append("a hardcoded cid literal can drift away from the emitter")
    return problems


#: W1R-16 census — verbatim from install_root.rs's
#: `no_lazy_ddl_outside_migrations_census`.
DDL_ALLOW = {
    # ensure_change_log: called only from Db::open / open_in_memory (asserted below)
    "vct-launcher-core/src/db/change_log.rs",
    # module DDL runs after a module_db_migrations ledger read that fails on the
    # stand-in; the other hits are error-message strings
    "vct-launcher-core/src/db/module_db_migrations.rs",
    # BEGIN IMMEDIATE / COMMIT / ROLLBACK — transaction control, not DDL
    "vct-launcher-core/src/db/project_state.rs",
}
DDL_NEEDLES = (
    "create table",
    "create index",
    "create unique index",
    "create trigger",
    "create view",
    "create virtual",
    "alter table",
    "drop table",
    "execute_batch(",
)
DDL_ROOTS = ("vct-launcher-core/src", "src")


def _rust_files(base: Path, roots: Sequence[str], *, skip_migrations: bool) -> List[Path]:
    files = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(base / root):
            if skip_migrations:
                dirnames[:] = [d for d in dirnames if d != "migrations"]
            for name in filenames:
                if not name.endswith(".rs"):
                    continue
                if skip_migrations and name == "migrations.rs":
                    continue
                files.append(Path(dirpath) / name)
    return sorted(files)


def _code_lines(text: str) -> Iterable[str]:
    """Production lines minus whole-line ``//`` comments (the census rule)."""
    for line in production_source(text).split("\n"):
        if not line.lstrip().startswith("//"):
            yield line


def ddl_hits(files: Dict[str, str]) -> Set[str]:
    """``rel_path -> text`` → the files with a DDL / execute_batch line."""
    hits = set()
    for rel, text in files.items():
        for line in _code_lines(text):
            norm = " ".join(line.strip().lower().split())
            if any(n in norm for n in DDL_NEEDLES):
                hits.add(rel)
                break
    return hits


def ensure_change_log_callers(files: Dict[str, str]) -> int:
    return sum(
        1
        for text in files.values()
        for line in _code_lines(text)
        if ".ensure_change_log()" in line
    )


# ── real-tree gates ─────────────────────────────────────────────────────────


class RealTreeGates(unittest.TestCase):
    """The gates, run against the checkout."""

    def test_no_destructive_subprocess_calls_in_install_path(self) -> None:
        """Bug 32 #1 + #2: the install path never invokes a volume-removal or
        compose-down command. If this fails, someone added a destructive
        shell-out — gate it behind ``--force-clean`` or remove it."""
        paths = (INSTALL_PY, INSTALL_SH, INSTALLER_RS, PROJECTS_V2_RS)
        self.assertEqual(
            destructive_findings({p: _read(p) for p in paths}), [],
            "FORBIDDEN destructive invocation (would destroy user data on install)",
        )

    def test_volume_rm_only_callable_from_migrate_volumes(self) -> None:
        """Bug 31 + Bug 32 #4: only storage_ux.rs's ``migrate_volumes`` may
        invoke ``volume rm``."""
        paths = (INSTALL_PY, INSTALL_SH, INSTALLER_RS)
        self.assertEqual(
            volume_rm_install_path_findings({p: _read(p) for p in paths}), [],
            "FORBIDDEN: 'volume rm' invocation — only migrate_volumes may invoke it",
        )
        self.assertEqual(
            volume_rm_outside_migrate_volumes(_read(STORAGE_UX_RS)), [],
            "FORBIDDEN: 'volume rm' outside migrate_volumes in storage_ux.rs",
        )
        # Sanity: migrate_volumes IS the function that calls it.
        body, _outside = _split_migrate_volumes(_read(STORAGE_UX_RS))
        self.assertIn(
            '"volume", "rm"', body,
            "expected migrate_volumes to invoke `volume rm` (it's the destructive cleanup step)",
        )

    def test_no_local_bypass_paths_in_licensing_or_validator(self) -> None:
        """Bug 33: no maintainer-token bypass in either half of the licensing
        code or in the Python validator. Prose (comments, docstrings) is
        allowed."""
        paths = (LICENSING_RS, CORE_LICENSING_RS, VALIDATOR_PY)
        self.assertEqual(
            bypass_findings({p: _read(p) for p in paths}), [],
            "FORBIDDEN: Bug 33 dropped local bypass paths",
        )

    def test_migrate_warning_string_is_rebuild_only_consent_v0270(self) -> None:
        self.assertEqual(migrate_warning_findings(_read(PROJECTS_V2_RS)), [])

    def test_migrate_additive_clause_is_conditional_on_auto_apply_v0270(self) -> None:
        self.assertEqual(additive_clause_findings(_read(PROJECTS_V2_RS)), [])

    def test_engine_entrypoint_is_not_wired_into_updater_main(self) -> None:
        """The cross-OS engine and bootstrap stub stay DORMANT: the updater's
        live code neither calls them nor parses ``--engine``."""
        self.assertEqual(dormant_engine_findings(_read(UPDATER_MAIN_RS)), [])

    def test_the_record_presence_check_stays_scoped_to_its_two_cids(self) -> None:
        """``condition_is_recorded`` does a SUBSTRING match, sound only for
        cids that are not a prefix of another registered id. Calling it with a
        third cid must be a deliberate decision (anchor the match first)."""
        args = presence_check_call_args(_read(BINARY_FRESHNESS_RS))
        self.assertTrue(args, "expected the probe-driven clears to consult the presence helper")
        self.assertEqual(
            [a for a in args if a not in PRESENCE_ALLOWED], [],
            "`condition_is_recorded` called with a cid outside its two reasoned ids",
        )

    def test_the_stale_cid_is_imported_not_retyped(self) -> None:
        """restart.rs reads ``CID_BINARY_STALE`` from its emitter; a retyped
        literal would silently stop matching the day the emitter renames it."""
        self.assertEqual(stale_cid_findings(_read(RESTART_RS)), [])

    def test_no_lazy_ddl_outside_migrations_census(self) -> None:
        """Every production DDL / ``execute_batch`` site on the launcher.db
        side is on ``DDL_ALLOW``; a lazy DDL on the managed connection flips
        the update stand-in to 'live' (W1R-16)."""
        files = {
            p.relative_to(SRC_TAURI).as_posix(): p.read_text(encoding="utf-8")
            for p in _rust_files(SRC_TAURI, DDL_ROOTS, skip_migrations=True)
        }
        self.assertGreater(len(files), 50, "suspiciously few sources scanned")
        self.assertEqual(sorted(ddl_hits(files) - DDL_ALLOW), [])
        # The change_log allowance holds only while its callers are the two
        # open paths.
        self.assertEqual(
            production_source(_read(CORE_DB_MOD_RS)).count("ensure_change_log()"), 2
        )
        every = {
            p.as_posix(): p.read_text(encoding="utf-8")
            for p in _rust_files(SRC_TAURI, DDL_ROOTS, skip_migrations=False)
        }
        self.assertEqual(
            ensure_change_log_callers(every), 2,
            "ensure_change_log gained a caller outside Db::open / open_in_memory",
        )


# ── mutation red-proofs: a needle in the formerly blind span is caught ──────


def _first_gated_item_end(text: str) -> int:
    """1-indexed last line of the file's FIRST test-gated item."""
    spans = cfg_test_spans(text)
    assert spans, "fixture file has no test-gated item to plant after"
    return spans[0][1]


def _old_cut_line(text: str, marker: str = "#[cfg(test)]") -> int:
    """1-indexed line where the OLD gate stopped reading."""
    return text[: text.index(marker)].count("\n") + 1


def _plant(text: str, after_line: int, planted: str) -> str:
    """Insert ``planted`` as production code after line ``after_line``,
    skipping forward to the first top-level blank line so the insertion lands
    between items, outside any test span."""
    lines = text.split("\n")
    gated = {n for a, b in cfg_test_spans(text) for n in range(a, b + 1)}
    i = after_line
    while i < len(lines) and (lines[i].strip() or (i + 1) in gated):
        i += 1
    return "\n".join(lines[:i] + ["", planted, ""] + lines[i:])


class MutationRedProofs(unittest.TestCase):
    """Each gate turns RED when its needle is planted below the first gated
    item of the REAL file — a span the old first-marker cut never read."""

    def _assert_planted_past_old_cut(self, text: str, mutated: str,
                                     marker: str = "#[cfg(test)]") -> None:
        planted_line = mutated[: mutated.index("fn w6z2_planted")].count("\n") + 1
        self.assertGreater(planted_line, _old_cut_line(text, marker),
                           "the plant must sit in the span the old gate could not see")

    def _plant_fn(self, path: Path, body: str, needle: str,
                  check: Callable[[str], object], marker: str = "#[cfg(test)]") -> str:
        text = _read(path)
        mutated = _plant(text, _first_gated_item_end(text),
                         f"fn w6z2_planted() {{ {body} }}")
        self._assert_planted_past_old_cut(text, mutated, marker)
        self.assertFalse(check(text), f"{path.name}: gate must be green on the real file")
        self.assertTrue(check(mutated), f"{path.name}: gate missed {needle!r} past the first gated item")
        return mutated

    def test_destructive_audit_sees_installer_rs_past_its_gated_use(self) -> None:
        self._plant_fn(
            INSTALLER_RS,
            'let _ = std::process::Command::new("sh").arg("podman-compose down");',
            "podman-compose down",
            lambda t: destructive_findings({INSTALLER_RS: t}),
        )

    def test_destructive_audit_sees_projects_v2_rs_past_its_gated_helper(self) -> None:
        self._plant_fn(
            PROJECTS_V2_RS,
            'let _ = ["docker", "volume", "prune"]; let _ = "docker volume prune";',
            "docker volume prune",
            lambda t: destructive_findings({PROJECTS_V2_RS: t}),
        )

    def test_volume_rm_audit_sees_installer_rs_past_its_gated_use(self) -> None:
        self._plant_fn(
            INSTALLER_RS,
            'let _ = ["podman", "volume", "rm", "x"];',
            '"volume", "rm"',
            lambda t: volume_rm_install_path_findings({INSTALLER_RS: t}),
        )

    def test_volume_rm_rule_sees_storage_ux_past_its_gated_helper(self) -> None:
        self._plant_fn(
            STORAGE_UX_RS,
            'let _ = "podman volume rm x";',
            "volume rm",
            volume_rm_outside_migrate_volumes,
        )

    def test_bypass_audit_sees_launcher_licensing_past_its_comment_marker(self) -> None:
        text = _read(LICENSING_RS)
        # The OLD cut stopped at a COMMENT that quotes the marker.
        old_cut = _old_cut_line(text)
        self.assertIn("#[cfg(test)]", text.split("\n")[old_cut - 1])
        self.assertTrue(text.split("\n")[old_cut - 1].lstrip().startswith("//"))
        self._plant_fn(
            LICENSING_RS,
            'let _ = std::env::var("VCT_MAINTAINER_TOKEN");',
            "MAINTAINER_TOKEN",
            lambda t: bypass_findings({LICENSING_RS: t}),
        )

    def test_projects_v2_negative_phrasing_is_seen_past_the_gated_helper(self) -> None:
        self._plant_fn(
            PROJECTS_V2_RS,
            'let _ = "copy drops the collection mid-swap";',
            "drops the collection mid-swap",
            migrate_warning_findings,
        )

    def test_projects_v2_required_phrasing_counts_only_production(self) -> None:
        """The positive pins must not be satisfied by the test module: with
        the production clause removed, quoting it in a gated item is not
        enough."""
        text = _read(PROJECTS_V2_RS)
        self.assertEqual(additive_clause_findings(text), [])
        gutted = text.replace("let additive_clause = if auto_apply_additive",
                              "let additive_clause = if always_on")
        self.assertNotEqual(gutted, text)
        gutted += ('\n#[cfg(test)]\nmod w6z2 {\n    const Q: &str = '
                   '"let additive_clause = if auto_apply_additive";\n}\n')
        self.assertTrue(additive_clause_findings(gutted))

    def test_dormant_engine_gate_sees_main_rs_past_a_gated_item(self) -> None:
        """vct-updater's main.rs has one gated item, the trailing test module,
        so the old cut was not blind TODAY; a gated helper above `main()` would
        have made it so. Planted after a synthetic gated helper."""
        text = _read(UPDATER_MAIN_RS)
        anchor = "fn main()"
        self.assertIn(anchor, text)
        mutated = text.replace(
            anchor,
            "#[cfg(test)]\nfn w6z2_helper() {}\n\n"
            "fn w6z2_planted() { let _ = engine::run_plan; }\n\n" + anchor,
            1,
        )
        self.assertEqual(dormant_engine_findings(text), [])
        self.assertEqual(dormant_engine_findings(mutated), ["engine::run_plan"])

    def test_presence_scope_gate_sees_binary_freshness_past_a_gated_item(self) -> None:
        mutated = self._plant_fn(
            BINARY_FRESHNESS_RS,
            "let _ = condition_is_recorded(install_path, CID_SOMETHING_ELSE);",
            "CID_SOMETHING_ELSE",
            lambda t: [a for a in presence_check_call_args(t) if a not in PRESENCE_ALLOWED],
            marker="mod tests {",
        )
        self.assertIn("CID_SOMETHING_ELSE", presence_check_call_args(mutated))

    def test_stale_cid_gate_sees_restart_rs_past_a_gated_item(self) -> None:
        self._plant_fn(
            RESTART_RS,
            'let _ = "launcher_binary_stale";',
            '"launcher_binary_stale"',
            stale_cid_findings,
            marker="mod tests",
        )

    def test_ddl_census_sees_installer_rs_past_its_gated_use(self) -> None:
        text = _read(INSTALLER_RS)
        mutated = _plant(text, _first_gated_item_end(text),
                         'fn w6z2_planted(c: &Connection) { c.execute_batch("x"); }')
        rel = "src/commands/installer.rs"
        self.assertEqual(ddl_hits({rel: text}), set())
        self.assertEqual(ddl_hits({rel: mutated}), {rel})

    def test_ddl_census_sees_production_after_the_test_module(self) -> None:
        """`types.rs` carries production code below its test module."""
        path = SRC_TAURI / "vct-launcher-core" / "src" / "types.rs"
        text = _read(path)
        spans = cfg_test_spans(text)
        last_end = spans[-1][1]
        lines = text.split("\n")
        self.assertTrue(
            any(ln.strip() for ln in lines[last_end:]),
            "types.rs no longer has production code after its last gated item; "
            "pick another fixture",
        )
        mutated = "\n".join(lines + ["", 'pub const W6Z2: &str = "CREATE TABLE x";', ""])
        rel = "vct-launcher-core/src/types.rs"
        self.assertEqual(ddl_hits({rel: mutated}), {rel})

    def test_change_log_caller_count_sees_past_a_gated_item(self) -> None:
        text = _read(INSTALLER_RS)
        mutated = _plant(text, _first_gated_item_end(text),
                         "fn w6z2_planted(db: &Db) { db.ensure_change_log(); }")
        self.assertEqual(ensure_change_log_callers({"x": text}), 0)
        self.assertEqual(ensure_change_log_callers({"x": mutated}), 1)


class ViewSemantics(unittest.TestCase):
    """The text views keep the originals' allowances: prose is not code."""

    def test_rust_comments_and_test_items_are_not_code(self) -> None:
        src = (
            "// podman volume rm in a comment\n"
            "/// docker-compose down in a doc comment\n"
            "fn f() {}\n"
            "#[cfg(test)]\n"
            "mod tests { const N: &str = \"podman volume rm\"; }\n"
            "fn g() { let _ = \"r#\"; }\n"
        )
        self.assertEqual(destructive_findings({Path("x.rs"): src}), [])

    def test_rust_string_after_a_hash_is_still_code(self) -> None:
        """The old `#`-cut heuristic hid a whole line after a `#` in a string."""
        src = 'fn g() { let _ = ("#", "podman-compose down"); }\n'
        self.assertEqual(len(destructive_findings({Path("x.rs"): src})), 1)

    def test_python_docstrings_and_comments_are_not_code(self) -> None:
        src = '"""MAINTAINER_TOKEN in a docstring"""\nx = 1  # Ed25519 here\n'
        self.assertEqual(bypass_findings({Path("v.py"): src}), [])
        self.assertEqual(len(bypass_findings({Path("v.py"): "k = 'maintainer_token'\n"})), 1)

    def test_migrate_volumes_body_is_the_only_allowed_home(self) -> None:
        src = (
            "pub async fn migrate_volumes(x: u8) -> u8 {\n"
            "    if x > 0 { let _ = [\"podman\", \"volume\", \"rm\"]; }\n"
            "    x\n"
            "}\n"
        )
        self.assertEqual(volume_rm_outside_migrate_volumes(src), [])
        self.assertEqual(
            volume_rm_outside_migrate_volumes(src + 'fn other() { let _ = "volume rm"; }\n'),
            ["volume rm"],
        )

    def test_collapse_ws_joins_backslash_continuations(self) -> None:
        self.assertEqual(collapse_ws('"a \\\n     b"'), '"a b"')

    def test_census_ignores_whole_line_comments(self) -> None:
        self.assertEqual(ddl_hits({"a.rs": "// CREATE TABLE x\nfn f() {}\n"}), set())
        self.assertEqual(ddl_hits({"a.rs": 'fn f() { q("CREATE  TABLE x"); }\n'}), {"a.rs"})


if __name__ == "__main__":
    unittest.main()
