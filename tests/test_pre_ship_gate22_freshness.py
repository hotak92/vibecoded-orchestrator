# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Behaviour test for Gate 22 (tri-OS install smoke) in scripts/pre-ship-check.sh.

WHAT THIS PINS (v0.2.91 WP-G, decision #13)
-------------------------------------------
Gate 22 asks GitHub for the most-recent COMPLETED run of
install-smoke-tri-os.yml on main and decides whether a release tag may be
pushed.  Four outcomes, and the interesting one changed this cycle:

    latest run not success        -> FAIL   (unchanged)
    success, <= 24 h old, 5 legs  -> PASS    (unchanged)
    success, > 24 h old           -> FAIL    (was WARN before v0.2.91)
    no completed run at all       -> WARN    (unchanged; bootstrap case)

The stale-green branch was WARN while a daily cron kept a fresh green run
always available.  That cron is gone from both repos (public 2026-07-23,
private mirror 2026-08-23) and is NOT coming back, so a >24 h-old green is
now a likely state rather than a rare outage artifact — and a non-blocking
gate on a likely state is not a gate.  If someone flips this back to
`gate_warn`, this test goes red.

HOW IT TESTS THE REAL THING
---------------------------
It does not re-implement the gate.  It slices the actual Gate-22 block out
of scripts/pre-ship-check.sh (between its section banner and the next
section banner), drops it into a tiny harness that defines the three
gate_* reporters plus $REPO, and runs it with a `gh` PATH shim that returns
canned payloads.  So the assertions run against the shipped bytes; editing
the script edits what this test measures.

The shim emits what `gh ... --jq ...` would print AFTER its own jq filter,
because the shim replaces gh wholesale:
  * `gh run list  ... --jq '.[0] // empty'`  -> one JSON object, or nothing.
  * `gh run view  ... --jq '[...]'`          -> a JSON array of legs.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRE_SHIP = REPO_ROOT / "scripts" / "pre-ship-check.sh"

GATE_START = "# ── Gate 22:"
GATE_END = "# ── Section 4:"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="Gate 22 is a bash block; needs bash"
)


# ── harness plumbing ────────────────────────────────────────────────────────


def extract_gate22(script_text: str) -> str:
    """Return the Gate-22 block verbatim, sliced by its section banners."""
    lines = script_text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(GATE_START)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(GATE_END)]
    assert len(starts) == 1, f"expected exactly one {GATE_START!r} banner, got {len(starts)}"
    assert ends, f"section banner {GATE_END!r} not found — anchors drifted"
    end = next(i for i in ends if i > starts[0])
    return "\n".join(lines[starts[0] : end])


HARNESS_PREAMBLE = """\
set -uo pipefail
RED=''
GREEN=''
YELLOW=''
RESET=''
gate_pass() { printf 'PASS|%s|\\n' "$1"; }
gate_fail() { printf 'FAIL|%s|%s\\n' "$1" "${2:-}"; }
gate_warn() { printf 'WARN|%s|%s\\n' "$1" "${2:-}"; }
REPO="hotak92/vibecoded-orchestrator"
"""

GH_SHIM = """\
#!/usr/bin/env bash
# Stand-in for the GitHub CLI. Prints post-jq payloads from the fixture env.
for a in "$@"; do
  case "$a" in
    list) printf '%s' "${GH_FIXTURE_RUNLIST:-}"; exit 0 ;;
    view) printf '%s' "${GH_FIXTURE_JOBS:-[]}"; exit 0 ;;
  esac
done
echo "unexpected gh invocation: $*" >&2
exit 1
"""


def run_gate(tmp_path: Path, script_text: str, runlist: str, jobs: str) -> tuple[str, str]:
    """Run the Gate-22 block against stubbed gh output.

    Returns (verdict, detail) where verdict is PASS / WARN / FAIL.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(GH_SHIM, encoding="utf-8")
    gh.chmod(0o755)

    harness = tmp_path / "gate22-harness.sh"
    harness.write_text(HARNESS_PREAMBLE + extract_gate22(script_text) + "\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GH_FIXTURE_RUNLIST"] = runlist
    env["GH_FIXTURE_JOBS"] = jobs

    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )
    reported = [ln for ln in proc.stdout.splitlines() if "|" in ln]
    assert len(reported) == 1, (
        f"Gate 22 must report exactly one verdict, got {reported!r}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    verdict, _name, detail = reported[0].split("|", 2)
    return verdict, detail


# ── fixtures ────────────────────────────────────────────────────────────────


def _iso(hours_ago: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runlist(conclusion: str, hours_ago: float, run_id: int = 424242) -> str:
    return json.dumps(
        {"databaseId": run_id, "conclusion": conclusion, "createdAt": _iso(hours_ago)}
    )


def _legs(n_success: int, n_fail: int = 0) -> str:
    names = ["ubuntu-22.04", "ubuntu-24.04", "macos-14", "windows-latest", "fedora-40"]
    out = [
        {"name": f"install smoke ({names[i]})", "conclusion": "success"}
        for i in range(n_success)
    ]
    out += [
        {"name": f"install smoke ({names[n_success + i]})", "conclusion": "failure"}
        for i in range(n_fail)
    ]
    return json.dumps(out)


@pytest.fixture(scope="module")
def script_text() -> str:
    assert PRE_SHIP.is_file(), f"pre-ship-check.sh not found at {PRE_SHIP}"
    return PRE_SHIP.read_text(encoding="utf-8")


# ── the four branches ───────────────────────────────────────────────────────


def test_no_completed_run_warns(tmp_path: Path, script_text: str):
    """Bootstrap case: nothing has run yet. Must NOT block a tag."""
    verdict, detail = run_gate(tmp_path, script_text, runlist="", jobs="[]")
    assert verdict == "WARN", f"no-run case must stay WARN, got {verdict}: {detail}"


def test_fresh_green_passes(tmp_path: Path, script_text: str):
    verdict, detail = run_gate(
        tmp_path, script_text, runlist=_runlist("success", 2), jobs=_legs(5)
    )
    assert verdict == "PASS", f"fresh 5/5 green must PASS, got {verdict}: {detail}"


def test_stale_green_fails(tmp_path: Path, script_text: str):
    """v0.2.91 decision #13 — this branch was WARN and is now a hard FAIL."""
    verdict, detail = run_gate(
        tmp_path, script_text, runlist=_runlist("success", 72), jobs=_legs(5)
    )
    assert verdict == "FAIL", (
        f"green older than 24h must FAIL (decision #13), got {verdict}: {detail}"
    )
    # The remediation must survive the WARN->FAIL flip: dispatching a fresh
    # run is free on the public repo and is the only way to clear this gate.
    assert "gh workflow run install-smoke-tri-os.yml" in detail, (
        f"stale-green FAIL must keep the free dispatch hint, got: {detail}"
    )


