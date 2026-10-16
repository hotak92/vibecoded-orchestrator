# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for the per-project code-graph WALK single-flight claim
(v0.2.103).

THE FIELD DEFECT (2026-10-14, v0.2.102 dogfood): two code-graph walks of the
SAME project ran at once — twice observed: a launcher-spawned walk (the
post-update "building code graph" build task, analyzer with
``--prune-stale``) racing a resync driver's ``--force-rewalk`` 15 s later,
and two analyzers 15 min apart on another project. The resync driver's
per-project claim (``codegraph_resync._driver_claim_lock_path``) serialises
DRIVERS only; nothing the launcher, the deferral-retry dispatcher, the extras
refresh or a shell wrapper spawned ever took it. Consequences: double
embedding load on the one GPU / Weaviate, and a ``--prune-stale`` walk racing
another walk's inserts.

THE RULE: at most ONE full/pruning walk per project at a time, machine-wide.
Every spawner of such a walk takes the claim here BEFORE the analyzer runs
and holds it for the walk's whole life (flock semantics: the OS frees it when
the holder dies, so a crashed walk leaves nothing to clean up). A second
request for a project already walking is REFUSED — never queued, never
killed, never given a timeout that could cut a multi-hour walk: it reports
"already running" to the log and, through
:data:`WALK_ALREADY_RUNNING_RC`, back to the launcher UI caller.

Who takes it (every spawner of a full or pruning walk):

  * the resync driver's blocking analyzer run
    (``codegraph_resync._run_resync_and_verify_slotted`` — in-process
    :func:`walk_claim`);
  * the deferral-retry full-tree incremental walk
    (``deferral_retry.retry_code_graph_walk`` — in-process);
  * the extras commit-stamp re-index
    (``codegraph_extras_refresh.refresh_extras`` — in-process);
  * every launcher Rust spawn and the user CLI, via the shell wrappers
    ``templates/scripts/code-graph-analyze{,.ps1}`` which now exec
    ``python -m vco_lib.codegraph_walk <analyzer> [args...]`` (:func:`main`),
    and the two Rust direct spawns (``codegraph_reanalyze.rs`` /
    ``project_codegraph_extras.rs``) which spawn the same ``-m`` hop
    (``vct_launcher_core::process::claimed_walk_args``). Rust never mirrors
    the lock logic — it only interprets the exit code.

SCOPED incremental runs (``--only-file`` / ``--only-files-from`` — the
Stop-hook drain batches) deliberately do NOT claim, with evidence:

  * the drain already serializes per canonical root
    (``codegraph_drain_root_<md5>.lock`` mkdir, requeue-on-contention) and
    rate-limits to one batch per 2 min — a multi-hour full walk holding the
    claim would otherwise silently drop consumed queue entries (the detached
    drain shell deletes its list file unconditionally);
  * correctness: a pruning walk's prune set is keyed on the files IT visited
    (``visited_uuids``; hash-skipped-but-current files are still marked
    visited — see ``analyze_code_graph._note_written_uuid``), and a scoped
    incremental only ever upserts rows for CURRENT files, which any full walk
    of the same project also visits — so its rows survive the prune and its
    writes are deterministic-UUID upserts the full walk would make anyway.
    The remaining overlap cost is one small batch's embed load, not the
    twin-full-walk GPU storm this claim exists to stop.
    Residual window (accepted by design, review R1-3): a file CREATED after
    the pruning walk's tree enumeration is not in ITS ``visited_uuids``; if a
    concurrent drain batch upserts that file's rows in the gap, the walk's
    end-of-run prune deletes them as stale, and nothing re-queues the file
    until its next edit — one file's rows, self-healing, the pre-existing
    semantics of ``--prune-stale`` (the alternative, claiming scoped runs,
    would silently drop drain batches whose list file is deleted
    unconditionally).

