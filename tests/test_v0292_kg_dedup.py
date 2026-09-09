# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for `vco_lib.kg_dedup` — the v0.2.92 WP-B2 duplicate-object
reconcile tool (`kg-dedup`) for field defect D13.

WP-B1 closed the CAUSE (one canonical POSIX `file_path` per write). It heals
a path only when that path is next written, so a node nobody edits again
stays duplicated forever. This tool reconciles those already-written rows.

The decision matrix encoded here is the destructive one: both the ACT case
(delete the extras) and the LEAVE-ALONE case (dry run, no duplicates,
legitimate chunk siblings) for every branch that gates a delete, plus every
unreadable state — which must REFUSE with a named reason rather than report
"0 duplicates" for a collection it never read.

Weaviate is faked (`_FakeBackend`), never live — same posture as
`tests/test_v0292_kg_sync_drift.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.common.wrapper_staging import effective_wrapper_text
from vco_lib import kg_dedup
from vco_lib.bundle_globs import script_patterns

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER_SH = REPO_ROOT / "templates" / "scripts" / "kg-dedup"
WRAPPER_PS1 = REPO_ROOT / "templates" / "scripts" / "kg-dedup.ps1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _row(uuid: str, file_path, updated_at=None, chunk_num=1) -> dict:
    return {
        "uuid": uuid,
        "file_path": file_path,
        "updated_at": updated_at,
        "chunk_num": chunk_num,
    }


class _FakeBackend:
    """Stands in for `kg_dedup.WeaviateBackend` — same four methods.

    `rows` is the collection content; every delete is RECORDED so a test can
    assert that a dry run touched nothing.
    """

    def __init__(
        self,
        rows=None,
        *,
        reachable=True,
        exists=True,
        weaviate_url="http://fake:8081",
        raise_on_fetch=None,
        raise_on_delete=None,
        raise_on_reachable=None,
        raise_on_exists=None,
    ) -> None:
        self.rows = list(rows or [])
        self._reachable = reachable
        self._exists = exists
        self.weaviate_url = weaviate_url
        self._raise_on_fetch = raise_on_fetch
        self._raise_on_delete = raise_on_delete
        self._raise_on_reachable = raise_on_reachable
        self._raise_on_exists = raise_on_exists
        self.deleted: list[str] = []
        self.closed = False

    def reachable(self) -> bool:
        if self._raise_on_reachable:
            raise self._raise_on_reachable
        return self._reachable

    def collection_exists(self, collection: str) -> bool:
        if self._raise_on_exists:
            raise self._raise_on_exists
        return self._exists

    def fetch_rows(self, collection: str):
        if self._raise_on_fetch:
            raise self._raise_on_fetch
        return list(self.rows)

    def delete(self, collection: str, uuids) -> int:
        if self._raise_on_delete:
            raise self._raise_on_delete
        n = 0
        for u in uuids:
            self.deleted.append(u)
            self.rows = [r for r in self.rows if r["uuid"] != u]
            n += 1
        return n

    def close(self) -> None:
        self.closed = True


# The field shape: one path duplicated 4x, another 2x.
def _field_rows() -> list[dict]:
    return [
        _row("a1", "knowledge/concepts/foo.md", "2026-08-01T00:00:00Z"),
        _row("a2", "knowledge/concepts/foo.md", "2026-08-02T00:00:00Z"),
        _row("a3", "knowledge/concepts/foo.md", "2026-09-01T00:00:00Z"),  # newest
        _row("a4", "knowledge/concepts/foo.md", "2026-07-01T00:00:00Z"),
        _row("b1", "knowledge/concepts/bar.md", "2026-08-10T00:00:00Z"),
        _row("b2", "knowledge/concepts/bar.md", "2026-08-20T00:00:00Z"),  # newest
        _row("c1", "knowledge/concepts/solo.md", "2026-08-05T00:00:00Z"),
    ]


# ---------------------------------------------------------------------------
# 1. Dry run — reports, deletes NOTHING (the leave-alone case)
# ---------------------------------------------------------------------------

def test_dry_run_reports_and_deletes_nothing():
    backend = _FakeBackend(_field_rows())
    report = kg_dedup.reconcile(backend, "MyKG")

    assert report.status == "duplicates"
    assert report.applied is False
    assert report.deleted == 0
    assert backend.deleted == [], "dry run must not delete anything"
    assert len(backend.rows) == 7, "dry run must leave the collection intact"
    assert report.scanned == 7
    assert report.distinct_keys == 3
    assert report.duplicate_objects == 4  # 3 extras on foo + 1 on bar
    assert report.exit_code == kg_dedup.EXIT_OK

    text = "\n".join(kg_dedup.format_report(report))
    assert "DRY RUN" in text and "--apply" in text
    assert "knowledge/concepts/foo.md" in text


def test_dry_run_is_the_default_for_the_cli():
    """`--apply` is required to delete: the bare CLI invocation must not."""
    backend = _FakeBackend(_field_rows())
    rc = kg_dedup.main(["--collection", "MyKG"], backend=backend)
    assert rc == kg_dedup.EXIT_OK
    assert backend.deleted == []


# ---------------------------------------------------------------------------
# 2. --apply — removes exactly the extras, keeps the newest (the act case)
# ---------------------------------------------------------------------------

def test_apply_deletes_extras_and_keeps_newest():
    backend = _FakeBackend(_field_rows())
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)

    assert report.applied is True
    assert report.deleted == 4
    assert sorted(backend.deleted) == ["a1", "a2", "a4", "b1"]
    survivors = sorted(r["uuid"] for r in backend.rows)
    assert survivors == ["a3", "b2", "c1"], (
        "must keep the newest of each duplicated path and every unique row"
    )
    keeps = {g.file_path: g.keep_uuid for g in report.groups}
    assert keeps["knowledge/concepts/foo.md"] == "a3"
    assert keeps["knowledge/concepts/bar.md"] == "b2"


def test_apply_via_cli_flag():
    backend = _FakeBackend(_field_rows())
    rc = kg_dedup.main(["--collection", "MyKG", "--apply"], backend=backend)
    assert rc == kg_dedup.EXIT_OK
    assert sorted(backend.deleted) == ["a1", "a2", "a4", "b1"]


def test_tiebreak_is_deterministic_when_updated_at_matches():
    """Equal timestamps must not make the survivor arbitrary — a dry run
    that cannot predict `--apply` is worse than no dry run."""
    rows = [
        _row("zzz", "knowledge/a.md", "2026-08-01T00:00:00Z"),
        _row("aaa", "knowledge/a.md", "2026-08-01T00:00:00Z"),
        _row("mmm", "knowledge/a.md", "2026-08-01T00:00:00Z"),
    ]
    first = kg_dedup.reconcile(_FakeBackend(list(rows)), "MyKG")
    second = kg_dedup.reconcile(_FakeBackend(list(reversed(rows))), "MyKG")
    assert first.groups[0].keep_uuid == "aaa"
    assert second.groups[0].keep_uuid == "aaa"


def test_row_with_no_updated_at_never_wins_over_a_dated_row():
    rows = [
        _row("undated", "knowledge/a.md", None),
        _row("dated", "knowledge/a.md", "2020-01-01T00:00:00Z"),
    ]
    report = kg_dedup.reconcile(_FakeBackend(rows), "MyKG")
    assert report.groups[0].keep_uuid == "dated"
    assert report.groups[0].delete_uuids == ("undated",)


# ---------------------------------------------------------------------------
# 3. Unreadable states REFUSE loudly — never "0 duplicates"
# ---------------------------------------------------------------------------

def test_unreachable_weaviate_refuses_loudly(capsys):
    backend = _FakeBackend(_field_rows(), reachable=False)
    report = kg_dedup.reconcile(backend, "MyKG")

    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_UNREACHABLE
    assert report.exit_code == kg_dedup.EXIT_REFUSED
    assert report.scanned == 0
    assert backend.deleted == []

    rc = kg_dedup.main(["--collection", "MyKG"], backend=backend)
    captured = capsys.readouterr()
    assert rc == kg_dedup.EXIT_REFUSED
    assert kg_dedup.REASON_UNREACHABLE in captured.err
    assert "no duplicates" not in captured.out
    assert captured.out == "", "a refusal must not print a clean report"


def test_reachability_probe_that_crashes_is_treated_as_unreachable():
    backend = _FakeBackend(
        _field_rows(), raise_on_reachable=OSError("connection reset")
    )
    report = kg_dedup.reconcile(backend, "MyKG")
    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_UNREACHABLE


def test_missing_collection_refuses(capsys):
    backend = _FakeBackend([], exists=False)
    report = kg_dedup.reconcile(backend, "NopeKG")
    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_COLLECTION_MISSING
    assert report.exit_code == kg_dedup.EXIT_REFUSED

    rc = kg_dedup.main(["--collection", "NopeKG"], backend=backend)
    captured = capsys.readouterr()
    assert rc == kg_dedup.EXIT_REFUSED
    assert kg_dedup.REASON_COLLECTION_MISSING in captured.err
    assert "NopeKG" in captured.err


def test_empty_collection_refuses_rather_than_reporting_zero(capsys):
    """The governing thesis: a check that cannot tell "I could not read
    this" from "this is fine" is not a check."""
    backend = _FakeBackend([], exists=True)
    report = kg_dedup.reconcile(backend, "MyKG")
    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_COLLECTION_EMPTY
    assert report.exit_code == kg_dedup.EXIT_REFUSED

    rc = kg_dedup.main(["--collection", "MyKG"], backend=backend)
    captured = capsys.readouterr()
    assert rc == kg_dedup.EXIT_REFUSED
    assert kg_dedup.REASON_COLLECTION_EMPTY in captured.err
    assert "no duplicates" not in captured.out


def test_read_failure_mid_scan_refuses():
    backend = _FakeBackend(
        _field_rows(), raise_on_fetch=RuntimeError("grpc deadline exceeded")
    )
    report = kg_dedup.reconcile(backend, "MyKG")
    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_READ_FAILED
    assert "grpc deadline exceeded" in report.detail
    assert backend.deleted == []


def test_delete_failure_refuses_and_does_not_claim_a_clean_run():
    backend = _FakeBackend(
        _field_rows(), raise_on_delete=RuntimeError("weaviate 500")
    )
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)
    assert report.status == "refused"
    assert report.reason == kg_dedup.REASON_DELETE_FAILED
    assert report.exit_code == kg_dedup.EXIT_REFUSED
    assert report.deleted == 0


def test_unresolvable_collection_is_a_usage_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    monkeypatch.delenv("KG_COLLECTION", raising=False)
    rc = kg_dedup.main(
        ["--project-root", str(tmp_path)], backend=_FakeBackend(_field_rows())
    )
    captured = capsys.readouterr()
    assert rc == kg_dedup.EXIT_USAGE
    assert "collection_unresolved" in captured.err


# ---------------------------------------------------------------------------
# 4. Clean collection is a no-op (the leave-alone case)
# ---------------------------------------------------------------------------

def test_collection_without_duplicates_is_a_clean_noop(capsys):
    rows = [
        _row("a", "knowledge/concepts/foo.md", "2026-08-01T00:00:00Z"),
        _row("b", "knowledge/concepts/bar.md", "2026-08-02T00:00:00Z"),
    ]
    backend = _FakeBackend(rows)
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)

    assert report.status == "ok"
    assert report.groups == ()
    assert report.deleted == 0
    assert report.applied is False
    assert backend.deleted == [], "no duplicates means no deletes, even with --apply"
    assert report.exit_code == kg_dedup.EXIT_OK

    rc = kg_dedup.main(["--collection", "MyKG", "--apply"], backend=_FakeBackend(rows))
    captured = capsys.readouterr()
    assert rc == kg_dedup.EXIT_OK
    assert "no duplicates" in captured.out


def test_chunk_siblings_are_not_duplicates():
    """A chunked node legitimately owns N objects for ONE file_path. Keying
    on file_path alone would delete real chunks — data loss dressed as a fix."""
    rows = [
        _row("c1", "knowledge/big.md", "2026-08-01T00:00:00Z", chunk_num=1),
        _row("c2", "knowledge/big.md", "2026-08-01T00:00:00Z", chunk_num=2),
        _row("c3", "knowledge/big.md", "2026-08-01T00:00:00Z", chunk_num=3),
    ]
    backend = _FakeBackend(rows)
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)
    assert report.status == "ok"
    assert backend.deleted == []
    assert len(backend.rows) == 3


def test_duplicated_chunk_is_reconciled_per_chunk():
    rows = [
        _row("c1", "knowledge/big.md", "2026-08-01T00:00:00Z", chunk_num=1),
        _row("c1b", "knowledge/big.md", "2026-08-05T00:00:00Z", chunk_num=1),
        _row("c2", "knowledge/big.md", "2026-08-01T00:00:00Z", chunk_num=2),
    ]
    backend = _FakeBackend(rows)
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)
    assert backend.deleted == ["c1"]
    assert sorted(r["uuid"] for r in backend.rows) == ["c1b", "c2"]
    assert report.groups[0].chunk_num == 1


# ---------------------------------------------------------------------------
# 5. WP-B1 canonicalisation is REUSED, at read time
# ---------------------------------------------------------------------------

def test_mixed_separator_spellings_are_one_group():
    """The D13 shape itself: a Windows-written row and a POSIX-written row
    for the SAME source file. `to_posix_rel` (the one shared normalizer that
    `sync_knowledge_graph.py` imports) must unite them."""
    rows = [
        _row("win", "knowledge\\concepts\\foo.md", "2026-08-01T00:00:00Z"),
        _row("posix", "knowledge/concepts/foo.md", "2026-09-01T00:00:00Z"),
    ]
    backend = _FakeBackend(rows)
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.file_path == "knowledge/concepts/foo.md"
    assert group.keep_uuid == "posix"
    assert backend.deleted == ["win"]
    assert len(group.spellings) == 2
    assert "mixed file_path spellings" in "\n".join(kg_dedup.format_report(report))


def test_grouping_uses_the_shared_normalizer():
    from vco_lib.paths import to_posix_rel
    groups, distinct = kg_dedup.group_rows([
        _row("x", "a\\b\\c.md"), _row("y", "a/b/c.md"),
    ])
    assert distinct == 1
    assert groups[0].file_path == to_posix_rel("a\\b\\c.md")


def test_rows_without_a_file_path_are_skipped_not_guessed_at():
    rows = [
        _row("n1", None), _row("n2", ""), _row("n3", "   "),
        _row("k1", "knowledge/a.md", "2026-08-01T00:00:00Z"),
        _row("k2", "knowledge/a.md", "2026-08-02T00:00:00Z"),
    ]
    backend = _FakeBackend(rows)
    report = kg_dedup.reconcile(backend, "MyKG", apply=True)
    assert backend.deleted == ["k1"]
    assert {r["uuid"] for r in backend.rows} == {"n1", "n2", "n3", "k2"}
    assert report.scanned == 5


# ---------------------------------------------------------------------------
# 6. Shipping: the wrappers exist, are in lockstep, and reach user projects
# ---------------------------------------------------------------------------

def test_both_wrappers_exist_and_bash_one_is_executable():
    assert WRAPPER_SH.is_file(), f"missing {WRAPPER_SH}"
    assert WRAPPER_PS1.is_file(), f"missing {WRAPPER_PS1}"
    assert os.access(WRAPPER_SH, os.X_OK), "kg-dedup must be executable"


def test_bash_wrapper_shebang_satisfies_the_parity_gate():
    """`.github/scripts/check_hook_parity.py::_has_extensionless_bash_sibling`
    accepts `kg-dedup` as the `.ps1`'s sibling only via this shebang."""
    first = WRAPPER_SH.read_bytes().split(b"\n", 1)[0].decode()
    assert first.startswith("#!") and "bash" in first


def test_ps1_wrapper_has_utf8_bom():
    assert WRAPPER_PS1.read_bytes().startswith(b"\xef\xbb\xbf")


def test_wrappers_are_in_lockstep():
    # v0.2.94: the ladder tokens (VCT_VENV / VCT_INSTALL_ROOT /
    # VCT_ORCHESTRATOR_ROOT / the import probe) live in the shared
    # `vct_venv_ladder.{sh,ps1}` both wrappers source. `effective_wrapper_text`
    # follows them there, so lockstep is still asserted over what each wrapper
    # actually executes — without mandating that both keep an inlined copy.
    sh = effective_wrapper_text(WRAPPER_SH)
    ps1 = effective_wrapper_text(WRAPPER_PS1, encoding="utf-8-sig")
    for token in (
        "VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT",
        "KG_SYNC_PROJECT_ROOT", "vco_lib.kg_dedup",
        "import weaviate, vco_lib",
    ):
        assert token in sh, f"{token} missing from the bash wrapper"
        assert token in ps1, f"{token} missing from the ps1 wrapper"
    # Both refuse rather than falling back to a bare interpreter. v0.2.94: the
    # refusal HEADLINE is composed at runtime by the shared ladder from the
    # tool name each wrapper hands it, so the literal
    # "kg-dedup: ERROR - ..." is in neither source any more. What each wrapper
    # still owns is the CALL, under its own name — and the refusal itself is
    # DRIVEN (bash, live) in tests/test_v0294_wrapper_venv_ladder_parity.py,
    # which is the stronger check a source scan was standing in for.
    assert 'vct_venv_ladder_refusal "kg-dedup"' in WRAPPER_SH.read_text(
        encoding="utf-8"
    )
    assert 'Write-VctLadderRefusal -Tool "kg-dedup"' in WRAPPER_PS1.read_text(
        encoding="utf-8-sig"
    )


def test_wrappers_forward_to_the_single_python_home():
    """One concern, one home: neither wrapper reimplements the reconcile."""
    assert '-m vco_lib.kg_dedup "$@"' in WRAPPER_SH.read_text(encoding="utf-8")
    assert "-m vco_lib.kg_dedup @args" in WRAPPER_PS1.read_text(encoding="utf-8-sig")


def test_script_patterns_already_ship_the_new_wrappers():
    """No manifest edit was needed: `kg-*` and `*.ps1` already match."""
    import fnmatch
    patterns = script_patterns()
    for name in ("kg-dedup", "kg-dedup.ps1"):
        assert any(fnmatch.fnmatch(name, p) for p in patterns), (
            f"{name} would NOT be copied into .claude/scripts/ by "
            f"vco_lib.bundle_globs.script_patterns()"
        )


def test_bash_wrapper_is_syntactically_valid():
    result = subprocess.run(
        ["bash", "-n", str(WRAPPER_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_module_is_runnable_as_python_dash_m():
    """The wrappers invoke `python -m vco_lib.kg_dedup`; prove the entry
    point resolves (a usage error, exit 2, is a successful resolution)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "vco_lib.kg_dedup", "--help"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout
    assert "Dry run by default" in result.stdout


def test_exit_codes_match_the_kg_sync_convention():
    """kg-sync documents 0 = clean, 1 = failure, 2 = usage. Do not collide."""
    assert (kg_dedup.EXIT_OK, kg_dedup.EXIT_REFUSED, kg_dedup.EXIT_USAGE) == (0, 1, 2)
