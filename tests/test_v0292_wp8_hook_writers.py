# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W7 — the shipped hooks actually write to the new metrics home.

The path change is only real if the hooks that fire on a user's machine put
rows in the new place. These tests EXECUTE the shipped `.sh` hooks against a
tmp home and look at the filesystem, rather than grepping their text — a
source-level assertion would have passed a hook whose helper call was
mis-wired.

Two halves per hook, as everywhere in this package:

* **the ACT** — the row lands in `$VCT_STATE_DIR/metrics`;
* **the LEAVE-ALONE** — nothing appears in, or changes inside, the frozen
  `$VCT_CLAUDE_DIR/metrics` archive.

Plus the gate: while a verified copy is still owed, writers deliberately stay
on the archive (the user's amendment — "writers switch to the new path only
after a verified copy"), and once the sentinel exists they move.

`.ps1` siblings are covered by
`tests/test_v0292_wp8_metrics_shell_parity.py`, which executes BOTH flavours
of the shared resolver against each other and against the Python SSOT. That
is where the tri-OS shape lives; this file is the Linux/macOS runtime proof
that the resolver is actually WIRED into each hook.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / "templates" / "hooks"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the .sh hook bodies are POSIX-only; the .ps1 rule is pinned by "
           "test_v0292_wp8_metrics_shell_parity.py",
)


@pytest.fixture
def home(tmp_path):
    """A fake machine: `$HOME`, `$VCT_STATE_DIR` and `$VCT_CLAUDE_DIR`."""
    root = tmp_path / "fake-home"
    (root / ".claude").mkdir(parents=True)
    (root / ".vct").mkdir(parents=True)
    return root


