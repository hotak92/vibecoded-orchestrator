# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Credential scan of what ONE Edit/Write introduced (post-tool-security).

The ONE implementation behind ``post-tool-security.sh`` and ``.ps1`` (A>B>C
tier A: both siblings call this script, neither re-implements the scan).
Owner ruling 2026-10-12: the hook used to grep the WHOLE edited file on every
Edit/Write, so a test placeholder already sitting in a file (the ``ghp_…``
fixtures in ``secrets.rs``) re-raised a desktop alert on every unrelated
edit. Now:

* **Scope = what the tool call introduced.** ``Edit`` -> the full lines the
  ``new_string`` occupies in the file after the edit (every occurrence);
  ``Write`` -> the whole file (the call wrote all of it). A credential that
  was already in the file and is not on a line the edit touched is not
  reported again. A pre-existing credential on a line the edit DID touch is
  reported: the line is part of the change. A payload that carries none of
  ``new_string`` / ``content`` (an unknown tool shape) is scanned
  whole -- recall wins when the scope cannot be known. An ``Edit`` whose
  ``new_string`` cannot be located in the file any more is scanned on the
  ``new_string`` text itself, reported without a file line number.
* **Explicit opt-out only.** A line carrying the marker
  ``vco-secret-scan: test-placeholder`` (the SSOT is
  ``vco_lib/credential_shapes.py::TEST_PLACEHOLDER_MARKER``; the caller passes
  the value from its own vocabulary mirror) is a declared test placeholder.
  Nothing is inferred from how a value looks.
* **Never the value.** Findings are ``(line, shape id, label)`` and the
  project-relative path; no part of a matched value is ever printed.

The vocabulary arrives on argv from the calling hook's ``_lib/credshapes``
mirror (``content_scan`` context), so this script needs no ``vco_lib`` on the
path -- it ships into ``<project>/.claude/scripts/`` and runs on stdlib only.

Usage::

    credscan_introduced.py --root ROOT --marker MARKER [--format json|nul]
        -- ID LABEL PATTERN [ID LABEL PATTERN ...]   < hook-payload.json
    credscan_introduced.py --root ROOT --shapes-env VAR   < hook-payload.json

Output (``json``): ``{"path", "scope", "findings": [{"line", "id",
"label"}], "summary", "patterns"}``. ``nul`` emits ``count``, ``path``,
``summary``, ``patterns`` and the findings JSON, each NUL-terminated, for the
bash sibling's single read loop. Exit status is 0 whenever a verdict was
produced; 2 on a usage error (the caller reports the scanner unavailable).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

#: The shape whose match is gated by a plausible key body (below).
PEM_SHAPE_ID = "pem_private_key"

#: v0.2.82: a PEM marker alerts only with a PLAUSIBLE body -- >= 120 base64
#: chars before ``-----END`` within 8 KiB. A real EC SEC1 P-256 body is ~164
#: chars; the secrets.rs write-guard stub is 13. Control flow over a
#: multi-line window, not vocabulary, so it lives here, not in the SSOT.
PEM_BODY_FLOOR = 120
PEM_WINDOW = 8192

Shape = Tuple[str, str, "re.Pattern[str]"]


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(starts: List[int], offset: int) -> int:
    """1-based line number of character ``offset``."""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _span_lines(text: str, starts: List[int],
                needle: str) -> Optional[List[int]]:
    """The 1-based lines every occurrence of ``needle`` occupies in
    ``text``, or None when it is not found. Every occurrence, also without
    ``replace_all``: an Edit's ``old_string`` was unique before the edit, but
    its ``new_string`` may also occur elsewhere afterwards, and reporting an
    identical line twice is safer than missing the edited one."""
    pos = text.find(needle)
    if pos < 0:
        return None
    lines: set = set()
    while pos >= 0:
        first = _line_of(starts, pos)
        last = _line_of(starts, pos + max(len(needle) - 1, 0))
        lines.update(range(first, last + 1))
        pos = text.find(needle, pos + max(len(needle), 1))
    return sorted(lines)


def _pem_plausible(text: str, end: int) -> bool:
    window = text[end:end + PEM_WINDOW]
    stop = window.find("-----END")
    body = window if stop < 0 else window[:stop]
    return len(re.sub(r"[^A-Za-z0-9+/=]", "", body)) >= PEM_BODY_FLOOR


def scan_lines(text: str, line_numbers: Sequence[int], shapes: Sequence[Shape],
               marker: str) -> List[Dict]:
    """Findings on the given 1-based lines of ``text``; a line carrying
    ``marker`` is skipped. The PEM body is read from ``text`` past the
    marker line (it spans the following lines)."""
    lines = text.split("\n")
    starts = _line_starts(text)
    found: List[Dict] = []
    for n in line_numbers:
        if n < 1 or n > len(lines):
            continue
        line = lines[n - 1]
        if marker and marker in line:
            continue
        for shape_id, label, rx in shapes:
            m = rx.search(line)
            if not m:
                continue
            if shape_id == PEM_SHAPE_ID and not _pem_plausible(
                    text, starts[n - 1] + m.end()):
                continue
            found.append({"line": n, "id": shape_id, "label": label})
    return found


