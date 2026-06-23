# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.windows_reserved_ports (v0.2.64).

The genuine bug: on Windows, WinNAT / Hyper-V reserve a dynamic TCP range
(e.g. 11410-11509) that refuses host binds; VCO's 11435 (ollama) / 11440
(code_embed) ports can land inside it. The container shows "Up" but the host
port never binds and the KG goes mute. install.py's HTTP probe misses this
because NOTHING is listening — the OS just refuses the bind.

These tests pin the PURE decision logic (parse + in-range test): given sample
`netsh int ipv4 show excludedportrange tcp` output, do we correctly identify
whether a port is reserved (the act case) AND correctly do nothing when it is
not (the leave-alone case)? They run on any OS — the parse is platform-free.
The OS-coupled paths (query_excluded_ranges / reserve_port_persistent) are
covered for their soft-fail no-op contract on non-Windows.
"""
from __future__ import annotations

import sys
from pathlib import Path

# vco_lib lives at the repo root, sibling to tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vco_lib.windows_reserved_ports import (  # noqa: E402
    check_ports,
    is_windows,
    parse_excluded_ranges,
    port_in_reserved_range,
    query_excluded_ranges,
    reserve_command,
    reserve_port_persistent,
)


# Canonical "Start Port / End Port" layout (the common netsh shape). The range
# 11410-11509 swallows VCO's ollama port 11435 but NOT code_embed port 11440.
_SAMPLE_START_END = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
     11410       11434
     50000       50059
     63000       63031

* - Administered port exclusions.
"""

# The same data but where the reserved window actually covers 11435 AND 11440.
_SAMPLE_COVERS_BOTH = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
     11410       11509

* - Administered port exclusions.
"""

# "Start Port / Number of Ports" layout (newer builds / some locales): a count,
# not an end port. 11430 + 100 ports => 11430-11529 (covers 11435 and 11440).
_SAMPLE_START_COUNT = """
Protocol tcp Port Exclusion Ranges

Start Port    Number of Ports
----------    ---------------
     11430              100
"""

# Header-only / no data rows — parses to nothing.
_SAMPLE_EMPTY = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------

* - Administered port exclusions.
"""


def test_parse_start_end_layout():
    ranges = parse_excluded_ranges(_SAMPLE_START_END)
    assert ranges == [(11410, 11434), (50000, 50059), (63000, 63031)]


def test_parse_start_count_layout_expands_to_inclusive_end():
    # 11430 with count 100 => inclusive 11430..11529.
    ranges = parse_excluded_ranges(_SAMPLE_START_COUNT)
    assert ranges == [(11430, 11529)]


def test_parse_empty_and_garbage_yields_no_ranges():
    assert parse_excluded_ranges(_SAMPLE_EMPTY) == []
    assert parse_excluded_ranges("") == []
    assert parse_excluded_ranges("totally unrelated text\nno digits here") == []
    # A lone single integer (not a pair) must not parse as a range.
    assert parse_excluded_ranges("   11435   ") == []


def test_port_in_range_detects_reserved_port():
    # ACT case: 11435 sits inside the 11410-11509 window.
    ranges = parse_excluded_ranges(_SAMPLE_COVERS_BOTH)
    assert port_in_reserved_range(11435, ranges) == (11410, 11509)
    assert port_in_reserved_range(11440, ranges) == (11410, 11509)


def test_port_not_in_range_is_leave_alone():
    # LEAVE-ALONE case: with the 11410-11434 window, 11435 and 11440 are FREE,
    # so the decision is "do nothing".
    ranges = parse_excluded_ranges(_SAMPLE_START_END)
    assert port_in_reserved_range(11435, ranges) is None
    assert port_in_reserved_range(11440, ranges) is None


def test_port_in_range_boundaries_are_inclusive():
    ranges = [(11410, 11509)]
    assert port_in_reserved_range(11410, ranges) == (11410, 11509)  # start edge
    assert port_in_reserved_range(11509, ranges) == (11410, 11509)  # end edge
    assert port_in_reserved_range(11409, ranges) is None
    assert port_in_reserved_range(11510, ranges) is None


def test_start_count_layout_covers_default_ports():
    ranges = parse_excluded_ranges(_SAMPLE_START_COUNT)
    assert port_in_reserved_range(11435, ranges) == (11430, 11529)
    assert port_in_reserved_range(11440, ranges) == (11430, 11529)


