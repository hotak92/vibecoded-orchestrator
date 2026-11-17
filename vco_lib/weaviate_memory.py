# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Weaviate's container memory limit, sized from the memory its container can use.

ONE home for the formula. install.py projects the result into
``infrastructure/.env`` as :data:`ENV_KEY` (through
:func:`vco_lib.compose_env.compose_substitution_env`, the file compose reads),
and ``infrastructure/docker-compose.yml`` sets ``mem_limit:
${VCT_WEAVIATE_MEM_LIMIT:-0}`` from it.

Why a limit at all: without one, cgroup v2 reports ``memory.max = max`` and
Weaviate's ``LIMIT_RESOURCES`` turns that into an effectively infinite Go soft
limit, so its memory guards never fire and the heap grows until the host runs
short (a 61 GB host lost its desktop session to systemd-oomd while Weaviate
held 30 GB). With a limit, Go's soft limit is 80% of it, Weaviate refuses a
write batch with "not enough memory" before reaching the cap, and on a real
OOM kill ``restart: unless-stopped`` brings it back (both verified on a
disposable container before shipping).

The formula — 40% of the budget, floor 2 GiB, ceiling 32 GiB, rounded down to
256 MiB — leaves the rest for what shares the machine: Ollama (embedding and
summary models; on a CPU-only host those sit in RAM), the code-embedding
service, and the desktop, IDE and browser. The floor is the smallest size
that starts Weaviate with a typical project's collections; the ceiling is
more than VCO's compressed (RQ-8) collections need on any machine seen so far
(a 900k-object install's live heap was ~6-8 GB uncompressed). Examples:
8 GB host -> 3 GiB, 16 GB -> 6.25 GiB, 32 GB -> 12.75 GiB, 64 GB -> 25.5 GiB,
128 GB -> 32 GiB.

Budget first, fraction second (v0.2.107, owner ruling 2026-11-15): the limit
is a fraction of the memory the container can actually use — ``min(host RAM,
container-engine RAM)``. Under Docker Desktop / podman machine the containers
run in a VM that is smaller than the host (WSL2 default: half of it), and a
limit sized from the host alone was 81% of that VM on a 15 GiB Windows
notebook (6144m of 7.4 GiB), so the VM ran out before Weaviate's own guard
refused a batch. The engine's figure is what ``<runtime> info`` reports
(:func:`engine_memory_gb`); when it cannot be read the host figure is used
alone, as before. ``VCT_ENGINE_MEMORY_GB`` states it by hand (``0`` = do not
ask the engine).

A container VCO did not create keeps its own compose, so the limit cannot be
written at create time. :func:`ensure_foreign_limit` applies it IN PLACE
(``<runtime> update --memory``) — no recreate, no restart — and is called at
every ``ensure-containers`` and every update, because an in-place limit does
not survive a restart on podman 4.9. ``VCO_WEAVIATE_FOREIGN_LIMIT=off`` is the
user's switch; a container that already carries a limit of its own is left
alone.

CLI::

    python -m vco_lib.weaviate_memory show
    python -m vco_lib.weaviate_memory ensure-foreign-limit [--dry-run] [--json]
"""
from __future__ import annotations

import functools
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

#: The ``infrastructure/.env`` key compose substitutes into ``mem_limit``.
ENV_KEY = "VCT_WEAVIATE_MEM_LIMIT"
#: The container engine's memory, stated by hand (GiB; ``0`` = do not probe).
ENGINE_ENV_KEY = "VCT_ENGINE_MEMORY_GB"
#: ``off`` = never apply the limit in place to a container VCO did not create.
FOREIGN_LIMIT_ENV = "VCO_WEAVIATE_FOREIGN_LIMIT"

FRACTION_OF_HOST = 0.40
FLOOR_MIB = 2048
CEILING_MIB = 32768
STEP_MIB = 256

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
_PROBE: Any = object()  # "ask the engine" (the default of ``engine_ram_gb``)

#: ``<runtime> info`` templates naming the engine's total memory in bytes —
#: docker's top-level field, then podman's (each runtime tried with its own
#: first; the other is the fallback for a docker-compatible podman shim).
_INFO_FORMATS: Mapping[str, tuple[str, ...]] = {
    "docker": ("{{.MemTotal}}", "{{.Host.MemTotal}}"),
    "podman": ("{{.Host.MemTotal}}", "{{.MemTotal}}"),
}


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def budget_gb(host_ram_gb: Optional[float], engine_ram_gb: Optional[float] = None) -> Optional[float]:
    """The memory Weaviate's container can actually use, in GiB: the smaller
    of the host's RAM and the container engine's (a VM under Docker Desktop /
    podman machine). An unknown figure is left out; both unknown → ``None``."""
    known = [v for v in (_positive(host_ram_gb), _positive(engine_ram_gb)) if v is not None]
    return min(known) if known else None


def mem_limit_mib(host_ram_gb: Optional[float],
                  engine_ram_gb: Optional[float] = None) -> Optional[int]:
    """The limit in MiB for a host with *host_ram_gb* GiB of RAM whose
    container engine has *engine_ram_gb* (``None`` = not known: the host
    alone), or ``None`` when neither is known (the probe failed — write
    nothing rather than guess)."""
    ram = budget_gb(host_ram_gb, engine_ram_gb)
    if ram is None:
        return None
    mib = int(ram * 1024 * FRACTION_OF_HOST)
    mib = max(FLOOR_MIB, min(CEILING_MIB, mib))
    return mib - (mib % STEP_MIB)


def engine_memory_gb(runtime: Optional[str], *, run: Optional[RunFn] = None) -> Optional[float]:
    """The container engine's total memory in GiB (``<runtime> info``), or
    ``None`` when it cannot be read. On native Linux this is the host's RAM;
    under Docker Desktop / podman machine it is the VM's."""
    if not runtime:
        return None
    if run is None:
        from vco_lib import tool_search_dirs as _tsd  # noqa: PLC0415

        run = _tsd.run
    name = Path(str(runtime)).name.lower()
    formats = _INFO_FORMATS["docker" if "docker" in name else "podman"]
    for fmt in formats:
        try:
            res = run([runtime, "info", "--format", fmt], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        text = (getattr(res, "stdout", "") or "").strip()
        if getattr(res, "returncode", 1) == 0 and text.isdecimal() and int(text) > 0:
            return int(text) / (1024 ** 3)
    return None


@functools.lru_cache(maxsize=1)
def _probed_engine_memory_gb() -> Optional[float]:
    """One engine probe per process (the runtime VCO already resolved)."""
    try:
        from vco_lib import containers as _containers  # noqa: PLC0415

        runtime = _containers.resolve(probe_compose=False).runtime
    except Exception:  # noqa: BLE001 — no runtime: the engine's memory is not known
        return None
    return engine_memory_gb(runtime)


def resolve_engine_memory_gb(environ: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """``VCT_ENGINE_MEMORY_GB`` when set (``0`` / unparsable = unknown, no
    probe), else what the engine reports."""
    import os  # noqa: PLC0415

    env = os.environ if environ is None else environ
    if ENGINE_ENV_KEY in env:
        return _positive(env.get(ENGINE_ENV_KEY))
    return _probed_engine_memory_gb()


def describe_budget(host_ram_gb: Optional[float], engine_ram_gb: Optional[float]) -> str:
    """One line naming BOTH numbers and the limit they give."""
    host, engine = _positive(host_ram_gb), _positive(engine_ram_gb)
    mib = mem_limit_mib(host, engine)
    parts = [f"host {host:.1f} GiB" if host is not None else "host RAM unknown",
             f"container engine {engine:.1f} GiB" if engine is not None
             else "container engine memory not readable"]
    limit = f"{mib}m ({int(FRACTION_OF_HOST * 100)}% of the smaller)" if mib is not None else "none"
    return f"Weaviate memory limit: {', '.join(parts)} -> {limit}"


def env_value(host_ram_gb: Optional[float],
              environ: Optional[Mapping[str, str]] = None,
              engine_ram_gb: Any = _PROBE) -> Optional[str]:
    """The ``VCT_WEAVIATE_MEM_LIMIT`` value install writes (``"<n>m"``), or
    ``None`` = write nothing: when the user exported the key themselves (their
    value reaches compose through its own environment, ``"0"`` = no limit), or
    when neither the host's nor the engine's memory is known (an existing line
    in ``.env`` is then kept). *engine_ram_gb* defaults to asking the engine
    (:func:`resolve_engine_memory_gb`); pass a number or ``None`` to state it."""
    import os  # noqa: PLC0415

    env = os.environ if environ is None else environ
    if ENV_KEY in env:
        return None
    engine = resolve_engine_memory_gb(env) if engine_ram_gb is _PROBE else engine_ram_gb
    mib = mem_limit_mib(host_ram_gb, engine)
    return None if mib is None else f"{mib}m"


# ─── the limit on a container VCO did not create ────────────────────────

APPLIED = "applied"            # the limit was missing and is now set
ALREADY = "already_limited"    # carries VCO's limit already
OWN_LIMIT = "own_limit"        # carries another limit: its owner's, left alone
NOT_FOREIGN = "not_foreign"    # VCO's own container (compose carries the limit) / no container
OPTED_OUT = "opted_out"        # VCO_WEAVIATE_FOREIGN_LIMIT=off, or the user's "0"
UNKNOWN = "unknown"            # the limit or the container could not be read — nothing done
WOULD_APPLY = "would_apply"    # --dry-run
FAILED = "failed"              # the engine refused the update


@dataclass
class ForeignLimit:
    status: str
    container: str = ""
    current_bytes: Optional[int] = None
    wanted: str = ""
    line: str = ""

    @property
    def uncapped(self) -> bool:
        """The container still runs without a limit after this call."""
        return self.current_bytes == 0 and self.status != APPLIED


def parse_limit_bytes(value: str) -> Optional[int]:
    """``"6144m"`` / ``"8g"`` / ``"1073741824"`` → bytes; ``None`` when the
    text is not a size (``"0"`` is 0 = no limit)."""
    text = (value or "").strip().lower()
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "b": 1}
    factor = 1
    if text and text[-1] in units:
        factor, text = units[text[-1]], text[:-1]
    if not text.isdecimal():
        return None
    return int(text) * factor


def wanted_limit(root: Path, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The limit VCO would give Weaviate on this machine: the user's exported
    ``VCT_WEAVIATE_MEM_LIMIT``, else the value install sized into
    ``infrastructure/.env``. ``None`` when neither states one."""
    import os  # noqa: PLC0415

    env = os.environ if environ is None else environ
    if ENV_KEY in env:
        return (env.get(ENV_KEY) or "").strip() or None
    try:
        from vco_lib import service_adoption as _sa  # noqa: PLC0415

        value = (_sa.infrastructure_env_for_substitution(Path(root) / "infrastructure") or {}).get(ENV_KEY)
    except Exception:  # noqa: BLE001 — an unreadable .env states no limit
        return None
    return (value or "").strip() or None


def _memory_of(runtime: str, name: str, run: RunFn) -> Optional[int]:
    try:
        res = run([runtime, "inspect", "--type", "container", "--format",
                   "{{.HostConfig.Memory}}", name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (getattr(res, "stdout", "") or "").strip()
    if getattr(res, "returncode", 1) != 0 or not text.lstrip("-").isdecimal():
        return None
    return max(0, int(text))


def ensure_foreign_limit(
    *,
    root: Path,
    runtime: Optional[str],
    row: Any,
    run: Optional[RunFn] = None,
    environ: Optional[Mapping[str, str]] = None,
    dry_run: bool = False,
) -> ForeignLimit:
    """Give the Weaviate container VCO uses but did not create (*row* is an
    ``adopted_container`` row) VCO's memory limit, in place. Never a recreate,
    never a restart. Acts only on positive evidence: the container's current
    limit was read and is 0, and the limit to apply is known. A container that
    already carries a limit — VCO's or its owner's — is left alone."""
    import os  # noqa: PLC0415

    env = os.environ if environ is None else environ
    mode = getattr(row, "mode", None)
    name = getattr(row, "container_name", None) or ""
    if mode != "adopted_container" or not name or not runtime:
        return ForeignLimit(NOT_FOREIGN, name)
    if (env.get(FOREIGN_LIMIT_ENV) or "").strip().lower() in ("off", "0", "false", "no"):
        return ForeignLimit(OPTED_OUT, name, line=(
            f"  [weaviate] {FOREIGN_LIMIT_ENV}=off — {name} keeps the memory limit its owner gave it"))
    if run is None:
        from vco_lib import tool_search_dirs as _tsd  # noqa: PLC0415

        run = _tsd.run
    current = _memory_of(runtime, name, run)
    wanted = wanted_limit(root, env)
    wanted_bytes = parse_limit_bytes(wanted) if wanted else None
    if current is None:
        return ForeignLimit(UNKNOWN, name, None, wanted or "", (
            f"  [weaviate] the memory limit of {name} could not be read — nothing changed"))
    if wanted_bytes == 0:
        return ForeignLimit(OPTED_OUT, name, current, wanted or "", (
            f"  [weaviate] {ENV_KEY}=0 (no limit, your setting) — {name} left as it is"))
    if current > 0:
        same = wanted_bytes is not None and current == wanted_bytes
        return ForeignLimit(ALREADY if same else OWN_LIMIT, name, current, wanted or "",
                            "" if same else (
            f"  [weaviate] {name} carries its own memory limit ({current // (1024 ** 2)}m"
            + (f"; VCO would size {wanted}" if wanted else "") + ") — left as it is"))
    if wanted is None or wanted_bytes is None:
        return ForeignLimit(UNKNOWN, name, current, "", (
            f"  [weaviate] {name} has no memory limit and VCO has not sized one yet "
            f"({ENV_KEY} is not in infrastructure/.env; `python install.py --update` writes it)"))
    argv = [runtime, "update", "--memory", wanted, "--memory-swap", wanted, name]
    if dry_run:
        return ForeignLimit(WOULD_APPLY, name, current, wanted,
                            f"  [weaviate] would run: {' '.join(argv)}")
    try:
        res = run(argv, capture_output=True, text=True, timeout=60)
        ok = getattr(res, "returncode", 1) == 0
        err = ((getattr(res, "stderr", "") or "").strip().splitlines() or [""])[-1]
    except (OSError, subprocess.SubprocessError) as exc:
        ok, err = False, str(exc)
    after = _memory_of(runtime, name, run) if ok else None
    if ok and after == wanted_bytes:
        return ForeignLimit(APPLIED, name, after, wanted, (
            f"  [weaviate] memory limit {wanted} applied in place to {name} (your container: "
            "not recreated, not restarted; re-applied after each restart)"))
    return ForeignLimit(FAILED, name, current, wanted, (
        f"  [weaviate] could not apply the memory limit to {name}"
        + (f": {err[:200]}" if err else "") + f" — by hand: {' '.join(argv)}"))


def ensure_foreign_limit_for_install(root: Path, *, runtime: Optional[str] = None,
                                     db_path: Optional[Path] = None, run: Optional[RunFn] = None,
                                     environ: Optional[Mapping[str, str]] = None,
                                     dry_run: bool = False) -> ForeignLimit:
    """:func:`ensure_foreign_limit` for the recorded Weaviate row (the
    ``ensure-containers`` hooks' entry point)."""
    from vco_lib import service_endpoints as _se  # noqa: PLC0415

    try:
        row = _se.load_rows(db_path).get("weaviate")
    except Exception:  # noqa: BLE001 — no rows: whose container it is is not known
        return ForeignLimit(UNKNOWN)
    if runtime is None:
        try:
            from vco_lib import containers as _containers  # noqa: PLC0415

            runtime = _containers.resolve(probe_compose=False).runtime
        except Exception:  # noqa: BLE001
            runtime = None
    return ensure_foreign_limit(root=root, runtime=runtime, row=row, run=run, environ=environ,
                                dry_run=dry_run)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m vco_lib.weaviate_memory")
    sub = parser.add_subparsers(dest="verb", required=True)
    show = sub.add_parser("show", help="print the host/engine memory and the limit they give")
    show.add_argument("--host-ram-gb", type=float, default=None)
    ens = sub.add_parser("ensure-foreign-limit",
                         help="apply the limit in place to a Weaviate container VCO did not create")
    ens.add_argument("--root", default=None)
    ens.add_argument("--dry-run", action="store_true")
    ens.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.verb == "show":
        host = args.host_ram_gb
        if host is None:
            try:
                from vco_lib import host_probe as _hp  # noqa: PLC0415

                host = _hp.probe_system_ram_gb()
            except Exception:  # noqa: BLE001 — an unknown host figure is printed as such
                host = None
        print(describe_budget(host, resolve_engine_memory_gb()))
        return 0
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    result = ensure_foreign_limit_for_install(root, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(asdict(result), sort_keys=True))
    elif result.line:
        print(result.line)
    return 1 if result.status == FAILED else 0


if __name__ == "__main__":  # pragma: no cover — CLI
    sys.exit(main())
