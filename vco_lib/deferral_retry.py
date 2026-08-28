# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Retry dispatcher for ``auto_retryable`` deferral conditions (v0.2.91 WP-H).

The gap this closes (report 6 §B.2 / §C.3, report 2 Topic 3): a condition like
``kg_sync_no_embedding_backend`` is TRANSIENT — the embedding backend was down
for the seconds the seed ran — but VCO treated it as terminal. It wrote the
entry, printed manual commands, and stopped. Nothing ever re-ran the seed when
the backend came back, so two field projects sat with an empty knowledge graph
for weeks while the ledger entry told their Claude to fix it by hand.

Shape: **registry data + ONE dispatcher.**
------------------------------------------
``vco_lib/deferral_conditions.toml`` declares, on each ``auto_retryable`` row::

    retry_action = "retry:py:<handler>"

and this module owns the handler table. Adding a retryable condition is a
registry line plus (if it needs a new one) a handler — never a bespoke hook
wired into whatever component happens to notice.

Why retrying is allowed here at all
-----------------------------------
Decision #4 draws the auto-fix boundary at "environment-level fixes auto-apply;
anything touching running binaries is surface-only". A retry sits on the
allowed side for a reason worth stating explicitly, because it is the one
side-effecting thing this release added to an otherwise read-only pass:

* A retry re-runs **owed WORK the user already consented to by installing** —
  the KG seed that install.py would have run if the backend had been up. It is
  not a repair of the user's machine, not a mutation of their configuration,
  and not a decision VCO is making on their behalf. The alternative to
  retrying is not "safety", it is "the work silently never happens".
* Every handler is **idempotent by construction**: the KG sync is content-hash
  gated (unchanged nodes are skipped), the analyzer walk is revision-gated. A
  spurious retry costs seconds and changes nothing.
* Every handler is **precondition-gated on POSITIVE evidence** for the backend
  ITS OWN work needs — text for the KG seed, code for the analyzer walk. No
  backend, no retry, no resolve. "Could not tell" is treated as "no"
  (:data:`SKIPPED`).
* Attempts are **capped** (:data:`MAX_ATTEMPTS`) per condition per folder and
  every attempt is recorded BEFORE the handler runs, so a handler that crashes
  still consumes its attempt and a permanently-failing retry degrades to the
  pre-v0.2.91 behaviour (an entry the user resolves by hand) instead of
  looping.

Anything doubtful stays behind the doctor's consent path — this dispatcher is
NOT a general "run commands from the ledger" engine, and must never become one:
handlers are named in code, and a condition can only ever trigger the handler
its registry row declares.

Clearing — the child's own narrow clear is the ONLY proof
---------------------------------------------------------
A zero exit is NOT evidence the work happened. Both tenants exit 0 on their
SKIP paths after re-emitting the very entry we are retrying:
``analyze_code_graph.py`` returns 0 both when no embedding backend answered and
when the code backend specifically is down; ``sync_knowledge_graph.py`` does
the same. Resolving on ``rc == 0`` therefore deleted the entry the child had
just re-written — and, because ``resolve_conditions`` also TOMBSTONES the id
for that run, a within-cycle re-emit could not put it back. The ledger then
said "resolved" and ``auto-resolutions.jsonl`` said "completed" for a seed that
never ran.

So the dispatcher asks the ledger instead: after the child exits, RE-READ the
report. The condition is resolved only when the CHILD's own paired clear
removed it — ``sync_knowledge_graph.py::_clear_sync_deferral_no_backend`` at
the end of a fully-successful tree sync, and ``analyze_code_graph.py``'s
``clear_backend_deferrals`` op after a completed walk. Still present, or an
unreadable ledger, ⇒ :data:`INCONCLUSIVE`: no resolve, no tombstone, no
success row.

Both handlers PIN the child's ledger root to ``folder`` —
``KG_SYNC_PROJECT_ROOT`` for the seed, ``--deferral-root`` for the analyzer
(wave-3 MAJOR-A) — because re-reading a ledger the child never wrote is not
evidence of anything.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

#: Per (folder, condition) attempt ceiling. Beyond it the entry stays for the
#: user — a retry that failed three times is not transient.
MAX_ATTEMPTS = 3

#: Attempt trail. Sits beside auto-resolutions.jsonl in the user-owned,
#: git-ignored logs dir.
ATTEMPTS_FILENAME = "deferral-retries.jsonl"

#: Single-instance lock for the driver, per folder. Two session-starts a
#: second apart must not run two KG seeds over the same tree.
PIDFILE_NAME = "deferral-retry.pid"

#: A held lock older than this is treated as abandoned even when its pid still
#: looks alive (pid reuse, or a child wedged forever). A KG seed over a large
#: tree is minutes; six hours cannot be a live one.
PIDFILE_STALE_SECONDS = 6 * 3600

