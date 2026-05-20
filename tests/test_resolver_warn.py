# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco_lib.resolver_warn`` — Step 17 of v0.2.21.

Covers the file-backed rate-limit policy documented in
``.claude/context/plans/v0.2.21-resolver-design.md`` §5:

* ``should_emit`` returns ``True`` on a cold cache.
* A second call within the 5-min window for the same (pid, error_kind)
  returns ``False`` (suppressed).
* After the suppression window expires, the next call returns ``True``.
* Different error_kinds in the same PID do NOT share suppression.
* ``VCO_HOOK_DEBUG=1`` bypasses suppression.
* ``record_emit`` writes one valid JSONL row with the expected shape.
* When the JSONL exceeds 1 MiB, rotation truncates it to the most-
  recent 100 entries.
* ``emit_warning_if_allowed`` writes the fixed-shape stderr line iff
  emission is allowed, and records the row.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vco_lib import resolver_warn


class _StateDirCtx:
    """Context manager that swaps ``VCT_STATE_DIR`` to a tempdir."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = mock.patch.dict(
            os.environ,
            {"VCT_STATE_DIR": self._tmp.name},
        )

    def __enter__(self) -> Path:
        self._patcher.start()
        return Path(self._tmp.name)

    def __exit__(self, *exc) -> None:
        self._patcher.stop()
        self._tmp.cleanup()


# ─── should_emit ────────────────────────────────────────────────────────


class ShouldEmitTest(unittest.TestCase):
    def test_cold_cache_returns_true(self) -> None:
        with _StateDirCtx():
            self.assertTrue(resolver_warn.should_emit("hub_unreachable"))

    def test_within_window_returns_false(self) -> None:
        with _StateDirCtx():
            # Prime: record now → should_emit False shortly after.
            resolver_warn.record_emit("hub_unreachable", "first")
            self.assertFalse(resolver_warn.should_emit("hub_unreachable"))

    def test_different_error_kind_returns_true(self) -> None:
        with _StateDirCtx():
            resolver_warn.record_emit("hub_unreachable", "first")
            # Same PID, different error_kind → not suppressed.
            self.assertTrue(resolver_warn.should_emit("project_not_registered"))

    def test_after_window_returns_true(self) -> None:
        with _StateDirCtx() as state_dir:
            # Hand-craft an old row so we don't have to sleep 5 min.
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            pid = os.getpid()
            key = f"{pid}:hub_unreachable"
            old_ts = int(time.time()) - resolver_warn.RATE_LIMIT_WINDOW_SECONDS - 10
            row = {
                "ts": old_ts,
                "pid": pid,
                "consumer": "test",
                "consumer_pid": pid,
                "error_kind": "hub_unreachable",
                "key": key,
                "detail": "stale",
                "user": "test",
            }
            jsonl.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertTrue(resolver_warn.should_emit("hub_unreachable"))

    def test_debug_env_bypasses_suppression(self) -> None:
        with _StateDirCtx():
            resolver_warn.record_emit("hub_unreachable", "first")
            with mock.patch.dict(os.environ, {"VCO_HOOK_DEBUG": "1"}):
                self.assertTrue(resolver_warn.should_emit("hub_unreachable"))

    def test_missing_cache_dir_returns_true(self) -> None:
        # Even with no cache file at all, should_emit must return True.
        with _StateDirCtx() as state_dir:
            cache = state_dir / "cache"
            if cache.exists():
                for p in cache.iterdir():
                    p.unlink()
                cache.rmdir()
            self.assertTrue(resolver_warn.should_emit("hub_unreachable"))


# ─── record_emit ────────────────────────────────────────────────────────


class RecordEmitTest(unittest.TestCase):
    def test_writes_one_row_with_expected_shape(self) -> None:
        with _StateDirCtx() as state_dir:
            resolver_warn.record_emit("hub_unreachable", "conn refused")
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            self.assertTrue(jsonl.exists())
            lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            for field in (
                "ts", "pid", "consumer", "consumer_pid",
                "error_kind", "key", "detail", "user",
            ):
                self.assertIn(field, row)
            self.assertEqual(row["error_kind"], "hub_unreachable")
            self.assertEqual(row["detail"], "conn refused")
            self.assertEqual(row["pid"], os.getpid())
            self.assertEqual(row["consumer_pid"], os.getpid())
            self.assertEqual(row["key"], f"{os.getpid()}:hub_unreachable")

    def test_detail_is_byte_capped(self) -> None:
        long_detail = "x" * (resolver_warn.DETAIL_MAX_BYTES + 500)
        with _StateDirCtx() as state_dir:
            resolver_warn.record_emit("hub_unreachable", long_detail)
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            row = json.loads(jsonl.read_text().splitlines()[0])
            self.assertLessEqual(
                len(row["detail"].encode("utf-8")),
                resolver_warn.DETAIL_MAX_BYTES,
            )

    def test_multiple_calls_append(self) -> None:
        with _StateDirCtx() as state_dir:
            resolver_warn.record_emit("hub_unreachable", "a")
            resolver_warn.record_emit("project_not_registered", "b")
            resolver_warn.record_emit("field_not_found", "c")
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3)
            kinds = [json.loads(ln)["error_kind"] for ln in lines]
            self.assertEqual(
                kinds,
                ["hub_unreachable", "project_not_registered", "field_not_found"],
            )


# ─── Rotation ───────────────────────────────────────────────────────────


class RotationTest(unittest.TestCase):
    def test_rotation_kicks_in_above_threshold(self) -> None:
        with _StateDirCtx() as state_dir:
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            # Write 1.5 MB of synthetic rows. Each row ~150 bytes.
            pid = os.getpid()
            row = {
                "ts": int(time.time()),
                "pid": pid,
                "consumer": "test",
                "consumer_pid": pid,
                "error_kind": "hub_unreachable",
                "key": f"{pid}:hub_unreachable",
                "detail": "x" * 50,
                "user": "test",
            }
            line = json.dumps(row) + "\n"
            n = (1_600_000 // len(line)) + 1
            with open(jsonl, "w", encoding="utf-8") as f:
                for _ in range(n):
                    f.write(line)
            pre_size = jsonl.stat().st_size
            self.assertGreater(pre_size, resolver_warn.ROTATION_THRESHOLD_BYTES)

            # Trigger rotation by appending one more row.
            resolver_warn.record_emit("project_not_registered", "trigger")

            post_lines = [
                ln for ln in jsonl.read_text().splitlines() if ln.strip()
            ]
            self.assertLessEqual(
                len(post_lines), resolver_warn.ROTATION_KEEP_LINES
            )
            # The last row must be the trigger we just appended.
            last = json.loads(post_lines[-1])
            self.assertEqual(last["error_kind"], "project_not_registered")
            self.assertEqual(last["detail"], "trigger")

    def test_rotation_does_not_fire_below_threshold(self) -> None:
        with _StateDirCtx() as state_dir:
            resolver_warn.record_emit("hub_unreachable", "small")
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            self.assertLess(jsonl.stat().st_size, 4096)  # tiny file
            # No truncation should have happened.
            lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)


# ─── emit_warning_if_allowed ────────────────────────────────────────────


class EmitWarningIfAllowedTest(unittest.TestCase):
    def test_first_call_writes_stderr_line_and_records(self) -> None:
        with _StateDirCtx() as state_dir:
            buf = io.StringIO()
            with mock.patch("sys.stderr", buf):
                emitted = resolver_warn.emit_warning_if_allowed(
                    "hub_unreachable", "conn refused"
                )
            self.assertTrue(emitted)
            line = buf.getvalue()
            self.assertIn("[vct] project_config: hub_unreachable", line)
            self.assertIn("conn refused", line)
            self.assertIn("Falling back to env", line)
            self.assertIn("rate-limited", line)
            self.assertIn("VCO_HOOK_DEBUG=1", line)
            # JSONL row recorded too.
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            self.assertTrue(jsonl.exists())

    def test_second_call_within_window_suppressed(self) -> None:
        with _StateDirCtx():
            buf1 = io.StringIO()
            buf2 = io.StringIO()
            with mock.patch("sys.stderr", buf1):
                self.assertTrue(
                    resolver_warn.emit_warning_if_allowed("hub_unreachable", "a")
                )
            with mock.patch("sys.stderr", buf2):
                self.assertFalse(
                    resolver_warn.emit_warning_if_allowed("hub_unreachable", "b")
                )
            self.assertEqual(buf2.getvalue(), "")

    def test_different_error_kind_not_suppressed(self) -> None:
        with _StateDirCtx():
            buf1 = io.StringIO()
            buf2 = io.StringIO()
            with mock.patch("sys.stderr", buf1):
                resolver_warn.emit_warning_if_allowed("hub_unreachable", "a")
            with mock.patch("sys.stderr", buf2):
                self.assertTrue(
                    resolver_warn.emit_warning_if_allowed(
                        "project_not_registered", "b"
                    )
                )
            self.assertIn("project_not_registered", buf2.getvalue())

    def test_debug_env_bypasses_suppression(self) -> None:
        with _StateDirCtx():
            buf1 = io.StringIO()
            buf2 = io.StringIO()
            with mock.patch("sys.stderr", buf1):
                resolver_warn.emit_warning_if_allowed("hub_unreachable", "a")
            with mock.patch.dict(os.environ, {"VCO_HOOK_DEBUG": "1"}):
                with mock.patch("sys.stderr", buf2):
                    self.assertTrue(
                        resolver_warn.emit_warning_if_allowed(
                            "hub_unreachable", "b"
                        )
                    )
            self.assertIn("hub_unreachable", buf2.getvalue())


# ─── Concurrent-append robustness ───────────────────────────────────────


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_appends_do_not_lose_rows(self) -> None:
        """20 thread-pool appends should land 20 valid JSONL rows.

        Smoke test for the flock-based atomic-append path; doesn't
        guarantee zero interleaving on every filesystem but does catch
        gross corruption.
        """
        import concurrent.futures

        with _StateDirCtx() as state_dir:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                futures = [
                    ex.submit(
                        resolver_warn.record_emit,
                        f"err_{i % 4}",
                        f"detail-{i}",
                    )
                    for i in range(20)
                ]
                for f in futures:
                    f.result()
            jsonl = state_dir / "cache" / "resolver_warn.jsonl"
            lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 20)
            for ln in lines:
                # Every line must be valid JSON.
                json.loads(ln)


if __name__ == "__main__":
    unittest.main()
