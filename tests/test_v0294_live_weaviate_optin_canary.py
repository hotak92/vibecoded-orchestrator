# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wiring proof for the live-Weaviate stand-aside (v0.2.94 W-WEAVIATE).

``tests/conftest.py`` pins ``WEAVIATE_URL`` at an unroutable sentinel for every
test file EXCEPT the handful in ``_LIVE_WEAVIATE_OPT_OUT_FILES`` — the
live-backend tests a shipped gate runs on purpose. That stand-aside is
load-bearing: if it silently stopped firing, ``scripts/pre-ship-check.sh``'s
live diff-gate would SKIP, pytest would exit 0, and the gate would print PASS
for a run that asserted nothing.

Nothing else observes the stand-aside — the files that depend on it SKIP when
the backend is absent, which is exactly what a broken stand-aside also looks
like. So this file is IN the opt-out list purely to be the observer, the same
way ``test_v0294_python_exe_spawn_guard.py`` sits in the resync-spawn list. It
asserts the mechanism, never a backend: no socket is opened here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import conftest as _conftest  # noqa: E402


def test_this_file_is_registered_as_a_stand_aside():
    assert (
        Path(__file__).name in _conftest._LIVE_WEAVIATE_OPT_OUT_FILES
    ), "the canary only proves anything while it is in the opt-out list"


def test_the_pin_stood_aside_and_the_ambient_value_survived():
    """The whole claim: an opted-in file sees the environment, not the pin.

    ``None`` (the key absent) is a real, required outcome — a live test doing
    ``os.environ.get("WEAVIATE_URL", "http://localhost:8081")`` needs the
    ABSENCE to reach its own default, so restoring an empty string instead
    would break it.
    """
    ambient = _conftest._AMBIENT_WEAVIATE_URL
    if ambient is None:
        assert "WEAVIATE_URL" not in os.environ, (
            "the stand-aside must REMOVE the key when the ambient env had "
            "none, not leave an empty string behind"
        )
    else:
        assert os.environ.get("WEAVIATE_URL") == ambient


def test_a_non_opted_out_file_would_still_be_pinned():
    """The stand-aside is a carve-out, not a hole: the default still holds."""
    from vco_lib.fixture_class_guard import UNROUTABLE_SENTINEL_URL

    assert (
        _conftest._weaviate_url_pin_for("test_some_other_file.py")
        == UNROUTABLE_SENTINEL_URL
    )
