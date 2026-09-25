# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 lane Z — install.py resolves Weaviate from the process env only
AFTER step [5b] has pinned that env to the service_endpoints rows.

install.py's Weaviate call sites (``_wh.weaviate_url_default()``: collections,
seed, readiness, schema migrations, binding self-heal, the deferral re-probe)
read ``WEAVIATE_URL`` then ``WEAVIATE_PORT``. Step [5b]
(``_reconcile_service_endpoints``) sets both from the rows
(``service_endpoints.transport_env``). A call site that ran BEFORE [5b] would
read whatever the invoking shell exported — a stale value, the exact drift
the rows exist to end. The audit (lane Z, 2026-09-24) found none: every such
site is reached only after [5b] on the full-install, ``--update`` and
``--no-containers`` paths, and ``--lightweight`` reaches none of them.

This test keeps it that way. It follows main()'s control flow over the real
call graph of install.py (``ast`` Call nodes — a name in a comment or a
string cannot satisfy it), and fails on any call that can reach
``weaviate_url_default`` while the env is not yet pinned. The mutation cases
below are its red proof: each inserts one pre-[5b] caller into a COPY of the
source and asserts the checker catches it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"

#: The call that pins the process env to the rows (step [5b]).
PIN = "_reconcile_service_endpoints"
#: The env-reading resolver the pin exists for.
LEAF = "weaviate_url_default"


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def reaching_functions(tree: ast.Module) -> set[str]:
    """Top-level functions of install.py that call ``weaviate_url_default``,
    directly or through other top-level functions (a fixpoint)."""
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    callees = {name: {_call_name(c) for c in ast.walk(node) if isinstance(c, ast.Call)}
               for name, node in defs.items()}
    reaching = {name for name, called in callees.items() if LEAF in called}
    while True:
        more = {name for name, called in callees.items() if called & reaching} - reaching
        if not more:
            return reaching
        reaching |= more


