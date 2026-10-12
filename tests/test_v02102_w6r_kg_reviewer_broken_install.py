# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R item 11 — post-git-commit-kg-sync names a broken install.

When ``$PY`` or the shipped ``.claude/scripts/kg_reviewer_backend.py`` is
missing, the hook used to fall back to the direct Haiku spawn SILENTLY. Under
the loud-fail rule a missing shipped script is a broken install: it is now
named on stderr, in ``kg-commit-review.log`` and in the commit's
``kg-commit-reviews.jsonl`` row (``broken_install``). The fallback spawn is
kept (the commit is still reviewed) — it no longer hides anything. With the
chain present nothing is reported and the chain is what runs. .sh and .ps1.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_v02102_w6n_kg_reviewer_fixes import _hook_cmd, _scratch_repo, _wait_capture


def _run(sibling: str, repo: Path, env: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
                          "tool_response": "[main abc123] committed"})
    proc = subprocess.run(_hook_cmd(sibling), cwd=repo, env=env, input=payload,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc


def _rows(repo: Path) -> list[dict]:
    p = repo / ".claude" / "logs" / "kg-commit-reviews.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()]


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_missing_chain_script_is_named_as_a_broken_install(tmp_path, sibling):
    repo, env = _scratch_repo(tmp_path, commits=1)
    proc = _run(sibling, repo, env)
    chain = "kg_reviewer_backend.py is missing"
    assert "broken install" in proc.stderr and chain in proc.stderr, proc.stderr
    if sibling == "sh":
        # The .sh log is append-only; the .ps1 spawn truncates its log, so
        # there the ledger row below is the persistent record.
        log = (repo / ".claude" / "logs" / "kg-commit-review.log")
        assert "broken install" in log.read_text(encoding="utf-8")
    row = _rows(repo)[-1]
    assert chain in row["broken_install"]
    # The fallback still reviews the commit (the fake `claude` was spawned).
    assert "first_file.txt" in _wait_capture(env)


_STUB_CHAIN = """import pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('--log') + 1]).with_name('chain-ran.marker').write_text('ok')
"""


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_healthy_install_reports_nothing(tmp_path, sibling):
    repo, env = _scratch_repo(tmp_path, commits=1)
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg_reviewer_backend.py").write_text(_STUB_CHAIN, encoding="utf-8")
    proc = _run(sibling, repo, env)
    assert "broken install" not in proc.stderr
    marker = repo / ".claude" / "logs" / "chain-ran.marker"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.exists(), "the chain, not the fallback, must run"
    assert "broken_install" not in _rows(repo)[-1]
