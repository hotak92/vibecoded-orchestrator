# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W7 — `cost-summary.py` finds the data wherever it now lives.

The reader is the surface a user actually looks at, so a metrics move that
left it reading one directory would be visible as a wrong number — the worst
failure mode for a cost report. This file pins:

* it reads the NEW home (`$VCT_STATE_DIR/metrics`);
* it reads the pre-v0.2.92 ARCHIVE (`$VCT_CLAUDE_DIR/metrics`) too;
* in the MIXED state it merges them and counts a COPIED row once — the
  migration duplicates history by design, so summing would inflate the total;
* it does NOT collapse genuine duplicates inside one file (the regression the
  pre-existing `test_cost_summary_py.py::test_session_filter` caught while
  this package was being written — kept here as a named property so it cannot
  come back);
* the "no data yet" message names the WRITE target, not a path VCO no longer
  writes to;
* its inline directory rule agrees with `vco_lib.paths` — the reader ships
  into every project's `.claude/scripts/` and must run on a bare stdlib
  interpreter, so it cannot import `vco_lib`; this is the enforcing half of
  that documented class-C mirror.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "templates" / "scripts" / "cost-summary.py"


def _record(session: str = "s", cost: float = 0.5, model: str = "claude-sonnet-4-6") -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session,
        "model": model,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "auth_mode": "api",
        "cost_usd": cost,
    }


@pytest.fixture
def homes(tmp_path):
    """`(archive_metrics, new_home_metrics, env)` — both roots inside tmp."""
    claude = tmp_path / "claude_home"
    state = tmp_path / "vct_root"
    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(claude)
    env["VCT_STATE_DIR"] = str(state)
    env.pop("PYTHONPATH", None)  # the script must need nothing on the path
    return claude / "metrics", state / "metrics", env


