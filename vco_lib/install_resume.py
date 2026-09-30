# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py resume: skip a step only when it VERIFIES as done (v0.2.100, owner Q5).

The promise this makes true (review L1-F19): ``--no-resume``, ``_RESUME_STATE``
and ``_should_skip_step`` shipped for months with no reader — every re-run
after a failure started at step 1/10 and redid everything (2026-09-29: four
"Resume Update" runs each restarted at 1/10; the successful one spent 110 s in
7/10 re-pulling models already present).

The rule, per step: **the last session's log says the step completed AND a real
side-effect verifier passes → print "verified, skipped" and skip; anything else
→ run the step.** The log alone is never enough (a venv can be deleted, a
requirements file can change between runs); the verifier alone would make the
first run after a clean clone skip work nobody ever did.

What resumes, and what verifies it:

========  ================================  ===========================================
step      work skipped                      verifier
========  ================================  ===========================================
1/10      Python wheel probe + prereqs      same interpreter version as the run that
                                            passed the check
3/10      venv creation                     the venv interpreter RUNS and reports a
                                            version (always verified — creating over a
                                            live venv is never useful)
4/10      pip install + editable installs   recorded dependency fingerprint unchanged
                                            (requirements*, both pyprojects, dev flag),
                                            ``pip check`` clean, editable-import probe
                                            run isolated (``-I``) from a cwd outside
                                            the checkout: ``vco_lib`` resolves and
                                            loads from the checkout with no
                                            site-packages copy; weaviate_mcp
                                            submodules import
7/10      model pulls                       ``/api/tags`` lists every planned model
                                            (``vco_lib.ollama_pull.ensure``)
========  ================================  ===========================================

Deliberately NOT skipped: 2/10 system detection (it reconciles the container
runtime and starts its daemon — live side effects step 5 needs, which no log
can stand in for), the embedding profile (replayed from the recorded choice,
see ``_choose_embedding_config`` — printed as verified when it replays), 5/5b
(services/bundle are reconciled against live state every run), 6/10 (the
Ollama wait IS a probe).

``--no-resume`` turns every skip off (the steps above then run).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

#: A session older than this is not resumed (the pre-existing 24 h rule).
STALE_AFTER = timedelta(hours=24)

#: Files whose content decides whether step 4's pip work is still current.
DEPS_FINGERPRINT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "claude_mcp_servers/pyproject.toml",
)

Verifier = Callable[[Mapping[str, Any]], "tuple[bool, str]"]
LogEvent = Callable[..., Any]


@dataclass
class Session:
    """The last install.py session's per-step outcome, read from install.jsonl."""

    enabled: bool = True
    phases: dict = field(default_factory=dict)
    #: step → ``data`` of the latest ok/skip event of that step
    data: dict = field(default_factory=dict)
    #: the session's recorded install choices: name → data (``step="choices"``)
    choices: dict = field(default_factory=dict)
    #: True once read from the log (main() snapshots it BEFORE this run logs its
    #: own session marker — read later, "the latest session" is this very run)
    loaded: bool = False

    def completed(self, step: str) -> bool:
        return self.enabled and self.phases.get(step) in ("ok", "skip")


def read_events(path: Optional[Path]) -> list:
    """Every JSON-object line of ``install.jsonl`` (malformed lines skipped)."""
    if path is None or not Path(path).is_file():
        return []
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def latest_session(events: list, *, now: Optional[datetime] = None) -> list:
    """The events of the most recent install.py session, ``[]`` when there is
    none or it is stale (>24 h, or an unparseable start timestamp — never
    resume on a malformed log). A session begins at ``actor=install.py,
    step=1/10, phase=start``."""
    start = -1
    for i, ev in enumerate(events):
        if (ev.get("actor") == "install.py" and ev.get("step") == "1/10"
                and ev.get("phase") == "start"):
            start = i
    if start < 0:
        return []
    session = events[start:]
    ts = session[0].get("ts", "")
    if ts:
        try:
            start_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return []
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) - start_dt > STALE_AFTER:
            return []
    return session


