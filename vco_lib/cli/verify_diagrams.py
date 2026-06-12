# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco verify-diagrams`` — end-to-end Diagrams Integration verifier.

The Phase 0/1/1.5/2/3 Diagrams Integration feature has many moving parts:

    * SQLite migration 022 (``project_modules``, ``project_diagrams``,
      ``diagram_snapshots``, ``diagram_access``,
      ``project_mcp_tool_grants``, ``diagram_index_retry``).
    * A ``project_modules('diagrams', enabled=1)`` row seeded at project
      create time.
    * Wrapper MCPs (``mermaid_proxy`` / ``excalidraw_proxy``) registered
      in ``~/.claude.json``.
    * ``vct-hub`` route serving per-MCP per-tool allowlists
      (``GET /api/v1/projects/{id}/mcp-tool-grants/{mcp}``).
    * A per-project Weaviate ``<Project>_Diagrams`` class bootstrapped on
      project init.
    * ``DIAGRAMS_COLLECTION`` + ``VCT_DIAGRAMS_ACCESS_LIST`` env vars
      projected to all three on-disk surfaces.
    * PreToolUse + PostToolUse hooks registered (path-validation guard
      for ``Write|Edit`` + ``mcp__mermaid__.*|mcp__excalidraw__.*``;
      delete cascade on Bash ``rm``).
    * The hook scripts physically present on disk (``.sh`` + ``.ps1``).
    * ``vco_lib.diagram_indexer`` + ``vco_lib.diagram_paths`` importable
      with their key functions resolvable.
    * Conditionally-rendered CLAUDE.md section.

A user installing VCO needs a single command that answers: "is the
diagrams feature actually wired correctly on this machine for this
project?". That command is ``vco verify-diagrams <project>``.

Exit-code policy (mirrors :mod:`vco_lib.cli.verify`):

* ``0`` — every check returned OK or SKIP.
* ``1`` — at least one check returned FAIL.
* ``2`` — environment problem the verifier cannot work around
  (project not in launcher DB, launcher DB unreadable).
* ``3`` — ``--fix`` ran but failed to repair at least one check.

Flags:

* ``--json`` — single JSON object on stdout (machine-readable).
* ``--fix`` — best-effort repair where possible; log+continue per-check
  on failure (unlike ``verify-pins`` / ``verify-env-projection`` which
  abort on first failure, the diagrams feature has heterogeneous
  fixers; aborting on first failure would hide later problems the user
  also needs to fix).
* ``--all`` — iterate every registered project.
* ``--quick`` — skip the slow checks (no Weaviate connectivity, no
  hub HTTP probes). Useful in CI / pre-commit hooks.

Cross-OS rules (see ``knowledge/concepts/cross-os-hook-portability.md``):

* All filesystem paths flow through :class:`pathlib.Path`.
* External binary probes go through :func:`shutil.which` (cached).
* HTTP via :mod:`urllib.request` (stdlib only — no ``requests`` dep).
* Hook-script presence checks accept either ``.sh`` (Unix) or ``.ps1``
  (Windows); platform mismatch downgrades to SKIP, not FAIL.

Cross-module dependencies:

* :mod:`vco_lib.config_projection` for ``resolve_project_folder`` /
  ``list_registered_projects`` (Phase 0.B). Imported lazily so this
  module's import doesn't fail when 0.B Part 2 hasn't merged.
* :mod:`vco_lib.diagram_indexer` for the importability check.
* :mod:`vco_lib.diagram_paths` for the round-trip check.
* :mod:`vco_lib.paths` for ``vct_root_dir()`` (launcher DB lookup).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Exit-code constants — keep stable; tests assert against these.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV_PROBLEM = 2
EXIT_FIX_FAILED = 3


# ---------------------------------------------------------------------------
# Per-check status labels.
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_FIXED = "fixed"   # like OK, but reached via --fix
STATUS_FIX_FAILED = "fix_failed"


# ---------------------------------------------------------------------------
# Phase 0.B dependency wrappers — mirror :mod:`vco_lib.cli.verify` /
# :mod:`vco_lib.cli.rebuild_diagram_index`. Tests monkey-patch these.
# ---------------------------------------------------------------------------


def _resolve_project_folder(project_id: str) -> Path:
    """Phase 0.B Part 2 dependency wrapper.

    Resolves a project id (UUID) or slug to its on-disk folder via
    :func:`vco_lib.config_projection.resolve_project_folder`. Kept as a
    thin wrapper so tests can monkey-patch ``_resolve_project_folder``
    on this module without touching the shared ``config_projection``
    symbol.

    Raises:
        LookupError: when no project matches the supplied id/slug.
    """
    from vco_lib.config_projection import resolve_project_folder
    return resolve_project_folder(project_id)


def _list_registered_projects() -> Iterable[Mapping[str, str]]:
    """Phase 0.B Part 2 dependency wrapper. Drives ``--all``.

    Returns ``{"id", "name", "slug", "folder_path", "folder"}`` dicts;
    ``folder`` is a back-compat alias for ``folder_path``.
    """
    from vco_lib.config_projection import list_registered_projects
    return list_registered_projects()


def _resolve_launcher_db_path() -> Path:
    """Launcher DB path. Thin wrapper for monkey-patching in tests."""
    try:
        from vco_lib.config_projection import _resolve_launcher_db_path as _resolve  # type: ignore  # noqa: E501
        return _resolve()
    except ImportError:
        from vco_lib.paths import vct_root_dir
        return vct_root_dir() / "launcher.db"


def _project_env_from_db(project_id: str) -> Mapping[str, str]:
    """Phase 0.B dependency wrapper — canonical env projection."""
    try:
        from vco_lib.config_projection import project_env_from_db  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.project_env_from_db() is not "
            "available. This subcommand requires Phase 0.B to be merged."
        ) from exc
    bundle = project_env_from_db(project_id)
    # ``project_env_from_db`` returns a ProjectEnvBundle TypedDict whose
    # ``canonical_env`` attribute holds the flat env map. We accept both
    # shapes for forward-compat with potential refactors.
    if isinstance(bundle, Mapping) and "canonical_env" in bundle:
        env = bundle["canonical_env"]
        if isinstance(env, Mapping):
            return {str(k): str(v) for k, v in env.items()}
    if isinstance(bundle, Mapping):
        return {str(k): str(v) for k, v in bundle.items()}
    return {}


