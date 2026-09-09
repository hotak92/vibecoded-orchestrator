# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94: the RL hooks must hand their project root to the child they spawn.

The three RL telemetry hooks resolve a project root of their own::

    PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

…then spawn a Python child that calls ``emit_outcome_event``. Downstream of
that call ``EmbeddingService.for_project()`` is reached with NO explicit root
(via ``rl_enrichment._get_rl_telemetry_writer`` and
``weaviate_mcp.embeddings._get_embedding_service``), so
``embedding_service._detect_project_root`` runs its ladder — and the child's
env carries neither ``KG_BASE_DIR`` nor ``$VCT_ORCHESTRATOR_ROOT``, so rule 4
wins: ``Path.cwd()`` when that directory holds a ``.claude/``.

The child's cwd is whatever the harness handed the hook, which is not
guaranteed to be the project. Whatever it lands on gets its deferral ledger
reconciled — and a reconcile REWRITES that root's ``CLAUDE.md``. Under pytest
the cwd is this checkout, whose ``CLAUDE.md`` is tracked; that is the
``M CLAUDE.md`` mid-run leak (see
``tests/test_v0294_no_test_writes_repo_root_claude_md.py``).

The hook already KNOWS the right root. It just never told the child. So each
snippet now pins ``KG_BASE_DIR`` — rule 2 of the very same ladder, and already
the key that means "the project's folder path" everywhere else
(``vco_lib/config_projection.py`` writes it as ``str(proj.folder_path)``) —
with ``setdefault``, so an explicit VS Code / launcher value still outranks it.

Why the env key rather than a ``project_root=`` parameter threaded through
``emit_outcome_event``: the child reaches ``for_project()`` by more than one
route (``rl_enrichment.py`` twice, ``weaviate_mcp/embeddings.py`` once), and
the nearest joint on the emit route is ``_get_rl_telemetry_writer()`` — a
cached ZERO-ARG getter with a ``_get_rl_telemetry_writer_for`` sibling and
monkeypatching call sites. Widening it would fix one route and widen a shared
API; pinning the env at the child's entry covers every route in that process,
which is what "the root the hook knows must reach the resolver" actually asks
for.

These assertions are line-ANCHORED (pin before the emitter import, inside the
snippet) rather than substring-based: a mention of ``KG_BASE_DIR`` anywhere in
a hook would satisfy a bare ``in`` check while the child still resolved
rootlessly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HOOKS = REPO_ROOT / "templates" / "hooks"

EMITTER_IMPORT = (
    "from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event"
)

#: (hook file, interpolation token the snippet must use, the line that ASSIGNS
#: that token in the hook, the marker that opens the embedded Python source).
CASES = [
    ("post-edit-outcome.sh", "$PROJECT_ROOT", "PROJECT_ROOT=", '-c "'),
    ("post-bash-context-record.sh", "$PROJECT_ROOT", "PROJECT_ROOT=", '-c "'),
    ("pre-bash-context-inject.sh", "$PROJECT_ROOT", "PROJECT_ROOT=", '-c "'),
    ("post-edit-outcome.ps1", "$ProjectRoot", "$ProjectRoot =", '= @"'),
    ("post-bash-context-record.ps1", "$ProjectRoot", "$ProjectRoot =", '= @"'),
    ("pre-bash-context-inject.ps1", "$ProjectRoot", "$ProjectRoot =", '= @"'),
]

SETDEFAULT = "os.environ.setdefault('KG_BASE_DIR', _vco_project_root)"


def _lines(name: str) -> list[str]:
    return (HOOKS / name).read_text(encoding="utf-8").split("\n")


def _index_of(lines: list[str], predicate, what: str, name: str) -> int:
    hits = [i for i, ln in enumerate(lines) if predicate(ln)]
    assert hits, f"{name}: no line {what}"
    return hits[0]