def _relpath(path: str, root: str) -> str:
    try:
        full = os.path.realpath(path)
        base = os.path.realpath(root) if root else ""
        if base and (full == base or full.startswith(base.rstrip(os.sep) + os.sep)):
            return os.path.relpath(full, base).replace(os.sep, "/")
    except (OSError, ValueError):
        pass
    return path


def introduced_scan(payload: Dict, root: str, shapes: Sequence[Shape],
                    marker: str) -> Dict:
    """The verdict for one PostToolUse payload (see the module docstring)."""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}
    path = str(ti.get("file_path") or "")
    verdict: Dict = {"path": _relpath(path, root) if path else "",
                     "scope": "none", "findings": []}
    if not path or not os.path.isfile(path):
        return verdict
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read().replace("\r\n", "\n")
    except OSError:
        return verdict
    starts = _line_starts(text)
    total = len(text.split("\n"))

    # Claude Code's file-writing tools are Edit, Write and NotebookEdit; the
    # former MultiEdit tool no longer exists (the 2.1.x CLI keeps its name
    # only as a legacy permission-rule alias), so there is no ``edits`` shape
    # to handle here.
    if "new_string" in ti:
        verdict["scope"] = "edit"
        new = str(ti.get("new_string") or "").replace("\r\n", "\n")
        found: List[Dict] = []
        if new:  # a deletion (empty new_string) introduces nothing
            hit = _span_lines(text, starts, new)
            if hit is not None:
                found = scan_lines(text, hit, shapes, marker)
            else:
                # Not locatable in the file any more: scan the edit text
                # itself, reported without a file line number.
                for f in scan_lines(new, range(1, len(new.split("\n")) + 1),
                                    shapes, marker):
                    f["line"] = None
                    found.append(f)
    else:
        verdict["scope"] = "write" if "content" in ti else "file"
        found = scan_lines(text, range(1, total + 1), shapes, marker)
    verdict["findings"] = found
    return verdict


def summarize(findings: Sequence[Dict]) -> Tuple[str, str]:
    """``(summary, patterns)``: ``"GitHub token (line 12); Generic secret
    (lines 3, 7)"`` and the space-joined distinct labels (the ledger's
    historical ``patterns`` field), both in first-seen label order."""
    order: List[str] = []
    lines: Dict[str, List[str]] = {}
    for f in findings:
        label = f["label"]
        if label not in lines:
            order.append(label)
            lines[label] = []
        where = "?" if f.get("line") is None else str(f["line"])
        if where not in lines[label]:
            lines[label].append(where)
    parts = []
    for label in order:
        ns = lines[label]
        word = "line" if len(ns) == 1 else "lines"
        parts.append(f"{label} ({word} {', '.join(ns)})")
    return "; ".join(parts), " ".join(order)


def _shapes(triples: Sequence[str]) -> List[Shape]:
    if len(triples) % 3:
        raise ValueError("shapes must be ID LABEL PATTERN triples")
    out: List[Shape] = []
    for i in range(0, len(triples), 3):
        out.append((triples[i], triples[i + 1], re.compile(triples[i + 2])))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="credscan_introduced.py")
    ap.add_argument("--root", default="")
    ap.add_argument("--marker", default="")
    ap.add_argument("--shapes-env", default="",
                    help="read {marker, shapes: [[id, label, re], ...]} as "
                         "JSON from this environment variable instead of "
                         "argv (the .ps1 sibling: Windows PowerShell 5.1 "
                         "mangles a native argument holding a double quote, "
                         "and the generic-secret patterns hold one)")
    ap.add_argument("--format", choices=("json", "nul"), default="json")
    ap.add_argument("shapes", nargs="*")
    try:
        args = ap.parse_args(argv)
        marker = args.marker
        triples = list(args.shapes)
        if args.shapes_env:
            spec = json.loads(os.environ.get(args.shapes_env) or "{}")
            marker = str(spec.get("marker") or "")
            triples = [str(x) for row in spec.get("shapes") or [] for x in row]
        shapes = _shapes(triples)
        if not shapes or not marker:
            raise ValueError("empty vocabulary or marker")
    except (SystemExit, ValueError, re.error) as exc:
        print(f"credscan_introduced: unusable arguments ({exc})",
              file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace")
                             or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    verdict = introduced_scan(payload, args.root, shapes, marker)
    verdict["summary"], verdict["patterns"] = summarize(verdict["findings"])
    if args.format == "json":
        sys.stdout.write(json.dumps(verdict))
    else:
        fields = [str(len(verdict["findings"])), verdict["path"],
                  verdict["summary"], verdict["patterns"],
                  json.dumps(verdict["findings"])]
        sys.stdout.write("".join(f + "\0" for f in fields))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
