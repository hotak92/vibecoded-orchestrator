# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 pull-in ⑥ — ``app_state["last_kg_sync_at"]`` finally has a reader.

``kg_context_triple.record`` and install.py stamp the row on every certified
whole-tree sync of the orchestrator root; nothing read it. ``vco doctor`` now
reports it (probe ``last_kg_sync``) in the human report and in ``--json``:

* row present → ``ok``, the stamp, its age, the collection it is about;
* DB present, row absent → ``unknown``, "never (no certified whole-tree sync
  recorded yet)";
* no launcher.db → ``unknown``, said so;
* not the orchestrator root → no finding at all (the row is root-scoped).

The DB-backed leg uses a throwaway sqlite file named by
``VCT_LAUNCHER_DB_PATH`` — never the user's launcher.db.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import install
from vco_lib import doctor, kg_context_triple, kg_sync_recency as ksr

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def _root(tmp_path: Path) -> Path:
    """A folder ``looks_like_orchestrator_root`` accepts (vco_lib/ + .claude/)."""
    root = tmp_path / "orch"
    (root / "vco_lib").mkdir(parents=True)
    (root / ".claude").mkdir()
    return root


def _rows(**kv) -> dict:
    rows = {"db_present": True}
    rows.update(kv)
    return rows


def _probe(folder: Path, rows: dict) -> list:
    res = doctor.DoctorResolvers(kg_sync_rows=lambda: rows)
    return doctor.probe_last_kg_sync(folder, res, {})


def test_registered_full_scope_only() -> None:
    fn, scopes = doctor.PROBES["last_kg_sync"]
    assert fn is doctor.probe_last_kg_sync
    assert scopes == (doctor.SCOPE_FULL,)


def test_stats_key_matches_install_py() -> None:
    assert ksr.APP_STATE_KEY_LAST_KG_SYNC_STATS == install._APP_STATE_KEY_LAST_KG_SYNC_STATS
    assert install._APP_STATE_KEY_LAST_KG_SYNC_AT == kg_context_triple.APP_STATE_KEY_LAST_KG_SYNC_AT


