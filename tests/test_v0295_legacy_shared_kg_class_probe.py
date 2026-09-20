# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 — the missing behavioural cover for the PR-34 legacy-shared-KG probe.

``_detect_legacy_shared_kg_class`` shipped in v0.2.12 (Group M) and was never
covered by a behavioural test. The only reference to it anywhere under
``tests/`` was one sentence in another module's docstring, and its condition id
``legacy_shared_kg_class_present`` appeared only in the registry-OWNERSHIP pin
(which asserts the id is registered, not that anything emits it).

The v0.2.95 ratchet lane moved the probe into ``vco_lib.install_weaviate`` and
tried to red-proof the move against an existing assertion. Nothing went red: a
FULL-suite run with the legacy class name deliberately corrupted — so the probe
could never match and the deferral could never fire — stayed green apart from
the four failures already known to be unrelated. A move whose loss no test can
see is exactly the shape a shared-component extraction hides, so the gap is
closed here, at the point it was found.

What this pins, in the order a reader needs it:

  1. the DECISION — a pre-v0.2.12 class on disk produces exactly one
     ``legacy_shared_kg_class_present`` entry; its absence produces none;
  2. the v0.2.23 B1 case-insensitive leg — the lowercase-c canonical counts as
     "already present", so a user upgrading from the v0.2.12–v0.2.22 range is
     not told their canonical was never created;
  3. the SOFT-FAIL contract — an unreachable Weaviate emits nothing and raises
     nothing (the schema-rebuild flow upstream owns that deferral);
  4. the NON-DESTRUCTIVE contract — the probe reads the schema and emits a
     deferral, and never issues a write/delete of its own. The picker is the
     consent mechanism; auto-dropping a populated class would lose every
     cross-project node the user has written;
  5. the WIRING — install.py's thin wrapper threads its ``_log_install_event``
     into the vco_lib home. That coupling is what the extraction had to carry
     across, and it is the one a future edit can drop while every other
     assertion here still passes.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402 — install.py is at repo root
from vco_lib import install_weaviate as _iw  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

CID = "legacy_shared_kg_class_present"
LEGACY = "VibeCodedTools_KnowledgeGraph"
CANONICAL = "VibeCodedOrchestrator_KnowledgeGraph"
LOWERCASE_C = "VibecodedOrchestrator_KnowledgeGraph"


