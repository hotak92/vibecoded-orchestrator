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
from vco_lib.fixture_class_guard import UNROUTABLE_SENTINEL_URL  # noqa: E402


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


# ---------------------------------------------------------------------------
# COLLECTION TIME — the moment the per-test fixture cannot reach
# ---------------------------------------------------------------------------
#
# Both pre-ship live files resolve `WEAVIATE_URL` at MODULE scope and feed it
# to an `@unittest.skipUnless(...)` evaluated at IMPORT. A stand-aside that
# lives only in a per-test fixture is therefore too late: the module already
# saw the sentinel, every live test skipped, and `scripts/pre-ship-check.sh`
# printed PASS for a leg that asserted nothing.
#
# The probe below needs no backend and writes nothing: it hands the child an
# ambient URL that is unreachable but DISTINCT (`:9998`), so the skip REASON
# names whichever value the module actually read. Sentinel in the reason means
# the stand-aside did not fire.

#: Loopback, nothing listening, and deliberately NOT a superstring of the
#: sentinel — `http://127.0.0.1:9` IS a prefix of `http://127.0.0.1:9998`, so a
#: same-host port would make the "sentinel absent" half of the assertion
#: vacuously false. Different octet, no substring relation either way.
_DISTINCT_AMBIENT = "http://127.0.0.9:9998"


def _child_skip_reasons(target: str, ambient: str) -> str:
    import subprocess

    from tests.common.child_env import child_env

    env = child_env()
    env["WEAVIATE_URL"] = ambient
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs",
         str(REPO_ROOT / "tests" / target)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    return proc.stdout


def test_the_collection_carve_out_is_exactly_the_opt_out_frozenset():
    """The decision, pure — one list drives both moments."""
    for name in sorted(_conftest._LIVE_WEAVIATE_OPT_OUT_FILES):
        assert _conftest._module_needs_ambient_weaviate_url(name), name
    for name in ("test_some_other_file.py", "test_weaviate_schema.py",
                 "conftest.py"):
        assert not _conftest._module_needs_ambient_weaviate_url(name), name
    # `None` from the hook means "use pytest's default collector".
    assert _conftest.pytest_pycollect_makemodule(
        Path("test_some_other_file.py"), None
    ) is None


def test_an_opt_out_module_reads_the_ambient_url_at_import():
    """WIRING, driven: a real child pytest, the real modules, real skip text."""
    for target in ("test_v0246_v46b_live_ci10_diff_gate.py",
                   "test_v0246_kg_sync_live.py"):
        out = _child_skip_reasons(target, _DISTINCT_AMBIENT)
        assert _DISTINCT_AMBIENT in out, (
            f"{target} did not read the ambient WEAVIATE_URL at import — "
            f"pre-ship's live leg would SKIP and still report PASS.\n{out[-1500:]}"
        )
        assert UNROUTABLE_SENTINEL_URL not in out, (
            f"{target} saw the suite pin at import:\n{out[-1500:]}"
        )


def test_a_non_opt_out_live_module_still_sees_the_pin():
    """The control — otherwise the test above would pass with no pin at all."""
    out = _child_skip_reasons("test_weaviate_schema.py", _DISTINCT_AMBIENT)
    assert UNROUTABLE_SENTINEL_URL in out, (
        f"a NON-opt-out live module reached the ambient backend:\n{out[-1500:]}"
    )
    assert _DISTINCT_AMBIENT not in out, out[-1500:]
