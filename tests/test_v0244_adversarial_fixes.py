"""v0.2.44 V44-F: regression tests for fix-now items from adversarial review.

Covers:
  - NUL byte in stored file_path doesn't crash _path_resolves_on_disk (Fix 1)
  - Embedded newline in stored file_path doesn't crash (Fix 1)
  - Rebind errors propagate to seed_errors (Fix 2)
  - Orphan-collection notice fires when primary_count > 0 and canonical differs (Fix 3)
  - Env/DB disagreement warning logs cleanly (Fix 4)
  - SHARED_KG_WRITE_DISABLED gate no longer blocks scope='project' writes
    when KG_COLLECTION == SHARED_KG_COLLECTION (Fix 6)
"""

import sys
from pathlib import Path


def test_path_resolves_on_disk_handles_nul_byte():
    """Fix 1: a NUL byte in file_path must not crash strategy 1."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    # Should return False, NOT raise
    result = install._path_resolves_on_disk("foo\x00bar.md")
    assert result is False


def test_path_resolves_on_disk_handles_embedded_newline():
    """Fix 1: a newline in file_path must not crash."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    result = install._path_resolves_on_disk("foo\nbar.md")
    assert result is False


def test_rebind_errors_propagate_to_seed_errors(monkeypatch, tmp_path):
    """Fix 2: when _rebind_orchestrator_root_to_canonical returns ACTUAL
    failures (not soft-fail deferrals), they MUST be extended into
    seed_errors so the audit-log warn entry fires.

    Discriminator: error strings containing "(skipped)" or "reconciled on
    next launcher boot" are deferrals (legitimate soft-fail) and stay
    non-propagated. Everything else (UPDATE failed:, import/use failed:,
    apply failed:) is a real failure and propagates.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Mock the rebind helper to return a REAL failure shape
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install,
        "_rebind_orchestrator_root_to_canonical",
        lambda canonical: ["launcher.db UPDATE failed: simulated"],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: 0)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    # Build minimal args+venv+sync_kg
    class _Args:
        update = True
    venv_py = tmp_path / "py"
    sync_kg = tmp_path / "sync"

    errs = install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=venv_py,
        sync_kg=sync_kg,
        weaviate_url="http://x",
        current_shared_kg="MyKG",
        current_kg_collection="MyKG",
    )
    assert any("UPDATE failed" in e for e in errs), \
        f"rebind real-failure error not propagated; got: {errs}"


def test_rebind_deferrals_do_not_propagate_to_seed_errors(monkeypatch, tmp_path):
    """Fix 2 inverse: soft-fail deferrals (launcher.db missing,
    'reconciled on next launcher boot') stay non-propagated so the
    existing test_rebind_soft_fails_when_launcher_db_missing contract
    holds — those signal a deliberate first-boot reconciliation path,
    not a failure."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install,
        "_rebind_orchestrator_root_to_canonical",
        lambda canonical: [
            "launcher.db: no orchestrator-root project row found (skipped)",
            "config_projection: orchestrator-root project_id not resolvable; "
            "env files will be reconciled on next launcher boot",
        ],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: 0)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _Args:
        update = True

    errs = install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="MyKG",
        current_kg_collection="MyKG",
    )
    assert errs == [], (
        f"deferral strings must not propagate; got: {errs}"
    )