Cross-OS: the lock is ``vco_lib.atomic.exclusive_file_lock``, which locks for
real on POSIX (``flock``) and Windows (``msvcrt.locking``, v0.2.103). A
kill of the ``-m`` wrapper alone never leaves the analyzer walking without
the claim: POSIX children inherit the flock descriptor, Windows children die
with the wrapper through a kill-on-close Job Object (:func:`_run_child`).
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import signal
import subprocess  # noqa: S404 — the child argv is the caller's own analyzer
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

#: Exit code a refused (would-be-twin) walk exits with — EX_TEMPFAIL: a
#: transient condition, the caller may retry later. Distinct from every
#: analyzer exit code (0 ok, 1 generic, 2 schema collision, 3 no files,
#: 4 insert errors, 5 deleted-file prune failures).
#: MUST MATCH vct_launcher_core::process::WALK_ALREADY_RUNNING_EXIT_CODE.
WALK_ALREADY_RUNNING_RC = 75

#: Analyzer argv flags that make a run a SCOPED incremental (a named file
#: set), not a tree walk — the one shape exempt from the claim (see the
#: module docstring for the evidence).
_SCOPED_FLAGS = ("--only-file", "--only-files-from")

#: Analyzer flags that consume the NEXT argv token as their value — skipped
#: when scanning for the positional repo path so a value is never mistaken
#: for it. MUST MATCH templates/scripts/analyze_code_graph.py's argparse
#: value flags (store/action append options).
_VALUE_FLAGS = frozenset({
    "--project", "-p", "--language", "-l", "--extra-path", "--primary-root",
    "--since-commit", "--only-file", "--only-files-from", "--canonical-source",
    "--deferral-root",
})


class WalkAlreadyRunning(Exception):
    """Another process holds this project's walk claim (flock timeout at 0)."""


def claim_key(project_name: str) -> str:
    """Project name → the single-flight claim key (the lock filename stem).

    ONE derivation for both per-project claims (this walk claim and the
    resync DRIVER claim, ``codegraph_resync._driver_claim_lock_path``, which
    re-points here): the canonical collection prefix via the never-raising
    ``codegraph_naming.sanitize_collection_prefix``; the legacy regex form
    when that cannot answer (broken install / unusable name). Every spawner
    derives the SAME key for the SAME project — the launcher passes the
    canonical prefix as ``--project`` and the driver a display name; both
    sanitize to the same key.
    """
    key = ""
    try:
        from vco_lib.codegraph_naming import sanitize_collection_prefix

        key = sanitize_collection_prefix(project_name or "")
    except Exception:  # noqa: BLE001 — broken install: fall back, never raise
        key = ""
    if not key:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", (project_name or "").strip()) or "project"
    return key


def walk_claim_lock_path(project_name: str) -> Path:
    """The per-project walk claim file, beside the resync-driver claims under
    the VCT state dir. A pure lock token — contents irrelevant, safe to leave
    on disk; the exclusion is the flock on it, not the file's existence.
    """
    from vco_lib.paths import vct_root_dir

    return (
        vct_root_dir() / "state" / "codegraph-walk-claims" / f"{claim_key(project_name)}.lock"
    )


@contextlib.contextmanager
def _held_walk_claim(project_name: str) -> Iterator[Tuple[Path, Optional[int]]]:
    """:func:`walk_claim`, also yielding the lock's descriptor when it is a
    POSIX ``flock`` a child can share by inheriting it (else ``None``)."""
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    lock_path = walk_claim_lock_path(project_name)
    try:
        with exclusive_file_lock(lock_path, timeout_s=0) as fd:
            yield lock_path, fd
    except LockTimeout as exc:
        raise WalkAlreadyRunning(
            f"{project_name}: another code-graph walk is already running "
            f"(claim: {lock_path})"
        ) from exc


