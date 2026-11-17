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
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from vco_lib import kg_context_triple
from vco_lib.intfile import read_int_line

# v0.2.96 (WP-3): the image-freshness verdict token the dispatch-side image
# gate branches on. Hard import — `vco_lib` importing `vco_lib` is the
# definition of a healthy install; the token itself comes from the ONE home
# (``vco_lib.code_embed_image``, via ``codegraph_resync``'s shared helper).
from vco_lib.code_embed_image import STALE as _IMAGE_STALE

#: Per (folder, condition) attempt ceiling. Beyond it the entry stays for the
#: user — a retry that failed three times is not transient.
MAX_ATTEMPTS = 3

#: v0.2.101 SF-1: how many LEDGER PASSES one driver makes before exiting.
#: install.py enqueues the per-project seed and — seconds later, at step 7d —
#: the shared seed, spawning a driver at each site. The second driver is
#: blocked by the first's per-folder pidfile lock (and the first read the
#: ledger before the second row existed), so the FIRST driver must pick the
#: later row up itself or the shared seed waits for the next session while the
#: install prints that it continues now. A pass that finds nothing NEW to do
#: ends the loop, so this bound is only reached while work keeps arriving or
#: stays permanently blocked — never a spin.
MAX_SETTLE_PASSES = 3

#: v0.2.101 (item 4): install.py no longer BLOCKS on the per-project KG seed.
#: It enqueues the owed work as ``kg_sync_failures_pending`` (whose registry
#: row declares ``retry_action = "retry:py:kg_seed"``) and spawns this driver
#: detached, so the embed runs in the background. This env channel carries the
#: context the enqueue resolved; the PER-PROJECT handler uses
#: :data:`SEED_CTX_ENV_KG_COLLECTION` to pin the child's target (Gap 3), and
#: :func:`stamp_metadata_repair_from_child` uses it as its third precondition.
#: A driver spawned any other way (the session-start hook) carries none of
#: these — which no longer costs the context triple anything, because the CHILD
#: records that itself (see below).
SEED_CTX_ENV_ACTIVE_EMBEDDING = "VCT_KG_SEED_CTX_ACTIVE_EMBEDDING"
SEED_CTX_ENV_KG_COLLECTION = "VCT_KG_SEED_CTX_KG_COLLECTION"
SEED_CTX_ENV_SHARED_KG_COLLECTION = "VCT_KG_SEED_CTX_SHARED_KG_COLLECTION"

#: The marker a SHARED-seed child carries (see that module): it refuses to
#: record the per-project context triple. Re-exported so the shared handler
#: and the child cannot disagree about the name.
#:
#: NOTE: the four ``app_state`` KEY names are NOT re-exported here any more
#: (v0.2.101 nit): this module has no reader of them, the only consumers were
#: tests, and a re-export kept "for the readers" that do not exist is the
#: false-promise shape. Tests import them from :mod:`vco_lib.kg_context_triple`.
SHARED_SEED_ENV = kg_context_triple.SHARED_SEED_ENV

#: Attempt trail. Sits beside auto-resolutions.jsonl in the user-owned,
#: git-ignored logs dir.
ATTEMPTS_FILENAME = "deferral-retries.jsonl"

#: Single-instance lock for the driver, per folder. Two session-starts a
#: second apart must not run two KG seeds over the same tree.
PIDFILE_NAME = "deferral-retry.pid"

#: A held lock whose LAST HEARTBEAT is older than this is treated as abandoned
#: even when its pid still looks alive — a driver wedged inside one handler.
#:
#: v0.2.101 (S2): the age is NOT "time since the driver started". The driver
#: touches its pidfile before every handler and between settle passes
#: (:func:`_heartbeat_lock`), so the bound covers ONE handler's silence. Since
#: v0.2.101 item 4 the driver also runs the full install-time seeds, and one
#: pass — ``kg_seed`` + ``kg_seed_shared`` + ``code_graph_walk`` +
#: ``codegraph_resync``, up to :data:`MAX_SETTLE_PASSES` times — legitimately
#: runs for many hours on a slow machine: a full KG sync alone can take well
#: over an hour on a big KG and a slow CPU (owner ruling 2026-10-06: shipped
#: timeouts are VERY long by design), and a code-graph walk over a wide
#: codebase is similar. Twenty-four hours without a single handler finishing
#: cannot be a live seed.
#:
#: Pid REUSE is not this bound's job any more: the pidfile records the
#: holder's process start token and :func:`_lock_is_held` compares it
#: (:func:`vco_lib.deferral_probes.process_identity_matches`), so a crashed
#: driver whose pid number was handed to another process is taken over at
#: once. The age bound only backs that up where the identity cannot be read.
PIDFILE_STALE_SECONDS = 24 * 3600

#: Result states.
STARTED = "started"       # attempt recorded BEFORE the handler ran
RETRIED = "retried"       # handler ran AND the child cleared its condition
FAILED = "failed"         # handler ran and did not succeed
#: Handler exited 0 but its condition is still in the ledger (or the ledger
#: could not be re-read). The child's SKIP paths exit 0 too, so this is the
#: honest verdict: something ran, nothing is proven, resolve nothing.
INCONCLUSIVE = "inconclusive"
SKIPPED = "skipped"       # precondition absent (backend down, cap reached…)

#: TRAIL-ONLY row status: a pass that reached this condition, found its
#: handler and its remaining attempts, and then declined to run because the
#: backend that handler needs was not reachable.
#:
#: v0.2.92 (WFT C7, register item 7). It is NOT a :class:`RetryResult` status
#: callers can receive — the backend gate still RETURNS :data:`SKIPPED`, whose
#: meaning callers and tests already depend on. It exists because the ledger
#: renders ``auto_retryable`` as "VCO retries this itself", a promise that is
#: true only while the backend eventually comes back, and NOTHING recorded the
#: fact that it had not. Verified before writing this: after five dispatch
#: passes with the backend down, ``attempts_path`` did not exist and
#: ``attempt_count`` returned 0 — every blocked pass wrote its verdict to a
#: per-run detached log and nowhere durable. So "VCO has retried N times and
#: the service was down each time" was not a sentence the code could form. Now
#: it is: :func:`retry_history` counts these rows and
#: :func:`retry_disposition_note` renders them.
#:
#: Deliberately a DIFFERENT status from :data:`STARTED`, so the attempt cap is
#: untouched — a machine whose backend is down for a week must still retry on
#: the day it comes back (``test_skips_do_not_burn_the_cap``).
BLOCKED = "blocked"

#: TRAIL-ONLY row status: the condition's ledger entry was CLEARED by
#: something other than this dispatcher (the resync driver run by hand, an
#: update's positive-zero probe — :func:`record_episode_end`). Like
#: :data:`BLOCKED` it is never a :class:`RetryResult` status callers receive.
#: It exists so the attempt cap is scoped to ONE EPISODE of a condition (see
#: :func:`retry_history`): without it a condition that came back months after
#: it was last fixed inherited the earlier episodes' attempts and was reported
#: as capped before VCO had retried it once (field 2026-10-16).
RESOLVED = "resolved"

#: v0.2.107 (A-003) verdict row: the attempt ran, did not clear its condition,
#: and there is POSITIVE evidence the MACHINE failed during it — Weaviate
#: answered "not ready" right after it, or its container start time changed
#: (``owed_work.machine_failed_between``). The row closes the attempt and
#: :func:`retry_history` does not count it toward the cap. Never written for a
#: worker that merely died (a ``STARTED`` row with no verdict still counts —
#: the v0.2.91 crash-loop rule), and at most :data:`MAX_EXCUSED_INTERRUPTIONS`
#: times per episode: a job that itself brings Weaviate down must still reach
#: the cap.
INTERRUPTED = "interrupted"
MAX_EXCUSED_INTERRUPTIONS = 3

#: v0.2.107 (A-003) RESULT-only status (never a trail row): the job did not
#: run and no attempt was consumed — Weaviate was not ready, or another
#: driver holds this folder. The machine queue keeps the project's entry.
QUEUED = "queued"
WEAVIATE_NOT_READY_DETAIL = (
    "Weaviate is not ready — left in the machine queue (no attempt consumed)"
)

#: v0.2.104 (R1 M1): names the dispatched attempt a clear happened INSIDE.
#: The dispatcher sets it in its own environment around ``handler.run`` (so
#: the handler, its children and their children inherit it) and records the
#: same value on the attempt's :data:`STARTED` row; :func:`record_episode_end`
#: copies it onto the :data:`RESOLVED` row, together with whether the
#: dispatcher that opened the attempt is still alive. That is how
#: :func:`retry_history` tells "the handler's own child cleared it while its
#: parent waits for the verdict" (leave the episode open) from "the entry was
#: cleared by hand after a driver died mid-attempt" (end it). Format:
#: ``<dispatcher pid>-<nonce>``.
ATTEMPT_ENV = "VCO_RETRY_ATTEMPT"
#: The dispatcher's process start token, beside :data:`ATTEMPT_ENV`, so the
#: liveness check survives pid reuse (``deferral_probes.process_identity_matches``).
ATTEMPT_DRIVER_TOKEN_ENV = "VCO_RETRY_ATTEMPT_DRIVER_TOKEN"

#: Consecutive blocked passes after which the generic "VCO retries this
#: itself" stops being the most useful thing to say. Below it the condition is
#: plausibly transient and the generic disposition is fair; at it, the reader
#: is owed the specific history instead.
BLOCKED_NOTE_THRESHOLD = 3