#: Result states.
STARTED = "started"       # attempt recorded BEFORE the handler ran
RETRIED = "retried"       # handler ran AND the child cleared its condition
FAILED = "failed"         # handler ran and did not succeed
#: Handler exited 0 but its condition is still in the ledger (or the ledger
#: could not be re-read). The child's SKIP paths exit 0 too, so this is the
#: honest verdict: something ran, nothing is proven, resolve nothing.
INCONCLUSIVE = "inconclusive"
SKIPPED = "skipped"       # precondition absent (backend down, cap reached…)

#: Which backend a handler's owed work actually needs. The gate asks THIS
#: question — "is any backend up" was the wrong one: on a machine whose text
#: backend answers and whose code backend does not, the either-backend probe
#: dispatched the analyzer retry into the exact state that emitted
#: ``code_graph_code_backend_unreachable``, burning an attempt on a provably
#: hopeless run.
TEXT_BACKEND = "text"
CODE_BACKEND = "code"


#: Trail lines go to stdout, which ``--json`` reserves for its result list.
#: One module flag rather than threading a "quiet" parameter through every
#: helper — the trail is diagnostics, not data flow.
_TRAIL_ENABLED = True


def trail(message: str) -> None:
    """Write one dispatcher trail line to stdout. Never raises.

    v0.2.91 dogfood fix. The driver runs DETACHED with its stdio redirected to
    ``<vct_root_dir>/logs/deferral-retry-<stamp>.log``, and it used to print
    exactly one line per RESULT — so a pass that dispatched nothing produced a
    ZERO-BYTE file. Fourteen of them accumulated on the maintainer's machine,
    and every diagnosis they could support was identical: "something ran, or
    maybe nothing did". A log that cannot distinguish "started and found
    nothing owed" from "died at import" from "never started" is not a log.

    So every taken path writes: driver start, the ledger read, per-condition
    gate verdicts (handler, attempt count, backend probe), the child argv, the
    child's exit code, the ledger RE-READ verdict, and a closing summary.
    """
    if not _TRAIL_ENABLED:
        return
    try:
        print(f"[retry] {message}", flush=True)
    except Exception:  # noqa: BLE001 — a broken stdout must not break a retry
        pass


@dataclass(frozen=True)
class RetryResult:
    condition_id: str
    status: str
    detail: str


@dataclass(frozen=True)
class RetryContext:
    """Everything a handler may use. Seams exist so tests inject a fake world."""

    folder: Path
    condition_id: str
    #: (folder, kind) -> True when the backend serving ``kind`` is reachable,
    #: False when provably not, None when it could not be determined.
    #: Tri-state on purpose: a probe that could not run must not read as
    #: "backend is up". ``kind`` is :data:`TEXT_BACKEND` or
    #: :data:`CODE_BACKEND`.
    backend_probe: Optional[Callable[[Path, str], Optional[bool]]] = None
    #: (argv, cwd) -> returncode. Injected so tests never spawn anything.
    runner: Optional[Callable[[Sequence[str], Path], int]] = None
    #: Interpreter for child processes (defaults to the running one, which is
    #: the VCO venv python on every path that reaches here).
    python: str = ""

    def backend_available(self, kind: str) -> Optional[bool]:
        probe = self.backend_probe or default_backend_probe
        try:
            return probe(self.folder, kind)
        except Exception:  # noqa: BLE001 — a failed probe is "unknown", not "up"
            return None

    def run(self, argv: Sequence[str]) -> int:
        """Run a handler's child, trailing the argv and the exit code.

        The ONE funnel every handler's child goes through, so the trail cannot
        miss a spawn a future handler adds (and so the injected test runner is
        covered by the same lines the production one is).
        """
        runner = self.runner or default_runner
        printable = " ".join(str(a) for a in argv)
        trail(f"{self.condition_id}: spawning child: {printable}")
        rc = runner(argv, self.folder)
        trail(f"{self.condition_id}: child exited {rc}")
        return rc

    def interpreter(self) -> str:
        return self.python or sys.executable or "python3"


# ---------------------------------------------------------------------------
# Default seams (production behaviour)
# ---------------------------------------------------------------------------


