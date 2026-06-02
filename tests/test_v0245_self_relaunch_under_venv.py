"""v0.2.45 V45-A: tests for `install._ensure_running_under_mcp_venv`.

The helper is supposed to relaunch install.py under
`claude_mcp_servers/.venv/bin/python` when:
  - `import weaviate` is unimportable from the current interpreter
  - AND `_resolve_venv_python_for_install(PROJECT_ROOT)` returns a different,
    existing interpreter
  - AND `VCT_INSTALL_RELAUNCHED` is not already set

In every other case it should be a no-op (return without touching
`os.execve`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Path adjustment so install.py is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util  # noqa: E402

import install  # noqa: E402  the module being tested


@pytest.fixture
def execve_recorder(monkeypatch):
    """Replace os.execve with a recorder that captures calls.

    Returns a list — each call to os.execve appends a dict {path, argv, env}.
    """
    calls: list[dict] = []

    def _fake_execve(path, argv, env):  # signature matches os.execve
        calls.append({"path": path, "argv": list(argv), "env": dict(env)})
        # do NOT actually exec — that would replace the test process

    monkeypatch.setattr(install.os, "execve", _fake_execve)
    return calls


def _force_find_spec(monkeypatch, *, found: bool):
    """Make `importlib.util.find_spec("weaviate")` return controlled value.

    `found=True` → returns a fake spec object (truthy / not-None).
    `found=False` → returns None.

    The helper imports `importlib.util` inside the function body, so we patch
    on the importlib.util module directly. find_spec resolves at call-time
    via attribute lookup on the module, which is what we patch here.
    """
    if found:
        fake_spec = SimpleNamespace(name="weaviate")
    else:
        fake_spec = None

    def _fake(name, package=None):
        if name == "weaviate":
            return fake_spec
        # Fall through to real implementation for any other module
        return _real_find_spec(name, package)

    _real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", _fake)


def test_skip_when_weaviate_importable(monkeypatch, execve_recorder):
    """If `import weaviate` already works, no relaunch — the simplest path.

    This is the steady-state CLI case: user ran `source .venv/bin/activate`
    then `python install.py --update`, weaviate is already on sys.path, so
    we trivially short-circuit.
    """
    _force_find_spec(monkeypatch, found=True)
    # Even if other conditions would otherwise trigger a relaunch, the
    # find_spec short-circuit must win. Sanity-check: clear the re-entry
    # guard so it can't be the thing skipping.
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)

    install._ensure_running_under_mcp_venv()

    assert execve_recorder == [], "execve must not be called when weaviate is importable"


def test_skip_when_re_entry_guard_set(monkeypatch, execve_recorder):
    """If VCT_INSTALL_RELAUNCHED=1, do NOT exec a second time.

    Prevents an exec loop when `_resolve_venv_python_for_install` returns a
    bogus venv that itself can't import weaviate.
    """
    _force_find_spec(monkeypatch, found=False)
    monkeypatch.setenv("VCT_INSTALL_RELAUNCHED", "1")

    install._ensure_running_under_mcp_venv()

    assert execve_recorder == [], "execve must not be called when re-entry guard is set"


def test_skip_when_resolve_returns_none(monkeypatch, execve_recorder):
    """If we can't resolve a venv interpreter, soft-fail and return.

    The helper's docstring guarantees no new failure mode here — step 7d will
    raise its own error later, but at least UPDATE_DEFERRED captures it.
    """
    _force_find_spec(monkeypatch, found=False)
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)
    monkeypatch.setattr(install, "_resolve_venv_python_for_install",
                        lambda root: None)

    install._ensure_running_under_mcp_venv()

    assert execve_recorder == [], "execve must not be called when resolver returns None"


def test_skip_when_target_does_not_exist(monkeypatch, execve_recorder):
    """If the resolved interpreter path doesn't exist on disk, soft-fail.

    Defends against stale `.venv` paths (e.g. user deleted the venv directory
    but the resolver's first-existing-path check raced).
    """
    _force_find_spec(monkeypatch, found=False)
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)
    monkeypatch.setattr(install, "_resolve_venv_python_for_install",
                        lambda root: Path("/nonexistent/python-does-not-exist"))

    install._ensure_running_under_mcp_venv()

    assert execve_recorder == [], "execve must not be called when target path missing"


def test_skip_when_already_running_under_target(monkeypatch, execve_recorder):
    """If the resolver returns the SAME interpreter we're already running,
    don't exec.

    This is the case where install.py is invoked under the MCP venv directly
    (e.g. the launcher already routes correctly post-V45-B). Resolver returns
    `sys.executable`, helper bails out.
    """
    _force_find_spec(monkeypatch, found=False)
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)
    monkeypatch.setattr(install, "_resolve_venv_python_for_install",
                        lambda root: Path(sys.executable))

    install._ensure_running_under_mcp_venv()

    assert execve_recorder == [], "execve must not be called when already running under target"


def test_execve_called_when_all_conditions_met(
    monkeypatch, execve_recorder, tmp_path,
):
    """Happy path: weaviate missing, venv resolves to a DIFFERENT existing
    interpreter — helper must os.execve into it with the re-entry guard.

    We synthesize a fake interpreter on disk (tmp_path/fake-python). Using
    tmp_path guarantees a real-file path that differs from sys.executable
    so the same-interpreter short-circuit doesn't fire.
    """
    _force_find_spec(monkeypatch, found=False)
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)

    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)

    # Sanity: tmp_path is not equal to sys.executable
    assert Path(fake_python).resolve() != Path(sys.executable).resolve()

    monkeypatch.setattr(install, "_resolve_venv_python_for_install",
                        lambda root: fake_python)

    # Use a stable sys.argv for assertion
    monkeypatch.setattr(install.sys, "argv",
                        ["/some/path/install.py", "--update"])

    install._ensure_running_under_mcp_venv()

    assert len(execve_recorder) == 1, (
        f"expected exactly one execve call, got {len(execve_recorder)}"
    )
    call = execve_recorder[0]
    # The path passed as the executable
    assert call["path"] == str(fake_python), (
        f"execve path must be the resolved venv interpreter; got {call['path']}"
    )
    # argv[0] must be the same target; remaining argv preserved verbatim
    assert call["argv"] == [str(fake_python), "/some/path/install.py", "--update"], (
        f"execve argv must be [target, *sys.argv]; got {call['argv']}"
    )
    # Re-entry guard env var must be set in the child env
    assert call["env"].get("VCT_INSTALL_RELAUNCHED") == "1", (
        "execve env must include VCT_INSTALL_RELAUNCHED=1 to prevent exec loop"
    )
