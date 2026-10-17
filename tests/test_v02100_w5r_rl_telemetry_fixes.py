# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 wave-5 review fixes on the RL telemetry path of the hook KG search.

* W5R-06 — a RESET hub connection is not retried (the hub may already have
  inserted the event; ``rl_events`` has no uniqueness), only a REFUSED one.
* W5R-07 — the hook producer prints its results BEFORE the hub POSTs, which
  go to a detached child; the dual-log secondary embed no longer re-embeds the
  active slot. Process-level latency is measured against a hung hub.
* W5R-08 — a twin that was wanted but comes out empty or partial is recorded
  in the loss ledger (not a DEBUG line).
* W5R-14 — each hook tags its events with its own task_type.
* W5R-15 — ledger rotation + append are serialized ACROSS processes.

Behavioural throughout: the real flow runs against the service-free fakes of
``tests/common/rl_kg_search_harness.py``, real subprocesses, or a local socket.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
HOOKS = REPO_ROOT / "templates" / "hooks"
for _p in (str(REPO_ROOT), str(MCP_DIR), str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common import rl_kg_search_harness as H  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402

pytest.importorskip("weaviate_mcp.server")

# Reuse the dual-log suite's fixtures (one home for the rig). Bound by
# assignment, not imported, so a test parameter of the same name is pytest's
# fixture injection and not a shadowed import.
from tests import test_v02100_dual_rl_hook_logging as _base  # noqa: E402

DUAL_ENV = _base.DUAL_ENV
EV = _base.EV
_loss_lines = _base._loss_lines
_clean_ledger = _base._clean_ledger
live_poster = _base.live_poster
project = _base.project
world = _base.world


# ---------------------------------------------------------------------------
# W5R-06 — no double insert on a reset connection
# ---------------------------------------------------------------------------


def test_reset_connection_is_recorded_without_retry(live_poster, monkeypatch):
    monkeypatch.setattr(live_poster, "_read_hub_token", lambda: "tok")
    calls = []

    def _reset(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError(ConnectionResetError(104, "reset by peer"))

    monkeypatch.setattr(live_poster.urllib.request, "urlopen", _reset)
    assert live_poster.post_rl_event(dict(EV)) is False
    assert len(calls) == 1, "a reset may follow a committed INSERT: never resend"
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [
        ("hub_post_failed", "connection_reset")
    ]


def test_reset_after_commit_stores_the_event_once(live_poster, monkeypatch, tmp_path):
    """End to end against a real socket: a hub that reads the whole request
    (i.e. could have committed it) and then resets the connection must see
    exactly ONE request. (urllib raises a reset in ``getresponse`` unwrapped,
    so this pins the real-socket behaviour; the URLError-wrapped reset — the
    case the retry used to catch — is the test above.)"""
    seen = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _serve():
        srv.settimeout(5)
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(2)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, body = data.partition(b"\r\n\r\n")
                length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0].split(b":")[1])
                while len(body) < length:
                    body += conn.recv(65536)
                seen.append(body)
            finally:
                # RST instead of a response: SO_LINGER 0 then close.
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    monkeypatch.setattr(live_poster, "_read_hub_token", lambda: "tok")
    monkeypatch.setattr(live_poster, "_read_hub_port", lambda: port)
    try:
        assert live_poster.post_rl_event(dict(EV)) is False
    finally:
        srv.close()
    assert len(seen) == 1, f"the event reached the hub {len(seen)} times"


# ---------------------------------------------------------------------------
# W5R-07 — output first, POSTs off the critical path, secondary-only embed
# ---------------------------------------------------------------------------


def test_hook_prints_before_any_event_is_sent(world, monkeypatch, capsys):
    mod, state = world
    order = []
    # Render the results (any non-discard tier) and log when each line prints.
    import weaviate_mcp.server as srv_mod

    monkeypatch.setattr(srv_mod, "_get_result_verbosity_by_score", lambda s: "summary")
    monkeypatch.setattr(
        srv_mod, "_format_result_by_tier",
        lambda r, tier, sidecar_db=None, coll=None: {"title": r["title"], "node_type": "concept",
                                                     "content": "c", "file_path": r["file_path"]},
    )
    real_print = print

    def _spy_print(*a, **k):
        order.append("print")
        real_print(*a, **k)

    monkeypatch.setattr("builtins.print", _spy_print)
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cap = sp.emit_rl_event

    def _ordered(ev, *, writer_factory=None):
        order.append("emit:" + ev.task_id)
        return cap(ev, writer_factory=writer_factory)

    monkeypatch.setattr(sp, "emit_rl_event", _ordered)
    H.run_main(mod)
    emits = [i for i, x in enumerate(order) if x.startswith("emit:")]
    prints = [i for i, x in enumerate(order) if x == "print"]
    assert prints and len(emits) == 2, order
    assert max(prints) < min(emits), f"an event was sent before the output: {order}"


