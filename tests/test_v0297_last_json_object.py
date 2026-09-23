# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — ONE parser for a vco_lib child's final ``--json`` report.

``vco_lib.child_process.last_json_object`` replaced two copies:
``machine_migrations._last_json_object`` (which ``gateway_freshness`` was
importing PRIVATELY) and ``embedding_enrichment._last_json_line``. They
disagreed on one input, and the disagreement mattered: with JSON progress
lines above a truncated final report, the lenient copy walked back to a
PROGRESS line and handed it to a caller that reads ``report["failed"]`` —
turning a killed enrichment child into a reported success.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import embedding_enrichment as ee  # noqa: E402
from vco_lib.child_process import last_json_object  # noqa: E402


def test_the_last_object_line_wins_over_stray_lines_above_it():
    out = 'warning: something\n{"a": 1}\nnot json\n{"b": 2}\ntrailing text\n'
    assert last_json_object(out) == {"b": 2}


def test_a_truncated_last_report_is_none_never_an_earlier_line():
    out = '{"progress": 1}\n{"progress": 2}\n{"enriched": 3, "fail'
    assert last_json_object(out) is None


def test_no_object_non_object_and_empty_are_none():
    assert last_json_object("") is None
    assert last_json_object(None) is None
    assert last_json_object("plain output\n") is None
    assert last_json_object("[1, 2]\n") is None


def _enrich(stdout: str) -> bool:
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    with mock.patch.object(ee.subprocess, "run", return_value=proc):
        return ee.enrich_collections_for_slot_change(
            venv_python=sys.executable,
            profile="qwen3",
            kg_collection="Foo_KnowledgeGraph",
            project_root=REPO_ROOT,
            log=lambda _m: None,
        )


def test_enrichment_reads_its_final_report():
    """LEAVE-ALONE: a complete report after progress lines still succeeds."""
    assert _enrich('{"progress": 1}\n{"enriched": 2, "skipped": 0, "failed": 0}\n') is True


def test_enrichment_does_not_mistake_a_progress_line_for_its_report():
    """ACT: the child died mid-report. RED with the old lenient
    ``_last_json_line``: it walked back to the EARLIER object (``failed: 0``)
    and the pass reported success over an unfinished collection."""
    assert _enrich('{"enriched": 1, "failed": 0}\n{"enriched": 2, "fai') is False
