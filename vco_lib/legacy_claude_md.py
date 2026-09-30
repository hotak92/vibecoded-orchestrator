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

So the migration also compares the body against renders of every template VCO
actually shipped (:func:`matches_a_shipped_render`), with the project's own
values and every identity the old renderers could have used.

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
from typing import Iterable, List, Optional, Tuple

TABLE_PATH = Path(__file__).resolve().parent / "legacy_templates" / "claude_md.toml"
#: The first release whose bundle wrapped CLAUDE.md in the managed markers.
FIRST_MARKED_RELEASE = (0, 2, 33)
_TEMPLATE_REL = "templates/CLAUDE.md.template"
_MODULE_RE = re.compile(r"\{\{#if_module_active\s+([a-z_]+)\s*\}\}")

__all__ = ["TABLE_PATH", "shipped_templates", "matches_a_shipped_render"]


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


def matches_a_shipped_render(
    body: str,
    *,
    folder: Path,
    orchestrator_root: Path,
    names: Iterable[Optional[str]],
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """The version whose render equals ``body`` (whitespace-normalised), or
    ``None``.

    Each shipped body is rendered through the SAME pipeline as today
    (``render_conditional_blocks`` + ``vco_lib.materialize.render``) with this
    project's roots, under every combination of the modules its conditional
    sections name (the modules active THEN are unknown), and under every
    ``{{PROJECT_NAME}}`` the old renderers could have used: the registered
    launcher.db name, the name given at install, the folder basename. Values
    are substituted unescaped (``escape="none"``), which is what every
    pre-v0.2.100 project-template renderer did.
    """
    from vco_lib import materialize as _mz
    from vco_lib.project_init import TemplateError, render_conditional_blocks

    want = _norm(body)
    candidates = list(dict.fromkeys(
        n for n in (*names, _mz.project_display_name(folder, None, db_path=db_path),
                    Path(folder).name)
        if n))
    base = _mz.MaterializeContext(orchestrator_root, folder, db_path=db_path)
    for version, template in reversed(shipped_templates()):  # newest first
        modules = sorted(set(_MODULE_RE.findall(template)))
        for n in range(len(modules) + 1):
            for active in itertools.combinations(modules, n):
                try:
                    text = render_conditional_blocks(template, active_modules=set(active))
                except TemplateError:
                    continue
                for name in candidates:
                    ctx = _mz.LazyContext(base, extra={"PROJECT_NAME": name})
                    rendered = _mz.render(text, ctx, allowed=_mz.GLOBAL_KEYS,
                                          escape="none").text
                    if _norm(rendered) == want:
                        return version
    return None


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