#: WHY a pass was blocked. The two gates below can both block the SAME
#: condition, and the honest note depends on which one did — so the gate that
#: blocks writes a detail from here and :func:`classify_block_detail` reads it
#: back. ONE home for the vocabulary: a detail edited on the writing side only
#: would silently revert the note to the wrong cause, which is the v0.2.96
#: ship-gate's MINOR-3 (the cause used to come from the HANDLER's static
#: image-gate FLAG, so a service that was simply DOWN told the user to rebuild
#: an image for a service that was not running).
BLOCK_CAUSE_BACKEND = "backend"
BLOCK_CAUSE_IMAGE = "image"
#: Neither — an old trail whose rows predate this vocabulary, or a future gate
#: that has not declared itself. The note then reports the streak WITHOUT
#: naming a cause, which is the honest answer (never a guessed one).
BLOCK_CAUSE_UNKNOWN = ""

#: The blocked-by-image detail, verbatim. A constant because two places must
#: agree on it: the gate that writes the row and the classifier that reads it.
BLOCKED_BY_IMAGE_DETAIL = (
    "code-embed image is STALE (older than its source) — embedding through "
    "it would silently truncate; the retry resumes by itself once the image "
    "is rebuilt"
)


def backend_block_detail(kind: str, reachable: Optional[bool]) -> str:
    """The blocked-by-backend detail for ``kind``, verbatim.

    Same ONE-home reason as :data:`BLOCKED_BY_IMAGE_DETAIL`; a function
    because the sentence names the backend and distinguishes "provably down"
    from "could not tell", both of which are reachability, not freshness.
    """
    return (
        f"no {kind} embedding backend reachable"
        if reachable is False
        else f"{kind} backend reachability unknown"
    )


def classify_block_detail(detail: str) -> str:
    """Which gate wrote this BLOCKED row's detail — one of the
    ``BLOCK_CAUSE_*`` tokens.

    Reads the detail rather than the handler's declared preconditions
    because a handler can have BOTH (``codegraph_resync`` needs a reachable
    code backend AND a current image), and only the row records which one
    actually blocked. Unrecognised text is :data:`BLOCK_CAUSE_UNKNOWN` — a
    row written by an older version says nothing about its cause, and
    inventing one for it is the defect this replaces.
    """
    text = str(detail or "")
    if BLOCKED_BY_IMAGE_DETAIL in text:
        return BLOCK_CAUSE_IMAGE
    for kind in (TEXT_BACKEND, CODE_BACKEND):
        if (
            backend_block_detail(kind, False) in text
            or backend_block_detail(kind, None) in text
        ):
            return BLOCK_CAUSE_BACKEND
    return BLOCK_CAUSE_UNKNOWN

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
        if self.python:
            return self.python
        # v0.2.94: the orchestrator venv, never a bare sys.executable — the
        # driver may itself have been started under the launcher's bootstrap
        # python; its children (kg-sync, the analyzer) need vco_lib + weaviate.
        from vco_lib.python_exe import resolve_or_current

        return resolve_or_current()


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


def _code_embed_image_verdict(folder: Path) -> str:
    """v0.2.96 (WP-3): the code-embed image verdict for ``folder``.

    ONE home — ``vco_lib.codegraph_resync.code_embed_image_verdict`` — so the
    dispatcher's gate and the gate inside the driver it spawns consult the
    same verdict and can never disagree (review M-1: a gate only install.py
    consults is bypassed by this very dispatcher). ``unknown`` on any failure,
    including a broken import: could-not-look must not read as a block.
    """
    try:
        from vco_lib.codegraph_resync import code_embed_image_verdict
    except Exception:  # noqa: BLE001 — broken install ⇒ unknown, never a crash
        return "unknown"
    try:
        return code_embed_image_verdict(folder)
    except Exception:  # noqa: BLE001 — a failed probe is unknown
        return "unknown"


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


def seed_context_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """The install-resolved context triple carried in a spawned driver's env.

    ``None`` when no ACTIVE_EMBEDDING was carried — a driver spawned by the
    session-start hook (not by an install) must never stamp the triple, and an
    install that never resolved one must not stamp an empty context over a real
    one. See :data:`SEED_CTX_ENV_ACTIVE_EMBEDDING`.
    """
    source = os.environ if env is None else env
    active = (source.get(SEED_CTX_ENV_ACTIVE_EMBEDDING) or "").strip()
    if not active:
        return None
    return {
        "active_embedding": active,
        "kg_collection": (source.get(SEED_CTX_ENV_KG_COLLECTION) or "").strip(),
        "shared_kg_collection": (
            source.get(SEED_CTX_ENV_SHARED_KG_COLLECTION) or ""
        ).strip(),
    }


def stamp_metadata_repair_from_child(folder: Path) -> bool:
    """Project install.py's ``app_state`` metadata-repair row (v0.2.101 item 4).

    The whole-tree pass is ENQUEUED now, so install.py never observes its exit
    and cannot project the row itself. This is the same move the context triple
    made: the handler records it, on the same proof (the child's own paired
    clear), through the SAME writer install.py uses —
    :func:`vco_lib.install_weaviate.stamp_kg_metadata_repair` — so the two
    paths cannot disagree about what the row means.

    Two preconditions ride along, both install.py's own:

    * a CARRIED context naming a collection — install.py's third precondition
      (``_sync_all and bool(current_kg_collection)``): the whole-tree run must
      have targeted the project's CONFIGURED collection, not the script's
      literal ``KnowledgeGraph`` fallback. No carried context ⇒ no proof.
    * ``repair_owed(folder) is False`` — inside the writer. This is what makes
      the row a PROJECTION rather than a second opinion: a repair that aborts
      part-way is COUNTED, not FAILED, so the run still exits 0 and the child
      withholds its file stamp. The exit code (and the paired clear with it)
      cannot see that; the file stamp can.

    Returns True when the row was written. Soft-fail: bookkeeping never fails
    the retry (mirrors install.py's own ``_write_app_state_key``).
    """
    context = seed_context_from_env()
    if not context or not context.get("kg_collection"):
        return False
    try:
        from vco_lib.install_weaviate import stamp_kg_metadata_repair
        from vco_lib.launcher_db_writer import write_app_state_key
        from vco_lib.paths import launcher_db_path

        return stamp_kg_metadata_repair(
            True, True,
            lambda key, value: write_app_state_key(launcher_db_path(), key, value),
            project_root=folder,
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
        trail(f"metadata-repair stamp failed (non-fatal): {exc}")
        return False


def retry_kg_seed(ctx: RetryContext) -> RetryResult:
    """Re-run the owed KG seed for ``folder``.

    Reuses the SHIPPED sync path (``sync_knowledge_graph.py --all``) rather
    than a second seeding implementation: that script is content-hash gated
    (unchanged nodes skip the embed), so a retry over an already-seeded tree is
    cheap, and its success path carries WP-B's own narrow clear.

    :data:`RETRIED` here means only "the child exited 0" — the script's SKIP
    path exits 0 as well. :func:`dispatch` downgrades it to
    :data:`INCONCLUSIVE` unless the child's own clear removed the condition.

    v0.2.101: this handler does NOT write the context triple. The CHILD does,
    on its own clean whole-tree success path, and only when the tree it walked
    is the ORCHESTRATOR ROOT's (``vco_lib.kg_context_triple`` documents why:
    the row is machine-global and install.py's comparison is the root's, so a
    registered project's ``--all`` — install-spawned, session-start-spawned, the
    launcher's Sync, a hand-run — must leave it untouched). That is also why a
    session-start driver converges exactly like an install-spawned one: neither
    needs anything carried.

    What this handler still owns is the metadata-repair PROJECTION and the
    child's TARGET pin (Gap 3).
    """
    script = _sync_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no sync_knowledge_graph.py found")
    rc = _run_project_sync(ctx, script, ["--all"])
    if rc == 0:
        if condition_cleared(ctx.folder, ctx.condition_id) is True:
            # v0.2.101 (caller-audit Gap 4/5/6): the CONTEXT TRIPLE is no
            # longer written here — the child that walked the tree recorded it
            # on its own success path (``vco_lib.kg_context_triple``), which is
            # why a session-start driver converges exactly like an
            # install-spawned one, and why the launcher's Sync / migrate /
            # hand-run paths do too. What stays is the metadata-repair
            # PROJECTION: not a context claim, but a reading of the child's own
            # file stamp.
            stamp_metadata_repair_from_child(ctx.folder)
        return RetryResult(ctx.condition_id, RETRIED, "kg sync --all completed")
    return RetryResult(ctx.condition_id, FAILED, f"kg sync --all exited {rc}")


def _run_project_sync(ctx: RetryContext, script: Path, args: Sequence[str]) -> int:
    """Run ``sync_knowledge_graph.py <args>`` for ``ctx.folder`` with the
    project-seed pins — ONE home for every per-project KG handler."""
    # v0.2.101 (caller-audit Gap 3): pin the child's TARGET from the context the
    # enqueue carried. Without it the child resolves its class through the hub
    # alone, and in a hub-down window `_resolve_collections` falls back to the
    # literal "KnowledgeGraph" — a walk into a class nobody reads. The hub still
    # WINS when it answers (`cfg.kg_collection or os.getenv(...)`), so this is
    # strictly a fallback, and it is what the foreground path always had (the
    # install's own shell env). S1 keeps the DRIVER base env free of it; the
    # carried value is the only way in.
    carried = seed_context_from_env() or {}
    _pinned = ("KG_SYNC_PROJECT_ROOT", "KG_COLLECTION")
    prev = {key: os.environ.get(key) for key in _pinned}
    os.environ["KG_SYNC_PROJECT_ROOT"] = str(ctx.folder)
    if carried.get("kg_collection"):
        os.environ["KG_COLLECTION"] = carried["kg_collection"]
    try:
        return ctx.run([ctx.interpreter(), str(script), *[str(a) for a in args]])
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def retry_kg_sync_paths(ctx: RetryContext) -> RetryResult:
    """v0.2.107 (A-003): ``kg_sync_failures_pending`` re-syncs ONLY the paths
    that failed (``owed_work.owed_failed_sync_paths``). The child's file-list
    run is the paired clear (``owed_work.settle_clean_sync_paths``: zero
    failures and nothing the entry owes left). No record for the CURRENT
    entry — it owes the whole tree (install.py's enqueued seed uses this
    condition) — falls back to :func:`retry_kg_seed`'s ``--all``."""
    from vco_lib.owed_work import owed_failed_sync_paths

    paths = owed_failed_sync_paths(ctx.folder)
    if not paths:
        return retry_kg_seed(ctx)
    script = _sync_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no sync_knowledge_graph.py found")
    rc = _run_project_sync(ctx, script, paths)
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED,
                           f"kg sync of the {len(paths)} failed path(s) completed")
    return RetryResult(ctx.condition_id, FAILED,
                       f"kg sync of the {len(paths)} failed path(s) exited {rc}")


