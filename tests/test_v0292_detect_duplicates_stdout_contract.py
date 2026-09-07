# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``detect_duplicates.py --json`` — stdout carries ONE document (v0.2.92).

Field finding (2026-09-05, while settling why the launcher's
``orchestrator_core::kg_check_duplicates`` parse had a ``stdout.find('{')``
salvage): the salvage was load-bearing, and the polluter was this shipped
script itself.

``DuplicateDetector.find_duplicates()`` runs BEFORE the ``--json`` branch and
printed its banner, node count, per-10 progress lines and its ``except``
branch to **stdout**. So every ``--json`` run emitted ~5 prose lines in front
of the payload — while this same file's comments asserted the opposite
("Machine-readable mode: ONLY the JSON document on stdout"). A claim in a
comment is not a guard; this file is the guard.

The tests DRIVE the real script through its real CLI. They do not scan the
source for ``print(``: a name in a comment satisfies a source scan, and the
whole point is that the source ALREADY claimed to be correct.

Weaviate is not required — the connection failure is itself an exercise of
the error path we most care about (the ``except`` branch is exactly where
``str(exc)`` used to reach stdout, and exception text routinely contains
``{``, which is what would make the launcher's salvage slice into the error).
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from tests.common.child_env import child_env

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "templates" / "scripts" / "detect_duplicates.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    """Drive the shipped script, pointed at a certainly-dead Weaviate.

    A dead backend is deliberate: it drives the constructor/except paths that
    used to write to stdout, without needing a live service in CI.
    """
    # Minimal hermetic base (not os.environ), then child_env() pins the
    # checkout FIRST on PYTHONPATH + $VCT_ORCHESTRATOR_ROOT so the child
    # imports this tree's vco_lib, never a stale site-packages copy.
    # `base` is POSITIONAL-ONLY in tests/common/child_env.py — passing it
    # as a keyword silently lands in **overrides on installs holding an
    # older shadow copy of the helper.
    env = child_env(
        {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": "/tmp",
        },
        WEAVIATE_URL="http://127.0.0.1:9",
        KG_COLLECTION="NoSuchCollection_ForTest",
        GRPC_PORT="9",
        # Keep the module's steerable sys.path probe off the real home.
        VCT_CLAUDE_DIR="/tmp/vct-claude-dir-that-does-not-exist",
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(REPO),
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"shipped script missing: {SCRIPT}"


def test_json_mode_stdout_is_empty_or_exactly_one_json_document() -> None:
    """The contract: nothing but the payload on stdout under ``--json``.

    If the run gets far enough to emit a payload, stdout must STRICT-parse as
    a whole (no leading prose, no trailing prose). If it dies before emitting
    one, stdout must be empty — a crash is allowed to produce no document, but
    never a document with prose around it.

    Mutation check: revert any ``_progress(...)`` call in ``find_duplicates``
    to a bare ``print(...)`` and this fails — stdout gains the banner line.
    """
    proc = _run("--json", "--threshold", "0.99")
    out = proc.stdout
    if out.strip() == "":
        return  # died before the payload; nothing claimed, nothing polluted
    payload = json.loads(out)  # STRICT: the whole stream, not a slice
    assert set(payload) >= {"threshold", "count", "pairs"}, payload


def test_json_mode_never_writes_progress_prose_to_stdout() -> None:
    """No banner / progress / error prose may appear on stdout under --json.

    Pins the specific strings that were the field pollution, so a future
    refactor that reintroduces any one of them is caught by name.
    """
    proc = _run("--json", "--threshold", "0.99")
    for marker in (
        "Scanning for duplicates",
        "Collection:",
        "nodes to analyze",
        "Progress:",
        "Analysis complete",
        "Error during duplicate detection",
    ):
        assert marker not in proc.stdout, (
            f"`{marker}` reached STDOUT under --json. stdout is a machine "
            f"contract there; route it through `_progress`. stdout was:\n"
            f"{proc.stdout[:600]}"
        )


def _load_module():
    """Import the shipped script as a module, by path.

    Importing (rather than scanning) is the point: these tests DRIVE
    ``find_duplicates`` so the routing is proven by execution. Module import
    opens no connection — ``DuplicateDetector.__init__`` does, and we bypass
    it with ``__new__``.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_dd_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _Page:
    """One `fetch_objects` page. Empty ⇒ the scan completes with 0 nodes."""

    objects: list = []


class _Query:
    def __init__(self, boom: bool) -> None:
        self._boom = boom

    def fetch_objects(self, **_kw):
        if self._boom:
            # Exception text carrying a '{' — the exact shape that made the
            # launcher's `stdout.find('{')` salvage slice into the ERROR TEXT.
            raise RuntimeError('grpc failed: {"code": 3, "detail": "bad target"}')
        return _Page()


class _Collection:
    def __init__(self, boom: bool = False) -> None:
        self.query = _Query(boom)


def _drive_find_duplicates(mod, *, json_mode: bool, boom: bool):
    """Run the REAL ``find_duplicates`` with a fake collection.

    Returns ``(stdout, stderr)`` captured around the call.
    """
    import contextlib

    det = mod.DuplicateDetector.__new__(mod.DuplicateDetector)
    det.threshold = 0.99
    det.collection = _Collection(boom)

    prior = mod._PROGRESS_TO_STDERR
    mod._PROGRESS_TO_STDERR = json_mode
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            det.find_duplicates()
    finally:
        mod._PROGRESS_TO_STDERR = prior
    return out.getvalue(), err.getvalue()


def test_json_mode_routes_the_scan_banner_to_stderr_not_stdout() -> None:
    """Drives the real scan. Under --json every line lands on stderr.

    Mutation check: revert any ``_progress(...)`` in ``find_duplicates`` to a
    bare ``print(...)`` and this fails — that line reappears on stdout.
    """
    mod = _load_module()
    out, err = _drive_find_duplicates(mod, json_mode=True, boom=False)
    assert out == "", f"nothing may reach stdout under --json; got: {out!r}"
    for marker in ("Scanning for duplicates", "nodes to analyze", "Analysis complete"):
        assert marker in err, f"`{marker}` must still be emitted, on stderr; got: {err!r}"


def test_json_mode_routes_the_error_branch_to_stderr_too() -> None:
    """The ``except`` branch is the one that mattered most.

    It prints ``str(exc)``, and exception text routinely contains ``{`` — that
    is what would make a first-'{' salvage parse the error instead of the
    payload. It must not reach stdout.
    """
    mod = _load_module()
    out, err = _drive_find_duplicates(mod, json_mode=True, boom=True)
    assert out == "", f"the error branch must not reach stdout; got: {out!r}"
    assert "Error during duplicate detection" in err
    assert '{"code": 3' in err, "the exception text itself belongs on stderr"


def test_human_mode_keeps_every_line_on_stdout() -> None:
    """The leave-alone half: without --json nothing moved.

    That path has no document contract, so relocating its output would be an
    unrequested behaviour change for a terminal user.
    """
    mod = _load_module()
    out, err = _drive_find_duplicates(mod, json_mode=False, boom=False)
    for marker in ("Scanning for duplicates", "nodes to analyze", "Analysis complete"):
        assert marker in out, f"human mode must keep `{marker}` on stdout; got: {out!r}"
    assert "Scanning for duplicates" not in err


# ═══════════════════════════════════════════════════════════════════════════
# A FAILED scan is not a CLEAN one (v0.2.92)
#
# `find_duplicates` catches every exception, prints it and returns `[]`. The
# CLI then exited 0 and said "✅ No duplicates detected", and the launcher's
# `kg_check_duplicates` read the empty `pairs` list as a verdict. Absent is
# not decided. These drive the real `main()` — with the real `find_duplicates`
# bound onto a doubles-based detector — so the decision is exercised, not
# asserted about.
# ═══════════════════════════════════════════════════════════════════════════


def _run_main(mod, argv: list[str], *, boom: bool):
    """Drive the real ``main()`` over a fake backend. Returns (rc, out, err)."""
    import contextlib

    real_find = mod.DuplicateDetector.find_duplicates

    class _FakeDetector:
        scan_error = None
        find_duplicates = real_find  # the REAL scan logic

        def __init__(self, similarity_threshold: float = 0.95) -> None:
            self.threshold = similarity_threshold
            self.collection = _Collection(boom)

        def close(self) -> None:
            pass

    prior_cls = mod.DuplicateDetector
    prior_argv = sys.argv
    prior_flag = mod._PROGRESS_TO_STDERR
    mod.DuplicateDetector = _FakeDetector
    sys.argv = ["detect_duplicates.py", *argv]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main()
    finally:
        mod.DuplicateDetector = prior_cls
        sys.argv = prior_argv
        mod._PROGRESS_TO_STDERR = prior_flag
    return rc, out.getvalue(), err.getvalue()


def test_failed_scan_exits_nonzero_in_json_mode() -> None:
    """THE ACT half. A query error must not exit 0 with an empty `pairs`.

    The launcher checks the exit status before parsing, so a non-zero code is
    what stops `{"count": 0}` from rendering as "0 duplicates" after a failure.

    Mutation check: drop the `scan_error` assignment in `find_duplicates`'s
    `except` and this returns 0.
    """
    mod = _load_module()
    rc, out, _err = _run_main(mod, ["--json", "--threshold", "0.99"], boom=True)
    assert rc == 1, f"a failed scan must exit non-zero; got {rc}"
    # The payload is still emitted (partial results are data), and stdout is
    # still exactly one document.
    assert json.loads(out)["count"] == 0


def test_clean_scan_still_exits_zero_in_json_mode() -> None:
    """THE LEAVE-ALONE half. A genuinely clean scan must stay exit 0.

    Without this, "make failures loud" could silently become "always fail",
    which would break the launcher's happy path and the every-10-edits hook.
    """
    mod = _load_module()
    rc, out, _err = _run_main(mod, ["--json", "--threshold", "0.99"], boom=False)
    assert rc == 0, f"a clean scan must exit 0; got {rc}"
    assert json.loads(out) == {"threshold": 0.99, "count": 0, "pairs": []}


def test_human_mode_does_not_call_a_failed_scan_clean() -> None:
    """The human path told the same lie in prose. It must not any more."""
    mod = _load_module()
    rc, out, _err = _run_main(mod, ["--threshold", "0.99"], boom=True)
    assert rc == 1
    assert "knowledge graph is clean" not in out, (
        f"a failed scan must not be reported as clean; stdout was:\n{out}"
    )
    assert "did NOT complete" in out


def test_human_mode_still_reports_a_clean_graph_as_clean() -> None:
    """Leave-alone half of the prose change."""
    mod = _load_module()
    rc, out, _err = _run_main(mod, ["--threshold", "0.99"], boom=False)
    assert rc == 0
    assert "knowledge graph is clean" in out
