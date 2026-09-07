# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W7 — the metrics COPY: both sides, all three axes.

`vco_lib.metrics_migration` carries `~/.claude/metrics/*.jsonl` into
`~/.vct/metrics/`. The user's binding amendment is **COPY, NEVER MOVE**, so
every test here comes in two halves:

* **the ACT** — the rows arrive, and the migration's own line-count/byte/
  containment verification passes;
* **the LEAVE-ALONE** — the originals are byte-identical afterwards, proved
  with a sha256 taken before and after, never by reading the code.

Test names below say which half they pin. The leave-alone half is the one that
must never be "fixed" by relaxing it: the whole point of the design is that a
user can still delete the archive themselves, later, having lost nothing.

Isolation: every test pins `$VCT_CLAUDE_DIR` and `$VCT_STATE_DIR` into
`tmp_path`. The suite's conftest already redirects both away from the real
user directories and installs an audit hook that REDS any test touching the
real `~/.claude`; these pins are the per-test layer on top, so a fixture can
never read the maintainer's real telemetry even if the conftest guard were
removed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib import metrics_migration as mm
from vco_lib.paths import legacy_claude_metrics_dir, vct_metrics_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """`(archive, new_home)` with both env roots pinned inside tmp_path."""
    claude = tmp_path / "claude_home"
    state = tmp_path / "vct_root"
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(claude))
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    return claude / "metrics", state / "metrics"


def _digests(directory: Path) -> "dict[str, str]":
    """sha256 of every file in `directory`, keyed by name. {} when absent."""
    if not directory.is_dir():
        return {}
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


def _seed(archive: Path, **files: str) -> "dict[str, str]":
    archive.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (archive / name.replace("__", ".")).write_text(body, encoding="utf-8")
    return _digests(archive)


def _rows(path: Path) -> "list[str]":
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln]


def _called_names(module_path: Path) -> "set[str]":
    """Dotted names of every function CALLED in a module (AST, not text).

    `foo.bar(...)` -> "foo.bar" and "bar"; `bar(...)` -> "bar". Comments and
    docstrings contribute nothing, which is the point: a module may DESCRIBE a
    destructive operation it deliberately does not perform.
    """
    import ast

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
    return names


# --------------------------------------------------------------------------- #
# axis 1 — FRESH install: a clean no-op, not an error
# --------------------------------------------------------------------------- #


def test_fresh_install_is_a_clean_noop(homes):
    """No archive at all -> `no_source`, exit 0, and NOTHING is created."""
    archive, new_home = homes
    assert not archive.exists()

    result = mm.migrate_metrics()

    assert result.status == "no_source"
    assert result.ok is True
    assert result.files == []
    assert not new_home.exists(), (
        "a fresh install must not have a metrics directory conjured for it by "
        "a migration that had nothing to do"
    )
    assert not archive.exists(), "the migration must never create the archive"


def test_fresh_install_cli_exits_zero_and_says_so(homes):
    """The CLI's fresh-install path is a success, not a silent failure."""
    proc = _run_cli(homes)
    assert proc.returncode == 0, proc.stderr
    assert "nothing to do" in proc.stdout


def _run_cli(homes, *args: str) -> subprocess.CompletedProcess:
    archive, new_home = homes
    import os

    env = dict(os.environ)
    env["VCT_CLAUDE_DIR"] = str(archive.parent)
    env["VCT_STATE_DIR"] = str(new_home.parent)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.metrics_migration", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


# --------------------------------------------------------------------------- #
# axis 2 — UPDATE: the copy happens (ACT) and the originals survive (LEAVE-ALONE)
# --------------------------------------------------------------------------- #


def test_update_copies_every_stream__act(homes):
    archive, new_home = homes
    _seed(
        archive,
        costs__jsonl='{"n":1}\n{"n":2}\n',
        failures__jsonl='{"e":"boom"}\n',
        kg_update_tokens__jsonl='{"session_id":"s"}\n',
    )

    result = mm.migrate_metrics()

    assert result.status == "migrated", result.to_dict()
    assert result.ok
    assert {f.name for f in result.files} == {
        "costs.jsonl", "failures.jsonl", "kg_update_tokens.jsonl"
    }
    assert all(f.verified for f in result.files)
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":2}']
    assert _rows(new_home / "failures.jsonl") == ['{"e":"boom"}']


