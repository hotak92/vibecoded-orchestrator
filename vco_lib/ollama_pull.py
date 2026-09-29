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
                "The models VCO uses could not be checked or pulled because Ollama was "
                "not answering. Nothing was assumed present."
            ),
            command_to_apply=(
                f"Check the Ollama container (`podman logs vco_ollama`) and that {self.url} "
                "answers, then re-run `python install.py --update`."
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
) -> EnsureResult:
    """Make ``models`` present: skip what ``/api/tags`` lists, pull the rest,
    verify afterwards. Raises :class:`OllamaPullError` (after trying every
    model) when a ``load_bearing`` one failed; other failures are returned."""
    base = base_url.rstrip("/")
    res = EnsureResult()
    try:
        present, missing = verify_present(base, models, http=http)
    except _PROBE_ERRORS as exc:
        present, missing = [], list(models)
        out(f"  (could not list present models: {exc}; pulling all)")
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


def wait_ready_step(base_url: str, *, timeout_s: float, log_event: LogEvent) -> None:
    """install.py step 6 (shim target of ``_wait_for_ollama``)."""
    print("[6/10] Waiting for Ollama ... ", end="", flush=True)
    log_event("6/10", "start", "waiting for Ollama")
    try:
        wait_ready(base_url, timeout_s=timeout_s)
    except OllamaNotReadyError as exc:
        print("NOT READY")
        log_event("6/10", "error", str(exc), data={"url": exc.url, "timeout_s": timeout_s})
        raise
    print("OK")
    log_event("6/10", "ok", "Ollama is ready", data={"url": base_url})


def ensure_plan_step(plan: PullPlan, urls: Mapping[str, Any], report: Any,
                     *, log_event: LogEvent) -> EnsureResult:
    """install.py step 7 (shim target of ``_pull_ollama_models``): ensure the
    plan's models, report non-fatal failures + a down code backend into the
    run's deferral report; raise :class:`OllamaPullError` for embeddings."""
    print("[7/10] Ollama models (exactly the ones in use) ... ", flush=True)
    for why in plan.rationale:
        print(f"    - {why}")
    log_event("7/10", "start", f"ensuring {len(plan.models)} Ollama model(s)",
              data={"models": list(plan.models)})
    base = str(urls["ollama_url"]).rstrip("/")
    try:
        res = ensure(base, plan.models, load_bearing=plan.embedding)
    except OllamaPullError as exc:
        log_event("7/10", "error", str(exc), data={"failed": exc.failed})
        raise
    if res.failed and report is not None:
        report.add_entry(pull_failed_entry(res.failed, base))
    if plan.code_backend_unavailable and report is not None:
        report.add_entry(code_embed_unavailable_entry(
            f"code_embed at {urls.get('code_embed_url')} did not answer /health"))
    log_event("7/10", "warn" if res.failed else "ok",
              f"present={len(res.present)} pulled={len(res.pulled)} failed={len(res.failed)}",
              data={"failed": res.failed})
    return res


def fail_step(exc: OllamaStepError, report: Any, folder: Path) -> int:
    """Clean exit for a typed step-6/7 failure: the deferral lands on disk
    NOW (the locked writer — this run returns before its final write) and in
    the run report; one stderr line, exit 1, no traceback."""
    import sys

    from vco_lib.deferral_emit import emit

    entry = exc.deferral_entry()
    if report is not None:
        report.add_entry(entry)
    emit(Path(folder), entry)
    print(f"\n  ERROR: {exc}\n  Recorded in UPDATE_DEFERRED.md ({entry.condition_id}).",
          file=sys.stderr)
    return 1


def code_embed_reachable(url: Optional[str], *, timeout_s: float = 60.0,
                         http: Optional[OllamaHttp] = None) -> Optional[bool]:
    """Bounded ``/health`` wait for the code_embed service (None: no URL)."""
    if not url:
        return None
    h = _http(http)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            h.get_json(f"{url.rstrip('/')}/health", 3.0)
            return True
        except _PROBE_ERRORS:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2.0)


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
