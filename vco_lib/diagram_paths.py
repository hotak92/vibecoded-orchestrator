# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Scoped-path validation for diagram saves (Phase 1.5.1 of the
diagrams-integration plan, 2026-05-24).

Why this module exists
----------------------
The plan's metadata-by-construction strategy (decision log entry
2026-05-25 v3) hinges on the FILE PATH being the primary tag source.
That works only if every diagram save lands at:

    .claude/diagrams/<category>[/<subcategory>...]/<name>.<ext>

with kebab-case identifiers. We enforce that mechanically — no
instruction-following required. Defense in depth: BOTH the wrapper MCP
(``claude_mcp_servers/wrappers/mermaid_proxy.py``) AND the PreToolUse
hook (Phase 1.5.A — sibling) call :func:`validate_scoped_path`. The
wrapper catches MCP-routed saves; the hook catches direct ``Write``
tool calls that bypass the MCP entirely.

Why a shared module (not duplicated inline)
-------------------------------------------
If the wrapper and the hook drift, the user sees different rejection
messages from the two entry points for the SAME path — confusing and
hard to debug. One regex, one error message, one mental model.

Cross-OS contract
-----------------
We accept Unix-style ``/`` AND Windows-style ``\\`` as separators
during validation; downstream code that consumes the resolved path
should normalise via :class:`pathlib.Path` before use. The path
regex is anchored at ``.claude/diagrams/`` regardless of separator
because both the wrapper and the hook see paths-as-strings (the MCP
JSON-RPC payload is JSON; the hook reads ``$CLAUDE_TOOL_INPUT``)
that can carry either separator depending on which side of the
Windows boundary the path originated on.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal


# ─── Public surface ───────────────────────────────────────────────────────

#: Allowed `kind` values for :func:`validate_scoped_path`. ``None`` accepts
#: either extension; the others enforce ext == kind.
DiagramKind = Literal["mermaid", "excalidraw"]


# The canonical scoped path layout. We split the validation into two regexes
# (suffix + parts) rather than one mega-pattern so the error message can
# point at the *specific* failure (wrong root, bad category casing, bad
# extension) instead of dumping a wall of regex at the user.
#
# Anchored at ``.claude/diagrams/`` — accepts an optional path prefix so
# the wrapper can pass either an absolute path (when the MCP resolves the
# user's CWD) or a project-relative path (the typical Claude usage).
_KEBAB_SEGMENT = r"[a-z0-9][a-z0-9-]*"

_SCOPED_PATH_RE = re.compile(
    r"^(?P<prefix>.*?[\\/])?\.claude[\\/]diagrams[\\/]"
    r"(?P<category>" + _KEBAB_SEGMENT + r"(?:[\\/]" + _KEBAB_SEGMENT + r")*)"
    r"[\\/](?P<name>" + _KEBAB_SEGMENT + r")"
    r"\.(?P<ext>mmd|excalidraw)$"
)

# Separate pattern for the traversal check: any segment that contains
# ``..`` (relative parent reference) is rejected unconditionally. We do
# this BEFORE the structural regex so traversal attempts get their own
# message rather than the generic "didn't match" error — easier for the
# user to understand and harder for an attacker to fingerprint.
_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")