def _env(home: Path, project: Path) -> dict:
    """Hook env pinned into `home`, with the AMBIENT VCO venv removed.

    `$VCT_INSTALL_ROOT` / `$VCT_VENV` are the first tiers of
    `_lib/resolve-vco-venv.sh`, and on a maintainer's box they point at their
    OWN VCO clone — whose `vco_lib` is a different copy from the one under
    test. Leaving them set makes these tests pass or fail on a property of the
    developer's machine (the §11 "ambient python3 validates the WRONG repo"
    trap, applied to hooks). Dropped by default; the one test that WANTS the
    migration to run supplies a shim venv pinned to THIS checkout.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["VCT_STATE_DIR"] = str(home / ".vct")
    env["VCT_CLAUDE_DIR"] = str(home / ".claude")
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_INSTALL_ROOT", None)
    env.pop("VCT_VENV", None)
    return env


def _shim_venv(tmp_path: Path) -> Path:
    """A `$VCT_VENV`-shaped directory whose python imports THIS checkout.

    `resolve-vco-venv.sh` tier 1 looks for `<VCT_VENV>/bin/python`, so a two-
    line wrapper is enough and it pins the migration under test to the repo
    under test rather than to whatever clone the developer has installed.
    """
    venv = tmp_path / "shim-venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    python = venv / "bin" / "python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        f'PYTHONPATH="{_REPO_ROOT}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return venv


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude" / "state").mkdir(parents=True)
    return project


def _run(hook: str, home: Path, project: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_HOOKS / hook)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=_env(home, project), timeout=60,
    )


def _new(home: Path, name: str) -> Path:
    return home / ".vct" / "metrics" / name


def _archive(home: Path, name: str) -> Path:
    return home / ".claude" / "metrics" / name


def _digests(directory: Path) -> "dict[str, str]":
    if not directory.is_dir():
        return {}
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir()) if p.is_file()
    }


# --------------------------------------------------------------------------- #
# cost-tracker (Stop)
# --------------------------------------------------------------------------- #


_STOP_PAYLOAD = {
    "session_id": "sess-1",
    "message": {
        "model": "claude-sonnet-4-6",
        "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_input_tokens": 5,
        },
    },
}


def test_cost_tracker_writes_to_the_new_home__act(home, tmp_path):
    project = _project(tmp_path)
    result = _run("cost-tracker.sh", home, project, _STOP_PAYLOAD)

    assert result.returncode == 0, result.stderr
    rows = [
        json.loads(ln)
        for ln in _new(home, "costs.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-1"
    assert rows[0]["input_tokens"] == 10


def test_cost_tracker_writes_nothing_under_the_claude_dir__leave_alone(
    home, tmp_path
):
    project = _project(tmp_path)
    before = _digests(home / ".claude" / "metrics")

    _run("cost-tracker.sh", home, project, _STOP_PAYLOAD)

    assert _digests(home / ".claude" / "metrics") == before
    assert not _archive(home, "costs.jsonl").exists(), (
        "the directive: VCO writes nothing under ~/.claude that the harness "
        "did not ask for"
    )


def test_cost_tracker_stays_on_the_archive_until_the_copy_is_verified(
    home, tmp_path
):
    """The user's amendment, at runtime.

    An archive holding rows and no sentinel means the copy is still owed, so
    the writer keeps appending where that machine's history already is.
    Nothing is stranded and nothing is double-counted.
    """
    project = _project(tmp_path)
    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    _archive(home, "costs.jsonl").write_text('{"pre":1}\n', encoding="utf-8")

    result = _run("cost-tracker.sh", home, project, _STOP_PAYLOAD)

    assert result.returncode == 0, result.stderr
    assert not _new(home, "costs.jsonl").exists()
    assert len(_archive(home, "costs.jsonl").read_text().splitlines()) == 2


def test_cost_tracker_moves_to_the_new_home_once_the_copy_is_verified(
    home, tmp_path
):
    """The other half of the same decision — the act, after the gate opens."""
    project = _project(tmp_path)
    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    _archive(home, "costs.jsonl").write_text('{"pre":1}\n', encoding="utf-8")

    from vco_lib.metrics_migration import migrate_metrics

    migration = migrate_metrics(
        home / ".claude" / "metrics", home / ".vct" / "metrics"
    )
    assert migration.ok, migration.to_dict()
    archive_after_copy = _digests(home / ".claude" / "metrics")

    result = _run("cost-tracker.sh", home, project, _STOP_PAYLOAD)

    assert result.returncode == 0, result.stderr
    rows = _new(home, "costs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2, "the copied row plus the new one"
    assert _digests(home / ".claude" / "metrics") == archive_after_copy, (
        "the archive is frozen once copied"
    )


def test_cost_tracker_ignores_a_zero_token_payload(home, tmp_path):
    """Pre-existing behaviour preserved across the move."""
    project = _project(tmp_path)
    payload = {
        "session_id": "s",
        "message": {"model": "m", "usage": {"input_tokens": 0, "output_tokens": 0}},
    }
    result = _run("cost-tracker.sh", home, project, payload)
    assert result.returncode == 0
    assert not _new(home, "costs.jsonl").exists()


# --------------------------------------------------------------------------- #
# stop-failure-notify (StopFailure)
# --------------------------------------------------------------------------- #


def test_stop_failure_notify_writes_to_the_new_home__act(home, tmp_path):
    project = _project(tmp_path)
    payload = {
        "session_id": "sess-2",
        "error": {"type": "rate_limit", "message": "slow down"},
    }
    result = _run("stop-failure-notify.sh", home, project, payload)

    assert result.returncode == 0, result.stderr
    body = _new(home, "failures.jsonl").read_text(encoding="utf-8")
    assert "rate_limit" in body


def test_stop_failure_notify_leaves_the_archive_alone__leave_alone(home, tmp_path):
    project = _project(tmp_path)
    before = _digests(home / ".claude" / "metrics")

    _run("stop-failure-notify.sh", home, project,
         {"session_id": "s", "error": {"type": "t", "message": "m"}})

    assert _digests(home / ".claude" / "metrics") == before
    assert not _archive(home, "failures.jsonl").exists()


# --------------------------------------------------------------------------- #
# post-compact (PostCompact)
# --------------------------------------------------------------------------- #


def test_post_compact_writes_to_the_new_home__act(home, tmp_path):
    project = _project(tmp_path)
    result = _run("post-compact.sh", home, project,
                  {"trigger": "manual", "session_id": "sess-3"})

    assert result.returncode == 0, result.stderr
    body = _new(home, "compactions.jsonl").read_text(encoding="utf-8")
    assert '"trigger":"manual"' in body


def test_post_compact_leaves_the_archive_alone__leave_alone(home, tmp_path):
    project = _project(tmp_path)
    before = _digests(home / ".claude" / "metrics")

    _run("post-compact.sh", home, project, {"trigger": "auto"})

    assert _digests(home / ".claude" / "metrics") == before
    assert not _archive(home, "compactions.jsonl").exists()


# --------------------------------------------------------------------------- #
# kg-update-nudge (SessionStart / UserPromptSubmit / PostToolUse)
# --------------------------------------------------------------------------- #


_KG_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "session_id": "sess-4",
    "tool_name": "Edit",
}


def test_kg_update_nudge_writes_its_counter_to_the_new_home__act(home, tmp_path):
    project = _project(tmp_path)
    (project / "knowledge").mkdir(parents=True, exist_ok=True)
    node = project / "knowledge" / "n.md"
    node.write_text("# n\n", encoding="utf-8")
    payload = dict(_KG_PAYLOAD, tool_input={"file_path": str(node)})

    result = _run("kg-update-nudge.sh", home, project, payload)

    assert result.returncode == 0, result.stderr
    assert _new(home, "kg_update_tokens.jsonl").is_file()
    assert not _archive(home, "kg_update_tokens.jsonl").exists()


def test_kg_update_nudge_seeds_its_counter_from_the_archive(home, tmp_path):
    """Counter CONTINUITY across the move — the baseline is not reset.

    A migrated machine whose new home has no counter yet (the archive was
    copied before that stream existed, or the state root was re-pointed) must
    pick the baseline up from the archive rather than start at zero and fire
    the nudge late. Read-only against the archive.

    The payload is a NON-knowledge PostToolUse on purpose: a knowledge-file
    edit is a deliberate baseline RESET (the hook's own semantics), which
    would mask whether the seed arrived.
    """
    project = _project(tmp_path)

    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    seed = {
        "session_id": "sess-4",
        "baseline": 4242,
        "last_seen_total": 4242,
        "fired_once": False,
        "metric_version": "v10",
    }
    _archive(home, "kg_update_tokens.jsonl").write_text(
        json.dumps(seed) + "\n", encoding="utf-8"
    )
    # Copy verified, so writers are on the new home; the counter itself was
    # not carried across.
    (home / ".vct" / "metrics").mkdir(parents=True, exist_ok=True)
    (home / ".vct" / "metrics" / ".migrated-from-claude.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    archive_before = _digests(home / ".claude" / "metrics")

    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-4",
        "tool_name": "Read",
        "tool_input": {"file_path": str(project / "src.py")},
    }
    result = _run("kg-update-nudge.sh", home, project, payload)

    assert result.returncode == 0, result.stderr
    rows = [
        json.loads(ln)
        for ln in _new(home, "kg_update_tokens.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    mine = [r for r in rows if r.get("session_id") == "sess-4"]
    assert mine, rows
    assert mine[-1].get("baseline") == 4242, (
        "the pre-move baseline must survive; restarting at zero would delay "
        "the nudge by a whole threshold"
    )
    assert _digests(home / ".claude" / "metrics") == archive_before, (
        "seeding READS the archive; it must never write it"
    )


# --------------------------------------------------------------------------- #
# embedding-failures-surface (SessionStart) — reader + migration trigger
# --------------------------------------------------------------------------- #


def test_embedding_surface_is_silent_and_writes_nothing_with_no_hint(
    home, tmp_path
):
    """The zero-output property survives the added migration trigger."""
    project = _project(tmp_path)
    before = _digests(home / ".claude" / "metrics")

    result = _run("embedding-failures-surface.sh", home, project, {})

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert _digests(home / ".claude" / "metrics") == before


def _hint(project: Path) -> None:
    (project / ".claude" / "context").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "context" / "EMBEDDING_FAILURES.md").write_text(
        "backend unreachable\n", encoding="utf-8"
    )


def test_embedding_surface_points_at_the_archive_when_the_copy_cannot_run(
    home, tmp_path
):
    """Degraded machine: no resolvable VCO venv, so no migration is possible.

    Only the ARCHIVE has the stream, and the hint must name IT — pointing at
    the new home would be a printed path that does not exist, and a printed
    path is shipped code.
    """
    project = _project(tmp_path)
    _hint(project)
    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    _archive(home, "embedding_failures.jsonl").write_text(
        '{"m":1}\n', encoding="utf-8"
    )

    result = _run("embedding-failures-surface.sh", home, project, {})

    assert result.returncode == 0, result.stderr
    assert str(_archive(home, "embedding_failures.jsonl")) in result.stdout
    assert "backend unreachable" in result.stdout
    assert not (home / ".vct" / "metrics" / "embedding_failures.jsonl").exists()


def test_embedding_surface_runs_the_copy_once_on_session_start__act(
    home, tmp_path
):
    """The DELIVERY mechanism, end to end.

    `install.py` runs the migration too, but an installed project must not
    have to wait for the next orchestrator update to get its history copied —
    so this SessionStart hook triggers it. Here the whole chain runs for real:
    hook -> `_lib/metrics-dir.sh` -> `_lib/resolve-vco-venv.sh` ->
    `python -m vco_lib.metrics_migration`.
    """
    project = _project(tmp_path)
    _hint(project)
    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    _archive(home, "embedding_failures.jsonl").write_text(
        '{"m":1}\n', encoding="utf-8"
    )
    _archive(home, "costs.jsonl").write_text('{"c":1}\n', encoding="utf-8")
    archive_before = _digests(home / ".claude" / "metrics")

    env = _env(home, project)
    env["VCT_VENV"] = str(_shim_venv(tmp_path))
    result = subprocess.run(
        ["bash", str(_HOOKS / "embedding-failures-surface.sh")],
        input="{}", capture_output=True, text=True, env=env, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    # ACT: the whole archive came across, verified, with the sentinel written.
    assert _new(home, "costs.jsonl").read_text(encoding="utf-8") == '{"c":1}\n'
    assert _new(home, "embedding_failures.jsonl").is_file()
    assert (home / ".vct" / "metrics" / ".migrated-from-claude.json").is_file()
    # ...and the hint now names the new home, because that is where the rows
    # the user would open now live.
    assert str(_new(home, "embedding_failures.jsonl")) in result.stdout
    # LEAVE-ALONE: the archive is byte-identical.
    assert _digests(home / ".claude" / "metrics") == archive_before


def test_embedding_surface_copy_is_idempotent_across_sessions__leave_alone(
    home, tmp_path
):
    """Every subsequent SessionStart must change nothing at all."""
    project = _project(tmp_path)
    _hint(project)
    (home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    _archive(home, "costs.jsonl").write_text('{"c":1}\n', encoding="utf-8")

    env = _env(home, project)
    env["VCT_VENV"] = str(_shim_venv(tmp_path))
    for _ in range(2):
        proc = subprocess.run(
            ["bash", str(_HOOKS / "embedding-failures-surface.sh")],
            input="{}", capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr

    assert _new(home, "costs.jsonl").read_text(encoding="utf-8") == '{"c":1}\n', (
        "a second session must not double the copied rows"
    )
    assert _archive(home, "costs.jsonl").read_text(encoding="utf-8") == '{"c":1}\n'


def test_embedding_surface_prefers_the_new_home_when_it_has_the_rows(
    home, tmp_path
):
    project = _project(tmp_path)
    (project / ".claude" / "context").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "context" / "EMBEDDING_FAILURES.md").write_text(
        "backend unreachable\n", encoding="utf-8"
    )
    (home / ".vct" / "metrics").mkdir(parents=True, exist_ok=True)
    _new(home, "embedding_failures.jsonl").write_text('{"m":1}\n', encoding="utf-8")

    result = _run("embedding-failures-surface.sh", home, project, {})

    assert str(_new(home, "embedding_failures.jsonl")) in result.stdout


# --------------------------------------------------------------------------- #
# the partial-install case: a hook without its helper must no-op, not guess
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hook", ["cost-tracker.sh", "post-compact.sh", "stop-failure-notify.sh",
             "kg-update-nudge.sh", "embedding-failures-surface.sh"]
)
def test_a_hook_without_the_helper_exits_cleanly_and_writes_nowhere(
    hook, home, tmp_path
):
    """`_lib/metrics-dir.sh` missing (partial install / hand-copied hook).

    Same discipline as the missing-Python case these hooks already had: exit
    0, write nothing. A hook that guessed a path here would resurrect the
    inline reconstruction the helper exists to delete.
    """
    import shutil

    staged = tmp_path / "hooks"
    shutil.copytree(_HOOKS, staged)
    (staged / "_lib" / "metrics-dir.sh").unlink()
    project = _project(tmp_path)

    result = subprocess.run(
        ["bash", str(staged / hook)],
        input=json.dumps(_STOP_PAYLOAD),
        capture_output=True, text=True, env=_env(home, project), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".vct" / "metrics").exists() or not list(
        (home / ".vct" / "metrics").glob("*.jsonl")
    )
    assert not (home / ".claude" / "metrics").exists() or not list(
        (home / ".claude" / "metrics").glob("*.jsonl")
    )
