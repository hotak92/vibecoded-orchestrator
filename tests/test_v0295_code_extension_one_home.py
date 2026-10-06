# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The code-file extension alternation has ONE home (v0.2.95, lane F10).

Before this, the same alternation was written out in eight shipped files,
each carrying a "MUST MATCH the other two" comment that only a human
reading all of them could enforce. ``_lib/code-extensions.{sh,ps1}`` is now
the home; ``pre-edit-context-inject``, ``pre-bash-context-inject`` and
``_lib/route-touched-path`` (which post-file-edit and post-bash-file-sync
both call) READ it.

Four pairs still carry their own literal, for reasons recorded in
``REMAINING_MIRRORS`` below. This is the one legitimate case for a
source-text ratchet named in CLAUDE.md — the subject IS a literal, and a
behavioural test cannot tell "the same alternation" from "a different
alternation that happens to accept this test's sample paths".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"

#: The one home, in both languages.
HOME_SH = HOOKS / "_lib" / "code-extensions.sh"
HOME_PS1 = HOOKS / "_lib" / "code-extensions.ps1"

#: Files that still spell the alternation out, and why they were not
#: migrated in this lane. Each is pinned to the home's literal below, so a
#: drift is a RED test rather than a silent divergence.
REMAINING_MIRRORS: dict[str, str] = {
    # v0.2.101 Wave 2: the pre-tool-use.{sh,ps1} mirrors were RETIRED with
    # their Read/Grep code-graph branches (the router hooks own those
    # surfaces now — pinned by test_codegraph_hook_gates_v0270.py's
    # retirement rows and test_v02101_pretool_use_read_branch_removed.py),
    # and the codegraph-query / command-noise-strip mirrors were retired
    # WITH those libs (wave-2 review SF-2).
    "code-graph-incremental.sh": "standalone entry point, not sourced by the context hooks",
    "code-graph-incremental.ps1": "sibling of the above",
    "stop-codegraph-drain.sh": "Stop-hook entry point, not sourced by the context hooks",
    "stop-codegraph-drain.ps1": "sibling of the above",
    # v0.2.101 Wave 2: the _lib/codegraph-query.{sh,ps1} mirrors (_CGQ_SOURCE_EXT_RE)
    # were RETIRED with the shell gates they served — the extension decision
    # for injection surfaces now lives in vco_lib/inject_intent.EXT_TO_LANG
    # (Python one-home, parity-pinned against the analyzer's dispatch table
    # by tests/test_v02101_structure_hook_format.py::TestExtLangOneHome).
}

_ALTERNATION = re.compile(
    r"\\?\.\((?P<body>py\|js\|[A-Za-z0-9|]*bash)\)\\?\$?"
)


def _home_alternation() -> str:
    body = HOME_SH.read_text(encoding="utf-8")
    match = re.search(r"^VCO_CODE_EXT_RE='(?P<re>[^']+)'$", body, re.MULTILINE)
    assert match, "code-extensions.sh must declare VCO_CODE_EXT_RE"
    inner = _ALTERNATION.search(match.group("re"))
    assert inner, f"unexpected shape for VCO_CODE_EXT_RE: {match.group('re')!r}"
    return inner.group("body")


def test_the_two_homes_agree() -> None:
    """The .sh and .ps1 homes encode the SAME decision."""
    sh = _home_alternation()
    ps1_body = HOME_PS1.read_text(encoding="utf-8")
    match = re.search(r"VcoCodeExtRe = '(?P<re>[^']+)'", ps1_body)
    assert match, "code-extensions.ps1 must declare $script:VcoCodeExtRe"
    inner = _ALTERNATION.search(match.group("re"))
    assert inner, f"unexpected shape for VcoCodeExtRe: {match.group('re')!r}"
    assert inner.group("body") == sh


@pytest.mark.parametrize("consumer", [
    "_lib/route-touched-path.sh",
])
def test_migrated_consumers_read_the_home_and_carry_no_literal(consumer: str) -> None:
    body = (HOOKS / consumer).read_text(encoding="utf-8")
    assert "vco_is_code_file" in body, (
        f"{consumer} must ask _lib/code-extensions.sh, not re-derive the answer"
    )
    assert not _ALTERNATION.search(body), (
        f"{consumer} still carries its own extension alternation; the point of "
        "_lib/code-extensions.sh is that it does not"
    )


