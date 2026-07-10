# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco verify-pins`` and ``vco verify-env-projection`` — Phase 0 acceptance.

These two subcommands implement the acceptance criteria for Phase 0 of
the diagrams-integration plan (``.claude/context/plans/
diagrams-integration-excalidraw-mermaid-2026-05-24.md`` §3 Phase 0):

* ``verify-pins`` — confirms the npm packages enumerated in
  ``bundled_mcp_versions.toml`` are installed at exactly the pinned
  version. Exits 0 on full agreement, 1 on drift, 2 when ``npm`` is
  missing (a sysinfo problem, not a pinning problem).

* ``verify-env-projection`` — confirms that the three on-disk env
  surfaces (``.claude/settings.json`` ``env``, ``.claude/env``,
  ``.vscode/settings.json`` ``claude-code.env``) match the canonical
  projection emitted by ``vco_lib.config_projection.project_env_from_db``
  for a given project. Exits 0 on full agreement, 1 on drift, 2 when
  the project cannot be located in the launcher DB.

Both subcommands accept ``--json`` for machine-readable output and
``--fix`` for in-place repair. ``--fix`` aborts on the first failure
rather than silently skipping — see the per-command help for exit
semantics.

Cross-OS rules (see ``knowledge/concepts/cross-os-hook-portability.md``):

* No ``shell=True``. All subprocess calls use an explicit ``args=[...]``
  list with a 60s timeout per package.
* ``shutil.which`` results are cached at module level to avoid
  re-detecting interpreters on every invocation.
* All filesystem paths flow through :class:`pathlib.Path`.
* No ``/tmp`` literals; ``tempfile.gettempdir()`` if scratch is needed.

Integration notes (Phase 0.A / 0.B dependencies)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two upstream modules land in parallel branches:

* **Phase 0.A** — ``vco_lib/bundled_versions.py`` exposes
  ``load_bundled_versions() -> Mapping[str, NpmPin]`` reading
  ``bundled_mcp_versions.toml`` at repo root, and
  ``install.py::_install_pinned_npm(package_key: str) -> InstallResult``.
* **Phase 0.B** — ``vco_lib/config_projection.py`` exposes
  ``project_env_from_db(project_id: str) -> ProjectEnvBundle`` (a
  TypedDict carrying ``canonical_env``, ``project_id``,
  ``project_root``) and ``apply_project_env(bundle: ProjectEnvBundle,
  *, surfaces=None, user_secret_bundle=None) -> dict[str, list[str]]``.
  The wrappers below adapt that landed API to this module's internal
  flat ``Mapping[str, str]`` contract (the plan-era API differed; see
  the wrapper docstrings).

This module imports them lazily via thin wrappers so that:

(a) the import does not fail when 0.A/0.B haven't merged yet — useful
    for the dispatcher's ``--help`` discovery; and
(b) tests can monkey-patch the wrappers without poking at the real
    upstream modules.

Each wrapper is marked with a ``# Phase 0.A/B dependency`` comment so
the post-merge integration step is obvious.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Exit-code constants — keep stable; tests assert against these.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_TOOL_MISSING = 2
EXIT_USAGE = 3  # argparse usage errors short-circuit via argparse itself,
                # but --fix follow-ups that fail to repair use this slot.


# ---------------------------------------------------------------------------
# Cached `shutil.which` lookups (cross-OS rule).
# ---------------------------------------------------------------------------

_WHICH_CACHE: dict[str, Optional[str]] = {}


def _which(tool: str) -> Optional[str]:
    """Cached ``shutil.which``. Repeated lookups in one process are cheap.

    Tests reset the cache via :func:`_reset_which_cache`.
    """
    if tool not in _WHICH_CACHE:
        _WHICH_CACHE[tool] = shutil.which(tool)
    return _WHICH_CACHE[tool]


def _reset_which_cache() -> None:
    """Test hook — clears the cached ``shutil.which`` results."""
    _WHICH_CACHE.clear()


