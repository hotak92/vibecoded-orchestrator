# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""THE resolver for "which interpreter can run our own code" (v0.2.94).

Why this module exists — the 2026-09-09 field defect
----------------------------------------------------
"Update all bundles" updated 8 projects and reported, for each one,
``code-graph re-index started in the background``. Every non-root project's
``~/.vct/logs/resync-<Project>-*.log`` was ~1 KB and held nothing but::

    ModuleNotFoundError: No module named 'vco_lib'
      code-summary: Weaviate unreachable: No module named 'weaviate'

The chain: the launcher spawns ``python -m vco_lib.project_init
install-bundle --update`` with a BARE PATH probe (``python3``) and
``current_dir(<orchestrator root>)``, so ``project_init`` itself imports
``vco_lib`` off the cwd — but ``sys.executable`` inside it is
``/usr/bin/python3``. ``codegraph_resync.spawn_background_resync`` then used
``python_exe or sys.executable`` plus a FILE path
(``str(Path(__file__).resolve())``) with ``cwd`` = the USER project, so the
detached child had neither ``vco_lib`` (wrong cwd) nor ``weaviate`` (wrong
interpreter). The spawn returned ``launched`` — nothing checked — so the GUI
reported success and the failure lived only in a log nobody reads.

Two rules follow, and this module is how they are kept:

1. **Every install/update path that asks "which python do I spawn" asks
   HERE.** No per-path copy. ``sys.executable`` is not that answer: a
   subprocess inherits the interpreter of whoever spawned IT, so one bare
   PATH probe at the top of the chain poisons every descendant.
2. **A spawn that cannot import its own package is a FAILURE, reported as
   one** — never a "launched" that only a log file contradicts. See
   :func:`preflight`, and its use at the spawn seam in
   ``codegraph_resync.spawn_background_resync``.

The ladder (canonical order)
----------------------------
1. ``$VCT_VENV`` — explicit override. Accepts a venv DIRECTORY or the
   interpreter binary itself.
2. ``<install_root>/.venv`` — the modern layout ``install.py`` creates.
3. ``<install_root>/claude_mcp_servers/.venv`` — the legacy layout.
4. ``sys.executable`` — **only** when it passes :func:`preflight` (it is the
   right answer when we are already running inside the orchestrator venv, and
   the wrong one in exactly the field case above).
5. Loud failure: :class:`PythonExeUnresolved`, naming every candidate tried
   and why each was rejected. NEVER a silent ``"python3"``.

``install_root`` comes from ``$VCT_INSTALL_ROOT`` / ``$VCT_ORCHESTRATOR_ROOT``
(validated with :func:`vco_lib.paths.looks_like_orchestrator_root`, the
existing one home for "is this an orchestrator clone" — a stale exported value
from a moved clone must not win), then the package's own parent directory,
which is exact rather than best-effort: ``install.py`` installs the
distribution with ``pip install -e .``, so an importable ``vco_lib`` lives
INSIDE the clone.

Cross-language pin (C-tier mirror, justified)
---------------------------------------------
``launcher/src-tauri/vct-launcher-core/src/python_resolve.rs`` walks the same
ladder. It cannot call this module — it must find a python BEFORE it can ask
python anything, which is the one shape A-tier (shared code) cannot cover. The
DATA is therefore what is locked: :data:`VENV_LAYOUTS`,
:data:`POSIX_INTERPRETER_NAMES`, :data:`WINDOWS_INTERPRETER_NAMES`,
:data:`VENV_ENV_VAR` and :data:`INSTALL_ROOT_ENV_VARS` are asserted against the
Rust source by ``tests/test_v0294_python_exe_parity.py``.
MUST MATCH launcher/src-tauri/vct-launcher-core/src/python_resolve.rs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from vco_lib.paths import looks_like_orchestrator_root

logger = logging.getLogger(__name__)

# ── The ladder's DATA. MUST MATCH python_resolve.rs (parity-tested). ─────────

#: Explicit override env var. May name a venv directory OR an interpreter.
VENV_ENV_VAR = "VCT_VENV"

#: Orchestrator-clone env vars, in probe order. The Rust mirror reads only the
#: first; the second is honoured here because several Python entry points
#: (hooks, `project_move`, `boot_service`) already publish it and a resolver
#: that ignored it would answer differently from its own callers.
INSTALL_ROOT_ENV_VARS = ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")