def retry_kg_node_formats(ctx: RetryContext) -> RetryResult:
    """v0.2.107 (A-003): ``kg_node_formats_refresh_failed`` re-runs the
    ``.node_formats.json`` refresh ALONE (``--refresh-node-formats``) — it
    writes no Weaviate row, so it never needed the whole-tree sync it used to
    get. The refresh's own paired clear retires the entry."""
    script = _sync_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no sync_knowledge_graph.py found")
    rc = _run_project_sync(ctx, script, ["--refresh-node-formats"])
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED, "node-format refresh completed")
    return RetryResult(ctx.condition_id, FAILED, f"node-format refresh exited {rc}")


def retry_kg_secondary_backfill(ctx: RetryContext) -> RetryResult:
    """v0.2.107 (A-003): ``kg_secondary_backfill_pending`` embeds only the rows
    that lack the secondary vector (``python -m vco_lib.owed_work backfill``,
    no tree walk). Only when rows the secondary refuses whole remain — which
    only kg-sync's writer can re-lay — does it fall back to ``--all``."""
    from vco_lib.owed_work import BACKFILL_DONE, BACKFILL_NEEDS_WALK

    rc = ctx.run([ctx.interpreter(), "-m", "vco_lib.owed_work", "backfill",
                  "--folder", str(ctx.folder)])
    if rc == BACKFILL_NEEDS_WALK:
        trail(f"{ctx.condition_id}: refused rows need kg-sync's writer — whole-tree sync")
        return retry_kg_seed(ctx)
    if rc == BACKFILL_DONE:
        return RetryResult(ctx.condition_id, RETRIED, "secondary backfill completed")
    return RetryResult(ctx.condition_id, FAILED, f"secondary backfill exited {rc}")


def _shared_collection_for(folder: Path, env: Optional[Mapping[str, str]] = None) -> str:
    """The shared KG collection ``folder`` should be seeded into, or ``""``.

    Precedence: the context an install spawn CARRIED
    (:data:`SEED_CTX_ENV_SHARED_KG_COLLECTION`) — it is what the install
    resolved and what the owed row was queued for — then the project's OWN
    declared ``SHARED_KG_COLLECTION`` in ``.claude/settings.json`` ``env`` (the
    canonical per-project channel), so a driver started by the session-start
    hook retries the same work with no install env to inherit.
    """
    source = os.environ if env is None else env
    carried = (source.get(SEED_CTX_ENV_SHARED_KG_COLLECTION) or "").strip()
    if carried:
        return carried
    from vco_lib.claude_settings_env import read_settings_env

    # The settings layer as Claude Code merges it (settings.json, then
    # settings.local.json — v0.2.104 L5); soft-fail ⇒ nothing to target.
    return str(read_settings_env(folder).get("SHARED_KG_COLLECTION") or "").strip()


def retry_kg_seed_shared(ctx: RetryContext) -> RetryResult:
    """Re-run the owed SHARED-collection seed for ``folder``.

    v0.2.101 item 4: install.py used to BLOCK on this seed (the same
    ``sync_knowledge_graph.py --all``, pointed at the shared class with
    ``KG_COLLECTION=<shared>`` in the child env). It is now enqueued instead,
    so the owed work needs this handler.

    It writes NO context record, and it makes sure the CHILD cannot either:
    the child env carries :data:`SHARED_SEED_ENV`, which is the guard
    ``vco_lib.kg_context_triple`` reads (a shared-targeted pass is not a
    statement about the project's own context — and the collection NAME cannot
    tell the two apart on the orchestrator root, where ``KG_COLLECTION ==
    SHARED_KG_COLLECTION`` by design). The paired clear is the script's own
    narrow one (``_clear_shared_seed_deferral``): only a fully successful
    ``--all`` that TARGETED the shared class retires the row, which is exactly
    what :func:`condition_cleared` re-reads.

    Returns :data:`SKIPPED` when this folder has no shared collection
    configured — there is genuinely nothing to seed, and the row is then the
    user's to dismiss (matching the pre-v0.2.91 disposition for a condition
    that cannot resolve itself).
    """
    script = _sync_script(ctx.folder)
    if script is None:
        return RetryResult(ctx.condition_id, SKIPPED, "no sync_knowledge_graph.py found")
    target = _shared_collection_for(ctx.folder)
    if not target:
        return RetryResult(
            ctx.condition_id, SKIPPED,
            "no SHARED_KG_COLLECTION is configured for this folder — nothing "
            "to seed into",
        )
    keys = ("KG_COLLECTION", "KG_BASE_DIR", "KG_SYNC_PROJECT_ROOT", SHARED_SEED_ENV)
    prev = {k: os.environ.get(k) for k in keys}
    os.environ["KG_COLLECTION"] = target
    os.environ["KG_BASE_DIR"] = str(ctx.folder)
    os.environ["KG_SYNC_PROJECT_ROOT"] = str(ctx.folder)
    # The child must not record the PER-PROJECT context triple: this run is
    # about the shared class (see kg_context_triple.SHARED_SEED_ENV).
    os.environ[SHARED_SEED_ENV] = "1"
    try:
        rc = ctx.run([ctx.interpreter(), str(script), "--all"])
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if rc == 0:
        return RetryResult(ctx.condition_id, RETRIED, "shared kg sync --all completed")
    return RetryResult(ctx.condition_id, FAILED, f"shared kg sync --all exited {rc}")


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
    # v0.2.103: this is a FULL-TREE (revision-gated) walk — it goes through
    # the ONE per-project walk claim (vco_lib.codegraph_walk), so a retry
    # never starts a twin beside a launcher/driver walk of the same project.
    # A held claim SKIPs the attempt (the entry stays owed for a later
    # retry — the same posture as "another retry driver is already running");
    # the walk is never killed and never waited on. The claim key falls back
    # to the folder name when no project name resolves — the same default the
    # analyzer itself applies to --project.
    from vco_lib.codegraph_walk import WalkAlreadyRunning, walk_claim

    try:
        with walk_claim(project or ctx.folder.name):
            rc = ctx.run(argv)
    except WalkAlreadyRunning:
        return RetryResult(
            ctx.condition_id, SKIPPED,
            "another code-graph walk is already running for this project — "
            "left owed for a later retry",
        )
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
    """The analyzer ``--project`` value: the folder's code-graph COLLECTION
    PREFIX, from the ONE resolver (:mod:`vco_lib.codegraph_project_prefix`).

    v0.2.105: this used ``paths.resolve_project_name``, which returns the hub's
    ``code_graph_project`` — the launcher SLUG. The analyzer sanitized
    ``orchestrator-root`` to ``Orchestrator_root`` and every retry re-walked
    the orchestrator root into a zombie class set, so the resync entry could
    never converge. ``None`` when nothing resolves (the callers skip or let the
    analyzer apply its own default)."""
    try:
        from vco_lib.codegraph_project_prefix import resolve_codegraph_prefix

        return resolve_codegraph_prefix(folder) or None
    except Exception:  # noqa: BLE001 — analyzer defaults to the dir name
        return None


def retry_ollama_models(ctx: RetryContext) -> RetryResult:
    """v0.2.100 — the work a run that found Ollama down skipped (model pulls,
    then the KG seed); ``ollama_not_ready_at_update`` /
    ``ollama_model_pull_failed``. ONE home for that work:
    :func:`vco_lib.ollama_pull.retry_owed_model_work`, which clears both rows
    itself only on proven success (the dispatcher's ledger re-read is then the
    proof). The seed is :func:`retry_kg_seed` — no second seeding path."""
    from vco_lib.ollama_pull import BLOCKED as _BLOCKED, DONE, retry_owed_model_work

    if condition_cleared(ctx.folder, ctx.condition_id) is True:
        return RetryResult(ctx.condition_id, RETRIED, "already completed by the sibling retry")
    # The seed counts only on PROOF: exit 0 AND no "ran without a backend" row.
    status, detail = retry_owed_model_work(ctx.folder, seed=lambda: (
        retry_kg_seed(ctx).status == RETRIED
        and condition_cleared(ctx.folder, "kg_sync_no_embedding_backend") is True))
    return RetryResult(ctx.condition_id,
                       {DONE: RETRIED, _BLOCKED: SKIPPED}.get(status, FAILED), detail)