def test_update_leaves_the_originals_byte_identical__leave_alone(homes):
    """The LEAVE-ALONE half, proved with sha256 — not by inspection."""
    archive, _ = homes
    before = _seed(
        archive,
        costs__jsonl='{"n":1}\n{"n":2}\n',
        failures__jsonl='{"e":"boom"}\n',
    )

    mm.migrate_metrics()

    assert _digests(archive) == before, (
        "the archive must be byte-identical after a migration; it is a frozen "
        "archive the user alone may delete"
    )


def test_the_migration_reports_the_leave_alone_itself__leave_alone(homes):
    """The property is MEASURED by the run, not only by this test.

    Every record carries `source_unchanged`, re-hashed after the merge, so a
    future change that started writing to the source would fail the run rather
    than waiting for a test to notice.
    """
    archive, _ = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    result = mm.migrate_metrics()

    assert result.originals_untouched is True
    assert all(f.source_unchanged for f in result.files)


def test_nothing_deletes_the_archive__leave_alone(homes):
    """No entry point removes an original. Not the API, not the CLI."""
    archive, _ = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    mm.migrate_metrics()
    mm.ensure_metrics_migrated()
    proc = _run_cli(homes)
    assert proc.returncode == 0, proc.stderr

    assert (archive / "costs.jsonl").is_file(), (
        "the old files are deleted by NOBODY — user-initiated cleanup only"
    )
    # And there is no code path that COULD do it. Asserted against the module's
    # AST, not its text: prose that MENTIONS `--cleanup` (the docstring says
    # there deliberately is not one) must not fail, while a real call must.
    # A behavioural assertion alone would pass a module that grew a delete
    # nobody happened to reach in this test.
    called = _called_names(_REPO_ROOT / "vco_lib" / "metrics_migration.py")
    forbidden = {
        "os.remove", "os.unlink", "unlink", "shutil.move", "os.rename",
        "os.replace", "shutil.rmtree", "os.rmdir", "os.truncate", "truncate",
    }
    assert not (called & forbidden), (
        f"metrics_migration.py calls {sorted(called & forbidden)} — the "
        f"archive is frozen and nothing here may remove, move or truncate a "
        f"file. (Atomic writes of the SENTINEL go through vco_lib.atomic.)"
    )
    assert "--cleanup" not in proc.stdout


def test_only_jsonl_streams_are_copied(homes):
    """A user's own file in the archive is not hoovered into VCO's state root."""
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')
    (archive / "my-notes.txt").write_text("private\n", encoding="utf-8")

    mm.migrate_metrics()

    assert (new_home / "costs.jsonl").is_file()
    assert not (new_home / "my-notes.txt").exists()


# --------------------------------------------------------------------------- #
# axis 3 — ALREADY-DAMAGED: interrupted, partial, re-run
# --------------------------------------------------------------------------- #


def test_running_twice_changes_nothing_the_second_time(homes):
    """Idempotence is the property that makes an interrupted run safe."""
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n{"n":2}\n', failures__jsonl='{"e":1}\n')

    first = mm.migrate_metrics()
    after_first = _digests(new_home)
    archive_after_first = _digests(archive)

    second = mm.migrate_metrics()

    assert first.status == "migrated"
    assert second.status == "already_current"
    assert second.appended_lines == 0
    assert {
        k: v for k, v in _digests(new_home).items() if k != mm.SENTINEL_NAME
    } == {k: v for k, v in after_first.items() if k != mm.SENTINEL_NAME}, (
        "a second run must not change a single destination byte"
    )
    assert _digests(archive) == archive_after_first


