# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Root (orchestrator-self) install delegation to the ONE bundle engine.

v0.2.85 (PLAN-v0285 WP-1, decisions D1/D2/D4/D5): the orchestrator root
no longer materializes its own ``.claude/{hooks,scripts,agents,skills,
settings.json}`` with a bespoke enumeration / classifier / manifest
writer / settings merge (the pre-v0.2.85 Steps 5b + 9b in ``install.py``).
Instead it invokes the SAME ``install-bundle`` CLI the launcher uses, as a
subprocess, against the root folder — becoming a THIRD client (alongside the
Rust create + update wrappers) of the shared CLI contract (argv shape + the
``--json`` stdout envelope + the parse-failure posture).

Why a subprocess and not an in-process ``install_project_bundle`` call
(PLAN-v0285 E7, deliberately rejected — not deferred): the subprocess
boundary IS the parity mechanism. Root, launcher-create, and launcher-update
all exercise the exact same argv + stdout contract, so any future stdout
pollution breaks all three surfaces identically and is caught by one test
family. An in-process call would drift a distinct code path that the
launcher's tests never touch.

This module holds two functions with one concern each (one-home rule):

* :func:`root_bundle_argv` — the pure python argv builder. Mirrors the Rust
  call-site's argv order (``launcher/src-tauri/src/commands/projects_v2.rs``
  ``run_install_bundle`` / ``run_install_bundle_update_with_root``). WP-3
  pins this against the Rust vector so either side drifting breaks a test.
