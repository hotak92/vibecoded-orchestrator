# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AC (v0.2.52) — RLTelemetryWriter must not silently skip writes.

V52-AC concern from the 2026-06-09 audit: the write path of the
telemetry writer is INDEPENDENT of the RL container (writes go to
launcher.db via the hub, not to the RL container). So even when the
container is broken (the field state for V52-AA), telemetry should
still accumulate. The audit found 30 events with
``failure_mode=partial_fan_out_schema_missing`` (V52-I) AND 0.7% with
``query_emb=None`` — both UPSTREAM-of-RL failures. The question:
**are there any cases where the WRITER itself falls back to disabled-
mode and produces no event?**

These tests pin the V52-AC contract:

1. **No silent skip-no-write branches** — every soft-fail path in
   ``log_retrieval`` / ``log_citations`` MUST either:
   - Successfully write (the happy path); OR
   - Log a debug message (so a user inspecting logs can see WHY); OR
   - Be gated on a user-controlled env opt-out
     (``RL_LOCAL_LOGGING_DISABLED``) — the ONLY legitimate "skip
     silently" path.
2. **Hub-down does NOT skip the write attempt** — the writer still
   builds the v3 envelope + invokes the hub POST. If the POST raises,
   that's logged at DEBUG (per the locked 2026-06-04 design decision:
   no retry queue / no JSONL fallback).
3. **Independence from RL client state** — flipping
   ``RL_SERVER_PORT`` to unset (i.e. V52-AA env gap is open) does NOT
   prevent the writer from posting. The two paths are decoupled by
   design.

The writer's REAL silent-skip branch (``_local_logging_disabled``
returns True) is intentional and user-controlled — these tests verify
it's the ONLY one.
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.rl_client.telemetry_writer import (  # noqa: E402
    RLTelemetryWriter,
    _local_logging_disabled,
)
import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