def test_interrupted_after_one_file_finishes_the_rest(homes):
    """A previous run copied some files and stopped — no sentinel was written.

    The next run must complete the job without duplicating what landed.
    """
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n{"n":2}\n', failures__jsonl='{"e":1}\n')
    # Simulate the crash: costs.jsonl fully copied, no sentinel, failures absent.
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / "costs.jsonl").write_text('{"n":1}\n{"n":2}\n', encoding="utf-8")
    assert not (new_home / mm.SENTINEL_NAME).exists()

    result = mm.migrate_metrics()

    assert result.ok, result.to_dict()
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":2}'], (
        "the already-copied file must not be doubled"
    )
    assert _rows(new_home / "failures.jsonl") == ['{"e":1}']
    assert (new_home / mm.SENTINEL_NAME).is_file()


def test_interrupted_mid_file_appends_only_the_missing_tail(homes):
    """A half-written destination gets exactly its missing rows, once."""
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n{"n":2}\n{"n":3}\n')
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / "costs.jsonl").write_text('{"n":1}\n', encoding="utf-8")

    result = mm.migrate_metrics()

    assert result.ok
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":2}', '{"n":3}']


def test_a_torn_last_line_is_repaired_before_appending(homes):
    """A destination whose last append was cut short must not glue rows."""
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":2}\n')
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / "costs.jsonl").write_text('{"n":1}', encoding="utf-8")  # no \n

    result = mm.migrate_metrics()

    assert result.ok, result.to_dict()
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":2}']


def test_new_rows_written_after_the_copy_are_preserved(homes):
    """The destination is APPENDED to, never rewritten from the source.

    A machine that migrated, then wrote new rows, then re-ran the migration
    (a bundle update, say) must keep the new rows.
    """
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"old":1}\n')
    mm.migrate_metrics()
    with open(new_home / "costs.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"new":1}\n')

    mm.migrate_metrics()

    assert _rows(new_home / "costs.jsonl") == ['{"old":1}', '{"new":1}']


def test_a_stale_hook_appending_to_the_archive_is_picked_up_later(homes):
    """The user-modified-hook case: the archive KEEPS growing after the copy.

    `templates/**` files are bundled, so a user who edited `cost-tracker.sh`
    has it PRESERVED by `install-bundle --update`
    (`bundle_user_modified_preserved`) and it goes on appending to the
    archive. A later migration run must carry the new rows across and only
    those.
    """
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')
    mm.migrate_metrics()
    with open(archive / "costs.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"n":2}\n')  # the preserved old hook, still writing here

    result = mm.migrate_metrics()

    assert result.status == "migrated"
    assert result.appended_lines == 1
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":2}']


# --------------------------------------------------------------------------- #
# dedup semantics — by LINE IDENTITY, and multiplicity-faithful
# --------------------------------------------------------------------------- #


def test_duplicate_rows_in_the_source_survive_the_copy(homes):
    """Dedup is a MULTISET difference, so genuine repeats are not lost.

    Two identical telemetry rows are legal (same second, same session). A set
    difference would have silently dropped one, buying idempotence with data
    loss — which the amendment's "leave everything intact" spirit forbids.
    """
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n{"n":1}\n{"n":1}\n')

    mm.migrate_metrics()

    assert _rows(new_home / "costs.jsonl") == ['{"n":1}'] * 3


def test_a_row_already_in_the_destination_is_not_appended_again(homes):
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n{"n":1}\n')
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / "costs.jsonl").write_text('{"n":1}\n', encoding="utf-8")

    result = mm.migrate_metrics()

    assert result.appended_lines == 1  # the SECOND copy only
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}', '{"n":1}']


# --------------------------------------------------------------------------- #
# the sentinel — the record that gates the writers
# --------------------------------------------------------------------------- #


def test_sentinel_is_written_only_after_every_file_verified(homes):
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    result = mm.migrate_metrics()

    assert result.sentinel_written
    payload = json.loads((new_home / mm.SENTINEL_NAME).read_text(encoding="utf-8"))
    assert payload["version"] == mm.SENTINEL_VERSION
    assert payload["files"]["costs.jsonl"]["source_lines"] == 1
    assert "never writes to them and never deletes them" in payload["note"]


