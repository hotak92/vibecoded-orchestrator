# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The `--check-drift` machine-readable verdict, and its cross-language pin.

Field evidence (2026-09-09): after "Update all", the launcher logged for all 8
projects *"kg-sync skipped … on-disk KG/docs unchanged — nothing to re-embed"*,
while `kg-sync --check-drift` reported one project 67 missing + 5 stale of 78 and
another 310 missing of 327 (its collection held ZERO objects).
The gate could not see Weaviate, so it inferred a fact about the STORE from a
fact about DISK WRITES.

The launcher now asks the store. It does that by parsing ONE line the wrapper
prints — a prefixed JSON line rather than a `--json` mode (this script prints
setup chatter on stdout before `main()` runs, so "stdout is JSON" is a contract
it cannot keep) and rather than the human summary (a caller that regexes prose
pins the prose). This file pins the line's shape and its prefix against the
Rust consumer.
"""

from __future__ import annotations

import json
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
_RUST = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "kg_sync.rs"


def _sentinel_prefix_from_python() -> str:
    """Read the constant WITHOUT importing the script (it has heavy module-level
    side effects — the same reason `kg_sync_drift` mirrors its hash helper
    rather than importing it)."""
    text = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^DRIFT_SENTINEL_PREFIX = "([^"]+)"', text, re.MULTILINE)
    assert m, "DRIFT_SENTINEL_PREFIX is missing from sync_knowledge_graph.py"
    return m.group(1)


def _sentinel_prefix_from_rust() -> str:
    text = _RUST.read_text(encoding="utf-8")
    m = re.search(r'KG_DRIFT_SENTINEL: &str = "([^"]+)"', text)
    assert m, "KG_DRIFT_SENTINEL is missing from kg_sync.rs"
    return m.group(1)


class TheSentinelIsPinnedAcrossLanguages(unittest.TestCase):
    def test_the_prefixes_match(self):
        self.assertEqual(
            _sentinel_prefix_from_python(), _sentinel_prefix_from_rust(),
            "the drift sentinel prefix drifted between the emitter "
            "(sync_knowledge_graph.py) and its only consumer (kg_sync.rs) — the "
            "launcher would then silently see NO verdict, which it renders as "
            "'could not confirm' and never as 'ok', but the check would be dead",
        )

    def test_both_sides_declare_the_pin(self):
        self.assertIn(
            "MUST MATCH ``kg_sync.rs::KG_DRIFT_SENTINEL``",
            _SCRIPT.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "MUST MATCH `templates/scripts/sync_knowledge_graph.py::DRIFT_SENTINEL_PREFIX`",
            _RUST.read_text(encoding="utf-8"),
        )


class TheSentinelLineIsWellFormed(unittest.TestCase):
    """`_print_drift_sentinel` is a pure-ish reporter; load it in isolation.

    The script cannot be imported (module-level env/Weaviate resolution), so the
    function's source is extracted and exec'd with a minimal namespace. That is
    the same trick the other tests over this script use, and it keeps the
    assertion on the REAL shipped code rather than a re-typed copy.
    """

    @staticmethod
    def _load_reporter():
        text = _SCRIPT.read_text(encoding="utf-8")
        start = text.index("def _print_drift_sentinel(")
        end = text.index("\ndef _run_check_drift(")
        ns: dict = {
            "json": json,
            "sys": sys,
            "DRIFT_SENTINEL_PREFIX": _sentinel_prefix_from_python(),
        }
        exec(compile(text[start:end], str(_SCRIPT), "exec"), ns)  # noqa: S102
        return ns["_print_drift_sentinel"]

    def setUp(self):
        self.fn = self._load_reporter()

    @staticmethod
    def _capture(fn, *args):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue()

    def test_a_drift_report_serialises_counts_not_paths(self):
        binding = types.SimpleNamespace(status="bound", kg_collection="X_KG", detail="")
        report = types.SimpleNamespace(
            status="drift", scanned=78,
            missing=("knowledge/a.md",) * 67, stale=("knowledge/b.md",) * 5,
            detail="drift — 67 missing, 5 stale out of 78 checked",
        )
        line = self._capture(self.fn, binding, report).strip()
        self.assertTrue(line.startswith(_sentinel_prefix_from_python()))
        payload = json.loads(line[len(_sentinel_prefix_from_python()):])
        self.assertEqual(payload["status"], "drift")
        self.assertEqual(payload["missing"], 67)
        self.assertEqual(payload["stale"], 5)
        self.assertEqual(payload["scanned"], 78)
        self.assertEqual(payload["binding"], "bound")

    def test_a_clean_report_says_ok(self):
        binding = types.SimpleNamespace(status="bound", kg_collection="X_KG", detail="")
        report = types.SimpleNamespace(
            status="ok", scanned=78, missing=(), stale=(), detail="ok",
        )
        line = self._capture(self.fn, binding, report).strip()
        payload = json.loads(line[len(_sentinel_prefix_from_python()):])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual((payload["missing"], payload["stale"]), (0, 0))

    def test_an_unbound_project_still_emits_a_verdict(self):
        """The `unbound` early-exit must NOT print nothing.

        Silence and 'ok' would be indistinguishable to the consumer, and the
        whole defect class here is absence of evidence read as evidence of
        absence. `binding != "bound"` is what the Rust parser turns into
        `Unavailable`.
        """
        binding = types.SimpleNamespace(
            status="unbound", kg_collection="", detail="no binding resolved",
        )
        line = self._capture(self.fn, binding, None).strip()
        payload = json.loads(line[len(_sentinel_prefix_from_python()):])
        self.assertEqual(payload["binding"], "unbound")
        self.assertNotEqual(payload["status"], "ok")

    def test_the_reporter_never_raises(self):
        """A report line must not be able to break a read-only scan."""

        class _Explodes:
            def __getattr__(self, _name):
                raise RuntimeError("boom")

        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.fn(_Explodes(), _Explodes())
        self.assertIn("sentinel not emitted", err.getvalue())


class TheCheckDriftPathEmitsOnBothExits(unittest.TestCase):
    """Source-level pin: BOTH exits of `_run_check_drift` report."""

    def test_both_the_unbound_and_the_scanned_exit_emit(self):
        text = _SCRIPT.read_text(encoding="utf-8")
        body = text[text.index("def _run_check_drift("):]
        # Up to the next top-level statement (`def`, `class`, or a module-level
        # constant / `#:` doc-comment) — resilient to the function's neighbours
        # moving, which they already have once.
        nxt = re.search(r"\n(?=(?:def |class |#: |[A-Z_]+(?:\s*:\s*\w|\s*=)))", body[1:])
        self.assertIsNotNone(nxt, "could not bound _run_check_drift's body")
        assert nxt is not None  # for the type checker
        body = body[: nxt.start() + 1]
        self.assertIn("sys.exit(0)", body, "sanity: the whole body was captured")
        self.assertEqual(
            body.count("_print_drift_sentinel("), 2,
            "both exits of --check-drift (the `unbound` early return and the "
            "post-scan return) must emit a verdict; a path that exits silently "
            "reads to the launcher as 'no verdict', which is correct but blind",
        )


if __name__ == "__main__":
    unittest.main()
