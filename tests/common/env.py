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
"""
from __future__ import annotations

import os
from typing import Callable, Optional

__all__ = ["EnvIsolationMixin", "set_env"]


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
