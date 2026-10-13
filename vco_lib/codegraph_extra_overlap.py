# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Can a folder be a project's extra code folder? The ONE overlap rule (v0.2.102 W6-CGD).

An extra code folder feeds another checkout into the project's own code-graph
collections, with its rows stamped by the folder's own root. A folder that
overlaps the project's own folder must never become one:

* ``same_as_project`` — it is the project's folder; nothing to add.
* ``inside_project`` — the project already indexes it, so adding it would
  index every file a second time under a second identity (``P`` + ``sub/x.py``
  and ``P/sub`` + ``x.py``) — a no-op at best, duplicates in practice.
* ``inside_project_ignored`` — it lies under a directory the code graph never
  indexes (``vendor/``, ``build/``, ``.wt/``, ``.claude/`` …), so the files are
  NOT indexed, but folders inside the project still cannot be added: the
  message says why instead of claiming they are already covered.
* ``contains_project`` — the project lies inside it; every project edit would
  be routed and indexed as the extra's.

Callers (rule A of the cross-language rule: one Python implementation, other
languages call it through ``python -m``):

* the launcher's Add command (``add_project_codegraph_extra_path``) through
  ``vco_lib_bridge::codegraph_extra_overlap`` →
  ``python -m vco_lib.codegraph_extra_overlap --extra … --project-folder … --json``;
* the analyzer: a nested ``--extra-path`` root of a combined walk is refused
  (not walked) and ``--as-extra-path`` with ``--primary-root`` exits 2;
* the automatic extra-folder refresh and the queued Stop drain
  (``vco_lib.codegraph_extras_refresh``) skip a nested row with a log line.

Both sides are compared after ``realpath`` (symlinks, ``..``) and with an
ASCII case fold, the same case rule registration has always used, so a
folder that only differs in case is refused rather than doubled. Paths are
compared component-wise: ``/a/foo`` does not contain ``/a/foobar``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from vco_lib.codegraph_row_classify import CODEGRAPH_IGNORE_PARTS

__all__ = [
    "CONTAINS_PROJECT",
    "INSIDE_PROJECT",
    "INSIDE_PROJECT_IGNORED",
    "OK",
    "SAME_AS_PROJECT",
    "OverlapVerdict",
    "classify",
    "main",
]

OK = "ok"
SAME_AS_PROJECT = "same_as_project"
INSIDE_PROJECT = "inside_project"
INSIDE_PROJECT_IGNORED = "inside_project_ignored"
CONTAINS_PROJECT = "contains_project"


@dataclass(frozen=True)
class OverlapVerdict:
    """The decision plus the one user-facing sentence the GUI and logs show."""

    verdict: str
    message: str = ""
    ignored_dir: str = ""

    @property
    def refused(self) -> bool:
        return self.verdict != OK

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "refused": self.refused,
                "message": self.message, "ignored_dir": self.ignored_dir}


def _normalise(path: str) -> str:
    """Forward slashes, realpath when the path is absolute, no trailing slash."""
    s = str(path).strip().replace("\\", "/")
    if os.path.isabs(s):
        try:
            s = os.path.realpath(s).replace("\\", "/")
        except (OSError, ValueError):
            pass
    while len(s) > 1 and s.endswith("/") and not s.endswith(":/"):
        s = s[:-1]
    return s


def _parts(norm: str) -> list:
    return [p for p in norm.split("/") if p]


def _fold(parts: list) -> list:
    return [p.lower() for p in parts]


def classify(extra: str, project_folder: str) -> OverlapVerdict:
    """Classify ``extra`` against the project's own folder (see the module doc)."""
    e_norm, p_norm = _normalise(extra), _normalise(project_folder)
    e_parts, p_parts = _parts(e_norm), _parts(p_norm)
    e_fold, p_fold = _fold(e_parts), _fold(p_parts)
    lead_e, lead_p = e_norm.startswith("/"), p_norm.startswith("/")
    if not e_parts or not p_parts or lead_e != lead_p:
        return OverlapVerdict(OK)
    if e_fold == p_fold:
        return OverlapVerdict(
            SAME_AS_PROJECT,
            f"'{extra}' is this project's own folder: it is already indexed by the "
            "project's code graph. Nothing to add.",
        )
    if e_fold[: len(p_fold)] == p_fold:
        rel = e_parts[len(p_parts):]
        ignored = next((p for p in rel if p in CODEGRAPH_IGNORE_PARTS or p == ".claude"), "")
        if ignored:
            why = ("its .claude/ tooling folder, which the project's own .claude setting "
                   "covers" if ignored == ".claude"
                   else f"'{ignored}/', which the code graph never indexes")
            return OverlapVerdict(
                INSIDE_PROJECT_IGNORED,
                f"'{extra}' is inside this project's folder ('{project_folder}'), under "
                f"{why}. Folders inside the project can't be added as code folders.",
                ignored_dir=ignored,
            )
        return OverlapVerdict(
            INSIDE_PROJECT,
            f"'{extra}' is inside this project's folder ('{project_folder}') and is "
            "already indexed as part of it. Nothing to add.",
        )
    if p_fold[: len(e_fold)] == e_fold:
        return OverlapVerdict(
            CONTAINS_PROJECT,
            f"'{extra}' contains this project's own folder ('{project_folder}'): every "
            "project edit would be indexed as the code folder's instead of the "
            "project's. Add a folder beside the project instead.",
        )
    return OverlapVerdict(OK)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI. ``--json``: one ``{"ok": true, "verdict": …, "refused": …, "message": …}``
    object, exit 0 (the verdict is in the reply). Plain: the refusal sentence and
    exit 3 when refused, nothing and exit 0 when allowed."""
    ap = argparse.ArgumentParser(prog="python -m vco_lib.codegraph_extra_overlap")
    ap.add_argument("--extra", required=True, help="the folder to register as a code folder")
    ap.add_argument("--project-folder", required=True, help="the project's own folder")
    ap.add_argument("--json", action="store_true", help="print the verdict as one JSON object")
    args = ap.parse_args(argv)
    v = classify(args.extra, args.project_folder)
    if args.json:
        print(json.dumps({"ok": True, **v.as_dict()}))
        return 0
    if v.refused:
        print(v.message)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