def retry_code_embed_backend(ctx: RetryContext) -> RetryResult:
    """v0.2.100 W4R-06 — ``code_embed_backend_unavailable`` (a VCO-managed
    code_embed that was DOWN at an update). No owed work beyond the service
    answering: the session-start ensure hook starts VCO's container, and the
    child :func:`vco_lib.ollama_pull.clear_code_embed_outage` re-reads the
    service and clears the row itself only when ``/health`` answers (the
    dispatcher's ledger re-read is then the proof). The code-graph work the
    outage skipped is owed under its own rows (``code_graph_*``)."""
    from vco_lib.ollama_pull import DONE, clear_code_embed_outage

    status, detail = clear_code_embed_outage(ctx.folder)
    return RetryResult(ctx.condition_id, RETRIED if status == DONE else SKIPPED, detail)


def retry_codegraph_extras_heal(ctx: RetryContext) -> RetryResult:
    """v0.2.105 (prune lane) — ``codegraph_extra_rows_missing``: re-index an
    enabled extra code folder whose rows were lost (most often by the
    pre-v0.2.105 orphan-cleanup command that pruned every unvisited row).

    The child is
    :func:`vco_lib.codegraph_extras_refresh.heal_missing_extra_paths` — a
    FULL, non-incremental re-sync of each missing path with the Sync-button
    argv, under the ONE per-project walk claim, which also performs its OWN
    paired clear (it re-detects and resolves the entry only on a POSITIVE
    empty list). This handler never touches the ledger: a zero-exit child
    that could not positively restore the rows is downgraded by the
    dispatcher's ledger re-read (:data:`INCONCLUSIVE`), exactly like every
    other tenant. Undeterminable detection ⇒ :data:`SKIPPED`.
    """
    try:
        from vco_lib.codegraph_extras_refresh import (
            heal_missing_extra_paths, missing_extra_paths,
        )
        from vco_lib.codegraph_resync import _resolve_analyzer
    except Exception as exc:  # noqa: BLE001 — broken install ⇒ skip, never crash
        return RetryResult(
            ctx.condition_id, SKIPPED,
            f"codegraph_extras_refresh unavailable: {exc}",
        )
    analyzer = _resolve_analyzer(ctx.folder)
    if analyzer is None:
        return RetryResult(
            ctx.condition_id, SKIPPED, "no analyze_code_graph.py found",
        )
    try:
        outcomes = heal_missing_extra_paths(
            str(ctx.folder), str(analyzer), ctx.interpreter())
    except Exception as exc:  # noqa: BLE001 — a raised heal is a failed one
        return RetryResult(
            ctx.condition_id, FAILED, f"extras heal raised: {exc!r}")
    if any(v in ("analyzer_failed", "record_failed", "busy")
           for v in outcomes.values()):
        return RetryResult(
            ctx.condition_id, FAILED, f"extras heal outcomes: {outcomes}")
    still = missing_extra_paths(str(ctx.folder))
    if still is None:
        return RetryResult(
            ctx.condition_id, SKIPPED,
            "post-heal detection undeterminable — left owed for a later retry",
        )
    if still:
        return RetryResult(
            ctx.condition_id, FAILED, f"still missing after heal: {still}")
    return RetryResult(
        ctx.condition_id, RETRIED,
        f"extras heal complete (outcomes={outcomes})",
    )


@dataclass(frozen=True)
class Handler:
    """One retry handler plus the backend ITS work needs.

    Pairing the two in one row is what stops the gate and the handler drifting
    apart: a handler cannot be added without declaring which backend has to be
    up for its work to be possible.

    v0.2.96 (WP-3) adds the second declared precondition:
    ``needs_current_code_embed_image`` — True when the handler's work embeds
    through the code-embed SERVICE, because a service that is reachable but
    built from stale source silently truncates over-window input at HTTP 200
    (register issue 8; the 2026-09-20 update pushed 3 156 embed requests
    through one). Reachability (``backend``) cannot see that; only the image
    verdict can, so the two gates are separate questions with the same shape.
    """

    run: Callable[[RetryContext], RetryResult]
    backend: str
    needs_current_code_embed_image: bool = False
    #: v0.2.107: the handler's child takes its OWN slot of the machine-wide
    #: bulk-writer pool (the resync driver, ``codegraph_resync``) — the
    #: worker must not hold one around it, or a 1-slot machine waits on itself.
    own_slot: bool = False
    #: v0.2.107: the work writes to (or reads from) Weaviate, so the machine
    #: queue's readiness gate applies before it starts.
    needs_weaviate: bool = True


#: handler name → handler. The registry references these as
#: ``retry_action = "retry:py:<name>"``; a row naming a handler that is not
#: here fails ``tests/test_v0291_retry_dispatch.py``.
HANDLERS: dict[str, Handler] = {
    "kg_seed": Handler(retry_kg_seed, TEXT_BACKEND),
    # v0.2.101 item 4: install.py enqueues the SHARED-collection seed too
    # (it used to block on it). Same shipped child, different target
    # collection — a distinct handler keeps each retry unambiguous about
    # which class it is filling.
    "kg_seed_shared": Handler(retry_kg_seed_shared, TEXT_BACKEND),
    # WP-3 review MAJOR-1: `code_graph_walk`'s handler does a BARE analyzer
    # walk that never enters the resync driver, so the driver-side verdict
    # gate cannot see it — without this flag, a stale-but-UP service passes
    # the reachability-only backend gate and this retry embeds straight
    # through the stale image (the exact defect WP-3 exists to close).
    "code_graph_walk": Handler(
        retry_code_graph_walk, CODE_BACKEND,
        needs_current_code_embed_image=True,
    ),
    "codegraph_resync": Handler(
        retry_codegraph_resync, CODE_BACKEND,
        needs_current_code_embed_image=True,
        own_slot=True,
    ),
    "ollama_models": Handler(retry_ollama_models, TEXT_BACKEND),
    # v0.2.107 (A-003): NARROW jobs for conditions whose registry row names
    # `kg_seed` — never named by the registry itself; `job_for_condition`
    # selects them, and a whole-tree `kg_seed` job in the same pass absorbs
    # them (its `--all` does their work and carries their clears).
    "kg_sync_paths": Handler(retry_kg_sync_paths, TEXT_BACKEND),
    "kg_node_formats": Handler(retry_kg_node_formats, TEXT_BACKEND, needs_weaviate=False),
    "kg_secondary_backfill": Handler(retry_kg_secondary_backfill, TEXT_BACKEND),
    "code_embed_backend": Handler(retry_code_embed_backend, CODE_BACKEND,
                                  needs_weaviate=False),
    # v0.2.105 (prune lane): re-indexing embeds through the code backend.
    "codegraph_extras_heal": Handler(
        retry_codegraph_extras_heal, CODE_BACKEND,
        needs_current_code_embed_image=True,
    ),
}


# ---------------------------------------------------------------------------
# Attempt ledger
# ---------------------------------------------------------------------------


def attempts_path(folder: Path) -> Path:
    return Path(folder) / ".claude" / "logs" / ATTEMPTS_FILENAME


def attempt_count(folder: Path, condition_id: str) -> int:
    """How many times a handler for this condition has been STARTED in the
    CURRENT episode here (:func:`retry_history` defines the episode).

    Counts :data:`STARTED` rows only — exactly one per dispatched handler
    invocation, written BEFORE the handler runs. Counting outcome rows instead
    (v0.2.91 pre-fix) meant a handler that CRASHED left no row at all, so the
    cap could never engage on the one failure mode it exists for: a retry that
    dies the same way every session, forever.

    An unreadable/absent trail counts as zero attempts — the cap protects
    against looping, and a missing log is not evidence that the cap was
    reached. ONE home with :func:`retry_history`, so the dispatcher's gate,
    ``vco doctor`` and the disposition note can never count differently.
    """
    return retry_history(folder, condition_id).attempts


def record_attempt(
    folder: Path, result: RetryResult, extra: Optional[Mapping[str, object]] = None,
) -> None:
    """Append one attempt row. Best-effort — observability never gates work.

    ``extra`` adds fields beside the four standard ones (the attempt id on a
    :data:`STARTED` row, the attempt context on a :data:`RESOLVED` row); it
    never overrides them.
    """
    path = attempts_path(folder)
    row: dict[str, object] = dict(extra or {})
    row.update({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "condition_id": result.condition_id,
        "status": result.status,
        "detail": result.detail,
    })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