def test_reserve_command_text_is_stable():
    # The warn message and the action share this exact command — pin it so the
    # printed fix can't drift from what reserve_port_persistent runs.
    assert reserve_command(11435) == (
        "netsh int ipv4 add excludedportrange protocol=tcp "
        "startport=11435 numberofports=1 store=persistent"
    )


def test_check_ports_emits_warning_and_returns_conflict(monkeypatch):
    """ACT path end-to-end (parse + decision) with the OS calls stubbed.

    Force the Windows branch and feed reserved ranges that cover 11435; assert
    check_ports surfaces the conflict and prints the actionable fix. Non-admin
    so it warns rather than mutating state.
    """
    import vco_lib.windows_reserved_ports as wrp

    monkeypatch.setattr(wrp, "is_windows", lambda: True)
    monkeypatch.setattr(wrp, "query_excluded_ranges", lambda: [(11410, 11509)])
    monkeypatch.setattr(wrp, "is_elevated", lambda: False)

    messages: list[str] = []
    unresolved = wrp.check_ports(
        [("ollama", 11435), ("code_embed", 11600)],
        log=messages.append,
    )

    # 11435 is reserved (conflict surfaced); 11600 is free (left alone).
    assert unresolved == [("ollama", 11435, (11410, 11509))]
    joined = "\n".join(messages)
    assert "ollama port 11435" in joined
    assert "startport=11435" in joined  # exact fix command was printed
    assert "11600" not in joined        # the free port produced no output


def test_check_ports_no_conflict_is_silent_leave_alone(monkeypatch):
    """LEAVE-ALONE path: every port is free of the reserved ranges → no
    warnings, no conflicts, no state change."""
    import vco_lib.windows_reserved_ports as wrp

    monkeypatch.setattr(wrp, "is_windows", lambda: True)
    # Reserved window does NOT cover 11435 / 11440.
    monkeypatch.setattr(wrp, "query_excluded_ranges", lambda: [(11410, 11434)])
    monkeypatch.setattr(wrp, "is_elevated", lambda: False)

    messages: list[str] = []
    unresolved = wrp.check_ports(
        [("ollama", 11435), ("code_embed", 11440)],
        log=messages.append,
    )
    assert unresolved == []
    assert messages == []


def test_check_ports_auto_reserves_when_elevated(monkeypatch):
    """ACT path, admin branch: a reserved port is auto-reserved and therefore
    NOT returned as an unresolved conflict."""
    import vco_lib.windows_reserved_ports as wrp

    monkeypatch.setattr(wrp, "is_windows", lambda: True)
    monkeypatch.setattr(wrp, "query_excluded_ranges", lambda: [(11410, 11509)])
    monkeypatch.setattr(wrp, "is_elevated", lambda: True)
    reserved: list[int] = []

    def _fake_reserve(port: int) -> bool:
        reserved.append(port)
        return True

    monkeypatch.setattr(wrp, "reserve_port_persistent", _fake_reserve)

    messages: list[str] = []
    unresolved = wrp.check_ports([("ollama", 11435)], log=messages.append)

    assert reserved == [11435]
    assert unresolved == []  # reservation succeeded → resolved
    assert "reserved it for application use" in "\n".join(messages)


def test_check_ports_is_noop_off_windows(monkeypatch):
    """Cross-OS safety: on non-Windows, check_ports does nothing even if a
    (hypothetical) range would match — the is_windows() gate short-circuits."""
    import vco_lib.windows_reserved_ports as wrp

    monkeypatch.setattr(wrp, "is_windows", lambda: False)
    # Even if query were called it would match; assert it is NOT called.
    def _boom():
        raise AssertionError("query_excluded_ranges must not run off Windows")

    monkeypatch.setattr(wrp, "query_excluded_ranges", _boom)
    assert wrp.check_ports([("ollama", 11435)]) == []


def test_os_coupled_helpers_softfail_off_windows():
    """On the actual test host (non-Windows in CI), the OS-coupled entry points
    soft-fail to safe no-ops without raising."""
    if is_windows():
        # On a real Windows host these may legitimately return data / True;
        # the soft-fail contract is only asserted for the non-Windows no-op.
        return
    assert query_excluded_ranges() == []
    assert reserve_port_persistent(11435) is False
