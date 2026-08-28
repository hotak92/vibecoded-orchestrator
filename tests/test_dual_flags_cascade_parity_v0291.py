# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-L — 3-way lockstep for the dual-flag host-wide-default cascade.

Plan decision #22 added an INSTALL-WIDE default tier to the three dual flags
(``dual_embedding_write_all_slots`` / ``dual_rl_log_enabled`` /
``dual_embedding_arctic_secondary``). Three surfaces resolve those flags and
MUST agree, or the GUI shows one thing while the hub serves another (the
Defect-D class):

  1. **Launcher / GUI** — ``Db::resolve_dual_flags`` in
     ``launcher/src-tauri/vct-launcher-core/src/db/settings.rs``. THE home.
  2. **Hub ``/config`` resolver** — ``vct-hub/src/config_api.rs``. Same crate,
     so it CALLS (1); there is no second implementation to drift.
  3. **Python env projection** —
     ``vco_lib/config_projection.py::_resolve_dual_flags_cascade``. A genuine
     mirror: separate process, separate language, no interpreter on the
     projection hot path (tier C of CLAUDE.md's A>B>C rule). This file is its
     lock.

What is pinned here:

  * the ADDRESSING table (module_id / setting_key / app_state key per flag) is
    byte-identical between the Rust const block and the Python literals;
  * the hub really delegates rather than keeping its own copy;
  * the Python resolver produces the Rust truth table's answer for every case,
    including the two the wording of decision #22 does not cover — an explicit
    per-project ``false`` under a host-wide ``true``, and the CROSS-TIER
    log⟹write clamp;
  * the Rust unit tests for those two cases still exist (a mirror lock is
    worthless if one side quietly deletes its half).
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import config_projection as cp  # noqa: E402

_RUST_CASCADE_SRC = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "settings.rs"
)
_RUST_HUB_SRC = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "config_api.rs"
)


def _rust_const(name: str) -> str:
    """Parse ``pub const <name>: &str = "<value>";`` out of the Rust home."""
    text = _RUST_CASCADE_SRC.read_text(encoding="utf-8")
    m = re.search(rf'pub const {re.escape(name)}\s*:\s*&str\s*=\s*"([^"]+)"\s*;', text)
    assert m, f"could not parse Rust const {name} from {_RUST_CASCADE_SRC}"
    return m.group(1)


# ─────────────────────────────────────────────────────────────────────
# 1. Addressing table parity (Rust consts ↔ Python literals)
# ─────────────────────────────────────────────────────────────────────


def test_rust_and_python_sources_present() -> None:
    assert _RUST_CASCADE_SRC.is_file(), f"missing Rust cascade at {_RUST_CASCADE_SRC}"
    assert _RUST_HUB_SRC.is_file(), f"missing hub resolver at {_RUST_HUB_SRC}"


@pytest.mark.parametrize(
    ("rust_const", "python_value"),
    [
        ("APP_STATE_KEY_DUAL_WRITE_DEFAULT", cp.APP_STATE_KEY_DUAL_WRITE_DEFAULT),
        ("APP_STATE_KEY_DUAL_RL_LOG_DEFAULT", cp.APP_STATE_KEY_DUAL_RL_LOG_DEFAULT),
        ("APP_STATE_KEY_DUAL_ARCTIC_DEFAULT", cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT),
        ("SETTING_KEY_DUAL_WRITE_ALL_SLOTS", cp._DUAL_SETTING_KEY_WRITE_ALL_SLOTS),
        ("SETTING_KEY_DUAL_RL_LOG", cp._DUAL_SETTING_KEY_RL_LOG),
        ("SETTING_KEY_DUAL_ARCTIC_SECONDARY", cp._DUAL_SETTING_KEY_ARCTIC_SECONDARY),
        (
            "DUAL_FLAG_MODULE_ID_ORCHESTRATOR_CORE",
            cp.ORCHESTRATOR_CORE_MODULE_ID,
        ),
        ("DUAL_FLAG_MODULE_ID_RL_RERANKER", cp._RL_RERANKER_MODULE_ID),
    ],
)
def test_addressing_constants_match_rust(rust_const: str, python_value: str) -> None:
    assert _rust_const(rust_const) == python_value, (
        f"{rust_const} drifted between the Rust cascade and the Python mirror — "
        "a renamed key silently splits the two resolvers"
    )


def test_app_state_keys_are_the_decision_22_names() -> None:
    """Decision #22 fixed these three names. Pin them so a 'tidier' rename
    cannot orphan every host-wide default a user has already set."""
    assert cp.APP_STATE_KEY_DUAL_WRITE_DEFAULT == "embedding.dual_write_default"
    assert cp.APP_STATE_KEY_DUAL_RL_LOG_DEFAULT == "embedding.dual_rl_log_default"
    assert cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT == "embedding.dual_arctic_default"


# ─────────────────────────────────────────────────────────────────────
# 2. The hub DELEGATES — it must not grow a third implementation
# ─────────────────────────────────────────────────────────────────────