def test_row_present_reports_stamp_age_and_collection(tmp_path: Path) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=3, hours=4, minutes=5)).isoformat()
    findings = _probe(_root(tmp_path), _rows(
        last_kg_sync_at=stamp, last_installed_kg_collection="Alpha_KnowledgeGraph",
        last_kg_sync_stats=json.dumps({"nodes_synced": 7, "nodes_skipped": 2}),
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f.probe == "last_kg_sync" and f.status == doctor.STATUS_OK
    assert f.summary.startswith(f"last KG sync: {stamp} (age 3d 4h)"), f.summary
    assert "Alpha_KnowledgeGraph" in f.summary
    assert f.detail["state"] == ksr.STATE_RECORDED
    assert f.detail["last_kg_sync_at"] == stamp
    assert 3 * 86400 < f.detail["age_seconds"] < 4 * 86400
    assert f.detail["install_stats"] == {"nodes_synced": 7, "nodes_skipped": 2}
    # Exposed in the JSON payload, and never as a problem.
    payload = doctor.DoctorReport(folder=tmp_path, scope=doctor.SCOPE_FULL,
                                  findings=findings).to_dict()
    entry = payload["findings"][0]
    assert entry["probe"] == "last_kg_sync" and "fix" not in entry
    assert entry["detail"]["last_kg_sync_at"] == stamp
    json.dumps(payload)  # serialisable


def test_row_absent_reports_never(tmp_path: Path) -> None:
    [f] = _probe(_root(tmp_path), _rows(last_kg_sync_at=None))
    assert f.status == doctor.STATUS_UNKNOWN
    assert f.summary == "last KG sync: never (no certified whole-tree sync recorded yet)"
    assert f.detail["state"] == ksr.STATE_NEVER


def test_no_launcher_db_is_unknown_and_says_so(tmp_path: Path) -> None:
    [f] = _probe(_root(tmp_path), {"db_present": False})
    assert f.status == doctor.STATUS_UNKNOWN
    assert "no launcher.db" in f.summary
    assert f.detail["state"] == ksr.STATE_NO_DB


def test_unparseable_stamp_is_unknown(tmp_path: Path) -> None:
    [f] = _probe(_root(tmp_path), _rows(last_kg_sync_at="yesterday-ish"))
    assert f.status == doctor.STATUS_UNKNOWN
    assert "yesterday-ish" in f.summary


def test_a_non_root_project_gets_no_finding(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    called = []
    res = doctor.DoctorResolvers(kg_sync_rows=lambda: called.append(1) or _rows())
    assert doctor.probe_last_kg_sync(project, res, {}) == []
    assert called == [], "a non-root folder must not even read the root's row"


@pytest.mark.parametrize("seconds,text", [
    (10, "<1m"), (42 * 60, "42m"), (5 * 3600 + 12 * 60, "5h 12m"),
    (3 * 86400 + 4 * 3600 + 59, "3d 4h"), (-5, "in the future (clock skew?)"),
])
def test_format_age(seconds: float, text: str) -> None:
    assert ksr.format_age(seconds) == text


def test_parse_stamp_accepts_both_writers_shapes() -> None:
    aware = ksr.parse_stamp("2026-10-06T11:00:00.123456+00:00")
    zulu = ksr.parse_stamp("2026-10-06T11:00:00Z")
    naive = ksr.parse_stamp("2026-10-06T11:00:00")
    assert aware and zulu and naive
    assert (NOW - zulu).total_seconds() == 3600
    assert zulu == naive  # naive is read as UTC, not local time
    assert ksr.parse_stamp("") is None and ksr.parse_stamp(None) is None


def _db(path: Path, rows: dict) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)", list(rows.items()))
    conn.commit()
    conn.close()


def test_default_reader_reads_a_real_launcher_db(tmp_path: Path, monkeypatch) -> None:
    """The DB-backed leg: the row WRITTEN by kg_context_triple.record is the
    row the doctor reads — through the default resolvers, no injection."""
    db = tmp_path / "launcher.db"
    _db(db, {})
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))

    from vco_lib.launcher_db_writer import write_app_state_key

    assert kg_context_triple.record(
        "qwen3", "Alpha_KnowledgeGraph", "Alpha_KnowledgeGraph",
        write_key=lambda k, v: write_app_state_key(db, k, v),
    )
    [f] = doctor.probe_last_kg_sync(_root(tmp_path), doctor.DoctorResolvers(), {})
    assert f.status == doctor.STATUS_OK, f.summary
    assert "Alpha_KnowledgeGraph" in f.summary and "(age <1m)" in f.summary


def test_default_reader_db_without_the_row_is_never(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "launcher.db"
    _db(db, {"unrelated": "x"})
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    [f] = doctor.probe_last_kg_sync(_root(tmp_path), doctor.DoctorResolvers(), {})
    assert f.detail["state"] == ksr.STATE_NEVER, f.summary


def test_default_reader_without_a_db_is_no_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(tmp_path / "absent.db"))
    [f] = doctor.probe_last_kg_sync(_root(tmp_path), doctor.DoctorResolvers(), {})
    assert f.detail["state"] == ksr.STATE_NO_DB, f.summary


def test_the_engine_runs_it_and_json_carries_it(tmp_path: Path, monkeypatch) -> None:
    """Through ``run_doctor`` (the CLI's and install's path), not just the
    probe function: the registered entry is what puts it in ``--json``."""
    monkeypatch.setattr(doctor, "PROBES",
                        {"last_kg_sync": doctor.PROBES["last_kg_sync"]})
    stamp = "2026-10-06T11:00:00+00:00"
    report = doctor.run_doctor(
        _root(tmp_path), scope=doctor.SCOPE_FULL,
        resolvers=doctor.DoctorResolvers(kg_sync_rows=lambda: _rows(last_kg_sync_at=stamp)),
    )
    [entry] = report.to_dict()["findings"]
    assert entry["probe"] == "last_kg_sync"
    assert entry["detail"]["last_kg_sync_at"] == stamp
    assert any("last KG sync: 2026-10-06T11:00:00+00:00" in ln for ln in report.render_lines())
