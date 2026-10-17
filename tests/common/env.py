# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Environment-variable isolation for tests — the ONE home (v0.2.94).

Three ``unittest.TestCase`` bases had grown their own ``set_env``
(``test_model_router_auth``, ``test_model_router_cli``,
``test_v0294_gateway_port_chain``), each carrying the same paragraph of
reasoning about the same bug. One helper, one paragraph:

**"Restore only if it was set" is not restoration.** A key a test INTRODUCES
survives into every later test in the process. That is how a test named for
isolation stops isolating, and it happened here twice:
``test_check_fails_on_a_non_loopback_bind_address`` assigned
``VCT_MODEL_GATEWAY_HOST=0.0.0.0`` with no cleanup, and because the variable
is normally unset there was nothing to restore — so it leaked, and any later
test resolving a bind address got a routable one and a refusal. Passing
alone, failing in a full run, with the cause several files away.

Registering the restore for the ABSENT case too is the whole fix, and it is
why this takes ``None`` to mean "unset it" rather than exposing two verbs:
setting and clearing need identical restoration, so they are one call.

``pytest.MonkeyPatch`` does the same job for pytest-style tests and is the
right tool there. This exists for ``unittest.TestCase`` classes, which have
``addCleanup`` but no monkeypatch fixture.

**Whole-process-state isolation (v0.2.104)** lives here too, because it is
the same concern one size up. Suites that RE-IMPORT a module under a fake env
(``weaviate_mcp.server`` / ``kg_access`` / a CLI script read their env at
import) change four process-globals at once: ``os.environ``, the
``sys.modules`` entries they evict and re-import, ``sys.path`` and sometimes
the cwd. Restoring only the env leaves a module object built from the fake
env in ``sys.modules`` for every later file — the v0.2.104 merge-only failure
(``test_kg_access_helper_module.py`` popped ``PROJECT_NAME`` and re-imported
``weaviate_mcp.*``, and a test several files later read the leftovers).
:func:`isolated_process_state` snapshots all four and puts them back;
:func:`isolate_process_state` registers it on a ``addCleanup`` /
``addClassCleanup``; :class:`IsolatedTestCase` does it per test; and
:func:`process_state_fixture` builds an autouse pytest fixture (any scope) for
module-level or class-level setup, which also covers ``unittest`` classes.
"""
from __future__ import annotations

import contextlib
import fnmatch
import os
import sys
import unittest
from typing import Callable, Iterable, Iterator, Literal, Optional

__all__ = [
    "EnvIsolationMixin",
    "IsolatedTestCase",
    "isolate_process_state",
    "isolated_process_state",
    "process_state_fixture",
    "set_env",
]


def set_env(
    register_cleanup: Callable[[Callable[[], None]], object],
    key: str,
    value: Optional[str],
) -> None:
    """Set (or, with ``value=None``, clear) ``key`` for one test.

    Args:
        register_cleanup: the test's ``addCleanup`` — passed in rather than
            inherited so a plain function, a fixture or a mixin can all use
            the same body.
        key: environment variable name.
        value: the new value, or ``None`` to remove the variable.
    """
    original = os.environ.get(key)

    def restore() -> None:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    register_cleanup(restore)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


class EnvIsolationMixin:
    """``self.set_env(key, value)`` for a ``unittest.TestCase``.

    Mixed in beside ``TestCase`` so the three gateway suites keep the call
    shape they already use and gain the restoration guarantee from one place.
    """

    def set_env(self, key: str, value: Optional[str]) -> None:
        """Set or clear ``key``, restoring its EXACT prior state after."""
        set_env(self.addCleanup, key, value)  # type: ignore[attr-defined]


def _module_matches(name: str, patterns: "tuple[str, ...]") -> bool:
    """``weaviate_mcp`` covers the package AND its submodules (not
    ``weaviate_mcp_other``); a pattern with a glob (``_test_sync_kg_*``)
    matches by :func:`fnmatch.fnmatchcase` for per-test unique names."""
    for pat in patterns:
        if name == pat or name.startswith(pat + "."):
            return True
        if any(c in pat for c in "*?[") and fnmatch.fnmatchcase(name, pat):
            return True
    return False


@contextlib.contextmanager
def isolated_process_state(
    modules: Iterable[str] = (),
    prepend_sys_path: Iterable["str | os.PathLike[str]"] = (),
) -> Iterator[None]:
    """Snapshot ``os.environ``, ``sys.path``, the cwd and every
    ``sys.modules`` entry matching *modules*; restore all four on exit.

    Args:
        modules: module-name patterns this block may evict / re-import (see
            :func:`_module_matches`). On exit every matching entry is dropped
            and the pre-block ones are put back — so the module object a
            later test imports is the one it would have got without us.
        prepend_sys_path: directories put at the front of ``sys.path`` for
            the block only (in the given order, first = highest priority).

    The env is restored WHOLE (clear + update), not by key list: the set of
    keys a re-imported module or a helper may write is open, and a list drifts.
    """
    patterns = tuple(modules)
    saved_env = dict(os.environ)
    saved_path = list(sys.path)
    saved_cwd = os.getcwd()
    saved_mods = {m: mod for m, mod in sys.modules.items() if _module_matches(m, patterns)}
    for entry in reversed(tuple(prepend_sys_path)):
        sys.path.insert(0, os.fspath(entry))
    try:
        yield
    finally:
        if patterns:
            for m in [m for m in sys.modules if _module_matches(m, patterns)]:
                del sys.modules[m]
            sys.modules.update(saved_mods)
        sys.path[:] = saved_path
        if os.getcwd() != saved_cwd:
            os.chdir(saved_cwd)
        if dict(os.environ) != saved_env:
            os.environ.clear()
            os.environ.update(saved_env)


def isolate_process_state(
    register_cleanup: Callable[[Callable[[], None]], object],
    modules: Iterable[str] = (),
    prepend_sys_path: Iterable["str | os.PathLike[str]"] = (),
) -> None:
    """Enter :func:`isolated_process_state` now and register its exit on
    *register_cleanup* — ``self.addCleanup`` in ``setUp``, or
    ``cls.addClassCleanup`` in ``setUpClass``."""
    cm = isolated_process_state(modules, prepend_sys_path)
    cm.__enter__()

    def _exit() -> None:
        cm.__exit__(None, None, None)

    register_cleanup(_exit)


class IsolatedTestCase(unittest.TestCase):
    """A ``TestCase`` whose every test gets :func:`isolated_process_state`.

    Subclasses name what they touch: ``ISOLATED_MODULES`` (module patterns
    they evict / re-import) and ``ISOLATED_SYS_PATH`` (directories they need
    importable). ``setUp`` overrides call ``super().setUp()`` FIRST, so
    anything they change is inside the snapshot.
    """

    ISOLATED_MODULES: "tuple[str, ...]" = ()
    ISOLATED_SYS_PATH: "tuple[str, ...]" = ()

    def setUp(self) -> None:
        super().setUp()
        isolate_process_state(self.addCleanup, self.ISOLATED_MODULES, self.ISOLATED_SYS_PATH)


def process_state_fixture(
    scope: "Literal['function', 'class', 'module', 'package', 'session']" = "function",
    modules: Iterable[str] = (),
    prepend_sys_path: Iterable["str | os.PathLike[str]"] = (),
    name: str = "process_state_isolation",
):
    """An AUTOUSE pytest fixture applying :func:`isolated_process_state` at
    *scope*. Assign it at module level::

        _isolate = process_state_fixture("module", modules=("weaviate_mcp",))

    ``scope="module"`` wraps the whole file — ``setUpClass`` /
    ``tearDownClass`` of ``unittest`` classes included (they run inside it) —
    which is the scope for state set up once per class or file. *name* is
    the fixture name another fixture requests to run inside the isolation.
    """
    import pytest  # local: unittest-only users of this module never need it

    patterns = tuple(modules)
    paths = tuple(str(p) for p in prepend_sys_path)

    @pytest.fixture(scope=scope, autouse=True, name=name)
    def _process_state_isolation():
        with isolated_process_state(patterns, paths):
            yield

    return _process_state_isolation
