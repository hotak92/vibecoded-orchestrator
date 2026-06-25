# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AF — per-project install/update parity: structural tests.

Audit finding (`.claude/context/audits/v0252-per-project-install-parity-2026-06-09.md`):
`update_project_v2` was skipping the 6 post-bundle steps `create_project_v2`
runs (populate, global-module enabled-seed, diagrams seed, codegraph
spawn, kg-sync spawn, kg-summary spawn). The fix extracts the inline
block into a private async helper `apply_post_bundle_steps` and wires
it into BOTH call sites.

These tests are source-parsing regression checks that ensure the
structural refactor is preserved (function exists, both call sites
invoke it in the right order, the helper body still contains all 6
ordered steps). They mirror the same shape as
``test_codegraph_language_scoped_prune.py::test_hook_sh_does_not_pass_prune_stale_v52_o7``
(regex over the .rs source).

A full Rust integration test (create a temp project, call create_v2 +
update_v2, assert post-bundle DB state) was deferred: too much DB
fixture scaffolding for first pass. The structural tests here pin the
refactor against accidental regression (e.g. someone re-inlines one of
the steps, or reorders the helper's internal block).

See v0.2.52 backlog § V52-AF.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

_PROJECTS_V2_PATH = (
    _REPO / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)


# ---------------------------------------------------------------------------
# Helper — load the .rs source once per test session.
# ---------------------------------------------------------------------------


def _load_projects_v2_source() -> str:
    """Return the projects_v2.rs source as a string.

    Fail loudly if the file disappeared — that's a structural break
    bigger than V52-AF.
    """
    assert _PROJECTS_V2_PATH.is_file(), (
        f"projects_v2.rs missing at {_PROJECTS_V2_PATH}. The V52-AF "
        "structural-parity tests can't validate a file that doesn't exist."
    )
    return _PROJECTS_V2_PATH.read_text(encoding="utf-8")


def _extract_fn_body(source: str, fn_name: str) -> str:
    """Return the body of an async fn / fn definition by name.

    Brace-counting parser — handles nested braces correctly. Returns
    the substring between the opening `{` and its matching `}`.

    Used to scope assertions to one function at a time (so a call-site
    assertion in `create_project_v2` doesn't get confused by code in
    `update_project_v2` or vice versa).
    """
    # Match `fn name(...)` or `async fn name(...)` or
    # `pub async fn name(...)` followed by the opening brace.
    # The signature may span multiple lines, so we just look for the
    # opening of a function whose name matches, then count braces.
    pattern = re.compile(
        r"(?:pub\s+)?(?:async\s+)?fn\s+" + re.escape(fn_name) + r"\b",
    )
    m = pattern.search(source)
    assert m, f"function `{fn_name}` not found in projects_v2.rs"

    # Find the first `{` after the signature start.
    brace_start = source.find("{", m.end())
    assert brace_start != -1, f"function `{fn_name}` has no opening brace"

    # Count braces to find the matching close. Tolerates braces inside
    # string literals well enough for the assertions below (we don't
    # need pixel-perfect Rust lexing — none of the .rs strings contain
    # unbalanced braces in this file).
    depth = 1
    i = brace_start + 1
    while i < len(source) and depth > 0:
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1

    assert depth == 0, f"function `{fn_name}` has unbalanced braces"
    return source[brace_start + 1 : i - 1]


# ---------------------------------------------------------------------------
# Test 1 — `apply_post_bundle_steps` exists with the expected signature.
# ---------------------------------------------------------------------------


def test_apply_post_bundle_steps_exists_with_expected_signature() -> None:
    """V52-AF (v0.2.52, 2026-06-09): the post-bundle phase MUST live in
    a single private async helper. Without this, create + update can
    drift apart again — which is exactly what the v0.2.45→v0.2.52 audit
    found.

    Asserts:
    - The function `apply_post_bundle_steps` exists.
    - It's declared `async` (background-task spawns need an await-able
      path).
    - It takes 6 parameters in the documented order: project_id (&str),
      project_name (&str), folder (&Path), app (&AppHandle), db (&Db),
      is_initial_create (bool).
    - It returns `Vec<String>` (the warnings aggregation contract).

    Regex-source-parsing assertion — mirrors the
    `test_hook_sh_does_not_pass_prune_stale_v52_o7` pattern.
    """
    src = _load_projects_v2_source()

    # Match the full signature across multiple lines (parameters are
    # one-per-line in the conventional Rust formatter style).
    pattern = re.compile(
        r"async\s+fn\s+apply_post_bundle_steps\s*\(\s*"
        r"project_id:\s*&str\s*,\s*"
        r"project_name:\s*&str\s*,\s*"
        r"folder:\s*&Path\s*,\s*"
        r"app:\s*&tauri::AppHandle\s*,\s*"
        r"db:\s*&Db\s*,\s*"
        r"is_initial_create:\s*bool\s*,?\s*"
        r"\)\s*->\s*Vec<String>",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "V52-AF regression: `apply_post_bundle_steps` is missing or its "
        "signature has changed. Expected:\n"
        "  async fn apply_post_bundle_steps(\n"
        "      project_id: &str,\n"
        "      project_name: &str,\n"
        "      folder: &Path,\n"
        "      app: &tauri::AppHandle,\n"
        "      db: &Db,\n"
        "      is_initial_create: bool,\n"
        "  ) -> Vec<String>\n"
        "If you're refactoring this helper, update both call sites "
        "(create_project_v2 + update_project_v2) AND this test."
    )


# ---------------------------------------------------------------------------
# Test 2 — `create_project_v2` calls the helper, doesn't re-inline.
# ---------------------------------------------------------------------------


def test_create_project_v2_calls_apply_post_bundle_steps() -> None:
    """V52-AF (v0.2.52, 2026-06-09): the create path MUST delegate its
    post-bundle phase to the shared helper. Without this, the helper
    exists but only update uses it — the drift starts again.

    v0.2.68 (Defect B): `create_project_v2` no longer runs the heavy
    post-bundle phase INLINE. The slow phases (bootstrap-collections +
    install-bundle + the post-bundle pipeline) were moved into a detached
    setup task so the New Project modal returns FAST instead of blocking
    ~51s on a cold backend. The create-path phases now live in the
    `create_setup_phases` closure (projects_v2.rs), which is what
    `create_project_v2` hands to `project_setup::spawn_setup_task`. The
    `apply_post_bundle_steps` delegation + create/update parity it guards
    are UNCHANGED — only the call moved from `create_project_v2`'s inline
    body into `create_setup_phases`. So this test now scopes the
    helper-call + `is_initial_create: true` assertions to
    `create_setup_phases`, and keeps the "not re-inlined into
    create_project_v2" sentinel on `create_project_v2` itself.

    Asserts:
    - `create_setup_phases`'s body contains a call to
      `apply_post_bundle_steps(...)`.
    - That call passes `is_initial_create: true` (codegraph spawn does
      NOT prune on first create — no rows possible).
    - The post-bundle `populate_project_state_from_filesystem` block is
      NOT inlined into `create_project_v2` (the helper owns it; the
      uniquely-identifying comment fragment must not be in that body).
    """
    src = _load_projects_v2_source()
    # v0.2.68: the create-path post-bundle phases live in this closure now.
    phases_body = _extract_fn_body(src, "create_setup_phases")

    # Helper call present in the create-path setup closure.
    assert "apply_post_bundle_steps(" in phases_body, (
        "V52-AF regression: the create path (`create_setup_phases`) no "
        "longer calls `apply_post_bundle_steps`. The post-bundle phase "
        "MUST be delegated to the shared helper so update_project_v2 can "
        "use the same code path. (v0.2.68 moved this call out of "
        "create_project_v2's inline body into the create_setup_phases "
        "closure — if you refactored again, point this assertion at the "
        "new home, don't drop the delegation.)"
    )

    # is_initial_create=true on create. (v0.2.68 arg names inside the
    # closure: &project_id / &project_name / &folder, vs the pre-v0.2.68
    # inline &row.id / &req.name / folder.)
    assert re.search(
        r"apply_post_bundle_steps\s*\([^)]*is_initial_create\s*\*/\s*true",
        phases_body,
        re.DOTALL,
    ) or re.search(
        # Tolerate the comment being dropped — match `true` as the last
        # positional arg after the 5 borrows, allowing either arg-name set.
        r"apply_post_bundle_steps\s*\(\s*&(?:row\.id|project_id)\s*,\s*"
        r"&(?:req\.name|project_name)\s*,\s*(?:&)?folder\s*,\s*&app\s*,\s*&db\s*,\s*"
        r"(?:/\*\s*is_initial_create\s*\*/\s*)?true",
        phases_body,
        re.DOTALL,
    ), (
        "V52-AF regression: the create path MUST pass "
        "`is_initial_create: true` to `apply_post_bundle_steps`. On "
        "first create no per-project code-graph rows can be stale, so "
        "prune_stale must be false (audit AF-6). Passing `false` here "
        "would let codegraph::spawn_initial_build do a no-op prune "
        "pass — wasteful but not catastrophic. Passing the wrong value "
        "is still a real regression."
    )

    # The post-bundle populate must NOT be re-inlined into create_project_v2.
    body = _extract_fn_body(src, "create_project_v2")

    # The post-bundle populate is no longer inline.
    # The unique sentinel: the comment block "Re-call populate now that
    # the bundle has dropped its files." was inside create_project_v2
    # pre-V52-AF; it MUST now live ONLY inside apply_post_bundle_steps.
    assert "Re-call populate now that the bundle" not in body, (
        "V52-AF regression: the post-bundle populate block was re-inlined "
        "into create_project_v2. It MUST live ONLY in "
        "apply_post_bundle_steps so update_project_v2 gets the same "
        "behavior (the whole point of the extraction). Move the block "
        "back into the helper."
    )


# ---------------------------------------------------------------------------
# Test 3 — `update_project_v2` calls the helper in the right place.
# ---------------------------------------------------------------------------


def test_update_project_v2_calls_apply_post_bundle_steps() -> None:
    """V52-AF (v0.2.52, 2026-06-09): `update_project_v2` MUST delegate
    its post-bundle phase to the same helper, AFTER step 5
    (apply_project_env_via_python) and BEFORE step 6
    (retry_failed_module_installs).

    Asserts:
    - `update_project_v2`'s body contains a call to
      `apply_post_bundle_steps(...)`.
    - The call passes `is_initial_create: false` (codegraph spawn DOES
      prune on update — files may have been deleted between create +
      update).
    - The call appears AFTER `apply_project_env_via_python` (string
      `apply_project_env_via_python` appears at a smaller offset than
      `apply_post_bundle_steps`).
    - The call appears BEFORE `retry_failed_module_installs` (string
      offset comparison the other way).

    Ordering matters: the codegraph + kg-sync background tasks read
    `.claude/env`, so they need the env refresh to have committed.
    `retry_failed_module_installs`'s audit writes mustn't race against
    the helper's audit entries — easier reasoning if the helper finishes
    first.
    """
    src = _load_projects_v2_source()
    body = _extract_fn_body(src, "update_project_v2")

    # Helper call present.
    assert "apply_post_bundle_steps(" in body, (
        "V52-AF SHIP-BLOCKER regression: `update_project_v2` no longer "
        "calls `apply_post_bundle_steps`. This is THE bug V52-AF was "
        "fixing: pre-v0.2.52, update_project_v2 skipped all 6 post-"
        "bundle steps. Without this call, a project created on v0.2.45 "
        "+ Update bundle on v0.2.52 ends up with new .md files on disk "
        "but stale launcher.db (agents/skills/hooks tables not "
        "re-derived). GUI tabs lie."
    )

    # is_initial_create=false on update.
    assert re.search(
        r"apply_post_bundle_steps\s*\([^)]*is_initial_create\s*\*/\s*false",
        body,
        re.DOTALL,
    ) or re.search(
        r"apply_post_bundle_steps\s*\(\s*&row\.id\s*,\s*"
        r"&row\.name\s*,\s*&folder\s*,\s*&app\s*,\s*&db\s*,\s*"
        r"(?:/\*\s*is_initial_create\s*\*/\s*)?false",
        body,
        re.DOTALL,
    ), (
        "V52-AF regression: `update_project_v2` MUST pass "
        "`is_initial_create: false` to `apply_post_bundle_steps`. On "
        "update, prior code-graph rows may reference files the user "
        "deleted between create + update, so prune_stale=true is the "
        "correct behavior (audit AF-6). Passing `true` here means the "
        "update would NOT prune stale rows — silent data drift."
    )

    # Ordering: apply_project_env_via_python (step 5) → helper → retry
    # (step 6).
    #
    # Use a call-site-matching regex (not bare .find()) for each anchor
    # — the body contains comment references to all three names (e.g.
    # the helper's call-site comment block names retry_failed_module_installs
    # in passing). We want the offset of the actual CALL, not the comment.
    env_call = re.search(r"apply_project_env_via_python\s*\(", body)
    helper_call = re.search(r"apply_post_bundle_steps\s*\(", body)
    retry_call = re.search(
        r"crate::commands::module_service::retry_failed_module_installs\s*\(",
        body,
    )
    assert env_call, (
        "could not locate apply_project_env_via_python call site in update_project_v2"
    )
    assert helper_call, (
        "could not locate apply_post_bundle_steps call site in update_project_v2"
    )
    assert retry_call, (
        "could not locate retry_failed_module_installs call site in update_project_v2"
    )
    env_offset = env_call.start()
    helper_offset = helper_call.start()
    retry_offset = retry_call.start()

    assert env_offset < helper_offset, (
        "V52-AF regression: `apply_post_bundle_steps` MUST be called "
        "AFTER `apply_project_env_via_python`. The helper spawns "
        "background tasks (kg-sync, codegraph) that read .claude/env "
        "— they need the freshly-written env to be committed first. "
        "If the helper runs before env refresh, the spawned tasks read "
        "stale VCT_ORCHESTRATOR_ROOT etc."
    )
    assert helper_offset < retry_offset, (
        "V52-AF regression: `apply_post_bundle_steps` MUST be called "
        "BEFORE `retry_failed_module_installs`. The retry path writes "
        "its own audit entries; ordering the helper FIRST keeps the "
        "audit log readable + avoids any future race conditions."
    )


# ---------------------------------------------------------------------------
# Test 4 — `apply_post_bundle_steps` contains all 6 ordered steps.
# ---------------------------------------------------------------------------


def test_apply_post_bundle_steps_contains_all_six_ordered_steps() -> None:
    """V52-AF (v0.2.52, 2026-06-09): the helper MUST contain all 6
    documented steps in the documented order. This pins the contract
    against accidental drift (someone deletes a step thinking it's
    unused; someone reorders the steps and breaks the race-condition
    constraints documented in comments).

    The 6 steps in order:
      1. populate_project_state_from_filesystem (post-bundle re-walk)
      2. seed_enabled_rows_for_new_project (global modules → this project)
      3. set_project_module_enabled('diagrams', true)
      4. upsert_code_graph_build + codegraph::spawn_initial_build
      5. upsert_kg_sync + kg_sync::spawn_initial_sync
      6. upsert_kg_summary + kg_summary::spawn_initial_summary

    Order is verified by string-offset comparison — each step's anchor
    must appear at a strictly increasing offset within the helper body.
    """
    src = _load_projects_v2_source()
    body = _extract_fn_body(src, "apply_post_bundle_steps")

    # Each (step_number, anchor_string, description) tuple. Anchor must
    # appear in the helper body; order is checked below.
    steps = [
        (1, "populate_project_state_from_filesystem",
         "step 1: post-bundle populate of agents/skills/hooks/kg-collection-access "
         "(audit AF-1: this is the headline gap)"),
        (2, "seed_enabled_rows_for_new_project",
         "step 2: seed enabled_for_project=true for global modules (audit AF-2)"),
        (3, "set_project_module_enabled",
         "step 3: seed project_modules('diagrams') = true (opt-out default)"),
        (4, "upsert_code_graph_build",
         "step 4a: queue PENDING code-graph build row"),
        (4, "codegraph::spawn_initial_build",
         "step 4b: spawn code-graph build background task (audit AF-6)"),
        (5, "upsert_kg_sync",
         "step 5a: queue PENDING kg-sync row"),
        (5, "kg_sync::spawn_initial_sync",
         "step 5b: spawn kg-sync background task (audit AF-7)"),
        (6, "upsert_kg_summary",
         "step 6a: queue PENDING kg-summary row"),
        (6, "kg_summary::spawn_initial_summary",
         "step 6b: spawn kg-summary background task (audit AF-8)"),
    ]

    offsets = []
    for step_num, anchor, description in steps:
        offset = body.find(anchor)
        assert offset >= 0, (
            f"V52-AF regression: missing {description}. "
            f"Anchor '{anchor}' not found in `apply_post_bundle_steps`. "
            f"This is one of the 6 documented post-bundle steps the "
            f"audit explicitly requires. Re-adding the step verbatim "
            f"is the fix; do NOT just remove this test."
        )
        offsets.append((step_num, anchor, offset, description))

    # Sort by offset (we'll then verify step numbers are monotonically
    # non-decreasing).
    sorted_by_offset = sorted(offsets, key=lambda t: t[2])
    last_step = 0
    for step_num, anchor, offset, description in sorted_by_offset:
        assert step_num >= last_step, (
            f"V52-AF regression: helper step ordering broken. "
            f"Anchor '{anchor}' (step {step_num}: {description}) "
            f"appears at offset {offset} but a later step has already "
            f"appeared (last seen step {last_step}). "
            f"The ordering rule from the audit + the comments in the "
            f"helper is: populate → global-seed → diagrams → codegraph "
            f"→ kg-sync → kg-summary. Race-condition fixes from prior "
            f"releases depend on this order. DO NOT reorder."
        )
        last_step = step_num


# ---------------------------------------------------------------------------
# Test 5 — helper does NOT use AppHandle-by-value (forbidding the
# pre-extraction pattern of consuming `app` mid-function).
# ---------------------------------------------------------------------------


def test_apply_post_bundle_steps_uses_apphandle_by_borrow() -> None:
    """V52-AF (v0.2.52, 2026-06-09): the helper MUST take `&AppHandle`
    (borrow), not `AppHandle` (owned). Owning would prevent the helper
    from cloning `app` for the 3 background-task spawns (each needs an
    owned AppHandle).

    Pre-extraction, `create_project_v2` consumed `app` by-value on the
    last spawn (kg_summary). The extracted helper instead clones at
    each spawn site, which works only if `app: &AppHandle`.

    This test asserts the borrow form is preserved — accidental
    refactoring to owned would compile (the test exists BECAUSE the
    compile-time check is the only safety net otherwise, and Rust does
    let you write `app: AppHandle` and clone, just less idiomatically).
    """
    src = _load_projects_v2_source()

    # Locate the helper signature.
    sig_match = re.search(
        r"async\s+fn\s+apply_post_bundle_steps\s*\([^)]*\)",
        src,
        re.DOTALL,
    )
    assert sig_match, "helper signature not found (Test 1 should catch this first)"
    sig = sig_match.group(0)

    # Require `&tauri::AppHandle` (with the ampersand). Reject the
    # bare `tauri::AppHandle` form.
    assert "&tauri::AppHandle" in sig, (
        "V52-AF regression: `apply_post_bundle_steps` MUST take "
        "`app: &tauri::AppHandle` (borrow), not `tauri::AppHandle` "
        "(owned). The helper needs to clone `app` 3 times for the "
        "codegraph / kg-sync / kg-summary spawns — that requires a "
        "borrow, not ownership."
    )


# ---------------------------------------------------------------------------
# Test 6 — load-bearing comments preserved (forensic-trail discipline).
# ---------------------------------------------------------------------------


def test_load_bearing_comments_preserved_in_helper() -> None:
    """V52-AF (v0.2.52, 2026-06-09): the helper inherits the load-
    bearing comments from the pre-extraction inline block. These
    comments document race-condition fixes from prior incidents
    (PR #149 populate-ordering, 2026-05-06 install-flow-overhaul,
    Gap 2 OSS launch, v0.2.49 Stream B, v0.2.37 F6).

    The CLAUDE.md project rule explicitly says: 'Preserve ALL the
    v0.2.49 Stream B + v0.2.37 Finding F6 + Gap 2 + race-condition
    comments — they're load-bearing forensic trail.'

    This test pins a handful of unique comment-fragment sentinels.
    """
    src = _load_projects_v2_source()
    body = _extract_fn_body(src, "apply_post_bundle_steps")

    required_sentinels = [
        # Populate-ordering race fix (PR #149-shape):
        ("Re-call populate now that the bundle",
         "populate-ordering race fix from PR #149"),
        # v0.2.49 Stream B (global-module seeding):
        ("v0.2.49 Stream B",
         "v0.2.49 Stream B context for global-module enabled-seed"),
        # 2026-05-06 install-flow-architectural-overhaul (diagrams seed):
        ("install-flow-architectural-overhaul",
         "2026-05-06 race-condition rule documenting why diagrams "
         "seed runs AFTER bundle install"),
        # Gap 2 (OSS launch 2026-05-12):
        ("Gap 2",
         "Gap 2 OSS launch context for codegraph spawn"),
        # codegraph race fix (2026-05-06):
        ("code-graph-analyze script not found",
         "codegraph race-fix forensic trail (the actual failure mode "
         "from the pre-fix state)"),
        # KG auto-sync rationale:
        ("KG auto-sync",
         "kg-sync background-spawn rationale + ordering discipline"),
        # KG summary auto-backfill:
        ("KG summary auto-backfill",
         "kg-summary background-spawn rationale"),
    ]

    for sentinel, description in required_sentinels:
        assert sentinel in body, (
            f"V52-AF regression: load-bearing comment dropped from "
            f"`apply_post_bundle_steps`: {description}. "
            f"Looking for sentinel: '{sentinel}'. "
            f"The CLAUDE.md project rule + V52-AF spec explicitly "
            f"require these comments to be preserved verbatim — they "
            f"document race-condition fixes from prior incidents and "
            f"are forensic trail for future maintainers."
        )


# ---------------------------------------------------------------------------
# Test 7 — `update_all_projects` correctly inherits the fix via
# update_project_v2 (no direct call needed).
# ---------------------------------------------------------------------------


def test_update_all_projects_does_not_duplicate_helper_call() -> None:
    """V52-AF (v0.2.52, 2026-06-09): `update_all_projects` iterates
    `list_projects_v2` and calls `update_project_v2` per row. It MUST
    NOT also call `apply_post_bundle_steps` directly — that would
    double-invoke the helper for every project (wasted work + duplicate
    spawn races).

    The audit confirms: 'update_all_projects only loops update_project_v2;
    whatever update_project_v2 misses, the bulk path misses N times.'
    Symmetrically, whatever update_project_v2 gains, the bulk path gains
    N times. So this test pins the bulk path against accidental
    duplicate wiring.
    """
    src = _load_projects_v2_source()
    body = _extract_fn_body(src, "update_all_projects")

    # No direct call to apply_post_bundle_steps inside update_all_projects.
    # The helper is reached transitively via the per-row update_project_v2
    # call (which IS expected to be present).
    assert "apply_post_bundle_steps(" not in body, (
        "V52-AF regression: `update_all_projects` is calling "
        "`apply_post_bundle_steps` directly. It MUST reach the helper "
        "transitively via `update_project_v2` (one call per project). "
        "Direct invocation would double-spawn the codegraph + kg-sync "
        "+ kg-summary background tasks per project — wasted work + "
        "potential races between the duplicate spawns."
    )

    # And update_all_projects MUST still call update_project_v2 (it's
    # the only way the bulk path gets the fix).
    assert "update_project_v2(" in body, (
        "V52-AF regression: `update_all_projects` no longer calls "
        "`update_project_v2`. The bulk update path depends on the "
        "per-project update path to inherit the V52-AF fix transitively. "
        "If the bulk path is now self-contained, it must call "
        "`apply_post_bundle_steps` directly per row — but that's a "
        "bigger refactor than V52-AF intended."
    )