def load_session(path: Optional[Path], *, enabled: bool = True,
                 now: Optional[datetime] = None) -> Session:
    """Parse ``install.jsonl`` → the most recent install.py session (<24 h).

    Only install.py's own events are read; the latest phase per step wins, so
    a later ``error`` demotes an earlier ``ok``. ``enabled=False``
    (``--no-resume``) reads nothing: no skips, no replayed choices.
    """
    out = Session(enabled=enabled, loaded=True)
    if not enabled:
        return out
    for ev in latest_session(read_events(path), now=now):
        step, phase = ev.get("step"), ev.get("phase")
        if not isinstance(step, str) or not isinstance(phase, str):
            continue
        if ev.get("actor") != "install.py":
            continue
        data = ev.get("data")
        if step == "choices":
            name = ev.get("detail")
            if phase == "ok" and isinstance(name, str) and name and isinstance(data, dict):
                out.choices[name] = data
            continue
        out.phases[step] = phase
        if phase in ("ok", "skip") and isinstance(data, dict):
            out.data[step] = data
    return out


def verified_skip(session: Session, step: str, label: str, verify: Verifier, *,
                  log_event: LogEvent, out: Callable[[str], None] = print) -> bool:
    """True → the caller skips ``step``. Logs a ``start`` (the session marker
    for step 1/10 must exist even when the step is skipped) and, on a skip, a
    ``skip`` event carrying the recorded data forward so the NEXT run can
    verify against the same evidence."""
    if not session.completed(step):
        return False
    recorded = session.data.get(step, {})
    try:
        ok, detail = verify(recorded)
    except Exception as exc:  # noqa: BLE001 — a verifier that crashed did not verify
        ok, detail = False, f"verifier raised {type(exc).__name__}: {exc}"
    if not ok:
        out(f"[{step}] {label}: last run completed it, but {detail} — running it again")
        return False
    log_event(step, "start", f"{label}: resume check")
    out(f"[{step}] {label}: verified, skipped ({detail})")
    log_event(step, "skip", f"{label}: verified, skipped ({detail})",
              data={**dict(recorded), "resume_verified": detail})
    return True


# ── verifiers ───────────────────────────────────────────────────────────────


def python_verifier(version: Optional[str] = None) -> Verifier:
    """Step 1/10: the interpreter is the one whose version passed the check."""
    v = sys.version_info
    mine = version or f"{v.major}.{v.minor}.{v.micro}"

    def verify(recorded: Mapping[str, Any]) -> "tuple[bool, str]":
        was = str(recorded.get("version") or "")
        if was != mine:
            return False, f"the interpreter changed ({was or 'unrecorded'} → {mine})"
        return True, f"Python {mine} unchanged"

    return verify


