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
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run_bundle(folder: Path, *extra: str) -> subprocess.CompletedProcess:
    # child_env() puts THIS checkout first on PYTHONPATH + pins
    # VCT_ORCHESTRATOR_ROOT, so the child cannot import a stale site-packages
    # `vco_lib` and measure code that is not in this tree.
    env = child_env()
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


# ===========================================================================
# v0.2.92 EXTENSION (2026-09-05 field bug) — the guard above had a BLIND SPOT.
# ===========================================================================
#
# Incident: adding a project with "safe add" reported
#
#     Setup failed.  ERROR
#     schema-migration runner produced unparseable output (trailing characters
#     at line 1 column 2): stderr tail: . Bundle install will proceed.
#
# Root cause, same class as v0.2.84 but one level DEEPER: the offending print
# was NOT in a CLI handler body (the only thing the structural test above
# inspects) — it was in a LIBRARY function three frames down.
# `schema_migration_runner._apply_subprocess_edge` relayed each migration
# EDGE's captured stdout onto the parent's stdout, so
# `python -m vco_lib.project_init migrate-schema` emitted
#
#     4_to_5: <Class> at v5 shape (props present/added)
#     ...
#     {"folder": ..., "planned": []}
#
# and the launcher's `serde_json::from_str::<Value>(&stdout)` parsed the
# leading `4` as a complete JSON number, then choked on the `_`.
#
# Lessons this block encodes, so the guard cannot have the same blind spot
# twice:
#   1. The contract belongs to the whole CALL TREE, not the handler body →
#      drive the REAL CLI as a subprocess and parse its stdout (tests below).
#   2. The set of contract-bearing subcommands must be DERIVED from the
#      argparse surface, not hand-listed → a new subcommand fails this file
#      until it is classified (`test_every_subcommand_is_classified`).
#   3. Relaying a child's captured stdout onto our own stdout is the specific
#      mechanism → banned by shape across `vco_lib`
#      (`test_no_library_relays_child_stdout_to_our_stdout`).

_LAUNCHER_DB_MIGRATIONS = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
    / "migrations"
)

#: Every `vco_lib.project_init` subcommand, classified by its stdout contract.
#:
#: * ``"always"``  — the handler ALWAYS prints one JSON document on stdout
#:                   (no `--json` flag exists). The launcher parses it strictly.
#: * ``"flag"``    — JSON on stdout only under `--json`; human text otherwise.
#: * ``"never"``   — no machine consumer of stdout.
#:
#: ``argv`` is a hermetic invocation (no Weaviate, no real launcher.db) used by
#: the behavioural test; ``None`` means "cannot be driven hermetically" — those
#: still have to be classified, they just aren't executed here.
#: Placeholders: ``{folder}``, ``{db}``, ``{migrations}``.
JSON_CONTRACT_SUBCOMMANDS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "derive": ("flag", ("--name", "SomeProject", "--json")),
    "check-bundle-resume": ("always", ("--folder", "{folder}")),
    "check-node-formats-schema": (
        "always",
        ("--folder", "{folder}", "--db", "{db}", "--project-id", "p1"),
    ),
    "check-code-formats-schema": (
        "always",
        ("--folder", "{folder}", "--db", "{db}", "--project-id", "p1"),
    ),
    "migrate-schema": (
        "always",
        (
            "--folder", "{folder}", "--db", "{db}", "--project-id", "p1",
            "--migrations-dir", "{migrations}",
        ),
    ),
    "dismiss-deferral": (
        "flag",
        ("--folder", "{folder}", "--condition-id", "nonexistent_x", "--json"),
    ),
    # `--json` surfaces exist, but each needs a live Weaviate / a real volume
    # dir, so they are classified-but-not-driven here.
    "install-bundle": ("flag", None),  # driven by the v0.2.84 tests above
    "re-render-claude-md": ("flag", None),
    "bootstrap-collections": ("flag", None),
    "migrate-collections": ("flag", None),
    "drop-collections": ("flag", None),
    "detect-orphan-code-collections": ("flag", None),
    "drop-orphan-code-collections": ("flag", None),
    "reclaim-stranded-code-segments": ("flag", None),
}