def test_orphan_collection_notice_fires(monkeypatch, tmp_path, capsys):
    """Fix 3: notice prints when canonical differs AND primary has rows."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "_rebind_orchestrator_root_to_canonical", lambda c: [])
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    # primary has 192 rows, shared has 0
    def _count(_url, name):
        return 192 if name == "OldKG" else 0
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _Args:
        update = True
    install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="NewKG",      # canonical (SHARED wins)
        current_kg_collection="OldKG",  # orphan after rebind
    )
    out = capsys.readouterr().out
    assert "Orphan collection 'OldKG' retains 192 rows" in out


def test_env_db_disagreement_warning(monkeypatch, capsys, tmp_path):
    """Fix 4 (V44-G1 updated): when env value disagrees with DB value, the
    hybrid SoT resolver fires and logs its rationale.

    V44-G1 replaced V44-F's silent "disagreement" WARNING prints with an
    explicit hybrid resolver (check Weaviate → first-install heuristic).
    The rationale must appear in stdout; the V44-F warning strings must NOT.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Stub the launcher_db_reader to return a value DIFFERENT from current env.
    def _fake_bindings():
        return ("DbPrimary_KG", "DbShared_KG")
    monkeypatch.setattr(
        "vco_lib.launcher_db_reader.get_orchestrator_root_bindings",
        _fake_bindings,
    )
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    # Stub everything downstream of the resolver that could blow up.
    monkeypatch.setattr(install, "_read_app_state_key", lambda k: None)
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_compute_on_disk_content_hashes", lambda *a, **k: {})
    monkeypatch.setattr(install, "_batch_query_weaviate_content_hashes", lambda *a, **k: {})
    monkeypatch.setattr(install, "_seed_weaviate_shared_kg_only", lambda **k: [])
    monkeypatch.setattr(install, "_prune_stale_kg_rows", lambda *a, **k: None)
    # No collections exist in Weaviate → hybrid resolver hits the bootstrap
    # branch (env wins, "no candidates exist in Weaviate yet"). For richer
    # branch coverage see the four test_g1_* cases below.
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: None)

    # We need .claude/scripts/sync_knowledge_graph.py to exist for the path.
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "sync_knowledge_graph.py").write_text("# stub")
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("# stub")

    # Set env to values that DISAGREE with the stubbed DB bindings.
    monkeypatch.setenv("KG_COLLECTION", "EnvPrimary_KG")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "EnvShared_KG")
    monkeypatch.setenv("WEAVIATE_URL", "http://x")

    class _Args:
        update = True
        skip_seed = False

    # Call should not raise; we only care about stdout containing the rationale.
    try:
        install._seed_weaviate(_Args())
    except SystemExit:
        pass
    except Exception:
        # Other failures downstream are fine — we just need the rationale print
        # which happens at the V44-G1 block, before any of the failure points.
        pass

    out = capsys.readouterr().out
    # V44-G1: rationale print replaces V44-F's WARNING. The resolver always
    # emits a "hybrid SoT" line (either "resolution:" for changes or
    # "no disagreement" for agreement).
    assert "hybrid SoT" in out, (
        f"hybrid SoT rationale not printed; stdout was:\n{out}"
    )
    # Old V44-F WARNING prints are subsumed and MUST NOT reappear.
    assert "KG_COLLECTION disagreement" not in out, (
        "V44-F's removed WARNING leaked back into stdout"
    )
    assert "SHARED_KG_COLLECTION disagreement" not in out, (
        "V44-F's removed WARNING leaked back into stdout"
    )


def test_shared_kg_write_disabled_does_not_block_project_scope_on_orchestrator_root(monkeypatch):
    """Fix 6: with KG_COLLECTION == SHARED_KG_COLLECTION (orchestrator-root
    post-rebind state), SHARED_KG_WRITE_DISABLED=true must NOT block
    scope='project' writes."""
    # Ensure import path
    mcp_dir = Path(__file__).resolve().parents[1] / "claude_mcp_servers"
    if str(mcp_dir) not in sys.path:
        sys.path.insert(0, str(mcp_dir))
    # Reset env
    monkeypatch.setenv("KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_WRITE_DISABLED", "true")
    # Force module to re-evaluate by importing fresh
    if "weaviate_mcp.server" in sys.modules:
        del sys.modules["weaviate_mcp.server"]
    from weaviate_mcp import server as mcp_server
    # Inspect the source line to verify the fix landed:
    import inspect
    src = inspect.getsource(mcp_server.store_knowledge_node)
    assert 'scope == "shared"' in src, (
        "Fix 6 not applied: gate should be on scope, not on collection name match"
    )
    # And the OLD predicate must not be present:
    assert 'target_collection_name == SHARED_KG_COLLECTION' not in src, (
        "Old predicate still present"
    )


# ─── V44-G2: dual-clone WARNING tests ─────────────────────────────────────