def test_dual_secondary_embed_skips_the_active_slot(world):
    mod, state = world
    H.run_main(mod)
    assert state["svc"].include_active == [False], (
        "the active vector is already in hand; re-embedding it cost the 1 s budget"
    )
    assert [e["task_id"].endswith(":arctic") for e in state["emitted"]] == [False, True]


def test_pre_tool_use_twin_embed_has_the_tighter_budget(world, monkeypatch):
    """pre-tool-use is synchronous under a 3 s harness timeout: a hung secondary
    embed may cost it at most PRE_TOOL_USE_DUAL_EMBED_BUDGET_S, and the skip is
    recorded under its own task_type."""
    mod, state = world
    state["svc"].delay_s = 3.0
    t0 = time.monotonic()
    H.run_main(mod, task_type="pre_tool_use_kg_search")
    elapsed = time.monotonic() - t0
    # Strictly tighter than the other hooks' cap (a hung embed costs those 1 s).
    assert elapsed < 0.8 * mod.HOOK_DUAL_EMBED_BUDGET_S, elapsed
    assert [(x["kind"], x["reason"], x["task_type"]) for x in _loss_lines()] == [
        ("dual_skip", "secondary_embed_timeout", "pre_tool_use_kg_search")]
    assert len(state["emitted"]) == 1, "the primary event is unaffected"


def test_hand_off_detaches_and_the_child_sends_both_events(tmp_path, monkeypatch):
    """The detached child is started with this interpreter + the module file,
    and that child — run here in the foreground — delivers primary and twin to
    the hub through the real writer path."""
    from claude_mcp_servers.rl_client import deferred_emit as de
    from claude_mcp_servers.rl_client.telemetry_emit import RetrievalEvent

    node = {"title": "N1", "score": 0.9, "emb": [0.1] * 4, "n_emb": [0.1] * 4}
    items = [
        de.DeferredEmit(RetrievalEvent(query="q", query_emb=[0.1] * 4, embedding_source="qwen3",
                                       embedding_dim=4, embedding_model="m", nodes=[node],
                                       task_id="pre_edit_t1", task_type="pre_edit_kg_search")),
        de.DeferredEmit(RetrievalEvent(query="q", query_emb=[0.2] * 4, embedding_source="arctic",
                                       embedding_dim=4, embedding_model="a", nodes=[node],
                                       task_id="pre_edit_t1:arctic", task_type="pre_edit_kg_search"),
                        ("arctic", 4, "a")),
    ]
    started = {}

    class _P:
        def __init__(self, argv, **kw):
            started["argv"] = argv
            started["kw"] = kw

    monkeypatch.setattr(de, "_can_detach", lambda: True)
    # The PARENT resolves each event's writer: the project's default writer
    # for the primary, the other slot's for the twin. Pin both resolvers to
    # real writers with a known identity, so this test does not depend on
    # whatever writer cache / project config earlier suites left behind.
    import importlib

    from claude_mcp_servers.rl_client import telemetry_emit
    from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter

    def _w(src, dim, model):
        return RLTelemetryWriter(project="proja", project_id="pid-a", embedding_source=src,
                                 embedding_dim=dim, embedding_model=model)

    monkeypatch.setattr(telemetry_emit, "_default_writer_factory", lambda: _w("qwen3", 4, "m"))
    srv_alias = importlib.import_module("claude_mcp_servers.weaviate_mcp.server")
    monkeypatch.setattr(srv_alias, "_get_rl_telemetry_writer_for",
                        lambda src, embedding_dim=0, embedding_model="": _w(src, embedding_dim, embedding_model))
    from types import SimpleNamespace

    monkeypatch.setattr(de, "subprocess", SimpleNamespace(Popen=_P, DEVNULL=subprocess.DEVNULL))
    assert de.hand_off(items) == "detached"
    argv = started["argv"]
    assert argv[0] == sys.executable and Path(argv[1]) == Path(de.__file__).resolve()
    assert started["kw"]["stdout"] is subprocess.DEVNULL
    assert started["kw"].get("start_new_session") or started["kw"].get("creationflags")
    payload_path = Path(argv[2])
    assert payload_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600

    # A recording hub on a local port.
    got = []

    class _Hub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            # Record only the RL event route: the child may also reach other
            # hub routes (seen on CI), which are not what this test measures.
            if self.path.rstrip("/").endswith("/api/v1/rl/events"):
                got.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):  # noqa: A002,D102 — silence the base logger
            pass

    hub = http.server.HTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    state_dir = tmp_path / "vct"
    state_dir.mkdir()
    (state_dir / "hub.token").write_text("tok")
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    env = child_env(**{
        "VCT_STATE_DIR": str(state_dir),
        "VCT_HUB_PORT": str(hub.server_address[1]),
        "VCT_HUB_ALLOW_TEST_POST": "1",
        "VCT_DISABLE_HUB_RESOLVER": "1",
        "CLAUDE_PROJECT_DIR": str(proj),
        "PROJECT_NAME": "ProjA",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    })
    try:
        proc = subprocess.run(argv, env=env, cwd=str(proj), capture_output=True,
                              text=True, timeout=120)
    finally:
        hub.shutdown()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not payload_path.exists(), "the child deletes its payload"
    ids = sorted(e.get("task_id") for e in got)
    assert ids == ["pre_edit_t1", "pre_edit_t1:arctic"], (ids, proc.stderr[-2000:])
    # The twin went out through the OTHER slot's writer identity, resolved in
    # the parent and rebuilt in the child.
    by_id = {e.get("task_id"): e for e in got}
    assert by_id["pre_edit_t1:arctic"].get("embedding_source") == "arctic", by_id
    assert by_id["pre_edit_t1"].get("embedding_source") == "qwen3", by_id
    assert {(e.get("project_id"), e.get("project_name")) for e in got} == {("pid-a", "proja")}, got