@dataclass(frozen=True)
class RetryHistory:
    """What has actually happened to one condition's retries in one folder.

    Read from the same append-only trail :func:`record_attempt` writes, so the
    history and the cap can never disagree about what a row means.
    """

    condition_id: str
    #: :data:`STARTED` rows — one per handler invocation. THE cap input.
    attempts: int = 0
    #: :data:`BLOCKED` rows in total (the backend was down when a pass looked).
    blocked: int = 0
    #: Trailing CONSECUTIVE blocked rows. The number a disposition may quote:
    #: "down each time" is only true of an unbroken run.
    blocked_streak: int = 0
    #: ISO timestamps bounding that streak (``""`` when there is none).
    streak_first_ts: str = ""
    streak_last_ts: str = ""
    #: The NEWEST blocked row's own ``detail`` — what actually blocked, as the
    #: gate recorded it. :func:`classify_block_detail` turns it into a cause;
    #: without it a reader can only guess from the handler's declared
    #: preconditions, and a handler with two of them guesses wrong half the
    #: time (v0.2.96 ship-gate MINOR-3).
    streak_last_detail: str = ""
    #: Status of the newest row of any kind (``""`` when the trail is empty).
    last_status: str = ""
    #: :data:`INTERRUPTED` rows in this episode (each one excused an attempt).
    interrupted: int = 0

    @property
    def cap_reached(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


#: Row statuses that END an episode (see :func:`retry_history`).
_EPISODE_END_STATUSES = frozenset({RETRIED, RESOLVED})
#: Row statuses that close an OPEN attempt (the dispatcher's verdict row).
_ATTEMPT_OUTCOME_STATUSES = frozenset({RETRIED, FAILED, INCONCLUSIVE, SKIPPED})


def retry_history(folder: Path, condition_id: str) -> RetryHistory:
    """Summarise ``condition_id``'s CURRENT EPISODE in ``folder``. Never raises.

    **Episodes (v0.2.104).** The trail is append-only and lives as long as the
    project, but the cap is about one occurrence of a condition: "a retry that
    failed three times is not transient". Counting the whole file instead
    (pre-fix) meant a condition fixed three times over the months — each time
    by a COMPLETED retry — came back and was reported as "ran 3 times and has
    STOPPED" before VCO had retried it once. So every count here covers only
    the rows AFTER the last row that ended an episode:

    * :data:`RETRIED` — the dispatcher's own verdict that the handler ran AND
      the child's paired clear removed the entry (it re-reads the ledger
      before writing it, so this row is proof for every emitter alike);
    * :data:`RESOLVED` — :func:`record_episode_end`: the entry was cleared by
      something else (the resync driver run by hand, an update's
      positive-zero probe). Ignored in exactly one case: it was written
      INSIDE the attempt that is still open (its ``attempt`` field equals the
      open :data:`STARTED` row's) while that attempt's dispatcher was alive —
      the handler's own child at work, whose attempt's verdict row decides (a
      child that clears and re-emits must not reset the cap each attempt and
      loop forever). Every other clear ends the episode, including one that
      lands while a :data:`STARTED` row has no verdict because its driver was
      SIGKILLed / the machine rebooted mid-walk (R1 M1): a dangling attempt
      must not latch the episode open forever.

    Why not the ledger entry's ``detected_at`` instead: most retryable
    emitters re-emit WITHOUT ``keep_first_detected``, so ``detected_at`` moves
    on every re-emission — including a failed retry's own — and a cap scoped
    to it would never engage. The trail's own episode-end rows have no such
    hole. :data:`MAX_ATTEMPTS` therefore means "per episode": a genuinely
    stuck current episode still stops at the cap.

    An absent or unreadable trail yields an all-zero history — the same
    posture :func:`attempt_count` takes, and for the same reason: a missing
    log is not evidence that anything happened, and everything downstream of
    this treats zero as "nothing to say beyond the generic disposition".
    """
    path = attempts_path(folder)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return RetryHistory(condition_id=condition_id)
    attempts = blocked = streak = interrupted = 0
    streak_first = streak_last = streak_detail = ""
    last_status = ""
    in_attempt = False
    open_attempt = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("condition_id") != condition_id:
            continue
        status = str(row.get("status") or "")
        ts = str(row.get("ts") or "")
        last_status = status or last_status
        if status in _EPISODE_END_STATUSES and not (
            status == RESOLVED and in_attempt and _is_own_childs_clear(row, open_attempt)
        ):
            # Episode over: what follows is a NEW occurrence of the condition.
            attempts = blocked = streak = interrupted = 0
            streak_first = streak_last = streak_detail = ""
            in_attempt = False
            open_attempt = ""
            continue
        if status == INTERRUPTED:
            # v0.2.107: the machine failed during the open attempt (positive
            # evidence, recorded by the dispatcher) — that attempt is excused.
            # A row that closes no open attempt excuses nothing.
            if in_attempt and attempts > 0:
                attempts -= 1
                interrupted += 1
            in_attempt = False
            open_attempt = ""
        elif status == STARTED:
            attempts += 1
            in_attempt = True
            open_attempt = str(row.get("attempt") or "")
        elif status in _ATTEMPT_OUTCOME_STATUSES:
            in_attempt = False
            open_attempt = ""
        if status == BLOCKED:
            blocked += 1
            streak = streak + 1 if streak else 1
            streak_first = streak_first or ts
            streak_last = ts
            streak_detail = str(row.get("detail") or "")
        elif status:
            # Any non-blocked row ENDS the streak: something did run, so
            # "down each time" would no longer be true of what follows.
            streak = 0
            streak_first = streak_last = streak_detail = ""
    return RetryHistory(
        condition_id=condition_id,
        attempts=attempts,
        blocked=blocked,
        blocked_streak=streak,
        streak_first_ts=streak_first,
        streak_last_ts=streak_last,
        streak_last_detail=streak_detail,
        last_status=last_status,
        interrupted=interrupted,
    )


def _is_own_childs_clear(row: Mapping[str, object], open_attempt: str) -> bool:
    """Was this :data:`RESOLVED` row written by the OPEN attempt's own child?

    True only when the row names that attempt AND did not record its
    dispatcher as gone. A row with no ``attempt`` (a hand-run driver, an
    update's clear) or one naming another attempt is an external clear.
    """
    attempt = str(row.get("attempt") or "")
    if not attempt or attempt != open_attempt:
        return False
    return row.get("attempt_driver_alive") is not False


def _new_attempt_id() -> str:
    import uuid

    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _attempt_driver_alive(attempt: str, token: Optional[str]) -> Optional[bool]:
    """Is the dispatcher that opened ``attempt`` still running? Tri-state.

    ``None`` = cannot tell (unparsable id, probe failure) — the caller keeps
    the row's attempt context and :func:`_is_own_childs_clear` reads a
    missing verdict as alive, the direction that cannot loop.
    """
    try:
        pid = int(attempt.split("-", 1)[0])
    except ValueError:
        return None
    if pid == os.getpid():
        return True
    try:
        from vco_lib.deferral_probes import pid_is_alive, process_identity_matches

        if not pid_is_alive(pid):
            return False
        return process_identity_matches(pid, token or None) is not False
    except Exception:  # noqa: BLE001 — cannot tell
        return None


def record_episode_end(folder: Path, condition_id: str, detail: str) -> None:
    """Mark that ``condition_id``'s entry was CLEARED outside this dispatcher.

    Writes one :data:`RESOLVED` row, which closes the current episode for
    :func:`retry_history` (and so for the cap and the disposition note). Only
    conditions this dispatcher can retry get a row — the trail is the retry
    record, not a general clear log. Best-effort, never raises: a missing row
    only means the next occurrence inherits this episode's count, the
    conservative direction.

    When the clear happens inside a dispatched attempt (:data:`ATTEMPT_ENV`
    is set — the handler's child, or anything it spawned), the row carries
    that attempt's id and whether its dispatcher is still alive, which is
    what lets :func:`retry_history` keep a live attempt's own clear from
    resetting the cap while an orphaned or later clear still ends the episode.
    """
    try:
        if handler_name_for(condition_id) is None:
            return
    except Exception:  # noqa: BLE001 — bookkeeping never fails the clear
        return
    extra: dict[str, object] = {}
    attempt = (os.environ.get(ATTEMPT_ENV) or "").strip()
    if attempt:
        extra["attempt"] = attempt
        alive = _attempt_driver_alive(attempt, os.environ.get(ATTEMPT_DRIVER_TOKEN_ENV))
        if alive is not None:
            extra["attempt_driver_alive"] = alive
    record_attempt(folder, RetryResult(condition_id, RESOLVED, detail), extra)


def retry_disposition_note(folder: Path, condition_id: str) -> str:
    """One honest sentence about THIS condition's retries, or ``""``.

    WFT C7, register item 7. The ledger classes ``codegraph_embed_resync_pending``
    ``auto_retryable`` and every surface renders that as "VCO retries this
    itself" — which is true only if the backend the retry needs comes back.
    A user whose code-embed service has been down since the entry appeared
    reads a promise that is being kept in form and not in substance, with
    nothing on any surface saying so.

    ``""`` means the generic disposition is still the most accurate thing
    available (nothing has been tried, or the run was recent enough to be
    plausibly transient) — an empty note is a real answer, not a failure.

    Deliberately a PURE READER. It does not touch the ledger: the entry
    belongs to ``vco_lib.codegraph_resync``, which re-emits it on every
    deferred run, so an explicit ``disposition`` written here would be
    overwritten by the next update and revert in silence. Forking another
    component's lifecycle to correct its text is not a fix; the callers of
    THIS function render the correction where they render the condition. The
    ledger's own leg needs
    ``deferral_probes.clear_mechanism_sentence`` to consult this reader — that
    file belongs to another lane and the recipe is in this package's report.
    """
    history = retry_history(folder, condition_id)
    if history.cap_reached:
        return (
            f"VCO ran this retry {history.attempts} time(s) and has STOPPED "
            f"(cap {MAX_ATTEMPTS}) — it is ordinary manual work now, not "
            "something VCO will pick up again on its own."
        )
    if history.blocked_streak >= BLOCKED_NOTE_THRESHOLD:
        window = (
            f" since {history.streak_first_ts}" if history.streak_first_ts else ""
        )
        # WP-3 review MINOR: the streak's CAUSE is not always reachability —
        # the image-freshness gate blocks while the service answers /health
        # perfectly. Say which it was; "backend was unreachable" is false for
        # image-gate blocks and sent the user to restart a service that was
        # never down.
        #
        # v0.2.96 ship-gate MINOR-3: read that from the BLOCKED row the gate
        # actually wrote, never from the handler's declared preconditions.
        # `codegraph_resync` and `code_graph_walk` have BOTH (a code backend
        # AND a current image), so deriving the cause from the image FLAG
        # rendered "the service is up but predates the running source —
        # rebuild it" for a service that was simply DOWN: a false,
        # actionable-sounding instruction, and the mirror image of the defect
        # this note exists to correct.
        cause = {
            BLOCK_CAUSE_IMAGE: (
                "the code-embedding image it needs is stale (the service is "
                "up but predates the running source — rebuild it before "
                "expecting this to resume)"
            ),
            BLOCK_CAUSE_BACKEND: "the backend it needs was unreachable every time",
        }.get(
            classify_block_detail(history.streak_last_detail),
            # An old trail (rows predating the recorded detail) cannot say
            # which gate blocked, and guessing is what this replaced.
            "VCO did not record which precondition was missing on those "
            "passes (a trail written by an older version)",
        )
        return (
            f"VCO has been unable to retry this on the last "
            f"{history.blocked_streak} pass(es){window}: {cause}. It will "
            "retry by itself the moment that changes, and not before — "
            "so nothing changes here until then."
        )
    return ""


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


#: v0.2.107 (A-003): conditions whose registry row names ``kg_seed`` but whose
#: owed work is narrower than a whole-tree sync.
_NARROW_KG_JOBS: dict[str, str] = {
    "kg_secondary_backfill_pending": "kg_secondary_backfill",
    "kg_node_formats_refresh_failed": "kg_node_formats",
    "kg_sync_failures_pending": "kg_sync_paths",
}
_WHOLE_TREE_JOB = "kg_seed"
#: Jobs that run before the rest of a pass: ``ollama_models`` seeds the KG
#: itself once the models are pulled, so a ``kg_seed`` job behind it finds
#: its condition already cleared instead of walking the tree a second time.
_FIRST_JOBS = ("code_embed_backend", "ollama_models")


def job_for_condition(folder: Path, condition_id: str) -> Optional[str]:
    """The handler that runs ``condition_id``'s owed work — the registry's,
    narrowed for the ``kg_seed`` family (:data:`_NARROW_KG_JOBS`). A sync
    failure without a failed-path record for the CURRENT entry owes the whole
    tree and stays ``kg_seed``."""
    name = handler_name_for(condition_id)
    if name != _WHOLE_TREE_JOB:
        return name
    narrow = _NARROW_KG_JOBS.get(condition_id)
    if narrow == "kg_sync_paths":
        try:
            from vco_lib.owed_work import owed_failed_sync_paths

            return narrow if owed_failed_sync_paths(Path(folder)) else _WHOLE_TREE_JOB
        except Exception:  # noqa: BLE001 — cannot tell ⇒ the whole tree
            return _WHOLE_TREE_JOB
    return narrow or _WHOLE_TREE_JOB


def plan_jobs(folder: Path, condition_ids: Sequence[str]) -> "list[tuple[str, list[str]]]":
    """Group owed conditions into jobs — ONE run per job, however many
    conditions need it. A whole-tree ``kg_seed`` job absorbs the narrow KG jobs
    of the same project (its ``--all`` does their work and carries their
    paired clears). Conditions without a handler are left out (the caller
    trails them)."""
    groups: dict[str, list[str]] = {}
    for cid in condition_ids:
        job = job_for_condition(folder, cid)
        if job is None:
            continue
        bucket = groups.setdefault(job, [])
        if cid not in bucket:
            bucket.append(cid)
    if _WHOLE_TREE_JOB in groups:
        for narrow in dict.fromkeys(_NARROW_KG_JOBS.values()):
            groups[_WHOLE_TREE_JOB].extend(groups.pop(narrow, []))
    first = [(j, groups.pop(j)) for j in _FIRST_JOBS if j in groups]
    return first + list(groups.items())


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
    """The pid in ``path``, or ``None``. Never raises.

    One line of policy over :func:`vco_lib.intfile.read_int_line`, which is
    the ONE reader for "first line of a small state file, as an int" — pid
    files, port files and lock files all go through it. ``minimum=1`` because
    a recorded 0 is not a process any more than a missing file is.

    Two readings changed with the shared reader, both narrowing and neither
    reachable from this module's own writer (``_acquire_lock`` writes the pid
    as the FIRST line, then the start token — :func:`_read_pidfile_token`
    reads that second line): a LEADING blank line now reads as ``None`` where
    strip-then-split tolerated it, and non-UTF-8 bytes read as ``None``
    rather than raising through ``read_text``. A pidfile of either shape was
    not written by us and is better treated as absent than as a lock.
    """
    return read_int_line(path, minimum=1)


def _read_pidfile_token(path: Path) -> Optional[str]:
    """The holder's process start token (second line), or ``None``. Never raises.

    v0.2.101 (S2): written by :func:`_acquire_lock` from
    :func:`vco_lib.deferral_probes.process_start_token`. A pidfile written by
    an older driver has only the pid line, and reads as ``None`` here — the
    identity check is then skipped and the age bound alone applies, exactly
    as before.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    token = lines[1].strip() if len(lines) > 1 else ""
    return token or None


def _lock_is_held(path: Path) -> bool:
    """Is another driver provably running for this folder?

    Stale-tolerant in every direction that can be proven:

    * a pidfile whose pid is provably gone is stale — take over;
    * a pidfile whose pid is alive but names a DIFFERENT process than the one
      that wrote it (the recorded start token no longer matches — pid reuse)
      is stale — take over;
    * a pidfile whose last heartbeat is older than
      :data:`PIDFILE_STALE_SECONDS` is abandoned even if its pid looks alive
      (a driver wedged inside one handler) — take over. The holder touches the
      file before every handler and between settle passes
      (:func:`_heartbeat_lock`), so a long multi-handler pass that keeps making
      progress never ages out.

    Otherwise (alive pid with a matching or unreadable identity, or a liveness
    probe that could not tell) the lock is treated as HELD: declining to start
    a second seed is the conservative branch, and the age bound above keeps
    that from becoming permanent.
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
        from vco_lib.deferral_probes import pid_is_alive, process_identity_matches

        if not pid_is_alive(pid):
            return False
        # False = the pid was provably handed to another process. None (no
        # token recorded, or the process cannot be inspected) stays HELD.
        return process_identity_matches(pid, _read_pidfile_token(path)) is not False
    except Exception:  # noqa: BLE001 — cannot tell ⇒ assume held
        return True


def _acquire_lock(folder: Path) -> Optional[Path]:
    """Claim the per-folder driver lock, or ``None`` when it is held.

    The file holds ``<pid>\n<process start token>\n`` — the token is what lets
    a later reader tell this driver from a process that inherited its pid
    number (:func:`_lock_is_held`). The first line alone is what every pid
    reader (:func:`_read_pidfile`, an older driver mid-update) parses.

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
        from vco_lib.deferral_probes import process_start_token

        token = process_start_token(os.getpid()) or ""
    except Exception:  # noqa: BLE001 — no token ⇒ age bound only
        token = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{os.getpid()}\n{token}\n" if token else f"{os.getpid()}\n",
            encoding="utf-8",
        )
    except OSError:
        # Cannot write the lock ⇒ cannot serialise. The caller proceeds
        # unlocked rather than silently skipping owed work: handlers are
        # idempotent, and a read-only `.claude/state` must not disable retries.
        return None
    return path


def _heartbeat_lock(folder: Path) -> None:
    """Refresh this driver's pidfile mtime — "still making progress". Never raises.

    v0.2.101 (S2): called before every handler and between settle passes, so
    :data:`PIDFILE_STALE_SECONDS` measures the silence of ONE handler, not the
    length of the whole run. Touches the file ONLY while it names this process:
    a driver running unlocked (``single_instance=False``, or a lock it could
    not write) must never refresh another driver's claim — that would keep a
    dead holder's file looking live.
    """
    path = pidfile_path(folder)
    try:
        if _read_pidfile(path) == os.getpid():
            os.utime(path, None)
    except OSError:
        pass


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
    machine_watch: Optional[Callable[[], Any]] = None,
    slot_guard: Optional[Callable[[bool], Any]] = None,
) -> list[RetryResult]:
    """Retry every owed condition whose precondition now holds.

    The gate order matters and is the safety property:

    1. no other driver holds this folder's lock (held ⇒ :data:`QUEUED`, no
       attempt);
    2. registry says the condition is ``auto_retryable`` AND names a handler;
       conditions are grouped into JOBS (:func:`plan_jobs`) — one run per job;
    3. attempts so far < :data:`MAX_ATTEMPTS`;
    4. the probe for the backend THAT handler needs returns **True** (False and
       None both skip);
    5. v0.2.96 (WP-3): for handlers declaring
       ``needs_current_code_embed_image``, the code-embed image verdict must
       not be ``stale`` — a stale verdict BLOCKS without consuming an attempt
       (same convention as gate 4's BLOCKED rows);
    6. v0.2.107: with ``machine_watch`` (the machine queue's worker), Weaviate
       must answer ready — otherwise :data:`QUEUED`, no row, no attempt;
    7. the attempt is recorded, THEN the job runs (inside ``slot_guard``);
    8. the condition is resolved only when the child's own paired clear removed
       it from the ledger — never on the exit code alone. With
       ``machine_watch``, an attempt that did not clear while the machine
       provably failed is recorded :data:`INTERRUPTED` (excused).

    ``single_instance=False`` is for tests that drive the dispatcher many times
    over one folder without exercising the lock.
    """
    folder = Path(folder)
    lock: Optional[Path] = None
    if single_instance:
        lock = _acquire_lock(folder)
        if lock is None and _lock_is_held(pidfile_path(folder)):
            return [
                RetryResult(cid, QUEUED, "another retry driver is already running")
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
            machine_watch=machine_watch,
            slot_guard=slot_guard,
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
    machine_watch: Optional[Callable[[], Any]] = None,
    slot_guard: Optional[Callable[[bool], Any]] = None,
) -> list[RetryResult]:
    explicit = condition_ids is not None
    results: list[RetryResult] = []
    #: cids THIS driver has already dispatched. A second pass must never
    #: re-attempt one: each dispatch burns an attempt, and one session of a
    #: down backend must not eat the cap three times over.
    attempted: "set[str]" = set()
    for _pass in range(1 if explicit else MAX_SETTLE_PASSES):
        # S2 heartbeat: a new settle pass is progress (see PIDFILE_STALE_SECONDS).
        _heartbeat_lock(folder)
        cids = [
            cid for cid in (
                list(condition_ids) if explicit else owed_condition_ids(folder)
            ) if cid not in attempted
        ]
        trail(
            f"ledger read at {folder}: "
            + (
                f"{len(cids)} owed retryable condition(s): {', '.join(cids)}"
                if cids
                else "no retryable condition owed (nothing to dispatch)"
            )
        )
        if not cids:
            break
        attempted.update(cids)
        pass_results = _dispatch_pass(
            folder, cids=cids, backend_probe=backend_probe, runner=runner,
            python=python, machine_watch=machine_watch, slot_guard=slot_guard,
        )
        results.extend(pass_results)
        if any(r.status == QUEUED for r in pass_results):
            # The machine is not ready: everything still owed waits in the
            # queue — another pass now would only find the same.
            break
        # SF-1: a row written WHILE we ran (install.py's second enqueue) is
        # picked up here — this driver holds the lock, and the driver spawned
        # for that row is blocked by it.
        if explicit or not [
            cid for cid in owed_condition_ids(folder) if cid not in attempted
        ]:
            break
    return results


def _dispatch_pass(
    folder: Path,
    *,
    cids: "Sequence[str]",
    backend_probe: Optional[Callable[[Path, str], Optional[bool]]],
    runner: Optional[Callable[[Sequence[str], Path], int]],
    python: str,
    machine_watch: Optional[Callable[[], Any]] = None,
    slot_guard: Optional[Callable[[bool], Any]] = None,
) -> list[RetryResult]:
    """One ledger pass: every JOB of *cids*, in order (see :func:`dispatch`)."""
    import contextlib

    results: list[RetryResult] = []
    #: backend kind → tri-state, probed at most once per kind per pass.
    probed: dict[str, Optional[bool]] = {}
    for cid in cids:
        if handler_name_for(cid) is None:
            # Only reachable when the CALLER named the cids: owed_condition_ids
            # already filters on this. Trail it rather than `continue`-ing in
            # silence — an unexplained absence from the results is the exact
            # thing the empty-log defect taught us not to ship.
            trail(f"{cid}: not retryable (registry declares no retry_action) — skipping")
    ran_jobs: "set[str]" = set()
    for name, group in plan_jobs(folder, cids):
        handler = HANDLERS.get(name)
        if handler is None:
            results.extend(
                RetryResult(cid, SKIPPED, f"registry names unknown handler {name!r}")
                for cid in group
            )
            continue
        live: list[str] = []
        for cid in group:
            if (name == _WHOLE_TREE_JOB and "ollama_models" in ran_jobs
                    and condition_cleared(folder, cid) is True):
                # The Ollama retry ran the seed earlier in this pass: no
                # second whole-tree run, no attempt.
                trail(f"{cid}: already cleared by an earlier job of this pass")
                results.append(RetryResult(cid, SKIPPED, "cleared by an earlier job of this pass"))
                continue
            attempts = attempt_count(folder, cid)
            trail(f"{cid}: job `{name}` (needs {handler.backend} backend), "
                  f"{attempts}/{MAX_ATTEMPTS} attempt(s) so far")
            if attempts >= MAX_ATTEMPTS:
                results.append(
                    RetryResult(cid, SKIPPED, f"attempt cap ({MAX_ATTEMPTS}) reached")
                )
                continue
            live.append(cid)
        if not live:
            continue
        ctx = RetryContext(
            folder=folder,
            condition_id=live[0],
            backend_probe=backend_probe,
            runner=runner,
            python=python,
        )
        if handler.backend not in probed:
            probed[handler.backend] = ctx.backend_available(handler.backend)
        backend = probed[handler.backend]
        trail(
            f"{', '.join(live)}: {handler.backend} backend gate → "
            + {True: "reachable", False: "provably down", None: "unknown"}[backend]
        )
        if backend is not True:
            # The detail is the ONE home's sentence (`backend_block_detail`),
            # because `retry_disposition_note` classifies the row back into a
            # cause from it — a second copy here would drift and silently
            # revert the note to the wrong cause.
            detail = backend_block_detail(handler.backend, backend)
            for cid in live:
                # v0.2.92 (WFT C7): record the BLOCK durably; a distinct
                # status keeps the cap untouched — being blocked is not an
                # attempt (`test_skips_do_not_burn_the_cap`).
                record_attempt(folder, RetryResult(cid, BLOCKED, detail))
                results.append(RetryResult(cid, SKIPPED, detail))
            continue
        # v0.2.96 (WP-3, register issue 8): IMAGE-freshness gate for handlers
        # whose work embeds through the code-embed service. A service built
        # from stale source answers the reachability gate and then silently
        # truncates over-window input at HTTP 200; three INCONCLUSIVE passes
        # would stop the retry FOREVER. So a stale verdict BLOCKS here, BEFORE
        # the STARTED row (being blocked is not an attempt), and the retry
        # resumes by itself once the verdict turns current. ``unknown`` and
        # ``current`` proceed — this gate must never be what skips owed work.
        if handler.needs_current_code_embed_image:
            verdict = _code_embed_image_verdict(folder)
            trail(f"{', '.join(live)}: code-embed image gate → {verdict}")
            if verdict == _IMAGE_STALE:
                for cid in live:
                    record_attempt(folder, RetryResult(cid, BLOCKED, BLOCKED_BY_IMAGE_DETAIL))
                    results.append(RetryResult(cid, SKIPPED, BLOCKED_BY_IMAGE_DETAIL))
                continue
        # v0.2.107 (A-003): the machine queue's readiness gate. Not ready ⇒
        # the job waits in the queue: no row, no attempt.
        before = None
        if machine_watch is not None and handler.needs_weaviate:
            before = _watch(machine_watch)
            if getattr(before, "ready", None) is not True:
                trail(f"{', '.join(live)}: {WEAVIATE_NOT_READY_DETAIL}")
                results.extend(RetryResult(cid, QUEUED, WEAVIATE_NOT_READY_DETAIL)
                               for cid in live)
                continue
        # Recorded BEFORE the job runs: a crash (or a worker that dies) must
        # still consume its attempt, or the cap can never engage on a handler
        # that always dies. One attempt id for the whole job — the env the
        # child's clears copy is one value.
        attempt = _new_attempt_id()
        for cid in live:
            record_attempt(
                folder, RetryResult(cid, STARTED, f"handler {name}"), {"attempt": attempt},
            )
        # S2 heartbeat: the staleness bound covers ONE handler's silence, so
        # a pass of several hours-long seeds never ages this driver out.
        _heartbeat_lock(folder)
        guard = slot_guard(handler.own_slot) if slot_guard is not None else contextlib.nullcontext()
        with guard:
            result = _run_handler_in_attempt(folder, live, handler, ctx, attempt)
        ran_jobs.add(name)
        machine_failed = False
        if before is not None:
            after = _watch(machine_watch)
            try:
                from vco_lib.owed_work import machine_failed_between

                machine_failed = after is not None and machine_failed_between(before, after)
            except Exception:  # noqa: BLE001 — no evidence ⇒ no excuse
                machine_failed = False
        for cid in live:
            verdict = _verdict(folder, cid, result, machine_failed, attempt,
                               primary=cid == live[0])
            record_attempt(folder, verdict,
                           {"attempt": attempt} if verdict.status == INTERRUPTED else None)
            if verdict.status == RETRIED:
                _record_resolution(folder, cid, verdict.detail)
            results.append(verdict)
    return results


def _watch(machine_watch: Optional[Callable[[], Any]]) -> Any:
    try:
        return machine_watch() if machine_watch is not None else None
    except Exception:  # noqa: BLE001 — a probe that blew up is "unknown"
        return None


def _verdict(folder: Path, cid: str, result: RetryResult, machine_failed: bool,
             attempt: str, *, primary: bool = True) -> RetryResult:
    """The verdict row for ``cid`` after its job ran. The ledger re-read is the
    ground truth: the job's PRIMARY condition is RETRIED when the job
    succeeded and its clear removed it (a job that exited 0 without clearing
    is INCONCLUSIVE); a condition the job ABSORBED is RETRIED whenever its
    clear happened — a whole-tree sync clears the backfill row and exits 1
    for one failed node. One that did not clear while the machine provably
    failed is INTERRUPTED (excused, at most
    :data:`MAX_EXCUSED_INTERRUPTIONS` times per episode)."""
    cleared = condition_cleared(folder, cid)
    trail(
        f"{cid}: ledger re-read → "
        + {
            True: "the child's own clear removed the condition",
            False: "the condition is STILL in the ledger",
            None: "the ledger could not be re-read",
        }[cleared]
    )
    if cleared is True and (result.status == RETRIED or not primary):
        return RetryResult(cid, RETRIED, result.detail)
    if machine_failed and cleared is not True:
        excused = retry_history(folder, cid).interrupted
        if excused < MAX_EXCUSED_INTERRUPTIONS:
            trail(f"{cid}: Weaviate failed during the job (attempt {attempt}) — "
                  "the attempt is excused and the job stays queued")
            return RetryResult(
                cid, INTERRUPTED,
                f"{result.detail}; Weaviate failed during the job — attempt excused",
            )
        trail(f"{cid}: Weaviate failed during the job again — {excused} attempt(s) "
              "already excused this episode, so this one counts")
    if result.status == RETRIED:
        return RetryResult(
            cid, INCONCLUSIVE,
            f"{result.detail}; the condition is STILL in the ledger"
            if cleared is False
            else f"{result.detail}; the ledger could not be re-read",
        )
    return RetryResult(cid, result.status, result.detail)


def _run_handler_in_attempt(
    folder: Path, cids: "Sequence[str]", handler: Handler, ctx: RetryContext, attempt: str,
) -> RetryResult:
    """Run ``handler`` with :data:`ATTEMPT_ENV` naming ``attempt``.

    The env pair is what a child's clear copies onto its :data:`RESOLVED` row
    (:func:`record_episode_end`); it is restored afterwards so nothing outside
    the attempt inherits it. A handler that RAISES still gets a verdict row for
    every condition of its job (R1 M1): an exception must not leave an attempt
    open, or a later external clear would be read as the attempt's own. The
    exception is then re-raised — the dispatcher does not swallow a crash it
    cannot judge.
    """
    try:
        from vco_lib.deferral_probes import process_start_token

        token = process_start_token(os.getpid()) or ""
    except Exception:  # noqa: BLE001 — no token ⇒ liveness by pid only
        token = ""
    keys = (ATTEMPT_ENV, ATTEMPT_DRIVER_TOKEN_ENV)
    prev = {k: os.environ.get(k) for k in keys}
    os.environ[ATTEMPT_ENV] = attempt
    os.environ[ATTEMPT_DRIVER_TOKEN_ENV] = token
    try:
        return handler.run(ctx)
    except BaseException as exc:
        for cid in cids:
            record_attempt(
                folder,
                RetryResult(cid, FAILED, f"handler raised {type(exc).__name__}: {exc}"),
            )
        raise
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def detached_log_glob() -> Optional[str]:
    """The glob the owed-work logs match, for install output.

    v0.2.101 item 4: the install tells the user the seed continues in the
    background and where to watch it. v0.2.107: the work runs in the machine's
    one owed-work worker (``vco_lib.owed_work``), whose logs are
    ``<vct_root_dir>/logs/owed-work-*.log`` — one per job, named after the
    project key and the worker pid. ``None`` when the logs dir cannot be
    resolved.
    """
    from vco_lib.owed_work import log_glob

    return log_glob()


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
# The hand-off to the machine queue — the session-start trigger
# ---------------------------------------------------------------------------


def spawn_detached(
    folder: Path, *, python: str = "", extra_env: Optional[Mapping[str, str]] = None
) -> bool:
    """Hand ``folder``'s owed work to the machine's owed-work queue.

    v0.2.107 (A-003): this used to start one detached
    ``python -m vco_lib.deferral_retry --folder <folder>`` driver per call — per
    project, per session start, ungated. It now ENQUEUES the project
    (:func:`vco_lib.owed_work.enqueue`, one entry per project however often it
    is asked) and makes sure the machine's ONE worker runs
    (:func:`vco_lib.owed_work.ensure_worker`), which works the queue one job
    per free slot of the bulk-writer pool and only while Weaviate is ready.
    The name is kept: install.py and the session-start trigger call it.

    *extra_env* (v0.2.101 item 4 — install.py's embedding pair and the context
    triple a proven seed stamps) is stored WITH the project's entry and
    overlaid on the worker's environment only while that project's jobs run;
    the worker's own environment is the caller's minus the keys that name one
    project's KG target (``owed_work._WORKER_ENV_STRIP``), because it serves
    every project.

    Returns True when the work is queued and a worker is running or on its
    way; False when the queue could not be written or no worker could be
    started — install.py then runs the seed in the foreground.
    """
    from vco_lib import owed_work

    try:
        owed_work.enqueue(Path(folder), env=extra_env)
    except Exception:  # noqa: BLE001 — cannot queue ⇒ the caller runs it
        return False
    return owed_work.ensure_worker(python=python, origin=f"enqueue {Path(folder).name}")


def session_start_owed_check(folder: Path, *, python: str = "") -> list[str]:
    """Session-start trigger: owed retryable cids, handed to the machine queue.

    ONE home for the check BOTH session-start hooks perform, so the bash and
    PowerShell siblings carry five lines of glue each instead of two copies of
    the logic (A-leg of the A>B>C rule — the hook already runs a Python
    interpreter, so there is no reason for the rule to live in shell).

    Contract:

    * Returns the owed condition ids (possibly empty). The caller prints them;
      this function never prints.
    * When something is owed and not every condition is capped, ENQUEUES the
      project in the machine's owed-work queue and makes sure the ONE worker
      runs (:func:`spawn_detached`, v0.2.107). It never starts a driver per
      project: three projects opening together are three queue entries and
      one worker. The worker — not the hook — gates on Weaviate and the
      embedding backend, so a session that starts before the containers are up
      leaves the work queued, with no attempt consumed.
    * Never raises: a SessionStart hook must not be able to break a session.

    v0.2.105 (prune lane): BEFORE reading the ledger, the lost-extras
    detection runs (``codegraph_extras_refresh.detect_and_emit_missing_
    extras``) — an enabled extra code folder with 0 rows of its own is
    emitted as owed work HERE, so the very same spawn can heal it. This is
    the session-start half of the detection the update path benefits from on
    the next session open; soft-fail, never a gate.
    """
    try:
        from vco_lib.codegraph_extras_refresh import (
            detect_and_emit_missing_extras,
        )

        detect_and_emit_missing_extras(str(Path(folder)))
    except Exception:  # noqa: BLE001 — detection must never block the check
        pass
    try:
        # v0.2.106 ("backfill once"): a secondary text-embedding slot enabled
        # since the last stamp is scheduled HERE (a file read of the project
        # env + its stamp; no Weaviate call), so this same spawn backfills it.
        from vco_lib.kg_secondary_backfill import schedule_for_folder

        schedule_for_folder(Path(folder))
    except Exception:  # noqa: BLE001 — detection must never block the check
        pass
    try:
        # v0.2.107: a chunker re-sync entry whose remedy has provably run
        # (both halves recorded, or the project's forced walk completed while
        # its half went to another tree) is retired HERE — file reads only, no
        # new walk. The update's registry probe does the same on its surface.
        from vco_lib.chunker_revision import heal_stuck_resync

        heal_stuck_resync(Path(folder))
    except Exception:  # noqa: BLE001 — a heal must never block the check
        pass
    try:
        owed = owed_condition_ids(Path(folder))
    except Exception:  # noqa: BLE001
        return []
    if not owed:
        return []
    # A driver whose EVERY condition is at the attempt cap does nothing but
    # log "attempt cap reached" (the cap gate in `_dispatch_pass` records
    # nothing) — so do not start an interpreter and a log file for it at
    # every session start. The owed list is still returned: the hook prints
    # it. A trail that cannot be counted is not "capped" — spawn and let the
    # driver decide.
    try:
        all_capped = all(
            attempt_count(Path(folder), cid) >= MAX_ATTEMPTS for cid in owed
        )
    except Exception:  # noqa: BLE001 — could-not-count ⇒ spawn
        all_capped = False
    if all_capped:
        return owed
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
    "BLOCKED",
    "BLOCKED_BY_IMAGE_DETAIL",
    "BLOCKED_NOTE_THRESHOLD",
    "BLOCK_CAUSE_BACKEND",
    "BLOCK_CAUSE_IMAGE",
    "BLOCK_CAUSE_UNKNOWN",
    "CODE_BACKEND",
    "FAILED",
    "HANDLERS",
    "INCONCLUSIVE",
    "INTERRUPTED",
    "MAX_ATTEMPTS",
    "MAX_EXCUSED_INTERRUPTIONS",
    "PIDFILE_NAME",
    "PIDFILE_STALE_SECONDS",
    "QUEUED",
    "RESOLVED",
    "RETRIED",
    "SKIPPED",
    "STARTED",
    "TEXT_BACKEND",
    "Handler",
    "RetryContext",
    "RetryHistory",
    "RetryResult",
    "attempt_count",
    "attempts_path",
    "backend_block_detail",
    "classify_block_detail",
    "condition_cleared",
    "default_backend_probe",
    "default_runner",
    "dispatch",
    "handler_name_for",
    "job_for_condition",
    "main",
    "owed_condition_ids",
    "pidfile_path",
    "plan_jobs",
    "record_attempt",
    "record_episode_end",
    "retry_disposition_note",
    "retry_history",
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
