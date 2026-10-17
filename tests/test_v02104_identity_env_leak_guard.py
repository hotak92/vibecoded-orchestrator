# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 W-IDENTITY: the suite fails a test that leaves the project
identity env changed, and ``tests.common.env`` restores everything a
re-importing test changes.

The incident: ``test_kg_access_helper_module.py`` popped ``PROJECT_NAME`` /
``CODE_GRAPH_PROJECT`` and re-imported ``weaviate_mcp.*`` under a fake env
with no restore; a test several files later resolved a different project and
failed only in the merged order. Two halves are pinned here:

* the GUARD (``tests/conftest.py`` — per test, per module, at collection),
  through its pure helpers and through a real child pytest (wiring);
* the HELPER (``tests/common/env.py``) — env, ``sys.modules``, ``sys.path``
  and cwd all come back, for both the act and the leave-alone case.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests import conftest as _conftest
from tests.common.child_env import child_env
from tests.common.env import isolate_process_state, isolated_process_state

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── guard: pure helpers ──────────────────────────────────────────────────────


def test_identity_leak_is_none_when_nothing_changed():
    before = _conftest._identity_env_snapshot()
    assert _conftest._identity_env_leak("t", before) is None


@pytest.mark.parametrize("key", _conftest._IDENTITY_ENV_KEYS)
def test_identity_leak_names_a_set_key(monkeypatch, key):
    monkeypatch.delenv(key, raising=False)
    before = _conftest._identity_env_snapshot()
    monkeypatch.setenv(key, "Alpha_KnowledgeGraph")
    msg = _conftest._identity_env_leak("tests/test_x.py::t", before)
    assert msg and key in msg and "Alpha_KnowledgeGraph" in msg and "test_x.py" in msg


def test_identity_leak_names_a_removed_key(monkeypatch):
    """Popping a key the session had is a leak too — that was the incident."""
    monkeypatch.setenv("PROJECT_NAME", "Ambient")
    before = _conftest._identity_env_snapshot()
    monkeypatch.delenv("PROJECT_NAME")
    msg = _conftest._identity_env_leak("t", before)
    assert msg and "PROJECT_NAME: 'Ambient' -> None" in msg


def test_restore_puts_back_set_and_absent_keys(monkeypatch):
    monkeypatch.setenv("KG_COLLECTION", "Orig")
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    before = _conftest._identity_env_snapshot()
    os.environ["KG_COLLECTION"] = "Fake"
    os.environ["CODE_GRAPH_PROJECT"] = "Fake"
    _conftest._restore_identity_env(before)
    assert os.environ["KG_COLLECTION"] == "Orig"
    assert "CODE_GRAPH_PROJECT" not in os.environ


# ── guard: wiring through a real child pytest ────────────────────────────────


def _run_child(tmp_path: Path, name: str, body: str) -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    env = child_env()
    env.pop("VCO_TEST_ALLOW_REAL_STATE", None)
    for key in _conftest._IDENTITY_ENV_KEYS:
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", str(tmp_path / name)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300,
    )
    return f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"


def test_the_guard_fails_a_test_that_leaks_identity(tmp_path):
    """Red before: no guard — the leaked KG_COLLECTION reached the next test."""
    out = _run_child(tmp_path, "test_identity_probe.py", (
        "import os\n"
        "def test_a_leaks():\n    os.environ['KG_COLLECTION'] = 'Alpha_KnowledgeGraph'\n"
        "def test_b_sees_clean_env():\n    assert 'KG_COLLECTION' not in os.environ\n"
        "def test_c_monkeypatch_is_fine(monkeypatch):\n"
        "    monkeypatch.setenv('PROJECT_NAME', 'X')\n"
    ))
    assert "rc=0" not in out, out[-2000:]
    assert "IdentityEnvLeak" in out and "test_a_leaks left project-identity env" in out, out[-2000:]
    # The guard restores after reporting: the NEXT test sees a clean env.
    assert "3 passed, 1 error" in out, out[-800:]


def test_the_guard_fails_a_setupclass_that_leaks_identity(tmp_path):
    """Module-scope twin: a class-level set the per-test snapshot cannot see."""
    out = _run_child(tmp_path, "test_identity_class_probe.py", (
        "import os, unittest\n"
        "class T(unittest.TestCase):\n"
        "    @classmethod\n"
        "    def setUpClass(cls):\n        os.environ['CODE_GRAPH_PROJECT'] = 'Alpha'\n"
        "    def test_one(self):\n        pass\n"
    ))
    assert "IdentityEnvLeak" in out and "CODE_GRAPH_PROJECT: None -> 'Alpha'" in out, out[-2000:]


def test_the_guard_fails_a_module_that_leaks_identity_at_import(tmp_path):
    """Collection-time check: an import-time mutation is a collection error."""
    out = _run_child(tmp_path, "test_identity_import_probe.py", (
        "import os\n"
        "os.environ['SHARED_KG_COLLECTION'] = 'Alpha_KnowledgeGraph'\n"
        "def test_x():\n    pass\n"
    ))
    assert "rc=0" not in out, out[-2000:]
    assert "importing test_identity_import_probe.py" in out and "SHARED_KG_COLLECTION" in out, out[-2000:]