#: venv directory layouts probed under an install root, in order. The second is
#: the pre-v0.2.74 location; still honoured so an un-migrated clone resolves.
VENV_LAYOUTS = (".venv", "claude_mcp_servers/.venv")

#: Interpreter file names probed inside each layout, IN ORDER, on EVERY OS.
#:
#: Not per-OS, and that is deliberate (v0.2.94 review R6/9). The Rust mirror is
#: one binary that must work cross-OS, so it probes all three unconditionally;
#: a Python side that skipped `bin/python*` on Windows would decide differently
#: from Rust on a real machine — an MSYS / Cygwin / Git-Bash venv on Windows has
#: `bin/`, not `Scripts/` — while a parity test comparing the UNION would report
#: agreement. Probing a path that cannot exist costs one `is_file()`.
#:
#: The POSIX/WINDOWS split below is documentation of which name belongs to which
#: layout convention; :data:`VENV_INTERPRETER_NAMES` is what is probed, and what
#: the parity test compares against each Rust candidate list.
POSIX_INTERPRETER_NAMES = ("bin/python", "bin/python3")
WINDOWS_INTERPRETER_NAMES = ("Scripts/python.exe",)
VENV_INTERPRETER_NAMES = POSIX_INTERPRETER_NAMES + WINDOWS_INTERPRETER_NAMES

#: What a spawned VCO child must be able to import. ``vco_lib`` is our own
#: package; ``weaviate`` is the heaviest third-party dependency every analyzer /
#: sync / resync child needs and the one a PEP-668 system python never has.
DEFAULT_REQUIRED_MODULES: tuple[str, ...] = ("vco_lib", "weaviate")

#: Tier labels carried on :class:`Candidate` (stable strings — logged, and the
#: tests assert on them rather than on prose).
TIER_VCT_VENV = "VCT_VENV"
TIER_INSTALL_ROOT = "install_root"
TIER_SYS_EXECUTABLE = "sys.executable"

#: Preflight subprocess deadline. Bounded because it runs a fresh interpreter
#: that imports `weaviate` (~1 s warm, a few seconds cold); the CHILD it gates
#: has no timeout (project rule: no global install timeout), only this probe.
PREFLIGHT_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class Candidate:
    """One rung of the ladder, and what became of it."""

    tier: str
    path: str
    #: True when this rung was ACCEPTED (and therefore returned).
    ok: bool = False
    #: Why it was rejected (empty when ``ok``).
    detail: str = ""


class PythonExeUnresolved(RuntimeError):
    """No interpreter on the ladder can run our own code.

    Carries every :class:`Candidate` tried so the message names paths rather
    than saying "not found" — a resolver that fails silently to ``python3`` is
    the defect this module exists to remove, and a resolver that fails
    ANONYMOUSLY is only marginally better.
    """

    def __init__(
        self,
        candidates: Sequence[Candidate],
        required_modules: Sequence[str],
        install_root: Optional[Path],
    ) -> None:
        self.candidates = tuple(candidates)
        self.required_modules = tuple(required_modules)
        self.install_root = install_root
        super().__init__(self._render())

    def _render(self) -> str:
        tried = (
            "; ".join(f"{c.tier}={c.path or '<unset>'} ({c.detail})" for c in self.candidates)
            or "no candidates"
        )
        root = str(self.install_root) if self.install_root else "<unresolved>"
        return (
            "no Python interpreter able to import "
            f"{', '.join(self.required_modules)} was found "
            f"(orchestrator root: {root}). Tried: {tried}. "
            "This is a BROKEN install, not a fallback case — re-run "
            "`python install.py --update` from the orchestrator root, or set "
            f"${VENV_ENV_VAR} to the venv that has them."
        )


# ---------------------------------------------------------------------------
# Layout helpers (pure)
# ---------------------------------------------------------------------------


def interpreter_names() -> tuple[str, ...]:
    """Interpreter file names to probe inside a venv, in order.

    OS-INDEPENDENT by design — see :data:`VENV_INTERPRETER_NAMES`. There is no
    ``os_name`` seam because there is no per-OS branch to inject into: the
    Windows layout is probed from a POSIX runner (and vice versa) simply by
    existing in the list, which is also what the Rust mirror does.
    """
    return VENV_INTERPRETER_NAMES


