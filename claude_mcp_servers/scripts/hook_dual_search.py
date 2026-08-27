#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2 (v0.2.91) — run the pre-edit hook's KG **and** code-graph searches in ONE
CPython process, with the two legs still concurrent.

Why this exists
---------------
The pre-edit hook issued two searches per cache miss:

    $VENV claude_mcp_servers/scripts/rl_kg_search.py  "<q>" --limit 1 --hook-format
    .claude/scripts/code-graph-query search "<q>" --limit 2 --hook-format ...

as two BACKGROUND subprocesses — so two FULL interpreter starts, overlapped. The
2026-08-27 perf investigation measured the miss path at ~1.5 s wall, of which the
DB work is ~3 ms (Weaviate hybrid) + ~58 ms (query embed): essentially all of it
is CPython start + `import weaviate` (0.41 s by itself) + client connect, paid
TWICE for two processes that import the very same modules.

WHAT THE FIRST CUT GOT WRONG (recorded so nobody re-derives it): merging the two
legs into ONE process and running them SEQUENTIALLY is *slower* than the old
path, not faster — measured 1.50 s vs 1.34 s. The old path's two interpreter
starts overlapped on separate cores, so its wall clock was max(kg, cg), not
kg + cg. Deduplicating the imports only helps if the two legs stay CONCURRENT.

So this driver does both:
  1. pays the shared, expensive imports ONCE, in the main thread, up front
     (`import weaviate` and friends are ~0.4 s of the ~1.0 s per-leg overhead);
  2. runs the two legs CONCURRENTLY in threads. Both are I/O-bound (HTTP to
     Weaviate / Ollama / the embed service) and release the GIL while waiting, so
     they overlap the way the two processes used to.

Zero functionality change is the CONTRACT
-----------------------------------------
This module does not re-implement either search. It calls the SAME entry points
with the SAME argv the two CLIs would have received, and captures each leg's
stdout verbatim:

  * KG  → ``rl_kg_search.main()``      (argv: ``<query> --limit N --hook-format``)
  * CG  → ``query_code_graph.main()``  (argv: ``search <query> --limit N
                                        --hook-format [--project P]
                                        [--exclude-file F] [--anchor A]``)

so the emitted blocks are byte-identical to the two-process path. The caller
(``_lib/query-cache.sh::vco_dual_search_cached``) applies the SAME per-leg output
caps (``head -40`` for KG, ``head -20`` for CG) and the SAME per-leg cache keys as
before, so cross-surface cache sharing with pre-bash / pre-tool-use is unchanged.
Verified by golden-output diff against the two-CLI path.

Output framing
--------------
Each ENABLED leg emits a marker line followed by that leg's stdout::

    <<<VCO-DUAL:KG>>>
    KG: ...
    <<<VCO-DUAL:CG>>>
    CODE: ...

Markers are on their own line and are the only thing the caller splits on. A
disabled leg emits no marker at all. Order is fixed (KG then CG) regardless of
which thread finished first — the legs write into per-thread buffers, never
straight to stdout.

Concurrent stdout capture
-------------------------
``contextlib.redirect_stdout`` is process-global and therefore NOT usable with
two concurrent legs. Instead ``sys.stdout`` is swapped for a proxy that routes
each ``write`` to the calling THREAD's buffer (falling back to the real stdout
for any other thread). Both producers emit through plain ``print``, which
resolves ``sys.stdout`` at call time, so the routing is exact.

Soft-fail
---------
Each leg is isolated: an exception (or a ``SystemExit`` from the code-graph CLI's
module-level import guards) yields an EMPTY block for that leg and never affects
the other. That matches the pre-P2 shell behaviour, where each producer was
invoked with ``2>/dev/null || true``.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import threading
from pathlib import Path

# MUST MATCH templates/hooks/_lib/query-cache.{sh,ps1} — the caller splits the
# stream on these exact lines.
KG_MARKER = "<<<VCO-DUAL:KG>>>"
CG_MARKER = "<<<VCO-DUAL:CG>>>"

_SCRIPTS_DIR = Path(__file__).resolve().parent          # claude_mcp_servers/scripts
_MCP_SERVERS_DIR = _SCRIPTS_DIR.parent                  # claude_mcp_servers


