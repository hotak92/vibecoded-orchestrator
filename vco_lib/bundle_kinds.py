# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundle's KIND vocabulary — one home (v0.2.101 catalogue plan).

A bundle file's *kind* is derived from its ``.claude/<bucket>/`` destination
prefix. The vocabulary drives three mechanisms that must agree:

* ``--skip-kind`` (CLI + ``install_project_bundle(skip_kinds=…)``) —
  :data:`BUNDLE_SKIP_KINDS` / :data:`FILE_KINDS`;
* per-op classification (:func:`bundle_op_kind`, :func:`classify_bundle_op_kind`);
* the enabled/disabled path math the launcher GUI's toggles and the engine's
  FS-disable contract share (:func:`agent_or_skill_already_present`,
  :func:`disabled_counterpart` — the Python sibling of the Rust
  ``vct-launcher-core::db::project_state::resolve_kind_paths``).

Extracted from ``vco_lib/project_init.py`` in v0.2.101 (the module is
line-ratchet-capped and the packs engine — ``vco_lib/packs.py`` — is the
second consumer of the path math). ``project_init`` re-exports every name
under its historical private alias (``_bundle_op_kind`` etc.), so its
call-sites and tests are untouched; THIS module is the one home.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

from vco_lib.paths import to_posix_rel

__all__ = [
    "BUNDLE_SKIP_KINDS",
    "FILE_KINDS",
    "agent_or_skill_already_present",
    "bundle_op_kind",
    "classify_bundle_op_kind",
    "disabled_counterpart",
]

# v0.2.85 PLAN-v0285 D6: the set of `--skip-kind` values accepted by the CLI
# and the `skip_kinds` param. The FILE kinds are enumeration KINDS (a file's
# kind is derived from its `.claude/<bucket>/` dest_rel prefix by
# `bundle_op_kind`); `settings` is NOT an op but the separate settings-merge
# step, so it is handled specially (skipping it leaves `settings_action == ""`).
# v0.2.101 (catalogue plan §5): `specializations` joins the file kinds — the
# `.claude/specializations/` docs tree ships as a plain recursive copy.
BUNDLE_SKIP_KINDS: frozenset[str] = frozenset({
    "agents", "skills", "hooks", "scripts", "settings", "specializations",
})

#: The subset of :data:`BUNDLE_SKIP_KINDS` that are enumerated FILE kinds
#: (``settings`` is the smart-merge step, never an op). One home for the
#: literal the engine's op-filter and orphan carry-forward both use.
FILE_KINDS: frozenset[str] = BUNDLE_SKIP_KINDS - {"settings"}


def agent_or_skill_already_present(
    project_dir: Path, name: str, kind: str,
) -> bool:
    """Return True if the agent/skill is already installed at either the
    enabled or disabled location, so install-bundle should skip copying.

    Mirrors `resolve_kind_paths()` in the Rust launcher-core
    (`vct-launcher-core::db::project_state`). `kind` is 'agent' or
    'skill'. Used by `_file_action` to honour the FS-disable contract:
    a user-disabled file (moved to `.claude/{agents,skills}.disabled/`
    by the launcher GUI) must NOT be resurrected by a bundle update.

    Path math is intentionally pure (no I/O beyond `.exists()`) so the
    helper is cheap to call once per bundle op.
    """
    claude = project_dir / ".claude"
    if kind == "agent":
        # Agents are individual .md files.
        leaf = f"{name}.md"
        return (
            (claude / "agents" / leaf).exists()
            or (claude / "agents.disabled" / leaf).exists()
        )
    if kind == "skill":
        # Skills are whole directories — the name IS the leaf.
        return (
            (claude / "skills" / name).exists()
            or (claude / "skills.disabled" / name).exists()
        )
    return False


