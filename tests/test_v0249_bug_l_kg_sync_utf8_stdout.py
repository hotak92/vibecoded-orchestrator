"""v0.2.49 Bug L regression test: sync_knowledge_graph.py reconfigures
stdout to UTF-8 + backslashreplace at startup, so emoji prints don't
crash on Windows cp1252 consoles."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    # tests/ is a sibling of the repo root
    return here.parent


def _extract_reconfigure_block(script: Path) -> str:
    """Extract just the top-of-file reconfigure block from the script.

    The block is bracketed by sentinel comments containing 'Bug L'
    (start marker) and ends at the next blank line followed by
    `import os`. Falls back to "first 50 lines" if markers shift —
    that still avoids the heavyweight weaviate/vco_lib imports
    further down the file which would fail in a venv-less subprocess.
    """
    text = script.read_text(encoding="utf-8")
    lines = text.splitlines()
    # The reconfigure block lives between `import sys` and the next
    # plain `import os` (which begins the post-reconfigure imports).
    out: list[str] = []
    saw_sys = False
    for line in lines:
        if not saw_sys:
            if line.strip() == "import sys":
                saw_sys = True
                out.append(line)
            continue
        if line.strip() == "import os":
            break  # stop BEFORE the heavy imports
        out.append(line)
    return "\n".join(out)


def test_sync_knowledge_graph_reconfigures_stdout_for_utf8():
    """At import time, sys.stdout.reconfigure should be called with
    encoding='utf-8', errors='backslashreplace'. Verify by spawning a
    subprocess that execs just the reconfigure block + checks the
    encoding."""
    script = _project_root() / "templates" / "scripts" / "sync_knowledge_graph.py"
    assert script.exists(), f"missing source-of-truth script: {script}"

    head = _extract_reconfigure_block(script)
    # Sanity-check the extraction caught the reconfigure call.
    assert "reconfigure" in head, (
        f"extraction logic broken — no 'reconfigure' in head:\n{head}"
    )

    harness = (
        "import sys\n"
        + head
        + "\nprint(sys.stdout.encoding)\nprint(sys.stdout.errors)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", harness],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    out_lines = result.stdout.strip().split("\n")
    # Last two lines should be the encoding + errors values.
    encoding, errors = out_lines[-2], out_lines[-1]
    assert encoding.lower() == "utf-8", (
        f"expected stdout encoding='utf-8' post-reconfigure, got {encoding!r}; "
        f"full output:\n{result.stdout}"
    )
    assert errors == "backslashreplace", (
        f"expected stdout errors='backslashreplace', got {errors!r}"
    )


def test_emoji_print_does_not_crash_when_stdout_is_cp1252():
    """Simulate the Windows direct-CLI invocation by monkey-patching
    sys.stdout to a cp1252-encoding wrapper BEFORE importing the
    script-top, then asserting the reconfigure runs + a subsequent
    emoji print survives.

    This is the actual failure mode Fabio reported: stdout was cp1252,
    the script crashed with UnicodeEncodeError on `print(f"❌ ...")`.
    """
    script = _project_root() / "templates" / "scripts" / "sync_knowledge_graph.py"
    head = _extract_reconfigure_block(script)
    assert "reconfigure" in head, (
        f"extraction logic broken — no 'reconfigure' in head:\n{head}"
    )
    harness = (
        "import sys, io\n"
        # Wrap stdout in a cp1252 TextIOWrapper — what Windows
        # console looks like by default.
        "raw = sys.stdout.buffer\n"
        "sys.stdout = io.TextIOWrapper(raw, encoding='cp1252', errors='strict', line_buffering=True)\n"
        # Now exec the script's top — reconfigure should override our
        # cp1252 wrapper. If it doesn't, the print below crashes.
        + head
        + "\nprint('emoji-test ❌ ✅ \U0001f4ca')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", harness],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess crashed (expected reconfigure to neutralize cp1252):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Either the emoji rendered as UTF-8 or as backslash-escapes — both
    # are acceptable; what's NOT acceptable is a UnicodeEncodeError.
    assert "emoji-test" in result.stdout, (
        f"expected the test marker in output, got:\n{result.stdout}"
    )