def venv_interpreters(venv_dir: "str | Path") -> list[Path]:
    """Every interpreter path to probe under ``venv_dir``, in ladder order."""
    base = Path(venv_dir)
    return [base / Path(rel) for rel in interpreter_names()]


def _first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for p in paths:
        try:
            if p.is_file():
                return p
        except OSError:  # pragma: no cover — defensive
            continue
    return None


def venv_in(root: "str | Path") -> Optional[Path]:
    """First existing interpreter under ``root``'s known venv layouts.

    MUST MATCH ``python_resolve.rs::venv_in`` (layout order, then interpreter
    name order).
    """
    base = Path(root)
    for layout in VENV_LAYOUTS:
        found = _first_existing(venv_interpreters(base / layout))
        if found is not None:
            return found
    return None


def resolve_install_root(explicit: "str | Path | None" = None) -> Optional[Path]:
    """The orchestrator clone this process belongs to, or ``None``.

    Ladder: an explicit argument, then :data:`INSTALL_ROOT_ENV_VARS` (each only
    when it really names a clone), then ``vco_lib/..``.

    ``vco_lib.boot_service.default_templates_root`` (the gateway daemon's
    templates root) delegates here since v0.2.94 — one home for the question.
    """
    if explicit:
        candidate = Path(explicit)
        if candidate.is_dir():
            return candidate
    for var in INSTALL_ROOT_ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if raw and looks_like_orchestrator_root(raw):
            return Path(raw)
    fallback = Path(__file__).resolve().parent.parent
    if looks_like_orchestrator_root(fallback):
        return fallback
    return None


# ---------------------------------------------------------------------------
# Preflight — "can this interpreter actually import our stack?"
# ---------------------------------------------------------------------------

#: Env vars that CHANGE the preflight's answer for a given interpreter, and are
#: therefore part of its memo key. Both alter what the probe (and the child it
#: gates) can import, and both legitimately change mid-process: ``PYTHONPATH``
#: is edited by callers that pin a checkout, and ``VIRTUAL_ENV`` moves when a
#: venv is created or activated. Keying on them means an install step that fixes
#: an interpreter is not overruled by a stale "no" — the alternative would be a
#: cache invalidation every caller has to remember.
_PREFLIGHT_ENV_KEYS: tuple[str, ...] = ("PYTHONPATH", "VIRTUAL_ENV")

#: ``{(interpreter, modules, env-fingerprint): (ok, detail)}`` — per
#: interpreter, per process. A preflight costs a fresh interpreter plus a
#: `weaviate` import; an 8-project bundle update asks the same question 8 times
#: and must pay once.
_PREFLIGHT_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...]], tuple[bool, str]
] = {}


def _preflight_env_fingerprint(env: "Optional[dict]" = None) -> tuple[str, ...]:
    """The values of :data:`_PREFLIGHT_ENV_KEYS`, in order."""
    environ = os.environ if env is None else env
    return tuple(environ.get(k, "") for k in _PREFLIGHT_ENV_KEYS)


def clear_preflight_cache() -> None:
    """Drop the per-process preflight memo.

    Rarely needed now that the key carries the env fingerprint: the one case it
    does NOT cover is an interpreter whose PACKAGES change under a FIXED env —
    a ``pip install`` into the same venv from inside this process. A caller that
    preflighted the venv interpreter BEFORE installing into it would need to
    call this afterwards; **none does today** (``install.py``'s only in-process
    preflight runs after ``_install_requirements``, so it already measures the
    populated venv). Recorded so that a future caller which inverts that order
    knows the memo exists. Tests and long-lived daemons use it too.
    """
    _PREFLIGHT_CACHE.clear()


def preflight_script() -> str:
    """``python -c`` body reporting which of ``sys.argv[1:]`` fail to import.

    Prints ONE line of JSON: a list of ``"<module>: <ExcType>: <msg>"`` strings,
    empty when every module imported. Same shape as
    ``install_companions.vco_lib_origin_script`` (a pure builder, so the script
    text is assertable without spawning anything).
    """
    return (
        "import importlib, json, sys\n"
        "bad = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {type(exc).__name__}: {exc}')\n"
        "print(json.dumps(bad))\n"
    )


