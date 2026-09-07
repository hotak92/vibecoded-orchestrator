# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The `.no-shared-fallback` marker filename is ONE string in five files.

`docs/VCT_SECRETS_PRIMITIVE.md` §"Design choices" promises a project can opt
out of the SHARED file-store tier by placing
`~/.vct-secrets/projects/<NAME>/.no-shared-fallback`. Five components have to
agree on that exact filename or the control silently stops working:

    WRITER   launcher/src-tauri/vct-launcher-core/src/secrets_file_store.rs
             (`NO_SHARED_FALLBACK_MARKER`) — joined by the launcher's
             "Disable shared secrets for this project" toggle in
             `src/commands/secrets_cmd.rs`, which no longer repeats the
             literal, and READ by the same crate's
             `probe_shared_fallback`, which gates the launcher's status
             surfaces so a badge cannot claim a shared value serves a
             project that opted out.
    READERS  vco_lib/agent_secrets.py
             templates/scripts/vct_secrets_resolve.sh
             templates/scripts/vct_secrets_resolve.ps1
             tools/vct-secrets/vct

A one-character drift in any of them yields a marker nobody reads — which is
exactly the state this file was written to end: until v0.3.0 the writer and
the doc existed and NO reader did.

SCOPE OF THIS FILE — read before trusting it. This is a DATA-parity check on
a constant. It CANNOT prove any resolver consults the marker: a spelling
could match perfectly in a file whose gate was deleted. That question is
answered only by the behavioural suites, each of which is red-proofed by
mutating its resolver's call site:

    tests/test_agent_secrets.py                 (Python)
    tests/test_vct_secrets_resolve.sh           (bash template)
    tests/test_vct_secrets_resolve_ps1.py       (PowerShell template)
    tools/vct-secrets/tests/test_vct.sh         (vct CLI)
    secrets_file_store::tests + secrets_cmd::tests  (Rust: the marker
        makes `probe_shared_fallback` / the panel badge report Absent)

Do not add a "the gate is wired" assertion here by grepping for the helper's
name. A source scan is satisfied by a comment containing that name, which is
how a previous guard in this repo passed while the code it guarded was gone
(knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md, #8).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The canonical spelling. Changing it here is not enough — change it in all
#: five files below, and update the doc paragraph that promises it.
MARKER = ".no-shared-fallback"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _extract(rel: str, pattern: str) -> str:
    m = re.search(pattern, _read(rel), re.MULTILINE)
    assert m, f"no marker literal matching {pattern!r} found in {rel}"
    return m.group(1)


_CASES = [
    # (component, path, regex capturing the marker literal)
    (
        "python-reader",
        "vco_lib/agent_secrets.py",
        r'NO_SHARED_FALLBACK_MARKER\s*=\s*"([^"]+)"',
    ),
    (
        "bash-reader",
        "templates/scripts/vct_secrets_resolve.sh",
        r'^NO_SHARED_FALLBACK_MARKER="([^"]+)"',
    ),
    (
        "powershell-reader",
        "templates/scripts/vct_secrets_resolve.ps1",
        r'\$VctNoSharedFallbackMarker\s*=\s*"([^"]+)"',
    ),
    (
        "vct-cli-reader",
        "tools/vct-secrets/vct",
        r'^NO_SHARED_FALLBACK_MARKER="([^"]+)"',
    ),
    (
        "rust-const",
        "launcher/src-tauri/vct-launcher-core/src/secrets_file_store.rs",
        r'NO_SHARED_FALLBACK_MARKER:\s*&str\s*=\s*"([^"]+)"',
    ),
]


@pytest.mark.parametrize(("component", "rel", "pattern"), _CASES)
def test_component_spells_the_marker_canonically(component, rel, pattern):
    assert _extract(rel, pattern) == MARKER, (
        f"{component} ({rel}) disagrees with the canonical marker name. "
        "The writer and every reader must use the identical filename or the "
        "documented per-project shared-secret opt-out stops working."
    )


def test_the_rust_writer_joins_the_constant_instead_of_repeating_it():
    """The toggle must not re-spell the marker.

    It used to: `proj_dir.join(".no-shared-fallback")` sat in
    `secrets_cmd.rs` while the same string was about to be needed by the
    launcher's READ path too. Two literals in one crate is the drift this
    file exists to prevent, one language in.
    """
    rel = "launcher/src-tauri/src/commands/secrets_cmd.rs"
    src = _read(rel)
    assert "NO_SHARED_FALLBACK_MARKER" in src, (
        "the launcher's shared-secrets toggle no longer references the "
        "shared constant — check it has not gone back to a bare literal"
    )
    # Prose may NAME the marker (the doc comments explaining the gate do,
    # and should). Only CODE may not re-spell it, so strip whole-line
    # comments — `///`, `//!` and `//` alike — before looking.
    code = "\n".join(
        line
        for line in src.splitlines()
        if not line.lstrip().startswith("//")
    )
    assert MARKER not in code, (
        f"{MARKER!r} is spelled literally in non-comment code in {rel}; "
        "join secrets_file_store::NO_SHARED_FALLBACK_MARKER instead"
    )


def test_all_five_components_agree_with_each_other():
    """Belt-and-braces: pairwise agreement, not just agreement with MARKER."""
    found = {c: _extract(rel, pat) for c, rel, pat in _CASES}
    assert len(set(found.values())) == 1, f"marker spellings diverged: {found}"


def test_the_documented_promise_names_the_same_file():
    """The doc paragraph is part of the contract, not commentary.

    `VCT_SECRETS_PRIMITIVE.md` is what a user reads before trusting the
    opt-out; a doc naming a different filename would send them to create a
    marker no resolver looks for.
    """
    doc = _read("docs/VCT_SECRETS_PRIMITIVE.md")
    assert MARKER in doc, (
        "docs/VCT_SECRETS_PRIMITIVE.md no longer names the marker file. If "
        "the opt-out was removed, remove the writer and the four readers too."
    )