def validate_scoped_path(
    path: str,
    kind: DiagramKind | None = None,
) -> str | None:
    """Validate a diagram save path against the scoped-path rule.

    Args:
        path: The save path as a string. May be absolute, project-
            relative, Unix-style or Windows-style; the validator
            normalises separators in the regex. Passing a
            :class:`~pathlib.PurePath` is supported via ``str(path)``
            at call sites.
        kind: Optional discriminator. When ``"mermaid"`` the extension
            must be ``.mmd``; when ``"excalidraw"`` it must be
            ``.excalidraw``; when ``None`` either extension is accepted.

    Returns:
        ``None`` if the path is valid; a human-readable error message
        (including a corrective example) otherwise. The error is
        intended to be surfaced verbatim to the caller — Claude reads
        it, the user reads it, both should be able to fix the call
        without reading docs.

    Notes:
        We accept multi-level categories
        (``.claude/diagrams/gui/auth/login-form.mmd``) — every segment
        between ``diagrams/`` and the filename is added to the tag set
        downstream. Single-level is also allowed
        (``.claude/diagrams/gui/login-form.mmd``). FLAT files at the
        root of ``.claude/diagrams/`` are REJECTED.
    """
    if not isinstance(path, str):
        # Coerce :class:`Path`-like objects without raising.
        path = str(path)

    if not path:
        return _format_error(
            path,
            "empty",
            kind=kind,
            hint="provide a path under .claude/diagrams/<category>/",
        )

    # Traversal check FIRST — gives a clearer message for ``../``-laden
    # paths than the structural regex would.
    if _TRAVERSAL_RE.search(path):
        return _format_error(
            path,
            "path traversal segment",
            kind=kind,
            hint="remove `..` segments; saves must stay under .claude/diagrams/",
        )

    m = _SCOPED_PATH_RE.match(path)
    if m is None:
        # Best-effort sub-diagnosis: which part of the pattern likely failed?
        return _diagnose_failure(path, kind)

    # Structural match succeeded — last check is the kind/ext agreement.
    if kind is not None:
        ext = m.group("ext")
        expected_ext = "mmd" if kind == "mermaid" else "excalidraw"
        if ext != expected_ext:
            return _format_error(
                path,
                f"extension `.{ext}` does not match diagram kind `{kind}` (expected `.{expected_ext}`)",
                kind=kind,
                hint=f"save to <name>.{expected_ext} instead",
            )

    return None


# ─── Diagnostics + formatting ─────────────────────────────────────────────


def _diagnose_failure(path: str, kind: DiagramKind | None) -> str:
    """Pick the most useful sub-message when the structural regex fails.

    We branch on common failure shapes (flat root, uppercase, underscore,
    bad extension) so the rejection message points at the *specific*
    fix rather than dumping the regex at the user. This is the cost
    of defense-in-depth: the same diagnostic runs in two code paths
    (wrapper MCP + PreToolUse hook), so consistency matters.
    """
    # Strip optional leading absolute prefix so we can reason about the
    # logical path inside the project. Use PurePosixPath to canonicalise
    # ``\\`` → ``/`` for inspection (NOT for output — we echo the
    # original).
    normalised = path.replace("\\", "/")
    if ".claude/diagrams/" not in normalised:
        return _format_error(
            path,
            "not under .claude/diagrams/",
            kind=kind,
            hint="diagrams must be saved under .claude/diagrams/<category>/<name>.<ext>",
        )

    # Tail = everything after the diagrams/ anchor.
    tail = normalised.split(".claude/diagrams/", 1)[1]
    if "/" not in tail:
        # Flat-folder save: ``.claude/diagrams/foo.mmd``. The plan
        # specifically calls this out as a reject case (§1.5.1).
        return _format_error(
            path,
            "flat-folder save (no category subdirectory)",
            kind=kind,
            hint="add a category directory, e.g. .claude/diagrams/gui/<name>.mmd",
        )

    # Now look at the tail parts to give a targeted message.
    parts = tail.split("/")
    name_with_ext = parts[-1]
    category_segments = parts[:-1]

    # Check extension first — if it's wrong even before considering
    # case, that's the user's clearest signal.
    if "." not in name_with_ext:
        return _format_error(
            path,
            "filename has no extension",
            kind=kind,
            hint="add `.mmd` (Mermaid) or `.excalidraw` (Excalidraw)",
        )
    stem, dot, ext = name_with_ext.rpartition(".")
    if ext not in ("mmd", "excalidraw"):
        return _format_error(
            path,
            f"unknown extension `.{ext}` (allowed: .mmd, .excalidraw)",
            kind=kind,
            hint="use .mmd for Mermaid or .excalidraw for Excalidraw",
        )

    # Casing / kebab checks. We diagnose category segments + the stem
    # separately so the user knows which one is wrong.
    for seg in category_segments:
        if not _is_kebab(seg):
            return _format_error(
                path,
                f"category segment `{seg}` is not kebab-case (lowercase + digits + `-` only)",
                kind=kind,
                hint="rename, e.g. `Auth Flow` → `auth-flow`",
            )
    if not _is_kebab(stem):
        return _format_error(
            path,
            f"filename `{stem}` is not kebab-case (lowercase + digits + `-` only)",
            kind=kind,
            hint="rename, e.g. `Login_Form` → `login-form`",
        )

    # If we reach here the structural regex still rejected — fall back
    # to a generic message rather than claiming we know the exact cause.
    return _format_error(
        path,
        "does not match the scoped-path pattern",
        kind=kind,
        hint="expected: .claude/diagrams/<category>/<name>.<ext> (lowercase kebab-case)",
    )


