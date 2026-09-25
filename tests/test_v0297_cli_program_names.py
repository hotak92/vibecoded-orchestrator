# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: no two shipped programs answer to the same command name, and every
``vco <verb>`` / ``vct-cli <verb>`` in shipped text names a verb that program
really has.

Why: from v0.2.33 to v0.2.96 VCO shipped two different programs called
``vco`` — the Rust launcher CLI (``launcher/tools/vct-cli``, ``[[bin]] name =
"vco"``) and the Python console script (``pyproject.toml`` ``[project.scripts]
vco``). Both defined ``vco project`` with disjoint subcommands, so whichever
came first on PATH silently hid the other, and a document telling the user to
run ``vco project list`` or ``vco project move`` was right on one machine and
an "invalid choice" on the next. The Rust binary is now ``vct-cli``.

Both halves read the REAL command trees and the real program inventory,
never a hand-kept copy — see ``tests/common/shipped_programs.py``.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.common.shipped_programs import (
    REPO,
    SKIP_PARTS,
    all_programs,
    command_name_collisions,
    command_trees,
    rust_cli_tree,
    tomllib,
)

#: Shipped text that is exempt from the verb check, and why.
#: - ``tests/``: a test may quote a wrong invocation on purpose, to pin a refusal.
#: - ``CHANGELOG.md``: history. Past entries correctly say ``vco hooks enable``
#:   for the releases in which the Rust binary was called ``vco``.
#: - ``cli_verbs.json``: the table itself.
EXEMPT_PREFIXES = ("tests/",)
EXEMPT_FILES = {"CHANGELOG.md", "launcher/tools/vct-cli/cli_verbs.json"}

TEXT_SUFFIXES = {
    "", ".md", ".py", ".rs", ".sh", ".ps1", ".psm1", ".ts", ".js", ".svelte",
    ".toml", ".json", ".yml", ".yaml", ".txt", ".bat", ".cmd", ".sql",
    ".template", ".html", ".command", ".desktop", ".cfg", ".ini",
}

INVOCATION = re.compile(
    r"(?<![A-Za-z0-9_./\\$-])(?P<prog>vco|vct-cli)(?:\.exe)?"
    r"(?P<args>(?:[ \t]+[a-z][a-z0-9-]*){1,2})"
)
#: Inline code: ``RST double`` or `markdown single` backticks, one line.
CODE_SPAN = re.compile(r"``([^`\n]+)``|`([^`\n]+)`")
FENCE = re.compile(r"^\s*(```|~~~)")
PROMPT = re.compile(r"[ \t]*(?:(?:\$|>|PS>)[ \t]+)?")
SHELL_OPERATOR = re.compile(r"(?:&&|\|\||\||;|\$\()[ \t]*")


def test_the_inventory_sees_every_kind_of_program() -> None:
    """A vacuous inventory would pass the collision check forever."""
    names = {name for name, _ in all_programs()}
    assert {"vct-hub", "vct-updater", "vct-cli"} <= names, names
    assert "vco" in names, "pyproject's `vco` console script is missing"
    assert "vct" in names, "the secrets CLI tools/vct-secrets/vct is missing"


def test_no_two_shipped_programs_share_a_command_name() -> None:
    collisions = command_name_collisions(all_programs())
    assert not collisions, (
        "two shipped programs answer to the same command name; whichever is "
        "first on PATH hides the other: "
        + "; ".join(f"{name}: {sorted(origins)}" for name, origins in sorted(collisions.items()))
    )


def test_the_collision_check_catches_a_duplicate() -> None:
    """The check itself: the pre-v0.2.97 shape (a Cargo bin and a pyproject
    script both named ``vco``) is reported; siblings of one tool are not."""
    programs = [
        ("vco", "launcher/tools/vct-cli/Cargo.toml [[bin]]"),
        ("vco", "pyproject.toml [project.scripts]"),
        ("vct", "tools/vct-secrets/vct{,.sh,.ps1,.cmd,.bat}"),
        ("vct", "tools/vct-secrets/vct{,.sh,.ps1,.cmd,.bat}"),
    ]
    assert command_name_collisions(programs) == {
        "vco": {"launcher/tools/vct-cli/Cargo.toml [[bin]]", "pyproject.toml [project.scripts]"}
    }