def test_hand_off_falls_back_inline_when_no_child_can_start(monkeypatch):
    from claude_mcp_servers.rl_client import deferred_emit as de

    sent = []
    monkeypatch.setattr(de, "_can_detach", lambda: True)

    def _boom(payload):
        raise OSError("no fork for you")

    monkeypatch.setattr(de, "_spawn_child", _boom)
    item = de.DeferredEmit(event=object())
    assert de.hand_off([item], emit=lambda ev, writer_factory=None: sent.append(ev) or True) == "inline"
    assert sent == [item.event], "events are sent inline, never dropped"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX timing rig")
def test_hook_process_latency_with_a_hung_hub(tmp_path):
    """Process-level: the producer's wall time must not include the hub POSTs.

    ``inline`` reproduces the pre-fix order (send, then exit) with every POST
    taking 2 s (the hub timeout); ``detached`` is the shipped path. The fake
    emit in the parent is the same in both, so the difference is exactly the
    POST time moved off the critical path."""
    timings = {}
    runs = (
        ("inline", False, 0.05, "pre_edit_kg_search"),
        ("detached", True, 0.05, "pre_edit_kg_search"),
        # pre-tool-use's worst case: hub wedged AND secondary embed hung.
        ("pre_tool_use_worst", True, 30.0, "pre_tool_use_kg_search"),
    )
    for label, real_detach, embed_delay, task_type in runs:
        res_path = tmp_path / f"res_{label}.json"
        cfg = {
            "collections": ["ProjA_KnowledgeGraph"],
            "embed_delay_s": embed_delay,
            "emit_delay_s": 2.0,
            "real_detach": real_detach,
            "task_type": task_type,
            "result_path": str(res_path),
        }
        cfg_path = tmp_path / f"cfg_{label}.json"
        cfg_path.write_text(json.dumps(cfg))
        env = child_env(**{**DUAL_ENV, "CLAUDE_PROJECT_DIR": str(tmp_path)})
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests/common/rl_kg_search_harness.py"), str(cfg_path)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        timings[label] = time.monotonic() - t0
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = json.loads(res_path.read_text())
        timings[label + "_main"] = out["main_elapsed_s"]
        if label == "pre_tool_use_worst":
            assert out["handed_off"] == 1, out  # primary only: the twin was over budget
        elif real_detach:
            assert out["handed_off"] == 2, out
        else:
            assert len(out["emitted"]) == 2
    print(f"\nHOOK-LATENCY-W5R07 {json.dumps({k: round(v, 3) for k, v in timings.items()})}")
    assert timings["inline_main"] >= 4.0, timings  # 2 POSTs x 2 s on the critical path
    assert timings["detached_main"] < 1.0, timings
    assert timings["inline"] - timings["detached"] >= 3.5, timings
    # The worst case (wedged hub AND hung secondary) costs pre-tool-use only
    # its 0.4 s embed cap over the fast detached run. The cap is wall-clock;
    # the process overhead around it is not — under a loaded machine (a full
    # suite beside a cargo build) the detached run itself went 0.08 s -> 0.31 s
    # and this leg 0.43 s -> 1.2 s. So the slack scales with the measured
    # detached baseline instead of being a fixed number; a hung secondary
    # (30 s) or a POST back on the critical path (2 s each) still fails it.
    slack = 0.5 + 3 * timings["detached_main"]
    assert timings["pre_tool_use_worst_main"] < 0.4 + slack, timings
    assert timings["pre_tool_use_worst"] - timings["detached"] < 0.4 + 0.5, timings