def default_backend_probe(folder: Path, kind: str) -> Optional[bool]:
    """Is the ``kind`` embedding backend reachable for ``folder``?

    Constructing an :class:`~vco_lib.embedding_service.EmbeddingService` and
    then asking it ``text_backend_ready()`` / ``code_backend_ready()`` IS the
    reachability check the EMITTERS performed. Reusing their exact predicate
    means the retry gate and the emitter can never disagree about what "the
    backend is down" means:

    * ``kg_sync_no_embedding_backend`` — the seed died constructing the
      service, so any successful construction plus a ready TEXT backend is the
      condition lifting.
    * ``code_graph_no_embedding_backend`` — same construction failure.
    * ``code_graph_code_backend_unreachable`` — the analyzer constructed the
      service FINE and then found ``code_backend_ready()`` False. Asking the
      either-backend question here (v0.2.91 wave-3 MAJOR-1b) passed in exactly
      the state that emitted the entry, so the retry re-ran the analyzer into
      its own skip path every time.

    Returns True / False / None (probe itself failed).
    """
    try:
        from vco_lib.embedding_service import (
            EmbeddingService,
            NoEmbeddingBackendError,
        )
    except Exception:  # noqa: BLE001 — import problem ⇒ unknown
        return None
    try:
        service = EmbeddingService.for_project(folder)
    except NoEmbeddingBackendError:
        return False
    except Exception:  # noqa: BLE001 — anything else ⇒ unknown, do not retry
        return None
    try:
        if kind == CODE_BACKEND:
            return bool(service.code_backend_ready())
        return bool(service.text_backend_ready())
    except Exception:  # noqa: BLE001 — a readiness probe that blew up ⇒ unknown
        return None
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


def default_runner(argv: Sequence[str], cwd: Path) -> int:
    """Run a handler's child process to completion, inheriting stdio.

    The dispatcher itself is ALREADY the detached child on the session-start
    path (see :func:`spawn_detached`), so handlers block here rather than
    detaching again: a second detach would leave nobody to observe the exit
    code, and the resolve must only happen on a proven success.
    """
    proc = subprocess.run(  # noqa: S603 — argv is ours, never shell
        list(argv), cwd=str(cwd), check=False,
    )
    return proc.returncode


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _sync_script(folder: Path) -> Optional[Path]:
    """The KG sync script for ``folder`` — bundled copy first, template second.

    Same resolution order as ``codegraph_resync._resolve_analyzer``: user
    projects run their ``.claude/scripts/`` copy; the orchestrator root has
    both and the bundled copy is the one install.py invokes.
    """
    for rel in (
        Path(".claude") / "scripts" / "sync_knowledge_graph.py",
        Path("templates") / "scripts" / "sync_knowledge_graph.py",
    ):
        candidate = folder / rel
        if candidate.is_file():
            return candidate
    return None


def _analyzer_script(folder: Path) -> Optional[Path]:
    for rel in (
        Path(".claude") / "scripts" / "analyze_code_graph.py",
        Path("templates") / "scripts" / "analyze_code_graph.py",
    ):
        candidate = folder / rel
        if candidate.is_file():
            return candidate
    return None


def retry_kg_seed(ctx: RetryContext) -> RetryResult:
    """Re-run the owed KG seed for ``folder``.

    Reuses the SHIPPED sync path (``sync_knowledge_graph.py --all``) rather
    than a second seeding implementation: that script is content-hash gated
    (unchanged nodes skip the embed), so a retry over an already-seeded tree is
    cheap, and its success path carries WP-B's own narrow clear.

    :data:`RETRIED` here means only "the child exited 0" — the script's SKIP
    path exits 0 as well. :func:`dispatch` downgrades it to
    :data:`INCONCLUSIVE` unless the child's own clear removed the condition.
    """
    script = _sync_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no sync_knowledge_graph.py found")
    env_root = str(ctx.folder)
    prev = os.environ.get("KG_SYNC_PROJECT_ROOT")
    os.environ["KG_SYNC_PROJECT_ROOT"] = env_root
    try:
        rc = ctx.run([ctx.interpreter(), str(script), "--all"])
    finally:
        if prev is None:
            os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
        else:
            os.environ["KG_SYNC_PROJECT_ROOT"] = prev
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED, "kg sync --all completed")
    return RetryResult(ctx.condition_id, FAILED, f"kg sync --all exited {rc}")


