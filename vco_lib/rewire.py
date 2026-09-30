# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Install-time rewriter for the ``VCO-REWIRE`` regions in shipped scripts
(v0.2.92 WP-16 / ruling R4+R21).

WHY THIS MODULE EXISTS — the history, so nobody re-derives it
-------------------------------------------------------------
The ``VCO-REWIRE-BEGIN/END: orchestrator-root-resolution`` sentinels landed in
commit ``bff46b8f`` ("sentinel-marked rewire blocks for 7 PR-2 scripts"). They
were NOT a promise of an install-time rewriter: they told the TEMPLATE-DRIFT
GATE (``.github/scripts/check_template_drift.py``) to strip those blocks before
comparing the orchestrator's own ``.claude/scripts/X`` against
``templates/scripts/X``. That gate was RETIRED in PR-39 / v0.2.12
(``.github/workflows/hook-parity.yml``: *"The two former drift gates … were
removed"*) and ``git ls-files .claude/scripts`` returns 0 in this repo — so
from v0.2.12 until now the ten sentinels had NO live consumer at all.

Ruling R21 kept R4 and chose to CONCRETIZE the convention rather than delete
it, because a compliant rewriter is cheap here and has a real user-facing
benefit: an installed script under ``<project>/.claude/scripts/`` currently
finds its orchestrator clone only through ``$VCT_ORCHESTRATOR_ROOT`` /
``$VCT_INSTALL_ROOT`` (its third rung, ``<script>/../..``, resolves to the USER
PROJECT root, which has no ``claude_mcp_servers/``). Run one of those scripts
from a plain shell with neither variable set and it cannot reach the clone.
Baking the install's own root into the region fixes exactly that case.

THE SPLIT WITH WP-15 — two mechanisms, one principle
----------------------------------------------------
WP-15 (``deferral_report.strip_vco_owned_regions``) makes a VCO-owned region
INVISIBLE to a comparison, so VCO's own injected content is never reported as
user divergence. That is the right tool for CLAUDE.md, where VCO injects into a
file the user also owns.

It is the WRONG tool here, and deliberately not used: these scripts are
VCO-owned code end to end, and the bundle's manifest compare must keep SEEING
a user edit inside the region. So this module takes the other route — the
rewritten bytes ARE the shipped bytes. ``_file_action`` hashes POST-transform
(``source_bytes``), the manifest records that hash, so an untouched rewritten
file classifies ``noop``/``overwrite`` and a user edit inside the region
classifies honestly as a divergence. Strip-at-compare here would make a user's
edit invisible and then silently overwrite it.

WHAT GETS BAKED
---------------
The shared path vocabulary (``vco_lib.materialize.PATH_KEYS``), and only
INSIDE a region. v0.2.100 WP-18: the rendering itself — token grammar,
per-language escaping, the unknown-placeholder rule — lives in
``vco_lib.materialize``; this module keeps only what is region-specific (the
sentinel grammar and the span walk). The vocabulary is the SAME set the
agent renderer and the moved-clone heal use (one ``path_subs``), which is
what retired the old "``{{PROJECT_ROOT}}`` is excluded here" drift. A name
outside it is left in place, warned about and recorded as a deferral row —
never a failed install.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional, Tuple

from vco_lib import materialize as _mz

if TYPE_CHECKING:  # pragma: no cover
    from vco_lib.materialize import RenderResult

__all__ = [
    "REWIRE_BEGIN",
    "REWIRE_END",
    "has_rewire_region",
    "render_regions",
    "rewire_bytes",
    "rewire_transform",
    "rewire_subs",
]

REWIRE_BEGIN = "VCO-REWIRE-BEGIN: orchestrator-root-resolution"
REWIRE_END = "VCO-REWIRE-END: orchestrator-root-resolution"


def rewire_subs(orchestrator_root: Path,
                project_root: Optional[Path] = None) -> Dict[str, str]:
    """The placeholder → value map applied inside a region.

    A thin view of :func:`vco_lib.materialize.path_subs` — the ONE path
    vocabulary the agent renderer and the moved-clone heal
    (``project_init._stale_orchestrator_root_heal_match``) share, pinned by
    ``tests/test_v0292_n35_rewire_transform.py::
    test_vocabulary_matches_the_stale_root_heal_map``.
    """
    return _mz.path_subs(orchestrator_root, project_root)


def _escape_for(filename: str, value: str) -> str:
    """Escape ``value`` for the region's literal context — delegated to the one
    escaping home, :func:`vco_lib.materialize.escape_value` (``.py`` →
    double-quoted Python literal, ``.ps1`` → single-quoted PowerShell literal,
    anything else → a double-quoted POSIX word)."""
    return _mz.escape_value(value, _mz.escape_for_filename(filename))


def _region_line_spans(lines: List[str], filename: str) -> List[Tuple[int, int]]:
    """Return ``[(begin_idx, end_idx)]`` line index pairs, one per region.

    A line marks a boundary when it CONTAINS the sentinel — the sentinels live
    in comments whose prefix differs per language and per indentation level
    (``# ``, ``    # ``), so an equality match would silently miss the two
    indented Python regions.

    Raises ``ValueError`` naming the file on any imbalance (a second BEGIN
    before the pending one closes, an END with no BEGIN, or a BEGIN still open
    at EOF). The bundle loop's per-op ``except`` records that as an error on
    the run — never a silent skip that would ship an unbaked script while
    reporting success.
    """
    spans: List[Tuple[int, int]] = []
    open_at = -1
    for idx, line in enumerate(lines):
        if REWIRE_BEGIN in line:
            if open_at >= 0:
                raise ValueError(
                    f"{filename}: VCO-REWIRE-BEGIN on line {idx + 1} while the "
                    f"region opened on line {open_at + 1} is still unclosed"
                )
            open_at = idx
        elif REWIRE_END in line:
            if open_at < 0:
                raise ValueError(
                    f"{filename}: VCO-REWIRE-END on line {idx + 1} with no "
                    f"matching VCO-REWIRE-BEGIN"
                )
            spans.append((open_at, idx))
            open_at = -1
    if open_at >= 0:
        raise ValueError(
            f"{filename}: VCO-REWIRE-BEGIN on line {open_at + 1} is never "
            f"closed by a VCO-REWIRE-END"
        )
    return spans


def has_rewire_region(data: bytes) -> bool:
    """True when ``data`` carries at least one region sentinel.

    Detection is by CONTENT, never by a hand-kept filename list: a list is
    exactly the drift the sentinels were invented to avoid, and it would go
    stale the first time a script is renamed or a new one is written.

    Deliberately tolerant — it answers "should this op get the transform?" and
    a file with an UNBALANCED region must answer True so
    :func:`rewire_bytes` can raise and the run can report it. Balance is
    validated at transform time, not here.
    """
    return REWIRE_BEGIN.encode("utf-8") in data or REWIRE_END.encode("utf-8") in data


def render_regions(
    data: bytes,
    ctx: Mapping[str, Optional[str]],
    *,
    allowed=_mz.PATH_KEYS,
    filename: str,
) -> "Tuple[bytes, RenderResult]":
    """Render every region of ``data`` through :func:`vco_lib.materialize.render`.

    Contract:
      * bytes OUTSIDE a region are untouched — including ``{{`` sequences from
        Python f-strings and PowerShell format strings, which is why the
        substitution is both token-exact and span-scoped;
      * a file with no region is returned BYTE-IDENTICAL (same object
        contents, no decode/encode round-trip artefacts);
      * line endings are preserved verbatim (``splitlines(keepends=True)``), so
        a CRLF working copy stays CRLF — a normalising rewrite would make every
        Windows checkout look user-modified to the manifest compare;
      * unbalanced sentinels raise ``ValueError`` naming the file (a MALFORMED
        TEMPLATE, not a placeholder the machine cannot fill — the bundle loop
        records it as an error on the run);
      * a placeholder the vocabulary does not hold is LEFT IN PLACE and
        reported in the result (owner rule, ``vco_lib.materialize``).
    """
    empty = _mz.RenderResult(text="")
    if not has_rewire_region(data):
        return data, empty
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{filename}: carries a VCO-REWIRE sentinel but is not valid UTF-8 "
            f"({exc})"
        ) from exc

    lines = text.splitlines(keepends=True)
    spans = _region_line_spans(lines, filename)
    if not spans:
        return data, empty

    escape = _mz.escape_for_filename(filename)
    total = empty
    for begin_idx, end_idx in spans:
        region = "".join(lines[begin_idx:end_idx + 1])
        result = _mz.render(region, ctx, allowed=allowed, escape=escape,
                            first_line=begin_idx + 1)
        new_lines = result.text.splitlines(keepends=True)
        lines[begin_idx:end_idx + 1] = new_lines
        total = total.merged(result, text="")
    return "".join(lines).encode("utf-8"), total