@pytest.mark.parametrize("name,token,assign,snippet_open", CASES)
def test_hook_child_is_handed_the_project_root(name, token, assign, snippet_open):
    """The pin exists, is inside the snippet, and precedes the emitter import."""
    lines = _lines(name)
    pin_line = f"_vco_project_root = r'''{token}'''"

    pin = _index_of(lines, lambda ln: ln == pin_line, f"exactly {pin_line!r}", name)
    emit = _index_of(
        lines, lambda ln: EMITTER_IMPORT in ln, f"importing {EMITTER_IMPORT!r}", name
    )
    opens = _index_of(
        lines,
        lambda ln: ln.rstrip().endswith(snippet_open),
        f"opening the embedded Python with {snippet_open!r}",
        name,
    )
    assigns = _index_of(
        lines, lambda ln: ln.strip().startswith(assign), f"assigning {assign!r}", name
    )

    assert assigns < pin, (
        f"{name}: {token} is interpolated into the Python snippet at line "
        f"{pin + 1} but only assigned at line {assigns + 1} — the child would "
        "be pinned at an empty root."
    )
    assert opens < pin < emit, (
        f"{name}: the KG_BASE_DIR pin (line {pin + 1}) must sit INSIDE the "
        f"embedded Python (opened line {opens + 1}) and BEFORE the "
        f"emit_outcome_event import (line {emit + 1}). Downstream of that "
        "import EmbeddingService.for_project() resolves rootlessly and "
        "reconciles whatever directory cwd happens to name."
    )

    # The two lines that make the pin do something, immediately after it.
    assert lines[pin + 1] == "if _vco_project_root:", (
        f"{name}: expected the emptiness guard right after the pin, got "
        f"{lines[pin + 1]!r}"
    )
    assert lines[pin + 2] == f"    {SETDEFAULT}", (
        f"{name}: expected {SETDEFAULT!r} right after the guard, got "
        f"{lines[pin + 2]!r}"
    )

    fragment = "\n".join([pin_line.replace(token, "/tmp/x"), *lines[pin + 1:pin + 3]])
    compile(fragment, name, "exec")  # the snippet fragment is valid Python


