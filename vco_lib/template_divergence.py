# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Does a project-level file MEANINGFULLY differ from its shipping reference?
(v0.2.92 WP-15.)

ONE home for the rule behind the `template_review_pending` nudge. It lived
inline in `vco_lib/project_init.py` — a 16,000-line module — where the two
halves of the rule (normalise whitespace; ignore VCO's own injected regions)
were a whitespace helper and a comparison expression 400 lines apart. Pulling
them out gives the rule a docstring, unit tests that do not need a bundle
fixture, and keeps `project_init` holding only the wiring (CLAUDE.md's
"extract before you add ~50 lines to a file past ~5,000" rule).

THE RULE, and why it has two independent steps:

  1. **Strip VCO-owned regions** (`deferral_report.strip_vco_owned_regions`).
     A file that differs from the reference only by content VCO ITSELF
     injected is not user divergence. Three families qualify: the
     deferral-reminder block, the `>>>VCO_MANAGED>>>` marker lines, and an
     AUTO region.
  2. **Normalise whitespace** (`normalise_for_diff`). Trailing spaces and
     trailing blank lines are not divergence either.

They are SEPARATE and must stay separate. Step 2 is lossless-by-construction
and several callers depend on that; folding step 1 into it would quietly turn
the whitespace normaliser into a content-dropping one for everyone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "normalise_for_diff",
    "meaningfully_differs",
    "remove_stale_root_claude_md_sidecar",
]


def normalise_for_diff(text: str) -> list[str]:
    """Normalise a file for the "meaningfully differs" check.

    Strips trailing whitespace per line and trims trailing blank lines so a
    one-line whitespace change doesn't flag the file for review. Anything
    beyond whitespace + EOL normalisation counts as a real diff.

    WHITESPACE ONLY — see this module's docstring. Do not grow it into a
    content filter.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def meaningfully_differs(existing_text: str, reference_text: str) -> bool:
    """True when the live file differs from its reference for a USER reason.

    Both sides go through the region strip and then the whitespace
    normaliser, so the answer is invariant to VCO's own injections and to
    line-ending / trailing-whitespace differences (tri-OS: a CRLF working
    copy is not divergence).

    What this deliberately does NOT hide: an edit INSIDE the managed region.
    Only the two marker LINES are stripped there, never the body — the body
    is the rendered template and is precisely what the nudge is about.
    """
    from vco_lib.deferral_report import strip_vco_owned_regions

    return (
        normalise_for_diff(strip_vco_owned_regions(existing_text))
        != normalise_for_diff(strip_vco_owned_regions(reference_text))
    )


def remove_stale_root_claude_md_sidecar(
    folder: Path,
    ref_rel: Path,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Delete the orchestrator root's stale ``CLAUDE.md.reference.md``.

    Until v0.2.92 every ROOT install wrote the PROJECT template's render to
    ``.claude/context/templates/CLAUDE.md.reference.md`` on a root whose live
    CLAUDE.md comes from ``ORCHESTRATOR-CLAUDE.md.template``. The sidecar is a
    VCO-GENERATED artifact under a VCO-owned directory — rewritten from
    scratch by every bundle run for the entries that DO apply — not user data;
    on the root it is a diff target pointing at a document the root does not
    use.

    Removing it is what carries the fix to the ALREADY-DAMAGED population:
    the file is on disk on every existing orchestrator root right now, and no
    comparison change alone would take it away.

    The LIVE ``CLAUDE.md`` is never touched. Best-effort — a removal failure
    is not worth failing an install over, since the divergence itself is
    already suppressed by the caller's skip.

    Returns True when a file was actually removed.
    """
    sidecar = folder / ref_rel
    try:
        if not sidecar.is_file():
            return False
        sidecar.unlink()
    except OSError:
        return False
    if log is not None:
        log(
            "removed stale orchestrator-root CLAUDE.md.reference.md "
            "(rendered from the PROJECT template; the root's CLAUDE.md comes "
            "from ORCHESTRATOR-CLAUDE.md.template)"
        )
    return True
