# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.52 V52-AJ: env → launcher.db → default embedding-resolution chain.

Validates the SHIP-BLOCKER fix for Fabio's Windows + CPU stuck-at-40%
install: install.py's sync_knowledge_graph.py subprocess must inherit
ACTIVE_EMBEDDING from launcher.db's app_state[embedding.active_profile]
when the user shell has no env override.

Three code paths covered:
  - install.py side: _resolve_active_embedding_for_install,
    _subprocess_env_with_embedding, _model_id_for_active.
  - EmbeddingService side: _resolve_active_embedding,
    _model_id_for_active, for_project() integration.
  - Threading: subprocess.run env= argument carries the resolved values.

All tests are pure-unit (no real Weaviate, no real Ollama, no real
sqlite outside tmp_path). The shared _make_launcher_db fixture mirrors
what the launcher's app_state migration produces — only the columns we
read (key, value, updated_at).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _make_launcher_db(tmp_dir: Path, *, active: str | None = None) -> Path:
    """Create a launcher.db inside tmp_dir with app_state seeded.

    Lives at ``<tmp_dir>/.vct/launcher.db`` so the install.py path
    discovery (driven by VCT_STATE_DIR) resolves to it. Pass
    ``active=None`` for "no key present"; ``active=""`` for "key
    present but empty"; ``active="<profile>"`` for the populated case.
    """
    state_dir = tmp_dir / ".vct"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_APP_STATE_SCHEMA)
        if active is not None:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?)",
                ("embedding.active_profile", active, 1000000),
            )
            conn.commit()
    finally:
        conn.close()
    return db_path


class IsolatedEnvMixin:
    """Per-test env isolation: clear ACTIVE_EMBEDDING + EMBEDDING_MODEL +
    VCT_STATE_DIR + VCT_LAUNCHER_DB_PATH; restore in tearDown.

    Without this mixin, the developer's real $HOME / $VCT_STATE_DIR
    bleeds into the tests and you get spurious passes (or failures)
    based on whatever launcher.db happens to exist locally.
    """

    def setUp(self) -> None:  # noqa: N802 — unittest naming
        super().setUp()  # type: ignore[misc]
        self._saved_env = {}
        for key in (
            "ACTIVE_EMBEDDING",
            "EMBEDDING_MODEL",
            "VCT_STATE_DIR",
            "VCT_LAUNCHER_DB_PATH",
            "HOME",
            "USERPROFILE",
        ):
            self._saved_env[key] = os.environ.pop(key, None)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        # Point both Linux/mac ($HOME) and Windows ($USERPROFILE) at tmp_path
        # so any unrelated Path.home() call doesn't escape into the real $HOME.
        os.environ["HOME"] = str(self.tmp_path)
        os.environ["USERPROFILE"] = str(self.tmp_path)
        os.environ["VCT_STATE_DIR"] = str(self.tmp_path / ".vct")

    def tearDown(self) -> None:  # noqa: N802 — unittest naming
        self._tmpdir.cleanup()
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        super().tearDown()  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────────
# Test 1: install.py threads ACTIVE_EMBEDDING into subprocess.run env
# ────────────────────────────────────────────────────────────────────────────


class TestInstallPyThreadsActiveEmbedding(IsolatedEnvMixin, unittest.TestCase):
    """When launcher.db has app_state[embedding.active_profile]=arctic and
    the install shell has NO env, _subprocess_env_with_embedding() must
    return an env dict carrying ACTIVE_EMBEDDING=arctic +
    EMBEDDING_MODEL=snowflake-arctic-embed2:latest.

    This is the heart of the V52-AJ fix — without it, the subprocess
    inherits a bare os.environ.copy() and silently falls back to qwen3.
    """

    def test_install_py_threads_active_embedding_to_subprocess(self) -> None:
        _make_launcher_db(self.tmp_path, active="arctic")
        # Fresh import to avoid the previous test's module-level state
        # (install.py is large; importing it once per test class is fine).
        import install as install_mod

        env = install_mod._subprocess_env_with_embedding()

        self.assertEqual(
            env.get("ACTIVE_EMBEDDING"),
            "arctic",
            "Subprocess env must carry launcher.db's active_profile when "
            "the install shell has no ACTIVE_EMBEDDING set",
        )
        self.assertEqual(
            env.get("EMBEDDING_MODEL"),
            "snowflake-arctic-embed2:latest",
            "EMBEDDING_MODEL must be derived from the resolved active profile",
        )

    def test_install_py_preserves_other_env_keys(self) -> None:
        """The threading helper must NOT clobber unrelated env vars
        (PATH, HOME, KG_COLLECTION, etc.) — it overlays only the
        embedding-related keys."""
        _make_launcher_db(self.tmp_path, active="arctic")
        os.environ["KG_COLLECTION"] = "TestCollection_KG"
        os.environ["SOMETHING_ELSE"] = "preserved"
        import install as install_mod

        env = install_mod._subprocess_env_with_embedding()

        self.assertEqual(env.get("KG_COLLECTION"), "TestCollection_KG")
        self.assertEqual(env.get("SOMETHING_ELSE"), "preserved")
        self.assertEqual(env.get("ACTIVE_EMBEDDING"), "arctic")