def test_dry_run_writes_nothing_at_all(homes):
    archive, new_home = homes
    before = _seed(archive, costs__jsonl='{"n":1}\n')

    result = mm.migrate_metrics(dry_run=True)

    assert result.dry_run
    assert result.files[0].appended_lines == 1
    assert not new_home.exists() or not (new_home / "costs.jsonl").exists()
    assert not (new_home / mm.SENTINEL_NAME).exists()
    assert _digests(archive) == before


def test_an_unparseable_sentinel_causes_a_re_merge_not_a_trusted_skip(homes):
    """"I could not read the record" must not read as "everything is fine"."""
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / mm.SENTINEL_NAME).write_text("{not json", encoding="utf-8")

    result = mm.migrate_metrics()

    assert result.ok
    assert result.status == "migrated"
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}']


def test_a_wrong_version_sentinel_causes_a_re_merge(homes):
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')
    new_home.mkdir(parents=True, exist_ok=True)
    (new_home / mm.SENTINEL_NAME).write_text(
        json.dumps({"version": mm.SENTINEL_VERSION + 99}), encoding="utf-8"
    )

    result = mm.migrate_metrics()

    assert result.ok
    assert _rows(new_home / "costs.jsonl") == ['{"n":1}']


# --------------------------------------------------------------------------- #
# refusals — the cases where doing nothing is the correct answer
# --------------------------------------------------------------------------- #


def test_source_equal_to_destination_is_refused(tmp_path, monkeypatch):
    """Copying a directory onto itself would double every row. Refuse."""
    shared = tmp_path / "same"
    (shared / "metrics").mkdir(parents=True)
    (shared / "metrics" / "costs.jsonl").write_text('{"n":1}\n', encoding="utf-8")
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(shared))
    monkeypatch.setenv("VCT_STATE_DIR", str(shared))

    result = mm.migrate_metrics()

    assert result.status == "failed"
    assert "same directory" in " ".join(result.errors)
    assert _rows(shared / "metrics" / "costs.jsonl") == ['{"n":1}']


def test_an_unwritable_destination_fails_loudly_and_keeps_the_source(
    homes, monkeypatch
):
    """A failed copy must report `failed` — never a green "already_current"."""
    archive, new_home = homes
    before = _seed(archive, costs__jsonl='{"n":1}\n')

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(mm, "_append_lines", _boom)
    result = mm.migrate_metrics()

    assert result.status == "failed"
    assert not result.ok
    assert result.originals_untouched
    assert _digests(archive) == before


def test_ensure_is_a_single_stat_when_there_is_no_archive(homes):
    """The hot path: `ensure_metrics_migrated` on a machine with no archive."""
    archive, new_home = homes
    result = mm.ensure_metrics_migrated()
    assert result.status == "no_source"
    assert not new_home.exists()


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #


def test_cli_json_reports_both_halves(homes):
    archive, _ = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    proc = _run_cli(homes, "--json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "migrated"
    assert payload["ok"] is True
    assert payload["originals_untouched"] is True
    assert payload["files"][0]["verified"] is True


def test_cli_quiet_is_silent_on_success_but_not_on_failure(homes, tmp_path):
    archive, _ = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    ok = _run_cli(homes, "--quiet")
    assert ok.returncode == 0
    assert ok.stdout.strip() == "", "a hook-driven run must print nothing"


def test_cli_dry_run_does_not_write(homes):
    archive, new_home = homes
    _seed(archive, costs__jsonl='{"n":1}\n')

    proc = _run_cli(homes, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    assert "would copy" in proc.stdout
    assert not (new_home / "costs.jsonl").exists()


# --------------------------------------------------------------------------- #
# resolver wiring
# --------------------------------------------------------------------------- #


def test_defaults_come_from_the_shared_resolvers(homes):
    """No inline path reconstruction — the module asks `vco_lib.paths`."""
    archive, new_home = homes
    result = mm.migrate_metrics()
    assert result.source == str(legacy_claude_metrics_dir()) == str(archive)
    assert result.dest == str(vct_metrics_dir()) == str(new_home)
