# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Static contract test for the v0.2.64 release tag-lag fix (P3).

THE PROBLEM this guards:
    A release tag (vN) is cut on a source commit whose IN-REPO prebuilt
    binaries are still vN-1's. `commit-dist-binaries` then lands the fresh
    vN binaries on main as a `chore(binary): refresh ... for vN [skip ci]`
    commit ABOVE the tag. So `git checkout vN` yields vN source + vN-1
    binaries — a manual-tag-checkout dev runs vN-1 CODE.

THE FIX (in .github/workflows/release.yml):
    1. `commit-dist-binaries` force-moves the vN tag onto the refresh commit
       so `git checkout vN` is self-consistent.
    2. That force-push re-fires Release (`on: push: tags`). The
       `refresh-guard` job detects the binary-refresh commit by its SUBJECT
       and publishes `is_binary_refresh`; `pre-release-gate` and `build`
       both gate on it, so the re-pointed-tag run is a no-op.

This is a STATIC lint over the workflow YAML — the live behaviour runs on
GitHub Actions, which pytest cannot exercise. The lint pins the wiring so a
future edit cannot silently break either the re-point (re-introducing the
lag) or the guard (re-introducing the rebuild loop).

The most important invariant this test enforces is the COUPLING between two
strings that live in different jobs and must agree exactly:

  - the SUBSTRINGS the `refresh-guard` step greps for, and
  - the `git commit -m` SUBJECT that `commit-dist-binaries` actually writes.