def test_hub_resolver_calls_the_shared_cascade() -> None:
    src = _RUST_HUB_SRC.read_text(encoding="utf-8")
    assert "resolve_dual_flags(&project.id)" in src, (
        "the hub /config resolver must call the shared cascade "
        "(Db::resolve_dual_flags), not re-derive the flags"
    )


def test_hub_resolver_has_no_raw_dual_flag_row_reads() -> None:
    """The pre-WP-L shape was ``get_setting(.., "dual_*").unwrap_or(false)``,
    which collapses "no per-project row" into false and cannot express the
    host-wide default tier. It must not come back."""
    src = _RUST_HUB_SRC.read_text(encoding="utf-8")
    # Ignore the test module: its fixtures legitimately seed raw rows.
    body = src.split("mod tests {")[0]
    for key in (
        cp._DUAL_SETTING_KEY_WRITE_ALL_SLOTS,
        cp._DUAL_SETTING_KEY_RL_LOG,
        cp._DUAL_SETTING_KEY_ARCTIC_SECONDARY,
    ):
        assert f'get_setting(&project.id, "orchestrator-core", "{key}"' not in body
        assert f'get_setting(&project.id, "vct-rl-reranker", "{key}"' not in body


# ─────────────────────────────────────────────────────────────────────
# 3. The shared truth table, driven through the Python mirror
# ─────────────────────────────────────────────────────────────────────
#
# (write_explicit, log_explicit, arctic_explicit,
#  write_global, log_global, arctic_global)
#   → (write_effective, log_effective, arctic_effective)
#
# The Rust half of this table lives in
# `settings.rs::dual_flag_cascade_tests` (case1..case9). Both halves must be
# updated together; `test_rust_keeps_its_half_of_the_truth_table` below is the
# tripwire for deleting one.
_TRUTH_TABLE: list[tuple[str, tuple, tuple[bool, bool, bool]]] = [
    (
        "case1 — nothing set anywhere",
        (None, None, None, False, False, False),
        (False, False, False),
    ),
    (
        "case2 — no project rows, host-wide defaults on",
        (None, None, None, True, True, True),
        (True, True, True),
    ),
    (
        "case3 — explicit project true beats host-wide false",
        (True, True, True, False, False, False),
        (True, True, True),
    ),
    (
        "case4 — explicit project FALSE beats host-wide TRUE",
        (False, False, False, True, True, True),
        (False, False, False),
    ),
    (
        "case4b — mixed: explicit write off, explicit arctic on, host-wide on",
        (False, None, True, True, True, True),
        (False, False, True),
    ),
    (
        "case8 — CROSS-TIER CLAMP: host-wide log on, project write off",
        (False, None, None, True, True, False),
        (False, False, False),
    ),
    (
        "case8b — inherited log true never promotes write",
        (None, None, None, False, True, False),
        (False, False, False),
    ),
    (
        "explicit log true still clamps to an explicit write false",
        (False, True, None, False, False, False),
        (False, False, False),
    ),
    (
        "arctic is independent of the clamp",
        (False, True, True, False, False, False),
        (False, False, True),
    ),
]


def _cascade_conn(
    tmp_path: Path,
    *,
    write_explicit: bool | None,
    log_explicit: bool | None,
    arctic_explicit: bool | None,
    write_global: bool,
    log_global: bool,
    arctic_global: bool,
) -> sqlite3.Connection:
    """A launcher.db slim enough for the cascade, seeded to one case."""
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE module_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            module_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL
        );
        CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    rows = [
        (
            write_explicit,
            cp.ORCHESTRATOR_CORE_MODULE_ID,
            cp._DUAL_SETTING_KEY_WRITE_ALL_SLOTS,
        ),
        (log_explicit, cp._RL_RERANKER_MODULE_ID, cp._DUAL_SETTING_KEY_RL_LOG),
        (
            arctic_explicit,
            cp.ORCHESTRATOR_CORE_MODULE_ID,
            cp._DUAL_SETTING_KEY_ARCTIC_SECONDARY,
        ),
    ]
    for value, module_id, key in rows:
        if value is None:
            continue
        conn.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, "
            "setting_value) VALUES (?, ?, ?, ?)",
            ("p1", module_id, key, "true" if value else "false"),
        )
    for value, key in (
        (write_global, cp.APP_STATE_KEY_DUAL_WRITE_DEFAULT),
        (log_global, cp.APP_STATE_KEY_DUAL_RL_LOG_DEFAULT),
        (arctic_global, cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT),
    ):
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (key, "true" if value else "false"),
        )
    conn.commit()
    return conn


@pytest.mark.parametrize(
    ("label", "seed", "expected"),
    [(label, seed, expected) for label, seed, expected in _TRUTH_TABLE],
    ids=[label.split(" —")[0] for label, _, _ in _TRUTH_TABLE],
)
def test_python_cascade_matches_the_shared_truth_table(
    tmp_path: Path,
    label: str,
    seed: tuple,
    expected: tuple[bool, bool, bool],
) -> None:
    conn = _cascade_conn(
        tmp_path,
        write_explicit=seed[0],
        log_explicit=seed[1],
        arctic_explicit=seed[2],
        write_global=seed[3],
        log_global=seed[4],
        arctic_global=seed[5],
    )
    try:
        got = cp._resolve_dual_flags_cascade(conn, "p1")
    finally:
        conn.close()
    assert got == expected, f"{label}: expected {expected}, got {got}"


