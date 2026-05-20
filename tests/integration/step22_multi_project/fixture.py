# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Step 22 fixture builder — seeds a 3-project launcher.db with a
distinctive access-matrix configuration, optionally bootstraps the
backing Weaviate collections, and starts the ``vct-hub`` binary
against the sandboxed state directory.

This is the input side of the Step 22 regression. The test file
:mod:`tests.integration.step22_multi_project.test_access_matrix`
consumes the fixture's output: a running hub + 3 known project
folders + a known access-matrix shape.

Sandbox isolation contract (see ``tests/common/sandbox.py``):

    * ``VCT_STATE_DIR`` = ``$RUNNER_TEMP/.vct-step22-<run_id>/``
    * Weaviate collections prefixed ``STEP22_<run_id>_…``
    * Keychain module-ids prefixed ``step22-<run_id>-`` (unused here
      — fixture doesn't write secrets — but reserved for symmetry).
    * Refuses to run unless ``VCO_CI_FIXTURE=1`` and ``VCT_STATE_DIR``
      is NOT under ``$HOME/.vct/``.

The access-matrix encoded by this fixture:

    ┌────────────┬──────────────────┬────────────────────────────┐
    │ Project    │ KG read access   │ Codegraph grants RECEIVED  │
    ├────────────┼──────────────────┼────────────────────────────┤
    │ A (alpha)  │ own + B's        │ FROM A (own slug only)     │
    │ B (beta)   │ own only         │ FROM A + B (A grants to B) │
    │ C (gamma)  │ own only         │ FROM C (own slug only)     │
    └────────────┴──────────────────┴────────────────────────────┘

Key asymmetries we test:
    1. A→B KG read, but NOT B→A     (kg_collection_access asymmetric)
    2. A→B codegraph grant, no C→A  (codegraph_access asymmetric)
    3. No B→A and no C→B leakage    (zero rows == no access)

The third project (C) acts as the negative control — it should
have ONLY its own collections in every list, never see A's or
B's even though they exist in the same launcher.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Allow imports of tests.common.sandbox when run as a script.
_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR.parent))

from tests.common.sandbox import (  # noqa: E402
    SandboxLayout,
    make_sandbox,
    teardown_sandbox,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "migrations"
)


# ─────────────────────────────────────────────────────────────────────
# Project descriptor (input to the fixture)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ProjectSpec:
    """A single fixture project. ``slug`` MUST be unique across the
    fixture (the DB enforces a UNIQUE index on projects.slug)."""

    slug: str
    display_name: str
    # Class prefix per launcher's canonical sanitiser
    # (vco_lib/project_naming.canonical_class_prefix). The fixture
    # uses it both as the codegraph prefix and the front-half of the
    # KG collection name.
    class_prefix: str
    # Filled in after registration:
    project_id: str = ""
    folder_path: Path = field(default=Path())

    def kg_collection_name(self) -> str:
        """Per-project primary KG collection — sandbox prefix is added by caller."""
        return f"{self.class_prefix}_KnowledgeGraph"

    def shared_kg_collection_name(self) -> str:
        return f"{self.class_prefix}_Shared"

    def archive_collection_name(self) -> str:
        return f"{self.class_prefix}_Development"


# Canonical 3-project shape used by the test. The class_prefix values
# are deliberately ASCII-clean so they're safe as Weaviate class
# names with the STEP22_<run_id>_ prefix tacked on the front.
CANONICAL_PROJECTS: tuple[ProjectSpec, ...] = (
    ProjectSpec(slug="proj-a-alpha", display_name="Project A (Alpha)", class_prefix="ProjectAAlpha"),
    ProjectSpec(slug="proj-b-beta", display_name="Project B (Beta)", class_prefix="ProjectBBeta"),
    ProjectSpec(slug="proj-c-gamma", display_name="Project C (Gamma)", class_prefix="ProjectCGamma"),
)


# ─────────────────────────────────────────────────────────────────────
# launcher.db seeding
# ─────────────────────────────────────────────────────────────────────


