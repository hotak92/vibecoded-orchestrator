# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.91 invariant (#21): no bare ``println!`` / ``eprintln!`` diagnostics
in the launcher Rust crates.

THE RULE: launcher, hub, and core diagnostics go through ``tracing`` macros
at an honest level, so the global ``logging.level`` preference (env
``VCO_LOG_LEVEL`` > launcher.db ``app_state['logging.level']`` > INFO,
resolved by ``vct_launcher_core::logging``) actually governs how chatty the
processes are. A bare print bypasses that entirely: it is unconditional at
every level, and it cannot be silenced, raised, or redirected.

THE EXCEPTION — machine contracts. Some printed lines are not commentary
ABOUT the work, they ARE the work's output: the words ``running pid=N`` /
``not-running`` / ``enabled`` that ``vct-hub --status`` and
``--boot-status`` put on stdout for their callers, the usage text of a
``--help``, and the stdout/stderr report of the standalone CI binaries
(``validate-manifest``, ``export-schema``). Routing those through a level
filter would let a log level empty a file CI diffs against, or hide the
answer to the command the user just ran. They stay bare `println!` and are
annotated ``// [vct-print-contract]`` — on the same line, or anywhere in
the comment block directly above.

So this scan fails on exactly one thing: a print that is NEITHER a tracing
macro NOR annotated as a contract. The failure message names both remedies
because both are legitimate; deciding which applies is the author's job,
and the annotation is where that decision gets recorded for the next
reader.

TEST-CODE EXCLUSION: every item gated by a ``#[cfg(...)]`` whose predicate
mentions ``test`` is skipped INDIVIDUALLY (attributes, then one
semicolon-terminated item or one brace-balanced block), and scanning
RESUMES after it. Both ``#[cfg(test)]`` and ``#[cfg(any(test,
debug_assertions))]`` count: the latter gates ``secrets::test_serialize``,
a test-support module excluded from release builds, whose prints are
debugging aids for a test run that has no subscriber installed. Skipping
per-item rather than cutting the file at the first marker matters because
several files gate a mid-file test helper and then continue with
production code (the lesson recorded in the v0.2.90 bare-tokio-spawn scan,
whose structure this test follows).

Scope: the three crates whose binaries ship to users —
``launcher/src-tauri/src`` (the Tauri app), ``vct-hub/src`` (the detached
daemon), and ``vct-launcher-core/src`` (the library both consume).
``vct-updater`` is out of scope: it is a tiny detached relaunch helper
that runs with no subscriber and whose few lines ARE its console output.

KNOWN TEMPORARY EXCLUSION: ``vct-hub/src/config_api.rs`` — owned by a
separate work package in this same release, migrating in the wave-5 fix
round. See ``PENDING_MIGRATION`` below; that list must be empty before the
release tags.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src",
    REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src",
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src",
)

#: Files whose prints are owned by another in-flight work package this
#: cycle. Each entry is a repo-relative path plus the reason it is not
#: this lane's to migrate. MUST be empty at release-tag time — the
#: no-deferred-fixes rule applies to this list like any other backlog.
PENDING_MIGRATION: dict[str, str] = {}

#: The annotation that marks a print as a deliberate machine contract.
CONTRACT_MARKER = "[vct-print-contract]"

_PRINT_MACRO = re.compile(r"\b(?:e?print(?:ln)?)!\s*\(")
#: A column-0 (top-level) Rust item start. Used ONLY by the real-tree
#: fixture below, as an oracle independent of the skip logic under test.
_TOP_LEVEL_ITEM = re.compile(
    r"^(?:pub\b|fn\b|const\b|static\b|struct\b|enum\b|impl\b|trait\b"
    r"|type\b|mod\b|use\b|async\b|unsafe\b|extern\b|#\[)"
)
_CFG_ATTR = re.compile(r"^\s*#\[\s*cfg\s*\((?P<pred>.*)$")
_ATTR = re.compile(r"^\s*#\[")
_LINE_COMMENT = re.compile(r"^\s*//")


#: Lexer state carried BETWEEN lines. Rust string literals and block
#: comments both span lines, so a per-line stripper desynchronises on the
#: first one it meets and mis-reads every quote after it. That is not a
#: theoretical concern: this crate's own migrated log messages are written
#: as `"...text \` + continuation, and a stateless stripper read the
#: CLOSING quote of such a literal as an OPENING one — which unbalanced
#: the brace counter and silently ended a `#[cfg(test)]` skip 400 lines
#: early, flagging test code as production. Found by running the first
#: version of this scan against the real tree.
_ST_CODE = "code"
_ST_STRING = "string"
_ST_RAW = "raw"
_ST_BLOCK_COMMENT = "block"