# ---------------------------------------------------------------------------
# Per-check result type.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _CheckResult:
    """Outcome of a single check.

    Fields:
        name: stable identifier for the check (snake_case, used as a
            JSON key and as the "[OK]/[FAIL]/[SKIP] name" prefix in the
            human-readable output).
        status: one of :data:`STATUS_OK`, :data:`STATUS_FAIL`,
            :data:`STATUS_SKIP`, :data:`STATUS_FIXED`,
            :data:`STATUS_FIX_FAILED`.
        detail: human-readable explanation. Always set; for OK rows
            it's a one-liner like "applied" or "row present".
        fix_hint: optional remediation hint shown on FAIL rows.
    """

    name: str
    status: str
    detail: str
    fix_hint: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.fix_hint:
            out["fix_hint"] = self.fix_hint
        return out


# ---------------------------------------------------------------------------
# Check 1: project row exists in launcher DB.
# ---------------------------------------------------------------------------


def _open_db_ro(db_path: Path) -> sqlite3.Connection:
    """Open the launcher DB read-only (URI mode for cross-platform).

    Read-only opens are CRITICAL — the launcher is the only writer; the
    verifier is a client. Opening read-write would create a WAL file
    owned by Python's process and disrupt the launcher's connection
    lifecycle (lesson from :mod:`vco_lib.config_projection`).
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"launcher.db not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _open_db_rw(db_path: Path) -> sqlite3.Connection:
    """Open the launcher DB read-write (for ``--fix`` paths only).

    The verifier writes to ``project_modules`` ONLY when ``--fix`` is set
    and the launcher is presumed quiesced. The single-writer invariant
    is documented in config_projection.py — see the read-only note there
    for why ``--fix`` callers must accept this risk explicitly.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"launcher.db not found at {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _check_project_row(project_id: str) -> tuple[_CheckResult, Optional[Mapping[str, Any]]]:
    """Check 1 + side-effect: also return the resolved project row so
    subsequent checks can use it (``folder_path``, ``name``).
    """
    try:
        db_path = _resolve_launcher_db_path()
    except Exception as exc:
        return _CheckResult(
            "project_row",
            STATUS_FAIL,
            f"cannot resolve launcher.db path: {exc}",
            fix_hint="ensure VCT_ROOT_DIR / ~/.vct is initialised",
        ), None
    try:
        conn = _open_db_ro(db_path)
    except FileNotFoundError as exc:
        return _CheckResult(
            "project_row",
            STATUS_FAIL,
            str(exc),
            fix_hint="launch the orchestrator GUI once to seed launcher.db",
        ), None
    except sqlite3.OperationalError as exc:
        return _CheckResult(
            "project_row",
            STATUS_FAIL,
            f"cannot open launcher.db: {exc}",
            fix_hint="check file permissions on launcher.db",
        ), None
    try:
        cur = conn.cursor()
        # Accept either rowid (id) or slug — same dual-lookup the rest
        # of the codebase uses.
        cur.execute(
            "SELECT id, name, folder_path, slug FROM projects "
            "WHERE id = ? OR slug = ? LIMIT 1",
            (project_id, project_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return _CheckResult(
            "project_row",
            STATUS_FAIL,
            f"no project row matched id-or-slug {project_id!r}",
            fix_hint="register the project via the launcher GUI first",
        ), None
    return _CheckResult(
        "project_row",
        STATUS_OK,
        f"project {row['name']!r} (id={row['id']})",
    ), {"id": row["id"], "name": row["name"], "folder_path": row["folder_path"], "slug": row["slug"]}


# ---------------------------------------------------------------------------
# Check 2: project_modules('diagrams', enabled=1) row present.
# ---------------------------------------------------------------------------


def _check_project_modules_row(
    project_id: str, *, fix: bool
) -> _CheckResult:
    try:
        db_path = _resolve_launcher_db_path()
        conn = _open_db_ro(db_path)
    except Exception as exc:
        return _CheckResult(
            "project_modules_row",
            STATUS_FAIL,
            f"cannot open launcher.db: {exc}",
        )
    try:
        cur = conn.cursor()
        # Migration 022 may not yet have run on this DB → trap that and
        # produce a more useful detail.
        try:
            cur.execute(
                "SELECT enabled FROM project_modules "
                "WHERE project_id = ? AND module_name = 'diagrams'",
                (project_id,),
            )
            row = cur.fetchone()
        except sqlite3.OperationalError as exc:
            return _CheckResult(
                "project_modules_row",
                STATUS_FAIL,
                f"project_modules table missing or unreadable: {exc}",
                fix_hint="run migration 022 (launcher will apply on next start)",
            )
    finally:
        conn.close()
    if row is not None and int(row["enabled"]) == 1:
        return _CheckResult(
            "project_modules_row",
            STATUS_OK,
            "project_modules('diagrams', enabled=1) row present",
        )
    if not fix:
        return _CheckResult(
            "project_modules_row",
            STATUS_FAIL,
            (
                "row missing"
                if row is None
                else f"row present but enabled={int(row['enabled'])}"
            ),
            fix_hint="re-run with --fix or toggle on in launcher DiagramsTab",
        )
    # --fix path: UPSERT the row.
    try:
        conn = _open_db_rw(db_path)
    except Exception as exc:
        return _CheckResult(
            "project_modules_row",
            STATUS_FIX_FAILED,
            f"cannot open launcher.db read-write: {exc}",
        )
    try:
        import time
        now = int(time.time())
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO project_modules "
            "(project_id, module_name, enabled, registered_at) "
            "VALUES (?, 'diagrams', 1, ?) "
            "ON CONFLICT(project_id, module_name) DO UPDATE SET "
            "enabled = 1",
            (project_id, now),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        return _CheckResult(
            "project_modules_row",
            STATUS_FIX_FAILED,
            f"INSERT failed: {exc}",
        )
    finally:
        conn.close()
    return _CheckResult(
        "project_modules_row",
        STATUS_FIXED,
        "row inserted/updated to enabled=1",
    )


# ---------------------------------------------------------------------------
# Check 3: migration 022 applied + diagrams tables exist.
# ---------------------------------------------------------------------------


DIAGRAMS_TABLES: tuple[str, ...] = (
    "project_diagrams",
    "diagram_snapshots",
    "diagram_access",
    "project_mcp_tool_grants",
    "project_modules",
    "diagram_index_retry",
)


def _check_migration_022() -> _CheckResult:
    try:
        db_path = _resolve_launcher_db_path()
        conn = _open_db_ro(db_path)
    except Exception as exc:
        return _CheckResult(
            "migration_022",
            STATUS_FAIL,
            f"cannot open launcher.db: {exc}",
        )
    try:
        cur = conn.cursor()
        # Migration tracking table — see migrations.rs::_schema_migrations.
        try:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) AS v "
                "FROM _schema_migrations"
            )
            row = cur.fetchone()
            max_version = int(row["v"]) if row else 0
        except sqlite3.OperationalError:
            return _CheckResult(
                "migration_022",
                STATUS_FAIL,
                "_schema_migrations table absent — launcher never started",
                fix_hint="launch the orchestrator GUI once",
            )
        # Confirm every diagrams table exists. Belt-and-braces: a corrupt
        # migration could leave _schema_migrations.v >= 22 with one of
        # the tables missing.
        missing: list[str] = []
        for table in DIAGRAMS_TABLES:
            cur.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name=? LIMIT 1",
                (table,),
            )
            if cur.fetchone() is None:
                missing.append(table)
    finally:
        conn.close()
    if max_version < 22:
        return _CheckResult(
            "migration_022",
            STATUS_FAIL,
            f"max(_schema_migrations.version)={max_version} < 22",
            fix_hint="restart the launcher to apply pending migrations",
        )
    if missing:
        return _CheckResult(
            "migration_022",
            STATUS_FAIL,
            f"migration 022 marked applied but tables missing: {missing}",
            fix_hint="DB corruption — restore from backup or re-init",
        )
    return _CheckResult(
        "migration_022",
        STATUS_OK,
        f"migration 22 applied + all {len(DIAGRAMS_TABLES)} tables present",
    )