def test_the_rust_verb_table_names_the_cargo_binary() -> None:
    program, verbs = rust_cli_tree()
    manifest = tomllib.loads(
        (REPO / "launcher" / "tools" / "vct-cli" / "Cargo.toml").read_text(encoding="utf-8")
    )
    assert [b["name"] for b in manifest["bin"]] == [program]
    assert "project" in verbs and "list" in verbs["project"]


# ─── every `vco <verb>` / `vct-cli <verb>` in shipped text ──────────────


def _shipped_text_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    files = []
    for rel in proc.stdout.splitlines():
        if not rel or rel in EXEMPT_FILES or rel.startswith(EXEMPT_PREFIXES):
            continue
        if SKIP_PARTS.intersection(Path(rel).parts):
            continue
        if Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        files.append(rel)
    assert len(files) > 500, "the shipped-file listing is implausibly small"
    return files


def _after_prompt(text: str, pos: int) -> int:
    prompt = PROMPT.match(text, pos)
    return prompt.end() if prompt else pos


def _command_starts(text: str, markdown: bool) -> set[int]:
    """Offsets at which a COMMAND begins, as opposed to prose: the start of an
    inline code span, and in a markdown fenced block the first word of a line
    or the word after a shell operator (``&&``, ``||``, ``|``, ``;``,
    ``$(``). A prompt (``$ ``, ``> ``) before it is skipped. A ``vco`` later
    in a span or a line — a shell comment, a sentence quoted in code — is not
    a command start."""
    starts: set[int] = set()
    for m in CODE_SPAN.finditer(text):
        inner = m.start(1) if m.group(1) is not None else m.start(2)
        starts.add(_after_prompt(text, inner))
    if markdown:
        offset, fenced = 0, False
        for line in text.splitlines(keepends=True):
            if FENCE.match(line):
                fenced = not fenced
            elif fenced:
                starts.add(offset + _after_prompt(line, 0))
                for op in SHELL_OPERATOR.finditer(line):
                    starts.add(offset + op.end())
            offset += len(line)
    return starts


def classify_invocations(
    text: str,
    trees: dict[str, dict[str, set[str]]],
    *,
    markdown: bool = False,
) -> list[str]:
    """Problems with each ``vco …`` / ``vct-cli …`` invocation in ``text``.

    In a command context (inline code, a fenced block) the first word must be
    a verb of THAT program, and a subverb, when the verb has them, must be
    one of its subverbs. In prose ``vco`` is also the product's short name
    ("vco always creates…"), so there only a word that belongs to the OTHER
    program is reported — the exact mix-up the two ``vco``s made possible.
    """
    starts = _command_starts(text, markdown)
    problems = []
    for m in INVOCATION.finditer(text):
        prog = m.group("prog")
        own = trees[prog]
        other = trees["vct-cli" if prog == "vco" else "vco"]
        words = m.group("args").split()
        strict = m.start() in starts
        line = text.count("\n", 0, m.start()) + 1
        where = f"line {line}: `{prog} {' '.join(words)}`"
        verb = words[0]
        if verb not in own:
            if strict or verb in other:
                owner = " (a verb of the other program)" if verb in other else ""
                problems.append(f"{where}: `{verb}` is not a {prog} verb{owner}")
            continue
        if len(words) < 2 or not own[verb]:
            continue
        sub = words[1]
        if sub in own[verb]:
            continue
        if strict or sub in other.get(verb, set()):
            problems.append(f"{where}: `{verb} {sub}` is not a {prog} command")
    return problems


def _trees() -> dict[str, dict[str, set[str]]]:
    return command_trees()