def _ordered_calls(node: ast.AST) -> list[ast.Call]:
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def _terminates(stmts: list[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


class _Flow:
    """Walk statements in execution order, tracking whether the env is
    pinned; record every call to a reaching function made while it is not.
    Conservative: a loop body may not run, an ``except`` may fire before the
    pin, and a branch is pinned after an ``if`` only when BOTH sides pin it
    (a side that returns does not flow on)."""

    def __init__(self, reaching: set[str]):
        self.reaching = reaching
        self.violations: list[tuple[int, str]] = []

    def expr(self, node: ast.AST, pinned: bool) -> bool:
        for call in _ordered_calls(node):
            name = _call_name(call)
            if name in self.reaching and not pinned:
                self.violations.append((call.lineno, name))
            if name == PIN:
                pinned = True
        return pinned

    def block(self, stmts: Iterable[ast.stmt], pinned: bool) -> bool:
        for stmt in stmts:
            pinned = self.stmt(stmt, pinned)
        return pinned

    def branch(self, stmts: list[ast.stmt], pinned: bool) -> bool:
        after = self.block(stmts, pinned)
        return True if _terminates(stmts) else after

    def stmt(self, stmt: ast.stmt, pinned: bool) -> bool:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return pinned  # defined, not run
        if isinstance(stmt, ast.If):
            pinned = self.expr(stmt.test, pinned)
            return self.branch(stmt.body, pinned) and self.branch(stmt.orelse, pinned)
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            pinned = self.expr(stmt.iter, pinned)
            self.block(stmt.body, pinned)
            self.block(stmt.orelse, pinned)
            return pinned
        if isinstance(stmt, ast.While):
            pinned = self.expr(stmt.test, pinned)
            self.block(stmt.body, pinned)
            self.block(stmt.orelse, pinned)
            return pinned
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                pinned = self.expr(item.context_expr, pinned)
            return self.block(stmt.body, pinned)
        if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            body_after = self.block(stmt.body, pinned)  # type: ignore[attr-defined]
            handlers_after = [self.branch(h.body, pinned) for h in stmt.handlers]  # type: ignore[attr-defined]
            else_after = self.block(stmt.orelse, body_after)  # type: ignore[attr-defined]
            self.block(stmt.finalbody, pinned)  # type: ignore[attr-defined]
            return else_after and all(handlers_after)
        if isinstance(stmt, ast.Match):
            pinned = self.expr(stmt.subject, pinned)
            return all([self.branch(case.body, pinned) for case in stmt.cases])
        return self.expr(stmt, pinned)


def unpinned_calls(source: str) -> tuple[set[str], list[tuple[int, str]]]:
    """``(reaching functions, [(line, callee)] called from main() before the
    pin)``."""
    tree = ast.parse(source)
    reaching = reaching_functions(tree)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    flow = _Flow(reaching)
    flow.block(main.body, pinned=False)
    return reaching, flow.violations


# ─── the real install.py ────────────────────────────────────────────────


def _source() -> str:
    return INSTALL_PY.read_text(encoding="utf-8")


def test_the_audit_covers_the_env_reading_call_sites():
    """Not vacuous: the sites the audit named are all in the reaching set."""
    reaching, _ = unpinned_calls(_source())
    for name in ("_apply_deferred_entries", "_post_install_probe_phase", "_wait_for_weaviate_ready",
                 "_ensure_collections", "_maybe_prompt_rebuild_collections", "_seed_weaviate",
                 "_run_schema_migration_scripts", "_emit_orchestrator_root_schema_deferrals",
                 "_self_heal_kg_bindings_on_update"):
        assert name in reaching, name
    assert "_run_lightweight" not in reaching  # --lightweight reaches no call site at all


def test_no_install_path_reads_the_weaviate_env_before_step_5b():
    _, violations = unpinned_calls(_source())
    assert violations == [], (
        "these install.py calls can reach vco_lib.weaviate_helpers.weaviate_url_default() "
        f"BEFORE step [5b] pinned the env to the service_endpoints rows: {violations}")


def test_after_step_5b_the_weaviate_call_sites_see_the_row_not_the_shell(tmp_path, monkeypatch):
    """The other half of the contract: once [5b] ran, the resolver every call
    site uses answers the ROW even though the shell exported a stale
    WEAVIATE_URL / WEAVIATE_PORT (unroutable ``:9`` — never contacted)."""
    import argparse
    from unittest import mock

    import install
    from tests.test_v0297_service_reconcile import CASES, FakeMachine, _make_root
    from vco_lib import service_endpoints as se
    from vco_lib import service_reconcile as sr
    from vco_lib import weaviate_helpers as wh

    case = next(c for c in CASES if c["id"] == "b_native_ollama_on_upstream_port")
    root = _make_root(tmp_path, case)
    machine = FakeMachine(case.get("containers", ()), case.get("http"))
    real = sr.reconcile

    def fake_reconcile(**kw):
        kw.update(run=machine.run, fetch=machine.fetch, tcp_open=machine.tcp_open,
                  port_free=machine.port_free, env={}, db_path=tmp_path / "absent.db",
                  services_toml_path=tmp_path / "none.toml", vct_config_dirs=[],
                  ensure_db=lambda: (False, "no registry in this test"))
        return real(**kw)

    monkeypatch.setattr(sr, "reconcile", fake_reconcile)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_SERVICE_ENDPOINTS", {"rows": {}, "pinned": False, "weaviate_pending": False})
    for key in se.transport_env({}):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEAVIATE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("WEAVIATE_PORT", "9")
    assert wh.weaviate_url_default() == "http://127.0.0.1:9"  # before [5b]: the shell's value
    args = argparse.Namespace(update=True, yes=True, quiet=True, on_conflict=None, service=[])
    decisions = install._reconcile_service_endpoints(
        args, mock.Mock(container_cmd="podman", has_gpu=False), mock.Mock())
    assert decisions is not None
    row_url = se.render_url("weaviate", install._SERVICE_ENDPOINTS["rows"]["weaviate"])
    assert row_url == "http://localhost:8081"
    assert wh.weaviate_url_default() == row_url  # after [5b]: the row


# ─── red proof: the checker catches a pre-[5b] caller ────────────────────


def _insert_before(source: str, anchor: str, line: str) -> str:
    assert source.count(anchor) == 1, anchor
    return source.replace(anchor, line + anchor)


def test_a_seed_moved_above_step_5b_is_caught():
    mutated = _insert_before(_source(), "    if args.lightweight:\n",
                             "    _seed_weaviate(args, deferral_report=None)\n")
    _, violations = unpinned_calls(mutated)
    assert [name for _line, name in violations] == ["_seed_weaviate"]


def test_a_reprobe_on_the_lightweight_path_is_caught():
    """--lightweight returns before [5b]: a Weaviate read added to it would
    see the stale env."""
    mutated = _insert_before(_source(), "    _run_machine_migrations(_lightweight_deferral)",
                             "    _wait_for_weaviate_ready()\n")
    reaching, violations = unpinned_calls(mutated)
    assert "_run_lightweight" in reaching
    assert [name for _line, name in violations] == ["_run_lightweight"]


def test_the_no_containers_branch_must_pin_too():
    """Drop [5b] from the --no-containers branch: the --update re-probe after
    it (``_post_install_probe_phase``) is then reached unpinned."""
    source = _source()
    anchor = "        if _reconcile_service_endpoints(args, sysinfo, _deferral_report, runtime=None) is None:"
    assert source.count(anchor) == 1
    mutated = source.replace(anchor, "        if _reconcile_other(args, sysinfo, _deferral_report, runtime=None) is None:")
    _, violations = unpinned_calls(mutated)
    assert "_post_install_probe_phase" in [name for _line, name in violations]
