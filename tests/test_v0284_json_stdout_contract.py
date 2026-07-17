# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 hotfix: `install-bundle --json` stdout is a MACHINE CONTRACT.

Incident (2026-07-17, maintainer dogfood of v0.2.84): the WP-4 adoption NOTICE
block printed to STDOUT. The `--json` CLI surface emits `json.dumps(result)` on
that same stream and the LAUNCHER parses it, so on any project that had
adoptable files the update-all report showed:

    install-bundle --update produced unparseable output
    (expected value at line 2 column 2) ... Project files may be partially
    updated.

The work had actually SUCCEEDED (adoptions + backups + audit rows all landed);
only the result envelope was corrupted — but the message is alarming and, for a
third party, indistinguishable from real damage.

Root rule (this file pins it): under `--json`, NOTHING may reach stdout except
the single JSON document. Human-facing notices go to stderr — the stream
`project_init._log_auto` already uses for the audit lines (and whose tail the
launcher surfaces), so nothing becomes less visible.

The tests drive the REAL CLI as a subprocess (the launcher's actual contract),
in both the fresh-install and the adoption-triggering update shape.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run_bundle(folder: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vco_lib.project_init",
            "install-bundle",
            "--folder",
            str(folder),
            "--orchestrator-root",
            str(REPO_ROOT),
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )


def _assert_stdout_is_pure_json(proc: subprocess.CompletedProcess) -> dict:
    """stdout must parse as ONE json document — the launcher's exact contract."""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover — failure path
        pytest.fail(
            "stdout under --json must be exactly one parseable JSON document "
            f"(the launcher does json.loads on it). Parse error: {exc}\n"
            f"--- stdout (first 400 chars) ---\n{proc.stdout[:400]}\n"
            f"--- stderr tail ---\n{proc.stderr[-400:]}"
        )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_fresh_install_json_stdout_parses(project: Path) -> None:
    proc = _run_bundle(project)
    assert proc.returncode == 0, proc.stderr[-500:]
    _assert_stdout_is_pure_json(proc)


def test_adoption_update_json_stdout_parses_REGRESSION_PIN(project: Path) -> None:
    """THE incident shape: a shipped codefile drifted → adoption fires → the
    NOTICE used to prepend prose to stdout → launcher parse error.

    Fails on the pre-fix tree with the exact reported error
    ("Expecting value: line 2 column 2").
    """
    _assert_stdout_is_pure_json(_run_bundle(project))

    drifted = project / ".claude" / "agents" / "coder.md"
    assert drifted.is_file(), "premise: the bundle ships this agent"
    drifted.write_text(
        drifted.read_text() + "\n# drifted bytes (stale shipped version shape)\n"
    )

    proc = _run_bundle(project, "--update")
    assert proc.returncode == 0, proc.stderr[-500:]
    result = _assert_stdout_is_pure_json(proc)

    # The adoption really happened (we pinned the contract, not the behaviour away).
    assert result["actions"]["adopt"], "premise: the drifted file was adopted"
    assert result.get("adopt_backup_dir"), "premise: a backup dir was recorded"

    # And the human-facing notice is still LOUD — on stderr, where the launcher
    # surfaces it and where the audit lines already live.
    assert "NOTICE — shipped-file adoption" in proc.stderr
    assert "auto-resolved" in proc.stderr


def test_no_bare_stdout_prints_in_bundle_install_body() -> None:
    """Structural belt-and-braces: `install_project_bundle`'s body must not
    grow a new bare `print(` (stdout). Any human-facing line inside the bundle
    flow must be `file=sys.stderr` (or go through a log callback), because the
    function runs under the --json contract. Guards the whole class, not just
    the one NOTICE this hotfix moved.
    """
    src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text()
    start = src.index("def install_project_bundle(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]

    offenders: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("print("):
            continue
        # Walk the statement to its closing paren so multi-line prints are seen.
        idx = body.index(raw)
        stmt = body[idx: idx + 800]
        if "file=sys.stderr" not in stmt.split("\n\n")[0]:
            offenders.append(line[:80])
    assert not offenders, (
        "bare stdout print(...) inside install_project_bundle — stdout is the "
        "--json machine contract; use file=sys.stderr. Offenders: " + repr(offenders)
    )