def _write(path: Path, records: "list[dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def _records_reported(stdout: str) -> int:
    for line in stdout.splitlines():
        if line.startswith("Records: "):
            return int(line.split()[1])
    raise AssertionError(f"no Records line in:\n{stdout}")


# --------------------------------------------------------------------------- #
# each location on its own
# --------------------------------------------------------------------------- #


def test_reads_the_new_home(homes):
    archive, new_home, env = homes
    _write(new_home / "costs.jsonl", [_record(), _record()])

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert _records_reported(proc.stdout) == 2


def test_reads_the_frozen_archive_when_that_is_where_the_data_is(homes):
    """A machine that has not migrated yet still gets its real total."""
    archive, new_home, env = homes
    _write(archive / "costs.jsonl", [_record(), _record(), _record()])

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert _records_reported(proc.stdout) == 3


# --------------------------------------------------------------------------- #
# the MIXED state — the case the whole reader change exists for
# --------------------------------------------------------------------------- #


def test_a_copied_row_is_counted_once_not_twice(homes):
    """After the migration BOTH directories hold the same rows, by design."""
    archive, new_home, env = homes
    rows = [_record("a"), _record("b")]
    _write(archive / "costs.jsonl", rows)
    _write(new_home / "costs.jsonl", rows)  # what the COPY leaves behind

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert _records_reported(proc.stdout) == 2, (
        "a migrated row exists in both directories; counting it twice would "
        "inflate every total this tool prints"
    )


def test_a_user_modified_hook_still_writing_to_the_archive_is_included(homes):
    """`templates/**` is bundled: an edited `cost-tracker.sh` is PRESERVED on
    update (`bundle_user_modified_preserved`) and goes on appending to the
    archive while everything else writes to the new home. That user's history
    is genuinely split, indefinitely, and the total must still be right.
    """
    archive, new_home, env = homes
    migrated = [_record("old")]
    _write(new_home / "costs.jsonl", migrated + [_record("new-hook")])
    _write(archive / "costs.jsonl", migrated + [_record("stale-hook")])

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert _records_reported(proc.stdout) == 3, (
        "shared row once + one row unique to each side"
    )


def test_the_split_is_disclosed_not_silently_merged(homes):
    archive, new_home, env = homes
    _write(archive / "costs.jsonl", [_record("a")])
    _write(new_home / "costs.jsonl", [_record("b")])

    proc = _run(env)

    assert "Note: merging" in proc.stdout
    assert str(new_home / "costs.jsonl") in proc.stdout
    assert str(archive / "costs.jsonl") in proc.stdout


def test_no_note_when_only_one_location_has_data(homes):
    archive, new_home, env = homes
    _write(new_home / "costs.jsonl", [_record("a")])

    proc = _run(env)

    assert "Note: merging" not in proc.stdout


# --------------------------------------------------------------------------- #
# the regression the existing suite caught — kept as a named property
# --------------------------------------------------------------------------- #


def test_identical_rows_inside_one_file_are_both_counted(homes):
    """Dedup is ACROSS files, never within one.

    Two responses can produce byte-identical rows (same second, same session,
    same tokens). A `set`-based merge collapsed them and under-reported spend;
    `test_cost_summary_py.py::test_session_filter` caught it. The merge is a
    max-multiplicity union for exactly this reason.
    """
    archive, new_home, env = homes
    row = _record("beta")
    _write(new_home / "costs.jsonl", [row, row, row])

    proc = _run(env)

    assert _records_reported(proc.stdout) == 3


def test_multiplicity_is_preserved_across_the_merge(homes):
    """Three copies in one place and two in the other -> three, not five."""
    archive, new_home, env = homes
    row = _record("beta")
    _write(new_home / "costs.jsonl", [row, row, row])
    _write(archive / "costs.jsonl", [row, row])

    proc = _run(env)

    assert _records_reported(proc.stdout) == 3


# --------------------------------------------------------------------------- #
# empty / degraded states
# --------------------------------------------------------------------------- #


def test_no_data_anywhere_names_the_write_target(homes):
    """A printed path is shipped code: it must name where data WILL land."""
    archive, new_home, env = homes

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "No cost data yet" in proc.stdout
    assert str(new_home / "costs.jsonl") in proc.stdout
    assert str(archive) not in proc.stdout, (
        "pointing a user at the frozen archive would be an instruction that "
        "does not help — nothing writes there any more"
    )


def test_explicit_costs_file_still_wins(homes, tmp_path):
    """The `--costs-file` test hook bypasses discovery entirely."""
    archive, new_home, env = homes
    _write(new_home / "costs.jsonl", [_record("ignored")])
    explicit = tmp_path / "explicit.jsonl"
    _write(explicit, [_record("x"), _record("y")])

    proc = _run(env, "--costs-file", str(explicit))

    assert _records_reported(proc.stdout) == 2
    assert "Note: merging" not in proc.stdout


def test_a_torn_line_does_not_abort_the_report(homes):
    archive, new_home, env = homes
    new_home.mkdir(parents=True, exist_ok=True)
    with open(new_home / "costs.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_record("a")) + "\n")
        fh.write('{"partial": tru\n')
        fh.write(json.dumps(_record("b")) + "\n")

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert _records_reported(proc.stdout) == 2


# --------------------------------------------------------------------------- #
# the class-C mirror, enforced
# --------------------------------------------------------------------------- #


def test_the_inline_rule_agrees_with_vco_lib_paths(tmp_path, monkeypatch):
    """The script's `metrics_dirs()` == `vco_lib.paths.metrics_read_dirs()`.

    The script cannot import `vco_lib` (stdlib-only, ships into every
    project's `.claude/scripts/`), so the rule is mirrored. This is the test
    that makes the mirror legal: change one side and it reds.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_cost_summary", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from vco_lib.paths import metrics_read_dirs

    for claude, state in (
        (tmp_path / "c1", tmp_path / "s1"),
        (tmp_path / "other" / "claude", tmp_path / "other" / "vct"),
    ):
        monkeypatch.setenv("VCT_CLAUDE_DIR", str(claude))
        monkeypatch.setenv("VCT_STATE_DIR", str(state))
        assert tuple(module.metrics_dirs()) == metrics_read_dirs()

    # And with no overrides at all, both fall back to the same home-relative
    # shape (compared as suffixes so the assertion says nothing about which
    # home this machine has).
    monkeypatch.delenv("VCT_CLAUDE_DIR", raising=False)
    monkeypatch.delenv("VCT_STATE_DIR", raising=False)
    inline = [Path(p).parts[-2:] for p in module.metrics_dirs()]
    assert inline == [(".vct", "metrics"), (".claude", "metrics")]


def test_the_script_imports_nothing_outside_the_stdlib():
    """It ships into projects that have no VCO packages installed."""
    import ast

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "vco_lib" not in imported, (
        "cost-summary.py must stay stdlib-only; the mirror test above is what "
        "keeps its inline path rule honest instead"
    )
    assert imported <= {
        "argparse", "json", "os", "sys", "collections", "datetime", "pathlib",
        "__future__",
    }, imported