def preflight(
    python_exe: "str | Path | None",
    modules: Sequence[str] = DEFAULT_REQUIRED_MODULES,
    *,
    timeout: int = PREFLIGHT_TIMEOUT_SECONDS,
    runner: Optional[Callable] = None,
    use_cache: bool = True,
) -> tuple[bool, str]:
    """Can ``python_exe`` import every module in ``modules``?

    Returns ``(ok, detail)``; ``detail`` names the FIRST missing module and its
    exception when not ok, and is empty when ok. Never raises.

    Two deliberate choices about the child environment, both so the probe
    models the REAL spawn rather than a friendlier one:

    * **cwd is a fresh temp dir.** Python puts the cwd on ``sys.path``, so
      probing from the orchestrator clone would find ``vco_lib/`` there for ANY
      interpreter — which is exactly the illusion that let the field defect
      ship (``project_init`` imported fine from cwd; its detached grandchild,
      cwd = the user project, did not).
    * **the environment is INHERITED, not scrubbed.** The child we are gating
      inherits it too, so a ``PYTHONPATH`` that genuinely makes ``vco_lib``
      importable must count here as well. (``install_companions.
      measure_vco_lib_origin`` scrubs it on purpose — it measures install
      HEALTH, the worst case; this measures what the spawn will actually get.)
    """
    if not python_exe:
        return (False, "no interpreter given")
    mods = tuple(modules)
    key = (str(python_exe), mods, _preflight_env_fingerprint())
    if use_cache and key in _PREFLIGHT_CACHE:
        return _PREFLIGHT_CACHE[key]

    run = runner or subprocess.run
    detail = ""
    ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="vco-preflight-") as neutral_cwd:
            proc = run(
                [str(python_exe), "-c", preflight_script(), *mods],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=neutral_cwd,
            )
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        detail = f"could not run {python_exe}: {type(exc).__name__}: {exc}"
    else:
        stdout = (getattr(proc, "stdout", "") or "").strip()
        rc = getattr(proc, "returncode", 1)
        payload = None
        if stdout:
            try:
                payload = json.loads(stdout.splitlines()[-1])
            except (ValueError, IndexError):
                payload = None
        if not isinstance(payload, list):
            stderr = (getattr(proc, "stderr", "") or "").strip()
            detail = (
                f"preflight probe on {python_exe} produced no verdict "
                f"(exit {rc}): {stderr[-300:] or stdout[-300:] or '<no output>'}"
            )
        elif payload:
            detail = f"{python_exe} cannot import {payload[0]}"
        else:
            ok = True

    result = (ok, detail)
    if use_cache:
        _PREFLIGHT_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def ladder_candidates(
    *,
    install_root: "str | Path | None" = None,
    env: "Optional[dict]" = None,
) -> list[Candidate]:
    """The ladder's rungs 1-3 (env override + install-root venvs), in order.

    Returns only rungs that RESOLVED to an existing file, plus one rejected
    :class:`Candidate` per rung that did not, so the failure message can name
    what was tried. ``sys.executable`` (rung 4) is added by
    :func:`resolve_vco_lib_python`, which alone knows whether to verify it.
    """
    environ = os.environ if env is None else env
    out: list[Candidate] = []

    raw_venv = (environ.get(VENV_ENV_VAR) or "").strip()
    if raw_venv:
        base = Path(raw_venv)
        found = _first_existing(venv_interpreters(base))
        if found is None and base.is_file():
            # $VCT_VENV may name the interpreter itself, not a venv dir.
            found = base
        if found is not None:
            out.append(Candidate(TIER_VCT_VENV, str(found), ok=True))
        else:
            out.append(
                Candidate(
                    TIER_VCT_VENV, raw_venv,
                    detail="set, but holds no interpreter",
                )
            )

    root = resolve_install_root(install_root)
    if root is None:
        out.append(
            Candidate(
                TIER_INSTALL_ROOT, "",
                detail=(
                    "no orchestrator clone resolved from "
                    f"${'/$'.join(INSTALL_ROOT_ENV_VARS)} or vco_lib/.."
                ),
            )
        )
        return out

    for layout in VENV_LAYOUTS:
        venv_dir = root / layout
        found = _first_existing(venv_interpreters(venv_dir))
        if found is not None:
            out.append(Candidate(f"{TIER_INSTALL_ROOT}:{layout}", str(found), ok=True))
        else:
            out.append(
                Candidate(
                    f"{TIER_INSTALL_ROOT}:{layout}", str(venv_dir),
                    detail="no interpreter at this layout",
                )
            )
    return out