def _strip_line(line: str, state: tuple) -> tuple[str, tuple]:
    """Blank out string/char literals and comments in one line, resuming
    from (and returning) the cross-line lexer `state`.

    Blanking rather than deleting keeps column offsets stable and means a
    brace, or a literal ``println!(``, inside a string or comment (doc
    examples, error-message templates, this test's own fixtures) cannot be
    mistaken for code.
    """
    kind = state[0]
    out: list[str] = []
    i, n = 0, len(line)

    # ── Resume an unterminated construct from a previous line ──
    if kind == _ST_STRING:
        while i < n:
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == '"':
                out.append(" " * (i + 1))
                i += 1
                state = (_ST_CODE,)
                break
            i += 1
        else:
            return " " * n, (_ST_STRING,)
        if state[0] == _ST_STRING:
            return " " * n, (_ST_STRING,)
    elif kind == _ST_RAW:
        closer = '"' + "#" * state[1]
        end = line.find(closer)
        if end == -1:
            return " " * n, state
        i = end + len(closer)
        out.append(" " * i)
        state = (_ST_CODE,)
    elif kind == _ST_BLOCK_COMMENT:
        depth = state[1]
        while i < n:
            if line.startswith("*/", i):
                depth -= 1
                i += 2
                if depth == 0:
                    break
                continue
            if line.startswith("/*", i):
                depth += 1
                i += 2
                continue
            i += 1
        out.append(" " * i)
        state = (_ST_CODE,) if depth == 0 else (_ST_BLOCK_COMMENT, depth)
        if state[0] == _ST_BLOCK_COMMENT:
            return " " * n, state

    # ── Normal scanning ──
    while i < n:
        ch = line[i]
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break  # line comment: the rest is prose
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if line.startswith("*/", j):
                    depth -= 1
                    j += 2
                elif line.startswith("/*", j):
                    depth += 1
                    j += 2
                else:
                    j += 1
            out.append(" " * (j - i))
            i = j
            if depth:
                return "".join(out), (_ST_BLOCK_COMMENT, depth)
            continue
        # Raw string: r"..." / r#"..."# / r##"..."## …
        if ch == "r" and i + 1 < n and line[i + 1] in '#"':
            j, hashes = i + 1, 0
            while j < n and line[j] == "#":
                hashes += 1
                j += 1
            if j < n and line[j] == '"':
                closer = '"' + "#" * hashes
                end = line.find(closer, j + 1)
                if end == -1:
                    out.append(" " * (n - i))
                    return "".join(out), (_ST_RAW, hashes)
                out.append(" " * (end + len(closer) - i))
                i = end + len(closer)
                continue
        if ch == '"':
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == '"':
                    break
                j += 1
            if j >= n:
                # Runs past end-of-line — Rust string literals may span
                # lines (and `\`-continuations look identical here).
                out.append(" " * (n - i))
                return "".join(out), (_ST_STRING,)
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue
        # Char literal — but NOT a lifetime (`'a`, `'static`).
        if ch == "'" and i + 2 < n:
            if line[i + 1] == "\\":
                end = line.find("'", i + 2)
            elif line[i + 2] == "'":
                end = i + 2
            else:
                end = -1
            if end != -1:
                out.append(" " * (end + 1 - i))
                i = end + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), state


def _strip_file(lines: list[str]) -> list[str]:
    """Code-only view of a whole file, lexed with cross-line state."""
    code: list[str] = []
    state: tuple = (_ST_CODE,)
    for line in lines:
        stripped, state = _strip_line(line, state)
        code.append(stripped)
    return code


def _strip_strings_and_comments(line: str) -> str:
    """Single-line convenience for callers with no cross-line context."""
    return _strip_line(line, (_ST_CODE,))[0]


def _cfg_predicate_mentions_test(lines: list[str], code: list[str], i: int) -> bool:
    """True when ``lines[i]`` starts a ``#[cfg(...)]`` whose predicate names
    the ``test`` cfg. Reads forward for multi-line attributes.

    Judged on the CODE view, so ``#[cfg(feature = "test-support")]`` is read
    for its cfg names and not for the text inside the quotes.
    """
    if not _CFG_ATTR.match(lines[i]):
        return False
    pred = code[i]
    j = i
    # Multi-line attribute: keep reading until brackets balance.
    while pred.count("[") > pred.count("]") and j + 1 < len(lines):
        j += 1
        pred += " " + code[j]
    return re.search(r"\btest\b", pred) is not None


def _skip_gated_item(lines: list[str], code: list[str], i: int) -> int:
    """``lines[i]`` is a test-gated attribute. Return the index just past the
    item it gates: any further attribute lines, then either a
    semicolon-terminated item (``mod tests;``, ``use ...;``) or one
    brace-balanced block (fn/mod/impl alike)."""
    j = i + 1
    while j < len(lines) and _ATTR.match(lines[j]):
        j += 1
    depth = 0
    seen_open = False
    while j < len(lines):
        depth += code[j].count("{") - code[j].count("}")
        if "{" in code[j]:
            seen_open = True
        if seen_open and depth <= 0:
            return j + 1
        if not seen_open and code[j].rstrip().endswith(";"):
            return j + 1
        j += 1
    return j