# ---------------------------------------------------------------------------
# Phase 0.A / 0.B dependency wrappers
#
# These thin wrappers exist so tests can monkey-patch the upstream APIs
# without our code path importing them at module-import time. The actual
# imports happen inside the wrapper bodies and are caught if the upstream
# module is not yet on disk (Phase 0.A / 0.B branches not merged).
# ---------------------------------------------------------------------------


def _load_bundled_versions() -> Mapping[str, Any]:
    """Phase 0.A dependency — actual import wired post-merge.

    Expected upstream API (per plan §3 Phase 0 step 1–2)::

        from vco_lib.bundled_versions import load_bundled_versions
        manifest = load_bundled_versions()
        # Returns a Mapping keyed by package_key (e.g. ``mermaid_mcp``),
        # each value exposing at least ``.package`` (str) and ``.version``
        # (str). Implementation may also surface ``.sha256``; we don't read
        # it here.

    Tests stub this function with a fixture that returns a dict of
    fixture pins. The integration step (post-Phase-0.A merge) replaces
    the ``raise`` below with the real import.
    """
    # Phase 0.A dependency — actual import wired post-merge.
    try:
        from vco_lib.bundled_versions import load_bundled_versions  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.bundled_versions.load_bundled_versions() is not "
            "available. This subcommand requires Phase 0.A to be merged. "
            "If you're running tests, ensure the test fixture monkey-"
            "patches `_load_bundled_versions` on `vco_lib.cli.verify`."
        ) from exc
    return load_bundled_versions()


def _install_pinned_npm(package_key: str) -> Any:
    """Install one pinned npm package (delegates to the vco_lib core).

    v0.2.77 7a-bis: the pinned-npm install core lives in
    ``vco_lib.install_npm`` (moved out of the top-level ``install`` script
    to break the vco_lib → install back-edge). This resolves the DI params
    the core needs — npm path, audit-log destination, and the repo root for
    ``file:`` pin resolution — from this CLI's own environment.

    Returns True when the package is installed at the exact pinned version,
    False otherwise (see ``install_pinned_npm``). Tests stub this function
    on ``vco_lib.cli.verify`` to simulate outcomes.
    """
    import os

    from vco_lib.install_npm import install_pinned_npm as _real

    npm_path = _which("npm")
    audit_log_path = (
        Path.home() / ".claude" / "metrics" / "bundled_versions.jsonl"
    )
    # Repo root anchor for ``file:`` pin resolution: prefer the canonical
    # env var the launcher/install set, else the in-tree layout
    # (verify.py lives at ``vco_lib/cli/verify.py`` → repo root is 3 up).
    env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    project_root = (
        Path(env_root) if env_root else Path(__file__).resolve().parents[2]
    )
    return _real(
        package_key,
        npm_path=npm_path,
        audit_log_path=audit_log_path,
        project_root=project_root,
    )


def _project_env_from_db(project_id: str) -> Mapping[str, str]:
    """Adapter over ``config_projection.project_env_from_db``.

    The landed Phase 0.B API returns a ``ProjectEnvBundle`` TypedDict
    (``canonical_env`` + ``project_id`` + ``project_root``), not the
    flat ``dict[str, str]`` the plan-era spec described. This module's
    internal contract (``expected.keys()``, ``_diff_surface``) wants
    the flat canonical map, so we unwrap ``canonical_env`` here.
    Raises ``LookupError`` subclasses (``ProjectNotFound``) upstream.
    """
    try:
        from vco_lib.config_projection import project_env_from_db
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.project_env_from_db() is not "
            "available. This subcommand requires Phase 0.B to be merged."
        ) from exc
    return project_env_from_db(project_id)["canonical_env"]