def retry_code_graph_walk(ctx: RetryContext) -> RetryResult:
    """Re-run the owed code-graph analysis for ``folder``.

    ``--incremental`` (revision-gated) so an already-converged graph costs a
    hash pass, not a full re-embed. NEVER ``--force-recreate`` / ``--prune-stale``:
    both are destructive and a retry has no consent for either.

    ``--deferral-root <folder>`` is the analyzer's counterpart to the
    ``KG_SYNC_PROJECT_ROOT`` pin :func:`retry_kg_seed` carries (wave-3
    MAJOR-A). Without it the child resolves its ledger from the
    ``VCT_ORCHESTRATOR_ROOT`` it INHERITS from this session, so on a
    launcher-managed user project it emitted and cleared in the orchestrator
    clone while :func:`condition_cleared` re-read the project — INCONCLUSIVE
    on every attempt until the cap burned, plus a cross-ledger resolve made on
    the project's evidence. Pinning it to ``folder`` makes emit-root ==
    clear-root == the root we re-read.

    :data:`RETRIED` here means only "the child exited 0" — the analyzer's two
    backend SKIP paths exit 0 as well. :func:`dispatch` downgrades it to
    :data:`INCONCLUSIVE` unless the analyzer's own clear removed the condition.
    """
    script = _analyzer_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no analyze_code_graph.py found")
    argv = [ctx.interpreter(), str(script), str(ctx.folder), "--incremental",
            "--deferral-root", str(ctx.folder)]
    project = _project_name(ctx.folder)
    if project:
        argv += ["--project", project]
    rc = ctx.run(argv)
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED, "code-graph walk completed")
    return RetryResult(ctx.condition_id, FAILED, f"code-graph walk exited {rc}")


def retry_codegraph_resync(ctx: RetryContext) -> RetryResult:
    """Re-run the owed code-graph RESYNC for ``folder`` (``codegraph_embed_resync_pending``).

    v0.2.91 dogfood fix. This condition shipped classed ``auto_retryable`` with
    NO ``retry_action``, so ``handler_name_for`` returned None, the cid never
    entered ``owed_condition_ids``, and the dispatcher could not select it —
    the ledger told the user "VCO retries this itself" while nothing did. The
    registry's own honesty rule (classification must never be mistakable for
    implementation) says either the retry gets wired or the tier is wrong; the
    machinery to converge this condition already exists, so it gets wired.

    The child is the R-7 DRIVER (``vco_lib.codegraph_resync --run-resync``),
    NOT a bare analyzer walk. That distinction is the whole point:

    * the driver runs the analyzer, then RE-PROBES the stale-row count, and
      resolves the ledger entry only on a positive zero — the paired clear this
      dispatcher gates on. A bare ``analyze_code_graph.py`` invocation (what
      the entry's own ``command_to_apply`` prints for a human) performs no such
      clear, so wiring THAT would have been INCONCLUSIVE on every attempt and
      would have burned the durable cap for nothing — the "a retry tenant
      without a narrow clear is unresolvable by construction" trap.
    * the driver also owns the WP-C entity reconcile, which is what actually
      removes the entity-orphan rows that keep the owed count off zero.

    Runs blocking (this process is already the detached child) and never
    forwards ``--force-recreate`` / ``--prune-stale``: a retry has consent for
    re-embedding owed rows, not for dropping anything.
    """
    project = _project_name(ctx.folder)
    if not project:
        return RetryResult(
            ctx.condition_id, SKIPPED,
            "cannot resolve the project name — the resync targets collections by prefix",
        )
    try:
        from vco_lib.codegraph_resync import _resolve_analyzer
        from vco_lib.paths import looks_like_orchestrator_root
    except Exception as exc:  # noqa: BLE001 — broken install ⇒ skip, never crash
        return RetryResult(
            ctx.condition_id, SKIPPED, f"codegraph_resync unavailable: {exc}",
        )
    analyzer = _resolve_analyzer(ctx.folder)
    if analyzer is None:
        return RetryResult(
            ctx.condition_id, SKIPPED, "no analyze_code_graph.py found",
        )
    argv = [
        ctx.interpreter(), "-m", "vco_lib.codegraph_resync", "--run-resync",
        "--project", project,
        "--repo-root", str(ctx.folder),
        "--analyzer", str(analyzer),
    ]
    # Same `.claude` decision the spawn path makes, from the SAME helper — so
    # the driver's post-walk verify probe classifies `.claude/**` rows exactly
    # as the owed gate that emitted the entry did. A mismatch here reads as
    # "never converges" forever.
    if looks_like_orchestrator_root(ctx.folder):
        argv.append("--index-dot-claude")
    rc = ctx.run(argv)
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED, "code-graph resync driver completed")
    return RetryResult(ctx.condition_id, FAILED, f"code-graph resync driver exited {rc}")


def _project_name(folder: Path) -> Optional[str]:
    """Project name for the analyzer, resolved by the ONE existing helper."""
    try:
        from vco_lib.paths import resolve_project_name

        return resolve_project_name(folder)
    except Exception:  # noqa: BLE001 — analyzer defaults to the dir name
        return None


@dataclass(frozen=True)
class Handler:
    """One retry handler plus the backend ITS work needs.

    Pairing the two in one row is what stops the gate and the handler drifting
    apart: a handler cannot be added without declaring which backend has to be
    up for its work to be possible.
    """

    run: Callable[[RetryContext], RetryResult]
    backend: str