If they drift, the guard stops recognising the refresh commit and the
workflow loops forever on a re-pointed tag. We assert both markers
(`chore(binary): refresh` and the literal `[skip ci]`) appear in the commit
subject the workflow produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The two markers the guard greps for. These MUST appear in the
# commit-dist-binaries commit subject or the guard never fires.
MARKER_REFRESH = "chore(binary): refresh"
MARKER_SKIP_CI = "[skip ci]"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert RELEASE_YML.is_file(), f"release.yml not found at {RELEASE_YML}"
    data = yaml.safe_load(RELEASE_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "release.yml did not parse to a mapping"
    return data


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "release.yml has no jobs mapping"
    return jobs


def _job_steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _step_run_text(steps: list[dict]) -> str:
    """Concatenate every step's run text for substring assertions."""
    return "\n".join(str(s.get("run", "")) for s in steps)


# ── refresh-guard job ─────────────────────────────────────────────────────


def test_refresh_guard_job_exists(jobs: dict):
    assert "refresh-guard" in jobs, "refresh-guard job is missing — re-trigger loop is unguarded"


def test_refresh_guard_publishes_output(jobs: dict):
    guard = jobs["refresh-guard"]
    outputs = guard.get("outputs") or {}
    assert "is_binary_refresh" in outputs, (
        "refresh-guard must publish is_binary_refresh as a job output so "
        "pre-release-gate / build can gate on it"
    )


def test_refresh_guard_matches_commit_markers(jobs: dict):
    """The guard must grep for BOTH markers that the refresh commit carries."""
    run = _step_run_text(_job_steps(jobs["refresh-guard"]))
    assert MARKER_REFRESH in run, (
        f"refresh-guard must grep for {MARKER_REFRESH!r} (the refresh-commit marker)"
    )
    assert MARKER_SKIP_CI in run, (
        f"refresh-guard must grep for the literal {MARKER_SKIP_CI!r} marker"
    )
    # Must read the SUBJECT (not the full body) so a user commit quoting the
    # phrase in its body cannot trip the guard.
    assert "git log -1 --format=%s" in run, (
        "refresh-guard must inspect the HEAD commit SUBJECT (git log -1 --format=%s)"
    )


# ── gating of expensive jobs on the guard ─────────────────────────────────


@pytest.mark.parametrize("job_name", ["pre-release-gate", "build"])
def test_expensive_job_gates_on_guard(jobs: dict, job_name: str):
    job = jobs[job_name]
    needs = job.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert "refresh-guard" in needs_list, (
        f"{job_name} must `needs: refresh-guard` so it can gate on the output"
    )
    cond = str(job.get("if", ""))
    assert "refresh-guard.outputs.is_binary_refresh" in cond, (
        f"{job_name} must gate its `if:` on refresh-guard's is_binary_refresh output"
    )
    assert "!= 'true'" in cond, (
        f"{job_name}'s guard condition must run only when is_binary_refresh != 'true'"
    )


# ── re-point step in commit-dist-binaries ──────────────────────────────────


def test_commit_dist_binaries_has_repoint_step(jobs: dict):
    steps = _job_steps(jobs["commit-dist-binaries"])
    repoint = [s for s in steps if "Re-point release tag" in str(s.get("name", ""))]
    assert len(repoint) == 1, (
        "commit-dist-binaries must have exactly one 'Re-point release tag' step"
    )
    step = repoint[0]

    cond = str(step.get("if", ""))
    # Only re-point on a real tag push that actually committed a refresh.
    assert "refs/tags/v" in cond, "re-point must be gated to tag pushes"
    assert "steps.stage.outputs.changed != '0'" in cond, (
        "re-point must only run when a binary refresh commit was actually made"
    )

    run = str(step.get("run", ""))
    assert "git tag -f" in run, "re-point must force-move the tag onto the refresh commit"
    assert "git push --force origin" in run, "re-point must force-push the moved tag"


def test_repoint_targets_pushed_refresh_commit(jobs: dict):
    """The re-point step must move the tag onto the SHA the push step recorded."""
    steps = _job_steps(jobs["commit-dist-binaries"])

    push_steps = [s for s in steps if str(s.get("id", "")) == "push_binaries"]
    assert len(push_steps) == 1, "commit-dist-binaries push step must have id: push_binaries"
    push_run = str(push_steps[0].get("run", ""))
    assert "refresh_sha=" in push_run and 'GITHUB_OUTPUT' in push_run, (
        "push step must export the refresh commit SHA as the refresh_sha output"
    )

    repoint = [s for s in steps if "Re-point release tag" in str(s.get("name", ""))][0]
    repoint_env = repoint.get("env") or {}
    assert any(
        "push_binaries.outputs.refresh_sha" in str(v) for v in repoint_env.values()
    ), "re-point step must consume steps.push_binaries.outputs.refresh_sha"


def test_refresh_commit_subject_carries_both_markers(jobs: dict):
    """The commit-dist-binaries commit subject must contain BOTH guard markers.

    This is the coupling invariant: the subject the workflow writes and the
    substrings the guard greps for must agree, or the guard never fires and
    the re-pointed tag loops the workflow forever.
    """
    run = _step_run_text(_job_steps(jobs["commit-dist-binaries"]))
    # The `git commit -m "<subject>` line is where both markers live.
    assert MARKER_REFRESH in run, (
        f"commit-dist-binaries commit subject must contain {MARKER_REFRESH!r}"
    )
    assert MARKER_SKIP_CI in run, (
        f"commit-dist-binaries commit subject must contain {MARKER_SKIP_CI!r}"
    )


# ── CONCERN-1: shallow-clone prev_tag^ false-fail at v0.2.65 ────────────────
#
# These are STATIC YAML lints. GitHub Actions' shallow-clone behaviour and
# `git rev-parse prev_tag^` resolution cannot be exercised inside pytest, so
# we pin the two structural guarantees that PREVENT the false-fail:
#   1. pre-release-gate's checkout fetches full history (fetch-depth: 0), so a
#      re-pointed prev_tag's PARENT commit is always present locally.
#   2. Gate 3 carries an explicit missing-parent fallback that emits a
#      ::warning:: (degraded comparison) instead of letting `|| true` coerce
#      an unresolvable-ref error into an empty diff (the false-fail).


def _pre_release_gate_checkout(jobs: dict) -> dict:
    """Return the first `actions/checkout` step of pre-release-gate."""
    steps = _job_steps(jobs["pre-release-gate"])
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "pre-release-gate must have an actions/checkout step"
    return checkouts[0]


def test_pre_release_gate_checkout_is_full_depth(jobs: dict):
    """pre-release-gate checkout must set fetch-depth: 0 (full history).

    Gate 3 compares against `prev_tag^` when prev_tag is a re-pointed
    binary-refresh commit (true from v0.2.65 on). On the default shallow
    clone (depth 1) the tag's parent is absent and `prev_tag^` is
    unresolvable → false-fail. fetch-depth: 0 guarantees the parent exists.
    """
    checkout = _pre_release_gate_checkout(jobs)
    with_block = checkout.get("with") or {}
    # YAML parses `0` as int; accept both int and str forms.
    assert str(with_block.get("fetch-depth")) == "0", (
        "pre-release-gate checkout must set fetch-depth: 0 so a re-pointed "
        "prev_tag's parent commit (prev_tag^) is present for Gate 3"
    )


def test_gate3_has_missing_parent_fallback_warning(jobs: dict):
    """Gate 3 must verify prev_tag^ resolves and fall back with a WARNING.

    The Gate must never FAIL on a heuristic it could not evaluate. When the
    parent is unresolvable, Gate 3 must (a) NOT use it, (b) emit a
    ::warning:: (degraded, not failed) and (c) compare against prev_tag
    itself instead.
    """
    run = _step_run_text(_job_steps(jobs["pre-release-gate"]))
    # The fallback guards `prev_tag^` resolution with an explicit rev-parse
    # verify before using it as the compare ref.
    assert "git rev-parse --verify --quiet" in run and "${prev_tag}^" in run, (
        "Gate 3 must verify ${prev_tag}^ resolves (git rev-parse --verify) "
        "before diffing against it"
    )
    # The degraded path is a WARNING, not an error/failure.
    assert "::warning::" in run, (
        "Gate 3's missing-parent path must emit ::warning:: (degraded), not fail"
    )


def test_gate3_diff_does_not_swallow_git_errors(jobs: dict):
    """The Gate 3 `git diff <compare_ref>..HEAD` must NOT be `|| true`-masked.

    Under `set -euo pipefail`, a genuine git error (unresolvable ref) must
    surface as a job failure, not be coerced into an empty diff that the gate
    then misreads as "dist unchanged" (the CONCERN-1 false-fail). We assert
    the specific dist diff assignment has no `|| true` swallow.
    """
    run = _step_run_text(_job_steps(jobs["pre-release-gate"]))
    # The exact assignment line introduced by the fix. The fragile bit is the
    # trailing `|| true` that we removed; assert the populated diff line exists
    # and is NOT followed by a `|| true` swallow on the same line.
    assert 'dist_diff="$(git diff "${compare_ref}..HEAD" -- launcher/dist/)"' in run, (
        "Gate 3 must assign dist_diff from a bare `git diff ...` (no `|| true` "
        "swallow that would mask an unresolvable-ref error as an empty diff)"
    )
    assert 'git diff "${compare_ref}..HEAD" -- launcher/dist/ || true' not in run, (
        "Gate 3's dist diff must NOT use `|| true` — that masks git errors into "
        "a false 'dist unchanged' gate failure"
    )


# ── CONCERN-2: self-heal tag re-point on re-run (changed == '0') ────────────
#
# STATIC YAML lints. The silent-defeat scenario (binary push succeeds, tag
# push fails, re-run skips everything) cannot be replayed in pytest. We pin
# the structural guarantees that close the hole:
#   1. A self-heal step exists in commit-dist-binaries that runs even when the
#      main re-point step is skipped (it is NOT gated on `changed != '0'`).
#   2. It runs after upstream failures (`always()`) and only on tag pushes.
#   3. It keeps the security posture: single-ref refspec, never `--force
#      --tags`.


def _self_heal_step(jobs: dict) -> dict:
    steps = _job_steps(jobs["commit-dist-binaries"])
    heal = [s for s in steps if "Self-heal" in str(s.get("name", ""))]
    assert len(heal) == 1, (
        "commit-dist-binaries must have exactly one 'Self-heal' tag re-point step"
    )
    return heal[0]


def test_self_heal_step_exists_and_runs_on_failure(jobs: dict):
    """Self-heal step must run even after an upstream step failed, on tag pushes.

    `always()` makes it run after a failed main re-point tag push; the
    tag-push gate keeps workflow_dispatch dry-runs a no-op.
    """
    step = _self_heal_step(jobs)
    cond = str(step.get("if", ""))
    assert "always()" in cond, (
        "self-heal must use always() so it runs after a failed main re-point step"
    )
    assert "refs/tags/v" in cond, "self-heal must be gated to tag pushes"


def test_self_heal_not_gated_on_changed(jobs: dict):
    """Self-heal must NOT be gated on `changed != '0'`.

    The whole point of CONCERN-2 is that a re-run has `changed == '0'` (binaries
    already on main) yet the tag is still stale. If self-heal also required
    `changed != '0'` it would skip exactly when healing is needed.
    """
    step = _self_heal_step(jobs)
    cond = str(step.get("if", ""))
    assert "steps.stage.outputs.changed" not in cond, (
        "self-heal must NOT gate on steps.stage.outputs.changed — it must run "
        "on a re-run where changed == '0' (binaries already on main, tag stale)"
    )


def test_self_heal_uses_single_ref_refspec(jobs: dict):
    """Self-heal must force-push a single tag ref, never `--force --tags`."""
    step = _self_heal_step(jobs)
    run = str(step.get("run", ""))
    assert 'git push --force origin "refs/tags/${tag}"' in run, (
        "self-heal must push the explicit single-ref refspec refs/tags/<tag>"
    )
    assert "--force --tags" not in run, (
        "self-heal must NEVER use `--force --tags` (would clobber unrelated tags)"
    )
    assert 'git tag -f "${tag}"' in run, (
        "self-heal must force-move the local tag onto the binary-refresh commit"
    )


def test_self_heal_targets_verified_refresh_commit(jobs: dict):
    """Self-heal must re-point onto a VERIFIED binary-refresh commit for THIS
    version found on origin/main — never blindly onto main HEAD.

    It greps origin/main history for the exact refresh-commit subject (refresh
    marker + version + [skip ci]) so an unrelated commit landing on main after
    the refresh cannot drag the tag forward.
    """
    step = _self_heal_step(jobs)
    run = str(step.get("run", ""))
    assert "origin/main" in run, "self-heal must search origin/main for the refresh commit"
    assert MARKER_REFRESH in run, (
        f"self-heal must match the refresh-commit subject marker {MARKER_REFRESH!r}"
    )
    assert MARKER_SKIP_CI in run, (
        f"self-heal must match the {MARKER_SKIP_CI!r} marker in the refresh subject"
    )
    # The version is interpolated into the subject match so only THIS version's
    # refresh commit is targeted.
    assert "for v${version}" in run, (
        "self-heal must match the refresh commit for THIS version (for v${version})"
    )


def test_self_heal_is_idempotent_noop_when_tag_matches(jobs: dict):
    """Self-heal must early-exit (no force-push) when the tag already matches.

    After the normal main re-point step runs, the tag already points at the
    refresh commit. Self-heal must detect that and exit 0 without a second
    force-push (idempotency).
    """
    step = _self_heal_step(jobs)
    run = str(step.get("run", ""))
    # Compares the current tag SHA against the located refresh SHA and exits
    # early when equal.
    assert 'current_tag_sha' in run and 'refresh_sha' in run, (
        "self-heal must compare the current tag SHA against the refresh SHA"
    )
    assert "no action (idempotent no-op)" in run, (
        "self-heal must early-exit as an idempotent no-op when the tag already "
        "points at the refresh commit"
    )


# ── Gate-21 hygiene: no personal-name leak in the public workflow ──────────


def test_no_personal_name_leak_in_release_workflow():
    """release.yml must not contain real personal names (Gate-21 hygiene).

    Static text scan over the file. Comments referencing past bug reports must
    use neutral descriptors (e.g. 'external-tester bug 1'), not real names.
    """
    text = RELEASE_YML.read_text(encoding="utf-8").lower()
    assert "fabio" not in text, (
        "release.yml must not reference a real personal name (Gate-21); use a "
        "neutral descriptor like 'external-tester' instead"
    )
