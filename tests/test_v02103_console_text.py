# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (Windows field): em dashes printed as garbage in a cp1252
console (install output, the code-graph walk's "already running" refusal).

``vco_lib.console_text.harden_stdio`` is the one home for the per-stream
choice on Windows: a real console keeps Unicode, a reader that declared
UTF-8 gets UTF-8, any other pipe/file gets transliterated ASCII. Windows is
simulated by ``sys.platform = "win32"`` in a child interpreter whose stdout /
stderr are byte buffers (a pipe, as the field's PowerShell wrappers see it).
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import console_text as ct

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeRaw:  # stands in for io._WindowsConsoleIO by name
    pass


_FakeRaw.__name__ = "_WindowsConsoleIO"


class _ConsoleStream:
    def __init__(self):
        self.buffer = type("B", (), {"raw": _FakeRaw()})()

    def reconfigure(self, **_kw):
        pass


def _pipe():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_plan_real_console_keeps_its_encoding():
    assert ct.stream_plan(_ConsoleStream(), environ={}) == {"errors": "replace"}


def test_plan_undeclared_pipe_is_ascii():
    assert ct.stream_plan(_pipe(), environ={}) == {
        "encoding": "ascii", "errors": ct.ASCII_ERROR_HANDLER}


@pytest.mark.parametrize("value", ["utf-8", "UTF8", "utf-8:replace", "utf_8"])
def test_plan_declared_utf8_reader_gets_utf8(value):
    assert ct.stream_plan(_pipe(), environ={"PYTHONIOENCODING": value}) == {
        "encoding": "utf-8", "errors": "replace"}


def test_plan_leaves_unconfigurable_streams_alone():
    assert ct.stream_plan(None, environ={}) is None
    assert ct.stream_plan(object(), environ={}) is None


def test_transliteration():
    assert ct.to_console_ascii("a — b → c ✓ … é") == "a - b -> c [ok] ... ?"


def test_posix_is_a_noop(monkeypatch):
    buf = _pipe()
    monkeypatch.setattr(sys, "stdout", buf)
    ct.harden_stdio(platform="linux")
    assert buf.encoding == "cp1252"


#: Windows is simulated at the one home only (faking ``sys.platform`` for
#: the whole interpreter breaks stdlib imports such as ``shutil``): every
#: caller that asks ``harden_stdio`` gets the win32 decision.
_CHILD = textwrap.dedent(r"""
    import io, sys
    import vco_lib.console_text as _ct
    _orig = _ct.harden_stdio
    _ct.harden_stdio = lambda *a, **k: _orig(*a, **{**k, "platform": "win32"})
    out = io.BytesIO()
    sys.stdout = io.TextIOWrapper(out, encoding="cp1252", write_through=True)
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
    {body}
    sys.stdout.flush()
    sys.__stdout__.buffer.write(out.getvalue())
""")


def _child(body: str, env_extra: "dict | None" = None) -> bytes:
    env = child_env(WEAVIATE_URL="http://127.0.0.1:9")
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env.update(env_extra or {})
    code = _CHILD.replace("{body}", textwrap.indent(textwrap.dedent(body), "").strip())
    res = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], capture_output=True,
                         timeout=120, env=env, cwd=str(REPO_ROOT))
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return res.stdout


def test_walk_main_delivers_the_refusal_to_a_pipe_as_ascii(tmp_path):
    """The wiring: `codegraph_walk.main` itself makes its streams safe, so
    the refusal it prints when the claim is held arrives readable (pre-fix:
    cp1252 byte 0x97 for the em dash, garbage to an OEM-code-page reader)."""
    out = _child(f"""
from vco_lib import codegraph_walk as cw
class _Held(Exception):
    pass
import contextlib
@contextlib.contextmanager
def _refuse(project):
    raise cw.WalkAlreadyRunning("held")
    yield
cw._held_walk_claim = _refuse
sys.stderr = sys.stdout  # capture the refusal line in the same buffer
rc = cw.main([{str(tmp_path / 'analyze_code_graph.py')!r}, "--project", "Demo", "."])
assert rc == cw.WALK_ALREADY_RUNNING_RC, rc
""")
    assert out.isascii(), out
    assert b"already running for this project - not starting a twin" in out


def test_a_declared_utf8_reader_still_gets_utf8():
    out = _child("""
from vco_lib.console_text import harden_stdio
harden_stdio()
print("—")
""", {"PYTHONIOENCODING": "utf-8"})
    assert out.strip() == "—".encode("utf-8")


def test_install_hardens_its_streams_at_import():
    """install.py's top-of-module sentinel goes through the one home: a
    pipe without a declared UTF-8 reader receives ASCII, not UTF-8 bytes a
    cp1252/OEM reader turns into garbage (pre-fix: b'\\xe2\\x80\\x94')."""
    out = _child("""
import install  # noqa: F401  (the import runs the sentinel)
print("step 7 — pull → ok")
""")
    assert out.strip() == b"step 7 - pull -> ok", out