# ────────────────────────────────────────────────────────────────────────────
# Test 2: EmbeddingService falls back to app_state when env is empty
# ────────────────────────────────────────────────────────────────────────────


class TestEmbeddingServiceLauncherDbFallback(IsolatedEnvMixin, unittest.TestCase):
    """EmbeddingService._resolve_active_embedding() must consult launcher.db
    when ACTIVE_EMBEDDING env is absent. This is the MCP-side defense — if
    install.py forgets to thread env (or a future caller spawns
    EmbeddingService.for_project from a stripped env), the launcher.db
    fallback still resolves to the right profile.
    """

    def test_falls_back_to_app_state_when_env_empty(self) -> None:
        _make_launcher_db(self.tmp_path, active="arctic")
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "arctic")

    def test_falls_back_to_default_when_both_empty(self) -> None:
        # No launcher.db file, no env — must default to qwen3.
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "qwen3")

    def test_strips_and_lowercases_db_value(self) -> None:
        # The DB stored value may have extra whitespace or capitalisation
        # (legacy migrations, user-edited rows). Normalise to canonical
        # form before returning.
        _make_launcher_db(self.tmp_path, active="  Arctic  ")
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "arctic")


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Env wins over launcher.db
# ────────────────────────────────────────────────────────────────────────────


class TestEnvWinsOverAppState(IsolatedEnvMixin, unittest.TestCase):
    """When BOTH the env and launcher.db have a value, env must win.

    Rationale: an explicit ``ACTIVE_EMBEDDING=qwen3`` in the install
    shell is a deliberate override (e.g. a power-user testing a model
    swap before committing it to the launcher GUI). The fallback only
    fires when the env is empty.
    """

    def test_env_wins_over_app_state_resolve(self) -> None:
        _make_launcher_db(self.tmp_path, active="arctic")
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(
            result,
            "qwen3",
            "Env-explicit ACTIVE_EMBEDDING must beat launcher.db",
        )

    def test_env_wins_in_install_py_helper_too(self) -> None:
        """install.py's helper must apply the same env-wins rule."""
        _make_launcher_db(self.tmp_path, active="arctic")
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        import install as install_mod

        env = install_mod._subprocess_env_with_embedding()
        self.assertEqual(env.get("ACTIVE_EMBEDDING"), "qwen3")
        # EMBEDDING_MODEL should be derived from the env-explicit
        # active, NOT the launcher.db value.
        self.assertEqual(env.get("EMBEDDING_MODEL"), "qwen3-embedding:0.6b")


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Soft-fail when launcher.db is unreachable
# ────────────────────────────────────────────────────────────────────────────


class TestSoftFailWhenLauncherDbUnreachable(IsolatedEnvMixin, unittest.TestCase):
    """Free-tier installs / pre-launcher-boot installs / corrupt-db cases
    must NEVER block install.py. The resolution chain falls through to
    qwen3 (the safe default) without raising.
    """

    def test_no_launcher_db_file(self) -> None:
        # tmp_path/.vct/ does NOT exist — no launcher.db at all.
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "qwen3")

    def test_install_py_returns_unchanged_env_when_no_db_no_env(self) -> None:
        # Both env and launcher.db are empty. install.py's helper must
        # leave ACTIVE_EMBEDDING / EMBEDDING_MODEL UNSET (not write
        # "qwen3" explicitly) so the subprocess uses its own default
        # — this preserves the pre-V52-AJ behaviour for free-tier installs.
        import install as install_mod

        env = install_mod._subprocess_env_with_embedding()
        self.assertNotIn(
            "ACTIVE_EMBEDDING", env,
            "When neither env nor db has a value, install.py must not "
            "inject ACTIVE_EMBEDDING — let the subprocess use its own default",
        )
        self.assertNotIn(
            "EMBEDDING_MODEL", env,
            "Same rationale: don't inject EMBEDDING_MODEL when nothing resolves",
        )

    def test_launcher_db_exists_but_key_absent(self) -> None:
        # launcher.db exists with the app_state table but no
        # embedding.active_profile row (fresh install where the user
        # has not yet opened the Identity tab). Must fall through to
        # qwen3.
        _make_launcher_db(self.tmp_path, active=None)
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "qwen3")

    def test_launcher_db_exists_but_value_empty(self) -> None:
        # The key is present but stores an empty string (legacy row,
        # or a user explicitly cleared it from the GUI). Treat as
        # "unset" and fall through to qwen3.
        _make_launcher_db(self.tmp_path, active="   ")
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "qwen3")

    def test_corrupt_launcher_db(self) -> None:
        # Write garbage bytes where launcher.db should be. SQLite will
        # either fail to open or fail on the first query; the reader
        # must soft-fail and return None → resolver falls back to qwen3.
        state_dir = self.tmp_path / ".vct"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "launcher.db").write_bytes(b"not a sqlite db at all")
        from vco_lib.embedding_service import _resolve_active_embedding

        result = _resolve_active_embedding()
        self.assertEqual(result, "qwen3")