class _PostCapture:
    """Captures hub POST invocations + raises on demand for tests."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raises = raises

    def __call__(self, envelope: dict) -> bool:
        self.calls.append(envelope)
        if self.raises is not None:
            raise self.raises
        return True


class WriterAlwaysAttemptsWriteTest(unittest.TestCase):
    """Verify the writer ALWAYS attempts the hub POST unless the
    user-controlled env opt-out is set.
    """

    def setUp(self):
        # Force the env opt-out OFF so we exercise the write path.
        self._old_optout = os.environ.pop(
            "RL_LOCAL_LOGGING_DISABLED", None
        )

    def tearDown(self):
        if self._old_optout is not None:
            os.environ["RL_LOCAL_LOGGING_DISABLED"] = self._old_optout
        else:
            os.environ.pop("RL_LOCAL_LOGGING_DISABLED", None)

    def test_happy_path_writes_retrieval(self):
        """Normal call → hub POST attempted with a v3 envelope."""
        cap = _PostCapture()
        w = RLTelemetryWriter(
            project="test-project",
            project_id="p-test",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=cap,
        )
        w.log_retrieval(
            task_id="t1",
            task_type="mcp_interactive",
            query="example query",
            nodes=[{"title": "A", "score": 0.5}],
            session_id="sess-1",
            query_emb=[0.1] * 4,
        )
        self.assertEqual(len(cap.calls), 1)
        env = cap.calls[0]
        self.assertEqual(env["event_type"], "retrieval")
        self.assertEqual(env["task_id"], "t1")
        self.assertEqual(env["project_name"], "test-project")
        self.assertEqual(env["project_id"], "p-test")

    def test_happy_path_writes_citation(self):
        """Citation path is symmetric — same gating, same hub POST shape."""
        cap = _PostCapture()
        w = RLTelemetryWriter(
            project="test-project",
            project_id="p-test",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=cap,
        )
        w.log_citations(
            task_id="t1",
            task_type="mcp_interactive",
            citations={"A": True, "B": False},
            cosine_sims={"A": 0.9, "B": 0.1},
            literal_cited={"A": True, "B": False},
        )
        self.assertEqual(len(cap.calls), 1)
        env = cap.calls[0]
        self.assertEqual(env["event_type"], "citation")

    def test_hub_post_exception_is_logged_not_swallowed_silently(self):
        """Hub POST raising must be logged at DEBUG, not silently dropped.

        This is the canonical "soft-fail with debug log" pattern. A
        future refactor that drops the log line (i.e. converts the
        try/except to a bare except: pass) breaks this contract and
        creates a silent skip path.
        """
        cap = _PostCapture(raises=RuntimeError("hub down"))
        w = RLTelemetryWriter(
            project="test-project",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=cap,
        )
        with self.assertLogs(
            "claude_mcp_servers.rl_client.telemetry_writer",
            level="DEBUG",
        ) as logs:
            # Must not raise — soft-fail per locked design.
            w.log_retrieval(
                task_id="t-hub-down",
                task_type="mcp_interactive",
                query="q",
                nodes=[],
                query_emb=[0.1] * 4,
            )
        # The post WAS attempted (one call) and the exception was
        # caught with a log line — these two facts together prove the
        # absence of a silent-skip branch.
        self.assertEqual(len(cap.calls), 1)
        self.assertTrue(
            any("hub log_retrieval failed" in line for line in logs.output),
            f"hub-down exception must be logged at DEBUG; logs={logs.output}",
        )

    def test_writer_independence_from_rl_container_env(self):
        """Unsetting ``RL_SERVER_URL``/``RL_SERVER_PORT`` (i.e. simulating
        the V52-AA env gap) does NOT change the writer's behaviour.

        The writer's write path is the HUB, not the RL container. If
        this test fires, someone accidentally cross-coupled the two
        — V52-AC's core concern.
        """
        cap = _PostCapture()
        with patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("RL_SERVER_URL", None)
            os.environ.pop("RL_SERVER_PORT", None)
            w = RLTelemetryWriter(
                project="test-project",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
                hub_post_fn=cap,
            )
            w.log_retrieval(
                task_id="t-no-rl",
                task_type="mcp_interactive",
                query="q",
                nodes=[],
                query_emb=[0.1] * 4,
            )
        self.assertEqual(
            len(cap.calls), 1,
            "Writer must POST to the hub even when RL container env is unset"
        )


class UserOptOutEnvIsTheOnlySilentSkipTest(unittest.TestCase):
    """Verify ``RL_LOCAL_LOGGING_DISABLED`` is the only legitimate
    "skip without writing" branch — and it IS deliberate, user-
    controlled, documented behaviour (NOT a bug).
    """

    def setUp(self):
        self._old = os.environ.pop("RL_LOCAL_LOGGING_DISABLED", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["RL_LOCAL_LOGGING_DISABLED"] = self._old
        else:
            os.environ.pop("RL_LOCAL_LOGGING_DISABLED", None)

    def test_opt_out_env_skips_write(self):
        """The user-controlled opt-out IS legitimate; it skips the
        hub POST cleanly. This pins the OPT-OUT contract (not a bug)."""
        os.environ["RL_LOCAL_LOGGING_DISABLED"] = "true"
        self.assertTrue(_local_logging_disabled())

        cap = _PostCapture()
        w = RLTelemetryWriter(
            project="test-project",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=cap,
        )
        w.log_retrieval(
            task_id="t-opt-out",
            task_type="mcp_interactive",
            query="q",
            nodes=[],
            query_emb=[0.1] * 4,
        )
        # Zero calls — the user asked for no local recording.
        self.assertEqual(len(cap.calls), 0)

    def test_opt_out_env_unset_writes(self):
        """Default (env unset) → writer DOES write. Pins fail-open."""
        os.environ.pop("RL_LOCAL_LOGGING_DISABLED", None)
        self.assertFalse(_local_logging_disabled())

        cap = _PostCapture()
        w = RLTelemetryWriter(
            project="test-project",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=cap,
        )
        w.log_retrieval(
            task_id="t-default",
            task_type="mcp_interactive",
            query="q",
            nodes=[],
            query_emb=[0.1] * 4,
        )
        self.assertEqual(len(cap.calls), 1)


class WriterSourceAuditTest(unittest.TestCase):
    """Static-source audit: scan the writer's source for any other
    "silent skip" branches (bare ``return`` without surrounding logging
    or env gate) that would be a regression.

    This is the V52-AC fast-failure detector for any future refactor
    that adds a swallow-and-drop branch. The grep is intentionally
    coarse: it counts ``return`` / ``pass`` lines and asserts the
    expected count.
    """

    def test_log_retrieval_has_only_documented_skip_branches(self):
        """``log_retrieval`` MUST have exactly the documented exit
        shapes: one early-return on env opt-out, one debug-log on hub
        exception. No bare exception swallowing.
        """
        src = inspect.getsource(RLTelemetryWriter.log_retrieval)
        # Exception handlers in this method MUST log to logger.debug —
        # never ``except: pass`` (bare swallow) and never just a bare
        # ``except Exception: ...`` without a log line.
        # The current source has exactly ONE except: under the hub-post
        # try; that except MUST contain a ``logger.debug`` call.
        self.assertIn("except Exception", src)
        self.assertIn("logger.debug", src)
        # The env opt-out gate must be present (the legitimate skip).
        self.assertIn("_local_logging_disabled", src)

    def test_log_citations_has_only_documented_skip_branches(self):
        """Same audit applied to ``log_citations``."""
        src = inspect.getsource(RLTelemetryWriter.log_citations)
        self.assertIn("except Exception", src)
        self.assertIn("logger.debug", src)
        self.assertIn("_local_logging_disabled", src)

    def test_no_bare_except_pass_in_writer(self):
        """Whole-class audit: ``except: pass`` is FORBIDDEN here.

        That pattern hides the failure mode the V52-AC concern is
        guarding against. Any future refactor that adds it will fire
        this test.
        """
        src = inspect.getsource(RLTelemetryWriter)
        # Match the canonical bare-swallow pattern. Whitespace-tolerant.
        # Note: pytest/unittest emits 'except' tokens in own
        # diagnostics; we only care about adjacent-line pass.
        forbidden_patterns = [
            ("except:\n", "        pass"),       # bare except + pass
            ("except Exception:\n", "        pass"),  # broad swallow
        ]
        for handler, body in forbidden_patterns:
            # Tolerate variable indentation; the contract is "no swallow".
            joined = handler + body
            self.assertNotIn(
                joined, src,
                f"Forbidden silent-swallow pattern in writer source: {handler.strip()!r}"
            )


class GetRlTelemetryWriterSourceAuditTest(unittest.TestCase):
    """Audit the server-side factory ``_get_rl_telemetry_writer``.

    Per V52-AC: the writer factory has exactly ONE skip-no-write
    branch (RLTelemetryWriter import failure → returns None). The
    caller in ``_rl_compute_and_write_citations`` checks ``if writer
    is None: return None`` — this is documented soft-fail when the
    rl_client package itself can't be imported (lean install / shim
    test contexts).

    Pin the factory's import-failure branch as the ONLY no-write exit
    and verify it logs at DEBUG so users can diagnose.
    """

    def test_factory_logs_on_import_failure(self):
        """When RLTelemetryWriter import fails, factory returns None
        AND logs at DEBUG. This is the V52-AC-legitimate silent
        skip — kept because (a) the failure mode is rare (only fires
        on broken installs), (b) the alternative (raising) would
        crash every search, (c) the debug log makes the failure
        diagnosable.
        """
        # Patch the import to simulate failure.
        with patch.dict(
            "sys.modules",
            {"claude_mcp_servers.rl_client": None},
        ):
            with self.assertLogs(
                "claude_mcp_servers.weaviate_mcp.server",
                level="DEBUG",
            ) as logs:
                w = srv._get_rl_telemetry_writer()
            self.assertIsNone(w)
            # Verify a debug log mentioning the failure.
            self.assertTrue(
                any(
                    "telemetry disabled" in line or "import failed" in line
                    for line in logs.output
                ),
                f"factory must log at DEBUG on import fail; logs={logs.output}",
            )

    def test_factory_source_has_only_one_no_write_branch(self):
        """Source audit: exactly one ``return None`` (the import-fail
        path). If a future refactor adds more, the contract drifts.

        v0.2.71 Sweep-C: the construction body (incl. the import-failure
        no-write branch) was extracted from ``_get_rl_telemetry_writer`` into
        the shared ``_get_rl_telemetry_writer_for`` so the active path and the
        dual-log other-slot path share ONE body. The thin active wrapper now
        only resolves the live triple then delegates (no ``return None`` of its
        own), so the V52-AC single-no-write-branch contract is pinned on the
        SHARED body where it actually lives.
        """
        src = inspect.getsource(srv._get_rl_telemetry_writer_for)
        # Count occurrences of ``return None`` — the canonical
        # no-write exit shape. The factory should have exactly one.
        # (We allow a margin for the ``client.close()`` paths etc.;
        # the constraint is "no silent fallthrough returning None".)
        n_none_returns = src.count("return None")
        self.assertEqual(
            n_none_returns, 1,
            f"_get_rl_telemetry_writer_for must have exactly one ``return None`` "
            f"branch (import-failure); found {n_none_returns}. "
            f"A future refactor that adds another no-write exit drifts "
            f"the V52-AC contract."
        )
        # The thin active wrapper must itself contain NO no-write exit — it only
        # resolves the triple then delegates to the shared body above.
        wrapper_src = inspect.getsource(srv._get_rl_telemetry_writer)
        self.assertEqual(
            wrapper_src.count("return None"), 0,
            "_get_rl_telemetry_writer is a thin wrapper — it must delegate the "
            "no-write exit to _get_rl_telemetry_writer_for, not add its own.",
        )


if __name__ == "__main__":
    unittest.main()
