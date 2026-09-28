"""v0.2.98 — a shipped GUI pointer must name a surface the launcher has.

A navigation pointer in user-facing text is a printed command: it has to name
something that exists and lead the reader to the intended control. Nothing
executes it, so nothing fails when it is wrong — the whole repo carried
``Preferences -> Special Secrets`` in 19 live places across 12 files (README,
docs, install.py, vco_lib, vct-module.json, the ORCHESTRATOR template,
search_mcp/wrapper.sh) while no launcher version ever had that label
(``git log --all -S "Special Secrets" -- launcher/`` is empty). The launcher's
own surfaces are:

* sidebar entry ``label: 'Secrets'`` at ``/preferences/secrets``
* Preferences section ``<h2 class="pr-section-title">Secrets</h2>``
* the tab a shared secret is added under: ``Shared (this user)``

The worst instance was not a docs line but a live remedy:
``vco_lib/codegraph_deferrals.py::_service_hint`` printed
``Preferences -> Special Secrets -> OpenAI -> Re-check`` into the
``command_to_apply`` field of a deferral entry — text the user is told to act
on, naming a row and a button that do not exist anywhere in the secrets panel.

These tests pin the invariant, not the prose: the labels come from the
launcher source, and the pointer under test is checked against them.

Three shapes hide a phrase from a contiguous search, and all three were found
in this tree rather than imagined:

* **string-literal glue** — ``"… Special " "Secrets …"`` (Python implicit
  concatenation) or ``'… Special ' + 'Secrets …'`` (JavaScript);
* **a backslash continuation** — ``"… Special \\\nSecrets …"`` in shell or Python;
* **hard-wrapped prose** — a blockquote or an 80-column wrap that puts
  ``Special`` at the end of one Markdown line and ``Secrets`` at the start of
  the next. This one survived the 19-site sweep *and* the first version of
  this guard, in ``tools/vct-secrets/MIGRATION.md``.

Each fold below is the reader's view: what the compiler joins, or what the eye
reads as one sentence, is one string.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER_SRC = REPO_ROOT / "launcher" / "src"

# The three files that define the secrets surface the pointers name.
SURFACE_FILES = {
    "sidebar": LAUNCHER_SRC / "lib" / "components" / "Sidebar.svelte",
    "preferences": LAUNCHER_SRC / "routes" / "preferences" / "+page.svelte",
    "panel": LAUNCHER_SRC / "lib" / "components" / "SecretsPanel.svelte",
}

ARROW = "→"
LEGACY_PHRASE = "Special Secrets"

# Everything a pointer could be written in. Pruned directories are the ones
# that do not ship text: vendored deps, the Rust build dir, the bundled
# binaries, and Python caches.
SCANNED_SUFFIXES = {".py", ".md", ".sh", ".ps1", ".json", ".svelte", ".ts", ".template"}
PRUNED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "target", "dist",
    "build", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}
# This guard file is exempt by necessity: it has to contain the phrase in
# order to search for it, and it is a test, not shipped guidance. Everything
# else is scanned, CHANGELOG.md included — see `_live_text` for why only its
# released sections are exempt.
SCAN_EXEMPT = {"tests/test_v0298_launcher_pointer.py"}


# A pointer can be SPLIT across string-literal glue, and the split hides it
# from a text search: ``"… Special " "Secrets …"`` (Python implicit
# concatenation), ``'… Special ' + 'Secrets …'`` (JavaScript) and
# ``f"… Special {'Secrets'}…"`` all read as one sentence to every user and as
# two unrelated fragments to a scanner. This is not hypothetical — it is how
# this cycle re-introduced the phrase it had just swept, in the emitter moved
# aside for being correct in every other respect. The collapse is deliberately
# dumb (no parser) because it also has to run on .md/.json/.svelte/.template,
# wherever there is no grammar to lean on; Python, which does have one, is
# read through `ast` as well (see `_python_string_values`).
_LITERAL_GLUE = re.compile(r"""(?P<q>["'])\s*(?:\+\s*)?\n?\s*(?P=q)""")
_BACKTICK_SPAN = re.compile(r"`[^`\n]*`")
# Formats where a line break is a wrap, not a boundary: joining their lines is
# reading them correctly. Deliberately NOT every suffix — in a .json or a .py,
# two adjacent lines are unrelated tokens and joining them could invent a
# phrase nobody wrote.
PROSE_SUFFIXES = {".md", ".template"}


def _collapse_literal_glue(text: str) -> str:
    """Join literals the compiler joins, leaving their contents intact.

    A comma or any other token between the quotes stops the match, so
    ``["Special", "Secrets"]`` — two genuinely separate strings — is not
    glued into one.
    """
    for _ in range(10):  # a chain of N literals needs N-1 passes
        text, hits = _LITERAL_GLUE.subn("", text)
        if not hits:
            break
    return text


_BACKSLASH_CONTINUATION = re.compile(r"\\\n[ \t]*")
# Markdown structures that BEGIN a block rather than continue a sentence.
# Split in two, because they differ in what they do to the line BELOW them: a
# list item continues into an indented line (lazy/indented continuation, the
# normal way a long list item is wrapped), while a heading, a table row or a
# fence is closed and owns nothing below it.
_HARD_BLOCK_START = re.compile(r"^(#|\||```|={3,}|<)")
_LIST_ITEM_START = re.compile(r"^([-*+] |\d+[.)] )")


def _unwrap_prose(text: str) -> str:
    """Join hard-wrapped prose the way the eye reads it.

    Markdown has no significance for a line break mid-sentence: a wrap, a
    blockquote marker and the leading spaces are all invisible to the reader.
    Joining them is what makes a wrapped pointer findable — and the block
    markers above keep genuinely separate blocks (list items, headings, table
    rows, fences) apart, so two neighbours are never glued into a phrase that
    nobody wrote.
    """
    out: list[str] = []
    prev = "fresh"  # "fresh" | "hard" | "list" | "plain"
    for line in text.splitlines():
        # Drop a blockquote marker but KEEP the indentation that follows it:
        # in ">   Secrets" the three spaces are what make the line a
        # continuation, and lstripping first would erase the evidence.
        quote = re.match(r"^[ \t]*>", line)
        body = line[quote.end():] if quote else line
        indented = body[:1] in (" ", "\t")
        stripped = body.strip()
        if not stripped:
            out.append("\n\n")
            prev = "fresh"
        elif _LIST_ITEM_START.match(stripped):
            out.append("\n" + stripped)
            prev = "list"
        elif _HARD_BLOCK_START.match(stripped):
            out.append("\n" + stripped)
            prev = "hard"
        elif indented and prev == "list":
            out.append(" " + stripped)
        elif prev == "plain":
            out.append(" " + stripped)
        else:
            out.append("\n" + stripped)
            prev = "plain"
    return "".join(out)


def _python_string_values(text: str) -> list[str]:
    """Every string a Python file holds, folded the way the compiler folds it.

    Adjacent literals are ONE string to Python and two fragments to a text
    scanner; `ast` hands back the folded value, which is what makes the split
    case visible. An unparseable file yields nothing — this test suite has no
    business failing over a source it cannot read.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _live_text(path: pathlib.Path, text: str) -> str:
    """The part of ``path`` that is guidance a reader acts on today.

    For CHANGELOG.md that is the `[Unreleased]` section only. A `## [x.y.z]`
    section below it is a record of what was written at that release; a
    historical note is not an instruction, and rewriting it would falsify the
    record — but a NEW entry is written by whoever adds it, so it stays scanned.
    """
    if path.name == "CHANGELOG.md":
        parts = re.split(r"(?m)^## \[", text)
        if len(parts) > 1:
            text = parts[0] + "## [" + parts[1]
    # A QUOTED mention is not a pointer. The distinction is the whole point of
    # this file: a pointer is bare text a reader follows, while a changelog
    # entry describing the sweep has to name the phrase it removed — and it
    # writes it in backticks. Every one of the 19 live pointers was bare.
    live = _collapse_literal_glue(
        _BACKSLASH_CONTINUATION.sub("", _BACKTICK_SPAN.sub("", text))
    )
    if path.suffix in PROSE_SUFFIXES:
        live = _unwrap_prose(live)
    if path.suffix == ".py":
        # The glue collapse above sees same-quote neighbours only; a Python
        # file is read a second time as the strings it actually contains, so
        # mixed quotes and parenthesised continuations are covered too.
        live += "\n" + "\n".join(
            _BACKTICK_SPAN.sub("", value)
            for value in _python_string_values(text)
        )
    return live


def _surface_text() -> str:
    missing = [str(p) for p in SURFACE_FILES.values() if not p.is_file()]
    if missing:
        raise AssertionError(f"launcher surface files missing: {missing}")
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in SURFACE_FILES.values())


class LauncherSurfaceLabelTests(unittest.TestCase):
    """The labels themselves, read from the launcher source."""

    def test_sidebar_entry_for_the_secrets_route_is_labelled_secrets(self) -> None:
        sidebar = SURFACE_FILES["sidebar"].read_text(encoding="utf-8")

        # The route literal and its label live in the same object entry; take
        # the label that follows the href rather than any other 'Secrets' word.
        match = re.search(
            r"href:\s*'/preferences/secrets',\s*\n\s*label:\s*'([^']+)'", sidebar
        )
        self.assertIsNotNone(
            match, "no sidebar entry found for /preferences/secrets"
        )
        self.assertEqual(match.group(1), "Secrets")

    def test_the_shared_tab_a_shared_key_is_added_to_exists(self) -> None:
        panel = SURFACE_FILES["panel"].read_text(encoding="utf-8")
        self.assertIn("Shared (this user)", panel)

    def test_preferences_page_carries_a_secrets_section(self) -> None:
        prefs = SURFACE_FILES["preferences"].read_text(encoding="utf-8")
        self.assertIn("pr-section-title\">Secrets</h2>", prefs)


class OpenaiRemedyTests(unittest.TestCase):
    """The live remedy that used to point at a row and a button that do not exist."""

    def _hint(self) -> str:
        from vco_lib.codegraph_deferrals import _service_hint

        return _service_hint("openai_embed")[1]

    def test_every_segment_after_preferences_is_a_real_surface_label(self) -> None:
        hint = self._hint()
        surface = _surface_text()
        for line in hint.splitlines():
            if ARROW not in line:
                continue
            segments = [s.strip() for s in line.split(ARROW)]
            self.assertEqual(segments[0], "Preferences")
            for seg in segments[1:]:
                seg = seg.rstrip(")")
                self.assertIn(
                    seg, surface,
                    f"pointer segment {seg!r} is not a launcher surface label",
                )

    def test_remedy_names_the_commands_that_actually_repair_it(self) -> None:
        hint = self._hint()
        # A probe that answers without reading the value, and the store command
        # whose value arrives on stdin (never in argv or shell history).
        self.assertIn("vct can-read --key openai_api_key", hint)
        self.assertIn("vct set --shared --key openai_api_key", hint)
        self.assertIn("stdin", hint)

    def test_remedy_does_not_name_a_control_that_does_not_exist(self) -> None:
        # The only 'Re-check' buttons in the launcher are in
        # InstallHealthGate.svelte and concern install health. The secrets
        # panel has none, so a remedy must not send the user looking for one.
        self.assertNotIn("Re-check", self._hint())


class SplitLiteralTests(unittest.TestCase):
    """The phrase is invisible to a text scan once it is split in two.

    Red-proof for this class came from the cycle that wrote it: the sweep had
    corrected every site, and the phrase came back in the very file the sweep
    had just touched, because the replacement text was written as two adjacent
    literals. A guard that reads raw bytes cannot see that — and neither could
    the sweep that produced it.
    """

    def test_python_implicit_concatenation_across_a_line_break(self) -> None:
        text = 'HINT = ("Open it in Preferences → Special "\n        "Secrets → Shared (this user).")\n'
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.py"), text))

    def test_python_adjacent_literals_with_mixed_quotes(self) -> None:
        # Python also concatenates a single- and a double-quoted neighbour; the
        # glue collapse is anchored on a repeated quote, so this shape is what
        # the `ast` pass is for.
        text = "HINT = 'Preferences → Special ' \"Secrets → Shared\"\n"
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.py"), text))

    def test_javascript_concatenation_with_a_plus(self) -> None:
        text = "const hint = 'Preferences → Special ' +\n  'Secrets → Shared (this user)'\n"
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.ts"), text))

    def test_the_non_python_path_collapses_glue_on_its_own(self) -> None:
        # There is no `ast` for a .svelte/.md/.json/.template file, so the dumb
        # collapse is the only instrument there and has to work unaided.
        text = 'let s = "Preferences → Special " "Secrets → Shared";\n'
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.svelte"), text))

    def test_a_quoted_mention_stays_exempt_once_folded(self) -> None:
        # The exemption has to survive the fold: a Python string that NAMES the
        # phrase in backticks records the sweep, it does not point anywhere.
        text = 'NOTE = "we removed `Special Secrets` in v0.2.98, favoring `Secrets`"\n'
        self.assertNotIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.py"), text))

    def test_genuinely_separate_strings_are_not_glued_together(self) -> None:
        # The false-positive side of the collapse: a comma is a real boundary,
        # in Python and in the text-only formats.
        for name in ("x.py", "x.svelte"):
            with self.subTest(name=name):
                text = 'WORDS = ["Special", "Secrets"]\n'
                self.assertNotIn(LEGACY_PHRASE, _live_text(pathlib.Path(name), text))

    def test_a_pointer_hard_wrapped_in_markdown_is_still_found(self) -> None:
        # The straggler this fold was written for: a blockquote wraps mid
        # pointer, so the phrase sits on two lines and no contiguous search —
        # not the 19-site sweep, not the first version of this guard — sees it.
        text = (
            "> written via the launcher GUI (OnboardingWizard / Preferences → Special\n"
            "> Secrets), then resolved through vct-hub.\n"
        )
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.md"), text))

    def test_a_pointer_wrapped_inside_the_orchestrator_template(self) -> None:
        text = "2. Store it in the launcher: Preferences → Special\n   Secrets.\n"
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("CLAUDE.md.template"), text))

    def test_a_backslash_continuation_is_collapsed(self) -> None:
        text = 'HINT="Preferences → Special \\\nSecrets"\n'
        self.assertIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.sh"), text))

    def test_markdown_blocks_are_not_glued_into_a_phrase_nobody_wrote(self) -> None:
        # The false-positive side of the prose fold: these are separate units
        # of text, and reading them as one sentence would be a lie.
        for text in (
            "- Special\n- Secrets\n",
            "# Special\nSecrets\n",
            "| Special |\n| Secrets |\n",
            "```\nSpecial\n```\nSecrets\n",
        ):
            with self.subTest(text=text):
                self.assertNotIn(
                    LEGACY_PHRASE, _live_text(pathlib.Path("x.md"), text)
                )

    def test_a_json_file_is_not_read_as_prose(self) -> None:
        # Line-per-key JSON: joining lines there would invent strings that the
        # file does not contain, so the prose fold is off for it.
        text = '{"a": "Special",\n "b": "Secrets"}\n'
        self.assertNotIn(LEGACY_PHRASE, _live_text(pathlib.Path("x.json"), text))

    def test_a_file_python_cannot_parse_is_still_read_as_text(self) -> None:
        # Half-written source is not this guard's business to reject: the raw
        # scan still runs, the `ast` pass simply contributes nothing.
        text = "def broken(:\n    Preferences → Special Specials\n"
        self.assertEqual(_live_text(pathlib.Path("x.py"), text).count(LEGACY_PHRASE), 0)


class NoLegacyPointerTests(unittest.TestCase):
    """The phrase cannot come back through a later copy-paste."""

    def _offenders(self) -> list[str]:
        found: list[str] = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            rel = path.relative_to(REPO_ROOT)
            if any(part in PRUNED_DIRS for part in rel.parts):
                continue
            if rel.as_posix() in SCAN_EXEMPT:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if LEGACY_PHRASE in _live_text(path, text):
                found.append(rel.as_posix())
        return found

    def test_no_shipped_file_names_the_legacy_label(self) -> None:
        self.assertEqual(self._offenders(), [])

    def test_the_canonical_pointer_is_the_one_the_launcher_shows(self) -> None:
        # Guards the sweep itself: a pointer that says only 'Secrets' is
        # correct; one that says 'Special Secrets' is not (covered above), and
        # one that stops at the section without naming the tab is what this
        # asserts is NOT what the remedy prints.
        hint = OpenaiRemedyTests()._hint()
        self.assertNotIn(f"Preferences {ARROW} Secrets\n", hint)


if __name__ == "__main__":
    unittest.main()