# ────────────────────────────────────────────────────────────────────────────
# Test 5: _model_id_for_active mapping (both implementations)
# ────────────────────────────────────────────────────────────────────────────


class TestModelIdForActiveMapping(unittest.TestCase):
    """Both install.py and embedding_service.py ship a copy of this
    mapping (install.py needs it before vco_lib is importable in some
    bootstrap edge-cases). They MUST agree byte-for-byte — drift between
    the two would mean install.py threads ``arctic`` but the subprocess
    picks a different model id, defeating the whole fix.
    """

    def test_arctic(self) -> None:
        import install as install_mod
        from vco_lib.embedding_service import _model_id_for_active as svc_map

        expected = "snowflake-arctic-embed2:latest"
        self.assertEqual(install_mod._model_id_for_active("arctic"), expected)
        self.assertEqual(svc_map("arctic"), expected)

    def test_qwen3(self) -> None:
        import install as install_mod
        from vco_lib.embedding_service import _model_id_for_active as svc_map

        expected = "qwen3-embedding:0.6b"
        self.assertEqual(install_mod._model_id_for_active("qwen3"), expected)
        self.assertEqual(svc_map("qwen3"), expected)

    def test_openai(self) -> None:
        import install as install_mod
        from vco_lib.embedding_service import _model_id_for_active as svc_map

        expected = "text-embedding-3-small"
        self.assertEqual(install_mod._model_id_for_active("openai"), expected)
        self.assertEqual(svc_map("openai"), expected)

    def test_unknown_falls_back_to_qwen3(self) -> None:
        # Defensive default — anything we don't recognise picks the
        # only-always-present-on-fresh-install text embedder.
        import install as install_mod
        from vco_lib.embedding_service import _model_id_for_active as svc_map

        expected = "qwen3-embedding:0.6b"
        self.assertEqual(install_mod._model_id_for_active("nosuchprofile"), expected)
        self.assertEqual(svc_map("nosuchprofile"), expected)

    def test_case_insensitive(self) -> None:
        # ARCTIC, Arctic, arctic must all resolve identically (the
        # launcher's GUI stores lowercase but old migrations / hand-
        # edited rows may have mixed case).
        import install as install_mod
        from vco_lib.embedding_service import _model_id_for_active as svc_map

        expected = "snowflake-arctic-embed2:latest"
        for variant in ("ARCTIC", "Arctic", "  arctic  "):
            self.assertEqual(install_mod._model_id_for_active(variant), expected)
            self.assertEqual(svc_map(variant), expected)


# ────────────────────────────────────────────────────────────────────────────
# Integration smoke: subprocess.run env= carries the resolved values
# ────────────────────────────────────────────────────────────────────────────


class TestSubprocessRunCarriesThreadedEnv(IsolatedEnvMixin, unittest.TestCase):
    """End-to-end: when install.py invokes subprocess.run with
    env=_subprocess_env_with_embedding(), the launcher.db's
    embedding.active_profile must surface in the child process's
    os.environ. Mocks subprocess.run to inspect what install.py would
    have passed without actually spawning anything.
    """

    def test_subprocess_env_carries_arctic_from_launcher_db(self) -> None:
        _make_launcher_db(self.tmp_path, active="arctic")
        import install as install_mod

        # Simulate the install.py call site: build the env, then pass
        # it to a fake subprocess.run. We verify what env= would carry.
        env = install_mod._subprocess_env_with_embedding()

        captured_env: dict = {}

        def fake_run(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))

            class FakeResult:
                returncode = 0
                stdout = b""
                stderr = b""

            return FakeResult()

        with mock.patch.object(install_mod.subprocess, "run", side_effect=fake_run):
            install_mod.subprocess.run(
                ["echo", "hello"], env=env, check=False
            )

        self.assertEqual(captured_env.get("ACTIVE_EMBEDDING"), "arctic")
        self.assertEqual(
            captured_env.get("EMBEDDING_MODEL"),
            "snowflake-arctic-embed2:latest",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
