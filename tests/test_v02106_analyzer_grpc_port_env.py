# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.106: the code-graph analyzer honours ``GRPC_PORT``.

It used to default its gRPC port to a literal 50052 and pass it explicitly to
``weaviate_helpers.connect_v4``, which defeated the helper's own ``$GRPC_PORT``
resolution: an analyzer pointed at a Weaviate on another gRPC port read from the
DEFAULT one (found when a throwaway instance's reads went to the live port).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_analyzer():
    path = ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("_acg_grpc_probe", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_connect_resolves_the_port_from_the_environment(monkeypatch):
    acg = _load_analyzer()
    seen = {}

    def fake_connect(url, grpc_port=None, **_kw):
        from vco_lib import weaviate_helpers
        import os
        seen["passed"] = grpc_port
        seen["effective"] = grpc_port if grpc_port is not None else int(
            os.environ.get("GRPC_PORT", weaviate_helpers.DEFAULT_GRPC_PORT))
        raise RuntimeError("stop after recording the port")

    monkeypatch.setenv("GRPC_PORT", "18903")
    monkeypatch.setattr(acg._wh, "connect_v4", fake_connect)
    analyzer = acg.CodeGraphAnalyzer("GrpcProbe", weaviate_url="http://127.0.0.1:9")
    try:
        analyzer.connect()
    except Exception:  # noqa: BLE001 — the fake stops the connect on purpose
        pass
    assert seen, "the analyzer never reached connect_v4"
    assert seen["passed"] is None, "an explicit port overrides $GRPC_PORT"
    assert seen["effective"] == 18903