def _apply_migrations(db_path: Path) -> None:
    """Apply every shipped launcher.db migration in version order.

    Mirrors what ``vct_launcher_core::db::migrations::apply`` does at
    runtime, but in pure Python — no need for a cargo build in the
    fixture setup path.

    Migration 013 toggles ``PRAGMA foreign_keys`` outside its
    transaction. ``sqlite3.executescript`` handles this correctly
    because it issues each `;`-separated statement individually and
    is implicitly outside a transaction when foreign_keys pragmas
    fire. We open the connection with ``isolation_level=None`` so we
    have manual control.
    """
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(
            f"migrations directory not found: {MIGRATIONS_DIR}"
        )
    sql_files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not sql_files:
        raise FileNotFoundError(
            f"no migrations found in {MIGRATIONS_DIR}"
        )

    # Use isolation_level=None so PRAGMA foreign_keys writes outside
    # a transaction (sqlite3's default mode wraps DML in implicit
    # transactions, which would render the off→on toggle in 013 a no-op).
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Build the migrations tracking table the Rust runner uses.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  INTEGER NOT NULL
            )
            """
        )
        already_applied: set[int] = {
            int(row[0])
            for row in conn.execute("SELECT version FROM _schema_migrations")
        }
        for sql_path in sql_files:
            # Parse the leading XXX_ as the version number.
            try:
                version = int(sql_path.name.split("_", 1)[0])
            except ValueError:
                continue
            if version in already_applied:
                continue
            sql = sql_path.read_text(encoding="utf-8")
            conn.executescript(sql)
            now_ms = int(time.time() * 1000)
            conn.execute(
                "INSERT INTO _schema_migrations (version, description, applied_at) "
                "VALUES (?, ?, ?)",
                (version, sql_path.stem, now_ms),
            )
    finally:
        conn.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_project_dir(layout: SandboxLayout, spec: ProjectSpec) -> Path:
    """Create a realistic-looking project directory on disk.

    The hub's resolver does NOT scan project files — it only uses
    ``projects.folder_path``. But a real path is required by the
    UNIQUE constraint and by the resolver's optional path-canonicalize
    check. We materialise a minimal README + a Python module so the
    folder is non-empty (useful for any future hook test that runs
    code-graph-analyze against it).
    """
    assert layout.project_root is not None, "make_sandbox(with_project_root=True) required"
    pdir = layout.project_root / spec.slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "README.md").write_text(
        f"# {spec.display_name}\n\n"
        f"Fixture project for Step 22 access-matrix regression.\n"
        f"slug: `{spec.slug}` class_prefix: `{spec.class_prefix}`\n",
        encoding="utf-8",
    )
    src = pdir / "src"
    src.mkdir(exist_ok=True)
    (src / "main.py").write_text(
        f'"""Synthetic module for {spec.display_name}."""\n\n\n'
        f'def hello() -> str:\n    return "{spec.slug}"\n',
        encoding="utf-8",
    )
    return pdir.resolve()


def _seed_project_row(conn: sqlite3.Connection, spec: ProjectSpec) -> None:
    """INSERT a single project row + its bindings.

    The schema layout (projects column order) tracks migration 013's
    post-rebuild order: id, name, folder_path, host, created_at,
    updated_at, slug.
    """
    now = _now_ms()
    conn.execute(
        """
        INSERT INTO projects
            (id, name, folder_path, host, created_at, updated_at, slug)
        VALUES (?, ?, ?, 'base', ?, ?, ?)
        """,
        (spec.project_id, spec.display_name, str(spec.folder_path), now, now, spec.slug),
    )


def _seed_kg_bindings(
    conn: sqlite3.Connection, spec: ProjectSpec, layout: SandboxLayout
) -> None:
    """Insert primary + shared + archive KG binding rows."""
    now = _now_ms()
    rows = [
        ("primary", layout.collection_name(spec.kg_collection_name())),
        ("shared", layout.collection_name(spec.shared_kg_collection_name())),
        ("archive", layout.collection_name(spec.archive_collection_name())),
    ]
    for role, coll_name in rows:
        conn.execute(
            """
            INSERT INTO project_kg_bindings
                (project_id, role, collection_name, embedding_model,
                 embedding_dim, kg_dir_path, weaviate_url, config_json, updated_at)
            VALUES (?, ?, ?, 'qwen3-embedding:0.6b', 1024, NULL, NULL, '{}', ?)
            """,
            (spec.project_id, role, coll_name, now),
        )
        # Track for teardown.
        if coll_name not in layout.created_collections:
            layout.created_collections.append(coll_name)


def _seed_codegraph_binding(
    conn: sqlite3.Connection, spec: ProjectSpec, layout: SandboxLayout
) -> None:
    """Insert the project_codegraph_bindings row + register the
    expected Weaviate codegraph collections for teardown."""
    now = _now_ms()
    prefix = layout.collection_name(spec.class_prefix)
    conn.execute(
        """
        INSERT INTO project_codegraph_bindings
            (project_id, collection_prefix, embedding_model, embedding_dim,
             last_analyzed_commit, last_analyzed_at, enabled, config_json, updated_at)
        VALUES (?, ?, 'CodeSage-Large-v2', 2048, NULL, NULL, 1, '{}', ?)
        """,
        (spec.project_id, prefix, now),
    )
    # The codegraph analyser creates 5 typed classes per prefix:
    # <Prefix>_CodeFunction, _CodeClass, _CodeModule, _CodeAPI,
    # _CodeInteraction. We register them all for teardown even though
    # the fixture itself only bootstraps the KG side. If a future
    # extension to this test boots code-graph-analyze, every produced
    # class is already on the cleanup list.
    for suffix in (
        "_CodeFunction",
        "_CodeClass",
        "_CodeModule",
        "_CodeAPI",
        "_CodeInteraction",
    ):
        coll = f"{prefix}{suffix}"
        if coll not in layout.created_collections:
            layout.created_collections.append(coll)


def _seed_module_settings(conn: sqlite3.Connection, spec: ProjectSpec) -> None:
    """Set the active_embedding module setting required by the resolver.

    The resolver defaults this to 'qwen3' when missing, but writing
    the row explicitly catches the case where the fixture's seeding
    drifts from the resolver's expectations.
    """
    conn.execute(
        """
        INSERT INTO module_settings
            (project_id, module_id, setting_key, setting_value)
        VALUES (?, 'orchestrator-core', 'active_embedding', ?)
        """,
        (spec.project_id, json.dumps("qwen3")),
    )


def _seed_access_matrix(
    conn: sqlite3.Connection, projects: list[ProjectSpec], layout: SandboxLayout
) -> None:
    """Apply the canonical Step 22 access-matrix asymmetries.

    Layout (A, B, C are the three projects in order):

    KG access (kg_collection_access):
        A → reads B's primary KG          (grant from A's perspective)
        B → reads NOTHING extra (own only)
        C → reads NOTHING extra (own only)

    Codegraph access (codegraph_access; row = "<grantor> grants <grantee> read"):
        A grants B read on A's codegraph
        (nothing else)

    Resulting `codegraph_access_list` (grantor slugs the project can READ):
        A: ['A']                  ← own slug always implicit
        B: ['A', 'B']             ← B was granted read on A; own slug too
        C: ['C']                  ← own slug only
    """
    a, b, c = projects
    # ── KG access matrix ───────────────────────────────────────────
    # A explicitly reads B's primary KG.
    conn.execute(
        """
        INSERT INTO kg_collection_access (project_id, collection_name, access_level)
        VALUES (?, ?, 'read')
        """,
        (a.project_id, layout.collection_name(b.kg_collection_name())),
    )
    # Defense-in-depth: explicitly write 'none' for C → B's primary
    # KG so the test can prove the resolver filters 'none' rows out
    # (not just "row absent === implicit deny"). 'none' is the
    # explicit-deny form documented in the access matrix CHECK constraint.
    conn.execute(
        """
        INSERT INTO kg_collection_access (project_id, collection_name, access_level)
        VALUES (?, ?, 'none')
        """,
        (c.project_id, layout.collection_name(b.kg_collection_name())),
    )
    # ── Codegraph grants ───────────────────────────────────────────
    now = _now_ms()
    # A grants B read access to A's codegraph.
    conn.execute(
        """
        INSERT INTO codegraph_access
            (grantor_project_id, grantee_project_id, access_level, granted_at)
        VALUES (?, ?, 'read', ?)
        """,
        (a.project_id, b.project_id, now),
    )


# ─────────────────────────────────────────────────────────────────────
# Top-level fixture orchestration
# ─────────────────────────────────────────────────────────────────────


@dataclass
class FixtureResult:
    """Output of :func:`build_fixture` — passed to the test as-is."""

    layout: SandboxLayout
    projects: list[ProjectSpec]

    def project_by_slug(self, slug: str) -> ProjectSpec:
        for p in self.projects:
            if p.slug == slug:
                return p
        raise KeyError(f"no project with slug {slug!r} in fixture")


def build_fixture(
    *,
    run_id: Optional[str] = None,
    runner_temp: Optional[Path] = None,
    bootstrap_weaviate: bool = False,
    weaviate_url: Optional[str] = None,
) -> FixtureResult:
    """Materialise the full fixture state.

    On return, ``$VCT_STATE_DIR/launcher.db`` contains 3 projects with
    the canonical access-matrix shape. The hub is NOT started here —
    the caller (pytest fixture or workflow step) does that, so the
    test can choose to start it directly or via the
    ``vct-hub start-if-not-running`` codepath.

    If ``bootstrap_weaviate=True``, the fixture also issues
    PUT /v1/schema requests to create each KG class with a trivial
    text2vec-none vectoriser (test data, no embedding model needed).
    Many test paths don't need this — the resolver returns
    ``kg_access_list`` directly from launcher.db without consulting
    Weaviate.
    """
    layout = make_sandbox(
        run_id=run_id,
        runner_temp=runner_temp,
        with_project_root=True,
    )

    # Apply schema migrations.
    db_path = layout.launcher_db_path()
    _apply_migrations(db_path)

    # Clone the canonical project specs so we can mutate them with
    # generated IDs without polluting the module-level constant.
    projects = [
        ProjectSpec(
            slug=p.slug,
            display_name=p.display_name,
            class_prefix=p.class_prefix,
        )
        for p in CANONICAL_PROJECTS
    ]
    for p in projects:
        p.project_id = str(uuid.uuid4())
        p.folder_path = _make_project_dir(layout, p)

    # Open a single connection in autocommit mode for the data writes.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for p in projects:
            _seed_project_row(conn, p)
            _seed_kg_bindings(conn, p, layout)
            _seed_codegraph_binding(conn, p, layout)
            _seed_module_settings(conn, p)
        _seed_access_matrix(conn, projects, layout)
        conn.execute("PRAGMA foreign_key_check")
    finally:
        conn.close()

    if bootstrap_weaviate:
        _bootstrap_weaviate_classes(
            layout, projects, weaviate_url=weaviate_url
        )

    return FixtureResult(layout=layout, projects=projects)


def _bootstrap_weaviate_classes(
    layout: SandboxLayout,
    projects: list[ProjectSpec],
    *,
    weaviate_url: Optional[str] = None,
) -> None:
    """Create every collection in layout.created_collections via the
    Weaviate REST API. Uses ``text2vec-none`` so the test doesn't need
    a real embedder running."""
    import urllib.error
    import urllib.request

    base = (weaviate_url or os.environ.get("WEAVIATE_URL", "http://localhost:8081")).rstrip("/")
    for coll in layout.created_collections:
        payload = {
            "class": coll,
            "description": f"Step 22 fixture class (run {layout.run_id})",
            "vectorizer": "none",
            "properties": [
                {"name": "title", "dataType": ["text"]},
                {"name": "content", "dataType": ["text"]},
            ],
        }
        req = urllib.request.Request(
            f"{base}/v1/schema",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status not in (200, 201):
                    print(
                        f"[step22-fixture] weaviate create {coll} returned HTTP {resp.status}",
                        file=sys.stderr,
                    )
        except urllib.error.HTTPError as e:
            if e.code == 422:
                # Already exists — acceptable on retried runs.
                continue
            raise


# ─────────────────────────────────────────────────────────────────────
# Hub lifecycle (subprocess wrapper)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class HubProc:
    """A running vct-hub subprocess. ``stop()`` is idempotent."""

    proc: subprocess.Popen
    layout: SandboxLayout
    port: int
    token: str

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)


def start_hub(
    layout: SandboxLayout,
    hub_binary: Path,
    *,
    startup_timeout_s: float = 10.0,
) -> HubProc:
    """Spawn vct-hub against ``layout.state_dir`` and wait for
    ``hub.port`` + ``hub.token`` to appear (the hub writes both before
    accepting connections, so their presence is sufficient to know
    /health will respond).
    """
    if not hub_binary.exists():
        raise FileNotFoundError(
            f"vct-hub binary not found at {hub_binary}; build with "
            f"`cargo build --release -p vct-hub` from launcher/src-tauri/"
        )

    env = {
        **os.environ,
        "VCT_STATE_DIR": str(layout.state_dir),
        "VCO_CI_FIXTURE": "1",
        # Force a free port by setting VCT_HUB_PORT=0 — the server
        # binds to a random port and writes the actual port to
        # hub.port. Step 22 cannot use 7700 because the developer's
        # real launcher may already be running on it.
        "VCT_HUB_PORT": "0",
    }
    # Clean stale port/token files so we know the values we read back
    # were freshly produced by this subprocess.
    for f in (layout.hub_port_file(), layout.hub_token_file(), layout.hub_pid_file()):
        if f.exists():
            f.unlink()

    proc = subprocess.Popen(
        [str(hub_binary)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.time() + startup_timeout_s
    last_err: Optional[str] = None
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(
                f"vct-hub exited early with code {proc.returncode}:\n{err}"
            )
        port_file = layout.hub_port_file()
        token_file = layout.hub_token_file()
        if port_file.exists() and token_file.exists():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
                token = token_file.read_text(encoding="utf-8").strip()
                if port > 0 and token:
                    return HubProc(proc=proc, layout=layout, port=port, token=token)
            except ValueError as e:
                last_err = str(e)
        time.sleep(0.1)

    # Timeout — tear the subprocess down and surface what we know.
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = ""
    if proc.stderr:
        try:
            err = proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    raise TimeoutError(
        f"vct-hub did not write hub.port + hub.token within "
        f"{startup_timeout_s}s (last_err={last_err!r}); stderr: {err}"
    )


# ─────────────────────────────────────────────────────────────────────
# Command-line entrypoint (for the CI workflow + local smoke checks)
# ─────────────────────────────────────────────────────────────────────


def _cli_main(argv: list[str]) -> int:
    """``python -m tests.integration.step22_multi_project.fixture <command>``

    Commands:
        build       — set up the fixture; print VCT_STATE_DIR + 3 project paths
        teardown    — drop the sandbox at $VCT_STATE_DIR
        info        — print a JSON snapshot of the seeded launcher.db rows
    """
    import argparse

    p = argparse.ArgumentParser(description="Step 22 multi-project fixture")
    p.add_argument(
        "command",
        choices=("build", "teardown", "info"),
        help="action to perform",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("VCO_TEST_RUN_ID") or None,
        help="explicit RUN_ID (defaults to $GITHUB_RUN_ID or random)",
    )
    p.add_argument(
        "--bootstrap-weaviate",
        action="store_true",
        help="also create Weaviate classes (only on build)",
    )
    p.add_argument(
        "--weaviate-url",
        default=os.environ.get("WEAVIATE_URL", "http://localhost:8081"),
    )
    args = p.parse_args(argv)

    if args.command == "build":
        fx = build_fixture(
            run_id=args.run_id,
            bootstrap_weaviate=args.bootstrap_weaviate,
            weaviate_url=args.weaviate_url,
        )
        # Print one machine-parseable line + a human summary on stderr.
        out = {
            "run_id": fx.layout.run_id,
            "vct_state_dir": str(fx.layout.state_dir),
            "launcher_db": str(fx.layout.launcher_db_path()),
            "projects": [
                {
                    "slug": s.slug,
                    "project_id": s.project_id,
                    "folder_path": str(s.folder_path),
                    "class_prefix": s.class_prefix,
                    "kg_collection": fx.layout.collection_name(s.kg_collection_name()),
                }
                for s in fx.projects
            ],
            "created_collections": fx.layout.created_collections,
        }
        print(json.dumps(out, indent=2))
        print(
            f"[step22-fixture] built sandbox at {fx.layout.state_dir} "
            f"(run_id={fx.layout.run_id}, 3 projects seeded)",
            file=sys.stderr,
        )
        return 0

    if args.command == "teardown":
        # Reconstruct a layout from env + run_id. We do NOT call
        # make_sandbox (it would refuse if VCO_CI_FIXTURE was not set
        # in the same shell). Build a minimal layout by hand.
        rid = args.run_id or os.environ.get("VCO_TEST_RUN_ID", "")
        if not rid:
            print("[step22-fixture] teardown: --run-id required (or $VCO_TEST_RUN_ID)", file=sys.stderr)
            return 64
        runner_temp = Path(
            os.environ.get("RUNNER_TEMP") or "/tmp"
        )
        layout = SandboxLayout(
            run_id=rid,
            state_dir=runner_temp / f".vct-step22-{rid}",
            collection_prefix=f"STEP22_{rid}_",
            keychain_module_prefix=f"step22-{rid}-",
        )
        # If launcher.db is present, list every collection name so we
        # drop them all (otherwise teardown is a state-dir rm only).
        db = layout.launcher_db_path()
        if db.exists():
            try:
                conn = sqlite3.connect(str(db))
                try:
                    for (coll,) in conn.execute(
                        "SELECT collection_name FROM project_kg_bindings"
                    ):
                        if coll not in layout.created_collections:
                            layout.created_collections.append(coll)
                    for (prefix,) in conn.execute(
                        "SELECT collection_prefix FROM project_codegraph_bindings"
                    ):
                        for suffix in (
                            "_CodeFunction",
                            "_CodeClass",
                            "_CodeModule",
                            "_CodeAPI",
                            "_CodeInteraction",
                        ):
                            c = f"{prefix}{suffix}"
                            if c not in layout.created_collections:
                                layout.created_collections.append(c)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                print(f"[step22-fixture] teardown: db read failed: {e}", file=sys.stderr)
        notes = teardown_sandbox(
            layout, weaviate_url=args.weaviate_url, drop_weaviate_collections=True
        )
        for n in notes:
            print(n, file=sys.stderr)
        return 0

    if args.command == "info":
        # Read back the seeded rows from the existing launcher.db.
        rid = args.run_id or os.environ.get("VCO_TEST_RUN_ID", "")
        if not rid:
            print("[step22-fixture] info: --run-id required", file=sys.stderr)
            return 64
        runner_temp = Path(os.environ.get("RUNNER_TEMP") or "/tmp")
        state_dir = runner_temp / f".vct-step22-{rid}"
        db = state_dir / "launcher.db"
        if not db.exists():
            print(f"[step22-fixture] info: no launcher.db at {db}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db))
        try:
            projects = [
                {"id": r[0], "slug": r[1], "name": r[2], "folder_path": r[3]}
                for r in conn.execute(
                    "SELECT id, slug, name, folder_path FROM projects ORDER BY slug"
                )
            ]
            kg_access = [
                {"project_id": r[0], "collection": r[1], "level": r[2]}
                for r in conn.execute(
                    "SELECT project_id, collection_name, access_level "
                    "FROM kg_collection_access ORDER BY project_id"
                )
            ]
            cg_access = [
                {"grantor": r[0], "grantee": r[1], "level": r[2]}
                for r in conn.execute(
                    "SELECT grantor_project_id, grantee_project_id, access_level "
                    "FROM codegraph_access ORDER BY grantor_project_id"
                )
            ]
        finally:
            conn.close()
        print(json.dumps({"projects": projects, "kg_access": kg_access, "codegraph_access": cg_access}, indent=2))
        return 0

    return 64  # unreachable


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
