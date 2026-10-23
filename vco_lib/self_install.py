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

The rest of the module is the root's step-9 settings.json handling that
install.py calls (:func:`configure_root_settings`, :func:`replace_all_settings`,
:func:`route_root_machine_env`). The bundle delegation itself is two functions
with one concern each (one-home rule):

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


def route_root_machine_env(
    install_root: Path,
    expected_env: Callable[[], dict],
    *,
    log: Optional[Callable[..., Any]] = None,
) -> list[str]:
    """v0.2.104 L5 (review R14B B2): move this machine's service URLs and
    ports out of the root ``.claude/settings.json`` ``env`` into
    ``.claude/settings.local.json``, as the env projection does for every
    project.

    ``install.py``'s ``_build_vco_settings_defaults`` and the managed-block
    merge that applies it (hooks included) stay as they are: this runs AFTER
    that write and moves only the machine keys whose value EQUALS what the
    defaults put there (``expected_env()`` — ``WEAVIATE_URL``, ``OLLAMA_URL``,
    ``CODE_EMBED_SERVICE_URL``, ``GRPC_PORT``), the evidence rule, so a value
    the user set stays. Nothing moves through a symlinked ``.claude/`` or
    ``settings.json``, or while git tracks the local file or cannot say (then a
    ``settings_local_json_tracked`` / ``settings_local_layer_unknown`` entry is
    recorded). Soft-fail: returns
    the keys moved, ``[]`` on any failure (logged through ``log``).
    """
    from vco_lib.settings_local_env import move_machine_env_out_of_settings
    from vco_lib.symlink_handler import is_symlink_blocking

    claude = Path(install_root) / ".claude"
    if is_symlink_blocking(claude) or is_symlink_blocking(claude / "settings.json"):
        return []
    try:
        moved = move_machine_env_out_of_settings(Path(install_root), expected_env())
    except Exception as exc:  # noqa: BLE001 — never fail the install over this
        if log is not None:
            log("9/10", "warn", f"route_root_machine_env: {exc}")
        return []
    if moved:
        print(f"  Claude settings: machine-specific env moved to settings.local.json: {', '.join(moved)}")
    return moved


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


# ---------------------------------------------------------------------------
# install.py step 9: the root ``.claude/settings.json`` (moved here from
# ``install.py::_configure_claude_settings`` in v0.2.104; that function stays a
# thin shim supplying install.py's defaults, sentinel and managed-block merge)
# ---------------------------------------------------------------------------


