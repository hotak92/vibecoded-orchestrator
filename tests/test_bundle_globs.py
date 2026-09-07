# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Guards for the single-source-of-truth bundle globs (v0.2.54 Track G, G-4).

History: three divergent hook-flavour policies coexisted (install.py Step 9b
native-only, project_init both-flavours, orchestrator-self everything-by-
iterdir), and the script-pattern list was inlined twice and HAD drifted
(project_init was missing the extension-less workflow wrappers). These tests
pin the unification so the lists can't fork again silently."""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib.bundle_globs import hook_globs, script_patterns  # noqa: E402

# The ONE documented artefact under a bundle-globs-governed directory that no
# ship pattern matches, on purpose. It is a launchd plist BODY, not a script:
# nothing in `.claude/scripts/` would ever execute it, and the live plist
# templates the boot service actually reads are in `templates/launchd/`
# (`vco_lib/boot_service.py:1082,1118,1205`). Same call already recorded at
# `tests/test_install_self_materializes_claude_dir.py:104`.
SHIP_PATTERN_EXEMPT = {"launchctl-plist.template"}


def test_hook_globs_ship_both_flavours():
    globs = hook_globs()
    assert "*.sh" in globs and "*.ps1" in globs, (
        "both-flavours policy regressed — see vco_lib/bundle_globs.py "
        "docstring for the v0.2.14 Concern-#2 rationale"
    )


def test_script_patterns_include_workflow_wrappers():
    """The drift that motivated the extraction: project bundles silently
    missed the extension-less workflow wrappers because only ONE of the two
    inline copies listed them."""
    pats = script_patterns()
    assert "detect-workflow-needs" in pats
    assert "generate-workflow" in pats
    assert "*.py" in pats and "*.ps1" in pats


def test_no_resurrected_inline_copies():
    """Neither install.py nor project_init may re-grow a private flavour
    policy / pattern list. String-level guard: the old native-only accessor
    must stay dead, and project_init (the ONE bundle engine) must reference the
    shared module.

    v0.2.85 (PLAN-v0285 WP-1) UPDATE: install.py no longer references
    ``vco_lib.bundle_globs`` at all — its bespoke Step-5b/9b hook/script
    materialize was DELETED and the root now DELEGATES to the install-bundle
    engine (project_init), which is the single home for the glob policy. So the
    guard for install.py is now inverted: it must have NO private flavour
    accessor (the resurrection risk) — but requiring a bundle_globs REFERENCE
    would force a copy back into install.py, defeating the delegation. Only
    project_init must still reference the shared module.
    """
    install_py = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    project_init = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(
        encoding="utf-8"
    )
    # project_init (the bundle engine) still owns + references the shared globs.
    assert "vco_lib.bundle_globs" in project_init
    # Neither file may resurrect a private native-flavour-only accessor.
    assert "def _hook_glob_for_os" not in install_py, (
        "native-flavour-only accessor resurrected in install.py"
    )
    assert "def _hook_glob_for_os" not in project_init, (
        "legacy single-glob accessor resurrected in project_init"
    )
    # install.py must NOT re-grow a private hook/script flavour policy: since
    # v0.2.85 it delegates, so it should carry no bundle-globs import at all
    # (a re-appearance would signal a resurrected inline enumeration).
    assert "from vco_lib.bundle_globs import" not in install_py, (
        "install.py re-grew a bundle-globs import — the root install "
        "delegates to the bundle engine (PLAN-v0285 D1); it must not "
        "re-enumerate hooks/scripts itself"
    )


def test_every_bundle_globbed_template_file_is_matched_by_a_ship_pattern():
    """The WALK: nothing under a globbed template dir ships by accident.

    The four tests above pin the CONTENT of the two lists (both hook
    flavours, the workflow wrappers, no resurrected inline copies) and the
    one below pins that a hook pair reaches a bundle. None of them looks at
    the directories the lists are pointed AT — so the lists stay correct
    about what they say and silent about what they miss. The two dead
    Windows hooks and the two drifted script lists were both discovered by
    someone eventually LOOKING at the tree; this is that look, on every run.

    Scope is exactly the two directories `vco_lib.bundle_globs` governs:
    `templates/hooks/**` (recursive — top level plus `_lib/`) against
    `hook_globs()`, and `templates/scripts/*` (non-recursive, matching
    `project_init._enumerate_bundle_files`' own `scripts_src.glob(pat)`)
    against `script_patterns()`. `templates/agents/`, `templates/skills/`,
    `templates/knowledge/` and `templates/launchd/` ship by their own fixed
    rules, not by these globs, and are deliberately not asserted here.

    It passes today (0 unmatched). The point is the NEXT artefact: a
    `.psm1` hook, a `.mjs` script, a hook dropped in a new subdirectory —
    each would be enumerated by nothing, installed nowhere, and reported by
    no existing gate.
    """
    unmatched: list[str] = []

    hooks_root = REPO_ROOT / "templates" / "hooks"
    assert hooks_root.is_dir(), hooks_root
    hook_files = [p for p in sorted(hooks_root.rglob("*")) if p.is_file()]
    assert hook_files, "found no template hooks — the walk is pointed wrong"
    for path in hook_files:
        if not any(fnmatch.fnmatch(path.name, g) for g in hook_globs()):
            unmatched.append(f"{path.relative_to(REPO_ROOT)}  (hook_globs)")

    scripts_root = REPO_ROOT / "templates" / "scripts"
    assert scripts_root.is_dir(), scripts_root
    script_files = [p for p in sorted(scripts_root.iterdir()) if p.is_file()]
    assert script_files, "found no template scripts — the walk is pointed wrong"
    for path in script_files:
        if path.name in SHIP_PATTERN_EXEMPT:
            continue
        if not any(fnmatch.fnmatch(path.name, p) for p in script_patterns()):
            unmatched.append(f"{path.relative_to(REPO_ROOT)}  (script_patterns)")

    assert not unmatched, (
        "template artefact(s) that NO ship pattern matches — they would be "
        "installed into no project and reported by nothing. Add a pattern to "
        "vco_lib/bundle_globs.py, or add the file to SHIP_PATTERN_EXEMPT in "
        "this module with the reason it must not ship:\n  "
        + "\n  ".join(unmatched)
    )


def test_project_bundle_ships_both_flavours(tmp_path):
    """Functional check through the real project_init bundle planner: a
    template hook pair must produce ops for BOTH flavours regardless of
    host OS."""
    from vco_lib import project_init

    orch = tmp_path / "orch"
    hooks = orch / "templates" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "sample-hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (hooks / "sample-hook.ps1").write_text("Write-Host hi\n", encoding="utf-8")

    ops = project_init._enumerate_bundle_files(orch)  # type: ignore[attr-defined]
    names = {Path(op.dest_rel).name for op in ops}
    assert "sample-hook.sh" in names
    assert "sample-hook.ps1" in names
