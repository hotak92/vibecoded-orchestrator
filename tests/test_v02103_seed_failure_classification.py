# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — ``install_weaviate.collections_and_seed`` classifies a failure
into ONE of three arms instead of reporting every exception as "Weaviate
unreachable" and restarting the container.

* connection-class → one ``<rt> start`` + retry, then
  ``weaviate_unreachable_at_update`` (pre-existing behaviour, kept);
* ``ImportError`` → loud "broken install: could not import X" +
  ``install_python_env_broken``, NO container start;
* anything else → "KG seed failed: <type>: <msg>" + ``kg_seed_failed``,
  NO container start.

Red-proof: collapse the classifier back to "everything is unreachable" (make
``_is_unreachable`` return True) → the two non-connection tests fail on the
recorded ``podman start`` and the condition id.
"""
from __future__ import annotations

import socket
import urllib.error

import pytest

from vco_lib import install_weaviate as iw
from vco_lib.deferral_report import DeferralReport


def _run(ensure, seed=lambda: None, *, rebuild=False, monkeypatch):
    calls: list = []
    monkeypatch.setattr("vco_lib.containers.find_existing_container",
                        lambda *a, **k: "vco_weaviate")
    report = DeferralReport()
    status = iw.collections_and_seed(
        ensure, seed, report=report, rebuild_performed=rebuild,
        runtime=lambda: "podman", run=lambda argv, **k: calls.append(argv),
        sleep=lambda _s: None)
    return status, calls, report


def _raiser(exc):
    def _f():
        raise exc
    return _f


@pytest.mark.parametrize("exc", [
    TimeoutError("Weaviate readiness probe did not return 200"),
    ConnectionRefusedError(111, "Connection refused"),
    urllib.error.URLError("[Errno 111] Connection refused"),
    socket.gaierror("Name or service not known"),
    type("WeaviateStartUpError", (Exception,), {})("Could not connect to Weaviate"),
    type("WeaviateConnectionError", (Exception,), {})("down"),
])
def test_connection_class_gets_one_restart_then_unreachable(exc, monkeypatch, capsys):
    status, calls, report = _run(_raiser(exc), monkeypatch=monkeypatch)
    assert status == iw.FAILED
    assert calls == [["podman", "start", "vco_weaviate"]]
    assert [e.condition_id for e in report.entries] == ["weaviate_unreachable_at_update"]
    assert "Weaviate unreachable" in capsys.readouterr().out


def test_import_error_is_a_broken_install_and_starts_no_container(monkeypatch, capsys):
    exc = ModuleNotFoundError("No module named 'weaviate'", name="weaviate")
    status, calls, report = _run(_raiser(exc), monkeypatch=monkeypatch)
    assert status == iw.FAILED
    assert calls == [], "a broken venv must never restart Weaviate"
    ids = [e.condition_id for e in report.entries]
    assert ids == ["install_python_env_broken"]
    err = capsys.readouterr().err
    assert "broken install" in err and "could not import `weaviate`" in err
    assert "install.py --update" in err
    entry = report.entries[0]
    assert entry.severity == "critical"
    assert "weaviate" in entry.detected


def test_import_error_raised_by_the_seed_is_classified_the_same(monkeypatch):
    status, calls, report = _run(
        lambda: None, _raiser(ImportError("cannot import name 'x' from 'y'")),
        monkeypatch=monkeypatch)
    assert status == iw.FAILED and calls == []
    assert [e.condition_id for e in report.entries] == ["install_python_env_broken"]


def test_other_exception_is_reported_as_a_seed_failure_not_an_outage(monkeypatch, capsys):
    status, calls, report = _run(_raiser(KeyError("properties")), monkeypatch=monkeypatch)
    assert status == iw.FAILED
    assert calls == [], "a seed bug must never restart Weaviate"
    assert [e.condition_id for e in report.entries] == ["kg_seed_failed"]
    err = capsys.readouterr().err
    assert "KG seed failed: KeyError: 'properties'" in err
    assert "KeyError" in report.entries[0].detected


def test_file_not_found_is_not_a_connection_error(monkeypatch):
    """An OSError from a missing script is not a socket failure."""
    status, calls, report = _run(_raiser(FileNotFoundError(2, "No such file", "sync.py")),
                                 monkeypatch=monkeypatch)
    assert calls == []
    assert [e.condition_id for e in report.entries] == ["kg_seed_failed"]


@pytest.mark.parametrize("exc", [
    ValueError("embed timed out waiting for batch"),
    TypeError("could not connect the dots: unsupported operand"),
    KeyError("host unreachable in config map"),
])
def test_a_programming_error_whose_text_looks_like_an_outage_is_not_one(exc, monkeypatch):
    """R24 LOW-4: the message markers do not apply to builtin programming-error
    types — a seed bug saying "timed out" restarts no container. Red-proof:
    remove the ``_PROGRAMMING_ERROR_TYPES`` early return → fails."""
    status, calls, report = _run(_raiser(exc), monkeypatch=monkeypatch)
    assert status == iw.FAILED and calls == []
    assert [e.condition_id for e in report.entries] == ["kg_seed_failed"]


def test_rebuild_drop_still_owes_a_reseed_on_non_connection_failure(monkeypatch):
    _status, calls, report = _run(_raiser(ValueError("bad schema")), rebuild=True,
                                  monkeypatch=monkeypatch)
    assert calls == []
    assert {e.condition_id for e in report.entries} == {"kg_seed_failed",
                                                        "rebuild_pending_seed"}


def test_retry_after_restart_that_hits_a_different_error_reports_that_error(monkeypatch):
    """Weaviate came back after the restart, then the seed hit a bug: the
    ledger must say what is true now (seed failure), not the stale outage."""
    attempts: list = []

    def ensure():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("not ready")
        raise RuntimeError("schema POST returned HTTP 500")

    status, calls, report = _run(ensure, monkeypatch=monkeypatch)
    assert status == iw.FAILED
    assert calls == [["podman", "start", "vco_weaviate"]]
    assert [e.condition_id for e in report.entries] == ["kg_seed_failed"]


def test_new_conditions_are_registered_with_the_declared_lifecycle():
    from vco_lib import deferral_registry as reg

    for cid in ("install_python_env_broken", "kg_seed_failed"):
        spec = reg.condition(cid)
        assert spec is not None, cid
        assert spec.condition_class == "action_required"
        assert spec.owner == "install.py"
        assert spec.clear_probe == "owned-drop-when-absent"