def rewire_bytes(data: bytes, orchestrator_root: Path, *, filename: str,
                 project_root: Optional[Path] = None) -> bytes:
    """Substitute the path vocabulary INSIDE every region of ``data``.

    Same contract as :func:`render_regions`, bound to one install's root.
    """
    ctx = _mz.LazyContext(_mz.MaterializeContext(Path(orchestrator_root), project_root))
    out, _result = render_regions(data, ctx, filename=filename)
    return out


def rewire_transform(
    orchestrator_root: Path, *, filename: str,
    project_root: Optional[Path] = None,
    label: str = "",
    sink: "Optional[_mz.FindingsSink]" = None,
) -> Callable[[bytes], bytes]:
    """Bundle-engine ``transform=`` factory bound to one install's root.

    The root is the INSTALL's own (computed at install time from
    ``orchestrator_root``), never a build-machine path — the same clone
    installed from ``/opt/vco`` and from ``C:\\tools\\vco`` bakes each of those
    respectively (R17.4). Returns a :class:`vco_lib.materialize.Transform`
    (a plain ``bytes -> bytes`` callable to the engine).
    """
    return _mz.Transform(
        label or filename,
        _mz.RenderSpec(allowed=_mz.PATH_KEYS, regions=True),
        _mz.MaterializeContext(Path(orchestrator_root), project_root),
        filename=filename,
        sink=sink,
    )