@pytest.mark.parametrize("consumer", [
    "_lib/route-touched-path.ps1",
])
def test_migrated_ps1_consumers_read_the_home(consumer: str) -> None:
    body = (HOOKS / consumer).read_text(encoding="utf-8-sig")
    assert "Test-VcoIsCodeFile" in body, (
        f"{consumer} must ask _lib/code-extensions.ps1"
    )
    assert not _ALTERNATION.search(body), (
        f"{consumer} still carries its own extension alternation"
    )


@pytest.mark.parametrize("wrapper", [
    "pre-edit-context-inject.sh",
    "pre-bash-context-inject.sh",
    "pre-write-context-inject.sh",
    "pre-edit-context-inject.ps1",
    "pre-bash-context-inject.ps1",
    "pre-write-context-inject.ps1",
])
def test_injection_wrappers_carry_no_code_file_decision(wrapper: str) -> None:
    """v0.2.101 Wave 2: pre-edit/pre-bash became thin router wrappers — the
    "is this a code file" decision left the shell entirely (the router's
    Python core makes it via vco_lib/inject_intent.language_for_path, whose
    EXT_TO_LANG table is parity-pinned against the analyzer by
    tests/test_v02101_structure_hook_format.py). The wrappers must carry
    NEITHER a literal alternation NOR a vco_is_code_file call: a shell-side
    decision beside the Python one is exactly the mirror-drift class this
    file exists to prevent."""
    body = (HOOKS / wrapper).read_text(encoding="utf-8-sig")
    assert not _ALTERNATION.search(body), (
        f"{wrapper} carries its own extension alternation"
    )
    executable = "\n".join(
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith(("#", "<#"))
    )
    assert "vco_is_code_file" not in executable and "Test-VcoIsCodeFile" not in executable, (
        f"{wrapper} must not re-derive the code-file decision — the router owns it"
    )


@pytest.mark.parametrize("mirror", sorted(REMAINING_MIRRORS))
def test_remaining_mirrors_match_the_home(mirror: str) -> None:
    """A mirror that drifts from the home is a RED test.

    Red-proof: change one extension in any of these files (or in
    ``_lib/code-extensions.sh``) and this parametrisation fails naming the
    file that diverged.
    """
    path = HOOKS / mirror
    assert path.exists(), f"{mirror} is listed as a mirror but does not exist"
    body = path.read_text(encoding="utf-8-sig")
    found = {m.group("body") for m in _ALTERNATION.finditer(body)}
    assert found, (
        f"{mirror} no longer carries the alternation — if it was migrated to "
        "_lib/code-extensions, drop it from REMAINING_MIRRORS"
    )
    home = _home_alternation()
    assert found == {home}, (
        f"{mirror} has drifted from _lib/code-extensions "
        f"({REMAINING_MIRRORS[mirror]}): {sorted(found)} != {home!r}"
    )


def test_the_mirror_inventory_is_complete() -> None:
    """No shipped hook may carry the alternation without being declared.

    Without this, a NEW copy could be added tomorrow and the ratchet above
    would never look at it.
    """
    declared = set(REMAINING_MIRRORS) | {"_lib/code-extensions.sh", "_lib/code-extensions.ps1"}
    offenders: list[str] = []
    for path in sorted(HOOKS.rglob("*")):
        if not path.is_file() or path.suffix not in (".sh", ".ps1"):
            continue
        rel = path.relative_to(HOOKS).as_posix()
        if rel in declared:
            continue
        if _ALTERNATION.search(path.read_text(encoding="utf-8-sig")):
            offenders.append(rel)
    assert not offenders, (
        "these hooks carry the code-extension alternation but are not declared "
        "in REMAINING_MIRRORS — either read _lib/code-extensions or declare the "
        f"mirror with its reason: {offenders}"
    )