# ---------------------------------------------------------------------------
# Check 4: MCP wrappers registered in ~/.claude.json.
# ---------------------------------------------------------------------------


def _claude_json_path() -> Path:
    """Path to the user's ``~/.claude.json``. Monkey-patched in tests."""
    return Path.home() / ".claude.json"


def _check_mcp_wrappers() -> _CheckResult:
    path = _claude_json_path()
    if not path.is_file():
        return _CheckResult(
            "mcp_wrappers",
            STATUS_FAIL,
            f"~/.claude.json absent at {path}",
            fix_hint="run install.py to register MCPs",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _CheckResult(
            "mcp_wrappers",
            STATUS_FAIL,
            f"cannot parse ~/.claude.json: {exc}",
        )
    servers = payload.get("mcpServers") or {}
    if not isinstance(servers, Mapping):
        return _CheckResult(
            "mcp_wrappers",
            STATUS_FAIL,
            "mcpServers is not an object in ~/.claude.json",
        )
    missing: list[str] = []
    wrong_module: list[str] = []
    expected_modules = {
        "mermaid": "claude_mcp_servers.wrappers.mermaid_proxy",
        "excalidraw": "claude_mcp_servers.wrappers.excalidraw_proxy",
    }
    for name, module in expected_modules.items():
        entry = servers.get(name)
        if not isinstance(entry, Mapping):
            missing.append(name)
            continue
        args = entry.get("args")
        if not isinstance(args, list) or module not in args:
            wrong_module.append(
                f"{name}: expected -m {module} in args, got {args!r}"
            )
    if missing:
        return _CheckResult(
            "mcp_wrappers",
            STATUS_FAIL,
            f"mcpServers missing entries: {missing}",
            fix_hint="re-run install.py to re-register wrapper MCPs",
        )
    if wrong_module:
        return _CheckResult(
            "mcp_wrappers",
            STATUS_FAIL,
            "wrapper(s) point at unexpected module: " + "; ".join(wrong_module),
            fix_hint="re-run install.py to fix the wrapper command line",
        )
    return _CheckResult(
        "mcp_wrappers",
        STATUS_OK,
        "mermaid + excalidraw wrappers registered with correct module path",
    )


# ---------------------------------------------------------------------------
# Check 5: hub serves allowlist for mermaid + excalidraw.
# ---------------------------------------------------------------------------


def _vct_hub_base_url() -> str:
    """Resolve the hub base URL. ``$VCT_HUB_PORT`` honoured; fallback to
    7700 (the default documented in CLAUDE.md)."""
    port = os.environ.get("VCT_HUB_PORT", "7700").strip() or "7700"
    return f"http://127.0.0.1:{port}"


def _vct_hub_token() -> Optional[str]:
    """Resolve the hub bearer token. Order: ``$VCT_HUB_TOKEN`` →
    ``<vct_root>/hub.token``. Returns ``None`` if unavailable; callers
    SKIP the check (cannot probe without auth)."""
    token = os.environ.get("VCT_HUB_TOKEN")
    if token:
        return token.strip()
    try:
        from vco_lib.paths import vct_root_dir
        token_path = vct_root_dir() / "hub.token"
        if token_path.is_file():
            return token_path.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None
    return None


def _http_get_json(url: str, token: Optional[str], *, timeout: float = 5.0) -> Any:
    """Stdlib GET → JSON. Returns the parsed payload or raises a
    :class:`urllib.error.URLError` / :class:`json.JSONDecodeError`.
    """
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — stdlib http call
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _check_hub_allowlist(project_id: str) -> _CheckResult:
    base = _vct_hub_base_url()
    token = _vct_hub_token()
    if token is None:
        return _CheckResult(
            "hub_allowlist",
            STATUS_SKIP,
            "no hub token available (set $VCT_HUB_TOKEN or start the hub)",
        )
    failures: list[str] = []
    for mcp_name in ("mermaid", "excalidraw"):
        url = f"{base}/api/v1/projects/{project_id}/mcp-tool-grants/{mcp_name}"
        try:
            payload = _http_get_json(url, token)
        except urllib.error.URLError as exc:
            return _CheckResult(
                "hub_allowlist",
                STATUS_SKIP,
                f"hub not reachable at {base}: {exc.reason}",
            )
        except Exception as exc:
            failures.append(f"{mcp_name}: HTTP/JSON error: {exc}")
            continue
        # The hub returns at least ``{"default_allow_all": bool}`` plus
        # potentially a list of denied/allowed tools. Treat ANY successful
        # JSON response as "the route exists" — the route's CONTENT is
        # validated by the hub's own tests; we only verify the route is
        # alive for THIS project.
        if not isinstance(payload, Mapping):
            failures.append(f"{mcp_name}: response is not a JSON object")
    if failures:
        return _CheckResult(
            "hub_allowlist",
            STATUS_FAIL,
            "; ".join(failures),
            fix_hint="restart vct-hub: vct-hub --stop && vct-hub --start-if-not-running",
        )
    return _CheckResult(
        "hub_allowlist",
        STATUS_OK,
        "hub serves allowlist routes for mermaid + excalidraw",
    )


# ---------------------------------------------------------------------------
# Check 6: env vars projected to all three surfaces.
# ---------------------------------------------------------------------------


# Keys this verifier checks. ``KG_COLLECTION`` is canonical (always
# present); ``DIAGRAMS_COLLECTION`` and ``VCT_DIAGRAMS_ACCESS_LIST`` are
# diagrams-specific. The latter two are NOT yet in
# ``vco_lib.config_projection._CANONICAL_KEYS`` (Phase 0.B was authored
# before the diagrams feature gained dedicated env vars); when the
# projection adds them, this verifier picks them up automatically via
# :func:`_project_env_from_db`.
DIAGRAMS_ENV_KEYS: tuple[str, ...] = (
    "KG_COLLECTION",
    "DIAGRAMS_COLLECTION",
    "VCT_DIAGRAMS_ACCESS_LIST",
)


def _read_claude_settings_env(folder: Path) -> dict[str, str]:
    path = folder / ".claude" / "settings.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = payload.get("env")
    if not isinstance(env, Mapping):
        return {}
    return {str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))}