* :func:`run_root_bundle_install` — spawn + capture + ``json.loads(stdout)``
  (the SAME parse the launcher performs). Parse failure → honest soft-fail
  mirroring the launcher's posture: return an error-shaped envelope with a
  ``warnings`` entry, so ``install.py`` prints PARTIAL and continues (it does
  NOT raise — a broken subprocess must not abort the whole install, exactly
  as the launcher's ``update_project_v2`` does not abort on a parse failure).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# --------------------------------------------------------------------------
# Flag mapping (PLAN-v0285 D5). Kept as a module-level constant so the WP-1
# tests and any future caller reference the SAME strings, and so the mapping
# is auditable in one place.
# --------------------------------------------------------------------------

# The three bundle "kinds" that the pre-v0.2.85 ``--skip-materialize-claude-
# dir`` flag left untouched (hooks/scripts/settings). Agents/skills/knowledge
# always installed even under that flag (Step 9b ran regardless), so they are
# NOT in this set — see D5.
SKIP_MATERIALIZE_CLAUDE_DIR_KINDS: tuple[str, ...] = ("hooks", "scripts", "settings")


def root_bundle_argv(
    install_root: Path,
    *,
    update_mode: bool,
    force: bool = False,
    dry_run: bool = False,
    skip_kinds: Iterable[str] = (),
    python_executable: Optional[str] = None,
) -> list[str]:
    """Build the ``install-bundle`` argv for the ROOT folder.

    Mirrors the launcher's argv order exactly (D2 parity):

        <python> -m vco_lib.project_init install-bundle
                 --folder <root> --orchestrator-root <root>
                 --project-folder <root>
                 [--update] [--force] [--dry-run]
                 [--skip-kind K ...]
                 --json

    The root passes ``--folder``, ``--orchestrator-root`` and
    ``--project-folder`` all pointing at ``install_root`` (the orchestrator
    clone IS both the source of truth and the install target — the "root
    installs itself" case). Mode flags land BEFORE ``--json`` to match the
    launcher's update wrapper (``run_install_bundle_update_with_root`` appends
    ``--update`` then ``--json``).

    ``skip_kinds`` entries render as repeated ``--skip-kind <K>`` (WP-2's
    additive CLI flag). Sorted for a stable, testable argv.
    """
    python_executable = python_executable or sys.executable
    root_str = str(install_root)
    argv: list[str] = [
        python_executable,
        "-m",
        "vco_lib.project_init",
        "install-bundle",
        "--folder",
        root_str,
        "--orchestrator-root",
        root_str,
        "--project-folder",
        root_str,
    ]
    if update_mode:
        argv.append("--update")
    if force:
        argv.append("--force")
    if dry_run:
        argv.append("--dry-run")
    for kind in sorted(set(skip_kinds)):
        argv.extend(["--skip-kind", kind])
    argv.append("--json")
    return argv


def _empty_actions_envelope(install_root: Path, *, update_mode: bool,
                            force: bool, dry_run: bool,
                            skip_kinds: Iterable[str] = ()) -> dict:
    """Return a bundle-shaped envelope with all-empty action buckets.

    Used as the base for the parse-failure error shape so downstream
    consumers (``install.py``'s human renderer, tests) see the SAME top-level
    keys they get on the happy path. The action keys come from the ONE home
    ``project_init.BUNDLE_ACTION_KEYS`` (WP-2). The additive ``skip_kinds`` key
    is included iff non-empty — matching ``install_project_bundle``'s envelope
    (present only when skip_kinds were requested), so the soft-fail shape is
    top-key-identical to the happy path for the same call.
    """
    action_keys = _bundle_action_keys()
    env: dict = {
        "folder": str(install_root),
        "orchestrator_root": str(install_root),
        "update_mode": bool(update_mode),
        "force": bool(force),
        "dry_run": bool(dry_run),
        "actions": {k: [] for k in action_keys},
        "settings_action": "",
        "manifest_written": False,
        "vco_version": "unknown",
        "warnings": [],
        "errors": [],
    }
    _sk = sorted(set(skip_kinds))
    if _sk:
        env["skip_kinds"] = _sk
    return env


def _bundle_action_keys() -> tuple[str, ...]:
    """The bundle envelope's ``actions`` keys — the ONE home is
    ``project_init.BUNDLE_ACTION_KEYS`` (WP-2, an ordered tuple).

    Imported directly: ``vco_lib`` is part of every healthy VCO install, so a
    missing constant means a BROKEN install and must LOUD-fail (the CLAUDE.md
    loud-fail rule for vco_lib imports), never degrade to a silent inline copy
    that masks the breakage.
    """
    from vco_lib.project_init import BUNDLE_ACTION_KEYS  # noqa: PLC0415
    return tuple(BUNDLE_ACTION_KEYS)


def format_bundle_result_lines(result: dict) -> list[str]:
    """Human-readable rendering of a bundle result envelope.

    PLAN-v0285 D2: the ONE home is ``project_init.format_bundle_result_lines``
    (WP-2 extracted it from ``_cmd_install_bundle``). This thin wrapper imports
    it so ``install.py``'s human output and the CLI's human output are the SAME
    code. Direct import (loud-fail): a missing symbol = broken install, never a
    silent inline-copy degrade.
    """
    from vco_lib.project_init import (  # noqa: PLC0415
        format_bundle_result_lines as _fmt,
    )
    return _fmt(result)


def run_root_bundle_install(
    install_root: Path,
    *,
    update_mode: bool,
    force: bool = False,
    dry_run: bool = False,
    skip_kinds: Iterable[str] = (),
    log_event: Optional[Callable[..., None]] = None,
    python_executable: Optional[str] = None,
    # `_runner` is a test seam. Its return value need only expose `.stdout`,
    # `.stderr`, `.returncode` (a subprocess.CompletedProcess or a duck-typed
    # stub) — hence the `Any` return, so tests can pass a minimal fake.
    _runner: Optional[Callable[[list[str], Path], Any]] = None,
) -> dict:
    """Delegate the root ``.claude/`` install to the ``install-bundle`` CLI.

    Spawns ``root_bundle_argv(...)`` with ``cwd=install_root`` (mirroring the
    Rust ``current_dir(&orch_root)``), captures stdout/stderr, and
    ``json.loads`` the stdout (the SAME parse the launcher performs). The
    parsed envelope is returned verbatim on success.

    On a parse failure (polluted stdout, subprocess launch failure), returns
    an error-shaped envelope carrying a single ``warnings`` entry in the
    launcher's exact wording ("install-bundle produced unparseable output
    (...): stderr tail: ..."). It NEVER raises — ``install.py`` prints PARTIAL
    and continues, mirroring ``update_project_v2``'s posture where a broken
    subprocess degrades to a warning, not an abort (PLAN-v0285 D2).

    ``_runner`` is a test seam: a callable ``(argv, cwd) ->
    CompletedProcess``. Default spawns via ``subprocess.run`` with
    ``stdin=DEVNULL`` (matching the launcher's ``stdin(null())``).

    Args:
        install_root: the orchestrator clone root (source == target).
        update_mode: D4 — ``bool(args.update) or manifest_exists``, resolved
            by the caller.
        force: maps ``--force-materialize-claude-dir`` → ``--force``.
        dry_run: maps the adopt-preview posture → ``--dry-run``.
        skip_kinds: bundle kinds to skip (D5/D6). ``--skip-materialize-claude-
            dir`` resolves to ``("hooks", "scripts", "settings")``;
            ``--no-agents`` / ``--no-skills`` add ``agents`` / ``skills``.
        log_event: optional forensic logger ``(step, phase, detail, data=)``.
        python_executable: override for tests; defaults to ``sys.executable``.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:
            # Older log_event signatures without the data kwarg.
            log_event(step, phase, detail)

    install_root = Path(install_root)
    argv = root_bundle_argv(
        install_root,
        update_mode=update_mode,
        force=force,
        dry_run=dry_run,
        skip_kinds=skip_kinds,
        python_executable=python_executable,
    )

    _log(
        "5b/10", "start",
        f"delegating root .claude/ install to install-bundle "
        f"(update_mode={update_mode}, dry_run={dry_run})",
        data={
            "argv": argv,
            "skip_kinds": sorted(set(skip_kinds)),
        },
    )

    def _default_runner(a: list[str], cwd: Path) -> "subprocess.CompletedProcess":
        # v0.2.85 NIT-2 (hermeticity): the child spawns `python -m
        # vco_lib.project_init`. By default `-m` resolves `vco_lib` from cwd +
        # sys.path — normally cwd == the orchestrator clone, which contains
        # vco_lib/, so it Just Works in production. But to GUARANTEE the child
        # imports the SAME vco_lib as this running parent (not a different one
        # ambiently on the venv's path — the exact footgun that let a stale
        # editable install answer under test), prepend the parent package's repo
        # root to the child's PYTHONPATH. Deterministic + defensive; no behaviour
        # change when cwd already resolves the same package.
        import os as _os  # noqa: PLC0415
        _repo_root = str(Path(__file__).resolve().parent.parent)
        _env = dict(_os.environ)
        _prev = _env.get("PYTHONPATH", "")
        _env["PYTHONPATH"] = (
            _repo_root + (_os.pathsep + _prev if _prev else "")
        )
        return subprocess.run(
            a,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env,
        )

    runner = _runner or _default_runner

    try:
        proc = runner(argv, install_root)
    except Exception as exc:  # noqa: BLE001 — subprocess launch failure soft-fails
        err = f"{type(exc).__name__}: {exc}"
        _log(
            "5b/10", "warn",
            f"install-bundle subprocess failed to start: {err}",
            data={"error": err},
        )
        result = _empty_actions_envelope(
            install_root, update_mode=update_mode, force=force, dry_run=dry_run,
            skip_kinds=skip_kinds,
        )
        result["warnings"].append(
            f"install-bundle subprocess failed to start: {err}. "
            "Root .claude/ may be unchanged."
        )
        result["errors"].append(
            {"path": str(install_root), "error": err}
        )
        return result

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError(
                f"expected a JSON object, got {type(result).__name__}"
            )
    except (ValueError, json.JSONDecodeError) as parse_err:
        # Honest soft-fail — mirror the launcher's parse-failure warning text
        # exactly ("produced unparseable output (...): stderr tail: ...") so
        # the same substring keying works across surfaces, and install.py
        # prints PARTIAL + continues (does NOT raise).
        stderr_tail = " | ".join(stderr.splitlines()[-3:])
        warn = (
            f"install-bundle produced unparseable output ({parse_err}): "
            f"stderr tail: {stderr_tail}. Root .claude/ may be partially "
            "updated."
        )
        _log("5b/10", "warn", warn, data={"stderr_tail": stderr_tail})
        result = _empty_actions_envelope(
            install_root, update_mode=update_mode, force=force, dry_run=dry_run,
            skip_kinds=skip_kinds,
        )
        result["warnings"].append(warn)
        return result

    # Successful parse — thread through the subprocess exit code as a forensic
    # note (the launcher does the same: errors are surfaced via `errors[]`,
    # a non-zero exit is logged but not treated as fatal).
    returncode = getattr(proc, "returncode", 0)
    action_counts = {
        k: len(v) for k, v in (result.get("actions") or {}).items() if v
    }
    _log(
        "5b/10",
        "warn" if result.get("errors") else "ok",
        f"install-bundle delegated run complete (exit={returncode}); "
        f"actions={action_counts}",
        data={
            "returncode": returncode,
            "action_counts": action_counts,
            "settings_action": result.get("settings_action", ""),
            "manifest_written": result.get("manifest_written", False),
            "warnings": result.get("warnings", []),
            "errors": result.get("errors", []),
        },
    )
    _record_root_settings_action(install_root, result)
    return result


# ---------------------------------------------------------------------------
# v0.2.103 F4 (Windows field run): the step-5b → step-9 settings.json handoff
# ---------------------------------------------------------------------------
#
# On a fresh clone step 5b (this delegated bundle run) CREATES the root's
# `.claude/settings.json` from the template; step 9 (`install.py
# _configure_claude_settings`) then found a file with no `_vco_managed_keys`
# and treated VCO's own fresh file as user-owned: `settings.json.vco-new` +
# a `claude_settings_user_modified_preserved` deferral. The bundle's own
# envelope says what it did; recording it here lets step 9 recognise a file
# VCO created in THIS process — the run signal, not a guess from the file's
# contents (an older root whose file was user-edited still carries the same
# template keys).

#: ``resolved install root → settings_action`` of the last delegated run in
#: this process. Only ``"created"`` is acted on.
_ROOT_SETTINGS_ACTION: dict[str, str] = {}


def _record_root_settings_action(install_root: Path, result: dict) -> None:
    try:
        key = str(Path(install_root).resolve())
    except OSError:
        return
    _ROOT_SETTINGS_ACTION[key] = str(result.get("settings_action", "") or "")


def root_settings_created_this_run(install_root: Path) -> bool:
    """True iff this process's delegated bundle run CREATED
    ``<install_root>/.claude/settings.json`` (settings_action ``created``)."""
    try:
        key = str(Path(install_root).resolve())
    except OSError:
        return False
    return _ROOT_SETTINGS_ACTION.get(key) == "created"


def adopt_bundle_created_settings(existing: dict, defaults: dict,
                                  sentinel: str) -> dict:
    """Merge install's step-9 ``defaults`` into a settings.json the bundle
    created this run, FILL-ONLY: every value already in the file wins (the
    template's hooks/permissions, and any env the config projection wrote in
    between — the canonical env writer), defaults fill what is missing,
    ``permissions.allow`` is the ordered union, and the managed-keys
    ``sentinel`` is stamped so later installs take the managed-block path.
    Pure: returns a new dict."""
    merged = dict(existing)
    for key, value in defaults.items():
        if key == sentinel:
            continue
        cur = merged.get(key)
        if key not in merged:
            merged[key] = value
        elif isinstance(cur, dict) and isinstance(value, dict):
            filled = dict(cur)
            for k, v in value.items():
                if k == "allow" and isinstance(filled.get(k), list) and isinstance(v, list):
                    filled[k] = list(filled[k]) + [x for x in v if x not in filled[k]]
                else:
                    filled.setdefault(k, v)
            merged[key] = filled
    merged[sentinel] = list(defaults.get(sentinel) or [])
    return merged


def replace_all_settings(install_root: Path, defaults: dict, sentinel: str) -> dict:
    """The root ``settings.json`` that ``install.py --adopt-project-replace-all``
    writes: what a fresh install lands — the shipped settings template (minus
    the hooks the launcher has parked for the root, exactly like the bundle's
    create branch, :func:`vco_lib.bundle_settings_io.fresh_settings_from_template`)
    with step 9's ``defaults`` filled in through
    :func:`adopt_bundle_created_settings`, the merge a fresh install's step 9
    applies to the file the bundle created.

    What replace-all resets is every user edit: nothing of the existing file is
    read. What it no longer does (v0.2.104) is drop the template's own keys —
    ``hooks``, ``skillOverrides``, ``idleCompaction``, the permission denies —
    until the next bundle update.

    Raises ``OSError`` / ``ValueError`` when the shipped template cannot be read
    or parsed (a broken install; the caller leaves the file alone and says so).
    """
    from vco_lib.bundle_settings_io import fresh_settings_from_template, settings_template_path
    from vco_lib.parked_hooks import read_parked_hooks

    fresh = fresh_settings_from_template(
        settings_template_path(install_root),
        parked=read_parked_hooks(Path(install_root)), kept_out=[])
    return adopt_bundle_created_settings(fresh, defaults, sentinel)