def _is_kebab(segment: str) -> bool:
    """Stricter than the regex: rejects empty strings, leading/trailing
    hyphens, and consecutive hyphens. The structural regex allows
    ``a--b`` (two hyphens) because ``[a-z0-9-]*`` is greedy; we tighten
    here so diagnostics surface the cleaner form."""
    if not segment:
        return False
    if segment[0] == "-" or segment[-1] == "-":
        return False
    if "--" in segment:
        # Not strictly required by the regex but keeps tags clean.
        # _SCOPED_PATH_RE still matches `a--b`, but humanise the form
        # for the rejection message.
        return False
    return all(c.islower() or c.isdigit() or c == "-" for c in segment)


def _format_error(
    path: str,
    reason: str,
    *,
    kind: DiagramKind | None,
    hint: str,
) -> str:
    """Build the canonical rejection message.

    Format is fixed so the wrapper MCP and the PreToolUse hook produce
    BYTE-IDENTICAL error strings — easier to grep for in support, and
    one shape for the test fixture.
    """
    kind_str = f" ({kind})" if kind else ""
    return (
        f"diagram save rejected: {reason}{kind_str}.\n"
        f"  given path: {path}\n"
        f"  fix: {hint}\n"
        f"  example: .claude/diagrams/gui/auth/login-form.mmd"
    )


# ─── Tag extraction (used by the indexer; co-located so the path rule
# and the tag derivation can't drift) ─────────────────────────────────────


def extract_category_tags(path: str) -> tuple[str, ...]:
    """Return the category segments of a valid scoped path as a tuple.

    Used by ``vco_lib/diagram_indexer.py`` (sibling Phase 1.5.A) to
    derive ``path_tags`` from ``category_path``. Returns an empty
    tuple when the path doesn't match the scoped layout — callers
    should validate first via :func:`validate_scoped_path`.

    Co-located here (not in the indexer) because the tag derivation
    is a direct consequence of the path rule; keeping them in the
    same module guarantees they can't drift if the path rule is ever
    relaxed/tightened.
    """
    m = _SCOPED_PATH_RE.match(path)
    if m is None:
        return ()
    category = m.group("category")
    # Normalise mixed separators that the regex tolerated to a single
    # forward-slash split.
    return tuple(s for s in PurePosixPath(category.replace("\\", "/")).parts if s)


# ─── CLI entry point (Phase 1.5.A integration) ────────────────────────────
#
# The PreToolUse hook templates/hooks/pre-diagram-path-validation.{sh,ps1}
# invokes `python -m vco_lib.diagram_paths validate <path> [--kind <k>]`.
# Phase 1.2's canonical API returns `str | None`; the CLI wraps it to emit
# the corrective message on stderr + exit 2 (PreToolUse block-the-write
# convention) so hook authors don't have to re-derive the format.


def _cli(argv: list[str] | None = None) -> int:
    """Validate a single diagram path. Exit 0 = OK, exit 2 = violation."""
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.diagram_paths",
        description="Validate diagram paths against the scoped-path rule.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser(
        "validate",
        help="Validate a single path; exit 0 ok, exit 2 violation.",
    )
    p_val.add_argument("file_path", type=Path)
    p_val.add_argument(
        "--kind",
        default="auto",
        choices=["auto", "mermaid", "excalidraw"],
        help="Force a diagram_type expectation (default: auto from suffix).",
    )

    args = parser.parse_args(argv)

    if args.cmd != "validate":  # pragma: no cover — argparse handles this
        parser.error("unknown command")

    kind: DiagramKind | None = None if args.kind == "auto" else args.kind  # type: ignore[assignment]
    err = validate_scoped_path(str(args.file_path), kind=kind)
    if err is not None:
        # Hooks expect the corrective message on stderr — piped straight to
        # Claude. Exit 2 = block-the-write per PreToolUse hook spec.
        print(err, file=sys.stderr)
        return 2

    # On success, print the parsed category tags + filename for any caller
    # that wants them (the indexer uses these).
    tags = extract_category_tags(str(args.file_path))
    name = Path(args.file_path).stem
    suffix = Path(args.file_path).suffix.lstrip(".")
    print(f"OK type={suffix} category={'/'.join(tags)} name={name}")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    raise SystemExit(_cli())