class _FakeResponse:
    """Minimal stand-in for what ``urlopen`` hands back."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


def _schema(*class_names: str) -> dict:
    return {"classes": [{"class": n} for n in class_names]}


class LegacySharedKgClassProbeTests(unittest.TestCase):
    """Drive the probe through install.py's wrapper — the shipped entry point."""

    def _run(self, schema_or_exc, *, record_log: bool = False):
        """Return (report, urlopen_mock, logged_events)."""
        logged: list = []

        def _recorder(step, phase, detail="", data=None, actor="install.py"):
            logged.append((step, phase, detail, data))

        if isinstance(schema_or_exc, Exception):
            side_effect, return_value = schema_or_exc, None
        else:
            side_effect, return_value = None, _FakeResponse(schema_or_exc)

        report = DeferralReport()
        with mock.patch.object(
            urllib.request, "urlopen",
            side_effect=side_effect, return_value=return_value,
        ) as urlopen_mock:
            if record_log:
                with mock.patch.object(install, "_log_install_event", _recorder):
                    install._detect_legacy_shared_kg_class(report)
            else:
                install._detect_legacy_shared_kg_class(report)
        return report, urlopen_mock, logged

    def _cids(self, report) -> list:
        return [e.condition_id for e in report.entries]

    # ── 1. the decision ────────────────────────────────────────────────────
    def test_legacy_class_present_emits_exactly_one_entry(self):
        report, _, _ = self._run(_schema(LEGACY, "SomeProject_KnowledgeGraph"))
        self.assertEqual(
            self._cids(report), [CID],
            "a pre-v0.2.12 shared-KG class on disk must produce exactly one "
            "deferral — the user's only notice that their cross-project KG "
            "is bound to a class VCO no longer writes to",
        )
        entry = report.entries[0]
        self.assertEqual(entry.severity, "info")
        self.assertIn(LEGACY, entry.detected)
        self.assertIn("not yet created", entry.detected)

    def test_legacy_class_absent_emits_nothing(self):
        report, _, _ = self._run(_schema(CANONICAL, "SomeProject_Development"))
        self.assertEqual(
            self._cids(report), [],
            "a Weaviate with no legacy class is the ordinary post-v0.2.12 "
            "state — emitting there would train users to ignore the ledger",
        )

    def test_canonical_alongside_legacy_is_reported_as_present(self):
        report, _, _ = self._run(_schema(LEGACY, CANONICAL))
        self.assertIn("already present", report.entries[0].detected)

    # ── 2. the v0.2.23 B1 case-insensitive leg ─────────────────────────────
    def test_lowercase_c_canonical_counts_as_present(self):
        """v0.2.23 B1: the v0.2.12–v0.2.22 default spelled the canonical with a
        lowercase c. Treating it as "not yet created" would tell a user
        upgrading from that range to create a class they already have."""
        report, _, _ = self._run(_schema(LEGACY, LOWERCASE_C))
        self.assertIn(
            "already present", report.entries[0].detected,
            "the lowercase-c canonical was not recognised — the B1 "
            "case-insensitive leg is gone",
        )

    # ── 3. soft-fail ───────────────────────────────────────────────────────
    def test_unreachable_weaviate_is_silent_and_does_not_raise(self):
        report, _, _ = self._run(OSError("connection refused"))
        self.assertEqual(
            self._cids(report), [],
            "Weaviate being down is already reported by the schema-rebuild "
            "flow; a second entry from here would double-report it",
        )

    def test_unparseable_schema_is_silent_and_does_not_raise(self):
        class _Garbage:
            def read(self):
                return b"<html>not json</html>"

        report = DeferralReport()
        with mock.patch.object(
            urllib.request, "urlopen", return_value=_Garbage(),
        ):
            install._detect_legacy_shared_kg_class(report)  # must not raise
        self.assertEqual(self._cids(report), [])

    # ── 4. non-destructive ─────────────────────────────────────────────────
    def test_probe_only_reads_the_schema(self):
        """The deferral text promises VCO never auto-renames or auto-drops the
        class. Pin the promise: the ONE request is a GET-shaped read of
        /v1/schema, and it targets the env-resolved endpoint rather than a
        hardcoded host."""
        import os

        report, urlopen_mock, _ = self._run(_schema(LEGACY))
        self.assertEqual(urlopen_mock.call_count, 1)
        url = urlopen_mock.call_args.args[0]
        self.assertTrue(url.endswith("/v1/schema"), url)
        expected_base = (
            os.environ.get("WEAVIATE_URL")
            or f"http://localhost:{os.environ.get('WEAVIATE_PORT', '8081')}"
        )
        self.assertEqual(url, f"{expected_base}/v1/schema")
        # The entry directs the user at the consent surface, not at a command
        # that would destroy data.
        self.assertIn("Manage shared KG", report.entries[0].command_to_apply)

    # ── 5. the wiring the v0.2.95 extraction had to carry across ───────────
    def test_wrapper_threads_install_pys_logger_into_the_vco_lib_home(self):
        """install.py keeps a thin wrapper whose entire job is to supply
        ``_log_install_event``. Drop that keyword and the probe still emits its
        deferral — every other assertion in this module stays green — while the
        install-event log silently loses the record. This is the assertion that
        sees it."""
        _, _, logged = self._run(_schema(LEGACY), record_log=True)
        matching = [
            ev for ev in logged
            if ev[1] == "info" and isinstance(ev[3], dict)
            and ev[3].get("legacy_class") == LEGACY
        ]
        self.assertTrue(
            matching,
            "install.py's wrapper did not thread its _log_install_event into "
            f"vco_lib.install_weaviate.detect_legacy_shared_kg_class; got "
            f"{logged!r}",
        )
        self.assertEqual(matching[0][0], "7d/10")

    def test_the_wrapper_delegates_to_the_vco_lib_home(self):
        """Pin the delegation itself, so the wrapper cannot quietly regrow a
        second copy of the probe inside install.py (the monolith ratchet the
        extraction served would then pass while the duplication returned)."""
        report = DeferralReport()
        with mock.patch.object(
            _iw, "detect_legacy_shared_kg_class",
        ) as impl:
            install._detect_legacy_shared_kg_class(report)
        impl.assert_called_once()
        self.assertIs(impl.call_args.args[0], report)
        self.assertIs(
            impl.call_args.kwargs.get("log_event"),
            install._log_install_event,
        )


if __name__ == "__main__":
    unittest.main()