def resolve_vco_lib_python(
    *,
    install_root: "str | Path | None" = None,
    modules: Sequence[str] = DEFAULT_REQUIRED_MODULES,
    env: "Optional[dict]" = None,
    verify_current: bool = True,
    runner: Optional[Callable] = None,
) -> Path:
    """Return the interpreter a VCO child process must be spawned with.

    Walks the ladder documented at module level. Raises
    :class:`PythonExeUnresolved` — naming every candidate tried — rather than
    degrading to a bare ``python3``: a missing venv means a BROKEN install, and
    a silent fallback converts that into a detached child that dies in a log
    file (the 2026-09-09 field defect).

    The venv rungs are accepted on EXISTENCE (no subprocess), matching the Rust
    mirror and keeping resolution free; ``sys.executable`` is accepted only
    when :func:`preflight` passes, because that rung is precisely the one that
    is wrong when a bare PATH probe seeded the process chain. Callers that are
    about to spawn a long-lived detached child should ALSO preflight the
    returned interpreter — see ``codegraph_resync.spawn_background_resync``,
    where a broken venv would otherwise still produce a doomed child.
    """
    tried = ladder_candidates(install_root=install_root, env=env)
    for cand in tried:
        if cand.ok:
            return Path(cand.path)

    current = sys.executable or ""
    if not current:
        tried.append(Candidate(TIER_SYS_EXECUTABLE, "", detail="sys.executable is empty"))
        raise PythonExeUnresolved(tried, modules, resolve_install_root(install_root))

    if not verify_current:
        tried.append(Candidate(TIER_SYS_EXECUTABLE, current, ok=True))
        return Path(current)

    ok, detail = preflight(current, modules, runner=runner)
    if ok:
        tried.append(Candidate(TIER_SYS_EXECUTABLE, current, ok=True))
        return Path(current)

    tried.append(Candidate(TIER_SYS_EXECUTABLE, current, detail=detail or "preflight failed"))
    raise PythonExeUnresolved(tried, modules, resolve_install_root(install_root))


def resolve_vco_lib_python_or_none(**kwargs) -> Optional[Path]:
    """:func:`resolve_vco_lib_python`, returning ``None`` instead of raising.

    For best-effort call sites that must not crash their caller (the
    "conservative defaults on best-effort paths" rule). The failure is logged
    at WARNING with the full candidate list — it is never invisible.
    """
    try:
        return resolve_vco_lib_python(**kwargs)
    except PythonExeUnresolved as exc:
        logger.warning("python resolution failed: %s", exc)
        return None


def resolve_or_current(**kwargs) -> str:
    """The interpreter path as a string, falling back to ``sys.executable``.

    The migration shim for call sites whose PREVIOUS behaviour was
    ``sys.executable`` and whose failure mode on a broken interpreter is
    already handled (a non-zero exit the caller reports). It still PREFERS the
    orchestrator venv, and still logs when the ladder could not answer — so it
    is a strictly better ``sys.executable``, never a silent PATH probe.
    """
    resolved = resolve_vco_lib_python_or_none(**kwargs)
    if resolved is not None:
        return str(resolved)
    return sys.executable or "python3"


__all__ = [
    "DEFAULT_REQUIRED_MODULES",
    "INSTALL_ROOT_ENV_VARS",
    "POSIX_INTERPRETER_NAMES",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "TIER_INSTALL_ROOT",
    "TIER_SYS_EXECUTABLE",
    "TIER_VCT_VENV",
    "VENV_ENV_VAR",
    "VENV_LAYOUTS",
    "WINDOWS_INTERPRETER_NAMES",
    "Candidate",
    "PythonExeUnresolved",
    "clear_preflight_cache",
    "interpreter_names",
    "ladder_candidates",
    "preflight",
    "preflight_script",
    "resolve_install_root",
    "resolve_or_current",
    "resolve_vco_lib_python",
    "resolve_vco_lib_python_or_none",
    "venv_in",
    "venv_interpreters",
]
