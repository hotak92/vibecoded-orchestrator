# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.paths.resolve_project_name — hub-failure warning (RT-11)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import paths  # noqa: E402


def test_hub_failure_emits_warning(caplog, tmp_path):
    """When the hub-resolver raises, resolve_project_name logs a warning
    and falls through to env-based resolution.

    RT-11: replaces the bare `except Exception: pass` silent swallow with
    `logger.warning(...)` so drift is visible in operator logs.
    """
    err_msg = "connection refused (hub is down)"

    def _bad_resolve(_cwd):
        raise RuntimeError(err_msg)

    # Verify import works — real test below.
    with caplog.at_level(logging.WARNING, logger="vco_lib.paths"):
        with mock.patch(
            "vco_lib.project_config.resolve", side_effect=RuntimeError(err_msg)
        ):
            with mock.patch.dict(
                "os.environ",
                {"CODE_GRAPH_PROJECT": "FallbackProject", "PROJECT_NAME": ""},
                clear=False,
            ):
                result = paths.resolve_project_name(cwd=tmp_path)

    assert result == "FallbackProject", f"expected fallback result, got {result!r}"
    # The warning must be present in the log records.
    hub_warnings = [
        r for r in caplog.records
        if r.name == "vco_lib.paths" and r.levelno == logging.WARNING
    ]
    assert hub_warnings, (
        "Expected a WARNING from vco_lib.paths when hub-resolver fails, but none found. "
        "Check that resolve_project_name logs the exception before falling through."
    )
    assert err_msg in hub_warnings[0].message, (
        f"Warning message should include the exception text. Got: {hub_warnings[0].message!r}"
    )


def test_hub_failure_warning_contains_resolver_name(caplog, tmp_path):
    """The warning text must identify resolve_project_name as the source."""
    with caplog.at_level(logging.WARNING, logger="vco_lib.paths"):
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=ConnectionError("hub offline"),
        ):
            with mock.patch.dict("os.environ", {}, clear=False):
                paths.resolve_project_name(cwd=tmp_path)

    hub_warnings = [
        r for r in caplog.records
        if r.name == "vco_lib.paths" and r.levelno == logging.WARNING
    ]
    assert hub_warnings, "Expected WARNING on hub failure"
    assert "resolve_project_name" in hub_warnings[0].message


def test_no_warning_when_hub_succeeds(caplog, tmp_path):
    """No warning should appear when the hub-resolver returns normally."""
    fake_cfg = mock.Mock()
    fake_cfg.code_graph_project = "SuccessProject"

    with caplog.at_level(logging.WARNING, logger="vco_lib.paths"):
        with mock.patch("vco_lib.project_config.resolve", return_value=fake_cfg):
            result = paths.resolve_project_name(cwd=tmp_path)

    assert result == "SuccessProject"
    hub_warnings = [
        r for r in caplog.records
        if r.name == "vco_lib.paths" and r.levelno == logging.WARNING
    ]
    assert not hub_warnings, f"Unexpected warnings on success: {hub_warnings}"