def _read_claude_env(folder: Path) -> dict[str, str]:
    path = folder / ".claude" / "env"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                out[key] = value
    except OSError:
        return {}
    return out


def _read_vscode_settings_env(folder: Path) -> dict[str, str]:
    path = folder / ".vscode" / "settings.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = payload.get("claude-code.env")
    if not isinstance(env, Mapping):
        return {}
    return {str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))}


def _check_env_projection(
    project_id: str, project_folder: Path, *, fix: bool
) -> _CheckResult:
    try:
        expected = _project_env_from_db(project_id)
    except Exception as exc:
        return _CheckResult(
            "env_projection",
            STATUS_FAIL,
            f"cannot project env from DB: {exc}",
        )
    surfaces = {
        ".claude/settings.json": _read_claude_settings_env(project_folder),
        ".claude/env": _read_claude_env(project_folder),
        ".vscode/settings.json": _read_vscode_settings_env(project_folder),
    }
    drift: list[str] = []
    for key in DIAGRAMS_ENV_KEYS:
        want = expected.get(key)
        if want is None:
            # Key not in canonical projection — the diagrams feature
            # may not have added it yet (Phase 0.B gap). Report SKIP-
            # rationale rather than FAIL so users with otherwise-fine
            # installs don't see a noisy error.
            drift.append(f"{key}: not in canonical projection (Phase 0.B gap)")
            continue
        for surface_name, values in surfaces.items():
            have = values.get(key)
            if have != want:
                drift.append(
                    f"{key} on {surface_name}: expected {want!r}, got {have!r}"
                )
    if not drift:
        return _CheckResult(
            "env_projection",
            STATUS_OK,
            f"all {len(DIAGRAMS_ENV_KEYS)} keys present on all 3 surfaces",
        )
    if not fix:
        return _CheckResult(
            "env_projection",
            STATUS_FAIL,
            f"{len(drift)} drift entries: " + "; ".join(drift[:3])
            + ("..." if len(drift) > 3 else ""),
            fix_hint="vco verify-env-projection {project_id} --fix".format(
                project_id=project_id
            ),
        )
    # --fix path: delegate to apply_project_env (single-writer contract).
    #
    # Contract (vco_lib.config_projection.apply_project_env):
    #   def apply_project_env(bundle: ProjectEnvBundle, *, surfaces=...)
    #       -> dict[str, list[str]]
    # It takes a full ProjectEnvBundle TypedDict (NOT a flat env mapping
    # and NOT a project_folder kwarg) and returns a report mapping each
    # written surface to the list of canonical keys it wrote. Failure is
    # signalled by raising ConfigProjectionError, not by an "ok" field.
    #
    # We re-resolve the bundle here (it's a cheap DB read) rather than
    # threading it through _project_env_from_db so the lazy-import
    # wrapper's flat-env return type stays unchanged for the drift-only
    # branch above.
    try:
        from vco_lib.config_projection import (  # type: ignore
            apply_project_env,
            project_env_from_db,
        )
    except ImportError as exc:
        return _CheckResult(
            "env_projection",
            STATUS_FIX_FAILED,
            f"apply_project_env unavailable: {exc}",
        )
    try:
        bundle = project_env_from_db(project_id)
    except Exception as exc:
        return _CheckResult(
            "env_projection",
            STATUS_FIX_FAILED,
            f"project_env_from_db raised: {exc}",
        )
    try:
        # Write to all 3 surfaces — the drift-detection above reads all
        # 3, so a half-write would re-fail on the next verify run.
        report = apply_project_env(
            bundle,
            surfaces=(
                "claude_settings_json",
                "claude_env",
                "vscode_settings_json",
            ),
        )
    except Exception as exc:
        return _CheckResult(
            "env_projection",
            STATUS_FIX_FAILED,
            f"apply_project_env raised: {exc}",
        )
    # apply_project_env signals success by returning without raising;
    # the dict it returns maps each written surface to the canonical
    # keys that landed. Defensive: a non-Mapping return (e.g. a mocked-
    # out test stub) is treated as success since no exception was
    # raised.
    surfaces_written = (
        sorted(report.keys()) if isinstance(report, Mapping) else []
    )
    return _CheckResult(
        "env_projection",
        STATUS_FIXED,
        f"projected {len(expected)} canonical keys to "
        f"{len(surfaces_written) or 3} surfaces",
    )