#: handler name → handler. The registry references these as
#: ``retry_action = "retry:py:<name>"``; a row naming a handler that is not
#: here fails ``tests/test_v0291_retry_dispatch.py``.
HANDLERS: dict[str, Handler] = {
    "kg_seed": Handler(retry_kg_seed, TEXT_BACKEND),
    "code_graph_walk": Handler(retry_code_graph_walk, CODE_BACKEND),
    "codegraph_resync": Handler(retry_codegraph_resync, CODE_BACKEND),
}


# ---------------------------------------------------------------------------
# Attempt ledger
# ---------------------------------------------------------------------------


def attempts_path(folder: Path) -> Path:
    return Path(folder) / ".claude" / "logs" / ATTEMPTS_FILENAME


def attempt_count(folder: Path, condition_id: str) -> int:
    """How many times a handler for this condition has been STARTED here.

    Counts :data:`STARTED` rows only — exactly one per dispatched handler
    invocation, written BEFORE the handler runs. Counting outcome rows instead
    (v0.2.91 pre-fix) meant a handler that CRASHED left no row at all, so the
    cap could never engage on the one failure mode it exists for: a retry that
    dies the same way every session, forever.

    An unreadable/absent trail counts as zero attempts — the cap protects
    against looping, and a missing log is not evidence that the cap was
    reached.
    """
    path = attempts_path(folder)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    n = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("condition_id") == condition_id and row.get("status") == STARTED:
            n += 1
    return n


def record_attempt(folder: Path, result: RetryResult) -> None:
    """Append one attempt row. Best-effort — observability never gates work."""
    path = attempts_path(folder)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "condition_id": result.condition_id,
        "status": result.status,
        "detail": result.detail,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Registry glue
# ---------------------------------------------------------------------------


def handler_name_for(condition_id: str) -> Optional[str]:
    """The handler the registry declares for ``condition_id``, or ``None``.

    ``None`` for any condition that is not ``auto_retryable``, declares no
    ``retry_action``, or is unregistered — all three mean "this dispatcher has
    no business touching it".

    ONE home for the lookup (v0.2.91 wave-3 NIT): the registry's own
    ``retry_handler_for`` accessor answers it — including the class gate, which
    the loader enforces by REFUSING a ``retry_action`` on any tier other than
    ``auto_retryable``. Re-deriving it here from ``condition()`` was a second
    copy of a rule the registry already owns, free to drift from it.
    """
    try:
        from vco_lib.deferral_registry import retry_handler_for

        return retry_handler_for(condition_id)
    except Exception:  # noqa: BLE001 — a registry problem ⇒ retry nothing
        return None


def retryable_condition_ids(condition_ids: Iterable[str]) -> list[str]:
    """Subset of ``condition_ids`` this dispatcher can act on (order kept)."""
    out: list[str] = []
    for cid in condition_ids:
        if cid and handler_name_for(cid) and cid not in out:
            out.append(cid)
    return out


def owed_condition_ids(folder: Path) -> list[str]:
    """Retryable cids sitting in ``folder``'s ledger right now.

    Reads the machine-readable sidecar through the canonical reader, so the
    session-start hook and the doctor ask the same question of the same bytes.
    """
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(folder))
    except Exception:  # noqa: BLE001 — unreadable ledger ⇒ nothing owed
        return []
    if not report:
        return []
    return retryable_condition_ids(
        getattr(e, "condition_id", "") for e in report.entries
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def condition_cleared(folder: Path, condition_id: str) -> Optional[bool]:
    """Tri-state: did the child's own paired clear remove ``condition_id``?

    ``True`` the ledger was read and the condition is gone · ``False`` read and
    still present · ``None`` the ledger could not be read.

    This is the ground truth :func:`dispatch` uses instead of the child's exit
    code, because every tenant exits 0 on its skip path after RE-EMITTING the
    entry. ``None`` is deliberately not ``True``: an unreadable ledger is not
    evidence that anything was cleared.
    """
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(folder))
    except Exception:  # noqa: BLE001 — unreadable ⇒ unknown, never "cleared"
        return None
    if not report:
        # No report on disk at all: every entry it held is gone, including
        # ours. That IS the cleared state (it is what a resolve of the last
        # entry leaves behind — the emitter deletes the files).
        return True
    return not any(
        getattr(e, "condition_id", "") == condition_id for e in report.entries
    )


def pidfile_path(folder: Path) -> Path:
    return Path(folder) / ".claude" / "state" / PIDFILE_NAME


def _read_pidfile(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw.splitlines()[0])
    except (ValueError, IndexError):
        return None


