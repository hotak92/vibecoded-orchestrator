# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W-INSTALL-ROOT (v0.2.97): conftest pins ``VCT_INSTALL_ROOT`` in-process.

``tests/common/child_env.py`` has pinned the var for CHILD processes since
review round 7; the in-process side was missing, so an ambient
``$VCT_INSTALL_ROOT`` exported by a launcher-started shell (a session inside
a real install — exactly how the suite runs on the dev box) made
``vco_lib.python_exe.resolve_install_root()`` and everything that rides it
(``vco_lib.containers.runtime_pin`` reading
``<root>/state/install/runtime.txt`` foremost) answer the REAL install
locally and the checkout on CI.

The module-scoped fixture below simulates that leak — it re-points
``VCT_INSTALL_ROOT`` at a fake "real" install root whose runtime.txt records
``docker`` — and the test proves the conftest per-test re-establish wins: an
in-process ``runtime_pin()`` call does NOT see the fake root's runtime.txt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.containers import PIN_VIA_RUNTIME_TXT, runtime_pin  # noqa: E402

_ALLOW_REAL_STATE = os.environ.get("VCO_TEST_ALLOW_REAL_STATE", "") not in (
    "", "0", "false",
)


@pytest.fixture(scope="module", autouse=True)
def _ambient_install_root_leak(tmp_path_factory):
    """Simulate the dev-shell leak: ``VCT_INSTALL_ROOT`` naming a real
    install root whose runtime.txt records ``docker``.

    Module-scoped on purpose: its setup runs BEFORE the function-scoped
    conftest re-establish (higher scope first), which is exactly the
    ordering of the real leak — set once, before any test."""
    fake_root = tmp_path_factory.mktemp("real_install_root")
    runtime_txt = fake_root / "state" / "install" / "runtime.txt"
    runtime_txt.parent.mkdir(parents=True, exist_ok=True)
    runtime_txt.write_text("docker\n", encoding="utf-8")
    prev = os.environ.get("VCT_INSTALL_ROOT")
    os.environ["VCT_INSTALL_ROOT"] = str(fake_root)
    yield fake_root
    if prev is None:
        os.environ.pop("VCT_INSTALL_ROOT", None)
    else:
        os.environ["VCT_INSTALL_ROOT"] = prev


@pytest.mark.skipif(
    _ALLOW_REAL_STATE,
    reason="VCO_TEST_ALLOW_REAL_STATE disables the conftest pins",
)
def test_fake_real_install_root_runtime_txt_not_visible_in_process(
    _ambient_install_root_leak: Path,
):
    fake_root = _ambient_install_root_leak
    fake_runtime_txt = fake_root / "state" / "install" / "runtime.txt"

    # Control: the fake runtime.txt IS a real pin when its root is passed
    # explicitly — the file is not inert, so what follows tests visibility,
    # not file validity.
    control = runtime_pin(env={}, install_root=fake_root)
    assert control == ("docker", PIN_VIA_RUNTIME_TXT, fake_runtime_txt), (
        "control failed: the fake runtime.txt must pin docker when visible"
    )

    # The conftest pin governs in-process: the per-test re-establish has
    # already run (function scope, after this module fixture's setup), so
    # the leak does NOT survive into the test.
    assert os.environ["VCT_INSTALL_ROOT"] == str(REPO_ROOT), (
        "the ambient VCT_INSTALL_ROOT leak survived the conftest pin"
    )

    # And so an in-process runtime_pin() — default install-root resolution —
    # does not read the fake root's runtime.txt. The checkout has no
    # state/install/runtime.txt, so CI-parity means NO pin at all.
    pin = runtime_pin(env={}, warn=lambda _m: None)
    assert pin is None or pin[2] != fake_runtime_txt, (
        f"in-process runtime_pin() read the fake real install root: {pin!r}"
    )
    assert pin is None, (
        "expected no runtime pin (the pinned checkout has no runtime.txt); "
        f"got {pin!r}"
    )