# ---------------------------------------------------------------------------
# W5R-08 — wanted-but-empty / partial twins are recorded
# ---------------------------------------------------------------------------


def _req(candidates):
    from claude_mcp_servers.rl_client.search_pipeline import RerankRequest

    return RerankRequest(query="q", candidates=candidates, limit=3, query_emb=[0.1] * 4,
                         embedding_source="qwen3", embedding_dim=4, embedding_model="m",
                         task_id="pre_edit_x", task_type="pre_bash_kg_search", dual_log=True,
                         other_query_emb=[0.2] * 4, other_embedding_source="arctic",
                         other_embedding_dim=4, other_embedding_model="a")


def test_twin_with_no_other_slot_vectors_is_recorded():
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cands = [{"title": "A", "score": 0.9, "emb": [0.1] * 4}, {"title": "B", "score": 0.8, "emb": [0.1] * 4}]
    assert sp._build_other_slot_event("pre_edit_x", _req(cands)) is None
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [("dual_skip", "no_other_slot_vectors")]
    assert lines[0]["task_type"] == "pre_bash_kg_search" and lines[0]["nodes"] == 2


def test_partial_twin_is_recorded_with_the_missing_count():
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cands = [
        {"title": "A", "score": 0.9, "emb": [0.1] * 4, "emb_other": [0.2] * 4},
        {"title": "B", "score": 0.8, "emb": [0.1] * 4},
        {"title": "C", "score": 0.7, "emb": [0.1] * 4},
    ]
    twin = sp._build_other_slot_event("pre_edit_x", _req(cands))
    assert twin is not None and len(twin.event.nodes) == 1
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"], line["missing"], line["nodes"]) == (
        "dual_partial", "missing_other_slot_vectors", 2, 3)


def test_full_twin_records_nothing():
    from claude_mcp_servers.rl_client import search_pipeline as sp

    cands = [{"title": "A", "score": 0.9, "emb": [0.1] * 4, "emb_other": [0.2] * 4}]
    assert sp._build_other_slot_event("pre_edit_x", _req(cands)) is not None
    assert _loss_lines() == []


def test_doctor_reports_partial_twins_apart_and_the_blind_spot():
    from vco_lib import doctor
    from vco_lib.rl_telemetry_loss import record_loss

    record_loss("dual_partial", "missing_other_slot_vectors", missing=3, nodes=5)
    record_loss("hub_post_failed", "timeout")
    (f,) = doctor.probe_rl_telemetry_loss(Path("/tmp/x"), doctor.DoctorResolvers(), {})
    assert "1 RL training event(s) lost" in f.summary, f.summary
    assert "1 dual-log twin(s) partial (3 node(s)" in f.summary, f.summary
    assert "killed by its timeout" in f.summary


# ---------------------------------------------------------------------------
# W5R-14 — each hook tags its own task_type
# ---------------------------------------------------------------------------


