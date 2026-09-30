# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ollama readiness + verified model pulls (v0.2.100 AD-6, L1-F14).

Moved out of ``install.py`` (``_wait_for_ollama`` / ``_pull_ollama_models``,
which keeps thin shims) and made honest:

* :func:`wait_ready` either sees Ollama answer or raises
  :class:`OllamaNotReadyError` — it never prints ``TIMEOUT`` and lets the run
  carry on as if the models were there;
* :func:`pull` parses the ``/api/pull`` NDJSON stream and requires a final
  ``{"status": "success"}``; an in-stream ``{"error": ...}`` or a stream that
  just ends is a FAILURE (pre-0.2.100 any EOF counted as success);
* :func:`ensure` checks ``/api/tags`` BEFORE pulling (a present model is
  skipped with a ``present`` line — no 110-second re-pull on every update)
  and AFTER (a pull that "succeeded" but is not listed is a failure);
* a failed pull of an EMBEDDING model raises :class:`OllamaPullError`; the
  caller turns it into the ``ollama_model_pull_failed`` deferral and a clean
  non-zero exit — no traceback.

Ollama not answering at step 6 (owner answers, 2026-09-29 — :func:`install_step`):

1. restart Ollama's container ONLY when VCO owns it — the service row is
   ``vco_managed`` AND the container's real compose labels name VCO's own
   project (:func:`restart_owned_ollama`: ``restart`` by name, never a
   recreate); an adopted or foreign Ollama is reported, never touched;
2. a bounded re-wait;
3. still down → the update CONTINUES: model pulls and the KG seed are skipped
   and recorded as the ``auto_retryable`` ``ollama_not_ready_at_update`` row,
   whose retry (:func:`retry_owed_model_work`, the ``ollama_models`` handler of
   :mod:`vco_lib.deferral_retry`) completes both once Ollama answers. The run
   exits 0 unless something else failed.

The base URL is always the caller's — the ``service_endpoints`` row (an
adopted Ollama on another port included), never an environment default.
HTTP goes through a tiny injectable layer so tests never reach a real Ollama.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence

from vco_lib.embedding_pull_plan import PullPlan, code_embed_unavailable_entry

NOT_READY_CID = "ollama_not_ready_at_update"
PULL_FAILED_CID = "ollama_model_pull_failed"

_PROBE_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)

LogEvent = Callable[..., Any]


class OllamaStepError(RuntimeError):
    """Base of the two typed step-6/7 failures."""

    def deferral_entry(self) -> Any:  # pragma: no cover — overridden
        raise NotImplementedError


class OllamaNotReadyError(OllamaStepError):
    def __init__(self, url: str, timeout_s: float) -> None:
        super().__init__(f"Ollama did not answer at {url} within {timeout_s:.0f}s")
        self.url = url
        self.timeout_s = timeout_s

    def deferral_entry(self) -> Any:
        from vco_lib.deferral_report import DeferralEntry

        return DeferralEntry(
            condition_id=NOT_READY_CID,
            title="Ollama did not answer; model setup not done",
            detected=str(self),
            why_deferred=(
                "The models VCO uses could not be checked or pulled, and the knowledge-graph "
                "seed was not run, because Ollama was not answering. Nothing was assumed "
                "present. The rest of the update completed; VCO retries the model pulls and "
                "the seed by itself once Ollama answers."
            ),
            command_to_apply=(
                f"Nothing to do if Ollama comes back. Otherwise check the Ollama container "
                f"(`podman logs vco_ollama` / `docker logs vco_ollama`) and that {self.url} "
                "answers; to run the owed retry now (from the install root):\n"
                "python -m vco_lib.deferral_retry --folder ."
            ),
            severity="warning",
        )


class OllamaPullError(OllamaStepError):
    """One or more EMBEDDING models could not be pulled / verified."""

    def __init__(self, failed: Mapping[str, str], base_url: str) -> None:
        self.failed = dict(failed)
        self.base_url = base_url
        super().__init__(
            "Load-bearing embedding model pull(s) failed: "
            + "; ".join(f"{m} ({why})" for m, why in self.failed.items())
            + ". The Knowledge Graph cannot work without them."
        )

    def deferral_entry(self) -> Any:
        return pull_failed_entry(self.failed, self.base_url)