def _ensure_sys_path() -> None:
    """Put the same directories on sys.path that the two wrappers arranged.

    ``rl_kg_search.py`` inserts ``claude_mcp_servers/`` itself (so ``weaviate_mcp``
    resolves); the ``code-graph-query`` bash wrapper exports
    ``PYTHONPATH=$ORCH_ROOT/claude_mcp_servers`` for the same reason. Doing both
    here keeps the in-process legs importing exactly what the subprocess legs did.
    """
    for p in (str(_SCRIPTS_DIR), str(_MCP_SERVERS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


class _ThreadRoutedStdout:
    """``sys.stdout`` stand-in that routes writes to the calling thread's buffer.

    Needed because the two legs run concurrently and ``redirect_stdout`` is
    process-global: a shared redirect would interleave the KG and CODE blocks.
    Threads that never registered a buffer (anything the producers spawn
    internally) fall through to the real stdout, which is what those writes
    would have hit on the two-process path anyway.
    """

    def __init__(self, fallback) -> None:
        self._fallback = fallback
        self._buffers: "dict[int, io.StringIO]" = {}

    def register(self, buf: io.StringIO) -> None:
        self._buffers[threading.get_ident()] = buf

    def _target(self):
        return self._buffers.get(threading.get_ident(), self._fallback)

    # --- minimal text-stream surface the producers touch -------------------
    def write(self, s):  # noqa: D102
        return self._target().write(s)

    def writelines(self, lines):  # noqa: D102
        for line in lines:
            self.write(line)

    def flush(self):  # noqa: D102
        try:
            self._target().flush()
        except Exception:  # noqa: BLE001 — a StringIO flush can't fail; be safe
            pass

    def isatty(self):  # noqa: D102
        return False

    @property
    def encoding(self):  # noqa: D102
        return getattr(self._fallback, "encoding", "utf-8")

    @property
    def errors(self):  # noqa: D102
        return getattr(self._fallback, "errors", None)


def _run_leg(proxy: _ThreadRoutedStdout, fn) -> str:
    """Run ``fn()`` in the CURRENT thread with its stdout captured; return it.

    Soft-fail per leg: any exception (including ``SystemExit`` — the code-graph
    CLI calls ``sys.exit(1)`` at module scope when weaviate-client is missing)
    returns whatever was printed before the failure, never propagates. stderr is
    left alone: the shell caller already redirects it to /dev/null, and silently
    swallowing it here would hide diagnostics from a manual run.
    """
    buf = io.StringIO()
    proxy.register(buf)
    try:
        fn()
    except SystemExit:
        pass
    except BaseException as exc:  # noqa: BLE001 — a leg must never kill the hook
        print(f"[hook_dual_search] leg failed: {exc!r}", file=sys.stderr)
    return buf.getvalue()


def _load_cg_module(cg_script: Path):
    """Import ``query_code_graph.py`` from an explicit path (it ships into a
    project as ``.claude/scripts/query_code_graph.py``, outside any package)."""
    spec = importlib.util.spec_from_file_location("_vco_query_code_graph", cg_script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {cg_script}")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so a module-level self-import inside the script sees a
    # consistent module object.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cg_argv(
    query: str, limit: int, project: str, exclude_file: str, anchor: str
) -> "list[str]":
    """The argv (minus argv[0]) the `code-graph-query search ...` CLI receives."""
    argv = ["search", query, "--limit", str(limit), "--hook-format"]
    if project:
        argv += ["--project", project]
    if exclude_file:
        argv += ["--exclude-file", exclude_file]
    if anchor:
        argv += ["--anchor", anchor]
    return argv


def _pin_argv(mod, fixed_args: "list[str]") -> None:
    """Make ``mod``'s ``parser.parse_args()`` read ``fixed_args`` instead of
    ``sys.argv``.

    The two legs run CONCURRENTLY, so they cannot share the process-global
    ``sys.argv`` — whichever leg set it last would win. Rather than patching the
    producers (which would break the "same entry point, same argv" equivalence
    this driver rests on), each producer module gets its OWN ``argparse`` shim
    bound to its OWN argument list. Nothing is shared between the legs, so there
    is no race to serialize.

    Only the TOP-LEVEL ``parse_args()`` call is affected (``args is None``);
    sub-parsers, which argparse always calls with an explicit list, are
    untouched. ``prog`` still derives from ``sys.argv[0]``, which only shows up
    in usage/error text that these callers never trigger.
    """
    import argparse as _argparse
    import types

    class _PinnedParser(_argparse.ArgumentParser):
        def parse_args(self, args=None, namespace=None):  # noqa: D102
            return super().parse_args(fixed_args if args is None else args, namespace)

    shim = types.ModuleType("argparse")
    shim.__dict__.update(_argparse.__dict__)
    shim.ArgumentParser = _PinnedParser
    mod.argparse = shim


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hook_dual_search.py",
        description="Run the pre-edit KG + code-graph searches in one interpreter.",
    )
    ap.add_argument("--query", required=True, help="the shared search query")
    ap.add_argument(
        "--kg-limit",
        type=int,
        default=0,
        help="KG results (0 = skip the KG leg entirely — e.g. its cache already hit)",
    )
    ap.add_argument(
        "--cg-limit",
        type=int,
        default=0,
        help="code-graph results (0 = skip the CG leg — non-code file, or cache hit)",
    )
    ap.add_argument("--cg-script", default="", help="path to query_code_graph.py")
    ap.add_argument("--cg-project", default="", help="value for the CLI's --project")
    ap.add_argument("--cg-exclude-file", default="", help="value for --exclude-file")
    ap.add_argument("--cg-anchor", default="", help="value for --anchor")
    args = ap.parse_args(argv)

    _ensure_sys_path()

    want_kg = args.kg_limit > 0
    want_cg = args.cg_limit > 0 and bool(args.cg_script)

    # ── 1. Pay the expensive imports ONCE, up front, in the main thread ─────
    # This is the whole point of the merge: `import weaviate` + weaviate_mcp are
    # ~0.4 s of each leg's ~1.0 s overhead, and both legs need them. Importing
    # here (rather than inside the workers) also removes any import-lock race
    # between the two threads.
    kg_mod = None
    if want_kg:
        try:
            import rl_kg_search as kg_mod  # noqa: F401 — resolved via _ensure_sys_path
        except BaseException as exc:  # noqa: BLE001
            print(f"[hook_dual_search] KG leg import failed: {exc!r}", file=sys.stderr)
            kg_mod = None
            want_kg = False

    cg_mod = None
    cg_script = Path(args.cg_script) if args.cg_script else None
    if want_cg and cg_script is not None and cg_script.is_file():
        try:
            cg_mod = _load_cg_module(cg_script)
        except BaseException as exc:  # noqa: BLE001 — includes SystemExit from
            # query_code_graph's module-level "weaviate-client not installed" guard
            print(f"[hook_dual_search] CG leg import failed: {exc!r}", file=sys.stderr)
            cg_mod = None
    if cg_mod is None:
        want_cg = False

    # Pin each leg's argv to its OWN list (no shared sys.argv → no race).
    if want_kg:
        _pin_argv(kg_mod, [args.query, "--limit", str(args.kg_limit), "--hook-format"])
    if want_cg:
        _pin_argv(
            cg_mod,
            _cg_argv(
                args.query,
                args.cg_limit,
                args.cg_project,
                args.cg_exclude_file,
                args.cg_anchor,
            ),
        )

    # ── 2. Run the legs CONCURRENTLY (they are I/O-bound: HTTP to Weaviate /
    #      Ollama / the embed service, so they overlap the way the two
    #      background subprocesses used to) ─────────────────────────────────
    real_stdout = sys.stdout
    proxy = _ThreadRoutedStdout(real_stdout)
    results: "dict[str, str]" = {"kg": "", "cg": ""}

    def _kg_worker() -> None:
        import asyncio

        # asyncio.run() in a worker thread creates + owns its own event loop;
        # it installs no signal handlers, so a non-main thread is fine.
        results["kg"] = _run_leg(proxy, lambda: asyncio.run(kg_mod.main()))

    def _cg_worker() -> None:
        results["cg"] = _run_leg(proxy, cg_mod.main)

    threads = []
    if want_kg:
        threads.append(threading.Thread(target=_kg_worker, name="vco-kg", daemon=True))
    if want_cg:
        threads.append(threading.Thread(target=_cg_worker, name="vco-cg", daemon=True))

    sys.stdout = proxy
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.stdout = real_stdout

    # ── 3. Emit in FIXED order (KG then CG), regardless of finish order ─────
    if args.kg_limit > 0:
        sys.stdout.write(KG_MARKER + "\n")
        sys.stdout.write(results["kg"])
        if results["kg"] and not results["kg"].endswith("\n"):
            sys.stdout.write("\n")
    if args.cg_limit > 0 and args.cg_script:
        sys.stdout.write(CG_MARKER + "\n")
        sys.stdout.write(results["cg"])
        if results["cg"] and not results["cg"].endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    # Match the two CLIs' quiet-on-failure posture: the hook redirects stderr to
    # /dev/null and treats a non-zero exit as "no context", never as an error.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