def test_both_trees_are_real_and_disjoint_where_they_meet() -> None:
    """The two CLIs share one verb, ``project``, with no common subcommand —
    the Phase-0 finding that made the rename safe. A future operation added to
    both would be a divergence to resolve, not a second copy to keep."""
    trees = _trees()
    py, rs = trees["vco"], trees["vct-cli"]
    assert {"doctor", "project", "verify-pins"} <= set(py)
    assert {"project", "hooks", "telemetry", "kg", "codegraph"} <= set(rs)
    for verb in set(py) & set(rs):
        shared = (py[verb] & rs[verb]) - {"help"}
        assert not shared, f"`{verb}` {sorted(shared)} is implemented by BOTH CLIs"


def test_every_vco_and_vct_cli_invocation_in_shipped_text_is_real() -> None:
    trees = _trees()
    failures = []
    for rel in _shipped_text_files():
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "vco" not in text and "vct-cli" not in text:
            continue
        for problem in classify_invocations(text, trees, markdown=rel.endswith(".md")):
            failures.append(f"{rel} {problem}")
    assert not failures, (
        "shipped text names a command its program does not have. Rust launcher "
        "verbs (project list/create/rename/delete/show, module, audit, license, "
        "hooks, telemetry, hub, kg, codegraph) are `vct-cli …`; `vco …` is the "
        "Python CLI (doctor, verify-*, project move / rename-collections, …):\n  "
        + "\n  ".join(failures)
    )


@pytest.mark.parametrize(
    ("text", "markdown", "expected"),
    [
        # the Rust verbs under the Python name — the pre-rename docs
        ("run `vco telemetry status`", False, ["`telemetry` is not a vco verb (a verb of the other program)"]),
        ("vco project list | jq .", False, ["`project list` is not a vco command"]),
        ("```\nvco hub health\n```\n", True, ["`hub` is not a vco verb (a verb of the other program)"]),
        # the Python verbs under the Rust name
        ("`vct-cli doctor`", False, ["`doctor` is not a vct-cli verb (a verb of the other program)"]),
        ("vct-cli project move x", False, ["`project move` is not a vct-cli command"]),
        # an invented verb inside code is reported; in prose it is the product name
        ("`vco codegraph-render x`", False, ["`codegraph-render` is not a vco verb"]),
        ("vco always creates the class", False, []),
        # a `vco` that does not START the code is not a command
        ("```\npython install.py --adopt  # writes vco collections\n```\n", True, []),
        ("```\n$ vct-cli hub health && vco verify-pins\n```\n", True, []),
        ("```\ncd x && vco hub url\n```\n", True, ["`hub` is not a vco verb (a verb of the other program)"]),
        # correct invocations
        ("`vco doctor`, `vco project move`, `vct-cli project list`", False, []),
        ("```\nvct-cli codegraph search q --project p\n```\n", True, []),
    ],
)
def test_the_classifier(text: str, markdown: bool, expected: list[str]) -> None:
    problems = classify_invocations(text, _trees(), markdown=markdown)
    assert [p.split(": ", 2)[2] for p in problems] == expected


def test_vct_cli_reads_the_file_the_telemetry_uploader_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``vct-cli telemetry pending`` reads where the uploader parks events:
    ``<home>/.vibecoded/telemetry_pending.jsonl``.

    This is the PYTHON half of the parity pin and asserts behaviour only —
    the uploader runs for real under a scratch home and must land the file at
    exactly that layout. The RUST half is
    ``launcher/tools/vct-cli/tests/cli_telemetry_pending.rs`` (the repo's
    existing black-box pattern: it runs the BUILT binary via
    ``CARGO_BIN_EXE_vct-cli`` and asserts the ``path`` it reports is the same
    layout under its scratch home). The old regex read of ``main.rs``'s
    constants is gone: a wrong-but-dead constant passed it, and the pytest
    suite never builds the Rust binary to check the live one.
    """
    from VCThelpers.telemetry import uploader

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert uploader._write_pending_jsonl([{"event": "probe"}]) == 1

    written = tmp_path / ".vibecoded" / "telemetry_pending.jsonl"
    assert written.read_text(encoding="utf-8") == '{"event":"probe"}\n'