def _lock_is_held(path: Path) -> bool:
    """Is another driver provably running for this folder?

    Stale-tolerant in BOTH directions:

    * a pidfile whose pid is provably gone is stale — take over;
    * a pidfile older than :data:`PIDFILE_STALE_SECONDS` is abandoned even if
      its pid looks alive (pid reuse, or a wedged child), because no legitimate
      retry runs that long — take over.

    Otherwise (alive pid, or a liveness probe that could not tell) the lock is
    treated as HELD: declining to start a second seed is the conservative
    branch, and the age bound above keeps that from becoming permanent.
    """
    pid = _read_pidfile(path)
    if pid is None:
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        age = 0.0
    if age > PIDFILE_STALE_SECONDS:
        return False
    if pid == os.getpid():
        return False
    try:
        from vco_lib.deferral_probes import pid_is_alive

        return pid_is_alive(pid)
    except Exception:  # noqa: BLE001 — cannot tell ⇒ assume held
        return True


def _acquire_lock(folder: Path) -> Optional[Path]:
    """Claim the per-folder driver lock, or ``None`` when it is held.

    Deliberately NOT an atomic ``O_EXCL`` create: the loser of a true race must
    not be left holding a stale file it never wrote, and the two writers we are
    separating are session-starts seconds apart, not microseconds. A residual
    race would at worst run two idempotent, hash-gated retries — the same cost
    as the pre-v0.2.91 behaviour with no lock at all.
    """
    path = pidfile_path(folder)
    if _lock_is_held(path):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        # Cannot write the lock ⇒ cannot serialise. The caller proceeds
        # unlocked rather than silently skipping owed work: handlers are
        # idempotent, and a read-only `.claude/state` must not disable retries.
        return None
    return path