def _apply_project_env(
    bundle: Mapping[str, str], *, project_folder: Path
) -> Any:
    """Adapter over ``config_projection.apply_project_env``.

    The landed Phase 0.B API takes a ``ProjectEnvBundle`` (which
    carries ``project_root`` itself — there is no ``project_folder``
    kwarg) and returns ``{surface: [keys_written]}``, raising
    ``ConfigProjectionError`` on write failure. We rebuild the bundle
    from this module's flat-map contract, opt into ALL THREE surfaces
    (the verifier diffs ``.vscode/settings.json`` too — writing only
    the two default surfaces would fail the round-trip idempotency
    check), and normalise the result to the ``{ok, message}`` shape
    ``_result_ok`` expects. Exceptions propagate to the caller's
    ``fix_failed`` handler.
    """
    try:
        from vco_lib.config_projection import (
            ProjectEnvBundle,
            apply_project_env,
        )
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.apply_project_env() is not "
            "available. This subcommand requires Phase 0.B to be merged."
        ) from exc
    real_bundle: ProjectEnvBundle = {
        "canonical_env": dict(bundle),
        # apply_project_env() reads only canonical_env + project_root;
        # project_id is carried for audit logging by other callers.
        "project_id": "",
        "project_root": project_folder,
    }
    report = apply_project_env(
        real_bundle,
        surfaces=(
            "claude_settings_json",
            "claude_env",
            "vscode_settings_json",
        ),
    )
    total = sum(len(keys) for keys in report.values())
    return {
        "ok": True,
        "message": f"wrote {total} keys across {len(report)} surfaces",
    }


def _list_registered_projects() -> Iterable[Mapping[str, str]]:
    """Phase 0.B dependency — actual import wired post-merge.

    Expected upstream API: ``config_projection.list_registered_projects()``
    returns an iterable of ``{"id": str, "slug": str, "folder": str}``
    dicts (one per project row in the launcher SQLite DB). Used by
    ``--all``. Stubbed in tests.
    """
    # Phase 0.B dependency — actual import wired post-merge.
    try:
        from vco_lib.config_projection import list_registered_projects  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.list_registered_projects() is not "
            "available. This subcommand requires Phase 0.B to be merged."
        ) from exc
    return list_registered_projects()


def _resolve_project_folder(project_id: str) -> Path:
    """Phase 0.B dependency — actual import wired post-merge.

    Expected upstream API: resolves a project slug/id to its on-disk
    folder (where ``.claude/`` and ``.vscode/`` live). Tests stub this
    to return a ``tmp_path`` fixture.
    """
    # Phase 0.B dependency — actual import wired post-merge.
    try:
        from vco_lib.config_projection import resolve_project_folder  # type: ignore  # noqa: E501
    except ImportError as exc:  # pragma: no cover — exercised post-merge
        raise RuntimeError(
            "vco_lib.config_projection.resolve_project_folder() is not "
            "available. This subcommand requires Phase 0.B to be merged."
        ) from exc
    return resolve_project_folder(project_id)


# ===========================================================================
# verify-pins
# ===========================================================================


@dataclasses.dataclass(frozen=True)
class _PinRow:
    """Single row in the verify-pins comparison table."""

    key: str           # manifest key, e.g. ``mermaid_mcp``
    package: str       # npm package name, e.g. ``claude-mermaid``
    pinned: str        # version from manifest
    installed: Optional[str]  # version from ``npm view``, None if missing
    status: str        # ``match`` | ``drift`` | ``missing`` | ``npm-not-available``