def pull_failed_entry(failed: Mapping[str, str], base_url: str) -> Any:
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=PULL_FAILED_CID,
        title="Ollama model pull failed",
        detected="; ".join(f"{m}: {why}" for m, why in failed.items()),
        why_deferred="A model the configuration uses is not present in Ollama.",
        command_to_apply="\n".join(
            [f"curl -X POST {base_url}/api/pull -d '{{\"name\": \"{m}\"}}'" for m in failed]
            + ["then re-run `python install.py --update`"]
        ),
        severity="warning",
    )


# ── HTTP layer (injectable) ─────────────────────────────────────────────────


class OllamaHttp(Protocol):
    def get_json(self, url: str, timeout: float) -> Any: ...

    def post_lines(self, url: str, payload: Mapping[str, Any], timeout: float) -> Iterable[bytes]: ...


class UrllibHttp:
    """The production layer: stdlib urllib, looked up at call time."""

    def get_json(self, url: str, timeout: float) -> Any:
        resp = urllib.request.urlopen(url, timeout=timeout)
        try:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise OSError(f"HTTP {status} from {url}")
            return json.loads(resp.read() or b"null")
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    def post_lines(self, url: str, payload: Mapping[str, Any], timeout: float) -> Iterator[bytes]:
        req = urllib.request.Request(
            url, data=json.dumps(dict(payload)).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            yield from resp
        finally:
            resp.close()


def _http(http: Optional[OllamaHttp]) -> OllamaHttp:
    return http if http is not None else UrllibHttp()


def _norm(model: str) -> str:
    """Ollama lists an untagged model as ``<name>:latest``."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


# ── readiness ───────────────────────────────────────────────────────────────


def wait_ready(
    base_url: str,
    *,
    timeout_s: float = 120.0,
    poll_s: float = 2.0,
    http: Optional[OllamaHttp] = None,
) -> None:
    """Return once ``GET /api/tags`` answers; raise :class:`OllamaNotReadyError`
    after ``timeout_s`` (bounded — a dead WSL2 port-forward cannot hang it)."""
    h = _http(http)
    url = f"{base_url.rstrip('/')}/api/tags"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            h.get_json(url, 3.0)
            return
        except _PROBE_ERRORS:
            pass
        if time.monotonic() >= deadline:
            raise OllamaNotReadyError(url, timeout_s)
        time.sleep(poll_s)


# ── presence + pull ─────────────────────────────────────────────────────────


def list_present(base_url: str, *, http: Optional[OllamaHttp] = None) -> set[str]:
    body = _http(http).get_json(f"{base_url.rstrip('/')}/api/tags", 10.0)
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        raise ValueError(f"unexpected /api/tags body from {base_url}")
    names: set[str] = set()
    for m in models:
        if isinstance(m, dict):
            for key in ("name", "model"):
                if isinstance(m.get(key), str):
                    names.add(_norm(m[key]))
    return names


def verify_present(
    base_url: str, models: Sequence[str], *, http: Optional[OllamaHttp] = None
) -> tuple[list[str], list[str]]:
    """``(present, missing)`` for ``models`` against ``/api/tags``."""
    have = list_present(base_url, http=http)
    present = [m for m in models if _norm(m) in have]
    return present, [m for m in models if _norm(m) not in have]


class PullFailed(RuntimeError):
    pass


def pull(base_url: str, model: str, *, http: Optional[OllamaHttp] = None,
         timeout_s: float = 600.0) -> None:
    """Pull one model; return only on a final ``status: success``."""
    last = ""
    try:
        for raw in _http(http).post_lines(
            f"{base_url.rstrip('/')}/api/pull", {"name": model}, timeout_s
        ):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                if obj.get("error"):
                    raise PullFailed(str(obj["error"]))
                last = str(obj.get("status") or last)
    except _PROBE_ERRORS as exc:
        raise PullFailed(str(exc)) from exc
    if last != "success":
        raise PullFailed(f"stream ended without success (last status: {last or 'none'})")


@dataclass
class EnsureResult:
    present: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def ensure(
    base_url: str,
    models: Sequence[str],
    *,
    load_bearing: Iterable[str] = (),
    http: Optional[OllamaHttp] = None,
    out: Callable[[str], None] = print,
    skip_present: bool = True,
) -> EnsureResult:
    """Make ``models`` present: skip what ``/api/tags`` lists, pull the rest,
    verify afterwards. Raises :class:`OllamaPullError` (after trying every
    model) when a ``load_bearing`` one failed; other failures are returned.
    ``skip_present=False`` (``install.py --no-resume``) pulls every model."""
    base = base_url.rstrip("/")
    res = EnsureResult()
    try:
        present, missing = verify_present(base, models, http=http)
    except _PROBE_ERRORS as exc:
        present, missing = [], list(models)
        out(f"  (could not list present models: {exc}; pulling all)")
    if not skip_present:
        present, missing = [], list(models)
    for m in present:
        out(f"  {m} ... present")
    res.present = present
    for m in missing:
        out(f"  Pulling {m} ... ")
        try:
            pull(base, m, http=http)
            res.pulled.append(m)
            out(f"  {m} ... OK")
        except PullFailed as exc:
            res.failed[m] = str(exc)
            out(f"  {m} ... FAILED ({exc})")
    if res.pulled:
        try:
            _p, unlisted = verify_present(base, res.pulled, http=http)
        except _PROBE_ERRORS as exc:
            unlisted = []
            out(f"  (post-pull /api/tags check failed: {exc})")
        for m in unlisted:
            res.pulled.remove(m)
            res.failed[m] = "pull reported success but /api/tags does not list it"
    lb = {_norm(m) for m in load_bearing}
    bad = {m: why for m, why in res.failed.items() if _norm(m) in lb}
    if bad:
        raise OllamaPullError(bad, base)
    return res


# ── install.py steps 6 / 7 ──────────────────────────────────────────────────


def wait_ready_step(base_url: str, *, timeout_s: float, log_event: LogEvent,
                    recover: Optional[Callable[[], "tuple[str, str]"]] = None,
                    rewait_s: float = 60.0,
                    http: Optional[OllamaHttp] = None) -> Optional[OllamaNotReadyError]:
    """install.py step 6: ``None`` once Ollama answers, else the typed error.

    Never raises for "not ready": the caller decides (install.py continues).
    ``recover`` is tried once on the first timeout — it returns
    ``(outcome, detail)`` from :func:`restart_owned_ollama`; only a
    ``restarted`` outcome earns the bounded re-wait."""
    print("[6/10] Waiting for Ollama ... ", end="", flush=True)
    log_event("6/10", "start", "waiting for Ollama")
    try:
        wait_ready(base_url, timeout_s=timeout_s, http=http)
    except OllamaNotReadyError as exc:
        err: Optional[OllamaNotReadyError] = exc
        print("NOT READY")
        outcome, detail = recover() if recover is not None else ("none", "no recovery available")
        print(f"  Ollama recovery: {detail}")
        log_event("6/10", "recover", detail, data={"outcome": outcome})
        if outcome == RESTARTED:
            print("  Waiting for Ollama again ... ", end="", flush=True)
            try:
                wait_ready(base_url, timeout_s=rewait_s, http=http)
                err = None
            except OllamaNotReadyError as again:
                err = again
                print("NOT READY")
        if err is not None:
            log_event("6/10", "error", str(err), data={"url": err.url, "timeout_s": timeout_s,
                                                        "recovery": outcome})
            return err
    print("OK")
    log_event("6/10", "ok", "Ollama is ready", data={"url": base_url})
    return None


def ensure_plan_step(plan: PullPlan, urls: Mapping[str, Any], report: Any,
                     *, log_event: LogEvent, resume: bool = True,
                     http: Optional[OllamaHttp] = None) -> EnsureResult:
    """install.py step 7: ensure the plan's models, report non-fatal failures +
    a down code backend into the run's deferral report; raise
    :class:`OllamaPullError` for embeddings. With ``resume`` (the default) a
    plan whose every model ``/api/tags`` lists prints "verified, skipped"."""
    print("[7/10] Ollama models (exactly the ones in use) ... ", flush=True)
    for why in plan.rationale:
        print(f"    - {why}")
    log_event("7/10", "start", f"ensuring {len(plan.models)} Ollama model(s)",
              data={"models": list(plan.models)})
    base = str(urls["ollama_url"]).rstrip("/")
    try:
        res = ensure(base, plan.models, load_bearing=plan.embedding, skip_present=resume,
                     http=http)
    except OllamaPullError as exc:
        log_event("7/10", "error", str(exc), data={"failed": exc.failed})
        raise
    if res.failed and report is not None:
        report.add_entry(pull_failed_entry(res.failed, base))
    if plan.code_backend_unavailable and report is not None:
        report.add_entry(code_embed_unavailable_entry(
            f"code_embed at {urls.get('code_embed_url')} did not answer /health"))
    if resume and plan.models and len(res.present) == len(plan.models):
        print(f"[7/10] Ollama models: verified, skipped ({len(res.present)} models present)")
    log_event("7/10", "warn" if res.failed else "ok",
              f"present={len(res.present)} pulled={len(res.pulled)} failed={len(res.failed)}",
              data={"failed": res.failed})
    return res


@dataclass
class StepOutcome:
    """install.py steps 6+7: ``rc`` non-None = exit with it; ``owed`` = Ollama
    was down, pulls + the KG seed are owed to the retry driver."""

    rc: Optional[int] = None
    owed: bool = False


def install_step(urls: Mapping[str, Any], report: Any, *, plan: Callable[[], PullPlan],
                 log_event: LogEvent, timeout_s: float, resume: bool = True,
                 recover: Optional[Callable[[], "tuple[str, str]"]] = None,
                 http: Optional[OllamaHttp] = None) -> StepOutcome:
    """install.py steps 6+7 (see the module docstring for the Ollama-down flow).

    Every failure lands in the run ``report`` — the run's exit-path flush
    (``install_deferral_flow.flush_on_exit``) writes it, whatever the exit."""
    down = wait_ready_step(str(urls["ollama_url"]).rstrip("/"), timeout_s=timeout_s,
                           log_event=log_event, recover=recover, http=http)
    if down is not None:
        if report is not None:
            report.add_entry(down.deferral_entry())
        print(f"  Continuing the update WITHOUT model pulls and the knowledge-graph seed; "
              f"recorded as {NOT_READY_CID} — VCO retries both once Ollama answers.")
        return StepOutcome(owed=True)
    try:
        ensure_plan_step(plan(), urls, report, log_event=log_event, resume=resume, http=http)
    except OllamaPullError as exc:
        if report is not None:
            report.add_entry(exc.deferral_entry())
        import sys

        print(f"\n  ERROR: {exc}\n  Recorded in UPDATE_DEFERRED.md ({PULL_FAILED_CID}).",
              file=sys.stderr)
        return StepOutcome(rc=1)
    return StepOutcome()


# ── recovery: restart an OWNED Ollama container (never adopted / foreign) ──

RESTARTED = "restarted"
NOT_OWNED = "not_owned"
NO_CONTAINER = "no_container"
RESTART_FAILED = "restart_failed"


def restart_owned_ollama(install_root: Path, runtime: str, row: Any, *,
                         run: Optional[Callable[..., Any]] = None,
                         find: Optional[Callable[..., Optional[str]]] = None,
                         identity: Optional[Callable[..., Any]] = None) -> "tuple[str, str]":
    """``(outcome, detail)``. Restarts ONLY a container VCO owns: the
    ``service_endpoints`` row is ``vco_managed`` AND the container's compose
    project label is VCO's own project (the same predicate the recreate guard
    uses, :func:`vco_lib.containers.foreign_compose_identity`). ``restart`` by
    name — never ``rm``, never a compose recreate. Anything else is reported."""
    import subprocess

    from vco_lib import containers as _c

    mode = getattr(row, "mode", None) if row is not None else "vco_managed"
    if mode != "vco_managed":
        who = getattr(row, "container_name", "") or getattr(row, "url", "") or "it"
        return NOT_OWNED, (f"Ollama is {mode} ({who}) — not VCO's container, so VCO does "
                           "not restart it; start it yourself")
    if not runtime:
        return NO_CONTAINER, "no container runtime detected — nothing VCO could restart"
    name = (find or _c.find_existing_container)("ollama", runtime=runtime)
    if not name:
        return NO_CONTAINER, "no Ollama container exists — nothing VCO could restart"
    why = _c.foreign_compose_identity(
        (identity or _c.compose_identity_of)(name, runtime),
        _c.own_compose_project(Path(install_root)))
    if why:
        return NOT_OWNED, f"container {name} {why} — not VCO's, left alone"
    try:
        res = (run or subprocess.run)([runtime, "restart", name], capture_output=True,
                                      text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RESTART_FAILED, f"`{runtime} restart {name}` could not run ({exc})"
    if getattr(res, "returncode", 1) != 0:
        tail = (getattr(res, "stderr", "") or "").strip().splitlines()[-1:]
        return RESTART_FAILED, (f"`{runtime} restart {name}` failed"
                                f"{': ' + tail[0] if tail else ''}")
    return RESTARTED, f"restarted VCO's own container {name}"


CODE_EMBED_READY = "ready"
CODE_EMBED_WARMING = "warming"
CODE_EMBED_DOWN = "down"


def _timed_out(exc: BaseException) -> bool:
    """A read that timed out: the port ACCEPTED the connection (the service is
    up and busy — CodeSage's first ``/health`` loads, or downloads, the model)."""
    seen: object = exc
    for _ in range(8):  # URLError.reason / __cause__ chain, bounded
        if isinstance(seen, TimeoutError):
            return True
        if not isinstance(seen, BaseException):
            return False  # e.g. a URLError whose reason is a plain string
        seen = getattr(seen, "reason", None) or seen.__cause__
    return False


def code_embed_container_running(runtime: str, *, run: Optional[Callable[..., Any]] = None,
                                 find: Optional[Callable[..., Optional[str]]] = None
                                 ) -> Optional[bool]:
    """Tri-state: is the code_embed container RUNNING? ``None`` = could not tell
    (no runtime, no such container, probe failed). Read-only."""
    import subprocess

    from vco_lib import containers as _c

    if not runtime:
        return None
    name = (find or _c.find_existing_container)("code_embed", runtime=runtime)
    if not name:
        return False
    try:
        res = (run or subprocess.run)([runtime, "inspect", "--type", "container", "--format",
                                       "{{.State.Running}}", name],
                                      capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return (res.stdout or "").strip().lower() == "true"


def code_embed_state(url: Optional[str], *, timeout_s: float = 60.0,
                     http: Optional[OllamaHttp] = None,
                     container_running: Optional[Callable[[], Optional[bool]]] = None
                     ) -> Optional[str]:
    """``READY`` / ``WARMING`` / ``DOWN`` for the code_embed service (``None``:
    no URL). v0.2.100 W3R-04: a service that accepts connections but has not
    answered ``/health`` within the bound is loading its model — a fresh GPU
    install downloads ~2.6 GB on the first request — and so is a container the
    runtime reports RUNNING; neither is an outage. ``DOWN`` only when the port
    never accepted AND the container is not provably running."""
    if not url:
        return None
    h = _http(http)
    deadline = time.monotonic() + timeout_s
    accepted = False
    while True:
        try:
            h.get_json(f"{url.rstrip('/')}/health", 3.0)
            return CODE_EMBED_READY
        except _PROBE_ERRORS + (TimeoutError,) as exc:
            accepted = accepted or _timed_out(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(2.0)
    if accepted:
        return CODE_EMBED_WARMING
    if container_running is not None and container_running() is True:
        return CODE_EMBED_WARMING
    return CODE_EMBED_DOWN


# ── launcher dual-flag toggle (python -m vco_lib.embedding_pull_plan ensure) ─


def ensure_for_machine(plan: PullPlan, root: Path, *, launcher_db: Optional[Path] = None,
                       wait_s: float = 30.0, quiet: bool = False,
                       http: Optional[OllamaHttp] = None) -> dict[str, Any]:
    """Ensure ``plan`` on this machine's Ollama (the service_endpoints row)."""
    from vco_lib import service_endpoints
    from vco_lib.deferral_emit import emit, resolve_conditions

    base = str(service_endpoints.machine_service_urls(launcher_db)["ollama_url"]).rstrip("/")
    say: Callable[[str], None] = (lambda _s: None) if quiet else print
    out: dict[str, Any] = {"ok": False, "ollama_url": base, "present": [], "pulled": [],
                           "failed": {}, "deferral": None, "error": None}
    try:
        wait_ready(base, timeout_s=wait_s, http=http)
        res = ensure(base, plan.models, load_bearing=plan.embedding, http=http, out=say)
    except OllamaStepError as exc:
        entry = exc.deferral_entry()
        emit(Path(root), entry)
        out.update(error=str(exc), deferral=entry.condition_id,
                   failed=getattr(exc, "failed", {}))
        return out
    out.update(present=res.present, pulled=res.pulled, failed=res.failed)
    if res.failed:
        emit(Path(root), pull_failed_entry(res.failed, base))
        out.update(deferral=PULL_FAILED_CID, error="some models could not be pulled")
        return out
    resolve_conditions(Path(root), (PULL_FAILED_CID, NOT_READY_CID))
    out["ok"] = True
    return out


# ── the retry of the owed work (deferral_retry handler ``ollama_models``) ────

DONE = "done"
BLOCKED = "blocked"
RETRY_FAILED = "failed"

def retry_owed_model_work(folder: Path, *, seed: Callable[[], bool],
                          launcher_db: Optional[Path] = None, wait_s: float = 15.0,
                          http: Optional[OllamaHttp] = None,
                          out: Callable[[str], None] = print) -> "tuple[str, str]":
    """Complete what a run that found Ollama down skipped: the machine's model
    pulls, then the KG seed. ``(DONE | BLOCKED | RETRY_FAILED, detail)``.

    Clears ``ollama_not_ready_at_update`` / ``ollama_model_pull_failed`` ONLY on
    proven success — every planned model listed by ``/api/tags`` AND ``seed()``
    True, which the caller answers with PROOF, not an exit code (the retry
    handler: the KG sync ran AND left no ``kg_sync_no_embedding_backend`` row).
    Ollama still down → ``BLOCKED``, rows untouched for the next retry."""
    from vco_lib import service_endpoints
    from vco_lib.deferral_emit import emit, resolve_conditions
    from vco_lib.embedding_pull_plan import PlanUnavailable, plan_from_machine

    folder = Path(folder)
    base = str(service_endpoints.machine_service_urls(launcher_db)["ollama_url"]).rstrip("/")
    try:
        wait_ready(base, timeout_s=wait_s, http=http)
    except OllamaNotReadyError as exc:
        return BLOCKED, f"{exc} — the entry stays for the next retry"
    try:
        pp = plan_from_machine(folder, launcher_db)
        res = ensure(base, pp.models, load_bearing=pp.embedding, http=http, out=out)
        missing = [] if res.failed else verify_present(base, pp.models, http=http)[1]
    except PlanUnavailable as exc:
        return RETRY_FAILED, str(exc)
    except OllamaPullError as exc:
        resolve_conditions(folder, (NOT_READY_CID,))
        emit(folder, exc.deferral_entry())
        return RETRY_FAILED, str(exc)
    except _PROBE_ERRORS as exc:
        return RETRY_FAILED, f"/api/tags could not be read after the pulls ({exc})"
    if res.failed or missing:
        failed = dict(res.failed) or {m: "not listed by /api/tags" for m in missing}
        resolve_conditions(folder, (NOT_READY_CID,))
        emit(folder, pull_failed_entry(failed, base))
        return RETRY_FAILED, "model(s) still missing: " + ", ".join(failed)
    if not seed():
        return RETRY_FAILED, "the knowledge-graph seed did not complete (not proven)"
    resolve_conditions(folder, (NOT_READY_CID, PULL_FAILED_CID))
    return DONE, f"{len(pp.models)} model(s) present, knowledge-graph seed completed"
