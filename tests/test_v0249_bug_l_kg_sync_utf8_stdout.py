"""v0.2.49 Bug L regression test: sync_knowledge_graph.py must not crash on a
Windows cp1252 console or pipe when it prints emoji.

v0.2.105: the script no longer carries its own ``reconfigure`` block; it
calls the ONE home, :func:`vco_lib.console_text.harden_stdio`, at import
time (right after its vco_lib path bootstrap). This test imports the real
script in a child whose stdout is a STRICT cp1252 pipe, with Windows
simulated at that one home, and prints the emoji that used to crash.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

_CHILD = textwrap.dedent(r"""
    import importlib.util, io, sys
    import vco_lib.console_text as _ct
    _orig = _ct.harden_stdio
    _ct.harden_stdio = lambda *a, **k: _orig(*a, **{**k, "platform": "win32"})
    out = io.BytesIO()
    sys.stdout = io.TextIOWrapper(out, encoding="cp1252", errors="strict", write_through=True)
    spec = importlib.util.spec_from_file_location("kg_sync_bug_l", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # module top hardens the streams
    print("emoji-test ❌ ✅ \U0001f4ca")
    sys.stdout.flush()
    sys.__stdout__.buffer.write(out.getvalue())
""")


def test_emoji_print_does_not_crash_when_stdout_is_cp1252():
    env = child_env(WEAVIATE_URL="http://127.0.0.1:9")
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    res = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _CHILD, str(SCRIPT)],
        capture_output=True, timeout=120, env=env, cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")[-2000:]
    assert res.stdout.strip() == b"emoji-test [x] [ok] ?", res.stdout