def test_red_latest_run_fails(tmp_path: Path, script_text: str):
    verdict, detail = run_gate(
        tmp_path, script_text, runlist=_runlist("failure", 1), jobs=_legs(4, 1)
    )
    assert verdict == "FAIL", f"red latest run must FAIL, got {verdict}: {detail}"


# ── per-leg branches (unchanged by WP-G; pinned so the flip stays scoped) ───


def test_fresh_green_with_missing_leg_warns(tmp_path: Path, script_text: str):
    """Fewer than 5 legs is a shape anomaly, not a red install path: WARN."""
    verdict, detail = run_gate(
        tmp_path, script_text, runlist=_runlist("success", 2), jobs=_legs(4)
    )
    assert verdict == "WARN", f"<5 legs must stay WARN, got {verdict}: {detail}"


def test_aggregate_green_but_leg_failed_fails(tmp_path: Path, script_text: str):
    """Defence in depth: aggregate success + a red leg is still a FAIL."""
    verdict, detail = run_gate(
        tmp_path, script_text, runlist=_runlist("success", 2), jobs=_legs(4, 1)
    )
    assert verdict == "FAIL", f"a failed matrix leg must FAIL, got {verdict}: {detail}"


# ── the documented freshness sources must match what the gate can SEE ───────
#
# v0.2.91 fix-round MINOR-6(b). Both the workflow header and the Gate-22
# comment listed "release.yml's `tri-os-smoke` workflow_call on every tag" as a
# freshness source. That is false twice over:
#
#   1. a `uses:`-called reusable workflow executes inside the CALLER's run and
#      never gets its own entry, so the gate's `gh run list --workflow
#      install-smoke-tri-os.yml` query cannot see it at all;
#   2. this gate runs BEFORE the tag is pushed, so the release run does not
#      exist yet when the gate reads.
#
# A maintainer who believes the false claim concludes "the last release
# refreshed the window" and treats a stale-green FAIL as a gate bug.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "install-smoke-tri-os.yml"

_FALSE_FRESHNESS_CLAIMS = (
    "release-time workflow_call",
    "`tri-os-smoke` workflow_call on every tag",
    "release.yml's\n# `tri-os-smoke` workflow_call",
)


def _freshness_paragraph(text: str, anchor: str) -> str:
    idx = text.index(anchor)
    return text[idx : idx + 1400]


def test_gate_comment_does_not_claim_the_workflow_call_refreshes_it(
    script_text: str,
):
    para = _freshness_paragraph(script_text, "# Freshness sources")
    for claim in _FALSE_FRESHNESS_CLAIMS:
        assert claim not in para, (
            f"Gate 22 still names {claim!r} as a freshness source; a "
            "`uses:`-called workflow never appears in this gate's own query"
        )
    assert "push-to-main" in para and "manual dispatch" in para
    assert "CALLER's" in para, "the comment must say WHY, not just drop the claim"


def test_workflow_header_does_not_claim_the_workflow_call_refreshes_gate22():
    text = WORKFLOW.read_text(encoding="utf-8")
    para = _freshness_paragraph(text, "# NO cron")
    for claim in _FALSE_FRESHNESS_CLAIMS:
        assert claim not in para, (
            f"install-smoke-tri-os.yml still names {claim!r} as a Gate-22 "
            "freshness source"
        )
    assert "workflow_dispatch" in para or "manual `workflow_dispatch`" in para
    assert "push-to-main" in para
    # The workflow_call is still described — as a release BLOCKER, which it is.
    assert "BLOCKER" in para


def test_update_recovery_doc_does_not_claim_install_py_removes_leftovers():
    """v0.2.91 fix-round MINOR-6(a).

    UPDATE-RECOVERY.md claimed `python install.py --update` performs the manual
    recipe's steps 2–3. Step 3 deletes `*.new`, `vct-*.old-*` and the stale
    lock/result files — all UNTRACKED — and the repair leg never touches an
    untracked file BY DESIGN (`dist_binary_repair.dist_dirty_paths` drops `??`
    rows; a repair that deleted untracked paths could destroy a staged binary
    somebody is waiting on). Nothing in the product removes them.
    """
    doc = (REPO_ROOT / "docs" / "post-install" / "UPDATE-RECOVERY.md").read_text(
        encoding="utf-8"
    )
    assert "performs\nsteps 2–3 for you" not in doc
    assert "performs steps 2–3 for you" not in doc
    assert "Step 3 is still yours." in doc, (
        "the doc must say plainly that the leftover cleanup is manual"
    )
    # And the restore half it DOES do is described accurately.
    assert "git checkout HEAD --" in doc
