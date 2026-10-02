# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every project ``CLAUDE.md`` template VCO has RELEASED, as reference data
(v0.2.100, WP-18 follow-up to review R18-01).

WHY
---
Before v0.2.100 the whole rendered template sat inside the VCO-managed
markers. The one-time migration to the split layout must tell an UNTOUCHED
managed body (convert silently) from a user-EDITED one (keep the text, back it
up, ask for a review). The evidence used to be only the reference sidecar the
previous UPDATE wrote — a project created before 0.2.100 and never updated has
none, so every such untouched project would have been reported as edited: a
backup plus a review row for nothing (owner: a false alarm).

So the migration also compares the body against every template VCO actually
shipped (:func:`matches_a_shipped_render`). The fast path is an exact render
with the project's own values; the general match (review R18F-03) is
STRUCTURAL — each released template compiled into a pattern whose
placeholders are wildcards — because an old render bakes the roots and the
name of its creation day: a re-cloned orchestrator, a moved project folder or
a renamed project must not turn an untouched file into an "edited" one.

An EDITED pre-split body is compared section by section against the
best-matching released template (:func:`strip_unedited_vco_sections`, review
R18F-05): a section equal to the template's (placeholders as wildcards) is
VCO's text, re-rendered in the managed region, so it is not duplicated into
the user's section; every edited or added section is kept verbatim.

THE DATA
--------
``vco_lib/legacy_templates/claude_md.toml`` holds each DISTINCT
``templates/CLAUDE.md.template`` body shipped by a release tag since the
managed-region markers exist (v0.2.33), deduplicated, with the tags that
shipped it and its sha256 (pinned by ``tests/test_v02100_legacy_claude_md.py``).
It is a TOML file so the sdist's ``vco_lib/**/*.toml`` rule ships it. Regenerate
(read-only ``git show`` of each tag) after a release that changed the template::

    python -m vco_lib.legacy_claude_md --regenerate