# ---------------------------------------------------------------------------
# Check 7: Weaviate <Project>_Diagrams class exists.
# ---------------------------------------------------------------------------


def _check_weaviate_class(
    project_name: str, *, fix: bool, quick: bool
) -> _CheckResult:
    if quick:
        return _CheckResult(
            "weaviate_diagrams_class",
            STATUS_SKIP,
            "--quick: Weaviate connectivity check skipped",
        )
    # Derive the expected class name. Mirrors project_init's
    # derive_project_diagrams_name to avoid import-time coupling on
    # the launcher.
    try:
        from vco_lib.project_init import derive_project_diagrams_name
        expected_class = derive_project_diagrams_name(project_name)
    except Exception as exc:
        return _CheckResult(
            "weaviate_diagrams_class",
            STATUS_FAIL,
            f"cannot derive expected class name: {exc}",
        )
    weaviate_url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    try:
        import weaviate  # type: ignore
    except ImportError:
        return _CheckResult(
            "weaviate_diagrams_class",
            STATUS_SKIP,
            "weaviate-client not installed in active Python env",
        )
    from urllib.parse import urlparse
    parsed = urlparse(weaviate_url)
    host = parsed.hostname or "localhost"
    http_port = parsed.port or 8081
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
    try:
        client = weaviate.connect_to_custom(
            http_host=host,
            http_port=http_port,
            http_secure=parsed.scheme == "https",
            grpc_host=host,
            grpc_port=grpc_port,
            grpc_secure=parsed.scheme == "https",
        )
    except Exception as exc:
        return _CheckResult(
            "weaviate_diagrams_class",
            STATUS_SKIP,
            f"Weaviate unreachable at {weaviate_url}: {exc}",
        )
    try:
        try:
            # Annotated Any: weaviate-client v4 stubs type list_all() as
            # a dict, which would narrow the defensive else-branch below
            # to Never; older clients returned an iterable of objects.
            collections: Any = client.collections.list_all()
        except Exception as exc:
            return _CheckResult(
                "weaviate_diagrams_class",
                STATUS_FAIL,
                f"cannot list Weaviate collections: {exc}",
            )
        # ``list_all`` returns a mapping in v4+; collect names defensively.
        if isinstance(collections, Mapping):
            existing_names = set(collections.keys())
        else:
            existing_names = {getattr(c, "name", str(c)) for c in collections}
        if expected_class in existing_names:
            return _CheckResult(
                "weaviate_diagrams_class",
                STATUS_OK,
                f"class {expected_class!r} exists in Weaviate",
            )
        if not fix:
            return _CheckResult(
                "weaviate_diagrams_class",
                STATUS_FAIL,
                f"class {expected_class!r} missing (have: "
                f"{sorted(existing_names)[:5]}...)",
                fix_hint="re-run install.py to bootstrap the Diagrams class",
            )
        # --fix path: create a minimal class. The full schema is owned
        # by the launcher's bootstrap path; we create a minimal shape
        # here so the indexer's upsert can succeed. This is best-effort:
        # if the launcher later re-runs its bootstrap, it'll converge to
        # the full schema.
        try:
            from weaviate.classes.config import Configure, Property, DataType  # type: ignore
            client.collections.create(
                name=expected_class,
                properties=[
                    Property(name="title", data_type=DataType.TEXT),
                    Property(name="content", data_type=DataType.TEXT),
                    Property(name="path_tags", data_type=DataType.TEXT_ARRAY),
                    Property(name="diagram_kind", data_type=DataType.TEXT),
                    Property(name="file_path", data_type=DataType.TEXT),
                    Property(name="chat_id", data_type=DataType.TEXT),
                    Property(name="linked_session_summary", data_type=DataType.TEXT),
                    Property(name="created_at", data_type=DataType.INT),
                    Property(name="updated_at", data_type=DataType.INT),
                ],
                vectorizer_config=Configure.Vectorizer.none(),
            )
        except Exception as exc:
            return _CheckResult(
                "weaviate_diagrams_class",
                STATUS_FIX_FAILED,
                f"create class failed: {exc}",
            )
        return _CheckResult(
            "weaviate_diagrams_class",
            STATUS_FIXED,
            f"created minimal {expected_class!r} class",
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Check 8: PreToolUse hook registered (two matchers).
# ---------------------------------------------------------------------------


def _check_pretooluse_hooks(project_folder: Path) -> _CheckResult:
    settings_path = project_folder / ".claude" / "settings.json"
    if not settings_path.exists():
        return _CheckResult(
            "pretooluse_hooks",
            STATUS_FAIL,
            f"{settings_path} absent",
            fix_hint="re-run install.py to render .claude/settings.json",
        )
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _CheckResult(
            "pretooluse_hooks",
            STATUS_FAIL,
            f"cannot parse settings.json: {exc}",
        )
    pre = (payload.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pre, list):
        return _CheckResult(
            "pretooluse_hooks",
            STATUS_FAIL,
            "no hooks.PreToolUse array in settings.json",
        )
    found_native = False
    found_mcp = False
    for entry in pre:
        if not isinstance(entry, Mapping):
            continue
        matcher = str(entry.get("matcher", ""))
        hooks = entry.get("hooks", [])
        if not isinstance(hooks, list):
            continue
        commands_blob = " ".join(
            str((h or {}).get("command", "")) for h in hooks
        )
        if "pre-diagram-path-validation" not in commands_blob:
            continue
        # The native matcher is "Write|Edit"; the MCP matcher is
        # "mcp__mermaid__.*|mcp__excalidraw__.*". Accept either order
        # of alternation and either platform's script suffix.
        if matcher == "Write|Edit" or matcher == "Edit|Write":
            found_native = True
        if "mcp__mermaid__" in matcher and "mcp__excalidraw__" in matcher:
            found_mcp = True
    if found_native and found_mcp:
        return _CheckResult(
            "pretooluse_hooks",
            STATUS_OK,
            "both PreToolUse entries (Write|Edit + MCP matchers) present",
        )
    missing = []
    if not found_native:
        missing.append("Write|Edit matcher")
    if not found_mcp:
        missing.append("mcp__mermaid__.*|mcp__excalidraw__.* matcher")
    return _CheckResult(
        "pretooluse_hooks",
        STATUS_FAIL,
        f"missing PreToolUse entries: {missing}",
        fix_hint="re-run install.py to re-render the settings.json hooks block",
    )


# ---------------------------------------------------------------------------
# Check 9: PostToolUse delete hook registered.
# ---------------------------------------------------------------------------


def _check_post_delete_hook(project_folder: Path) -> _CheckResult:
    settings_path = project_folder / ".claude" / "settings.json"
    if not settings_path.exists():
        return _CheckResult(
            "post_delete_hook",
            STATUS_FAIL,
            f"{settings_path} absent",
        )
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _CheckResult(
            "post_delete_hook",
            STATUS_FAIL,
            f"cannot parse settings.json: {exc}",
        )
    post = (payload.get("hooks") or {}).get("PostToolUse")
    if not isinstance(post, list):
        return _CheckResult(
            "post_delete_hook",
            STATUS_FAIL,
            "no hooks.PostToolUse array in settings.json",
        )
    for entry in post:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("matcher", "")) != "Bash":
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, Mapping):
                continue
            if "post-file-delete" in str(hook.get("command", "")):
                return _CheckResult(
                    "post_delete_hook",
                    STATUS_OK,
                    "PostToolUse Bash entry → post-file-delete registered",
                )
    return _CheckResult(
        "post_delete_hook",
        STATUS_FAIL,
        "no Bash-matcher hook pointing at post-file-delete",
        fix_hint="re-run install.py to re-render the settings.json hooks block",
    )


