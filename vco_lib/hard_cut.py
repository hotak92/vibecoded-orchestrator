# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""§7 HARD-CUT primitive — code-only, data-preserving clean reset.

This is the canonical implementation of DESIGN-v0300-update-system-architecture
.md §7.4 ("What a hard cut actually executes"). A **hard cut** is *"fresh
checkout of the CODE + re-run install"* — it is NOT "wipe state". It may touch
ONLY the git clone + derived caches (§7.2 MAY-TOUCH); it MUST preserve every
``~/.vct/*`` DB, the Weaviate container + all vectors, ``knowledge/**``,
``.claude/state/``, and ``~/.vct-secrets/**`` (§7.1 MUST-PRESERVE).

**INERT in v0.2.60.** ``hard_cut`` is fully built + tested but is NOT invoked
from any v0.2.60 code path. Its ONLY trigger is the ``min_upgradable_from``
version floor (Piece 5), which is set to an inert value (``"0.0.0"``) so the
below-floor branch never fires until v0.3.0 raises the floor. The Rust
``perform_hard_cut`` command exists but is unreachable in the normal update
flow (gated behind the inert floor check). See the test
``tests/test_hard_cut.py::test_hard_cut_not_invoked_by_normal_update`` and the
Rust ``test_perform_hard_cut_not_wired_into_update_orchestrator``.

Execution steps (§7.4 — performed EXACTLY in this order):

  1. ``git bundle create <vct_root>/backups/pre-hardcut-<stamp>.bundle --all``
     + ``git bundle verify``. **ABORT on failure** (§7.3.3) — never proceed
     when the safety net failed.
  2. Write a ``hard_cut_performed`` deferral naming the bundle path + the exact
     restore command (§7.3.2).
  3. ``git fetch upstream <to-tag>`` + ``git reset --hard <to-tag>`` (CODE only).
     Corrupt-``.git`` → sibling-clone fallback (noted as a secondary path; the
     primary is in-place reset).
  4. ``install.py --update`` (idempotent).
  5. The migration runner (Piece 2 — REUSED via ``run_schema_migrations``; this
     module does NOT duplicate it).
  6. Binary refresh + relaunch is the launcher's existing ``finalize`` — the
     primitive RETURNS a result; the Rust caller drives finalize.

Time/random are unavailable in the build environment, so the ``stamp`` is
PASSED IN by the caller (the launcher generates it from wall-clock). The
production code's Rust caller passes a real timestamp; tests inject a fixed
stamp. The primitive itself never calls ``time``/``random``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "HardCutResult",
    "MUST_PRESERVE_VCT_FILES",
    "must_preserve_paths",
    "hard_cut",
]

#: §7.1 MUST-PRESERVE — files/dirs under ``~/.vct`` a hard cut NEVER touches.
#: (The Weaviate container/volume, ``knowledge/**``, ``.claude/state/`` and
#: ``~/.vct-secrets/**`` are preserved by construction — a code-only
#: ``git reset --hard <tag>`` + ``git bundle`` never reach them — but they are
#: enumerated in :func:`must_preserve_paths` so the test can assert the executed
#: command set touches none of them.)
MUST_PRESERVE_VCT_FILES: tuple[str, ...] = (
    "launcher.db",
    "hub.db",
    "services.toml",
    "hub.token",
    "hub.port",
    "hub.pid",
)

#: Generous timeouts: the install + migrate steps can be slow on a cold venv /
#: large KG. git ops are quick.
_GIT_TIMEOUT = 300
_INSTALL_TIMEOUT = 1800


@dataclass
class HardCutResult:
    """Outcome of a :func:`hard_cut` invocation.

    ``ok`` is True only when EVERY step (bundle+verify, deferral, reset,
    install, migrate) completed. ``aborted_before_reset`` is True when the
    safety net (git bundle) failed — in that case NOTHING destructive ran
    (§7.3.3). The Rust caller drives binary-refresh + relaunch (step 6) only
    when ``ok`` is True.
    """

    from_version: str
    to_version: str
    ok: bool = False
    #: True when step 1's bundle/verify failed → reset never ran (no data risk).
    aborted_before_reset: bool = False
    bundle_path: Optional[str] = None
    bundle_verified: bool = False
    deferral_written: bool = False
    reset_done: bool = False
    install_done: bool = False
    migrate_done: bool = False
    #: Set when the in-place reset path was abandoned for the sibling-clone
    #: fallback (corrupt .git). v0.2.60: the fallback is a documented secondary
    #: path; the primitive records the need rather than performing the clone.
    sibling_clone_required: bool = False
    error: Optional[str] = None
    #: Step-by-step log for the launcher / forensics.
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "ok": self.ok,
            "aborted_before_reset": self.aborted_before_reset,
            "bundle_path": self.bundle_path,
            "bundle_verified": self.bundle_verified,
            "deferral_written": self.deferral_written,
            "reset_done": self.reset_done,
            "install_done": self.install_done,
            "migrate_done": self.migrate_done,
            "sibling_clone_required": self.sibling_clone_required,
            "error": self.error,
            "steps": self.steps,
        }


def must_preserve_paths(vct_root: Path, clone_root: Path) -> list[Path]:
    """Return the absolute MUST-PRESERVE paths (§7.1) for an install.

    Used by the test to assert that the executed git/install/migrate command
    set touches none of these. Enumerates: the ``~/.vct`` DBs/config/runtime
    files, ``~/.vct-secrets/``, the project's ``knowledge/`` tree, and
    ``.claude/state/``. The Weaviate container/volume lives outside the clone
    (machine-local podman volume) so there's no path under the clone to list —
    a code-only ``git reset --hard`` can never reach it.
    """
    paths: list[Path] = [vct_root / name for name in MUST_PRESERVE_VCT_FILES]
    # ~/.vct-secrets lives beside ~/.vct (sibling), not under it.
    paths.append(vct_root.parent / ".vct-secrets")
    paths.append(clone_root / "knowledge")
    paths.append(clone_root / ".claude" / "state")
    return paths


def hard_cut(
    from_version: str,
    to_version: str,
    *,
    clone_root: Path,
    vct_root: Path,
    project_id: Optional[str],
    stamp: str,
    upstream_remote: str = "vco_upstream",
    db_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    weaviate_url: str = "http://localhost:8081",
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    deferral_writer: Optional[Callable[..., bool]] = None,
    migration_runner: Optional[Callable[..., object]] = None,
    now_ms: Optional[int] = None,
) -> HardCutResult:
    """Execute a §7.4 hard cut (code-only, data-preserving). INERT in v0.2.60.

    Steps run EXACTLY in §7.4 order; a failure at any step before the reset
    aborts WITHOUT any destructive action (the bundle is the safety net and
    must succeed first, §7.3.3).

    Args:
        from_version: the installed version (read by the caller).
        to_version: the target tag/version (e.g. ``"0.3.0"`` → tag ``v0.3.0``).
        clone_root: the orchestrator git clone root (the CODE; the ONLY thing
            ``git reset --hard`` touches).
        vct_root: ``~/.vct`` — where the bundle backup lands
            (``<vct_root>/backups/``). NEVER reset/touched beyond writing the
            backup file + the deferral.
        project_id: project id threaded to the migration runner (step 5).
        stamp: the backup-filename timestamp, PASSED IN (time/random
            unavailable to the agent; the production caller supplies wall-clock).
        upstream_remote: pinned public remote (caller ran ensure_upstream_remote).
        db_path: launcher.db for the migration runner (defaults under vct_root).
        env: resolved env for subprocesses.
        weaviate_url: target Weaviate for the migration runner.
        runner: subprocess runner (injectable for tests).
        deferral_writer: callable ``(clone_root, bundle_path, from, to,
            restore_cmd) -> bool`` writing the ``hard_cut_performed`` deferral
            (injectable; defaults to :func:`_default_deferral_writer`).
        migration_runner: callable matching ``run_schema_migrations`` (injectable;
            defaults to the real Piece-2 runner — REUSED, not duplicated).
        now_ms: materialized_at for the migration runner (injected).

    Returns:
        :class:`HardCutResult`.
    """
    run = runner or _default_runner
    write_deferral = deferral_writer or _default_deferral_writer
    res = HardCutResult(from_version=from_version, to_version=to_version)
    sub_env = dict(env or {})
    sub_env.setdefault("WEAVIATE_URL", weaviate_url)
    tag = to_version if to_version.startswith("v") else f"v{to_version}"

    if not clone_root.joinpath(".git").exists():
        # Corrupt / missing .git → the in-place reset path is impossible.
        # v0.2.60: record the sibling-clone-fallback need (DESIGN §7.4 step 3
        # secondary path) and ABORT rather than perform a clone in this
        # primitive (the clone+repoint is the launcher's job; this is the
        # documented secondary path). No destructive action taken.
        res.sibling_clone_required = True
        res.aborted_before_reset = True
        res.error = (
            f"{clone_root}/.git is missing or corrupt — the in-place "
            "git reset path is unavailable. The launcher must fall back to a "
            "sibling-clone + repoint (DESIGN §7.4 step 3 secondary path). "
            "Nothing was touched."
        )
        res.steps.append("ABORT: corrupt/missing .git → sibling-clone fallback")
        return res

    # ---- Step 1: git bundle --all + verify (the safety net) -------------
    backups_dir = vct_root / "backups"
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.aborted_before_reset = True
        res.error = (
            f"could not create backup dir {backups_dir} ({exc}); ABORTING "
            "before any destructive step (§7.3.3)"
        )
        res.steps.append(f"ABORT: backup dir uncreatable ({exc})")
        return res

    bundle_path = backups_dir / f"pre-hardcut-{stamp}.bundle"
    res.bundle_path = str(bundle_path)
    try:
        create = run(
            ["git", "bundle", "create", str(bundle_path), "--all"],
            cwd=str(clone_root), env=sub_env, timeout=_GIT_TIMEOUT,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.aborted_before_reset = True
        res.error = f"git bundle create failed to run ({exc}); ABORTING (§7.3.3)"
        res.steps.append(f"ABORT: git bundle create spawn error ({exc})")
        return res
    if create.returncode != 0:
        res.aborted_before_reset = True
        res.error = (
            f"git bundle create exited rc={create.returncode}; ABORTING before "
            "any destructive step (§7.3.3). The clone is untouched."
        )
        res.steps.append(f"ABORT: git bundle create rc={create.returncode}")
        return res
    res.steps.append(f"git bundle created at {bundle_path}")

    try:
        verify = run(
            ["git", "bundle", "verify", str(bundle_path)],
            cwd=str(clone_root), env=sub_env, timeout=_GIT_TIMEOUT,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.aborted_before_reset = True
        res.error = f"git bundle verify failed to run ({exc}); ABORTING (§7.3.3)"
        res.steps.append(f"ABORT: git bundle verify spawn error ({exc})")
        return res
    if verify.returncode != 0:
        res.aborted_before_reset = True
        res.error = (
            f"git bundle verify exited rc={verify.returncode}; the backup is "
            "NOT trustworthy → ABORTING before any destructive step (§7.3.3). "
            "The clone is untouched."
        )
        res.steps.append(f"ABORT: git bundle verify rc={verify.returncode}")
        return res
    res.bundle_verified = True
    res.steps.append("git bundle verified")

    # ---- Step 2: write the hard_cut_performed deferral ------------------
    restore_cmd = (
        f"git -C {clone_root} fetch {bundle_path} "
        f"'refs/*:refs/heads/restored-pre-hardcut/*' && "
        f"git -C {clone_root} reset --hard restored-pre-hardcut/main"
    )
    try:
        res.deferral_written = bool(
            write_deferral(
                clone_root=clone_root,
                bundle_path=str(bundle_path),
                from_version=from_version,
                to_version=to_version,
                restore_cmd=restore_cmd,
            )
        )
    except Exception as exc:  # never block on a deferral-write failure
        logger.warning("hard_cut: deferral write failed (non-fatal): %s", exc)
        res.deferral_written = False
    res.steps.append(
        f"hard_cut_performed deferral {'written' if res.deferral_written else 'write-failed'}"
    )

    # ---- Step 3: git fetch upstream <tag> + git reset --hard <tag> ------
    # (CODE ONLY — never touches ~/.vct, secrets, the Weaviate volume, or the
    # knowledge/ tree. git reset --hard only mutates git-TRACKED files in the
    # clone; ~/.vct + ~/.vct-secrets are outside the clone, knowledge/**/*.md
    # is committed and is restored from the bundle if anything goes wrong.)
    try:
        fetch = run(
            ["git", "fetch", upstream_remote, tag],
            cwd=str(clone_root), env=sub_env, timeout=_GIT_TIMEOUT,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.error = f"git fetch {upstream_remote} {tag} failed to run ({exc})"
        res.steps.append(f"FAIL: git fetch spawn error ({exc})")
        return res
    if fetch.returncode != 0:
        res.error = (
            f"git fetch {upstream_remote} {tag} exited rc={fetch.returncode}; "
            "the reset did NOT run. Restore is unnecessary (clone untouched)."
        )
        res.steps.append(f"FAIL: git fetch rc={fetch.returncode}")
        return res
    res.steps.append(f"git fetch {upstream_remote} {tag} ok")

    try:
        reset = run(
            ["git", "reset", "--hard", tag],
            cwd=str(clone_root), env=sub_env, timeout=_GIT_TIMEOUT,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.error = (
            f"git reset --hard {tag} failed to run ({exc}); restore from "
            f"{bundle_path} if the tree is inconsistent"
        )
        res.steps.append(f"FAIL: git reset spawn error ({exc})")
        return res
    if reset.returncode != 0:
        res.error = (
            f"git reset --hard {tag} exited rc={reset.returncode}; restore from "
            f"{bundle_path} if the tree is inconsistent"
        )
        res.steps.append(f"FAIL: git reset rc={reset.returncode}")
        return res
    res.reset_done = True
    res.steps.append(f"git reset --hard {tag} ok (CODE-only)")

    # ---- Step 4: install.py --update (idempotent) -----------------------
    install_py = clone_root / "install.py"
    if not install_py.is_file():
        res.error = (
            f"install.py not found at {install_py} after reset; the reset "
            "completed but install could not run. Restore from the bundle."
        )
        res.steps.append("FAIL: install.py missing post-reset")
        return res
    try:
        install = run(
            [sys.executable, str(install_py), "--update"],
            cwd=str(clone_root), env=sub_env, timeout=_INSTALL_TIMEOUT,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.error = f"install.py --update failed to run ({exc})"
        res.steps.append(f"FAIL: install.py spawn error ({exc})")
        return res
    if install.returncode != 0:
        res.error = (
            f"install.py --update exited rc={install.returncode}; the code is "
            "at the target version but install did not complete. Re-run "
            "`python install.py --update` from the clone, or restore the bundle."
        )
        res.steps.append(f"FAIL: install.py rc={install.returncode}")
        return res
    res.install_done = True
    res.steps.append("install.py --update ok")

    # ---- Step 5: migration runner (Piece 2 — REUSED, not duplicated) ----
    runner_fn = migration_runner or _default_migration_runner
    effective_db = db_path or (vct_root / "launcher.db")
    try:
        runner_fn(
            db_path=effective_db,
            project_id=project_id,
            migrations_dir=clone_root / "migrations",
            weaviate_url=weaviate_url,
            env=sub_env,
            now_ms=now_ms,
            project_root=clone_root,
            # The hard cut is the ROOT orchestrator update → migrate the
            # orchestrator-wide artifacts too (shared KG + Layer-5 shapes).
            include_orchestrator_wide=True,
        )
        res.migrate_done = True
        res.steps.append("migration runner ok (Piece 2, reused)")
    except Exception as exc:  # the runner is soft-fail by contract
        res.error = (
            f"migration runner raised ({type(exc).__name__}: {exc}); the code "
            "+ install are at the target version but migrations did not run. "
            "Re-run `python -m vco_lib.project_init migrate-schema`."
        )
        res.steps.append(f"FAIL: migration runner error ({exc})")
        return res

    # Step 6 (binary refresh + relaunch) is the launcher's finalize — return.
    res.ok = True
    res.steps.append("hard_cut complete (caller drives binary refresh + relaunch)")
    return res


# ---------------------------------------------------------------------------
# Injectable defaults
# ---------------------------------------------------------------------------


def _default_runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def _default_deferral_writer(
    *,
    clone_root: Path,
    bundle_path: str,
    from_version: str,
    to_version: str,
    restore_cmd: str,
) -> bool:
    """Write the ``hard_cut_performed`` deferral to UPDATE_DEFERRED.md (§7.3.2).

    Mirrors the existing deferral writers (e.g.
    ``_write_node_formats_migration_deferral``). Soft-fail → False.
    """
    try:
        from .deferral_report import DeferralEntry, DeferralReport

        report = DeferralReport.read(clone_root)
        report.add_entry(
            DeferralEntry(
                condition_id="hard_cut_performed",
                severity="info",
                title=f"Hard cut performed: v{from_version} → v{to_version}",
                detected=(
                    f"A code-only hard cut reset the orchestrator clone from "
                    f"v{from_version} to v{to_version} (`git reset --hard`). "
                    f"Your launcher.db, hub.db, services.toml, Weaviate vectors, "
                    f"knowledge/** nodes, .claude/state/, and ~/.vct-secrets/ "
                    f"were PRESERVED — only the git-tracked code changed. A full "
                    f"backup of every local branch/commit (including your "
                    f"committed KG nodes) was saved to `{bundle_path}`."
                ),
                why_deferred=(
                    "This entry is a forensic record + restore handle, not a "
                    "pending action. It documents the exact command to restore "
                    "the pre-hard-cut clone state should you need it."
                ),
                command_to_apply=(
                    "# Restore the pre-hard-cut clone state from the backup bundle:\n"
                    f"{restore_cmd}\n"
                    "# Then dismiss:\n"
                    "python -m vco_lib.project_init dismiss-deferral "
                    f"--folder {str(clone_root)!r} --condition-id hard_cut_performed"
                ),
            )
        )
        report.write(clone_root)
        return True
    except Exception as exc:  # never block the hard cut on a deferral write
        logger.warning(
            "_default_deferral_writer: hard_cut_performed write failed (%s)", exc
        )
        return False


def _default_migration_runner(**kwargs):
    """Lazy import + call the Piece-2 runner so this module has no hard import
    dependency at module-load (and tests can monkeypatch cleanly)."""
    from .schema_migration_runner import run_schema_migrations

    return run_schema_migrations(**kwargs)