def _comment_text(line: str, code: str) -> str:
    """The ``//`` comment tail of a line, or ``""`` when it has none.

    `_strip_line` stops emitting at a real (non-string) ``//``, so the
    length of the code view is exactly where the comment begins.
    """
    return line[len(code):] if len(code) < len(line) else ""


def _has_contract_marker(
    lines: list[str], code: list[str], i: int, in_code_state: set[int] | None = None
) -> bool:
    """True when the print at ``lines[i]`` is annotated as a machine
    contract — in the comment on its own line, or anywhere in the
    contiguous run of ``//`` comment lines directly above it.

    The comment-block form is what lets an author explain WHY a line is a
    contract in full sentences instead of cramming a justification onto
    the end of a code line (or, worse, writing a bare marker with no
    reason at all).

    ## The marker must be in a COMMENT, never merely present in the text

    Searching the raw line for the marker was a real hole in this gate:

        eprintln!("see [vct-print-contract] in the docs");

    exempted itself. The locator was satisfiable by a *description* of an
    annotation rather than an annotation — so any bare print that merely
    MENTIONS the convention (a diagnostic explaining the rule, an error
    message quoting this scan's own guidance text, which contains the
    marker verbatim) silently left the ratchet. Same failure shape as a
    scan that matches a commented-out call instead of the real one: the
    gate's grip on reality supplied by the thing it is gating.

    Both lookups are now anchored to real comment syntax — the code
    view decides where a comment begins, so a marker inside a string
    literal cannot be one — and a ``//`` line only counts as a comment
    when it BEGINS in the lexer's code state, so text inside a multi-line
    string cannot impersonate a comment block either.
    """
    if CONTRACT_MARKER in _comment_text(lines[i], code[i]):
        return True
    if in_code_state is None:
        in_code_state = _code_state_line_starts(lines)
    j = i - 1
    while j >= 0 and j in in_code_state and _LINE_COMMENT.match(lines[j]):
        if CONTRACT_MARKER in lines[j]:
            return True
        j -= 1
    return False