def _release_lock(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        if _read_pidfile(path) == os.getpid():
            path.unlink()
    except OSError:
        pass


def dispatch(
    folder: Path,
    *,
    condition_ids: Optional[Sequence[str]] = None,
    backend_probe: Optional[Callable[[Path, str], Optional[bool]]] = None,
    runner: Optional[Callable[[Sequence[str], Path], int]] = None,
    python: str = "",
    single_instance: bool = True,
) -> list[RetryResult]:
    """Retry every owed condition whose precondition now holds.

    The gate order matters and is the safety property:

    1. no other driver holds this folder's lock;
    2. registry says the condition is ``auto_retryable`` AND names a handler;
    3. attempts so far < :data:`MAX_ATTEMPTS`;
    4. the probe for the backend THAT handler needs returns **True** (False and
       None both skip);
    5. the attempt is recorded, THEN the handler runs;
    6. the condition is resolved only when the child's own paired clear removed
       it from the ledger — never on the exit code alone.

    ``single_instance=False`` is for tests that drive the dispatcher many times
    over one folder without exercising the lock.
    """
    folder = Path(folder)
    lock: Optional[Path] = None
    if single_instance:
        lock = _acquire_lock(folder)
        if lock is None and _lock_is_held(pidfile_path(folder)):
            return [
                RetryResult(cid, SKIPPED, "another retry driver is already running")
                for cid in (
                    list(condition_ids)
                    if condition_ids is not None
                    else owed_condition_ids(folder)
                )
            ]
    try:
        return _dispatch_locked(
            folder,
            condition_ids=condition_ids,
            backend_probe=backend_probe,
            runner=runner,
            python=python,
        )
    finally:
        _release_lock(lock)


def _dispatch_locked(
    folder: Path,
    *,
    condition_ids: Optional[Sequence[str]],
    backend_probe: Optional[Callable[[Path, str], Optional[bool]]],
    runner: Optional[Callable[[Sequence[str], Path], int]],
    python: str,
) -> list[RetryResult]:
    explicit = condition_ids is not None
    cids = list(condition_ids) if explicit else owed_condition_ids(folder)
    trail(
        f"ledger read at {folder}: "
        + (
            f"{len(cids)} owed retryable condition(s): {', '.join(cids)}"
            if cids
            else "no retryable condition owed (nothing to dispatch)"
        )
    )
    results: list[RetryResult] = []
    #: backend kind → tri-state, probed at most once per kind per pass.
    probed: dict[str, Optional[bool]] = {}
    for cid in cids:
        name = handler_name_for(cid)
        if name is None:
            # Only reachable when the CALLER named the cids: owed_condition_ids
            # already filters on this. Trail it rather than `continue`-ing in
            # silence — an unexplained absence from the results is the exact
            # thing the empty-log defect taught us not to ship.
            trail(f"{cid}: not retryable (registry declares no retry_action) — skipping")
            continue
        handler = HANDLERS.get(name)
        if handler is None:
            results.append(
                RetryResult(cid, SKIPPED, f"registry names unknown handler {name!r}")
            )
            continue
        attempts = attempt_count(folder, cid)
        trail(f"{cid}: handler `{name}` (needs {handler.backend} backend), "
              f"{attempts}/{MAX_ATTEMPTS} attempt(s) so far")
        if attempts >= MAX_ATTEMPTS:
            results.append(
                RetryResult(cid, SKIPPED, f"attempt cap ({MAX_ATTEMPTS}) reached")
            )
            continue
        ctx = RetryContext(
            folder=folder,
            condition_id=cid,
            backend_probe=backend_probe,
            runner=runner,
            python=python,
        )
        if handler.backend not in probed:
            probed[handler.backend] = ctx.backend_available(handler.backend)
        backend = probed[handler.backend]
        trail(
            f"{cid}: {handler.backend} backend gate → "
            + {True: "reachable", False: "provably down"}.get(backend, "unknown")
        )
        if backend is not True:
            results.append(
                RetryResult(
                    cid, SKIPPED,
                    f"no {handler.backend} embedding backend reachable"
                    if backend is False
                    else f"{handler.backend} backend reachability unknown",
                )
            )
            continue
        # Recorded BEFORE the handler runs: a crash must still consume its
        # attempt, or the cap can never engage on a handler that always dies.
        record_attempt(folder, RetryResult(cid, STARTED, f"handler {name}"))
        result = handler.run(ctx)
        if result.status == RETRIED:
            cleared = condition_cleared(folder, cid)
            trail(
                f"{cid}: ledger re-read → "
                + {
                    True: "the child's own clear removed the condition",
                    False: "the condition is STILL in the ledger",
                }.get(cleared, "the ledger could not be re-read")
            )
            if cleared is not True:
                result = RetryResult(
                    cid, INCONCLUSIVE,
                    f"{result.detail}; the condition is STILL in the ledger"
                    if cleared is False
                    else f"{result.detail}; the ledger could not be re-read",
                )
        record_attempt(folder, result)
        if result.status == RETRIED:
            _record_resolution(folder, cid, result.detail)
        results.append(result)
    return results


def _log_path_for_stamp(stamp: str) -> Optional[Path]:
    """``<vct_root_dir>/logs/deferral-retry-<stamp>.log``, or ``None``."""
    try:
        from vco_lib.paths import vct_root_dir

        logs_dir = vct_root_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / f"deferral-retry-{stamp}.log"
    except Exception:  # noqa: BLE001 — logging must never block the spawn
        return None


def _record_resolution(folder: Path, condition_id: str, detail: str) -> None:
    """Record the auto-resolution the CHILD performed. Soft-fail.

    Deliberately does NOT call ``resolve_conditions``: by the time we get here
    the child's own paired clear has already removed the entry — that removal
    is the evidence we gated on. Re-resolving would additionally TOMBSTONE the
    id for this process's run, which is precisely how the pre-fix dispatcher
    erased entries a skip-path child had just re-written.
    """
    try:
        from vco_lib.deferral_emit import record_auto_resolution

        record_auto_resolution(
            Path(folder), condition_id, "retried_owed_work", detail,
        )
    except Exception:  # noqa: BLE001 — bookkeeping never fails the retry
        pass


# ---------------------------------------------------------------------------
# Detached spawn — the session-start trigger
# ---------------------------------------------------------------------------

#: Handles for children that deliberately outlive us (suppresses Popen's
#: destructor ResourceWarning) — the codegraph-resync driver's precedent.
_DETACHED_CHILDREN: list = []


def spawn_detached(folder: Path, *, python: str = "") -> bool:
    """Spawn ``python -m vco_lib.deferral_retry --folder <folder>`` detached.

    Used by the session-start hook: the hook must return in milliseconds, and
    a KG seed can take minutes. Returns True when the child was launched (NOT
    when the retry succeeded — nobody waits for that).

    Child stdout/stderr go to ``<vct_root_dir>/logs/deferral-retry-*.log`` so a
    driver that dies mid-run leaves a record (the R-5 lesson: DEVNULL is how a
    walk dies at 40% with no trace anywhere).
    """
    argv = [python or sys.executable, "-m", "vco_lib.deferral_retry",
            "--folder", str(folder)]
    log_handle = None
    log_path = _log_path_for_stamp(time.strftime("%Y%m%d-%H%M%S"))
    if log_path is not None:
        try:
            log_handle = open(log_path, "ab")
            # v0.2.91 dogfood fix: the SPAWNER writes the first line, before
            # Popen. Without it a child that dies before its first print (a
            # bad interpreter, an import error swallowed by the OS, a spawn
            # that never happened) leaves a zero-byte file indistinguishable
            # from a healthy "nothing owed" pass — which is exactly what the
            # field logs looked like.
            log_handle.write(
                (
                    f"# vco deferral-retry driver spawned "
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                    f"# folder: {folder}\n"
                    f"# argv:   {' '.join(argv)}\n"
                ).encode("utf-8")
            )
            log_handle.flush()
        except Exception:  # noqa: BLE001 — logging must never block the spawn
            log_handle = None
    child_out = log_handle if log_handle is not None else subprocess.DEVNULL
    kwargs = {
        "cwd": str(folder),
        "stdout": child_out,
        "stderr": child_out,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        _DETACHED_CHILDREN.append(
            subprocess.Popen(argv, **kwargs)  # noqa: S603 — argv is ours
        )
        return True
    except Exception:  # noqa: BLE001 — a failed spawn is a no-op, never a crash
        return False
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass


def session_start_owed_check(folder: Path, *, python: str = "") -> list[str]:
    """Session-start trigger: owed retryable cids, handed to a detached driver.

    ONE home for the check BOTH session-start hooks perform, so the bash and
    PowerShell siblings carry five lines of glue each instead of two copies of
    the logic (A-leg of the A>B>C rule — the hook already runs a Python
    interpreter, so there is no reason for the rule to live in shell).

    Contract:

    * Returns the owed condition ids (possibly empty). The caller prints them;
      this function never prints.
    * Spawns AT MOST ONE detached driver per call, and only when something is
      owed. The driver — not the hook — probes the backend, so a session that
      starts before the containers finish coming up simply retries next time
      rather than the hook having to guess.
    * Never raises: a SessionStart hook must not be able to break a session.
    """
    try:
        owed = owed_condition_ids(Path(folder))
    except Exception:  # noqa: BLE001
        return []
    if not owed:
        return []
    try:
        spawn_detached(Path(folder), python=python)
    except Exception:  # noqa: BLE001 — a failed spawn is a no-op
        pass
    return owed


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.deferral_retry [--folder DIR] [--json]``.

    Exit 0 whenever the pass RAN (including "nothing owed" and "backend still
    down") — this is a best-effort background driver, and a non-zero exit would
    turn a legitimately-skipped retry into a visible failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.deferral_retry",
        description=(
            "Retry deferral conditions the registry marks auto_retryable, "
            "when their precondition holds. Idempotent; attempt-capped."
        ),
    )
    parser.add_argument("--folder", type=Path, default=None,
                        help="project folder (default: current directory)")
    parser.add_argument("--json", action="store_true",
                        help="emit the machine-readable result list")
    parser.add_argument("--list", action="store_true",
                        help="list owed retryable conditions and exit")
    args = parser.parse_args(argv)
    folder = Path(args.folder) if args.folder else Path.cwd()

    if args.list:
        owed = owed_condition_ids(folder)
        print(json.dumps(owed) if args.json else "\n".join(owed))
        return 0

    # --json is a machine contract: stdout carries the result list and nothing
    # else. The human trail is suppressed there rather than corrupting it.
    if not args.json:
        trail(
            f"driver start pid={os.getpid()} "
            f"ts={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"folder={folder}"
        )
    results = dispatch(folder) if not args.json else _dispatch_quiet(folder)
    if args.json:
        print(json.dumps([
            {"condition_id": r.condition_id, "status": r.status, "detail": r.detail}
            for r in results
        ]))
        return 0
    for r in results:
        trail(f"{r.condition_id}: {r.status} — {r.detail}")
    tally: dict[str, int] = {}
    for r in results:
        tally[r.status] = tally.get(r.status, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "none"
    trail(f"driver done: {len(results)} result(s) [{summary}]")
    return 0


def _dispatch_quiet(folder: Path) -> list[RetryResult]:
    """``dispatch`` with the trail silenced — for the ``--json`` stdout contract."""
    global _TRAIL_ENABLED
    previous = _TRAIL_ENABLED
    _TRAIL_ENABLED = False
    try:
        return dispatch(folder)
    finally:
        _TRAIL_ENABLED = previous


__all__ = [
    "ATTEMPTS_FILENAME",
    "CODE_BACKEND",
    "FAILED",
    "HANDLERS",
    "INCONCLUSIVE",
    "MAX_ATTEMPTS",
    "PIDFILE_NAME",
    "PIDFILE_STALE_SECONDS",
    "RETRIED",
    "SKIPPED",
    "STARTED",
    "TEXT_BACKEND",
    "Handler",
    "RetryContext",
    "RetryResult",
    "attempt_count",
    "attempts_path",
    "condition_cleared",
    "default_backend_probe",
    "default_runner",
    "dispatch",
    "handler_name_for",
    "main",
    "owed_condition_ids",
    "pidfile_path",
    "record_attempt",
    "retry_code_graph_walk",
    "retry_codegraph_resync",
    "retry_kg_seed",
    "retryable_condition_ids",
    "session_start_owed_check",
    "spawn_detached",
    "trail",
]


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
