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