# ---------------------------------------------------------------------------
# Check 10: hook scripts physically present + executable.
# ---------------------------------------------------------------------------


HOOK_SCRIPT_NAMES: tuple[str, ...] = (
    "pre-diagram-path-validation",
    "post-file-delete",
)


def _check_hook_scripts_on_disk(project_folder: Path) -> _CheckResult:
    hooks_dir = project_folder / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return _CheckResult(
            "hook_scripts_on_disk",
            STATUS_FAIL,
            f"{hooks_dir} is not a directory",
            fix_hint="re-run install.py to create .claude/hooks/",
        )
    missing: list[str] = []
    not_executable: list[str] = []
    for stem in HOOK_SCRIPT_NAMES:
        # Accept either .sh OR .ps1 — the platform-specific install lays
        # one or the other (or both for cross-OS development checkouts).
        sh = hooks_dir / f"{stem}.sh"
        ps1 = hooks_dir / f"{stem}.ps1"
        present_sh = sh.is_file()
        present_ps1 = ps1.is_file()
        if not (present_sh or present_ps1):
            missing.append(stem)
            continue
        # Executable bit only meaningful on POSIX. Skip the check on
        # Windows (st_mode bits are different and the launcher uses
        # PowerShell directly).
        if present_sh and os.name != "nt":
            if not os.access(sh, os.X_OK):
                not_executable.append(str(sh))
    if missing:
        return _CheckResult(
            "hook_scripts_on_disk",
            STATUS_FAIL,
            f"hook scripts missing: {missing}",
            fix_hint="re-run install.py to materialise the templates",
        )
    if not_executable:
        return _CheckResult(
            "hook_scripts_on_disk",
            STATUS_FAIL,
            f"hook scripts not executable: {not_executable}",
            fix_hint=f"chmod +x {' '.join(not_executable)}",
        )
    return _CheckResult(
        "hook_scripts_on_disk",
        STATUS_OK,
        f"all {len(HOOK_SCRIPT_NAMES)} hook scripts present + executable",
    )


# ---------------------------------------------------------------------------
# Check 11: vco_lib.diagram_indexer importable + key functions resolvable.
# ---------------------------------------------------------------------------


def _check_indexer_importable() -> _CheckResult:
    try:
        from vco_lib.diagram_indexer import (  # noqa: F401
            index_diagram,
            drop_diagram_by_path,
            parse_mermaid,
            parse_excalidraw,
        )
    except ImportError as exc:
        return _CheckResult(
            "indexer_importable",
            STATUS_FAIL,
            f"vco_lib.diagram_indexer not importable: {exc}",
        )
    except Exception as exc:
        return _CheckResult(
            "indexer_importable",
            STATUS_FAIL,
            f"vco_lib.diagram_indexer raised on import: {exc}",
        )
    # Optional async variant — present in Phase 1.5.A but tolerate
    # absence so older snapshots don't FAIL here.
    has_async = False
    try:
        from vco_lib.diagram_indexer import index_diagram_async  # noqa: F401
        has_async = True
    except ImportError:
        pass
    detail = "key functions resolvable"
    if has_async:
        detail += " (incl. index_diagram_async)"
    return _CheckResult("indexer_importable", STATUS_OK, detail)


# ---------------------------------------------------------------------------
# Check 12: path validator round-trip.
# ---------------------------------------------------------------------------