def _npm_view_version(package: str, *, npm_path: str) -> Optional[str]:
    """Run ``npm view -g <package> version`` and return the installed
    version as a stripped string, or ``None`` when the package is not
    installed / npm returned an error / output was empty.

    Cross-OS:
        * ``shell=False`` always.
        * ``capture_output=True`` + ``text=True`` so we get a decoded str.
        * ``timeout=60`` per package — enough for slow registries, not so
          long that a hung registry blocks the verify forever.

    Note: ``npm view`` queries the registry, not the local install — for
    the local-install case we wrap it with ``-g`` and rely on
    ``npm list -g <package> --depth=0 --json`` instead. We use the
    ``npm list`` path here because the verify is about what's INSTALLED,
    not what's published.
    """
    args = [npm_path, "list", "-g", package, "--depth=0", "--json"]
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except (OSError, ValueError):
        return None

    # ``npm list`` exits non-zero when peer-dep warnings exist but still
    # emits the JSON payload on stdout — so don't gate on returncode.
    raw = (completed.stdout or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    deps = payload.get("dependencies") or {}
    entry = deps.get(package)
    if not isinstance(entry, dict):
        return None
    version = entry.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _classify(pinned: str, installed: Optional[str]) -> str:
    if installed is None:
        return "missing"
    if installed == pinned:
        return "match"
    return "drift"


def _collect_pin_rows(
    manifest: Mapping[str, Any], *, npm_path: Optional[str]
) -> list[_PinRow]:
    """Build one :class:`_PinRow` per manifest entry. If ``npm_path`` is
    ``None`` (npm not installed), every row is marked
    ``npm-not-available``; callers convert that to exit-code 2.
    """
    rows: list[_PinRow] = []
    for key, pin in manifest.items():
        package = _pin_attr(pin, "package")
        version = _pin_attr(pin, "version")
        if not package or not version:
            # Defensive: a malformed manifest entry is treated as drift,
            # not a hard crash. The plan rejects floating versions but
            # validation lives in Phase 0.A; we surface it cleanly.
            rows.append(
                _PinRow(
                    key=key,
                    package=package or f"<missing-package-for-{key}>",
                    pinned=version or "<missing-version>",
                    installed=None,
                    status="drift",
                )
            )
            continue
        if npm_path is None:
            rows.append(
                _PinRow(
                    key=key,
                    package=package,
                    pinned=version,
                    installed=None,
                    status="npm-not-available",
                )
            )
            continue
        installed = _npm_view_version(package, npm_path=npm_path)
        rows.append(
            _PinRow(
                key=key,
                package=package,
                pinned=version,
                installed=installed,
                status=_classify(version, installed),
            )
        )
    return rows


def _pin_attr(pin: Any, name: str) -> Optional[str]:
    """Accept either a dict (``pin["package"]``) or a dataclass-like
    object (``pin.package``). Phase 0.A's exact shape is TBD; this
    accessor is symmetric to both."""
    if isinstance(pin, Mapping):
        v = pin.get(name)
    else:
        v = getattr(pin, name, None)
    if isinstance(v, str):
        return v
    return None


def _format_pin_table(rows: Sequence[_PinRow]) -> str:
    """Human-readable table. Stable column order — tests assert on the
    header tokens."""
    if not rows:
        return "(no pinned packages in manifest)"
    headers = ("package", "pinned", "installed", "status")
    data = [
        (
            r.package,
            r.pinned,
            r.installed if r.installed is not None else "-",
            r.status,
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in data)) for i, h in enumerate(headers)]
    sep = "  "
    lines = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append(sep.join("-" * widths[i] for i in range(len(headers))))
    for row in data:
        lines.append(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def _verify_pins_json_payload(rows: Sequence[_PinRow], *, exit_code: int) -> dict[str, Any]:
    return {
        "command": "verify-pins",
        "exit_code": exit_code,
        "overall": (
            "ok" if exit_code == EXIT_OK else
            "npm_not_available" if exit_code == EXIT_TOOL_MISSING else
            "drift"
        ),
        "rows": [dataclasses.asdict(r) for r in rows],
    }


def cmd_verify_pins(args: argparse.Namespace) -> int:
    """Entry-point for ``vco verify-pins``."""
    # 1. Detect npm. If absent, surface that distinctly (exit 2 — sysinfo
    #    problem, not a pinning problem). The plan's acceptance criterion
    #    for Phase 1 also wants this: "npm absent → install.py warns
    #    clearly and skips Mermaid setup" — same idea.
    npm_path = _which("npm")

    # 2. Load the manifest. A missing manifest is a hard error — Phase
    #    0.A's job is to ship it. We catch the ImportError-shaped
    #    runtime errors so the user sees a useful message instead of a
    #    bare traceback.
    try:
        manifest = _load_bundled_versions()
    except Exception as exc:
        msg = f"verify-pins: cannot load bundled manifest: {exc}"
        if args.json:
            json.dump(
                {
                    "command": "verify-pins",
                    "exit_code": EXIT_DRIFT,
                    "overall": "manifest_error",
                    "error": str(exc),
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            print(msg, file=sys.stderr)
        return EXIT_DRIFT

    rows = _collect_pin_rows(manifest, npm_path=npm_path)

    # 3. Decide overall exit code.
    if npm_path is None:
        exit_code = EXIT_TOOL_MISSING
    elif any(r.status in {"drift", "missing"} for r in rows):
        exit_code = EXIT_DRIFT
    else:
        exit_code = EXIT_OK

    # 4. --fix path. Walks the drift/missing rows and calls Agent A's
    #    installer for each. Aborts on the first failure so the user can
    #    fix the underlying cause rather than ending up with a half-
    #    repaired stack.
    if args.fix and exit_code == EXIT_DRIFT:
        failures: list[tuple[str, str]] = []
        for row in rows:
            if row.status not in {"drift", "missing"}:
                continue
            try:
                result = _install_pinned_npm(row.key)
            except Exception as exc:
                failures.append((row.key, f"installer raised: {exc}"))
                break
            ok = _result_ok(result)
            if not ok:
                msg = _result_message(result) or "installer reported failure"
                failures.append((row.key, msg))
                break
        if failures:
            key, why = failures[0]
            if args.json:
                json.dump(
                    {
                        "command": "verify-pins",
                        "exit_code": EXIT_USAGE,
                        "overall": "fix_failed",
                        "failed_key": key,
                        "error": why,
                    },
                    sys.stdout,
                )
                sys.stdout.write("\n")
            else:
                print(
                    f"verify-pins --fix: aborting on first failure: "
                    f"{key}: {why}",
                    file=sys.stderr,
                )
            return EXIT_USAGE
        # Re-run verification after a clean --fix so the caller sees the
        # repaired state. This is also the round-trip idempotency check
        # for pins.
        return cmd_verify_pins(
            argparse.Namespace(json=args.json, fix=False)
        )

    # 5. Emit output.
    if args.json:
        json.dump(_verify_pins_json_payload(rows, exit_code=exit_code), sys.stdout)
        sys.stdout.write("\n")
    else:
        if exit_code == EXIT_OK:
            print("OK — all pinned packages match manifest.")
            print(_format_pin_table(rows))
        elif exit_code == EXIT_TOOL_MISSING:
            print(
                "npm not available on PATH — cannot verify pins. "
                "(This is a sysinfo problem, not a pinning problem.)",
                file=sys.stderr,
            )
        else:
            print("DRIFT — some packages do not match the pinned versions:")
            print(_format_pin_table(rows))
            print(
                "\nRun `vco verify-pins --fix` to re-install pinned "
                "versions (aborts on first failure).",
                file=sys.stderr,
            )
    return exit_code


def _result_ok(result: Any) -> bool:
    """Accept either ``result.ok`` or ``result["ok"]`` shapes."""
    if isinstance(result, Mapping):
        return bool(result.get("ok", False))
    return bool(getattr(result, "ok", False))


def _result_message(result: Any) -> Optional[str]:
    if isinstance(result, Mapping):
        v = result.get("message")
    else:
        v = getattr(result, "message", None)
    return v if isinstance(v, str) else None


# ===========================================================================
# verify-env-projection
# ===========================================================================


@dataclasses.dataclass(frozen=True)
class _EnvSurface:
    """Where a key/value lives in the project on-disk env."""

    name: str            # ``.claude/settings.json`` etc. — human label
    path: Path
    values: dict[str, str]   # the env subset read from this surface


def _read_claude_settings_env(folder: Path) -> _EnvSurface:
    """Load ``.claude/settings.json`` ``env`` block.

    A missing file is treated as an empty surface — that's what drives
    "DRIFT: surface is missing every expected key" rather than an
    exception.
    """
    path = folder / ".claude" / "settings.json"
    values: dict[str, str] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            env = payload.get("env")
            if isinstance(env, Mapping):
                for k, v in env.items():
                    if isinstance(v, str):
                        values[k] = v
                    else:
                        values[k] = str(v)
        except (OSError, ValueError):
            # Malformed JSON: keep values empty → every expected key
            # will register as drift, which is the right signal.
            pass
    return _EnvSurface(name=".claude/settings.json", path=path, values=values)


def _read_claude_env(folder: Path) -> _EnvSurface:
    """Load ``.claude/env`` (KEY=VALUE shell-style)."""
    path = folder / ".claude" / "env"
    values: dict[str, str] = {}
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                # Strip a leading ``export `` for shell-source compatibility.
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
        except OSError:
            pass
    return _EnvSurface(name=".claude/env", path=path, values=values)


def _read_vscode_settings_env(folder: Path) -> _EnvSurface:
    """Load ``.vscode/settings.json`` ``claude-code.env`` block.

    The VS Code extension surfaces are not propagated to MCP subprocesses
    on Linux (see CLAUDE.md note), but we still verify the projection
    here because the launcher writes this surface for editor consistency
    and a stale value is still drift.
    """
    path = folder / ".vscode" / "settings.json"
    values: dict[str, str] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            env = payload.get("claude-code.env")
            if isinstance(env, Mapping):
                for k, v in env.items():
                    if isinstance(v, str):
                        values[k] = v
                    else:
                        values[k] = str(v)
        except (OSError, ValueError):
            pass
    return _EnvSurface(name=".vscode/settings.json", path=path, values=values)


def _diff_surface(
    surface: _EnvSurface, expected: Mapping[str, str]
) -> list[dict[str, str]]:
    """Compare ``surface.values`` to the canonical ``expected`` bundle.

    Returns one row per drift entry — empty list means the surface
    matches the projection for every canonical key. Extra keys on the
    surface are NOT flagged here (they may be user-authored escape
    hatches the projection doesn't speak about). Only deviations from
    the canonical set count.
    """
    drift: list[dict[str, str]] = []
    for key, want in expected.items():
        have = surface.values.get(key)
        if have == want:
            continue
        drift.append(
            {
                "surface": surface.name,
                "key": key,
                "expected": want,
                "actual": have if have is not None else "<missing>",
            }
        )
    return drift


def _verify_env_projection_for_project(
    project_id: str, *, fix: bool, json_mode: bool
) -> tuple[int, dict[str, Any]]:
    """Core verifier for one project. Returns ``(exit_code, payload)``
    so the caller can aggregate over ``--all``.

    Round-trip idempotency: after ``--fix`` we re-read the three surfaces
    and re-diff. If the re-diff is non-empty the contract is broken; we
    return EXIT_USAGE (3) with a payload that names the offending keys.
    """
    try:
        expected = _project_env_from_db(project_id)
        folder = _resolve_project_folder(project_id)
    except LookupError as exc:
        return EXIT_TOOL_MISSING, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_TOOL_MISSING,
            "overall": "project_not_found",
            "error": str(exc),
        }
    except Exception as exc:
        return EXIT_TOOL_MISSING, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_TOOL_MISSING,
            "overall": "db_unreadable",
            "error": str(exc),
        }

    surfaces = (
        _read_claude_settings_env(folder),
        _read_claude_env(folder),
        _read_vscode_settings_env(folder),
    )
    drift_rows: list[dict[str, str]] = []
    for s in surfaces:
        drift_rows.extend(_diff_surface(s, expected))

    if not drift_rows:
        return EXIT_OK, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_OK,
            "overall": "ok",
            "expected_keys": sorted(expected.keys()),
        }

    if not fix:
        return EXIT_DRIFT, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_DRIFT,
            "overall": "drift",
            "drift": drift_rows,
        }

    # --fix path: re-project from the DB. Aborts on failure with exit 3.
    try:
        apply_result = _apply_project_env(expected, project_folder=folder)
    except Exception as exc:
        return EXIT_USAGE, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_USAGE,
            "overall": "fix_failed",
            "error": str(exc),
        }
    if not _result_ok(apply_result):
        return EXIT_USAGE, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_USAGE,
            "overall": "fix_failed",
            "error": _result_message(apply_result) or "apply_project_env reported failure",
        }

    # Round-trip idempotency check.
    surfaces_after = (
        _read_claude_settings_env(folder),
        _read_claude_env(folder),
        _read_vscode_settings_env(folder),
    )
    drift_after: list[dict[str, str]] = []
    for s in surfaces_after:
        drift_after.extend(_diff_surface(s, expected))
    if drift_after:
        return EXIT_USAGE, {
            "command": "verify-env-projection",
            "project_id": project_id,
            "exit_code": EXIT_USAGE,
            "overall": "fix_not_idempotent",
            "remaining_drift": drift_after,
            "note": (
                "apply_project_env() returned ok but a second verify "
                "still shows drift — the projection contract is broken."
            ),
        }
    return EXIT_OK, {
        "command": "verify-env-projection",
        "project_id": project_id,
        "exit_code": EXIT_OK,
        "overall": "ok_after_fix",
        "expected_keys": sorted(expected.keys()),
    }