"""
from __future__ import annotations

import hashlib
import itertools
import re
import subprocess
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Tuple

TABLE_PATH = Path(__file__).resolve().parent / "legacy_templates" / "claude_md.toml"
#: The first release whose bundle wrapped CLAUDE.md in the managed markers.
FIRST_MARKED_RELEASE = (0, 2, 33)
_TEMPLATE_REL = "templates/CLAUDE.md.template"
_MODULE_RE = re.compile(r"\{\{#if_module_active\s+([a-z_]+)\s*\}\}")
#: A placeholder the old renderers substituted (or left literal): ``{{KEY}}``.
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
#: A section boundary: a level-2 Markdown heading outside a code fence. (The
#: title — level 1 — and its introduction stay in the leading piece, which is
#: always the user's.)
_SECTION_HEADING_RE = re.compile(r"^## \S")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

__all__ = ["TABLE_PATH", "shipped_templates", "matches_a_shipped_render",
           "strip_unedited_vco_sections"]


@lru_cache(maxsize=1)
def shipped_templates() -> Tuple[Tuple[str, str], ...]:
    """``((version, body), ...)`` oldest first. A missing or unreadable table
    is a BROKEN install and raises (never a silent "no evidence")."""
    data = tomllib.loads(TABLE_PATH.read_text(encoding="utf-8"))
    if data.get("format_version") != 1:
        raise RuntimeError(f"{TABLE_PATH}: unsupported format_version")
    return tuple((str(t["version"]), str(t["body"])) for t in data["template"])


def _norm(text: str) -> List[str]:
    from vco_lib.template_divergence import normalise_for_diff

    return normalise_for_diff(text.strip("\n"))


def _conditional_variants(template: str) -> List[Tuple[Tuple[str, ...], str]]:
    """Every conditional-block rendering of ``template``: one per combination
    of the modules it names (which modules were active THEN is unknown)."""
    from vco_lib.project_init import TemplateError, render_conditional_blocks

    modules = sorted(set(_MODULE_RE.findall(template)))
    out: List[Tuple[Tuple[str, ...], str]] = []
    for n in range(len(modules) + 1):
        for active in itertools.combinations(modules, n):
            try:
                out.append((active, render_conditional_blocks(
                    template, active_modules=set(active))))
            except TemplateError:
                continue
    return out


def _compile_structural(text: str) -> Pattern[str]:
    """``text`` (a template after its conditional blocks) as a pattern that
    matches any render of it: literal text escaped, whitespace-normalised
    line by line; each ``{{KEY}}`` a one-line wildcard, and every later use of
    the same key a backreference (one render substitutes one value per key)."""
    joined = "\n".join(_norm(text))
    parts: List[str] = []
    seen: Dict[str, str] = {}
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(joined):
        parts.append(re.escape(joined[pos:m.start()]))
        key = m.group(1)
        if key in seen:
            parts.append(f"(?P={seen[key]})")
        else:
            seen[key] = f"k_{key}"
            parts.append(f"(?P<k_{key}>[^\n]+?)")
        pos = m.end()
    parts.append(re.escape(joined[pos:]))
    return re.compile("".join(parts))


@lru_cache(maxsize=1)
def _structural_table() -> Tuple[Tuple[str, Tuple[Tuple[Pattern[str], str], ...]], ...]:
    """``((version, ((pattern, variant_text), ...)), ...)`` newest first."""
    return tuple(
        (version, tuple((_compile_structural(text), text)
                        for _active, text in _conditional_variants(template)))
        for version, template in reversed(shipped_templates())
    )


def _structural_match(body: str) -> Optional[str]:
    """The version of which ``body`` is SOME render — under any roots and any
    project name — or ``None``."""
    want = "\n".join(_norm(body))
    for version, variants in _structural_table():
        for pattern, _text in variants:
            if pattern.fullmatch(want):
                return version
    return None


def matches_a_shipped_render(
    body: str,
    *,
    folder: Path,
    orchestrator_root: Optional[Path],
    names: Iterable[Optional[str]],
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """The version of which ``body`` is an unmodified render
    (whitespace-normalised), or ``None``.

    Fast path — an exact render with THIS project's values: each shipped body
    through the same pipeline as today (``render_conditional_blocks`` +
    ``vco_lib.materialize.render``, ``escape="none"`` as every pre-v0.2.100
    project-template renderer did), under every module combination and every
    ``{{PROJECT_NAME}}`` the old renderers could have used (the registered
    launcher.db name, the name given at install, the folder basename).

    General path (review R18F-03) — a STRUCTURAL match: the old render baked
    the roots and the name of its creation day, and a re-cloned orchestrator,
    a moved folder or a renamed project changes them. Each shipped body is
    compiled with its placeholders as wildcards (:func:`_compile_structural`),
    so any render of it matches while any edit to its literal text does not.
    """
    if orchestrator_root is not None:
        exact = _exact_render_match(body, folder=folder,
                                    orchestrator_root=orchestrator_root,
                                    names=names, db_path=db_path)
        if exact is not None:
            return exact
    return _structural_match(body)


def _exact_render_match(
    body: str,
    *,
    folder: Path,
    orchestrator_root: Path,
    names: Iterable[Optional[str]],
    db_path: Optional[Path],
) -> Optional[str]:
    from vco_lib import materialize as _mz

    want = _norm(body)
    candidates = list(dict.fromkeys(
        n for n in (*names, _mz.project_display_name(folder, None, db_path=db_path),
                    Path(folder).name)
        if n))
    base = _mz.MaterializeContext(orchestrator_root, folder, db_path=db_path)
    for version, template in reversed(shipped_templates()):  # newest first
        for _active, text in _conditional_variants(template):
            for name in candidates:
                ctx = _mz.LazyContext(base, extra={"PROJECT_NAME": name})
                rendered = _mz.render(text, ctx, allowed=_mz.GLOBAL_KEYS,
                                      escape="none").text
                if _norm(rendered) == want:
                    return version
    return None


# ---------------------------------------------------------------------------
# Section-by-section comparison of an EDITED pre-split body (review R18F-05)
# ---------------------------------------------------------------------------

def split_sections(text: str) -> List[str]:
    """``text`` cut before every level-2 heading outside a code fence. The
    pieces concatenate back to ``text`` exactly (nothing is dropped); the
    first piece is whatever precedes the first heading (possibly empty)."""
    lines = text.splitlines(keepends=True)
    sections: List[str] = []
    current: List[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and _SECTION_HEADING_RE.match(line):
            sections.append("".join(current))
            current = []
        current.append(line)
    sections.append("".join(current))
    return sections


def _heading(section: str) -> str:
    first = section.lstrip("\n").split("\n", 1)[0]
    return first.rstrip() if _SECTION_HEADING_RE.match(first) else ""


def _section_patterns(variant_text: str) -> Dict[str, List[Pattern[str]]]:
    """The released sections of one template variant, by heading (the
    heading's placeholders make it a pattern too, so key by its literal
    prefix before the first placeholder)."""
    out: Dict[str, List[Pattern[str]]] = {}
    for sec in split_sections(variant_text):
        if not "\n".join(_norm(sec)).strip():
            continue
        out.setdefault(_heading_key(_heading(sec)), []).append(_compile_structural(sec))
    return out


def _heading_key(heading: str) -> str:
    m = _PLACEHOLDER_RE.search(heading)
    return heading[:m.start()] if m else heading


def _section_is_released(section: str, patterns: Dict[str, List[Pattern[str]]]) -> bool:
    want = "\n".join(_norm(section))
    heading = _heading(section)
    for key, pats in patterns.items():
        if (not heading.startswith(key)) if key else heading:
            continue
        if any(p.fullmatch(want) for p in pats):
            return True
    return False


def strip_unedited_vco_sections(
    body: str,
    *,
    keep_headings: Iterable[str] = (),
) -> Tuple[str, List[str]]:
    """``(kept_text, dropped_headings)`` for an EDITED pre-split managed body.

    The body is compared section by section with the BEST-matching released
    template variant (the one of which the most sections are unmodified
    renders). A section equal to that template's section with the same
    heading — placeholders as wildcards — is VCO's text: VCO re-renders its
    current version in the managed region, so it is dropped rather than
    duplicated into the user's section. Kept VERBATIM, in order:

    * every section the user edited, and every section they added;
    * the text before the first heading (the title and introduction);
    * every section whose heading is in ``keep_headings`` (the headings of the
      CURRENT user-section template — Project Overview, Tech Stack, Key Paths:
      they are the user's part now, edited or not).

    The caller backs the whole original up before writing, so the dropped
    text — VCO's own — also survives on disk.
    """
    keep = {h.strip() for h in keep_headings if h.strip()}
    sections = split_sections(body)
    released = [s for s in sections[1:] if _heading(s) not in keep]
    best: Optional[Dict[str, List[Pattern[str]]]] = None
    best_score = 0
    for _version, variants in _structural_table():
        for _pattern, text in variants:
            patterns = _section_patterns(text)
            score = sum(1 for s in released if _section_is_released(s, patterns))
            if score > best_score:
                best, best_score = patterns, score
    if best is None:
        return body, []
    kept: List[str] = [sections[0]]
    dropped: List[str] = []
    for sec in sections[1:]:
        heading = _heading(sec)
        if heading not in keep and _section_is_released(sec, best):
            dropped.append(heading)
        else:
            kept.append(sec)
    return "".join(kept), dropped


# ---------------------------------------------------------------------------
# Maintenance: regenerate the table from the release tags (read-only git)
# ---------------------------------------------------------------------------

def _toml_multiline(body: str) -> str:
    escaped = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + escaped + '"""'


def _version_key(tag: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def regenerate(repo: Path) -> str:
    tags = subprocess.run(["git", "tag", "-l", "v*"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.split()
    tags = sorted((t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)
                   and _version_key(t) >= FIRST_MARKED_RELEASE), key=_version_key)
    seen: dict = {}
    for tag in tags:
        proc = subprocess.run(["git", "show", f"{tag}:{_TEMPLATE_REL}"], cwd=repo,
                              capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            continue
        body = proc.stdout.decode("utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        seen.setdefault(digest, (tag, body, []))[2].append(tag)
    out = [
        "# SPDX-License-Identifier: AGPL-3.0-or-later",
        "# Every DISTINCT templates/CLAUDE.md.template shipped by a release tag since",
        "# the managed markers (v0.2.33). GENERATED by",
        "# `python -m vco_lib.legacy_claude_md --regenerate`; do not edit by hand.",
        "# Reader: vco_lib/legacy_claude_md.py.",
        "",
        "format_version = 1",
    ]
    for digest, (first, body, shipped_by) in seen.items():
        out += ["", "[[template]]", f'version = "{first}"',
                "tags = [" + ", ".join(f'"{t}"' for t in shipped_by) + "]",
                f'sha256 = "{digest}"', "body = " + _toml_multiline(body)]
    return "\n".join(out) + "\n"


if __name__ == "__main__":  # pragma: no cover — maintenance helper
    if sys.argv[1:] == ["--regenerate"]:
        text = regenerate(Path(__file__).resolve().parent.parent)
        TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TABLE_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {TABLE_PATH}")
    else:
        print("usage: python -m vco_lib.legacy_claude_md --regenerate", file=sys.stderr)
        raise SystemExit(2)