def test_g2_dual_clone_warning_fires(monkeypatch, tmp_path, capsys):
    """G2: when launcher.db orchestrator-root project points at a DIFFERENT
    folder than PROJECT_ROOT, _check_dual_clone must return a (registered,
    current) tuple indicating the mismatch.
    """
    import sys
    import sqlite3
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Build a fixture launcher.db
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT, folder_path TEXT,
            host TEXT, created_at INT, updated_at INT, slug TEXT, rl_port INT
        )
        """
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    conn.execute(
        "INSERT INTO projects VALUES ('test-pid', 'TestProj', ?, "
        "'orchestrator_root', 0, 0, 'test', NULL)",
        (str(elsewhere),),  # registered points elsewhere
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db_path))

    # PROJECT_ROOT is tmp_path itself, not tmp_path/elsewhere
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    result = install._check_dual_clone()
    assert result is not None, (
        "expected dual-clone mismatch tuple, got None"
    )
    registered, current = result
    assert "elsewhere" in registered, (
        f"registered path should mention 'elsewhere', got {registered!r}"
    )
    assert str(tmp_path.resolve()) == current, (
        f"current path should equal resolved PROJECT_ROOT, got {current!r}"
    )


def test_g2_no_warning_when_paths_match(monkeypatch, tmp_path):
    """G2: when launcher.db folder_path matches PROJECT_ROOT, _check_dual_clone
    returns None (no warning).
    """
    import sys
    import sqlite3
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT, folder_path TEXT,
            host TEXT, created_at INT, updated_at INT, slug TEXT, rl_port INT
        )
        """
    )
    conn.execute(
        "INSERT INTO projects VALUES ('test-pid', 'TestProj', ?, "
        "'orchestrator_root', 0, 0, 'test', NULL)",
        (str(tmp_path),),  # matches PROJECT_ROOT
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db_path))
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    assert install._check_dual_clone() is None


def test_g2_no_warning_when_db_missing(monkeypatch, tmp_path):
    """G2: missing launcher.db -> returns None gracefully (no crash, no warning)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    monkeypatch.setenv(
        "VCT_LAUNCHER_DB_PATH", str(tmp_path / "nonexistent.db")
    )
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    assert install._check_dual_clone() is None


# ────────────────────────────────────────────────────────────────────────
# V44-G1: hybrid env-vs-DB SoT resolver branch coverage
# ────────────────────────────────────────────────────────────────────────


def test_g1_hybrid_resolution_picks_only_extant_collection(monkeypatch):
    """G1: when env and DB agree (no conflict), resolver short-circuits to
    the "no conflict" branch regardless of Weaviate contents."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Mock _count: env_shared=VibeCodedOrchestrator_KG exists with 1130 rows,
    # everything else returns None (nonexistent).
    def _count(_url, name):
        return 1130 if name == "VibeCodedOrchestrator_KG" else None
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)

    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="VCODev_KG",
        env_shared="VibeCodedOrchestrator_KG",
        db_primary="VCODev_KG",
        db_shared="VibeCodedOrchestrator_KG",
        weaviate_url="http://x",
    )
    # env_kg == db_primary AND env_shared == db_shared → no disagreement.
    assert "no conflict" in rat
    assert kg == "VCODev_KG"
    assert sh == "VibeCodedOrchestrator_KG"


def test_g1_hybrid_picks_extant_when_env_db_disagree(monkeypatch):
    """G1: env=X, DB=Y, only Y exists in Weaviate → pick Y (with rows wins)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    def _count(_url, name):
        return 1130 if name == "DbWinner_KG" else None  # only DB's value exists
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)

    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="EnvLoser_KG",
        env_shared="EnvLoserShared_KG",
        db_primary="DbWinner_KG",
        db_shared="DbWinner_KG",
        weaviate_url="http://x",
    )
    assert kg == "DbWinner_KG"
    assert sh == "DbWinner_KG"
    assert "weaviate" in rat


def test_g1_first_install_heuristic_env_wins(monkeypatch):
    """G1: env=X, DB=Y, BOTH exist with rows (true tie) → env wins on fresh
    install (app_state.last_installed_kg_collection UNSET)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    def _count(_url, name):
        return 500  # every candidate exists with rows
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)
    # Fresh install — app_state key UNSET.
    monkeypatch.setattr(install, "_read_app_state_key", lambda k: None)

    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="EnvWinner_KG",
        env_shared="EnvWinner_KG",
        db_primary="DbValue_KG",
        db_shared="DbValue_KG",
        weaviate_url="http://x",
    )
    assert kg == "EnvWinner_KG"
    assert sh == "EnvWinner_KG"
    assert "fresh install" in rat or "env" in rat.lower()