def test_rust_keeps_its_half_of_the_truth_table() -> None:
    """A mirror lock is worthless if one side deletes its half. Pin the Rust
    test names for the two cases nothing else would catch."""
    src = _RUST_CASCADE_SRC.read_text(encoding="utf-8")
    for name in (
        "case4_explicit_project_false_beats_global_true",
        "case8_cross_tier_clamp_global_log_on_project_write_off",
        "case8b_inherited_log_true_never_forces_write_true",
        "case5_clearing_the_row_returns_to_inheriting",
    ):
        assert f"fn {name}(" in src, (
            f"Rust cascade test {name} is gone — the Python mirror above is "
            "now unlocked on that case"
        )


# ─────────────────────────────────────────────────────────────────────
# 4. Malformed / edge values resolve identically on both sides
# ─────────────────────────────────────────────────────────────────────


def test_malformed_project_row_is_treated_as_absent(tmp_path: Path) -> None:
    """Matches ``Db::dual_flag_explicit``: a non-bool row is ABSENT, not
    fail-open-true. These flags are opt-in cost multipliers."""
    conn = _cascade_conn(
        tmp_path,
        write_explicit=None,
        log_explicit=None,
        arctic_explicit=None,
        write_global=False,
        log_global=False,
        arctic_global=False,
    )
    conn.execute(
        "INSERT INTO module_settings (project_id, module_id, setting_key, "
        "setting_value) VALUES (?, ?, ?, ?)",
        (
            "p1",
            cp.ORCHESTRATOR_CORE_MODULE_ID,
            cp._DUAL_SETTING_KEY_ARCTIC_SECONDARY,
            '"yes"',
        ),
    )
    conn.commit()
    try:
        assert cp._resolve_dual_flags_cascade(conn, "p1") == (False, False, False)
    finally:
        conn.close()


def test_global_default_parses_only_true_and_one(tmp_path: Path) -> None:
    """``Db::app_state_get_bool`` is ``matches!(v, "true" | "1")`` — NOT a
    generic truthy parse. The Python mirror must reject "yes" the same way,
    or the two tiers disagree on a hand-edited row."""
    conn = _cascade_conn(
        tmp_path,
        write_explicit=None,
        log_explicit=None,
        arctic_explicit=None,
        write_global=False,
        log_global=False,
        arctic_global=False,
    )
    try:
        conn.execute(
            "UPDATE app_state SET value = 'yes' WHERE key = ?",
            (cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT,),
        )
        conn.commit()
        assert cp._resolve_dual_flags_cascade(conn, "p1")[2] is False

        conn.execute(
            "UPDATE app_state SET value = '1' WHERE key = ?",
            (cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT,),
        )
        conn.commit()
        assert cp._resolve_dual_flags_cascade(conn, "p1")[2] is True
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# 5. Plan §F #25 — collection is MODULE-INDEPENDENT
# ─────────────────────────────────────────────────────────────────────


def test_rl_log_resolution_ignores_module_installs(tmp_path: Path) -> None:
    """``dual_rl_log_enabled`` is addressed under module_id
    ``vct-rl-reranker``, but the paid module gates RERANKING, never
    COLLECTION. The cascade must not consult ``module_installs`` or any
    enabled flag — the act/leave-alone pair for a check that must NOT exist.
    """
    conn = _cascade_conn(
        tmp_path,
        write_explicit=True,
        log_explicit=True,
        arctic_explicit=None,
        write_global=False,
        log_global=False,
        arctic_global=False,
    )
    try:
        without_module = cp._resolve_dual_flags_cascade(conn, "p1")
        # Now add the harshest "module not available here" shape the DB can
        # hold: an explicit per-project disable row for the module.
        conn.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, "
            "setting_value) VALUES (?, ?, ?, ?)",
            ("p1", cp._RL_RERANKER_MODULE_ID, "enabled_for_project", "false"),
        )
        conn.commit()
        with_disabled_module = cp._resolve_dual_flags_cascade(conn, "p1")
    finally:
        conn.close()

    assert without_module == with_disabled_module == (True, True, False), (
        "telemetry collection must not depend on the RL module being "
        "installed or enabled"
    )


def test_python_cascade_source_has_no_module_installs_read() -> None:
    """Static tripwire: the cascade's source must never learn to read
    ``module_installs`` (the docstring is allowed to NAME the table — it says
    why the read is forbidden; a SQL read of it is what must not appear)."""
    src = Path(cp.__file__).read_text(encoding="utf-8")
    start = src.index("def _resolve_dual_flags_cascade(")
    end = src.index("def _fetch_kg_bindings(", start)
    body = src[start:end]
    assert "FROM module_installs" not in body
    assert "from module_installs" not in body
    assert "enabled_for_project" not in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
