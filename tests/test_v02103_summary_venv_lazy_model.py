# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the KG-summary Ollama model resolves LAZILY and its failure
message names the interpreter.

v0.2.102 W5-D resolved ``summary_backends.OLLAMA_DEFAULT_MODEL`` at import
time via ``vco_lib.summary_model``. Two defects followed:

1. Every per-node generator child printed ``[summary_backends] broken
   install: vco_lib is not importable`` even when the Ollama tier was never
   used — and, spawned under a USER PROJECT's own ``.venv`` (the launcher's
   pre-v0.2.103 resolver preferred it), that line was also the LAST stderr
   line, so the launcher's error snippet surfaced it as the node's failure
   reason instead of the generator's real stdout diagnostic.
2. The message's remediation ("re-run the orchestrator's install / update")
   was FALSE for the project-venv case: no install ever puts vco_lib into a
   user project's app venv.

Fixed behaviour pinned here:

* importing ``summary_backends`` (and ``generate-kg-summary``) prints
  NOTHING when vco_lib is unimportable — resolution happens on first use;
* when the tier IS consulted without vco_lib, the stderr line names the
  running interpreter and the orchestrator-venv requirement (never a false
  "broken install … re-run install" claim);
* ``KG_SUMMARY_OLLAMA_MODEL`` still wins without touching vco_lib;
* ``ollama_default_model()`` honours a monkeypatched module attribute (the
  seam the v0.2.100 tier-model tests pin through).

The subprocess legs run ``python -S -E`` from a temp cwd so the repo's own
editable-installed vco_lib cannot leak in — no network, no Weaviate.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"

# What the subprocess legs print/report. Kept verbatim-sync with
# templates/scripts/summary_backends.py::_resolve_ollama_model.
MESSAGE_NEEDLES = (
    "vco_lib is not importable from this interpreter",
    "must run under the orchestrator's own venv",
)

_PROBE = r"""
import importlib.util, io, sys
from contextlib import redirect_stderr

scripts_dir, mod_name, mod_path = sys.argv[1], sys.argv[2], sys.argv[3]

def load():
    if mod_name == "summary_backends":
        sys.path.insert(0, scripts_dir)
        return importlib.import_module("summary_backends")
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

buf = io.StringIO()
with redirect_stderr(buf):
    mod = load()
import_time_stderr = buf.getvalue()
print("IMPORT_STDERR:", import_time_stderr.replace("\n", "\\n"))

buf2 = io.StringIO()
with redirect_stderr(buf2):
    value = mod.OLLAMA_DEFAULT_MODEL
print("VALUE:", repr(value))
print("USE_STDERR:", buf2.getvalue().replace("\n", "\\n"))
"""


def _run_probe(monkeypatch, tmp_path, mod_name, mod_path, extra_env=None):
    """Run the probe under an interpreter that cannot import vco_lib.

    `child_env()` supplies the hermetic base the repo's subprocess-spawn
    ratchet requires (checkout import roots pinned); the `-S -E` flags make
    the child ignore PYTHONPATH and site-packages entirely, which is what
    hides vco_lib for these legs — the `-E` flag, not a scrubbed env.
    """
    env = child_env(
        {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        **(extra_env or {}),
    )
    out = subprocess.run(
        [sys.executable, "-S", "-E", "-c", _PROBE, str(SCRIPTS_DIR), mod_name, str(mod_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"probe failed: {out.stderr}"
    lines = dict(
        line.split(": ", 1) for line in out.stdout.splitlines() if ": " in line
    )
    return lines


def test_summary_backends_import_is_silent_without_vco_lib(monkeypatch, tmp_path):
    """No stderr at import; the notice fires only on first attribute use."""
    lines = _run_probe(
        monkeypatch, tmp_path, "summary_backends", str(SCRIPTS_DIR / "summary_backends.py")
    )
    # 1. Import must be silent — pre-fix, the broken-install line printed
    #    here on EVERY node, Ollama tier used or not.
    assert lines["IMPORT_STDERR"] == ""
    # 2. First use resolves: "" (tier unavailable) + the honest notice.
    assert lines["VALUE"] == "''"
    for needle in MESSAGE_NEEDLES:
        assert needle in lines["USE_STDERR"], lines["USE_STDERR"]
    # Never again the false remediation.
    assert "re-run" not in lines["USE_STDERR"]


def test_summary_backends_env_override_needs_no_vco_lib(monkeypatch, tmp_path):
    lines = _run_probe(
        monkeypatch,
        tmp_path,
        "summary_backends",
        str(SCRIPTS_DIR / "summary_backends.py"),
        extra_env={"KG_SUMMARY_OLLAMA_MODEL": "custom:1b"},
    )
    assert lines["IMPORT_STDERR"] == ""
    assert lines["VALUE"] == "'custom:1b'"
    assert lines["USE_STDERR"] == ""


def test_generate_kg_summary_re_export_is_lazy(monkeypatch, tmp_path):
    """The generator module re-export must not trigger resolution either."""
    lines = _run_probe(
        monkeypatch,
        tmp_path,
        "generate_kg_summary",
        str(SCRIPTS_DIR / "generate-kg-summary.py"),
    )
    assert lines["IMPORT_STDERR"] == ""
    assert lines["VALUE"] == "''"
    for needle in MESSAGE_NEEDLES:
        assert needle in lines["USE_STDERR"], lines["USE_STDERR"]


def test_ollama_default_model_honours_monkeypatched_attribute():
    """The lazy resolver defers to a pinned module attribute (the seam the
    v0.2.100 tier-model tests and `call_ollama`'s default rely on)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    sb = importlib.import_module("summary_backends")
    saved = sb.__dict__.get("OLLAMA_DEFAULT_MODEL")
    try:
        sb.__dict__["OLLAMA_DEFAULT_MODEL"] = "pinned:1b"
        assert sb.ollama_default_model() == "pinned:1b"
        assert sb.OLLAMA_DEFAULT_MODEL == "pinned:1b"
    finally:
        if saved is None:
            sb.__dict__.pop("OLLAMA_DEFAULT_MODEL", None)
        else:
            sb.__dict__["OLLAMA_DEFAULT_MODEL"] = saved


def test_unknown_module_attribute_still_raises():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    sb = importlib.import_module("summary_backends")
    try:
        sb.DEFINITELY_NOT_A_NAME  # noqa: B018
    except AttributeError as exc:
        assert "DEFINITELY_NOT_A_NAME" in str(exc)
    else:
        raise AssertionError("unknown attribute must raise AttributeError")