def venv_python_runs(venv_python: Path, *, run: Callable[..., Any] = subprocess.run
                     ) -> "tuple[bool, str]":
    """Step 3/10: the venv interpreter exists AND runs."""
    if not Path(venv_python).exists():
        return False, f"{venv_python} is missing"
    try:
        res = run([str(venv_python), "-c",
                   "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{venv_python} does not run ({exc})"
    if getattr(res, "returncode", 1) != 0:
        return False, f"{venv_python} exited {res.returncode}"
    return True, f"venv Python {(res.stdout or '').strip() or '?'} runs"


def deps_fingerprint(root: Path, *, dev: bool) -> dict:
    """What step 4/10 installed FROM — recorded on its final ``ok`` event."""
    files = {}
    for rel in DEPS_FINGERPRINT_FILES:
        path = Path(root) / rel
        try:
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            files[rel] = None
    return {"deps_fingerprint": {"dev": bool(dev), "files": files}}


#: The editable-import probe. It REPORTS where ``vco_lib`` resolves (as one
#: JSON line) and :func:`vco_lib_resolution_verdict` decides — the decision
#: stays in this process, where it is unit-testable. ``find_spec`` names the
#: file and every package directory the import system would serve ``vco_lib``
#: from; the import then proves the package actually loads from there.
#: A frozen copy in site-packages is the v0.2.92 shadow-copy breakage.
_EDITABLE_PROBE = (
    "import sys, json, importlib, importlib.util\n"
    "out = {'origin': None, 'locations': [], 'file': None, 'error': None}\n"
    "try:\n"
    "    spec = importlib.util.find_spec('vco_lib')\n"
    "    if spec is not None:\n"
    "        out['origin'] = spec.origin\n"
    "        out['locations'] = list(spec.submodule_search_locations or [])\n"
    "        out['file'] = getattr(importlib.import_module('vco_lib'), '__file__', None)\n"
    "except Exception as exc:\n"
    "    out['error'] = '%s: %s' % (type(exc).__name__, exc)\n"
    "print(json.dumps(out))\n"
)

#: Directory names that mark an installed (non-editable) copy.
_SITE_DIRS = frozenset({"site-packages", "dist-packages"})


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def vco_lib_resolution_verdict(probe_stdout: str, root: Path) -> "tuple[bool, str]":
    """Decide from the probe's report whether ``vco_lib`` is served by the
    checkout: its origin, its loaded ``__file__`` and EVERY package location
    must lie under ``root``, and none may be a site-packages directory."""
    lines = (probe_stdout or "").strip().splitlines()
    try:
        rep = json.loads(lines[-1]) if lines else None
    except ValueError:
        rep = None
    if not isinstance(rep, dict):
        return False, "the vco_lib editable-import probe printed no report"
    if rep.get("error"):
        return False, f"vco_lib does not import ({rep['error']})"
    places = [p for p in [rep.get("origin"), rep.get("file"), *(rep.get("locations") or [])]
              if p]
    if not rep.get("origin") and not rep.get("locations"):
        return False, "vco_lib is not installed in the venv (no editable install)"
    root = Path(root).resolve()
    for raw in places:
        path = Path(str(raw)).resolve()
        if _SITE_DIRS.intersection(path.parts) and not _under(path, root):
            return False, f"vco_lib resolves from a site-packages copy ({path}), not the checkout"
        if not _under(path, root):
            return False, f"vco_lib resolves outside the checkout ({path})"
    return True, ""


def deps_verifier(root: Path, venv_python: Path, *, dev: bool,
                  weaviate_mcp_probe: str,
                  run: Callable[..., Any] = subprocess.run) -> Verifier:
    """Step 4/10: fingerprint unchanged + ``pip check`` + editable imports.

    Every probe runs ISOLATED (``python -I``: no cwd / script dir on
    ``sys.path``, no ``PYTHONPATH``, no user site) from an EMPTY temporary
    cwd outside the checkout — so it sees only what the venv itself serves,
    exactly as an MCP subprocess whose cwd is some other project folder does.
    Run from ``cwd=root`` instead, ``import vco_lib`` would resolve from the
    cwd whether the editable install exists or a shadow copy wins, and the
    probe could never fail (review W4R-02)."""

    def _ok(argv: list, what: str, cwd: str) -> "tuple[bool, str, str]":
        try:
            res = run(argv, capture_output=True, text=True, timeout=300, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{what} could not run ({exc})", ""
        if getattr(res, "returncode", 1) != 0:
            tail = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()[-1:]
            return False, f"{what} failed{': ' + tail[0] if tail else ''}", ""
        return True, "", res.stdout or ""

    def verify(recorded: Mapping[str, Any]) -> "tuple[bool, str]":
        want = deps_fingerprint(root, dev=dev)["deps_fingerprint"]
        if recorded.get("deps_fingerprint") != want:
            return False, "the dependency files (or --dev) changed since"
        py = str(venv_python)
        with tempfile.TemporaryDirectory(prefix="vco-resume-probe-") as cwd:
            probes: "tuple[tuple[list, str, Optional[Callable[[str], tuple[bool, str]]]], ...]" = (
                ([py, "-I", "-m", "pip", "check"], "`pip check`", None),
                ([py, "-I", "-c", _EDITABLE_PROBE], "the vco_lib editable-import probe",
                 lambda out: vco_lib_resolution_verdict(out, root)),
                ([py, "-I", "-c", weaviate_mcp_probe], "the weaviate_mcp import probe", None),
            )
            for argv, what, judge in probes:
                ok, why, stdout = _ok(argv, what, cwd)
                if ok and judge is not None:
                    ok, why = judge(stdout)
                if not ok:
                    return False, why
        return True, "dependencies unchanged, pip check OK, editable imports OK"

    return verify