def test_task_type_resolution(monkeypatch):
    mod = H.import_rl_kg_search()
    monkeypatch.delenv("VCO_RL_TASK_TYPE", raising=False)
    assert mod.resolve_task_type(None) == "cli_kg_search"
    monkeypatch.setenv("VCO_RL_TASK_TYPE", "pre_bash_kg_search")
    assert mod.resolve_task_type(None) == "pre_bash_kg_search"
    assert mod.resolve_task_type("subagent_kg_search") == "subagent_kg_search"
    monkeypatch.setenv("VCO_RL_TASK_TYPE", "rm -rf")
    assert mod.resolve_task_type(None) == "cli_kg_search"


def test_events_and_ids_carry_the_task_type(world):
    mod, state = world
    H.run_main(mod, task_type="pre_tool_use_kg_search")
    assert {e["task_type"] for e in state["emitted"]} == {"pre_tool_use_kg_search"}
    assert state["emitted"][0]["task_id"].startswith("pre_tool_use_")


# v0.2.101: the router loads this producer IN-PROCESS with a PINNED argparse
# (hook_dual_search._pin_argv), so sys.argv does NOT carry the producer's
# flags — the stub must parse them the same way the real rl_kg_search does.
# The VCO_RL_TASK_TYPE env fallback covered the legacy direct-spawn hooks;
# the last of them (pre-tool-use §5) was retired in wave-3, so every live
# task-type now travels as the router's --task-type argv (the env fallback
# remains in resolve_task_type as documented behaviour).
_TT_PRODUCER = (
    "import argparse, os\n"
    "\n"
    "def main(argv=None):\n"
    "    ap = argparse.ArgumentParser()\n"
    "    ap.add_argument('query')\n"
    "    ap.add_argument('--limit', type=int, default=3)\n"
    "    ap.add_argument('--hook-format', action='store_true')\n"
    "    ap.add_argument('--injection-profile')\n"
    "    ap.add_argument('--task-type')\n"
    "    ap.add_argument('--transcript')\n"
    "    a = ap.parse_args(argv)\n"
    "    tt = a.task_type or os.environ.get('VCO_RL_TASK_TYPE', '')\n"
    "    print('KG: probe tt=' + tt + ' | concept | score=0.90 | FULL NODE:')\n"
    "    print('body')\n"
    "    print('KG: probe2 tt=' + tt + ' | concept | score=0.80 | FULL NODE:')\n"
    "    print('body2')\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    main()\n"
)


def _tt_rig(tmp_path):
    orch = tmp_path / "orch"
    (orch / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    vb = orch / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(shutil.which("python3") or sys.executable, vb / "python")
    (orch / "claude_mcp_servers" / "scripts" / "rl_kg_search.py").write_text(_TT_PRODUCER)
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "logs").mkdir(parents=True)
    target = proj / "notes.md"
    target.write_text("x\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCT_ORCHESTRATOR_ROOT", "VCO_RL_TASK_TYPE",
                        "VCO_INJECT_PROFILE")}
    env.update({"CLAUDE_PROJECT_DIR": str(proj), "VCT_INSTALL_ROOT": str(orch),
                "VCT_STATE_DIR": str(tmp_path / "vctstate"),
                "HOME": str(tmp_path / "home"),
                # v0.2.101: the injection wrappers are thin router drivers —
                # the REAL router resolves from this checkout while the KG
                # producer stays this rig's stub. (VCT_INSTALL_ROOT above
                # still wins for the legacy rows' rl_kg_search resolution,
                # so pre-tool-use is untouched.)
                "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
                "VCO_ROUTER_KG_SCRIPT": str(orch / "claude_mcp_servers/scripts/rl_kg_search.py")})
    return orch, proj, target, env