def _registered_subcommands() -> set[str]:
    """Every subcommand name the REAL argparse surface registers.

    Derived from the parser, not from a literal list — that is the whole point:
    a subcommand added later cannot silently escape this file's classification.
    """
    from vco_lib import project_init as pinit

    parser = pinit._build_arg_parser()
    names: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 — argparse has no public API
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            names.update(choices.keys())
    return names


def _seed_launcher_db(db_path: Path, *, codegraph_at: int = 6) -> None:
    """A real-shaped launcher.db with codegraph_collection recorded BELOW
    canonical, so `migrate-schema` takes the edge-applying path."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE _schema_migrations (version INTEGER PRIMARY KEY,"
            " description TEXT NOT NULL, applied_at INTEGER NOT NULL)"
        )
        for f in sorted(_LAUNCHER_DB_MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug,"
            " created_at, updated_at, rl_port)"
            " VALUES ('p1','guard','/tmp/guard','base','p1',1,1,NULL)"
        )
        for suffix in (
            "CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction",
        ):
            conn.execute(
                "INSERT OR REPLACE INTO artifact_schema_versions"
                " (project_id, artifact_type, artifact_name, schema_version,"
                "  materialized_at) VALUES ('p1','codegraph_collection',?,?,1)",
                (f"GuardPrefix_{suffix}", codegraph_at),
            )
        conn.commit()
    finally:
        conn.close()


def _noisy_migrations_dir(root: Path) -> Path:
    """A migrations/ dir whose 6→7 edge prints human progress on ITS stdout.

    That is exactly what the shipped `migrations/codegraph_collection/*.py`
    edges do (`4_to_5: <Class> at v5 shape …`, `EDGE_APPLIED=1`) — the edge is
    not at fault; relaying its stdout onto OUR stdout was.
    """
    d = root / "migrations" / "codegraph_collection"
    d.mkdir(parents=True)
    (d / "6_to_7.py").write_text(
        "import sys\n"
        "print('6_to_7: GuardPrefix_CodeModule purged 0 rows')\n"
        "print('EDGE_APPLIED=1')\n"
        "sys.exit(0)\n"
    )
    return root / "migrations"


def _run_subcommand(
    subcommand: str, argv: tuple[str, ...], tmp_path: Path
) -> subprocess.CompletedProcess:
    folder = tmp_path / "proj"
    folder.mkdir(exist_ok=True)
    db = tmp_path / "launcher.db"
    if not db.exists():
        _seed_launcher_db(db)
    migrations = tmp_path / "migrations"
    if not migrations.exists():
        _noisy_migrations_dir(tmp_path)
    resolved = [
        a.format(folder=folder, db=db, migrations=migrations) for a in argv
    ]
    # Hermetic: this checkout pinned for the child (child_env), no Weaviate on
    # that port, and a codegraph prefix that only exists in the seeded registry.
    env = child_env(
        WEAVIATE_URL="http://127.0.0.1:9",
        CODE_GRAPH_PROJECT="GuardPrefix",
    )
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.project_init", subcommand, *resolved],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_every_subcommand_is_classified() -> None:
    """The blind-spot killer: the classification table is checked against the
    LIVE argparse surface, both ways.

    `migrate-schema` existed for four releases without anyone asking whether its
    stdout was a machine contract (it is). A newly-added subcommand must not be
    able to repeat that — adding one to `_build_arg_parser` turns this test red until
    its stdout contract is written down here.
    """
    registered = _registered_subcommands()
    classified = set(JSON_CONTRACT_SUBCOMMANDS)
    assert registered, "premise: build_parser registers subcommands"
    missing = sorted(registered - classified)
    assert not missing, (
        "new `vco_lib.project_init` subcommand(s) with no stdout-contract "
        "classification: " + repr(missing) + ". Add each to "
        "JSON_CONTRACT_SUBCOMMANDS as 'always' / 'flag' / 'never' — and if it "
        "emits JSON the launcher parses, give it a hermetic argv so the "
        "behavioural test below drives it."
    )
    stale = sorted(classified - registered)
    assert not stale, (
        "JSON_CONTRACT_SUBCOMMANDS names subcommand(s) the parser no longer "
        "registers: " + repr(stale)
    )


@pytest.mark.parametrize(
    "subcommand",
    sorted(
        name
        for name, (kind, argv) in JSON_CONTRACT_SUBCOMMANDS.items()
        if kind in ("always", "flag") and argv is not None
    ),
)
def test_json_contract_subcommand_stdout_is_pure_json(
    subcommand: str, tmp_path: Path
) -> None:
    """Drive the REAL CLI and parse its stdout exactly as the launcher does.

    Behavioural, not source-shaped: it does not matter WHERE in the call tree a
    stray print lives — the handler body, a helper module, or a relayed
    subprocess — this goes red.
    """
    _kind, argv = JSON_CONTRACT_SUBCOMMANDS[subcommand]
    assert argv is not None
    proc = _run_subcommand(subcommand, argv, tmp_path)
    try:
        json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"`project_init {subcommand}` stdout must be exactly one JSON "
            f"document (the launcher does serde_json::from_str on the WHOLE "
            f"stream). Parse error: {exc}\n"
            f"--- stdout (first 400 chars) ---\n{proc.stdout[:400]}\n"
            f"--- stderr tail ---\n{proc.stderr[-400:]}"
        )


def test_migrate_schema_edge_narrative_goes_to_stderr_REGRESSION_PIN(
    tmp_path: Path,
) -> None:
    """THE 2026-09-05 field-bug shape, end to end.

    Pre-fix this fails with the user's exact error class — Python's
    `json.loads` says ``Extra data: line 1 column 2 (char 1)``; serde says
    ``trailing characters at line 1 column 2``.

    Also pins that the narrative is not merely deleted: it must still be
    visible, on stderr, which is where the launcher tails it and where
    install.py's console already shows it.
    """
    _kind, argv = JSON_CONTRACT_SUBCOMMANDS["migrate-schema"]
    assert argv is not None
    proc = _run_subcommand("migrate-schema", argv, tmp_path)

    report = json.loads(proc.stdout)  # RED pre-fix
    # Premise: the edge really ran (we pinned the stream, not the work away).
    assert report["applied"] >= 1, (
        "premise: the seeded v6 registry must make the 6_to_7 edge run; "
        f"report={report}"
    )
    assert "6_to_7: GuardPrefix_CodeModule purged 0 rows" in proc.stderr, (
        "the edge narrative must stay visible — on stderr"
    )
    assert "EDGE_APPLIED=1" not in proc.stdout


def test_no_library_relays_child_stdout_to_our_stdout() -> None:
    """Ban the exact MECHANISM across `vco_lib`, not just the one site.

    A library function cannot know whether its caller is under a JSON contract,
    so echoing a captured child's stdout onto our own stdout is never safe
    there. `file=sys.stderr` (or a logger) is.
    """
    import re as _re

    relay = _re.compile(
        r"print\(\s*(?:[A-Za-z_][\w]*\.)?(?:stdout|stderr_and_stdout)\b"
    )
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "vco_lib").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in relay.finditer(src):
            call = _balanced_call(src, match.start() + len("print"))
            if "file=" in call:
                continue
            line_no = src[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line_no}")
    assert not offenders, (
        "library code relaying a child subprocess's stdout onto OUR stdout — "
        "the caller may be under a --json machine contract (v0.2.92 "
        "migrate-schema field bug). Use file=sys.stderr. Offenders: "
        + repr(offenders)
    )


def _balanced_call(src: str, open_paren_idx: int) -> str:
    """Return `src[open_paren_idx:close]` for the matching `)` (naive but
    sufficient: these are short print() calls with no paren-bearing strings)."""
    depth = 0
    for i in range(open_paren_idx, min(len(src), open_paren_idx + 2000)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren_idx: i + 1]
    return src[open_paren_idx: open_paren_idx + 2000]