# ── helper: restores everything, leaves the rest alone ───────────────────────


def test_isolated_process_state_restores_env_modules_path_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "Orig")
    monkeypatch.delenv("KG_COLLECTION", raising=False)
    orig_mod = types.ModuleType("_r3iso_probe_mod")
    monkeypatch.setitem(sys.modules, "_r3iso_probe_mod", orig_mod)
    path_before = list(sys.path)
    cwd_before = os.getcwd()
    with isolated_process_state(modules=("_r3iso_probe_mod",), prepend_sys_path=(tmp_path,)):
        assert sys.path[0] == str(tmp_path)
        os.environ.pop("PROJECT_NAME")
        os.environ["KG_COLLECTION"] = "Fake"
        sys.modules["_r3iso_probe_mod"] = types.ModuleType("_r3iso_probe_mod")
        sys.modules["_r3iso_probe_mod.sub"] = types.ModuleType("_r3iso_probe_mod.sub")
        sys.path.insert(0, "/nonexistent-r3iso")
        os.chdir(tmp_path)
    assert os.environ["PROJECT_NAME"] == "Orig"
    assert "KG_COLLECTION" not in os.environ
    assert sys.modules["_r3iso_probe_mod"] is orig_mod
    assert "_r3iso_probe_mod.sub" not in sys.modules
    assert sys.path == path_before
    assert os.getcwd() == cwd_before


def test_isolated_process_state_leaves_unlisted_modules_alone(monkeypatch):
    """Leave-alone: a module outside *modules* (or a look-alike name) that the
    block imports stays imported — only the named families are rolled back."""
    monkeypatch.delitem(sys.modules, "_r3iso_other", raising=False)
    monkeypatch.delitem(sys.modules, "_r3iso_probe_modx", raising=False)
    with isolated_process_state(modules=("_r3iso_probe_mod",)):
        sys.modules["_r3iso_other"] = types.ModuleType("_r3iso_other")
        sys.modules["_r3iso_probe_modx"] = types.ModuleType("_r3iso_probe_modx")
    assert "_r3iso_other" in sys.modules
    assert "_r3iso_probe_modx" in sys.modules


def test_glob_pattern_rolls_back_unique_module_names(monkeypatch):
    with isolated_process_state(modules=("_r3iso_unique_*",)):
        sys.modules["_r3iso_unique_123"] = types.ModuleType("_r3iso_unique_123")
    assert "_r3iso_unique_123" not in sys.modules


def test_isolate_process_state_restores_on_the_registered_cleanup(monkeypatch):
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    cleanups = []
    isolate_process_state(cleanups.append)
    os.environ["CODE_GRAPH_PROJECT"] = "Fake"
    assert len(cleanups) == 1
    cleanups[0]()
    assert "CODE_GRAPH_PROJECT" not in os.environ


# ── the migrated files no longer leak (red on the pre-v0.2.104 versions) ─────


@pytest.mark.parametrize("test_file, leaked_key", [
    # _fresh_server SET KG_COLLECTION=... and kept the reloaded weaviate_mcp.
    ("tests/test_kg_access_list.py", "KG_COLLECTION"),
    # setUp POPPED PROJECT_NAME / CODE_GRAPH_PROJECT, never restored.
    ("tests/test_query_code_graph_access_list.py", "CODE_GRAPH_PROJECT"),
    # _fresh_search_knowledge SET KG_COLLECTION and VCT_KG_ACCESS_LIST.
    ("tests/test_search_knowledge_access_list.py", "VCT_KG_ACCESS_LIST"),
])
def test_a_downstream_test_sees_the_ambient_identity(tmp_path, test_file, leaked_key):
    """Run the migrated file, then a downstream probe that reads the key, in
    ONE child process with the key set to an ambient value. Before the fix the
    probe saw the value the file left behind (or none); now it sees the
    ambient one."""
    probe = tmp_path / "test_zz_downstream_probe.py"
    probe.write_text(
        "import os, sys\n"
        "def test_probe():\n"
        f"    assert os.environ.get({leaked_key!r}) == 'Ambient_Value', "
        f"os.environ.get({leaked_key!r})\n"
        "    srv = sys.modules.get('weaviate_mcp.server')\n"
        "    assert srv is None or getattr(srv, 'KG_COLLECTION', None) != 'Alpha_KnowledgeGraph'\n",
        encoding="utf-8",
    )
    env = child_env()
    env.pop("VCO_TEST_ALLOW_REAL_STATE", None)
    for key in _conftest._IDENTITY_ENV_KEYS:
        env.pop(key, None)
    env[leaked_key] = "Ambient_Value"
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(REPO_ROOT), str(REPO_ROOT / test_file), str(probe)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