@contextlib.contextmanager
def walk_claim(project_name: str) -> Iterator[Path]:
    """Hold this project's walk claim for the block, NON-blocking.

    Raises :class:`WalkAlreadyRunning` immediately when another process holds
    it. NEVER blocks waiting and never times a walk out — walks legitimately
    run for hours and the claim outlives exactly as long as its holder
    (flock semantics). Yields the lock path (for log lines).
    """
    with _held_walk_claim(project_name) as (lock_path, _fd):
        yield lock_path


#: Signals the ``-m`` hop passes on to its analyzer child instead of dying
#: alone (v0.2.103 final review L-5): a terminate aimed at the wrapper stops
#: the walk it fronts. SIGINT is not here — Ctrl-C reaches the whole
#: foreground group, the child included.
_FORWARDED_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
)


# --- Windows: the analyzer dies with the wrapper (v0.2.103 L-5 / R17-2) -----
#
# POSIX keeps the claim alive under a wrapper-only kill by letting the child
# INHERIT the flock descriptor. Windows has no equivalent: the
# ``msvcrt.locking`` byte-range lock belongs to the wrapper PROCESS, and
# ``TerminateProcess`` (Stop-Process, Task Manager, the launcher's kill)
# cannot be caught, so a wrapper-only kill freed the claim while the analyzer
# walked on — admitting a twin. The Windows mechanism is the inverse one: the
# analyzer is put in a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
# whose ONLY handle lives in the wrapper. When the wrapper dies (any way —
# the kernel closes its handles), the job closes and the kernel terminates
# every process in it: the analyzer and anything it spawned after the
# assignment. Claim and walk end together.
#
# The handle is created non-inheritable (NULL security attributes), so the
# child never holds a copy that would keep the job open. Nested jobs are
# supported since Windows 8, so a wrapper that itself runs inside a job (a
# launcher, a CI runner) can still assign its child. Residual window: a
# process the analyzer spawns in the microseconds between ``CreateProcess``
# and the assignment is not in the job (the analyzer is a Python script
# whose first spawns happen long after interpreter start-up). If any step
# fails, the walk still runs and one stderr line says the wrapper-kill
# protection is off for it — the walk itself is never refused for this.