def configure_root_settings(
    install_root: Path,
    build_defaults: Callable[[], dict],
    *,
    sentinel: str,
    managed_keys: Iterable[str],
    merge: Callable[[dict, dict], dict],
    adopt_project_mode: Optional[str] = None,
    deferral_report: Any = None,
    settings_template_skipped: bool = False,
) -> None:
    """Create or refresh ``<install_root>/.claude/settings.json`` (install.py step 9).

    ``build_defaults()`` returns install.py's VCO defaults (called only once
    the target file is settled); ``sentinel`` is the managed-keys marker,
    ``managed_keys`` the keys it lists, ``merge(existing, defaults)`` the
    managed-block merge. v0.2.46 V47-A (Gap A) — behaviour matrix:

    * File missing → write what a fresh install lands: the shipped settings
      template + VCO defaults (including the sentinel), the same result as
      replace-all below — or the defaults alone when
      ``settings_template_skipped`` (``--skip-materialize-claude-dir``: the
      template's hooks would name scripts this run did not materialize).
    * File exists + parses + has the sentinel → managed-block merge:
      refresh listed keys from defaults, preserve every other top-level key.
    * File exists + parses + no sentinel (legacy / external) → treat as
      user-owned. Land VCO defaults as ``settings.json.vco-new`` sibling,
      emit deferral entry, leave original alone.
    * File exists + parse fails → emit deferral entry, leave file alone.
    * ``adopt_project_mode == "replace-all"`` → unconditionally overwrite
      with what a fresh install lands — the shipped settings template plus
      VCO defaults (:func:`replace_all_settings`; advanced / explicit opt-in
      via ``--adopt-project-replace-all``). Every user edit is reset; no
      template key is dropped. A missing template leaves the file alone (or
      absent) with a stderr error.
    * ``adopt_project_mode in (None, "adopt")`` → use the safe matrix above.
    * ``adopt_project_mode == "no-adopt"`` and ``"dry-run"`` are handled
      upstream in install.py ``main()`` before this function is reached.

    * No sentinel but step 5b's bundle created the file THIS run (v0.2.103
      F4) → VCO's own: defaults filled in, sentinel stamped, no deferral.

    ``deferral_report`` is optional (deferrals skipped when not threaded).
    V47-B (Gap B): a symlinked ``.claude/`` or ``settings.json`` is never
    written through — content goes to a ``.vco-new`` sibling + a deferral,
    mode-AGNOSTIC (user decision 2026-06-03).
    """
    import os

    from vco_lib.deferral_report import DeferralEntry
    from vco_lib.symlink_handler import (
        check_vco_new_collision,
        compute_vco_new_path,
        emit_symlink_deferral,
        is_symlink_blocking,
    )

    install_root = Path(install_root)
    settings_dir = install_root / ".claude"

    # V47-B (Gap B): a symlinked .claude/ → write to a .vco-new sibling dir.
    if is_symlink_blocking(settings_dir):
        vco_new_dir = compute_vco_new_path(settings_dir)
        if deferral_report is not None:
            emit_symlink_deferral(
                deferral_report, settings_dir, vco_new_dir,
                install_root=install_root,
            )
        # post-adversarial L1: never clobber a prior run's .vco-new dir.
        if check_vco_new_collision(
            vco_new_dir,
            install_root=install_root,
            deferral=deferral_report,
        ):
            print(
                f"  Claude settings: prior {vco_new_dir.name}/ from a "
                f"previous run exists — leaving untouched (see "
                f"UPDATE_DEFERRED.md for reconciliation)"
            )
            return
        print(
            f"  Claude settings: .claude/ is a symlink — writing to "
            f"{vco_new_dir.name}/ instead (see UPDATE_DEFERRED.md)"
        )
        settings_dir = vco_new_dir

    settings_dir.mkdir(exist_ok=True)

    settings_file = settings_dir / "settings.json"

    # V47-B: a symlinked settings.json (dangling included) → .vco-new sibling.
    if is_symlink_blocking(settings_file):
        vco_new_file = compute_vco_new_path(settings_file)
        if deferral_report is not None:
            emit_symlink_deferral(
                deferral_report, settings_file, vco_new_file,
                install_root=install_root,
            )
        # post-adversarial L1: ditto for a prior settings.json.vco-new.
        if check_vco_new_collision(
            vco_new_file,
            install_root=install_root,
            deferral=deferral_report,
        ):
            print(
                f"  Claude settings: prior {vco_new_file.name} from a "
                f"previous run exists — leaving untouched (see "
                f"UPDATE_DEFERRED.md for reconciliation)"
            )
            return
        print(
            f"  Claude settings: settings.json is a symlink — writing to "
            f"{vco_new_file.name} instead (see UPDATE_DEFERRED.md)"
        )
        settings_file = vco_new_file

    defaults = build_defaults()
    rendered_defaults = json.dumps(defaults, indent=2) + "\n"

    # Cases 1 and 2 write the WHOLE file: both write what a fresh install
    # lands (v0.2.104) — the shipped template + these defaults, or the
    # defaults alone under --skip-materialize-claude-dir (the template's
    # hooks would name scripts this run never materialized).
    whole_file = not os.path.lexists(os.fspath(settings_file)) or adopt_project_mode == "replace-all"
    if whole_file:
        # Case 1: file missing (lexists: a diverted dangling symlink is not
        # "missing"). Case 2: --adopt-project-replace-all — overwrite outright
        # (it used to write `defaults` alone and drop every template-only key).
        verb = "replaced" if os.path.lexists(os.fspath(settings_file)) else "created"
        try:
            fresh = (defaults if settings_template_skipped else
                     replace_all_settings(
                         install_root, defaults, sentinel))
        except (OSError, ValueError) as exc:
            print(
                f"  Claude settings: NOT {verb} — the shipped settings template "
                f"could not be read ({type(exc).__name__}: {exc}); the install "
                "looks broken, re-clone or repair templates/ and re-run",
                file=sys.stderr,
            )
            return
        settings_file.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
        if verb == "replaced":
            print("  Claude settings: replaced (--adopt-project-replace-all)")
        return

    # Case 3: file exists — try to parse and decide.
    try:
        existing_text = settings_file.read_text(encoding="utf-8")
        existing = json.loads(existing_text)
    except (OSError, json.JSONDecodeError) as exc:
        # Parse / read failed — emit deferral and leave file alone.
        print(
            "  Claude settings: existing file unparseable "
            f"({type(exc).__name__}); preserved on disk"
        )
        if deferral_report is not None:
            deferral_report.add_entry(
                DeferralEntry(
                    condition_id="claude_settings_unparseable",
                    title="`.claude/settings.json` unparseable; preserved on disk",
                    detected=(
                        f"`.claude/settings.json` exists but failed to parse as "
                        f"JSON ({type(exc).__name__}: {exc}). VCO did not modify "
                        "the file."
                    ),
                    why_deferred=(
                        "Auto-replacing an unparseable settings file would "
                        "discard user customizations that may still be "
                        "recoverable. Inspect manually and either repair the "
                        "JSON or accept VCO defaults via the command below."
                    ),
                    command_to_apply=(
                        "python install.py --adopt-project-replace-all"
                    ),
                    severity="warning",
                    kg_node_refs=[],
                )
            )
        return

    if not isinstance(existing, dict):
        # A non-object JSON (array/string/number) cannot merge — as unparseable.
        print(
            "  Claude settings: existing file is not a JSON object; "
            "preserved on disk"
        )
        if deferral_report is not None:
            deferral_report.add_entry(
                DeferralEntry(
                    condition_id="claude_settings_unparseable",
                    title=(
                        "`.claude/settings.json` is not a JSON object; "
                        "preserved on disk"
                    ),
                    detected=(
                        "`.claude/settings.json` parsed to a "
                        f"{type(existing).__name__} (expected a JSON object). "
                        "VCO did not modify the file."
                    ),
                    why_deferred=(
                        "VCO's settings template is a JSON object; cannot "
                        "merge with a non-object structure."
                    ),
                    command_to_apply=(
                        "python install.py --adopt-project-replace-all"
                    ),
                    severity="warning",
                    kg_node_refs=[],
                )
            )
        return

    # Case 4: file parsed and is a dict — managed-block detection.
    if sentinel in existing:
        # Managed-block-aware file → refresh listed keys, preserve rest.
        merged = merge(existing, defaults)
        settings_file.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8"
        )
        print("  Claude settings: managed keys refreshed (user keys preserved)")
        return

    # v0.2.103 F4: step 5b's bundle CREATED this file this run — VCO's own, so fill in the
    # defaults and stamp the sentinel (vco_lib.self_install). Not a tautology: a symlinked
    # .claude/ or settings.json diverted settings_file to a .vco-new sibling above.
    if settings_file == install_root / ".claude" / "settings.json" and \
            root_settings_created_this_run(install_root):
        adopted = adopt_bundle_created_settings(
            existing, defaults, sentinel)
        settings_file.write_text(json.dumps(adopted, indent=2) + "\n", encoding="utf-8")
        print("  Claude settings: VCO defaults added to the file install-bundle created")
        return

    # Case 5: no _vco_managed_keys, not created this run — legacy / external:
    # VCO defaults land as a `.vco-new` sibling + a deferral.
    vco_new_path = settings_file.with_suffix(settings_file.suffix + ".vco-new")
    vco_new_path.write_text(rendered_defaults, encoding="utf-8")
    print(
        "  Claude settings: legacy file detected (no `_vco_managed_keys`); "
        f"VCO defaults written to {vco_new_path.name}, original preserved"
    )
    if deferral_report is not None:
        deferral_report.add_entry(
            DeferralEntry(
                condition_id="claude_settings_user_modified_preserved",
                title=(
                    "`.claude/settings.json` preserved (no VCO-managed-block "
                    "marker)"
                ),
                detected=(
                    "`.claude/settings.json` exists but lacks the top-level "
                    f"`{sentinel}` array that marks the file "
                    "as VCO-managed. VCO treats this as user-owned and did "
                    f"not modify it. VCO defaults landed at "
                    f"`.claude/settings.json.vco-new`."
                ),
                why_deferred=(
                    "Without the managed-keys sentinel VCO cannot tell which "
                    "top-level keys are safe to refresh and which are user "
                    "extensions. Manual merge keeps you in control. To accept "
                    "VCO defaults outright run the command below; to merge "
                    "selectively, copy the keys you want from "
                    "`.claude/settings.json.vco-new` into your existing file, "
                    f"then add a top-level "
                    f'`"{sentinel}": '
                    f"{list(managed_keys)!r}` so future updates "
                    "refresh those keys automatically."
                ),
                command_to_apply=(
                    "python install.py --adopt-project-replace-all"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