def _check_path_validator() -> _CheckResult:
    try:
        from vco_lib.diagram_paths import validate_scoped_path
    except ImportError as exc:
        return _CheckResult(
            "path_validator",
            STATUS_FAIL,
            f"vco_lib.diagram_paths not importable: {exc}",
        )
    # Known-good path → returns None.
    good = ".claude/diagrams/gui/auth/login-form.mmd"
    err = validate_scoped_path(good)
    if err is not None:
        return _CheckResult(
            "path_validator",
            STATUS_FAIL,
            f"validator rejected known-good path {good!r}: {err}",
        )
    # Known-bad path → returns string.
    bad = ".claude/diagrams/flat-file-no-category.mmd"
    err = validate_scoped_path(bad)
    if err is None:
        return _CheckResult(
            "path_validator",
            STATUS_FAIL,
            f"validator accepted known-bad path {bad!r}",
        )
    return _CheckResult(
        "path_validator",
        STATUS_OK,
        "round-trip OK (good→None, bad→string)",
    )


# ---------------------------------------------------------------------------
# Check 13: CLAUDE.md diagrams section rendered.
# ---------------------------------------------------------------------------


CLAUDE_MD_SECTION_HEADER = "## Diagrams (Mermaid + Excalidraw)"


def _check_claude_md_section(project_folder: Path) -> _CheckResult:
    path = project_folder / "CLAUDE.md"
    if not path.is_file():
        # The conditional section is OPTIONAL — if CLAUDE.md is absent
        # entirely (older project, or user-deleted), SKIP rather than
        # FAIL. The user can re-render via the launcher.
        return _CheckResult(
            "claude_md_section",
            STATUS_SKIP,
            "CLAUDE.md absent (project may pre-date conditional sections)",
        )
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _CheckResult(
            "claude_md_section",
            STATUS_FAIL,
            f"cannot read CLAUDE.md: {exc}",
        )
    if CLAUDE_MD_SECTION_HEADER in content:
        return _CheckResult(
            "claude_md_section",
            STATUS_OK,
            "diagrams section present",
        )
    # Section missing — but the diagrams module may have been disabled
    # for this project, in which case "missing" is the correct state.
    # Surface as FAIL only when the module is active; we don't have
    # cheap module-state lookup here, so leave it as FAIL with a hint.
    return _CheckResult(
        "claude_md_section",
        STATUS_FAIL,
        f"section header {CLAUDE_MD_SECTION_HEADER!r} not found in CLAUDE.md",
        fix_hint="if diagrams is enabled, re-render CLAUDE.md from template",
    )


# ===========================================================================
# Orchestration
# ===========================================================================


@dataclasses.dataclass
class _ProjectVerifyReport:
    project_id: str
    project_name: str
    project_folder: str
    checks: list[_CheckResult] = dataclasses.field(default_factory=list)

    def summary_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        return counts

    def exit_code(self) -> int:
        """Worst-of policy.

        FIX_FAILED → 3
        FAIL → 1
        SKIP / OK / FIXED → 0
        """
        statuses = {c.status for c in self.checks}
        if STATUS_FIX_FAILED in statuses:
            return EXIT_FIX_FAILED
        if STATUS_FAIL in statuses:
            return EXIT_FAIL
        return EXIT_OK

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_folder": self.project_folder,
            "checks": [c.to_payload() for c in self.checks],
            "summary": self.summary_counts(),
            "exit_code": self.exit_code(),
        }


def _verify_one_project(
    project_id: str, *, fix: bool, quick: bool
) -> tuple[int, _ProjectVerifyReport]:
    """Run all checks for one project. Returns ``(exit_code, report)``."""
    # Check 1 — DB row lookup (also resolves project name + folder).
    project_check, project_row = _check_project_row(project_id)
    if project_row is None:
        # Without a project row we cannot run any other check. Surface
        # as EXIT_ENV_PROBLEM directly — this is a sysinfo problem.
        report = _ProjectVerifyReport(
            project_id=project_id,
            project_name="<unknown>",
            project_folder="<unknown>",
            checks=[project_check],
        )
        return EXIT_ENV_PROBLEM, report

    resolved_id = str(project_row["id"])
    project_name = str(project_row["name"])
    # Prefer the launcher's folder_path; fall back to
    # _resolve_project_folder if the column is empty (older schemas).
    folder_path = str(project_row.get("folder_path") or "")
    if folder_path:
        project_folder = Path(folder_path)
    else:
        try:
            project_folder = Path(_resolve_project_folder(resolved_id))
        except Exception as exc:
            project_check = _CheckResult(
                project_check.name,
                STATUS_FAIL,
                f"{project_check.detail}; folder resolution failed: {exc}",
            )
            return EXIT_ENV_PROBLEM, _ProjectVerifyReport(
                project_id=resolved_id,
                project_name=project_name,
                project_folder="<unknown>",
                checks=[project_check],
            )

    report = _ProjectVerifyReport(
        project_id=resolved_id,
        project_name=project_name,
        project_folder=str(project_folder),
        checks=[project_check],
    )

    # The remaining 12 checks each receive their own slice of arguments.
    # Order matches the README/help text so users can correlate at a
    # glance. Each check catches its own exceptions; the orchestrator
    # NEVER lets a check raise out.
    check_runners: list[Callable[[], _CheckResult]] = [
        lambda: _check_project_modules_row(resolved_id, fix=fix),
        lambda: _check_migration_022(),
        lambda: _check_mcp_wrappers(),
        lambda: _check_hub_allowlist(resolved_id) if not quick else _CheckResult(
            "hub_allowlist",
            STATUS_SKIP,
            "--quick: hub HTTP probe skipped",
        ),
        lambda: _check_env_projection(resolved_id, project_folder, fix=fix),
        lambda: _check_weaviate_class(project_name, fix=fix, quick=quick),
        lambda: _check_pretooluse_hooks(project_folder),
        lambda: _check_post_delete_hook(project_folder),
        lambda: _check_hook_scripts_on_disk(project_folder),
        lambda: _check_indexer_importable(),
        lambda: _check_path_validator(),
        lambda: _check_claude_md_section(project_folder),
    ]
    for runner in check_runners:
        try:
            result = runner()
        except Exception as exc:  # pragma: no cover — defensive
            result = _CheckResult(
                "<unknown>",
                STATUS_FAIL,
                f"check raised unexpectedly: {exc}",
            )
        report.checks.append(result)

    return report.exit_code(), report


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