def _format_env_drift_table(drift: Sequence[Mapping[str, str]]) -> str:
    if not drift:
        return "(no drift)"
    headers = ("surface", "key", "expected", "actual")
    rows = [tuple(d.get(h, "") for h in headers) for d in drift]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = "  "
    lines = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append(sep.join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        lines.append(sep.join(r[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def cmd_verify_env_projection(args: argparse.Namespace) -> int:
    """Entry-point for ``vco verify-env-projection``."""
    if args.all:
        try:
            projects = list(_list_registered_projects())
        except Exception as exc:
            if args.json:
                json.dump(
                    {
                        "command": "verify-env-projection",
                        "exit_code": EXIT_TOOL_MISSING,
                        "overall": "db_unreadable",
                        "error": str(exc),
                    },
                    sys.stdout,
                )
                sys.stdout.write("\n")
            else:
                print(
                    f"verify-env-projection --all: cannot list projects: {exc}",
                    file=sys.stderr,
                )
            return EXIT_TOOL_MISSING
        worst = EXIT_OK
        results = []
        for project in projects:
            pid = project.get("id") or project.get("slug")
            if not pid:
                continue
            code, payload = _verify_env_projection_for_project(
                pid, fix=bool(args.fix), json_mode=bool(args.json)
            )
            results.append(payload)
            worst = max(worst, code)
        if args.json:
            json.dump(
                {
                    "command": "verify-env-projection",
                    "exit_code": worst,
                    "overall": ("ok" if worst == EXIT_OK else "drift" if worst == EXIT_DRIFT else "error"),
                    "projects": results,
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        else:
            for r in results:
                _print_env_projection_human(r)
            print(f"\nOverall exit: {worst}")
        return worst

    project_id = args.project_id
    if not project_id:
        print(
            "verify-env-projection: missing positional project_slug_or_id "
            "(or pass --all).",
            file=sys.stderr,
        )
        return EXIT_USAGE
    code, payload = _verify_env_projection_for_project(
        project_id, fix=bool(args.fix), json_mode=bool(args.json)
    )
    if args.json:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    else:
        _print_env_projection_human(payload)
    return code


def _print_env_projection_human(payload: Mapping[str, Any]) -> None:
    overall = payload.get("overall", "")
    pid = payload.get("project_id", "<unknown>")
    if overall in {"ok", "ok_after_fix"}:
        label = "OK" if overall == "ok" else "OK (after --fix)"
        keys = payload.get("expected_keys", [])
        print(f"{label} — {pid}: all 3 surfaces match the DB projection "
              f"({len(keys)} canonical keys).")
        return
    if overall == "drift":
        print(f"DRIFT — {pid}:")
        drift = payload.get("drift", [])
        print(_format_env_drift_table(drift))
        print(
            "\nRun `vco verify-env-projection {pid} --fix` to re-project "
            "from the DB.".format(pid=pid),
            file=sys.stderr,
        )
        return
    if overall == "project_not_found":
        print(f"verify-env-projection: project not found: {pid}", file=sys.stderr)
        return
    if overall == "db_unreadable":
        print(
            f"verify-env-projection: launcher DB unreadable for {pid}: "
            f"{payload.get('error', '')}",
            file=sys.stderr,
        )
        return
    if overall == "fix_failed":
        print(
            f"verify-env-projection --fix: aborted on {pid}: "
            f"{payload.get('error', '')}",
            file=sys.stderr,
        )
        return
    if overall == "fix_not_idempotent":
        print(
            f"verify-env-projection --fix on {pid}: idempotency check "
            f"failed — apply_project_env returned ok but a second verify "
            f"still shows drift. This means the projection contract is "
            f"broken; please open a bug.",
            file=sys.stderr,
        )
        remaining = payload.get("remaining_drift", [])
        print(_format_env_drift_table(remaining), file=sys.stderr)
        return
    # Fall-through.
    print(json.dumps(dict(payload), indent=2))


# ===========================================================================
# argparse wiring
# ===========================================================================


def add_subparsers(sub: Any) -> None:
    """Register ``verify-pins`` + ``verify-env-projection`` onto a parent
    subparsers action. Called by :mod:`vco_lib.cli.__main__`.
    """
    p_pins = sub.add_parser(
        "verify-pins",
        help=(
            "Verify installed npm packages match bundled_mcp_versions.toml. "
            "Exit 0=OK, 1=drift, 2=npm not available."
        ),
    )
    p_pins.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (machine-readable).",
    )
    p_pins.add_argument(
        "--fix", action="store_true",
        help=(
            "Re-install each drifted package at the pinned version via "
            "install._install_pinned_npm. Aborts on the first failure; "
            "does NOT silently skip."
        ),
    )
    p_pins.set_defaults(func=cmd_verify_pins)

    p_env = sub.add_parser(
        "verify-env-projection",
        help=(
            "Verify .claude/settings.json env, .claude/env, and "
            ".vscode/settings.json match the canonical DB projection for "
            "a project. Exit 0=OK, 1=drift, 2=project/DB error."
        ),
    )
    p_env.add_argument(
        "project_id", nargs="?", default=None,
        help=(
            "Project slug or rowid (resolved via "
            "vco_lib.config_projection.resolve_project_folder). "
            "Omit when using --all."
        ),
    )
    p_env.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON object on stdout (machine-readable).",
    )
    p_env.add_argument(
        "--fix", action="store_true",
        help=(
            "Re-project the env bundle from the DB onto all three on-disk "
            "surfaces via config_projection.apply_project_env. Runs a "
            "round-trip idempotency check afterwards; if the re-verify "
            "still shows drift, the contract is broken and exit 3 is "
            "returned."
        ),
    )
    p_env.add_argument(
        "--all", action="store_true",
        help=(
            "Verify every project registered in the launcher DB. "
            "Worst exit code across all projects wins."
        ),
    )
    p_env.set_defaults(func=cmd_verify_env_projection)