def _scan_file(path: Path, rel: str | None = None) -> list[str]:
    """Return violation descriptions for one .rs file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    code = _strip_file(lines)
    in_code_state = _code_state_line_starts(lines)
    if rel is None:
        try:
            rel = str(path.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(path)

    violations: list[str] = []
    i = 0
    while i < len(lines):
        if _cfg_predicate_mentions_test(lines, code, i):
            i = _skip_gated_item(lines, code, i)
            continue
        if not _LINE_COMMENT.match(lines[i]) and _PRINT_MACRO.search(code[i]):
            if not _has_contract_marker(lines, code, i, in_code_state):
                violations.append(
                    f"{rel}:{i + 1}: bare print bypasses the log level. "
                    f"Either (a) migrate it to a tracing macro at an honest "
                    f"level (tracing::error!/warn!/info!/debug!), or (b) if it "
                    f"is a machine contract — CLI stdout a caller parses, a "
                    f"--help/usage text, a standalone tool's own report — "
                    f"annotate it `// {CONTRACT_MARKER}` on this line or in "
                    f"the comment block above, saying who consumes it."
                )
        i += 1
    return violations


def _code_state_line_starts(lines: list[str]) -> set[int]:
    """Indices of lines that BEGIN in the lexer's code state — i.e. not
    inside a multi-line string literal or block comment.

    Any harness that injects synthetic code into a real file must consult
    this. Injecting blind lands inside things like
    ``secrets_cmd.rs``'s multi-line ``HUB_UNREACHABLE_CLI_FALLBACK`` const,
    where the injected text is genuinely string CONTENT — the scanner is
    right to ignore it, and a harness without cross-line state reads that
    correct answer as a blind spot. The injection tooling needs the same
    lexer state as the scanner it is testing, or it manufactures its own
    false alarms (the bug class this whole scan exists for, one level up).
    """
    clean: set[int] = set()
    state: tuple = (_ST_CODE,)
    for i, line in enumerate(lines):
        if state[0] == _ST_CODE:
            clean.add(i)
        _, state = _strip_line(line, state)
    return clean


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        files.extend(sorted(root.rglob("*.rs")))
    return files


class NoBarePrintsInRustCrates(unittest.TestCase):
    def test_scan_roots_exist_and_are_nonempty(self) -> None:
        """Guard the scan against path drift going silently green."""
        for root in SCAN_ROOTS:
            self.assertTrue(root.is_dir(), f"scan root missing: {root}")
        self.assertGreater(
            len(_iter_scan_files()),
            100,
            "suspiciously few .rs files — scan roots drifted?",
        )

    def test_no_bare_prints(self) -> None:
        violations: list[str] = []
        skipped: list[str] = []
        for rs in _iter_scan_files():
            rel = str(rs.relative_to(REPO_ROOT)).replace("\\", "/")
            if rel in PENDING_MIGRATION:
                skipped.append(rel)
                continue
            violations.extend(_scan_file(rs, rel))
        self.assertEqual(
            violations,
            [],
            f"{len(violations)} bare print(s) with no tracing migration and no "
            f"{CONTRACT_MARKER} annotation:\n" + "\n".join(violations),
        )
        # A pending-migration entry for a file that no longer exists (or was
        # already migrated) is stale bookkeeping — fail so the list cannot
        # rot into a permanent, unexamined hole in the ratchet.
        for rel in PENDING_MIGRATION:
            self.assertIn(
                rel,
                skipped,
                f"PENDING_MIGRATION names {rel}, which the scan never reached "
                f"(moved or deleted?). Remove the stale entry.",
            )

    def test_pending_migration_entries_still_need_migrating(self) -> None:
        """A file on the pending list that is ALREADY clean must come off it.

        Without this, an exclusion added for a real reason would outlive that
        reason and silently keep a whole file out of the ratchet forever.
        """
        for rel, reason in PENDING_MIGRATION.items():
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            self.assertNotEqual(
                _scan_file(path, rel),
                [],
                f"{rel} is on PENDING_MIGRATION ({reason}) but has no "
                f"remaining violations — delete the entry so the file is "
                f"scanned normally from now on.",
            )


class ScannerSelfCheck(unittest.TestCase):
    """The scanner cannot pass vacuously.

    A regex that stopped matching, a root that moved, or a comment-stripper
    that blanked everything would leave `test_no_bare_prints` green while
    checking nothing. These tests assert the scanner still SEES the tree.
    """

    def test_scanner_finds_print_macros_in_the_real_tree(self) -> None:
        """The three crates DO contain print macros — a scanner that finds
        none has stopped working, whatever the violations list says."""
        hits = 0
        for rs in _iter_scan_files():
            for line in rs.read_text(encoding="utf-8").splitlines():
                if _PRINT_MACRO.search(_strip_strings_and_comments(line)):
                    hits += 1
        self.assertGreater(
            hits,
            25,
            "the scanner detected almost no print macros across three crates "
            "that demonstrably contain them — the matcher or the comment "
            "stripper is broken.",
        )

    def test_scanner_recognises_a_known_annotated_contract(self) -> None:
        """`vct-hub --status` prints its answer to stdout under a contract
        annotation. The scanner must see BOTH the print and the marker."""
        path = (
            REPO_ROOT
            / "launcher"
            / "src-tauri"
            / "vct-hub"
            / "src"
            / "lifecycle.rs"
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = [
            n
            for n, ln in enumerate(lines)
            if 'println!("not-running")' in ln
        ]
        self.assertEqual(
            len(idx),
            1,
            "the `--status` not-running contract line moved; update this "
            "self-check to a current contract site (do NOT just delete it).",
        )
        n = idx[0]
        self.assertTrue(
            _PRINT_MACRO.search(_strip_strings_and_comments(lines[n])),
            "scanner no longer detects a plain println! call",
        )
        self.assertTrue(
            _has_contract_marker(lines, _strip_file(lines), n),
            "scanner no longer detects the contract annotation",
        )
        self.assertEqual(_scan_file(path), [], "lifecycle.rs should be clean")

    def test_scanner_flags_an_injected_violation_in_a_real_file(self) -> None:
        """Copy a real, currently-clean source file, add ONE bare print, and
        confirm the scan catches it. Proves the whole pipeline — file read,
        cfg skipping, comment stripping, marker lookup — is live."""
        path = (
            REPO_ROOT
            / "launcher"
            / "src-tauri"
            / "vct-launcher-core"
            / "src"
            / "logging.rs"
        )
        original = path.read_text(encoding="utf-8")
        self.assertEqual(
            _scan_file(path), [], "logging.rs should be clean to start from"
        )
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "logging.rs"
            probe.write_text(
                original + '\npub fn leak() { eprintln!("oops"); }\n',
                encoding="utf-8",
            )
            found = _scan_file(probe, "probe.rs")
        self.assertEqual(len(found), 1, found)
        self.assertIn("bare print", found[0])


class RealTreeCfgTestSkipFixture(unittest.TestCase):
    """Pin the `#[cfg(test)]` skip against a REAL, large source file.

    ## The failure direction this exists for

    The `#[cfg(test)]` skip can go wrong two ways, and they are not equally
    safe:

      * skip ends TOO EARLY → test code judged production → false
        POSITIVES. Loud. You see them and fix them. (This happened: a
        stateless string lexer desynchronised on a multi-line literal and
        produced 19 of them.)
      * skip runs TOO FAR → production code judged test → false
        NEGATIVES. **Silent.** A whole region stops being scanned and a
        bare print there is never reported.

    `ScannerSelfCheck` cannot catch the second one. Asserting "the scanner
    still sees print macros and a known contract" stays perfectly true
    while one file quietly goes unscanned — the self-check is satisfied by
    the rest of the tree.

    The synthetic `test_cfg_test_item_does_not_blind_the_rest_of_the_file`
    covers the shape, but at six lines it is satisfied by any scanner that
    tracks one brace pair. This exercises the same property across ~1300
    lines of real test module followed by real production code, where a
    subtly mis-tracked span (a nested module, a brace in a format string,
    a multi-line const) actually has room to go wrong.

    ## Why assertions are structural, not line numbers

    The fixture file is live source and will drift. Everything here is
    derived from the file at run time; nothing hardcodes a line. If the
    file stops exercising the property (no test-gated region, or nothing
    but test code after it), the test SKIPS with a message naming a
    replacement criterion rather than failing — a fixture that has stopped
    being representative is a bookkeeping problem, not a regression.
    """

    #: A large real file with exactly the shape under test: a substantial
    #: `#[cfg(test)]` module that CLOSES mid-file, with production
    #: `#[command]` fns (and migrated tracing sites) after it.
    FIXTURE = "launcher/src-tauri/src/commands/secrets_cmd.rs"

    #: Below this, the tail is too small to be a meaningful exercise.
    MIN_TAIL_LINES = 50

    def _load(self):
        """Locate the fixture's shape using an oracle INDEPENDENT of
        `_skip_gated_item`.

        This independence is the whole design, and getting it wrong is a
        live trap: the first version of this class derived the production
        tail from `_skip_gated_item` itself. Red-proofing it against a
        simulated cut-to-EOF regression exposed the consequence — with the
        bug injected the tail measured zero lines, the "file no longer has
        the shape" guard fired, and all five tests SKIPPED green. A guard
        against silent failure that silently disables itself is worse than
        no guard, because it reads as coverage.

        So the shape is established from COLUMN-0 item structure instead:
        a top-level `#[cfg(test)] mod … {`, the column-0 `}` that closes
        it, and the next column-0 item after that. Rust indents everything
        inside a module, so this needs no brace arithmetic and shares
        nothing with the code under test but the (separately red-proofed)
        lexer. If the two disagree, that disagreement IS the regression —
        and it now surfaces as a FAILURE, never as a skip.
        """
        path = REPO_ROOT / self.FIXTURE
        if not path.is_file():
            self.skipTest(
                f"fixture {self.FIXTURE} no longer exists — repoint FIXTURE at "
                f"another large file whose #[cfg(test)] module closes mid-file "
                f"with production code after it."
            )
        lines = path.read_text(encoding="utf-8").splitlines()
        code = _strip_file(lines)

        # Last top-level test gate that is immediately followed by `mod … {`.
        gate = None
        for i in range(len(lines)):
            if not _cfg_predicate_mentions_test(lines, code, i):
                continue
            nxt = code[i + 1] if i + 1 < len(lines) else ""
            if re.match(r"^\s*(?:pub\s+)?mod\s+\w+\s*\{", nxt):
                gate = i
        if gate is None:
            self.skipTest(
                f"{self.FIXTURE} no longer has a top-level `#[cfg(test)] mod …` "
                f"region — repoint FIXTURE at a file that does."
            )

        # Column-0 `}` closing that module, then the next column-0 item.
        close = next(
            (j for j in range(gate + 1, len(lines)) if code[j].startswith("}")),
            None,
        )
        tail = None
        if close is not None:
            tail = next(
                (
                    k
                    for k in range(close + 1, len(lines))
                    if _TOP_LEVEL_ITEM.match(code[k])
                ),
                None,
            )
        if tail is None or len(lines) - tail < self.MIN_TAIL_LINES:
            self.skipTest(
                f"{self.FIXTURE} no longer has >={self.MIN_TAIL_LINES} lines of "
                f"top-level production code after its test module (the shape "
                f"this fixture pins) — repoint FIXTURE at a file that does."
            )
        return path, lines, code, gate, close, tail

    def _probe(self, lines: list[str], at: int) -> bool:
        """Insert a bare print before index `at`; is it reported at that line?"""
        mutated = lines[:at] + ['    eprintln!("fixture probe");'] + lines[at:]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe.rs"
            p.write_text("\n".join(mutated) + "\n", encoding="utf-8")
            return any(f":{at + 1}:" in v for v in _scan_file(p, "probe.rs"))

    def test_fixture_is_currently_clean(self) -> None:
        """The probes below mean nothing unless the file starts clean."""
        path, *_rest = self._load()
        self.assertEqual(_scan_file(path), [])

    def test_skip_resumes_before_eof_instead_of_cutting_to_eof(self) -> None:
        _, lines, code, gate, _close, _tail = self._load()
        end = _skip_gated_item(lines, code, gate)
        self.assertLess(
            end,
            len(lines),
            "the #[cfg(test)] skip consumed the rest of the file — every line "
            "after it is now unscanned, silently.",
        )
        self.assertGreater(
            end, gate, "the skip did not advance past its own gate attribute"
        )

    def test_skip_agrees_with_the_independent_structural_oracle(self) -> None:
        """Parity: where `_skip_gated_item` resumes must match where
        column-0 structure says the test module ended.

        `tail` is the first top-level item AFTER the closing brace, so the
        skip should land at or before it (blank lines and comments may sit
        between), and never before the module actually closed.
        """
        _, lines, code, gate, _close, tail = self._load()
        end = _skip_gated_item(lines, code, gate)
        self.assertLessEqual(
            end,
            tail,
            f"skip resumed at line {end + 1}, PAST the first production item at "
            f"line {tail + 1} — it is overrunning into production code.",
        )
        self.assertGreater(
            end,
            gate + 1,
            f"skip resumed at line {end + 1}, before the test module could have "
            f"closed — it is ending early.",
        )

    def test_production_tail_after_the_test_module_is_still_scanned(self) -> None:
        """The false-NEGATIVE guard: a bare print anywhere in the production
        tail must be reported. Probes at the start, middle and end of the
        tail, so a skip that overruns by any amount is caught."""
        _, lines, _, _, _close, tail_start = self._load()
        clean = _code_state_line_starts(lines)
        tail = [i for i in range(tail_start, len(lines)) if i in clean]
        self.assertGreaterEqual(
            len(tail), 3, "too few injectable lines in the tail to probe"
        )
        for label, at in (
            ("start", tail[0]),
            ("middle", tail[len(tail) // 2]),
            ("end", tail[-1]),
        ):
            with self.subTest(position=label, line=at + 1):
                self.assertTrue(
                    self._probe(lines, at),
                    f"BLIND SPOT: a bare print at line {at + 1} (tail {label}) "
                    f"was not reported — the #[cfg(test)] skip is overrunning "
                    f"into production code.",
                )

    def test_inside_the_test_module_is_still_exempt(self) -> None:
        """The complementary direction: the skip must not end early either,
        or real test code starts getting reported."""
        # Bound at the module's CLOSING brace, not at the next top-level
        # item: the gap between them (blank lines, a section comment) is
        # already production territory, and probing there would assert the
        # opposite of the truth. Caught by this very test on first run.
        _, lines, _, gate, close, _tail = self._load()
        clean = _code_state_line_starts(lines)
        inside = [i for i in range(gate + 2, close) if i in clean]
        if len(inside) < 3:
            self.skipTest("test region too small to probe")
        for label, at in (
            ("start", inside[0]),
            ("middle", inside[len(inside) // 2]),
            ("end", inside[-1]),
        ):
            with self.subTest(position=label, line=at + 1):
                self.assertFalse(
                    self._probe(lines, at),
                    f"a print at line {at + 1} inside the #[cfg(test)] region "
                    f"(region {label}) was reported — the skip is ending early.",
                )

    def test_injection_points_exclude_multiline_string_interiors(self) -> None:
        """Guard the HARNESS, not the scanner.

        `_code_state_line_starts` is what keeps the probes above honest.
        Injecting blind lands inside things like this file's multi-line
        `&str` consts, where the injected text is string CONTENT — the
        scanner correctly ignores it and a naive harness reads that correct
        answer as a blind spot. (Exactly what happened while validating
        this fixture: one blind probe landed mid-const and reported a
        false blind spot.) So the fixture file must actually CONTAIN such
        a construct, or these probes are not being filtered by anything.
        """
        _, lines, _, _, _close, _tail = self._load()
        clean = _code_state_line_starts(lines)
        interiors = [i for i in range(len(lines)) if i not in clean]
        self.assertGreater(
            len(interiors),
            0,
            f"{self.FIXTURE} no longer contains any multi-line string or block "
            f"comment, so the harness's state filtering is untested here — "
            f"repoint FIXTURE at a file that has one.",
        )
        # And the filter must be doing real work: an injection into one of
        # those interiors is correctly NOT reported as a violation.
        self.assertFalse(
            self._probe(lines, interiors[len(interiors) // 2]),
            "text injected into a string/comment interior was reported as "
            "code — the lexer's cross-line state is broken.",
        )


class ScannerBehavior(unittest.TestCase):
    """The scanner's own contract — one case per reviewed edge."""

    def _scan_source(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe.rs"
            p.write_text(source, encoding="utf-8")
            return _scan_file(p, "probe.rs")

    def test_flags_bare_println_and_eprintln(self) -> None:
        v = self._scan_source(
            'fn a() { println!("hi"); }\nfn b() { eprintln!("bye"); }\n'
        )
        self.assertEqual(len(v), 2, v)

    def test_flags_newline_free_print_and_eprint(self) -> None:
        # `print!` / `eprint!` bypass the level exactly like their `ln`
        # siblings; the ratchet would be trivially evadable without them.
        v = self._scan_source(
            'fn a() { print!("hi"); }\nfn b() { eprint!("bye"); }\n'
        )
        self.assertEqual(len(v), 2, v)

    def test_tracing_macros_are_not_flagged(self) -> None:
        v = self._scan_source(
            'fn a() {\n'
            '    tracing::info!("started");\n'
            '    tracing::error!(error = %e, "failed");\n'
            '}\n'
        )
        self.assertEqual(v, [], v)

    def test_same_line_contract_marker_exempts(self) -> None:
        v = self._scan_source(
            'fn a() { println!("not-running"); // [vct-print-contract]\n}\n'
        )
        self.assertEqual(v, [], v)

    def test_marker_anywhere_in_the_comment_block_above_exempts(self) -> None:
        v = self._scan_source(
            "fn a() {\n"
            "    // [vct-print-contract] the CLI's answer, parsed by callers;\n"
            "    // routing it through the log subscriber would let a level\n"
            "    // setting suppress the result of the command.\n"
            '    println!("enabled");\n'
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_marker_inside_the_prints_own_string_does_NOT_exempt(self) -> None:
        """REGRESSION — the locator variant, found in this gate after
        delivery.

        The exemption lookup searched the RAW line, so a bare print whose
        own message merely MENTIONED the convention exempted itself. Any
        diagnostic explaining the rule — including one quoting this scan's
        failure text, which contains the marker verbatim — silently left
        the ratchet. The locator was satisfiable by a *description* of an
        annotation instead of an annotation.
        """
        # Pin the hazard: the naive locator MUST be fooled by this input,
        # otherwise the case below is not exercising anything.
        shadowed = 'eprintln!("see [vct-print-contract] in the docs");'
        self.assertIn(
            CONTRACT_MARKER,
            shadowed,
            "the fixture no longer contains the marker text — this test would "
            "pass without exercising the shadowing hazard.",
        )
        v = self._scan_source(f"fn a() {{\n    {shadowed}\n}}\n")
        self.assertEqual(len(v), 1, v)

    def test_marker_in_a_string_that_looks_like_a_comment_does_NOT_exempt(
        self,
    ) -> None:
        """A `//`-prefixed line INSIDE a multi-line string literal is text,
        not a comment block, and must not license the print after it.

        The string CLOSES on the fake-comment line, so that line sits
        directly above the print and would be walked by the comment-block
        lookup. That placement is deliberate: an earlier version of this
        test put the closing quote on its own line, which stopped the
        upward walk immediately and made the case pass with or without the
        code-state guard — a test that could not fail, which is the exact
        defect class this file exists to catch. Found by red-proofing
        against the pre-fix locator.
        """
        v = self._scan_source(
            "fn a() {\n"
            '    let s = "\n'
            '// [vct-print-contract] not really a comment";\n'
            '    eprintln!("real diagnostic");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)

    def test_real_annotation_exempts_even_if_the_string_also_has_marker(
        self,
    ) -> None:
        # The fix must not overcorrect: a genuine trailing annotation still
        # exempts, whatever the message text happens to contain.
        v = self._scan_source(
            'fn a() {\n    println!("[vct-print-contract] literal"); '
            "// [vct-print-contract] real annotation\n}\n"
        )
        self.assertEqual(v, [], v)

    def test_marker_above_a_blank_line_does_NOT_exempt(self) -> None:
        # The block must be contiguous; otherwise a marker written for one
        # print would silently license an unrelated one added later.
        v = self._scan_source(
            "fn a() {\n"
            "    // [vct-print-contract] belongs to something else\n"
            "\n"
            '    println!("unrelated");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)

    def test_cfg_test_item_does_not_blind_the_rest_of_the_file(self) -> None:
        v = self._scan_source(
            "#[cfg(test)]\nfn helper() {\n"
            '    println!("test noise");\n'
            "}\n"
            "pub fn prod() {\n"
            '    eprintln!("real diagnostic");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn(":6:", v[0])

    def test_cfg_any_test_debug_assertions_is_also_exempt(self) -> None:
        # `secrets::test_serialize` is gated exactly this way.
        v = self._scan_source(
            "#[cfg(any(test, debug_assertions))]\npub mod test_serialize {\n"
            '    pub fn f() { eprintln!("[vct-tests] WARN: flock failed"); }\n'
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_cfg_test_mod_and_semicolon_items_are_skipped(self) -> None:
        v = self._scan_source(
            "#[cfg(test)]\nmod tests {\n"
            '    fn helper() { println!("x"); }\n'
            "}\n"
            "#[cfg(test)]\nmod more_tests;\n"
            "#[cfg(test)]\nuse std::fs;\n"
        )
        self.assertEqual(v, [], v)

    def test_non_test_cfg_gates_are_still_scanned(self) -> None:
        # `#[cfg(windows)]` code ships to users; its prints are not exempt.
        v = self._scan_source(
            "#[cfg(windows)]\nfn win_only() {\n"
            '    eprintln!("windows diagnostic");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)

    def test_feature_test_support_string_is_not_a_test_cfg(self) -> None:
        # The cfg NAME is what matters. A string that merely contains the
        # letters "test" must not open a hole.
        v = self._scan_source(
            '#[cfg(feature = "testable-thing")]\nfn f() {\n'
            '    eprintln!("still a diagnostic");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)

    def test_comment_and_doc_mentions_are_ignored(self) -> None:
        v = self._scan_source(
            "//! module docs mention println!(\"x\") historically\n"
            "/// doc: use eprintln!() here? no.\n"
            "pub fn documented() {\n"
            "    let x = 1; // legacy println!(\"y\") note\n"
            "    let _ = x;\n"
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_print_macro_inside_a_string_literal_is_ignored(self) -> None:
        # An error message or doc example that quotes the macro name is not
        # a call site.
        v = self._scan_source(
            'fn a() {\n'
            '    let hint = "run println!(\\"x\\") to debug";\n'
            '    tracing::info!(hint, "explained");\n'
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_braces_inside_strings_do_not_break_the_cfg_test_skip(self) -> None:
        # A format string full of braces inside a test module must not
        # unbalance the brace counter and resume the scan mid-test-code.
        v = self._scan_source(
            "#[cfg(test)]\nmod tests {\n"
            '    fn helper() { println!("{{{{ unbalanced {"); }\n'
            '    fn other() { println!("more"); }\n'
            "}\n"
            "pub fn prod() {\n"
            '    eprintln!("real");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn(":7:", v[0])

    def test_multiline_string_literal_does_not_desync_the_lexer(self) -> None:
        """REGRESSION (found against the real tree): a Rust string literal
        that spans lines — the `"text \\` + continuation shape almost every
        migrated log message uses — must not make the CLOSING quote read as
        an OPENING one. When it did, the brace counter unbalanced and the
        `#[cfg(test)]` skip ended ~400 lines early, flagging four test-only
        prints in manifest.rs as production violations."""
        v = self._scan_source(
            "#[cfg(test)]\nmod tests {\n"
            "    fn a() {\n"
            "        eprintln!(\n"
            '            "[test skip] fixture not present \\\n'
            '             (path: {}) — skipping",\n'
            "            p.display()\n"
            "        );\n"
            "    }\n"
            "    fn b() {\n"
            '        eprintln!("second test print");\n'
            "    }\n"
            "}\n"
            "pub fn prod() {\n"
            '    eprintln!("the only real violation");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn(":15:", v[0])

    def test_block_comment_contents_are_ignored(self) -> None:
        # Block comments nest in Rust and can contain braces + macro text.
        v = self._scan_source(
            "/* historic note:\n"
            '   we used to call println!("x") here, and { braces } too\n'
            "   /* nested */\n"
            "*/\n"
            "pub fn prod() {\n"
            '    eprintln!("real");\n'
            "}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn(":6:", v[0])

    def test_raw_string_contents_are_ignored(self) -> None:
        v = self._scan_source(
            "pub fn a() {\n"
            '    let sql = r#"SELECT \'{\' , println!("x") -- not code"#;\n'
            "    tracing::debug!(sql, \"query\");\n"
            "}\n"
        )
        self.assertEqual(v, [], v)

    def test_multiline_print_macro_is_flagged_once(self) -> None:
        v = self._scan_source(
            "pub fn a() {\n"
            "    eprintln!(\n"
            '        "a long message {}",\n'
            "        value\n"
            "    );\n"
            "}\n"
        )
        self.assertEqual(len(v), 1, v)
        self.assertIn(":2:", v[0])

    def test_failure_message_names_both_remedies(self) -> None:
        v = self._scan_source('fn a() { println!("x"); }\n')
        self.assertEqual(len(v), 1, v)
        self.assertIn("tracing", v[0])
        self.assertIn(CONTRACT_MARKER, v[0])
        self.assertIn("probe.rs:1", v[0])


if __name__ == "__main__":
    unittest.main()