_HOOK_CASES = [
    ("pre-edit-context-inject", "pre_edit_kg_search",
     lambda t: {"tool_name": "Edit", "session_id": "s-tt1",
                "tool_input": {"file_path": str(t), "new_string": "widget reranker notes\n"}}),
    # v0.2.101 §C1: the trigger must be READ-classified now — a MECHANICAL
    # command (pytest/build/test) spawns no producer at all by design.
    ("pre-bash-context-inject", "pre_bash_kg_search",
     lambda t: {"tool_name": "Bash", "session_id": "s-tt2",
                "tool_input": {"command": "cat tests/test_widget_reranker.py"}}),
    # v0.2.101 wave-3 (review nit-6): the pre-tool-use row was RETIRED with
    # its §5 KG-suggestion branch — Edit/Write KG context is the pre-edit/
    # pre-write router wrappers' one home now (their task types are pinned by
    # the two rows above). pre_tool_use_kg_search STAYS registered in
    # KNOWN_TASK_TYPES: the historical RL corpus keeps its partition label.
    # v0.2.101 §C4/§C5: subagent-start-kg-inject's KG half was RETIRED (the
    # SubagentStart payload carries no prompt — the old query could never
    # fire). Its successor surface is the agent-brief PreToolUse hook; the
    # task_type now travels as the router's --task-type argv (read by this
    # stub from sys.argv, below) instead of a hook-exported VCO_RL_TASK_TYPE.
    ("agent-brief-kg-inject", "agent_brief_kg_search",
     lambda t: {"tool_name": "Agent", "session_id": "s-tt4", "prompt_id": "p-tt4",
                "tool_input": {"prompt": "Task: implement the widget reranker",
                               "description": "coder lane", "model": "m"}}),
]