def classify_bundle_op_kind(dest_rel: str) -> Optional[tuple[str, str]]:
    """If `dest_rel` is an agent .md or skill file/dir, return the
    (kind, name) tuple suitable for `agent_or_skill_already_present`.

    Returns None for hooks, scripts, settings, infra — anything not
    subject to the FS-disable rule.

    Cross-OS: `_BundleFileOp.dest_rel` is built via `str(Path(...))`
    whose separator depends on the host OS (`/` on POSIX, `\\` on
    Windows). Normalise both flavours via `pathlib.PurePosixPath`
    after a backslash-to-slash swap so the classifier works uniformly
    regardless of where the bundle was enumerated.
    """
    # PurePosixPath alone treats `\\` as a literal character, so a
    # Windows-shaped dest_rel ('.claude\\agents\\foo.md') would not split
    # into the expected parts. Normalise to `/` first.
    normalised = dest_rel.replace("\\", "/")
    parts = PurePosixPath(normalised).parts
    # All FS-disable-relevant ops live under .claude/<bucket>/...
    if len(parts) < 3 or parts[0] != ".claude":
        return None
    bucket = parts[1]
    if bucket == "agents" and len(parts) == 3 and parts[2].endswith(".md"):
        # `.claude/agents/<name>.md` — name is the stem (sans `.md`).
        return ("agent", parts[2][:-3])
    if bucket == "skills" and len(parts) >= 3:
        # Skills are recursive; every shipped file lives under
        # `.claude/skills/<name>/...`. Skip the whole skill when its
        # directory has a `.disabled/` counterpart.
        return ("skill", parts[2])
    return None


def bundle_op_kind(dest_rel: str) -> Optional[str]:
    """Map a bundle `dest_rel` to its enumeration KIND for `--skip-kind`
    (v0.2.85 PLAN-v0285 D6).

    Returns one of `"hooks"`, `"scripts"`, `"agents"`, `"skills"`,
    `"specializations"` (v0.2.101 catalogue plan §5), or None (for anything
    not covered by a skip-kind — curated/per-project knowledge nodes,
    `.vscode/tasks.json`, etc.).

    `hooks` INCLUDES `.claude/hooks/_lib/...` (the always-overwrite lib files
    ship as part of the hooks kind). `settings` is deliberately absent: the
    settings.json smart-merge is NOT an enumerated op, so it is skipped at the
    merge call-site, not here.

    Cross-OS: `dest_rel` carries the host separator (`\\` on Windows). Normalize
    via the shared `to_posix_rel` helper before the prefix test — the same
    discipline the knowledge-retirement branch and `classify_bundle_op_kind`
    already use.
    """
    normalized = to_posix_rel(dest_rel)
    if normalized.startswith(".claude/hooks/"):
        return "hooks"
    if normalized.startswith(".claude/scripts/"):
        return "scripts"
    if normalized.startswith(".claude/agents/"):
        return "agents"
    if normalized.startswith(".claude/skills/"):
        return "skills"
    if normalized.startswith(".claude/specializations/"):
        return "specializations"
    return None


def disabled_counterpart(dest_rel: str) -> Optional[str]:
    """The disabled-side path of an agent/skill bundle dest_rel, else None.

    ``.claude/agents/<name>.md`` → ``.claude/agents.disabled/<name>.md``;
    ``.claude/skills/<name>/…`` → ``.claude/skills.disabled/<name>/…`` (the
    tail is preserved, so a skill's companion files map one-to-one). The
    Python sibling of the Rust ``resolve_kind_paths`` bucket math — the
    launcher GUI's enable/disable toggle MOVES files between exactly these
    two locations, so any engine pass that reasons about "where else could
    this shipped file be" must consult this mapping. Separator-normalised
    (Windows manifest keys carry ``\\``); the result is always POSIX-shaped.
    """
    normalised = to_posix_rel(dest_rel)
    parts = PurePosixPath(normalised).parts
    if len(parts) < 3 or parts[0] != ".claude":
        return None
    if parts[1] not in ("agents", "skills"):
        return None
    return "/".join((".claude", f"{parts[1]}.disabled", *parts[2:]))