#: JOBOBJECTINFOCLASS value for JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _load_kernel32():  # pragma: no cover — Windows only
    """kernel32 with explicit signatures (HANDLE is pointer-sized: a default
    ``int`` return would truncate it on 64-bit)."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _last_error() -> int:
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


def _bind_child_to_kill_on_close_job(pid: int, kernel32=None) -> Optional[int]:
    """Put process ``pid`` in a fresh KILL_ON_JOB_CLOSE job; return the job
    handle (held by THIS process only), or ``None`` after one stderr line when
    any step fails. ``kernel32`` is injectable for tests."""
    k32 = kernel32 if kernel32 is not None else _load_kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        _warn_job(f"CreateJobObjectW failed (error {_last_error()})")
        return None
    info = _JobExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info)):
        _warn_job(f"SetInformationJobObject failed (error {_last_error()})")
        k32.CloseHandle(job)
        return None
    proc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not proc:
        _warn_job(f"OpenProcess({pid}) failed (error {_last_error()})")
        k32.CloseHandle(job)
        return None
    try:
        if not k32.AssignProcessToJobObject(job, proc):
            _warn_job(f"AssignProcessToJobObject failed (error {_last_error()})")
            k32.CloseHandle(job)
            return None
    finally:
        k32.CloseHandle(proc)
    return job


def _warn_job(detail: str) -> None:
    print(
        f"[codegraph-walk] WARNING: could not tie the analyzer to this "
        f"wrapper's life ({detail}) — killing only the wrapper would leave "
        f"the walk running without its claim",
        file=sys.stderr, flush=True,
    )


def _tie_child_to_wrapper(pid: int) -> Optional[int]:
    """Platform branch: on Windows bind ``pid`` to a kill-on-close job (the
    returned handle must stay open until the child has ended); a no-op
    returning ``None`` on POSIX, where the inherited flock descriptor and
    signal forwarding already cover a wrapper kill."""
    if sys.platform != "win32":
        return None
    try:
        return _bind_child_to_kill_on_close_job(pid)
    except Exception as exc:  # noqa: BLE001 — never fail the walk for this
        _warn_job(f"{type(exc).__name__}: {exc}")
        return None


def _release_job(job: Optional[int], kernel32=None) -> None:
    """Close the job handle once the child has ended (kills any straggler the
    analyzer left in the job — the walk is over either way)."""
    if job is None:
        return
    try:
        (kernel32 if kernel32 is not None else _load_kernel32()).CloseHandle(job)
    except Exception:  # noqa: BLE001 — process exit closes it anyway
        pass


def _run_child(child: List[str], *, cwd: "str | Path | None",
               claim_fd: Optional[int] = None) -> int:
    """Run the analyzer child to completion and return its exit code.

    * A forwarded signal (:data:`_FORWARDED_SIGNALS`) delivered to this
      wrapper is sent on to the child, and the wrapper keeps waiting for it
      — the claim is released only once the walk has really ended.
    * ``claim_fd`` (POSIX ``flock``): the child INHERITS the claim's
      descriptor, so the claim is held for the child's whole life even if
      the wrapper is killed outright (SIGKILL cannot be forwarded) — a
      wrapper-only kill can no longer free the claim under a running walk
      and admit a twin. On Windows the byte-range lock is per process and
      ``TerminateProcess`` cannot be caught, so the child is instead bound to
      a kill-on-close Job Object held by the wrapper
      (:func:`_tie_child_to_wrapper`): the wrapper's death ends the analyzer.
    * Any other exception while waiting (KeyboardInterrupt) ends the child
      too, as ``subprocess.run`` does.
    """
    state: dict = {"proc": None, "pending": []}

    def _forward(signum: int, _frame: object) -> None:
        proc = state["proc"]
        if proc is None:
            state["pending"].append(signum)
            return
        try:
            proc.send_signal(signum)
        except OSError:
            pass  # already gone

    previous: dict = {}
    for sig in _FORWARDED_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, _forward)
        except (OSError, ValueError):  # not the main thread / not settable
            pass
    try:
        pass_fds = (claim_fd,) if claim_fd is not None else ()
        proc = subprocess.Popen(child, cwd=cwd, pass_fds=pass_fds)  # noqa: S603
        state["proc"] = proc
        job = _tie_child_to_wrapper(proc.pid)
        for signum in state["pending"]:
            _forward(signum, None)
        try:
            rc = proc.wait()
            # A child killed by a signal reports -signum; a process exit code
            # is 0-255, so report it the way shells do (128 + signum) instead
            # of letting sys.exit wrap -15 to 241 (review R17-3).
            return 128 - rc if rc < 0 else rc
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            _release_job(job)
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError, TypeError):
                pass


def argv_is_scoped(args: Sequence[str]) -> bool:
    """True when the analyzer argv is a SCOPED incremental (``--only-file`` /
    ``--only-files-from``) — exempt from the claim (module docstring)."""
    return any(
        a in _SCOPED_FLAGS or any(a.startswith(f + "=") for f in _SCOPED_FLAGS)
        for a in args
    )


def project_from_argv(args: Sequence[str]) -> Optional[str]:
    """Derive the claim key's project from an analyzer argv: the
    ``--project``/``-p`` value, else the repo directory name — the SAME
    default the analyzer itself applies (``--project`` help: "default: repo
    directory name"). ``None`` when neither exists (the caller then runs
    WITHOUT the claim and says so — fail-open with a log line, never a crash
    and never a refusal that could block a legitimate walk).
    """
    project: Optional[str] = None
    positional: Optional[str] = None
    items = [str(a) for a in args]
    i = 0
    while i < len(items):
        arg = items[i]
        if arg in ("--project", "-p"):
            if i + 1 < len(items):
                project = items[i + 1]
                i += 2
                continue
        elif arg.startswith("--project="):
            project = arg.split("=", 1)[1]
        elif arg in _VALUE_FLAGS:
            i += 2  # skip this flag's value — it is not the repo path
            continue
        elif any(arg.startswith(f + "=") for f in _VALUE_FLAGS if f.startswith("--")):
            pass  # --flag=value carries no separate token
        elif not arg.startswith("-") and positional is None:
            positional = arg
        i += 1
    if project and project.strip():
        return project.strip()
    if positional:
        # resolve(): the analyzer walks what the path MEANS, and a relative
        # repo path is rooted at the caller's cwd (inherited here).
        name = Path(positional).resolve().name
        return name or None
    return None


def already_running_message(project_name: str) -> str:
    """The ONE refusal sentence (log + stderr + the launcher's toast text
    derives from the same fact: exit code :data:`WALK_ALREADY_RUNNING_RC`)."""
    return (
        f"[codegraph-walk] {project_name}: another code-graph walk is already "
        f"running for this project — not starting a twin "
        f"(claim: {walk_claim_lock_path(project_name)})"
    )


def run_claimed(script: "str | Path", args: Sequence[str], *, cwd: "str | Path | None" = None) -> int:
    """Spawn ``[sys.executable, script, *args]`` under the walk claim.

    ``sys.executable`` is the analyzer-capable interpreter by construction:
    every caller of this CLI resolved exactly such an interpreter to run
    ``-m vco_lib.codegraph_walk`` (the shell wrappers' venv ladder probes
    ``import weaviate, weaviate_mcp, vco_lib``; the launcher uses the RT-4
    vco_lib ladder). The child's stdio is INHERITED, so the analyzer's
    ``--json-progress`` stream reaches the spawning launcher untouched.

    Returns the analyzer's exit code; :data:`WALK_ALREADY_RUNNING_RC` when
    the claim is held (nothing spawned); scoped incrementals run without the
    claim. The claim is held for the ANALYZER's life, not just this
    wrapper's: a terminate signal to the wrapper is forwarded to the child,
    on POSIX the child inherits the claim itself, and on Windows the child is
    bound to a kill-on-close job held by the wrapper (:func:`_run_child`).
    """
    argv = [str(a) for a in args]
    child = [sys.executable, str(script), *argv]
    if argv_is_scoped(argv):
        return _run_child(child, cwd=cwd)
    project = project_from_argv(argv)
    if not project:
        print(
            "[codegraph-walk] WARNING: no --project and no repo path in the "
            "analyzer argv — running WITHOUT the single-flight claim",
            file=sys.stderr, flush=True,
        )
        return _run_child(child, cwd=cwd)
    try:
        with _held_walk_claim(project) as (_lock_path, claim_fd):
            return _run_child(child, cwd=cwd, claim_fd=claim_fd)
    except WalkAlreadyRunning:
        print(already_running_message(project), file=sys.stderr, flush=True)
        return WALK_ALREADY_RUNNING_RC


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.codegraph_walk <analyze_code_graph.py> [args...]``.

    The claim-taking wrapper every shell/launcher spawn of a full or pruning
    walk routes through (module docstring). Stdio is inherited; the exit code
    is the analyzer's, or :data:`WALK_ALREADY_RUNNING_RC` on refusal.

    The wrapper's own lines (the refusal above all) usually reach the user
    through a PowerShell/shell wrapper's pipe on Windows, so its streams are
    made console-safe first (:func:`vco_lib.console_text.harden_stdio`).
    """
    from vco_lib.console_text import harden_stdio

    harden_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: python -m vco_lib.codegraph_walk "
            "<analyze_code_graph.py> [analyzer args...]",
            file=sys.stderr,
        )
        return 2
    script, rest = args[0], args[1:]
    return run_claimed(script, rest)


if __name__ == "__main__":
    sys.exit(main())