@pytest.mark.parametrize("shell", ["sh", "ps1"])
@pytest.mark.parametrize("hook,expected,payload", _HOOK_CASES, ids=[c[0] for c in _HOOK_CASES])
def test_each_hook_tags_its_own_task_type(tmp_path, shell, hook, expected, payload):
    if shell == "sh" and (sys.platform == "win32" or shutil.which("bash") is None):
        pytest.skip("bash hook")
    if shell == "ps1" and (shutil.which("pwsh") is None or sys.platform == "win32"):
        pytest.skip("pwsh on POSIX")
    orch, proj, target, env = _tt_rig(tmp_path)
    argv = (["bash", str(HOOKS / f"{hook}.sh")] if shell == "sh"
            else ["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / f"{hook}.ps1")])
    if hook == "pre-tool-use":
        argv += ["Edit", ""]
    if hook == "agent-brief-kg-inject":
        # Thin router wrapper: the REAL router comes from this checkout; the
        # stub producer above stays the rig's own (VCO_ROUTER_KG_SCRIPT seam).
        env["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
        env["VCO_ROUTER_KG_SCRIPT"] = str(orch / "claude_mcp_servers/scripts/rl_kg_search.py")
    proc = subprocess.run(argv, input=json.dumps(payload(target)), capture_output=True,
                          text=True, env=env, cwd=str(orch), timeout=120)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert f"tt={expected}" in proc.stdout, (proc.stdout, proc.stderr[-1500:])


# ---------------------------------------------------------------------------
# W5R-15 — rotation + append are serialized across processes
# ---------------------------------------------------------------------------

_WRITER = (
    "import sys\n"
    "import vco_lib.rl_telemetry_loss as m\n"
    "m._MAX_BYTES = 1\n"          # rotate on every append
    "m._KEEP_LINES = 10 ** 6\n"   # ...keeping everything: any lost line is a race
    # v0.2.104: a lock wait this short times out on ANY machine under four
    # writers, so the lock-busy path runs on every pass, not only when the
    # host happens to be loaded (where the pre-fix unlocked append lost lines).
    "m._LOCK_TIMEOUT_S = 0.01\n"
    "for i in range(int(sys.argv[2])):\n"
    "    m.record_loss('dual_skip', 'race', seq=sys.argv[1] + '-' + str(i))\n"
)

_READ_SEQS = (
    "import json, sys\n"
    "import vco_lib.rl_telemetry_loss as m\n"
    "print(json.dumps([r['seq'] for r in m.iter_records()]))\n"
)

_LEDGER_SEQS_AFTER_ONE_MORE = (
    "import json, sys\n"
    "import vco_lib.rl_telemetry_loss as m\n"
    "m.record_loss('dual_skip', 'race', seq='final')\n"
    "print(json.dumps([json.loads(x)['seq'] for x in m.loss_log_path().read_text().splitlines()]))\n"
)


def _child_json(code, state):
    """Run ``code`` in a child Python against the state dir ``state``; its
    stdout is JSON."""
    proc = subprocess.run([sys.executable, "-c", code],
                          env=child_env(VCT_STATE_DIR=str(state)), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@pytest.mark.skipif(sys.platform == "win32", reason="rotation by os.replace of a file other "
                    "writers reopen is the POSIX shape; the lock-busy spill is pinned "
                    "cross-platform by the deterministic test below")
def test_rotation_does_not_drop_lines_written_by_other_processes(tmp_path):
    state = tmp_path / "vct"
    env = child_env(VCT_STATE_DIR=str(state))
    n_proc, per = 4, 150
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(k), str(per)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for k in range(n_proc)
    ]
    for p in procs:
        p.wait(timeout=120)
        assert p.returncode == 0, (p.stderr.read() if p.stderr else b"")[-2000:]
    seqs = _child_json(_READ_SEQS, state)
    assert len(seqs) == len(set(seqs)), "a spilled line was counted twice"
    assert len(set(seqs)) == n_proc * per, (
        f"{n_proc * per - len(set(seqs))} line(s) lost to a rotation race")
    # The next locked append folds every spilled line into the ledger itself.
    ledger = _child_json(_LEDGER_SEQS_AFTER_ONE_MORE, state)
    assert sorted(ledger) == sorted(seqs + ["final"])


_BUSY_WRITER = (
    "import vco_lib.rl_telemetry_loss as m\n"
    "m._LOCK_TIMEOUT_S = 0.05\n"
    "m.record_loss('hub_post_failed', 'refused', seq='while-busy')\n"
)


def test_a_writer_that_finds_the_lock_busy_never_writes_into_a_rotation(tmp_path):
    """v0.2.104 §I: the exact interleaving that lost lines, made deterministic.

    The test process holds the ledger lock and is MID-ROTATION — it has read
    the tail and not yet replaced the file. A writer in another process times
    out on the lock. Pre-fix that writer appended to the ledger unlocked, into
    the inode the rotation then replaced: the line was gone. Now it spills to
    a pending file, which readers count and the next locked append drains.
    """
    from vco_lib import rl_telemetry_loss as m
    from vco_lib.atomic import atomic_write_text, exclusive_file_lock

    state = tmp_path / "vct"
    env = child_env(VCT_STATE_DIR=str(state))
    ledger = state / "metrics" / m.LOSS_FILE_NAME
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"ts_ms":1,"kind":"dual_skip","reason":"r","seq":"old"}\n')

    with exclusive_file_lock(m.lock_path(ledger)):
        tail = ledger.read_text(encoding="utf-8")          # rotation: tail read
        proc = subprocess.run([sys.executable, "-c", _BUSY_WRITER], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        atomic_write_text(ledger, tail, fsync=False)       # rotation: replace

    assert _child_json(_READ_SEQS, state) == ["old", "while-busy"]
    assert m.summarize(path=ledger)["by_kind"] == {
        "dual_skip": {"r": 1}, "hub_post_failed": {"refused": 1}}
    # Drained by the next writer that holds the lock: into the ledger, once.
    assert _child_json(_LEDGER_SEQS_AFTER_ONE_MORE, state) == ["old", "while-busy", "final"]
    assert not list(m.pending_dir(ledger).glob("*.jsonl"))
    assert _child_json(_READ_SEQS, state) == ["old", "while-busy", "final"]


def test_episode_bump_finds_its_line_when_it_was_spilled(tmp_path, monkeypatch):
    """v0.2.104 §I: the hub-down episode's in-place ``suppressed`` bump is the
    ledger's other rewriter. It drains spilled lines under the lock first, so
    an episode line that was spilled (lock busy at record time) is still
    bumped — and kept — rather than missed."""
    from claude_mcp_servers.rl_client import hub_writer
    from vco_lib import rl_telemetry_loss as m

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    ledger = m.loss_log_path()
    m._spill(ledger, json.dumps({"ts_ms": 1, "kind": "hub_post_failed",
                                 "reason": "refused", "episode": "ep-1"},
                                separators=(",", ":")) + "\n")
    assert not ledger.exists()
    hub_writer._bump_episode_ledger_line("ep-1", 7)
    recs = [json.loads(x) for x in ledger.read_text().splitlines()]
    (sid,) = [r.pop(m.SPILL_ID_KEY) for r in recs]  # its idempotency key survives
    assert sid
    assert recs == [{"ts_ms": 1, "kind": "hub_post_failed", "reason": "refused",
                     "episode": "ep-1", "suppressed": 7}]
    assert [{k: v for k, v in r.items() if k != m.SPILL_ID_KEY}
            for r in m.iter_records()] == recs