_STATUS_LABELS: dict[str, str] = {
    STATUS_OK: "[OK]  ",
    STATUS_FAIL: "[FAIL]",
    STATUS_SKIP: "[SKIP]",
    STATUS_FIXED: "[FIX] ",
    STATUS_FIX_FAILED: "[FAIL]",
}


def _format_human(report: _ProjectVerifyReport) -> str:
    lines = [
        f"verify-diagrams: {report.project_name} "
        f"(project_id={report.project_id})",
        f"  folder: {report.project_folder}",
        "",
    ]
    for c in report.checks:
        label = _STATUS_LABELS.get(c.status, f"[{c.status.upper()}]")
        lines.append(f"  {label} {c.name} — {c.detail}")
        if c.fix_hint and c.status in (STATUS_FAIL, STATUS_FIX_FAILED):
            lines.append(f"         > fix: {c.fix_hint}")
    counts = report.summary_counts()
    summary_parts = [
        f"{counts.get(STATUS_OK, 0)} OK",
        f"{counts.get(STATUS_FAIL, 0) + counts.get(STATUS_FIX_FAILED, 0)} FAIL",
        f"{counts.get(STATUS_SKIP, 0)} SKIP",
    ]
    if counts.get(STATUS_FIXED):
        summary_parts.append(f"{counts[STATUS_FIXED]} FIXED")
    lines.append("")
    lines.append("Summary: " + ", ".join(summary_parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# argparse entry-point
# ---------------------------------------------------------------------------


def cmd_verify_diagrams(args: argparse.Namespace) -> int:
    """Entry-point for ``vco verify-diagrams``."""
    if args.all and args.project_id:
        msg = "--all and a positional project_id are mutually exclusive"
        if args.json:
            json.dump(
                {
                    "command": "verify-diagrams",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "usage_error",
                    "error": msg,
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(f"verify-diagrams: {msg}", file=sys.stderr)
        return EXIT_ENV_PROBLEM

    if not args.all and not args.project_id:
        msg = "missing positional project_slug_or_id (or pass --all)"
        if args.json:
            json.dump(
                {
                    "command": "verify-diagrams",
                    "exit_code": EXIT_ENV_PROBLEM,
                    "overall": "usage_error",
                    "error": msg,
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(f"verify-diagrams: {msg}", file=sys.stderr)
        return EXIT_ENV_PROBLEM

    if args.all:
        try:
            projects = list(_list_registered_projects())
        except Exception as exc:
            if args.json:
                json.dump(
                    {
                        "command": "verify-diagrams",
                        "exit_code": EXIT_ENV_PROBLEM,
                        "overall": "db_unreadable",
                        "error": str(exc),
                    },
                    sys.stdout,
                )
                sys.stdout.write("\n")
            else:
                print(
                    f"verify-diagrams --all: cannot list projects: {exc}",
                    file=sys.stderr,
                )
            return EXIT_ENV_PROBLEM
        worst = EXIT_OK
        reports: list[_ProjectVerifyReport] = []
        for project in projects:
            pid = project.get("id") or project.get("slug")
            if not pid:
                continue
            code, report = _verify_one_project(
                pid, fix=bool(args.fix), quick=bool(args.quick)
            )
            reports.append(report)
            worst = max(worst, code)
        if args.json:
            json.dump(
                {
                    "command": "verify-diagrams",
                    "exit_code": worst,
                    "overall": _overall_label(worst),
                    "projects": [r.to_payload() for r in reports],
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            for r in reports:
                print(_format_human(r))
                print()
            print(f"Overall exit: {worst}")
        return worst

    code, report = _verify_one_project(
        args.project_id, fix=bool(args.fix), quick=bool(args.quick)
    )
    if args.json:
        # Build the payload with the orchestrator-chosen exit code
        # (NOT the report's internal exit_code, which may be 1 when
        # the orchestrator decided 2/env_problem because the project
        # row was missing — see test_orchestration_project_not_found).
        payload = {**report.to_payload(), "command": "verify-diagrams",
                   "exit_code": code, "overall": _overall_label(code)}
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(_format_human(report))
    return code


def _overall_label(exit_code: int) -> str:
    if exit_code == EXIT_OK:
        return "ok"
    if exit_code == EXIT_FAIL:
        return "fail"
    if exit_code == EXIT_ENV_PROBLEM:
        return "env_problem"
    if exit_code == EXIT_FIX_FAILED:
        return "fix_failed"
    return "unknown"


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def add_subparsers(sub: Any) -> None:
    """Register ``verify-diagrams`` onto the parent subparsers.

    Called by :mod:`vco_lib.cli.__main__`.
    """
    parser = sub.add_parser(
        "verify-diagrams",
        help=(
            "Verify the diagrams feature is wired correctly for a "
            "project (DB rows, MCP wrappers, hub allowlist, env "
            "projection, Weaviate class, hooks, importable modules, "
            "CLAUDE.md section). Exit 0=OK, 1=fail, 2=env problem, "
            "3=--fix couldn't repair."
        ),
    )
    parser.add_argument(
        "project_id", nargs="?", default=None,
        help=(
            "Project slug or rowid (resolved via the launcher DB). "
            "Omit when using --all."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (machine-readable).",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help=(
            "Best-effort repair where possible: re-seed project_modules "
            "row, re-project env, create minimal Weaviate class. Logs "
            "and continues per-check on failure (unlike verify-pins "
            "which aborts; diagrams has heterogeneous fixers and the "
            "user benefits from seeing every fixable problem at once)."
        ),
    )
    parser.add_argument(
        "--all", action="store_true",
        help=(
            "Iterate every project registered in the launcher DB. "
            "Worst exit code across all projects wins."
        ),
    )
    parser.add_argument(
        "--quick", action="store_true",
        help=(
            "Skip the slow checks (Weaviate connectivity, hub HTTP "
            "probe). Useful in CI / pre-commit hooks."
        ),
    )
    parser.set_defaults(func=cmd_verify_diagrams)
