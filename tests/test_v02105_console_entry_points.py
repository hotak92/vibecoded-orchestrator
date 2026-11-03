# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 (Windows field): ``code-graph-analyze`` died with
``'charmap' codec can't encode character '✅'`` and the user was told
"Failed to connect to Weaviate".

Two defects, two pins:

1. Every Python entry point a shipped ``templates/scripts/*.ps1`` wrapper runs
   makes its streams console-safe through the ONE home,
   :func:`vco_lib.console_text.harden_stdio`, before it prints. Pinned
   BEHAVIOURALLY per wrapper: the target is run as ``__main__`` in a child
   whose stdout is a cp1252 pipe and whose platform is simulated as Windows
   at that one home; whatever it prints afterwards must arrive as ASCII.
   The wrapper list is discovered from the ``.ps1`` files, so a new wrapper
   is covered without editing this test.
2. Only a transport failure reads "Failed to connect to Weaviate"
   (:func:`vco_lib.weaviate_helpers.describe_connect_failure`), and the
   analyzer's success line is no longer inside the connect ``try``.
"""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import weaviate_helpers as wh

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "templates" / "scripts"
ANALYZER = SCRIPTS / "analyze_code_graph.py"

#: Wrapper targets that run under the SYSTEM interpreter with no venv (their
#: wrappers say "Pure-stdlib Python - no venv required"), so vco_lib is not
#: guaranteed importable there. They are pinned differently: every character
#: in them encodes in cp1252, so they cannot raise on a cp1252 stream.
STDLIB_ONLY = {"detect_workflow_needs.py", "generate_workflow.py"}

_SCRIPT_TARGET = re.compile(r'Join-Path \$ScriptDir "([A-Za-z0-9_]+\.py)"')
_MODULE_TARGET = re.compile(r"-m (vco_lib\.[a-z_]+)")


def _wrapper_targets() -> list[tuple[str, str]]:
    """(wrapper, target) for every Python entry point a .ps1 wrapper runs."""
    out: set[tuple[str, str]] = set()
    for ps1 in sorted(SCRIPTS.glob("*.ps1")):
        text = ps1.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in _SCRIPT_TARGET.finditer(line):
                out.add((ps1.name, m.group(1)))
            for m in _MODULE_TARGET.finditer(line):
                out.add((ps1.name, m.group(1)))
    return sorted(out)


TARGETS = _wrapper_targets()


def test_the_wrapper_scan_finds_the_known_entry_points():
    names = {t for _, t in TARGETS}
    # A scan that silently matched nothing would make the parametrized test
    # below vacuous; these are the field-relevant ones.
    assert {"analyze_code_graph.py", "query_code_graph.py", "sync_knowledge_graph.py",
            "vco_lib.codegraph_walk", "vco_lib.kg_dedup"} <= names, names


#: Simulated Windows at the one home (faking sys.platform for the whole
#: interpreter breaks stdlib imports); stdout/stderr are cp1252 pipes.
_CHILD = textwrap.dedent(r"""
    import io, runpy, sys
    import vco_lib.console_text as _ct
    _orig = _ct.harden_stdio
    _ct.harden_stdio = lambda *a, **k: _orig(*a, **{**k, "platform": "win32"})
    out = io.BytesIO()
    sys.stdout = io.TextIOWrapper(out, encoding="cp1252", errors="strict", write_through=True)
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace",
                                  write_through=True)
    target, kind = sys.argv[1], sys.argv[2]
    sys.argv = [target, "--vco-no-such-flag-xyz"]
    try:
        if kind == "module":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(target, run_name="__main__")
    except SystemExit:
        pass
    print("after the entry point: step 7 — pull → ok ✅")
    sys.stdout.flush()
    sys.__stdout__.buffer.write(out.getvalue())
""")


def _run_entry(target: str) -> bytes:
    kind = "module" if target.startswith("vco_lib.") else "path"
    path = target if kind == "module" else str(SCRIPTS / target)
    env = child_env(WEAVIATE_URL="http://127.0.0.1:9")
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'claude_mcp_servers'}{':' if sys.platform != 'win32' else ';'}{REPO_ROOT}"
    res = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _CHILD, path, kind],
        capture_output=True, timeout=180, env=env, cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")[-3000:]
    return res.stdout


@pytest.mark.parametrize(
    "wrapper,target",
    [t for t in TARGETS if t[1] not in STDLIB_ONLY],
    ids=[f"{w}->{t}" for w, t in TARGETS if t not in STDLIB_ONLY],
)
def test_every_wrapper_entry_point_hardens_its_streams(wrapper, target):
    """Pre-fix (analyze_code_graph.py): the cp1252 pipe kept its strict
    encoding, the child's final print raised UnicodeEncodeError on the arrow
    and the child exited non-zero — the field crash."""
    out = _run_entry(target)
    assert out.isascii(), out[-400:]
    assert b"after the entry point: step 7 - pull -> ok [ok]" in out, (wrapper, out[-400:])


@pytest.mark.parametrize("target", sorted(STDLIB_ONLY))
def test_stdlib_only_targets_cannot_raise_on_cp1252(target):
    src = (SCRIPTS / target).read_text(encoding="utf-8")
    bad = sorted({ch for ch in src if not _cp1252_ok(ch)})
    assert not bad, f"{target} carries characters cp1252 cannot encode: {bad}"
    assert target in {t for _, t in TARGETS}


def _cp1252_ok(ch: str) -> bool:
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


# ── error attribution ─────────────────────────────────────────────────────


class WeaviateStartUpError(Exception):
    """Same class NAME as the weaviate-client readiness failure."""


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(111, "Connection refused"),
    WeaviateStartUpError("Could not connect to Weaviate:Connection to Weaviate failed."),
    TimeoutError("timed out"),
    RuntimeError("Connect call failed ('127.0.0.1', 8081)"),
])
def test_transport_failures_read_as_connect_failures(exc):
    msg = wh.describe_connect_failure(exc, "http://localhost:8081")
    assert msg.startswith("Failed to connect to Weaviate at http://localhost:8081"), msg


@pytest.mark.parametrize("exc", [
    UnicodeEncodeError("charmap", "✅", 0, 1, "character maps to <undefined>"),
    ValueError("invalid grpc_port"),
    TypeError("connect_to_custom() got an unexpected keyword argument"),
])
def test_other_exceptions_never_read_as_connect_failures(exc):
    msg = wh.describe_connect_failure(exc, "http://localhost:8081")
    assert "Failed to connect" not in msg
    assert type(exc).__name__ in msg and "not a connection failure" in msg


@pytest.fixture(scope="module")
def acg():
    spec = importlib.util.spec_from_file_location("acg_v02105_console", ANALYZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _analyzer(acg):
    a = acg.CodeGraphAnalyzer.__new__(acg.CodeGraphAnalyzer)
    a.weaviate_url = "http://localhost:8081"
    a.grpc_port = 50052
    a.client = None
    return a


def test_a_success_line_that_cannot_print_is_not_a_connect_failure(acg, monkeypatch):
    """The field shape: connect succeeds, the "✅ Connected" print raises on a
    strict cp1252 stream. Pre-fix the print sat inside the connect ``try``,
    so connect() returned False after printing "Failed to connect to
    Weaviate: 'charmap' codec can't encode...". Now the encode error is not
    swallowed into a false diagnosis (and harden_stdio, called at the
    analyzer's entry, keeps it from happening at all)."""
    monkeypatch.setattr(acg._wh, "connect_v4", lambda *a, **k: object())
    err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                                       errors="strict", write_through=True))
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(UnicodeEncodeError):
        _analyzer(acg).connect()
    assert "Failed to connect" not in err.getvalue()


def test_analyzer_connect_reports_a_refusal_as_a_connect_failure(acg, monkeypatch, capsys):
    def _refuse(*_a, **_k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(acg._wh, "connect_v4", _refuse)
    assert _analyzer(acg).connect() is False
    assert "Failed to connect to Weaviate at http://localhost:8081" in capsys.readouterr().err


def test_analyzer_connect_names_a_non_transport_error_honestly(acg, monkeypatch, capsys):
    def _bad(*_a, **_k):
        raise ValueError("invalid grpc_port")

    monkeypatch.setattr(acg._wh, "connect_v4", _bad)
    assert _analyzer(acg).connect() is False
    err = capsys.readouterr().err
    assert "Failed to connect" not in err and "ValueError" in err