@pytest.mark.parametrize("name", [c[0] for c in CASES if c[0].endswith(".sh")])
def test_sh_pin_block_carries_no_double_quote(name):
    """A ``"`` inside a .sh snippet ends the shell string, not the comment.

    The three POSIX hooks build their child source as ``python -c "…"`` — a
    DOUBLE-quoted shell word. A double quote anywhere in the interpolated
    block terminates it early, and the hook then dies on a syntax error at the
    next expansion instead of emitting telemetry. Caught while writing this
    block (the rationale comment originally quoted a phrase); pinned so the
    next editor of these comments learns it from a red test, not from a hook
    that silently stopped firing.
    """
    lines = _lines(name)
    start = _index_of(
        lines,
        lambda ln: ln.startswith("# v0.2.94: pin the project root"),
        "opening the pin block",
        name,
    )
    end = _index_of(lines, lambda ln: ln.strip() == SETDEFAULT, "the setdefault", name)
    block = lines[start:end + 1]
    offenders = [(start + i + 1, ln) for i, ln in enumerate(block) if '"' in ln]
    assert not offenders, (
        f'{name}: double quote(s) inside the python -c "…" shell word: '
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# $VCT_PROJECT_ROOT — the hand-off the pre-bash hook read but nobody performed
# ---------------------------------------------------------------------------
#
# ``pre-bash-context-inject.sh`` has always resolved its project_id as
# ``CLAUDE_PROJECT_DIR`` → ``VCT_PROJECT_ROOT`` → ``''``, and
# ``tests/test_v0292_env_keys_documented.py`` documents that second rung as
# "per-project root handed to embedded python by the pre-bash hook". Nothing
# in the tree ever SET it, so with ``CLAUDE_PROJECT_DIR`` unset the rung
# collapsed to ``''`` and every ``pre_bash`` event was emitted with a NULL
# project_id — while the two sibling hooks, which interpolate their root
# straight into the call, tagged theirs correctly. The ``.ps1`` sibling was
# worse still: it had no second rung at all, only ``''``.
#
# v0.2.94 makes the documented hand-off true rather than deleting the rung:
# the hook exports the root it already computed, on both OSes, and the two
# flavours now read it identically.

READER = (
    "    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', "
    "os.environ.get('VCT_PROJECT_ROOT', '')))"
)

#: (hook, the exact export line, the root token, that token's assignment).
PRE_BASH_CASES = [
    (
        "pre-bash-context-inject.sh",
        '      VCT_PROJECT_ROOT="$PROJECT_ROOT" \\',
        "$PROJECT_ROOT",
        "PROJECT_ROOT=",
    ),
    (
        "pre-bash-context-inject.ps1",
        "    $env:VCT_PROJECT_ROOT = $ProjectRoot",
        "$ProjectRoot",
        "$ProjectRoot =",
    ),
]


@pytest.mark.parametrize("name,export,token,assign", PRE_BASH_CASES)
def test_pre_bash_hook_exports_the_root_its_child_reads(name, export, token, assign):
    """Export, spawn, read — in that order, on both OSes.

    Ordering is the whole assertion: an export placed after the child is
    spawned is not an export at all, and a reader without one resolves ``''``.
    Red-proof: delete the export line and this goes red on the ordering
    assertion, not on a substring.
    """
    lines = _lines(name)
    pin_line = f"_vco_project_root = r'''{token}'''"

    assigns = _index_of(
        lines, lambda ln: ln.strip().startswith(assign), f"assigning {assign!r}", name
    )
    exports = _index_of(lines, lambda ln: ln == export, f"exactly {export!r}", name)
    pin = _index_of(lines, lambda ln: ln == pin_line, f"exactly {pin_line!r}", name)
    reader = _index_of(lines, lambda ln: ln == READER, f"exactly {READER!r}", name)

    assert assigns < exports, (
        f"{name}: VCT_PROJECT_ROOT is exported at line {exports + 1} from a "
        f"{token} only assigned at line {assigns + 1} — the child reads empty."
    )
    assert exports < pin < reader, (
        f"{name}: the export (line {exports + 1}) must precede the child it is "
        f"for (its embedded Python starts at line {pin + 1}) and the read of "
        f"VCT_PROJECT_ROOT inside it (line {reader + 1}). Ordering is what "
        "makes the hand-off real; presence alone is not."
    )


@pytest.mark.parametrize("name,token,_assign,_open", [(c[0], c[1], c[2], c[3]) for c in CASES])
def test_every_rl_hook_hands_resolve_for_project_a_root(name, token, _assign, _open):
    """No hook may fall back to ``''`` — by either mechanism, but by one.

    Two mechanisms are in use and both are fine: the two sibling hooks
    interpolate their root straight into the call, and pre-bash exports it and
    reads it back (its query is passed by env for quoting reasons, and the
    root rides the same channel). What is NOT fine is the third state this
    test exists to forbid — the pre-v0.2.94 ``.ps1``, which asked for
    ``CLAUDE_PROJECT_DIR`` and gave up, silently emitting NULL project_ids on
    Windows while the ``.sh`` looked correct.
    """
    lines = _lines(name)
    call = _index_of(
        lines,
        lambda ln: "resolve_for_project(os.environ" in ln,
        "calling resolve_for_project",
        name,
    )
    line = lines[call]

    interpolated = token in line
    via_env = "os.environ.get('VCT_PROJECT_ROOT', '')" in line
    assert interpolated or via_env, (
        f"{name}:{call + 1} resolves the project with no root at all — "
        f"{line.strip()!r}. Interpolate {token} or read the exported "
        "VCT_PROJECT_ROOT; do not fall back to ''."
    )
    if via_env:
        # The reader is only honest if this same file performs the export.
        assert any("VCT_PROJECT_ROOT" in ln and "resolve_for_project" not in ln
                   for ln in lines[:call]), (
            f"{name}: reads VCT_PROJECT_ROOT at line {call + 1} but never sets "
            "it — that is the promise this block was written to close."
        )