def test_g1_subsequent_update_db_wins(monkeypatch):
    """G1: env=X, DB=Y, BOTH exist with rows (true tie) → DB wins on
    subsequent update (app_state.last_installed_kg_collection SET)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    def _count(_url, name):
        return 500  # every candidate exists with rows
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)
    # Subsequent update — app_state key SET.
    monkeypatch.setattr(
        install, "_read_app_state_key", lambda k: "VibeCodedOrchestrator_KG",
    )

    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="EnvStale_KG",
        env_shared="EnvStale_KG",
        db_primary="DbWinner_KG",
        db_shared="DbWinner_KG",
        weaviate_url="http://x",
    )
    assert kg == "DbWinner_KG"
    assert sh == "DbWinner_KG"
    assert "subsequent update" in rat or "db" in rat.lower()


# ────────────────────────────────────────────────────────────────────────
# V44-H: synthesizer fix-now items (post-5+2+1 pre-tag review)
# ────────────────────────────────────────────────────────────────────────


def test_h1_weaviate_unreachable_defers_rebind(monkeypatch):
    """H1: when all candidates return None from Weaviate (unreachable),
    resolver returns a 'deferred:' rationale and DB-recorded names as
    last-known-good fallback."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda _u, _n: None)
    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="EnvKG",
        env_shared="EnvShared",
        db_primary="DbKG",
        db_shared="DbShared",
        weaviate_url="http://x",
    )
    assert rat.startswith("deferred:"), f"expected deferred rationale, got {rat!r}"
    # DB-recorded names win as fallback when set (last known good state).
    assert kg == "DbKG"
    assert sh == "DbShared"


def test_h2_orphan_notice_fires_when_env_reassigned(
    monkeypatch, capsys, tmp_path,
):
    """H2: when G1 reassigns current_kg_collection, the orphan notice in
    _seed_weaviate_shared_kg_only must still fire for the PRE-resolution
    name (passed via the new orphan_candidate_kg kwarg)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install, "_rebind_orchestrator_root_to_canonical", lambda c: [],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)

    def _count(_u, name):
        return 100 if name == "OldKG" else 0

    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _A:
        update = True

    install._seed_weaviate_shared_kg_only(
        args=_A(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="NewKG",
        current_kg_collection="NewKG",  # already reassigned by G1
        orphan_candidate_kg="OldKG",    # pre-reassignment name
    )
    out = capsys.readouterr().out
    assert "Orphan collection 'OldKG' retains 100 rows" in out


def test_h3_path_traversal_blocked():
    """H3: _path_resolves_on_disk rejects path traversal that escapes
    PROJECT_ROOT. /etc/passwd exists on Linux but is outside the project
    tree — the orphan-prune must be allowed to remove a row pointing at
    such a value."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    # /etc/passwd almost certainly exists on Linux but isn't inside PROJECT_ROOT
    assert install._path_resolves_on_disk("../../../etc/passwd") is False
    # Worktree-prefix branch also blocked when the stripped path traverses
    assert install._path_resolves_on_disk(
        ".claude/worktrees/agent-x/../../../../../../etc/passwd"
    ) is False


def test_h4_all_empty_resolver_inputs_bootstrap_rationale():
    """H4: when all 4 inputs are empty/None, return bootstrap rationale
    with empty canonical names (no empty-string propagation into
    downstream GraphQL)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    kg, sh, rat = install._resolve_orchestrator_root_canonical(
        env_kg="",
        env_shared="",
        db_primary=None,
        db_shared=None,
        weaviate_url="http://x",
    )
    assert kg == ""
    assert sh == ""
    assert "bootstrap" in rat.lower()


def test_h5_dual_clone_blocks_rebind(monkeypatch):
    """H5: dual-clone detection short-circuits
    _rebind_orchestrator_root_to_canonical with a (skipped — ...) error
    tail that the V44-F seed_errors deferral discriminator recognizes."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    # Force the module-level flag set by _seed_weaviate's dual-clone block.
    monkeypatch.setattr(
        install, "_DUAL_CLONE_DETECTED_THIS_RUN", True, raising=False,
    )
    errors = install._rebind_orchestrator_root_to_canonical("CanonicalKG")
    assert any("dual-clone" in e.lower() for e in errors), (
        f"expected dual-clone error in {errors!r}"
    )
    assert any("(skipped" in e for e in errors), (
        "(skipped) marker required for V44-F discriminator"
    )
