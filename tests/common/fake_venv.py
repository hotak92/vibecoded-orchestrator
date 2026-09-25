# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A fake install venv whose ``bin/python`` actually RUNS the checkout's code.

install.py resolves its venv interpreter by PATH ONLY
(``vco_lib.install_companions.resolve_install_venv_python``: does
``<root>/.venv/bin/python`` exist?), so a test that wants the tier-A
subprocess path — ``<venv-python> -m vco_lib.x`` — without building a real
venv drops a POSIX stub at exactly that path. The stub delegates to the
CURRENT interpreter (``#!{sys.executable}``) with the checkout first on
``sys.path``, then strips the ``-m <module>`` pair argparse never sees in a
real ``python -m`` and runs the module under ``runpy`` — the house idiom
since ``tests/test_codegraph_analyze_shim_rt4.py::_make_fake_venv``.

The point is hermeticity in BOTH directions: the fake venv answers the
resolver (so the code under test takes the subprocess branch, not the
``python_exe is None`` soft-fail), and the child executes THIS checkout's
``vco_lib`` even on a machine whose site-packages holds a stale non-editable
copy of it (the documented dogfood shadow — the same reason
``tests/common/child_env.py`` exists for ``sys.executable`` children).

POSIX-only (a ``#!`` script is not an executable on Windows); callers skip
on ``os.name != "posix"`` — CI runs the suite on ubuntu-latest only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_STUB = """#!{interpreter}
import runpy
import sys

sys.path.insert(0, {repo_root!r})
# A real `python -m mod args...` hands the module only `args...`; the stub's
# argv is [stub, "-m", mod, *args], so drop the two leading flag entries.
sys.argv = [sys.argv[0], *sys.argv[3:]]
runpy.run_module({module!r}, run_name="__main__")
"""


def install_fake_venv_python(root: Path, *, module: str = "vco_lib.openai_key") -> Path:
    """Write ``<root>/.venv/bin/python`` as a stub running ``python -m module``.

    Returns the stub's path. Idempotent per (root, module): overwrites.
    """
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    py = venv_bin / "python"
    py.write_text(
        _STUB.format(interpreter=sys.executable, repo_root=str(REPO_ROOT), module=module),
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | 0o111)  # ugo+x — subprocess needs exec
    return py
